---
name: Usage
description: A local allowance meter for Claude Code and Codex subscriptions, with the API-equivalent cost beside it.
colors:
  ground: "#f0f0f2"
  sidebar: "#e7e7ea"
  panel: "#ffffff"
  panel-2: "#f6f6f8"
  hair: "rgba(0, 0, 0, 0.085)"
  hair-strong: "rgba(0, 0, 0, 0.14)"
  ink: "#1d1d1f"
  ink-2: "#4f4f55"
  ink-3: "#6b6b71"
  track: "#e5e5ea"
  plan: "#2a78d6"
  plan-soft: "#b7d3f6"
  plan-ink: "#1c5cab"
  plan-wash: "rgba(42, 120, 214, 0.10)"
  warn: "#fab219"
  warn-ink: "#8a5a00"
  warn-wash: "rgba(250, 178, 25, 0.16)"
  crit: "#d03b3b"
  crit-ink: "#b42a2a"
  crit-wash: "rgba(208, 59, 59, 0.10)"
  good-ink: "#006300"
  s1: "#2a78d6"
  s2: "#eb6834"
  s3: "#1baf7a"
  s4: "#eda100"
  s5: "#e87ba4"
  s6: "#008300"
  heat-0: "#ececf0"
  heat-1: "#cde2fb"
  heat-2: "#86b6ef"
  heat-3: "#3987e5"
  heat-4: "#1c5cab"
  grid: "#e6e6ea"
  axis: "#c4c4ca"
  tip: "#ffffff"
typography:
  display:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "34px"
    fontWeight: 650
    lineHeight: 1.05
    letterSpacing: "-0.025em"
    fontFeature: "\"tnum\""
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "22px"
    fontWeight: 650
    letterSpacing: "-0.015em"
  metric:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 650
    letterSpacing: "-0.01em"
    fontFeature: "\"tnum\""
  stat:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 650
    letterSpacing: "-0.01em"
    fontFeature: "\"tnum\""
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "13.5px"
    fontWeight: 650
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
  caption:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"SF Pro Text\", \"Segoe UI\", system-ui, sans-serif"
    fontSize: "11.5px"
    fontWeight: 400
  mono:
    fontFamily: "ui-monospace, \"SF Mono\", Menlo, monospace"
    fontSize: "12px"
    fontWeight: 400
rounded:
  xs: "3px"
  sm: "6px"
  control: "7px"
  md: "8px"
  lg: "10px"
  panel: "12px"
spacing:
  hair: "2px"
  xs: "4px"
  sm: "8px"
  row: "10px"
  md: "12px"
  head: "14px"
  gap: "16px"
  inset: "18px"
  page-y: "24px"
  page-x: "32px"
components:
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "14px 18px 18px"
  nav-item:
    textColor: "{colors.ink-2}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "7px 10px"
  nav-item-hover:
    backgroundColor: "{colors.hair}"
    textColor: "{colors.ink}"
  nav-item-active:
    backgroundColor: "{colors.plan-wash}"
    textColor: "{colors.ink}"
  segmented:
    backgroundColor: "{colors.hair}"
    rounded: "{rounded.md}"
    padding: "2px"
  segmented-option:
    textColor: "{colors.ink-2}"
    typography: "{typography.caption}"
    rounded: "{rounded.sm}"
    padding: "5px 8px"
  segmented-option-pressed:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
  button-sync:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.caption}"
    rounded: "{rounded.control}"
    padding: "6px 8px"
  search-field:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "9px"
    padding: "0 12px"
    height: "34px"
  meter-track:
    backgroundColor: "{colors.track}"
    rounded: "5px"
    height: "10px"
  meter-fill:
    backgroundColor: "{colors.plan}"
  meter-fill-warn:
    backgroundColor: "{colors.warn}"
  meter-fill-crit:
    backgroundColor: "{colors.crit}"
  meter-projection:
    backgroundColor: "{colors.plan-soft}"
  meter-projection-over:
    backgroundColor: "{colors.crit-wash}"
  capacity-bar:
    backgroundColor: "{colors.track}"
    rounded: "6px"
    height: "12px"
  forecast-note:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.md}"
    padding: "8px 10px"
  forecast-note-warn:
    backgroundColor: "{colors.warn-wash}"
    textColor: "{colors.ink}"
  forecast-note-crit:
    backgroundColor: "{colors.crit-wash}"
    textColor: "{colors.ink}"
  level-tag-warn:
    textColor: "{colors.warn-ink}"
  level-tag-crit:
    textColor: "{colors.crit-ink}"
  list-row:
    padding: "10px 18px"
  list-row-hover:
    backgroundColor: "{colors.panel-2}"
  table-row-selected:
    backgroundColor: "{colors.plan-wash}"
  tooltip:
    backgroundColor: "{colors.tip}"
    textColor: "{colors.ink}"
    typography: "{typography.caption}"
    rounded: "{rounded.lg}"
    padding: "9px 11px"
  link:
    textColor: "{colors.plan-ink}"
---

# Design System: Usage

## Overview

**Creative North Star: "The Data Plan Allowance"**

The subscription is an allowance, and the system is built to read like the storage meter in a Mac's settings: a native source-list sidebar on a vibrancy-grey ground, grouped inset panels sitting on that ground, and one recurring data form, the segmented capacity bar, that says how much is used, how much is projected, and how much is left. Cost sits beside headroom, never below it.

Density is that of a native utility, not a marketing dashboard. Body text is 13px, panels are separated by 16px, and metrics are grouped into hairline-divided cells inside one panel rather than broken out into a wall of separate KPI cards. Colour is almost absent: neutral greys carry structure, a single plan blue carries "used", and amber and red are held back for the moment an allowance actually runs short.

Light and dark are equal citizens. Every token has a dark value (recorded in the sidecar) applied under `prefers-color-scheme: dark` and under an explicit `[data-theme="dark"]`, with `[data-theme="light"]` able to override the system. The page runs unchanged in a browser and in the macOS app's WKWebView, offline, so nothing depends on a webfont or a CDN.

**Key Characteristics:**
- Native macOS source-list sidebar (236px) on a grey ground; content in inset panels.
- Segmented capacity bars are the one recurring data form.
- One plan blue; amber and red are reserved status colours.
- Hairline rings and separators, no card shadows.
- System SF type, tabular figures for every number, SF Mono only for IDs.
- Fills animate into place once, on the first load; every later change (period, source, sync) shows the new values instantly. Motion stops entirely under reduced motion.

## Colors

A near-monochrome Apple-grey system with one blue for "used" and two reserved alarm colours.

### Primary
- **Plan Blue** (plan): the colour of used allowance. Meter fills, share bars, weekly-history bars, the active nav icon, the focus ring, sort arrows, the "now" marker on range bars. In dark it brightens to keep contrast on the charcoal panel.
- **Plan Blue Soft** (plan-soft): projection. The part of a meter the current pace will consume before reset, the current (incomplete) week in weekly history, the expected band on range bars.
- **Plan Ink** (plan-ink): blue as text. Links, link buttons, "Copy" actions.
- **Plan Wash** (plan-wash): blue as a selection tint. Active nav item, selected table row, text selection, chart column hover.

### Secondary
- **Watch Amber** (warn / warn-ink / warn-wash): an allowance at or above 75% used, or projected to reach 75% by reset. Fill uses `warn`; the "High" tag and icon use `warn-ink`; the forecast note background uses `warn-wash`.
- **Run-out Red** (crit / crit-ink / crit-wash): an allowance at or above 90%, or projected to run out before it resets. Fill uses `crit`; the "Almost out" tag uses `crit-ink`; the forecast note and an over-limit projection segment use `crit-wash`. The limit marker on range bars is `crit`.
- **Synced Green** (good-ink): only the status dot of a source that synced successfully. Never used for money, deltas, or "under limit".

### Tertiary
- **Series Palette** (s1 to s6): the categorical palette for capacity bars, stacked cost charts and their legends, assigned in fixed order by rank. It is the dataviz reference palette validated against both panel surfaces (#ffffff light, #262628 dark). An "Other" remainder segment is always `ink-3`.
- **Heat Ramp** (heat-0 to heat-4): a single-hue plan-blue sequential ramp for the activity calendar and hour-by-weekday grid; heat-0 is the empty cell. In dark the ramp inverts so heavier usage is brighter.

### Neutral
- **Vibrancy Ground** (ground): the page behind the panels.
- **Source List Grey** (sidebar): the sidebar, one step darker than the ground in light and one step lighter in dark.
- **Panel White** (panel): every inset panel; also the cut colour of meter tick gaps and the pressed segment.
- **Inset Grey** (panel-2): row hover, the neutral forecast note, legend highlight, inline code chips.
- **Hairline** (hair / hair-strong): the 1px ring around panels and separators between rows and cells; hair-strong for input outlines and hovered controls. Also the recessed trough of segmented controls and the sync block.
- **Ink** (ink / ink-2 / ink-3): primary text and big figures; secondary text, legend names and cost deltas; tertiary meta, axis ticks and labels.
- **Track** (track): the empty part of every bar.
- **Chart Grid / Axis** (grid / axis): gridlines and the baseline of charts.
- **Tooltip** (tip): the floating tooltip surface.

### Named Rules
**The One Blue Rule.** Plan blue is the only brand hue. It means "used" or "selected", nothing else; no second accent is introduced for emphasis.

**The Reserved Alarm Rule.** Amber appears only at ≥75% used or projected, red only at ≥90% or a projected run-out. Every status colour ships with an icon and a word ("High", "Almost out", the forecast sentence), never colour alone.

**The Neutral Money Rule.** Cost deltas are `ink-2` with a sign and "vs previous period". Spending more is not an error and spending less is not a success; money never takes a status colour.

**The Labeled Series Rule.** Light slots s3 to s5 fall below 3:1 against white, so every categorical bar ships with a labeled legend (swatch, name, value, share) and hover-isolation. A series colour is never the only carrier of meaning.

**The Glyph Not Hue Rule.** Source identity is a monochrome SVG glyph in `ink-2` (asterisk for Claude, terminal for Codex), never a colour. Series colours describe models and token types, not sources.

## Typography

**Display Font:** the system UI face (-apple-system / SF Pro, with Segoe UI and system-ui fallbacks)
**Body Font:** the same system face
**Label/Mono Font:** ui-monospace / SF Mono (with Menlo), for session IDs, paths and commands only

**Character:** this is a native-Mac world, so the system face is its material, not a fallback: semibold 650 weights with slight negative tracking on large figures, regular 400 for running text, and tabular figures wherever a number can change.

### Hierarchy
- **Display** (650, 34px, 1.05, -0.025em, tabular; 30px under 860px): the big cost figure and the projected-at-reset percentage. One per panel at most.
- **Headline** (650, 22px, -0.015em): the view title at the top of each section.
- **Metric** (650, 20px, -0.01em, tabular): the "% used" figure on each allowance meter.
- **Stat** (650, 17px, -0.01em, tabular): efficiency and fact cells; 14px/600 in the cost stat strip, 13.5px in window facts.
- **Title** (650, 13.5px): panel titles, source names, empty-state headings; 15px for the brand name and inspector title.
- **Body** (400, 13px, 1.45): running text, nav items (500), meter names (600), session names (600; 500 in `ink-2` when untitled).
- **Caption** (400, 12–12.5px): meta lines, meter footers, forecast notes, legend rows, controls.
- **Label** (400, 11.5px, `ink-3`): stat keys, table headers (600), nav group headers (600), counts. Sentence case; 10.5–11px only for chart axes and calendar labels.

### Named Rules
**The Tabular Figures Rule.** Every number that can change (percentages, dollars, counts, durations) uses tabular figures so values do not jitter as they refresh.

**The Mono For IDs Rule.** Monospace is for identifiers, paths and commands. Model names, costs and labels stay in the system face.

## Layout

A two-column app shell: a sticky 236px sidebar and a main column (`minmax(0, 1fr)`) padded 24px by 32px, with each view capped at 1320px and centred. Views are switched from the sidebar, one at a time, so a section is one click away rather than stacked below the others.

Panels sit on a 16px grid gap. The overview is two columns at roughly 1.45 : 1 that always end level: allowances then the daily cost chart on the left, the cost panel then recent sessions on the right. The shorter column is filled with real content, never padding: the chart grows to the height it's given, or recent sessions gains whole rows. A full-width Activity panel (year calendar beside the hour-by-weekday grid, then four facts) closes the page; there is no separate Activity page. Inside a panel, the head is inset 14px 18px and the body 14px 18px 18px; list rows are 10px 18px. Related figures are laid out as cells of one strip (three cost stats, six efficiency cells, two-by-two facts) divided by hairlines, not as separate cards.

Responsive behaviour: at 1180px every paired grid collapses to one column, the efficiency strip goes to three cells, and the session inspector drops below the table. At 860px the sidebar becomes a sticky top bar with the brand, a horizontally scrolling nav row, and only the source switch and Sync button; main padding tightens to 18px 16px, two-up grids stack, and the efficiency strip goes to two cells.

## Elevation & Depth

The system is flat and tonal. Depth comes from the ground/panel contrast and a 1px hairline ring (`box-shadow: 0 0 0 1px var(--hair)`), never a drop shadow. The only lifted object is the floating tooltip; the only micro-lift is the pressed segment in a segmented control, mimicking the native control.

### Shadow Vocabulary
- **Hairline ring** (`box-shadow: 0 0 0 1px var(--hair)`): panels, sync button, inline code chips, the tooltip edge.
- **Pressed segment** (`box-shadow: 0 1px 2px rgba(0,0,0,0.08), 0 0 0 0.5px var(--hair)`): the selected option of a segmented control.
- **Tooltip** (`box-shadow: 0 8px 28px rgba(20,20,30,0.16), 0 1px 3px rgba(20,20,30,0.08)`; dark `0 10px 30px rgba(0,0,0,0.5), 0 1px 3px rgba(0,0,0,0.4)`): the chart and capacity tooltip only.

### Named Rules
**The Hairline Not Shadow Rule.** Panels are separated from the ground by tone and a 1px ring. If something needs to feel raised, it must actually float above the page (a tooltip); resting surfaces stay flat.

## Shapes

Softly rounded, native-control geometry. Panels are 12px; floating and grouped controls 10px (tooltip, sync block); nav items, segmented troughs and forecast notes 8px; buttons and selects 7px; segments and legend rows 6px; swatches and heat cells 3px. Bars are capsules: their radius is always half their height (10px meter at 5px, 12px capacity bar at 6px, 8px share bar at 4px, 6px range track at 3px). Status dots are 7px circles. Separators are 1px hairlines; there are no heavy borders anywhere.

## Components

### Allowance Meter (signature)
The storage-meter bar that defines the world.
- **Shape:** 10px capsule on `track`, inside 3px of vertical breathing room.
- **Layers:** the used fill in `plan` (or `warn` / `crit` by level) over the projection in `plan-soft`; when the projection runs past 100% it becomes a `crit-wash` segment. A 2px `ink` marker at 55% opacity shows how far through the window "now" is.
- **Segments:** 2px tick gaps cut in the panel colour divide the bar into the window's natural units (5 for the 5-hour window, 7 for the weekly window).
- **Head and foot:** name at body/600 on the left, the Metric figure plus "used" on the right, preceded by a level tag (icon + "High" / "Almost out") when warn or crit. The foot reads "Resets in **2h 47m** · 6:20 PM" against the API-rate cost of the window.
- **Forecast note:** below a weekly meter, an 8px-radius `panel-2` note with an info icon; it turns `warn-wash` or `crit-wash` with an alert icon when the projection is at risk, and says when the run-out would happen. Estimates are labeled as estimates.
- **Motion:** fills grow from 0 on the first load only (0.9s, `cubic-bezier(0.16, 1, 0.3, 1)`); later renders snap to the new value.

### Capacity Bar and Legend (signature)
- **Shape:** 12px capsule, segments separated by 2px gaps over `track`, coloured s1 to s6 by rank with "Other" in `ink-3`.
- **Legend:** always present; rows of 10px swatch, name in `ink-2`, value at 600, share in `ink-3`, all tabular.
- **Hover:** hovering a segment or legend row dims the others to 30% and highlights the row in `panel-2`; the tooltip names the share.
- **Motion:** segments grow in by flex-grow (0.8s, same ease) on the first load only; a period or source change snaps them to the new split.

### Source Split
- **When:** cost is shown for Both sources; hidden when one source is selected.
- **Shape:** one row per source: glyph, name at 600, an 8px `plan` share bar on `track`, amount at 600, share in `ink-3`. Sources keep the Glyph Not Hue rule, so every row's bar is the same blue; the bar length and the number carry the difference.
- **Placement:** under the total in the Overview cost panel and the Cost page's total panel. The Cost page then gives each source its own panel (amount, delta vs its own previous period, share of the total, token-type capacity bar, messages / sessions / per message), in the same order as the Limits page: Claude, then Codex.

### Big Figure
- The Display figure changes instantly on period or source change (no counting animation), followed by a neutral delta ("+48% vs previous 7 days") and a `caption` note that it is an API-equivalent estimate.

### Panels
- **Corner Style:** 12px.
- **Background:** `panel` on the `ground`.
- **Shadow Strategy:** hairline ring only (see Elevation & Depth).
- **Internal Padding:** head 14px 18px, body 14px 18px 18px; stacked rows divided by 1px `hair`.
- **Panel head:** title at 13.5px/650 with an optional `ink-3` 16px icon, and a right-aligned `ink-3` aside or a `plan-ink` link button.

### Segmented Control
- **Style:** `hair` trough, 8px radius, 2px inset; options at 12px/550 in `ink-2`.
- **Pressed:** `panel` background, `ink` text, the pressed-segment micro-shadow.
- **Use:** source switch, appearance, and the cost period (1d to 1y).

### Buttons and Links
- **Sync button:** `panel` surface with a hairline ring, 7px radius, 12px/600, leading sync icon that spins while syncing; ring strengthens to `hair-strong` on hover.
- **Link button:** text-only `plan-ink` at 12px/550, underline on hover.
- **Focus:** every interactive element shows a 2px `plan` outline at 2px offset.

### Navigation
- **Style:** native source list. Items 7px 10px, 8px radius, 13px/500 `ink-2` with a 16px line icon; group headers are 11.5px/600 `ink-3` in sentence case.
- **States:** hover `hair` fill and `ink` text; current page `plan-wash` fill, 600 weight, icon in `plan`.
- **Badges:** a right-aligned tabular count in `ink-3`, or a 7px status dot (`warn` / `crit`) on Limits when an allowance is at risk.
- **Mobile:** collapses to a horizontally scrolling row; counts, dots and group headers hide.

### Inputs / Fields
- **Search:** 34px tall, `panel`, 9px radius, `hair-strong` ring, leading search icon in `ink-3`.
- **Focus:** the ring becomes 2px `plan`.
- **Select:** transparent with a 1px `hair-strong` border, 7px radius, 12px text.

### Lists and Tables
- **Session row:** source glyph, name at 600 (falling back to the first prompt in quotes, then "Untitled · project · date" in `ink-2`), a `caption` meta line, and a right-aligned tabular cost over a relative time. Hover `panel-2`.
- **Data table:** 11.5px/600 `ink-3` headers with a hairline under them, 10px 12px cells (18px at the row edges), hairline row separators, numeric columns right-aligned and tabular, hover `panel-2`, selected `plan-wash`. The sorted header turns `ink` with a `plan` direction indicator.

### Tooltip
- `tip` surface, 10px radius, 9px 11px padding, the tooltip shadow plus hairline ring; a 650 title over rows of label and tabular value. Fades and rises 4px in 0.12s.

## Do's and Don'ts

### Do:
- **Do** express any quantity against a limit as a segmented capsule meter with used, projected and remaining parts.
- **Do** keep plan blue as the only accent; use `plan-soft` for projections and `plan-wash` for selection.
- **Do** switch a meter to amber at ≥75% and red at ≥90% or projected run-out, always with an icon and a word.
- **Do** pair every categorical colour with a labeled legend, assigning s1 to s6 in rank order and "Other" in `ink-3`.
- **Do** use tabular figures for every changing number and SF Mono only for IDs, paths and commands.
- **Do** group related figures as hairline-divided cells inside one panel.
- **Do** define every new colour token in both the light `:root` block and both dark blocks.
- **Do** animate fills only on the first load, with `cubic-bezier(0.16, 1, 0.3, 1)`, and drop all motion under `prefers-reduced-motion`. **Don't** animate recalculation when the period or source changes.

### Don't:
- **Don't** give panels drop shadows; use the 1px hairline ring.
- **Don't** colour cost deltas or money green or red.
- **Don't** identify Claude or Codex by colour; use the monochrome glyph.
- **Don't** use amber or red for anything other than an allowance at risk, its limit line, or a failed sync.
- **Don't** break metrics into a wall of separate KPI cards.
- **Don't** load webfonts or external assets; the page must render offline in WKWebView.
