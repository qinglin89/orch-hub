---
last-updated: 2026-07-07
verified-against: 26711f3ca1dd2aaee4e68377327869e6d2d1724e
---

# APIs and Interfaces

## Public CLI

### `python -m orch_hub.cli repos`

- Purpose: list managed repos discovered from ai-native-development with readiness.
- Output: human-readable status lines, followed by failed readiness reasons for non-ready repos.
- Source: registry adapter plus readiness checker.
- Non-goal: does not start orchestrators.

### `python -m orch_hub.cli repos --json`

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
      }
    }
  ]
}
```

`readiness.checks` contains only failed checks as `{name, message}`; it is empty when `ready` is true. Internally the readiness report keeps all seven check results for callers that need the full picture.

## HTTP API

### `GET /api/health`

- Purpose: minimal public service liveness.
- Shape: `{"status": "ok"}`.
- Auth: public by design; it must not disclose deployment configuration.

### `GET /api/repos`

- Purpose: list managed repos with readiness.
- Source: same functions as CLI `repos --json`.
- Shape: exactly the same top-level object as `python -m orch_hub.cli repos --json`.

### `GET /api/repos/{repo_id}/tasks`

- Purpose: list tasks for one repo, where `repo_id` is the managed-repo name.
- Shape: `tasks_payload([inventory])`, the same `{"repos": [...]}` envelope as
  all-repo task inventory with a single repo entry.

### `GET /api/tasks`

- Purpose: list tasks across discovered repos.
- Shape: `tasks_payload(list_tasks(repos))`, preserving `index_present`,
  ordered tasks, frontmatter-authoritative status/blockers, and `missing` /
  `unknown` sentinel statuses.

Current HTTP behavior:

- `create_app(config=None)` captures `Config.from_env()` by default and is testable with an explicit `Config`.
- `Config.token` is loaded from `ORCH_HUB_TOKEN`; surrounding whitespace is
  stripped and empty/whitespace-only values count as unset.
- Module-level `app` is the `uvicorn orch_hub.api:app` target.
- `run(host="127.0.0.1", port=8765)` and console script `orch-hub-api` serve
  the app on localhost by default.
- Default-deny middleware protects every path outside `/api/health`, including
  `/docs`, `/openapi.json`, unknown paths, and future dashboard/run/log routes.
- Missing or malformed credentials return HTTP 401 with
  `WWW-Authenticate: Bearer` and `{"detail": "missing bearer token"}`; wrong
  credentials return HTTP 401 with `{"detail": "invalid bearer token"}`.
- When `ORCH_HUB_TOKEN` is unset, protected routes fail closed with HTTP 503
  instead of serving unauthenticated; `/api/health` still works.
- Token comparison uses `secrets.compare_digest`; auth error responses are
  static and never echo presented credentials.
- `RegistryError` returns HTTP 500 with `{"detail": ...}`.
- Unknown `repo_id` on the per-repo task endpoint returns HTTP 404.

## Future HTTP API

### `GET /api/runs`

- Purpose: list active and recent runs.

### `POST /api/runs`

- Purpose: start an orchestrator run for `{repo_id, task_id, flags}` after readiness checks.

### `POST /api/runs/{run_id}/stop`

- Purpose: request graceful stop, initially by writing future `stop.flag`.

### `GET /api/escalations`

- Purpose: list pending human questions from control-dir question files.

### `POST /api/escalations/{question_id}/answer`

- Purpose: write an answer file and audit the response.

### `GET /api/runs/{run_id}/logs`

- Purpose: retrieve recent log text.

### `GET /api/runs/{run_id}/logs/stream`

- Purpose: SSE stream for log tailing.

## Internal Interfaces

### Registry Provider

Input: config with ai-native-development path.

Output: list of managed repo models.

Behavior: command source first, registry-file fallback second. An exit-0 command with invalid JSON raises `RegistryError`; command missing/non-zero falls back; missing fallback file returns an empty list.

### Readiness Checker

Input: managed repo model.

Output: readiness report with boolean `ready` and list of failed checks.

Behavior: pure inspection and subprocess checks only; no mutation. Missing repo path skips subprocess execution but records `git_clean` and `aii_status` as failed so launch readiness still fails closed.

### Task Store

Input: repo path.

Output: `RepoTaskInventory` values containing repo fields, `index_present`, and ordered `TaskSummary` rows.

Behavior: parse `.ai-tasks/index.md` and task frontmatter locally. The index supplies active task ids and summaries; task frontmatter supplies authoritative status and blockers. The reusable `tasks_payload(inventories)` helper serializes as `{"repos": [...]}` for future CLI/API adapters.

### Run Manager

Input: repo id, task id, start flags.

Output: run record.

Behavior: future subprocess lifecycle and per-repo serialization.

### Control Dir Store

Input: run dir.

Output: pending question records and answer/stop write operations.

Behavior: file-channel only.

### Notifier

Input: notification event.

Output: delivery result.

Behavior: fan-out capable; Bark is the expected first implementation.

## API Conventions

- Prefer absolute paths internally; avoid exposing more path data than the UI needs.
- Keep JSON stable and explicit.
- Include `ready: false` with reasons instead of raising for ordinary readiness failures.
- Raise or return operational errors for invalid registry JSON, unreadable config, or malformed user requests.
- Keep task `blocked` distinct from run `waiting`.
- Never make API handlers call orchestrator internals directly.
