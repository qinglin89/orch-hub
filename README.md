# orch-hub

A long-lived local control plane for AI coding orchestrators. It runs on your
Mac and is meant to be driven remotely (iPhone/iPad or another machine) over a
private network, coordinating the per-repository orchestrator programs that run
dev/review loops over `.ai-tasks/`.

This repository implements the core application slices — repo discovery, launch
readiness, and task inventory — plus a first **HTTP service foundation** that
exposes them behind **bearer-token auth**. The remaining surfaces (orchestrator
runs, question relay, logs, notifications, dashboard) are planned and
intentionally not built yet.

## What this slice does

- Discovers managed repos from **ai-native-development** — the same registry
  the deployment tooling already maintains. orch-hub does not keep its own repo
  registry and does not treat GitHub as the source of truth.
- Reports, per repo, whether it is **ready to launch** its deployed
  orchestrator. Readiness fails closed: any missing piece blocks launch.

### Discovery source

1. Preferred: `aii-2 registry list --json` (invoked by absolute path inside the
   ai-native-development checkout).
2. Fallback (command unavailable or non-zero exit):
   `<ai-native-development>/.registry/repos.local.json`.

Both sources return the same shape — a JSON array of `{name, path, manifest}`.
A missing fallback file yields an empty repo list; malformed JSON is a hard
error.

### Readiness checks

For each managed repo, in a stable order:

| Check | Passes when |
|---|---|
| `repo_path_exists` | the repo directory exists |
| `manifest_exists` | `.ai-deploy-manifest.json` is present |
| `orchestrator_exists` | `.cursor/orchestrator/orchestrator.py` is present |
| `venv_python_exists` | `.cursor/orchestrator/.venv/bin/python` is present |
| `task_index_exists` | `.ai-tasks/index.md` is present |
| `git_clean` | `git status --porcelain` is empty |
| `aii_status` | `aii-2 status <repo>` exits 0 |

A repo is ready only when all checks pass.

## Configuration

| Setting | Default | Override |
|---|---|---|
| ai-native-development path | `/Users/linqing/workplace/github.com/qinglin89/ai-native-development` | `ORCH_HUB_AI_NATIVE_DEPLOYMENT` |
| HTTP bearer token | unset (protected routes answer 503) | `ORCH_HUB_TOKEN` |
| HTTP bind address | `127.0.0.1` | `ORCH_HUB_HOST` |
| HTTP port | `8765` | `ORCH_HUB_PORT` |
| Data directory (run dirs and audit history) | `~/.orch-hub` | `ORCH_HUB_DATA_DIR` |

An empty or whitespace-only value counts as unset for every variable;
surrounding whitespace is stripped. A malformed `ORCH_HUB_PORT` fails startup
instead of silently binding the default port.

## CLI usage

```bash
# Human-readable listing
python -m orch_hub.cli repos

# Stable JSON (for automation and future HTTP reuse)
python -m orch_hub.cli repos --json
```

JSON shape:

```json
{
  "repos": [
    {
      "name": "example",
      "path": "/absolute/path/to/example",
      "manifest": "/absolute/path/to/example/.ai-deploy-manifest.json",
      "readiness": {
        "ready": false,
        "checks": [
          { "name": "git_clean", "message": "working tree has uncommitted changes" }
        ]
      }
    }
  ]
}
```

`readiness.checks` lists only the **failed** checks (the reasons a repo is not
ready); it is empty when `ready` is `true`.

## HTTP API

A FastAPI app (`orch_hub.api`) exposes the same discovery, readiness, and
task-inventory functions over HTTP. Handlers are thin adapters over the core
functions, so the JSON matches the CLI shapes.

```bash
# Serve on localhost (default 127.0.0.1:8765)
python -m orch_hub.api

# or via the installed console script
orch-hub-api

# host/port come from the environment (ORCH_HUB_HOST / ORCH_HUB_PORT)
ORCH_HUB_PORT=9000 orch-hub-api

# or with uvicorn directly
uvicorn orch_hub.api:app --host 127.0.0.1 --port 8765
```

| Endpoint | Returns |
|---|---|
| `GET /api/health` | `{"status": "ok"}` — public, minimal liveness probe |
| `GET /api/repos` | the same object as `repos --json` (repos with readiness) |
| `GET /api/tasks` | task inventory across all repos |
| `GET /api/repos/{repo_id}/tasks` | task inventory for one repo (`repo_id` is the repo name) |
| `GET /api/repos/{repo_id}/tasks/{task_id}` | one task's inventory fields plus its raw `.ai-tasks/<task_id>.md` body (`present`/`content`); read-only, scoped inside the repo |

`GET /api/repos` and the task endpoints reuse the CLI's `repos_payload` /
`tasks_payload` shapes unchanged; task responses preserve `index_present` and
the `missing`/`unknown` sentinel statuses. A malformed registry returns HTTP
500; an unknown `repo_id` returns HTTP 404.

### Authentication

Every route except `GET /api/health` requires a bearer token, sourced from the
`ORCH_HUB_TOKEN` environment variable at service start:

```bash
export ORCH_HUB_TOKEN="$(openssl rand -hex 32)"
orch-hub-api

curl -H "Authorization: Bearer $ORCH_HUB_TOKEN" http://127.0.0.1:8765/api/repos
```

- There is no unauthenticated mode. If no token is configured, every protected
  route fails closed with HTTP 503 (`bearer token not configured`); only the
  health probe stays reachable.
- Requests with a missing or invalid token get HTTP 401 with a
  `WWW-Authenticate: Bearer` header. Token comparison is constant-time, and
  responses/logs never contain the presented token.
- Enforcement is a default-deny middleware: any path outside the public health
  probe — including OpenAPI docs and future run/log/dashboard routes — is
  protected the moment it exists.
- `GET /api/health` is deliberately public and minimal (`{"status": "ok"}`) so
  liveness probes need no secret; it no longer reports configuration details.

### Dashboard

A mobile-first web dashboard is served by the same app (open `/` in a browser).
It is a dependency-free, build-stepless single-page app; the token is presented
as follows:

- The **shell** (`/`, `/app.js`, `/styles.css`) is served **publicly** — it is
  client code with no secrets. A browser cannot attach an `Authorization`
  header on plain navigation, so the bootstrap page must load unauthenticated.
- The SPA prompts for the bearer token once and keeps it **only in the
  browser's `localStorage`**, attaching `Authorization: Bearer <token>` to every
  data `fetch` — including the SSE log stream, which is read with `fetch` + a
  `ReadableStream` reader rather than `EventSource` (which cannot set the
  header). No cookies and no query-string tokens: the API stays on a single
  credential path and secrets stay out of URLs and server logs. A `401` clears
  the stored token and returns to the login gate.
- Every data route stays protected; only the three shell assets are on the auth
  allowlist (alongside `GET /api/health`).

Current views:

- **Repo list** — readiness reasons from `/api/repos` plus live run badges;
  tap through to a repo.
- **All-task list** — `/api/tasks`, filterable by repo and frontmatter status,
  with `missing`/`unknown` sentinels and missing-index states shown as data
  problems; tap through to a task.
- **Repo detail** — the repo's tasks with a status filter, live run badges, and
  the missing-index (`index_present == false`) state made explicit.
- **Task detail** — the task's frontmatter status/summary/blockers, its current
  run status (with a **View logs** link), the raw task-file body (from
  `GET /api/repos/{repo_id}/tasks/{task_id}`), and — when the run is `waiting` —
  an inline answer box that writes the answer via
  `POST /api/runs/{run_id}/escalations/{seq}/answer` (`"stop"` ends the run).
- **Run history** — recent runs newest-first from `GET /api/history/runs`, each
  with its lifecycle state and timestamps; tap through to its log tail.
- **Log tail** — one run's orchestrator log. A finished run shows a bounded
  recent tail (`GET /api/runs/{run_id}/logs`); a live run follows the
  bearer-authenticated SSE stream (`GET /api/runs/{run_id}/logs/stream`) via
  `fetch` + a `ReadableStream` reader, with Pause/Reload controls.

### Deployment assumptions (private network only)

orch-hub controls local code execution, so treat the HTTP service as a remote
code execution surface:

- Reachable only over a private network: localhost, or a Tailscale tailnet.
  Never expose it to the public internet.
- Binding defaults to `127.0.0.1:8765`. For tailnet access, either bind the
  Tailscale interface explicitly (`ORCH_HUB_HOST=<tailscale-ip> orch-hub-api`)
  or keep the localhost bind behind `tailscale serve`.
- The bearer token is required even on the tailnet: tailnet membership is the
  primary boundary, the token is defense in depth.

## Run as a launchd service

To keep the hub resident on the Mac (start at login, restart on exit), install
it as a launchd user agent:

```bash
.venv/bin/orch-hub launchd deploy   # install/update package, write plist, bootstrap
.venv/bin/orch-hub launchd status   # launchd state + /api/health probe
.venv/bin/orch-hub launchd stop     # unload hub service only
.venv/bin/orch-hub launchd update   # same flow as deploy after code changes
```

The launchd label is a singleton (`com.qinglin89.orch-hub`) in the user's
`gui/$UID` domain. `launchd stop` unloads only that hub service; it does not
write run `stop.flag` files or signal detached orchestrator subprocesses.
Detailed install, update, stop, and log inspection guidance is in
[docs/launchd.md](docs/launchd.md). The plist must set `ORCH_HUB_TOKEN` —
`deploy` preserves an existing plist token, uses `ORCH_HUB_TOKEN` when set, or
generates and prints a new token.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Tests use temporary directories and injected fake subprocess runners; they
never touch real repos or the real ai-native-development registry.

## Not implemented yet (by design)

- Bark notifications; remote intake-task; IM bot control.

The core functions (`discover_repos`, `check_readiness`) are written to be
reused unchanged by those future surfaces.
