---
last-updated: 2026-07-09
verified-against: 98194a7b5c814f85807bb897298db131bc593900
---

# Project Overview

## Purpose

orch-hub is intended to be a long-lived local control plane for AI coding orchestrators. It will run on the user's Mac and allow remote control from iPhone, iPad, or another machine over a private network. The hub coordinates existing per-repository orchestrator programs that drive dev/review loops over `.ai-tasks/`.

The implemented slices are discovery/readiness, task inventory, the FastAPI
HTTP foundation, fail-closed bearer-token auth, environment-configured service
binding, launchd service packaging/docs, the control-dir file-channel adapter,
the run manager, run/escalation HTTP routes, log viewing, run history/audit,
the mobile dashboard, and Bark notifications: read the ai-native-development
local registry, list managed repos, report launch readiness, parse each repo's
`.ai-tasks/` index plus task frontmatter, expose those payloads through HTTP,
require `ORCH_HUB_TOKEN` on every non-health HTTP path, provide a launchd
user-agent template for resident macOS operation, read/write orchestrator
question, answer, and stop files, launch/list/stop/reattach orchestrator
subprocess runs with per-repo serialization, answer waiting questions remotely,
retrieve or stream run logs, persist run lifecycle plus answer-audit metadata,
serve the dashboard, and push Bark notifications for pending questions and
terminal run states. Later slices add remote intake-task and IM bot control.

## Users

- Primary user: qinglin, operating local AI coding workflows from mobile devices or another computer.
- Remote clients: mobile-first web dashboard on iPhone/iPad; later possible IM bot surfaces.
- Local service operator: launchd on the Mac, keeping the hub resident.

## Scope

- Discover managed repos from ai-native-development, not from GitHub and not from a separate orch-hub registry.
- Inspect each managed repo for orchestrator launch readiness.
- List active, waiting, pending, and blocked tasks per repo.
- Launch and supervise one orchestrator run per repo at a time, each run attached to one `.ai-tasks/<task-id>.md` task.
- Surface orchestrator human input requests remotely, collect answers, and deliver them back through the implemented control-dir file channel.
- Stream or tail orchestrator logs to the dashboard.
- Persist run lifecycle, stop request, and answer-audit metadata for later inspection without duplicating human answer text into history.
- Push mobile notifications for pending questions and terminal run states via
  Bark when `ORCH_HUB_BARK_URL` is configured.
- Provide simple remote auth suitable for a Tailscale-only v1 deployment.
- Provide a mobile-first dashboard for repo, task, run, question, and log views.
- Support launchd-based long-lived operation on macOS.

## Non-Goals

- Do not treat GitHub as the registry source of truth.
- Do not maintain a separate orch-hub repository registry.
- Do not import `.cursor/orchestrator/orchestrator.py`; launch it as a subprocess.
- Do not add web, network, or orch-hub imports into the orchestrator itself.
- Do not make the service public on the internet.
- Do not implement watchdogs or hang/log-silence detection.
- Do not run more than one orchestrator at a time in the same target repo.
- Do not hand-edit deployed protocol/orchestrator copies in target repos; canonical changes belong in ai-native-development.

## Tech Stack

- Language: Python.
- First implementation: package plus CLI, using pytest tests and stdlib subprocess/path/json primitives where practical.
- HTTP service: FastAPI and uvicorn, with a localhost-default entry point,
  environment-configured host/port, and fail-closed bearer-token middleware.
- UI: mobile-first web dashboard served by the hub.
- Local deployment: launchd user-agent template and docs on macOS.
- Network boundary: Tailscale for private connectivity, with a simple bearer token in v1.
- Notifications: Bark is the implemented v1 push path; IM bots are later.
- External command source: `aii-2` from ai-native-development.

## Repository Structure

The repository contains the first product package and tests:

```text
orch_hub/
  __init__.py
  api.py
  cli.py
  config.py
  history.py
  models.py
  registry.py
  readiness.py
  runs.py
  logs.py
  notifications.py
  subprocess_utils.py
  task_store.py
  control_dir.py
  web/
tests/
  test_api.py
  test_cli.py
  test_config.py
  test_control_dir.py
  test_history.py
  test_logs.py
  test_registry.py
  test_readiness.py
  test_runs.py
  test_task_store.py
deploy/
  launchd/
    com.qinglin89.orch-hub.plist
docs/
  launchd.md
```

## External Project Facts

- ai-native-development root: `/Users/linqing/workplace/github.com/qinglin89/ai-native-development`.
- Default registry source should be `aii-2 registry list --json`; fallback is `<ai-native-development>/.registry/repos.local.json`.
- The canonical orchestrator lives at `ai-native-development/canonical/orchestrator/orchestrator.py` and is deployed into target repos at `.cursor/orchestrator/orchestrator.py`.
- The inspected orchestrator is a local CLI scheduler. It runs a dev/review loop over one `.ai-tasks/` task, selects dev or review turns from task file status and session log, pauses for human input through `ask_human`, and uses stdin unless launched with `--control-dir`.
