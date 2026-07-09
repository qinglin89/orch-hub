---
last-updated: 2026-07-10
verified-against: 30c1e08a6e706cab2d922417cf0024aa2920c5f7
---

# Architecture: Components

## Registry Adapter

Discovers managed repos from ai-native-development. Preferred source is `aii-2 registry list --json`; fallback is `<ai-native-development>/.registry/repos.local.json`. The adapter normalizes entries into local models containing at least repo name, path, and manifest path.

## Readiness Checker

Determines whether a managed repo is safe to offer as launchable. Initial readiness checks:

1. Repo path exists.
2. `.ai-deploy-manifest.json` exists.
3. `.cursor/orchestrator/orchestrator.py` exists.
4. `.cursor/orchestrator/.venv/bin/python` exists.
5. `.ai-tasks/index.md` exists.
6. `git status --porcelain` is clean.
7. `aii-2 status <repo>` exits successfully.

Each failure should produce a concise machine-readable reason and a human-readable message.

## Task Inventory

Reads `.ai-tasks/index.md` and individual task frontmatter from managed repos. The implemented `task_store` slice treats the index as the active task-id set plus human summary, and treats each `.ai-tasks/<id>.md` frontmatter as the source of truth for status and blockers. It classifies task-file statuses such as `pending`, `in_progress`, `final_review`, `completed`, and `blocked`, with parser sentinels for missing task files or missing status. Future run-manager data will overlay active/waiting run state without replacing the task-file status.

## Run Manager

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

## Run History Store

Implemented file-backed history extends each run dir with append-only
`events.jsonl`. `orch_hub.history` serializes `RunEvent` records and returns
`history_error` events for unreadable or malformed history lines instead of
hiding them. `RunManager` appends `run_started`, `run_ended`,
`stop_requested`, and `answer_recorded` events; answer audit events include
responder and question metadata but deliberately do not copy answer text out of
the control-dir answer file.

## Control Dir Store

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

## API Service

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

## Launchd Service Packaging

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

## Dashboard

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

## Notification Adapter

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
