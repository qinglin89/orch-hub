"""Typed domain data shared by the CLI and future API routes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ManagedRepo:
    """A managed repository discovered from mandrel."""

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
class ProtocolPlacement:
    """Which line ``mandrel`` placed a repo's deployed protocol on, verbatim.

    Relayed, never derived. ``mandrel status`` owns the one canonical-identity
    comparison — two commits carrying one deployable canonical tree are one
    protocol and place identically — and it is the only thing entitled to run
    it. The hub reads its answer out of the payload and renders it; a second
    placement spelled here would be a second answer to a question that must have
    exactly one. So no git call, no read of ``.ai-deploy-lock.json`` or the
    manifest, no comparison of revisions, and no ordering of one line against
    another happens on this path.

    Two fields because ``mandrel``'s reading has two faces and publishes both
    (its ``PlacementReading``): ``line`` is the name of the line whose protocol
    the repo's receipt carries (``targets[].placed_line``), and ``finding`` is
    what that same comparison becomes when there is no line to name — the
    placement finding's ``kind``. Exactly one is set when ``mandrel`` said
    anything about placement; both are ``None`` when it said nothing, which
    covers a reading it never reached (no receipt at all), a line *this
    checkout* could not resolve — a fact about the mandrel repository and not
    about the target, so relaying it as the target's line would blame the wrong
    one — and every way the payload was unavailable or unreadable.

    Both values are ``mandrel``'s vocabulary and are kept opaque: the hub holds
    no dictionary of line names to bucket them into, so a value it has never
    seen travels through and renders as itself.
    """

    line: str | None = None
    finding: str | None = None


@dataclass(frozen=True)
class ReadinessReport:
    """Aggregate launch readiness for a managed repo.

    ``checks`` holds every check that ran, in order, so callers can inspect the
    full picture. ``ready`` is true only when all checks passed. The serialized
    form (:meth:`to_dict`) exposes just the failures as reasons, matching the
    documented ``{"ready": ..., "checks": [...]}`` API shape.

    ``placement`` is a passenger, not a check: it rides here because it comes
    back on the ``mandrel status`` call the readiness gate already makes (one
    subprocess, not two), and it is deliberately absent from :meth:`to_dict`.
    It never touches ``ready``, ``failures``, or any gate — display only. It
    could not ride a check even if it wanted to: ``to_dict`` serializes only
    failures, so a passing check's message never leaves the process.
    """

    checks: tuple[ReadinessCheck, ...]
    placement: ProtocolPlacement = ProtocolPlacement()

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

    ``placed_line`` / ``placement_finding`` are siblings of ``readiness`` for
    the same reason, and are taken from the report's passenger
    (:class:`ProtocolPlacement`) rather than from a check — ``readiness`` lists
    only failures, so a passing check's message never reaches a client. Both
    keys are always present, ``null`` where ``mandrel`` named nothing: a field
    that appears and vanishes by case is one every consumer has to branch on
    before it can read anything.

    ``placed_line`` is not a revision and does not answer ``commit``'s question.
    Placement is canonical identity, so a repo whose receipt names a commit
    other than base's, while its deployable canonical tree *is* base's, is on
    base; pairing the two as though they were one fact would present that repo
    as diverged.
    """
    return {
        **repo.to_dict(),
        "readiness": report.to_dict(),
        "commit": commit.to_dict() if commit is not None else None,
        "housekeeping_pending": housekeeping_pending,
        "placed_line": report.placement.line,
        "placement_finding": report.placement.finding,
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

    ``auto_semantic_eval`` is the launch-time opt-out for this run's automatic
    Layer 2: ``False`` means a completion this run produces enqueues no semantic
    turn and records a terminal ``skipped`` job instead (Layer 1 still runs — it
    makes no model call, so skipping it would save nothing and forfeit the whole
    deterministic layer). Hub-owned by construction: it never reaches the
    orchestrator, which parses argv strictly and would abort a launch on an
    unknown flag. Absent from a persisted record means **on**, so every run
    written before the field existed still evaluates.

    ``end_settling`` is the second half of the end write: ``ended_ts`` settles a
    run permanently (the manager never revisits it), but the work that end
    *triggers* — the evidence settles, the notification, the completion handed
    to the evaluation sidecar — happens after it and is not in the same file. So
    the end is written in two phases: this flag goes true in the same atomic
    write that sets ``ended_ts``, and false once that tail has run. A record
    found ended with it still true belongs to a hub that stopped mid-transition,
    and the tail is resumed. Absent means false, which is what bounds the resume
    to runs a hub with this field actually ended — a record written before it
    existed is settled by definition and never replayed. Internal like
    ``control_dir``/``argv``: it stays in ``run.json``.
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
    auto_semantic_eval: bool = True
    end_settling: bool = False
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

    ``protocol`` is the run's persisted protocol provenance — the deployed
    protocol identity captured at launch and the verdict of the end check over it
    (:meth:`orch_hub.protocol_provenance.ProtocolSnapshot.to_dict`, including its
    derived ``coverage``) — embedded the same way and for the same reason: it is a
    small per-run artifact the history view shows beside the launch snapshot, and
    a per-run fetch for it would be one request per row. It is the field a
    published report's cohort key comes from, so whether a run carries one is
    exactly what an operator cannot otherwise see without walking run dirs.
    ``None`` — serialized as ``protocol: null`` — is "unknown" in the same sense
    ``effective_config`` is: a run launched before the capture existed, which is
    the artifact's own absent case and not a verdict about that run's deployment.
    """

    status: RunStatus
    events: tuple[RunEvent, ...] = ()
    sessions: SessionsSnapshot | None = None
    session_est: SessionEst | None = None
    effective_config: dict | None = None
    protocol: dict | None = None

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
            "protocol": self.protocol,
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
    read for this response*, and ``page``/``total_pages`` count this traversal's
    windows from it for the "Page N of M" indicator. Those are display data, not
    navigation inputs: with history mutating between requests a position is a
    description, while the cursor is the contract. A window past the last run —
    its cursor's neighbourhood was pruned — carries no entries and reports
    ``total_pages + 1``, so it reads as past the end rather than as a real page.

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
        """1-based position of this window in the traversal it belongs to.

        Counted as *the windows above this one, plus one* (a ceiling), not
        ``offset // page_size``: a mutation mid-traversal can leave a window off
        the page grid — its anchor's position no longer a multiple of the size —
        and the floor form would then report page 1 for a window that does have a
        Previous, the indicator contradicting the controls beside it. The ceiling
        keeps "page 1" and "no Previous" the same fact.

        An empty window has no runs to be the page *of*, so this count lands at
        ``total_pages + 1``: a position past everything that exists, which is what
        makes the past-the-end state read as past the end. An empty history is
        page 1 of 0 pages — the view has a position even with nothing at it.
        """
        return self._windows_above + 1

    @property
    def total_pages(self) -> int:
        """How many windows this traversal holds: above, this one, and below.

        Windows of the walk, not ``total_runs / page_size`` slots of the
        snapshot. The two agree for every traversal that sits on the snapshot's
        page grid — which is every traversal nothing was persisted or pruned in
        the middle of. When something was, the walk genuinely has a window the
        grid does not: reverse windows realign from the bottom (each is bounded
        *up* from a real run), so a run added mid-walk ends up alone in a ragged
        partial window at the top. Counting the walk's own windows is what keeps
        the pair honest there — ``1 <= page <= total_pages`` for every non-empty
        window, from either direction — where a grid count would pair a
        legitimate third window with "of 2".

        An empty window counts for nothing, so an empty history is 0 pages and a
        past-the-end window reports the pages that do exist (its ``page`` being
        one past them).
        """
        return self._windows_above + (1 if self.entries else 0) + self._windows_below

    @property
    def _windows_above(self) -> int:
        """Windows between the newest run and this one — the Previous chain."""
        return self._window_count(self.offset)

    @property
    def _windows_below(self) -> int:
        """Windows between this one and the oldest run — the Next chain."""
        return self._window_count(self.total_runs - self.offset - len(self.entries))

    def _window_count(self, runs: int) -> int:
        """Windows that ``runs`` consecutive runs fill, the last one partial."""
        if self.page_size <= 0:
            return 0
        return -(-max(0, runs) // self.page_size)

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

    ``options`` is the orchestrator's selection-independent launch catalog — what
    any launch MAY select, beside the values this one DID select — normalized by
    :func:`orch_hub.orch_config.parse_option_catalog` to the subset a caller
    renders: ``{"agents": {backend: {role: {agent: {effort_axis, efforts,
    models: [{id, effort_axis, efforts}]}}}}}``. ``None`` means the payload
    published no usable catalog, and a caller must then keep free-text entry
    rather than offering a list of its own — orch-hub owns no model, effort, or
    agent policy. The catalog is advisory even when present: the orchestrator
    deliberately still launches an uncatalogued model, so a caller must keep that
    reachable too.
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
    options: dict | None = None
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
            "options": self.options,
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
    ``dev_agent``/``review_agent`` apply only to cc-codex — neither is ever sent
    to Cursor, which runs both roles on the one SDK. ``review_agent`` belongs to
    the split shape alone; the legacy run-wide ``mode`` predates the flag and
    refuses it rather than dropping it silently. The orthogonal launch flags
    ``plan_gate``/``once``/``max_sessions`` keep their existing per-flag UI
    semantics and are honored in every mode.
    """

    mode: str | None = None
    dev_mode: str | None = None
    review_mode: str | None = None
    backend: str | None = None
    dev_agent: str | None = None
    dev_model: str | None = None
    dev_effort: str | None = None
    review_agent: str | None = None
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
    review requested changes (the taskfile remediation predicate), and
    ``advancement_entry_count`` is the complementary partition — the work entries
    that advanced planned scope rather than converging on review findings, so
    ``remediation_count + advancement_entry_count == work_entry_count``;
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
    advancement_entry_count: int
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
            "advancement_entry_count": self.advancement_entry_count,
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
class SessionReadRollup:
    """One session's prefetch-reading facts, as the static report keeps them.

    The deterministic diagnosis behind the task-level
    ``prefetch_set_read_completeness`` score: a 0.5 aggregate is not actionable
    without knowing *which* session missed *which* declared doc, and the raw
    per-session rollup lives in the evidence pack, which a reader of the report
    alone does not have. Distilled from
    :class:`orch_hub.session_reads.SessionReads` — the trace and its timing
    anchors stay in the pack (they are the semantic layer's input, not a
    deterministic one).

    ``applicable`` is ``True`` only for a session the prefetch instruction
    reaches that declared a non-empty lazy set, ``False`` when there was nothing
    to comply with, and ``None`` when the session-time declaration could not be
    recovered — so applicability itself is unknown. ``assessable`` is whether the
    reading could be measured at all. ``coverage``/``complete`` are ``None``
    unless both hold; there is no default-to-zero, because a zero is a claim
    about the session rather than about the evidence. ``detail`` is why, when
    either is unclaimed.
    """

    session_id: str
    role: str | None
    kind: str | None
    applicable: bool | None
    assessable: bool
    expected_count: int
    read_count: int
    missing: tuple[str, ...]
    coverage: float | None
    complete: bool | None
    undeclared_read_count: int
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "role": self.role,
            "kind": self.kind,
            "applicable": self.applicable,
            "assessable": self.assessable,
            "expected_count": self.expected_count,
            "read_count": self.read_count,
            "missing": list(self.missing),
            "coverage": self.coverage,
            "complete": self.complete,
            "undeclared_read_count": self.undeclared_read_count,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReadMetrics:
    """Task-level rollup of how the task's sessions read the declared memory.

    The deterministic half of the read side (the semantic half judges the
    ordered trace). ``available`` mirrors
    :class:`orch_hub.session_reads.ReadBehavior`: whether the pack carries read
    evidence at all, not whether a read was attempted — a pack built where no
    transcript store exists and one built for sessions that left none say the
    same thing.

    The three counts are deliberately separate denominators:
    ``applicable_count`` is the sessions the measurement is *about*,
    ``unknown_applicable_count`` those whose applicability could not be
    established, and ``scored_count`` the applicable ones actually measurable.
    ``complete_count`` is the numerator. That split is what keeps missing
    evidence out of the quality score: it lowers the score's *confidence*
    (``scored_count`` over the candidates) instead of counting as a session that
    read nothing.

    ``dropped_candidate_count`` is the fourth, and the one with no rollup behind
    it: sessions the reader's own cap
    (:data:`orch_hub.session_reads.MAX_SESSIONS`) never looked at, whose role says
    the prefetch instruction reached them. They belong in the confidence
    denominator for the same reason an unmeasurable session does — they are
    candidates nobody measured — and out of the quality one for the same reason
    too. Counting only what ``sessions`` carries would let a cap report full
    evidence coverage for a task it read three quarters of.
    """

    available: bool
    session_count: int
    applicable_count: int
    unknown_applicable_count: int
    scored_count: int
    complete_count: int
    dropped_candidate_count: int = 0
    sessions: tuple[SessionReadRollup, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "session_count": self.session_count,
            "applicable_count": self.applicable_count,
            "unknown_applicable_count": self.unknown_applicable_count,
            "scored_count": self.scored_count,
            "complete_count": self.complete_count,
            "dropped_candidate_count": self.dropped_candidate_count,
            "sessions": [s.to_dict() for s in self.sessions],
            "warnings": list(self.warnings),
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
    self-describing without re-reading the pack. ``reads`` is the memory-read
    rollup (:class:`ReadMetrics`) the ``prefetch_set_read_completeness`` axis is
    computed from, and ``schema_version`` is the report-shape version that says
    which axes a report could carry — an older report predates an axis rather
    than scoring it badly, so an aggregate across the boundary must not mix them.
    No semantic judgment, no model call — every field is a pure function of the
    evidence pack.
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
    reads: ReadMetrics | None = None
    schema_version: str | None = None

    def to_dict(self) -> dict:
        return {
            "evaluation_id": self.evaluation_id,
            "schema_version": self.schema_version,
            "generated_ts": self.generated_ts,
            "repo_name": self.repo_name,
            "task_id": self.task_id,
            "target_present": self.target_present,
            "target_source": self.target_source,
            "scores": [score.to_dict() for score in self.scores],
            "warnings": list(self.warnings),
            "runs": self.runs.to_dict(),
            "protocol": self.protocol.to_dict(),
            "reads": self.reads.to_dict() if self.reads is not None else None,
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
    identity), and ``input_evidence_id``/``input_static_metrics``/
    ``input_evidence_schema`` link the report to the evidence and static-metrics
    inputs actually used.

    ``input_evidence_schema`` is the evidence pack's serialized shape version
    (:data:`orch_hub.evaluation.EVIDENCE_SCHEMA_VERSION`) and exists for
    *cohorting*: when the pack gains an input the evaluator reasons over, the
    confidence baseline moves, so reports either side of that bump are not
    directly comparable on confidence. Recording it on the report makes the
    boundary machine-visible without diffing two evidence packs. ``None`` on a
    report written before the field existed.

    ``input_entry_fidelity`` is the other provenance fact a downstream consumer
    needs before reusing this report's inputs: whether the pack carried each
    session's task file *as that session entered it* (``exact``), some of them
    (``partial``), or none (``reconstructed`` — see
    :data:`orch_hub.evaluation.FIDELITY_RECONSTRUCTED` for why that is not a
    neutral approximation). ``None`` on a report written before the field
    existed, which is not the same claim as ``reconstructed``.

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
    input_evidence_schema: str | None = None
    input_entry_fidelity: str | None = None
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
            "input_evidence_schema": self.input_evidence_schema,
            "input_entry_fidelity": self.input_entry_fidelity,
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
class ReportArtifact:
    """One artifact file of a cataloged report, with its content hash.

    Part of a :class:`ReportCatalogEntry` (:mod:`orch_hub.report_catalog`). The
    hash is what makes an imported report verifiable and de-duplicable by an
    external consumer: two catalog entries with the same ``sha256`` for
    ``report.md`` carry the same report body regardless of which repo/task/
    evaluation id produced them. ``size`` is the byte length at catalog time.
    Recorded once, at catalog time, and never rewritten — so a later
    retention prune of the artifacts leaves the hash as the durable record of
    what was published.
    """

    name: str
    size: int
    sha256: str

    def to_dict(self) -> dict:
        return {"name": self.name, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class ReportProvenance:
    """Cohorting provenance for one cataloged report, from durable artifacts only.

    Part of a :class:`ReportCatalogEntry`. A consumer importing many reports
    needs to group them by what produced them — which runs, which repo state,
    which protocol/orchestrator configuration. Every field here is read from the
    evaluation's own persisted ``evidence.json``, never re-derived from live
    repo/run state, so the entry stays reproducible from the artifacts it names.

    The ``*_available`` flags are the load-bearing part: provenance the evidence
    pack does not carry is reported as explicitly **unavailable with a reason**,
    never as an empty value a consumer could mistake for "no runs" or "default
    config". ``orchestrator_config`` is always unavailable — evidence packs carry
    no run ``effective-config`` snapshot — and its ``detail`` says so.
    ``git_newest_sha`` is the newest commit in the evidence git window (git log
    is reverse-chronological), i.e. roughly the repo state the task ended at, not
    necessarily current HEAD.

    ``protocol_revision`` and ``protocol_payload_sha256`` are the deployed
    protocol identity every contributing run verifiably ran under
    (:class:`orch_hub.protocol_provenance.EffectiveProtocol`, taken from the
    pack's durable per-run captures): the mandrel commit that produced the
    payload, and the deploy lock's digest of the payload itself. They
    are what a consumer cohorts reports by when asking whether a protocol change
    helped, which is why they are unanimity-gated rather than best-effort — and
    why they are carried as one fact: a revision published without the digest its
    runs verified names a source tree the report cannot show the sessions read.
    A task whose runs span a redeployment, whose runs verified different payloads
    under one revision, or whose provenance the hub never verified publishes the
    reason and neither field. Packs written before the capture existed carry no
    such section at all, and packs from before the identity became a pair carry
    only the revision; both are unavailable for their own stated reason — never
    completed or backfilled from the repo's current deployment, which by
    publication time is usually a later one.
    """

    runs_available: bool = False
    run_ids: tuple[str, ...] = ()
    runs_detail: str | None = None
    git_available: bool = False
    git_since: str | None = None
    git_until: str | None = None
    git_commit_count: int = 0
    git_newest_sha: str | None = None
    git_detail: str | None = None
    protocol_available: bool = False
    protocol_revision: str | None = None
    protocol_payload_sha256: str | None = None
    protocol_detail: str | None = None
    config_available: bool = False
    config_detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "runs": {
                "available": self.runs_available,
                "run_ids": list(self.run_ids),
                "count": len(self.run_ids),
                "detail": self.runs_detail,
            },
            "git": {
                "available": self.git_available,
                "since": self.git_since,
                "until": self.git_until,
                "commit_count": self.git_commit_count,
                "newest_sha": self.git_newest_sha,
                "detail": self.git_detail,
            },
            "protocol": {
                "available": self.protocol_available,
                "effective_revision": self.protocol_revision,
                "deploy_lock_hash": self.protocol_payload_sha256,
                "detail": self.protocol_detail,
            },
            "orchestrator_config": {
                "available": self.config_available,
                "detail": self.config_detail,
            },
        }


@dataclass(frozen=True)
class ReportCatalogEntry:
    """One completed L1+L2 evaluation report, published in the global catalog.

    The line format of the append-only catalog (:mod:`orch_hub.report_catalog`)
    and the item an external protocol-evolution consumer imports. It exists so a
    consumer can discover reports across *every* managed repo through one ordered
    feed instead of walking each repo's tasks and each task's evaluation dirs.

    Two identities, deliberately distinct:

    - **source identity** — ``repo_slug``/``task_id``/``evaluation_id``, the
      hub-owned path that locates the artifacts, plus display ``repo_name``/
      ``repo_path``. Unchanged from the existing per-task evaluation surface, so
      task-level routes stay compatible.
    - **global identity** — ``seq``, the catalog's monotonic append counter, and
      ``report_key``, a stable opaque digest of the source identity. The key is
      what a consumer stores and re-fetches by: it carries no ordering
      information, so a consumer never compares scoped ``evaluation_id``
      timestamps (which collide across repos) to reason about order. ``seq``
      carries the order, and is the cursor. Both are unique across the catalog,
      and that uniqueness *is* the exactly-once traversal guarantee — a file
      holding a repeat is served by no read path at all
      (:mod:`orch_hub.report_catalog`).

    ``target_source`` and ``archived`` are deliberately different facts about
    different moments: the first is where the task file was when the *evidence*
    was built (``active``/``archived``), the second is that the task was archived
    by the time the report was *published*. A report of live work is evaluated
    ``active`` and publishes later, once close-out archives its task, so the two
    disagree for exactly the reports whose protocol story finished after they were
    written. Only a report with ``archived`` set is ever cataloged, so this is a
    provenance pair rather than a filter.

    ``generated_ts`` and the evaluator triple come from the semantic report, so
    reports are cohortable by evaluator model/schema. Entries are immutable once
    appended: an entry whose artifacts a later retention prune removed stays in
    the catalog as an auditable record of a report that once existed.
    """

    seq: int
    report_key: str
    cataloged_ts: str | None
    repo_name: str
    repo_slug: str
    repo_path: Path | None
    task_id: str
    evaluation_id: str
    generated_ts: str | None
    target_source: str | None
    archived: bool
    backend: str | None = None
    model: str | None = None
    schema_version: str | None = None
    artifacts: tuple[ReportArtifact, ...] = ()
    provenance: ReportProvenance = field(default_factory=ReportProvenance)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "report_key": self.report_key,
            "cataloged_ts": self.cataloged_ts,
            "repo_name": self.repo_name,
            "repo_slug": self.repo_slug,
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "task_id": self.task_id,
            "evaluation_id": self.evaluation_id,
            "generated_ts": self.generated_ts,
            "target_source": self.target_source,
            "archived": self.archived,
            "evaluator": {
                "backend": self.backend,
                "model": self.model,
                "schema_version": self.schema_version,
            },
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class ReportPage:
    """One cursor-anchored page of the global report feed.

    The traversal unit an external protocol-evolution consumer polls: entries in
    ascending ``seq`` (:class:`ReportCatalogEntry`) plus exactly what it needs to
    ask for the next one. Paging here is *forward-only over an append-only file*,
    which is why it is a plain integer watermark rather than the opaque
    bidirectional anchor run history uses (:class:`RunHistoryPage`): a published
    entry is immutable and never renumbered, so "everything after ``seq`` N" names
    the same suffix forever and no insertion can shift a window under the pager.

    ``cursor`` echoes the watermark this page resumed from and ``next_cursor`` is
    what to send as the next request's cursor — the last returned entry's ``seq``,
    or ``cursor`` unchanged for an empty page, so a consumer stores one number
    unconditionally and never has to special-case "nothing new yet". ``has_more``
    says whether the catalog already holds more beyond this page (keep polling
    immediately) as opposed to being caught up (poll later); a consumer must read
    it rather than infer the end from a short page, since a page can be short and
    still have successors appended a moment later.
    """

    entries: tuple[ReportCatalogEntry, ...] = ()
    cursor: int = 0
    next_cursor: int = 0
    has_more: bool = False

    def to_dict(self) -> dict:
        return {
            "reports": [entry.to_dict() for entry in self.entries],
            "cursor": self.cursor,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
        }


@dataclass(frozen=True)
class ReportBundle:
    """One published report's catalog entry plus its artifact contents.

    The import unit fetched by global ``report_key``, so a consumer never needs to
    know the ``<repo-slug>/<task-id>/<evaluation-id>/`` layout the artifacts
    actually live under. ``entry`` is the immutable feed item; the four artifact
    fields are the same shapes the per-task evaluation detail route returns
    (parsed JSON, plus ``report_md`` as raw text), so a client that already reads
    that route parses these identically.

    ``available`` is the load-bearing flag and is all-or-nothing, matching what
    publication required: a bundle is served only when the *whole* L1+L2 set is
    still on disk and readable. A published entry whose artifacts a later
    retention prune removed is therefore ``available: false`` with a ``detail``
    saying so — the entry stays auditable (that is the point of an append-only
    catalog outliving its artifacts) while the fetch fails closed rather than
    handing back a partial import. The same applies to an entry whose stored
    identity cannot address a contained directory, or cannot be trusted to name
    the artifacts it was published from, and to a *damaged* set — a link standing
    where a published artifact should be, refused rather than followed. That last
    ``detail`` is deliberately distinguishable from the pruned ones: retention
    removing artifacts is the feed's designed end state, so reporting a tampered
    tree as that end state would hide it from the operator who can repair it.

    The three JSON artifacts carry whatever their file parses to — object, array,
    or scalar — exactly like the per-task detail route, because the artifact bytes
    are what a consumer imports and what the recorded hash covers. Inside an
    ``available`` bundle a ``null`` artifact therefore means the file's content is
    JSON ``null``, never that the file is missing: availability already guarantees
    all four are present.

    ``mismatched`` names artifacts whose bytes no longer hash to the ``sha256``
    the entry recorded at publication, and ``unverified`` names artifacts the entry
    records no usable digest for (a manifest an older/leaner writer never wrote, or
    one a hand-edit left malformed or recorded more than once). Neither is an
    availability failure — the content is still served — because the consumer, not
    the hub, decides what to do about a report edited or unverifiable after the
    fact; the hub's job is to make the discrepancy impossible to miss, which is
    what recording the hashes was for. ``verified`` is the strict summary: every
    served artifact was compared against a recorded digest *and* matched.
    """

    entry: ReportCatalogEntry
    available: bool
    detail: str | None = None
    evidence: object | None = None
    static_metrics: object | None = None
    semantic_report: object | None = None
    report_md: str | None = None
    mismatched: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        """Whether every served artifact was checked against a digest and matched.

        Deliberately not "nothing mismatched": an artifact with no usable recorded
        digest was never compared to anything, so counting it as verified would
        let a manifest a hand-edit stripped read as a hash-checked import.
        """
        return self.available and not self.mismatched and not self.unverified

    def to_dict(self) -> dict:
        return {
            "report_key": self.entry.report_key,
            "entry": self.entry.to_dict(),
            "available": self.available,
            "detail": self.detail,
            "artifacts": {
                "evidence": self.evidence,
                "static_metrics": self.static_metrics,
                "semantic_report": self.semantic_report,
                "report_md": self.report_md,
            },
            "integrity": {
                "verified": self.verified,
                "mismatched": list(self.mismatched),
                "unverified": list(self.unverified),
            },
        }


@dataclass(frozen=True)
class ReportArtifactRead:
    """One published report artifact's persisted bytes, or why they are not served.

    The unit behind the raw-artifact route, and deliberately *not* a
    :class:`ReportBundle` with one field filled in. A bundle is the import unit:
    all-or-nothing, parsed, re-serialized by the JSON response — which is exactly
    what a consumer cannot verify a digest against, since it never sees the bytes
    the hash was taken over. This carries those bytes, so ``sha256(data)`` and
    ``len(data)`` are comparable to the ``sha256``/``size`` the feed entry recorded
    at publication, and it is addressed per artifact rather than per set: an
    artifact still on disk beside a pruned sibling is served, because a byte-level
    check of *this* file does not need the other three.

    Exactly one of ``data`` and ``reason`` is set. ``reason`` is a stable machine
    code (:mod:`orch_hub.report_catalog`) rather than prose, because the HTTP
    adapter maps it to a status — a raw-byte response has no envelope to put
    ``available: false`` in — and ``detail`` is the human sentence beside it.

    ``integrity`` mirrors the bundle's vocabulary for this one artifact:
    ``verified`` (compared against a recorded digest and matched), ``mismatched``
    (compared and differs — still served, because only the consumer can decide what
    an edited report means for its import), or ``unverified`` (the entry records no
    usable digest for it, so nothing was compared).
    """

    entry: ReportCatalogEntry
    name: str
    data: bytes | None = None
    reason: str | None = None
    detail: str | None = None
    integrity: str = "unverified"
    recorded_sha256: str | None = None

    @property
    def served(self) -> bool:
        return self.data is not None


@dataclass(frozen=True)
class CatalogDefect:
    """One thing wrong with the global report catalog, located precisely enough to fix.

    The unit an operator verify run reports (:mod:`orch_hub.report_catalog`). The
    catalog is append-only and never rewritten, so the write path cannot produce
    any of these: a defect means the file was hand-edited, merged, restored over a
    live one, or left with a torn tail by a crashed append — or that the artifact
    tree an entry names has been damaged since. Naming *where* is the whole point,
    since the operator's alternative is reading a ``500`` and guessing: ``line`` is
    the 1-based line of ``catalog.jsonl``, and ``seq``/``report_key`` are the
    offending identities when the defect is about an entry rather than about bytes
    that would not parse.

    ``path`` is the other half of "located precisely enough to fix", for the
    findings whose repair is not in the catalog file at all: a damaged, incomplete,
    or unreadable artifact set is cleared in the evaluations tree, and an operator
    told only which report is affected would still have to reconstruct
    ``<data_dir>/evaluations/<repo-slug>/<task-id>/<evaluation-id>/`` by hand. It is
    unset for findings the catalog line itself carries, where ``line`` is the
    address.

    ``blocks_feed`` separates the two severities that matter, and separates them by
    *consequence* rather than by tidiness. A repeated ``seq`` or ``report_key``
    makes the cursor ambiguous, so every read path fails closed and the feed serves
    nothing until it is repaired; a skipped unparseable line, an entry whose
    artifacts retention pruned or a link damaged, and an entry whose stored identity
    cannot address its artifacts all leave the traversal intact and answer for
    themselves at fetch time. Only the first kind is what a verify run's non-zero
    exit means.
    """

    kind: str
    detail: str
    line: int | None = None
    seq: int | None = None
    report_key: str | None = None
    path: Path | None = None
    blocks_feed: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "line": self.line,
            "seq": self.seq,
            "report_key": self.report_key,
            "path": str(self.path) if self.path else None,
            "blocks_feed": self.blocks_feed,
        }


@dataclass(frozen=True)
class CatalogAudit:
    """The global report catalog's integrity, as one read-only diagnostic.

    What ``orch-hub eval catalog verify`` prints (:mod:`orch_hub.report_catalog`).
    It answers two questions an operator otherwise has to infer from a ``500``:
    *can the feed be traversed at all* (``serves_feed``, the fail-closed condition
    every read path shares), and *what exactly is wrong and where* (``defects``).

    The artifact census counts entries, not lines, and keeps states apart that a
    coarser count would fold together. ``pruned`` is the designed end state of an
    entry outliving the artifacts it indexes — retention removes the evaluation
    *directory*, so it is that directory's absence and **not** a defect;
    ``damaged`` is a symbolic link standing where the hub wrote a regular artifact
    or a tree directory, which the hub never writes, cannot reclaim (a prune
    refuses the linked hop and keeps the id), and only an operator can clear.
    Reporting the second as the first would hide the one state that accumulates
    until someone acts. ``incomplete`` is a directory that is still there without
    the whole published set (however few of it survive), ``unreadable`` an IO
    fault in a file that is still standing, and ``unaddressable`` a stored identity
    that cannot be trusted to name its own artifacts.

    Every count is the answer a fetch of that entry gives, because a census an
    operator reads as "these reports are fine" has to be one a consumer can act
    on: the states come from the classification :meth:`read_bundle` performs, over
    the same descriptor and the same per-artifact opens.
    """

    catalog_path: Path
    present: bool = False
    lines: int = 0
    entries: int = 0
    serves_feed: bool = True
    retained: int = 0
    pruned: int = 0
    incomplete: int = 0
    damaged: int = 0
    unreadable: int = 0
    unaddressable: int = 0
    defects: tuple[CatalogDefect, ...] = ()

    def to_dict(self) -> dict:
        return {
            "catalog_path": str(self.catalog_path),
            "present": self.present,
            "lines": self.lines,
            "entries": self.entries,
            "serves_feed": self.serves_feed,
            "artifacts": {
                "retained": self.retained,
                "pruned": self.pruned,
                "incomplete": self.incomplete,
                "damaged": self.damaged,
                "unreadable": self.unreadable,
                "unaddressable": self.unaddressable,
            },
            "defects": [defect.to_dict() for defect in self.defects],
        }


@dataclass(frozen=True)
class CatalogRepairAction:
    """One line the repair would drop, and why dropping it changes nothing.

    Every action is identity-preserving by construction
    (:meth:`orch_hub.report_catalog.ReportCatalog.repair`): the line is either a
    byte-identical repeat of an earlier one, so the surviving copy carries the same
    published ``seq`` and ``report_key``, or a trailing partial line no reader has
    ever parsed. Nothing here renumbers, rewrites, or removes a distinct entry —
    those are the operator's decision, and the repair refuses rather than making it.
    """

    kind: str
    line: int
    detail: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "line": self.line, "detail": self.detail}


@dataclass(frozen=True)
class CatalogRepair:
    """The outcome of a repair attempt on the append-only catalog: plan, or refusal.

    Three states, and they are exclusive. A **plan** (``actions`` non-empty,
    ``refused`` unset) is the identity-preserving drop set, applied only when the
    operator confirmed it (``applied``) and reported as a dry run otherwise — the
    file is durable publication state, so the default is to describe rather than
    rewrite. A **refusal** (``refused`` set, ``actions`` empty) is a conflict no
    rule can resolve safely: two distinct entries sharing a ``seq`` or a
    ``report_key``, where dropping or renumbering either moves an identity a
    consumer may already have stored. Nothing at all is written in that case, not
    even the safe part, so an operator's ``verify`` line numbers stay valid while
    they resolve it by hand. **Nothing to do** is both empty.

    ``serves_feed`` describes the catalog *as it now stands on disk* — after the
    rewrite when one happened, and unchanged otherwise — so a caller reads one
    field to decide whether the feed is working rather than replaying the plan.
    ``backup_path`` is the pre-repair copy written beside the catalog before the
    replacement, present only on an applied repair.
    """

    catalog_path: Path
    actions: tuple[CatalogRepairAction, ...] = ()
    applied: bool = False
    refused: str | None = None
    backup_path: Path | None = None
    serves_feed: bool = True

    def to_dict(self) -> dict:
        return {
            "catalog_path": str(self.catalog_path),
            "actions": [action.to_dict() for action in self.actions],
            "applied": self.applied,
            "refused": self.refused,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "serves_feed": self.serves_feed,
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
    frontmatter status, run overlay states, and intake status: ``deferred``
    (accepted, held out of the model turn by the automatic dispatch policy) is a
    *waiting* state that no restart sweep may fail; ``queued`` (admitted, turn
    about to start) and ``running`` (the model turn is in flight) are transient;
    ``completed``, ``failed``, and ``skipped`` are terminal. ``defer_reason``
    is the bounded machine reason a deferred job is still waiting (never operator
    prose), and ``auto`` records whether this attempt was produced mechanically by
    the retired completion trigger or asked for by an operator. Every record a
    current hub writes carries ``auto=False``; the field remains because persisted
    records from the automatic era do not. ``skip_reason`` is likewise
    legacy-only: the bounded machine token on a ``skipped`` record, written when a
    task completed under a run launched with the automatic Layer 2 opt-out
    (``RunRecord.auto_semantic_eval``). Its vocabulary is the single token
    :data:`orch_hub.semantic_jobs.SKIP_RUN_OPT_OUT`, never operator prose — free
    text would be unanalyzable and would leak repo content into a persisted
    surface. All three reason fields are mutually exclusive per record and answer
    different questions: why a *waiting* job waits, why an *attempt* failed, and
    why no attempt was made. ``backend``/``model``/
    ``schema_version`` are the selected evaluator identity (the
    :class:`SemanticEvaluatorInfo` triple), stamped at creation so the
    queued/running UI shows which model is running before any report exists.
    ``evaluation_id`` is set only on ``completed`` and links the job to the
    freshly written evaluation dir the dashboard then selects; ``error`` is the
    bounded failure diagnostic on ``failed`` (never full prompt text or unbounded
    model output). ``started_ts`` marks the queued→running transition and
    ``ended_ts`` the terminal transition (both ``None`` until they happen).

    ``deadline_ts`` is this attempt's whole-attempt bound: the instant after which
    an in-flight record may be reaped as stuck
    (:meth:`orch_hub.semantic_jobs.SemanticJobManager.reconcile_stuck`). It is
    written once, by the queued→running flip that also stamps ``started_ts``, and
    it is persisted rather than recomputed because the worker is held to it too —
    the worker must finish its pre-model work early enough that the model turn's
    own deadline expires *before* this instant, which is what makes reaping safe:
    a record past ``deadline_ts`` cannot be a turn still inside its model
    deadline. ``None`` on a record that has not started a turn (``deferred``,
    ``queued``, ``skipped``), and on every record when the turn deadline is
    disabled (``ORCH_HUB_EVAL_TURN_TIMEOUT=0``), which removes the bound entirely.
    It survives onto the terminal record as evidence of the bound the attempt ran
    under.
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
    defer_reason: str | None = None
    skip_reason: str | None = None
    auto: bool = False
    created_ts: str | None = None
    updated_ts: str | None = None
    started_ts: str | None = None
    deadline_ts: str | None = None
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
            "defer_reason": self.defer_reason,
            "skip_reason": self.skip_reason,
            "auto": self.auto,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "started_ts": self.started_ts,
            "deadline_ts": self.deadline_ts,
            "ended_ts": self.ended_ts,
        }


@dataclass(frozen=True)
class EvaluationControl:
    """Whether automatic Layer 2 dispatch is currently held by an operator pause.

    The persisted shape of :class:`orch_hub.eval_control.EvaluationControlStore`,
    and one of the two config-like facts a queue reader cannot infer from the job
    records (the other being ``auto_enabled``). It is deliberately the same kind of
    answer: a pause must never be a *second silent condition* on a queue whose
    failure modes are already silent by construction — so it travels in the queue
    payload and explains itself rather than leaving the reader with a still queue
    and no reason.

    ``paused_ts`` is when the drain actually stopped, not when the button was last
    pressed: pausing an already-paused queue keeps the original stamp, so "held
    since 09:14" stays true across repeated presses and a second browser session.
    It is ``None`` on a pause that no operator made (see below) and on a running
    queue.

    ``reason`` is a bounded machine token from a closed vocabulary
    (:data:`orch_hub.eval_control.PAUSE_OPERATOR`,
    :data:`~orch_hub.eval_control.PAUSE_UNREADABLE`), never operator prose — the
    same rule the job record's ``defer_reason``/``skip_reason`` follow, for the
    same reasons: prose is unanalyzable, leaks repo content into a persisted
    surface, and invites recording a rationalization. It is ``None`` exactly when
    ``paused`` is false. The two tokens are kept apart because they call for
    opposite responses: an operator pause ends when the operator ends it, while an
    unreadable control record is damage that will not fix itself and must not be
    reported as somebody's decision.
    """

    paused: bool = False
    paused_ts: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "paused": self.paused,
            "paused_ts": self.paused_ts,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EvaluationQueueEntry:
    """One semantic job as the operator's queue view shows it.

    A :class:`SemanticJob` plus the two facts a queue reader needs and the record
    itself cannot state: how long it has been waiting, and whether that wait has
    passed the configured bound (``ORCH_HUB_EVAL_QUEUE_MAX_AGE``). Both are
    derived at read time against a clock, never persisted — the record is the
    truth and this is a projection of it, so a config change re-renders rather
    than migrating anything.

    ``age_seconds`` is measured from ``created_ts`` (when the completion was
    accepted, not when a turn started) because that is the interval the corpus
    cares about: how long a completed task has gone unmeasured. It is ``None``
    for a record whose stamp will not parse, which reads as "cannot say" rather
    than as zero.

    ``overdue`` is deliberately narrow: it flags a **deferred** job past the
    bound and nothing else. Only legacy records reach that status now — nothing
    accepts new ones — so the flag reads as "this has been resting since the
    automatic path was retired". A ``queued``/``running`` job that has hung is a
    different failure
    with its own recovery — its ``deadline_ts`` settles it, at startup or on the
    resident sweep — and giving one threshold two meanings is how a threshold
    stops meaning anything.
    """

    job: SemanticJob
    age_seconds: int | None = None
    overdue: bool = False

    def to_dict(self) -> dict:
        return {
            "job": self.job.to_dict(),
            "age_seconds": self.age_seconds,
            "overdue": self.overdue,
        }


@dataclass(frozen=True)
class EvaluationQueueSnapshot:
    """The cross-repo state of automatic Layer 2 evaluation, for one read.

    Automatic evaluation exists to remove production-side selection from the
    evaluated corpus, which makes its own failure modes quiet by construction: a
    queue that stops draining, a job that fails, or a switch left off all look
    identical from outside — nothing happens, and the corpus simply stops
    growing. Nobody notices an absence. This is the shape that makes the absence
    countable.

    ``counts`` is a **complete census** by job status over every persisted
    record, never capped, so a truncated ``entries`` page can never make the
    queue look shorter than it is. Every known status is present (zero-valued
    when unseen) so a reader can render a stable set of figures, and an
    unrecognized status from a hand-edited record is counted under its own key
    rather than silently dropped.

    What it counts is **records** — one per ``(repo, task)``, each holding that
    task's *latest* attempt — and deliberately not "how many tasks carry a
    report". The two diverge by design:
    :meth:`~orch_hub.semantic_jobs.SemanticJobManager.start_job` overwrites a
    task's terminal record for a manual re-run, while the completed evaluation
    that record pointed at stays in the task's retained evaluation history. So a
    successful report followed by a failed retry counts here as ``failed``, and
    correctly — this is the state of the queue, not the size of the corpus. It
    would understate a corpus in the other direction too: a task completed before
    automatic evaluation existed has no record here at all. Report existence is
    answered by the evaluation tree and the publication feed
    (:mod:`orch_hub.report_catalog`), neither of which an attempt overwrites; a
    reader of this shape must label its figures as the attempt lifecycle they
    are.

    ``entries`` carries the jobs an operator might act on — everything except
    ``completed`` and ``skipped``, the two resting outcomes the counts already
    report (one healthy, one deliberate) — ordered the way the queue itself is: waiting jobs oldest-first (the
    dispatcher's own promotion order), then settled failures newest-first. It is
    capped at :data:`orch_hub.eval_dispatch.QUEUE_ENTRY_LIMIT`, with ``truncated``
    saying so, because a surface that silently shows the first N reads as "this
    is everything".

    ``skipped_by_repo`` is the one figure the flat census cannot carry: a
    per-repo tally of records resting at ``skipped``, i.e. completed tasks whose
    run was launched with the Layer 2 opt-out. It is per repo because that is
    where a shrinking corpus shows up as a pattern rather than as scattered rows —
    skips cluster on the expensive, remediation-heavy tail — and it is a *count*
    because individual rows are the least likely thing anyone scrolls to. Skipped
    records are deliberately absent from ``entries``: they are a resting,
    intentional outcome with nothing to act on, and one row per opted-out task
    would crowd out the jobs that are genuinely stuck. Like every other figure
    here it is over *records*, one per ``(repo, task)`` holding that task's latest
    attempt — so it is the count of tasks currently resting at skipped, never a
    share of that repo's tasks and never a lifetime total (a manual evaluation
    overwrites a skipped record like any other terminal one).

    ``auto_enabled``, ``control``, and ``queue_max_age_seconds`` are the pieces of
    state a queue reader cannot infer from records. The first says whether
    completed runs are generating Layer 1 unasked; the second is the persisted
    operator pause (:class:`EvaluationControl`), which held the retired automatic
    drain and is reported rather than stamped on records; and the last is the bound
    ``overdue`` was measured against, so the reader renders the real threshold
    instead of a literal that drifts from it. Both are reported independently:
    telling an operator only one of
    those sends them to fix the wrong switch.
    """

    auto_enabled: bool = False
    control: EvaluationControl = field(default_factory=EvaluationControl)
    queue_max_age_seconds: int = 0
    counts: Mapping[str, int] = field(default_factory=dict)
    skipped_by_repo: Mapping[str, int] = field(default_factory=dict)
    overdue: int = 0
    entries: tuple[EvaluationQueueEntry, ...] = ()
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "auto_enabled": self.auto_enabled,
            "control": self.control.to_dict(),
            "queue_max_age_seconds": self.queue_max_age_seconds,
            "counts": dict(self.counts),
            "skipped_by_repo": dict(self.skipped_by_repo),
            "overdue": self.overdue,
            "entries": [entry.to_dict() for entry in self.entries],
            "truncated": self.truncated,
        }


# -- protocol identity coverage -----------------------------------------------
# Read-only census of the one fact this hub publishes that nothing else surfaces:
# whether a run — and the report of the task it worked — carries a verified
# deployed-protocol identity, and when it does not, why. Both populations already
# hold the answer per item (a run's ``protocol.json``, a catalog entry's
# ``provenance.protocol``); what nobody could ask before was how much of the
# corpus carries one. These shapes are the aggregate, and nothing more: the census
# starts no check, repairs no run, and publishes nothing.


@dataclass(frozen=True)
class RunProtocolCoverage:
    """How every persisted run buckets by protocol identity coverage.

    ``counts`` is a **complete census** over run records, never capped, keyed by
    :data:`orch_hub.protocol_provenance.RUN_COVERAGES` with every value present
    (zero-valued when unseen) so a reader renders a stable set of figures instead
    of inferring an absent key as zero. A value outside that vocabulary is counted
    under its own key rather than dropped, on the same rule as the semantic-job
    census — the count is real even when no surface has a label for it yet.

    ``total`` is the number of run records walked, so ``counts`` sums to it. The
    one figure that answers the coverage question is ``counts['verified']``, and
    it is deliberately not repeated as a field of its own: a headline that can
    disagree with the breakdown below it is a defect waiting to happen.

    What it counts is **runs**, one entry per persisted run dir — not tasks and
    not reports. A task publishes an identity only if *every* contributing run
    carries one (unanimity,
    :func:`orch_hub.protocol_provenance.effective_protocol`), so this population
    is strictly the more forgiving of the two and its verified share is an upper
    bound on the published one, never an estimate of it. The published side is
    :class:`ReportProtocolCoverage`.
    """

    total: int = 0
    counts: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"total": self.total, "counts": dict(self.counts)}


@dataclass(frozen=True)
class ReportProtocolCoverageEntry:
    """One published task that carries no protocol identity, and why.

    A row per *task*, stated through the one published report that decides it —
    that task's newest (:class:`ReportProtocolCoverage`). The identity is that
    report's own (``seq``/``report_key`` plus the source repo/task/evaluation), so
    the row is traceable to the exact feed entry the verdict was read off and
    opens the evaluation that was published, without a second lookup. A task
    republished several times therefore appears once, under its current answer,
    rather than once per rerun.

    ``detail`` is the reason the hub persisted at publication time — how many
    contributing runs carried no verified identity, or which revisions/payloads
    they disagreed about — read back verbatim, never re-derived from current run
    or repo state.
    """

    seq: int = 0
    report_key: str = ""
    repo_name: str = ""
    task_id: str = ""
    evaluation_id: str = ""
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "report_key": self.report_key,
            "repo_name": self.repo_name,
            "task_id": self.task_id,
            "evaluation_id": self.evaluation_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReportProtocolCoverage:
    """How much published work carries a protocol identity, and why the rest does not.

    Two populations over the same append-only catalog, because the corpus has two
    honest denominators and neither is the other:

    - ``tasks``/``tasks_with_identity`` — distinct published tasks
      (``(repo_slug, task_id)``), each answered by the **newest** report published
      for it. This is the census the coverage question means: a task is the unit
      of work that gets evaluated, and how many times the hub happened to re-run
      the evaluator over one says nothing about whether that work can be cohorted
      by protocol revision. Counting report versions instead lets one task with
      three semantic reruns weigh three times, and — since provenance is recomputed
      per evidence pack — even land on both sides of the same figure.
    - ``reports``/``reports_with_identity`` — every catalog entry, which is what a
      consumer pages through. Kept beside the task figures rather than in place of
      them, so the two surfaces reconcile: the feed shows report versions and this
      says how much of it is cohortable, while the task figures say how much of
      the *work* is.

    Which report speaks for a task is an explicit rule, not an accident of order:
    the newest by evaluation-generation
    (:func:`orch_hub.evaluation.evaluation_order_key`, the per-task view's own
    rule), tie-broken by publication ``seq``. Not "any version has one" — that
    would report a task as covered on the strength of a report superseded since —
    and not unanimity across versions, which would be a third rule nobody else
    holds and would let a rerun cost a task coverage it still publishes.

    The reasons are carried as ``entries`` rather than as a second ``counts``
    table, because unlike a run there is no per-report reason *vocabulary* to
    bucket by: a catalog entry records availability plus one operator-readable
    sentence, and that sentence embeds run counts, revisions, and digests.
    Bucketing it would mean parsing the hub's own prose back into classes it never
    published — so each uncovered task states its own reason instead, capped
    (``truncated`` says when) so one large corpus cannot return an unbounded page.
    Every count stays complete regardless of that cap.

    ``available`` is about the *census*, not about any report: false means the
    catalog could not be read at all — evaluation disabled, or a feed failing
    closed on a duplicate identity — and ``detail`` says which. It is a separate
    flag rather than a zero total, under the rule this whole subsystem is built
    on: an unavailable answer is said, never served as an empty one a reader could
    mistake for "nothing published".
    """

    available: bool = False
    detail: str | None = None
    tasks: int = 0
    tasks_with_identity: int = 0
    reports: int = 0
    reports_with_identity: int = 0
    entries: tuple[ReportProtocolCoverageEntry, ...] = ()
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "detail": self.detail,
            "tasks": self.tasks,
            "tasks_with_identity": self.tasks_with_identity,
            "reports": self.reports,
            "reports_with_identity": self.reports_with_identity,
            "entries": [entry.to_dict() for entry in self.entries],
            "truncated": self.truncated,
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
