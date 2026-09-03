"""The read-only view of the semantic-job queue.

This module used to own the automatic Layer 2 dispatch policy — a capacity gate
over the evaluator provider's subscription window, spacing between turns, and a
sweep that promoted one held ``deferred`` record per tick. All of it is gone
(:mod:`orch_hub.completion_eval` says why): a semantic evaluation now starts only
when a person asks for one, so there is no queue to drain and no policy to decide
when a mechanical turn may spend.

What remains is the projection. The store still holds one record per
``(repo, task)`` — every attempt somebody started, and whatever legacy automatic
records a hub accumulated while the automatic path existed — and
:func:`queue_snapshot` folds them into one cross-repo census for the Overview and
the CLI. It reports; nothing here can start, promote, or hold a turn.

The census is worth keeping for the manual path rather than a leftover of the
automatic one. A Layer 2 turn runs for minutes and is asked for from a phone, so
the person who started it is usually somewhere else by the time it settles, and
every other evaluation surface is scoped to a single ``(repo, task)`` — which
cannot answer "is anything running?" or "did one of these fail while I was
away?" without opening tasks one at a time.

Legacy ``deferred`` and ``skipped`` records rest in that census exactly as they
are: they were written by an earlier process, no code path can promote or
reproduce one, and they stay until a manual
:meth:`~orch_hub.semantic_jobs.SemanticJobManager.start_job` supersedes the
record with a fresh attempt. That is the intended shape — a held record is a task
nobody has asked about, not a turn waiting to fire the moment a network comes
back — and it is why nothing here flags one as late.
"""

from __future__ import annotations

import datetime as dt

from .models import (
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
    say"."""
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
# because a semantic job's outcomes are silent by construction: a turn that
# failed, a legacy record nobody will ever promote, and an evaluation nobody asked
# for all look identical from outside — nothing happens, quietly.


_WAITING_ORDER = (STATUS_DEFERRED, STATUS_QUEUED, STATUS_RUNNING)
"""Statuses whose jobs are still owed a turn, in the order a reader wants them.

They lead the queue view, and within it a legacy held record precedes an admitted
one: "what is not going to happen by itself" is the question this surface exists
to answer, and an admitted turn is already answering itself."""


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
    jobs: list[SemanticJob],
    *,
    now: dt.datetime | None = None,
    limit: int = QUEUE_ENTRY_LIMIT,
) -> EvaluationQueueSnapshot:
    """Fold every persisted job into the cross-repo view of the semantic queue.

    Pure: takes the records (one :meth:`SemanticJobManager.list_jobs` walk) and a
    clock, and returns a projection. It holds no state, reads no file, and writes
    nothing.

    It takes no ``Config``, which is the shape of the change rather than an
    omission. The fold used to read three values — the trigger gate to echo, the
    stall bound to measure held records against — and every one of them described a
    dispatcher. There is no configuration that changes what this store holds or
    how it should be read, so a config parameter here would only suggest one
    exists. ``auto_evaluation_enabled`` in particular governs automatic *Layer 1*
    and must not filter or annotate these records.

    ``counts`` is one entry per persisted *record* — and the store keeps one
    record per ``(repo, task)``, holding that task's **latest** attempt. So the
    census states the store's attempt lifecycle and deliberately not how many
    tasks carry a report: :meth:`SemanticJobManager.start_job` overwrites a
    terminal record for a re-run while the completed evaluation it pointed at
    survives in the task's retained history, so a report followed by a failed
    retry counts here as ``failed`` (correctly — the queue is what this reports
    on), and a task nobody asked to evaluate contributes no record at all. Report
    existence has its own authorities (the evaluation tree, the publication feed)
    that no attempt can overwrite. Consumers must label these figures as the
    attempt lifecycle they are; presenting ``completed`` as an evaluated-corpus
    denominator would be wrong in both directions.

    ``entries`` holds what is left to look at, so the three resting outcomes stay
    out of it: ``completed`` and ``cancelled`` are settled attempts, and
    ``skipped`` is a legacy record for a completion the retired launch opt-out
    declined. All three remain in ``counts``, which is where a reader looks for
    what the store holds rather than for what to do next.
    """
    moment = now or _utc_now()
    counts: dict[str, int] = {status: 0 for status in STATUSES}
    pending: list[SemanticJob] = []
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
        if job.status not in (STATUS_COMPLETED, STATUS_CANCELLED, STATUS_SKIPPED):
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
    entries = [
        EvaluationQueueEntry(job=job, age_seconds=_age_seconds(job, moment))
        for job in pending[: max(limit, 0)]
    ]
    return EvaluationQueueSnapshot(
        counts=counts,
        entries=tuple(entries),
        truncated=len(pending) > len(entries),
    )


def _age_seconds(job: SemanticJob, moment: dt.datetime) -> int | None:
    """How long since this attempt was accepted, or ``None`` if it cannot be said."""
    created = _parse_ts(job.created_ts)
    return int((moment - created).total_seconds()) if created is not None else None
