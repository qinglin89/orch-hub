---
last-updated: 2026-07-24
verified-against: 3779a5d5ac7b2dd74f669990a25dc318e24f54f8
---

# APIs: HTTP — Core

Health, dashboard shell, repo/task/usage/orchestrator-config routes, and
cross-cutting app/auth behavior. Run routes: `http-runs.md`. Evaluation
routes: `http-evaluation.md`. Intake routes: `http-intake.md`.

## `GET /api/health`

- Purpose: minimal public service liveness.
- Shape: `{"status": "ok"}`.
- Auth: public by design; it must not disclose deployment configuration.

## `GET /`, `GET /app.js`, `GET /styles.css`

- Purpose: serve the public dashboard shell assets.
- Auth: public by design because browser navigation cannot attach an
  `Authorization` header. These assets contain no secrets; every dashboard data
  request still uses protected HTTP APIs with `Authorization: Bearer <token>`.
- Cache: each response sends `Cache-Control: no-store` so mobile browsers fetch
  the current shell after service updates rather than serving stale JS/CSS from
  heuristic cache.
- Dashboard behavior: `#/overview` is the default authenticated landing route
  and derives its metrics, attention list, live activity, and recent outcomes
  plus display-only subscription usage from protected repo/task/run/escalation/
  history/usage APIs. Non-401 source failures degrade only the affected panel or
  usage section, 401 re-enters the login gate, and route teardown cancels its
  chained refresh before stale loads can repaint or keep polling.

## `GET /api/repos`

- Purpose: list managed repos with readiness and display metadata.
- Source: same functions as CLI `repos --json`.
- Shape: exactly the same top-level object as `python -m orch_hub.cli repos --json`, including best-effort `commit` and display-only `housekeeping_pending`.

## `GET /api/repos/{repo_id}/commits`

- Purpose: list one repo's recent commits newest-first for the dashboard detail
  view.
- Source: resolves `repo_id` through registry discovery, then delegates to
  `git_meta.read_commits`.
- Shape: `{"repo": "quantx", "available": true, "commits": [CommitInfo...]}`.
  Each commit is `{hash, short_hash, committed_ts, subject}`.
- Query: `limit` defaults to 10 and is clamped by `git_meta` to `1..50`.
- Errors: unknown repos return HTTP 404; git lookup failure returns HTTP 200
  with `available: false` and `commits: []` so repo/task content stays visible.

## `GET /api/repos/{repo_id}/tasks`

- Purpose: list tasks for one repo, where `repo_id` is the managed-repo name.
- Shape: `tasks_payload([inventory])`, the same `{"repos": [...]}` envelope as
  all-repo task inventory with a single repo entry. Task ids are normalized
  bare ids even when the source index uses Markdown-link Task cells.

## `GET /api/repos/{repo_id}/tasks/{task_id}`

- Purpose: fetch one listed task's frontmatter-authoritative inventory fields
  plus raw markdown body for the dashboard task-detail view.
- Source: resolves `repo_id` through registry discovery, gets status/summary/
  blockers through `list_repo_tasks`, gates `task_id` on normalized index
  membership, then reads `.ai-tasks/<task_id>.md` through `read_task_content`.
- Shape: `TaskSummary.to_dict()` plus `{"present": bool, "content": str|null}`.
  Listed-but-absent task files return HTTP 200 with `present: false` and
  `content: null`; unknown repos or unlisted task ids return HTTP 404.

## `GET /api/tasks`

- Purpose: list tasks across discovered repos.
- Shape: `tasks_payload(list_tasks(repos))`, preserving `index_present`,
  ordered tasks, frontmatter-authoritative status/blockers, and `missing` /
  `unknown` sentinel statuses.

## `GET /api/usage`

- Purpose: report display-only Claude and Codex subscription capacity for the
  Overview without affecting readiness, attention, run state, launch, or intake.
- Source: delegates to `UsageService`, which ingests the opt-in Claude
  statusLine sink into the latest hub-owned observation store, then reads Claude
  from that store and Codex from local Codex rollout `rate_limits` snapshots
  under `ORCH_HUB_CODEX_HOME`. The wired Claude stream-json observer records
  nothing for Claude Code 2.1.218 because that stream has no 5h/7d subscription
  windows. It never starts probe turns, calls provider endpoints, opens
  credential files, edits `~/.claude/**`, or consumes reset credits.
- Shape: `{"providers": [ProviderUsage...]}`. Each provider is
  `{provider, available, plan, windows, credits, observed_ts, stale, detail}`;
  windows carry `key`, optional `label`/`window_minutes`, `used_percent`,
  derived `remaining_percent`, optional `resets_at`, and exact `used`/`limit`
  only when a provider supplies them.
- Errors/degradation: protected like every data route. Missing, unsupported,
  stale, or failed provider sources return explicit provider states rather than
  zero usage or route failure; account, credential, private endpoint, and
  reset-credit identifiers are excluded.

## `GET /api/repos/{repo_id}/orchestrator-config`

- Purpose: query the target repo's deployed orchestrator for the Start dialog's
  resolved launch configuration via `orchestrator.py --print-config`.
- Source: resolves `repo_id` through registry discovery, then runs the
  repo-local orchestrator with `cwd=<repo>` and optional `backend`, legacy
  `profile`, `dev_profile`, and `review_profile` query params. It does not read
  `orchestrator.toml` directly and is not readiness-gated.
- Shape: `EffectiveConfig.to_dict()` with schema/revisions/profile list,
  authoritative-or-derived `profiles.dev`/`profiles.review`, hub-owned
  `split_profiles`, backend, resolved role values, budgets, sandbox, provenance
  `sources`, and preview launch fields. Missing legacy `profiles` derives both
  roles from top-level `profile` but leaves `split_profiles=false`.
- Errors: unknown repo returns 404; unrecognized backend returns 400; missing
  old deployments, nonzero `--print-config`, malformed output, or unsupported
  schema return 502 with the orchestrator/parser reason so launch fails closed.

## Current HTTP Behavior

- `create_app(config=None, *, run_manager=None, intake_manager=None,
  evaluation_service=None, static_evaluator=None, semantic_evaluator=None,
  semantic_job_manager=None, usage_service=None)`
  captures `Config.from_env()` by default, is testable with explicit/injected
  services, and holds one `RunManager` for run, run-task, escalation, log, and
  background-monitor scans plus one `IntakeManager` for remote intake routes and
  one `UsageService` for the protected usage route, Claude statusLine sink
  ingestion, and Claude stream observation feed.
  Default managers are built with `build_notifier(config)`: run-ended/
  pending-question and intake-ready notifications share the configured Bark
  path, and `None` keeps delivery disabled. The default intake backend is
  `ClaudeIntakeAgent`; the default semantic backend is `ClaudeSemanticBackend`
  constructed at app startup but only invoked by semantic generation or probed
  by the doctor. Both default Claude backends receive the shared
  `UsageService.claude_observer()` callback; observer failures are swallowed and
  never alter intake/semantic outcomes.
- The app constructs a `RunMonitor` over that manager, exposes it as
  `app.state.run_monitor`, starts it only during FastAPI lifespan startup, and
  stops it during shutdown; app construction alone starts no worker thread.
- `Config` is loaded from environment by default. Current variables are
  `ORCH_HUB_AI_NATIVE_DEPLOYMENT`, `ORCH_HUB_TOKEN`, `ORCH_HUB_HOST`,
  `ORCH_HUB_PORT`, `ORCH_HUB_DATA_DIR`, `ORCH_HUB_BARK_URL`,
  `ORCH_HUB_EVAL_ENABLED`, `ORCH_HUB_EVAL_MODEL`,
  `ORCH_HUB_EVAL_RETENTION`, and `ORCH_HUB_CODEX_HOME`; surrounding whitespace
  is stripped and empty/whitespace-only values count as unset.
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
  structured launch `LaunchError`, empty answer, or answer-mode guard
  `ValueError` → 400; orchestrator-config query failures → 502.
  `LaunchBlockedError` includes `reasons`; `BlockedTaskError` includes
  `acknowledge_required: true`.
- Intake-manager errors map to client-actionable statuses: `UnknownIntakeError`
  → 404; `IntakeBusyError`, `IntakeNotReadyError`, and `IntakeStateError` →
  409; empty request/feedback `ValueError` → 400. Agent failures are lifecycle
  outcomes in the intake record. Mutating intake routes persist and return
  transient records (`drafting`/`revising`/`confirming`) before the slow agent
  turn finishes; clients poll `GET /api/intakes/{id}` for the resting result.
- The dashboard repo/task status layout consumes these APIs directly: repo
  cards use `/api/repos` for readiness/latest commit/housekeeping warning
  state, `/api/runs` for the tri-state run signal, and `/api/tasks` for active
  task counts; repo detail uses `/api/repos` for the housekeeping warning,
  uses `/api/repos/{repo_id}/commits` for recent commits, and treats
  `available:false` as a graceful unavailable state.
- The dashboard task-detail run controls consume these existing run APIs
  directly: Start first queries `GET /api/repos/{repo_id}/orchestrator-config`
  and then sends `{repo_id, task_id, acknowledge_blocked, launch}` to
  `POST /api/runs`; Stop sends `POST /api/runs/{run_id}/stop`, and frontend
  error rendering preserves structured 409 fields instead of flattening every
  refusal to `detail`.
