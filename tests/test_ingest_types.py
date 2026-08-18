"""Ingest of the sections that used to have no writer at all.

Before this, threshold_rule, authority_grant and ballot_measure existed only in
schema.sql: nothing in the codebase could write them, so v_headroom and
v_near_miss could never return a row. These tests are the guard on that.
"""

from taxdb import db, ingest
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


def threshold(**over):
    row = {
        "state_usps": "OH",
        "measure_class": "levy_override",
        "threshold_value": 60.0,
        "threshold_basis": "votes_cast",
        "statute_cite": "ORC 5705.19",
        "confidence": "high",
        "source": STATUTE,
    }
    row.update(over)
    return row


def grant(**over):
    row = {
        "state_usps": "OH",
        "jurisdiction_kind": "county",
        "category": "sales_use",
        "instrument_code": "county_general_sales",
        "permitted": "yes",
        "max_rate": 2.25,
        "max_rate_unit": "percent",
        "statute_cite": "ORC 5739.021",
        "confidence": "high",
        "source": STATUTE,
    }
    row.update(over)
    return row


def measure(geoid, **over):
    row = {
        "geoid": geoid,
        "election_date": "2024-11-05",
        "measure_id_local": "Issue 7",
        "measure_class": "levy_override",
        "outcome": "failed",
        "votes_yes": 1000,
        "votes_no": 900,
        "confidence": "high",
        "source": CANVASS,
    }
    row.update(over)
    return row


class MultiSectionTests(DbTest):
    def setUp(self):
        super().setUp()
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)

    def test_all_sections_write(self):
        res = ingest.load_doc(self.conn, {
            "researcher": "test",
            "thresholds": [threshold()],
            "grants": [grant()],
            "measures": [measure(self.geoid)],
            "findings": [{
                "geoid": self.geoid, "category": "sales_use",
                "instrument_code": "county_general_sales", "status": "levied",
                "rate_value": 1.25, "rate_unit": "percent", "confidence": "high",
                "source": STATUTE, "source_quote": "1.25 percent",
            }],
            "profile": {"state_usps": "OH", "home_rule_doctrine": "home_rule"},
        }, label="all")
        self.assertEqual(res["rejected"], 0, res["errors"])
        self.assertEqual(res["by_type"],
                         {"findings": 1, "measures": 1, "thresholds": 1,
                          "grants": 1, "profile": 1})
        self.assertEqual(res["written"], 5)

    def test_document_with_no_known_section_is_refused(self):
        with self.assertRaises(SystemExit):
            ingest.load_doc(self.conn, {"notes": "nothing useful"}, label="junk")

    def test_profile_only_document_is_accepted(self):
        res = ingest.load_doc(self.conn, {
            "profile": {"state_usps": "OH", "property_tax_limit_type": "rate_cap"}},
            label="profile")
        self.assertEqual(res["by_type"]["profile"], 1)
        row = self.conn.execute(
            "SELECT property_tax_limit_type, verified_at FROM state_profile "
            "WHERE state_usps='OH'").fetchone()
        self.assertEqual(row["property_tax_limit_type"], "rate_cap")
        self.assertIsNotNone(row["verified_at"])

    def test_reload_updates_rather_than_duplicating(self):
        doc = {
            "thresholds": [threshold()],
            "grants": [grant()],
            "measures": [measure(self.geoid)],
        }
        ingest.load_doc(self.conn, doc, label="first")
        ingest.load_doc(self.conn, doc, label="second")
        for table in ("threshold_rule", "authority_grant", "ballot_measure"):
            n = self.conn.execute("SELECT COUNT(*) c FROM %s" % table).fetchone()["c"]
            self.assertEqual(n, 1, "%s duplicated on re-ingest" % table)

    def test_reload_does_not_blank_a_populated_column(self):
        ingest.load_doc(self.conn, {
            "measures": [measure(self.geoid, stated_purpose="Fire and EMS")]},
            label="first")
        ingest.load_doc(self.conn, {"measures": [measure(self.geoid)]}, label="second")
        row = self.conn.execute(
            "SELECT stated_purpose FROM ballot_measure").fetchone()
        self.assertEqual(row["stated_purpose"], "Fire and EMS")

    def test_every_written_claim_gets_a_primary_citation(self):
        ingest.load_doc(self.conn, {
            "thresholds": [threshold()], "grants": [grant()],
            "measures": [measure(self.geoid)],
        }, label="cites")
        rows = self.conn.execute(
            "SELECT claim_table, role FROM claim_source ORDER BY claim_table").fetchall()
        self.assertEqual(
            [(r["claim_table"], r["role"]) for r in rows],
            [("authority_grant", "primary"), ("ballot_measure", "primary"),
             ("threshold_rule", "primary")])

    def test_corroborating_source_is_recorded(self):
        ingest.load_doc(self.conn, {"measures": [measure(
            self.geoid,
            corroborating_sources=[{"url": "https://sos.example.gov/results",
                                    "name": "Secretary of State"}])]}, label="corr")
        roles = [r["role"] for r in self.conn.execute(
            "SELECT role FROM claim_source WHERE claim_table='ballot_measure' "
            "ORDER BY role")]
        self.assertEqual(roles, ["corroborating", "primary"])

    def test_work_item_for_the_pass_is_opened(self):
        ingest.load_doc(self.conn, {"measures": [measure(self.geoid)]}, label="wi")
        row = self.conn.execute(
            "SELECT status FROM work_item WHERE geoid=? AND category='elections'",
            (self.geoid,)).fetchone()
        self.assertEqual(row["status"], "needs_review")


class MeasureArithmeticTests(DbTest):
    def setUp(self):
        super().setUp()
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)

    def test_percentage_and_total_are_computed_from_counts(self):
        ingest.load_doc(self.conn, {"measures": [measure(
            self.geoid, votes_yes=1200, votes_no=800)]}, label="calc")
        row = self.conn.execute(
            "SELECT votes_total, pct_yes FROM ballot_measure").fetchone()
        self.assertEqual(row["votes_total"], 2000)
        self.assertAlmostEqual(row["pct_yes"], 60.0)

    def test_turnout_is_computed(self):
        ingest.load_doc(self.conn, {"measures": [measure(
            self.geoid, registered_voters=4000, ballots_cast=2200)]}, label="turnout")
        row = self.conn.execute("SELECT turnout_pct FROM ballot_measure").fetchone()
        self.assertAlmostEqual(row["turnout_pct"], 55.0)

    def test_threshold_and_margin_come_from_the_rule_in_force(self):
        ingest.load_doc(self.conn, {
            "thresholds": [threshold(threshold_value=60.0)],
            "measures": [measure(self.geoid, votes_yes=1000, votes_no=1000)],
        }, label="margin")
        row = self.conn.execute(
            "SELECT threshold_required, threshold_basis, margin_vs_threshold, "
            "threshold_rule_id FROM ballot_measure").fetchone()
        self.assertAlmostEqual(row["threshold_required"], 60.0)
        self.assertEqual(row["threshold_basis"], "votes_cast")
        self.assertAlmostEqual(row["margin_vs_threshold"], -10.0)
        self.assertIsNotNone(row["threshold_rule_id"])

    def test_purpose_specific_threshold_wins_over_either(self):
        ingest.load_doc(self.conn, {"thresholds": [
            threshold(purpose_restriction="either", threshold_value=50.0),
            threshold(purpose_restriction="special", threshold_value=66.67),
        ]}, label="rules")
        ingest.load_doc(self.conn, {"measures": [measure(
            self.geoid, purpose_type="special")]}, label="m")
        row = self.conn.execute(
            "SELECT threshold_required FROM ballot_measure").fetchone()
        self.assertAlmostEqual(row["threshold_required"], 66.67)

    def test_jurisdiction_kind_beats_the_statewide_default(self):
        ingest.load_doc(self.conn, {"thresholds": [
            threshold(jurisdiction_kind=None, threshold_value=50.0),
            threshold(jurisdiction_kind="county", threshold_value=60.0),
        ]}, label="rules")
        ingest.load_doc(self.conn, {"measures": [measure(self.geoid)]}, label="m")
        row = self.conn.execute(
            "SELECT threshold_required FROM ballot_measure").fetchone()
        self.assertAlmostEqual(row["threshold_required"], 60.0)

    def test_rule_adopted_after_the_election_is_not_applied(self):
        """A margin recomputed against today's matrix is wrong for anything
        older than the last rule change."""
        ingest.load_doc(self.conn, {"thresholds": [
            threshold(threshold_value=50.0, effective_from="1990-01-01",
                      effective_to="2019-12-31"),
            threshold(threshold_value=66.67, effective_from="2020-01-01"),
        ]}, label="rules")
        ingest.load_doc(self.conn, {"measures": [
            measure(self.geoid, election_date="2016-11-08", measure_id_local="old"),
            measure(self.geoid, election_date="2024-11-05", measure_id_local="new"),
        ]}, label="m")
        rows = dict(self.conn.execute(
            "SELECT measure_id_local, threshold_required FROM ballot_measure").fetchall())
        self.assertAlmostEqual(rows["old"], 50.0)
        self.assertAlmostEqual(rows["new"], 66.67)

    def test_supplied_threshold_is_not_overwritten(self):
        ingest.load_doc(self.conn, {
            "thresholds": [threshold(threshold_value=60.0)],
            "measures": [measure(self.geoid, threshold_required=66.67,
                                 threshold_basis="registered_voters")],
        }, label="explicit")
        row = self.conn.execute(
            "SELECT threshold_required, threshold_basis FROM ballot_measure").fetchone()
        self.assertAlmostEqual(row["threshold_required"], 66.67)
        self.assertEqual(row["threshold_basis"], "registered_voters")


class RejectionTests(DbTest):
    def setUp(self):
        super().setUp()
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)

    def _errors(self, doc):
        res = ingest.load_doc(self.conn, doc, allow_partial=True, label="bad")
        return res["errors"], res

    def test_fraction_threshold_is_rejected(self):
        """0.6667 and 66.67 both look like two-thirds to a model. Only one is
        usable, and the wrong one makes every margin nonsense."""
        errs, res = self._errors({"thresholds": [threshold(threshold_value=0.6667)]})
        self.assertEqual(res["written"], 0)
        self.assertTrue(any("66.67" in e for e in errs), errs)

    def test_threshold_without_a_cite_is_rejected(self):
        errs, res = self._errors({"thresholds": [threshold(statute_cite=None)]})
        self.assertEqual(res["written"], 0)
        self.assertTrue(any("statute_cite" in e for e in errs), errs)

    def test_percentage_that_contradicts_the_counts_is_rejected(self):
        errs, res = self._errors({"measures": [measure(
            self.geoid, votes_yes=1000, votes_no=1000, pct_yes=75.0)]})
        self.assertEqual(res["written"], 0)
        self.assertTrue(any("does not match" in e for e in errs), errs)

    def test_passed_below_its_own_threshold_is_rejected(self):
        errs, res = self._errors({"measures": [measure(
            self.geoid, outcome="passed", votes_yes=1000, votes_no=1000,
            threshold_required=66.67)]})
        self.assertEqual(res["written"], 0)
        self.assertTrue(any("below" in e for e in errs), errs)

    def test_votes_exceeding_the_total_is_rejected(self):
        errs, res = self._errors({"measures": [measure(
            self.geoid, votes_yes=900, votes_no=900, votes_total=1000)]})
        self.assertEqual(res["written"], 0)
        self.assertTrue(any("exceeds" in e for e in errs), errs)

    def test_non_iso_election_date_is_rejected(self):
        errs, res = self._errors({"measures": [measure(
            self.geoid, election_date="11/05/2024")]})
        self.assertEqual(res["written"], 0)
        self.assertTrue(any("ISO" in e for e in errs), errs)

    def test_barred_tax_with_a_cap_is_rejected(self):
        errs, res = self._errors({"grants": [grant(permitted="no", max_rate=2.0)]})
        self.assertEqual(res["written"], 0)
        self.assertTrue(any("permitted is 'no'" in e for e in errs), errs)

    def test_instrument_from_another_category_is_rejected(self):
        errs, res = self._errors({"grants": [grant(
            category="sales_use", instrument_code="transient_lodging")]})
        self.assertEqual(res["written"], 0)
        self.assertTrue(any("does not belong" in e for e in errs), errs)

    def test_unknown_profile_field_is_rejected(self):
        errs, res = self._errors({"profile": {"state_usps": "OH", "tax_rate": 3}})
        self.assertEqual(res["written"], 0)
        self.assertTrue(any("unknown profile field" in e for e in errs), errs)

    def test_measure_for_an_unseeded_place_is_rejected(self):
        errs, res = self._errors({"measures": [measure("9999999")]})
        self.assertEqual(res["written"], 0)
        self.assertTrue(any("not a seeded jurisdiction" in e for e in errs), errs)

    def test_valid_rows_survive_alongside_rejected_ones(self):
        errs, res = self._errors({
            "thresholds": [threshold(), threshold(threshold_value=0.5,
                                                  measure_class="bond_go")],
        })
        self.assertEqual(res["written"], 1)
        self.assertEqual(len(errs), 1, errs)


class ProductViewTests(DbTest):
    """The views that were structurally empty before this change."""

    def setUp(self):
        super().setUp()
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)

    def test_near_miss_populates_from_one_document(self):
        ingest.load_doc(self.conn, {
            "thresholds": [threshold(threshold_value=60.0)],
            "measures": [measure(self.geoid, votes_yes=1100, votes_no=900)],
        }, label="nm")
        rows = self.conn.execute(
            "SELECT name, pct_yes, margin_vs_threshold FROM v_near_miss").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["margin_vs_threshold"], -5.0)

    def test_headroom_populates_from_a_grant_plus_a_rate(self):
        ingest.load_doc(self.conn, {
            "grants": [grant(max_rate=2.25)],
            "findings": [{
                "geoid": self.geoid, "category": "sales_use",
                "instrument_code": "county_general_sales", "status": "levied",
                "rate_value": 1.25, "rate_unit": "percent", "confidence": "high",
                "source": STATUTE, "source_quote": "1.25 percent",
            }],
        }, label="hr")
        row = self.conn.execute(
            "SELECT levied_rate, headroom FROM v_headroom WHERE geoid=?",
            (self.geoid,)).fetchone()
        self.assertAlmostEqual(row["levied_rate"], 1.25)
        self.assertAlmostEqual(row["headroom"], 1.0)

    def test_live_threshold_returns_one_row_per_rule_version(self):
        ingest.load_doc(self.conn, {"thresholds": [
            threshold(threshold_value=50.0, effective_from="1990-01-01",
                      effective_to="2019-12-31"),
            threshold(threshold_value=66.67, effective_from="2020-01-01"),
        ]}, label="versions")
        rows = self.conn.execute(
            "SELECT threshold_value FROM v_live_threshold").fetchall()
        self.assertEqual([r["threshold_value"] for r in rows], [66.67])
