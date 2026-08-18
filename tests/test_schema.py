from tests._db import DbTest

EXPECTED_TABLES = {
    "jurisdiction", "census_gid_crosswalk", "source", "raw_document",
    "archive_file", "state_profile", "authority_grant", "threshold_rule",
    "tax_instrument", "rate_change_event", "ballot_measure",
    "measure_attempt_chain", "campaign_committee", "revenue_base",
    "yield_estimate", "claim_source", "coverage_assertion", "work_item",
    "run_log", "revenue_measure_class", "statute_section",
}

EXPECTED_VIEWS = {
    "v_current_tax", "v_sunset_watch", "v_live_grant", "v_headroom",
    "v_near_miss", "v_measure_capture_gap", "v_coverage",
}


class SchemaTests(DbTest):
    def test_tables(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        self.assertTrue(EXPECTED_TABLES.issubset(names), EXPECTED_TABLES - names)

    def test_views(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'")}
        self.assertEqual(names, EXPECTED_VIEWS)

    def test_revenue_measure_classes(self):
        rows = [r[0] for r in self.conn.execute(
            "SELECT measure_class FROM revenue_measure_class ORDER BY 1")]
        self.assertIn("levy_override", rows)
        self.assertIn("de_bruce", rows)
        self.assertIn("assessment_district", rows)

    def test_foreign_keys_on(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_reapply_is_idempotent(self):
        from taxdb import db
        db.apply_schema(self.conn)
        n = self.conn.execute("SELECT COUNT(*) c FROM census_gid_crosswalk").fetchone()["c"]
        self.assertEqual(n, 51)
