---
last-updated: 2026-08-26
verified-against: 0c72a56457e9fddbb1e1c1166e34ffb5c324ef60
---

# Features: Run Control

Run lifecycle, control-dir channel, and question relay. Log viewing and run
history/audit: `run-observability.md`.

## Run Lifecycle

Status: implemented

Description: Start, stop, list, get, and reattach orchestrator subprocesses.
Each run attaches to one repo and one task. Same-repo live runs are serialized;
cross-repo runs may overlap. A shared per-repo admission section also prevents
a run start from overlapping a non-terminal hub-driven remote intake.

Modules: admission, runs, intake, config, control_dir, models, readiness,
task_store, orch_config, protocol_provenance.

APIs: internal `RunManager`; `POST /api/runs`, `GET /api/runs`,
`GET /api/runs/{run_id}`, `GET /api/runs/{run_id}/sessions`,
`GET /api/runs/{run_id}/effective-config`, `POST /api/runs/{run_id}/stop`,
`GET /api/repos/{repo_id}/orchestrator-config`.

Implemented behavior:

- `RunManager.start_run(repo, task_id, extra_args=(), acknowledge_blocked=False)`
  gates launches fail-closed: no non-terminal intake through shared repo
  admission, same-repo live run, task inventory/status from `list_repo_tasks`,
  then readiness from `check_readiness`.
- Run and intake starts hold one per-repo admission section until the winner's
  persisted record exists, so simultaneous starts admit exactly one. Persisted
  unended run records remain admission-live until `_refresh` records the end;
  terminal intake records release admission. Cross-kind 409s carry
  `conflict: run_live|intake_in_flight` plus the blocking id and are separate
  from readiness.
- Task launch validation uses frontmatter-authoritative `TaskSummary.status`;
  unlisted tasks, `missing`/`unknown` task-store sentinels, and unrecognized
  statuses are rejected. `blocked` tasks require explicit acknowledgement.
- Run dirs live outside target repos under `<data_dir>/runs/<run_id>/`, with
  `run.json`, a fresh `control/` dir, and `spawn.out`. `run_id` combines UTC
  timestamp, repo slug, task slug, and a collision suffix when needed.
- After readiness has compared deployed target files with the manifest, launch
  captures the validated `(source_git_commit, canonical_payload_sha256)` identity
  from `.ai-deploy-lock.json` into run-owned `protocol.json`. At a directly
  observed end, the manager rechecks the lock and bounded complete lock/manifest
  target set against actual files; only an unchanged pair plus a matching payload
  is stable/publishable. The two boundaries are point observations, no historical
  state is backfilled, and provenance failure never changes run state.
- Launch command is `/usr/bin/caffeinate -i <repo>/.mandrel/orchestrator/.venv/bin/python
  <repo>/.mandrel/orchestrator/orchestrator.py <task-id> --control-dir
  <run>/control [extra args]`, with `cwd=<repo>`, stdin from devnull,
  stdout/stderr redirected to `spawn.out`, `start_new_session=True`, and an
  explicit `PATH` that includes common user-installed agent CLI directories
  before the inherited and baseline macOS paths.
- `orch_config.query_effective_config` asks the repo-local deployed
  orchestrator for `--print-config` with `cwd=<repo>` and optional shared
  backend, legacy profile, and independent DEV/REVIEW profile flags. Observed
  `profiles.dev`/`profiles.review` values are authoritative; older responses
  derive both roles from top-level `profile` for display but carry
  `split_profiles=false`. Missing deployments, nonzero exits, malformed
  launch-resolving fields, or unsupported schema versions fail closed. The
  advisory `options` catalog is normalized by backend/role/agent; absent or
  unusable catalog content becomes no offered list rather than a query failure
  or hub-owned fallback.
- Structured dashboard launches use `LaunchSpec`/`build_launch_args` before
  reaching `RunManager.start_run`: independent `dev_mode`/`review_mode`
  selections emit named `--dev-profile`/`--review-profile` flags or role-local
  Custom overrides. Backend may inherit only for Default/Default; split Custom
  cc-codex roles each require agent/model/effort and Cursor rejects either
  role-agent flag. Legacy structured `mode` still maps one selection to both
  roles, rejects the newer `review_agent`, and cannot be mixed with the split
  shape; raw `extra_args` remains verbatim and mutually exclusive with
  structured `launch`.
- `run.json` is the file-backed source of truth and is atomically rewritten;
  it persists the internal `spawn_out` path so reattached runs and pre-field
  records can still surface startup diagnostics. In-memory `Popen` handles only
  capture exit codes while the hub is the parent process.
- Reattach scans run dirs and probes pid liveness. If a run ended while the hub
  was down, `ended_ts` is persisted with `exit_code=None`.
- Live status is `active` or `waiting` based on pending control-dir questions
  and carries `stop_requested=true` while the live run's `stop.flag` exists.
  End status is classified as `completed`, `stopped`, `error`, or `ended` from
  frozen evidence in that run's own dir: final session status or an anchored
  `close-out done` line, then control-dir stop evidence, then exit code. It
  never falls back to the target repo's later task state; terminal run payloads
  keep `stop_requested=false`.
- `stop_run` writes `stop.flag` through `control_dir.request_stop` and refuses
  unknown, broken, or already-ended runs.
- HTTP run routes resolve repos through registry discovery, return serialized
  `RunStatus` payloads including the live-only `stop_requested` overlay, expose
  start/list/get/stop behavior, and map launch/run manager errors to 4xx JSON
  responses (`RepoBusyError` and ended/blocked states use 409; unknown runs use
  404).
- Per-session context usage is log-derived: live runs parse the current
  orchestrator log on demand, terminal runs serve the `sessions.json` snapshot
  persisted at the observed live-to-ended transition. Parsing failures are
  isolated from run state.
- The real run's startup `effective-config:` JSON is parsed from the located
  orchestrator log or `spawn.out`, persisted as `effective_config.json` at the
  live-to-ended transition, served by `GET /api/runs/{run_id}/effective-config`,
  and embedded snapshot-only in run history.

## Control-dir File Channel

Status: implemented

Description: Hub-side file adapter for the deployed orchestrator
`--control-dir` contract. It discovers human-question files, classifies pending
and answered state, writes answer files, and writes stop flags without adding
web/API logic to the adapter.

Modules: control_dir, models.

APIs: internal `list_questions`, `pending_questions`, `write_answer`,
`request_stop`, `DuplicateAnswerError`, and `Question`.

Implemented behavior:

- `list_questions(control_dir)` reads `NNN-question.json` files ordered by
  filename sequence; missing dirs return no questions; malformed question files
  are returned with `Question.error` instead of being skipped.
- Filename sequence is authoritative over payload `seq`; unknown `kind` values
  are surfaced for forward compatibility.
- `pending_questions(control_dir)` treats a question as pending until a same-seq
  answer file passes the orchestrator validity rule: JSON object with a
  non-empty string `answer`. Malformed answers keep the question pending and
  expose `Question.answer_error` for repair.
- `write_answer(question, answer, responder="orch-hub")` strips the answer,
  rejects empty text, mirrors the observed question filename, writes full audit
  fields atomically with a same-dir temp file plus rename, refuses to overwrite
  an existing valid answer, and repairs malformed answer files by atomic
  rewrite.
- `normalize_answer(text, mode, kind)` is a pure helper for explicit answer
  modes. It mirrors the orchestrator discussion marker regex and strict
  plan-gate confirm-word parser, fills empty plan-gate `answer` with `confirm`,
  passes `stop` through before every guard, and refuses mode/kind mismatches
  instead of silently reinterpreting a button press.
- `request_stop(control_dir)` writes an idempotent empty `stop.flag` and raises
  `FileNotFoundError` if the run dir does not exist.
- Tests cover discovery, parsing, pending/answered state, stale and malformed
  files, duplicate answer refusal, repair rewrite, stop flag writes, and
  `Question.to_dict()` path exclusion.

## Human Question Relay

Status: implemented

Description: Detect pending control-dir question files, list them remotely with
run/repo context, notify the user, accept answers, write answer files, and
audit the responder.

Modules: control_dir, runs, api, notifications, models.

APIs: `GET /api/escalations`,
`POST /api/runs/{run_id}/escalations/{seq}/answer`.

Implemented behavior:

- `GET /api/escalations` scans live runs only and returns pending
  control-dir questions with `run_id`, `repo_name`, `task_id`, and serialized
  `Question`.
- The answer route identifies questions by `(run_id, seq)` and writes through
  `control_dir.write_answer`, preserving duplicate-answer protection, malformed
  answer repair, empty-answer rejection, and responder audit fields. Omitted
  request `mode` is the legacy verbatim path; explicit `"answer"`/`"discuss"`
  modes are normalized fail-closed against question `kind` before writing.
- The dashboard task detail view renders two explicit-mode buttons for waiting
  runs: Discuss/Answer normally, Feedback/Confirm for `plan-gate`. Plan-gate
  Confirm can submit an empty box for one-tap approval; 400 guard details render
  inline; `"stop"` remains valid in every mode/kind.
- When a notifier is configured, `RunManager._status` fires a Bark `needs
  input` notification the first time its live-run `pending_questions` scan sees
  each `(run_id, seq)`. Dedup is in-memory, so rescans do not duplicate pushes
  in one hub process and a restart re-notifies a still-open question once.
  `plan-gate` questions use compact push text (`plan ready for review`) instead
  of the full plan banner; other question prompts are still included but bounded
  so oversized banners do not break delivery. The full prompt remains available
  through the dashboard/control-dir/log surfaces.
