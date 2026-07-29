---
last-updated: 2026-07-29
verified-against: fa9930065ae3d9050dac426640151657d71fe3a2
---

# APIs: Internal — Core and Sidecar

Registry/readiness/task-store interfaces, evaluation/static/semantic/usage
service contracts, and cross-cutting API conventions. Run/intake/control/log/
history/notifier interfaces: `internal-runtime.md`.

## Registry Provider

Input: config with ai-native-development path.

Output: list of managed repo models.

Behavior: command source first, registry-file fallback second. An exit-0 command with invalid JSON raises `RegistryError`; command missing/non-zero falls back; missing fallback file returns an empty list.

## Readiness Checker

Input: managed repo model.

Output: readiness report with boolean `ready` and list of failed checks.

Behavior: pure inspection and subprocess checks only; no mutation. Missing repo path skips subprocess execution but records `git_clean` and `aii_status` as failed so launch readiness still fails closed.

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

## Task Evaluation Service

Input: managed repo, exact task id, and optional evaluation id.

Output: `EvaluationTarget`, `RelatedRun` values, or a bounded `EvidencePack`
persisted as `evidence.json` under the hub data dir.

Behavior: `EvaluationService` is gated by `Config.evaluation_enabled` and raises
`EvaluationDisabledError` from every operation when disabled. Target resolution
checks active task files before archive files. Related-run aggregation loads
persisted run records read-only via `runs.load_records` and matches exact
`(repo_path, task_id)`. Evidence building reads run events, session snapshots,
bounded orchestrator logs, bounded `spawn.out`, terminal record fields, and
best-effort git-window commits; per-run artifact failures become warnings, while
target task IO errors fail the build. Persistence writes only
`<data_dir>/evaluations/<repo>/<task>/<evaluation-id>/evidence.json` and never
mutates target repos, run records, `.ai/`, or `.ai-tasks/`.
Persisted evaluation listing and artifact reads are structural and contained:
`list_evaluations` reports newest-first ids plus artifact filenames, while
`read_artifact` parses JSON artifacts and `read_text_artifact` reads text
artifacts such as semantic `report.md` for a known evaluation dir. Corrupt JSON
fails loudly. `prune_evaluations`/`auto_prune` enforce the per-task retention
cap under the hub-owned evaluation tree; top-level generation paths call
retention only after their complete artifact set is durable and protect the
just-written id.

## Static Metrics Evaluator

Input: config, an `EvidencePack`, or a managed repo plus exact task id and
optional evaluation id.

Output: `RunMetrics`, `StaticMetricsReport`, and a persisted
`static_metrics.json` beside the pack's `evidence.json`.

Behavior: `StaticEvaluator` is gated by `Config.evaluation_enabled` and raises
`EvaluationDisabledError` from run/report/generate operations when disabled.
`run_metrics(pack)` is pure over bounded pack evidence and computes run counts,
evidence-only outcome partition, failure/diagnostic availability, human-input
count, dev/review/plan-gate session counts, context/turn/duration aggregates,
warning count, and target active/archive source. `build_report(pack)` parses
the task file content from the pack for frontmatter legality, required
sections, session-log structure, review coverage, verdict/remediation counts,
status-transition path, and completion hygiene, then returns five transparent
score components with evidence-backed warnings and confidence. `generate_metrics`
delegates to `EvaluationService.generate_evidence`, writes `static_metrics.json`
atomically into the same evaluation artifact directory, and removes the
reserved dir on report-write failure. It mints evidence with retention deferred
and prunes only after the static artifact is durable. HTTP/dashboard
presentation wraps it outside the module through the evaluation API routes and
Layer 1 dashboard view; the evaluator itself never mutates target repos or run
records.

## Semantic Evaluator

Input: config, an `EvidencePack` plus `StaticMetricsReport`, or a managed repo
plus exact task id.

Output: `SemanticReport`, a deterministic Markdown render, and persisted
`semantic_report.json` plus `report.md` beside the pack/static artifacts.

Behavior: `SemanticEvaluator` is gated by `Config.evaluation_enabled` and raises
`EvaluationDisabledError` when disabled. `build_semantic_evidence(pack, static)`
constructs a bounded model input from the task file, static metrics, capped git
subjects, and explicit omissions; raw logs and unavailable `.ai/` snippets/diffs
are omitted and lower confidence rather than failing. `build_prompt` requires a
sentinel-wrapped JSON report. `parse_report` stamps provenance from the harness,
requires scored rationales and evidence-backed findings, and treats malformed
model output as `SemanticEvaluationError`. `evaluate(repo, task_id)` mints a
fresh evaluation dir, writes evidence/static/semantic/Markdown atomically,
removes the whole reserved dir on backend/model/schema failure, and prunes older
evaluations only after the whole semantic artifact set is durable.

`SemanticEvaluator.doctor()` delegates to the backend's no-cost availability
probe and returns `SemanticDoctor` (`backend`, `model`, `schema_version`,
`available`, `detail`) when enabled; disabled mode raises
`EvaluationDisabledError`. The concrete Claude backend's doctor resolves
`claude` on the augmented run PATH without launching a subprocess or model turn.

`ClaudeSemanticBackend` is the default concrete backend. It runs one stateless
Claude CLI turn with all built-in tools disabled (`--tools ""`), safe mode,
no session persistence, plan-mode writes blocked, and a fresh empty cwd outside
the target repo; this enforces evidence-boundedness structurally rather than
trusting prompt instructions. It accepts an optional usage observer called with
stdout after the turn returns; observer failures are swallowed so usage
bookkeeping cannot change semantic-evaluation outcomes.

## Usage Service

Input: config with `codex_home`, optional `ClaudeUsageStore`, and injectable
clock.

Output: `SubscriptionUsage`, `ProviderUsage`, `UsageWindow`, `WindowBoundary`,
and optional `UsageCredits` values; `UsageService.claude_observer()` callback
for hub-driven Claude stdout.

Behavior: `read_codex_usage(codex_home)` scans the newest bounded Codex session
rollout files for `rate_limits`, normalizes dynamic buckets plus optional
primary/secondary windows and aggregate credits, and never invokes Codex or
opens credential/reset-credit identifiers. `ClaudeUsageStore` persists the
latest Claude observation under `<data_dir>/usage/claude.json` with per-writer
temp files plus `os.replace`; `read_claude_statusline(path)` reads the bounded
operator-installed sink `<data_dir>/usage/claude-statusline.json` and
`UsageService` merges it into the store per window, not by whole-record write
recency. Merge identity is store-internal `UsageWindow.figure_instant`; same
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
Claude figure parsers accept only verified statusLine-shaped
`rate_limits.five_hour/seven_day.used_percentage` windows. `UsageService`
serializes Claude store writes, aggregates Claude then Codex behind a 30s
cache, and returns fail-soft unavailable provider snapshots on missing or
failed sources. All usage data is display-only and must not affect readiness,
attention, run state, launch, or intake.

## API Conventions

- Prefer absolute paths internally; avoid exposing more path data than the UI needs.
- Keep JSON stable and explicit.
- Include `ready: false` with reasons instead of raising for ordinary readiness failures.
- Keep display metadata such as commit history and housekeeping marker state
  separate from launch readiness.
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
