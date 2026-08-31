"""Automatic Layer 2 dispatch policy: when a mechanical evaluation may spend a turn.

Completion-triggered evaluation (:mod:`orch_hub.completion_eval`) accepts every
completed task into the semantic-job store as ``deferred``. This module decides
when a held job is allowed to become a model turn. The two concerns are split on
purpose: the job manager owns durable lifecycle and knows nothing about
subscription usage, and this owns policy and persists nothing of its own.

Cost is not what makes automatic evaluation risky. One Layer 2 turn is a bounded
``claude -p`` call — measured at ~32k input tokens against tasks that consumed
several dev sessions of up to 200k context each. *Contention* is the risk: that
turn draws on the same subscription rate window the next dev session needs, so a
turn fired at the wrong moment spends the work budget on measuring the work.

The policy is three rules, all configurable (:mod:`orch_hub.config`) because a
threshold nobody can tune is a threshold nobody can be wrong about out loud:

1. **A utilization ceiling.** Above ``eval_usage_ceiling`` on the evaluator
   provider's highest currently-measured window, a held job stays held.
2. **Fail toward deferral.** Missing, stale, rolled-over, or otherwise
   unreadable usage defers — an unknown window is not an empty window. This is
   deliberately the *permanent* failure mode when a usage feed dies: the queue
   stops, visibly, rather than quietly spending a window nobody can see.
3. **Spacing between turns.** A ceiling plus a queue has a failure mode the
   ceiling alone does not: under sustained pressure the queue accumulates and
   then becomes dispatchable *all at once* when the window rolls, spending the
   fresh window's opening capacity exactly when the operator returns to work.
   Single-flight prevents double-spending one task, never a herd across tasks.
   So at most one held job is dispatched per ``eval_dispatch_spacing``, measured
   from the last turn admitted to spend — manual turns included, since they
   consume the same window, and counting from *admission* rather than from the
   worker's own start stamp, which is written a moment later and off the lock.

None of it applies to an operator asking for an evaluation now
(:meth:`orch_hub.semantic_jobs.SemanticJobManager.start_job`), which is both the
right semantics — this task exists to remove *production-side* selection, and a
human choosing to look is not that — and the escape hatch when a broken usage
feed would otherwise hold the queue indefinitely.

All of it is subordinate to the two switches that decide whether the automatic
path exists at all (``evaluation_enabled`` and ``eval_auto``, jointly
:attr:`orch_hub.config.Config.auto_evaluation_enabled`). The queue is durable, so
records accepted by an earlier process outlive it and a sweep that ignored the
switches would promote them anyway; :meth:`DeferredDispatcher.sweep` therefore
checks them before anything else and simply holds.

Beside them sits the one hold an operator can actually reach at the moment they
want it: the persisted pause (:mod:`orch_hub.eval_control`), checked in the same
place and with the same effect on the queue. It is a separate lever rather than a
live-editable ``eval_auto`` because the two mean different things — the switch
stops completions being *accepted*, permanently losing every task that completes
while it is off, whereas a pause holds only the drain and costs the corpus nothing
but time.

The sweep is driven by the resident run monitor and by the completion hook's own
off-lock executor, so a completion arriving in a quiet window becomes a turn
within milliseconds while a held queue is retried for as long as it takes. It is
idempotent and stateless: every decision is re-derived from the persisted records,
the current pause state, and the current usage read, so a hub restart resumes the
queue without a startup step and without a second thing to keep in sync.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .config import Config
from .models import (
    PROVIDER_CLAUDE,
    EvaluationControl,
    EvaluationQueueEntry,
    EvaluationQueueSnapshot,
    ProviderUsage,
    SemanticJob,
)
from .semantic_jobs import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DEFERRED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUSES,
    SemanticJobManager,
)
from .usage import current_used_percent

_log = logging.getLogger(__name__)

REASON_CAPACITY = "capacity"
"""Held because the provider's utilization is at or above the configured ceiling."""

REASON_USAGE_UNKNOWN = "usage-unknown"
"""Held because current utilization could not be read at all.

Missing, unavailable, stale, or rolled-over figures all land here. Kept distinct
from :data:`REASON_CAPACITY` because they call for opposite responses: a real
ceiling hit resolves itself when the window rolls, while an unreadable feed
resolves itself never and is the operator's problem to fix."""

REASON_PACING = "pacing"
"""Held because another turn was admitted less than the configured spacing ago."""

QUEUE_ENTRY_LIMIT = 20
"""Ceiling on the rows :func:`queue_snapshot` returns (counts stay complete).

An operator surface has to fit on a phone, and the queue's pathological shapes
are unbounded: a usage feed that died a week ago holds one row per completed task
since, and ``failed`` records are terminal, so they accumulate for the life of the
store. Capping the rows keeps the surface readable; keeping ``counts`` a full
census keeps it honest, which is the half that actually matters here — a figure
that quietly meant "the first twenty" would report a twenty-deep queue as calm,
which is exactly the silence this surface exists to break. ``truncated`` says when
the cap bit."""

UNATTRIBUTABLE_REASON = (
    "semantic evaluation abandoned (deferred job carries no repo attribution)"
)
"""Diagnostic for a held record whose persisted repo is gone, so no target can be
resolved without guessing. Settled ``failed`` rather than left in the queue."""

ControlReader = Callable[[], EvaluationControl]
"""Reads the hub-owned automatic-dispatch pause switch.

Wired to :meth:`orch_hub.eval_control.EvaluationControlStore.state`; a plain
callable for the same reason :data:`UsageReader` is one — this module consumes one
value and must not acquire the ability to write the control. Defaults to
:func:`_never_paused` so a dispatcher built without one simply is not paused
(which is "no control store attached", a different thing from "the control store
could not be read" — the store itself decides that one, and it fails toward
paused)."""


def _never_paused() -> EvaluationControl:
    return EvaluationControl()


UsageReader = Callable[[], ProviderUsage]
"""Reads the evaluator provider's current subscription snapshot.

Wired to :meth:`orch_hub.usage.UsageService.provider_usage` for the provider the
Layer 2 backend actually spends against; tests inject a fake so no test depends
on the operator's real subscription data. Deliberately a plain callable rather
than the service itself: this module consumes one display-only snapshot and must
never acquire the ability to trigger, write, or interpret usage state."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_ts(value: str | None) -> dt.datetime | None:
    """Parse a persisted ISO-8601 job timestamp to aware UTC, or ``None``.

    Local copy of the same tolerant parse the other modules keep (``usage``,
    ``logs``, ``static_metrics``), so this module stays independent. An
    unparseable stamp reads as absent, which every caller here treats as "cannot
    prove this constraint applies" — the direction that keeps a corrupt timestamp
    from freezing the queue."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _turn_instant(job: SemanticJob) -> dt.datetime | None:
    """When this record began drawing on the subscription window, or ``None``.

    ``started_ts`` is the honest answer once a turn is *running*, but it is not
    written by the step that admits the turn. Both admission paths
    (:meth:`~orch_hub.semantic_jobs.SemanticJobManager.dispatch_deferred` and
    :meth:`~orch_hub.semantic_jobs.SemanticJobManager.start_job`) persist
    ``queued`` under the job-manager lock and then submit the turn *outside* it,
    and the worker stamps ``started_ts`` when it flips the record to ``running``.
    Between those two moments a record that has already been admitted to spend
    carries no start stamp at all.

    Pacing off ``started_ts`` alone therefore leaves a gap the spacing rule exists
    to close: a second sweep, running after the first released its single-flight
    lock but before the promoted worker started, sees no start stamp anywhere and
    promotes another turn immediately. Not the concurrent herd the single-flight
    lock already refuses — a strictly sequential one, and just as expensive.

    So an admitted-but-not-yet-started turn paces from its admitting write
    (``updated_ts``, which for a ``queued`` record is exactly that write), until
    the worker replaces it with the real start. Erring in this direction is
    deliberate: spacing measured from admission starts at most milliseconds early,
    while spacing measured from a stamp that does not exist yet starts a second
    turn. A ``queued`` record whose worker never ran stops constraining once the
    spacing elapses, the same as a real turn would — and startup reconciliation
    settles it ``failed`` at the next restart."""
    started = _parse_ts(job.started_ts)
    if started is not None:
        return started
    if job.status == STATUS_QUEUED:
        return _parse_ts(job.updated_ts)
    return None


class CapacityGate:
    """Decides whether the evaluator's subscription window can afford a turn now.

    Reads one display-only provider snapshot and answers with either ``None``
    (dispatch) or a bounded machine reason. It never writes, never triggers a
    model call, and never interprets more of the usage payload than
    :func:`orch_hub.usage.current_used_percent` exposes — the window semantics
    (rollover, figure-versus-boundary identity, staleness) stay owned by the usage
    module rather than being re-derived here.

    ``provider`` is the subscription the Layer 2 backend actually spends against.
    It defaults to Claude because the shipped backend is the Claude CLI; when a
    second backend lands the wiring picks the matching provider, and nothing in
    the policy changes.
    """

    def __init__(
        self,
        config: Config,
        *,
        usage_reader: UsageReader,
        provider: str = PROVIDER_CLAUDE,
        clock: Callable[[], dt.datetime] = _utc_now,
    ):
        self._config = config
        self._reader = usage_reader
        self._provider = provider
        self._clock = clock

    @property
    def provider(self) -> str:
        return self._provider

    def check(self) -> str | None:
        """``None`` when a turn may start now, else the bounded reason it may not.

        Fail-toward-deferral is the whole shape of this method: every path that is
        not a positively-read percentage below the ceiling returns a reason. A
        failing reader is included — usage is a display-only sidecar and its
        failures are already swallowed everywhere else, so an exception here means
        we know even less than "unavailable", which cannot be grounds to spend.
        """
        try:
            snapshot = self._reader()
        except Exception as exc:  # noqa: BLE001 - unknown usage can never admit a turn
            _log.warning(
                "automatic evaluation capacity read failed (%s): %s",
                type(exc).__name__,
                exc,
            )
            return REASON_USAGE_UNKNOWN
        used = current_used_percent(snapshot, now=self._clock())
        if used is None:
            return REASON_USAGE_UNKNOWN
        if used >= self._config.eval_usage_ceiling:
            return REASON_CAPACITY
        return None


class DeferredDispatcher:
    """Drains the deferred semantic-job queue at the rate the policy allows.

    One :meth:`sweep` is the entire mechanism: read every persisted job once,
    apply spacing and then capacity, and promote at most one held job. Holding the
    per-sweep budget at one job is what makes the spacing rule mean anything —
    promoting the whole queue and *then* pacing would already have spent the
    window.

    Sweeps are **non-blocking single-flight**: two of them can be asked for at
    once (the resident tick and a completion that just landed), and two that
    overlap would each pass the spacing check against the same pre-dispatch state
    and then promote a *different* job — one turn per sweep, two turns at once,
    which is the herd the spacing exists to prevent. A second concurrent sweep
    therefore returns immediately rather than queueing: the one already running is
    doing exactly the same work, so nothing is lost by declining.

    Stateless across sweeps except for one in-memory set: which jobs have already
    been logged as stalled, so the age bound produces one line per job rather than
    one per sweep. That mirrors the run manager's in-memory question-push dedup —
    a restart re-announcing a still-stalled queue once is the correct behavior,
    not a leak.

    A second, much shorter lock sits under :meth:`admission_barrier` and covers the
    promotion step alone. Single-flight keeps two sweeps from promoting at once;
    this keeps a promotion from interleaving with a *pause write*, which the
    single-flight lock knows nothing about because the writer is an HTTP thread
    that never sweeps. Deliberately not the sweep lock: a pause request would then
    queue behind a whole store walk and a capacity read, and the thing that has to
    be serialized is one control read plus one record write, not the sweep that
    contains them.
    """

    def __init__(
        self,
        config: Config,
        *,
        job_manager: SemanticJobManager,
        gate: CapacityGate,
        control: ControlReader = _never_paused,
        clock: Callable[[], dt.datetime] = _utc_now,
    ):
        self._config = config
        self._jobs = job_manager
        self._gate = gate
        self._control = control
        self._clock = clock
        self._sweeping = threading.Lock()
        self._admission = threading.Lock()
        self._stall_logged: set[tuple[str, str, str]] = set()

    @property
    def enabled(self) -> bool:
        """Whether the automatic path may promote anything: both gates open.

        The same predicate :class:`~orch_hub.completion_eval.CompletionEvaluationTrigger`
        checks, read from the one place that derives it
        (:attr:`orch_hub.config.Config.auto_evaluation_enabled`), because these two
        halves of the automatic path must never disagree about whether it is on.

        Deliberately does NOT fold in the operator pause, even though both hold the
        queue: the trigger reads this to decide whether to *accept* a completion,
        and a pause must not stop acceptance — a paused queue that quietly dropped
        completions would be the ``ORCH_HUB_EVAL_AUTO`` behaviour it exists to
        replace. The pause is checked in :meth:`sweep` alone.
        """
        return self._config.auto_evaluation_enabled

    @property
    def paused(self) -> bool:
        """Whether an operator is currently holding the drain.

        Read fresh on every sweep rather than cached: the whole point of moving the
        pause out of the environment is that it changes while the service runs, so
        a dispatcher that remembered it would reintroduce the restart it removed.
        An unreadable control record reads as paused — the store's fail-closed
        direction (:mod:`orch_hub.eval_control`), consumed here rather than
        re-decided.
        """
        return self._control().paused

    @contextmanager
    def admission_barrier(self) -> Iterator[None]:
        """Hold the promotion step while the caller writes the pause control.

        The pause and the drain live in two different stores written by two
        different threads, so "paused" is only a real hold if the two cannot
        interleave. Without this they can, and visibly: a sweep that read the
        control a moment before a ``POST .../queue/pause`` goes on to walk the
        store, check capacity, and promote a job *after* the pause is durable —
        even after the route has answered with a payload saying the queue is held.
        The operator sees a paused queue spend a turn.

        So the promotion boundary re-reads the control inside this barrier, and a
        control writer takes the barrier around its write. Whichever gets it first
        wins cleanly: a pause that lands first is seen by the recheck and the sweep
        holds; a promotion that lands first is a turn admitted while the queue was
        genuinely running, and the payload the route then reads back shows it —
        which is the honest report, not a lost race.

        Exposed as a barrier rather than as a ``pause()`` method on this class on
        purpose: the dispatcher reads the control and must not acquire the ability
        to write it (:data:`ControlReader`). It lends the lock; composing the two
        is the caller's business.

        Held only across the recheck and one small record write, never across the
        store walk or the capacity read, so a control write waits on the shortest
        section that makes it correct. Lock order is barrier → job manager, the
        same direction the sweep already takes, so there is no cycle to deadlock
        on. It is a plain lock rather than a reentrant one because nothing may take
        it twice on one thread: a sweep re-entered from inside a turn (which a
        synchronous executor makes possible) is refused by the single-flight lock
        well before it reaches here, and a re-entrant lock would hide that rather
        than keep it true.
        """
        with self._admission:
            yield

    def sweep(self) -> SemanticJob | None:
        """Dispatch at most one held job; return it, or ``None`` if none started.

        Gated first, and this is not symmetry with the trigger — it is the only
        gate on this half. The deferred queue is *durable*, so the records a hub
        accepted while automatic evaluation was on outlive the process that
        accepted them: a hub restarted with ``ORCH_HUB_EVAL_AUTO`` off would
        otherwise promote them, which is a model turn in the deployment that just
        said manual-only. With the subsystem gate off it is worse than a stray
        turn — the evaluator refuses a disabled subsystem, so the promotion settles
        the record ``failed`` and *consumes* the task's one automatic attempt,
        turning "evaluation is switched off" into "these tasks have no report and
        never will".

        So a closed gate holds the queue exactly as it is: nothing walked, nothing
        written, nothing failed. Deferral is already the state that means "not
        now", and it survives restarts untouched, so reopening both switches needs
        no recovery step — the next sweep simply drains what was always there. The
        gate state is global config, not a per-job fact, so it is deliberately not
        stamped onto the records as a ``defer_reason``: writing one would churn the
        whole queue on a config change and then leave every record claiming a
        reason that is no longer true.

        Order matters after that. Spacing is checked before capacity because it is
        free and because a capacity read is the one step that can trigger background work
        (the usage service refreshes a stale observation as a side effect of being
        consulted) — asking for that while the answer cannot be acted on anyway is
        wasted. Both checks precede any write, so a sweep that changes nothing is
        one directory walk.

        The oldest held job goes first, so the queue is a queue: under sustained
        pressure the corpus fills in completion order rather than favouring
        whatever happened to be swept last.

        The operator pause is the second gate and behaves identically: the queue is
        held exactly as it is, nothing walked, nothing written, nothing failed. It
        gets the same treatment for the same reason — it is one global fact, not a
        per-job one, so stamping it as a ``defer_reason`` would churn every held
        record on a toggle and leave them claiming a reason that stopped being true
        the moment somebody resumed. It travels to readers through the queue
        snapshot instead (:class:`~orch_hub.models.EvaluationControl`). What it does
        *not* share with the trigger gate is the cost of being on: completions keep
        landing as ``deferred`` records while it holds, so resuming drains the same
        queue in the same order rather than restarting a corpus with a hole in it.

        The pause is read twice, and only the second read decides. This one is a
        pre-filter that keeps a held queue from being walked at all; the one inside
        :meth:`admission_barrier` at the promotion boundary is the one a pause
        *write* is serialized against, because everything between the two — the
        store walk, the spacing check, the capacity read — is time in which the
        operator can press the button.

        Returns ``None`` immediately when another sweep is already running (see the
        class docstring): declining is correct, not a missed tick.
        """
        if not self.enabled:
            return None
        if self.paused:
            return None
        if not self._sweeping.acquire(blocking=False):
            return None
        try:
            return self._sweep_locked()
        finally:
            self._sweeping.release()

    def _sweep_locked(self) -> SemanticJob | None:
        """One sweep's body; the caller holds the single-flight lock.

        The lock is this dispatcher's own and is always taken *before* any job-
        manager lock, never the other way round, so the one-directional order
        (dispatcher -> job manager -> nothing) leaves no cycle to deadlock on.
        """
        jobs = self._jobs.list_jobs()
        deferred = sorted(
            (job for job in jobs if job.status == STATUS_DEFERRED),
            key=lambda job: (job.created_ts or "", job.repo_name, job.task_id),
        )
        if not deferred:
            return None
        now = self._clock()
        self._log_stalled(deferred, now)

        reason = self._spacing_reason(jobs, now) or self._gate.check()
        if reason is not None:
            self._hold(deferred, reason)
            return None
        for job in deferred:
            if job.managed_repo() is None:
                # Unactionable forever: settle it where failures are visible rather
                # than leaving a permanent resident every sweep silently skips.
                # Not under the barrier: it spends nothing, so a pause has no
                # interest in holding it.
                self._jobs.fail_deferred(job, UNATTRIBUTABLE_REASON)
                continue
            with self.admission_barrier():
                # The authoritative pause check. The one in `sweep` is a cheap
                # pre-filter that saves the walk below; this is the one a pause
                # write is serialized against, so it is re-read here rather than
                # carried down from there — everything in between (the walk, the
                # spacing check, the capacity read) is time in which an operator
                # can have pressed the button.
                if self._control().paused:
                    return None
                started = self._jobs.dispatch_deferred(job)
            if started is not None:
                _log.info(
                    "automatic semantic job %s dispatched for %s/%s",
                    started.job_id,
                    started.repo_name,
                    started.task_id,
                )
                return started
        return None

    def _spacing_reason(
        self, jobs: list[SemanticJob], now: dt.datetime
    ) -> str | None:
        """``REASON_PACING`` while the previous turn is still inside the spacing.

        "The previous turn" is the newest turn instant across *all* records, not
        just automatic ones: a manual evaluation draws on the same window, so
        pacing after it is the same protection. And "turn instant" is
        :func:`_turn_instant` rather than ``started_ts``, because a turn already
        admitted but not yet started has no start stamp and must still pace — see
        there. A record with no instant at all (never admitted, or corrupt) simply
        does not constrain anything.
        """
        spacing = self._config.eval_dispatch_spacing
        if spacing <= 0:
            return None
        starts = [ts for ts in (_turn_instant(job) for job in jobs) if ts]
        if not starts:
            return None
        if (now - max(starts)).total_seconds() < spacing:
            return REASON_PACING
        return None

    def _hold(self, deferred: list[SemanticJob], reason: str) -> None:
        """Record the same reason on every held job; writes only what changed."""
        for job in deferred:
            try:
                self._jobs.hold_deferred(job, reason)
            except Exception as exc:  # noqa: BLE001 - one bad record never stalls the queue
                _log.warning(
                    "recording deferral reason failed for %s/%s (%s): %s",
                    job.repo_name,
                    job.task_id,
                    type(exc).__name__,
                    exc,
                )

    def _log_stalled(self, deferred: list[SemanticJob], now: dt.datetime) -> None:
        """Warn once per job that has waited past the configured age bound.

        Deferral is meant to be temporary, and every reason it is not — an
        exhausted weekly window, a usage feed that stopped reporting, a ``claude``
        binary that can no longer refresh it — looks identical from inside the
        queue: nothing happens, quietly, and the corpus stops growing. This bounds
        how long that can stay invisible. It changes no dispatch decision; a
        stalled job is still a job waiting for capacity, not a failed one.
        """
        max_age = self._config.eval_queue_max_age
        if max_age <= 0:
            return
        for job in deferred:
            created = _parse_ts(job.created_ts)
            if created is None:
                continue
            age = (now - created).total_seconds()
            if age < max_age:
                continue
            key = (job.repo_name, job.task_id, job.job_id)
            if key in self._stall_logged:
                continue
            self._stall_logged.add(key)
            _log.warning(
                "automatic semantic evaluation for %s/%s has waited %.0fh"
                " (reason=%s); the evaluation queue is not draining",
                job.repo_name,
                job.task_id,
                age / 3600.0,
                job.defer_reason or "unknown",
            )


# -- operator view -------------------------------------------------------------
#
# Everything above decides; this reports. The split matters because the automatic
# path's failure modes are *silent by construction*: a held queue, a failed turn,
# and a switch left off are indistinguishable from outside — nothing happens, and
# the corpus stops growing. A selection-free corpus that nobody can tell has
# stopped filling is worth no more than the judgment-selected one it replaced.


_WAITING_ORDER = (STATUS_DEFERRED, STATUS_QUEUED, STATUS_RUNNING)
"""Statuses whose jobs are still owed a turn, in the order a reader wants them.

They lead the queue view, and within it a held job precedes an admitted one:
"what is stuck" is the question this surface exists to answer, and an admitted
turn is already answering itself."""


def _waiting_key(job: SemanticJob) -> tuple:
    """Oldest-first within the waiting group — the dispatcher's own promotion
    order, so the top of the view is what actually runs next."""
    return (
        _WAITING_ORDER.index(job.status),
        job.created_ts or "",
        job.repo_name,
        job.task_id,
    )


def _settled_key(job: SemanticJob) -> tuple:
    """Newest-first within the settled group (note the reverse sort at the call
    site): a settled record is a log entry, and the newest failure is the one
    still worth reading — an old one has either been superseded by a fresh
    attempt or is part of a pattern the counts already state."""
    return (job.updated_ts or job.ended_ts or job.created_ts or "", job.repo_name, job.task_id)


def queue_snapshot(
    config: Config,
    jobs: list[SemanticJob],
    *,
    control: EvaluationControl | None = None,
    now: dt.datetime | None = None,
    limit: int = QUEUE_ENTRY_LIMIT,
) -> EvaluationQueueSnapshot:
    """Fold every persisted job into the cross-repo view of the automatic path.

    Pure: takes the records (one :meth:`SemanticJobManager.list_jobs` walk), the
    pause state, and a clock, reads three config values, and returns a projection.
    It holds no state, writes nothing, and — unlike :meth:`DeferredDispatcher.sweep`
    — is deliberately **not** gated on ``auto_evaluation_enabled``: a reader asking
    why nothing is draining must be able to see the held records *and* that the
    switch is off. Hiding the queue while the gate is closed would answer the
    question with the same silence that made it necessary. The gate state travels
    as ``auto_enabled`` instead, so the reader explains rather than omits.

    ``control`` is the other hold on the drain and travels the same way, for the
    same reason. It is passed in as a *value* rather than read here, which is what
    keeps this function pure: a projection that opened a file would make every
    caller's read depend on store IO it did not ask for, and the sweep and this
    view would each be reading the switch on their own schedule. ``None`` means "no
    pause state supplied" and reads as running — callers that own a store pass its
    state.

    ``counts`` is one entry per persisted *record* — and the store keeps one
    record per ``(repo, task)``, holding that task's **latest** attempt. So the
    census states the queue's attempt lifecycle and deliberately not how many
    tasks carry a report: :meth:`SemanticJobManager.start_job` overwrites a
    terminal record for a manual re-run while the completed evaluation it pointed
    at survives in the task's retained history, so a report followed by a failed
    retry counts here as ``failed`` (correctly — the queue is what this reports
    on), and a task completed before automatic evaluation existed contributes no
    record at all. Report existence has its own authorities (the evaluation tree,
    the publication feed) that no attempt can overwrite. Consumers must label
    these figures as the attempt lifecycle they are; presenting ``completed`` as
    an evaluated-corpus denominator would be wrong in both directions.

    ``overdue`` counts held jobs past ``eval_queue_max_age`` — the same jobs, by
    the same rule, that :meth:`DeferredDispatcher._log_stalled` warns about, so the
    dashboard and the service log name one population. A bound of ``0`` disables
    the flag entirely (no job is overdue), matching the config contract.

    ``skipped_by_repo`` folds the tasks that completed and deliberately got no
    turn (the launch-time Layer 2 opt-out) into a per-repo count. Per repo because
    that is the shape a thinning corpus actually has — skips cluster on the
    expensive, remediation-heavy tail rather than spreading evenly — and a count
    rather than rows because individual rows are the least likely thing anyone
    scrolls to. Only repos with at least one skip appear: the store knows nothing
    about repos it holds no record for, so a zero row would be an invention.
    Skipped records are kept out of ``entries`` alongside ``completed`` and
    ``cancelled``: all three are resting outcomes with nothing to act on, and a row
    per opted-out or deliberately stopped task would crowd out the jobs that are
    genuinely stuck. All three stay in ``counts``, which is where a reader looks to
    find out how much of the corpus was declined rather than measured.
    """
    moment = now or _utc_now()
    max_age = config.eval_queue_max_age
    counts: dict[str, int] = {status: 0 for status in STATUSES}
    skipped_by_repo: dict[str, int] = {}
    pending: list[SemanticJob] = []
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
        if job.status == STATUS_SKIPPED:
            repo_name = job.repo_name or "(unattributed)"
            skipped_by_repo[repo_name] = skipped_by_repo.get(repo_name, 0) + 1
        elif job.status not in (STATUS_COMPLETED, STATUS_CANCELLED):
            pending.append(job)
    # Two groups with opposite senses, so they are sorted apart and concatenated
    # rather than forced into one comparator.
    pending = sorted(
        (job for job in pending if job.status in _WAITING_ORDER), key=_waiting_key
    ) + sorted(
        (job for job in pending if job.status not in _WAITING_ORDER),
        key=_settled_key,
        reverse=True,
    )
    entries = []
    overdue = 0
    for job in pending:
        created = _parse_ts(job.created_ts)
        age = int((moment - created).total_seconds()) if created is not None else None
        stalled = (
            job.status == STATUS_DEFERRED
            and max_age > 0
            and age is not None
            and age >= max_age
        )
        if stalled:
            overdue += 1
        if len(entries) < max(limit, 0):
            entries.append(
                EvaluationQueueEntry(job=job, age_seconds=age, overdue=stalled)
            )
    return EvaluationQueueSnapshot(
        auto_enabled=config.auto_evaluation_enabled,
        control=control if control is not None else EvaluationControl(),
        queue_max_age_seconds=max_age,
        counts=counts,
        # Most-skipped first, ties by repo name: the reader is looking for which
        # repo is thinning, and a deterministic tiebreak keeps the payload stable
        # across reads of an unchanged store.
        skipped_by_repo=dict(
            sorted(skipped_by_repo.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        overdue=overdue,
        entries=tuple(entries),
        truncated=len(pending) > len(entries),
    )
