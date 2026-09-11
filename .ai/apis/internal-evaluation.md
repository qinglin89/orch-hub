---
last-updated: 2026-09-11
verified-against: 782efbd5cfd56b6852fb2ed61e19eb51433a1549
---

# APIs: Internal — Evaluation Chain

Evidence -> static -> semantic -> publication service contracts.
Core/usage interfaces and API conventions: `internal-core.md`. Run/intake/
control/log/history/notifier interfaces: `internal-runtime.md`. HTTP surface:
`http-evaluation.md`.

## Task Evaluation Service

Input: managed repo, exact task id, and optional evaluation id.

Output: `EvaluationTarget`, `RelatedRun` values, or a bounded `EvidencePack`
persisted as schema-v19 `evidence.json` under the hub data dir.

Behavior: `EvaluationService` is gated by `Config.evaluation_enabled` and raises
`EvaluationDisabledError` from every operation when disabled. Target resolution
checks active task files before archive files. Related-run aggregation loads
persisted run records read-only via `runs.load_records` and matches exact
`(repo_path, task_id)`. Evidence building reads run events, session summaries,
bounded per-session entry task files, persisted run protocol snapshots,
orchestrator logs, `spawn.out`, terminal record fields, and producer-bounded
best-effort git-window commits. The protocol aggregate publishes the atomic
effective source-revision/deploy-lock-payload-digest pair only when every
contributing run durably verified the same identity using its actual-file-backed
end verdict; legacy, malformed, unverified, revision-mixed, and payload-mixed
captures remain unavailable with a reason and are never backfilled from the live
target. `lock_schema_version` is copied beside an available pair only when every
run recorded the same value; missing/mixed versions remain null and never affect
identity comparison or gating. Whole entry files
drive exact/partial/reconstructed fidelity; complete entry frontmatter is the
preferred source for session-time prefetch facts and the pack's `.ai/` prefetch
slice. The reader then uses persisted session ids against the Claude transcript
root derived from `ORCH_HUB_CLAUDE_HOME` and adds one ordered
declared/undeclared `.ai/` trace. Absent, lost, cut, capped, or unobserved
evidence is explicit and raw transcript content is never retained. Per-run/
secondary artifact failures become warnings, while target task IO errors fail
the build. Persistence writes only
`<data_dir>/evaluations/<repo>/<task>/<evaluation-id>/evidence.json` and never
mutates target repos, run records, `.ai/`, or `.ai-tasks/`.
Persisted evaluation creation, listing, artifact reads/writes, and removal are
structural and contained. The evaluations base may resolve through links, but
all repo/task/evaluation hops beneath it are opened no-follow; artifacts and
recursive removal operate relative to the resulting descriptors.
`list_evaluations` reports only real dirs/regular files; `read_artifact` parses
JSON, `read_text_artifact` reads text such as semantic `report.md`, and
`remove_evaluation` is the single best-effort cleanup/retention seam. Linked
interior hops raise storage damage on reads and are retained rather than followed
on removal; corrupt JSON fails loudly. `prune_evaluations`/`auto_prune` enforce
the per-task retention cap, report only successful removals, run only after the
complete artifact set is durable, and protect the just-written id.

## Static Metrics Evaluator

Input: config, an `EvidencePack`, or a managed repo plus exact task id and
optional evaluation id.

Output: `RunMetrics`, `StaticMetricsReport`, and a persisted
`static_metrics.json` beside the pack's `evidence.json`.

Behavior: `StaticEvaluator` is gated by `Config.evaluation_enabled` and raises
`EvaluationDisabledError` from run/report/generate operations when disabled.
`run_metrics(pack)` is pure over bounded pack evidence and computes run counts,
evidence-only outcome partition, failure/diagnostic availability, human-input
count, dev/review/plan-gate session counts, context/turn/duration aggregates,
warning count, and target active/archive source. `build_report(pack)` parses
the task file content from the pack for frontmatter legality, required
sections, session-log structure, review coverage, verdict/remediation counts,
status-transition path, and completion hygiene. Transition legality is
entry-kind-aware: a dev entry may collapse a first claim and completion into
`pending → final_review`, while a review entry may not. Work entries are
partitioned into advancement and remediation; estimate fit counts only
advancement, while remediation remains neutral diagnostic evidence. The report
returns six transparent score components with evidence-backed warnings and
confidence. The sixth,
`prefetch_set_read_completeness`, scores complete applicable/assessable
session-time sets while keeping unmeasured and reader-capped candidates in the
confidence denominator only. Reports stamp static schema v5. A review-off entry
whose `Verdict` head is `none` counts as review coverage without a judgment,
needs no `Group`/`Findings`, permits `final_review → completed`, and is stepped
over when locating the dev claimant's final `Open`; `review_skipped_count`
reports the non-penalized coverage fact. `generate_metrics`
delegates to `EvaluationService.generate_evidence`, writes `static_metrics.json`
atomically into the same evaluation artifact directory, and removes the
reserved dir on report-write failure. It mints evidence with retention deferred
and prunes only after the static artifact is durable. HTTP/dashboard
presentation wraps it outside the module through the evaluation API routes and
Layer 1 dashboard view; the evaluator itself never mutates target repos or run
records.

## Semantic Evaluator

Input: config, an `EvidencePack` plus `StaticMetricsReport`, or a managed repo
plus exact task id.

Output: `SemanticReport`, a deterministic Markdown render, and persisted
`semantic_report.json` plus `report.md` beside the pack/static artifacts; report
provenance includes the input evidence-schema cohort.

Behavior: `SemanticEvaluator` is gated by `Config.evaluation_enabled` and raises
`EvaluationDisabledError` when disabled. `build_semantic_evidence(pack, static)`
constructs a bounded model input from the task file, static metrics, capped git
subjects, the pack's producer-bounded `.ai/` docs/same-window patches and ordered
session-read rollups, and explicit omissions. Snippet/diff/read availability is
computed independently from the persisted sections; unavailable or partial
parts and raw logs are omitted or lower confidence rather than failing.
Semantic schema v2 adds `retrieval_effectiveness` over all declared/undeclared
reads and grounds `rediscovery_risk` in the same timing evidence; unobserved
sessions are missing evidence, never bad behavior. `build_prompt` requires a
sentinel-wrapped JSON report. `parse_report` stamps provenance from the harness,
requires scored rationales and evidence-backed findings, and treats malformed
model output as `SemanticEvaluationError`. `evaluate(repo, task_id)` mints a
fresh evaluation dir, writes evidence/static/semantic/Markdown atomically,
removes the whole reserved dir on backend/model/schema failure, and prunes older
evaluations only after the whole semantic artifact set is durable.

`SemanticEvaluator.doctor()` delegates to the backend's no-cost availability
probe and returns `SemanticDoctor` (`backend`, `model`, `schema_version`,
`available`, `detail`) when enabled; disabled mode raises
`EvaluationDisabledError`. The concrete Claude backend's doctor resolves
`claude` on the augmented run PATH without launching a subprocess or model turn.

`ClaudeSemanticBackend` is the default concrete backend. It runs one stateless
Claude CLI turn with all built-in tools disabled (`--tools ""`), safe mode,
no session persistence, plan-mode writes blocked, and a fresh empty cwd outside
the target repo; this enforces evidence-boundedness structurally rather than
trusting prompt instructions. It accepts an optional usage observer called with
stdout after the turn returns; observer failures are swallowed so usage
bookkeeping cannot change semantic-evaluation outcomes.
The command runs through `subprocess_utils.run_command` with the configured
turn timeout, DEVNULL stdin, augmented environment, and an optional per-attempt
`CommandCanceller`. Timeout/cancel diagnostics do not stringify prompt argv.
`SemanticEvaluator` seals the result atomically against cancel immediately after
backend return; a winning cancel takes the existing all-or-nothing cleanup path,
while a winning seal makes all later persistence/publication non-cancellable.

## Automatic Completion And Semantic Jobs

Input: a completed `RunRecord` for automatic Layer 1, or an explicit managed-
repo/task start for Layer 2.

Output: deterministic Layer 1 artifacts plus one durable current `SemanticJob`
per `(repo, task)` for explicit Layer 2 attempts; `EvaluationQueueSnapshot` for
cross-repo operator readback.

Behavior: `CompletionEvaluationTrigger` runs only when both evaluation gates
are open and only after the run manager has settled terminal evidence. It owns
only `StaticEvaluator.generate_metrics`, submits the whole build through its
executor, and holds no semantic-job manager or dispatcher reference; failures
are isolated from run state. No completion path writes, skips, enqueues, or
starts a semantic job, and no durable Layer 1 debt is recorded.

`SemanticJobManager.start_job` is the only production Layer 2 start path. It
overwrites resting and legacy deferred/skipped records while queued/running
turns retain single-flight behavior, restart orphan reconciliation, bounded
failure diagnostics, and completed `evaluation_id` linkage. Each running record
persists the whole-attempt deadline the worker is held to; a resident sweep
settles only expired transients, while timeout `0` disables both deadline and
live reap. `cancel_job` allows legacy-deferred/queued/running, kills or
cooperatively withholds live work, and refuses after the evaluator seals its
result. Live ownership remains registered through terminal write and survives a
failed write until reconciliation. Fresh manual completed/failed outcomes
notify; cancellation does not. `list_persisted_jobs` exposes the same store walk
without constructing a manager; strict mode distinguishes a genuinely absent
root from storage the caller could not read.

`queue_snapshot` is pure/read-only: uncapped latest-attempt counts plus capped
rows ordered live first, unresolved outcomes newest-first, and inert legacy
deferred records last. It takes no config, exposes no automatic gate or pause,
and cannot admit or promote a job.

## Global Report Catalog

Input: config/data dir, managed repo + exact task/evaluation identity for live
publication, or the existing evaluations tree for reconciliation.

Output: immutable `ReportCatalogEntry` values, forward-cursor `ReportPage`
values, all-or-nothing `ReportBundle` values, per-file `ReportArtifactRead`
values, and unpublished evaluation ids for retention protection.

Behavior: `ReportCatalog` shares the evaluation gate. It publishes only
coherent complete L1+L2 artifacts whose task is archived, assigning a monotonic
global `seq` and stable source-derived `report_key` in an append-only fsynced
JSONL catalog. A cross-process lock serializes allocation; startup
`reconcile()` deterministically/idempotently backfills missed and newly archived
reports without evaluating. `page(after_seq, limit)` is strictly-greater,
ascending, and forward-only; duplicate durable sequences/keys fail closed.
`read_bundle(entry)` performs contained by-key reads, requires the whole artifact
set, and reports strict digest verified/mismatched/unverified state while
preserving pruned entries as explicitly unavailable. Every repo/task/evaluation
hop is walked no-follow and each fixed artifact is opened relative to the final
descriptor; a linked interior hop returns distinct damaged-tree unavailability
rather than pruned/incomplete.
`read_artifact_bytes(entry, name)` returns one fixed-name artifact's persisted
bytes without normalization, reports integrity per file, and keeps readable
mismatches visible. It uses the same descriptor rule, so neither read path's file
hop can be redirected by a symlink or check/reopen swap; path-resolution and IO
faults become structured unreadable results. Publication completeness rejects a
linked directory or artifact, manifest digests use the same descriptor chain,
and the retention guard treats the same damaged candidate as non-debt.
`unpublished_ids()` and publication share one artifact-derived eligibility rule;
uncertain debt is protected from pruning. The catalog publishes
provenance/artifacts only and never decides protocol evolution. Report protocol
provenance comes from the persisted pack aggregate and serializes atomically as
`provenance.protocol.effective_revision` plus `deploy_lock_hash`, with unanimous
optional `lock_schema_version` bookkeeping beside the pair; current target
deployment state is never consulted at catalog or API time, and legacy
revision-only records read back unavailable.
