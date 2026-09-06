# Running orch-hub as a launchd service

This guide installs the orch-hub HTTP service as a macOS **launchd user
agent**: it starts at login, restarts if it exits, and logs to
`~/Library/Logs/orch-hub/`. The template plist lives at
[`deploy/launchd/com.qinglin89.orch-hub.plist`](../deploy/launchd/com.qinglin89.orch-hub.plist).

> [!NOTE]
> **This guide is the source-checkout path.** If you installed a compiled
> release, the launchd service is already configured, and the verb that
> configures it is `orch-hub setup` — not `launchd deploy`, which installs a
> checkout's package into `<checkout>/.venv` and points launchd at that. See
> [installing from a release](release.md). Everything else here — the security
> model, the environment variables, `launchd start|stop|restart|status`, stop
> semantics, logs, and troubleshooting — applies to both shapes unchanged.

## Security model (read first)

orch-hub is a remote code execution control surface — it launches orchestrator
subprocesses in your repos. The deployment assumptions are
non-negotiable:

- **Never expose the service to the public internet.** No port forwarding, no
  reverse proxies to public hosts, no `0.0.0.0` binds.
- **The network boundary is your Tailscale tailnet** (or plain localhost).
  Only devices on your tailnet should be able to reach the port.
- **The bearer token is required even on the tailnet.** Tailnet membership is
  the primary boundary; the token is defense in depth.
- **The service fails closed without a token.** If `ORCH_HUB_TOKEN` is unset
  or blank, every protected route answers `503`; only `GET /api/health` and
  the public dashboard shell assets serve. The shell contains no repo data and
  cannot call protected APIs without the token, so the plist must set one.

Two supported network shapes:

1. **Recommended — loopback + `tailscale serve`.** Keep the default
   `ORCH_HUB_HOST=127.0.0.1` and publish the port on the tailnet with
   Tailscale (recent versions: `tailscale serve --bg 8765`; see
   `tailscale serve --help` for your version). Tailscale terminates HTTPS and
   the raw port stays loopback-only.
2. **Direct tailnet bind.** Set `ORCH_HUB_HOST` to the Mac's Tailscale IP
   (`tailscale ip -4`, a `100.x.y.z` address). Traffic is WireGuard-encrypted
   between tailnet devices; the port is unreachable from anywhere else.

## Environment variables

The resident service reads environment variables — the `orch-hub-api` service
console script takes no arguments. The integrated deploy command renders the
following operational settings into the plist's `EnvironmentVariables` dict.

| Variable | Default | Purpose |
|---|---|---|
| `ORCH_HUB_TOKEN` | unset → protected routes answer 503 | Bearer token required by every operational data/action route. Generate with `openssl rand -hex 32`. |
| `ORCH_HUB_HOST` | `127.0.0.1` | Bind address. Loopback or a Tailscale IP only. |
| `ORCH_HUB_PORT` | `8765` | Bind port. A malformed value fails startup instead of binding a default silently. |
| `ORCH_HUB_MANDREL_PATH` | unset; required | Mandrel checkout that owns the managed-repo registry; first-time deploy may use `--mandrel` instead. |
| `ORCH_HUB_DATA_DIR` | `~/.orch-hub` | Hub-owned run dirs and audit history. |
| `ORCH_HUB_BARK_URL` | unset → push disabled | Optional [Bark](https://github.com/Finb/Bark) push endpoint (server + device key, e.g. `https://api.day.app/<device-key>`) for run-lifecycle, pending-question, intake-ready, and semantic-evaluation-outcome notifications. Embeds a device-key secret — keep the plist `chmod 600`. |
| `PATH` | launchd's minimal default | Readiness checks invoke `git`; `mandrel` may need Homebrew tooling. The template sets a sane explicit PATH. |

Empty or whitespace-only values count as unset. In the plist, write absolute
paths — launchd does not expand `~`.

The Python configuration also has feature-specific defaults for usage,
intake, and evaluation. The integrated launchd template currently relies on
those defaults rather than rendering every advanced override; see
[Claude usage](claude-usage.md) and [task evaluation](evaluation.md).
Remote intake defaults to `claude-opus-5` with `max` effort; override those
turns in a foreground service with `ORCH_HUB_INTAKE_MODEL` and
`ORCH_HUB_INTAKE_EFFORT`. The integrated launchd deployment currently uses the
defaults rather than persisting those two overrides.

`ORCH_HUB_MANDREL_PATH` was called `ORCH_HUB_AI_NATIVE_DEPLOYMENT` before the
canonical repository was renamed to mandrel. The old name is still read when
the new one is unset, so a plist installed before the rename keeps working
across a package upgrade; `orch-hub launchd update` rewrites the plist with the
new name. Set only the new name in anything written from now on.

## 1. Install the CLI into a venv

From the repository checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

This installs two primary console scripts:

- `.venv/bin/orch-hub` — operational CLI, including launchd management.
- `.venv/bin/orch-hub-api` — the resident HTTP service launchd runs.

It also installs the optional `.venv/bin/orch-hub-statusline` wrapper, a legacy
fallback carrier for Claude subscription usage ([Claude usage](claude-usage.md)).

A compiled release has none of these console scripts: it ships one executable
that answers `orch-hub serve` where a source install runs `orch-hub-api`, and
`orch-hub statusline` where one runs `orch-hub-statusline`. If that is what you
have, skip to [installing from a release](release.md) and use `orch-hub setup`
in place of sections 1 and 2.

The official operational entry point is `orch-hub launchd ...`. The
`deploy/launchd/` directory holds deployment artifacts, including the plist
template, but does not provide a second shell-script interface. Keeping the
entry point inside the installed package lets the operations flow share the
same config parsing, subprocess wrapper, and tests as the rest of orch-hub.

## 2. Deploy or update the launchd agent

The integrated command creates/updates the venv package install, writes a
machine-generated plist, validates it, unloads any currently loaded
`com.qinglin89.orch-hub` user agent, and bootstraps the new one:

```bash
.venv/bin/orch-hub launchd deploy --mandrel /absolute/path/to/mandrel
```

Later deploys reuse the Mandrel path in the installed plist. After code
changes, run the same flow again, or use the explicit alias:

```bash
.venv/bin/orch-hub launchd update
```

`deploy` / `update` resolve configuration in this order:

| Setting | Resolution |
|---|---|
| Checkout | nearest orch-hub checkout above the current directory, or `--checkout` |
| Token | `--token`, then `ORCH_HUB_TOKEN`, then existing installed plist token, then a generated token printed once |
| Mandrel checkout | `--mandrel`, then `ORCH_HUB_MANDREL_PATH`, then the legacy environment name, then the value in an existing installed plist; first deployment fails with guidance if none is available |
| Host/port/data dir | corresponding flags, then environment, then defaults |
| Plist/log paths | `--plist-path` / `--log-dir`, then `~/Library/...` defaults |
| Bark push URL | `ORCH_HUB_BARK_URL`, then the value already in the installed plist (optional; unset = push disabled). No flag — the secret is supplied through the environment. |

Common examples:

```bash
# First deployment; prints a generated token if none exists.
.venv/bin/orch-hub launchd deploy --mandrel /absolute/path/to/mandrel

# Use an explicit token or rotate it.
.venv/bin/orch-hub launchd deploy --token "$(openssl rand -hex 32)"

# Bind directly to the Mac's Tailscale IP instead of loopback.
.venv/bin/orch-hub launchd deploy --host "$(tailscale ip -4)"

# Enable Bark push (run lifecycle, pending questions, intake drafts, finished
# semantic evaluations). Supplied through the
# environment, not a flag, since the URL embeds a device-key secret; a later
# `update` without it re-exported preserves the value already in the plist.
ORCH_HUB_BARK_URL="https://api.day.app/<device-key>" .venv/bin/orch-hub launchd deploy
```

The generated plist is written with mode `600` because it embeds the bearer
token. Its structure comes from the checked-in template at
[`deploy/launchd/com.qinglin89.orch-hub.plist`](../deploy/launchd/com.qinglin89.orch-hub.plist);
the CLI parses that template with `plistlib`, replaces the `REPLACE-...`
values, verifies no placeholders remain, and writes the installed plist. This
keeps plist structure in one place while the Python code owns install/reload
semantics.

### Verify push delivery

`bark push: enabled` in the deploy output only means a URL is **configured** in
the plist — it does not prove Bark accepts a push. To check delivery end to end:

```bash
.venv/bin/orch-hub notify test
```

This sends one harmless test notification through the configured backend and
exits `0` only after the backend accepts it. A missing configuration or a failed
delivery exits non-zero with a message, and the secret Bark URL is never
printed. Run from a plain shell it reads `ORCH_HUB_BARK_URL` from the
environment, then falls back to the value in the installed launchd plist — so
you can verify the resident service's own configuration without re-exporting the
secret.

## 3. Start, stop, or restart without changing deployment

Use these lifecycle commands when the installed package and plist are already
correct:

```bash
.venv/bin/orch-hub launchd start
.venv/bin/orch-hub launchd stop
.venv/bin/orch-hub launchd restart
```

Command semantics:

| Command | Effect |
|---|---|
| `start` | Bootstrap the installed plist if the job is not loaded. If already loaded, report that state and do nothing. |
| `stop` | Unload the fixed launchd label. If already unloaded, report that state and do nothing. |
| `restart` | `stop` then `start`; does not reinstall package code and does not rewrite the plist. |
| `deploy` | Install/update the local package, rewrite plist from the template, validate it, unload any existing job, and bootstrap. |
| `update` | Alias for `deploy`, named for the code-update workflow. |

There is one launchd-managed orch-hub service: the fixed label
`com.qinglin89.orch-hub` in the current user's `gui/$UID` domain.

## 4. Check status

```bash
.venv/bin/orch-hub launchd status
```

Status reports:

- fixed launchd label (`com.qinglin89.orch-hub`);
- installed plist path and whether it exists;
- whether launchd currently has the job loaded;
- configured dashboard URL;
- public `/api/health` probe result.

For lower-level launchd diagnostics:

```bash
launchctl print gui/$UID/com.qinglin89.orch-hub
```

## 5. Stop semantics

Stopping the launchd service is deliberately different from stopping an
orchestrator run:

- It does not call the run API.
- It does not write any run `stop.flag`.
- It does not signal pids recorded in `~/.orch-hub/runs/.../run.json`.
- It does not wait for active orchestrator runs to finish.

The hub launches orchestrators as detached subprocess sessions
(`start_new_session=True`), so unloading the hub should leave those runs to
continue under their own process/session. To stop a run, use the dashboard or
`POST /api/runs/{run_id}/stop` while the hub is running.

The `launchctl bootout` call itself may briefly wait for launchd to tear down
the hub process, but it does not block on orchestrator completion.

## 6. Manual fallback

The checked-in plist template remains at
[`deploy/launchd/com.qinglin89.orch-hub.plist`](../deploy/launchd/com.qinglin89.orch-hub.plist)
for inspection or manual recovery. If you edit the installed plist yourself,
reload it with:

```bash
launchctl bootout gui/$UID/com.qinglin89.orch-hub
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.qinglin89.orch-hub.plist
```

To remove the launchd registration entirely:

```bash
launchctl bootout gui/$UID/com.qinglin89.orch-hub
rm ~/Library/LaunchAgents/com.qinglin89.orch-hub.plist
rm -r ~/Library/Logs/orch-hub   # optional
```

On a release install, `orch-hub uninstall` does the equivalent and also removes
the installed runtime and command link, keeping hub-owned data unless
`--purge-data --yes` is given. Neither path stops orchestrator runs; see
[stop semantics](#5-stop-semantics).

## 7. Logs

uvicorn writes access logs to stdout and lifecycle/errors to stderr:

```bash
tail -f ~/Library/Logs/orch-hub/orch-hub.log       # access log
tail -f ~/Library/Logs/orch-hub/orch-hub.err.log   # startup + errors
```

## Troubleshooting

- **Every protected data/action request answers 503** — `ORCH_HUB_TOKEN` is
  missing or blank in the plist's `EnvironmentVariables`. The health probe and
  dashboard shell still load, but the shell cannot fetch operational data.
- **Service exits immediately** — check
  `~/Library/Logs/orch-hub/orch-hub.err.log` and the `last exit code` in
  `launchctl print`. A malformed `ORCH_HUB_PORT` fails startup by design; a
  port already in use fails the bind.
- **`Bootstrap failed: 5: Input/output error`** — the label is usually
  already loaded; `launchctl bootout gui/$UID/com.qinglin89.orch-hub` first.
- **No log files appear** — the log directory is missing; launchd does not
  create directories (`mkdir -p ~/Library/Logs/orch-hub`).
- **Empty repo list / readiness errors** — verify
  `ORCH_HUB_MANDREL_PATH` points at a real checkout and that `git` is reachable
  through the plist's `PATH`.
- **No push notifications** — confirm `ORCH_HUB_BARK_URL` is present and
  non-blank in the installed plist's `EnvironmentVariables` (`orch-hub launchd
  deploy` prints `bark push: enabled` when a URL is *configured* — it does not
  verify delivery). Run `orch-hub notify test` to check delivery end to end.
  Delivery is best-effort: a bad URL or unreachable Bark server drops the
  notification and is logged (without the URL) but never affects run state.
