---
last-updated: 2026-08-16
verified-against: cad31851334def4258d4004c2e62516807aaff85
---

# Features: Remote Intake-Task

Confirmed remote task drafting. Intake review view details:
`dashboard-views.md`. Other operations surfaces: `ops.md`.

## Remote Intake-Task

Status: implemented

Description: Let the user type a request remotely, review the full proposed
task file and index entry, revise or cancel it, and only on explicit
confirmation write the new task into the target repo.

Modules: admission, intake, intake_agent, runs, api, web, models, readiness,
task_store, notifications.

APIs: protected `POST /api/intakes`, `GET /api/intakes`,
`GET /api/intakes/{id}`, `GET /api/intakes/{id}/history`,
`POST /api/intakes/{id}/revise`, `POST /api/intakes/{id}/confirm`, and
`POST /api/intakes/{id}/cancel`.

Implemented behavior:

- `IntakeManager` owns hub state under `<ORCH_HUB_DATA_DIR>/intake/<id>/`
  (`intake.json` plus metadata-only `events.jsonl`) and enforces one in-flight
  intake per repo. A shared per-repo admission section first excludes every
  persisted unended run and is held until the drafting record exists; a
  non-terminal intake symmetrically blocks run start. Admission conflicts carry
  the blocking id and remain separate from readiness.
- Draft and revise turns produce hub-owned proposals only. They are not active
  tasks, are not returned by task inventory, and are not launchable. Draft,
  revise, and confirm turns run off the manager lock after persisting transient
  status; clients observe the settled outcome by polling the intake record.
- Successful draft/revise turns that settle to `awaiting_review` emit one
  metadata-only intake-ready Bark notification when configured; failures,
  cancellations/discards, and confirmation do not notify.
- The dashboard repo-detail view starts intake; the intake review view shows the
  complete proposed task file, index entry, notes, status, and audit events with
  Confirm, Request changes, and Cancel outcomes. In-flight turns show a live
  working panel with elapsed time; confirmed intakes route to the created task
  detail so the existing Start control launches it. A live run withholds intake
  start; a non-terminal intake withholds run Start. Linked admission notes and
  raced-409 repaints do not change readiness, attention, or run-state signals.
- A legacy/recovered overlap refuses revise/confirm before handle use and keeps
  lifecycle-legal Cancel, which writes no target state and releases the intake
  claim.
- The concrete default backend is `ClaudeIntakeAgent`, a headless Claude CLI
  adapter that drives `/intake-task` with structured draft/confirm envelopes,
  session resume, read-only `--permission-mode plan` draft/revise turns, and a
  write-permitted confirm turn. Draft/revise/confirm share the config-resolved
  `ORCH_HUB_INTAKE_MODEL` / `ORCH_HUB_INTAKE_EFFORT` tier, default
  `claude-opus-5` / `max`.
- Confirmation is not trusted on agent exit code alone. The manager records
  `confirmed` only after the created task appears through `list_repo_tasks` with
  frontmatter status `pending` or `blocked`; no-write, partial-write,
  invalid-status, agent-failure, cancellation, and lost-session outcomes record
  `failed`/`cancelled` and do not surface a launchable task. In-process
  failed/unverified confirms roll back the proposed task file and `index.md`.
- Startup reconciliation marks orphaned persisted transient intakes as `failed`
  so they stop holding the repo intake slot; it does not mutate target repos.
