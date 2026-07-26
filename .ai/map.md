---
last-updated: 2026-07-26
verified-against: 3582180d93032df92e22fd39c83b64896b526583
---

# Project Map

Cross-reference only; full behavior lives in `features/`, `modules/`,
`apis/` docs.

## Feature -> Modules

| Feature | Modules | Key Decisions |
|---|---|---|
| Repo discovery and readiness | config, registry, readiness, git_meta, housekeeping, subprocess_utils, cli, api, models | implemented; ai-native-development registry = truth (absolute `aii-2` first, registry-file fallback); readiness JSON lists failed checks only; commit/housekeeping metadata display-only |
| Task inventory | task_store, api, models | implemented; local parser only; index ids/summaries normalized (Markdown-link cells, five-column form); frontmatter authoritative status/blockers; reads index-gated + contained under `.ai-tasks`; exact-id archive + `session-est` reads |
| Task evaluation foundation | evaluation, config, task_store, runs, history, sessions, logs, git_meta, models, api, web | implemented; `ORCH_HUB_EVAL_ENABLED` gate + per-task `ORCH_HUB_EVAL_RETENTION`; exact active/archive ids; read-only `(repo_path, task_id)` run aggregation; bounded evidence + best-effort git window; hub-owned artifacts under `<data_dir>/evaluations/`; prune only after durable artifacts |
| Task static metrics | static_metrics, evaluation, config, models, api, web | implemented; gated deterministic Layer 1 over `EvidencePack`; rollups, protocol facts, five score axes, confidence, warnings; `static_metrics.json` beside `evidence.json`; no model call |
| Task semantic evaluator | semantic_jobs, semantic, static_metrics, evaluation, config, models, notifications, api, cli, web | implemented; gated Layer 2 model report over bounded evidence + static; persisted async job blocks duplicate in-flight calls, survives restart; fresh terminal outcomes emit metadata-only push; `semantic_report.json` + `report.md`; isolated Claude CLI backend (no tools/persistence/repo cwd; safe mode; plan-mode writes blocked); doctor probes without model call |
| Agent subscription usage | usage, statusline, models, config, api, intake_agent, semantic, web | implemented; normalized Claude/Codex windows/percentages/resets/freshness/plan/credits; Codex = newest local rollout `rate_limits`; Claude = hub store fed by operator statusLine sink (2.1.218 stream-json has no 5h/7d windows → wired observer usually inert); fail-soft display-only; never gates readiness/attention/run/launch/intake |
| HTTP API foundation | api, config, registry, readiness, task_store, models | implemented; thin FastAPI routes reuse CLI/core payload helpers; localhost-default env-configured uvicorn; only health + enumerated shell assets public |
| Control-dir file channel | control_dir, models | implemented; file-channel only; malformed answers = pending repair; atomic answer writes; mirrors answer-mode parser rules; idempotent `stop.flag` |
| Run lifecycle | runs, orch_config, sessions, api, config, control_dir, models, readiness, task_store | implemented; detached `caffeinate` launch with explicit agent-CLI `PATH`; external run dirs; `run.json` + internal `spawn_out`; per-repo serialization; reattach; live-only `stop_requested`; stop/end classification; split-role `--print-config` preview + legacy detection; split/legacy argv; session/effective-config snapshots from logs |
| Human question relay | control_dir, runs, api, notifications, models, web | implemented over live control dirs; omitted mode = legacy verbatim; explicit answer/discuss fail-closed by question kind; metadata-only audit events carry mode; Bark push once per `(run_id, seq)`, compact plan-gate text |
| Log viewing | logs, runs, api | implemented; locate from `RunRecord`; bounded recent tails; `spawn.out` diagnostic fallback; bearer-header SSE |
| Mobile dashboard | api, web | implemented mobile-first SPA v1; public shell + localStorage bearer token; CSS-only theme with pre-paint Mode/Palette axes; Overview landing + display-only usage section; display-ready softening only for live runs with sole `git_clean` failure (raw gates unchanged); split DEV/REVIEW Start dialog; evaluation L1/L2 sidecar; details in features/dashboard.md + dashboard-views.md |
| Remote intake-task | intake, intake_agent, api, web, models, readiness, task_store, notifications | implemented; readiness-gated draft/revise/confirm/cancel; hub-owned draft/audit state; off-lock transient turns + dashboard polling; Claude CLI backend; metadata-only intake-ready push; confirm verified via inventory `pending`/`blocked` |
| Auth and private access | api, config | implemented pure-ASGI bearer middleware; Tailscale-only/private exposure still required; SSE header auth via fetch streams, no query tokens |
| Notifications | notifications, config, runs, intake, semantic_jobs, monitor, launchd, api, cli | implemented Bark backend; `ORCH_HUB_BARK_URL`; owning managers trigger run/question, intake-ready, and semantic-job-outcome pushes; delivery isolated; `notify test` probe with plist fallback + URL redaction |
| Launchd service | launchd, cli, config, api, docs, deploy | implemented resident Mac service packaging + ops CLI; plist template = source of truth; manages only the fixed service label, not orchestrator runs |
| Run history and audit | history, runs, orch_config, sessions, api, models, control_dir, task_store | implemented; per-run `events.jsonl`; launch-time task status; metadata events with mode, never answer text; session/effective-config snapshots + `session-est` for history views; run-scoped reads reach archived/ended context without listing archive |

## Feature -> APIs

| Feature | APIs |
|---|---|
| Repo discovery and readiness | CLI `repos [--json]`; `GET /api/repos`; `GET /api/repos/{repo_id}/commits` |
| Task inventory | `GET /api/tasks`; `GET /api/repos/{repo_id}/tasks[/{task_id}]` |
| Task evaluation foundation | internal `EvaluationService` (target/evidence/generate/list/read/prune methods); `ORCH_HUB_EVAL_RETENTION`; `GET /api/evaluation`; `GET /api/repos/{repo_id}/evaluations/{task_id}[/{evaluation_id}]`; `POST .../evidence`; dashboard `#/evaluations/{repo}/{task}` |
| Task static metrics | internal `StaticEvaluator` (`run_metrics`, `build_report`, `generate_metrics`, `STATIC_METRICS_NAME`); `POST .../evaluations/{task_id}/metrics`; detail readback; dashboard Layer 1 |
| Task semantic evaluator | internal `SemanticEvaluator.evaluate`/`doctor`, `SemanticJobManager`, `build_semantic_evidence`, `build_prompt`, `parse_report`, `render_report_md`, `ClaudeSemanticBackend`; `POST`/`GET .../evaluations/{task_id}/semantic/job`; `GET /api/evaluation/doctor`; CLI `orch-hub eval doctor`; detail `semantic_report`/`report_md` readback |
| Agent subscription usage | console script `orch-hub-statusline`; internal `UsageService`, `ClaudeUsageStore`, statusline/stream/codex parsers; `ORCH_HUB_CODEX_HOME`; protected `GET /api/usage`; Overview usage section |
| HTTP API foundation | public shell routes; `GET /api/health`; all protected data APIs; `orch-hub-api` |
| Control-dir file channel | internal `list_questions`, `pending_questions`, `write_answer`, `request_stop` |
| Run lifecycle | internal `RunManager`; `GET /api/repos/{repo_id}/orchestrator-config`; `POST/GET /api/runs`; `GET /api/runs/{run_id}[/sessions|/effective-config]`; `POST /api/runs/{run_id}/stop` |
| Human question relay | `GET /api/escalations`; `POST /api/runs/{run_id}/escalations/{seq}/answer` |
| Log viewing | `GET /api/runs/{run_id}/logs[/stream]` |
| Mobile dashboard | `GET /`, `/app.js`, `/styles.css`; all protected APIs incl. orchestrator-config, run task/sessions/effective-config, evaluation sidecar routes |
| Remote intake-task | `POST/GET /api/intakes`; `GET /api/intakes/{intake_id}[/history]`; `POST /api/intakes/{intake_id}/revise|confirm|cancel` |
| Auth and private access | `ORCH_HUB_TOKEN`; `Authorization: Bearer <token>`; public `GET /api/health`; protected docs/API routes |
| Notifications | internal notifier interface; `ORCH_HUB_BARK_URL`; Bark POST backend; CLI `orch-hub notify test` |
| Launchd service | CLI `orch-hub launchd deploy\|update\|start\|stop\|restart\|status`; `orch-hub-api`; `deploy/launchd/com.qinglin89.orch-hub.plist`; `docs/launchd.md` |
| Run history and audit | `GET /api/history/runs`; `GET /api/runs/{run_id}/task`; run sessions/effective-config routes |

## Module -> Key Responsibilities

| Module | Responsibilities |
|---|---|
| config | env overrides; default ai-native-development path; token/Bark URL; host/port/data-dir; intake model/effort; eval gate/model/effort/retention; Codex home; derived `aii-2`/registry paths |
| models | stable shared data shapes for CLI/API/tests; `RunRecord` → `ManagedRepo` reconstruction for run-scoped reads |
| registry | discover managed repos from `aii-2` or registry file |
| readiness | seven fail-closed launch readiness checks |
| git_meta | latest/recent/windowed commit metadata; display/evidence only; degrades to unavailable, never gates |
| housekeeping | `.ai/.housekeeping-pending` display-only warning state |
| subprocess_utils | consistent external command runs; missing executables surfaced distinctly |
| cli | human + JSON readiness output; launchd, notify-test, eval-doctor dispatch |
| launchd | deployment settings incl. optional Bark URL; plist render/install; package install/update; launchctl lifecycle; status probe |
| task_store | parse `.ai-tasks` indexes + frontmatter; contained active/archive reads + typed `session-est` by exact id |
| intake | intake lifecycle; hub-owned records/events; readiness + repo-busy gates; off-lock transient turns; orphan reconciliation; intake-ready triggers; inventory-verified confirm/rollback |
| intake_agent | Claude CLI `/intake-task` session; structured envelopes; session resume; read-only draft/revise; write-permitted confirm |
| runs | start/list/get/stop/reattach subprocess runs; run state; per-repo serialization; live/end classification; history events; session/effective-config snapshots; isolated notifications |
| orch_config | split-role `--print-config` query; strict parse + legacy detection; split/legacy argv; tolerant `effective-config:` snapshots; no orchestrator imports |
| evaluation | resolve targets; aggregate related runs; bounded evidence packs; persist/prune/list/read hub-owned artifacts behind optional gate |
| static_metrics | deterministic Layer 1 score reports from evidence packs; `static_metrics.json` persistence; retention after durable writes |
| semantic | Layer 2 model-judged reports; Markdown render; Claude CLI evidence isolation; doctor; persisted artifacts behind gate |
| semantic_jobs | persisted async job records; duplicate in-flight guard; off-lock execution; orphan reconciliation; completed-job evaluation linkage; isolated fresh terminal-outcome push |
| usage | read-only Claude/Codex normalization; Codex rollout scan; statusLine sink ingestion; bounded cache; fail-soft states |
| statusline | operator-installed statusLine wrapper; captures only `rate_limits` + `subscription_type`; passes wrapped command through; never edits `~/.claude/**` |
| history | append/read per-run JSONL events; no answer text; answer-mode metadata round-trip |
| control_dir | question/answer/stop file mechanics; pure answer-mode normalization |
| logs | locate/read/stream orchestrator logs from run records |
| sessions | parse per-session context usage from logs; persist/load `sessions.json` |
| api | app factory; pure-ASGI bearer middleware; public shell; protected route wiring; uvicorn entry |
| notifications | backend-agnostic payloads; Bark backend; notifier protocol; run/question/intake/semantic-job/probe formatters |
| web | mobile-first dashboard SPA assets (shell, theme, views, controls); details in modules/web.md |
| docs | launchd, evaluation, Claude statusLine operator docs |
| deploy | launchd plist template + future deployment templates |

## Maintenance Rules

Update this map when:

- a new major feature is added
- a feature depends on a new module
- a new API joins a feature workflow
- responsibilities shift between modules
- a planned module is renamed, split, or merged
