---
last-updated: 2026-08-08
verified-against: 5e1702b264129f93fc0b05ac157649aaf558af77
---

# Features: Mobile Dashboard — Routed Views

Catalog of routed view behavior. Feature status/description/modules/APIs and
shell/theme/accessibility/Overview infrastructure are in `dashboard.md`.

Implemented behavior:

- Repo list, repo detail, Overview `REPOS READY`, and readiness-derived
  Attention rows share one display-readiness rule over raw `/api/repos`
  readiness plus `/api/runs`: display READY when raw-ready, or when an
  active/waiting live run exists and `git_clean` is the sole raw failure.
  Missing run data, ended/idle runs, and any additional failure fall back to raw
  readiness; Start controls, intake, backend gates, and `check_readiness` still
  consume raw readiness, and waiting runs keep their pending-answer Attention
  item. Repo cards also render a tri-state run signal as a bare leading dot
  before the repo name: yellow waiting, green active non-waiting, gray idle; the
  state word exists only in title/aria text. Display-ready cards add latest
  commit time from the shared repo `commit` field plus active task count from
  `/api/tasks` task-inventory rows; not-ready cards keep readiness failure
  reasons. Cards render a compact yellow "Housekeeping pending" warning when
  `/api/repos` reports `housekeeping_pending`.
- All-task and repo-detail views render task inventory from `/api/tasks` and
  `/api/repos/{repo_id}/tasks`, preserving frontmatter-authoritative status,
  `index_present`, and `missing`/`unknown` sentinel semantics. The all-tasks
  view filters the fetched payload client-side by repo/status plus
  case-insensitive text over task id, summary, repo, status, and blockers;
  it shows visible-of-total counts, has one Clear action, distinguishes empty
  inventory from no matches, and keeps missing-index cards visible by default.
- Task cards show live run state only as a left-border accent (yellow waiting,
  green active), with no separate dot/word row, kept distinct from the
  frontmatter status badge so protocol `blocked` is not confused with runtime
  `waiting`.
- Repo detail renders a recent-commits section from
  `GET /api/repos/{repo_id}/commits`; unreadable git shows "Commit history
  unavailable" and readable empty history shows "No commits found". It also
  renders the full housekeeping warning near the top when the marker is pending.
- Task detail fetches `GET /api/repos/{repo_id}/tasks/{task_id}` for raw task
  content, shows current/most-recent run state, links to logs, renders live
  per-session context strips from `GET /api/runs/{run_id}/sessions` for active
  runs, and renders answer forms for waiting questions from `/api/escalations`.
  Waiting-question forms send explicit answer modes: ordinary questions show
  Discuss/Answer, plan gates show Feedback/Confirm, Confirm allows empty input
  for one-tap approval, and guard 400 details appear inline.
- Task detail Start keeps one shared Backend selector and gives DEV and REVIEW
  independent Default/Standard/Excellent/Custom selectors. Presets are
  read-only; only a selected Custom role exposes editable fields. The first
  config query omits role parameters to probe split capability; later coherent
  queries include each named role profile, cache by backend plus both role
  selections, and discard stale generations. Custom seeds from an inherited
  role while retaining the other role's named selection, and its edits stay
  local across unrelated queries. Query failure or `split_profiles=false`
  disables Launch; unavailable named profiles remain visibly disabled. Submit
  posts split `dev_mode`/`review_mode` launch data, while shared backend and
  plan-gate remain orthogonal. Stop posts to
  `POST /api/runs/{run_id}/stop`. `blocked` tasks require explicit
  acknowledgement; stale/raced starts render the backend 409 response.
- Start is hidden or replaced with an explanation when the dashboard already
  sees a live run for the repo, the task file is missing/unknown or otherwise
  non-runnable, or best-effort `/api/repos` readiness reports launch blockers.
- Repo admission pre-reflects the cross-kind gate without changing operational
  presentation: repo detail withholds New Task Intake while an active/waiting
  run holds the repo and links to its task controls; task detail withholds Start
  while drafting/awaiting_review/revising/confirming intake state holds the repo
  and links to intake review. Structured admission 409s repaint into the same
  linked explanation. These branches precede readiness, but do not change
  READY/NOT READY, Needs attention, or run-state signals.
- Run history enters on the newest fixed-50 window from
  `GET /api/history/runs`; Previous/Next refetch by echoing the server's opaque
  direction-carrying cursors, so insert/prune races do not create overlap or
  gaps. The pager shows the server's traversal-window position/count, disables
  unavailable boundaries, and gives empty history and navigable past-the-end
  windows distinct states.
- Repo/state/task-id filters compose client-side over only the loaded window
  before grouping by `(repo, task)`; count/no-match text explicitly names that
  page-local scope. Every newly encountered task group starts collapsed, while
  explicit expansion choices survive filter repaints and page navigation for
  groups encountered again; each header and rollup stays visible. Each group shows
  per-run session strips from persisted snapshots plus a rollup of observed dev
  sessions vs frontmatter `session-est`, peak per-session context, summed run
  wall-clock from `started_ts`/`ended_ts`, and each run's embedded
  `effective_config` launch snapshot as an out-of-link disclosure. Snapshot
  rows use authoritative `profiles.dev`/`profiles.review`, identify Custom from
  CLI model provenance, and derive both roles from legacy top-level `profile`
  when the additive fields are absent. Evaluation-enabled history payloads also
  carry one deduplicated newest-evaluation state per task group; the inline
  `Evaluation →` action shows a compact grey/amber/green light for not
  evaluated, L1, or L1 + L2, with the exact state only in title/ARIA text.
- A central evaluation-report feed at `#/reports` is entered through the gated
  `All evaluation reports →` link owned by the Run-history page in every state:
  populated, zero runs, or a cursor page whose runs vanished. The view probes
  evaluation availability before fetching `GET /api/evaluation/reports`, keeps
  the server's ascending global order, shows sequence/source/evaluator/L1+L2 and
  explicit run/git provenance, and links coherent source rows to the existing
  per-task evaluation detail. Forward paging sends the server's `next_cursor`;
  exact Previous navigation uses a client-side trail of visited cursors because
  sequence gaps make arithmetic reconstruction unsound. Empty feed, past-end
  page, and disabled subsystem remain distinct states.
- Log view uses recent logs for finished runs and fetch/ReadableStream SSE for
  live runs. It renders the `spawn.out` diagnostic when the recent-log route has
  no orchestrator log, and a live stream that ends before any log line falls
  back to that recent-log diagnostic. SSE heartbeats are skipped, blank data log
  lines are preserved, `event: end` closes the stream, navigation/reload/pause
  aborts the active stream, and untrusted log content is appended as text nodes.
- A run-scoped task view at `#/runs/{run_id}/task` fetches
  `GET /api/runs/{run_id}/task` and shows the active/archived source. Overview
  Current activity, Run history, and the log view's task back-link share one
  destination rule: live `active`/`waiting` runs with repo+task attribution
  open full task detail for controls, questions, and session strips; an ended
  run with a task id (or a live run missing repo attribution) opens the
  run-scoped active-then-archive view; cards without a task id retain direct-log
  fallback, while the already-open log view falls back to Runs. Both task views
  retain `View logs`; Recent outcomes, Needs attention, Repos, and Tasks are
  unchanged, and Tasks remains active-task-only.
- A task-evaluation sidecar view at
  `#/evaluations/{repo}/{task}[/{evaluation_id}]` probes `GET /api/evaluation`
  first, then lists and reads `/api/repos/{repo}/evaluations/{task}` artifacts
  for exact active-or-archived task ids. Active Task detail deliberately has no
  evaluation entry and issues no evaluation availability/list request.
  Run-history task groups are the entry point: enabled history responses render
  gated `Evaluation →` links inline in the rollup summary, outside the collapse
  toggle, with a batched newest-evaluation light derived from the same newest id
  the sidecar opens when no explicit id is in the route. Disabled hubs render no
  history action or marker, and the evaluation route shows a concise disabled
  explanation when gated off.
- The evaluation view renders two visually distinct layers over the same
  evidence: deterministic Layer 1 static metrics and optional Layer 2 semantic
  model judgments. Generate posts `/metrics`; Run semantic evaluation posts
  `/semantic/job`, reads persisted job state on every load, replaces the trigger
  with a running panel while `queued`/`running`, polls with navigation teardown,
  shows bounded failed/error diagnostics with Retry, and selects the completed
  job's `evaluation_id` so the fresh semantic report paints.
  The view probes `GET /api/evaluation/doctor`; an enabled but unavailable
  semantic backend disables only the Layer 2 trigger with an explanation while
  Layer 1 stays usable. Semantic reports render evaluator provenance, per-axis
  rationales, severity/confidence/evidence-ref findings, suggested improvements,
  JSON and `report.md` downloads, and history-picker model/schema labels. Raw
  evidence/static/semantic JSON and Markdown are offered only through
  Copy/Download affordances, never inline raw logs.
- The remote-intake review view polls `GET /api/intakes/{id}` while an intake
  status is `drafting`, `revising`, or `confirming`; it shows live status,
  elapsed time, and a safe-to-navigate-away note, stops polling on resting
  statuses, and tears down the single poll handle on navigation like live logs.
- If an `awaiting_review` intake overlaps a recovered live run, the review view
  removes Confirm and Request changes, links to the run, and retains only
  lifecycle-legal Cancel. A raced revise/confirm `run_live` 409 repaints into
  this fail-closed state.
