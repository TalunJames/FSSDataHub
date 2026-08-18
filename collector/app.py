"""FastAPI application served on TrueNAS Scale."""

import json
import os
import secrets
import threading

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from taxdb import db, ingest, ledger
from taxdb.vocab import (
    CATEGORIES, CONFIDENCE, EXTRACTION_METHODS, INSTRUMENTS, RATE_UNITS,
    SOURCE_TYPES, STATUSES, TERNARY, WORK_STATUSES,
)

from . import crawl, extract, intake, interview, store, worker
from .settings import (
    SECRET_KEYS, VALID_CATEGORIES, VALID_KINDS, VALID_PROVIDERS, VALID_SCHEDULE,
)

HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

app = FastAPI(title="Tax Database Collector", version="0.3.0")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

security = HTTPBasic(auto_error=False)


def _auth(credentials: HTTPBasicCredentials = Depends(security)):
    password = os.environ.get("COLLECTOR_PASSWORD")
    if not password:
        return True
    user = os.environ.get("COLLECTOR_USER", "taxdb")
    if credentials is None:
        raise HTTPException(401, "auth required",
                            headers={"WWW-Authenticate": "Basic"})
    u_ok = secrets.compare_digest(credentials.username, user)
    p_ok = secrets.compare_digest(credentials.password, password)
    if not (u_ok and p_ok):
        raise HTTPException(401, "auth required",
                            headers={"WWW-Authenticate": "Basic"})
    return True


class NoCacheHTML(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static"):
            return response
        response.headers["Cache-Control"] = "no-store"
        return response


app.add_middleware(NoCacheHTML)


@app.on_event("startup")
def _startup():
    os.makedirs(db.CACHE_DIR, exist_ok=True)
    os.makedirs(db.OUT_DIR, exist_ok=True)
    os.makedirs(db.ARCHIVE_DIR, exist_ok=True)
    conn = db.init_db()
    store.apply_schema(conn)
    conn.close()
    worker.start()


def _ctx(request, **extra):
    conn = store.connect()
    try:
        settings = store.get_all(conn)
        stats = _stats(conn)
    finally:
        conn.close()
    ctx = {
        "request": request,
        "settings": store.mask(settings),
        "stats": stats,
        "worker": worker.snapshot(),
        "categories": CATEGORIES,
        "instruments": INSTRUMENTS,
        "statuses": sorted(STATUSES),
        "rate_units": sorted(RATE_UNITS),
        "source_types": sorted(SOURCE_TYPES),
        "confidence": sorted(CONFIDENCE),
        "ternary": sorted(TERNARY),
        "work_statuses": sorted(WORK_STATUSES),
        "providers": VALID_PROVIDERS,
        "schedules": VALID_SCHEDULE,
        "kinds": VALID_KINDS,
        "extraction_methods": sorted(EXTRACTION_METHODS),
        "instruments_json": json.dumps(INSTRUMENTS),
        "categories_json": json.dumps(CATEGORIES),
    }
    ctx.update(extra)
    return ctx


def _stats(conn):
    def n(sql, params=()):
        return conn.execute(sql, params).fetchone()[0]

    db_exists = True
    try:
        n_j = n("SELECT COUNT(*) FROM jurisdiction")
    except Exception:
        n_j = 0
        db_exists = False
    tot = n("SELECT COUNT(*) FROM work_item") if db_exists else 0
    pending = n("SELECT COUNT(*) FROM work_item WHERE status='pending'") if tot else 0
    review = n("SELECT COUNT(*) FROM work_item WHERE status='needs_review'") if tot else 0
    running = n("SELECT COUNT(*) FROM crawl_run WHERE status='running'")
    instruments = n("SELECT COUNT(*) FROM tax_instrument WHERE superseded_by IS NULL") if db_exists else 0
    pages = n("SELECT COUNT(*) FROM crawl_page")
    try:
        intake_queued = n("SELECT COUNT(*) FROM intake_item WHERE status='queued'")
    except Exception:
        intake_queued = 0
    return {
        "jurisdictions": n_j,
        "work_items": tot,
        "pending": pending,
        "needs_review": review,
        "runs_active": running,
        "instruments": instruments,
        "pages": pages,
        "intake_queued": intake_queued,
        "db_path": db.db_path(),
        "ready": n_j > 0,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: bool = Depends(_auth)):
    conn = store.connect()
    try:
        recent = conn.execute(
            "SELECT * FROM crawl_run ORDER BY id DESC LIMIT 12").fetchall()
        queue = conn.execute(
            "SELECT status, COUNT(*) c FROM work_item GROUP BY status "
            "ORDER BY c DESC").fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "dashboard.html", _ctx(
        request, recent=recent, queue=queue, nav="dash"))


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, _: bool = Depends(_auth)):
    return templates.TemplateResponse(request, "settings.html", _ctx(request, nav="settings"))


@app.get("/manual", response_class=HTMLResponse)
def manual_page(request: Request, geoid: str = "", category: str = "",
                _: bool = Depends(_auth)):
    return templates.TemplateResponse(request, "manual.html", _ctx(
        request, nav="manual",
        start_geoid_json=json.dumps(geoid or ""),
        start_category_json=json.dumps(category or "")))


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request, _: bool = Depends(_auth)):
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT w.id, w.geoid, w.category, w.last_error, w.updated_at, "
            "w.priority, j.name, j.state_usps, j.kind, j.population "
            "FROM work_item w JOIN jurisdiction j ON j.geoid=w.geoid "
            "WHERE w.status='needs_review' ORDER BY w.priority DESC LIMIT 80"
        ).fetchall()
        findings = {}
        for r in rows[:40]:
            findings[r["geoid"] + "/" + r["category"]] = conn.execute(
                "SELECT t.id, t.instrument_code, t.status, t.rate_value, t.rate_unit, "
                "t.confidence, t.extraction_method, t.retrieved_at, s.url "
                "FROM tax_instrument t JOIN source s ON s.id=t.source_id "
                "WHERE t.geoid=? AND t.category=? AND t.superseded_by IS NULL",
                (r["geoid"], r["category"])).fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "review.html", _ctx(
        request, rows=rows, findings=findings, nav="review"))


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, run_id: int = None, _: bool = Depends(_auth)):
    conn = store.connect()
    try:
        runs = conn.execute(
            "SELECT * FROM crawl_run ORDER BY id DESC LIMIT 50").fetchall()
        pages = []
        extracts = []
        active = run_id
        if not active and runs:
            active = runs[0]["id"]
        if active:
            pages = conn.execute(
                "SELECT * FROM crawl_page WHERE run_id=? ORDER BY id DESC LIMIT 100",
                (active,)).fetchall()
            extracts = conn.execute(
                "SELECT id, geoid, category, provider, model, parsed_ok, error, "
                "created_at, substr(raw_response,1,400) AS preview "
                "FROM crawl_extract WHERE run_id=? ORDER BY id DESC LIMIT 40",
                (active,)).fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "runs.html", _ctx(
        request, runs=runs, pages=pages, extracts=extracts, active=active, nav="runs"))


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, state: str = "", status: str = "",
               _: bool = Depends(_auth)):
    conn = store.connect()
    try:
        sql = ("SELECT w.id, w.geoid, w.category, w.status, w.priority, w.attempts, "
               "w.last_error, j.name, j.state_usps, j.kind, j.population "
               "FROM work_item w JOIN jurisdiction j ON j.geoid=w.geoid WHERE 1=1")
        params = []
        if state:
            sql += " AND j.state_usps=?"
            params.append(state.upper())
        if status:
            sql += " AND w.status=?"
            params.append(status)
        sql += " ORDER BY w.priority DESC LIMIT 200"
        rows = conn.execute(sql, params).fetchall()
        states = [r[0] for r in conn.execute(
            "SELECT DISTINCT state_usps FROM jurisdiction ORDER BY 1")]
    finally:
        conn.close()
    return templates.TemplateResponse(request, "queue.html", _ctx(
        request, rows=rows, states=states, filter_state=state.upper(),
        filter_status=status, nav="queue"))


@app.get("/api/status")
def api_status(_: bool = Depends(_auth)):
    conn = store.connect()
    try:
        stats = _stats(conn)
        s = store.get_all(conn)
    finally:
        conn.close()
    snap = worker.snapshot()
    return {
        "stats": stats,
        "worker": snap,
        "continuous_enabled": store.as_bool(s.get("continuous_enabled")),
        "schedule_enabled": store.as_bool(s.get("schedule_enabled")),
        "provider": s.get("provider"),
    }


@app.post("/api/settings")
async def api_settings(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    conn = store.connect()
    try:
        current = store.get_all(conn)
        updates = {}
        for k, v in body.items():
            if k not in current and k not in SECRET_KEYS:
                continue
            if k in SECRET_KEYS and isinstance(v, str) and set(v) <= set("*") | set("0123456789"):
                # ignore masked placeholder unless it looks like a new key (has non-star)
                if "*" in v:
                    continue
            if k == "provider" and v not in VALID_PROVIDERS:
                raise HTTPException(400, "invalid provider")
            if k == "schedule_kind" and v not in VALID_SCHEDULE:
                raise HTTPException(400, "invalid schedule")
            updates[k] = v if v is None else str(v)
        store.put_many(conn, updates)
    finally:
        conn.close()
    return {"ok": True, "updated": sorted(updates)}


@app.post("/api/crawl/toggle")
async def api_toggle(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    enabled = bool(body.get("enabled"))
    conn = store.connect()
    try:
        store.put(conn, "continuous_enabled", "1" if enabled else "0")
        conn.commit()
    finally:
        conn.close()
    if not enabled:
        worker.request_stop()
    else:
        worker.resume()
    return {"ok": True, "enabled": enabled}


@app.post("/api/crawl/burst")
async def api_burst(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    size = int(body.get("size") or 20)
    worker.request_burst(size)
    return {"ok": True, "size": size}


@app.post("/api/crawl/stop")
def api_stop(_: bool = Depends(_auth)):
    worker.request_stop()
    return {"ok": True}


@app.post("/api/init")
def api_init(_: bool = Depends(_auth)):
    conn = db.init_db()
    store.apply_schema(conn)
    from taxdb import sources as src
    n = src.seed_catalog(conn)
    conn.close()
    return {"ok": True, "sources": n, "path": db.db_path()}


@app.post("/api/seed")
async def api_seed(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    snap = worker.snapshot()
    if snap.get("state") == "running":
        raise HTTPException(409, "collector is busy")

    def _go():
        worker.run_seed(
            include_mcd=bool(body.get("include_mcd")),
            counties_only=bool(body.get("counties_only")),
            force=bool(body.get("force")),
        )

    threading.Thread(target=_go, name="seed", daemon=True).start()
    return {"ok": True, "started": True}


@app.post("/api/plan")
async def api_plan(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    states = [x.strip().upper() for x in (body.get("states") or "").replace(",", " ").split() if x.strip()]
    kinds = [x.strip() for x in (body.get("kinds") or "county,place,state").split(",") if x.strip()]
    cats = [x.strip() for x in (body.get("categories") or ",".join(CATEGORIES)).split(",") if x.strip()]
    min_pop = int(body.get("min_pop") or 0)
    limit = body.get("limit")
    limit = int(limit) if limit else None
    n = worker.run_plan(states or None, kinds=kinds, categories=cats,
                        min_pop=min_pop, limit=limit)
    return {"ok": True, "created": n}


@app.post("/api/bulk/sst")
async def api_sst(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    snap = worker.snapshot()
    if snap.get("state") == "running":
        raise HTTPException(409, "collector is busy")
    states = [x.strip().upper() for x in (body.get("states") or "").replace(",", " ").split() if x.strip()]

    def _go():
        worker.run_adapter("sst", states=states or None)

    threading.Thread(target=_go, name="sst", daemon=True).start()
    return {"ok": True, "started": True, "states": states}


@app.post("/api/bulk/cog")
async def api_cog(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    snap = worker.snapshot()
    if snap.get("state") == "running":
        raise HTTPException(409, "collector is busy")

    def _go():
        worker.run_cog(force=bool(body.get("force")))

    threading.Thread(target=_go, name="cog", daemon=True).start()
    return {"ok": True, "started": True}


@app.post("/api/statutes/fetch")
async def api_statutes(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    usps = (body.get("state") or "").strip().upper()
    if len(usps) != 2:
        raise HTTPException(400, "state USPS code required")
    snap = worker.snapshot()
    if snap.get("state") == "running":
        raise HTTPException(409, "collector is busy")

    def _go():
        worker.run_statutes(usps, force=bool(body.get("force")))

    threading.Thread(target=_go, name="statutes", daemon=True).start()
    return {"ok": True, "started": True, "state": usps}


@app.get("/api/jurisdictions")
def api_jurisdictions(q: str = "", _: bool = Depends(_auth)):
    q = (q or "").strip()
    if len(q) < 2:
        return []
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT geoid, name, state_usps, kind, population FROM jurisdiction "
            "WHERE name LIKE ? OR geoid = ? OR state_usps = ? "
            "ORDER BY population DESC LIMIT 30",
            ("%" + q + "%", q, q.upper())).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post("/api/ingest")
async def api_ingest(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    if "findings" not in body and "finding" in body:
        body = {
            "schema_version": "1.0",
            "researcher": body.get("researcher") or "manual",
            "extraction_method": "manual",
            "findings": [body["finding"]],
        }
    body.setdefault("extraction_method", "manual")
    body.setdefault("researcher", "manual")
    conn = store.connect()
    try:
        res = ingest.load_doc(conn, body, allow_partial=True, label="manual")
    except SystemExit as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()
    return res


@app.post("/api/ingest-file")
async def api_ingest_file(file: UploadFile = File(...), _: bool = Depends(_auth)):
    raw = await file.read()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "not JSON: %s" % exc)
    conn = store.connect()
    try:
        res = ingest.load_doc(conn, doc, allow_partial=True, label=file.filename or "upload")
    except SystemExit as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()
    return res


@app.post("/api/review")
async def api_review(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    geoid = body.get("geoid")
    category = body.get("category")
    status = body.get("status") or "complete"
    if not geoid or not category:
        raise HTTPException(400, "geoid and category required")
    if status not in WORK_STATUSES:
        raise HTTPException(400, "bad status")
    conn = store.connect()
    try:
        ledger.set_status(conn, geoid, category, status)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/fetch-url")
async def api_fetch_url(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        raise HTTPException(400, "url must be http(s)")
    conn = store.connect()
    try:
        s = store.get_all(conn)
        run_id = store.start_run(conn, "manual_url", provider=s.get("provider"))
        client = crawl.client_for(s)
        try:
            page = crawl.fetch_one(client, url, s)
        finally:
            client.close()
        aid = None
        if page.get("blob") and page.get("robots_allowed"):
            aid = crawl.archive_page(conn, page)
        crawl.record_page(conn, run_id, body.get("geoid"), body.get("category"), page, aid)
        n_find = 0
        extract_err = None
        preview = (page.get("text") or "")[:8000]
        if body.get("extract") and (s.get("provider") or "none") != "none" and page.get("text"):
            geoid = body.get("geoid")
            cats = [body["category"]] if body.get("category") else None
            packet = ""
            if geoid:
                from taxdb import packets
                packet = packets.build(conn, geoid, cats)
            raw, doc, extract_err = extract.extract(s, packet or "Extract tax facts from this page.", preview)
            conn.execute(
                "INSERT INTO crawl_extract (run_id, geoid, category, provider, model, "
                "raw_response, parsed_ok, error, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, geoid, body.get("category"), s.get("provider"),
                 worker._model_name(s), (raw or "")[:200000],
                 0 if extract_err or not doc else 1, extract_err, db.now()))
            if doc and not extract_err:
                res = ingest.load_doc(conn, doc, allow_partial=True, label="manual_url")
                n_find = res["written"]
        store.finish_run(conn, run_id, "ok" if not page.get("error") else "failed",
                         page.get("error"), pages=1, findings=n_find)
        return {
            "ok": not page.get("error"),
            "http_status": page.get("http_status"),
            "title": page.get("title"),
            "text": preview,
            "archive_file_id": aid,
            "error": page.get("error"),
            "extract_error": extract_err,
            "findings_written": n_find,
            "robots_allowed": page.get("robots_allowed"),
        }
    finally:
        conn.close()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/packet/{geoid}")
def api_packet(geoid: str, category: str = None, _: bool = Depends(_auth)):
    from taxdb import packets
    conn = store.connect()
    try:
        cats = [category] if category else None
        return HTMLResponse(packets.build(conn, geoid, cats), media_type="text/markdown")
    except SystemExit as exc:
        raise HTTPException(404, str(exc))
    finally:
        conn.close()


@app.get("/api/manual/inbox")
def api_inbox(_: bool = Depends(_auth)):
    conn = store.connect()
    try:
        return interview.inbox(conn)
    finally:
        conn.close()


@app.get("/api/interview")
def api_interview(geoid: str, category: str, _: bool = Depends(_auth)):
    if category not in CATEGORIES:
        raise HTTPException(400, "unknown category")
    conn = store.connect()
    try:
        payload = interview.session(conn, geoid, category)
        if not payload:
            raise HTTPException(404, "no jurisdiction %s" % geoid)
        return payload
    finally:
        conn.close()


@app.post("/api/interview/answer")
async def api_interview_answer(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    geoid = body.get("geoid")
    category = body.get("category")
    action = body.get("action")
    if not geoid or not category:
        raise HTTPException(400, "geoid and category required")
    if action not in ("answered", "skipped", "unknown", "skip_rest"):
        raise HTTPException(400, "bad action")
    conn = store.connect()
    try:
        sess = interview.session(conn, geoid, category)
        if not sess:
            raise HTTPException(404, "no jurisdiction")
        question = sess.get("question")
        if body.get("question_id") and question and body["question_id"] != question["id"]:
            question = {"id": body["question_id"],
                        "instrument_code": body["question_id"].split(".")[-1],
                        "title": body["question_id"], "category": category}
        if not question:
            return {"ok": True, "written": 0, "session": sess}
        result = interview.apply_answer(
            conn, geoid, category, question, action,
            body.get("payload") or {},
            researcher=body.get("researcher") or "manual")
        sess = interview.session(conn, geoid, category)
        result["session"] = sess
        result["ok"] = True
        return result
    except SystemExit as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


@app.get("/api/intake")
def api_intake_list(geoid: str = None, _: bool = Depends(_auth)):
    conn = store.connect()
    try:
        return intake.list_items(conn, geoid=geoid)
    finally:
        conn.close()


@app.post("/api/intake")
async def api_intake_create(request: Request, _: bool = Depends(_auth)):
    form = await request.form()
    geoid = (form.get("geoid") or "").strip() or None
    category = (form.get("category") or "").strip() or None
    url = (form.get("url") or "").strip() or None
    note = (form.get("note") or "").strip() or None
    if category and category not in CATEGORIES:
        raise HTTPException(400, "unknown category")
    if not geoid:
        raise HTTPException(400, "pick a jurisdiction first")
    conn = store.connect()
    ids = []
    try:
        if url:
            if not url.startswith("http"):
                raise HTTPException(400, "url must be http(s)")
            ids.append(intake.enqueue(conn, geoid, category, "url", url=url, note=note))
        files = form.getlist("files") or form.getlist("file")
        for up in files:
            if not hasattr(up, "read"):
                continue
            data = await up.read()
            if not data:
                continue
            if len(data) > 12 * 1024 * 1024:
                raise HTTPException(400, "%s is larger than 12 MB" % (up.filename or "file"))
            ctype = getattr(up, "content_type", None) or ""
            kind = intake.kind_of(ctype, up.filename or "")
            if not kind:
                raise HTTPException(400, "unsupported file %s — use PDF or an image" % (up.filename or ""))
            ids.append(intake.enqueue(
                conn, geoid, category, kind, url=url, filename=up.filename,
                blob=data, content_type=ctype, note=note))
        if not ids:
            raise HTTPException(400, "provide a URL or drop a PDF / image")
    finally:
        conn.close()
    return {"ok": True, "ids": ids, "queued": len(ids)}


@app.post("/api/intake/{item_id}/run")
def api_intake_run(item_id: int, _: bool = Depends(_auth)):
    def _go():
        conn = store.connect()
        try:
            row = conn.execute("SELECT * FROM intake_item WHERE id=?", (item_id,)).fetchone()
            if not row:
                return
            s = store.get_all(conn)
            intake.process_item(conn, s, row)
        finally:
            conn.close()

    threading.Thread(target=_go, name="intake-%s" % item_id, daemon=True).start()
    return {"ok": True, "started": True}
