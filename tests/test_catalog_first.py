"""Catalog-first crawling: tagged sources seed only their categories, and
an item's first crawl reads the catalog instead of searching the web."""

import unittest
from unittest import mock

from taxdb import db
from tests._db import DbTest


class CatalogFirstTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import crawl, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.crawl = crawl
        self.store = store
        store.apply_schema(self.conn)
        self.geoid = self.place()  # an Ohio place, state_fips 39

    def _tagged_source(self, url, cats, tier=2):
        return db.get_or_create_source(
            self.conn, url, "Tagged source", source_type="bulk_file",
            authority_tier=tier, scope_geoid="39", categories=cats)

    def _settings(self, **kw):
        s = {"web_search": "1", "catalog_first": "1"}
        s.update(kw)
        return s

    def test_tagged_source_seeds_only_its_category(self):
        self._tagged_source("https://tax.example.gov/millage", "property")
        sales = self.crawl.seeds_for(self.conn, self.geoid, "OH",
                                     self._settings(), category="sales_use")
        prop = self.crawl.seeds_for(self.conn, self.geoid, "OH",
                                    self._settings(), category="property")
        self.assertNotIn("https://tax.example.gov/millage", sales)
        self.assertIn("https://tax.example.gov/millage", prop)

    def test_untagged_source_seeds_every_category(self):
        db.get_or_create_source(
            self.conn, "https://agency.example.gov/", "Agency",
            source_type="agency_table", authority_tier=2, scope_geoid="39")
        sales = self.crawl.seeds_for(self.conn, self.geoid, "OH",
                                     self._settings(), category="sales_use")
        self.assertIn("https://agency.example.gov/", sales)

    def test_multi_category_tag_matches_each_listed_category(self):
        self._tagged_source("https://tax.example.gov/edr",
                            "lodging_meals,sales_use")
        for cat in ("lodging_meals", "sales_use"):
            self.assertIn("https://tax.example.gov/edr",
                          self.crawl.seeds_for(self.conn, self.geoid, "OH",
                                               self._settings(), category=cat))
        self.assertNotIn("https://tax.example.gov/edr",
                         self.crawl.seeds_for(self.conn, self.geoid, "OH",
                                              self._settings(),
                                              category="property"))

    def test_first_crawl_with_coverage_skips_the_web_search(self):
        self._tagged_source("https://tax.example.gov/millage", "property")
        with mock.patch.object(self.crawl, "search_web") as sw:
            urls = self.crawl.item_seeds(
                self.conn, None, self._settings(), self.geoid, "property",
                "Testville city", "OH", "place")
        sw.assert_not_called()
        self.assertIn("https://tax.example.gov/millage", urls)

    def test_uncovered_category_still_searches(self):
        self._tagged_source("https://tax.example.gov/millage", "property")
        with mock.patch.object(self.crawl, "search_web",
                               return_value=[]) as sw:
            self.crawl.item_seeds(
                self.conn, None, self._settings(), self.geoid, "sales_use",
                "Testville city", "OH", "place")
        sw.assert_called()

    def test_retry_after_a_crawl_searches_again(self):
        self._tagged_source("https://tax.example.gov/millage", "property")
        self.conn.execute(
            "INSERT INTO crawl_page (geoid, category, url, fetched_at) "
            "VALUES (?,?,?,?)",
            (self.geoid, "property", "https://tax.example.gov/millage",
             db.now()))
        self.conn.commit()
        with mock.patch.object(self.crawl, "search_web",
                               return_value=[]) as sw:
            self.crawl.item_seeds(
                self.conn, None, self._settings(), self.geoid, "property",
                "Testville city", "OH", "place")
        sw.assert_called()

    def test_setting_off_restores_the_old_behavior(self):
        self._tagged_source("https://tax.example.gov/millage", "property")
        with mock.patch.object(self.crawl, "search_web",
                               return_value=[]) as sw:
            self.crawl.item_seeds(
                self.conn, None, self._settings(catalog_first="0"),
                self.geoid, "property", "Testville city", "OH", "place")
        sw.assert_called()

    def test_reseeding_backfills_categories_on_existing_rows(self):
        # Databases seeded before the tags existed get them on the next seed.
        sid = db.get_or_create_source(
            self.conn, "https://tax.example.gov/millage", "Tagged source",
            source_type="bulk_file", authority_tier=2, scope_geoid="39")
        again = db.get_or_create_source(
            self.conn, "https://tax.example.gov/millage", "Tagged source",
            source_type="bulk_file", authority_tier=2, scope_geoid="39",
            categories="property")
        self.assertEqual(sid, again)
        row = self.conn.execute(
            "SELECT categories FROM source WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["categories"], "property")


if __name__ == "__main__":
    unittest.main()
