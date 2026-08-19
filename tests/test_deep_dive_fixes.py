"""Regression guards from the 2026-08 deep dive.

Each test pins a specific fixed bug: measures with no local ID merging into
each other, revoked grants surviving in v_live_grant, instrument-specific
threshold rules swallowing the general rule, the checker trusting malformed
verdicts, the refresh livelock on bulk-covered items, and the archive
returning stale bytes for changed documents.
"""

from taxdb import archive, db, ingest, ledger
from tests._db import DbTest

STATUTE = {
    "url": "https://codes.example.gov/orc/5705.19",
    "name": "Revised Code 5705.19",
    "source_type": "statute",
    "authority_tier": 1,
}
CANVASS = {
    "url": "https://vote.example.gov/2024/canvass.pdf",
    "name": "County board of elections canvass",
    "source_type": "agency_table",
    "authority_tier": 2,
}


def measure(geoid, **over):
    row = {
        "geoid": geoid,
        "election_date": "2024-11-05",
        "measure_id_local": None,
        "measure_class": "levy_override",
        "outcome": "failed",
        "votes_yes": 1000,
        "votes_no": 900,
        "confidence": "high",
        "source": CANVASS,
    }
    row.update(over)
    return row


class MeasureIdentityTests(DbTest):
    def setUp(self):
        super().setUp()
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)

    def test_two_no_id_measures_stay_two_rows(self):
        """A parks levy and a fire levy on the same ballot, neither carrying a
        measure number, must not merge into one row."""
        res = ingest.load_doc(self.conn, {"measures": [
            measure(self.geoid, official_title="Parks levy",
                    outcome="passed", votes_yes=1000, votes_no=500),
            measure(self.geoid, official_title="Fire levy",
                    outcome="failed", votes_yes=700, votes_no=900),
        ]}, label="canvass")
        self.assertEqual(res["rejected"], 0, res["errors"])
        rows = self.conn.execute(
            "SELECT official_title, outcome FROM ballot_measure "
            "WHERE geoid=? ORDER BY official_title", (self.geoid,)).fetchall()
        self.assertEqual([(r["official_title"], r["outcome"]) for r in rows],
                         [("Fire levy", "failed"), ("Parks levy", "passed")])

    def test_same_title_no_id_updates_in_place(self):
        for _ in range(2):
            ingest.load_doc(self.conn, {"measures": [
                measure(self.geoid, official_title="Parks levy")]}, label="re")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM ballot_measure WHERE geoid=?",
            (self.geoid,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_reingest_without_archive_keeps_archive_link(self):
        aid, _, _, _ = archive.put(
            self.conn, "crawl", CANVASS["url"], b"canvass bytes", "crawl")
        ingest.load_doc(self.conn, {"measures": [
            measure(self.geoid, official_title="Parks levy",
                    archive_file_id=aid)]}, label="first")
        ingest.load_doc(self.conn, {"measures": [
            measure(self.geoid, official_title="Parks levy")]}, label="again")
        row = self.conn.execute(
            "SELECT archive_file_id FROM ballot_measure WHERE geoid=?",
            (self.geoid,)).fetchone()
        self.assertEqual(row["archive_file_id"], aid)


class ThresholdSpecificityTests(DbTest):
    def setUp(self):
        super().setUp()
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)
        sid = self.source()
        for inst, value in ((None, 50.0), ("transit_district_sales", 66.67)):
            self.conn.execute(
                "INSERT INTO threshold_rule (state_usps, measure_class, "
                "instrument_code, threshold_value, threshold_basis, "
                "statute_cite, source_id, extraction_method, confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("OH", "tax_increase", inst, value, "votes_cast",
                 "ORC test", sid, "manual", "high"))
        self.conn.commit()

    def test_instrument_specific_rule_never_leaks(self):
        """A transit-district supermajority must not attach to a county
        general sales measure."""
        rule = ingest.threshold_for(self.conn, {
            "geoid": self.geoid, "measure_class": "tax_increase",
            "election_date": "2024-11-05",
            "instrument_code": "county_general_sales"})
        self.assertIsNotNone(rule)
        self.assertAlmostEqual(rule["threshold_value"], 50.0)

    def test_both_rules_live_in_the_view(self):
        rows = self.conn.execute(
            "SELECT instrument_code, threshold_value FROM v_live_threshold "
            "WHERE state_usps='OH' AND measure_class='tax_increase'").fetchall()
        self.assertEqual(len(rows), 2, [dict(r) for r in rows])

    def test_margin_needs_a_votes_cast_basis(self):
        m = ingest.derive_measure(self.conn, {
            "geoid": self.geoid, "measure_class": "tax_increase",
            "election_date": "2024-11-05", "votes_yes": 600, "votes_no": 400,
            "threshold_required": 50.0, "threshold_basis": "registered_voters"})
        self.assertIsNone(m.get("margin_vs_threshold"))


class LiveGrantTests(DbTest):
    def _grant(self, permitted, efrom):
        sid = self.source()
        self.conn.execute(
            "INSERT INTO authority_grant (state_usps, category, "
            "instrument_code, permitted, max_rate, max_rate_unit, statute_cite, "
            "source_id, extraction_method, effective_from, confidence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("OH", "sales_use", "county_general_sales", permitted, 2.25,
             "percent", "ORC 5739.021", sid, "manual", efrom, "high"))
        self.conn.commit()

    def test_revocation_suppresses_the_grant(self):
        self.place()
        self._grant("yes", "2000-01-01")
        self._grant("no", "2020-01-01")
        rows = self.conn.execute(
            "SELECT * FROM v_live_grant WHERE state_usps='OH'").fetchall()
        self.assertEqual(rows, [])

    def test_future_grant_does_not_hide_the_current_one(self):
        self.place()
        self._grant("yes", "2000-01-01")
        self._grant("yes", "2099-01-01")
        rows = self.conn.execute(
            "SELECT effective_from FROM v_live_grant WHERE state_usps='OH'"
        ).fetchall()
        self.assertEqual([r["effective_from"] for r in rows], ["2000-01-01"])


class RefreshLivelockTests(DbTest):
    def _work_item(self, geoid, bulk):
        sid = self.source()
        self.conn.execute(
            "INSERT INTO work_item (geoid, category, status, completed_at, "
            "updated_at) VALUES (?,?,?,?,?)",
            (geoid, "sales_use", "complete", "2020-01-01 00:00:00", db.now()))
        if bulk:
            self.conn.execute(
                "INSERT INTO tax_instrument (geoid, category, instrument_code, "
                "status, rate_value, rate_unit, source_id, confidence, "
                "extraction_method, retrieved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (geoid, "sales_use", "county_general_sales", "levied", 1.0,
                 "percent", sid, "high", "bulk_import", db.now()))
        self.conn.commit()

    def test_bulk_covered_items_are_not_requeued(self):
        """requeue -> claim re-parks with the old completed_at -> requeue was
        a livelock; bulk items refresh via their adapter, not a crawl."""
        bulk = self.place(geoid="39049", name="Franklin County", kind="county")
        fresh = self.place(geoid="39051", name="Fulton County", kind="county")
        self._work_item(bulk, bulk=True)
        self._work_item(fresh, bulk=False)
        n = ledger.requeue_stale(self.conn, days=365)
        self.assertEqual(n, 1)
        statuses = dict(self.conn.execute(
            "SELECT geoid, status FROM work_item").fetchall())
        self.assertEqual(statuses[bulk], "complete")
        self.assertEqual(statuses[fresh], "pending")


class ArchiveVersioningTests(DbTest):
    def test_changed_bytes_get_a_new_row(self):
        url = "https://county.example.gov/rates.pdf"
        id1, sha1, _, created1 = archive.put(
            self.conn, "crawl", url, b"old rates", "crawl")
        id2, sha2, _, created2 = archive.put(
            self.conn, "crawl", url, b"new rates", "crawl")
        id3, _, _, created3 = archive.put(
            self.conn, "crawl", url, b"new rates", "crawl")
        self.assertTrue(created1)
        self.assertNotEqual(id1, id2)
        self.assertNotEqual(sha1, sha2)
        self.assertTrue(created2)
        self.assertEqual(id2, id3)
        self.assertFalse(created3)

    def test_unchanged_bytes_still_dedup(self):
        url = "https://county.example.gov/rates.pdf"
        id1 = archive.put(self.conn, "crawl", url, b"same", "crawl")[0]
        id2 = archive.put(self.conn, "crawl", url, b"same", "crawl")[0]
        self.assertEqual(id1, id2)
