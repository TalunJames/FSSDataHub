"""Dated object store. Bytes now, parsing later.

Every archived file is immutable: same adapter + period + URL is unique,
but the same bytes in a later period are a new row. That is the property
the diff engine depends on -- without it, a quiet quarter looks like a
quarter you never fetched.
"""

import os
import hashlib

from . import db


def _safe_name(url_or_name):
    base = os.path.basename((url_or_name or "file").split("?")[0]) or "file"
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in base)[:120]


def store_path(adapter, period_label, sha256, filename):
    return os.path.join(
        db.ARCHIVE_DIR, adapter, period_label, "%s_%s" % (sha256[:16], filename)
    )


def put(conn, adapter, url, blob, period_label, period_start=None, period_end=None,
        source_id=None, content_type=None, filename=None):
    """Write bytes to the archive and insert an archive_file row.

    Returns (archive_file_id, sha256, path, created). created is False when
    this adapter/period/url was already archived (the existing row is returned).
    """
    sha = hashlib.sha256(blob).hexdigest()
    filename = _safe_name(filename or url)
    path = store_path(adapter, period_label, sha, filename)

    existing = conn.execute(
        "SELECT id, sha256, store_path FROM archive_file "
        "WHERE adapter=? AND period_label=? AND url=?",
        (adapter, period_label, url),
    ).fetchone()
    if existing and existing["sha256"] == sha:
        return existing["id"], existing["sha256"], existing["store_path"], False
    if existing:
        # Same key, different bytes: the document changed since it was last
        # archived. Rows are immutable and the base key is taken, so the new
        # bytes go under a content-versioned label — returning the stale row
        # here meant a re-crawled rate PDF's evidence trail pointed at the
        # old rates forever.
        period_label = "%s@%s" % (period_label, sha[:12])
        path = store_path(adapter, period_label, sha, filename)
        versioned = conn.execute(
            "SELECT id, sha256, store_path FROM archive_file "
            "WHERE adapter=? AND period_label=? AND url=?",
            (adapter, period_label, url),
        ).fetchone()
        if versioned:
            return versioned["id"], versioned["sha256"], versioned["store_path"], False

    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(blob)

    cur = conn.execute(
        "INSERT INTO archive_file (source_id, adapter, url, period_label, "
        "period_start, period_end, sha256, byte_size, content_type, store_path, "
        "retrieved_at, parse_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (source_id, adapter, url, period_label, period_start, period_end,
         sha, len(blob), content_type, path, db.now(), "pending"),
    )
    conn.commit()
    return cur.lastrowid, sha, path, True


def list_files(conn, adapter=None):
    sql = ("SELECT id, adapter, period_label, url, sha256, byte_size, "
           "parse_status, retrieved_at FROM archive_file")
    params = []
    if adapter:
        sql += " WHERE adapter=?"
        params.append(adapter)
    sql += " ORDER BY adapter, period_label, id"
    return conn.execute(sql, params).fetchall()
