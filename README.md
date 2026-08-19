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

The crawler runs on [Crawlee](https://crawlee.dev/python/), which needs Python
3.10+. On an older interpreter it is skipped and the collector falls back to its
own sequential fetch loop, so the Mac path works either way. The TrueNAS image
is Python 3.12 and gets the full engine, including a headless browser for the
county sites that render their rate tables in JavaScript. See
`collector/README.md`.

Open it, add an AI key under Settings, and press **Start collecting**. It sets
itself up, loads the free national files, works out each state's rules, then
works down every county and city by population, re-checking records as they
age. Nothing else needs choosing. See [Running unattended](#running-unattended).

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

Every layer has a writer. `threshold_rule`, `authority_grant` and
`ballot_measure` are filled by the research passes below; nothing in L2 or L4
is populate-by-hand-only.

Schema: `taxdb/schema.sql` (one file, 18 tables, 8 views).
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

## Three research passes

The work queue holds more than tax rates, because a rate on its own does not
answer a client's question. Each pass writes a different layer, and the order
is a value judgement: 51 rows of state law make every rate underneath them
mean something.

| pass | runs per | fills | why it comes first |
|---|---|---|---|
| `framework` | state (51) | `threshold_rule` · `authority_grant` · `state_profile` | Without caps there is no headroom; without thresholds a margin cannot be computed. One pass covers every jurisdiction in the state. |
| tax categories | county, place | `tax_instrument` | The core record: what is levied, at what rate, under what cite. |
| `elections` | county (3,222) | `ballot_measure` | Canvasses and official-results abstracts are published by counties, not cities, so this is where measure history actually lives. |

A findings document may carry any of them at once, since one page often
answers more than one question:

```json
{
  "schema_version": "1.1",
  "researcher": "your name or model id",
  "findings":   [ /* tax_instrument  — rates and caps in force */ ],
  "measures":   [ /* ballot_measure  — what voters were asked, and the result */ ],
  "thresholds": [ /* threshold_rule  — what it takes to pass */ ],
  "grants":     [ /* authority_grant — what the state permits, up to what cap */ ],
  "profile":    { /* state_profile   — one state's statutory frame */ }
}
```

Ingest validates each section separately and reports what it wrote:

```bash
./bin/taxdb ingest findings/oh-framework.json
# wrote 34 row(s) across 1 jurisdiction(s): 12 grants, 21 thresholds, 1 profile
```

Percentages are percentages. Two-thirds is `66.67`, and `0.6667` is rejected:
a threshold stored as a fraction makes every margin computed against it wrong.

Vote arithmetic is derived, not trusted. Give the counts and ingest computes
the total, the yes share, the turnout, the threshold that applied **on that
election date**, and the margin against it. A percentage that contradicts its
own counts is rejected, and so is a measure marked `passed` that sits below its
own threshold.

---

## The loop

```
seed ──► plan ──► next --emit ──► [research] ──► ingest ──► review ──► export
          │                                          │
          └──────────── fetch / archive ─────────────┘
```

```bash
./bin/taxdb plan --state OH --kinds state --categories framework
./bin/taxdb plan --state OH --kinds county --categories property,sales_use,elections
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

The collector does all three on its own before it crawls anything; the buttons
under Settings are only for jumping the queue. Adapter-filled work items close
as `complete` with the adapter named: a published rate file with archived bytes
and a tier-2 citation is better provenance than a crawled web page, so it is
filed rather than queued for a human. They come back on the `refresh_days`
timer like anything else.

Archive a rate file before you have a parser for it. In 48 states, taxes
that expired are only recoverable by differencing consecutive periods, and
a period you did not save is gone:

```bash
./bin/taxdb archive put --adapter cdtfa823 --period 2026Q3 \
    --url https://www.cdtfa.ca.gov/formspubs/cdtfa823.pdf cdtfa823.pdf
./bin/taxdb archive list
```

---

## Running unattended

The point of the collector is that the database fills itself. Press **Start
collecting** and the autopilot decides what to do next from the state of the
database, in this order:

1. **Set up** — seed the Census jurisdiction registry, then the source catalog.
2. **Free national files** — SST rate files (24 states of sales tax) and Census
   of Governments collections. These cost nothing and park thousands of work
   items as already answered.
3. **State frameworks** — one pass per state, ahead of any local work.
4. **Statute corpora** — for states whose framework research is still open.
5. **Expand** — add the next chunk of counties and cities, most populous first,
   with their tax categories and (for counties) their elections pass.
6. **Refresh** — send records older than `refresh_days` back for a fresh look.

When there is nothing left it says so, rather than reporting an empty queue as
though the work were finished.

Throughput and cost, both of which are settings rather than rewrites:

- **`workers`** researches that many jurisdictions at once. Almost all of an
  item is spent waiting on a web server and then a model, so this is the
  throughput dial: one worker is roughly 860 items a day, or about four months
  for the whole country. The fetch ceiling is global and divided among workers,
  so raising it never increases the load on a county web server.
- **`search_qpm`** caps search across the whole app, because search is the one
  thing that does not scale with workers. `auto` is 60 with an API key and 12
  without, and an engine that refuses is benched with escalating backoff while
  the next one answers. Without a key, search alone limits the run to about two
  items a minute however many workers run.
- **`batch_extract`** sends the reading through the Message Batches API at half
  price, back within the hour instead of the second. Extraction is most of the
  bill and none of it is urgent. Items park at `awaiting_ai` while a batch is
  out; the second checker still runs on each result as it lands, so what needs a
  human does not change.

Guards worth knowing about:

- **Attempt ceiling.** A place whose rate page cannot be found is parked at
  `blocked` after `max_attempts` tries instead of recycling forever at the head
  of a population-weighted queue.
- **Blocked search is reported.** The crawler prefers a search API when
  `search_api_key` is set and falls back to scraped result pages. A query every
  engine refused is recorded as blocked, so a throttled night cannot look like
  a thorough one. Set a Brave Search key to avoid the problem.
- **Failed downloads cool off.** A bulk file that will not fetch is retried on a
  timer, never in a loop.
- **Nothing found is an answer.** An elections pass that reaches a county's
  records and finds no revenue measures writes a `spot_checked` coverage
  assertion with `measures_found = 0`, and the item closes as `no_data`.

The second checker runs on all three passes, with a different skeptic's prompt
for each: it re-reads rates against their quotes, measures against their
canvasses, and thresholds against the statute text. What passes is filed
without a human. What is flagged lands under **Needs you** with the reason.

The mechanical checks that run first are sorted into two kinds, and the split
is what keeps **Needs you** short. A *contradiction* — a rate outside any
plausible range for its unit, a `prohibited` status carrying a rate, a rate
above its own cap, a threshold stored as a fraction, a result dated in the
future — goes straight to a human and does not spend a checker call. A
*concern* — no source quote, a quote the text search could not locate, an
unset source tier, no threshold on file yet — is handed to the model as
context and the model rules on it. Concerns alone never queue an item. A bulk
rate file has no prose to quote, and PDF text extraction mangles whitespace
often enough that an exact quote miss is weak evidence; treating either as a
defect put rows on the review page whose only fault was an empty optional
field.

Everything on this page has a manual equivalent under Settings, and autopilot
can be switched off there entirely.

---

## Product views

These are what a brief reads. They are computed, not stored.

| view | question |
|---|---|
| `v_current_tax` | every tax currently in force, with source and authority tier |
| `v_sunset_watch` | taxes expiring within three years, plus prior measure history |
| `v_headroom` | unused authority under the state cap |
| `v_live_grant` | one live, most-specific authority grant per state/kind/instrument |
| `v_live_threshold` | one live, most-specific vote threshold per state/kind/measure class |
| `v_near_miss` | measures that failed within 8 points of threshold in the last 6 years |
| `v_measure_capture_gap` | rate changes with no election on record |
| `v_coverage` | research progress by state, kind, and category |

The collector surfaces all of these under **The data**, with an explicit note
on each empty one saying which pass fills it. An empty product view is a
coverage gap, never a finding.

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

It also covers the layers that used to have no writer: that a threshold
recorded as `0.6667` is rejected, that a measure's margin is computed against
the rule in force **on its election date** rather than today's, that
re-ingesting the same document updates rather than duplicating, that a
percentage contradicting its own vote counts is refused, that autopilot plans
state frameworks before local work and elections only for counties, and that a
blocked search engine is reported instead of passing as an empty result.

Concurrency and cost have their own suites, because both failed silently rather
than loudly. Eight threads claiming one queue never take the same jurisdiction
twice (the old read-then-write claim duplicated about a fifth of a batch). The
request ceiling stays at thirty a minute whatever the worker count. Each worker
gets its own Crawlee storage directory. One exploding county does not sink the
other nineteen in its batch. A refusing search engine is benched rather than
retried. Batch reading survives an unparseable result, an errored result, a
failed submit, and a restart mid-flight; and thinking is always explicit,
because omitting it on a current model quietly lets reasoning eat the token
budget the JSON needed.

---

## Command reference

| command | purpose |
|---|---|
| `taxdb init [--with-sources]` | create the database |
| `taxdb seed [--include-mcd] [--counties-only] [--force]` | load jurisdictions |
| `taxdb plan --state XX --kinds --categories --min-pop` | queue work (categories include the `framework` and `elections` passes) |
| `taxdb next --limit N [--emit]` | claim work, optionally write packets |
| `taxdb packet GEOID` | print one research packet |
| `taxdb ingest FILE [--dry-run]` | load findings, measures, thresholds, grants, profile |
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
