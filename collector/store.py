"""Settings, schema apply, and crawl-run helpers."""

import os
import threading

from taxdb import db
from .settings import DEFAULTS, SECRET_KEYS

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

_schema_done = set()
_schema_lock = threading.Lock()


def apply_schema(conn):
    with open(SCHEMA) as fh:
        conn.executescript(fh.read())
    _migrate(conn)
    for key, value in DEFAULTS.items():
        conn.execute(
            "INSERT OR IGNORE INTO collector_setting (key, value, updated_at) "
            "VALUES (?,?,?)", (key, value, db.now()))
    _pull_env_secrets(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Rebuilds that CREATE IF NOT EXISTS cannot retrofit."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='crawl_run'"
    ).fetchone()
    if row and "mode IN (" in (row["sql"] or ""):
        # Old databases constrain crawl_run.mode to the original six modes,
        # which rejects fetch/cog/statutes runs. Rebuild without the CHECK.
        # legacy_alter_table keeps the rename from rewriting crawl_page's FK.
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("ALTER TABLE crawl_run RENAME TO crawl_run_old")
        conn.execute(
            "CREATE TABLE crawl_run ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " mode TEXT NOT NULL,"
            " status TEXT NOT NULL CHECK (status IN ('running','ok','stopped','failed')),"
            " provider TEXT, filter_states TEXT,"
            " items_claimed INTEGER NOT NULL DEFAULT 0,"
            " pages_fetched INTEGER NOT NULL DEFAULT 0,"
            " findings_written INTEGER NOT NULL DEFAULT 0,"
            " started_at TEXT NOT NULL, finished_at TEXT, message TEXT)")
        conn.execute("INSERT INTO crawl_run SELECT * FROM crawl_run_old")
        conn.execute("DROP TABLE crawl_run_old")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cr_started ON crawl_run(started_at DESC)")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()

    # One-time model upgrade: installs that never entered an Anthropic key
    # still carry the old default model. Never touches a key the user has set,
    # and the marker keeps it from ever re-running.
    done = conn.execute(
        "SELECT 1 FROM collector_setting WHERE key='model_default_migrated'").fetchone()
    if not done:
        anth_model = conn.execute(
            "SELECT value FROM collector_setting WHERE key='anthropic_model'").fetchone()
        anth_key = conn.execute(
            "SELECT value FROM collector_setting WHERE key='anthropic_api_key'").fetchone()
        if (anth_model and anth_model["value"] == "claude-haiku-4-5"
                and (anth_key is None or not (anth_key["value"] or "").strip())):
            conn.execute(
                "UPDATE collector_setting SET value=?, updated_at=? WHERE key='anthropic_model'",
                (DEFAULTS["anthropic_model"], db.now()))
        conn.execute(
            "INSERT OR IGNORE INTO collector_setting (key, value, updated_at) "
            "VALUES ('model_default_migrated','1',?)", (db.now(),))


def _pull_env_secrets(conn):
    """Fill empty secret rows from the environment on first boot."""
    env_map = {
        "openai_api_key": "OPENAI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "llama_api_key": "LLAMA_API_KEY",
        "llama_base_url": "LLAMA_BASE_URL",
        "openai_base_url": "OPENAI_BASE_URL",
        "openai_model": "OPENAI_MODEL",
        "anthropic_model": "ANTHROPIC_MODEL",
        "llama_model": "LLAMA_MODEL",
        "provider": "COLLECTOR_PROVIDER",
        "search_api_key": "SEARCH_API_KEY",
    }
    for key, env in env_map.items():
        raw = os.environ.get(env)
        if not raw:
            continue
        row = conn.execute(
            "SELECT value FROM collector_setting WHERE key=?", (key,)).fetchone()
        if row and (row["value"] or "").strip():
            continue
        conn.execute(
            "UPDATE collector_setting SET value=?, updated_at=? WHERE key=?",
            (raw.strip(), db.now(), key))


def connect(create=True):
    """Open a connection, applying both schemas once per process per path.

    apply_schema is idempotent but not free (executescript + defaults +
    migrations), and this runs on every request thread on a NAS.
    """
    path = db.db_path()
    if path in _schema_done:
        return db.connect(create=create, apply=False)
    with _schema_lock:
        conn = db.connect(create=create)
        apply_schema(conn)
        _schema_done.add(path)
        return conn


def get_all(conn):
    rows = conn.execute("SELECT key, value FROM collector_setting").fetchall()
    out = dict(DEFAULTS)
    out.update({r["key"]: r["value"] if r["value"] is not None else "" for r in rows})
    return out


def get(conn, key, default=None):
    row = conn.execute(
        "SELECT value FROM collector_setting WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULTS.get(key, default)
    return row["value"]


def put(conn, key, value):
    conn.execute(
        "INSERT INTO collector_setting (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, "" if value is None else str(value), db.now()))


def put_many(conn, mapping):
    for k, v in mapping.items():
        put(conn, k, v)
    conn.commit()


def as_bool(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def as_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_float(value, default=0.0):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def sanitize_updates(current, body):
    """Filter a settings POST down to safe writes.

    Unknown keys are dropped. Masked secrets round-trip from the form as
    ****abcd; real keys never contain '*', so any '*' means "unchanged" —
    writing it back would destroy the stored key.
    """
    updates = {}
    for k, v in body.items():
        if k not in current and k not in SECRET_KEYS:
            continue
        if k in SECRET_KEYS and isinstance(v, str) and "*" in v:
            continue
        updates[k] = v if v is None else str(v)
    return updates


def mask(settings):
    """Return a copy with secret values redacted for the UI/API."""
    out = dict(settings)
    for k in SECRET_KEYS:
        v = out.get(k) or ""
        if v:
            out[k] = ("*" * max(0, len(v) - 4)) + v[-4:]
            out[k + "_set"] = True
        else:
            out[k] = ""
            out[k + "_set"] = False
    return out


def start_run(conn, mode, provider=None, filter_states=None):
    cur = conn.execute(
        "INSERT INTO crawl_run (mode, status, provider, filter_states, started_at) "
        "VALUES (?,?,?,?,?)",
        (mode, "running", provider, filter_states, db.now()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, status, message=None, items=0, pages=0, findings=0):
    conn.execute(
        "UPDATE crawl_run SET status=?, finished_at=?, message=?, "
        "items_claimed=?, pages_fetched=?, findings_written=? WHERE id=?",
        (status, db.now(), message, items, pages, findings, run_id))
    conn.commit()


def bump_run(conn, run_id, items=0, pages=0, findings=0):
    conn.execute(
        "UPDATE crawl_run SET items_claimed=items_claimed+?, "
        "pages_fetched=pages_fetched+?, findings_written=findings_written+? "
        "WHERE id=?", (items, pages, findings, run_id))
    conn.commit()
