"""Shared test database."""

import os
import tempfile
import shutil
import unittest

from taxdb import db


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._archive = db.ARCHIVE_DIR
        db.ARCHIVE_DIR = os.path.join(self.tmpdir, "archive")
        os.makedirs(db.ARCHIVE_DIR)
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.conn = db.init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        db.ARCHIVE_DIR = self._archive
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def source(self, url="https://example.test/statute", name="Test statute",
               source_type="statute", tier=1):
        return db.get_or_create_source(
            self.conn, url, name, source_type=source_type, authority_tier=tier)

    def place(self, geoid="3912345", name="Testville city", state="OH",
              fips="39", kind="place", pop=50000):
        self.conn.execute(
            "INSERT OR IGNORE INTO jurisdiction "
            "(geoid, kind, name, state_usps, state_fips, parent_geoid, funcstat, population) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (fips, "state", "Ohio" if state == "OH" else state, state, fips, None, "A", None))
        self.conn.execute(
            "INSERT OR REPLACE INTO jurisdiction "
            "(geoid, kind, name, state_usps, state_fips, parent_geoid, funcstat, population) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (geoid, kind, name, state, fips, fips, "A", pop))
        self.conn.execute(
            "INSERT OR IGNORE INTO state_profile (state_usps, state_name) VALUES (?,?)",
            (state, "Ohio" if state == "OH" else state))
        self.conn.commit()
        return geoid
