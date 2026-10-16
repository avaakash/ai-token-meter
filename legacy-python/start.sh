#!/usr/bin/env bash
# Starts the Claude usage dashboard in the background, waits for it to finish
# warming up (it scans Claude Code's local logs into a SQLite cache before it
# will answer any request), then opens it in your browser.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${CLAUDE_USAGE_DASHBOARD_PORT:-4870}"
URL="http://127.0.0.1:${PORT}/"
LOG="$DIR/server.log"

cd "$DIR"

if lsof -i ":$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "Dashboard already running at $URL"
else
  echo "Starting dashboard..."
  nohup python3 "$DIR/server.py" >> "$LOG" 2>&1 &
  disown

  printf "Warming up (scanning Claude Code logs into the local cache)"
  ready=false
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null -m 1 "${URL}api/usage"; then
      ready=true
      break
    fi
    printf "."
    sleep 1
  done
  echo

  if [ "$ready" != "true" ]; then
    echo "Still not responding after 60s — check $LOG for errors." >&2
    exit 1
  fi
fi

echo "Dashboard running at: $URL"
open "$URL" 2>/dev/null || true
