"""Database connection, schema apply, and run logging."""

import os
import sqlite3
import json
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("TAX_DATABASE_DATA", ROOT)
DEFAULT_DB = os.path.join(DATA_DIR, "tax.db")
SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
CACHE_DIR = os.environ.get("TAX_DATABASE_CACHE", os.path.join(DATA_DIR, "cache"))
OUT_DIR = os.environ.get("TAX_DATABASE_OUT", os.path.join(DATA_DIR, "out"))
ARCHIVE_DIR = os.environ.get("TAX_DATABASE_ARCHIVE", os.path.join(DATA_DIR, "archive"))


def db_path():
    return os.environ.get("TAX_DATABASE_DB", DEFAULT_DB)


def now():
    # Space separator, not 'T': SQLite's datetime() emits the space form, and
    # string comparisons between the two silently fail ('T' sorts above ' ').
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat(sep=" ")


def today():
    return datetime.date.today().isoformat()


def connect(create=True, path=None, apply=True):
    """Open a connection. apply=False skips the schema pass for callers
    (the web collector) that already applied it once this process."""
    path = db_path() if path is None else path
    if path != ":memory:" and not create and not os.path.exists(path):
        raise SystemExit("no database at %s -- run `taxdb init` first" % path)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    if apply and (create or path == ":memory:" or os.path.exists(path)):
        apply_schema(conn)
    return conn


def apply_schema(conn):
    with open(SCHEMA) as fh:
        conn.executescript(fh.read())
    from . import fips
    fips.seed_crosswalk(conn)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Add columns/tables that CREATE IF NOT EXISTS cannot retrofit."""
    _ensure_column(conn, "source", "content_sha256", "TEXT")
    _ensure_column(conn, "source", "content_changed", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "tax_instrument", "source_quote", "TEXT")


def _ensure_column(conn, table, column, decl):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    if column not in cols:
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))


def init_db(path=None):
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    conn = connect(path=path)
    apply_schema(conn)
    return conn


class Run(object):
    """Context manager that records every mutating command in run_log."""

    def __init__(self, conn, command, args=None):
        self.conn = conn
        self.command = command
        self.args = args
        self.rows_in = 0
        self.rows_out = 0
        self.message = None
        self.id = None

    def __enter__(self):
        cur = self.conn.execute(
            "INSERT INTO run_log (command, args, started_at) VALUES (?,?,?)",
            (self.command, json.dumps(self.args, default=str), now()),
        )
        self.id = cur.lastrowid
        self.conn.commit()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute(
            "UPDATE run_log SET finished_at=?, rows_in=?, rows_out=?, ok=?, message=? "
            "WHERE id=?",
            (now(), self.rows_in, self.rows_out, 0 if exc else 1,
             ("%s: %s" % (exc_type.__name__, exc)) if exc else self.message, self.id),
        )
        self.conn.commit()
        return False


def get_or_create_source(conn, url, name, source_type="portal", authority_tier=4,
                         scope_geoid=None, publisher=None, notes=None):
    row = conn.execute(
        "SELECT id FROM source WHERE url=? AND scope_geoid IS ?", (url, scope_geoid)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO source (scope_geoid, name, url, source_type, authority_tier, "
        "publisher, notes) VALUES (?,?,?,?,?,?,?)",
        (scope_geoid, name, url, source_type, authority_tier, publisher, notes),
    )
    return cur.lastrowid
