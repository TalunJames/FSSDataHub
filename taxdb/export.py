"""Exports: flat CSVs plus a coverage report."""

import csv
import os

from . import db

TAX_SQL = """
SELECT j.state_usps, j.kind, j.geoid, j.name AS jurisdiction, j.population,
       t.category, t.instrument_code, t.label, t.status,
       t.rate_value, t.rate_unit, t.rate_basis,
       t.cap_type, t.cap_value, t.cap_unit, t.cap_note,
       t.voter_approval_required, t.effective_date, t.expiration_date, t.fiscal_year,
       t.statute_cite, s.name AS source_name, s.url AS source_url,
       s.authority_tier, t.confidence, t.extraction_method, t.researcher,
       t.retrieved_at, t.notes
FROM tax_instrument t
JOIN jurisdiction j ON j.geoid = t.geoid
JOIN source s       ON s.id    = t.source_id
WHERE t.superseded_by IS NULL
{where}
ORDER BY j.state_usps, j.kind, j.name, t.category, t.instrument_code
"""

COVERAGE_SQL = """
SELECT j.state_usps, j.kind, w.category,
       SUM(w.status='complete')     AS complete,
       SUM(w.status='needs_review') AS needs_review,
       SUM(w.status='in_progress')  AS in_progress,
       SUM(w.status='pending')      AS pending,
       SUM(w.status='no_data')      AS no_data,
       SUM(w.status='blocked')      AS blocked,
       COUNT(*)                     AS total,
       SUM(CASE WHEN w.status='complete' THEN COALESCE(j.population,0) ELSE 0 END) AS pop_complete,
       SUM(COALESCE(j.population,0)) AS pop_total
FROM work_item w JOIN jurisdiction j ON j.geoid = w.geoid
GROUP BY j.state_usps, j.kind, w.category
ORDER BY j.state_usps, j.kind, w.category
"""


def _dump(conn, sql, params, path):
    rows = conn.execute(sql, params).fetchall()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as fh:
        if not rows:
            fh.write("")
            return 0
        w = csv.writer(fh)
        w.writerow(rows[0].keys())
        for r in rows:
            w.writerow(list(r))
    return len(rows)


def export_all(conn, outdir=None, states=None):
    outdir = outdir or db.OUT_DIR
    where, params = "", []
    if states:
        where = " AND j.state_usps IN (%s)" % ",".join("?" * len(states))
        params = [s.upper() for s in states]

    written = {}
    written["taxes.csv"] = _dump(conn, TAX_SQL.format(where=where), params,
                                 os.path.join(outdir, "taxes.csv"))
    written["coverage.csv"] = _dump(conn, COVERAGE_SQL, [],
                                    os.path.join(outdir, "coverage.csv"))
    written["jurisdictions.csv"] = _dump(
        conn,
        "SELECT geoid, kind, name, state_usps, county_fips, population, "
        "population_year, land_sqmi, lat, lon FROM jurisdiction "
        "WHERE 1=1 %s ORDER BY state_usps, kind, name"
        % (" AND state_usps IN (%s)" % ",".join("?" * len(states)) if states else ""),
        [s.upper() for s in states] if states else [],
        os.path.join(outdir, "jurisdictions.csv"))
    written["sources.csv"] = _dump(
        conn,
        "SELECT id, scope_geoid, name, url, source_type, authority_tier, verified, "
        "http_status, last_checked FROM source ORDER BY authority_tier, name", [],
        os.path.join(outdir, "sources.csv"))
    written["state_profiles.csv"] = _dump(
        conn, "SELECT * FROM state_profile ORDER BY state_usps", [],
        os.path.join(outdir, "state_profiles.csv"))
    written["coverage_assertions.csv"] = _dump(
        conn, "SELECT * FROM coverage_assertion ORDER BY domain, scope_geoid", [],
        os.path.join(outdir, "coverage_assertions.csv"))
    written["thresholds.csv"] = _dump(
        conn,
        "SELECT * FROM threshold_rule %s ORDER BY state_usps, measure_class"
        % ("WHERE state_usps IN (%s)" % ",".join("?" * len(states)) if states else ""),
        [s.upper() for s in states] if states else [],
        os.path.join(outdir, "thresholds.csv"))
    written["authority_caps.csv"] = _dump(
        conn,
        "SELECT * FROM authority_grant %s ORDER BY state_usps, category, instrument_code"
        % ("WHERE state_usps IN (%s)" % ",".join("?" * len(states)) if states else ""),
        [s.upper() for s in states] if states else [],
        os.path.join(outdir, "authority_caps.csv"))
    written["headroom.csv"] = _dump(
        conn,
        "SELECT * FROM v_headroom %s ORDER BY population DESC"
        % ("WHERE state_usps IN (%s)" % ",".join("?" * len(states)) if states else ""),
        [s.upper() for s in states] if states else [],
        os.path.join(outdir, "headroom.csv"))
    written["ballot_measures.csv"] = _dump(
        conn,
        "SELECT b.*, j.name AS jurisdiction, j.state_usps FROM ballot_measure b "
        "JOIN jurisdiction j ON j.geoid = b.geoid WHERE b.superseded_by IS NULL "
        "%s ORDER BY b.election_date DESC"
        % (" AND j.state_usps IN (%s)" % ",".join("?" * len(states)) if states else ""),
        [s.upper() for s in states] if states else [],
        os.path.join(outdir, "ballot_measures.csv"))
    return outdir, written
