---
last-updated: 2026-07-10
verified-against: 30c1e08a6e706cab2d922417cf0024aa2920c5f7
---

# Features: Dashboard Ops

## Mobile Dashboard

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
  `/api/runs` as a bare leading dot before the repo name: yellow waiting, green
  active non-waiting, gray idle; the state word exists only in title/aria text.
  Ready cards add latest commit time from the shared repo `commit` field plus
  active task count from `/api/tasks` task-inventory rows; not-ready cards keep
  readiness failure reasons.
- All-task and repo-detail views render task inventory from `/api/tasks` and
  `/api/repos/{repo_id}/tasks`, preserving frontmatter-authoritative status,
  `index_present`, and `missing`/`unknown` sentinel semantics.
- Task cards show live run state only as a left-border accent (yellow waiting,
  green active), with no separate dot/word row, kept distinct from the
  frontmatter status badge so protocol `blocked` is not confused with runtime
  `waiting`.
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

## Notifications

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

Status: future

Description: Let the user type a request remotely and have the hub run a one-shot agent session to invoke `/intake-task`, producing a pending task that can then be orchestrated.

## IM Bot Control

Status: future

Description: Add an IM bot surface, likely Feishu long-connection, for repo/task menus and basic run controls. Web remains the primary v1 answering surface.
