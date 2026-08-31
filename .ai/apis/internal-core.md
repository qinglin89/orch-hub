---
last-updated: 2026-08-31
verified-against: af1d7a6a07599b2a23c4f0c723e0b5ce3aa94fec
---

# APIs: Internal — Core and Usage

Registry/readiness/task-store/usage interfaces and cross-cutting API
conventions. Evaluation/static/semantic/report-catalog contracts:
`internal-evaluation.md`. Run/intake/control/log/history/notifier interfaces:
`internal-runtime.md`.

## Registry Provider

Input: config with mandrel path.

Output: list of managed repo models.

Behavior: command source first, registry-file fallback second. An exit-0 command with invalid JSON raises `RegistryError`; command missing/non-zero falls back; missing fallback file returns an empty list.

## Readiness Checker

Input: managed repo model.

Output: readiness report with boolean `ready`, failed checks, and a
display-only `ProtocolPlacement` passenger.

Behavior: pure inspection and subprocess checks only; no mutation. The one
`mandrel status --json <repo>` call uses its exit code for the unchanged gate
and relays schema-2 `placed_line` / structurally marked placement finding
without git, receipt reads, revision comparisons, or a hub vocabulary. Missing
repo path skips subprocess execution but records `git_clean` and
`mandrel_status` as failed so launch readiness still fails closed; old,
missing, malformed, or unexpected JSON produces no placement.

## Housekeeping Marker

Input: managed repo model.

Output: boolean housekeeping-pending marker state.

Behavior: checks only `<repo>/.ai/.housekeeping-pending` for display metadata.
The value is included in repo payloads but never participates in readiness or
run-start gates.

## Task Store

Input: repo path.

Output: `RepoTaskInventory` values containing repo fields, `index_present`, ordered `TaskSummary` rows, and optional raw task content for one listed task.

Behavior: parse `.ai-tasks/index.md` and task frontmatter locally. The index supplies active task ids and summaries; task frontmatter supplies authoritative status and blockers. The reusable `tasks_payload(inventories)` helper serializes as `{"repos": [...]}` for future CLI/API adapters.
`read_task_content(repo, task_id)` and `read_archived_task_content(repo,
task_id)` are the raw-body readers: they reject non-bare filenames, require the
file target to resolve under the resolved active/archive `.ai-tasks` directory,
and return `None` for absent or unsafe content.

## Usage Service

Input: config with `codex_home`, optional `ClaudeUsageStore` /
`ClaudeUsageCollector`, injectable refresh dispatch, and clock.

Output: `SubscriptionUsage`, `ProviderUsage`, `UsageWindow`, `WindowBoundary`,
and optional `UsageCredits` values; `UsageService.claude_observer()` callback
for hub-driven Claude stdout; non-blocking
`UsageService.request_claude_refresh()` callable for read/run triggers.

Behavior: `read_codex_usage(codex_home)` scans the newest bounded Codex session
rollout files for `rate_limits`, normalizes dynamic buckets plus optional
primary/secondary windows and aggregate credits, and never invokes Codex or
opens credential/reset-credit identifiers. `parse_claude_usage_cache` reads
only `fetchedAtMs`, reset instants, and `limits[].percent` figures paired by
kind/reset from the CLI's structured cache. `ClaudeUsageCollector` refreshes
that cache via timeout/backoff/freshness/single-flight-bounded
`claude -p "/usage"` and fails closed unless the JSON envelope proves zero
turns/cost; failures keep the previous source timestamp.

`ClaudeUsageStore` persists the latest observation under
`<data_dir>/usage/claude.json`; `UsageService` ingests the cache first, then
merges the bounded optional statusLine sink per window. Merge identity is
store-internal `UsageWindow.figure_instant`; same
window instants take the higher `used_percent`, newer instants replace as a
rollover, older instants are ignored, candidate-only windows are added, and
omitted stored windows are retained. Pre-`figure_instant` store windows keep an
unknown identity on read and yield once to a candidate with a real
`figure_instant`, self-healing upgraded `claude.json` files without exposing
that field in `GET /api/usage`. The provider-level `observed_ts` remains a
single freshness stamp: same-window-set merges advance to the later stamp,
while partial mixed-window-set merges floor to the older stamp so a kept or
newly-added window is not dated newer than its source observation.
`parse_claude_stream_usage(stdout, observed_ts=...)` remains wired to
hub-driven `claude -p --output-format stream-json`; no verified version carries
a used-percentage there (2.1.218 has no rate-limit signal; 2.1.220 adds a
percentage-less `rate_limit_event`). `parse_claude_stream_boundaries` and
`merge_window_boundaries` advance only the reset instant of an already-stored
known window, never its figures/plan/`observed_ts`, and never create a window.
StatusLine/stream figure parsers accept only verified
`rate_limits.five_hour/seven_day.used_percentage` windows. `UsageService`
serializes store writes, asynchronously requests refresh when its stored Claude
observation is older than the collector horizon, aggregates behind a 30s cache,
and returns fail-soft unavailable snapshots. The app gives the same request
callable to `RunManager` for terminal transitions.
`provider_usage(provider)` exposes the raw normalized provider object to the
automatic-L2 capacity gate; missing/stale/current-window ambiguity means defer.
No usage data may affect readiness, attention, run state, launch, or intake, and
manual semantic evaluation remains ungated.

## API Conventions

- Prefer absolute paths internally; avoid exposing more path data than the UI needs.
- Keep JSON stable and explicit.
- Include `ready: false` with reasons instead of raising for ordinary readiness failures.
- Keep display metadata such as commit history, housekeeping marker state, and
  mandrel-owned protocol placement separate from launch readiness.
- Raise or return operational errors for invalid registry JSON, unreadable config, or malformed user requests.
- Keep task `blocked` distinct from run `waiting`.
- Never make API handlers call orchestrator internals directly.
- API/run handlers should reuse `orch_hub.control_dir` for question, answer,
  and stop files instead of reconstructing filenames or validity rules.
- API/run handlers should reuse `orch_hub.runs.RunManager` for launch gates,
  per-repo serialization, stop requests, and end classification instead of
  rebuilding subprocess or run-dir behavior in route code.
- API/intake handlers should reuse `orch_hub.intake.IntakeManager` for intake
  gates, lifecycle state, audit events, confirmation verification, and rollback
  instead of deciding draft/task status in route code.
- API/log handlers should reuse `orch_hub.logs` for log location, bounded
  reads, and SSE framing instead of accepting client paths or rebuilding the
  orchestrator filename convention in route code.
- API/history handlers should reuse `RunManager.recent_runs` /
  `orch_hub.history` rather than reading run dirs directly in route code.
