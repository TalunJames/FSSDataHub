# Collector (TrueNAS SCALE)

A web app that lives next to `taxdb` and fills it without being told what to do
next. Add an AI key and press **Start the crawler**. There is one button on the
Today page because there is one decision: run, or pause.

It populates the database three ways:

1. **Crawl** official sources (state DOR, recorded URLs, then a web search for the county/city site, its tax PDFs, and its election canvasses)
2. **Extract** with OpenAI-compatible APIs, Anthropic, or a local Llama/Ollama server
3. **Manual entry** — drop a URL/PDF/photo for the AI, or answer only the questions still open (skip any)

## What it decides on its own

The autopilot reads the state of the database and picks the next most valuable
thing nobody has done: set up the registry, load the free national bulk files,
work out each state's rules, then work down every county and city by population,
re-checking records as they age. An empty queue is no longer the end of the work.

Three passes go through the same queue, and the ordering is the efficiency
argument:

| pass | runs per | fills |
|---|---|---|
| state rules | state (51) | vote thresholds, authority caps, state profile |
| tax categories | county, place | rates and caps in force |
| election results | county | past revenue measures and their margins |

State rules come first because 51 rows of state law are what make every rate
underneath them mean something: without a cap there is no headroom, and without
a threshold a margin cannot be computed.

Guards, all visible in the UI:

- A place whose page cannot be found is parked as **stuck** after a few tries, not retried forever.
- A search every engine refused is reported as blocked, so a throttled night never looks like a thorough one. Set a **Brave Search API key** under Settings to avoid it.
- A county with no findable measures closes as **no data** with a coverage assertion recording that we looked, which is not the same as a blank.
- Rows a bulk file already answered are filed as done, not queued for review.
- Records older than `refresh_days` go back in the queue on their own.

Every extraction is re-read by a **second checker** — deterministic sanity checks plus a second, skeptical model pass, with a different prompt per pass: rates are checked against their quotes, measures against their canvasses, thresholds against the statute text. Items that pass are marked `complete` automatically; only flagged items land under **Review**, with the reason attached. If the checker cannot run, items fail toward review, never toward trust. Turn it off under **Setup → Double-check the AI** to review everything by hand.

The mechanical checks separate **contradictions** from **concerns**, and that split is what keeps **Needs you** worth opening. A contradiction (a rate outside any plausible range for its unit, a `prohibited` status carrying a rate, a rate above its own cap, a threshold stored as a fraction, a result dated in the future) goes to a human and skips the model call — there is nothing a second opinion adds. A concern (no source quote, a quote the search could not locate, an unset source tier, no threshold on file yet) is handed to the model as context, and the model decides whether it matters. Concerns on their own never queue an item for a human.

Work items an adapter already answered are filed as **done** with the adapter named, not queued: a published rate file carries archived bytes and a tier-2 citation, which is stronger provenance than a crawled page. The SST files alone cover 24 states, so parking them for review buried the queue under tens of thousands of spreadsheet rows.

The crawler respects `robots.txt`, stays on government hosts, rate-limits, and archives every byte before anyone extracts a number.

With Anthropic, `claude-sonnet-5` is the recommended extractor (the default); setting the checker model to `claude-haiku-4-5` keeps the second pass cheap. **Save & test key** on the settings page verifies a key with one tiny call.

## Run locally

```bash
python3 -m pip install -r requirements.txt
python3 -m collector
```

Open http://127.0.0.1:8080

**Start collecting** does the setup itself. The CLI equivalents, if you prefer
to watch them run:

```bash
./bin/taxdb init --with-sources
./bin/taxdb seed
```

To work one state first, use **Add a specific state to the list** on the Work
list page. To run a bulk file by hand, use **Do it by hand** under Setup. The
crawler skips work items already filled by an adapter.

## Pages

| page | what it is for |
|---|---|
| **Today** | How many places need a person, what ran overnight, and one button: run or pause |
| **Review** | One flagged place at a time, with the archived quote beside the number and three keys to settle it |
| **The record** | Every place and tax we hold: the rate on file, where it stands, when we last looked |
| **Setup** | Three questions — who reads the documents, whether a second AI checks, when it crawls. Everything else is behind one disclosure |
| Add a source · What the database holds · Work list · Activity log | The detail, out of the daily path |

The look is the **Broadsheet** design system, vendored as `static/broadsheet.css`
from `Design/_ds/broadsheet-33f8ea22/`. Treat that file as upstream and put the
app's own rules in `static/style.css`; both take every color, size and radius
from the system's tokens. `collector/present.py` holds the formatting and the
queries the screens need, so the routes stay thin.

## TrueNAS SCALE (always on)

The database is supposed to live on the NAS, not on a laptop. Datasets, install script, and Custom App YAML: **`TRUENAS.md`**.

Short version: datasets are `Seawolf/FogSignal/taxdata` (code) and `Seawolf/FogSignal/taxdata/sql` (database). From this Mac:

```bash
NAS=truenas.local sh deploy/push-to-truenas.sh
```

Open `http://<nas-ip>:3490`. `tax.db` is `/mnt/Seawolf/FogSignal/taxdata/sql/tax.db` and comes back after a reboot.

If Ollama is already an app on the same NAS, set **Local Llama / Ollama → Base URL** to that app’s IP (`http://172.16.x.x:11434`) or `http://host.docker.internal:11434` when Ollama listens on the host.

Optional env: `COLLECTOR_PASSWORD` (HTTP basic auth, user `taxdb` unless `COLLECTOR_USER` is set). First-boot API keys can come from `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `LLAMA_BASE_URL`; after that the Settings page is the source of truth.

The collector is a **bounded research crawler**, not a whole-internet scrape. It starts from the state agency catalog, searches for that county or city’s own site, follows tax-looking links (PDFs first), and stops at `max_pages_per_item`. Hosts have to look like that jurisdiction or a `.gov` / `.us` site.

## Fetch engine

Fetching runs on [Crawlee](https://crawlee.dev/python/), which brings retries
with backoff, a request queue that survives a crash mid-run, and polite
concurrency across hosts. Every judgement about *what* to fetch is still ours:
the official-host test, the relevance scoring, archiving, and text extraction
all live in `crawl.py` and are unchanged. `fetcher.py` is transport only.

| setting | meaning |
|---|---|
| `use_crawlee` | off falls back to the old sequential loop |
| `workers` | work items researched at once (the throughput dial) |
| `concurrency` | parallel fetches within one work item |
| `max_retries` | attempts per page before it is recorded as an error |
| `browser_render` | re-fetch thin pages in headless Chromium |
| `max_render_pages` | browser fetches allowed per work item |
| `render_min_chars` | an HTML page under this much text counts as thin |

**Seconds between requests** still caps the rate. It used to be a sleep
between single fetches; now it is a ceiling on fetches per minute, spent across
hosts instead of idling. Two seconds means thirty fetches a minute either way,
**and that ceiling is global**: the budget is divided among workers, so raising
the worker count never increases the load on a county web server.

## The worker pool

One coordinator thread decides what happens next; a pool of `workers` threads
researches jurisdictions. Nearly all of a work item is spent waiting on a
government web server and then on a model, so the pool is the throughput lever:
a single worker manages roughly 860 items a day, which is about four months for
a national run.

Each worker owns its database connection, its Crawlee storage directory (Crawlee
purges storage on start, so a shared directory would have one worker wipe
another's request queue mid-run), and its slot in the status snapshot. Sharing
the queue is safe because `ledger.claim` is one atomic statement: the read and
the write used to be separate, which looked fine single-threaded and handed the
same jurisdiction to two workers about a fifth of the time.

One unresearchable jurisdiction no longer sinks the batch it was in. It goes
back to the queue with the reason attached, and `max_attempts` still parks a
repeat offender.

**Search is what actually limits this.** Fetching is spread over thousands of
government hosts, but every worker queries the same two or three search engines,
and a scraped result page throttles by IP long before any documented limit. So
search has its own process-wide ceiling (`search_qpm`, `auto` = 60 with an API
key and 12 without), and an engine that refuses is benched with escalating
backoff while the next one answers. Without a key, twelve queries a minute at
about five queries an item is roughly two items a minute no matter how many
workers run. **The Brave Search key is what makes the pool worth having.**

## Batch reading

Reading the documents is most of what a national run costs, and none of it is
urgent: nobody is waiting on one county's lodging tax to come back in two
seconds. **Read documents in batches** under Settings sends the same requests
through the Message Batches API at half the price, returned within the hour
instead of the second.

Crawling and reading come apart when it is on:

```
off   crawl -> read -> ingest -> check
on    crawl -> park ... submit ... collect -> ingest -> check
```

The crawler still archives every byte and builds the packet, then parks both and
leaves the work item at `awaiting_ai` — a status `claim` will not take and the
stale sweep will not touch, so it sits safely for hours. A submitter posts what
has accumulated (`batch_min_items`, or anything at all when nothing else is in
flight). A collector polls, ingests each result, and hands it to the second
checker exactly as the live path does, so **what reaches Needs you does not
change**.

The second checker stays inline on purpose. It is the cheap model and a small
share of the bill, it needs the ingest to have happened before it can read the
rows back, and keeping it synchronous preserves fail-toward-review: a check that
could not run leaves the item for a human rather than trusting it.

Anthropic only. A failed submit leaves the items queued, because the crawl was
the expensive part and the submit was not.

**The browser pass** is the reason to want this. A county running CivicPlus,
Granicus, or OpenGov renders its rate table in JavaScript, so an HTTP fetch
returns a nav shell and the research unit gets written down as `unknown` when
the number was there all along. Pages that come back with less text than
`render_min_chars` are re-fetched in Chromium and archived again, marked
`rendered` in the packet text. Chromium runs with `--no-sandbox` because the
container runs as root; that is the one isolation layer given up here.

Two behaviour notes:

- Crawlee treats an unreachable `robots.txt` as permissive. The **fail closed
  if robots.txt cannot be fetched** setting therefore only applies to the
  legacy loop. Disallowed URLs are still recorded as `robots_allowed=0` rows.
- A per-host `Crawl-delay` directive is not read. Our own rate cap applies
  instead, so lower **seconds between requests** if a host asks for more room.
- `max_bytes` now rejects an oversized response after reading it rather than
  aborting the stream partway.

Crawlee needs **Python 3.10+**. On an older interpreter (including the system
`python3` on macOS) the import fails and the sequential loop runs instead, so
the collector still works. The Docker image is Python 3.12 and includes
Chromium, which adds roughly 500 MB.
