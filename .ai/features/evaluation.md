---
last-updated: 2026-08-12
verified-against: 4d3c177440ca1e87091d10a510836d86783c7578
---

# Features: Task Evaluation

Evidence foundation, deterministic Layer 1 static metrics, Layer 2 semantic
evaluation, and the cross-repo publication feed. Other core features: `core.md`.

## Task Evaluation Foundation

Status: implemented

Description: Build optional read-only evidence packs for one active or archived
task so static metrics and semantic evaluators have bounded task/run/git/`.ai/`
input without changing normal run-control state.

Modules: evaluation, ai_snapshot, session_reads, task_snapshots, config,
task_store, runs, history, sessions, logs, git_meta, subprocess_utils, models.

APIs: internal `EvaluationService.resolve_target`, `related_runs`,
`build_evidence`, `generate_evidence`, `list_evaluations`, `read_artifact`,
`read_text_artifact`, and `artifact_dir`; protected `GET /api/evaluation` and
`/api/repos/{repo_id}/evaluations/{task_id}` evidence routes; batched
run-history newest-state in `GET /api/history/runs`.

Implemented behavior:

- `ORCH_HUB_EVAL_ENABLED` defaults true for local use; falsy tokens disable the
  service fail-closed, and malformed explicit values raise config errors.
- `ORCH_HUB_EVAL_RETENTION` defaults to keeping the 20 newest evaluations per
  task; `0` disables pruning, and malformed/negative values fail startup.
- Target resolution reads active `.ai-tasks/<task_id>.md` first, then
  `.ai-tasks/archive/<task_id>.md`, by exact id and contained path checks.
- Related-run aggregation includes every persisted run with exact
  `(repo_path, task_id)` identity and excludes other repos/tasks and unreadable
  unattributed run dirs.
- Evidence packs include task content/archive state, related run records,
  metadata-only events, persisted session summaries and session-entry task
  files, bounded orchestrator-log and `spawn.out` excerpts, terminal fields,
  explicit warnings, best-effort git-window metadata, and a producer-bounded
  `.ai/` view. Entry files are capped at 64KB each / 192KB per pack on valid
  UTF-8 boundaries; only whole files count as exact, while cut heads remain
  explicitly truncated and supply declared frontmatter facts only when the
  closing fence survived.
- The `.ai/` prefetch slice is the first-seen union of usable session-entry
  declarations, with the accumulated target task file as the explicit fallback.
  The bounded per-session read section likewise prefers the entry snapshot over
  a Claude transcript task-file read for its session-time expected set, then
  retains one ordered declared/undeclared `.ai/` trace with activity,
  write-order, close-out, cap, and observability evidence. No raw transcript
  content is retained; every part degrades independently with warnings.
- Eager memory topics resolve through `.ai/index.md` routing before file-shape
  fallback (`topic.md` versus `topic/index.md`). Snapshot roots and child reads
  stay inside the managed repo; document/router reads and git patch output are
  bounded at their producers, and a failed patch read preserves collected docs.
- `evidence.json` records its shape as schema v16; pre-version and older-schema
  packs remain readable as additive earlier shapes. Schema 15 accounts for
  capped/unobserved read sessions; schema 16 adds bounded session-entry files,
  their exact/partial/reconstructed fidelity, and prefetch-source provenance.
- Git evidence uses ambient commit-date history reachable from HEAD over the
  run lifecycle window, capped at 50 commits and 64KB at the subprocess
  producer. A byte cut drops its partial trailing record and marks the evidence
  prefix truncated; any related run with no parseable end keeps `until`
  open-ended.
- `generate_evidence` persists `evidence.json` atomically under
  `<data_dir>/evaluations/<repo>/<task>/<evaluation-id>/`, cleaning an empty
  reserved dir on failure. It never mutates target repos, task files, run
  records, `.ai/`, or `.ai-tasks/`. Standalone evidence generation auto-prunes
  after the evidence artifact is durable; metrics/semantic generation defer
  retention until their full artifact set is durable, protecting the new id.
- `list_evaluations` enumerates persisted evaluation dirs newest-first by
  `(timestamp, numeric collision suffix)` and reports only which artifact files
  exist; `read_artifact` returns parsed JSON artifacts and `read_text_artifact`
  returns text artifacts such as `report.md` for a contained evaluation dir.
- HTTP routes expose availability, persisted evaluation listing, evaluation
  detail readback, evidence generation, and the semantic doctor while preserving
  the same disabled fail-closed gate: only `GET /api/evaluation` returns 200
  while disabled; every other evaluation route 404s.
- Run history folds newest persisted evaluation artifacts into one batched
  state per returned task group (`none`, `l1`, `l1_l2`) so dashboard entry
  lights do not need one list request per group; disabled mode returns no
  history states.
- Static and semantic reports are computed by separate downstream layers; no
  scoring or model calls are part of this foundation.

## Task Static Metrics

Status: implemented

Description: Compute deterministic, non-semantic Layer 1 task-quality metrics
from one evidence pack and persist the report beside the pack.

Modules: static_metrics, evaluation, session_reads, config, models.

APIs: internal `StaticEvaluator.run_metrics`, `build_report`,
`generate_metrics`, and `STATIC_METRICS_NAME`; protected
`POST /api/repos/{repo_id}/evaluations/{task_id}/metrics` plus evaluation detail
readback.

Implemented behavior:

- `StaticEvaluator` shares the evaluation subsystem's
  `ORCH_HUB_EVAL_ENABLED` fail-closed gate and raises
  `EvaluationDisabledError` when disabled.
- `run_metrics(pack)` derives run/session/event rollups from pack evidence:
  related run count, evidence-only terminal outcome partition, failure count,
  failed-without-diagnostic count, human-input count, dev/review/plan-gate
  session counts, turn/context aggregates, duration windows, warning count,
  and active/archive target source.
- `build_report(pack)` parses the task file for frontmatter legality,
  required sections, session-log entry counts, review coverage, verdict and
  remediation counts, blocker/fix-set/prefetch facts, status-transition
  continuity, final-status match, and completed-task final `Open: none`. A dev
  work entry may collapse its mechanical first claim and completion into
  `pending → final_review`; the same edge remains illegal for a review entry.
- Static reports carry six transparent score axes: protocol,
  reliability/diagnostics, handoff structure, estimate fit, and
  knowledge-maintenance signals, plus `prefetch_set_read_completeness`. The read
  score is the fraction of assessable applicable sessions whose complete
  session-time lazy set was read; its confidence is measured candidates over all
  candidates, including sessions dropped by the reader cap. Penalties append
  evidence-naming warnings; missing or partial evidence lowers confidence or
  makes an axis not assessable instead of crashing. `static_metrics.json`
  stamps static schema v4 so interpretation cohorts remain separable. Estimate
  fit counts advancement work entries only; work after a `changes-requested`
  review stays remediation until the next review, remains a diagnostic, and is
  not charged to the advancement estimate.
- Two knowledge signals are intentionally omitted because the evidence pack
  cannot derive them: per-commit touched file paths and archived-task blocker
  references across other tasks.
- `generate_metrics(repo, task_id, evaluation_id=None)` delegates to
  `EvaluationService.generate_evidence`, writes `static_metrics.json`
  atomically in the same evaluation artifact directory as `evidence.json`, and
  cleans the reserved directory on report-write failure so no half-pair
  remains; retention runs only after the static artifact is durable.
- `POST /api/repos/{repo_id}/evaluations/{task_id}/metrics` runs a fresh
  deterministic evidence+static refresh and returns the new report; the detail
  route can later read the paired `evidence` and `static_metrics` artifacts.
- The dashboard renders deterministic Layer 1 reports as distinct from Layer 2
  semantic reports and offers raw JSON only through copy/download affordances
  rather than inline log/evidence display.
- Static metrics have no semantic model call; semantic evaluation is the
  downstream Layer 2 route.
- Tests cover run/session rollups, protocol parsing, score axes, partial
  evidence behavior, persistence, disabled mode, and prior protocol
  false-negative regressions.

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

Modules: report_catalog, evaluation, semantic, task_store, models, api, web.

APIs: internal `ReportCatalog.record_evaluation`, `reconcile`, `page`,
`read_bundle`, `read_artifact_bytes`, and `unpublished_ids`; protected
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
  already-existing reports, including reports evaluated before archive.
- Append allocation is guarded across threads/processes with `flock`; each line
  is flushed/fsynced and torn tails are fenced. Duplicate durable sequences or
  keys make all reads/publication fail closed instead of silently skipping a
  report.
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
  strict digest `verified`, `mismatched`, and `unverified` state.
- Raw-artifact lookup selects one of the four fixed names and returns its
  persisted bytes without parsing or normalization, so consumers can verify the
  cataloged size/SHA-256. Availability is per artifact; mismatched bytes remain
  visible with integrity headers. Unknown key/name is 404, incoherent identity
  409, pruned bytes 410, and unreadable/damaged storage 500. The artifact is
  opened no-follow and read through the same descriptor, closing symlink and
  check/reopen races at the file hop.
- Feed items carry source/global identity, generated/cataloged times,
  evaluation-time target source plus publication-time archived state, evaluator
  backend/model/schema, artifact size/hash manifests, and run/git provenance.
  Protocol/config provenance absent from evidence is explicitly unavailable
  with a reason, never inferred.
- `#/reports` preserves global order, uses a visited-cursor trail for exact
  Previous navigation, and links each coherent row to existing per-task report
  detail. Every Run-history state exposes the gated `All evaluation reports →`
  entry, including histories with zero runs or a cursor page pruned away.
