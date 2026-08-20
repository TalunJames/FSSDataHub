"""The stale-document guard, batch timeline entries, and the diagnostic export.

The guard's promise: an old document can add history but can never displace
a newer rate as current, and when the machine cannot tell which is newer, a
human decides (hard flag), never the extraction order.
"""

import os
import shutil
import tempfile
import unittest

from taxdb import db, ingest
from tests._db import DbTest

STATUTE = {
    "url": "https://codes.example.gov/orc/5739.021",
    "name": "Revised Code 5739.021",
    "source_type": "statute",
    "authority_tier": 1,
}


def finding(geoid, **over):
    row = {
        "geoid": geoid,
        "category": "sales_use",
        "instrument_code": "county_general_sales",
        "status": "levied",
        "rate_value": 1.25,
        "rate_unit": "percent",
        "confidence": "high",
        "source": STATUTE,
        "source_quote": "the rate is 1.25 percent",
    }
    row.update(over)
    return row


class StalenessGuardTests(DbTest):
    def setUp(self):
        super().setUp()
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)

    def _current(self):
        return self.conn.execute(
            "SELECT * FROM tax_instrument WHERE geoid=? AND category='sales_use' "
            "AND superseded_by IS NULL", (self.geoid,)).fetchone()

    def _load(self, **over):
        res = ingest.load_doc(self.conn, {"findings": [finding(self.geoid, **over)]},
                              label="t")
        self.assertEqual(res["rejected"], 0, res["errors"])
        return res

    def test_older_document_files_as_history_not_current(self):
        self._load(rate_value=2.0, effective_date="2021-07-01")
        self._load(rate_value=1.0, effective_date="2015-01-01")
        cur = self._current()
        self.assertAlmostEqual(cur["rate_value"], 2.0)
        self.assertEqual(cur["effective_date"], "2021-07-01")
        old = self.conn.execute(
            "SELECT * FROM tax_instrument WHERE effective_date='2015-01-01'"
        ).fetchone()
        self.assertEqual(old["superseded_by"], cur["id"])
        self.assertIn("history", old["notes"])

    def test_newer_document_supersedes_normally(self):
        self._load(rate_value=2.0, effective_date="2021-07-01")
        self._load(rate_value=2.5, effective_date="2024-01-01")
        cur = self._current()
        self.assertAlmostEqual(cur["rate_value"], 2.5)

    def test_older_fiscal_year_files_as_history(self):
        self._load(rate_value=2.0, fiscal_year=2021)
        self._load(rate_value=1.0, fiscal_year=2015)
        self.assertAlmostEqual(self._current()["rate_value"], 2.0)

    def test_same_year_mixed_precision_is_not_treated_as_older(self):
        self._load(rate_value=2.0, effective_date="2021-07-01")
        self._load(rate_value=2.1, fiscal_year=2021)
        # Ambiguous, so the normal supersede happens; the checker's date
        # flags are the net for this case, not the ingest guard.
        self.assertAlmostEqual(self._current()["rate_value"], 2.1)

    def test_undated_over_dated_supersedes_but_hard_flags(self):
        try:
            from collector import check
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self._load(rate_value=2.0, effective_date="2021-07-01")
        self._load(rate_value=1.0)
        self.assertAlmostEqual(self._current()["rate_value"], 1.0)
        rows = check.live_rows(self.conn, self.geoid, "sales_use")
        flags = check.deterministic_flags(rows, "the rate is 1.25 percent")
        hard = [f for f in check.hard_only(flags) if f["code"] == "undated_supersede"]
        self.assertEqual(len(hard), 1, flags)

    def test_undated_over_undated_raises_no_date_flag(self):
        try:
            from collector import check
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self._load(rate_value=2.0)
        self._load(rate_value=2.1)
        rows = check.live_rows(self.conn, self.geoid, "sales_use")
        flags = check.deterministic_flags(rows, "the rate is 1.25 percent")
        self.assertFalse([f for f in flags
                          if f["code"] in ("undated_supersede", "date_regression")],
                         flags)

    def test_legacy_regression_is_flagged_on_recheck(self):
        """Data written before the guard: a 2015-dated row sitting as current
        over a superseded 2021 row must hard-flag when the item is rechecked."""
        try:
            from collector import check
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        sid = self.source()
        cur = self.conn.execute(
            "INSERT INTO tax_instrument (geoid, category, instrument_code, status, "
            "rate_value, rate_unit, effective_date, source_id, confidence, "
            "extraction_method, retrieved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.geoid, "sales_use", "county_general_sales", "levied", 1.0,
             "percent", "2015-01-01", sid, "high", "manual", db.now()))
        self.conn.execute(
            "INSERT INTO tax_instrument (geoid, category, instrument_code, status, "
            "rate_value, rate_unit, effective_date, source_id, confidence, "
            "extraction_method, retrieved_at, superseded_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.geoid, "sales_use", "county_general_sales", "levied", 2.0,
             "percent", "2021-07-01", sid, "high", "manual", db.now(),
             cur.lastrowid))
        self.conn.commit()
        rows = check.live_rows(self.conn, self.geoid, "sales_use")
        flags = check.deterministic_flags(rows, "")
        hard = [f for f in check.hard_only(flags) if f["code"] == "date_regression"]
        self.assertEqual(len(hard), 1, flags)


class TimelineBatchTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import present, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.present = present
        store.apply_schema(self.conn)

    def test_batch_send_and_return_appear(self):
        now = db.now()
        self.conn.execute(
            "INSERT INTO extract_batch (provider, status, n_items, created_at, "
            "submitted_at, collected_at) VALUES ('anthropic', 'collected', 37, "
            "?, ?, ?)", (now, now, now))
        self.conn.commit()
        texts = [e["text"] for e in self.present.timeline(self.conn)]
        self.assertTrue(any("Sent a batch of 37" in t for t in texts), texts)
        self.assertTrue(any("came back from Anthropic" in t for t in texts), texts)

    def test_failed_batch_is_flagged(self):
        now = db.now()
        self.conn.execute(
            "INSERT INTO extract_batch (provider, status, n_items, created_at, "
            "message) VALUES ('anthropic', 'failed', 12, ?, "
            "'submit never reached the provider')", (now,))
        self.conn.commit()
        events = self.present.timeline(self.conn)
        bad = [e for e in events if "could not be completed" in e["text"]]
        self.assertEqual(len(bad), 1, [e["text"] for e in events])
        self.assertEqual(bad[0]["tone"], "flag")


class DiagnosticExportTests(unittest.TestCase):
    """Through the real app with a throwaway database."""

    def setUp(self):
        try:
            from fastapi.testclient import TestClient
            from collector import app as appmod
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.tmpdir = tempfile.mkdtemp()
        self._old_db = os.environ.get("TAX_DATABASE_DB")
        os.environ["TAX_DATABASE_DB"] = os.path.join(self.tmpdir, "test.db")
        self.client = TestClient(appmod.app)
        self.version = appmod.__version__

    def tearDown(self):
        if self._old_db is None:
            os.environ.pop("TAX_DATABASE_DB", None)
        else:
            os.environ["TAX_DATABASE_DB"] = self._old_db
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_export_downloads_a_report(self):
        r = self.client.get("/api/logs/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers.get("content-disposition", ""))
        body = r.text
        self.assertIn("FSSDataHub diagnostic", body)
        self.assertIn("version: %s" % self.version, body)
        self.assertIn("Settings (secrets masked)", body)
        self.assertIn("Application log", body)
        # No secret material, masked or not, beyond the masked settings dump.
        self.assertNotIn("sk-ant", body)

    def test_export_says_what_is_in_the_database(self):
        """The report used to carry every error and no totals, which left
        "is it collecting anything at all?" unanswerable."""
        body = self.client.get("/api/logs/export").text
        for heading in ("Contents", "Work items by status",
                        "Batch items by status", "Checker verdicts"):
            self.assertIn(heading, body)
        for label in ("jurisdictions:", "tax instruments:",
                      "  from a bulk adapter:", "  from the crawler:",
                      "  filed from bulk:"):
            self.assertIn(label, body)

    def test_export_shows_reads_that_produced_nothing(self):
        """An empty read closes as 'done'; the old panel only showed
        'failed', so the commonest outcome was invisible."""
        from taxdb import db as dbmod
        from collector import store
        conn = store.connect()
        try:
            store.apply_schema(conn)
            run_id = store.start_run(conn, "continuous")
            conn.execute(
                "INSERT INTO extract_batch (provider, status, n_items, "
                "created_at) VALUES ('anthropic','collected',1,?)",
                (dbmod.now(),))
            conn.execute(
                "INSERT INTO extract_batch_item (batch_id, custom_id, run_id, "
                "geoid, category, packet, n_pages, status, error, created_at) "
                "VALUES (1,'x-1',?, '39049','property','p',3,'done',"
                "'extractor returned 0 valid rows (2 rejected); pages archived',?)",
                (run_id, dbmod.now()))
            conn.commit()
        finally:
            conn.close()
        body = self.client.get("/api/logs/export").text
        self.assertIn("Batch items that produced nothing", body)
        self.assertIn("0 valid rows", body)

    def test_export_groups_refusals_by_host(self):
        """77 refusals turned out to be six sites. That is the useful shape."""
        from taxdb import db as dbmod
        from collector import store
        conn = store.connect()
        try:
            store.apply_schema(conn)
            run_id = store.start_run(conn, "continuous")
            rows = [("https://www.mass.gov/a", 403), ("https://www.mass.gov/b", 403),
                    ("https://auditor.example.gov/c", 403),
                    ("https://fine.example.gov/d", 200)]
            for url, status in rows:
                conn.execute(
                    "INSERT INTO crawl_page (run_id, geoid, category, url, "
                    "http_status, error, fetched_at) VALUES (?,?,?,?,?,?,?)",
                    (run_id, "39049", "property", url, status,
                     "blocked" if status == 403 else None, dbmod.now()))
            conn.commit()
        finally:
            conn.close()
        body = self.client.get("/api/logs/export").text
        start = body.find("Hosts that refused us")
        end = body.find("Recent page errors")
        block = body[start:end]
        self.assertIn('"host": "www.mass.gov", "n": 2', block)
        self.assertIn("auditor.example.gov", block)
        # A page that came back fine is not a refusal.
        self.assertNotIn("fine.example.gov", block)
