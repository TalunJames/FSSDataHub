"""Collector: settings, JSON extract, crawl policy."""

import datetime
import unittest

from tests._db import DbTest


class ExtractParseTests(unittest.TestCase):
    def setUp(self):
        try:
            from collector.extract import parse_json_payload, ExtractError
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.parse = parse_json_payload
        self.ExtractError = ExtractError

    def test_bare_object(self):
        doc = self.parse('{"findings":[]}')
        self.assertEqual(doc["findings"], [])

    def test_fenced_block(self):
        doc = self.parse("Sure.\n```json\n{\"findings\":[{\"geoid\":\"1\"}]}\n```\n")
        self.assertEqual(doc["findings"][0]["geoid"], "1")

    def test_preamble(self):
        doc = self.parse("Here you go:\n{\"schema_version\":\"1.0\",\"findings\":[]}\nThanks")
        self.assertEqual(doc["schema_version"], "1.0")

    def test_garbage(self):
        with self.assertRaises(self.ExtractError):
            self.parse("no json here")

    def test_think_block_stripped(self):
        # Reasoning models (Qwen3 on Ollama) prepend a monologue whose braces
        # would otherwise poison the first-{-to-last-} extraction.
        doc = self.parse('<think>Hmm, {"draft": 1}? No.</think>\n{"verdicts":[]}')
        self.assertEqual(doc["verdicts"], [])

    def test_unterminated_think_is_an_error(self):
        # A think block the output cap cut off has no answer to parse.
        with self.assertRaises(self.ExtractError):
            self.parse('<think>Considering {"verdicts": []} but also')


class CrawlPolicyTests(unittest.TestCase):
    def setUp(self):
        try:
            from collector import crawl
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.crawl = crawl

    def test_gov_hosts_allowed(self):
        seeds = {"tax.ohio.gov"}
        self.assertTrue(self.crawl.allowed_host("tax.ohio.gov", seeds))
        self.assertTrue(self.crawl.allowed_host("dor.wa.gov", seeds))
        self.assertTrue(self.crawl.allowed_host("revenue.ny.gov", seeds))

    def test_social_blocked(self):
        self.assertFalse(self.crawl.allowed_host("facebook.com", {"tax.ohio.gov"}))

    def test_seed_subdomain(self):
        self.assertTrue(self.crawl.allowed_host("www.tax.ohio.gov", {"tax.ohio.gov"}))

    def test_relevance(self):
        self.assertTrue(self.crawl.looks_relevant("https://dor.wa.gov/taxes-rates/local-sales-tax"))
        self.assertFalse(self.crawl.looks_relevant("https://dor.wa.gov/logo.png"))

    def test_kind_of_pdf_and_image(self):
        from collector import intake
        self.assertEqual(intake.kind_of("application/pdf", "x.pdf"), "pdf")
        self.assertEqual(intake.kind_of("image/png", "shot.png"), "image")
        self.assertIsNone(intake.kind_of("text/plain", "notes.txt"))

    def test_ddg_unwrap(self):
        href = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Ftax.ohio.gov%2Frates"
        self.assertEqual(self.crawl._unwrap_ddg(href), "https://tax.ohio.gov/rates")

    def test_html_strips_chrome(self):
        html = b"""<html><head><title>Rates</title></head>
        <body><nav>Home About</nav><header>Banner</header>
        <p>County sales tax is 1.5 percent.</p>
        <footer>Copyright</footer></body></html>"""
        text, title = self.crawl._html_text(html)
        self.assertEqual(title, "Rates")
        self.assertIn("1.5 percent", text)
        self.assertNotIn("Copyright", text)
        self.assertNotIn("Banner", text)

    def test_pdf_and_dept_links_are_followed(self):
        self.assertTrue(self.crawl.is_document_url("https://co.franklin.oh.us/tax/rates.pdf"))
        self.assertTrue(self.crawl.looks_relevant("https://co.franklin.oh.us/docs/rates.pdf"))
        self.assertTrue(self.crawl.should_follow(
            "https://franklincountyohio.gov/treasurer", "Treasurer", 0))
        self.assertFalse(self.crawl.should_follow(
            "https://franklincountyohio.gov/parks", "Parks and rec", 1))

    def test_jurisdiction_org_host_allowed(self):
        seeds = {"tax.ohio.gov"}
        self.assertTrue(self.crawl.allowed_host(
            "franklincountyohio.org", seeds, name="Franklin County", state="OH"))
        self.assertTrue(self.crawl.allowed_host(
            "co.franklin.oh.us", seeds, name="Franklin County", state="OH"))
        self.assertFalse(self.crawl.allowed_host(
            "franklincountynews.com", seeds, name="Franklin County", state="OH"))
        self.assertFalse(self.crawl.allowed_host(
            "random-advice.org", seeds, name="Franklin County", state="OH"))

    def test_search_queries_include_site_and_pdf(self):
        qs = " ".join(self.crawl.search_queries(
            "Franklin County", "OH", "lodging", kind="county"))
        self.assertIn("official website", qs)
        self.assertIn("filetype:pdf", qs)
        self.assertIn("site:.gov", qs)

    def test_seed_hosts_lowercased(self):
        hosts = self.crawl.seed_hosts_for([
            "https://TAX.Ohio.GOV/rates", "https://co.franklin.oh.us/x", "notaurl"])
        self.assertEqual(hosts, {"tax.ohio.gov", "co.franklin.oh.us"})


class PageRecordTests(unittest.TestCase):
    """The page shape both fetch engines must produce identically."""

    def setUp(self):
        try:
            from collector import crawl
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.crawl = crawl

    def test_html_page_record(self):
        blob = b"<html><head><title>Lodging Tax</title></head><body>" \
               b"<p>The transient occupancy tax is 3 percent.</p></body></html>"
        page = self.crawl.page_record(
            "https://co.franklin.oh.us/tax", "https://co.franklin.oh.us/tax",
            200, "text/html; charset=utf-8", blob)
        self.assertEqual(page["title"], "Lodging Tax")
        self.assertIn("3 percent", page["text"])
        self.assertIsNone(page["error"])
        self.assertEqual(page["robots_allowed"], 1)
        self.assertEqual(len(page["sha256"]), 64)

    def test_http_error_status_becomes_error(self):
        page = self.crawl.page_record(
            "https://x.gov/a", "https://x.gov/a", 404, "text/html", b"gone")
        self.assertEqual(page["error"], "HTTP 404")

    def test_error_page_has_same_keys(self):
        good = self.crawl.page_record(
            "https://x.gov/a", "https://x.gov/a", 200, "text/html", b"<p>hi</p>")
        bad = self.crawl.error_page("https://x.gov/a", "timed out")
        self.assertEqual(set(good), set(bad))
        self.assertEqual(bad["blob"], b"")
        self.assertEqual(bad["error"], "timed out")

    def test_robots_page_flagged_not_allowed(self):
        page = self.crawl.error_page(
            "https://x.gov/a", "robots.txt disallows this URL",
            robots_allowed_flag=0)
        self.assertEqual(page["robots_allowed"], 0)

    def test_follow_targets_marks_documents_preferred(self):
        blob = b"""<html><body>
        <a href="/docs/rates.pdf">Rate table</a>
        <a href="/treasurer">Treasurer</a>
        <a href="/parks">Parks</a>
        <a href="https://facebook.com/county">Facebook</a>
        </body></html>"""
        targets = self.crawl.follow_targets(
            "https://co.franklin.oh.us/", "https://co.franklin.oh.us/", blob, 0,
            {"co.franklin.oh.us"}, name="Franklin County", state="OH")
        found = dict(targets)
        self.assertTrue(found["https://co.franklin.oh.us/docs/rates.pdf"])
        self.assertIn("https://co.franklin.oh.us/treasurer", found)
        self.assertNotIn("https://co.franklin.oh.us/parks", found)
        self.assertFalse(any("facebook" in u for u in found))

    def test_follow_targets_skips_offsite(self):
        blob = b'<html><body><a href="https://vendor-cms.com/tax">Tax</a></body></html>'
        targets = self.crawl.follow_targets(
            "https://co.franklin.oh.us/", "https://co.franklin.oh.us/", blob, 0,
            {"co.franklin.oh.us"}, name="Franklin County", state="OH")
        self.assertEqual(targets, [])


class EngineSelectionTests(unittest.TestCase):
    def setUp(self):
        try:
            from collector import crawl, fetcher
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.crawl = crawl
        self.fetcher = fetcher

    def test_setting_off_disables_crawlee(self):
        self.assertFalse(self.crawl.crawlee_enabled({"use_crawlee": "0"}))

    def test_setting_on_follows_import(self):
        self.assertEqual(
            self.crawl.crawlee_enabled({"use_crawlee": "1"}),
            self.fetcher.available())

    def test_delay_becomes_rate_cap(self):
        # Per-item rate, so the pool size is part of the sum: the ceiling the
        # setting describes is global. See test_request_ceiling_is_global.
        one = {"delay_seconds": "2.0", "workers": "1"}
        self.assertAlmostEqual(self.fetcher._tasks_per_minute(one), 30.0)
        self.assertAlmostEqual(self.fetcher._tasks_per_minute(
            {"delay_seconds": "0.5", "workers": "1"}), 120.0)
        self.assertEqual(self.fetcher._tasks_per_minute({"delay_seconds": "0"}),
                         float("inf"))
        self.assertAlmostEqual(self.fetcher._tasks_per_minute(
            {"delay_seconds": "2.0", "workers": "6"}) * 6, 30.0)

    def test_pool_size_has_one_definition(self):
        """The pool and the rate divisor must not drift apart."""
        from collector import store, worker
        for n in ("1", "4", "9"):
            self.assertEqual(worker.worker_count({"workers": n}),
                             store.worker_count({"workers": n}))
        self.assertEqual(store.worker_count({}),
                         int(store.DEFAULTS["workers"]))
        self.assertEqual(store.worker_count({"workers": "999"}), 32)
        self.assertEqual(store.worker_count({"workers": "0"}), 1)

    def test_thin_html_marked_for_render(self):
        plan = {"render_min": 400}
        thin = {"blob": b"<html></html>", "content_type": "text/html",
                "text": "Home About Contact", "error": None}
        self.assertTrue(self.fetcher._is_thin(thin, plan))
        fat = dict(thin, text="x" * 500)
        self.assertFalse(self.fetcher._is_thin(fat, plan))

    def test_pdf_and_rendered_pages_never_re_rendered(self):
        plan = {"render_min": 400}
        pdf = {"blob": b"%PDF", "content_type": "application/pdf", "text": "",
               "error": None}
        self.assertFalse(self.fetcher._is_thin(pdf, plan))
        already = {"blob": b"<html></html>", "content_type": "text/html",
                   "text": "", "error": None, "rendered": True}
        self.assertFalse(self.fetcher._is_thin(already, plan))
        failed = {"blob": b"", "content_type": "text/html", "text": "",
                  "error": "timed out"}
        self.assertFalse(self.fetcher._is_thin(failed, plan))


class StoreTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.store = store
        store.apply_schema(self.conn)

    def test_defaults_and_put(self):
        s = self.store.get_all(self.conn)
        self.assertEqual(s["provider"], "none")
        self.store.put_many(self.conn, {"provider": "llama", "llama_model": "llama3.1"})
        s = self.store.get_all(self.conn)
        self.assertEqual(s["provider"], "llama")
        self.assertEqual(s["llama_model"], "llama3.1")

    def test_mask_secrets(self):
        self.store.put(self.conn, "openai_api_key", "sk-abcdefghijklmnop")
        self.conn.commit()
        masked = self.store.mask(self.store.get_all(self.conn))
        self.assertTrue(masked["openai_api_key"].endswith("mnop"))
        self.assertIn("*", masked["openai_api_key"])
        self.assertTrue(masked["openai_api_key_set"])

    def test_run_lifecycle(self):
        rid = self.store.start_run(self.conn, "burst", provider="none")
        self.store.bump_run(self.conn, rid, items=1, pages=3, findings=2)
        self.store.finish_run(self.conn, rid, "ok", "done", items=1, pages=3, findings=2)
        row = self.conn.execute("SELECT * FROM crawl_run WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["pages_fetched"], 3)

    def test_schema_tables_exist(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("collector_setting", names)
        self.assertIn("crawl_run", names)
        self.assertIn("crawl_page", names)
        self.assertIn("crawl_extract", names)
        self.assertIn("intake_item", names)
        self.assertIn("interview_answer", names)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        try:
            from collector import worker, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.worker = worker
        self.store = store

    def test_hourly_due_when_never_run(self):
        s = {"schedule_enabled": "1", "schedule_kind": "hourly", "last_scheduled_at": ""}
        self.assertTrue(self.worker._schedule_due(s))

    def test_hourly_not_due_immediately(self):
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S")
        s = {"schedule_enabled": "1", "schedule_kind": "hourly", "last_scheduled_at": now}
        self.assertFalse(self.worker._schedule_due(s))

    def test_disabled(self):
        s = {"schedule_enabled": "0", "schedule_kind": "hourly", "last_scheduled_at": ""}
        self.assertFalse(self.worker._schedule_due(s))


class InterviewTests(DbTest):
    def setUp(self):
        super().setUp()
        from collector import interview, store
        self.interview = interview
        store.apply_schema(self.conn)
        self.geoid = self.place()

    def test_place_sales_asks_municipal_not_county(self):
        codes = self.interview.primary_codes("place", "sales_use")
        self.assertIn("municipal_general_sales", codes)
        self.assertNotIn("county_general_sales", codes)

    def test_session_asks_gaps_only(self):
        sess = self.interview.session(self.conn, self.geoid, "sales_use")
        self.assertIsNotNone(sess["question"])
        self.assertEqual(sess["question"]["instrument_code"], "municipal_general_sales")
        self.assertIn("Nothing", sess["question"]["why"])

    def test_skip_advances(self):
        sess = self.interview.session(self.conn, self.geoid, "sales_use")
        first = sess["question"]["id"]
        self.interview.apply_answer(
            self.conn, self.geoid, "sales_use", sess["question"], "skipped", {})
        sess2 = self.interview.session(self.conn, self.geoid, "sales_use")
        self.assertNotEqual(sess2["question"]["id"], first)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM tax_instrument WHERE geoid=?", (self.geoid,)).fetchone()["c"]
        self.assertEqual(n, 0)

    def test_dont_know_writes_unknown(self):
        sess = self.interview.session(self.conn, self.geoid, "sales_use")
        res = self.interview.apply_answer(
            self.conn, self.geoid, "sales_use", sess["question"], "unknown",
            {"notes": "could not find it"})
        self.assertEqual(res["written"], 1)
        row = self.conn.execute(
            "SELECT status, notes FROM tax_instrument WHERE geoid=? AND superseded_by IS NULL",
            (self.geoid,)).fetchone()
        self.assertEqual(row["status"], "unknown")

    def test_complete_row_not_asked(self):
        sess = self.interview.session(self.conn, self.geoid, "sales_use")
        q = sess["question"]
        res = self.interview.apply_answer(
            self.conn, self.geoid, "sales_use", q, "answered",
            {"status": "levied", "rate_value": 1.5, "rate_unit": "percent",
             "source_url": "https://example.test/rate"})
        self.assertEqual(res["written"], 1)
        sess2 = self.interview.session(self.conn, self.geoid, "sales_use")
        if sess2["question"]:
            self.assertNotEqual(sess2["question"]["instrument_code"], q["instrument_code"])

    def test_skip_rest_clears_queue(self):
        sess = self.interview.session(self.conn, self.geoid, "sales_use")
        self.interview.apply_answer(
            self.conn, self.geoid, "sales_use", sess["question"], "skip_rest", {})
        sess2 = self.interview.session(self.conn, self.geoid, "sales_use")
        self.assertIsNone(sess2["question"])
        self.assertEqual(sess2["remaining"], 0)


class AnthropicTuningTests(unittest.TestCase):
    """Thinking and effort are model-specific; the wrong pair is a 400."""

    def setUp(self):
        try:
            from collector import extract
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.extract = extract

    def test_five_generation_gets_adaptive_thinking_and_effort(self):
        body = self.extract.anthropic_tuning("claude-sonnet-5")
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertIn("effort", body["output_config"])

    def test_four_five_generation_gets_neither(self):
        # Adaptive thinking and effort are rejected outright by these models.
        self.assertEqual(self.extract.anthropic_tuning("claude-haiku-4-5"), {})

    def test_unknown_model_falls_back_to_what_everything_accepts(self):
        # The model field is free text. A typo must not 400 every item.
        self.assertEqual(self.extract.anthropic_tuning("claude-sonnet-9000"), {})

    def test_thinking_is_always_explicit_for_capable_models(self):
        """Omitting it means adaptive on the 5 generation, and max_tokens caps
        thinking and the answer together — a silent truncation risk."""
        for model in ("claude-sonnet-5", "claude-opus-5"):
            self.assertIn("thinking", self.extract.anthropic_tuning(model))

    def test_checker_thinks_harder_than_the_extractor(self):
        levels = ["low", "medium", "high", "xhigh", "max"]
        self.assertGreater(levels.index(self.extract.DEFAULT_CHECKER_EFFORT),
                           levels.index(self.extract.DEFAULT_EFFORT))

    def test_truncated_response_reports_the_cap_not_bad_json(self):
        """A response cut off at max_tokens must say so. Reported as a JSON
        parse failure, the log reads 'the model wrote garbage' and nobody
        raises the cap."""
        from unittest import mock

        class _Resp:
            status_code = 200
            def json(self):
                return {"stop_reason": "max_tokens",
                        "content": [{"type": "text", "text": '{"findings": ['}]}

        class _Client:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def post(self, *a, **k):
                return _Resp()

        with mock.patch.object(self.extract.httpx, "Client",
                               return_value=_Client()):
            with self.assertRaises(self.extract.ExtractError) as ctx:
                self.extract._anthropic("key", "claude-sonnet-5", "prompt")
        self.assertIn("truncated", str(ctx.exception))

    def test_stale_prompt_caching_beta_header_is_gone(self):
        import inspect
        src = inspect.getsource(self.extract._anthropic)
        self.assertNotIn("prompt-caching-2024-07-31", src)

    def test_extractor_asks_for_tier_and_corroboration(self):
        self.assertIn("authority_tier", self.extract.SYSTEM)
        self.assertIn("corroborating_sources", self.extract.SYSTEM)


class KeywordFilterTests(unittest.TestCase):
    """Only keyword-matching links are fetched; only keyword-matching
    content is stored and read."""

    def setUp(self):
        try:
            from collector import crawl
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.crawl = crawl
        self.crawl.configure_keywords({})

    def tearDown(self):
        self.crawl.configure_keywords({})

    def test_relevant_source_page_does_not_fan_out(self):
        # A page whose own URL matches a keyword must not drag every nav
        # link on it into the queue; each link earns its place.
        blob = b"""<html><body>
        <a href="/parks">Parks and rec</a>
        <a href="/calendar">Calendar</a>
        <a href="/lodging-tax">Lodging tax</a>
        </body></html>"""
        targets = self.crawl.follow_targets(
            "https://co.franklin.oh.us/taxes", "https://co.franklin.oh.us/taxes",
            blob, 1, {"co.franklin.oh.us"}, name="Franklin County", state="OH")
        urls = [u for u, _ in targets]
        self.assertEqual(urls, ["https://co.franklin.oh.us/lodging-tax"])

    def test_extra_keywords_widen_the_follow_test(self):
        url = "https://co.franklin.oh.us/stormwater-fee"
        self.assertFalse(self.crawl.looks_relevant(url))
        self.crawl.configure_keywords({"crawl_keywords": "stormwater, impact fee"})
        self.assertTrue(self.crawl.looks_relevant(url))

    def test_content_relevant_keeps_tax_text(self):
        page = {"url": "https://x.gov/a", "final_url": "https://x.gov/a",
                "content_type": "text/html",
                "text": "The county lodging tax is 3 percent."}
        self.assertTrue(self.crawl.content_relevant(page))

    def test_content_relevant_drops_offtopic_text(self):
        page = {"url": "https://x.gov/parks", "final_url": "https://x.gov/parks",
                "content_type": "text/html",
                "text": "The pool opens Memorial Day weekend."}
        self.assertFalse(self.crawl.content_relevant(page))

    def test_content_relevant_always_keeps_documents(self):
        page = {"url": "https://x.gov/rates.pdf", "final_url": "https://x.gov/rates.pdf",
                "content_type": "application/pdf", "text": ""}
        self.assertTrue(self.crawl.content_relevant(page))

    def test_content_relevant_election_text_needs_category(self):
        page = {"url": "https://x.gov/results", "final_url": "https://x.gov/results",
                "content_type": "text/html",
                "text": "Precinct summary for the bond issue."}
        self.assertFalse(self.crawl.content_relevant(page))
        self.assertTrue(self.crawl.content_relevant(page, category="elections"))

    def test_extra_keywords_count_as_content(self):
        page = {"url": "https://x.gov/fees", "final_url": "https://x.gov/fees",
                "content_type": "text/html",
                "text": "The stormwater fee schedule for 2026."}
        self.assertFalse(self.crawl.content_relevant(page))
        self.crawl.configure_keywords({"crawl_keywords": "stormwater"})
        self.assertTrue(self.crawl.content_relevant(page))

    def test_content_filter_defaults_on(self):
        self.assertTrue(self.crawl.content_filter_on({}))
        self.assertFalse(self.crawl.content_filter_on({"content_filter": "0"}))

    def test_default_keywords_cover_revenue_measure_terms(self):
        from collector.settings import DEFAULTS
        self.crawl.configure_keywords(DEFAULTS)
        self.assertTrue(self.crawl.looks_relevant(
            "https://co.franklin.oh.us/2026-bond-program"))
        self.assertTrue(self.crawl.looks_relevant(
            "https://co.franklin.oh.us/gross-receipts"))
        self.assertTrue(self.crawl.content_relevant({
            "url": "https://x.gov/a", "final_url": "https://x.gov/a",
            "content_type": "text/html",
            "text": "The district's sinking fund renewal."}))
        # Still off for pages none of the words describe.
        self.assertFalse(self.crawl.looks_relevant(
            "https://co.franklin.oh.us/parks"))

    def test_phrase_keywords_match_url_spellings(self):
        self.crawl.configure_keywords({"crawl_keywords": "mill rate"})
        for url in ("https://x.gov/mill-rate", "https://x.gov/mill_rate.pdf",
                    "https://x.gov/millrate"):
            self.assertTrue(self.crawl.looks_relevant(url), url)
        self.assertFalse(self.crawl.looks_relevant("https://x.gov/treadmill"))
