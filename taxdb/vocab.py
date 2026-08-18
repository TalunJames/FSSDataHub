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
