"""Welcome screen: shown once per version update, then stamped and gone."""

import os
import shutil
import tempfile
import unittest


class WelcomeFlowTests(unittest.TestCase):
    """Routes through the real app with a throwaway database.

    TestClient is used without a `with` block on purpose: that skips the
    startup event, so the background worker never launches during tests.
    """

    def setUp(self):
        try:
            from fastapi.testclient import TestClient
            from collector import app as appmod
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.tmpdir = tempfile.mkdtemp()
        self._old_db = os.environ.get("TAX_DATABASE_DB")
        os.environ["TAX_DATABASE_DB"] = os.path.join(self.tmpdir, "test.db")
        self.client = TestClient(appmod.app)
        self.version = appmod.__version__

    def tearDown(self):
        if self._old_db is None:
            os.environ.pop("TAX_DATABASE_DB", None)
        else:
            os.environ["TAX_DATABASE_DB"] = self._old_db
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pages_detour_to_welcome_until_acknowledged(self):
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 307)
        self.assertEqual(r.headers["location"], "/welcome")
        # Review is gated too; settings stays reachable as the escape hatch.
        r = self.client.get("/review", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/welcome")
        r = self.client.get("/settings", follow_redirects=False)
        self.assertEqual(r.status_code, 200)

    def test_welcome_renders_with_version_and_news(self):
        r = self.client.get("/welcome")
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.version, r.text)
        self.assertIn("double-check", r.text)

    def test_complete_stamps_and_ungates(self):
        r = self.client.post("/api/setup/complete")
        self.assertEqual(r.json()["version"], self.version)
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 200)

    def test_checker_provider_validated(self):
        r = self.client.post("/api/settings", json={"checker_provider": "bogus"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/settings", json={"checker_provider": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIn("checker_provider", r.json()["updated"])

    def test_checker_test_reports_missing_provider_without_a_call(self):
        # checker follows the extractor here, and the extractor is none.
        self.client.post("/api/settings",
                         json={"provider": "none", "checker_provider": ""})
        r = self.client.post("/api/checker/test")
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertIn("no provider", body["error"])
