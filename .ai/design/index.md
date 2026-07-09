---
last-updated: 2026-07-10
verified-against: 3c6da44b4a90ac6e3c3d3730308604007148ff5d
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

## Detail Files

| File | Contains |
|---|---|
| tradeoffs.md | Architectural and operational tradeoffs |
| patterns.md | Implementation patterns in use |

## Anti-Patterns to Avoid

- Importing `.cursor/orchestrator/orchestrator.py`.
- Maintaining a second repo registry.
- Treating GitHub as registry truth.
- Starting orchestrator subprocesses before task inventory and readiness gates pass.
- Reusing run/control dirs across runs.
- Making network calls from the orchestrator control channel.
- Duplicating control-dir filename, validity, or stop-flag logic outside
  `orch_hub.control_dir`.
- Duplicating orchestrator log filename attribution outside `orch_hub.logs`.
- Copying human answer text into history/audit records.
- Letting notification delivery failures affect run records, history, or
  control-dir state.
- Logging Bark endpoint URLs or device keys.
- Passing bearer tokens in log-stream query strings.
- Adding a cookie or query-token credential path for the dashboard shell.
- Exposing the hub outside the tailnet.
- Running multiple orchestrators against the same repo.
- Adding watchdog or log-silence detection.
- Hardcoding target repo paths in product code.
- Using broad shell commands where structured subprocess arguments are practical.

## Open Design Items

- Retention policy for run dirs and per-run `events.jsonl` history under
  `ORCH_HUB_DATA_DIR`.
- Exact route schema beyond the implemented repo/task/run/escalation/log/history
  and dashboard-shell API surface.
