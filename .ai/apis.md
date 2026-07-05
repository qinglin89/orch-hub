---
last-updated: 2026-07-05
verified-against: 2ac51313fe081792d3cae8a8b40729561ab9b8f1
---

# APIs and Interfaces

## Public CLI

### `python -m orch_hub.cli repos`

- Purpose: list managed repos discovered from ai-native-development with readiness.
- Initial output: human-readable table or lines.
- Source: registry adapter plus readiness checker.
- Non-goal: does not start orchestrators.

### `python -m orch_hub.cli repos --json`

- Purpose: stable JSON form for automation and future FastAPI reuse.
- Expected shape:

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

Exact field names may evolve during the first implementation task, but should remain coherent for future HTTP responses.

## Future HTTP API

### `GET /api/health`

- Purpose: service liveness and configuration sanity.

### `GET /api/repos`

- Purpose: list managed repos with readiness and run badges.
- Source: same functions as CLI `repos --json`.

### `GET /api/repos/{repo_id}/tasks`

- Purpose: list tasks for one repo, including task frontmatter status and hub run overlay.

### `GET /api/tasks`

- Purpose: list tasks across repos, filterable by repo and state.

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

Behavior: command source first, registry-file fallback second.

### Readiness Checker

Input: managed repo model.

Output: readiness report with boolean `ready` and list of failed checks.

Behavior: pure inspection and subprocess checks only; no mutation.

### Task Store

Input: repo path.

Output: task summaries and optional full task text.

Behavior: parse `.ai-tasks/index.md` and task frontmatter locally.

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
