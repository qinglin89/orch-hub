---
last-updated: 2026-07-23
verified-against: 506e8867fd47221bc27ba394686fc70c75e5e00e
---

# Project Map

## Feature -> Modules

| Feature | Modules | Key Decisions |
|---|---|---|
| Repo discovery and readiness | config, registry, readiness, git_meta, housekeeping, subprocess_utils, cli, api, models | implemented; ai-native-development registry is source of truth; absolute-path `aii-2` first, registry-file fallback; readiness JSON lists failed checks only; commit and housekeeping metadata are best-effort/display-only data |
| Task inventory | task_store, api, models | implemented; local parser only; index supplies active ids/summaries after normalizing bare-id or full-cell Markdown-link Task cells to safe bare ids; also supports five-column `id|title|status|session-est|blockers` indexes with `title` as summary; otherwise summaries use the legacy positional third column; task frontmatter supplies authoritative status/blockers; normal single-task content reads are index-gated and contained under `.ai-tasks`; exact-id archived content and typed `session-est` reads are available for run-scoped history access |
| Task evaluation foundation | evaluation, config, task_store, runs, history, sessions, logs, git_meta, models, api, web | implemented; optional `ORCH_HUB_EVAL_ENABLED` gate plus per-task `ORCH_HUB_EVAL_RETENTION`; resolves exact active/archive task ids, aggregates exact `(repo_path, task_id)` runs via read-only record loading, includes bounded events/sessions/log/spawn diagnostics plus best-effort git commit-window metadata, persists hub-owned artifacts under `<data_dir>/evaluations/`, lists/evaluates persisted artifacts newest-first, prunes older evaluations only after generation artifacts are durable, and exposes the sidecar through protected HTTP plus dashboard entry points |
| Task static metrics | static_metrics, evaluation, config, models, api, web | implemented; optional evaluation-gated deterministic Layer 1 report over `EvidencePack`; computes run/session rollups, taskfile/session-log protocol facts, five transparent score axes, confidence, and warnings; persists `static_metrics.json` beside `evidence.json`; exposed through `POST /metrics`, evaluation detail readback, and the dashboard Layer 1 view; no model call |
| Task semantic evaluator | semantic, static_metrics, evaluation, config, models, api, cli, web | implemented; optional Layer 2 model report over bounded evidence plus static metrics; persists `semantic_report.json` and `report.md` beside evidence/static artifacts; default Claude CLI backend disables tools, safe-mode customizations, session persistence, target-repo cwd, and plan-mode writes; doctor probes backend availability without a model call; exposed through `POST /semantic`, evaluation detail readback, `GET /api/evaluation/doctor`, `orch-hub eval doctor`, and dashboard Layer 2 controls/history labels |
| HTTP API foundation | api, config, registry, readiness, task_store, models | implemented; thin FastAPI routes reuse CLI/core payload helpers; env-configurable localhost-default uvicorn entry point; only health plus enumerated dashboard shell assets are public |
| Control-dir file channel | control_dir, models | implemented; file-channel only; lists question files, treats malformed answers as pending repair, writes answers atomically, mirrors answer-mode parser rules for explicit mode normalization, and writes idempotent `stop.flag` |
| Run lifecycle | runs, orch_config, sessions, api, config, control_dir, models, readiness, task_store | implemented; detached `caffeinate` subprocess launch with explicit agent-CLI `PATH`, external run dirs, file-backed `run.json` with internal `spawn_out`, per-repo serialization, reattach, live-only `stop_requested` overlay, stop/end classification, deployed-orchestrator `--print-config` preview query, structured launch-mode argv builder, per-session/effective-config snapshots parsed from logs, protected HTTP config/start/list/get/sessions/effective-config/stop routes |
| Human question relay | control_dir, runs, api, notifications, models, web | implemented over live run control dirs; omitted-mode answers stay legacy verbatim, explicit answer/discuss modes are normalized fail-closed by question kind, answers are audited via control-dir files plus metadata-only history events carrying mode, and Bark pending-question push fires once per observed `(run_id, seq)` with compact plan-gate text and bounded non-plan prompts |
| Log viewing | logs, runs, api | implemented; locate logs from `RunRecord`, return bounded recent tails, fall back to non-empty `spawn.out` diagnostics when no orchestrator log exists, and stream SSE with bearer-header auth |
| Mobile dashboard | api, web | implemented mobile-first single-page v1; public static shell with phone/tablet bottom navigation, tablet direct-child card-list grids, desktop flush-left rail plus per-view `#view` width classes (overview/list fill, detail/form bounded, logs/history wide, evaluation read bounded), nested chronological feeds kept single-column, CSS-only neon-wasteland industrial control-console theme in `styles.css` with independent persisted Appearance axes (Mode Dark/Light/System on pre-paint `data-theme`, Palette Neon Cyan/Wasteland Orange on pre-paint `data-palette`; Dark + Neon Cyan first-visit fallback; System OS-follow keeps palette unchanged), Neon Cyan graphite/cyan and Wasteland Orange warm oxidized-metal/dust/ember token presets, decorative toxic-green/radiation-amber/rust tokens kept separate from semantic state colours, localStorage bearer token, protected API fetches, default Operations Overview over existing repo/task/run/escalation/history APIs with route-scoped refresh, accessible route headings/focus/scoped announcements with keyboard-only visible heading landing ring, route-generation guard for stale route paints, router landing/error work, re-render landing, and intake/evaluation post-POST handoffs, repo/task run-state status layout, searchable/counted/clearable all-tasks view over existing task inventory, reload-stable Stop requested control from run state, housekeeping warnings, orchestrator-config-driven Default/Standard/Excellent/Custom Start dialog with backend selector and plan-gate toggle, kind-aware question answer buttons, live session context strips, filterable/counted/collapsible grouped run history with est-vs-actual rollups, effective-config snapshots, and inline evaluation links outside collapse toggles, run-scoped task-file view, task-evaluation Layer 1/Layer 2 sidecar view with doctor-gated semantic trigger, semantic report rendering, history model/schema labels, recent commits, and recent/live log views |
| Remote intake-task | intake, intake_agent, api, web, models, readiness, task_store, notifications | implemented; readiness-gated draft/revise/confirm/cancel flow with hub-owned draft/audit state, off-lock transient agent turns polled by the dashboard review view, Claude CLI backend, complete draft review, metadata-only intake-ready push after successful draft/revision, and confirmation verified through task inventory as `pending`/`blocked` before a task becomes launchable |
| Auth and private access | api, config | implemented pure-ASGI bearer-token middleware; Tailscale-only/private exposure remains required; SSE uses header auth via fetch streams, not query tokens |
| Notifications | notifications, config, runs, intake, monitor, launchd, api, cli | implemented Bark backend; `ORCH_HUB_BARK_URL` config; run manager owns terminal/pending-question triggers, intake manager owns intake-ready draft/revision triggers, delivery is isolated, and CLI `notify test` probes delivery directly with launchd-plist fallback and URL redaction |
| Launchd service | launchd, cli, config, api, docs, deploy | implemented resident Mac service packaging and operations CLI; plist template is source of truth; lifecycle commands manage only the fixed service label, not orchestrator runs |
| Run history and audit | history, runs, orch_config, sessions, api, models, control_dir, task_store | implemented; per-run `events.jsonl`, launch-time task status on run records, lifecycle/stop/answer metadata events including answer mode but not answer text, per-run session/effective-config snapshots and task `session-est` embedded for history views, and run-scoped task-file/effective-config reads can reach archived or ended-run context without listing archive contents |

## Feature -> APIs

| Feature | APIs |
|---|---|
| Repo discovery and readiness | CLI `repos`, CLI `repos --json`, `GET /api/repos`, `GET /api/repos/{repo_id}/commits` |
| Task inventory | `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`, `GET /api/repos/{repo_id}/tasks/{task_id}` |
| Task evaluation foundation | internal `EvaluationService.resolve_target`, `related_runs`, `build_evidence`, `generate_evidence`, `auto_prune`, `prune_evaluations`, `list_evaluations`, `read_artifact`, `read_text_artifact`, `artifact_dir`; `ORCH_HUB_EVAL_RETENTION`; `GET /api/evaluation`; `GET /api/repos/{repo_id}/evaluations/{task_id}`; `GET /api/repos/{repo_id}/evaluations/{task_id}/{evaluation_id}`; `POST /api/repos/{repo_id}/evaluations/{task_id}/evidence`; dashboard `#/evaluations/{repo}/{task}` |
| Task static metrics | internal `StaticEvaluator.run_metrics`, `build_report`, `generate_metrics`, and `STATIC_METRICS_NAME`; `POST /api/repos/{repo_id}/evaluations/{task_id}/metrics`; detail route readback; dashboard Layer 1 metrics rendering |
| Task semantic evaluator | internal `SemanticEvaluator.evaluate`, `SemanticEvaluator.doctor`, `build_semantic_evidence`, `build_prompt`, `parse_report`, `render_report_md`, `ClaudeSemanticBackend`; `POST /api/repos/{repo_id}/evaluations/{task_id}/semantic`; `GET /api/evaluation/doctor`; CLI `orch-hub eval doctor`; detail route `semantic_report` and `report_md` readback; dashboard semantic trigger/report/history rendering |
| HTTP API foundation | public shell routes, `GET /api/health`, repo/task/run/escalation/log/history/intake/evaluation APIs, `orch-hub-api` |
| Control-dir file channel | internal `list_questions`, `pending_questions`, `write_answer`, `request_stop` |
| Run lifecycle | internal `RunManager`; `GET /api/repos/{repo_id}/orchestrator-config`; `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}`, `GET /api/runs/{run_id}/sessions`, `GET /api/runs/{run_id}/effective-config`, `POST /api/runs/{run_id}/stop` |
| Human question relay | `GET /api/escalations`, `POST /api/runs/{run_id}/escalations/{seq}/answer` |
| Log viewing | `GET /api/runs/{run_id}/logs`, `GET /api/runs/{run_id}/logs/stream` |
| Mobile dashboard | `GET /`, `GET /app.js`, `GET /styles.css`; protected repo/task/run/escalation/log/history/commit/evaluation APIs including Overview data sources, `GET /api/repos/{repo_id}/orchestrator-config`, `GET /api/runs/{run_id}/task`, `GET /api/runs/{run_id}/sessions`, `GET /api/runs/{run_id}/effective-config`, and task-evaluation sidecar routes |
| Remote intake-task | `POST /api/intakes`, `GET /api/intakes`, `GET /api/intakes/{intake_id}`, `GET /api/intakes/{intake_id}/history`, `POST /api/intakes/{intake_id}/revise`, `POST /api/intakes/{intake_id}/confirm`, `POST /api/intakes/{intake_id}/cancel` |
| Auth and private access | `ORCH_HUB_TOKEN`, `Authorization: Bearer <token>`, public `GET /api/health`, protected docs/API routes |
| Notifications | internal notifier interface, `ORCH_HUB_BARK_URL`, Bark POST backend, CLI `orch-hub notify test` |
| Launchd service | CLI `orch-hub launchd deploy|update|start|stop|restart|status`, `orch-hub-api`, `deploy/launchd/com.qinglin89.orch-hub.plist`, `docs/launchd.md` |
| Run history and audit | `GET /api/history/runs`, `GET /api/runs/{run_id}/task`, `GET /api/runs/{run_id}/sessions`, `GET /api/runs/{run_id}/effective-config` |

## Module -> Key Responsibilities

| Module | Responsibilities |
|---|---|
| config | Environment overrides, default ai-native-development path, bearer token and Bark URL loading, host/port/data-dir/evaluation service settings plus semantic model and evaluation retention, derived `aii-2` and registry-file paths |
| models | Stable repo/readiness/task inventory/question/run/session/history/evaluation-target/static-metrics/semantic-report/semantic-doctor data structures shared by CLI/API/tests; `RunRecord` can reconstruct its `ManagedRepo` for run-scoped repo reads |
| registry | Discover managed repos from `aii-2` or registry file |
| readiness | Validate target repo launch readiness with seven fail-closed checks |
| git_meta | Read latest/recent/windowed git commit metadata for display and evaluation evidence, degrading to unavailable instead of gating repo/task content |
| housekeeping | Read `.ai/.housekeeping-pending` as display-only repo warning state |
| subprocess_utils | Run external commands consistently and surface missing executables distinctly |
| cli | Human and JSON repo readiness output; launchd command dispatch; notification delivery-check dispatch; semantic evaluator doctor dispatch |
| launchd | Resolve launchd deployment settings including optional Bark URL, expose installed-plist Bark URL lookup, render/install plist from template, install/update package, run launchctl lifecycle commands, and probe service status |
| task_store | Parse `.ai-tasks` indexes, including Markdown-link Task cells, and task frontmatter into repo task inventories; read contained active or archived task file bodies and typed `session-est` by exact id |
| intake | Own remote intake lifecycle, hub-owned intake records/events, readiness and repo-busy gates, off-lock transient draft/revise/confirm turns, read-only draft/revise/cancel semantics, startup orphan reconciliation, intake-ready notification triggers, and inventory-verified confirm/rollback |
| intake_agent | Drive a Claude CLI `/intake-task` session with structured envelopes, session resume, read-only draft/revise turns, and write-permitted confirm |
| runs | Start/list/get/stop/reattach orchestrator subprocess runs, persist run state, expose read-only run-record loading, enforce per-repo serialization, classify live/end status, append run history events, snapshot/serve session and effective-config context, and trigger isolated notifications |
| orch_config | Query deployed orchestrator `--print-config`, strict-parse launch preview configs, build structured launch-mode argv, and parse/persist tolerant `effective-config:` snapshots without importing orchestrator code |
| evaluation | Resolve task evaluation targets, aggregate related run evidence, build bounded evidence packs, persist/prune hub-owned evaluation artifacts, and list/read JSON/text artifacts behind the optional evaluation gate |
| static_metrics | Compute deterministic Layer 1 run/session/protocol score reports from evidence packs and persist `static_metrics.json` beside `evidence.json` behind the optional evaluation gate; API/web wrap it for sidecar presentation and retention runs after durable writes |
| semantic | Compute Layer 2 model-judged reports from bounded evidence/static inputs, render Markdown, enforce concrete Claude CLI evidence isolation, report backend availability via doctor, and persist semantic artifacts behind the optional evaluation gate |
| history | Append/read per-run JSONL audit/history events without storing answer text; round-trip answer mode metadata |
| control_dir | Implement question/answer/stop file-channel handling and pure answer-mode normalization |
| logs | Locate, read, and stream orchestrator logs from run records |
| sessions | Parse per-protocol-session context usage from orchestrator logs and persist/load `sessions.json` snapshots |
| api | FastAPI app factory, pure-ASGI bearer auth middleware, public dashboard shell, health/repo/task/run/run-task/escalation/log/history/intake/evaluation route wiring, uvicorn entry point |
| notifications | Backend-agnostic notification payloads, Bark backend, notifier protocol, and run/question/intake/probe formatters |
| web | Mobile-first responsive dashboard SPA assets, including the CSS-only neon-wasteland industrial control-console theme with independent persisted Appearance mode/palette selection, Operations Overview aggregation/refresh, flush-left desktop rail with per-view width classes, route-level accessibility/focus infrastructure with modality-gated page-title ring, orchestrator-config-driven run launch dialog, kind-aware answer controls, task-evaluation Layer 1/Layer 2 view/entry points with doctor gating and semantic history labels, effective-config history disclosure, teardown-scoped live log streaming, and intake polling |
| docs | Launchd and evaluation operator documentation |
| deploy | Launchd plist template and future deployment templates |

## Maintenance Rules

Update this map when:

- a new major feature is added
- a feature depends on a new module
- a new API joins a feature workflow
- responsibilities shift between modules
- a planned module is renamed, split, or merged
