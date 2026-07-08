# Running orch-hub as a launchd service

This guide installs the orch-hub HTTP service as a macOS **launchd user
agent**: it starts at login, restarts if it exits, and logs to
`~/Library/Logs/orch-hub/`. The template plist lives at
[`deploy/launchd/com.qinglin89.orch-hub.plist`](../deploy/launchd/com.qinglin89.orch-hub.plist).

## Security model (read first)

orch-hub is a remote code execution control surface — it is being built to
launch orchestrator subprocesses in your repos. The deployment assumptions are
non-negotiable:

- **Never expose the service to the public internet.** No port forwarding, no
  reverse proxies to public hosts, no `0.0.0.0` binds.
- **The network boundary is your Tailscale tailnet** (or plain localhost).
  Only devices on your tailnet should be able to reach the port.
- **The bearer token is required even on the tailnet.** Tailnet membership is
  the primary boundary; the token is defense in depth.
- **The service fails closed without a token.** If `ORCH_HUB_TOKEN` is unset
  or blank, every protected route answers `503`; only `GET /api/health`
  serves. There is no unauthenticated mode, so the plist must set the token.

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

The resident service is configured entirely through environment variables —
the `orch-hub-api` service console script takes no arguments, so the plist's
`EnvironmentVariables` dict is the service configuration surface.

| Variable | Default | Purpose |
|---|---|---|
| `ORCH_HUB_TOKEN` | unset → protected routes answer 503 | Bearer token required by every route except `GET /api/health`. Generate with `openssl rand -hex 32`. |
| `ORCH_HUB_HOST` | `127.0.0.1` | Bind address. Loopback or a Tailscale IP only. |
| `ORCH_HUB_PORT` | `8765` | Bind port. A malformed value fails startup instead of binding a default silently. |
| `ORCH_HUB_AI_NATIVE_DEPLOYMENT` | `/Users/linqing/workplace/github.com/qinglin89/ai-native-development` | ai-native-development checkout that owns the managed-repo registry. |
| `ORCH_HUB_DATA_DIR` | `~/.orch-hub` | Hub-owned run dirs and audit history. |
| `PATH` | launchd's minimal default | Readiness checks invoke `git`; `aii-2` may need Homebrew tooling. The template sets a sane explicit PATH. |

Empty or whitespace-only values count as unset. In the plist, write absolute
paths — launchd does not expand `~`.

## 1. Install the CLI into a venv

From the repository checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

This installs two console scripts:

- `.venv/bin/orch-hub` — operational CLI, including launchd management.
- `.venv/bin/orch-hub-api` — the resident HTTP service launchd runs.

## 2. Deploy or update the launchd agent

The integrated command creates/updates the venv package install, writes a
machine-generated plist, validates it, unloads any currently loaded
`com.qinglin89.orch-hub` user agent, and bootstraps the new one:

```bash
.venv/bin/orch-hub launchd deploy
```

After code changes, run the same flow again, or use the explicit alias:

```bash
.venv/bin/orch-hub launchd update
```

`deploy` / `update` resolve configuration in this order:

| Setting | Resolution |
|---|---|
| Checkout | nearest orch-hub checkout above the current directory, or `--checkout` |
| Token | `--token`, then `ORCH_HUB_TOKEN`, then existing installed plist token, then a generated token printed once |
| ai-native checkout | `--ai-native`, then `ORCH_HUB_AI_NATIVE_DEPLOYMENT`, then built-in default |
| Host/port/data dir | corresponding flags, then environment, then defaults |
| Plist/log paths | `--plist-path` / `--log-dir`, then `~/Library/...` defaults |

Common examples:

```bash
# First deployment; prints a generated token if none exists.
.venv/bin/orch-hub launchd deploy

# Use an explicit token or rotate it.
.venv/bin/orch-hub launchd deploy --token "$(openssl rand -hex 32)"

# Bind directly to the Mac's Tailscale IP instead of loopback.
.venv/bin/orch-hub launchd deploy --host "$(tailscale ip -4)"
```

The generated plist is written with mode `600` because it embeds the bearer
token.

## 3. Check status

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

## 4. Stop the hub service

```bash
.venv/bin/orch-hub launchd stop
```

There is one launchd-managed orch-hub service: the fixed label
`com.qinglin89.orch-hub` in the current user's `gui/$UID` domain. `stop`
unloads that job. It is idempotent: if the job is not loaded, the command
reports that it is already stopped.

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

## 5. Manual fallback

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

## 6. Logs

uvicorn writes access logs to stdout and lifecycle/errors to stderr:

```bash
tail -f ~/Library/Logs/orch-hub/orch-hub.log       # access log
tail -f ~/Library/Logs/orch-hub/orch-hub.err.log   # startup + errors
```

## Troubleshooting

- **Every route except `/api/health` answers 503** — `ORCH_HUB_TOKEN` is
  missing or blank in the plist's `EnvironmentVariables`.
- **Service exits immediately** — check
  `~/Library/Logs/orch-hub/orch-hub.err.log` and the `last exit code` in
  `launchctl print`. A malformed `ORCH_HUB_PORT` fails startup by design; a
  port already in use fails the bind.
- **`Bootstrap failed: 5: Input/output error`** — the label is usually
  already loaded; `launchctl bootout gui/$UID/com.qinglin89.orch-hub` first.
- **No log files appear** — the log directory is missing; launchd does not
  create directories (`mkdir -p ~/Library/Logs/orch-hub`).
- **Empty repo list / readiness errors** — verify
  `ORCH_HUB_AI_NATIVE_DEPLOYMENT` points at a real checkout and that `git` is
  reachable through the plist's `PATH`.
