---
last-updated: 2026-09-05
verified-against: a283a610b718babbc8c0752ffbe81e2429bd61f2
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
The service can run from a checkout or from a versioned compiled installation;
release construction and publication remain operator-side producer flows.

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

Compiled delivery is an unsigned, self-checking artifact rather than a code
signature boundary. The installer validates host architecture, the measured
macOS floor, and payload digests before extraction. The build audit rejects
source-bearing first-party payloads and repository, protocol, task, agent,
secret, and absolute-path leakage, including content nested in freezer
archives.

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
- Every hub-observed completed task mechanically produces Layer 1; completion
  never creates, skips, enqueues, or starts a Layer 2 job.
- Layer 2 starts only through an explicit per-task request. Its turns and
  resident reaping share one persisted whole-attempt deadline; per-job cancel
  cannot report success after result settlement has begun.
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
- Subscription usage is display-only, never a launch, health, or evaluation
  gate. Unavailable/stale data is shown as such, never as zero.
- Claude usage requires no statusLine setup. A legacy statusLine hook remains
  operator-installed; orch-hub never edits `~/.claude/**`, and the wrapper
  preserves the operator's visible statusline behavior.
- Compiled installs are versioned under one user-owned prefix; `current` and
  `previous` move only after a staged candidate validates, while mutable data
  stays outside the prefix and uninstall requires a payload-bound ownership
  marker.
- Release build and package outputs keep the last known-good canonical bytes
  until a staged replacement passes. Publication is immutable per tag and
  uses create-only placement, not the replace-oriented atomic-directory helper.
- A publishable clean build settles its source before slow work by exporting
  the exact commit it records in `runtime.json`; version, dependency
  declarations, and freezer inputs come from that immutable tree. Dirty or
  unidentifiable trees remain buildable but record an unpublishable source.
- A release tag gates publication of locally built bytes. Publication records
  checksums and closure metadata and requires every target payload's recorded
  source revision to equal the tag commit.

## Related Memory

Related: `features/index.md` (capabilities), `modules/index.md` (boundaries),
`apis/index.md` (CLI/HTTP/internal contracts), `design/index.md` (rationale,
tradeoffs, patterns).
