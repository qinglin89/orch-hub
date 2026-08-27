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

The sweep is driven by the resident run monitor and by the completion hook's own
off-lock executor, so a completion arriving in a quiet window becomes a turn
within milliseconds while a held queue is retried for as long as it takes. It is
idempotent and stateless: every decision is re-derived from the persisted records
and the current usage read, so a hub restart resumes the queue without a startup
step and without a second thing to keep in sync.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable

from .config import Config
from .models import PROVIDER_CLAUDE, ProviderUsage, SemanticJob
from .semantic_jobs import STATUS_DEFERRED, STATUS_QUEUED, SemanticJobManager
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

UNATTRIBUTABLE_REASON = (
    "semantic evaluation abandoned (deferred job carries no repo attribution)"
)
"""Diagnostic for a held record whose persisted repo is gone, so no target can be
resolved without guessing. Settled ``failed`` rather than left in the queue."""

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
    """

    def __init__(
        self,
        config: Config,
        *,
        job_manager: SemanticJobManager,
        gate: CapacityGate,
        clock: Callable[[], dt.datetime] = _utc_now,
    ):
        self._config = config
        self._jobs = job_manager
        self._gate = gate
        self._clock = clock
        self._sweeping = threading.Lock()
        self._stall_logged: set[tuple[str, str, str]] = set()

    @property
    def enabled(self) -> bool:
        """Whether the automatic path may promote anything: both gates open.

        The same predicate :class:`~orch_hub.completion_eval.CompletionEvaluationTrigger`
        checks, read from the one place that derives it
        (:attr:`orch_hub.config.Config.auto_evaluation_enabled`), because these two
        halves of the automatic path must never disagree about whether it is on.
        """
        return self._config.auto_evaluation_enabled

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

        Returns ``None`` immediately when another sweep is already running (see the
        class docstring): declining is correct, not a missed tick.
        """
        if not self.enabled:
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
                self._jobs.fail_deferred(job, UNATTRIBUTABLE_REASON)
                continue
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
