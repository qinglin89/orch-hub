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

The agent turn runs synchronously inside each mutating call in v1 (a fake in
tests, a real turn later). The transient ``drafting``/``revising``/``confirming``
statuses are persisted *before* the turn so an interrupted hub leaves an
observable non-terminal record — and, when a slow real backend arrives, they
already hold the per-repo/per-intake slot needed to move the turn off the lock.
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

from .config import Config
from .models import (
    IntakeDraft,
    IntakeEvent,
    IntakeHistoryEntry,
    IntakeRecord,
    ManagedRepo,
    ReadinessReport,
)
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
    tests), ``readiness_checker`` defaults to the real launch gate, and
    ``clock`` must return timezone-aware UTC datetimes.

    The invariant the tests pin down: the target repo is unchanged until an
    explicit :meth:`confirm_intake`, because only that method reaches the
    agent's write path. Drafting, revision, cancellation, agent failure, and a
    hub restart before confirmation all leave the repo untouched.
    """

    def __init__(
        self,
        config: Config,
        *,
        agent: IntakeAgent | None = None,
        readiness_checker: Callable[[ManagedRepo, Config], ReadinessReport] = check_readiness,
        clock: Callable[[], dt.datetime] = _utc_now,
    ):
        self._config = config
        self._agent: IntakeAgent = agent if agent is not None else _UnavailableIntakeAgent()
        self._readiness_checker = readiness_checker
        self._clock = clock
        self._lock = threading.Lock()
        # Live conversational handles by intake id. Not persisted: a hub restart
        # drops them, so a non-terminal intake whose handle is gone is treated as
        # a lost session (fail closed) on the next revise/confirm — never a
        # silent repo write.
        self._handles: dict[str, object] = {}

    @property
    def intake_dir(self) -> Path:
        return self._config.data_dir / INTAKE_SUBDIR

    # -- start ----------------------------------------------------------------

    def start_intake(self, repo: ManagedRepo, request: str) -> IntakeRecord:
        """Gate, draft, and record one intake for ``repo``.

        Gates fail closed cheapest-first: non-empty request, no other in-flight
        intake for the repo, then readiness. The ``drafting`` record is persisted
        *before* the agent turn so an interrupted hub leaves an observable record
        and (critically) never a repo write. A drafting failure — including the
        default unconfigured backend — resolves to a ``failed`` record with an
        audited reason, not an exception.
        """
        text = request.strip()
        if not text:
            raise ValueError("intake request must be a non-empty string")
        with self._lock:
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
            try:
                result = self._agent.draft(text, repo)
            except Exception as exc:  # noqa: BLE001 - any backend failure is a failed intake
                return self._fail(intake_dir, record, f"draft failed: {exc}")
            self._handles[intake_id] = result.handle
            updated = replace(
                record,
                status=STATUS_AWAITING_REVIEW,
                draft=result.draft,
                agent_ref=result.ref,
                updated_ts=self._clock().isoformat(),
            )
            _write_record(intake_dir, updated)
            return updated

    # -- revise ---------------------------------------------------------------

    def revise_intake(self, intake_id: str, feedback: str) -> IntakeRecord:
        """Send review feedback back to the same session for a fresh draft.

        Legal only from ``awaiting_review`` (a resting, reviewable state); any
        other status fails closed. A lost session (handle gone after a restart)
        or an agent failure resolves to a ``failed`` record. The revised draft
        replaces the current one and bumps ``iteration``; each revision is
        audited as its own iteration event.
        """
        text = feedback.strip()
        if not text:
            raise ValueError("revision feedback must be a non-empty string")
        with self._lock:
            intake_dir = self._intake_dir_for(intake_id)
            record = _load_record(intake_dir)
            self._require_status(record, STATUS_AWAITING_REVIEW, "revise")
            repo = self._require_repo(record)
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
            try:
                result = self._agent.revise(handle, text, repo)
            except Exception as exc:  # noqa: BLE001
                return self._fail(intake_dir, revising, f"revision failed: {exc}")
            self._handles[intake_id] = result.handle
            iteration = record.iteration + 1
            updated = replace(
                revising,
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
                detail=_summarize_feedback(text),
            )
            return updated

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
        """
        with self._lock:
            intake_dir = self._intake_dir_for(intake_id)
            record = _load_record(intake_dir)
            self._require_status(record, STATUS_AWAITING_REVIEW, "confirm")
            repo = self._require_repo(record)
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
            # Snapshot the exact `.ai-tasks/` paths a confirm write may touch,
            # so a failed or unverified turn is rolled back to leave the repo
            # unchanged.
            guard = _ConfirmGuard.capture(repo, draft.task_id)
            try:
                result = self._agent.confirm(handle, repo)
            except Exception as exc:  # noqa: BLE001
                guard.rollback()
                return self._fail(intake_dir, confirming, f"confirm failed: {exc}")
            created_id = (result.task_id or "").strip() or draft.task_id
            if not _task_landed(repo, created_id):
                guard.rollback()
                return self._fail(
                    intake_dir,
                    confirming,
                    f"confirm turn did not create a valid pending/blocked task"
                    f" {created_id!r}: it is not in the repo task inventory as a"
                    " new intake task (missing task file or index row, or an"
                    " unexpected frontmatter status)",
                )
            self._handles.pop(intake_id, None)
            updated = replace(
                confirming,
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
            return updated

    # -- cancel ---------------------------------------------------------------

    def cancel_intake(self, intake_id: str) -> IntakeRecord:
        """Cancel a non-confirmed intake, leaving the target repo unchanged.

        Legal from ``drafting``/``awaiting_review``/``revising`` (not
        ``confirming``, whose write may already have landed, nor a terminal
        state). Cancellation never needs a live session, so it always succeeds
        for a cancellable intake — even after a restart dropped the handle — and
        releases the session best-effort if one is still held.
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

    # -- inspection -----------------------------------------------------------

    def get_intake(self, intake_id: str) -> IntakeRecord:
        """Return one intake's current record; unknown ids raise."""
        with self._lock:
            return _load_record(self._intake_dir_for(intake_id))

    def list_intakes(self) -> list[IntakeRecord]:
        """Return every known intake, oldest first (ids sort by start time)."""
        with self._lock:
            return [_load_record(d) for d in self._intake_dirs()]

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
        for intake_dir in self._intake_dirs():
            record = _load_record(intake_dir)
            if record.status in _TERMINAL:
                continue
            if record.repo_path == repo.path:
                raise IntakeBusyError(repo.name, record.intake_id)

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
