---
last-updated: 2026-07-09
verified-against: 424b37c2c56496458fbffdd1c11a4079d1983c82
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

Reads `.ai-tasks/index.md` and individual task frontmatter from managed repos. The implemented `task_store` slice treats the index as the active task-id set plus human summary, and treats each `.ai-tasks/<id>.md` frontmatter as the source of truth for status and blockers. It classifies task-file statuses such as `pending`, `in_progress`, `final_review`, `completed`, and `blocked`, with parser sentinels for missing task files or missing status. Future run-manager data will overlay active/waiting run state without replacing the task-file status.

### Run Manager

Implemented component that launches each target repo's deployed orchestrator as
a detached subprocess and persists hub-owned run state:

```text
/usr/bin/caffeinate -i \
  <repo>/.cursor/orchestrator/.venv/bin/python \
  <repo>/.cursor/orchestrator/orchestrator.py \
  <task-id> \
  --control-dir <run-dir> \
  [flags]
```

The run manager uses `cwd=<repo>`, `start_new_session=True`, stdin from
devnull, and stdout/stderr redirected to `<data_dir>/runs/<run_id>/spawn.out`.
It creates a fresh external run dir per run:

```text
<data_dir>/runs/<run_id>/
  run.json
  events.jsonl
  control/
  spawn.out
```

`run.json` records run id, repo/task identity, launch-time task-store status,
pid, started timestamp, control dir, log dir, argv, optional ended timestamp,
and optional exit code. `events.jsonl` records run lifecycle, stop-request, and
answer-audit metadata events. The started timestamp is captured just before
spawning so it is a sound lower bound for the child orchestrator's
second-precision log filename. A resident manager keeps `Popen` handles only to
capture exit codes; a fresh manager reattaches by scanning run dirs and probing
pid liveness. Cross-repo parallelism is allowed; same-repo live runs are not.

### Run History Store

Implemented file-backed history extends each run dir with append-only
`events.jsonl`. `orch_hub.history` serializes `RunEvent` records and returns
`history_error` events for unreadable or malformed history lines instead of
hiding them. `RunManager` appends `run_started`, `run_ended`,
`stop_requested`, and `answer_recorded` events; answer audit events include
responder and question metadata but deliberately do not copy answer text out of
the control-dir answer file.

### Control Dir Store

Implemented file-channel adapter for orchestrator questions, answers, and stop
requests. `list_questions` and `pending_questions` read `NNN-question.json`
files, use the filename sequence as authoritative, tolerate malformed question
payloads by surfacing an error, and keep a question pending until a same-seq
answer file satisfies the orchestrator rule: JSON object with a non-empty
string `answer`. `write_answer` mirrors the observed question filename, writes
`seq`, `ts`, `responder`, and stripped `answer` atomically by same-dir temp file
plus rename, refuses to overwrite an existing valid answer, and repairs a
malformed answer by atomic rewrite. `request_stop` writes an idempotent empty
`stop.flag` in an existing run dir. The module is file-channel only and has no
web/API/subprocess ownership.

### API Service

Implemented FastAPI service exposing health, repo readiness, task inventory,
task content, run, escalation, log, history, and dashboard shell endpoints. The
current route set includes public `GET /`, `GET /app.js`, `GET /styles.css`,
and `GET /api/health`, plus protected `GET /api/repos`, `GET /api/tasks`,
`GET /api/repos/{repo_id}/tasks`, `GET /api/repos/{repo_id}/tasks/{task_id}`,
run start/list/get/stop routes, escalation list/answer routes,
`GET /api/runs/{run_id}/logs`, `GET /api/runs/{run_id}/logs/stream`, and
`GET /api/history/runs`.
The public shell assets contain no secrets; every data path, docs/OpenAPI, and
unknown future path requires `Authorization: Bearer <ORCH_HUB_TOKEN>` when the
token is configured. If no token is configured, protected routes fail closed
with HTTP 503. The auth middleware is pure ASGI rather than Starlette
`BaseHTTPMiddleware` so SSE responses stream incrementally and disconnects
propagate. The API calls the same registry/readiness/task/run/control/log
functions used by the CLI and core tests and keeps route handlers thin over
shared application services.

### Launchd Service Packaging

Implemented macOS launchd user-agent packaging lives in
`deploy/launchd/com.qinglin89.orch-hub.plist`, with operator docs in
`docs/launchd.md`. The plist runs the installed `orch-hub-api` console script,
sets `ORCH_HUB_TOKEN`, `ORCH_HUB_HOST`, `ORCH_HUB_PORT`,
`ORCH_HUB_AI_NATIVE_DEPLOYMENT`, `ORCH_HUB_DATA_DIR`, optional
`ORCH_HUB_BARK_URL`, and an explicit `PATH`, uses `RunAtLoad` plus
`KeepAlive`, and logs to `~/Library/Logs/orch-hub/`. The token and optional
Bark URL are embedded in the user-agent plist, so installs should keep it
`chmod 600`. Deploy/update fail on unresolved required template placeholders;
unknown optional placeholders from newer templates are blanked so old installed
CLIs can refresh a newer checkout when empty means unset/disabled.

### Dashboard

Implemented mobile-first, build-stepless SPA under `orch_hub/web/`. It is
served as an explicit public shell (`/`, `/app.js`, `/styles.css`) because plain
browser navigation cannot attach a bearer header. The browser stores the token
in `localStorage` and adds `Authorization: Bearer ...` to every data `fetch`,
including the log stream. Main views:

- All repos with readiness from `/api/repos` and live run badges from `/api/runs`.
- All tasks from `/api/tasks`, filterable by repo and frontmatter status.
- Repo detail with task filters and the missing-index state.
- Task detail showing task file content, run state, answer input when waiting,
  and a link to logs.
- Run history from `GET /api/history/runs`.
- Run log tail: finished runs use `GET /api/runs/{run_id}/logs`; live runs use
  `GET /api/runs/{run_id}/logs/stream` through `fetch` plus
  `ReadableStream.getReader()`, not `EventSource`.

### Notification Adapter

Implemented notifier interface with Bark as v1 backend. `Config.bark_url`
comes from `ORCH_HUB_BARK_URL`; blank/unset disables notifications. The
production API app builds `RunManager(..., notifier=build_notifier(config))`.
`BarkNotifier` posts JSON (`title`, `body`, optional `group`) to the configured
full Bark endpoint, which includes the device key and must never be logged.
`RunManager._emit` isolates delivery: the default dispatch starts a daemon
thread, backend exceptions are swallowed after logging only exception type and
notification title, and notification failure cannot corrupt `run.json`,
`events.jsonl`, or control-dir files.

Triggers live in `RunManager`: terminal-state notifications fire exactly once
when `_refresh` observes the live-to-ended transition and use the same
`_classify_end` result as the dashboard (`completed` covers task completion or
close-out archival); pending-question notifications fire from `_status` when a
live run's existing `pending_questions` scan first observes a question.
Question dedup is an in-memory `(run_id, seq)` set, so a hub restart re-notifies
a still-open question once and answered questions never re-fire.

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

## Data Flow

### Discovery and Readiness

1. Load config, including ai-native-development path.
2. Try `aii-2 registry list --json`.
3. If command fails or is unavailable, read `<ai-native-development>/.registry/repos.local.json`.
4. For each repo entry, run readiness checks.
5. Return stable JSON for CLI and API responses.

### HTTP Foundation

1. `create_app(config=None)` captures a `Config` value once per FastAPI app,
   including `ORCH_HUB_TOKEN` from `Config.token`.
2. Module-level `app = create_app()` is the uvicorn target.
3. `run(host=None, port=None)` serves the app with uvicorn, defaulting to
   `Config.host` / `Config.port` from `ORCH_HUB_HOST` and `ORCH_HUB_PORT`
   (`127.0.0.1:8765` when unset); private-network exposure must stay
   Tailscale-only.
4. A default-deny pure-ASGI HTTP middleware allows public access only to
   `/api/health` and the three dashboard shell asset paths. Missing/malformed
   credentials return 401 plus `WWW-Authenticate: Bearer`, invalid credentials
   return 401, and an unset token returns 503 for protected data routes. Pure
   ASGI is required for long-lived SSE log streams; `BaseHTTPMiddleware` can
   buffer streaming responses and delay disconnect propagation.
5. `RegistryError` maps to HTTP 500; unknown repo ids or unlisted task ids on
   per-repo task lookup/detail routes map to HTTP 404. Ordinary readiness
   failures stay inside 200 payloads as `ready: false` with failure reasons.

### Task Inventory

1. Receive `ManagedRepo` values from registry discovery or a caller-provided filter.
2. For each repo, read `.ai-tasks/index.md`; missing indexes return `index_present=false` with no tasks so callers can surface the repo-level issue.
3. Parse task ids and summaries from the index table without trusting the index Status column.
4. Read each listed `.ai-tasks/<task-id>.md` frontmatter for authoritative `status` and `blockers`.
5. Return `RepoTaskInventory` values that can be serialized directly for future task APIs.
6. The single-task content endpoint gates on index membership, rejects unsafe
   task ids in the reader, and resolves the candidate file target to ensure raw
   content stays under the resolved `.ai-tasks` directory.

### Start Run

1. Client selects repo and task.
2. Hub enforces the per-repo single-live-run rule.
3. Hub verifies task existence/status through `list_repo_tasks` and rejects
   unlisted, `missing`, `unknown`, or unrecognized statuses. A `blocked` task
   requires explicit acknowledgement.
4. Hub verifies repo readiness with the existing checker.
5. Hub creates a fresh run dir outside the target repo.
6. Hub starts the deployed orchestrator subprocess with `--control-dir`.
7. Hub records pid, repo, task, control dir, log dir, start time, argv, and
   later observed end state in `run.json`, including the launch-time task
   frontmatter status from `TaskSummary`.
8. Hub appends a `run_started` history event.

### Human Answer

1. Orchestrator writes a question file in the control dir.
2. Hub lists it as waiting with `pending_questions` and notifies the user once
   per `(run_id, seq)` when Bark is configured.
3. User submits an answer through the dashboard or future bot.
4. Hub writes the answer file with `write_answer` and records an audit event.
5. Orchestrator consumes the answer and continues.

### Stop

1. User requests stop.
2. Hub writes `stop.flag` with `request_stop` for graceful stop at a session boundary.
3. Hub records a `stop_requested` event only for the first idempotent stop write.
4. If a hard kill is ever exposed, it must warn that the repo may be dirty and children may be orphaned.

### Log Access

1. Client requests recent or streamed logs for a known `run_id`.
2. API resolves the run through `RunManager.get_run`; unknown/path-escaping ids
   return HTTP 404.
3. `orch_hub.logs` locates `<utc-ts>-<task-id>.log` under the run record's
   `log_dir`, using `task_id` and `floor(started_ts)` for attribution. A later
   re-run gets a later stamp; records with no usable `started_ts` fall back to
   newest matching task log.
4. Recent retrieval returns bounded lines with graceful `present: false`
   details for missing, rotated, or unreadable logs.
5. SSE streaming emits a bounded initial tail, then appended complete lines,
   heartbeat comments while idle, and `event: end` on run end or log rotation.
   Browser clients use `fetch` plus a stream reader with the standard bearer
   header; the hub does not accept query-string log stream credentials.

### Run State Refresh

1. Hub lists run dirs under `<data_dir>/runs/`.
2. For a run with an in-memory `Popen`, `poll()` captures exit code when it
   ends.
3. For reattached runs, pid liveness decides whether the run is still active;
   if not, the hub records `ended_ts` with unknown exit code.
4. Live runs are `waiting` when control-dir questions are pending, otherwise
   `active`.
5. End classification prefers task completion/archival evidence, then stop
   evidence (`stop.flag` or a valid `"stop"` answer), then bare exit code.
6. When the hub observes an end, it rewrites `run.json` and appends a
   `run_ended` event with the observed exit code when available.
7. The same observed end fires one best-effort terminal notification through
   the configured notifier, without changing run-state writes if delivery
   fails.

### Run History Access

1. Client requests recent history through `GET /api/history/runs`.
2. API asks `RunManager.recent_runs(limit=...)`, which clamps the limit to
   `0..200`, scans run dirs newest-first, refreshes computed run status, and
   loads each run's `events.jsonl`.
3. Response embeds each run's current serialized `RunStatus` plus persisted
   event records. History parsing failures are represented as events rather
   than route failures.

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
