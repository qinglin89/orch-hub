---
last-updated: 2026-07-11
verified-against: 47bc951e59ce6283b1e1595e009abca13a8618fa
---

# Features

| Feature | Status | Key Modules | Key APIs |
|---|---|---|---|
| Repo discovery and readiness | implemented | config, registry, readiness, git_meta, housekeeping, subprocess_utils, cli, api, models | CLI `repos`, CLI `repos --json`, `GET /api/repos`, `GET /api/repos/{repo_id}/commits` |
| Task inventory | implemented | task_store, api, models | `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`, `GET /api/repos/{repo_id}/tasks/{task_id}` |
| HTTP API foundation | implemented | api, config, registry, readiness, task_store, models | public shell routes, `GET /api/health`, repo/task/run/escalation/log/history APIs |
| Control-dir file channel | implemented | control_dir, models | internal file-channel functions |
| Run lifecycle | implemented | runs, api, config, control_dir, models, readiness, task_store | internal `RunManager`; `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{id}`, `POST /api/runs/{id}/stop` |
| Human question relay | implemented | control_dir, runs, api, notifications, web | `GET /api/escalations`, `POST /api/runs/{id}/escalations/{seq}/answer` |
| Log viewing | implemented | logs, runs, api | `GET /api/runs/{id}/logs`, `GET /api/runs/{id}/logs/stream` |
| Mobile dashboard | implemented | api, web | public shell routes plus protected repo/task/run/escalation/log/history/commit APIs, including run start/stop controls and run-scoped task-file view |
| Auth and private access | implemented | api, config | default-deny bearer middleware on HTTP routes |
| Notifications | implemented | notifications, config, runs, monitor, launchd, api, cli | Bark delivery backend; `ORCH_HUB_BARK_URL`; CLI `orch-hub notify test` |
| Launchd service | implemented | launchd, cli, config, api, docs, deploy | CLI `orch-hub launchd ...`, `orch-hub-api`, launchd plist/docs |
| Run history and audit | implemented | history, runs, api, models, control_dir | `GET /api/history/runs`, `GET /api/runs/{id}/task` |
| Remote intake-task | implemented | intake, intake_agent, api, web, models, readiness, task_store | `POST /api/intakes`, `GET /api/intakes`, `GET /api/intakes/{id}`, `GET /api/intakes/{id}/history`, `POST /api/intakes/{id}/revise|confirm|cancel` |
| IM bot control | future | notifications or bot adapter, api | future bot callback interface |

## Detail Files

| File | Contains |
|---|---|
| core.md | Repo discovery/readiness, task inventory, HTTP foundation, auth/private access |
| run-control.md | Run lifecycle, control-dir channel, question relay, log viewing, run history/audit |
| dashboard-ops.md | Mobile dashboard, notifications, launchd service, future remote intake and IM bot surfaces |
