"""Gap-driven interview: only ask what the crawler and extractor left open."""

import json

from taxdb import db, ingest, ledger
from taxdb.sources import STATE_AGENCIES
from taxdb.vocab import CATEGORIES, INSTRUMENTS, RATE_UNITS, STATUSES

LABELS = {
    "general_operating_levy": "General operating levy / millage",
    "debt_service_levy": "Debt service levy",
    "special_district_levy": "Special district levy",
    "tif_increment": "TIF increment",
    "assessment_ratio": "Assessment ratio",
    "homestead_exemption": "Homestead exemption",
    "rate_cap": "Property rate cap",
    "levy_growth_limit": "Levy growth limit",
    "assessment_growth_limit": "Assessment growth limit",
    "local_option_sales": "Local-option sales tax",
    "county_general_sales": "County general sales tax",
    "municipal_general_sales": "Municipal general sales tax",
    "special_purpose_sales": "Special-purpose / district sales tax",
    "transit_district_sales": "Transit district sales tax",
    "use_tax": "Local use tax",
    "gross_receipts": "Gross receipts tax",
    "sales_tax_cap": "Local sales-tax cap",
    "local_income_tax": "Local income tax",
    "earnings_tax": "Earnings tax",
    "occupational_license": "Occupational license tax",
    "payroll_expense": "Payroll expense tax",
    "net_profits": "Net profits tax",
    "intangibles": "Intangibles tax",
    "transient_lodging": "Transient lodging / hotel tax",
    "hotel_motel": "Hotel/motel tax (if separate)",
    "food_beverage": "Food and beverage tax",
    "tourism_district": "Tourism district tax",
    "franchise_fee": "Franchise fee",
    "utility_users": "Utility users tax",
    "business_license": "Business license tax",
    "impact_fee": "Impact fee",
    "special_assessment": "Special assessment",
    "real_estate_transfer": "Real estate transfer tax",
    "local_motor_fuel": "Local motor fuel tax",
    "amusement_admissions": "Amusement / admissions tax",
    "severance": "Severance tax",
}

PROMPTS = {
    "general_operating_levy": "What is the current general operating millage or levy rate, with its unit and tax year?",
    "debt_service_levy": "Is there a separate debt-service levy? If yes, rate and unit.",
    "rate_cap": "What rate cap (if any) binds this jurisdiction, with the statutory cite?",
    "levy_growth_limit": "What levy growth limit applies here, if any?",
    "assessment_ratio": "What assessment ratio(s) apply, by class if they differ?",
    "county_general_sales": "What sales tax rate does this county itself impose (not the combined rate shoppers pay)?",
    "municipal_general_sales": "What sales tax rate does this city/village itself impose (not the combined rate)?",
    "local_option_sales": "What local-option sales tax rate is imposed here, if any?",
    "sales_tax_cap": "What is the statutory maximum local sales rate, and is there headroom?",
    "use_tax": "Is a local use tax imposed?",
    "special_purpose_sales": "Any special-purpose or transit sales tax on top of the general local rate?",
    "local_income_tax": "Is there a local income, earnings, or occupational tax? Rate?",
    "earnings_tax": "Earnings-tax rate, if this place uses that form instead of a general income tax.",
    "occupational_license": "Occupational license tax: imposed, prohibited, or unused?",
    "transient_lodging": "Transient lodging / hotel tax rate, and what it funds.",
    "food_beverage": "Food and beverage tax, if any.",
    "franchise_fee": "Utility franchise fees: imposed?",
    "utility_users": "Utility users tax?",
    "real_estate_transfer": "Real estate transfer tax?",
}

DEFAULT_UNITS = {
    "property": "mills",
    "sales_use": "percent",
    "income_payroll": "percent",
    "lodging_meals": "percent",
    "other_levy": "percent",
}


def primary_codes(kind, category):
    """Instruments worth asking first, given jurisdiction kind."""
    codes = list(INSTRUMENTS.get(category, []))
    if category == "sales_use":
        if kind == "county":
            order = ["county_general_sales", "local_option_sales", "use_tax",
                     "special_purpose_sales", "sales_tax_cap"]
        elif kind == "place":
            order = ["municipal_general_sales", "local_option_sales", "use_tax",
                     "special_purpose_sales", "sales_tax_cap"]
        else:
            order = ["local_option_sales", "sales_tax_cap"]
        return [c for c in order if c in codes]
    if category == "property":
        return ["general_operating_levy", "debt_service_levy", "rate_cap",
                "levy_growth_limit", "assessment_ratio"]
    if category == "income_payroll":
        return ["local_income_tax", "earnings_tax", "occupational_license"]
    if category == "lodging_meals":
        return ["transient_lodging", "food_beverage"]
    if category == "other_levy":
        return ["franchise_fee", "utility_users", "real_estate_transfer",
                "business_license"]
    return codes[:5]


def _row_complete(row):
    if not row:
        return False
    status = row["status"]
    if status == "unknown":
        return False
    if status == "levied":
        code = row["instrument_code"]
        if row["rate_value"] is None and code not in ("assessment_ratio", "homestead_exemption"):
            return False
        return True
    return status in ("authorized_not_levied", "prohibited", "repealed")


def _why(row, work, pages, extract_row):
    if extract_row and not extract_row["parsed_ok"]:
        crawl_note = "The extractor could not parse a finding from crawled pages."
    elif work and work["last_error"]:
        crawl_note = work["last_error"]
    elif pages == 0 and work:
        crawl_note = "The crawler has not archived a usable page yet."
    else:
        crawl_note = None

    if row is None:
        if crawl_note:
            return "Nothing on file. " + crawl_note
        return "Nothing recorded yet for this instrument."
    if row["status"] == "unknown":
        extra = (row["notes"] or "").strip()
        return "On file as unknown" + ((" — " + extra) if extra else ".")
    if row["status"] == "levied" and row["rate_value"] is None:
        return "Marked levied, but no rate was captured."
    if row["confidence"] == "low":
        return "A low-confidence figure is on file — confirm or replace it."
    return None


def citations(conn, geoid, state_usps=None):
    """URLs the operator can cite without retyping."""
    out, seen = [], set()

    def add(url, label, origin):
        if not url or url in seen:
            return
        seen.add(url)
        out.append({"url": url, "label": label or url, "origin": origin})

    for r in conn.execute(
            "SELECT url, filename, kind FROM intake_item WHERE geoid=? "
            "ORDER BY id DESC LIMIT 8", (geoid,)):
        add(r["url"] or ("intake:" + (r["filename"] or str(r["kind"]))),
            r["filename"] or r["kind"], "intake")
    for r in conn.execute(
            "SELECT url, title FROM crawl_page WHERE geoid=? AND url NOT LIKE 'file:%' "
            "ORDER BY id DESC LIMIT 8", (geoid,)):
        add(r["url"], r["title"], "crawl")
    for r in conn.execute(
            "SELECT url, name FROM source WHERE scope_geoid=? ORDER BY authority_tier LIMIT 6",
            (geoid,)):
        add(r["url"], r["name"], "catalog")
    if state_usps and state_usps in STATE_AGENCIES:
        name, url = STATE_AGENCIES[state_usps]
        add(url, name, "agency")
    return out


def session(conn, geoid, category):
    """Build the interview payload for one jurisdiction × category."""
    j = conn.execute("SELECT * FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
    if not j:
        return None
    work = conn.execute(
        "SELECT * FROM work_item WHERE geoid=? AND category=?",
        (geoid, category)).fetchone()
    known = conn.execute(
        "SELECT t.*, s.url AS source_url FROM tax_instrument t "
        "JOIN source s ON s.id=t.source_id "
        "WHERE t.geoid=? AND t.category=? AND t.superseded_by IS NULL "
        "ORDER BY t.instrument_code", (geoid, category)).fetchall()
    by_code = {r["instrument_code"]: r for r in known}
    pages = conn.execute(
        "SELECT COUNT(*) c FROM crawl_page WHERE geoid=?", (geoid,)).fetchone()["c"]
    extract_row = conn.execute(
        "SELECT parsed_ok, error, created_at FROM crawl_extract "
        "WHERE geoid=? AND category=? ORDER BY id DESC LIMIT 1",
        (geoid, category)).fetchone()
    skipped = {
        r["question_id"] for r in conn.execute(
            "SELECT question_id FROM interview_answer WHERE geoid=? AND category=? "
            "AND action IN ('skipped','skip_rest','answered','unknown') "
            "AND created_at >= datetime('now','-12 hours')",
            (geoid, category))
    }

    questions = []
    for code in primary_codes(j["kind"], category):
        qid = "%s.%s" % (category, code)
        if qid in skipped:
            continue
        row = by_code.get(code)
        if _row_complete(row) and not (row and row["confidence"] == "low"):
            continue
        why = _why(row, work, pages, extract_row)
        if not why:
            continue
        questions.append(_instrument_question(category, code, why, row, j["kind"]))

    cites = citations(conn, geoid, j["state_usps"])
    remaining = len(questions)
    return {
        "geoid": geoid,
        "category": category,
        "jurisdiction": {
            "name": j["name"],
            "kind": j["kind"],
            "state": j["state_usps"],
            "population": j["population"],
        },
        "work_status": work["status"] if work else None,
        "last_error": (work["last_error"] if work else None),
        "pages_archived": pages,
        "extract_error": (extract_row["error"] if extract_row else None),
        "known": [_known_brief(r) for r in known],
        "citations": cites,
        "question": questions[0] if questions else None,
        "remaining": remaining,
        "default_unit": DEFAULT_UNITS.get(category, "percent"),
        "rate_units": sorted(RATE_UNITS),
        "statuses": sorted(STATUSES),
        "category_label": CATEGORIES.get(category, category),
    }


def _known_brief(r):
    rate = None
    if r["rate_value"] is not None:
        rate = "%s %s" % (r["rate_value"], r["rate_unit"] or "")
    return {
        "instrument_code": r["instrument_code"],
        "label": LABELS.get(r["instrument_code"], r["instrument_code"]),
        "status": r["status"],
        "rate": rate,
        "confidence": r["confidence"],
        "source_url": r["source_url"],
    }


def _instrument_question(category, code, why, row, kind):
    known = None
    if row:
        known = {
            "status": row["status"],
            "rate_value": row["rate_value"],
            "rate_unit": row["rate_unit"],
            "fiscal_year": row["fiscal_year"],
            "statute_cite": row["statute_cite"],
            "notes": row["notes"],
            "source_url": row["source_url"] if "source_url" in row.keys() else None,
        }
    return {
        "id": "%s.%s" % (category, code),
        "kind": "instrument",
        "instrument_code": code,
        "category": category,
        "title": LABELS.get(code, code.replace("_", " ")),
        "prompt": PROMPTS.get(code) or ("What is the status and rate of %s?" % code.replace("_", " ")),
        "why": why,
        "known": known,
        "fields": ["status", "rate_value", "rate_unit", "fiscal_year",
                   "statute_cite", "source_url", "notes"],
    }


def record_action(conn, geoid, category, question_id, action, payload=None):
    conn.execute(
        "INSERT INTO interview_answer (geoid, category, question_id, action, payload, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (geoid, category, question_id, action,
         json.dumps(payload, default=str) if payload else None, db.now()))
    conn.commit()


def apply_answer(conn, geoid, category, question, action, payload, researcher="manual"):
    """Write a finding, or record a skip. Returns ingest result or skip info."""
    qid = question["id"]
    if action in ("skipped", "skip_rest"):
        record_action(conn, geoid, category, qid, action, payload)
        if action == "skip_rest":
            for code in primary_codes(
                    conn.execute("SELECT kind FROM jurisdiction WHERE geoid=?",
                                 (geoid,)).fetchone()["kind"],
                    category):
                oid = "%s.%s" % (category, code)
                if oid != qid:
                    record_action(conn, geoid, category, oid, "skip_rest")
        return {"written": 0, "action": action}

    payload = payload or {}
    status = payload.get("status") or ("unknown" if action == "unknown" else None)
    if action == "unknown":
        status = "unknown"
    if status not in STATUSES:
        return {"written": 0, "errors": ["status %r is not valid" % status], "action": action}

    source_url = (payload.get("source_url") or "").strip()
    if not source_url:
        source_url = _fallback_source(conn, geoid)
    finding = {
        "geoid": geoid,
        "category": category,
        "instrument_code": question["instrument_code"],
        "label": question.get("title"),
        "status": status,
        "rate_value": _num(payload.get("rate_value")),
        "rate_unit": payload.get("rate_unit") or None,
        "fiscal_year": _int(payload.get("fiscal_year")),
        "statute_cite": payload.get("statute_cite") or None,
        "confidence": "high" if action == "answered" and status != "unknown" else "low",
        "extraction_method": "manual",
        "notes": payload.get("notes") or None,
        "source": {
            "url": source_url,
            "name": payload.get("source_name") or "Manual interview",
            "source_type": payload.get("source_type") or "portal",
            "authority_tier": int(payload.get("authority_tier") or 4),
        },
    }
    if status != "levied":
        finding["rate_value"] = finding["rate_value"]  # keep if they filled it
    doc = {
        "schema_version": "1.0",
        "researcher": researcher,
        "extraction_method": "manual",
        "findings": [finding],
    }
    res = ingest.load_doc(conn, doc, allow_partial=True, label="interview:%s" % qid)
    record_action(conn, geoid, category, qid, action, finding)
    if res.get("written") and status != "unknown":
        # Stay in needs_review until they finish or accept on the review page.
        ledger.set_status(conn, geoid, category, "needs_review")
        conn.commit()
    return {"written": res.get("written", 0), "errors": res.get("errors") or [],
            "action": action, "finding": finding}


def _fallback_source(conn, geoid):
    cites = citations(conn, geoid)
    if cites:
        return cites[0]["url"]
    j = conn.execute("SELECT state_usps FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
    if j and j["state_usps"] in STATE_AGENCIES:
        return STATE_AGENCIES[j["state_usps"]][1]
    return "manual:interview"


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def inbox(conn, limit=40):
    """Work items that still need a human, plus empty gaps after a crawl."""
    rows = conn.execute(
        "SELECT w.geoid, w.category, w.status, w.last_error, w.priority, "
        "j.name, j.state_usps, j.kind, j.population "
        "FROM work_item w JOIN jurisdiction j ON j.geoid=w.geoid "
        "WHERE w.status IN ('needs_review','blocked') "
        "OR (w.last_error IS NOT NULL AND w.last_error != '') "
        "ORDER BY w.priority DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
