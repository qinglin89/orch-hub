---
last-updated: 2026-07-10
verified-against: 30c1e08a6e706cab2d922417cf0024aa2920c5f7
---

# Modules: Web and Operations

## API

Location: `orch_hub/api.py`

Responsibility: expose the implemented HTTP foundation as a thin FastAPI
adapter over shared core functions. `create_app(config=None, *,
run_manager=None)` builds an app with a default-deny bearer-token middleware,
public `GET /api/health`, public dashboard shell routes served with
`Cache-Control: no-store`, protected repo/task routes, and protected run,
escalation, log, and history routes over one
`RunManager` instance plus a lifespan-owned `RunMonitor`. Implemented run
routes are `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}`, and
`POST /api/runs/{run_id}/stop`; implemented escalation routes are
`GET /api/escalations` and
`POST /api/runs/{run_id}/escalations/{seq}/answer`; implemented log routes are
`GET /api/runs/{run_id}/logs` and
`GET /api/runs/{run_id}/logs/stream`; implemented history route is
`GET /api/history/runs`; implemented commit route is
`GET /api/repos/{repo_id}/commits`. `GET /api/repos` includes a best-effort
`commit` field shared with CLI JSON; recent commit lookup failures return
`available:false` rather than raising. The recent-log route adds a nested `diagnostic` from
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
history, and recent/live log views; `styles.css` holds the mobile layout. The
dashboard reads only HTTP payloads, uses `fetch` +
`ReadableStream.getReader()` for SSE log streaming so the bearer header is
present, aborts active streams on navigation/reload/pause, and writes untrusted
log/task data as escaped HTML or text nodes.

Repo cards show readiness plus a bare leading run-state dot before the repo name
(yellow waiting, green active, gray idle; state word only in title/aria text);
ready cards add latest commit time and active task count from task inventory.
Task cards use only left-border run accents, with no dot/word row, distinct
from frontmatter status badges. Repo detail adds a recent-commits section with
unavailable and empty-history states.

Run controls stay frontend-only over the existing API: Start posts
`{repo_id, task_id, acknowledge_blocked, extra_args}` to `POST /api/runs` from a
launch-options dialog whose first args are always `--backend cc-codex` plus
only user-changed cc-codex overrides; it does not expose a backend selector.
Stop posts to `POST /api/runs/{run_id}/stop`, and launch refusal UI renders
structured 409 details such as readiness `reasons`, repo-busy `run_id`, or
blocked-task `acknowledge_required`. The log view renders the recent-log
route's `spawn.out` diagnostic and falls back to it when a live stream ends
before any orchestrator log line is seen.

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
