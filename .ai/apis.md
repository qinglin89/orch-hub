---
last-updated: 2026-07-08
verified-against: e6909681ed003d16cb03b7670151501af7248250
---

# APIs and Interfaces

## Public CLI

### `python -m orch_hub.cli repos`

- Purpose: list managed repos discovered from ai-native-development with readiness.
- Output: human-readable status lines, followed by failed readiness reasons for non-ready repos.
- Source: registry adapter plus readiness checker.
- Non-goal: does not start orchestrators.
- Operational errors, including malformed config such as a non-integer
  `ORCH_HUB_PORT`, return exit 1 with `error: ...` on stderr.

### `python -m orch_hub.cli repos --json`

- Purpose: stable JSON form for automation and future FastAPI reuse.
- Stable shape:

```json
{
  "repos": [
    {
      "name": "quantx",
      "path": "/absolute/path/to/quantx",
      "manifest": "/absolute/path/to/quantx/.ai-deploy-manifest.json",
      "readiness": {
        "ready": true,
        "checks": []
      }
    }
  ]
}
```

`readiness.checks` contains only failed checks as `{name, message}`; it is empty when `ready` is true. Internally the readiness report keeps all seven check results for callers that need the full picture.

### `orch-hub launchd deploy|update`

- Purpose: install or refresh the resident macOS launchd service from the
  current checkout.
- Source: `orch_hub.cli` delegates to `orch_hub.launchd.deploy_service`.
- Behavior: resolve deployment settings, optionally create/update `.venv`,
  run local `pip install`, render the installed plist from the checked-in
  template, write it mode `600`, validate it with `plutil`, boot out the fixed
  label if loaded, and bootstrap it.
- Configuration precedence: CLI flag, environment, default for paths/host/port;
  bearer token specifically resolves as `--token`, `ORCH_HUB_TOKEN`, existing
  installed plist token, then generated token printed once.
- Errors: operational failures return exit 1 with `error: ...`; missing or
  invalid templates fail before venv/pip work.

### `orch-hub launchd start|stop|restart`

- Purpose: manage the installed launchd user-agent lifecycle without
  reinstalling package code or rewriting the plist.
- Scope: commands target only `gui/$UID/com.qinglin89.orch-hub`.
- Semantics: `start` bootstraps the installed plist when unloaded; `stop`
  bootouts the fixed label and treats an absent job as already stopped;
  `restart` is stop then start.
- Non-goal: these commands do not call run APIs, write run `stop.flag` files,
  inspect run records, or signal detached orchestrator subprocesses.

### `orch-hub launchd status`

- Purpose: report launchd label/config hints and service health without manual
  launchctl commands.
- Output: fixed label, installed plist path/presence, loaded state, program
  path when readable, dashboard URL, plist parse error when present, launchctl
  output when unloaded, and the public `/api/health` probe result.

## HTTP API

### `GET /api/health`

- Purpose: minimal public service liveness.
- Shape: `{"status": "ok"}`.
- Auth: public by design; it must not disclose deployment configuration.

### `GET /`, `GET /app.js`, `GET /styles.css`

- Purpose: serve the public dashboard shell assets.
- Auth: public by design because browser navigation cannot attach an
  `Authorization` header. These assets contain no secrets; every dashboard data
  request still uses protected HTTP APIs with `Authorization: Bearer <token>`.

### `GET /api/repos`

- Purpose: list managed repos with readiness.
- Source: same functions as CLI `repos --json`.
- Shape: exactly the same top-level object as `python -m orch_hub.cli repos --json`.

### `GET /api/repos/{repo_id}/tasks`

- Purpose: list tasks for one repo, where `repo_id` is the managed-repo name.
- Shape: `tasks_payload([inventory])`, the same `{"repos": [...]}` envelope as
  all-repo task inventory with a single repo entry.

### `GET /api/repos/{repo_id}/tasks/{task_id}`

- Purpose: fetch one listed task's frontmatter-authoritative inventory fields
  plus raw markdown body for the dashboard task-detail view.
- Source: resolves `repo_id` through registry discovery, gets status/summary/
  blockers through `list_repo_tasks`, gates `task_id` on index membership, then
  reads `.ai-tasks/<task_id>.md` through `read_task_content`.
- Shape: `TaskSummary.to_dict()` plus `{"present": bool, "content": str|null}`.
  Listed-but-absent task files return HTTP 200 with `present: false` and
  `content: null`; unknown repos or unlisted task ids return HTTP 404.

### `GET /api/tasks`

- Purpose: list tasks across discovered repos.
- Shape: `tasks_payload(list_tasks(repos))`, preserving `index_present`,
  ordered tasks, frontmatter-authoritative status/blockers, and `missing` /
  `unknown` sentinel statuses.

### `POST /api/runs`

- Purpose: start an orchestrator run for `{repo_id, task_id, extra_args,
  acknowledge_blocked}`.
- Source: resolves `repo_id` through registry discovery, then delegates launch
  gates and subprocess start to the app's single `RunManager`.
- Shape: returns the new run's serialized `RunStatus` with HTTP 201.

### `GET /api/runs`

- Purpose: list known runs with live/waiting/end-state overlays.
- Shape: `{"runs": [RunStatus.to_dict(), ...]}`.

### `GET /api/history/runs`

- Purpose: list recent runs newest-first with computed run status and persisted
  audit/history events.
- Source: delegates to `RunManager.recent_runs(limit=...)`, which refreshes
  run state and reads each run's `events.jsonl`.
- Shape: `{"runs": [RunHistoryEntry.to_dict(), ...]}` where each entry embeds
  the normal `RunStatus` fields plus `events`.
- Query: `limit` defaults to 50 and is clamped to `0..200`.

### `GET /api/runs/{run_id}`

- Purpose: fetch one run's serialized `RunStatus`.
- Errors: unknown run id returns HTTP 404.

### `POST /api/runs/{run_id}/stop`

- Purpose: request graceful stop by delegating to `RunManager.stop_run`, which
  writes `stop.flag` through `control_dir.request_stop`.
- Shape: `{"run_id": ..., "newly_requested": bool, "detail": ...}`; the detail
  surfaces the pending-question stop caveat.

### `GET /api/runs/{run_id}/logs`

- Purpose: retrieve a bounded recent tail of one run's orchestrator log.
- Source: resolves `run_id` through `RunManager.get_run`, then locates and
  reads the log from the run record's `log_dir`, `task_id`, and `started_ts`.
  Clients never supply a filesystem path.
- Shape: `{"run_id", "present", "lines", "truncated", "size", "detail"}`.
  Missing, rotated, or unreadable logs are HTTP 200 with `present: false` and a
  detail string. Unknown runs return HTTP 404.
- Query: `tail` controls returned line count, clamped by the logs module.

### `GET /api/runs/{run_id}/logs/stream`

- Purpose: stream one run's orchestrator log as Server-Sent Events.
- Source: resolves `run_id` through `RunManager.get_run`, emits a bounded
  initial tail, follows appended complete lines, re-probes run liveness each
  poll, and closes with `event: end` when the run ends or the log rotates away.
- Shape: `text/event-stream`; ordinary log lines are `data:` events, idle
  heartbeats are SSE comments, and stream termination is an `event: end`.
  Unknown runs return HTTP 404.
- Auth/client contract: standard `Authorization: Bearer <token>` only. Browser
  clients use `fetch` plus a `ReadableStream` reader because `EventSource`
  cannot set authorization headers; query-string tokens are not supported.

### `GET /api/escalations`

- Purpose: list pending control-dir questions across live runs, with run/repo
  context.
- Shape: `{"escalations": [{"run_id", "repo_name", "task_id", "question"}]}`.

### `POST /api/runs/{run_id}/escalations/{seq}/answer`

- Purpose: write an answer file for one run/question sequence, auditing the
  responder.
- Source: locates the question with `control_dir.list_questions`, writes via
  `control_dir.write_answer`, then records answer-audit metadata through
  `RunManager.audit_answer`; no orchestrator code is imported.
- Shape: `{"run_id": ..., "seq": ..., "answered": true, "responder": ...,
  "answer": ...}`.
  The response echoes the submitted answer for the immediate caller, but
  persisted history deliberately excludes answer text.

Current HTTP behavior:

- `create_app(config=None, *, run_manager=None)` captures `Config.from_env()`
  by default, is testable with an explicit `Config`, and holds one `RunManager`
  instance for run, escalation, and log routes.
- `Config` is loaded from environment by default. Current variables are
  `ORCH_HUB_AI_NATIVE_DEPLOYMENT`, `ORCH_HUB_TOKEN`, `ORCH_HUB_HOST`,
  `ORCH_HUB_PORT`, and `ORCH_HUB_DATA_DIR`; surrounding whitespace is stripped
  and empty/whitespace-only values count as unset.
- Module-level `app` is the `uvicorn orch_hub.api:app` target.
- `run(host=None, port=None)` and console script `orch-hub-api` serve the app
  on `ORCH_HUB_HOST` / `ORCH_HUB_PORT`, defaulting to `127.0.0.1:8765`;
  explicit function arguments override the environment.
- Default-deny pure-ASGI middleware protects every path outside `/api/health`
  and the enumerated dashboard shell paths (`/`, `/app.js`, `/styles.css`),
  including `/docs`, `/openapi.json`, unknown paths, and future data routes.
  Pure ASGI is used instead of `BaseHTTPMiddleware` so streaming responses are
  not buffered and disconnects propagate.
- Missing or malformed credentials return HTTP 401 with
  `WWW-Authenticate: Bearer` and `{"detail": "missing bearer token"}`; wrong
  credentials return HTTP 401 with `{"detail": "invalid bearer token"}`.
- When `ORCH_HUB_TOKEN` is unset, protected routes fail closed with HTTP 503
  instead of serving unauthenticated; `/api/health` still works.
- Token comparison uses `secrets.compare_digest`; auth error responses are
  static and never echo presented credentials.
- `RegistryError` returns HTTP 500 with `{"detail": ...}`.
- Unknown `repo_id` on the per-repo task endpoint returns HTTP 404.
- Run-manager/control-dir errors map to client-actionable statuses:
  `UnknownRunError` → 404; `RepoBusyError`, `RunEndedError`,
  `BlockedTaskError`, `LaunchBlockedError`, and `DuplicateAnswerError` → 409;
  empty answer `ValueError` → 400. `LaunchBlockedError` includes `reasons`;
  `BlockedTaskError` includes `acknowledge_required: true`.

## Internal Interfaces

### Registry Provider

Input: config with ai-native-development path.

Output: list of managed repo models.

Behavior: command source first, registry-file fallback second. An exit-0 command with invalid JSON raises `RegistryError`; command missing/non-zero falls back; missing fallback file returns an empty list.

### Readiness Checker

Input: managed repo model.

Output: readiness report with boolean `ready` and list of failed checks.

Behavior: pure inspection and subprocess checks only; no mutation. Missing repo path skips subprocess execution but records `git_clean` and `aii_status` as failed so launch readiness still fails closed.

### Task Store

Input: repo path.

Output: `RepoTaskInventory` values containing repo fields, `index_present`, ordered `TaskSummary` rows, and optional raw task content for one listed task.

Behavior: parse `.ai-tasks/index.md` and task frontmatter locally. The index supplies active task ids and summaries; task frontmatter supplies authoritative status and blockers. The reusable `tasks_payload(inventories)` helper serializes as `{"repos": [...]}` for future CLI/API adapters.
`read_task_content(repo, task_id)` is the only raw-body reader: it rejects
non-bare filenames, requires the file target to resolve under the resolved
`.ai-tasks` directory, and returns `None` for absent or unsafe content.

### Run Manager

Input: repo id, task id, start flags.

Output: `RunRecord` from `start_run`, `RunStatus` from list/get operations,
and idempotent stop-request results.

Behavior: implemented subprocess lifecycle, per-repo serialization, and history
event ownership.
`RunManager` receives a `ManagedRepo` plus task id, rejects launches when the
repo already has a live run, when task inventory is missing/unlisted or reports
`missing`/`unknown`/unrecognized statuses, when a `blocked` task is not
explicitly acknowledged, or when readiness fails. A successful launch creates a
fresh `<data_dir>/runs/<run_id>/` dir with `control/`, starts the target repo's
deployed orchestrator through `/usr/bin/caffeinate -i` with `--control-dir`,
persists `run.json` with launch-time task status, appends `run_started`, and
records stdout/stderr in `spawn.out`. `list_runs` and `get_run` refresh
live/end state and support reattach from disk by pid probe; observed ends append
`run_ended`. `stop_run` delegates to `control_dir.request_stop`, appends
`stop_requested` only on the first idempotent write, and refuses ended or
broken runs so a post-mortem flag cannot rewrite end classification.
`audit_answer` appends responder/question metadata after a control-dir answer
write, without copying answer text. `recent_runs` returns newest-first
`RunHistoryEntry` values from run records plus `events.jsonl`.

### Control Dir Store

Input: run dir.

Output: pending question records and answer/stop write operations.

Behavior: file-channel only. Missing control dirs list no questions.
Question filenames supply the authoritative sequence. Malformed question JSON is
reported on the returned `Question` instead of hiding the file. A question is
answered only when a same-seq answer file is a JSON object with a non-empty
string `answer`; the stripped valid answer is exposed as `Question.answer`.
Malformed answers keep the question pending and can be repaired with
`write_answer`. Valid answer files are never overwritten. `request_stop` writes
an empty `stop.flag` and requires the run dir to exist; `stop_requested` and
`stop_answered` are read helpers for run classification.

### Log Store

Input: run record.

Output: `LogSnapshot` for bounded recent reads and async SSE frames for live
tailing.

Behavior: file access is scoped to `RunRecord.log_dir` and `RunRecord.task_id`.
Log files are located by the deployed orchestrator's
`<utc-ts>-<task-id>.log` filename convention, with `floor(started_ts)` as the
lower bound. Recent reads cap line and byte counts and return absent snapshots
for missing/rotated logs. Live tails emit only complete newline-terminated
lines, use byte offsets to avoid duplicate initial/follow output, and terminate
gracefully when the run ends or the located file disappears.

### History Store

Input: run dir and `RunEvent` values.

Output: append-only `events.jsonl` plus `RunEvent` readback.

Behavior: `append_event` writes one JSON object per line. `read_events` returns
an empty tuple for missing history and `history_error` events for corrupt or
unreadable history lines. History is metadata-only; answer text remains in the
control-dir answer file, not in `events.jsonl`.

### Notifier

Input: notification event.

Output: delivery result.

Behavior: fan-out capable; Bark is the expected first implementation.

### Launchd Service Template

Input: environment variables in
`deploy/launchd/com.qinglin89.orch-hub.plist`.

Output: resident `orch-hub-api` process managed by launchd, with stdout/stderr
logs under `~/Library/Logs/orch-hub/`.

Behavior: user-agent template sets all current `ORCH_HUB_*` variables and an
explicit `PATH`, starts at login, restarts on exit, and requires the operator
to replace placeholders before bootstrapping. Install/start/stop/reload/log
commands live in `docs/launchd.md`.

## API Conventions

- Prefer absolute paths internally; avoid exposing more path data than the UI needs.
- Keep JSON stable and explicit.
- Include `ready: false` with reasons instead of raising for ordinary readiness failures.
- Raise or return operational errors for invalid registry JSON, unreadable config, or malformed user requests.
- Keep task `blocked` distinct from run `waiting`.
- Never make API handlers call orchestrator internals directly.
- API/run handlers should reuse `orch_hub.control_dir` for question, answer,
  and stop files instead of reconstructing filenames or validity rules.
- API/run handlers should reuse `orch_hub.runs.RunManager` for launch gates,
  per-repo serialization, stop requests, and end classification instead of
  rebuilding subprocess or run-dir behavior in route code.
- API/log handlers should reuse `orch_hub.logs` for log location, bounded
  reads, and SSE framing instead of accepting client paths or rebuilding the
  orchestrator filename convention in route code.
- API/history handlers should reuse `RunManager.recent_runs` /
  `orch_hub.history` rather than reading run dirs directly in route code.
