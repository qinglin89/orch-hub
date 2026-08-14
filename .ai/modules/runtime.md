---
last-updated: 2026-08-14
verified-against: 3bed60ab286b522f11ee0c6382855dfcf3101500
---

# Modules: Runtime

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
`/usr/bin/caffeinate -i <repo>/.cursor/orchestrator/.venv/bin/python
<repo>/.cursor/orchestrator/orchestrator.py <task-id> --control-dir
<run>/control [extra args]`, `cwd=<repo>`, stdin from devnull, stdout/stderr to
`spawn.out`, `start_new_session=True`, and an explicit environment whose `PATH`
prepends common user agent-CLI locations (`~/.local/bin`, `~/.claude/local`,
`~/.npm-global/bin`, `~/.cargo/bin`) while preserving the inherited environment
and baseline macOS paths. `run.json` is the source of truth,
stores launch-time task frontmatter status plus the internal `spawn_out` path,
and is rewritten atomically; in-memory `Popen` handles exist only to capture
exit codes for runs that end while the hub process is alive. `load_records`
returns `(run_dir, RunRecord)` pairs without refresh, pid probing, or writes for
historical/evaluation consumers that must not mutate run state.

Reattach scans run dirs, loads records, and probes pid liveness with
`os.kill(pid, 0)`. If a run ended while the hub was down, `ended_ts` is
recorded with `exit_code=None`. Live `RunStatus` reads derive a
`stop_requested` overlay from `control_dir.stop_requested` so a requested stop
survives dashboard reloads until the run ends; terminal statuses leave that
field false because stop evidence is expressed by end classification. End
classification prefers task completion or archival evidence, then hub stop
evidence (`stop.flag` or a valid `"stop"` answer), then bare exit code. Broken
run dirs surface as `error` states and do not block repo launches when no repo
attribution is available. The manager appends `run_started`, `run_ended`,
`stop_requested`, and `answer_recorded` events and exposes newest-first
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
fallback, and `recent_runs()` embeds the persisted effective config plus each
run's persisted session snapshot and current active-or-archived `session-est`.
Task estimates are deduped per `(repo, task)` and degrade to unknown on
task-file read failures.

Launch also captures the validated deployed protocol identity pair into
run-owned `protocol.json` after the readiness actual-file gate and before spawn.
At a directly observed live-to-ended transition, `_settle_protocol` rechecks the
lock and verifies the bounded complete lock/manifest target set against actual
files. Only an unchanged pair plus a matching payload is publishable; unobserved
ends, receipt damage, redeployment, or target drift stay explicitly unavailable
without changing run state or being repaired from later target state.

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

## Orchestrator Launch Config

Location: `orch_hub/orch_config.py`

Responsibility: keep orch-hub read-only with respect to orchestrator launch
policy. `query_effective_config` runs the repo-local deployed orchestrator
Python with `orchestrator.py --print-config`, `cwd=<repo>`, and optional shared
backend, legacy run-wide profile, and independent DEV/REVIEW profile flags. It
fails closed on missing deployment binaries, timeouts, nonzero exits, non-JSON,
unsupported schema versions, or malformed schema-v1 fields. An observed
`profiles.dev`/`profiles.review` pair sets the hub-owned split capability
marker; an older response without `profiles` derives both display selections
from top-level `profile` but remains incapable of split launch.
`build_launch_args(LaunchSpec)` owns mutually exclusive split
`dev_mode`/`review_mode` and legacy `mode` request shapes. Split named modes
emit only their role-profile flag; any non-Default role requires an explicit
shared backend; Custom emits only that role's explicit fields, with a complete
cc-codex DEV agent/model/effort tuple and no Cursor `--dev-agent`. Snapshot
helpers parse `effective-config:` log lines, persist/load
`effective_config.json`, and remain tolerant/raw because snapshots are
display/history data, not launch-preview validation.

## Run Monitor

Location: `orch_hub/monitor.py`

Responsibility: run one resident daemon thread that periodically calls a
scanner's `list_runs()` method, normally the app's single `RunManager`, so
run-ended notifications, pending-question notifications, and terminal usage
refresh requests fire without dashboard/API polling. `RunMonitor.start()` and
`stop()` are idempotent; `stop()` uses a bounded join, and scan failures are
logged and swallowed so the service keeps running. The default interval is 30
seconds and intentionally not an environment setting.

The monitor owns no run state, event state, notification formatting, or
de-duplication; it only schedules the existing run-manager status scan.

## History

Location: `orch_hub/history.py`

Responsibility: append and read metadata-only run history events in
`<data_dir>/runs/<run_id>/events.jsonl`. `append_event(run_dir, event)` writes
one sorted-key JSON object per line. `read_events(run_dir)` returns
`RunEvent` records, tolerates missing history as empty, and returns
`history_error` events for unreadable files, invalid JSON lines, or non-object
lines.

The module is storage-only: it does not parse task files, inspect control-dir
answers, decide event timing, or store human answer text. It round-trips the
optional answer `mode` field for `answer_recorded` audit events.

## Control Dir

Location: `orch_hub/control_dir.py`

Responsibility: implement the hub side of the orchestrator `--control-dir`
file channel. `list_questions(control_dir)` returns `Question` records for
matching `NNN-question.json` files; `pending_questions(control_dir)` filters to
questions without a same-seq valid answer; `write_answer(question, answer,
responder="orch-hub")` atomically writes the mirrored `NNN-answer.json` with
`seq`, `ts`, `responder`, and stripped `answer`; valid answers are exposed on
`Question.answer`. `request_stop(control_dir)` writes an idempotent empty
`stop.flag`; `stop_requested` and `stop_answered` support run-end
classification. Existing valid answers raise `DuplicateAnswerError`; malformed
answers can be repaired by atomic rewrite.

The module also owns pure answer-mode normalization constants and helpers that
mirror the deployed orchestrator parser: discussion markers match
`^(?:[?？]|discuss\s*:)` case-insensitively, plan-gate approval is a strict bare
confirm word after stripping/lowercasing, and `stop` bypasses every mode/kind
guard. `normalize_answer` is used only for explicit API modes; omitted mode is
the answer route's legacy verbatim path.

## Logs

Location: `orch_hub/logs.py`

Responsibility: locate and read/stream deployed orchestrator logs for known
runs. The module receives `RunRecord` values from the run manager and never
accepts a client-supplied path. It matches the orchestrator filename convention
`<utc-ts>-<task-id>.log` under the record's `log_dir`, using
`floor(started_ts)` as the lower bound and returning the earliest matching file
for correct re-run attribution. Records with no usable `started_ts` fall back
to the newest matching task log.

`read_recent(record, max_lines=...)` returns a `LogSnapshot` with bounded tail
lines, a `truncated` flag, current size, and graceful absent details for
missing/rotated/unreadable logs. `read_spawn_out(record, max_lines=...)` reads
the persisted startup capture path directly and treats missing, empty, or
blank-only captures as absent. `tail_events(record, is_live=...)` is an async
SSE generator: it waits for the log to appear, emits a bounded initial tail,
follows appended complete lines with byte offsets and a raw-bytes pending
buffer, sends idle heartbeat comments, and ends with `event: end` on run death
or log rotation. File reads run inline on the event loop in v1; move them to a
thread only if connection count grows.

## Sessions

Location: `orch_hub/sessions.py`

Responsibility: parse orchestrator run logs into per-protocol-session context
usage without changing the orchestrator contract. The parser recognizes
timestamp-prefixed `--- {role} session start/end` and dev `plan-gate`
boundaries plus `run finished: ... context≈N tokens` turn lines, reconciles
`(pending)` session ids when the end marker reveals the real id, counts turns,
keeps the latest/peak context figure, and tolerates missing, truncated, orphaned,
or mid-write lines as partial data. `read_live(record)` locates and parses the
current repo-side log; `write_snapshot`/`read_snapshot` atomically persist and
tolerantly load `sessions.json`.

## Session Entry Task Snapshots

Location: `orch_hub/task_snapshots.py`

Responsibility: preserve the target task file as each protocol `session`
entered it, using the session-start brackets already parsed from the
orchestrator log. Live scans capture only the last open bracket within a
three-minute window; already-ended, superseded, over-cap, and terminally
unobserved brackets remain explicit content-less misses. The terminal run
transition settles late-revealed session ids and records any bracket no live
scan saw. `plan-gate` brackets are excluded because they do not claim the task.

`task_snapshots.json` lives beside `sessions.json`; whole-artifact mutations are
serialized by a stable per-run `flock` and persisted by atomic rename. Capture
keeps at most 24 content-bearing entries and a 256KB UTF-8-safe head per file,
recording true byte size and truncation. `SessionTaskSnapshot.complete`
distinguishes a whole replayable file from a kept head, while
`frontmatter_complete` permits declaration consumers to use a head only when
its closing frontmatter fence is present. Missing, unreadable, or mid-write task
files warn and retry during the entry window; they never fail the run.

## Notifications

Location: `orch_hub/notifications.py`

Responsibility: define backend-agnostic notification payloads, the `Notifier`
protocol, the Bark backend, and shared event formatters. `Notification`
contains title, body, and optional group; `to_dict()` maps to the Bark JSON
payload. `BarkNotifier` POSTs JSON to the configured full Bark endpoint with a
bounded timeout and an injectable transport for tests. `build_notifier(config)`
returns `BarkNotifier` when `Config.bark_url` is set and `None` when push is
disabled.

Formatters are pure: `run_end_notification(record, state, detail)` turns the
run manager's completed/stopped/error/ended classification into device text,
`question_notification(repo_name, task_id, question)` formats a newly observed
pending control-dir question as `needs input`,
`intake_ready_notification(record)` formats a draft/revision that settled to
`awaiting_review`, `semantic_job_notification(job)` formats a fresh completed
or failed semantic-evaluation outcome, and `probe_notification()` builds the
harmless delivery-check payload used by `orch-hub notify test`.
`question_notification` keys plan-gate compaction off `question.kind ==
"plan-gate"` and emits the fixed prompt `plan ready for review`; every other
question keeps leading prompt content bounded by `QUESTION_PROMPT_MAX_CHARS`.
The complete question text remains in the dashboard, the control-dir question,
and the orchestrator log. Intake-ready and semantic-job payloads carry only
bounded identity/status metadata; semantic failures never include the backend
diagnostic/model output. The module does not decide when notifications fire;
`RunManager` owns live-run trigger policy, `IntakeManager` owns intake trigger
policy, `SemanticJobManager` owns fresh terminal-job trigger policy, and the CLI
sends probes directly so failures affect only the command exit code.
