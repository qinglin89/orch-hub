---
last-updated: 2026-07-26
verified-against: 4d04e3d949437dc103a2a6cb1572d13c1f657d7f
---

# Features: Task Evaluation

Evidence foundation, deterministic Layer 1 static metrics, and Layer 2
semantic evaluation. Other core features: `core.md`.

## Task Evaluation Foundation

Status: implemented

Description: Build optional read-only evidence packs for one active or archived
task so static metrics and semantic evaluators have bounded task/run/git input
without changing normal run-control state.

Modules: evaluation, config, task_store, runs, history, sessions, logs,
git_meta, models.

APIs: internal `EvaluationService.resolve_target`, `related_runs`,
`build_evidence`, `generate_evidence`, `list_evaluations`, `read_artifact`,
`read_text_artifact`, and `artifact_dir`; protected `GET /api/evaluation` and
`/api/repos/{repo_id}/evaluations/{task_id}` evidence routes.

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
  metadata-only events, persisted session snapshots, bounded orchestrator-log
  and `spawn.out` excerpts with size/truncation metadata, terminal record fields,
  explicit warnings, and best-effort git commit-window metadata.
- Git evidence uses ambient commit-date history reachable from HEAD over the
  run lifecycle window, capped at 50 commits; any related run with no parseable
  end keeps `until` open-ended.
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
- Static and semantic reports are computed by separate downstream layers; no
  scoring or model calls are part of this foundation.

## Task Static Metrics

Status: implemented

Description: Compute deterministic, non-semantic Layer 1 task-quality metrics
from one evidence pack and persist the report beside the pack.

Modules: static_metrics, evaluation, config, models.

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
  continuity, final-status match, and completed-task final `Open: none`.
- Static reports carry five transparent score axes: protocol,
  reliability/diagnostics, handoff structure, estimate fit, and
  knowledge-maintenance signals. Penalties append evidence-naming warnings;
  missing or partial evidence lowers confidence or makes an axis not
  assessable instead of crashing.
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

Modules: semantic_jobs, semantic, static_metrics, evaluation, config, models,
notifications, api, cli, web.

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
  evaluator identity, linked input ids, summary, and suggested improvements.
- Bounded prompt evidence includes the task file, static metrics, capped commit
  subjects, warnings, and explicit omissions. Raw logs plus unavailable `.ai/`
  snippets/diffs are excluded and lower confidence rather than failing.
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
