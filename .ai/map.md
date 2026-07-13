---
last-updated: 2026-07-13
verified-against: 43c276722a202b443f555680804c045756a94a6a
---

# Project Map

## Feature -> Modules

| Feature | Modules | Key Decisions |
|---|---|---|
| Repo discovery and readiness | config, registry, readiness, git_meta, housekeeping, subprocess_utils, cli, api, models | implemented; ai-native-development registry is source of truth; absolute-path `aii-2` first, registry-file fallback; readiness JSON lists failed checks only; commit and housekeeping metadata are best-effort/display-only data |
| Task inventory | task_store, api, models | implemented; local parser only; index supplies active ids/summaries after normalizing bare-id or full-cell Markdown-link Task cells to safe bare ids; also supports five-column `id|title|status|session-est|blockers` indexes with `title` as summary; otherwise summaries use the legacy positional third column; task frontmatter supplies authoritative status/blockers; normal single-task content reads are index-gated and contained under `.ai-tasks`; exact-id archived content reads are available only for run-scoped history access |
| HTTP API foundation | api, config, registry, readiness, task_store, models | implemented; thin FastAPI routes reuse CLI/core payload helpers; env-configurable localhost-default uvicorn entry point; only health plus enumerated dashboard shell assets are public |
| Control-dir file channel | control_dir, models | implemented; file-channel only; lists question files, treats malformed answers as pending repair, writes answers atomically, and writes idempotent `stop.flag` |
| Run lifecycle | runs, api, config, control_dir, models, readiness, task_store | implemented; detached `caffeinate` subprocess launch with explicit agent-CLI `PATH`, external run dirs, file-backed `run.json` with internal `spawn_out`, per-repo serialization, reattach, stop/end classification, protected HTTP start/list/get/stop routes |
| Human question relay | control_dir, runs, api, notifications, models, web | implemented over live run control dirs; answers are audited via control-dir files and metadata-only history events; Bark pending-question push fires once per observed `(run_id, seq)` with compact plan-gate text and bounded non-plan prompts |
| Log viewing | logs, runs, api | implemented; locate logs from `RunRecord`, return bounded recent tails, fall back to non-empty `spawn.out` diagnostics when no orchestrator log exists, and stream SSE with bearer-header auth |
| Mobile dashboard | api, web | implemented mobile-first single-page v1; public static shell, localStorage bearer token, protected API fetches, repo/task run-state status layout, housekeeping warnings, cc-codex Start dialog with optional plan-gate flag, run history, run-scoped task-file view, recent commits, and recent/live log views |
| Remote intake-task | intake, intake_agent, api, web, models, readiness, task_store | implemented; readiness-gated draft/revise/confirm/cancel flow with hub-owned draft/audit state, off-lock transient agent turns polled by the dashboard review view, Claude CLI backend, complete draft review, and confirmation verified through task inventory as `pending`/`blocked` before a task becomes launchable |
| Auth and private access | api, config | implemented pure-ASGI bearer-token middleware; Tailscale-only/private exposure remains required; SSE uses header auth via fetch streams, not query tokens |
| Notifications | notifications, config, runs, launchd, api, cli | implemented Bark backend; `ORCH_HUB_BARK_URL` config; run manager owns terminal/pending-question triggers and isolation; CLI `notify test` probes delivery directly with launchd-plist fallback and URL redaction |
| Launchd service | launchd, cli, config, api, docs, deploy | implemented resident Mac service packaging and operations CLI; plist template is source of truth; lifecycle commands manage only the fixed service label, not orchestrator runs |
| Run history and audit | history, runs, api, models, control_dir | implemented; per-run `events.jsonl`, launch-time task status on run records, lifecycle/stop/answer metadata events, no answer text copied into history, run-scoped task-file reads can reach archived tasks without listing archive contents |

## Feature -> APIs

| Feature | APIs |
|---|---|
| Repo discovery and readiness | CLI `repos`, CLI `repos --json`, `GET /api/repos`, `GET /api/repos/{repo_id}/commits` |
| Task inventory | `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`, `GET /api/repos/{repo_id}/tasks/{task_id}` |
| HTTP API foundation | public shell routes, `GET /api/health`, repo/task/run/escalation/log/history APIs, `orch-hub-api` |
| Control-dir file channel | internal `list_questions`, `pending_questions`, `write_answer`, `request_stop` |
| Run lifecycle | internal `RunManager`; `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}`, `POST /api/runs/{run_id}/stop` |
| Human question relay | `GET /api/escalations`, `POST /api/runs/{run_id}/escalations/{seq}/answer` |
| Log viewing | `GET /api/runs/{run_id}/logs`, `GET /api/runs/{run_id}/logs/stream` |
| Mobile dashboard | `GET /`, `GET /app.js`, `GET /styles.css`; protected repo/task/run/escalation/log/history/commit APIs including `GET /api/runs/{run_id}/task` |
| Remote intake-task | `POST /api/intakes`, `GET /api/intakes`, `GET /api/intakes/{intake_id}`, `GET /api/intakes/{intake_id}/history`, `POST /api/intakes/{intake_id}/revise`, `POST /api/intakes/{intake_id}/confirm`, `POST /api/intakes/{intake_id}/cancel` |
| Auth and private access | `ORCH_HUB_TOKEN`, `Authorization: Bearer <token>`, public `GET /api/health`, protected docs/API routes |
| Notifications | internal notifier interface, `ORCH_HUB_BARK_URL`, Bark POST backend, CLI `orch-hub notify test` |
| Launchd service | CLI `orch-hub launchd deploy|update|start|stop|restart|status`, `orch-hub-api`, `deploy/launchd/com.qinglin89.orch-hub.plist`, `docs/launchd.md` |
| Run history and audit | `GET /api/history/runs`, `GET /api/runs/{run_id}/task` |

## Module -> Key Responsibilities

| Module | Responsibilities |
|---|---|
| config | Environment overrides, default ai-native-development path, bearer token and Bark URL loading, host/port/data-dir service settings, derived `aii-2` and registry-file paths |
| models | Stable repo/readiness/task inventory/question/run/history data structures shared by CLI/API/tests; `RunRecord` can reconstruct its `ManagedRepo` for run-scoped repo reads |
| registry | Discover managed repos from `aii-2` or registry file |
| readiness | Validate target repo launch readiness with seven fail-closed checks |
| git_meta | Read latest/recent git commit metadata for display, degrading to unavailable instead of gating repo content |
| housekeeping | Read `.ai/.housekeeping-pending` as display-only repo warning state |
| subprocess_utils | Run external commands consistently and surface missing executables distinctly |
| cli | Human and JSON repo readiness output; launchd command dispatch; notification delivery-check dispatch |
| launchd | Resolve launchd deployment settings including optional Bark URL, expose installed-plist Bark URL lookup, render/install plist from template, install/update package, run launchctl lifecycle commands, and probe service status |
| task_store | Parse `.ai-tasks` indexes, including Markdown-link Task cells, and task frontmatter into repo task inventories; read contained active or archived task file bodies by exact id |
| intake | Own remote intake lifecycle, hub-owned intake records/events, readiness and repo-busy gates, off-lock transient draft/revise/confirm turns, read-only draft/revise/cancel semantics, startup orphan reconciliation, and inventory-verified confirm/rollback |
| intake_agent | Drive a Claude CLI `/intake-task` session with structured envelopes, session resume, read-only draft/revise turns, and write-permitted confirm |
| runs | Start/list/get/stop/reattach orchestrator subprocess runs, persist run state, enforce per-repo serialization, classify live/end status, append run history events, and trigger isolated notifications |
| history | Append/read per-run JSONL audit/history events without storing answer text |
| control_dir | Implement question/answer/stop file-channel handling |
| logs | Locate, read, and stream orchestrator logs from run records |
| api | FastAPI app factory, pure-ASGI bearer auth middleware, public dashboard shell, health/repo/task/run/run-task/escalation/log/history route wiring, uvicorn entry point |
| notifications | Backend-agnostic notification payloads, Bark backend, notifier protocol, and run/question/probe formatters |
| web | Mobile dashboard SPA assets, including teardown-scoped live log streaming and intake polling |
| docs | Launchd operator documentation |
| deploy | Launchd plist template and future deployment templates |

## Maintenance Rules

Update this map when:

- a new major feature is added
- a feature depends on a new module
- a new API joins a feature workflow
- responsibilities shift between modules
- a planned module is renamed, split, or merged
