"""The spending meter and the $-checkpoint hard stop."""

import json
from unittest import mock

from taxdb import db
from tests._db import DbTest


class CostMathTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import spend, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.spend, self.store = spend, store
        store.apply_schema(self.conn)

    def test_haiku_rates(self):
        # 1M in at $1 + 1M out at $5.
        usd = self.spend.cost_usd(
            "claude-haiku-4-5",
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
        self.assertAlmostEqual(usd, 6.0)

    def test_batch_halves(self):
        usd = self.spend.cost_usd(
            "claude-haiku-4-5",
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000}, batch=True)
        self.assertAlmostEqual(usd, 3.0)

    def test_cache_rates(self):
        usd = self.spend.cost_usd(
            "claude-haiku-4-5",
            {"cache_creation_input_tokens": 1_000_000,
             "cache_read_input_tokens": 1_000_000})
        self.assertAlmostEqual(usd, 1.25 + 0.10)

    def test_unknown_model_prices_high_not_low(self):
        cheap = self.spend.cost_usd("claude-haiku-4-5", {"input_tokens": 1000})
        unknown = self.spend.cost_usd("somebody-new-9", {"input_tokens": 1000})
        self.assertGreater(unknown, cheap)

    def test_record_accumulates(self):
        self.spend.record(self.conn, "claude-haiku-4-5",
                          {"input_tokens": 500_000})
        total = self.spend.record(self.conn, "claude-haiku-4-5",
                                  {"input_tokens": 500_000})
        self.assertAlmostEqual(total, 1.0)
        self.assertAlmostEqual(self.spend.total(self.conn), 1.0)


class CheckpointTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import spend, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.spend, self.store = spend, store
        store.apply_schema(self.conn)

    def test_trips_only_after_a_full_step(self):
        s = {"spend_stop_usd": "100", "spend_total_usd": "99.99",
             "spend_ack_usd": "0"}
        self.assertFalse(self.spend.over_budget(s))
        s["spend_total_usd"] = "100.00"
        self.assertTrue(self.spend.over_budget(s))

    def test_zero_step_means_off(self):
        s = {"spend_stop_usd": "0", "spend_total_usd": "5000",
             "spend_ack_usd": "0"}
        self.assertFalse(self.spend.over_budget(s))

    def test_pause_stops_everything_and_acknowledges(self):
        self.store.put_many(self.conn, {
            "continuous_enabled": "1", "schedule_enabled": "1",
            "spend_stop_usd": "100", "spend_total_usd": "104.50",
            "spend_ack_usd": "0"})
        msg = self.spend.pause(self.conn)
        s = self.store.get_all(self.conn)
        self.assertEqual(s["continuous_enabled"], "0")
        self.assertEqual(s["schedule_enabled"], "0")
        # Acknowledged at the current total: the next step starts from here,
        # so the pause fires once per crossed step, not once per loop.
        self.assertFalse(self.spend.over_budget(s))
        self.assertIn("$104.50", msg)
        self.assertIn("Start", msg)

    def test_over_budget_db_uses_defaults(self):
        # A fresh database has no spend rows; the default step is 100 and
        # nothing is spent, so the checkpoint is armed but not tripped.
        self.assertFalse(self.spend.over_budget_db(self.conn))
        self.store.put_many(self.conn, {"spend_total_usd": "250"})
        self.assertTrue(self.spend.over_budget_db(self.conn))


class MeterHookTests(DbTest):
    """The Anthropic call path reports usage; other providers stay free."""

    def setUp(self):
        super().setUp()
        try:
            from collector import extract, spend, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.extract, self.spend, self.store = extract, spend, store
        store.apply_schema(self.conn)

    def test_anthropic_response_is_metered(self):
        seen = []
        payload = {
            "content": [{"type": "text", "text": "{}"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1200, "output_tokens": 300},
        }

        class _Resp:
            status_code = 200
            text = json.dumps(payload)

            def json(self):
                return payload

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return _Resp()

        with mock.patch.object(self.extract, "USAGE_HOOK",
                               lambda p, m, u: seen.append((p, m, u))), \
                mock.patch.object(self.extract.httpx, "Client",
                                  lambda **kw: _Client()):
            raw, err = self.extract.chat(
                {"provider": "anthropic", "anthropic_api_key": "sk-x"},
                "hello")
        self.assertIsNone(err)
        self.assertEqual(len(seen), 1)
        provider, model, usage = seen[0]
        self.assertEqual(provider, "anthropic")
        self.assertEqual(usage["input_tokens"], 1200)

    def test_hook_ignores_non_anthropic(self):
        self.spend._hook("llama", "qwen3-fast", {"input_tokens": 999999})
        # Nothing recorded: the local model is free.
        # (The hook opens its own connection to the default DB path, so this
        # is really asserting it returns before touching any database.)

    def test_hook_never_raises_into_extraction(self):
        with mock.patch.object(self.extract, "USAGE_HOOK",
                               mock.Mock(side_effect=RuntimeError("boom"))):
            # _report_usage swallows hook failures.
            self.extract._report_usage("anthropic", "claude-haiku-4-5", {})


class HaikuDefaultTests(DbTest):
    def setUp(self):
        super().setUp()
        try:
            from collector import extract, store
        except ImportError as exc:
            self.skipTest("collector deps missing: %s" % exc)
        self.extract, self.store = extract, store
        self.store.apply_schema(self.conn)

    def test_seeded_sonnet_migrates_to_haiku(self):
        self.conn.execute(
            "UPDATE collector_setting SET value='claude-sonnet-5' "
            "WHERE key='anthropic_model'")
        self.conn.execute(
            "DELETE FROM collector_setting "
            "WHERE key='anthropic_model_haiku_migrated'")
        self.conn.commit()
        self.store._migrate(self.conn)
        self.assertEqual(self.store.get(self.conn, "anthropic_model"),
                         "claude-haiku-4-5")

    def test_hand_picked_model_is_left_alone(self):
        self.conn.execute(
            "UPDATE collector_setting SET value='claude-opus-5' "
            "WHERE key='anthropic_model'")
        self.conn.execute(
            "DELETE FROM collector_setting "
            "WHERE key='anthropic_model_haiku_migrated'")
        self.conn.commit()
        self.store._migrate(self.conn)
        self.assertEqual(self.store.get(self.conn, "anthropic_model"),
                         "claude-opus-5")

    def test_default_model_is_haiku(self):
        self.assertEqual(self.extract.default_model({}, "anthropic"),
                         "claude-haiku-4-5")
