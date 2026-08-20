"""Which states a statute snapshot actually carries.

The v2026.08 corpus publishes statutes for 50 of 52 jurisdictions. Georgia and
North Carolina have constitutions and guidance but no statute file, and a bare
"HTTP Error 404" gave the autopilot no way to tell that from an outage — so it
chose Georgia every cooldown, failed, and never fetched any other state.
"""

import unittest

from tests._db import DbTest

MANIFEST = {
    "version": "v2026.08",
    "files": [
        {"file": "us_oh_statutes.parquet"},
        {"file": "us_ga_constitutions.parquet"},
        {"file": "us_ga_court_rules.parquet"},
        {"file": "us_ga_guidance.parquet"},
    ],
}


class SnapshotAvailabilityTests(unittest.TestCase):
    def setUp(self):
        try:
            from taxdb import statutes
        except ImportError as exc:
            self.skipTest("deps missing: %s" % exc)
        self.statutes = statutes
        self._saved = dict(statutes._manifest_cache)
        statutes._manifest_cache.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        self.statutes._manifest_cache.clear()
        self.statutes._manifest_cache.update(self._saved)

    def _cache(self, manifest=MANIFEST):
        files = {f["file"] for f in manifest["files"]} if manifest else None
        self.statutes._manifest_cache[self.statutes.SNAPSHOT] = (
            {"files": files, "version": manifest["version"]}
            if manifest else None)

    def test_a_published_state_is_available(self):
        self._cache()
        self.assertTrue(self.statutes.published("OH"))

    def test_a_state_with_no_statute_file_is_not(self):
        self._cache()
        self.assertFalse(self.statutes.published("GA"))

    def test_an_unreadable_manifest_is_not_evidence_of_absence(self):
        """A network hiccup must not look like a missing corpus."""
        self._cache(None)
        self.assertTrue(self.statutes.published("GA"))

    def test_the_error_says_what_the_snapshot_does_have(self):
        self._cache()
        self.assertEqual(self.statutes.other_files("GA"),
                         ["constitutions", "court_rules", "guidance"])

    def test_case_does_not_matter(self):
        self._cache()
        self.assertTrue(self.statutes.published("oh"))
        self.assertFalse(self.statutes.published("ga"))


class FetchRefusalTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from taxdb import statutes
        except ImportError as exc:
            self.skipTest("deps missing: %s" % exc)
        self.statutes = statutes
        self._saved = dict(statutes._manifest_cache)
        statutes._manifest_cache.clear()
        statutes._manifest_cache[statutes.SNAPSHOT] = {
            "files": {f["file"] for f in MANIFEST["files"]},
            "version": MANIFEST["version"]}
        self.addCleanup(self._restore)

    def _restore(self):
        self.statutes._manifest_cache.clear()
        self.statutes._manifest_cache.update(self._saved)

    def test_an_unpublished_state_is_refused_before_any_download(self):
        """Refused locally: no request goes out, so no 404 to interpret."""
        called = []
        real = self.statutes.fetch
        self.statutes.fetch = lambda *a, **kw: called.append(a) or (None, None, b"")
        try:
            with self.assertRaises(self.statutes.NotPublished) as caught:
                self.statutes.fetch_state(self.conn, "GA")
        finally:
            self.statutes.fetch = real
        self.assertEqual(called, [])
        self.assertIn("no statute corpus", str(caught.exception))
        self.assertIn("constitutions", str(caught.exception))

    def test_not_published_is_a_statutes_error(self):
        """Callers that catch the base class keep working."""
        self.assertTrue(issubclass(self.statutes.NotPublished,
                                   self.statutes.StatutesError))


class RunnerTests(DbTest):
    """What the collector does with a state it cannot download."""

    def setUp(self):
        super().setUp()
        try:
            from collector import store, worker
            from taxdb import statutes
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.store, self.worker, self.statutes = store, worker, statutes
        store.apply_schema(self.conn)

    def test_the_marker_key_names_the_snapshot(self):
        key = self.worker.statutes_absent_key("ga")
        self.assertEqual(key, "statutes_absent:%s:GA" % self.statutes.SNAPSHOT)

    def test_a_bare_404_is_recognised_as_a_missing_corpus(self):
        """When the manifest cannot be read, the 404 is the only evidence."""
        import urllib.error
        exc = urllib.error.HTTPError(
            "https://x/us_ga_statutes.parquet", 404, "Not Found", {}, None)
        self.assertTrue(self.worker._looks_like_missing_corpus(exc))

    def test_other_failures_are_not_mistaken_for_it(self):
        for exc in (RuntimeError("connection reset by peer"),
                    RuntimeError("HTTP Error 500: Internal Server Error"),
                    RuntimeError("timed out")):
            self.assertFalse(self.worker._looks_like_missing_corpus(exc), exc)

    def test_an_unpublished_state_is_remembered_and_not_a_failure(self):
        """One run, marked, and the autopilot stops choosing it."""
        import os
        from unittest import mock
        # Point the collector's own connect() at this test database, and put
        # the environment back afterwards so later tests are unaffected.
        previous = os.environ.get("TAX_DATABASE_DB")
        os.environ["TAX_DATABASE_DB"] = self.db_path
        self.addCleanup(lambda: os.environ.__setitem__(
            "TAX_DATABASE_DB", previous) if previous is not None
            else os.environ.pop("TAX_DATABASE_DB", None))
        self.store._schema_done.discard(self.db_path)
        with mock.patch.object(self.statutes, "published", return_value=False), \
             mock.patch.object(self.statutes, "other_files",
                               return_value=["constitutions"]):
            res = self.worker.run_statutes("GA")
        self.assertTrue(res["unavailable"])
        conn = self.store.connect()
        try:
            self.assertEqual(
                self.store.get(conn, self.worker.statutes_absent_key("GA")), "1")
            run = conn.execute("SELECT status, message FROM crawl_run "
                               "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        # Recorded as a finished run, not a red failure: nothing is broken.
        self.assertEqual(run["status"], "ok")
        self.assertIn("no statute corpus", run["message"])


if __name__ == "__main__":
    unittest.main()
