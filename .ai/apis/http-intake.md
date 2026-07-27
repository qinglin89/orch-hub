---
last-updated: 2026-07-27
verified-against: 2bc59e6a5e02c101b912b35b561835b8a65f9d0d
---

# APIs: HTTP — Remote Intake

Remote intake-task lifecycle routes. Cross-cutting auth/app behavior and
intake error mapping: `http-core.md`.

## `POST /api/intakes`

- Purpose: start a remote `/intake-task` drafting session for `{repo_id,
  request}`.
- Source: resolves `repo_id` through registry discovery, then delegates gates
  and draft creation to the app's single `IntakeManager`.
- Shape: returns the serialized transient `IntakeRecord` with HTTP 201
  (`status: drafting`, no draft yet) immediately after the record is persisted.
  Clients poll `GET /api/intakes/{intake_id}` for the settled
  `awaiting_review` draft or `failed` outcome.
- Errors: unknown repo returns 404; empty request returns 400; repo already has
  an in-flight intake or readiness fails returns 409, with readiness `reasons`
  included only for readiness. A persisted unended run is checked before
  readiness and returns HTTP 409 before record/agent creation:
  `{"detail", "conflict": "run_live", "run_id"}`.

## `GET /api/intakes`

- Purpose: list known intake records for dashboard resume and audit views.
- Shape: `{"intakes": [IntakeRecord.to_dict(), ...]}`.

## `GET /api/intakes/{intake_id}`

- Purpose: fetch one intake's current draft/status; this is the polling target
  while a mutating intake turn is transient.
- Shape: `IntakeRecord.to_dict()`, including the complete draft when present.
- Errors: unknown intakes return 404.

## `GET /api/intakes/{intake_id}/history`

- Purpose: fetch one intake record plus metadata-only lifecycle/revision audit
  events.
- Shape: the current `IntakeRecord.to_dict()` fields flattened at top level
  plus `"events": [IntakeEvent...]`.

## `POST /api/intakes/{intake_id}/revise`

- Purpose: send non-empty human feedback back to the same intake session and
  return a revised complete draft for review.
- Source: delegates to `IntakeManager.revise_intake`.
- Shape: serialized transient `IntakeRecord` (`status: revising`) returned
  immediately; poll `GET /api/intakes/{intake_id}` for the settled
  `awaiting_review` draft or `failed` outcome. Wrong lifecycle state returns
  409; empty feedback returns 400; a lost session can fail synchronously as a
  `failed` record. A recovered live-run overlap returns structured `run_live`
  409 before the handle is consumed or state mutates.

## `POST /api/intakes/{intake_id}/confirm`

- Purpose: explicitly approve the current draft and let the intake session
  perform the protocol-defined task/index write.
- Source: delegates to `IntakeManager.confirm_intake`, which records
  `confirmed` only after the created task appears through task inventory with
  frontmatter status `pending` or `blocked`.
- Shape: serialized transient `IntakeRecord` (`status: confirming`) returned
  immediately; poll `GET /api/intakes/{intake_id}` for `confirmed` plus
  `created_task_id` or a `failed` outcome. A failed/unverified confirm is an
  auditable record with status `failed`, not a transport 5xx.
- A recovered live-run overlap returns structured `run_live` 409 before the
  handle is consumed or target state can be written.

## `POST /api/intakes/{intake_id}/cancel`

- Purpose: cancel a draft/review intake without writing target repo task files.
- Shape: serialized `IntakeRecord` with status `cancelled`; legal from
  `drafting`, `awaiting_review`, and `revising`. Wrong lifecycle state,
  including `confirming`, returns 409.
- Cancellation is deliberately not blocked by a recovered live-run overlap: it
  writes no repo state and is the intake-side way to release the claim.
