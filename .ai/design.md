---
last-updated: 2026-07-08
verified-against: ad8c79d6782abbf5c670550a9d4a7d6963088fae
---

# Design Principles and Decisions

## Core Principles

- Local-first control plane: orch-hub coordinates local repos and local processes on the Mac.
- Single registry source: managed repos come from ai-native-development registry data.
- Orchestrator isolation: launch the deployed orchestrator as a subprocess; never import it.
- Non-invasive orchestrator integration: `--control-dir` adds a file channel while preserving stdin behavior when unset.
- File-channel boundary: questions, answers, and stop requests cross through run-dir files, not through network code inside the orchestrator.
- Mobile-first operation: remote workflows should be practical from iPhone/iPad.
- Fail closed before launch: readiness checks should block unclear or unsafe runs.
- Per-repo serialization: one active run per target repo to protect the working tree.
- Auditability: human questions, answers, and stop requests should be reconstructable after the fact.

## Key Tradeoffs

### Tradeoff: ai-native-development registry vs orch-hub registry

- Chose: ai-native-development registry.
- Reason: deployment tooling already tracks managed repos locally, and duplicate registries would drift.
- Consequence: orch-hub startup depends on ai-native-development path or `aii-2` availability.

### Tradeoff: subprocess boundary vs importing orchestrator code

- Chose: subprocess boundary.
- Reason: the orchestrator is a CLI state machine with blocking loops and `sys.exit` paths; importing would couple the hub to internals and risk process-level side effects.
- Consequence: status is inferred through task files, control-dir files, process
  state, and run records rather than direct orchestrator internals.

### Tradeoff: file-backed run records vs in-memory state only

- Chose: per-run `run.json` under `<data_dir>/runs/<run_id>/`.
- Reason: the hub is a resident process that may restart while orchestrator
  subprocesses keep running; persisted records let a fresh manager reattach by
  scanning run dirs and probing pid liveness.
- Consequence: ends observed while the hub was down have unknown exit code, and
  pid reuse after reboot can temporarily make a stale run look active until the
  dir is cleaned.

### Tradeoff: write `run.json` after spawn vs before spawn

- Chose: write after spawn succeeds.
- Reason: a crash before spawn would otherwise leave a false live run blocking
  the repo forever.
- Consequence: a hub crash in the tiny post-spawn/pre-write window leaves an
  unattributed broken run dir that surfaces as `error` but cannot block repo
  launches.

### Tradeoff: control-dir files vs HTTP callbacks inside orchestrator

- Chose: control-dir files.
- Reason: keeps orchestrator local, testable, and free of web/network dependencies.
- Consequence: hub must poll or watch files and handle stale files robustly.

### Tradeoff: Tailscale plus bearer token vs full auth system

- Chose: Tailscale plus simple bearer token for v1.
- Reason: the service controls arbitrary local code execution, so public exposure is out of scope; tailnet membership is the main security boundary.
- Consequence: multi-user permissions and public OAuth flows are deferred.

### Tradeoff: launchd environment config vs service CLI flags

- Chose: configure the resident service entirely through environment variables.
- Reason: launchd user agents naturally carry configuration in
  `EnvironmentVariables`, and the installed `orch-hub-api` console script can
  remain argument-free.
- Consequence: malformed env values must fail explicitly or be reported as
  operational CLI errors; the plist embeds `ORCH_HUB_TOKEN` and must be kept
  permission-restricted.

### Tradeoff: shared core before dashboard

- Chose: CLI/shared-core slices before dashboard, then a thin FastAPI
  foundation over the same functions.
- Reason: this validates external integration and task parsing before adding UI
  complexity, while keeping HTTP behavior aligned with the tested CLI/core
  payloads.
- Consequence: the dashboard can consume stable repo and task APIs; run,
  question, log, auth, and UI behavior remain separate slices.

## Patterns in Use

- Prefer small, reusable application functions behind both CLI and API routes.
- Represent repo, readiness, task, run, and escalation data with typed Python models.
- Use `pathlib.Path` for filesystem paths.
- Use subprocess wrappers for blocking `aii-2` and `git` checks; detached
  orchestrator runs use direct `subprocess.Popen` with argument lists so the
  hub can keep a process handle and redirect output.
- Return stable JSON from CLI commands and API endpoints from the same payload
  helpers.
- Keep FastAPI route handlers thin over core functions; do not duplicate
  discovery, readiness, or task-store logic in HTTP handlers.
- Tests should use temporary directories and mocked subprocess calls, never real user repos.
- Configuration should have explicit defaults and environment overrides.
- Empty or whitespace-only config environment values count as unset;
  surrounding whitespace is stripped, and malformed ports raise an explicit
  error instead of silently falling back.
- Treat a control-dir question as pending until its answer file is valid by
  the orchestrator's own rule; malformed answers remain visible so the hub can
  repair them by atomic rewrite.
- Refuse to overwrite existing valid answer files; they may already be
  consumed and are part of the answer audit trail.
- Keep run dirs outside target repos and create a fresh control dir per run;
  stale `stop.flag` files would stop reused runs immediately.
- Treat `run.json` as the source of truth for run state; in-memory process
  handles are only an optimization for observing exit codes.
- Classify finished runs from stable evidence in order: task
  completed/archived, hub stop evidence, then exit code.
- Require explicit acknowledgement for any `blocked` task launch because the
  hub cannot reliably distinguish a resumable blocked session from a foreign
  manual-session `claimed-by` id.

## Anti-Patterns to Avoid

- Importing `.cursor/orchestrator/orchestrator.py`.
- Maintaining a second repo registry.
- Treating GitHub as registry truth.
- Starting orchestrator subprocesses before task inventory and readiness gates pass.
- Reusing run/control dirs across runs.
- Making network calls from the orchestrator control channel.
- Duplicating control-dir filename, validity, or stop-flag logic outside
  `orch_hub.control_dir`.
- Exposing the hub outside the tailnet.
- Running multiple orchestrators against the same repo.
- Adding watchdog or log-silence detection.
- Hardcoding target repo paths in product code.
- Using broad shell commands where structured subprocess arguments are practical.

## Open Design Items

- Retention policy for run dirs and audit history under `ORCH_HUB_DATA_DIR`.
- Exact route schema beyond the implemented health/repos/tasks API foundation.
- Exact dashboard layout and frontend stack.
- Bark server packaging on the Mac.
- Whether run history is file-backed JSONL, SQLite, or another lightweight store.
