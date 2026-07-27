"""Repo-level admission: a live run and a remote intake never coexist.

Run control and remote intake are independent subsystems that both act on one
target repo's working tree. A hub-driven ``/intake-task`` session reads the repo
to draft a task and, on confirmation, writes ``.ai-tasks/``; a live orchestrator
run is concurrently rewriting that same tree. Overlapping them drafts against a
moving snapshot and races two writers over repo task state, so the hub admits
**at most one of the two per repo**:

- a run start is refused while the repo has a non-terminal intake
  (:class:`IntakeInFlightError`),
- an intake start is refused while the repo has a live run
  (:class:`RunLiveError`).

Both directions are decided inside **one per-repo critical section**
(:meth:`RepoAdmission.admit_run` / :meth:`RepoAdmission.admit_intake`), held
until the winner's record is persisted. Two simultaneous starts for one idle
repo therefore cannot both pass: the loser observes the winner's persisted
record and is refused. Different repos hold different locks, preserving the
existing "cross-repo runs may overlap" property.

Occupancy is **not** duplicated here. The coordinator asks each subsystem
through an injected probe (:data:`RepoOccupancyProbe`) so the file-backed run
records and intake records stay the single source of truth — an in-memory
reservation table would have to be rebuilt on restart and could diverge from
what ``runs/`` and ``intake/`` actually contain.

Locking is one level deep, which is what keeps it deadlock-free: a caller takes
a repo lock and then at most its **own** manager lock, and the probes take no
manager lock at all (both are read-only scans; see ``live_run_for`` /
``active_intake_for``). So no lock is ever held while acquiring a lock of the
same kind, and no manager lock is ever held while waiting for a repo lock. It
also keeps repos independent: the repo lock is per repo, and a probe never
queues behind another repo's launch or intake turn.

:meth:`require_no_live_run` — the fail-closed guard an intake calls from inside
its own lock — therefore takes neither a repo lock nor a manager lock. It needs
no section: an existing non-terminal intake already refuses every run start, so
the only overlap it can see is a pre-existing (legacy or restart-recovered) one,
which no mutual exclusion could have prevented.

This is admission, not readiness. Readiness answers "is this repo in a launchable
state"; admission answers "is this repo already claimed by the other subsystem".
A live run dirties the working tree, so ``git_clean`` usually fails during one —
reporting that as the reason an intake was refused would be a misleading proxy.
Callers therefore run the admission check *before* their readiness gate.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .models import ManagedRepo

RepoOccupancyProbe = Callable[[ManagedRepo], str | None]
"""Reports the id of a subsystem's current occupant of ``repo``, else ``None``.

Implemented by :meth:`orch_hub.runs.RunManager.live_run_for` (live run id) and
:meth:`orch_hub.intake.IntakeManager.active_intake_for` (non-terminal intake id).
Each manager binds its own probe to the coordinator it is constructed with.
"""


def _unoccupied(repo: ManagedRepo) -> str | None:
    """Default probe for an unbound subsystem: nothing to conflict with.

    A coordinator with no run manager bound is a process with no run manager at
    all (a unit test, or the CLI), so "no live run" is the truth, not a
    fail-open. Both probes are bound at manager construction, before any request
    thread exists.
    """
    return None


class AdmissionConflictError(Exception):
    """A repo is already claimed by the other subsystem (fail closed).

    Distinct from each manager's own same-kind serialization error
    (``RepoBusyError`` for run-vs-run, ``IntakeBusyError`` for intake-vs-intake):
    those guarantees are unchanged and still enforced by their own manager.
    """


class RunLiveError(AdmissionConflictError):
    """An intake operation was refused: the repo has a live orchestrator run.

    ``run_id`` is the blocking run, carried structurally so the API can hand the
    client a direct link instead of a parsed message.
    """

    def __init__(
        self, repo_name: str, run_id: str, action: str = "start an intake"
    ):
        self.repo_name = repo_name
        self.run_id = run_id
        self.action = action
        super().__init__(
            f"cannot {action} for repo {repo_name!r}: run {run_id} is live"
            " (a run and a remote intake cannot share a repo)"
        )


class IntakeInFlightError(AdmissionConflictError):
    """A run start was refused: the repo has a non-terminal remote intake.

    ``intake_id`` is the blocking intake. It settles by review (confirm/cancel)
    or by failing, at which point the run start is admitted.
    """

    def __init__(
        self, repo_name: str, intake_id: str, action: str = "start a run"
    ):
        self.repo_name = repo_name
        self.intake_id = intake_id
        self.action = action
        super().__init__(
            f"cannot {action} for repo {repo_name!r}: intake {intake_id} is in"
            " flight (a run and a remote intake cannot share a repo)"
        )


class RepoAdmission:
    """The per-repo critical section shared by run launch and intake start.

    One instance lives in the resident hub service and is handed to both the
    :class:`~orch_hub.runs.RunManager` and the
    :class:`~orch_hub.intake.IntakeManager`; each binds its occupancy probe on
    construction. A manager constructed without one gets its own private
    coordinator, which then only enforces the (vacuous) unbound-peer case — that
    is the standalone-manager test path, where the peer subsystem does not exist.

    Binding happens at construction, before any request thread, so the probe
    attributes need no lock of their own. One hub process has exactly one manager
    of each kind; binding a second of the same kind replaces the probe.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        # Per-repo locks, created on first use and keyed by repo path (the same
        # identity run records and intake records are matched on). Bounded by the
        # number of managed repos.
        self._repo_locks: dict[str, threading.Lock] = {}
        self._live_run: RepoOccupancyProbe = _unoccupied
        self._active_intake: RepoOccupancyProbe = _unoccupied

    def bind_run_source(self, probe: RepoOccupancyProbe) -> None:
        """Register the run subsystem's live-run probe."""
        self._live_run = probe

    def bind_intake_source(self, probe: RepoOccupancyProbe) -> None:
        """Register the intake subsystem's non-terminal-intake probe."""
        self._active_intake = probe

    @contextmanager
    def admit_run(self, repo: ManagedRepo) -> Iterator[None]:
        """Hold ``repo``'s admission section for a run start, or refuse it.

        Raises :class:`IntakeInFlightError` when a non-terminal intake holds the
        repo. The caller must persist its run record before leaving the block:
        the section is what makes a concurrent intake start observe this run.
        """
        with self._repo_lock(repo):
            intake_id = self._active_intake(repo)
            if intake_id is not None:
                raise IntakeInFlightError(repo.name, intake_id)
            yield

    @contextmanager
    def admit_intake(self, repo: ManagedRepo) -> Iterator[None]:
        """Hold ``repo``'s admission section for an intake start, or refuse it.

        Raises :class:`RunLiveError` when a live run holds the repo. The caller
        must persist its intake record before leaving the block, for the same
        reason as :meth:`admit_run`.
        """
        with self._repo_lock(repo):
            run_id = self._live_run(repo)
            if run_id is not None:
                raise RunLiveError(repo.name, run_id)
            yield

    def require_no_live_run(self, repo: ManagedRepo, action: str) -> None:
        """Fail closed when ``repo`` has a live run, without taking the section.

        The guard for intake operations that are not admissions but must not
        proceed across an overlap that already exists — a legacy record, or a
        run reattached after a restart alongside a surviving ``awaiting_review``
        intake. Taking no repo lock is deliberate: the caller holds the intake
        manager lock, and the lock order in the module docstring forbids
        acquiring a repo lock beneath it.
        """
        run_id = self._live_run(repo)
        if run_id is not None:
            raise RunLiveError(repo.name, run_id, action)

    def _repo_lock(self, repo: ManagedRepo) -> threading.Lock:
        with self._guard:
            key = str(repo.path)
            lock = self._repo_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._repo_locks[key] = lock
            return lock
