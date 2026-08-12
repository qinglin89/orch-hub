---
last-updated: 2026-08-12
verified-against: 2e00e023cf20630e53f5cde5ad21d30375cc7717
---

# Features

| Feature | Status | Key Modules | Key APIs |
|---|---|---|---|
| Repo discovery and readiness | implemented | config, registry, readiness, git_meta, housekeeping, subprocess_utils, cli, api, models | CLI `repos`, CLI `repos --json`, `GET /api/repos`, `GET /api/repos/{repo_id}/commits` |
| Task inventory | implemented | task_store, api, models | `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`, `GET /api/repos/{repo_id}/tasks/{task_id}` |
| Task evaluation foundation | implemented | evaluation, ai_snapshot, session_reads, task_snapshots, config, task_store, runs, history, sessions, logs, git_meta, models, api, web | internal `EvaluationService` evidence build/generate/list/read/prune; schema-v16 bounded session-entry task files + fidelity, `.ai/` snapshot/patch, and ordered session-read evidence; `ORCH_HUB_EVAL_RETENTION`; `GET /api/evaluation`; `GET/POST /api/repos/{repo_id}/evaluations/{task_id}[/{evaluation_id}]`; batched newest-state in `GET /api/history/runs`; dashboard `#/evaluations/{repo}/{task}` |
| Task static metrics | implemented | static_metrics, session_reads, evaluation, config, models, api, web | internal `StaticEvaluator.run_metrics`, `build_report`, `generate_metrics`; entry-kind-aware transitions; six axes incl. advancement-only estimate fit and prefetch-set read completeness; remediation diagnostics; static schema v4; `POST /api/repos/{repo_id}/evaluations/{task_id}/metrics`; persists and displays `static_metrics.json` beside `evidence.json`; no model call |
| Task semantic evaluator | implemented | semantic_jobs, semantic, session_reads, static_metrics, evaluation, config, models, api, cli, web | internal `SemanticEvaluator.evaluate/doctor`, `SemanticJobManager`, `build_semantic_evidence`, `build_prompt`, `parse_report`, `ClaudeSemanticBackend`; semantic-schema-v2 retrieval effectiveness/rediscovery grounding; `POST`/`GET /api/repos/{repo_id}/evaluations/{task_id}/semantic/job`; `GET /api/evaluation/doctor`; `orch-hub eval doctor`; dashboard Layer 2 running/failed/completed job states plus report/history rendering; persists `semantic_report.json` and `report.md` beside evidence/static artifacts |
| Global evaluation report feed | implemented | report_catalog, evaluation, semantic, task_store, models, api, web | internal `ReportCatalog`; `GET /api/evaluation/reports[?after=<seq>&limit=<n>]`; `GET /api/evaluation/reports/{report_key}`; dashboard `#/reports`; append-only global order/identity, startup reconciliation, retention debt guard, contained integrity-reporting bundle export |
| Agent subscription usage | implemented | usage, statusline, runs, models, config, api, intake_agent, semantic, web, docs | primary Claude utilization-cache parser + bounded model-free `ClaudeUsageCollector`; stale-read and run-end refresh triggers; per-window cache/statusLine merge; Codex rollout reader; legacy `orch-hub-statusline`; protected `GET /api/usage`; display-only, fail-soft, no gates |
| HTTP API foundation | implemented | api, config, registry, readiness, task_store, models | public shell routes, `GET /api/health`, repo/task/run/escalation/log/history/intake/evaluation/usage APIs |
| Control-dir file channel | implemented | control_dir, models | internal file-channel functions |
| Run lifecycle | implemented | admission, runs, intake, orch_config, sessions, task_snapshots, api, config, control_dir, models, readiness, task_store | one per-repo run↔intake admission section before readiness; post-claim task-file snapshots per protocol session; internal `RunManager`; `GET /api/repos/{repo_id}/orchestrator-config`; `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{id}`, `GET /api/runs/{id}/sessions`, `GET /api/runs/{id}/effective-config`, `POST /api/runs/{id}/stop` |
| Human question relay | implemented | control_dir, runs, api, notifications, web | `GET /api/escalations`, `POST /api/runs/{id}/escalations/{seq}/answer` |
| Log viewing | implemented | logs, runs, api | `GET /api/runs/{id}/logs`, `GET /api/runs/{id}/logs/stream` |
| Mobile dashboard | implemented | api, web | responsive public shell routes with CSS-only neon-wasteland industrial control-console theme and independent persisted Mode (Dark/Light/System) plus Palette (Neon Cyan/Wasteland Orange) selection, plus protected repo/task/run/escalation/log/history/commit/evaluation/usage APIs, including Operations Overview, Subscription usage cards, searchable/counted all-tasks inventory, linked run↔intake admission explanations without readiness/state mutation, orchestrator-config-driven split DEV/REVIEW run start dialog, session context strips, filterable/collapsible grouped run history with per-role effective-config snapshots and batched evaluation-state lights, run-scoped task-file view, task-evaluation Layer 1/Layer 2 sidecar, and the global report feed view |
| Auth and private access | implemented | api, config | default-deny bearer middleware on HTTP routes |
| Notifications | implemented | notifications, config, runs, intake, monitor, launchd, api, cli | Bark delivery backend; `ORCH_HUB_BARK_URL`; CLI `orch-hub notify test` |
| Launchd service | implemented | launchd, cli, config, api, docs, deploy | CLI `orch-hub launchd ...`, `orch-hub-api`, launchd plist/docs |
| Run history and audit | implemented | history, runs, orch_config, sessions, api, models, control_dir, task_store | `GET /api/history/runs`, `GET /api/runs/{id}/task`, `GET /api/runs/{id}/sessions`, `GET /api/runs/{id}/effective-config` |
| Remote intake-task | implemented | admission, intake, intake_agent, runs, api, web, models, readiness, task_store, notifications | shared run↔intake admission; `POST /api/intakes`, `GET /api/intakes`, `GET /api/intakes/{id}` polling, `GET /api/intakes/{id}/history`, `POST /api/intakes/{id}/revise|confirm|cancel` |
| IM bot control | future | notifications or bot adapter, api | future bot callback interface |

## Detail Files

| File | Contains |
|---|---|
| core.md | Repo discovery/readiness, task inventory, HTTP foundation, auth/private access |
| evaluation.md | Task evaluation foundation, task static metrics, task semantic evaluator, global report feed |
| run-control.md | Run lifecycle, control-dir channel, question relay |
| run-observability.md | Log viewing, run history/audit |
| dashboard.md | Mobile dashboard shell, theme, responsiveness, accessibility, Overview, route infrastructure |
| dashboard-views.md | Mobile dashboard routed view catalog: repo/task/start/history/log/evaluation/report-feed/intake |
| ops.md | Agent subscription usage, notifications, launchd service, remote intake, future IM bot surface |
