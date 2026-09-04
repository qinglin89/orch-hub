---
last-updated: 2026-09-04
verified-against: 370050db7d568c3ba44524f1358937c39c3b58de
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
  Custom `agent`/`model`/`effort` fields, and orthogonal flags); the server maps
  it through `build_launch_args`. Every Custom cc-codex role requires the
  complete tuple, while Cursor rejects role-agent fields. Legacy structured
  `mode` remains a mutually exclusive compatibility shape that applies one
  selection to both roles and rejects the newer `review_agent`. Raw `extra_args`
  callers remain supported verbatim, but `launch` plus non-empty `extra_args`
  is 400.
- `acknowledge_blocked` is hub-only and never joins the strictly parsed
  orchestrator argv. Unknown request keys remain ignored for compatibility with
  cached clients that still send the retired `auto_semantic_eval` field.
- Shape: returns the new run's serialized `RunStatus` with HTTP 201.
- Admission error: a non-terminal remote intake returns HTTP 409 before any
  run record or subprocess exists:
  `{"detail", "conflict": "intake_in_flight", "intake_id"}`. This is not a
  readiness failure and carries no `reasons`.

## `GET /api/runs`

- Purpose: list known runs with live/waiting/end-state overlays.
- Shape: `{"runs": [RunStatus.to_dict(), ...]}`. Live run entries include
  `stop_requested: true` after `POST /stop` writes `stop.flag`; terminal entries
  keep `stop_requested: false` and express honored stops through `state/detail`.

## `GET /api/history/runs`

- Purpose: expose all persisted runs through stable newest-first pages, plus a
  separate bounded newest-runs summary for Overview.
- Source: paged mode delegates to `RunManager.history_page(cursor=...)`;
  summary mode delegates to `RunManager.recent_runs(limit=...)`. Both refresh
  and read only the returned window/summary, including each run's
  `events.jsonl`.
- Paged query/shape: no `limit` means fixed 50-run windows. Omit `cursor` for
  the newest window; thereafter echo opaque `prev_cursor`/`next_cursor` values
  (their direction + run-id anchor is server-owned). The payload has `runs`,
  `page`, `page_size`, `total_runs`, `total_pages`, `has_prev`, `has_next`,
  `prev_cursor`, and `next_cursor`. Anchored directions keep adjacent windows
  disjoint and complete across insert/prune races.
- Page metadata: `page`/`total_pages` count the current cursor traversal's
  windows, so a ragged walk can be longer than `ceil(total_runs / page_size)`
  without rendering `page > total_pages`. A non-empty window is always within
  its count; an empty past-the-end window is HTTP 200 with
  `page == total_pages + 1`, no Next, and a Previous back to real runs. Empty
  history is page 1 of 0.
- Summary query/shape: `?limit=N` returns only `runs` plus evaluation sidecar
  fields; `N` is clamped to `0..200`. Summary responses have no totals or
  cursors. Sending both `limit` and `cursor` is HTTP 400.
- Every run entry embeds the normal `RunStatus` fields, including
  `stop_requested`, plus `events`, persisted session snapshot fields
  (`sessions`, `context_budget`), `session_est`
  (`{"current","total"}` or `null`), and the catalog-free run snapshot
  `effective_config` (`null` when no startup snapshot exists). Both top-level
  contracts also include
  `evaluation_enabled` plus `evaluation_states`, one entry per distinct
  returned task group: `{repo_name, repo_path, task_id, latest_evaluation_id,
  level}` where `level` is `none`, `l1`, or `l1_l2`.
- Evaluation state: derived by deduplicating the returned runs by
  `(repo_path, task_id)` and structurally listing each task's newest persisted
  evaluation once. Empty/evidence-only folds to `none`, static metrics gives
  `l1`, and static metrics plus semantic report gives `l1_l2`. Disabled
  evaluation mode returns `evaluation_enabled:false` and no state reads; a
  per-task state read failure omits only that marker and never hides run
  history.

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
  back to log parsing for pre-field runs. Both read paths omit `options`, so a
  pre-drop file that still stores the catalog remains readable with the same
  served shape as a new snapshot.
- Shape: `{"run_id", "effective_config"}` where `effective_config` is the
  orchestrator startup object minus its selection-independent `options` catalog,
  or `null` when absent/unparsed. Unknown runs return 404.

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
