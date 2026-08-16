---
last-updated: 2026-08-16
verified-against: cad31851334def4258d4004c2e62516807aaff85
---

# Modules: Web UI Views

Routed view catalog of the dashboard SPA. Shell, theme, accessibility/route
infrastructure, and the Overview route: `web.md`.

## Repo and task inventory views

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

## Run controls, shared destinations, and logs

Run controls stay frontend-only over protected HTTP APIs: Start keeps one shared
Backend selector and independent DEV/REVIEW Default/Standard/Excellent/Custom
selectors. Presets render read-only role values; a selected Custom role alone
renders editable fields, including the cc-codex DEV agent select. The initial
config query omits role parameters to detect split capability; later queries
apply both named selections coherently, cache by backend and both roles, and
drop stale generations. Custom queries that role as inherited while retaining
the other role's named profile, then keeps edits in local dialog state. Query
failure, `split_profiles=false`, or an unavailable named profile fails closed
with a visible reason. Submit posts split `dev_mode`/`review_mode` launch data
inside `{repo_id, task_id, acknowledge_blocked, launch}`; backend and plan-gate
remain orthogonal.
Stop posts to `POST /api/runs/{run_id}/stop`; while a live run record carries
`stop_requested`, the Stop control renders disabled as "Stop requested" from
server state so reloads and other devices do not re-enable it. Launch refusal UI
renders structured 409 details such as readiness `reasons`, repo-busy `run_id`,
or blocked-task `acknowledge_required`. Repo-admission conflicts are recognized
by their structured discriminator: task detail withholds Start and links to a
non-terminal intake; repo detail withholds intake start and links to the live
run's task controls. A raced 409 repaints into the same explanation. Admission
notes precede readiness but never alter readiness badges, attention rows, or
run-state signals. The log view renders the recent-log route's `spawn.out`
diagnostic and falls back to it when a live stream ends before any orchestrator
log line is seen. Overview Current activity, Run history, and the log view's
task back-link share one task-first destination rule: live `active`/`waiting`
runs with repo+task attribution open full task detail for
controls/questions/session strips; other runs carrying a task id use
`#/runs/{run_id}/task` so archived task files stay reachable; cards without a
task id fall back to logs, while the log view falls back to Runs. Both task
destinations retain `View logs`, and normal task views stay active-only.

## Task detail and run history

Task detail fetches `GET /api/runs/{run_id}/sessions` for live runs only and
renders compact protocol-session strips against the returned context budget.
Waiting-question forms render two kind-aware explicit-mode buttons:
Discuss/Answer for ordinary escalations and Feedback/Confirm for `plan-gate`;
plan-gate Confirm may post empty text for one-tap approval, while other empty
replies are blocked locally and server-side guards still own correctness.
Run history enters on the newest fixed-50 window from `GET /api/history/runs`
and refetches adjacent windows by echoing opaque server cursors; the pager uses
the response's traversal-window position/count and cursor boundaries, including
a navigable past-the-end state. It consumes embedded
`sessions`/`context_budget`, `session_est`, and batched
`evaluation_enabled`/`evaluation_states`; filters only the loaded window by
repo, run state, and task-id text before grouping by `(repo, task)`, and labels
counts/no-match text as page-local. Newly encountered groups start collapsed;
the explicit expansion set survives filter repaints and page navigation where a
group reappears. Visible cards open the run's task through the shared
liveness-aware destination rule, with direct-log fallback only when no task id
is available; each card renders its embedded `effective_config` snapshot as a
disclosure outside the link. Enabled task groups show an inline `Evaluation →`
action with a grey/amber/green state light
for not evaluated, L1, or L1 + L2; the state is from the same newest evaluation
the sidecar route opens and is exposed through title/ARIA text, not a visible
badge or per-group list request. Effective-config rows prefer
`profiles.dev`/`profiles.review`, derive both from legacy top-level `profile`,
and label Custom from CLI model provenance. Rollups show observed dev-session
count vs estimate, peak context, and summed wall-clock from run-record ISO
timestamps.

## Report feed and evaluation sidecar

The `#/reports` central feed view first probes `GET /api/evaluation`, then reads
the forward-only global feed from `GET /api/evaluation/reports`. Run history
owns its gated `All evaluation reports →` entry through one shared page head, so
the entry remains discoverable with populated history, zero persisted runs, or
a cursor page pruned away. Feed rows preserve server `seq` order, expose
repo/task/evaluation, evaluator/completeness, timestamps, key, and explicit
run/git provenance, and link coherent identities to existing per-task detail.
Next follows `next_cursor`; Previous reuses a client-side trail of exact visited
cursors rather than computing across possible sequence gaps. Never-published,
past-end, and disabled states are distinct, and the view performs no evaluation
write/model call.

Task evaluation uses `#/evaluations/{repo}/{task}[/{evaluation_id}]` with the
Tasks tab active. It probes availability first, distinguishes disabled,
never-generated, evidence-only, static-metrics, and semantic-report states,
renders target active/archive state plus deterministic score/run/protocol/
evidence-warning rollups, and offers raw evidence/static/semantic JSON plus
semantic `report.md` only as Copy/Download artifacts. Active Task detail has no
evaluation entry and performs no evaluation availability/list read. Run-history
inline rollup links appear only when enabled and stay outside collapse toggles
so activation navigates without toggling; their state lights are supplied by the
history payload rather than by one list request per task group. Generate posts
`/metrics` and re-renders to the latest deterministic Layer 1 report without
mutating target task or run state. Run semantic
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

## Intake views

Remote intake stays separate from run controls: repo detail posts a natural
language request to `POST /api/intakes`, the review view displays the complete
draft task file and index row plus metadata-only history, Request changes posts
to revise, Cancel terminates the intake, and Confirm creates the task. While
draft/revise/confirm is transient, the review view shows an in-flight panel with
status, elapsed time from `updated_ts`, a safe-to-navigate-away note, and a poll
of `GET /api/intakes/{id}`; the poll uses one module-level handle that is torn
down on navigation like the log stream. A confirmed intake routes to the created
task's existing detail/Start flow. In a recovered live-run overlap, the review
withholds revise/confirm, retains lifecycle-legal Cancel, and links to the run;
a raced admission refusal repaints to that state.
