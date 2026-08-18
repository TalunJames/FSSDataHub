"""Work ledger: research passes, attempt ceilings, and refresh."""

from taxdb import db, ledger
from taxdb.vocab import ELECTIONS, FRAMEWORK
from tests._db import DbTest


class PlanPassTests(DbTest):
    def setUp(self):
        super().setUp()
        self.place(geoid="39049", name="Franklin County", kind="county", pop=1300000)
        self.place(geoid="3918000", name="Columbus city", kind="place", pop=900000)

    def test_framework_is_planned_against_states_only(self):
        ledger.plan(self.conn, states=["OH"], kinds=("state", "county", "place"),
                    categories=[FRAMEWORK])
        rows = self.conn.execute(
            "SELECT w.geoid, j.kind FROM work_item w JOIN jurisdiction j "
            "ON j.geoid=w.geoid WHERE w.category=?", (FRAMEWORK,)).fetchall()
        self.assertEqual([(r["geoid"], r["kind"]) for r in rows], [("39", "state")])

    def test_elections_is_planned_against_counties_only(self):
        ledger.plan(self.conn, states=["OH"], kinds=("state", "county", "place"),
                    categories=[ELECTIONS])
        rows = self.conn.execute(
            "SELECT geoid FROM work_item WHERE category=?", (ELECTIONS,)).fetchall()
        self.assertEqual([r["geoid"] for r in rows], ["39049"])

    def test_tax_categories_are_unaffected(self):
        ledger.plan(self.conn, states=["OH"], kinds=("county", "place"),
                    categories=["sales_use"])
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM work_item WHERE category='sales_use'").fetchone()["c"]
        self.assertEqual(n, 2)

    def test_unknown_category_still_refused(self):
        with self.assertRaises(SystemExit):
            ledger.plan(self.conn, states=["OH"], categories=["nonsense"])

    def test_states_missing_pass_shrinks_once_planned(self):
        self.assertEqual(ledger.states_missing_pass(self.conn, FRAMEWORK), ["OH"])
        ledger.plan(self.conn, states=["OH"], kinds=("state",), categories=[FRAMEWORK])
        self.assertEqual(ledger.states_missing_pass(self.conn, FRAMEWORK), [])


class AttemptCeilingTests(DbTest):
    def setUp(self):
        super().setUp()
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)
        ledger.plan(self.conn, states=["OH"], kinds=("county",),
                    categories=["sales_use"])

    def _attempts(self, n):
        self.conn.execute("UPDATE work_item SET attempts=?", (n,))
        self.conn.commit()

    def test_item_under_the_ceiling_is_still_claimed(self):
        self._attempts(2)
        rows = ledger.claim(self.conn, limit=5, max_attempts=4)
        self.assertEqual(len(rows), 1)

    def test_item_at_the_ceiling_is_not_claimed(self):
        """A place whose rate page cannot be found used to recycle forever at
        the head of a population-weighted queue, starving everything else."""
        self._attempts(4)
        rows = ledger.claim(self.conn, limit=5, max_attempts=4)
        self.assertEqual(rows, [])

    def test_exhausted_items_are_parked_visibly(self):
        self._attempts(4)
        ledger.claim(self.conn, limit=5, max_attempts=4)
        row = self.conn.execute(
            "SELECT status, last_error FROM work_item").fetchone()
        self.assertEqual(row["status"], "blocked")
        self.assertIn("gave up", row["last_error"])

    def test_no_ceiling_keeps_the_old_behaviour(self):
        self._attempts(99)
        rows = ledger.claim(self.conn, limit=5)
        self.assertEqual(len(rows), 1)


class RefreshTests(DbTest):
    def setUp(self):
        super().setUp()
        self.geoid = self.place(geoid="39049", name="Franklin County",
                                kind="county", pop=1300000)
        ledger.plan(self.conn, states=["OH"], kinds=("county",),
                    categories=["sales_use"])

    def _complete(self, days_ago):
        self.conn.execute(
            "UPDATE work_item SET status='complete', attempts=3, "
            "completed_at=datetime('now', ?)", ("-%d days" % days_ago,))
        self.conn.commit()

    def test_stale_item_returns_to_pending(self):
        self._complete(400)
        self.assertEqual(ledger.requeue_stale(self.conn, days=365), 1)
        row = self.conn.execute(
            "SELECT status, attempts, last_error FROM work_item").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0, "a refresh is a fresh look, not a retry")
        self.assertIn("refresh", row["last_error"])

    def test_recent_item_is_left_alone(self):
        self._complete(30)
        self.assertEqual(ledger.requeue_stale(self.conn, days=365), 0)
        row = self.conn.execute("SELECT status FROM work_item").fetchone()
        self.assertEqual(row["status"], "complete")

    def test_unplanned_lists_the_biggest_places_first(self):
        self.place(geoid="3918000", name="Columbus city", kind="place", pop=900000)
        self.place(geoid="3915000", name="Cleveland city", kind="place", pop=370000)
        rows = ledger.unplanned(self.conn, ["sales_use"], kinds=("county", "place"))
        self.assertEqual([r["geoid"] for r in rows], ["3918000", "3915000"])
