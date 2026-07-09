---
last-updated: 2026-07-10
verified-against: 3191b5852164fdff5706f750bd0c47f892cb5709
---

# Features

## Feature Index

| Feature | Status | Key Modules | Key APIs |
|---|---|---|---|
| Repo discovery and readiness | implemented | config, registry, readiness, git_meta, subprocess_utils, cli, api, models | CLI `repos`, CLI `repos --json`, `GET /api/repos`, `GET /api/repos/{repo_id}/commits` |
| Task inventory | implemented | task_store, api, models | `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`, `GET /api/repos/{repo_id}/tasks/{task_id}` |
| HTTP API foundation | implemented | api, config, registry, readiness, task_store, models | public shell routes, `GET /api/health`, repo/task/run/escalation/log/history APIs |
| Control-dir file channel | implemented | control_dir, models | internal file-channel functions |
| Run lifecycle | implemented | runs, api, config, control_dir, models, readiness, task_store | internal `RunManager`; `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{id}`, `POST /api/runs/{id}/stop` |
| Human question relay | implemented | control_dir, runs, api, notifications, web | `GET /api/escalations`, `POST /api/runs/{id}/escalations/{seq}/answer` |
| Log viewing | implemented | logs, runs, api | `GET /api/runs/{id}/logs`, `GET /api/runs/{id}/logs/stream` |
| Mobile dashboard | implemented | api, web | public shell routes plus protected repo/task/run/escalation/log/history/commit APIs, including run start/stop controls |
| Auth and private access | implemented | api, config | default-deny bearer middleware on HTTP routes |
| Notifications | implemented | notifications, config, runs, monitor, launchd, api, cli | Bark delivery backend; `ORCH_HUB_BARK_URL`; CLI `orch-hub notify test` |
| Launchd service | implemented | launchd, cli, config, api, docs, deploy | CLI `orch-hub launchd ...`, `orch-hub-api`, launchd plist/docs |
| Run history and audit | implemented | history, runs, api, models, control_dir | `GET /api/history/runs` |
| Remote intake-task | future | runs or agent adapter, task_store | future task creation endpoint |
| IM bot control | future | notifications or bot adapter, api | future bot callback interface |

## Feature Details

### Repo Discovery and Readiness

Status: implemented

Description: Load managed repos from ai-native-development and report whether each is ready for orchestration. This validates the registry and target-repo deployment assumptions before service/UI work.

Modules: config, registry, readiness, git_meta, models, subprocess_utils, cli.

APIs: `python -m orch_hub.cli repos`, `python -m orch_hub.cli repos --json`,
`GET /api/repos`, `GET /api/repos/{repo_id}/commits`.

Implemented behavior:

- Discovery invokes `<ai-native-development>/aii-2 registry list --json` by absolute path, then falls back to `<ai-native-development>/.registry/repos.local.json` when the command is unavailable or exits non-zero.
- Registry data is a JSON array of `{name, path, manifest}`. Missing fallback file means no repos; invalid JSON or malformed entries raise an operational error.
- Readiness runs seven checks in stable order: repo path, manifest, deployed orchestrator script, orchestrator venv Python, `.ai-tasks/index.md`, clean `git status --porcelain`, and passing `aii-2 status <repo>`.
- Public readiness JSON serializes `ready` and only failed checks as `{name, message}` reasons; ready repos have `checks: []`.
- `git_meta` reads display-only commit history separately from readiness:
  `latest_commit(repo)` returns `CommitInfo | None`, and `read_commits(repo,
  limit)` returns `CommitLog(available, commits)` newest-first with `limit`
  clamped to `1..50`. Missing git, non-git dirs, missing paths, and failed
  `git log` degrade to unavailable/`None` rather than hiding repo content.
- CLI `repos --json` and `GET /api/repos` share `repos_payload`; each repo now
  includes `commit: null` or `{hash, short_hash, committed_ts, subject}`. Human
  CLI output remains unchanged.
- `GET /api/repos/{repo_id}/commits` returns recent commits for repo detail;
  unknown repos are 404, but commit lookup failure is HTTP 200 with
  `available: false` and an empty list.
- Tests cover registry parsing/fallbacks, every requested missing-piece readiness failure, dirty git state, `aii-2 status` failure, config, and CLI output.

### Task Inventory

Status: implemented

Description: Read `.ai-tasks/index.md` and task frontmatter in managed repos. Distinguish task-file statuses from hub run overlay statuses.

Modules: task_store, api, models.

APIs: `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`,
`GET /api/repos/{repo_id}/tasks/{task_id}`.

Implemented behavior:

- `list_repo_tasks(repo)` reads `.ai-tasks/index.md` for active task ids and human summaries, preserving index row order.
- Task cells may be bare ids or full-cell Markdown links such as
  `[id](id.md)`; link targets are reduced to the target basename with one
  trailing `.md` stripped before file lookup and API payloads, so directory
  prefixes cannot steer reads outside `.ai-tasks`.
- The index Status column is not authoritative; each `.ai-tasks/<id>.md` frontmatter supplies task status and blockers.
- Missing `.ai-tasks/index.md` is represented as `index_present=False` with no task rows, distinct from a present-but-empty index.
- Missing listed task files use status `missing`; files with no parseable status use `unknown`.
- `TaskSummary`, `RepoTaskInventory`, and `tasks_payload` provide reusable data shapes for future CLI/API adapters.
- The single-task detail endpoint returns frontmatter-authoritative fields plus
  `present` and raw `content`; raw reads require normalized index membership
  and resolved containment under `.ai-tasks`, so symlinked files outside the
  task dir are not disclosed.

### HTTP API Foundation

Status: implemented

Description: Expose current repo readiness and task inventory data through a
resident FastAPI app without duplicating core logic, protected by bearer-token
auth on every non-health path.

Modules: api, config, registry, readiness, task_store, models.

APIs: `GET /`, `GET /app.js`, `GET /styles.css`, `GET /api/health`,
repo/task/run/escalation/log/history data routes; `python -m orch_hub.api`;
`orch-hub-api`; `uvicorn orch_hub.api:app`.

Implemented behavior:

- `create_app(config=None)` builds a FastAPI app, capturing explicit config for
  tests or `Config.from_env()` by default.
- `GET /api/repos` returns the same shape as CLI `repos --json`, including
  readiness failure reasons and best-effort latest commit metadata.
- Task endpoints return the `tasks_payload` envelope, preserving
  `index_present`, ordered tasks, frontmatter-authoritative status/blockers,
  and task-store sentinel statuses.
- Malformed registry data maps to HTTP 500; unknown repo ids on per-repo task
  lookup map to HTTP 404.
- Per-repo commit history lookup failure returns data (`available:false`), not
  a route error, so repo/task views stay visible when git metadata is absent.
- `GET /api/health` is public and minimal (`{"status": "ok"}`); it no longer
  exposes deployment paths.
- A default-deny middleware protects every path except `/api/health` and the
  three dashboard shell assets. Missing/malformed or invalid credentials return
  401; unset `ORCH_HUB_TOKEN` returns 503 on protected data routes.
- The three dashboard shell assets (`/`, `/app.js`, `/styles.css`) are served
  with `Cache-Control: no-store` so browser heuristic caching cannot hide a new
  build after `orch-hub launchd update`.
- `run()` and `orch-hub-api` read `ORCH_HUB_HOST` / `ORCH_HUB_PORT` and bind
  to `127.0.0.1:8765` by default; any private exposure should remain
  Tailscale-only.

### Run Lifecycle

Status: implemented

Description: Start, stop, list, get, and reattach orchestrator subprocesses.
Each run attaches to one repo and one task. Same-repo live runs are serialized;
cross-repo runs may overlap.

Modules: runs, config, control_dir, models, readiness, task_store.

APIs: internal `RunManager`; `POST /api/runs`, `GET /api/runs`,
`GET /api/runs/{run_id}`, `POST /api/runs/{run_id}/stop`.

Implemented behavior:

- `RunManager.start_run(repo, task_id, extra_args=(), acknowledge_blocked=False)`
  gates launches fail-closed: same-repo live run, task inventory/status from
  `list_repo_tasks`, then readiness from `check_readiness`.
- Task launch validation uses frontmatter-authoritative `TaskSummary.status`;
  unlisted tasks, `missing`/`unknown` task-store sentinels, and unrecognized
  statuses are rejected. `blocked` tasks require explicit acknowledgement.
- Run dirs live outside target repos under `<data_dir>/runs/<run_id>/`, with
  `run.json`, a fresh `control/` dir, and `spawn.out`. `run_id` combines UTC
  timestamp, repo slug, task slug, and a collision suffix when needed.
- Launch command is `/usr/bin/caffeinate -i <repo>/.cursor/orchestrator/.venv/bin/python
  <repo>/.cursor/orchestrator/orchestrator.py <task-id> --control-dir
  <run>/control [extra args]`, with `cwd=<repo>`, stdin from devnull,
  stdout/stderr redirected to `spawn.out`, `start_new_session=True`, and an
  explicit `PATH` that includes common user-installed agent CLI directories
  before the inherited and baseline macOS paths.
- `run.json` is the file-backed source of truth and is atomically rewritten;
  it persists the internal `spawn_out` path so reattached runs and pre-field
  records can still surface startup diagnostics. In-memory `Popen` handles only
  capture exit codes while the hub is the parent process.
- Reattach scans run dirs and probes pid liveness. If a run ended while the hub
  was down, `ended_ts` is persisted with `exit_code=None`.
- Live status is `active` or `waiting` based on pending control-dir questions.
  End status is classified as `completed`, `stopped`, `error`, or `ended` by
  task completion/archival evidence, stop evidence, and then exit code.
- `stop_run` writes `stop.flag` through `control_dir.request_stop` and refuses
  unknown, broken, or already-ended runs.
- HTTP run routes resolve repos through registry discovery, return serialized
  `RunStatus` payloads, expose start/list/get/stop behavior, and map launch/run
  manager errors to 4xx JSON responses (`RepoBusyError` and ended/blocked
  states use 409; unknown runs use 404).

### Control-dir File Channel

Status: implemented

Description: Hub-side file adapter for the deployed orchestrator
`--control-dir` contract. It discovers human-question files, classifies pending
and answered state, writes answer files, and writes stop flags without adding
web/API logic to the adapter.

Modules: control_dir, models.

APIs: internal `list_questions`, `pending_questions`, `write_answer`,
`request_stop`, `DuplicateAnswerError`, and `Question`.

Implemented behavior:

- `list_questions(control_dir)` reads `NNN-question.json` files ordered by
  filename sequence; missing dirs return no questions; malformed question files
  are returned with `Question.error` instead of being skipped.
- Filename sequence is authoritative over payload `seq`; unknown `kind` values
  are surfaced for forward compatibility.
- `pending_questions(control_dir)` treats a question as pending until a same-seq
  answer file passes the orchestrator validity rule: JSON object with a
  non-empty string `answer`. Malformed answers keep the question pending and
  expose `Question.answer_error` for repair.
- `write_answer(question, answer, responder="orch-hub")` strips the answer,
  rejects empty text, mirrors the observed question filename, writes full audit
  fields atomically with a same-dir temp file plus rename, refuses to overwrite
  an existing valid answer, and repairs malformed answer files by atomic
  rewrite.
- `request_stop(control_dir)` writes an idempotent empty `stop.flag` and raises
  `FileNotFoundError` if the run dir does not exist.
- Tests cover discovery, parsing, pending/answered state, stale and malformed
  files, duplicate answer refusal, repair rewrite, stop flag writes, and
  `Question.to_dict()` path exclusion.

### Human Question Relay

Status: implemented

Description: Detect pending control-dir question files, list them remotely with
run/repo context, notify the user, accept answers, write answer files, and
audit the responder.

Modules: control_dir, runs, api, notifications, models.

APIs: `GET /api/escalations`,
`POST /api/runs/{run_id}/escalations/{seq}/answer`.

Implemented behavior:

- `GET /api/escalations` scans live runs only and returns pending
  control-dir questions with `run_id`, `repo_name`, `task_id`, and serialized
  `Question`.
- The answer route identifies questions by `(run_id, seq)` and writes through
  `control_dir.write_answer`, preserving duplicate-answer protection, malformed
  answer repair, empty-answer rejection, and responder audit fields.
- The dashboard task detail view renders inline answer boxes for waiting runs,
  submits with responder `"dashboard"`, and re-renders after the answer file is
  written.
- When a notifier is configured, `RunManager._status` fires a Bark `needs
  input` notification the first time its live-run `pending_questions` scan sees
  each `(run_id, seq)`. Dedup is in-memory, so rescans do not duplicate pushes
  in one hub process and a restart re-notifies a still-open question once.

### Log Viewing

Status: implemented

Description: Read and live-tail deployed orchestrator logs for known runs while keeping filesystem access scoped to persisted run records.

Modules: logs, runs, api.

APIs: `GET /api/runs/{run_id}/logs`,
`GET /api/runs/{run_id}/logs/stream`.

Implemented behavior:

- Run records carry `log_dir`, `task_id`, and `started_ts`, not the exact log
  filename. `logs.locate_log(record)` matches
  `<utc-ts>-<task_id>.log` under the recorded log dir and returns the earliest
  stamp at or after `floor(started_ts)`; broken records with no usable
  `started_ts` fall back to the newest matching file.
- `RunManager.start_run` stamps `started_ts` just before spawning, so the
  child orchestrator's later second-precision log stamp is at or after the
  floored start. This avoids hiding logs when launch crosses a second boundary
  while still rejecting fast re-run logs from earlier runs. Same-wall-clock-
  second re-runs of one task remain indistinguishable by the orchestrator's
  second-precision filenames.
- `GET /api/runs/{run_id}/logs` returns a bounded recent tail: default 200
  lines, hard-capped to 10k lines and a 1 MiB byte window. Missing, rotated, or
  unreadable logs return `present: false` with `detail`, not an HTTP error.
- When no orchestrator log is located, `GET /api/runs/{run_id}/logs` may add a
  nested `diagnostic` sourced from a bounded `spawn.out` tail. Empty or blank
  captures stay `diagnostic: null`, and top-level `present`/`lines` remain
  strictly orchestrator-log semantics.
- `GET /api/runs/{run_id}/logs/stream` returns `text/event-stream`: a bounded
  initial tail, one `data:` event per completed appended line, heartbeat
  comments while idle, and terminal `event: end` on run end or log rotation.
  The tailer uses byte offsets and a raw-bytes pending buffer so a UTF-8
  character split across reads is not corrupted.
- Browser clients must stream with `fetch` plus a `ReadableStream` reader under
  the standard `Authorization: Bearer ...` header. The hub does not accept log
  stream credentials in query strings.

### Mobile Dashboard

Status: implemented

Description: Mobile-first web UI for repo/task inspection, run start/stop,
run observation, answering waiting questions, run history, and logs.

Modules: api, web.

APIs: public `GET /`, `GET /app.js`, `GET /styles.css`; protected
repo/task/run/escalation/log/history HTTP APIs. Log streaming uses the
bearer-authenticated SSE endpoint through `fetch`/ReadableStream, not browser
`EventSource`.

Implemented behavior:

- The dashboard is a vanilla, build-stepless SPA served as an explicit public
  shell. It stores the bearer token in `localStorage`, attaches
  `Authorization: Bearer ...` to every data `fetch`, clears the token on 401,
  and uses no cookie or query-token credential path.
- Repo list renders readiness from `/api/repos` and a tri-state run signal from
  `/api/runs`: yellow waiting, green active non-waiting, gray idle. Ready cards
  add latest commit time from the shared repo `commit` field plus active task
  count from `/api/tasks` task-inventory rows; not-ready cards keep readiness
  failure reasons.
- All-task and repo-detail views render task inventory from `/api/tasks` and
  `/api/repos/{repo_id}/tasks`, preserving frontmatter-authoritative status,
  `index_present`, and `missing`/`unknown` sentinel semantics.
- Task cards show live run state as a left-border accent plus dot signal
  (yellow waiting, green active), kept distinct from the frontmatter status
  badge so protocol `blocked` is not confused with runtime `waiting`.
- Repo detail renders a recent-commits section from
  `GET /api/repos/{repo_id}/commits`; unreadable git shows "Commit history
  unavailable" and readable empty history shows "No commits found".
- Task detail fetches `GET /api/repos/{repo_id}/tasks/{task_id}` for raw task
  content, shows current/most-recent run state, links to logs, and renders
  answer forms for waiting questions from `/api/escalations`.
- Task detail Start opens a cc-codex launch-options dialog and posts
  `extra_args` with fixed `["--backend", "cc-codex"]` plus only changed
  overrides for `--dev-agent`, `--dev-model`, `--dev-effort`,
  `--review-model`, and `--review-effort`; no backend selector is exposed.
  Stop posts to `POST /api/runs/{run_id}/stop`. `blocked` tasks require an
  explicit acknowledgement checkbox; stale/raced starts render the backend 409
  response instead of trusting frontend state.
- Start is hidden or replaced with an explanation when the dashboard already
  sees a live run for the repo, the task file is missing/unknown or otherwise
  non-runnable, or best-effort `/api/repos` readiness reports launch blockers.
- Run history uses `GET /api/history/runs` newest-first.
- Log view uses recent logs for finished runs and fetch/ReadableStream SSE for
  live runs. It renders the `spawn.out` diagnostic when the recent-log route has
  no orchestrator log, and a live stream that ends before any log line falls
  back to that recent-log diagnostic. SSE heartbeats are skipped, blank data log
  lines are preserved, `event: end` closes the stream, navigation/reload/pause
  aborts the active stream, and untrusted log content is appended as text nodes.

### Auth and Private Access

Status: implemented

Description: Protect the service with bearer-token auth and deploy it behind Tailscale/private networking.

Modules: api, config.

Implemented behavior:

- `Config.token` is loaded from `ORCH_HUB_TOKEN`; empty/whitespace-only values
  count as unset.
- Every HTTP path except `/api/health` and the public dashboard shell assets
  requires `Authorization: Bearer <token>` when configured.
- The middleware is pure ASGI rather than Starlette `BaseHTTPMiddleware` so
  streaming responses are not buffered and client disconnects propagate.
- Protected routes fail closed with HTTP 503 when the token is unset; there is
  no unauthenticated mode.
- Auth error responses are static and never echo presented credentials.

### Notifications

Status: implemented

Description: Notify when a pending question is first observed and when a run
reaches a terminal state. Bark is the v1 backend.

Modules: notifications, config, runs, monitor, launchd, api, cli.

Implemented behavior:

- `ORCH_HUB_BARK_URL` configures the full Bark endpoint including device key;
  unset or whitespace-only values disable notifications. `build_notifier`
  returns `BarkNotifier` or `None`, with `None` as the only disabled signal.
- `orch-hub notify test` sends one `probe_notification()` directly through the
  configured notifier and exits 0 only after backend acceptance. Missing config
  or delivery failure exits non-zero, the resolved Bark URL is redacted from
  errors, and an unset shell env falls back to the installed launchd plist.
- `Notification` carries title/body/optional group. `BarkNotifier` sends JSON
  to the configured endpoint with a bounded timeout and injectable transport;
  the URL is treated as secret and is never logged.
- The API app builds the default `RunManager` with `build_notifier(config)`.
  Production delivery uses the run manager's daemon-thread dispatch; tests can
  inject synchronous dispatch.
- The FastAPI lifespan starts a resident `RunMonitor` over the app's
  `RunManager`; it calls `list_runs()` on a 30s interval, is created without
  starting a thread at app construction, and stops cleanly on shutdown.
- `RunManager._emit` wraps backend delivery so every failure is best-effort:
  exceptions are swallowed after logging only exception type and notification
  title, and run/control-dir/history state is unchanged.
- Terminal notifications fire exactly once when `_refresh` observes a
  live-to-ended transition. The message uses the same `_classify_end` state and
  detail as the dashboard; close-out completion is represented by the
  `completed` classification detail for task completion or archival.
- Pending-question notifications fire from `_status` while the run is live,
  reusing the existing `pending_questions` scan. Each question is de-duped by
  in-memory `(run_id, seq)`; answered questions are filtered out before
  notification and never fire.
- API/dashboard reads still refresh run status, but they are no longer required
  for timely pushes because the resident monitor drives the same status path.
- Launchd deploy/update surface `ORCH_HUB_BARK_URL` into the installed plist,
  preserve an existing installed value when the environment omits it, render a
  blank string when disabled, and print only `bark push: enabled/disabled`.

### Launchd Service

Status: implemented

Description: Package, install, update, manage, and document the hub as a
long-lived launchd agent on macOS.

Modules: launchd, cli, config, api, docs, deploy.

APIs: CLI `orch-hub launchd deploy|update|start|stop|restart|status`;
`orch-hub-api`; launchd plist template/docs.

Implemented behavior:

- `orch-hub launchd deploy` and `update` resolve deployment settings, install
  or refresh the checkout `.venv` package, render the installed plist from the
  checked-in template, validate it, boot out any loaded singleton label, and
  bootstrap the service. After a successful bootout, deploy/update wait until
  `launchctl print` stops reporting the label, then retry transient bootstrap
  exit-5/input-output failures with bounded backoff. The template is parsed
  before venv/pip work.
- Token resolution is explicit `--token`, then `ORCH_HUB_TOKEN`, then an
  existing installed plist token, then a generated token printed once. The
  installed plist is written mode `600` because it embeds the bearer token.
- Bark URL resolution is `ORCH_HUB_BARK_URL`, then the existing installed-plist
  value, then disabled. It has no CLI flag; CLI output prints only whether Bark
  push is enabled because the URL embeds a device-key secret.
- `deploy/launchd/com.qinglin89.orch-hub.plist` is the plist-structure source
  of truth. Placeholders cover the checkout, token, host/port,
  ai-native-development checkout, data dir, optional Bark URL, log dir, and
  `PATH`. Required placeholders fail loudly before writing/loading; unknown
  optional `REPLACE-...` placeholders from a newer template render as `""` so
  an older installed CLI can update a newer checkout when empty means disabled.
- `RunAtLoad` and `KeepAlive` keep the service resident; stdout/stderr go to
  `~/Library/Logs/orch-hub/`.
- `start` bootstraps the installed plist if the job is unloaded; `stop` boots
  out the fixed `com.qinglin89.orch-hub` user-agent label; `restart` is stop
  then start without package reinstall or plist rewrite.
- Launchd lifecycle operations are scoped to the hub service only: they do not
  call run APIs, write run `stop.flag` files, inspect run records, or signal
  detached orchestrator subprocesses.
- `status` reports the fixed label, installed plist path/presence, load state,
  configured dashboard URL, program path when readable, plist parse errors,
  launchctl output when unloaded, and the public `/api/health` probe result.
- `docs/launchd.md` documents the integrated deploy/update/lifecycle flow,
  singleton service cardinality, template ownership, token behavior, manual
  fallback commands, log inspection, optional Bark URL config, push delivery
  checks through `notify test`, and Tailscale-only exposure assumptions.

### Run History and Audit

Status: implemented

Description: Persist run lifecycle, stop request, and human answer-audit
metadata for later inspection without copying human answer text into history.

Modules: history, runs, api, models, control_dir.

APIs: `GET /api/history/runs`.

Implemented behavior:

- Per-run history is append-only JSONL at
  `<data_dir>/runs/<run_id>/events.jsonl`, colocated with `run.json`,
  `control/`, and `spawn.out`.
- `RunRecord.task_status` stores the launch-time task-store/frontmatter status
  from `TaskSummary`, so history records carry task context without re-parsing
  task files.
- `RunManager` appends `run_started`, `run_ended`, `stop_requested`, and
  `answer_recorded` events. Stop events are written only for the first
  idempotent stop request; end events include the observed exit code when the
  hub has it.
- Answer audit events record responder and question metadata (`seq`, question
  timestamp/kind/banner/message) but deliberately exclude the answer text.
- `GET /api/history/runs` returns recent runs newest-first with the normal
  serialized run status plus embedded persisted events; `limit` defaults to 50
  and is clamped to `0..200`.
- History readback treats missing `events.jsonl` as empty history and surfaces
  unreadable or malformed event lines as `history_error` events.

### Remote Intake-Task

Status: future

Description: Let the user type a request remotely and have the hub run a one-shot agent session to invoke `/intake-task`, producing a pending task that can then be orchestrated.

### IM Bot Control

Status: future

Description: Add an IM bot surface, likely Feishu long-connection, for repo/task menus and basic run controls. Web remains the primary v1 answering surface.
