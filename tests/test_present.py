"""What the screens say: formatting, the record view, and the review deck."""

import datetime
import json
import unittest

from taxdb import db
from tests._db import DbTest

from collector import present, store


class FormattingTests(unittest.TestCase):
    def test_rate_units_read_as_english(self):
        self.assertEqual(present.rate(1.25, "percent"), "1.25%")
        self.assertEqual(present.rate(5.6, "mills"), "5.6 mills")
        self.assertEqual(present.rate(250.0, "usd_flat"), "$250")
        self.assertEqual(present.rate(12.5, "dollars_per_1000_av"), "12.5 per $1,000 AV")

    def test_missing_rate_is_a_dash_not_a_zero(self):
        self.assertEqual(present.rate(None, "percent"), "—")

    def test_when_reads_as_a_clock_not_a_stamp(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        recent = (now - datetime.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(present.when(recent).endswith("today")
                        or "yesterday" in present.when(recent))
        self.assertEqual(present.when(None), "—")
        self.assertEqual(present.when("not a date"), "—")

    def test_greeting_skips_the_default_researcher_label(self):
        noon = datetime.datetime(2026, 8, 18, 11, 0)
        self.assertEqual(present.greeting("collector", noon), "Good morning.")
        self.assertEqual(present.greeting("Carter", noon), "Good morning, Carter.")


class ViewTests(DbTest):
    def setUp(self):
        super(ViewTests, self).setUp()
        store.apply_schema(self.conn)
        self.geoid = self.place()
        self.sid = self.source(url="https://tax.example.gov/rates",
                               name="County rate table", source_type="agency_table")

    def _finding(self, rate=1.25, quote="the county levy is 1.25 percent",
                 superseded=None):
        self.conn.execute(
            "INSERT INTO tax_instrument (geoid, category, instrument_code, status, "
            "rate_value, rate_unit, source_id, confidence, extraction_method, "
            "retrieved_at, source_quote, superseded_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.geoid, "sales_use", "county_sales_general", "levied", rate,
             "percent", self.sid, "high", "agent_research", db.now(), quote,
             superseded))
        self.conn.commit()

    def _work(self, status="needs_review", category="sales_use"):
        self.conn.execute(
            "INSERT OR REPLACE INTO work_item (geoid, category, status, priority, "
            "updated_at, completed_at) VALUES (?,?,?,?,?,?)",
            (self.geoid, category, status, 10, db.now(),
             db.now() if status == "complete" else None))
        self.conn.commit()

    def test_review_item_carries_the_quote_and_the_flag_reason(self):
        self._work()
        self._finding()
        self.conn.execute(
            "INSERT INTO check_result (geoid, category, verdict, flags, created_at) "
            "VALUES (?,?,'flag',?,?)",
            (self.geoid, "sales_use", json.dumps(
                [{"instrument_code": "county_sales_general",
                  "reason": "the quote is the state rate table"}]), db.now()))
        self.conn.commit()
        items = present.review_items(self.conn)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["rate"], "1.25%")
        self.assertIn("state rate table", item["reasons"][0])
        self.assertIn("1.25 percent", item["quote"])
        self.assertEqual(item["source_url"], "https://tax.example.gov/rates")
        self.assertEqual(item["previous"], "—")

    def test_review_item_shows_what_was_on_record_before(self):
        self._work()
        self._finding(rate=0.75)
        prior = self.conn.execute(
            "SELECT id FROM tax_instrument ORDER BY id LIMIT 1").fetchone()[0]
        # Only one row per place and tax may be current, so retire the old one
        # before writing the new rate — the same order ingest uses.
        self.conn.execute("UPDATE tax_instrument SET superseded_by=id WHERE id=?",
                          (prior,))
        self._finding(rate=1.25)
        current = self.conn.execute(
            "SELECT id FROM tax_instrument ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.conn.execute("UPDATE tax_instrument SET superseded_by=? WHERE id=?",
                          (current, prior))
        self.conn.commit()
        item = present.review_items(self.conn)[0]
        self.assertEqual(item["rate"], "1.25%")
        self.assertEqual(item["previous"], "0.75%")

    def test_record_counts_every_chip_and_filters_to_one(self):
        self._work(status="complete")
        self._work(status="needs_review", category="property")
        self._finding()
        view = present.record(self.conn)
        chips = dict((c["key"], c["count"]) for c in view["chips"])
        self.assertEqual(chips["all"], 2)
        self.assertEqual(chips["published"], 1)
        self.assertEqual(chips["review"], 1)
        only = present.record(self.conn, filt="review")
        self.assertEqual(len(only["places"]), 1)
        self.assertEqual(only["places"][0]["standing"], "Needs you")

    def test_record_search_matches_name_state_or_geoid(self):
        self._work(status="complete")
        self.assertEqual(len(present.record(self.conn, q="Testville")["places"]), 1)
        self.assertEqual(len(present.record(self.conn, q=self.geoid)["places"]), 1)
        self.assertEqual(len(present.record(self.conn, q="Nowhere")["places"]), 0)

    def test_inbox_names_a_place_once_however_many_taxes_it_has(self):
        self._work(status="needs_review", category="sales_use")
        self._work(status="needs_review", category="property")
        rows = present.inbox(
            self.conn, {"needs_review": 2, "ready": True, "pending": 1,
                        "in_progress": 0},
            {"provider": "anthropic"}, {"juris_total": 10, "juris_done": 1})
        self.assertTrue(rows)
        self.assertEqual(rows[0]["why"].count("Testville city"), 1)

    def test_timeline_reports_what_the_checker_did(self):
        self.conn.execute(
            "INSERT INTO check_result (geoid, category, verdict, created_at) "
            "VALUES (?,?,'pass',?)", (self.geoid, "sales_use", db.now()))
        self.conn.commit()
        events = present.timeline(self.conn)
        self.assertTrue(any("confirmed by the second check" in e["text"]
                            for e in events))


if __name__ == "__main__":
    unittest.main()
