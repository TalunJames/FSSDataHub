# Collector (TrueNAS SCALE)

A web app that lives next to `taxdb` and populates it three ways:

1. **Crawl** official tax sources (state DOR, recorded URLs, then a web search for the county/city site and tax PDFs)
2. **Extract** with OpenAI-compatible APIs, Anthropic, or a local Llama/Ollama server
3. **Manual entry** — drop a URL/PDF/photo for the AI, or answer only the questions still open (skip any)

Every extraction is re-read by a **second checker** — deterministic sanity checks (the quoted source text must appear in the crawled documents, rates must be plausible for their unit) plus a second, skeptical model pass. Items that pass are marked `complete` automatically; only flagged items land on the Review page, with the reason attached. If the checker cannot run, items fail toward review, never toward trust. Turn it off under **Keys & crawl → Second checker** to review everything by hand.

The crawler respects `robots.txt`, stays on government hosts, rate-limits, and archives every byte before anyone extracts a number.

With Anthropic, `claude-sonnet-5` is the recommended extractor (the default); setting the checker model to `claude-haiku-4-5` keeps the second pass cheap. **Save & test key** on the settings page verifies a key with one tiny call.

## Run locally

```bash
python3 -m pip install -r requirements.txt
python3 -m collector
```

Open http://127.0.0.1:8080

```bash
./bin/taxdb init --with-sources   # or use Initialize schema in the UI
./bin/taxdb seed                  # or Seed jurisdictions in the UI
```

Then **Plan work** for a state and either toggle **Continuous**, click **Run now**, or set a schedule under **Keys & crawl**.

Before crawling a state's sales tax, load the Streamlined Sales Tax files (**Load SST rate files** on the dashboard, or `taxdb fetch sst --state XX`). Load **Census of Governments 2022** for the collections prior, and **Fetch Open US Law statutes** so packets include the actual statute text. The crawler skips work items already filled by an adapter.

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
