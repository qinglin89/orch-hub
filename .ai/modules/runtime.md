---
last-updated: 2026-07-10
verified-against: 30c1e08a6e706cab2d922417cf0024aa2920c5f7
---

# Modules: Runtime

## Run Manager

Location: `orch_hub/runs.py`

Responsibility: own run lifecycle for deployed orchestrator subprocesses:
start, stop, list, get, reattach, record pid, enforce a per-repo live-run
mutex, connect run dirs, and expose computed run state to future API/UI layers.
Launch gates run fail-closed in order: existing live run for the repo, task
inventory/status from `list_repo_tasks`, then readiness from `check_readiness`.
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
exit codes for runs that end while the hub process is alive.

Reattach scans run dirs, loads records, and probes pid liveness with
`os.kill(pid, 0)`. If a run ended while the hub was down, `ended_ts` is
recorded with `exit_code=None`. End classification prefers task completion or
archival evidence, then hub stop evidence (`stop.flag` or a valid `"stop"`
answer), then bare exit code. Broken run dirs surface as `error` states and do
not block repo launches when no repo attribution is available. The manager
appends `run_started`, `run_ended`, `stop_requested`, and `answer_recorded`
events and exposes newest-first `recent_runs(limit=...)` history entries.

The manager also owns notification trigger timing when a notifier is
configured: run-end notifications fire once at the observed live-to-ended
transition using the same end classification shown by the dashboard, and
pending-question notifications fire from `_status` when the existing
`pending_questions` scan first sees a question. API reads and the resident
`RunMonitor` both drive this same status path. Question push dedup is an
in-memory `(run_id, seq)` set, so rescans do not duplicate pushes within one
hub process and a restart re-notifies a still-open question once. Delivery goes
through `_emit`, which dispatches best-effort work off the lock by default and
swallows backend exceptions so notification failures cannot corrupt run state.

## Run Monitor

Location: `orch_hub/monitor.py`

Responsibility: run one resident daemon thread that periodically calls a
scanner's `list_runs()` method, normally the app's single `RunManager`, so
run-ended and pending-question notifications fire without dashboard/API
polling. `RunMonitor.start()` and `stop()` are idempotent; `stop()` uses a
bounded join, and scan failures are logged and swallowed so the service keeps
running. The default interval is 30 seconds and intentionally not an
environment setting.

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
answers, decide event timing, or store human answer text.

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
pending control-dir question as `needs input`, and `probe_notification()` builds
the harmless delivery-check payload used by `orch-hub notify test`. The module
does not decide when notifications fire; `RunManager` owns live-run trigger
policy and isolation, while the CLI sends probes directly so failures affect
only the command exit code.
