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

from taxdb import db, ingest

from . import check, extract, store

API = "https://api.anthropic.com/v1/messages/batches"

# Providers with a batch surface we speak.
SUPPORTED = ("anthropic",)


def enabled(settings):
    """Batch mode is opt-in: it trades minutes of latency for half the bill."""
    return (store.as_bool(settings.get("batch_extract"))
            and (settings.get("provider") or "none").strip().lower() in SUPPORTED)


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
        "max_tokens": 8192,
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
    except Exception as exc:
        # The items stay 'queued', so the next tick tries again. Only the batch
        # row is marked failed, which keeps the failure visible in the log.
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
            raw = _text_of(outcome.get("message"))
            err = None
        else:
            raw, err = None, "batch result %s: %s" % (
                kind, json.dumps(outcome.get("error") or {})[:200])
        conn.execute(
            "UPDATE extract_batch_item SET status='ready', raw_response=?, "
            "error=? WHERE id=?", (raw, err, item["id"]))
        landed += 1
    conn.execute(
        "UPDATE extract_batch SET status='collected', collected_at=? WHERE id=?",
        (db.now(), batch_row["id"]))
    conn.commit()
    return {"landed": landed, "unknown": unknown}


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
    for item in ready(conn, limit):
        try:
            ok = apply_result(conn, settings, item, item["raw_response"],
                              item["error"])
        except Exception as exc:
            # Do not let one poisonous result loop forever on every tick.
            conn.execute(
                "UPDATE extract_batch_item SET status='failed', error=? WHERE id=?",
                (("apply failed: %s" % exc)[:500], item["id"]))
            conn.commit()
            ok = False
        done += 1 if ok else 0
        failed += 0 if ok else 1
    if done or failed:
        conn.execute(
            "UPDATE extract_batch SET n_succeeded=n_succeeded+?, "
            "n_failed=n_failed+? WHERE id=("
            "  SELECT batch_id FROM extract_batch_item WHERE id=?)",
            (done, failed, item["id"]))
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
                          label="batch:%s/%s" % (geoid, category))

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
                        item["doc_text"])
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

    depth = queue_depth(conn)
    floor = store.as_int(settings.get("batch_min_items"), 25)
    if depth and (depth >= floor or not out["waiting"]):
        _, n = submit(conn, settings)
        out["submitted"] = n
    return out
