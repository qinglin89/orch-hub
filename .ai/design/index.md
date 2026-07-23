---
last-updated: 2026-07-23
verified-against: 0e716612253d7579fc8dee78b1dc565782e2755d
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
- Human-confirmed task creation: remote intake drafts stay hub-owned until the
  user explicitly confirms the complete task file and index row.
- Read-only task evaluation: evidence packs, deterministic static reports, and
  evidence-bounded semantic reports are hub-owned sidecar artifacts and must not
  mutate target tasks, run state, or protocol memory.
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
- Letting notification delivery failures affect run/intake records, history, or
  control-dir state.
- Logging Bark endpoint URLs or device keys.
- Passing bearer tokens in log-stream query strings.
- Adding a cookie or query-token credential path for the dashboard shell.
- Exposing the hub outside the tailnet.
- Running multiple orchestrators against the same repo.
- Treating an unconfirmed intake draft as a task or exposing it through active
  task inventory.
- Duplicating run start/stop behavior inside the intake flow after a task is
  created.
- Letting evaluation generation write into target repos or affect run/task
  lifecycle decisions.
- Letting semantic evaluator backends read live target-repo state outside the
  persisted evidence/static prompt boundary.
- Adding watchdog or log-silence detection.
- Hardcoding target repo paths in product code.
- Using broad shell commands where structured subprocess arguments are practical.

## Open Design Items

- Retention/reconciliation policy for hub-owned run dirs, intake dirs, and
  run dirs and intake dirs under `ORCH_HUB_DATA_DIR`, including per-run
  `events.jsonl` history and stuck `confirming` intake records after a
  mid-write process crash. Evaluation artifact retention is implemented as a
  per-task newest-N cap.
- Exact route schema beyond the implemented repo/task/run/escalation/log/history/
  intake/evaluation and dashboard-shell API surface.
