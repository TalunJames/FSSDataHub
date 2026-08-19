"""Meter Claude spending and pause the collector at a dollar checkpoint.

Every Anthropic response reports exactly how many tokens it consumed; this
module prices that usage at published per-token rates and keeps a running
total in collector_setting. When another spend_stop_usd (default $100) has
accumulated since the last checkpoint, the collector pauses itself — both
continuous mode and the overnight schedule — and says so on the home page.
Pressing Start begins the next $100.

The figure is an estimate computed from the provider's own token counts at
the rates below; it is not a bill. Unknown model names are priced at the
Opus rate so the meter over-counts rather than under-counts, and only the
Anthropic provider is metered: the local model is free and other providers'
prices are not known here.
"""

import os

from taxdb import db

from . import extract, store

# $ per million tokens: (input, output). Batch API is half of both.
# Sonnet 5 is listed at its standard rate; while introductory pricing lasts
# the meter over-counts, which is the safe direction for a stop switch.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
FALLBACK_PRICE = (5.0, 25.0)

# Cache multipliers on the input rate.
CACHE_WRITE = 1.25
CACHE_READ = 0.10

TOTAL_KEY = "spend_total_usd"
ACK_KEY = "spend_ack_usd"
STEP_KEY = "spend_stop_usd"


def cost_usd(model, usage, batch=False):
    """Price one response's usage dict. usage keys follow the Messages API."""
    rate_in, rate_out = PRICES.get((model or "").strip(), FALLBACK_PRICE)
    usage = usage or {}

    def n(key):
        v = usage.get(key)
        return v if isinstance(v, (int, float)) else 0

    cost = (n("input_tokens") * rate_in
            + n("cache_creation_input_tokens") * rate_in * CACHE_WRITE
            + n("cache_read_input_tokens") * rate_in * CACHE_READ
            + n("output_tokens") * rate_out) / 1e6
    return cost / 2.0 if batch else cost


def record(conn, model, usage, batch=False, commit=True):
    """Add one response's cost to the running total. Returns the new total."""
    add = cost_usd(model, usage, batch=batch)
    if add <= 0:
        return total(conn)
    new = total(conn) + add
    store.put(conn, TOTAL_KEY, "%.6f" % new)
    if commit:
        conn.commit()
    return new


def total(conn):
    return store.as_float(store.get(conn, TOTAL_KEY, "0"), 0.0)


def over_budget(settings):
    """True when another spend_stop_usd has accrued since the last checkpoint.

    A step of 0 (or anything unparseable) turns the checkpoint off."""
    step = store.as_float(settings.get(STEP_KEY), 0.0)
    if step <= 0:
        return False
    spent = store.as_float(settings.get(TOTAL_KEY), 0.0)
    acked = store.as_float(settings.get(ACK_KEY), 0.0)
    return spent - acked >= step


def over_budget_db(conn):
    """Fresh read for callers whose settings snapshot may be stale."""
    return over_budget({
        STEP_KEY: store.get(conn, STEP_KEY, "0"),
        TOTAL_KEY: store.get(conn, TOTAL_KEY, "0"),
        ACK_KEY: store.get(conn, ACK_KEY, "0"),
    })


def pause(conn):
    """Stop everything that spends on its own, and start the next checkpoint.

    Turns off continuous mode AND the schedule: a hard stop that quietly
    resumed itself overnight would not be one. The acknowledgment is stamped
    now, so pressing Start runs the next full step. Returns the message the
    worker should show."""
    spent = total(conn)
    acked = store.as_float(store.get(conn, ACK_KEY, "0"), 0.0)
    step = store.as_float(store.get(conn, STEP_KEY, "0"), 0.0)
    since = max(0.0, spent - acked)
    store.put(conn, ACK_KEY, "%.6f" % spent)
    store.put(conn, "continuous_enabled", "0")
    store.put(conn, "schedule_enabled", "0")
    conn.commit()
    return ("Paused at the spending checkpoint: about $%.2f of Claude reading "
            "since the last go-ahead, roughly $%.2f total (estimated from "
            "token counts). Press Start for the next $%.0f." % (
                since, spent, step))


def _hook(provider, model, usage):
    """extract.USAGE_HOOK target: meter Anthropic responses as they happen."""
    if provider != "anthropic":
        return
    # The app always has its database by the time anything extracts. Never
    # create one from here: a test pointed at a temp database would otherwise
    # mint a stray tax.db at the default path.
    if not os.path.exists(db.db_path()):
        return
    conn = store.connect()
    try:
        record(conn, model, usage)
    finally:
        conn.close()


extract.USAGE_HOOK = _hook
