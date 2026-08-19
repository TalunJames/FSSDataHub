"""Search memory between rounds, and the checker's review recommendations."""

import json
import unittest
from unittest import mock

from taxdb import db

from tests._db import DbTest


class SearchLogTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import crawl, searchlog, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.crawl = crawl
        self.searchlog = searchlog
        self.store = store
        store.apply_schema(self.conn)
        self.geoid = self.place()
        self.category = "sales_use"
        self.conn.execute(
            "INSERT INTO work_item (geoid, category, priority, status, updated_at) "
            "VALUES (?,?,?,?,?)",
            (self.geoid, self.category, 10, "pending", db.now()))
        self.conn.commit()
        self.built_in = ["query one", "query two", "query three"]

    def _settings(self, **kw):
        s = self.store.get_all(self.conn)
        s.update(kw)
        return s

    def _record(self, outcomes):
        """outcomes: {query: (kept, blocked)}"""
        diag = {"by_query": {
            q: {"kept": kept, "blocked": blocked}
            for q, (kept, blocked) in outcomes.items()}}
        self.searchlog.record_round(self.conn, self.geoid, self.category, diag)
        return diag

    def _plan(self, exclude=None, allow_reflect=False, **kw):
        return self.searchlog.plan_round(
            self.conn, self._settings(**kw), self.geoid, self.category,
            self.built_in, exclude=exclude, allow_reflect=allow_reflect)

    def test_first_round_runs_the_built_ins(self):
        self.assertEqual(self._plan(), self.built_in)

    def test_wording_that_found_nothing_is_not_repeated(self):
        self._record({"query one": (0, False), "query two": (3, False)})
        plan = self._plan()
        self.assertNotIn("query one", plan)
        self.assertIn("query two", plan)
        self.assertIn("query three", plan)

    def test_blocked_wording_is_retried(self):
        """Every engine refusing says nothing about the wording."""
        self._record({"query one": (0, True)})
        self.assertIn("query one", self._plan())

    def test_all_dead_falls_back_to_the_built_ins(self):
        """Websites change; a stale 'nothing' beats not searching at all."""
        self._record({q: (0, False) for q in self.built_in})
        self.assertEqual(self._plan(), self.built_in)

    def test_mid_item_retry_never_falls_back(self):
        """With everything already tried this item, the retry is skipped."""
        self._record({q: (0, False) for q in self.built_in})
        self.assertEqual(self._plan(exclude=set(self.built_in)), [])

    def test_record_round_clears_diag_and_counts_tries(self):
        diag = self._record({"query one": (0, False)})
        self.assertEqual(diag["by_query"], {})
        self._record({"query one": (2, False)})
        row = self.conn.execute(
            "SELECT tries, last_outcome, last_kept FROM search_query "
            "WHERE query='query one'").fetchone()
        self.assertEqual(row["tries"], 2)
        self.assertEqual(row["last_outcome"], "found")
        self.assertEqual(row["last_kept"], 2)

    def test_ai_wording_runs_first(self):
        self._record({"query one": (0, False)})
        self.conn.execute(
            "INSERT INTO search_query (geoid, category, query, source, created_at) "
            "VALUES (?,?,?,'ai',?)",
            (self.geoid, self.category, "franklin county fiscal officer rates",
             db.now()))
        self.conn.commit()
        plan = self._plan()
        self.assertEqual(plan[0], "franklin county fiscal officer rates")

    def test_reflection_proposes_new_wording_when_nothing_is_on_file(self):
        self._record({q: (0, False) for q in self.built_in})
        raw = json.dumps({"queries": ["query two",  # already tried: dropped
                                      "testville city auditor tax rate table"],
                          "note": "name the office"})
        with mock.patch("collector.extract.chat", return_value=(raw, None)) as chat:
            plan = self._plan(allow_reflect=True)
        chat.assert_called_once()
        self.assertEqual(plan[0], "testville city auditor tax rate table")
        row = self.conn.execute(
            "SELECT source, tries FROM search_query WHERE query LIKE "
            "'testville city auditor%'").fetchone()
        self.assertEqual(row["source"], "ai")
        self.assertEqual(row["tries"], 0)

    def test_reflection_prompt_carries_what_the_crawler_saw(self):
        self._record({q: (0, False) for q in self.built_in})
        self.conn.execute(
            "INSERT INTO crawl_page (geoid, category, url, title, fetched_at) "
            "VALUES (?,?,?,?,?)",
            (self.geoid, self.category, "https://testville.oh.us/fiscal",
             "Fiscal Office | Testville", db.now()))
        self.conn.execute(
            "UPDATE work_item SET last_error='extractor returned 0 valid rows' "
            "WHERE geoid=?", (self.geoid,))
        self.conn.commit()
        raw = json.dumps({"queries": ["testville fiscal office levy"]})
        with mock.patch("collector.extract.chat", return_value=(raw, None)) as chat:
            self._plan(allow_reflect=True)
        prompt = chat.call_args[0][1]
        self.assertIn("Fiscal Office | Testville", prompt)
        self.assertIn("query one", prompt)
        self.assertIn("extractor returned 0 valid rows", prompt)

    def test_reflection_respects_the_off_switch(self):
        self._record({q: (0, False) for q in self.built_in})
        with mock.patch("collector.extract.chat") as chat:
            self._plan(allow_reflect=True, search_learn="0")
        chat.assert_not_called()

    def test_reflection_skips_when_no_provider(self):
        self._record({q: (0, False) for q in self.built_in})
        with mock.patch("collector.extract.chat") as chat:
            self._plan(allow_reflect=True, provider="none", checker_provider="")
        chat.assert_not_called()

    def test_reflection_skips_when_findings_are_on_file_and_check_passed(self):
        """A refresh of a completed item keeps its proven wording."""
        sid = self.source()
        self.conn.execute(
            "INSERT INTO tax_instrument (geoid, category, instrument_code, "
            "status, rate_value, rate_unit, source_id, confidence, "
            "extraction_method, researcher, retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.geoid, self.category, "municipal_general_sales", "levied",
             1.5, "percent", sid, "high", "agent_research", "test", db.now()))
        self.conn.execute(
            "INSERT INTO check_result (geoid, category, verdict, created_at) "
            "VALUES (?,?,'pass',?)", (self.geoid, self.category, db.now()))
        self.conn.commit()
        self._record({q: (0, False) for q in self.built_in})
        with mock.patch("collector.extract.chat") as chat:
            self._plan(allow_reflect=True)
        chat.assert_not_called()

    def test_reflection_runs_again_when_the_check_flagged(self):
        """'Try again' on a flagged item deserves new wording too."""
        sid = self.source()
        self.conn.execute(
            "INSERT INTO tax_instrument (geoid, category, instrument_code, "
            "status, rate_value, rate_unit, source_id, confidence, "
            "extraction_method, researcher, retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.geoid, self.category, "municipal_general_sales", "levied",
             1.5, "percent", sid, "high", "agent_research", "test", db.now()))
        self.conn.execute(
            "INSERT INTO check_result (geoid, category, verdict, created_at) "
            "VALUES (?,?,'flag',?)", (self.geoid, self.category, db.now()))
        self.conn.commit()
        self._record({q: (0, False) for q in self.built_in})
        raw = json.dumps({"queries": ["testville certified rate resolution"]})
        with mock.patch("collector.extract.chat", return_value=(raw, None)) as chat:
            self._plan(allow_reflect=True)
        chat.assert_called_once()

    def test_reflection_stops_at_the_ai_query_cap(self):
        for i in range(self.searchlog.MAX_AI_QUERIES):
            self.conn.execute(
                "INSERT INTO search_query (geoid, category, query, source, "
                "tries, last_outcome, created_at) VALUES (?,?,?,'ai',1,'nothing',?)",
                (self.geoid, self.category, "ai query %d" % i, db.now()))
        self.conn.commit()
        with mock.patch("collector.extract.chat") as chat:
            n = self.searchlog.reflect(
                self.conn, self._settings(), self.geoid, self.category)
        chat.assert_not_called()
        self.assertEqual(n, 0)

    def test_a_failed_reflection_never_breaks_planning(self):
        self._record({q: (0, False) for q in self.built_in})
        with mock.patch("collector.extract.chat",
                        side_effect=RuntimeError("boom")):
            plan = self._plan(allow_reflect=True)
        self.assertEqual(plan, self.built_in)


class AdviceTests(DbTest):
    """The checker's recommendation beside every flagged item."""

    def setUp(self):
        super().setUp()
        try:
            from collector import check, present, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.checkmod = check
        self.present = present
        self.store = store
        store.apply_schema(self.conn)
        self.geoid = self.place()
        self.sid = self.source(url="https://example.test/rates", tier=2)
        self.conn.execute(
            "INSERT INTO work_item (geoid, category, priority, status, updated_at) "
            "VALUES (?,?,?,?,?)",
            (self.geoid, "sales_use", 10, "needs_review", db.now()))
        self.conn.commit()
        self.doc_text = "City rates page. The sales tax rate is 1.5% this year."

    def _finding(self, rate=1.5):
        self.conn.execute(
            "INSERT INTO tax_instrument (geoid, category, instrument_code, "
            "status, rate_value, rate_unit, source_id, confidence, "
            "extraction_method, researcher, retrieved_at, source_quote) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.geoid, "sales_use", "municipal_general_sales", "levied",
             rate, "percent", self.sid, "high", "agent_research", "test",
             db.now(), "sales tax rate is 1.5%"))
        self.conn.commit()

    def _settings(self, **kw):
        s = self.store.get_all(self.conn)
        s.update({"provider": "anthropic", "anthropic_api_key": "sk-x",
                  "checker_enabled": "1"})
        s.update(kw)
        return s

    def _advice_row(self):
        row = self.conn.execute(
            "SELECT advice FROM check_result ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row["advice"]) if row and row["advice"] else None

    def test_model_advice_is_recorded_with_the_flag(self):
        self._finding()
        raw = json.dumps({
            "verdicts": [{"instrument_code": "municipal_general_sales",
                          "verdict": "flag", "reason": "looks like the state rate"}],
            "advice": {"lean": "try_again",
                       "hint": "The quote reads like a statewide table."}})
        with mock.patch("collector.extract.chat", return_value=(raw, None)):
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        self.assertEqual(verdict, "flag")
        advice = self._advice_row()
        self.assertEqual(advice["lean"], "try_again")
        self.assertIn("statewide table", advice["hint"])

    def test_flag_without_advice_gets_the_default_hint(self):
        self._finding()
        raw = json.dumps({"verdicts": [
            {"instrument_code": "municipal_general_sales", "verdict": "flag",
             "reason": "unsure"}]})
        with mock.patch("collector.extract.chat", return_value=(raw, None)):
            self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        self.assertEqual(self._advice_row()["lean"], "unsure")

    def test_a_pass_stores_no_advice(self):
        self._finding()
        raw = json.dumps({
            "verdicts": [{"instrument_code": "municipal_general_sales",
                          "verdict": "pass", "reason": ""}],
            "advice": {"lean": "publish", "hint": "All fine."}})
        with mock.patch("collector.extract.chat", return_value=(raw, None)):
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        self.assertEqual(verdict, "pass")
        self.assertIsNone(self._advice_row())

    def test_hard_flags_carry_written_advice_without_a_model_call(self):
        self._finding(rate=400.0)  # implausible for percent
        with mock.patch("collector.extract.chat") as chat:
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        chat.assert_not_called()
        self.assertEqual(verdict, "flag")
        advice = self._advice_row()
        self.assertEqual(advice["lean"], "try_again")
        self.assertIn("unit", advice["hint"])

    def test_checker_error_advises_reading_the_source(self):
        self._finding()
        with mock.patch("collector.extract.chat", return_value=(None, "HTTP 529")):
            self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        advice = self._advice_row()
        self.assertEqual(advice["lean"], "unsure")
        self.assertIn("never ran", advice["hint"])

    def test_review_page_maps_the_lean_to_a_button(self):
        self._finding()
        self.conn.execute(
            "INSERT INTO check_result (geoid, category, verdict, flags, advice, "
            "created_at) VALUES (?,?,'flag',?,?,?)",
            (self.geoid, "sales_use",
             json.dumps([{"instrument_code": "municipal_general_sales",
                          "reason": "state rate"}]),
             json.dumps({"lean": "try_again", "hint": "Check the source."}),
             db.now()))
        self.conn.commit()
        item = self.present.review_items(self.conn)[0]
        self.assertEqual(item["advice"]["status"], "pending")
        self.assertEqual(item["advice"]["label"], "Try again")
        self.assertEqual(item["advice"]["hint"], "Check the source.")

    def test_zero_row_items_get_a_fallback_hint(self):
        advice = self.present.checker_advice(
            self.conn, self.geoid, "sales_use",
            last_error="extractor returned 0 valid rows (2 rejected); pages archived")
        self.assertEqual(advice["status"], "pending")
        self.assertIn("new wording", advice["hint"])


if __name__ == "__main__":
    unittest.main()
