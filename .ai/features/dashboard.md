---
last-updated: 2026-07-24
verified-against: 3779a5d5ac7b2dd74f669990a25dc318e24f54f8
---

# Features: Mobile Dashboard — Shell and Infrastructure

Shell, theme, responsiveness, accessibility, Overview, and route-lifecycle
infrastructure. The routed view catalog (repo/task/start/history/log/
evaluation/intake views) is in `dashboard-views.md`.

## Mobile Dashboard

Status: implemented

Description: Mobile-first web UI for repo/task inspection, run start/stop,
run observation, answering waiting questions, run history, and logs.

Modules: api, web.

APIs: public `GET /`, `GET /app.js`, `GET /styles.css`; protected
repo/task/run/escalation/log/history/intake/evaluation/usage HTTP APIs, including the
run-scoped task file route, orchestrator-config preview route, and run
effective-config route. Log streaming uses the bearer-authenticated SSE endpoint
through `fetch`/ReadableStream, not browser `EventSource`.

Implemented behavior:

- The dashboard is a vanilla, build-stepless SPA served as an explicit public
  shell. It stores the bearer token in `localStorage`, attaches
  `Authorization: Bearer ...` to every data `fetch`, clears the token on 401,
  and uses no cookie or query-token credential path.
- `styles.css` owns a CSS-only neon-wasteland industrial control-console theme:
  system sans for prose, restrained monospace for identifiers/labels/output,
  independent Appearance axes for Mode (`Dark`/`Light`/`System` via
  `data-theme`, first-visit safe fallback Dark) and Palette (`Neon Cyan`/
  `Wasteland Orange` via `data-palette`, first-visit safe fallback Neon Cyan),
  token presets for Neon Cyan graphite/cyan and Wasteland Orange warm
  oxidized-metal/dust/ember dark+light variants, decorative toxic-green/
  radiation-amber/rust tokens used only for atmosphere, accessible semantic
  status colours kept distinct from decoration, and code-native grid/scanline/
  bloom/edge/glow treatments with no external fonts, build step, or decorative
  network assets. The shell-header Appearance popover persists the mode and
  palette under separate `localStorage` keys, an inline head script resolves both
  attributes before the stylesheet paints, System follows `prefers-color-scheme`
  while open without changing palette, and document `color-scheme` plus browser
  `theme-color` stay aligned with the resolved mode and palette surface.
- The shell is mobile-first responsive: phones keep the fixed bottom tab bar and
  original single column; tablet widths keep bottom navigation while only
  top-level `#view > .list` card lists become grids. Desktop widths move
  navigation to a fixed left rail flush with the viewport edge; `app.js` tags
  `#view` with a view-kind class so overview/list views fill the workspace,
  detail/form/run-task/intake views keep a bounded reading column, and logs/run
  history use a wider column, all left-anchored after the rail. Nested
  chronological feeds such as recent commits, session strips, overview
  activity/outcomes, and run cards stay single-column newest-first because they
  are not direct `#view > .list` children.
- Accessibility is route-level infrastructure: every routed view starts with one
  visible `h1.page-title`; router/full-view repaints mirror it into
  `document.title` and move focus there; `#view` is not a live region, with
  announcements scoped to status, alert, and filter-count regions. Heading
  focus remains the routed landing point, but the visible heading ring is gated
  by input modality so keyboard-driven navigation shows it while touch/pointer
  navigation does not leave a stray page-title outline. Shared focus styling and
  minimum target sizes cover dashboard controls and keyboard-scrollable blocks.
- The Operations Overview is the default authenticated landing route
  (`#/overview`). It remains client-side over existing protected APIs,
  parallel-fetching repos, tasks, live runs, escalations, run history, and
  subscription usage into summary metrics, prioritized attention items, current
  activity, recent outcomes, and display-only agent capacity. Waiting questions,
  display-readiness failures, housekeeping, live runs, and ended runs deep-link
  to existing task/repo/log views.
- Overview fetches are per-source best-effort: non-401 failures degrade only
  the affected panel to an unavailable state, while 401 still returns to the
  login gate. Its route-scoped refresh uses a chained timeout, registers its
  cancellation handle before the initial load, drops stale paints after
  navigation, and tears down through the router so requests never overlap or
  keep polling after the view is left.
- The Overview Subscription usage section is a separate section below the
  operational panels over protected `GET /api/usage`. It renders independent
  Claude and Codex cards with plan/tier, every available window, used and
  remaining percentages, reset time, freshness/stale flags, explicit
  unavailable/unknown states, and optional aggregate reset-credit availability.
  It is display-only: source failures degrade only that section, never create
  Needs attention items, and never affect readiness, run state, or run/intake
  gates.
- Route generation is the stale-view ownership guard: router bumps it on every
  entry; routed handlers, `rerender`, and intake action success/error paths
  capture before their own await/POST and skip stale paints, landing, error
  views, or handoffs. Non-router re-renders with no navigation capture the live
  generation and still paint; feature-level poll/stream/refresh handles still
  own their own cancellation.
