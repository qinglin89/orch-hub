---
last-updated: 2026-07-10
verified-against: 30c1e08a6e706cab2d922417cf0024aa2920c5f7
---

# APIs: Internal Interfaces

## Registry Provider

Input: config with ai-native-development path.

Output: list of managed repo models.

Behavior: command source first, registry-file fallback second. An exit-0 command with invalid JSON raises `RegistryError`; command missing/non-zero falls back; missing fallback file returns an empty list.

## Readiness Checker

Input: managed repo model.

Output: readiness report with boolean `ready` and list of failed checks.

Behavior: pure inspection and subprocess checks only; no mutation. Missing repo path skips subprocess execution but records `git_clean` and `aii_status` as failed so launch readiness still fails closed.

## Task Store

Input: repo path.

Output: `RepoTaskInventory` values containing repo fields, `index_present`, ordered `TaskSummary` rows, and optional raw task content for one listed task.

Behavior: parse `.ai-tasks/index.md` and task frontmatter locally. The index supplies active task ids and summaries; task frontmatter supplies authoritative status and blockers. The reusable `tasks_payload(inventories)` helper serializes as `{"repos": [...]}` for future CLI/API adapters.
`read_task_content(repo, task_id)` is the only raw-body reader: it rejects
non-bare filenames, requires the file target to resolve under the resolved
`.ai-tasks` directory, and returns `None` for absent or unsafe content.

## Run Manager

Input: repo id, task id, start flags.

Output: `RunRecord` from `start_run`, `RunStatus` from list/get operations,
and idempotent stop-request results.

Behavior: implemented subprocess lifecycle, per-repo serialization, and history
event ownership.
`RunManager` receives a `ManagedRepo` plus task id, rejects launches when the
repo already has a live run, when task inventory is missing/unlisted or reports
`missing`/`unknown`/unrecognized statuses, when a `blocked` task is not
explicitly acknowledged, or when readiness fails. A successful launch creates a
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
`audit_answer` appends responder/question metadata after a control-dir answer
write, without copying answer text. `recent_runs` returns newest-first
`RunHistoryEntry` values from run records plus `events.jsonl`.

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
control-dir answer file, not in `events.jsonl`.

## Notifier

Input: `Notification` event with title, body, and optional group.

Output: best-effort delivery through a configured backend.

Behavior: `build_notifier(config)` returns `BarkNotifier` when
`Config.bark_url` is set, otherwise `None`. Bark sends JSON to the configured
full endpoint, which includes the device key and must not be logged. The run
manager owns live-run trigger timing and isolation: terminal notifications fire
at the observed live-to-ended transition, pending-question notifications fire
once per observed `(run_id, seq)`, and backend exceptions are swallowed without
changing run records, history, or control-dir files. `orch-hub notify test`
delivers a probe directly through the same notifier factory so delivery failure
is visible to the CLI exit code while remaining outside run state.

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

## API Conventions

- Prefer absolute paths internally; avoid exposing more path data than the UI needs.
- Keep JSON stable and explicit.
- Include `ready: false` with reasons instead of raising for ordinary readiness failures.
- Raise or return operational errors for invalid registry JSON, unreadable config, or malformed user requests.
- Keep task `blocked` distinct from run `waiting`.
- Never make API handlers call orchestrator internals directly.
- API/run handlers should reuse `orch_hub.control_dir` for question, answer,
  and stop files instead of reconstructing filenames or validity rules.
- API/run handlers should reuse `orch_hub.runs.RunManager` for launch gates,
  per-repo serialization, stop requests, and end classification instead of
  rebuilding subprocess or run-dir behavior in route code.
- API/log handlers should reuse `orch_hub.logs` for log location, bounded
  reads, and SSE framing instead of accepting client paths or rebuilding the
  orchestrator filename convention in route code.
- API/history handlers should reuse `RunManager.recent_runs` /
  `orch_hub.history` rather than reading run dirs directly in route code.
