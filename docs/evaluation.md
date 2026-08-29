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

Both run automatically when a task reaches `completed`
([Automatic evaluation](#automatic-evaluation-of-completed-tasks)), and both can
also be generated on demand at any time. Either way they are persisted as
hub-owned artifacts under
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
| `ORCH_HUB_EVAL_AUTO` | `true` (on) | Whether a task that reaches `completed` is evaluated automatically (see [Automatic evaluation](#automatic-evaluation-of-completed-tasks)). Falsy disables *only* the trigger — every manual route, control, and artifact stays exactly as it was. Subordinate to `ORCH_HUB_EVAL_ENABLED`: a disabled subsystem has nothing to trigger. |
| `ORCH_HUB_EVAL_MODEL` | `claude-opus-5` | Model the **Layer 2** semantic evaluator drives through the Claude CLI. Only matters when evaluation is enabled and a semantic evaluation is requested; Layer 1 makes no model call. |
| `ORCH_HUB_EVAL_EFFORT` | `max` | Reasoning effort for that same Layer 2 turn (`--effort`). Unset/blank means the default. Not validated at startup — an effort the installed `claude` rejects fails that one semantic job, not the service. |
| `ORCH_HUB_EVAL_RETENTION` | `20` | Per-task cap on retained evaluations (see [Retention](#retention)). `0` disables retention (keep everything); a positive integer is the cap. A negative or non-integer value fails startup. |
| `ORCH_HUB_EVAL_USAGE_CEILING` | `80` | Subscription utilization (percent) at or above which an **automatic** Layer 2 turn waits instead of running (see [When an automatic Layer 2 turn actually runs](#when-an-automatic-layer-2-turn-actually-runs)). Compared against the most exhausted currently-measured window. Only the automatic path consults it; a manual evaluation is never capacity-gated. A value outside 0-100, or a non-number, fails startup. |
| `ORCH_HUB_EVAL_DISPATCH_SPACING` | `900` (15 min) | Minimum seconds between two automatic Layer 2 turns, measured from the last turn admitted to spend (manual turns included). `0` drains the queue as fast as capacity allows. A negative or non-integer value fails startup. |
| `ORCH_HUB_EVAL_QUEUE_MAX_AGE` | `21600` (6 h) | How long a job may wait for capacity before the hub logs it as a stalled queue and flags it on the dashboard (see [Seeing the queue](#seeing-the-queue)). Visibility only — it changes no dispatch decision. `0` disables the flag. A negative or non-integer value fails startup. |

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

## Automatic evaluation of completed tasks

By default nobody decides which completed tasks get a report: when a run's own
evidence says it finished its task, the hub evaluates that task on the spot.
This exists to keep the evaluated corpus from being *selected* — an operator who
evaluates the tasks that felt problematic produces a corpus that cannot support
recurrence or base-rate claims, however carefully it is later consumed.

What fires, and when:

- The trigger is the run manager's live→ended transition, and only for a run
  whose end classifies **completed** — the same verdict the dashboard shows,
  read from that run's own frozen run-dir evidence.
- **Layer 1 needs no queue.** It is deterministic and makes no model call, so
  there is nothing to schedule — it simply runs.
- **Layer 2 is enqueued, not started.** It becomes an ordinary semantic job —
  the same persisted per-task record a manual Layer 2 creates — but it lands in a
  new `deferred` state first, because *when* it may spend a turn is a policy
  question (see [below](#when-an-automatic-layer-2-turn-actually-runs)). Cost is
  not the concern; contention is: an L2 turn can land in the same subscription
  rate window a dev session needs.
- **That job record is written before the trigger hands off any work.** A run's
  end is observed exactly once and then settled forever, so the hub records the
  job first and decides about the turn afterwards. A hub stopped or crashed
  before the turn runs therefore still owes that task an evaluation and pays it:
  a held job is untouched by the startup sweep and picked up by the next dispatch
  sweep. (A job interrupted *mid-turn* is still settled `failed` at startup —
  *"semantic evaluation abandoned (hub restarted during queued)"* — visible in the
  task's Layer 2 panel and retryable from its button.)
- **A hub that stops mid-transition resumes it, rather than losing the
  completion.** The end write itself is what the trigger sits behind, and it is
  irreversible: nothing re-observes a run that already has an end time. So the
  end is written in two phases — one atomic write marks the run ended *and* its
  end-transition work still owed, and a second clears the mark once that work has
  run. A run found ended with the mark still set belongs to a hub that stopped
  inside the transition, and the next status scan finishes it: the evidence
  settles, then the completion is handed over as usual. Settling on resume reads
  only what that run left in its own directory — its session snapshot, or the
  stdout capture behind it — never the orchestrator log in the repo, which the
  outage may have taken away; so a cleaned log costs a resumed run's evidence
  nothing. Only runs a hub with this marker actually ended can be resumed, so
  nothing reaches back into already archived history. The run-end push and the
  usage refresh are *not* replayed — they describe a moment that has passed — and
  the audit trail keeps exactly one `run_ended` event.
- **A task gets at most one automatic Layer 2, ever.** The completion signal is
  per *run*, not per task (a task that completes without being archived stays
  launchable, and the close-out-only re-run that follows classifies `completed`
  again), so the durable per-`(repo, task)` job record is the guard. Any existing
  record — held, in-flight, completed, or failed — means the automatic path
  accepts nothing. A `failed` job is therefore *recorded*, not retried forever;
  retrying it is the operator's explicit Layer 2 button.
- Layer 1 is not guarded that way. It is free and deterministic, so a repeated
  completion signal simply produces a fresher deterministic report; only the
  complete Layer 1 + Layer 2 set is ever published to the feed.

What it can never do:

- Change run state, readiness, attention, or a launch/intake gate. It runs after
  the ending run is already persisted, off the run manager's thread, and every
  failure is logged and dropped.
- Reach a task that completed **outside** a hub run. There is no signal for
  those, and their evidence would be near-empty anyway; they are the backfill
  problem, not the trigger's.

One further quiet: an automatic job settling to **completed** sends no push.
Every completed task now produces one, it carries nothing to act on, and it would
arrive right behind the run-end push that does. An automatic **failure** still
pushes — that is the corpus quietly shrinking. A manual evaluation pushes on
both outcomes, as before.

Set `ORCH_HUB_EVAL_AUTO=false` to return to on-demand-only evaluation with the
subsystem otherwise untouched. Any jobs already accepted stay held rather than
running or failing; see [below](#when-an-automatic-layer-2-turn-actually-runs).

### Declining Layer 2 for one run

The launch dialog carries an **Evaluate on completion** checkbox, on by default.
Unchecking it declines the automatic Layer 2 turn for a task *this run* completes.
It exists for weeks when the subscription budget has to go to development rather
than to measuring it — deferral cannot serve that, since it moves spend inside the
weekly bucket rather than out of it.

Three things bound it, and they are what make it admissible at all:

- **Layer 1 still runs.** It makes no model call, so declining it would save
  nothing and forfeit the whole deterministic layer.
- **The skip is a record, not an absence.** The task gets a terminal `skipped` job
  with a bounded machine reason (`run-opt-out` — never free text, which would be
  unanalyzable and would leak repo content into a persisted surface). It is
  counted in the queue census and broken out per repo, so a corpus that is being
  thinned says so. An opt-out whose entries simply vanished would hand back, at
  production time, exactly the selection automatic evaluation exists to remove —
  and skips do not thin a corpus evenly, they cluster on the expensive,
  remediation-heavy tail.
- **It never reaches the orchestrator.** The choice rides the start request
  (`auto_semantic_eval`, beside `acknowledge_blocked`) and is persisted on the run
  record. It is not orchestrator argv: the orchestrator parses argv strictly and
  an unknown flag would abort the launch. A run record written before the field
  existed reads as *on*, so no earlier run is retroactively excluded.

When two runs of one task disagree — a task stays launchable until it is archived,
so it can complete more than once — the run that **completes the task** writes the
record and decides, in both directions: a later completing run can neither enqueue
over a skip nor skip over an accepted job. A run that does not complete the task
never reaches the trigger, so unchecking the box on session 1 of a three-session
task decides nothing.

Un-skipping is an operator action, never a mechanical one: the task's Layer 2
panel still offers the button, and running it produces the report the launch
declined.

## When an automatic Layer 2 turn actually runs

An automatically enqueued Layer 2 job starts `deferred` and is promoted to a real
turn by a background sweep — on the resident 30-second tick, and once immediately
after the completion that created it, so an uncontended completion still becomes
a turn within milliseconds.

Three rules decide, all of them tunable (see
[Configuration](#configuration)) so the policy can change without a code change:

1. **A utilization ceiling.** If the evaluator's subscription provider (Claude,
   today) reports `ORCH_HUB_EVAL_USAGE_CEILING` percent or more used on its most
   exhausted currently-measured window, the job waits. One L2 turn measures ~32k
   input tokens against tasks that consumed several dev sessions of up to 200k
   context each — under 1% of the work it measures — so the default 80% leaves
   roughly twenty times the turn's own cost in headroom. The ceiling is not there
   to make the turn *fit*; it is there so measurement never competes with work in
   a window the operator has started rationing.
2. **Unknown capacity waits.** Missing, unavailable, stale, rolled-over, or
   otherwise unreadable usage defers the job — an unknown window is not an empty
   window. This is deliberately permanent when a usage feed dies: the queue stops
   visibly rather than quietly spending a window nobody can see.
3. **Spacing between turns.** At most one held job is promoted per
   `ORCH_HUB_EVAL_DISPATCH_SPACING`, measured from the last turn *admitted* to
   spend — a manual evaluation counts, since it draws on the same window, and a
   turn counts from the moment it was admitted rather than from the moment its
   worker got going, so two sweeps in quick succession cannot both find "no turn
   has started yet". Without this, a queue that accumulated under sustained
   pressure would become dispatchable *all at once* the moment the window rolled,
   spending the fresh window's opening capacity exactly when the operator comes
   back to work.

The queue is **durable and never dropped**. A held job survives a hub restart
untouched — nothing fails it, and the next sweep resumes it — and the oldest held
job always goes first, so the corpus fills in completion order.

Durable also means it outlives the switches. A queue accepted while automatic
evaluation was on stays exactly where it is if the hub is restarted with
`ORCH_HUB_EVAL_AUTO` or `ORCH_HUB_EVAL_ENABLED` off: nothing is promoted (that
would be a model turn in a deployment that said manual-only) and nothing is
failed (with the subsystem off, a promotion would settle the record `failed` and
spend the task's one automatic attempt on a turn that never ran). Turn both back
on and the next sweep drains what was always there — the switches need no
recovery step, because "deferred" already means "not now".

**None of this applies to the Layer 2 button.** An operator asking for an
evaluation now is not the production-side selection this policy exists to remove,
so a manual start ignores the ceiling, the spacing, and any held record for that
task. That is also the escape hatch if a broken usage feed ever holds the queue
indefinitely.

If a job waits longer than `ORCH_HUB_EVAL_QUEUE_MAX_AGE` (default six hours — one
full five-hour window plus slack), the hub logs a warning naming the repo, task,
and reason. It stays a held job, not a failed one; the bound exists so a queue
that has stopped draining — usually because the usage feed broke, which defers
forever by design — cannot stay invisible for a week.

## Seeing the queue

A log line is enough only if someone is reading the log. Automatic evaluation's
failure modes are all *absences* — a queue nobody drains, a turn that failed, a
switch left off — and an absence is exactly what nobody notices. So the state of
the automatic path is a section on the dashboard's Operations Overview, on the
same footing as Subscription usage: its own block below the operational panels,
display-only, never a **Needs attention** row and never a gate. Nothing is
blocked by a stalled evaluation queue; only the measuring has stopped.

It shows:

- **The census** — *the latest attempt per task*: how many completed, how many
  are waiting, how many failed, how many were skipped. These counts are over *every* persisted job
  record and are never truncated; a figure that quietly meant "the first twenty"
  would report a twenty-deep queue as calm, which is the silence this section
  exists to break.

  It counts **attempts, not reports**, and the line says so. The store keeps one
  record per task holding its latest attempt, and a manual re-run overwrites a
  terminal one — so a task whose report completed and whose later retry failed
  counts here as `failed`, while that first report is still there in the task's
  evaluation history. That is the right answer for a queue view: the retry did
  fail, and this section reports on the automatic path, not on the corpus. For
  what a task actually carries, open its evaluation view or the
  [report feed](#the-global-report-feed); a task completed before automatic
  evaluation existed has no record here at all.
- **Skips per repo** — the tasks that completed and were deliberately not
  evaluated, split by repo, because that is the shape a thinning corpus has.
  Repos with no skipped record are absent rather than shown as zero: the job store
  knows nothing about repos it holds no record for. Like the rest of the census
  this counts *current records*, so it is never a share of a repo's tasks.
- **The outstanding jobs**, oldest first: which task, which repo, how long it has
  waited, and — for a held job — the reason in prose (`capacity`,
  `usage-unknown`, `pacing`). Each row links to that task's evaluation view. The
  row list *is* capped for readability; when it is, the section says so. Skipped
  and completed jobs are counted but not listed: both are resting outcomes with
  nothing to act on, and one row per opted-out task would crowd out what is
  actually stuck.
- **Why nothing is draining.** Three of the four reasons are on the records
  themselves. The fourth — `ORCH_HUB_EVAL_ENABLED` or `ORCH_HUB_EVAL_AUTO`
  switched off — deliberately leaves no per-record trace (a closed gate is global
  config, not a fact about a job, and stamping it would churn the whole queue on
  a config change), so the section names it from configuration instead.
- **A stall flag** on any held job past `ORCH_HUB_EVAL_QUEUE_MAX_AGE`, with the
  bound spelled out. It is the same population the service log warns about, by
  the same rule, so the two never disagree.

A read failure renders as *unavailable* rather than an empty queue — an unknown
queue is not a calm one — and it degrades only this section. With
`ORCH_HUB_EVAL_ENABLED` falsy the section is absent entirely: there is no queue,
and a permanent "disabled" notice about a deliberate configuration is noise.

A single task's held state is visible where you would expect it too: the Layer 2
panel of that task's evaluation view shows a queued-and-waiting job with its
reason, instead of the "never evaluated" trigger it used to show. The button
stays available there, relabelled — running it now is the documented way past a
queue a broken usage feed would otherwise hold indefinitely. A skipped task reads
the same way: the panel says the run declined the turn, rather than showing the
never-evaluated trigger, and still offers to run it.

Both surfaces are reads. Opening them never dispatches, holds, or rewrites a job,
and never consults the usage service (a usage read can trigger a background
refresh, which a passive dashboard poll has no business causing).

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
effective protocol identity).
Provenance the evidence pack does not carry is reported as **explicitly
unavailable with a reason** — today that is always the case for the
orchestrator `effective-config` snapshot, since evidence packs record it
nowhere.

### Protocol provenance

`provenance.protocol` is the cohort key for asking whether a protocol change
helped, and it is a **pair**:

- `effective_revision` — the mandrel commit whose canonical protocol produced
  the deployed payload.
- `deploy_lock_hash` — `.ai-deploy-lock.json`'s digest of that payload.

Neither is published without the other. The commit alone names a source tree the
hub cannot show the sessions read, and the digest alone names bytes with no place
in any history; a consumer cohorting by half of it would group reports the hub
never confirmed belong together.

Three distinct things stand behind that pair, and they are worth keeping apart
when reading a `detail`:

1. **Copying the lock (per run, at launch).** Before the orchestrator starts, the
   hub stamps the repo's `.ai-deploy-lock.json` into the hub-owned run dir as
   `protocol.json`. That freezes *what the deploy claimed*, so evaluation never
   reads today's lock for yesterday's run. It is a copy of a receipt, not a check
   of the tree.
2. **Validating the actual files (per run, at two boundaries).** A receipt is not
   the tree: hand-editing a deployed protocol file — which the protocol forbids,
   and which is therefore the case worth catching — leaves both receipts
   untouched. At **launch**, readiness has already run `mandrel status`, which
   compares every deployed file against `.ai-deploy-manifest.json`; the run exists
   only because that passed. At the **end** of a run whose exit the hub observed
   itself, the hub re-reads the lock and then hashes every deployed file the lock
   names against the manifest's record of it. Only a run that passed both keeps a
   publishable identity. These are two point observations: nothing is watched
   between them, and the hub claims nothing about a file edited and restored in
   between.

   The end look happens **once, at that transition, and never again**. A run
   whose end the hub did not observe — it was down, or the run was a resumed tail
   — therefore never gets one, and can never publish an identity. That is
   permanent by design, not a check still owed: a later look would compare
   against whatever is deployed *now*, so an ordinary redeployment after the run
   would read as a mid-run change the hub never saw. Recovering such a run would
   need either an observation taken *at* its end boundary by something else, or a
   complete trusted history of every change to both receipts and the deployed
   files across the whole gap — and an in-place edit leaves no receipt at all.
   Nothing produces either, so the honest answer is the permanent one.
3. **Aggregating over the task (across runs).** A report is about a task, which
   several runs worked, so the pair is published only when **every** contributing
   run verified **one and the same** pair.

Anything else is `available: false` with both fields `null` and a `detail`
naming which case it was: a run from before the hub captured this, a deployment
that moved mid-run, deployed files that no longer matched their receipt, a run
whose end the hub was not up to observe, runs that span a redeployment, or runs
that agree on a revision but verified **different payloads** under it. Reports
published before the identity became a pair (evidence schema 17 and earlier)
carry a revision reached by comparing lock copies alone; they read back
unavailable with that reason rather than being served as a verified identity with
a field missing.

Nothing is ever backfilled or completed from a repo's current deployment lock or
files, which by publication time usually name a later revision than the runs
used. The practical cost is that every run that ended before the hub verified
payloads publishes no identity, so older tasks read as protocol-provenance
unavailable — which is the honest state, not a defect to repair.

### Where you read it

Because "unavailable" is common and permanent, both surfaces state it rather than
leaving a blank:

- **Run history** carries each run's own snapshot beside its launch config, as a
  `Protocol identity:` disclosure. Its verdict is one `coverage` value —
  `verified`, `redeployed mid-run`, `deployed files edited`, `never checked`,
  `end check inconclusive`, `no identity captured`, `record unreadable`, or
  `no record` — plus, only while a run is still live, `end check pending`. A run
  that has finished never shows an outstanding check. Opening the row gives the
  captured revision and payload digest, when the capture and the end check ran,
  how many deployed files that check compared, and the hub's own one-line reason
  for this run.
- **The report feed** (`#/reports`) shows a `protocol` row on every card beside
  `runs` and `commits`: the effective revision when the task's runs agreed on
  one, otherwise `unavailable` followed by the reason — how many contributing
  runs carried no verified identity, or which revisions or payloads they
  disagreed about.

### How much of the corpus carries one

```
GET /api/protocol-coverage
```

The aggregate of the two rows above — `Protocol identity coverage →` from Run
history or the report feed, `#/protocol` — for the question neither one answers:
how much of this hub's corpus can be cohorted by protocol revision at all.

- `runs` counts **every persisted run** by the same `coverage` values the run
  rows show, complete and never capped, with every bucket present at zero. It is
  the more forgiving population by construction: a report needs *all* its runs to
  have verified the same identity, so the verified run share is an upper bound on
  the published one and the other buckets are what explains the difference.
  `end check pending` here means a check that is genuinely still owed — a live
  run, or one whose end tail was interrupted and which the next scan resumes.
  A run record broken past refreshing (an unreadable `run.json`) can never reach
  its check, so it reads as `never checked` like any other decided absence.
- `reports` counts the published side, over **tasks** first: `tasks` and
  `tasks_with_identity` are distinct published `(repo, task)` pairs, each answered
  by that task's **newest** report — newest by evaluation id, with publication
  order as the tie-break. A rerun mints a fresh evaluation id rather than
  overwriting, so counting catalog entries would let one much-rerun task weigh
  several times; `reports` and `reports_with_identity` carry that entry
  population beside it, because the feed a consumer pages is made of entries and
  the two figures have to reconcile. Each uncovered task is listed once, under
  the report that decides it, with the reason the hub persisted when it published
  that report (newest first, capped; `truncated` says when, and the counts stay
  complete). Its own `available: false` means the census could not be taken at
  all — evaluation disabled, or a catalog failing closed — never that nothing is
  published.

Deliberately not an `/api/evaluation/…` route: the run half is a property of run
records and keeps answering on a hub with the sidecar switched off, which is
exactly the hub whose every report is unpublished. Reading it is read-only in the
strict sense — no pid probe, no record rewrite, no publication, no model call.
Asking how many runs carry an identity must never spend the one look a run gets.

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

### Verifying and repairing the catalog

Two CLI-only verbs turn that `500` into something you can act on. They are local
operations on durable publication state, so they are deliberately not exposed on
the dashboard, and neither runs automatically at startup — a broken catalog is
already fail-closed and safe, and silently self-repairing it at boot would trade
a visible halt for an invisible rewrite.

```
orch-hub eval catalog verify [--json]
```

Read-only: it publishes nothing, evaluates nothing, makes no model call, and
writes no byte, so it is safe against a live hub. It prints the entry/line count,
the artifact census, and every defect it finds, each located by catalog line
number plus the `seq`/`report_key` it collides with — duplicates, lines that do
not parse, and entries whose stored identity cannot address their own artifacts.

**Exit code is `0` exactly when the feed can be traversed.** Non-zero means a
repeated `seq`/`report_key` or an unreadable catalog: the same condition that
makes the feed routes answer `500`. Damaged and pruned artifacts are reported but
do not change the exit code, so a health check can tell "the feed is down" from
"there is a symlink to clear".

The census keeps those two apart on purpose:

| State | Meaning |
|---|---|
| `retained` | The complete L1+L2 set is on disk and every file opens. |
| `pruned` | The evaluation directory is gone, which is what retention removes. **Not a defect** — an entry outliving what it indexes is the design. |
| `incomplete` | The directory is still there without the whole set (however much of it is missing). Retention removes whole dirs, so this is not retention's doing. |
| `damaged` | A symlink stands where the hub wrote an artifact, or at a `<repo>/<task>/<evaluation>` directory hop. |
| `unreadable` | An artifact is still standing and will not open. |
| `unaddressable` | The line's stored identity is not the one its `report_key` derives from. |

Each state is the answer a bundle fetch of that entry gives, so `retained` means a
consumer can actually take that report — verify opens all four artifacts, it does
not merely list them. It stops at the open rather than reading the bytes, so a
verify run costs the same whatever the catalog indexes; a fault that appears only
part-way through a file is left for the fetch that meets it.

`damaged` is the state worth acting on. The hub never writes a symlink inside the
evaluations tree, every read refuses one rather than following it, and a prune
walks the tree's own hops no-follow — so it refuses the linked hop and reports the
id as *retained*. Damage therefore accumulates until you remove the link by hand;
folding it into `pruned` would present it as retention doing its job. Because that
repair is in the tree rather than in the catalog file, those findings print the
evaluation directory on an `at:` line (and carry `path` in `--json`), so clearing
one does not mean reconstructing
`<data_dir>/evaluations/<repo-slug>/<task-id>/<evaluation-id>/` by hand.

```
orch-hub eval catalog repair [--confirm] [--json]
```

Without `--confirm` it only prints the plan. The rule it admits lines under is
narrow and absolute: **every already-published `seq` and `report_key` stays
exactly where it is, or nothing happens.** Two drops qualify —

- a line byte-identical to an earlier one (a merged catalog, or a backup restored
  over a live one), since the surviving copy is the same bytes;
- trailing bytes from an interrupted append that parse as no entry at all.

Everything else is **refused**, with the conflicting line pair named and *nothing*
written — not even the safe drops, so the line numbers `verify` gave you stay
valid. In particular, two *distinct* entries sharing a `seq` or a `report_key` can
only be separated by renumbering or dropping one, and both move an identity a
consumer may already have imported; that decision belongs to whoever knows what
has been consumed. Interior lines that merely fail to parse are also left alone:
every read already skips them, and they are the only surviving evidence of
whatever damaged the file.

A confirmed repair copies the original to
`report-catalog/catalog.jsonl.<timestamp>.bak` before replacing it (the copy is
inert — only `catalog.jsonl` is ever read), and writes through a temp file plus
rename so a crash leaves either the old file or the new one. A backup never
overwrites one: the stamp is second-resolution, so a second repair within the same
second takes the next free name (`...<timestamp>-1.bak`) rather than erasing the
evidence the first one preserved. Exit code follows `verify`: `0` when the catalog
on disk now serves the feed, so an unconfirmed plan against a broken catalog is
non-zero. A catalog that cannot be read or backed up at all prints one `error:`
line and exits non-zero.

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
| `GET /api/evaluation/queue` | Cross-repo state of the automatic path: a complete per-status census of every job record — each task's *latest attempt*, not a count of tasks carrying a report — plus a per-repo tally of skipped tasks, the jobs still owed a turn (capped, `truncated` says when), the configured stall bound, and whether the trigger gate is open. Read-only — it never dispatches. `404` when disabled. |
| `GET /api/evaluation/reports` | One cursor-paginated page of the [global report feed](#the-global-report-feed), oldest-published first. |
| `GET /api/evaluation/reports/{report_key}` | One published report's import bundle by global key; pruned artifacts answer `200 {"available": false}`. |
| `GET /api/evaluation/reports/{report_key}/artifacts/{name}` | One published artifact's [byte-exact persisted content](#verifying-one-artifact-byte-for-byte) for independent digest verification; pruned answers `410`. |
| `GET /api/history/runs` | One page of run history plus batched newest-evaluation state (`none`/`l1`/`l1_l2`) per distinct task group on that page. |
| `GET /api/repos/{repo}/evaluations/{task}` | List a task's evaluations (newest-first) + resolved target state. |
| `GET /api/repos/{repo}/evaluations/{task}/{id}` | Read one evaluation's evidence/static/semantic/`report.md`. |
| `POST /api/repos/{repo}/evaluations/{task}/evidence` | Generate a bounded evidence pack only. |
| `POST /api/repos/{repo}/evaluations/{task}/metrics` | Generate evidence + Layer 1 static metrics. |
| `POST /api/repos/{repo}/evaluations/{task}/semantic/job` | Start or reuse the current async Layer 2 job; duplicate in-flight starts return the same job. |
| `GET /api/repos/{repo}/evaluations/{task}/semantic/job` | Read the current Layer 2 job (`deferred`/`queued`/`running`/`completed`/`failed`/`skipped`; `null` when none exists). |

When disabled, every route except `GET /api/evaluation` answers `404` with the
fail-closed reason.
