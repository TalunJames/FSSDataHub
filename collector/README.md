# Collector (TrueNAS SCALE)

A web app that lives next to `taxdb` and fills it without being told what to do
next. Add an AI key and press **Start collecting**. There is one button on the
home page because there is one decision: run, or pause.

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

Every extraction is re-read by a **second checker** — deterministic sanity checks plus a second, skeptical model pass, with a different prompt per pass: rates are checked against their quotes, measures against their canvasses, thresholds against the statute text. Items that pass are marked `complete` automatically; only flagged items land under **Needs you**, with the reason attached. If the checker cannot run, items fail toward review, never toward trust. Turn it off under **Settings → Second checker** to review everything by hand.

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
list page. To run a bulk file by hand, use **Do it by hand** under Settings. The
crawler skips work items already filled by an adapter.

## Pages

| page | what it is for |
|---|---|
| **Home** | Start or pause, and one line saying what is happening now |
| **Needs you** | Only what the second check flagged, with the rows to judge it by |
| **Add a source** | Drop the real document, or answer the questions still open |
| **The data** | What the database holds, and which product views are still empty |
| Work list · Activity log · Settings | The detail, out of the daily path |

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
