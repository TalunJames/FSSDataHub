"""Source catalog: the starting points for each state, and liveness checking.

These are entry points, not answers. Seeding them saves the first twenty
minutes of every state's research; `taxdb sources check` confirms they still
resolve. Neither step verifies that the *content* is what you need -- that
is what the state profile research does.
"""

import hashlib
import urllib.request
import urllib.error

from . import db

STATE_AGENCIES = {
    "AL": ("Alabama Department of Revenue", "https://www.revenue.alabama.gov/"),
    "AK": ("Alaska Department of Revenue, Tax Division", "https://tax.alaska.gov/"),
    "AZ": ("Arizona Department of Revenue", "https://azdor.gov/"),
    "AR": ("Arkansas Department of Finance and Administration", "https://www.dfa.arkansas.gov/"),
    "CA": ("California Dept. of Tax and Fee Administration", "https://www.cdtfa.ca.gov/"),
    "CO": ("Colorado Department of Revenue", "https://tax.colorado.gov/"),
    "CT": ("Connecticut Department of Revenue Services", "https://portal.ct.gov/DRS"),
    "DE": ("Delaware Division of Revenue", "https://revenue.delaware.gov/"),
    "DC": ("DC Office of Tax and Revenue", "https://otr.cfo.dc.gov/"),
    "FL": ("Florida Department of Revenue", "https://floridarevenue.com/"),
    "GA": ("Georgia Department of Revenue", "https://dor.georgia.gov/"),
    "HI": ("Hawaii Department of Taxation", "https://tax.hawaii.gov/"),
    "ID": ("Idaho State Tax Commission", "https://tax.idaho.gov/"),
    "IL": ("Illinois Department of Revenue", "https://tax.illinois.gov/"),
    "IN": ("Indiana Department of Revenue", "https://www.in.gov/dor/"),
    "IA": ("Iowa Department of Revenue", "https://revenue.iowa.gov/"),
    "KS": ("Kansas Department of Revenue", "https://www.ksrevenue.gov/"),
    "KY": ("Kentucky Department of Revenue", "https://revenue.ky.gov/"),
    "LA": ("Louisiana Department of Revenue", "https://revenue.louisiana.gov/"),
    "ME": ("Maine Revenue Services", "https://www.maine.gov/revenue/"),
    "MD": ("Comptroller of Maryland", "https://www.marylandtaxes.gov/"),
    "MA": ("Massachusetts Department of Revenue", "https://www.mass.gov/orgs/massachusetts-department-of-revenue"),
    "MI": ("Michigan Department of Treasury", "https://www.michigan.gov/taxes"),
    "MN": ("Minnesota Department of Revenue", "https://www.revenue.state.mn.us/"),
    "MS": ("Mississippi Department of Revenue", "https://www.dor.ms.gov/"),
    "MO": ("Missouri Department of Revenue", "https://dor.mo.gov/"),
    "MT": ("Montana Department of Revenue", "https://mtrevenue.gov/"),
    "NE": ("Nebraska Department of Revenue", "https://revenue.nebraska.gov/"),
    "NV": ("Nevada Department of Taxation", "https://tax.nv.gov/"),
    "NH": ("New Hampshire Department of Revenue Administration", "https://www.revenue.nh.gov/"),
    "NJ": ("New Jersey Division of Taxation", "https://www.nj.gov/treasury/taxation/"),
    "NM": ("New Mexico Taxation and Revenue Department", "https://www.tax.newmexico.gov/"),
    "NY": ("New York State Department of Taxation and Finance", "https://www.tax.ny.gov/"),
    "NC": ("North Carolina Department of Revenue", "https://www.ncdor.gov/"),
    "ND": ("North Dakota Office of State Tax Commissioner", "https://www.tax.nd.gov/"),
    "OH": ("Ohio Department of Taxation", "https://tax.ohio.gov/"),
    "OK": ("Oklahoma Tax Commission", "https://oklahoma.gov/tax.html"),
    "OR": ("Oregon Department of Revenue", "https://www.oregon.gov/dor/"),
    "PA": ("Pennsylvania Department of Revenue", "https://www.revenue.pa.gov/"),
    "RI": ("Rhode Island Division of Taxation", "https://tax.ri.gov/"),
    "SC": ("South Carolina Department of Revenue", "https://dor.sc.gov/"),
    "SD": ("South Dakota Department of Revenue", "https://dor.sd.gov/"),
    "TN": ("Tennessee Department of Revenue", "https://www.tn.gov/revenue.html"),
    "TX": ("Texas Comptroller of Public Accounts", "https://comptroller.texas.gov/"),
    "UT": ("Utah State Tax Commission", "https://tax.utah.gov/"),
    "VT": ("Vermont Department of Taxes", "https://tax.vermont.gov/"),
    "VA": ("Virginia Department of Taxation", "https://www.tax.virginia.gov/"),
    "WA": ("Washington Department of Revenue", "https://dor.wa.gov/"),
    "WV": ("West Virginia Tax Division", "https://tax.wv.gov/"),
    "WI": ("Wisconsin Department of Revenue", "https://www.revenue.wi.gov/"),
    "WY": ("Wyoming Department of Revenue", "https://revenue.wyo.gov/"),
}

NATIONAL = [
    ("Census Annual Survey of State and Local Government Finances",
     "https://www.census.gov/programs-surveys/gov-finances.html",
     "bulk_file", 2,
     "Dollars collected by every county and municipality. Authoritative for "
     "collections, silent on rates, caps, and unused authority."),
    ("Census of Governments 2022 individual unit finance file",
     "https://www2.census.gov/programs-surveys/gov-finances/tables/2022/2022_Individual_Unit_File.zip",
     "bulk_file", 2,
     "Item-code amounts per government unit. Levy/no-levy prior and revenue_base. "
     "Load with `taxdb cog`."),
    ("Census Census of Governments",
     "https://www.census.gov/programs-surveys/cog.html",
     "bulk_file", 2,
     "Counts and classifies every government unit, including special districts "
     "that levy their own property taxes."),
    ("Census 2022 Government Units Listing",
     "https://www.census.gov/data/datasets/2022/econ/gus/public-use-files.html",
     "bulk_file", 2,
     "ID spine. Carries FIPS alongside the 9-digit Census government ID. "
     "Trap: GID first two digits are Census state codes, not FIPS."),
    ("Streamlined Sales Tax Governing Board rate files",
     "https://www.streamlinedsalestax.org/ratesandboundry/Rates/",
     "bulk_file", 2,
     "One format, 24 member states, quarterly local sales rates with effective "
     "dates. Load with `taxdb fetch sst`. Archive the rate file, not the boundary file."),
    ("Open US Law statute snapshots",
     "https://oss-data-us.vaquill.ai/index.json",
     "secondary", 3,
     "Per-state statute Parquet. Secondary compilation — cite the statute and "
     "keep source_url as primary. `taxdb statutes fetch XX` then grep."),
    ("Municode clients API (unofficial)",
     "https://api.municode.com/Clients/stateAbbr?stateAbbr=OH",
     "ordinance", 3,
     "Undocumented SPA endpoint. Enumerates municipalities with a hosted code. "
     "Expect breakage; use as a seed list, not a cite."),
    ("California Elections Data Archive (CEDA)",
     "https://www.csus.edu/college/social-sciences-interdisciplinary-studies/"
     "institute-social-research/california-elections-data-archive.html",
     "bulk_file", 2,
     "Every CA local measure from 1995. ScholarWorks may 403 scripted clients; "
     "pull through a browser once per year. Adapter later."),
    ("OpenElections Clarify parser",
     "https://github.com/openelections/clarify",
     "portal", 3,
     "Scytl/SOE Clarity ENR sites. Local measures with vote counts for many "
     "counties. Election-layer adapter later."),
    ("Lincoln Institute -- Significant Features of the Property Tax",
     "https://www.lincolninst.edu/data/significant-features-property-tax/",
     "secondary", 4,
     "Best available cross-state compilation of property tax limits. Archived; "
     "use to orient and to cross-check; cite the statute it points to, not this."),
    ("Tax Foundation -- state and local tax data",
     "https://taxfoundation.org/data/",
     "secondary", 4,
     "Useful for sanity-checking magnitudes. Not citable as the source of a rate."),
    ("Baker, Janas and Kueng ICPSR 208462",
     "https://www.openicpsr.org/openicpsr/project/208462",
     "secondary", 4,
     "Local rates 2000-2022, county level. Gap finder only, never a cite. Login required."),
]


def seed_catalog(conn):
    """Insert state agency and national sources as unverified leads."""
    n = 0
    for usps, (name, url) in STATE_AGENCIES.items():
        row = conn.execute("SELECT state_fips FROM jurisdiction WHERE kind='state' "
                           "AND state_usps=?", (usps,)).fetchone()
        scope = row["state_fips"] if row else None
        db.get_or_create_source(conn, url, name, source_type="agency_table",
                                authority_tier=2, scope_geoid=scope,
                                notes="Seeded entry point; not yet content-verified.")
        conn.execute("UPDATE state_profile SET revenue_agency=?, revenue_agency_url=? "
                     "WHERE state_usps=?", (name, url, usps))
        n += 1
    for name, url, stype, tier, note in NATIONAL:
        db.get_or_create_source(conn, url, name, source_type=stype,
                                authority_tier=tier, notes=note)
        n += 1
    conn.commit()
    return n


def check(conn, limit=None, timeout=20, fetch=None):
    """GET each source URL, record status, and flag content that changed."""
    sql = "SELECT id, url, content_sha256 FROM source ORDER BY verified, id"
    if limit:
        sql += " LIMIT %d" % int(limit)
    results = []
    getter = fetch or _http_get
    for row in conn.execute(sql).fetchall():
        status, ok, sha, changed = None, 0, None, 0
        try:
            status, blob = getter(row["url"], timeout)
            ok = 1 if status and 200 <= status < 400 else 0
            if blob:
                sha = hashlib.sha256(blob).hexdigest()
                prev = row["content_sha256"]
                if prev and prev != sha:
                    changed = 1
                conn.execute(
                    "INSERT OR IGNORE INTO raw_document "
                    "(source_id, url, sha256, byte_size, retrieved_at) "
                    "VALUES (?,?,?,?,?)",
                    (row["id"], row["url"], sha, len(blob), db.now()))
        except urllib.error.HTTPError as e:
            status = e.code
            ok = 1 if e.code in (401, 403, 405) else 0
        except Exception:
            status = None
            ok = 0
        conn.execute(
            "UPDATE source SET verified=?, http_status=?, last_checked=?, "
            "content_sha256=COALESCE(?, content_sha256), content_changed=? "
            "WHERE id=?",
            (ok, status, db.now(), sha, changed, row["id"]))
        results.append((row["url"], status, ok, changed))
    conn.commit()
    return results


def _http_get(url, timeout):
    req = urllib.request.Request(
        url, method="GET",
        headers={"User-Agent": "tax-database/0.3"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Skip hashing huge bulk files during a catalog check.
        clen = resp.headers.get("Content-Length")
        if clen and int(clen) > 2_000_000:
            return resp.status, b""
        blob = resp.read(2_000_001)
        if len(blob) > 2_000_000:
            return resp.status, b""
        return resp.status, blob
