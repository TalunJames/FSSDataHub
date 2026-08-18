"""Collector defaults and secret-key names.

Values live in collector_setting. Environment variables override on first
boot only when the row is empty, so the UI remains the source of truth
after that.
"""

from taxdb.vocab import CATEGORIES, JURISDICTION_KINDS

SECRET_KEYS = {
    "openai_api_key",
    "anthropic_api_key",
    "llama_api_key",
}

DEFAULTS = {
    "continuous_enabled": "0",
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
    "filter_categories": ",".join(CATEGORIES),
    "min_pop": "0",
    "provider": "none",                # none | openai | anthropic | llama
    "openai_api_key": "",
    "openai_base_url": "https://api.openai.com/v1",
    "openai_model": "gpt-4o-mini",
    "anthropic_api_key": "",
    "anthropic_model": "claude-haiku-4-5",
    "llama_base_url": "http://host.docker.internal:11434",
    "llama_api_key": "",
    "llama_model": "llama3.1",
    "researcher": "collector",
}

VALID_KINDS = sorted(JURISDICTION_KINDS)
VALID_CATEGORIES = sorted(CATEGORIES)
VALID_PROVIDERS = ("none", "openai", "anthropic", "llama")
VALID_SCHEDULE = ("hourly", "every_6h", "daily", "weekly")
