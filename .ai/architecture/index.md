---
last-updated: 2026-07-10
verified-against: 3c614a80ca0d3514395a306a983a40760d6f7150
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
        | subprocess run with --control-dir
        v
Orchestrator run
  logs/
  control-dir question/answer files
```

## Detail Files

| File | Contains |
|---|---|
| components.md | Registry, readiness, task inventory, run manager, history, control-dir, API, launchd, dashboard, notifications |
| data-flow.md | Discovery/readiness, HTTP foundation, task inventory, start/answer/stop/log/run-state/history flows |

## Layers

```text
UI/API/CLI
  calls
Application services: discovery, readiness, task inventory, run control, history
  call
Adapters: aii-2 command, registry file, git subprocess, orchestrator subprocess, control-dir files
  operate on
Managed repos and ai-native-development
```

Core application services are independent from FastAPI so CLI and HTTP routes can reuse them.

## External Dependencies

- ai-native-development repository and its `aii-2` CLI.
- Managed target repos with deployed `.cursor/orchestrator/` payloads.
- Git CLI for cleanliness checks.
- `/usr/bin/caffeinate` on macOS for long-running orchestrator subprocesses.
- Python virtual environment inside each target repo's `.cursor/orchestrator/.venv`.
- FastAPI and uvicorn for the HTTP foundation.
- launchd for resident macOS service operation.
- Bark push endpoint when `ORCH_HUB_BARK_URL` is configured.
- Tailscale service exposure.

## Deployment

v1 deployment target is the user's Mac. The implemented launchd user-agent
template should run `orch-hub-api` from the repo venv, set a nonblank
`ORCH_HUB_TOKEN`, and listen only on localhost behind Tailscale Serve or on a
private Tailscale IP. Simple bearer-token auth is required for all protected
HTTP routes even on the tailnet; tailnet membership remains the primary
boundary. `ORCH_HUB_DATA_DIR` defaults to `~/.orch-hub`; run dirs and
`events.jsonl` history are stored under `<data_dir>/runs/<run_id>/`.
`ORCH_HUB_BARK_URL` is optional launchd environment config; deploy/update can
read it from the environment and preserve an existing installed-plist value.

## Invariants

- ai-native-development registry is the source of managed repo truth.
- orch-hub never imports orchestrator code.
- The orchestrator remains usable manually when `--control-dir` is unset.
- Control-dir adapter code stays file-channel only; API/UI/run-manager code calls it instead of duplicating file writes.
- Same target repo cannot have two active orchestrator runs.
- Run dirs must stay outside target repo working trees and be fresh per run.
- Human answers are auditable, but history must not duplicate answer text.
- Log access is scoped to known run records; clients never supply log paths.
- Readiness must fail closed; missing deploy pieces, dirty trees, or failed `aii-2 status` should block launch.
- Notification delivery is best-effort and must never corrupt run/control-dir
  state; Bark URLs embed device-key secrets and must not be logged.
