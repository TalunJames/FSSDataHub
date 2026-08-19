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
    ("Census Annual Survey of School System Finances (F-33)",
     "https://www.census.gov/programs-surveys/school-finances.html",
     "bulk_file", 2,
     "Per-district revenue by source for every US school district. Levy/no-levy "
     "prior for school property taxes; silent on rates."),
    ("OpenElections results repositories",
     "https://github.com/openelections",
     "bulk_file", 3,
     "Per-state repos of official precinct/county results as CSV. Local measures "
     "appear where counties reported them. Companion to the Clarify parser entry."),
    ("MSRB EMMA municipal disclosures",
     "https://emma.msrb.org/",
     "portal", 3,
     "Official statements for every muni bond issue: pledged taxes, rates, "
     "election results in the OS. Search portal, not bulk; heavy bot defenses."),
    ("NCSL statewide ballot measures database",
     "https://www.ncsl.org/elections-and-campaigns/statewide-ballot-measures-database",
     "secondary", 4,
     "Statewide measures only -- useful for the framework pass (threshold and cap "
     "changes arrive as constitutional amendments). Not for local measures."),
    ("Ballotpedia local ballot measure elections",
     "https://ballotpedia.org/Local_ballot_measure_elections_in_2026",
     "secondary", 4,
     "Broad local-measure tracking, strongest in large counties. URL is "
     "year-stamped; bump it annually. Orientation and gap finding, never a cite."),
]

# State-published bulk files and rate databases. Scoped to the state in
# seed_catalog, so collector.crawl seeds them into every crawl for a
# jurisdiction in that state. Sales entries exist only for states outside the
# SST feed above; property/income/lodging/elections entries are the states
# whose agencies publish real compilations rather than lookup widgets.
STATE_BULK = [
    # --- local sales rates, non-SST states ---
    ("AL", "Alabama DOR sales and use tax rates",
     "https://www.revenue.alabama.gov/sales-use/tax-rates/",
     "agency_table", 2,
     "State-administered local rates, downloadable charts. Self-administered "
     "cities (Birmingham etc.) publish separately."),
    ("AK", "Alaska Remote Seller Sales Tax Commission",
     "https://arsstc.org/",
     "agency_table", 2,
     "No state sales tax; ARSSTC compiles member municipalities' local rates "
     "and boundaries. Non-member boroughs still publish their own."),
    ("AZ", "Arizona DOR TPT rate table",
     "https://azdor.gov/business/transaction-privilege-tax/tax-rate-table",
     "bulk_file", 2,
     "Monthly rate table for every city and county TPT jurisdiction, "
     "spreadsheet download."),
    ("CA", "CDTFA open data portal",
     "https://www.cdtfa.ca.gov/dataportal/",
     "bulk_file", 2,
     "Sales/use rates by city and county incl. district taxes, as datasets. "
     "Pair with CEDA for the measures that created each district tax."),
    ("CO", "Colorado DOR sales/use tax rates (DR 1002)",
     "https://tax.colorado.gov/DR1002",
     "bulk_file", 2,
     "Semiannual rate publication for state-collected locals. Home-rule cities "
     "self-collect and publish their own rates."),
    ("IL", "Illinois DOR tax rate database",
     "https://tax.illinois.gov/research/taxrates.html",
     "agency_table", 2,
     "Machine-readable local sales rates by jurisdiction, semiannual."),
    ("LA", "Louisiana Uniform Local Sales Tax Board",
     "https://lulstb.com/",
     "agency_table", 2,
     "Parish-by-parish local rate tables and lookup. Site blocks scripted "
     "clients; expect the browser fetch path."),
    ("MO", "Missouri DOR sales/use tax rate tables",
     "https://dor.mo.gov/taxation/business/tax-types/sales-use/rate-tables/",
     "bulk_file", 2,
     "Quarterly downloadable rate tables for every taxing jurisdiction, "
     "with district detail."),
    ("NY", "NY Publication 718 sales tax rates",
     "https://www.tax.ny.gov/pdf/publications/sales/pub718.pdf",
     "bulk_file", 2,
     "All county/city rates in one PDF. The 718 series (718-A etc.) covers "
     "special rates; school district income surcharges live elsewhere."),
    ("TX", "Texas Comptroller city sales and use tax rates",
     "https://comptroller.texas.gov/taxes/sales/city.php",
     "agency_table", 2,
     "City rates with effective dates; sibling pages carry county, SPD, and "
     "transit rates. Quarterly updates."),
    # --- property tax rates and levies ---
    ("TX", "Texas Comptroller property tax rates and levies",
     "https://comptroller.texas.gov/taxes/property-tax/rates/index.php",
     "bulk_file", 2,
     "Annual rate and levy spreadsheets for every taxing unit, from PTAD "
     "surveys."),
    ("OH", "Ohio DOT tax data series -- property",
     "https://tax.ohio.gov/researcher/tax-analysis/tax-data-series/all-property-taxes",
     "bulk_file", 2,
     "Millage by taxing district (DTE tables), yearly Excel. The rate answer "
     "for every Ohio levy."),
    ("NJ", "NJ general tax rates by municipality",
     "https://www.nj.gov/treasury/taxation/lpt/localtax.shtml",
     "agency_table", 2,
     "Annual general and effective property tax rates for all 564 "
     "municipalities."),
    ("FL", "Florida DOR property tax data portal",
     "https://floridarevenue.com/property/Pages/DataPortal.aspx",
     "bulk_file", 2,
     "Millage by taxing authority (DR-403/DR-420 series) and tax roll data, "
     "statewide."),
    ("NY", "NY ORPTS property tax data",
     "https://www.tax.ny.gov/research/property/default.htm",
     "bulk_file", 2,
     "Levies, full-value tax rates, and constitutional tax limit data for "
     "every local government and school district."),
    ("WA", "Washington DOR property tax statistics",
     "https://dor.wa.gov/about/statistics-reports/property-tax-statistics",
     "bulk_file", 2,
     "Levy rates and amounts by taxing district, annual workbooks."),
    ("WI", "Wisconsin DOR reports (town/village/city rates)",
     "https://www.revenue.wi.gov/Pages/Report/Home.aspx",
     "bulk_file", 2,
     "Apportioned property tax rates and levies per municipality and school "
     "district."),
    ("MI", "Michigan Treasury millage rate reports",
     "https://www.michigan.gov/taxes/property/estimator/related/millage-rates",
     "bulk_file", 2,
     "Statewide millage exports per year (L-4029 rollup). The estimator's "
     "database backs it."),
    ("MN", "Minnesota DOR property tax reports",
     "https://www.revenue.state.mn.us/property-tax-reports",
     "bulk_file", 2,
     "Levies and rates by county, city, and special district; voter-approved "
     "referendum levies broken out."),
    ("IL", "Illinois DOR property tax statistics",
     "https://tax.illinois.gov/research/taxstats/propertytaxstatistics.html",
     "bulk_file", 2,
     "District-level rates and extensions (tables 27-28), annual."),
    ("KS", "Kansas DOR property valuation statistics",
     "https://www.ksrevenue.gov/pvdstatistics.html",
     "bulk_file", 2,
     "County clerk levy sheets rolled up: mill levies for every taxing "
     "subdivision."),
    # --- local income / payroll ---
    ("OH", "Ohio municipal income tax rate database (The Finder)",
     "https://thefinder.tax.ohio.gov/",
     "agency_table", 2,
     "Downloadable rate database for every municipal income tax and school "
     "district income tax. The income answer for Ohio."),
    ("PA", "PA DCED municipal statistics (EIT/LST register)",
     "https://dced.pa.gov/local-government/municipal-statistics/",
     "agency_table", 2,
     "The official register of earned income and local services tax rates for "
     "every PA municipality and school district. munstats.pa.gov blocks "
     "scripted clients; enter from this page."),
    ("IN", "Indiana DOR Departmental Notice #1",
     "https://www.in.gov/dor/files/dn01.pdf",
     "bulk_file", 2,
     "All 92 county local income tax rates in one PDF, updated as rates "
     "change."),
    ("MD", "Maryland DLS local tax rates report",
     "https://dls.maryland.gov/pubs/prod/NoPblTabPDF/2026CountyLocalTaxRates.pdf",
     "bulk_file", 2,
     "County income, property, recordation, and transfer rates in one annual "
     "PDF. URL is year-stamped; bump it annually."),
    ("MI", "Michigan city income tax",
     "https://www.michigan.gov/taxes/citytax",
     "agency_table", 2,
     "The complete list of Michigan's city income taxes and rates."),
    # --- lodging and local-option taxes ---
    ("FL", "Florida EDR county and municipal data",
     "https://edr.state.fl.us/Content/local-government/data/county-municipal/index.cfm",
     "bulk_file", 2,
     "Local option taxes (tourist development, discretionary surtax, fuel) "
     "with rates and adoption history, per county. Best lodging source in FL."),
    ("TX", "Texas hotel occupancy tax reporting",
     "https://comptroller.texas.gov/transparency/local/hotel-reporting/",
     "bulk_file", 2,
     "Municipal HOT rates and receipts, self-reported to the Comptroller."),
    # --- election results for the measures pass ---
    ("OH", "Ohio SOS election results and data",
     "https://www.ohiosos.gov/elections/election-results-and-data/",
     "portal", 2,
     "Questions and issues results as per-election spreadsheets, statewide. "
     "403s scripted clients; use the browser fetch path."),
    ("WA", "Washington SOS election data and research",
     "https://www.sos.wa.gov/elections/data-research",
     "portal", 2,
     "Results archives incl. local measures, plus voter turnout by county."),
    ("TX", "Texas Bond Review Board local debt data",
     "https://www.brb.texas.gov/",
     "bulk_file", 2,
     "Local government debt outstanding and bond election data, annual "
     "spreadsheets by issuer."),
    ("CA", "California City Finance (Coleman)",
     "https://californiacityfinance.com/",
     "secondary", 4,
     "Tracks every CA local revenue measure with pass/fail and vote shares. "
     "Cross-check for CEDA; cite the county canvass, not this."),
]


def _state_fips(conn, usps):
    row = conn.execute("SELECT state_fips FROM jurisdiction WHERE kind='state' "
                       "AND state_usps=?", (usps,)).fetchone()
    return row["state_fips"] if row else None


def _seed_scoped(conn, usps, name, url, stype, tier, note):
    """Seed a state-scoped source, adopting any row seeded before states existed.

    `init --with-sources` runs before `seed`, so a first pass can store these
    with a NULL scope; without the adoption the post-seed pass would insert a
    scoped duplicate (UNIQUE is on url+scope).
    """
    scope = _state_fips(conn, usps)
    if scope:
        conn.execute(
            "UPDATE source SET scope_geoid=? WHERE url=? AND scope_geoid IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM source WHERE url=? AND scope_geoid=?)",
            (scope, url, url, scope))
    return db.get_or_create_source(conn, url, name, source_type=stype,
                                   authority_tier=tier, scope_geoid=scope,
                                   notes=note)


def seed_catalog(conn):
    """Insert state agency, state bulk, and national sources as unverified leads."""
    n = 0
    for usps, (name, url) in STATE_AGENCIES.items():
        _seed_scoped(conn, usps, name, url, "agency_table", 2,
                     "Seeded entry point; not yet content-verified.")
        conn.execute("UPDATE state_profile SET revenue_agency=?, revenue_agency_url=? "
                     "WHERE state_usps=?", (name, url, usps))
        n += 1
    for usps, name, url, stype, tier, note in STATE_BULK:
        _seed_scoped(conn, usps, name, url, stype, tier, note)
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
