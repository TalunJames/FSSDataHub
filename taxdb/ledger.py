"""The work ledger: what to research, in what order, and what came back.

Priority is population-weighted on purpose. Nationwide coverage of ~22,000
jurisdictions is a long project, and the ordering determines whether the
first 500 rows cover 40% of the U.S. population or 0.4% of it.
"""

import math
import sqlite3

from . import db
from .vocab import PASS_KINDS, WORK_CATEGORIES, WORK_STATUSES

# An adapter-filled row is a published rate file with a period label, an
# archived copy of the bytes, and a tier-2 citation. That is better provenance
# than a crawled county web page, and there is nothing for a human to judge.
# It is filed, not queued: parking it at needs_review put tens of thousands of
# spreadsheet rows on the review page, which is where a real review queue goes
# to die. Refresh still reaches these on the normal refresh_days timer.
BULK_NOTE = "filled from a published bulk rate file; no human review needed"

KIND_WEIGHT = {"state": 1000, "county": 100, "place": 10, "mcd": 5, "school": 20}


def priority_for(kind, population):
    """Population-weighted priority, log-scaled so an 8M-person city does not
    swamp the queue by six orders of magnitude over a 400-person village."""
    pop = population or 0
    return int(KIND_WEIGHT.get(kind, 1) * (math.log10(pop + 10) ** 2))


def plan(conn, states=None, kinds=("county", "place"), categories=None,
         min_pop=0, batch=None, limit=None, geoids=None):
    """Create work items for the selected jurisdictions x categories.

    geoids narrows the plan to specific places. Without it, the autopilot's
    expand step had no way to plan its chunk except by re-planning whole
    states, once per geoid.
    """
    categories = list(categories or WORK_CATEGORIES.keys())
    for c in categories:
        if c not in WORK_CATEGORIES:
            raise SystemExit("unknown category %r" % c)
    # A pass only makes sense against the kind of jurisdiction that publishes
    # it: state framework at the state, elections at the county.
    fixed = {c: PASS_KINDS[c] for c in categories if c in PASS_KINDS}

    sql = ("SELECT geoid, kind, population FROM jurisdiction "
           "WHERE kind IN (%s)" % ",".join("?" * len(kinds)))
    params = list(kinds)
    if geoids:
        sql += " AND geoid IN (%s)" % ",".join("?" * len(geoids))
        params += list(geoids)
    if states:
        sql += " AND state_usps IN (%s)" % ",".join("?" * len(states))
        params += [s.upper() for s in states]
    if min_pop:
        sql += " AND COALESCE(population,0) >= ?"
        params.append(min_pop)
    sql += " ORDER BY COALESCE(population,0) DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)

    created = 0
    for row in conn.execute(sql, params).fetchall():
        pri = priority_for(row["kind"], row["population"])
        for cat in categories:
            if cat in fixed and row["kind"] not in fixed[cat]:
                continue
            # Inserted pending; the single park_bulk_covered sweep below files
            # the bulk-answered ones. A per-row _has_bulk lookup here cost one
            # SELECT per (jurisdiction x category) — ~110k queries on a full
            # national plan — to compute what the sweep already computes.
            cur = conn.execute(
                "INSERT OR IGNORE INTO work_item (geoid, category, priority, batch, "
                "status, updated_at) VALUES (?,?,?,?,?,?)",
                (row["geoid"], cat, pri, batch, "pending", db.now()),
            )
            created += cur.rowcount
    park_bulk_covered(conn)
    conn.commit()
    return created


# RETURNING lets the claim be one statement, which is what makes it safe for
# more than one worker. Added in SQLite 3.35 (2021); the container is 3.4x.
_ATOMIC_CLAIM = sqlite3.sqlite_version_info >= (3, 35, 0)


def claim(conn, limit=10, states=None, categories=None, batch=None, kinds=None,
          min_pop=None, max_attempts=None):
    """Take the next N pending items and mark them in progress.

    Atomic, because several workers claim from this queue at once. The select
    and the update are one statement, so SQLite's write lock decides the
    winner and two workers cannot walk away with the same jurisdiction. The
    old read-then-write form looked correct single-threaded and duplicated
    work the moment a second worker existed.

    max_attempts keeps a jurisdiction whose rate page cannot be found from
    recycling forever at the head of the queue. Items that hit the ceiling are
    parked at 'blocked' so they are visible rather than silently dropped.
    """
    park_bulk_covered(conn)
    if max_attempts:
        block_exhausted(conn, max_attempts)
    # Ids only: this is the subquery an atomic UPDATE ... WHERE id IN (...)
    # needs, and the RETURNING clause gives back the columns callers read.
    sql = ("SELECT w.id FROM work_item w "
           "JOIN jurisdiction j ON j.geoid = w.geoid WHERE w.status = 'pending' "
           "AND NOT EXISTS (SELECT 1 FROM tax_instrument t "
           "WHERE t.geoid=w.geoid AND t.category=w.category "
           "AND t.superseded_by IS NULL AND t.extraction_method='bulk_import')")
    params = []
    if states:
        sql += " AND j.state_usps IN (%s)" % ",".join("?" * len(states))
        params += [s.upper() for s in states]
    if kinds:
        sql += " AND j.kind IN (%s)" % ",".join("?" * len(kinds))
        params += list(kinds)
    if categories:
        sql += " AND w.category IN (%s)" % ",".join("?" * len(categories))
        params += list(categories)
    if batch:
        sql += " AND w.batch = ?"
        params.append(batch)
    if min_pop:
        sql += " AND COALESCE(j.population,0) >= ?"
        params.append(min_pop)
    if max_attempts:
        sql += " AND w.attempts < ?"
        params.append(max_attempts)
    sql += " ORDER BY w.priority DESC, w.geoid LIMIT ?"
    params.append(limit)

    now = db.now()
    if _ATOMIC_CLAIM:
        rows = conn.execute(
            "UPDATE work_item SET status='in_progress', attempts=attempts+1, "
            "claimed_at=?, updated_at=? WHERE id IN (%s) "
            "RETURNING id, geoid, category, priority" % sql,
            [now, now] + params).fetchall()
        conn.commit()
        return rows

    # Old SQLite: take the write lock up front instead, which costs a little
    # concurrency but keeps the same guarantee.
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        ids = [r["id"] for r in conn.execute(sql, params).fetchall()]
        for wid in ids:
            conn.execute(
                "UPDATE work_item SET status='in_progress', attempts=attempts+1, "
                "claimed_at=?, updated_at=? WHERE id=?", (now, now, wid))
        rows = conn.execute(
            "SELECT id, geoid, category, priority FROM work_item WHERE id IN (%s)"
            % ",".join("?" * len(ids)), ids).fetchall() if ids else []
        conn.execute("COMMIT")
    finally:
        conn.isolation_level = ""
    return rows


def park_bulk_covered(conn, pairs=None, commit=True):
    """File items an adapter already answered, so they are neither crawled
    nor put in front of a human. See BULK_NOTE.

    pairs, when given, restricts the sweep to those (geoid, category) tuples —
    per-document ingest passes what it touched instead of re-sweeping the whole
    ledger. commit=False leaves the transaction to the caller so ingest stays
    atomic end to end.
    """
    sql = ("UPDATE work_item SET status='complete', completed_at=COALESCE(completed_at,?), "
           "last_error=?, updated_at=? "
           "WHERE status IN ('pending','in_progress','needs_review') AND EXISTS ("
           "  SELECT 1 FROM tax_instrument t WHERE t.geoid=work_item.geoid "
           "  AND t.category=work_item.category AND t.superseded_by IS NULL "
           "  AND t.extraction_method='bulk_import')")
    params = [db.now(), BULK_NOTE, db.now()]
    pairs = list(pairs) if pairs is not None else None
    if pairs is not None:
        if not pairs:
            return 0
        sql += " AND (%s)" % " OR ".join(
            "(geoid=? AND category=?)" for _ in pairs)
        for geoid, cat in pairs:
            params += [geoid, cat]
    cur = conn.execute(sql, params)
    if commit:
        conn.commit()
    return cur.rowcount


def set_status(conn, geoid, category, status, error=None):
    if status not in WORK_STATUSES:
        raise SystemExit("unknown work status %r" % status)
    completed = db.now() if status in ("complete", "no_data") else None
    conn.execute(
        "UPDATE work_item SET status=?, last_error=?, completed_at=?, updated_at=? "
        "WHERE geoid=? AND category=?",
        (status, error, completed, db.now(), geoid, category))


def release_stale(conn, hours=24):
    """Return long-running in_progress items to the queue."""
    cur = conn.execute(
        "UPDATE work_item SET status='pending', updated_at=? "
        "WHERE status='in_progress' AND claimed_at < datetime('now', ?)",
        (db.now(), "-%d hours" % hours))
    conn.commit()
    return cur.rowcount


def status_report(conn, states=None):
    """Coverage by state and status, weighted by population."""
    sql = ("SELECT j.state_usps AS st, w.status AS status, COUNT(*) AS n, "
           "SUM(COALESCE(j.population,0)) AS pop FROM work_item w "
           "JOIN jurisdiction j ON j.geoid = w.geoid")
    params = []
    if states:
        sql += " WHERE j.state_usps IN (%s)" % ",".join("?" * len(states))
        params += [s.upper() for s in states]
    sql += " GROUP BY j.state_usps, w.status ORDER BY j.state_usps"
    return conn.execute(sql, params).fetchall()


def block_exhausted(conn, max_attempts):
    """Park pending items that have burned through their attempts."""
    cur = conn.execute(
        "UPDATE work_item SET status='blocked', updated_at=?, "
        "last_error=COALESCE(last_error,'') || ' | gave up after ' || attempts || "
        "' attempts; needs a source by hand' "
        "WHERE status='pending' AND attempts >= ?",
        (db.now(), max_attempts))
    conn.commit()
    return cur.rowcount


def requeue_stale(conn, days=365, limit=500):
    """Send long-finished items back to pending so the data refreshes itself.

    Rates change, sunsets arrive, and a row researched two years ago is a
    liability rather than an asset. Attempts reset: this is a fresh look, not
    a retry of a failure.
    """
    # Bulk-covered items stay out: their data refreshes when the adapter
    # re-runs, not by crawling, and requeueing them only for claim() to park
    # them again left continuous mode spinning REFRESH -> park -> REFRESH
    # forever once the queue was otherwise drained.
    rows = conn.execute(
        "SELECT id FROM work_item WHERE status IN ('complete','no_data') "
        "AND completed_at IS NOT NULL "
        "AND completed_at < datetime('now', ?) "
        "AND NOT EXISTS (SELECT 1 FROM tax_instrument t "
        "  WHERE t.geoid=work_item.geoid AND t.category=work_item.category "
        "  AND t.superseded_by IS NULL AND t.extraction_method='bulk_import') "
        "ORDER BY priority DESC LIMIT ?",
        ("-%d days" % int(days), limit)).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE work_item SET status='pending', attempts=0, claimed_at=NULL, "
            "last_error='refresh: last researched more than %d days ago', "
            "updated_at=? WHERE id=?" % int(days), (db.now(), r["id"]))
    conn.commit()
    return len(rows)


def unplanned(conn, categories, states=None, kinds=("county", "place"), limit=250):
    """Jurisdictions with no work item yet, most populous first.

    This is what lets the collector keep going without anyone drawing a slice
    by hand: it always knows the next most valuable thing nobody has planned.
    """
    # "Missing at least one of the categories", not "missing all of them":
    # a place someone planned for property alone must still be expanded to
    # the other passes, or it never gets them. plan() is INSERT OR IGNORE,
    # so re-planning the categories it already has is free.
    sql = ("SELECT j.geoid, j.kind, j.population FROM jurisdiction j "
           "WHERE j.kind IN (%s) AND ("
           "  SELECT COUNT(DISTINCT w.category) FROM work_item w "
           "  WHERE w.geoid = j.geoid AND w.category IN (%s)) < %d"
           % (",".join("?" * len(kinds)), ",".join("?" * len(categories)),
              len(categories)))
    params = list(kinds) + list(categories)
    if states:
        sql += " AND j.state_usps IN (%s)" % ",".join("?" * len(states))
        params += [s.upper() for s in states]
    sql += " ORDER BY COALESCE(j.population,0) DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def states_missing_pass(conn, pass_name, states=None):
    """States with no work item for a pass yet, most populous first."""
    kinds = PASS_KINDS.get(pass_name, ("state",))
    sql = ("SELECT DISTINCT j.state_usps FROM jurisdiction j "
           "WHERE j.kind IN (%s) AND NOT EXISTS ("
           "  SELECT 1 FROM work_item w WHERE w.geoid = j.geoid "
           "  AND w.category = ?)" % ",".join("?" * len(kinds)))
    params = list(kinds) + [pass_name]
    if states:
        sql += " AND j.state_usps IN (%s)" % ",".join("?" * len(states))
        params += [s.upper() for s in states]
    sql += " ORDER BY j.state_usps"
    return [r["state_usps"] for r in conn.execute(sql, params).fetchall()]
