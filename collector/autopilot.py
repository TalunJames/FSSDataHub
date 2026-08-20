"""What to do next, decided from the state of the database.

Continuous mode used to stop being useful the moment a hand-drawn slice ran
out: the loop would claim an empty queue, log "queue empty", and sleep
forever. This module is the missing half. It looks at what is in the database,
picks the next most valuable thing nobody has done, and hands it back as a
named action for the worker to run.

Order is deliberate and it is the whole efficiency argument:

  1. setup     seed the jurisdiction registry, then the source catalog
  2. bulk      free national files first (SST rates, Census collections)
  3. framework one pass per state fills thresholds and caps for every
               jurisdiction inside it, so 51 items unlock 22,000
  4. statutes  the corpus that makes framework packets worth reading
  5. expand    plan the next chunk of jurisdictions, most populous first
  6. refresh   re-research what has gone stale

Decisions only. Running the action belongs to the worker, which owns the job
lock; keeping this module free of that lets it be tested without threads.
"""

from taxdb import ledger
from taxdb.vocab import CATEGORIES, ELECTIONS, FRAMEWORK

from . import store

# Actions the worker knows how to run. Keep in sync with worker._ACTIONS.
SEED = "seed"
SOURCES = "sources"
SST = "sst"
COG = "cog"
STATUTES = "statutes"
PLAN_FRAMEWORK = "plan_framework"
EXPAND = "expand"
REFRESH = "refresh"

# Actions that hit the network hard and are pointless to retry immediately.
# SOURCES is here because next_action returns it whenever the source table is
# empty; if seeding it ever fails, an uncooled retry is a hot loop.
COOLDOWN_HOURS = {SEED: 6, SOURCES: 1, SST: 12, COG: 24, STATUTES: 12}


def enabled(settings):
    return store.as_bool(settings.get("autopilot_enabled"))


def scope_states(settings):
    """Autopilot honours the same state filter as the crawler."""
    raw = settings.get("filter_states") or ""
    return [x.strip().upper() for x in raw.replace(",", " ").split() if x.strip()]


def _count(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchone()[0]
    except Exception:
        return 0


def _tried_recently(conn, key, hours):
    """Cooldown marker, so a failing bulk download cannot spin the loop."""
    if not hours:
        return False
    row = conn.execute(
        "SELECT 1 FROM collector_setting WHERE key=? AND value > "
        "strftime('%Y-%m-%d %H:%M:%S', 'now', ?)",
        ("autopilot_tried:" + key, "-%d hours" % hours)).fetchone()
    return row is not None


def mark_tried(conn, key):
    from taxdb import db
    store.put(conn, "autopilot_tried:" + key, db.now())
    conn.commit()


def next_action(conn, settings):
    """The next action, as (action, kwargs, label). None when nothing is due.

    label is written for a reader who does not know the schema. It is what the
    home page shows while the action runs.
    """
    states = scope_states(settings)
    chunk = max(10, store.as_int(settings.get("autopilot_chunk"), 200))

    if _count(conn, "SELECT COUNT(*) FROM jurisdiction") == 0:
        if _tried_recently(conn, SEED, COOLDOWN_HOURS[SEED]):
            return None
        return SEED, {}, "Downloading the list of every US county and city"

    if _count(conn, "SELECT COUNT(*) FROM source") == 0 \
            and not _tried_recently(conn, SOURCES, COOLDOWN_HOURS[SOURCES]):
        return SOURCES, {}, "Recording the state revenue agency for each state"

    # Bulk files before any crawling: they are free, national, and they park
    # thousands of work items as already-answered.
    if store.as_bool(settings.get("autopilot_bulk")):
        n_bulk = _count(
            conn, "SELECT COUNT(*) FROM tax_instrument WHERE extraction_method="
                  "'bulk_import' AND superseded_by IS NULL")
        if n_bulk == 0 and not _tried_recently(conn, SST, COOLDOWN_HOURS[SST]):
            return SST, {"states": states or None}, \
                "Loading published sales tax rates for the 24 SST member states"

        if _count(conn, "SELECT COUNT(*) FROM revenue_base") == 0 \
                and not _tried_recently(conn, COG, COOLDOWN_HOURS[COG]):
            return COG, {}, "Loading Census of Governments collections"

    # One framework pass per state. 51 items that gate the headroom and
    # threshold layers for every jurisdiction underneath them.
    missing = ledger.states_missing_pass(conn, FRAMEWORK, states or None)
    if missing:
        return PLAN_FRAMEWORK, {"states": missing}, \
            "Queueing the statutory framework for %d state(s)" % len(missing)

    # Statute corpus for a state whose framework work is still open.
    if store.as_bool(settings.get("autopilot_statutes")):
        usps = _state_needing_statutes(conn, states, settings)
        if usps and not _tried_recently(conn, STATUTES + ":" + usps,
                                       COOLDOWN_HOURS[STATUTES]):
            return STATUTES, {"usps": usps}, \
                "Downloading %s statutes so the research packets cite real law" % usps

    rows = ledger.unplanned(conn, list(CATEGORIES), states or None,
                            kinds=_kinds(settings), limit=chunk)
    if rows:
        return EXPAND, {"geoids": [r["geoid"] for r in rows]}, \
            "Adding the next %d places to the work list" % len(rows)

    days = store.as_int(settings.get("refresh_days"), 365)
    # Same bulk-covered exclusion as ledger.requeue_stale, or this count never
    # reaches zero for items claim() would only park again — a livelock.
    if days > 0 and _count(
            conn,
            "SELECT COUNT(*) FROM work_item WHERE status IN ('complete','no_data') "
            "AND completed_at IS NOT NULL AND completed_at < datetime('now', ?) "
            "AND NOT EXISTS (SELECT 1 FROM tax_instrument t "
            "  WHERE t.geoid=work_item.geoid AND t.category=work_item.category "
            "  AND t.superseded_by IS NULL AND t.extraction_method='bulk_import')",
            ("-%d days" % days,)):
        return REFRESH, {"days": days}, \
            "Re-checking records last researched over %d days ago" % days

    return None


def _kinds(settings):
    raw = settings.get("filter_kinds") or "county,place"
    kinds = [x.strip() for x in raw.split(",") if x.strip()]
    return tuple(k for k in kinds if k in ("county", "place", "mcd")) or ("county", "place")


def _statutes_absent(settings):
    """States the current snapshot has no statute corpus for.

    Sorting by population means an unsatisfiable state stays at the head of
    this queue forever: Georgia is not in the v2026.08 statute corpus, and
    before this it was chosen every cooldown, failed on a 404, and no other
    state's statutes were ever downloaded.
    """
    from taxdb import statutes
    prefix = "statutes_absent:%s:" % statutes.SNAPSHOT
    return {key[len(prefix):] for key, value in (settings or {}).items()
            if key.startswith(prefix) and store.as_bool(value)}


def _state_needing_statutes(conn, states, settings=None):
    """A state with open framework work and no statute text on file."""
    sql = ("SELECT j.state_usps FROM work_item w "
           "JOIN jurisdiction j ON j.geoid = w.geoid "
           "WHERE w.category = ? AND w.status IN ('pending','in_progress') "
           "AND NOT EXISTS (SELECT 1 FROM statute_section s "
           "                WHERE s.state_usps = j.state_usps)")
    params = [FRAMEWORK]
    if states:
        sql += " AND j.state_usps IN (%s)" % ",".join("?" * len(states))
        params += states
    absent = _statutes_absent(settings)
    if absent:
        sql += " AND j.state_usps NOT IN (%s)" % ",".join("?" * len(absent))
        params += sorted(absent)
    sql += " ORDER BY COALESCE(j.population,0) DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return row["state_usps"] if row else None


def expand(conn, geoids, settings):
    """Plan every applicable pass for one chunk of jurisdictions.

    One plan() call for the whole chunk. The old per-geoid loop passed
    states=[the geoid's state] because plan() had no geoid filter, which
    planned the entire state once per geoid — the home page said "adding the
    next 200 places" while tens of thousands of items were being created.
    """
    cats = list(CATEGORIES)
    if store.as_bool(settings.get("autopilot_elections")):
        cats.append(ELECTIONS)
    return ledger.plan(conn, kinds=("county", "place", "mcd", "state"),
                       categories=cats, geoids=list(geoids),
                       batch="autopilot")


def progress(conn, settings):
    """Plain-language picture of where the whole project stands.

    Population coverage is the honest headline. Item counts move fast and mean
    little; the share of the country whose taxes are actually recorded is what
    the database is for.
    """
    total_pop = _count(conn, "SELECT SUM(COALESCE(population,0)) FROM jurisdiction "
                             "WHERE kind IN ('county','place')") or 0
    done_pop = _count(
        conn,
        "SELECT SUM(pop) FROM (SELECT DISTINCT j.geoid, COALESCE(j.population,0) AS pop "
        "FROM work_item w JOIN jurisdiction j ON j.geoid=w.geoid "
        "WHERE w.status='complete' AND j.kind IN ('county','place'))") or 0
    juris_total = _count(conn, "SELECT COUNT(*) FROM jurisdiction "
                               "WHERE kind IN ('county','place')")
    # Same kind filter as juris_total: counting completed state and mcd items
    # against a county/place denominator sent the header over 100% and the
    # "places left" figure negative.
    juris_done = _count(
        conn, "SELECT COUNT(*) FROM (SELECT DISTINCT w.geoid FROM work_item w "
              "JOIN jurisdiction j ON j.geoid=w.geoid "
              "WHERE w.status='complete' AND j.kind IN ('county','place'))")
    states_total = _count(conn, "SELECT COUNT(*) FROM jurisdiction WHERE kind='state'")
    states_done = _count(
        conn, "SELECT COUNT(*) FROM work_item w JOIN jurisdiction j "
              "ON j.geoid=w.geoid WHERE w.category=? AND w.status='complete'",
        (FRAMEWORK,))
    return {
        "pop_total": total_pop,
        "pop_done": done_pop,
        "pop_pct": round(100.0 * done_pop / total_pop, 1) if total_pop else 0.0,
        "juris_total": juris_total,
        "juris_done": juris_done,
        "states_total": states_total,
        "states_done": states_done,
    }
