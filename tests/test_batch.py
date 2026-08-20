"""Batch extraction: park, submit, poll, collect, and the round trip."""

import json
import unittest
from unittest import mock

from taxdb import db
from tests._db import DbTest


def _finding(geoid="3912345"):
    return {
        "geoid": geoid, "category": "sales_use",
        "instrument_code": "municipal_general_sales", "status": "levied",
        "rate_value": 1.5, "rate_unit": "percent", "confidence": "high",
        "source_quote": "the sales tax rate is 1.5%",
        "source": {"url": "https://testville.gov/rates", "name": "Rates",
                   "source_type": "agency_table", "authority_tier": 2},
    }


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Stream:
    def __init__(self, lines, status=200):
        self.status_code = status
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return b""


class _Client:
    """Stand-in for httpx.Client that records calls."""

    def __init__(self, post=None, get=None, stream=None):
        self._post, self._get, self._stream = post, get, stream
        self.posted = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        self.posted.append((url, json))
        return self._post

    def get(self, url, headers=None):
        return self._get

    def stream(self, method, url, headers=None):
        return self._stream


class BatchTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import batch, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.batch, self.store = batch, store
        store.apply_schema(self.conn)
        self.geoid = self.place()
        store.put_many(self.conn, {
            "provider": "anthropic", "anthropic_api_key": "sk-test",
            "anthropic_model": "claude-sonnet-5", "batch_extract": "1",
            "checker_enabled": "0"})
        self.run_id = store.start_run(self.conn, "burst")
        self.conn.commit()

    def _settings(self, **kw):
        s = self.store.get_all(self.conn)
        s.update(kw)
        return s

    def _park(self, n=3, category="sales_use"):
        for i in range(n):
            self.batch.park(self.conn, self.run_id, self.geoid,
                            category if n == 1 else "%s" % category,
                            "PACKET %d" % i, "DOCUMENT TEXT %d" % i, 4,
                            search_note=None)

    # ------------------------------------------------------------ enablement
    def test_off_by_default(self):
        self.assertFalse(self.batch.enabled(
            self._settings(batch_extract="0")))

    def test_only_for_providers_with_a_batch_surface(self):
        self.assertTrue(self.batch.enabled(self._settings()))
        self.assertFalse(self.batch.enabled(self._settings(provider="llama")))
        self.assertFalse(self.batch.enabled(self._settings(provider="none")))

    # ---------------------------------------------------------------- parking
    def test_park_queues_without_calling_a_model(self):
        with mock.patch("collector.extract.chat") as chat:
            self.batch.park(self.conn, self.run_id, self.geoid, "sales_use",
                            "PACKET", "DOCS", 6)
        chat.assert_not_called()
        self.assertEqual(self.batch.queue_depth(self.conn), 1)
        row = self.batch.queued(self.conn)[0]
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["packet"], "PACKET")
        self.assertEqual(row["doc_text"], "DOCS")
        self.assertEqual(row["n_pages"], 6)

    def test_custom_ids_are_unique_across_recrawls(self):
        a = self.batch.custom_id("39001", "sales_use", 1)
        b = self.batch.custom_id("39001", "sales_use", 2)
        self.assertNotEqual(a, b)
        self.assertNotIn("_", a.split("-", 2)[2])   # provider-safe

    # ------------------------------------------------------------- submitting
    def test_submit_posts_requests_and_records_the_remote_id(self):
        self._park(3)
        client = _Client(post=_Resp(200, {"id": "msgbatch_abc"}))
        with mock.patch.object(self.batch.httpx, "Client", return_value=client):
            batch_id, n = self.batch.submit(self.conn, self._settings())
        self.assertEqual(n, 3)
        url, payload = client.posted[0]
        self.assertEqual(len(payload["requests"]), 3)
        first = payload["requests"][0]
        self.assertIn("custom_id", first)
        self.assertEqual(first["params"]["model"], "claude-sonnet-5")
        # Thinking is explicit so max_tokens cannot be eaten by reasoning.
        self.assertEqual(first["params"]["thinking"], {"type": "adaptive"})
        row = self.conn.execute(
            "SELECT * FROM extract_batch WHERE id=?", (batch_id,)).fetchone()
        self.assertEqual(row["remote_id"], "msgbatch_abc")
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(self.batch.queue_depth(self.conn), 0)

    def test_failed_submit_leaves_the_items_queued_to_retry(self):
        self._park(2)
        client = _Client(post=_Resp(500, text="upstream on fire"))
        with mock.patch.object(self.batch.httpx, "Client", return_value=client):
            with self.assertRaises(Exception):
                self.batch.submit(self.conn, self._settings())
        # Work is not lost: the crawl was expensive, the submit was not.
        self.assertEqual(self.batch.queue_depth(self.conn), 2)
        row = self.conn.execute(
            "SELECT status, message FROM extract_batch ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("on fire", row["message"])

    def test_submit_with_nothing_queued_is_a_no_op(self):
        self.assertEqual(self.batch.submit(self.conn, self._settings()), (None, 0))

    # ----------------------------------------------------------------- polling
    def test_poll_marks_ended(self):
        self._park(1)
        client = _Client(post=_Resp(200, {"id": "b1"}))
        with mock.patch.object(self.batch.httpx, "Client", return_value=client):
            self.batch.submit(self.conn, self._settings())
        row = self.batch.in_flight(self.conn)[0]
        polled = _Client(get=_Resp(200, {"processing_status": "ended"}))
        with mock.patch.object(self.batch.httpx, "Client", return_value=polled):
            status = self.batch.poll(self.conn, self._settings(), row)
        self.assertEqual(status, "ended")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM extract_batch WHERE id=?",
            (row["id"],)).fetchone()["status"], "ended")

    # -------------------------------------------------------------- collecting
    def _submit_one(self, doc_text="DOCS"):
        self.batch.park(self.conn, self.run_id, self.geoid, "sales_use",
                        "PACKET", doc_text, 4)
        client = _Client(post=_Resp(200, {"id": "b-collect"}))
        with mock.patch.object(self.batch.httpx, "Client", return_value=client):
            self.batch.submit(self.conn, self._settings())
        return self.batch.in_flight(self.conn)[0]

    def _results_client(self, results):
        lines = [json.dumps(r) for r in results]
        return _Client(stream=_Stream(lines))

    def test_successful_result_is_ingested(self):
        row = self._submit_one()
        item = self.conn.execute(
            "SELECT custom_id FROM extract_batch_item").fetchone()
        body = json.dumps({"findings": [_finding(self.geoid)]})
        results = [{"custom_id": item["custom_id"],
                    "result": {"type": "succeeded",
                               "message": {"content": [{"type": "text",
                                                        "text": body}]}}}]
        with mock.patch.object(self.batch.httpx, "Client",
                               return_value=self._results_client(results)):
            self.assertEqual(
                self.batch.collect(self.conn, self._settings(), row)["landed"], 1)
        # Downloaded but not yet ingested: applying is metered separately.
        self.assertEqual(self.batch.ready_depth(self.conn), 1)
        got = self.batch.apply_ready(self.conn, self._settings())
        self.assertEqual(got, {"done": 1, "empty": 0, "failed": 0})
        n = self.conn.execute(
            "SELECT COUNT(*) FROM tax_instrument WHERE geoid=?",
            (self.geoid,)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM extract_batch WHERE id=?",
            (row["id"],)).fetchone()["status"], "collected")

    def test_errored_result_returns_the_item_to_the_queue(self):
        row = self._submit_one()
        item = self.conn.execute(
            "SELECT custom_id FROM extract_batch_item").fetchone()
        self.conn.execute(
            "INSERT INTO work_item (geoid, category, priority, status, updated_at) "
            "VALUES (?,?,?,?,?)",
            (self.geoid, "sales_use", 10, "awaiting_ai", db.now()))
        self.conn.commit()
        results = [{"custom_id": item["custom_id"],
                    "result": {"type": "errored",
                               "error": {"type": "overloaded_error"}}}]
        with mock.patch.object(self.batch.httpx, "Client",
                               return_value=self._results_client(results)):
            self.batch.collect(self.conn, self._settings(), row)
        got = self.batch.apply_ready(self.conn, self._settings())
        self.assertEqual(got, {"done": 0, "empty": 0, "failed": 1})
        wi = self.conn.execute(
            "SELECT status, last_error FROM work_item WHERE geoid=? AND category=?",
            (self.geoid, "sales_use")).fetchone()
        self.assertEqual(wi["status"], "pending")
        self.assertIn("overloaded", wi["last_error"])

    def test_unparseable_result_is_recorded_not_swallowed(self):
        row = self._submit_one()
        item = self.conn.execute(
            "SELECT custom_id FROM extract_batch_item").fetchone()
        results = [{"custom_id": item["custom_id"],
                    "result": {"type": "succeeded",
                               "message": {"content": [{"type": "text",
                                                        "text": "not json"}]}}}]
        with mock.patch.object(self.batch.httpx, "Client",
                               return_value=self._results_client(results)):
            self.batch.collect(self.conn, self._settings(), row)
        self.assertEqual(
            self.batch.apply_ready(self.conn, self._settings())["failed"], 1)
        ce = self.conn.execute(
            "SELECT parsed_ok, error FROM crawl_extract ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(ce["parsed_ok"], 0)
        self.assertIsNotNone(ce["error"])

    def test_result_for_an_unknown_custom_id_is_ignored(self):
        row = self._submit_one()
        self.conn.execute(
            "INSERT INTO work_item (geoid, category, priority, status, updated_at) "
            "VALUES (?,?,?,?,?)",
            (self.geoid, "sales_use", 10, "awaiting_ai", db.now()))
        self.conn.commit()
        results = [{"custom_id": "x-nope-nope-1",
                    "result": {"type": "succeeded",
                               "message": {"content": []}}}]
        with mock.patch.object(self.batch.httpx, "Client",
                               return_value=self._results_client(results)):
            got = self.batch.collect(self.conn, self._settings(), row)
        self.assertEqual(got, {"landed": 0, "unknown": 1, "orphaned": 1})
        self.assertEqual(self.batch.ready_depth(self.conn), 0)
        # The submitted item never got a result. It must not sit at
        # awaiting_ai forever: it fails visibly and the work item requeues.
        item = self.conn.execute(
            "SELECT status FROM extract_batch_item").fetchone()
        self.assertEqual(item["status"], "failed")
        wi = self.conn.execute(
            "SELECT status FROM work_item WHERE geoid=? AND category=?",
            (self.geoid, "sales_use")).fetchone()
        self.assertEqual(wi["status"], "pending")

    # ------------------------------------------------------ ambiguous submit
    def test_ambiguous_submit_parks_unconfirmed_then_adopts(self):
        """A submit that dies after the request may have been accepted.
        Requeueing would post (and pay for) the batch twice; the items park
        under an 'unconfirmed' batch and reconcile adopts the remote one."""
        import httpx

        self._park(2)

        class _TimeoutClient(_Client):
            def post(self, url, headers=None, json=None):
                raise httpx.ReadTimeout("read timed out")

        with mock.patch.object(self.batch.httpx, "Client",
                               return_value=_TimeoutClient()):
            with self.assertRaises(httpx.ReadTimeout):
                self.batch.submit(self.conn, self._settings())

        row = self.conn.execute("SELECT * FROM extract_batch").fetchone()
        self.assertEqual(row["status"], "unconfirmed")
        statuses = [r["status"] for r in self.conn.execute(
            "SELECT status FROM extract_batch_item")]
        self.assertEqual(statuses, ["submitted", "submitted"])

        class _ListClient(_Client):
            def get(self, url, params=None, headers=None):
                return _Resp(200, {"data": [
                    {"id": "b-lost", "request_counts": {"processing": 2}}]})

        with mock.patch.object(self.batch.httpx, "Client",
                               return_value=_ListClient()):
            n = self.batch.reconcile_unconfirmed(self.conn, self._settings())
        self.assertEqual(n, 1)
        row = self.conn.execute("SELECT * FROM extract_batch").fetchone()
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["remote_id"], "b-lost")

    def test_connection_never_made_leaves_items_queued(self):
        import httpx

        self._park(2)

        class _DeadClient(_Client):
            def post(self, url, headers=None, json=None):
                raise httpx.ConnectError("no route to host")

        with mock.patch.object(self.batch.httpx, "Client",
                               return_value=_DeadClient()):
            with self.assertRaises(httpx.ConnectError):
                self.batch.submit(self.conn, self._settings())

        row = self.conn.execute("SELECT * FROM extract_batch").fetchone()
        self.assertEqual(row["status"], "failed")
        statuses = [r["status"] for r in self.conn.execute(
            "SELECT status FROM extract_batch_item")]
        self.assertEqual(statuses, ["queued", "queued"])

    # -------------------------------------------------------------------- tick
    def test_tick_holds_small_queues_then_sends_when_idle(self):
        self._park(2)
        s = self._settings(batch_min_items="10")
        # Nothing in flight, so a short queue still goes rather than stalling.
        client = _Client(post=_Resp(200, {"id": "b-tick"}))
        with mock.patch.object(self.batch.httpx, "Client", return_value=client):
            out = self.batch.tick(self.conn, s)
        self.assertEqual(out["submitted"], 2)

    def test_applying_is_metered_so_a_big_batch_cannot_stall_the_crawl(self):
        """Downloading 200 results is one stream; ingesting them is 200 model
        calls. Unmetered, a finished batch would hold the coordinator for the
        better part of an hour and stop crawling."""
        row = self._submit_one()
        for i in range(30):
            self.conn.execute(
                "INSERT INTO extract_batch_item (batch_id, custom_id, run_id, "
                "geoid, category, packet, doc_text, n_pages, status, "
                "raw_response, created_at) VALUES (?,?,?,?,?,?,?,?, 'ready', ?, ?)",
                (row["id"], "x-ready-%d" % i, self.run_id, self.geoid,
                 "sales_use", "p", "d", 1, "{}", db.now()))
        self.conn.commit()
        self.assertEqual(self.batch.ready_depth(self.conn), 30)
        applied = []
        got = self.batch.apply_ready(
            self.conn, self._settings(), limit=10,
            apply_result=lambda *a: applied.append(1) or True)
        self.assertEqual(len(applied), 10)
        self.assertEqual(got["done"], 10)

    def test_an_empty_read_is_not_counted_as_a_failure(self):
        """A county with no such tax is a real answer, not a broken read.

        Filing the two together is what made a healthy pipeline report 88 of
        106 items failed.
        """
        row = self._submit_one()
        item = self.conn.execute(
            "SELECT custom_id FROM extract_batch_item").fetchone()
        results = [{"custom_id": item["custom_id"],
                    "result": {"type": "succeeded",
                               "message": {"content": [
                                   {"type": "text",
                                    "text": json.dumps({"findings": []})}]}}}]
        with mock.patch.object(self.batch.httpx, "Client",
                               return_value=self._results_client(results)):
            self.batch.collect(self.conn, self._settings(), row)
        got = self.batch.apply_ready(self.conn, self._settings())
        self.assertEqual(got, {"done": 0, "empty": 1, "failed": 0})
        counts = self.conn.execute(
            "SELECT n_succeeded, n_empty, n_failed FROM extract_batch "
            "WHERE id=?", (row["id"],)).fetchone()
        self.assertEqual(counts["n_empty"], 1)
        self.assertEqual(counts["n_failed"], 0)

    def test_an_empty_read_says_why_on_the_item(self):
        """A 'done' row with no note was the diagnostic's blind spot."""
        row = self._submit_one()
        item = self.conn.execute(
            "SELECT custom_id FROM extract_batch_item").fetchone()
        results = [{"custom_id": item["custom_id"],
                    "result": {"type": "succeeded",
                               "message": {"content": [
                                   {"type": "text",
                                    "text": json.dumps({"findings": []})}]}}}]
        with mock.patch.object(self.batch.httpx, "Client",
                               return_value=self._results_client(results)):
            self.batch.collect(self.conn, self._settings(), row)
        self.batch.apply_ready(self.conn, self._settings())
        got = self.conn.execute(
            "SELECT status, error FROM extract_batch_item").fetchone()
        self.assertEqual(got["status"], "done")
        self.assertIn("0 valid rows", got["error"])

    def test_findings_are_credited_to_the_run_that_crawled_them(self):
        """The crawl run is closed long before the batch comes back.

        Without the retro-credit every continuous run reads '0 findings'
        however much it collected, because in batch mode the reading happens
        after the run that fetched the pages has finished.
        """
        row = self._submit_one()
        item = self.conn.execute(
            "SELECT custom_id FROM extract_batch_item").fetchone()
        self.store.finish_run(self.conn, self.run_id, "ok", None,
                              items=1, pages=4, findings=0)
        body = json.dumps({"findings": [_finding(self.geoid)]})
        results = [{"custom_id": item["custom_id"],
                    "result": {"type": "succeeded",
                               "message": {"content": [{"type": "text",
                                                        "text": body}]}}}]
        with mock.patch.object(self.batch.httpx, "Client",
                               return_value=self._results_client(results)):
            self.batch.collect(self.conn, self._settings(), row)
        self.batch.apply_ready(self.conn, self._settings())
        got = self.conn.execute(
            "SELECT pages_fetched, findings_written FROM crawl_run WHERE id=?",
            (self.run_id,)).fetchone()
        self.assertEqual(got["findings_written"], 1)
        self.assertEqual(got["pages_fetched"], 4)

    def test_a_poisonous_result_is_failed_not_retried_forever(self):
        row = self._submit_one()
        self.conn.execute(
            "UPDATE extract_batch_item SET status='ready', raw_response='{}'")
        self.conn.commit()

        def explode(*a):
            raise RuntimeError("ingest blew up")

        got = self.batch.apply_ready(self.conn, self._settings(),
                                     apply_result=explode)
        self.assertEqual(got["failed"], 1)
        # Not left 'ready', or every tick would re-run the same explosion.
        self.assertEqual(self.batch.ready_depth(self.conn), 0)
        item = self.conn.execute(
            "SELECT status, error FROM extract_batch_item").fetchone()
        self.assertEqual(item["status"], "failed")
        self.assertIn("blew up", item["error"])

    def test_tick_collects_before_it_submits(self):
        """A restart must pick up in-flight work, not pile more on top."""
        row = self._submit_one()
        self.conn.execute("UPDATE extract_batch SET status='ended' WHERE id=?",
                          (row["id"],))
        self.conn.commit()
        self._park(1)
        order = []

        def fake_collect(conn, settings, batch_row):
            order.append("collect")
            conn.execute("UPDATE extract_batch SET status='collected' WHERE id=?",
                         (batch_row["id"],))
            conn.commit()
            return {"landed": 1, "unknown": 0}

        def fake_submit(conn, settings, limit=None):
            order.append("submit")
            conn.execute("UPDATE extract_batch_item SET status='submitted' "
                         "WHERE status='queued'")
            conn.commit()
            return 99, 1

        with mock.patch.object(self.batch, "collect", fake_collect), \
             mock.patch.object(self.batch, "submit", fake_submit):
            self.batch.tick(self.conn, self._settings(batch_min_items="1"))
        self.assertEqual(order, ["collect", "submit"])


class BatchWorkerPathTests(DbTest):
    """The crawl half: park and move on, without calling a model."""

    def setUp(self):
        super().setUp()
        try:
            from collector import store, worker
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.store, self.worker = store, worker
        store.apply_schema(self.conn)
        self.geoid = self.place()
        store.put_many(self.conn, {
            "provider": "anthropic", "anthropic_api_key": "sk-test",
            "batch_extract": "1"})
        self.conn.execute(
            "INSERT INTO work_item (geoid, category, priority, status, updated_at) "
            "VALUES (?,?,?,?,?)", (self.geoid, "sales_use", 10, "in_progress",
                                   db.now()))
        self.run_id = store.start_run(self.conn, "burst")
        self.conn.commit()

    def test_crawl_parks_the_item_and_skips_the_model(self):
        row = {"geoid": self.geoid, "category": "sales_use"}
        with mock.patch.object(self.worker.crawl, "crawl_item",
                               return_value=([{"url": "u"}], "PAGE TEXT")), \
             mock.patch.object(self.worker.crawl, "search_note",
                               return_value=None), \
             mock.patch.object(self.worker.extract, "extract") as ex:
            pages, findings = self.worker._process(
                self.conn, None, self.store.get_all(self.conn), self.run_id, row)
        ex.assert_not_called()
        self.assertEqual(findings, 0)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM work_item WHERE geoid=?",
            (self.geoid,)).fetchone()["status"], "awaiting_ai")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM extract_batch_item").fetchone()[0], 1)

    def test_awaiting_items_are_not_reclaimed_or_stale_swept(self):
        from taxdb import ledger
        self.conn.execute(
            "UPDATE work_item SET status='awaiting_ai', claimed_at=? WHERE geoid=?",
            ("2000-01-01 00:00:00", self.geoid))
        self.conn.commit()
        self.assertEqual(ledger.claim(self.conn, limit=5), [])
        ledger.release_stale(self.conn, hours=1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM work_item WHERE geoid=?",
            (self.geoid,)).fetchone()["status"], "awaiting_ai")


if __name__ == "__main__":
    unittest.main()


class RoundTripTests(DbTest):
    """A batched item and a live one must end in the same state."""

    def setUp(self):
        super().setUp()
        try:
            from collector import batch, store, worker
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.batch, self.store, self.worker = batch, store, worker
        store.apply_schema(self.conn)
        self.geoid = self.place()
        store.put_many(self.conn, {
            "provider": "anthropic", "anthropic_api_key": "sk-test",
            "anthropic_model": "claude-sonnet-5", "checker_enabled": "1"})
        self.conn.execute(
            "INSERT INTO work_item (geoid, category, priority, status, updated_at) "
            "VALUES (?,?,?,?,?)",
            (self.geoid, "sales_use", 10, "in_progress", db.now()))
        self.run_id = store.start_run(self.conn, "burst")
        self.conn.commit()
        self.doc_text = "City rates. The sales tax rate is 1.5% this year."
        self.body = json.dumps({"findings": [_finding(self.geoid)]})

    def _settings(self, **kw):
        s = self.store.get_all(self.conn)
        s.update(kw)
        return s

    def test_batched_item_reaches_complete_like_a_live_one(self):
        s = self._settings(batch_extract="1")
        row = {"geoid": self.geoid, "category": "sales_use"}
        with mock.patch.object(self.worker.crawl, "crawl_item",
                               return_value=([{"url": "u"}], self.doc_text)), \
             mock.patch.object(self.worker.crawl, "search_note",
                               return_value=None):
            self.worker._process(self.conn, None, s, self.run_id, row)
        self.assertEqual(self._status(), "awaiting_ai")

        client = _Client(post=_Resp(200, {"id": "b-rt"}))
        with mock.patch.object(self.batch.httpx, "Client", return_value=client):
            self.batch.submit(self.conn, s)
        brow = self.batch.in_flight(self.conn)[0]
        cid = self.conn.execute(
            "SELECT custom_id FROM extract_batch_item").fetchone()["custom_id"]
        results = [{"custom_id": cid,
                    "result": {"type": "succeeded",
                               "message": {"content": [{"type": "text",
                                                        "text": self.body}]}}}]
        verdict = json.dumps({"verdicts": [
            {"instrument_code": "municipal_general_sales", "verdict": "pass",
             "reason": ""}]})
        lines = [json.dumps(r) for r in results]
        with mock.patch.object(self.batch.httpx, "Client",
                               return_value=_Client(stream=_Stream(lines))):
            self.batch.collect(self.conn, s, brow)
        with mock.patch("collector.extract.chat", return_value=(verdict, None)):
            self.batch.apply_ready(self.conn, s)

        # Same end state as the synchronous path: auto-verified, not queued.
        self.assertEqual(self._status(), "complete")
        self.assertEqual(self.conn.execute(
            "SELECT verdict FROM check_result").fetchone()["verdict"], "pass")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM tax_instrument WHERE geoid=?",
            (self.geoid,)).fetchone()[0], 1)

    def _status(self):
        return self.conn.execute(
            "SELECT status FROM work_item WHERE geoid=? AND category=?",
            (self.geoid, "sales_use")).fetchone()["status"]
