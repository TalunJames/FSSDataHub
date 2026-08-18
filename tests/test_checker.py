"""Second checker: deterministic flags, verdict application, migrations."""

import json
import unittest
from unittest import mock

from taxdb import db
from tests._db import DbTest


def _finding_row(**kw):
    base = {
        "id": 1, "instrument_code": "county_general_sales", "label": None,
        "status": "levied", "rate_value": 1.5, "rate_unit": "percent",
        "cap_value": None, "cap_unit": None, "confidence": "high",
        "source_quote": "the county sales tax rate is 1.5%",
        "notes": None, "effective_date": None, "url": "https://example.test",
    }
    base.update(kw)
    return base


class DeterministicFlagTests(unittest.TestCase):
    def setUp(self):
        try:
            from collector import check
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.check = check

    def test_clean_finding_passes(self):
        doc = "Rates page. The county sales tax rate is 1.5% effective 2024."
        flags = self.check.deterministic_flags([_finding_row()], doc)
        self.assertEqual(flags, [])

    def test_missing_quote_flagged(self):
        flags = self.check.deterministic_flags(
            [_finding_row(source_quote="")], "whatever")
        self.assertEqual([f["code"] for f in flags], ["no_quote"])

    def test_quote_not_in_text_flagged(self):
        flags = self.check.deterministic_flags(
            [_finding_row(source_quote="rate is 7.25%")], "different text entirely")
        self.assertIn("quote_missing", [f["code"] for f in flags])

    def test_quote_check_skipped_without_text(self):
        # Image-only intake has no crawled text; do not flag on that alone.
        flags = self.check.deterministic_flags([_finding_row()], "")
        self.assertEqual(flags, [])

    def test_implausible_mills_flagged(self):
        doc = "the levy is 9000 mills"
        flags = self.check.deterministic_flags(
            [_finding_row(rate_value=9000, rate_unit="mills",
                          source_quote="the levy is 9000 mills")], doc)
        self.assertIn("implausible_rate", [f["code"] for f in flags])

    def test_low_confidence_flagged(self):
        doc = "the county sales tax rate is 1.5%"
        flags = self.check.deterministic_flags(
            [_finding_row(confidence="low")], doc)
        self.assertIn("low_confidence", [f["code"] for f in flags])

    def test_prohibited_with_rate_flagged(self):
        doc = "the county sales tax rate is 1.5%"
        flags = self.check.deterministic_flags(
            [_finding_row(status="prohibited")], doc)
        self.assertIn("status_conflict", [f["code"] for f in flags])

    def test_rate_over_cap_flagged(self):
        doc = "the county sales tax rate is 1.5%"
        flags = self.check.deterministic_flags(
            [_finding_row(cap_value=1.0, cap_unit="percent")], doc)
        self.assertIn("over_cap", [f["code"] for f in flags])


class RunAndApplyTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import check, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.checkmod = check
        self.store = store
        store.apply_schema(self.conn)
        self.geoid = self.place()
        sid = self.source(url="https://example.test/rates", tier=2)
        self.conn.execute(
            "INSERT INTO tax_instrument (geoid, category, instrument_code, status, "
            "rate_value, rate_unit, source_id, confidence, extraction_method, "
            "researcher, retrieved_at, source_quote) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.geoid, "sales_use", "municipal_general_sales", "levied", 1.5,
             "percent", sid, "high", "agent_research", "test", db.now(),
             "sales tax rate is 1.5%"))
        self.conn.execute(
            "INSERT INTO work_item (geoid, category, priority, status, updated_at) "
            "VALUES (?,?,?,?,?)",
            (self.geoid, "sales_use", 10, "needs_review", db.now()))
        self.conn.commit()
        self.doc_text = "City rates page. The sales tax rate is 1.5% this year."

    def _settings(self, **kw):
        s = self.store.get_all(self.conn)
        s.update({"provider": "anthropic", "anthropic_api_key": "sk-x",
                  "checker_enabled": "1"})
        s.update(kw)
        return s

    def _status(self):
        return self.conn.execute(
            "SELECT status, last_error FROM work_item WHERE geoid=? AND category=?",
            (self.geoid, "sales_use")).fetchone()

    def test_pass_marks_complete(self):
        raw = json.dumps({"verdicts": [
            {"instrument_code": "municipal_general_sales", "verdict": "pass",
             "reason": ""}]})
        with mock.patch("collector.extract.chat", return_value=(raw, None)):
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        self.assertEqual(verdict, "pass")
        self.assertEqual(self._status()["status"], "complete")
        row = self.conn.execute("SELECT * FROM check_result").fetchone()
        self.assertEqual(row["verdict"], "pass")

    def test_ai_flag_goes_to_review(self):
        raw = json.dumps({"verdicts": [
            {"instrument_code": "municipal_general_sales", "verdict": "flag",
             "reason": "looks like the state rate"}]})
        with mock.patch("collector.extract.chat", return_value=(raw, None)):
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        self.assertEqual(verdict, "flag")
        row = self._status()
        self.assertEqual(row["status"], "needs_review")
        self.assertIn("state rate", row["last_error"])

    def test_checker_api_error_fails_toward_review(self):
        with mock.patch("collector.extract.chat", return_value=(None, "HTTP 529")):
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        self.assertEqual(verdict, "error")
        self.assertEqual(self._status()["status"], "needs_review")

    def test_no_provider_fails_toward_review(self):
        verdict, _ = self.checkmod.run_and_apply(
            self.conn, self._settings(provider="none"), None, self.geoid,
            "sales_use", self.doc_text)
        self.assertEqual(verdict, "error")
        self.assertEqual(self._status()["status"], "needs_review")

    def test_disabled_leaves_status_alone(self):
        verdict, _ = self.checkmod.run_and_apply(
            self.conn, self._settings(checker_enabled="0"), None, self.geoid,
            "sales_use", self.doc_text)
        self.assertEqual(verdict, "off")
        self.assertEqual(self._status()["status"], "needs_review")

    def test_deterministic_flag_skips_model_pass(self):
        # Quote not present in the crawled text: flag even if the AI passes it.
        raw = json.dumps({"verdicts": [
            {"instrument_code": "municipal_general_sales", "verdict": "pass",
             "reason": ""}]})
        with mock.patch("collector.extract.chat", return_value=(raw, None)):
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                "totally unrelated page text")
        self.assertEqual(verdict, "flag")
        self.assertEqual(self._status()["status"], "needs_review")


class MigrationTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.store = store

    def test_old_mode_check_is_rebuilt(self):
        # Simulate a database created before fetch/cog/statutes existed.
        self.conn.executescript("""
            CREATE TABLE crawl_run (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL CHECK (mode IN (
                    'burst','continuous','schedule','manual_url','seed','plan')),
                status TEXT NOT NULL CHECK (status IN (
                    'running','ok','stopped','failed')),
                provider TEXT, filter_states TEXT,
                items_claimed INTEGER NOT NULL DEFAULT 0,
                pages_fetched INTEGER NOT NULL DEFAULT 0,
                findings_written INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL, finished_at TEXT, message TEXT);
        """)
        self.conn.execute(
            "INSERT INTO crawl_run (mode, status, started_at) VALUES (?,?,?)",
            ("burst", "ok", db.now()))
        self.conn.commit()
        self.store.apply_schema(self.conn)
        # Old rows survive, and the previously-rejected modes now insert.
        rows = self.conn.execute("SELECT mode FROM crawl_run").fetchall()
        self.assertEqual([r["mode"] for r in rows], ["burst"])
        for mode in ("fetch", "cog", "statutes"):
            self.store.start_run(self.conn, mode)

    def test_unused_default_model_upgraded_once(self):
        self.store.apply_schema(self.conn)
        self.conn.execute(
            "UPDATE collector_setting SET value='claude-haiku-4-5' "
            "WHERE key='anthropic_model'")
        self.conn.execute(
            "DELETE FROM collector_setting WHERE key='model_default_migrated'")
        self.conn.commit()
        self.store.apply_schema(self.conn)
        s = self.store.get_all(self.conn)
        self.assertEqual(s["anthropic_model"], "claude-sonnet-5")
        # Second run must not overwrite a deliberate later choice.
        self.store.put(self.conn, "anthropic_model", "claude-haiku-4-5")
        self.conn.commit()
        self.store.apply_schema(self.conn)
        s = self.store.get_all(self.conn)
        self.assertEqual(s["anthropic_model"], "claude-haiku-4-5")

    def test_key_never_set_by_masked_placeholder(self):
        self.store.apply_schema(self.conn)
        self.store.put(self.conn, "anthropic_api_key", "sk-ant-realkeyAbCd")
        self.conn.commit()
        current = self.store.get_all(self.conn)
        masked = self.store.mask(current)["anthropic_api_key"]
        updates = self.store.sanitize_updates(
            current, {"anthropic_api_key": masked, "provider": "anthropic",
                      "bogus_key": "x"})
        self.assertNotIn("anthropic_api_key", updates)
        self.assertNotIn("bogus_key", updates)
        self.assertEqual(updates["provider"], "anthropic")
        # A genuinely new key is saved.
        updates = self.store.sanitize_updates(
            current, {"anthropic_api_key": "sk-ant-newkey"})
        self.assertEqual(updates["anthropic_api_key"], "sk-ant-newkey")
