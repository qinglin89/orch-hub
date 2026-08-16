---
last-updated: 2026-08-16
verified-against: cad31851334def4258d4004c2e62516807aaff85
---

# Project Map

Cross-reference only; full behavior lives in `features/`, `modules/`,
`apis/` docs. Cells are keyword anchors for routing and absorption sync —
behavior and rationale stay in the owning doc.

## Feature -> Modules

| Feature | Modules | Key Decisions |
|---|---|---|
| Repo discovery and readiness | config, registry, readiness, git_meta, housekeeping, subprocess_utils, cli, api, models | registry = truth; fail-closed readiness; git/housekeeping display-only |
| Task inventory | task_store, api, models | frontmatter-authoritative status; index-gated contained reads |
| Task evaluation foundation | evaluation, protocol_provenance, ai_snapshot, session_reads, task_snapshots, config, task_store, runs, history, sessions, logs, git_meta, subprocess_utils, models, api, web | gated read-only schema-v18 evidence packs; unanimous protocol identity; hub-owned artifacts; descriptor containment |
| Task static metrics | static_metrics, session_reads, evaluation, config, models, api, web | deterministic Layer 1; static schema v4; no model call |
| Task semantic evaluator | semantic_jobs, semantic, ai_snapshot, session_reads, static_metrics, evaluation, config, models, notifications, api, cli, web | evidence-bounded Layer 2; semantic-schema-v2; persisted async jobs; isolated Claude CLI |
| Global evaluation report feed | report_catalog, protocol_provenance, evaluation, semantic, task_store, models, api, cli, web | append-only catalog; seq + report_key identity; reconciliation; verify/repair CLI; consumers decide evolution |
| Agent subscription usage | usage, statusline, runs, models, config, api, intake_agent, semantic, web, docs | display-only fail-soft; cache-primary Claude; model-free refresh; never gates |
| HTTP API foundation | api, config, registry, readiness, task_store, models | thin routes over shared helpers; only health + shell public |
| Control-dir file channel | control_dir, models | file-channel only; atomic answers; idempotent stop.flag |
| Run lifecycle | admission, runs, intake, orch_config, sessions, task_snapshots, protocol_provenance, api, config, control_dir, models, readiness, task_store | admission before readiness; per-repo serialization; detached launch; pre-spawn protocol capture + observed-end recheck |
| Human question relay | control_dir, runs, api, notifications, models, web | fail-closed answer modes; metadata-only audit; one push per (run_id, seq) |
| Log viewing | logs, runs, api | run-record located; bounded tails; bearer-header SSE |
| Mobile dashboard | api, web | mobile-first SPA; display-only overlays; details in features/dashboard.md + dashboard-views.md |
| Remote intake-task | admission, intake, intake_agent, runs, api, web, models, readiness, task_store, notifications | hub-owned drafts; read-only until confirm; inventory-verified confirm |
| Auth and private access | api, config | fail-closed bearer middleware; private network still required |
| Notifications | notifications, config, runs, intake, semantic_jobs, monitor, launchd, api, cli | Bark backend; isolated best-effort delivery |
| Launchd service | launchd, cli, config, api, docs, deploy | plist template = truth; fixed service label only |
| Run history and audit | history, runs, orch_config, sessions, task_snapshots, api, models, control_dir, task_store, evaluation | append-only events.jsonl; metadata only, never answer text |

## Feature -> APIs

| Feature | APIs |
|---|---|
| Repo discovery and readiness | CLI `repos [--json]`; `GET /api/repos`; `GET /api/repos/{repo_id}/commits` |
| Task inventory | `GET /api/tasks`; `GET /api/repos/{repo_id}/tasks[/{task_id}]` |
| Task evaluation foundation | internal `EvaluationService`; `ORCH_HUB_EVAL_RETENTION`; `GET /api/evaluation`; `GET /api/repos/{repo_id}/evaluations/{task_id}[/{evaluation_id}]`; `POST .../evidence`; batched state in `GET /api/history/runs`; dashboard `#/evaluations/{repo}/{task}` |
| Task static metrics | internal `StaticEvaluator`; `POST .../evaluations/{task_id}/metrics`; detail readback; dashboard Layer 1 |
| Task semantic evaluator | internal `SemanticEvaluator` + `SemanticJobManager`; `POST`/`GET .../evaluations/{task_id}/semantic/job`; `GET /api/evaluation/doctor`; CLI `orch-hub eval doctor`; report readback |
| Global evaluation report feed | internal `ReportCatalog`; CLI `orch-hub eval catalog verify\|repair [--json] [--confirm]`; `GET /api/evaluation/reports[?after=<seq>&limit=<n>]`; `GET /api/evaluation/reports/{report_key}[/artifacts/{name}]`; dashboard `#/reports` |
| Agent subscription usage | internal `UsageService` + collectors/parsers; legacy `orch-hub-statusline`; `ORCH_HUB_CODEX_HOME`; protected `GET /api/usage` |
| HTTP API foundation | public shell routes; `GET /api/health`; all protected data APIs; `orch-hub-api` |
| Control-dir file channel | internal `list_questions`, `pending_questions`, `write_answer`, `request_stop` |
| Run lifecycle | internal `RunManager`; `GET /api/repos/{repo_id}/orchestrator-config`; `POST/GET /api/runs`; `GET /api/runs/{run_id}[/sessions|/effective-config]`; `POST /api/runs/{run_id}/stop` |
| Human question relay | `GET /api/escalations`; `POST /api/runs/{run_id}/escalations/{seq}/answer` |
| Log viewing | `GET /api/runs/{run_id}/logs[/stream]` |
| Mobile dashboard | `GET /`, `/app.js`, `/styles.css`; all protected APIs |
| Remote intake-task | `POST/GET /api/intakes`; `GET /api/intakes/{intake_id}[/history]`; `POST /api/intakes/{intake_id}/revise|confirm|cancel` |
| Auth and private access | `ORCH_HUB_TOKEN`; `Authorization: Bearer <token>`; public `GET /api/health` |
| Notifications | internal notifier interface; `ORCH_HUB_BARK_URL`; CLI `orch-hub notify test` |
| Launchd service | CLI `orch-hub launchd deploy\|update\|start\|stop\|restart\|status`; `orch-hub-api`; `deploy/launchd/com.qinglin89.orch-hub.plist`; `docs/launchd.md` |
| Run history and audit | `GET /api/history/runs`; `GET /api/runs/{run_id}/task`; run sessions/effective-config routes |

## Module -> Key Responsibilities

| Module | Responsibilities |
|---|---|
| config | env overrides; tokens/URLs; eval/usage settings; derived paths |
| models | shared data shapes; run-record repo reconstruction |
| registry | managed-repo discovery from `aii-2` or registry file |
| readiness | fail-closed launch readiness checks |
| git_meta | best-effort commit/patch metadata; display/evidence only |
| housekeeping | display-only `.ai/.housekeeping-pending` state |
| subprocess_utils | shared blocking command runs; capped streamed stdout |
| cli | human/JSON output; launchd/notify/eval/catalog dispatch |
| launchd | plist render/install; launchctl lifecycle; status probe |
| task_store | `.ai-tasks` index/frontmatter parsing; contained reads |
| admission | per-repo run↔intake start critical section |
| intake | intake lifecycle/audit; verified confirm/rollback |
| intake_agent | Claude CLI `/intake-task` session backend |
| runs | run subprocess lifecycle/state; per-repo serialization; terminal hooks |
| orch_config | `--print-config` query/parse; split/legacy argv |
| evaluation | evidence targets/packs; contained artifact persistence/retention |
| protocol_provenance | deployment identity capture + observed-end verification |
| ai_snapshot | bounded routed `.ai/` docs + patch evidence |
| session_reads | bounded transcript prefetch facts + ordered read traces |
| static_metrics | deterministic Layer 1 scores; `static_metrics.json` |
| semantic | evidence-bounded Layer 2 reports; backend isolation; doctor |
| semantic_jobs | persisted async jobs; duplicate guard; orphan reconciliation |
| report_catalog | append-only publication feed; audit/repair; retention guard |
| usage | Claude cache/CLI + Codex rollout collection; fail-soft merge |
| statusline | legacy optional statusLine fallback wrapper |
| monitor | resident run-status scan for proactive notifications |
| history | per-run JSONL events; metadata only |
| control_dir | question/answer/stop file mechanics |
| logs | run-record log location/tails/streams |
| sessions | per-session context parsing; `sessions.json` |
| task_snapshots | post-claim task-file capture; `task_snapshots.json` |
| api | app factory; bearer middleware; route wiring; uvicorn entry |
| notifications | notifier interface; Bark backend; formatters |
| web | mobile-first dashboard SPA assets; see modules/web.md |
| docs | operator docs |
| deploy | launchd plist + deployment templates |

## Maintenance Rules

Update this map when:

- a new major feature is added
- a feature depends on a new module
- a new API joins a feature workflow
- responsibilities shift between modules
- a planned module is renamed, split, or merged
