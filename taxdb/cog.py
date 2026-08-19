"""Census of Governments 2022 individual unit finance files.

Collections, not rates. A zero hotel/sales/property collection is a prior
that the jurisdiction likely does not levy that tax -- confirm before
writing authorized_not_levied. Amounts populate revenue_base as USD.

2022 public-use layout (32-char records):
  1-12  ID (Census state, type, Census county, unit)
  13-15 item code
  16-27 amount in thousands of dollars
  28-31 year
  32    imputation flag

The ID is a Census GOVS identifier: its state code is the gapless 01-51
*Census* sequence, not FIPS (OH is 36, not 39), and its county code is the
Census sequential county number, not the odd-numbered FIPS code. Treating
either as FIPS files collections against the wrong state or county — the
exact trap `fips.py` and the census_gid_crosswalk exist to prevent. States go
through `census_state_to_fips`; counties are matched by the PID directory's
own name against the seeded jurisdiction list; municipalities use the PID's
FIPS place code, which genuinely is FIPS.
"""

import io
import re
import zipfile

from . import db
from .fips import census_state_to_fips
from .seed import fetch

COG_ZIP = ("https://www2.census.gov/programs-surveys/gov-finances/tables/"
           "2022/2022_Individual_Unit_File.zip")
DAT_NAME = "2022_Individual_Unit_files/2022FinEstDAT_07152026modp.txt"
PID_NAME = "2022_Individual_Unit_files/Fin_PID_2022.txt"
VINTAGE = "cog2022"
FISCAL_YEAR = 2022

# Item code -> (base_type, category label for notes)
TAX_ITEMS = {
    "T01": ("collections_property", "property"),
    "T09": ("collections_sales_use", "sales_use"),
    "T19": ("collections_lodging_meals", "lodging_meals"),
    "T40": ("collections_income_payroll", "income_payroll"),
}

def load(conn, force=False):
    """Download the 2022 unit file, map to GEOIDs, write revenue_base."""
    path, sha, blob = fetch(COG_ZIP, force=force)
    source_id = db.get_or_create_source(
        conn, COG_ZIP, "Census of Governments 2022 individual unit finance file",
        source_type="bulk_file", authority_tier=2,
        publisher="U.S. Census Bureau",
        notes="Collections in thousands of dollars, not rates. Levy/no-levy prior only.")
    conn.execute(
        "INSERT OR IGNORE INTO raw_document (source_id, url, sha256, byte_size, "
        "cache_path, retrieved_at) VALUES (?,?,?,?,?,?)",
        (source_id, COG_ZIP, sha, len(blob), path, db.now()))

    z = zipfile.ZipFile(io.BytesIO(blob))
    pid = parse_pid(z.read(PID_NAME).decode("latin-1"))
    dat = z.read(DAT_NAME).decode("latin-1")

    counties = county_lookup(conn)
    conn.execute("DELETE FROM revenue_base WHERE vintage=?", (VINTAGE,))
    written, unmapped = 0, 0
    for rec in parse_dat(dat):
        if rec["item"] not in TAX_ITEMS:
            continue
        geoid = geoid_for(rec, pid, counties)
        if not geoid:
            unmapped += 1
            continue
        if not conn.execute("SELECT 1 FROM jurisdiction WHERE geoid=?",
                            (geoid,)).fetchone():
            unmapped += 1
            continue
        base_type, category = TAX_ITEMS[rec["item"]]
        usd = (rec["amount"] or 0) * 1000
        conn.execute(
            "INSERT OR REPLACE INTO revenue_base "
            "(geoid, fiscal_year, base_type, base_value, base_unit, is_estimated, "
            "estimation_method, source_id, extraction_method, vintage, "
            "is_current_vintage, confidence, retrieved_at, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (geoid, FISCAL_YEAR, base_type, usd, "usd", 1,
             "census_cog_collections", source_id, "bulk_import", VINTAGE, 1,
             "medium", db.now(),
             "Census CoG 2022 item %s (%s) collections, thousands×1000. "
             "Not a tax rate. $0 is a no-levy prior to confirm."
             % (rec["item"], category)))
        written += 1
    conn.commit()
    return {"written": written, "unmapped": unmapped, "sha256": sha}


def parse_dat(text):
    for line in text.splitlines():
        if len(line) < 32:
            continue
        ident = line[0:12]
        item = line[12:15].strip()
        amt_s = line[15:27].strip()
        year_s = line[27:31].strip()
        flag = line[31:32]
        try:
            amount = int(amt_s) if amt_s else 0
        except ValueError:
            continue
        yield {
            "id": ident,
            "fips": ident[0:2],
            "gtype": ident[2:3],
            "county": ident[3:6],
            "unit": ident[6:12],
            "item": item,
            "amount": amount,
            "year": year_s,
            "flag": flag,
        }


def parse_pid(text):
    """id12 -> {name, place_fips, kind}."""
    out = {}
    for line in text.splitlines():
        if len(line) < 116:
            continue
        ident = line[0:12]
        out[ident] = {
            "name": line[12:76].strip(),
            "county_name": line[76:111].strip(),
            "place_fips": line[111:116].strip(),
            "gtype": ident[2:3],
        }
    return out


def _norm_name(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def county_lookup(conn):
    """(state_fips, normalized name) -> county geoid, from the seeded list.

    The GOVS county code is a sequential Census number with no safe arithmetic
    mapping to FIPS, so counties are matched by their own name instead.
    """
    out = {}
    for r in conn.execute(
            "SELECT geoid, state_fips, name FROM jurisdiction WHERE kind='county'"):
        out[(r["state_fips"], _norm_name(r["name"]))] = r["geoid"]
    return out


def geoid_for(rec, pid, counties=None):
    try:
        fips = census_state_to_fips(rec["fips"])
    except (KeyError, ValueError):
        return None
    gtype = rec["gtype"]
    if gtype == "0":
        return fips
    if gtype == "1":
        info = pid.get(rec["id"]) or {}
        return (counties or {}).get((fips, _norm_name(info.get("name"))))
    if gtype == "2":
        info = pid.get(rec["id"]) or {}
        place = (info.get("place_fips") or "").strip()
        if len(place) == 5 and not place.startswith("99"):
            return fips + place
        return None
    return None
