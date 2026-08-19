"""Background loop: toggle, schedule, and burst crawls.

One coordinator thread decides what happens next — drain the intake queue,
run an autopilot step, honour the schedule — and a pool of worker threads
researches jurisdictions. Nearly all of a work item is spent waiting on a
government web server and then on a model, so the pool is what turns a
four-month national run into a three-week one. It does not increase the load
on any single host: the fetch ceiling is global and divided among workers,
and search has its own process-wide throttle.

Every worker owns its own database connection, its own Crawlee storage
directory, and its own slot in the status snapshot. The queue is safe to
share because `ledger.claim` is atomic.
"""

import datetime
import queue
import threading
import time
import traceback

from taxdb import db, ingest, ledger, packets, seed as seedmod, sources, coverage
from taxdb.vocab import ELECTIONS, FRAMEWORK, WORK_CATEGORIES

from . import autopilot, batch, check, crawl, extract, intake, store
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
    "step": "",
    "warning": "",
}
# slot -> what that worker is on right now. The aggregate fields above stay
# populated from whichever worker moved last, so the one-line home page and
# the status API keep working unchanged.
_slots = {}


def snapshot():
    with _lock:
        snap = dict(_status)
        snap["workers"] = [dict(v, slot=k) for k, v in sorted(_slots.items())]
        snap["active"] = len(_slots)
        return snap


def _set(**kwargs):
    with _lock:
        _status.update(kwargs)


def _slot_set(slot, **kwargs):
    """Record what one worker is doing, and mirror it into the summary."""
    with _lock:
        _slots.setdefault(slot, {}).update(kwargs)
        for key in ("current_geoid", "current_name", "step"):
            if key in kwargs:
                _status[key] = kwargs[key]


def _slot_clear(slot):
    with _lock:
        _slots.pop(slot, None)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _cancel.clear()
    _thread = threading.Thread(target=_loop, name="collector-worker", daemon=True)
    _thread.start()


def request_stop():
    _cancel.set()
    _set(step="")
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
    last_batch_tick = 0.0
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
            if now - last_batch_tick > 60:
                last_batch_tick = now
                _batch_tick()
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
                    # An empty queue is not the end of the work. Ask the
                    # autopilot for the next thing and keep going.
                    if not _autopilot_step(s):
                        _set(step="", message="everything planned is done and "
                                              "current — nothing left to research")
                        time.sleep(20)
                continue
            if _schedule_due(s):
                _mark_scheduled()
                _run_batch("schedule", store.as_int(s.get("burst_size"), 20))
                continue
            _set(state="idle", mode=None, run_id=None, current_geoid=None,
                 current_name=None, step="")
            time.sleep(1.5)
        except Exception:
            _set(state="error", message=traceback.format_exc()[-500:])
            time.sleep(5)


def _batch_tick():
    """Collect finished batch extractions and submit what has accumulated.

    Runs on the coordinator, not a worker: it is one HTTP poll plus ingest, and
    keeping it off the pool means a long collect cannot starve crawling.
    """
    conn = store.connect()
    try:
        s = store.get_all(conn)
        if not batch.enabled(s):
            return
        out = batch.tick(conn, s)
        if out["collected"] or out["failed"] or out["submitted"]:
            bits = []
            if out["submitted"]:
                bits.append("sent %d for batch reading" % out["submitted"])
            if out["collected"]:
                bits.append("read back %d" % out["collected"])
            if out["failed"]:
                bits.append("%d came back unusable" % out["failed"])
            if out["ready"]:
                bits.append("%d still to ingest" % out["ready"])
            _set(message="; ".join(bits))
    except Exception as exc:
        _set(message="batch extraction: %s" % str(exc)[:300])
    finally:
        conn.close()


def _autopilot_step(settings):
    """Run one autopilot action. True if something was done."""
    if not autopilot.enabled(settings):
        return False
    conn = store.connect()
    try:
        plan = autopilot.next_action(conn, settings)
    finally:
        conn.close()
    if not plan:
        return False
    action, kwargs, label = plan
    _set(state="running", mode="autopilot", step=label, message=label)
    try:
        run_action(action, kwargs, settings)
        return True
    except Exception as exc:
        # A failed bulk download must not spin the loop. The cooldown marker
        # was written before the attempt, so the next tick moves on.
        _set(state="idle", step="", message="%s failed: %s" % (label, str(exc)[:300]))
        time.sleep(5)
        return True


def run_action(action, kwargs, settings=None):
    """Execute one autopilot action. Raises on failure."""
    conn = store.connect()
    try:
        settings = settings or store.get_all(conn)
        if action in autopilot.COOLDOWN_HOURS:
            autopilot.mark_tried(conn, action)
        if action == autopilot.STATUTES:
            autopilot.mark_tried(conn, "%s:%s" % (action, kwargs["usps"]))
    finally:
        conn.close()

    if action == autopilot.SEED:
        return run_seed(include_mcd=False)
    if action == autopilot.SOURCES:
        conn = store.connect()
        try:
            n = sources.seed_catalog(conn)
            coverage.seed_empty_states(conn)
            _set(message="recorded %d state source(s)" % n)
            return n
        finally:
            conn.close()
    if action == autopilot.SST:
        return run_adapter("sst", states=kwargs.get("states"))
    if action == autopilot.COG:
        return run_cog()
    if action == autopilot.STATUTES:
        return run_statutes(kwargs["usps"])
    if action == autopilot.PLAN_FRAMEWORK:
        n = run_plan(kwargs["states"], kinds=("state",), categories=[FRAMEWORK])
        _set(message="queued the statutory framework for %d state(s)"
                     % len(kwargs["states"]))
        return n
    if action == autopilot.EXPAND:
        conn = store.connect()
        try:
            n = autopilot.expand(conn, kwargs["geoids"], settings)
            _set(message="added %d work item(s) for %d place(s)"
                         % (n, len(kwargs["geoids"])))
            return n
        finally:
            conn.close()
    if action == autopilot.REFRESH:
        conn = store.connect()
        try:
            n = ledger.requeue_stale(conn, days=kwargs.get("days", 365))
            _set(message="sent %d stale item(s) back for a fresh look" % n)
            return n
        finally:
            conn.close()
    raise RuntimeError("unknown autopilot action %r" % action)


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


def worker_count(settings):
    """How many jurisdictions to research at once. See store.worker_count."""
    return store.worker_count(settings)


def _run_batch(mode, limit):
    """Claim a batch and research it across the worker pool.

    The pool shares one claimed batch through a queue rather than each worker
    calling claim() itself: one claim per batch keeps the run table readable
    and the ledger writes down to one burst instead of N.
    """
    if _cancel.is_set() and mode != "burst":
        _cancel.clear()
        return True
    if not _job.acquire(blocking=False):
        _set(message="another job is running")
        time.sleep(2)
        return False
    conn = store.connect()
    run_id = None
    tally = {"items": 0, "pages": 0, "findings": 0, "errors": 0}
    tally_lock = threading.Lock()
    stopped = False
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

        pending = queue.Queue()
        for row in rows:
            pending.put(row)
        n_workers = min(worker_count(s), len(rows))

        # One httpx client for the pool: it is thread-safe and pooling
        # connections across workers is the point.
        client = crawl.client_for(s)

        def run_slot(slot):
            # Crawlee purges its storage on start, so each worker needs its own.
            fetcher_mod = None
            try:
                from . import fetcher as fetcher_mod
                fetcher_mod.set_slot(slot)
            except Exception:
                pass                      # legacy loop; no storage to isolate
            wconn = store.connect()
            try:
                while not _cancel.is_set():
                    try:
                        row = pending.get_nowait()
                    except queue.Empty:
                        return
                    try:
                        n_pages, n_find = _process(
                            wconn, client, s, run_id, row, slot=slot)
                        with tally_lock:
                            tally["items"] += 1
                            tally["pages"] += n_pages
                            tally["findings"] += n_find
                        store.bump_run(wconn, run_id, items=1, pages=n_pages,
                                       findings=n_find)
                    except Exception as exc:
                        # One unresearchable jurisdiction must not take the
                        # other nineteen down with it. Return it to the queue
                        # with the reason; max_attempts stops a repeat offender.
                        with tally_lock:
                            tally["errors"] += 1
                        _return_to_queue(wconn, row, exc)
                    finally:
                        _slot_clear(slot)
            finally:
                wconn.close()

        threads = [threading.Thread(target=run_slot, args=(i,), daemon=True,
                                    name="collector-w%d" % i)
                   for i in range(n_workers)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            client.close()

        stopped = _cancel.is_set()
        if stopped:
            store.finish_run(conn, run_id, "stopped", "stopped by operator",
                             items=tally["items"], pages=tally["pages"],
                             findings=tally["findings"])
            _set(state="idle", message="stopped", step="",
                 current_geoid=None, current_name=None)
            _cancel.clear()
            return True

        note = None
        if tally["errors"]:
            note = "%d item(s) errored and went back in the queue" % tally["errors"]
        store.finish_run(conn, run_id, "ok", note, items=tally["items"],
                         pages=tally["pages"], findings=tally["findings"])
        _set(state="idle", current_geoid=None, current_name=None, step="",
             message="finished %s: %d item(s), %d page(s), %d finding(s)%s"
             % (mode, tally["items"], tally["pages"], tally["findings"],
                "" if not note else " (%s)" % note))
        return True
    except Exception as exc:
        if run_id:
            store.finish_run(conn, run_id, "failed", str(exc)[:500],
                             items=tally["items"], pages=tally["pages"],
                             findings=tally["findings"])
        _set(state="error", message=str(exc)[:500])
        return True
    finally:
        conn.close()
        _job.release()


def _return_to_queue(conn, row, exc):
    """Hand a failed item back, with the reason recorded where it is visible."""
    try:
        ledger.set_status(conn, row["geoid"], row["category"], "pending",
                          error=("worker error: %s" % exc)[:500])
        conn.commit()
    except Exception:
        pass


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
        max_attempts=store.as_int(s.get("max_attempts"), 4) or None,
    )


def _process(conn, client, s, run_id, row, slot=0):
    geoid, category = row["geoid"], row["category"]
    j = conn.execute("SELECT * FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
    name = j["name"] if j else geoid
    state = j["state_usps"] if j else ""
    _slot_set(slot, current_geoid=geoid,
              current_name="%s (%s) / %s" % (name, state, category),
              step=_step_label(name, state, category))

    diag = crawl.new_diag()
    pages, text = crawl.crawl_item(
        conn, client, s, run_id, geoid, category, name, state, diag=diag)
    n_pages = len(pages)
    search_note = crawl.search_note(diag)
    if search_note:
        _set(warning=search_note)

    def park(status, message):
        """Leave the item somewhere a human can act on, with the reason."""
        note = message if not search_note else "%s (%s)" % (message, search_note)
        ledger.set_status(conn, geoid, category, status)
        conn.execute(
            "UPDATE work_item SET last_error=?, updated_at=? WHERE geoid=? AND category=?",
            (note[:500], db.now(), geoid, category))
        conn.commit()

    if (s.get("provider") or "none") == "none":
        park("needs_review",
             "archived %d page(s); no AI provider — enter findings manually or "
             "set a provider and return to queue" % n_pages)
        return n_pages, 0

    # lean: the packet rides in the uncached user message; the rules it
    # would repeat are already in the extractor's cached system prompt.
    packet = packets.build(conn, geoid, [category], lean=True)
    researcher = s.get("researcher") or "collector"

    if batch.enabled(s):
        # Pages are archived; the reading happens in a batch at half price.
        # The item parks at awaiting_ai, which claim() will not take and the
        # stale sweep will not touch, so it sits safely for hours.
        batch.park(conn, run_id, geoid, category, packet, text, n_pages,
                   search_note=search_note)
        ledger.set_status(conn, geoid, category, "awaiting_ai",
                          error="crawled; queued for batch extraction")
        conn.commit()
        _slot_set(slot, step="Queued %s for batch reading" % name)
        return n_pages, 0

    raw, doc, err = extract.extract(s, packet, text, researcher=researcher)
    conn.execute(
        "INSERT INTO crawl_extract (run_id, geoid, category, provider, model, "
        "raw_response, parsed_ok, error, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, geoid, category, s.get("provider"), _model_name(s),
         (raw or "")[:200000], 0 if err or not doc else 1, err, db.now()))
    conn.commit()

    if err or not doc:
        park("pending", (err or "extract failed"))
        return n_pages, 0

    ingest.stamp_doc(doc, geoid=geoid, category=category, state_usps=state,
                     researcher=researcher)

    res = ingest.load_doc(conn, doc, allow_partial=True,
                          label="crawl:%s/%s" % (geoid, category),
                          work_item=(geoid, category))

    if res["written"] == 0:
        # An empty answer is a real answer for the elections pass: this county
        # published nothing we could find. Record it as a coverage gap rather
        # than leaving a blank that reads like "never tried".
        if category == ELECTIONS and not res["rejected"]:
            coverage.assert_scope(
                conn, "ballot_measure", geoid, scope_type="county",
                completeness="spot_checked", measures_found=0,
                basis="Collector searched the county elections office and found "
                      "no revenue measures in the documents it could reach.",
                asserted_by="collector")
            ledger.set_status(conn, geoid, category, "no_data")
            conn.execute(
                "UPDATE work_item SET last_error=?, updated_at=? "
                "WHERE geoid=? AND category=?",
                ("no revenue measures found; recorded as a coverage gap",
                 db.now(), geoid, category))
            conn.commit()
            return n_pages, 0
        park("needs_review",
             "extractor returned 0 valid rows (%d rejected); pages archived"
             % res["rejected"])
        return n_pages, 0

    if category == ELECTIONS and res["by_type"].get("measures"):
        coverage.assert_scope(
            conn, "ballot_measure", geoid, scope_type="county",
            completeness="partial", measures_found=res["by_type"]["measures"],
            basis="Collector read county elections documents. Coverage is "
                  "partial until a full canvass series is loaded.",
            asserted_by="collector")

    # Second checker: items that pass are marked complete with no human
    # review; anything flagged stays at needs_review with the reasons.
    check.run_and_apply(conn, s, run_id, geoid, category, text)
    if search_note:
        conn.execute(
            "UPDATE work_item SET last_error=COALESCE(last_error || ' | ', '') || ? "
            "WHERE geoid=? AND category=?", (search_note[:300], geoid, category))
        conn.commit()
    return n_pages, res["written"]


def _step_label(name, state, category):
    """What the home page says we are doing, in plain language."""
    if category == FRAMEWORK:
        return "Reading %s state law: what it takes to pass a measure" % state
    if category == ELECTIONS:
        return "Reading %s election results for past revenue measures" % name
    return "Researching %s taxes in %s, %s" % (
        category.replace("_", " "), name, state)


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
        cats = list(categories or WORK_CATEGORIES.keys())
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
