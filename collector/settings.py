"""Collector defaults and secret-key names.

Values live in collector_setting. Environment variables override on first
boot only when the row is empty, so the UI remains the source of truth
after that.
"""

from taxdb.vocab import JURISDICTION_KINDS, WORK_CATEGORIES

SECRET_KEYS = {
    "openai_api_key",
    "anthropic_api_key",
    "llama_api_key",
    "search_api_key",
}

DEFAULTS = {
    "continuous_enabled": "0",
    # Autopilot decides what to research next instead of waiting for someone
    # to draw a slice by hand. Without it, continuous mode stops being useful
    # the moment the planned queue empties.
    "autopilot_enabled": "1",
    "autopilot_bulk": "1",          # load free national bulk files first
    "autopilot_statutes": "1",      # fetch statute corpora for open states
    "autopilot_elections": "1",     # research local ballot measures too
    "autopilot_chunk": "200",       # jurisdictions added to the queue per step
    "refresh_days": "365",          # re-research anything older than this
    "max_attempts": "4",            # give up on an item after this many tries
    # Web search. Scraped result pages get blocked; a key makes the crawler
    # reliable. auto = use the API when a key is present, otherwise scrape.
    "search_provider": "auto",      # auto | brave | scrape
    "search_api_key": "",
    # Queries per minute, process-wide, across every worker. Search is the one
    # thing that does not scale with worker count: fetching is spread over
    # thousands of government hosts, but every worker queries the same engines,
    # and a scraped engine starts refusing long before any documented limit.
    # auto = 60 with an API key, 12 when scraping. 0 = no ceiling.
    # This is the real cap on throughput without a key: at ~5 queries an item,
    # 12/min is about 2 items a minute no matter how many workers run.
    "search_qpm": "auto",
    "schedule_enabled": "0",
    "schedule_kind": "daily",          # hourly | every_6h | daily | weekly
    "schedule_time": "02:00",
    "schedule_weekday": "0",           # Monday=0 ... for weekly
    "last_scheduled_at": "",
    "burst_size": "20",
    "delay_seconds": "2.0",
    "max_pages_per_item": "16",
    "max_depth": "3",
    "max_bytes": "8000000",
    "max_text_chars": "80000",
    "web_search": "1",
    "strict_robots": "0",
    # Follow filter. A link is fetched only when its own URL or link text
    # matches the built-in tax and election keywords; these add to that
    # list. The defaults cover local revenue-measure terms the built-ins
    # miss: bond pages outside the elections pass, mill rates written
    # without "millage", gross receipts pages, school sinking funds,
    # special district and Mello-Roos financing, and fee schedules.
    # Multi-word phrases match across spaces, hyphens, and underscores.
    "crawl_keywords": ("bond, mill rate, gross receipts, sinking fund, "
                       "special district, impact fee, fee schedule, "
                       "mello-roos, community facilities district"),
    # Keep only pages whose fetched content shows a keyword. Off-topic
    # pages are still logged and their links still followed, but they are
    # not archived and their text is not sent to the AI.
    "content_filter": "1",
    "use_crawlee": "1",                # off = sequential httpx loop
    # Work items researched at once. Nearly all of an item is spent waiting on
    # the network and the model, so this is the throughput lever. It does not
    # raise the load on any government host: delay_seconds is a global ceiling
    # divided among workers. Past about 4, set a Brave Search key — scraped
    # result pages are what gives out first.
    "workers": "4",
    "concurrency": "4",                # parallel fetches within one item
    "max_retries": "3",
    "browser_render": "1",             # re-fetch thin pages in Chromium
    "max_render_pages": "4",
    "render_min_chars": "400",
    "user_agent": "TaxDatabaseCollector/0.3 (local tax research; TrueNAS)",
    "filter_states": "",
    "filter_kinds": "county,place,state",
    "filter_categories": ",".join(WORK_CATEGORIES),
    "min_pop": "0",
    "provider": "none",                # none | openai | anthropic | llama
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_model": "gpt-4o-mini",
    "anthropic_api_key": "",
    "anthropic_model": "claude-sonnet-5",
    "llama_base_url": "http://host.docker.internal:11434",
    "llama_api_key": "",
    "llama_model": "qwen3-fast",
    "researcher": "collector",
    # Batch extraction. The same requests at half the price, returned within
    # the hour instead of the second. Off by default because it changes the
    # feel of the app: an item is crawled now and read later, so findings stop
    # appearing seconds after a page is fetched. For a national run that is a
    # trade worth making — extraction is most of the bill and nothing about
    # filling a ledger is latency-sensitive. Anthropic only.
    "batch_extract": "0",
    "batch_max_items": "200",   # requests per batch
    "batch_min_items": "25",    # wait for this many before sending, unless idle
    # Results ingested per coordinator tick. Downloading a batch is one HTTP
    # stream; ingesting and second-checking each result is a model call, so
    # applying is metered to keep a finished batch from stalling the crawl.
    "batch_apply_per_tick": "25",
    # Second checker: a separate AI pass that sanity-checks each extraction.
    # Items that pass are marked complete without human review; anything the
    # checker flags lands on the Review page with the reason.
    "checker_enabled": "1",
    # Where the second pass runs. Empty = the extractor's provider. Default
    # is the local llama model: the check matters less than the extraction,
    # and a free second opinion next door beats paying twice per item. If the
    # local model cannot be reached, items fail toward review, never toward
    # trust — the crawl keeps going, findings just wait for a person.
    "checker_provider": "llama",
    "checker_model": "",       # empty = the checker provider's default model;
                               # with Anthropic, claude-haiku-4-5 keeps checks cheap
    "checker_max_chars": "80000",   # match max_text_chars: a quote in the
                                   # back half of the documents must be findable
    # The version whose welcome screen was acknowledged. When it trails the
    # running version, page loads land on /welcome once so an update is seen
    # and anything new that needs a decision gets one.
    "setup_seen_version": "",
}

# Shown as suggestions in the settings UI; the field stays free-text.
ANTHROPIC_MODELS = (
    "claude-sonnet-5",    # recommended: strong extraction at reasonable cost
    "claude-haiku-4-5",   # cheapest; fine for the checker or simple pages
    "claude-opus-5",      # deepest reading; slowest and priciest
)

# Local models offered by name. Any model pulled into Ollama works; these
# get first-class options because they are the realistic candidates for the
# second checker on the NAS. The tag must match `ollama list` exactly.
LLAMA_MODELS = (
    "qwen3-fast",   # the checker default Carter picked
    "llama3.1",     # lighter; fine when memory is tight
    "gpt-oss:20b",  # stronger reasoning; needs more memory, pull it first
)

VALID_KINDS = sorted(JURISDICTION_KINDS)
VALID_CATEGORIES = sorted(WORK_CATEGORIES)
VALID_SEARCH = ("auto", "brave", "scrape")
VALID_PROVIDERS = ("none", "openai", "anthropic", "llama")
# Empty string = run the second pass on the extractor's own provider.
VALID_CHECKER_PROVIDERS = ("", "llama", "anthropic", "openai")
VALID_SCHEDULE = ("hourly", "every_6h", "daily", "weekly")

# Shown on the welcome screen after an update. Plain words, newest release
# only — this is read by the owner, not a developer.
WHATS_NEW = (
    ("A large round of fixes under the hood",
     "The crawler stops wandering into careers pages and staff directories, "
     "re-archives documents whose contents changed, and reads old county "
     "pages' special characters correctly. The double-checker now judges "
     "only what each run actually found (so it is much faster on the local "
     "model) and refuses to wave anything through on a garbled answer. "
     "Items can no longer get stuck invisibly when batch reading is turned "
     "off, and a provider outage no longer walks good items into 'blocked'. "
     "Two election measures without ballot numbers stay two records, and "
     "revoked state authority no longer shows as live. Stored API keys can "
     "now be removed from Settings with the new Remove button."),
    ("The double-check moved to your local model",
     "The second AI pass that verifies each extraction now runs on the "
     "NAS's own model (Ollama) by default, so double-checking is free. "
     "You can pick which local model does it; qwen3-fast is the default. "
     "Confirm below that the collector can reach it. If it ever can't, "
     "nothing is trusted blindly: findings simply wait on the Review page."),
    ("This setup screen is new",
     "After every update, the app opens here once so you can see what "
     "changed and fill in anything new it needs. Skip it any time; it "
     "won't come back until the next update."),
    ("Smarter crawling",
     "The crawler now follows and stores only pages that look like they "
     "are about taxes, bonds, and elections, so off-topic pages stop "
     "costing money to read."),
)
