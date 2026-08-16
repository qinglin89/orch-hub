---
last-updated: 2026-08-16
verified-against: cad31851334def4258d4004c2e62516807aaff85
---

# Features: Semantic Evaluation and Report Feed

Layer 2 semantic evaluation and the cross-repo publication feed, over the
evidence/static foundation in `evaluation.md`. Other core features: `core.md`.

## Task Semantic Evaluator

Status: implemented

Description: Run optional Layer 2 semantic task-quality evaluation over bounded
evidence plus deterministic static metrics and persist model-judged JSON and
Markdown reports without mutating target repos.

Modules: semantic_jobs, semantic, ai_snapshot, session_reads, static_metrics,
evaluation, config, models, notifications, api, cli, web.

APIs: internal `SemanticEvaluator.evaluate`, `build_semantic_evidence`,
`build_prompt`, `parse_report`, `render_report_md`, `ClaudeSemanticBackend`,
`SemanticJobManager`; protected `POST`/`GET`
`/api/repos/{repo_id}/evaluations/{task_id}/semantic/job`,
`GET /api/evaluation/doctor`, CLI `orch-hub eval doctor`, dashboard Layer 2
controls, and evaluation detail readback of `semantic_report` and `report.md`.

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
  remain omitted or lower confidence rather than failing; unobserved sessions
  are missing evidence, never bad behavior. Raw logs stay out.
- Reports stamp `input_evidence_schema` and `input_entry_fidelity`; confidence
  comparisons must remain within one evidence-schema cohort, and replay cases
  can distinguish exact, partial, and reconstructed session-entry inputs.
- The default Claude CLI backend is evidence-bounded structurally: it runs one
  stateless `claude -p` turn from a fresh empty cwd outside the target repo with
  `--tools ""`, `--safe-mode`, `--no-session-persistence`, and
  `--permission-mode plan`.
- The semantic doctor is a cheap availability/config probe: HTTP returns
  `SemanticDoctor` and CLI exits 0 only when evaluation is enabled and the
  backend resolves. It never performs a model call or gates run control.
- Reruns preserve history by minting a fresh evaluation dir each time; evaluator
  backend/model/schema identity distinguishes comparable reports and is surfaced
  in dashboard history picker labels.
- `SemanticJobManager` persists one current job per `(repo, task)` under
  `<data_dir>/semantic-jobs/`, prevents duplicate in-flight model calls, marks
  restart-orphaned transient jobs failed, and links completed jobs to the fresh
  `evaluation_id`. Fresh completed/failed finalizers emit one metadata-only,
  best-effort push after persistence; transient/error states, duplicate settles,
  and restart orphan reconciliation stay silent.
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
`#/reports`.

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
  the persisted pack's runs share one verified deployed identity. Missing/mixed
  protocol evidence, legacy revision-only records, and absent config evidence
  are explicitly unavailable with a reason, never inferred from current target
  state.
- `#/reports` preserves global order, uses a visited-cursor trail for exact
  Previous navigation, and links each coherent row to existing per-task report
  detail. Every Run-history state exposes the gated `All evaluation reports →`
  entry, including histories with zero runs or a cursor page pruned away.
