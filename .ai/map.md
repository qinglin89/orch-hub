---
last-updated: 2026-07-08
verified-against: 8547d083ef9ca6131d0341f524ea24fb349402f2
---

# Project Map

## Feature -> Modules

| Feature | Modules | Key Decisions |
|---|---|---|
| Repo discovery and readiness | config, registry, readiness, subprocess_utils, cli, api, models | implemented; ai-native-development registry is source of truth; absolute-path `aii-2` first, registry-file fallback; readiness JSON lists failed checks only |
| Task inventory | task_store, api, models | implemented; local parser only; index supplies active ids/summaries, task frontmatter supplies authoritative status/blockers; run overlay is supplied by runs |
| HTTP API foundation | api, config, registry, readiness, task_store, models | implemented; thin FastAPI routes reuse CLI/core payload helpers; env-configurable localhost-default uvicorn entry point; non-health paths are bearer-protected |
| Control-dir file channel | control_dir, models | implemented; file-channel only; lists question files, treats malformed answers as pending repair, writes answers atomically, and writes idempotent `stop.flag` |
| Run lifecycle | runs, api, config, control_dir, models, readiness, task_store | implemented; detached `caffeinate` subprocess launch, external run dirs, file-backed `run.json`, per-repo serialization, reattach, stop/end classification, protected HTTP start/list/get/stop routes |
| Human question relay | control_dir, runs, api, notifications, models | API implemented over live run control dirs; answers are audited via control-dir files and metadata-only history events; dashboard and notification surfacing remain separate |
| Log viewing | logs, runs, api | implemented; locate logs from `RunRecord`, return bounded recent tails, and stream SSE with bearer-header auth |
| Mobile dashboard | api, web | mobile-first single-page v1 |
| Auth and private access | api, config | implemented pure-ASGI bearer-token middleware; Tailscale-only/private exposure remains required; SSE uses header auth via fetch streams, not query tokens |
| Notifications | notifications, control_dir, runs | Bark first; notifier interface for later backends |
| Launchd service | config, api, docs, deploy | implemented resident Mac service packaging |
| Run history and audit | history, runs, api, models, control_dir | implemented; per-run `events.jsonl`, launch-time task status on run records, lifecycle/stop/answer metadata events, no answer text copied into history |

## Feature -> APIs

| Feature | APIs |
|---|---|
| Repo discovery and readiness | CLI `repos`, CLI `repos --json`, `GET /api/repos` |
| Task inventory | `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks` |
| HTTP API foundation | `GET /api/health`, `GET /api/repos`, `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`, `orch-hub-api` |
| Control-dir file channel | internal `list_questions`, `pending_questions`, `write_answer`, `request_stop` |
| Run lifecycle | internal `RunManager`; `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}`, `POST /api/runs/{run_id}/stop` |
| Human question relay | `GET /api/escalations`, `POST /api/runs/{run_id}/escalations/{seq}/answer` |
| Log viewing | `GET /api/runs/{run_id}/logs`, `GET /api/runs/{run_id}/logs/stream` |
| Mobile dashboard | future web routes using protected HTTP APIs plus public health |
| Auth and private access | `ORCH_HUB_TOKEN`, `Authorization: Bearer <token>`, public `GET /api/health`, protected docs/API routes |
| Notifications | internal notifier interface |
| Launchd service | `orch-hub-api`, `deploy/launchd/com.qinglin89.orch-hub.plist`, `docs/launchd.md` |
| Run history and audit | `GET /api/history/runs` |

## Module -> Key Responsibilities

| Module | Responsibilities |
|---|---|
| config | Environment overrides, default ai-native-development path, bearer token loading, host/port/data-dir service settings, derived `aii-2` and registry-file paths |
| models | Stable repo/readiness/task inventory/question/run/history data structures shared by CLI/API/tests |
| registry | Discover managed repos from `aii-2` or registry file |
| readiness | Validate target repo launch readiness with seven fail-closed checks |
| subprocess_utils | Run external commands consistently and surface missing executables distinctly |
| cli | Human and JSON repo readiness output |
| task_store | Parse `.ai-tasks` indexes and task frontmatter into repo task inventories |
| runs | Start/list/get/stop/reattach orchestrator subprocess runs, persist run state, enforce per-repo serialization, classify live/end status, and append run history events |
| history | Append/read per-run JSONL audit/history events without storing answer text |
| control_dir | Implement question/answer/stop file-channel handling |
| logs | Locate, read, and stream orchestrator logs from run records |
| api | FastAPI app factory, pure-ASGI bearer auth middleware, health/repo/task/run/escalation/log/history route wiring, uvicorn entry point |
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
