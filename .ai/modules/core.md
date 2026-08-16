---
last-updated: 2026-08-16
verified-against: cad31851334def4258d4004c2e62516807aaff85
---

# Modules: Core

Foundation modules. Evaluation/usage/statusline: `sidecar.md`. Intake:
`intake.md`. Run control: `runtime.md`. API/web/launchd: `web.md`.

## CLI

Location: `orch_hub/cli.py`

Responsibility: expose user-facing commands. Repo commands list managed repos
and readiness; JSON output also carries best-effort latest commit metadata and
housekeeping marker state:

```bash
python -m orch_hub.cli repos
python -m orch_hub.cli repos --json
```

Launchd commands dispatch to `orch_hub.launchd` for service deployment and
lifecycle operations:

```bash
orch-hub launchd deploy|update|start|stop|restart|status
```

Notification commands dispatch through the configured notifier path:

```bash
orch-hub notify test
```

`notify test` resolves `Config.from_env()`, falls back to the installed launchd
plist's Bark URL when the shell env is unset, sends one probe notification
directly through `build_notifier(config)`, and reports delivery failure without
printing the secret endpoint.

Evaluation commands expose operator smoke checks:

```bash
orch-hub eval doctor
```

`eval doctor` reports the optional evaluation gate, semantic backend/model/
schema, retention setting, and backend availability without a model call; it
exits 0 only when evaluation is enabled and the backend is available.

Dependencies: config, registry, readiness, housekeeping, models, launchd,
notifications, semantic.

## Config

Location: `orch_hub/config.py`

Responsibility: define `ORCH_HUB_AI_NATIVE_DEPLOYMENT` override, default
ai-native-development path, `ORCH_HUB_TOKEN` bearer-token loading,
`ORCH_HUB_HOST`, `ORCH_HUB_PORT`, `ORCH_HUB_DATA_DIR`, `ORCH_HUB_BARK_URL`,
`ORCH_HUB_INTAKE_MODEL`, `ORCH_HUB_INTAKE_EFFORT`,
`ORCH_HUB_EVAL_ENABLED`, `ORCH_HUB_EVAL_MODEL`, `ORCH_HUB_EVAL_EFFORT`,
`ORCH_HUB_EVAL_RETENTION`, `ORCH_HUB_CLAUDE_HOME`, `ORCH_HUB_CODEX_HOME`, and
derived Claude transcript, `aii-2`, and registry file paths:

```text
/Users/linqing/workplace/github.com/qinglin89/ai-native-development
```

All current environment variables strip surrounding whitespace. Empty or
whitespace-only values count as unset; a malformed or out-of-range
`ORCH_HUB_PORT` raises `ValueError` instead of silently binding the default.
`ORCH_HUB_DATA_DIR` defaults to `~/.orch-hub`; the run manager writes
`<data_dir>/runs/<run_id>/` directories there, and evaluation writes
`<data_dir>/evaluations/<repo>/<task>/<evaluation-id>/` artifacts there.
`ORCH_HUB_BARK_URL` is the full Bark endpoint including device key; empty or
whitespace-only values disable push notifications. `ORCH_HUB_INTAKE_MODEL` /
`ORCH_HUB_INTAKE_EFFORT` select one tier shared by draft/revise/confirm,
defaulting to `claude-opus-5` / `max`; their constants live in `config.py`
because `config` cannot import the agent side of the
`intake_agent → intake/runs → config` dependency chain. `ORCH_HUB_EVAL_ENABLED`
defaults true and accepts normal truthy/falsy tokens; malformed explicit values
raise `ValueError`, while falsy values disable evaluation services fail-closed.
`ORCH_HUB_EVAL_MODEL` selects the semantic evaluator's Claude CLI model and
defaults to `claude-opus-5`; `ORCH_HUB_EVAL_EFFORT` selects its reasoning
effort and defaults to `max`. Blank/unset uses either default so the backend is
constructible without launching anything. `ORCH_HUB_EVAL_RETENTION` defaults
to 20 newest evaluations per task; `0` keeps all, and negative/non-integer
values raise `ValueError`. `ORCH_HUB_CODEX_HOME` defaults to `~/.codex` and is
used only for read-only subscription-usage scans of Codex session rollouts.
`ORCH_HUB_CLAUDE_HOME` defaults to `~/.claude`; evaluation derives its
`projects/` transcript root from it for bounded session-read evidence.

## Models

Location: `orch_hub/models.py`

Responsibility: define stable data shapes for managed repos, readiness reports,
commit metadata, housekeeping marker state, task inventory, control-dir
questions, run state, run history, intake state, evaluation target/run links,
static-metrics reports, semantic reports, and semantic evaluator doctor output.
`UsageWindow`, `UsageCredits`, `ProviderUsage`, and `SubscriptionUsage`
normalize Claude/Codex subscription-capacity display data; omitted provider
fields stay `None` rather than zero so unavailable data cannot masquerade as
unused quota.
`CommitInfo` carries `hash`, `short_hash`, `committed_ts`, and `subject`;
`repos_payload` accepts `(repo, report)`, `(repo, report, commit)`, or
`(repo, report, commit, housekeeping_pending)` so CLI JSON and
`GET /api/repos` stay identical and always include `commit` (`null` when
unavailable) plus `housekeeping_pending`.
`TaskSummary` carries `task_id`, task-file `status`, optional index summary,
and blockers; `SessionSummary`/`SessionsSnapshot` describe per-protocol-session
context usage parsed from orchestrator logs, and `SessionEst` is the typed
frontmatter `session-est: current/total` read for run-history rollups.
`RepoTaskInventory` pairs a `ManagedRepo` with `index_present` and ordered
tasks. `Question` carries filename-authoritative `seq`, display/audit fields,
parse error, answered state, stripped valid `answer`, and answer repair error;
`to_dict()` deliberately excludes the local path. `RunRecord` is the persisted
run shape, carries launch-time `task_status`, and can reconstruct its
`ManagedRepo` from persisted `repo_path` for run-scoped repo reads; `RunStatus`
overlays active/waiting/completed/stopped/error/ended state plus pending
question count. `RunEvent` stores metadata-only history events and
`RunHistoryEntry` combines a computed run status with its persisted events.
Public readiness payloads contain `ready` plus failed check reasons only.
`IntakeDraft` carries the full proposed task file plus index entry for human
review. `IntakeRecord` persists hub-owned intake status, draft, created task id,
repo attribution, and audit metadata while omitting internal agent handles from
serialized payloads. `IntakeEvent` and `IntakeHistoryEntry` expose metadata-only
intake history. `EvaluationTarget` and `RelatedRun` are the shared shapes for
evaluation target resolution and run aggregation; `RunRollup`, `RunMetrics`,
`ProtocolMetrics`, `ScoreComponent`, and `StaticMetricsReport` are the
deterministic static-metrics output shapes. `SemanticScore`,
`SemanticFinding`, `SemanticEvaluatorInfo`, and `SemanticReport` carry Layer 2
scores, findings, severity/confidence/evidence refs, evaluator identity, and
suggested improvements; `SemanticReport` also stamps the input evidence-schema
cohort so confidence comparisons do not cross a changed evidence baseline.
Evidence composites stay in
`evaluation.py` because they reference `logs.LogSnapshot`, avoiding a
models/logs import cycle.

## Registry

Location: `orch_hub/registry.py`

Responsibility: prefer `<ai-native-development>/aii-2 registry list --json`; if unavailable or failing, fallback to `<ai-native-development>/.registry/repos.local.json`. Validate and normalize JSON-array entries into `ManagedRepo`.

## Readiness

Location: `orch_hub/readiness.py`

Responsibility: check repo filesystem, deployed manifest, orchestrator script, venv Python, task index, clean git tree, and `aii-2 status <repo>`. Reports keep all seven checks internally and fail closed when paths or tools are missing.

## Git Metadata

Location: `orch_hub/git_meta.py`

Responsibility: read best-effort git commit metadata for managed repos without
participating in launch readiness. `latest_commit(repo)` returns the newest
`CommitInfo` or `None`; `read_commits(repo, limit=...)` returns recent commits;
`read_commits_between(repo, since, until, limit=...)` returns windowed commits
for evaluation evidence. All return `CommitLog(available, commits, truncated)`
newest-first with limits clamped to `1..50`; the fixed 64KB cap is enforced by
the subprocess producer as well as the count limit. A byte-cut capture drops
its trailing line before parsing so no partial subject becomes a `CommitInfo`,
and `truncated` propagates the incomplete prefix into evaluation evidence. The
readers invoke `git log` in the repo, parse `%H`, `%h`, `%cI`, and `%s` using an
ASCII unit-separator field delimiter, and degrade to unavailable on missing
git, non-git dirs, missing paths, or non-zero exit. Commit lookup failure must
not hide repo/task content or fail evidence packs.
`read_path_diff_between` uses the same timestamp window and a caller-required
byte cap for a path-scoped patch stream; rendering disables external diff and
textconv execution, and the result preserves true output size/truncation.

## Housekeeping

Location: `orch_hub/housekeeping.py`

Responsibility: read the repo-local `.ai/.housekeeping-pending` marker for
display-only dashboard/API metadata. `housekeeping_pending(repo)` returns a
boolean and must not participate in readiness or launch gating.

## Subprocess Utilities

Location: `orch_hub/subprocess_utils.py`

Responsibility: central wrapper for running commands with cwd, capture, timeout,
and concise output extraction. Optional capped stdout mode streams and counts the
whole output while retaining only its bounded head; one absolute deadline covers
drain plus wait, and expiry kills/reaps the child before raising `TimeoutExpired`.
Missing executables are surfaced distinctly from ordinary command failures.
Do not hide command failures that readiness or API callers need to report.
The detached orchestrator launch is not routed through this blocking wrapper;
`RunManager` uses `subprocess.Popen` directly so it can keep a process handle
and redirect output.

## Task Inventory

Location: `orch_hub/task_store.py`

Responsibility: parse active task files without importing protocol or orchestrator code. `list_repo_tasks(repo)` reads `.ai-tasks/index.md` for active task ids and summaries, normalizing bare Task cells or full-cell Markdown links like `[id](id.md)` to the safe bare id used for file lookup and API payloads. It also parses five-column `id | title | status | session-est | blockers` indexes, using `title` as the summary there while preserving the legacy positional third-column summary for other table layouts. It then reads each task file frontmatter for authoritative status and blockers. Missing indexes return `index_present=False`; missing task files use a `missing` sentinel status; task files without parseable status use `unknown`. `read_task_content(repo, task_id)` backs the dashboard detail view and stays active-task scoped under `.ai-tasks`; `read_archived_task_content(repo, task_id)` reads `.ai-tasks/archive/<task_id>.md` for run-scoped historical access; `read_session_est(repo, task_id)` reads active-then-archive frontmatter `session-est: current/total` into `SessionEst | None`, with only genuine active-file absence falling through to archive and malformed/missing values degrading to unknown. Raw content readers reject non-bare filenames, resolve the target, and only return content whose resolved path remains under the tight containing directory.
