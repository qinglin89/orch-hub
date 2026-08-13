---
last-updated: 2026-08-14
verified-against: 81883a7c862b027c1d70fbdd0a22aeaa427a3a3c
---

# Modules: Sidecar Analysis and Usage

Task-evaluation layers (evidence, static metrics, semantic, jobs) and
the global report catalog, plus display-only subscription usage. Foundation
modules: `core.md`.

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
snapshot. Schema-v17 packs keep each entry task file beside its session and each
run's durable `protocol.json`; the computed top-level protocol section publishes
one revision only when all runs completed stable end checks for the same commit.
Missing, malformed, unverified, and mixed captures remain explicitly
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
`generate_evidence` mints an evaluation id
and atomically writes `evidence.json` under
`<data_dir>/evaluations/<repo>/<task>/<evaluation-id>/`. `list_evaluations`
enumerates persisted dirs newest-first by `(timestamp, numeric suffix)` and
reports present artifact filenames; `read_artifact` parses contained artifact
JSON and `read_text_artifact` reads contained text artifacts such as semantic
`report.md` for the API/dashboard read layer. Retention is per task via
`ORCH_HUB_EVAL_RETENTION`; `prune_evaluations` removes older evaluation dirs
inside that `(repo, task)` subtree, and generation paths prune only after their
complete artifact set is durable, protecting the just-written id so failed
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

## Global Report Catalog

Location: `orch_hub/report_catalog.py`

Responsibility: publish archived tasks' coherent, complete L1+L2 artifact sets
through one hub-owned append-only catalog at
`<data_dir>/report-catalog/catalog.jsonl`. `ReportCatalog` assigns an immutable
monotonic `seq` (global order/cursor) and a stable opaque `report_key` derived
from the source evaluation identity. Its semantic-finalization hook is
best-effort promptness; the deterministic startup `reconcile()` sweep is the
correctness path and makes backfill/restart idempotent. Reports evaluated while
active remain candidates and publish after a contained archive probe succeeds.

The read/check/allocate/append transaction uses a process-wide `flock`; appends
are fsynced single lines with torn-tail fencing. `page()` provides forward-only
strictly-greater integer-watermark traversal; `read_bundle()` resolves a global
key to the complete contained artifact set and reports digest verification. Both
bundle and raw-artifact reads open each fixed artifact name no-follow, validate
the descriptor, and consume that descriptor, so a symlink or check/reopen swap
at the file hop cannot redirect returned bytes. A linked bundle artifact is
damaged (distinct from retention-pruned/incomplete) and makes the all-or-nothing
bundle unavailable. The same file-hop rule gates publication completeness and
manifest hashing, so a link is never published as an artifact or hashed through.
Catalog/source identity ambiguity fails closed. Published entries outlive
retention-pruned artifacts, which remain auditable but bundle-unavailable.

Protocol provenance is copied only from the persisted evidence pack's
fail-closed aggregate into `provenance.protocol.effective_revision`; the catalog
never reads a target's current deployment lock or invents a revision for legacy,
unverified, or mixed runs.

`unpublished_ids()` is injected as `EvaluationService.retention_guard` so a
prune cannot destroy recoverable publication debt. The guard and publisher read
the same artifact-derived candidate and publishability predicate; uncertainty
defers publication but protects retention. The catalog owns publication and
provenance only—it never evaluates, ranks, decides protocol evolution, or
creates protocol-improvement tasks.

## Usage

Location: `orch_hub/usage.py`

Responsibility: produce provider-normalized, read-only Claude/Codex
subscription usage for display. Codex usage is read from the newest local
`rate_limits` snapshots in `<codex_home>/sessions/**/rollout-*.jsonl`, bounded
by file count and size, with optional primary/secondary windows and aggregate
credits sanitized by whitelist. Claude's primary carrier is the CLI's
`~/.claude.json.cachedUsageUtilization`: `parse_claude_usage_cache` retains only
`fetchedAtMs`, the two window reset instants, and `limits[].percent` figures
paired by kind and equal normalized reset; ambiguous window-map
`utilization`, account ids, credentials, projects, and machine fields are
ignored. `ClaudeUsageCollector` reads that cache and refreshes it through
`claude -p "/usage"` from an empty cwd. Its non-blocking lock, five-minute
freshness/attempt floors, 60s timeout, and mandatory zero-turn/zero-cost JSON
envelope make refresh bounded, single-flight, and model-free; failures return
the old observation with its original timestamp.

`UsageService` ingests this cache first and the optional
`<data_dir>/usage/claude-statusline.json` fallback second, merging both into
`<data_dir>/usage/claude.json` per window. `merge_observations` keys each figure
by store-internal `UsageWindow.figure_instant`; known reset instants within five
minutes are one jitter-tolerant identity whose stored instant converges forward,
while farther-apart instants retain genuine-rollover ordering. Same-window
figures take `max(used_percent)`, legacy stores self-heal, and partial merges
floor provider freshness to the older source. Stale/empty reads dispatch a
background collector refresh for the next read; the app also gives `RunManager`
this request callable for each observed terminal transition. No verified
stream-json version carries a percentage;
2.1.220's percentage-less event may advance only a stored reset boundary.
`ClaudeUsageStore.record()` writes via per-writer temp files plus `os.replace`
so concurrent observations cannot collide; `UsageService` serializes Claude
store read-modify-write paths. It aggregates Claude then Codex behind a 30s
cache and returns fail-soft
`ProviderUsage.unavailable(...)` snapshots on missing/failed sources. It never
starts a model turn, directly calls provider endpoints, opens credential files,
spends Codex reset credits, or participates in operational gates.

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

## StatusLine Writer

Location: `orch_hub/statusline.py`

Responsibility: retain legacy console script `orch-hub-statusline` for an
operator-owned Claude Code `statusLine.command` installation. It reads stdin,
persists only the `rate_limits` container plus `subscription_type` to
`<data_dir>/usage/claude-statusline.json`, then runs everything after `--` as
the operator's wrapped statusline argv with the same stdin and inherited
stdout/stderr/exit code. It deliberately avoids `Config.from_env()` validation,
never edits `~/.claude/**`, never starts Claude or calls provider endpoints, and
drops session id, transcript path, cwd, model, workspace, and cost fields before
writing the sink. It owns no setup detection or guidance; deploy/update do not
inspect Claude settings, and the wrapper is optional because the cache carrier
is primary.
