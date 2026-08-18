"""The work ledger: what to research, in what order, and what came back.

Priority is population-weighted on purpose. Nationwide coverage of ~22,000
jurisdictions is a long project, and the ordering determines whether the
first 500 rows cover 40% of the U.S. population or 0.4% of it.
"""

import math

from . import db
from .vocab import PASS_KINDS, WORK_CATEGORIES, WORK_STATUSES

KIND_WEIGHT = {"state": 1000, "county": 100, "place": 10, "mcd": 5, "school": 20}


def priority_for(kind, population):
    """Population-weighted priority, log-scaled so an 8M-person city does not
    swamp the queue by six orders of magnitude over a 400-person village."""
    pop = population or 0
    return int(KIND_WEIGHT.get(kind, 1) * (math.log10(pop + 10) ** 2))


def plan(conn, states=None, kinds=("county", "place"), categories=None,
         min_pop=0, batch=None, limit=None):
    """Create work items for the selected jurisdictions x categories."""
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
            status = "pending"
            if _has_bulk(conn, row["geoid"], cat):
                status = "needs_review"
            cur = conn.execute(
                "INSERT OR IGNORE INTO work_item (geoid, category, priority, batch, "
                "status, updated_at) VALUES (?,?,?,?,?,?)",
                (row["geoid"], cat, pri, batch, status, db.now()),
            )
            created += cur.rowcount
    park_bulk_covered(conn)
    conn.commit()
    return created


def claim(conn, limit=10, states=None, categories=None, batch=None, kinds=None,
          min_pop=None, max_attempts=None):
    """Take the next N pending items and mark them in progress.

    max_attempts keeps a jurisdiction whose rate page cannot be found from
    recycling forever at the head of the queue. Items that hit the ceiling are
    parked at 'blocked' so they are visible rather than silently dropped.
    """
    park_bulk_covered(conn)
    if max_attempts:
        block_exhausted(conn, max_attempts)
    sql = ("SELECT w.id, w.geoid, w.category, w.priority FROM work_item w "
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

    rows = conn.execute(sql, params).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE work_item SET status='in_progress', attempts=attempts+1, "
            "claimed_at=?, updated_at=? WHERE id=?", (db.now(), db.now(), r["id"]))
    conn.commit()
    return rows


def _has_bulk(conn, geoid, category):
    return conn.execute(
        "SELECT 1 FROM tax_instrument WHERE geoid=? AND category=? "
        "AND superseded_by IS NULL AND extraction_method='bulk_import'",
        (geoid, category)).fetchone() is not None


def park_bulk_covered(conn):
    """Pending items already filled by an adapter should not be crawled."""
    cur = conn.execute(
        "UPDATE work_item SET status='needs_review', updated_at=? "
        "WHERE status IN ('pending','in_progress') AND EXISTS ("
        "  SELECT 1 FROM tax_instrument t WHERE t.geoid=work_item.geoid "
        "  AND t.category=work_item.category AND t.superseded_by IS NULL "
        "  AND t.extraction_method='bulk_import')",
        (db.now(),))
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
    rows = conn.execute(
        "SELECT id FROM work_item WHERE status IN ('complete','no_data') "
        "AND completed_at IS NOT NULL "
        "AND completed_at < datetime('now', ?) ORDER BY priority DESC LIMIT ?",
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
    sql = ("SELECT j.geoid, j.kind, j.population FROM jurisdiction j "
           "WHERE j.kind IN (%s) AND NOT EXISTS ("
           "  SELECT 1 FROM work_item w WHERE w.geoid = j.geoid "
           "  AND w.category IN (%s))" % (",".join("?" * len(kinds)),
                                          ",".join("?" * len(categories))))
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
