"""Pricing regressions. Run with python3 -B -m unittest test_pricing."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from pricing import resolve_rate


TS = "2026-10-14T00:00:00Z"


class PricingTests(unittest.TestCase):
    def test_new_model_rates(self):
        for model, expected in {
            "gpt-6-sol": (2, 10, 0.2),
            "gpt-6.1-sol": (2, 10, 0.1),
            "gpt-6-luna": (0.1, 0.5, 0.01),
        }.items():
            with self.subTest(model=model):
                self.assertIn(model, server.codex.load_pricing()["models"])
                self.assertEqual(server.codex.price_for_model(model, TS), expected)
        for model, expected in {
            "claude-fable-5-1": (10, 50), "claude-mythos-5-1": (10, 50),
            "claude-opus-5": (5, 25), "claude-opus-5-5": (4, 20),
            "claude-sonnet-5-5": (2, 10), "claude-haiku-5-5": (0.1, 0.5),
        }.items():
            with self.subTest(model=model):
                self.assertIn(model, server.claude.load_pricing()["models"])
                self.assertEqual(server.claude.price_for_model(model, TS), expected)

    def test_dated_snapshots_inherit_full_rates(self):
        self.assertEqual(server.codex.price_for_model("gpt-6.1-sol-2026-09-29", TS),
                         (2, 10, 0.1))
        costs = server.claude.compute_costs("claude-sonnet-5-5-20260928", TS,
                                           {"cache_read": 1_000_000})
        self.assertAlmostEqual(costs["cost_cache_read"], 0.1)
        # The earlier Sonnet 5 model must not capture Sonnet 5.5's price.
        self.assertEqual(server.claude.price_for_model("claude-sonnet-5-20260901", TS),
                         (2, 10))

    def test_new_claude_cache_discounts(self):
        for model, cached_price in {
            "claude-fable-5-1": 0.25, "claude-mythos-5-1": 0.25,
            "claude-opus-5-5": 0.2, "claude-sonnet-5-5": 0.1,
            "claude-opus-4-8": 0.5,
        }.items():
            with self.subTest(model=model):
                costs = server.claude.compute_costs(model, TS, {"cache_read": 1_000_000})
                self.assertAlmostEqual(costs["cost_cache_read"], cached_price)

    def test_haiku_threshold_counts_all_prompt_tokens(self):
        short = server.claude.compute_costs("claude-haiku-5-5", TS,
            {"input": 1000, "cache_read": 97000, "cache_5m": 1000, "cache_1h": 1000,
             "output": 1000})
        long = server.claude.compute_costs("claude-haiku-5-5", TS,
            {"input": 1001, "cache_read": 97000, "cache_5m": 1000, "cache_1h": 1000,
             "output": 1000})
        self.assertAlmostEqual(short["cost_input"], 0.0001)
        self.assertAlmostEqual(long["cost_input"], 0.0005005)
        self.assertAlmostEqual(long["cost_cache_read"], short["cost_cache_read"] * 5)
        self.assertAlmostEqual(long["cost_cache_write"], short["cost_cache_write"] * 5)
        self.assertAlmostEqual(long["cost_output"], short["cost_output"] * 5)

    def test_codex_long_context_threshold(self):
        self.assertEqual(server.codex.price_for_model("gpt-6.1-sol", TS, 272000),
                         (2, 10, 0.1))
        self.assertEqual(server.codex.price_for_model("gpt-6.1-sol", TS, 272001),
                         (4, 15, 0.2))
        costs = server.codex.compute_costs("gpt-6.1-sol", TS,
            {"input": 300000, "cached_input": 250000, "cache_write": 20000,
             "output": 1000})
        self.assertAlmostEqual(costs["cost_input"], 0.12)
        self.assertAlmostEqual(costs["cost_cache_read"], 0.05)
        self.assertAlmostEqual(costs["cost_cache_write"], 0.1)
        self.assertAlmostEqual(costs["cost_output"], 0.015)

    def test_codex_writes_replace_uncached_input(self):
        fields = {"input": 100000, "cached_input": 70000, "cache_write": 20000,
                  "output": 1000}
        costs = server.codex.compute_costs("gpt-6.1-sol", TS, fields)
        self.assertAlmostEqual(costs["cost"], 0.02 + 0.007 + 0.05 + 0.01)
        self.assertAlmostEqual(costs["cost_no_cache"], 0.21)
        for tier, multiplier in {"fast": 2, "priority": 2, "ultrafast": 6,
                                 "batch": 0.5, "flex": 0.5}.items():
            with self.subTest(tier=tier):
                tier_costs = server.codex.compute_costs("gpt-6.1-sol", TS,
                                                       {**fields, "service_tier": tier})
                self.assertAlmostEqual(tier_costs["cost"], costs["cost"] * multiplier)

    def test_dated_schedules_keep_historical_rates(self):
        pricing = {"models": {"example": [
            {"from": None, "input": 5, "output": 25},
            {"from": "2026-10-01", "input": 4, "output": 20},
        ]}, "prefix_fallback": [], "unknown_default": {"input": 3, "output": 15}}
        self.assertEqual(resolve_rate(pricing, "example", "2026-09-30T23:59:59Z")["input"], 5)
        self.assertEqual(resolve_rate(pricing, "example", "2026-10-01T00:00:00Z")["input"], 4)
        self.assertEqual(resolve_rate(pricing, "example", None)["input"], 5)

    def test_scan_and_database_repricing_preserve_cache_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            records = [
                {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
                {"type": "event_msg", "timestamp": TS, "payload": {
                    "type": "token_count", "info": {"last_token_usage": {
                        "input_tokens": 100000, "cached_input_tokens": 70000,
                        "cache_write_input_tokens": 20000, "output_tokens": 1000}}}},
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            with patch.object(server, "DB_PATH", str(Path(directory) / "usage.sqlite3")):
                server.init_db()
                with server.get_conn() as conn:
                    conn.row_factory = sqlite3.Row
                    server.codex.scan_file(conn, str(path), {})
                    row = conn.execute("SELECT * FROM messages").fetchone()
                    self.assertEqual(row["cache_5m_tokens"], 20000)
                    self.assertAlmostEqual(row["cost_cache_write"], 0.05)
                    conn.execute("UPDATE messages SET cost = 99, cost_cache_write = 99")
                    server.ensure_pricing_current(conn)
                    row = conn.execute("SELECT * FROM messages").fetchone()
                    self.assertAlmostEqual(row["cost"], 0.087)
                    self.assertAlmostEqual(row["cost_cache_write"], 0.05)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
