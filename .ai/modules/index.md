---
last-updated: 2026-08-18
verified-against: 29e4c53412a0960cba31118549551de691b1af70
---

# Modules

Full responsibilities live in the detail files below.

| Module | Location | Description |
|---|---|---|
| CLI | `orch_hub/cli.py` | `repos` readiness output; launchd/notify/eval-doctor dispatch |
| Config | `orch_hub/config.py` | env defaults/overrides; token/Bark/eval/Claude/Codex settings; derived paths |
| Launchd operations | `orch_hub/launchd.py` | deploy/update/lifecycle/status; Bark URL surfacing |
| Models | `orch_hub/models.py` | shared data shapes + payload helpers; run-record repo reconstruction |
| Registry | `orch_hub/registry.py` | managed repo discovery from `mandrel` or registry file |
| Readiness | `orch_hub/readiness.py` | fail-closed per-repo launch readiness checks |
| Git metadata | `orch_hub/git_meta.py` | best-effort producer-capped commit reads and caller-capped path-patch streams for display/evidence |
| Housekeeping | `orch_hub/housekeeping.py` | display-only `.ai/.housekeeping-pending` detection |
| Subprocess utilities | `orch_hub/subprocess_utils.py` | shared blocking wrappers; optional producer-capped stdout with whole-call timeout |
| Task inventory | `orch_hub/task_store.py` | `.ai-tasks` index/frontmatter parsing; contained active/archive reads |
| Repo admission | `orch_hub/admission.py` | per-repo atomic run↔intake admission; persisted occupancy probes; structured conflicts |
| Evaluation | `orch_hub/evaluation.py` | evidence targets/packs; artifact persistence/retention/reads |
| AI snapshot evidence | `orch_hub/ai_snapshot.py` | producer-bounded routed `.ai/` docs and same-window patch evidence |
| Session read evidence | `orch_hub/session_reads.py` | bounded transcript-derived prefetch facts and ordered `.ai/` read traces |
| Static metrics | `orch_hub/static_metrics.py` | deterministic six-axis metrics over evidence packs; `static_metrics.json` |
| Semantic evaluator | `orch_hub/semantic.py` | evidence-bounded model evaluation incl. retrieval effectiveness; backend isolation; doctor |
| Semantic jobs | `orch_hub/semantic_jobs.py` | persisted async job status; duplicate guard; orphan reconciliation |
| Report catalog | `orch_hub/report_catalog.py` | append-only global publication feed; reconciliation; retention guard; contained bundle + byte-exact artifact reads |
| Usage | `orch_hub/usage.py` | Claude cache parser/poller + fallback merging; Codex normalization; fail-soft |
| StatusLine writer | `orch_hub/statusline.py` | legacy optional statusLine fallback feeding the usage sink |
| Intake manager | `orch_hub/intake.py` | remote intake lifecycle/audit; off-lock turns; verified confirm |
| Intake agent | `orch_hub/intake_agent.py` | Claude CLI `/intake-task` backend with envelopes/resume |
| Run manager | `orch_hub/runs.py` | subprocess lifecycle/state/stop; per-repo serialization; terminal side-effect hooks |
| Orchestrator launch config | `orch_hub/orch_config.py` | `--print-config` query/parse; split/legacy argv; config snapshots |
| Run monitor | `orch_hub/monitor.py` | resident status scanner for proactive notifications |
| History | `orch_hub/history.py` | per-run JSONL event storage/readback |
| Control dir | `orch_hub/control_dir.py` | question/answer/stop file-channel handling |
| Logs | `orch_hub/logs.py` | log location, bounded tails, SSE frames |
| Sessions | `orch_hub/sessions.py` | per-session context parsing; `sessions.json` persistence |
| Session entry task snapshots | `orch_hub/task_snapshots.py` | post-claim task-file capture/miss accounting; `task_snapshots.json` persistence |
| API | `orch_hub/api.py` | FastAPI app factory; bearer middleware; route wiring; uvicorn entry |
| Notifications | `orch_hub/notifications.py` | notifier interface; Bark backend; formatters |
| Web UI | `orch_hub/web/` | mobile-first dashboard SPA assets |
| Launchd plist | `deploy/launchd/com.qinglin89.orch-hub.plist` | launchd user-agent template |
| Operator docs | `docs/` | launchd/evaluation/Claude usage operator docs |
| Tests | `tests/` | pytest coverage with fake repos and mocked subprocesses |

## Detail Files

| File | Contains |
|---|---|
| core.md | CLI, config, models, registry, readiness, git metadata, housekeeping, subprocess utilities, task inventory |
| evaluation.md | Evaluation foundation, AI snapshot evidence, static metrics, semantic evaluator, semantic jobs |
| report-catalog.md | Global report catalog publication |
| usage.md | Subscription usage collection, statusLine writer |
| intake.md | Intake manager, intake agent |
| runtime.md | Repo admission, run manager, orchestrator launch config, run monitor |
| runtime-io.md | History, control-dir, logs, sessions, session-entry task snapshots, notifications |
| service.md | API app, launchd operations |
| web.md | Web UI shell, theme, accessibility/route infrastructure, Overview |
| web-views.md | Web UI routed views: repo/task, run controls, run history, report feed, evaluation, intake |
| boundaries.md | Cross-module boundary rules (what each module must not do) |
