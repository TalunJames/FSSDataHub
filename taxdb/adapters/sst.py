"""Streamlined Sales Tax Governing Board rate files.

One adapter, 24 member states, one schema. Rate files carry effective-from
and effective-to dates, so rate_change_event can be filled from the source
rather than by differencing snapshots.

Do not archive the boundary files (~300 MB per state per quarter). Rate
files identify counties and places by FIPS, which maps directly to Census
GEOID. Special districts have no GEOID and are listed as unmapped.

The four rate columns are not interchangeable (intrastate vs interstate vs
food vs other varies by state). This adapter stores the general intrastate
rate and notes when food/drug differs.

Member files that have not been updated in years (IN, KY, NJ, RI, MI as of
2026) are ingested with a stale warning and confidence=medium.
"""

import csv
import datetime
import hashlib
import io
import re
import urllib.request
import zipfile

from .. import archive, db, ingest
from .base import Adapter

RATES_DIR = "https://www.streamlinedsalestax.org/ratesandboundry/Rates/"
TECH_GUIDE = ("https://www.streamlinedsalestax.org/docs/default-source/"
              "technology/technology-guide-october-2022.pdf")

# Type of Taxing Authority (X12 1721), zero-padded.
COUNTY_TYPES = {"00"}
PLACE_TYPES = {"01", "02", "03", "04", "09"}
TOWNSHIP_TYPES = {"05"}
STATE_TYPES = {"45"}
SPECIAL_TYPES = {"63", "69", "79", "10", "11", "21", "22", "23", "24", "25"}

INSTRUMENT = {
    "00": ("sales_use", "county_general_sales", "county", "county general sales tax"),
    "01": ("sales_use", "municipal_general_sales", "place", "municipal general sales tax"),
    "02": ("sales_use", "municipal_general_sales", "place", "town general sales tax"),
    "03": ("sales_use", "municipal_general_sales", "place", "village general sales tax"),
    "04": ("sales_use", "municipal_general_sales", "place", "borough general sales tax"),
    "09": ("sales_use", "municipal_general_sales", "place", "municipal general sales tax"),
    "05": ("sales_use", "municipal_general_sales", "mcd", "township general sales tax"),
}

HREF_RE = re.compile(r'href=["\']?([^"\'>\s]+\.(?:csv|zip))', re.I)
NAME_RE = re.compile(r"([A-Z]{2})R(\d{4}Q[1-4])", re.I)

# Files on the board site older than this year are historical only.
STALE_BEFORE_YEAR = 2025


class SstRates(Adapter):
    key = "sst"
    state = None
    categories = ("sales_use",)
    url = RATES_DIR
    source_name = "Streamlined Sales Tax Governing Board rate files"
    source_type = "bulk_file"
    authority_tier = 2
    description = ("Quarterly SSTGB rate database, one format for every member "
                   "state. General intrastate rate only; food/drug and interstate "
                   "columns may differ by state.")
    period_label = None

    def run(self, conn, dry_run=False, archive_only=False, states=None):
        files = list_rate_files(self.fetch_index())
        want = {s.upper() for s in states} if states else None
        if want:
            files = [f for f in files if f["state"] in want]
        if not files:
            return {"written": 0, "rejected": 0, "unmapped": [], "errors": [],
                    "sha256": None, "archive_file_id": None, "files": 0}

        all_findings, all_unmapped, all_events = [], [], []
        last_sha, last_aid = None, None
        n_files = 0
        for meta in files:
            blob = self._download(meta["url"])
            n_files += 1
            source_id = db.get_or_create_source(
                conn, meta["url"],
                "SST %s %s rate file" % (meta["state"], meta["period"]),
                source_type=self.source_type, authority_tier=self.authority_tier,
                scope_geoid=_state_geoid(conn, meta["state"]),
                publisher="Streamlined Sales Tax Governing Board",
                notes=self.description)
            sha = hashlib.sha256(blob).hexdigest()
            last_sha = sha
            conn.execute(
                "INSERT OR IGNORE INTO raw_document (source_id, url, sha256, byte_size, "
                "retrieved_at) VALUES (?,?,?,?,?)",
                (source_id, meta["url"], sha, len(blob), db.now()))
            aid, sha, _, _ = archive.put(
                conn, self.key, meta["url"], blob, meta["period"],
                source_id=source_id, filename=meta["filename"])
            last_aid = aid
            conn.commit()
            if archive_only:
                continue
            text = decode_rate_file(blob)
            findings, unmapped, events = rows_to_findings(
                conn, text, meta, archive_id=aid, source_url=meta["url"])
            all_findings.extend(findings)
            all_unmapped.extend(unmapped)
            all_events.extend(events)

        if archive_only:
            return {"written": 0, "rejected": 0, "unmapped": [], "errors": [],
                    "sha256": last_sha, "archive_file_id": last_aid, "files": n_files}

        doc = {"schema_version": "1.0", "researcher": "adapter:sst",
               "extraction_method": "bulk_import", "findings": all_findings}
        res = ingest.load_doc(conn, doc, dry_run=dry_run, allow_partial=True,
                              label="adapter:sst")
        if not dry_run:
            written_events = write_events(conn, all_events)
            res["rate_change_events"] = written_events
        res["unmapped"] = all_unmapped
        res["sha256"] = last_sha
        res["archive_file_id"] = last_aid
        res["files"] = n_files
        return res

    def fetch_index(self):
        req = urllib.request.Request(
            self.url, headers={"User-Agent": "tax-database/0.3"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("latin-1", errors="replace")

    def _download(self, url):
        from ..seed import fetch
        _, _, blob = fetch(url)
        return blob

    def parse(self, conn, blob):
        raise NotImplementedError("SST is multi-file; use run()")


def list_rate_files(html):
    """Parse the IIS directory listing into one record per member file."""
    found = {}
    for href in HREF_RE.findall(html or ""):
        name = href.split("/")[-1]
        m = NAME_RE.search(name)
        if not m:
            continue
        state, period = m.group(1).upper(), m.group(2).upper()
        url = href if href.startswith("http") else (RATES_DIR + name)
        rec = {"state": state, "period": period, "filename": name, "url": url}
        prev = found.get(state)
        if prev is None or _period_key(period) >= _period_key(prev["period"]):
            found[state] = rec
    return [found[k] for k in sorted(found)]


def _period_key(period):
    m = re.match(r"(\d{4})Q([1-4])", period or "")
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def decode_rate_file(blob):
    raw = blob
    if raw[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = [n for n in z.namelist()
                 if n.lower().endswith((".csv", ".txt")) and not n.endswith("/")]
        if not names:
            return ""
        raw = z.read(names[0])
    return raw.decode("latin-1", errors="replace")


def parse_rate_rows(text):
    """Yield canonical rate rows from an SST CSV (no header)."""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("state"):
            continue
        parts = next(csv.reader([line]))
        if len(parts) < 9:
            continue
        state_fips = re.sub(r"\D", "", parts[0]).zfill(2)
        if len(state_fips) != 2:
            continue
        jtype = re.sub(r"\D", "", parts[1]).zfill(2)
        jfips = (parts[2] or "").strip()
        try:
            intra = float(parts[3]) if parts[3] not in ("",) else None
            inter = float(parts[4]) if parts[4] not in ("",) else None
            food = float(parts[5]) if parts[5] not in ("",) else None
        except ValueError:
            continue
        begin, end = _iso_date(parts[7]), _iso_date(parts[8])
        yield {
            "state_fips": state_fips,
            "jtype": jtype,
            "jfips": jfips,
            "rate_intra": intra,
            "rate_inter": inter,
            "rate_food": food,
            "begin": begin,
            "end": end,
            "open_ended": (parts[8] or "").strip().startswith("9999"),
        }


def geoid_for(jtype, state_fips, jfips):
    """Census GEOID, or None if this jurisdiction type has no Census key."""
    jtype = (jtype or "").zfill(2)
    state_fips = (state_fips or "").zfill(2)
    digits = re.sub(r"\D", "", jfips or "")
    if jtype in COUNTY_TYPES:
        return state_fips + digits.zfill(3)
    if jtype in PLACE_TYPES:
        return state_fips + digits.zfill(5)
    return None


def percent(raw):
    """SST stores 0.01500 for 1.5%. Values already > 1 are treated as percent."""
    if raw is None:
        return None
    if raw > 1:
        return round(raw, 4)
    return round(raw * 100.0, 4)


def is_current(row, today=None):
    today = today or datetime.date.today().isoformat()
    if row.get("open_ended"):
        return True
    end = row.get("end")
    return bool(end and end >= today)


def rows_to_findings(conn, text, meta, archive_id=None, source_url=None, today=None):
    today = today or datetime.date.today().isoformat()
    stale = _period_key(meta.get("period"))[0] < STALE_BEFORE_YEAR
    source = {
        "url": source_url or meta.get("url") or RATES_DIR,
        "name": "SST %s %s" % (meta.get("state"), meta.get("period")),
        "source_type": "bulk_file",
        "authority_tier": 2,
    }
    grouped = {}
    unmapped = []
    for row in parse_rate_rows(text):
        jtype = row["jtype"]
        if jtype in STATE_TYPES:
            unmapped.append(("%s/%s/%s" % (row["state_fips"], jtype, row["jfips"]),
                             "state taxing authority — not a local instrument"))
            continue
        if jtype in SPECIAL_TYPES or jtype not in INSTRUMENT:
            unmapped.append(("%s/%s/%s" % (row["state_fips"], jtype, row["jfips"]),
                             "special/other taxing district, no Census GEOID"))
            continue
        geoid = geoid_for(jtype, row["state_fips"], row["jfips"])
        spec = INSTRUMENT[jtype]
        if not geoid:
            unmapped.append(("%s/%s/%s" % (row["state_fips"], jtype, row["jfips"]),
                             "no GEOID mapping for type %s" % jtype))
            continue
        live = conn.execute(
            "SELECT geoid, kind FROM jurisdiction WHERE geoid=?", (geoid,)
        ).fetchone()
        if not live:
            unmapped.append((geoid, "no seeded jurisdiction for SST FIPS "
                             "(type %s code %s)" % (jtype, row["jfips"])))
            continue
        if spec[2] != "mcd" and live["kind"] != spec[2]:
            # Place FIPS colliding with something else — still usable if kinds close.
            if not (spec[2] == "place" and live["kind"] in ("place", "mcd")):
                unmapped.append((geoid, "GEOID kind is %s, SST type %s expects %s"
                                 % (live["kind"], jtype, spec[2])))
                continue
        key = (geoid, spec[1])
        grouped.setdefault(key, []).append((row, spec, geoid))

    findings, events = [], []
    for (geoid, code), items in grouped.items():
        items.sort(key=lambda it: it[0]["begin"] or "")
        current = [it for it in items if is_current(it[0], today)]
        pick = current[-1] if current else None
        if pick:
            row, spec, geoid = pick
            rate = percent(row["rate_intra"])
            cat, code, _kind, label = spec
            notes = ["SST type %s FIPS %s. General intrastate rate."
                     % (row["jtype"], row["jfips"])]
            if row["rate_food"] is not None and row["rate_intra"] is not None \
                    and abs(row["rate_food"] - row["rate_intra"]) > 1e-8:
                notes.append("Food/drug rate differs (%s%% vs %s%% general)."
                             % (percent(row["rate_food"]), rate))
            if row["rate_inter"] is not None and row["rate_intra"] is not None \
                    and abs(row["rate_inter"] - row["rate_intra"]) > 1e-8:
                notes.append("Interstate rate differs (%s%%)."
                             % percent(row["rate_inter"]))
            if stale:
                notes.append("Source file period %s is stale; treat as historical "
                             "and corroborate." % meta.get("period"))
            findings.append({
                "geoid": geoid,
                "category": cat,
                "instrument_code": code,
                "label": label,
                "status": "levied" if (rate or 0) > 0 else "authorized_not_levied",
                "rate_value": rate if (rate or 0) > 0 else 0,
                "rate_unit": "percent",
                "rate_basis": "taxable retail sales (SST general intrastate rate)",
                "effective_date": row["begin"],
                "expiration_date": None if row["open_ended"] else row["end"],
                "fiscal_year": int(row["begin"][:4]) if row["begin"] else None,
                "confidence": "medium" if stale else "high",
                "extraction_method": "bulk_import",
                "researcher": "adapter:sst",
                "archive_file_id": archive_id,
                "source": source,
                "source_quote": _quote(row),
                "notes": " ".join(notes),
            })
        events.extend(_events_for(items, geoid, code, today))
    return findings, unmapped, events


def _quote(row):
    raw = row.get("rate_intra")
    if raw is None:
        return None
    return "%.5f" % raw if raw < 1 else str(raw)


def _events_for(items, geoid, code, today):
    out = []
    prev = None
    for row, spec, _geoid in items:
        rate = percent(row["rate_intra"])
        period = row["begin"] or ""
        if prev is None:
            out.append(_event(geoid, spec[0], code, "new", None, rate, period))
        else:
            pr = percent(prev[0]["rate_intra"])
            if pr != rate:
                ctype = "increase" if (rate or 0) > (pr or 0) else "decrease"
                out.append(_event(geoid, spec[0], code, ctype, pr, rate, period))
            elif row["open_ended"] and not prev[0].get("open_ended"):
                out.append(_event(geoid, spec[0], code, "extended", pr, rate, period))
        prev = (row, spec)
    last_row = items[-1][0]
    if last_row.get("end") and not last_row.get("open_ended") and last_row["end"] < today:
        out.append(_event(geoid, items[-1][1][0], code, "expired",
                          percent(last_row["rate_intra"]), None, last_row["end"]))
    return out


def _event(geoid, category, code, change_type, before, after, period):
    return {
        "geoid": geoid,
        "category": category,
        "instrument_code": code,
        "change_type": change_type,
        "rate_before": before,
        "rate_after": after,
        "rate_unit": "percent",
        "effective_period": period,
        "confidence": "high",
        "notes": "From SST rate file effective dates.",
    }


def write_events(conn, events):
    n = 0
    for e in events:
        if not e.get("effective_period"):
            continue
        try:
            conn.execute(
                "INSERT OR IGNORE INTO rate_change_event "
                "(geoid, category, instrument_code, change_type, rate_before, "
                "rate_after, rate_unit, effective_period, confidence, detected_at, "
                "notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (e["geoid"], e["category"], e["instrument_code"], e["change_type"],
                 e.get("rate_before"), e.get("rate_after"), e.get("rate_unit"),
                 e["effective_period"], e["confidence"], db.now(), e.get("notes")))
            n += 1
        except Exception:
            continue
    conn.commit()
    return n


def _iso_date(value):
    v = re.sub(r"\D", "", value or "")
    if len(v) != 8:
        return None
    if v.startswith("9999"):
        return "9999-12-31"
    return "%s-%s-%s" % (v[:4], v[4:6], v[6:])


def _state_geoid(conn, usps):
    row = conn.execute(
        "SELECT geoid FROM jurisdiction WHERE kind='state' AND state_usps=?",
        (usps,)).fetchone()
    return row["geoid"] if row else None
