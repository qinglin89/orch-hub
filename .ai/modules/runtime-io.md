---
last-updated: 2026-08-27
verified-against: c86cb40b5f7880c1876147c32c428fd8ef51d474
---

# Modules: Runtime I/O and Observability

Run file channels and derived observability. Run admission/launch/monitor:
`runtime.md`.

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
tolerantly load `sessions.json`. Terminal classification uses
`read_run_owned(run_dir, record)` for final session evidence (snapshot first,
then the run's `spawn.out`) and `read_run_closeout(record)` for the anchored
`close-out done` completion path that has no session bracket. Neither helper
reads the collision-prone repo-side log or mutable task state.

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
policy, `SemanticJobManager` owns fresh terminal-job trigger policy (manual
outcomes and automatic failures notify; automatic success and skip are quiet),
and the CLI sends probes directly so failures affect only the command exit code.
