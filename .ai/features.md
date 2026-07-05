---
last-updated: 2026-07-05
verified-against: 2ac51313fe081792d3cae8a8b40729561ab9b8f1
---

# Features

## Feature Index

| Feature | Status | Key Modules | Key APIs |
|---|---|---|---|
| Repo discovery and readiness | planned first | registry, readiness, cli, models | CLI `repos`, future `GET /api/repos` |
| Task inventory | planned | task_store, models | future `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks` |
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

Status: planned first

Description: Load managed repos from ai-native-development and report whether each is ready for orchestration. This validates the registry and target-repo deployment assumptions before service/UI work.

Modules: config, registry, readiness, models, subprocess_utils, cli.

APIs: `python -m orch_hub.cli repos`, `python -m orch_hub.cli repos --json`; future `GET /api/repos`.

Acceptance direction: pytest coverage for registry parsing, missing registry file, ready repo, missing deploy pieces, dirty git tree, and `aii-2 status` failure.

### Task Inventory

Status: planned

Description: Read `.ai-tasks/index.md` and task frontmatter in managed repos. Distinguish task-file statuses from hub run overlay statuses.

Modules: task_store, models.

APIs: future task list endpoints.

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
