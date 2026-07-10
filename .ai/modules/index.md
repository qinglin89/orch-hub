---
last-updated: 2026-07-10
verified-against: 29515ffc32699ee33332719dcad301209e8e9ebb
---

# Modules

| Module | Location | Description |
|---|---|---|
| CLI | `orch_hub/cli.py` | Implemented `repos` discovery/readiness output, launchd operations dispatch, and notification delivery checks |
| Config | `orch_hub/config.py` | Implemented defaults, service env overrides, bearer token and Bark URL loading, data-dir setting, and derived `aii-2`/registry paths |
| Launchd operations | `orch_hub/launchd.py` | Implemented macOS launchd deploy/update/start/stop/restart/status helpers, including optional Bark URL plist surfacing and lookup |
| Models | `orch_hub/models.py` | Implemented repo, readiness, commit, task inventory, question, and run models plus payload helpers and run-record repo reconstruction |
| Registry | `orch_hub/registry.py` | Implemented managed repo discovery from `aii-2` or registry file |
| Readiness | `orch_hub/readiness.py` | Implemented fail-closed per-repo launch readiness checks |
| Git metadata | `orch_hub/git_meta.py` | Implemented best-effort latest/recent commit reads for dashboard/API display |
| Housekeeping | `orch_hub/housekeeping.py` | Implemented display-only `.ai/.housekeeping-pending` marker detection |
| Subprocess utilities | `orch_hub/subprocess_utils.py` | Implemented shared subprocess execution wrappers |
| Task inventory | `orch_hub/task_store.py` | Implemented local parser for `.ai-tasks/index.md`, including Markdown-link Task cells, task frontmatter, and contained active/archive raw task-content reads |
| Run manager | `orch_hub/runs.py` | Implemented orchestrator subprocess lifecycle, run state, stop requests, and per-repo serialization |
| Run monitor | `orch_hub/monitor.py` | Implemented resident daemon-thread status scanner for proactive notification delivery |
| History | `orch_hub/history.py` | Implemented per-run JSONL event storage and readback |
| Control dir | `orch_hub/control_dir.py` | Implemented question/answer/stop file-channel handling |
| Logs | `orch_hub/logs.py` | Implemented log location, bounded recent tails, and SSE tail frames |
| API | `orch_hub/api.py` | Implemented FastAPI app factory, fail-closed pure-ASGI bearer middleware, lifespan-owned monitor, public dashboard shell, health/repo/commit/task/run/run-task/escalation/log/history routes, and uvicorn entry point |
| Notifications | `orch_hub/notifications.py` | Implemented notifier interface, Bark backend, and run/question/probe notification formatters |
| Web UI | `orch_hub/web/` | Implemented vanilla mobile dashboard SPA assets, including status layout, recent commits, task-detail run controls, and run-scoped task-file view |
| Launchd plist | `deploy/launchd/com.qinglin89.orch-hub.plist` | Implemented launchd user-agent template for resident service operation |
| Deployment docs | `docs/launchd.md` | Implemented install/start/stop/log guidance and Tailscale assumptions |
| Tests | `tests/` | Pytest coverage using fake repos and mocked subprocesses |

## Detail Files

| File | Contains |
|---|---|
| core.md | CLI, config, models, registry, readiness, git metadata, housekeeping, subprocess utilities, task inventory |
| runtime.md | Run manager, monitor, history, control-dir, logs, notifications |
| web.md | API, web UI, and launchd operations |

## Boundary Rules

- Registry code does not perform readiness checks.
- Readiness code does not launch orchestrators.
- Git metadata is display-only and never gates readiness or run launch.
- Housekeeping marker state is display-only and never gates readiness or run launch.
- Task inventory reads task files but does not decide orchestrator state-machine turns.
- Run manager launches subprocesses but does not parse task protocol beyond pre-start checks.
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
