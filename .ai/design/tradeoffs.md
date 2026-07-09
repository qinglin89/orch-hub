---
last-updated: 2026-07-10
verified-against: 30c1e08a6e706cab2d922417cf0024aa2920c5f7
---

# Design: Tradeoffs

## Tradeoff: ai-native-development registry vs orch-hub registry

- Chose: ai-native-development registry.
- Reason: deployment tooling already tracks managed repos locally, and duplicate registries would drift.
- Consequence: orch-hub startup depends on ai-native-development path or `aii-2` availability.

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
  `EnvironmentVariables`, and the installed `orch-hub-api` console script can
  remain argument-free.
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

## Tradeoff: best-effort notifier boundary vs notification-owned run state

- Chose: `RunManager` owns trigger timing and isolates delivery through `_emit`.
- Reason: notification failure must not affect `run.json`, `events.jsonl`, or
  the control-dir channel, and run/end/question classification already belongs
  in the run manager.
- Consequence: `BarkNotifier` may raise for direct callers, but production
  delivery runs off the lock through an injected dispatch and logs only
  exception type plus non-secret notification title.

## Tradeoff: in-memory question dedup vs persisted notification ledger

- Chose: in-memory `(run_id, seq)` dedup for pending-question pushes.
- Reason: the hub only needs to suppress duplicate pushes during normal status
  rescans; persisting a notification ledger would add storage and cleanup for a
  small per-process reminder mechanism.
- Consequence: a hub restart re-notifies a still-open question once; an answered
  question is no longer pending, so it never re-fires.

## Tradeoff: shared core before dashboard

- Chose: CLI/shared-core slices before dashboard, then a thin FastAPI
  foundation over the same functions.
- Reason: this validates external integration and task parsing before adding UI
  complexity, while keeping HTTP behavior aligned with the tested CLI/core
  payloads.
- Consequence: the dashboard can consume stable repo and task APIs; run,
  question, log, and auth behavior were added as separate slices, and the v1
  dashboard now uses those APIs without adding frontend build tooling.
