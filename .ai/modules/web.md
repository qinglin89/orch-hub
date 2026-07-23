---
last-updated: 2026-07-24
verified-against: 1a28122553522038c00c5d2b23aa4cd078a77903
---

# Modules: Web and Operations

## API

Location: `orch_hub/api.py`

Responsibility: expose the implemented HTTP foundation as a thin FastAPI
adapter over shared core functions. `create_app(config=None, *,
run_manager=None, intake_manager=None, evaluation_service=None,
static_evaluator=None, semantic_evaluator=None, semantic_job_manager=None)`
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
The default intake backend is `ClaudeIntakeAgent`; the default semantic backend
is `ClaudeSemanticBackend`, constructed at app startup but only launched by the
semantic job manager. Default run and intake managers are both built with
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

## Web UI

Location: `orch_hub/web/`

Responsibility: provide the build-stepless mobile-first dashboard shell.
`index.html` defines the static app container and navigation; CSS keeps the
bottom tab bar on phone/tablet widths and turns it into a desktop left rail
flush with the viewport edge. At desktop widths, `app.js` tags `#view` with a
view-kind class: overview/list views fill the remaining workspace, detail/form/
run-task/intake views use a bounded reading column, and logs/run history use a
wider column; top-level card grids are scoped to direct `#view > .list`
children so nested chronological feeds remain single-column newest-first.
`styles.css` also owns the CSS-only neon-wasteland industrial control-console
theme: independent Appearance axes for Mode (`Dark`/`Light`/`System` on
`data-theme`) and Palette (`Neon Cyan`/`Wasteland Orange` on `data-palette`),
token presets for graphite/electric-cyan and warm oxidized-metal/dust/ember
dark+light variants, semantic status colours kept separate from decorative
toxic-green, radiation-amber, and rust tokens, system typography, monospace
operational labels/ids/output, and static grid/scanline/bloom/edge/glow effects
without external fonts/assets or a frontend build step. Dark + Neon Cyan is the
first-visit safe fallback; a static shell-header Appearance popover persists
mode and palette under separate `localStorage` keys, an inline head script
resolves both `<html>` attributes before stylesheet paint, System follows
`prefers-color-scheme` changes while open without changing palette, and document
`color-scheme` plus browser `theme-color` track the resolved mode/palette.
The shell assets are served uncached by the API so updates reach browsers on the
next load. `app.js` owns token entry/localStorage,
bearer `fetch` calls, hash routing,
route-level headings/focus/announcements, an Operations Overview default landing
route, repo/task/detail/run views, repo/task run-state signals,
searchable/counted all-tasks inventory, recent commits, task-detail Start/Stop
run controls, live per-session context strips, waiting-question answer forms,
filterable/counted/collapsible grouped run history with est-vs-actual rollups
and effective-config disclosures, a run-scoped task-file view, task-evaluation
Layer 1/Layer 2 sidecar view and entry points, remote intake start/review views,
and recent/live log views;
`styles.css` holds the mobile-first responsive layout. The dashboard
reads only HTTP payloads, uses `fetch` +
`ReadableStream.getReader()` for SSE log streaming so the bearer header is
present, aborts active streams on navigation/reload/pause, and writes untrusted
log/task data as escaped HTML or text nodes.

Every routed view renders one visible `h1.page-title`; router/full-view
repaints sync it into `document.title` and move focus there, while background
Overview refreshes, intake ticks, and log appends do not move focus. The page
heading remains a programmatic landing target, but its visible focus ring is
gated by last input modality: keyboard navigation adds the ring, while
touch/pointer navigation keeps focus without a stray title outline. The main
`#view` is not an ARIA live region; scoped status/alert/count regions carry the
announcements. A monotonic route generation guards stale route completions:
router-owned landing/error work, each handler's own post-await paint, `rerender`
landing/error work, and intake action post-POST paint/navigation all drop when a
newer route owns the view. Capture point matters: action guards are captured
before the action POST, while non-router re-renders with no navigation capture
the live generation and still paint. Feature-owned cancellation handles remain
responsible for streams, intake polling, and Overview refresh.

The Overview route (`#/overview`) aggregates existing protected repo/task/run/
escalation/history payloads into summary metrics, prioritized attention, live
activity, and recent outcomes; each non-401 source failure degrades only its
panel, and its chained background refresh is registered before the initial load
so router teardown cancels stale paints and polling after navigation. Its repos
ready metric and readiness-derived Attention rows use the same display-readiness
rule as repo views: raw-ready, or active/waiting live run with `git_clean` as
the sole raw failure.

Repo cards and repo detail present readiness through a shared display rule over
raw `/api/repos` readiness plus `/api/runs`: show READY when raw-ready, or while
an active/waiting live run has only `git_clean` failing; missing run data,
ended/idle runs, and additional failures fall back to raw readiness. This is
presentation-only: Start controls, intake, backend gates, and
`check_readiness` still consume raw readiness, and waiting runs keep their
pending-answer Attention item. Repo cards also show a bare leading run-state dot
before the repo name (yellow waiting, green active, gray idle; state word only
in title/aria text); display-ready cards add latest commit time and active task
count from task inventory. Cards and repo detail show a yellow "Housekeeping
pending" warning from the shared repo payload when `.ai/.housekeeping-pending`
exists.
Task cards use only left-border run accents, with no dot/word row, distinct
from frontmatter status badges. Repo detail adds a recent-commits section with
unavailable and empty-history states.

The all-tasks view consumes one `/api/tasks` payload per route entry, then
filters client-side by repo/status and case-insensitive task text (id, summary,
repo, status, blockers). It keeps missing-index cards outside status/search
filters, shows visible-of-total task counts, distinguishes empty inventory from
no matches, and has one Clear action for all task filters.

Run controls stay frontend-only over protected HTTP APIs: Start opens a
four-mode launch dialog (Default, Standard, Excellent, Custom) that queries
`GET /api/repos/{repo_id}/orchestrator-config` for the selected backend/profile.
Preset modes render read-only DEV/REVIEW `agent:model @ effort` rows from the
orchestrator; Custom renders editable model/effort inputs in the same row and a
cc-codex-only dev-agent select. Query failures disable Launch and surface the
orchestrator/parser error; Standard/Excellent mode options are disabled with a
visible reason when `available_profiles` omits them. Submit posts
`{repo_id, task_id, acknowledge_blocked, launch}` to `POST /api/runs`, and the
plan-gate checkbox sets `launch.plan_gate=true` independently of mode/backend.
Stop posts to `POST /api/runs/{run_id}/stop`; while a live run record carries
`stop_requested`, the Stop control renders disabled as "Stop requested" from
server state so reloads and other devices do not re-enable it. Launch refusal UI
renders structured 409 details such as readiness `reasons`, repo-busy `run_id`,
or blocked-task `acknowledge_required`. The log view renders the recent-log
route's `spawn.out` diagnostic and falls back to it when a live stream ends
before any orchestrator log line is seen; its task back-link sends live
`active`/`waiting` runs with repo+task attribution to the full task detail
route for controls/questions/session strips, while ended or
attribution-incomplete runs use `#/runs/{run_id}/task` so archived task files
stay reachable through run history and normal task views stay active-only.

Task detail fetches `GET /api/runs/{run_id}/sessions` for live runs only and
renders compact protocol-session strips against the returned context budget.
Waiting-question forms render two kind-aware explicit-mode buttons:
Discuss/Answer for ordinary escalations and Feedback/Confirm for `plan-gate`;
plan-gate Confirm may post empty text for one-tap approval, while other empty
replies are blocked locally and server-side guards still own correctness.
Run history consumes the embedded `sessions`/`context_budget` and `session_est`
fields from `GET /api/history/runs`, filters the already-fetched newest-first
list by repo, run state, and task-id text before grouping by `(repo, task)`,
shows filtered group/run counts plus distinct empty/no-match states, preserves
log links on visible run cards, renders each run's embedded `effective_config`
snapshot as a disclosure outside the log-link card, and lets each task group
collapse without hiding its rollup. Rollups show observed dev-session count vs
estimate, peak context, and summed wall-clock from run-record ISO timestamps.

Task evaluation uses `#/evaluations/{repo}/{task}[/{evaluation_id}]` with the
Tasks tab active. It probes availability first, distinguishes disabled,
never-generated, evidence-only, static-metrics, and semantic-report states,
renders target active/archive state plus deterministic score/run/protocol/
evidence-warning rollups, and offers raw evidence/static/semantic JSON plus
semantic `report.md` only as Copy/Download artifacts. Task detail links and
run-history inline rollup links appear only when availability reports enabled;
run-history links stay outside collapse toggles so activation navigates without
toggling. Generate posts `/metrics` and re-renders to the latest deterministic
Layer 1 report without mutating target task or run state. Run semantic
evaluation posts `/semantic/job`, reads the persisted current job on every view
load, replaces the trigger with a running panel for `queued`/`running`, polls via
a single chained timeout torn down on navigation, shows `failed`/`error`
diagnostics with Retry, and selects the completed job's `evaluation_id` so the
fresh semantic report paints. A best-effort `GET /api/evaluation/doctor` probe
disables only the Layer 2 start/retry trigger when the semantic backend is
unavailable while keeping Layer 1 usable; a failed probe degrades to the default
enabled trigger. Semantic reports render evaluator provenance, per-axis
rationales, severity/confidence/evidence-ref findings, and suggested
improvements.

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
