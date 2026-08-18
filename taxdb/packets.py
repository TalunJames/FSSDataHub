"""Research packets.

A packet is a self-contained brief for one jurisdiction: what is already
known, which state framework applies, which sources are on file, and the
exact JSON shape the answer must come back in. It is what makes the work
delegable -- to a subagent, a contractor, or yourself three weeks from now.
"""

import json
import os

from . import db
from .vocab import CATEGORIES, INSTRUMENTS

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
  checks that the quote appears in the archived bytes.

## Return format

Write a JSON file matching this shape and load it with `taxdb ingest FILE`:

```json
{schema}
```
"""

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


def build(conn, geoid, categories=None):
    j = conn.execute("SELECT * FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
    if not j:
        raise SystemExit("no jurisdiction %r" % geoid)

    if categories is None:
        rows = conn.execute(
            "SELECT category FROM work_item WHERE geoid=? AND status IN "
            "('pending','in_progress','needs_review')", (geoid,)).fetchall()
        categories = [r["category"] for r in rows] or list(CATEGORIES)

    known = conn.execute(
        "SELECT category, instrument_code, status, rate_value, rate_unit, retrieved_at "
        "FROM tax_instrument WHERE geoid=? AND superseded_by IS NULL "
        "ORDER BY category", (geoid,)).fetchall()
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

    srcs = conn.execute(
        "SELECT name, url, authority_tier FROM source WHERE scope_geoid IN (?,?,?) "
        "ORDER BY authority_tier", (geoid, j["state_fips"], j["county_fips"])).fetchall()
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
        schema=json.dumps(example, indent=2),
    )


def write_batch(conn, rows, outdir):
    """Write one packet file per jurisdiction in a claimed batch."""
    os.makedirs(outdir, exist_ok=True)
    by_geoid = {}
    for r in rows:
        by_geoid.setdefault(r["geoid"], []).append(r["category"])
    paths = []
    for geoid, cats in by_geoid.items():
        path = os.path.join(outdir, "%s.md" % geoid)
        with open(path, "w") as fh:
            fh.write(build(conn, geoid, cats))
        paths.append(path)
    return paths
