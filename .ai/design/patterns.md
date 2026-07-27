---
last-updated: 2026-07-27
verified-against: 2bc59e6a5e02c101b912b35b561835b8a65f9d0d
---

# Design: Patterns

- Prefer small, reusable application functions behind both CLI and API routes.
- Represent repo, readiness, task, run, and escalation data with typed Python models.
- Use `pathlib.Path` for filesystem paths.
- Use subprocess wrappers for blocking `aii-2` and `git` checks; detached
  orchestrator runs use direct `subprocess.Popen` with argument lists so the
  hub can keep a process handle and redirect output.
- Return stable JSON from CLI commands and API endpoints from the same payload
  helpers.
- Keep FastAPI route handlers thin over core functions; do not duplicate
  discovery, readiness, or task-store logic in HTTP handlers.
- Tests should use temporary directories and mocked subprocess calls, never real user repos.
- Configuration should have explicit defaults and environment overrides.
- Empty or whitespace-only config environment values count as unset;
  surrounding whitespace is stripped, and malformed ports raise an explicit
  error instead of silently falling back.
- Treat a control-dir question as pending until its answer file is valid by
  the orchestrator's own rule; malformed answers remain visible so the hub can
  repair them by atomic rewrite.
- Refuse to overwrite existing valid answer files; they may already be
  consumed and are part of the answer audit trail.
- Keep run dirs outside target repos and create a fresh control dir per run;
  stale `stop.flag` files would stop reused runs immediately.
- Treat `run.json` as the source of truth for run state; in-memory process
  handles are only an optimization for observing exit codes.
- Coordinate run/intake start through one per-repo admission section held until
  the winner's record is persisted; ask each manager's lock-free file-backed
  occupancy probe instead of duplicating reservations in memory.
- Treat every readable, repo-attributed persisted unended run record as
  admission-live until the normal refresh path persists its end; process death
  alone must not open a fail-open intake window.
- Store launch-time task frontmatter status on `RunRecord.task_status`; history
  code should not re-parse task files to reconstruct run context.
- Keep run history metadata-only: answer audit records responder and question
  metadata, while exact human answer text remains only in the control-dir
  answer file consumed by the orchestrator.
- Classify finished runs from stable evidence in order: task
  completed/archived, hub stop evidence, then exit code.
- Keep notification trigger policy in the owning manager: `RunManager` fires
  terminal notifications at the live-to-ended transition and pending-question
  notifications from the existing `_status` scan; `IntakeManager` fires
  intake-ready notifications from successful draft/revise finalizers.
- Treat `None` as the only disabled-notification mechanism; do not add a
  parallel null-object flag. A configured notifier receives `Notification`
  objects with title/body/group.
- Never log a Bark URL or transport detail that could contain the device key;
  log only exception type plus non-secret notification title.
- Require explicit acknowledgement for any `blocked` task launch because the
  hub cannot reliably distinguish a resumable blocked session from a foreign
  manual-session `claimed-by` id.
- Locate orchestrator logs from `RunRecord` fields only: `log_dir`, `task_id`,
  and `started_ts`; never from client-supplied paths.
- Treat missing or rotated logs as normal data states, not API errors.
- Stream only complete newline-terminated log lines; keep partial trailing bytes
  pending so split UTF-8 characters are not corrupted across reads.
- Keep dashboard data access behind the API: the SPA renders readiness,
  task-store sentinel states, task content, run state, answers, history, logs,
  and task-evaluation evidence/static/semantic summaries from HTTP payloads
  rather than reading repo files or recomputing checks.
- Keep evaluation listing structural in `EvaluationService`; presentation-only
  enrichment such as semantic evaluator model/schema labels belongs in API/web
  adapters and must degrade without blanking the primary list payload.
- Run evaluation retention only after the top-level generation's artifact set is
  durable, and protect the just-written evaluation id; failed static/semantic
  attempts must not delete older retained history.
- Serve dashboard shell assets through an enumerated allowlist rather than a
  broad static mount; every non-shell data route stays bearer-protected.
- Keep dashboard presentation CSS-only: theme tokens, system fonts, semantic
  state colours, and static atmosphere live in `styles.css`; do not add a build
  step, external font, or decorative network asset for the control surface.
- For raw task content, gate on task-index membership and resolve the task file
  target under the resolved `.ai-tasks` directory so symlinked files cannot
  disclose content outside the task dir.
