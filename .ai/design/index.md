---
last-updated: 2026-09-03
verified-against: 4d6e1a3b7c31a073417b9b681d5a009e288b71cf
---

# Design Principles and Decisions

## Core Principles

- Local-first control plane: orch-hub coordinates local repos and local processes on the Mac.
- Single registry source: managed repos come from mandrel registry data.
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
- Publication boundary: orch-hub durably publishes complete archived-task
  evaluation artifacts, identity, hashes, and provenance; downstream consumers
  alone interpret them, decide protocol evolution, and create improvement work.
- Display-only capacity: agent subscription usage is not readiness, attention,
  run-state, launch, intake, or evaluation policy.
- Observable, bounded Layer 2: model turns carry configured deadlines; live
  reaping uses the attempt's enforced persisted bound; starts are explicit and
  cancel is honest only while the attempt remains stoppable.
- Per-repo serialization: one active run per target repo to protect the working tree.
- Cross-kind repo admission: a live run and a non-terminal hub-driven intake
  cannot share a repo; admission is atomic and distinct from readiness.
- Auditability: human questions, answers, and stop requests should be reconstructable after the fact.

## Detail Files

| File | Contains |
|---|---|
| tradeoffs.md | Architectural and operational tradeoffs |
| patterns.md | Implementation patterns in use |

## Anti-Patterns to Avoid

- Importing `.mandrel/orchestrator/orchestrator.py`.
- Maintaining a second repo registry.
- Re-deriving a repo's protocol-line placement from git, receipts, manifests,
  revisions, or frozen run provenance instead of relaying mandrel's current
  canonical-identity reading opaquely.
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
- Using readiness/`git_clean` as a proxy for run↔intake admission, or keeping a
  second in-memory occupancy truth beside persisted run/intake records.
- Treating an unconfirmed intake draft as a task or exposing it through active
  task inventory.
- Duplicating run start/stop behavior inside the intake flow after a task is
  created.
- Letting evaluation generation write into target repos or affect run/task
  lifecycle decisions.
- Letting semantic evaluator backends read live target-repo state outside the
  persisted evidence/static prompt boundary.
- Ranking published evaluation reports, inferring protocol changes from them, or
  creating protocol-improvement tasks inside orch-hub's publication path.
- Starting model turns solely to refresh subscription usage, scraping provider
  UI/private endpoints, spending Codex reset credits, or treating quota data as
  repo health.
- Adding heuristic watchdog or log-silence detection; semantic-job recovery must
  use its enforced deadline rather than infer a hang from quiet output.
- Hardcoding target repo paths in product code.
- Using broad shell commands where structured subprocess arguments are practical.

## Open Design Items

- Retention/reconciliation policy for hub-owned run, intake, and evaluation
  state under `ORCH_HUB_DATA_DIR`, including per-run
  `events.jsonl` history and stuck `confirming` intake records after a
  mid-write process crash. Evaluation artifact retention is implemented as a
  per-task newest-N cap; the append-only report catalog deliberately outlives
  pruned artifacts and currently grows without bound.
- Exact route schema beyond the implemented repo/task/run/escalation/log/history/
  intake/evaluation/usage and dashboard-shell API surface.
