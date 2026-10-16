#!/usr/bin/env python3
"""Fake Claude Code and Codex logs plus limit history, for screenshots.

    python3 scripts/demo-data.py DIR

Writes DIR/home (a stand-in $HOME with ~/.claude and ~/.codex logs) and
DIR/usage.sqlite3, then prints the usage-cli command that serves the app UI
from them. Every project, prompt and title here is made up.
"""
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

random.seed(7)
UTC = timezone.utc
NOW = datetime.now(UTC).replace(microsecond=0)
DAYS = 75

PROJECTS = ["aurora-api", "pixel-garden", "ledger-cli", "trail-maps", "recipe-box", "dotfiles"]
PROJECT_WEIGHTS = [5, 4, 3, 2, 2, 1]
TASKS = [
    "Add rate limiting to the upload endpoint",
    "Fix flaky snapshot tests in CI",
    "Migrate auth to passkeys",
    "Profile the slow tile renderer",
    "Write a CSV importer for bank exports",
    "Refactor the settings screen",
    "Add offline mode with a sync queue",
    "Investigate memory leak in the worker pool",
    "Port the build to Swift Package Manager",
    "Design the onboarding empty states",
    "Add dark mode to the map legend",
    "Speed up cold start",
    "Clean up the zsh prompt",
    "Paginate the recipes API",
    "Add retry with backoff to webhooks",
    "Review the database migration plan",
]
FOLLOW_UPS = [
    "Looks good. Can you also add tests for the edge cases?",
    "That broke the build, here is the error. Can you fix it?",
    "Now update the README with the new flag.",
    "Can we make this faster? It still takes a few seconds.",
    "Good. Commit-ready? Run the linter first.",
    "Explain why the second approach is safer.",
]
REPLIES = [
    "I traced the problem to the request handler: it reads the whole body before checking the size. I moved the check up front and added a test.",
    "The flakiness comes from a timestamp in the snapshot. I froze the clock in the fixture, and the test now passes 50 runs in a row.",
    "Done. The new code path is behind a flag so we can roll it out gradually.",
    "I profiled it: most of the time goes to re-parsing the same file. Caching the parsed result cuts the run from 4.1s to 0.6s.",
    "Here's the plan: split the migration into three steps so each one can be rolled back on its own.",
]
TOOLS = ["Read", "Edit", "Bash", "Grep", "Write", "Glob"]
CLAUDE_MODELS = [("claude-opus-5-5", 6), ("claude-sonnet-5-5", 3), ("claude-haiku-5-5", 1)]
CODEX_MODELS = [("gpt-5.5", 3), ("gpt-5.4", 1)]


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def pick(weighted):
    return random.choices([w[0] for w in weighted], [w[1] for w in weighted])[0]


def session_starts():
    """Work sessions, busier on weekdays, ramping up over the period."""
    starts = []
    for d in range(DAYS, -1, -1):
        day = (NOW - timedelta(days=d)).replace(hour=0, minute=0, second=0)
        weekend = day.weekday() >= 5
        count = random.choice([0, 0, 1, 1, 2] if weekend else [1, 2, 2, 3, 3, 4])
        count += 1 if d < 21 and not weekend else 0
        for _ in range(count):
            start = day + timedelta(hours=random.choice([9, 10, 11, 13, 14, 15, 16, 20, 21]), minutes=random.randint(0, 59))
            if start < NOW - timedelta(minutes=20):
                starts.append(start)
    # Something running right now, so the 5-hour window is lively.
    starts.append(NOW - timedelta(hours=1, minutes=40))
    return sorted(starts)


def claude_usage(model, turn_index):
    heavy = model.startswith("claude-opus")
    return {
        "input_tokens": random.randint(4, 40),
        "output_tokens": random.randint(800, 6000 if heavy else 3500),
        "cache_read_input_tokens": random.randint(60_000, 260_000) + turn_index * 9_000,
        "cache_creation": {"ephemeral_5m_input_tokens": random.randint(2_000, 24_000), "ephemeral_1h_input_tokens": 0},
    }


def write_claude(home):
    root = os.path.join(home, ".claude", "projects")
    for start in session_starts():
        project = random.choices(PROJECTS, PROJECT_WEIGHTS)[0]
        cwd = f"/Users/demo/code/{project}"
        folder = os.path.join(root, cwd.replace("/", "-"))
        os.makedirs(folder, exist_ok=True)
        sid = str(uuid.UUID(int=random.getrandbits(128)))
        model = pick(CLAUDE_MODELS)
        task = random.choice(TASKS)
        lines, t, parent = [], start, None

        def record(obj):
            nonlocal parent
            uid = str(uuid.UUID(int=random.getrandbits(128)))
            obj.update({"uuid": uid, "parentUuid": parent, "sessionId": sid, "cwd": cwd,
                        "timestamp": iso(t), "isSidechain": False, "version": "2.1.0"})
            parent = uid
            lines.append(obj)

        prompts = [task] + random.sample(FOLLOW_UPS, random.randint(1, 4))
        for i, prompt in enumerate(prompts):
            record({"type": "user", "message": {"role": "user", "content": prompt}})
            for step in range(random.randint(6, 26)):
                t += timedelta(seconds=random.randint(8, 70))
                if t > NOW:
                    break
                tool = random.choice(TOOLS)
                last = step >= 5 and random.random() < 0.12
                content = [{"type": "text", "text": random.choice(REPLIES)}] if last else \
                    [{"type": "tool_use", "id": f"toolu_{random.getrandbits(48):x}", "name": tool, "input": {}}]
                record({"type": "assistant", "message": {
                    "id": f"msg_{random.getrandbits(64):x}", "role": "assistant", "model": model,
                    "content": content, "usage": claude_usage(model, step)}})
                if last:
                    break
            t += timedelta(minutes=random.randint(1, 12))
            if t > NOW:
                break
        lines.insert(1, {"type": "ai-title", "aiTitle": task, "sessionId": sid})
        with open(os.path.join(folder, f"{sid}.jsonl"), "w") as f:
            f.writelines(json.dumps(line) + "\n" for line in lines)


def codex_rate_limits(at):
    weekly_reset = NOW + timedelta(days=2, hours=9)
    return {
        "limit_id": "codex", "plan_type": "plus",
        "primary": {"used_percent": 18.0, "window_minutes": 300,
                    "resets_at": int((NOW + timedelta(hours=3, minutes=10)).timestamp())},
        "secondary": {"used_percent": 34.0 if at > NOW - timedelta(hours=6) else 22.0, "window_minutes": 10080,
                      "resets_at": int(weekly_reset.timestamp())},
    }


def write_codex(home):
    for start in session_starts()[::2]:
        project = random.choices(PROJECTS[:4], PROJECT_WEIGHTS[:4])[0]
        cwd = f"/Users/demo/code/{project}"
        sid = str(uuid.UUID(int=random.getrandbits(128)))
        folder = os.path.join(home, ".codex", "sessions", start.strftime("%Y/%m/%d"))
        os.makedirs(folder, exist_ok=True)
        model = pick(CODEX_MODELS)
        t = start
        lines = [{"timestamp": iso(t), "type": "session_meta", "payload": {"id": sid, "cwd": cwd, "originator": "codex_cli_rs"}},
                 {"timestamp": iso(t), "type": "turn_context", "payload": {"model": model, "cwd": cwd}}]
        task = random.choice(TASKS)
        for prompt in [task] + random.sample(FOLLOW_UPS, random.randint(0, 2)):
            lines.append({"timestamp": iso(t), "type": "event_msg", "payload": {"type": "task_started"}})
            lines.append({"timestamp": iso(t), "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}]}})
            for _ in range(random.randint(2, 8)):
                t += timedelta(seconds=random.randint(10, 60))
                if t > NOW:
                    break
                lines.append({"timestamp": iso(t), "type": "response_item", "payload": {
                    "type": "function_call", "name": "shell", "arguments": "{}", "call_id": f"call_{random.getrandbits(40):x}"}})
                lines.append({"timestamp": iso(t), "type": "event_msg", "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {
                        "input_tokens": random.randint(15_000, 90_000), "cached_input_tokens": random.randint(10_000, 80_000),
                        "output_tokens": random.randint(200, 2_500), "reasoning_output_tokens": random.randint(0, 900)}},
                    "rate_limits": codex_rate_limits(t)}})
            lines.append({"timestamp": iso(t), "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": random.choice(REPLIES)}]}})
            t += timedelta(minutes=random.randint(2, 15))
        name = f"rollout-{start.strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"
        with open(os.path.join(folder, name), "w") as f:
            f.writelines(json.dumps(line) + "\n" for line in lines)
        with open(os.path.join(home, ".codex", "session_index.jsonl"), "a") as f:
            f.write(json.dumps({"id": sid, "thread_name": task}) + "\n")


def seed_limits(db_path):
    """Remote limit readings can't come from logs; the real app polls them."""
    db = sqlite3.connect(db_path)
    ts = lambda dt: dt.astimezone().isoformat()

    five_reset = NOW + timedelta(hours=3, minutes=20)
    week_reset = NOW + timedelta(days=2, hours=14)
    db.execute("""INSERT INTO remote_usage_polls (polled_at, five_hour_pct, five_hour_resets_at, weekly_pct, weekly_resets_at)
                  VALUES (?, 41, ?, 63, ?)""", (ts(NOW - timedelta(minutes=4)), ts(five_reset), ts(week_reset)))

    def window_cost(source, start, end):
        row = db.execute("SELECT COALESCE(SUM(cost), 0) FROM messages WHERE source = ? AND timestamp >= ? AND timestamp < ?",
                         (source, iso(start), iso(end))).fetchone()
        return row[0]

    def snapshot(source, window_type, start, length, at, cap):
        cost = window_cost(source, start, at)
        if cost > 0:
            db.execute("""INSERT INTO limit_snapshots (source, window_type, percentage, dollar_value, window_start, window_end, created_at)
                          VALUES (?, ?, ?, ?, ?, ?, ?)""",
                       (source, window_type, min(100, round(cost / cap * 100)), cost, ts(start), ts(start + length), ts(at)))
        return min(100, round(cost / cap * 100))

    # Readings in proportion to the logged cost, so the limit estimates are
    # stable, and scaled so the live window lands on the polled percentage.
    five_start = five_reset - timedelta(hours=5)
    five_cap = window_cost("claude", five_start, NOW) / 0.41
    for hours_ago in range(240, 0, -5):
        start = five_start - timedelta(hours=hours_ago)
        snapshot("claude", "five_hour", start, timedelta(hours=5), start + timedelta(hours=4), five_cap)

    codex_reset = datetime.fromtimestamp(codex_rate_limits(NOW)["secondary"]["resets_at"], UTC)
    for source, current_pct, reset in [("claude", 63, week_reset), ("codex", 34, codex_reset)]:
        cap = window_cost(source, reset - timedelta(days=7), NOW) / (current_pct / 100)
        for weeks_back in range(4, -1, -1):
            start = reset - timedelta(days=7 * (weeks_back + 1))
            db.execute("INSERT OR REPLACE INTO weekly_windows VALUES (?, ?, ?, ?)", (source, ts(start), ts(start + timedelta(days=7)), ts(start)))
            for hours in range(12, 7 * 24, 12):
                at = start + timedelta(hours=hours)
                if at > NOW:
                    at = NOW - timedelta(minutes=5)
                pct = snapshot(source, "weekly", start, timedelta(days=7), at, cap)
                db.execute("INSERT OR REPLACE INTO weekly_daily_max_pct VALUES (?, ?, ?, ?, ?)",
                           (source, ts(start), at.astimezone().date().isoformat(), pct, ts(at)))
                if at > NOW - timedelta(minutes=10):
                    break
    db.commit()
    db.close()


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = os.path.abspath(sys.argv[1])
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home, db_path = os.path.join(out, "home"), os.path.join(out, "usage.sqlite3")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(home)
    write_claude(home)
    write_codex(home)

    cli = ["swift", "run", "--package-path", os.path.join(repo, "macpapp"), "usage-cli",
           "--db", db_path, "--pricing", os.path.join(repo, "macpapp/Resources"), "--home", home]
    subprocess.run(cli + ["scan"], check=True)
    seed_limits(db_path)
    print("Serve it with:\n  " + " ".join(cli + ["--web", os.path.join(repo, "macpapp/Resources/web"), "serve"]))


if __name__ == "__main__":
    main()
