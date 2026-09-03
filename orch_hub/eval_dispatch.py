"""The read-only view of the semantic-job queue.

This module used to own the automatic Layer 2 dispatch policy — a capacity gate
over the evaluator provider's subscription window, spacing between turns, and a
sweep that promoted one held ``deferred`` record per tick. All of it is gone
(:mod:`orch_hub.completion_eval` says why): a semantic evaluation now starts only
when a person asks for one, so there is no queue to drain and no policy to decide
when a mechanical turn may spend.

What remains is the projection. The store still holds one record per
``(repo, task)`` — every manual attempt, and whatever legacy automatic records a
hub accumulated while the automatic path existed — and :func:`queue_snapshot`
folds them into one cross-repo census for the Overview and the CLI. It reports;
nothing here can start, promote, or hold a turn.

Legacy ``deferred`` records are the visible edge of that removal. They were
accepted by an earlier process and there is now no code path that can promote
one, so they rest in the census exactly as they are until an operator's manual
:meth:`~orch_hub.semantic_jobs.SemanticJobManager.start_job` supersedes the
record with a fresh attempt. That is the intended shape: a held record is a task
nobody has asked about, not a turn waiting to fire the moment a network comes
back.
"""

from __future__ import annotations

import datetime as dt

from .config import Config
from .models import (
    EvaluationControl,
    EvaluationQueueEntry,
    EvaluationQueueSnapshot,
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
)

QUEUE_ENTRY_LIMIT = 20
"""Ceiling on the rows :func:`queue_snapshot` returns (counts stay complete).

An operator surface has to fit on a phone, and the queue's pathological shapes
are unbounded: ``failed`` records are terminal, so they accumulate for the life
of the store, and a hub that ran the automatic path carries one legacy held
record per task it accepted. Capping the rows keeps the surface readable; keeping
``counts`` a full census keeps it honest, which is the half that actually matters
here — a figure that quietly meant "the first twenty" would report a twenty-deep
queue as calm. ``truncated`` says when the cap bit."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_ts(value: str | None) -> dt.datetime | None:
    """Parse a persisted ISO-8601 job timestamp to aware UTC, or ``None``.

    Local copy of the same tolerant parse the other modules keep (``usage``,
    ``logs``, ``static_metrics``), so this module stays independent. An
    unparseable stamp reads as absent, which every caller here treats as "cannot
    prove this constraint applies"."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


# -- operator view -------------------------------------------------------------
#
# Nothing here decides; it reports. Which is the whole module now, and matters
# because a semantic job's failure modes are silent by construction: a failed
# turn, a legacy record nobody will ever promote, and an evaluation nobody asked
# for all look identical from outside — nothing happens, quietly.


_WAITING_ORDER = (STATUS_DEFERRED, STATUS_QUEUED, STATUS_RUNNING)
"""Statuses whose jobs are still owed a turn, in the order a reader wants them.

They lead the queue view, and within it a held job precedes an admitted one:
"what is stuck" is the question this surface exists to answer, and an admitted
turn is already answering itself."""


def _waiting_key(job: SemanticJob) -> tuple:
    """Oldest-first within the waiting group, so the reader meets the longest-
    unanswered record first."""
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
    """Fold every persisted job into the cross-repo view of the semantic queue.

    Pure: takes the records (one :meth:`SemanticJobManager.list_jobs` walk), the
    pause state, and a clock, reads two config values, and returns a projection.
    It holds no state and writes nothing. It is deliberately **not** gated on
    ``auto_evaluation_enabled``: a reader asking why a record is sitting still must
    be able to see it, and hiding records behind a switch would answer the question
    with the same silence that made it necessary. The switch state travels as
    ``auto_enabled`` instead, so the reader explains rather than omits.

    ``control`` is passed in as a *value* rather than read here, which is what
    keeps this function pure: a projection that opened a file would make every
    caller's read depend on store IO it did not ask for. ``None`` means "no pause
    state supplied" and reads as running — callers that own a store pass its state.

    ``counts`` is one entry per persisted *record* — and the store keeps one
    record per ``(repo, task)``, holding that task's **latest** attempt. So the
    census states the store's attempt lifecycle and deliberately not how many
    tasks carry a report: :meth:`SemanticJobManager.start_job` overwrites a
    terminal record for a manual re-run while the completed evaluation it pointed
    at survives in the task's retained history, so a report followed by a failed
    retry counts here as ``failed`` (correctly — the queue is what this reports
    on), and a task completed before automatic evaluation existed contributes no
    record at all. Report existence has its own authorities (the evaluation tree,
    the publication feed) that no attempt can overwrite. Consumers must label
    these figures as the attempt lifecycle they are; presenting ``completed`` as
    an evaluated-corpus denominator would be wrong in both directions.

    ``overdue`` counts held jobs past ``eval_queue_max_age``. Only legacy
    ``deferred`` records can reach that state now — nothing accepts new ones — so
    the flag reads as "this record has been resting since the automatic path was
    retired", not as a queue failing to drain. A bound of ``0`` disables the flag
    entirely (no job is overdue), matching the config contract.

    ``skipped_by_repo`` folds the tasks that completed and deliberately got no
    turn (the retired launch-time Layer 2 opt-out) into a per-repo count. Per repo because
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
