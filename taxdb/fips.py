"""Census GID state-code vs FIPS state-code crosswalk.

Census Government Units Listing carries a 9-digit government ID whose first
two digits are a *Census* state code, not FIPS. Both are alphabetical, which
is why this bites: Census codes are gapless sequential 01-51 while FIPS skips
03, 07, 14, 43, and 52. AL/AK/AZ agree; everything after Arizona silently shifts.

Hardcoded and unit-tested. Do not derive this from arithmetic at load time.
"""

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}

# FIPS -> USPS. Includes PR (72), which is not in the 01-51 Census GID sequence.
FIPS_TO_USPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "72": "PR",
}

USPS_TO_FIPS = {v: k for k, v in FIPS_TO_USPS.items()}

# 50 states + DC, FIPS order. Census GID state codes are this list numbered 01-51.
FIPS_GID_ORDER = [
    "01", "02", "04", "05", "06", "08", "09", "10", "11", "12",
    "13", "15", "16", "17", "18", "19", "20", "21", "22", "23",
    "24", "25", "26", "27", "28", "29", "30", "31", "32", "33",
    "34", "35", "36", "37", "38", "39", "40", "41", "42", "44",
    "45", "46", "47", "48", "49", "50", "51", "53", "54", "55",
    "56",
]

FIPS_SKIPS = ("03", "07", "14", "43", "52")


def gid_crosswalk_rows():
    """Yield dicts for census_gid_crosswalk: one row per state + DC."""
    rows = []
    for i, fips in enumerate(FIPS_GID_ORDER, start=1):
        usps = FIPS_TO_USPS[fips]
        rows.append({
            "fips_state": fips,
            "census_state": "%02d" % i,
            "usps": usps,
            "name": STATE_NAMES[usps],
        })
    return rows


def census_state_to_fips(census_state):
    """Map a 2-digit Census GID state code to FIPS. Raises KeyError if unknown."""
    n = int(census_state)
    if n < 1 or n > len(FIPS_GID_ORDER):
        raise KeyError("census state code %r out of range" % census_state)
    return FIPS_GID_ORDER[n - 1]


def fips_to_census_state(fips_state):
    """Map a 2-digit FIPS state code to Census GID state code."""
    try:
        i = FIPS_GID_ORDER.index(fips_state)
    except ValueError:
        raise KeyError("FIPS state %r is not in the Census GID 01-51 sequence" % fips_state)
    return "%02d" % (i + 1)


def seed_crosswalk(conn):
    for row in gid_crosswalk_rows():
        conn.execute(
            "INSERT OR REPLACE INTO census_gid_crosswalk "
            "(fips_state, census_state, usps, name) VALUES (?,?,?,?)",
            (row["fips_state"], row["census_state"], row["usps"], row["name"]),
        )
    conn.commit()
