"""Unattended operation: what to do next, and when to stop trying.

The failure this guards against is the original one: continuous mode claiming
an empty queue, logging "queue empty", and sleeping forever while the operator
believes the country is being worked through.
"""

from taxdb import db, ledger
from taxdb.vocab import ELECTIONS, FRAMEWORK
from tests._db import DbTest


class AutopilotTest(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import autopilot, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.autopilot = autopilot
        self.store = store
        store.apply_schema(self.conn)
        self.settings = dict(store.get_all(self.conn))
        # Network stages are off unless a test turns them on.
        self.settings.update({"autopilot_bulk": "0", "autopilot_statutes": "0"})

    def _states(self):
        for fips, usps, name in (("39", "OH", "Ohio"), ("53", "WA", "Washington")):
            self.conn.execute(
                "INSERT OR REPLACE INTO jurisdiction (geoid, kind, name, state_usps, "
                "state_fips, funcstat) VALUES (?,?,?,?,?,'A')",
                (fips, "state", name, usps, fips))
            self.conn.execute(
                "INSERT OR IGNORE INTO state_profile (state_usps, state_name) "
                "VALUES (?,?)", (usps, name))
        self.conn.commit()

    def _locals(self):
        rows = (("39049", "county", "Franklin County", "OH", "39", 1300000),
                ("53033", "county", "King County", "WA", "53", 2200000),
                ("3918000", "place", "Columbus city", "OH", "39", 900000))
        for geoid, kind, name, usps, fips, pop in rows:
            self.conn.execute(
                "INSERT OR REPLACE INTO jurisdiction (geoid, kind, name, state_usps, "
                "state_fips, county_fips, funcstat, population) VALUES (?,?,?,?,?,?,'A',?)",
                (geoid, kind, name, usps, fips,
                 geoid if kind == "county" else None, pop))
        self.conn.commit()

    def _next(self):
        return self.autopilot.next_action(self.conn, self.settings)

    def test_empty_database_seeds_first(self):
        action, _, label = self._next()
        self.assertEqual(action, self.autopilot.SEED)
        self.assertNotIn("geoid", label.lower())

    def test_source_catalog_comes_before_any_crawling(self):
        self._states()
        self._locals()
        action, _, _ = self._next()
        self.assertEqual(action, self.autopilot.SOURCES)

    def test_state_framework_is_planned_before_local_work(self):
        """51 framework items gate thresholds and caps for 22,000
        jurisdictions, so they are worth more per call than any single city."""
        self._states()
        self._locals()
        self.source(url="https://tax.example.gov/")
        action, kwargs, _ = self._next()
        self.assertEqual(action, self.autopilot.PLAN_FRAMEWORK)
        self.assertEqual(sorted(kwargs["states"]), ["OH", "WA"])

    def test_expansion_follows_the_framework(self):
        self._states()
        self._locals()
        self.source(url="https://tax.example.gov/")
        ledger.plan(self.conn, states=["OH", "WA"], kinds=("state",),
                    categories=[FRAMEWORK])
        action, kwargs, _ = self._next()
        self.assertEqual(action, self.autopilot.EXPAND)
        # Most populous first: King County before Franklin before Columbus.
        self.assertEqual(kwargs["geoids"], ["53033", "39049", "3918000"])

    def test_expansion_plans_elections_only_for_counties(self):
        """Canvasses are published by counties. Planning an elections pass for
        every city would triple the queue and find nothing."""
        self._states()
        self._locals()
        self.autopilot.expand(self.conn, ["39049", "3918000"], self.settings)
        rows = dict(self.conn.execute(
            "SELECT geoid, COUNT(*) FROM work_item WHERE category=? GROUP BY geoid",
            (ELECTIONS,)).fetchall())
        self.assertEqual(sorted(rows), ["39049"])

    def test_elections_pass_can_be_turned_off(self):
        self._states()
        self._locals()
        self.settings["autopilot_elections"] = "0"
        self.autopilot.expand(self.conn, ["39049"], self.settings)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM work_item WHERE category=?",
            (ELECTIONS,)).fetchone()["c"]
        self.assertEqual(n, 0)

    def test_nothing_left_to_do_returns_none(self):
        self._states()
        self._locals()
        self.source(url="https://tax.example.gov/")
        ledger.plan(self.conn, states=["OH", "WA"], kinds=("state",),
                    categories=[FRAMEWORK])
        self.autopilot.expand(self.conn, ["39049", "53033", "3918000"], self.settings)
        self.assertIsNone(self._next())

    def test_stale_records_are_offered_for_a_refresh(self):
        self._states()
        self._locals()
        self.source(url="https://tax.example.gov/")
        ledger.plan(self.conn, states=["OH", "WA"], kinds=("state",),
                    categories=[FRAMEWORK])
        self.autopilot.expand(self.conn, ["39049", "53033", "3918000"], self.settings)
        self.conn.execute(
            "UPDATE work_item SET status='complete', "
            "completed_at=datetime('now','-400 days')")
        self.conn.commit()
        action, kwargs, _ = self._next()
        self.assertEqual(action, self.autopilot.REFRESH)
        self.assertEqual(kwargs["days"], 365)

    def test_refresh_can_be_turned_off(self):
        self._states()
        self._locals()
        self.source(url="https://tax.example.gov/")
        ledger.plan(self.conn, states=["OH", "WA"], kinds=("state",),
                    categories=[FRAMEWORK])
        self.autopilot.expand(self.conn, ["39049", "53033", "3918000"], self.settings)
        self.conn.execute(
            "UPDATE work_item SET status='complete', "
            "completed_at=datetime('now','-400 days')")
        self.conn.commit()
        self.settings["refresh_days"] = "0"
        self.assertIsNone(self._next())

    def test_a_failed_bulk_download_does_not_spin_the_loop(self):
        """The cooldown marker is written before the attempt, so a download
        that fails every time cannot become an infinite retry."""
        self._states()
        self._locals()
        self.source(url="https://tax.example.gov/")
        self.settings["autopilot_bulk"] = "1"
        action, _, _ = self._next()
        self.assertEqual(action, self.autopilot.SST)
        self.autopilot.mark_tried(self.conn, self.autopilot.SST)
        action, _, _ = self._next()
        self.assertNotEqual(action, self.autopilot.SST)

    def test_state_filter_is_honoured(self):
        self._states()
        self._locals()
        self.source(url="https://tax.example.gov/")
        self.settings["filter_states"] = "WA"
        action, kwargs, _ = self._next()
        self.assertEqual(action, self.autopilot.PLAN_FRAMEWORK)
        self.assertEqual(kwargs["states"], ["WA"])

    def test_progress_is_reported_as_population_coverage(self):
        self._states()
        self._locals()
        self.autopilot.expand(self.conn, ["39049", "53033", "3918000"], self.settings)
        self.conn.execute(
            "UPDATE work_item SET status='complete' WHERE geoid='53033'")
        self.conn.commit()
        p = self.autopilot.progress(self.conn, self.settings)
        self.assertEqual(p["juris_total"], 3)
        self.assertEqual(p["juris_done"], 1)
        # King County is 2.2M of a 4.4M total.
        self.assertAlmostEqual(p["pop_pct"], 50.0)
