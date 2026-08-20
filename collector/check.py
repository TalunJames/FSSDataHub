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

# Appended to every checker prompt: the shape of the recommendation the
# human reviewer sees beside the flags. The reviewer's buttons are exactly
# these three actions plus reading the source themselves, so the lean must
# be one of them or an honest "unsure".
ADVICE_GUIDANCE = """
advice is your overall read for the human who reviews anything you flag.
lean:
- "publish" when the values are defensible despite the concerns raised.
- "try_again" when better official documents likely exist and another
  crawl should find them (wrong document, wrong jurisdiction, stale page).
- "no_such_tax" only when the documents affirmatively show this tax or
  measure does not exist here.
- "unsure" when a person genuinely has to read the source to decide.
hint: one or two plain sentences saying what to check first and why.
Always include advice, even when every verdict is pass.
"""

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
{"verdicts":[{"instrument_code":"...","verdict":"pass"|"flag","reason":"short reason, empty when pass"}],
 "advice":{"lean":"publish"|"try_again"|"no_such_tax"|"unsure","hint":"one or two sentences"}}
Include one verdict per finding you were given.
""" + ADVICE_GUIDANCE


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
{"verdicts":[{"instrument_code":"<measure_id_local or election_date>","verdict":"pass"|"flag","reason":"short reason, empty when pass"}],
 "advice":{"lean":"publish"|"try_again"|"no_such_tax"|"unsure","hint":"one or two sentences"}}
Include one verdict per measure you were given.
""" + ADVICE_GUIDANCE

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
{"verdicts":[{"instrument_code":"<measure_class or instrument_code>","verdict":"pass"|"flag","reason":"short reason, empty when pass"}],
 "advice":{"lean":"publish"|"try_again"|"no_such_tax"|"unsure","hint":"one or two sentences"}}
Include one verdict per row you were given.
""" + ADVICE_GUIDANCE


def _id_filter(ids, column="id"):
    """SQL fragment restricting to specific row ids, or nothing at all."""
    if not ids:
        return "", []
    ids = sorted(ids)
    return (" AND %s IN (%s)" % (column, ",".join("?" * len(ids))), ids)


def live_rows(conn, geoid, category, ids=None):
    where, params = _id_filter(ids, "t.id")
    # prev_as_of: the best date on whatever this row displaced. The date
    # checks in deterministic_flags use it to catch a stale document taking
    # over from a newer rate — including data written before that guard
    # existed, whenever a refresh re-checks the item.
    return conn.execute(
        "SELECT t.id, t.instrument_code, t.label, t.status, t.rate_value, "
        "t.rate_unit, t.cap_value, t.cap_unit, t.confidence, t.source_quote, "
        "t.notes, t.effective_date, t.fiscal_year, t.extraction_method, s.url, "
        "(SELECT MAX(COALESCE(p.effective_date, p.fiscal_year || '-01-01')) "
        " FROM tax_instrument p WHERE p.superseded_by = t.id AND p.id != t.id) "
        "AS prev_as_of "
        "FROM tax_instrument t LEFT JOIN source s ON s.id=t.source_id "
        "WHERE t.geoid=? AND t.category=? AND t.superseded_by IS NULL" + where,
        [geoid, category] + params).fetchall()


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


# Vocabulary of documents that carry official-looking dollar figures that are
# not taxes: what a government pays or charges, not what anyone owes it.
# Matched against the finding's label and quote after _norm (lowercased,
# punctuation collapsed), so "per-diem" and "Per Diem" both hit "per diem".
# A hit is a review flag, not a deletion — the rare tax document that talks
# about reimbursement goes to a human with clear advice, which is the point.
NOT_A_TAX_TERMS = ("reimburs", "per diem", "travel allowance",
                   "lodging allowance", "mileage rate", "expense allowance")


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
        blob = _norm("%s %s" % (r["label"] or "", quote))
        if blob and any(t in blob for t in NOT_A_TAX_TERMS):
            flag("not_a_tax", inst,
                 "the label or quote reads like a reimbursement or per-diem "
                 "schedule (what a government pays, not a tax anyone owes)",
                 hard=True)
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
        # Time must not run backwards. Ingest files a clearly-older document
        # as history, so a regression or a dated-to-undated replacement here
        # means the machine could not tell which rate is current — a human
        # must. Year-precision rows compare by year only, so a same-year
        # refresh does not flag itself.
        prev = _field(r, "prev_as_of")
        if prev:
            prev = str(prev)[:10]
            eff = r["effective_date"]
            fy = _field(r, "fiscal_year")
            if eff:
                if str(eff)[:10] < prev:
                    flag("date_regression", inst,
                         "current rate is dated %s but it replaced one dated "
                         "%s — an older document may have taken over from a "
                         "newer rate" % (str(eff)[:10], prev), hard=True)
            elif fy is not None:
                try:
                    if int(fy) < int(prev[:4]):
                        flag("date_regression", inst,
                             "current rate is for %s but it replaced one "
                             "dated %s — an older document may have taken "
                             "over from a newer rate" % (fy, prev), hard=True)
                except (TypeError, ValueError):
                    pass
            else:
                flag("undated_supersede", inst,
                     "an undated rate replaced one dated %s — confirm which "
                     "is actually current" % prev, hard=True)
    return flags


def live_measures(conn, geoid, limit=40, ids=None):
    where, params = _id_filter(ids, "b.id")
    return conn.execute(
        "SELECT b.id, b.measure_id_local, b.election_date, b.election_type, "
        "b.measure_class, b.category, b.instrument_code, b.outcome, b.rate_value, "
        "b.rate_unit, b.principal_amount, b.votes_yes, b.votes_no, b.votes_total, "
        "b.pct_yes, b.threshold_required, b.threshold_basis, b.margin_vs_threshold, "
        "b.stated_purpose, b.confidence, b.notes, s.url, s.authority_tier "
        "FROM ballot_measure b LEFT JOIN source s ON s.id=b.source_id "
        "WHERE b.geoid=? AND b.superseded_by IS NULL" + where +
        " ORDER BY b.election_date DESC LIMIT ?",
        [geoid] + params + [limit]).fetchall()


def live_framework(conn, usps, limit=80, thr_ids=None, ag_ids=None):
    """Thresholds and grants for one state, as one checkable list."""
    thr_where, thr_params = _id_filter(thr_ids, "t.id")
    ag_where, ag_params = _id_filter(ag_ids, "g.id")
    thr = conn.execute(
        "SELECT t.id, t.measure_class AS label, t.jurisdiction_kind, "
        "t.purpose_restriction, t.threshold_value, t.threshold_basis, "
        "t.statute_cite, t.confidence, t.notes, s.url, s.authority_tier "
        "FROM threshold_rule t LEFT JOIN source s ON s.id=t.source_id "
        "WHERE t.state_usps=?" + thr_where + " ORDER BY t.id DESC LIMIT ?",
        [usps] + thr_params + [limit]).fetchall()
    ag = conn.execute(
        "SELECT g.id, g.instrument_code AS label, g.jurisdiction_kind, g.category, "
        "g.permitted, g.max_rate, g.max_rate_unit, g.statute_cite, g.confidence, "
        "g.notes, s.url, s.authority_tier "
        "FROM authority_grant g LEFT JOIN source s ON s.id=g.source_id "
        "WHERE g.state_usps=?" + ag_where + " ORDER BY g.id DESC LIMIT ?",
        [usps] + ag_params + [limit]).fetchall()
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


# Documents longer than this are excerpted around each finding's anchor text
# (source_quote, measure id, statute cite) instead of sent whole. The verdicts
# are judged against those anchors, so the text around them is what the
# checker actually needs — on the local model, prompt evaluation of an 80k-char
# document is the dominant per-item latency.
EXCERPT_OVER = 15000

_ANCHOR_FIELDS = ("source_quote", "measure_id_local", "statute_cite")


def excerpt_doc(doc_text, rows, max_chars, pad=2000, head=3000):
    """Anchor-centered windows of doc_text, merged and capped at max_chars.

    Falls back to the plain head of the document when no anchor can be
    located, which is exactly the pre-excerpt behavior.
    """
    if not doc_text or len(doc_text) <= min(EXCERPT_OVER, max_chars):
        return (doc_text or "")[:max_chars]
    spans = [(0, min(head, len(doc_text)))]
    found = False
    for r in rows:
        for field in _ANCHOR_FIELDS:
            anchor = str(_field(r, field) or "").strip()
            if len(anchor) < 3:
                continue
            pos = doc_text.find(anchor[:80])
            if pos < 0:
                continue
            found = True
            spans.append((max(0, pos - pad),
                          min(len(doc_text), pos + len(anchor) + pad)))
    if not found:
        return doc_text[:max_chars]
    spans.sort()
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts, total = [], 0
    for start, end in merged:
        chunk = doc_text[start:end][:max(0, max_chars - total)]
        if not chunk:
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n\n[... document trimmed to the passages around each finding ...]\n\n".join(parts)


# Recommendations a lean may take; anything else the model says is "unsure".
ADVICE_LEANS = ("publish", "try_again", "no_such_tax", "unsure")

# What to tell the reviewer for each hard mechanical flag. These items skip
# the model call (a contradiction is not a judgement call), so their advice
# is written here, once, in plain words. Format: code -> (lean, hint).
HARD_ADVICE = {
    "not_a_tax": ("try_again",
        "The words around this number describe a reimbursement or per-diem "
        "schedule — what a government pays its own travelers — not a tax "
        "anyone owes. Send it back so the crawler hunts for the real tax; "
        "if this place truly has none, No such tax is the honest answer."),
    "implausible_rate": ("try_again",
        "The number is far outside anything plausible for its unit, which "
        "usually means units got mixed up (mills read as percent, or a "
        "state total read as local). If the source really says this, type "
        "it in yourself; otherwise send it back."),
    "status_conflict": ("unsure",
        "The record says this tax is prohibited but a rate was recorded "
        "with it. One of the two is wrong: check whether the document "
        "describes a real levy or a ban."),
    "over_cap": ("try_again",
        "The rate is above its own recorded cap, so one of the two numbers "
        "likely belongs to another jurisdiction or year. Check which one "
        "the source actually supports."),
    "date_regression": ("unsure",
        "This rate came from an older document than the rate it replaced. "
        "Compare the dates in both sources: if the older document really is "
        "current, publish; if not, try again so the newer rate comes back."),
    "undated_supersede": ("unsure",
        "An undated rate replaced a dated one, so the machine cannot tell "
        "which is current. Open both sources and keep whichever the newer "
        "document supports."),
    "no_result": ("try_again",
        "The measure has no vote counts and no percentage, so it cannot be "
        "judged against its threshold. Try again so the crawler looks for "
        "the certified canvass, or type the numbers in from the county's "
        "results."),
    "future_result": ("try_again",
        "A result is recorded for an election that has not happened yet, "
        "so the date or the result is wrong. Send it back unless the "
        "source clearly supports both."),
    "threshold_scale": ("try_again",
        "The threshold looks like a fraction (0.6) rather than a "
        "percentage (60). If the statute is quoted correctly, type the "
        "corrected number in yourself."),
}

ADVICE_ERROR = {
    "lean": "unsure",
    "hint": ("The second check never ran, so nothing has confirmed this. "
             "Read the archived text against the number before publishing."),
}

ADVICE_FLAGGED_DEFAULT = {
    "lean": "unsure",
    "hint": ("The checker flagged this but gave no recommendation. Weigh "
             "its reasons against the archived source text."),
}


def hard_advice(flags):
    """The reviewer hint for a hard-flagged item, from its first known code."""
    for f in flags:
        lean_hint = HARD_ADVICE.get(f.get("code"))
        if lean_hint:
            return {"lean": lean_hint[0], "hint": lean_hint[1]}
    return dict(ADVICE_FLAGGED_DEFAULT)


def parse_advice(doc):
    """The model's advice object, normalized, or None when absent/garbled."""
    adv = doc.get("advice") if isinstance(doc, dict) else None
    if not isinstance(adv, dict):
        return None
    lean = str(adv.get("lean") or "").strip().lower()
    hint = str(adv.get("hint") or "").strip()[:400]
    if lean not in ADVICE_LEANS:
        lean = "unsure"
    if not hint and lean == "unsure":
        return None
    return {"lean": lean, "hint": hint}


def ai_flags(settings, jurisdiction, category, rows, doc_text, images=None,
             system=None, concerns=None):
    """Returns (flags, advice, error). error is set when the call failed.

    advice is the model's recommendation for the reviewer, or None — an
    older prompt or a small local model may not return one, and the
    verdicts stand on their own without it.
    """
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
           excerpt_doc(doc_text, rows, max_chars)
           or "_none — rely on the quotes and common sense_"))
    model = (settings.get("checker_model") or "").strip() or None
    # The checker thinks harder than the extractor on purpose: its whole job
    # is to be skeptical, and a checker that rubber-stamps is worse than none.
    raw, err = extract.chat(settings, prompt, system=system or CHECK_SYSTEM,
                            images=images, model=model,
                            provider=checker_provider(settings),
                            effort=extract.DEFAULT_CHECKER_EFFORT)
    if err:
        return [], None, err
    try:
        doc = extract.parse_json_payload(raw)
    except extract.ExtractError as exc:
        return [], None, "checker returned unparseable output: %s" % exc
    # A parseable answer in the wrong shape is still not a verdict. Anything
    # short of one explicit verdict per finding is an error, and any verdict
    # that is not exactly "pass" is a flag — the failure direction here must
    # be review, never trust.
    verdicts = doc.get("verdicts") if isinstance(doc, dict) else \
        (doc if isinstance(doc, list) else None)
    if not isinstance(verdicts, list) or not all(
            isinstance(v, dict) for v in verdicts):
        return [], None, "checker returned no usable verdicts array"
    if len(verdicts) < len(rows):
        return [], None, ("checker returned %d verdict(s) for %d finding(s) — "
                          "not one per finding as asked"
                          % (len(verdicts), len(rows)))
    out = []
    for v in verdicts:
        word = str(v.get("verdict") or "").strip().lower()
        if word != "pass":
            out.append({
                "code": "ai_flag",
                "instrument_code": v.get("instrument_code"),
                "reason": (v.get("reason")
                           or "checker verdict %r" % (word or "missing"))[:300],
            })
    return out, parse_advice(doc), None


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


def record(conn, run_id, geoid, category, verdict, flags, settings,
           advice=None):
    conn.execute(
        "INSERT INTO check_result (run_id, geoid, category, verdict, flags, "
        "advice, provider, model, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, geoid, category, verdict,
         json.dumps(flags) if flags else None,
         json.dumps(advice) if advice else None,
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


def subject(conn, geoid, category, claim_ids=None):
    """What to check for this work item, and how to describe it to the model.

    Returns (rows, flag_fn, system_prompt, noun). The three passes check
    different tables, so the checker has to know which one it is looking at.

    claim_ids, when given, is load_doc's {"table": set(ids)} for the ingest
    being checked: only the rows this document wrote are judged, not the
    whole accumulated table. Re-verdicting years of already-verified measures
    on every county refresh paid for itself exactly never — and let one old
    hard-flagged row block auto-complete for every future run of that county.
    """
    def _ids(table):
        if claim_ids is None:
            return None
        return claim_ids.get(table) or None

    if category == ELECTIONS:
        return (live_measures(conn, geoid, ids=_ids("ballot_measure")),
                measure_flags, MEASURE_SYSTEM, "measure")
    if category == FRAMEWORK:
        j = conn.execute("SELECT state_usps FROM jurisdiction WHERE geoid=?",
                         (geoid,)).fetchone()
        usps = j["state_usps"] if j else geoid
        return (live_framework(conn, usps, thr_ids=_ids("threshold_rule"),
                               ag_ids=_ids("authority_grant")),
                framework_flags, FRAMEWORK_SYSTEM, "framework rule")
    return (live_rows(conn, geoid, category, ids=_ids("tax_instrument")),
            deterministic_flags, CHECK_SYSTEM, "finding")


def run_and_apply(conn, settings, run_id, geoid, category, doc_text,
                  images=None, jurisdiction=None, claim_ids=None):
    """Check what was just written for one work item and set its status.

    Returns (verdict, message). verdict is 'pass', 'flag', 'error', or
    'off' when the checker is disabled (status is then left at
    needs_review, the pre-checker behavior).
    """
    if not store.as_bool(settings.get("checker_enabled")):
        return "off", "checker disabled — human review required"

    rows, flag_fn, system, noun = subject(conn, geoid, category, claim_ids)
    if not rows and claim_ids is not None:
        # The ingest wrote nothing new for the subject table (pure re-run or
        # updates the id filter can still see); judge what is on file instead
        # of skipping the check.
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
        record(conn, run_id, geoid, category, "flag", hard, settings,
               advice=hard_advice(hard))
        conn.commit()
        return "flag", message

    err = None
    ai = []
    advice = None
    if checker_provider(settings) != "none":
        ai, advice, err = ai_flags(settings, jurisdiction, category, rows,
                                   doc_text, images=images, system=system,
                                   concerns=soft)
    else:
        err = "no AI provider for the checker"

    if err:
        verdict = "error"
        message = "second check unavailable (%s) — review by hand" % err[:200]
        ledger.set_status(conn, geoid, category, "needs_review", error=message)
        flags = soft
        advice = dict(ADVICE_ERROR)
    elif ai:
        # The model found something. Carry the soft concerns along as context
        # for whoever reads the review page.
        verdict = "flag"
        flags = ai + soft
        message = summarize(flags)
        ledger.set_status(conn, geoid, category, "needs_review", error=message[:500])
        advice = advice or dict(ADVICE_FLAGGED_DEFAULT)
    else:
        # Soft concerns only, and the model read the documents and was not
        # troubled. That is the call we asked it to make: file it.
        verdict = "pass"
        flags = soft
        message = "second check passed — auto-verified"
        if soft:
            message += " (%d mechanical concern(s) judged immaterial)" % len(soft)
        ledger.set_status(conn, geoid, category, "complete", error=None)
        advice = None

    record(conn, run_id, geoid, category, verdict, flags, settings,
           advice=advice)
    conn.commit()
    return verdict, message
