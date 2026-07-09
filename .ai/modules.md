---
last-updated: 2026-07-10
verified-against: 3191b5852164fdff5706f750bd0c47f892cb5709
---

# Modules

## Module Index

| Module | Location | Description |
|---|---|---|
| CLI | `orch_hub/cli.py` | Implemented `repos` discovery/readiness output, launchd operations dispatch, and notification delivery checks |
| Config | `orch_hub/config.py` | Implemented defaults, service env overrides, bearer token and Bark URL loading, data-dir setting, and derived `aii-2`/registry paths |
| Launchd operations | `orch_hub/launchd.py` | Implemented macOS launchd deploy/update/start/stop/restart/status helpers, including optional Bark URL plist surfacing and lookup |
| Models | `orch_hub/models.py` | Implemented repo, readiness, commit, task inventory, question, and run models plus payload helpers |
| Registry | `orch_hub/registry.py` | Implemented managed repo discovery from `aii-2` or registry file |
| Readiness | `orch_hub/readiness.py` | Implemented fail-closed per-repo launch readiness checks |
| Git metadata | `orch_hub/git_meta.py` | Implemented best-effort latest/recent commit reads for dashboard/API display |
| Subprocess utilities | `orch_hub/subprocess_utils.py` | Implemented shared subprocess execution wrappers |
| Task inventory | `orch_hub/task_store.py` | Implemented local parser for `.ai-tasks/index.md`, including Markdown-link Task cells, task frontmatter, and contained raw task-content reads |
| Run manager | `orch_hub/runs.py` | Implemented orchestrator subprocess lifecycle, run state, stop requests, and per-repo serialization |
| Run monitor | `orch_hub/monitor.py` | Implemented resident daemon-thread status scanner for proactive notification delivery |
| History | `orch_hub/history.py` | Implemented per-run JSONL event storage and readback |
| Control dir | `orch_hub/control_dir.py` | Implemented question/answer/stop file-channel handling |
| Logs | `orch_hub/logs.py` | Implemented log location, bounded recent tails, and SSE tail frames |
| API | `orch_hub/api.py` | Implemented FastAPI app factory, fail-closed pure-ASGI bearer middleware, lifespan-owned monitor, public dashboard shell, health/repo/commit/task/run/escalation/log/history routes, and uvicorn entry point |
| Notifications | `orch_hub/notifications.py` | Implemented notifier interface, Bark backend, and run/question/probe notification formatters |
| Web UI | `orch_hub/web/` | Implemented vanilla mobile dashboard SPA assets, including status layout, recent commits, and task-detail run controls |
| Launchd plist | `deploy/launchd/com.qinglin89.orch-hub.plist` | Implemented launchd user-agent template for resident service operation |
| Deployment docs | `docs/launchd.md` | Implemented install/start/stop/log guidance and Tailscale assumptions |
| Tests | `tests/` | Pytest coverage using fake repos and mocked subprocesses |

## Module Details

### CLI

Location: `orch_hub/cli.py`

Responsibility: expose user-facing commands. Repo commands list managed repos
and readiness; JSON output also carries best-effort latest commit metadata:

```bash
python -m orch_hub.cli repos
python -m orch_hub.cli repos --json
```

Launchd commands dispatch to `orch_hub.launchd` for service deployment and
lifecycle operations:

```bash
orch-hub launchd deploy|update|start|stop|restart|status
```

Notification commands dispatch through the configured notifier path:

```bash
orch-hub notify test
```

`notify test` resolves `Config.from_env()`, falls back to the installed launchd
plist's Bark URL when the shell env is unset, sends one probe notification
directly through `build_notifier(config)`, and reports delivery failure without
printing the secret endpoint.

Dependencies: config, registry, readiness, models, launchd, notifications.

### Launchd Operations

Location: `orch_hub/launchd.py`

Responsibility: manage the singleton macOS launchd user agent
`com.qinglin89.orch-hub`. `deploy_service` / `update` resolve the checkout,
ai-native-development path, token, host/port/data dir, plist path, log dir,
`PATH`, and optional Bark URL; render the installed plist from
`deploy/launchd/com.qinglin89.orch-hub.plist`; install/update the checkout
`.venv` package; validate with `plutil`; boot out any loaded service; and
bootstrap through `launchctl`. Required template placeholders fail before write
or load; unknown optional placeholders from newer templates render as `""` for
old-CLI/new-template update compatibility. After a successful bootout,
deploy/update poll for the label to disappear and retry transient bootstrap
exit-5/input-output failures with bounded backoff. Token precedence is explicit
option, env var, existing plist, then generated token. Bark URL precedence is
`ORCH_HUB_BARK_URL`, then existing installed-plist value, then disabled; it has
no deploy CLI flag and is never echoed because the URL embeds a device-key
secret. `installed_bark_url(plist_path=...)` reads the installed plist value for
`orch-hub notify test` when the current shell has not exported the URL.

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
`ORCH_HUB_HOST`, `ORCH_HUB_PORT`, `ORCH_HUB_DATA_DIR`, `ORCH_HUB_BARK_URL`,
and derived
`aii-2`/registry file paths:

```text
/Users/linqing/workplace/github.com/qinglin89/ai-native-development
```

All current environment variables strip surrounding whitespace. Empty or
whitespace-only values count as unset; a malformed or out-of-range
`ORCH_HUB_PORT` raises `ValueError` instead of silently binding the default.
`ORCH_HUB_DATA_DIR` defaults to `~/.orch-hub`; the run manager writes
`<data_dir>/runs/<run_id>/` directories there. `ORCH_HUB_BARK_URL` is the full
Bark endpoint including device key; empty or whitespace-only values disable
push notifications.

### Models

Location: `orch_hub/models.py`

Responsibility: define stable data shapes for managed repos, readiness reports,
commit metadata, task inventory, control-dir questions, run state, and run
history.
`CommitInfo` carries `hash`, `short_hash`, `committed_ts`, and `subject`;
`repos_payload` accepts `(repo, report)` or `(repo, report, commit)` so CLI
JSON and `GET /api/repos` stay identical and always include `commit` (`null`
when unavailable).
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

### Git Metadata

Location: `orch_hub/git_meta.py`

Responsibility: read display-only git commit metadata for managed repos without
participating in launch readiness. `latest_commit(repo)` returns the newest
`CommitInfo` or `None`; `read_commits(repo, limit=...)` returns
`CommitLog(available, commits)` newest-first with the limit clamped to `1..50`.
It invokes `git log` in the repo, parses `%H`, `%h`, `%cI`, and `%s` using an
ASCII unit-separator field delimiter, and degrades to unavailable on missing
git, non-git dirs, missing paths, or non-zero exit. Commit lookup failure must
not hide repo/task content.

### Subprocess Utilities

Location: `orch_hub/subprocess_utils.py`

Responsibility: central wrapper for running commands with cwd, capture, timeout if later needed, and concise output extraction. Do not hide command failures that readiness or API callers need to report.
The detached orchestrator launch is not routed through this blocking wrapper;
`RunManager` uses `subprocess.Popen` directly so it can keep a process handle
and redirect output.

### Task Inventory

Location: `orch_hub/task_store.py`

Responsibility: parse active task files without importing protocol or orchestrator code. `list_repo_tasks(repo)` reads `.ai-tasks/index.md` for active task ids and summaries, normalizing bare Task cells or full-cell Markdown links like `[id](id.md)` to the safe bare id used for file lookup and API payloads, then reads each task file frontmatter for authoritative status and blockers. Missing indexes return `index_present=False`; missing task files use a `missing` sentinel status; task files without parseable status use `unknown`. `read_task_content(repo, task_id)` backs the dashboard detail view; it rejects non-bare filenames, resolves the task file target, and only returns raw content whose resolved path remains under the resolved `.ai-tasks` directory.

### API

Location: `orch_hub/api.py`

Responsibility: expose the implemented HTTP foundation as a thin FastAPI
adapter over shared core functions. `create_app(config=None, *,
run_manager=None)` builds an app with a default-deny bearer-token middleware,
public `GET /api/health`, public dashboard shell routes served with
`Cache-Control: no-store`, protected repo/task routes, and protected run,
escalation, log, and history routes over one
`RunManager` instance plus a lifespan-owned `RunMonitor`. Implemented run
routes are `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}`, and
`POST /api/runs/{run_id}/stop`; implemented escalation routes are
`GET /api/escalations` and
`POST /api/runs/{run_id}/escalations/{seq}/answer`; implemented log routes are
`GET /api/runs/{run_id}/logs` and
`GET /api/runs/{run_id}/logs/stream`; implemented history route is
`GET /api/history/runs`; implemented commit route is
`GET /api/repos/{repo_id}/commits`. `GET /api/repos` includes a best-effort
`commit` field shared with CLI JSON; recent commit lookup failures return
`available:false` rather than raising. The recent-log route adds a nested `diagnostic` from
`spawn.out` only when the orchestrator log is absent and the capture is
non-empty. Module-level `app` supports
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
defines the static app container and bottom navigation; the shell assets are
served uncached by the API so updates reach browsers on the next load. `app.js`
owns token entry/localStorage, bearer `fetch` calls, hash routing,
repo/task/detail/run views, repo/task run-state signals, recent commits,
task-detail Start/Stop run controls, waiting-question answer forms, run
history, and recent/live log views; `styles.css` holds the mobile layout. The
dashboard reads only HTTP payloads, uses `fetch` +
`ReadableStream.getReader()` for SSE log streaming so the bearer header is
present, aborts active streams on navigation/reload/pause, and writes untrusted
log/task data as escaped HTML or text nodes.

Repo cards show readiness plus a separate run signal (yellow waiting, green
active, gray idle); ready cards add latest commit time and active task count
from task inventory. Task cards use left-border/dot run accents distinct from
frontmatter status badges. Repo detail adds a recent-commits section with
unavailable and empty-history states.

Run controls stay frontend-only over the existing API: Start posts
`{repo_id, task_id, acknowledge_blocked, extra_args}` to `POST /api/runs` from a
launch-options dialog whose first args are always `--backend cc-codex` plus
only user-changed cc-codex overrides; it does not expose a backend selector.
Stop posts to `POST /api/runs/{run_id}/stop`, and launch refusal UI renders
structured 409 details such as readiness `reasons`, repo-busy `run_id`, or
blocked-task `acknowledge_required`. The log view renders the recent-log
route's `spawn.out` diagnostic and falls back to it when a live stream ends
before any orchestrator log line is seen.

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
`spawn.out`, `start_new_session=True`, and an explicit environment whose `PATH`
prepends common user agent-CLI locations (`~/.local/bin`, `~/.claude/local`,
`~/.npm-global/bin`, `~/.cargo/bin`) while preserving the inherited environment
and baseline macOS paths. `run.json` is the source of truth,
stores launch-time task frontmatter status plus the internal `spawn_out` path,
and is rewritten atomically; in-memory `Popen` handles exist only to capture
exit codes for runs that end while the hub process is alive.

Reattach scans run dirs, loads records, and probes pid liveness with
`os.kill(pid, 0)`. If a run ended while the hub was down, `ended_ts` is
recorded with `exit_code=None`. End classification prefers task completion or
archival evidence, then hub stop evidence (`stop.flag` or a valid `"stop"`
answer), then bare exit code. Broken run dirs surface as `error` states and do
not block repo launches when no repo attribution is available. The manager
appends `run_started`, `run_ended`, `stop_requested`, and `answer_recorded`
events and exposes newest-first `recent_runs(limit=...)` history entries.

The manager also owns notification trigger timing when a notifier is
configured: run-end notifications fire once at the observed live-to-ended
transition using the same end classification shown by the dashboard, and
pending-question notifications fire from `_status` when the existing
`pending_questions` scan first sees a question. API reads and the resident
`RunMonitor` both drive this same status path. Question push dedup is an
in-memory `(run_id, seq)` set, so rescans do not duplicate pushes within one
hub process and a restart re-notifies a still-open question once. Delivery goes
through `_emit`, which dispatches best-effort work off the lock by default and
swallows backend exceptions so notification failures cannot corrupt run state.

### Run Monitor

Location: `orch_hub/monitor.py`

Responsibility: run one resident daemon thread that periodically calls a
scanner's `list_runs()` method, normally the app's single `RunManager`, so
run-ended and pending-question notifications fire without dashboard/API
polling. `RunMonitor.start()` and `stop()` are idempotent; `stop()` uses a
bounded join, and scan failures are logged and swallowed so the service keeps
running. The default interval is 30 seconds and intentionally not an
environment setting.

The monitor owns no run state, event state, notification formatting, or
de-duplication; it only schedules the existing run-manager status scan.

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
missing/rotated/unreadable logs. `read_spawn_out(record, max_lines=...)` reads
the persisted startup capture path directly and treats missing, empty, or
blank-only captures as absent. `tail_events(record, is_live=...)` is an async
SSE generator: it waits for the log to appear, emits a bounded initial tail,
follows appended complete lines with byte offsets and a raw-bytes pending
buffer, sends idle heartbeat comments, and ends with `event: end` on run death
or log rotation. File reads run inline on the event loop in v1; move them to a
thread only if connection count grows.

### Notifications

Location: `orch_hub/notifications.py`

Responsibility: define backend-agnostic notification payloads, the `Notifier`
protocol, the Bark backend, and shared event formatters. `Notification`
contains title, body, and optional group; `to_dict()` maps to the Bark JSON
payload. `BarkNotifier` POSTs JSON to the configured full Bark endpoint with a
bounded timeout and an injectable transport for tests. `build_notifier(config)`
returns `BarkNotifier` when `Config.bark_url` is set and `None` when push is
disabled.

Formatters are pure: `run_end_notification(record, state, detail)` turns the
run manager's completed/stopped/error/ended classification into device text,
`question_notification(repo_name, task_id, question)` formats a newly observed
pending control-dir question as `needs input`, and `probe_notification()` builds
the harmless delivery-check payload used by `orch-hub notify test`. The module
does not decide when notifications fire; `RunManager` owns live-run trigger
policy and isolation, while the CLI sends probes directly so failures affect
only the command exit code.

## Boundary Rules

- Registry code does not perform readiness checks.
- Readiness code does not launch orchestrators.
- Git metadata is display-only and never gates readiness or run launch.
- Task inventory reads task files but does not decide orchestrator state-machine turns.
- Run manager launches subprocesses but does not parse task protocol beyond pre-start checks.
- History serializes run/audit metadata only; `RunManager` decides when events
  are written.
- Control dir handles file-channel mechanics only; API/UI/run-manager layers own identity, audit storage, notifications, and subprocess state.
- Logs locate/read/stream files only from run records; API/UI callers never pass log paths.
- Run monitor schedules `RunManager.list_runs()` only; it must not own run
  state, notification policy, or de-duplication.
- API and CLI should be thin adapters over application functions.
- Launchd operations manage only the fixed hub service label; run stop
  semantics stay in the run API/control-dir flow.
- Web UI callers should consume protected HTTP APIs and must not parse managed
  repo files directly.
- No module imports deployed orchestrator code.
