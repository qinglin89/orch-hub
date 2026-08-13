---
last-updated: 2026-08-14
verified-against: 4e09c50fd0890e76feca291c3dbbcfc6898b022b
---

# Architecture

orch-hub is a local control plane: inspect managed repos, start orchestrator
runs, answer human prompts, read run state, show display-only subscription
capacity, draft tasks via confirmed intake, and build/view read-only
task-evaluation artifacts plus publish complete archived-task reports through a
global feed. It coordinates existing
systems rather than replacing them: ai-native-development remains the
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

The hub never imports deployed orchestrator code: it launches the deployed
script as a subprocess and communicates only through file contracts (task
files, run records, control-dir files, logs).

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

## Data Ownership

| Data | Source Of Truth | Architecture Rule |
|---|---|---|
| Managed repos | ai-native-development registry / `aii-2` | hub reads/normalizes; never invents membership |
| Launch readiness | computed from repo files, git state, `aii-2 status` | fails closed; gates run launch |
| Task ids, summaries, status, blockers | target repo `.ai-tasks/index.md` + task frontmatter | read-only inventory; runtime overlays never replace task status |
| Run records and audit history | `<ORCH_HUB_DATA_DIR>/runs/` | run dirs stay outside target trees, fresh per run; `protocol.json` freezes the validated deployed commit/payload before spawn and records an observable-end stability verdict |
| Intake records and audit history | `<ORCH_HUB_DATA_DIR>/intake/` | unconfirmed drafts are hub-owned, not active tasks; no target-repo writes |
| Evaluation artifacts | `<ORCH_HUB_DATA_DIR>/evaluations/` | sidecar analysis state; never mutates tasks, runs, `.ai/`, `.ai-tasks/` |
| Evaluation report catalog | `<ORCH_HUB_DATA_DIR>/report-catalog/catalog.jsonl` | append-only publication identity/order + hashes/provenance; entries outlive artifacts; consumers decide protocol evolution |
| Agent subscription usage | Codex local rollouts + Claude local utilization cache + optional statusLine figures + Claude stream boundaries + hub `<data_dir>/usage/claude.json` | display-only; cache = primary Claude figure carrier; bounded model-free CLI refresh; statusLine = fallback; never gates |
| Human questions and answers | per-run control-dir files | answer text lives in answer files; history is metadata-only |
| Orchestrator logs | deployed orchestrator log dir, resolved from run records | clients never supply log paths |
| Startup diagnostics | hub-owned run dir capture | scoped to the persisted run record |
| Commit metadata | target repo git history | display/evidence only; never gates |
| Housekeeping marker | target repo `.ai/.housekeeping-pending` | display-only; never gates |
| Notifications | derived from run/question/intake/semantic-job state | best-effort delivery; must not affect state writes |

## Core Flows

Discovery reads the registry, then overlays per-repo readiness; ordinary
readiness failures stay payload data, not route errors.

Run launch: operator-selected repo/task → shared repo admission (no
non-terminal remote intake), per-repo live-run serialization, task
existence/status validation via the task store, readiness verification,
external run dir creation, validated `.ai-deploy-lock.json` capture into the
run dir, then deployed-orchestrator launch with a control dir inside it. An
observable end rechecks the lock; only an unchanged capture is publishable
protocol evidence. Capture failure degrades provenance without creating cohort
admission policy. Cross-repo runs may overlap.

Human input flows through the control dir: the orchestrator writes question
files; the hub lists them, can notify, writes answer files atomically, and
records metadata-only audit events — answer text never enters history.

Stop/completion are observed from run state and file evidence: graceful stop
writes `stop.flag`; end classification combines task-completion, stop, and
process-exit evidence. History is append-only per run; read failures surface
as history data.

Log access resolves a known run record first, then locates recent/streamed
logs from its task/timestamp context; browser streaming uses
bearer-authenticated fetch streams, never query tokens.

Remote intake: shared repo admission first refuses a start while any readable,
repo-attributed persisted unended run holds the repo; otherwise a
natural-language request for a ready repo creates hub-owned intake state.
`/intake-task` runs read-only draft/revise
turns showing the full proposed task file plus index row, and only explicit
confirm runs the write turn. A non-terminal intake blocks run start until it
settles. A legacy/recovered overlap fails closed for revise/confirm while
lifecycle-legal Cancel remains available. `confirmed` requires the created
task to round-trip through frontmatter-authoritative inventory as
`pending`/`blocked`; every other outcome stays outside inventory and rolls back
the paths it was authorized to touch while the process is alive.
`awaiting_review` settlement emits one metadata-only intake-ready notification
off the state-critical path when Bark is configured.

Task evaluation: exact active/archive id → read-only aggregation of every
`(repo_path, task_id)` run with bounded history/session/log/spawn diagnostics,
a producer-bounded best-effort git window, bounded per-session entry task files,
and a producer-bounded `.ai/` eager/prefetch snapshot plus same-window `.ai/`
patches, plus bounded ordered session read behavior from persisted ids and the
configured Claude transcript store, plus each run's persisted deployed-protocol
snapshot → schema-v17 `evidence.json` under the hub data dir. The pack computes
an effective revision only when every contributing run verified the same commit;
legacy, malformed, unverified, or mixed provenance is explicitly unavailable and
never backfilled from current target state. Entry files come from existing log
brackets, live in the run dir, and carry exact/partial/reconstructed fidelity;
snapshot-derived prefetch declarations
supersede accumulated task state only when their frontmatter is complete.
Static metrics adds deterministic run/session, taskfile/session-log, six-axis
score (including prefetch-set read completeness), confidence, and warning
rollups as static-schema-v4 `static_metrics.json` beside it. The
semantic layer builds a model prompt from the persisted evidence/static chain,
including the bounded `.ai/` copy, independently available patch stream, and
ordered read trace; semantic-schema-v2 retrieval effectiveness is distinct and
grounds rediscovery risk. Missing/partial/unobserved parts and raw logs are
recorded as omission or confidence degradation, never poor behavior. It persists
`semantic_report.json` + `report.md` in the same dir and stamps the evidence
schema cohort plus entry fidelity on the report. The default Claude CLI backend runs from an empty
non-repo cwd with tools disabled, safe mode, no session persistence, and
plan-mode writes blocked — the model cannot read live target-repo state outside
the prompt. Protected routes expose availability, generation, async
semantic job start/status, doctor readiness, history, and artifact readback.
`SemanticJobManager` stores one current job per `(repo, task)` under
`<ORCH_HUB_DATA_DIR>/semantic-jobs/`, returns the existing transient job
instead of starting a duplicate model call, and reconciles restart-orphaned
transient jobs to failed. A fresh completed/failed settlement emits one
metadata-only notification through isolated dispatch after persistence;
startup reconciliation stays silent. The dashboard doctor-gates the semantic
trigger, polls persisted job state, and keeps raw artifacts behind
Copy/Download.

Global report publication: a coherent durable L1+L2 evaluation whose task is
archived → semantic-completion hook or deterministic startup reconciliation →
one append-only catalog entry with global `seq`, stable opaque `report_key`,
artifact hashes, evaluator identity, and explicit provenance availability,
including `provenance.protocol.effective_revision` copied from the persisted
evidence aggregate when available.
Reconciliation retries missed/newly eligible reports without a model call; a
shared retention guard protects publishable or undecidable unpublished
artifacts until cataloged. Protected forward-cursor list, by-key contained
bundle, and byte-exact per-artifact routes are read-only. Published entries
remain auditable after retention prunes their artifacts; the hub exposes
integrity/unavailability and leaves interpretation or protocol-evolution
decisions to external consumers.

Subscription usage is a sidecar display flow: `UsageService` reads Claude
Code's whitelisted structured utilization cache first, then merges the optional
statusLine sink, serves the hub-owned observation store, and reads Codex from
recent local rollout `rate_limits`. A stale/empty Claude read dispatches a
background collector; the same request is made once when `RunManager` observes
a live-to-ended transition. The collector is single-flight, freshness/backoff/
timeout bounded, runs `claude -p "/usage"` from an empty cwd, and accepts the
turn only when its JSON envelope reports zero model turns and zero cost. Cache
figures retain the CLI's real `fetchedAtMs` and come only from
`limits[].percent` entries paired to window-map resets by kind and instant.
Claude figures are keyed by store-internal `figure_instant`; reset instants
within five minutes share one jitter-tolerant identity that converges forward,
so fallback writers cannot lower same-window usage while genuine rollovers still
replace old figures. Legacy stores self-heal, and partial merges do not overstate
freshness. The stream-json observer remains a boundary-only fallback on verified
versions. Protected `GET /api/usage` returns both providers fail-soft;
missing/stale/failed sources never feed attention, readiness, run state, or
launch/intake gates.

## Component Boundaries

- Registry discovery performs no readiness checks; readiness launches no
  orchestrators.
- Git metadata and housekeeping marker state are display-only; never gate
  launch.
- Task inventory reads task files but does not decide orchestrator turn state.
- Intake owns the draft/revise/confirm/cancel lifecycle, audit state, and
  read-only-until-confirm contract.
- Repo admission owns only the cross-kind invariant: one per-repo critical
  section admits either a run start or an intake start until the winner's
  persisted record exists. Run/intake managers retain their same-kind gates and
  file-backed occupancy truth; admission is not readiness.
- Evaluation owns evidence assembly/persistence; it reads target tasks/run
  artifacts and writes only hub-owned artifacts.
- Static metrics owns deterministic scoring over evidence packs; no model
  calls, no target/run mutation.
- Semantic evaluation owns model-judged reports over bounded evidence plus
  static metrics; read isolation is enforced in the backend, not by prompt
  alone.
- Semantic jobs own Layer 2 job state, duplicate in-flight prevention,
  failed/retryable diagnostics, and completed `evaluation_id` linkage — not
  evaluation evidence or target state.
- Report catalog owns cross-repo publication identity/order, reconciliation,
  provenance/hash manifests, bundle containment, byte-exact artifact
  verification reads, and publication-debt retention protection—not evaluation
  judgment or protocol-evolution policy.
- Evaluation API/dashboard presentation owns sidecar routing, readback, doctor
  gating, job polling, history comparison; never gates run control or inlines
  raw logs.
- Run control owns subprocess lifecycle, run records, same-repo serialization,
  and live/end classification.
- Control-dir handling owns only file-channel mechanics for questions,
  answers, and stop requests.
- History stores run/answer-audit metadata; it does not decide event timing.
- Logs are located from run records only; callers never pass local paths.
- Notification delivery is isolated from run/intake state persistence.
- Usage collection owns only display capacity data; it may run the Claude CLI's
  local `/usage` command only with per-turn zero-model accounting, but never
  starts a model turn, directly opens provider endpoints/credentials, scrapes
  UI, spends reset credits, or influences readiness/attention/run/intake
  decisions.
- API, CLI, dashboard, launchd, and future bot surfaces stay adapters over
  shared services.

## External Dependencies

- ai-native-development repository and its `aii-2` CLI.
- Managed target repos with deployed `.cursor/orchestrator/` payloads.
- Git CLI for repo state, display metadata, and evaluation commit windows.
- `/usr/bin/caffeinate` on macOS for long-running orchestrator subprocesses.
- Python venv inside each target repo's deployed orchestrator directory.
- FastAPI and uvicorn; launchd for resident operation.
- Optional Bark push endpoint.
- Claude CLI for intake, semantic evaluation, and model-free local `/usage`
  cache refreshes; Claude Code's structured utilization cache is the primary
  figure carrier.
- Claude Code statusLine stdin via legacy `orch-hub-statusline` as an optional
  fallback; 2.1.220 stream `rate_limit_event` as a passive boundary refresh.
- Codex local session rollouts for read-only rate-limit display; Codex is
  never invoked for usage.
- Tailscale or an equivalent private network.

## Invariants

- ai-native-development registry is the source of managed repo truth.
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
