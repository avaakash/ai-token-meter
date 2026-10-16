# 💸 Claude Usage Dashboard

> *"How much would this have cost me if I were paying per token?"*
> — you, on a subscription plan, purely out of morbid curiosity

A local dashboard that reads your Claude Code session transcripts and shows you
the **token usage** and the **estimated bill you're not actually paying** thanks
to your subscription. It's basically a fun little "receipt you'll never get" for
your AI habit. 🧾✨

⚠️ **Let's be honest about the use-case:** this is mostly a toy. If you're on a
Pro/Max plan, these dollar figures are *hypothetical* — a way to visualize what
API pricing *would* have charged you. The genuinely useful bits (real rate-limit
windows, actual usage %) are a small slice. The rest is for the dopamine of
watching a big number go up. 📈

Runs entirely on `localhost` — 🔒 **nothing leaves your machine.**

---

## 🚀 Quick start

```
./start.sh
```

Starts the server in the background (if it isn't already running), waits for its
startup scan to finish, prints the URL, and opens your browser. Run it again
while it's already up and it just opens the existing tab — no duplicate servers.

🌐 Default URL: **http://127.0.0.1:4870/**

Port 4870 taken? Override it:

```
CLAUDE_USAGE_DASHBOARD_PORT=5050 ./start.sh
```

🛑 To stop it: `lsof -i :4870` to find the process, then `kill` it. (If you set
it up as a `launchd` service below, use `launchctl bootout` instead — a plain
`kill` just gets respawned.)

---

## ⚙️ What happens on startup

Every time the server boots it runs a full **incremental scan** of your Claude
Code logs (`~/.claude/projects/**/*.jsonl`) *before* accepting its first request
— matching timestamps, tokens, and costs into a local `usage.sqlite3` cache. On
a machine with a lot of history this takes a few seconds, which is why
`start.sh` politely waits instead of opening a blank page. After that, scans are
incremental (every ~60s while the tab is open, plus once per request) so they're
snappy. ⚡

---

## 🔌 Claude API sync (the actually-useful part)

The **"Rate limit windows"** section can show your *real* usage percentage and
reset times straight from Anthropic — not a local guess.

Click the **"Claude API ⓘ"** button on the dashboard for live connection status
and setup steps. Short version: install the
[Claude Code CLI](https://docs.claude.com/en/docs/claude-code/overview) and log
in once (`claude auth login`, or just use Claude Code normally). The dashboard
reads the OAuth token Claude Code already keeps in your macOS Keychain — 🔑 no
separate API key, no extra login.

It re-syncs every 10 minutes automatically, or on demand via **"Sync now."**

Skip this and everything still works — it just falls back to a local heuristic
for window boundaries and drops the "Claude-reported %" line.

---

## 📂 What's in the box

| File | Purpose |
|---|---|
| 🐍 `server.py` | Backend: log scanning, SQLite cache, pricing, HTTP API |
| 📊 `index.html` | Main dashboard page |
| 📋 `sessions.html` | All-sessions table (sortable / filterable / searchable) |
| 💵 `pricing_claude.json`, `pricing_codex.json` | Per-model rates per million tokens, with dated schedules, cache discounts, and context tiers |
| ▶️ `start.sh` | Starts the server and opens the dashboard |
| 🗄️ `usage.sqlite3` | Local cache (gitignored) — safe to delete, rebuilt from your logs |
| 📝 `server.log` | Server output (gitignored) |

---

## ♻️ Running persistently (macOS `launchd`)

Want it to start at login and restart itself if it ever crashes? Install it as a
`launchd` agent:

```xml
<!-- ~/Library/LaunchAgents/com.claude.usage-dashboard.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.usage-dashboard</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/env</string>
        <string>python3</string>
        <string>/path/to/.claude-usage-dashboard/server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/.claude-usage-dashboard</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>/path/to/.claude-usage-dashboard/server.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/.claude-usage-dashboard/server.log</string>
</dict>
</plist>
```

Then:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.claude.usage-dashboard.plist
```

To stop/restart, use `launchctl bootout` / `bootstrap` (not `kill` — `KeepAlive`
will just respawn it).

---

🎉 That's it. Go watch the imaginary money pile up.
