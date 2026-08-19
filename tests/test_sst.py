"""SST adapter: FIPS mapping, current-rate selection, directory listing."""

from tests._db import DbTest

OH_CSV = """\
39,00,001,.01500,.01500,.01500,.01500,20010101,20051231
39,00,001,.01000,.01000,.01000,.01000,20060101,20060331
39,00,001,.01500,.01500,.01500,.01500,20060401,99991231
39,00,003,.01000,.01000,.01000,.01000,19880101,99991231
39,45,39,.05750,.05750,.05750,.05750,20140101,99991231
39,63,96000,.01000,.01000,.01000,.01000,20250401,99991231
47,1,00100,0.0225,0.0225,0.0225,0.0225,20200101,99991231
"""

INDEX_HTML = """
<html><body>
<a href="OHR2026Q1NOV28.csv">OHR2026Q1NOV28.csv</a>
<a href="WIR2026Q3MAY22.csv">WIR2026Q3MAY22.csv</a>
<a href="INR2008Q4MAY7.csv">INR2008Q4MAY7.csv</a>
<a href="OHB2026Q1NOV28.zip">boundary skip</a>
</body></html>
"""


class SstParseTests(DbTest):
    def setUp(self):
        super().setUp()
        self.place("39001", "Adams County", kind="county", pop=27000)
        self.place("39003", "Allen County", kind="county", pop=100000)
        self.place("4700100", "Test City city", state="TN", fips="47",
                   kind="place", pop=9000)

    def test_geoid_county_and_place_and_tn_unpadded_type(self):
        from taxdb.adapters.sst import geoid_for
        self.assertEqual(geoid_for("00", "39", "001"), "39001")
        self.assertEqual(geoid_for("01", "55", "00100"), "5500100")
        self.assertEqual(geoid_for("1", "47", "00100"), "4700100")
        self.assertIsNone(geoid_for("45", "39", "39"))
        self.assertIsNone(geoid_for("63", "39", "96000"))

    def test_current_county_rate_and_history_events(self):
        from taxdb.adapters.sst import rows_to_findings
        meta = {"state": "OH", "period": "2026Q1", "url": "https://example.test/OHR.csv"}
        findings, unmapped, events = rows_to_findings(
            self.conn, OH_CSV, meta, today="2026-08-18")
        by = {f["geoid"]: f for f in findings if f["instrument_code"] == "county_general_sales"}
        self.assertEqual(by["39001"]["rate_value"], 1.5)
        self.assertEqual(by["39001"]["status"], "levied")
        self.assertEqual(by["39001"]["source_quote"], "0.01500")
        self.assertEqual(by["39003"]["rate_value"], 1.0)
        place = [f for f in findings if f["geoid"] == "4700100"]
        self.assertEqual(place[0]["rate_value"], 2.25)
        self.assertEqual(place[0]["instrument_code"], "municipal_general_sales")
        reasons = " ".join(r for _, r in unmapped)
        self.assertIn("state taxing authority", reasons)
        self.assertIn("special", reasons)
        types = {e["change_type"] for e in events if e["geoid"] == "39001"}
        self.assertIn("new", types)
        self.assertIn("decrease", types)
        self.assertIn("increase", types)

    def test_stale_file_lowers_confidence(self):
        from taxdb.adapters.sst import rows_to_findings
        meta = {"state": "IN", "period": "2008Q4", "url": "https://example.test/IN.csv"}
        csv = "18,00,001,.01000,.01000,.01000,.01000,20080101,99991231\n"
        self.place("18001", "Adams County", state="IN", fips="18", kind="county")
        findings, _, _ = rows_to_findings(self.conn, csv, meta, today="2026-08-18")
        self.assertEqual(findings[0]["confidence"], "medium")
        self.assertIn("stale", findings[0]["notes"])

    def test_directory_listing_picks_one_file_per_state(self):
        from taxdb.adapters.sst import list_rate_files
        files = list_rate_files(INDEX_HTML)
        states = {f["state"]: f["period"] for f in files}
        self.assertEqual(states["OH"], "2026Q1")
        self.assertEqual(states["WI"], "2026Q3")
        self.assertEqual(states["IN"], "2008Q4")
        self.assertTrue(all(f["filename"].endswith(".csv") for f in files))

    def test_ingest_files_work_items_without_human_review(self):
        from taxdb.adapters.sst import rows_to_findings
        from taxdb import ingest, ledger
        meta = {"state": "OH", "period": "2026Q1", "url": "https://example.test/OHR.csv"}
        findings, _, _ = rows_to_findings(self.conn, OH_CSV, meta, today="2026-08-18")
        oh = [f for f in findings if f["geoid"] == "39001"]
        ingest.load_doc(self.conn, {"findings": oh, "extraction_method": "bulk_import"},
                        label="sst")
        claimed = ledger.claim(self.conn, limit=10, states=["OH"], categories=["sales_use"])
        self.assertEqual(claimed, [])
        row = self.conn.execute(
            "SELECT status, last_error, completed_at FROM work_item "
            "WHERE geoid='39001' AND category='sales_use'").fetchone()
        # Filed, not queued: a published rate file with a tier-2 citation and
        # archived bytes has better provenance than a crawled web page, and
        # tens of thousands of them would bury a real review queue.
        self.assertEqual(row["status"], "complete")
        self.assertIn("no human review needed", row["last_error"])
        self.assertIsNotNone(row["completed_at"])
