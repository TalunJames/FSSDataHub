"""Tax Database CLI."""

import argparse
import json
import os
import sys

from . import db, seed, ledger, packets, ingest, export, sources, archive, coverage
from . import adapters
from .vocab import CATEGORIES, COMPLETENESS, WORK_CATEGORIES


def _fmt(n):
    return "{:,}".format(n or 0)


def cmd_init(args):
    conn = db.init_db()
    print("initialized %s" % db.db_path())
    n = sources.seed_catalog(conn) if args.with_sources else 0
    if n:
        print("seeded %d source entry points (unverified -- run `taxdb sources check`)" % n)
    return 0


def cmd_seed(args):
    conn = db.connect(create=False)
    kinds = ["county", "place"]
    if args.include_mcd:
        kinds.append("mcd")
    if args.counties_only:
        kinds = ["county"]
    with db.Run(conn, "seed", vars(args)) as run:
        counts = seed.seed(conn, kinds=tuple(kinds), force=args.force)
        run.rows_out = sum(counts.values())
    for k, v in sorted(counts.items()):
        print("  %-8s %s" % (k, _fmt(v)))
    print("total %s jurisdictions" % _fmt(sum(counts.values())))
    sources.seed_catalog(conn)
    n = coverage.seed_empty_states(conn)
    print("seeded %d empty-state coverage assertions (ballot_measure = none)" % n)
    return 0


def cmd_plan(args):
    conn = db.connect(create=False)
    kinds = tuple(args.kinds.split(",")) if args.kinds else ("county", "place")
    cats = args.categories.split(",") if args.categories else None
    with db.Run(conn, "plan", vars(args)) as run:
        n = ledger.plan(conn, states=args.state, kinds=kinds, categories=cats,
                        min_pop=args.min_pop, batch=args.batch, limit=args.limit)
        run.rows_out = n
    print("created %s work items" % _fmt(n))
    total = conn.execute("SELECT COUNT(*) c FROM work_item WHERE status='pending'"
                         ).fetchone()["c"]
    print("%s pending in queue" % _fmt(total))
    return 0


def cmd_next(args):
    conn = db.connect(create=False)
    cats = args.categories.split(",") if args.categories else None
    kinds = args.kinds.split(",") if args.kinds else None
    rows = ledger.claim(conn, limit=args.limit, states=args.state, categories=cats,
                        batch=args.batch, kinds=kinds)
    if not rows:
        print("queue empty for that filter -- run `taxdb plan` first, or widen the filter")
        return 0
    if args.emit:
        outdir = args.out or os.path.join(db.OUT_DIR, "packets")
        paths = packets.write_batch(conn, rows, outdir)
        print("wrote %d packet(s) to %s" % (len(paths), outdir))
        for p in paths:
            print("  " + p)
    else:
        for r in rows:
            j = conn.execute("SELECT name, state_usps, kind, population FROM jurisdiction "
                             "WHERE geoid=?", (r["geoid"],)).fetchone()
            print("%-11s %-4s %-8s %-38s %s" % (
                r["geoid"], j["state_usps"], j["kind"], j["name"][:38], r["category"]))
    return 0


def cmd_packet(args):
    conn = db.connect(create=False)
    cats = args.categories.split(",") if args.categories else None
    text = packets.build(conn, args.geoid, cats)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print("wrote %s" % args.out)
    else:
        sys.stdout.write(text)
    return 0


def cmd_ingest(args):
    conn = db.connect(create=False)
    with db.Run(conn, "ingest", vars(args)) as run:
        res = ingest.load(conn, args.file, dry_run=args.dry_run,
                          allow_partial=args.allow_partial)
        run.rows_in = res["valid"] + res["rejected"]
        run.rows_out = res["written"]
    if args.dry_run:
        print("dry run: %d valid, %d rejected" % (res["valid"], res["rejected"]))
    else:
        by_type = res.get("by_type") or {}
        detail = ", ".join("%d %s" % (n, name) for name, n in sorted(by_type.items())
                           if n) or "nothing"
        print("wrote %d row(s) across %d jurisdiction(s): %s; marked needs_review"
              % (res["written"], res.get("jurisdictions", 0), detail))
    for e in res["errors"]:
        print("  REJECTED %s" % e)
    return 0


def cmd_fetch(args):
    conn = db.connect(create=False)
    if args.list or not args.adapter:
        print("%-18s %-5s %-22s %s" % ("KEY", "STATE", "CATEGORIES", "SOURCE"))
        if not adapters.ADAPTERS:
            print("(no adapters registered yet -- subclass Adapter in taxdb/adapters/)")
            return 0
        for key, cls in sorted(adapters.ADAPTERS.items()):
            print("%-18s %-5s %-22s %s" % (key, cls.state or "-",
                                           ",".join(cls.categories), cls.url))
        return 0
    a = adapters.get(args.adapter)
    with db.Run(conn, "fetch", vars(args)) as run:
        res = a.run(conn, dry_run=args.dry_run, archive_only=args.archive_only,
                    states=args.state)
        run.rows_out = res["written"]
    if args.archive_only:
        print("%s: archived %s (archive_file_id=%s)"
              % (a.key, (res.get("sha256") or "")[:16] or "-", res.get("archive_file_id")))
        return 0
    print("%s: %d finding(s) written, %d rejected, %d source row(s) unmapped"
          % (a.key, res["written"], res["rejected"], len(res["unmapped"])))
    if res.get("files"):
        print("  %d file(s)" % res["files"])
    if res.get("rate_change_events"):
        print("  %s rate_change_event row(s)" % _fmt(res["rate_change_events"]))
    for e in res["errors"][:10]:
        print("  REJECTED %s" % e)
    if res["unmapped"]:
        path = os.path.join(db.OUT_DIR, "unmapped_%s.csv" % a.key)
        os.makedirs(db.OUT_DIR, exist_ok=True)
        with open(path, "w") as fh:
            fh.write("raw_name,reason\n")
            for name, why in res["unmapped"]:
                fh.write('"%s","%s"\n' % (name.replace('"', "'"), why.replace('"', "'")))
        print("  unmapped rows listed in %s" % path)
    return 0


def cmd_archive(args):
    conn = db.connect(create=False)
    if args.action == "put":
        if not (args.adapter and args.period and args.url and args.file):
            raise SystemExit("archive put requires --adapter --period --url FILE")
        with open(args.file, "rb") as fh:
            blob = fh.read()
        sid = db.get_or_create_source(
            conn, args.url, args.adapter, source_type="bulk_file", authority_tier=2)
        aid, sha, path, created = archive.put(
            conn, args.adapter, args.url, blob, args.period,
            period_start=args.period_start, period_end=args.period_end,
            source_id=sid, filename=args.file)
        print("%s archive_file %d  %s  %s" % (
            "created" if created else "exists", aid, sha[:16], path))
        return 0
    rows = archive.list_files(conn, adapter=args.adapter)
    if not rows:
        print("(archive empty)")
        return 0
    print("%-5s %-18s %-12s %-12s %s" % ("ID", "ADAPTER", "PERIOD", "STATUS", "URL"))
    for r in rows:
        print("%-5d %-18s %-12s %-12s %s" % (
            r["id"], r["adapter"][:18], r["period_label"],
            r["parse_status"] or "-", r["url"][:60]))
    print("%s file(s)" % _fmt(len(rows)))
    return 0


def cmd_coverage(args):
    conn = db.connect(create=False)
    if args.action == "seed":
        n = coverage.seed_empty_states(conn)
        print("inserted %d empty-state assertion(s)" % n)
        return 0
    if args.geoid:
        row = coverage.for_jurisdiction(conn, args.geoid, domain=args.domain)
        if not row:
            print("no coverage assertion for %s / %s" % (args.geoid, args.domain))
            return 1
        for k in row.keys():
            print("  %-22s %s" % (k, row[k] if row[k] is not None else ""))
        return 0
    rows = coverage.list_assertions(conn, domain=args.domain, completeness=args.completeness)
    print("%-6s %-16s %-8s %-12s %-12s %s" % (
        "ID", "DOMAIN", "SCOPE", "GEOID", "COMPLETE", "NAME"))
    for r in rows:
        print("%-6d %-16s %-8s %-12s %-12s %s" % (
            r["id"], r["domain"], r["scope_type"], r["scope_geoid"],
            r["completeness"], r["name"] or ""))
    print("%s assertion(s)" % _fmt(len(rows)))
    return 0


def cmd_review(args):
    conn = db.connect(create=False)
    if args.geoid:
        if not args.category:
            # set_status matches WHERE category=?, so a missing category
            # updates nothing while printing success.
            raise SystemExit("--category is required with --geoid")
        ledger.set_status(conn, args.geoid, args.category, args.status)
        conn.commit()
        print("%s / %s -> %s" % (args.geoid, args.category, args.status))
        return 0
    rows = conn.execute(
        "SELECT w.geoid, w.category, j.name, j.state_usps FROM work_item w "
        "JOIN jurisdiction j ON j.geoid=w.geoid WHERE w.status='needs_review' "
        "ORDER BY w.priority DESC LIMIT ?", (args.limit,)).fetchall()
    for r in rows:
        print("%-11s %-4s %-32s %s" % (r["geoid"], r["state_usps"],
                                       r["name"][:32], r["category"]))
    print("\n%d awaiting review (use --geoid GEOID --category CAT --status complete)"
          % len(rows))
    return 0


def cmd_status(args):
    conn = db.connect(create=False)
    j = conn.execute(
        "SELECT kind, COUNT(*) c, SUM(COALESCE(population,0)) p FROM jurisdiction "
        "GROUP BY kind ORDER BY c DESC").fetchall()
    print("JURISDICTIONS")
    if not j:
        print("  (none -- run `taxdb seed`)")
    for r in j:
        print("  %-8s %8s   pop %s" % (r["kind"], _fmt(r["c"]), _fmt(r["p"])))

    n_cov = conn.execute("SELECT completeness, COUNT(*) c FROM coverage_assertion "
                         "GROUP BY completeness ORDER BY c DESC").fetchall()
    if n_cov:
        print("\nCOVERAGE ASSERTIONS")
        for r in n_cov:
            print("  %-14s %8s" % (r["completeness"], _fmt(r["c"])))

    tot = conn.execute("SELECT COUNT(*) c FROM work_item").fetchone()["c"]
    if not tot:
        print("\nNo work planned yet. Run `taxdb plan --state XX`.")
    else:
        print("\nQUEUE")
        for r in conn.execute(
                "SELECT status, COUNT(*) c FROM work_item GROUP BY status "
                "ORDER BY c DESC").fetchall():
            print("  %-14s %8s  %5.1f%%" % (r["status"], _fmt(r["c"]), 100.0 * r["c"] / tot))

        done = conn.execute(
            "SELECT SUM(CASE WHEN w.status IN ('complete','no_data') THEN "
            "COALESCE(j.population,0) ELSE 0 END) d, SUM(COALESCE(j.population,0)) t "
            "FROM work_item w JOIN jurisdiction j ON j.geoid=w.geoid").fetchone()
        if done["t"]:
            print("\n  population-weighted coverage: %.2f%%" % (100.0 * done["d"] / done["t"]))

        rows = ledger.status_report(conn, args.state)
        if args.by_state and rows:
            print("\nBY STATE")
            agg = {}
            for r in rows:
                agg.setdefault(r["st"], {})[r["status"]] = r["n"]
            for st in sorted(agg):
                d = agg[st]
                t = sum(d.values())
                c = d.get("complete", 0) + d.get("no_data", 0)
                print("  %-3s %6s items  %5.1f%% done" % (st, _fmt(t), 100.0 * c / t))

    n_tax = conn.execute("SELECT COUNT(*) c FROM tax_instrument "
                         "WHERE superseded_by IS NULL").fetchone()["c"]
    n_bm = conn.execute("SELECT COUNT(*) c FROM ballot_measure "
                        "WHERE superseded_by IS NULL").fetchone()["c"]
    n_af = conn.execute("SELECT COUNT(*) c FROM archive_file").fetchone()["c"]
    print("\n%s current tax instrument records" % _fmt(n_tax))
    print("%s current ballot measures" % _fmt(n_bm))
    print("%s archived files" % _fmt(n_af))
    n_st = conn.execute("SELECT COUNT(*) c FROM statute_section").fetchone()["c"]
    n_rb = conn.execute("SELECT COUNT(*) c FROM revenue_base").fetchone()["c"]
    print("%s statute sections cached" % _fmt(n_st))
    print("%s revenue_base rows" % _fmt(n_rb))
    return 0


def cmd_verify(args):
    conn = db.connect(create=False)
    checks = ingest.verify(conn)
    bad = 0
    for label, n, sample in checks:
        flag = "ok  " if n == 0 else "FLAG"
        print("%s %-62s %s" % (flag, label, _fmt(n)))
        if n:
            bad += n
            for s in sample:
                print("       %s" % (tuple(s),))
    print("\n%s row(s) flagged" % _fmt(bad))
    return 1 if bad and args.strict else 0


def cmd_export(args):
    conn = db.connect(create=False)
    outdir, written = export.export_all(conn, args.out, args.state)
    for k, v in written.items():
        print("  %-22s %s rows" % (k, _fmt(v)))
    print("wrote to %s" % outdir)
    return 0


def cmd_sources(args):
    conn = db.connect(create=False)
    if args.action == "check":
        res = sources.check(conn, limit=args.limit)
        ok = sum(1 for _, _, o, _c in res if o)
        changed = sum(1 for _u, _s, _o, c in res if c)
        for url, status, o, ch in res:
            if ch:
                print("  CHANGED    %-6s %s" % (status, url))
            elif not o:
                hint = "bot filter?" if status in (404, None) else ""
                print("  UNVERIFIED %-6s %-58s %s" % (status, url, hint))
        print("%d/%d source URLs resolve; %d content change(s)" % (ok, len(res), changed))
    elif args.action == "add":
        sid = db.get_or_create_source(
            conn, args.url, args.name or args.url, source_type=args.type,
            authority_tier=args.tier, scope_geoid=args.geoid)
        conn.commit()
        print("source %d" % sid)
    else:
        sql = "SELECT id, scope_geoid, authority_tier, verified, name, url FROM source"
        params = []
        if args.geoid:
            sql += " WHERE scope_geoid=?"
            params.append(args.geoid)
        sql += " ORDER BY authority_tier, name"
        for r in conn.execute(sql, params).fetchall():
            print("%-5d t%d %s %-12s %-46s %s" % (
                r["id"], r["authority_tier"], "OK " if r["verified"] else "?? ",
                r["scope_geoid"] or "-", (r["name"] or "")[:46], r["url"]))
    return 0


def cmd_profile(args):
    conn = db.connect(create=False)
    if args.set:
        field, _, value = args.set.partition("=")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(state_profile)")]
        if field not in cols:
            raise SystemExit("unknown field %r; valid: %s" % (field, ", ".join(cols)))
        conn.execute("UPDATE state_profile SET %s=?, verified_at=? WHERE state_usps=?"
                     % field, (value, db.now(), args.state.upper()))
        conn.commit()
        print("%s.%s set" % (args.state.upper(), field))
        return 0
    r = conn.execute("SELECT * FROM state_profile WHERE state_usps=?",
                     (args.state.upper(),)).fetchone()
    if not r:
        raise SystemExit("no profile row for %s -- run `taxdb seed`" % args.state)
    for k in r.keys():
        print("  %-28s %s" % (k, r[k] if r[k] is not None else ""))
    return 0


def cmd_cog(args):
    from . import cog
    conn = db.connect(create=False)
    with db.Run(conn, "cog", vars(args)) as run:
        res = cog.load(conn, force=args.force)
        run.rows_out = res["written"]
    print("revenue_base: %s row(s); %s source row(s) unmapped"
          % (_fmt(res["written"]), _fmt(res["unmapped"])))
    return 0


def cmd_statutes(args):
    from . import statutes
    conn = db.connect(create=False)
    usps = args.state.upper()
    if args.action == "fetch":
        try:
            with db.Run(conn, "statutes", vars(args)) as run:
                res = statutes.fetch_state(conn, usps, force=args.force)
                run.rows_out = res["written"]
        except statutes.StatutesError as exc:
            raise SystemExit(str(exc))
        print("%s: %s section(s) from %s" % (usps, _fmt(res["written"]), res["snapshot"]))
        return 0
    rows = statutes.grep(conn, usps, args.terms, limit=args.limit)
    if not rows:
        print("no matches — run `taxdb statutes fetch %s` first, or widen terms" % usps)
        return 0
    for r in rows:
        print("%s  [%s]  %s" % (r["citation"] or "", r["act_status"] or "",
                                (r["section_title"] or "")[:80]))
        excerpt = (r["excerpt"] or "").replace("\n", " ")
        if excerpt:
            print("    %s" % excerpt[:240])
        if r["source_url"]:
            print("    %s" % r["source_url"])
    print("%s hit(s)" % _fmt(len(rows)))
    return 0


def cmd_geocode(args):
    from . import geocode
    geoid = geocode.lookup(args.query, args.state, kind=args.kind)
    if not geoid:
        print("no match")
        return 1
    print(geoid)
    return 0


def cmd_sql(args):
    conn = db.connect(create=False)
    rows = conn.execute(args.query).fetchall()
    if not rows:
        print("(no rows)")
        return 0
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
        return 0
    keys = rows[0].keys()
    print(" | ".join(keys))
    for r in rows[:args.limit]:
        print(" | ".join("" if v is None else str(v) for v in r))
    if len(rows) > args.limit:
        print("... %s more rows" % _fmt(len(rows) - args.limit))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="taxdb",
        description="Tax Database: local tax authority, capacity, and election history")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="create the database")
    s.add_argument("--with-sources", action="store_true",
                   help="also seed state agency source entry points")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("seed", help="load jurisdictions from Census bulk files")
    s.add_argument("--include-mcd", action="store_true",
                   help="include townships/minor civil divisions")
    s.add_argument("--counties-only", action="store_true")
    s.add_argument("--force", action="store_true", help="re-download cached files")
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("plan", help="create work items")
    s.add_argument("--state", nargs="*", help="USPS codes, e.g. --state OH MI")
    s.add_argument("--kinds", help="comma list: county,place,mcd,state")
    s.add_argument("--categories",
                   help="comma list: %s (the last two are research passes: "
                        "framework runs per state, elections per county)"
                        % ",".join(WORK_CATEGORIES))
    s.add_argument("--min-pop", type=int, default=0)
    s.add_argument("--batch")
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("next", help="claim the next items off the queue")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--state", nargs="*")
    s.add_argument("--kinds")
    s.add_argument("--categories")
    s.add_argument("--batch")
    s.add_argument("--emit", action="store_true", help="write research packets")
    s.add_argument("--out")
    s.set_defaults(func=cmd_next)

    s = sub.add_parser("packet", help="print a research packet for one jurisdiction")
    s.add_argument("geoid")
    s.add_argument("--categories")
    s.add_argument("--out")
    s.set_defaults(func=cmd_packet)

    s = sub.add_parser("ingest", help="load a findings JSON file")
    s.add_argument("file")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--allow-partial", action="store_true")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("fetch", help="run a bulk source adapter")
    s.add_argument("adapter", nargs="?")
    s.add_argument("--list", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--archive-only", action="store_true",
                   help="save bytes with a period label; do not parse")
    s.add_argument("--state", nargs="*", help="limit SST (and similar) to these USPS codes")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("archive", help="dated object store")
    s.add_argument("action", choices=["list", "put"], nargs="?", default="list")
    s.add_argument("file", nargs="?")
    s.add_argument("--adapter")
    s.add_argument("--period", help="period label, e.g. 2026Q3")
    s.add_argument("--period-start")
    s.add_argument("--period-end")
    s.add_argument("--url")
    s.set_defaults(func=cmd_archive)

    s = sub.add_parser("coverage", help="coverage assertions")
    s.add_argument("action", choices=["list", "seed"], nargs="?", default="list")
    s.add_argument("--domain", default="ballot_measure")
    s.add_argument("--completeness", choices=sorted(COMPLETENESS))
    s.add_argument("--geoid", help="resolve the assertion that covers this jurisdiction")
    s.set_defaults(func=cmd_coverage)

    s = sub.add_parser("review", help="list or resolve items awaiting review")
    s.add_argument("--geoid")
    s.add_argument("--category")
    s.add_argument("--status", default="complete")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("status", help="coverage dashboard")
    s.add_argument("--state", nargs="*")
    s.add_argument("--by-state", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("verify", help="data integrity checks")
    s.add_argument("--strict", action="store_true", help="exit nonzero on any flag")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("export", help="write CSVs")
    s.add_argument("--out")
    s.add_argument("--state", nargs="*")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("sources", help="manage the source catalog")
    s.add_argument("action", choices=["list", "check", "add"], nargs="?", default="list")
    s.add_argument("--url")
    s.add_argument("--name")
    s.add_argument("--type", default="portal")
    s.add_argument("--tier", type=int, default=4)
    s.add_argument("--geoid")
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_sources)

    s = sub.add_parser("profile", help="view or edit a state statutory profile")
    s.add_argument("state")
    s.add_argument("--set", help="field=value")
    s.set_defaults(func=cmd_profile)

    s = sub.add_parser("cog", help="load Census of Governments 2022 unit finance files")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_cog)

    s = sub.add_parser("statutes", help="Open US Law statute corpus")
    s.add_argument("action", choices=["fetch", "grep"], nargs="?", default="grep")
    s.add_argument("state", help="USPS code")
    s.add_argument("terms", nargs="*", help="grep terms (default: tax keywords)")
    s.add_argument("--force", action="store_true")
    s.add_argument("--limit", type=int, default=25)
    s.set_defaults(func=cmd_statutes)

    s = sub.add_parser("geocode", help="Census geocoder (no key) → GEOID")
    s.add_argument("query", help='place name, e.g. "Columbus"')
    s.add_argument("--state", required=True)
    s.add_argument("--kind", choices=["county", "place"])
    s.set_defaults(func=cmd_geocode)

    s = sub.add_parser("sql", help="run a read-only query")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sql)

    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args)
