---
last-updated: 2026-09-03
verified-against: 4d6e1a3b7c31a073417b9b681d5a009e288b71cf
---

# Architecture: Ownership and Boundaries

Data ownership, component boundaries, and external dependencies. Shape and
invariants: `index.md`. Runtime flows: `flows.md`.

## Data Ownership

| Data | Source Of Truth | Architecture Rule |
|---|---|---|
| Managed repos | mandrel registry / `mandrel` | hub reads/normalizes; never invents membership |
| Launch readiness | computed from repo files, git state, `mandrel status` | fails closed; gates run launch |
| Current repo protocol placement | `mandrel status --json` canonical-identity reading | hub relays opaque line/finding per request for display; never derives from git, receipt, manifest, revision, or frozen run provenance |
| Task ids, summaries, status, blockers | target repo `.ai-tasks/index.md` + task frontmatter | read-only inventory; runtime overlays never replace task status |
| Run records and audit history | `<ORCH_HUB_DATA_DIR>/runs/` | run dirs stay outside target trees, fresh per run; `protocol.json` freezes the validated deployed commit/payload before spawn and records the bounded actual-file check at an observed end |
| Intake records and audit history | `<ORCH_HUB_DATA_DIR>/intake/` | unconfirmed drafts are hub-owned, not active tasks; no target-repo writes |
| Evaluation artifacts | `<ORCH_HUB_DATA_DIR>/evaluations/` | sidecar analysis state; never mutates tasks, runs, `.ai/`, `.ai-tasks/` |
| Semantic job records | `<ORCH_HUB_DATA_DIR>/semantic-jobs/` | one latest explicit attempt/outcome per `(repo, task)`; whole-attempt deadline and cancellation state; legacy deferred/skipped records remain readable but inert |
| Evaluation report catalog | `<ORCH_HUB_DATA_DIR>/report-catalog/catalog.jsonl` | append-only publication identity/order + hashes/provenance; entries outlive artifacts; confirmed repair may remove only identity-preserving redundant/torn bytes after a durable non-overwriting backup; consumers decide protocol evolution |
| Agent subscription usage | Codex local rollouts + Claude local utilization cache + optional statusLine figures + Claude stream boundaries + hub `<data_dir>/usage/claude.json` | display-only; cache = primary Claude figure carrier; bounded model-free CLI refresh; statusLine = fallback |
| Human questions and answers | per-run control-dir files | answer text lives in answer files; history is metadata-only |
| Orchestrator logs | deployed orchestrator log dir, resolved from run records | clients never supply log paths |
| Startup diagnostics | hub-owned run dir capture | scoped to the persisted run record |
| Commit metadata | target repo git history | display/evidence only; never gates |
| Housekeeping marker | target repo `.ai/.housekeeping-pending` | display-only; never gates |
| Notifications | derived from run/question/intake/semantic-job state | best-effort delivery; must not affect state writes |
| Installed runtime | selected user prefix with version directories, `current`/`previous`, and payload-bound ownership marker | installer-owned immutable program bytes; mutable hub data stays outside; uninstall refuses unproven prefixes |
| Release artifacts | local `build/release/`, `dist/release/`, and immutable per-tag publication destination | producer-owned generated state; validate staged bytes before canonical replace, and never overwrite a published tag |

## Component Boundaries

- Registry discovery performs no readiness checks; readiness launches no
  orchestrators.
- Readiness may carry mandrel's protocol placement from its existing status
  subprocess as a display-only passenger; only the exit code gates, and the hub
  must not compute a second placement.
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
- Completion evaluation owns the isolated run-end sidecar trigger: automatic
  Layer 1 after run evidence settles, with no semantic-job dependency or durable
  debt; it cannot change run state, readiness, attention, or admission.
- Semantic jobs own the one-record-per-task explicit Layer 2 lifecycle,
  single-flight guard, enforced deadline/reaping, cancellation and completed
  `evaluation_id` linkage — not evaluation evidence or target state.
- Report catalog owns cross-repo publication identity/order, reconciliation,
  provenance/hash manifests, bundle containment, byte-exact artifact
  verification reads, publication-debt retention protection, and explicit local
  audit/identity-preserving repair—not evaluation judgment or protocol-evolution
  policy.
- Evaluation API/dashboard presentation owns sidecar routing, readback, doctor
  gating, job polling/cancel, cross-repo attempt projection, and history
  comparison; never gates run control or inlines raw logs.
- Run control owns subprocess lifecycle, run records, same-repo serialization,
  and live/end classification.
- Control-dir handling owns only file-channel mechanics for questions,
  answers, and stop requests.
- History stores run/answer-audit metadata; it does not decide event timing.
- Logs are located from run records only; callers never pass local paths.
- Notification delivery is isolated from run/intake state persistence.
- Usage collection owns display data; it may run the Claude CLI's
  local `/usage` command only with per-turn zero-model accounting, but never
  starts a model turn, directly opens provider endpoints/credentials, scrapes
  UI, spends reset credits, or influences readiness/attention/run/intake
  or evaluation decisions.
- API, CLI, dashboard, launchd, and future bot surfaces stay adapters over
  shared services.
- Runtime resource lookup is the only source/frozen filesystem abstraction;
  installed-runtime commands consume the selected versioned prefix and do not
  depend on a checkout.
- Release build/audit/package/publish modules are producer tooling. They may
  inspect source and generated artifacts but do not own installed runtime state.
- `atomic_dir` is a last-good replacement primitive. Immutable publication uses
  explicit create-only placement because replacing an existing tag is invalid.

## External Dependencies

- Mandrel repository and its repo-local `bin/mandrel` CLI.
- Managed target repos with deployed `.mandrel/orchestrator/` payloads.
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
- Pinned PyInstaller and python-build-standalone CPython for release production;
  neither is a dependency of an installed runtime.
- POSIX shell and standard macOS tools for the self-extracting installer; Git
  and optionally `gh` establish the publication tag gate.
