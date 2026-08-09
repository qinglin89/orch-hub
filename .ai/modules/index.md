---
last-updated: 2026-08-09
verified-against: 79b6b88c586380012edb532809d2318f34131f5a
---

# Modules

Full responsibilities live in the detail files below.

| Module | Location | Description |
|---|---|---|
| CLI | `orch_hub/cli.py` | `repos` readiness output; launchd/notify/eval-doctor dispatch |
| Config | `orch_hub/config.py` | env defaults/overrides; token/Bark/eval/Codex settings; derived paths |
| Launchd operations | `orch_hub/launchd.py` | deploy/update/lifecycle/status; Bark URL surfacing |
| Models | `orch_hub/models.py` | shared data shapes + payload helpers; run-record repo reconstruction |
| Registry | `orch_hub/registry.py` | managed repo discovery from `aii-2` or registry file |
| Readiness | `orch_hub/readiness.py` | fail-closed per-repo launch readiness checks |
| Git metadata | `orch_hub/git_meta.py` | best-effort commit reads and capped path-patch streams for display/evidence |
| Housekeeping | `orch_hub/housekeeping.py` | display-only `.ai/.housekeeping-pending` detection |
| Subprocess utilities | `orch_hub/subprocess_utils.py` | shared blocking wrappers; optional producer-capped stdout with whole-call timeout |
| Task inventory | `orch_hub/task_store.py` | `.ai-tasks` index/frontmatter parsing; contained active/archive reads |
| Repo admission | `orch_hub/admission.py` | per-repo atomic run↔intake admission; persisted occupancy probes; structured conflicts |
| Evaluation | `orch_hub/evaluation.py` | evidence targets/packs; artifact persistence/retention/reads |
| AI snapshot evidence | `orch_hub/ai_snapshot.py` | producer-bounded routed `.ai/` docs and same-window patch evidence |
| Static metrics | `orch_hub/static_metrics.py` | deterministic metrics over evidence packs; `static_metrics.json` |
| Semantic evaluator | `orch_hub/semantic.py` | evidence-bounded model evaluation; backend isolation; doctor |
| Semantic jobs | `orch_hub/semantic_jobs.py` | persisted async job status; duplicate guard; orphan reconciliation |
| Report catalog | `orch_hub/report_catalog.py` | append-only global publication feed; reconciliation; retention guard; contained bundle reads |
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
| sidecar.md | Evaluation, AI snapshot evidence, static metrics, semantic evaluator, semantic jobs, usage, statusLine writer |
| intake.md | Intake manager, intake agent |
| runtime.md | Repo admission, run manager, orchestrator launch config, monitor, history, control-dir, logs, notifications |
| service.md | API app, launchd operations |
| web.md | Web UI |

## Boundary Rules

- Registry code does not perform readiness checks.
- Readiness code does not launch orchestrators.
- Git metadata is best-effort display/evidence; never gates readiness, launch,
  or evidence.
- Housekeeping marker state is display-only; never gates readiness or launch.
- Task inventory reads task files; it does not decide orchestrator turns.
- Repo admission coordinates only cross-kind run/intake starts; each manager
  keeps same-kind serialization and file-backed occupancy truth. Admission is
  checked before, and remains separate from, readiness.
- Evaluation reads target tasks/run artifacts via existing stores; writes only
  contained artifacts under `<ORCH_HUB_DATA_DIR>/evaluations/`.
- AI snapshot evidence resolves memory through the target router/file shape,
  enforces repo containment and byte bounds at every read producer, and degrades
  docs and patches independently; it never writes the target snapshot.
- Static metrics read evidence packs; write only contained
  `static_metrics.json` in the same evaluation dir; no target/run/protocol
  mutation.
- Semantic evaluation reads only the bounded evidence/static prompt; writes
  only `semantic_report.json` + `report.md` under the evaluation dir; backends
  enforce read isolation structurally.
- Report catalog publishes complete archived-task artifacts with stable global
  identity/order and explicit provenance/integrity; it does not judge reports or
  decide protocol evolution. Its retention guard preserves unpublished debt.
- Usage reads provider-owned local signals plus the hub-owned Claude store and
  may run Claude's local `/usage` command only when its envelope proves zero
  model activity. The legacy statusLine writer captures only whitelisted
  capacity fields and passes the operator command through; neither directly
  opens credentials/endpoints, spends reset credits, or gates operations.
- Intake manager owns intake state, intake-ready trigger timing, and the
  confirmation postcondition; API/web neither treat drafts as tasks nor decide
  confirm outcomes.
- Run manager launches subprocesses; no task-protocol parsing beyond pre-start
  checks.
- Orchestrator launch config queries deployed `--print-config` and builds argv
  from structured selections; never imports orchestrator code or reads
  `orchestrator.toml`.
- History serializes run/audit metadata only; `RunManager` decides event
  timing.
- Control dir handles file-channel mechanics only; API/UI/run-manager own
  identity, audit storage, notifications, subprocess state.
- Logs locate/read/stream only from run records; callers never pass log paths.
- Run monitor schedules `RunManager.list_runs()` only; it owns no run state,
  notification policy, or de-duplication.
- API and CLI stay thin adapters over application functions.
- Launchd operations manage only the fixed hub service label; run stop stays
  in the run API/control-dir flow.
- Web UI consumes protected HTTP APIs; it never parses managed repo files
  directly.
- No module imports deployed orchestrator code.
