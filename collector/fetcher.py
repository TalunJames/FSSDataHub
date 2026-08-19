"""Crawlee-backed fetch loop for one work item.

Crawlee brings the three things the sequential loop in `crawl.py` could not
do: retries with backoff, a request queue that survives a crash, and polite
concurrency across hosts. It brings a fourth that matters more for local
government: a real browser for the county sites that render their rate
tables in JavaScript, where an HTTP fetch returns a nav shell and the
research unit gets recorded as `unknown` when the number was there all along.

What Crawlee does NOT do here: every judgement about what counts as an
official host, a relevant link, or a tax document still comes from
`crawl.py`. Archiving, text extraction, and the ledger are untouched. This
module is the transport only.

Requires Python 3.10+. `crawl.crawlee_enabled` checks the import and falls
back to the legacy loop when it fails, so an older interpreter still runs.
"""

import asyncio
import functools
import logging
import os
import threading
import uuid
from datetime import timedelta
from urllib.parse import urlparse

from taxdb import db

from . import store

log = logging.getLogger("collector.fetcher")

# Set once, when a browser pass proves impossible in this container (usually
# `playwright install chromium` was never run). Avoids retrying per item.
_browser_broken = False


class Unavailable(Exception):
    """Crawlee could not run here; the caller should use the legacy loop."""


def available():
    try:
        from crawlee.crawlers import HttpCrawler  # noqa: F401
    except Exception:
        return False
    return True


def browser_available():
    if _browser_broken:
        return False
    try:
        from crawlee.crawlers import PlaywrightCrawler  # noqa: F401
    except Exception:
        return False
    return True


# Crawlee purges its storage directory on start. Two workers sharing one
# directory purge each other's request queue mid-run, so every worker gets its
# own. The slot is set by the worker thread before it processes an item.
_slot = threading.local()


def set_slot(n):
    _slot.n = int(n)


def current_slot():
    return getattr(_slot, "n", 0)


def storage_dir(slot=None):
    slot = current_slot() if slot is None else slot
    return os.path.join(db.DATA_DIR, "crawlee", "w%d" % slot)


def _tasks_per_minute(settings):
    """Carry `delay_seconds` over as a rate cap rather than a sleep.

    The old loop slept between every fetch, so 2.0 meant 30 fetches a
    minute for the whole item. Keep that ceiling, but let Crawlee spend it
    across several hosts at once instead of one at a time.

    The ceiling is global, not per worker. Crawlee only knows about the item
    in front of it, so the budget is divided by the worker count: eight
    workers at two seconds still make thirty requests a minute between them,
    which is what the setting says on the page and what a county's web server
    experiences. Without the division, raising the worker count would quietly
    multiply the load we put on government sites.
    """
    delay = store.as_float(settings.get("delay_seconds"), 2.0)
    if delay <= 0:
        return float("inf")
    return max(1.0, (60.0 / delay) / store.worker_count(settings))


def _plan(settings):
    return {
        "max_pages": store.as_int(settings.get("max_pages_per_item"), 16),
        "max_depth": store.as_int(settings.get("max_depth"), 3),
        "max_bytes": store.as_int(settings.get("max_bytes"), 8000000),
        "max_chars": store.as_int(settings.get("max_text_chars"), 80000),
        "retries": store.as_int(settings.get("max_retries"), 3),
        "concurrency": max(1, store.as_int(settings.get("concurrency"), 4)),
        "rate": _tasks_per_minute(settings),
        "render": store.as_bool(settings.get("browser_render")),
        "render_pages": store.as_int(settings.get("max_render_pages"), 4),
        "render_min": store.as_int(settings.get("render_min_chars"), 400),
        "user_agent": settings.get("user_agent") or store.DEFAULTS["user_agent"],
        "content_filter": store.as_bool(settings.get("content_filter", "1")),
    }


def crawl_item(conn, client, settings, run_id, geoid, category, name, state,
               diag=None):
    """Same contract as `crawl.crawl_item_legacy`: (pages, combined_text).

    diag collects search counters so a blocked search engine is reported the
    same way whichever engine ran.
    """
    from . import crawl

    if diag is None:
        diag = crawl.new_diag()
    crawl.configure_keywords(settings)
    j = conn.execute("SELECT kind FROM jurisdiction WHERE geoid=?", (geoid,)).fetchone()
    kind = j["kind"] if j else None

    # Search is sync httpx and runs before the loop opens, as it always has.
    seed_urls = crawl.item_seeds(
        conn, client, settings, geoid, category, name, state, kind, diag=diag)
    if not seed_urls:
        return [], ""

    plan = _plan(settings)
    ctx = {
        "pages": [],
        "texts": [],
        "text_shas": set(),
        "seed_hosts": crawl.seed_hosts_for(seed_urls),
        "thin": [],
    }
    try:
        asyncio.run(_run_item(
            conn, settings, plan, run_id, geoid, category, name, state,
            seed_urls, ctx, client, kind, diag))
    except Exception as exc:
        # Hand the item to the legacy loop only if nothing was recorded yet.
        # Once pages are in the ledger, falling back would archive them twice.
        if not ctx["pages"]:
            raise Unavailable(str(exc))
        log.warning("crawlee run for %s/%s ended early: %s", geoid, category, exc)

    return ctx["pages"], crawl.combine_texts(ctx["texts"], plan["max_chars"])


async def _run_item(conn, settings, plan, run_id, geoid, category, name, state,
                    seed_urls, ctx, client, kind, diag=None):
    from . import crawl

    await _http_round(conn, plan, run_id, geoid, category, name, state,
                      seed_urls, ctx, plan["max_pages"])

    # The legacy loop searched a second time when the first pass found
    # nothing tax-shaped. Keep that, with whatever page budget is left.
    spent = len(ctx["pages"])
    if (spent < plan["max_pages"] and store.as_bool(settings.get("web_search"))
            and not crawl._has_signal(ctx["pages"])):
        extra = await asyncio.to_thread(
            functools.partial(crawl.search_web, client, name, state, category,
                              kind=kind, limit=16, settings=settings, diag=diag))
        already = set(seed_urls)
        extra = [u for u in extra if u not in already]
        if extra:
            ctx["seed_hosts"] |= crawl.seed_hosts_for(extra)
            await _http_round(conn, plan, run_id, geoid, category, name, state,
                              extra, ctx, plan["max_pages"] - spent)

    if plan["render"] and ctx["thin"] and browser_available():
        await _render_round(conn, plan, run_id, geoid, category, ctx)


async def _fresh_queue():
    """A request queue of our own, for exactly one crawler round.

    Crawlee caches storage instances process-wide by (alias, storage_dir),
    and the shared default queue broke two ways. First, `asyncio.run` per
    item means the cached queue's lock is bound to a previous item's closed
    event loop, so every item after the first crashed and fell back to the
    legacy loop — silently, re-running its web searches. Second, pending
    requests left when one round hit its budget bled into the next round and
    the browser round, which then spent their budgets on someone else's URLs.
    A unique alias sidesteps the cache; drop() deletes the queue and evicts
    the instance when the round is done.
    """
    from crawlee.configuration import Configuration
    from crawlee.storages import RequestQueue

    return await RequestQueue.open(
        alias="round-%s" % uuid.uuid4().hex,
        configuration=Configuration(storage_dir=storage_dir(),
                                    purge_on_start=True))


def _crawler_kwargs(plan, budget):
    from crawlee import ConcurrencySettings
    from crawlee.configuration import Configuration

    return {
        "max_requests_per_crawl": budget,
        "max_request_retries": plan["retries"],
        # A blocked response (401/403/429) from a government site is the
        # site's answer, not a fingerprint problem: every rotated session
        # comes from the same address with the same honest UA, so the
        # default ten rotations were ten identical fetches.
        "max_session_rotations": 1,
        # Crawlee checks robots.txt for every request when this is on, which
        # is what the old loop did on its own. Note it treats an unreachable
        # robots.txt as permissive; `strict_robots` fail-closed behaviour
        # only applies to the legacy loop.
        "respect_robots_txt_file": True,
        "concurrency_settings": ConcurrencySettings(
            min_concurrency=1,
            desired_concurrency=plan["concurrency"],
            max_concurrency=plan["concurrency"],
            max_tasks_per_minute=plan["rate"],
        ),
        "request_handler_timeout": timedelta(seconds=120),
        "configuration": Configuration(
            storage_dir=storage_dir(),
            purge_on_start=True,
        ),
        "configure_logging": False,
    }


def _http_client(plan):
    """An honest User-Agent, not a spoofed browser fingerprint.

    Crawlee's default impit client impersonates Firefox. We are crawling
    government sites for public records and say who we are, so disable the
    header generator and send the configured UA.
    """
    from crawlee.http_clients import HttpxHttpClient

    return HttpxHttpClient(
        header_generator=None,
        headers={"User-Agent": plan["user_agent"]},
        timeout=45.0,
    )


async def _http_round(conn, plan, run_id, geoid, category, name, state,
                      urls, ctx, budget):
    """One Crawlee pass over `urls`, following tax-looking links from them."""
    from crawlee import Request
    from crawlee.crawlers import HttpCrawler

    from . import crawl

    if budget <= 0 or not urls:
        return

    try:
        rq = await _fresh_queue()
        crawler = HttpCrawler(http_client=_http_client(plan),
                              request_manager=rq,
                              **_crawler_kwargs(plan, budget))
    except Exception as exc:
        raise Unavailable("could not start HttpCrawler: %s" % exc)

    @crawler.router.default_handler
    async def handler(context):
        url = context.request.url
        depth = int((context.request.user_data or {}).get("depth") or 0)
        resp = context.http_response
        blob = await resp.read()
        final = context.request.loaded_url or url
        ctype = resp.headers.get("content-type", "") or ""

        if len(blob) > plan["max_bytes"]:
            # Crawlee reads the body before we see it, so this rejects after
            # the fact rather than aborting mid-stream like the old loop.
            page = crawl.error_page(
                url, "response larger than %d bytes" % plan["max_bytes"], final)
        else:
            page = crawl.page_record(url, final, resp.status_code, ctype, blob)
        _keep(conn, run_id, geoid, category, page, ctx, plan)

        final_host = (urlparse(final).hostname or "").lower()
        if final_host and crawl.allowed_host(final_host, ctx["seed_hosts"],
                                             name=name, state=state):
            ctx["seed_hosts"].add(final_host)

        if depth >= plan["max_depth"] or not blob or "html" not in ctype:
            return
        targets = crawl.follow_targets(url, final, blob, depth, ctx["seed_hosts"],
                                       name=name, state=state, category=category)
        # Documents first: the page budget can run out before the queue does.
        targets.sort(key=lambda pair: not pair[1])
        if targets:
            await context.add_requests([
                Request.from_url(href, user_data={"depth": depth + 1})
                for href, _ in targets
            ])

    @crawler.on_skipped_request
    async def skipped(url, reason):
        if reason != "robots_txt":
            return
        page = crawl.error_page(
            url, "robots.txt disallows this URL", robots_allowed_flag=0)
        _keep(conn, run_id, geoid, category, page, ctx, plan, archive=False)

    @crawler.failed_request_handler
    async def failed(context, error):
        page = crawl.error_page(context.request.url, str(error)[:400])
        _keep(conn, run_id, geoid, category, page, ctx, plan, archive=False)

    try:
        await crawler.run([
            Request.from_url(u, user_data={"depth": 0}) for u in urls
        ])
    finally:
        try:
            await rq.drop()
        except Exception:
            pass


async def _render_round(conn, plan, run_id, geoid, category, ctx):
    """Re-fetch thin HTML pages in a real browser and keep what renders."""
    global _browser_broken
    from crawlee.crawlers import PlaywrightCrawler

    from . import crawl

    urls = list(dict.fromkeys(ctx["thin"]))[:plan["render_pages"]]
    if not urls:
        return

    try:
        rq = await _fresh_queue()
        crawler = PlaywrightCrawler(
            headless=True,
            browser_type="chromium",
            # Chromium's own sandbox refuses to start as root, which is how
            # this container runs. The pages are public government sites and
            # nothing from them is executed outside the browser, but this is
            # the one place we give up a layer of isolation.
            browser_launch_options={"args": ["--no-sandbox"]},
            request_manager=rq,
            **_crawler_kwargs(plan, len(urls)))
    except Exception as exc:
        _browser_broken = True
        log.warning("browser pass unavailable, staying with HTTP text: %s", exc)
        return

    @crawler.router.default_handler
    async def handler(context):
        html = await context.page.content()
        blob = (html or "").encode("utf-8")
        status = context.response.status if context.response else None
        page = crawl.page_record(
            context.request.url, context.page.url or context.request.url,
            status, "text/html; charset=utf-8", blob)
        page["rendered"] = True
        _keep(conn, run_id, geoid, category, page, ctx, plan, label="render")

    @crawler.failed_request_handler
    async def failed(context, error):
        log.info("render failed for %s: %s", context.request.url, error)

    try:
        await crawler.run(urls)
    except Exception as exc:
        _browser_broken = True
        log.warning("browser pass failed, staying with HTTP text: %s", exc)
    finally:
        try:
            await rq.drop()
        except Exception:
            pass


def _keep(conn, run_id, geoid, category, page, ctx, plan, archive=True,
          label="crawl"):
    """Archive, record, and collect one page. Mirrors the legacy loop."""
    from . import crawl

    # An off-topic page is still logged and its links still followed;
    # only storage and model tokens are withheld from it.
    keep = not plan["content_filter"] or crawl.content_relevant(page, category)
    aid = None
    if archive and keep and page.get("blob") and page.get("robots_allowed"):
        try:
            aid = crawl.archive_page(conn, page, period_label=label)
        except Exception as exc:
            page["error"] = (page.get("error") or "") + "; archive: %s" % exc
    crawl.record_page(conn, run_id, geoid, category, page, aid)
    ctx["pages"].append(page)

    if keep and page.get("text") and not page.get("error"):
        # Same bytes reached via www/non-www or a redirect variant would be
        # sent to the model twice; one copy is plenty.
        sha = page.get("sha256")
        if not sha or sha not in ctx["text_shas"]:
            if sha:
                ctx["text_shas"].add(sha)
            header = "URL: %s\nTITLE: %s\n" % (
                page.get("final_url") or page["url"], page.get("title") or "")
            if page.get("rendered"):
                header += "NOTE: rendered in a browser\n"
            ctx["texts"].append((crawl.is_document_page(page),
                                 len(ctx["texts"]), header + page["text"]))

    if _is_thin(page, plan):
        ctx["thin"].append(page.get("final_url") or page["url"])
    # The bytes are archived and the caller's handler keeps its own reference
    # for link harvesting; holding every blob here kept up to 16 x 8MB per
    # worker in memory until the item completed.
    page["blob"] = b""


def _is_thin(page, plan):
    """An HTML page that came back with too little text to be the real page.

    No keyword test here: every page past the seeds already earned its
    fetch through the follow filter, and the anchor text that qualified it
    is gone by now — a URL-only re-test would starve JS rate pages whose
    address is as plain as /rates.
    """
    from . import crawl

    if page.get("rendered") or page.get("error") or not crawl.page_bytes(page):
        return False
    if "html" not in (page.get("content_type") or ""):
        return False
    return len(page.get("text") or "") < plan["render_min"]
