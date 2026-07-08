---
last-updated: 2026-07-08
verified-against: ad8c79d6782abbf5c670550a9d4a7d6963088fae
---

# Conventions

## Code Style

- Use Python with type hints for product code.
- Prefer stdlib modules for the first slice unless an external dependency removes real complexity.
- Use `pathlib.Path` for filesystem work.
- Keep subprocess invocations argument-list based; avoid shell strings unless there is a specific reason.
- Use direct `subprocess.Popen` for detached long-running orchestrator runs;
  the shared blocking subprocess wrapper is for command checks such as `git`
  and `aii-2`.
- Keep CLI output human-readable by default and stable JSON with `--json`.
- Keep functions small enough to reuse from both CLI and FastAPI routes.

## Naming

- Package name: `orch_hub`.
- Use explicit domain names: `ManagedRepo`, `ReadinessReport`, `TaskSummary`,
  `RunRecord`, `RunStatus`, `Question`, `Answer`.
- Use `repo_path`, `task_id`, `run_id`, and `control_dir` consistently.
- Use `waiting` for an active run awaiting a human answer; reserve `blocked` for task frontmatter status.

## Error Handling

- Fail closed for launch readiness.
- Fail closed for run starts: repo already busy, missing/unlisted/unknown task,
  unrecognized task status, unacknowledged `blocked` task, or readiness failure
  should prevent subprocess launch.
- Return structured reasons for readiness failures.
- Include concise command output for failed `aii-2 status` checks.
- Registry loading should tolerate a missing registry file as an empty repo list, but invalid JSON should be an explicit error.
- CLI should return non-zero for operational errors.
- CLI config parsing errors should be reported as `error: ...` with non-zero
  exit, not as uncaught tracebacks.
- Invoke `aii-2` through the configured absolute path (`Config.aii2_bin`), not by relying on PATH.
- Empty or whitespace-only environment config values count as unset; malformed
  explicit values such as an invalid `ORCH_HUB_PORT` should fail loudly.

## Logging

- CLI logs should stay quiet unless an error occurs.
- Service logs should include run id, repo name/path, task id, and event kind
  once run/event logging exists.
- Human answers should be audit logged with timestamp and responder identity when available.
- Do not log secrets or bearer tokens.

## Directory Organization

- Keep core logic in `orch_hub/`.
- Keep tests in `tests/`.
- Keep web assets under `orch_hub/web/` or a clear equivalent when the dashboard exists.
- Keep deployment templates under `deploy/` and operator docs under `docs/`.
- Do not place generated run dirs inside managed target repos unless deliberately matching an orchestrator contract.
- Put hub-owned run dirs under `<ORCH_HUB_DATA_DIR>/runs/`, outside every
  managed target repo.

## Git Workflow

- Use Conventional Commit style where practical, for example `feat(cli): add repo readiness command`.
- Keep task-scoped changes small enough for review.
- Do not commit `.ai-tasks/`; it is active local workflow state and is gitignored.
- Do commit `.ai/` memory docs when they change.

## Testing Conventions

- Use pytest.
- Tests must not depend on real user repos or the real ai-native-development registry.
- Use `tmp_path` for fake repos and registry files.
- Mock subprocess for dirty git trees and `aii-2 status` failures.
- Inject fake spawners, clocks, and pid probes for run-manager tests instead
  of starting real orchestrator subprocesses.
- Prefer testing structured return values before testing text formatting.
- Inject fake subprocess runners in tests instead of calling real managed repos or the real ai-native-development registry.

## Dependencies

- Keep the first slice dependency-light.
- FastAPI and uvicorn are core runtime dependencies for the HTTP foundation.
- httpx is a dev dependency for FastAPI TestClient coverage.
- Frontend dependencies should be justified by the dashboard implementation, not added during CLI-only work.

## Security

- Treat orch-hub as a remote code execution control surface.
- Bind service access to Tailscale/private interfaces for v1.
- Require a bearer token even on the tailnet.
- Never expose the hub publicly without a new security design.
