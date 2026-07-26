---
last-updated: 2026-07-26
verified-against: 4d04e3d949437dc103a2a6cb1572d13c1f657d7f
---

# Modules: Sidecar Analysis and Usage

Task-evaluation layers (evidence, static metrics, semantic, jobs) and
display-only subscription usage. Foundation modules: `core.md`.

## Evaluation

Location: `orch_hub/evaluation.py`

Responsibility: optional read-only task-evaluation foundation gated by
`Config.evaluation_enabled`. `EvaluationService.resolve_target(repo, task_id)`
reads active `.ai-tasks/<id>.md` first, then archived
`.ai-tasks/archive/<id>.md`; `related_runs` aggregates exact
`(repo_path, task_id)` run records through `runs.load_records` without refresh,
pid probing, or writes; `build_evidence` assembles a bounded `EvidencePack`
from task content, related run records, metadata-only events, persisted session
snapshots, bounded orchestrator-log and `spawn.out` excerpts, terminal fields,
and best-effort git-window commits; `generate_evidence` mints an evaluation id
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
taskfile/session-log protocol facts, five transparent score axes
(`protocol`, `reliability`, `handoff`, `estimate`, `knowledge`), confidence
values, and evidence-backed warnings. `generate_metrics(repo, task_id,
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
and explicit omissions; raw orchestrator/spawn logs and unavailable `.ai/`
snippets/diffs stay out of the prompt and lower confidence instead of failing.

The report parser requires sentinel-wrapped JSON, stamps task/evaluation
provenance itself, rejects malformed schema output, and requires each finding to
carry at least one non-empty evidence ref. The default `ClaudeSemanticBackend`
runs one `claude -p --output-format stream-json` turn with `--tools ""`,
`--safe-mode`, `--no-session-persistence`, and `--permission-mode plan` from a
fresh empty cwd outside the target repo. Its `doctor()` is a no-cost
`claude`-resolution probe surfaced through HTTP/CLI so an unavailable backend
can disable Layer 2 without affecting Layer 1 or run control. That structural
boundary is load-bearing: the semantic model must judge only the persisted
prompt/evidence and cannot read live repo files, `.ai/`, raw logs, or secrets.
API exposes generation through `SemanticJobManager` at protected `POST`/`GET`
`/api/repos/{repo_id}/evaluations/{task_id}/semantic/job`, detail readback, and
a gated `GET /api/evaluation/doctor` readiness route.

## Usage

Location: `orch_hub/usage.py`

Responsibility: produce provider-normalized, read-only Claude/Codex
subscription usage for display. Codex usage is read from the newest local
`rate_limits` snapshots in `<codex_home>/sessions/**/rollout-*.jsonl`, bounded
by file count and size, with optional primary/secondary windows and aggregate
credits sanitized by whitelist. Claude usage is read from
`<data_dir>/usage/claude.json`; `UsageService` first ingests a newer
`<data_dir>/usage/claude-statusline.json` record from the operator-installed
writer, then reads the same store so the HTTP payload and staleness rules stay
unchanged. The hub-driven `claude -p --output-format stream-json` observer is
still wired, but Claude Code 2.1.218 exposes no subscription windows on that
channel, so unsupported/absent output records no observation. Both
`parse_claude_stream_usage` and `parse_claude_statusline` accept only verified
statusLine-shaped `rate_limits.five_hour/seven_day` windows with
`used_percentage` and optional `resets_at`, reject reset-only quota frames and
ambiguous `utilization`, and never raise into driving/read paths.
`ClaudeUsageStore.record()` writes via per-writer temp files plus `os.replace`
so concurrent observations cannot collide. `UsageService` aggregates Claude then
Codex behind a 30s cache and returns fail-soft
`ProviderUsage.unavailable(...)` snapshots on missing/failed sources. It never
starts a model turn, calls provider endpoints, opens credential files, spends
Codex reset credits, or participates in readiness/attention/run/intake gates.

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

Responsibility: expose console script `orch-hub-statusline` for operator-owned
Claude Code `statusLine.command` installation. It reads the CLI's stdin payload,
persists only the `rate_limits` container plus `subscription_type` to
`<data_dir>/usage/claude-statusline.json`, then runs everything after `--` as
the operator's wrapped statusline argv with the same stdin and inherited
stdout/stderr/exit code. It deliberately avoids `Config.from_env()` validation,
never edits `~/.claude/**`, never starts Claude or calls provider endpoints, and
drops session id, transcript path, cwd, model, workspace, and cost fields before
writing the sink.
The same module owns read-only setup detection/guidance helpers for
`~/.claude/settings.local.json` then `settings.json`; the CLI uses them after
successful `launchd deploy|update` to print fail-soft wrapper setup guidance
without moving settings reads into the launchd mutation path.
