---
last-updated: 2026-07-06
verified-against: 27df28d046cf78be92bae5793c43030f9e8500c7
---

# Features

## Feature Index

| Feature | Status | Key Modules | Key APIs |
|---|---|---|---|
| Repo discovery and readiness | implemented | config, registry, readiness, subprocess_utils, cli, models | CLI `repos`, CLI `repos --json`, future `GET /api/repos` |
| Task inventory | implemented | task_store, models | future `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks` |
| Run lifecycle | planned | runs, subprocess_utils, control_dir | future `POST /api/runs`, `GET /api/runs` |
| Human question relay | planned | control_dir, runs, notifications | future `GET /api/escalations`, `POST /api/escalations/{id}/answer` |
| Log viewing | planned | logs, runs, api | future `GET /api/runs/{id}/logs`, SSE stream |
| Mobile dashboard | planned | api, web | future web routes and API calls |
| Auth and private access | planned | api, config | bearer-token middleware |
| Notifications | planned | notifications, control_dir, runs | Bark delivery backend |
| Launchd service | planned | config, api | service packaging/docs |
| Run history and audit | planned | runs, control_dir, notifications | future history endpoints |
| Remote intake-task | future | runs or agent adapter, task_store | future task creation endpoint |
| IM bot control | future | notifications or bot adapter, api | future bot callback interface |

## Feature Details

### Repo Discovery and Readiness

Status: implemented

Description: Load managed repos from ai-native-development and report whether each is ready for orchestration. This validates the registry and target-repo deployment assumptions before service/UI work.

Modules: config, registry, readiness, models, subprocess_utils, cli.

APIs: `python -m orch_hub.cli repos`, `python -m orch_hub.cli repos --json`; future `GET /api/repos`.

Implemented behavior:

- Discovery invokes `<ai-native-development>/aii-2 registry list --json` by absolute path, then falls back to `<ai-native-development>/.registry/repos.local.json` when the command is unavailable or exits non-zero.
- Registry data is a JSON array of `{name, path, manifest}`. Missing fallback file means no repos; invalid JSON or malformed entries raise an operational error.
- Readiness runs seven checks in stable order: repo path, manifest, deployed orchestrator script, orchestrator venv Python, `.ai-tasks/index.md`, clean `git status --porcelain`, and passing `aii-2 status <repo>`.
- Public readiness JSON serializes `ready` and only failed checks as `{name, message}` reasons; ready repos have `checks: []`.
- Tests cover registry parsing/fallbacks, every requested missing-piece readiness failure, dirty git state, `aii-2 status` failure, config, and CLI output.

### Task Inventory

Status: implemented

Description: Read `.ai-tasks/index.md` and task frontmatter in managed repos. Distinguish task-file statuses from hub run overlay statuses.

Modules: task_store, models.

APIs: future task list endpoints.

Implemented behavior:

- `list_repo_tasks(repo)` reads `.ai-tasks/index.md` for active task ids and human summaries, preserving index row order.
- The index Status column is not authoritative; each `.ai-tasks/<id>.md` frontmatter supplies task status and blockers.
- Missing `.ai-tasks/index.md` is represented as `index_present=False` with no task rows, distinct from a present-but-empty index.
- Missing listed task files use status `missing`; files with no parseable status use `unknown`.
- `TaskSummary`, `RepoTaskInventory`, and `tasks_payload` provide reusable data shapes for future CLI/API adapters.

### Run Lifecycle

Status: planned

Description: Start, stop, list, and reattach orchestrator subprocesses. Each run attaches to one repo and one task. Same-repo runs are serialized.

Modules: runs, subprocess_utils, control_dir, models.

APIs: future run endpoints.

Dependency: orchestrator `--control-dir` support is needed before full remote control, but basic launch mechanics can be modeled before that.

### Human Question Relay

Status: planned

Description: Detect pending control-dir question files, render them in the UI, accept answers, write answer files, and audit the response.

Modules: control_dir, runs, notifications, models.

APIs: future escalation endpoints.

Dependency: external orchestrator change to support `--control-dir`.

### Log Viewing

Status: planned

Description: Show task/run logs in the dashboard and support live tailing through SSE.

Modules: logs, runs, api.

APIs: future log endpoints.

### Mobile Dashboard

Status: planned

Description: Web UI for repo list, task list, task detail, active/waiting status, answer input, and logs. It should be mobile-first and usable from iPhone/iPad.

Modules: api, web.

APIs: all public HTTP APIs.

### Auth and Private Access

Status: planned

Description: Protect the service with bearer-token auth and deploy it behind Tailscale/private networking.

Modules: api, config.

### Notifications

Status: planned

Description: Notify when an escalation is created, a run ends/errors, or close-out completes. Bark is the v1 backend.

Modules: notifications, control_dir, runs.

### Launchd Service

Status: planned

Description: Package/document the hub as a long-lived launchd agent on macOS.

Modules: config, api, docs.

### Run History and Audit

Status: planned

Description: Persist run events, human answers, stop requests, and end states for later inspection.

Modules: runs, control_dir.

### Remote Intake-Task

Status: future

Description: Let the user type a request remotely and have the hub run a one-shot agent session to invoke `/intake-task`, producing a pending task that can then be orchestrated.

### IM Bot Control

Status: future

Description: Add an IM bot surface, likely Feishu long-connection, for repo/task menus and basic run controls. Web remains the primary v1 answering surface.
