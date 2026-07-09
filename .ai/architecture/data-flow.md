---
last-updated: 2026-07-10
verified-against: 30c1e08a6e706cab2d922417cf0024aa2920c5f7
---

# Architecture: Data Flow

## Discovery and Readiness

1. Load config, including ai-native-development path.
2. Try `aii-2 registry list --json`.
3. If command fails or is unavailable, read `<ai-native-development>/.registry/repos.local.json`.
4. For each repo entry, run readiness checks.
5. Return stable JSON for CLI and API responses.

## HTTP Foundation

1. `create_app(config=None)` captures a `Config` value once per FastAPI app,
   including `ORCH_HUB_TOKEN` from `Config.token`.
2. Module-level `app = create_app()` is the uvicorn target.
3. `run(host=None, port=None)` serves the app with uvicorn, defaulting to
   `Config.host` / `Config.port` from `ORCH_HUB_HOST` and `ORCH_HUB_PORT`
   (`127.0.0.1:8765` when unset); private-network exposure must stay
   Tailscale-only.
4. A default-deny pure-ASGI HTTP middleware allows public access only to
   `/api/health` and the three dashboard shell asset paths. Missing/malformed
   credentials return 401 plus `WWW-Authenticate: Bearer`, invalid credentials
   return 401, and an unset token returns 503 for protected data routes. Pure
   ASGI is required for long-lived SSE log streams; `BaseHTTPMiddleware` can
   buffer streaming responses and delay disconnect propagation.
5. `RegistryError` maps to HTTP 500; unknown repo ids or unlisted task ids on
   per-repo task lookup/detail routes map to HTTP 404. Ordinary readiness
   failures stay inside 200 payloads as `ready: false` with failure reasons.

## Task Inventory

1. Receive `ManagedRepo` values from registry discovery or a caller-provided filter.
2. For each repo, read `.ai-tasks/index.md`; missing indexes return `index_present=false` with no tasks so callers can surface the repo-level issue.
3. Parse task ids and summaries from the index table without trusting the index Status column.
4. Read each listed `.ai-tasks/<task-id>.md` frontmatter for authoritative `status` and `blockers`.
5. Return `RepoTaskInventory` values that can be serialized directly for future task APIs.
6. The single-task content endpoint gates on index membership, rejects unsafe
   task ids in the reader, and resolves the candidate file target to ensure raw
   content stays under the resolved `.ai-tasks` directory.

## Start Run

1. Client selects repo and task.
2. Hub enforces the per-repo single-live-run rule.
3. Hub verifies task existence/status through `list_repo_tasks` and rejects
   unlisted, `missing`, `unknown`, or unrecognized statuses. A `blocked` task
   requires explicit acknowledgement.
4. Hub verifies repo readiness with the existing checker.
5. Hub creates a fresh run dir outside the target repo.
6. Hub starts the deployed orchestrator subprocess with `--control-dir`.
7. Hub records pid, repo, task, control dir, log dir, start time, argv, and
   later observed end state in `run.json`, including the launch-time task
   frontmatter status from `TaskSummary`.
8. Hub appends a `run_started` history event.

## Human Answer

1. Orchestrator writes a question file in the control dir.
2. Hub lists it as waiting with `pending_questions` and notifies the user once
   per `(run_id, seq)` when Bark is configured.
3. User submits an answer through the dashboard or future bot.
4. Hub writes the answer file with `write_answer` and records an audit event.
5. Orchestrator consumes the answer and continues.

## Stop

1. User requests stop.
2. Hub writes `stop.flag` with `request_stop` for graceful stop at a session boundary.
3. Hub records a `stop_requested` event only for the first idempotent stop write.
4. If a hard kill is ever exposed, it must warn that the repo may be dirty and children may be orphaned.

## Log Access

1. Client requests recent or streamed logs for a known `run_id`.
2. API resolves the run through `RunManager.get_run`; unknown/path-escaping ids
   return HTTP 404.
3. `orch_hub.logs` locates `<utc-ts>-<task-id>.log` under the run record's
   `log_dir`, using `task_id` and `floor(started_ts)` for attribution. A later
   re-run gets a later stamp; records with no usable `started_ts` fall back to
   newest matching task log.
4. Recent retrieval returns bounded lines with graceful `present: false`
   details for missing, rotated, or unreadable logs.
5. SSE streaming emits a bounded initial tail, then appended complete lines,
   heartbeat comments while idle, and `event: end` on run end or log rotation.
   Browser clients use `fetch` plus a stream reader with the standard bearer
   header; the hub does not accept query-string log stream credentials.

## Run State Refresh

1. Hub lists run dirs under `<data_dir>/runs/`.
2. For a run with an in-memory `Popen`, `poll()` captures exit code when it
   ends.
3. For reattached runs, pid liveness decides whether the run is still active;
   if not, the hub records `ended_ts` with unknown exit code.
4. Live runs are `waiting` when control-dir questions are pending, otherwise
   `active`.
5. End classification prefers task completion/archival evidence, then stop
   evidence (`stop.flag` or a valid `"stop"` answer), then bare exit code.
6. When the hub observes an end, it rewrites `run.json` and appends a
   `run_ended` event with the observed exit code when available.
7. The same observed end fires one best-effort terminal notification through
   the configured notifier, without changing run-state writes if delivery
   fails.

## Run History Access

1. Client requests recent history through `GET /api/history/runs`.
2. API asks `RunManager.recent_runs(limit=...)`, which clamps the limit to
   `0..200`, scans run dirs newest-first, refreshes computed run status, and
   loads each run's `events.jsonl`.
3. Response embeds each run's current serialized `RunStatus` plus persisted
   event records. History parsing failures are represented as events rather
   than route failures.
