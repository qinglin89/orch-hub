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
a managed target repo is written. A completed Layer 1 + Layer 2 report of an
archived task is additionally published to a cross-repo
[global report feed](#the-global-report-feed) for external consumers.

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
- every other evaluation route — list, detail, evidence/metrics/semantic-job
  generation/status, the global report feed and bundle export, and the doctor —
  fails closed with `404`;
- no `<data_dir>/evaluations` or `<data_dir>/report-catalog` tree is created,
  and nothing is published;
- Run history hides its per-task `Evaluation →` actions, its state lights, and
  the `All evaluation reports →` entry, while the evaluation and report-feed
  routes render a short disabled note.

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
| `ORCH_HUB_EVAL_MODEL` | `claude-opus-5` | Model the **Layer 2** semantic evaluator drives through the Claude CLI. Only matters when evaluation is enabled and a semantic evaluation is requested; Layer 1 makes no model call. |
| `ORCH_HUB_EVAL_EFFORT` | `max` | Reasoning effort for that same Layer 2 turn (`--effort`). Unset/blank means the default. Not validated at startup — an effort the installed `claude` rejects fails that one semantic job, not the service. |
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
- built-in tools are disabled (`--tools ""`), so the model cannot open any repo
  file — no live `.ai/`, no raw logs, no secrets;
- it runs in safe mode with no session persistence and plan-mode writes blocked.

The evaluator does see the project's `.ai/` memory, but only as the **bounded
copy the evidence pack took** when the evaluation was generated — the eager set,
the lazy `prefetch:` docs the task's sessions declared, and the `.ai/`-scoped
patches for the run
window, each capped and carried inside `evidence.json`. That is what grounds the
`ai_freshness_utility` and `knowledge_signal_density` judgments. A live re-read
would be the opposite of this guarantee: it would drift from the persisted
artifact and make the report unreproducible.

So a semantic report is reproducible from its persisted evidence pack and cannot
have read live target-repo state outside the prompt. This is verified by tests
that assert the backend's argv/cwd isolation and that a semantic run leaves the
target `.ai-tasks/` untouched.

Each report records the evidence pack's shape version as `input_evidence_schema`.
When a pack gains an input the evaluator reasons over, the confidence baseline
moves, so reports are only directly comparable on confidence within one schema
version — compare cohorts, not raw numbers across a bump.

## Session-entry fidelity

While a run is live, the hub captures the task file **as each session entered
it** and keeps it in the run dir; the evidence pack carries those bytes per
session, and each report records `input_entry_fidelity` — `exact` when every
session the pack knows of has its entry file *whole*, `partial` when some do, and
`reconstructed` when none do. A file the pack's byte caps had to cut is carried
as a head and marked `truncated`, and it does not count as exact: what the cut
dropped is unknown rather than absent, so a case built from it cannot claim to
reproduce the session.

It matters because the archived task file cannot give the entry state back: the
estimate total is raised mid-flight, `prefetch:` is backfilled with what was
consulted, and Goal/Scope/Acceptance are adjusted as understanding sharpens. All
three losses run the same direction, so anything rebuilt from the archived file
describes a task that was better understood and better estimated than it was at
the time. Runs that ended before this capture existed stay `reconstructed`;
nothing backfills them, and the marker is there so work built on those reports
can weight them accordingly.

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
make room for an evaluation that did not complete. A prune is also skipped while
it would delete a complete report that is still waiting to be published to the
[global report feed](#the-global-report-feed) — see
[Retention, and the one thing that can halt publication](#retention-and-the-one-thing-that-can-halt-publication).

## The global report feed

Everything above is scoped to one `(repo, task)`. The feed is the other
direction: **one ordered, cross-repo stream of the completed reports the hub has
published**, so an external consumer — the intended one being a
protocol-evolution tool that reads finished tasks and proposes protocol changes —
can import incrementally without walking every repo, every task, and every
evaluation directory on each poll.

### Where the boundary is

orch-hub **publishes evaluation artifacts**. It does not decide protocol
evolution, does not rank repos or people, and does not create
protocol-improvement tasks. The feed hands over reports plus the provenance
needed to cohort them; what to conclude and what to change is entirely the
consumer's side of the line. Nothing in the feed writes to a target repo, and
listing or exporting cannot cause an unevaluated task to be evaluated (there is
no model call anywhere on these paths).

### What gets published, and when

Publication is **artifact-based and trigger-agnostic** — how an evaluation was
started is irrelevant. A report enters the catalog when both hold:

1. its evaluation directory holds the **whole** Layer 1 + Layer 2 set —
   `evidence.json`, `static_metrics.json`, `semantic_report.json`, `report.md` —
   durably on disk; and
2. its **task is archived** (completed and moved to `.ai-tasks/archive/`).

So a partial directory, an evidence-only or metrics-only evaluation, and a
failed semantic job are simply not candidates. A task evaluated while it was
still active is published later, once close-out archives it: the hub re-examines
uncataloged candidates on every reconciliation sweep. A report of a task that is
never archived is never published.

Publication happens on two paths, and they converge on the same identity: a hook
right after a semantic evaluation's artifacts are written (promptness), and a
reconciliation sweep at service start (correctness — it backfills whatever the
hook missed and picks up newly archived tasks). Reconciliation is idempotent:
restart it as often as you like, nothing is ever published twice.

The catalog is one append-only file, `<ORCH_HUB_DATA_DIR>/report-catalog/catalog.jsonl`.
Entries are immutable and never rewritten.

### Identity: `seq` and `report_key`

Each published report gets two identities, deliberately distinct:

- **`seq`** — the catalog's monotonic append counter. It carries the *order* and
  is the cursor. Scoped `evaluation_id` timestamps collide across repos, so they
  are useless for global ordering; `seq` is what a consumer traverses by.
- **`report_key`** — a stable, opaque digest of the source
  `(repo, task, evaluation)` triple. It is what a consumer stores and re-fetches
  by. It carries no ordering information on purpose, so no client can build
  ordering assumptions on it.

Both are unique across the catalog, and that uniqueness *is* the exactly-once
traversal guarantee.

### Reading the feed

```
GET /api/evaluation/reports?after=<seq>&limit=<n>
```

- `after` is the consumer's watermark — the highest `seq` it has already
  imported; omit it on the first poll. The page holds the entries strictly after
  it, in ascending `seq`.
- `next_cursor` is what to send as the next `after` (it is returned even for an
  empty page, so a consumer stores one number unconditionally), and `has_more`
  says whether more is available *right now*. **Never infer the end from a short
  page** — read `has_more`.
- `limit` is 1..500 (default 50). Out of range is a `400`, not a silent clamp.
- **Cursor policy, in one line:** a watermark must be a non-negative integer or
  the request fails with `400`. It is deliberately never quietly reset — resetting
  to the start re-imports everything, resetting to the end loses reports. A stale
  cursor past the end is simply an empty page.

Each item carries the source identity (repo/task/evaluation), the global
identity, generated and published timestamps, the archived-target facts, the
evaluator's backend/model/schema, the artifact names with their sizes and
sha256 digests, and provenance for cohorting (run ids, git commit window,
effective protocol revision).
Provenance the evidence pack does not carry is reported as **explicitly
unavailable with a reason** — today that is always the case for the
orchestrator `effective-config` snapshot, since evidence packs record it
nowhere.

`provenance.protocol.effective_revision` is the ai-native-development commit
whose deployed protocol the report's runs ran under — the cohort key for asking
whether a protocol change helped. Each run stamps its repo's
`.ai-deploy-lock.json` into its run dir before the orchestrator starts, and the
hub re-checks it when the run ends; a report publishes a revision only when
**every** contributing run verified the **same** one. Anything else — a run from
before the hub captured this, a deployment that moved mid-run, a run whose end
the hub was not up to observe, or runs that span a redeployment — is
`available: false` with a `detail` naming which of those it was. Nothing is ever
backfilled from a repo's current deployment lock, which by publication time
usually names a later revision than the runs used.

### Fetching one report

```
GET /api/evaluation/reports/{report_key}
```

Returns the entry plus the four artifacts in the same parsed shapes the per-task
detail route uses, so a consumer never needs to know the
`<repo-slug>/<task-id>/<evaluation-id>/` layout.

- `available` is **all-or-nothing**: the whole set must still be on disk and
  readable.
- A report whose artifacts a retention prune has since removed answers
  `200 {"available": false, "detail": ...}` — *not* `404`. The report was
  published and its entry stays auditable; "no such report" would misreport it.
  An unknown key is the `404`.
- Each artifact is **opened without following a link at its own hop**, and the
  bytes are parsed from that open file — the same rule as the byte-exact route
  below, so an artifact replaced after publication by a link out of the tree is
  refused rather than exported, and a swap landing while the read is in flight
  changes nothing. That answers `available: false` with a *damaged* detail,
  deliberately worded apart from the pruned and partially pruned ones: retention
  removing artifacts is this feed's designed end state, and reporting a tampered
  tree as that end state would hide it. (Symlinks *above* the evaluation dir — a
  relocated data volume — are unaffected.) The same rule decides publication: a
  dir holding a linked artifact is not a complete report, so its digest is never
  recorded over an outside file, and a sweep publishes it only once repaired.
- `integrity` reports which served artifacts no longer hash to the digest
  recorded at publication (`mismatched`) and which the entry has no usable
  digest for (`unverified`). Neither blocks the download — only the consumer can
  decide what an edited or uncheckable report means for its import — and
  `verified` is the strict summary: every artifact checked *and* matched.

The artifacts here are **parsed**, so this route is for importing a report, not
for checking one: to recompute a digest yourself, fetch the bytes instead (next
section).

### Verifying one artifact byte-for-byte

```
GET /api/evaluation/reports/{report_key}/artifacts/{name}
```

`name` is one of the four published artifact filenames — `evidence.json`,
`static_metrics.json`, `semantic_report.json`, `report.md`. The body is that file
**exactly as persisted**: no parsing, no re-encoding, no normalization.

That is the difference from the bundle route above, and the reason this route
exists. The bundle parses each artifact and the JSON response re-serializes it,
so the bytes a consumer receives there are *this service's* formatting — key
order, escaping, separators, trailing newline — and re-hashing them proves
nothing about what was published. Hashing **this** body reproduces the `sha256`
the feed entry recorded, and its length is the recorded `size`.

- Availability is **per artifact**, not per set: a surviving artifact beside a
  pruned sibling is served, because checking one file's digest needs nothing from
  the other three. (The bundle is all-or-nothing because a half-populated
  *import* would be taken for a whole report; one file has no such failure mode.)
- Bytes that no longer hash to the recorded digest are **still served**. The body
  must stay byte-exact, so the verdict travels in headers:
  `X-Report-Artifact-Integrity` is `verified` / `mismatched` / `unverified`, and
  `X-Report-Artifact-Sha256` echoes the recorded digest when the entry holds a
  usable one. Withholding an edited artifact would withhold the evidence of the
  edit from the one caller equipped to prove it.
- Status codes carry what the bundle says in its envelope, since a raw body has
  none:

| Case | Status |
|---|---|
| served — including `mismatched` and `unverified` | `200` |
| unknown `report_key` | `404` |
| `name` is not one of the four published artifacts | `404` |
| stored identity is not the one the report was published under | `409` |
| published, artifact removed by retention | `410` |
| the file is present and will not read, or a symlink stands where it should | `500` |
| the catalog holds a duplicate `seq`/`report_key` | `500` |
| evaluation disabled / missing or wrong bearer token | `404` / `401` |

`410` rather than `404` for a pruned artifact is deliberate: the entry proves the
report was published, so "gone" and "wrong key" must be distinguishable without
parsing prose. A symlinked artifact is `500` rather than `410` for the
mirror-image reason: it is a damaged artifact tree, and reporting it as "pruned"
would present tampering as retention's designed end state.

A traversal attempt reads nothing — `name` selects from a closed set and is never
a path, and an escaped separator matches no single path segment in the first
place. Nor can the *filesystem* widen the boundary: because the four names are
fixed but the files standing under them are not, the artifact is **opened without
following a link at its own hop**, and the bytes are then read from that open
file — so an artifact replaced by a symlink out of the tree is refused rather
than followed, and a swap landing while the read is in flight changes nothing
about what the response carries. (Symlinks *above* the evaluation dir — a
relocated data volume — are unaffected.) Nothing on this path opens a target
repo, publishes, evaluates, or prunes.

```
curl -fsS -H "Authorization: Bearer $ORCH_HUB_TOKEN" \
  "$HUB/api/evaluation/reports/$KEY/artifacts/evidence.json" | shasum -a 256
```

### Retention, and the one thing that can halt publication

Retention (above) and the feed are coordinated: a prune is **skipped** while it
would delete a complete report that is publishable but not yet cataloged, so a
failed publication can always be recovered by the next sweep. Once published, an
entry outlives its artifacts by design — it stays in the feed as an auditable
record of a report that once existed, with `available: false` on fetch.

Because the catalog is append-only and never rewritten, the only way to break it
is to edit, merge, or restore the file by hand. If that leaves two entries
sharing a `seq` or a `report_key`, both feed routes answer `500` naming the
offending value and publication pauses (retention also stops pruning meanwhile,
so nothing is lost) until the file is repaired. A damaged catalog is inert, not
dangerous.

### From the dashboard

Run history carries an `All evaluation reports →` link on evaluation-enabled
hubs. The view lists the feed in its global publication order, showing each
report's sequence, repo/task, evaluator identity, Layer 1 + Layer 2
completeness, run/commit provenance, and global key, and pages forward and back
through the same cursor an external consumer uses. Every row opens the existing
per-task evaluation view at that exact evaluation, so the per-task history
picker and artifact affordances remain the report detail — the central view adds
a way in, it does not replace them.

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
  "semantic": { "backend": "claude-cli", "model": "claude-opus-5",
                "schema_version": "1", "available": true,
                "detail": "claude resolved at /usr/local/bin/claude" } }
```

Enabled-gated like every action route: a disabled hub answers `404`. The
dashboard calls this on the evaluation view and, when the backend is
unconfigured (`available: false`), disables the Layer 2 control with an
explanation while keeping the free Layer 1 metrics usable — so an unconfigured
backend is surfaced up front instead of as an error after a click.

## Using it from the dashboard

Open a task's evaluation view from the per-task `Evaluation →` link in Run
history. The adjacent status light shows the newest evaluation at a glance:
grey means not evaluated, amber means L1, and green means L1 + L2; the exact
state is also exposed in the link's hover title and screen-reader label. These
states are batched into `GET /api/history/runs`; the dashboard does not fan out
one evaluation request per task group. Active Task detail deliberately has no
evaluation entry. The evaluation view:

- resolves the task active-then-archive, so a completed/archived task stays
  evaluable;
- **Generate metrics** runs the free deterministic Layer 1 refresh;
- **Run semantic evaluation** starts or reuses the async Layer 2 job (shown with
  an up-front cost/read-only callout; disabled with an explanation when the
  doctor reports the backend is unavailable), then shows reload-survivable
  running, failed/retry, or completed-selected-report state;
- renders Layer 1 and Layer 2 as visually and semantically distinct sections, so
  you can tell deterministic metrics from model judgments;
- offers an evaluation-history picker (newest-first) that names each semantic
  run's evaluator model/version for comparison, and Copy/Download affordances for
  the raw evidence/metrics/semantic JSON and the `report.md` Markdown export. Raw
  run logs are never rendered inline.

For the cross-repo view of everything that has been *published*, Run history also
links to `All evaluation reports →`; see
[the global report feed](#the-global-report-feed).

## HTTP surface

All routes are protected by the bearer token like every non-health route.

| Route | Purpose |
|---|---|
| `GET /api/evaluation` | Availability — the only evaluation route that answers (`200 {enabled}`) while disabled. |
| `GET /api/evaluation/doctor` | Layer 2 readiness probe (no model call). `404` when disabled. |
| `GET /api/evaluation/reports` | One cursor-paginated page of the [global report feed](#the-global-report-feed), oldest-published first. |
| `GET /api/evaluation/reports/{report_key}` | One published report's import bundle by global key; pruned artifacts answer `200 {"available": false}`. |
| `GET /api/evaluation/reports/{report_key}/artifacts/{name}` | One published artifact's [byte-exact persisted content](#verifying-one-artifact-byte-for-byte) for independent digest verification; pruned answers `410`. |
| `GET /api/history/runs` | One page of run history plus batched newest-evaluation state (`none`/`l1`/`l1_l2`) per distinct task group on that page. |
| `GET /api/repos/{repo}/evaluations/{task}` | List a task's evaluations (newest-first) + resolved target state. |
| `GET /api/repos/{repo}/evaluations/{task}/{id}` | Read one evaluation's evidence/static/semantic/`report.md`. |
| `POST /api/repos/{repo}/evaluations/{task}/evidence` | Generate a bounded evidence pack only. |
| `POST /api/repos/{repo}/evaluations/{task}/metrics` | Generate evidence + Layer 1 static metrics. |
| `POST /api/repos/{repo}/evaluations/{task}/semantic/job` | Start or reuse the current async Layer 2 job; duplicate in-flight starts return the same job. |
| `GET /api/repos/{repo}/evaluations/{task}/semantic/job` | Read the current Layer 2 job (`queued`/`running`/`completed`/`failed`; `null` when none exists). |

When disabled, every route except `GET /api/evaluation` answers `404` with the
fail-closed reason.
