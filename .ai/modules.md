---
last-updated: 2026-07-09
verified-against: 72fee874f65041e047c32d3634b0cfe7f19e6e63
---

# Modules

## Module Index

| Module | Location | Description |
|---|---|---|
| CLI | `orch_hub/cli.py` | Implemented `repos` discovery/readiness output and launchd operations dispatch |
| Config | `orch_hub/config.py` | Implemented defaults, service env overrides, bearer token loading, data-dir setting, and derived `aii-2`/registry paths |
| Launchd operations | `orch_hub/launchd.py` | Implemented macOS launchd deploy/update/start/stop/restart/status helpers |
| Models | `orch_hub/models.py` | Implemented repo, readiness, task inventory, question, and run models plus payload helpers |
| Registry | `orch_hub/registry.py` | Implemented managed repo discovery from `aii-2` or registry file |
| Readiness | `orch_hub/readiness.py` | Implemented fail-closed per-repo launch readiness checks |
| Subprocess utilities | `orch_hub/subprocess_utils.py` | Implemented shared subprocess execution wrappers |
| Task inventory | `orch_hub/task_store.py` | Implemented local parser for `.ai-tasks/index.md`, task frontmatter, and contained raw task-content reads |
| Run manager | `orch_hub/runs.py` | Implemented orchestrator subprocess lifecycle, run state, stop requests, and per-repo serialization |
| History | `orch_hub/history.py` | Implemented per-run JSONL event storage and readback |
| Control dir | `orch_hub/control_dir.py` | Implemented question/answer/stop file-channel handling |
| Logs | `orch_hub/logs.py` | Implemented log location, bounded recent tails, and SSE tail frames |
| API | `orch_hub/api.py` | Implemented FastAPI app factory, fail-closed pure-ASGI bearer middleware, public dashboard shell, health/repo/task/run/escalation/log/history routes, and uvicorn entry point |
| Notifications | `orch_hub/notifications.py` | Future notifier interface and Bark adapter |
| Web UI | `orch_hub/web/` | Implemented vanilla mobile dashboard SPA assets, including task-detail run controls |
| Launchd plist | `deploy/launchd/com.qinglin89.orch-hub.plist` | Implemented launchd user-agent template for resident service operation |
| Deployment docs | `docs/launchd.md` | Implemented install/start/stop/log guidance and Tailscale assumptions |
| Tests | `tests/` | Pytest coverage using fake repos and mocked subprocesses |

## Module Details

### CLI

Location: `orch_hub/cli.py`

Responsibility: expose user-facing commands. Repo commands list managed repos
and readiness:

```bash
python -m orch_hub.cli repos
python -m orch_hub.cli repos --json
```

Launchd commands dispatch to `orch_hub.launchd` for service deployment and
lifecycle operations:

```bash
orch-hub launchd deploy|update|start|stop|restart|status
```

Dependencies: config, registry, readiness, models, launchd.

### Launchd Operations

Location: `orch_hub/launchd.py`

Responsibility: manage the singleton macOS launchd user agent
`com.qinglin89.orch-hub`. `deploy_service` / `update` resolve the checkout,
ai-native-development path, token, host/port/data dir, plist path, log dir, and
`PATH`; render the installed plist from
`deploy/launchd/com.qinglin89.orch-hub.plist`; install/update the checkout
`.venv` package; validate with `plutil`; boot out any loaded service; and
bootstrap through `launchctl`. Token precedence is explicit option, env var,
existing plist, then generated token.

`start_service`, `stop_service`, and `restart_service` are launchd lifecycle
helpers only. They bootstrap or boot out the fixed label and deliberately do
not call `RunManager`, write control-dir stop files, inspect run records, or
signal detached orchestrator subprocesses. `status_service` combines
`launchctl print`, installed plist parsing, and a public `/api/health` probe.

Dependencies: config, subprocess_utils, plistlib, urllib.

### Config

Location: `orch_hub/config.py`

Responsibility: define `ORCH_HUB_AI_NATIVE_DEPLOYMENT` override, default
ai-native-development path, `ORCH_HUB_TOKEN` bearer-token loading,
`ORCH_HUB_HOST`, `ORCH_HUB_PORT`, `ORCH_HUB_DATA_DIR`, and derived
`aii-2`/registry file paths:

```text
/Users/linqing/workplace/github.com/qinglin89/ai-native-development
```

All current environment variables strip surrounding whitespace. Empty or
whitespace-only values count as unset; a malformed or out-of-range
`ORCH_HUB_PORT` raises `ValueError` instead of silently binding the default.
`ORCH_HUB_DATA_DIR` defaults to `~/.orch-hub`; the run manager writes
`<data_dir>/runs/<run_id>/` directories there. Future config belongs here too:
notifier settings and storage-specific options.

### Models

Location: `orch_hub/models.py`

Responsibility: define stable data shapes for managed repos, readiness reports,
task inventory, control-dir questions, run state, and run history.
`TaskSummary` carries `task_id`, task-file `status`, optional index summary,
and blockers;
`RepoTaskInventory` pairs a `ManagedRepo` with `index_present` and ordered
tasks. `Question` carries filename-authoritative `seq`, display/audit fields,
parse error, answered state, stripped valid `answer`, and answer repair error;
`to_dict()` deliberately excludes the local path. `RunRecord` is the persisted
run shape and carries launch-time `task_status`; `RunStatus` overlays
active/waiting/completed/stopped/error/ended state plus pending question count.
`RunEvent` stores metadata-only history events and `RunHistoryEntry` combines a
computed run status with its persisted events. Public readiness payloads
contain `ready` plus failed check reasons only.

### Registry

Location: `orch_hub/registry.py`

Responsibility: prefer `<ai-native-development>/aii-2 registry list --json`; if unavailable or failing, fallback to `<ai-native-development>/.registry/repos.local.json`. Validate and normalize JSON-array entries into `ManagedRepo`.

### Readiness

Location: `orch_hub/readiness.py`

Responsibility: check repo filesystem, deployed manifest, orchestrator script, venv Python, task index, clean git tree, and `aii-2 status <repo>`. Reports keep all seven checks internally and fail closed when paths or tools are missing.

### Subprocess Utilities

Location: `orch_hub/subprocess_utils.py`

Responsibility: central wrapper for running commands with cwd, capture, timeout if later needed, and concise output extraction. Do not hide command failures that readiness or API callers need to report.
The detached orchestrator launch is not routed through this blocking wrapper;
`RunManager` uses `subprocess.Popen` directly so it can keep a process handle
and redirect output.

### Task Inventory

Location: `orch_hub/task_store.py`

Responsibility: parse active task files without importing protocol or orchestrator code. `list_repo_tasks(repo)` reads `.ai-tasks/index.md` for active task ids and summaries, then reads each task file frontmatter for authoritative status and blockers. Missing indexes return `index_present=False`; missing task files use a `missing` sentinel status; task files without parseable status use `unknown`. `read_task_content(repo, task_id)` backs the dashboard detail view; it rejects non-bare filenames, resolves the task file target, and only returns raw content whose resolved path remains under the resolved `.ai-tasks` directory.

### API

Location: `orch_hub/api.py`

Responsibility: expose the implemented HTTP foundation as a thin FastAPI
adapter over shared core functions. `create_app(config=None, *,
run_manager=None)` builds an app with a default-deny bearer-token middleware,
public `GET /api/health`, public dashboard shell routes, protected repo/task
routes, and protected run, escalation, log, and history routes over one
`RunManager` instance. Implemented run
routes are `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}`, and
`POST /api/runs/{run_id}/stop`; implemented escalation routes are
`GET /api/escalations` and
`POST /api/runs/{run_id}/escalations/{seq}/answer`; implemented log routes are
`GET /api/runs/{run_id}/logs` and
`GET /api/runs/{run_id}/logs/stream`; implemented history route is
`GET /api/history/runs`. Module-level `app` supports
`uvicorn orch_hub.api:app`; `run(host=None, port=None)` and the `orch-hub-api`
console script serve locally by default and read host/port from environment
when arguments are omitted. The bearer middleware is pure ASGI, not
`BaseHTTPMiddleware`, so SSE streaming is not buffered and client disconnects
propagate. Browser log tailing uses `fetch`/ReadableStream with the standard
bearer header, not `EventSource` or query tokens. `RegistryError` returns HTTP
500; unknown repo/task lookups and unknown runs return HTTP 404; launch/run
state conflicts return HTTP 409 with structured details where useful.

### Web UI

Location: `orch_hub/web/`

Responsibility: provide the build-stepless mobile dashboard shell. `index.html`
defines the static app container and bottom navigation; `app.js` owns token
entry/localStorage, bearer `fetch` calls, hash routing, repo/task/detail/run
views, task-detail Start/Stop run controls, waiting-question answer forms, run
history, and recent/live log views; `styles.css` holds the mobile layout. The
dashboard reads only HTTP payloads, uses `fetch` +
`ReadableStream.getReader()` for SSE log streaming so the bearer header is
present, aborts active streams on navigation/reload/pause, and writes untrusted
log/task data as escaped HTML or text nodes.

Run controls stay frontend-only over the existing API: Start posts
`{repo_id, task_id, acknowledge_blocked}` to `POST /api/runs`, Stop posts to
`POST /api/runs/{run_id}/stop`, and launch refusal UI renders structured 409
details such as readiness `reasons`, repo-busy `run_id`, or blocked-task
`acknowledge_required`.

### Run Manager

Location: `orch_hub/runs.py`

Responsibility: own run lifecycle for deployed orchestrator subprocesses:
start, stop, list, get, reattach, record pid, enforce a per-repo live-run
mutex, connect run dirs, and expose computed run state to future API/UI layers.
Launch gates run fail-closed in order: existing live run for the repo, task
inventory/status from `list_repo_tasks`, then readiness from `check_readiness`.
`blocked` tasks require explicit acknowledgement before launch; `missing`,
`unknown`, and unrecognized task statuses are rejected.

Run dirs live under `<data_dir>/runs/<run_id>/` with `run.json`,
`events.jsonl`, `control/`, and `spawn.out`; each run gets a fresh external
control dir. Launch uses
`/usr/bin/caffeinate -i <repo>/.cursor/orchestrator/.venv/bin/python
<repo>/.cursor/orchestrator/orchestrator.py <task-id> --control-dir
<run>/control [extra args]`, `cwd=<repo>`, stdin from devnull, stdout/stderr to
`spawn.out`, and `start_new_session=True`. `run.json` is the source of truth,
stores launch-time task frontmatter status, and is rewritten atomically;
in-memory `Popen` handles exist only to capture exit codes for runs that end
while the hub process is alive.

Reattach scans run dirs, loads records, and probes pid liveness with
`os.kill(pid, 0)`. If a run ended while the hub was down, `ended_ts` is
recorded with `exit_code=None`. End classification prefers task completion or
archival evidence, then hub stop evidence (`stop.flag` or a valid `"stop"`
answer), then bare exit code. Broken run dirs surface as `error` states and do
not block repo launches when no repo attribution is available. The manager
appends `run_started`, `run_ended`, `stop_requested`, and `answer_recorded`
events and exposes newest-first `recent_runs(limit=...)` history entries.

### History

Location: `orch_hub/history.py`

Responsibility: append and read metadata-only run history events in
`<data_dir>/runs/<run_id>/events.jsonl`. `append_event(run_dir, event)` writes
one sorted-key JSON object per line. `read_events(run_dir)` returns
`RunEvent` records, tolerates missing history as empty, and returns
`history_error` events for unreadable files, invalid JSON lines, or non-object
lines.

The module is storage-only: it does not parse task files, inspect control-dir
answers, decide event timing, or store human answer text.

### Control Dir

Location: `orch_hub/control_dir.py`

Responsibility: implement the hub side of the orchestrator `--control-dir`
file channel. `list_questions(control_dir)` returns `Question` records for
matching `NNN-question.json` files; `pending_questions(control_dir)` filters to
questions without a same-seq valid answer; `write_answer(question, answer,
responder="orch-hub")` atomically writes the mirrored `NNN-answer.json` with
`seq`, `ts`, `responder`, and stripped `answer`; valid answers are exposed on
`Question.answer`. `request_stop(control_dir)` writes an idempotent empty
`stop.flag`; `stop_requested` and `stop_answered` support run-end
classification. Existing valid answers raise `DuplicateAnswerError`; malformed
answers can be repaired by atomic rewrite.

### Logs

Location: `orch_hub/logs.py`

Responsibility: locate and read/stream deployed orchestrator logs for known
runs. The module receives `RunRecord` values from the run manager and never
accepts a client-supplied path. It matches the orchestrator filename convention
`<utc-ts>-<task-id>.log` under the record's `log_dir`, using
`floor(started_ts)` as the lower bound and returning the earliest matching file
for correct re-run attribution. Records with no usable `started_ts` fall back
to the newest matching task log.

`read_recent(record, max_lines=...)` returns a `LogSnapshot` with bounded tail
lines, a `truncated` flag, current size, and graceful absent details for
missing/rotated/unreadable logs. `tail_events(record, is_live=...)` is an async
SSE generator: it waits for the log to appear, emits a bounded initial tail,
follows appended complete lines with byte offsets and a raw-bytes pending
buffer, sends idle heartbeat comments, and ends with `event: end` on run death
or log rotation. File reads run inline on the event loop in v1; move them to a
thread only if connection count grows.

## Boundary Rules

- Registry code does not perform readiness checks.
- Readiness code does not launch orchestrators.
- Task inventory reads task files but does not decide orchestrator state-machine turns.
- Run manager launches subprocesses but does not parse task protocol beyond pre-start checks.
- History serializes run/audit metadata only; `RunManager` decides when events
  are written.
- Control dir handles file-channel mechanics only; API/UI/run-manager layers own identity, audit storage, notifications, and subprocess state.
- Logs locate/read/stream files only from run records; API/UI callers never pass log paths.
- API and CLI should be thin adapters over application functions.
- Launchd operations manage only the fixed hub service label; run stop
  semantics stay in the run API/control-dir flow.
- Web UI callers should consume protected HTTP APIs and must not parse managed
  repo files directly.
- No module imports deployed orchestrator code.
