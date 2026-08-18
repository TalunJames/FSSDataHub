import unittest

from taxdb.fips import (
    FIPS_SKIPS, FIPS_GID_ORDER, gid_crosswalk_rows,
    census_state_to_fips, fips_to_census_state,
)
from tests._db import DbTest


class FipsTests(unittest.TestCase):
    def test_skips_are_real_gaps(self):
        self.assertEqual(FIPS_SKIPS, ("03", "07", "14", "43", "52"))
        for skip in FIPS_SKIPS:
            self.assertNotIn(skip, FIPS_GID_ORDER)

    def test_al_ak_agree_az_diverges(self):
        rows = {r["usps"]: r for r in gid_crosswalk_rows()}
        self.assertEqual(rows["AL"]["fips_state"], rows["AL"]["census_state"])
        self.assertEqual(rows["AK"]["fips_state"], rows["AK"]["census_state"])
        self.assertEqual(rows["AZ"]["fips_state"], "04")
        self.assertEqual(rows["AZ"]["census_state"], "03")
        self.assertNotEqual(rows["CA"]["fips_state"], rows["CA"]["census_state"])
        self.assertEqual(rows["WA"]["fips_state"], "53")
        self.assertEqual(rows["WA"]["census_state"], "48")

    def test_round_trip(self):
        for fips in FIPS_GID_ORDER:
            census = fips_to_census_state(fips)
            self.assertEqual(census_state_to_fips(census), fips)

    def test_51_rows(self):
        self.assertEqual(len(gid_crosswalk_rows()), 51)


class CrosswalkSeedTests(DbTest):
    def test_seeded_on_init(self):
        n = self.conn.execute("SELECT COUNT(*) c FROM census_gid_crosswalk").fetchone()["c"]
        self.assertEqual(n, 51)
        az = self.conn.execute(
            "SELECT * FROM census_gid_crosswalk WHERE usps='AZ'").fetchone()
        self.assertEqual(az["fips_state"], "04")
        self.assertEqual(az["census_state"], "03")


if __name__ == "__main__":
    unittest.main()
