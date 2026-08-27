"""Async semantic-evaluation job lifecycle: deferred / queued / running / completed / failed.

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
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .config import Config
from .models import ManagedRepo, SemanticJob
from .notifications import Notification, Notifier, semantic_job_notification
from .semantic import SemanticEvaluator

_log = logging.getLogger(__name__)

SEMANTIC_JOBS_SUBDIR = "semantic-jobs"
"""Top-level hub-data-dir subtree holding semantic-job records, parallel to the
run manager's ``runs/`` and the intake manager's ``intake/`` trees."""

SEMANTIC_JOB_NAME = "job.json"
"""The persisted job record, one per ``<repo-slug>/<task-id>/`` dir."""

# Lifecycle statuses. The transient pair brackets an in-flight evaluation turn;
# the terminal pair is a resting outcome. ``error`` is reserved for an unreadable
# record (a broken dir that must surface, not hide), distinct from ``failed`` (a
# legitimate evaluation-failure outcome), and — carrying no repo attribution —
# never holds a ``(repo, task)`` duplicate slot. Mirrors the intake statuses.
STATUS_DEFERRED = "deferred"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_ERROR = "error"

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

_TERMINAL = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_ERROR})
"""Resting states: a new attempt may overwrite a job in one of these."""

_JOB_ID_TS = "%Y%m%dT%H%M%SZ"
"""Job-id stamp format (matches the run/eval id stamps). The id is the attempt's
start stamp — per-attempt identity for the client to detect a superseded attempt
across polls; correctness of the finalize guard rests on the status, not on this
id being globally unique (see :meth:`SemanticJobManager._finalize`)."""

_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
"""Repo-name slug rule (matches ``runs._slug``/``evaluation._SLUG``): collapse any
run of unsafe chars to ``-`` (a local copy, so this module stays independent)."""

_ERROR_MAX_CHARS = 2000
"""Ceiling on a persisted failure diagnostic. The backend already bounds its
stderr tail, and parse/schema errors are short, so this is a defensive second cap
that guarantees the scope's "no unbounded model output" even if an unexpected
exception carries a large message."""


class SemanticJobError(Exception):
    """Base class for semantic-job manager operational errors."""


class UnknownSemanticJobError(SemanticJobError):
    """No semantic job exists for the given ``(repo, task_id)``."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


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

        Overriding a ``deferred`` record is the point, not an oversight: the
        capacity policy exists to keep *mechanical* evaluation out of the window
        the operator is working in, and an operator asking for this evaluation now
        is not the production-side selection that policy shapes. So this path
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
        record (deferred, transient, ``completed``, ``failed``, or ``error``) means
        the task's L2 story is already told, in flight, or queued, so this returns
        ``None`` and accepts nothing.

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

    def _queue_locked(
        self,
        job_dir: Path,
        repo: ManagedRepo,
        task_id: str,
        *,
        status: str,
        auto: bool,
    ) -> SemanticJob:
        """Persist a fresh record for this attempt; caller holds the lock.

        Shared by both start paths so they differ only in *when* they admit an
        attempt and in which state it starts — never in what an accepted record
        looks like. Persisting before anything else is what makes a hub that dies
        leave an observable record (an orphaned turn is reconciled to ``failed`` at
        startup; a held one simply keeps waiting) rather than a silent gap. The
        evaluator identity (backend/model/schema) is stamped now so the
        deferred/queued/running UI names the model before a report exists.

        The job id is this attempt's *creation* stamp and is not re-minted when a
        deferred attempt is later promoted: one acceptance is one attempt however
        long the policy holds it, and the finalize guards key off the status, not
        the id's recency.
        """
        job_dir.mkdir(parents=True, exist_ok=True)
        info = self._evaluator.evaluator_info
        job_id = self._clock().strftime(_JOB_ID_TS)
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
            auto=auto,
            created_ts=ts,
            updated_ts=ts,
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
        blocks no read; the terminal write re-acquires the lock. Any evaluation
        failure (backend/model/process error, a non-conforming report, a disabled
        subsystem, an unsafe id that slipped through) becomes a clean ``failed``
        job with a bounded diagnostic — the evaluator's own all-or-nothing cleanup
        has already removed any partial artifacts, so the failed job leaves no
        half-written evaluation and never mutates target state.
        """
        if not self._begin_running(job_dir, job_id):
            return
        try:
            report = self._evaluator.evaluate(repo, task_id)
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

    def _begin_running(self, job_dir: Path, job_id: str) -> bool:
        """Flip a still-``queued`` job to ``running`` under the lock; else no-op.

        Returns False (so the turn aborts before running the model) when the
        record no longer represents this attempt as ``queued`` — a defensive check:
        the duplicate guard already prevents a second attempt from starting while
        this one is transient, so in practice the record is always this attempt's
        ``queued`` here.
        """
        with self._lock:
            record = self._load_if_present(job_dir)
            if record is None or record.status != STATUS_QUEUED or record.job_id != job_id:
                return False
            ts = self._clock().isoformat()
            _write_record(
                job_dir,
                replace(record, status=STATUS_RUNNING, started_ts=ts, updated_ts=ts),
            )
            return True

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
        auto=_bool_field(data, "auto"),
        created_ts=_str_field(data, "created_ts"),
        updated_ts=_str_field(data, "updated_ts"),
        started_ts=_str_field(data, "started_ts"),
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
