---
last-updated: 2026-08-12
verified-against: 2e00e023cf20630e53f5cde5ad21d30375cc7717
---

# Project Map

Cross-reference only; full behavior lives in `features/`, `modules/`,
`apis/` docs.

## Feature -> Modules

| Feature | Modules | Key Decisions |
|---|---|---|
| Repo discovery and readiness | config, registry, readiness, git_meta, housekeeping, subprocess_utils, cli, api, models | implemented; ai-native-development registry = truth (absolute `aii-2` first, registry-file fallback); readiness JSON lists failed checks only; commit/housekeeping metadata display-only |
| Task inventory | task_store, api, models | implemented; local parser only; index ids/summaries normalized (Markdown-link cells, five-column form); frontmatter authoritative status/blockers; reads index-gated + contained under `.ai-tasks`; exact-id archive + `session-est` reads |
| Task evaluation foundation | evaluation, ai_snapshot, session_reads, task_snapshots, config, task_store, runs, history, sessions, logs, git_meta, subprocess_utils, models, api, web | implemented; `ORCH_HUB_EVAL_ENABLED` gate + per-task `ORCH_HUB_EVAL_RETENTION`; exact active/archive ids; read-only `(repo_path, task_id)` run aggregation; schema-v16 producer-bounded evidence with session-entry task files/fidelity, best-effort git window, routed eager/session-prefetch `.ai/` snapshot/patches, ordered session-read rollups, and explicit cap/observability loss; hub-owned artifacts under `<data_dir>/evaluations/`; prune only after durable artifacts; run-history state is batched from newest artifacts |
| Task static metrics | static_metrics, session_reads, evaluation, config, models, api, web | implemented; gated deterministic Layer 1 over `EvidencePack`; rollups, entry-kind-aware protocol facts, six score axes incl. advancement-only estimate fit and `prefetch_set_read_completeness`, confidence, warnings; remediation is neutral diagnostic evidence; static schema v4; `static_metrics.json` beside `evidence.json`; no model call |
| Task semantic evaluator | semantic_jobs, semantic, ai_snapshot, session_reads, static_metrics, evaluation, config, models, notifications, api, cli, web | implemented; semantic-schema-v2 Layer 2 report over bounded evidence + static + persisted `.ai/` copy/read trace; retrieval effectiveness is distinct and grounds rediscovery risk; independent snippet/patch/read availability controls assessability/confidence; reports stamp the evidence-schema cohort and entry fidelity; persisted async job blocks duplicate in-flight calls, survives restart; fresh terminal outcomes emit metadata-only push; `semantic_report.json` + `report.md`; isolated Claude CLI backend (no tools/persistence/repo cwd; safe mode; plan-mode writes blocked); doctor probes without model call |
| Global evaluation report feed | report_catalog, evaluation, semantic, task_store, models, api, web | implemented; append-only cross-repo publication catalog; archived + coherent durable L1+L2 candidacy; monotonic `seq` cursor + opaque stable `report_key`; semantic hook + idempotent startup reconciliation; retention guard preserves publication debt; contained bundle export + explicit provenance/integrity/unavailability; hub publishes, consumer decides protocol evolution |
| Agent subscription usage | usage, statusline, runs, models, config, api, intake_agent, semantic, web, docs | implemented; Codex = newest local rollout; Claude = whitelisted local utilization cache + bounded model-free `/usage` refresh on stale reads/run ends; statusLine/stream = fallbacks; per-window merge; fail-soft display-only; never gates |
| HTTP API foundation | api, config, registry, readiness, task_store, models | implemented; thin FastAPI routes reuse CLI/core payload helpers; localhost-default env-configured uvicorn; only health + enumerated shell assets public |
| Control-dir file channel | control_dir, models | implemented; file-channel only; malformed answers = pending repair; atomic answer writes; mirrors answer-mode parser rules; idempotent `stop.flag` |
| Run lifecycle | admission, runs, intake, orch_config, sessions, task_snapshots, api, config, control_dir, models, readiness, task_store | implemented; shared per-repo run↔intake admission before readiness; detached `caffeinate` launch with explicit agent-CLI `PATH`; external run dirs; `run.json` + internal `spawn_out`; per-repo run serialization; reattach; live-only `stop_requested`; stop/end classification; split-role `--print-config` preview + legacy detection; split/legacy argv; session/effective-config snapshots plus post-claim task-file snapshots from log brackets |
| Human question relay | control_dir, runs, api, notifications, models, web | implemented over live control dirs; omitted mode = legacy verbatim; explicit answer/discuss fail-closed by question kind; metadata-only audit events carry mode; Bark push once per `(run_id, seq)`, compact plan-gate text |
| Log viewing | logs, runs, api | implemented; locate from `RunRecord`; bounded recent tails; `spawn.out` diagnostic fallback; bearer-header SSE |
| Mobile dashboard | api, web | implemented mobile-first SPA v1; public shell + localStorage bearer token; CSS-only theme with pre-paint Mode/Palette axes; Overview landing + display-only usage section; display-ready softening only for live runs with sole `git_clean` failure (raw gates unchanged); run↔intake admission pre-reflection with linked blockers and neutral readiness/state presentation; split DEV/REVIEW Start dialog; Run-history evaluation state lights + all-state global-feed entry; per-task evaluation L1/L2 sidecar + global report view; details in features/dashboard.md + dashboard-views.md |
| Remote intake-task | admission, intake, intake_agent, runs, api, web, models, readiness, task_store, notifications | implemented; shared per-repo run↔intake admission before readiness; hub-owned draft/audit state; off-lock transient turns + dashboard polling; recovered overlaps refuse revise/confirm but permit lifecycle-legal Cancel; Claude CLI backend; metadata-only intake-ready push; confirm verified via inventory `pending`/`blocked` |
| Auth and private access | api, config | implemented pure-ASGI bearer middleware; Tailscale-only/private exposure still required; SSE header auth via fetch streams, no query tokens |
| Notifications | notifications, config, runs, intake, semantic_jobs, monitor, launchd, api, cli | implemented Bark backend; `ORCH_HUB_BARK_URL`; owning managers trigger run/question, intake-ready, and semantic-job-outcome pushes; delivery isolated; `notify test` probe with plist fallback + URL redaction |
| Launchd service | launchd, cli, config, api, docs, deploy | implemented resident Mac service packaging + ops CLI; plist template = source of truth; manages only the fixed service label, not orchestrator runs |
| Run history and audit | history, runs, orch_config, sessions, task_snapshots, api, models, control_dir, task_store, evaluation | implemented; per-run `events.jsonl`; launch-time task status; metadata events with mode, never answer text; session/effective-config snapshots + session-entry task snapshots + `session-est`; batched newest evaluation state per task group; run-scoped reads reach archived/ended context without listing archive |

## Feature -> APIs

| Feature | APIs |
|---|---|
| Repo discovery and readiness | CLI `repos [--json]`; `GET /api/repos`; `GET /api/repos/{repo_id}/commits` |
| Task inventory | `GET /api/tasks`; `GET /api/repos/{repo_id}/tasks[/{task_id}]` |
| Task evaluation foundation | internal `EvaluationService` (target/evidence/generate/list/read/prune methods); `ORCH_HUB_EVAL_RETENTION`; `GET /api/evaluation`; `GET /api/repos/{repo_id}/evaluations/{task_id}[/{evaluation_id}]`; `POST .../evidence`; `GET /api/history/runs` batched newest-state; dashboard `#/evaluations/{repo}/{task}` |
| Task static metrics | internal `StaticEvaluator` (`run_metrics`, `build_report`, `generate_metrics`, `STATIC_METRICS_NAME`); `POST .../evaluations/{task_id}/metrics`; detail readback; dashboard Layer 1 |
| Task semantic evaluator | internal `SemanticEvaluator.evaluate`/`doctor`, `SemanticJobManager`, `build_semantic_evidence`, `build_prompt`, `parse_report`, `render_report_md`, `ClaudeSemanticBackend`; `POST`/`GET .../evaluations/{task_id}/semantic/job`; `GET /api/evaluation/doctor`; CLI `orch-hub eval doctor`; detail `semantic_report`/`report_md` readback |
| Global evaluation report feed | internal `ReportCatalog` (`record_evaluation`, `reconcile`, `page`, `read_bundle`, `unpublished_ids`); `GET /api/evaluation/reports[?after=<seq>&limit=<n>]`; `GET /api/evaluation/reports/{report_key}`; dashboard `#/reports` |
| Agent subscription usage | internal `ClaudeUsageCollector`, cache/statusline/stream-boundary/codex parsers, `UsageService`, `ClaudeUsageStore`, `RunManager` refresh hook; legacy `orch-hub-statusline`; `ORCH_HUB_CODEX_HOME`; protected `GET /api/usage`; Overview usage section |
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
| Run history and audit | `GET /api/history/runs` incl. optional batched evaluation state; `GET /api/runs/{run_id}/task`; run sessions/effective-config routes |

## Module -> Key Responsibilities

| Module | Responsibilities |
|---|---|
| config | env overrides; default ai-native-development path; token/Bark URL; host/port/data-dir; intake model/effort; eval gate/model/effort/retention; Claude/Codex homes; derived Claude transcript, `aii-2`, and registry paths |
| models | stable shared data shapes for CLI/API/tests; `RunRecord` → `ManagedRepo` reconstruction for run-scoped reads |
| registry | discover managed repos from `aii-2` or registry file |
| readiness | seven fail-closed launch readiness checks |
| git_meta | producer-capped latest/recent/windowed commit metadata + caller-capped path patch streams; whole-record truncation; external diff/textconv disabled; display/evidence only; degrades to unavailable, never gates |
| housekeeping | `.ai/.housekeeping-pending` display-only warning state |
| subprocess_utils | consistent external command runs; optional streamed stdout head/size cap with one drain+wait timeout and kill/reap on expiry; missing executables surfaced distinctly |
| cli | human + JSON readiness output; launchd, notify-test, eval-doctor dispatch |
| launchd | deployment settings incl. optional Bark URL; plist render/install; package install/update; launchctl lifecycle; status probe |
| task_store | parse `.ai-tasks` indexes + frontmatter; contained active/archive reads + typed `session-est` by exact id |
| admission | per-repo run↔intake start critical section; lock-free persisted occupancy probes; structured cross-kind conflicts; recovered-overlap guard |
| intake | intake lifecycle; hub-owned records/events; shared admission + same-kind + readiness gates; off-lock transient turns; recovered-overlap fail-closed writes; orphan reconciliation; intake-ready triggers; inventory-verified confirm/rollback |
| intake_agent | Claude CLI `/intake-task` session; structured envelopes; session resume; read-only draft/revise; write-permitted confirm |
| runs | start/list/get/stop/reattach subprocess runs; run state; per-repo serialization; live/end classification; history events; session/effective-config snapshots; isolated notification + usage-refresh terminal hooks |
| orch_config | split-role `--print-config` query; strict parse + legacy detection; split/legacy argv; tolerant `effective-config:` snapshots; no orchestrator imports |
| evaluation | resolve targets; aggregate related runs; build versioned bounded evidence packs including producer-bounded `.ai/` snapshots/patches and session-read behavior; persist/prune/list/read hub-owned artifacts behind optional gate |
| ai_snapshot | resolve routed eager/prefetch memory; enforce repo containment and producer byte caps; collect independently degradable `.ai/` docs and same-window patches |
| session_reads | bounded transcript-derived session-time prefetch facts and ordered declared/undeclared `.ai/` read traces; deterministic close-out/write-before-read exclusions; explicit gaps/caps/observability |
| static_metrics | deterministic Layer 1 score reports from evidence packs, incl. prefetch-set read completeness; static schema cohort; `static_metrics.json` persistence; retention after durable writes |
| semantic | Layer 2 model-judged reports over persisted evidence/static/`.ai/` and ordered read traces; retrieval-effectiveness/rediscovery grounding; independent degradation; evidence-schema cohort stamp; Markdown render; Claude CLI live-repo isolation; doctor; persisted artifacts behind gate |
| semantic_jobs | persisted async job records; duplicate in-flight guard; off-lock execution; orphan reconciliation; completed-job evaluation linkage; isolated fresh terminal-outcome push |
| report_catalog | append-only global publication identity/order; complete archived-report reconciliation; cross-process append lock; cursor paging; contained bundle/integrity read; retention debt guard; explicit provenance/unavailability |
| usage | Claude cache parser + single-flight model-free CLI collector; cache/statusLine figure merge; Codex rollout scan; stream-boundary merge; bounded aggregate cache; fail-soft/rolled states |
| statusline | legacy optional statusLine wrapper; captures only `rate_limits` + `subscription_type`; passes wrapped command through; never edits `~/.claude/**` |
| history | append/read per-run JSONL events; no answer text; answer-mode metadata round-trip |
| control_dir | question/answer/stop file mechanics; pure answer-mode normalization |
| logs | locate/read/stream orchestrator logs from run records |
| sessions | parse per-session context usage from logs; persist/load `sessions.json` |
| task_snapshots | capture/account one post-claim task file per protocol session from parsed log brackets; serialize run-artifact updates; UTF-8-safe bounded heads and exact/frontmatter completeness |
| api | app factory; pure-ASGI bearer middleware; public shell; protected route wiring; uvicorn entry |
| notifications | backend-agnostic payloads; Bark backend; notifier protocol; run/question/intake/semantic-job/probe formatters |
| web | mobile-first dashboard SPA assets (shell, theme, views, controls); details in modules/web.md |
| docs | launchd, evaluation, Claude usage/poller + legacy statusLine operator docs |
| deploy | launchd plist template + future deployment templates |

## Maintenance Rules

Update this map when:

- a new major feature is added
- a feature depends on a new module
- a new API joins a feature workflow
- responsibilities shift between modules
- a planned module is renamed, split, or merged
