---
last-updated: 2026-08-22
verified-against: 908707b16015269eefab9696bba52fa91feb9ea9
---

# APIs: CLI

## `python -m orch_hub.cli repos`

- Purpose: list managed repos discovered from mandrel with readiness.
- Output: human-readable status lines, followed by failed readiness reasons for non-ready repos.
- Source: registry adapter plus readiness checker.
- Non-goal: does not start orchestrators.
- Operational errors, including malformed config such as a non-integer
  `ORCH_HUB_PORT`, return exit 1 with `error: ...` on stderr.

## `python -m orch_hub.cli repos --json`

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
      },
      "commit": {
        "hash": "0123456789abcdef0123456789abcdef01234567",
        "short_hash": "0123456",
        "committed_ts": "2026-07-09T16:25:00+08:00",
        "subject": "feat(example): update dashboard"
      },
      "housekeeping_pending": true
    }
  ]
}
```

`readiness.checks` contains only failed checks as `{name, message}`; it is empty when `ready` is true. Internally the readiness report keeps all seven check results for callers that need the full picture. `commit` is the latest commit or `null` when git metadata is unavailable; this field is best-effort and does not affect readiness.
`housekeeping_pending` is true when `.ai/.housekeeping-pending` exists in the
managed repo and is also display-only; it does not affect readiness or launch
gating. Human `repos` output does not show commit or housekeeping metadata.

## `orch-hub serve|version`

- `serve [--host HOST] [--port PORT]` starts the API/dashboard in the
  foreground through the same `orch_hub.__main__` dispatch used by the frozen
  binary; it replaces the absent `orch-hub-api` console script in a compiled
  release.
- `version [--json]` reports package version, source/frozen runtime shape, and
  Python version. The self-extracting installer's separate `--version` reports
  only artifact version and target.

## Installed-runtime verbs

- `install --from PAYLOAD [--prefix P] [--bin-dir B] [--no-link] [--json]`
  validates and places one payload, atomically selects it, and optionally links
  `orch-hub` through `current`.
- `setup [--prefix P] [--mandrel M] [--token T] [--host H] [--port N]
  [--data-dir D] [--plist-path P] [--log-dir L]` configures the compiled
  program in launchd and exits non-zero unless health reaches `ok`.
- `upgrade --from PAYLOAD [--prefix P] [--bin-dir B] [--plist-path P]`
  installs/selects a version and reruns setup while inheriting installed service
  configuration; failure leaves the prior version installed and names rollback.
- `rollback [--to VERSION] [--prefix P] [--plist-path P]` selects the recorded
  previous or named installed version, restarts without plist rewrite, and exits
  non-zero on restart/health failure. Bind parsing happens before selection and
  fails closed.
- `versions [--prefix P] [--json]` lists installed versions plus
  current/previous markers.
- `uninstall [--prefix P] [--bin-dir B] [--plist-path P]
  [--purge-data --yes]` proves prefix ownership, stops the singleton service
  before deletion, removes only its own link/plist/runtime, and keeps hub data
  by default. Custom `--bin-dir` must be repeated to remove that link.
- A frozen installed command infers its own non-default prefix; a `.run`
  self-extractor runs from temporary payload state, so later installer runs
  must repeat a non-default `--prefix`/`--bin-dir`.

## `orch-hub launchd deploy|update`

- Purpose: install or refresh the resident macOS launchd service from the
  current checkout.
- Source: `orch_hub.cli` delegates to `orch_hub.launchd.deploy_service`.
- Behavior: resolve deployment settings, optionally create/update `.venv`,
  run local `pip install`, render the installed plist from the checked-in
  template, write it mode `600`, validate it with `plutil`, boot out the fixed
  label if loaded, wait for a successful bootout to leave the launchd domain,
  and bootstrap it with bounded retry for transient exit-5/input-output
  failures. Rendering still happens before install/write; required leftovers
  fail before side effects, while unknown optional placeholders render empty for
  old-CLI/new-template compatibility.
- Post-success output: deploy/update print only the launchd/Bark/token summary;
  they do not inspect Claude settings or request statusLine setup. Claude usage
  is supplied by the CLI cache poller without an operator configuration change.
- Configuration precedence: the Mandrel checkout resolves as `--mandrel`,
  current env, legacy env, then an existing installed plist; there is no
  portable default, so first deploy requires an explicit source. Host, port,
  and data paths resolve flag, environment, installed plist, then default. The
  bearer token resolves as `--token`, `ORCH_HUB_TOKEN`, existing installed
  plist token, then a generated token printed once. Bark URL resolves as
  `ORCH_HUB_BARK_URL`, then existing installed plist value, then disabled; it
  has no CLI flag, and CLI output reports only `bark push: enabled/disabled`
  because the URL embeds a device-key secret.
- Errors: operational failures return exit 1 with `error: ...`; non-transient
  bootout/bootstrap failures are not retried, and missing or invalid templates
  fail before venv/pip work.

## Release producer CLIs

- `python3 -m orch_hub.release_build [--out|--work|--venv|--python ...]
  [--skip-install] [--no-clean] [--allow-findings]
  [--allow-unsupported-macos] [--json]` provisions the pinned interpreter,
  freezes/audits a staged payload, and publishes only a passing candidate.
- `python3 -m orch_hub.release_package [--payload P ...]
  [--out|--work|--template ...] [--allow-findings]
  [--allow-unsupported-macos] [--json]` emits the deterministic target-keyed
  artifact set. A payload may be repeated for multiple targets; the shipped
  target is darwin-arm64.
- `python3 -m orch_hub.release_publish --tag v<version> --to DEST
  [--release|--work ...] [--json]` re-verifies a packaged set and publishes it
  once to `<directory>/<tag>/` or `github:<owner>/<repo>`. It requires tag =
  clean HEAD and checkout/release versions to agree; GitHub requires `gh` and a
  remote tag because `--verify-tag` is mandatory.

## `orch-hub launchd start|stop|restart`

- Purpose: manage the installed launchd user-agent lifecycle without
  reinstalling package code or rewriting the plist.
- Scope: commands target only `gui/$UID/com.qinglin89.orch-hub`.
- Semantics: `start` bootstraps the installed plist when unloaded; `stop`
  bootouts the fixed label and treats an absent job as already stopped;
  `restart` is stop then start.
- Non-goal: these commands do not call run APIs, write run `stop.flag` files,
  inspect run records, or signal detached orchestrator subprocesses.

## `orch-hub launchd status`

- Purpose: report launchd label/config hints and service health without manual
  launchctl commands.
- Output: fixed label, installed plist path/presence, loaded state, program
  path when readable, dashboard URL, plist parse error when present, launchctl
  output when unloaded, and the public `/api/health` probe result.

## `orch-hub notify test`

- Purpose: verify push delivery, not merely that a Bark URL is configured.
- Source: `orch_hub.cli` resolves config, then delivers through
  `notifications.build_notifier(config)`.
- Behavior: sends one harmless `probe_notification()` and exits 0 only after the
  backend accepts delivery. When the current/legacy Mandrel path or
  `ORCH_HUB_BARK_URL` is unset in the shell, it recovers that configuration from
  the installed launchd plist; `--plist-path` overrides that lookup for tests or
  alternate installs.
- Errors: missing config exits 1 with a clear message; backend failures exit 1
  with the failure type/detail after redacting the resolved Bark URL/device key.
- Non-goal: does not call `RunManager` or write run records, history events,
  task files, or control-dir files.

## `orch-hub eval doctor`

- Purpose: smoke-check whether optional Layer 2 semantic evaluation is enabled,
  configured, and runnable.
- Output: human-readable backend, model, schema version, retention setting,
  availability, and detail.
- Behavior: resolves `Config.from_env()`, fails closed when evaluation is
  disabled, and calls `ClaudeSemanticBackend.doctor()` to resolve `claude` on
  the augmented agent PATH without launching a model turn.
- Exit code: `0` only when evaluation is enabled and the semantic backend is
  available; disabled, unavailable, or malformed config exits non-zero.
- Non-goal: does not generate evaluation artifacts, call the model, inspect
  repos, start/stop runs, or gate run-control flows.

## `orch-hub eval catalog verify [--json]`

- Purpose: diagnose `<data-dir>/report-catalog/catalog.jsonl` and the artifact
  sets its entries address without changing either.
- Output: catalog line/entry counts, located parse/duplicate/identity defects,
  and retained/pruned/incomplete/damaged/unreadable/unaddressable artifact
  counts; human output includes repair paths and `--json` emits the stable model
  shape.
- Exit code: `0` iff the catalog can serve a feed; duplicate `seq`/`report_key`
  or an unreadable catalog exits non-zero, while per-entry artifact defects stay
  visible without claiming traversal is down.
- Non-goal: performs no evaluation or model call and never repairs automatically.

## `orch-hub eval catalog repair [--json] [--confirm]`

- Purpose: plan or explicitly apply the only safe catalog rewrite.
- Behavior: without `--confirm`, reports a dry-run plan. A confirmed repair may
  remove only byte-identical repeated lines and an unparseable torn tail; it
  refuses distinct identity conflicts, preserves every published `seq` and
  `report_key`, and writes a collision-safe timestamped backup before atomic
  replacement under the catalog append lock.
- Exit code: follows whether the on-disk catalog serves the feed after the
  attempt; refusal, an unapplied plan against ambiguity, disabled evaluation,
  malformed config, or a reported catalog-domain failure returns `1`. Such
  failures print one `error:` line on stderr and no partial JSON result.
- Non-goal: does not alter evaluation artifacts or run at service startup.

## `orch-hub-statusline`

- Purpose: legacy optional Claude Code `statusLine.command` wrapper that can
  still contribute the CLI's local subscription `rate_limits` payload.
- Options: `--sink <path>` overrides the sink file; `--data-dir <dir>` writes
  under `<dir>/usage/claude-statusline.json`; `--print-sink` prints the resolved
  sink and reads no stdin. Everything after `--` is the operator's own statusline
  command argv.
- Behavior: reads stdin bytes once, best-effort writes only `rate_limits` plus
  `subscription_type` to the bounded hub sink, then passes the same stdin to the
  wrapped command with inherited stdout/stderr and the wrapped exit code. No
  wrapped command prints nothing and exits 0; an unrunnable wrapped command exits
  127 without a partial status line.
- Non-goal: does not edit `~/.claude/**`, start Claude turns, call provider
  endpoints, read credentials/accounts, or gate any hub operation; new
  installations do not require it.
