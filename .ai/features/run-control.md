---
last-updated: 2026-07-13
verified-against: 8a93e1feb98b3c5e80d9fe1cbc63d244f7f68756
---

# Features: Run Control

## Run Lifecycle

Status: implemented

Description: Start, stop, list, get, and reattach orchestrator subprocesses.
Each run attaches to one repo and one task. Same-repo live runs are serialized;
cross-repo runs may overlap.

Modules: runs, config, control_dir, models, readiness, task_store.

APIs: internal `RunManager`; `POST /api/runs`, `GET /api/runs`,
`GET /api/runs/{run_id}`, `GET /api/runs/{run_id}/sessions`,
`POST /api/runs/{run_id}/stop`.

Implemented behavior:

- `RunManager.start_run(repo, task_id, extra_args=(), acknowledge_blocked=False)`
  gates launches fail-closed: same-repo live run, task inventory/status from
  `list_repo_tasks`, then readiness from `check_readiness`.
- Task launch validation uses frontmatter-authoritative `TaskSummary.status`;
  unlisted tasks, `missing`/`unknown` task-store sentinels, and unrecognized
  statuses are rejected. `blocked` tasks require explicit acknowledgement.
- Run dirs live outside target repos under `<data_dir>/runs/<run_id>/`, with
  `run.json`, a fresh `control/` dir, and `spawn.out`. `run_id` combines UTC
  timestamp, repo slug, task slug, and a collision suffix when needed.
- Launch command is `/usr/bin/caffeinate -i <repo>/.cursor/orchestrator/.venv/bin/python
  <repo>/.cursor/orchestrator/orchestrator.py <task-id> --control-dir
  <run>/control [extra args]`, with `cwd=<repo>`, stdin from devnull,
  stdout/stderr redirected to `spawn.out`, `start_new_session=True`, and an
  explicit `PATH` that includes common user-installed agent CLI directories
  before the inherited and baseline macOS paths.
- `run.json` is the file-backed source of truth and is atomically rewritten;
  it persists the internal `spawn_out` path so reattached runs and pre-field
  records can still surface startup diagnostics. In-memory `Popen` handles only
  capture exit codes while the hub is the parent process.
- Reattach scans run dirs and probes pid liveness. If a run ended while the hub
  was down, `ended_ts` is persisted with `exit_code=None`.
- Live status is `active` or `waiting` based on pending control-dir questions
  and carries `stop_requested=true` while the live run's `stop.flag` exists.
  End status is classified as `completed`, `stopped`, `error`, or `ended` by
  task completion/archival evidence, stop evidence, and then exit code; terminal
  run payloads keep `stop_requested=false`.
- `stop_run` writes `stop.flag` through `control_dir.request_stop` and refuses
  unknown, broken, or already-ended runs.
- HTTP run routes resolve repos through registry discovery, return serialized
  `RunStatus` payloads including the live-only `stop_requested` overlay, expose
  start/list/get/stop behavior, and map launch/run manager errors to 4xx JSON
  responses (`RepoBusyError` and ended/blocked states use 409; unknown runs use
  404).
- Per-session context usage is log-derived: live runs parse the current
  orchestrator log on demand, terminal runs serve the `sessions.json` snapshot
  persisted at the observed live-to-ended transition. Parsing failures are
  isolated from run state.

## Control-dir File Channel

Status: implemented

Description: Hub-side file adapter for the deployed orchestrator
`--control-dir` contract. It discovers human-question files, classifies pending
and answered state, writes answer files, and writes stop flags without adding
web/API logic to the adapter.

Modules: control_dir, models.

APIs: internal `list_questions`, `pending_questions`, `write_answer`,
`request_stop`, `DuplicateAnswerError`, and `Question`.

Implemented behavior:

- `list_questions(control_dir)` reads `NNN-question.json` files ordered by
  filename sequence; missing dirs return no questions; malformed question files
  are returned with `Question.error` instead of being skipped.
- Filename sequence is authoritative over payload `seq`; unknown `kind` values
  are surfaced for forward compatibility.
- `pending_questions(control_dir)` treats a question as pending until a same-seq
  answer file passes the orchestrator validity rule: JSON object with a
  non-empty string `answer`. Malformed answers keep the question pending and
  expose `Question.answer_error` for repair.
- `write_answer(question, answer, responder="orch-hub")` strips the answer,
  rejects empty text, mirrors the observed question filename, writes full audit
  fields atomically with a same-dir temp file plus rename, refuses to overwrite
  an existing valid answer, and repairs malformed answer files by atomic
  rewrite.
- `request_stop(control_dir)` writes an idempotent empty `stop.flag` and raises
  `FileNotFoundError` if the run dir does not exist.
- Tests cover discovery, parsing, pending/answered state, stale and malformed
  files, duplicate answer refusal, repair rewrite, stop flag writes, and
  `Question.to_dict()` path exclusion.

## Human Question Relay

Status: implemented

Description: Detect pending control-dir question files, list them remotely with
run/repo context, notify the user, accept answers, write answer files, and
audit the responder.

Modules: control_dir, runs, api, notifications, models.

APIs: `GET /api/escalations`,
`POST /api/runs/{run_id}/escalations/{seq}/answer`.

Implemented behavior:

- `GET /api/escalations` scans live runs only and returns pending
  control-dir questions with `run_id`, `repo_name`, `task_id`, and serialized
  `Question`.
- The answer route identifies questions by `(run_id, seq)` and writes through
  `control_dir.write_answer`, preserving duplicate-answer protection, malformed
  answer repair, empty-answer rejection, and responder audit fields.
- The dashboard task detail view renders inline answer boxes for waiting runs,
  submits with responder `"dashboard"`, and re-renders after the answer file is
  written.
- When a notifier is configured, `RunManager._status` fires a Bark `needs
  input` notification the first time its live-run `pending_questions` scan sees
  each `(run_id, seq)`. Dedup is in-memory, so rescans do not duplicate pushes
  in one hub process and a restart re-notifies a still-open question once.
  `plan-gate` questions use compact push text (`plan ready for review`) instead
  of the full plan banner; other question prompts are still included but bounded
  so oversized banners do not break delivery. The full prompt remains available
  through the dashboard/control-dir/log surfaces.

## Log Viewing

Status: implemented

Description: Read and live-tail deployed orchestrator logs for known runs while keeping filesystem access scoped to persisted run records.

Modules: logs, runs, api.

APIs: `GET /api/runs/{run_id}/logs`,
`GET /api/runs/{run_id}/logs/stream`.

Implemented behavior:

- Run records carry `log_dir`, `task_id`, and `started_ts`, not the exact log
  filename. `logs.locate_log(record)` matches
  `<utc-ts>-<task_id>.log` under the recorded log dir and returns the earliest
  stamp at or after `floor(started_ts)`; broken records with no usable
  `started_ts` fall back to the newest matching file.
- `RunManager.start_run` stamps `started_ts` just before spawning, so the
  child orchestrator's later second-precision log stamp is at or after the
  floored start. This avoids hiding logs when launch crosses a second boundary
  while still rejecting fast re-run logs from earlier runs. Same-wall-clock-
  second re-runs of one task remain indistinguishable by the orchestrator's
  second-precision filenames.
- `GET /api/runs/{run_id}/logs` returns a bounded recent tail: default 200
  lines, hard-capped to 10k lines and a 1 MiB byte window. Missing, rotated, or
  unreadable logs return `present: false` with `detail`, not an HTTP error.
- When no orchestrator log is located, `GET /api/runs/{run_id}/logs` may add a
  nested `diagnostic` sourced from a bounded `spawn.out` tail. Empty or blank
  captures stay `diagnostic: null`, and top-level `present`/`lines` remain
  strictly orchestrator-log semantics.
- `GET /api/runs/{run_id}/logs/stream` returns `text/event-stream`: a bounded
  initial tail, one `data:` event per completed appended line, heartbeat
  comments while idle, and terminal `event: end` on run end or log rotation.
  The tailer uses byte offsets and a raw-bytes pending buffer so a UTF-8
  character split across reads is not corrupted.
- Browser clients must stream with `fetch` plus a `ReadableStream` reader under
  the standard `Authorization: Bearer ...` header. The hub does not accept log
  stream credentials in query strings.

## Run History and Audit

Status: implemented

Description: Persist run lifecycle, stop request, and human answer-audit
metadata for later inspection without copying human answer text into history.

Modules: history, runs, api, models, control_dir.

APIs: `GET /api/history/runs`, `GET /api/runs/{run_id}/task`,
`GET /api/runs/{run_id}/sessions`.

Implemented behavior:

- Per-run history is append-only JSONL at
  `<data_dir>/runs/<run_id>/events.jsonl`, colocated with `run.json`,
  `control/`, and `spawn.out`.
- `RunRecord.task_status` stores the launch-time task-store/frontmatter status
  from `TaskSummary`, so history records carry task context without re-parsing
  task files.
- `RunManager` appends `run_started`, `run_ended`, `stop_requested`, and
  `answer_recorded` events. Stop events are written only for the first
  idempotent stop request; end events include the observed exit code when the
  hub has it.
- Answer audit events record responder and question metadata (`seq`, question
  timestamp/kind/banner/message) but deliberately exclude the answer text.
- `GET /api/history/runs` returns recent runs newest-first with the normal
  serialized run status plus embedded persisted events, per-run persisted
  session snapshots (`sessions`, `context_budget`), and the task's typed
  `session_est` (`null` when unknown); `limit` defaults to 50 and is clamped to
  `0..200`.
- `GET /api/runs/{run_id}/task` reads the task file through the persisted run
  identity: active `.ai-tasks/<task_id>.md` first, then
  `.ai-tasks/archive/<task_id>.md`. It returns `source:
  "active"|"archived"|null`, `present`, and `content`; archived tasks remain
  reachable only through a run record, not through the active task inventory.
- `GET /api/runs/{run_id}/sessions` returns `{run_id, sessions,
  context_budget}`. Unknown runs are 404; live runs parse the current log;
  ended runs prefer the persisted snapshot and still return 200 with empty or
  partial data when logs are absent or malformed.
- History readback treats missing `events.jsonl` as empty history and surfaces
  unreadable or malformed event lines as `history_error` events.
