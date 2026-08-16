---
last-updated: 2026-08-16
verified-against: cad31851334def4258d4004c2e62516807aaff85
---

# Architecture: Ownership and Boundaries

Data ownership, component boundaries, and external dependencies. Shape and
invariants: `index.md`. Runtime flows: `flows.md`.

## Data Ownership

| Data | Source Of Truth | Architecture Rule |
|---|---|---|
| Managed repos | ai-native-development registry / `aii-2` | hub reads/normalizes; never invents membership |
| Launch readiness | computed from repo files, git state, `aii-2 status` | fails closed; gates run launch |
| Task ids, summaries, status, blockers | target repo `.ai-tasks/index.md` + task frontmatter | read-only inventory; runtime overlays never replace task status |
| Run records and audit history | `<ORCH_HUB_DATA_DIR>/runs/` | run dirs stay outside target trees, fresh per run; `protocol.json` freezes the validated deployed commit/payload before spawn and records the bounded actual-file check at an observed end |
| Intake records and audit history | `<ORCH_HUB_DATA_DIR>/intake/` | unconfirmed drafts are hub-owned, not active tasks; no target-repo writes |
| Evaluation artifacts | `<ORCH_HUB_DATA_DIR>/evaluations/` | sidecar analysis state; never mutates tasks, runs, `.ai/`, `.ai-tasks/` |
| Evaluation report catalog | `<ORCH_HUB_DATA_DIR>/report-catalog/catalog.jsonl` | append-only publication identity/order + hashes/provenance; entries outlive artifacts; confirmed repair may remove only identity-preserving redundant/torn bytes after a durable non-overwriting backup; consumers decide protocol evolution |
| Agent subscription usage | Codex local rollouts + Claude local utilization cache + optional statusLine figures + Claude stream boundaries + hub `<data_dir>/usage/claude.json` | display-only; cache = primary Claude figure carrier; bounded model-free CLI refresh; statusLine = fallback; never gates |
| Human questions and answers | per-run control-dir files | answer text lives in answer files; history is metadata-only |
| Orchestrator logs | deployed orchestrator log dir, resolved from run records | clients never supply log paths |
| Startup diagnostics | hub-owned run dir capture | scoped to the persisted run record |
| Commit metadata | target repo git history | display/evidence only; never gates |
| Housekeeping marker | target repo `.ai/.housekeeping-pending` | display-only; never gates |
| Notifications | derived from run/question/intake/semantic-job state | best-effort delivery; must not affect state writes |

## Component Boundaries

- Registry discovery performs no readiness checks; readiness launches no
  orchestrators.
- Git metadata and housekeeping marker state are display-only; never gate
  launch.
- Task inventory reads task files but does not decide orchestrator turn state.
- Intake owns the draft/revise/confirm/cancel lifecycle, audit state, and
  read-only-until-confirm contract.
- Repo admission owns only the cross-kind invariant: one per-repo critical
  section admits either a run start or an intake start until the winner's
  persisted record exists. Run/intake managers retain their same-kind gates and
  file-backed occupancy truth; admission is not readiness.
- Evaluation owns evidence assembly/persistence; it reads target tasks/run
  artifacts and writes only hub-owned artifacts.
- Static metrics owns deterministic scoring over evidence packs; no model
  calls, no target/run mutation.
- Semantic evaluation owns model-judged reports over bounded evidence plus
  static metrics; read isolation is enforced in the backend, not by prompt
  alone.
- Semantic jobs own Layer 2 job state, duplicate in-flight prevention,
  failed/retryable diagnostics, and completed `evaluation_id` linkage — not
  evaluation evidence or target state.
- Report catalog owns cross-repo publication identity/order, reconciliation,
  provenance/hash manifests, bundle containment, byte-exact artifact
  verification reads, publication-debt retention protection, and explicit local
  audit/identity-preserving repair—not evaluation judgment or protocol-evolution
  policy.
- Evaluation API/dashboard presentation owns sidecar routing, readback, doctor
  gating, job polling, history comparison; never gates run control or inlines
  raw logs.
- Run control owns subprocess lifecycle, run records, same-repo serialization,
  and live/end classification.
- Control-dir handling owns only file-channel mechanics for questions,
  answers, and stop requests.
- History stores run/answer-audit metadata; it does not decide event timing.
- Logs are located from run records only; callers never pass local paths.
- Notification delivery is isolated from run/intake state persistence.
- Usage collection owns only display capacity data; it may run the Claude CLI's
  local `/usage` command only with per-turn zero-model accounting, but never
  starts a model turn, directly opens provider endpoints/credentials, scrapes
  UI, spends reset credits, or influences readiness/attention/run/intake
  decisions.
- API, CLI, dashboard, launchd, and future bot surfaces stay adapters over
  shared services.

## External Dependencies

- ai-native-development repository and its `aii-2` CLI.
- Managed target repos with deployed `.cursor/orchestrator/` payloads.
- Git CLI for repo state, display metadata, and evaluation commit windows.
- `/usr/bin/caffeinate` on macOS for long-running orchestrator subprocesses.
- Python venv inside each target repo's deployed orchestrator directory.
- FastAPI and uvicorn; launchd for resident operation.
- Optional Bark push endpoint.
- Claude CLI for intake, semantic evaluation, and model-free local `/usage`
  cache refreshes; Claude Code's structured utilization cache is the primary
  figure carrier.
- Claude Code statusLine stdin via legacy `orch-hub-statusline` as an optional
  fallback; 2.1.220 stream `rate_limit_event` as a passive boundary refresh.
- Codex local session rollouts for read-only rate-limit display; Codex is
  never invoked for usage.
- Tailscale or an equivalent private network.

