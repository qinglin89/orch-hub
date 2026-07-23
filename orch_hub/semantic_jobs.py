"""Async semantic-evaluation job lifecycle: queued / running / completed / failed.

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
import os
import re
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .config import Config
from .models import ManagedRepo, SemanticJob
from .semantic import SemanticEvaluator

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
finalize steps key off these."""

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
    ):
        self._config = config
        self._evaluator = evaluator
        self._clock = clock
        self._executor = executor
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
        another." Otherwise (no record yet, or a terminal one) a fresh ``queued``
        record overwrites it, is persisted before the turn so an interrupted hub
        leaves an observable orphan, and the evaluation turn runs off the lock
        (:meth:`_run_job`). The evaluator identity (backend/model/schema) is
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
            job_dir.mkdir(parents=True, exist_ok=True)
            info = self._evaluator.evaluator_info
            job_id = self._clock().strftime(_JOB_ID_TS)
            ts = self._clock().isoformat()
            record = SemanticJob(
                job_id=job_id,
                repo_name=repo.name,
                repo_path=repo.path,
                task_id=task_id,
                status=STATUS_QUEUED,
                backend=info.backend,
                model=info.model,
                schema_version=info.schema_version,
                created_ts=ts,
                updated_ts=ts,
            )
            _write_record(job_dir, record)
        # Run the evaluation off the lock; the transient record is returned now and
        # the turn settles it asynchronously. Submit strictly OUTSIDE the lock: the
        # turn re-acquires it to flip to running / finalize, which a synchronous
        # executor would deadlock on the non-reentrant lock.
        self._submit(lambda: self._run_job(job_dir, job_id, repo, task_id))
        return record

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
        """
        with self._lock:
            record = self._load_if_present(job_dir)
            if record is None or record.status not in _TRANSIENT or record.job_id != job_id:
                return
            ts = self._clock().isoformat()
            _write_record(
                job_dir,
                replace(
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
                ),
            )

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


def _path_field(data: dict, key: str) -> Path | None:
    value = _str_field(data, key)
    return Path(value) if value else None
