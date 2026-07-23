---
last-updated: 2026-07-23
verified-against: 0e716612253d7579fc8dee78b1dc565782e2755d
---

# APIs: Internal Interfaces

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
trusting prompt instructions.

## Intake Manager

Input: managed repo, natural-language request, optional revision feedback, and
explicit confirm/cancel operations.

Output: persisted `IntakeRecord` values, metadata-only intake events, and
optional intake-ready notification dispatch.

Behavior: `IntakeManager` gates start on request text, one in-flight intake per
repo, and readiness. It stores records under `<data_dir>/intake/<id>/` and
drives an injected `IntakeAgent`. Draft/revise turns are proposal-only; confirm
is the sole write-capable turn. The manager records `confirmed` only when the
reported/draft task id is visible through `list_repo_tasks` with status
`pending` or `blocked`; otherwise it records `failed`. Failed/unverified
in-process confirms restore the proposed task file and `.ai-tasks/index.md`
snapshot. Cancel is allowed before confirming and writes no target repo files.
Successful draft/revise finalizers emit one metadata-only notification after
`awaiting_review` persistence; failures, cancels/discards, and confirms do not.

## Intake Agent

Input: request/feedback/handle plus managed repo.

Output: `AgentDraftResult` with a complete `IntakeDraft`, or
`AgentConfirmResult` with a best-effort created task id.

Behavior: the protocol requires draft/revise to be read-only and confirm to
perform the `/intake-task` write. `ClaudeIntakeAgent` is the default concrete
adapter: it shells out to Claude in the target repo, uses stream-json output,
requires structured draft envelopes, resumes sessions by id, runs draft/revise
under `--permission-mode plan`, and uses the write-permitted mode only for
confirm. Bad CLI exits or bad envelopes raise so the manager records failed
intakes.

## Run Manager

Input: repo id, task id, start flags.

Output: `RunRecord` from `start_run`, `RunStatus` from list/get operations,
read-only `(run_dir, RunRecord)` records from `load_records`, and idempotent
stop-request results.

Behavior: implemented subprocess lifecycle, per-repo serialization, and history
event ownership.
`RunManager` receives a `ManagedRepo` plus task id, rejects launches when the
repo already has a live run, when task inventory is missing/unlisted or reports
`missing`/`unknown`/unrecognized statuses, when a `blocked` task is not
explicitly acknowledged, or when readiness fails. A successful launch creates a
fresh `<data_dir>/runs/<run_id>/` dir with `control/`, starts the target repo's
deployed orchestrator through `/usr/bin/caffeinate -i` with `--control-dir`,
an explicit subprocess `PATH` for user-installed agent CLIs, persists
`run.json` with launch-time task status and the internal `spawn_out` path,
appends `run_started`, and records stdout/stderr in `spawn.out`.
`list_runs` and `get_run` refresh
live/end state and support reattach from disk by pid probe; observed ends append
`run_ended`. `stop_run` delegates to `control_dir.request_stop`, appends
`stop_requested` only on the first idempotent write, and refuses ended or
broken runs so a post-mortem flag cannot rewrite end classification.
`audit_answer` appends responder/question metadata plus optional answer `mode`
after a control-dir answer write, without copying answer text. `recent_runs`
returns newest-first `RunHistoryEntry` values from run records plus
`events.jsonl`.

## Control Dir Store

Input: run dir.

Output: pending question records and answer/stop write operations.

Behavior: file-channel only. Missing control dirs list no questions.
Question filenames supply the authoritative sequence. Malformed question JSON is
reported on the returned `Question` instead of hiding the file. A question is
answered only when a same-seq answer file is a JSON object with a non-empty
string `answer`; the stripped valid answer is exposed as `Question.answer`.
Malformed answers keep the question pending and can be repaired with
`write_answer`. Valid answer files are never overwritten. `request_stop` writes
an empty `stop.flag` and requires the run dir to exist; `stop_requested` and
`stop_answered` are read helpers for run classification.
`normalize_answer(text, mode=..., kind=...)` mirrors the orchestrator's
discussion-marker and strict plan-gate confirm parsing for explicit dashboard
button intent. It prepends/refuses discussion markers or plan-gate confirm words
so mode and parser outcome cannot silently diverge; omitted API `mode` skips
this helper for legacy verbatim writes.

## Log Store

Input: run record.

Output: `LogSnapshot` for bounded recent reads and async SSE frames for live
tailing.

Behavior: file access is scoped to `RunRecord.log_dir` and `RunRecord.task_id`.
Log files are located by the deployed orchestrator's
`<utc-ts>-<task-id>.log` filename convention, with `floor(started_ts)` as the
lower bound. Recent reads cap line and byte counts and return absent snapshots
for missing/rotated logs. Startup diagnostics read the persisted `spawn_out`
path directly, returning absent snapshots for missing, empty, or blank-only
captures. Live tails emit only complete newline-terminated lines, use byte
offsets to avoid duplicate initial/follow output, and terminate gracefully when
the run ends or the located file disappears.

## History Store

Input: run dir and `RunEvent` values.

Output: append-only `events.jsonl` plus `RunEvent` readback.

Behavior: `append_event` writes one JSON object per line. `read_events` returns
an empty tuple for missing history and `history_error` events for corrupt or
unreadable history lines. History is metadata-only; answer text remains in the
control-dir answer file, not in `events.jsonl`. `answer_recorded` events may
carry `mode` (`answer`, `discuss`, or `null` for legacy omitted-mode callers).

## Notifier

Input: `Notification` event with title, body, and optional group.

Output: best-effort delivery through a configured backend.

Behavior: `build_notifier(config)` returns `BarkNotifier` when
`Config.bark_url` is set, otherwise `None`. Bark sends JSON to the configured
full endpoint, which includes the device key and must not be logged. The run
manager owns trigger timing and isolation for its domain: `RunManager` fires
terminal notifications at the observed live-to-ended transition and
pending-question notifications once per observed `(run_id, seq)`;
`IntakeManager` fires intake-ready notifications after successful
draft/revision settlement. Backend exceptions are swallowed without changing
run records, intake records, history, or control-dir files. `orch-hub notify
test` delivers a probe directly through the same notifier factory so delivery
failure is visible to the CLI exit code while remaining outside manager state.

## Launchd Service Template

Input: environment variables in
`deploy/launchd/com.qinglin89.orch-hub.plist`.

Output: resident `orch-hub-api` process managed by launchd, with stdout/stderr
logs under `~/Library/Logs/orch-hub/`.

Behavior: user-agent template sets all current `ORCH_HUB_*` variables
including optional `ORCH_HUB_BARK_URL`, plus an explicit `PATH`, starts at
login, and restarts on exit. Required placeholders must resolve before writing
or bootstrapping; unknown optional placeholders are blanked for forward
compatibility with newer templates. Install/start/stop/reload/log commands live
in `docs/launchd.md`.

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
