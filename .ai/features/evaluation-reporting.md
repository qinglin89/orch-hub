---
last-updated: 2026-09-08
verified-against: 3a8b4afe4e6e2c12071429594a974fd215039f6a
---

# Features: Semantic Evaluation and Report Feed

Layer 2 semantic evaluation and the cross-repo publication feed, over the
evidence/static foundation in `evaluation.md`. Other core features: `core.md`.

## Task Semantic Evaluator

Status: implemented

Description: Run optional Layer 2 semantic task-quality evaluation over bounded
evidence plus deterministic static metrics and persist model-judged JSON and
Markdown reports without mutating target repos.

Modules: completion_eval, eval_dispatch, semantic_jobs, semantic, ai_snapshot,
session_reads, static_metrics, evaluation, usage, runs, monitor, config, models,
notifications, api, cli, web.

APIs: internal `SemanticEvaluator.evaluate`, `build_semantic_evidence`,
`build_prompt`, `parse_report`, `render_report_md`, `ClaudeSemanticBackend`,
`CompletionEvaluationTrigger`, `SemanticJobManager`, `queue_snapshot`;
protected `POST`/`GET`
`/api/repos/{repo_id}/evaluations/{task_id}/semantic/job`,
`POST .../semantic/job/cancel`, `GET /api/evaluation/doctor`,
`GET /api/evaluation/queue`, CLI `orch-hub eval doctor|queue`,
dashboard Layer 2 controls, and evaluation detail readback of
`semantic_report` and `report.md`.

Implemented behavior:

- `SemanticEvaluator` shares `ORCH_HUB_EVAL_ENABLED`, builds a fresh evidence
  pack, computes static metrics, invokes an injected backend, and persists
  `evidence.json`, `static_metrics.json`, `semantic_report.json`, and
  `report.md` as one all-or-nothing evaluation dir. Retention runs only after
  the complete semantic artifact set is durable.
- The report schema has no naked overall score: it carries per-dimension scores
  with rationales/confidence, findings with severity/confidence/evidence refs,
  evaluator identity, linked input ids, input evidence-schema cohort, summary,
  and suggested improvements.
- Bounded prompt evidence includes the task file, static metrics, capped commit
  subjects, the pack's bounded `.ai/` docs/patch stream and ordered session-read
  rollups, warnings, and explicit omissions. Snippet/diff/read availability is
  computed independently from what the pack carries: snippets ground
  `ai_freshness_utility`; the ordered trace grounds semantic-schema-v2
  `retrieval_effectiveness` and `rediscovery_risk`. Missing or partial parts
  remain omitted or lower confidence rather than failing. A positive task-file
  work-entry minus dev-session-evidence gap is likewise an explicit coverage
  omission with a flat confidence cost; it states only this record's reach and
  leaves outside-hub work versus absent/incomplete session summaries as possible
  causes. Unobserved sessions are missing evidence, never bad behavior. Raw logs
  stay out.
- Reports stamp `input_evidence_schema` and `input_entry_fidelity`; confidence
  comparisons must remain within one evidence-schema cohort, and replay cases
  can distinguish exact, partial, and reconstructed session-entry inputs.
- The default Claude CLI backend is evidence-bounded structurally: it runs one
  stateless `claude -p` turn from a fresh empty cwd outside the target repo with
  `--tools ""`, `--safe-mode`, `--no-session-persistence`, and
  `--permission-mode plan`. `ORCH_HUB_EVAL_TURN_TIMEOUT` defaults to 1800s and
  bounds that subprocess through the shared runner; `0` disables both its
  deadline and live deadline reaping. Timeout diagnostics never stringify the
  evidence-bearing argv.
- The semantic doctor is a cheap availability/config probe: HTTP returns
  `SemanticDoctor` and CLI exits 0 only when evaluation is enabled and the
  backend resolves. It never performs a model call or gates run control.
- Reruns preserve history by minting a fresh evaluation dir each time; evaluator
  backend/model/schema identity distinguishes comparable reports and is surfaced
  in dashboard history picker labels.
- `SemanticJobManager` persists one current job per `(repo, task)` under
  `<data_dir>/semantic-jobs/`. Explicit starts reuse transient turns and
  overwrite resting records. Lifecycle is `queued` → `running` →
  `completed|failed|cancelled`, plus `error` for an unreadable record. A running
  record persists the whole-attempt deadline its worker is enforced against; the
  resident sweep settles only expired transients, while startup reconciliation
  still fails process-orphaned queued/running turns. Legacy `deferred`/`skipped`
  records remain readable and inert until an explicit start supersedes them.
- Per-attempt cancellation is legal only from deferred/queued/running. A live
  canceller kills the direct model child or withholds a not-yet-opened turn; an
  atomic seal at model return makes later cancel refuse while parsing,
  persistence, publication, or terminal settlement finishes. Collision-resistant
  attempt ids and job-id-keyed live ownership keep a late worker from settling or
  unregistering a retry. A failed terminal record write retains sealed ownership
  until deadline/startup reconciliation records the failure.
- Every hub-observed run completion triggers deterministic Layer 1 after
  session, task, and deployed-protocol evidence settles. The whole build runs
  off the run-manager lock; failures are logged/dropped and cannot change run
  state, readiness, attention, or launch/intake admission. The trigger holds no
  semantic-job or dispatch collaborator and records no durable retry debt.
- Layer 2 has no automatic path: completion, startup, monitor ticks, usage
  changes, and network reconnection cannot create, skip, enqueue, promote, or
  start a turn. `ORCH_HUB_EVAL_AUTO` gates automatic Layer 1 only. Layer 2 begins
  only through `SemanticJobManager.start_job` from the per-task action/API.
- `GET /api/evaluation/queue` is a read-only cross-repo latest-attempt census
  plus actionable-first capped rows: running, queued, unresolved outcomes
  newest-first, then legacy deferred records. Completed/skipped/cancelled stay
  out of the row cap; unknown statuses remain counted. The Overview offers
  per-job cancel and no queue-level control.
- `orch-hub eval queue [--json]` takes the same projection over a manager-free
  persisted-store read, labels the census as attempts rather than evaluated
  corpus, labels legacy holds as never auto-starting, and changes nothing. Only a genuinely absent
  jobs root reads empty; non-directory, unstatable, or untraversable storage is a
  concise operational failure rather than a zero census.
- Fresh explicit completed/failed finalizers notify. Cancellation,
  transient/error states, duplicate settles, and startup orphan reconciliation
  stay silent.
- Backend/model failures or non-conforming output remove the reserved evaluation
  dir and surface as persisted failed jobs with bounded diagnostics; disabled
  mode still fails closed at service/API boundaries.

## Global Evaluation Report Feed

Status: implemented

Description: Publish complete L1+L2 reports for archived tasks through one
durable, globally ordered feed without walking every repo/task evaluation tree.
The hub publishes cohortable artifacts; it does not rank reports, decide
protocol evolution, or create protocol-improvement tasks.

Modules: report_catalog, evaluation, semantic, task_store, models, api, cli, web.

APIs: internal `ReportCatalog.record_evaluation`, `reconcile`, `page`,
`read_bundle`, `read_artifact_bytes`, `unpublished_ids`, `audit`, and `repair`;
CLI `orch-hub eval catalog verify|repair`; protected
`GET /api/evaluation/reports[?after=<seq>&limit=<n>]` and
`GET /api/evaluation/reports/{report_key}` plus
`GET /api/evaluation/reports/{report_key}/artifacts/{name}`; dashboard
`#/reports`; protected `GET /api/protocol-coverage`; dashboard `#/protocol`.

Implemented behavior:

- `<data_dir>/report-catalog/catalog.jsonl` is append-only. Each immutable entry
  has a monotonic global `seq` for order/cursors and a stable opaque
  `report_key` derived from `(repo_slug, task_id, evaluation_id)` for retrieval;
  scoped evaluation timestamps never define global order.
- Publication requires the coherent all-or-nothing artifact set
  (`evidence.json`, `static_metrics.json`, `semantic_report.json`, `report.md`)
  and an archived task. A semantic-finalization hook publishes after durability;
  startup reconciliation deterministically/idempotently backfills missed and
  already-existing reports, including reports evaluated before archive. Every
  candidate is re-opened through the shared descriptor walk; an interior linked
  directory or artifact is neither publishable nor retention debt, and manifest
  hashing opens each fixed name relative to that descriptor so a check-to-hash
  swap cannot record outside bytes.
- Append allocation is guarded across threads/processes with `flock`; each line
  is flushed/fsynced and torn tails are fenced. Duplicate durable sequences or
  keys make all reads/publication fail closed instead of silently skipping a
  report.
- `audit()` shares the feed's per-line scanner and reports every located parse,
  duplicate-identity, stored-identity, and artifact defect. Its artifact census
  walks each full source/key identity once through the contained descriptor,
  distinguishes absent/pruned dirs from standing incomplete or linked/damaged
  trees, and opens every required artifact so `retained` means fetchable at the
  open boundary. `verify [--json]` is read-only/model-free and exits non-zero
  exactly when catalog ambiguity or unreadability makes the feed fail closed.
- `repair()` is dry-run by default and writes only with `--confirm`. It may drop
  byte-identical repeated lines and an unparseable torn tail; distinct `seq` or
  `report_key` conflicts refuse without a partial rewrite. A confirmed repair
  holds the append lock for plan/write, preserves every published identity,
  commits a non-overwriting timestamped backup first, and replaces the catalog
  by staged fsync + rename; operational failures are concise CLI errors.
- Retention asks the catalog for unpublished debt before pruning. Publication
  and retention share one artifact-derived publishability predicate; uncertain
  candidates are retried by publication and protected from deletion. Published
  entries outlive pruned artifacts and remain auditable.
- Feed paging is ascending and forward-only: `after` is a non-negative integer
  watermark, pages return entries strictly after it plus `next_cursor` and
  `has_more`, and `limit` is 1..500 (default 50). Invalid cursors/limits fail
  with 400; stale or gap cursors retain strictly-greater semantics.
- Bundle lookup by global key is contained and all-or-nothing. Unknown keys are
  404; published-but-pruned, partial, unreadable, unparseable, or incoherently
  identified bundles are 200 with `available:false`. Served content reports
  strict digest `verified`, `mismatched`, and `unverified` state. Each fixed
  artifact name is opened no-follow and parsed from that descriptor; the
  repo/task/evaluation hops are likewise walked no-follow, so a link or
  check/reopen swap at any in-tree hop cannot export outside bytes. Linked hops
  return damaged-tree detail distinct from pruned/incomplete states; links used
  to reach the evaluations base remain supported.
- Raw-artifact lookup selects one of the four fixed names and returns its
  persisted bytes without parsing or normalization, so consumers can verify the
  cataloged size/SHA-256. Availability is per artifact; mismatched bytes remain
  visible with integrity headers. Unknown key/name is 404, incoherent identity
  409, pruned bytes 410, and unreadable/damaged storage 500. The artifact is
  opened no-follow and read through the same contained directory descriptor,
  closing symlink and check/reopen races at every in-tree hop.
- Feed items carry source/global identity, generated/cataloged times,
  evaluation-time target source plus publication-time archived state, evaluator
  backend/model/schema, artifact size/hash manifests, run/git provenance, and
  atomic `provenance.protocol.effective_revision` plus `deploy_lock_hash` when
  the persisted pack's runs share one verified deployed identity. The lock's own
  `lock_schema_version` rides beside that pair only when every run recorded the
  same value; it never participates in identity comparison or gating and does
  not identify deployment-mapping generations. Missing/mixed protocol evidence,
  legacy revision-only records, and absent config evidence are explicitly
  unavailable with a reason, never inferred from current target state.
- Feed cards render the persisted protocol provenance block beside run/git
  provenance; unavailable identity always includes its cataloged reason.
- `ReportCatalog.protocol_coverage()` counts distinct published
  `(repo_slug, task_id)` work units, each decided by its newest published
  evaluation using `evaluation_order_key` (publication `seq` tie-break), while
  separately retaining complete report-version counts. Uncovered rows are one per
  task, linked to the deciding report, newest publication first and capped at 50;
  counts remain complete. Disabled or ambiguous catalogs return an unavailable
  section rather than a false zero.
- `GET /api/protocol-coverage` composes that task census with the independent
  persisted-run census outside the evaluation gate; it performs no evaluation,
  reconciliation, protocol settlement, or mutation.
- `#/reports` preserves global order, uses a visited-cursor trail for exact
  Previous navigation, and links each coherent row to existing per-task report
  detail. Every Run-history state exposes the gated `All evaluation reports →`
  entry, including histories with zero runs or a cursor page pruned away.
