"""Claude Code-specific usage source: log parsing, pricing, and rate-limit
windows. Everything here is specific to how Claude Code's local transcripts
(~/.claude/projects/**/*.jsonl) and Anthropic's own usage API are shaped —
see server.py for the source-agnostic plumbing that calls into this module.
"""

import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import server

SOURCE_NAME = "claude"

HOME = os.path.expanduser("~")
LOG_GLOBS = [os.path.join(HOME, ".claude", "projects", "**", "*.jsonl")]
PRICING_PATH = os.path.join(server.APP_DIR, "pricing_claude.json")

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
# Pricing ($ per 1,000,000 tokens), loaded from pricing_claude.json — NOT
# hardcoded. Each model maps to a chronological list of rate periods; see
# _pick_rate for the resolution rule (latest "from" at-or-before a message's
# own timestamp). Historical costs never shift when pricing_claude.json gets
# a new dated entry appended — only server.ensure_pricing_current's hash
# check triggers a recompute, and each row re-resolves from its own timestamp.
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


def _pick_rate(schedule, ts_str):
    dt = server._parse_ts(ts_str)
    ordered = sorted(schedule, key=lambda e: e["from"] or "0000-01-01")
    applicable = ordered[0]
    for entry in ordered:
        entry_from = entry.get("from")
        if entry_from is None:
            applicable = entry
            continue
        entry_from_dt = server._parse_ts(entry_from + "T00:00:00+00:00")
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


def compute_costs(model, ts_str, token_fields):
    """token_fields: {"input", "output", "cache_read", "cache_5m", "cache_1h"}.
    Returns a dict: cost, cost_no_cache, cost_input, cost_output, cost_cache,
    cost_cache_read, cost_cache_write."""
    input_tokens = token_fields.get("input", 0)
    output_tokens = token_fields.get("output", 0)
    cache_read = token_fields.get("cache_read", 0)
    cache_5m = token_fields.get("cache_5m", 0)
    cache_1h = token_fields.get("cache_1h", 0)

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
    return {
        "cost": cost,
        "cost_no_cache": cost_no_cache,
        "cost_input": cost_input,
        "cost_output": cost_output,
        "cost_cache": cost_cache,
        "cost_cache_read": cost_cache_read,
        "cost_cache_write": cost_cache_write,
    }


def token_fields_from_row(row):
    """Rebuild the token_fields dict compute_costs expects from a stored
    `messages` row (used by server.recompute_all_costs, which works purely
    off already-indexed DB columns — no log re-read)."""
    return {
        "input": row["input_tokens"] or 0,
        "output": row["output_tokens"] or 0,
        "cache_read": row["cache_read_tokens"] or 0,
        "cache_5m": row["cache_5m_tokens"] or 0,
        "cache_1h": row["cache_1h_tokens"] or 0,
    }


# ---------------------------------------------------------------------------
# JSONL scanning
# ---------------------------------------------------------------------------


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

                costs = compute_costs(
                    model, ts,
                    {"input": input_tokens, "output": output_tokens, "cache_read": cache_read,
                     "cache_5m": cache_5m, "cache_1h": cache_1h},
                )

                # `day`/`weekday`/`hour` all derive from the SAME local-time
                # conversion so a message logged just after local midnight
                # (but before UTC midnight, e.g. IST is UTC+5:30) lands on
                # the calendar day the user actually experienced it on —
                # not on the UTC day the raw timestamp prefix implies.
                day, weekday, hour = server.day_fields(ts)

                new_rows.append(
                    (
                        msg_id,
                        SOURCE_NAME,
                        model,
                        ts,
                        day,
                        weekday,
                        hour,
                        server.project_name_from_cwd(cwd),
                        cwd,
                        session_id,
                        input_tokens,
                        output_tokens,
                        cache_read,
                        cache_5m,
                        cache_1h,
                        None,  # reasoning_output_tokens — Claude-N/A
                        None,  # service_tier — Claude-N/A
                        costs["cost"],
                        costs["cost_no_cache"],
                        costs["cost_input"],
                        costs["cost_output"],
                        costs["cost_cache"],
                        costs["cost_cache_read"],
                        costs["cost_cache_write"],
                    )
                )
    except OSError:
        return

    if new_rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO messages
            (message_id, source, model, timestamp, day, weekday, hour, project, cwd, session_id,
             input_tokens, output_tokens, cache_read_tokens, cache_5m_tokens,
             cache_1h_tokens, reasoning_output_tokens, service_tier, cost, cost_no_cache,
             cost_input, cost_output, cost_cache, cost_cache_read, cost_cache_write)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


# ---------------------------------------------------------------------------
# 5-hour / weekly window heuristics (fallback when the remote poll is
# unavailable — see get_heuristic_window)
# ---------------------------------------------------------------------------

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
    """Fallback only, used when Anthropic's own usage API isn't available.
    Derives the current 5-hour session window from actual message
    timestamps: a new window starts (10-minute-bucketed) at the first
    message sent after the previous window's 5 hours have fully elapsed.
    This is NOT a fixed clock grid — idle time shifts it — and it's an
    approximation; the remote API is authoritative when reachable."""
    cutoff = (now - timedelta(days=FIVE_HOUR_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT timestamp FROM messages WHERE source = ? AND day >= ? ORDER BY timestamp ASC",
        (SOURCE_NAME, cutoff),
    ).fetchall()

    window_start = window_end = None
    for (ts,) in rows:
        dt = server._parse_ts(ts)
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


def get_heuristic_window(conn, window_type, now):
    if window_type == "five_hour":
        return compute_five_hour_window(conn, now)
    if window_type == "weekly":
        start = _window_start(WEEKLY_ANCHOR, WEEKLY_PERIOD, now)
        return start, start + WEEKLY_PERIOD
    return None


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
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('claude_remote_usage_last_error', '')"
    )
    conn.commit()


def record_remote_usage_error(message):
    with server._db_lock:
        with server.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('claude_remote_usage_last_error', ?)",
                (message,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('claude_remote_usage_last_attempt_at', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.commit()


def poll_remote_usage_once():
    """Fetch once and persist the result (or the error), then auto-log a
    (percentage, dollar) snapshot for each window type using THIS poll's own
    utilization — the dollar side is computed from the window boundaries this
    same poll just reported (see server.build_limit_windows: it prefers the
    latest remote poll, which by now includes the one just stored), not a
    stale or heuristic window. Safe to call from a background timer or an
    on-demand API trigger."""
    try:
        data = fetch_remote_usage()
    except Exception as e:
        record_remote_usage_error(str(e))
        return {"ok": False, "error": str(e)}

    with server._db_lock:
        with server.get_conn() as conn:
            store_remote_usage_poll(conn, data)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('claude_remote_usage_last_attempt_at', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.commit()

    five_hour_pct = (data.get("five_hour") or {}).get("utilization")
    weekly_pct = (data.get("seven_day") or {}).get("utilization")
    if five_hour_pct is not None:
        server.create_limit_snapshot(SOURCE_NAME, "five_hour", five_hour_pct)
    if weekly_pct is not None:
        snapshot = server.create_limit_snapshot(SOURCE_NAME, "weekly", weekly_pct)
        if snapshot.get("saved"):
            server.record_weekly_daily_max(
                SOURCE_NAME, weekly_pct, snapshot["window_start"], snapshot["window_end"], snapshot["created_at"]
            )

    return {"ok": True}


def _remote_usage_poll_loop():
    while True:
        poll_remote_usage_once()
        time.sleep(REMOTE_USAGE_POLL_SECONDS)


def start_background_poll():
    threading.Thread(target=_remote_usage_poll_loop, daemon=True).start()


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
        "SELECT key, value FROM meta WHERE key IN ('claude_remote_usage_last_error', 'claude_remote_usage_last_attempt_at')"
    ).fetchall()
    status = {r["key"]: r["value"] for r in rows}
    return {
        "last_attempt_at": status.get("claude_remote_usage_last_attempt_at"),
        "last_error": status.get("claude_remote_usage_last_error") or None,
    }


def get_remote_window(conn):
    """Returns {"five_hour": {"start", "end", "remote_pct"} | None,
    "weekly": {...} | None, "remote_status": {...}, "remote_polled_at": ...}
    using Anthropic's own polled usage data — see server.build_limit_windows,
    which prefers this over get_heuristic_window whenever a value is present."""
    remote = get_latest_remote_usage(conn)
    result = {"five_hour": None, "weekly": None,
              "remote_status": get_remote_usage_status(conn),
              "remote_polled_at": remote["polled_at"] if remote else None}
    if not remote:
        return result
    if remote["five_hour_resets_at"]:
        end = server._parse_ts(remote["five_hour_resets_at"])
        if end is not None:
            result["five_hour"] = {"start": end - FIVE_HOUR_PERIOD, "end": end, "remote_pct": remote["five_hour_pct"]}
    if remote["weekly_resets_at"]:
        end = server._parse_ts(remote["weekly_resets_at"])
        if end is not None:
            result["weekly"] = {"start": end - WEEKLY_PERIOD, "end": end, "remote_pct": remote["weekly_pct"]}
    return result
