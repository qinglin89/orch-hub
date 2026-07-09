---
last-updated: 2026-07-10
verified-against: 29a10e84360d2aae3e243d9d0eab4f4cf81eee07
---

# APIs: HTTP

## HTTP API

### `GET /api/health`

- Purpose: minimal public service liveness.
- Shape: `{"status": "ok"}`.
- Auth: public by design; it must not disclose deployment configuration.

### `GET /`, `GET /app.js`, `GET /styles.css`

- Purpose: serve the public dashboard shell assets.
- Auth: public by design because browser navigation cannot attach an
  `Authorization` header. These assets contain no secrets; every dashboard data
  request still uses protected HTTP APIs with `Authorization: Bearer <token>`.
- Cache: each response sends `Cache-Control: no-store` so mobile browsers fetch
  the current shell after service updates rather than serving stale JS/CSS from
  heuristic cache.

### `GET /api/repos`

- Purpose: list managed repos with readiness.
- Source: same functions as CLI `repos --json`.
- Shape: exactly the same top-level object as `python -m orch_hub.cli repos --json`, including best-effort `commit`.

### `GET /api/repos/{repo_id}/commits`

- Purpose: list one repo's recent commits newest-first for the dashboard detail
  view.
- Source: resolves `repo_id` through registry discovery, then delegates to
  `git_meta.read_commits`.
- Shape: `{"repo": "quantx", "available": true, "commits": [CommitInfo...]}`.
  Each commit is `{hash, short_hash, committed_ts, subject}`.
- Query: `limit` defaults to 10 and is clamped by `git_meta` to `1..50`.
- Errors: unknown repos return HTTP 404; git lookup failure returns HTTP 200
  with `available: false` and `commits: []` so repo/task content stays visible.

### `GET /api/repos/{repo_id}/tasks`

- Purpose: list tasks for one repo, where `repo_id` is the managed-repo name.
- Shape: `tasks_payload([inventory])`, the same `{"repos": [...]}` envelope as
  all-repo task inventory with a single repo entry. Task ids are normalized
  bare ids even when the source index uses Markdown-link Task cells.

### `GET /api/repos/{repo_id}/tasks/{task_id}`

- Purpose: fetch one listed task's frontmatter-authoritative inventory fields
  plus raw markdown body for the dashboard task-detail view.
- Source: resolves `repo_id` through registry discovery, gets status/summary/
  blockers through `list_repo_tasks`, gates `task_id` on normalized index
  membership, then reads `.ai-tasks/<task_id>.md` through `read_task_content`.
- Shape: `TaskSummary.to_dict()` plus `{"present": bool, "content": str|null}`.
  Listed-but-absent task files return HTTP 200 with `present: false` and
  `content: null`; unknown repos or unlisted task ids return HTTP 404.

### `GET /api/tasks`

- Purpose: list tasks across discovered repos.
- Shape: `tasks_payload(list_tasks(repos))`, preserving `index_present`,
  ordered tasks, frontmatter-authoritative status/blockers, and `missing` /
  `unknown` sentinel statuses.

### `POST /api/runs`

- Purpose: start an orchestrator run for `{repo_id, task_id, extra_args,
  acknowledge_blocked}`.
- Source: resolves `repo_id` through registry discovery, then delegates launch
  gates and subprocess start to the app's single `RunManager`.
- Dashboard caller: the task-detail Start dialog sends `extra_args` beginning
  with fixed `["--backend", "cc-codex"]` plus only changed cc-codex override
  fields; the dashboard does not expose a backend selector.
- Shape: returns the new run's serialized `RunStatus` with HTTP 201.

### `GET /api/runs`

- Purpose: list known runs with live/waiting/end-state overlays.
- Shape: `{"runs": [RunStatus.to_dict(), ...]}`.

### `GET /api/history/runs`

- Purpose: list recent runs newest-first with computed run status and persisted
  audit/history events.
- Source: delegates to `RunManager.recent_runs(limit=...)`, which refreshes
  run state and reads each run's `events.jsonl`.
- Shape: `{"runs": [RunHistoryEntry.to_dict(), ...]}` where each entry embeds
  the normal `RunStatus` fields plus `events`.
- Query: `limit` defaults to 50 and is clamped to `0..200`.

### `GET /api/runs/{run_id}`

- Purpose: fetch one run's serialized `RunStatus`.
- Errors: unknown run id returns HTTP 404.

### `POST /api/runs/{run_id}/stop`

- Purpose: request graceful stop by delegating to `RunManager.stop_run`, which
  writes `stop.flag` through `control_dir.request_stop`.
- Shape: `{"run_id": ..., "newly_requested": bool, "detail": ...}`; the detail
  surfaces the pending-question stop caveat.

### `GET /api/runs/{run_id}/logs`

- Purpose: retrieve a bounded recent tail of one run's orchestrator log.
- Source: resolves `run_id` through `RunManager.get_run`, then locates and
  reads the log from the run record's `log_dir`, `task_id`, and `started_ts`.
  Clients never supply a filesystem path.
- Shape: `{"run_id", "present", "lines", "truncated", "size", "detail",
  "diagnostic"}`. Missing, rotated, or unreadable logs are HTTP 200 with
  `present: false` and a detail string. `diagnostic` is `null` unless no
  orchestrator log is located and the run's bounded `spawn.out` tail is
  non-empty; when present it is a normal log snapshot plus
  `"source": "spawn.out"`. Unknown runs return HTTP 404.
- Query: `tail` controls returned line count, clamped by the logs module.

### `GET /api/runs/{run_id}/logs/stream`

- Purpose: stream one run's orchestrator log as Server-Sent Events.
- Source: resolves `run_id` through `RunManager.get_run`, emits a bounded
  initial tail, follows appended complete lines, re-probes run liveness each
  poll, and closes with `event: end` when the run ends or the log rotates away.
- Shape: `text/event-stream`; ordinary log lines are `data:` events, idle
  heartbeats are SSE comments, and stream termination is an `event: end`.
  Unknown runs return HTTP 404.
- Auth/client contract: standard `Authorization: Bearer <token>` only. Browser
  clients use `fetch` plus a `ReadableStream` reader because `EventSource`
  cannot set authorization headers; query-string tokens are not supported.

### `GET /api/escalations`

- Purpose: list pending control-dir questions across live runs, with run/repo
  context.
- Shape: `{"escalations": [{"run_id", "repo_name", "task_id", "question"}]}`.

### `POST /api/runs/{run_id}/escalations/{seq}/answer`

- Purpose: write an answer file for one run/question sequence, auditing the
  responder.
- Source: locates the question with `control_dir.list_questions`, writes via
  `control_dir.write_answer`, then records answer-audit metadata through
  `RunManager.audit_answer`; no orchestrator code is imported.
- Shape: `{"run_id": ..., "seq": ..., "answered": true, "responder": ...,
  "answer": ...}`.
  The response echoes the submitted answer for the immediate caller, but
  persisted history deliberately excludes answer text.

Current HTTP behavior:

- `create_app(config=None, *, run_manager=None)` captures `Config.from_env()`
  by default, is testable with an explicit `Config`, and holds one `RunManager`
  instance for run, escalation, log, and background-monitor scans.
- The app constructs a `RunMonitor` over that manager, exposes it as
  `app.state.run_monitor`, starts it only during FastAPI lifespan startup, and
  stops it during shutdown; app construction alone starts no worker thread.
- `Config` is loaded from environment by default. Current variables are
  `ORCH_HUB_AI_NATIVE_DEPLOYMENT`, `ORCH_HUB_TOKEN`, `ORCH_HUB_HOST`,
  `ORCH_HUB_PORT`, `ORCH_HUB_DATA_DIR`, and `ORCH_HUB_BARK_URL`; surrounding
  whitespace is stripped and empty/whitespace-only values count as unset.
- Module-level `app` is the `uvicorn orch_hub.api:app` target.
- `run(host=None, port=None)` and console script `orch-hub-api` serve the app
  on `ORCH_HUB_HOST` / `ORCH_HUB_PORT`, defaulting to `127.0.0.1:8765`;
  explicit function arguments override the environment.
- Default-deny pure-ASGI middleware protects every path outside `/api/health`
  and the enumerated dashboard shell paths (`/`, `/app.js`, `/styles.css`),
  including `/docs`, `/openapi.json`, unknown paths, and future data routes.
  Pure ASGI is used instead of `BaseHTTPMiddleware` so streaming responses are
  not buffered and disconnects propagate.
- The enumerated dashboard shell paths are public but uncached:
  `Cache-Control: no-store` is attached to each shell `FileResponse`.
- Missing or malformed credentials return HTTP 401 with
  `WWW-Authenticate: Bearer` and `{"detail": "missing bearer token"}`; wrong
  credentials return HTTP 401 with `{"detail": "invalid bearer token"}`.
- When `ORCH_HUB_TOKEN` is unset, protected routes fail closed with HTTP 503
  instead of serving unauthenticated; `/api/health` still works.
- Token comparison uses `secrets.compare_digest`; auth error responses are
  static and never echo presented credentials.
- `RegistryError` returns HTTP 500 with `{"detail": ...}`.
- Unknown `repo_id` on the per-repo task endpoint returns HTTP 404.
- Run-manager/control-dir errors map to client-actionable statuses:
  `UnknownRunError` → 404; `RepoBusyError`, `RunEndedError`,
  `BlockedTaskError`, `LaunchBlockedError`, and `DuplicateAnswerError` → 409;
  empty answer `ValueError` → 400. `LaunchBlockedError` includes `reasons`;
  `BlockedTaskError` includes `acknowledge_required: true`.
- The dashboard repo/task status layout consumes these APIs directly: repo
  cards use `/api/repos` for readiness/latest commit, `/api/runs` for the
  tri-state run signal, and `/api/tasks` for active task counts; repo detail
  uses `/api/repos/{repo_id}/commits` for recent commits and treats
  `available:false` as a graceful unavailable state.
- The dashboard task-detail run controls consume these existing run APIs
  directly: Start sends `{repo_id, task_id, acknowledge_blocked, extra_args}`
  to `POST /api/runs` with fixed `--backend cc-codex` and optional cc-codex
  overrides, Stop sends `POST /api/runs/{run_id}/stop`, and frontend error
  rendering preserves structured 409 fields instead of flattening every refusal
  to `detail`.
