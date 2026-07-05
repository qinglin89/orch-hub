---
last-updated: 2026-07-05
verified-against: 2ac51313fe081792d3cae8a8b40729561ab9b8f1
---

# AI Knowledge Router

Load only what the current task needs.

## Documents

| Document | File | Use when |
|---|---|---|
| Overview | overview.md | what orch-hub is, users, scope, non-goals, tech stack |
| Architecture | architecture.md | service shape, components, data flow, external dependencies, deployment |
| Design | design/index.md | principles, tradeoffs, patterns, anti-patterns |
| Modules | modules.md | planned module ownership, responsibilities, dependencies, boundaries |
| APIs | apis.md | CLI, future HTTP routes, internal interfaces, API conventions |
| Features | features.md | capabilities, status, cross-module behavior chains |
| Conventions | conventions/index.md | code style, naming, error/logging, git workflow, testing, dependencies |
| Map | map.md | feature to module to API cross-reference; horizontal sync source for absorption |

## Routing Table

| Task Type | Load Order |
|---|---|
| New feature | map -> features -> modules -> apis |
| API change | map -> apis -> modules -> architecture |
| Bug / debugging | map -> modules -> code |
| Architecture change | architecture -> design -> modules |
| Code review | map -> relevant docs -> code/tests |
| Conventions question | conventions/index.md |
| Onboarding | overview -> architecture -> map |
| Orchestrator integration | architecture -> apis -> modules -> features |
| Dashboard/UI work | features -> architecture -> design/index.md -> modules |

## Domain Vocabulary

- orch-hub: the resident local control plane built in this repository.
- Hub: short name for orch-hub service, CLI, and dashboard.
- ai-native-development: sibling canonical deployment/protocol repository at `/Users/linqing/workplace/github.com/qinglin89/ai-native-development`.
- aii-2: command from ai-native-development used to deploy/status managed repos and list the local registry.
- Managed repo: a target repository registered by ai-native-development, discovered from `aii-2 registry list --json` or `.registry/repos.local.json`.
- Target repo: a managed repo where an orchestrator may be launched.
- Deployed orchestrator: target repo copy of `.cursor/orchestrator/orchestrator.py`.
- Run: one orchestrator subprocess attached to one target repo and one `.ai-tasks/<task-id>.md` task.
- Active task: a task with an attached running orchestrator process.
- Waiting task: an active task whose orchestrator has emitted a human question and is awaiting an answer.
- Pending task: a task whose frontmatter status is `pending`.
- Blocked task: a task whose frontmatter status is `blocked`.
- Control dir: future per-run directory shared between orch-hub and orchestrator for question/answer files and stop requests.
- Question file: future `NNN-question.json` written by orchestrator when human input is needed.
- Answer file: future `NNN-answer.json` written by orch-hub after the remote user answers.
- `stop.flag`: future control-dir file requesting graceful stop at an orchestrator session boundary.
- Readiness: pre-launch checks proving a managed repo has required deployed files, task index, clean git state, and a passing `aii-2 status`.
