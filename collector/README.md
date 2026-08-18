# Collector (TrueNAS SCALE)

A web app that lives next to `taxdb` and populates it three ways:

1. **Crawl** official tax sources (state DOR, recorded URLs, then a web search for the county/city site and tax PDFs)
2. **Extract** with OpenAI-compatible APIs, Anthropic, or a local Llama/Ollama server
3. **Manual entry** — drop a URL/PDF/photo for the AI, or answer only the questions still open (skip any)

Findings still go through `needs_review`. The crawler respects `robots.txt`, stays on government hosts, rate-limits, and archives every byte before anyone extracts a number.

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
