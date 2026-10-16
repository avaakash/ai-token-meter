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
import statistics
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
                offset INTEGER,
                carry_model TEXT,
                carry_service_tier TEXT,
                carry_session_id TEXT,
                carry_cwd TEXT
            )
            """
        )
        # carry_* columns were added after the initial release — backfill them
        # onto any file_state table created before that (codex.py needs them to
        # remember model/session/cwd across incremental scans of the same file;
        # see scan_file() there for why).
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(file_state)")}
        for col in ("carry_model", "carry_service_tier", "carry_session_id", "carry_cwd"):
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE file_state ADD COLUMN {col} TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_titles (
                session_id TEXT PRIMARY KEY,
                ai_title TEXT,
                custom_title TEXT
            )
            """
        )
        # app_title: a name kept outside the transcripts (the Claude desktop
        # app's session metadata, Codex's session_index.jsonl), with
        # app_title_source "user" when the person renamed it there.
        _ensure_columns(conn, "session_titles", {
            "app_title": "TEXT", "app_title_source": "TEXT", "first_prompt": "TEXT",
        })
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
        # Not dropped on schema bump — this is user-observed history (the
        # remote-polled utilization %) that can't be regenerated from logs,
        # same rationale as limit_snapshots above.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_windows (
                source TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (source, window_start)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_daily_max_pct (
                source TEXT NOT NULL,
                window_start TEXT NOT NULL,
                day TEXT NOT NULL,
                latest_percentage REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, window_start, day)
            )
            """
        )
        # Column used to store a running MAX across polls; renamed once the
        # semantics changed to "latest observed reading" (see
        # _upsert_weekly_daily_max) — Claude's weekly quota is a rolling
        # 7-day window, so usage can legitimately decrease during the week,
        # not just rise to a hard reset. Non-destructive, safe every startup.
        existing_daily_pct_cols = {row[1] for row in conn.execute("PRAGMA table_info(weekly_daily_max_pct)").fetchall()}
        if "max_percentage" in existing_daily_pct_cols and "latest_percentage" not in existing_daily_pct_cols:
            conn.execute("ALTER TABLE weekly_daily_max_pct RENAME COLUMN max_percentage TO latest_percentage")
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
            cur = conn.execute(
                "SELECT path, mtime, size, offset, carry_model, carry_service_tier, "
                "carry_session_id, carry_cwd FROM file_state"
            )
            state = {
                row[0]: {
                    "mtime": row[1], "size": row[2], "offset": row[3],
                    "carry_model": row[4], "carry_service_tier": row[5],
                    "carry_session_id": row[6], "carry_cwd": row[7],
                }
                for row in cur.fetchall()
            }
            for module in SOURCES.values():
                for pattern in module.LOG_GLOBS:
                    for path in glob.glob(pattern, recursive=True):
                        module.scan_file(conn, path, state)
                module.sync_session_titles(conn)
            ensure_first_prompts_current(conn)
            ensure_day_fields_current(conn)
            ensure_project_field_current(conn)
            # Full pricing-hash-triggered recompute only happens at startup
            # (see main()) — it walks every row and would make whichever
            # request first observes a changed pricing file pay for a full
            # table recompute. Here we only backfill rows that have no cost
            # at all yet (should be ~0 in steady state; scan_file() computes
            # cost for rows it inserts), so page loads stay cheap.
            recompute_missing_costs(conn)
            conn.commit()


def recompute_all_costs(conn):
    """Re-derive cost* columns for every row from its stored (source, model,
    timestamp, tokens) using each source's own current pricing file — no log
    file access needed. Safe to run any time a pricing file changes: each
    message re-resolves its own rate period from its own timestamp, so
    historical amounts only change if the rate period that actually applied
    to them changed. Expensive (walks every row) — call only at startup, via
    ensure_pricing_current(); never from the per-request scan_all() path."""
    _recompute_costs(conn, where_sql="")


def recompute_missing_costs(conn):
    """Cheap per-request counterpart to recompute_all_costs(): only touches
    rows that have never been priced (cost_cache_read IS NULL). Safe to call
    on every scan_all() — normally a no-op since scan_file() prices rows as
    it inserts them."""
    _recompute_costs(conn, where_sql="WHERE cost_cache_read IS NULL")


def _recompute_costs(conn, where_sql):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT message_id, source, model, timestamp, input_tokens, output_tokens,
               cache_read_tokens, cache_5m_tokens, cache_1h_tokens,
               reasoning_output_tokens, service_tier
        FROM messages
        {where_sql}
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
    """Startup-only: full recompute of every row's cost when either pricing
    file's content has changed since last run, or a stray null cost slipped
    through. Called once from main() before the server starts serving —
    never from scan_all(), which would make it a per-request cost."""
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


# Bump this if the first-prompt rules change. On a bump, each log file is
# re-read from the top only as far as its first prompt.
FIRST_PROMPT_VERSION = 1


def ensure_first_prompts_current(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = 'first_prompt_version'").fetchone()
    if row and int(row[0]) >= FIRST_PROMPT_VERSION:
        return
    for module in SOURCES.values():
        if hasattr(module, "backfill_first_prompts"):
            module.backfill_first_prompts(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('first_prompt_version', ?)",
        (str(FIRST_PROMPT_VERSION),),
    )


def store_first_prompts(conn, rows):
    """rows: (session_id, text). Keeps the first one ever recorded."""
    conn.executemany(
        """
        INSERT INTO session_titles (session_id, first_prompt) VALUES (?, ?)
        ON CONFLICT(session_id) DO UPDATE SET first_prompt = excluded.first_prompt
        WHERE session_titles.first_prompt IS NULL
        """,
        rows,
    )


def store_app_titles(conn, rows):
    """rows: (session_id, title, source). Writes only rows that changed."""
    conn.executemany(
        """
        INSERT INTO session_titles (session_id, app_title, app_title_source) VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            app_title = excluded.app_title, app_title_source = excluded.app_title_source
        WHERE session_titles.app_title IS NOT excluded.app_title
           OR session_titles.app_title_source IS NOT excluded.app_title_source
        """,
        rows,
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


def _floor_window_key(iso_str):
    """Normalize a window_start/window_end to whole-minute resolution before
    using it as a dedup key. Confirmed empirically: the remote-reported reset
    timestamp for the same real weekly window jitters by about a second
    between polls, straddling a minute boundary — 643 polls against one real
    window (true boundary 10:00:00) came back split ~evenly between
    09:59:59.xxx and 10:00:00.xxx. A plain floor would keep that split (09:59
    vs 10:00); rounding to the nearest minute collapses both sides onto the
    same key. Without this, every poll would fragment one real window into
    its own near-duplicate weekly_windows row."""
    dt = _parse_ts(iso_str)
    if dt is None:
        return iso_str
    if dt.second >= 30:
        dt = dt + timedelta(minutes=1)
    return dt.replace(second=0, microsecond=0).isoformat()


def _validate_percentage(percentage):
    try:
        percentage = float(percentage)
    except (TypeError, ValueError):
        raise ValueError("percentage must be a number")
    if not (0 <= percentage <= 100):
        raise ValueError("percentage must be between 0 and 100")
    return percentage


def _resolve_weekly_window_key(conn, source, window_start, window_end):
    """Returns the window_start to actually file this reading under. If the
    most recently tracked window for `source` started within half a period
    of this reading's own reported window_start, treat it as the SAME window
    (return the existing key) instead of starting a new row.

    Confirmed empirically: Claude's weekly reset sits on a fixed calendar
    grid (resets_at holds steady for days, then jumps a full ~7 days at the
    real boundary) — normal jitter there is sub-minute, already collapsed by
    _floor_window_key, so this rarely even needs to kick in. Codex's is
    different: its resets_at is recomputed fresh every single poll (~7 days
    from *now*, not from a fixed anchor), so window_start creeps forward by
    exactly the ~10-minute poll interval every time — with no merge logic
    that fragments into one near-duplicate weekly_windows row per poll
    (confirmed: 26 rows accumulated in about a day). Since there's no real
    weekly cycle to align to for a source like that, this collapses it into
    one continuously-extending "current" entry instead — the honest
    representation, and what keeps historical-week comparisons meaningful
    for sources that DO have real cycles."""
    latest = conn.execute(
        """
        SELECT window_start, window_end FROM weekly_windows
        WHERE source = ? ORDER BY window_start DESC LIMIT 1
        """,
        (source,),
    ).fetchone()
    if latest is None:
        return window_start

    latest_start = _parse_ts(latest["window_start"])
    new_start = _parse_ts(window_start)
    new_end = _parse_ts(window_end)
    if latest_start is None or new_start is None or new_end is None:
        return window_start

    period = new_end - new_start
    if abs(new_start - latest_start) <= period / 2:
        return latest["window_start"]
    return window_start


def _upsert_weekly_daily_max(conn, source, percentage, window_start, window_end, observed_at):
    """Write half of record_weekly_daily_max, taking an already-open `conn`
    so callers doing many writes (backfill_weekly_usage_from_snapshots) can
    share one connection instead of opening one per row — `with get_conn()`
    commits/rolls back on exit but does NOT close the connection, so a tight
    loop calling get_conn() per iteration leaks a file descriptor per call
    and can exhaust the process's fd limit (confirmed: backfilling 643
    historical rows this way crashed the dashboard under launchd's default
    limit, which is far lower than an interactive shell's).

    Stores the LATEST reading per day, not a running max: usage can
    legitimately go DOWN within a window, not just rise until a reset
    (confirmed on Claude: observed 7% in the morning drop to 4% by evening
    within the same fixed window — a running max would have frozen at 7%
    and never reflected that). The WHERE guard only lets a write through if
    it's at least as recent as what's already stored — live polls and the
    backfill replay both process rows chronologically already, so this is a
    defensive backstop against an out-of-order call, not the primary
    ordering mechanism — and it's also what makes replaying (self-healing)
    old max-corrupted rows during a backfill converge on the correct latest
    value instead of getting stuck on the inflated one."""
    window_start = _floor_window_key(window_start)
    window_end = _floor_window_key(window_end)
    day, _, _ = day_fields(observed_at)

    effective_start = _resolve_weekly_window_key(conn, source, window_start, window_end)

    conn.execute(
        """
        INSERT INTO weekly_windows (source, window_start, window_end, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source, window_start) DO UPDATE SET
            window_end = excluded.window_end
        WHERE excluded.window_end > weekly_windows.window_end
        """,
        (source, effective_start, window_end, observed_at),
    )
    conn.execute(
        """
        INSERT INTO weekly_daily_max_pct (source, window_start, day, latest_percentage, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source, window_start, day) DO UPDATE SET
            latest_percentage = excluded.latest_percentage,
            updated_at = excluded.updated_at
        WHERE excluded.updated_at >= weekly_daily_max_pct.updated_at
        """,
        (source, effective_start, day, percentage, observed_at),
    )


def record_weekly_daily_max(source, percentage, window_start, window_end, observed_at=None):
    """Track, per weekly-limit window, the highest utilization % observed on
    each calendar day within it. `percentage` only exists as remote-polled
    data — there's no way to derive or backfill it from local session/cost
    data — so this must be called with a live reading, paired with the
    (window_start, window_end) that reading belongs to (as returned by
    create_limit_snapshot, so both stay pinned to the same window).

    `observed_at` (ISO timestamp) defaults to now — pass the reading's own
    original timestamp instead when replaying history (see
    backfill_weekly_usage_from_snapshots), so the day it lands on is the day
    it was actually observed, not the day the backfill happened to run."""
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")
    percentage = _validate_percentage(percentage)
    if observed_at is None:
        observed_at = datetime.now(timezone.utc).isoformat()

    with _db_lock:
        conn = get_conn()
        try:
            conn.row_factory = sqlite3.Row
            _upsert_weekly_daily_max(conn, source, percentage, window_start, window_end, observed_at)
            conn.commit()
        finally:
            conn.close()


def backfill_weekly_usage_from_snapshots(source):
    """Replay a source's own limit_snapshots history (window_type='weekly')
    into weekly_windows/weekly_daily_max_pct. Covers readings recorded before
    daily-max tracking existed — Claude has been polling and storing weekly
    snapshots for a while; Codex only gained the same wiring alongside this
    feature, so it has no such backlog (yet).

    Idempotent: re-applies the same MAX upserts every time, so it's cheap and
    safe to call unconditionally on every server start rather than needing a
    one-shot migration flag. Uses a single connection for the whole replay
    (can be hundreds of rows) rather than one per row — see
    _upsert_weekly_daily_max's docstring for why that matters."""
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")
    with _db_lock:
        conn = get_conn()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT percentage, window_start, window_end, created_at
                FROM limit_snapshots
                WHERE source = ? AND window_type = 'weekly'
                ORDER BY created_at ASC
                """,
                (source,),
            ).fetchall()
            for row in rows:
                percentage = _validate_percentage(row["percentage"])
                _upsert_weekly_daily_max(
                    conn, source, percentage, row["window_start"], row["window_end"], row["created_at"]
                )
            conn.commit()
        finally:
            conn.close()


def list_weekly_usage(source=None, limit=20):
    """Every tracked weekly window (most recent first), each with its
    per-day utilization high-water-marks nested underneath (oldest day
    first)."""
    if source is not None and source not in SOURCES:
        raise ValueError(f"unknown source: {source}")

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        # Collapsed per source BEFORE the limit is applied: a fragmenting
        # source (see _collapse_overlapping_windows) would otherwise fill the
        # whole list with near-duplicate rows covering one real week, which is
        # what made the weekly column show the same date several times over.
        if source:
            rows = conn.execute(
                """
                SELECT source, window_start, window_end, created_at
                FROM weekly_windows WHERE source = ? ORDER BY window_start DESC
                """,
                (source,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT source, window_start, window_end, created_at
                FROM weekly_windows ORDER BY window_start DESC
                """
            ).fetchall()

        now = datetime.now(timezone.utc)
        by_source = {}
        for r in rows:
            by_source.setdefault(r["source"], []).append(r)
        windows = []
        for src_rows in by_source.values():
            windows.extend(_collapse_overlapping_windows(src_rows, now)[:limit])
        windows.sort(key=lambda r: r["window_start"], reverse=True)
        windows = windows[:limit]

        result = []
        for w in windows:
            days = conn.execute(
                """
                SELECT day, latest_percentage, updated_at
                FROM weekly_daily_max_pct
                WHERE source = ? AND window_start = ?
                ORDER BY day ASC
                """,
                (w["source"], w["window_start"]),
            ).fetchall()
            daily_max = [
                {"day": d["day"], "latest_percentage": d["latest_percentage"], "updated_at": d["updated_at"]}
                for d in days
            ]
            # From the raw poll history rather than the daily table: a
            # collapsed window's own readings are filed under whichever
            # fragment key was current when each was recorded, so the daily
            # rows under this key can be empty for a week that was heavily
            # used. None (not 0) when there were no polls at all, so callers
            # can still tell "unused" from "unknown".
            peak = conn.execute(
                """
                SELECT MAX(percentage) AS peak FROM limit_snapshots
                WHERE source = ? AND window_type = 'weekly'
                  AND created_at >= ? AND created_at < ?
                """,
                (w["source"], w["window_start"], w["window_end"]),
            ).fetchone()["peak"]
            if peak is None and daily_max:
                peak = daily_max[-1]["latest_percentage"]
            result.append({
                "source": w["source"],
                "window_start": w["window_start"],
                "window_end": w["window_end"],
                "created_at": w["created_at"],
                "daily_max": daily_max,
                "week_max_percentage": peak,
            })
        return result


def get_weekly_usage_window(source, window_start):
    """Single weekly window lookup by its exact key, with nested daily
    high-water-marks. Returns None if that window has never been recorded."""
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        w = conn.execute(
            """
            SELECT source, window_start, window_end, created_at
            FROM weekly_windows WHERE source = ? AND window_start = ?
            """,
            (source, window_start),
        ).fetchone()
        if w is None:
            return None
        days = conn.execute(
            """
            SELECT day, latest_percentage, updated_at
            FROM weekly_daily_max_pct
            WHERE source = ? AND window_start = ?
            ORDER BY day ASC
            """,
            (source, window_start),
        ).fetchall()
        daily_max = [
            {"day": d["day"], "latest_percentage": d["latest_percentage"], "updated_at": d["updated_at"]}
            for d in days
        ]
        return {
            "source": w["source"],
            "window_start": w["window_start"],
            "window_end": w["window_end"],
            "created_at": w["created_at"],
            "daily_max": daily_max,
            "week_max_percentage": daily_max[-1]["latest_percentage"] if daily_max else None,
        }


MIN_COMPLETED_WEEKS_FOR_HISTORY = 3
MIN_CURRENT_WEEK_DAYS_FOR_PACE = 2
MIN_ELAPSED_HOURS_FOR_PACE = 18


def predict_weekly_usage(source):
    """Predict how much of the weekly limit % a source is on track to use,
    built entirely from Anthropic's/OpenAI's own polled percentage history
    (weekly_daily_max_pct) — never from local $ cost, which only sees this
    machine's CLI activity and would silently undercount any other surface
    (e.g. Claude web chat, Cowork) that doesn't write local logs but does
    count against the same account-wide quota.

    Two independent signals, both gated on a minimum amount of data so an
    early/noisy reading never gets reported as a confident number:

      - historical_avg_percentage: once >= MIN_COMPLETED_WEEKS_FOR_HISTORY
        full weeks have been observed, the average of each week's final
        (most recent) reading — "how much you typically end up using."
      - pace_projection_percentage: the current in-progress week's latest
        reading scaled up by how much of the 7-day window has elapsed so
        far — "at this rate, you'd end up around X% for the week."
        Requires both a minimum number of distinct days observed AND a
        minimum elapsed time, since scaling up a reading from just a few
        hours into the window would wildly overreact to normal noise.

    Neither is a guess when data is thin: both are None (not 0 or a wild
    number) until their own minimum is met."""
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")

    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        windows = conn.execute(
            """
            SELECT window_start, window_end FROM weekly_windows
            WHERE source = ? ORDER BY window_start DESC
            """,
            (source,),
        ).fetchall()

        if not windows:
            return {
                "available": False,
                "historical_avg_percentage": None,
                "historical_weeks_used": 0,
                "historical_weeks_needed": MIN_COMPLETED_WEEKS_FOR_HISTORY,
                "pace_projection_percentage": None,
                "current_percentage": None,
                "current_window_start": None,
                "current_window_end": None,
            }

        now = datetime.now(timezone.utc)

        # The one window (if any) still in progress; everything else has
        # fully played out and is eligible for the historical average.
        # Overlapping rows are collapsed first — a source whose reported reset
        # time is recomputed from "now" on every poll (Codex) accumulates one
        # near-duplicate window row per poll, and counting each as a separate
        # completed week made the "typical week" average meaningless
        # (confirmed: 1363 "weeks" averaging 0.3%, since almost every fragment
        # covers a sliver of one real week).
        current = None
        completed = []
        for w in _collapse_overlapping_windows(windows, now):
            w_end = _parse_ts(w["window_end"])
            if current is None and w_end is not None and w_end > now:
                current = w
            else:
                completed.append(w)

        # Each completed week's peak reading, read straight from the raw poll
        # history rather than from weekly_daily_max_pct. That table is keyed by
        # the window_start a reading was filed under, so for a fragmenting
        # source its rows scatter across hundreds of near-duplicate keys and a
        # collapsed window finds almost none of its own readings under its own
        # key (confirmed: real weeks that peaked above 50% looked like 0%).
        # Peak rather than latest, because the reading right at a reset rolls
        # back to 0 and that artifact would otherwise become the week's total.
        historical_finals = []
        for w in completed:
            row = conn.execute(
                """
                SELECT MAX(percentage) AS peak FROM limit_snapshots
                WHERE source = ? AND window_type = 'weekly'
                  AND created_at >= ? AND created_at < ?
                """,
                (source, w["window_start"], w["window_end"]),
            ).fetchone()
            if row is not None and row["peak"] is not None:
                historical_finals.append(row["peak"])

        historical_avg = (
            sum(historical_finals) / len(historical_finals)
            if len(historical_finals) >= MIN_COMPLETED_WEEKS_FOR_HISTORY
            else None
        )

        pace_projection = None
        current_percentage = None
        current_window_start = current["window_start"] if current else None
        current_window_end = current["window_end"] if current else None

        if current is not None:
            current_days = conn.execute(
                """
                SELECT day, latest_percentage FROM weekly_daily_max_pct
                WHERE source = ? AND window_start = ? ORDER BY day ASC
                """,
                (source, current["window_start"]),
            ).fetchall()

            if current_days:
                current_percentage = current_days[-1]["latest_percentage"]
                current_start = _parse_ts(current["window_start"])
                current_end = _parse_ts(current["window_end"])
                if current_start is not None and current_end is not None:
                    elapsed = (now - current_start).total_seconds()
                    total = (current_end - current_start).total_seconds()
                    elapsed_hours = elapsed / 3600.0
                    if (
                        len(current_days) >= MIN_CURRENT_WEEK_DAYS_FOR_PACE
                        and elapsed_hours >= MIN_ELAPSED_HOURS_FOR_PACE
                        and elapsed > 0
                    ):
                        pace_projection = min(100.0, current_percentage / elapsed * total)

    return {
        "available": historical_avg is not None or pace_projection is not None,
        "historical_avg_percentage": historical_avg,
        "historical_weeks_used": len(historical_finals),
        "historical_weeks_needed": MIN_COMPLETED_WEEKS_FOR_HISTORY,
        "pace_projection_percentage": pace_projection,
        "current_percentage": current_percentage,
        "current_window_start": current_window_start,
        "current_window_end": current_window_end,
        "exhaustion": predict_weekly_exhaustion(source),
    }


# ---------------------------------------------------------------------------
# "When will I run out?" — projected exhaustion time for the weekly window.
#
# Built off limit_snapshots (one remote-reported % reading per ~10-minute
# poll) rather than weekly_daily_max_pct (one reading per day): an ETA needs
# sub-day resolution, and the snapshot table already holds it.
#
# The naive model — scale current % by elapsed wall-clock fraction — is
# badly biased, because usage is bursty and follows a working rhythm (nights
# and weekends contribute nearly nothing). Backtested over the weekly history
# in this DB it overshoots badly early in the week. So instead we learn the
# SHAPE of a typical week: the median cumulative share of a week's final
# usage reached by each elapsed hour. Projecting through that shape, and then
# walking it forward to find where the curve would cross 100%, was ~40% more
# accurate than flat scaling in backtest (see typical_error_pp, which is
# recomputed from the user's own history rather than hardcoded).
# ---------------------------------------------------------------------------

EXHAUSTION_MIN_REFERENCE_WEEKS = 3
EXHAUSTION_MIN_ELAPSED_HOURS = 12    # before this, the shape denominator is tiny and the projection is noise
EXHAUSTION_MIN_SHAPE_FRACTION = 0.05
EXHAUSTION_MIN_WEEK_FINAL_PCT = 5    # a barely-used week teaches nothing about pacing
EXHAUSTION_MIN_WEEK_COVERAGE_HOURS = 100
EXHAUSTION_SHAPE_HOURS = 168


def _weekly_snapshot_series(conn, source):
    """One entry per tracked weekly window: its cleaned (timestamp, %) series.

    Readings below the window's running max are dropped — at the reset
    instant the remote API briefly reports 0% against the not-yet-rolled
    window (see create_limit_snapshot), and a stale low reading mid-series
    would otherwise look like the quota went backwards."""
    windows = conn.execute(
        """
        SELECT window_start, window_end FROM weekly_windows
        WHERE source = ? ORDER BY window_start ASC
        """,
        (source,),
    ).fetchall()

    series = []
    for w in windows:
        start, end = _parse_ts(w["window_start"]), _parse_ts(w["window_end"])
        if start is None or end is None:
            continue
        rows = conn.execute(
            """
            SELECT created_at, percentage FROM limit_snapshots
            WHERE source = ? AND window_type = 'weekly'
              AND created_at >= ? AND created_at < ?
            ORDER BY created_at ASC
            """,
            (source, w["window_start"], w["window_end"]),
        ).fetchall()

        points, running_max = [], 0.0
        for r in rows:
            ts = _parse_ts(r["created_at"])
            if ts is None or r["percentage"] is None or r["percentage"] < running_max:
                continue
            running_max = r["percentage"]
            points.append((ts, r["percentage"]))
        if points:
            series.append({
                "start": start,
                "end": end,
                "points": points,
                "final": points[-1][1],
                "coverage_hours": (points[-1][0] - start).total_seconds() / 3600.0,
            })
    return series


def _collapse_overlapping(items, start_of, end_of, anchor=None):
    """Reduce a set of time windows to genuinely separate ones, newest first,
    by walking backwards and skipping anything that overlaps what's already
    been kept. `anchor`, when given, is always kept as the newest entry.

    Sources whose reported reset time is recomputed from "now" on every poll
    (Codex) accumulate one near-duplicate window row per poll — see
    _resolve_weekly_window_key — and all of those rows cover the same
    underlying week. Without this, one real week gets counted hundreds of
    times."""
    ordered = sorted(items, key=start_of, reverse=True)
    kept = []
    boundary = None
    if anchor is not None:
        kept.append(anchor)
        boundary = start_of(anchor)
    for item in ordered:
        if anchor is not None and item is anchor:
            continue
        if boundary is not None and end_of(item) > boundary:
            continue
        kept.append(item)
        boundary = start_of(item)
    return kept


def _collapse_overlapping_windows(rows, now):
    """_collapse_overlapping for raw weekly_windows rows, anchored on the
    window still in progress so the current week is never dropped in favour
    of an overlapping fragment."""
    usable = [r for r in rows if _parse_ts(r["window_start"]) and _parse_ts(r["window_end"])]
    anchor = next(
        (r for r in sorted(usable, key=lambda r: _parse_ts(r["window_start"]), reverse=True)
         if _parse_ts(r["window_end"]) > now),
        None,
    )
    return _collapse_overlapping(
        usable,
        start_of=lambda r: _parse_ts(r["window_start"]),
        end_of=lambda r: _parse_ts(r["window_end"]),
        anchor=anchor,
    )


def _distinct_weeks(series, current):
    return _collapse_overlapping(
        series, start_of=lambda e: e["start"], end_of=lambda e: e["end"], anchor=current
    )


def _pct_at_hour(entry, hour):
    """The window's reported % as of `hour` elapsed hours in, or None if the
    series doesn't reach back that far."""
    target = entry["start"] + timedelta(hours=hour)
    seen = [p for t, p in entry["points"] if t <= target]
    return max(seen) if seen else None


def _cumulative_shape(reference):
    """Median cumulative share of a week's final usage by each elapsed hour,
    across the reference weeks. Monotone by construction."""
    shape = []
    previous = 0.0
    for hour in range(EXHAUSTION_SHAPE_HOURS + 1):
        shares = []
        for entry in reference:
            if entry["coverage_hours"] < hour:
                continue
            pct = _pct_at_hour(entry, hour)
            if pct is not None:
                shares.append(min(1.0, pct / entry["final"]))
        value = statistics.median(shares) if shares else previous
        previous = max(previous, value)
        shape.append(previous)
    return shape


def _shape_crossing_hour(shape, projected_final, from_hour):
    """First elapsed hour at or after `from_hour` where the projected curve
    (projected_final scaled by the shape) reaches 100%, linearly interpolated
    between the hourly samples. None if it never does within the window."""
    if projected_final <= 100:
        return None
    start = max(0, int(from_hour))
    for hour in range(start, EXHAUSTION_SHAPE_HOURS):
        low, high = projected_final * shape[hour], projected_final * shape[hour + 1]
        if high >= 100:
            if high == low:
                return float(hour + 1)
            return hour + (100 - low) / (high - low)
    return None


def predict_weekly_exhaustion(source):
    """Project when the weekly quota would hit 100% at the current pace.

    Returns a dict that is always shaped the same; `available` is False (with
    a `reason`) whenever there isn't enough data to say anything honest."""
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")

    def unavailable(reason, **extra):
        return {
            "available": False, "reason": reason, "method": None,
            "current_percentage": None, "elapsed_hours": None,
            "projected_final_percentage": None,
            "projected_final_low": None, "projected_final_high": None,
            "will_exhaust": None, "exhaustion_at": None,
            "exhaustion_at_earliest": None, "exhaustion_at_latest": None,
            "seconds_until_exhaustion": None, "burn_rate_pct_per_day": None,
            "reference_weeks": 0, "typical_error_pp": None,
            "window_end": None, **extra,
        }

    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        series = _weekly_snapshot_series(conn, source)

    current = next((e for e in reversed(series) if e["start"] <= now < e["end"]), None)
    if current is None:
        return unavailable("no weekly window in progress")

    series = _distinct_weeks(series, current)

    elapsed_hours = (now - current["start"]).total_seconds() / 3600.0
    current_pct = current["points"][-1][1]
    window_end = current["end"].isoformat()
    burn_rate = current_pct / elapsed_hours * 24 if elapsed_hours > 0 else None

    if elapsed_hours < EXHAUSTION_MIN_ELAPSED_HOURS:
        return unavailable(
            f"only {elapsed_hours:.0f}h into the week — too early to project",
            current_percentage=current_pct, elapsed_hours=elapsed_hours,
            burn_rate_pct_per_day=burn_rate, window_end=window_end,
        )

    reference = [
        e for e in series
        if e is not current
        and e["final"] >= EXHAUSTION_MIN_WEEK_FINAL_PCT
        and e["coverage_hours"] >= EXHAUSTION_MIN_WEEK_COVERAGE_HOURS
    ]

    hour_index = min(EXHAUSTION_SHAPE_HOURS, int(elapsed_hours))
    method = "shape"
    shape = _cumulative_shape(reference) if len(reference) >= EXHAUSTION_MIN_REFERENCE_WEEKS else None
    if shape is None or shape[hour_index] < EXHAUSTION_MIN_SHAPE_FRACTION:
        # Not enough comparable weeks (or the typical week has barely started
        # by now) — fall back to flat wall-clock scaling, which is worse but
        # at least says something. The UI leans on `method` to hedge it.
        method = "flat"
        shape = [hour / EXHAUSTION_SHAPE_HOURS for hour in range(EXHAUSTION_SHAPE_HOURS + 1)]

    projected_final = current_pct / shape[hour_index] if shape[hour_index] > 0 else None
    if projected_final is None:
        return unavailable("no usable pace signal yet", current_percentage=current_pct,
                           elapsed_hours=elapsed_hours, burn_rate_pct_per_day=burn_rate,
                           window_end=window_end)

    # Calibrate the uncertainty band against how this same projection would
    # have done at this same point in each reference week — the spread of
    # actual/predicted ratios IS the error bar, measured on the user's own
    # history instead of assumed.
    ratios, errors = [], []
    for entry in reference:
        if entry["coverage_hours"] < hour_index:
            continue
        pct_then = _pct_at_hour(entry, hour_index)
        if pct_then is None or shape[hour_index] <= 0:
            continue
        predicted = pct_then / shape[hour_index]
        if predicted <= 0:
            continue
        ratios.append(entry["final"] / predicted)
        errors.append(abs(min(100.0, predicted) - entry["final"]))

    if ratios:
        ordered = sorted(ratios)
        low_ratio = ordered[int(len(ordered) * 0.2)]
        high_ratio = ordered[min(len(ordered) - 1, int(len(ordered) * 0.8))]
    else:
        low_ratio = high_ratio = None

    projected_low = projected_final * low_ratio if low_ratio is not None else None
    projected_high = projected_final * high_ratio if high_ratio is not None else None

    def crossing_iso(final_value):
        if final_value is None:
            return None
        hour = _shape_crossing_hour(shape, final_value, elapsed_hours)
        if hour is None:
            return None
        # The shape is sampled hourly, so a crossing can interpolate to a
        # moment just behind `now`; report it as "now" rather than as a time
        # in the past.
        return max(now, current["start"] + timedelta(hours=hour)).isoformat()

    exhaustion_at = crossing_iso(projected_final)
    # A higher projected total burns the quota sooner, so the optimistic
    # (latest) ETA comes from the low end of the band and vice versa.
    earliest = crossing_iso(projected_high)
    latest = crossing_iso(projected_low)

    seconds_until = None
    if exhaustion_at is not None:
        eta = _parse_ts(exhaustion_at)
        if eta is not None:
            seconds_until = max(0.0, (eta - now).total_seconds())

    return {
        "available": True,
        "reason": None,
        "method": method,
        "current_percentage": current_pct,
        "elapsed_hours": elapsed_hours,
        "projected_final_percentage": min(100.0, projected_final),
        "projected_final_low": min(100.0, projected_low) if projected_low is not None else None,
        "projected_final_high": min(100.0, projected_high) if projected_high is not None else None,
        "will_exhaust": exhaustion_at is not None,
        "exhaustion_at": exhaustion_at,
        "exhaustion_at_earliest": earliest,
        "exhaustion_at_latest": latest,
        "seconds_until_exhaustion": seconds_until,
        "burn_rate_pct_per_day": burn_rate,
        "reference_weeks": len(reference),
        "typical_error_pp": statistics.mean(errors) if errors else None,
        "window_end": window_end,
    }


def delete_weekly_usage_window(source, window_start):
    """Remove a weekly window and its nested daily high-water-marks. Returns
    True if a window was actually deleted, False if it didn't exist."""
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")

    with _db_lock:
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM weekly_daily_max_pct WHERE source = ? AND window_start = ?",
                (source, window_start),
            )
            cur = conn.execute(
                "DELETE FROM weekly_windows WHERE source = ? AND window_start = ?",
                (source, window_start),
            )
            conn.commit()
            return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Aggregation for the API response
# ---------------------------------------------------------------------------


# A rename made in an app outranks one recorded in the transcript (the app
# can rename a session whose CLI isn't running, which never reaches the log);
# generated titles come after any rename; the first prompt is returned
# separately so the UI can present it as a quote rather than a title.
SESSION_TITLE_COLUMNS = """
                (SELECT COALESCE(CASE WHEN st.app_title_source = 'user' THEN st.app_title END,
                                 st.custom_title, st.app_title, st.ai_title)
                 FROM session_titles st WHERE st.session_id = messages.session_id) AS title,
                (SELECT st.first_prompt FROM session_titles st WHERE st.session_id = messages.session_id) AS first_prompt"""


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


USAGE_PERIODS = [("1d", 1), ("3d", 3), ("7d", 7), ("2w", 14), ("1m", 30), ("3m", 90), ("6m", 180), ("1y", 365)]


def build_period_totals(conn, source, now=None):
    """Rolling-window totals keyed by period ("1d", "7d", ...). Cutoffs are
    formatted like the stored timestamps (UTC, millisecond "Z" suffix) so a
    plain string comparison is a correct time comparison."""
    now = now or datetime.now(timezone.utc)
    where_sql, where_params = _where(source, "timestamp >= ?")
    periods = {}
    for key, days in USAGE_PERIODS:
        since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        row = conn.execute(
            f"""
            SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COUNT(*) AS message_count,
                COUNT(DISTINCT session_id) AS session_count
            FROM messages {where_sql}
            """,
            where_params + (since,),
        ).fetchone()
        periods[key] = {**{k: row[k] for k in row.keys()}, "days": days, "since": since}
    return periods


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

        session_cols = f"""
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
                {SESSION_TITLE_COLUMNS}
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

        periods = build_period_totals(conn, source)

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
        "periods": periods,
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
                {SESSION_TITLE_COLUMNS}
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
                {SESSION_TITLE_COLUMNS}
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
        elif parsed.path == "/api/weekly-usage":
            source = qs.get("source", [None])[0]
            if source is not None and source not in SOURCES:
                self._send_error_json(400, f"unknown source; expected one of: {', '.join(SOURCES)}")
                return
            try:
                limit = int(qs.get("limit", ["20"])[0])
            except ValueError:
                limit = 20

            def build_weekly_usage_response():
                result = {"weeks": list_weekly_usage(source, limit)}
                if source:
                    result["prediction"] = predict_weekly_usage(source)
                return result

            self._send_json_result(build_weekly_usage_response)
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
            # The page is read straight off disk on every request, so a
            # browser holding a heuristically-cached copy is the one way the
            # UI can lag the code that's actually running.
            self.send_header("Cache-Control", "no-store")
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
    for source in SOURCES:
        backfill_weekly_usage_from_snapshots(source)
    with _db_lock:
        with get_conn() as conn:
            ensure_pricing_current(conn)  # full recompute if pricing files changed since last run
            conn.commit()
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
