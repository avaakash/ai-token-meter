"""Claude Code-specific usage source: log parsing, pricing, and rate-limit
windows. Everything here is specific to how Claude Code's local transcripts
(~/.claude/projects/**/*.jsonl) and Anthropic's own usage API are shaped —
see server.py for the source-agnostic plumbing that calls into this module.
"""

import glob
import hashlib
import json
import os
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import server
from pricing import resolve_rate

SOURCE_NAME = "claude"

HOME = os.path.expanduser("~")
LOG_GLOBS = [os.path.join(HOME, ".claude", "projects", "**", "*.jsonl")]
# The Claude desktop app keeps its own per-session metadata, including names
# it generated and renames made while the CLI wasn't running — neither of
# which is ever written into the transcript.
APP_SESSIONS_GLOB = os.path.join(
    HOME, "Library", "Application Support", "Claude", "claude-code-sessions", "**", "*.json"
)
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
# pricing.resolve_rate for the rule (latest "from" at-or-before a message's
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


def price_for_model(model, ts_str, prompt_tokens=0):
    """Return (input_price_per_mtok, output_price_per_mtok) for a model at a given time."""
    rate = resolve_rate(load_pricing(), model, ts_str, prompt_tokens)
    return (rate["input"], rate["output"])


def compute_costs(model, ts_str, token_fields):
    """token_fields: {"input", "output", "cache_read", "cache_5m", "cache_1h"}.
    Returns a dict: cost, cost_no_cache, cost_input, cost_output, cost_cache,
    cost_cache_read, cost_cache_write."""
    input_tokens = token_fields.get("input", 0)
    output_tokens = token_fields.get("output", 0)
    cache_read = token_fields.get("cache_read", 0)
    cache_5m = token_fields.get("cache_5m", 0)
    cache_1h = token_fields.get("cache_1h", 0)

    prompt_tokens = input_tokens + cache_read + cache_5m + cache_1h
    rate = resolve_rate(load_pricing(), model, ts_str, prompt_tokens)
    in_price, out_price = rate["input"], rate["output"]
    in_ppt = in_price / 1_000_000.0
    out_ppt = out_price / 1_000_000.0
    cost_input = input_tokens * in_ppt
    cost_output = output_tokens * out_ppt
    cost_cache_read = cache_read * rate.get("cached_input", in_price * 0.1) / 1_000_000.0
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


def _between(text, start, end):
    i = text.find(start)
    if i == -1:
        return ""
    j = text.find(end, i + len(start))
    return text[i + len(start):j if j != -1 else None].strip()


def first_prompt_text(obj):
    """The person's own words in a `user` record, or None for tool results,
    injected context (skill bodies, caveats, reminders) and bare slash
    commands. A slash command with arguments reads as typed: "/work-issue 5364"."""
    if obj.get("type") != "user" or obj.get("isMeta") or obj.get("isSidechain"):
        return None
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list):
        content = next((c.get("text") for c in content
                        if isinstance(c, dict) and c.get("type") == "text" and c.get("text")), None)
    if not isinstance(content, str):
        return None
    text = content.strip()
    if "<command-name>" in text:
        name, args = _between(text, "<command-name>", "</command-name>"), _between(text, "<command-args>", "</command-args>")
        if not name or not args:
            return None
        text = f"{name} {args}"
    elif text.startswith("<") or text.startswith("Caveat:"):
        return None
    text = " ".join(text.split())
    if text.startswith("/") and " " not in text:
        return None  # a bare slash command, typed without the <command-name> wrapper
    return text[:200] or None


def _first_prompt_in(path):
    """(session_id, text) for the file's first prompt, reading only as far
    as needed."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"type":"user"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                text = first_prompt_text(obj)
                if text and obj.get("sessionId"):
                    return obj["sessionId"], text
    except OSError:
        pass
    return None


def backfill_first_prompts(conn):
    rows = []
    for pattern in LOG_GLOBS:
        for path in glob.glob(pattern, recursive=True):
            found = _first_prompt_in(path)
            if found:
                rows.append(found)
    server.store_first_prompts(conn, rows)


def sync_session_titles(conn):
    rows = []
    for path in glob.glob(APP_SESSIONS_GLOB, recursive=True):
        try:
            with open(path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        session_id, title = meta.get("cliSessionId"), meta.get("title")
        if isinstance(session_id, str) and isinstance(title, str) and title.strip():
            rows.append((session_id, title.strip(), meta.get("titleSource") or "auto"))
    server.store_app_titles(conn, rows)


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
    first_prompt = None
    want_prompt = offset == 0  # a resumed scan has already passed the start
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

                if want_prompt and obj.get("type") == "user":
                    text = first_prompt_text(obj)
                    if text and obj.get("sessionId"):
                        first_prompt = (obj["sessionId"], text)
                        want_prompt = False
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

    if first_prompt:
        server.store_first_prompts(conn, [first_prompt])

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
# boundary: Wednesday 15:30 IST / 10:00 UTC — verified against two
# consecutive real resets, 2026-07-15 and 2026-07-22), independent of usage.
# Period is exactly 7 days, so ANY Wednesday 15:30 IST lands on the same grid
# as any other — the calendar date of the anchor below is arbitrary, only
# the weekday/time matter. (Previously assumed Thursday without having
# real reset data to check it against — that assumption was wrong: right at
# the reset instant, Anthropic's API briefly reports 0% utilization with no
# resets_at at all, which forces a fallback to this heuristic, and a
# wrong anchor day made that fallback land on an entirely different,
# bogus window until remote data resumed.)
WEEKLY_ANCHOR = datetime(2026, 7, 15, 15, 30, tzinfo=IST)  # a confirmed real Wednesday reset
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
            # The core poll above (remote_usage_polls + limit_snapshots) has
            # already succeeded and committed by this point — a bug in this
            # secondary daily-max tracking must never turn the whole poll
            # into a reported failure (confirmed: it silently killed the
            # background poll thread entirely, since nothing here caught it,
            # and made "Sync now" always report failure to the UI even
            # though the real percentage had already updated). Deliberately
            # NOT routed through record_remote_usage_error — that field
            # drives the "sync failed" status the UI shows, and the actual
            # sync did succeed here; just log it for later debugging.
            try:
                server.record_weekly_daily_max(
                    SOURCE_NAME, weekly_pct, snapshot["window_start"], snapshot["window_end"], snapshot["created_at"]
                )
            except Exception:
                traceback.print_exc()

    return {"ok": True}


def _remote_usage_poll_loop():
    while True:
        try:
            poll_remote_usage_once()
        except Exception:
            # Backstop: an uncaught exception here would silently kill this
            # daemon thread forever (confirmed — that's exactly what
            # happened), with no further polling and no visible symptom
            # beyond a slowly staling "last synced" time. Log and keep going
            # rather than let one bad poll end all future polling.
            traceback.print_exc()
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
