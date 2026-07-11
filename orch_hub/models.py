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


@dataclass(frozen=True)
class CommitInfo:
    """A managed repo's git commit, for the dashboard's status views.

    Display-only metadata, deliberately separate from readiness: readiness runs
    git as a launch *gate* (is the tree clean?), while this is history shown on
    the repo card and detail view. ``committed_ts`` is git's strict ISO-8601
    committer date (``%cI``, carrying the original offset), taken verbatim — the
    hub does not reformat or re-zone it. A commit-lookup failure is represented
    as absence (``None`` here, or an empty/`available: false` log elsewhere), so
    it never hides the repo or its readiness.
    """

    hash: str
    short_hash: str
    committed_ts: str
    subject: str

    def to_dict(self) -> dict:
        return {
            "hash": self.hash,
            "short_hash": self.short_hash,
            "committed_ts": self.committed_ts,
            "subject": self.subject,
        }


def repo_readiness_dict(
    repo: ManagedRepo,
    report: ReadinessReport,
    commit: CommitInfo | None = None,
    housekeeping_pending: bool = False,
) -> dict:
    """Combine repo status metadata into per-repo JSON.

    ``commit`` is the repo's most recent commit for the dashboard repo card, or
    ``None`` when git could not be read (best-effort: a commit-lookup failure
    must never hide the repo or its readiness). It defaults to ``None`` so a
    caller that has no commit (or does not want the git cost) still gets the
    stable shape with ``commit: null``.

    ``housekeeping_pending`` reflects only the display marker
    ``.ai/.housekeeping-pending``. It is informational dashboard/API metadata
    and deliberately separate from readiness.
    """
    return {
        **repo.to_dict(),
        "readiness": report.to_dict(),
        "commit": commit.to_dict() if commit is not None else None,
        "housekeeping_pending": housekeeping_pending,
    }


def repos_payload(
    entries: Iterable[
        tuple[ManagedRepo, ReadinessReport]
        | tuple[ManagedRepo, ReadinessReport, CommitInfo | None]
        | tuple[ManagedRepo, ReadinessReport, CommitInfo | None, bool]
    ],
) -> dict:
    """Build the top-level ``{"repos": [...]}`` payload for CLI/API output.

    Each entry is ``(repo, report)``, ``(repo, report, commit)``, or
    ``(repo, report, commit, housekeeping_pending)``; omitted optional values
    keep ``commit: null`` and ``housekeeping_pending: false``, so this stays the
    single shared helper for both ``python -m orch_hub.cli repos --json`` and
    ``GET /api/repos``.
    """
    return {"repos": [repo_readiness_dict(*entry) for entry in entries]}


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

    def managed_repo(self) -> ManagedRepo | None:
        """Reconstruct the run's :class:`ManagedRepo` from persisted fields.

        Returns ``None`` for a stub record with no ``repo_path`` (an unreadable
        ``run.json``), so callers that need to reach the repo — the run-scoped
        task read, task-completion classification — can resolve it from run
        identity alone without re-consulting the registry. The manifest is the
        deployment-standard path (not persisted on the record); task-file reads
        use only ``path``, so its exact value does not affect them.
        """
        if self.repo_path is None:
            return None
        return ManagedRepo(
            name=self.repo_name or self.repo_path.name,
            path=self.repo_path,
            manifest=self.repo_path / ".ai-deploy-manifest.json",
        )

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


@dataclass(frozen=True)
class IntakeDraft:
    """A complete proposed task, as an intake agent drafts it before any write.

    The draft is the whole review artifact: the dashboard shows ``task_file``
    (the full ``.ai-tasks/<id>.md`` content the agent would write) and
    ``index_entry`` (the ``.ai-tasks/index.md`` row it would append) so the
    human sees exactly what confirmation will create. It is deliberately *not*
    a task: it lives only in hub-owned intake state, never in the target repo
    or the active task inventory, until an explicit confirmation. ``task_id`` is
    the proposed id (it can change across revisions); ``notes`` carries agent
    clarifications such as duplicate-overlap or oversized-scope warnings.
    """

    task_id: str
    summary: str
    task_file: str
    index_entry: str
    notes: str | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "summary": self.summary,
            "task_file": self.task_file,
            "index_entry": self.index_entry,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class IntakeRecord:
    """One remote intake session, as persisted in ``intake/<id>/intake.json``.

    The record — not hub memory — is the source of truth for an intake, so the
    current draft and lifecycle status survive a hub restart and stay
    inspectable for audit. ``status`` is an intake lifecycle label
    (``drafting``/``awaiting_review``/``revising``/``confirming`` while an agent
    turn is in flight, and the terminal ``confirmed``/``cancelled``/``failed``);
    it is deliberately distinct from task frontmatter status and from run
    overlay states. ``draft`` holds the complete current proposal (``None`` only
    before the first draft lands or when a draft turn failed). ``iteration`` is
    the revision count (0 = initial draft). ``error`` describes a failure;
    ``created_task_id`` is set only after a confirmed write. ``agent_ref`` is the
    persistable conversational handle for the intake session and is internal
    (never serialized to API callers), like a run's control dir.
    """

    intake_id: str
    repo_name: str
    repo_path: Path | None
    request: str
    status: str
    draft: IntakeDraft | None = None
    iteration: int = 0
    created_ts: str | None = None
    updated_ts: str | None = None
    error: str | None = None
    created_task_id: str | None = None
    agent_ref: str | None = None

    def managed_repo(self) -> ManagedRepo | None:
        """Reconstruct the intake's :class:`ManagedRepo` from persisted fields.

        Mirrors :meth:`RunRecord.managed_repo`: callers that must reach the repo
        (the confirming write, a reattached manager) resolve it from intake
        identity alone. Returns ``None`` for a record with no ``repo_path``.
        """
        if self.repo_path is None:
            return None
        return ManagedRepo(
            name=self.repo_name or self.repo_path.name,
            path=self.repo_path,
            manifest=self.repo_path / ".ai-deploy-manifest.json",
        )

    def to_dict(self) -> dict:
        # ``agent_ref`` is deliberately internal (the conversational handle the
        # UI never needs), so it stays in ``intake.json`` and out of API output.
        return {
            "intake_id": self.intake_id,
            "repo_name": self.repo_name,
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "request": self.request,
            "status": self.status,
            "draft": self.draft.to_dict() if self.draft is not None else None,
            "iteration": self.iteration,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "error": self.error,
            "created_task_id": self.created_task_id,
        }


@dataclass(frozen=True)
class IntakeEvent:
    """One persisted intake audit event.

    Like :class:`RunEvent`, event files store metadata only: an unconfirmed
    draft is never copied here, so audit stays reconstructable without treating
    a draft as a task. ``kind`` records the lifecycle step (creation, revision,
    confirmation, cancellation, failure); ``iteration`` ties a revision event to
    its intake iteration; ``task_id`` carries the proposed id on draft events and
    the created id on confirmation; ``detail`` is a concise human-readable note
    (for a revision, the feedback that drove it).
    """

    ts: str
    kind: str
    intake_id: str
    repo_name: str = ""
    repo_path: Path | None = None
    iteration: int | None = None
    task_id: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "kind": self.kind,
            "intake_id": self.intake_id,
            "repo_name": self.repo_name,
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "iteration": self.iteration,
            "task_id": self.task_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class IntakeHistoryEntry:
    """Intake read model: the current record plus its persisted audit events."""

    record: IntakeRecord
    events: tuple[IntakeEvent, ...] = ()

    def to_dict(self) -> dict:
        return {
            **self.record.to_dict(),
            "events": [event.to_dict() for event in self.events],
        }
