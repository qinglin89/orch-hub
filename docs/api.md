# HTTP API reference

orch-hub exposes one FastAPI service for the dashboard and for trusted clients
inside the same private-network boundary. Inventory routes reuse registry and
readiness services also used by the CLI; run, intake, and evaluation routes
delegate to their owning application services.

## Access and authentication

The default base URL is `http://127.0.0.1:8765`.

The public allowlist is deliberately small:

| Method/path | Purpose |
|---|---|
| `GET /api/health` | Minimal `{"status":"ok"}` liveness probe |
| `GET /` | Dashboard bootstrap HTML |
| `GET /app.js` | Dashboard client code |
| `GET /styles.css` | Dashboard styles |

Every operational data and action route requires
`Authorization: Bearer <token>`. With no configured token, protected routes
return 503; a missing or invalid presented token returns 401. Unknown and
future paths are protected by the same default-deny middleware unless
deliberately added to the public allowlist.

```bash
curl -H "Authorization: Bearer $ORCH_HUB_TOKEN" \
  http://127.0.0.1:8765/api/repos
```

OpenAPI routes are protected too. A trusted client can retrieve the schema with
an authenticated request to `/openapi.json`.

## Portfolio and inventory

| Method/path | Purpose |
|---|---|
| `GET /api/repos` | Managed repos, launch readiness, latest commit, and housekeeping state |
| `GET /api/usage` | Display-only Claude and Codex subscription capacity |
| `GET /api/tasks` | Active task inventory across all repos |
| `GET /api/repos/{repo_id}/tasks` | Active task inventory for one repo |
| `GET /api/repos/{repo_id}/tasks/{task_id}` | One indexed task plus its raw active task-file body |
| `GET /api/repos/{repo_id}/commits` | Recent commits, newest first |
| `GET /api/repos/{repo_id}/orchestrator-config` | Target orchestrator's resolved launch configuration and option catalog |

`repo_id` is the name in the Mandrel registry, not a filesystem path. Inventory
keeps missing index/file states explicit rather than silently dropping them.
The task-detail route is active-index scoped; historical run/task access uses
the run-scoped route below.

## Runs, history, logs, and human input

| Method/path | Purpose |
|---|---|
| `POST /api/runs` | Start one repo-local orchestrator run |
| `GET /api/runs` | Current and known run states |
| `GET /api/history/runs` | Cursor-paged history or a bounded recent summary |
| `GET /api/runs/{run_id}` | One run's current state |
| `GET /api/runs/{run_id}/task` | Run-associated task body, active first and archive fallback |
| `POST /api/runs/{run_id}/stop` | Request graceful stop at a scheduler boundary |
| `GET /api/runs/{run_id}/sessions` | Live or persisted per-session context summary |
| `GET /api/runs/{run_id}/effective-config` | Authoritative launch snapshot parsed from the run |
| `GET /api/runs/{run_id}/logs` | Bounded recent log tail |
| `GET /api/runs/{run_id}/logs/stream` | Bearer-authenticated SSE log stream |
| `GET /api/escalations` | Pending questions across live runs |
| `POST /api/runs/{run_id}/escalations/{seq}/answer` | Answer or discuss one pending question |

Run starts fail closed on readiness, repo admission, task legality, and an
uninterpretable launch selection. One repo may have at most one live run. A
non-terminal remote intake also holds that repo's admission slot.

Two start-request fields are hub-only and never become orchestrator argv:
`acknowledge_blocked` (launch a `blocked` task anyway) and `auto_semantic_eval`
(default true; false declines the automatic Layer 2 evaluation of a task this run
completes — see `docs/evaluation.md`).

The stop route is graceful: it writes the scheduler's stop request and returns.
The scheduler consumes it at the next session boundary or during an outstanding
human-input wait; it does not kill the current coding-agent process immediately.
Log streams use `fetch` with an authorization header rather than browser
`EventSource`, which cannot attach that header.

## Remote task intake

| Method/path | Purpose |
|---|---|
| `POST /api/intakes` | Start a draft for one repo |
| `GET /api/intakes` | List known intake records |
| `GET /api/intakes/{intake_id}` | Current draft and lifecycle state |
| `GET /api/intakes/{intake_id}/history` | Current intake record plus metadata-only audit events |
| `POST /api/intakes/{intake_id}/revise` | Ask the existing intake session for a revised draft |
| `POST /api/intakes/{intake_id}/confirm` | Confirm and write the reviewed task into the target repo |
| `POST /api/intakes/{intake_id}/cancel` | Cancel without writing the target repo |

Draft and revise turns are read-only over the target repo. Confirmation is the
only route that reaches the task write path. The default intake backend uses an
installed and authenticated Claude CLI. Starting an intake shares the full repo
readiness and admission gates used for launch: the worktree must be clean and
no live run or other non-terminal intake may own the repo.

## Task evaluation and report feed

| Method/path | Purpose |
|---|---|
| `GET /api/evaluation` | Whether the optional evaluation subsystem is enabled |
| `GET /api/evaluation/doctor` | Layer 2 backend availability without a model call |
| `GET /api/repos/{repo_id}/evaluations/{task_id}` | Persisted evaluations for an active or archived task |
| `GET /api/repos/{repo_id}/evaluations/{task_id}/{evaluation_id}` | One evaluation's artifacts |
| `POST /api/repos/{repo_id}/evaluations/{task_id}/evidence` | Generate a fresh bounded evidence pack |
| `POST /api/repos/{repo_id}/evaluations/{task_id}/metrics` | Generate deterministic Layer 1 metrics |
| `POST /api/repos/{repo_id}/evaluations/{task_id}/semantic/job` | Start or reuse an asynchronous Layer 2 job |
| `GET /api/repos/{repo_id}/evaluations/{task_id}/semantic/job` | Read the current Layer 2 job state |
| `GET /api/evaluation/reports` | Cursor-paged cross-repo report feed |
| `GET /api/evaluation/reports/{report_key}` | One published report bundle |
| `GET /api/evaluation/reports/{report_key}/artifacts/{name}` | Exact persisted artifact bytes |

Evaluation writes only hub-owned artifacts under `ORCH_HUB_DATA_DIR`; it does
not mutate target repos, task files, run records, or Mandrel protocol state.
Layer 1 is deterministic. Layer 2 invokes Claude over a bounded persisted
evidence prompt. See [Task evaluation](evaluation.md) for evidence, retention,
provenance, and report-feed semantics.

## Error model

| Status | Meaning |
|---|---|
| 400 | Invalid launch selection, query, or lifecycle input |
| 401 | Missing or invalid bearer token |
| 404 | Unknown resource, or a disabled optional evaluation route |
| 409 | Repo busy, lifecycle conflict, duplicate answer, or incoherent artifact identity |
| 410 | Published evaluation artifact was pruned or not retained |
| 422 | Request body does not match the declared API schema |
| 500 | Registry, storage, or internal consistency failure |
| 502 | The target orchestrator could not provide a usable configuration |
| 503 | No service bearer token is configured |

Operational failures return concise details and structured conflict fields
where a client needs the blocking run, intake, or readiness reason. Presented
tokens and secret Bark URLs are never echoed.
