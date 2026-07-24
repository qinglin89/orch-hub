---
last-updated: 2026-07-24
verified-against: 3c0a2c192a6f495ad83c0a71bc2ccbce526f33da
---

# Modules

| Module | Location | Description |
|---|---|---|
| CLI | `orch_hub/cli.py` | Implemented `repos` discovery/readiness output, launchd operations dispatch, notification delivery checks, and semantic evaluator doctor checks |
| Config | `orch_hub/config.py` | Implemented defaults, service env overrides, bearer token/Bark URL/evaluation gate/model/retention loading, data-dir setting, Codex home for usage, and derived `aii-2`/registry paths |
| Launchd operations | `orch_hub/launchd.py` | Implemented macOS launchd deploy/update/start/stop/restart/status helpers, including optional Bark URL plist surfacing and lookup |
| Models | `orch_hub/models.py` | Implemented repo, readiness, commit, task inventory, question, run, session/context, history, intake, evaluation target/run-link, static-metrics report, semantic-report, semantic-doctor, semantic-job, and subscription-usage models plus payload helpers and run-record repo reconstruction |
| Registry | `orch_hub/registry.py` | Implemented managed repo discovery from `aii-2` or registry file |
| Readiness | `orch_hub/readiness.py` | Implemented fail-closed per-repo launch readiness checks |
| Git metadata | `orch_hub/git_meta.py` | Implemented best-effort latest/recent/windowed commit reads for dashboard/API display and evaluation evidence |
| Housekeeping | `orch_hub/housekeeping.py` | Implemented display-only `.ai/.housekeeping-pending` marker detection |
| Subprocess utilities | `orch_hub/subprocess_utils.py` | Implemented shared subprocess execution wrappers |
| Task inventory | `orch_hub/task_store.py` | Implemented local parser for `.ai-tasks/index.md`, including Markdown-link Task cells, task frontmatter, typed `session-est`, and contained active/archive task reads |
| Evaluation | `orch_hub/evaluation.py` | Implemented optional read-only task evidence target resolution, related-run aggregation, bounded evidence-pack assembly, contained artifact persistence, durable-post-generation retention pruning, and persisted evaluation listing/artifact reads |
| Static metrics | `orch_hub/static_metrics.py` | Implemented optional deterministic task-quality metrics over evidence packs, score components, and `static_metrics.json` persistence beside `evidence.json` with retention after durable writes |
| Semantic evaluator | `orch_hub/semantic.py` | Implemented optional evidence-bounded model evaluation over evidence/static artifacts, Claude CLI backend isolation, semantic doctor readiness, `semantic_report.json`, and `report.md` persistence |
| Semantic jobs | `orch_hub/semantic_jobs.py` | Implemented persisted async semantic-evaluation job status, duplicate in-flight prevention, startup orphan reconciliation, bounded failure diagnostics, and completed-job `evaluation_id` linkage |
| Usage | `orch_hub/usage.py` | Implemented read-only Claude/Codex subscription-usage normalization, Codex rollout scanning, Claude statusLine sink ingestion, Claude observation storage/parser, bounded aggregation cache, and fail-soft provider snapshots for display |
| StatusLine writer | `orch_hub/statusline.py` | Implemented operator-installed `orch-hub-statusline` wrapper that captures Claude Code statusLine `rate_limits` into the hub data dir while passing the operator's own statusline command through unchanged |
| Intake manager | `orch_hub/intake.py` | Implemented remote intake lifecycle/state/audit service with off-lock draft/revise/confirm turns, cancel/reconcile safeguards, intake-ready notification triggers, and inventory-verified confirmation |
| Intake agent | `orch_hub/intake_agent.py` | Implemented Claude CLI `/intake-task` backend with structured envelopes, session resume, read-only draft/revise turns, and write-permitted confirm |
| Run manager | `orch_hub/runs.py` | Implemented orchestrator subprocess lifecycle, run state, stop requests, per-session/effective-config snapshot handling, and per-repo serialization |
| Orchestrator launch config | `orch_hub/orch_config.py` | Implemented deployed-orchestrator `--print-config` split-role query/strict parsing with legacy capability detection, split/legacy structured argv construction, and tolerant `effective-config:` snapshot helpers |
| Run monitor | `orch_hub/monitor.py` | Implemented resident daemon-thread status scanner for proactive notification delivery |
| History | `orch_hub/history.py` | Implemented per-run JSONL event storage and readback |
| Control dir | `orch_hub/control_dir.py` | Implemented question/answer/stop file-channel handling |
| Logs | `orch_hub/logs.py` | Implemented log location, bounded recent tails, and SSE tail frames |
| Sessions | `orch_hub/sessions.py` | Implemented tolerant parsing and persistence of per-protocol-session context usage from orchestrator logs |
| API | `orch_hub/api.py` | Implemented FastAPI app factory, fail-closed pure-ASGI bearer middleware, lifespan-owned monitor, public dashboard shell, health/repo/commit/task/run/run-task/escalation/log/history/intake/evaluation/usage routes, and uvicorn entry point |
| Notifications | `orch_hub/notifications.py` | Implemented notifier interface, Bark backend, and run/question/intake/probe notification formatters |
| Web UI | `orch_hub/web/` | Implemented vanilla mobile-first dashboard SPA assets, including responsive phone/tablet/desktop shell, CSS-only neon-wasteland industrial control-console theme with independent persisted Mode (Dark/Light/System) and Palette (Neon Cyan/Wasteland Orange) selection, Operations Overview aggregation/refresh plus Subscription usage cards, route-level accessibility/focus infrastructure, status layout, searchable/counted all-tasks inventory, recent commits, task-detail run controls/session strips, filterable/counted grouped run-history rollups, run-scoped task-file view, task-evaluation Layer 1/Layer 2 sidecar view with doctor-gated semantic controls, and polled remote intake review loop |
| Launchd plist | `deploy/launchd/com.qinglin89.orch-hub.plist` | Implemented launchd user-agent template for resident service operation |
| Operator docs | `docs/` | Implemented launchd install/start/stop/log guidance, optional task-evaluation boundary/retention/doctor documentation, and Claude statusLine usage hook documentation |
| Tests | `tests/` | Pytest coverage using fake repos and mocked subprocesses |

## Detail Files

| File | Contains |
|---|---|
| core.md | CLI, statusLine writer, config, models, registry, readiness, git metadata, housekeeping, subprocess utilities, task inventory, evaluation, static metrics, semantic evaluator, usage, intake manager, intake agent |
| runtime.md | Run manager, orchestrator launch config, monitor, history, control-dir, logs, notifications |
| web.md | API, web UI, and launchd operations |

## Boundary Rules

- Registry code does not perform readiness checks.
- Readiness code does not launch orchestrators.
- Git metadata is best-effort display/evaluation evidence and never gates
  readiness, run launch, or evidence generation.
- Housekeeping marker state is display-only and never gates readiness or run launch.
- Task inventory reads task files but does not decide orchestrator state-machine turns.
- Evaluation reads target tasks and run artifacts through existing stores and
  writes only contained artifacts under `<ORCH_HUB_DATA_DIR>/evaluations/`.
- Static metrics read evidence packs and write only contained
  `static_metrics.json` artifacts under the same evaluation dir; they do not
  mutate target repos, run records, or protocol state.
- Semantic evaluation reads only the bounded prompt built from evidence/static
  artifacts and writes only `semantic_report.json` plus `report.md` under the
  evaluation dir; concrete backends must enforce read isolation structurally.
- Usage reads only provider-owned local signals plus the hub-owned Claude
  observation store; the statusLine writer captures only whitelisted Claude
  capacity fields and passes through the operator command. Neither path starts
  probe turns, spends reset credits, or gates readiness, attention, run state,
  launch, or intake.
- Intake manager owns intake state, intake-ready notification trigger timing,
  and the confirmation postcondition; API/web layers do not treat drafts as
  active tasks or decide whether a confirm landed.
- Run manager launches subprocesses but does not parse task protocol beyond pre-start checks.
- Orchestrator launch config asks deployed orchestrators for effective launch
  policy and builds argv from structured selections; it never imports deployed
  orchestrator code or reads target `orchestrator.toml` directly.
- History serializes run/audit metadata only; `RunManager` decides when events
  are written.
- Control dir handles file-channel mechanics only; API/UI/run-manager layers own identity, audit storage, notifications, and subprocess state.
- Logs locate/read/stream files only from run records; API/UI callers never pass log paths.
- Run monitor schedules `RunManager.list_runs()` only; it must not own run
  state, notification policy, or de-duplication.
- API and CLI should be thin adapters over application functions.
- Launchd operations manage only the fixed hub service label; run stop
  semantics stay in the run API/control-dir flow.
- Web UI callers should consume protected HTTP APIs and must not parse managed
  repo files directly.
- No module imports deployed orchestrator code.
