# Task evaluation (optional module)

orch-hub can score the quality of a task's dev/review process from its task
file, run history, and logs. This is an **optional sidecar**: it is bolted on
beside run control and can be disabled or excluded entirely without affecting
repos, tasks, run history, run logs, Start/Stop, or control-dir human input.

Read this if you operate the hub and want to turn evaluation on or off, control
how much history it keeps, or understand what the semantic evaluator is allowed
to touch.

## What it is (and is not)

Task evaluation produces read-only **analysis artifacts** about a task — it
never changes the task, its run records, `.ai/`, or `.ai-tasks/`. It has two
layers over the same bounded evidence:

| Layer | Name | Cost | What it is |
|---|---|---|---|
| 1 | Static metrics | Free, instant, no model | Deterministic protocol/reliability/handoff/estimate/knowledge scores computed from the task file and run history. |
| 2 | Semantic evaluation | A model call (real cost, slow) | A Claude model's judgment of what a deterministic check cannot see — knowledge-signal density, handoff/decision-trace completeness, acceptance coverage, protocol soft-failures — with scored axes, findings, and suggested improvements. |

Both are generated on demand and persisted as hub-owned artifacts under
`<ORCH_HUB_DATA_DIR>/evaluations/<repo>/<task>/<evaluation-id>/`. Nothing under
a managed target repo is written.

It is **not**:

- a gate on anything — evaluator readiness never blocks Start, Stop, run logs,
  or control-dir answers;
- a task run — a semantic evaluation is a single read-only model turn, not an
  orchestrator run, and it holds no repo run slot;
- a live inspector — the model sees only the persisted evidence pack, never the
  live repo.

## The module boundary

Core orch-hub (discovery, readiness, run control, history, logs, intake,
notifications) works with evaluation **disabled or removed**. When the
subsystem is off:

- `GET /api/evaluation` still answers `200 {"enabled": false}` so the dashboard
  can hide its controls with a concise explanation instead of probing a dead
  route;
- every other evaluation route — list, detail, evidence/metrics/semantic
  generation, and the doctor — fails closed with `404`;
- no `<data_dir>/evaluations` tree is created;
- the dashboard hides the "Task evaluation" entry links on task detail and in
  run history, and the evaluation route itself renders a short disabled note.

This boundary is covered by tests
(`tests/test_api.py::test_disabling_evaluation_leaves_core_run_control_intact`
and the disabled-mode evaluation tests), so a customer deployment can ship with
`ORCH_HUB_EVAL_ENABLED=false` and rely on the core surface staying intact.

## Configuration

All evaluation settings are environment variables (the same surface as the rest
of the hub — for the launchd service they live in the plist's
`EnvironmentVariables` dict).

| Variable | Default | Purpose |
|---|---|---|
| `ORCH_HUB_EVAL_ENABLED` | `true` (enabled) | Master switch. A falsy token (`0`/`false`/`no`/`off`, case-insensitive) disables the whole subsystem. Unset/blank means the default; an unrecognized value fails startup rather than guessing. |
| `ORCH_HUB_EVAL_MODEL` | `claude-opus-4-8` | Model the **Layer 2** semantic evaluator drives through the Claude CLI. Only matters when evaluation is enabled and a semantic evaluation is requested; Layer 1 makes no model call. |
| `ORCH_HUB_EVAL_RETENTION` | `20` | Per-task cap on retained evaluations (see [Retention](#retention)). `0` disables retention (keep everything); a positive integer is the cap. A negative or non-integer value fails startup. |

To exclude evaluation from a deployment, set `ORCH_HUB_EVAL_ENABLED=false`. No
other change is required — the routes and dashboard controls self-disable.

## Read-only guarantee

Evaluation only ever writes hub-owned artifacts under
`<ORCH_HUB_DATA_DIR>/evaluations/`. The target repo's task file, `.ai-tasks/`,
`.ai/`, and run records are never modified by any evaluation path. Evidence and
static-metrics generation are plain file reads.

The **semantic** evaluator's isolation is enforced structurally in the backend,
not by asking the model nicely:

- the Claude CLI turn runs from a fresh **empty, non-repo** working directory —
  even the cwd path and a git-status snapshot reflect no target-repo state;
- built-in tools are disabled (`--tools ""`), so the model cannot open `.ai/`,
  raw logs, secrets, or any repo file;
- it runs in safe mode with no session persistence and plan-mode writes blocked.

So a semantic report is reproducible from its persisted evidence pack and cannot
have read live target-repo state outside the prompt. This is verified by tests
that assert the backend's argv/cwd isolation and that a semantic run leaves the
target `.ai-tasks/` untouched.

## Retention

Evaluations accumulate one directory per generation. `ORCH_HUB_EVAL_RETENTION`
bounds how many of the **newest** evaluations are kept per task; older ones are
pruned automatically on the next generation, so the tree stays bounded while the
freshest history is kept for trend analysis.

- Default `20` per task.
- `0` disables retention (unbounded — keep every evaluation).
- Pruning is scoped strictly to one `(repo, task)` subtree and only ever deletes
  the subsystem's own artifacts.

Retention runs **only after** a generation's whole artifact set is durable, so a
failed static or semantic generation never deletes older retained history to
make room for an evaluation that did not complete.

## The evaluator doctor (smoke check)

The doctor answers "is Layer 2 runnable, and how?" without spending anything: it
is a configuration/availability probe (does the `claude` CLI resolve on PATH?),
not a model call. It never gates run control.

**CLI:**

```
orch-hub eval doctor
```

Prints whether evaluation is enabled, the backend/model/schema version, the
retention setting, and whether the backend is available (with the resolved path
or the not-found reason). Exit code is `0` only when evaluation is enabled **and**
the backend resolves, so a script can detect "Layer 2 not ready"; a disabled
subsystem or an unresolved CLI exits non-zero.

**HTTP:** `GET /api/evaluation/doctor`

```json
{ "enabled": true,
  "semantic": { "backend": "claude-cli", "model": "claude-opus-4-8",
                "schema_version": "1", "available": true,
                "detail": "claude resolved at /usr/local/bin/claude" } }
```

Enabled-gated like every action route: a disabled hub answers `404`. The
dashboard calls this on the evaluation view and, when the backend is
unconfigured (`available: false`), disables the Layer 2 control with an
explanation while keeping the free Layer 1 metrics usable — so an unconfigured
backend is surfaced up front instead of as an error after a click.

## Using it from the dashboard

Open a task's evaluation view from the "Task evaluation" link on task detail
(active tasks) or the per-task "Evaluation" link in run history (archived tasks).
The view:

- resolves the task active-then-archive, so a completed/archived task stays
  evaluable;
- **Generate metrics** runs the free deterministic Layer 1 refresh;
- **Run semantic evaluation** runs Layer 2 (shown with an up-front cost/read-only
  callout; disabled with an explanation when the doctor reports the backend is
  unavailable);
- renders Layer 1 and Layer 2 as visually and semantically distinct sections, so
  you can tell deterministic metrics from model judgments;
- offers an evaluation-history picker (newest-first) that names each semantic
  run's evaluator model/version for comparison, and Copy/Download affordances for
  the raw evidence/metrics/semantic JSON and the `report.md` Markdown export. Raw
  run logs are never rendered inline.

## HTTP surface

All routes are protected by the bearer token like every non-health route.

| Route | Purpose |
|---|---|
| `GET /api/evaluation` | Availability — the only evaluation route that answers (`200 {enabled}`) while disabled. |
| `GET /api/evaluation/doctor` | Layer 2 readiness probe (no model call). `404` when disabled. |
| `GET /api/repos/{repo}/evaluations/{task}` | List a task's evaluations (newest-first) + resolved target state. |
| `GET /api/repos/{repo}/evaluations/{task}/{id}` | Read one evaluation's evidence/static/semantic/`report.md`. |
| `POST /api/repos/{repo}/evaluations/{task}/evidence` | Generate a bounded evidence pack only. |
| `POST /api/repos/{repo}/evaluations/{task}/metrics` | Generate evidence + Layer 1 static metrics. |
| `POST /api/repos/{repo}/evaluations/{task}/semantic` | Generate the full Layer 2 chain (evidence + static + semantic + `report.md`). |

When disabled, every route except `GET /api/evaluation` answers `404` with the
fail-closed reason.
