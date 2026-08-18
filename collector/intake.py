"""User-supplied sources (URL, PDF, image) that the AI should read."""

import hashlib
import os
import uuid

from taxdb import archive, db, ingest, ledger, packets

from . import crawl, extract, store

INTAKE_DIR = os.path.join(db.DATA_DIR, "intake")

ALLOWED = {
    "application/pdf": "pdf",
    "image/png": "image",
    "image/jpeg": "image",
    "image/jpg": "image",
    "image/webp": "image",
    "image/gif": "image",
    "image/heic": "image",
    "image/heif": "image",
}


def kind_of(content_type, filename=""):
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in ALLOWED:
        return ALLOWED[ctype]
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".heif")):
        return "image"
    return None


def save_upload(filename, content_type, blob):
    os.makedirs(INTAKE_DIR, exist_ok=True)
    sha = hashlib.sha256(blob).hexdigest()
    ext = os.path.splitext(filename or "")[1][:8] or ""
    stored = os.path.join(INTAKE_DIR, "%s_%s%s" % (sha[:16], uuid.uuid4().hex[:8], ext))
    with open(stored, "wb") as fh:
        fh.write(blob)
    return stored, sha


def enqueue(conn, geoid, category, kind, url=None, filename=None, blob=None,
            content_type=None, note=None):
    store_path = None
    sha = None
    size = None
    if blob:
        store_path, sha = save_upload(filename, content_type, blob)
        size = len(blob)
        if kind == "pdf" or (content_type or "").startswith("application/pdf"):
            page = {
                "url": url or ("file:" + (filename or "upload.pdf")),
                "final_url": url or ("file:" + (filename or "upload.pdf")),
                "blob": blob,
                "content_type": content_type or "application/pdf",
                "title": filename,
                "sha256": sha,
            }
            try:
                archive.put(
                    conn, "intake", page["url"], blob, "intake",
                    content_type=page["content_type"], filename=filename)
            except Exception:
                pass
    cur = conn.execute(
        "INSERT INTO intake_item (geoid, category, kind, url, filename, store_path, "
        "content_type, sha256, byte_size, note, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (geoid, category, kind, url, filename, store_path, content_type, sha, size,
         note, "queued", db.now()))
    conn.commit()
    _ensure_work(conn, geoid, category)
    return cur.lastrowid


def _ensure_work(conn, geoid, category):
    if not geoid or not category:
        return
    j = conn.execute("SELECT kind, population FROM jurisdiction WHERE geoid=?",
                     (geoid,)).fetchone()
    pri = ledger.priority_for(j["kind"], j["population"]) if j else 0
    conn.execute(
        "INSERT OR IGNORE INTO work_item (geoid, category, priority, updated_at) "
        "VALUES (?,?,?,?)", (geoid, category, pri, db.now()))
    conn.execute(
        "UPDATE work_item SET status='in_progress', last_error=?, updated_at=? "
        "WHERE geoid=? AND category=? AND status='pending'",
        ("intake queued", db.now(), geoid, category))
    conn.commit()


def list_items(conn, geoid=None, status=None, limit=50):
    sql = "SELECT * FROM intake_item WHERE 1=1"
    params = []
    if geoid:
        sql += " AND geoid=?"
        params.append(geoid)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def next_queued(conn):
    return conn.execute(
        "SELECT * FROM intake_item WHERE status='queued' ORDER BY id LIMIT 1"
    ).fetchone()


def process_item(conn, settings, item):
    """Fetch/read the source, extract, ingest. Returns findings written."""
    item_id = item["id"] if not isinstance(item, dict) else item["id"]
    if not isinstance(item, dict):
        item = dict(item)
    conn.execute(
        "UPDATE intake_item SET status='running' WHERE id=?", (item_id,))
    conn.commit()
    geoid, category = item.get("geoid"), item.get("category")
    try:
        text, images, err = _materialize(conn, settings, item)
        if err and not text and not images:
            _fail(conn, item_id, err)
            return 0
        if (settings.get("provider") or "none") == "none":
            conn.execute(
                "UPDATE intake_item SET status='ok', finished_at=?, error=? WHERE id=?",
                (db.now(), "archived; no AI provider — run extract after setting one",
                 item_id))
            conn.commit()
            return 0
        packet = ""
        if geoid:
            try:
                packet = packets.build(conn, geoid, [category] if category else None)
            except SystemExit:
                packet = "Extract tax facts from this operator-supplied source."
        else:
            packet = "Extract tax facts from this operator-supplied source."
        docs = text or ""
        if item.get("note"):
            docs = ("Operator note: %s\n\n" % item["note"]) + docs
        if item.get("url"):
            docs = ("Source URL: %s\n\n" % item["url"]) + docs
        researcher = settings.get("researcher") or "intake"
        raw, doc, xerr = extract.extract(
            settings, packet, docs, researcher=researcher, images=images)
        conn.execute(
            "INSERT INTO crawl_extract (run_id, geoid, category, provider, model, "
            "raw_response, parsed_ok, error, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (None, geoid, category, settings.get("provider"),
             _model(settings), (raw or "")[:200000],
             0 if xerr or not doc else 1, xerr, db.now()))
        if xerr or not doc:
            _fail(conn, item_id, xerr or "extract failed")
            return 0
        for f in doc.get("findings") or []:
            if geoid:
                f.setdefault("geoid", geoid)
            if category:
                f.setdefault("category", category)
            f.setdefault("extraction_method", "agent_research")
            if item.get("url"):
                src = f.get("source") or {}
                src.setdefault("url", item["url"])
                f["source"] = src
        res = ingest.load_doc(conn, doc, allow_partial=True,
                              label="intake:%s" % item_id)
        conn.execute(
            "UPDATE intake_item SET status='ok', finished_at=?, findings_written=?, "
            "error=? WHERE id=?",
            (db.now(), res.get("written") or 0,
             None if res.get("written") else "0 valid findings",
             item_id))
        if geoid and category:
            ledger.set_status(conn, geoid, category, "needs_review")
        conn.commit()
        return res.get("written") or 0
    except Exception as exc:
        _fail(conn, item_id, str(exc)[:500])
        return 0


def _fail(conn, item_id, message):
    conn.execute(
        "UPDATE intake_item SET status='failed', finished_at=?, error=? WHERE id=?",
        (db.now(), message, item_id))
    conn.commit()


def _model(s):
    p = s.get("provider")
    if p == "openai":
        return s.get("openai_model")
    if p == "anthropic":
        return s.get("anthropic_model")
    if p == "llama":
        return s.get("llama_model")
    return None


def _materialize(conn, settings, item):
    kind = item.get("kind")
    if kind == "url":
        client = crawl.client_for(settings)
        try:
            page = crawl.fetch_one(client, item["url"], settings)
        finally:
            client.close()
        if page.get("blob") and page.get("robots_allowed"):
            try:
                crawl.archive_page(conn, page)
            except Exception:
                pass
        crawl.record_page(conn, None, item.get("geoid"), item.get("category"), page, None)
        if page.get("error") and not page.get("text"):
            return "", [], page["error"]
        return page.get("text") or "", [], page.get("error")
    path = item.get("store_path")
    if not path or not os.path.exists(path):
        return "", [], "uploaded file missing on disk"
    with open(path, "rb") as fh:
        blob = fh.read()
    ctype = item.get("content_type") or ""
    if kind == "pdf":
        text = crawl._pdf_text(blob)
        if text and not text.startswith("[pdf extract failed") and len(text.strip()) > 40:
            return text, [], None
        return text or "", [], "PDF had little extractable text — add a screenshot of the rate table"
    if kind == "image":
        mime = ctype.split(";")[0].strip() if ctype else "image/png"
        if mime not in ALLOWED:
            mime = "image/png"
        return "", [{"mime": mime, "data": blob}], None
    return "", [], "unknown intake kind %r" % kind
