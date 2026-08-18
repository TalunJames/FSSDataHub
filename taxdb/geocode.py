"""Census geocoder fallback for adapter rows that have a name but no GEOID.

The geographies API does not require a key. It is the authoritative match
for a Census-GEOID-keyed database. Call it only after the local name lookup
fails -- it is a network round-trip.
"""

import json
import urllib.parse
import urllib.request

from .fips import FIPS_TO_USPS

GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress"

_cache = {}


class GeocodeError(Exception):
    pass


def lookup(name, state, kind=None, timeout=20):
    """Return a GEOID or None. kind is county|place|None (accept either)."""
    key = (normalize_query(name), (state or "").upper(), kind)
    if key in _cache:
        return _cache[key]
    geoid = _lookup(name, state, kind, timeout)
    _cache[key] = geoid
    return geoid


def normalize_query(name):
    return " ".join((name or "").split()).strip()


def _lookup(name, state, kind, timeout):
    q = normalize_query(name)
    if not q or not state:
        return None
    address = "%s, %s" % (q, state.upper())
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }
    url = GEOCODER + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "tax-database/0.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    matches = ((data.get("result") or {}).get("addressMatches")) or []
    if not matches:
        return None
    geos = (matches[0].get("geographies")) or {}
    if kind == "county":
        rows = geos.get("Counties") or []
        if rows:
            return rows[0].get("GEOID")
        return None
    places = geos.get("Incorporated Places") or geos.get("IncorporatedPlaces") or []
    if places and kind in (None, "place"):
        return places[0].get("GEOID")
    if kind in (None, "county"):
        counties = geos.get("Counties") or []
        if counties:
            return counties[0].get("GEOID")
    return None


def geoid_state_usps(geoid):
    if not geoid or len(geoid) < 2:
        return None
    return FIPS_TO_USPS.get(geoid[:2])
