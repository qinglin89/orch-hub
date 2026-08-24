# orch-hub

A private-network control plane for operating a portfolio of
[Mandrel](https://github.com/qinglin89/mandrel)-managed AI coding workflows
from a Mac, phone, tablet, or another computer.

Mandrel owns the repo-local protocol: task lifecycle, curated project memory,
fresh-context development and review, and the optional scheduler. orch-hub is
the optional layer above it. It discovers registered repositories, starts and
supervises their deployed schedulers, brings human decisions to one dashboard,
and keeps run history outside the repositories themselves.

> [!IMPORTANT]
> orch-hub can remotely launch coding agents that change local files. Treat it
> as a remote code-execution surface. Run it only on localhost or a private
> tailnet, require the bearer token, and never expose it to the public internet.

## What it provides

| Area | Capability |
|---|---|
| Portfolio | Repo discovery, deployment readiness, task inventory, live-run state, and attention queues across every Mandrel-managed repo |
| Run control | Start, gracefully stop, and reattach repo-local orchestrator runs, with at most one live run per repo |
| Human decisions | Answer agent questions and plan gates remotely without turning the workflow back into a terminal session |
| Observability | Live log streaming, bounded log tails, per-session context use, effective launch configuration, and durable run history |
| Task intake | Draft, revise, confirm, or cancel a new task remotely; the target repo is unchanged until confirmation |
| Evaluation | Optional deterministic Layer 1 metrics, Claude-backed Layer 2 evaluation, and a cross-repo report feed |
| Capacity | Display-only Claude and Codex subscription usage that never gates readiness or launch |
| Operations | Mobile-first dependency-free dashboard, launchd resident service, and optional Bark notifications |
| Delivery | One-command compiled install with no Python or checkout, versioned upgrade and rollback, and tag-bound release publication |

The unit of control is a portfolio, not a chat session. Sessions are transient
executors inside a durable task lifecycle; orch-hub shows the state that still
matters after any one conversation has ended.

## Requirements

- macOS for the supported resident-service workflow. The compiled release
  targets Apple Silicon (`darwin-arm64`) and declares macOS 11.0 as its floor.
- Python 3.10+ **only when running from a checkout**. A release install carries
  its own interpreter and needs no Python, pip, or venv. Mandrel is a separate
  program either way, and its end-to-end setup requires Python 3.11+.
- A local Mandrel checkout with an executable `bin/mandrel`.
- At least one repo registered with Mandrel. Follow the
  [Mandrel Quickstart](https://github.com/qinglin89/mandrel#quickstart); before
  starting runs or remote task intake from orch-hub, initialize the repo and
  [bootstrap its scheduler](https://github.com/qinglin89/mandrel#running-the-loop-unattended).
- A clean target-repo worktree when starting a run or remote intake.
- Optional: Tailscale for remote access, Bark for push notifications, and an
  installed/authenticated Claude CLI for remote intake and Layer 2 evaluation.

orch-hub understands Mandrel's current backend/profile launch schema. The
deployed orchestrator remains the source of truth for resolved defaults and
published model/effort choices.

## Quickstart

### From a release: one command, no Python and no checkout

```bash
curl -fLO <url>/orch-hub-0.1.0-darwin-arm64.run
curl -fLO <url>/release.json
curl -fLO <url>/SHA256SUMS
shasum -a 256 -c SHA256SUMS
sh orch-hub-0.1.0-darwin-arm64.run --mandrel /absolute/path/to/mandrel
```

The installer refuses a machine it does not fit, an older macOS than it
supports, and a corrupted download *before* unpacking anything. It then
installs a user-local versioned runtime under `~/.local/orch-hub`, links
`orch-hub` into `~/.local/bin`, configures the launchd service, waits for the
health endpoint, and prints a generated bearer token once if it had to create
one.

```bash
orch-hub launchd status   # label, plist, load state, dashboard URL, health
orch-hub versions         # what is installed, and which is current
```

Upgrading is running a newer installer — the token, Mandrel path, bind address,
data directory, and push endpoint are preserved, and the replaced version stays
installed so `orch-hub rollback` can return to it. `orch-hub uninstall` removes
the service and runtime while keeping hub-owned data. Full operator and
maintainer detail: [installing from a release](docs/release.md).

### From a checkout: development

```bash
git clone https://github.com/qinglin89/orch-hub
cd orch-hub
python3 -m venv .venv
.venv/bin/pip install .
```

Set the Mandrel checkout explicitly and start in the foreground first:

```bash
export ORCH_HUB_MANDREL_PATH=/absolute/path/to/mandrel
export ORCH_HUB_TOKEN="$(openssl rand -hex 32)"
.venv/bin/orch-hub-api
```

Open <http://127.0.0.1:8765>, enter the bearer token, and confirm that the
expected repos appear. The dashboard stores the token in that browser profile's
`localStorage` and sends it only in the `Authorization` header; it never puts a
token in a cookie or URL.

Once the foreground check works, install the resident launchd service:

```bash
.venv/bin/orch-hub launchd deploy \
  --mandrel "$ORCH_HUB_MANDREL_PATH" \
  --token "$ORCH_HUB_TOKEN"
.venv/bin/orch-hub launchd status
```

For remote access, keep the default loopback bind and publish it only inside a
tailnet, for example with `tailscale serve`. See the
[launchd and private-network guide](docs/launchd.md) before enabling remote
access.

## How it fits together

```text
Phone / tablet / browser
        |
        | authenticated HTTP over a private network
        v
orch-hub on the operator's Mac
        |
        | Mandrel registry + filesystem/control-dir contracts
        v
Managed repo N
  .ai/                         curated project snapshot
  .ai-tasks/                   active task state + local archive
  .mandrel/orchestrator/       repo-local scheduler
        |
        | detached dev/review sessions
        v
Claude Code / Cursor / Codex
```

Mandrel remains the source of truth for registered repos and deployed protocol
state. The target repo owns its task and project-memory files. orch-hub owns
only its external service state under `ORCH_HUB_DATA_DIR`, including run,
intake, and evaluation records.

Development slices are bounded to one effective context window. A task that
needs more than one development session carries an explicit plan and structured
handoffs. Ordinary task sessions begin from a bounded slice: eager project
invariants, one task file, and supporting documents routed by that task. At
completion, admitted durable findings update the curated snapshot and the full
task record moves to the local archive.

## Repo discovery and launch readiness

orch-hub reads the same local registry Mandrel maintains; it does not maintain
a second registry or treat GitHub as the source of truth. It prefers
`<mandrel>/bin/mandrel registry list --json` and falls back to
`<mandrel>/.registry/repos.local.json` when the command is unavailable or
fails.

A repo can launch only when all readiness checks pass:

| Check | Pass condition |
|---|---|
| `repo_path_exists` | Registered repo directory exists |
| `manifest_exists` | `.ai-deploy-manifest.json` exists |
| `orchestrator_exists` | `.mandrel/orchestrator/orchestrator.py` exists |
| `venv_python_exists` | Deployed orchestrator Python exists |
| `task_index_exists` | `.ai-tasks/index.md` exists |
| `git_clean` | `git status --porcelain` is empty |
| `mandrel_status` | `mandrel status <repo-path>` exits successfully |

Readiness fails closed. Repo admission is a separate gate: one repo cannot
hold a live run and a non-terminal remote intake at the same time.

## Dashboard and API

The mobile-first dashboard is served by the same FastAPI process. Its stable
surfaces are:

- operations overview and subscription-capacity display;
- repo and task inventory, including readiness and blockers;
- task run controls, questions, session context, and logs;
- paged run history with recorded launch configuration;
- confirmed-only remote task intake;
- per-task evaluation and the global report feed.

The target repo's deployed orchestrator is the authority for launch choices.
DEV and REVIEW can select profiles independently; Custom model/effort choices
are filtered from the orchestrator's own option catalog, and Start fails closed
when that configuration cannot be interpreted safely.

See [Dashboard behavior](docs/dashboard.md) and the
[HTTP API reference](docs/api.md) for the detailed contracts.

## Configuration

Core service configuration:

| Variable | Default | Purpose |
|---|---|---|
| `ORCH_HUB_MANDREL_PATH` | unset; required | Mandrel checkout that owns the registry; first-time launchd deploy may use `--mandrel` instead |
| `ORCH_HUB_TOKEN` | unset | Required bearer token; protected routes fail closed with 503 when absent |
| `ORCH_HUB_HOST` | `127.0.0.1` | Loopback or an explicit Tailscale interface only |
| `ORCH_HUB_PORT` | `8765` | HTTP port |
| `ORCH_HUB_DATA_DIR` | `~/.orch-hub` | Hub-owned run, intake, history, and evaluation state |
| `ORCH_HUB_BARK_URL` | unset | Optional Bark endpoint; contains a device-key secret |

Empty values count as unset. Invalid explicit values such as a malformed port
fail startup instead of silently selecting a fallback. Feature-specific
configuration is documented in [Task evaluation](docs/evaluation.md),
[Claude usage](docs/claude-usage.md), and
[launchd operation](docs/launchd.md). Remote intake uses Claude and defaults to
`claude-opus-5` with `max` effort. Foreground runs can override those turns
with `ORCH_HUB_INTAKE_MODEL` and `ORCH_HUB_INTAKE_EFFORT`; the integrated
launchd deployment currently uses the defaults.

## CLI

```bash
# Managed repos and launch readiness
.venv/bin/orch-hub repos
.venv/bin/orch-hub repos --json

# Resident service lifecycle
.venv/bin/orch-hub launchd deploy --mandrel /absolute/path/to/mandrel
.venv/bin/orch-hub launchd status
.venv/bin/orch-hub launchd restart
.venv/bin/orch-hub launchd stop

# Optional subsystems
.venv/bin/orch-hub notify test
.venv/bin/orch-hub eval doctor
.venv/bin/orch-hub eval catalog verify
```

An installed release is one executable with the same command surface, plus the
verbs a checkout has no use for:

```bash
orch-hub setup --mandrel /absolute/path/to/mandrel   # configure and start
orch-hub upgrade --from /path/to/payload             # or just run a newer .run
orch-hub rollback                                    # back to the previous version
orch-hub versions
orch-hub uninstall
orch-hub serve                                       # foreground service
```

`launchd deploy` / `update` install a checkout's package into `<checkout>/.venv`
and point launchd at it, so they are checkout-only; a release uses `setup` and
`upgrade`. `serve` is how a runtime with no console scripts starts the HTTP
service — a compiled release ships one executable, so `orch-hub-api` exists only
in a source install.

`repos --json` and `GET /api/repos` use the same payload builder. HTTP handlers
delegate to the owning domain services rather than duplicating registry,
readiness, or lifecycle logic.

## Security boundary

- Never bind to `0.0.0.0`, port-forward the service, or place it behind a
  public reverse proxy.
- The bearer token is required even on a tailnet. Tailnet membership is the
  primary network boundary; the token is defense in depth.
- The only public HTTP paths are the minimal `GET /api/health` probe and the
  dashboard shell assets `/`, `/app.js`, and `/styles.css`. Every operational
  data and action route is bearer-protected, and paths outside that explicit
  allowlist fail closed.
- The shell contains no secrets. A browser keeps the token in `localStorage`,
  so use a trusted device and browser profile.
- Starting a run launches the target repo's Mandrel scheduler. That scheduler
  is not a sandbox and may run coding agents with filesystem permission prompts
  disabled. Read Mandrel's
  [Safety](https://github.com/qinglin89/mandrel#safety) section first.
- launchd stop unloads only the hub service. Detached orchestrator runs continue
  until they finish or receive a graceful run-level stop request.

## Operator documentation

- [Installing from a release, and building one](docs/release.md)
- [Dashboard behavior](docs/dashboard.md)
- [HTTP API reference](docs/api.md)
- [launchd deployment, Tailscale, logs, and recovery](docs/launchd.md)
- [Task evaluation and the report feed](docs/evaluation.md)
- [Claude subscription-usage collection](docs/claude-usage.md)

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Tests use temporary directories and injected subprocess runners. They do not
touch the real Mandrel registry or launch real coding-agent sessions.

Building a release is three commands from the checkout, on a host that is
already the target (there is no cross-building a compiled runtime):

```bash
python3 -m orch_hub.release_build     # -> dist/orch-hub-darwin-arm64/
python3 -m orch_hub.release_package   # -> dist/release/{*.run,release.json,SHA256SUMS}
python3 -m orch_hub.release_publish --tag v0.1.0 --to ~/Releases/orch-hub
```

The freezer and the bundled interpreter are pinned, the payload is audited for
first-party source and maintainer-specific literals, packaging the same payload
twice is byte-identical, and publication refuses a tag that does not name the
release, a checkout that is not at that tag, bytes that disagree with their own
inventory, or a destination already holding the tag. See
[the release guide](docs/release.md#part-2--producing-a-release).

## Project status

orch-hub is currently developed as a personal, local operator surface. It is
not a hosted service, a multi-user control plane, or a replacement for Mandrel.
The source repository remains private; an unsigned Apple Silicon binary preview
is available from
[orch-hub-releases](https://github.com/qinglin89/orch-hub-releases).
