---
last-updated: 2026-08-16
verified-against: cad31851334def4258d4004c2e62516807aaff85
---

# AI Knowledge Router

Load only what the current task needs.

## Documents

| Document | File | Use when |
|---|---|---|
| Overview | overview.md | what orch-hub is, users, scope, non-goals, tech stack |
| Architecture | architecture/index.md | service shape, boundaries, data ownership, core flows, external dependencies, deployment |
| Design | design/index.md | principles, tradeoffs, patterns, anti-patterns |
| Modules | modules/index.md | planned module ownership, responsibilities, dependencies, boundaries |
| APIs | apis/index.md | CLI, HTTP routes, internal interfaces, API conventions |
| Features | features/index.md | capabilities, status, cross-module behavior chains |
| Conventions | conventions.md | code style, naming, error/logging, git workflow, testing, dependencies |
| Glossary | glossary.md | domain vocabulary and product terms |
| Map | map.md | feature to module to API cross-reference; horizontal sync source for absorption |

## Routing Table

| Task Type | Load Order |
|---|---|
| New feature | map -> features/index.md -> modules/index.md -> apis/index.md |
| API change | map -> apis/index.md -> modules/index.md -> architecture/index.md |
| Bug / debugging | map -> modules/index.md -> code |
| Architecture change | architecture/index.md -> design/index.md -> modules/index.md |
| Code review | map -> relevant docs -> code/tests |
| Conventions question | conventions.md |
| Onboarding | overview -> architecture/index.md -> map |
| Orchestrator integration | architecture/index.md -> apis/index.md -> modules/index.md -> features/index.md |
| Dashboard/UI work | features/index.md -> architecture/index.md -> design/index.md -> modules/index.md |
| Deployment packaging | features/index.md -> architecture/index.md -> modules/index.md -> conventions |
