---
last-updated: 2026-07-05
verified-against: 03f15d281756cfbbb2aa37b66f91336c02e53b6a
---

# Modules

## Module Index

| Module | Location | Description |
|---|---|---|
| CLI | `orch_hub/cli.py` | Implemented `repos` command for discovery/readiness output |
| Config | `orch_hub/config.py` | Implemented defaults, `ORCH_HUB_AI_NATIVE_DEPLOYMENT`, and derived `aii-2`/registry paths |
| Models | `orch_hub/models.py` | Implemented `ManagedRepo`, readiness models, and repo payload helpers |
| Registry | `orch_hub/registry.py` | Implemented managed repo discovery from `aii-2` or registry file |
| Readiness | `orch_hub/readiness.py` | Implemented fail-closed per-repo launch readiness checks |
| Subprocess utilities | `orch_hub/subprocess_utils.py` | Implemented shared subprocess execution wrappers |
| Task inventory | `orch_hub/task_store.py` | Future parser for `.ai-tasks/index.md` and task frontmatter |
| Run manager | `orch_hub/runs.py` | Future orchestrator subprocess lifecycle and per-repo serialization |
| Control dir | `orch_hub/control_dir.py` | Future question/answer/stop file-channel handling |
| Logs | `orch_hub/logs.py` | Future log tailing and SSE support |
| API | `orch_hub/api.py` | Future FastAPI app and routes |
| Notifications | `orch_hub/notifications.py` | Future notifier interface and Bark adapter |
| Web UI | `orch_hub/web/` | Future mobile-first dashboard assets |
| Tests | `tests/` | Pytest coverage using fake repos and mocked subprocesses |

## Module Details

### CLI

Location: `orch_hub/cli.py`

Responsibility: expose user-facing commands. The implemented first command lists repos and readiness:

```bash
python -m orch_hub.cli repos
python -m orch_hub.cli repos --json
```

Dependencies: config, registry, readiness, models.

### Config

Location: `orch_hub/config.py`

Responsibility: define `ORCH_HUB_AI_NATIVE_DEPLOYMENT` override, default ai-native-development path, and derived `aii-2`/registry file paths:

```text
/Users/linqing/workplace/github.com/qinglin89/ai-native-development
```

Future config belongs here too: data dir, token, host/port, notifier settings.

### Models

Location: `orch_hub/models.py`

Responsibility: define stable data shapes for managed repos and readiness reports now; add task summaries, run records, questions, and answers in future slices. Public readiness payloads contain `ready` plus failed check reasons only.

### Registry

Location: `orch_hub/registry.py`

Responsibility: prefer `<ai-native-development>/aii-2 registry list --json`; if unavailable or failing, fallback to `<ai-native-development>/.registry/repos.local.json`. Validate and normalize JSON-array entries into `ManagedRepo`.

### Readiness

Location: `orch_hub/readiness.py`

Responsibility: check repo filesystem, deployed manifest, orchestrator script, venv Python, task index, clean git tree, and `aii-2 status <repo>`. Reports keep all seven checks internally and fail closed when paths or tools are missing.

### Subprocess Utilities

Location: `orch_hub/subprocess_utils.py`

Responsibility: central wrapper for running commands with cwd, capture, timeout if later needed, and concise output extraction. Do not hide command failures that readiness or API callers need to report.

### Task Inventory

Location: `orch_hub/task_store.py`

Responsibility: future parser for active task files. It should not import protocol or orchestrator code; use small local frontmatter parsing for fields needed by the UI.

### Run Manager

Location: `orch_hub/runs.py`

Responsibility: future owner of run lifecycle: start, stop, list, reattach, record pid, enforce per-repo mutex, connect run dirs, and expose run state to API/UI.

### Control Dir

Location: `orch_hub/control_dir.py`

Responsibility: future file-channel implementation for pending questions, answer writes, and stop flags.

## Boundary Rules

- Registry code does not perform readiness checks.
- Readiness code does not launch orchestrators.
- Task inventory reads task files but does not decide orchestrator state-machine turns.
- Run manager launches subprocesses but does not parse task protocol beyond pre-start checks.
- API and CLI should be thin adapters over application functions.
- No module imports deployed orchestrator code.
