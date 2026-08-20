"""Search memory: never repeat a dead query, and learn better wording.

Every web query the crawler runs for a work item is logged with its
outcome. When the same item comes back for another round — a human pressed
"Try again", the extractor returned nothing, a stale item is refreshed —
the plan for that round is built from the log:

- Queries that answered with nothing worth keeping are set aside rather
  than re-run word for word. A blocked query (every engine refused) says
  nothing about the wording and stays eligible.
- When earlier rounds produced no findings at all, the reflection step asks
  the checker's model to read what the crawler *did* see — the queries
  tried, the page titles reached, the recorded failure — and propose new
  wording. Those queries run first next round.

Reflection runs on the checker's provider (the free local model by
default), so learning costs nothing on a standard install. If the model is
unreachable the plan simply falls back to the untried built-ins: searching
never waits on reflection.
"""

import json

from taxdb import db
from taxdb.vocab import ELECTIONS, FRAMEWORK

from . import check, extract, store

# Queries actually run in one round. The built-in templates are 5-6; this
# leaves room for AI wording without letting a long log balloon the quota.
MAX_PER_ROUND = 8

# AI suggestions kept per (place, category) before reflection stops. A cap,
# not a target: an item that fails this many rephrasings needs a human, not
# a seventeenth query.
MAX_AI_QUERIES = 16

# What each pass is actually looking for, in the reflection prompt's words.
GOALS = {
    FRAMEWORK: ("the state statutes and constitutional provisions that set "
                "local tax authority, rate caps, and the vote thresholds "
                "for passing local revenue measures"),
    ELECTIONS: ("the county's certified election results — canvasses and "
                "abstracts of votes — for local revenue measures (levies, "
                "bonds, local tax questions)"),
}

REFLECT_SYSTEM = """You improve web-search wording for a crawler that
researches official US local-government tax and election documents.
Earlier rounds of searching did not find what was needed. You are shown
what was searched, how each query did, the pages that were reached, and
why the round failed.

Propose new queries that are genuinely different from the ones already
tried: name the actual office that publishes the document (auditor,
fiscal officer, treasurer, board of elections, recorder), use the words
the jurisdiction's own pages use (visible in the page titles you are
shown), try the state-specific term for the tax or document, or search
the state agency that compiles local rates. Do not repeat or lightly
reword a query that already found nothing.

Respond with ONLY valid JSON:
{"queries": ["...", "..."], "note": "one short line on what you changed"}
Give 2 to 4 queries, each a complete search-engine query string.
"""


def _goal(category):
    return GOALS.get(category, "the jurisdiction's own current local %s tax "
                     "rate, from its official website or official documents"
                     % (category or "").replace("_", " "))


def record_round(conn, geoid, category, diag):
    """File this round's per-query outcomes and clear them from diag.

    diag['by_query'] accumulates within one search call; popping it here
    keeps a second call in the same item (the mid-crawl retry) from
    double-counting the first call's queries.
    """
    by_query = (diag or {}).pop("by_query", None) if diag else None
    if diag is not None:
        diag["by_query"] = {}
    # Banked before the early return, and zeroed as it is banked. An item
    # calls this once per search round, so a counter left standing would be
    # charged again by the next round of the same item.
    _bank_api_calls(conn, diag)
    if not by_query or not geoid:
        conn.commit()
        return 0
    now = db.now()
    n = 0
    for query, info in by_query.items():
        if info.get("blocked"):
            outcome = "blocked"
        elif info.get("kept"):
            outcome = "found"
        else:
            outcome = "nothing"
        # Only a round that actually found something replaces the cache. A
        # blocked engine or an empty result must not wipe URLs that still
        # work, or every throttled night would cost the allowance twice.
        urls = info.get("urls") or []
        results = json.dumps(urls) if urls else None
        conn.execute(
            "INSERT INTO search_query (geoid, category, query, source, tries, "
            "last_outcome, last_kept, results, created_at, last_tried_at) "
            "VALUES (?,?,?,?,1,?,?,?,?,?) "
            "ON CONFLICT(geoid, category, query) DO UPDATE SET "
            "tries=tries+1, last_outcome=excluded.last_outcome, "
            "last_kept=excluded.last_kept, last_tried_at=excluded.last_tried_at, "
            "results=COALESCE(excluded.results, search_query.results)",
            (geoid, category, query, "built_in", outcome,
             int(info.get("kept") or 0), results, now, now))
        n += 1
    conn.commit()
    return n


def _bank_api_calls(conn, diag):
    """Add this round's paid search calls to the month's running total.

    Kept here rather than in `crawl` because this is the one place in the
    search path that already holds a database connection.
    """
    made = int((diag or {}).get("api_calls") or 0)
    if not made:
        return
    diag["api_calls"] = 0
    from . import crawl
    month = crawl.api_month()
    s = store.get_all(conn)
    before = 0 if (s.get("search_api_month") or "") != month \
        else store.as_int(s.get("search_api_calls"), 0)
    store.put_many(conn, {"search_api_month": month,
                          "search_api_calls": str(before + made)})


def reuse(conn, settings, geoid, category, queries):
    """Split a plan into (urls already on file, queries still worth asking).

    Wording this item ran recently, and that found something, is not asked
    again: the URLs it returned are handed straight back. Government rate
    pages do not move week to week, and an item that goes back in the queue
    was paying the search allowance again for the same answer — which is
    where a metered search plan actually goes.
    """
    days = store.as_int(settings.get("search_cache_days"), 7)
    if days <= 0 or not queries:
        return [], list(queries)
    rows = conn.execute(
        "SELECT query, results FROM search_query WHERE geoid=? AND category=? "
        "AND results IS NOT NULL AND last_tried_at IS NOT NULL "
        "AND last_tried_at >= datetime('now', ?)",
        (geoid, category, "-%d days" % days)).fetchall()
    fresh = {}
    for r in rows:
        try:
            urls = json.loads(r["results"])
        except (TypeError, ValueError):
            continue
        if isinstance(urls, list) and urls:
            fresh[r["query"]] = [u for u in urls if isinstance(u, str)]
    if not fresh:
        return [], list(queries)
    out, seen, remaining = [], set(), []
    for q in queries:
        if q in fresh:
            for u in fresh[q]:
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        else:
            remaining.append(q)
    return out, remaining


def _log_rows(conn, geoid, category):
    return conn.execute(
        "SELECT query, source, tries, last_outcome, "
        "COALESCE(last_kept, 0) AS kept FROM search_query "
        "WHERE geoid=? AND category=? ORDER BY id", (geoid, category)).fetchall()


def _nothing_on_file(conn, geoid, category):
    """True when no earlier round wrote a single live row for this item."""
    if category == ELECTIONS:
        n = conn.execute(
            "SELECT COUNT(*) FROM ballot_measure WHERE geoid=? "
            "AND superseded_by IS NULL", (geoid,)).fetchone()[0]
        return n == 0
    if category == FRAMEWORK:
        j = conn.execute("SELECT state_usps FROM jurisdiction WHERE geoid=?",
                         (geoid,)).fetchone()
        usps = j["state_usps"] if j else geoid
        n = conn.execute(
            "SELECT (SELECT COUNT(*) FROM threshold_rule WHERE state_usps=?) "
            "+ (SELECT COUNT(*) FROM authority_grant WHERE state_usps=?)",
            (usps, usps)).fetchone()[0]
        return n == 0
    n = conn.execute(
        "SELECT COUNT(*) FROM tax_instrument WHERE geoid=? AND category=? "
        "AND superseded_by IS NULL", (geoid, category)).fetchone()[0]
    return n == 0


def _last_check_flagged(conn, geoid, category):
    row = conn.execute(
        "SELECT verdict FROM check_result WHERE geoid=? AND category=? "
        "ORDER BY id DESC LIMIT 1", (geoid, category)).fetchone()
    return bool(row) and row["verdict"] in ("flag", "error")


def _should_reflect(conn, geoid, category):
    """Reflect only when the item is plainly not found yet.

    Nothing on file means every earlier round came up dry. A latest check
    verdict of flag/error means what was found did not hold up — a human
    pressed "Try again" or will. A completed item being refreshed matches
    neither and keeps its proven wording without a model call.
    """
    return (_nothing_on_file(conn, geoid, category)
            or _last_check_flagged(conn, geoid, category))


def plan_round(conn, settings, geoid, category, built_in, exclude=None,
               allow_reflect=True):
    """The queries this round should run, in the order to run them.

    built_in is the standard template list for this item (the caller builds
    it — this module stays import-free of crawl). exclude, when given, is
    the set of queries already tried in this item's current crawl: used by
    the mid-crawl retry so it only runs wording the first pass did not.
    """
    exclude = exclude or set()
    rows = _log_rows(conn, geoid, category)
    if not rows:
        # First round ever for this item: nothing to learn from yet.
        return [q for q in built_in if q not in exclude][:MAX_PER_ROUND]

    if allow_reflect:
        untried_ai = [r for r in rows if r["source"] == "ai" and r["tries"] == 0]
        if not untried_ai and _should_reflect(conn, geoid, category):
            try:
                if reflect(conn, settings, geoid, category):
                    rows = _log_rows(conn, geoid, category)
            except Exception:
                # Reflection is an optimization. Searching never waits on it.
                pass

    dead = set()
    ai_first, ai_retry = [], []
    for r in rows:
        if r["source"] == "ai":
            if r["tries"] == 0:
                ai_first.append(r["query"])
            elif r["last_outcome"] in ("found", "blocked"):
                ai_retry.append(r["query"])
        if r["last_outcome"] == "nothing":
            dead.add(r["query"])

    plan, seen = [], set()
    for q in ai_first + [q for q in built_in if q not in dead] + ai_retry:
        if q in seen or q in exclude:
            continue
        seen.add(q)
        plan.append(q)
    if not plan and not exclude:
        # Every known query is dead and reflection had nothing new. Websites
        # change; a stale "nothing" beats not searching at all.
        return list(built_in)[:MAX_PER_ROUND]
    return plan[:MAX_PER_ROUND]


def _recent_pages(conn, geoid, category, limit=20):
    return conn.execute(
        "SELECT title, COALESCE(final_url, url) AS url FROM crawl_page "
        "WHERE geoid=? AND category=? AND error IS NULL "
        "ORDER BY id DESC LIMIT ?", (geoid, category, limit)).fetchall()


def reflect(conn, settings, geoid, category, name=None, state=None):
    """Ask the checker's model for better wording. Returns queries added.

    Free on a standard install (the checker runs on the local model), and
    every failure path returns 0 rather than raising: a crawl must never
    fail because its search coach was unreachable.
    """
    if not store.as_bool(settings.get("search_learn", "1")):
        return 0
    provider = check.checker_provider(settings)
    if provider == "none":
        return 0
    n_ai = conn.execute(
        "SELECT COUNT(*) FROM search_query WHERE geoid=? AND category=? "
        "AND source='ai'", (geoid, category)).fetchone()[0]
    if n_ai >= MAX_AI_QUERIES:
        return 0

    if name is None or state is None:
        j = conn.execute("SELECT name, state_usps, kind FROM jurisdiction "
                         "WHERE geoid=?", (geoid,)).fetchone()
        name = name or (j["name"] if j else geoid)
        state = state or (j["state_usps"] if j else "")

    tried = _log_rows(conn, geoid, category)
    tried_lines = []
    for r in tried:
        tried_lines.append("- %r -> %s (%d result(s) kept)"
                           % (r["query"], r["last_outcome"] or "untried",
                              r["kept"]))
    page_lines = []
    for p in _recent_pages(conn, geoid, category):
        page_lines.append("- %s  %s" % ((p["title"] or "").strip()[:120],
                                        p["url"]))
    item = conn.execute(
        "SELECT last_error FROM work_item WHERE geoid=? AND category=?",
        (geoid, category)).fetchone()
    reason = (item["last_error"] if item and item["last_error"] else
              "no usable findings yet")

    prompt = (
        "## Jurisdiction\n%s, %s (geoid %s)\n\n"
        "## Looking for\n%s\n\n"
        "## Why the last round failed\n%s\n\n"
        "## Queries already tried\n%s\n\n"
        "## Pages the crawler reached (titles are the site's own words)\n%s\n\n"
        "Propose new queries. Respond with JSON only."
        % (name, state, geoid, _goal(category), reason,
           "\n".join(tried_lines) or "_none_",
           "\n".join(page_lines) or "_none — searching never got that far_"))

    model = (settings.get("checker_model") or "").strip() or None
    raw, err = extract.chat(settings, prompt, system=REFLECT_SYSTEM,
                            model=model, provider=provider)
    if err or not raw:
        return 0
    try:
        doc = extract.parse_json_payload(raw)
    except extract.ExtractError:
        return 0
    queries = doc.get("queries") if isinstance(doc, dict) else None
    if not isinstance(queries, list):
        return 0

    known = set(r["query"].strip().lower() for r in tried)
    now = db.now()
    added = 0
    for q in queries:
        if not isinstance(q, str):
            continue
        q = " ".join(q.split())[:200]
        if len(q) < 8 or q.lower() in known:
            continue
        known.add(q.lower())
        conn.execute(
            "INSERT OR IGNORE INTO search_query (geoid, category, query, "
            "source, created_at) VALUES (?,?,?,'ai',?)",
            (geoid, category, q, now))
        added += 1
        if n_ai + added >= MAX_AI_QUERIES:
            break
    conn.commit()
    return added
