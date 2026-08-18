from taxdb import db
from tests._db import DbTest


class ArchiveTests(DbTest):
    def test_identical_sha_two_periods_both_archive(self):
        from taxdb import archive
        blob = b"rate file contents"
        sid = self.source()
        a1, sha1, _, created1 = archive.put(
            self.conn, "wa_dor_sales", "https://example.test/rates.csv",
            blob, "2026Q2", source_id=sid)
        a2, sha2, _, created2 = archive.put(
            self.conn, "wa_dor_sales", "https://example.test/rates.csv",
            blob, "2026Q3", source_id=sid)
        self.assertTrue(created1)
        self.assertTrue(created2)
        self.assertEqual(sha1, sha2)
        self.assertNotEqual(a1, a2)
        n = self.conn.execute("SELECT COUNT(*) c FROM archive_file").fetchone()["c"]
        self.assertEqual(n, 2)

    def test_duplicate_adapter_period_url_rejected(self):
        from taxdb import archive
        blob = b"rate file contents"
        sid = self.source()
        a1, _, _, created1 = archive.put(
            self.conn, "wa_dor_sales", "https://example.test/rates.csv",
            blob, "2026Q3", source_id=sid)
        a2, _, _, created2 = archive.put(
            self.conn, "wa_dor_sales", "https://example.test/rates.csv",
            blob, "2026Q3", source_id=sid)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(a1, a2)


class IngestTests(DbTest):
    def test_supersede_preserves_history(self):
        from taxdb import ingest
        geoid = self.place()
        doc = {
            "researcher": "test",
            "extraction_method": "manual",
            "findings": [{
                "geoid": geoid,
                "category": "sales_use",
                "instrument_code": "municipal_general_sales",
                "status": "levied",
                "rate_value": 1.0,
                "rate_unit": "percent",
                "confidence": "high",
                "source": {
                    "url": "https://example.test/rates",
                    "name": "DOR",
                    "source_type": "agency_table",
                    "authority_tier": 2,
                },
            }],
        }
        ingest.load_doc(self.conn, doc, label="v1")
        doc["findings"][0]["rate_value"] = 1.25
        ingest.load_doc(self.conn, doc, label="v2")
        live = self.conn.execute(
            "SELECT rate_value FROM tax_instrument WHERE geoid=? AND superseded_by IS NULL",
            (geoid,)).fetchone()
        old = self.conn.execute(
            "SELECT rate_value FROM tax_instrument WHERE geoid=? AND superseded_by IS NOT NULL",
            (geoid,)).fetchone()
        self.assertEqual(live["rate_value"], 1.25)
        self.assertEqual(old["rate_value"], 1.0)

    def test_rejects_unknown_geoid(self):
        from taxdb import ingest
        doc = {
            "findings": [{
                "geoid": "9999999",
                "category": "sales_use",
                "instrument_code": "municipal_general_sales",
                "status": "levied",
                "rate_value": 1.0,
                "rate_unit": "percent",
                "confidence": "high",
                "source": {"url": "https://example.test/x", "authority_tier": 2},
            }],
        }
        with self.assertRaises(SystemExit):
            ingest.load_doc(self.conn, doc, label="bad")
