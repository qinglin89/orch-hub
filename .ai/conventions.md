---
last-updated: 2026-08-18
verified-against: 29e4c53412a0960cba31118549551de691b1af70
---

# Conventions

## Code Style

- Use Python with type hints for product code.
- Prefer stdlib modules for the first slice unless an external dependency removes real complexity.
- Use `pathlib.Path` for filesystem work.
- Keep subprocess invocations argument-list based; avoid shell strings unless there is a specific reason.
- Use direct `subprocess.Popen` for detached long-running orchestrator runs;
  the shared blocking subprocess wrapper is for command checks such as `git`
  and `mandrel`.
- Keep CLI output human-readable by default and stable JSON with `--json`.
- Keep functions small enough to reuse from both CLI and FastAPI routes.

## Naming

- Package name: `orch_hub`.
- Use explicit domain names: `ManagedRepo`, `ReadinessReport`, `TaskSummary`,
  `RunRecord`, `RunStatus`, `Question`, `Answer`, `EvaluationTarget`,
  `RelatedRun`, `EvidencePack`.
- Use `repo_path`, `task_id`, `run_id`, and `control_dir` consistently.
- Use `waiting` for an active run awaiting a human answer; reserve `blocked` for task frontmatter status.

## Error Handling

- Fail closed for launch readiness.
- Fail closed for repo admission: a persisted unended run and a non-terminal
  hub-driven intake cannot share a repo. Check admission before readiness and
  return the blocking id without changing readiness payloads.
- Fail closed for run starts: repo already busy, missing/unlisted/unknown task,
  unrecognized task status, unacknowledged `blocked` task, or readiness failure
  should prevent subprocess launch.
- Return structured reasons for readiness failures.
- Include concise command output for failed `mandrel status` checks.
- Registry loading should tolerate a missing registry file as an empty repo list, but invalid JSON should be an explicit error.
- CLI should return non-zero for operational errors.
- CLI config parsing errors should be reported as `error: ...` with non-zero
  exit, not as uncaught tracebacks.
- Invoke mandrel through the configured absolute path (`Config.mandrel_bin`),
  not by relying on PATH.
- Empty or whitespace-only environment config values count as unset; malformed
  explicit values such as an invalid `ORCH_HUB_PORT` should fail loudly.
- Evaluation disabled mode should fail closed at service/API boundaries and
  leave normal repo/task/run control available.

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
- Put hub-owned evaluation artifacts under `<ORCH_HUB_DATA_DIR>/evaluations/`,
  outside every managed target repo.

## Git Workflow

- Use Conventional Commit style where practical, for example `feat(cli): add repo readiness command`.
- Keep task-scoped changes small enough for review.
- Do not commit `.ai-tasks/`; it is active local workflow state and is gitignored.
- Do commit `.ai/` memory docs when they change.

## Testing Conventions

- Use pytest.
- Tests must not depend on real user repos or the real mandrel registry.
- Use `tmp_path` for fake repos and registry files.
- Mock subprocess for dirty git trees and `mandrel status` failures.
- Inject fake spawners, clocks, and pid probes for run-manager tests instead
  of starting real orchestrator subprocesses.
- Prefer testing structured return values before testing text formatting.
- Inject fake subprocess runners in tests instead of calling real managed repos
  or the real mandrel registry.
- Inject fake record loaders, git runners, and clocks for task-evaluation tests
  instead of reading real run history or invoking real git.
- Inject fake semantic backends/runners for Layer 2 evaluation tests; do not
  launch real Claude CLI, and assert the concrete backend's argv/cwd isolation.
- For non-trivial `orch_hub/web/app.js` behavior, extend the committed
  JavaScriptCore harness (`tests/keyboard_harness.js`, wrapped by
  `tests/test_web_keyboard.py`), which drives the real SPA against a stubbed
  browser and skips when no `jsc` is available; served-asset greps are for
  static shell/boundary guarantees.

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
