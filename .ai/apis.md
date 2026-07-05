---
last-updated: 2026-07-06
verified-against: 27df28d046cf78be92bae5793c43030f9e8500c7
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
