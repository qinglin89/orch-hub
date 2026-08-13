---
last-updated: 2026-08-14
verified-against: 81883a7c862b027c1d70fbdd0a22aeaa427a3a3c
---

# APIs: HTTP — Task Evaluation

Evaluation availability, doctor, global report feed/export, per-task
listing/readback, evidence/metrics generation, and semantic-job routes.
Cross-cutting auth/app behavior: `http-core.md`.

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

## `GET /api/evaluation/reports`

- Purpose: incrementally list complete L1+L2 reports for archived tasks across
  every managed repo, oldest-published first, without triggering evaluation or
  reconciliation.
- Query: optional `after=<seq>` non-negative integer watermark; optional
  `limit` 1..500 (default 50). Malformed/negative cursors and invalid limits are
  400. Any accepted cursor means strictly greater; past-end and sequence-gap
  cursors resolve without silently resetting or skipping.
- Shape: `{"enabled":true,"reports":[ReportCatalogEntry],"cursor",
  "next_cursor","has_more"}`. Entries include global/source identity,
  timestamps, archive/evaluator state, artifact sizes/hashes, and explicit
  run/git/protocol/config provenance availability. Protocol provenance includes
  `effective_revision` only when every contributing run's persisted launch/end
  snapshot verified the same deployed commit; otherwise `available:false` and
  `detail` explain the missing, unverified, or mixed evidence.
- Integrity failure: duplicate durable `seq` or `report_key` makes cursor
  traversal ambiguous and returns 500 naming the defect.
- Auth/disabled behavior: protected; disabled returns 404.

## `GET /api/evaluation/reports/{report_key}/artifacts/{name}`

- Purpose: return one published artifact exactly as persisted so a consumer can
  independently verify the feed manifest's `size` and `sha256`; no parsing,
  re-serialization, evaluation, or retention runs on this path.
- Names: `evidence.json`, `static_metrics.json`, `semantic_report.json`, or
  `report.md` only. The selector is never treated as a caller-controlled path.
- Response: the byte-exact body with its JSON/Markdown media type,
  `X-Report-Artifact-Integrity: verified|mismatched|unverified`, and
  `X-Report-Artifact-Sha256` when one usable catalog digest exists. Mismatched
  readable bytes are still served.
- Availability: per artifact, unlike the bundle's all-or-nothing import rule;
  an intact file remains readable beside a pruned sibling. Unknown key/name is
  404, incoherent stored identity 409, pruned artifact 410, unreadable or
  symlinked artifact 500, and ambiguous catalog 500.
- Containment: identity/path validation precedes IO; the fixed artifact name is
  opened no-follow, validated, and read through one descriptor so a symlink or
  check/reopen swap at that file hop cannot redirect returned bytes.
- Auth/disabled behavior: protected; disabled returns 404.

## `GET /api/evaluation/reports/{report_key}`

- Purpose: fetch one published report's immutable import bundle from its opaque
  global key without constructing repo/task/evaluation paths.
- Shape: `{"report_key","entry","available","detail","artifacts",
  "integrity"}`. An available bundle contains parsed evidence/static/semantic
  JSON plus raw `report_md`; `integrity` is
  `{verified,mismatched,unverified}`.
- Availability: unknown key is 404. A published report whose complete artifact
  set was pruned, partially removed, damaged by a linked artifact, unreadable,
  unparseable, unsafe, or identity-incoherent returns 200 with
  `available:false` and an explicit reason. The damaged detail is distinct from
  retention-pruned/incomplete states. Digest mismatch/unavailability is reported
  while content remains available for the consumer's decision.
- Containment: all four fixed artifact names are opened no-follow, validated,
  and read through their descriptors; a symlink or check/reopen swap at the file
  hop cannot redirect parsed JSON or Markdown bytes. Links above the evaluation
  directory (including a symlinked data dir) remain supported.
- Auth/disabled behavior: protected; disabled returns 404.

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
