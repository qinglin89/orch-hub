---
last-updated: 2026-09-05
verified-against: a283a610b718babbc8c0752ffbe81e2429bd61f2
---

# Features: Operations Surfaces

Notifications, launchd service, compiled release delivery, and the future IM
bot surface. Subscription usage: `usage.md`; remote intake: `intake.md`;
dashboard shell/views: `dashboard.md`, `dashboard-views.md`.

## Notifications

Status: implemented

Description: Notify when a pending question is first observed, when a run
reaches a terminal state, when an intake draft/revision is ready for review,
and for actionable semantic outcomes. Bark is the v1 backend.

Modules: notifications, config, runs, intake, semantic_jobs, monitor, launchd,
api, cli.

Implemented behavior:

- `ORCH_HUB_BARK_URL` configures the full Bark endpoint including device key;
  unset or whitespace-only values disable notifications. `build_notifier`
  returns `BarkNotifier` or `None`, with `None` as the only disabled signal.
- `orch-hub notify test` sends one `probe_notification()` directly through the
  configured notifier and exits 0 only after backend acceptance. Missing config
  or delivery failure exits non-zero, the resolved Bark URL is redacted from
  errors, and an unset shell Mandrel path or Bark URL falls back to the selected
  installed launchd plist.
- `Notification` carries title/body/optional group. `BarkNotifier` sends JSON
  to the configured endpoint with a bounded timeout and injectable transport;
  the URL is treated as secret and is never logged.
- The API app builds the default `RunManager`, `IntakeManager`, and
  `SemanticJobManager` with `build_notifier(config)`. Production delivery uses
  manager-owned daemon-thread dispatch; tests can inject synchronous dispatch.
- The FastAPI lifespan starts a resident `RunMonitor` over the app's
  `RunManager` plus registered sweeps; it calls them on a 30s interval, is
  created without starting a thread at app construction, and stops cleanly on
  shutdown. Automatic evaluation uses one sweep to retry held jobs.
- `RunManager._emit` wraps backend delivery so every failure is best-effort:
  exceptions are swallowed after logging only exception type and notification
  title, and run/control-dir/history state is unchanged.
- Terminal notifications fire exactly once when `_refresh` observes a
  live-to-ended transition. The message uses the same `_classify_end` state and
  detail as the dashboard; `completed` comes from that run's frozen final
  session status or anchored `close-out done` line, never later task state.
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
- Semantic-job notifications fire exactly once from `SemanticJobManager`'s
  guarded fresh finalizer after `job.json` is persisted. Explicit
  completed/failed outcomes notify. Payloads carry only repo, task id, outcome,
  and completed evaluation id; backend diagnostics/model output stay out.
  Deferred/queued/running/skipped/error records, duplicate settles, and restart reconciliation
  do not emit. Delivery failure cannot change the settled job record.
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
`orch-hub setup`; `orch-hub-api` / `orch-hub serve`; launchd plist
template/docs.

Implemented behavior:

- `orch-hub launchd deploy` and `update` resolve deployment settings, install
  or refresh the checkout `.venv` package, render the installed plist from the
  checked-in template, validate it, boot out any loaded singleton label, and
  bootstrap the service. After a successful bootout, deploy/update wait until
  `launchctl print` stops reporting the label, then retry transient bootstrap
  exit-5/input-output failures with bounded backoff. The template is parsed
  before venv/pip work.
- The same renderer accepts an installed compiled program: checkout deployment
  runs `<checkout>/.venv/bin/orch-hub-api`; release setup runs
  `<prefix>/current/orch-hub serve`. `program_argv` is the sole launch answer,
  and `ProgramArguments` is rendered as an array rather than reconstructed from
  a checkout.
- `orch-hub setup` verifies a selected installed version and executable
  Mandrel checkout, writes/loads the plist without pip or a checkout, then
  waits for `/api/health`. Setup/upgrade inherit host, port, data dir, Mandrel,
  token, and Bark URL from the installed plist when flags/environment omit
  them; the same fail-closed config parser handles inherited values.
- Deploy/update print no Claude statusLine setup guidance; Claude usage works
  from the CLI cache without any operator settings change.
- Token resolution is explicit `--token`, then `ORCH_HUB_TOKEN`, then an
  existing installed plist token, then a generated token printed once. The
  installed plist is written mode `600` because it embeds the bearer token.
- Mandrel-path resolution is explicit `--mandrel`, current env, legacy env,
  then an existing installed plist. There is no portable default; first deploy
  fails with actionable guidance when none of those sources exists.
- Bark URL resolution is `ORCH_HUB_BARK_URL`, then the existing installed-plist
  value, then disabled. It has no CLI flag; CLI output prints only whether Bark
  push is enabled because the URL embeds a device-key secret.
- `deploy/launchd/com.qinglin89.orch-hub.plist` is the plist-structure source
  of truth. Placeholders cover the checkout, token, host/port,
  mandrel checkout, data dir, optional Bark URL, log dir, and
  `PATH`. Required placeholders fail loudly before writing/loading; unknown
  optional `REPLACE-...` placeholders from a newer template render as `""` so
  an older installed CLI can update a newer checkout when empty means disabled.
  Its legacy checkout-shaped program value is intentional old-CLI/new-template
  compatibility; the current renderer replaces the whole program array.
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

## Compiled Release Delivery

Status: implemented (`darwin-arm64` only)

Description: Build and audit a standalone runtime, install it without Python or
an orch-hub checkout, and package/publish immutable unsigned release artifacts.

Modules: resources, macho, release_audit, release_build, release_python,
install, atomic_dir, release_package, release_publish, cli, launchd, docs,
deploy.

APIs: CLI `serve|version|install|setup|upgrade|rollback|versions|uninstall`;
producer CLIs `python3 -m orch_hub.release_build|release_package|release_publish`;
`.run`, `runtime.json`, `release.json`, `SHA256SUMS`, `publication.json`.

Implemented behavior:

- PyInstaller onedir freezes one executable plus runtime libraries/resources;
  first-party `.py`/`.pyc`, repo/protocol/task state, secrets, and maintainer
  paths are forbidden. The audit opens the executable CArchive and nested PYZ,
  not only filesystem files.
- The build uses a digest-pinned python-build-standalone interpreter and
  measures the produced Mach-O floor; a missing or above-promise floor fails.
  Packaging validates an ASCII dotted floor because downstream readers cannot
  safely enforce an unreadable declaration.
- Clean builds record and freeze an exported commit before slow work; dirty or
  unidentifiable sources still build but are marked unpublishable. The
  `source_revision` flows verbatim from `runtime.json` into each target's
  `release.json` runtime entry.
- Installed layout is `<prefix>/versions/<version>`, atomic `current`, recorded
  `previous`, and a stable command link. Upgrade/rollback select a version and
  restart the fixed service without rewriting data or configuration; uninstall
  proves prefix ownership and stops the service before deletion, preserving
  hub data unless purge is explicitly confirmed.
- The self-extracting POSIX-shell `.run` checks os/arch, minimum macOS, and its
  appended archive digest before unpacking, then delegates install/setup to the
  extracted runtime. `--extract` verifies integrity but intentionally skips the
  host gate.
- Packaging deterministically emits a target-keyed `release.json`, installer,
  and `SHA256SUMS`. Publication re-verifies bytes, tag/version/HEAD/clean-tree
  identity, installer headers, and that every payload's recorded clean source
  revision equals the tag commit; filesystem storage uses atomic `mkdir`
  no-clobber while GitHub publication uses `gh release create --verify-tag`.
  `publication.json` pins the checksum file's digest and source tag/commit.
- Installed-version retention and statusLine reachability remain separate
  tasks.
- Human workflow and refusal tables live in `docs/release.md`; the artifact is
  unsigned, with signing/notarization reserved as an optional future wrapper.

## IM Bot Control

Status: future

Description: Add an IM bot surface, likely Feishu long-connection, for repo/task menus and basic run controls. Web remains the primary v1 answering surface.
