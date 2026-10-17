# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Anyone who uses Claude Code and/or the Codex CLI on this Mac: mostly people on a subscription plan (Pro/Max, ChatGPT plans), and also people billed per token through the API, who track their own budgets instead of plan limits. The page must make sense to a stranger on first launch, including when one source is not installed or not yet synced, and when there is little or no history.

They open it to answer two questions, with equal weight:
1. **Am I near my limits?** Current 5-hour and weekly usage %, when each resets, and whether the current pace will exhaust the weekly quota before the reset. For per-token users, the limits are their own daily, weekly or monthly dollar budgets.
2. **What did it cost?** The API-equivalent dollar value of their usage for a chosen period, and how it trends.

Drilling into sessions, projects and models is a secondary, investigative job. Settings (budgets, the menu bar, per-model prices, model groups) is occasional upkeep.

## Product Purpose

A local, private dashboard that reads Claude Code's and Codex CLI's own session logs, prices every assistant turn at published API rates, and pairs that with the providers' own reported rate-limit usage. Success: in a few seconds a user knows how much headroom they have and what their usage would have cost.

## Positioning

Everything runs on the user's machine from logs the CLIs already write: no account, no API key, nothing uploaded. Limit percentages come from the providers themselves (Anthropic's OAuth usage endpoint, `codex app-server`), not estimates; dollar figures are explicitly hypothetical API-equivalent costs.

## Operating Context

- Token Meter, the native macOS menu bar app (`macpapp/`), shows the dashboard in a WKWebView over an `app://` scheme. Its page is `macpapp/Resources/web/index.html`, which started from `design/dashboard-redesign.html`. The retired Python dashboard (`legacy-python/`, served at `http://127.0.0.1:4870/`) keeps its own `index.html` and `sessions.html`.
- The project website is `docs/index.html` (GitHub Pages), a Persuade surface for downloading the app.
- Data API: `/api/usage?source=all|claude|codex`, `/api/app/dashboard`, `/api/app/transcript?id=`, `/api/app/settings`, `/api/all-sessions`, `/api/session?id=`, `/api/limit-snapshots`, `/api/weekly-usage`; writes are `POST /api/remote-usage/poll`, `/api/app/prices`, `/api/app/prices/reset` and `/api/app/budgets`, with parameters in the query string.
- Auto-refreshes every 60s; remote limits re-sync every 10 minutes or on "Sync now".
- Sources: Claude, Codex, or both combined.

## Capabilities and Constraints

- Plain HTML/CSS/JS, no build step, no frameworks, works offline (no CDN dependencies) because it runs inside the app.
- Must work in light and dark themes.
- Session naming: Claude sessions carry a custom or AI-generated title when Claude Code recorded one; many older Claude sessions and most Codex sessions have none, so the UI needs a meaningful fallback (not the bare folder name).
- Dollar values are hypothetical for subscription users and must be labeled as API-equivalent estimates.
- Prices are the built-in tables (`macpapp/Resources/pricing_*.json`) with the user's own per-model edits layered on top; an edit reprices all stored usage, and Reset always restores the built-in price. User settings live in the local database and survive app updates.
- Budgets use the same allowance language as plan limits: a meter per calendar day, week (from Monday) or month, amber at 75%, red at 90%.
- The menu bar item shows up to 3 limit windows chosen in Settings, each marked by its source's glyph (never a text label or colour), as used or left, optionally with time until reset. Alert colours always follow usage used.
- Model groups overlap by design (a model can be in several), so group costs are shown as each group's share of the total, never as slices of a stacked bar. Built-in groups (Anthropic, OpenAI, model families) match by rule so new models join them; user edits add or exclude models on top, and reset restores the built-ins.

## Evidence on Hand

Real usage data lives only on each user's machine. Design work uses synthetic sample data shaped like the API; never embed a real user's sessions, paths or titles in committed files.

## Product Principles

1. Headroom and cost share the first screen; everything else is one click away, not stacked below.
2. Provider-reported numbers are shown as fact; local estimates and projections are labeled as such.
3. Every number answers a question a user actually asks; no metric is shown just because it can be computed.
4. Private by construction: nothing in the UI implies data leaves the machine.
