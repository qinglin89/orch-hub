---
last-updated: 2026-07-05
verified-against: 2ac51313fe081792d3cae8a8b40729561ab9b8f1
---

# Conventions

## Code Style

- Use Python with type hints for product code.
- Prefer stdlib modules for the first slice unless an external dependency removes real complexity.
- Use `pathlib.Path` for filesystem work.
- Keep subprocess invocations argument-list based; avoid shell strings unless there is a specific reason.
- Keep CLI output human-readable by default and stable JSON with `--json`.
- Keep functions small enough to reuse from both CLI and future FastAPI routes.

## Naming

- Package name: `orch_hub`.
- Use explicit domain names: `ManagedRepo`, `ReadinessReport`, `TaskSummary`, `RunRecord`, `Question`, `Answer`.
- Use `repo_path`, `task_id`, `run_id`, and `control_dir` consistently.
- Use `waiting` for an active run awaiting a human answer; reserve `blocked` for task frontmatter status.

## Error Handling

- Fail closed for launch readiness.
- Return structured reasons for readiness failures.
- Include concise command output for failed `aii-2 status` checks.
- Registry loading should tolerate a missing registry file as an empty repo list, but invalid JSON should be an explicit error.
- CLI should return non-zero for operational errors.

## Logging

- CLI logs should stay quiet unless an error occurs.
- Future service logs should include run id, repo name/path, task id, and event kind.
- Human answers should be audit logged with timestamp and responder identity when available.
- Do not log secrets or bearer tokens.

## Directory Organization

- Keep core logic in `orch_hub/`.
- Keep tests in `tests/`.
- Keep web assets under `orch_hub/web/` or a clear equivalent when the dashboard exists.
- Do not place generated run dirs inside managed target repos unless deliberately matching an orchestrator contract.

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
- Prefer testing structured return values before testing text formatting.

## Dependencies

- Keep the first slice dependency-light.
- FastAPI/uvicorn can be added when the service API task starts.
- Frontend dependencies should be justified by the dashboard implementation, not added during CLI-only work.

## Security

- Treat orch-hub as a remote code execution control surface.
- Bind service access to Tailscale/private interfaces for v1.
- Require a bearer token even on the tailnet.
- Never expose the hub publicly without a new security design.
