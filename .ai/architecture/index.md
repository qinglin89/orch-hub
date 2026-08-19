---
last-updated: 2026-08-19
verified-against: c7fbcf08b005bfe0a06f6a3614923646a51e3595
---

# Architecture

orch-hub is a local control plane: inspect managed repos, start orchestrator
runs, answer human prompts, read run state, show display-only subscription
capacity, draft tasks via confirmed intake, and build/view read-only
task-evaluation artifacts plus publish complete archived-task reports through a
global feed. It coordinates existing
systems rather than replacing them: mandrel remains the
registry, target repos own their task files and deployed orchestrator
payloads, and orch-hub owns only its service surface plus external run state.

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
mandrel registry / `bin/mandrel`
        |
        | points at
        v
Target repo N
  .ai-tasks/
  .mandrel/orchestrator/orchestrator.py
  .mandrel/orchestrator/.venv/bin/python
        |
        | detached subprocess with --control-dir
        v
Orchestrator run
  hub-owned run dir
  control-dir question/answer files
  orchestrator logs
```

The hub never imports deployed orchestrator code: it launches the deployed
script as a subprocess and communicates only through file contracts (task
files, run records, control-dir files, logs).

## Layers

```text
UI / API / CLI
  call
Application services: discovery, readiness, task inventory, run control, history
  call
Adapters: mandrel command, registry file, git subprocess, orchestrator subprocess,
          control-dir files, launchd, notification backend
  operate on
Managed repos, local service state, and external operator tools
```

Application services are FastAPI-independent so CLI, HTTP, dashboard, and
future bot surfaces share behavior; API/UI adapters must not duplicate
registry parsing, readiness checks, control-dir writes, or log path
resolution.

## Trust And Deployment Boundaries

v1 runs as a macOS user service behind Tailscale or an equivalent private
boundary. Bearer auth is still required on every protected route — tailnet
membership is not application authorization. The dashboard shell may be public
(browser navigation cannot attach `Authorization`; shell assets hold no
secrets); every other route fails closed without a valid token. launchd owns
resident operation and manages only the fixed orch-hub service label; graceful
run stop stays in the run-control/control-dir path. Configured secrets (bearer
tokens, Bark endpoints) must not be logged or displayed.

## Detail Files

| File | Contains |
|---|---|
| flows.md | Core flows: discovery/readiness, run launch + protocol capture, control-dir human input, stop/completion, log access, remote intake, task evaluation, report publication, subscription usage |
| boundaries.md | Data ownership (source-of-truth table + rules), component boundaries, external dependencies |

## Invariants

- Mandrel's registry is the source of managed repo truth.
- orch-hub never imports orchestrator code.
- The orchestrator remains usable manually when `--control-dir` is unset.
- Control-dir adapter code stays file-channel only.
- The same target repo cannot have two active orchestrator runs.
- Hub-owned run dirs stay outside target repo working trees.
- Hub-owned evaluation artifacts stay outside target trees and do not mutate
  task/run/protocol state.
- Global report entries are append-only and uniquely ordered; retained or pruned
  artifact state is reported explicitly, and publication never decides protocol
  evolution.
- Hub-owned unconfirmed intake drafts are not tasks and cannot be launched.
- A persisted unended run and a non-terminal hub-driven intake must not coexist
  for one repo; admission conflicts are structured 409s, not readiness
  failures.
- A confirmed intake must land through task inventory as `pending`/`blocked`;
  every other confirm outcome is failed/unverified.
- Human answers are auditable, but history must not duplicate answer text.
- Log access is scoped to known run records.
- Readiness must fail closed before launch.
- Notification delivery must never corrupt run, intake, history, or
  control-dir state.
- Bark URLs embed device-key secrets and must not be logged.
- Subscription usage is display-only: unavailable/stale data is shown as such,
  never as zero usage, never as a launch or health gate.
- Claude usage requires no statusLine setup. A legacy statusLine hook remains
  operator-installed; orch-hub never edits `~/.claude/**`, and the wrapper
  preserves the operator's visible statusline behavior.

## Related Memory

Related: `features/index.md` (capabilities), `modules/index.md` (boundaries),
`apis/index.md` (CLI/HTTP/internal contracts), `design/index.md` (rationale,
tradeoffs, patterns).
