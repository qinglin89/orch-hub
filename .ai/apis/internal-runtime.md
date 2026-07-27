---
last-updated: 2026-07-27
verified-against: 2bc59e6a5e02c101b912b35b561835b8a65f9d0d
---

# APIs: Internal — Runtime

Repo-admission, intake, run, control-dir, log, history, notifier, and launchd-template
interfaces. Core/sidecar interfaces and API conventions: `internal-core.md`.

## Repo Admission

Input: `ManagedRepo` plus manager-provided run/intake occupancy probes.

Output: `admit_run` / `admit_intake` per-repo context managers, structured
`IntakeInFlightError` / `RunLiveError`, and the recovered-overlap
`require_no_live_run` guard.

Behavior: both start paths share one per-repo critical section until the
winner's file-backed record is persisted. `RunManager.live_run_for` treats
every readable repo-attributed record with no `ended_ts` as occupied;
`IntakeManager.active_intake_for` treats every non-terminal record as occupied.
Probes are read-only and manager-lock-free. `create_app` rebinds both managers
to one coordinator before serving. Admission is checked before readiness but
does not modify readiness or each manager's same-kind serialization.

## Intake Manager

Input: managed repo, natural-language request, optional revision feedback, and
explicit confirm/cancel operations.

Output: persisted `IntakeRecord` values, metadata-only intake events, and
optional intake-ready notification dispatch.

Behavior: `IntakeManager` gates start on request text, shared repo admission,
one in-flight intake per repo, and readiness. It stores records under
`<data_dir>/intake/<id>/` and drives an injected `IntakeAgent`. Draft/revise
turns are proposal-only; confirm is the sole write-capable turn. Recovered
live-run overlaps refuse revise/confirm before handle use; lifecycle-legal
Cancel stays available because it writes no repo state. The manager records
`confirmed` only when the
reported/draft task id is visible through `list_repo_tasks` with status
`pending` or `blocked`; otherwise it records `failed`. Failed/unverified
in-process confirms restore the proposed task file and `.ai-tasks/index.md`
snapshot. Cancel is allowed before confirming and writes no target repo files.
Successful draft/revise finalizers emit one metadata-only notification after
`awaiting_review` persistence; failures, cancels/discards, and confirms do not.

## Intake Agent

Input: request/feedback/handle plus managed repo.

Output: `AgentDraftResult` with a complete `IntakeDraft`, or
`AgentConfirmResult` with a best-effort created task id.

Behavior: the protocol requires draft/revise to be read-only and confirm to
perform the `/intake-task` write. `ClaudeIntakeAgent` is the default concrete
adapter: it shells out to Claude in the target repo, uses stream-json output,
requires structured draft envelopes, resumes sessions by id, runs draft/revise
under `--permission-mode plan`, and uses the write-permitted mode only for
confirm. One model/effort pair applies to every turn and comes from
`Config.intake_model` / `Config.intake_effort` (environment defaults:
`claude-opus-5` / `max`). It accepts an optional usage observer called with
stdout after a Claude turn returns; observer failures are swallowed so usage
bookkeeping cannot change intake outcomes. Bad CLI exits or bad envelopes raise
so the manager records failed intakes.

## Run Manager

Input: repo id, task id, start flags.

Output: `RunRecord` from `start_run`, `RunStatus` from list/get operations,
read-only `(run_dir, RunRecord)` records from `load_records`, and idempotent
stop-request results.

Behavior: implemented subprocess lifecycle, per-repo serialization, and history
event ownership.
`RunManager` receives a `ManagedRepo` plus task id, rejects launches when the
repo has a non-terminal intake, when it already has a live run, when task
inventory is missing/unlisted or reports `missing`/`unknown`/unrecognized
statuses, when a `blocked` task is not explicitly acknowledged, or when
readiness fails. A successful launch creates a
fresh `<data_dir>/runs/<run_id>/` dir with `control/`, starts the target repo's
deployed orchestrator through `/usr/bin/caffeinate -i` with `--control-dir`,
an explicit subprocess `PATH` for user-installed agent CLIs, persists
`run.json` with launch-time task status and the internal `spawn_out` path,
appends `run_started`, and records stdout/stderr in `spawn.out`.
`list_runs` and `get_run` refresh
live/end state and support reattach from disk by pid probe; observed ends append
`run_ended`. `stop_run` delegates to `control_dir.request_stop`, appends
`stop_requested` only on the first idempotent write, and refuses ended or
broken runs so a post-mortem flag cannot rewrite end classification.
`audit_answer` appends responder/question metadata plus optional answer `mode`
after a control-dir answer write, without copying answer text. `recent_runs`
returns newest-first `RunHistoryEntry` values from run records plus
`events.jsonl`.

## Control Dir Store

Input: run dir.

Output: pending question records and answer/stop write operations.

Behavior: file-channel only. Missing control dirs list no questions.
Question filenames supply the authoritative sequence. Malformed question JSON is
reported on the returned `Question` instead of hiding the file. A question is
answered only when a same-seq answer file is a JSON object with a non-empty
string `answer`; the stripped valid answer is exposed as `Question.answer`.
Malformed answers keep the question pending and can be repaired with
`write_answer`. Valid answer files are never overwritten. `request_stop` writes
an empty `stop.flag` and requires the run dir to exist; `stop_requested` and
`stop_answered` are read helpers for run classification.
`normalize_answer(text, mode=..., kind=...)` mirrors the orchestrator's
discussion-marker and strict plan-gate confirm parsing for explicit dashboard
button intent. It prepends/refuses discussion markers or plan-gate confirm words
so mode and parser outcome cannot silently diverge; omitted API `mode` skips
this helper for legacy verbatim writes.

## Log Store

Input: run record.

Output: `LogSnapshot` for bounded recent reads and async SSE frames for live
tailing.

Behavior: file access is scoped to `RunRecord.log_dir` and `RunRecord.task_id`.
Log files are located by the deployed orchestrator's
`<utc-ts>-<task-id>.log` filename convention, with `floor(started_ts)` as the
lower bound. Recent reads cap line and byte counts and return absent snapshots
for missing/rotated logs. Startup diagnostics read the persisted `spawn_out`
path directly, returning absent snapshots for missing, empty, or blank-only
captures. Live tails emit only complete newline-terminated lines, use byte
offsets to avoid duplicate initial/follow output, and terminate gracefully when
the run ends or the located file disappears.

## History Store

Input: run dir and `RunEvent` values.

Output: append-only `events.jsonl` plus `RunEvent` readback.

Behavior: `append_event` writes one JSON object per line. `read_events` returns
an empty tuple for missing history and `history_error` events for corrupt or
unreadable history lines. History is metadata-only; answer text remains in the
control-dir answer file, not in `events.jsonl`. `answer_recorded` events may
carry `mode` (`answer`, `discuss`, or `null` for legacy omitted-mode callers).

## Notifier

Input: `Notification` event with title, body, and optional group.

Output: best-effort delivery through a configured backend.

Behavior: `build_notifier(config)` returns `BarkNotifier` when
`Config.bark_url` is set, otherwise `None`. Bark sends JSON to the configured
full endpoint, which includes the device key and must not be logged. The run
manager owns trigger timing and isolation for its domain: `RunManager` fires
terminal notifications at the observed live-to-ended transition and
pending-question notifications once per observed `(run_id, seq)`;
`IntakeManager` fires intake-ready notifications after successful
draft/revision settlement. Backend exceptions are swallowed without changing
run records, intake records, history, or control-dir files. `orch-hub notify
test` delivers a probe directly through the same notifier factory so delivery
failure is visible to the CLI exit code while remaining outside manager state.

## Launchd Service Template

Input: environment variables in
`deploy/launchd/com.qinglin89.orch-hub.plist`.

Output: resident `orch-hub-api` process managed by launchd, with stdout/stderr
logs under `~/Library/Logs/orch-hub/`.

Behavior: user-agent template sets all current `ORCH_HUB_*` variables
including optional `ORCH_HUB_BARK_URL`, plus an explicit `PATH`, starts at
login, and restarts on exit. Required placeholders must resolve before writing
or bootstrapping; unknown optional placeholders are blanked for forward
compatibility with newer templates. Install/start/stop/reload/log commands live
in `docs/launchd.md`.
