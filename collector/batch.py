"""Batch extraction: the same requests at half the price.

Extraction is the dominant cost of a national run, and not one item of it is
latency-sensitive. Nobody is waiting on Adams County's lodging tax to come back
in two seconds; the database is being filled over weeks. That is precisely the
trade the Message Batches API offers: identical requests, 50% off, returned
within the hour instead of the second.

Turning it on splits the loop that used to be one step:

    crawl -> extract -> ingest -> check          synchronous
    crawl -> park ... submit ... collect -> ingest -> check   batched

The crawler still archives every byte and builds the packet, then parks both in
`extract_batch_item` and leaves the work item at `awaiting_ai`. A submitter
posts whatever has accumulated. A collector polls, ingests each result, and
hands it to the second checker exactly as the synchronous path does, so the
review semantics are unchanged.

The checker stays synchronous on purpose. It is the cheap model and the small
share of the bill, it needs the ingest to have happened before it can read the
rows back, and keeping it inline preserves fail-toward-review: a checker that
could not run leaves the item for a human rather than trusting it.

Only Anthropic is wired up. The OpenAI-compatible batch surface is a different
shape (file upload, then a job), and Ollama has none.
"""

import json
import uuid

import httpx

from taxdb import db, ingest, ledger

from . import check, extract, store

API = "https://api.anthropic.com/v1/messages/batches"

# Providers with a batch surface we speak.
SUPPORTED = ("anthropic",)


def enabled(settings):
    """Batch mode is opt-in: it trades minutes of latency for half the bill."""
    return (store.as_bool(settings.get("batch_extract"))
            and (settings.get("provider") or "none").strip().lower() in SUPPORTED)


def pending_work(conn):
    """True while anything is parked anywhere in the batch pipeline.

    The tick must keep running on this even after batch mode is switched off:
    in-flight batches still need polling and collecting, ready results still
    need applying, and queued items need handing back — or every one of them
    sits at awaiting_ai forever, invisible to claim() and the stale sweep."""
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM extract_batch "
        "        WHERE status IN ('unconfirmed','submitted','ended')) + "
        "       (SELECT COUNT(*) FROM extract_batch_item "
        "        WHERE status IN ('queued','ready'))").fetchone()
    return bool(row[0])


def requeue_queued(conn):
    """Hand parked-but-unsubmitted items back to the synchronous path.

    Batch mode was turned off (or the provider switched away) with items
    already crawled and queued. They re-crawl, which costs pages, but the
    alternative was costing them entirely."""
    rows = conn.execute("SELECT id, geoid, category FROM extract_batch_item "
                        "WHERE status='queued'").fetchall()
    for r in rows:
        conn.execute(
            "UPDATE extract_batch_item SET status='failed', "
            "error='batch mode turned off before submission' WHERE id=?",
            (r["id"],))
        ledger.set_status(conn, r["geoid"], r["category"], "pending",
                          error="batch mode turned off; queued for live reading")
    if rows:
        conn.commit()
    return len(rows)


def _headers(settings):
    key = (settings.get("anthropic_api_key") or "").strip()
    if not key:
        raise extract.ExtractError("Anthropic API key is empty")
    return {"Content-Type": "application/json", "x-api-key": key,
            "anthropic-version": "2023-06-01"}


def custom_id(geoid, category, seq):
    """Provider-safe and unique.

    The sequence is the queue row's own id, not a timestamp: four workers park
    items in the same millisecond routinely, and a clock-derived id collided.
    A row id is also stable and greppable, which a uuid would not be.
    """
    return "x-%s-%s-%d" % (geoid, category.replace("_", "-"), seq)


# --------------------------------------------------------------------- parking

def park(conn, run_id, geoid, category, packet, doc_text, n_pages,
         search_note=None):
    """Hold one crawled item for the next batch. Returns the queue row id.

    The custom_id is filled from the row id after insert, so it is unique by
    construction however many workers park at once.
    """
    cur = conn.execute(
        "INSERT INTO extract_batch_item (custom_id, run_id, geoid, category, "
        "packet, doc_text, n_pages, search_note, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,'queued',?)",
        ("pending-%s" % _nonce(), run_id, geoid, category, packet,
         doc_text, n_pages, search_note, db.now()))
    row_id = cur.lastrowid
    conn.execute("UPDATE extract_batch_item SET custom_id=? WHERE id=?",
                 (custom_id(geoid, category, row_id), row_id))
    conn.commit()
    return row_id


def _nonce():
    """Placeholder that cannot collide before the row id is known."""
    return uuid.uuid4().hex


def queued(conn, limit=None):
    sql = ("SELECT * FROM extract_batch_item WHERE status='queued' "
           "ORDER BY id")
    if limit:
        sql += " LIMIT %d" % int(limit)
    return conn.execute(sql).fetchall()


def queue_depth(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM extract_batch_item WHERE status='queued'"
    ).fetchone()[0]


# ------------------------------------------------------------------ submitting

def _request_for(settings, model, row):
    prompt = extract._user_prompt(row["packet"], row["doc_text"])
    params = {
        "model": model,
        "max_tokens": extract.ANTHROPIC_MAX_TOKENS,
        "system": [{"type": "text", "text": extract.SYSTEM,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": prompt}],
    }
    params.update(extract.anthropic_tuning(model))
    return {"custom_id": row["custom_id"], "params": params}


def submit(conn, settings, limit=None):
    """Post one batch of everything queued. Returns (batch_id, n) or (None, 0)."""
    rows = queued(conn, limit or store.as_int(settings.get("batch_max_items"), 200))
    if not rows:
        return None, 0
    model = extract.default_model(settings, "anthropic")
    payload = {"requests": [_request_for(settings, model, r) for r in rows]}

    cur = conn.execute(
        "INSERT INTO extract_batch (provider, model, status, n_items, created_at) "
        "VALUES (?,?,'building',?,?)",
        (settings.get("provider"), model, len(rows), db.now()))
    batch_id = cur.lastrowid
    conn.commit()

    try:
        with httpx.Client(timeout=180.0) as client:
            r = client.post(API, headers=_headers(settings), json=payload)
        if r.status_code >= 400:
            raise extract.ExtractError(
                "batch submit %s: %s" % (r.status_code, r.text[:400]))
        remote = (r.json() or {}).get("id")
        if not remote:
            raise extract.ExtractError("batch submit returned no id")
    except httpx.HTTPError as exc:
        if not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            # The request may have reached the provider before the connection
            # died. Requeueing here would post the same batch twice and pay
            # for both, so the items park as submitted under an 'unconfirmed'
            # batch and reconcile_unconfirmed() adopts or requeues them.
            conn.execute(
                "UPDATE extract_batch SET status='unconfirmed', message=? "
                "WHERE id=?",
                ("submit outcome unknown: %s" % str(exc)[:400], batch_id))
            conn.executemany(
                "UPDATE extract_batch_item SET batch_id=?, status='submitted' "
                "WHERE id=?", [(batch_id, r["id"]) for r in rows])
            conn.commit()
            raise
        # Never left this machine: the items stay 'queued' and the next tick
        # tries again. Only the batch row is marked failed, for the log.
        conn.execute(
            "UPDATE extract_batch SET status='failed', message=? WHERE id=?",
            (str(exc)[:500], batch_id))
        conn.commit()
        raise
    except Exception as exc:
        # The provider answered (an HTTP status or a bad body), so nothing
        # was accepted. Items stay 'queued' for the next tick.
        conn.execute(
            "UPDATE extract_batch SET status='failed', message=? WHERE id=?",
            (str(exc)[:500], batch_id))
        conn.commit()
        raise

    conn.execute(
        "UPDATE extract_batch SET remote_id=?, status='submitted', submitted_at=? "
        "WHERE id=?", (remote, db.now(), batch_id))
    conn.executemany(
        "UPDATE extract_batch_item SET batch_id=?, status='submitted' WHERE id=?",
        [(batch_id, r["id"]) for r in rows])
    conn.commit()
    return batch_id, len(rows)


def in_flight(conn):
    return conn.execute(
        "SELECT * FROM extract_batch WHERE status IN ('submitted','ended') "
        "ORDER BY id").fetchall()


def reconcile_unconfirmed(conn, settings):
    """Resolve batches whose submit outcome was never learned.

    Lists the provider's recent batches. One that no local row knows about
    and whose request count matches is our lost submit: adopt its id and
    poll it like any other. If nothing at the provider can be it and the
    batch is old enough that a slow accept is off the table, the items go
    back to 'queued' — one submission is paid either way, never two.
    """
    rows = conn.execute(
        "SELECT * FROM extract_batch WHERE status='unconfirmed' ORDER BY id"
    ).fetchall()
    if not rows:
        return 0
    # Paginate: with only the first page, a busy account could hide the lost
    # batch behind newer ones, and the age-out below would requeue items the
    # provider is actually processing — the double submission this function
    # exists to prevent. listed_all records whether the walk reached the end.
    remote = []
    listed_all = False
    try:
        params = {"limit": 100}
        with httpx.Client(timeout=60.0) as client:
            for _ in range(5):
                r = client.get(API, params=params, headers=_headers(settings))
                if r.status_code >= 400:
                    return 0
                page = r.json() or {}
                remote.extend(page.get("data") or [])
                if not page.get("has_more"):
                    listed_all = True
                    break
                params["after_id"] = page.get("last_id")
    except Exception:
        return 0

    known = {x["remote_id"] for x in conn.execute(
        "SELECT remote_id FROM extract_batch WHERE remote_id IS NOT NULL")}

    def n_requests(b):
        rc = b.get("request_counts") or {}
        return sum(v for v in rc.values() if isinstance(v, int))

    resolved = 0
    for row in rows:
        matches = [b for b in remote
                   if b.get("id") and b["id"] not in known
                   and n_requests(b) == row["n_items"]]
        if len(matches) == 1:
            conn.execute(
                "UPDATE extract_batch SET remote_id=?, status='submitted', "
                "submitted_at=?, message='adopted after an ambiguous submit' "
                "WHERE id=?", (matches[0]["id"], db.now(), row["id"]))
            known.add(matches[0]["id"])
            resolved += 1
            continue
        aged = conn.execute(
            "SELECT 1 FROM extract_batch WHERE id=? AND "
            "created_at < datetime('now', '-2 hours')", (row["id"],)).fetchone()
        if not matches and aged and listed_all:
            conn.execute(
                "UPDATE extract_batch SET status='failed', "
                "message='submit never reached the provider; items requeued' "
                "WHERE id=?", (row["id"],))
            conn.execute(
                "UPDATE extract_batch_item SET status='queued', batch_id=NULL "
                "WHERE batch_id=? AND status='submitted'", (row["id"],))
            resolved += 1
    if resolved:
        conn.commit()
    return resolved


def poll(conn, settings, batch_row):
    """Ask the provider whether a batch has finished. Returns its status."""
    with httpx.Client(timeout=60.0) as client:
        r = client.get("%s/%s" % (API, batch_row["remote_id"]),
                       headers=_headers(settings))
    if r.status_code >= 400:
        raise extract.ExtractError(
            "batch poll %s: %s" % (r.status_code, r.text[:400]))
    status = (r.json() or {}).get("processing_status")
    if status == "ended" and batch_row["status"] != "ended":
        conn.execute("UPDATE extract_batch SET status='ended' WHERE id=?",
                     (batch_row["id"],))
        conn.commit()
    return status


# ------------------------------------------------------------------ collecting

def _results(settings, remote_id):
    """Stream the JSONL results for a finished batch."""
    with httpx.Client(timeout=300.0) as client:
        with client.stream("GET", "%s/%s/results" % (API, remote_id),
                           headers=_headers(settings)) as r:
            if r.status_code >= 400:
                raise extract.ExtractError(
                    "batch results %s: %s" % (r.status_code, r.read()[:400]))
            for line in r.iter_lines():
                line = (line or "").strip()
                if line:
                    yield json.loads(line)


def _text_of(message):
    parts = []
    for block in (message or {}).get("content") or []:
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts)


def collect(conn, settings, batch_row):
    """Download a finished batch's results. Returns how many landed.

    Downloading and applying are deliberately separate. The download is one
    HTTP stream; applying is an ingest plus a second-checker call per item, so
    a 200-item batch would hold the coordinator thread for the better part of
    an hour and stop the crawl dead. Results are banked as 'ready' and applied
    a few per tick instead.
    """
    by_custom = {
        r["custom_id"]: r for r in conn.execute(
            "SELECT id, custom_id FROM extract_batch_item WHERE batch_id=?",
            (batch_row["id"],)).fetchall()}
    landed = unknown = 0
    for res in _results(settings, batch_row["remote_id"]):
        item = by_custom.get(res.get("custom_id"))
        if item is None:
            unknown += 1
            continue
        outcome = (res.get("result") or {})
        kind = outcome.get("type")
        if kind == "succeeded":
            msg = outcome.get("message") or {}
            raw = _text_of(msg)
            err = None
            if msg.get("stop_reason") == "max_tokens":
                # Truncated JSON fails the parse downstream; name the real
                # cause here so the item's error says what happened.
                raw, err = None, "batch result truncated at the output cap"
        else:
            raw, err = None, "batch result %s: %s" % (
                kind, json.dumps(outcome.get("error") or {})[:200])
        # status guard: a partially collected batch stays 'ended' and is
        # collected again next tick. Without the guard, items already applied
        # ('done') were flipped back to 'ready' and re-ingested — with a
        # second checker call each, the exact spend batching exists to avoid.
        conn.execute(
            "UPDATE extract_batch_item SET status='ready', raw_response=?, "
            "error=? WHERE id=? AND status='submitted'", (raw, err, item["id"]))
        landed += 1
    # Anything the provider never answered for would otherwise sit at
    # 'submitted' forever, with its work item parked at awaiting_ai where
    # neither claim() nor the stale sweep will ever pick it up again. Fail it
    # toward the queue instead.
    orphans = conn.execute(
        "SELECT id, geoid, category FROM extract_batch_item "
        "WHERE batch_id=? AND status='submitted'", (batch_row["id"],)).fetchall()
    for o in orphans:
        conn.execute(
            "UPDATE extract_batch_item SET status='failed', "
            "error='no result returned for this item' WHERE id=?", (o["id"],))
        ledger.set_status(conn, o["geoid"], o["category"], "pending",
                          error="batch returned no result — requeued")
    conn.execute(
        "UPDATE extract_batch SET status='collected', collected_at=? WHERE id=?",
        (db.now(), batch_row["id"]))
    conn.commit()
    return {"landed": landed, "unknown": unknown, "orphaned": len(orphans)}


def ready(conn, limit=25):
    return conn.execute(
        "SELECT * FROM extract_batch_item WHERE status='ready' ORDER BY id "
        "LIMIT ?", (int(limit),)).fetchall()


def ready_depth(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM extract_batch_item WHERE status='ready'"
    ).fetchone()[0]


def apply_ready(conn, settings, limit=25, apply_result=None):
    """Ingest and check a metered number of downloaded results."""
    apply_result = apply_result or apply_one
    done = failed = 0
    # Tallied per batch: one metered pass can apply items from several
    # batches, and crediting them all to the last item's batch made the
    # per-batch counts fiction.
    per_batch = {}
    for item in ready(conn, limit):
        try:
            ok = apply_result(conn, settings, item, item["raw_response"],
                              item["error"])
        except (Exception, SystemExit) as exc:
            # Do not let one poisonous result loop forever on every tick —
            # and hand the work item to a human, or it sits at awaiting_ai
            # forever with its batch item marked failed.
            conn.rollback()
            conn.execute(
                "UPDATE extract_batch_item SET status='failed', error=? WHERE id=?",
                (("apply failed: %s" % exc)[:500], item["id"]))
            ledger.set_status(conn, item["geoid"], item["category"],
                              "needs_review",
                              error=("batch result could not be applied: %s"
                                     % exc)[:500])
            conn.commit()
            ok = False
        done += 1 if ok else 0
        failed += 0 if ok else 1
        d, f = per_batch.get(item["batch_id"], (0, 0))
        per_batch[item["batch_id"]] = (d + (1 if ok else 0), f + (0 if ok else 1))
    for batch_id, (d, f) in per_batch.items():
        conn.execute(
            "UPDATE extract_batch SET n_succeeded=n_succeeded+?, "
            "n_failed=n_failed+? WHERE id=?", (d, f, batch_id))
    if per_batch:
        conn.commit()
    return {"done": done, "failed": failed}


def apply_one(conn, settings, item, raw, err):
    """Ingest one batch result and run the second checker on it.

    Mirrors the synchronous path in worker._process from the extract call
    onward, so a batched item and a live one end up in the same state.
    Returns True when the item produced rows.
    """
    from taxdb import coverage, ledger
    from taxdb.vocab import ELECTIONS

    geoid, category = item["geoid"], item["category"]
    j = conn.execute(
        "SELECT state_usps FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
    state = j["state_usps"] if j else ""
    researcher = settings.get("researcher") or "collector"

    doc = None
    if raw is not None:
        try:
            doc = extract.parse_json_payload(raw)
            doc.setdefault("schema_version", "1.1")
            doc.setdefault("researcher", researcher)
            doc.setdefault("extraction_method", "agent_research")
            if isinstance(doc.get("profile"), list) and len(doc["profile"]) == 1:
                doc["profile"] = doc["profile"][0]
            if not [k for k in extract.SECTION_KEYS if doc.get(k) is not None]:
                doc, err = None, "JSON has none of the expected sections"
        except extract.ExtractError as exc:
            doc, err = None, str(exc)

    conn.execute(
        "INSERT INTO crawl_extract (run_id, geoid, category, provider, model, "
        "raw_response, parsed_ok, error, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (item["run_id"], geoid, category, settings.get("provider"),
         extract.default_model(settings, "anthropic"), (raw or "")[:200000],
         0 if (err or not doc) else 1, err, db.now()))

    def close_item(status, note):
        conn.execute(
            "UPDATE extract_batch_item SET status=?, error=? WHERE id=?",
            ("done" if status != "failed" else "failed", note, item["id"]))
        conn.commit()

    if err or not doc:
        ledger.set_status(conn, geoid, category, "pending",
                          error=(err or "batch extract failed")[:500])
        close_item("failed", (err or "")[:500])
        return False

    ingest.stamp_doc(doc, geoid=geoid, category=category, state_usps=state,
                     researcher=researcher)
    res = ingest.load_doc(conn, doc, allow_partial=True,
                          label="batch:%s/%s" % (geoid, category),
                          work_item=(geoid, category))

    if res["written"] == 0:
        if category == ELECTIONS and not res["rejected"]:
            coverage.assert_scope(
                conn, "ballot_measure", geoid, scope_type="county",
                completeness="spot_checked", measures_found=0,
                basis="Collector searched the county elections office and found "
                      "no revenue measures in the documents it could reach.",
                asserted_by="collector")
            ledger.set_status(conn, geoid, category, "no_data",
                              error="no revenue measures found; recorded as a "
                                    "coverage gap")
        else:
            ledger.set_status(
                conn, geoid, category, "needs_review",
                error="extractor returned 0 valid rows (%d rejected); pages "
                      "archived" % res["rejected"])
        close_item("done", None)
        return False

    if category == ELECTIONS and res["by_type"].get("measures"):
        coverage.assert_scope(
            conn, "ballot_measure", geoid, scope_type="county",
            completeness="partial", measures_found=res["by_type"]["measures"],
            basis="Collector read county elections documents. Coverage is "
                  "partial until a full canvass series is loaded.",
            asserted_by="collector")

    check.run_and_apply(conn, settings, item["run_id"], geoid, category,
                        item["doc_text"], claim_ids=res.get("claim_ids"))
    if item["search_note"]:
        conn.execute(
            "UPDATE work_item SET last_error=COALESCE(last_error || ' | ', '') || ? "
            "WHERE geoid=? AND category=?",
            (item["search_note"][:300], geoid, category))
    close_item("done", None)
    return True


# ------------------------------------------------------------------------ tick

def tick(conn, settings):
    """One pass: download what finished, apply a few, submit what accumulated.

    Downloading first means a restart picks up in-flight work rather than
    piling more on top of it. Applying is metered so this never holds the
    coordinator long enough to stall crawling.
    """
    out = {"collected": 0, "failed": 0, "submitted": 0, "batches": 0,
           "waiting": 0, "ready": 0}
    reconcile_unconfirmed(conn, settings)
    for row in in_flight(conn):
        try:
            status = row["status"] if row["status"] == "ended" else poll(
                conn, settings, row)
            if status != "ended":
                out["waiting"] += 1
                continue
            collect(conn, settings, row)
            out["batches"] += 1
        except Exception as exc:
            # Discard any half-applied collect before recording the failure,
            # so the batch retries from a clean slate next tick.
            conn.rollback()
            conn.execute(
                "UPDATE extract_batch SET message=? WHERE id=?",
                (str(exc)[:500], row["id"]))
            conn.commit()
            out["waiting"] += 1

    applied = apply_ready(
        conn, settings,
        limit=store.as_int(settings.get("batch_apply_per_tick"), 25))
    out["collected"] = applied["done"]
    out["failed"] = applied["failed"]
    out["ready"] = ready_depth(conn)

    # Only submission is gated on the setting. Everything above — polling,
    # collecting, applying — must run regardless, or turning batch mode off
    # mid-flight strands every parked item at awaiting_ai.
    depth = queue_depth(conn)
    if not enabled(settings):
        out["requeued"] = requeue_queued(conn) if depth else 0
        return out
    floor = store.as_int(settings.get("batch_min_items"), 25)
    if depth and (depth >= floor or not out["waiting"]):
        _, n = submit(conn, settings)
        out["submitted"] = n
    return out
