# 🐍 Legacy Python dashboard

> **Retired.** This is the original browser version of the dashboard. It has
> been replaced by **[Token Meter](../README.md)**, the native Mac app, which does
> everything this did and more (transcripts with per-turn costs, the Claude /
> Codex cost split, menu bar alerts). It's kept here because it's the only
> version that runs on Linux or in a plain browser, and because it's the
> reference the Swift port was checked against.

A local dashboard that reads your Claude Code and Codex session logs and shows
your token usage, the API-equivalent cost, and your real rate-limit usage. It runs
on `localhost`, and nothing leaves your machine.

## 🚀 Quick start

```
cd legacy-python
./start.sh
```

This starts the server in the background, waits for the first scan of your logs,
and opens **http://127.0.0.1:4870/**. Running it again just reopens the tab.

- **Different port:** `CLAUDE_USAGE_DASHBOARD_PORT=5050 ./start.sh`
- **Stop it:** find the process with `lsof -i :4870`, then `kill` it.

Needs Python 3 with only the standard library. Pricing is read from
`pricing_claude.json` and `pricing_codex.json` in this folder. They're a
separate copy from the Mac app's (`../macpapp/Resources/`) and no longer kept
in sync with it.

## 🔌 Live limits

The rate-limit cards show your real usage % and reset times:
- **Claude:** read with the OAuth token Claude Code keeps in your macOS Keychain.
- **Codex:** read through `codex app-server`.

Both re-sync every 10 minutes, or on demand with **Sync now**. Without them the
cards fall back to estimated windows.

## 📂 Files

| File | Purpose |
|---|---|
| `server.py` | Log scanning, SQLite cache, pricing and the HTTP API |
| `claude.py`, `codex.py`, `pricing.py` | Per-source parsing, pricing and limits |
| `index.html`, `sessions.html` | The dashboard and the all-sessions table |
| `start.sh` | Starts the server and opens the dashboard |
| `pricing_claude.json`, `pricing_codex.json` | API prices per model |
| `test_*.py` | `python3 -B -m unittest test_pricing test_periods test_titles` |
| `usage.sqlite3`, `server.log` | Local cache and log (gitignored). Safe to delete; rebuilt from your logs. |

## 🔁 Moving to Token Meter

Token Meter can import this dashboard's history (rate-limit snapshots and weekly
readings can't be rebuilt from logs):

```
./build.sh --install --import-db legacy-python/usage.sqlite3
```

Run that from the repo root, then stop this server so the two don't both poll.
