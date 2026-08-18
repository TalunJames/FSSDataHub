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
    "use_crawlee": "1",                # off = sequential httpx loop
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
    "llama_model": "llama3.1",
    "researcher": "collector",
    # Second checker: a separate AI pass that sanity-checks each extraction.
    # Items that pass are marked complete without human review; anything the
    # checker flags lands on the Review page with the reason.
    "checker_enabled": "1",
    "checker_model": "",       # empty = same model as the extractor; with
                               # Anthropic, claude-haiku-4-5 keeps checks cheap
    "checker_max_chars": "40000",
}

# Shown as suggestions in the settings UI; the field stays free-text.
ANTHROPIC_MODELS = (
    "claude-sonnet-5",    # recommended: strong extraction at reasonable cost
    "claude-haiku-4-5",   # cheapest; fine for the checker or simple pages
    "claude-opus-5",      # deepest reading; slowest and priciest
)

VALID_KINDS = sorted(JURISDICTION_KINDS)
VALID_CATEGORIES = sorted(WORK_CATEGORIES)
VALID_SEARCH = ("auto", "brave", "scrape")
VALID_PROVIDERS = ("none", "openai", "anthropic", "llama")
VALID_SCHEDULE = ("hourly", "every_6h", "daily", "weekly")
