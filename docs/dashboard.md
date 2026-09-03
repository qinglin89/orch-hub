# Dashboard behavior

orch-hub serves a dependency-free, mobile-first single-page application from
the same FastAPI process as its protected data routes. It is designed for one
operator checking a portfolio from a phone, tablet, or another computer on a
private network.

## Login and credential handling

The bootstrap shell (`/`, `/app.js`, and `/styles.css`) is public because a
browser cannot attach a bearer header to ordinary page navigation. The shell
contains no repo data or secret.

On first load, the dashboard asks for the bearer token and stores it in that
browser profile's `localStorage`. Every data request carries
`Authorization: Bearer <token>`. Tokens are not put in cookies, query strings,
SSE URLs, or server logs. A 401 clears the stored token and returns to the login
gate.

Use only a trusted device and browser profile. The HTTP service must still stay
on localhost or inside a private tailnet; bearer authentication is not a
replacement for that network boundary.

## Main views

| View | What it shows |
|---|---|
| Overview | Repos ready, active tasks, current activity, outcomes needing attention, display-only Claude/Codex capacity, and the cross-repo Layer 2 evaluation state with its per-job cancel control |
| Repos | Registry-backed repo cards with readiness, live-run state, active-task counts, latest commit, and housekeeping warnings |
| Repo detail | One repo's readiness, commits, task inventory, live run, and remote-intake entry point |
| Tasks | Cross-repo active task inventory with repo, status, text, and blocker filters |
| Task detail | Task frontmatter/body, current or recent run, session context, Start/Stop controls, logs, and pending questions |
| Runs | Cursor-paged history grouped by repo/task, including session rollups and authoritative launch snapshots |
| Log view | Bounded recent tail for ended runs or a bearer-authenticated live SSE stream |
| Intake review | Draft/revise/confirm/cancel lifecycle with a metadata-only audit trail |
| Task evaluation | Layer 1 metrics, optional Layer 2 reports, evidence/provenance, and evaluation history |
| Evaluation reports | Globally ordered cross-repo feed of complete archived-task reports |

The Overview refreshes while visible. Missing subscription-usage data degrades
only its own panel; evaluation failures remain isolated to history and
evaluation surfaces. Neither becomes repo health or a launch gate. The Layer 2
section is a read with one control, and it changes only what gets *measured*:
cancelling stops one attempt, killing its model turn if one is running, and
touches neither readiness, run state, nor launch/intake admission. A Cancel
pressed after that job's turn has already returned reports that the job is
settling instead of stopping it; the row shows the outcome it reached on the next
refresh. Nothing on this section can start an evaluation — a Layer 2 turn begins
only from a task's own evaluation view.

## Starting a run

The task-detail Start dialog queries
`GET /api/repos/{repo_id}/orchestrator-config`. orch-hub understands Mandrel's
current backend/profile launch schema; the target repo's deployed orchestrator
is the source of truth for resolved defaults and published model/effort choices.

- One run has one shared backend.
- DEV and REVIEW choose their profiles independently.
- Each role can inherit Default, select an available named profile such as
  Standard or Excellent, or use Custom overrides.
- On the `cc-codex` backend, a Custom role selects its own agent, model, and
  effort as a complete tuple.
- Option catalogs are filtered by backend, role, and agent. When a catalog is
  absent, that role degrades to explained free text rather than a hub-invented
  list. An uncatalogued model remains reachable through Other.
- Changing an agent clears model and effort values that belonged to the prior
  agent.
- The optional plan gate is independent of profile selection.

If the config query fails or reports a deployment too old to accept role-local
launch flags, Start is disabled with the orchestrator's reason. The hub never
guesses a launch policy.

Start is also withheld when the task is not runnable, the repo fails raw launch
readiness, another run is live, or a non-terminal remote intake owns the repo's
admission slot. A `blocked` task requires explicit acknowledgement.

## Human questions and stopping

A waiting run exposes its current question on task detail. Ordinary questions
offer Discuss and Answer; plan gates offer Feedback and Confirm. The server
normalizes those modes against the question kind and rejects an answer whose
button intent would be ambiguous.

Stop requests are graceful. The dashboard writes the control-dir `stop.flag`
through `POST /api/runs/{run_id}/stop`; the scheduler consumes it at the next
session boundary or during an outstanding human-input wait. It does not kill
the current coding-agent process immediately. Stopping the launchd service is a
different operation and does not stop detached runs.

## Remote task intake

New Task Intake starts a Claude-backed draft without writing `.ai-tasks/` in
the target repo. The operator can review the complete proposed task and index
entry, request changes through the same intake session, confirm, or cancel.
Only confirmation reaches the write path.

Starting an intake requires the same full repo readiness report as launch,
including a clean worktree and deployed scheduler. A live run and a
non-terminal intake cannot coexist for one repo. The UI shows the blocking run
or intake and links to the surface where the operator can resolve it.

## History, logs, and evaluation

Run history is server-paged and newest first. Each task group keeps its
frontmatter estimate beside observed development sessions, per-session context,
elapsed run time, events, and the effective configuration captured from the
run itself. Historical task links continue working after task closeout by
falling back to the local task archive.

Each run card also carries a `Protocol identity:` disclosure: whether the hub
can prove which deployed protocol that run ran under end to end, and when it
cannot, which reason applies. See
[Protocol provenance](evaluation.md#protocol-provenance).

`Protocol identity coverage →` opens the aggregate of that field over every run
and every published task (`#/protocol`): how many carry a verified identity and
what the reasons are for the rest. A republished task counts once, under its
newest report, with the report-version figures shown beside the task ones. It is
a read — it starts no check and repairs no run — and it stays available with the
evaluation sidecar switched off, since the run half is a property of run records.

Ended runs show a bounded recent log tail. Live runs use `fetch` plus a
`ReadableStream` reader so the bearer header remains attached; browser
`EventSource` is not used. Log text is inserted as text, not HTML.

When evaluation is enabled, Run history links each task group to its evaluation
sidecar. Layer 1 generates deterministic metrics. Layer 2 starts an asynchronous
Claude-backed job, persists its state, and reports bounded failure diagnostics.
Complete Layer 1 + Layer 2 evaluations for archived tasks can appear in the
global report feed. See [Task evaluation](evaluation.md) for the artifact and
provenance contract.
