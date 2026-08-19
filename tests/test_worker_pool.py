"""The worker pool: fan-out, isolation, and failure containment."""

import os
import threading
import time
import unittest
from unittest import mock

from taxdb import db
from tests._db import DbTest


class WorkerPoolTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import store, worker
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.store, self.worker = store, worker
        # Point the collector's own connect() at this test database.
        self._prev_db = os.environ.get("TAX_DATABASE_DB")
        os.environ["TAX_DATABASE_DB"] = self.db_path
        store._schema_done.discard(self.db_path)
        store.apply_schema(self.conn)
        for i in range(12):
            geoid = "39%05d" % (20000 + i)
            self.place(geoid=geoid, name="Town %d" % i, pop=5000 + i)
            self.conn.execute(
                "INSERT INTO work_item (geoid, category, priority, status, updated_at) "
                "VALUES (?,?,?,?,?)", (geoid, "property", 50 - i, "pending", db.now()))
        store.put_many(self.conn, {"workers": "4", "provider": "none",
                                   "delay_seconds": "0", "search_qpm": "0"})
        self.conn.commit()

    def tearDown(self):
        if self._prev_db is None:
            os.environ.pop("TAX_DATABASE_DB", None)
        else:
            os.environ["TAX_DATABASE_DB"] = self._prev_db
        self.store._schema_done.discard(self.db_path)
        super().tearDown()

    def _batch(self, process):
        with mock.patch.object(self.worker, "_process", side_effect=process), \
             mock.patch.object(self.worker.crawl, "client_for",
                               return_value=mock.MagicMock()):
            self.worker._run_batch("burst", 12)

    def test_every_item_is_researched_exactly_once(self):
        seen, lock = [], threading.Lock()

        def process(conn, client, s, run_id, row, slot=0):
            with lock:
                seen.append((row["geoid"], slot))
            time.sleep(0.01)          # let the pool actually overlap
            return 2, 1

        self._batch(process)
        geoids = [g for g, _ in seen]
        self.assertEqual(len(geoids), 12)
        self.assertEqual(len(set(geoids)), 12, "an item was researched twice")

    def test_work_is_spread_across_slots(self):
        slots, lock = set(), threading.Lock()

        def process(conn, client, s, run_id, row, slot=0):
            with lock:
                slots.add(slot)
            time.sleep(0.05)
            return 1, 1

        self._batch(process)
        self.assertGreater(len(slots), 1, "the pool ran single-threaded")
        self.assertLessEqual(len(slots), 4)

    def test_each_worker_gets_its_own_crawlee_directory(self):
        """Crawlee purges its storage on start, so a shared directory would
        have one worker wipe another's request queue mid-run."""
        try:
            from collector import fetcher
        except ImportError as exc:
            self.skipTest("fetcher unavailable: %s" % exc)
        dirs, lock = set(), threading.Lock()

        def process(conn, client, s, run_id, row, slot=0):
            with lock:
                dirs.add(fetcher.storage_dir())
            time.sleep(0.05)
            return 0, 0

        self._batch(process)
        self.assertGreater(len(dirs), 1)
        self.assertEqual(len(dirs), len({d for d in dirs}))

    def test_one_bad_item_does_not_sink_the_batch(self):
        done, lock = [], threading.Lock()

        def process(conn, client, s, run_id, row, slot=0):
            if row["geoid"].endswith("20003"):
                raise RuntimeError("county website exploded")
            with lock:
                done.append(row["geoid"])
            return 1, 1

        self._batch(process)
        self.assertEqual(len(done), 11)
        # The casualty went back to the queue with the reason attached.
        row = self.conn.execute(
            "SELECT status, last_error FROM work_item WHERE geoid='3920003'"
        ).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIn("county website exploded", row["last_error"])
        # And the run is recorded as ok, with the casualty count noted.
        run = self.conn.execute(
            "SELECT status, message, items_claimed FROM crawl_run "
            "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["items_claimed"], 11)
        self.assertIn("errored", run["message"])

    def test_stop_drains_without_finishing_the_batch(self):
        started, lock = [], threading.Lock()

        def process(conn, client, s, run_id, row, slot=0):
            with lock:
                started.append(row["geoid"])
                n = len(started)
            if n >= 2:
                self.worker._cancel.set()
            time.sleep(0.01)
            return 1, 1

        self._batch(process)
        self.assertLess(len(started), 12)
        run = self.conn.execute(
            "SELECT status FROM crawl_run ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(run["status"], "stopped")
        self.assertFalse(self.worker._cancel.is_set(), "cancel flag was not cleared")

    def test_a_stalled_worker_is_abandoned(self):
        """One wedged crawl froze the pool, the run, and every batch tick
        behind it for nine hours overnight. The pool now stops waiting."""
        self.store.put_many(self.conn, {"item_stall_minutes": "0.01"})
        self.conn.commit()
        release = threading.Event()

        def process(conn, client, s, run_id, row, slot=0):
            if row["geoid"].endswith("20000"):
                release.wait(30)      # far past the stall ceiling
            return 1, 0

        t0 = time.monotonic()
        try:
            self._batch(process)
            elapsed = time.monotonic() - t0
            # Read before releasing: the abandoned thread's late bump_run is
            # harmless in production but would shift the counts asserted here.
            run = self.conn.execute(
                "SELECT status, message, items_claimed FROM crawl_run "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            release.set()             # let the abandoned daemon thread go
        self.assertLess(elapsed, 20, "the pool waited on the wedged worker")
        self.assertEqual(run["status"], "ok")
        self.assertIn("stalled", run["message"])
        # The other eleven items were still researched.
        self.assertEqual(run["items_claimed"], 11)
        # No ghost worker lingers in the snapshot.
        self.assertEqual(self.worker.snapshot()["workers"], [])

    def test_restart_closes_orphaned_runs(self):
        run_id = self.store.start_run(self.conn, "continuous", provider="none")
        self.worker._close_orphaned_runs(self.conn)
        row = self.conn.execute(
            "SELECT * FROM crawl_run WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("restarted", row["message"])
        self.assertTrue(row["finished_at"])

    def test_snapshot_reports_each_worker(self):
        seen_counts, lock = [], threading.Lock()

        def process(conn, client, s, run_id, row, slot=0):
            self.worker._slot_set(slot, current_geoid=row["geoid"],
                                  current_name=row["geoid"], step="working")
            time.sleep(0.05)
            with lock:
                seen_counts.append(len(self.worker.snapshot()["workers"]))
            return 1, 1

        self._batch(process)
        self.assertTrue(any(c > 1 for c in seen_counts),
                        "snapshot never showed more than one worker")
        # Slots are released, so an idle pool does not look busy.
        self.assertEqual(self.worker.snapshot()["workers"], [])


if __name__ == "__main__":
    unittest.main()
