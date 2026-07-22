---
last-updated: 2026-07-22
verified-against: 9d6d2609fb57375489cc8358d30224efddd7ba82
---

# Features: Core

## Repo Discovery and Readiness

Status: implemented

Description: Load managed repos from ai-native-development and report whether each is ready for orchestration. This validates the registry and target-repo deployment assumptions before service/UI work.

Modules: config, registry, readiness, git_meta, housekeeping, models,
subprocess_utils, cli.

APIs: `python -m orch_hub.cli repos`, `python -m orch_hub.cli repos --json`,
`GET /api/repos`, `GET /api/repos/{repo_id}/commits`.

Implemented behavior:

- Discovery invokes `<ai-native-development>/aii-2 registry list --json` by absolute path, then falls back to `<ai-native-development>/.registry/repos.local.json` when the command is unavailable or exits non-zero.
- Registry data is a JSON array of `{name, path, manifest}`. Missing fallback file means no repos; invalid JSON or malformed entries raise an operational error.
- Readiness runs seven checks in stable order: repo path, manifest, deployed orchestrator script, orchestrator venv Python, `.ai-tasks/index.md`, clean `git status --porcelain`, and passing `aii-2 status <repo>`.
- Public readiness JSON serializes `ready` and only failed checks as `{name, message}` reasons; ready repos have `checks: []`.
- `git_meta` reads display-only commit history separately from readiness:
  `latest_commit(repo)` returns `CommitInfo | None`, and `read_commits(repo,
  limit)` returns `CommitLog(available, commits)` newest-first with `limit`
  clamped to `1..50`. Missing git, non-git dirs, missing paths, and failed
  `git log` degrade to unavailable/`None` rather than hiding repo content.
- `housekeeping_pending(repo)` reads the display-only
  `.ai/.housekeeping-pending` marker and returns `housekeeping_pending`; it
  never changes launch readiness or run-start gating.
- CLI `repos --json` and `GET /api/repos` share `repos_payload`; each repo now
  includes `commit: null` or `{hash, short_hash, committed_ts, subject}` plus
  `housekeeping_pending`. Human CLI output remains unchanged.
- `GET /api/repos/{repo_id}/commits` returns recent commits for repo detail;
  unknown repos are 404, but commit lookup failure is HTTP 200 with
  `available: false` and an empty list.
- Tests cover registry parsing/fallbacks, every requested missing-piece readiness failure, dirty git state, `aii-2 status` failure, config, and CLI output.

## Task Inventory

Status: implemented

Description: Read `.ai-tasks/index.md` and task frontmatter in managed repos. Distinguish task-file statuses from hub run overlay statuses.

Modules: task_store, api, models.

APIs: `GET /api/tasks`, `GET /api/repos/{repo_id}/tasks`,
`GET /api/repos/{repo_id}/tasks/{task_id}`.

Implemented behavior:

- `list_repo_tasks(repo)` reads `.ai-tasks/index.md` for active task ids and human summaries, preserving index row order.
- Task cells may be bare ids or full-cell Markdown links such as
  `[id](id.md)`; link targets are reduced to the target basename with one
  trailing `.md` stripped before file lookup and API payloads, so directory
  prefixes cannot steer reads outside `.ai-tasks`.
- Indexes may use either the legacy task-column layouts or the five-column
  `id | title | status | session-est | blockers` layout; the five-column
  `title` column supplies the summary, while other layouts keep the legacy
  positional third-column summary convention.
- The index Status column is not authoritative; each `.ai-tasks/<id>.md` frontmatter supplies task status and blockers.
- Missing `.ai-tasks/index.md` is represented as `index_present=False` with no task rows, distinct from a present-but-empty index.
- Missing listed task files use status `missing`; files with no parseable status use `unknown`.
- `TaskSummary`, `RepoTaskInventory`, and `tasks_payload` provide reusable data shapes for future CLI/API adapters.
- The single-task detail endpoint returns frontmatter-authoritative fields plus
  `present` and raw `content`; raw reads require normalized index membership
  and resolved containment under `.ai-tasks`, so symlinked files outside the
  task dir are not disclosed.
- Exact-id run-history helpers can read archived task files after close-out and
  parse active-then-archive `session-est: current/total` into a typed
  `SessionEst`; malformed or missing estimates degrade to unknown.

## Task Evaluation Foundation

Status: implemented

Description: Build optional read-only evidence packs for one active or archived
task so later static metrics and semantic evaluators have bounded task/run/git
input without changing normal run-control state.

Modules: evaluation, config, task_store, runs, history, sessions, logs,
git_meta, models.

APIs: internal `EvaluationService.resolve_target`, `related_runs`,
`build_evidence`, `generate_evidence`, `list_evaluations`, `read_artifact`,
and `artifact_dir`; protected `GET /api/evaluation` and
`/api/repos/{repo_id}/evaluations/{task_id}` evidence routes.

Implemented behavior:

- `ORCH_HUB_EVAL_ENABLED` defaults true for local use; falsy tokens disable the
  service fail-closed, and malformed explicit values raise config errors.
- Target resolution reads active `.ai-tasks/<task_id>.md` first, then
  `.ai-tasks/archive/<task_id>.md`, by exact id and contained path checks.
- Related-run aggregation includes every persisted run with exact
  `(repo_path, task_id)` identity and excludes other repos/tasks and unreadable
  unattributed run dirs.
- Evidence packs include task content/archive state, related run records,
  metadata-only events, persisted session snapshots, bounded orchestrator-log
  and `spawn.out` excerpts with size/truncation metadata, terminal record fields,
  explicit warnings, and best-effort git commit-window metadata.
- Git evidence uses ambient commit-date history reachable from HEAD over the
  run lifecycle window, capped at 50 commits; any related run with no parseable
  end keeps `until` open-ended.
- `generate_evidence` persists `evidence.json` atomically under
  `<data_dir>/evaluations/<repo>/<task>/<evaluation-id>/`, cleaning an empty
  reserved dir on failure. It never mutates target repos, task files, run
  records, `.ai/`, or `.ai-tasks/`.
- `list_evaluations` enumerates persisted evaluation dirs newest-first by
  `(timestamp, numeric collision suffix)` and reports only which artifact files
  exist; `read_artifact` returns parsed `evidence.json` or `static_metrics.json`
  for a contained evaluation dir and lets corrupt JSON surface loudly.
- HTTP routes expose availability, persisted evaluation listing, evaluation
  detail readback, and evidence generation while preserving the same disabled
  fail-closed gate: only `GET /api/evaluation` returns 200 while disabled; the
  action/read routes 404.
- Static scores are still computed by the separate Layer 1 static metrics layer;
  no semantic model calls are part of this foundation.

## Task Static Metrics

Status: implemented

Description: Compute deterministic, non-semantic Layer 1 task-quality metrics
from one evidence pack and persist the report beside the pack.

Modules: static_metrics, evaluation, config, models.

APIs: internal `StaticEvaluator.run_metrics`, `build_report`,
`generate_metrics`, and `STATIC_METRICS_NAME`; protected
`POST /api/repos/{repo_id}/evaluations/{task_id}/metrics` plus evaluation detail
readback.

Implemented behavior:

- `StaticEvaluator` shares the evaluation subsystem's
  `ORCH_HUB_EVAL_ENABLED` fail-closed gate and raises
  `EvaluationDisabledError` when disabled.
- `run_metrics(pack)` derives run/session/event rollups from pack evidence:
  related run count, evidence-only terminal outcome partition, failure count,
  failed-without-diagnostic count, human-input count, dev/review/plan-gate
  session counts, turn/context aggregates, duration windows, warning count,
  and active/archive target source.
- `build_report(pack)` parses the task file for frontmatter legality,
  required sections, session-log entry counts, review coverage, verdict and
  remediation counts, blocker/fix-set/prefetch facts, status-transition
  continuity, final-status match, and completed-task final `Open: none`.
- Static reports carry five transparent score axes: protocol,
  reliability/diagnostics, handoff structure, estimate fit, and
  knowledge-maintenance signals. Penalties append evidence-naming warnings;
  missing or partial evidence lowers confidence or makes an axis not
  assessable instead of crashing.
- Two knowledge signals are intentionally omitted because the evidence pack
  cannot derive them: per-commit touched file paths and archived-task blocker
  references across other tasks.
- `generate_metrics(repo, task_id, evaluation_id=None)` delegates to
  `EvaluationService.generate_evidence`, writes `static_metrics.json`
  atomically in the same evaluation artifact directory as `evidence.json`, and
  cleans the reserved directory on report-write failure so no half-pair
  remains.
- `POST /api/repos/{repo_id}/evaluations/{task_id}/metrics` runs a fresh
  deterministic evidence+static refresh and returns the new report; the detail
  route can later read the paired `evidence` and `static_metrics` artifacts.
- The dashboard renders the deterministic Layer 1 report, keeps semantic
  evaluation explicitly future/unavailable, and offers raw JSON only through
  copy/download affordances rather than inline log/evidence display.
- Static metrics have no semantic model call; semantic evaluation remains a
  downstream task.
- Tests cover run/session rollups, protocol parsing, score axes, partial
  evidence behavior, persistence, disabled mode, and prior protocol
  false-negative regressions.

## HTTP API Foundation

Status: implemented

Description: Expose current repo readiness and task inventory data through a
resident FastAPI app without duplicating core logic, protected by bearer-token
auth on every non-health path.

Modules: api, config, registry, readiness, task_store, models.

APIs: `GET /`, `GET /app.js`, `GET /styles.css`, `GET /api/health`,
repo/task/run/escalation/log/history/intake/evaluation data routes; `python -m orch_hub.api`;
`orch-hub-api`; `uvicorn orch_hub.api:app`.

Implemented behavior:

- `create_app(config=None)` builds a FastAPI app, capturing explicit config for
  tests or `Config.from_env()` by default.
- `GET /api/repos` returns the same shape as CLI `repos --json`, including
  readiness failure reasons, best-effort latest commit metadata, and
  display-only housekeeping marker state.
- Task endpoints return the `tasks_payload` envelope, preserving
  `index_present`, ordered tasks, frontmatter-authoritative status/blockers,
  and task-store sentinel statuses.
- Malformed registry data maps to HTTP 500; unknown repo ids on per-repo task
  lookup map to HTTP 404.
- Per-repo commit history lookup failure returns data (`available:false`), not
  a route error, so repo/task views stay visible when git metadata is absent.
- `GET /api/health` is public and minimal (`{"status": "ok"}`); it no longer
  exposes deployment paths.
- A default-deny middleware protects every path except `/api/health` and the
  three dashboard shell assets. Missing/malformed or invalid credentials return
  401; unset `ORCH_HUB_TOKEN` returns 503 on protected data routes.
- The three dashboard shell assets (`/`, `/app.js`, `/styles.css`) are served
  with `Cache-Control: no-store` so browser heuristic caching cannot hide a new
  build after `orch-hub launchd update`.
- `run()` and `orch-hub-api` read `ORCH_HUB_HOST` / `ORCH_HUB_PORT` and bind
  to `127.0.0.1:8765` by default; any private exposure should remain
  Tailscale-only.

## Auth and Private Access

Status: implemented

Description: Protect the service with bearer-token auth and deploy it behind Tailscale/private networking.

Modules: api, config.

Implemented behavior:

- `Config.token` is loaded from `ORCH_HUB_TOKEN`; empty/whitespace-only values
  count as unset.
- Every HTTP path except `/api/health` and the public dashboard shell assets
  requires `Authorization: Bearer <token>` when configured.
- The middleware is pure ASGI rather than Starlette `BaseHTTPMiddleware` so
  streaming responses are not buffered and client disconnects propagate.
- Protected routes fail closed with HTTP 503 when the token is unset; there is
  no unauthenticated mode.
- Auth error responses are static and never echo presented credentials.
