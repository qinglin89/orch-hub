---
last-updated: 2026-08-19
verified-against: c7fbcf08b005bfe0a06f6a3614923646a51e3595
---

# Glossary

## Product Terms

- orch-hub: the resident local control plane built in this repository.
- Hub: short name for orch-hub service, CLI, and dashboard.
- mandrel: sibling canonical deployment/protocol repository at
  `/Users/linqing/workplace/github.com/qinglin89/mandrel`; its repo-local
  `bin/mandrel` command deploys/statuses managed repos and lists the registry.
- Managed repo: a target repository registered by mandrel, discovered from
  `mandrel registry list --json` or `.registry/repos.local.json`.
- Target repo: a managed repo where an orchestrator may be launched.
- Deployed orchestrator: target repo copy of `.mandrel/orchestrator/orchestrator.py`.

## Run Terms

- Run: one orchestrator subprocess attached to one target repo and one `.ai-tasks/<task-id>.md` task.
- Run manager: hub application service in `orch_hub.runs` that starts, lists,
  gets, stops, reattaches, persists, and classifies runs.
- Run record: `run.json` under `<ORCH_HUB_DATA_DIR>/runs/<run_id>/`, the
  file-backed source of truth for one run.
- Active task: a task with an attached running orchestrator process.
- Waiting task: an active task whose orchestrator has emitted a human question and is awaiting an answer.
- Pending task: a task whose frontmatter status is `pending`.
- Blocked task: a task whose frontmatter status is `blocked`.

## Control and Log Terms

- Control dir: per-run directory shared between orch-hub and orchestrator for question/answer files and stop requests.
- Question file: `NNN-question.json` written by orchestrator when human input is needed.
- Answer file: `NNN-answer.json` written by orch-hub after the remote user answers.
- `stop.flag`: control-dir file requesting graceful stop at a session boundary, or abort while an answer is pending.
- Log snapshot: bounded recent tail read from the orchestrator log located through one `RunRecord`; absent/rotated logs are represented as data, not errors.
- Log stream: SSE tail of a run's located orchestrator log, authenticated with the normal bearer header and consumed by browsers through `fetch`/ReadableStream.

## Deployment Terms

- Readiness: pre-launch checks proving a managed repo has required deployed
  files, task index, clean git state, and a passing `mandrel status`.
- Launchd service: resident macOS user agent that runs `orch-hub-api` from the repo venv.
- Deployment template: checked-in operational artifact such as `deploy/launchd/com.qinglin89.orch-hub.plist`.
