---
version: 1
slug: "design-dashboard-redesign-html"
primary_target: "design/dashboard-redesign.html"
related_targets: ["index.html","sessions.html"]
---

# Dashboard redesign (design/dashboard-redesign.html)

Scope: the whole local dashboard (overview, limits, cost, activity, sessions, projects, models), one page with a left sidebar. Mode: Operate.

Audience and job: any Claude Code / Codex CLI subscriber, glancing between coding sessions. Two equal questions: am I near my limits, and what did it cost. Drill-down (sessions, projects, models) is secondary. Must keep: run-out forecast, activity heatmap, daily cost chart, efficiency tiles. Must not feel too sparse or playful. Sessions are named title → first prompt → "Untitled · project · date".

Constraints: plain HTML/CSS/JS, offline, light and dark, runs in WKWebView and in the browser. Design build uses synthetic sample data; it uses the live API when served.

## Direction contract

THESIS: The subscription is an allowance. The page leads with what is left and when it renews, with the API-equivalent cost of what was used beside it, and refuses the KPI-tile-wall of a generic analytics template.

OWN-WORLD: Native macOS source-list sidebar with vibrancy-grey ground; grouped inset panels on that ground; segmented capacity bars (storage-meter style) as the one recurring data form; system SF type, tabular figures, SF Mono only for IDs; one plan blue, amber at 75%, red at 90%; hairline separators inside panels, no card shadows.

STORY: The visitor sees their headroom and reset time in one glance, sees what the period would have cost and what it was spent on, and can step into any section from the sidebar without scrolling past the rest.

FIRST VIEWPORT: Sidebar 232px (sections, source switch, sync status). Main: row 1, allowance panels per source (Claude: 5-hour + weekly meters with "resets in" and the forecast line; Codex: weekly) at ~60% width, beside a Cost panel (period segmented control, large $ figure, capacity bar by model). Row 2: daily cost chart beside recent sessions with real names.

FORM: Data Plan Allowance, candidate 1 of my ordered list (pick card), seed key 43f60ef2. Signature interaction: meters and the capacity bar fill on load and re-flow when the period or source changes, with the big cost figure rolling to its new value; hovering a capacity segment isolates it and names its share.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
