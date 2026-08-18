"""CoG unit-file parse, statute grep, source content-hash check, ledger skip."""

from tests._db import DbTest


class CogParseTests(DbTest):
    def test_county_and_place_geoids(self):
        from taxdb.cog import parse_dat, parse_pid, geoid_for
        pid_line = (
            "392001110693WEST UNION VILLAGE" + " " * (64 - 18)
            + "Adams" + " " * (35 - 5)
            + "84294   314722             093022"
        )
        # Build a 146-char PID row by the documented offsets.
        ident = "392001110693"
        name = "WEST UNION VILLAGE".ljust(64)
        county = "Adams".ljust(35)
        place = "84294"
        rest = "   314722             093022"
        pid_line = ident + name + county + place + rest
        self.assertGreaterEqual(len(pid_line), 116)
        pid = parse_pid(pid_line + "\n")
        self.assertEqual(pid[ident]["place_fips"], "84294")

        dat = "391001000000T01         1232022R\n"
        rec = next(parse_dat(dat))
        self.assertEqual(rec["item"], "T01")
        self.assertEqual(rec["gtype"], "1")
        self.assertEqual(geoid_for(rec, {}), "39001")

        rec_city = {
            "id": ident, "fips": "39", "gtype": "2", "county": "001",
            "unit": "110693", "item": "T09", "amount": 10,
        }
        self.assertEqual(geoid_for(rec_city, pid), "3984294")


class StatuteGrepTests(DbTest):
    def test_grep_or(self):
        from taxdb import statutes
        self.conn.execute(
            "INSERT INTO statute_section (state_usps, snapshot, citation, "
            "section_title, act_status, text, source_url) VALUES (?,?,?,?,?,?,?)",
            ("OH", "vtest", "ORC 5739.021", "Additional sales tax levied by county",
             "in_force", "any county may levy a tax at the rate of not more than one per cent",
             "https://example.test/5739.021"))
        self.conn.commit()
        rows = statutes.grep(self.conn, "OH", ["lodging", "sales tax"], limit=10)
        self.assertEqual(len(rows), 1)
        self.assertIn("5739.021", rows[0]["citation"])
        none = statutes.grep(self.conn, "OH", ["zzzz"], limit=10)
        self.assertEqual(len(none), 0)


class SourceChangeTests(DbTest):
    def test_flags_content_change(self):
        from taxdb import sources, db as tdb
        sid = tdb.get_or_create_source(
            self.conn, "https://example.test/rates", "Rates",
            source_type="agency_table", authority_tier=2)
        self.conn.execute(
            "UPDATE source SET content_sha256=? WHERE id=?",
            ("a" * 64, sid))
        self.conn.commit()

        bodies = {"https://example.test/rates": (200, b"new contents")}

        def fake_get(url, timeout):
            return bodies[url]

        res = sources.check(self.conn, fetch=fake_get)
        self.assertEqual(res[0][2], 1)
        self.assertEqual(res[0][3], 1)
        row = self.conn.execute("SELECT content_changed FROM source WHERE id=?",
                                (sid,)).fetchone()
        self.assertEqual(row["content_changed"], 1)


class LedgerSkipTests(DbTest):
    def test_plan_parks_bulk_import(self):
        from taxdb import ingest, ledger
        geoid = self.place()
        ingest.load_doc(self.conn, {
            "extraction_method": "bulk_import",
            "findings": [{
                "geoid": geoid,
                "category": "sales_use",
                "instrument_code": "municipal_general_sales",
                "status": "levied",
                "rate_value": 1.0,
                "rate_unit": "percent",
                "confidence": "high",
                "source": {"url": "https://example.test/sst", "authority_tier": 2,
                           "source_type": "bulk_file"},
            }],
        }, label="bulk")
        claimed = ledger.claim(self.conn, limit=20, states=["OH"],
                               categories=["sales_use"])
        self.assertFalse(any(r["geoid"] == geoid for r in claimed))
