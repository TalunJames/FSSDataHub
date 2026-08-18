"""Search reliability, and the second checker on the new research passes."""

import unittest

from taxdb import ingest
from taxdb.vocab import ELECTIONS, FRAMEWORK
from tests._db import DbTest

STATUTE = {"url": "https://codes.example.gov/orc", "name": "Code",
           "source_type": "statute", "authority_tier": 1}
AGGREGATOR = {"url": "https://taxrates.example.com/oh", "name": "Vendor table",
              "source_type": "secondary", "authority_tier": 4}


class SearchQueryTests(unittest.TestCase):
    def setUp(self):
        try:
            from collector import crawl
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.crawl = crawl

    def test_framework_queries_look_for_law_not_rate_tables(self):
        qs = " ".join(self.crawl.search_queries("Ohio", "OH", FRAMEWORK, "state"))
        self.assertIn("statute", qs)
        self.assertIn("two-thirds", qs)

    def test_elections_queries_look_for_canvasses(self):
        qs = " ".join(self.crawl.search_queries(
            "Franklin County", "OH", ELECTIONS, "county"))
        self.assertIn("canvass", qs)
        self.assertIn("official results", qs)

    def test_tax_queries_are_unchanged(self):
        qs = " ".join(self.crawl.search_queries(
            "Franklin County", "OH", "sales_use", "county"))
        self.assertIn("sales use tax", qs)

    def test_election_pages_are_followed_on_the_elections_pass(self):
        url = "https://vote.example.gov/2024/abstract-of-votes.htm"
        self.assertTrue(self.crawl.should_follow(url, "Abstract of votes", 2,
                                                 category=ELECTIONS))

    def test_blocked_search_is_reported_not_silently_empty(self):
        """An engine answering with a challenge page used to be
        indistinguishable from a genuine zero-result search."""
        note = self.crawl.search_note({
            "queries": 5, "answered": 0, "blocked": 5, "hits": 0,
            "kept": 0, "provider": "scrape"})
        self.assertIn("blocked", note)

    def test_genuine_zero_result_search_is_described_differently(self):
        note = self.crawl.search_note({
            "queries": 5, "answered": 5, "blocked": 0, "hits": 0,
            "kept": 0, "provider": "brave"})
        self.assertIsNotNone(note)
        self.assertNotIn("blocked", note)

    def test_successful_search_produces_no_note(self):
        self.assertIsNone(self.crawl.search_note({
            "queries": 5, "answered": 5, "blocked": 0, "hits": 12,
            "kept": 6, "provider": "brave"}))

    def test_no_search_attempted_produces_no_note(self):
        self.assertIsNone(self.crawl.search_note(self.crawl.new_diag()))


class CheckerSubjectTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import check, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.check = check
        store.apply_schema(self.conn)
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)

    def test_checker_reads_measures_for_the_elections_pass(self):
        ingest.load_doc(self.conn, {"measures": [{
            "geoid": self.geoid, "election_date": "2024-11-05",
            "measure_id_local": "Issue 7", "measure_class": "levy_override",
            "outcome": "failed", "votes_yes": 100, "votes_no": 120,
            "confidence": "high", "source": STATUTE}]}, label="m")
        rows, flag_fn, _, noun = self.check.subject(self.conn, self.geoid, ELECTIONS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(noun, "measure")

    def test_checker_reads_state_rules_for_the_framework_pass(self):
        ingest.load_doc(self.conn, {"thresholds": [{
            "state_usps": "OH", "measure_class": "bond_go",
            "threshold_value": 60.0, "threshold_basis": "votes_cast",
            "statute_cite": "ORC 133.18", "confidence": "high",
            "source": STATUTE}]}, label="t")
        rows, _, _, noun = self.check.subject(self.conn, "39", FRAMEWORK)
        self.assertEqual(len(rows), 1)
        self.assertEqual(noun, "framework rule")

    def test_measure_with_no_threshold_is_flagged(self):
        """Without a threshold there is no margin, and the margin is the whole
        point of keeping measure history."""
        ingest.load_doc(self.conn, {"measures": [{
            "geoid": self.geoid, "election_date": "2024-11-05",
            "measure_id_local": "Issue 7", "measure_class": "levy_override",
            "outcome": "failed", "votes_yes": 100, "votes_no": 120,
            "confidence": "high", "source": STATUTE}]}, label="m")
        rows = self.check.live_measures(self.conn, self.geoid)
        codes = [f["code"] for f in self.check.measure_flags(rows, "Issue 7 failed")]
        self.assertIn("no_threshold", codes)

    def test_measure_sourced_to_an_aggregator_is_flagged(self):
        ingest.load_doc(self.conn, {"measures": [{
            "geoid": self.geoid, "election_date": "2024-11-05",
            "measure_id_local": "Issue 7", "measure_class": "levy_override",
            "outcome": "failed", "votes_yes": 100, "votes_no": 120,
            "confidence": "high", "source": AGGREGATOR}]}, label="m")
        rows = self.check.live_measures(self.conn, self.geoid)
        codes = [f["code"] for f in self.check.measure_flags(rows, "Issue 7")]
        self.assertIn("weak_source", codes)

    def test_measure_id_absent_from_the_documents_is_flagged(self):
        ingest.load_doc(self.conn, {"measures": [{
            "geoid": self.geoid, "election_date": "2024-11-05",
            "measure_id_local": "Issue 7", "measure_class": "levy_override",
            "outcome": "failed", "votes_yes": 100, "votes_no": 120,
            "confidence": "high", "source": STATUTE}]}, label="m")
        rows = self.check.live_measures(self.conn, self.geoid)
        codes = [f["code"] for f in self.check.measure_flags(
            rows, "an unrelated page about parking")]
        self.assertIn("id_missing", codes)

    def test_cap_with_no_ceiling_is_flagged_for_a_look(self):
        ingest.load_doc(self.conn, {"grants": [{
            "state_usps": "OH", "jurisdiction_kind": "county",
            "category": "sales_use", "instrument_code": "county_general_sales",
            "permitted": "yes", "statute_cite": "ORC 5739.021",
            "confidence": "high", "source": STATUTE}]}, label="g")
        rows = self.check.live_framework(self.conn, "OH")
        codes = [f["code"] for f in self.check.framework_flags(rows, "ORC 5739.021")]
        self.assertIn("no_cap", codes)

    def test_cite_missing_from_the_documents_is_flagged(self):
        ingest.load_doc(self.conn, {"thresholds": [{
            "state_usps": "OH", "measure_class": "bond_go",
            "threshold_value": 60.0, "threshold_basis": "votes_cast",
            "statute_cite": "ORC 133.18", "confidence": "high",
            "source": STATUTE}]}, label="t")
        rows = self.check.live_framework(self.conn, "OH")
        codes = [f["code"] for f in self.check.framework_flags(
            rows, "a page that never mentions that section")]
        self.assertIn("cite_missing", codes)

    def test_well_sourced_threshold_passes_the_deterministic_checks(self):
        ingest.load_doc(self.conn, {"thresholds": [{
            "state_usps": "OH", "measure_class": "bond_go",
            "threshold_value": 60.0, "threshold_basis": "votes_cast",
            "statute_cite": "ORC 133.18", "confidence": "high",
            "source": STATUTE}]}, label="t")
        rows = self.check.live_framework(self.conn, "OH")
        flags = self.check.framework_flags(
            rows, "Under ORC 133.18 a bond issue requires sixty percent.")
        self.assertEqual(flags, [])
