"""Validate and load research findings.

Ingest is strict on purpose. A finding that fails validation is rejected
with a reason rather than stored with a shrug -- silently accepting a
malformed rate is how a 22,000-row dataset becomes untrustworthy.
"""

import json

from . import db, ledger
from .vocab import validate_finding, SOURCE_TYPES


def load(conn, path, dry_run=False, allow_partial=False):
    with open(path) as fh:
        doc = json.load(fh)
    return load_doc(conn, doc, dry_run=dry_run, allow_partial=allow_partial,
                    label=path)


def load_doc(conn, doc, dry_run=False, allow_partial=False, label="<doc>"):
    findings = doc.get("findings")
    if findings is None:
        raise SystemExit("%s: no 'findings' array" % label)

    researcher = doc.get("researcher") or "unknown"
    retrieved_at = doc.get("retrieved_at") or db.now()
    method = doc.get("extraction_method") or "agent_research"

    errors, ok_rows = [], []
    for i, f in enumerate(findings):
        errs = validate_finding(f, i)
        geoid = f.get("geoid")
        if geoid and not conn.execute(
                "SELECT 1 FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone():
            errs.append("finding[%d]: geoid %r is not a seeded jurisdiction" % (i, geoid))
        st = (f.get("source") or {}).get("source_type")
        if st and st not in SOURCE_TYPES:
            errs.append("finding[%d]: source_type %r not in %s" % (i, st, sorted(SOURCE_TYPES)))
        if errs:
            errors.extend(errs)
        else:
            ok_rows.append(f)

    if errors and not allow_partial:
        msg = "\n".join("  " + e for e in errors)
        raise SystemExit("%d finding(s) rejected, nothing written:\n%s\n\n"
                         "Fix the file, or re-run with --allow-partial to load "
                         "the %d valid rows." % (len(errors), msg, len(ok_rows)))

    if dry_run:
        return {"valid": len(ok_rows), "rejected": len(errors), "written": 0,
                "errors": errors}

    written, touched = 0, set()
    for f in ok_rows:
        src = f["source"]
        source_id = db.get_or_create_source(
            conn, src["url"], src.get("name") or src["url"],
            source_type=src.get("source_type") or "portal",
            authority_tier=src.get("authority_tier") or 4,
            scope_geoid=f["geoid"],
        )

        # Supersede rather than overwrite. Park the prior live row on a
        # self-reference first so the partial unique index (live rows only)
        # lets the insert through; then point it at the new id.
        old = conn.execute(
            "SELECT id FROM tax_instrument WHERE geoid=? AND category=? "
            "AND instrument_code=? AND superseded_by IS NULL",
            (f["geoid"], f["category"], f["instrument_code"])).fetchone()
        if old:
            conn.execute(
                "UPDATE tax_instrument SET superseded_by=id WHERE id=?", (old["id"],))

        cur = conn.execute(
            "INSERT INTO tax_instrument (geoid, category, instrument_code, label, "
            "status, rate_value, rate_unit, rate_basis, cap_type, cap_value, cap_unit, "
            "cap_note, voter_approval_required, effective_date, expiration_date, "
            "fiscal_year, statute_cite, source_id, archive_file_id, confidence, "
            "extraction_method, researcher, retrieved_at, notes, source_quote) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["geoid"], f["category"], f["instrument_code"], f.get("label"),
             f["status"], f.get("rate_value"), f.get("rate_unit"), f.get("rate_basis"),
             f.get("cap_type"), f.get("cap_value"), f.get("cap_unit"), f.get("cap_note"),
             f.get("voter_approval_required"), f.get("effective_date"),
             f.get("expiration_date"), f.get("fiscal_year"), f.get("statute_cite"),
             source_id, f.get("archive_file_id"), f["confidence"],
             f.get("extraction_method") or method,
             f.get("researcher") or researcher, f.get("retrieved_at") or retrieved_at,
             f.get("notes"), f.get("source_quote")))
        if old:
            conn.execute("UPDATE tax_instrument SET superseded_by=? WHERE id=?",
                         (cur.lastrowid, old["id"]))
        written += 1
        touched.add((f["geoid"], f["category"]))

    for geoid, cat in touched:
        j = conn.execute("SELECT kind, population FROM jurisdiction WHERE geoid=?",
                         (geoid,)).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO work_item (geoid, category, priority, updated_at) "
            "VALUES (?,?,?,?)",
            (geoid, cat, ledger.priority_for(j["kind"], j["population"]), db.now()))
        ledger.set_status(conn, geoid, cat, "needs_review")
    ledger.park_bulk_covered(conn)
    conn.commit()

    return {"valid": len(ok_rows), "rejected": len(errors), "written": written,
            "jurisdictions": len({g for g, _ in touched}), "errors": errors}


def verify(conn):
    """Integrity checks that catch the failure modes this dataset is prone to."""
    checks = []

    def add(label, sql, params=()):
        rows = conn.execute(sql, params).fetchall()
        checks.append((label, len(rows), rows[:8]))

    add("current rows with no source URL",
        "SELECT t.id, t.geoid FROM tax_instrument t LEFT JOIN source s ON s.id=t.source_id "
        "WHERE t.superseded_by IS NULL AND (s.url IS NULL OR s.url='')")

    add("levied with no rate",
        "SELECT geoid, category, instrument_code FROM tax_instrument "
        "WHERE superseded_by IS NULL AND status='levied' AND rate_value IS NULL")

    add("rate with no unit",
        "SELECT geoid, instrument_code, rate_value FROM tax_instrument "
        "WHERE superseded_by IS NULL AND rate_value IS NOT NULL AND rate_unit IS NULL")

    add("percent rate outside 0-25 (likely mills/percent mixup)",
        "SELECT geoid, instrument_code, rate_value FROM tax_instrument "
        "WHERE superseded_by IS NULL AND rate_unit='percent' "
        "AND (rate_value < 0 OR rate_value > 25)")

    add("rate above its own stated cap",
        "SELECT geoid, instrument_code, rate_value, cap_value FROM tax_instrument "
        "WHERE superseded_by IS NULL AND cap_value IS NOT NULL "
        "AND rate_value IS NOT NULL AND rate_unit IS cap_unit AND rate_value > cap_value")

    add("sourced only to tier-4 aggregators",
        "SELECT t.geoid, t.instrument_code FROM tax_instrument t JOIN source s "
        "ON s.id=t.source_id WHERE t.superseded_by IS NULL AND s.authority_tier=4")

    add("marked complete but holding no findings",
        "SELECT w.geoid, w.category FROM work_item w WHERE w.status='complete' "
        "AND NOT EXISTS (SELECT 1 FROM tax_instrument t WHERE t.geoid=w.geoid "
        "AND t.category=w.category AND t.superseded_by IS NULL)")

    add("stale: retrieved more than 400 days ago",
        "SELECT geoid, instrument_code, retrieved_at FROM tax_instrument "
        "WHERE superseded_by IS NULL AND retrieved_at < date('now','-400 day')")

    add("non-ISO expiration_date (julianday NULL)",
        "SELECT geoid, instrument_code, expiration_date FROM tax_instrument "
        "WHERE superseded_by IS NULL AND expiration_date IS NOT NULL "
        "AND julianday(expiration_date) IS NULL")

    add("passed measure with negative margin vs threshold",
        "SELECT id, geoid, pct_yes, threshold_required, margin_vs_threshold "
        "FROM ballot_measure WHERE outcome='passed' AND margin_vs_threshold < 0")

    add("pct_yes inconsistent with vote counts",
        "SELECT id, geoid, votes_yes, votes_total, pct_yes FROM ballot_measure "
        "WHERE votes_yes IS NOT NULL AND votes_total > 0 AND pct_yes IS NOT NULL "
        "AND ABS(pct_yes - (100.0 * votes_yes / votes_total)) > 0.15")

    add("rate_change_event.measure_id points at missing ballot_measure",
        "SELECT e.id, e.geoid, e.measure_id FROM rate_change_event e "
        "WHERE e.measure_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM ballot_measure b WHERE b.id = e.measure_id)")

    add("client-facing row with only a tier-4 primary source and no corroboration",
        "SELECT t.id, t.geoid FROM tax_instrument t "
        "JOIN source s ON s.id = t.source_id "
        "WHERE t.superseded_by IS NULL AND s.authority_tier = 4 "
        "AND NOT EXISTS (SELECT 1 FROM claim_source cs "
        "                WHERE cs.claim_table='tax_instrument' AND cs.claim_id=t.id "
        "                  AND cs.role='corroborating' AND cs.agrees=1)")

    add("agent_research row with no source_quote",
        "SELECT id, geoid FROM tax_instrument WHERE superseded_by IS NULL "
        "AND extraction_method='agent_research' "
        "AND (source_quote IS NULL OR trim(source_quote)='')")

    missing_quote = []
    blob_cache = {}
    qrows = conn.execute(
        "SELECT t.id, t.geoid, t.source_quote, a.store_path FROM tax_instrument t "
        "JOIN archive_file a ON a.id = t.archive_file_id "
        "WHERE t.superseded_by IS NULL AND t.source_quote IS NOT NULL "
        "AND trim(t.source_quote) != ''"
    ).fetchall()
    for r in qrows:
        path = r["store_path"]
        if path not in blob_cache:
            blob_cache[path] = _archive_text(path)
        if r["source_quote"] not in blob_cache[path]:
            missing_quote.append((r["id"], r["geoid"]))
    checks.append(("source_quote not found in archived bytes",
                   len(missing_quote), missing_quote[:8]))

    return checks


def _archive_text(path, limit=2_000_000):
    try:
        with open(path, "rb") as fh:
            blob = fh.read(limit)
    except OSError:
        return ""
    if blob[:2] == b"PK":
        import io, zipfile
        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
            names = [n for n in z.namelist() if not n.endswith("/")]
            if names:
                blob = z.read(names[0])[:limit]
        except Exception:
            return ""
    return blob.decode("latin-1", errors="replace")
