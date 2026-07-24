---
last-updated: 2026-07-24
verified-against: 3779a5d5ac7b2dd74f669990a25dc318e24f54f8
---

# APIs: HTTP — Runs, Logs, Escalations

Run lifecycle, history, session/effective-config context, log, and
human-question routes. Cross-cutting auth/app behavior: `http-core.md`.

## `POST /api/runs`

- Purpose: start an orchestrator run for `{repo_id, task_id, launch|extra_args,
  acknowledge_blocked}`.
- Source: resolves `repo_id` through registry discovery, then delegates launch
  gates and subprocess start to the app's single `RunManager`.
- Dashboard caller: the task-detail Start dialog sends structured `launch`
  selection (`dev_mode`, `review_mode`, optional shared `backend`, role-local
  Custom fields, and orthogonal flags); the server maps it through
  `build_launch_args`. Legacy structured `mode` remains a mutually exclusive
  compatibility shape that applies one selection to both roles. Raw
  `extra_args` callers remain supported verbatim, but `launch` plus non-empty
  `extra_args` is 400.
- Shape: returns the new run's serialized `RunStatus` with HTTP 201.

## `GET /api/runs`

- Purpose: list known runs with live/waiting/end-state overlays.
- Shape: `{"runs": [RunStatus.to_dict(), ...]}`. Live run entries include
  `stop_requested: true` after `POST /stop` writes `stop.flag`; terminal entries
  keep `stop_requested: false` and express honored stops through `state/detail`.

## `GET /api/history/runs`

- Purpose: list recent runs newest-first with computed run status and persisted
  audit/history events.
- Source: delegates to `RunManager.recent_runs(limit=...)`, which refreshes
  run state and reads each run's `events.jsonl`.
- Shape: `{"runs": [RunHistoryEntry.to_dict(), ...]}` where each entry embeds
  the normal `RunStatus` fields, including `stop_requested`, plus `events`,
  persisted session snapshot fields (`sessions`, `context_budget`), and
  `session_est` (`{"current","total"}` or `null` when unknown), and
  `effective_config` (`null` when the run has no persisted startup snapshot).
- Query: `limit` defaults to 50 and is clamped to `0..200`.

## `GET /api/runs/{run_id}`

- Purpose: fetch one run's serialized `RunStatus`.
- Shape note: same `RunStatus.to_dict()` shape as list entries, including the
  live-only `stop_requested` boolean.
- Errors: unknown run id returns HTTP 404.

## `GET /api/runs/{run_id}/task`

- Purpose: fetch the task file associated with a persisted run, even after
  close-out archives the task.
- Source: resolves `run_id` through `RunManager.get_run`, reconstructs the repo
  from `RunRecord.repo_path`/`task_id`, reads active
  `.ai-tasks/<task_id>.md` first, then falls back to
  `.ai-tasks/archive/<task_id>.md`.
- Shape: `{"run_id", "task_id", "source", "present", "content"}` where
  `source` is `"active"`, `"archived"`, or `null`. Unknown runs return HTTP
  404; missing active/archive files return HTTP 200 with `present: false`.
- Boundary: this route is run-scoped and non-index-gated so historical run task
  links survive archival; `/api/tasks` and `/api/repos/{repo_id}/tasks/{task_id}`
  remain active-only and provide no general archived-task listing.

## `GET /api/runs/{run_id}/sessions`

- Purpose: return per-protocol-session context usage for one run.
- Source: resolves `run_id` through `RunManager.read_sessions`. Live runs parse
  the current repo-side orchestrator log; ended runs serve the persisted
  `sessions.json` snapshot written next to `run.json`, falling back to log parse
  only for pre-field runs with no snapshot.
- Shape: `{"run_id", "sessions", "context_budget"}`. Each session has
  `role`, `kind` (`session` or `plan-gate`), `sid`, `turns`, `context_tokens`,
  `status_before`, `end_status`, `start_hms`, and `open`. Empty or partial data
  is HTTP 200; unknown runs return HTTP 404.

## `GET /api/runs/{run_id}/effective-config`

- Purpose: return the real run's authoritative `effective-config:` launch
  snapshot, authoritative if it differs from the Start-dialog preview.
- Source: resolves `run_id` through `RunManager.read_effective_config`. Live
  runs parse the located orchestrator log; ended runs prefer the persisted
  `<run>/effective_config.json` written at live-to-ended transition and fall
  back to log parsing for pre-field runs.
- Shape: `{"run_id", "effective_config"}` where `effective_config` is the raw
  orchestrator JSON object or `null` when absent/unparsed. Unknown runs return
  404.

## `POST /api/runs/{run_id}/stop`

- Purpose: request graceful stop by delegating to `RunManager.stop_run`, which
  writes `stop.flag` through `control_dir.request_stop`.
- Shape: `{"run_id": ..., "newly_requested": bool, "detail": ...}`; the detail
  surfaces the pending-question stop caveat.

## `GET /api/runs/{run_id}/logs`

- Purpose: retrieve a bounded recent tail of one run's orchestrator log.
- Source: resolves `run_id` through `RunManager.get_run`, then locates and
  reads the log from the run record's `log_dir`, `task_id`, and `started_ts`.
  Clients never supply a filesystem path.
- Shape: `{"run_id", "present", "lines", "truncated", "size", "detail",
  "diagnostic"}`. Missing, rotated, or unreadable logs are HTTP 200 with
  `present: false` and a detail string. `diagnostic` is `null` unless no
  orchestrator log is located and the run's bounded `spawn.out` tail is
  non-empty; when present it is a normal log snapshot plus
  `"source": "spawn.out"`. Unknown runs return HTTP 404.
- Query: `tail` controls returned line count, clamped by the logs module.

## `GET /api/runs/{run_id}/logs/stream`

- Purpose: stream one run's orchestrator log as Server-Sent Events.
- Source: resolves `run_id` through `RunManager.get_run`, emits a bounded
  initial tail, follows appended complete lines, re-probes run liveness each
  poll, and closes with `event: end` when the run ends or the log rotates away.
- Shape: `text/event-stream`; ordinary log lines are `data:` events, idle
  heartbeats are SSE comments, and stream termination is an `event: end`.
  Unknown runs return HTTP 404.
- Auth/client contract: standard `Authorization: Bearer <token>` only. Browser
  clients use `fetch` plus a `ReadableStream` reader because `EventSource`
  cannot set authorization headers; query-string tokens are not supported.

## `GET /api/escalations`

- Purpose: list pending control-dir questions across live runs, with run/repo
  context.
- Shape: `{"escalations": [{"run_id", "repo_name", "task_id", "question"}]}`.

## `POST /api/runs/{run_id}/escalations/{seq}/answer`

- Purpose: write an answer file for one run/question sequence, auditing the
  responder.
- Source: locates the question with `control_dir.list_questions`, writes via
  `control_dir.write_answer`, then records answer-audit metadata through
  `RunManager.audit_answer`; no orchestrator code is imported. Explicit
  `mode` values (`"answer"` or `"discuss"`) are normalized by
  `control_dir.normalize_answer` against the question `kind` before writing;
  omitted `mode` is the legacy path and writes the submitted answer verbatim.
- Body: `{"answer": str, "responder": str|null, "mode": "answer"|"discuss"|null}`.
  `mode` is optional. Explicit modes fail closed with HTTP 400 if button intent
  would not match the orchestrator parse: marker-prefixed non-plan answers must
  use `discuss`; plan-gate approval uses `answer` with empty text or a bare
  confirm word; plan-gate feedback uses `discuss` and rejects bare confirm
  words. `stop` passes through in every mode/kind.
- Shape: `{"run_id": ..., "seq": ..., "answered": true, "responder": ...,
  "answer": ...}`.
  The response echoes the normalized/written answer for the immediate caller,
  but persisted history deliberately excludes answer text while recording the
  answer `mode` metadata.
