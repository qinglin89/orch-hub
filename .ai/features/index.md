---
last-updated: 2026-07-23
verified-against: 6cf6ebe2dcdca6b126f7cf4c0ee54edf0775f0c6
---

# Features

| Feature | Status | Key Modules | Key APIs |
|---|---|---|---|
| Repo discovery and readiness | implemented | config, registry, readiness, git_meta, housekeeping, subprocess_utils, cli, api, models | CLI `repos`, CLI `repos --json`, `GET /api/repos`, `GET /api/repos/{repo_id}/commits` |
| Task inventory | implemented | task_store, api, models | `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`, `GET /api/repos/{repo_id}/tasks/{task_id}` |
| Task evaluation foundation | implemented | evaluation, config, task_store, runs, history, sessions, logs, git_meta, models, api, web | internal `EvaluationService` evidence build/generate/list/read/prune; `ORCH_HUB_EVAL_RETENTION`; `GET /api/evaluation`; `GET/POST /api/repos/{repo_id}/evaluations/{task_id}[/{evaluation_id}]`; dashboard `#/evaluations/{repo}/{task}` |
| Task static metrics | implemented | static_metrics, evaluation, config, models, api, web | internal `StaticEvaluator.run_metrics`, `build_report`, `generate_metrics`; `POST /api/repos/{repo_id}/evaluations/{task_id}/metrics`; persists and displays `static_metrics.json` beside `evidence.json`; no model call |
| Task semantic evaluator | implemented | semantic_jobs, semantic, static_metrics, evaluation, config, models, api, cli, web | internal `SemanticEvaluator.evaluate/doctor`, `SemanticJobManager`, `build_semantic_evidence`, `build_prompt`, `parse_report`, `ClaudeSemanticBackend`; `POST`/`GET /api/repos/{repo_id}/evaluations/{task_id}/semantic/job`; `GET /api/evaluation/doctor`; `orch-hub eval doctor`; dashboard Layer 2 running/failed/completed job states plus report/history rendering; persists `semantic_report.json` and `report.md` beside evidence/static artifacts |
| HTTP API foundation | implemented | api, config, registry, readiness, task_store, models | public shell routes, `GET /api/health`, repo/task/run/escalation/log/history/intake/evaluation APIs |
| Control-dir file channel | implemented | control_dir, models | internal file-channel functions |
| Run lifecycle | implemented | runs, orch_config, sessions, api, config, control_dir, models, readiness, task_store | internal `RunManager`; `GET /api/repos/{repo_id}/orchestrator-config`; `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{id}`, `GET /api/runs/{id}/sessions`, `GET /api/runs/{id}/effective-config`, `POST /api/runs/{id}/stop` |
| Human question relay | implemented | control_dir, runs, api, notifications, web | `GET /api/escalations`, `POST /api/runs/{id}/escalations/{seq}/answer` |
| Log viewing | implemented | logs, runs, api | `GET /api/runs/{id}/logs`, `GET /api/runs/{id}/logs/stream` |
| Mobile dashboard | implemented | api, web | responsive public shell routes with CSS-only neon-wasteland industrial control-console theme and independent persisted Mode (Dark/Light/System) plus Palette (Neon Cyan/Wasteland Orange) selection, plus protected repo/task/run/escalation/log/history/commit/evaluation APIs, including Operations Overview, searchable/counted all-tasks inventory, orchestrator-config-driven four-mode run start dialog, session context strips, filterable/collapsible grouped run history with effective-config snapshots, run-scoped task-file view, and task-evaluation Layer 1/Layer 2 sidecar view with doctor-gated semantic controls |
| Auth and private access | implemented | api, config | default-deny bearer middleware on HTTP routes |
| Notifications | implemented | notifications, config, runs, intake, monitor, launchd, api, cli | Bark delivery backend; `ORCH_HUB_BARK_URL`; CLI `orch-hub notify test` |
| Launchd service | implemented | launchd, cli, config, api, docs, deploy | CLI `orch-hub launchd ...`, `orch-hub-api`, launchd plist/docs |
| Run history and audit | implemented | history, runs, orch_config, sessions, api, models, control_dir, task_store | `GET /api/history/runs`, `GET /api/runs/{id}/task`, `GET /api/runs/{id}/sessions`, `GET /api/runs/{id}/effective-config` |
| Remote intake-task | implemented | intake, intake_agent, api, web, models, readiness, task_store, notifications | `POST /api/intakes`, `GET /api/intakes`, `GET /api/intakes/{id}` polling, `GET /api/intakes/{id}/history`, `POST /api/intakes/{id}/revise|confirm|cancel` |
| IM bot control | future | notifications or bot adapter, api | future bot callback interface |

## Detail Files

| File | Contains |
|---|---|
| core.md | Repo discovery/readiness, task inventory, task evaluation foundation, task static metrics, task semantic evaluator, HTTP foundation, auth/private access |
| run-control.md | Run lifecycle, control-dir channel, question relay, log viewing, run history/audit |
| dashboard-ops.md | Mobile dashboard, notifications, launchd service, future remote intake and IM bot surfaces |
