---
last-updated: 2026-07-30
verified-against: d47c1abaa9ffe73ee11d53e7835aa02c8437f14d
---

# Features: Run Observability

Log viewing and run history/audit. Run lifecycle, control-dir channel, and
question relay: `run-control.md`.

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
`GET /api/runs/{run_id}/sessions`,
`GET /api/runs/{run_id}/effective-config`.

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
- Answer audit events record responder, optional answer `mode`, and question
  metadata (`seq`, question timestamp/kind/banner/message) but deliberately
  exclude the answer text.
- `GET /api/history/runs` returns recent runs newest-first with the normal
  serialized run status plus embedded persisted events, per-run persisted
  session snapshots (`sessions`, `context_budget`), and the task's typed
  `session_est` (`null` when unknown), the persisted run `effective_config`
  snapshot (`null` when absent), and batched optional task-evaluation state.
  Evaluation state is returned once per distinct `(repo_path, task_id)` group as
  `none` / `l1` / `l1_l2`, skips all reads when evaluation is disabled, and
  fails soft per task so history still renders if an evaluation sidecar read
  fails; `limit` defaults to 50 and is clamped to `0..200`.
- `GET /api/runs/{run_id}/task` reads the task file through the persisted run
  identity: active `.ai-tasks/<task_id>.md` first, then
  `.ai-tasks/archive/<task_id>.md`. It returns `source:
  "active"|"archived"|null`, `present`, and `content`; archived tasks remain
  reachable only through a run record, not through the active task inventory.
- `GET /api/runs/{run_id}/sessions` returns `{run_id, sessions,
  context_budget}`. Unknown runs are 404; live runs parse the current log;
  ended runs prefer the persisted snapshot and still return 200 with empty or
  partial data when logs are absent or malformed.
- `GET /api/runs/{run_id}/effective-config` returns `{run_id,
  effective_config}`. Live runs parse the current log, ended runs prefer the
  persisted snapshot and fall back to log parsing for pre-field runs; unknown
  runs are 404 and absent/unparsed snapshots return `null`.
- History readback treats missing `events.jsonl` as empty history and surfaces
  unreadable or malformed event lines as `history_error` events.
