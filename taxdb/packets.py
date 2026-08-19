"""Research packets.

A packet is a self-contained brief for one jurisdiction: what is already
known, which state framework applies, which sources are on file, and the
exact JSON shape the answer must come back in. It is what makes the work
delegable -- to a subagent, a contractor, or yourself three weeks from now.
"""

import json
import os

from . import db
from .vocab import (
    CATEGORIES, ELECTIONS, FRAMEWORK, INSTRUMENTS, MEASURE_CLASSES,
    REVENUE_MEASURE_CLASSES, THRESHOLD_BASES,
)

PROMPT_HEADER = """\
# Research packet: {name} ({kind}, {state})

GEOID `{geoid}` | population {pop} | categories: {cats}

## What is already recorded

{known}

## State framework ({state})

{profile}

## Sources already on file

{sources}

## Census collections prior (not a cite)

{priors}

## State statute excerpts on file

{statutes}

## What to find

{questions}

## Rules

{rules}

## Return format

Write a JSON file matching this shape and load it with `taxdb ingest FILE`:

```json
{schema}
```
"""

# The full rules make a packet a self-contained brief for a human researcher.
# The collector's model calls already carry all of this in the cached system
# prompt (extract.SYSTEM), so repeating it in the uncached user message paid
# for the same ~200 tokens on every one of 22,000 items. The lean form keeps
# only what the system prompt does not say.
RULES_FULL = """\
- Every claim needs a source URL. Prefer primary law (statutes, adopted
  ordinances) and the state agency of record over aggregators and vendors.
- If a tax is legally available here but not imposed, record it with
  `status: "authorized_not_levied"`. Absence of a row is not an answer.
- If a tax is barred by state law, record `status: "prohibited"` with the cite.
- If you cannot find a rate, use `status: "unknown"` and say why in `notes`.
  Do not estimate, interpolate from neighbors, or infer from a state average.
- Give the rate exactly as published, with its unit. Do not convert mills to
  percent or vice versa.
- Note the fiscal or tax year the figure applies to.
- Dates must be ISO `YYYY-MM-DD`. US formats silently vanish from sunset watch.
- Include `source_quote`: a short verbatim phrase from the documents that
  contains the rate, the prohibition, or the authorization. `taxdb verify`
  checks that the quote appears in the archived bytes."""

RULES_LEAN = """\
- Note the fiscal or tax year the figure applies to.
- `taxdb verify` later checks that `source_quote` appears in the archived
  bytes, so copy it exactly."""

EXAMPLE = {
    "schema_version": "1.0",
    "researcher": "your name or model id",
    "findings": [
        {
            "geoid": "GEOID",
            "category": "sales_use",
            "instrument_code": "county_general_sales",
            "label": "county general sales tax",
            "status": "levied",
            "rate_value": 1.5,
            "rate_unit": "percent",
            "rate_basis": "gross receipts from retail sales",
            "cap_type": "rate_cap",
            "cap_value": 2.0,
            "cap_unit": "percent",
            "cap_note": "statutory maximum absent voter approval",
            "voter_approval_required": "yes",
            "effective_date": "2019-10-01",
            "fiscal_year": 2026,
            "statute_cite": "Rev. Stat. § 00-000",
            "source": {
                "url": "https://...",
                "name": "State DOR local rate table",
                "source_type": "agency_table",
                "authority_tier": 2,
            },
            "confidence": "high",
            "source_quote": "1.5 percent county sales tax",
            "notes": "",
        }
    ],
}

QUESTIONS = {
    "property": [
        "Current general operating millage / levy rate, with unit and tax year.",
        "Debt service levy, if separate.",
        "Assessment ratio(s) by property class.",
        "Rate cap, levy growth cap, and assessment growth cap that bind this "
        "jurisdiction, with statutory cite.",
        "Whether the jurisdiction is at, under, or over its cap, and by how much.",
        "Voter-approved overrides in force and their expiration.",
    ],
    "sales_use": [
        "Local sales tax rate imposed by this jurisdiction (not the combined rate).",
        "Statutory maximum local rate, and remaining headroom.",
        "Special purpose / transit / district sales taxes layered on top.",
        "Whether a local use tax is imposed.",
        "Whether the jurisdiction is authorized to levy but currently does not.",
    ],
    "income_payroll": [
        "Local income, earnings, or occupational license tax rate, if any.",
        "Whether state law permits this class of jurisdiction to impose one.",
        "Resident vs. nonresident rate differences.",
    ],
    "lodging_meals": [
        "Transient lodging / hotel tax rate and what it funds.",
        "Food and beverage tax rate, if any.",
        "Statutory cap and voter approval requirement.",
    ],
    "other_levy": [
        "Franchise fees and utility users tax.",
        "Business license tax structure.",
        "Real estate transfer tax.",
        "Local motor fuel, amusement, or severance taxes.",
        "Impact fees and standing special assessments.",
    ],
}


def build(conn, geoid, categories=None, lean=False):
    """Packet for one work item. Routes the two research passes to their own
    shapes; everything else is the tax-rate packet.

    lean=True drops the rules the model already has in its system prompt.
    Use it for collector model calls; human-facing packet files keep the
    full, self-contained rules."""
    j = conn.execute("SELECT * FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
    if not j:
        raise SystemExit("no jurisdiction %r" % geoid)

    cats = [c for c in (categories or []) if c]
    if any(c in (FRAMEWORK, ELECTIONS) for c in cats):
        # A pass has its own packet shape. Mixing one with anything else used
        # to fall through to the five-category tax packet — five times the
        # tokens, and none of them describing the claimed item.
        if len(set(cats)) > 1:
            raise SystemExit(
                "one packet per pass: cannot mix %r in a single packet" % (cats,))
        if cats[0] == FRAMEWORK:
            return build_framework(conn, j["state_usps"], lean=lean)
        return build_elections(conn, geoid, lean=lean)

    if categories is None:
        rows = conn.execute(
            "SELECT category FROM work_item WHERE geoid=? AND status IN "
            "('pending','in_progress','needs_review')", (geoid,)).fetchall()
        categories = [r["category"] for r in rows if r["category"] in CATEGORIES]
        categories = categories or list(CATEGORIES)
    else:
        unknown = [c for c in cats if c not in CATEGORIES]
        if unknown or not cats:
            raise SystemExit("unknown packet categories %r" % (unknown or cats,))
        categories = cats

    # Scoped to the categories being researched and bounded: this text rides
    # in the model prompt for every item, so an unbounded dump of every live
    # row across all five categories is paid for on every call.
    known = conn.execute(
        "SELECT category, instrument_code, status, rate_value, rate_unit, retrieved_at "
        "FROM tax_instrument WHERE geoid=? AND superseded_by IS NULL "
        "AND category IN (%s) ORDER BY category LIMIT 60"
        % ",".join("?" * len(categories)), [geoid] + list(categories)).fetchall()
    if known:
        known_txt = "\n".join(
            "- `%s` / `%s`: %s%s (as of %s)" % (
                r["category"], r["instrument_code"], r["status"],
                (" @ %s %s" % (r["rate_value"], r["rate_unit"]))
                if r["rate_value"] is not None else "",
                (r["retrieved_at"] or "")[:10])
            for r in known)
    else:
        known_txt = "_Nothing recorded yet._"

    p = conn.execute("SELECT * FROM state_profile WHERE state_usps=?",
                     (j["state_usps"],)).fetchone()
    if p and (p["verified_at"] or p["property_tax_limit_summary"]):
        profile_txt = "\n".join(filter(None, [
            "- Home rule doctrine: %s" % (p["home_rule_doctrine"] or "unknown"),
            "- Property tax limit type: %s" % (p["property_tax_limit_type"] or "unknown"),
            "- %s" % p["property_tax_limit_summary"] if p["property_tax_limit_summary"] else None,
            "- Local sales tax allowed: %s (max %s)" % (
                p["local_sales_tax_allowed"] or "unknown", p["local_sales_tax_max"]),
            "- Local income tax allowed: %s" % (p["local_income_tax_allowed"] or "unknown"),
            "- Statutes: %s" % p["statute_root_url"] if p["statute_root_url"] else None,
            "- Revenue agency: %s %s" % (p["revenue_agency"] or "", p["revenue_agency_url"] or ""),
        ]))
    else:
        profile_txt = ("_State profile not yet researched._ Do this state's profile "
                       "first (`taxdb profile %s`) -- it answers the 'possible taxes' "
                       "question for every jurisdiction in the state at once."
                       % j["state_usps"])

    # Bounded: scoped sources accumulate as a place is re-researched, and an
    # unbounded list grows the prompt of every future packet for this geoid.
    srcs = conn.execute(
        "SELECT name, url, authority_tier FROM source WHERE scope_geoid IN (?,?,?) "
        "ORDER BY authority_tier LIMIT 25",
        (geoid, j["state_fips"], j["county_fips"])).fetchall()
    src_txt = "\n".join("- [tier %d] %s -- %s" % (s["authority_tier"], s["name"], s["url"])
                        for s in srcs) or "_None on file. Start from the state agency of record._"

    priors = conn.execute(
        "SELECT base_type, base_value, notes FROM revenue_base "
        "WHERE geoid=? AND is_current_vintage=1 ORDER BY base_type",
        (geoid,)).fetchall()
    if priors:
        prior_txt = "\n".join(
            "- `%s`: $%s  — %s" % (
                r["base_type"], "{:,.0f}".format(r["base_value"] or 0),
                (r["notes"] or "")[:160])
            for r in priors)
        prior_txt += ("\n\nZero collections are a no-levy *prior*, not a finding. "
                      "Confirm before recording `authorized_not_levied`.")
    else:
        prior_txt = "_No Census of Governments collections loaded. `taxdb cog`._"

    try:
        from . import statutes
        hits = statutes.for_packet(conn, j["state_usps"], categories)
    except Exception:
        hits = []
    if hits:
        statute_txt = "\n\n".join(
            "- **%s** (%s) %s\n  %s" % (
                h.get("citation") or "", h.get("act_status") or "",
                h.get("section_title") or "",
                (h.get("excerpt") or "").replace("\n", " ")[:500])
            for h in hits)
        statute_txt += ("\n\nSecondary compilation. Cite the statute; keep "
                        "`source_url` as the primary.")
    else:
        statute_txt = ("_No local statute corpus for %s. "
                       "`taxdb statutes fetch %s` then re-emit this packet._"
                       % (j["state_usps"], j["state_usps"]))

    q_txt = ""
    for c in categories:
        q_txt += "\n### %s\n\n%s\n\n" % (c, CATEGORIES.get(c, ""))
        q_txt += "\n".join("%d. %s" % (i + 1, q) for i, q in enumerate(QUESTIONS.get(c, [])))
        q_txt += "\n\nValid `instrument_code` values: %s\n" % ", ".join(
            "`%s`" % x for x in INSTRUMENTS.get(c, []))

    example = json.loads(json.dumps(EXAMPLE))
    example["findings"][0]["geoid"] = geoid

    return PROMPT_HEADER.format(
        name=j["name"], kind=j["kind"], state=j["state_usps"], geoid=geoid,
        pop="{:,}".format(j["population"]) if j["population"] else "unknown",
        cats=", ".join(categories), known=known_txt, profile=profile_txt,
        sources=src_txt, priors=prior_txt, statutes=statute_txt, questions=q_txt,
        rules=RULES_LEAN if lean else RULES_FULL,
        schema=json.dumps(example, indent=2),
    )


FRAMEWORK_HEADER = """\
# State framework packet: {state_name} ({state})

One pass per state. What you record here applies to every county, city, and
township in {state}, so it is worth more per hour than any single rate.

## What is already recorded

{known}

## Sources on file

{sources}

## Statute excerpts on file

{statutes}

## What to find

### 1. Vote thresholds (`thresholds`)

For each way a local government in {state} can ask voters for revenue:

- The share of the vote it takes to pass, as a percentage. Two-thirds is
  `66.67`, never `0.6667`.
- `threshold_basis`: share of what. Votes cast is not the same as registered
  voters, and a dual-majority state is different again. Allowed values:
  {bases}.
- Whether the threshold differs for general-purpose versus special-purpose
  revenue. If it does, record two rows with `purpose_restriction` set.
- Election timing limits: which dates a revenue measure may appear on.
- Any turnout validation requirement (a measure that passes but fails a
  turnout test has still failed).
- Sunset rules: whether a sunset is required, the maximum term, whether it
  may be reimposed, and any cooling-off period after a loss.
- Whether the governing body can act alone, and any petition alternative.
- The statute and, where it exists, the constitutional cite.

Measure classes to cover: {classes}.

### 2. Authority and caps (`grants`)

For each tax category and each kind of jurisdiction (county, place, mcd):

- Is it permitted: `yes`, `no`, or `conditional`. `no` is a finding, not a blank.
- The maximum rate and its unit, if capped.
- Any aggregate cap across overlapping jurisdictions, and the stacking rule.
- Eligibility conditions (population floors, charter status, county consent).
- The statutory cite for each.

### 3. State profile (`profile`)

Home rule doctrine, property tax limit type and a one-paragraph summary,
whether local sales / income / lodging taxes are allowed at all, the maximum
local sales rate, the statute root URL, and the revenue agency of record.

## Rules

{rules}

## Return format

```json
{schema}
```
"""

FRAMEWORK_RULES_FULL = """\
- Every threshold and every cap needs a source URL and a statute cite. A
  threshold with no cite cannot be used in front of a client.
- Thresholds change. When a rule was amended, record the new row with
  `effective_from` set to the amendment date rather than editing history.
- Percentages are percentages: 60 means sixty percent.
- Do not infer one state's rule from a neighbor's. If you cannot find it,
  omit the row and say so in `notes` on the profile.
- Dates must be ISO `YYYY-MM-DD`."""

FRAMEWORK_RULES_LEAN = """\
- Thresholds change. When a rule was amended, record the new row with
  `effective_from` set to the amendment date rather than editing history.
- If you cannot find a rule, omit the row and say so in `notes` on the
  profile."""

FRAMEWORK_EXAMPLE = {
    "schema_version": "1.1",
    "researcher": "your name or model id",
    "profile": {
        "state_usps": "ST",
        "home_rule_doctrine": "home_rule",
        "property_tax_limit_type": "levy_growth_cap",
        "property_tax_limit_summary": "Levy growth capped at 1% annually absent a voted lid lift.",
        "local_sales_tax_allowed": "yes",
        "local_sales_tax_max": 2.5,
        "local_income_tax_allowed": "no",
        "local_lodging_tax_allowed": "yes",
        "statute_root_url": "https://...",
        "revenue_agency": "State Department of Revenue",
        "revenue_agency_url": "https://...",
    },
    "thresholds": [
        {
            "state_usps": "ST",
            "jurisdiction_kind": "place",
            "measure_class": "levy_override",
            "purpose_restriction": "special",
            "threshold_value": 60.0,
            "threshold_basis": "votes_cast_plus_turnout_validation",
            "threshold_note": "60% of votes cast, plus turnout of 40% of the last general.",
            "election_timing": "any of four annual election dates",
            "turnout_requirement": "40% of turnout at the last general election",
            "sunset_required": "yes",
            "sunset_max_years": 6,
            "reimposition_allowed": "yes",
            "cooling_off_months": 0,
            "governing_body_vote": "majority of the council to place it",
            "petition_alternative": "no",
            "statute_cite": "Rev. Code § 00.00.000",
            "constitutional_cite": "Const. art. VII § 2",
            "effective_from": "2007-01-01",
            "source": {
                "url": "https://...",
                "name": "State code, revenue title",
                "source_type": "statute",
                "authority_tier": 1,
            },
            "confidence": "high",
            "source_quote": "sixty percent of the voters voting thereon",
            "notes": "",
        }
    ],
    "grants": [
        {
            "state_usps": "ST",
            "jurisdiction_kind": "county",
            "category": "lodging_meals",
            "instrument_code": "transient_lodging",
            "permitted": "yes",
            "eligibility_note": "Counties over 40,000 population.",
            "max_rate": 4.0,
            "max_rate_unit": "percent",
            "aggregate_cap_note": "County and city combined may not exceed 6%.",
            "stacking_rule": "city rate is credited against the county rate",
            "statute_cite": "Rev. Code § 00.00.111",
            "source": {
                "url": "https://...",
                "name": "State code, lodging tax chapter",
                "source_type": "statute",
                "authority_tier": 1,
            },
            "confidence": "high",
            "source_quote": "shall not exceed four percent",
            "notes": "",
        }
    ],
}

ELECTIONS_HEADER = """\
# Local revenue measure packet: {name} ({state})

GEOID `{geoid}` | population {pop}

Revenue measures put to voters inside this county, including measures of the
cities, towns, school districts, and special districts within it. County
canvasses and official results abstracts are the primary source; the
Secretary of State is second.

## What is already recorded

{known}

## Applicable thresholds on file ({state})

{thresholds}

## Sources on file

{sources}

## What to find

- Every revenue measure on the ballot in the last {years} years: taxes, bonds,
  levy overrides and renewals, parcel taxes and assessments.
- For each: election date, local measure identifier as printed (Issue 7,
  Measure A, Proposition 1), the official title, and the ballot question.
- What it was: `measure_class` from {classes}.
- The rate or amount asked for, with unit, plus any principal amount for bonds
  and the term in years.
- Stated purpose, and whether general or special purpose.
- The result: votes yes, votes no, and total. Record the counts and let the
  percentage be computed. If only a percentage is published, record that.
- Turnout: registered voters and ballots cast, when the canvass shows them.
- Whether it was a renewal, and of what.

## Rules

{rules}

## Return format

```json
{schema}
```
"""

ELECTIONS_RULES_FULL = """\
- Certified or official results only. Never election-night returns.
- Record the counts as printed. Do not round, and do not recompute a
  percentage that the source already prints; if they disagree, note it.
- A measure that lost is as valuable as one that won. Record both.
- `outcome` is `passed` only when the source says it passed. If it cleared a
  majority but failed a supermajority or turnout test, that is `failed`.
- One row per measure. If the same question returned at a later election,
  that is a second row, not an edit.
- Dates must be ISO `YYYY-MM-DD`.
- If you find no measures at all, return an empty `measures` array. That is a
  real answer and it is recorded as a coverage gap, not as a zero."""

ELECTIONS_RULES_LEAN = """\
- A measure that lost is as valuable as one that won. Record both.
- `outcome` is `passed` only when the source says it passed. If it cleared a
  majority but failed a supermajority or turnout test, that is `failed`.
- One row per measure. If the same question returned at a later election,
  that is a second row, not an edit.
- If you find no measures at all, return an empty `measures` array. That is a
  real answer and it is recorded as a coverage gap, not as a zero."""

ELECTIONS_EXAMPLE = {
    "schema_version": "1.1",
    "researcher": "your name or model id",
    "measures": [
        {
            "geoid": "GEOID",
            "election_date": "2024-11-05",
            "election_type": "general",
            "measure_id_local": "Issue 7",
            "official_title": "Additional levy for current expenses",
            "ballot_question": "Shall an additional tax be levied ...",
            "full_text_url": "https://...",
            "measure_class": "levy_override",
            "category": "property",
            "instrument_code": "general_operating_levy",
            "is_renewal": 0,
            "rate_value": 2.5,
            "rate_unit": "mills",
            "duration_years": 5,
            "purpose_type": "special",
            "stated_purpose": "Fire and emergency medical services",
            "annual_revenue_est": 1850000,
            "votes_yes": 12403,
            "votes_no": 9887,
            "votes_total": 22290,
            "registered_voters": 41200,
            "ballots_cast": 23110,
            "outcome": "passed",
            "source": {
                "url": "https://...",
                "name": "County board of elections official canvass",
                "source_type": "agency_table",
                "authority_tier": 2,
            },
            "confidence": "high",
            "notes": "",
        }
    ],
}


def build_framework(conn, usps, lean=False):
    """State-level packet: thresholds, authority caps, and the profile."""
    usps = (usps or "").upper()
    p = conn.execute("SELECT * FROM state_profile WHERE state_usps=?", (usps,)).fetchone()
    state_name = p["state_name"] if p else usps

    known = []
    n_thr = conn.execute("SELECT COUNT(*) c FROM threshold_rule WHERE state_usps=?",
                         (usps,)).fetchone()["c"]
    n_ag = conn.execute("SELECT COUNT(*) c FROM authority_grant WHERE state_usps=?",
                        (usps,)).fetchone()["c"]
    known.append("- %d threshold rule(s) and %d authority grant(s) on file." % (n_thr, n_ag))
    for r in conn.execute(
            "SELECT jurisdiction_kind, measure_class, purpose_restriction, "
            "threshold_value, threshold_basis, statute_cite FROM threshold_rule "
            "WHERE state_usps=? ORDER BY measure_class LIMIT 40", (usps,)):
        known.append("- `%s` / %s%s: %s%% of %s (%s)" % (
            r["measure_class"], r["jurisdiction_kind"] or "any kind",
            " / %s purpose" % r["purpose_restriction"] if r["purpose_restriction"] else "",
            r["threshold_value"], r["threshold_basis"], r["statute_cite"]))
    for r in conn.execute(
            "SELECT jurisdiction_kind, category, instrument_code, permitted, max_rate, "
            "max_rate_unit FROM authority_grant WHERE state_usps=? "
            "ORDER BY category LIMIT 40", (usps,)):
        known.append("- `%s` / `%s` / %s: %s%s" % (
            r["category"], r["instrument_code"], r["jurisdiction_kind"] or "any kind",
            r["permitted"],
            " up to %s %s" % (r["max_rate"], r["max_rate_unit"])
            if r["max_rate"] is not None else ""))
    if p and p["verified_at"]:
        known.append("- Profile last verified %s." % (p["verified_at"] or "")[:10])
    else:
        known.append("- Profile has never been researched.")

    srcs = conn.execute(
        "SELECT name, url, authority_tier FROM source WHERE scope_geoid IN "
        "(SELECT geoid FROM jurisdiction WHERE kind='state' AND state_usps=?) "
        "ORDER BY authority_tier LIMIT 30", (usps,)).fetchall()
    src_txt = "\n".join("- [tier %d] %s -- %s" % (s["authority_tier"], s["name"], s["url"])
                        for s in srcs) or "_None on file._"

    try:
        from . import statutes
        hits = statutes.for_packet(conn, usps, [FRAMEWORK])
    except Exception:
        hits = []
    statute_txt = "\n\n".join(
        "- **%s** %s\n  %s" % (
            h.get("citation") or "", h.get("section_title") or "",
            (h.get("excerpt") or "").replace("\n", " ")[:500])
        for h in hits) or ("_No statute corpus for %s. `taxdb statutes fetch %s`._"
                           % (usps, usps))

    example = json.loads(json.dumps(FRAMEWORK_EXAMPLE))
    example["profile"]["state_usps"] = usps
    for row in example["thresholds"]:
        row["state_usps"] = usps
    for row in example["grants"]:
        row["state_usps"] = usps

    return FRAMEWORK_HEADER.format(
        state=usps, state_name=state_name, known="\n".join(known),
        sources=src_txt, statutes=statute_txt,
        bases=", ".join("`%s`" % b for b in sorted(THRESHOLD_BASES)),
        classes=", ".join("`%s`" % c for c in sorted(REVENUE_MEASURE_CLASSES)),
        rules=FRAMEWORK_RULES_LEAN if lean else FRAMEWORK_RULES_FULL,
        schema=json.dumps(example, indent=2),
    )


MEASURE_LOOKBACK_YEARS = 12


def build_elections(conn, geoid, lean=False):
    """County-level packet: revenue measures and their certified results."""
    j = conn.execute("SELECT * FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
    if not j:
        raise SystemExit("no jurisdiction %r" % geoid)

    known = conn.execute(
        "SELECT election_date, measure_id_local, measure_class, outcome, pct_yes "
        "FROM ballot_measure WHERE geoid=? AND superseded_by IS NULL "
        "ORDER BY election_date DESC LIMIT 40", (geoid,)).fetchall()
    known_txt = "\n".join(
        "- %s `%s` %s: %s%s" % (
            r["election_date"], r["measure_id_local"] or "?", r["measure_class"],
            r["outcome"],
            " at %s%%" % r["pct_yes"] if r["pct_yes"] is not None else "")
        for r in known) or "_No measures recorded for this county yet._"

    thr = conn.execute(
        "SELECT measure_class, jurisdiction_kind, purpose_restriction, threshold_value, "
        "threshold_basis FROM v_live_threshold WHERE state_usps=? "
        "ORDER BY measure_class LIMIT 40", (j["state_usps"],)).fetchall()
    thr_txt = "\n".join(
        "- `%s` / %s%s: %s%% of %s" % (
            r["measure_class"], r["jurisdiction_kind"] or "any kind",
            " / %s purpose" % r["purpose_restriction"] if r["purpose_restriction"] else "",
            r["threshold_value"], r["threshold_basis"])
        for r in thr) or ("_No thresholds recorded for %s yet. Run that state's "
                          "framework pass first; the margin against threshold cannot "
                          "be computed without it._" % j["state_usps"])

    srcs = conn.execute(
        "SELECT name, url, authority_tier FROM source WHERE scope_geoid IN (?,?) "
        "ORDER BY authority_tier LIMIT 20", (geoid, j["state_fips"])).fetchall()
    src_txt = "\n".join("- [tier %d] %s -- %s" % (s["authority_tier"], s["name"], s["url"])
                        for s in srcs) or "_None on file. Start from the county elections office._"

    example = json.loads(json.dumps(ELECTIONS_EXAMPLE))
    example["measures"][0]["geoid"] = geoid

    return ELECTIONS_HEADER.format(
        name=j["name"], state=j["state_usps"], geoid=geoid,
        pop="{:,}".format(j["population"]) if j["population"] else "unknown",
        known=known_txt, thresholds=thr_txt, sources=src_txt,
        years=MEASURE_LOOKBACK_YEARS,
        classes=", ".join("`%s`" % c for c in sorted(MEASURE_CLASSES)),
        rules=ELECTIONS_RULES_LEAN if lean else ELECTIONS_RULES_FULL,
        schema=json.dumps(example, indent=2),
    )


def write_batch(conn, rows, outdir):
    """Write one packet file per jurisdiction in a claimed batch.

    The two passes get their own files: they have their own packet shapes,
    and folding them into the geoid's tax packet emitted the wrong brief.
    """
    os.makedirs(outdir, exist_ok=True)
    by_geoid = {}
    passes = []
    for r in rows:
        if r["category"] in (FRAMEWORK, ELECTIONS):
            passes.append((r["geoid"], r["category"]))
        else:
            by_geoid.setdefault(r["geoid"], []).append(r["category"])
    paths = []
    for geoid, cats in by_geoid.items():
        path = os.path.join(outdir, "%s.md" % geoid)
        with open(path, "w") as fh:
            fh.write(build(conn, geoid, cats))
        paths.append(path)
    for geoid, cat in passes:
        path = os.path.join(outdir, "%s-%s.md" % (geoid, cat))
        with open(path, "w") as fh:
            fh.write(build(conn, geoid, [cat]))
        paths.append(path)
    return paths
