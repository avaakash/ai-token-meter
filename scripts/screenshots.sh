#!/usr/bin/env bash
# Regenerates docs/assets/screenshots/ from synthetic data (scripts/demo-data.py),
# never from your own logs. Needs a Chromium-based browser for headless capture:
#   CHROME=/path/to/chrome ./scripts/screenshots.sh
set -euo pipefail
cd "$(dirname "$0")/.."

CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
[[ -x "$CHROME" ]] || { echo "set CHROME to a Chrome or Chromium binary" >&2; exit 2; }
demo="$(mktemp -d)"
out="docs/assets/screenshots"
port=4871

python3 scripts/demo-data.py "$demo" >/dev/null
swift build --package-path macpapp --product usage-cli
cli="$(swift build --package-path macpapp --show-bin-path)/usage-cli"
"$cli" --db "$demo/usage.sqlite3" --pricing macpapp/Resources --home "$demo/home" --web macpapp/Resources/web --port "$port" serve 2>/dev/null &
server=$!
trap 'kill $server 2>/dev/null; rm -rf "$demo"' EXIT
for _ in $(seq 1 50); do curl -s -o /dev/null "http://127.0.0.1:$port/" && break; sleep 0.2; done

session="$(sqlite3 "$demo/usage.sqlite3" "SELECT session_id FROM messages WHERE source = 'claude'
  AND timestamp > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-3 days') GROUP BY session_id ORDER BY SUM(cost) DESC LIMIT 1")"

mkdir -p "$out"
for view in overview limits cost sessions projects models "session/$session"; do
  for theme in light dark; do
    "$CHROME" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=2 --window-size=1280,820 \
      --virtual-time-budget=8000 --screenshot="$out/${view%%/*}-$theme.png" \
      "http://127.0.0.1:$port/?motion=off&theme=$theme#$view" 2>/dev/null
  done
done
echo "Wrote $out/"
