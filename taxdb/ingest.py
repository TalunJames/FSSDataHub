"""Validate and load research findings.

Ingest is strict on purpose. A finding that fails validation is rejected
with a reason rather than stored with a shrug -- silently accepting a
malformed rate is how a 22,000-row dataset becomes untrustworthy.

One document can carry four kinds of claim, because a research pass rarely
finds exactly one kind:

    findings    -> tax_instrument   (what is levied, at what rate)
    measures    -> ballot_measure   (what voters were asked, and the result)
    thresholds  -> threshold_rule   (what it takes to pass)
    grants      -> authority_grant  (what the state permits, up to what cap)
    profile     -> state_profile    (one state's statutory frame)

Anything written also gets a claim_source row with role='primary', so the
two-source rule has something to build on instead of an empty table.
"""

import json
import re
from urllib.parse import urlparse

from . import coverage, db, ledger
from .vocab import (
    ELECTIONS, FRAMEWORK, SOURCE_TYPES, validate_finding, validate_grant,
    validate_measure, validate_profile, validate_threshold,
)

# doc key -> (table, validator, label used in error messages)
SECTIONS = (
    ("findings", "tax_instrument", validate_finding),
    ("measures", "ballot_measure", validate_measure),
    ("thresholds", "threshold_rule", validate_threshold),
    ("grants", "authority_grant", validate_grant),
)


def load(conn, path, dry_run=False, allow_partial=False):
    with open(path) as fh:
        doc = json.load(fh)
    return load_doc(conn, doc, dry_run=dry_run, allow_partial=allow_partial,
                    label=path)


def stamp_doc(doc, geoid=None, category=None, state_usps=None, researcher=None,
              method="agent_research", source_url=None):
    """Fill the fields a model should not have to repeat on every row.

    A model that restates the geoid on all forty rows gets one of them wrong
    eventually, and a threshold filed against the wrong state is worse than a
    missing one. category is only stamped onto tax findings: 'elections' and
    'framework' are work-queue passes, not tax categories, and stamping one
    into a finding rejects the whole row.
    """
    from .vocab import CATEGORIES

    def base(row):
        if researcher:
            row.setdefault("researcher", researcher)
        row.setdefault("extraction_method", method)
        if source_url:
            src = row.get("source") or {}
            src.setdefault("url", source_url)
            row["source"] = src

    for f in doc.get("findings") or []:
        if geoid:
            f.setdefault("geoid", geoid)
        if category in CATEGORIES:
            f.setdefault("category", category)
        base(f)
    for m in doc.get("measures") or []:
        if geoid:
            m.setdefault("geoid", geoid)
        base(m)
    for t in doc.get("thresholds") or []:
        if state_usps:
            t.setdefault("state_usps", state_usps)
        base(t)
    for g in doc.get("grants") or []:
        if state_usps:
            g.setdefault("state_usps", state_usps)
        base(g)
    prof = doc.get("profile")
    if isinstance(prof, dict) and state_usps:
        prof.setdefault("state_usps", state_usps)
    return doc


def _rows_of(doc, key):
    rows = doc.get(key)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise SystemExit("%r must be an array" % key)
    return rows


def load_doc(conn, doc, dry_run=False, allow_partial=False, label="<doc>"):
    """Validate every section, then write the ones that pass.

    Returns counts. `written` is the total across all sections; `by_type`
    breaks it down. Errors name their section and index, so a rejection is
    traceable back to one row of one array.
    """
    present = [key for key, _, _ in SECTIONS if doc.get(key) is not None]
    if not present and doc.get("profile") is None:
        raise SystemExit("%s: no 'findings' array" % label)

    researcher = doc.get("researcher") or "unknown"
    retrieved_at = doc.get("retrieved_at") or db.now()
    method = doc.get("extraction_method") or "agent_research"

    errors = []
    ok = {}
    for key, table, validator in SECTIONS:
        ok[key] = []
        for i, row in enumerate(_rows_of(doc, key)):
            if key == "measures":
                # Derive first: the checks on vote arithmetic and on
                # "passed below its own threshold" are only meaningful once
                # the implied total and the applicable threshold are filled in.
                row = derive_measure(conn, dict(row))
            errs = list(validator(row, i))
            errs.extend(_context_errors(conn, key, row, i))
            if errs:
                errors.extend(errs)
            else:
                ok[key].append(row)

    profile = doc.get("profile")
    profiles = []
    if profile is not None:
        for i, row in enumerate(profile if isinstance(profile, list) else [profile]):
            errs = validate_profile(row, i)
            if errs:
                errors.extend(errs)
            else:
                profiles.append(row)

    n_valid = sum(len(v) for v in ok.values()) + len(profiles)

    if errors and not allow_partial:
        msg = "\n".join("  " + e for e in errors)
        raise SystemExit("%d row(s) rejected, nothing written:\n%s\n\n"
                         "Fix the file, or re-run with --allow-partial to load "
                         "the %d valid rows." % (len(errors), msg, n_valid))

    if dry_run:
        return {"valid": n_valid, "rejected": len(errors), "written": 0,
                "by_type": {k: 0 for k, _, _ in SECTIONS}, "errors": errors}

    ctx = _Ctx(conn, researcher, retrieved_at, method)
    # Order matters: thresholds land before measures so a document that
    # supplies both can denormalize its own threshold onto its own measures.
    by_type = {}
    by_type["profile"] = _write_profiles(ctx, profiles)
    by_type["thresholds"] = _write_thresholds(ctx, ok["thresholds"])
    by_type["grants"] = _write_grants(ctx, ok["grants"])
    by_type["measures"] = _write_measures(ctx, ok["measures"])
    by_type["findings"] = _write_findings(ctx, ok["findings"])

    for geoid, cat in ctx.touched:
        _touch_work_item(conn, geoid, cat)
    ledger.park_bulk_covered(conn)
    conn.commit()

    return {
        "valid": n_valid,
        "rejected": len(errors),
        "written": sum(by_type.values()),
        "by_type": by_type,
        "jurisdictions": len({g for g, _ in ctx.touched}),
        "errors": errors,
    }


def _context_errors(conn, key, row, index):
    """Checks that need the database: real geoid, known source_type."""
    errs = []
    prefix = {"findings": "finding", "measures": "measure",
              "thresholds": "threshold", "grants": "grant"}[key]
    geoid = row.get("geoid")
    if geoid and not conn.execute(
            "SELECT 1 FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone():
        errs.append("%s[%d]: geoid %r is not a seeded jurisdiction"
                    % (prefix, index, geoid))
    st = (row.get("source") or {}).get("source_type")
    if st and st not in SOURCE_TYPES:
        errs.append("%s[%d]: source_type %r not in %s"
                    % (prefix, index, st, sorted(SOURCE_TYPES)))
    return errs


# Statute and code hosts. A rule read off the code itself is primary law.
_CODE_HOST = re.compile(
    r"legis|legislature|statute|codes?\.|municode|amlegal|generalcode|"
    r"ecode360|library\.municode|law\.justia|casetext|lawserver", re.I)
_CODE_PATH = re.compile(
    r"/statute|/code|/ordinance|/charter|/constitution|/title\d|/chapter", re.I)


def infer_tier(url, source_type=None):
    """Authority tier from the URL when the researcher did not set one.

    Defaulting every crawled page to tier 4 made 'weak source' the normal
    state of the database and the signal meaningless. The crawler only keeps
    government hosts in the first place, so a .gov page is the agency of
    record until something says otherwise.
    """
    if source_type in ("statute", "ordinance"):
        return 1
    if source_type == "secondary":
        return 3
    u = (url or "").lower()
    try:
        host = urlparse(u).hostname or ""
    except ValueError:
        host = ""
    if _CODE_HOST.search(host) or _CODE_PATH.search(u):
        return 1
    if host.endswith(".gov") or host.endswith(".us") or ".gov." in host:
        return 2
    if host.endswith(".org") or host.endswith(".edu"):
        return 3
    return 4


class _Ctx:
    def __init__(self, conn, researcher, retrieved_at, method):
        self.conn = conn
        self.researcher = researcher
        self.retrieved_at = retrieved_at
        self.method = method
        self.touched = set()
        self.measured = set()

    def source_id(self, row, scope_geoid=None):
        src = row["source"]
        return db.get_or_create_source(
            self.conn, src["url"], src.get("name") or src["url"],
            source_type=src.get("source_type") or "portal",
            authority_tier=src.get("authority_tier") or infer_tier(
                src["url"], src.get("source_type")),
            scope_geoid=scope_geoid,
        )

    def cite(self, table, claim_id, row, primary_source_id):
        """Record provenance in claim_source: the primary plus any corroboration."""
        _claim_source(self.conn, table, claim_id, primary_source_id, "primary",
                      row, self.retrieved_at)
        for extra in row.get("corroborating_sources") or []:
            if not (extra or {}).get("url"):
                continue
            sid = db.get_or_create_source(
                self.conn, extra["url"], extra.get("name") or extra["url"],
                source_type=extra.get("source_type") or "secondary",
                authority_tier=extra.get("authority_tier") or 4)
            _claim_source(self.conn, table, claim_id, sid,
                          extra.get("role") or "corroborating", extra,
                          self.retrieved_at)


def _claim_source(conn, table, claim_id, source_id, role, row, retrieved_at):
    conn.execute(
        "INSERT OR IGNORE INTO claim_source (claim_table, claim_id, source_id, "
        "archive_file_id, role, agrees, observed_value, retrieved_at, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (table, claim_id, source_id, row.get("archive_file_id"), role,
         1 if role in ("primary", "corroborating") else 0,
         row.get("source_quote") or row.get("observed_value"),
         row.get("retrieved_at") or retrieved_at, row.get("source_note")))


def _touch_work_item(conn, geoid, category):
    j = conn.execute("SELECT kind, population FROM jurisdiction WHERE geoid=?",
                     (geoid,)).fetchone()
    if not j:
        return
    conn.execute(
        "INSERT OR IGNORE INTO work_item (geoid, category, priority, updated_at) "
        "VALUES (?,?,?,?)",
        (geoid, category, ledger.priority_for(j["kind"], j["population"]), db.now()))
    # A row an adapter filled is filed, not queued — see ledger.BULK_NOTE.
    if conn.execute(
            "SELECT 1 FROM tax_instrument WHERE geoid=? AND category=? "
            "AND superseded_by IS NULL AND extraction_method='bulk_import'",
            (geoid, category)).fetchone():
        ledger.set_status(conn, geoid, category, "complete", error=ledger.BULK_NOTE)
    else:
        ledger.set_status(conn, geoid, category, "needs_review")


# ---------------------------------------------------------------- writers

def _write_findings(ctx, rows):
    conn = ctx.conn
    written = 0
    for f in rows:
        source_id = ctx.source_id(f, scope_geoid=f["geoid"])

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
             f.get("extraction_method") or ctx.method,
             f.get("researcher") or ctx.researcher,
             f.get("retrieved_at") or ctx.retrieved_at,
             f.get("notes"), f.get("source_quote")))
        if old:
            conn.execute("UPDATE tax_instrument SET superseded_by=? WHERE id=?",
                         (cur.lastrowid, old["id"]))
        ctx.cite("tax_instrument", cur.lastrowid, f, source_id)
        written += 1
        ctx.touched.add((f["geoid"], f["category"]))
    return written


MEASURE_COLS = (
    "election_type", "measure_id_local", "official_title", "ballot_question",
    "full_text_url", "measure_class", "category", "instrument_code",
    "is_renewal", "rate_value", "rate_unit", "rate_increment",
    "principal_amount", "duration_years", "sunset_date", "purpose_type",
    "stated_purpose", "annual_revenue_est", "oversight_provisions",
    "threshold_required", "threshold_basis", "threshold_rule_id",
    "votes_yes", "votes_no", "votes_total", "pct_yes", "margin_vs_threshold",
    "outcome", "registered_voters", "ballots_cast", "turnout_pct",
    "concurrent_measures", "confidence", "extraction_method", "researcher",
    "retrieved_at", "notes",
)


def _write_measures(ctx, rows):
    """ballot_measure has a hard UNIQUE(geoid, election_date, measure_id_local),
    so a re-run updates the existing row in place rather than superseding it.
    Superseding would violate the constraint, and downstream FKs
    (rate_change_event.measure_id, measure_attempt_chain) point at the id."""
    conn = ctx.conn
    written = 0
    for m in rows:
        m = derive_measure(conn, dict(m))
        source_id = ctx.source_id(m, scope_geoid=m["geoid"])
        values = {c: m.get(c) for c in MEASURE_COLS}
        values["extraction_method"] = m.get("extraction_method") or ctx.method
        values["researcher"] = m.get("researcher") or ctx.researcher
        values["retrieved_at"] = m.get("retrieved_at") or ctx.retrieved_at

        old = conn.execute(
            "SELECT id FROM ballot_measure WHERE geoid=? AND election_date=? "
            "AND ifnull(measure_id_local,'')=ifnull(?,'')",
            (m["geoid"], m["election_date"], m.get("measure_id_local"))).fetchone()
        if old:
            # Fill blanks and accept corrections, but never blank out a column
            # that already holds a value with an explicit null.
            sets = [c for c in MEASURE_COLS if values.get(c) is not None]
            conn.execute(
                "UPDATE ballot_measure SET %s, source_id=?, archive_file_id=? WHERE id=?"
                % ", ".join("%s=?" % c for c in sets),
                [values[c] for c in sets]
                + [source_id, m.get("archive_file_id"), old["id"]])
            claim_id = old["id"]
        else:
            cols = ["geoid", "election_date"] + list(MEASURE_COLS) + [
                "source_id", "archive_file_id"]
            params = [m["geoid"], m["election_date"]] + [
                values[c] for c in MEASURE_COLS] + [source_id, m.get("archive_file_id")]
            cur = conn.execute(
                "INSERT INTO ballot_measure (%s) VALUES (%s)"
                % (", ".join(cols), ",".join("?" * len(cols))), params)
            claim_id = cur.lastrowid
        ctx.cite("ballot_measure", claim_id, m, source_id)
        written += 1
        ctx.touched.add((m["geoid"], ELECTIONS))
        ctx.measured.add(m["geoid"])

    # Holding measures for a place without saying how complete they are is the
    # failure coverage_assertion exists to prevent. Claim 'partial' only when
    # nobody has claimed anything better.
    for geoid in ctx.measured:
        coverage.assert_scope(
            conn, "ballot_measure", geoid,
            scope_type=coverage.scope_type_for(conn, geoid),
            completeness="partial", only_if_absent=True,
            measures_found=len([m for m in rows if m.get("geoid") == geoid]),
            basis="Loaded from research. Coverage is partial until a full "
                  "canvass series is confirmed for this scope.",
            asserted_by=ctx.researcher)
    return written


def derive_measure(conn, m):
    """Fill the arithmetic and the threshold that applied on election day.

    Denormalizing the threshold onto the measure is deliberate: rules change
    (CA Upland 2017), and a margin recomputed against today's matrix is wrong
    for anything older than the last rule change.
    """
    yes, no = m.get("votes_yes"), m.get("votes_no")
    if m.get("votes_total") is None and _isnum(yes) and _isnum(no):
        m["votes_total"] = yes + no
    if m.get("pct_yes") is None and _isnum(yes) and _isnum(m.get("votes_total")) \
            and m["votes_total"] > 0:
        m["pct_yes"] = round(100.0 * yes / m["votes_total"], 4)
    if m.get("turnout_pct") is None and _isnum(m.get("ballots_cast")) \
            and _isnum(m.get("registered_voters")) and m["registered_voters"] > 0:
        m["turnout_pct"] = round(100.0 * m["ballots_cast"] / m["registered_voters"], 4)

    if m.get("threshold_required") is None:
        rule = threshold_for(conn, m)
        if rule:
            m["threshold_rule_id"] = rule["id"]
            m["threshold_required"] = rule["threshold_value"]
            if m.get("threshold_basis") is None:
                m["threshold_basis"] = rule["threshold_basis"]

    if m.get("margin_vs_threshold") is None and _isnum(m.get("pct_yes")) \
            and _isnum(m.get("threshold_required")):
        m["margin_vs_threshold"] = round(m["pct_yes"] - m["threshold_required"], 4)
    return m


def threshold_for(conn, m):
    """The threshold_rule in force for this measure on its election date.

    Most specific first: the jurisdiction's own kind beats a statewide default,
    and a purpose-specific rule beats 'either'.
    """
    j = conn.execute("SELECT state_usps, kind FROM jurisdiction WHERE geoid=?",
                     (m.get("geoid"),)).fetchone()
    if not j:
        return None
    purpose = m.get("purpose_type")
    purposes = [purpose, "either", None] if purpose in ("general", "special") \
        else ["either", None]
    for kind in (j["kind"], None):
        for pur in purposes:
            row = conn.execute(
                "SELECT * FROM threshold_rule WHERE state_usps=? AND measure_class=? "
                "AND (jurisdiction_kind IS ?) AND (purpose_restriction IS ?) "
                "AND (effective_from IS NULL OR effective_from <= ?) "
                "AND (effective_to IS NULL OR effective_to >= ?) "
                "ORDER BY COALESCE(effective_from,'0000') DESC, id DESC LIMIT 1",
                (j["state_usps"], m.get("measure_class"), kind, pur,
                 m.get("election_date"), m.get("election_date"))).fetchone()
            if row:
                return row
    return None


THRESHOLD_COLS = (
    "jurisdiction_kind", "measure_class", "instrument_code", "purpose_restriction",
    "threshold_value", "threshold_basis", "threshold_note", "election_timing",
    "timing_note", "turnout_requirement", "sunset_required", "sunset_max_years",
    "reimposition_allowed", "cooling_off_months", "governing_body_vote",
    "petition_alternative", "ballot_language_rules", "statute_cite",
    "constitutional_cite", "effective_from", "effective_to", "extraction_method",
    "confidence", "notes",
)


def _write_thresholds(ctx, rows):
    conn = ctx.conn
    written = 0
    for t in rows:
        t = dict(t)
        t["state_usps"] = t["state_usps"].upper()
        scope = _state_geoid(conn, t["state_usps"])
        source_id = ctx.source_id(t, scope_geoid=scope)
        values = {c: t.get(c) for c in THRESHOLD_COLS}
        values["extraction_method"] = t.get("extraction_method") or ctx.method

        old = conn.execute(
            "SELECT id FROM threshold_rule WHERE state_usps=? "
            "AND ifnull(jurisdiction_kind,'')=ifnull(?,'') AND measure_class=? "
            "AND ifnull(instrument_code,'')=ifnull(?,'') "
            "AND ifnull(purpose_restriction,'')=ifnull(?,'') "
            "AND ifnull(effective_from,'')=ifnull(?,'')",
            (t["state_usps"], t.get("jurisdiction_kind"), t["measure_class"],
             t.get("instrument_code"), t.get("purpose_restriction"),
             t.get("effective_from"))).fetchone()
        if old:
            sets = [c for c in THRESHOLD_COLS if values.get(c) is not None]
            conn.execute(
                "UPDATE threshold_rule SET %s, source_id=?, archive_file_id=? WHERE id=?"
                % ", ".join("%s=?" % c for c in sets),
                [values[c] for c in sets]
                + [source_id, t.get("archive_file_id"), old["id"]])
            claim_id = old["id"]
        else:
            cols = ["state_usps"] + list(THRESHOLD_COLS) + ["source_id", "archive_file_id"]
            params = [t["state_usps"]] + [values[c] for c in THRESHOLD_COLS] + [
                source_id, t.get("archive_file_id")]
            cur = conn.execute(
                "INSERT INTO threshold_rule (%s) VALUES (%s)"
                % (", ".join(cols), ",".join("?" * len(cols))), params)
            claim_id = cur.lastrowid
        ctx.cite("threshold_rule", claim_id, t, source_id)
        written += 1
        if scope:
            ctx.touched.add((scope, FRAMEWORK))
    return written


GRANT_COLS = (
    "jurisdiction_kind", "category", "instrument_code", "permitted",
    "eligibility_note", "max_rate", "max_rate_unit", "aggregate_cap_note",
    "stacking_rule", "statute_cite", "extraction_method", "effective_from",
    "effective_to", "confidence", "notes",
)


def _write_grants(ctx, rows):
    conn = ctx.conn
    written = 0
    for g in rows:
        g = dict(g)
        g["state_usps"] = g["state_usps"].upper()
        scope = _state_geoid(conn, g["state_usps"])
        source_id = ctx.source_id(g, scope_geoid=scope)
        values = {c: g.get(c) for c in GRANT_COLS}
        values["extraction_method"] = g.get("extraction_method") or ctx.method

        old = conn.execute(
            "SELECT id FROM authority_grant WHERE state_usps=? "
            "AND ifnull(jurisdiction_kind,'')=ifnull(?,'') AND category=? "
            "AND instrument_code=? AND ifnull(effective_from,'')=ifnull(?,'')",
            (g["state_usps"], g.get("jurisdiction_kind"), g["category"],
             g["instrument_code"], g.get("effective_from"))).fetchone()
        if old:
            sets = [c for c in GRANT_COLS if values.get(c) is not None]
            conn.execute(
                "UPDATE authority_grant SET %s, source_id=?, archive_file_id=? WHERE id=?"
                % ", ".join("%s=?" % c for c in sets),
                [values[c] for c in sets]
                + [source_id, g.get("archive_file_id"), old["id"]])
            claim_id = old["id"]
        else:
            cols = ["state_usps"] + list(GRANT_COLS) + ["source_id", "archive_file_id"]
            params = [g["state_usps"]] + [values[c] for c in GRANT_COLS] + [
                source_id, g.get("archive_file_id")]
            cur = conn.execute(
                "INSERT INTO authority_grant (%s) VALUES (%s)"
                % (", ".join(cols), ",".join("?" * len(cols))), params)
            claim_id = cur.lastrowid
        ctx.cite("authority_grant", claim_id, g, source_id)
        written += 1
        if scope:
            ctx.touched.add((scope, FRAMEWORK))
    return written


def _write_profiles(ctx, rows):
    conn = ctx.conn
    written = 0
    for p in rows:
        usps = p["state_usps"].upper()
        fields = {k: v for k, v in p.items()
                  if k != "state_usps" and v is not None}
        if not fields:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO state_profile (state_usps, state_name) VALUES (?,?)",
            (usps, usps))
        conn.execute(
            "UPDATE state_profile SET %s, verified_at=?, verified_by=? WHERE state_usps=?"
            % ", ".join("%s=?" % k for k in fields),
            list(fields.values()) + [db.now(), ctx.researcher, usps])
        written += 1
    return written


def _state_geoid(conn, usps):
    row = conn.execute(
        "SELECT geoid FROM jurisdiction WHERE kind='state' AND state_usps=?",
        (usps,)).fetchone()
    return row["geoid"] if row else None


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


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

    # ---- the layers that used to have no writer, and so no checks either
    add("threshold recorded as a fraction rather than a percentage",
        "SELECT id, state_usps, measure_class, threshold_value FROM threshold_rule "
        "WHERE threshold_value <= 1.0")

    add("threshold or cap with no statutory cite",
        "SELECT 'threshold' AS kind, id, state_usps FROM threshold_rule "
        "WHERE statute_cite IS NULL OR trim(statute_cite)='' "
        "UNION ALL SELECT 'grant', id, state_usps FROM authority_grant "
        "WHERE statute_cite IS NULL OR trim(statute_cite)=''")

    add("cap with a rate but no unit",
        "SELECT id, state_usps, instrument_code, max_rate FROM authority_grant "
        "WHERE max_rate IS NOT NULL AND (max_rate_unit IS NULL OR max_rate_unit='')")

    add("measure with a result but no threshold to judge it against",
        "SELECT id, geoid, election_date, measure_id_local FROM ballot_measure "
        "WHERE superseded_by IS NULL AND pct_yes IS NOT NULL "
        "AND threshold_required IS NULL")

    add("measure margin inconsistent with its own yes share and threshold",
        "SELECT id, geoid, pct_yes, threshold_required, margin_vs_threshold "
        "FROM ballot_measure WHERE margin_vs_threshold IS NOT NULL "
        "AND pct_yes IS NOT NULL AND threshold_required IS NOT NULL "
        "AND ABS(margin_vs_threshold - (pct_yes - threshold_required)) > 0.05")

    add("measure with no vote counts and no percentage",
        "SELECT id, geoid, election_date FROM ballot_measure "
        "WHERE superseded_by IS NULL AND votes_total IS NULL AND pct_yes IS NULL")

    # The gap that keeps v_headroom empty for a whole state.
    add("state holding tax rates but no authority caps",
        "SELECT DISTINCT j.state_usps FROM tax_instrument t "
        "JOIN jurisdiction j ON j.geoid = t.geoid "
        "WHERE t.superseded_by IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM authority_grant g WHERE g.state_usps = j.state_usps)")

    add("state holding measures but no vote thresholds",
        "SELECT DISTINCT j.state_usps FROM ballot_measure b "
        "JOIN jurisdiction j ON j.geoid = b.geoid "
        "WHERE b.superseded_by IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM threshold_rule r WHERE r.state_usps = j.state_usps)")

    add("county with measures recorded but no coverage assertion",
        "SELECT DISTINCT b.geoid FROM ballot_measure b WHERE NOT EXISTS ("
        "  SELECT 1 FROM coverage_assertion c WHERE c.domain='ballot_measure' "
        "  AND c.scope_geoid = b.geoid)")

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
