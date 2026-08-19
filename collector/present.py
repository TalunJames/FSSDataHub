"""What the pages say: formatting and the queries only the UI needs.

The routes in app.py stay thin; everything that turns database rows into the
sentences and figures the screens show lives here. Nothing in this module
invents a number — every figure is counted from the database, and anything
missing is rendered as an em dash rather than guessed.
"""

import datetime
import json

from taxdb.vocab import ELECTIONS, FRAMEWORK

# Stored timestamps are UTC (taxdb.db.now). Screens show the box's local
# time, which on TrueNAS is whatever TZ the container carries.
STAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


def parse_stamp(value):
    """A stored timestamp as an aware UTC datetime, or None."""
    if not value:
        return None
    text = str(value).strip()[:19]
    for fmt in STAMP_FORMATS:
        try:
            naive = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=datetime.timezone.utc)
    return None


def local(value):
    stamp = parse_stamp(value)
    return stamp.astimezone() if stamp else None


def utc_floor(hours_ago=0, at_hour=None):
    """A UTC timestamp string for use in SQL comparisons."""
    now = datetime.datetime.now(datetime.timezone.utc)
    if at_hour is not None:
        local_now = now.astimezone()
        edge = local_now.replace(hour=at_hour, minute=0, second=0, microsecond=0)
        if edge > local_now:
            edge -= datetime.timedelta(days=1)
        now = edge.astimezone(datetime.timezone.utc)
    else:
        now -= datetime.timedelta(hours=hours_ago)
    return now.strftime("%Y-%m-%d %H:%M:%S")


def clock(stamp):
    return stamp.strftime("%I:%M%p").lstrip("0").lower()


def when(value):
    """'4:12am today', 'yesterday, 11:48pm', 'Aug 14' — never a raw stamp."""
    stamp = local(value)
    if not stamp:
        return "—"
    now = datetime.datetime.now(stamp.tzinfo)
    days = (now.date() - stamp.date()).days
    if days <= 0:
        if (now - stamp).total_seconds() < 90:
            return "just now"
        return "%s today" % clock(stamp)
    if days == 1:
        return "yesterday, %s" % clock(stamp)
    if days < 7:
        return "%s, %s" % (stamp.strftime("%a"), clock(stamp))
    return stamp.strftime("%b %-d")


def dateline(now=None):
    now = now or datetime.datetime.now()
    return now.strftime("%A, %-d %B")


def greeting(name=None, now=None):
    now = now or datetime.datetime.now()
    hour = now.hour
    part = "morning" if hour < 12 else ("afternoon" if hour < 18 else "evening")
    if name and name not in ("collector", "manual", ""):
        return "Good %s, %s." % (part, name)
    return "Good %s." % part


UNIT_SUFFIX = {
    "percent": "%",
    "mills": " mills",
    "ratio": " ratio",
    "usd_flat": " USD",
    "usd_per_unit": " USD per unit",
    "dollars_per_1000_av": " per $1,000 AV",
}


def rate(value, unit):
    """A rate as it is written down, or an em dash when there is none."""
    if value is None:
        return "—"
    text = ("%.4f" % float(value)).rstrip("0").rstrip(".")
    if unit == "usd_flat":
        return "$" + text
    return text + UNIT_SUFFIX.get(unit or "", " " + (unit or ""))


def category_label(category):
    if category in (FRAMEWORK, ELECTIONS):
        return {FRAMEWORK: "state rules", ELECTIONS: "ballot measures"}[category]
    return category.replace("_", " ")


def place_line(row):
    """'County · Ohio · population 1,326,063' for a jurisdiction row."""
    bits = [str(row["kind"]).title(), row["state_usps"]]
    pop = row["population"] if "population" in row.keys() else None
    if pop:
        bits.append("population {:,}".format(pop))
    return " · ".join(b for b in bits if b)


def _rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return []


def _one(conn, sql, params=()):
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


def _count(conn, sql, params=()):
    row = _one(conn, sql, params)
    return (row[0] or 0) if row else 0


# ── the rail and the header ───────────────────────────────────────────

def week(conn):
    """Counts for the rail. Seven days back, from real rows only."""
    edge = utc_floor(hours_ago=24 * 7)
    return {
        "published": _count(
            conn, "SELECT COUNT(*) FROM work_item WHERE status='complete' "
                  "AND completed_at >= ?", (edge,)),
        "flagged": _count(
            conn, "SELECT COUNT(*) FROM check_result WHERE verdict='flag' "
                  "AND created_at >= ?", (edge,)),
        "pages": _count(
            conn, "SELECT COUNT(*) FROM crawl_page WHERE fetched_at >= ?", (edge,)),
    }


def published_since_last_night(conn):
    """Work items finished since 6pm local yesterday."""
    return _count(
        conn, "SELECT COUNT(*) FROM work_item WHERE status='complete' "
              "AND completed_at >= ?", (utc_floor(at_hour=18),))


# ── today ─────────────────────────────────────────────────────────────

def inbox(conn, stats, settings, progress):
    """The short list of things only a person can settle.

    Every entry is a real count from the database. When the crawler and the
    checker handled everything, this comes back empty and the page says so.
    """
    items = []
    review = stats.get("needs_review") or 0
    if review:
        names = [r["name"] for r in _rows(
            conn, "SELECT j.name, MAX(w.priority) p FROM work_item w "
                  "JOIN jurisdiction j ON j.geoid=w.geoid "
                  "WHERE w.status='needs_review' GROUP BY j.name "
                  "ORDER BY p DESC LIMIT 3")]
        items.append({
            "tone": "flag",
            "title": "%d %s the checker could not confirm" % (
                review, "rate" if review == 1 else "rates"),
            "why": ", ".join(names) + ("…" if review > len(names) else ""),
            "when": "waiting",
            "cta": "Review them",
            "href": "/review",
        })
    stuck = _rows(
        conn, "SELECT j.name, j.state_usps, w.last_error, w.updated_at "
              "FROM work_item w JOIN jurisdiction j ON j.geoid=w.geoid "
              "WHERE w.status='blocked' ORDER BY w.updated_at DESC LIMIT 3")
    if stuck:
        total = _count(conn, "SELECT COUNT(*) FROM work_item WHERE status='blocked'")
        first = stuck[0]
        items.append({
            "tone": "quiet",
            "title": "%s could not be read" % first["name"] + (
                " and %d other%s" % (total - 1, "" if total == 2 else "s")
                if total > 1 else ""),
            "why": (first["last_error"] or "The site did not answer.")[:140],
            "when": when(first["updated_at"]),
            "cta": "Work list",
            "href": "/queue?status=blocked",
        })
    intake_failed = _count(
        conn, "SELECT COUNT(*) FROM intake_item WHERE status='failed'")
    if intake_failed:
        items.append({
            "tone": "quiet",
            "title": "%d document%s the reader could not use" % (
                intake_failed, "" if intake_failed == 1 else "s"),
            "why": "Uploaded by hand, but the extractor returned nothing usable.",
            "when": "",
            "cta": "Add a source",
            "href": "/manual",
        })
    if (stats.get("ready") and not stats.get("pending")
            and not stats.get("in_progress")
            and not stats.get("awaiting_ai")):
        left = (progress or {}).get("juris_total", 0) - (progress or {}).get("juris_done", 0)
        items.append({
            "tone": "go",
            "title": "Nothing is queued",
            "why": ("%s places in the country are still unresearched. Add a state "
                    "to keep the crawler busy." % "{:,}".format(left)) if left > 0
                   else "Every place on file has been researched.",
            "when": "",
            "cta": "Add a state",
            "href": "/queue",
        })
    if (settings.get("provider") or "none") == "none":
        items.append({
            "tone": "flag",
            "title": "No AI key, so nothing is being read",
            "why": "Pages are downloaded and archived, but no rates come out of them.",
            "when": "",
            "cta": "Open setup",
            "href": "/settings",
        })
    return items


def timeline(conn, limit=8):
    """What happened while nobody was watching, newest first."""
    events = []
    edge = utc_floor(hours_ago=48)

    passes = _count(
        conn, "SELECT COUNT(*) FROM check_result WHERE verdict='pass' "
              "AND created_at >= ?", (edge,))
    if passes:
        last = _one(conn, "SELECT MAX(created_at) FROM check_result "
                          "WHERE verdict='pass' AND created_at >= ?", (edge,))
        events.append({
            "tone": "good", "ts": last[0],
            "text": "%d %s confirmed by the second check and written to the "
                    "database." % (passes, "answer" if passes == 1 else "answers"),
        })
    flags = _count(
        conn, "SELECT COUNT(*) FROM check_result WHERE verdict IN ('flag','error') "
              "AND created_at >= ?", (edge,))
    if flags:
        last = _one(conn, "SELECT MAX(created_at) FROM check_result "
                          "WHERE verdict IN ('flag','error') AND created_at >= ?",
                    (edge,))
        events.append({
            "tone": "flag", "ts": last[0],
            "text": "%d %s flagged for you — the checker could not tie them to "
                    "their source." % (flags, "answer" if flags == 1 else "answers"),
        })
    for r in _rows(
            conn, "SELECT id, mode, status, message, items_claimed, pages_fetched, "
                  "findings_written, started_at, finished_at FROM crawl_run "
                  "WHERE started_at >= ? ORDER BY id DESC LIMIT 6", (edge,)):
        if r["status"] == "running":
            text = "%s run started." % str(r["mode"]).replace("_", " ").capitalize()
        elif r["status"] == "ok":
            text = "%s run finished: %d page%s read, %d record%s written." % (
                str(r["mode"]).replace("_", " ").capitalize(),
                r["pages_fetched"], "" if r["pages_fetched"] == 1 else "s",
                r["findings_written"], "" if r["findings_written"] == 1 else "s")
        else:
            text = "%s run %s. %s" % (
                str(r["mode"]).replace("_", " ").capitalize(), r["status"],
                (r["message"] or "")[:120])
        events.append({
            "tone": "flag" if r["status"] == "failed" else "quiet",
            "ts": r["finished_at"] or r["started_at"],
            "text": text.strip(),
            "href": "/runs?run_id=%d" % r["id"],
        })
    events.sort(key=lambda e: e["ts"] or "", reverse=True)
    for e in events:
        e["time"] = when(e["ts"])
    return events[:limit]


# ── review ────────────────────────────────────────────────────────────

def flag_reasons(conn, geoid, category):
    """Why the checker sent this one to a person, in its own words."""
    row = _one(
        conn, "SELECT verdict, flags FROM check_result WHERE geoid=? AND category=? "
              "ORDER BY id DESC LIMIT 1", (geoid, category))
    if not row:
        return []
    try:
        flags = json.loads(row["flags"]) if row["flags"] else []
    except ValueError:
        flags = []
    out = []
    for f in flags:
        if not isinstance(f, dict):
            continue
        reason = (f.get("reason") or "").strip()
        if reason:
            out.append(reason)
    if not out and row["verdict"] == "error":
        out.append("The second check could not run, so this was never confirmed.")
    return out


def review_items(conn, limit=40):
    """Everything waiting on a person, with the evidence beside it."""
    rows = _rows(
        conn, "SELECT w.geoid, w.category, w.last_error, w.updated_at, w.priority, "
              "j.name, j.state_usps, j.kind, j.population "
              "FROM work_item w JOIN jurisdiction j ON j.geoid=w.geoid "
              "WHERE w.status='needs_review' ORDER BY w.priority DESC, w.updated_at "
              "LIMIT ?", (limit,))
    items = []
    for r in rows:
        item = {
            "geoid": r["geoid"],
            "category": r["category"],
            "category_label": category_label(r["category"]),
            "name": r["name"],
            "state_usps": r["state_usps"],
            "meta": "%s · %s" % (place_line(r), category_label(r["category"])),
            "reasons": flag_reasons(conn, r["geoid"], r["category"]),
            "last_error": r["last_error"],
            "findings": [], "measures": [], "thresholds": [], "grants": [],
            "rate": None, "rate_note": "", "previous": None, "previous_note": "",
            "quote": "", "source_url": "", "source_name": "", "tier": "",
            "fetched": "",
        }
        if r["category"] == ELECTIONS:
            item["measures"] = _rows(
                conn, "SELECT b.election_date, b.measure_id_local, b.measure_class, "
                      "b.outcome, b.pct_yes, b.threshold_required, "
                      "b.margin_vs_threshold, b.stated_purpose, b.confidence, s.url "
                      "FROM ballot_measure b LEFT JOIN source s ON s.id=b.source_id "
                      "WHERE b.geoid=? AND b.superseded_by IS NULL "
                      "ORDER BY b.election_date DESC LIMIT 25", (r["geoid"],))
        elif r["category"] == FRAMEWORK:
            item["thresholds"] = _rows(
                conn, "SELECT t.measure_class, t.jurisdiction_kind, "
                      "t.purpose_restriction, t.threshold_value, t.threshold_basis, "
                      "t.statute_cite, t.confidence, s.url FROM threshold_rule t "
                      "LEFT JOIN source s ON s.id=t.source_id WHERE t.state_usps=? "
                      "ORDER BY t.measure_class LIMIT 25", (r["state_usps"],))
            item["grants"] = _rows(
                conn, "SELECT g.category, g.instrument_code, g.jurisdiction_kind, "
                      "g.permitted, g.max_rate, g.max_rate_unit, g.statute_cite, "
                      "g.confidence, s.url FROM authority_grant g "
                      "LEFT JOIN source s ON s.id=g.source_id WHERE g.state_usps=? "
                      "ORDER BY g.category LIMIT 25", (r["state_usps"],))
        else:
            item["findings"] = _rows(
                conn, "SELECT t.id, t.instrument_code, t.label, t.status, "
                      "t.rate_value, t.rate_unit, t.confidence, t.extraction_method, "
                      "t.retrieved_at, t.source_quote, t.effective_date, "
                      "s.url, s.name AS source_name, s.authority_tier "
                      "FROM tax_instrument t JOIN source s ON s.id=t.source_id "
                      "WHERE t.geoid=? AND t.category=? AND t.superseded_by IS NULL "
                      "ORDER BY t.id", (r["geoid"], r["category"]))
            _headline(conn, item)
        items.append(item)
    return items


TIER_WORDS = {
    1: "highest authority", 2: "second tier",
    3: "third tier", 4: "secondary source",
}


def _headline(conn, item):
    """The one number a reviewer is being asked about, and its evidence."""
    rows = [f for f in item["findings"] if f["rate_value"] is not None]
    row = rows[0] if rows else (item["findings"][0] if item["findings"] else None)
    if row is None:
        return
    item["rate"] = rate(row["rate_value"], row["rate_unit"])
    note = [row["label"] or row["instrument_code"]]
    if row["status"] and row["status"] != "levied":
        note.append(row["status"].replace("_", " "))
    if row["effective_date"]:
        note.append("effective %s" % row["effective_date"])
    item["rate_note"] = ", ".join(n for n in note if n)
    item["quote"] = (row["source_quote"] or "").strip()
    item["source_url"] = row["url"]
    item["source_name"] = row["source_name"] or row["url"]
    tier = row["authority_tier"]
    item["tier"] = TIER_WORDS.get(tier, "tier %s" % tier)
    item["fetched"] = when(row["retrieved_at"])
    item["headline_code"] = row["instrument_code"]

    prior = _one(
        conn, "SELECT rate_value, rate_unit, retrieved_at FROM tax_instrument "
              "WHERE geoid=? AND category=? AND instrument_code=? "
              "AND superseded_by IS NOT NULL ORDER BY id DESC LIMIT 1",
        (item["geoid"], item["category"], row["instrument_code"]))
    if prior:
        item["previous"] = rate(prior["rate_value"], prior["rate_unit"])
        item["previous_note"] = "on record since %s" % when(prior["retrieved_at"])
    else:
        item["previous"] = "—"
        item["previous_note"] = "nothing on record before this"


# ── the record ────────────────────────────────────────────────────────

# Chip label → the work_item statuses it covers.
RECORD_FILTERS = (
    ("all", "All", None),
    ("review", "Needs you", ("needs_review",)),
    ("published", "Published", ("complete",)),
    ("working", "In progress", ("pending", "in_progress", "awaiting_ai")),
    ("none", "Not levied", ("no_data",)),
    ("stuck", "Stuck", ("blocked",)),
)

STANDING = {
    "needs_review": ("Needs you", "flag",
                     "The automatic check could not confirm this one. "
                     "It is waiting on the Review page."),
    "complete": ("Published", "ok",
                 "Confirmed and live in the tax database."),
    "no_data": ("No such tax", "none",
                "Checked and confirmed: this place does not levy this tax."),
    "in_progress": ("Crawling now", "busy",
                    "The crawler is reading this place's website right now."),
    "pending": ("Queued", "busy", "Waiting its turn in the work list."),
    "awaiting_ai": ("Waiting to be read", "busy",
                    "The pages are fetched and filed. They go to the AI in the "
                    "next batch, which costs half as much as reading them one "
                    "at a time."),
    "blocked": ("Stuck", "busy",
                "The crawler gave up after repeated failures. "
                "Its last error is on the work list."),
}

PER_PAGE = 50


def record(conn, q="", filt="all", page=1, per_page=PER_PAGE):
    """One row per place and tax: what we hold and when we last looked."""
    statuses = dict((f[0], f[2]) for f in RECORD_FILTERS).get(filt)
    where, params = ["1=1"], []
    q = (q or "").strip()
    if q:
        where.append("(j.name LIKE ? OR j.geoid = ? OR j.state_usps = ?)")
        params += ["%" + q + "%", q, q.upper()]
    counts = {"all": 0}
    for row in _rows(conn,
                     "SELECT w.status, COUNT(*) n FROM work_item w "
                     "JOIN jurisdiction j ON j.geoid=w.geoid "
                     "WHERE " + " AND ".join(where) + " GROUP BY w.status", params):
        counts[row["status"]] = row["n"]
        counts["all"] += row["n"]
    chips = []
    for key, label, group in RECORD_FILTERS:
        n = counts["all"] if group is None else sum(counts.get(s, 0) for s in group)
        chips.append({"key": key, "label": label, "count": n, "on": key == filt})

    if statuses:
        where.append("w.status IN (%s)" % ",".join("?" * len(statuses)))
        params += list(statuses)
    total = _count(conn, "SELECT COUNT(*) FROM work_item w "
                         "JOIN jurisdiction j ON j.geoid=w.geoid "
                         "WHERE " + " AND ".join(where), params)
    page = max(1, int(page or 1))
    offset = (page - 1) * per_page
    rows = _rows(
        conn,
        "SELECT w.geoid, w.category, w.status, w.updated_at, w.completed_at, "
        "j.name, j.state_usps, j.kind, j.population "
        "FROM work_item w JOIN jurisdiction j ON j.geoid=w.geoid "
        "WHERE " + " AND ".join(where) +
        " ORDER BY j.population DESC, j.name, w.category LIMIT ? OFFSET ?",
        params + [per_page, offset])

    places = []
    for r in rows:
        label, tone, tip = STANDING.get(
            r["status"], (r["status"], "busy", "Current work-list status."))
        top = _one(
            conn, "SELECT rate_value, rate_unit FROM tax_instrument "
                  "WHERE geoid=? AND category=? AND superseded_by IS NULL "
                  "AND rate_value IS NOT NULL ORDER BY id LIMIT 1",
            (r["geoid"], r["category"]))
        places.append({
            "geoid": r["geoid"],
            "category": r["category"],
            "name": r["name"],
            "meta": "%s · %s · %s" % (r["state_usps"], r["kind"], r["geoid"]),
            "tax": category_label(r["category"]),
            "rate": rate(top["rate_value"], top["rate_unit"]) if top else "—",
            "standing": label,
            "tone": tone,
            "tip": tip,
            "checked": when(r["completed_at"] or r["updated_at"]),
            "status": r["status"],
        })
    return {
        "places": places,
        "chips": chips,
        "total": total,
        "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "showing": len(places),
        "offset": offset,
    }
