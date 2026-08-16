---
last-updated: 2026-08-16
verified-against: cad31851334def4258d4004c2e62516807aaff85
---

# Modules: Boundary Rules

Cross-module boundary rules; per-module responsibilities live in the sibling
detail files.

## Boundary Rules

- Registry code does not perform readiness checks.
- Readiness code does not launch orchestrators.
- Git metadata is best-effort display/evidence; never gates readiness, launch,
  or evidence.
- Housekeeping marker state is display-only; never gates readiness or launch.
- Task inventory reads task files; it does not decide orchestrator turns.
- Repo admission coordinates only cross-kind run/intake starts; each manager
  keeps same-kind serialization and file-backed occupancy truth. Admission is
  checked before, and remains separate from, readiness.
- Evaluation reads target tasks/run artifacts via existing stores; writes only
  contained artifacts under `<ORCH_HUB_DATA_DIR>/evaluations/`.
- AI snapshot evidence resolves memory through the target router/file shape,
  enforces repo containment and byte bounds at every read producer, and degrades
  docs and patches independently; it never writes the target snapshot.
- Session-read evidence derives bounded ordered facts from configured Claude
  transcripts, excludes close-out/read-back events, and reports missing or
  uncertain evidence instead of inventing behavior; it retains no raw transcript.
- Session-entry task snapshots observe existing log brackets, persist only in
  hub-owned run dirs, and never alter orchestrator or target task state.
- Static metrics read evidence packs; write only contained
  `static_metrics.json` in the same evaluation dir; no target/run/protocol
  mutation.
- Semantic evaluation reads only the bounded evidence/static prompt; writes
  only `semantic_report.json` + `report.md` under the evaluation dir; backends
  enforce read isolation structurally.
- Report catalog publishes complete archived-task artifacts with stable global
  identity/order and explicit provenance/integrity; it does not judge reports or
  decide protocol evolution. Its retention guard preserves unpublished debt.
- Usage reads provider-owned local signals plus the hub-owned Claude store and
  may run Claude's local `/usage` command only when its envelope proves zero
  model activity. The legacy statusLine writer captures only whitelisted
  capacity fields and passes the operator command through; neither directly
  opens credentials/endpoints, spends reset credits, or gates operations.
- Intake manager owns intake state, intake-ready trigger timing, and the
  confirmation postcondition; API/web neither treat drafts as tasks nor decide
  confirm outcomes.
- Run manager launches subprocesses; no task-protocol parsing beyond pre-start
  checks.
- Orchestrator launch config queries deployed `--print-config` and builds argv
  from structured selections; never imports orchestrator code or reads
  `orchestrator.toml`.
- History serializes run/audit metadata only; `RunManager` decides event
  timing.
- Control dir handles file-channel mechanics only; API/UI/run-manager own
  identity, audit storage, notifications, subprocess state.
- Logs locate/read/stream only from run records; callers never pass log paths.
- Run monitor schedules `RunManager.list_runs()` only; it owns no run state,
  notification policy, or de-duplication.
- API and CLI stay thin adapters over application functions.
- Launchd operations manage only the fixed hub service label; run stop stays
  in the run API/control-dir flow.
- Web UI consumes protected HTTP APIs; it never parses managed repo files
  directly.
- No module imports deployed orchestrator code.
