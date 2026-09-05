---
last-updated: 2026-09-05
verified-against: a283a610b718babbc8c0752ffbe81e2429bd61f2
---

# Design: Tradeoffs

## Tradeoff: mandrel registry vs orch-hub registry

- Chose: mandrel registry.
- Reason: deployment tooling already tracks managed repos locally, and duplicate registries would drift.
- Consequence: orch-hub startup depends on the mandrel path or executable
  availability.

## Tradeoff: subprocess boundary vs importing orchestrator code

- Chose: subprocess boundary.
- Reason: the orchestrator is a CLI state machine with blocking loops and `sys.exit` paths; importing would couple the hub to internals and risk process-level side effects.
- Consequence: status is inferred through task files, control-dir files, process
  state, and run records rather than direct orchestrator internals.

## Tradeoff: file-backed run records vs in-memory state only

- Chose: per-run `run.json` under `<data_dir>/runs/<run_id>/`.
- Reason: the hub is a resident process that may restart while orchestrator
  subprocesses keep running; persisted records let a fresh manager reattach by
  scanning run dirs and probing pid liveness.
- Consequence: ends observed while the hub was down have unknown exit code, and
  pid reuse after reboot can temporarily make a stale run look active until the
  dir is cleaned.

## Tradeoff: write `run.json` after spawn vs before spawn

- Chose: write after spawn succeeds.
- Reason: a crash before spawn would otherwise leave a false live run blocking
  the repo forever.
- Consequence: a hub crash in the tiny post-spawn/pre-write window leaves an
  unattributed broken run dir that surfaces as `error` but cannot block repo
  launches.

## Tradeoff: per-run JSONL history vs SQLite

- Chose: append-only `events.jsonl` in each existing run dir.
- Reason: v1 history is local, per-run, and naturally colocated with
  `run.json`; JSONL keeps the storage inspectable and avoids a second
  database/retention substrate before dashboard needs prove it out.
- Consequence: recent-run history scans run directories and history errors are
  surfaced as synthetic events; retention remains a run-dir policy question.

## Tradeoff: pre-spawn `started_ts` vs post-spawn timestamp

- Chose: capture `RunRecord.started_ts` just before spawning.
- Reason: the deployed orchestrator chooses its second-precision log filename
  during child startup; a post-spawn parent timestamp can land one second after
  the child's log stamp and make the run's real log look pre-start. Pre-spawn
  `started_ts`, floored to the second, is a sound lower bound for log
  attribution.
- Consequence: log location can reject earlier fast re-run logs without a
  pre-start skew. Same-wall-clock-second re-runs of one task remain
  indistinguishable unless the orchestrator adds finer stamps or run markers.

## Tradeoff: control-dir files vs HTTP callbacks inside orchestrator

- Chose: control-dir files.
- Reason: keeps orchestrator local, testable, and free of web/network dependencies.
- Consequence: hub must poll or watch files and handle stale files robustly.

## Tradeoff: Tailscale plus bearer token vs full auth system

- Chose: Tailscale plus simple bearer token for v1.
- Reason: the service controls arbitrary local code execution, so public exposure is out of scope; tailnet membership is the main security boundary.
- Consequence: multi-user permissions and public OAuth flows are deferred.

## Tradeoff: bearer-header fetch streams vs query-token SSE

- Chose: browser log tailing uses `fetch` plus a `ReadableStream` reader with
  the normal `Authorization: Bearer ...` header.
- Reason: `EventSource` cannot set authorization headers, and query tokens
  would add a second credential path plus put secrets in URLs/logs.
- Consequence: dashboard log streaming needs a fetch-stream client helper; the
  API auth surface stays single-path and tokens stay out of URLs.

## Tradeoff: public dashboard shell vs cookie/token exchange

- Chose: explicitly public shell assets (`/`, `/app.js`, `/styles.css`) plus
  browser `localStorage` for the bearer token.
- Reason: a browser cannot attach `Authorization` on plain navigation, while a
  cookie or query-token exchange would add a second credential path. The shell
  is static client code with no embedded secret; all data routes still require
  the normal bearer header.
- Consequence: the public allowlist must stay tiny and explicit, and the SPA
  owns token entry, storage, sign-out, and 401 re-login behavior.

## Tradeoff: pure-ASGI auth middleware vs `BaseHTTPMiddleware`

- Chose: pure ASGI bearer middleware.
- Reason: Starlette `BaseHTTPMiddleware` can wrap streaming responses in anyio
  streams that buffer body chunks and delay client-disconnect propagation,
  which is a bad fit for long-lived SSE log tails.
- Consequence: auth policy stays default-deny and behavior-compatible, but the
  middleware sits at the ASGI layer so SSE can pass through incrementally.

## Tradeoff: launchd environment config vs service CLI flags

- Chose: configure the resident service entirely through environment variables.
- Reason: launchd user agents naturally carry configuration in
  `EnvironmentVariables`; the checkout's `orch-hub-api` console script remains
  argument-free and the compiled program needs only `serve`.
- Consequence: malformed env values must fail explicitly or be reported as
  operational CLI errors; the plist embeds `ORCH_HUB_TOKEN` and must be kept
  permission-restricted.

## Tradeoff: launchd placeholder tolerance vs self re-exec

- Chose: keep render-before-install ordering and tolerate unknown optional
  `REPLACE-...` placeholders by rendering them as `""`.
- Reason: `orch-hub launchd update` runs the old installed CLI; installing the
  package first would not reload already-imported Python modules, while a true
  re-exec adds recursion and token-precedence complexity.
- Consequence: new optional launchd template fields are forward-compatible when
  empty means disabled; new required fields are a breaking change that requires
  updating the installed CLI first.

## Tradeoff: best-effort notifier boundary vs notification-owned state

- Chose: the domain manager that observes the event owns trigger timing and
  isolates delivery through `_emit`.
- Reason: notification failure must not affect `run.json`, intake records,
  `events.jsonl`, or the control-dir channel; run/end/question classification
  belongs in `RunManager`, while draft/revision readiness belongs in
  `IntakeManager`.
- Consequence: `BarkNotifier` may raise for direct callers, but production
  manager delivery runs off the lock through an injected dispatch and logs only
  exception type plus non-secret notification title.

## Tradeoff: in-memory question dedup vs persisted notification ledger

- Chose: in-memory `(run_id, seq)` dedup for pending-question pushes.
- Reason: the hub only needs to suppress duplicate pushes during normal status
  rescans; persisting a notification ledger would add storage and cleanup for a
  small per-process reminder mechanism.
- Consequence: a hub restart re-notifies a still-open question once; an answered
  question is no longer pending, so it never re-fires.

## Tradeoff: off-lock intake turns vs synchronous request turns

- Chose: persist a transient intake status under the manager lock, return that
  record immediately, and run the slow draft/revise/confirm agent turn through
  an injected executor outside the lock.
- Reason: remote intake turns can take long enough to freeze unrelated reads and
  dashboard pages if they run under the request/manager lock; submitting outside
  the non-reentrant lock also lets synchronous test executors re-acquire the
  lock during finalize without deadlocking.
- Consequence: clients must poll `GET /api/intakes/{id}` until the record
  settles; finalizers re-load and settle only the expected transient status so
  cancel wins over draft/revise completion, and startup `reconcile_orphans()`
  marks abandoned transient records `failed` without target-repo mutation.

## Tradeoff: file-backed repo admission vs in-memory reservations

- Chose: one per-repo admission section that asks the run/intake managers'
  read-only persisted-record probes and stays held until the winner writes its
  record.
- Reason: a separate reservation table would require restart reconstruction
  and could diverge from `run.json` / `intake.json`. File-backed occupancy also
  makes a persisted unended run fail closed until the normal refresh transition
  writes all end snapshots and notifications.
- Consequence: probes take no manager lock, preserving repo-section → own-lock
  ordering and cross-repo concurrency; a just-ended run may over-block until
  refresh, but cannot transiently admit intake against an unended record.

## Tradeoff: shared core before dashboard

- Chose: CLI/shared-core slices before dashboard, then a thin FastAPI
  foundation over the same functions.
- Reason: this validates external integration and task parsing before adding UI
  complexity, while keeping HTTP behavior aligned with the tested CLI/core
  payloads.
- Consequence: the dashboard can consume stable repo and task APIs; run,
  question, log, and auth behavior were added as separate slices, and the v1
  dashboard now uses those APIs without adding frontend build tooling.

## Tradeoff: PyInstaller onedir vs onefile or a native compiler

- Chose: one ARM64 PyInstaller onedir payload wrapped by a self-extracting
  installer.
- Reason: it keeps one operator-facing artifact while making the runtime tree
  auditable and avoiding onefile's startup extraction and native-compiler
  compatibility work.
- Consequence: the installed version is a directory, and the release producer
  must inspect both the executable archive and nested PYZ content for excluded
  source and repository state.

## Tradeoff: versioned prefix vs in-place executable replacement

- Chose: immutable `<prefix>/versions/<version>` payloads with `current` and
  `previous` links.
- Reason: a staged candidate can validate before activation and rollback keeps
  a concrete known-good version.
- Consequence: later installs must use the same nondefault prefix/bin directory,
  and version retention is a separate policy task.

## Tradeoff: pinned standalone CPython vs the host Python

- Chose: a digest-pinned python-build-standalone CPython and a measured Mach-O
  deployment floor.
- Reason: release bytes should not depend on a workstation's mutable Python or
  claim broader macOS compatibility than their native objects support.
- Consequence: producer pins and floor validation are release maintenance; the
  installed runtime has no host-Python dependency.

## Tradeoff: local build with tag-gated publish vs CI-origin artifacts

- Chose: build and package locally, then require a verified Git tag before
  filesystem or GitHub publication.
- Reason: it delivers the private release workflow without first creating a CI
  signing and artifact pipeline.
- Consequence: a clean build exports the commit before freezing and records it
  in the payload; publication rejects dirty, unknown, or non-tag revisions.
  Local builds from modified trees or source exports remain possible but are
  deliberately unpublishable.

## Tradeoff: replace staging vs immutable tag publication

- Chose: atomic last-good replacement for build/package outputs and create-only
  placement for a published tag.
- Reason: canonical producer outputs may advance after validation, while an
  existing tag publication must never be silently replaced.
- Consequence: the shared atomic-directory helper is intentionally unsuitable
  for the publication destination.
