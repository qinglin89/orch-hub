---
last-updated: 2026-07-20
verified-against: 1ab6f87d248bdb0d7c350186aee3cf9659b848b5
---

# Features: Dashboard Ops

## Mobile Dashboard

Status: implemented

Description: Mobile-first web UI for repo/task inspection, run start/stop,
run observation, answering waiting questions, run history, and logs.

Modules: api, web.

APIs: public `GET /`, `GET /app.js`, `GET /styles.css`; protected
repo/task/run/escalation/log/history/intake HTTP APIs, including the run-scoped
task file route. Log streaming uses the bearer-authenticated SSE endpoint
through `fetch`/ReadableStream, not browser `EventSource`.

Implemented behavior:

- The dashboard is a vanilla, build-stepless SPA served as an explicit public
  shell. It stores the bearer token in `localStorage`, attaches
  `Authorization: Bearer ...` to every data `fetch`, clears the token on 401,
  and uses no cookie or query-token credential path.
- Accessibility is route-level infrastructure: every routed view starts with one
  visible `h1.page-title`; router/full-view repaints mirror it into
  `document.title` and move focus there; `#view` is not a live region, with
  announcements scoped to status, alert, and filter-count regions; shared focus
  styling and minimum target sizes cover dashboard controls.
- The Operations Overview is the default authenticated landing route
  (`#/overview`). It remains client-side over existing protected APIs,
  parallel-fetching repos, tasks, live runs, escalations, and run history into
  summary metrics, prioritized attention items, current activity, and recent
  outcomes. Waiting questions, readiness failures, housekeeping, live runs, and
  ended runs deep-link to existing task/repo/log views.
- Overview fetches are per-source best-effort: non-401 failures degrade only
  the affected panel to an unavailable state, while 401 still returns to the
  login gate. Its route-scoped refresh uses a chained timeout, registers its
  cancellation handle before the initial load, drops stale paints after
  navigation, and tears down through the router so requests never overlap or
  keep polling after the view is left.
- Route generation is the stale-view ownership guard: router bumps it on every
  entry; routed handlers, `rerender`, and intake action success/error paths
  capture before their own await/POST and skip stale paints, landing, error
  views, or handoffs. Non-router re-renders with no navigation capture the live
  generation and still paint; feature-level poll/stream/refresh handles still
  own their own cancellation.
- Repo list renders readiness from `/api/repos` and a tri-state run signal from
  `/api/runs` as a bare leading dot before the repo name: yellow waiting, green
  active non-waiting, gray idle; the state word exists only in title/aria text.
  Ready cards add latest commit time from the shared repo `commit` field plus
  active task count from `/api/tasks` task-inventory rows; not-ready cards keep
  readiness failure reasons. Cards render a compact yellow "Housekeeping
  pending" warning when `/api/repos` reports `housekeeping_pending`.
- All-task and repo-detail views render task inventory from `/api/tasks` and
  `/api/repos/{repo_id}/tasks`, preserving frontmatter-authoritative status,
  `index_present`, and `missing`/`unknown` sentinel semantics. The all-tasks
  view filters the fetched payload client-side by repo/status plus
  case-insensitive text over task id, summary, repo, status, and blockers;
  it shows visible-of-total counts, has one Clear action, distinguishes empty
  inventory from no matches, and keeps missing-index cards visible by default.
- Task cards show live run state only as a left-border accent (yellow waiting,
  green active), with no separate dot/word row, kept distinct from the
  frontmatter status badge so protocol `blocked` is not confused with runtime
  `waiting`.
- Repo detail renders a recent-commits section from
  `GET /api/repos/{repo_id}/commits`; unreadable git shows "Commit history
  unavailable" and readable empty history shows "No commits found". It also
  renders the full housekeeping warning near the top when the marker is pending.
- Task detail fetches `GET /api/repos/{repo_id}/tasks/{task_id}` for raw task
  content, shows current/most-recent run state, links to logs, renders live
  per-session context strips from `GET /api/runs/{run_id}/sessions` for active
  runs, and renders answer forms for waiting questions from `/api/escalations`.
  Waiting-question forms send explicit answer modes: ordinary questions show
  Discuss/Answer, plan gates show Feedback/Confirm, Confirm allows empty input
  for one-tap approval, and guard 400 details appear inline.
- Task detail Start opens a cc-codex launch-options dialog and posts
  `extra_args` with fixed `["--backend", "cc-codex"]` plus only changed
  overrides for `--dev-agent`, `--dev-model`, `--dev-effort`,
  `--review-model`, and `--review-effort`; a default-off plan-gate checkbox
  appends a single bare `--plan-gate` token when enabled. No backend selector is
  exposed. Stop posts to `POST /api/runs/{run_id}/stop`. `blocked` tasks require
  an explicit acknowledgement checkbox; stale/raced starts render the backend
  409 response instead of trusting frontend state.
- Start is hidden or replaced with an explanation when the dashboard already
  sees a live run for the repo, the task file is missing/unknown or otherwise
  non-runnable, or best-effort `/api/repos` readiness reports launch blockers.
- Run history uses `GET /api/history/runs` newest-first, grouped client-side by
  `(repo, task)`. Repo/state/task-id filters compose client-side over the
  fetched payload before grouping; the view shows filtered group/run counts,
  distinguishes empty history from no matches, and lets task groups collapse
  while keeping each group's header and rollup visible. Each task group shows
  per-run session strips from persisted snapshots plus a rollup of observed dev
  sessions vs frontmatter `session-est`, peak per-session context, and summed
  run wall-clock from `started_ts`/`ended_ts`.
- Log view uses recent logs for finished runs and fetch/ReadableStream SSE for
  live runs. It renders the `spawn.out` diagnostic when the recent-log route has
  no orchestrator log, and a live stream that ends before any log line falls
  back to that recent-log diagnostic. SSE heartbeats are skipped, blank data log
  lines are preserved, `event: end` closes the stream, navigation/reload/pause
  aborts the active stream, and untrusted log content is appended as text nodes.
- A run-scoped task view at `#/runs/{run_id}/task` fetches
  `GET /api/runs/{run_id}/task`, shows the active/archived source, and is used
  by the log view's task back-link so run task links still open after close-out
  archives the task. The Tasks page remains active-task-only.
- The remote-intake review view polls `GET /api/intakes/{id}` while an intake
  status is `drafting`, `revising`, or `confirming`; it shows live status,
  elapsed time, and a safe-to-navigate-away note, stops polling on resting
  statuses, and tears down the single poll handle on navigation like live logs.

## Notifications

Status: implemented

Description: Notify when a pending question is first observed, when a run
reaches a terminal state, and when an intake draft/revision is ready for
review. Bark is the v1 backend.

Modules: notifications, config, runs, intake, monitor, launchd, api, cli.

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
- The API app builds the default `RunManager` and `IntakeManager` with
  `build_notifier(config)`. Production delivery uses manager-owned
  daemon-thread dispatch; tests can inject synchronous dispatch.
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
  notification and never fire. Plan-gate pending-question pushes replace the
  full plan report with compact text, while non-plan-gate prompts are bounded;
  complete question content remains available in the dashboard, control-dir
  question file, and orchestrator log.
- Intake-ready notifications fire exactly once from `IntakeManager` draft/revise
  success finalizers after the record is persisted to `awaiting_review`.
  Payloads carry only repo, intake id, and draft/revision iteration metadata;
  failed, cancelled/discarded, confirmed, and still-running intake turns do not
  emit. A `None` notifier is a no-op and backend failures are swallowed without
  changing `intake.json` or `events.jsonl`.
- API/dashboard reads still refresh run status, but they are no longer required
  for timely pushes because the resident monitor drives the same status path.
- Launchd deploy/update surface `ORCH_HUB_BARK_URL` into the installed plist,
  preserve an existing installed value when the environment omits it, render a
  blank string when disabled, and print only `bark push: enabled/disabled`.

## Launchd Service

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

## Remote Intake-Task

Status: implemented

Description: Let the user type a request remotely, review the full proposed
task file and index entry, revise or cancel it, and only on explicit
confirmation write the new task into the target repo.

Modules: intake, intake_agent, api, web, models, readiness, task_store, notifications.

APIs: protected `POST /api/intakes`, `GET /api/intakes`,
`GET /api/intakes/{id}`, `GET /api/intakes/{id}/history`,
`POST /api/intakes/{id}/revise`, `POST /api/intakes/{id}/confirm`, and
`POST /api/intakes/{id}/cancel`.

Implemented behavior:

- `IntakeManager` owns hub state under `<ORCH_HUB_DATA_DIR>/intake/<id>/`
  (`intake.json` plus metadata-only `events.jsonl`) and enforces one in-flight
  intake per repo after readiness passes.
- Draft and revise turns produce hub-owned proposals only. They are not active
  tasks, are not returned by task inventory, and are not launchable. Draft,
  revise, and confirm turns run off the manager lock after persisting transient
  status; clients observe the settled outcome by polling the intake record.
- Successful draft/revise turns that settle to `awaiting_review` emit one
  metadata-only intake-ready Bark notification when configured; failures,
  cancellations/discards, and confirmation do not notify.
- The dashboard repo-detail view starts intake; the intake review view shows the
  complete proposed task file, index entry, notes, status, and audit events with
  Confirm, Request changes, and Cancel outcomes. In-flight turns show a live
  working panel with elapsed time; confirmed intakes route to the created task
  detail so the existing Start control launches it.
- The concrete default backend is `ClaudeIntakeAgent`, a headless Claude CLI
  adapter that drives `/intake-task` with structured draft/confirm envelopes,
  session resume, read-only `--permission-mode plan` draft/revise turns, and a
  write-permitted confirm turn.
- Confirmation is not trusted on agent exit code alone. The manager records
  `confirmed` only after the created task appears through `list_repo_tasks` with
  frontmatter status `pending` or `blocked`; no-write, partial-write,
  invalid-status, agent-failure, cancellation, and lost-session outcomes record
  `failed`/`cancelled` and do not surface a launchable task. In-process
  failed/unverified confirms roll back the proposed task file and `index.md`.
- Startup reconciliation marks orphaned persisted transient intakes as `failed`
  so they stop holding the repo intake slot; it does not mutate target repos.

## IM Bot Control

Status: future

Description: Add an IM bot surface, likely Feishu long-connection, for repo/task menus and basic run controls. Web remains the primary v1 answering surface.
