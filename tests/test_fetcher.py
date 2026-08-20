"""Crawlee fetch loop, driven against a stubbed engine.

Crawlee needs Python 3.10+ and is not importable on every machine that runs
these tests, so this installs a fake `crawlee` package that mimics the parts
`fetcher.py` uses: a crawler that collects the registered handlers, then
drives them over canned responses while honouring the page budget and the
links a handler enqueues.

This exercises our integration logic — budget, depth, host policy, archiving,
thin-page detection, the second search round. It cannot catch a mismatch with
the real Crawlee API; that is what `collector/README.md` and a run on the NAS
are for.
"""

import asyncio
import sys
import types
import unittest

from tests._db import DbTest


class _Headers(dict):
    def get(self, key, default=None):
        return dict.get(self, key.lower(), default)


class _Response:
    def __init__(self, status, ctype, blob):
        self.status_code = status
        self.headers = _Headers({"content-type": ctype})
        self._blob = blob

    async def read(self):
        return self._blob


class _Request:
    def __init__(self, url, user_data=None):
        self.url = url
        self.loaded_url = url
        self.user_data = user_data or {}

    @classmethod
    def from_url(cls, url, user_data=None, **kwargs):
        return cls(url, user_data=user_data)


class _Context:
    def __init__(self, request, response, queue):
        self.request = request
        self.http_response = response
        self._queue = queue

    async def add_requests(self, requests):
        self._queue.extend(requests)


_SessionError = type("SessionError", (Exception,), {})


class _FakeCrawler:
    """Drives handlers over `pages`, a url -> (status, ctype, blob) map."""

    pages = {}
    robots_blocked = set()
    broken = set()
    blocked = {}       # url -> HTTP status behind a "session is blocked" fail
    started = []

    def __init__(self, **kwargs):
        self.opts = kwargs
        self.handler = None
        self.skipped_cb = None
        self.failed_cb = None
        type(self).started.append(kwargs)

    @property
    def router(self):
        crawler = self

        class _Router:
            def default_handler(self, fn):
                crawler.handler = fn
                return fn

        return _Router()

    def on_skipped_request(self, fn):
        self.skipped_cb = fn
        return fn

    def failed_request_handler(self, fn):
        self.failed_cb = fn
        return fn

    async def run(self, requests):
        budget = self.opts.get("max_requests_per_crawl") or 0
        queue = list(requests)
        seen = set()
        fetched = 0
        while queue and fetched < budget:
            req = queue.pop(0)
            if isinstance(req, str):
                req = _Request(req, user_data={"depth": 0})
            if req.url in seen:
                continue
            seen.add(req.url)
            fetched += 1
            if req.url in type(self).robots_blocked:
                if self.skipped_cb:
                    await self.skipped_cb(req.url, "robots_txt")
                continue
            if req.url in type(self).broken:
                if self.failed_cb:
                    await self.failed_cb(_Context(req, None, queue),
                                         RuntimeError("connection reset"))
                continue
            if req.url in type(self).blocked:
                if self.failed_cb:
                    await self.failed_cb(
                        _Context(req, None, queue),
                        _SessionError(
                            "Assuming the session is blocked based on HTTP "
                            "status code %d" % type(self).blocked[req.url]))
                continue
            spec = type(self).pages.get(req.url)
            if spec is None:
                if self.failed_cb:
                    await self.failed_cb(_Context(req, None, queue),
                                         RuntimeError("HTTP 404"))
                continue
            status, ctype, blob = spec
            await self.handler(_Context(req, _Response(status, ctype, blob), queue))


class _Page:
    def __init__(self, url, html):
        self.url = url
        self._html = html

    async def content(self):
        return self._html


class _PwResponse:
    def __init__(self, status):
        self.status = status


class _PwContext:
    def __init__(self, request, page, response):
        self.request = request
        self.page = page
        self.response = response


class _FakePlaywrightCrawler(_FakeCrawler):
    """Same handler plumbing, but hands over a page instead of a response."""

    rendered = {}

    async def run(self, requests):
        budget = self.opts.get("max_requests_per_crawl") or 0
        for req in list(requests)[:budget]:
            if isinstance(req, str):
                req = _Request(req, user_data={"depth": 0})
            html = type(self).rendered.get(req.url)
            if html is None:
                if self.failed_cb:
                    await self.failed_cb(_PwContext(req, None, None),
                                         RuntimeError("render timeout"))
                continue
            await self.handler(
                _PwContext(req, _Page(req.url, html), _PwResponse(200)))


class _FakeBrowserFetch:
    """Canned responses for the browser-network fetch of blocked documents.

    Mimics the playwright surface fetcher._fetch_round touches:
    async_playwright() -> chromium.launch() -> new_context() -> request.get().
    """

    responses = {}   # url -> (status, ctype, blob)
    fetched = []

    class _Response:
        def __init__(self, url, status, ctype, blob):
            self.url = url
            self.status = status
            self.headers = _Headers({"content-type": ctype})
            self._blob = blob

        async def body(self):
            return self._blob

    class _RequestContext:
        async def get(self, url, timeout=None):
            _FakeBrowserFetch.fetched.append(url)
            spec = _FakeBrowserFetch.responses.get(url)
            if spec is None:
                raise RuntimeError("net::ERR_FAILED at %s" % url)
            status, ctype, blob = spec
            return _FakeBrowserFetch._Response(url, status, ctype, blob)

    class _Context:
        request = None

        def __init__(self):
            self.request = _FakeBrowserFetch._RequestContext()

    class _Browser:
        async def new_context(self):
            return _FakeBrowserFetch._Context()

        async def close(self):
            pass

    class _Chromium:
        async def launch(self, **kwargs):
            return _FakeBrowserFetch._Browser()

    class _PW:
        chromium = None

        def __init__(self):
            self.chromium = _FakeBrowserFetch._Chromium()

    class _Manager:
        async def __aenter__(self):
            return _FakeBrowserFetch._PW()

        async def __aexit__(self, *args):
            return False


def _fake_async_playwright():
    return _FakeBrowserFetch._Manager()


class _FakeRequestQueue:
    """Mirrors the real contract fetcher relies on: open(alias=...,
    configuration=...) returns a queue, drop() deletes it. Aliases are
    recorded so a test can assert each round got its own queue."""

    opened = []
    dropped = []

    def __init__(self, alias):
        self.alias = alias

    @classmethod
    async def open(cls, alias=None, configuration=None, **kwargs):
        cls.opened.append(alias)
        return cls(alias)

    async def drop(self):
        type(self).dropped.append(self.alias)


def _install_fake_crawlee():
    """Put minimal fake `crawlee` and `playwright` packages in sys.modules;
    return an undo callable."""
    saved = {k: v for k, v in sys.modules.items()
             if k.split(".")[0] in ("crawlee", "playwright")}

    root = types.ModuleType("crawlee")
    root.Request = _Request
    root.ConcurrencySettings = lambda **kw: kw

    config = types.ModuleType("crawlee.configuration")
    config.Configuration = lambda **kw: kw

    crawlers = types.ModuleType("crawlee.crawlers")
    crawlers.HttpCrawler = _FakeCrawler
    crawlers.PlaywrightCrawler = _FakePlaywrightCrawler

    clients = types.ModuleType("crawlee.http_clients")
    clients.HttpxHttpClient = lambda **kw: kw

    storages = types.ModuleType("crawlee.storages")
    storages.RequestQueue = _FakeRequestQueue

    pw_root = types.ModuleType("playwright")
    pw_api = types.ModuleType("playwright.async_api")
    pw_api.async_playwright = _fake_async_playwright

    for name, mod in (("crawlee", root), ("crawlee.configuration", config),
                      ("crawlee.crawlers", crawlers),
                      ("crawlee.http_clients", clients),
                      ("crawlee.storages", storages),
                      ("playwright", pw_root),
                      ("playwright.async_api", pw_api)):
        sys.modules[name] = mod

    def undo():
        for name in list(sys.modules):
            if name.split(".")[0] in ("crawlee", "playwright"):
                del sys.modules[name]
        sys.modules.update(saved)

    return undo


MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

COUNTY_HOME = b"""<html><head><title>Franklin County</title></head><body>
<p>Welcome to Franklin County, Ohio. Offices and services.</p>
<a href="/treasurer">Treasurer</a>
<a href="/lodging-tax-rates.pdf">Lodging tax rate table</a>
<a href="/parks">Parks and recreation</a>
<a href="https://facebook.com/franklincounty">Follow us</a>
</body></html>"""

TREASURER = b"""<html><head><title>Treasurer</title></head><body>
<p>The Franklin County transient occupancy tax is levied at 3 percent under
Ohio Revised Code 5739.09, unchanged since 2015. Lodging tax collections are
remitted monthly to the county treasurer, who also administers the sales tax
rate table and the property tax millage schedule for every township.</p>
</body></html>"""

THIN_APP = b"""<html><head><title>Tax Rates</title></head><body>
<div id="root"></div><script src="/app.js"></script></body></html>"""

RENDERED_APP = b"""<html><head><title>Tax Rates</title></head><body>
<div id="root"><table><tr><th>Lodging tax</th><td>3.0%</td></tr></table>
<p>The transient occupancy tax rate for unincorporated Franklin County is
3.0 percent, authorized under Ohio Revised Code 5739.09 and levied since
2015 with no sunset date recorded in the ordinance.</p></div></body></html>"""


class FetcherTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import crawl, fetcher, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        store.apply_schema(self.conn)
        self.store = store
        self.crawl = crawl
        self.fetcher = fetcher
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                state="OH", kind="county")
        self.run_id = store.start_run(self.conn, "burst", provider="none")
        self.undo = _install_fake_crawlee()
        _FakeCrawler.pages = {}
        _FakeCrawler.robots_blocked = set()
        _FakeCrawler.broken = set()
        _FakeCrawler.blocked = {}
        _FakeCrawler.started = []
        _FakePlaywrightCrawler.rendered = {}
        _FakeBrowserFetch.responses = {}
        _FakeBrowserFetch.fetched = []
        self.fetcher._browser_broken = False
        # No live search or web calls from these tests.
        self._real_search = crawl.search_web
        crawl.search_web = lambda *a, **kw: []

    def tearDown(self):
        self.crawl.search_web = self._real_search
        self.undo()
        super().tearDown()

    def settings(self, **over):
        s = dict(self.store.get_all(self.conn))
        s.update({"web_search": "0", "browser_render": "0", "delay_seconds": "0"})
        s.update(over)
        return s

    def crawl_item(self, settings):
        return self.fetcher.crawl_item(
            self.conn, None, settings, self.run_id, self.geoid, "lodging",
            "Franklin County", "OH")

    def seed(self, url="https://franklincountyohio.gov/"):
        self.crawl.seeds_for = lambda *a, **kw: [url]

    def test_follows_links_and_archives(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html", COUNTY_HOME),
            "https://franklincountyohio.gov/treasurer": (200, "text/html", TREASURER),
            "https://franklincountyohio.gov/lodging-tax-rates.pdf":
                (200, "application/pdf", MINIMAL_PDF),
        }
        self.seed()
        pages, text = self.crawl_item(self.settings())

        urls = [p["url"] for p in pages]
        self.assertIn("https://franklincountyohio.gov/", urls)
        self.assertIn("https://franklincountyohio.gov/treasurer", urls)
        self.assertIn("https://franklincountyohio.gov/lodging-tax-rates.pdf", urls)
        # Parks is off-topic and Facebook is blocked, so neither is fetched.
        self.assertFalse(any("parks" in u for u in urls))
        self.assertFalse(any("facebook" in u for u in urls))

        self.assertIn("3 percent", text)
        self.assertIn("URL: https://franklincountyohio.gov/treasurer", text)

        rows = self.conn.execute(
            "SELECT url, archive_file_id FROM crawl_page WHERE run_id=?",
            (self.run_id,)).fetchall()
        self.assertEqual(len(rows), len(pages))
        self.assertTrue(all(r["archive_file_id"] for r in rows))

    def test_offtopic_page_logged_but_not_stored_or_read(self):
        parks = (b"<html><head><title>Parks</title></head><body>"
                 b"<p>The pool opens Memorial Day weekend.</p></body></html>")
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/parks": (200, "text/html", parks),
        }
        self.seed("https://franklincountyohio.gov/parks")
        pages, text = self.crawl_item(self.settings())

        self.assertEqual(len(pages), 1)
        self.assertNotIn("Memorial Day", text)
        row = self.conn.execute(
            "SELECT archive_file_id, text_chars FROM crawl_page WHERE run_id=?",
            (self.run_id,)).fetchone()
        self.assertIsNone(row["archive_file_id"])
        # The fetch itself is still on the ledger, gap included.
        self.assertGreater(row["text_chars"], 0)

    def test_content_filter_off_keeps_everything(self):
        parks = (b"<html><head><title>Parks</title></head><body>"
                 b"<p>The pool opens Memorial Day weekend.</p></body></html>")
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/parks": (200, "text/html", parks),
        }
        self.seed("https://franklincountyohio.gov/parks")
        pages, text = self.crawl_item(self.settings(content_filter="0"))

        self.assertIn("Memorial Day", text)
        row = self.conn.execute(
            "SELECT archive_file_id FROM crawl_page WHERE run_id=?",
            (self.run_id,)).fetchone()
        self.assertIsNotNone(row["archive_file_id"])

    def test_documents_go_first(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html", COUNTY_HOME),
            "https://franklincountyohio.gov/treasurer": (200, "text/html", TREASURER),
            "https://franklincountyohio.gov/lodging-tax-rates.pdf":
                (200, "application/pdf", MINIMAL_PDF),
        }
        self.seed()
        # Budget for the seed plus exactly one link: it must be the PDF.
        pages, _ = self.crawl_item(self.settings(max_pages_per_item="2"))
        self.assertEqual(len(pages), 2)
        self.assertTrue(pages[1]["url"].endswith(".pdf"))

    def test_page_budget_respected(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html", COUNTY_HOME),
            "https://franklincountyohio.gov/treasurer": (200, "text/html", TREASURER),
            "https://franklincountyohio.gov/lodging-tax-rates.pdf":
                (200, "application/pdf", MINIMAL_PDF),
        }
        self.seed()
        pages, _ = self.crawl_item(self.settings(max_pages_per_item="1"))
        self.assertEqual(len(pages), 1)

    def test_depth_zero_does_not_follow(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html", COUNTY_HOME),
            "https://franklincountyohio.gov/treasurer": (200, "text/html", TREASURER),
        }
        self.seed()
        pages, _ = self.crawl_item(self.settings(max_depth="0"))
        self.assertEqual([p["url"] for p in pages],
                         ["https://franklincountyohio.gov/"])

    def test_robots_skip_recorded_as_gap(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html", COUNTY_HOME),
        }
        _FakeCrawler.robots_blocked = {"https://franklincountyohio.gov/"}
        self.seed()
        pages, text = self.crawl_item(self.settings())
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["robots_allowed"], 0)
        self.assertEqual(text, "")
        row = self.conn.execute(
            "SELECT robots_allowed, error, archive_file_id FROM crawl_page "
            "WHERE run_id=?", (self.run_id,)).fetchone()
        self.assertEqual(row["robots_allowed"], 0)
        self.assertIn("robots.txt", row["error"])
        self.assertIsNone(row["archive_file_id"])

    def test_failed_request_recorded(self):
        _FakeCrawler.broken = {"https://franklincountyohio.gov/"}
        self.seed()
        pages, text = self.crawl_item(self.settings())
        self.assertEqual(len(pages), 1)
        self.assertIn("connection reset", pages[0]["error"])
        self.assertEqual(text, "")

    def test_oversized_response_rejected(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html", COUNTY_HOME),
        }
        self.seed()
        pages, _ = self.crawl_item(self.settings(max_bytes="10"))
        self.assertIn("larger than 10 bytes", pages[0]["error"])
        self.assertEqual(pages[0]["blob"], b"")

    def test_no_seeds_is_not_an_error(self):
        self.crawl.seeds_for = lambda *a, **kw: []
        pages, text = self.crawl_item(self.settings())
        self.assertEqual(pages, [])
        self.assertEqual(text, "")

    def test_text_truncated_to_budget(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/treasurer": (200, "text/html", TREASURER),
        }
        self.seed("https://franklincountyohio.gov/treasurer")
        _, text = self.crawl_item(self.settings(max_text_chars="120"))
        self.assertTrue(text.endswith("[truncated]"))

    def test_browser_pass_rescues_a_javascript_page(self):
        url = "https://franklincountyohio.gov/rates"
        _FakeCrawler.pages = {url: (200, "text/html", THIN_APP)}
        _FakePlaywrightCrawler.rendered = {url: RENDERED_APP.decode("utf-8")}
        self.seed(url)
        pages, text = self.crawl_item(
            self.settings(browser_render="1", max_pages_per_item="1"))

        self.assertEqual(len(pages), 2)
        self.assertFalse(pages[0].get("rendered"))
        self.assertTrue(pages[1].get("rendered"))
        # The HTTP fetch found a nav shell; the browser found the rate.
        self.assertNotIn("3.0 percent", pages[0]["text"])
        self.assertIn("3.0 percent", text)
        self.assertIn("rendered in a browser", text)
        # Both versions are archived and recorded, not one replacing the other.
        rows = self.conn.execute(
            "SELECT archive_file_id FROM crawl_page WHERE run_id=?",
            (self.run_id,)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["archive_file_id"] for r in rows))

    def test_failed_render_leaves_the_http_page_alone(self):
        url = "https://franklincountyohio.gov/rates"
        _FakeCrawler.pages = {url: (200, "text/html", THIN_APP)}
        _FakePlaywrightCrawler.rendered = {}
        self.seed(url)
        pages, _ = self.crawl_item(
            self.settings(browser_render="1", max_pages_per_item="1"))
        self.assertEqual(len(pages), 1)
        self.assertFalse(pages[0].get("rendered"))

    def test_render_budget_capped(self):
        home = b"""<html><body>
        <a href="/tax-a">Tax A</a><a href="/tax-b">Tax B</a>
        <a href="/tax-c">Tax C</a></body></html>"""
        thin = (200, "text/html", b"<html><body><div id=root></div></body></html>")
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html", home),
            "https://franklincountyohio.gov/tax-a": thin,
            "https://franklincountyohio.gov/tax-b": thin,
            "https://franklincountyohio.gov/tax-c": thin,
        }
        _FakePlaywrightCrawler.rendered = {
            u: RENDERED_APP.decode("utf-8") for u in _FakeCrawler.pages}
        self.seed()
        pages, _ = self.crawl_item(self.settings(
            browser_render="1", max_render_pages="2", max_pages_per_item="4"))
        self.assertEqual(sum(1 for p in pages if p.get("rendered")), 2)

    def test_browser_pass_skipped_when_text_is_substantial(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/treasurer": (200, "text/html", TREASURER),
        }
        self.seed("https://franklincountyohio.gov/treasurer")
        pages, _ = self.crawl_item(
            self.settings(browser_render="1", max_pages_per_item="1"))
        self.assertEqual(len(pages), 1)
        self.assertFalse(pages[0].get("rendered"))

    def test_pdf_never_sent_to_the_browser(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/rates.pdf":
                (200, "application/pdf", MINIMAL_PDF),
        }
        self.seed("https://franklincountyohio.gov/rates.pdf")
        pages, _ = self.crawl_item(
            self.settings(browser_render="1", max_pages_per_item="1"))
        self.assertEqual(len(pages), 1)
        self.assertFalse(any(p.get("rendered") for p in pages))

    def test_bot_blocked_page_rescued_by_the_browser(self):
        """mass.gov answered a whole night of honest HTTP fetches with 403
        while serving every real browser that asked. A blocked page goes to
        the render round, where a real browser does the asking."""
        url = "https://franklincountyohio.gov/"
        _FakeCrawler.blocked = {url: 403}
        _FakePlaywrightCrawler.rendered = {url: RENDERED_APP.decode("utf-8")}
        self.seed(url)
        pages, text = self.crawl_item(
            self.settings(browser_render="1", max_pages_per_item="1"))
        self.assertIn("3.0 percent", text)
        self.assertTrue(any(p.get("rendered") for p in pages))
        # The HTTP block itself stays on the record.
        self.assertTrue(any("blocked" in (p.get("error") or "") for p in pages))

    def test_blocked_page_left_alone_when_render_is_off(self):
        url = "https://franklincountyohio.gov/"
        _FakeCrawler.blocked = {url: 403}
        _FakePlaywrightCrawler.rendered = {url: RENDERED_APP.decode("utf-8")}
        self.seed(url)
        pages, text = self.crawl_item(self.settings(max_pages_per_item="1"))
        self.assertEqual(text, "")
        self.assertFalse(any(p.get("rendered") for p in pages))

    def test_rate_limited_page_not_retried_in_the_browser(self):
        """429 means stop asking; re-asking with Chromium is not politeness."""
        url = "https://franklincountyohio.gov/"
        _FakeCrawler.blocked = {url: 429}
        _FakePlaywrightCrawler.rendered = {url: RENDERED_APP.decode("utf-8")}
        self.seed(url)
        pages, text = self.crawl_item(
            self.settings(browser_render="1", max_pages_per_item="1"))
        self.assertEqual(text, "")
        self.assertFalse(any(p.get("rendered") for p in pages))

    def test_blocked_document_fetched_through_the_browser(self):
        """A rendered page cannot carry a PDF, and the blocked URLs that
        matter are mostly documents. Their bytes come back through the
        browser's own network stack instead."""
        url = "https://franklincountyohio.gov/lodging-rates.csv"
        _FakeCrawler.blocked = {url: 403}
        _FakeBrowserFetch.responses = {
            url: (200, "text/csv",
                  b"tax,rate\nCounty lodging tax,3.0 percent\n")}
        self.seed(url)
        pages, text = self.crawl_item(
            self.settings(browser_render="1", max_pages_per_item="1"))
        self.assertIn("3.0 percent", text)
        # Fetched, not rendered — and never sent to the render crawler.
        self.assertFalse(any(p.get("rendered") for p in pages))
        self.assertEqual(_FakeBrowserFetch.fetched, [url])
        # The block and the rescue are both on the record.
        self.assertTrue(any("blocked" in (p.get("error") or "") for p in pages))
        self.assertTrue(any(p.get("http_status") == 200 for p in pages))

    def test_document_still_blocked_in_the_browser_stays_a_gap(self):
        url = "https://franklincountyohio.gov/lodging-rates.pdf"
        _FakeCrawler.blocked = {url: 403}
        _FakeBrowserFetch.responses = {
            url: (403, "text/html", b"<html><body>Access denied</body></html>")}
        self.seed(url)
        pages, text = self.crawl_item(
            self.settings(browser_render="1", max_pages_per_item="1"))
        self.assertEqual(text, "")
        self.assertTrue(any((p.get("error") or "") == "HTTP 403" for p in pages))

    def test_blocked_document_fetches_capped_like_renders(self):
        home = b"""<html><body>
        <a href="/tax-a.pdf">Tax A</a><a href="/tax-b.pdf">Tax B</a>
        <a href="/tax-c.pdf">Tax C</a></body></html>"""
        base = "https://franklincountyohio.gov"
        _FakeCrawler.pages = {base + "/": (200, "text/html", home)}
        _FakeCrawler.blocked = {
            base + p: 403 for p in ("/tax-a.pdf", "/tax-b.pdf", "/tax-c.pdf")}
        _FakeBrowserFetch.responses = {
            u: (200, "text/csv", b"tax,rate\nlodging,3\n")
            for u in _FakeCrawler.blocked}
        self.seed(base + "/")
        self.crawl_item(self.settings(
            browser_render="1", max_render_pages="2", max_pages_per_item="4"))
        self.assertEqual(len(_FakeBrowserFetch.fetched), 2)

    def test_blocked_status_parsing(self):
        f = self.fetcher._blocked_status
        self.assertEqual(f(_SessionError(
            "Assuming the session is blocked based on HTTP status code 403")), 403)
        # Resilient to the error arriving pre-stringified under another type.
        self.assertEqual(f(RuntimeError(
            "Assuming the session is blocked based on HTTP status code 429")), 429)
        self.assertIsNone(f(RuntimeError("connection reset")))

    def test_a_round_that_never_returns_is_abandoned(self):
        """crawler.run() has idled forever after finishing every page; the
        ceiling turns that into one lost round instead of a lost night."""
        url = "https://franklincountyohio.gov/treasurer"
        _FakeCrawler.pages = {url: (200, "text/html", TREASURER)}

        class _StallingCrawler(_FakeCrawler):
            stops = []

            async def run(self, requests):
                await super().run(requests)
                self._hang = asyncio.Event()
                await self._hang.wait()

            def stop(self):
                type(self).stops.append(True)
                self._hang.set()

        crawlers = sys.modules["crawlee.crawlers"]
        crawlers.HttpCrawler = _StallingCrawler
        real_deadline = self.fetcher._round_deadline
        self.fetcher._round_deadline = lambda plan, budget: 0.2
        try:
            self.seed(url)
            pages, text = self.crawl_item(self.settings())
        finally:
            self.fetcher._round_deadline = real_deadline
            crawlers.HttpCrawler = _FakeCrawler
        self.assertTrue(_StallingCrawler.stops, "the stuck round was not stopped")
        # Everything fetched before the stall is kept.
        self.assertEqual(len(pages), 1)
        self.assertIn("transient occupancy", text)

    def test_engine_options_carry_the_policy(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html", COUNTY_HOME),
        }
        self.seed()
        self.crawl_item(self.settings(
            max_pages_per_item="9", max_retries="5", concurrency="3",
            delay_seconds="2.0", workers="1"))
        opts = _FakeCrawler.started[0]
        self.assertEqual(opts["max_requests_per_crawl"], 9)
        self.assertEqual(opts["max_request_retries"], 5)
        self.assertTrue(opts["respect_robots_txt_file"])
        self.assertEqual(opts["concurrency_settings"]["max_concurrency"], 3)
        self.assertAlmostEqual(
            opts["concurrency_settings"]["max_tasks_per_minute"], 30.0)

    def test_request_ceiling_is_global_not_per_worker(self):
        """Two seconds means thirty requests a minute, whatever the pool size.

        Crawlee only sees the item in front of it, so the budget is divided by
        the worker count. Without that, raising the worker count would quietly
        multiply the load on county web servers while the settings page still
        claimed thirty a minute."""
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html", COUNTY_HOME),
        }
        self.seed()
        for workers in (1, 4, 8):
            _FakeCrawler.started = []
            self.crawl_item(self.settings(delay_seconds="2.0",
                                          workers=str(workers)))
            per_item = (_FakeCrawler.started[0]["concurrency_settings"]
                        ["max_tasks_per_minute"])
            self.assertAlmostEqual(per_item * workers, 30.0,
                                   msg="global ceiling drifted at %d workers"
                                       % workers)

    def test_second_search_round_when_first_finds_nothing(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html",
                                                b"<html><body>Hello</body></html>"),
            "https://co.franklin.oh.us/lodging.pdf": (200, "application/pdf",
                                                      MINIMAL_PDF),
        }
        self.seed()
        calls = []

        def fake_search(client, name, state, category, kind=None, limit=12,
                        settings=None, diag=None, queries=None):
            calls.append(limit)
            return ["https://co.franklin.oh.us/lodging.pdf"] if calls else []

        self.crawl.search_web = fake_search
        pages, _ = self.crawl_item(self.settings(web_search="1"))
        urls = [p["url"] for p in pages]
        self.assertIn("https://co.franklin.oh.us/lodging.pdf", urls)

    def test_each_round_gets_its_own_request_queue(self):
        """Crawlee caches storage instances process-wide; a shared default
        queue crashed item 2 on item 1's closed event loop and bled leftover
        requests between rounds. Every round must open a fresh alias and
        drop it when done."""
        _FakeRequestQueue.opened = []
        _FakeRequestQueue.dropped = []
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html",
                                                b"<html><body>Hello</body></html>"),
            "https://co.franklin.oh.us/lodging.pdf": (200, "application/pdf",
                                                      MINIMAL_PDF),
        }
        self.seed()

        def fake_search(client, name, state, category, kind=None, limit=12,
                        settings=None, diag=None, queries=None):
            return ["https://co.franklin.oh.us/lodging.pdf"]

        self.crawl.search_web = fake_search
        self.crawl_item(self.settings(web_search="1"))
        self.assertEqual(len(_FakeRequestQueue.opened),
                         len(set(_FakeRequestQueue.opened)),
                         "rounds shared a request queue alias")
        self.assertEqual(sorted(_FakeRequestQueue.opened),
                         sorted(_FakeRequestQueue.dropped),
                         "an opened queue was never dropped")


class BothEnginesReportSearchTests(DbTest):
    """A blocked search engine has to look the same on either transport.

    The Crawlee engine and the legacy loop take different paths to the same
    ledger rows. If only one of them counted a refused search, a throttled
    night would read as thorough depending on which interpreter was running.
    """

    def setUp(self):
        super().setUp()
        try:
            from collector import crawl, fetcher, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.crawl, self.fetcher, self.store = crawl, fetcher, store
        self.undo = _install_fake_crawlee()
        store.apply_schema(self.conn)
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)
        self.run_id = store.start_run(self.conn, "test")
        _FakeCrawler.pages = {}
        _FakeCrawler.robots_blocked = set()
        _FakeCrawler.broken = set()
        _FakeCrawler.blocked = {}
        _FakeCrawler.started = []
        _FakePlaywrightCrawler.rendered = {}
        _FakeBrowserFetch.responses = {}
        _FakeBrowserFetch.fetched = []
        self._real_ddg = crawl._ddg_search
        self._real_bing = crawl._bing_search
        self._real_seeds = crawl.seeds_for
        # Every engine refuses: the (urls, blocked) contract both search
        # helpers return.
        crawl._ddg_search = lambda *a, **kw: ([], True)
        crawl._bing_search = lambda *a, **kw: ([], True)
        crawl.seeds_for = lambda *a, **kw: []

    def tearDown(self):
        self.crawl._ddg_search = self._real_ddg
        self.crawl._bing_search = self._real_bing
        self.crawl.seeds_for = self._real_seeds
        self.undo()
        super().tearDown()

    def _settings(self, **over):
        s = dict(self.store.get_all(self.conn))
        s.update({"web_search": "1", "browser_render": "0", "delay_seconds": "0",
                  "search_qpm": "0", "search_provider": "scrape",
                  "search_api_key": ""})
        s.update(over)
        return s

    def test_crawlee_engine_reports_the_block(self):
        diag = self.crawl.new_diag()
        self.fetcher.crawl_item(
            self.conn, None, self._settings(), self.run_id, self.geoid,
            "lodging_meals", "Franklin County", "OH", diag=diag)
        self.assertTrue(diag["queries"])
        self.assertEqual(diag["blocked"], diag["queries"])
        self.assertIn("blocked", self.crawl.search_note(diag))

    def test_legacy_loop_reports_the_block(self):
        diag = self.crawl.new_diag()
        self.crawl.crawl_item_legacy(
            self.conn, None, self._settings(use_crawlee="0"), self.run_id,
            self.geoid, "lodging_meals", "Franklin County", "OH", diag=diag)
        self.assertTrue(diag["queries"])
        self.assertEqual(diag["blocked"], diag["queries"])
        self.assertIn("blocked", self.crawl.search_note(diag))

    def test_dispatcher_fills_a_diag_it_was_not_given(self):
        """crawl_item creates its own counters when a caller omits them, so
        neither engine can end up counting into nothing."""
        pages, text = self.crawl.crawl_item(
            self.conn, None, self._settings(), self.run_id, self.geoid,
            "lodging_meals", "Franklin County", "OH")
        self.assertEqual(pages, [])


class ThreadLoopTests(unittest.TestCase):
    """The loop a worker thread keeps for every item it handles.

    Crawlee caches its storage instances process-wide and the asyncio locks
    inside them bind to the loop that first touched them, so two items on one
    thread have to share a loop or the second one trips over the first's.
    """

    def setUp(self):
        try:
            from collector import fetcher
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.fetcher = fetcher
        self.addCleanup(self.fetcher.close_thread_loop)
        self.fetcher.close_thread_loop()

    def test_two_items_share_one_loop(self):
        seen = []

        async def note():
            seen.append(asyncio.get_running_loop())

        self.fetcher._run_item_on_thread_loop(note())
        self.fetcher._run_item_on_thread_loop(note())
        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0], seen[1])

    def test_a_lock_from_the_first_item_still_works_in_the_second(self):
        """The exact failure this replaced: a lock outliving its loop."""
        holder = {}

        async def make():
            holder["lock"] = asyncio.Lock()

        async def use():
            async with holder["lock"]:
                return True

        self.fetcher._run_item_on_thread_loop(make())
        self.assertTrue(self.fetcher._run_item_on_thread_loop(use()))

    def test_an_abandoned_task_is_cancelled_before_the_next_item(self):
        """A round that ignored its ceiling must not outlive its item."""
        leaked = {}

        async def forever():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                leaked["cancelled"] = True
                raise

        async def start():
            leaked["task"] = asyncio.ensure_future(forever())
            await asyncio.sleep(0)

        self.fetcher._run_item_on_thread_loop(start())
        self.assertTrue(leaked["task"].done())
        self.assertTrue(leaked.get("cancelled"))

    def test_each_thread_gets_its_own_loop(self):
        import threading

        loops = {}

        def work(name):
            async def note():
                loops[name] = asyncio.get_running_loop()
            self.fetcher._run_item_on_thread_loop(note())
            self.fetcher.close_thread_loop()

        threads = [threading.Thread(target=work, args=(n,)) for n in "ab"]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(loops), 2)
        self.assertIsNot(loops["a"], loops["b"])

    def test_closing_releases_the_loop(self):
        async def note():
            return asyncio.get_running_loop()

        first = self.fetcher._run_item_on_thread_loop(note())
        self.fetcher.close_thread_loop()
        self.assertTrue(first.is_closed())
        second = self.fetcher._run_item_on_thread_loop(note())
        self.assertIsNot(first, second)
        self.assertFalse(second.is_closed())

    def test_closing_twice_is_harmless(self):
        self.fetcher.close_thread_loop()
        self.fetcher.close_thread_loop()


class UnavailableTests(unittest.TestCase):
    def setUp(self):
        try:
            from collector import crawl, fetcher
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.crawl = crawl
        self.fetcher = fetcher

    def test_dead_engine_falls_back_to_legacy(self):
        """A crawler that cannot start must not lose the item."""
        undo = _install_fake_crawlee()
        called = {}
        try:
            import crawlee.crawlers as fake

            def boom(**kwargs):
                raise RuntimeError("no engine here")

            fake.HttpCrawler = boom
            self.crawl.seeds_for = lambda *a, **kw: ["https://x.gov/"]

            def legacy(*args, **kwargs):
                called["legacy"] = True
                return [], ""

            real_legacy = self.crawl.crawl_item_legacy
            self.crawl.crawl_item_legacy = legacy
            try:
                self.crawl.crawl_item(
                    _StubConn(), None, {"use_crawlee": "1", "web_search": "0"},
                    1, "39049", "lodging", "Franklin County", "OH")
            finally:
                self.crawl.crawl_item_legacy = real_legacy
        finally:
            undo()
        self.assertTrue(called.get("legacy"))


class _StubConn:
    def execute(self, *args):
        return self

    def fetchone(self):
        return None


if __name__ == "__main__":
    unittest.main()
