# orch-hub

A long-lived local control plane for AI coding orchestrators. It runs on your
Mac and is meant to be driven remotely (iPhone/iPad or another machine) over a
private network, coordinating the per-repository orchestrator programs that run
dev/review loops over `.ai-tasks/`.

This repository implements the core application slices — repo discovery, launch
readiness, and task inventory — plus a first **HTTP service foundation** that
exposes them. The remaining surfaces (orchestrator runs, question relay, logs,
notifications, dashboard, auth) are planned and intentionally not built yet.

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

# or with uvicorn directly, e.g. to change host/port
uvicorn orch_hub.api:app --host 127.0.0.1 --port 8765
```

| Endpoint | Returns |
|---|---|
| `GET /api/health` | `{"status": "ok", "ai_native_path": "..."}` |
| `GET /api/repos` | the same object as `repos --json` (repos with readiness) |
| `GET /api/tasks` | task inventory across all repos |
| `GET /api/repos/{repo_id}/tasks` | task inventory for one repo (`repo_id` is the repo name) |

`GET /api/repos` and the task endpoints reuse the CLI's `repos_payload` /
`tasks_payload` shapes unchanged; task responses preserve `index_present` and
the `missing`/`unknown` sentinel statuses. A malformed registry returns HTTP
500; an unknown `repo_id` returns HTTP 404.

Binding defaults to localhost only; private-network exposure and bearer-token
auth are a separate planned slice.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Tests use temporary directories and injected fake subprocess runners; they
never touch real repos or the real ai-native-development registry.

## Not implemented yet (by design)

- Web/mobile dashboard.
- Orchestrator subprocess runs and `--control-dir` question/answer relay.
- Log streaming, Bark notifications, Tailscale/bearer-token auth, launchd
  packaging.

The core functions (`discover_repos`, `check_readiness`) are written to be
reused unchanged by those future surfaces.
