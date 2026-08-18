---
last-updated: 2026-08-18
verified-against: 29e4c53412a0960cba31118549551de691b1af70
---

# Project Overview

## Purpose

orch-hub is intended to be a long-lived local control plane for AI coding orchestrators. It will run on the user's Mac and allow remote control from iPhone, iPad, or another machine over a private network. The hub coordinates existing per-repository orchestrator programs that drive dev/review loops over `.ai-tasks/`.

The implemented slices are discovery/readiness, task inventory, the FastAPI
HTTP foundation, fail-closed bearer-token auth, environment-configured service
binding, launchd service packaging/docs, the control-dir file-channel adapter,
the run manager, run/escalation HTTP routes, log viewing, run history/audit,
the mobile dashboard, display-only agent subscription usage, Bark
notifications, remote intake-task, the task-evaluation foundation,
deterministic static task metrics, the optional semantic task evaluator, and
the global evaluation-report feed:
read the
mandrel local registry, list
managed repos, report launch
readiness, parse each repo's `.ai-tasks/` index plus task frontmatter, expose
those payloads through HTTP, require `ORCH_HUB_TOKEN` on every non-health HTTP
path, provide a launchd user-agent template for resident macOS operation,
read/write orchestrator question, answer, and stop files, launch/list/stop/
reattach orchestrator subprocess runs with per-repo serialization, answer
waiting questions remotely, retrieve or stream run logs, persist run lifecycle
plus answer-audit metadata, serve the dashboard, push Bark notifications for
pending questions, terminal run states, and intake drafts/revisions ready for
review, and fresh completed/failed semantic-evaluation jobs, and run a confirmed
remote intake loop that drafts a task before writing it to the target repo,
generate bounded read-only task evidence packs
under the hub data dir for active or archived tasks, compute deterministic
protocol/reliability/handoff/estimate/knowledge-maintenance/prefetch-reading
static reports as
`static_metrics.json` beside the evidence pack, run an evidence-bounded Claude
CLI semantic evaluator through persisted async job records that prevent
duplicate in-flight model calls and link completed jobs to `semantic_report.json`
plus `report.md`, and expose evaluation evidence/static/semantic artifacts
through protected HTTP routes. Complete L1+L2 reports for archived tasks are
published to one append-only, globally ordered catalog with stable keys,
cursor-paged discovery, contained bundle export, and byte-exact per-artifact
verification reads. Report protocol provenance is the unanimous atomic pair of
source commit plus deploy-lock payload digest, backed per run by actual deployed
file checks at launch and observed end; reconciliation heals missed publication
without re-running the evaluator. The dashboard renders
Claude/Codex subscription
usage as a separate Overview capacity section over protected `GET /api/usage`:
Codex is read from local session rollout `rate_limits`; Claude reads a
hub-owned observation store primarily fed from Claude Code's structured local
utilization cache. Stale reads and terminal runs request an asynchronous,
single-flight, timeout-bounded `claude -p "/usage"` refresh whose JSON envelope
must prove zero model turns/cost; the operator-installed statusLine sink remains
an optional fallback. Hub-driven Claude Code 2.1.220 stream events can refresh
a stored window's reset boundary without creating a window or changing figures;
no verified stream carries a used-percentage. Rolled-over windows render as no
active window rather than presenting retained figures as current. Missing,
stale, or failing data is explicit display state that never affects readiness,
attention, run state, or launch/intake gates. The dashboard renders the
task-evaluation sidecar with
Layer 1 static metrics, doctor-gated Layer 2 semantic job
running/failed/completed controls/report rendering, history model/version
comparison, and artifact copy/download affordances; later slices add IM bot
control. A central report view preserves the catalog's global publication order
and links back to existing per-task detail.

## Users

- Primary user: qinglin, operating local AI coding workflows from mobile devices or another computer.
- Remote clients: mobile-first web dashboard on iPhone/iPad; later possible IM bot surfaces.
- Local service operator: launchd on the Mac, keeping the hub resident.

## Scope

- Discover managed repos from mandrel, not from GitHub and not from a separate
  orch-hub registry.
- Inspect each managed repo for orchestrator launch readiness.
- List active, waiting, pending, and blocked tasks per repo.
- Launch and supervise one orchestrator run per repo at a time, each run attached to one `.ai-tasks/<task-id>.md` task.
- Mutually exclude live orchestrator runs and non-terminal hub-driven remote
  intakes per repo through one atomic admission coordinator; keep admission
  separate from readiness.
- Surface orchestrator human input requests remotely, collect answers, and deliver them back through the implemented control-dir file channel.
- Stream or tail orchestrator logs to the dashboard.
- Persist run lifecycle, stop request, and answer-audit metadata for later inspection without duplicating human answer text into history.
- Push mobile notifications for pending questions, terminal run states, and
  intake drafts/revisions ready for review, and fresh completed/failed
  semantic-evaluation jobs via Bark when `ORCH_HUB_BARK_URL` is configured.
- Draft, revise, confirm, or cancel new task proposals remotely while keeping
  target repo `.ai-tasks/` files unchanged until explicit confirmation.
- Generate bounded, read-only evidence packs for active or archived tasks from
  task files, related run records/events/sessions/log diagnostics, best-effort
  git commit windows, `.ai/` snapshots/patches, and transcript-derived ordered
  session memory-read behavior without retaining raw transcripts; protocol
  evidence never backfills from current target files or receipts.
- Compute deterministic static task-quality reports from evidence packs,
  including run/session rollups, taskfile/session-log protocol metrics, six
  score axes (including prefetch-set read completeness), evidence-backed
  warnings, and versioned `static_metrics.json` persistence in the hub-owned
  evaluation artifact directory.
- Run optional semantic task evaluations from the same bounded evidence/static
  chain, including retrieval effectiveness over ordered `.ai/` reads and
  evidence-grounded rediscovery risk, while invoking Claude CLI from an isolated
  empty cwd with tools disabled so reports are reproducible from persisted
  artifacts and cannot read target repo files outside the prompt.
- Show Claude and Codex subscription usage as display-only capacity data:
  provider windows, used/remaining percentages, reset time, freshness, and
  unavailable/stale reasons, without starting model turns, directly opening
  credentials/provider endpoints, scraping UI, consuming reset credits, or
  influencing operational gates. Codex is local rollout data; Claude's primary
  carrier is the CLI's whitelisted structured utilization cache, refreshed by
  its local `/usage` command only when the turn reports zero model activity.
  The opt-in statusLine sink remains compatible, and percentage-less 2.1.220
  stream events may refresh stored reset boundaries.
- Expose task evaluation as a sidecar HTTP workflow: availability, evidence
  generation/readback, static metrics generation/readback, semantic generation/
  readback, semantic doctor readiness, active or archived task targeting,
  history selection with evaluator model/version identity, retention, and
  artifact inspection without target-repo mutation.
- Publish archived tasks' complete L1+L2 reports through one durable cross-repo
  feed with global order/identity, explicit artifact integrity/provenance
  (including the unanimous source-revision/payload-digest protocol pair), and
  contained bundle plus byte-exact artifact retrieval. orch-hub publishes;
  external consumers decide protocol evolution and any resulting improvement
  work.
- Provide simple remote auth suitable for a Tailscale-only v1 deployment.
- Provide a mobile-first dashboard for repo, task, run, question, log, intake,
  and task-evaluation views.
- Support launchd-based long-lived operation on macOS.

## Non-Goals

- Do not treat GitHub as the registry source of truth.
- Do not maintain a separate orch-hub repository registry.
- Do not import `.cursor/orchestrator/orchestrator.py`; launch it as a subprocess.
- Do not add web, network, or orch-hub imports into the orchestrator itself.
- Do not make the service public on the internet.
- Do not implement watchdogs or hang/log-silence detection.
- Do not run more than one orchestrator at a time in the same target repo.
- Do not treat unconfirmed intake drafts as active tasks or expose them through
  task inventory.
- Do not hand-edit deployed protocol/orchestrator copies in target repos;
  canonical changes belong in mandrel.
- Do not treat subscription usage as repo health or launch readiness, and do
  not start model turns solely to refresh quota data.
- Do not rank global evaluation reports, decide protocol evolution, or create
  protocol-improvement tasks from the publication path.

## Tech Stack

- Language: Python.
- First implementation: package plus CLI, using pytest tests and stdlib subprocess/path/json primitives where practical.
- HTTP service: FastAPI and uvicorn, with a localhost-default entry point,
  environment-configured host/port, and fail-closed bearer-token middleware.
- UI: mobile-first web dashboard served by the hub.
- Local deployment: launchd user-agent template and docs on macOS.
- Network boundary: Tailscale for private connectivity, with a simple bearer token in v1.
- Notifications: Bark is the implemented v1 push path; IM bots are later.
- External command/data sources: `mandrel` from the mandrel checkout; Claude CLI
  for remote intake, semantic evaluation, and model-free local `/usage`
  refreshes; Claude's local utilization cache; local Codex session rollout
  files for read-only rate-limit display.

## Repository Structure

The repository contains the first product package and tests:

```text
orch_hub/
  __init__.py
  admission.py
  api.py
  cli.py
  config.py
  evaluation.py
  history.py
  intake.py
  intake_agent.py
  models.py
  registry.py
  readiness.py
  runs.py
  semantic.py
  static_metrics.py
  usage.py
  logs.py
  notifications.py
  subprocess_utils.py
  task_store.py
  control_dir.py
  web/
tests/
  test_api.py
  test_cli.py
  test_config.py
  test_control_dir.py
  test_history.py
  test_intake.py
  test_intake_agent.py
  test_logs.py
  test_registry.py
  test_readiness.py
  test_runs.py
  test_task_store.py
  test_usage.py
deploy/
  launchd/
    com.qinglin89.orch-hub.plist
docs/
  launchd.md
```

## External Project Facts

- Mandrel root: `/Users/linqing/workplace/github.com/qinglin89/mandrel`.
- Default registry source is `<mandrel>/bin/mandrel registry list --json`;
  fallback is `<mandrel>/.registry/repos.local.json`.
- The canonical orchestrator lives at
  `mandrel/canonical/orchestrator/orchestrator.py` and is deployed into target
  repos at `.cursor/orchestrator/orchestrator.py`.
- The inspected orchestrator is a local CLI scheduler. It runs a dev/review loop over one `.ai-tasks/` task, selects dev or review turns from task file status and session log, pauses for human input through `ask_human`, and uses stdin unless launched with `--control-dir`.
