---
last-updated: 2026-07-21
verified-against: ba505657be3bdb33d39216d89f71fa27dbd851cb
---

# Modules

| Module | Location | Description |
|---|---|---|
| CLI | `orch_hub/cli.py` | Implemented `repos` discovery/readiness output, launchd operations dispatch, and notification delivery checks |
| Config | `orch_hub/config.py` | Implemented defaults, service env overrides, bearer token and Bark URL loading, data-dir setting, and derived `aii-2`/registry paths |
| Launchd operations | `orch_hub/launchd.py` | Implemented macOS launchd deploy/update/start/stop/restart/status helpers, including optional Bark URL plist surfacing and lookup |
| Models | `orch_hub/models.py` | Implemented repo, readiness, commit, task inventory, question, run, session/context, history, and intake models plus payload helpers and run-record repo reconstruction |
| Registry | `orch_hub/registry.py` | Implemented managed repo discovery from `aii-2` or registry file |
| Readiness | `orch_hub/readiness.py` | Implemented fail-closed per-repo launch readiness checks |
| Git metadata | `orch_hub/git_meta.py` | Implemented best-effort latest/recent commit reads for dashboard/API display |
| Housekeeping | `orch_hub/housekeeping.py` | Implemented display-only `.ai/.housekeeping-pending` marker detection |
| Subprocess utilities | `orch_hub/subprocess_utils.py` | Implemented shared subprocess execution wrappers |
| Task inventory | `orch_hub/task_store.py` | Implemented local parser for `.ai-tasks/index.md`, including Markdown-link Task cells, task frontmatter, typed `session-est`, and contained active/archive task reads |
| Intake manager | `orch_hub/intake.py` | Implemented remote intake lifecycle/state/audit service with off-lock draft/revise/confirm turns, cancel/reconcile safeguards, intake-ready notification triggers, and inventory-verified confirmation |
| Intake agent | `orch_hub/intake_agent.py` | Implemented Claude CLI `/intake-task` backend with structured envelopes, session resume, read-only draft/revise turns, and write-permitted confirm |
| Run manager | `orch_hub/runs.py` | Implemented orchestrator subprocess lifecycle, run state, stop requests, per-session snapshot handling, and per-repo serialization |
| Run monitor | `orch_hub/monitor.py` | Implemented resident daemon-thread status scanner for proactive notification delivery |
| History | `orch_hub/history.py` | Implemented per-run JSONL event storage and readback |
| Control dir | `orch_hub/control_dir.py` | Implemented question/answer/stop file-channel handling |
| Logs | `orch_hub/logs.py` | Implemented log location, bounded recent tails, and SSE tail frames |
| Sessions | `orch_hub/sessions.py` | Implemented tolerant parsing and persistence of per-protocol-session context usage from orchestrator logs |
| API | `orch_hub/api.py` | Implemented FastAPI app factory, fail-closed pure-ASGI bearer middleware, lifespan-owned monitor, public dashboard shell, health/repo/commit/task/run/run-task/escalation/log/history/intake routes, and uvicorn entry point |
| Notifications | `orch_hub/notifications.py` | Implemented notifier interface, Bark backend, and run/question/intake/probe notification formatters |
| Web UI | `orch_hub/web/` | Implemented vanilla mobile-first dashboard SPA assets, including responsive phone/tablet/desktop shell, CSS-only neon-wasteland industrial control-console theme with independent persisted Mode (Dark/Light/System) and Palette (Neon Cyan/Wasteland Orange) selection, Operations Overview aggregation/refresh, route-level accessibility/focus infrastructure, status layout, searchable/counted all-tasks inventory, recent commits, task-detail run controls/session strips, filterable/counted grouped run-history rollups, run-scoped task-file view, and polled remote intake review loop |
| Launchd plist | `deploy/launchd/com.qinglin89.orch-hub.plist` | Implemented launchd user-agent template for resident service operation |
| Deployment docs | `docs/launchd.md` | Implemented install/start/stop/log guidance and Tailscale assumptions |
| Tests | `tests/` | Pytest coverage using fake repos and mocked subprocesses |

## Detail Files

| File | Contains |
|---|---|
| core.md | CLI, config, models, registry, readiness, git metadata, housekeeping, subprocess utilities, task inventory, intake manager, intake agent |
| runtime.md | Run manager, monitor, history, control-dir, logs, notifications |
| web.md | API, web UI, and launchd operations |

## Boundary Rules

- Registry code does not perform readiness checks.
- Readiness code does not launch orchestrators.
- Git metadata is display-only and never gates readiness or run launch.
- Housekeeping marker state is display-only and never gates readiness or run launch.
- Task inventory reads task files but does not decide orchestrator state-machine turns.
- Intake manager owns intake state, intake-ready notification trigger timing,
  and the confirmation postcondition; API/web layers do not treat drafts as
  active tasks or decide whether a confirm landed.
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
