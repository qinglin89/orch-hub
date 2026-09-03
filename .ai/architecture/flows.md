---
last-updated: 2026-09-03
verified-against: 4d6e1a3b7c31a073417b9b681d5a009e288b71cf
---

# Architecture: Core Flows

End-to-end runtime flows of the hub. Shape, layers, trust boundaries, and
invariants: `index.md`. Data ownership and component seams: `boundaries.md`.

## Core Flows

Discovery reads the registry, then overlays per-repo readiness; ordinary
readiness failures stay payload data, not route errors.

Run launch: operator-selected repo/task → shared repo admission (no
non-terminal remote intake), per-repo live-run serialization, task
existence/status validation via the task store, readiness verification,
external run dir creation, validated `.ai-deploy-lock.json` capture into the
run dir, then deployed-orchestrator launch with a control dir inside it. Launch
readiness has compared actual target files with the deploy manifest; an observed
end rechecks the lock and bounded complete lock/manifest target set against the
actual files. Only the unchanged identity pair with a matching end payload is
publishable. These are two point observations, not continuous monitoring;
capture/check failure degrades provenance without creating cohort admission
policy. Cross-repo runs may overlap.

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
snapshot → schema-v18 `evidence.json` under the hub data dir. The pack publishes
one effective protocol identity only when every contributing run verified the
same `(source_git_commit, canonical_payload_sha256)` pair; legacy, malformed,
unverified, revision-mixed, or payload-mixed provenance is explicitly
unavailable and never backfilled from current target state. Entry files come from existing log
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
schema cohort plus entry fidelity on the report. The default Claude CLI backend
runs from an empty non-repo cwd with tools disabled, safe mode, no session
persistence, plan-mode writes blocked, and the configured turn deadline — the
model cannot read live target-repo state outside the prompt or hold a worker
past the attempt's enforced bound. Protected routes expose availability,
generation, async semantic job start/status/cancel, cross-repo attempt state,
doctor readiness, history, and artifact readback.
The evaluations base may be reached through links for relocated storage, but
creation, listing, artifact reads/writes, cleanup, and retention all open every
repo/task/evaluation segment beneath it no-follow and act relative to contained
descriptors; linked interior state is damage, never a source/target of bytes or
an alias for ordinary pruning.
After a run's terminal session/task/protocol snapshots settle, a completed
classification invokes an isolated sidecar trigger. With evaluation and its
automatic gate enabled, it submits a fresh free Layer 1 build off the run-manager
lock. The trigger holds no semantic-job manager or dispatcher, writes no job
record, and promises no durable retry: a stopped worker loses a reproducible
artifact that the next completion or manual metrics request regenerates.
`SemanticJobManager.start_job` is the only production Layer 2 start path and
keeps one current record per `(repo, task)` under
`<ORCH_HUB_DATA_DIR>/semantic-jobs/`; startup fails orphaned transients, while
the resident monitor reaps only records past the persisted deadline their
workers are also held to. A cancellable attempt owns a collision-resistant id
and live process handle until terminal state is durable; cancel kills or
withholds uncommitted work, while a sealed/settling result refuses a false
success. Legacy `deferred`/`skipped` records are never promoted; an explicit
start supersedes them. Protected queue readback is a read-only uncapped
latest-attempt census plus capped actionable-first rows, with inert holds last;
`orch-hub eval queue` reads the same projection directly from storage. The
dashboard offers per-job cancel beside that state and keeps raw artifacts behind
Copy/Download.

Global report publication: a coherent durable L1+L2 evaluation whose task is
archived → semantic-completion hook or deterministic startup reconciliation →
one append-only catalog entry with global `seq`, stable opaque `report_key`,
artifact hashes, evaluator identity, and explicit provenance availability,
including the atomic `provenance.protocol.effective_revision` plus
`deploy_lock_hash` copied from the persisted evidence aggregate when available;
older revision-only packs/catalog lines read back unavailable rather than being
completed from later state.
Reconciliation retries missed/newly eligible reports without a model call; a
shared retention guard protects publishable or undecidable unpublished
artifacts until cataloged. Candidate publication, by-key bundles, and byte-exact
per-artifact reads share the evaluation tree's descriptor walk and refuse every
interior linked directory/file hop. Protected forward-cursor list and artifact
routes are read-only. Published entries
remain auditable after retention prunes their artifacts; the hub exposes
integrity/unavailability and leaves interpretation or protocol-evolution
decisions to external consumers.
The local operator CLI audits catalog parsing/identity ambiguity and each
entry's contained artifact state without evaluating or writing. Its only repair
rewrites provably redundant duplicate lines or an unparseable torn tail after
explicit confirmation, under the publication lock and after preserving the
original bytes in a collision-safe backup; distinct identity conflicts refuse.

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
missing/stale/failed sources never feed attention, readiness, run state,
launch/intake gates, or evaluation decisions.

Release production: a pinned standalone ARM64 CPython plus the project sources
and resources enter a PyInstaller onedir build. The producer audits the
executable CArchive, nested compressed PYZ content, collected files, Mach-O
deployment floors, and operator smoke behavior before atomically replacing the
canonical build. Packaging revalidates that build, writes a deterministic
self-extracting `.run`, `release.json`, and `SHA256SUMS`, then replaces the
canonical release directory only after every staged check passes. Publication
requires a verified Git tag, rechecks staged bytes, writes `publication.json`
to close the checksum-set identity, and creates one tag destination without
overwriting an existing publication.

Release install: the shell preamble checks ARM64 macOS and the manifest's
minimum version, verifies the embedded archive digest, then extracts. The
Python installer validates the cross-language manifest and payload, stages a
versioned runtime below the selected prefix, and updates `current`, `previous`,
and the command link after validation. Setup renders launchd from either a
checkout program or the compiled `current/orch-hub serve` program while
preserving installed host, port, data, Mandrel, token, and Bark values when not
overridden. Uninstall stops the fixed service and removes only a prefix whose
ownership marker is valid and bound to its payload.
