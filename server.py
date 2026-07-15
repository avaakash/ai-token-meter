#!/usr/bin/env python3
"""Local dashboard for Claude Code + Codex CLI token usage & estimated cost.

Reads Claude Code's local session transcripts (~/.claude/projects/**/*.jsonl)
and Codex CLI's local session logs (~/.codex/sessions/**/*.jsonl +
~/.codex/archived_sessions/*.jsonl), incrementally indexes token usage into a
local SQLite cache, and serves a small self-refreshing HTML dashboard plus a
JSON API. Source-specific parsing/pricing/rate-limit logic lives in claude.py
and codex.py — this file is the source-agnostic plumbing that dispatches to
whichever of those a request asks for (see SOURCES below).

Runs entirely on localhost. No data leaves the machine.
"""

import glob
import hashlib
import http.server
import json
import os
import sqlite3
import socketserver
import sys
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

HOME = os.path.expanduser("~")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "usage.sqlite3")
HTML_PATH = os.path.join(APP_DIR, "index.html")
SESSIONS_HTML_PATH = os.path.join(APP_DIR, "sessions.html")
PORT = int(os.environ.get("CLAUDE_USAGE_DASHBOARD_PORT", "4870"))

# claude.py / codex.py both do `import server` to reach the shared helpers
# below (get_conn, _db_lock, _parse_ts, day_fields, project_name_from_cwd,
# create_limit_snapshot). Whether this file is run as a script (__main__) or
# imported normally, force sys.modules['server'] to be THIS module object
# before importing them — otherwise Python would load a second, separate
# copy of this file under the name 'server' when they `import server`,
# splitting _db_lock/DB state across two module instances.
sys.modules["server"] = sys.modules[__name__]

import claude
import codex

SOURCES = {claude.SOURCE_NAME: claude, codex.SOURCE_NAME: codex}


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def day_fields(ts):
    """Returns (day, weekday, hour) in the machine's local timezone, derived
    from a single conversion so all three always agree with each other."""
    dt = _parse_ts(ts)
    if dt is None:
        return "unknown", None, None
    local_dt = dt.astimezone()
    return local_dt.strftime("%Y-%m-%d"), local_dt.weekday(), local_dt.hour


WORKTREE_PATH_MARKERS = ("/.claude/worktrees/", "/.worktrees/")


def project_name_from_cwd(cwd):
    """Groups a cwd under the repo it belongs to, not the exact directory —
    a worktree checkout (<repo>/.claude/worktrees/<name>[/<subpath>], or the
    older <repo>/.worktrees/<name>[/<subpath>] convention) is the same
    project as the repo itself, and a subpath within one shouldn't fragment
    into its own project just because that's the basename."""
    if not cwd:
        return "unknown"
    normalized = cwd.rstrip("/")
    for marker in WORKTREE_PATH_MARKERS:
        idx = normalized.find(marker)
        if idx != -1:
            normalized = normalized[:idx]
            break
    return os.path.basename(normalized) or normalized


# ---------------------------------------------------------------------------
# SQLite cache + incremental log indexing
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()

# Bump this whenever the `messages` table schema changes. On a version bump,
# the cache is dropped and rebuilt from the (untouched) source log files.
SCHEMA_VERSION = 6


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_columns(conn, table, columns):
    """Non-destructively add any missing columns (name -> SQL type) to `table`.
    Used for derived/additive columns that can be backfilled or safely
    defaulted, so adding one never requires a full log re-scan."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, sql_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def init_db():
    with get_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        current_version = int(row[0]) if row else 0
        if current_version < SCHEMA_VERSION:
            conn.execute("DROP TABLE IF EXISTS messages")
            conn.execute("DROP TABLE IF EXISTS file_state")
            conn.execute("DROP TABLE IF EXISTS session_titles")
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'claude',
                model TEXT,
                timestamp TEXT,
                day TEXT,
                weekday INTEGER,
                hour INTEGER,
                project TEXT,
                cwd TEXT,
                session_id TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_5m_tokens INTEGER,
                cache_1h_tokens INTEGER,
                reasoning_output_tokens INTEGER,
                service_tier TEXT,
                cost REAL,
                cost_no_cache REAL,
                cost_input REAL,
                cost_output REAL,
                cost_cache REAL,
                cost_cache_read REAL,
                cost_cache_write REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_state (
                path TEXT PRIMARY KEY,
                mtime REAL,
                size INTEGER,
                offset INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_titles (
                session_id TEXT PRIMARY KEY,
                ai_title TEXT,
                custom_title TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS limit_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_type TEXT NOT NULL,
                percentage REAL NOT NULL,
                dollar_value REAL NOT NULL,
                window_start TEXT,
                window_end TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        # Not dropped on schema bump — this is user-observed history that
        # can't be regenerated from logs. Additive column only.
        _ensure_columns(conn, "limit_snapshots", {"source": "TEXT NOT NULL DEFAULT 'claude'"})
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remote_usage_polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                polled_at TEXT NOT NULL,
                five_hour_pct REAL,
                five_hour_resets_at TEXT,
                weekly_pct REAL,
                weekly_resets_at TEXT,
                raw_json TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_day ON messages(day)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_model ON messages(model)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_weekday_hour ON messages(weekday, hour)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_limit_snapshots_type ON limit_snapshots(source, window_type, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_remote_usage_polls_polled_at ON remote_usage_polls(polled_at)"
        )
        conn.commit()


def scan_all():
    with _db_lock:
        with get_conn() as conn:
            cur = conn.execute("SELECT path, mtime, size, offset FROM file_state")
            state = {
                row[0]: {"mtime": row[1], "size": row[2], "offset": row[3]}
                for row in cur.fetchall()
            }
            for module in SOURCES.values():
                for pattern in module.LOG_GLOBS:
                    for path in glob.glob(pattern, recursive=True):
                        module.scan_file(conn, path, state)
            ensure_day_fields_current(conn)
            ensure_project_field_current(conn)
            ensure_pricing_current(conn)
            conn.commit()


def recompute_all_costs(conn):
    """Re-derive cost* columns for every row from its stored (source, model,
    timestamp, tokens) using each source's own current pricing file — no log
    file access needed. Safe to run any time a pricing file changes: each
    message re-resolves its own rate period from its own timestamp, so
    historical amounts only change if the rate period that actually applied
    to them changed."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT message_id, source, model, timestamp, input_tokens, output_tokens,
               cache_read_tokens, cache_5m_tokens, cache_1h_tokens,
               reasoning_output_tokens, service_tier
        FROM messages
        """
    ).fetchall()
    updates = []
    for row in rows:
        module = SOURCES.get(row["source"])
        if module is None:
            continue
        token_fields = module.token_fields_from_row(row)
        costs = module.compute_costs(row["model"], row["timestamp"], token_fields)
        updates.append((
            costs["cost"], costs["cost_no_cache"], costs["cost_input"], costs["cost_output"],
            costs["cost_cache"], costs["cost_cache_read"], costs["cost_cache_write"], row["message_id"],
        ))
    if updates:
        conn.executemany(
            """
            UPDATE messages
            SET cost = ?, cost_no_cache = ?, cost_input = ?, cost_output = ?, cost_cache = ?,
                cost_cache_read = ?, cost_cache_write = ?
            WHERE message_id = ?
            """,
            updates,
        )


def ensure_pricing_current(conn):
    combined_hash = hashlib.sha256(
        "".join(m.pricing_file_hash() for m in SOURCES.values()).encode()
    ).hexdigest()
    row = conn.execute("SELECT value FROM meta WHERE key = 'pricing_hash'").fetchone()
    stored_hash = row[0] if row else None
    needs_recompute = stored_hash != combined_hash
    if not needs_recompute:
        null_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE cost_cache_read IS NULL"
        ).fetchone()[0]
        needs_recompute = null_count > 0
    if not needs_recompute:
        return
    recompute_all_costs(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('pricing_hash', ?)",
        (combined_hash,),
    )


def recompute_day_fields(conn):
    """Re-derive day/weekday/hour for every row from its stored timestamp
    using day_fields() — DB-only, no log file access needed."""
    rows = conn.execute("SELECT message_id, timestamp FROM messages").fetchall()
    updates = [(*day_fields(ts), message_id) for message_id, ts in rows]
    if updates:
        conn.executemany(
            "UPDATE messages SET day = ?, weekday = ?, hour = ? WHERE message_id = ?",
            updates,
        )


# Bump this if day_fields()'s logic ever changes (e.g. a different local-time
# rule). On a bump, every row's day/weekday/hour is recomputed from its
# already-stored timestamp — no log re-scan needed.
DAY_FIELDS_VERSION = 1


def ensure_day_fields_current(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = 'day_fields_version'").fetchone()
    stored_version = int(row[0]) if row else 0
    if stored_version >= DAY_FIELDS_VERSION:
        return
    recompute_day_fields(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('day_fields_version', ?)",
        (str(DAY_FIELDS_VERSION),),
    )


def recompute_project_field(conn):
    """Re-derive `project` for every row from its stored `cwd` using
    project_name_from_cwd() — DB-only, no log file access needed."""
    rows = conn.execute("SELECT message_id, cwd FROM messages").fetchall()
    updates = [(project_name_from_cwd(cwd), message_id) for message_id, cwd in rows]
    if updates:
        conn.executemany(
            "UPDATE messages SET project = ? WHERE message_id = ?",
            updates,
        )


# Bump this if project_name_from_cwd()'s grouping logic ever changes. On a
# bump, every row's `project` is recomputed from its already-stored `cwd` —
# no log re-scan needed.
PROJECT_FIELD_VERSION = 2


def ensure_project_field_current(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = 'project_field_version'").fetchone()
    stored_version = int(row[0]) if row else 0
    if stored_version >= PROJECT_FIELD_VERSION:
        return
    recompute_project_field(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('project_field_version', ?)",
        (str(PROJECT_FIELD_VERSION),),
    )


# ---------------------------------------------------------------------------
# Rate-limit windows, generalized across sources
# ---------------------------------------------------------------------------


def _new_bucket(start, end):
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost": 0.0,
        "cost_input": 0.0,
        "cost_output": 0.0,
        "cost_cache_read": 0.0,
        "cost_cache_write": 0.0,
        "message_count": 0,
    }


def sum_window(conn, source, window_start, window_end, day_lookback_days, bucket_period, num_buckets):
    """Sum tokens/cost (with per-token-type cost, and a per-model breakdown)
    for `source`'s messages within [window_start, window_end), plus a
    fixed-size series of `num_buckets` sub-windows of `bucket_period` each,
    starting at window_start, for sparkline-style charts.

    Uses the `day` index to pull only a small recent slice, then filters
    precisely against the exact window boundary using each row's own
    timestamp."""
    day_cutoff = (window_start - timedelta(days=day_lookback_days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        SELECT timestamp, model, input_tokens, output_tokens, cache_read_tokens,
               cache_5m_tokens, cache_1h_tokens, cost, cost_input, cost_output,
               cost_cache_read, cost_cache_write
        FROM messages
        WHERE source = ? AND day >= ?
        """,
        (source, day_cutoff),
    ).fetchall()

    input_tokens = output_tokens = cache_read = cache_write = message_count = 0
    cost = cost_input = cost_output = cost_cache_read = cost_cache_write = 0.0
    by_model = {}
    buckets = [
        _new_bucket(window_start + i * bucket_period, window_start + (i + 1) * bucket_period)
        for i in range(num_buckets)
    ]

    for r in rows:
        dt = _parse_ts(r["timestamp"])
        if dt is None or dt < window_start or dt >= window_end:
            continue

        it = r["input_tokens"] or 0
        ot = r["output_tokens"] or 0
        cr = r["cache_read_tokens"] or 0
        cw = (r["cache_5m_tokens"] or 0) + (r["cache_1h_tokens"] or 0)
        c = r["cost"] or 0.0
        ci = r["cost_input"] or 0.0
        co = r["cost_output"] or 0.0
        ccr = r["cost_cache_read"] or 0.0
        ccw = r["cost_cache_write"] or 0.0

        input_tokens += it
        output_tokens += ot
        cache_read += cr
        cache_write += cw
        cost += c
        cost_input += ci
        cost_output += co
        cost_cache_read += ccr
        cost_cache_write += ccw
        message_count += 1

        model = r["model"] or "unknown"
        entry = by_model.setdefault(
            model, {"cost": 0.0, "input_tokens": 0, "output_tokens": 0, "message_count": 0}
        )
        entry["cost"] += c
        entry["input_tokens"] += it
        entry["output_tokens"] += ot
        entry["message_count"] += 1

        idx = int((dt - window_start) // bucket_period)
        if 0 <= idx < num_buckets:
            b = buckets[idx]
            b["input_tokens"] += it
            b["output_tokens"] += ot
            b["cache_read_tokens"] += cr
            b["cache_write_tokens"] += cw
            b["cost"] += c
            b["cost_input"] += ci
            b["cost_output"] += co
            b["cost_cache_read"] += ccr
            b["cost_cache_write"] += ccw
            b["message_count"] += 1

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cost": cost,
        "cost_input": cost_input,
        "cost_output": cost_output,
        "cost_cache_read": cost_cache_read,
        "cost_cache_write": cost_cache_write,
        "message_count": message_count,
        "by_model": by_model,
        "buckets": buckets,
    }


def _previous_window_summary(conn, source, prev_start, prev_end, day_lookback_days, bucket_period):
    usage = sum_window(
        conn, source, prev_start, prev_end, day_lookback_days=day_lookback_days,
        bucket_period=bucket_period, num_buckets=1,
    )
    return {
        "window_start": prev_start.isoformat(),
        "window_end": prev_end.isoformat(),
        "cost": usage["cost"],
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "message_count": usage["message_count"],
    }


def build_limit_windows(conn, source):
    """Ask the source's own get_remote_window() first (authoritative when
    available); fall back to get_heuristic_window(); omit the window
    entirely (None) if neither has anything — see each source module for
    what "available" means for it."""
    module = SOURCES[source]
    now = datetime.now(timezone.utc)
    remote = module.get_remote_window(conn)

    result = {
        "remote_status": remote.get("remote_status"),
        "remote_polled_at": remote.get("remote_polled_at"),
    }

    for window_type, bucket_period, num_buckets, day_lookback in (
        ("five_hour", timedelta(hours=1), 5, 2),
        ("weekly", timedelta(days=1), 7, 8),
    ):
        remote_window = remote.get(window_type)
        if remote_window:
            start, end = remote_window["start"], remote_window["end"]
            remote_pct, win_source = remote_window["remote_pct"], "remote"
        else:
            heuristic = module.get_heuristic_window(conn, window_type, now)
            if heuristic is None:
                result[window_type] = None
                continue
            start, end = heuristic
            remote_pct, win_source = None, "heuristic"

        period = end - start
        usage = sum_window(conn, source, start, end, day_lookback_days=day_lookback,
                            bucket_period=bucket_period, num_buckets=num_buckets)
        previous = _previous_window_summary(conn, source, start - period, start,
                                             day_lookback_days=day_lookback + 1, bucket_period=period)

        result[window_type] = {
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "seconds_remaining": max(0.0, (end - now).total_seconds()),
            "source": win_source,
            "remote_pct": remote_pct,
            "previous": previous,
            "credit_estimate": estimate_credit_limit(conn, source, window_type),
            **usage,
        }

    return result


def create_limit_snapshot(source, window_type, percentage):
    """Pair a source's own reported plan-usage percentage for a poll with the
    cost we've locally computed for that same window at that moment, and
    store both. Neither value is derived from the other — the point is to
    accumulate (percentage, dollar) pairs over time to see how they relate.

    A 0% reading paired with nonzero local cost is a contradiction (0% used
    should mean ~$0 spent in that window) rather than a real data point —
    seen in practice when a poll briefly reports 0 while the window's
    resets_at hasn't actually rolled over yet. Such rows are dropped rather
    than stored, since they'd corrupt the %-to-$ estimate."""
    if window_type not in ("five_hour", "weekly"):
        raise ValueError("window_type must be 'five_hour' or 'weekly'")
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")
    try:
        percentage = float(percentage)
    except (TypeError, ValueError):
        raise ValueError("percentage must be a number")
    if not (0 <= percentage <= 100):
        raise ValueError("percentage must be between 0 and 100")

    scan_all()
    with _db_lock:
        with get_conn() as conn:
            conn.row_factory = sqlite3.Row
            window = build_limit_windows(conn, source)[window_type]
            if window is None:
                return {"saved": False, "reason": "no active window to pair with"}
            dollar_value = window["cost"]

            if percentage == 0 and dollar_value > 0:
                return {"saved": False, "reason": "0% with nonzero local cost — discarded as invalid"}

            created_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO limit_snapshots
                (source, window_type, percentage, dollar_value, window_start, window_end, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (source, window_type, percentage, dollar_value, window["window_start"], window["window_end"], created_at),
            )
            conn.commit()

    return {
        "saved": True,
        "source": source,
        "window_type": window_type,
        "percentage": percentage,
        "dollar_value": dollar_value,
        "window_start": window["window_start"],
        "window_end": window["window_end"],
        "created_at": created_at,
    }


CREDIT_ESTIMATE_MIN_POINTS = 20


def _dedupe_plateaus(rows):
    """Drop consecutive entries whose percentage didn't change from the last
    KEPT one — a flat run means no usage happened between those polls
    (idle time), so they're repeats of the same evidence, not new data
    points, and would just over-weight that level in the estimate."""
    kept = []
    last_pct = None
    for r in rows:
        if last_pct is not None and r["percentage"] == last_pct:
            continue
        kept.append(r)
        last_pct = r["percentage"]
    return kept


def estimate_credit_limit(conn, source, window_type):
    rows = conn.execute(
        """
        SELECT percentage, dollar_value, created_at FROM limit_snapshots
        WHERE source = ? AND window_type = ? ORDER BY created_at ASC
        """,
        (source, window_type),
    ).fetchall()

    deduped = _dedupe_plateaus(rows)
    if len(deduped) < CREDIT_ESTIMATE_MIN_POINTS:
        return {
            "available": False,
            "points": len(deduped),
            "points_needed": CREDIT_ESTIMATE_MIN_POINTS,
        }

    implied_totals = sorted(
        r["dollar_value"] / (r["percentage"] / 100.0) for r in deduped if r["percentage"] > 0
    )
    if not implied_totals:
        return {
            "available": False,
            "points": len(deduped),
            "points_needed": CREDIT_ESTIMATE_MIN_POINTS,
        }

    mid = len(implied_totals) // 2
    estimated_total = (
        implied_totals[mid] if len(implied_totals) % 2
        else (implied_totals[mid - 1] + implied_totals[mid]) / 2.0
    )

    latest = rows[-1]
    remaining = max(0.0, estimated_total - latest["dollar_value"])

    return {
        "available": True,
        "points": len(deduped),
        "estimated_total_dollars": estimated_total,
        "current_dollars": latest["dollar_value"],
        "current_pct": latest["percentage"],
        "remaining_dollars": remaining,
    }


def list_limit_snapshots(source=None, limit=200):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        if source:
            rows = conn.execute(
                """
                SELECT id, source, window_type, percentage, dollar_value, window_start, window_end, created_at
                FROM limit_snapshots WHERE source = ? ORDER BY created_at DESC LIMIT ?
                """,
                (source, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, source, window_type, percentage, dollar_value, window_start, window_end, created_at
                FROM limit_snapshots ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]


# ---------------------------------------------------------------------------
# Aggregation for the API response
# ---------------------------------------------------------------------------


def _where(source, *extra):
    """Builds a WHERE clause with the source filter FIRST, extra conditions
    after — callers that pass their own placeholder-bearing extra conditions
    (e.g. "day >= ?") append their own params after what this returns, so the
    source condition/param must lead in both the SQL text and the params
    tuple for the two to stay positionally aligned."""
    conditions = []
    params = []
    if source != "all":
        conditions.append("source = ?")
        params.append(source)
    conditions.extend(extra)
    if conditions:
        return "WHERE " + " AND ".join(conditions), tuple(params)
    return "", ()


def build_usage_payload(source="all"):
    scan_all()
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row

        where_sql, where_params = _where(source)

        totals = conn.execute(
            f"""
            SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(cost_no_cache), 0) AS cost_no_cache,
                COUNT(*) AS message_count,
                COUNT(DISTINCT session_id) AS session_count
            FROM messages {where_sql}
            """,
            where_params,
        ).fetchone()

        by_source = conn.execute(
            """
            SELECT source,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COUNT(*) AS message_count,
                COUNT(DISTINCT session_id) AS session_count
            FROM messages
            GROUP BY source
            """
        ).fetchall()

        by_model = conn.execute(
            f"""
            SELECT source, model,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(cost_input), 0) AS cost_input,
                COALESCE(SUM(cost_output), 0) AS cost_output,
                COALESCE(SUM(cost_cache), 0) AS cost_cache,
                COALESCE(SUM(cost_cache_read), 0) AS cost_cache_read,
                COALESCE(SUM(cost_cache_write), 0) AS cost_cache_write,
                COUNT(*) AS message_count
            FROM messages {where_sql}
            GROUP BY source, model
            ORDER BY cost DESC
            """,
            where_params,
        ).fetchall()

        day_where_sql, day_where_params = _where(source, "day != 'unknown'")
        by_day = conn.execute(
            f"""
            SELECT day,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(cost_input), 0) AS cost_input,
                COALESCE(SUM(cost_output), 0) AS cost_output,
                COALESCE(SUM(cost_cache_read), 0) AS cost_cache_read,
                COALESCE(SUM(cost_cache_write), 0) AS cost_cache_write
            FROM messages {day_where_sql}
            GROUP BY day
            ORDER BY day DESC
            LIMIT 30
            """,
            day_where_params,
        ).fetchall()

        heatmap_cutoff = (datetime.now() - timedelta(days=375)).strftime("%Y-%m-%d")
        heatmap_where_sql, heatmap_where_params = _where(source, "day != 'unknown'", "day >= ?")
        heatmap_days = conn.execute(
            f"""
            SELECT day,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COUNT(*) AS message_count
            FROM messages {heatmap_where_sql}
            GROUP BY day
            ORDER BY day ASC
            """,
            heatmap_where_params + (heatmap_cutoff,),
        ).fetchall()

        by_project = conn.execute(
            f"""
            SELECT source, project,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(cost_input), 0) AS cost_input,
                COALESCE(SUM(cost_output), 0) AS cost_output,
                COALESCE(SUM(cost_cache), 0) AS cost_cache,
                COALESCE(SUM(cost_cache_read), 0) AS cost_cache_read,
                COALESCE(SUM(cost_cache_write), 0) AS cost_cache_write,
                COUNT(*) AS message_count
            FROM messages {where_sql}
            GROUP BY source, project
            ORDER BY cost DESC
            LIMIT 15
            """,
            where_params,
        ).fetchall()

        session_cols = """
                session_id, source,
                (SELECT project FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS project,
                (SELECT cwd FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS cwd,
                MIN(timestamp) AS start_ts,
                MAX(timestamp) AS end_ts,
                COUNT(*) AS message_count,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(cost_input), 0) AS cost_input,
                COALESCE(SUM(cost_output), 0) AS cost_output,
                COALESCE(SUM(cost_cache), 0) AS cost_cache,
                COALESCE(SUM(cost_cache_read), 0) AS cost_cache_read,
                COALESCE(SUM(cost_cache_write), 0) AS cost_cache_write,
                GROUP_CONCAT(DISTINCT model) AS models,
                (SELECT COALESCE(custom_title, ai_title) FROM session_titles st WHERE st.session_id = messages.session_id) AS title
        """
        session_where_sql, session_where_params = _where(source, "session_id IS NOT NULL")

        top_sessions = conn.execute(
            f"SELECT {session_cols} FROM messages {session_where_sql} GROUP BY session_id ORDER BY cost DESC LIMIT 10",
            session_where_params,
        ).fetchall()

        last_sessions = conn.execute(
            f"SELECT {session_cols} FROM messages {session_where_sql} GROUP BY session_id ORDER BY end_ts DESC LIMIT 10",
            session_where_params,
        ).fetchall()

        top_day = conn.execute(
            f"""
            SELECT day, COALESCE(SUM(cost), 0) AS cost
            FROM messages {day_where_sql}
            GROUP BY day
            ORDER BY cost DESC
            LIMIT 1
            """,
            day_where_params,
        ).fetchone()

        hw_where_sql, hw_where_params = _where(source, "weekday IS NOT NULL", "hour IS NOT NULL")
        hour_weekday = conn.execute(
            f"""
            SELECT weekday, hour,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                COUNT(*) AS message_count
            FROM messages {hw_where_sql}
            GROUP BY weekday, hour
            """,
            hw_where_params,
        ).fetchall()

        session_spans = conn.execute(
            f"""
            SELECT session_id,
                (SELECT project FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS project,
                MIN(timestamp) AS start_ts,
                MAX(timestamp) AS end_ts, SUM(cost) AS cost, COUNT(*) AS message_count
            FROM messages {session_where_sql}
            GROUP BY session_id
            """,
            session_where_params,
        ).fetchall()
        longest_session = None
        longest_seconds = -1
        for row in session_spans:
            start = _parse_ts(row["start_ts"])
            end = _parse_ts(row["end_ts"])
            if not start or not end:
                continue
            seconds = (end - start).total_seconds()
            if seconds > longest_seconds:
                longest_seconds = seconds
                longest_session = {
                    "session_id": row["session_id"],
                    "project": row["project"],
                    "start_ts": row["start_ts"],
                    "end_ts": row["end_ts"],
                    "cost": row["cost"],
                    "message_count": row["message_count"],
                    "duration_seconds": seconds,
                }

        today = datetime.now().strftime("%Y-%m-%d")
        today_where_sql, today_where_params = _where(source, "day = ?")
        today_row = conn.execute(
            f"""
            SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cost), 0) AS cost
            FROM messages {today_where_sql}
            """,
            today_where_params + (today,),
        ).fetchone()

        limits = {name: build_limit_windows(conn, name) for name in SOURCES}

    def row_to_dict(r):
        return {k: r[k] for k in r.keys()}

    pricing = {name: {"last_verified": m.load_pricing().get("last_verified"),
                       "source": m.load_pricing().get("source")}
               for name, m in SOURCES.items()}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "pricing": pricing,
        "totals": row_to_dict(totals),
        "by_source": [row_to_dict(r) for r in by_source],
        "today": row_to_dict(today_row),
        "limits": limits,
        "by_model": [row_to_dict(r) for r in by_model],
        "by_day": [row_to_dict(r) for r in by_day],
        "heatmap_days": [row_to_dict(r) for r in heatmap_days],
        "by_project": [row_to_dict(r) for r in by_project],
        "top_sessions": [row_to_dict(r) for r in top_sessions],
        "last_sessions": [row_to_dict(r) for r in last_sessions],
        "top_day": row_to_dict(top_day) if top_day else None,
        "longest_session": longest_session,
        "hour_weekday": [row_to_dict(r) for r in hour_weekday],
    }


def find_sessions(query, source="all", limit=20):
    """Look up sessions by exact ID or ID prefix. Returns a list (usually 0 or 1 items)."""
    query = (query or "").strip()
    if not query:
        return []

    scan_all()
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        where_sql, where_params = _where(source, "session_id LIKE ? ESCAPE '\\'")
        rows = conn.execute(
            f"""
            SELECT session_id, source,
                (SELECT project FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS project,
                (SELECT cwd FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS cwd,
                MIN(timestamp) AS start_ts,
                MAX(timestamp) AS end_ts,
                COUNT(*) AS message_count,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                GROUP_CONCAT(DISTINCT model) AS models,
                (SELECT COALESCE(custom_title, ai_title) FROM session_titles st WHERE st.session_id = messages.session_id) AS title
            FROM messages
            {where_sql}
            GROUP BY session_id
            ORDER BY cost DESC
            LIMIT ?
            """,
            where_params + (query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", limit),
        ).fetchall()

        results = []
        for row in rows:
            d = {k: row[k] for k in row.keys()}
            model_rows = conn.execute(
                """
                SELECT model,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                    COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                    COALESCE(SUM(cost), 0) AS cost,
                    COUNT(*) AS message_count
                FROM messages
                WHERE session_id = ?
                GROUP BY model
                ORDER BY cost DESC
                """,
                (row["session_id"],),
            ).fetchall()
            d["by_model"] = [{k: r[k] for k in r.keys()} for r in model_rows]
            results.append(d)
        return results


def list_all_sessions(source="all"):
    """Every distinct session, unpaginated — the dataset is small enough
    (low hundreds) for the browser to sort/filter/search client-side."""
    scan_all()
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        where_sql, where_params = _where(source, "session_id IS NOT NULL")
        rows = conn.execute(
            f"""
            SELECT session_id, source,
                (SELECT project FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS project,
                (SELECT cwd FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS cwd,
                MIN(timestamp) AS start_ts,
                MAX(timestamp) AS end_ts,
                COUNT(*) AS message_count,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                GROUP_CONCAT(DISTINCT model) AS models,
                (SELECT COALESCE(custom_title, ai_title) FROM session_titles st WHERE st.session_id = messages.session_id) AS title
            FROM messages
            {where_sql}
            GROUP BY session_id
            ORDER BY end_ts DESC
            """,
            where_params,
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def _valid_source(value, allow_all=True):
    if value is None or value == "":
        return "all" if allow_all else claude.SOURCE_NAME
    if value == "all" and allow_all:
        return "all"
    if value in SOURCES:
        return value
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep stdout/log quiet

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            self._serve_file(HTML_PATH, "text/html; charset=utf-8")
        elif parsed.path in ("/sessions", "/sessions.html"):
            self._serve_file(SESSIONS_HTML_PATH, "text/html; charset=utf-8")
        elif parsed.path == "/api/usage":
            source = _valid_source(qs.get("source", [None])[0])
            if source is None:
                self._send_error_json(400, f"unknown source; expected one of: all, {', '.join(SOURCES)}")
                return
            self._send_json_result(lambda: build_usage_payload(source))
        elif parsed.path == "/api/session":
            query = qs.get("id", [""])[0]
            source = _valid_source(qs.get("source", [None])[0])
            if source is None:
                self._send_error_json(400, f"unknown source; expected one of: all, {', '.join(SOURCES)}")
                return
            self._send_json_result(lambda: {"query": query, "results": find_sessions(query, source)})
        elif parsed.path == "/api/all-sessions":
            source = _valid_source(qs.get("source", [None])[0])
            if source is None:
                self._send_error_json(400, f"unknown source; expected one of: all, {', '.join(SOURCES)}")
                return
            self._send_json_result(lambda: {"sessions": list_all_sessions(source)})
        elif parsed.path == "/api/limit-snapshots":
            source = qs.get("source", [None])[0]
            if source is not None and source not in SOURCES:
                self._send_error_json(400, f"unknown source; expected one of: {', '.join(SOURCES)}")
                return
            self._send_json_result(lambda: {"snapshots": list_limit_snapshots(source)})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/remote-usage/poll":
            source = qs.get("source", [claude.SOURCE_NAME])[0]
            if source not in SOURCES:
                self._send_error_json(400, f"unknown source; expected one of: {', '.join(SOURCES)}")
                return
            module = SOURCES[source]
            if not hasattr(module, "poll_remote_usage_once"):
                self._send_error_json(400, f"not applicable for source={source} — its data is inline in logs, not polled")
                return
            self._send_json_result(module.poll_remote_usage_once)
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json_result(self, producer):
        try:
            payload = producer()
            body = json.dumps(payload).encode("utf-8")
            status = 200
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status, message):
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self.send_response(404)
            self.end_headers()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    init_db()
    scan_all()  # warm the cache before first request
    for module in SOURCES.values():
        if hasattr(module, "start_background_poll"):
            module.start_background_poll()  # polls immediately, then every ~10min
    server_obj = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Usage dashboard running at http://127.0.0.1:{PORT}/  (Ctrl+C to stop)")
    try:
        server_obj.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
