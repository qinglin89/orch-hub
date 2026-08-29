---
last-updated: 2026-08-29
verified-against: b0df3407d9cf9420ddb7e616fa5e3e4b147ac584
---

# Modules: Runtime

Run admission, launch, and monitoring. Run file channels and observability
(history, control-dir, logs, sessions, task snapshots, notifications):
`runtime-io.md`.

## Repo Admission

Location: `orch_hub/admission.py`

Responsibility: enforce the cross-kind repo invariant: one live orchestrator
run and one non-terminal hub-driven intake cannot coexist. `RepoAdmission`
holds a distinct critical section per repo across each start gate until the
winner persists `run.json` or `intake.json`; cross-repo starts remain
independent. It asks `RunManager.live_run_for` and
`IntakeManager.active_intake_for` for file-backed occupancy rather than keeping
a second reservation table. Run occupancy is every readable repo-attributed
persisted record with no `ended_ts`, even after process death until `_refresh`
persists the end; intake occupancy is every non-terminal record.

The probes are read-only and manager-lock-free so lock order stays repo section
→ own manager lock and cross-repo work never queues behind an unrelated
manager-wide readiness call. `create_app` places both managers on one
coordinator even when they were independently injected. Cross-kind refusals
carry the blocking id through `RunLiveError` / `IntakeInFlightError`; admission
is checked before readiness and never changes readiness payloads. A recovered
overlap uses the non-locking `require_no_live_run` guard to refuse intake
revise/confirm while lifecycle-legal Cancel remains available.

## Run Manager

Location: `orch_hub/runs.py`

Responsibility: own run lifecycle for deployed orchestrator subprocesses:
start, stop, list, get, reattach, record pid, enforce a per-repo live-run
mutex, connect run dirs, and expose computed run state to future API/UI layers.
Launch gates run fail-closed in order: shared repo admission (no non-terminal
intake), existing live run for the repo, task inventory/status from
`list_repo_tasks`, then readiness from `check_readiness`.
`blocked` tasks require explicit acknowledgement before launch; `missing`,
`unknown`, and unrecognized task statuses are rejected.

Run dirs live under `<data_dir>/runs/<run_id>/` with `run.json`,
`events.jsonl`, `control/`, and `spawn.out`; each run gets a fresh external
control dir. Launch uses
`/usr/bin/caffeinate -i <repo>/.mandrel/orchestrator/.venv/bin/python
<repo>/.mandrel/orchestrator/orchestrator.py <task-id> --control-dir
<run>/control [extra args]`, `cwd=<repo>`, stdin from devnull, stdout/stderr to
`spawn.out`, `start_new_session=True`, and an explicit environment whose `PATH`
prepends common user agent-CLI locations (`~/.local/bin`, `~/.claude/local`,
`~/.npm-global/bin`, `~/.cargo/bin`) while preserving the inherited environment
and baseline macOS paths. `run.json` is the source of truth,
stores launch-time task frontmatter status plus the internal `spawn_out` path,
the hub-only default-true `auto_semantic_eval` completion choice, and is rewritten
atomically; the choice never becomes orchestrator argv and absent legacy values
load true. In-memory `Popen` handles exist only to capture exit codes for runs
that end while the hub process is alive. `load_records`
returns `(run_dir, RunRecord)` pairs without refresh, pid probing, or writes for
historical/evaluation consumers that must not mutate run state.

Reattach scans run dirs, loads records, and probes pid liveness with
`os.kill(pid, 0)`. If a run ended while the hub was down, `ended_ts` is
recorded with `exit_code=None`. Live `RunStatus` reads derive a
`stop_requested` overlay from `control_dir.stop_requested` so a requested stop
survives dashboard reloads until the run ends; terminal statuses leave that
field false because stop evidence is expressed by end classification. End
classification uses only frozen evidence owned by the run: completion from
the final parsed session status or the anchored `close-out done` line in its
`spawn.out`, then hub stop evidence (`stop.flag` or a valid `"stop"` answer),
then bare exit code. Missing evidence degrades to `ended`/`error`; the target
repo's current task state is never a fallback. Broken run dirs surface as
`error` states and do not block repo launches when no repo attribution is
available. The manager appends `run_started`, `run_ended`, `stop_requested`,
and `answer_recorded` events and exposes newest-first
`recent_runs(limit=...)` history entries. Answer events include optional
`mode` metadata (`answer`/`discuss`/`null`) but never the answer text.

The manager persists per-session context snapshots when a run transitions from
live to ended: it parses the repo-side orchestrator log once into `sessions.json`
beside `run.json`, isolated so parse/IO failures cannot affect run state.
`read_sessions(run_id)` serves live runs from the current log, ended runs from
the persisted snapshot, and falls back to live parsing only for pre-field runs
with no snapshot. The same transition snapshots the real startup
`effective-config:` JSON into `effective_config.json`; `read_effective_config`
serves live runs from the current log, ended runs from the snapshot with log
fallback, and Run-history reads embed the persisted effective config, protocol
snapshot, per-session snapshot, and current active-or-archived `session-est`.
Task estimates are deduped per `(repo, task)` and degrade to unknown on
task-file read failures.

Launch also captures the validated deployed protocol identity pair into
run-owned `protocol.json` after the readiness actual-file gate and before spawn.
At a directly observed live-to-ended transition, `_settle_protocol` rechecks the
lock and verifies the bounded complete lock/manifest target set against actual
files. Only an unchanged pair plus a matching payload is publishable; unobserved
ends, receipt damage, redeployment, or target drift stay explicitly unavailable
without changing run state or being repaired from later target state.
`protocol_coverage(config)` is the non-refreshing aggregate over these run-owned
artifacts. It joins a pending artifact to `_refresh`'s persisted reachability
branches through `_end_check_owed`: error stubs can never settle, unended records
still can, and ended records can only while `end_settling` remains. The read uses
`load_records`, performs no pid probe/write/settlement, and preserves the module's
ordered reason vocabulary rather than deriving a second protocol verdict.

The manager also owns notification trigger timing when a notifier is
configured: run-end notifications fire once at the observed live-to-ended
transition using the same end classification shown by the dashboard, and
pending-question notifications fire from `_status` when the existing
`pending_questions` scan first sees a question. API reads and the resident
`RunMonitor` both drive this same status path. Question push dedup is an
in-memory `(run_id, seq)` set, so rescans do not duplicate pushes within one
hub process and a restart re-notifies a still-open question once. Plan-gate
question pushes keep the same trigger path but use compact notification text so
the full plan report does not enter the Bark/APNs payload. Delivery goes through
`_emit`, which dispatches best-effort work off the lock by default and swallows
backend exceptions so notification failures cannot corrupt run state.
The same once-only live-to-ended branch invokes an optional plain
`usage_refresh` callable after persistence/notification classification. The
app supplies `UsageService.request_claude_refresh`, which returns immediately;
hook failures are logged by run id and cannot alter run state.
The same settled terminal tail invokes an optional task-completion hook for a
`completed` classification only after session/task/protocol/config snapshots.
The two-phase `end_settling` record marker resumes an interrupted tail entirely
from run-owned evidence; completion-hook failure is isolated from run outcome.

## Orchestrator Launch Config

Location: `orch_hub/orch_config.py`

Responsibility: keep orch-hub read-only with respect to orchestrator launch
policy. `query_effective_config` runs the repo-local deployed orchestrator
Python with `orchestrator.py --print-config`, `cwd=<repo>`, and optional shared
backend, legacy run-wide profile, and independent DEV/REVIEW profile flags. It
fails closed on missing deployment binaries, timeouts, nonzero exits, non-JSON,
unsupported schema versions, or malformed launch-resolving fields. The advisory
`options` catalog is the sole tolerant field: it is normalized to an
`agents[backend][role][agent]` projection, with absent/unusable content becoming
`None` rather than disabling launch or supplying hub-owned choices. An observed
`profiles.dev`/`profiles.review` pair sets the hub-owned split capability
marker; an older response without `profiles` derives both display selections
from top-level `profile` but remains incapable of split launch.
`build_launch_args(LaunchSpec)` owns mutually exclusive split
`dev_mode`/`review_mode` and legacy `mode` request shapes. Split named modes
emit only their role-profile flag; any non-Default role requires an explicit
shared backend; Custom emits only that role's explicit fields. Each Custom
cc-codex role requires its complete agent/model/effort tuple; Cursor rejects
either role-agent flag. The legacy run-wide shape rejects `review_agent` rather
than ignoring it. Snapshot
helpers parse `effective-config:` log lines, persist/load
`effective_config.json`, and remain tolerant/raw because snapshots are
display/history data, not launch-preview validation.

## Run Monitor

Location: `orch_hub/monitor.py`

Responsibility: run one resident daemon thread that periodically calls a
scanner's `list_runs()` method plus configured sweeps, normally the app's single
`RunManager` and automatic-evaluation dispatcher, so run-ended notifications,
pending-question notifications, terminal usage refresh requests, and deferred
Layer 2 retry happen without dashboard/API polling. `RunMonitor.start()` and
`stop()` are idempotent; `stop()` uses a bounded join, and scan failures are
logged and swallowed so the service keeps running. The default interval is 30
seconds and intentionally not an environment setting.

The monitor owns no run/evaluation state, event state, notification formatting,
or de-duplication; it only schedules the existing run-manager status scan and
isolated sweeps.
