---
last-updated: 2026-07-24
verified-against: 272dcc0657cc2b1943fd3b8b559de72cff468143
---

# APIs: CLI

## `python -m orch_hub.cli repos`

- Purpose: list managed repos discovered from ai-native-development with readiness.
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
- Configuration precedence: CLI flag, environment, default for paths/host/port;
  bearer token specifically resolves as `--token`, `ORCH_HUB_TOKEN`, existing
  installed plist token, then generated token printed once. Bark URL resolves
  as `ORCH_HUB_BARK_URL`, then existing installed plist value, then disabled;
  it has no CLI flag, and CLI output reports only `bark push:
  enabled/disabled` because the URL embeds a device-key secret.
- Errors: operational failures return exit 1 with `error: ...`; non-transient
  bootout/bootstrap failures are not retried, and missing or invalid templates
  fail before venv/pip work.

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
  backend accepts delivery. If `ORCH_HUB_BARK_URL` is unset in the shell, it
  falls back to the installed launchd plist's value; `--plist-path` overrides
  that lookup for tests or alternate installs.
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

## `orch-hub-statusline`

- Purpose: operator-installed Claude Code `statusLine.command` wrapper that
  captures the CLI's local subscription `rate_limits` payload for the Overview.
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
  endpoints, read credentials/accounts, or gate any hub operation.
