"""One work item, start to finish, with the network stubbed out.

Covers the paths that only appear when the three passes run through the real
worker: packet selection, section acceptance from the model, the honest empty
answer, and the coverage assertion that keeps a blank from reading as a zero.
"""

import json

from taxdb import coverage, ingest, ledger, packets
from taxdb.vocab import ELECTIONS, FRAMEWORK
from tests._db import DbTest


class PacketTests(DbTest):
    def setUp(self):
        super().setUp()
        self.place(geoid="39049", name="Franklin County", kind="county", pop=1300000)

    def test_framework_packet_asks_for_thresholds_and_caps(self):
        text = packets.build(self.conn, "39", [FRAMEWORK])
        self.assertIn("State framework packet", text)
        self.assertIn("thresholds", text)
        self.assertIn("grants", text)
        self.assertIn("66.67", text, "the fraction trap must be spelled out")

    def test_elections_packet_asks_for_certified_results(self):
        text = packets.build(self.conn, "39049", [ELECTIONS])
        self.assertIn("Local revenue measure packet", text)
        self.assertIn("measures", text)
        self.assertIn("Certified or official results only", text)

    def test_elections_packet_says_an_empty_answer_is_allowed(self):
        text = packets.build(self.conn, "39049", [ELECTIONS])
        self.assertIn("empty `measures` array", text)

    def test_elections_packet_warns_when_no_thresholds_are_on_file(self):
        text = packets.build(self.conn, "39049", [ELECTIONS])
        self.assertIn("framework pass first", text)

    def test_elections_packet_lists_thresholds_once_they_exist(self):
        ingest.load_doc(self.conn, {"thresholds": [{
            "state_usps": "OH", "measure_class": "levy_override",
            "threshold_value": 60.0, "threshold_basis": "votes_cast",
            "statute_cite": "ORC 5705.19", "confidence": "high",
            "source": {"url": "https://codes.example.gov", "name": "Code",
                       "source_type": "statute", "authority_tier": 1}}]},
            label="t")
        text = packets.build(self.conn, "39049", [ELECTIONS])
        self.assertIn("levy_override", text)
        self.assertNotIn("framework pass first", text)

    def test_tax_packet_is_unaffected(self):
        text = packets.build(self.conn, "39049", ["sales_use"])
        self.assertIn("Research packet", text)
        self.assertIn("county_general_sales", text)

    def test_a_pass_category_never_leaks_into_the_tax_packet(self):
        ledger.plan(self.conn, states=["OH"], kinds=("county",),
                    categories=["sales_use", ELECTIONS])
        text = packets.build(self.conn, "39049")
        self.assertIn("Research packet", text)


class ExtractSectionTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import extract
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.extract = extract
        self._real_chat = extract.chat

    def tearDown(self):
        self.extract.chat = self._real_chat
        super().tearDown()

    def _run(self, payload):
        self.extract.chat = lambda *a, **k: (json.dumps(payload), None)
        return self.extract.extract({"provider": "openai"}, "packet", "docs")

    def test_a_thresholds_only_response_is_accepted(self):
        """The old code required a 'findings' array, so every threshold and
        every measure a model found was thrown away as malformed."""
        raw, doc, err = self._run({"thresholds": [{"state_usps": "OH"}]})
        self.assertIsNone(err)
        self.assertEqual(len(doc["thresholds"]), 1)

    def test_a_measures_only_response_is_accepted(self):
        raw, doc, err = self._run({"measures": []})
        self.assertIsNone(err)
        self.assertEqual(doc["measures"], [])

    def test_a_response_with_no_known_section_is_an_error(self):
        raw, doc, err = self._run({"summary": "I could not find anything"})
        self.assertIsNone(doc)
        self.assertIn("expected sections", err)

    def test_a_section_that_is_not_an_array_is_an_error(self):
        raw, doc, err = self._run({"measures": {"geoid": "39049"}})
        self.assertIsNone(doc)
        self.assertIn("must be an array", err)

    def test_a_single_element_profile_array_is_unwrapped(self):
        raw, doc, err = self._run({"profile": [{"state_usps": "OH"}]})
        self.assertIsNone(err)
        self.assertIsInstance(doc["profile"], dict)


class StampingTests(DbTest):
    def test_a_pass_name_is_never_stamped_as_a_tax_category(self):
        """'elections' is a work-queue pass, not a tax category. Stamping it
        onto a finding would reject the whole row."""
        doc = {"findings": [{"instrument_code": "county_general_sales"}]}
        ingest.stamp_doc(doc, geoid="39049", category=ELECTIONS)
        self.assertNotIn("category", doc["findings"][0])

    def test_a_tax_category_is_stamped(self):
        doc = {"findings": [{"instrument_code": "county_general_sales"}]}
        ingest.stamp_doc(doc, geoid="39049", category="sales_use")
        self.assertEqual(doc["findings"][0]["category"], "sales_use")

    def test_state_is_stamped_onto_thresholds_and_caps(self):
        doc = {"thresholds": [{"measure_class": "bond_go"}],
               "grants": [{"instrument_code": "use_tax"}],
               "profile": {}}
        ingest.stamp_doc(doc, state_usps="OH")
        self.assertEqual(doc["thresholds"][0]["state_usps"], "OH")
        self.assertEqual(doc["grants"][0]["state_usps"], "OH")
        self.assertEqual(doc["profile"]["state_usps"], "OH")

    def test_a_supplied_value_is_not_overwritten(self):
        doc = {"thresholds": [{"state_usps": "WA"}]}
        ingest.stamp_doc(doc, state_usps="OH")
        self.assertEqual(doc["thresholds"][0]["state_usps"], "WA")


class CoverageHonestyTests(DbTest):
    """A county with no measures found and a county nobody looked at must
    never read the same way."""

    def setUp(self):
        super().setUp()
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)

    def test_every_state_starts_at_completeness_none(self):
        coverage.seed_empty_states(self.conn)
        row = coverage.for_jurisdiction(self.conn, self.geoid)
        self.assertEqual(row["completeness"], "none")

    def test_a_searched_county_with_nothing_found_is_recorded_as_such(self):
        coverage.seed_empty_states(self.conn)
        coverage.assert_scope(
            self.conn, "ballot_measure", self.geoid, completeness="spot_checked",
            basis="searched the county elections office", measures_found=0)
        row = coverage.for_jurisdiction(self.conn, self.geoid)
        self.assertEqual(row["completeness"], "spot_checked")
        self.assertEqual(row["measures_found"], 0)
        self.assertEqual(row["scope_geoid"], self.geoid)

    def test_a_second_pass_updates_rather_than_duplicating(self):
        coverage.assert_scope(self.conn, "ballot_measure", self.geoid,
                              completeness="spot_checked", measures_found=0)
        coverage.assert_scope(self.conn, "ballot_measure", self.geoid,
                              completeness="partial", measures_found=9)
        rows = self.conn.execute(
            "SELECT completeness, measures_found FROM coverage_assertion "
            "WHERE scope_geoid=?", (self.geoid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["completeness"], "partial")
        self.assertEqual(rows[0]["measures_found"], 9)


class WorkerPassTests(DbTest):
    """_process, with crawling and the model stubbed out."""

    def setUp(self):
        super().setUp()
        try:
            from collector import check, crawl, extract, store, worker
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.worker, self.crawl, self.extract, self.check = worker, crawl, extract, check
        store.apply_schema(self.conn)
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)
        self.settings = dict(store.get_all(self.conn))
        self.settings.update({"provider": "openai", "checker_enabled": "0"})
        self.run_id = store.start_run(self.conn, "test")

        self._real_crawl_item = crawl.crawl_item
        self._real_extract = extract.extract
        crawl.crawl_item = lambda *a, **k: ([], "Issue 7 passed with 12,403 yes votes.")

    def tearDown(self):
        self.crawl.crawl_item = self._real_crawl_item
        self.extract.extract = self._real_extract
        super().tearDown()

    def _model_returns(self, payload):
        self.extract.extract = lambda *a, **k: ("raw", payload, None)

    def _process(self, category):
        ledger.plan(self.conn, states=["OH"], kinds=("county", "state"),
                    categories=[category])
        geoid = "39" if category == FRAMEWORK else self.geoid
        row = {"geoid": geoid, "category": category}
        return self.worker._process(self.conn, None, self.settings, self.run_id, row)

    def _status(self, geoid, category):
        return self.conn.execute(
            "SELECT status, last_error FROM work_item WHERE geoid=? AND category=?",
            (geoid, category)).fetchone()

    def test_measures_are_written_and_coverage_is_asserted(self):
        self._model_returns({"measures": [{
            "election_date": "2024-11-05", "measure_id_local": "Issue 7",
            "measure_class": "levy_override", "outcome": "passed",
            "votes_yes": 12403, "votes_no": 9887, "confidence": "high",
            "source": {"url": "https://vote.example.gov/canvass",
                       "name": "Canvass", "source_type": "agency_table",
                       "authority_tier": 2}}]})
        pages, written = self._process(ELECTIONS)
        self.assertEqual(written, 1)
        row = coverage.for_jurisdiction(self.conn, self.geoid)
        self.assertEqual(row["completeness"], "partial")
        self.assertEqual(row["measures_found"], 1)

    def test_no_measures_found_is_recorded_as_a_gap_not_a_failure(self):
        """An empty answer for a county that publishes nothing findable is a
        real answer. Leaving it at needs_review would pile up work forever."""
        self._model_returns({"measures": []})
        pages, written = self._process(ELECTIONS)
        self.assertEqual(written, 0)
        row = self._status(self.geoid, ELECTIONS)
        self.assertEqual(row["status"], "no_data")
        cov = coverage.for_jurisdiction(self.conn, self.geoid)
        self.assertEqual(cov["completeness"], "spot_checked")
        self.assertEqual(cov["measures_found"], 0)

    def test_framework_rows_are_stamped_with_the_state(self):
        self._model_returns({"thresholds": [{
            "measure_class": "bond_go", "threshold_value": 60.0,
            "threshold_basis": "votes_cast", "statute_cite": "ORC 133.18",
            "confidence": "high",
            "source": {"url": "https://codes.example.gov", "name": "Code",
                       "source_type": "statute", "authority_tier": 1}}]})
        pages, written = self._process(FRAMEWORK)
        self.assertEqual(written, 1)
        row = self.conn.execute(
            "SELECT state_usps FROM threshold_rule").fetchone()
        self.assertEqual(row["state_usps"], "OH")

    def test_a_rejected_extraction_leaves_the_reason_on_the_item(self):
        self._model_returns({"thresholds": [{
            "measure_class": "bond_go", "threshold_value": 0.6,
            "threshold_basis": "votes_cast", "statute_cite": "ORC 133.18",
            "confidence": "high",
            "source": {"url": "https://codes.example.gov", "name": "Code",
                       "source_type": "statute", "authority_tier": 1}}]})
        pages, written = self._process(FRAMEWORK)
        self.assertEqual(written, 0)
        row = self._status("39", FRAMEWORK)
        self.assertEqual(row["status"], "needs_review")
        self.assertIn("rejected", row["last_error"])

    def test_a_blocked_search_is_written_onto_the_item(self):
        def crawl_with_blocked_search(conn, client, s, run_id, geoid, category,
                                      name, state, diag=None):
            if diag is not None:
                diag.update({"queries": 5, "answered": 0, "blocked": 5,
                             "hits": 0, "provider": "scrape"})
            return [], ""
        self.crawl.crawl_item = crawl_with_blocked_search
        self.settings["provider"] = "none"
        pages, written = self._process("sales_use")
        row = self._status(self.geoid, "sales_use")
        self.assertIn("blocked", row["last_error"])
