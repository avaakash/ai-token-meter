# 🖥️ Token Meter: the Swift package

The app's source. To build, install or release it, see the
[main README](../README.md); `../build.sh` wraps everything here.

| Target | What it is |
|---|---|
| `UsageCore` | Log scanning, SQLite cache, pricing, limits and forecasts. A port of the [Python dashboard](../legacy-python/) that keeps its database schema, so a database can move between the two |
| `TokenMeter` | The menu bar app: status item, dashboard window (WKWebView over an `app://` scheme), background sync, Open at Login |
| `usage-cli` | The backend without the UI, for debugging and UI work in a browser |
| `usage-core-checks` | Core logic checks (XCTest isn't available with the Command Line Tools alone) |

`Resources/web/index.html` is the dashboard UI. `Resources/pricing_*.json` hold
the API prices, copied into the app at build time. `Resources/AppIcon.icns` is
drawn by `../scripts/make-icon.swift`.

## 🛠️ Development

From the repo root:

```sh
swift run --package-path macpapp usage-core-checks
swift run --package-path macpapp usage-cli --db /tmp/u.sqlite3 --pricing macpapp/Resources get '/api/usage?source=all'
```

To work on the UI in a browser (`http://127.0.0.1:4871/`; macOS system
colours only apply inside the app), against made-up data:

```sh
python3 scripts/demo-data.py /tmp/token-meter-demo   # prints the serve command
```

Add `?theme=dark` or `?motion=off` to the URL to force the theme or turn off
animation.

To run the built app against a scratch database:

```sh
TOKEN_METER_DB=/tmp/u.sqlite3 "build/Token Meter.app/Contents/MacOS/TokenMeter"
```

## ⚠️ Limitations

- Release builds are ad-hoc signed, not notarized, so a downloaded copy needs
  **Open Anyway** once. A smooth double-click install needs Developer ID
  signing and notarization.
- The Claude usage endpoint and Codex's `app-server` rate-limit call aren't
  documented public APIs and may change.
