---
last-updated: 2026-07-13
verified-against: 593b593183f551c0e4a4c068c9de6ec9b84d937c
---

# Modules: Core

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

Dependencies: config, registry, readiness, housekeeping, models, launchd,
notifications.

## Config

Location: `orch_hub/config.py`

Responsibility: define `ORCH_HUB_AI_NATIVE_DEPLOYMENT` override, default
ai-native-development path, `ORCH_HUB_TOKEN` bearer-token loading,
`ORCH_HUB_HOST`, `ORCH_HUB_PORT`, `ORCH_HUB_DATA_DIR`, `ORCH_HUB_BARK_URL`,
and derived
`aii-2`/registry file paths:

```text
/Users/linqing/workplace/github.com/qinglin89/ai-native-development
```

All current environment variables strip surrounding whitespace. Empty or
whitespace-only values count as unset; a malformed or out-of-range
`ORCH_HUB_PORT` raises `ValueError` instead of silently binding the default.
`ORCH_HUB_DATA_DIR` defaults to `~/.orch-hub`; the run manager writes
`<data_dir>/runs/<run_id>/` directories there. `ORCH_HUB_BARK_URL` is the full
Bark endpoint including device key; empty or whitespace-only values disable
push notifications.

## Models

Location: `orch_hub/models.py`

Responsibility: define stable data shapes for managed repos, readiness reports,
commit metadata, housekeeping marker state, task inventory, control-dir
questions, run state, run history, and intake state.
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
intake history.

## Registry

Location: `orch_hub/registry.py`

Responsibility: prefer `<ai-native-development>/aii-2 registry list --json`; if unavailable or failing, fallback to `<ai-native-development>/.registry/repos.local.json`. Validate and normalize JSON-array entries into `ManagedRepo`.

## Readiness

Location: `orch_hub/readiness.py`

Responsibility: check repo filesystem, deployed manifest, orchestrator script, venv Python, task index, clean git tree, and `aii-2 status <repo>`. Reports keep all seven checks internally and fail closed when paths or tools are missing.

## Git Metadata

Location: `orch_hub/git_meta.py`

Responsibility: read display-only git commit metadata for managed repos without
participating in launch readiness. `latest_commit(repo)` returns the newest
`CommitInfo` or `None`; `read_commits(repo, limit=...)` returns
`CommitLog(available, commits)` newest-first with the limit clamped to `1..50`.
It invokes `git log` in the repo, parses `%H`, `%h`, `%cI`, and `%s` using an
ASCII unit-separator field delimiter, and degrades to unavailable on missing
git, non-git dirs, missing paths, or non-zero exit. Commit lookup failure must
not hide repo/task content.

## Housekeeping

Location: `orch_hub/housekeeping.py`

Responsibility: read the repo-local `.ai/.housekeeping-pending` marker for
display-only dashboard/API metadata. `housekeeping_pending(repo)` returns a
boolean and must not participate in readiness or launch gating.

## Subprocess Utilities

Location: `orch_hub/subprocess_utils.py`

Responsibility: central wrapper for running commands with cwd, capture, timeout if later needed, and concise output extraction. Do not hide command failures that readiness or API callers need to report.
The detached orchestrator launch is not routed through this blocking wrapper;
`RunManager` uses `subprocess.Popen` directly so it can keep a process handle
and redirect output.

## Task Inventory

Location: `orch_hub/task_store.py`

Responsibility: parse active task files without importing protocol or orchestrator code. `list_repo_tasks(repo)` reads `.ai-tasks/index.md` for active task ids and summaries, normalizing bare Task cells or full-cell Markdown links like `[id](id.md)` to the safe bare id used for file lookup and API payloads. It also parses five-column `id | title | status | session-est | blockers` indexes, using `title` as the summary there while preserving the legacy positional third-column summary for other table layouts. It then reads each task file frontmatter for authoritative status and blockers. Missing indexes return `index_present=False`; missing task files use a `missing` sentinel status; task files without parseable status use `unknown`. `read_task_content(repo, task_id)` backs the dashboard detail view and stays active-task scoped under `.ai-tasks`; `read_archived_task_content(repo, task_id)` reads `.ai-tasks/archive/<task_id>.md` for run-scoped historical access; `read_session_est(repo, task_id)` reads active-then-archive frontmatter `session-est: current/total` into `SessionEst | None`, with only genuine active-file absence falling through to archive and malformed/missing values degrading to unknown. Raw content readers reject non-bare filenames, resolve the target, and only return content whose resolved path remains under the tight containing directory.

## Intake Manager

Location: `orch_hub/intake.py`

Responsibility: own the remote intake-task lifecycle and hub-owned audit state.
`IntakeManager` stores each intake under `<data_dir>/intake/<intake_id>/` with
`intake.json` and metadata-only `events.jsonl`, gates start on non-empty input,
one in-flight intake per repo, and readiness, then drives an injected
`IntakeAgent` through draft, revise, confirm, and cancel turns.

Draft, revise, and confirm turns persist a transient status
(`drafting`/`revising`/`confirming`) under the manager lock, submit the slow
agent turn through an injected executor outside the lock, then re-acquire the
lock to finalize. Submitting outside the non-reentrant lock is load-bearing for
synchronous test executors. Finalizers re-load the record and settle only the
expected transient status; a cancel that landed during draft/revise wins, the
finished turn is discarded, and cancelled intakes are never resurrected.
`confirming` is deliberately non-cancellable because the write may already have
landed.

Draft/revise turns return complete proposals and leave target repos unchanged.
Only `confirm_intake` calls the agent write path. Confirmation is verified via
`list_repo_tasks`: the created id must appear in the target repo inventory with
frontmatter status `pending` or `blocked`. Missing/index-only/no-write,
invalid-status, agent-failure, lost-session, and unverified outcomes become
`failed` rather than `confirmed`; in-process failed/unverified confirms restore
the proposed task file and `.ai-tasks/index.md` snapshot. On manager startup,
`reconcile_orphans()` fails persisted transient records as lost sessions so
they stop holding the repo intake slot; it never mutates the target repo.

## Intake Agent

Location: `orch_hub/intake_agent.py`

Responsibility: provide the concrete Claude CLI backend for `/intake-task`.
`ClaudeIntakeAgent` runs `claude -p --output-format stream-json` in the target
repo, uses a structured fenced JSON envelope for drafts and confirmations,
threads session ids through `--session-id`/`--resume`, and parses assistant text
from stream events. Draft and revise use `--permission-mode plan`; confirm uses
the write-permitted CLI mode. Bad exits, stream errors, missing/invalid required
envelopes, and bad handles fail the agent turn so the manager records an
auditable failed intake.
