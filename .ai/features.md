---
last-updated: 2026-07-08
verified-against: a79ba11724a2cccbd750f719d351b803c6fcda9d
---

# Features

## Feature Index

| Feature | Status | Key Modules | Key APIs |
|---|---|---|---|
| Repo discovery and readiness | implemented | config, registry, readiness, subprocess_utils, cli, api, models | CLI `repos`, CLI `repos --json`, `GET /api/repos` |
| Task inventory | implemented | task_store, api, models | `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`, `GET /api/repos/{repo_id}/tasks/{task_id}` |
| HTTP API foundation | implemented | api, config, registry, readiness, task_store, models | public shell routes, `GET /api/health`, repo/task/run/escalation/log/history APIs |
| Control-dir file channel | implemented | control_dir, models | internal file-channel functions |
| Run lifecycle | implemented | runs, api, config, control_dir, models, readiness, task_store | internal `RunManager`; `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{id}`, `POST /api/runs/{id}/stop` |
| Human question relay | API + dashboard answering implemented; notifications planned | control_dir, runs, api, notifications, web | `GET /api/escalations`, `POST /api/runs/{id}/escalations/{seq}/answer` |
| Log viewing | implemented | logs, runs, api | `GET /api/runs/{id}/logs`, `GET /api/runs/{id}/logs/stream` |
| Mobile dashboard | implemented | api, web | public shell routes plus protected repo/task/run/escalation/log/history APIs |
| Auth and private access | implemented | api, config | default-deny bearer middleware on HTTP routes |
| Notifications | planned | notifications, control_dir, runs | Bark delivery backend |
| Launchd service | implemented | config, api, docs, deploy | `orch-hub-api`, launchd plist/docs |
| Run history and audit | implemented | history, runs, api, models, control_dir | `GET /api/history/runs` |
| Remote intake-task | future | runs or agent adapter, task_store | future task creation endpoint |
| IM bot control | future | notifications or bot adapter, api | future bot callback interface |

## Feature Details

### Repo Discovery and Readiness

Status: implemented

Description: Load managed repos from ai-native-development and report whether each is ready for orchestration. This validates the registry and target-repo deployment assumptions before service/UI work.

Modules: config, registry, readiness, models, subprocess_utils, cli.

APIs: `python -m orch_hub.cli repos`, `python -m orch_hub.cli repos --json`,
`GET /api/repos`.

Implemented behavior:

- Discovery invokes `<ai-native-development>/aii-2 registry list --json` by absolute path, then falls back to `<ai-native-development>/.registry/repos.local.json` when the command is unavailable or exits non-zero.
- Registry data is a JSON array of `{name, path, manifest}`. Missing fallback file means no repos; invalid JSON or malformed entries raise an operational error.
- Readiness runs seven checks in stable order: repo path, manifest, deployed orchestrator script, orchestrator venv Python, `.ai-tasks/index.md`, clean `git status --porcelain`, and passing `aii-2 status <repo>`.
- Public readiness JSON serializes `ready` and only failed checks as `{name, message}` reasons; ready repos have `checks: []`.
- Tests cover registry parsing/fallbacks, every requested missing-piece readiness failure, dirty git state, `aii-2 status` failure, config, and CLI output.

### Task Inventory

Status: implemented

Description: Read `.ai-tasks/index.md` and task frontmatter in managed repos. Distinguish task-file statuses from hub run overlay statuses.

Modules: task_store, api, models.

APIs: `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`,
`GET /api/repos/{repo_id}/tasks/{task_id}`.

Implemented behavior:

- `list_repo_tasks(repo)` reads `.ai-tasks/index.md` for active task ids and human summaries, preserving index row order.
- The index Status column is not authoritative; each `.ai-tasks/<id>.md` frontmatter supplies task status and blockers.
- Missing `.ai-tasks/index.md` is represented as `index_present=False` with no task rows, distinct from a present-but-empty index.
- Missing listed task files use status `missing`; files with no parseable status use `unknown`.
- `TaskSummary`, `RepoTaskInventory`, and `tasks_payload` provide reusable data shapes for future CLI/API adapters.
- The single-task detail endpoint returns frontmatter-authoritative fields plus
  `present` and raw `content`; raw reads require index membership and resolved
  containment under `.ai-tasks`, so symlinked files outside the task dir are
  not disclosed.

### HTTP API Foundation

Status: implemented

Description: Expose current repo readiness and task inventory data through a
resident FastAPI app without duplicating core logic, protected by bearer-token
auth on every non-health path.

Modules: api, config, registry, readiness, task_store, models.

APIs: `GET /`, `GET /app.js`, `GET /styles.css`, `GET /api/health`,
repo/task/run/escalation/log/history data routes; `python -m orch_hub.api`;
`orch-hub-api`; `uvicorn orch_hub.api:app`.

Implemented behavior:

- `create_app(config=None)` builds a FastAPI app, capturing explicit config for
  tests or `Config.from_env()` by default.
- `GET /api/repos` returns the same shape as CLI `repos --json`, including
  readiness failure reasons.
- Task endpoints return the `tasks_payload` envelope, preserving
  `index_present`, ordered tasks, frontmatter-authoritative status/blockers,
  and task-store sentinel statuses.
- Malformed registry data maps to HTTP 500; unknown repo ids on per-repo task
  lookup map to HTTP 404.
- `GET /api/health` is public and minimal (`{"status": "ok"}`); it no longer
  exposes deployment paths.
- A default-deny middleware protects every path except `/api/health` and the
  three dashboard shell assets. Missing/malformed or invalid credentials return
  401; unset `ORCH_HUB_TOKEN` returns 503 on protected data routes.
- `run()` and `orch-hub-api` read `ORCH_HUB_HOST` / `ORCH_HUB_PORT` and bind
  to `127.0.0.1:8765` by default; any private exposure should remain
  Tailscale-only.

### Run Lifecycle

Status: implemented

Description: Start, stop, list, get, and reattach orchestrator subprocesses.
Each run attaches to one repo and one task. Same-repo live runs are serialized;
cross-repo runs may overlap.

Modules: runs, config, control_dir, models, readiness, task_store.

APIs: internal `RunManager`; `POST /api/runs`, `GET /api/runs`,
`GET /api/runs/{run_id}`, `POST /api/runs/{run_id}/stop`.

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
  stdout/stderr redirected to `spawn.out`, and `start_new_session=True`.
- `run.json` is the file-backed source of truth and is atomically rewritten;
  in-memory `Popen` handles only capture exit codes while the hub is the
  parent process.
- Reattach scans run dirs and probes pid liveness. If a run ended while the hub
  was down, `ended_ts` is persisted with `exit_code=None`.
- Live status is `active` or `waiting` based on pending control-dir questions.
  End status is classified as `completed`, `stopped`, `error`, or `ended` by
  task completion/archival evidence, stop evidence, and then exit code.
- `stop_run` writes `stop.flag` through `control_dir.request_stop` and refuses
  unknown, broken, or already-ended runs.
- HTTP run routes resolve repos through registry discovery, return serialized
  `RunStatus` payloads, expose start/list/get/stop behavior, and map launch/run
  manager errors to 4xx JSON responses (`RepoBusyError` and ended/blocked
  states use 409; unknown runs use 404).

### Control-dir File Channel

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

### Human Question Relay

Status: API + dashboard answering implemented; notifications planned

Description: Detect pending control-dir question files, list them remotely with
run/repo context, accept answers, write answer files, and audit the responder.

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

Remaining slices: notifications.

### Log Viewing

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
- `GET /api/runs/{run_id}/logs/stream` returns `text/event-stream`: a bounded
  initial tail, one `data:` event per completed appended line, heartbeat
  comments while idle, and terminal `event: end` on run end or log rotation.
  The tailer uses byte offsets and a raw-bytes pending buffer so a UTF-8
  character split across reads is not corrupted.
- Browser clients must stream with `fetch` plus a `ReadableStream` reader under
  the standard `Authorization: Bearer ...` header. The hub does not accept log
  stream credentials in query strings.

### Mobile Dashboard

Status: implemented

Description: Mobile-first web UI for repo/task inspection, run observation,
answering waiting questions, run history, and logs.

Modules: api, web.

APIs: public `GET /`, `GET /app.js`, `GET /styles.css`; protected
repo/task/run/escalation/log/history HTTP APIs. Log streaming uses the
bearer-authenticated SSE endpoint through `fetch`/ReadableStream, not browser
`EventSource`.

Implemented behavior:

- The dashboard is a vanilla, build-stepless SPA served as an explicit public
  shell. It stores the bearer token in `localStorage`, attaches
  `Authorization: Bearer ...` to every data `fetch`, clears the token on 401,
  and uses no cookie or query-token credential path.
- Repo list renders readiness from `/api/repos` and active/waiting badges from
  `/api/runs`; it never recomputes readiness in the browser.
- All-task and repo-detail views render task inventory from `/api/tasks` and
  `/api/repos/{repo_id}/tasks`, preserving frontmatter-authoritative status,
  `index_present`, and `missing`/`unknown` sentinel semantics.
- Task detail fetches `GET /api/repos/{repo_id}/tasks/{task_id}` for raw task
  content, shows current/most-recent run state, links to logs, and renders
  answer forms for waiting questions from `/api/escalations`.
- Run history uses `GET /api/history/runs` newest-first.
- Log view uses recent logs for finished runs and fetch/ReadableStream SSE for
  live runs. SSE heartbeats are skipped, blank data log lines are preserved,
  `event: end` closes the stream, navigation/reload/pause aborts the active
  stream, and untrusted log content is appended as text nodes.

### Auth and Private Access

Status: implemented

Description: Protect the service with bearer-token auth and deploy it behind Tailscale/private networking.

Modules: api, config.

Implemented behavior:

- `Config.token` is loaded from `ORCH_HUB_TOKEN`; empty/whitespace-only values
  count as unset.
- Every HTTP path except `/api/health` and the public dashboard shell assets
  requires `Authorization: Bearer <token>` when configured.
- The middleware is pure ASGI rather than Starlette `BaseHTTPMiddleware` so
  streaming responses are not buffered and client disconnects propagate.
- Protected routes fail closed with HTTP 503 when the token is unset; there is
  no unauthenticated mode.
- Auth error responses are static and never echo presented credentials.

### Notifications

Status: planned

Description: Notify when an escalation is created, a run ends/errors, or close-out completes. Bark is the v1 backend.

Modules: notifications, control_dir, runs.

### Launchd Service

Status: implemented

Description: Package/document the hub as a long-lived launchd agent on macOS.

Modules: config, api, docs, deploy.

Implemented behavior:

- `deploy/launchd/com.qinglin89.orch-hub.plist` is a launchd user-agent
  template that runs the installed `orch-hub-api` console script.
- The plist sets `ORCH_HUB_TOKEN`, `ORCH_HUB_HOST`, `ORCH_HUB_PORT`,
  `ORCH_HUB_AI_NATIVE_DEPLOYMENT`, `ORCH_HUB_DATA_DIR`, and an explicit
  `PATH`; token replacement is required because protected routes fail closed
  when the token is unset or blank.
- `RunAtLoad` and `KeepAlive` keep the service resident; stdout/stderr go to
  `~/Library/Logs/orch-hub/`.
- `docs/launchd.md` documents manual run-first verification, installation,
  `launchctl bootstrap` / `kickstart -k` / `bootout`, plist reloads, log
  inspection, and Tailscale-only exposure assumptions.

### Run History and Audit

Status: implemented

Description: Persist run lifecycle, stop request, and human answer-audit
metadata for later inspection without copying human answer text into history.

Modules: history, runs, api, models, control_dir.

APIs: `GET /api/history/runs`.

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
  serialized run status plus embedded persisted events; `limit` defaults to 50
  and is clamped to `0..200`.
- History readback treats missing `events.jsonl` as empty history and surfaces
  unreadable or malformed event lines as `history_error` events.

### Remote Intake-Task

Status: future

Description: Let the user type a request remotely and have the hub run a one-shot agent session to invoke `/intake-task`, producing a pending task that can then be orchestrated.

### IM Bot Control

Status: future

Description: Add an IM bot surface, likely Feishu long-connection, for repo/task menus and basic run controls. Web remains the primary v1 answering surface.
