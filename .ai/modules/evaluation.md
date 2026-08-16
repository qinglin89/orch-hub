---
last-updated: 2026-08-16
verified-against: cad31851334def4258d4004c2e62516807aaff85
---

# Modules: Task Evaluation Layers

Evidence, static metrics, semantic evaluation, and semantic jobs. Publication:
`report-catalog.md`. Usage display: `usage.md`. Foundation modules: `core.md`.

## Evaluation

Location: `orch_hub/evaluation.py`

Responsibility: optional read-only task-evaluation foundation gated by
`Config.evaluation_enabled`. `EvaluationService.resolve_target(repo, task_id)`
reads active `.ai-tasks/<id>.md` first, then archived
`.ai-tasks/archive/<id>.md`; `related_runs` aggregates exact
`(repo_path, task_id)` run records through `runs.load_records` without refresh,
pid probing, or writes; `build_evidence` assembles a bounded `EvidencePack`
from task content, related run records, metadata-only events, persisted session
summaries and entry-task snapshots, bounded orchestrator-log and `spawn.out`
excerpts, terminal fields, best-effort git-window commits, and a bounded `.ai/`
snapshot. Schema-v18 packs keep each entry task file beside its session and each
run's durable schema-2 `protocol.json`; the computed top-level protocol section
publishes one source-revision/deploy-lock-digest pair only when all runs completed
actual-file-backed stable end checks for the same identity. Missing, malformed,
unverified, revision-mixed, and payload-mixed captures remain explicitly
unavailable. Entry files are capped at 64KB per entry and 192KB per pack, UTF-8
boundary safe. Only a whole file counts toward
`exact`/`partial`/`reconstructed` fidelity; a kept head remains marked partial
and is a declaration source only when its frontmatter is complete.

The memory section contains the routed eager set, a prefetch slice from the
first-seen union of usable session-entry declarations (falling back to the
accumulated target task file when none survive), and a `.ai/`-scoped patch
stream over the same git window. The `reads` section prefers each entry
snapshot for its session-time prefetch set, then falls back to the session's
own transcript task-file read. It retains bounded prefetch facts and one ordered
declared/undeclared `.ai/` trace with activity/write-order anchors, but no
transcript text. Transcript absence, lost rows, trace/session caps, unobserved
sessions, and snapshot cuts degrade explicitly; per-part failures remain
independent, and every entry/doc/router/diff/transcript bound is enforced at its
producer.
`generate_evidence` mints an evaluation id and atomically writes
`evidence.json` under
`<data_dir>/evaluations/<repo>/<task>/<evaluation-id>/`. The evaluations base
may be reached through links for relocated data dirs, but directory creation,
listing, artifact reads/writes, failure cleanup, and retention all open every
repo/task/evaluation hop below it no-follow and operate relative to the resulting
descriptor. Recursive removal opens each child no-follow and unlinks it relative
to its contained parent. Listings include only real directories/regular files;
linked interior hops are refused as storage damage rather than followed or
reported as pruned. Retention is per task via `ORCH_HUB_EVAL_RETENTION`;
`prune_evaluations` removes older evaluation dirs inside that `(repo, task)`
subtree, reports only successful removals, and generation paths prune only after
their complete artifact set is durable, protecting the just-written id so failed
static/semantic attempts never shrink retained history.

Per-run artifact reads are isolated into warnings so corrupt/missing run
evidence yields a partial pack; target task file IO errors fail loudly because
the task file is the primary subject. The git window spans earliest parseable
run start to latest parseable run end, but remains open-ended when any related
run has no parseable end. The foundation does not compute scores or invoke
semantic models; HTTP/dashboard presentation lives in `api.py`/`web/` as a
sidecar over these read/generate methods.

## Static Metrics

Location: `orch_hub/static_metrics.py`

Responsibility: compute deterministic, non-semantic task-quality metrics from
one `EvidencePack` and optionally persist `static_metrics.json` beside the
pack's `evidence.json` under the hub-owned evaluation artifact directory.
`StaticEvaluator` shares the evaluation gate and raises
`EvaluationDisabledError` when disabled. `run_metrics(pack)` derives
run/session/event/diagnostic rollups from bounded pack evidence, including
evidence-only run outcome partitioning, failure/diagnostic counts,
human-input count, dev/review/plan-gate session counts, context/duration
aggregates, target source, and warning count. `build_report(pack)` adds
taskfile/session-log protocol facts, six transparent score axes
(`protocol`, `reliability`, `handoff`, `estimate`, `knowledge`,
`prefetch_set_read_completeness`), confidence values, per-session read
diagnosis, and evidence-backed warnings. The read axis scores complete
session-time sets only across applicable assessable sessions and keeps missing
or capped candidates in confidence rather than quality. Static schema v4 makes
transition legality entry-kind-aware (`pending → final_review` is dev-only) and
partitions work entries into advancement versus remediation; estimate fit counts
only advancement while remediation remains neutral diagnostic evidence.
`generate_metrics(repo, task_id,
evaluation_id=None)` delegates to `EvaluationService.generate_evidence`, writes
`static_metrics.json` atomically in the same evaluation dir, and cleans the
reserved dir if the report write fails. `api.py` exposes this as the protected
`POST /api/repos/{repo_id}/evaluations/{task_id}/metrics` deterministic refresh
route; dashboard rendering stays in `web/`. Metrics generation mints evidence
with retention deferred and auto-prunes only after `static_metrics.json` is
durable.

The evaluator is read-only over packs: it never reaches back into target repos
or run dirs during scoring. Knowledge-maintenance checks are limited to facts
the pack actually carries; per-commit touched files and cross-task archived
blocker references are intentionally omitted until the evidence layer grows
those inputs.

## Semantic Evaluator

Location: `orch_hub/semantic.py`

Responsibility: compute optional Layer 2 semantic task-quality reports from one
bounded `EvidencePack` plus its deterministic `StaticMetricsReport`, then
persist `semantic_report.json` and `report.md` beside `evidence.json` and
`static_metrics.json` in a fresh evaluation dir. `SemanticEvaluator` shares the
evaluation gate and raises `EvaluationDisabledError` when disabled. Its evidence
distillation includes the task file, static report, capped git commit subjects,
the pack's bounded `.ai/` docs/windowed patches, its ordered session-read
rollups, and explicit omissions. Independent snapshot/read availability comes
from what the pack actually carries: snippets make `ai_freshness_utility`
assessable, while the read trace grounds semantic-schema-v2
`retrieval_effectiveness` and `rediscovery_risk`. Missing, unobserved, or
partially lost read evidence lowers confidence rather than becoming bad session
behavior. Raw orchestrator/spawn logs stay out of the prompt.

The report parser requires sentinel-wrapped JSON, stamps task/evaluation
provenance itself (including the evidence-pack schema cohort and session-entry
fidelity), rejects malformed schema output, and requires each finding to carry
at least one non-empty evidence ref. The default `ClaudeSemanticBackend`
runs one `claude -p --output-format stream-json` turn with `--tools ""`,
`--safe-mode`, `--no-session-persistence`, and `--permission-mode plan` from a
fresh empty cwd outside the target repo. Its `doctor()` is a no-cost
`claude`-resolution probe surfaced through HTTP/CLI so an unavailable backend
can disable Layer 2 without affecting Layer 1 or run control. That structural
boundary is load-bearing: the semantic model must judge only the persisted
prompt/evidence and cannot read live repo files, raw logs, or secrets. It sees
`.ai/` only as the bounded persisted copy in the prompt; a live reread would
break artifact reproducibility.
API exposes generation through `SemanticJobManager` at protected `POST`/`GET`
`/api/repos/{repo_id}/evaluations/{task_id}/semantic/job`, detail readback, and
a gated `GET /api/evaluation/doctor` readiness route.

## Semantic Jobs

`SemanticJobManager` in `orch_hub/semantic_jobs.py` persists one current
`job.json` per `(repo, task)` under
`<data_dir>/semantic-jobs/<repo-slug>/<task-id>/`, returns existing
`queued`/`running` jobs to prevent duplicate model calls, runs
`SemanticEvaluator.evaluate` off-lock/off-thread, reconciles orphaned transient
jobs to failed on startup, and links completed jobs to the fresh
`evaluation_id`. A guarded fresh completion/failure finalizer emits one
metadata-only notification through isolated manager-owned dispatch; startup
orphan reconciliation stays silent. The old synchronous `POST /semantic` route
is retired.
