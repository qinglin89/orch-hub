---
last-updated: 2026-07-26
verified-against: 3582180d93032df92e22fd39c83b64896b526583
---

# Modules: Service Surface

FastAPI app adapter and launchd service operations. Dashboard SPA assets:
`web.md`.

## API

Location: `orch_hub/api.py`

Responsibility: expose the implemented HTTP foundation as a thin FastAPI
adapter over shared core functions. `create_app(config=None, *,
run_manager=None, intake_manager=None, evaluation_service=None,
static_evaluator=None, semantic_evaluator=None, semantic_job_manager=None,
usage_service=None)`
builds an app with a
default-deny bearer-token middleware,
public `GET /api/health`, public dashboard shell routes served with
`Cache-Control: no-store`, protected repo/task routes, and protected run,
escalation, log, and history routes over one `RunManager` instance plus a
lifespan-owned `RunMonitor`, plus protected intake routes over one
`IntakeManager`. Implemented run
routes are `GET /api/repos/{repo_id}/orchestrator-config`,
`POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}`,
`GET /api/runs/{run_id}/task`, `GET /api/runs/{run_id}/sessions`,
`GET /api/runs/{run_id}/effective-config`, and
`POST /api/runs/{run_id}/stop`;
implemented escalation routes are
`GET /api/escalations` and
`POST /api/runs/{run_id}/escalations/{seq}/answer`; implemented log routes are
`GET /api/runs/{run_id}/logs` and
`GET /api/runs/{run_id}/logs/stream`; implemented history route is
`GET /api/history/runs`; implemented commit route is
`GET /api/repos/{repo_id}/commits`; implemented usage route is
`GET /api/usage`. `GET /api/repos` includes a best-effort
`commit` field and display-only `housekeeping_pending` state shared with CLI
JSON; recent commit lookup failures return `available:false` rather than
raising. `GET /api/usage` returns fail-soft Claude/Codex subscription capacity
from `UsageService` as display-only normalized provider windows, freshness,
plan, and aggregate credit data, excluding credentials/account/reset-credit
identifiers. The run-scoped task route reads active task content first and falls
back to the task archive without opening general archived-task listing. The
intake routes are `POST /api/intakes`, `GET /api/intakes`,
`GET /api/intakes/{intake_id}`, `GET /api/intakes/{intake_id}/history`,
`POST /api/intakes/{intake_id}/revise`,
`POST /api/intakes/{intake_id}/confirm`, and
`POST /api/intakes/{intake_id}/cancel`; manager errors map to 404/409/400 and
agent-turn failures are returned as auditable intake records rather than 5xx.
The evaluation routes are `GET /api/evaluation`,
`GET /api/repos/{repo_id}/evaluations/{task_id}`,
`GET /api/repos/{repo_id}/evaluations/{task_id}/{evaluation_id}`,
`GET /api/evaluation/doctor`,
`POST /api/repos/{repo_id}/evaluations/{task_id}/evidence`,
`POST /api/repos/{repo_id}/evaluations/{task_id}/metrics`,
`POST /api/repos/{repo_id}/evaluations/{task_id}/semantic/job`, and
`GET /api/repos/{repo_id}/evaluations/{task_id}/semantic/job`; they resolve exact
active-or-archived task ids outside the active task index, write/read only
hub-owned evaluation artifacts, map disabled mode to 404 except for the
availability route, and persist semantic backend/model/schema failures as
terminal failed jobs with bounded diagnostics and no partial artifact.
Evaluation detail returns `evidence`, `static_metrics`,
`semantic_report`, and `report_md`; evaluation list summaries include
best-effort semantic evaluator identity for history comparison, and the doctor
route reports Layer 2 backend availability without a model call.
Mutating intake routes return transient records
(`drafting`/`revising`/`confirming`) immediately; clients poll
`GET /api/intakes/{intake_id}` for the settled outcome.
The default intake backend is `ClaudeIntakeAgent`, constructed with
`Config.intake_model` / `Config.intake_effort`; the default semantic backend is
`ClaudeSemanticBackend`, constructed at app startup but only launched by the
semantic job manager. The default `UsageService` ingests the opt-in Claude
statusLine sink on the read path and is constructed before those Claude backends
so one shared best-effort stream observer can still record a supported Claude
usage observation without probe turns if that carrier appears. Current Claude
Code stream-json does not carry the 5h/7d windows, so unsupported output records
nothing. Default run and intake managers are
both built with
`build_notifier(config)`; run-ended /
pending-question and intake-ready delivery failures stay isolated in the owning
manager and never become route errors. The recent-log route adds a nested
`diagnostic` from
`spawn.out` only when the orchestrator log is absent and the capture is
non-empty. Module-level `app` supports
`uvicorn orch_hub.api:app`; `run(host=None, port=None)` and the `orch-hub-api`
console script serve locally by default and read host/port from environment
when arguments are omitted. The bearer middleware is pure ASGI, not
`BaseHTTPMiddleware`, so SSE streaming is not buffered and client disconnects
propagate. Browser log tailing uses `fetch`/ReadableStream with the standard
bearer header, not `EventSource` or query tokens. `RegistryError` returns HTTP
500; unknown repo/task lookups and unknown runs return HTTP 404; launch/run
state conflicts return HTTP 409 with structured details where useful.
The escalation answer route preserves omitted-mode legacy writes but normalizes
explicit `mode: "answer"|"discuss"` against question kind before writing and
audits the mode without copying answer text into history. The orchestrator-config
route is protected, not readiness-gated, and maps missing/old deployment,
nonzero, malformed, or unsupported `--print-config` responses to 502 so Start
fails closed instead of using hub defaults. `POST /api/runs` accepts either
structured `launch` (server-built argv) or legacy `extra_args`, never both.

## Launchd Operations

Location: `orch_hub/launchd.py`

Responsibility: manage the singleton macOS launchd user agent
`com.qinglin89.orch-hub`. `deploy_service` / `update` resolve the checkout,
ai-native-development path, token, host/port/data dir, plist path, log dir,
`PATH`, and optional Bark URL; render the installed plist from
`deploy/launchd/com.qinglin89.orch-hub.plist`; install/update the checkout
`.venv` package; validate with `plutil`; boot out any loaded service; and
bootstrap through `launchctl`. Required template placeholders fail before write
or load; unknown optional placeholders from newer templates render as `""` for
old-CLI/new-template update compatibility. After a successful bootout,
deploy/update poll for the label to disappear and retry transient bootstrap
exit-5/input-output failures with bounded backoff. Token precedence is explicit
option, env var, existing plist, then generated token. Bark URL precedence is
`ORCH_HUB_BARK_URL`, then existing installed-plist value, then disabled; it has
no deploy CLI flag and is never echoed because the URL embeds a device-key
secret. `installed_bark_url(plist_path=...)` reads the installed plist value for
`orch-hub notify test` when the current shell has not exported the URL.

`start_service`, `stop_service`, and `restart_service` are launchd lifecycle
helpers only. They bootstrap or boot out the fixed label and deliberately do
not call `RunManager`, write control-dir stop files, inspect run records, or
signal detached orchestrator subprocesses. `status_service` combines
`launchctl print`, installed plist parsing, and a public `/api/health` probe.

Dependencies: config, subprocess_utils, plistlib, urllib.
