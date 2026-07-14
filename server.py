#!/usr/bin/env python3
"""Local dashboard for Claude Code token usage & estimated API cost.

Reads Claude Code's local session transcripts (~/.claude/projects/**/*.jsonl),
incrementally indexes token usage into a local SQLite cache, and serves a
small self-refreshing HTML dashboard plus a JSON API.

Runs entirely on localhost. No data leaves the machine.
"""

import glob
import hashlib
import http.server
import json
import os
import sqlite3
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

HOME = os.path.expanduser("~")
PROJECTS_GLOB = os.path.join(HOME, ".claude", "projects", "**", "*.jsonl")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "usage.sqlite3")
HTML_PATH = os.path.join(APP_DIR, "index.html")
SESSIONS_HTML_PATH = os.path.join(APP_DIR, "sessions.html")
PRICING_PATH = os.path.join(APP_DIR, "pricing.json")
PORT = int(os.environ.get("CLAUDE_USAGE_DASHBOARD_PORT", "4870"))

# ---------------------------------------------------------------------------
# Remote usage polling — Anthropic's own account-level usage/reset data.
# This is authoritative (it's literally what Claude Code's own status line
# reads), and replaces our local reconstruction of window boundaries whenever
# available. Auth reuses Claude Code's own OAuth token from the macOS
# Keychain (same one the CLI keeps refreshed for its own use) — no cookie to
# copy or expire, as long as you keep using Claude Code periodically.
# ---------------------------------------------------------------------------
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
REMOTE_USAGE_POLL_SECONDS = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Pricing ($ per 1,000,000 tokens), loaded from pricing.json — NOT hardcoded.
#
# Each model maps to a chronological list of rate periods:
#   {"from": null | "YYYY-MM-DD", "input": ..., "output": ...}
# "from": null means "earliest" (applies until the next dated entry takes
# over). A message is priced using the rate period whose "from" date is the
# latest one at-or-before that message's own timestamp — this is what lets
# a price change be recorded without rewriting history: old messages keep
# resolving to the old rate period, new messages resolve to the new one,
# purely based on their timestamp, not on when the lookup runs.
#
# To update pricing: edit pricing.json (append a new dated entry rather than
# editing an existing one — see notes below). The running server detects the
# file's content hash changed and re-derives cost for every already-indexed
# message from its stored (model, timestamp, tokens) — no log re-scan needed,
# and unrelated schema migrations never touch this hash, so they can't
# accidentally change historical dollar amounts.
# ---------------------------------------------------------------------------

_pricing_cache = {"mtime": None, "data": None}


def load_pricing():
    mtime = os.path.getmtime(PRICING_PATH)
    if _pricing_cache["mtime"] != mtime:
        with open(PRICING_PATH) as f:
            _pricing_cache["data"] = json.load(f)
        _pricing_cache["mtime"] = mtime
    return _pricing_cache["data"]


def pricing_file_hash():
    with open(PRICING_PATH, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


IST = timezone(timedelta(hours=5, minutes=30))

# Weekly limit: Anthropic resets this on a fixed 7-day clock (confirmed
# boundary: Thursday 15:30 IST), independent of usage. Period is exactly 7
# days, so ANY Thursday 15:30 IST lands on the same grid as any other — the
# calendar date of the anchor below is arbitrary, only the weekday/time matter.
WEEKLY_ANCHOR = datetime(2024, 1, 4, 15, 30, tzinfo=IST)  # a Thursday
WEEKLY_PERIOD = timedelta(days=7)

# 5-hour limit: NOT a fixed clock grid. Confirmed empirically (2026-07-10) —
# a >5h idle gap mid-day reset the window at the next message, rather than at
# the next slot of a fixed grid extrapolated from an earlier boundary. So the
# window is usage-anchored: it starts at the first message after the prior
# window's 5 hours have fully elapsed, and must be derived by walking actual
# message timestamps (see compute_five_hour_window), not by ticking a clock.
FIVE_HOUR_PERIOD = timedelta(hours=5)
FIVE_HOUR_LOOKBACK_DAYS = 14  # long enough that a 5h+ idle gap is essentially guaranteed


def _window_start(anchor, period, now):
    elapsed = now - anchor
    periods = elapsed // period
    return anchor + periods * period


def _floor_to_10min(dt):
    # Confirmed empirically (2026-07-10): Anthropic buckets the session start
    # down to the nearest 10-minute mark rather than using the exact request
    # timestamp — a session whose first message landed at 12:25:33 reset at
    # 12:20 (window end 17:20), not 12:25:33 (which would give 17:25:33).
    # IST's UTC offset (5h30m = 330min) is itself a multiple of 10, so
    # flooring gives the same result in UTC or IST — no tz conversion needed.
    return dt.replace(minute=(dt.minute // 10) * 10, second=0, microsecond=0)


def compute_five_hour_window(conn, now):
    """Fallback only, used when Anthropic's own usage API (see
    fetch_remote_usage below) isn't available. Derives the current 5-hour
    session window from actual message timestamps: a new window starts
    (10-minute-bucketed) at the first message sent after the previous
    window's 5 hours have fully elapsed. This is NOT a fixed clock grid —
    idle time shifts it — and it's an approximation; the remote API is
    authoritative when reachable."""
    cutoff = (now - timedelta(days=FIVE_HOUR_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT timestamp FROM messages WHERE day >= ? ORDER BY timestamp ASC",
        (cutoff,),
    ).fetchall()

    window_start = window_end = None
    for (ts,) in rows:
        dt = _parse_ts(ts)
        if dt is None:
            continue
        if window_end is None or dt >= window_end:
            window_start = _floor_to_10min(dt)
            window_end = window_start + FIVE_HOUR_PERIOD

    if window_start is None:
        # No messages at all within the lookback range.
        window_start = _floor_to_10min(now)
        window_end = window_start + FIVE_HOUR_PERIOD

    return window_start, window_end


# ---------------------------------------------------------------------------
# Remote usage polling
# ---------------------------------------------------------------------------


def _load_oauth_token():
    """Read Claude Code's own OAuth access token from the macOS Keychain —
    the same credential the CLI keeps refreshed for its own API calls, so
    there's nothing here for us to renew manually."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"could not read macOS Keychain: {e}") from e

    if result.returncode != 0:
        raise RuntimeError(
            f"no '{KEYCHAIN_SERVICE}' Keychain entry found — is Claude Code installed and logged in?"
        )
    try:
        token = json.loads(result.stdout)["claudeAiOauth"]["accessToken"]
    except (json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"unexpected Keychain entry format: {e}") from e

    if not token:
        raise RuntimeError("Keychain entry has no access token")
    return token


def fetch_remote_usage():
    """Fetch Anthropic's own account-level usage/reset data. Raises with a
    diagnosable message on any failure (missing token, expired session)
    rather than returning partial/garbage data."""
    token = _load_oauth_token()

    req = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "")
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from Claude usage API — token may be expired; use Claude Code to refresh it") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error reaching Claude usage API: {e.reason}") from e

    if "application/json" not in content_type:
        raise RuntimeError("Claude usage API did not return JSON")

    return json.loads(body)


def store_remote_usage_poll(conn, data):
    five_hour = data.get("five_hour") or {}
    seven_day = data.get("seven_day") or {}
    polled_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO remote_usage_polls
        (polled_at, five_hour_pct, five_hour_resets_at, weekly_pct, weekly_resets_at, raw_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            polled_at,
            five_hour.get("utilization"),
            five_hour.get("resets_at"),
            seven_day.get("utilization"),
            seven_day.get("resets_at"),
            json.dumps(data),
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('remote_usage_last_error', '')"
    )
    conn.commit()


def record_remote_usage_error(message):
    with _db_lock:
        with get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('remote_usage_last_error', ?)",
                (message,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('remote_usage_last_attempt_at', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.commit()


def poll_remote_usage_once():
    """Fetch once and persist the result (or the error), then auto-log a
    (percentage, dollar) snapshot for each window type using THIS poll's own
    utilization — the dollar side is computed from the window boundaries this
    same poll just reported (see build_limit_windows: it prefers the latest
    remote poll, which by now includes the one just stored), not a stale or
    heuristic window. Safe to call from a background timer or an on-demand
    API trigger."""
    try:
        data = fetch_remote_usage()
    except Exception as e:
        record_remote_usage_error(str(e))
        return {"ok": False, "error": str(e)}

    with _db_lock:
        with get_conn() as conn:
            store_remote_usage_poll(conn, data)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('remote_usage_last_attempt_at', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.commit()

    five_hour_pct = (data.get("five_hour") or {}).get("utilization")
    weekly_pct = (data.get("seven_day") or {}).get("utilization")
    if five_hour_pct is not None:
        create_limit_snapshot("five_hour", five_hour_pct)
    if weekly_pct is not None:
        create_limit_snapshot("weekly", weekly_pct)

    return {"ok": True}


def _remote_usage_poll_loop():
    while True:
        poll_remote_usage_once()
        time.sleep(REMOTE_USAGE_POLL_SECONDS)


def get_latest_remote_usage(conn):
    row = conn.execute(
        """
        SELECT polled_at, five_hour_pct, five_hour_resets_at, weekly_pct, weekly_resets_at
        FROM remote_usage_polls ORDER BY polled_at DESC LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return {
        "polled_at": row["polled_at"],
        "five_hour_pct": row["five_hour_pct"],
        "five_hour_resets_at": row["five_hour_resets_at"],
        "weekly_pct": row["weekly_pct"],
        "weekly_resets_at": row["weekly_resets_at"],
    }


def get_remote_usage_status(conn):
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('remote_usage_last_error', 'remote_usage_last_attempt_at')"
    ).fetchall()
    status = {r["key"]: r["value"] for r in rows}
    return {
        "last_attempt_at": status.get("remote_usage_last_attempt_at"),
        "last_error": status.get("remote_usage_last_error") or None,
    }


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


def sum_window(conn, window_start, window_end, day_lookback_days, bucket_period, num_buckets):
    """Sum tokens/cost (with per-token-type cost, and a per-model breakdown)
    for messages within [window_start, window_end), plus a fixed-size series
    of `num_buckets` sub-windows of `bucket_period` each, starting at
    window_start, for sparkline-style charts.

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
        WHERE day >= ?
        """,
        (day_cutoff,),
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


def _previous_window_summary(conn, prev_start, prev_end, day_lookback_days, bucket_period):
    usage = sum_window(
        conn, prev_start, prev_end, day_lookback_days=day_lookback_days,
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


def build_limit_windows(conn):
    now = datetime.now(timezone.utc)
    remote = get_latest_remote_usage(conn)

    five_hour_source = "heuristic"
    if remote and remote["five_hour_resets_at"]:
        five_hour_end = _parse_ts(remote["five_hour_resets_at"])
        five_hour_start = five_hour_end - FIVE_HOUR_PERIOD
        five_hour_source = "remote"
    else:
        five_hour_start, five_hour_end = compute_five_hour_window(conn, now)

    weekly_source = "heuristic"
    if remote and remote["weekly_resets_at"]:
        weekly_end = _parse_ts(remote["weekly_resets_at"])
        weekly_start = weekly_end - WEEKLY_PERIOD
        weekly_source = "remote"
    else:
        weekly_start = _window_start(WEEKLY_ANCHOR, WEEKLY_PERIOD, now)
        weekly_end = weekly_start + WEEKLY_PERIOD

    five_hour_usage = sum_window(
        conn, five_hour_start, five_hour_end, day_lookback_days=2,
        bucket_period=timedelta(hours=1), num_buckets=5,
    )
    weekly_usage = sum_window(
        conn, weekly_start, weekly_end, day_lookback_days=8,
        bucket_period=timedelta(days=1), num_buckets=7,
    )

    five_hour_previous = _previous_window_summary(
        conn, five_hour_start - FIVE_HOUR_PERIOD, five_hour_start,
        day_lookback_days=2, bucket_period=FIVE_HOUR_PERIOD,
    )
    weekly_previous = _previous_window_summary(
        conn, weekly_start - WEEKLY_PERIOD, weekly_start,
        day_lookback_days=9, bucket_period=WEEKLY_PERIOD,
    )

    return {
        "five_hour": {
            "window_start": five_hour_start.isoformat(),
            "window_end": five_hour_end.isoformat(),
            "seconds_remaining": max(0.0, (five_hour_end - now).total_seconds()),
            "source": five_hour_source,
            "remote_pct": remote["five_hour_pct"] if remote else None,
            "previous": five_hour_previous,
            "credit_estimate": estimate_credit_limit(conn, "five_hour"),
            **five_hour_usage,
        },
        "weekly": {
            "window_start": weekly_start.isoformat(),
            "window_end": weekly_end.isoformat(),
            "seconds_remaining": max(0.0, (weekly_end - now).total_seconds()),
            "source": weekly_source,
            "remote_pct": remote["weekly_pct"] if remote else None,
            "previous": weekly_previous,
            "credit_estimate": estimate_credit_limit(conn, "weekly"),
            **weekly_usage,
        },
        "remote_status": get_remote_usage_status(conn),
        "remote_polled_at": remote["polled_at"] if remote else None,
    }


def create_limit_snapshot(window_type, percentage):
    """Pair Anthropic's reported plan-usage percentage for a poll with the
    cost we've locally computed for that same window at that moment, and
    store both. Neither value is derived from the other — the point is to
    accumulate (percentage, dollar) pairs over time to see how they relate.

    A 0% reading paired with nonzero local cost is a contradiction (0% used
    should mean ~$0 spent in that window) rather than a real data point —
    seen in practice when Anthropic's utilization briefly reports 0 while
    the window's resets_at hasn't actually rolled over yet. Such rows are
    dropped rather than stored, since they'd corrupt the %-to-$ estimate."""
    if window_type not in ("five_hour", "weekly"):
        raise ValueError("window_type must be 'five_hour' or 'weekly'")
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
            window = build_limit_windows(conn)[window_type]
            dollar_value = window["cost"]

            if percentage == 0 and dollar_value > 0:
                return {"saved": False, "reason": "0% with nonzero local cost — discarded as invalid"}

            created_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO limit_snapshots
                (window_type, percentage, dollar_value, window_start, window_end, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (window_type, percentage, dollar_value, window["window_start"], window["window_end"], created_at),
            )
            conn.commit()

    return {
        "saved": True,
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


def estimate_credit_limit(conn, window_type):
    rows = conn.execute(
        """
        SELECT percentage, dollar_value, created_at FROM limit_snapshots
        WHERE window_type = ? ORDER BY created_at ASC
        """,
        (window_type,),
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


def list_limit_snapshots(limit=200):
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, window_type, percentage, dollar_value, window_start, window_end, created_at
            FROM limit_snapshots
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]


def day_fields(ts):
    """Returns (day, weekday, hour) in the machine's local timezone, derived
    from a single conversion so all three always agree with each other."""
    dt = _parse_ts(ts)
    if dt is None:
        return "unknown", None, None
    local_dt = dt.astimezone()
    return local_dt.strftime("%Y-%m-%d"), local_dt.weekday(), local_dt.hour


def _pick_rate(schedule, ts_str):
    dt = _parse_ts(ts_str)
    ordered = sorted(schedule, key=lambda e: e["from"] or "0000-01-01")
    applicable = ordered[0]
    for entry in ordered:
        entry_from = entry.get("from")
        if entry_from is None:
            applicable = entry
            continue
        entry_from_dt = _parse_ts(entry_from + "T00:00:00+00:00")
        if dt is not None and entry_from_dt is not None and dt >= entry_from_dt:
            applicable = entry
    return (applicable["input"], applicable["output"])


def price_for_model(model, ts_str):
    """Return (input_price_per_mtok, output_price_per_mtok) for a model at a given time."""
    pricing = load_pricing()
    if not model:
        model = "unknown"

    schedule = pricing["models"].get(model)
    if schedule:
        return _pick_rate(schedule, ts_str)

    for prefix, rate in pricing["prefix_fallback"]:
        if model.startswith(prefix):
            return (rate["input"], rate["output"])

    d = pricing["unknown_default"]
    return (d["input"], d["output"])


def compute_costs(model, ts_str, input_tokens, output_tokens, cache_read, cache_5m, cache_1h):
    """Returns (cost, cost_no_cache, cost_input, cost_output, cost_cache,
    cost_cache_read, cost_cache_write) for one message."""
    in_price, out_price = price_for_model(model, ts_str)
    in_ppt = in_price / 1_000_000.0
    out_ppt = out_price / 1_000_000.0
    cost_input = input_tokens * in_ppt
    cost_output = output_tokens * out_ppt
    cost_cache_read = cache_read * in_ppt * 0.1
    cost_cache_write = cache_5m * in_ppt * 1.25 + cache_1h * in_ppt * 2.0
    cost_cache = cost_cache_read + cost_cache_write
    cost = cost_input + cost_output + cost_cache
    # If none of this had been cached, every cached/cache-writing token would
    # instead have been billed as plain input.
    cost_no_cache = (input_tokens + cache_read + cache_5m + cache_1h) * in_ppt + output_tokens * out_ppt
    return cost, cost_no_cache, cost_input, cost_output, cost_cache, cost_cache_read, cost_cache_write


# ---------------------------------------------------------------------------
# SQLite cache + incremental log indexing
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()

# Bump this whenever the `messages` table schema changes. On a version bump,
# the cache is dropped and rebuilt from the (untouched) source log files.
SCHEMA_VERSION = 5


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_columns(conn, table, columns):
    """Non-destructively add any missing columns (name -> SQL type) to `table`.
    Used for derived columns that can be backfilled from data already stored
    in the table, so adding one never requires a full log re-scan."""
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
        _ensure_columns(conn, "messages", {"cost_cache_read": "REAL", "cost_cache_write": "REAL"})
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_limit_snapshots_type ON limit_snapshots(window_type, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_remote_usage_polls_polled_at ON remote_usage_polls(polled_at)"
        )
        conn.commit()


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


def scan_file(conn, path, state):
    try:
        stat = os.stat(path)
    except OSError:
        return

    mtime, size = stat.st_mtime, stat.st_size
    prev = state.get(path)

    if prev and prev["mtime"] == mtime and prev["size"] == size:
        return  # unchanged

    offset = prev["offset"] if prev else 0
    if prev and size < prev["size"]:
        offset = 0  # file was truncated/rewritten; re-read from scratch

    new_rows = []
    ai_title_rows = []
    custom_title_rows = []
    new_offset = offset
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            while True:
                line_start = f.tell()
                line = f.readline()
                if not line:
                    break
                if not line.endswith("\n"):
                    # Partial line (file still being written) — stop, retry next scan.
                    break
                new_offset = f.tell()
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if obj.get("type") == "ai-title":
                    ai_title = obj.get("aiTitle")
                    ai_session_id = obj.get("sessionId")
                    if ai_title and ai_session_id:
                        ai_title_rows.append((ai_session_id, ai_title))
                    continue

                if obj.get("type") == "custom-title":
                    # The title Claude Code's own UI actually shows for a
                    # session (its resume/session picker) — preferred over
                    # ai-title, which is a separate auto-summary mechanism.
                    custom_title = obj.get("customTitle")
                    custom_session_id = obj.get("sessionId")
                    if custom_title and custom_session_id:
                        custom_title_rows.append((custom_session_id, custom_title))
                    continue

                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage")
                msg_id = msg.get("id")
                if not usage or not msg_id:
                    continue

                model = msg.get("model") or "unknown"
                ts = obj.get("timestamp")
                cwd = obj.get("cwd")
                session_id = obj.get("sessionId")

                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                cache_read = int(usage.get("cache_read_input_tokens") or 0)

                cache_creation = usage.get("cache_creation") or {}
                if cache_creation:
                    cache_5m = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
                    cache_1h = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
                else:
                    # Older/simple format: assume default 5-minute TTL.
                    cache_5m = int(usage.get("cache_creation_input_tokens") or 0)
                    cache_1h = 0

                cost, cost_no_cache, cost_input, cost_output, cost_cache, cost_cache_read, cost_cache_write = compute_costs(
                    model, ts, input_tokens, output_tokens, cache_read, cache_5m, cache_1h
                )

                # `day`/`weekday`/`hour` all derive from the SAME local-time
                # conversion so a message logged just after local midnight
                # (but before UTC midnight, e.g. IST is UTC+5:30) lands on
                # the calendar day the user actually experienced it on —
                # not on the UTC day the raw timestamp prefix implies.
                day, weekday, hour = day_fields(ts)

                new_rows.append(
                    (
                        msg_id,
                        model,
                        ts,
                        day,
                        weekday,
                        hour,
                        project_name_from_cwd(cwd),
                        cwd,
                        session_id,
                        input_tokens,
                        output_tokens,
                        cache_read,
                        cache_5m,
                        cache_1h,
                        cost,
                        cost_no_cache,
                        cost_input,
                        cost_output,
                        cost_cache,
                        cost_cache_read,
                        cost_cache_write,
                    )
                )
    except OSError:
        return

    if new_rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO messages
            (message_id, model, timestamp, day, weekday, hour, project, cwd, session_id,
             input_tokens, output_tokens, cache_read_tokens, cache_5m_tokens,
             cache_1h_tokens, cost, cost_no_cache, cost_input, cost_output, cost_cache,
             cost_cache_read, cost_cache_write)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            new_rows,
        )

    if ai_title_rows:
        # Titles can be re-generated multiple times per session; rows are
        # appended in file order, so REPLACE naturally keeps the latest one.
        conn.executemany(
            """
            INSERT INTO session_titles (session_id, ai_title) VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET ai_title = excluded.ai_title
            """,
            ai_title_rows,
        )

    if custom_title_rows:
        conn.executemany(
            """
            INSERT INTO session_titles (session_id, custom_title) VALUES (?, ?)
            ON CONFLICT(session_id) DO UPDATE SET custom_title = excluded.custom_title
            """,
            custom_title_rows,
        )

    conn.execute(
        "INSERT OR REPLACE INTO file_state (path, mtime, size, offset) VALUES (?, ?, ?, ?)",
        (path, mtime, size, new_offset),
    )


def recompute_all_costs(conn):
    """Re-derive cost* columns for every row from its stored (model, timestamp,
    tokens) using the current pricing.json — no log file access needed. Safe
    to run any time pricing.json changes: each message re-resolves its own
    rate period from its own timestamp, so historical amounts only change if
    the rate period that actually applied to them changed.
    """
    rows = conn.execute(
        """
        SELECT message_id, model, timestamp, input_tokens, output_tokens,
               cache_read_tokens, cache_5m_tokens, cache_1h_tokens
        FROM messages
        """
    ).fetchall()
    updates = []
    for message_id, model, ts, input_tokens, output_tokens, cache_read, cache_5m, cache_1h in rows:
        cost, cost_no_cache, cost_input, cost_output, cost_cache, cost_cache_read, cost_cache_write = compute_costs(
            model, ts, input_tokens, output_tokens, cache_read, cache_5m, cache_1h
        )
        updates.append(
            (cost, cost_no_cache, cost_input, cost_output, cost_cache, cost_cache_read, cost_cache_write, message_id)
        )
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
    current_hash = pricing_file_hash()
    row = conn.execute("SELECT value FROM meta WHERE key = 'pricing_hash'").fetchone()
    stored_hash = row[0] if row else None
    needs_recompute = stored_hash != current_hash
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
        (current_hash,),
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


def scan_all():
    with _db_lock:
        with get_conn() as conn:
            cur = conn.execute("SELECT path, mtime, size, offset FROM file_state")
            state = {
                row[0]: {"mtime": row[1], "size": row[2], "offset": row[3]}
                for row in cur.fetchall()
            }
            for path in glob.glob(PROJECTS_GLOB, recursive=True):
                scan_file(conn, path, state)
            ensure_day_fields_current(conn)
            ensure_project_field_current(conn)
            ensure_pricing_current(conn)
            conn.commit()


# ---------------------------------------------------------------------------
# Aggregation for the API response
# ---------------------------------------------------------------------------


def build_usage_payload():
    scan_all()
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row

        totals = conn.execute(
            """
            SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(cost_no_cache), 0) AS cost_no_cache,
                COUNT(*) AS message_count,
                COUNT(DISTINCT session_id) AS session_count
            FROM messages
            """
        ).fetchone()

        by_model = conn.execute(
            """
            SELECT model,
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
            FROM messages
            GROUP BY model
            ORDER BY cost DESC
            """
        ).fetchall()

        by_day = conn.execute(
            """
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
            FROM messages
            WHERE day != 'unknown'
            GROUP BY day
            ORDER BY day DESC
            LIMIT 30
            """
        ).fetchall()

        heatmap_cutoff = (datetime.now() - timedelta(days=375)).strftime("%Y-%m-%d")
        heatmap_days = conn.execute(
            """
            SELECT day,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COUNT(*) AS message_count
            FROM messages
            WHERE day != 'unknown' AND day >= ?
            GROUP BY day
            ORDER BY day ASC
            """,
            (heatmap_cutoff,),
        ).fetchall()

        by_project = conn.execute(
            """
            SELECT project,
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
            FROM messages
            GROUP BY project
            ORDER BY cost DESC
            LIMIT 15
            """
        ).fetchall()

        top_sessions = conn.execute(
            """
            SELECT session_id,
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
            FROM messages
            WHERE session_id IS NOT NULL
            GROUP BY session_id
            ORDER BY cost DESC
            LIMIT 10
            """
        ).fetchall()

        last_sessions = conn.execute(
            """
            SELECT session_id,
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
            FROM messages
            WHERE session_id IS NOT NULL
            GROUP BY session_id
            ORDER BY end_ts DESC
            LIMIT 10
            """
        ).fetchall()

        top_day = conn.execute(
            """
            SELECT day, COALESCE(SUM(cost), 0) AS cost
            FROM messages
            WHERE day != 'unknown'
            GROUP BY day
            ORDER BY cost DESC
            LIMIT 1
            """
        ).fetchone()

        hour_weekday = conn.execute(
            """
            SELECT weekday, hour,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                COUNT(*) AS message_count
            FROM messages
            WHERE weekday IS NOT NULL AND hour IS NOT NULL
            GROUP BY weekday, hour
            """
        ).fetchall()

        session_spans = conn.execute(
            """
            SELECT session_id,
                (SELECT project FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS project,
                MIN(timestamp) AS start_ts,
                MAX(timestamp) AS end_ts, SUM(cost) AS cost, COUNT(*) AS message_count
            FROM messages
            WHERE session_id IS NOT NULL
            GROUP BY session_id
            """
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
        today_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cost), 0) AS cost
            FROM messages WHERE day = ?
            """,
            (today,),
        ).fetchone()

        limits = build_limit_windows(conn)

    def row_to_dict(r):
        return {k: r[k] for k in r.keys()}

    pricing = load_pricing()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pricing_last_verified": pricing.get("last_verified"),
        "pricing_source": pricing.get("source"),
        "totals": row_to_dict(totals),
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


def find_sessions(query, limit=20):
    """Look up sessions by exact ID or ID prefix. Returns a list (usually 0 or 1 items)."""
    query = (query or "").strip()
    if not query:
        return []

    scan_all()
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT session_id,
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
            WHERE session_id LIKE ? ESCAPE '\\'
            GROUP BY session_id
            ORDER BY cost DESC
            LIMIT ?
            """,
            (query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", limit),
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


def list_all_sessions():
    """Every distinct session, unpaginated — the dataset is small enough
    (low hundreds) for the browser to sort/filter/search client-side."""
    scan_all()
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT session_id,
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
            WHERE session_id IS NOT NULL
            GROUP BY session_id
            ORDER BY end_ts DESC
            """
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep stdout/log quiet

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._serve_file(HTML_PATH, "text/html; charset=utf-8")
        elif parsed.path in ("/sessions", "/sessions.html"):
            self._serve_file(SESSIONS_HTML_PATH, "text/html; charset=utf-8")
        elif parsed.path == "/api/usage":
            self._send_json_result(build_usage_payload)
        elif parsed.path == "/api/session":
            query = parse_qs(parsed.query).get("id", [""])[0]
            self._send_json_result(lambda: {"query": query, "results": find_sessions(query)})
        elif parsed.path == "/api/all-sessions":
            self._send_json_result(lambda: {"sessions": list_all_sessions()})
        elif parsed.path == "/api/limit-snapshots":
            self._send_json_result(lambda: {"snapshots": list_limit_snapshots()})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/remote-usage/poll":
            self._send_json_result(poll_remote_usage_once)
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
    threading.Thread(target=_remote_usage_poll_loop, daemon=True).start()  # polls immediately, then every 10min
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Claude usage dashboard running at http://127.0.0.1:{PORT}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
