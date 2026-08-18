"""Settings, schema apply, and crawl-run helpers."""

import os

from taxdb import db
from .settings import DEFAULTS, SECRET_KEYS

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def apply_schema(conn):
    with open(SCHEMA) as fh:
        conn.executescript(fh.read())
    for key, value in DEFAULTS.items():
        conn.execute(
            "INSERT OR IGNORE INTO collector_setting (key, value, updated_at) "
            "VALUES (?,?,?)", (key, value, db.now()))
    _pull_env_secrets(conn)
    conn.commit()
    return conn


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
    conn = db.connect(create=create)
    apply_schema(conn)
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
