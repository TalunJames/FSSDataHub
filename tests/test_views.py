from taxdb import db
from tests._db import DbTest


class HeadroomTests(DbTest):
    def test_overlapping_grants_do_not_fan_out(self):
        geoid = self.place()
        sid = self.source()
        for kind, efrom in ((None, "1990-01-01"), (None, "2010-01-01"), ("place", "2015-01-01")):
            self.conn.execute(
                "INSERT INTO authority_grant ("
                "state_usps, jurisdiction_kind, category, instrument_code, permitted, "
                "max_rate, max_rate_unit, statute_cite, source_id, extraction_method, "
                "effective_from, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("OH", kind, "sales_use", "municipal_general_sales", "yes",
                 3.0, "percent", "ORC 5739.02", sid, "manual", efrom, "high"))
        self.conn.execute(
            "INSERT INTO tax_instrument ("
            "geoid, category, instrument_code, status, rate_value, rate_unit, "
            "source_id, confidence, extraction_method, retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (geoid, "sales_use", "municipal_general_sales", "levied", 1.25, "percent",
             sid, "high", "manual", db.now()))
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT levied_rate, headroom FROM v_headroom WHERE geoid=?", (geoid,)
        ).fetchall()
        self.assertEqual(len(rows), 1, [dict(r) for r in rows])
        self.assertAlmostEqual(rows[0]["levied_rate"], 1.25)
        self.assertAlmostEqual(rows[0]["headroom"], 1.75)


class NearMissTests(DbTest):
    def test_levy_override_is_not_dropped(self):
        geoid = self.place()
        sid = self.source()
        self.conn.execute(
            "INSERT INTO ballot_measure ("
            "geoid, election_date, measure_id_local, measure_class, outcome, "
            "pct_yes, threshold_required, margin_vs_threshold, "
            "source_id, confidence, extraction_method, retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (geoid, "2024-11-05", "Issue 7", "levy_override", "failed",
             61.4, 66.67, -5.27, sid, "high", "manual", db.now()))
        self.conn.commit()
        rows = self.conn.execute("SELECT measure_class FROM v_near_miss").fetchall()
        self.assertEqual([r["measure_class"] for r in rows], ["levy_override"])

    def test_tax_like_filter_would_miss_it(self):
        self.assertFalse("levy_override".startswith("tax"))


class SunsetTests(DbTest):
    def _levy(self, geoid, sid, expiration, code="county_general_sales"):
        self.conn.execute(
            "INSERT INTO tax_instrument ("
            "geoid, category, instrument_code, status, rate_value, rate_unit, "
            "expiration_date, source_id, confidence, extraction_method, retrieved_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (geoid, "sales_use", code, "levied", 0.5, "percent",
             expiration, sid, "high", "manual", db.now()))

    def test_non_iso_date_excluded(self):
        geoid = self.place()
        sid = self.source()
        self._levy(geoid, sid, "6/30/2027")
        self.conn.commit()
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM v_sunset_watch WHERE geoid=?", (geoid,)
        ).fetchone()["c"]
        self.assertEqual(n, 0)
        flags = self.conn.execute(
            "SELECT COUNT(*) c FROM tax_instrument "
            "WHERE expiration_date IS NOT NULL AND julianday(expiration_date) IS NULL"
        ).fetchone()["c"]
        self.assertEqual(flags, 1)

    def test_iso_date_included(self):
        geoid = self.place("3999999")
        sid = self.source("https://example.test/iso")
        self._levy(geoid, sid, "2027-08-01", code="municipal_general_sales")
        self.conn.commit()
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM v_sunset_watch WHERE geoid=?", (geoid,)
        ).fetchone()["c"]
        self.assertEqual(n, 1)
