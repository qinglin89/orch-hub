"""Async semantic-evaluation job lifecycle: deferred / queued / running / settled.

Layer 2 semantic evaluation (:meth:`orch_hub.semantic.SemanticEvaluator.evaluate`)
is one long model turn. Driven synchronously it leaves the caller — and the
dashboard — with no observable state between the click and the result: the page
looks idle while a model call runs, and a reload or another browser session
cannot tell whether an evaluation is already in flight or recently failed. This
manager wraps that turn in an explicit, persisted job so its in-flight and
terminal state is visible across reload/navigation/other sessions, and a
duplicate model call for the same ``(repo, task)`` is prevented while one is
already running.

It mirrors the intake manager's off-lock-turn architecture
(:mod:`orch_hub.intake`): :meth:`SemanticJobManager.start_job` gates + persists
the transient ``queued`` job under the lock and returns it at once; the (slow)
evaluation turn runs off the lock through an injectable executor
(:data:`SemanticJobExecutor`, a daemon thread by default; tests inject a
synchronous one) and re-acquires the lock to finalize. Persisting the transient
status *before* the turn means a hub that dies mid-turn leaves an observable
orphan, reconciled to ``failed`` at startup (:meth:`reconcile_orphans`) so a
stuck job never blocks a fresh attempt.

A turn can also stop making progress without the hub dying, and that case needs
its own answer because the startup sweep — which settles any transient record it
finds, on the reasoning that a hub only now starting owns no workers — would
settle live turns if it ran while the service was up. :meth:`reconcile_stuck` is
that answer: the same settle, admitted by a clock instead of by process absence,
driven by the resident monitor tick.

What that clock reads is one **whole-attempt deadline**, persisted on the record
as ``deadline_ts`` when the turn starts and binding on the worker as well as on
the sweep:

.. code-block:: text

    started_ts ........ EVIDENCE_GRACE ........ eval_turn_timeout ....... SETTLE_GRACE ....... deadline_ts
               |<- evidence pack, static ->|<- the model subprocess ->|<- parse, write, publish ->|
                                           ^                          ^
                                  turn must start by            turn is dead by

The worker is refused a model turn once the first segment is spent, so the model
subprocess — bounded by ``Config.eval_turn_timeout`` — is dead by the second mark,
a whole ``SETTLE_GRACE_SECONDS`` before the record becomes reapable. That is the
property the sweep rests on: **a record past its ``deadline_ts`` cannot be a turn
still inside its own model deadline.** Deriving the bound from ``started_ts`` and a
fixed grace instead would only *assume* it — an evidence build slower than the
grace would leave a live, unexpired model turn to be reaped as stuck, and the
worker would go on to publish artifacts the freed slot had already replaced. So
"stuck" stays a derived, enforced fact rather than a guess about how long an
evaluation ought to take.

Layout under ``Config.data_dir``, parallel to the run manager's ``runs/`` and the
intake manager's ``intake/`` trees:

.. code-block:: text

    <data_dir>/semantic-jobs/<repo-slug>/<task-id>/
        job.json    hub-owned job record (atomic rewrite on updates)

There is exactly **one record per** ``(repo, task)`` at a deterministic path: a
new attempt (only legal once the prior one is terminal) overwrites it. So the job
tree stays bounded (one tiny file per evaluated task, not one per attempt), the
duplicate guard and the "current job for this task" read are a single
deterministic-path load with no directory scan, and a completed attempt's result
is never lost — it lives in the evaluations tree keyed by ``evaluation_id`` (the
job record only points at it). Both path components — the slugged repo segment
and the task-id segment — are validated as single safe path components, which is
what makes the deterministic path containment-safe (and keeps every record on the
two-level ``<repo-slug>/<task-id>/`` layout the startup reconcile scan walks).

The operator's own lever on a single record is :meth:`SemanticJobManager.cancel_job`
(modelled on the intake manager's cancel): it settles a ``deferred``/``queued``/
``running`` record to terminal ``cancelled``, freeing the task's slot at once. From
``deferred``/``queued`` the worker's own guard prevents the model turn; from
``running`` the turn is *killed*, through the canceller this manager registers for
each in-flight attempt — so the record and the spending stop together rather than
the record alone. That is what separates a cancel from the reap
(:meth:`SemanticJobManager.reconcile_stuck`), which by then has no live child left
to reach: its records are past a deadline the model subprocess's own timeout
expired a whole settle grace earlier. The queue-wide counterpart — holding every
held job without settling any of them — is not this manager's business at all: it
is a hub-owned switch read by the dispatch policy (:mod:`orch_hub.eval_control`).

A cancel also has an end, and it is the same canceller that draws it. Once the
turn *returns*, the window is spent and the worker is committing its result —
parsing, writing both artifacts, publishing to the global feed, settling the
record. Nothing there can be undone by stopping something, so the attempt seals
its canceller at that instant and a later cancel is refused (409) instead of
writing ``cancelled`` over an evaluation that exists on disk and in the feed. The
seal and the cancel are one atomic decision inside the canceller's own lock, and
the handle stays registered until the terminal record is written, so there is no
gap between the two in which a cancel could slip through and lie:

.. code-block:: text

    running
      |            a cancel here kills the turn:
      |            nothing is parsed, persisted, or published
    turn returns   <-- the attempt seals its result here
      |            a cancel here is refused (409):
      |            the report is already being written and published
    record terminal

A completion the operator opted out of Layer 2 for is the one record that never
becomes an attempt: it is written straight to a terminal ``skipped``
(:meth:`SemanticJobManager.skip_if_absent`), carrying a bounded machine reason, so
the corpus's missing entries are countable instead of invisible. Layer 1 is never
skipped — it makes no model call.

Automatic (completion-triggered) attempts enter one state earlier than an
operator-driven one. They are accepted as ``deferred``: durably recorded, holding
the task's duplicate slot, but not yet admitted to a model turn — the automatic
dispatch policy (:mod:`orch_hub.eval_dispatch`) decides when the subscription
window can afford one. A deferred record is therefore neither transient nor
terminal: the startup orphan sweep must leave it alone (nothing is in flight to
abandon), and only :meth:`SemanticJobManager.dispatch_deferred` promotes it into
the ordinary ``queued`` -> ``running`` -> terminal path. The manager owns the
*mechanism*; the policy — a utilization ceiling, a spacing between turns, a bound
on how long a job may wait — lives outside it, so this module still knows nothing
about subscription usage.

It also owns the trigger timing for the terminal-outcome push (mirroring how the
run manager owns run-end pushes and the intake manager owns intake-ready ones):
one metadata-only notification fires from the finalize that settles a job to
``completed``/``failed``, so an operator who started an evaluation and navigated
away learns the result without keeping the dashboard open. One exception, and it
is about noise rather than isolation: an *automatic* job settling to ``completed``
pushes nothing. Every completed task now produces one, nobody is waiting on it,
and a second push per completion would drown the run-end push that is actually
actionable — while an automatic *failure* still pushes, because that is the
corpus quietly shrinking. Delivery is isolated — a dead Bark backend can never
corrupt the job record.

The manager owns **only** job status. The read-only evaluation itself — evidence,
static metrics, semantic report, retention — stays entirely in
:class:`~orch_hub.semantic.SemanticEvaluator`, so the async wrapper introduces no
new target-repo/run mutation: a failed or in-flight job leaves exactly the
hub-owned-artifacts-only footprint the synchronous path did (an aborted attempt's
partial artifacts are cleaned by the evaluator's own all-or-nothing dir removal).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .config import Config
from .models import ManagedRepo, SemanticJob
from .notifications import Notification, Notifier, semantic_job_notification
from .semantic import SemanticEvaluator
from .subprocess_utils import CancelOutcome, CommandCanceller

_log = logging.getLogger(__name__)

SEMANTIC_JOBS_SUBDIR = "semantic-jobs"
"""Top-level hub-data-dir subtree holding semantic-job records, parallel to the
run manager's ``runs/`` and the intake manager's ``intake/`` trees."""

SEMANTIC_JOB_NAME = "job.json"
"""The persisted job record, one per ``<repo-slug>/<task-id>/`` dir."""

# Lifecycle statuses. The transient pair brackets an in-flight evaluation turn;
# the terminal set is a resting outcome. ``error`` is reserved for an unreadable
# record (a broken dir that must surface, not hide), distinct from ``failed`` (a
# legitimate evaluation-failure outcome), and — carrying no repo attribution —
# never holds a ``(repo, task)`` duplicate slot. ``skipped`` is the one terminal
# state reached without an attempt: a completed task whose run declined the model
# turn. ``cancelled`` is the operator stopping an attempt that was already
# accepted, which is neither of those two: not a failure (nothing went wrong) and
# not a launch-time decline (the task was in the queue, and — from ``running`` — a
# turn had already been spent on it). Mirrors the intake statuses.
STATUS_DEFERRED = "deferred"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

STATUSES = (
    STATUS_DEFERRED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_SKIPPED,
    STATUS_ERROR,
)
"""Every status a record may hold, in lifecycle order.

Exists for the census an operator surface takes over the whole store
(:func:`orch_hub.eval_dispatch.queue_snapshot`): a count keyed only by the
statuses actually *observed* silently omits the interesting zeroes — "no jobs
failed" and "no jobs are held" are exactly the figures worth stating — so the
enumeration has to be declared rather than discovered. Ordered because a reader
renders it in this order; a new status is appended where it belongs in the
lifecycle, and every counting surface picks it up without being edited."""

SKIP_RUN_OPT_OUT = "run-opt-out"
"""Skipped because the run that completed the task was launched with the Layer 2
opt-out (``RunRecord.auto_semantic_eval`` false)."""

SKIP_REASONS = frozenset({SKIP_RUN_OPT_OUT})
"""The closed vocabulary a ``skipped`` record's reason may hold.

Closed, and enforced at the one entry point (:meth:`SemanticJobManager.skip_if_absent`
raises on anything else), because the whole admissibility of the opt-out rests on
the skip being *countable*: free text — operator prose especially — is
unanalyzable, leaks repo content into a persisted surface, and invites recording a
rationalization that is itself the bias this task exists to remove. A new skip
cause is a new token here, added deliberately."""

_TRANSIENT = frozenset({STATUS_QUEUED, STATUS_RUNNING})
"""In-flight states that bracket an evaluation turn. A persisted transient record
with no live turn (a hub that died mid-turn) is an orphan holding the
``(repo, task)`` duplicate slot, reconciled to ``failed`` at startup
(:meth:`SemanticJobManager.reconcile_orphans`). The duplicate guard and the
finalize steps key off these.

``deferred`` is deliberately NOT here. A deferred job has no turn to abandon, so
a restart sweep that failed it would destroy exactly the queue the deferral
exists to hold — every held evaluation would be lost to the first hub restart,
which for a corpus that must not be selection-biased is the same silent gap
automatic triggering was built to close."""

_TERMINAL = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELLED,
        STATUS_SKIPPED,
        STATUS_ERROR,
    }
)
"""Resting states: a new attempt may overwrite a job in one of these.

``skipped`` belongs here, and overwritable is the right reading of it: an
operator who later asks for this task's evaluation (:meth:`start_job`) gets one,
and the corpus gains the report the skip declined. What must not happen is the
*automatic* path silently reversing it, and that is prevented by the
once-per-task guard the skip record itself is (:meth:`skip_if_absent` /
:meth:`enqueue_if_absent` both refuse against any existing record).

``cancelled`` sits here on exactly the same terms and by the same two rules: the
operator who cancelled can ask again (:meth:`start_job` overwrites it), and the
automatic path cannot quietly re-enqueue what was just stopped, because the record
itself is the once-per-task guard."""

_CANCELLABLE = frozenset({STATUS_DEFERRED, STATUS_QUEUED, STATUS_RUNNING})
"""The statuses :meth:`SemanticJobManager.cancel_job` may act on.

Declared rather than derived as "not terminal", even though today the two sets
partition :data:`STATUSES` between them. The loader deliberately preserves a
status it does not recognize (a record from a newer build, or a hand-damaged
file) so it stays visible instead of being normalized away, and "not terminal"
would quietly sweep exactly those into a cancel — rewriting a state nobody here
understands. An allowlist refuses them the way it refuses a resting record, which
is the fail-closed direction for an operation that overwrites.

Also the set the dashboard mirrors (``EVQ_CANCELLABLE`` in ``web/app.js``) to
decide which rows get a Cancel button, so the two must say the same thing.

It is a necessary condition, not a sufficient one: a ``running`` record whose
turn has already returned is cancellable by status and refused anyway, because
the attempt has sealed its result (:meth:`SemanticJobManager.cancel_job`). Status
cannot express that — the record reads ``running`` throughout — which is why the
second condition lives on the attempt's canceller rather than in this set."""

_JOB_ID_TS = "%Y%m%dT%H%M%SZ"
"""Job-id stamp prefix (matches the run/eval id stamps): readable, sortable, and
enough on its own to say when an attempt was accepted."""

_JOB_ID_TOKEN_BYTES = 4
"""Random suffix appended to the stamp, and the whole of what makes an id unique.

The stamp alone is not per-attempt identity, it is per-*second* identity, and the
finalize guards are keyed on that id (:meth:`SemanticJobManager._finalize`,
:meth:`SemanticJobManager._begin_running`). Cancelling a running job and
immediately asking for another puts two attempts inside one second: the abandoned
worker then finalizes and finds a transient record carrying what it believes is
its own id, and settles the *replacement* with the cancelled attempt's result.

Randomness rather than a clock or a counter, because both of the obvious
alternatives fail exactly where this has to hold. The clock is injectable and, in
tests and across a suspend, repeats — sub-second precision would not have
separated the two attempts above. A per-process counter resets on restart, which
is precisely when a stale id can meet a fresh one. Four bytes is far more than
the two-attempts-per-second-per-task population needs."""

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
"""Repo-name slug rule (matches ``runs._slug``/``evaluation._SLUG``): collapse any
run of unsafe chars to ``-`` (a local copy, so this module stays independent)."""

_ERROR_MAX_CHARS = 2000
"""Ceiling on a persisted failure diagnostic. The backend already bounds its
stderr tail, and parse/schema errors are short, so this is a defensive second cap
that guarantees the scope's "no unbounded model output" even if an unexpected
exception carries a large message."""

EVIDENCE_GRACE_SECONDS = 600
"""How long an attempt may spend *before* its model turn starts.

``Config.eval_turn_timeout`` bounds only the model subprocess. On this side of it
sits the gap between the admitting ``queued`` write and the worker actually
starting, then the evaluator's own pre-model work: building the evidence pack (a
task-file read, a run/event/session walk, and git subprocess calls whose cost
scales with the task's history), computing static metrics, and rendering the
prompt.

It is an *enforced* allowance, not an assumption. :meth:`SemanticJobManager._run_job`
hands the evaluator the instant this expires, and the evaluator refuses to start a
model turn after it (:meth:`orch_hub.semantic.SemanticEvaluator.evaluate`) — which
is what pins the model turn's own deadline inside the whole-attempt bound below.
Refusing costs nothing: the model turn has not started, so no window is spent, and
the attempt settles as an ordinary ``failed`` naming the overrun instead of running
on toward a reap that could not distinguish it from a hang. Ten minutes is far more
than a local evidence build has been observed to take."""

SETTLE_GRACE_SECONDS = 600
"""How long an attempt may spend *after* its model turn returns, before it is stuck.

The other side of the turn: parsing the report, writing the three artifacts,
offering the evaluation to the report catalog, pruning retention, and the finalize
write. All local IO over small files, so ten minutes is again far more than it
takes — and this is the margin that keeps the reap off a worker that is mid-write:
the model turn cannot end later than ``deadline_ts - SETTLE_GRACE_SECONDS``, so a
worker persisting a fresh report always has this whole interval before its record
becomes reapable.

Erring long is the right direction on both sides: this sweep settles a record whose
worker may still be alive, so the cost of extra grace is a later reap, while too
little fails an attempt that was about to succeed."""


class SemanticJobError(Exception):
    """Base class for semantic-job manager operational errors."""


class UnknownSemanticJobError(SemanticJobError):
    """No semantic job exists for the given ``(repo, task_id)``."""


class SemanticJobStateError(SemanticJobError):
    """The job exists but its current status does not permit the operation.

    Mirrors :class:`orch_hub.intake.IntakeStateError` (409): the target is real
    and named, and the refusal is about lifecycle state rather than identity — so
    it must not be reported as "not found", which would send an operator looking
    for a record that is sitting right there."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_ts(value: str | None) -> dt.datetime | None:
    """Parse a persisted ISO-8601 job timestamp to aware UTC, or ``None``.

    A local copy of the same tolerant parse the sibling modules keep
    (``eval_dispatch``, ``usage``, ``logs``, ``static_metrics``), so this module
    stays independent. An unparseable stamp reads as absent, which
    :meth:`SemanticJobManager.reconcile_stuck` treats as "cannot prove this record
    is overdue" and leaves alone."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _attempt_instant(record: SemanticJob) -> dt.datetime | None:
    """When this transient record's attempt began, or ``None`` if unknowable.

    ``started_ts`` once a worker flipped the record to ``running``; otherwise the
    ``queued`` record's own admitting write (``updated_ts``), which is the moment
    the attempt was accepted and the last point at which anything is known to have
    progressed. ``created_ts`` is the final fallback for a record written before
    those stamps existed. Mirrors :func:`orch_hub.eval_dispatch._turn_instant`,
    which reads the same two stamps for the opposite purpose (pacing).

    Only the *fallback* age source for :meth:`SemanticJobManager.reconcile_stuck`
    (see :func:`_reap_instant`): a record that started a turn carries the deadline
    the worker itself is held to, and reaping reads that instead."""
    for value in (record.started_ts, record.updated_ts, record.created_ts):
        parsed = _parse_ts(value)
        if parsed is not None:
            return parsed
    return None


def _reap_instant(record: SemanticJob, bound: int) -> dt.datetime | None:
    """When this transient record may be settled as stuck, or ``None`` if never.

    The persisted ``deadline_ts`` when the record has one — the whole-attempt
    deadline its worker was handed at the queued→running flip, and the only reading
    that proves the model turn inside it has already expired.

    ``bound`` (the same whole-attempt span, from the *current* config) is the
    fallback for a transient record that carries no deadline, dated from
    :func:`_attempt_instant`. Two records reach it: a ``queued`` one whose worker
    never started — safe, because no model turn exists to reap early and
    :meth:`SemanticJobManager._begin_running` refuses to start one against a
    settled record — and a ``running`` one persisted before this field existed,
    which can only survive a restart and is therefore the startup sweep's business
    anyway. A record whose stamps are all unreadable returns ``None``: unprovable
    age is not evidence of being stuck."""
    deadline = _parse_ts(record.deadline_ts)
    if deadline is not None:
        return deadline
    instant = _attempt_instant(record)
    if instant is None:
        return None
    return instant + dt.timedelta(seconds=bound)


SemanticJobExecutor = Callable[[Callable[[], None]], None]
"""Runs one evaluation turn's off-lock work. The default detaches it onto a daemon
thread so :meth:`SemanticJobManager.start_job` returns immediately with the
transient ``queued`` record; the turn then re-acquires the manager lock to
finalize. Mirrors the intake manager's ``executor`` seam. Tests inject a
synchronous executor (``lambda run: run()``) so a turn settles before ``start_job``
returns."""


def _thread_executor(run: Callable[[], None]) -> None:
    """Default executor: run one evaluation turn on a short-lived daemon thread.

    The turn (a real ``claude`` model call) can take many seconds, and it must
    never stall job reads, the duplicate guard, or an unrelated task's evaluation,
    so it runs off the manager lock. The thread is daemon so it never blocks
    interpreter shutdown; an unfinished read-only turn is safe to abandon (its
    orphan record is reconciled to ``failed`` at the next startup).
    """
    threading.Thread(target=run, daemon=True, name="orch-hub-semantic-job").start()


SemanticJobNotifyDispatch = Callable[[Callable[[], None]], None]
"""Runs one notification delivery. The default detaches it onto a daemon thread.

Kept separate from :data:`SemanticJobExecutor` even though the shapes match: a
test injects a synchronous executor to settle a turn deterministically, and must
still be able to choose independently whether delivery is synchronous. Mirrors
the intake manager's ``notify_dispatch`` seam."""


def _thread_notify_dispatch(deliver: Callable[[], None]) -> None:
    """Default notification dispatch: deliver on a short-lived daemon thread.

    Mirrors :func:`orch_hub.intake._thread_notify_dispatch`. The push is emitted
    from :meth:`SemanticJobManager._finalize`, which holds the manager lock, so a
    slow or hung Bark backend must never stall the finalize (nor an unrelated
    task's job read). The thread is daemon so it never blocks interpreter
    shutdown; a dropped push is an acceptable loss. Tests inject a synchronous
    dispatch (``lambda deliver: deliver()``) for deterministic assertions.
    """
    threading.Thread(
        target=deliver, daemon=True, name="orch-hub-semantic-job-notify"
    ).start()


def _slug(value: str) -> str:
    """Make a repo name safe as a semantic-jobs path component.

    Collapses any run of unsafe chars to ``-``, but — like ``runs._slug``/
    ``evaluation._slug`` — can still yield a bare ``.``/``..`` dot reference (e.g.
    a repo literally named ``.``), which is NOT a usable single path segment.
    :func:`_safe_segment` is what rejects those, and
    :meth:`SemanticJobManager._job_dir_for` runs the slug through it before
    composing a dir, so a dot-reference slug cannot collapse/escape the intended
    ``<repo-slug>/<task-id>/`` layout.
    """
    return _SLUG.sub("-", value).strip("-") or "x"


def _safe_segment(value: str, *, label: str) -> str:
    """Return ``value`` iff it is a single safe path segment, else raise (fail closed).

    Mirrors :func:`orch_hub.evaluation._safe_segment`: rejects an empty value, a
    separator, a null byte, or a ``.``/``..`` dot reference. Both the slugged repo
    segment and the task id are path components of the deterministic job dir, so a
    value that is not a real single segment must be refused before any dir is
    created — otherwise it steers the record off its intended
    ``<repo-slug>/<task-id>/`` path (a ``.`` slug collapses the ``<repo-slug>/``
    level so the record lands where the two-level reconcile scan can never see it;
    a ``..`` slug escapes the tree). Raises ``ValueError`` (the API maps it to 400).
    """
    if (
        not value
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or value in (".", "..")
    ):
        raise ValueError(f"unsafe {label} path segment: {value!r}")
    return value


def _validate_task_id(task_id: str) -> None:
    """Reject a task id that is not a single safe path segment (fail closed).

    A public-entry guard: :meth:`SemanticJobManager.start_job`/:meth:`get_job`
    call it before acquiring the lock, so a crafted id yields a clean 400 before
    any lock/dir work. The shape check is :func:`_safe_segment`;
    :meth:`SemanticJobManager._job_dir_for` re-applies it (to the task id and the
    slugged repo) at the single path-composition point.
    """
    _safe_segment(task_id, label="task id")


def _bound_error(reason: str) -> str:
    """Bound a failure diagnostic to :data:`_ERROR_MAX_CHARS` (defensive cap)."""
    reason = reason.strip()
    if len(reason) <= _ERROR_MAX_CHARS:
        return reason
    return reason[: _ERROR_MAX_CHARS - 1].rstrip() + "…"


class SemanticJobManager:
    """Owns async semantic-evaluation job lifecycle, hub-owned status, and dedup.

    One instance lives in the resident hub service. Collaborators are injectable
    for tests: ``evaluator`` is the read-only Layer-2 engine it drives (a real
    :class:`~orch_hub.semantic.SemanticEvaluator`, or a fake in tests), ``clock``
    must return timezone-aware UTC datetimes (it stamps the job id and lifecycle
    timestamps), and ``executor`` runs each evaluation turn off the manager lock
    (a daemon thread in production; a synchronous ``lambda run: run()`` in tests so
    a turn settles before :meth:`start_job` returns).

    ``notifier`` is the optional push backend (``None`` disables notifications
    entirely): the manager owns *when* a semantic-job push fires — exactly once,
    from the finalize that settles a job to ``completed``/``failed``
    (:meth:`_finalize`) — and ``notify_dispatch`` runs each delivery off the lock
    so a hung backend cannot stall a finalize.

    Two start paths share one record and differ only in what they admit:
    :meth:`start_job` is an operator asking for an evaluation now (it overwrites a
    terminal record with a fresh attempt), and :meth:`start_job_if_absent` is the
    completion trigger asking for one at most once ever (any existing record wins).

    Concurrency: :meth:`start_job` gates and persists the transient ``queued``
    status under the lock, returns that transient record, then runs the turn off
    the lock (:meth:`_run_job` flips it to ``running``, drives the evaluation, and
    finalizes). Reads (:meth:`get_job`) therefore never block behind a turn. The
    duplicate guard is what serializes attempts per ``(repo, task)``: while a job
    is transient a fresh ``start_job`` returns it rather than launching a second
    model turn, so at most one turn is ever in flight for a task and no finalize
    can race a superseding attempt.
    """

    def __init__(
        self,
        config: Config,
        *,
        evaluator: SemanticEvaluator,
        clock: Callable[[], dt.datetime] = _utc_now,
        executor: SemanticJobExecutor = _thread_executor,
        notifier: Notifier | None = None,
        notify_dispatch: SemanticJobNotifyDispatch = _thread_notify_dispatch,
    ):
        self._config = config
        self._evaluator = evaluator
        self._clock = clock
        self._executor = executor
        self._notifier = notifier
        self._notify_dispatch = notify_dispatch
        self._lock = threading.Lock()
        # In-flight turns this process owns, ``job_dir -> (job_id, canceller)``,
        # guarded by the same lock as the records. It is what gives
        # :meth:`cancel_job` something to actually stop: a record says a turn is
        # running, but only the worker holds the process, and this is where the
        # worker publishes the handle that reaches it. Registered atomically with
        # the queued->running write and dropped when the turn returns, so a
        # ``running`` record with no entry here is unambiguously not our worker's
        # (an orphan from a dead process, or a turn already past its last step) —
        # nothing to stop rather than a lost handle.
        self._live: dict[Path, tuple[str, CommandCanceller]] = {}

    @property
    def jobs_dir(self) -> Path:
        return self._config.data_dir / SEMANTIC_JOBS_SUBDIR

    # -- start ----------------------------------------------------------------

    def start_job(self, repo: ManagedRepo, task_id: str) -> SemanticJob:
        """Start (or reuse) the semantic-evaluation job for ``(repo, task_id)``.

        The duplicate guard is the first thing under the lock: if the current
        record for this ``(repo, task)`` is still transient (``queued``/
        ``running``), it is returned as-is and **no** new model turn is launched —
        the scope's "return the existing in-flight job instead of starting
        another." Otherwise (no record yet, a terminal one, or one the automatic
        dispatcher is still holding ``deferred``) a fresh ``queued`` record
        overwrites it, is persisted before the turn so an interrupted hub leaves
        an observable orphan, and the evaluation turn runs off the lock
        (:meth:`_run_job`).

        Overriding a ``deferred`` or ``skipped`` record is the point, not an
        oversight: the capacity policy and the launch-time opt-out both exist to
        keep *mechanical* evaluation out of the window the operator is working in,
        and an operator asking for this evaluation now is not the production-side
        selection either of them shapes — asking undoes a skip by producing the
        report it declined, which is the corpus gaining an entry, not losing one. So this path
        never consults utilization — it is also the documented escape hatch when a
        broken usage feed would otherwise hold the queue indefinitely. The fresh
        record is stamped ``auto=False``, so a report's provenance still says
        whether its existence was somebody's choice. The evaluator identity
        (backend/model/schema) is
        stamped now so the queued/running UI names the model before a report
        exists. An unsafe ``task_id`` raises ``ValueError`` (mapped to 400) before
        any dir is touched. Returns the transient ``queued`` record immediately; a
        later :meth:`get_job` (the dashboard poll) observes ``running`` and then
        the ``completed`` (with ``evaluation_id``) or ``failed`` (with a bounded
        diagnostic) outcome.
        """
        _validate_task_id(task_id)
        with self._lock:
            job_dir = self._job_dir_for(repo.name, task_id)
            current = self._load_if_present(job_dir)
            if current is not None and current.status in _TRANSIENT:
                # Duplicate guard: an in-flight job already owns this (repo, task);
                # hand the caller the same job to poll rather than a second turn.
                return current
            record = self._queue_locked(
                job_dir, repo, task_id, status=STATUS_QUEUED, auto=False
            )
        # Run the evaluation off the lock; the transient record is returned now and
        # the turn settles it asynchronously. Submit strictly OUTSIDE the lock: the
        # turn re-acquires it to flip to running / finalize, which a synchronous
        # executor would deadlock on the non-reentrant lock.
        self._submit(lambda: self._run_job(job_dir, record.job_id, repo, task_id))
        return record

    def enqueue_if_absent(self, repo: ManagedRepo, task_id: str) -> SemanticJob | None:
        """Accept a job for ``(repo, task_id)`` only if the task has never had one.

        The automatic (completion-triggered) counterpart of :meth:`start_job`, and
        it differs from it twice over. It is a *stricter* guard: ``start_job`` is
        an operator asking for this task to be evaluated now — so it overwrites a
        resting record and starts a fresh attempt — while this one exists to make a
        task's automatic evaluation happen **at most once, ever**. Any existing
        record (deferred, transient, ``completed``, ``failed``, ``skipped``, or
        ``error``) means the task's L2 story is already told, in flight, queued, or
        deliberately declined, so this returns ``None`` and accepts nothing.

        And it *enqueues* rather than starts: the record lands as ``deferred`` and
        **no turn is submitted here**. Admission is the dispatch policy's decision
        (:mod:`orch_hub.eval_dispatch`), which weighs the subscription window a
        mechanical turn would draw on, and it deliberately does not run on this
        path — the caller is the run manager's end transition, holding its lock,
        where the only thing that may happen is one small durable write.

        The record is the guard, and it has to be: the completion signal driving
        this is per *run*, not per task (a task completed but not archived stays
        launchable, and the close-out-only re-run that follows classifies
        ``completed`` again), so the corpus needs a task-level once-guard rather
        than trusting the signal's cardinality. The one record per ``(repo, task)``
        this manager already keeps is exactly that: durable across hub restarts and
        atomically written. Which is also why a ``failed`` record blocks a retry
        here — recorded, not retried forever; the operator's manual
        :meth:`start_job` is the retry path.

        Returns the fresh ``deferred`` record when it did accept one. An unsafe
        ``task_id`` raises ``ValueError`` like every other public entry.
        """
        _validate_task_id(task_id)
        with self._lock:
            job_dir = self._job_dir_for(repo.name, task_id)
            if self._load_if_present(job_dir) is not None:
                return None
            return self._queue_locked(
                job_dir, repo, task_id, status=STATUS_DEFERRED, auto=True
            )

    def skip_if_absent(
        self, repo: ManagedRepo, task_id: str, reason: str
    ) -> SemanticJob | None:
        """Record that ``(repo, task_id)`` completed and gets **no** model turn.

        The opt-out's counterpart to :meth:`enqueue_if_absent`, and deliberately
        its exact twin: same once-per-task guard (any existing record wins and
        this accepts nothing), same durable one-record-per-``(repo, task)`` write,
        same inline O(1) cost on the completion path. The only difference is where
        the record comes to rest — terminal ``skipped`` with a bounded reason,
        instead of ``deferred`` awaiting a turn.

        That symmetry is the point rather than a convenience. A skip has to be a
        *record*: an operator may choose to spend a week's subscription budget on
        development instead of on measuring it, but a corpus whose missing entries
        left no trace would be a corpus that lies about its own denominator — and
        skips do not thin it evenly, they cluster on the expensive,
        remediation-heavy tail. So the skip lands in the same store, is counted by
        the same census, and is guarded by the same record.

        The guard also settles what happens when a multi-run task's runs disagree:
        whichever run *completes the task* writes first and decides, and the other
        direction is refused identically — a later completing run cannot enqueue
        over a skip, and cannot skip over an accepted job. A run that does not
        complete the task never reaches this path at all, which is why unchecking
        the box on session 1 of a three-session task decides nothing.

        ``reason`` must be one of :data:`SKIP_REASONS`; anything else raises
        ``ValueError`` rather than being persisted, so the vocabulary cannot drift
        into free text. No notification: unlike a failure, a skip is exactly what
        the operator asked for.
        """
        if reason not in SKIP_REASONS:
            raise ValueError(f"unknown semantic-job skip reason: {reason!r}")
        _validate_task_id(task_id)
        with self._lock:
            job_dir = self._job_dir_for(repo.name, task_id)
            if self._load_if_present(job_dir) is not None:
                return None
            return self._queue_locked(
                job_dir,
                repo,
                task_id,
                status=STATUS_SKIPPED,
                auto=True,
                skip_reason=reason,
            )

    def _queue_locked(
        self,
        job_dir: Path,
        repo: ManagedRepo,
        task_id: str,
        *,
        status: str,
        auto: bool,
        skip_reason: str | None = None,
    ) -> SemanticJob:
        """Persist a fresh record for this attempt; caller holds the lock.

        Shared by both start paths so they differ only in *when* they admit an
        attempt and in which state it starts — never in what an accepted record
        looks like. Persisting before anything else is what makes a hub that dies
        leave an observable record (an orphaned turn is reconciled to ``failed`` at
        startup; a held one simply keeps waiting) rather than a silent gap. The
        evaluator identity (backend/model/schema) is stamped now so the
        deferred/queued/running UI names the model before a report exists.

        The job id is this attempt's *creation* stamp plus a random token, and is
        not re-minted when a deferred attempt is later promoted: one acceptance is
        one attempt however long the policy holds it. The token is what makes the
        id an attempt identity rather than a second identity
        (:data:`_JOB_ID_TOKEN_BYTES`) — the finalize guards match on it, and a
        cancelled attempt's late worker must not be able to mistake its
        same-second replacement for itself.

        A record created directly in a terminal state (a ``skipped`` one — the only
        outcome reachable without an attempt) gets its ``ended_ts`` here, since
        nothing later will settle it; ``started_ts`` stays ``None`` because no turn
        ever began. It carries the evaluator identity like every other record: the
        useful reading of a skip is which evaluator's denominator lost this task,
        and stamping it keeps one record shape rather than two.
        """
        job_dir.mkdir(parents=True, exist_ok=True)
        info = self._evaluator.evaluator_info
        job_id = (
            f"{self._clock().strftime(_JOB_ID_TS)}"
            f"-{secrets.token_hex(_JOB_ID_TOKEN_BYTES)}"
        )
        ts = self._clock().isoformat()
        record = SemanticJob(
            job_id=job_id,
            repo_name=repo.name,
            repo_path=repo.path,
            task_id=task_id,
            status=status,
            backend=info.backend,
            model=info.model,
            schema_version=info.schema_version,
            skip_reason=skip_reason,
            auto=auto,
            created_ts=ts,
            updated_ts=ts,
            ended_ts=ts if status in _TERMINAL else None,
        )
        _write_record(job_dir, record)
        return record

    # -- deferred queue -------------------------------------------------------

    def dispatch_deferred(self, job: SemanticJob) -> SemanticJob | None:
        """Promote one held ``deferred`` record to ``queued`` and run its turn.

        The one door out of the deferred queue, called by the dispatch policy once
        it has decided the subscription window can afford this turn. It re-reads
        the record under the lock and promotes only a record that is *still* the
        same deferred attempt, so a manual :meth:`start_job` that superseded it
        while the policy was deciding is never clobbered and never double-run.

        Returns the promoted ``queued`` record, or ``None`` when the record moved
        on (superseded, already promoted, or gone). ``None`` is a normal outcome,
        not an error: the caller simply tries the next held job. A record whose
        repo cannot be reconstructed is refused here rather than dispatched with a
        guessed target — :meth:`fail_deferred` is how such a record stops being an
        invisible permanent resident of the queue.
        """
        repo = job.managed_repo()
        if repo is None:
            return None
        _validate_task_id(job.task_id)
        with self._lock:
            job_dir = self._job_dir_for(job.repo_name, job.task_id)
            current = self._load_if_present(job_dir)
            if (
                current is None
                or current.status != STATUS_DEFERRED
                or current.job_id != job.job_id
            ):
                return None
            ts = self._clock().isoformat()
            record = replace(
                current,
                status=STATUS_QUEUED,
                defer_reason=None,
                updated_ts=ts,
            )
            _write_record(job_dir, record)
        # Same off-lock submit contract as start_job (see there).
        self._submit(
            lambda: self._run_job(job_dir, record.job_id, repo, job.task_id)
        )
        return record

    def hold_deferred(self, job: SemanticJob, reason: str) -> SemanticJob | None:
        """Record *why* a held job is still waiting; a no-op when nothing changed.

        The deferred queue's only operator-facing content. The reason is a bounded
        machine token chosen by the policy (never operator prose, which would be
        unanalyzable and would leak repo content into a persisted surface), so a
        stalled queue can be read — and counted — rather than merely observed to be
        quiet.

        Writes only when the reason actually differs, because the policy re-decides
        on every sweep and a queue that is legitimately waiting for hours must not
        rewrite its own records every 30 seconds. Like :meth:`dispatch_deferred`,
        it touches only a record that is still the same deferred attempt.
        """
        _validate_task_id(job.task_id)
        with self._lock:
            job_dir = self._job_dir_for(job.repo_name, job.task_id)
            current = self._load_if_present(job_dir)
            if (
                current is None
                or current.status != STATUS_DEFERRED
                or current.job_id != job.job_id
                or current.defer_reason == reason
            ):
                return None
            ts = self._clock().isoformat()
            record = replace(current, defer_reason=reason, updated_ts=ts)
            _write_record(job_dir, record)
            return record

    def fail_deferred(self, job: SemanticJob, reason: str) -> SemanticJob | None:
        """Settle an undispatchable held job to ``failed`` (visible, not a silent stall).

        For a deferred record the dispatcher can never act on — one whose persisted
        repo attribution is gone, so no target can be resolved without guessing.
        Leaving it deferred would make it a permanent resident of the queue that
        every sweep skips and no operator has a reason to look at; failing it puts
        it where every other unrecoverable job outcome already is, and frees the
        task's slot for a manual retry.

        Notifies like any other fresh failure — it is the corpus losing a report.
        """
        _validate_task_id(job.task_id)
        with self._lock:
            job_dir = self._job_dir_for(job.repo_name, job.task_id)
            current = self._load_if_present(job_dir)
            if (
                current is None
                or current.status != STATUS_DEFERRED
                or current.job_id != job.job_id
            ):
                return None
            ts = self._clock().isoformat()
            record = replace(
                current,
                status=STATUS_FAILED,
                defer_reason=None,
                error=_bound_error(reason),
                ended_ts=ts,
                updated_ts=ts,
            )
            _write_record(job_dir, record)
            self._emit(semantic_job_notification(record))
            return record

    # -- operator cancel ------------------------------------------------------

    def cancel_job(self, repo: ManagedRepo, task_id: str) -> SemanticJob:
        """Settle this task's non-terminal job to ``cancelled``; return the record.

        The per-job counterpart of the queue-wide pause, modelled on
        :meth:`orch_hub.intake.IntakeManager.cancel_intake`: legal from
        :data:`_CANCELLABLE` (``deferred``, ``queued``, ``running``), refused from
        any other status (:class:`SemanticJobStateError` → 409), and 404 for a task
        that has no record at all. Like the intake cancel it writes nothing to the
        target repo, and it needs no live turn, so it also succeeds on a job whose
        worker died with the process that owned it.

        **The work stops before the record says it did.** The turn is signalled
        first and the terminal record is written only afterwards, so the outcome
        this reports is never ahead of the thing it reports on:

        - ``deferred`` — the queue simply loses a job it had not spent anything on.
        - ``queued`` — admitted but not yet begun. :meth:`_begin_running` refuses to
          promote a record that is no longer ``queued``, so the worker aborts and
          **no model turn opens**.
        - ``running`` — the model subprocess is killed through the attempt's
          registered canceller (:meth:`_stop_live_turn_locked`), so the turn stops
          spending the window the cancel was protecting. The worker's call raises,
          the evaluator's all-or-nothing cleanup removes the reserved evaluation
          dir, and its :meth:`_finalize` finds a non-transient record and writes
          nothing over this outcome — so a cancelled attempt leaves neither
          artifacts nor a published report behind the record that says it stopped.
        - ``running`` with no live turn in *this* process — a record orphaned by a
          previous hub. There is nothing left to kill and the record settles, which
          is honest rather than a compromise: the turn is not running here.

        And one refusal that is not about the record's status at all: a ``running``
        attempt whose **turn has already returned** is refused
        (:class:`SemanticJobStateError` → 409). Past that instant the window is
        spent and the worker is parsing, writing, publishing, and settling — work a
        cancel cannot undo and must not claim to have prevented. The attempt seals
        its canceller as the turn returns (:meth:`SemanticEvaluator._seal_turn
        <orch_hub.semantic.SemanticEvaluator._seal_turn>`) and stays registered
        until its terminal record is written, so the two decisions are taken under
        one lock and exactly one of them wins: either the cancel arrives first and
        nothing is persisted or published, or it arrives second and is told the
        attempt is settling. The alternative — accepting it — is precisely the
        divergence this refuses to write: a record reading ``cancelled`` beside a
        report standing in the evaluations tree and in the global feed.

        ``started_ts`` still distinguishes the two readings afterwards: a cancelled
        record carrying one is a turn that had opened; one without is a turn that
        never did.

        No notification. A cancel is the operator's own action, taken at the surface
        that shows the outcome — the same reason a ``skipped`` record is silent,
        and the opposite of a failure, which is the corpus shrinking without anyone
        asking. Terminal ``cancelled`` also holds the once-per-task guard, so the
        automatic path cannot re-enqueue what was just stopped; :meth:`start_job`
        remains the way to ask again.
        """
        _validate_task_id(task_id)
        with self._lock:
            job_dir = self._job_dir_for(repo.name, task_id)
            record = self._load_if_present(job_dir)
            if record is None:
                raise UnknownSemanticJobError(
                    f"no semantic job for repo {repo.name!r} task {task_id!r}"
                )
            if record.status not in _CANCELLABLE:
                raise SemanticJobStateError(
                    f"semantic job for task {task_id!r} cannot be cancelled from"
                    f" status {record.status!r}"
                )
            # Stop the work first: a kill that fails propagates from here, and a
            # turn already past stopping refuses below — either way the record is
            # left exactly as it was rather than claiming an outcome the hub did
            # not achieve.
            outcome = self._stop_live_turn_locked(job_dir, record)
            if outcome is CancelOutcome.TOO_LATE:
                raise SemanticJobStateError(
                    f"semantic job for task {task_id!r} finished its model turn"
                    " and is settling its result; it can no longer be cancelled"
                )
            ts = self._clock().isoformat()
            cancelled = replace(
                record,
                status=STATUS_CANCELLED,
                # The held job's waiting reason is no longer true and would read as
                # the cause of the cancel; the record's status is the cause.
                defer_reason=None,
                ended_ts=ts,
                updated_ts=ts,
            )
            _write_record(job_dir, cancelled)
            _log.info(
                "semantic job %s for %s/%s cancelled from %s (live turn %s)",
                record.job_id,
                record.repo_name,
                record.task_id,
                record.status,
                "stopped" if outcome is CancelOutcome.SIGNALLED else "none",
            )
            return cancelled

    def list_jobs(self) -> list[SemanticJob]:
        """Every persisted job record, one walk of the tree, deterministic order.

        The cross-cutting read: the dispatch policy needs the whole set at once
        (which jobs are held, and when any turn last started — a manual one
        consumes the same window as an automatic one), and an operator view of the
        queue needs the same walk. Reads only; an unreadable record surfaces as its
        ``error`` record rather than being skipped, so a broken dir cannot hide.
        """
        with self._lock:
            return [_load_record(job_dir) for job_dir in self._job_dirs()]

    def _run_job(
        self, job_dir: Path, job_id: str, repo: ManagedRepo, task_id: str
    ) -> None:
        """Off-lock evaluation turn: flip to running, evaluate, then finalize.

        The queued→running flip re-acquires the lock briefly so a poll can observe
        the ``running`` state; the (slow) evaluation itself runs OFF the lock so it
        blocks no read; the terminal write re-acquires the lock. That flip is also
        what bounds the attempt, and the evaluator is handed the half of the bound
        it has to honour (:func:`_turn_start_by`): finish the evidence build in
        time to open the model turn, or open none.

        The other bound on the turn is the operator's: this mints the attempt's
        :class:`~orch_hub.subprocess_utils.CommandCanceller`, registers it as the
        live handle for this ``(repo, task)`` for the whole attempt, and hands it
        to the evaluator — so :meth:`cancel_job` kills the model subprocess rather
        than relabelling a record whose turn keeps spending. Any evaluation
        failure (backend/model/process error, a cancel, a non-conforming report, a
        disabled subsystem, an unsafe id that slipped through) becomes a clean
        ``failed`` job with a bounded diagnostic — the evaluator's own
        all-or-nothing cleanup has already removed any partial artifacts, so the
        failed job leaves no half-written evaluation and never mutates target
        state.

        "The whole attempt" is the load-bearing word in that registration. The
        handle is not just a way to kill a child: it is where the attempt's
        cancellable/settled boundary lives (the evaluator seals it as the turn
        returns), so it stays registered until this attempt's terminal record is
        written. Between the turn returning and the record settling, a cancel then
        meets a *sealed* handle and is refused — instead of meeting no handle,
        being accepted, and writing ``cancelled`` over an evaluation the worker
        had already persisted and published.
        """
        cancel = CommandCanceller()
        running = self._begin_running(job_dir, job_id, cancel)
        if running is None:
            return
        try:
            try:
                report = self._evaluator.evaluate(
                    repo,
                    task_id,
                    turn_start_by=_turn_start_by(running),
                    cancel=cancel,
                )
            except Exception as exc:  # noqa: BLE001 - any failure is a failed job
                self._finalize(
                    job_dir,
                    job_id,
                    status=STATUS_FAILED,
                    error=_bound_error(f"semantic evaluation failed: {exc}"),
                )
                return
            self._finalize(
                job_dir,
                job_id,
                status=STATUS_COMPLETED,
                evaluation_id=report.evaluation_id,
                backend=report.evaluator.backend,
                model=report.evaluator.model,
                schema_version=report.evaluator.schema_version,
            )
        finally:
            # AFTER the terminal write, not after the turn. The handle is what
            # carries the attempt's seal, and a cancel that found no handle would
            # be accepted — so releasing between the turn and the record would
            # re-open, for exactly the width of a finalize, the window in which a
            # ``cancelled`` record can be written over an evaluation that was
            # already persisted and published.
            self._release_attempt(job_dir, job_id)

    def _begin_running(
        self, job_dir: Path, job_id: str, cancel: CommandCanceller
    ) -> SemanticJob | None:
        """Flip a still-``queued`` job to ``running`` under the lock; else no-op.

        Returns ``None`` (so the turn aborts before running the model) when the
        record no longer represents this attempt as ``queued``. That covers the
        defensive case — the duplicate guard already prevents a second attempt
        from starting while this one is transient — and the one case that is not
        defensive at all: :meth:`reconcile_stuck` may have settled this record
        while its turn sat in the executor, and a worker must never start a model
        turn against a record already reported as failed.

        This is also where the attempt acquires its bound. The returned running
        record carries ``deadline_ts`` — ``started_ts`` plus the whole-attempt span
        (:meth:`_attempt_bound`) — persisted here, once, so the sweep and the
        worker read the same instant instead of each deriving one. With the turn
        deadline disabled there is no bound to derive and the field stays ``None``,
        which is also what turns the sweep off for this record.

        It is likewise where the attempt's canceller is published, in the same
        locked section as the ``running`` write and never before it. Registering
        earlier would advertise a stoppable turn to :meth:`cancel_job` while this
        worker might still abort (the guard below), and registering later would
        leave a window in which the record says ``running`` and no handle reaches
        it — so the two facts are made true together.
        """
        with self._lock:
            record = self._load_if_present(job_dir)
            if record is None or record.status != STATUS_QUEUED or record.job_id != job_id:
                return None
            now = self._clock()
            ts = now.isoformat()
            bound = self._attempt_bound()
            running = replace(
                record,
                status=STATUS_RUNNING,
                started_ts=ts,
                deadline_ts=(
                    (now + dt.timedelta(seconds=bound)).isoformat() if bound else None
                ),
                updated_ts=ts,
            )
            _write_record(job_dir, running)
            self._live[job_dir] = (job_id, cancel)
            return running

    def _release_attempt(self, job_dir: Path, job_id: str) -> None:
        """Withdraw this attempt's cancel handle once its record is terminal.

        Not once its *turn* returns: the handle carries the attempt's settled
        seal, so dropping it early would make a cancel arriving during the
        finalize meet nothing and be accepted (:meth:`_run_job`).

        Keyed on ``job_id`` as well as the dir so a late release can never drop a
        *newer* attempt's handle: after a cancel, an operator may already have
        started the replacement, and unregistering it would leave that live turn
        unstoppable — the same superseding-attempt care every other guard here
        takes, in the one place where being late is normal rather than defensive.
        """
        with self._lock:
            live = self._live.get(job_dir)
            if live is not None and live[0] == job_id:
                del self._live[job_dir]

    def _stop_live_turn_locked(
        self, job_dir: Path, record: SemanticJob
    ) -> CancelOutcome:
        """Reach this record's in-flight attempt, if this process is running one.

        Returns what the attempt's canceller reports
        (:class:`~orch_hub.subprocess_utils.CancelOutcome`), which is the decision
        :meth:`cancel_job` acts on:

        - ``SIGNALLED`` — a live model subprocess was killed.
        - ``NOTHING_TO_STOP`` — the cancel took, with nothing to kill. Not a
          failure, and it covers most shapes: a ``deferred``/``queued`` record
          whose turn has not begun, a ``running`` record orphaned by the process
          that owned it, and an attempt still in its pre-model evidence build (the
          canceller's flag stops it at the evaluator's checkpoint, before any
          window is spent). Reported for a record this process holds no handle for
          at all, since there is likewise nothing here to stop.
        - ``TOO_LATE`` — the attempt sealed its result as the turn returned and is
          now persisting/publishing/settling it. The cancel is refused rather than
          recorded, because the work it names is already done.

        A kill that *fails* is not swallowed: it propagates out of
        :meth:`cancel_job` before the terminal record is written, so a turn the hub
        could not stop is never reported as cancelled.

        The caller holds the lock, which is what makes this exact rather than
        best-effort: the registration and the record it belongs to are written
        under the same lock, so the pair cannot be observed half-updated. The
        cancel-versus-seal decision inside the canceller is exact for its own
        reason — one lock, two one-shot transitions — and does not need this one.
        """
        live = self._live.get(job_dir)
        if live is None or live[0] != record.job_id:
            return CancelOutcome.NOTHING_TO_STOP
        return live[1].cancel()

    def _attempt_bound(self) -> int:
        """Seconds one whole attempt may take, or ``0`` when it is unbounded.

        The model subprocess's configured deadline plus the two enforced allowances
        bracketing it (:data:`EVIDENCE_GRACE_SECONDS`, :data:`SETTLE_GRACE_SECONDS`).
        ``Config.eval_turn_timeout`` of ``0`` disables the deadline, and with it the
        whole bound: with nothing bounding the model turn there is no instant at
        which the rest of the attempt can be called overdue either.
        """
        timeout = self._config.eval_turn_timeout
        if timeout <= 0:
            return 0
        return EVIDENCE_GRACE_SECONDS + timeout + SETTLE_GRACE_SECONDS

    def _finalize(
        self,
        job_dir: Path,
        job_id: str,
        *,
        status: str,
        evaluation_id: str | None = None,
        error: str | None = None,
        backend: str | None = None,
        model: str | None = None,
        schema_version: str | None = None,
    ) -> None:
        """Settle a finished turn to its terminal state (under the lock).

        Re-loads and only settles a record still transient and still this attempt
        (``job_id`` match): the duplicate guard means no superseding attempt can
        exist while this turn runs, so this is defensive — it never clobbers a
        record some other attempt owns. ``completed`` carries the fresh
        ``evaluation_id`` (the link the dashboard selects) and re-affirms the
        evaluator identity from the report; ``failed`` carries the bounded
        diagnostic. The creation-time backend/model/schema are preserved when a
        finalize does not pass them (the ``failed`` path), so a failed job still
        names which model it tried.

        This is also the single semantic-job notification trigger, and the guard
        above is what makes it fire exactly once per settled job: only the write
        that actually moves a transient record to its terminal state reaches the
        emit, so a duplicate/superseded finalize (which returns early) and the
        transient ``queued``/``running`` writes notify nothing. Startup
        reconciliation writes ``failed`` without going through here, so a restart
        sweep — not a fresh outcome the operator is waiting on — stays silent.

        One further silence, and it is a policy rather than a guard: an
        ``auto`` job settling to ``completed`` notifies nothing. That outcome used
        to be rare and requested; since evaluation became mechanical it happens on
        every completed task, carries nothing to act on, and would arrive right
        behind the run-end push that does. An automatic *failure* still notifies —
        it is the corpus quietly shrinking, which is exactly what nobody would
        otherwise notice.
        """
        with self._lock:
            record = self._load_if_present(job_dir)
            if record is None or record.status not in _TRANSIENT or record.job_id != job_id:
                return
            ts = self._clock().isoformat()
            settled = replace(
                record,
                status=status,
                evaluation_id=evaluation_id,
                error=error,
                backend=backend if backend is not None else record.backend,
                model=model if model is not None else record.model,
                schema_version=(
                    schema_version
                    if schema_version is not None
                    else record.schema_version
                ),
                ended_ts=ts,
                updated_ts=ts,
            )
            _write_record(job_dir, settled)
            # Settled to a terminal state: notify best-effort, off the lock. The
            # guard above returns before this, so no duplicate settle re-notifies.
            if settled.auto and settled.status == STATUS_COMPLETED:
                return
            self._emit(semantic_job_notification(settled))

    # -- startup reconciliation -----------------------------------------------

    def reconcile_orphans(self) -> list[SemanticJob]:
        """Fail every orphaned transient job; return what was reconciled.

        A persisted ``queued``/``running`` record with no live turn is an orphan
        from a previous hub process that died mid-turn: no thread will finalize it,
        so it would hold its ``(repo, task)`` duplicate slot forever (a fresh
        :meth:`start_job` would keep returning the stuck job and never retry). This
        marks each such record ``failed`` (hub restart) so the slot frees and the
        dashboard shows a clear, retryable failure.

        It must run once at startup, before any new job is submitted (the FastAPI
        lifespan calls it, mirroring the intake manager's reconciliation); at that
        point every transient record on disk is unambiguously an orphan. It never
        mutates target repo/run state — it only rewrites hub-owned job records.

        ``deferred`` records are untouched, and that is load-bearing rather than
        incidental: a held job has no abandoned turn to clean up, so failing it
        would delete the queue on every restart — the deferred queue's whole value
        is that it survives one. Nothing else needs to happen here for it either;
        the resident dispatch sweep picks a held job back up on its own schedule,
        so recovery needs no startup step of its own.
        """
        reconciled: list[SemanticJob] = []
        with self._lock:
            for job_dir in self._job_dirs():
                record = _load_record(job_dir)
                if record.status not in _TRANSIENT:
                    continue
                ts = self._clock().isoformat()
                updated = replace(
                    record,
                    status=STATUS_FAILED,
                    error=_bound_error(
                        f"semantic evaluation abandoned (hub restarted during"
                        f" {record.status})"
                    ),
                    ended_ts=ts,
                    updated_ts=ts,
                )
                _write_record(job_dir, updated)
                reconciled.append(updated)
        return reconciled

    def reconcile_stuck(self) -> list[SemanticJob]:
        """Fail every in-flight job that has outlived its bound; return what settled.

        The resident counterpart of :meth:`reconcile_orphans`, and the two split on
        *when* a transient record can be judged rather than on what they do to it.
        The startup sweep can settle any transient record it finds, because a hub
        that is only now starting has no worker that could own one. This one runs
        while workers exist, so it cannot use presence as the test and needs a
        clock instead — and it runs on the resident monitor tick, so a job that
        gets stuck at 09:00 stops holding its ``(repo, task)`` slot the same
        morning rather than at the next restart.

        The bound is the attempt's own, not one this sweep invents: a running
        record carries the ``deadline_ts`` its worker was handed at the
        queued→running flip (:meth:`_begin_running`), and the worker is refused a
        model turn late enough to outlive it (:func:`_turn_start_by`). So a record
        past that instant is one whose model subprocess deadline expired at least
        :data:`SETTLE_GRACE_SECONDS` ago — which is what makes settling it sound
        rather than merely likely-safe. Re-deriving the limit here from
        ``started_ts`` plus a fixed grace would drop exactly that guarantee: an
        evidence build slower than the grace would leave a live turn, still inside
        an unexpired model deadline, to be reaped as stuck.

        A transient record with no deadline falls back to the current config's span
        from :func:`_attempt_instant` (:func:`_reap_instant` says which records
        those are and why the fallback is safe for them). A record whose stamps are
        all unreadable is left alone rather than settled on an unprovable age (the
        startup sweep will catch it). With the turn deadline disabled (``0``) the
        sweep does nothing at all: without it there is no non-arbitrary line
        between a hung turn and a long one, and inventing one is exactly how a
        legitimate evaluation gets killed.

        A settled record's worker may still be alive — in a phase with no bound of
        its own (a wedged evidence build, a post-kill drain) or persisting past its
        settle grace — and that is accepted, because the consequences are
        contained. That worker's own :meth:`_finalize` finds a non-transient record
        and returns without clobbering this outcome; its evaluation was read-only
        over target state throughout; and if it does eventually succeed, its
        artifacts are durable and publishable exactly as they would have been —
        only this record, which reports the *attempt*, no longer points at them.
        The alternative — holding the slot until someone notices and restarts the
        hub — is the failure this exists to end.

        Failing here notifies, unlike the startup sweep: this *is* a fresh outcome
        arriving while the operator is running, the same event a backend failure
        would have pushed had the turn managed to fail on its own.
        """
        bound = self._attempt_bound()
        if bound <= 0:
            return []
        settled: list[SemanticJob] = []
        with self._lock:
            now = self._clock()
            for job_dir in self._job_dirs():
                record = _load_record(job_dir)
                if record.status not in _TRANSIENT:
                    continue
                deadline = _reap_instant(record, bound)
                if deadline is None or now < deadline:
                    continue
                ts = now.isoformat()
                updated = replace(
                    record,
                    status=STATUS_FAILED,
                    error=_bound_error(
                        f"semantic evaluation stuck in {record.status}: its attempt"
                        f" deadline ({deadline.isoformat()}) passed, so the turn was"
                        f" abandoned and its slot freed"
                    ),
                    ended_ts=ts,
                    updated_ts=ts,
                )
                _write_record(job_dir, updated)
                _log.warning(
                    "semantic job %s for %s/%s abandoned: stuck in %s past its"
                    " attempt deadline %s",
                    record.job_id,
                    record.repo_name,
                    record.task_id,
                    record.status,
                    deadline.isoformat(),
                )
                self._emit(semantic_job_notification(updated))
                settled.append(updated)
        return settled

    # -- inspection -----------------------------------------------------------

    def get_job(self, repo: ManagedRepo, task_id: str) -> SemanticJob:
        """Return the current semantic job for ``(repo, task_id)``; else raise.

        The dashboard's poll target and page-load state read: one deterministic
        job.json load, so it reflects a running turn or a recently-failed/completed
        attempt without a directory scan. Raises :class:`UnknownSemanticJobError`
        when the task has never had a semantic job (the API maps it to a
        ``{"job": null}`` 200 so a never-evaluated task is not an error). An unsafe
        ``task_id`` raises ``ValueError`` (mapped to 400).
        """
        _validate_task_id(task_id)
        with self._lock:
            job_dir = self._job_dir_for(repo.name, task_id)
            record = self._load_if_present(job_dir)
            if record is None:
                raise UnknownSemanticJobError(
                    f"no semantic job for repo {repo.name!r} task {task_id!r}"
                )
            return record

    def find_job(self, repo: ManagedRepo, task_id: str) -> SemanticJob | None:
        """Return the current semantic job for ``(repo, task_id)``, or ``None``.

        The non-raising counterpart of :meth:`get_job` for the API's page-load
        read, where a never-evaluated task is the normal ``{"job": null}`` case
        rather than a 404. An unsafe ``task_id`` still raises ``ValueError``.
        """
        try:
            return self.get_job(repo, task_id)
        except UnknownSemanticJobError:
            return None

    # -- internals ------------------------------------------------------------

    def _submit(self, run: Callable[[], None]) -> None:
        """Run one evaluation turn's off-lock work through the injected executor.

        MUST be called outside ``self._lock``: ``run`` re-acquires the lock to flip
        to running / finalize, and the non-reentrant lock would deadlock a
        synchronous executor otherwise. The default thread executor detaches ``run``
        so :meth:`start_job` returns at once with the transient record.
        """
        self._executor(run)

    def _emit(self, notification: Notification) -> None:
        """Deliver a notification best-effort; never raise into job state.

        Mirrors :meth:`orch_hub.intake.IntakeManager._emit`. Isolation is the
        invariant: a backend failure (network down, bad Bark key) must not
        corrupt the just-written ``job.json`` — the terminal record is already
        durable when this runs. It is called from :meth:`_finalize` under the
        manager lock, so delivery runs through the injected dispatch (a daemon
        thread in production) off the lock, and the backend call is wrapped so any
        exception is logged, not propagated. A ``None`` notifier short-circuits —
        no dispatch work at all.
        """
        notifier = self._notifier
        if notifier is None:
            return

        def deliver() -> None:
            try:
                notifier.notify(notification)
            except Exception as exc:  # noqa: BLE001 - delivery is best-effort
                # Log the exception *type* and the (non-secret) title only; a
                # Bark URL embeds a device key, so never log transport detail.
                _log.warning(
                    "semantic-job notification delivery failed (%s): %s",
                    type(exc).__name__,
                    notification.title,
                )

        self._notify_dispatch(deliver)

    def _job_dir_for(self, repo_name: str, task_id: str) -> Path:
        """Deterministic contained job dir for one ``(repo, task)`` (pure path math).

        ``<data_dir>/semantic-jobs/<repo-slug>/<task-id>/`` — both components are
        validated single safe segments via :func:`_safe_segment` (mirroring
        :func:`orch_hub.evaluation.task_evaluations_dir`): the repo name is slugged
        *and then* validated (a slug can still be a bare ``.`` — e.g. a repo named
        ``.`` — which would collapse the ``<repo-slug>/`` level and drop the record
        where the two-level :meth:`reconcile_orphans` scan can't reach it, stranding
        a crashed transient job's duplicate slot forever), and the task id is
        re-validated even though the public entry already checked it. As a final
        defense the resolved path is asserted to stay under the resolved jobs base,
        so neither a crafted task id nor repo name can steer a read/write outside
        the semantic-jobs tree. Creates nothing.
        """
        base = self.jobs_dir
        repo_seg = _safe_segment(_slug(repo_name), label="repo")
        task_seg = _safe_segment(task_id, label="task id")
        target = base / repo_seg / task_seg
        if not target.resolve().is_relative_to(base.resolve()):
            raise ValueError(f"semantic-jobs path escapes {base}: {target}")
        return target

    def _job_dirs(self) -> list[Path]:
        """Every ``<repo-slug>/<task-id>/`` dir holding a job record (for reconcile).

        Two-level walk of the jobs tree (repo slug, then task id), returning only
        dirs that actually contain a ``job.json``. An absent tree is an empty list.
        """
        base = self.jobs_dir
        if not base.is_dir():
            return []
        dirs: list[Path] = []
        for repo_dir in sorted(base.iterdir()):
            if not repo_dir.is_dir():
                continue
            for task_dir in sorted(repo_dir.iterdir()):
                if task_dir.is_dir() and (task_dir / SEMANTIC_JOB_NAME).is_file():
                    dirs.append(task_dir)
        return dirs

    def _load_if_present(self, job_dir: Path) -> SemanticJob | None:
        """Load a job record, or ``None`` when the task has no job dir/file yet."""
        if not (job_dir / SEMANTIC_JOB_NAME).is_file():
            return None
        return _load_record(job_dir)


# -- persistence --------------------------------------------------------------


def _turn_start_by(record: SemanticJob) -> dt.datetime | None:
    """The instant this attempt's model turn must have started by, or ``None``.

    The first mark on the attempt timeline: ``started_ts`` plus
    :data:`EVIDENCE_GRACE_SECONDS`. Handed to
    :meth:`orch_hub.semantic.SemanticEvaluator.evaluate`, which refuses to open a
    model turn past it — the enforcement that keeps the subprocess deadline inside
    the persisted ``deadline_ts``, so the sweep can never reap a turn that is still
    within its own bound.

    Derived from the same persisted ``started_ts`` the deadline was computed from,
    so the two marks cannot drift apart. ``None`` when the attempt is unbounded
    (no ``deadline_ts``) or its start stamp will not parse — in both cases there is
    no window to enforce, which is exactly the state in which the sweep also stands
    down.
    """
    if not record.deadline_ts:
        return None
    started = _parse_ts(record.started_ts)
    if started is None:
        return None
    return started + dt.timedelta(seconds=EVIDENCE_GRACE_SECONDS)


def _write_record(job_dir: Path, record: SemanticJob) -> None:
    """Persist ``job.json`` atomically (same-dir tmp + rename), like run.json."""
    path = job_dir / SEMANTIC_JOB_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record.to_dict(), indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _load_record(job_dir: Path) -> SemanticJob:
    """Load ``job.json``; an unreadable one yields an ``error`` record.

    An error record surfaces a broken dir instead of hiding it, and — carrying no
    repo attribution and a terminal ``error`` status — never holds a
    ``(repo, task)`` duplicate slot and is skipped by reconciliation (mirrors the
    intake manager's error record). The persisted shape is exactly
    :meth:`SemanticJob.to_dict`, so this round-trips it field-for-field.
    """
    path = job_dir / SEMANTIC_JOB_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _error_record(job_dir, "missing job.json")
    except (OSError, ValueError) as err:
        return _error_record(job_dir, f"unreadable job.json ({err})")
    if not isinstance(data, dict):
        return _error_record(job_dir, "job.json is not a JSON object")
    return SemanticJob(
        job_id=_str_field(data, "job_id") or job_dir.name,
        repo_name=_str_field(data, "repo_name") or "",
        repo_path=_path_field(data, "repo_path"),
        task_id=_str_field(data, "task_id") or job_dir.name,
        status=_str_field(data, "status") or STATUS_ERROR,
        backend=_str_field(data, "backend"),
        model=_str_field(data, "model"),
        schema_version=_str_field(data, "schema_version"),
        evaluation_id=_str_field(data, "evaluation_id"),
        error=_str_field(data, "error"),
        defer_reason=_str_field(data, "defer_reason"),
        skip_reason=_str_field(data, "skip_reason"),
        auto=_bool_field(data, "auto"),
        created_ts=_str_field(data, "created_ts"),
        updated_ts=_str_field(data, "updated_ts"),
        started_ts=_str_field(data, "started_ts"),
        deadline_ts=_str_field(data, "deadline_ts"),
        ended_ts=_str_field(data, "ended_ts"),
    )


def _error_record(job_dir: Path, error: str) -> SemanticJob:
    return SemanticJob(
        job_id=job_dir.name,
        repo_name="",
        repo_path=None,
        task_id=job_dir.name,
        status=STATUS_ERROR,
        error=error,
    )


def _str_field(data: dict, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _bool_field(data: dict, key: str) -> bool:
    """Read a boolean field; anything else (absent, null, malformed) is ``False``.

    ``auto`` is newer than the records already on disk, and every one of those was
    written by the operator-driven start path — so absent reads as the truth for
    that cohort rather than as a guess."""
    value = data.get(key)
    return value is True


def _path_field(data: dict, key: str) -> Path | None:
    value = _str_field(data, key)
    return Path(value) if value else None
