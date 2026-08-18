"""Coverage assertions: the table that makes a partial dataset honest.

`taxdb brief` must refuse to print election history without a matching assertion.
Seed every state with completeness='none' *before* loading measures, so there
is never a window where silence is read as 'they never tried'.
"""

from . import db
from .fips import FIPS_TO_USPS

BALLOT_NONE_BASIS = (
    "No statewide local-measure file asserted yet. Absence of ballot_measure "
    "rows is a coverage gap, not evidence that this jurisdiction never tried."
)


def seed_empty_states(conn, domain="ballot_measure", asserted_by="taxdb coverage seed"):
    """One completeness='none' row per state (and DC, PR) for the given domain."""
    n = 0
    for fips, usps in sorted(FIPS_TO_USPS.items()):
        cur = conn.execute(
            "INSERT OR IGNORE INTO coverage_assertion ("
            "domain, scope_type, scope_geoid, jurisdiction_kind, "
            "period_start, period_end, completeness, basis, known_exclusions, "
            "asserted_by, asserted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (domain, "state", fips, None,
             "1990-01-01", "2099-12-31", "none", BALLOT_NONE_BASIS,
             "All local measures until a statewide or county source is loaded.",
             asserted_by, db.now()),
        )
        n += cur.rowcount
    conn.commit()
    return n


def for_jurisdiction(conn, geoid, domain="ballot_measure"):
    """Most-specific matching assertion: place, then county, then state."""
    j = conn.execute(
        "SELECT geoid, kind, state_fips, county_fips FROM jurisdiction WHERE geoid=?",
        (geoid,),
    ).fetchone()
    if not j:
        return None
    candidates = [geoid]
    if j["county_fips"] and j["county_fips"] != geoid:
        candidates.append(j["county_fips"])
    if j["state_fips"] and j["state_fips"] != geoid:
        candidates.append(j["state_fips"])
    for scope in candidates:
        row = conn.execute(
            "SELECT * FROM coverage_assertion WHERE domain=? AND scope_geoid=? "
            "ORDER BY CASE completeness "
            "  WHEN 'complete' THEN 1 WHEN 'substantial' THEN 2 "
            "  WHEN 'partial' THEN 3 WHEN 'spot_checked' THEN 4 "
            "  ELSE 5 END, asserted_at DESC LIMIT 1",
            (domain, scope),
        ).fetchone()
        if row:
            return row
    return None


def list_assertions(conn, domain=None, completeness=None):
    sql = ("SELECT c.id, c.domain, c.scope_type, c.scope_geoid, c.completeness, "
           "c.basis, j.name, j.state_usps FROM coverage_assertion c "
           "LEFT JOIN jurisdiction j ON j.geoid = c.scope_geoid")
    clauses, params = [], []
    if domain:
        clauses.append("c.domain=?")
        params.append(domain)
    if completeness:
        clauses.append("c.completeness=?")
        params.append(completeness)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY c.scope_geoid, c.domain"
    return conn.execute(sql, params).fetchall()
