---
last-updated: 2026-07-08
verified-against: ad8c79d6782abbf5c670550a9d4a7d6963088fae
---

# Project Map

## Feature -> Modules

| Feature | Modules | Key Decisions |
|---|---|---|
| Repo discovery and readiness | config, registry, readiness, subprocess_utils, cli, api, models | implemented; ai-native-development registry is source of truth; absolute-path `aii-2` first, registry-file fallback; readiness JSON lists failed checks only |
| Task inventory | task_store, api, models | implemented; local parser only; index supplies active ids/summaries, task frontmatter supplies authoritative status/blockers; run overlay is supplied by runs |
| HTTP API foundation | api, config, registry, readiness, task_store, models | implemented; thin FastAPI routes reuse CLI/core payload helpers; env-configurable localhost-default uvicorn entry point; non-health paths are bearer-protected |
| Control-dir file channel | control_dir, models | implemented; file-channel only; lists question files, treats malformed answers as pending repair, writes answers atomically, and writes idempotent `stop.flag` |
| Run lifecycle | runs, config, control_dir, models, readiness, task_store | implemented; detached `caffeinate` subprocess launch, external run dirs, file-backed `run.json`, per-repo serialization, reattach, stop/end classification |
| Human question relay | control_dir, runs, notifications, models | implemented control-dir adapter plus future API/UI/notification relay; audit answers |
| Log viewing | logs, runs, api | tail orchestrator log files; SSE for live stream |
| Mobile dashboard | api, web | mobile-first single-page v1 |
| Auth and private access | api, config | implemented bearer-token middleware; Tailscale-only/private exposure remains required; no public exposure |
| Notifications | notifications, control_dir, runs | Bark first; notifier interface for later backends |
| Launchd service | config, api, docs, deploy | implemented resident Mac service packaging |
| Run history and audit | runs, control_dir, models | preserve answer and run event history |

## Feature -> APIs

| Feature | APIs |
|---|---|
| Repo discovery and readiness | CLI `repos`, CLI `repos --json`, `GET /api/repos` |
| Task inventory | `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks` |
| HTTP API foundation | `GET /api/health`, `GET /api/repos`, `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`, `orch-hub-api` |
| Control-dir file channel | internal `list_questions`, `pending_questions`, `write_answer`, `request_stop` |
| Run lifecycle | internal `RunManager`; future `POST /api/runs`, `GET /api/runs`, `POST /api/runs/{run_id}/stop` |
| Human question relay | future `GET /api/escalations`, `POST /api/escalations/{question_id}/answer` |
| Log viewing | future `GET /api/runs/{run_id}/logs`, `GET /api/runs/{run_id}/logs/stream` |
| Mobile dashboard | future web routes using protected HTTP APIs plus public health |
| Auth and private access | `ORCH_HUB_TOKEN`, `Authorization: Bearer <token>`, public `GET /api/health`, protected docs/API routes |
| Notifications | internal notifier interface |
| Launchd service | `orch-hub-api`, `deploy/launchd/com.qinglin89.orch-hub.plist`, `docs/launchd.md` |
| Run history and audit | future run history endpoints |

## Module -> Key Responsibilities

| Module | Responsibilities |
|---|---|
| config | Environment overrides, default ai-native-development path, bearer token loading, host/port/data-dir service settings, derived `aii-2` and registry-file paths |
| models | Stable repo/readiness/task inventory/question/run data structures shared by CLI/API/tests |
| registry | Discover managed repos from `aii-2` or registry file |
| readiness | Validate target repo launch readiness with seven fail-closed checks |
| subprocess_utils | Run external commands consistently and surface missing executables distinctly |
| cli | Human and JSON repo readiness output |
| task_store | Parse `.ai-tasks` indexes and task frontmatter into repo task inventories |
| runs | Start/list/get/stop/reattach orchestrator subprocess runs, persist run state, enforce per-repo serialization, and classify live/end status |
| control_dir | Implement question/answer/stop file-channel handling |
| logs | Read and stream orchestrator logs |
| api | FastAPI app factory, bearer auth middleware, health/repo/task route wiring, uvicorn entry point, future run/log routes |
| notifications | Bark and future notification backends |
| web | Mobile dashboard assets |
| docs | Launchd operator documentation |
| deploy | Launchd plist template and future deployment templates |

## Maintenance Rules

Update this map when:

- a new major feature is added
- a feature depends on a new module
- a new API joins a feature workflow
- responsibilities shift between modules
- a planned module is renamed, split, or merged
