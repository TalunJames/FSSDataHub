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
- Records older than `refresh_days` go back in the queue on their own.

Every extraction is re-read by a **second checker** — deterministic sanity checks plus a second, skeptical model pass, with a different prompt per pass: rates are checked against their quotes, measures against their canvasses, thresholds against the statute text. Items that pass are marked `complete` automatically; only flagged items land under **Review**, with the reason attached. If the checker cannot run, items fail toward review, never toward trust. Turn it off under **Setup → Double-check the AI** to review everything by hand.

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
| `concurrency` | parallel fetches within one work item |
| `max_retries` | attempts per page before it is recorded as an error |
| `browser_render` | re-fetch thin pages in headless Chromium |
| `max_render_pages` | browser fetches allowed per work item |
| `render_min_chars` | an HTML page under this much text counts as thin |

**Seconds between requests** still caps the rate. It used to be a sleep
between single fetches; now it is a ceiling on fetches per minute for the item,
spent across hosts instead of idling. Two seconds means thirty fetches a
minute either way.

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
