"""Common-sense second checker.

After the extractor writes findings for a (jurisdiction, category), this
pass decides whether a human needs to look at them. Two layers:

1. Deterministic checks — free, run always. These are sorted into two
   kinds, and the distinction is the whole point of the pass:

   *Hard* flags are internal contradictions no amount of judgement can
   explain away: a rate outside any plausible range for its unit, a
   'prohibited' status carrying a rate, a rate above its own cap, a
   threshold recorded as a fraction, a result dated in the future. These
   go straight to a human and skip the model call.

   *Soft* flags are missing or weak optional metadata: no source quote, a
   quote the text search could not locate, a tier-4 source, no threshold
   on file yet. On their own these are not defects — a bulk rate file has
   no prose to quote, and PDF text extraction mangles whitespace often
   enough that exact quote matching produces more noise than signal.

2. AI cross-check — a second model call with a skeptic's prompt, which is
   handed the soft flags and asked to judge them. Does the quote support
   the number, is this the jurisdiction's own local rate (not a state or
   combined rate), does the unit make sense, and does any soft concern
   actually matter here.

Soft flags alone never queue an item for a human: the model decides. That
is what keeps the review page a list of real judgement calls rather than a
list of rows whose optional fields were empty.

Everything that passes is marked complete with no human review. Anything
with a hard flag, or that the model flags, lands on the Review page with
the reasons attached. If the checker itself cannot run (API error), the
item is flagged rather than silently passed — fail toward review, never
toward trust.
"""

import json
import re

from taxdb import db, ledger
from taxdb.vocab import ELECTIONS, FRAMEWORK

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


MEASURE_SYSTEM = CHECK_SYSTEM_MEASURE = """You are a skeptical reviewer for a
US local ballot-measure database. Another model read official election
documents and recorded these measures. Catch mistakes, do not be agreeable.

Flag a measure when any of these fail:
- The vote counts or percentage do not match the source documents.
- The result is recorded for the wrong jurisdiction, or for a statewide
  measure rather than a local one.
- measure_class is wrong (a bond recorded as a tax increase, a renewal
  recorded as a new tax).
- The outcome contradicts the numbers or the source's own statement.
- The rate or principal amount does not match the ballot question.
- The election date does not match the document.
- The numbers look like unofficial election-night returns rather than a
  certified canvass.

Pass a measure only when it is defensible as-is. When uncertain, flag.
Respond with ONLY valid JSON:
{"verdicts":[{"instrument_code":"<measure_id_local or election_date>","verdict":"pass"|"flag","reason":"short reason, empty when pass"}]}
Include one verdict per measure you were given.
"""

FRAMEWORK_SYSTEM = """You are a skeptical reviewer for a US local-tax
statutory database. Another model read state code and recorded vote
thresholds and authority caps. Catch mistakes, do not be agreeable.

Flag a row when any of these fail:
- The threshold percentage does not match the statute quoted (two-thirds is
  66.67; three-fifths is 60).
- The basis is wrong: a share of votes cast is not a share of registered
  voters, and a dual-majority requirement is neither.
- The cap or maximum rate is a statewide rate rather than the local ceiling,
  or combines state and local.
- The rule is attributed to the wrong kind of jurisdiction.
- The cite does not support the rule, or is from another state.
- The rule was superseded by a later amendment or court decision that the
  document itself mentions.

Pass a row only when it is defensible as-is. When uncertain, flag.
Respond with ONLY valid JSON:
{"verdicts":[{"instrument_code":"<measure_class or instrument_code>","verdict":"pass"|"flag","reason":"short reason, empty when pass"}]}
Include one verdict per row you were given.
"""


def live_rows(conn, geoid, category):
    return conn.execute(
        "SELECT t.id, t.instrument_code, t.label, t.status, t.rate_value, "
        "t.rate_unit, t.cap_value, t.cap_unit, t.confidence, t.source_quote, "
        "t.notes, t.effective_date, t.extraction_method, s.url "
        "FROM tax_instrument t LEFT JOIN source s ON s.id=t.source_id "
        "WHERE t.geoid=? AND t.category=? AND t.superseded_by IS NULL",
        (geoid, category)).fetchall()


def _norm(text):
    """Whitespace- and case-insensitive containment form."""
    return re.sub(r"[^a-z0-9.%$]+", " ", (text or "").lower()).strip()


def _field(row, key, default=None):
    """Read a column that may not be in every caller's SELECT list."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def _flagger(flags):
    """Collect flags, each tagged hard (a contradiction) or soft (a concern)."""
    def flag(code, label, reason, hard=False):
        flags.append({"code": code, "instrument_code": label, "reason": reason,
                      "hard": bool(hard)})
    return flag


def hard_only(flags):
    return [f for f in flags if f.get("hard")]


def soft_only(flags):
    return [f for f in flags if not f.get("hard")]


def deterministic_flags(rows, doc_text):
    flags = []
    flag = _flagger(flags)
    norm_doc = _norm(doc_text) if doc_text else ""

    for r in rows:
        inst = r["instrument_code"]
        # A bulk rate file is a spreadsheet, not prose. There is nothing to
        # quote, and demanding a quote of it queues every adapter-filled row
        # for a human who has nothing to add.
        from_bulk = _field(r, "extraction_method") == "bulk_import"
        quote = (r["source_quote"] or "").strip()
        if not quote:
            if not from_bulk:
                flag("no_quote", inst, "no source_quote to verify against")
        elif norm_doc and _norm(quote) not in norm_doc:
            # Soft: PDF text extraction mangles whitespace, ligatures and
            # hyphenation often enough that an exact miss is weak evidence.
            flag("quote_missing", inst,
                 "source_quote not found in the crawled text")
        rate, unit = r["rate_value"], r["rate_unit"]
        if rate is not None and unit in PLAUSIBLE:
            lo, hi = PLAUSIBLE[unit]
            if not (lo <= rate <= hi):
                flag("implausible_rate", inst,
                     "%s %s is outside the plausible range for that unit"
                     % (rate, unit), hard=True)
        if r["status"] == "prohibited" and rate is not None:
            flag("status_conflict", inst,
                 "status is 'prohibited' but a rate_value is recorded", hard=True)
        if (r["cap_value"] is not None and rate is not None
                and r["cap_unit"] == unit and rate > r["cap_value"]):
            flag("over_cap", inst, "rate is above its own recorded cap", hard=True)
        if r["confidence"] == "low":
            flag("low_confidence", inst,
                 "extractor marked this low confidence")
    return flags


def live_measures(conn, geoid, limit=40):
    return conn.execute(
        "SELECT b.id, b.measure_id_local, b.election_date, b.election_type, "
        "b.measure_class, b.category, b.instrument_code, b.outcome, b.rate_value, "
        "b.rate_unit, b.principal_amount, b.votes_yes, b.votes_no, b.votes_total, "
        "b.pct_yes, b.threshold_required, b.threshold_basis, b.margin_vs_threshold, "
        "b.stated_purpose, b.confidence, b.notes, s.url, s.authority_tier "
        "FROM ballot_measure b LEFT JOIN source s ON s.id=b.source_id "
        "WHERE b.geoid=? AND b.superseded_by IS NULL "
        "ORDER BY b.election_date DESC LIMIT ?", (geoid, limit)).fetchall()


def live_framework(conn, usps, limit=80):
    """Thresholds and grants for one state, as one checkable list."""
    thr = conn.execute(
        "SELECT t.id, t.measure_class AS label, t.jurisdiction_kind, "
        "t.purpose_restriction, t.threshold_value, t.threshold_basis, "
        "t.statute_cite, t.confidence, t.notes, s.url, s.authority_tier "
        "FROM threshold_rule t LEFT JOIN source s ON s.id=t.source_id "
        "WHERE t.state_usps=? ORDER BY t.id DESC LIMIT ?", (usps, limit)).fetchall()
    ag = conn.execute(
        "SELECT g.id, g.instrument_code AS label, g.jurisdiction_kind, g.category, "
        "g.permitted, g.max_rate, g.max_rate_unit, g.statute_cite, g.confidence, "
        "g.notes, s.url, s.authority_tier "
        "FROM authority_grant g LEFT JOIN source s ON s.id=g.source_id "
        "WHERE g.state_usps=? ORDER BY g.id DESC LIMIT ?", (usps, limit)).fetchall()
    return list(thr) + list(ag)


def measure_flags(rows, doc_text):
    """Deterministic checks on recorded measures."""
    flags = []
    flag = _flagger(flags)
    norm_doc = _norm(doc_text) if doc_text else ""

    for r in rows:
        label = r["measure_id_local"] or r["election_date"]
        if r["votes_total"] is None and r["pct_yes"] is None:
            # Hard: a measure with neither counts nor a percentage cannot be
            # placed against a threshold, so it is not yet a finding.
            flag("no_result", label, "no vote counts and no percentage recorded",
                 hard=True)
        if r["threshold_required"] is None:
            # Soft, and usually about sequencing rather than the measure: the
            # state framework pass has not landed yet.
            flag("no_threshold", label,
                 "no threshold on file for this measure class, so the margin "
                 "against threshold cannot be computed — run the state "
                 "framework pass")
        if r["outcome"] == "unknown":
            flag("outcome_unknown", label, "outcome is unknown")
        if r["confidence"] == "low":
            flag("low_confidence", label, "extractor marked this low confidence")
        if (r["authority_tier"] or 4) >= 4:
            flag("weak_source", label,
                 "sourced to a tier-4 aggregator rather than the elections office")
        if norm_doc and r["measure_id_local"]:
            if _norm(str(r["measure_id_local"])) not in norm_doc:
                flag("id_missing", label,
                     "measure identifier does not appear in the crawled documents")
        if r["election_date"] and r["election_date"] > db.now()[:10] \
                and r["outcome"] in ("passed", "failed"):
            flag("future_result", label,
                 "election date is in the future but a result is recorded",
                 hard=True)
    return flags


def framework_flags(rows, doc_text):
    """Deterministic checks on recorded thresholds and caps."""
    flags = []
    flag = _flagger(flags)
    norm_doc = _norm(doc_text) if doc_text else ""

    for r in rows:
        keys = r.keys()
        label = r["label"]
        if "threshold_value" in keys and r["threshold_value"] is not None:
            tv = r["threshold_value"]
            if tv <= 1.0:
                # Hard: a fraction stored as a percentage makes every margin
                # computed against it wrong.
                flag("threshold_scale", label,
                     "threshold %s looks like a fraction; percentages are "
                     "expected (two-thirds is 66.67)" % tv, hard=True)
            elif tv < 50.0:
                flag("threshold_low", label,
                     "threshold %s%% is below a simple majority" % tv)
        if "permitted" in keys and r["permitted"] == "yes" \
                and r["max_rate"] is None:
            flag("no_cap", label,
                 "permitted with no maximum rate recorded — confirm the "
                 "statute really sets no ceiling")
        if not r["statute_cite"]:
            flag("no_cite", label, "no statutory cite")
        elif norm_doc and _norm(r["statute_cite"]) not in norm_doc:
            flag("cite_missing", label,
                 "cite %s does not appear in the crawled documents" % r["statute_cite"])
        if r["confidence"] == "low":
            flag("low_confidence", label, "extractor marked this low confidence")
        if (r["authority_tier"] or 4) >= 4:
            flag("weak_source", label,
                 "sourced to a tier-4 aggregator rather than the code itself")
    return flags


CONCERN_GUIDANCE = """
These are automated concerns raised by cheap mechanical checks, not defects.
They are yours to judge, and most of them are noise. A bulk rate file has no
prose to quote. PDF text extraction routinely mangles whitespace, ligatures
and hyphenation, so a quote the text search could not locate is usually still
a correct quote. A source tier is often just an un-set field. A missing
threshold means the state framework pass has not run yet, which says nothing
about the row in front of you.

Judge the findings on the evidence. Do not flag a row because one of these
concerns is attached to it — flag it only if, reading the documents, you
think the recorded value is actually wrong or unsupportable.
"""


def ai_flags(settings, jurisdiction, category, rows, doc_text, images=None,
             system=None, concerns=None):
    """Returns (flags, error). error is set when the checker call failed."""
    max_chars = store.as_int(settings.get("checker_max_chars"), 80000)
    # Whatever columns the subject query selected, minus the internal id.
    findings = [{k: r[k] for k in r.keys() if k != "id"} for r in rows]
    concern_block = ""
    if concerns:
        concern_block = (
            "## Automated concerns to weigh\n```json\n%s\n```\n%s\n"
            % (json.dumps([{"row": c.get("instrument_code"),
                            "concern": c.get("reason")} for c in concerns],
                          indent=1),
               CONCERN_GUIDANCE))
    prompt = (
        "## Jurisdiction\n%s\n\n## Category\n%s\n\n"
        "## Findings to verify\n```json\n%s\n```\n\n"
        "%s"
        "## Source documents (truncated)\n%s\n\n"
        "Respond with JSON only."
        % (jurisdiction, category,
           json.dumps(findings, indent=1),
           concern_block,
           (doc_text or "_none — rely on the quotes and common sense_")[:max_chars]))
    model = (settings.get("checker_model") or "").strip() or None
    # The checker thinks harder than the extractor on purpose: its whole job
    # is to be skeptical, and a checker that rubber-stamps is worse than none.
    raw, err = extract.chat(settings, prompt, system=system or CHECK_SYSTEM,
                            images=images, model=model,
                            provider=checker_provider(settings),
                            effort=extract.DEFAULT_CHECKER_EFFORT)
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


def checker_provider(settings):
    """The provider the second pass runs on.

    An empty checker_provider means "same as the extractor" — the pre-split
    behavior. The default is the local llama model: checking matters less
    than extracting, and a free second opinion is still a second opinion.
    """
    own = (settings.get("checker_provider") or "").strip().lower()
    return own or (settings.get("provider") or "none").strip().lower()


def checker_model_name(settings):
    return ((settings.get("checker_model") or "").strip()
            or extract.default_model(settings, checker_provider(settings)))


def record(conn, run_id, geoid, category, verdict, flags, settings):
    conn.execute(
        "INSERT INTO check_result (run_id, geoid, category, verdict, flags, "
        "provider, model, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, geoid, category, verdict,
         json.dumps(flags) if flags else None,
         checker_provider(settings), checker_model_name(settings), db.now()))


def summarize(flags, limit=3):
    parts = []
    for f in flags[:limit]:
        parts.append("%s: %s" % (f.get("instrument_code") or "?", f.get("reason")))
    more = len(flags) - limit
    if more > 0:
        parts.append("+%d more" % more)
    lead = ("contradiction found" if hard_only(flags) else "checker flagged")
    return lead + " — " + "; ".join(parts)


def subject(conn, geoid, category):
    """What to check for this work item, and how to describe it to the model.

    Returns (rows, flag_fn, system_prompt, noun). The three passes check
    different tables, so the checker has to know which one it is looking at.
    """
    if category == ELECTIONS:
        return (live_measures(conn, geoid), measure_flags, MEASURE_SYSTEM,
                "measure")
    if category == FRAMEWORK:
        j = conn.execute("SELECT state_usps FROM jurisdiction WHERE geoid=?",
                         (geoid,)).fetchone()
        usps = j["state_usps"] if j else geoid
        return (live_framework(conn, usps), framework_flags, FRAMEWORK_SYSTEM,
                "framework rule")
    return (live_rows(conn, geoid, category), deterministic_flags, CHECK_SYSTEM,
            "finding")


def run_and_apply(conn, settings, run_id, geoid, category, doc_text,
                  images=None, jurisdiction=None):
    """Check what was just written for one work item and set its status.

    Returns (verdict, message). verdict is 'pass', 'flag', 'error', or
    'off' when the checker is disabled (status is then left at
    needs_review, the pre-checker behavior).
    """
    if not store.as_bool(settings.get("checker_enabled")):
        return "off", "checker disabled — human review required"

    rows, flag_fn, system, noun = subject(conn, geoid, category)
    if not rows:
        return "off", "nothing to check"

    if jurisdiction is None:
        j = conn.execute("SELECT name, state_usps, kind, population "
                         "FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
        jurisdiction = ("%s, %s (%s, pop %s, geoid %s)" %
                        (j["name"], j["state_usps"], j["kind"],
                         j["population"], geoid)) if j else geoid

    mechanical = flag_fn(rows, doc_text)
    hard = hard_only(mechanical)
    soft = soft_only(mechanical)

    # A contradiction is not a judgement call. Send it to a human and skip the
    # model call entirely — there is nothing for a second opinion to add, and
    # at ~22k tokens a call it is the one place skipping is free money.
    if hard:
        message = summarize(hard)
        ledger.set_status(conn, geoid, category, "needs_review",
                          error=message[:500])
        record(conn, run_id, geoid, category, "flag", hard, settings)
        conn.commit()
        return "flag", message

    err = None
    ai = []
    if checker_provider(settings) != "none":
        ai, err = ai_flags(settings, jurisdiction, category, rows,
                           doc_text, images=images, system=system,
                           concerns=soft)
    else:
        err = "no AI provider for the checker"

    if err:
        verdict = "error"
        message = "second check unavailable (%s) — review by hand" % err[:200]
        ledger.set_status(conn, geoid, category, "needs_review", error=message)
        flags = soft
    elif ai:
        # The model found something. Carry the soft concerns along as context
        # for whoever reads the review page.
        verdict = "flag"
        flags = ai + soft
        message = summarize(flags)
        ledger.set_status(conn, geoid, category, "needs_review", error=message[:500])
    else:
        # Soft concerns only, and the model read the documents and was not
        # troubled. That is the call we asked it to make: file it.
        verdict = "pass"
        flags = soft
        message = "second check passed — auto-verified"
        if soft:
            message += " (%d mechanical concern(s) judged immaterial)" % len(soft)
        ledger.set_status(conn, geoid, category, "complete", error=None)

    record(conn, run_id, geoid, category, verdict, flags, settings)
    conn.commit()
    return verdict, message
