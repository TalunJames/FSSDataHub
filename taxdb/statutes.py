"""Open US Law statute snapshots, stored locally for grep-then-read.

Secondary compilation (CC BY 4.0 data). Cite the statute and keep source_url
as the primary. Spot-check a sample per state against the official site
before trusting a state wholesale.

Requires pyarrow only for the fetch step. After load, grep is SQLite.
"""

import json

from . import db
from .seed import fetch

MANIFEST_URL = "https://oss-data-us.vaquill.ai/index.json"
SNAPSHOT = "v2026.08"
PARQUET_URL = "https://oss-data-us.vaquill.ai/%s/us_%s_statutes.parquet"

DEFAULT_TERMS = (
    "sales tax", "use tax", "lodging", "hotel", "transient",
    "property tax", "millage", "levy", "income tax", "occupational",
    "earnings tax", "franchise", "excise", "voter approval", "charter",
)


class StatutesError(Exception):
    pass


class NotPublished(StatutesError):
    """This snapshot carries no statutes for that state.

    Distinct from a download failure: retrying will not help, and the caller
    should stop asking rather than burning a failed run per cooldown. The
    v2026.08 snapshot publishes statutes for 50 of 52 jurisdictions; Georgia
    and North Carolina have constitutions and guidance but no statute corpus,
    and a bare "HTTP Error 404" gave no way to tell that from an outage.
    """


def parquet_url(usps, snapshot=SNAPSHOT):
    return PARQUET_URL % (snapshot, usps.lower())


# The manifest is a 60KB index of every file in the snapshot. Read at most
# once per process: it changes when the snapshot does, and the snapshot is
# pinned in this module.
_manifest_cache = {}


def manifest(snapshot=SNAPSHOT, force=False):
    """The snapshot's file index, or None when it cannot be read.

    A manifest we cannot fetch must never be the reason a download is
    refused, so every caller treats None as "no opinion".
    """
    if not force and snapshot in _manifest_cache:
        return _manifest_cache[snapshot]
    got = _read_manifest(force=force)
    # `fetch` caches by file name, and the index is always index.json, so a
    # snapshot bump would otherwise be answered from the previous snapshot's
    # index. Disagreement means the copy on disk is stale: fetch it again.
    if got and got.get("version") and got["version"] != snapshot and not force:
        got = _read_manifest(force=True)
    _manifest_cache[snapshot] = got
    return got


def _read_manifest(force=False):
    try:
        _path, _sha, blob = fetch(MANIFEST_URL, force=force)
        data = json.loads(blob.decode("utf-8"))
        return {"files": {f.get("file") for f in data.get("files") or []},
                "version": data.get("version")}
    except Exception:
        return None


def published(usps, snapshot=SNAPSHOT):
    """Whether the snapshot has a statute corpus for this state.

    True when the manifest lists it, False when the manifest is readable and
    does not, and True when there is no manifest to consult — an unreachable
    index is not evidence of absence.
    """
    man = manifest(snapshot)
    if not man or not man.get("files"):
        return True
    return ("us_%s_statutes.parquet" % usps.lower()) in man["files"]


def other_files(usps, snapshot=SNAPSHOT):
    """What the snapshot does carry for a state, for the error message."""
    man = manifest(snapshot)
    if not man or not man.get("files"):
        return []
    prefix = "us_%s_" % usps.lower()
    return sorted(f[len(prefix):].replace(".parquet", "")
                  for f in man["files"] if f.startswith(prefix))


def fetch_state(conn, usps, force=False, snapshot=SNAPSHOT):
    """Download one state's parquet and load statute_section."""
    usps = usps.upper()
    if not published(usps, snapshot):
        have = other_files(usps, snapshot)
        raise NotPublished(
            "%s has no statute corpus in snapshot %s%s" % (
                usps, snapshot,
                "; it does publish %s" % ", ".join(have) if have else ""))
    url = parquet_url(usps, snapshot)
    path, sha, blob = fetch(url, force=force)
    source_id = db.get_or_create_source(
        conn, url, "Open US Law %s statutes %s" % (usps, snapshot),
        source_type="secondary", authority_tier=3,
        publisher="Open US Law / Vaquill",
        notes="Secondary compilation. Cite the statute; keep source_url as primary.")
    conn.execute(
        "INSERT OR IGNORE INTO raw_document (source_id, url, sha256, byte_size, "
        "cache_path, retrieved_at) VALUES (?,?,?,?,?,?)",
        (source_id, url, sha, len(blob), path, db.now()))

    rows = read_parquet(path)
    conn.execute(
        "DELETE FROM statute_section WHERE state_usps=? AND snapshot=?",
        (usps, snapshot))
    n = 0
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO statute_section "
            "(state_usps, snapshot, citation, section_title, act_status, text, "
            "source_url, last_amended_year) VALUES (?,?,?,?,?,?,?,?)",
            (usps, snapshot, r.get("citation"), r.get("section_title"),
             r.get("act_status"), r.get("text"), r.get("source_url"),
             _year(r.get("last_amended_year"))))
        n += 1
    conn.commit()
    return {"written": n, "sha256": sha, "path": path, "snapshot": snapshot}


def read_parquet(path):
    try:
        import pyarrow.parquet as pq
    except ImportError:
        raise StatutesError(
            "reading Open US Law parquet needs pyarrow — "
            "pip install pyarrow  (already in collector requirements.txt)")
    table = pq.read_table(path, columns=[
        "citation", "section_title", "act_status", "text", "source_url",
        "last_amended_year",
    ])
    cols = {name: table.column(name).to_pylist() for name in table.column_names}
    n = len(next(iter(cols.values()), []))
    out = []
    for i in range(n):
        out.append({k: (None if cols[k][i] is None else cols[k][i]) for k in cols})
    return out


def grep(conn, usps, terms, limit=25):
    """Return matching sections. Every term is OR'd; empty terms use DEFAULT_TERMS."""
    usps = usps.upper()
    terms = [t for t in (terms or []) if t and t.strip()] or list(DEFAULT_TERMS)
    clauses, params = [], [usps]
    for t in terms:
        like = "%" + t.strip() + "%"
        clauses.append("(section_title LIKE ? OR text LIKE ? OR citation LIKE ?)")
        params.extend([like, like, like])
    sql = ("SELECT citation, section_title, act_status, source_url, "
           "substr(text, 1, 800) AS excerpt, last_amended_year "
           "FROM statute_section WHERE state_usps=? AND (%s) "
           "ORDER BY citation LIMIT ?" % " OR ".join(clauses))
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def for_packet(conn, usps, categories, limit=8):
    from .vocab import CATEGORIES
    terms = []
    extra = {
        "sales_use": ["sales tax", "use tax", "gross receipts", "local option"],
        "lodging_meals": ["lodging", "hotel", "transient", "occupancy", "meals"],
        "property": ["property tax", "millage", "levy limit", "assessment"],
        "income_payroll": ["income tax", "earnings tax", "occupational"],
        "other_levy": ["franchise", "excise", "transfer tax", "utility tax"],
        # The framework pass reads for thresholds and caps, not rates.
        "framework": ["three-fifths", "two-thirds", "sixty percent",
                      "majority of the electors", "shall not exceed",
                      "submitted to the electors", "special election",
                      "levy limit", "home rule"],
        "elections": ["canvass", "certif", "submitted to the electors",
                      "ballot", "special election"],
    }
    for c in categories or CATEGORIES:
        terms.extend(extra.get(c, []))
    rows = grep(conn, usps, terms, limit=limit)
    return [dict(r) for r in rows]


def _year(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
