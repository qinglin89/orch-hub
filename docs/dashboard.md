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
URL fragments, SSE URLs, or server logs. A 401 clears the stored token and
returns to the login gate.

The login gate says where the token comes from: on the Mac running orch-hub,
`orch-hub open` opens the dashboard and copies the token to the clipboard, and
`orch-hub token --copy` only copies it. Paste it into the token field and press
Connect. Where the browser can read the clipboard (a secure context, such as
https or the `localhost` page `orch-hub open` uses, exposing
`navigator.clipboard.readText`), the gate also shows a **Paste** button that
fills the field from the clipboard, trimmed. Paste never submits; a refused
clipboard permission or an empty clipboard leaves a short note instead. Over a
plain-http tailnet address the button is absent, and an ordinary paste into the
field works as before.

Use only a trusted device and browser profile. The HTTP service must still stay
on localhost or inside a private tailnet; bearer authentication is not a
replacement for that network boundary.

## Main views

| View | What it shows |
|---|---|
| Overview | Repos ready, active tasks, current activity, outcomes needing attention, and display-only Claude/Codex capacity |
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
evaluation surfaces. Neither becomes repo health or a launch gate.

## First run: adding a repo

orch-hub lists only repos registered with Mandrel. When the repo list loads
successfully and is empty, Overview, Repos, and Tasks show a **Get started**
card instead of an empty state. The Overview also omits its all-clear texts
("Nothing needs attention right now.", "No live runs.", "No recent runs.") so
an empty hub never reads as healthy. A repo list that failed to load is not
this state; those views keep their usual error or unavailable handling. The
card offers two routes:

- **Tell your coding agent.** *Copy prompt for your agent* copies a prompt that
  points the agent at the
  [add-a-repo guide](https://github.com/qinglin89/orch-hub/blob/dev/docs/agent-setup.md#add-a-repo).
  Replace `/path/to/your-repo` before sending it. Without clipboard access (a
  plain-http address) the button says so, and the prompt text stays
  selectable.
- **Run it yourself.** Deploy with
  `~/.local/share/mandrel/bin/mandrel deploy --bootstrap-orchestrator /path/to/your-repo`
  (the installer's default Mandrel location; use your own checkout's
  `bin/mandrel` if Mandrel lives elsewhere), commit `.gitignore` and
  `.ai-deploy-lock.json`, then run `/ai-init` in that repo with your coding
  agent and commit `.ai/`.

The card only explains and copies text. Every step runs on the Mac; no
dashboard control writes into a target repo. Once Mandrel registers the repo,
it appears on the next load or Overview refresh.

## Readiness fixes

Wherever the dashboard lists failed readiness checks (repo cards, repo detail,
the task-detail run controls, and the New task intake refusal), each check shows
its message and, under it in muted text, a one-line **Fix:** hint from the hub.
The hint is usually a command to run on the Mac, for example
`<mandrel>/bin/mandrel deploy --bootstrap-orchestrator <repo>` for a missing
deploy piece, or the first few uncommitted paths for a dirty tree. Overview
attention rows stay a compact list of failed check names; their hints ride the
row's tooltip, and the row opens repo detail, where they are listed in full.
Hints are guidance only. They never change which checks fail or what gates a
launch or intake.

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

Ended runs show a bounded recent log tail. Live runs use `fetch` plus a
`ReadableStream` reader so the bearer header remains attached; browser
`EventSource` is not used. Log text is inserted as text, not HTML.

When evaluation is enabled, Run history links each task group to its evaluation
sidecar. Layer 1 generates deterministic metrics. Layer 2 starts an asynchronous
Claude-backed job, persists its state, and reports bounded failure diagnostics.
Complete Layer 1 + Layer 2 evaluations for archived tasks can appear in the
global report feed. See [Task evaluation](evaluation.md) for the artifact and
provenance contract.
