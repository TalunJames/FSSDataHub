"""Seed the jurisdiction registry from Census bulk files.

Uses www2.census.gov bulk downloads rather than api.census.gov, because the
API now requires a key and the bulk files do not. Geography comes from the
Gazetteer; population comes from the Population Estimates Program.
"""

import os
import io
import csv
import zipfile
import hashlib
import urllib.request

from . import db
from .fips import STATE_NAMES, FIPS_TO_USPS

GAZ_YEAR = "2025"
POP_YEAR = 2025

GAZ_BASE = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/%s_Gazetteer/" % GAZ_YEAR
GAZ_FILES = {
    "county": GAZ_BASE + "%s_Gaz_counties_national.zip" % GAZ_YEAR,
    "place":  GAZ_BASE + "%s_Gaz_place_national.zip" % GAZ_YEAR,
    "mcd":    GAZ_BASE + "%s_Gaz_cousubs_national.zip" % GAZ_YEAR,
}

POP_COUNTY = ("https://www2.census.gov/programs-surveys/popest/datasets/"
              "2020-2025/counties/totals/co-est2025-alldata.csv")
POP_SUB = ("https://www2.census.gov/programs-surveys/popest/datasets/"
           "2020-2025/cities/totals/sub-est2025.csv")


def fetch(url, force=False):
    """Download to the local cache and return (path, sha256, bytes)."""
    os.makedirs(db.CACHE_DIR, exist_ok=True)
    fname = url.rsplit("/", 1)[-1]
    path = os.path.join(db.CACHE_DIR, fname)
    if os.path.exists(path) and not force:
        with open(path, "rb") as fh:
            blob = fh.read()
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "tax-database/0.2"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            blob = resp.read()
        with open(path, "wb") as fh:
            fh.write(blob)
    return path, hashlib.sha256(blob).hexdigest(), blob


def _record_document(conn, url, path, sha, blob, source_id):
    conn.execute(
        "INSERT OR IGNORE INTO raw_document (source_id, url, sha256, content_type, "
        "byte_size, cache_path, retrieved_at) VALUES (?,?,?,?,?,?,?)",
        (source_id, url, sha, None, len(blob), path, db.now()),
    )


def _gaz_rows(blob):
    text = blob.decode("latin-1")
    inner = zipfile.ZipFile(io.BytesIO(blob)) if blob[:2] == b"PK" else None
    if inner:
        text = inner.read(inner.namelist()[0]).decode("latin-1")
    rdr = csv.DictReader(io.StringIO(text), delimiter="|")
    for row in rdr:
        yield {(k or "").strip(): (v or "").strip() for k, v in row.items()}


def load_populations(conn, force=False):
    """Return {geoid: population} for counties, places, and MCDs."""
    pops = {}

    path, sha, blob = fetch(POP_COUNTY, force)
    sid = db.get_or_create_source(
        conn, POP_COUNTY, "Census PEP county population estimates",
        source_type="bulk_file", authority_tier=2, publisher="U.S. Census Bureau")
    _record_document(conn, POP_COUNTY, path, sha, blob, sid)
    rdr = csv.DictReader(io.StringIO(blob.decode("latin-1")))
    for r in rdr:
        if r["SUMLEV"] == "040":
            pops[r["STATE"]] = int(r["POPESTIMATE%d" % POP_YEAR])
        elif r["SUMLEV"] == "050":
            pops[r["STATE"] + r["COUNTY"]] = int(r["POPESTIMATE%d" % POP_YEAR])

    path, sha, blob = fetch(POP_SUB, force)
    sid = db.get_or_create_source(
        conn, POP_SUB, "Census PEP subcounty population estimates",
        source_type="bulk_file", authority_tier=2, publisher="U.S. Census Bureau")
    _record_document(conn, POP_SUB, path, sha, blob, sid)
    rdr = csv.DictReader(io.StringIO(blob.decode("latin-1")))
    for r in rdr:
        try:
            pop = int(r["POPESTIMATE%d" % POP_YEAR])
        except (ValueError, KeyError):
            continue
        if r["SUMLEV"] in ("162", "170"):
            pops[r["STATE"] + r["PLACE"]] = pop
        elif r["SUMLEV"] == "061":
            pops[r["STATE"] + r["COUNTY"] + r["COUSUB"]] = pop
    return pops


def seed(conn, kinds=("county", "place"), force=False, active_only=True):
    """Load jurisdictions. Returns a per-kind count dict."""
    pops = load_populations(conn, force)
    counts = {"state": _seed_states(conn, pops)}

    for kind in kinds:
        url = GAZ_FILES[kind]
        path, sha, blob = fetch(url, force)
        sid = db.get_or_create_source(
            conn, url, "Census %s Gazetteer (%s)" % (GAZ_YEAR, kind),
            source_type="bulk_file", authority_tier=2, publisher="U.S. Census Bureau")
        _record_document(conn, url, path, sha, blob, sid)

        n = 0
        for row in _gaz_rows(blob):
            geoid = row["GEOID"]
            usps = row["USPS"]
            fips = geoid[:2]
            funcstat = row.get("FUNCSTAT") or ("A" if kind == "county" else None)

            # FUNCSTAT 'A' means an active, functioning government. Anything
            # else (mostly CDPs and defunct entities) has no taxing power and
            # would inflate the ledger with unworkable rows.
            if active_only and kind in ("place", "mcd") and funcstat != "A":
                continue

            if kind == "county":
                parent, county_fips = fips, geoid
            elif kind == "place":
                parent, county_fips = fips, None
            else:
                parent, county_fips = geoid[:5], geoid[:5]

            name = row["NAME"]
            coterminous = 1 if (kind == "county" and usps == "VA"
                                and "city" in name.lower()) else 0

            # UPSERT, not REPLACE: an annual Gazetteer refresh updates only the
            # Census-sourced columns and leaves hand-recorded notes alone.
            conn.execute(
                "INSERT INTO jurisdiction (geoid, kind, name, state_usps, "
                "state_fips, county_fips, parent_geoid, lsad, funcstat, population, "
                "population_year, land_sqmi, lat, lon, coterminous) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(geoid) DO UPDATE SET "
                "kind=excluded.kind, name=excluded.name, "
                "state_usps=excluded.state_usps, state_fips=excluded.state_fips, "
                "county_fips=excluded.county_fips, parent_geoid=excluded.parent_geoid, "
                "lsad=excluded.lsad, funcstat=excluded.funcstat, "
                "population=excluded.population, population_year=excluded.population_year, "
                "land_sqmi=excluded.land_sqmi, lat=excluded.lat, lon=excluded.lon, "
                "coterminous=excluded.coterminous",
                (geoid, kind, name, usps, fips, county_fips, parent,
                 row.get("LSAD"), funcstat, pops.get(geoid), POP_YEAR,
                 _f(row.get("ALAND_SQMI")), _f(row.get("INTPTLAT")),
                 _f(row.get("INTPTLONG")), coterminous),
            )
            n += 1
        counts[kind] = n
        conn.commit()

    return counts


def _seed_states(conn, pops=None):
    pops = pops or {}
    for fips, usps in FIPS_TO_USPS.items():
        conn.execute(
            "INSERT INTO jurisdiction (geoid, kind, name, state_usps, "
            "state_fips, parent_geoid, funcstat, population, population_year) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(geoid) DO UPDATE SET "
            "kind=excluded.kind, name=excluded.name, "
            "state_usps=excluded.state_usps, state_fips=excluded.state_fips, "
            "parent_geoid=excluded.parent_geoid, funcstat=excluded.funcstat, "
            "population=excluded.population, "
            "population_year=excluded.population_year",
            (fips, "state", STATE_NAMES.get(usps, usps), usps, fips, None, "A",
             pops.get(fips), POP_YEAR if fips in pops else None),
        )
        conn.execute(
            "INSERT OR IGNORE INTO state_profile (state_usps, state_name) VALUES (?,?)",
            (usps, STATE_NAMES.get(usps, usps)),
        )
    conn.commit()
    return len(FIPS_TO_USPS)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
