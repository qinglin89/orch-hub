---
last-updated: 2026-07-24
verified-against: 3779a5d5ac7b2dd74f669990a25dc318e24f54f8
---

# APIs: HTTP — Task Evaluation

Evaluation availability, doctor, listing/readback, evidence/metrics
generation, and semantic-job routes. Cross-cutting auth/app behavior:
`http-core.md`.

## `GET /api/evaluation`

- Purpose: report whether the optional task-evaluation subsystem is enabled.
- Shape: `{"enabled": bool}`.
- Auth: protected like every non-health data route.
- Disabled behavior: this is the only evaluation route that returns 200 while
  disabled; every other evaluation route fails closed as 404.

## `GET /api/evaluation/doctor`

- Purpose: report Layer 2 semantic-evaluator readiness without a model call.
- Shape: `{"enabled": true, "semantic": SemanticDoctor}` where `semantic` is
  `{backend, model, schema_version, available, detail}`.
- Disabled behavior: enabled-gated like action routes; disabled returns 404.
- Boundary: used by dashboard/CLI smoke checks only; never gates Start/Stop,
  run history, logs, or control-dir answers.

## `GET /api/repos/{repo_id}/evaluations/{task_id}`

- Purpose: list persisted evaluations for an exact active or archived task id
  plus the task target state.
- Source: resolves `repo_id` through registry discovery, then uses
  `EvaluationService.list_evaluations` and active-then-archive target
  resolution. It is deliberately not gated by the active task index.
- Shape: `{"repo","task_id","enabled":true,"target","latest_evaluation_id","evaluations"}`.
  `target` is `{present: bool|null, source: "active"|"archived"|null}`.
  `evaluations` is newest-first summaries
  `{evaluation_id, has_evidence, has_static_metrics, has_semantic_report,
  evaluator}`. `evaluator` is best-effort `{backend, model, schema_version}`
  read from a present semantic report for history-picker comparison, or `null`
  for non-semantic/degraded entries; corrupt semantic report JSON never 500s the
  list route.
- Errors: unknown repo returns 404; unsafe task id returns 400; disabled returns
  404. A never-evaluated task returns 200 with an empty evaluation list.

## `GET /api/repos/{repo_id}/evaluations/{task_id}/{evaluation_id}`

- Purpose: read one persisted evaluation's artifacts for dashboard rendering
  and local inspection.
- Shape: `{"evaluation_id","evidence","static_metrics","semantic_report","report_md"}`.
  JSON artifacts are parsed objects and `report_md` is raw text; each is `null`
  when that artifact is absent from the evaluation dir.
- Errors: unknown repo or unknown evaluation id returns 404; unsafe task id
  returns 400; disabled returns 404; corrupt artifact JSON surfaces as a 500.

## `POST /api/repos/{repo_id}/evaluations/{task_id}/evidence`

- Purpose: generate a fresh bounded evidence pack for an exact active or
  archived task id.
- Source: delegates to `EvaluationService.generate_evidence`, writing only
  hub-owned `<data_dir>/evaluations/.../evidence.json`.
- Shape: HTTP 201 `{"evaluation_id","evidence"}`.
- Errors: unknown repo returns 404; unsafe task id returns 400; disabled returns
  404.

## `POST /api/repos/{repo_id}/evaluations/{task_id}/metrics`

- Purpose: generate a fresh deterministic Layer 1 evaluation: evidence plus
  `static_metrics.json` in one hub-owned evaluation dir.
- Source: delegates to `StaticEvaluator.generate_metrics`; no semantic model
  call and no target task/run mutation.
- Shape: HTTP 201 `{"evaluation_id","static_metrics"}`. The paired evidence is
  read through the evaluation detail route.
- Errors: unknown repo returns 404; unsafe task id returns 400; disabled returns
  404.

## `POST /api/repos/{repo_id}/evaluations/{task_id}/semantic/job`

- Purpose: start or reuse the current async Layer 2 semantic-evaluation job for
  an exact active or archived task id.
- Source: delegates to `SemanticJobManager.start_job`, which persists
  `semantic-jobs/<repo-slug>/<task-id>/job.json` and runs
  `SemanticEvaluator.evaluate` off-thread. The evaluator writes only hub-owned
  evidence/static/semantic/Markdown artifacts; backend/schema failures settle the
  job to `failed` with a bounded diagnostic and leave no partial evaluation.
- Shape: HTTP 201 `{"job": SemanticJob}`. `queued`/`running` jobs carry
  evaluator identity; `completed` carries `evaluation_id`; `failed` carries
  bounded `error`. A duplicate start while the job is in flight returns the
  existing job and starts no second model call.
- Errors: unknown repo returns 404; unsafe repo/task segments return 400;
  disabled returns 404.

## `GET /api/repos/{repo_id}/evaluations/{task_id}/semantic/job`

- Purpose: read the current semantic-evaluation job state for page load and
  polling.
- Shape: HTTP 200 `{"job": SemanticJob|null}`; `null` means the task has never
  had a semantic job.
- Errors: unknown repo returns 404; unsafe repo/task segments return 400;
  disabled returns 404. Corrupt job records surface as terminal `error` jobs
  rather than hiding broken state.
