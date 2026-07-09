"""Typed domain data shared by the CLI and future API routes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ManagedRepo:
    """A managed repository discovered from ai-native-development."""

    name: str
    path: Path
    manifest: Path

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "manifest": str(self.manifest),
        }


@dataclass(frozen=True)
class ReadinessCheck:
    """One launch-readiness check outcome.

    ``name`` is a stable machine-readable key (e.g. ``git_clean``); ``message``
    is a concise human-readable detail or failure reason.
    """

    name: str
    passed: bool
    message: str

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "message": self.message}


@dataclass(frozen=True)
class ReadinessReport:
    """Aggregate launch readiness for a managed repo.

    ``checks`` holds every check that ran, in order, so callers can inspect the
    full picture. ``ready`` is true only when all checks passed. The serialized
    form (:meth:`to_dict`) exposes just the failures as reasons, matching the
    documented ``{"ready": ..., "checks": [...]}`` API shape.
    """

    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> tuple[ReadinessCheck, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "checks": [
                {"name": c.name, "message": c.message} for c in self.failures
            ],
        }


def repo_readiness_dict(repo: ManagedRepo, report: ReadinessReport) -> dict:
    """Combine a repo and its readiness into the public per-repo JSON shape."""
    return {**repo.to_dict(), "readiness": report.to_dict()}


def repos_payload(
    pairs: Iterable[tuple[ManagedRepo, ReadinessReport]],
) -> dict:
    """Build the top-level ``{"repos": [...]}`` payload for CLI/API output."""
    return {"repos": [repo_readiness_dict(repo, report) for repo, report in pairs]}


@dataclass(frozen=True)
class TaskSummary:
    """One managed-repo task, as read from its ``.ai-tasks`` files.

    ``status`` is the task-file frontmatter status
    (``pending``/``in_progress``/``final_review``/``completed``/``blocked``, or a
    parser sentinel). It is deliberately kept distinct from the future run
    overlay labels (``active``/``waiting``) the run manager will attach once run
    data exists. ``summary`` is the human text from the index row when present;
    ``blockers`` holds the frontmatter blocker entries (empty unless blocked).
    """

    task_id: str
    status: str
    summary: str | None = None
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class RepoTaskInventory:
    """A managed repo paired with its active-task summaries.

    ``index_present`` is False when the repo has no ``.ai-tasks/index.md``, which
    callers surface distinctly from a present-but-empty index. ``tasks`` follows
    the order of the index rows.
    """

    repo: ManagedRepo
    index_present: bool
    tasks: tuple[TaskSummary, ...]

    def to_dict(self) -> dict:
        return {
            **self.repo.to_dict(),
            "index_present": self.index_present,
            "tasks": [task.to_dict() for task in self.tasks],
        }


def tasks_payload(inventories: Iterable[RepoTaskInventory]) -> dict:
    """Build the top-level ``{"repos": [...]}`` task payload for CLI/API output."""
    return {"repos": [inventory.to_dict() for inventory in inventories]}


@dataclass(frozen=True)
class Question:
    """One orchestrator human-input request read from a run control dir.

    ``seq`` comes from the question filename, which is authoritative over any
    ``seq`` field inside the payload. ``banner`` is the audit text; ``message``
    is the display alias (identical to ``banner`` in v1). Payload fields are
    ``None`` when absent or not strings; ``error`` is set when the question file
    itself cannot be read as a JSON object.

    ``answered`` is True only when a same-seq answer file exists *and* satisfies
    the orchestrator's validity rule (a JSON object with a non-empty string
    ``answer``). A present-but-malformed answer file keeps the question pending
    — the orchestrator re-reads it each tick, never consumes it — and is
    described by ``answer_error`` so callers can repair it by rewriting.
    ``answer`` carries the stripped text of the valid answer when answered
    (the same value the orchestrator consumes; ``"stop"`` aborts the run).
    """

    seq: int
    path: Path
    ts: str | None = None
    kind: str | None = None
    banner: str | None = None
    message: str | None = None
    error: str | None = None
    answered: bool = False
    answer: str | None = None
    answer_error: str | None = None

    @property
    def pending(self) -> bool:
        return not self.answered

    def to_dict(self) -> dict:
        # ``path`` is deliberately internal: the UI gets run-level context from
        # the future run manager, not filesystem locations.
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "banner": self.banner,
            "message": self.message,
            "error": self.error,
            "answered": self.answered,
            "answer": self.answer,
            "answer_error": self.answer_error,
        }


@dataclass(frozen=True)
class RunRecord:
    """One orchestrator run, as persisted in its run dir's ``run.json``.

    The record — not hub memory — is the source of truth for a run: a fresh
    run manager reattaches by loading records and probing pid liveness. ``pid``
    is the ``caffeinate`` wrapper's pid, which tracks the orchestrator (it
    exits when the orchestrator exits and forwards its exit code).

    ``ended_ts``/``exit_code`` stay ``None`` while the run is alive.
    ``ended_ts`` is when the hub *observed* the end, not the exact exit time;
    ``exit_code`` remains ``None`` on a run that ended while the hub was down
    (a reattached process is no longer our child, so its status is gone).
    ``spawn_out`` is the subprocess stdout+stderr capture path; it is the only
    diagnostic for a run that failed *before* the orchestrator opened its own
    log file (bad credential, missing CLI on PATH, import error), which the
    logs route falls back to when no orchestrator log is located.
    ``error`` is never persisted: it is set only on stub records built from an
    unreadable ``run.json``, so a broken dir cannot hide a run. Stubs carry
    ``None`` placeholders for the unknown fields.
    """

    run_id: str
    repo_name: str = ""
    repo_path: Path | None = None
    task_id: str = ""
    task_status: str | None = None
    control_dir: Path | None = None
    log_dir: Path | None = None
    spawn_out: Path | None = None
    argv: tuple[str, ...] = ()
    pid: int | None = None
    started_ts: str | None = None
    ended_ts: str | None = None
    exit_code: int | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        # ``control_dir``, ``log_dir``, ``spawn_out``, and ``argv`` are
        # deliberately internal (filesystem detail the UI does not need); they
        # stay in ``run.json``.
        return {
            "run_id": self.run_id,
            "repo_name": self.repo_name,
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "task_id": self.task_id,
            "task_status": self.task_status,
            "pid": self.pid,
            "started_ts": self.started_ts,
            "ended_ts": self.ended_ts,
            "exit_code": self.exit_code,
            "error": self.error,
        }


@dataclass(frozen=True)
class RunStatus:
    """A run record paired with its computed live/end state.

    ``state`` is a run-overlay label, deliberately distinct from task
    frontmatter statuses: ``active``/``waiting`` while the process is alive
    (``waiting`` = a control-dir question is pending a human answer), and
    ``completed``/``stopped``/``error``/``ended`` for finished runs.
    ``pending`` counts unanswered control-dir questions.
    """

    record: RunRecord
    state: str
    detail: str | None = None
    pending: int = 0

    def to_dict(self) -> dict:
        return {
            **self.record.to_dict(),
            "state": self.state,
            "detail": self.detail,
            "pending_questions": self.pending,
        }


@dataclass(frozen=True)
class RunEvent:
    """One persisted audit/history event for a run.

    Event files deliberately store metadata only. Human answer text is not part
    of this model; the control-dir answer file is the contract artifact that
    contains the exact answer consumed by the orchestrator.
    """

    ts: str
    kind: str
    run_id: str
    repo_name: str = ""
    repo_path: Path | None = None
    task_id: str = ""
    task_status: str | None = None
    state: str | None = None
    detail: str | None = None
    exit_code: int | None = None
    seq: int | None = None
    question_ts: str | None = None
    question_kind: str | None = None
    question_banner: str | None = None
    question_message: str | None = None
    responder: str | None = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "kind": self.kind,
            "run_id": self.run_id,
            "repo_name": self.repo_name,
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "task_id": self.task_id,
            "task_status": self.task_status,
            "state": self.state,
            "detail": self.detail,
            "exit_code": self.exit_code,
            "seq": self.seq,
            "question_ts": self.question_ts,
            "question_kind": self.question_kind,
            "question_banner": self.question_banner,
            "question_message": self.question_message,
            "responder": self.responder,
        }


@dataclass(frozen=True)
class RunHistoryEntry:
    """Recent-run read model: current status plus persisted audit events."""

    status: RunStatus
    events: tuple[RunEvent, ...] = ()

    def to_dict(self) -> dict:
        return {
            **self.status.to_dict(),
            "events": [event.to_dict() for event in self.events],
        }
