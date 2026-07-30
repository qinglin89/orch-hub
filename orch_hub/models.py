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

    ``effective_config`` is the run's authoritative ``effective-config:`` launch
    snapshot (the raw JSON dict the orchestrator logged at startup — same shape as
    :class:`EffectiveConfig`), embedded so the history view shows what actually
    launched (profile/models/efforts/sources/revisions) without a per-run follow-up
    request. ``None`` — serialized as ``effective_config: null`` — is "unknown": a
    run with no persisted snapshot yet (live/pre-field) or whose startup line could
    not be parsed. Read snapshot-only here (cheap), like ``sessions``.
    """

    status: RunStatus
    events: tuple[RunEvent, ...] = ()
    sessions: SessionsSnapshot | None = None
    session_est: SessionEst | None = None
    effective_config: dict | None = None

    def to_dict(self) -> dict:
        snapshot = self.sessions if self.sessions is not None else SessionsSnapshot()
        return {
            **self.status.to_dict(),
            "events": [event.to_dict() for event in self.events],
            **snapshot.to_dict(),
            "session_est": (
                self.session_est.to_dict() if self.session_est is not None else None
            ),
            "effective_config": self.effective_config,
        }


HISTORY_PAGE_SIZE = 50
"""Run-history page size: how many runs one page of run history holds.

Fixed, not caller-supplied. Paging is a traversal contract — every persisted run
reachable exactly once by walking cursors — and a size a client could vary
between two steps would make each cursor mean a different window depending on
who asked, on top of unbounding the per-request read. A caller that wants a
short glanceable strip asks for the *summary* instead, which is a separate
contract (:data:`HISTORY_SUMMARY_MAX`) and not walkable."""

HISTORY_SUMMARY_MAX = 200
"""Upper clamp on a bounded newest-first history summary (the Overview's
recent-outcomes strip). A summary carries no cursors and no totals, so a
caller-chosen size here can never become a page size."""


@dataclass(frozen=True)
class RunHistoryPage:
    """One cursor-anchored page of run history: entries plus what a pager needs.

    Paging is anchored, not positional, in *both* directions. Each cursor names a
    run id and a direction in the manager's stable newest-first ordering: the
    older-direction one the response hands back starts the next window at that run
    (inclusive), the newer-direction one ends the window above at it (exclusive).
    Because every step is a real run id rather than an offset into a list re-read
    on every request, a run persisted or pruned between two requests cannot shift
    a window under the pager: the next page never repeats the previous page's last
    run and never skips its own first one, stepping back never returns a window
    overlapping the one it came from, and no run can be pushed past the traversal
    by later insertions. A cursor whose run has since been pruned still resolves —
    the ordering position is what is anchored, not the directory.

    ``prev_cursor``/``next_cursor`` are ``None`` exactly at the boundaries, which
    is what ``has_prev``/``has_next`` report; a client navigates by echoing a
    cursor back, never by computing one. Either is handed back only when a run
    exists that way, so a control never offers a step into nothing — unless that
    neighbourhood is pruned before the step is taken, which is the explicit
    empty-window state below.

    ``offset`` (not serialized) is where this window begins in the ordering *as
    read for this response*, and ``page``/``total_pages`` derive from it for the
    "Page N of M" indicator. Those are display data, not navigation inputs: with
    history mutating between requests a position is a description, while the
    cursor is the contract. A window past the last run — its cursor's
    neighbourhood was pruned — carries no entries and reports ``total_pages + 1``,
    so it reads as past the end rather than as a real page.

    ``total_runs`` counts every persisted run dir, not just this page: the pager's
    status line and the "page-local results are not global results" contract both
    depend on it.
    """

    entries: tuple[RunHistoryEntry, ...] = ()
    offset: int = 0
    page_size: int = HISTORY_PAGE_SIZE
    total_runs: int = 0
    prev_cursor: str | None = None
    next_cursor: str | None = None

    @property
    def page(self) -> int:
        """1-based position of this window; ``total_pages + 1`` past the end.

        Counted as *windows above this one, plus one* (a ceiling), not
        ``offset // page_size``: a mutation mid-traversal can leave a window off
        the page grid — its anchor's position no longer a multiple of the size —
        and the floor form would then report page 1 for a window that does have a
        Previous, the indicator contradicting the controls beside it. The ceiling
        keeps "page 1" and "no Previous" the same fact.

        An empty history is page 1 of 0 pages — the view has a position even
        when there is nothing at it.
        """
        if self.offset >= self.total_runs:
            return self.total_pages + 1
        return -(-self.offset // self.page_size) + 1

    @property
    def total_pages(self) -> int:
        """Number of pages the current total spans; 0 for an empty history."""
        if self.page_size <= 0:
            return 0
        return -(-self.total_runs // self.page_size)

    @property
    def has_prev(self) -> bool:
        return self.prev_cursor is not None

    @property
    def has_next(self) -> bool:
        return self.next_cursor is not None

    def to_dict(self) -> dict:
        return {
            "runs": [entry.to_dict() for entry in self.entries],
            "page": self.page,
            "page_size": self.page_size,
            "total_runs": self.total_runs,
            "total_pages": self.total_pages,
            "has_prev": self.has_prev,
            "has_next": self.has_next,
            "prev_cursor": self.prev_cursor,
            "next_cursor": self.next_cursor,
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
class RoleConfig:
    """One role's resolved agent/model/effort inside an :class:`EffectiveConfig`.

    The deployed orchestrator's ``--print-config`` output nests a ``dev`` and a
    ``review`` object of exactly this shape. ``agent`` is the CLI agent name for a
    cc-codex role (``claude``/``codex`` for dev, ``codex`` for review) or
    ``cursor`` for a Cursor-backend role, and is ``None`` only when the
    orchestrator omitted it (e.g. a Cursor dev role, which has no separate CLI
    agent). ``model``/``effort`` are the resolved values; they are display-only
    here — orch-hub never validates or duplicates them, the orchestrator owns
    that policy.
    """

    agent: str | None
    model: str | None
    effort: str | None

    def to_dict(self) -> dict:
        return {"agent": self.agent, "model": self.model, "effort": self.effort}


@dataclass(frozen=True)
class EffectiveConfig:
    """The deployed orchestrator's resolved launch configuration, as one snapshot.

    Two sources produce this exact shape (the orchestrator's own
    ``launch_config_dict``): a preview query
    (:func:`orch_hub.orch_config.query_effective_config` over ``--print-config``,
    where the launch fields ``task_id``/``once``/``plan_gate``/``control_dir`` are
    null/false) and the authoritative ``effective-config:`` line every real run
    logs at startup (where they are real). orch-hub treats the orchestrator as the
    single owner of model/effort/profile policy: this is a read-only projection it
    displays and persists, never a place it invents defaults.

    ``config_revision`` is the sha256 of ``orchestrator.toml`` alone;
    ``effective_revision`` also changes when an environment override or the
    selected resolution changes, so it is the correct preview cache key.
    ``profile`` is the *run-wide* ``--profile`` selection or ``"default"``;
    ``available_profiles`` is the set a UI may offer (``standard``/``excellent``).
    ``sources`` is diagnostic provenance per field (``cli``/``profile:<name>``/
    ``env:<NAME>``/``config``). ``codex_sandbox`` is ``None`` for the Cursor
    backend. Built strictly (fail-closed) by
    :func:`orch_hub.orch_config.parse_effective_config`.

    ``dev_profile``/``review_profile`` are the per-role selections the run
    actually resolved with, from the orchestrator's authoritative ``profiles``
    object. ``split_profiles`` records whether that object was really observed:
    an older deployment omits it, and both role values are then *derived* from
    the run-wide ``profile`` so legacy configs stay readable. A UI must gate
    independent role selection on ``split_profiles`` — deriving a value is not
    evidence that the deployment understands ``--dev-profile``/
    ``--review-profile``.
    """

    schema_version: int
    config_revision: str | None
    effective_revision: str | None
    profile: str
    available_profiles: tuple[str, ...]
    dev_profile: str
    review_profile: str
    split_profiles: bool
    backend: str
    dev: RoleConfig
    review: RoleConfig
    max_sessions: int | None
    context_budget: int | None
    codex_sandbox: str | None
    sources: dict[str, str]
    task_id: str | None = None
    once: bool = False
    plan_gate: bool = False
    control_dir: str | None = None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "config_revision": self.config_revision,
            "effective_revision": self.effective_revision,
            "profile": self.profile,
            "profiles": {"dev": self.dev_profile, "review": self.review_profile},
            "split_profiles": self.split_profiles,
            "available_profiles": list(self.available_profiles),
            "backend": self.backend,
            "dev": self.dev.to_dict(),
            "review": self.review.to_dict(),
            "max_sessions": self.max_sessions,
            "context_budget": self.context_budget,
            "codex_sandbox": self.codex_sandbox,
            "sources": dict(self.sources),
            "task_id": self.task_id,
            "once": self.once,
            "plan_gate": self.plan_gate,
            "control_dir": self.control_dir,
        }


@dataclass(frozen=True)
class LaunchSpec:
    """A dashboard launch-mode selection, before it becomes orchestrator argv.

    The input to :func:`orch_hub.orch_config.build_launch_args`, kept as typed
    data (not raw arg strings) so command construction is one tested function
    rather than logic duplicated across the API and the browser.

    The selection comes in exactly one of two mutually exclusive shapes, each
    using the same four UI modes (``default``/``standard``/``excellent``/
    ``custom``):

    - ``dev_mode``/``review_mode`` — the independent per-role selection the
      split Start dialog sends. Named roles emit ``--dev-profile``/
      ``--review-profile``; an omitted role is ``default`` and emits nothing.
    - ``mode`` — the legacy run-wide selection, kept verbatim for pre-split API
      callers: named modes emit the ``--profile`` shorthand for both roles.

    Setting both shapes is a :class:`~orch_hub.orch_config.LaunchError`, as is
    setting neither. ``backend`` is ``None`` to *inherit* the orchestrator's
    configured backend (legal only when every role is ``default``, which then
    omits ``--backend``) or an explicit ``cursor``/``cc-codex``. The
    ``dev_*``/``review_*`` value fields are used only by a ``custom`` role (a
    preset takes those values from the orchestrator, never from the user), and
    ``dev_agent`` applies only to cc-codex — it is never sent to Cursor. The
    orthogonal launch flags ``plan_gate``/``once``/``max_sessions`` keep their
    existing per-flag UI semantics and are honored in every mode.
    """

    mode: str | None = None
    dev_mode: str | None = None
    review_mode: str | None = None
    backend: str | None = None
    dev_agent: str | None = None
    dev_model: str | None = None
    dev_effort: str | None = None
    review_model: str | None = None
    review_effort: str | None = None
    plan_gate: bool = False
    once: bool = False
    max_sessions: int | None = None


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
class SemanticEvaluatorInfo:
    """Which evaluator produced a semantic report — the rerun/version identity.

    Output of the semantic evaluator (:mod:`orch_hub.semantic`), stamped by the
    harness (not the model) so a report says exactly what judged it. ``backend``
    is the concrete adapter (``"fake"`` in tests, ``"claude-cli"`` for the real
    driver); ``model`` is the model id it ran (``None`` for a backend with no
    model, e.g. the unavailable default); ``schema_version`` is the semantic
    prompt/report contract version the report conforms to. The scope asks for
    reruns with different evaluator versions/models preserved for comparison:
    each semantic evaluation lands in its own evaluation dir, and this triple is
    how two reports over the same task are told apart.
    """

    backend: str
    model: str | None
    schema_version: str

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "model": self.model,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class SemanticDoctor:
    """A semantic-evaluator readiness/smoke check — is Layer 2 runnable and how.

    Output of the evaluator doctor (:meth:`orch_hub.semantic.SemanticBackend.doctor`
    and :meth:`~orch_hub.semantic.SemanticEvaluator.doctor`): a
    *configuration/availability* probe, deliberately NOT a full evaluation (no
    model call, no cost). It carries the evaluator identity (``backend``/``model``/
    ``schema_version`` — the same triple as :class:`SemanticEvaluatorInfo`) plus
    ``available`` (whether the backend can actually run — e.g. the ``claude`` CLI
    resolves on the augmented PATH) and a human-readable ``detail`` (the resolved
    path, or why it is unavailable). It gates the dashboard's Layer 2 control and
    the ``orch-hub eval doctor`` CLI; it never gates run Start/Stop/log flows.
    """

    backend: str
    model: str | None
    schema_version: str
    available: bool
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "model": self.model,
            "schema_version": self.schema_version,
            "available": self.available,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SemanticScore:
    """One semantic score axis with its explanation (never a naked number).

    Part of the semantic report (:mod:`orch_hub.semantic`). Unlike the static
    :class:`ScoreComponent` — whose ``warnings`` are deterministic
    evidence-pointing penalties — a semantic score carries a required, free-text
    ``rationale`` the evaluator model wrote: the scope forbids a naked overall
    score, so every axis must justify itself. ``dimension`` is a semantic axis
    key (e.g. ``handoff_completeness``); ``score`` is ``0.0``–``1.0`` (1.0 =
    strong) or ``None`` when the model could not assess it from the evidence;
    ``confidence`` is ``0.0``–``1.0``.
    """

    dimension: str
    score: float | None
    confidence: float
    rationale: str

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "score": self.score,
            "confidence": self.confidence,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class SemanticFinding:
    """One semantic diagnosis: a dimension, its severity, and how to fix it.

    Part of the semantic report (:mod:`orch_hub.semantic`). Where a
    :class:`SemanticScore` rates an axis, a finding names a concrete issue on it:
    ``dimension`` is the semantic axis, ``severity`` one of
    ``info``/``low``/``medium``/``high``, ``summary`` a one-line claim, ``detail``
    the optional explanation, ``confidence`` the model's ``0.0``–``1.0`` certainty,
    ``evidence_refs`` the pointers into the evidence it rests on (e.g.
    ``"session-log entry sid-b"``, ``"score:handoff"``), and ``suggestion`` a
    concrete improvement (often a system/protocol change, per the scope's
    "suggested system improvements"). The evidence refs are what keep a finding
    auditable rather than an unfalsifiable assertion.
    """

    dimension: str
    severity: str
    summary: str
    confidence: float
    evidence_refs: tuple[str, ...] = ()
    detail: str | None = None
    suggestion: str | None = None

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "severity": self.severity,
            "summary": self.summary,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "detail": self.detail,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class SemanticReport:
    """The full semantic evaluation report for one task, persisted as
    ``semantic_report.json`` beside ``evidence.json``/``static_metrics.json``.

    Top-level output of the semantic evaluator (:mod:`orch_hub.semantic`): the
    Layer-2 counterpart to :class:`StaticMetricsReport`, judged by a model over a
    bounded evidence input rather than computed deterministically. Task identity
    and provenance are stamped by the harness so the artifact is self-describing
    and the model cannot spoof what it evaluated: ``evaluation_id`` names the
    evaluation dir (shared with the evidence/static artifacts it consumed),
    ``evaluator`` records which backend/model/version produced it (rerun
    identity), and ``input_evidence_id``/``input_static_metrics`` link the report
    to the evidence and static-metrics inputs actually used.

    The model supplies the judgment: ``summary`` (narrative overall assessment —
    the explanation that replaces a naked overall score), ``scores`` (per-axis,
    each with a rationale), ``findings`` (diagnoses with severity and fixes), and
    ``improvements`` (suggested system/protocol changes). ``overall_confidence``
    is the model's confidence *after* the harness degrades it for missing/partial
    evidence (so a thin evidence input lowers confidence instead of failing the
    evaluation); ``warnings`` records those evidence omissions/degradations.
    """

    evaluation_id: str
    generated_ts: str | None
    repo_name: str
    task_id: str
    target_present: bool
    target_source: str | None
    evaluator: SemanticEvaluatorInfo
    summary: str | None
    overall_confidence: float
    scores: tuple[SemanticScore, ...] = ()
    findings: tuple[SemanticFinding, ...] = ()
    improvements: tuple[str, ...] = ()
    input_evidence_id: str | None = None
    input_static_metrics: bool = False
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "evaluation_id": self.evaluation_id,
            "generated_ts": self.generated_ts,
            "repo_name": self.repo_name,
            "task_id": self.task_id,
            "target_present": self.target_present,
            "target_source": self.target_source,
            "evaluator": self.evaluator.to_dict(),
            "summary": self.summary,
            "overall_confidence": self.overall_confidence,
            "scores": [score.to_dict() for score in self.scores],
            "findings": [finding.to_dict() for finding in self.findings],
            "improvements": list(self.improvements),
            "input_evidence_id": self.input_evidence_id,
            "input_static_metrics": self.input_static_metrics,
            "warnings": list(self.warnings),
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


@dataclass(frozen=True)
class SemanticJob:
    """One async Layer-2 semantic-evaluation attempt for a ``(repo, task_id)``.

    Output of the semantic-job manager (:mod:`orch_hub.semantic_jobs`), persisted
    as ``semantic-jobs/<repo>/<task>/job.json`` so the in-flight and terminal
    state of a long semantic model turn is observable across reload, navigation,
    and another browser session — the state the one-shot synchronous
    :meth:`orch_hub.semantic.SemanticEvaluator.evaluate` call left invisible.
    There is exactly one record per ``(repo, task)`` (a new attempt overwrites the
    prior terminal one), so this is the *current* attempt's status, not a history.

    ``status`` is a semantic-job lifecycle label, deliberately distinct from task
    frontmatter status, run overlay states, and intake status: ``queued`` (created,
    not yet started) and ``running`` (the model turn is in flight) are transient;
    ``completed`` and ``failed`` are terminal. ``backend``/``model``/
    ``schema_version`` are the selected evaluator identity (the
    :class:`SemanticEvaluatorInfo` triple), stamped at creation so the
    queued/running UI shows which model is running before any report exists.
    ``evaluation_id`` is set only on ``completed`` and links the job to the
    freshly written evaluation dir the dashboard then selects; ``error`` is the
    bounded failure diagnostic on ``failed`` (never full prompt text or unbounded
    model output). ``started_ts`` marks the queued→running transition and
    ``ended_ts`` the terminal transition (both ``None`` until they happen).
    """

    job_id: str
    repo_name: str
    repo_path: Path | None
    task_id: str
    status: str
    backend: str | None = None
    model: str | None = None
    schema_version: str | None = None
    evaluation_id: str | None = None
    error: str | None = None
    created_ts: str | None = None
    updated_ts: str | None = None
    started_ts: str | None = None
    ended_ts: str | None = None

    def managed_repo(self) -> ManagedRepo | None:
        """Reconstruct the job's :class:`ManagedRepo` from persisted fields.

        Mirrors :meth:`RunRecord.managed_repo`/:meth:`IntakeRecord.managed_repo`:
        the off-lock evaluation turn resolves the repo from job identity alone.
        Returns ``None`` for a record with no ``repo_path`` (an unreadable
        ``job.json``), so such a record can never drive an evaluation.
        """
        if self.repo_path is None:
            return None
        return ManagedRepo(
            name=self.repo_name or self.repo_path.name,
            path=self.repo_path,
            manifest=self.repo_path / ".ai-deploy-manifest.json",
        )

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "repo_name": self.repo_name,
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "task_id": self.task_id,
            "status": self.status,
            "backend": self.backend,
            "model": self.model,
            "schema_version": self.schema_version,
            "evaluation_id": self.evaluation_id,
            "error": self.error,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "started_ts": self.started_ts,
            "ended_ts": self.ended_ts,
        }


# -- agent subscription usage -------------------------------------------------
# Read-only, display-only view of the *agent CLI subscriptions* (Claude, Codex)
# the hub drives — how much of each provider's rate-limit windows is spent and
# when they reset — so the Operations Overview can show available agent capacity
# without conflating it with repo health, run state, or launch gates. The shapes
# are provider-neutral and normalized (:mod:`orch_hub.usage` fills them from each
# provider's own local signal); they are strictly informational and never feed
# readiness, attention, or any run/intake decision.

PROVIDER_CLAUDE = "claude"
PROVIDER_CODEX = "codex"


@dataclass(frozen=True)
class UsageCredits:
    """Optional reset-credit balance a provider exposes alongside its limits.

    Codex reports a ``credits`` object (whether the account holds rate-limit
    reset credits, and a coarse balance). This carries only the *aggregate*
    fields — ``has_credits`` / ``unlimited`` / ``balance`` — and deliberately
    never any per-credit / reset-credit *identifier* (the scope bars exposing
    those). ``balance`` is kept as the provider's opaque string (e.g. ``"0"``);
    it is a count, not an id. Reading this never *consumes* a reset credit — the
    hub only ever reads the local rate-limit signal, never the credit-spend path.
    """

    has_credits: bool | None = None
    unlimited: bool | None = None
    balance: str | None = None

    def to_dict(self) -> dict:
        return {
            "has_credits": self.has_credits,
            "unlimited": self.unlimited,
            "balance": self.balance,
        }


@dataclass(frozen=True)
class UsageWindow:
    """One rate-limit window (bucket) of a provider subscription.

    A provider exposes one or more windows — Codex a ``primary`` (often 5h) and
    optional ``secondary`` (often weekly); Claude a five-hour and a seven-day
    window. ``key`` is the stable machine key (``"primary"``/``"secondary"`` or
    ``"five_hour"``/``"seven_day"``) and ``label`` the human name. Usage is
    expressed as ``used_percent`` with a derived ``remaining_percent`` — the UI
    must not invent a fixed message total, so ``used``/``limit`` are populated
    *only* when the provider gives an exact numeric count/limit (neither Claude
    nor Codex does today; both report percent). ``resets_at`` is the window reset
    time as an ISO-8601 UTC string (derived from the provider's epoch), or
    ``None`` when unknown. Any field the provider omits stays ``None`` rather than
    defaulting to zero, so a missing signal never reads as "0% used".

    ``resets_at`` may be refreshed on its own, without the figures: Claude's
    boundary arrives on a channel that carries no percentage (see
    ``orch_hub.usage.merge_window_boundaries``). A consumer therefore must not
    assume the percentage was observed *inside* the window this boundary closes —
    ``window_minutes`` plus the provider's ``observed_ts`` are what decide that,
    and a boundary already in the past means no window is open at all.

    ``figure_instant`` is the reset boundary of the window ``used_percent`` was
    actually measured in — the figure's *own* window identity. At observation it
    equals ``resets_at``, but a boundary-only refresh advances ``resets_at``
    (the displayed boundary) ahead of it, so the two must stay distinct: the
    per-window sink merge (``orch_hub.usage.merge_observations``) keys window
    identity on ``figure_instant``, never on the displayable ``resets_at``.
    Store-internal only — deliberately excluded from :meth:`to_dict` so the
    ``GET /api/usage`` payload is unchanged; it round-trips through the Claude
    observation store instead.
    """

    key: str
    label: str | None = None
    window_minutes: int | None = None
    used_percent: float | None = None
    remaining_percent: float | None = None
    resets_at: str | None = None
    used: int | None = None
    limit: int | None = None
    figure_instant: str | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "window_minutes": self.window_minutes,
            "used_percent": self.used_percent,
            "remaining_percent": self.remaining_percent,
            "resets_at": self.resets_at,
            "used": self.used,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class ProviderUsage:
    """Normalized subscription-usage snapshot for one provider (Claude or Codex).

    ``available`` is the load-bearing flag: ``False`` means the hub has no usable
    signal (never observed, unsupported CLI output, a read failure) and the UI
    shows *unavailable* rather than zero usage — ``windows`` is empty and
    ``detail`` says why. When ``True``, ``windows`` carries the parsed buckets,
    ``plan`` the sanitized plan/tier name (never an account or credential id),
    and ``credits`` the optional reset-credit balance. ``observed_ts`` is when the
    *figures* were captured (ISO-8601 UTC) — a boundary-only refresh of a window
    does not move it — and ``stale`` marks an observation older than the freshness
    threshold, still shown but flagged as last-observed rather than current.
    Everything here is display-only.
    """

    provider: str
    available: bool
    plan: str | None = None
    windows: tuple[UsageWindow, ...] = ()
    credits: UsageCredits | None = None
    observed_ts: str | None = None
    stale: bool = False
    detail: str | None = None

    @classmethod
    def unavailable(cls, provider: str, detail: str | None = None) -> "ProviderUsage":
        """A fail-soft snapshot: no usable signal, so the UI shows unavailable."""
        return cls(provider=provider, available=False, detail=detail)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "available": self.available,
            "plan": self.plan,
            "windows": [w.to_dict() for w in self.windows],
            "credits": self.credits.to_dict() if self.credits is not None else None,
            "observed_ts": self.observed_ts,
            "stale": self.stale,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SubscriptionUsage:
    """The whole agent-subscription-usage payload: one entry per provider.

    The single object the usage HTTP route returns and the Overview renders. Each
    provider is independent — one being unavailable never suppresses the other,
    and the presence of this payload never gates any operational decision.
    """

    providers: tuple[ProviderUsage, ...] = ()

    def to_dict(self) -> dict:
        return {"providers": [p.to_dict() for p in self.providers]}
