"""Remote intake-task lifecycle: draft, revise, confirm, cancel.

The intake manager lets a remote client draft a new ``.ai-tasks/`` task through
the ``/intake-task`` protocol *without* bypassing its confirmation gate. It owns
a hub-owned intake-dir layout under ``Config.data_dir``, parallel to the run
manager's ``runs/`` tree:

.. code-block:: text

    <data_dir>/intake/<intake_id>/
        intake.json    hub-owned intake record (atomic rewrite on updates)
        events.jsonl   append-only audit (creation/revision/confirmation/...)

The load-bearing invariant is **read-only until confirmation**: the target repo
is never modified while a draft is being written or revised. Structurally, only
:meth:`IntakeManager.confirm_intake` ever calls the agent's write path
(:meth:`IntakeAgent.confirm`); ``start_intake``/``revise_intake`` call the
read-only ``draft``/``revise`` turns, and ``cancel_intake`` writes nothing. So a
cancelled, failed, interrupted, or simply unconfirmed intake leaves the repo
untouched by construction, not by the agent's good behavior.

The conversational agent itself is an injected adapter (:class:`IntakeAgent`),
exactly as the run manager injects its ``spawner``: tests drive a fake, and the
concrete CLI-backed agent that resumes a ``/intake-task`` session lands with the
dashboard flow that actually needs to drive it. The default backend is
:class:`_UnavailableIntakeAgent`, so the manager is always constructible and an
unconfigured backend fails an intake cleanly (auditable) instead of crashing.

Each agent turn runs **off the manager lock**. A mutating call
(``start``/``revise``/``confirm``) holds the lock only long enough to gate and
persist the transient ``drafting``/``revising``/``confirming`` status, then
returns that transient record at once and runs the (potentially slow, real)
turn through an injectable executor seam (:data:`IntakeExecutor`, a daemon
thread by default; tests inject a synchronous one). The turn re-acquires the
lock to finalize, so reads (``get``/``list``) and a concurrent ``cancel`` never
block behind an in-flight turn. Finalize re-loads the record and respects a
cancel that landed during the turn — it discards the turn's result and never
resurrects a cancelled intake. Because the transient status is persisted before
the turn, a hub that dies mid-turn leaves an observable orphan holding the
repo's intake slot; :meth:`IntakeManager.reconcile_orphans` (run once at
startup) fails those orphans so the slot frees. The confirm turn is the only
one that writes, and it stays fenced by :class:`_ConfirmGuard` (captured just
before the write) plus the inventory postcondition, so a failed/unverified
confirm still rolls back — the read-only-until-confirmation invariant is
unchanged by moving the turn off the lock.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import logging
import os
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .admission import RepoAdmission
from .config import Config
from .models import (
    IntakeDraft,
    IntakeEvent,
    IntakeHistoryEntry,
    IntakeRecord,
    ManagedRepo,
    ReadinessReport,
)
from .notifications import Notification, Notifier, intake_ready_notification
from .readiness import check_readiness
from .task_store import list_repo_tasks

_log = logging.getLogger(__name__)

INTAKE_SUBDIR = "intake"
INTAKE_RECORD_NAME = "intake.json"
INTAKE_EVENTS_NAME = "events.jsonl"

# Lifecycle statuses. The transient trio brackets an in-flight agent turn; the
# terminal quartet is a resting outcome. ``error`` is reserved for an unreadable
# record (a broken dir that must surface, not hide), distinct from ``failed``
# (a legitimate agent-failure outcome).
STATUS_DRAFTING = "drafting"
STATUS_AWAITING_REVIEW = "awaiting_review"
STATUS_REVISING = "revising"
STATUS_CONFIRMING = "confirming"
STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
STATUS_ERROR = "error"

_TERMINAL = frozenset(
    {STATUS_CONFIRMED, STATUS_CANCELLED, STATUS_FAILED, STATUS_ERROR}
)
"""Resting states: no further lifecycle operation is legal."""

_CANCELLABLE = frozenset({STATUS_DRAFTING, STATUS_AWAITING_REVIEW, STATUS_REVISING})
"""States a cancel may leave. ``confirming`` is deliberately excluded: its write
may have already landed, so cancelling it could mislabel a created task."""

_TRANSIENT = frozenset({STATUS_DRAFTING, STATUS_REVISING, STATUS_CONFIRMING})
"""In-flight states that bracket an agent turn. A persisted transient record
with no live turn (a hub that died mid-turn) is an orphan holding the repo's
intake slot, reconciled to ``failed`` at startup (:meth:`IntakeManager.reconcile_orphans`).
The finalize step keys off these: it only settles a turn whose record is still
in the expected transient status, so a cancel that landed meanwhile wins."""

KIND_INTAKE_CREATED = "intake_created"
KIND_INTAKE_REVISED = "intake_revised"
KIND_INTAKE_CONFIRMED = "intake_confirmed"
KIND_INTAKE_CANCELLED = "intake_cancelled"
KIND_INTAKE_FAILED = "intake_failed"

_INTAKE_ID_TS = "%Y%m%dT%H%M%SZ"
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


class IntakeError(Exception):
    """Base class for intake-manager operational errors."""


class UnknownIntakeError(IntakeError):
    """No intake dir exists for the given intake id."""


class IntakeStateError(IntakeError):
    """The operation is illegal for the intake's current status (fail closed)."""


class IntakeBusyError(IntakeError):
    """The target repo already has an in-flight (non-terminal) intake."""

    def __init__(self, repo_name: str, intake_id: str):
        self.intake_id = intake_id
        super().__init__(
            f"repo {repo_name!r} already has in-flight intake {intake_id}"
        )


class IntakeNotReadyError(IntakeError):
    """Readiness gating refused the intake before it started (fail closed)."""

    def __init__(self, reasons: Sequence[str]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class AgentDraftResult:
    """One conversational draft/revise turn's output.

    ``handle`` is the live, opaque conversational handle the manager keeps in
    memory to continue the session; ``ref`` is its persistable form (e.g. a CLI
    session id) recorded on the intake for audit and possible resume. ``draft``
    is the complete proposed task the human reviews.
    """

    handle: object
    ref: str | None
    draft: IntakeDraft


@dataclass(frozen=True)
class AgentConfirmResult:
    """The result of the agent's protocol-defined write on confirmation."""

    task_id: str
    task_path: Path | None = None


class IntakeAgent(Protocol):
    """The conversational backend the intake manager drives.

    Injected like the run manager's ``spawner``. Contract:

    - ``draft``/``revise`` MUST be read-only — they produce a proposal and never
      touch the target repo. The manager relies on this for its core invariant.
    - ``confirm`` performs the protocol-defined write (the task file plus the
      index entry) and is the ONLY write-capable turn; the manager calls it only
      on an explicit confirmation.
    - ``cancel`` releases the conversational session; it writes nothing.
    """

    def draft(self, request: str, repo: ManagedRepo) -> AgentDraftResult: ...

    def revise(
        self, handle: object, feedback: str, repo: ManagedRepo
    ) -> AgentDraftResult: ...

    def confirm(self, handle: object, repo: ManagedRepo) -> AgentConfirmResult: ...

    def cancel(self, handle: object) -> None: ...


class _UnavailableIntakeAgent:
    """Default backend: no concrete conversational agent is wired yet.

    The manager, its state machine, and its audit trail are fully exercised with
    an injected agent (a fake in tests, a real CLI-driven session once the
    dashboard drives it). Until then this default makes every drafting turn fail
    cleanly — the intake is recorded as ``failed`` with a clear reason rather
    than the manager crashing — while ``cancel`` stays a harmless no-op.
    """

    def draft(self, request: str, repo: ManagedRepo) -> AgentDraftResult:
        raise IntakeError("no intake agent configured")

    def revise(
        self, handle: object, feedback: str, repo: ManagedRepo
    ) -> AgentDraftResult:
        raise IntakeError("no intake agent configured")

    def confirm(self, handle: object, repo: ManagedRepo) -> AgentConfirmResult:
        raise IntakeError("no intake agent configured")

    def cancel(self, handle: object) -> None:
        return None


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


IntakeExecutor = Callable[[Callable[[], None]], None]
"""Runs one intake turn's off-lock work. The default detaches it onto a daemon
thread so the mutating call returns immediately with the transient record; the
turn then re-acquires the manager lock to finalize. Mirrors the run manager's
``notify_dispatch`` seam. Tests inject a synchronous executor
(``lambda run: run()``) so a turn settles before the mutating call returns."""


def _thread_executor(run: Callable[[], None]) -> None:
    """Default executor: run one intake turn on a short-lived daemon thread.

    The turn (a real ``/intake-task`` CLI session later) can take seconds, and it
    must never stall intake reads, a concurrent cancel, or an unrelated repo's
    intake, so it runs off the manager lock. The thread is daemon so it never
    blocks interpreter shutdown; an unfinished read-only turn is safe to abandon.
    """
    threading.Thread(target=run, daemon=True, name="orch-hub-intake").start()


def _thread_notify_dispatch(deliver: Callable[[], None]) -> None:
    """Default notification dispatch: deliver on a short-lived daemon thread.

    Mirrors the run manager's ``_thread_dispatch``. An intake-ready push is
    emitted from a finalize step, which holds the manager lock, so a slow or
    hung Bark backend must never stall the finalize (nor an unrelated repo's
    intake): delivery runs off the lock on a daemon thread that never blocks
    interpreter shutdown. Tests inject a synchronous dispatch
    (``lambda deliver: deliver()``) for deterministic assertions.
    """
    threading.Thread(
        target=deliver, daemon=True, name="orch-hub-intake-notify"
    ).start()


def _slug(value: str) -> str:
    """Make a repo name safe as an intake-dir path component."""
    return _SLUG.sub("-", value).strip("-") or "x"


INTAKE_TASK_STATUSES = frozenset({"pending", "blocked"})
"""Frontmatter statuses a freshly intaked task file may legally carry.

``/intake-task`` produces a ``pending`` task, or a ``blocked`` one when it
records blockers (human-resolved: ``blocked`` is valid only with blockers,
otherwise the state is ``pending``). Any other landed status —
``in_progress``/``final_review``/``completed``, the task-store
``missing``/``unknown`` sentinels, or an unrecognized value — means the confirm
turn did not produce a valid new intake task, so the confirmation fails closed.
"""


def _task_landed(repo: ManagedRepo, task_id: str) -> bool:
    """True iff ``task_id`` now appears as a valid new intake task in ``repo``.

    The confirmation postcondition: a genuinely written intake task has both a
    ``.ai-tasks/<id>.md`` file and an ``.ai-tasks/index.md`` row, *and* a legal
    post-intake frontmatter status (:data:`INTAKE_TASK_STATUSES` — ``pending``,
    or ``blocked`` when intake records blockers). It therefore round-trips
    through the same frontmatter-authoritative, index-gated task-inventory path
    the dashboard routes to. A ``missing`` sentinel (index row but no file), an
    ``unknown`` sentinel (file without a parseable status), an in-flight or
    finished status such as ``in_progress``/``final_review``/``completed``, an
    unwritten index, or an absent id all fail closed — an exit-0 agent turn that
    wrote nothing, only part of the task, or a task in the wrong state is not a
    confirmation.
    """
    try:
        inventory = list_repo_tasks(repo)
    except OSError:
        return False
    return any(
        task.task_id == task_id and task.status in INTAKE_TASK_STATUSES
        for task in inventory.tasks
    )


def _safe_task_path(repo: ManagedRepo, task_id: str) -> Path | None:
    """``.ai-tasks/<task_id>.md`` iff ``task_id`` is a safe single segment.

    Mirrors ``task_store``'s containment rule so a crafted draft id can never
    make rollback delete a file outside ``.ai-tasks/``. Returns ``None`` for an
    unsafe id; such an id could not have produced an inventory-visible task
    anyway, so verification fails and no in-tree task file needs removing.
    """
    if (
        not task_id
        or "/" in task_id
        or "\\" in task_id
        or "\x00" in task_id
        or task_id in (".", "..")
    ):
        return None
    return repo.path / ".ai-tasks" / f"{task_id}.md"


@dataclass
class _ConfirmGuard:
    """Snapshot of the ``.ai-tasks/`` paths a confirm write is allowed to touch.

    Captured just before the write turn so a failed or unverified confirm can be
    rolled back, upholding the acceptance invariant that a failed/unconfirmed
    intake leaves the target repo unchanged. Scoped to exactly the proposed task
    file and ``index.md`` — the two paths the confirm prompt authorizes — so
    rollback never disturbs an unrelated concurrent write to some other task's
    file. (A concurrent writer to ``index.md`` itself during the confirm window
    is the same cross-manager race already noted for run dirs; it is not newly
    introduced here.)
    """

    task_path: Path | None
    task_existed: bool
    index_path: Path
    index_existed: bool
    index_before: bytes | None

    @classmethod
    def capture(cls, repo: ManagedRepo, task_id: str) -> _ConfirmGuard:
        task_path = _safe_task_path(repo, task_id)
        index_path = repo.path / ".ai-tasks" / "index.md"
        index_existed = index_path.exists()
        try:
            index_before = index_path.read_bytes() if index_existed else None
        except OSError:
            index_before = None  # existed but unreadable: restore is best-effort
        return cls(
            task_path=task_path,
            task_existed=bool(task_path and task_path.exists()),
            index_path=index_path,
            index_existed=index_existed,
            index_before=index_before,
        )

    def rollback(self) -> None:
        """Undo a confirm turn's write to the snapshotted paths (best effort)."""
        # Remove a task file this turn created; leave a pre-existing one alone.
        if self.task_path is not None and not self.task_existed:
            try:
                self.task_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as err:  # pragma: no cover - unexpected fs error
                _log.warning(
                    "intake rollback could not remove %s: %s", self.task_path, err
                )
        # Restore index.md: drop one this turn created, else revert its content.
        try:
            if not self.index_existed:
                self.index_path.unlink(missing_ok=True)
            elif (
                self.index_before is not None
                and self.index_path.read_bytes() != self.index_before
            ):
                self.index_path.write_bytes(self.index_before)
        except OSError as err:  # pragma: no cover - unexpected fs error
            _log.warning(
                "intake rollback could not restore %s: %s", self.index_path, err
            )


class IntakeManager:
    """Owns remote intake lifecycle, hub-owned state, and audit.

    One instance lives in the resident hub service. Collaborators are
    injectable for tests: ``agent`` is the conversational backend (a fake in
    tests), ``readiness_checker`` defaults to the real launch gate, ``clock``
    must return timezone-aware UTC datetimes, and ``executor`` runs each agent
    turn off the manager lock (a daemon thread in production; a synchronous
    ``lambda run: run()`` in tests).

    ``notifier`` is the optional push backend (``None`` disables notifications
    with no delivery work at all, mirroring the run manager); it fires one
    metadata-only ``intake ready`` push each time a draft or revision settles to
    ``awaiting_review``. ``notify_dispatch`` runs each delivery off the manager
    lock — a daemon thread by default so a slow Bark backend never stalls a
    finalize; tests inject a synchronous dispatch.

    ``admission`` is the per-repo critical section shared with the run manager
    (:mod:`orch_hub.admission`): the hub admits either a live run or a remote
    intake for one repo, never both. The manager registers its own
    non-terminal-intake probe on the coordinator it is given, and a standalone
    manager (every intake-only test) gets a private one whose run side is
    unbound — a process with no run manager has no run to conflict with.

    The invariant the tests pin down: the target repo is unchanged until an
    explicit :meth:`confirm_intake`, because only that method reaches the
    agent's write path. Drafting, revision, cancellation, agent failure, and a
    hub restart before confirmation all leave the repo untouched.

    Concurrency: a mutating call gates and persists the transient status under
    the lock, returns that transient record, then runs the turn off the lock and
    re-acquires it to finalize (:meth:`_finalize_draft`/`_finalize_revise`/
    `_finalize_confirm`). Reads and cancel therefore never block behind a turn.
    Finalize re-loads and settles only a record still in its expected transient
    status, so a cancel that landed during the turn wins and the turn's result is
    discarded (never resurrecting a cancelled intake).
    """

    def __init__(
        self,
        config: Config,
        *,
        agent: IntakeAgent | None = None,
        readiness_checker: Callable[[ManagedRepo, Config], ReadinessReport] = check_readiness,
        clock: Callable[[], dt.datetime] = _utc_now,
        executor: IntakeExecutor = _thread_executor,
        notifier: Notifier | None = None,
        notify_dispatch: IntakeExecutor = _thread_notify_dispatch,
        admission: RepoAdmission | None = None,
    ):
        self._config = config
        self._agent: IntakeAgent = agent if agent is not None else _UnavailableIntakeAgent()
        self._readiness_checker = readiness_checker
        self._clock = clock
        self._executor = executor
        self._notifier = notifier
        self._notify_dispatch = notify_dispatch
        self._lock = threading.Lock()
        # Live conversational handles by intake id. Not persisted: a hub restart
        # drops them, so a non-terminal intake whose handle is gone is treated as
        # a lost session (fail closed) on the next revise/confirm — never a
        # silent repo write.
        self._handles: dict[str, object] = {}
        self.use_admission(admission if admission is not None else RepoAdmission())

    @property
    def intake_dir(self) -> Path:
        return self._config.data_dir / INTAKE_SUBDIR

    @property
    def admission(self) -> RepoAdmission:
        """The shared repo-admission coordinator (see :mod:`orch_hub.admission`)."""
        return self._admission

    def use_admission(self, admission: RepoAdmission) -> None:
        """Place this manager on ``admission``, registering its intake probe.

        The intake-side mirror of
        :meth:`orch_hub.runs.RunManager.use_admission`, with the same contract:
        called at construction and again by :func:`orch_hub.api.create_app` when
        two independently built managers must end up on ONE coordinator.
        """
        self._admission = admission
        admission.bind_intake_source(self.active_intake_for)

    # -- start ----------------------------------------------------------------

    def start_intake(self, repo: ManagedRepo, request: str) -> IntakeRecord:
        """Gate and start one intake for ``repo``; return the ``drafting`` record.

        Gates fail closed cheapest-first: non-empty request, the shared repo
        admission section (no live run for this repo), no other in-flight intake
        for the repo, then readiness. The ``drafting`` record is persisted under
        the lock *before* the agent turn so an interrupted hub leaves an
        observable record and (critically) never a repo write. The drafting turn
        then runs off the lock (:meth:`_run_draft_turn`) and this returns the
        transient ``drafting`` record immediately — a later ``get``/``list`` (or
        the dashboard poll) observes the settled ``awaiting_review`` draft, or a
        ``failed`` record if the turn (including the default unconfigured
        backend) failed.

        Admission comes before readiness deliberately: a live run dirties the
        working tree, so readiness would usually refuse with a ``git_clean``
        failure — a misleading proxy for "this repo is claimed by a run". The
        section stays held until the ``drafting`` record is on disk, so a
        concurrent run start either loses the section and then sees this intake,
        or wins it and makes this start fail with
        :class:`~orch_hub.admission.RunLiveError`.
        """
        text = request.strip()
        if not text:
            raise ValueError("intake request must be a non-empty string")
        with self._admission.admit_intake(repo), self._lock:
            self._ensure_repo_free(repo)
            self._gate_readiness(repo)
            intake_dir, intake_id = self._create_intake_dir(repo.name)
            ts = self._clock().isoformat()
            record = IntakeRecord(
                intake_id=intake_id,
                repo_name=repo.name,
                repo_path=repo.path,
                request=text,
                status=STATUS_DRAFTING,
                created_ts=ts,
                updated_ts=ts,
            )
            _write_record(intake_dir, record)
            self._append(
                intake_dir,
                record,
                KIND_INTAKE_CREATED,
                ts=ts,
                detail="intake started",
            )
        # Draft off the lock; the transient record is returned now and the turn
        # settles the record asynchronously. Submit strictly OUTSIDE the lock:
        # the turn re-acquires it to finalize, which a synchronous executor would
        # deadlock on the non-reentrant lock. Outside the admission section too —
        # the record is persisted, so the repo is already claimed and a long turn
        # need not hold the section.
        self._submit(lambda: self._run_draft_turn(intake_dir, intake_id, text, repo))
        return record

    def _run_draft_turn(
        self, intake_dir: Path, intake_id: str, text: str, repo: ManagedRepo
    ) -> None:
        """Off-lock drafting turn, then re-acquire the lock to finalize."""
        try:
            result = self._agent.draft(text, repo)
        except Exception as exc:  # noqa: BLE001 - any backend failure is a failed intake
            with self._lock:
                self._finalize_failure(
                    intake_dir, intake_id, STATUS_DRAFTING, f"draft failed: {exc}"
                )
            return
        with self._lock:
            self._finalize_draft(intake_dir, intake_id, result)

    def _finalize_draft(
        self, intake_dir: Path, intake_id: str, result: AgentDraftResult
    ) -> None:
        """Settle a finished draft to ``awaiting_review`` (called under the lock).

        Re-loads first: if the record is no longer ``drafting`` a cancel landed
        during the turn, so the draft is discarded and its session released — the
        intake is never resurrected from its resting ``cancelled`` state.
        """
        record = _load_record(intake_dir)
        if record.status != STATUS_DRAFTING:
            self._discard_turn(intake_id, result.handle)
            return
        self._handles[intake_id] = result.handle
        updated = replace(
            record,
            status=STATUS_AWAITING_REVIEW,
            draft=result.draft,
            agent_ref=result.ref,
            updated_ts=self._clock().isoformat(),
        )
        _write_record(intake_dir, updated)
        # Draft settled to awaiting_review: notify best-effort, off the lock. The
        # cancel-race branch above returns before this, so a discarded turn (and
        # every failure path, which never reaches here) fires nothing.
        self._emit(intake_ready_notification(updated))

    # -- revise ---------------------------------------------------------------

    def revise_intake(self, intake_id: str, feedback: str) -> IntakeRecord:
        """Send review feedback back to the same session for a fresh draft.

        Legal only from ``awaiting_review`` (a resting, reviewable state); any
        other status fails closed. A lost session (handle gone after a restart)
        or an agent failure resolves to a ``failed`` record. The revised draft
        replaces the current one and bumps ``iteration``; each revision is
        audited as its own iteration event.

        A recovered overlap (a live run alongside this intake, which admission
        prevents from forming but cannot undo for a legacy or restart-reattached
        pair) fails closed with
        :class:`~orch_hub.admission.RunLiveError`: revising re-reads a repo the
        run is actively rewriting. The intake stays cancellable — refusing here
        is a guard, not a state change.
        """
        text = feedback.strip()
        if not text:
            raise ValueError("revision feedback must be a non-empty string")
        with self._lock:
            intake_dir = self._intake_dir_for(intake_id)
            record = _load_record(intake_dir)
            self._require_status(record, STATUS_AWAITING_REVIEW, "revise")
            repo = self._require_repo(record)
            self._admission.require_no_live_run(repo, "revise an intake")
            handle = self._handles.get(intake_id)
            if handle is None:
                return self._fail(
                    intake_dir,
                    record,
                    "intake session lost (hub restarted before confirmation)",
                )
            revising = replace(
                record, status=STATUS_REVISING, updated_ts=self._clock().isoformat()
            )
            _write_record(intake_dir, revising)
        # Revise off the lock; return the transient ``revising`` record now.
        self._submit(
            lambda: self._run_revise_turn(intake_dir, intake_id, handle, text, repo)
        )
        return revising

    def _run_revise_turn(
        self,
        intake_dir: Path,
        intake_id: str,
        handle: object,
        text: str,
        repo: ManagedRepo,
    ) -> None:
        """Off-lock revision turn, then re-acquire the lock to finalize."""
        try:
            result = self._agent.revise(handle, text, repo)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._finalize_failure(
                    intake_dir, intake_id, STATUS_REVISING, f"revision failed: {exc}"
                )
            return
        with self._lock:
            self._finalize_revise(intake_dir, intake_id, result, text)

    def _finalize_revise(
        self,
        intake_dir: Path,
        intake_id: str,
        result: AgentDraftResult,
        feedback: str,
    ) -> None:
        """Settle a finished revision to ``awaiting_review`` (under the lock).

        Like :meth:`_finalize_draft`, a record no longer ``revising`` means a
        cancel landed during the turn: discard and never resurrect. Otherwise the
        revised draft replaces the current one, bumps ``iteration``, and audits
        the revision as its own iteration event.
        """
        record = _load_record(intake_dir)
        if record.status != STATUS_REVISING:
            self._discard_turn(intake_id, result.handle)
            return
        self._handles[intake_id] = result.handle
        iteration = record.iteration + 1
        updated = replace(
            record,
            status=STATUS_AWAITING_REVIEW,
            draft=result.draft,
            iteration=iteration,
            agent_ref=result.ref,
            updated_ts=self._clock().isoformat(),
        )
        _write_record(intake_dir, updated)
        self._append(
            intake_dir,
            updated,
            KIND_INTAKE_REVISED,
            ts=updated.updated_ts,
            iteration=iteration,
            task_id=result.draft.task_id,
            detail=_summarize_feedback(feedback),
        )
        # Revision settled to awaiting_review: notify best-effort, off the lock —
        # same success-only trigger as the initial draft.
        self._emit(intake_ready_notification(updated))

    # -- confirm --------------------------------------------------------------

    def confirm_intake(self, intake_id: str) -> IntakeRecord:
        """Confirm the current draft, letting the session perform the write.

        Legal only from ``awaiting_review``. This is the sole method that reaches
        the agent's write path, so it is the only way an intake modifies the
        target repo. The ``confirming`` status is persisted before the write so a
        hub death mid-write is reconcilable (the task may exist).

        The agent's exit code alone is not trusted as proof: an agent turn can
        exit 0 having written nothing or only part of the task, and a
        write-permitted turn can mutate ``.ai-tasks/`` and then fail. So the
        record is only marked ``confirmed`` once the task actually lands in the
        target repo's task inventory (:func:`_task_landed`: a real
        ``.ai-tasks/<id>.md`` file, an ``index.md`` row, *and* a legal
        post-intake status — ``pending`` or ``blocked`` — round-tripping through
        the same frontmatter-authoritative, index-gated path the dashboard routes
        to). A lost session, an agent-turn failure, or an unverified write all
        resolve to
        ``failed`` — and any partial ``.ai-tasks/`` write from a failed/unverified
        confirm turn is rolled back (:class:`_ConfirmGuard`), so a non-confirmed
        intake leaves the target repo unchanged.

        A recovered overlap fails closed here for a stronger reason than in
        :meth:`revise_intake`: this is the one turn that writes ``.ai-tasks/``,
        and a live run owns that tree. Refusing leaves the intake reviewable and
        cancellable rather than racing two writers.
        """
        with self._lock:
            intake_dir = self._intake_dir_for(intake_id)
            record = _load_record(intake_dir)
            self._require_status(record, STATUS_AWAITING_REVIEW, "confirm")
            repo = self._require_repo(record)
            self._admission.require_no_live_run(repo, "confirm an intake")
            handle = self._handles.get(intake_id)
            if handle is None:
                return self._fail(
                    intake_dir,
                    record,
                    "intake session lost (hub restarted before confirmation)",
                )
            draft = record.draft
            if draft is None:  # awaiting_review always carries a draft; defensive.
                return self._fail(
                    intake_dir, record, "intake has no draft to confirm"
                )
            confirming = replace(
                record, status=STATUS_CONFIRMING, updated_ts=self._clock().isoformat()
            )
            _write_record(intake_dir, confirming)
        # Run the write turn off the lock; return the transient ``confirming``
        # record now. ``confirming`` is non-cancellable, so no cancel can race the
        # write, but finalize still re-checks the status defensively.
        self._submit(
            lambda: self._run_confirm_turn(intake_dir, intake_id, handle, repo, draft)
        )
        return confirming

    def _run_confirm_turn(
        self,
        intake_dir: Path,
        intake_id: str,
        handle: object,
        repo: ManagedRepo,
        draft: IntakeDraft,
    ) -> None:
        """Off-lock confirm write turn: guard, write, verify, then finalize.

        The guard snapshots the exact ``.ai-tasks/`` paths the write may touch,
        captured just before the write turn so a failed or unverified confirm is
        rolled back to leave the repo unchanged. The agent's exit code is not
        trusted: the record only settles ``confirmed`` once :func:`_task_landed`
        confirms the task round-trips through the repo inventory as a legal
        ``pending``/``blocked`` intake task. Rollback happens off the lock (it
        touches only target-repo files); finalize re-acquires the lock.
        """
        guard = _ConfirmGuard.capture(repo, draft.task_id)
        try:
            result = self._agent.confirm(handle, repo)
        except Exception as exc:  # noqa: BLE001
            guard.rollback()
            with self._lock:
                self._finalize_failure(
                    intake_dir, intake_id, STATUS_CONFIRMING, f"confirm failed: {exc}"
                )
            return
        created_id = (result.task_id or "").strip() or draft.task_id
        if not _task_landed(repo, created_id):
            guard.rollback()
            with self._lock:
                self._finalize_failure(
                    intake_dir,
                    intake_id,
                    STATUS_CONFIRMING,
                    f"confirm turn did not create a valid pending/blocked task"
                    f" {created_id!r}: it is not in the repo task inventory as a"
                    " new intake task (missing task file or index row, or an"
                    " unexpected frontmatter status)",
                )
            return
        with self._lock:
            self._finalize_confirm(intake_dir, intake_id, created_id)

    def _finalize_confirm(
        self, intake_dir: Path, intake_id: str, created_id: str
    ) -> None:
        """Settle a verified confirm to ``confirmed`` (under the lock)."""
        record = _load_record(intake_dir)
        if record.status != STATUS_CONFIRMING:
            # Defensive: ``confirming`` is non-cancellable, so nothing legal moves
            # it. Drop the handle rather than double-write over a settled record.
            self._handles.pop(intake_id, None)
            return
        self._handles.pop(intake_id, None)
        updated = replace(
            record,
            status=STATUS_CONFIRMED,
            created_task_id=created_id,
            updated_ts=self._clock().isoformat(),
        )
        _write_record(intake_dir, updated)
        self._append(
            intake_dir,
            updated,
            KIND_INTAKE_CONFIRMED,
            ts=updated.updated_ts,
            iteration=updated.iteration,
            task_id=created_id,
            detail="task and index entry written",
        )

    # -- cancel ---------------------------------------------------------------

    def cancel_intake(self, intake_id: str) -> IntakeRecord:
        """Cancel a non-confirmed intake, leaving the target repo unchanged.

        Legal from ``drafting``/``awaiting_review``/``revising`` (not
        ``confirming``, whose write may already have landed, nor a terminal
        state). Cancellation never needs a live session, so it always succeeds
        for a cancellable intake — even after a restart dropped the handle — and
        releases the session best-effort if one is still held.

        Deliberately NOT admission-guarded: cancelling writes nothing to the
        target repo, and it is the escape hatch that clears a recovered overlap
        (a live run plus a surviving intake) whose revise/confirm are fail-closed.
        Refusing it would strand the intake — and keep it blocking the repo's
        next run start.
        """
        with self._lock:
            intake_dir = self._intake_dir_for(intake_id)
            record = _load_record(intake_dir)
            if record.status not in _CANCELLABLE:
                raise IntakeStateError(
                    f"intake {intake_id!r} cannot be cancelled from status"
                    f" {record.status!r}"
                )
            handle = self._handles.pop(intake_id, None)
            if handle is not None:
                try:
                    self._agent.cancel(handle)
                except Exception as exc:  # noqa: BLE001 - cancel is best-effort
                    _log.warning(
                        "intake agent cancel failed (%s) for %s",
                        type(exc).__name__,
                        intake_id,
                    )
            updated = replace(
                record,
                status=STATUS_CANCELLED,
                updated_ts=self._clock().isoformat(),
            )
            _write_record(intake_dir, updated)
            self._append(
                intake_dir,
                updated,
                KIND_INTAKE_CANCELLED,
                ts=updated.updated_ts,
                iteration=updated.iteration,
                detail="intake cancelled",
            )
            return updated

    # -- startup reconciliation -----------------------------------------------

    def reconcile_orphans(self) -> list[IntakeRecord]:
        """Fail every orphaned transient record; return what was reconciled.

        A persisted ``drafting``/``revising``/``confirming`` record with no live
        turn is an orphan from a previous hub process that died mid-turn: its
        in-memory conversational handle is gone and no thread will finalize it, so
        it would hold the repo's intake slot forever (a non-terminal record makes
        :meth:`start_intake` refuse a new intake for that repo). This marks each
        such record ``failed`` (lost session) so the slot frees.

        It must run once at startup, before any new intake is submitted (the
        FastAPI lifespan calls it, mirroring the run monitor's start); at that
        point every transient record on disk is unambiguously an orphan.

        It never mutates the target repo: rollback is a live-process guarantee
        (:class:`_ConfirmGuard` lives in memory), so a ``confirming`` orphan whose
        write already landed leaves a real task — surfaced independently through
        task inventory — with a conservatively-``failed`` intake. That mid-write
        crash accounting is the flagged open item, not closed here.
        """
        reconciled: list[IntakeRecord] = []
        with self._lock:
            for intake_dir in self._intake_dirs():
                record = _load_record(intake_dir)
                if record.status not in _TRANSIENT:
                    continue
                reconciled.append(
                    self._fail(
                        intake_dir,
                        record,
                        f"intake session lost (hub restarted during {record.status})",
                    )
                )
        return reconciled

    # -- inspection -----------------------------------------------------------

    def get_intake(self, intake_id: str) -> IntakeRecord:
        """Return one intake's current record; unknown ids raise."""
        with self._lock:
            return _load_record(self._intake_dir_for(intake_id))

    def list_intakes(self) -> list[IntakeRecord]:
        """Return every known intake, oldest first (ids sort by start time)."""
        with self._lock:
            return [_load_record(d) for d in self._intake_dirs()]

    def active_intake_for(self, repo: ManagedRepo) -> str | None:
        """Return ``repo``'s non-terminal intake id, or ``None`` — lock-free.

        The intake subsystem's occupancy probe for the shared repo admission
        coordinator, and the same scan the one-intake-per-repo gate uses — one
        definition of "this repo has an intake in flight", derived from the
        persisted intake records rather than a second in-memory table.

        Deliberately outside :attr:`_lock`. The admission coordinator calls this
        while holding a repo's section, and taking the manager-wide lock would
        make a run launch for one repo queue behind an unrelated repo's intake
        turn. Correctness does not need it: this reads only ``intake.json``
        files, each written atomically (same-dir temp + rename), so a concurrent
        write yields either the old or the new record and never a torn one — and
        the repo section already orders this read against the record write it
        must observe.
        """
        return self._active_intake_id(repo)

    def intake_history(self, intake_id: str) -> IntakeHistoryEntry:
        """Return one intake's record paired with its persisted audit events."""
        with self._lock:
            intake_dir = self._intake_dir_for(intake_id)
            return IntakeHistoryEntry(
                record=_load_record(intake_dir),
                events=read_events(intake_dir),
            )

    # -- internals ------------------------------------------------------------

    def _ensure_repo_free(self, repo: ManagedRepo) -> None:
        intake_id = self._active_intake_id(repo)
        if intake_id is not None:
            raise IntakeBusyError(repo.name, intake_id)

    def _active_intake_id(self, repo: ManagedRepo) -> str | None:
        """Non-terminal intake id for ``repo``, or ``None``.

        Pure reads with no side effects, so it is safe both under the manager
        lock (the one-intake-per-repo gate) and outside it (the admission probe).

        Non-terminal is the transient trio plus ``awaiting_review``: each one
        either has a turn in flight or is a draft still awaiting a human. The
        terminal quartet (``confirmed``/``cancelled``/``failed``/``error``) is a
        resting outcome and holds nothing — an ``error`` record additionally
        carries no repo attribution, so it can never match a repo.
        """
        for intake_dir in self._intake_dirs():
            record = _load_record(intake_dir)
            if record.status in _TERMINAL:
                continue
            if record.repo_path == repo.path:
                return record.intake_id
        return None

    def _gate_readiness(self, repo: ManagedRepo) -> None:
        report = self._readiness_checker(repo, self._config)
        if not report.ready:
            raise IntakeNotReadyError(
                [f"{check.name}: {check.message}" for check in report.failures]
            )

    def _require_status(
        self, record: IntakeRecord, expected: str, action: str
    ) -> None:
        if record.status != expected:
            raise IntakeStateError(
                f"intake {record.intake_id!r} cannot {action} from status"
                f" {record.status!r} (expected {expected!r})"
            )

    def _require_repo(self, record: IntakeRecord) -> ManagedRepo:
        repo = record.managed_repo()
        if repo is None:
            raise IntakeStateError(
                f"intake {record.intake_id!r} has no repo path on record"
            )
        return repo

    def _submit(self, run: Callable[[], None]) -> None:
        """Run one intake turn's off-lock work through the injected executor.

        MUST be called outside ``self._lock``: ``run`` re-acquires the lock to
        finalize, and the non-reentrant lock would deadlock a synchronous
        executor otherwise. The default thread executor detaches ``run`` so the
        mutating call returns at once with the transient record.
        """
        self._executor(run)

    def _emit(self, notification: Notification) -> None:
        """Deliver a notification best-effort; never raise into intake state.

        Mirrors :meth:`orch_hub.runs.RunManager._emit`. Isolation is the
        invariant: a backend failure (network down, bad Bark key) must not
        corrupt ``intake.json`` / ``events.jsonl``. This is called from a
        finalize step under the manager lock, so delivery runs through the
        injected dispatch (a daemon thread in production) off the lock, and the
        backend call is wrapped so any exception is logged, not propagated. A
        ``None`` notifier short-circuits — no dispatch work at all.
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
                    "intake notification delivery failed (%s): %s",
                    type(exc).__name__,
                    notification.title,
                )

        self._notify_dispatch(deliver)

    def _finalize_failure(
        self, intake_dir: Path, intake_id: str, expected: str, reason: str
    ) -> None:
        """Fail an intake whose off-lock turn errored — unless it settled first.

        Called under the lock. Re-loads and only fails a record still in its
        ``expected`` transient status: a cancel that landed during the turn
        already left it ``cancelled`` (terminal), and respecting that is what
        stops a late turn failure from clobbering a resting outcome.
        """
        record = _load_record(intake_dir)
        if record.status != expected:
            self._handles.pop(intake_id, None)
            return
        self._fail(intake_dir, record, reason)

    def _discard_turn(self, intake_id: str, handle: object) -> None:
        """Drop a finished turn whose intake was cancelled meanwhile.

        Called under the lock from a finalize step. The turn's result is thrown
        away (draft/revise are read-only — nothing to undo), any stored handle is
        dropped, and the now-orphaned conversational session is released
        best-effort (a no-op for the real CLI agent). This is the handle-leak
        guard: a discarded turn never leaves a live session dangling.
        """
        self._handles.pop(intake_id, None)
        if handle is not None:
            try:
                self._agent.cancel(handle)
            except Exception as exc:  # noqa: BLE001 - cancel is best-effort
                _log.warning(
                    "intake agent cancel failed (%s) for %s",
                    type(exc).__name__,
                    intake_id,
                )

    def _fail(
        self, intake_dir: Path, record: IntakeRecord, reason: str
    ) -> IntakeRecord:
        """Mark an intake ``failed`` with an audited reason; no repo write.

        Used for every agent-turn failure and lost-session case. The in-memory
        handle is dropped and the terminal record is persisted, so a failed
        intake frees its repo slot and stays inspectable for audit.
        """
        self._handles.pop(record.intake_id, None)
        updated = replace(
            record,
            status=STATUS_FAILED,
            error=reason,
            updated_ts=self._clock().isoformat(),
        )
        _write_record(intake_dir, updated)
        self._append(
            intake_dir,
            updated,
            KIND_INTAKE_FAILED,
            ts=updated.updated_ts,
            iteration=updated.iteration,
            detail=reason,
        )
        return updated

    def _append(
        self,
        intake_dir: Path,
        record: IntakeRecord,
        kind: str,
        *,
        ts: str,
        iteration: int | None = None,
        task_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        append_event(
            intake_dir,
            IntakeEvent(
                ts=ts,
                kind=kind,
                intake_id=record.intake_id,
                repo_name=record.repo_name,
                repo_path=record.repo_path,
                iteration=iteration,
                task_id=task_id,
                detail=detail,
            ),
        )

    def _intake_dirs(self) -> list[Path]:
        if not self.intake_dir.is_dir():
            return []
        return sorted(p for p in self.intake_dir.iterdir() if p.is_dir())

    def _intake_dir_for(self, intake_id: str) -> Path:
        # Reject path-escaping ids up front; intake ids are single dir names.
        intake_dir = self.intake_dir / intake_id
        if Path(intake_id).name != intake_id or not intake_dir.is_dir():
            raise UnknownIntakeError(f"no intake {intake_id!r}")
        return intake_dir

    def _create_intake_dir(self, repo_name: str) -> tuple[Path, str]:
        """Create a fresh, collision-free intake dir; return it and its id."""
        base = "-".join(
            (self._clock().strftime(_INTAKE_ID_TS), _slug(repo_name), "intake")
        )
        for attempt in itertools.count():
            intake_id = base if attempt == 0 else f"{base}-{attempt + 1}"
            intake_dir = self.intake_dir / intake_id
            try:
                intake_dir.mkdir(parents=True)
            except FileExistsError:
                continue
            return intake_dir, intake_id
        raise AssertionError("unreachable")


def _summarize_feedback(feedback: str, limit: int = 200) -> str:
    """Condense revision feedback into a single audited detail line."""
    collapsed = " ".join(feedback.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


# -- persistence --------------------------------------------------------------


def _write_record(intake_dir: Path, record: IntakeRecord) -> None:
    """Persist ``intake.json`` atomically (same-dir tmp + rename).

    Includes the internal ``agent_ref`` (kept out of API output but needed to
    reconstruct the record), unlike :meth:`IntakeRecord.to_dict`.
    """
    draft = record.draft
    payload = {
        "intake_id": record.intake_id,
        "repo_name": record.repo_name,
        "repo_path": str(record.repo_path) if record.repo_path else None,
        "request": record.request,
        "status": record.status,
        "draft": draft.to_dict() if draft is not None else None,
        "iteration": record.iteration,
        "created_ts": record.created_ts,
        "updated_ts": record.updated_ts,
        "error": record.error,
        "created_task_id": record.created_task_id,
        "agent_ref": record.agent_ref,
    }
    path = intake_dir / INTAKE_RECORD_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _load_record(intake_dir: Path) -> IntakeRecord:
    """Load ``intake.json``; an unreadable one yields an ``error`` record.

    An error record surfaces a broken dir instead of hiding it, and carries no
    repo attribution, so it never holds a repo's intake slot.
    """
    path = intake_dir / INTAKE_RECORD_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _error_record(intake_dir, "missing intake.json")
    except (OSError, ValueError) as err:
        return _error_record(intake_dir, f"unreadable intake.json ({err})")
    if not isinstance(data, dict):
        return _error_record(intake_dir, "intake.json is not a JSON object")
    return IntakeRecord(
        intake_id=_str_field(data, "intake_id") or intake_dir.name,
        repo_name=_str_field(data, "repo_name") or "",
        repo_path=_path_field(data, "repo_path"),
        request=_str_field(data, "request") or "",
        status=_str_field(data, "status") or STATUS_ERROR,
        draft=_draft_from_dict(data.get("draft")),
        iteration=_int_field(data, "iteration") or 0,
        created_ts=_str_field(data, "created_ts"),
        updated_ts=_str_field(data, "updated_ts"),
        error=_str_field(data, "error"),
        created_task_id=_str_field(data, "created_task_id"),
        agent_ref=_str_field(data, "agent_ref"),
    )


def _error_record(intake_dir: Path, error: str) -> IntakeRecord:
    return IntakeRecord(
        intake_id=intake_dir.name,
        repo_name="",
        repo_path=None,
        request="",
        status=STATUS_ERROR,
        error=error,
    )


def _draft_from_dict(data: object) -> IntakeDraft | None:
    if not isinstance(data, dict):
        return None
    return IntakeDraft(
        task_id=_str_field(data, "task_id") or "",
        summary=_str_field(data, "summary") or "",
        task_file=data.get("task_file") if isinstance(data.get("task_file"), str) else "",
        index_entry=data.get("index_entry") if isinstance(data.get("index_entry"), str) else "",
        notes=_str_field(data, "notes"),
    )


def append_event(intake_dir: Path, event: IntakeEvent) -> None:
    """Append one intake audit event to the dir's JSONL history file."""
    path = intake_dir / INTAKE_EVENTS_NAME
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")


def read_events(intake_dir: Path) -> tuple[IntakeEvent, ...]:
    """Read an intake's audit events; corrupt lines surface as failed events."""
    path = intake_dir / INTAKE_EVENTS_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ()
    except OSError as err:
        return (_event_error(intake_dir, f"unreadable {INTAKE_EVENTS_NAME} ({err})"),)

    events: list[IntakeEvent] = []
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError as err:
            events.append(_event_error(intake_dir, f"line {lineno}: invalid JSON ({err})"))
            continue
        if not isinstance(data, dict):
            events.append(_event_error(intake_dir, f"line {lineno}: not a JSON object"))
            continue
        events.append(
            IntakeEvent(
                ts=_str_field(data, "ts") or "",
                kind=_str_field(data, "kind") or KIND_INTAKE_FAILED,
                intake_id=_str_field(data, "intake_id") or intake_dir.name,
                repo_name=_str_field(data, "repo_name") or "",
                repo_path=_path_field(data, "repo_path"),
                iteration=_int_field(data, "iteration"),
                task_id=_str_field(data, "task_id"),
                detail=_str_field(data, "detail"),
            )
        )
    return tuple(events)


def _event_error(intake_dir: Path, detail: str) -> IntakeEvent:
    return IntakeEvent(
        ts="",
        kind=KIND_INTAKE_FAILED,
        intake_id=intake_dir.name,
        detail=detail,
    )


def _str_field(data: dict, key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _path_field(data: dict, key: str) -> Path | None:
    value = _str_field(data, key)
    return Path(value) if value else None


def _int_field(data: dict, key: str) -> int | None:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None
