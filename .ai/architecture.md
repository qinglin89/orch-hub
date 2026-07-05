---
last-updated: 2026-07-05
verified-against: 2ac51313fe081792d3cae8a8b40729561ab9b8f1
---

# Architecture

## System Diagram

```text
Mobile browser / remote client
        |
        | Tailscale + bearer token
        v
orch-hub service on Mac
        |
        | reads registry/status
        v
ai-native-development / aii-2
        |
        | discovers managed repos
        v
Target repo N
  .ai-tasks/
  .cursor/orchestrator/orchestrator.py
  .cursor/orchestrator/.venv/bin/python
        |
        | future subprocess run with --control-dir
        v
Orchestrator run
  logs/
  control-dir question/answer files
```

## Components

### Registry Adapter

Discovers managed repos from ai-native-development. Preferred source is `aii-2 registry list --json`; fallback is `<ai-native-development>/.registry/repos.local.json`. The adapter normalizes entries into local models containing at least repo name, path, and manifest path.

### Readiness Checker

Determines whether a managed repo is safe to offer as launchable. Initial readiness checks:

1. Repo path exists.
2. `.ai-deploy-manifest.json` exists.
3. `.cursor/orchestrator/orchestrator.py` exists.
4. `.cursor/orchestrator/.venv/bin/python` exists.
5. `.ai-tasks/index.md` exists.
6. `git status --porcelain` is clean.
7. `aii-2 status <repo>` exits successfully.

Each failure should produce a concise machine-readable reason and a human-readable message.

### Task Inventory

Reads `.ai-tasks/index.md` and individual task frontmatter from managed repos. It should classify tasks by file status (`pending`, `in_progress`, `final_review`, `blocked`) and overlay run state from orch-hub to label active and waiting tasks.

### Run Manager

Future component that launches each target repo's deployed orchestrator as a subprocess:

```text
<repo>/.cursor/orchestrator/.venv/bin/python \
  <repo>/.cursor/orchestrator/orchestrator.py \
  <task-id> \
  --control-dir <run-dir> \
  [flags]
```

The run manager should use `cwd=<repo>` and keep a per-repo mutex or serial queue. Cross-repo parallelism is allowed; same-repo parallelism is not.

### Control Dir Store

Future file-channel adapter for orchestrator questions and answers. It watches for `NNN-question.json`, writes corresponding `NNN-answer.json`, tracks pending questions, and writes `stop.flag` for graceful stop. It must not require web or network logic inside the orchestrator.

### API Service

Future FastAPI service exposing repo, task, run, escalation, log, and health endpoints. The API should call the same registry/readiness/task/run functions used by the CLI.

### Dashboard

Future mobile-first web UI. Main views:

- All repos with readiness and run badges.
- All tasks, filterable by repo and state.
- Repo detail with task filters.
- Task detail showing task file content, run state/logs, and answer input when waiting.

### Notification Adapter

Future notifier interface with Bark as v1 backend. Notification triggers are escalation created, run ended/errored, and close-out done.

## Layers

```text
UI/API/CLI
  calls
Application services: discovery, readiness, task inventory, run control
  call
Adapters: aii-2 command, registry file, git subprocess, orchestrator subprocess, control-dir files
  operate on
Managed repos and ai-native-development
```

Core application services should be independent from FastAPI so CLI and HTTP routes can reuse them.

## Data Flow

### Discovery and Readiness

1. Load config, including ai-native-development path.
2. Try `aii-2 registry list --json`.
3. If command fails or is unavailable, read `<ai-native-development>/.registry/repos.local.json`.
4. For each repo entry, run readiness checks.
5. Return stable JSON for CLI output and future API responses.

### Start Run

1. Client selects repo and task.
2. Hub verifies repo readiness and task existence/status.
3. Hub enforces per-repo single-run rule.
4. Hub creates a run dir.
5. Hub starts the deployed orchestrator subprocess with future `--control-dir`.
6. Hub records pid, repo, task, run dir, start time, and log path.

### Human Answer

1. Orchestrator writes a question file in the control dir.
2. Hub lists it as waiting and notifies the user.
3. User submits an answer through the dashboard or future bot.
4. Hub writes the answer file and records an audit event.
5. Orchestrator consumes the answer and continues.

### Stop

1. User requests stop.
2. Hub writes `stop.flag` for graceful stop at a session boundary.
3. If a hard kill is ever exposed, it must warn that the repo may be dirty and children may be orphaned.

## External Dependencies

- ai-native-development repository and its `aii-2` CLI.
- Managed target repos with deployed `.cursor/orchestrator/` payloads.
- Git CLI for cleanliness checks.
- Python virtual environment inside each target repo's `.cursor/orchestrator/.venv`.
- Future: FastAPI, uvicorn, Bark, launchd, Tailscale.

## Deployment

v1 deployment target is the user's Mac. The service should run as a launchd agent and listen only on a private Tailscale-reachable interface or localhost behind Tailscale Serve. Simple bearer-token auth is acceptable for v1 because tailnet membership is the primary boundary.

## Invariants

- ai-native-development registry is the source of managed repo truth.
- orch-hub never imports orchestrator code.
- The orchestrator remains usable manually when `--control-dir` is unset.
- Same target repo cannot have two active orchestrator runs.
- Human answers are auditable.
- Readiness must fail closed; missing deploy pieces, dirty trees, or failed `aii-2 status` should block launch.
