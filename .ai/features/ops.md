---
last-updated: 2026-07-24
verified-against: 5b9035236a5c892c44f182f178d8e101a49c800d
---

# Features: Operations Surfaces

Subscription usage display, notifications, launchd service, remote intake,
and the future IM bot surface. Dashboard shell/views: `dashboard.md`,
`dashboard-views.md`.

## Agent Subscription Usage

Status: implemented

Description: Display provider-aware Claude and Codex subscription capacity on
the Operations Overview without conflating quota with repo health or run state.

Modules: usage, statusline, models, config, api, intake_agent, semantic, web.

APIs: protected `GET /api/usage`; internal `UsageService`,
`ClaudeUsageStore`, `parse_claude_stream_usage`, `parse_claude_statusline`,
`read_claude_statusline`, `read_codex_usage`; console script
`orch-hub-statusline`; `ORCH_HUB_CODEX_HOME`.

Implemented behavior:

- `GET /api/usage` returns both providers fail-soft in a normalized payload:
  provider key, availability/detail, plan, windows, used/remaining percentages,
  reset times, observation timestamp, staleness, and optional aggregate credits.
- Codex usage is read only from local session rollout `rate_limits` under the
  configured Codex home; orch-hub never invokes Codex, opens credentials, or
  consumes reset credits.
- Claude usage is read from the hub-owned observation store. The
  operator-installed `orch-hub-statusline` wrapper persists the CLI's statusLine
  `rate_limits` payload to `<data_dir>/usage/claude-statusline.json`; the read
  path ingests it into `ClaudeUsageStore` when newer, so stale last-observed
  figures survive sink deletion/rotation instead of becoming zero usage.
- The best-effort observer remains wired into intake and semantic `claude -p`
  stream-json backends, but Claude Code 2.1.218 exposes no 5h/7d subscription
  windows on that stream; unsupported/absent output records nothing.
- The statusLine writer is opt-in and pass-through: the hub never writes
  `~/.claude/**`, the sink stores only `rate_limits` plus `subscription_type`,
  and wrapped operator statusline argv/stdin/stdout/stderr/exit code are relayed.
- After successful `launchd deploy|update`, the CLI inspects
  `~/.claude/settings.local.json` then `settings.json` read-only and prints
  copy-ready statusLine setup guidance when the effective command lacks
  `orch-hub-statusline`; guidance uses the deployed wrapper path, adds
  `--data-dir` for non-default data dirs, preserves an existing command after
  `-- /bin/sh -c`, is silent when already configured, and never fails deploy.
- The Overview renders separate Claude/Codex cards with percent-first bars,
  reset timing, freshness/stale flags, explicit unknown/unavailable states, and
  aggregate reset-credit availability. Source failures degrade only this
  section and never create Needs attention rows or affect readiness, run state,
  launch, or intake gates.

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
- After the deploy summary, deploy/update may print advisory Claude usage setup
  guidance; this read-only `~/.claude` check is outside the system-mutation path,
  fail-soft, and suppressed when the statusLine already routes through
  `orch-hub-statusline`.
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
