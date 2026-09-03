---
last-updated: 2026-09-03
verified-against: 4d6e1a3b7c31a073417b9b681d5a009e288b71cf
---

# Features: Agent Subscription Usage

Display-only Claude/Codex capacity. Dashboard rendering details:
`dashboard.md`. Other operations surfaces: `ops.md`.

## Agent Subscription Usage

Status: implemented

Description: Display provider-aware Claude and Codex subscription capacity on
the Operations Overview without conflating quota with repo health, run state,
or evaluation policy.

Modules: usage, statusline, runs, models, config, api, intake_agent, semantic,
web, docs.

APIs: protected `GET /api/usage`; internal `UsageService`,
`ClaudeUsageStore`, `ClaudeUsageCollector`, `parse_claude_usage_cache`,
`WindowBoundary`, `parse_claude_stream_usage`,
`parse_claude_stream_boundaries`, `merge_window_boundaries`,
`parse_claude_statusline`, `read_claude_statusline`, `read_codex_usage`;
legacy console script `orch-hub-statusline`; `ORCH_HUB_CODEX_HOME`.

Implemented behavior:

- `GET /api/usage` returns both providers fail-soft in a normalized payload:
  provider key, availability/detail, plan, windows, used/remaining percentages,
  reset times, observation timestamp, staleness, and optional aggregate credits.
- Codex usage is read only from local session rollout `rate_limits` under the
  configured Codex home; orch-hub never invokes Codex, opens credentials, or
  consumes reset credits.
- Claude's primary figure carrier is
  `~/.claude.json.cachedUsageUtilization`: the parser retains only
  `fetchedAtMs`, window reset instants, and the unambiguous
  `limits[].percent` entries paired by kind/reset. Account, credential, project,
  and machine fields are neither persisted nor logged.
- A stale/empty Overview read dispatches `ClaudeUsageCollector` in the
  background; `RunManager` requests the same refresh once at each observed
  live-to-ended transition. The collector is non-blocking single-flight,
  freshness/backoff/60s-timeout bounded, runs `claude -p "/usage"` from an
  empty cwd, and accepts only a JSON envelope with explicit zero turns/cost.
  Failures retain the previous figures and their real `fetchedAtMs`.
- `UsageService` ingests the cache first and the legacy statusLine sink second;
  both merge into `ClaudeUsageStore` per window, so existing wrapper
  installations remain compatible without being required. Cross-carrier reset
  instants within five minutes are treated as one jitter-tolerant window and
  converge to the later identity; farther-apart instants preserve rollover
  ordering, so same-window stale writers cannot lower the stored percentage.
- The best-effort observer wired into intake and semantic `claude -p`
  stream-json backends sees no used-percentage on verified versions: 2.1.218
  emits no rate-limit signal, while 2.1.220 emits a percentage-less
  `rate_limit_event`. The latter advances only `resets_at` on an already-stored
  known window; it never creates a window or changes figures, plan, or
  `observed_ts`. Unsupported/absent output records nothing.
- The legacy statusLine writer is opt-in and pass-through: the hub never writes
  `~/.claude/**`, the sink stores only `rate_limits` plus `subscription_type`,
  and wrapped operator statusline argv/stdin/stdout/stderr/exit code are relayed.
- `launchd deploy|update` no longer inspect Claude settings or print wrapper
  guidance; no statusLine setup is required, and existing settings are never
  edited or removed automatically.
- The Overview renders separate Claude/Codex cards with percent-first bars,
  reset timing, freshness/stale flags, explicit unknown/unavailable states, and
  aggregate reset-credit availability. A passed boundary reads as no active
  window/starts at next call; retained figures are last-observed with no fill
  or inferred remaining percentage. A refreshed open window whose figure
  predates its start reads usage unknown. Source failures degrade only this
  section and never create Needs attention rows or affect readiness, run state,
  launch, or intake gates.
- Usage exports no capacity-decision or single-provider scheduler read. All
  consumers are display/observation paths; missing, stale, rolled-over,
  boundary-less, or partly unreadable windows remain explicit presentation
  states and never affect evaluation.
