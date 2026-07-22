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

    ``stop_requested`` is True only while the run is *live* and a graceful stop
    has been requested (its ``stop.flag`` exists). It is server-derived on every
    render, so a reloaded dashboard — or another device — keeps showing the
    disabled "Stop requested" control from run state rather than transient
    client button text. A finished run leaves it False: a stop it honored is
    already conveyed by the ``stopped`` state/detail, so the field carries no
    separate terminal semantics.
    """

    record: RunRecord
    state: str
    detail: str | None = None
    pending: int = 0
    stop_requested: bool = False

    def to_dict(self) -> dict:
        return {
            **self.record.to_dict(),
            "state": self.state,
            "detail": self.detail,
            "pending_questions": self.pending,
            "stop_requested": self.stop_requested,
        }


@dataclass(frozen=True)
class RunEvent:
    """One persisted audit/history event for a run.

    Event files deliberately store metadata only. Human answer text is not part
    of this model; the control-dir answer file is the contract artifact that
    contains the exact answer consumed by the orchestrator. On an
    ``answer_recorded`` event, ``mode`` records the answer's button intent
    (:data:`orch_hub.control_dir.ANSWER_MODES` — ``answer``/``discuss``), which is
    intent metadata, not the answer text.
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
    mode: str | None = None

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
            "mode": self.mode,
        }


@dataclass(frozen=True)
class RunHistoryEntry:
    """Recent-run read model: current status, persisted audit events, the run's
    per-session context summary, and the run task's session estimate.

    ``sessions`` is the run's persisted :class:`SessionsSnapshot` (the
    ``sessions.json`` written once at the live->ended transition), embedded so
    the history view renders each run's context strips without an extra per-run
    request. It is an empty snapshot for a live or pre-field run that has none;
    ``None`` is normalized to an empty snapshot in :meth:`to_dict` so the
    serialized shape always carries ``sessions``/``context_budget``. The
    snapshot's own keys (``sessions``, ``context_budget``) are spread at the top
    level, matching the ``GET /api/runs/{run_id}/sessions`` route shape so a
    client reads them identically from either surface.

    ``session_est`` is the run task's frontmatter :class:`SessionEst`, read
    *current* (so a task whose est total was later raised shows the new total),
    embedded so the history view's est-vs-actual task rollup needs no per-task
    request. ``None`` — serialized as ``session_est: null`` — is "unknown": a
    task with no readable/parseable ``session-est``. The backend reads it once
    per ``(repo, task)`` and stamps the same value on each of that task's runs.
    """

    status: RunStatus
    events: tuple[RunEvent, ...] = ()
    sessions: SessionsSnapshot | None = None
    session_est: SessionEst | None = None

    def to_dict(self) -> dict:
        snapshot = self.sessions if self.sessions is not None else SessionsSnapshot()
        return {
            **self.status.to_dict(),
            "events": [event.to_dict() for event in self.events],
            **snapshot.to_dict(),
            "session_est": (
                self.session_est.to_dict() if self.session_est is not None else None
            ),
        }


CONTEXT_BUDGET_TOKENS = 200_000
"""Soft display reference for per-session context: one effective context window
(~200k tokens, the protocol's ``session-est`` sizing). It is a line the dashboard
renders context against, never a cap — a real dev session's context can exceed it
(an observed run peaked at ~278k). Kept here as shared domain data so the
snapshot's default does not depend on the sessions module."""


@dataclass(frozen=True)
class SessionSummary:
    """One protocol session (or its plan-gate) parsed from a run's log.

    The orchestrator log brackets each session between ``--- {role} session
    start``/``end`` markers (and each dev plan-gate between ``--- {role} plan-gate
    start``/``end``); the per-turn ``run finished: ... context≈N tokens`` lines in
    between report that conversation's context size. This is the parsed summary of
    one such bracket — deliberately *not* a task or run status, but display/audit
    metadata derived from log text (see :mod:`orch_hub.sessions`).

    ``role`` is ``dev``/``review``; ``kind`` distinguishes a normal ``session``
    from a dev ``plan-gate`` (the planning pre-session, which loops its own
    turns). ``sid`` is the backend session id, reconciled to the real value when
    the start line logged ``(pending)`` and the end line revealed it (``None``
    while still pending). ``turns`` counts the bracket's ``run finished`` lines;
    ``context_tokens`` is the latest one's ``context≈`` (monotonic within a
    conversation, so latest ≈ peak; ``None`` with zero turns). Each session is a
    fresh backend conversation, so context is per-session, not cumulative across
    the run. ``status_before`` is the task status logged at start; ``end_status``
    the status at end (``None`` for a plan-gate, which logs no end status, and for
    a still-open session). ``start_hms`` is the ``HH:MM:SS`` time-of-day of the
    start line — a display stamp only, never a duration source (the log carries no
    date and wraps past midnight). ``open`` is True when no end line was seen: a
    live session's trailing bracket, or a truncated tail.
    """

    role: str
    kind: str
    sid: str | None = None
    turns: int = 0
    context_tokens: int | None = None
    status_before: str | None = None
    end_status: str | None = None
    start_hms: str | None = None
    open: bool = False

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "kind": self.kind,
            "sid": self.sid,
            "turns": self.turns,
            "context_tokens": self.context_tokens,
            "status_before": self.status_before,
            "end_status": self.end_status,
            "start_hms": self.start_hms,
            "open": self.open,
        }


@dataclass(frozen=True)
class SessionsSnapshot:
    """A run's ordered per-session summaries plus the context-budget reference.

    ``sessions`` is in log order (each plan-gate immediately before its sibling
    dev session). ``context_budget`` is the soft display reference
    (:data:`CONTEXT_BUDGET_TOKENS`) the dashboard renders context against; it is
    carried on the snapshot so a persisted audit keeps the value it was written
    with even if the constant later changes. An empty ``sessions`` is a valid
    snapshot — a run with no log yet, a log cleaned before its end was observed,
    or one with no parseable markers.
    """

    sessions: tuple[SessionSummary, ...] = ()
    context_budget: int = CONTEXT_BUDGET_TOKENS

    def to_dict(self) -> dict:
        return {
            "sessions": [summary.to_dict() for summary in self.sessions],
            "context_budget": self.context_budget,
        }


@dataclass(frozen=True)
class SessionEst:
    """A task's frontmatter ``session-est: <current>/<total>`` estimate.

    ``current`` is the task file's own dev-session progress count (it increments
    at each dev-advancement session entry); ``total`` is the estimated
    dev-session budget. The dashboard's est-vs-actual rollup measures the
    dev-session count *observed in persisted run history* against ``total`` — a
    figure independent of ``current``, which is the task's self-reported
    progress. Parsed by :func:`orch_hub.task_store.read_session_est`; a missing
    or malformed est is represented as absence (``None`` at the call sites),
    never a sentinel here, so both fields are always real parsed integers.
    """

    current: int
    total: int

    def to_dict(self) -> dict:
        return {"current": self.current, "total": self.total}


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


@dataclass(frozen=True)
class EvaluationTarget:
    """The task an evaluation is about, resolved to its file and where it lives.

    Output of the read-only target resolver (:mod:`orch_hub.evaluation`).
    ``source`` is ``"active"`` when the file is the live ``.ai-tasks/<id>.md``,
    ``"archived"`` when close-out has moved it to ``.ai-tasks/archive/<id>.md``,
    and ``None`` when neither exists (``present=False``, ``content=None``) — a
    task id with no file, evaluable for its run history alone. ``content`` is the
    full task-file markdown: task files are small and are the primary subject of
    evaluation, so unlike run logs they are not bounded. ``repo_path`` is retained
    for related-run aggregation and is serialized like :class:`RunRecord`'s.
    """

    repo_name: str
    repo_path: Path | None
    task_id: str
    source: str | None
    present: bool
    content: str | None = None

    def to_dict(self) -> dict:
        return {
            "repo_name": self.repo_name,
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "task_id": self.task_id,
            "source": self.source,
            "present": self.present,
            "content": self.content,
        }


@dataclass(frozen=True)
class RelatedRun:
    """One hub run that belongs to an evaluated task, plus its run dir.

    ``run_dir`` is carried (not just ``record.run_id``) because the evidence
    builder reads a run's history (``events.jsonl``), session snapshot
    (``sessions.json``), and startup capture (``spawn.out``) from it; it is
    internal filesystem detail and is excluded from :meth:`to_dict`, matching the
    other run/question models. The serialized shape is just the run record.
    """

    record: RunRecord
    run_dir: Path

    def to_dict(self) -> dict:
        return self.record.to_dict()


@dataclass(frozen=True)
class RunRollup:
    """One related run's deterministic, evidence-derived static metrics.

    Output of the static evaluator (:mod:`orch_hub.static_metrics`), computed
    read-only from the run's evidence — record, metadata-only history events, and
    per-session context snapshot — with no model call. ``outcome`` is one of the
    ``RUN_OUTCOME_*`` values (an *evidence-only* classification from raw
    ``ended_ts``/``exit_code``/``error`` + the ``stop_requested`` event, kept
    deliberately distinct from the run manager's live verdict, which needs
    control-dir/task-store reads the pack does not carry). ``ended`` is the raw
    presence of ``ended_ts`` (matches :attr:`RunEvidence.ended`), *not* the
    outcome — a ``broken`` run has ``ended=False`` yet is not ``live``.
    ``duration_seconds`` is ``ended_ts - started_ts`` when both parse (``None``
    when either is absent/malformed or the difference is negative). Session counts
    come from the snapshot: ``dev``/``review`` count ``kind == "session"``
    brackets by role, ``plan_gate`` counts the dev planning pre-session.
    ``peak_context_tokens`` is the max ``context≈`` across the run's sessions
    (``None`` when none reported one). ``has_diagnostic`` is whether any
    failure-explaining text survived — a non-empty orchestrator log tail or
    ``spawn.out`` startup capture.
    """

    run_id: str
    task_status_at_launch: str | None
    outcome: str
    ended: bool
    exit_code: int | None
    stop_requested: bool
    duration_seconds: float | None
    answer_count: int
    dev_session_count: int
    review_session_count: int
    plan_gate_count: int
    total_turns: int
    peak_context_tokens: int | None
    has_diagnostic: bool
    warning_count: int

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "task_status_at_launch": self.task_status_at_launch,
            "outcome": self.outcome,
            "ended": self.ended,
            "exit_code": self.exit_code,
            "stop_requested": self.stop_requested,
            "duration_seconds": self.duration_seconds,
            "answer_count": self.answer_count,
            "dev_session_count": self.dev_session_count,
            "review_session_count": self.review_session_count,
            "plan_gate_count": self.plan_gate_count,
            "total_turns": self.total_turns,
            "peak_context_tokens": self.peak_context_tokens,
            "has_diagnostic": self.has_diagnostic,
            "warning_count": self.warning_count,
        }


@dataclass(frozen=True)
class RunMetrics:
    """Task-level run/session/event rollup derived from one evidence pack.

    The static evaluator's (:mod:`orch_hub.static_metrics`) aggregate of every
    related run's :class:`RunRollup`, plus the pack-level facts a static score
    needs: the ``evaluation_id`` it derives from (so a persisted report ties to a
    specific pack), whether the target task file was found and where
    (``target_source``: ``"active"``/``"archived"``/``None`` — the
    close-out/archive-state signal), and the total evidence-degradation
    ``warning_count`` (pack-level plus every run's). The outcome counts partition
    the runs (``run_count == live + clean + failed + stopped + ended_unknown +
    broken``). ``failed_without_diagnostic_count`` is the reliability red flag — a
    failed/broken run with no log or ``spawn.out`` to explain it.
    ``total_duration_seconds`` and ``peak_context_tokens`` are ``None`` when no
    run reported a measurable value, kept distinct from ``0``.

    Session-2 composes this into the full static-metrics report with protocol
    metrics parsed from the task file, score components, and warnings.
    """

    evaluation_id: str
    target_present: bool
    target_source: str | None
    run_count: int
    live_count: int
    clean_count: int
    failed_count: int
    stopped_count: int
    ended_unknown_count: int
    broken_count: int
    failed_without_diagnostic_count: int
    human_input_count: int
    dev_session_count: int
    review_session_count: int
    plan_gate_count: int
    total_turns: int
    peak_context_tokens: int | None
    total_duration_seconds: float | None
    warning_count: int
    runs: tuple[RunRollup, ...] = ()

    @property
    def failure_count(self) -> int:
        """Runs that failed: non-zero exit (``failed``) plus unreadable records
        (``broken``). The scope's "run failure count"."""
        return self.failed_count + self.broken_count

    def to_dict(self) -> dict:
        return {
            "evaluation_id": self.evaluation_id,
            "target_present": self.target_present,
            "target_source": self.target_source,
            "run_count": self.run_count,
            "live_count": self.live_count,
            "clean_count": self.clean_count,
            "failed_count": self.failed_count,
            "stopped_count": self.stopped_count,
            "ended_unknown_count": self.ended_unknown_count,
            "broken_count": self.broken_count,
            "failure_count": self.failure_count,
            "failed_without_diagnostic_count": self.failed_without_diagnostic_count,
            "human_input_count": self.human_input_count,
            "dev_session_count": self.dev_session_count,
            "review_session_count": self.review_session_count,
            "plan_gate_count": self.plan_gate_count,
            "total_turns": self.total_turns,
            "peak_context_tokens": self.peak_context_tokens,
            "total_duration_seconds": self.total_duration_seconds,
            "warning_count": self.warning_count,
            "runs": [run.to_dict() for run in self.runs],
        }


@dataclass(frozen=True)
class ProtocolMetrics:
    """Deterministic protocol-structure facts parsed from the evaluated task file.

    Output of the static evaluator (:mod:`orch_hub.static_metrics`), computed
    read-only from the evidence pack's task content (the full ``.ai-tasks/<id>.md``
    markdown) with no model call. These are *facts only* — the interpretation
    (scores, warnings) lives in :class:`ScoreComponent`, so this shape stays a
    stable, greppable record of what the task file structurally contains.

    Presence: ``task_content_present`` is False for a task with no file (an id
    evaluable only for run history); every downstream fact then degrades to its
    empty/zero default rather than crashing. ``frontmatter_present`` is whether
    the file opened with a ``---`` fence.

    Frontmatter legality (taskfile schema §2): ``frontmatter_status`` is the raw
    ``status`` value; ``status_legal`` whether it is one of the five lifecycle
    statuses; ``id_matches`` whether frontmatter ``id`` equals the evaluated task
    id; ``session_est`` the parsed ``current/total`` (``None`` when absent or
    malformed, with ``session_est_present``/``session_est_wellformed`` splitting
    those two cases); ``blocker_rule_ok`` whether the blocked⇔≥1-blocker rule
    holds; ``fix_set_present`` whether a ``fix-set`` line is set (it must be
    cleared before completion); ``prefetch_present``/``prefetch_wellformed``
    describe the optional ``prefetch`` list.

    Body (taskfile §5): ``missing_sections`` are required ``##`` headings absent
    from the file; ``has_session_plan`` whether the optional plan section exists.

    Session log (taskfile §5): ``entry_count`` counts ``###`` entries;
    ``work_entry_count``/``review_entry_count``/``malformed_entry_count``
    partition them (a heading that does not parse is malformed).
    ``uncovered_work_entries`` are work session ids with no ``review of <id>``
    entry; ``pass_verdict_count``/``changes_requested_count`` tally review
    verdicts; ``remediation_count`` is work entries whose most recent preceding
    review requested changes (the taskfile remediation predicate);
    ``blocked_entry_count`` is entries transitioning to ``blocked``;
    ``dispute_unresolved_count`` counts ``Dispute-unresolved`` markers;
    ``incomplete_entries`` are entry ids missing a required field.

    Transition path (taskfile §3): ``illegal_transitions`` are ``"a -> b"``
    strings for entry status transitions not in the legal table;
    ``transition_continuity_ok`` whether each entry's start status equals the
    previous entry's end status; ``final_status_matches`` whether the last
    entry's end status equals the frontmatter status.

    Completion: ``completed`` mirrors ``frontmatter_status == "completed"``;
    ``final_open_none`` is whether the last ``Open:`` field in the log is
    ``none`` (required only when completed — every forward-looking item resolved).
    """

    task_content_present: bool
    frontmatter_present: bool
    frontmatter_status: str | None
    status_legal: bool
    id_present: bool
    id_matches: bool
    session_est: SessionEst | None
    session_est_present: bool
    session_est_wellformed: bool
    blocker_rule_ok: bool
    fix_set_present: bool
    prefetch_present: bool
    prefetch_wellformed: bool
    missing_sections: tuple[str, ...]
    has_session_plan: bool
    session_log_present: bool
    entry_count: int
    work_entry_count: int
    review_entry_count: int
    malformed_entry_count: int
    uncovered_work_entries: tuple[str, ...]
    pass_verdict_count: int
    changes_requested_count: int
    remediation_count: int
    blocked_entry_count: int
    dispute_unresolved_count: int
    incomplete_entries: tuple[str, ...]
    illegal_transitions: tuple[str, ...]
    transition_continuity_ok: bool
    final_status_matches: bool
    completed: bool
    final_open_none: bool

    def to_dict(self) -> dict:
        return {
            "task_content_present": self.task_content_present,
            "frontmatter_present": self.frontmatter_present,
            "frontmatter_status": self.frontmatter_status,
            "status_legal": self.status_legal,
            "id_present": self.id_present,
            "id_matches": self.id_matches,
            "session_est": (
                self.session_est.to_dict() if self.session_est is not None else None
            ),
            "session_est_present": self.session_est_present,
            "session_est_wellformed": self.session_est_wellformed,
            "blocker_rule_ok": self.blocker_rule_ok,
            "fix_set_present": self.fix_set_present,
            "prefetch_present": self.prefetch_present,
            "prefetch_wellformed": self.prefetch_wellformed,
            "missing_sections": list(self.missing_sections),
            "has_session_plan": self.has_session_plan,
            "session_log_present": self.session_log_present,
            "entry_count": self.entry_count,
            "work_entry_count": self.work_entry_count,
            "review_entry_count": self.review_entry_count,
            "malformed_entry_count": self.malformed_entry_count,
            "uncovered_work_entries": list(self.uncovered_work_entries),
            "pass_verdict_count": self.pass_verdict_count,
            "changes_requested_count": self.changes_requested_count,
            "remediation_count": self.remediation_count,
            "blocked_entry_count": self.blocked_entry_count,
            "dispute_unresolved_count": self.dispute_unresolved_count,
            "incomplete_entries": list(self.incomplete_entries),
            "illegal_transitions": list(self.illegal_transitions),
            "transition_continuity_ok": self.transition_continuity_ok,
            "final_status_matches": self.final_status_matches,
            "completed": self.completed,
            "final_open_none": self.final_open_none,
        }


@dataclass(frozen=True)
class ScoreComponent:
    """One evidence-backed static score axis, computed deterministically.

    The static evaluator (:mod:`orch_hub.static_metrics`) reports a small set of
    these rather than one opaque number, so a degraded score is always explained
    by its ``warnings`` (each names the evidence that lowered it). ``name`` is a
    stable machine key (``protocol``/``reliability``/``handoff``/``estimate``/
    ``knowledge``). ``score`` is ``0.0``–``1.0`` (1.0 = clean), or ``None`` when
    the axis is *not assessable* from the evidence (no task file, no runs, no
    estimate) — kept distinct from a real ``0.0``. ``confidence`` is ``0.0``–
    ``1.0`` and drops as backing evidence goes missing/corrupt, so a partial pack
    lowers confidence instead of asserting a false score. ``warnings`` are the
    human-readable, evidence-pointing reasons the score fell short (empty when
    clean).
    """

    name: str
    score: float | None
    confidence: float
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "score": self.score,
            "confidence": self.confidence,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class StaticMetricsReport:
    """The full deterministic static-metrics report for one evaluated task.

    Top-level output of the static evaluator (:mod:`orch_hub.static_metrics`),
    persisted as ``static_metrics.json`` beside the ``evidence.json`` it derives
    from (one dir per evaluation), so a report always ties to a specific pack via
    ``evaluation_id``. It composes the run/session rollup (:class:`RunMetrics`),
    the parsed task-file protocol facts (:class:`ProtocolMetrics`), and the
    evidence-backed :class:`ScoreComponent` axes. ``warnings`` is the pack's
    *evidence-degradation* list (missing/corrupt run artifacts) surfaced at the
    top level, kept separate from the per-score quality warnings. ``generated_ts``
    stamps when the report was computed. Task identity (``repo_name``/``task_id``/
    ``target_present``/``target_source``) is carried so the artifact is
    self-describing without re-reading the pack. No semantic judgment, no model
    call — every field is a pure function of the evidence pack.
    """

    evaluation_id: str
    generated_ts: str | None
    repo_name: str
    task_id: str
    target_present: bool
    target_source: str | None
    runs: RunMetrics
    protocol: ProtocolMetrics
    scores: tuple[ScoreComponent, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "evaluation_id": self.evaluation_id,
            "generated_ts": self.generated_ts,
            "repo_name": self.repo_name,
            "task_id": self.task_id,
            "target_present": self.target_present,
            "target_source": self.target_source,
            "scores": [score.to_dict() for score in self.scores],
            "warnings": list(self.warnings),
            "runs": self.runs.to_dict(),
            "protocol": self.protocol.to_dict(),
        }


@dataclass(frozen=True)
class StoredEvaluation:
    """One persisted evaluation directory for a ``(repo, task_id)``, enumerated.

    Output of :meth:`orch_hub.evaluation.EvaluationService.list_evaluations`: the
    ``evaluation_id`` naming the dir plus ``artifacts``, the sorted filenames
    actually present in it (e.g. ``evidence.json``, ``static_metrics.json`` — an
    evidence-only generation has just the former). It is a structural directory
    manifest, deliberately not the artifact *contents*: the evaluation-history
    picker needs only which evaluations exist and what each carries, and reads a
    specific artifact on demand. The API maps ``artifacts`` membership to explicit
    ``has_evidence``/``has_static_metrics`` booleans so the concrete filenames
    stay an internal detail of the evaluation subsystem.
    """

    evaluation_id: str
    artifacts: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "evaluation_id": self.evaluation_id,
            "artifacts": list(self.artifacts),
        }
