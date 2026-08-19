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

from taxdb import db, export, ingest, ledger
from taxdb.vocab import (
    CATEGORIES, CONFIDENCE, ELECTIONS, EXTRACTION_METHODS, FRAMEWORK,
    INSTRUMENTS, RATE_UNITS, SOURCE_TYPES, STATUSES, TERNARY, WORK_CATEGORIES,
    WORK_STATUSES,
)

from . import autopilot, crawl, extract, intake, interview, present, store, worker
from .settings import (
    ANTHROPIC_MODELS, VALID_KINDS, VALID_PROVIDERS, VALID_SCHEDULE, VALID_SEARCH,
)

HERE = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))

app = FastAPI(title="Tax Database Collector", version="0.4.0")
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
        progress = autopilot.progress(conn, settings) if stats["ready"] else None
        week = present.week(conn)
    finally:
        conn.close()
    user = os.environ.get("COLLECTOR_USER", "")
    ctx = {
        "request": request,
        "week": week,
        "user": user,
        "user_initials": "".join(w[0] for w in user.replace(".", " ").split()[:2]).upper(),
        "tally": _tally(progress),
        "settings": store.mask(settings),
        "stats": stats,
        "progress": progress,
        "running": store.as_bool(settings.get("continuous_enabled")),
        "collecting": _collecting_line(settings, stats, worker.snapshot()),
        "blockers": _blockers(settings, stats),
        "worker": worker.snapshot(),
        "categories": CATEGORIES,
        "work_categories": WORK_CATEGORIES,
        "instruments": INSTRUMENTS,
        "searches": VALID_SEARCH,
        "statuses": sorted(STATUSES),
        "rate_units": sorted(RATE_UNITS),
        "source_types": sorted(SOURCE_TYPES),
        "confidence": sorted(CONFIDENCE),
        "ternary": sorted(TERNARY),
        "work_statuses": sorted(WORK_STATUSES),
        "providers": VALID_PROVIDERS,
        "anthropic_models": ANTHROPIC_MODELS,
        "schedules": VALID_SCHEDULE,
        "kinds": VALID_KINDS,
        "extraction_methods": sorted(EXTRACTION_METHODS),
        "instruments_json": json.dumps(INSTRUMENTS),
        "categories_json": json.dumps(CATEGORIES),
    }
    ctx.update(extra)
    return ctx


def _tally(progress):
    """The header's one figure: places researched out of places on file."""
    if not progress or not progress.get("juris_total"):
        return None
    done = progress["juris_done"]
    total = progress["juris_total"]
    return {
        "done": done,
        "total": total,
        "left": total - done,
        "pct": round(100.0 * done / total, 1),
    }


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
    in_progress = n("SELECT COUNT(*) FROM work_item WHERE status='in_progress'") if tot else 0
    review = n("SELECT COUNT(*) FROM work_item WHERE status='needs_review'") if tot else 0
    done = n("SELECT COUNT(*) FROM work_item WHERE status IN ('complete','no_data')") if tot else 0
    running = n("SELECT COUNT(*) FROM crawl_run WHERE status='running'")
    instruments = n("SELECT COUNT(*) FROM tax_instrument WHERE superseded_by IS NULL") if db_exists else 0
    pages = n("SELECT COUNT(*) FROM crawl_page")
    try:
        intake_queued = n("SELECT COUNT(*) FROM intake_item WHERE status='queued'")
    except Exception:
        intake_queued = 0
    try:
        auto_verified = n(
            "SELECT COUNT(*) FROM (SELECT geoid, category FROM check_result "
            "WHERE verdict='pass' GROUP BY geoid, category)")
    except Exception:
        auto_verified = 0
    blocked = n("SELECT COUNT(*) FROM work_item WHERE status='blocked'") if tot else 0
    awaiting = n("SELECT COUNT(*) FROM work_item WHERE status='awaiting_ai'") if tot else 0
    try:
        batch_queued = n("SELECT COUNT(*) FROM extract_batch_item "
                         "WHERE status='queued'")
        batch_flight = n("SELECT COUNT(*) FROM extract_batch "
                         "WHERE status IN ('submitted','ended')")
    except Exception:
        batch_queued = batch_flight = 0
    return {
        "jurisdictions": n_j,
        "work_items": tot,
        "pending": pending,
        "in_progress": in_progress,
        "needs_review": review,
        "blocked": blocked,
        # Crawled and waiting on a batch read. Without this the dashboard reads
        # as stalled while thousands of items are legitimately in flight.
        "awaiting_ai": awaiting,
        "batch_queued": batch_queued,
        "batch_in_flight": batch_flight,
        "done": done,
        "pct_done": round(100.0 * done / tot, 1) if tot else 0,
        "auto_verified": auto_verified,
        "runs_active": running,
        "instruments": instruments,
        "pages": pages,
        "intake_queued": intake_queued,
        "db_path": db.db_path(),
        "ready": n_j > 0,
    }


def _blockers(settings, stats):
    """The short list of things stopping unattended collection.

    One decision each, in the order they matter. The home page shows the first
    one and nothing else, so a new user is never looking at four problems.
    """
    out = []
    if not stats["ready"]:
        out.append({
            "id": "seed",
            "title": "Set up the database",
            "detail": "Downloads the Census list of every US county and city. "
                      "Takes a few minutes and only happens once.",
            "action": "Set up and start",
        })
        return out
    if (settings.get("provider") or "none") == "none":
        out.append({
            "id": "provider",
            "title": "Add an AI key so it can read documents",
            "detail": "Without one, pages are downloaded and filed but nobody "
                      "reads them, so no rates get recorded.",
            "action": "Open settings",
            "href": "/settings",
        })
    return out


def _collecting_line(settings, stats, snap):
    """One sentence: what the collector is doing right now.

    Paused is checked before the current step, otherwise the page keeps
    reporting the job that was in flight when someone hit pause as though it
    were still starting new work.
    """
    running = store.as_bool(settings.get("continuous_enabled"))
    busy = snap.get("state") == "running"
    if not running:
        if busy:
            return "Finishing the current item, then stopping"
        return "Paused. Nothing is being collected."
    if snap.get("state") == "error":
        return snap.get("message") or "Something went wrong"
    if stats.get("awaiting_ai") and not snap.get("step"):
        return ("Crawling; %d item(s) waiting on a batch read"
                % stats["awaiting_ai"])
    if snap.get("step"):
        return snap["step"]
    if snap.get("current_name"):
        return "Working on %s" % snap["current_name"]
    if snap.get("message"):
        return snap["message"]
    return "Starting up"


# Full-table counts for the data page only. These are scans, so they stay off
# the status poll that the home page hits every couple of seconds.
COUNTED_TABLES = (
    "tax_instrument", "ballot_measure", "threshold_rule", "authority_grant",
    "revenue_base", "statute_section", "coverage_assertion", "claim_source",
    "rate_change_event", "archive_file",
)


def _table_counts(conn):
    counts = {}
    for table in COUNTED_TABLES:
        try:
            counts[table] = conn.execute(
                "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        except Exception:
            counts[table] = 0
    return counts


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: bool = Depends(_auth)):
    """Today: what needs a person, what ran overnight, and one button."""
    conn = store.connect()
    try:
        settings = store.get_all(conn)
        stats = _stats(conn)
        progress = autopilot.progress(conn, settings) if stats["ready"] else None
        next_up = None
        if stats["ready"]:
            plan = autopilot.next_action(conn, settings)
            next_up = plan[2] if plan else None
        inbox = present.inbox(conn, stats, settings, progress)
        timeline = present.timeline(conn)
        published = present.published_since_last_night(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "dashboard.html", _ctx(
        request, next_up=next_up, inbox=inbox, timeline=timeline,
        published_recent=published, greeting=present.greeting(settings.get("researcher")),
        dateline=present.dateline(), nav="today"))


@app.get("/data", response_class=HTMLResponse)
def data_page(request: Request, _: bool = Depends(_auth)):
    """What the database actually holds, and which parts are still empty."""
    conn = store.connect()
    try:
        counts = _table_counts(conn)

        def rows(sql, params=()):
            try:
                return conn.execute(sql, params).fetchall()
            except Exception:
                return []

        sunset = rows(
            "SELECT state_usps, name, kind, category, instrument_code, rate_value, "
            "rate_unit, expiration_date, days_out, prior_revenue_measures, "
            "best_prior_yes FROM v_sunset_watch LIMIT 25")
        near_miss = rows(
            "SELECT state_usps, name, election_date, measure_id_local, measure_class, "
            "pct_yes, threshold_required, margin_vs_threshold, stated_purpose "
            "FROM v_near_miss LIMIT 25")
        headroom = rows(
            "SELECT state_usps, name, kind, category, instrument_code, max_rate, "
            "max_rate_unit, levied_rate, headroom FROM v_headroom "
            "WHERE headroom > 0 ORDER BY population DESC LIMIT 25")
        gap = rows(
            "SELECT state_usps, name, category, instrument_code, change_type, "
            "rate_before, rate_after, effective_period FROM v_measure_capture_gap "
            "LIMIT 25")
        by_state = rows(
            "SELECT j.state_usps AS st, COUNT(*) AS n, "
            "SUM(w.status='complete') AS done, "
            "SUM(w.status='needs_review') AS review, "
            "SUM(w.status='pending') AS pending, "
            "SUM(w.status='blocked') AS blocked "
            "FROM work_item w JOIN jurisdiction j ON j.geoid=w.geoid "
            "GROUP BY j.state_usps ORDER BY j.state_usps")
        coverage_rows = rows(
            "SELECT completeness, COUNT(*) c FROM coverage_assertion "
            "WHERE domain='ballot_measure' GROUP BY completeness ORDER BY 2 DESC")
    finally:
        conn.close()
    return templates.TemplateResponse(request, "data.html", _ctx(
        request, counts=counts, sunset=sunset, near_miss=near_miss,
        headroom=headroom, gap=gap, by_state=by_state,
        coverage_rows=coverage_rows, nav="data"))


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
    """One flagged place at a time, with the archived source beside it."""
    conn = store.connect()
    try:
        items = present.review_items(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "review.html", _ctx(
        request, items=items, nav="review"))


@app.get("/record", response_class=HTMLResponse)
def record_page(request: Request, q: str = "", filter: str = "all", page: int = 1,
                _: bool = Depends(_auth)):
    """Every place we track: what we hold, and when we last looked."""
    conn = store.connect()
    try:
        view = present.record(conn, q=q, filt=filter, page=page)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "record.html", _ctx(
        request, view=view, q=q, filter_key=filter, nav="record"))


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
        progress = autopilot.progress(conn, s) if stats["ready"] else None
    finally:
        conn.close()
    snap = worker.snapshot()
    return {
        "stats": stats,
        "progress": progress,
        "tally": _tally(progress),
        "worker": snap,
        "collecting": _collecting_line(s, stats, snap),
        "blockers": _blockers(s, stats),
        "running": store.as_bool(s.get("continuous_enabled")),
        "continuous_enabled": store.as_bool(s.get("continuous_enabled")),
        "autopilot_enabled": store.as_bool(s.get("autopilot_enabled")),
        "schedule_enabled": store.as_bool(s.get("schedule_enabled")),
        "checker_enabled": store.as_bool(s.get("checker_enabled")),
        "provider": s.get("provider"),
        "warning": snap.get("warning") or "",
    }


@app.post("/api/settings")
async def api_settings(request: Request, _: bool = Depends(_auth)):
    body = await request.json()
    conn = store.connect()
    try:
        current = store.get_all(conn)
        if body.get("provider") is not None and body["provider"] not in VALID_PROVIDERS:
            raise HTTPException(400, "invalid provider")
        if body.get("schedule_kind") is not None and body["schedule_kind"] not in VALID_SCHEDULE:
            raise HTTPException(400, "invalid schedule")
        if body.get("search_provider") is not None \
                and body["search_provider"] not in VALID_SEARCH:
            raise HTTPException(400, "invalid search provider")
        updates = store.sanitize_updates(current, body)
        store.put_many(conn, updates)
    finally:
        conn.close()
    return {"ok": True, "updated": sorted(updates)}


@app.post("/api/provider/test")
def api_provider_test(_: bool = Depends(_auth)):
    """One tiny completion against the saved provider settings."""
    conn = store.connect()
    try:
        s = store.get_all(conn)
    finally:
        conn.close()
    if (s.get("provider") or "none") == "none":
        return {"ok": False, "error": "provider is 'none' — pick one and save first"}
    raw, err = extract.chat(
        s, "Reply with the single word: ready",
        system="You are a connectivity test. Reply with exactly one word: ready")
    return {
        "ok": not err,
        "provider": s.get("provider"),
        "model": extract.default_model(s),
        "response": (raw or "").strip()[:200],
        "error": err,
    }


@app.get("/api/activity")
def api_activity(limit: int = 30, _: bool = Depends(_auth)):
    """Recent problems and checker verdicts, newest first."""
    limit = max(1, min(int(limit or 30), 100))
    events = []
    conn = store.connect()
    try:
        for r in conn.execute(
                "SELECT id, mode, status, message, "
                "COALESCE(finished_at, started_at) AS ts FROM crawl_run "
                "WHERE status IN ('failed','stopped') "
                "ORDER BY id DESC LIMIT ?", (limit,)):
            events.append({
                "kind": "run_" + r["status"], "ts": r["ts"],
                "title": "Run #%d (%s) %s" % (r["id"], r["mode"], r["status"]),
                "detail": r["message"] or "", "href": "/runs?run_id=%d" % r["id"],
            })
        for r in conn.execute(
                "SELECT id, geoid, category, filename, url, error, "
                "COALESCE(finished_at, created_at) AS ts FROM intake_item "
                "WHERE status='failed' ORDER BY id DESC LIMIT ?", (limit,)):
            events.append({
                "kind": "intake_failed", "ts": r["ts"],
                "title": "Intake #%d failed (%s)" % (
                    r["id"], r["filename"] or r["url"] or "item"),
                "detail": r["error"] or "", "href": "/manual",
            })
        for r in conn.execute(
                "SELECT c.id, c.geoid, c.category, c.verdict, c.flags, c.created_at AS ts, "
                "j.name, j.state_usps FROM check_result c "
                "LEFT JOIN jurisdiction j ON j.geoid=c.geoid "
                "WHERE c.verdict IN ('flag','error') "
                "ORDER BY c.id DESC LIMIT ?", (limit,)):
            try:
                flags = json.loads(r["flags"]) if r["flags"] else []
            except ValueError:
                flags = []
            detail = "; ".join(
                "%s: %s" % (f.get("instrument_code") or "?", f.get("reason") or "")
                for f in flags[:3]) or "checker could not run"
            events.append({
                "kind": "check_" + r["verdict"], "ts": r["ts"],
                "title": "%s (%s) / %s %s" % (
                    r["name"] or r["geoid"], r["state_usps"] or "?",
                    r["category"],
                    "flagged" if r["verdict"] == "flag" else "check errored"),
                "detail": detail, "href": "/review",
            })
    finally:
        conn.close()
    events.sort(key=lambda e: e["ts"] or "", reverse=True)
    return events[:limit]


@app.get("/api/timeline")
def api_timeline(limit: int = 8, _: bool = Depends(_auth)):
    """The 'while you were away' feed, for the home page's own refresh."""
    conn = store.connect()
    try:
        return present.timeline(conn, limit=max(1, min(int(limit or 8), 30)))
    finally:
        conn.close()


@app.post("/api/start")
def api_start(_: bool = Depends(_auth)):
    """Turn on unattended collection, and set it up first if needed.

    This is the whole home page. Everything it does used to be four separate
    buttons plus a form nobody could fill in correctly the first time.
    """
    conn = store.connect()
    try:
        store.put_many(conn, {"continuous_enabled": "1", "autopilot_enabled": "1"})
        stats = _stats(conn)
    finally:
        conn.close()
    worker.resume()
    worker.start()
    return {"ok": True, "running": True, "ready": stats["ready"]}


@app.post("/api/pause")
def api_pause(_: bool = Depends(_auth)):
    worker.request_stop()
    return {"ok": True, "running": False}


@app.post("/api/autopilot/step")
def api_autopilot_step(_: bool = Depends(_auth)):
    """Run one autopilot step now, without turning on continuous mode."""
    conn = store.connect()
    try:
        settings = store.get_all(conn)
        plan = autopilot.next_action(conn, settings)
    finally:
        conn.close()
    if not plan:
        return {"ok": True, "action": None,
                "label": "nothing to do — everything planned is current"}
    action, kwargs, label = plan

    def _go():
        try:
            worker.run_action(action, kwargs, settings)
        except Exception:
            pass

    threading.Thread(target=_go, name="autopilot-step", daemon=True).start()
    return {"ok": True, "action": action, "label": label}


@app.post("/api/export")
def api_export(_: bool = Depends(_auth)):
    conn = store.connect()
    try:
        outdir, counts = export.export_all(conn)
    finally:
        conn.close()
    return {"ok": True, "dir": outdir, "files": counts,
            "rows": sum(counts.values())}


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
    cats = [x.strip() for x in (body.get("categories") or ",".join(WORK_CATEGORIES)).split(",") if x.strip()]
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
    if category in (FRAMEWORK, ELECTIONS):
        raise HTTPException(
            400, "The guided questions cover tax rates. For %s, open the item "
                 "in Review or drop the document under Add a source." % category)
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
    if category and category not in WORK_CATEGORIES:
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
