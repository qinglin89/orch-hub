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
    """

    seq: int
    path: Path
    ts: str | None = None
    kind: str | None = None
    banner: str | None = None
    message: str | None = None
    error: str | None = None
    answered: bool = False
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
            "answer_error": self.answer_error,
        }
