---
last-updated: 2026-09-03
verified-against: 4d6e1a3b7c31a073417b9b681d5a009e288b71cf
---

# Features: Task Evaluation

Evidence foundation and deterministic Layer 1 static metrics. Layer 2 semantic
evaluation and the cross-repo publication feed: `evaluation-reporting.md`.
Other core features: `core.md`.

## Task Evaluation Foundation

Status: implemented

Description: Build optional read-only evidence packs for one active or archived
task so static metrics and semantic evaluators have bounded task/run/git/`.ai/`
input without changing normal run-control state.

Modules: evaluation, protocol_provenance, ai_snapshot, session_reads,
task_snapshots, config, task_store, runs, history, sessions, logs, git_meta,
subprocess_utils, models.

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
  each run's durable deployed-protocol snapshot, explicit warnings, best-effort
  git-window metadata, and a producer-bounded `.ai/` view. Entry files are
  capped at 64KB each / 192KB per pack on valid UTF-8 boundaries; only whole
  files count as exact, while cut heads remain explicitly truncated and supply
  declared frontmatter facts only when the closing fence survived.
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
- `evidence.json` records its shape as schema v18; pre-version and older-schema
  packs remain readable as earlier shapes. Schema 16 adds bounded session-entry
  files/fidelity/prefetch-source provenance; schema 17 adds each run's persisted
  `protocol.json`; schema 18 makes the aggregate the atomic source-revision plus
  deploy-lock payload-digest pair and requires the schema-2 per-run actual-file
  verdict. The pair is available only when every contributing run completed a
  stable end check for the same identity; missing, corrupt, unverified,
  revision-mixed, or payload-mixed provenance carries a reason and neither half.
  Earlier stable/revision-only artifacts remain readable but unavailable and are
  never repaired from current target state.
- Git evidence uses ambient commit-date history reachable from HEAD over the
  run lifecycle window, capped at 50 commits and 64KB at the subprocess
  producer. A byte cut drops its partial trailing record and marks the evidence
  prefix truncated; any related run with no parseable end keeps `until`
  open-ended.
- `generate_evidence` reserves its directory and persists `evidence.json`
  atomically through the evaluations tree's descriptor walk, cleaning the
  reserved dir through the same walk on failure. It never mutates target repos,
  task files, run records, `.ai/`, or `.ai-tasks/`. Standalone evidence
  generation auto-prunes after the evidence artifact is durable;
  metrics/semantic generation defer retention until their full artifact set is
  durable, protecting the new id.
- `list_evaluations` enumerates persisted evaluation dirs newest-first by
  `(timestamp, numeric collision suffix)` and reports only regular artifact
  files. The evaluations base may itself be reached through links (relocated
  data dir), but creation, reads, artifact writes, failure cleanup, and retention
  all walk every repo/task/evaluation hop no-follow relative to an already-open
  descriptor; recursive removal likewise unlinks relative to contained parent
  descriptors and reports only evaluations actually removed. Linked interior
  hops are storage damage, distinct from an absent/pruned file, and are retained
  rather than followed by a best-effort delete.
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
- A hub-observed task completion automatically runs the same
  `generate_metrics` path off the run-manager lock. Layer 1 is deliberately per
  completion signal rather than task-once: repeats produce fresher free L1-only
  dirs, while publication still requires a complete L1+L2 artifact set. The
  trigger creates no semantic job or durable retry debt.
- The dashboard renders deterministic Layer 1 reports as distinct from Layer 2
  semantic reports and offers raw JSON only through copy/download affordances
  rather than inline log/evidence display.
- Static metrics have no semantic model call; semantic evaluation is the
  downstream Layer 2 route.
- Tests cover run/session rollups, protocol parsing, score axes, partial
  evidence behavior, persistence, disabled mode, and prior protocol
  false-negative regressions.
