---
last-updated: 2026-07-08
verified-against: bd7587946a0490c12b6d3a958a2edfd91f4bade8
---

# Features

## Feature Index

| Feature | Status | Key Modules | Key APIs |
|---|---|---|---|
| Repo discovery and readiness | implemented | config, registry, readiness, subprocess_utils, cli, api, models | CLI `repos`, CLI `repos --json`, `GET /api/repos` |
| Task inventory | implemented | task_store, api, models | `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks` |
| HTTP API foundation | implemented | api, config, registry, readiness, task_store, models | `GET /api/health`, `GET /api/repos`, `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks` |
| Control-dir file channel | implemented | control_dir, models | internal file-channel functions |
| Run lifecycle | planned | runs, subprocess_utils, control_dir | future `POST /api/runs`, `GET /api/runs` |
| Human question relay | planned | control_dir, runs, notifications | future `GET /api/escalations`, `POST /api/escalations/{id}/answer` |
| Log viewing | planned | logs, runs, api | future `GET /api/runs/{id}/logs`, SSE stream |
| Mobile dashboard | planned | api, web | future web routes and API calls |
| Auth and private access | implemented | api, config | default-deny bearer middleware on HTTP routes |
| Notifications | planned | notifications, control_dir, runs | Bark delivery backend |
| Launchd service | implemented | config, api, docs, deploy | `orch-hub-api`, launchd plist/docs |
| Run history and audit | planned | runs, control_dir, notifications | future history endpoints |
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

APIs: `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`.

Implemented behavior:

- `list_repo_tasks(repo)` reads `.ai-tasks/index.md` for active task ids and human summaries, preserving index row order.
- The index Status column is not authoritative; each `.ai-tasks/<id>.md` frontmatter supplies task status and blockers.
- Missing `.ai-tasks/index.md` is represented as `index_present=False` with no task rows, distinct from a present-but-empty index.
- Missing listed task files use status `missing`; files with no parseable status use `unknown`.
- `TaskSummary`, `RepoTaskInventory`, and `tasks_payload` provide reusable data shapes for future CLI/API adapters.

### HTTP API Foundation

Status: implemented

Description: Expose current repo readiness and task inventory data through a
resident FastAPI app without duplicating core logic, protected by bearer-token
auth on every non-health path.

Modules: api, config, registry, readiness, task_store, models.

APIs: `GET /api/health`, `GET /api/repos`, `GET /api/tasks`,
`GET /api/repos/{repo_id}/tasks`; `python -m orch_hub.api`; `orch-hub-api`;
`uvicorn orch_hub.api:app`.

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
- A default-deny middleware protects every other path, including OpenAPI docs,
  unknown paths, and future routes. Missing/malformed or invalid credentials
  return 401; unset `ORCH_HUB_TOKEN` returns 503 on protected routes.
- `run()` and `orch-hub-api` read `ORCH_HUB_HOST` / `ORCH_HUB_PORT` and bind
  to `127.0.0.1:8765` by default; any private exposure should remain
  Tailscale-only.

### Run Lifecycle

Status: planned

Description: Start, stop, list, and reattach orchestrator subprocesses. Each run attaches to one repo and one task. Same-repo runs are serialized.

Modules: runs, subprocess_utils, control_dir, models.

APIs: future run endpoints.

Dependency: the orchestrator and hub-side control-dir adapter are implemented;
run lifecycle owns run-dir layout, subprocess launch, process tracking, and API
integration.

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

Status: planned

Description: Detect pending control-dir question files, render them in the UI, accept answers, write answer files, and audit the response.

Modules: control_dir, runs, notifications, models.

APIs: future escalation endpoints.

Dependency: run manager must create fresh run dirs and expose API/UI routes
that reuse the implemented control-dir adapter; notifications and audit storage
remain future slices.

### Log Viewing

Status: planned

Description: Show task/run logs in the dashboard and support live tailing through SSE.

Modules: logs, runs, api.

APIs: future log endpoints.

### Mobile Dashboard

Status: planned

Description: Web UI for repo list, task list, task detail, active/waiting status, answer input, and logs. It should be mobile-first and usable from iPhone/iPad.

Modules: api, web.

APIs: all protected HTTP APIs plus the public health route.

### Auth and Private Access

Status: implemented

Description: Protect the service with bearer-token auth and deploy it behind Tailscale/private networking.

Modules: api, config.

Implemented behavior:

- `Config.token` is loaded from `ORCH_HUB_TOKEN`; empty/whitespace-only values
  count as unset.
- Every HTTP path except `/api/health` requires `Authorization: Bearer <token>`
  when configured.
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

Status: planned

Description: Persist run events, human answers, stop requests, and end states for later inspection.

Modules: runs, control_dir, models.

### Remote Intake-Task

Status: future

Description: Let the user type a request remotely and have the hub run a one-shot agent session to invoke `/intake-task`, producing a pending task that can then be orchestrated.

### IM Bot Control

Status: future

Description: Add an IM bot surface, likely Feishu long-connection, for repo/task menus and basic run controls. Web remains the primary v1 answering surface.
