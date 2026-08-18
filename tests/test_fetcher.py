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


class _FakeCrawler:
    """Drives handlers over `pages`, a url -> (status, ctype, blob) map."""

    pages = {}
    robots_blocked = set()
    broken = set()
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


def _install_fake_crawlee():
    """Put a minimal fake `crawlee` in sys.modules; return an undo callable."""
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] == "crawlee"}

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

    for name, mod in (("crawlee", root), ("crawlee.configuration", config),
                      ("crawlee.crawlers", crawlers),
                      ("crawlee.http_clients", clients)):
        sys.modules[name] = mod

    def undo():
        for name in list(sys.modules):
            if name.split(".")[0] == "crawlee":
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
        _FakeCrawler.started = []
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

    def test_engine_options_carry_the_policy(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html", COUNTY_HOME),
        }
        self.seed()
        self.crawl_item(self.settings(
            max_pages_per_item="9", max_retries="5", concurrency="3",
            delay_seconds="2.0"))
        opts = _FakeCrawler.started[0]
        self.assertEqual(opts["max_requests_per_crawl"], 9)
        self.assertEqual(opts["max_request_retries"], 5)
        self.assertTrue(opts["respect_robots_txt_file"])
        self.assertEqual(opts["concurrency_settings"]["max_concurrency"], 3)
        self.assertAlmostEqual(
            opts["concurrency_settings"]["max_tasks_per_minute"], 30.0)

    def test_second_search_round_when_first_finds_nothing(self):
        _FakeCrawler.pages = {
            "https://franklincountyohio.gov/": (200, "text/html",
                                                b"<html><body>Hello</body></html>"),
            "https://co.franklin.oh.us/lodging.pdf": (200, "application/pdf",
                                                      MINIMAL_PDF),
        }
        self.seed()
        calls = []

        def fake_search(client, name, state, category, kind=None, limit=12):
            calls.append(limit)
            return ["https://co.franklin.oh.us/lodging.pdf"] if calls else []

        self.crawl.search_web = fake_search
        pages, _ = self.crawl_item(self.settings(web_search="1"))
        urls = [p["url"] for p in pages]
        self.assertIn("https://co.franklin.oh.us/lodging.pdf", urls)


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
