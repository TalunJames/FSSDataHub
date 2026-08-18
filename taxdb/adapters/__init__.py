"""Adapter registry.

Subclass Adapter, implement parse, and add the class to ADAPTERS.
It becomes available as `taxdb fetch <key>`.
"""

from .sst import SstRates

ADAPTERS = {a.key: a for a in [
    SstRates,
]}


def get(key):
    if key not in ADAPTERS:
        raise SystemExit("unknown adapter %r; available: %s"
                         % (key, ", ".join(sorted(ADAPTERS)) or "(none yet)"))
    return ADAPTERS[key]()
