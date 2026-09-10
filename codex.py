"""Codex CLI usage source: log parsing, pricing, and rate-limit windows.

Codex's local log (~/.codex/sessions/**/*.jsonl + ~/.codex/archived_sessions/
*.jsonl) is structurally different from Claude Code's: there's no per-project
subfolder (cwd lives inside the records), no stable per-turn message id, and
model/service-tier attribution isn't on the token-usage event itself — it's
carried by separate `turn_context`/`thread_settings_applied` records that
must be tracked statefully while walking the file in order.
"""

import hashlib
import json
import os
import select
import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import server

SOURCE_NAME = "codex"

HOME = os.path.expanduser("~")
LOG_GLOBS = [
    os.path.join(HOME, ".codex", "sessions", "**", "*.jsonl"),
    os.path.join(HOME, ".codex", "archived_sessions", "*.jsonl"),
]
PRICING_PATH = os.path.join(server.APP_DIR, "pricing_codex.json")

# ---------------------------------------------------------------------------
# Pricing ($ per 1,000,000 tokens), loaded from pricing_codex.json. Unlike
# Claude's TTL cache-write tiers, OpenAI's prompt caching is a flat discount
# on the *cached portion of input* (cached_input_tokens is a subset of
# input_tokens, not additive) — no cache-write cost concept at all. On top of
# that, OpenAI's API service tiers (standard/flex/batch/priority) apply a
# flat multiplier to every rate; Codex logs the active tier per session in
# `thread_settings_applied` events, tracked statefully the same way model is.
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
    return (applicable["input"], applicable["output"], applicable["cached_input"])


def price_for_model(model, ts_str):
    """Return (input, output, cached_input) $/Mtok for a model at a given time."""
    pricing = load_pricing()
    if not model:
        model = "unknown"

    schedule = pricing["models"].get(model)
    if schedule:
        return _pick_rate(schedule, ts_str)

    for prefix, rate in pricing["prefix_fallback"]:
        if model.startswith(prefix):
            return (rate["input"], rate["output"], rate["cached_input"])

    d = pricing["unknown_default"]
    return (d["input"], d["output"], d["cached_input"])


def _tier_multiplier(service_tier):
    pricing = load_pricing()
    multipliers = pricing.get("tier_multipliers", {})
    return multipliers.get(service_tier or "default", 1.0)


def compute_costs(model, ts_str, token_fields):
    """token_fields: {"input", "output", "cached_input", "reasoning_output",
    "service_tier"}. `input` already includes `cached_input` as a subset
    (OpenAI semantics — confirmed: input_tokens + output_tokens ==
    total_tokens). Returns the same dict shape as claude.compute_costs, with
    cost_cache_write always 0 (no cache-write concept for Codex)."""
    input_tokens = token_fields.get("input", 0)
    output_tokens = token_fields.get("output", 0)
    cached_input = token_fields.get("cached_input", 0)
    fresh_input = max(0, input_tokens - cached_input)

    in_price, out_price, cached_price = price_for_model(model, ts_str)
    mult = _tier_multiplier(token_fields.get("service_tier"))
    in_ppt = in_price * mult / 1_000_000.0
    out_ppt = out_price * mult / 1_000_000.0
    cached_ppt = cached_price * mult / 1_000_000.0

    cost_input = fresh_input * in_ppt
    cost_cache_read = cached_input * cached_ppt
    cost_output = output_tokens * out_ppt
    cost_cache_write = 0.0
    cost_cache = cost_cache_read
    cost = cost_input + cost_output + cost_cache
    # If none of this had been cached, every cached token would instead have
    # been billed as plain (fresh) input.
    cost_no_cache = input_tokens * in_ppt + output_tokens * out_ppt
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
    `messages` row. `cache_read_tokens` is reused directly for Codex's
    cached_input_tokens (same semantic: tokens read from cache instead of
    freshly processed) — no separate column needed."""
    return {
        "input": row["input_tokens"] or 0,
        "output": row["output_tokens"] or 0,
        "cached_input": row["cache_read_tokens"] or 0,
        "reasoning_output": row["reasoning_output_tokens"] or 0,
        "service_tier": row["service_tier"],
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

    # Codex archives finished sessions — moves them from sessions/**/*.jsonl
    # to archived_sessions/<basename> — some time after they're last scanned.
    # Without this, the archived path looks like a brand-new file (no
    # file_state row) and gets re-read from offset 0, inserting a second
    # full copy of every row already recorded under the old path. Detect the
    # move by basename and adopt the old row (offset, carry state) instead;
    # the stale old-path row is deleted once the rename is persisted below.
    renamed_from = None
    if prev is None:
        codex_root = os.path.join(HOME, ".codex") + os.sep
        basename = os.path.basename(path)
        for other_path, other_info in state.items():
            if other_path != path and other_path.startswith(codex_root) \
                    and os.path.basename(other_path) == basename \
                    and not os.path.exists(other_path):
                prev, renamed_from = other_info, other_path
                break

    if prev and prev["mtime"] == mtime and prev["size"] == size:
        if renamed_from:
            _persist_rename(conn, path, mtime, size, prev, renamed_from)
        return  # unchanged

    offset = prev["offset"] if prev else 0
    truncated = prev and size < prev["size"]
    if truncated:
        offset = 0  # file was truncated/rewritten; re-read from scratch

    # Model/session/cwd are carried by records seen earlier in the file, not
    # on every line — when the file is re-scanned from a mid-file offset (the
    # common case: this function is called on every dashboard load, resuming
    # from wherever the last scan left off), a token_count event can appear
    # before this call has seen any turn_context/thread_settings_applied/
    # session_meta of its own. Seed from what the previous scan of this same
    # file last resolved, so those events don't fall back to "unknown" —
    # unless the file was truncated/rewritten, in which case that carried
    # state describes content that's gone and must not leak into the new
    # content's rows.
    carry = prev if (prev and not truncated) else None
    new_rows = []
    new_offset = offset
    session_id = carry["carry_session_id"] if carry else None
    current_cwd = carry["carry_cwd"] if carry else None
    current_model = carry["carry_model"] if carry else None
    current_service_tier = (carry["carry_service_tier"] if carry else None) or "default"
    latest_rate_limits = None
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

                rtype = obj.get("type")
                payload = obj.get("payload") or {}

                if rtype == "session_meta":
                    session_id = payload.get("id") or payload.get("session_id") or session_id
                    current_cwd = payload.get("cwd") or current_cwd
                    continue

                if rtype == "turn_context":
                    current_model = payload.get("model") or current_model
                    current_cwd = payload.get("cwd") or current_cwd
                    continue

                if rtype != "event_msg":
                    continue

                event_type = payload.get("type")

                if event_type == "thread_settings_applied":
                    settings = payload.get("thread_settings") or {}
                    current_model = settings.get("model") or current_model
                    current_service_tier = settings.get("service_tier") or current_service_tier
                    continue

                if event_type != "token_count":
                    continue

                info = payload.get("info") or {}
                last = info.get("last_token_usage") or {}
                if not last:
                    continue

                rate_limits = payload.get("rate_limits")
                if rate_limits:
                    latest_rate_limits = rate_limits

                ts = obj.get("timestamp")
                input_tokens = int(last.get("input_tokens") or 0)
                output_tokens = int(last.get("output_tokens") or 0)
                cached_input = int(last.get("cached_input_tokens") or 0)
                reasoning_output = int(last.get("reasoning_output_tokens") or 0)

                costs = compute_costs(
                    current_model or "unknown", ts,
                    {"input": input_tokens, "output": output_tokens, "cached_input": cached_input,
                     "reasoning_output": reasoning_output, "service_tier": current_service_tier},
                )

                day, weekday, hour = server.day_fields(ts)
                message_id = f"codex:{path}:{line_start}"

                new_rows.append(
                    (
                        message_id,
                        SOURCE_NAME,
                        current_model or "unknown",
                        ts,
                        day,
                        weekday,
                        hour,
                        server.project_name_from_cwd(current_cwd),
                        current_cwd,
                        session_id,
                        input_tokens,
                        output_tokens,
                        cached_input,   # cache_read_tokens (reused semantic)
                        0,               # cache_5m_tokens — N/A for Codex
                        0,               # cache_1h_tokens — N/A for Codex
                        reasoning_output,
                        current_service_tier,
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

    if latest_rate_limits is not None:
        _store_rate_limits(conn, latest_rate_limits, origin="log")

    _persist_rename(
        conn, path, mtime, size,
        {"offset": new_offset, "carry_model": current_model, "carry_service_tier": current_service_tier,
         "carry_session_id": session_id, "carry_cwd": current_cwd},
        renamed_from,
    )


def _persist_rename(conn, path, mtime, size, info, renamed_from):
    """Write path's file_state row from `info` (offset + carry_*), and if this
    call resolved a sessions/ -> archived_sessions/ move, drop the old path's
    now-stale row so it can't be mistaken for a rename source again."""
    conn.execute(
        """
        INSERT OR REPLACE INTO file_state
        (path, mtime, size, offset, carry_model, carry_service_tier, carry_session_id, carry_cwd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (path, mtime, size, info["offset"], info["carry_model"], info["carry_service_tier"],
         info["carry_session_id"], info["carry_cwd"]),
    )
    if renamed_from:
        conn.execute("DELETE FROM file_state WHERE path = ?", (renamed_from,))


# ---------------------------------------------------------------------------
# Rate-limit windows
# ---------------------------------------------------------------------------

# Codex's `rate_limits.primary`/`.secondary` don't come pre-labeled as
# "5-hour" or "weekly" — each just carries its own `window_minutes`. Bucket
# by the window length actually observed rather than assuming an order.
FIVE_HOUR_MINUTES_RANGE = (200, 400)   # ~5h, some slack for rounding
WEEKLY_MINUTES_RANGE = (9000, 11500)   # ~7d (10080 observed)


def _normalize_window(win):
    """The live `codex app-server` JSON-RPC response uses camelCase
    (usedPercent/windowDurationMins/resetsAt); the passively-logged
    `payload.rate_limits` shape uses snake_case (used_percent/window_minutes/
    resets_at). Normalize both to the same snake_case shape so downstream
    code (get_remote_window, _map_window) never needs to care which source
    produced it."""
    if not win:
        return None
    return {
        "used_percent": win.get("used_percent", win.get("usedPercent")),
        "window_minutes": win.get("window_minutes", win.get("windowDurationMins")),
        "resets_at": win.get("resets_at", win.get("resetsAt")),
    }


def _normalize_rate_limits(rate_limits):
    return {
        "limit_id": rate_limits.get("limit_id", rate_limits.get("limitId")),
        "limit_name": rate_limits.get("limit_name", rate_limits.get("limitName")),
        "primary": _normalize_window(rate_limits.get("primary")),
        "secondary": _normalize_window(rate_limits.get("secondary")),
        "plan_type": rate_limits.get("plan_type", rate_limits.get("planType")),
    }


def _store_rate_limits(conn, rate_limits, origin):
    """origin: "log" (passively observed in a `token_count` event, only
    present under certain auth/plan combinations — see scan_file) or "poll"
    (actively fetched via `codex app-server`'s account/rateLimits/read — see
    poll_remote_usage_once). "poll" data is preferred when both exist since
    it's live rather than whatever Codex last happened to log; callers using
    plain INSERT OR REPLACE means whichever wrote most recently wins, so the
    background poll (every 10 min) naturally keeps this fresh regardless of
    which path last ran."""
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('codex_rate_limits', ?)",
        (json.dumps({
            "rate_limits": _normalize_rate_limits(rate_limits),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "origin": origin,
        }),),
    )


def _parse_resets_at(resets_at):
    """Codex's `resets_at` is a Unix epoch (seconds), unlike Claude's ISO
    string of the same name — confirmed by observed data (e.g. 1776318167).
    Falls back to ISO parsing defensively in case this varies by plan type."""
    if isinstance(resets_at, (int, float)):
        return datetime.fromtimestamp(resets_at, tz=timezone.utc)
    return server._parse_ts(resets_at)


def _map_window(win, now):
    if not win:
        return None
    resets_at = win.get("resets_at")
    window_minutes = win.get("window_minutes")
    used_percent = win.get("used_percent")
    if not resets_at or not window_minutes:
        return None
    end = _parse_resets_at(resets_at)
    if end is None:
        return None
    if end <= now:
        # A reset time in the past means this snapshot is stale (e.g. an
        # idle account whose window already rolled over without a newer
        # scan observing it) — showing it would imply a live quota that
        # isn't actually current.
        return None
    return {"start": end - timedelta(minutes=window_minutes), "end": end, "remote_pct": used_percent}


def get_remote_usage_status(conn):
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('codex_remote_usage_last_error', 'codex_remote_usage_last_attempt_at')"
    ).fetchall()
    status = {r[0]: r[1] for r in rows}
    return {
        "last_attempt_at": status.get("codex_remote_usage_last_attempt_at"),
        "last_error": status.get("codex_remote_usage_last_error") or None,
    }


def get_remote_window(conn):
    """Reads the latest `rate_limits` blob in `meta` — written either by the
    active poll (poll_remote_usage_once, preferred: live, doesn't require
    recent Codex usage) or passively by scan_file when Codex itself logs one
    inline. Returns {"five_hour": {...}|None, "weekly": {...}|None}; both
    None when nothing has ever been observed (e.g. pure API-key billing has
    no quota concept — see SPEC-codex-support.md section 3), when Codex's
    window shapes don't match either known bucket, or when the only
    snapshot on hand is stale (see _map_window)."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'codex_rate_limits'").fetchone()
    result = {"five_hour": None, "weekly": None, "remote_status": get_remote_usage_status(conn), "remote_polled_at": None}
    if not row:
        return result
    try:
        data = json.loads(row[0])
    except (json.JSONDecodeError, ValueError):
        return result
    rate_limits = data.get("rate_limits") or {}
    result["remote_polled_at"] = data.get("observed_at")
    now = datetime.now(timezone.utc)
    for win in (rate_limits.get("primary"), rate_limits.get("secondary")):
        if not win or not win.get("window_minutes"):
            continue
        minutes = win["window_minutes"]
        mapped = _map_window(win, now)
        if mapped is None:
            continue
        if FIVE_HOUR_MINUTES_RANGE[0] <= minutes <= FIVE_HOUR_MINUTES_RANGE[1] and result["five_hour"] is None:
            result["five_hour"] = mapped
        elif WEEKLY_MINUTES_RANGE[0] <= minutes <= WEEKLY_MINUTES_RANGE[1] and result["weekly"] is None:
            result["weekly"] = mapped
    return result


def get_heuristic_window(conn, window_type, now):
    """No known local heuristic for Codex's rate-limit windows yet (see
    SPEC-codex-support.md open question #3) — cumulative cost still shows,
    just no rate-limit card, which is correct for apikey/metered billing
    (no quota window exists to reconstruct)."""
    return None


# ---------------------------------------------------------------------------
# Live rate-limit polling via `codex app-server`
#
# Codex has no standalone HTTP usage endpoint like Anthropic's
# api.anthropic.com/api/oauth/usage. Instead, the Codex CLI itself exposes a
# newline-delimited JSON-RPC server (`codex app-server`) that reuses the
# OAuth tokens already stored in ~/.codex/auth.json (same "no separate
# login" story as Claude Code's Keychain token) and answers
# `account/rateLimits/read` with live plan/usage data. Verified manually:
# this is MORE current than passively scraping the last-logged
# `payload.rate_limits` (which can be stale/wrong — e.g. it showed a
# long-expired resets_at and a stale plan_type after the account moved from
# metered API billing to a ChatGPT plan).
# ---------------------------------------------------------------------------

APP_SERVER_TIMEOUT_SECONDS = 15
REMOTE_USAGE_POLL_SECONDS = 600  # 10 minutes, matching Claude's cadence

# When this dashboard runs as a launchd service (see README), the process's
# PATH is launchd's minimal default (/usr/bin:/bin:/usr/sbin:/sbin) — it does
# NOT include /opt/homebrew/bin or other locations an interactive shell's
# PATH would have, even though `codex` is installed and on PATH for the user.
# shutil.which() alone would fail there, so check common install locations
# too before giving up.
_CODEX_BINARY_FALLBACK_DIRS = [
    "/opt/homebrew/bin", "/usr/local/bin",
    os.path.expanduser("~/.local/bin"), os.path.expanduser("~/bin"),
]


def _find_codex_binary():
    found = shutil.which("codex")
    if found:
        return found
    for d in _CODEX_BINARY_FALLBACK_DIRS:
        candidate = os.path.join(d, "codex")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _call_app_server(requests):
    """Spawn `codex app-server`, perform the initialize handshake, send each
    (id, method, params) in `requests`, and return {id: result}. Raises
    RuntimeError with a diagnosable message on any failure (binary missing,
    timeout, JSON-RPC error)."""
    binary = _find_codex_binary()
    if not binary:
        raise RuntimeError("could not find the 'codex' CLI binary — is it installed?")
    try:
        proc = subprocess.Popen(
            [binary, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except OSError as e:
        raise RuntimeError(f"could not launch '{binary} app-server': {e}") from e

    results = {}
    errors = {}
    try:
        def send(msg):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        send({"jsonrpc": "2.0", "id": "init", "method": "initialize",
              "params": {"clientInfo": {"name": "claude-usage-dashboard", "version": "1.0"}}})
        for req_id, method, params in requests:
            send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})

        pending = {req_id for req_id, _, _ in requests}
        deadline = time.time() + APP_SERVER_TIMEOUT_SECONDS
        while pending and time.time() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.time()))
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            msg_id = msg.get("id")
            if msg_id in pending:
                if "error" in msg:
                    errors[msg_id] = msg["error"]
                else:
                    results[msg_id] = msg.get("result")
                pending.discard(msg_id)

        if pending:
            raise RuntimeError(f"'codex app-server' timed out waiting for: {', '.join(sorted(pending))}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

    if errors:
        first = next(iter(errors.values()))
        message = first.get("message", str(first)) if isinstance(first, dict) else str(first)
        raise RuntimeError(f"codex app-server error: {message}")

    return results


def _record_remote_usage_error(message):
    with server._db_lock:
        with server.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('codex_remote_usage_last_error', ?)",
                (message,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('codex_remote_usage_last_attempt_at', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.commit()


def poll_remote_usage_once():
    """Fetch live rate-limit data via `codex app-server` and persist it (or
    the error), mirroring claude.poll_remote_usage_once's contract. Safe to
    call from a background timer or the on-demand "Sync now" API trigger."""
    try:
        results = _call_app_server([("rl", "account/rateLimits/read", {})])
    except Exception as e:
        _record_remote_usage_error(str(e))
        return {"ok": False, "error": str(e)}

    data = results.get("rl") or {}
    rate_limits = data.get("rateLimits")
    if not rate_limits:
        _record_remote_usage_error("codex app-server returned no rateLimits (not authenticated via a ChatGPT plan?)")
        return {"ok": False, "error": "no rateLimits in response"}

    with server._db_lock:
        with server.get_conn() as conn:
            _store_rate_limits(conn, rate_limits, origin="poll")
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('codex_remote_usage_last_attempt_at', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('codex_remote_usage_last_error', '')")
            conn.commit()

    # Extracted from the raw response (not re-read from the DB) so this
    # doesn't need a second connection/lock right after releasing the one
    # above — create_limit_snapshot/record_weekly_daily_max below acquire
    # their own locks and would deadlock if called while still holding it.
    weekly_pct = None
    normalized = _normalize_rate_limits(rate_limits)
    now = datetime.now(timezone.utc)
    for win in (normalized.get("primary"), normalized.get("secondary")):
        if not win or not win.get("window_minutes"):
            continue
        if WEEKLY_MINUTES_RANGE[0] <= win["window_minutes"] <= WEEKLY_MINUTES_RANGE[1]:
            mapped = _map_window(win, now)
            if mapped:
                weekly_pct = mapped["remote_pct"]
            break

    if weekly_pct is not None:
        snapshot = server.create_limit_snapshot(SOURCE_NAME, "weekly", weekly_pct)
        if snapshot.get("saved"):
            # The core poll above has already succeeded and committed by this
            # point — a bug in this secondary daily-max tracking must never
            # turn the whole poll into a reported failure (confirmed: it
            # silently killed the background poll thread entirely, since
            # nothing here caught it, and made "Sync now" always report
            # failure to the UI even though the real percentage had already
            # updated). Deliberately NOT routed through
            # _record_remote_usage_error — that field drives the "sync
            # failed" status the UI shows, and the actual sync did succeed
            # here; just log it for later debugging.
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
