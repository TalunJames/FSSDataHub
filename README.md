# FSSDataHub

A national database of local tax authority, capacity, and election history
for every city, county, and state in the United States.

Stdlib Python 3 and SQLite for the CLI. The TrueNAS collector (web UI + crawler + AI) is optional and has its own dependencies.

```bash
git clone https://github.com/TalunJames/FSSDataHub.git
cd FSSDataHub
```

```bash
./bin/taxdb init --with-sources
./bin/taxdb seed
./bin/taxdb status
```

To populate the ledger from a NAS, a browser, or an AI endpoint — **run the collector on TrueNAS**, not on this Mac. See `TRUENAS.md`.

```bash
NAS=truenas.local sh deploy/push-to-truenas.sh
# then open http://<nas-ip>:3490
```

To try the UI on this Mac only (data stays local and goes away when the laptop sleeps):

```bash
python3 -m pip install -r requirements.txt
python3 -m collector          # http://127.0.0.1:8080
```

`seed` downloads Census Gazetteer and Population Estimates bulk files
(no API key) and loads every active county and incorporated place, plus
the 50 states, DC, and Puerto Rico. Township/MCD taxing units are optional
(`--include-mcd`).

---

## What this is shaped around

There is no national database of local tax rates, unused authority, or
revenue-measure elections. This framework is a **registry, a ledger, and
an archive**, built so that partial coverage is honest and every number
carries a citation.

| | count |
|---|---|
| counties and county-equivalents | 3,222 |
| incorporated places (active governments) | 19,469 |
| × 5 tax categories | ~114,000 research units |

Absence is not an answer. Every tax instrument has a `status`:

| status | meaning |
|---|---|
| `levied` | currently imposed, with a rate |
| `authorized_not_levied` | legally available here, not imposed |
| `prohibited` | affirmatively barred by state law, with the cite |
| `repealed` | imposed historically, since ended |
| `unknown` | looked, could not find it — say where in `notes` |

A county with no lodging-tax row is a gap in research. A county recorded
as `authorized_not_levied` at up to 5% under a named statute is a finding.

---

## Layers

```
L0  ARCHIVE     dated object store — every rate file, every period, forever
L1  SPINE       jurisdiction registry (Census GEOID) + GID↔FIPS crosswalk
L2  RULES       state_profile · threshold_rule · authority_grant
L3  FACTS       tax_instrument · rate_change_event
L4  HISTORY     ballot_measure · attempt chains · campaign committees
L5  CAPACITY    revenue_base → yield_estimate · headroom
L6  PRODUCT     v_sunset_watch · v_near_miss · v_headroom · v_coverage
```

Schema: `taxdb/schema.sql` (one file, 18 tables, 7 views).
Vocabulary: `taxdb/vocab.py`. Read both before the first real batch.

Provenance is required on every claim: `source_id`, `archive_file_id`,
`extraction_method`, `confidence`, `retrieved_at`. Updates never overwrite
(`superseded_by`), so rate history is preserved. Sources are tiered 1–4
(primary law → secondary aggregator). Client-facing rows need a tier-1/2/3
primary source and a corroborating `claim_source` row.

Coverage assertions keep a partial database from lying. A Kansas city with
zero measures because Kansas publishes no statewide file must never read
the same as a Washington city that genuinely never tried. `taxdb seed` writes
a `completeness='none'` row for every state *before* any measures land.

---

## The loop

```
seed ──► plan ──► next --emit ──► [research] ──► ingest ──► review ──► export
          │                                          │
          └──────────── fetch / archive ─────────────┘
```

```bash
./bin/taxdb plan --state OH --kinds county --categories property,sales_use
./bin/taxdb next --state OH --limit 25 --emit
./bin/taxdb ingest findings/oh-batch-1.json
./bin/taxdb verify --strict
```

Research a state's statutory framework **once**, before any city in it:

```bash
./bin/taxdb profile OH
./bin/taxdb profile OH --set home_rule_doctrine=home_rule
```

Fill what a file already knows **before** crawling. SST is 24 states of sales
tax; Census of Governments is a nationwide levy/no-levy prior; Open US Law is
the statute grep for profiles:

```bash
./bin/taxdb fetch sst                 # or: fetch sst --state OH WA
./bin/taxdb cog
./bin/taxdb statutes fetch OH
./bin/taxdb statutes grep OH lodging
```

The collector dashboard has the same three buttons. Adapter-filled work items
are parked at `needs_review` so the crawler does not recrawl them.

Archive a rate file before you have a parser for it. In 48 states, taxes
that expired are only recoverable by differencing consecutive periods, and
a period you did not save is gone:

```bash
./bin/taxdb archive put --adapter cdtfa823 --period 2026Q3 \
    --url https://www.cdtfa.ca.gov/formspubs/cdtfa823.pdf cdtfa823.pdf
./bin/taxdb archive list
```

---

## Product views

These are what a brief reads. They are computed, not stored.

| view | question |
|---|---|
| `v_current_tax` | every tax currently in force, with source and authority tier |
| `v_sunset_watch` | taxes expiring within three years, plus prior measure history |
| `v_headroom` | unused authority under the state cap |
| `v_live_grant` | one live, most-specific authority grant per state/kind/instrument |
| `v_near_miss` | measures that failed within 8 points of threshold in the last 6 years |
| `v_measure_capture_gap` | rate changes with no election on record |
| `v_coverage` | research progress by state, kind, and category |

Dates must be ISO `YYYY-MM-DD` on ingest. SQLite's `julianday('6/30/2027')`
returns NULL, and an unnormalized row silently vanishes from sunset watch.
`taxdb verify` flags that.

---

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite asserts the Census-state-code-is-not-FIPS trap, that overlapping
authority grants do not fan out `v_headroom`, that `levy_override` near-misses
are not dropped by a `tax%` filter, that byte-identical files in consecutive
periods both archive, and that non-ISO sunset dates stay out of the watch list.

---

## Command reference

| command | purpose |
|---|---|
| `taxdb init [--with-sources]` | create the database |
| `taxdb seed [--include-mcd] [--counties-only] [--force]` | load jurisdictions |
| `taxdb plan --state XX --kinds --categories --min-pop` | queue work |
| `taxdb next --limit N [--emit]` | claim work, optionally write packets |
| `taxdb packet GEOID` | print one research packet |
| `taxdb ingest FILE [--dry-run]` | load findings |
| `taxdb fetch [ADAPTER] [--archive-only] [--state]` | run a bulk adapter (`sst` = 24-state sales files) |
| `taxdb cog [--force]` | Census of Governments 2022 collections → `revenue_base` |
| `taxdb statutes fetch XX` | cache Open US Law parquet for one state |
| `taxdb statutes grep XX [terms]` | search the local statute corpus |
| `taxdb geocode NAME --state XX` | Census geocoder → GEOID (no key) |
| `taxdb archive {list,put}` | dated object store |
| `taxdb coverage {list,seed} [--geoid]` | coverage assertions |
| `taxdb review [--geoid --category --status]` | sign off |
| `taxdb status [--by-state]` | coverage dashboard |
| `taxdb verify [--strict]` | integrity checks |
| `taxdb export [--out DIR] [--state XX]` | write CSVs |
| `taxdb sources {list,check,add}` | source catalog |
| `taxdb profile XX [--set field=value]` | state statutory framework |
| `taxdb sql "SELECT ..."` | query directly |
