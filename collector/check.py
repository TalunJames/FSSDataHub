"""Common-sense second checker.

After the extractor writes findings for a (jurisdiction, category), this
pass decides whether a human needs to look at them. Two layers:

1. Deterministic checks — free, run always: the quoted source text must
   actually appear in the crawled documents, rates must be plausible for
   their unit, statuses must be internally consistent, and low-confidence
   rows always go to a human.
2. AI cross-check — a second model call with a skeptic's prompt: does the
   quote support the number, is this the jurisdiction's own local rate
   (not a state or combined rate), does the unit make sense.

Everything that passes both is marked complete with no human review.
Anything flagged lands on the Review page with the reasons attached.
If the checker itself cannot run (API error), the item is flagged rather
than silently passed — fail toward review, never toward trust.
"""

import json
import re

from taxdb import db, ledger

from . import extract, store

# Plausible ranges by rate unit. Outside these, a human looks first.
# Wide on purpose: they catch unit mixups (mills recorded as percent),
# not policy judgments.
PLAUSIBLE = {
    "percent": (0.0, 25.0),
    "mills": (0.0, 500.0),
    "dollars_per_1000_av": (0.0, 100.0),
    "ratio": (0.0, 1.5),
    "usd_flat": (0.0, 1000000.0),
    "usd_per_unit": (0.0, 100000.0),
}

CHECK_SYSTEM = """You are a skeptical reviewer for a US local-tax database.
You are the second set of eyes on findings another model extracted from
official documents. Your job is to catch mistakes, not to be agreeable.

Flag a finding when any of these fail:
- The source_quote does not support the recorded value, status, or unit.
- The rate looks like a state, combined, or neighboring jurisdiction's rate
  rather than this jurisdiction's own local rate.
- The unit looks wrong for the instrument (mills vs percent mixups).
- The status contradicts the evidence (e.g. "levied" where the document
  says the tax was repealed or merely authorized).
- The magnitude fails common sense for this kind of tax.
- The documents are about a different place than the jurisdiction named.

Pass a finding only when it is defensible as-is. When uncertain, flag.
Respond with ONLY valid JSON:
{"verdicts":[{"instrument_code":"...","verdict":"pass"|"flag","reason":"short reason, empty when pass"}]}
Include one verdict per finding you were given.
"""


def live_rows(conn, geoid, category):
    return conn.execute(
        "SELECT t.id, t.instrument_code, t.label, t.status, t.rate_value, "
        "t.rate_unit, t.cap_value, t.cap_unit, t.confidence, t.source_quote, "
        "t.notes, t.effective_date, s.url "
        "FROM tax_instrument t LEFT JOIN source s ON s.id=t.source_id "
        "WHERE t.geoid=? AND t.category=? AND t.superseded_by IS NULL",
        (geoid, category)).fetchall()


def _norm(text):
    """Whitespace- and case-insensitive containment form."""
    return re.sub(r"[^a-z0-9.%$]+", " ", (text or "").lower()).strip()


def deterministic_flags(rows, doc_text):
    flags = []
    norm_doc = _norm(doc_text) if doc_text else ""

    def flag(code, instrument, reason):
        flags.append({"code": code, "instrument_code": instrument, "reason": reason})

    for r in rows:
        inst = r["instrument_code"]
        quote = (r["source_quote"] or "").strip()
        if not quote:
            flag("no_quote", inst, "no source_quote to verify against")
        elif norm_doc and _norm(quote) not in norm_doc:
            flag("quote_missing", inst,
                 "source_quote not found in the crawled text")
        rate, unit = r["rate_value"], r["rate_unit"]
        if rate is not None and unit in PLAUSIBLE:
            lo, hi = PLAUSIBLE[unit]
            if not (lo <= rate <= hi):
                flag("implausible_rate", inst,
                     "%s %s is outside the plausible range for that unit"
                     % (rate, unit))
        if r["status"] == "prohibited" and rate is not None:
            flag("status_conflict", inst,
                 "status is 'prohibited' but a rate_value is recorded")
        if (r["cap_value"] is not None and rate is not None
                and r["cap_unit"] == unit and rate > r["cap_value"]):
            flag("over_cap", inst, "rate is above its own recorded cap")
        if r["confidence"] == "low":
            flag("low_confidence", inst,
                 "extractor marked this low confidence")
    return flags


def ai_flags(settings, jurisdiction, category, rows, doc_text, images=None):
    """Returns (flags, error). error is set when the checker call failed."""
    max_chars = store.as_int(settings.get("checker_max_chars"), 40000)
    findings = [{
        "instrument_code": r["instrument_code"],
        "label": r["label"],
        "status": r["status"],
        "rate_value": r["rate_value"],
        "rate_unit": r["rate_unit"],
        "confidence": r["confidence"],
        "source_quote": r["source_quote"],
        "source_url": r["url"],
        "effective_date": r["effective_date"],
        "notes": r["notes"],
    } for r in rows]
    prompt = (
        "## Jurisdiction\n%s\n\n## Category\n%s\n\n"
        "## Findings to verify\n```json\n%s\n```\n\n"
        "## Source documents (truncated)\n%s\n\n"
        "Respond with JSON only."
        % (jurisdiction, category,
           json.dumps(findings, indent=1),
           (doc_text or "_none — rely on the quotes and common sense_")[:max_chars]))
    model = (settings.get("checker_model") or "").strip() or None
    raw, err = extract.chat(settings, prompt, system=CHECK_SYSTEM,
                            images=images, model=model)
    if err:
        return [], err
    try:
        doc = extract.parse_json_payload(raw)
    except extract.ExtractError as exc:
        return [], "checker returned unparseable output: %s" % exc
    out = []
    for v in doc.get("verdicts") or []:
        if (v.get("verdict") or "").strip().lower() == "flag":
            out.append({
                "code": "ai_flag",
                "instrument_code": v.get("instrument_code"),
                "reason": (v.get("reason") or "checker flagged")[:300],
            })
    return out, None


def checker_model_name(settings):
    return ((settings.get("checker_model") or "").strip()
            or extract.default_model(settings))


def record(conn, run_id, geoid, category, verdict, flags, settings):
    conn.execute(
        "INSERT INTO check_result (run_id, geoid, category, verdict, flags, "
        "provider, model, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, geoid, category, verdict,
         json.dumps(flags) if flags else None,
         settings.get("provider"), checker_model_name(settings), db.now()))


def summarize(flags, limit=3):
    parts = []
    for f in flags[:limit]:
        parts.append("%s: %s" % (f.get("instrument_code") or "?", f.get("reason")))
    more = len(flags) - limit
    if more > 0:
        parts.append("+%d more" % more)
    return "checker flagged — " + "; ".join(parts)


def run_and_apply(conn, settings, run_id, geoid, category, doc_text,
                  images=None, jurisdiction=None):
    """Check the live findings for one work item and set its status.

    Returns (verdict, message). verdict is 'pass', 'flag', 'error', or
    'off' when the checker is disabled (status is then left at
    needs_review, the pre-checker behavior).
    """
    if not store.as_bool(settings.get("checker_enabled")):
        return "off", "checker disabled — human review required"

    rows = live_rows(conn, geoid, category)
    if not rows:
        return "off", "nothing to check"

    if jurisdiction is None:
        j = conn.execute("SELECT name, state_usps, kind, population "
                         "FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
        jurisdiction = ("%s, %s (%s, pop %s, geoid %s)" %
                        (j["name"], j["state_usps"], j["kind"],
                         j["population"], geoid)) if j else geoid

    flags = deterministic_flags(rows, doc_text)
    err = None
    if (settings.get("provider") or "none") != "none":
        more, err = ai_flags(settings, jurisdiction, category, rows,
                             doc_text, images=images)
        flags.extend(more)
    else:
        err = "no AI provider for the checker"

    if err:
        verdict = "error"
        message = "second check unavailable (%s) — review by hand" % err[:200]
        ledger.set_status(conn, geoid, category, "needs_review", error=message)
    elif flags:
        verdict = "flag"
        message = summarize(flags)
        ledger.set_status(conn, geoid, category, "needs_review", error=message[:500])
    else:
        verdict = "pass"
        message = "second check passed — auto-verified"
        ledger.set_status(conn, geoid, category, "complete", error=None)

    record(conn, run_id, geoid, category, verdict, flags, settings)
    conn.commit()
    return verdict, message
