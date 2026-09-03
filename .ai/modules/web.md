---
last-updated: 2026-09-03
verified-against: 4d6e1a3b7c31a073417b9b681d5a009e288b71cf
---

# Modules: Web UI

Dashboard SPA assets. The FastAPI app adapter and launchd operations are in
`service.md`; routed view details are in `web-views.md`.

## Web UI

Location: `orch_hub/web/`

Responsibility: provide the build-stepless mobile-first dashboard shell.
`index.html` defines the static app container and navigation; CSS keeps the
bottom tab bar on phone/tablet widths and turns it into a desktop left rail
flush with the viewport edge. At desktop widths, `app.js` tags `#view` with a
view-kind class: overview/list views fill the remaining workspace, detail/form/
run-task/intake views use a bounded reading column, and logs/run history use a
wider column; top-level card grids are scoped to direct `#view > .list`
children so nested chronological feeds remain single-column newest-first.
`styles.css` also owns the CSS-only neon-wasteland industrial control-console
theme: independent Appearance axes for Mode (`Dark`/`Light`/`System` on
`data-theme`) and Palette (`Neon Cyan`/`Wasteland Orange` on `data-palette`),
token presets for graphite/electric-cyan and warm oxidized-metal/dust/ember
dark+light variants, semantic status colours kept separate from decorative
toxic-green, radiation-amber, and rust tokens, system typography, monospace
operational labels/ids/output, and static grid/scanline/bloom/edge/glow effects
without external fonts/assets or a frontend build step. Dark + Neon Cyan is the
first-visit safe fallback; a static shell-header Appearance popover persists
mode and palette under separate `localStorage` keys, an inline head script
resolves both `<html>` attributes before stylesheet paint, System follows
`prefers-color-scheme` changes while open without changing palette, and document
`color-scheme` plus browser `theme-color` track the resolved mode/palette.
The shell assets are served uncached by the API so updates reach browsers on the
next load. `app.js` owns token entry/localStorage,
bearer `fetch` calls, hash routing,
route-level headings/focus/announcements, an Operations Overview default landing
route, repo/task/detail/run views, repo/task run-state signals,
searchable/counted all-tasks inventory, recent commits, task-detail Start/Stop
run controls, live per-session context strips, waiting-question answer forms,
cursor-paged/filterable grouped run history whose task groups start collapsed,
with est-vs-actual rollups, batched evaluation-state lights, and
effective-config disclosures, a run-scoped task-file view, task-evaluation
Layer 1/Layer 2 sidecar view and global evaluation-report feed reached from Run
history, remote intake start/review views, and recent/live log views;
`styles.css` holds the mobile-first responsive layout. The dashboard
reads only HTTP payloads, uses `fetch` +
`ReadableStream.getReader()` for SSE log streaming so the bearer header is
present, aborts active streams on navigation/reload/pause, and writes untrusted
log/task data as escaped HTML or text nodes.

Every routed view renders one visible `h1.page-title`; router/full-view
repaints sync it into `document.title` and move focus there, while background
Overview refreshes, intake ticks, and log appends do not move focus. The page
heading remains a programmatic landing target, but its visible focus ring is
gated by last input modality: keyboard navigation adds the ring, while
touch/pointer navigation keeps focus without a stray title outline. The main
`#view` is not an ARIA live region; scoped status/alert/count regions carry the
announcements. A monotonic route generation guards stale route completions:
router-owned landing/error work, each handler's own post-await paint, `rerender`
landing/error work, and intake action post-POST paint/navigation all drop when a
newer route owns the view. Capture point matters: action guards are captured
before the action POST, while non-router re-renders with no navigation capture
the live generation and still paint. Feature-owned cancellation handles remain
responsible for streams, intake polling, and Overview refresh.

The Overview route (`#/overview`) aggregates protected repo/task/run/
escalation/history/usage/evaluation-queue payloads into summary metrics,
prioritized attention, live activity, recent outcomes, and separate Subscription
usage/Layer 2 evaluation sections; each
non-401 source failure degrades only its panel/section, and its chained
background refresh is registered before the initial load so router teardown
cancels stale paints and polling after navigation. Its repos ready metric and
readiness-derived Attention rows use the same display-readiness rule as repo
views: raw-ready, or active/waiting live run with `git_clean` as the sole raw
failure. Subscription usage cards show Claude/Codex provider windows,
used/remaining percentages, reset time, plan, freshness/stale and unavailable
states, and aggregate reset-credit availability, but never create attention
items or affect readiness/run/intake gates. The evaluation section shows the
complete latest-attempt census and capped actionable-first rows, with inert
legacy holds explicit and last. Cancellable rows offer Cancel outside the task
link with scoped success/error announcement; nothing on the section starts or
promotes a job. Disabled hides it and read failure degrades only that section.
