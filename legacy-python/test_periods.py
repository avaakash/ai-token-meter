"""Usage-in-period totals. Run with python3 -B -m unittest test_periods."""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import server


NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)


class PeriodTotalsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db_patch = patch.object(server, "DB_PATH", str(Path(directory.name) / "usage.sqlite3"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        server.init_db()

    def insert(self, conn, message_id, timestamp, source="claude", session_id="s1", cost=1.0):
        conn.execute(
            """INSERT INTO messages (message_id, source, timestamp, session_id, input_tokens,
                   output_tokens, cache_read_tokens, cache_5m_tokens, cache_1h_tokens, cost)
               VALUES (?, ?, ?, ?, 10, 20, 30, 4, 6, ?)""",
            (message_id, source, timestamp, session_id, cost),
        )

    def totals(self, source="all"):
        with server.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            return server.build_period_totals(conn, source, now=NOW)

    def test_rolling_windows_include_only_messages_since_cutoff(self):
        with server.get_conn() as conn:
            self.insert(conn, "a", "2026-10-16T11:00:00.000Z", session_id="s1")
            self.insert(conn, "b", "2026-10-15T12:00:00.000Z", session_id="s1")  # exactly 24h ago
            self.insert(conn, "c", "2026-10-15T11:59:59.999Z", session_id="s2")  # just outside 1d
            self.insert(conn, "d", "2026-10-01T00:00:00.000Z", session_id="s3")
            self.insert(conn, "e", "2025-10-01T00:00:00.000Z", session_id="s4")  # older than 1y
            conn.commit()

        periods = self.totals()
        self.assertEqual([k for k, _ in server.USAGE_PERIODS], list(periods))
        self.assertEqual(periods["1d"]["message_count"], 2)
        self.assertEqual(periods["1d"]["session_count"], 1)
        self.assertEqual(periods["3d"]["message_count"], 3)
        self.assertEqual(periods["2w"]["message_count"], 3)
        self.assertEqual(periods["1m"]["message_count"], 4)
        self.assertEqual(periods["1y"]["message_count"], 4)
        self.assertEqual(periods["1y"]["session_count"], 3)
        self.assertEqual(periods["1d"]["since"], "2026-10-15T12:00:00.000Z")
        self.assertEqual(periods["1d"]["cache_write_tokens"], 20)
        self.assertAlmostEqual(periods["1m"]["cost"], 4.0)

    def test_source_filter(self):
        with server.get_conn() as conn:
            self.insert(conn, "a", "2026-10-16T11:00:00.000Z", source="claude", cost=1.0)
            self.insert(conn, "b", "2026-10-16T11:00:00.000Z", source="codex", cost=2.0)
            conn.commit()

        self.assertAlmostEqual(self.totals("codex")["7d"]["cost"], 2.0)
        self.assertAlmostEqual(self.totals("all")["7d"]["cost"], 3.0)

    def test_empty_period_returns_zeros(self):
        periods = self.totals()
        self.assertEqual(periods["7d"]["cost"], 0)
        self.assertEqual(periods["7d"]["message_count"], 0)


if __name__ == "__main__":
    unittest.main()
