"""Adapter contract.

An adapter turns one official, machine-readable source into validated
findings. Everything a state publishes as a downloadable rate table should
become an adapter -- it is the difference between researching 3,000 counties
by hand and researching the 30 or so that no state file covers.

Subclass, set the class attributes, implement `fetch` and `parse`, and
register it in adapters/__init__.py.
"""

import hashlib
import re
import urllib.request

from .. import db, archive
from .. import geocode


class Adapter(object):
    key = None
    state = None
    categories = ()
    url = None
    source_name = None
    source_type = "agency_table"
    authority_tier = 2
    description = ""
    period_label = None

    def fetch(self):
        req = urllib.request.Request(
            self.url, headers={"User-Agent": "tax-database/0.2"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()

    def parse(self, conn, blob):
        """Return (findings, unmapped).

        findings -- list of dicts matching the ingest schema
        unmapped -- list of (raw_name, reason) for rows deliberately skipped
        """
        raise NotImplementedError

    def run(self, conn, dry_run=False, archive_only=False, states=None):
        blob = self.fetch()
        sha = hashlib.sha256(blob).hexdigest()

        source_id = db.get_or_create_source(
            conn, self.url, self.source_name or self.key,
            source_type=self.source_type, authority_tier=self.authority_tier,
            scope_geoid=self._state_fips(conn), notes=self.description)

        conn.execute(
            "INSERT OR IGNORE INTO raw_document (source_id, url, sha256, byte_size, "
            "retrieved_at) VALUES (?,?,?,?,?)",
            (source_id, self.url, sha, len(blob), db.now()))

        archive_id = None
        if self.period_label:
            archive_id, sha, _, _ = archive.put(
                conn, self.key, self.url, blob, self.period_label,
                source_id=source_id)

        conn.commit()

        if archive_only:
            return {"written": 0, "rejected": 0, "unmapped": [], "errors": [],
                    "sha256": sha, "archive_file_id": archive_id}

        findings, unmapped = self.parse(conn, blob)
        for f in findings:
            f.setdefault("source", {
                "url": self.url,
                "name": self.source_name or self.key,
                "source_type": self.source_type,
                "authority_tier": self.authority_tier,
            })
            f.setdefault("extraction_method", "bulk_import")
            f.setdefault("researcher", "adapter:%s" % self.key)
            f.setdefault("retrieved_at", db.today())
            if archive_id:
                f.setdefault("archive_file_id", archive_id)

        doc = {"schema_version": "1.0", "researcher": "adapter:%s" % self.key,
               "extraction_method": "bulk_import", "findings": findings}

        from .. import ingest
        res = ingest.load_doc(conn, doc, dry_run=dry_run, label="adapter:%s" % self.key)
        res["unmapped"] = unmapped
        res["sha256"] = sha
        res["archive_file_id"] = archive_id
        return res

    def _state_fips(self, conn):
        if not self.state:
            return None
        row = conn.execute("SELECT state_fips FROM jurisdiction WHERE kind='state' "
                           "AND state_usps=?", (self.state,)).fetchone()
        return row["state_fips"] if row else None


_SUFFIX = re.compile(
    r"\s+(city and borough|and borough|census area|municipality|borough|county|"
    r"parish|city|town|village|CDP|township|charter township|plantation|gore)$",
    re.I)


def normalize(name):
    """Reduce a jurisdiction name to a comparable key.

    State rate tables print 'OTHELLO'; the Census prints 'Othello city'.
    Repeated suffix stripping handles 'Nome city and borough' style names.
    """
    s = (name or "").strip()
    for _ in range(3):
        new = _SUFFIX.sub("", s).strip()
        if new == s:
            break
        s = new
    s = s.upper().replace("&", "AND").replace(".", "")
    s = re.sub(r"\bST\b", "SAINT", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_lookup(conn, state, kinds=("county", "place")):
    """{(kind, normalized_name): [geoid, ...]} for one state."""
    look = {}
    rows = conn.execute(
        "SELECT geoid, name, kind FROM jurisdiction WHERE state_usps=? "
        "AND kind IN (%s)" % ",".join("?" * len(kinds)),
        [state] + list(kinds)).fetchall()
    for r in rows:
        look.setdefault((r["kind"], normalize(r["name"])), []).append(r["geoid"])
    return look


def resolve(look, kind, name, state=None, use_geocoder=False):
    """Return (geoid, None) or (None, reason)."""
    hits = look.get((kind, normalize(name)), [])
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        if use_geocoder and state:
            geoid = geocode.lookup(name, state, kind)
            if geoid:
                return geoid, None
        return None, "no Census %s named %r" % (kind, name)
    return None, "ambiguous: %d %ss named %r" % (len(hits), kind, name)
