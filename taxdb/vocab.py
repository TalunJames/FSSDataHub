"""Controlled vocabulary.

Free-text categories are how a 22,000-row research project turns into
mush. Everything that gets stored is validated against these lists.
"""

CATEGORIES = {
    "property": "Ad valorem taxes on real and personal property, plus their limits.",
    "sales_use": "General and special local sales, use, and gross receipts taxes.",
    "income_payroll": "Local income, earnings, occupational, and payroll taxes.",
    "lodging_meals": "Transient lodging, hotel/motel, food and beverage taxes.",
    "other_levy": "Franchise fees, utility taxes, impact fees, special assessments, excise.",
}

# Instrument codes, grouped by category. The 'possible taxes' question is
# answered by carrying a row with status='authorized_not_levied' rather
# than by silence, so codes exist for instruments a place may never levy.
INSTRUMENTS = {
    "property": [
        "general_operating_levy",
        "debt_service_levy",
        "special_district_levy",
        "tif_increment",
        "assessment_ratio",
        "homestead_exemption",
        "rate_cap",
        "levy_growth_limit",
        "assessment_growth_limit",
    ],
    "sales_use": [
        "local_option_sales",
        "county_general_sales",
        "municipal_general_sales",
        "special_purpose_sales",
        "transit_district_sales",
        "use_tax",
        "gross_receipts",
        "sales_tax_cap",
    ],
    "income_payroll": [
        "local_income_tax",
        "earnings_tax",
        "occupational_license",
        "payroll_expense",
        "net_profits",
        "intangibles",
    ],
    "lodging_meals": [
        "transient_lodging",
        "hotel_motel",
        "food_beverage",
        "tourism_district",
    ],
    "other_levy": [
        "franchise_fee",
        "utility_users",
        "business_license",
        "impact_fee",
        "special_assessment",
        "real_estate_transfer",
        "local_motor_fuel",
        "amusement_admissions",
        "severance",
    ],
}

ALL_INSTRUMENTS = {c for codes in INSTRUMENTS.values() for c in codes}

# Two research passes that are not tax categories but do belong in the work
# queue. 'framework' runs once per state and fills threshold_rule,
# authority_grant, and state_profile -- 51 rows that gate v_headroom for every
# jurisdiction underneath. 'elections' runs per county, where canvass and
# official-results documents actually live, and fills ballot_measure.
FRAMEWORK = "framework"
ELECTIONS = "elections"

PASSES = {
    FRAMEWORK: "State statutory framework: thresholds, authority caps, profile.",
    ELECTIONS: "Local revenue measures and their results, from official canvasses.",
}

# What may appear in work_item.category. Findings validation still uses
# CATEGORIES: a tax row can only carry a tax category.
WORK_CATEGORIES = dict(CATEGORIES)
WORK_CATEGORIES.update(PASSES)

# The kind of jurisdiction each pass is planned against.
PASS_KINDS = {FRAMEWORK: ("state",), ELECTIONS: ("county",)}

STATUSES = {
    "levied",
    "authorized_not_levied",
    "prohibited",
    "repealed",
    "unknown",
}

RATE_UNITS = {
    "percent", "mills", "dollars_per_1000_av", "usd_flat", "usd_per_unit", "ratio",
}

CAP_TYPES = {
    "rate_cap", "levy_growth_cap", "assessment_cap", "aggregate_cap", "none",
}

CONFIDENCE = {"high", "medium", "low"}

EXTRACTION_METHODS = {"bulk_import", "agent_research", "manual", "api"}

WORK_STATUSES = {
    "pending", "in_progress", "needs_review", "complete", "no_data", "blocked",
    # Crawled and archived, waiting on a batch extraction to come back. Not
    # claimable (claim only takes 'pending') and not stale-swept (release_stale
    # only touches 'in_progress'), so it parks safely for hours.
    "awaiting_ai",
}

SOURCE_TYPES = {
    "statute", "agency_table", "bulk_file", "ordinance", "portal", "secondary",
}

JURISDICTION_KINDS = {"state", "county", "place", "mcd", "school"}

TERNARY = {"yes", "no", "conditional", "unknown"}

MEASURE_CLASSES = {
    "tax_new": "New tax not previously levied here.",
    "tax_increase": "Rate increase on an existing tax.",
    "tax_extension": "Extends an existing tax past its sunset, same rate.",
    "tax_increase_and_extension": "Both. Common and politically distinct.",
    "tax_renewal": "Reimposition after expiration.",
    "tax_repeal": "Voter-initiated repeal or reduction.",
    "bond_go": "General obligation bond, property-tax secured.",
    "bond_revenue": "Revenue bond requiring voter approval.",
    "levy_override": "Exceeds a levy/revenue cap (WA lid lift, WI override, OH levy).",
    "levy_renewal": "Renews an expiring override.",
    "assessment_district": "Parcel tax, benefit assessment, Mello-Roos, BID.",
    "de_bruce": "TABOR revenue-retention (CO-specific).",
    "charter_fiscal": "Charter amendment with revenue effect.",
    "advisory": "Non-binding companion measure.",
    "other": "",
}

REVENUE_MEASURE_CLASSES = {
    "tax_new", "tax_increase", "tax_extension", "tax_increase_and_extension",
    "tax_renewal", "bond_go", "bond_revenue", "levy_override", "levy_renewal",
    "assessment_district", "de_bruce", "charter_fiscal",
}

THRESHOLD_BASES = {
    "votes_cast",
    "registered_voters",
    "dual_majority",
    "votes_cast_plus_turnout_validation",
    "governing_body_only",
}

ELECTION_TYPES = {
    "general", "primary", "special", "consolidated", "mail_only", "annual_town_meeting",
}

CHANGE_TYPES = {
    "new", "increase", "decrease", "expired", "abolished",
    "extended", "renamed", "boundary_change",
}

HOME_RULE = {"dillon", "home_rule", "mixed", "unknown"}

PROPERTY_TAX_LIMIT_TYPES = {
    "rate_cap", "levy_growth_cap", "assessment_cap", "combined", "none", "unknown",
}

BASE_TYPES = {
    "taxable_sales", "assessed_value", "taxable_av", "room_revenue",
    "wage_base", "parcels", "utility_receipts",
    "collections_property", "collections_sales_use",
    "collections_lodging_meals", "collections_income_payroll",
}

YIELD_METHODS = {
    "official_estimate", "base_x_rate", "collections_scaled", "peer_regression",
}

COMPLETENESS = {"complete", "substantial", "partial", "spot_checked", "none"}

CLAIM_ROLES = {"primary", "corroborating", "conflicting", "superseded"}

OUTCOMES = {"passed", "failed", "withdrawn", "pending", "invalidated", "unknown"}


def validate_finding(f, index=0):
    """Return a list of human-readable problems with a finding dict."""
    errs = []

    def bad(msg):
        errs.append("finding[%d]: %s" % (index, msg))

    cat = f.get("category")
    if cat not in CATEGORIES:
        bad("category %r not in %s" % (cat, sorted(CATEGORIES)))

    code = f.get("instrument_code")
    if code not in ALL_INSTRUMENTS:
        bad("instrument_code %r is not a known instrument" % (code,))
    elif cat in INSTRUMENTS and code not in INSTRUMENTS[cat]:
        bad("instrument_code %r does not belong to category %r" % (code, cat))

    if f.get("status") not in STATUSES:
        bad("status %r not in %s" % (f.get("status"), sorted(STATUSES)))

    if not f.get("geoid"):
        bad("missing geoid")

    ru = f.get("rate_unit")
    if ru is not None and ru not in RATE_UNITS:
        bad("rate_unit %r not in %s" % (ru, sorted(RATE_UNITS)))

    ct = f.get("cap_type")
    if ct is not None and ct not in CAP_TYPES:
        bad("cap_type %r not in %s" % (ct, sorted(CAP_TYPES)))

    conf = f.get("confidence")
    if conf not in CONFIDENCE:
        bad("confidence %r not in %s" % (conf, sorted(CONFIDENCE)))

    va = f.get("voter_approval_required")
    if va is not None and va not in TERNARY:
        bad("voter_approval_required %r not in %s" % (va, sorted(TERNARY)))

    src = f.get("source") or {}
    if not src.get("url"):
        bad("no source.url -- every claim needs a citation")
    tier = src.get("authority_tier")
    if tier is not None and tier not in (1, 2, 3, 4):
        bad("source.authority_tier %r must be 1-4" % (tier,))

    if f.get("rate_value") is not None and not ru:
        bad("rate_value given without rate_unit")

    if ru == "percent" and isinstance(f.get("rate_value"), (int, float)):
        if f["rate_value"] > 25 or f["rate_value"] < 0:
            bad("rate_value %s percent is out of plausible range -- unit mixup?"
                % f["rate_value"])

    if f.get("status") == "levied" and f.get("rate_value") is None \
            and code not in ("assessment_ratio", "homestead_exemption"):
        bad("status='levied' but no rate_value (use status='unknown' if the "
            "rate could not be found)")

    exp = f.get("expiration_date")
    if exp and not _looks_iso_date(exp):
        bad("expiration_date %r is not ISO YYYY-MM-DD "
            "(SQLite julianday() returns NULL on US dates, silently dropping "
            "the row from v_sunset_watch)" % exp)

    return errs


def _looks_iso_date(value):
    if not isinstance(value, str) or len(value) < 10:
        return False
    part = value[:10]
    if part[4] != "-" or part[7] != "-":
        return False
    try:
        y, m, d = int(part[:4]), int(part[5:7]), int(part[8:10])
    except ValueError:
        return False
    return 1 <= m <= 12 and 1 <= d <= 31 and y >= 1800


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_source(src, bad):
    if not (src or {}).get("url"):
        bad("no source.url -- every claim needs a citation")
    tier = (src or {}).get("authority_tier")
    if tier is not None and tier not in (1, 2, 3, 4):
        bad("source.authority_tier %r must be 1-4" % (tier,))


def validate_measure(m, index=0):
    """Problems with a ballot_measure row.

    Vote arithmetic is checked here rather than at read time: a measure whose
    pct_yes disagrees with its own vote counts is the one number nobody should
    ever quote to a client.
    """
    errs = []

    def bad(msg):
        errs.append("measure[%d]: %s" % (index, msg))

    if not m.get("geoid"):
        bad("missing geoid")

    date = m.get("election_date")
    if not date:
        bad("missing election_date")
    elif not _looks_iso_date(date):
        bad("election_date %r is not ISO YYYY-MM-DD" % (date,))

    cls = m.get("measure_class")
    if cls not in MEASURE_CLASSES:
        bad("measure_class %r not in %s" % (cls, sorted(MEASURE_CLASSES)))

    out = m.get("outcome")
    if out not in OUTCOMES:
        bad("outcome %r not in %s" % (out, sorted(OUTCOMES)))

    et = m.get("election_type")
    if et is not None and et not in ELECTION_TYPES:
        bad("election_type %r not in %s" % (et, sorted(ELECTION_TYPES)))

    cat = m.get("category")
    if cat is not None and cat not in CATEGORIES:
        bad("category %r not in %s" % (cat, sorted(CATEGORIES)))

    code = m.get("instrument_code")
    if code is not None and code not in ALL_INSTRUMENTS:
        bad("instrument_code %r is not a known instrument" % (code,))

    tb = m.get("threshold_basis")
    if tb is not None and tb not in THRESHOLD_BASES:
        bad("threshold_basis %r not in %s" % (tb, sorted(THRESHOLD_BASES)))

    if m.get("confidence") not in CONFIDENCE:
        bad("confidence %r not in %s" % (m.get("confidence"), sorted(CONFIDENCE)))

    for field in ("pct_yes", "threshold_required", "turnout_pct"):
        v = m.get(field)
        if v is not None and (not _num(v) or v < 0 or v > 100):
            bad("%s %r must be a percentage between 0 and 100" % (field, v))

    for field in ("votes_yes", "votes_no", "votes_total", "registered_voters",
                  "ballots_cast"):
        v = m.get(field)
        if v is not None and (not _num(v) or v < 0):
            bad("%s %r must be a non-negative count" % (field, v))

    yes, no, total = m.get("votes_yes"), m.get("votes_no"), m.get("votes_total")
    if _num(yes) and _num(no) and _num(total) and yes + no > total:
        bad("votes_yes + votes_no (%s) exceeds votes_total (%s)" % (yes + no, total))

    # Fall back to yes+no when no total was given, so omitting votes_total
    # cannot smuggle a percentage past this check.
    effective_total = total if _num(total) else (
        yes + no if _num(yes) and _num(no) else None)
    pct = m.get("pct_yes")
    if pct is None and _num(yes) and _num(effective_total) and effective_total > 0:
        pct = 100.0 * yes / effective_total
    if _num(yes) and _num(effective_total) and effective_total > 0 and _num(m.get("pct_yes")):
        implied = 100.0 * yes / effective_total
        if abs(implied - m["pct_yes"]) > 0.5:
            bad("pct_yes %s does not match votes_yes/votes_total (%.2f)"
                % (m["pct_yes"], implied))

    if out == "passed" and _num(pct) and _num(m.get("threshold_required")):
        if pct < m["threshold_required"]:
            bad("outcome is 'passed' but %.2f%% yes is below the %s threshold "
                "-- check the basis (votes cast vs registered voters)"
                % (pct, m["threshold_required"]))

    for field in ("sunset_date",):
        v = m.get(field)
        if v and not _looks_iso_date(v):
            bad("%s %r is not ISO YYYY-MM-DD" % (field, v))

    _check_source(m.get("source"), bad)
    return errs


def validate_threshold(t, index=0):
    """Problems with a threshold_rule row."""
    errs = []

    def bad(msg):
        errs.append("threshold[%d]: %s" % (index, msg))

    st = t.get("state_usps")
    if not st or len(str(st)) != 2:
        bad("state_usps %r must be a two-letter code" % (st,))

    kind = t.get("jurisdiction_kind")
    if kind is not None and kind not in JURISDICTION_KINDS:
        bad("jurisdiction_kind %r not in %s" % (kind, sorted(JURISDICTION_KINDS)))

    cls = t.get("measure_class")
    if cls not in MEASURE_CLASSES:
        bad("measure_class %r not in %s" % (cls, sorted(MEASURE_CLASSES)))

    code = t.get("instrument_code")
    if code is not None and code not in ALL_INSTRUMENTS:
        bad("instrument_code %r is not a known instrument" % (code,))

    pr = t.get("purpose_restriction")
    if pr is not None and pr not in ("general", "special", "either"):
        bad("purpose_restriction %r must be general, special, or either" % (pr,))

    tv = t.get("threshold_value")
    if not _num(tv):
        bad("threshold_value is required and must be a number")
    elif tv < 0 or tv > 100:
        bad("threshold_value %r must be a percentage between 0 and 100 "
            "(two-thirds is 66.67, not 0.6667)" % (tv,))
    elif tv <= 1.0:
        # 0.6667 and 66.67 both read as two-thirds to a model, and only one of
        # them makes every downstream margin correct.
        bad("threshold_value %r looks like a fraction; record percentages "
            "(two-thirds is 66.67, not 0.6667)" % (tv,))

    tb = t.get("threshold_basis")
    if tb not in THRESHOLD_BASES:
        bad("threshold_basis %r not in %s" % (tb, sorted(THRESHOLD_BASES)))

    for field in ("sunset_required", "reimposition_allowed", "petition_alternative"):
        v = t.get(field)
        if v is not None and v not in TERNARY:
            bad("%s %r not in %s" % (field, v, sorted(TERNARY)))

    sm = t.get("sunset_max_years")
    if sm is not None and (not _num(sm) or sm < 0):
        bad("sunset_max_years %r must be a non-negative number" % (sm,))

    co = t.get("cooling_off_months")
    if co is not None and (not _num(co) or co < 0):
        bad("cooling_off_months %r must be a non-negative number" % (co,))

    if not t.get("statute_cite"):
        bad("statute_cite is required -- a threshold with no cite is unusable")

    if t.get("confidence") not in CONFIDENCE:
        bad("confidence %r not in %s" % (t.get("confidence"), sorted(CONFIDENCE)))

    for field in ("effective_from", "effective_to"):
        v = t.get(field)
        if v and not _looks_iso_date(v):
            bad("%s %r is not ISO YYYY-MM-DD" % (field, v))

    _check_source(t.get("source"), bad)
    return errs


def validate_grant(g, index=0):
    """Problems with an authority_grant row."""
    errs = []

    def bad(msg):
        errs.append("grant[%d]: %s" % (index, msg))

    st = g.get("state_usps")
    if not st or len(str(st)) != 2:
        bad("state_usps %r must be a two-letter code" % (st,))

    kind = g.get("jurisdiction_kind")
    if kind is not None and kind not in JURISDICTION_KINDS:
        bad("jurisdiction_kind %r not in %s" % (kind, sorted(JURISDICTION_KINDS)))

    cat = g.get("category")
    if cat not in CATEGORIES:
        bad("category %r not in %s" % (cat, sorted(CATEGORIES)))

    code = g.get("instrument_code")
    if code not in ALL_INSTRUMENTS:
        bad("instrument_code %r is not a known instrument" % (code,))
    elif cat in INSTRUMENTS and code not in INSTRUMENTS[cat]:
        bad("instrument_code %r does not belong to category %r" % (code, cat))

    perm = g.get("permitted")
    if perm not in ("yes", "no", "conditional"):
        bad("permitted %r must be yes, no, or conditional" % (perm,))

    mr, mu = g.get("max_rate"), g.get("max_rate_unit")
    if mr is not None and not _num(mr):
        bad("max_rate %r must be a number" % (mr,))
    if mr is not None and not mu:
        bad("max_rate given without max_rate_unit")
    if mu is not None and mu not in RATE_UNITS:
        bad("max_rate_unit %r not in %s" % (mu, sorted(RATE_UNITS)))
    if mu == "percent" and _num(mr) and (mr < 0 or mr > 25):
        bad("max_rate %s percent is out of plausible range -- unit mixup?" % (mr,))
    if perm == "no" and mr is not None:
        bad("permitted is 'no' but a max_rate is recorded")

    if not g.get("statute_cite"):
        bad("statute_cite is required -- a cap with no cite is unusable")

    if g.get("confidence") not in CONFIDENCE:
        bad("confidence %r not in %s" % (g.get("confidence"), sorted(CONFIDENCE)))

    for field in ("effective_from", "effective_to"):
        v = g.get(field)
        if v and not _looks_iso_date(v):
            bad("%s %r is not ISO YYYY-MM-DD" % (field, v))

    _check_source(g.get("source"), bad)
    return errs


PROFILE_FIELDS = {
    "home_rule_doctrine": HOME_RULE,
    "property_tax_limit_type": PROPERTY_TAX_LIMIT_TYPES,
    "local_sales_tax_allowed": TERNARY,
    "local_income_tax_allowed": TERNARY,
    "local_lodging_tax_allowed": TERNARY,
    "property_tax_limit_summary": None,
    "local_sales_tax_max": None,
    "statute_root_url": None,
    "revenue_agency": None,
    "revenue_agency_url": None,
    "notes": None,
}


def validate_profile(p, index=0):
    """Problems with a state_profile patch."""
    errs = []

    def bad(msg):
        errs.append("profile[%d]: %s" % (index, msg))

    st = p.get("state_usps")
    if not st or len(str(st)) != 2:
        bad("state_usps %r must be a two-letter code" % (st,))

    for key, allowed in PROFILE_FIELDS.items():
        v = p.get(key)
        if v is None or allowed is None:
            continue
        if v not in allowed:
            bad("%s %r not in %s" % (key, v, sorted(allowed)))

    mx = p.get("local_sales_tax_max")
    if mx is not None and (not _num(mx) or mx < 0 or mx > 25):
        bad("local_sales_tax_max %r must be a percentage between 0 and 25" % (mx,))

    unknown = [k for k in p if k not in PROFILE_FIELDS and k != "state_usps"]
    if unknown:
        bad("unknown profile field(s): %s" % ", ".join(sorted(unknown)))
    return errs
