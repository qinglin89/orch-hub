---
last-updated: 2026-07-13
verified-against: 16d166c038b4e7232be688c95c7b9a4b5f360518
---

# Modules: Web and Operations

## API

Location: `orch_hub/api.py`

Responsibility: expose the implemented HTTP foundation as a thin FastAPI
adapter over shared core functions. `create_app(config=None, *,
run_manager=None, intake_manager=None)` builds an app with a default-deny bearer-token middleware,
public `GET /api/health`, public dashboard shell routes served with
`Cache-Control: no-store`, protected repo/task routes, and protected run,
escalation, log, and history routes over one `RunManager` instance plus a
lifespan-owned `RunMonitor`, plus protected intake routes over one
`IntakeManager`. Implemented run
routes are `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}`,
`GET /api/runs/{run_id}/task`, and `POST /api/runs/{run_id}/stop`;
implemented escalation routes are
`GET /api/escalations` and
`POST /api/runs/{run_id}/escalations/{seq}/answer`; implemented log routes are
`GET /api/runs/{run_id}/logs` and
`GET /api/runs/{run_id}/logs/stream`; implemented history route is
`GET /api/history/runs`; implemented commit route is
`GET /api/repos/{repo_id}/commits`. `GET /api/repos` includes a best-effort
`commit` field and display-only `housekeeping_pending` state shared with CLI
JSON; recent commit lookup failures return `available:false` rather than
raising. The run-scoped task route reads active task content first and falls
back to the task archive without opening general archived-task listing. The
intake routes are `POST /api/intakes`, `GET /api/intakes`,
`GET /api/intakes/{intake_id}`, `GET /api/intakes/{intake_id}/history`,
`POST /api/intakes/{intake_id}/revise`,
`POST /api/intakes/{intake_id}/confirm`, and
`POST /api/intakes/{intake_id}/cancel`; manager errors map to 404/409/400 and
agent-turn failures are returned as auditable intake records rather than 5xx.
Mutating intake routes return transient records
(`drafting`/`revising`/`confirming`) immediately; clients poll
`GET /api/intakes/{intake_id}` for the settled outcome.
The default intake backend is `ClaudeIntakeAgent`. The
recent-log route adds a nested `diagnostic` from
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

## Web UI

Location: `orch_hub/web/`

Responsibility: provide the build-stepless mobile dashboard shell. `index.html`
defines the static app container and bottom navigation; the shell assets are
served uncached by the API so updates reach browsers on the next load. `app.js`
owns token entry/localStorage, bearer `fetch` calls, hash routing,
repo/task/detail/run views, repo/task run-state signals, recent commits,
task-detail Start/Stop run controls, waiting-question answer forms, run
history, a run-scoped task-file view, remote intake start/review views, and
recent/live log views; `styles.css` holds the mobile layout. The dashboard reads only HTTP payloads, uses `fetch` +
`ReadableStream.getReader()` for SSE log streaming so the bearer header is
present, aborts active streams on navigation/reload/pause, and writes untrusted
log/task data as escaped HTML or text nodes.

Repo cards show readiness plus a bare leading run-state dot before the repo name
(yellow waiting, green active, gray idle; state word only in title/aria text);
ready cards add latest commit time and active task count from task inventory.
Cards and repo detail show a yellow "Housekeeping pending" warning from the
shared repo payload when `.ai/.housekeeping-pending` exists.
Task cards use only left-border run accents, with no dot/word row, distinct
from frontmatter status badges. Repo detail adds a recent-commits section with
unavailable and empty-history states.

Run controls stay frontend-only over the existing API: Start posts
`{repo_id, task_id, acknowledge_blocked, extra_args}` to `POST /api/runs` from a
launch-options dialog whose first args are always `--backend cc-codex`, followed
by only user-changed cc-codex overrides and, when enabled, a bare
`--plan-gate`; it does not expose a backend selector.
Stop posts to `POST /api/runs/{run_id}/stop`, and launch refusal UI renders
structured 409 details such as readiness `reasons`, repo-busy `run_id`, or
blocked-task `acknowledge_required`. The log view renders the recent-log
route's `spawn.out` diagnostic and falls back to it when a live stream ends
before any orchestrator log line is seen; its task back-link routes to
`#/runs/{run_id}/task` so archived task files stay reachable through run
history while normal task views stay active-only.

Remote intake stays separate from run controls: repo detail posts a natural
language request to `POST /api/intakes`, the review view displays the complete
draft task file and index row plus metadata-only history, Request changes posts
to revise, Cancel terminates the intake, and Confirm creates the task. While
draft/revise/confirm is transient, the review view shows an in-flight panel with
status, elapsed time from `updated_ts`, a safe-to-navigate-away note, and a poll
of `GET /api/intakes/{id}`; the poll uses one module-level handle that is torn
down on navigation like the log stream. A confirmed intake routes to the created
task's existing detail/Start flow.

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
