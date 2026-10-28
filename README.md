# orch-hub

**Run and supervise your AI coding agents across every repo from one browser
tab.** Start a task and let Claude Code, Codex, or Cursor carry it through
development and review in fresh sessions; answer their questions from your
phone; see every run, log, and decision afterwards.

orch-hub is the dashboard for [Mandrel](https://github.com/qinglin89/mandrel),
a protocol that keeps long-running AI coding work bounded: one task file as
ground truth, curated project memory, and a deterministic scheduler outside the
model. Mandrel works on its own; orch-hub puts a portfolio of Mandrel-managed
repos behind one private web UI and keeps run history outside the repos.

[![orch-hub operations overview across a synthetic three-repo portfolio](https://raw.githubusercontent.com/qinglin89/orch-hub-releases/main/assets/visual-demo/v0.1.0/01-overview.png)](https://github.com/qinglin89/orch-hub-releases#see-the-workflow)

## Install (macOS on Apple Silicon)

**Tell your coding agent** (Claude Code, Codex, Cursor):

```text
Set up orch-hub for me by following https://github.com/qinglin89/orch-hub/blob/dev/docs/agent-setup.md
```

It installs everything, asks which repos you want orch-hub to manage, prepares
them, and opens the dashboard.

**Or run it yourself:**

```bash
curl -fsSL https://raw.githubusercontent.com/qinglin89/orch-hub/dev/install.sh | sh
```

One command checks your Mac, installs Mandrel (or reuses yours) with a Python
3.11+ for it (from Homebrew when one is missing), downloads and verifies the
orch-hub release, starts it as a background service on `127.0.0.1:8765`, and
opens the dashboard with the sign-in token on your clipboard. No sudo, nothing
system-wide; `sh install.sh --dry-run` prints the plan first if you would rather
[read the script](install.sh) and run it yourself. Then add a repo: the
dashboard shows how, or tell your agent

```text
Add my repo at /path/to/your-repo to orch-hub by following https://github.com/qinglin89/orch-hub/blob/dev/docs/agent-setup.md#add-a-repo
```

```bash
orch-hub open             # the dashboard, with the token on the clipboard
orch-hub launchd status   # is the service loaded and healthy?
```

Upgrading is running the installer again: the token, Mandrel checkout, bind
address, and data directory are kept, and `orch-hub rollback` returns to the
version an upgrade replaced. The step-by-step manual install, the options, and
uninstalling are in [the release guide](docs/release.md).

> [!IMPORTANT]
> orch-hub launches coding agents that change files on your Mac: treat it as a
> remote code-execution surface. It listens on localhost; reach it from a phone
> only over a private network such as Tailscale, keep the bearer token secret,
> and never expose it to the internet. See [Security boundary](#security-boundary).

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

- macOS 11+ on Apple Silicon for the one-line install and the resident
  service. Elsewhere, [run from source](#run-from-source) in the foreground.
- Git (the Xcode Command Line Tools: `xcode-select --install`).
- Python 3.11+ for Mandrel. The installer offers Homebrew's `python@3.13` when
  none is found; orch-hub itself carries its own interpreter.
- `jq`, which Mandrel's agent hooks call (the installer adds it with Homebrew
  when it can).
- At least one coding-agent CLI, logged in: Claude Code, Codex, or Cursor.
  Remote task intake and Layer 2 evaluation use Claude Code. The scheduler's
  default pairs Claude for development with Codex for review; with only one
  CLI, pick it for both roles when you start a run.
- Per managed repo: Git history and a clean working tree, Mandrel deployed,
  and `/ai-init` run once. [The agent guide](docs/agent-setup.md#add-a-repo)
  walks through it, and the dashboard names the next step for any repo that is
  not ready yet.
- Optional: Tailscale for remote access, Bark for push notifications.

orch-hub understands Mandrel's current backend/profile launch schema. The
deployed orchestrator remains the source of truth for resolved defaults and
published model/effort choices.

## Run from source

Running from a checkout needs Python 3.10+ for orch-hub itself (a release
carries its own interpreter):

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

Readiness fails closed. Every failed check carries a one-line `hint` with the
command or step that fixes it, in `orch-hub repos --json`, `GET /api/repos`,
and on the dashboard. Repo admission is a separate gate: one repo cannot hold a
live run and a non-terminal remote intake at the same time.

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

# Dashboard hand-off on this Mac: never puts the token in a URL or a log
.venv/bin/orch-hub open                 # open the dashboard, token on the clipboard
.venv/bin/orch-hub token --copy         # just copy the token again

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

`repos`, `token`, and `open` read the installed service's configuration from
its plist, so they work from a fresh terminal without exporting anything.
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
python3 -m orch_hub.release_publish --tag v0.2.0 --to github:qinglin89/orch-hub
```

The freezer and the bundled interpreter are pinned, the payload is audited for
first-party source and maintainer-specific literals, packaging the same payload
twice is byte-identical, and publication refuses a tag that does not name the
release, a checkout that is not at that tag, bytes that disagree with their own
inventory, or a destination already holding the tag. See
[the release guide](docs/release.md#part-2--producing-a-release).

## Project status

orch-hub is preview software, developed as a personal, local operator surface:
a single-operator control plane on your own Mac, not a hosted service, a
multi-user platform, or a replacement for Mandrel. Release binaries are
unsigned Apple Silicon builds published on
[GitHub Releases](https://github.com/qinglin89/orch-hub/releases). Issues and
feedback are welcome.

## License

[Apache-2.0](LICENSE).
