"""Turn crawled text + a research packet into ingestible findings JSON.

Providers:
  openai  — OpenAI-compatible /v1/chat/completions (OpenAI, Groq, LM Studio, llama.cpp)
  anthropic — Anthropic Messages API
  llama   — Ollama /api/chat, with /v1/chat/completions as fallback
  none    — skip extraction
"""

import base64
import json
import re

import httpx

from taxdb.vocab import validate_finding

SYSTEM = """You are a tax-research extractor. You read official pages and fill
a JSON findings file for a US local-tax database.

The packet in the user message says which sections to return. Return only the
sections it asks for. The sections are:

  findings    tax rates and caps in force for one jurisdiction
  measures    revenue measures put to voters, and the certified result
  thresholds  what share of the vote a class of measure needs, by statute
  grants      what a state permits its local governments to levy, and the cap
  profile     one state's statutory frame (a single object, not an array)

Rules:
- Every claim needs a source.url that appears in the provided documents.
- Prefer primary law (statutes, ordinances) and the agency of record.
- If a tax is legally available but not imposed, status is "authorized_not_levied".
- If barred by state law, status is "prohibited" with the cite.
- If you cannot find a rate, status is "unknown" and say why in notes.
- Do not estimate, interpolate from neighbors, or invent numbers.
- Give the rate exactly as published, with its unit. Do not convert mills to
  percent. Percentages are percentages: two-thirds is 66.67, not 0.6667.
- Dates must be ISO YYYY-MM-DD.
- confidence is high only when the figure is printed on a primary source.
- Every tax finding must include source_quote: a short verbatim phrase copied
  from the documents that contains the rate, the prohibition, or the
  authorization.
- For measures, record the vote counts as printed and let the percentage be
  computed. Certified results only, never election-night returns.
- For thresholds and grants, the statutory cite is required.
- Set source.authority_tier from what you actually read: 1 for statute, code
  or ordinance text, 2 for the agency of record (a state revenue department, a
  county auditor or elections office), 3 for a university or association
  compilation, 4 for a commercial aggregator. Do not leave it blank.
- When a second document supports the same figure, list it under
  corroborating_sources as [{"url": ..., "name": ...}]. A rate confirmed by
  the ordinance and the rate table is worth more than either alone, and this
  is what lets a row be used with a client.
- Return ONLY valid JSON matching the schema in the user message.
- If the documents do not support a row, omit it rather than guess. An empty
  array is a real answer.
- Use only the allowed category and instrument_code values listed in the packet.
"""

# Doc keys ingest knows how to write. A framework or elections packet returns
# none of them under "findings", so accepting only that key silently threw
# away every threshold and every measure a model found.
SECTION_KEYS = ("findings", "measures", "thresholds", "grants", "profile")


# What each Anthropic model accepts. These are not interchangeable: adaptive
# thinking and `effort` are rejected outright by the 4.5-generation models,
# which still take the old fixed thinking budget. Sending the wrong pair is a
# 400, so the shape is looked up rather than assumed.
ANTHROPIC_CAPS = {
    "claude-opus-5":    {"adaptive": True,  "effort": True},
    "claude-sonnet-5":  {"adaptive": True,  "effort": True},
    "claude-opus-4-8":  {"adaptive": True,  "effort": True},
    "claude-opus-4-7":  {"adaptive": True,  "effort": True},
    "claude-sonnet-4-6": {"adaptive": True, "effort": True},
    "claude-opus-4-6":  {"adaptive": True,  "effort": True},
    "claude-haiku-4-5": {"adaptive": False, "effort": False},
    "claude-sonnet-4-5": {"adaptive": False, "effort": False},
}

# Effort is the cost and latency dial. Extraction is transcription with
# judgement about units and status, not open reasoning, so it does not need to
# think hard. The checker is the opposite: its whole job is to be skeptical,
# and a checker that rubber-stamps is worse than no checker.
DEFAULT_EFFORT = "low"
DEFAULT_CHECKER_EFFORT = "medium"


def anthropic_caps(model):
    """Capabilities for a model, defaulting to the conservative shape.

    An unknown model string (the field is free text) gets no thinking and no
    effort, which every model accepts. Guessing the other way turns a typo
    into a 400 on every item.
    """
    return ANTHROPIC_CAPS.get((model or "").strip(),
                              {"adaptive": False, "effort": False})


# One cap for every Anthropic call, thinking and answer together. 8192 was
# too tight: adaptive thinking spends from the same budget, and a
# findings-heavy county could truncate mid-JSON, fail the parse, and send the
# item back for a full recrawl and re-extraction — the whole cost, paid twice.
# 16384 is still safe without streaming.
ANTHROPIC_MAX_TOKENS = 16384


def anthropic_tuning(model, effort=None, max_tokens=ANTHROPIC_MAX_TOKENS):
    """Thinking and effort parameters for one Anthropic call.

    Thinking is on by default on the 5-generation models when the parameter is
    omitted, and `max_tokens` caps thinking and the answer together. Left
    alone, a findings-heavy page can spend the budget reasoning and return
    truncated JSON, which fails the parse, sends the item back to the queue,
    and burns an attempt. So thinking is always explicit here.
    """
    caps = anthropic_caps(model)
    body = {}
    if caps["adaptive"]:
        body["thinking"] = {"type": "adaptive"}
    if caps["effort"]:
        body["output_config"] = {"effort": effort or DEFAULT_EFFORT}
    return body


class ExtractError(Exception):
    pass


def parse_json_payload(text):
    """Pull a JSON object out of a model response, including fenced blocks."""
    if text is None:
        raise ExtractError("empty model response")
    raw = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExtractError("model did not return JSON: %s" % exc)


def default_model(settings, provider=None):
    provider = provider or (settings.get("provider") or "none").strip().lower()
    if provider == "openai":
        return settings.get("openai_model") or "gpt-4o-mini"
    if provider == "anthropic":
        return settings.get("anthropic_model") or "claude-sonnet-5"
    if provider == "llama":
        return settings.get("llama_model") or "llama3.1"
    return None


def chat(settings, prompt, system=SYSTEM, images=None, model=None, effort=None,
         provider=None):
    """One completion against the configured provider.

    Returns (raw_text, error). model overrides the provider's default —
    the second checker uses this to run a different (cheaper) model, and
    effort lets it think harder than the extractor does. provider overrides
    the extractor's provider entirely, which is how the checker runs on the
    free local model while extraction stays on a paid API.
    """
    provider = (provider or settings.get("provider") or "none").strip().lower()
    if provider in ("", "none"):
        return None, "no AI provider configured"
    images = images or []
    model = model or default_model(settings, provider)
    try:
        if provider == "openai":
            raw = _openai_compat(
                (settings.get("openai_base_url") or "https://api.openai.com/v1").rstrip("/"),
                settings.get("openai_api_key") or "",
                model, prompt, images=images, system=system)
        elif provider == "anthropic":
            raw = _anthropic(settings.get("anthropic_api_key") or "", model, prompt,
                             images=images, system=system, effort=effort)
        elif provider == "llama":
            raw = _llama(
                (settings.get("llama_base_url") or "http://127.0.0.1:11434").rstrip("/"),
                settings.get("llama_api_key") or "",
                model, prompt, images=images, system=system)
        else:
            return None, "unknown provider %r" % provider
    except ExtractError as exc:
        return None, str(exc)
    except httpx.HTTPError as exc:
        return None, "provider HTTP error: %s" % exc
    return raw, None


def extract(settings, packet_text, documents_text, researcher="collector", images=None):
    prompt = _user_prompt(packet_text, documents_text)
    raw, err = chat(settings, prompt, images=images)
    if err:
        return None, None, err

    try:
        doc = parse_json_payload(raw)
    except ExtractError as exc:
        return raw, None, str(exc)

    doc.setdefault("schema_version", "1.1")
    doc.setdefault("researcher", researcher)
    doc.setdefault("extraction_method", "agent_research")

    present = [k for k in SECTION_KEYS if doc.get(k) is not None]
    if not present:
        return raw, None, ("JSON has none of the expected sections (%s)"
                           % ", ".join(SECTION_KEYS))
    for key in present:
        if key != "profile" and not isinstance(doc[key], list):
            return raw, None, "%r must be an array" % key
    if isinstance(doc.get("profile"), list) and len(doc["profile"]) == 1:
        doc["profile"] = doc["profile"][0]
    return raw, doc, None


def validate_doc(conn, doc):
    """Return (ok_rows, errors) without writing."""
    errors, ok_rows = [], []
    for i, f in enumerate(doc.get("findings") or []):
        errs = validate_finding(f, i)
        geoid = f.get("geoid")
        if geoid and not conn.execute(
                "SELECT 1 FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone():
            errs.append("finding[%d]: geoid %r is not a seeded jurisdiction" % (i, geoid))
        if errs:
            errors.extend(errs)
        else:
            ok_rows.append(f)
    return ok_rows, errors


def _user_prompt(packet_text, documents_text):
    return (
        packet_text
        + "\n\n## Documents fetched for this jurisdiction\n\n"
        + (documents_text or "_No documents fetched._")
        + "\n\nRespond with JSON only."
    )


def _b64(blob):
    return base64.b64encode(blob).decode("ascii")


def _openai_compat(base_url, api_key, model, prompt, images=None, system=SYSTEM):
    url = base_url
    if not url.endswith("/chat/completions"):
        url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    user_content = prompt
    if images:
        parts = [{"type": "text", "text": prompt}]
        for img in images:
            mime = img.get("mime") or "image/png"
            parts.append({
                "type": "image_url",
                "image_url": {"url": "data:%s;base64,%s" % (mime, _b64(img["data"]))},
            })
        user_content = parts
    body = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    }
    with httpx.Client(timeout=180.0) as client:
        r = client.post(url, headers=headers, json=body)
        if r.status_code >= 400:
            raise ExtractError("OpenAI-compatible API %s: %s" % (r.status_code, r.text[:400]))
        data = r.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ExtractError("unexpected OpenAI-compatible response")


def _anthropic(api_key, model, prompt, images=None, system=SYSTEM, effort=None):
    if not api_key:
        raise ExtractError("Anthropic API key is empty")
    # Prompt caching is generally available; the old beta header is noise.
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    content = []
    for img in images or []:
        mime = img.get("mime") or "image/png"
        if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            mime = "image/jpeg"
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": _b64(img["data"])},
        })
    content.append({"type": "text", "text": prompt})
    body = {
        "model": model,
        "max_tokens": ANTHROPIC_MAX_TOKENS,
        # The breakpoint is correct placement even though it does not fire
        # today: this prompt is a few hundred tokens and the minimum cacheable
        # prefix is larger. It costs nothing and starts paying if the prompt
        # grows. The documents are per-jurisdiction and sit after it, so there
        # is nothing else here worth caching.
        "system": [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{"role": "user", "content": content}],
    }
    body.update(anthropic_tuning(model, effort=effort,
                                 max_tokens=body["max_tokens"]))
    with httpx.Client(timeout=180.0) as client:
        r = client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
        if r.status_code >= 400:
            raise ExtractError("Anthropic API %s: %s" % (r.status_code, r.text[:400]))
        data = r.json()
    # A truncated answer is not a malformed one. Say which it was, so the log
    # reads "raise the cap", not "the model wrote garbage".
    if data.get("stop_reason") == "max_tokens":
        raise ExtractError(
            "response hit the %d-token output cap and was truncated"
            % ANTHROPIC_MAX_TOKENS)
    parts = []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
    if not parts:
        raise ExtractError("Anthropic response had no text")
    return "\n".join(parts)


def _llama(base_url, api_key, model, prompt, images=None, system=SYSTEM):
    """Prefer Ollama native /api/chat; fall back to OpenAI-compatible."""
    native = base_url
    if native.endswith("/v1"):
        native = native[:-3]
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    msg = {
        "role": "user",
        "content": prompt,
    }
    if images:
        msg["images"] = [_b64(img["data"]) for img in images]
    body = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            msg,
        ],
        "options": {"temperature": 0.1},
    }
    with httpx.Client(timeout=300.0) as client:
        try:
            r = client.post(native.rstrip("/") + "/api/chat", headers=headers, json=body)
            if r.status_code < 400:
                data = r.json()
                got = (data.get("message") or {}).get("content")
                if got:
                    return got
        except httpx.HTTPError:
            pass
        return _openai_compat(native.rstrip("/") + "/v1", api_key, model, prompt,
                              images=images, system=system)
