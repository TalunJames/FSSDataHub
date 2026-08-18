"""Background loop: toggle, schedule, and burst crawls."""

import datetime
import threading
import time
import traceback

from taxdb import db, ingest, ledger, packets, seed as seedmod, sources, coverage
from taxdb.vocab import CATEGORIES

from . import check, crawl, extract, intake, store
from .settings import VALID_CATEGORIES, VALID_KINDS

_cancel = threading.Event()
_burst = threading.Event()
_burst_size = 20
_thread = None
_lock = threading.Lock()
_job = threading.Lock()
_status = {
    "state": "idle",
    "mode": None,
    "run_id": None,
    "current_geoid": None,
    "current_name": None,
    "message": "",
}


def snapshot():
    with _lock:
        return dict(_status)


def _set(**kwargs):
    with _lock:
        _status.update(kwargs)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _cancel.clear()
    _thread = threading.Thread(target=_loop, name="collector-worker", daemon=True)
    _thread.start()


def request_stop():
    _cancel.set()
    conn = store.connect()
    try:
        store.put(conn, "continuous_enabled", "0")
        conn.commit()
    finally:
        conn.close()
    _set(message="stop requested")


def resume():
    _cancel.clear()


def _process_next_intake():
    """Drain one operator-supplied source. Returns True if work was done."""
    if not _job.acquire(blocking=False):
        return False
    conn = None
    try:
        conn = store.connect()
        row = intake.next_queued(conn)
        if not row:
            return False
        s = store.get_all(conn)
        _set(state="running", mode="intake", current_geoid=row["geoid"],
             current_name="intake #%s %s" % (row["id"], row["filename"] or row["url"] or row["kind"]))
        n = intake.process_item(conn, s, row)
        _set(state="idle", message="intake #%s wrote %d finding(s)" % (row["id"], n),
             current_geoid=None, current_name=None)
        return True
    except Exception as exc:
        _set(state="error", message=str(exc)[:500])
        return True
    finally:
        if conn is not None:
            conn.close()
        _job.release()


def request_burst(size):
    global _burst_size
    _burst_size = max(1, int(size))
    _cancel.clear()
    _burst.set()


def _loop():
    last_stale_sweep = 0.0
    while True:
        try:
            now = time.monotonic()
            if now - last_stale_sweep > 600:
                # Return items stranded at in_progress by a crash or restart.
                last_stale_sweep = now
                conn = store.connect()
                try:
                    ledger.release_stale(conn, hours=2)
                finally:
                    conn.close()
            if _process_next_intake():
                continue
            if _burst.is_set():
                if _run_batch("burst", _burst_size):
                    _burst.clear()
                continue
            conn = store.connect()
            try:
                s = store.get_all(conn)
            finally:
                conn.close()
            if store.as_bool(s.get("continuous_enabled")):
                # Claim a full batch per run so the run table stays readable;
                # the cancel flag is still checked between items.
                _run_batch("continuous", store.as_int(s.get("burst_size"), 20))
                if (snapshot().get("message") or "").startswith("queue empty"):
                    time.sleep(8)
                continue
            if _schedule_due(s):
                _mark_scheduled()
                _run_batch("schedule", store.as_int(s.get("burst_size"), 20))
                continue
            _set(state="idle", mode=None, run_id=None, current_geoid=None,
                 current_name=None)
            time.sleep(1.5)
        except Exception:
            _set(state="error", message=traceback.format_exc()[-500:])
            time.sleep(5)


def _schedule_due(s):
    if not store.as_bool(s.get("schedule_enabled")):
        return False
    now = datetime.datetime.utcnow()
    last = (s.get("last_scheduled_at") or "").strip()
    kind = (s.get("schedule_kind") or "daily").strip()
    last_dt = _parse_dt(last)

    if kind == "hourly":
        return last_dt is None or (now - last_dt).total_seconds() >= 3600
    if kind == "every_6h":
        return last_dt is None or (now - last_dt).total_seconds() >= 6 * 3600

    hh, mm = _parse_hhmm(s.get("schedule_time") or "02:00")
    past_clock = (now.hour, now.minute) >= (hh, mm)
    if not past_clock:
        return False
    if kind == "daily":
        if last_dt and last_dt.date() == now.date():
            return False
        return True
    if kind == "weekly":
        want = store.as_int(s.get("schedule_weekday"), 0) % 7
        if now.weekday() != want:
            return False
        if last_dt and (now.date() - last_dt.date()).days < 7:
            return False
        return True
    return False


def _mark_scheduled():
    conn = store.connect()
    try:
        store.put(conn, "last_scheduled_at", db.now())
        conn.commit()
    finally:
        conn.close()


def _parse_hhmm(value):
    try:
        parts = value.strip().split(":")
        return int(parts[0]) % 24, int(parts[1]) % 60
    except (ValueError, IndexError, AttributeError):
        return 2, 0


def _parse_dt(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def _run_batch(mode, limit):
    if _cancel.is_set() and mode != "burst":
        _cancel.clear()
        return True
    if not _job.acquire(blocking=False):
        _set(message="another job is running")
        time.sleep(2)
        return False
    conn = store.connect()
    run_id = None
    items = pages = findings = 0
    try:
        s = store.get_all(conn)
        run_id = store.start_run(conn, mode, provider=s.get("provider"),
                                 filter_states=s.get("filter_states") or None)
        _set(state="running", mode=mode, run_id=run_id, message="")
        rows = _claim(conn, s, limit)
        if not rows:
            store.finish_run(conn, run_id, "ok", "queue empty for current filters",
                             items=0, pages=0, findings=0)
            _set(state="idle", message="queue empty — plan work first")
            return True
        client = crawl.client_for(s)
        try:
            for row in rows:
                if _cancel.is_set():
                    store.finish_run(conn, run_id, "stopped", "stopped by operator",
                                     items=items, pages=pages, findings=findings)
                    _set(state="idle", message="stopped")
                    _cancel.clear()
                    return True
                n_pages, n_find = _process(conn, client, s, run_id, row)
                items += 1
                pages += n_pages
                findings += n_find
                store.bump_run(conn, run_id, items=0, pages=n_pages, findings=n_find)
                store.bump_run(conn, run_id, items=1)
        finally:
            client.close()
        store.finish_run(conn, run_id, "ok", None, items=items, pages=pages,
                         findings=findings)
        _set(state="idle", current_geoid=None, current_name=None,
             message="finished %s: %d item(s), %d page(s), %d finding(s)"
             % (mode, items, pages, findings))
        return True
    except Exception as exc:
        if run_id:
            store.finish_run(conn, run_id, "failed", str(exc)[:500],
                             items=items, pages=pages, findings=findings)
        _set(state="error", message=str(exc)[:500])
        return True
    finally:
        conn.close()
        _job.release()


def _claim(conn, s, limit):
    states = [x.strip().upper() for x in (s.get("filter_states") or "").split(",") if x.strip()]
    kinds = [x.strip() for x in (s.get("filter_kinds") or "").split(",") if x.strip()]
    cats = [x.strip() for x in (s.get("filter_categories") or "").split(",") if x.strip()]
    kinds = [k for k in kinds if k in VALID_KINDS] or None
    cats = [c for c in cats if c in VALID_CATEGORIES] or None
    return ledger.claim(
        conn, limit=limit,
        states=states or None,
        categories=cats,
        kinds=kinds,
        min_pop=store.as_int(s.get("min_pop"), 0) or None,
    )


def _process(conn, client, s, run_id, row):
    geoid, category = row["geoid"], row["category"]
    j = conn.execute("SELECT * FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
    name = j["name"] if j else geoid
    state = j["state_usps"] if j else ""
    _set(current_geoid=geoid, current_name="%s (%s) / %s" % (name, state, category))

    pages, text = crawl.crawl_item(
        conn, client, s, run_id, geoid, category, name, state)
    n_pages = len(pages)

    if (s.get("provider") or "none") == "none":
        ledger.set_status(conn, geoid, category, "needs_review")
        conn.execute(
            "UPDATE work_item SET last_error=?, updated_at=? WHERE geoid=? AND category=?",
            ("archived %d page(s); no AI provider — enter findings manually or set a provider and return to queue"
             % n_pages, db.now(), geoid, category))
        conn.commit()
        return n_pages, 0

    packet = packets.build(conn, geoid, [category])
    researcher = s.get("researcher") or "collector"
    raw, doc, err = extract.extract(s, packet, text, researcher=researcher)
    conn.execute(
        "INSERT INTO crawl_extract (run_id, geoid, category, provider, model, "
        "raw_response, parsed_ok, error, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, geoid, category, s.get("provider"), _model_name(s),
         (raw or "")[:200000], 0 if err or not doc else 1, err, db.now()))
    conn.commit()

    if err or not doc:
        ledger.set_status(conn, geoid, category, "pending")
        conn.execute(
            "UPDATE work_item SET last_error=?, updated_at=? WHERE geoid=? AND category=?",
            ((err or "extract failed")[:500], db.now(), geoid, category))
        conn.commit()
        return n_pages, 0

    for f in doc.get("findings") or []:
        f.setdefault("geoid", geoid)
        f.setdefault("extraction_method", "agent_research")
        f.setdefault("researcher", researcher)

    res = ingest.load_doc(conn, doc, allow_partial=True,
                          label="crawl:%s/%s" % (geoid, category))
    if res["written"] == 0:
        ledger.set_status(conn, geoid, category, "needs_review")
        conn.execute(
            "UPDATE work_item SET last_error=?, updated_at=? WHERE geoid=? AND category=?",
            ("extractor returned 0 valid findings (%d rejected); pages archived"
             % res["rejected"], db.now(), geoid, category))
        conn.commit()
        return n_pages, 0

    # Second checker: items that pass are marked complete with no human
    # review; anything flagged stays at needs_review with the reasons.
    check.run_and_apply(conn, s, run_id, geoid, category, text)
    return n_pages, res["written"]


def _model_name(s):
    p = s.get("provider")
    if p == "openai":
        return s.get("openai_model")
    if p == "anthropic":
        return s.get("anthropic_model")
    if p == "llama":
        return s.get("llama_model")
    return None


def run_seed(include_mcd=False, counties_only=False, force=False):
    """Blocking seed; intended to be called from a worker thread."""
    if not _job.acquire(timeout=2):
        raise RuntimeError("collector is busy")
    conn = store.connect()
    run_id = store.start_run(conn, "seed", provider=None)
    _set(state="running", mode="seed", run_id=run_id, message="seeding jurisdictions")
    try:
        kinds = ["county", "place"]
        if include_mcd:
            kinds.append("mcd")
        if counties_only:
            kinds = ["county"]
        with db.Run(conn, "seed", {"include_mcd": include_mcd}) as run:
            counts = seedmod.seed(conn, kinds=tuple(kinds), force=force)
            run.rows_out = sum(counts.values())
        sources.seed_catalog(conn)
        n = coverage.seed_empty_states(conn)
        msg = "jurisdictions %s; empty-state coverage %d" % (counts, n)
        store.finish_run(conn, run_id, "ok", msg, items=sum(counts.values()))
        _set(state="idle", message=msg)
        return counts
    except Exception as exc:
        store.finish_run(conn, run_id, "failed", str(exc)[:500])
        _set(state="error", message=str(exc)[:500])
        raise
    finally:
        conn.close()
        _job.release()


def run_plan(states, kinds=None, categories=None, min_pop=0, limit=None):
    conn = store.connect()
    run_id = store.start_run(conn, "plan", filter_states=",".join(states or []))
    try:
        kinds = tuple(kinds or ("county", "place", "state"))
        cats = list(categories or CATEGORIES.keys())
        n = ledger.plan(conn, states=states, kinds=kinds, categories=cats,
                        min_pop=min_pop, limit=limit)
        store.finish_run(conn, run_id, "ok", "created %d work items" % n, items=n)
        return n
    except Exception as exc:
        store.finish_run(conn, run_id, "failed", str(exc)[:500])
        raise
    finally:
        conn.close()


def run_adapter(key, states=None, archive_only=False):
    from taxdb import adapters
    if not _job.acquire(timeout=2):
        raise RuntimeError("collector is busy")
    conn = store.connect()
    run_id = store.start_run(conn, "fetch", filter_states=",".join(states or []))
    _set(state="running", mode="fetch", run_id=run_id, message="adapter %s" % key)
    try:
        a = adapters.get(key)
        with db.Run(conn, "fetch", {"adapter": key, "states": states}) as run:
            res = a.run(conn, archive_only=archive_only, states=states)
            run.rows_out = res["written"]
        msg = "%s: %d findings, %d unmapped" % (
            key, res["written"], len(res.get("unmapped") or []))
        store.finish_run(conn, run_id, "ok", msg, findings=res["written"])
        _set(state="idle", message=msg)
        return res
    except Exception as exc:
        store.finish_run(conn, run_id, "failed", str(exc)[:500])
        _set(state="error", message=str(exc)[:500])
        raise
    finally:
        conn.close()
        _job.release()


def run_cog(force=False):
    from taxdb import cog
    if not _job.acquire(timeout=2):
        raise RuntimeError("collector is busy")
    conn = store.connect()
    run_id = store.start_run(conn, "cog")
    _set(state="running", mode="cog", run_id=run_id, message="Census of Governments 2022")
    try:
        with db.Run(conn, "cog", {"force": force}) as run:
            res = cog.load(conn, force=force)
            run.rows_out = res["written"]
        msg = "revenue_base %d rows (%d unmapped)" % (res["written"], res["unmapped"])
        store.finish_run(conn, run_id, "ok", msg, items=res["written"])
        _set(state="idle", message=msg)
        return res
    except Exception as exc:
        store.finish_run(conn, run_id, "failed", str(exc)[:500])
        _set(state="error", message=str(exc)[:500])
        raise
    finally:
        conn.close()
        _job.release()


def run_statutes(usps, force=False):
    from taxdb import statutes
    if not _job.acquire(timeout=2):
        raise RuntimeError("collector is busy")
    conn = store.connect()
    run_id = store.start_run(conn, "statutes", filter_states=usps)
    _set(state="running", mode="statutes", run_id=run_id,
         message="Open US Law %s" % usps)
    try:
        with db.Run(conn, "statutes", {"state": usps}) as run:
            res = statutes.fetch_state(conn, usps, force=force)
            run.rows_out = res["written"]
        msg = "%s: %d statute sections" % (usps.upper(), res["written"])
        store.finish_run(conn, run_id, "ok", msg, items=res["written"])
        _set(state="idle", message=msg)
        return res
    except Exception as exc:
        store.finish_run(conn, run_id, "failed", str(exc)[:500])
        _set(state="error", message=str(exc)[:500])
        raise
    finally:
        conn.close()
        _job.release()
