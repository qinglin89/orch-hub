---
last-updated: 2026-07-06
verified-against: a6bc7abaa8f2d181f0efb6cdde519ebea8bb7c4c
---

# Project Map

## Feature -> Modules

| Feature | Modules | Key Decisions |
|---|---|---|
| Repo discovery and readiness | config, registry, readiness, subprocess_utils, cli, api, models | implemented; ai-native-development registry is source of truth; absolute-path `aii-2` first, registry-file fallback; readiness JSON lists failed checks only |
| Task inventory | task_store, api, models | implemented; local parser only; index supplies active ids/summaries, task frontmatter supplies authoritative status/blockers; run overlay remains future |
| HTTP API foundation | api, config, registry, readiness, task_store, models | implemented; thin FastAPI routes reuse CLI/core payload helpers; localhost-default uvicorn entry point |
| Run lifecycle | runs, subprocess_utils, control_dir, models | launch deployed orchestrator as subprocess; per-repo serialization |
| Human question relay | control_dir, runs, notifications, models | file-channel question/answer contract; audit answers |
| Log viewing | logs, runs, api | tail orchestrator log files; SSE for live stream |
| Mobile dashboard | api, web | mobile-first single-page v1 |
| Auth and private access | api, config | Tailscale plus bearer token; no public exposure |
| Notifications | notifications, control_dir, runs | Bark first; notifier interface for later backends |
| Launchd service | config, docs | resident Mac service |
| Run history and audit | runs, control_dir, models | preserve answer and run event history |

## Feature -> APIs

| Feature | APIs |
|---|---|
| Repo discovery and readiness | CLI `repos`, CLI `repos --json`, `GET /api/repos` |
| Task inventory | `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks` |
| HTTP API foundation | `GET /api/health`, `GET /api/repos`, `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`, `orch-hub-api` |
| Run lifecycle | future `POST /api/runs`, `GET /api/runs`, `POST /api/runs/{run_id}/stop` |
| Human question relay | future `GET /api/escalations`, `POST /api/escalations/{question_id}/answer` |
| Log viewing | future `GET /api/runs/{run_id}/logs`, `GET /api/runs/{run_id}/logs/stream` |
| Mobile dashboard | future web routes using the public HTTP APIs |
| Auth and private access | future bearer-token middleware/config |
| Notifications | internal notifier interface |
| Launchd service | docs/config, not an API |
| Run history and audit | future run history endpoints |

## Module -> Key Responsibilities

| Module | Responsibilities |
|---|---|
| config | Environment overrides, default ai-native-development path, derived `aii-2` and registry-file paths, future service settings |
| models | Stable repo/readiness/task inventory data structures shared by CLI/API/tests |
| registry | Discover managed repos from `aii-2` or registry file |
| readiness | Validate target repo launch readiness with seven fail-closed checks |
| subprocess_utils | Run external commands consistently and surface missing executables distinctly |
| cli | Human and JSON repo readiness output |
| task_store | Parse `.ai-tasks` indexes and task frontmatter into repo task inventories |
| runs | Own orchestrator subprocess lifecycle and run state |
| control_dir | Own question/answer/stop file-channel handling |
| logs | Read and stream orchestrator logs |
| api | FastAPI app factory, health/repo/task route wiring, uvicorn entry point, future auth/run/log routes |
| notifications | Bark and future notification backends |
| web | Mobile dashboard assets |

## Maintenance Rules

Update this map when:

- a new major feature is added
- a feature depends on a new module
- a new API joins a feature workflow
- responsibilities shift between modules
- a planned module is renamed, split, or merged
