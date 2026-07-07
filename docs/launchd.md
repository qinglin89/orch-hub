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

The service is configured entirely through environment variables — the
console script takes no arguments, so the plist's `EnvironmentVariables` dict
is the single configuration surface.

| Variable | Default | Purpose |
|---|---|---|
| `ORCH_HUB_TOKEN` | unset → protected routes answer 503 | Bearer token required by every route except `GET /api/health`. Generate with `openssl rand -hex 32`. |
| `ORCH_HUB_HOST` | `127.0.0.1` | Bind address. Loopback or a Tailscale IP only. |
| `ORCH_HUB_PORT` | `8765` | Bind port. A malformed value fails startup instead of binding a default silently. |
| `ORCH_HUB_AI_NATIVE_DEPLOYMENT` | `/Users/linqing/workplace/github.com/qinglin89/ai-native-development` | ai-native-development checkout that owns the managed-repo registry. |
| `ORCH_HUB_DATA_DIR` | `~/.orch-hub` | Reserved for future run dirs and audit history; nothing writes here yet. |
| `PATH` | launchd's minimal default | Readiness checks invoke `git`; `aii-2` may need Homebrew tooling. The template sets a sane explicit PATH. |

Empty or whitespace-only values count as unset. In the plist, write absolute
paths — launchd does not expand `~`.

## 1. Install orch-hub into a venv

From the repository checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

This installs the `orch-hub-api` console script at
`<checkout>/.venv/bin/orch-hub-api` — the path the plist runs.

## 2. Run the service manually first

Always verify the service runs in the foreground before handing it to
launchd; startup errors are much easier to read in a terminal:

```bash
export ORCH_HUB_TOKEN="$(openssl rand -hex 32)"
echo "$ORCH_HUB_TOKEN"   # keep it for the plist and your clients
.venv/bin/orch-hub-api
```

From another terminal:

```bash
curl -s http://127.0.0.1:8765/api/health
# {"status":"ok"}
curl -s -H "Authorization: Bearer $ORCH_HUB_TOKEN" http://127.0.0.1:8765/api/repos
```

Stop it with `Ctrl-C` once both calls answer.

## 3. Install the launchd agent

```bash
mkdir -p ~/Library/Logs/orch-hub   # launchd creates log files, not directories
cp deploy/launchd/com.qinglin89.orch-hub.plist ~/Library/LaunchAgents/
```

Edit `~/Library/LaunchAgents/com.qinglin89.orch-hub.plist` and replace every
`REPLACE-...` placeholder:

| Placeholder | Replace with |
|---|---|
| `REPLACE-ORCH-HUB-CHECKOUT` | absolute path of this repository checkout |
| `REPLACE-BEARER-TOKEN` | the token from step 2 (or a fresh `openssl rand -hex 32`) |
| `REPLACE-AI-NATIVE-DEVELOPMENT-CHECKOUT` | absolute path of the ai-native-development checkout |
| `REPLACE-HOME` | your home directory, e.g. `/Users/you` |

The plist embeds the bearer token, so restrict and validate it, then load:

```bash
chmod 600 ~/Library/LaunchAgents/com.qinglin89.orch-hub.plist
plutil -lint ~/Library/LaunchAgents/com.qinglin89.orch-hub.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.qinglin89.orch-hub.plist
```

With `RunAtLoad` the service starts immediately and again at every login.

## 4. Manage the service

```bash
# Status (state, pid, last exit code)
launchctl print gui/$UID/com.qinglin89.orch-hub

# Restart now (also starts it if stopped)
launchctl kickstart -k gui/$UID/com.qinglin89.orch-hub

# Stop and unload (KeepAlive means a plain kill just respawns it)
launchctl bootout gui/$UID/com.qinglin89.orch-hub
```

After editing the plist (token rotation, port change), apply it with a
reload — launchd does not re-read a loaded plist:

```bash
launchctl bootout gui/$UID/com.qinglin89.orch-hub
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.qinglin89.orch-hub.plist
```

Uninstall completely:

```bash
launchctl bootout gui/$UID/com.qinglin89.orch-hub
rm ~/Library/LaunchAgents/com.qinglin89.orch-hub.plist
rm -r ~/Library/Logs/orch-hub   # optional
```

## 5. Logs

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
