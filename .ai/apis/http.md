---
last-updated: 2026-07-23
verified-against: 5f11bbf080b3c83dc1e7110a44f626759851e235
---

# APIs: HTTP

## `GET /api/health`

- Purpose: minimal public service liveness.
- Shape: `{"status": "ok"}`.
- Auth: public by design; it must not disclose deployment configuration.

## `GET /`, `GET /app.js`, `GET /styles.css`

- Purpose: serve the public dashboard shell assets.
- Auth: public by design because browser navigation cannot attach an
  `Authorization` header. These assets contain no secrets; every dashboard data
  request still uses protected HTTP APIs with `Authorization: Bearer <token>`.
- Cache: each response sends `Cache-Control: no-store` so mobile browsers fetch
  the current shell after service updates rather than serving stale JS/CSS from
  heuristic cache.
- Dashboard behavior: `#/overview` is the default authenticated landing route
  and derives its metrics, attention list, live activity, and recent outcomes
  from the existing protected repo/task/run/escalation/history APIs; it adds no
  Overview-specific HTTP API. Non-401 source failures degrade only the affected
  panel, 401 re-enters the login gate, and route teardown cancels its chained
  refresh before stale loads can repaint or keep polling.

## `GET /api/repos`

- Purpose: list managed repos with readiness and display metadata.
- Source: same functions as CLI `repos --json`.
- Shape: exactly the same top-level object as `python -m orch_hub.cli repos --json`, including best-effort `commit` and display-only `housekeeping_pending`.

## `GET /api/repos/{repo_id}/commits`

- Purpose: list one repo's recent commits newest-first for the dashboard detail
  view.
- Source: resolves `repo_id` through registry discovery, then delegates to
  `git_meta.read_commits`.
- Shape: `{"repo": "quantx", "available": true, "commits": [CommitInfo...]}`.
  Each commit is `{hash, short_hash, committed_ts, subject}`.
- Query: `limit` defaults to 10 and is clamped by `git_meta` to `1..50`.
- Errors: unknown repos return HTTP 404; git lookup failure returns HTTP 200
  with `available: false` and `commits: []` so repo/task content stays visible.

## `GET /api/repos/{repo_id}/tasks`

- Purpose: list tasks for one repo, where `repo_id` is the managed-repo name.
- Shape: `tasks_payload([inventory])`, the same `{"repos": [...]}` envelope as
  all-repo task inventory with a single repo entry. Task ids are normalized
  bare ids even when the source index uses Markdown-link Task cells.

## `GET /api/repos/{repo_id}/tasks/{task_id}`

- Purpose: fetch one listed task's frontmatter-authoritative inventory fields
  plus raw markdown body for the dashboard task-detail view.
- Source: resolves `repo_id` through registry discovery, gets status/summary/
  blockers through `list_repo_tasks`, gates `task_id` on normalized index
  membership, then reads `.ai-tasks/<task_id>.md` through `read_task_content`.
- Shape: `TaskSummary.to_dict()` plus `{"present": bool, "content": str|null}`.
  Listed-but-absent task files return HTTP 200 with `present: false` and
  `content: null`; unknown repos or unlisted task ids return HTTP 404.

## `GET /api/tasks`

- Purpose: list tasks across discovered repos.
- Shape: `tasks_payload(list_tasks(repos))`, preserving `index_present`,
  ordered tasks, frontmatter-authoritative status/blockers, and `missing` /
  `unknown` sentinel statuses.

## `GET /api/repos/{repo_id}/orchestrator-config`

- Purpose: query the target repo's deployed orchestrator for the Start dialog's
  resolved launch configuration via `orchestrator.py --print-config`.
- Source: resolves `repo_id` through registry discovery, then runs the
  repo-local orchestrator with `cwd=<repo>` and optional `backend`/`profile`
  query params. It does not read `orchestrator.toml` directly and is not
  readiness-gated.
- Shape: `EffectiveConfig.to_dict()` with schema/revisions/profile list,
  backend, `dev`/`review` role agent/model/effort, budgets, sandbox,
  provenance `sources`, and preview launch fields.
- Errors: unknown repo returns 404; unrecognized backend returns 400; missing
  old deployments, nonzero `--print-config`, malformed output, or unsupported
  schema return 502 with the orchestrator/parser reason so launch fails closed.

## `GET /api/evaluation`

- Purpose: report whether the optional task-evaluation subsystem is enabled.
- Shape: `{"enabled": bool}`.
- Auth: protected like every non-health data route.
- Disabled behavior: this is the only evaluation route that returns 200 while
  disabled; every other evaluation route fails closed as 404.

## `GET /api/evaluation/doctor`

- Purpose: report Layer 2 semantic-evaluator readiness without a model call.
- Shape: `{"enabled": true, "semantic": SemanticDoctor}` where `semantic` is
  `{backend, model, schema_version, available, detail}`.
- Disabled behavior: enabled-gated like action routes; disabled returns 404.
- Boundary: used by dashboard/CLI smoke checks only; never gates Start/Stop,
  run history, logs, or control-dir answers.

## `GET /api/repos/{repo_id}/evaluations/{task_id}`

- Purpose: list persisted evaluations for an exact active or archived task id
  plus the task target state.
- Source: resolves `repo_id` through registry discovery, then uses
  `EvaluationService.list_evaluations` and active-then-archive target
  resolution. It is deliberately not gated by the active task index.
- Shape: `{"repo","task_id","enabled":true,"target","latest_evaluation_id","evaluations"}`.
  `target` is `{present: bool|null, source: "active"|"archived"|null}`.
  `evaluations` is newest-first summaries
  `{evaluation_id, has_evidence, has_static_metrics, has_semantic_report,
  evaluator}`. `evaluator` is best-effort `{backend, model, schema_version}`
  read from a present semantic report for history-picker comparison, or `null`
  for non-semantic/degraded entries; corrupt semantic report JSON never 500s the
  list route.
- Errors: unknown repo returns 404; unsafe task id returns 400; disabled returns
  404. A never-evaluated task returns 200 with an empty evaluation list.

## `GET /api/repos/{repo_id}/evaluations/{task_id}/{evaluation_id}`

- Purpose: read one persisted evaluation's artifacts for dashboard rendering
  and local inspection.
- Shape: `{"evaluation_id","evidence","static_metrics","semantic_report","report_md"}`.
  JSON artifacts are parsed objects and `report_md` is raw text; each is `null`
  when that artifact is absent from the evaluation dir.
- Errors: unknown repo or unknown evaluation id returns 404; unsafe task id
  returns 400; disabled returns 404; corrupt artifact JSON surfaces as a 500.

## `POST /api/repos/{repo_id}/evaluations/{task_id}/evidence`

- Purpose: generate a fresh bounded evidence pack for an exact active or
  archived task id.
- Source: delegates to `EvaluationService.generate_evidence`, writing only
  hub-owned `<data_dir>/evaluations/.../evidence.json`.
- Shape: HTTP 201 `{"evaluation_id","evidence"}`.
- Errors: unknown repo returns 404; unsafe task id returns 400; disabled returns
  404.

## `POST /api/repos/{repo_id}/evaluations/{task_id}/semantic`

- Purpose: generate a fresh Layer 2 semantic evaluation: evidence plus
  `static_metrics.json`, `semantic_report.json`, and `report.md` in one
  hub-owned evaluation dir.
- Source: delegates to `SemanticEvaluator.evaluate`; the default Claude CLI
  backend runs from an isolated empty cwd with tools disabled, safe mode, no
  session persistence, and plan-mode writes blocked, so the model is bounded to
  the persisted prompt/evidence and cannot read the target repo.
- Shape: HTTP 201 `{"evaluation_id","semantic_report"}`. The paired evidence,
  static metrics, and Markdown report are read through the evaluation detail
  route.
- Errors: unknown repo returns 404; unsafe task id returns 400; disabled returns
  404; backend/model failures or non-conforming semantic output return 502 and
  leave no persisted artifact.

## `POST /api/repos/{repo_id}/evaluations/{task_id}/metrics`

- Purpose: generate a fresh deterministic Layer 1 evaluation: evidence plus
  `static_metrics.json` in one hub-owned evaluation dir.
- Source: delegates to `StaticEvaluator.generate_metrics`; no semantic model
  call and no target task/run mutation.
- Shape: HTTP 201 `{"evaluation_id","static_metrics"}`. The paired evidence is
  read through the evaluation detail route.
- Errors: unknown repo returns 404; unsafe task id returns 400; disabled returns
  404.

## `POST /api/runs`

- Purpose: start an orchestrator run for `{repo_id, task_id, launch|extra_args,
  acknowledge_blocked}`.
- Source: resolves `repo_id` through registry discovery, then delegates launch
  gates and subprocess start to the app's single `RunManager`.
- Dashboard caller: the task-detail Start dialog sends structured `launch`
  selection (`mode`, optional `backend`, Custom role overrides, and orthogonal
  `plan_gate`); the server maps it through `build_launch_args`. Legacy
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

## `POST /api/intakes`

- Purpose: start a remote `/intake-task` drafting session for `{repo_id,
  request}`.
- Source: resolves `repo_id` through registry discovery, then delegates gates
  and draft creation to the app's single `IntakeManager`.
- Shape: returns the serialized transient `IntakeRecord` with HTTP 201
  (`status: drafting`, no draft yet) immediately after the record is persisted.
  Clients poll `GET /api/intakes/{intake_id}` for the settled
  `awaiting_review` draft or `failed` outcome.
- Errors: unknown repo returns 404; empty request returns 400; repo already has
  an in-flight intake or readiness fails returns 409, with readiness `reasons`
  included.

## `GET /api/intakes`

- Purpose: list known intake records for dashboard resume and audit views.
- Shape: `{"intakes": [IntakeRecord.to_dict(), ...]}`.

## `GET /api/intakes/{intake_id}`

- Purpose: fetch one intake's current draft/status; this is the polling target
  while a mutating intake turn is transient.
- Shape: `IntakeRecord.to_dict()`, including the complete draft when present.
- Errors: unknown intakes return 404.

## `GET /api/intakes/{intake_id}/history`

- Purpose: fetch one intake record plus metadata-only lifecycle/revision audit
  events.
- Shape: the current `IntakeRecord.to_dict()` fields flattened at top level
  plus `"events": [IntakeEvent...]`.

## `POST /api/intakes/{intake_id}/revise`

- Purpose: send non-empty human feedback back to the same intake session and
  return a revised complete draft for review.
- Source: delegates to `IntakeManager.revise_intake`.
- Shape: serialized transient `IntakeRecord` (`status: revising`) returned
  immediately; poll `GET /api/intakes/{intake_id}` for the settled
  `awaiting_review` draft or `failed` outcome. Wrong lifecycle state returns
  409; empty feedback returns 400; a lost session can fail synchronously as a
  `failed` record.

## `POST /api/intakes/{intake_id}/confirm`

- Purpose: explicitly approve the current draft and let the intake session
  perform the protocol-defined task/index write.
- Source: delegates to `IntakeManager.confirm_intake`, which records
  `confirmed` only after the created task appears through task inventory with
  frontmatter status `pending` or `blocked`.
- Shape: serialized transient `IntakeRecord` (`status: confirming`) returned
  immediately; poll `GET /api/intakes/{intake_id}` for `confirmed` plus
  `created_task_id` or a `failed` outcome. A failed/unverified confirm is an
  auditable record with status `failed`, not a transport 5xx.

## `POST /api/intakes/{intake_id}/cancel`

- Purpose: cancel a draft/review intake without writing target repo task files.
- Shape: serialized `IntakeRecord` with status `cancelled`; legal from
  `drafting`, `awaiting_review`, and `revising`. Wrong lifecycle state,
  including `confirming`, returns 409.

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

## Current HTTP Behavior


- `create_app(config=None, *, run_manager=None, intake_manager=None,
  evaluation_service=None, static_evaluator=None, semantic_evaluator=None)`
  captures `Config.from_env()` by default, is testable with explicit/injected
  services, and holds one `RunManager` for run, run-task, escalation, log, and
  background-monitor scans plus one `IntakeManager` for remote intake routes.
  Default managers are built with `build_notifier(config)`: run-ended/
  pending-question and intake-ready notifications share the configured Bark
  path, and `None` keeps delivery disabled. The default intake backend is
  `ClaudeIntakeAgent`; the default semantic backend is `ClaudeSemanticBackend`
  constructed at app startup but only invoked by semantic generation or probed
  by the doctor.
- The app constructs a `RunMonitor` over that manager, exposes it as
  `app.state.run_monitor`, starts it only during FastAPI lifespan startup, and
  stops it during shutdown; app construction alone starts no worker thread.
- `Config` is loaded from environment by default. Current variables are
  `ORCH_HUB_AI_NATIVE_DEPLOYMENT`, `ORCH_HUB_TOKEN`, `ORCH_HUB_HOST`,
  `ORCH_HUB_PORT`, `ORCH_HUB_DATA_DIR`, `ORCH_HUB_BARK_URL`,
  `ORCH_HUB_EVAL_ENABLED`, `ORCH_HUB_EVAL_MODEL`, and
  `ORCH_HUB_EVAL_RETENTION`; surrounding whitespace is stripped and
  empty/whitespace-only values count as unset.
- Module-level `app` is the `uvicorn orch_hub.api:app` target.
- `run(host=None, port=None)` and console script `orch-hub-api` serve the app
  on `ORCH_HUB_HOST` / `ORCH_HUB_PORT`, defaulting to `127.0.0.1:8765`;
  explicit function arguments override the environment.
- Default-deny pure-ASGI middleware protects every path outside `/api/health`
  and the enumerated dashboard shell paths (`/`, `/app.js`, `/styles.css`),
  including `/docs`, `/openapi.json`, unknown paths, and future data routes.
  Pure ASGI is used instead of `BaseHTTPMiddleware` so streaming responses are
  not buffered and disconnects propagate.
- The enumerated dashboard shell paths are public but uncached:
  `Cache-Control: no-store` is attached to each shell `FileResponse`.
- Missing or malformed credentials return HTTP 401 with
  `WWW-Authenticate: Bearer` and `{"detail": "missing bearer token"}`; wrong
  credentials return HTTP 401 with `{"detail": "invalid bearer token"}`.
- When `ORCH_HUB_TOKEN` is unset, protected routes fail closed with HTTP 503
  instead of serving unauthenticated; `/api/health` still works.
- Token comparison uses `secrets.compare_digest`; auth error responses are
  static and never echo presented credentials.
- `RegistryError` returns HTTP 500 with `{"detail": ...}`.
- Unknown `repo_id` on the per-repo task endpoint returns HTTP 404.
- Run-manager/control-dir errors map to client-actionable statuses:
  `UnknownRunError` → 404; `RepoBusyError`, `RunEndedError`,
  `BlockedTaskError`, `LaunchBlockedError`, and `DuplicateAnswerError` → 409;
  structured launch `LaunchError`, empty answer, or answer-mode guard
  `ValueError` → 400; orchestrator-config query failures → 502.
  `LaunchBlockedError` includes `reasons`; `BlockedTaskError` includes
  `acknowledge_required: true`.
- Intake-manager errors map to client-actionable statuses: `UnknownIntakeError`
  → 404; `IntakeBusyError`, `IntakeNotReadyError`, and `IntakeStateError` →
  409; empty request/feedback `ValueError` → 400. Agent failures are lifecycle
  outcomes in the intake record. Mutating intake routes persist and return
  transient records (`drafting`/`revising`/`confirming`) before the slow agent
  turn finishes; clients poll `GET /api/intakes/{id}` for the resting result.
- The dashboard repo/task status layout consumes these APIs directly: repo
  cards use `/api/repos` for readiness/latest commit/housekeeping warning
  state, `/api/runs` for the tri-state run signal, and `/api/tasks` for active
  task counts; repo detail uses `/api/repos` for the housekeeping warning,
  uses `/api/repos/{repo_id}/commits` for recent commits, and treats
  `available:false` as a graceful unavailable state.
- The dashboard task-detail run controls consume these existing run APIs
  directly: Start first queries `GET /api/repos/{repo_id}/orchestrator-config`
  and then sends `{repo_id, task_id, acknowledge_blocked, launch}` to
  `POST /api/runs`; Stop sends `POST /api/runs/{run_id}/stop`, and frontend
  error rendering preserves structured 409 fields instead of flattening every
  refusal to `detail`.
