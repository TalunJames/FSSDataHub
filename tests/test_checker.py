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

    def test_reimbursement_schedule_is_hard_flagged(self):
        doc = "Maximum lodging reimbursement rate NY City 342.00 23.00 69.00"
        flags = self.check.deterministic_flags(
            [_finding_row(label="transient lodging maximum reimbursement rate",
                          source_quote="NY City 342.00 23.00 69.00")], doc)
        codes = [f["code"] for f in flags]
        self.assertIn("not_a_tax", codes)
        hard = [f for f in flags if f["code"] == "not_a_tax"]
        advice = self.check.hard_advice(hard)
        self.assertEqual(advice["lean"], "try_again")
        self.assertIn("per-diem", advice["hint"])

    def test_per_diem_quote_is_hard_flagged(self):
        doc = "Meals per diem for Travis County is $59"
        flags = self.check.deterministic_flags(
            [_finding_row(label=None,
                          source_quote="Meals per diem for Travis County is $59")],
            doc)
        self.assertIn("not_a_tax", [f["code"] for f in flags])

    def test_ordinary_tax_wording_is_not_flagged_as_reimbursement(self):
        doc = "Rates page. The county sales tax rate is 1.5% effective 2024."
        flags = self.check.deterministic_flags(
            [_finding_row(label="county general sales tax")], doc)
        self.assertNotIn("not_a_tax", [f["code"] for f in flags])

    def test_implausible_mills_flagged(self):
        doc = "the levy is 9000 mills"
        flags = self.check.deterministic_flags(
            [_finding_row(rate_value=9000, rate_unit="mills",
                          source_quote="the levy is 9000 mills")], doc)
        self.assertIn("implausible_rate", [f["code"] for f in flags])

    def test_bulk_row_without_a_quote_is_not_flagged(self):
        # A published rate file is a spreadsheet. There is nothing to quote,
        # and demanding a quote of it queues every adapter row for a human.
        flags = self.check.deterministic_flags(
            [_finding_row(source_quote="", extraction_method="bulk_import")],
            "whatever")
        self.assertEqual(flags, [])

    def test_hard_and_soft_flags_are_separated(self):
        doc = "the county sales tax rate is 1.5%"
        flags = self.check.deterministic_flags(
            [_finding_row(confidence="low", rate_value=9000, rate_unit="mills",
                          source_quote="the levy is 9000 mills")], doc)
        self.assertEqual([f["code"] for f in self.check.hard_only(flags)],
                         ["implausible_rate"])
        self.assertEqual(sorted(f["code"] for f in self.check.soft_only(flags)),
                         ["low_confidence", "quote_missing"])

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


class QualityFlagTests(unittest.TestCase):
    """The three extraction errors that kept reaching the database.

    All three were being caught, expensively and inconsistently, by the AI
    checker reading the whole document. They are mechanical.
    """

    def setUp(self):
        try:
            from collector import check
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.check = check

    def _flags(self, row, doc="", place=None):
        return [f["code"] for f in
                self.check.deterministic_flags([row], doc, place=place)]

    # ------------------------------------------------------- cross-references
    def test_borough_pointer_is_flagged(self):
        """'*Manhattan - see New York City' is not Manhattan's rate."""
        quote = "*Manhattan - see New York City"
        self.assertIn("cross_reference",
                      self._flags(_finding_row(source_quote=quote), quote))

    def test_prose_containing_see_is_not_flagged(self):
        quote = "As you can see the county rate is 1.5%"
        self.assertNotIn("cross_reference",
                         self._flags(_finding_row(source_quote=quote), quote))

    def test_tennessee_does_not_look_like_a_pointer(self):
        quote = "Tennessee counties may levy 2.75%"
        self.assertNotIn("cross_reference",
                         self._flags(_finding_row(source_quote=quote), quote))

    # ---------------------------------------------------------- combined rate
    def _sales(self, **kw):
        place = {"state_usps": "MI", "kind": "county", "category": "sales_use",
                 "state_rate": 6.0}
        place.update(kw)
        return place

    def test_local_rate_at_the_state_rate_is_flagged(self):
        """6% recorded for Wayne County is Michigan's own statewide rate."""
        codes = self._flags(_finding_row(rate_value=6.0), place=self._sales())
        self.assertIn("combined_rate", codes)

    def test_a_normal_local_add_on_is_not_flagged(self):
        codes = self._flags(_finding_row(rate_value=0.5), place=self._sales())
        self.assertNotIn("combined_rate", codes)

    def test_no_state_row_on_file_means_no_guess(self):
        """The comparison rate is read from the database or not used at all."""
        codes = self._flags(_finding_row(rate_value=9.75),
                            place=self._sales(state_rate=None))
        self.assertNotIn("combined_rate", codes)

    def test_non_percent_units_are_left_alone(self):
        codes = self._flags(
            _finding_row(rate_value=40.0, rate_unit="mills"),
            place=self._sales(state_rate=5.75))
        self.assertNotIn("combined_rate", codes)

    def test_lodging_taxes_are_not_compared_to_the_state_rate(self):
        """A county lodging tax is routinely several times its state's.

        Only sales rate tables publish a combined state-plus-local figure, so
        only there does a local rate at the state's rate mean anything.
        """
        codes = self._flags(
            _finding_row(rate_value=8.0, instrument_code="hotel_motel"),
            place=self._sales(category="lodging_meals", state_rate=1.0))
        self.assertNotIn("combined_rate", codes)

    # ------------------------------------------------------ wrong-state source
    def test_source_from_another_states_site_is_a_hard_flag(self):
        """Orange County NC was twice recorded as Orange County CA."""
        place = {"state_usps": "CA", "kind": "county", "state_rate": None}
        flags = self.check.deterministic_flags(
            [_finding_row(url="https://www.dor.nc.gov/taxes/lodging")],
            "", place=place)
        hard = [f for f in flags if f["code"] == "wrong_state_source"]
        self.assertEqual(len(hard), 1, flags)
        self.assertTrue(hard[0]["hard"])

    def test_own_states_site_is_fine(self):
        place = {"state_usps": "CA", "kind": "county", "state_rate": None}
        codes = self._flags(
            _finding_row(url="https://cdtfa.ca.gov/rates"), place=place)
        self.assertNotIn("wrong_state_source", codes)

    def test_a_state_that_does_not_use_its_usps_code_is_not_flagged(self):
        """Massachusetts is mass.gov, so there is nothing to read here."""
        place = {"state_usps": "CA", "kind": "county", "state_rate": None}
        codes = self._flags(
            _finding_row(url="https://www.mass.gov/rates"), place=place)
        self.assertNotIn("wrong_state_source", codes)

    def test_veterans_affairs_is_not_virginia(self):
        """va.gov is a federal department; Virginia uses virginia.gov."""
        place = {"state_usps": "CA", "kind": "county", "state_rate": None}
        codes = self._flags(
            _finding_row(url="https://www.va.gov/exemptions"), place=place)
        self.assertNotIn("wrong_state_source", codes)

    def test_bulk_rows_are_exempt(self):
        """An adapter's rate file lives wherever it is published."""
        place = {"state_usps": "CA", "kind": "county", "state_rate": None}
        codes = self._flags(
            _finding_row(url="https://www.dor.nc.gov/f.csv",
                         extraction_method="bulk_import"), place=place)
        self.assertNotIn("wrong_state_source", codes)

    def test_no_place_context_disables_the_outside_checks(self):
        """Callers that pass rows alone keep the old behaviour."""
        codes = self._flags(
            _finding_row(rate_value=6.0, url="https://www.dor.nc.gov/x"))
        self.assertNotIn("wrong_state_source", codes)
        self.assertNotIn("combined_rate", codes)


class PlaceContextTests(DbTest):
    """What the checker is told about the jurisdiction it is judging."""

    def setUp(self):
        super().setUp()
        try:
            from collector import check, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.check = check
        store.apply_schema(self.conn)

    def test_state_rate_comes_from_the_states_own_row(self):
        self.place(geoid="26", name="Michigan", state="MI", kind="state")
        county = self.place(geoid="26163", name="Wayne County", state="MI",
                            kind="county")
        sid = self.source()
        self.conn.execute(
            "INSERT INTO tax_instrument (geoid, category, instrument_code, "
            "status, rate_value, rate_unit, source_id, confidence, "
            "extraction_method, retrieved_at) VALUES "
            "('26','sales_use','state_general_sales','levied',6.0,'percent',"
            "?,'high','manual',?)", (sid, db.now()))
        self.conn.commit()
        ctx = self.check.place_context(self.conn, county, "sales_use")
        self.assertEqual(ctx["state_rate"], 6.0)
        self.assertEqual(ctx["kind"], "county")
        self.assertEqual(ctx["category"], "sales_use")
        self.assertEqual(ctx["state_usps"], "MI")

    def test_no_state_row_means_no_rate(self):
        county = self.place(geoid="26163", name="Wayne County", state="MI",
                            kind="county")
        ctx = self.check.place_context(self.conn, county, "sales_use")
        self.assertIsNone(ctx["state_rate"])

    def test_a_state_is_not_compared_against_itself(self):
        state = self.place(geoid="26", name="Michigan", state="MI",
                           kind="state")
        ctx = self.check.place_context(self.conn, state, "sales_use")
        self.assertIsNone(ctx["state_rate"])

    def test_unknown_geoid_is_empty(self):
        self.assertEqual(self.check.place_context(self.conn, "99999", "x"), {})


class ExcerptTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import check
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.checkmod = check

    def test_long_doc_is_trimmed_around_the_quote(self):
        quote = "the sales tax rate is 1.5 percent"
        doc = ("nav " * 8000) + quote + (" footer" * 8000)
        rows = [{"source_quote": quote}]
        out = self.checkmod.excerpt_doc(doc, rows, max_chars=80000)
        self.assertIn(quote, out)
        self.assertLess(len(out), 12000)

    def test_short_doc_passes_through(self):
        doc = "short page mentioning a rate"
        out = self.checkmod.excerpt_doc(doc, [{"source_quote": "rate"}], 80000)
        self.assertEqual(out, doc)

    def test_no_anchor_falls_back_to_the_head(self):
        doc = "x" * 100000
        out = self.checkmod.excerpt_doc(doc, [{"source_quote": "absent"}], 20000)
        self.assertEqual(out, doc[:20000])


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

    def test_empty_verdicts_array_fails_toward_review(self):
        """{"verdicts": []} is parseable and says nothing about the finding.
        Treating it as a clean pass was the trust inversion the design
        forbids — a small local model drifts to shapes like this."""
        raw = json.dumps({"verdicts": []})
        with mock.patch("collector.extract.chat", return_value=(raw, None)):
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        self.assertEqual(verdict, "error")
        self.assertEqual(self._status()["status"], "needs_review")

    def test_wrong_shape_fails_toward_review(self):
        raw = json.dumps({"results": [{"verdict": "pass"}]})
        with mock.patch("collector.extract.chat", return_value=(raw, None)):
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        self.assertEqual(verdict, "error")
        self.assertEqual(self._status()["status"], "needs_review")

    def test_nonstandard_verdict_word_counts_as_flag(self):
        raw = json.dumps({"verdicts": [
            {"instrument_code": "municipal_general_sales",
             "verdict": "flagged", "reason": "unsure"}]})
        with mock.patch("collector.extract.chat", return_value=(raw, None)):
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        self.assertEqual(verdict, "flag")
        self.assertEqual(self._status()["status"], "needs_review")

    def test_no_provider_fails_toward_review(self):
        # checker_provider="" follows the extractor, so provider=none means
        # the checker has nobody to run on either.
        verdict, _ = self.checkmod.run_and_apply(
            self.conn, self._settings(provider="none", checker_provider=""),
            None, self.geoid, "sales_use", self.doc_text)
        self.assertEqual(verdict, "error")
        self.assertEqual(self._status()["status"], "needs_review")

    def test_checker_runs_on_its_own_provider(self):
        """The second pass defaults to the free local model.

        Extraction stays on the paid API; the checker call must carry its own
        provider so the same settings dict routes the two passes differently."""
        raw = json.dumps({"verdicts": [
            {"instrument_code": "municipal_general_sales", "verdict": "pass",
             "reason": ""}]})
        with mock.patch("collector.extract.chat", return_value=(raw, None)) as chat:
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        self.assertEqual(verdict, "pass")
        self.assertEqual(chat.call_args.kwargs["provider"], "llama")
        row = self.conn.execute(
            "SELECT provider, model FROM check_result").fetchone()
        self.assertEqual(row["provider"], "llama")
        self.assertEqual(row["model"], "qwen3-fast")

    def test_empty_checker_provider_follows_the_extractor(self):
        raw = json.dumps({"verdicts": []})
        with mock.patch("collector.extract.chat", return_value=(raw, None)) as chat:
            self.checkmod.run_and_apply(
                self.conn, self._settings(checker_provider=""), None,
                self.geoid, "sales_use", self.doc_text)
        self.assertEqual(chat.call_args.kwargs["provider"], "anthropic")

    def test_checker_model_still_overrides(self):
        raw = json.dumps({"verdicts": []})
        s = self._settings(checker_provider="llama", checker_model="llama3.2")
        with mock.patch("collector.extract.chat", return_value=(raw, None)) as chat:
            self.checkmod.run_and_apply(
                self.conn, s, None, self.geoid, "sales_use", self.doc_text)
        self.assertEqual(chat.call_args.kwargs["model"], "llama3.2")
        row = self.conn.execute("SELECT model FROM check_result").fetchone()
        self.assertEqual(row["model"], "llama3.2")

    def test_disabled_leaves_status_alone(self):
        verdict, _ = self.checkmod.run_and_apply(
            self.conn, self._settings(checker_enabled="0"), None, self.geoid,
            "sales_use", self.doc_text)
        self.assertEqual(verdict, "off")
        self.assertEqual(self._status()["status"], "needs_review")

    def test_hard_flag_beats_model_pass_and_skips_the_call(self):
        """A contradiction is not a judgement call.

        A rate above its own recorded cap cannot be explained away, so it goes
        to a human without spending a checker call on a second opinion."""
        self.conn.execute(
            "UPDATE tax_instrument SET cap_value=1.0, cap_unit='percent' "
            "WHERE geoid=? AND category=?", (self.geoid, "sales_use"))
        self.conn.commit()
        with mock.patch("collector.extract.chat") as chat:
            verdict, message = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                self.doc_text)
        chat.assert_not_called()
        self.assertEqual(verdict, "flag")
        self.assertEqual(self._status()["status"], "needs_review")
        self.assertIn("above its own recorded cap", message)

    def test_soft_flag_alone_is_cleared_by_the_model(self):
        """The mechanical checks raise concerns; the model rules on them.

        An exact quote miss is weak evidence — PDF extraction mangles
        whitespace routinely — so a model that read the documents and was not
        troubled files the row instead of queueing it for a human."""
        raw = json.dumps({"verdicts": [
            {"instrument_code": "municipal_general_sales", "verdict": "pass",
             "reason": ""}]})
        with mock.patch("collector.extract.chat", return_value=(raw, None)) as chat:
            verdict, message = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                "totally unrelated page text")
        self.assertEqual(verdict, "pass")
        self.assertEqual(self._status()["status"], "complete")
        self.assertIn("judged immaterial", message)
        # The concern was handed to the model rather than hidden from it.
        self.assertIn("Automated concerns", chat.call_args[0][1])

    def test_soft_flags_ride_along_when_the_model_also_flags(self):
        raw = json.dumps({"verdicts": [
            {"instrument_code": "municipal_general_sales", "verdict": "flag",
             "reason": "this is the combined rate"}]})
        with mock.patch("collector.extract.chat", return_value=(raw, None)):
            verdict, _ = self.checkmod.run_and_apply(
                self.conn, self._settings(), None, self.geoid, "sales_use",
                "totally unrelated page text")
        self.assertEqual(verdict, "flag")
        codes = {f["code"] for f in json.loads(
            self.conn.execute(
                "SELECT flags FROM check_result").fetchone()["flags"])}
        self.assertEqual(codes, {"ai_flag", "quote_missing"})


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

    def test_an_old_batch_table_gains_the_empty_counter(self):
        """A rebuild that copies positionally breaks when columns are added.

        The 'unconfirmed' rebuild used INSERT ... SELECT *, so the moment
        n_empty was retrofitted first, the oldest databases crashed on
        startup with "table has 12 columns but 13 values were supplied".
        """
        self.conn.executescript("""
            CREATE TABLE extract_batch (
                id INTEGER PRIMARY KEY AUTOINCREMENT, remote_id TEXT UNIQUE,
                provider TEXT NOT NULL, model TEXT,
                status TEXT NOT NULL CHECK (status IN (
                    'building','submitted','ended','collected','failed')),
                n_items INTEGER NOT NULL DEFAULT 0,
                n_succeeded INTEGER NOT NULL DEFAULT 0,
                n_failed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, submitted_at TEXT,
                collected_at TEXT, message TEXT);
        """)
        self.conn.execute(
            "INSERT INTO extract_batch (provider, status, n_items, "
            "n_succeeded, n_failed, created_at) VALUES (?,?,?,?,?,?)",
            ("anthropic", "collected", 5, 2, 3, db.now()))
        self.conn.commit()
        self.store.apply_schema(self.conn)
        row = self.conn.execute(
            "SELECT n_items, n_succeeded, n_failed, n_empty "
            "FROM extract_batch").fetchone()
        self.assertEqual(tuple(row), (5, 2, 3, 0))
        # The rebuild's own purpose still works.
        self.conn.execute(
            "INSERT INTO extract_batch (provider, status, created_at) "
            "VALUES ('anthropic','unconfirmed',?)", (db.now(),))
        self.conn.commit()

    def test_an_old_search_log_gains_the_results_column(self):
        self.conn.executescript("""
            CREATE TABLE search_query (
                id INTEGER PRIMARY KEY AUTOINCREMENT, geoid TEXT NOT NULL,
                category TEXT NOT NULL, query TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'built_in',
                tries INTEGER NOT NULL DEFAULT 0, last_outcome TEXT,
                last_kept INTEGER, created_at TEXT NOT NULL,
                last_tried_at TEXT, UNIQUE(geoid, category, query));
        """)
        self.conn.execute(
            "INSERT INTO search_query (geoid, category, query, created_at) "
            "VALUES ('39049','sales_use','q',?)", (db.now(),))
        self.conn.commit()
        self.store.apply_schema(self.conn)
        row = self.conn.execute(
            "SELECT query, results FROM search_query").fetchone()
        self.assertEqual(row["query"], "q")
        self.assertIsNone(row["results"])

    def test_unused_default_model_upgraded_once(self):
        """The old seeded reader default converges on the current default,
        once, and a deliberate later choice is never touched."""
        self.store.apply_schema(self.conn)
        self.conn.execute(
            "UPDATE collector_setting SET value='claude-sonnet-5' "
            "WHERE key='anthropic_model'")
        self.conn.execute(
            "DELETE FROM collector_setting "
            "WHERE key='anthropic_model_haiku_migrated'")
        self.conn.commit()
        self.store.apply_schema(self.conn)
        s = self.store.get_all(self.conn)
        self.assertEqual(s["anthropic_model"], "claude-haiku-4-5")
        # Second run must not overwrite a deliberate later choice.
        self.store.put(self.conn, "anthropic_model", "claude-sonnet-5")
        self.conn.commit()
        self.store.apply_schema(self.conn)
        s = self.store.get_all(self.conn)
        self.assertEqual(s["anthropic_model"], "claude-sonnet-5")

    def test_seeded_llama_model_upgraded_once(self):
        """The old seeded llama3.1 becomes the checker default, once.

        Before checker_provider existed the llama settings sat unused, so a
        stored llama3.1 is leftover seeding, not a choice. A hand-set model,
        or llama3.1 re-chosen after the upgrade, is never touched."""
        self.store.apply_schema(self.conn)
        self.conn.execute(
            "UPDATE collector_setting SET value='llama3.1' WHERE key='llama_model'")
        self.conn.execute(
            "DELETE FROM collector_setting WHERE key='llama_model_migrated'")
        self.conn.commit()
        self.store.apply_schema(self.conn)
        self.assertEqual(self.store.get(self.conn, "llama_model"), "qwen3-fast")
        # Choosing llama3.1 again afterwards sticks.
        self.store.put(self.conn, "llama_model", "llama3.1")
        self.conn.commit()
        self.store.apply_schema(self.conn)
        self.assertEqual(self.store.get(self.conn, "llama_model"), "llama3.1")

    def test_hand_set_llama_model_not_upgraded(self):
        self.store.apply_schema(self.conn)
        self.conn.execute(
            "UPDATE collector_setting SET value='mistral' WHERE key='llama_model'")
        self.conn.execute(
            "DELETE FROM collector_setting WHERE key='llama_model_migrated'")
        self.conn.commit()
        self.store.apply_schema(self.conn)
        self.assertEqual(self.store.get(self.conn, "llama_model"), "mistral")

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
