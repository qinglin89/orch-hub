---
last-updated: 2026-07-19
verified-against: 60c0b31dfa8f74dd43f6daa68f46e54281d4799c
---

# Architecture

orch-hub is a local control plane for remotely inspecting managed repos,
starting deployed orchestrator runs, answering human-input prompts, reading run
state, and drafting new tasks through a confirmed intake loop. It coordinates
existing systems rather than replacing them:
ai-native-development remains the managed-repo registry, target repos own their
task files and deployed orchestrator payloads, and orch-hub owns only its
service surface plus external run state.

## System Shape

```text
Mobile browser / remote client
        |
        | private network + bearer token
        v
orch-hub service on the user's Mac
        |
        | discovers repos and checks readiness
        v
ai-native-development / aii-2 registry
        |
        | points at
        v
Target repo N
  .ai-tasks/
  .cursor/orchestrator/orchestrator.py
  .cursor/orchestrator/.venv/bin/python
        |
        | detached subprocess with --control-dir
        v
Orchestrator run
  hub-owned run dir
  control-dir question/answer files
  orchestrator logs
```

The hub never imports deployed orchestrator code. It starts the deployed script
as a subprocess and communicates through file contracts: task files in the
target repo, hub-owned run records, control-dir files, and log files.

## Layers

```text
UI / API / CLI
  call
Application services: discovery, readiness, task inventory, run control, history
  call
Adapters: aii-2 command, registry file, git subprocess, orchestrator subprocess,
          control-dir files, launchd, notification backend
  operate on
Managed repos, local service state, and external operator tools
```

Application services are independent from FastAPI so CLI, HTTP, dashboard, and
future bot surfaces can share the same behavior. API and UI layers adapt those
services; they should not duplicate registry parsing, readiness checks,
control-dir writes, or log path resolution.

## Trust And Deployment Boundaries

v1 runs as a macOS user service on the user's Mac. Private exposure is expected
to stay behind Tailscale or another private network boundary. Bearer-token auth
is still required for every protected HTTP route because tailnet membership is
not treated as sufficient application authorization.

The dashboard shell can be public because ordinary browser navigation cannot
attach an `Authorization` header and the shell assets contain no secrets. Every
data route, documentation route, unknown route, and future protected route must
fail closed without a configured valid token.

launchd owns resident service operation. It should manage only the fixed
orch-hub service label; graceful run stop semantics stay in the run-control and
control-dir path. Secrets embedded in service configuration, especially bearer
tokens and Bark endpoints, must not be logged or displayed.

## Data Ownership

| Data | Source Of Truth | Architecture Rule |
|---|---|---|
| Managed repos | ai-native-development registry / `aii-2` | orch-hub reads and normalizes; it does not invent repo membership |
| Launch readiness | Computed from target repo files, git state, and `aii-2 status` | readiness fails closed and gates run launch |
| Task ids, summaries, status, blockers | Target repo `.ai-tasks/index.md` plus task frontmatter | task inventory is read-only; runtime overlays do not replace task status |
| Run records and audit history | Hub data dir under `<ORCH_HUB_DATA_DIR>/runs/` | run dirs stay outside target repo working trees and are fresh per run |
| Intake records and audit history | Hub data dir under `<ORCH_HUB_DATA_DIR>/intake/` | unconfirmed drafts are hub-owned state, not active tasks, and do not modify target repos |
| Human questions and answers | Per-run control dir files | answer text belongs in answer files; history stores metadata only |
| Orchestrator logs | Deployed orchestrator log directory, resolved from run records | clients never supply log paths |
| Startup diagnostics | Hub-owned run dir capture | diagnostic output is scoped to the persisted run record |
| Commit metadata | Target repo git history | display-only; never gates readiness or launch |
| Housekeeping marker | Target repo `.ai/.housekeeping-pending` | display-only dashboard/API warning; never gates readiness or launch |
| Notifications | Derived from run state, pending questions, and intake draft/revision state | delivery is best-effort and must not affect state writes |

## Core Flows

Discovery starts from the managed-repo registry, then overlays readiness for
each repo. Readiness results are returned as data for operators and dashboards;
ordinary readiness failures stay in payloads rather than becoming route-level
transport errors.

Run launch starts from an operator-selected repo and task. The hub serializes
live runs per target repo, validates task existence and task status from the
task store, verifies readiness, creates an external run dir, then starts the
deployed orchestrator with a control dir in that run dir. Cross-repo runs may
overlap.

Human input flows through the control dir. The orchestrator writes question
files; the hub lists pending questions, can notify the user, writes answer
files atomically, and records metadata-only audit events. The hub does not copy
answer text into history.

Stop and completion are observed through run state and file evidence. A
graceful stop request writes `stop.flag`; end classification uses task
completion evidence, stop evidence, and process exit information. History is
append-only per run and read failures are surfaced as history data rather than
silently hidden.

Log access is scoped through known run records. The API resolves a run first,
then locates recent or streamed logs from that record's task and timestamp
context. Browser streaming uses bearer-authenticated fetch streams instead of a
credential-bearing query string.

Remote intake starts from a natural-language request for a ready repo. The hub
persists hub-owned intake state, drives a conversational `/intake-task` session
through draft/revise turns that are treated as read-only proposals, and shows the
full proposed task file plus index row for human review. Only explicit confirm
calls the agent write turn. The manager records `confirmed` only after the
created task round-trips through the frontmatter-authoritative task inventory as
`pending` or `blocked`; failed, cancelled, unverified, or interrupted intakes
stay outside active task inventory and roll back the task file/index paths they
were authorized to touch when the process is still alive. When a draft or
revision settles to `awaiting_review`, the manager emits one metadata-only
intake-ready notification off the state-critical path when Bark is configured.

## Component Boundaries

- Registry discovery does not perform readiness checks.
- Readiness does not launch orchestrators.
- Git metadata is display-only and never gates launch.
- Housekeeping marker state is display-only and never gates launch.
- Task inventory reads task files but does not decide orchestrator turn state.
- Intake owns draft/revise/confirm/cancel lifecycle, intake audit state, and the
  read-only-until-confirm safety contract.
- Run control owns subprocess lifecycle, run records, same-repo serialization,
  and live/end-state classification.
- Control-dir handling owns only the file-channel mechanics for questions,
  answers, and stop requests.
- History stores run and answer-audit metadata; it does not decide event timing.
- Logs are located from run records only; API and UI callers never pass local
  paths.
- Notification delivery is isolated from run/intake state persistence.
- API, CLI, dashboard, launchd, and future bot surfaces stay as adapters over
  shared application services.

## External Dependencies

- ai-native-development repository and its `aii-2` CLI.
- Managed target repos with deployed `.cursor/orchestrator/` payloads.
- Git CLI for target-repo state and display metadata.
- `/usr/bin/caffeinate` on macOS for long-running orchestrator subprocesses.
- Python virtual environment inside each target repo's deployed orchestrator
  directory.
- FastAPI and uvicorn for the HTTP service.
- launchd for resident macOS service operation.
- Optional Bark push endpoint when configured.
- Claude CLI for the concrete remote intake backend.
- Tailscale or an equivalent private network for remote access.

## Invariants

- ai-native-development registry is the source of managed repo truth.
- orch-hub never imports orchestrator code.
- The orchestrator remains usable manually when `--control-dir` is unset.
- Control-dir adapter code stays file-channel only.
- The same target repo cannot have two active orchestrator runs.
- Hub-owned run dirs stay outside target repo working trees.
- Hub-owned unconfirmed intake drafts are not tasks and cannot be launched.
- A confirmed intake must land through task inventory as `pending` or `blocked`;
  every other confirm outcome is failed/unverified.
- Human answers are auditable, but history must not duplicate answer text.
- Log access is scoped to known run records.
- Readiness must fail closed before launch.
- Notification delivery must never corrupt run, intake, history, or control-dir
  state.
- Bark URLs embed device-key secrets and must not be logged.

## Related Memory

- `features/index.md` owns user-facing capabilities and implemented behavior.
- `modules/index.md` owns code-module responsibilities and boundaries.
- `apis/index.md` owns CLI, HTTP, and internal API contracts.
- `design/index.md` owns rationale, tradeoffs, and architectural patterns.
