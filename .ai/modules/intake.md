---
last-updated: 2026-07-24
verified-against: d249e7ab7e818633e9ef82d6218de21c229d32e1
---

# Modules: Remote Intake

Intake lifecycle manager and its Claude CLI agent backend. Foundation
modules: `core.md`.

## Intake Manager

Location: `orch_hub/intake.py`

Responsibility: own the remote intake-task lifecycle, hub-owned audit state,
and intake-ready notification trigger timing.
`IntakeManager` stores each intake under `<data_dir>/intake/<intake_id>/` with
`intake.json` and metadata-only `events.jsonl`, gates start on non-empty input,
one in-flight intake per repo, and readiness, then drives an injected
`IntakeAgent` through draft, revise, confirm, and cancel turns. It accepts an
optional notifier and dispatch seam; `None` disables delivery work.

Draft, revise, and confirm turns persist a transient status
(`drafting`/`revising`/`confirming`) under the manager lock, submit the slow
agent turn through an injected executor outside the lock, then re-acquire the
lock to finalize. Submitting outside the non-reentrant lock is load-bearing for
synchronous test executors. Finalizers re-load the record and settle only the
expected transient status; a cancel that landed during draft/revise wins, the
finished turn is discarded, and cancelled intakes are never resurrected.
`confirming` is deliberately non-cancellable because the write may already have
landed.

Draft/revise turns return complete proposals and leave target repos unchanged.
When either turn successfully settles the record to `awaiting_review`, the
manager fires one best-effort metadata-only intake-ready notification after
persistence; cancel/discard, failure, and confirm paths do not emit.
Only `confirm_intake` calls the agent write path. Confirmation is verified via
`list_repo_tasks`: the created id must appear in the target repo inventory with
frontmatter status `pending` or `blocked`. Missing/index-only/no-write,
invalid-status, agent-failure, lost-session, and unverified outcomes become
`failed` rather than `confirmed`; in-process failed/unverified confirms restore
the proposed task file and `.ai-tasks/index.md` snapshot. On manager startup,
`reconcile_orphans()` fails persisted transient records as lost sessions so
they stop holding the repo intake slot; it never mutates the target repo.

## Intake Agent

Location: `orch_hub/intake_agent.py`

Responsibility: provide the concrete Claude CLI backend for `/intake-task`.
`ClaudeIntakeAgent` runs `claude -p --output-format stream-json` in the target
repo, uses a structured fenced JSON envelope for drafts and confirmations,
threads session ids through `--session-id`/`--resume`, and parses assistant text
from stream events. Draft and revise use `--permission-mode plan`; confirm uses
the write-permitted CLI mode. Bad exits, stream errors, missing/invalid required
envelopes, and bad handles fail the agent turn so the manager records an
auditable failed intake.
