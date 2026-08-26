"""Static (deterministic, non-semantic) task quality metrics from an evidence pack.

This module is the first layer *over* the read-only evidence pack
(:mod:`orch_hub.evaluation`): it computes stable, model-token-free metrics that
describe how a task's runs went, so every completed task can get a protocol and
operability signal without a semantic model call. It is part of the same
optional evaluation subsystem and shares its config gate
(``Config.evaluation_enabled``) and its
:class:`~orch_hub.evaluation.EvaluationDisabledError` fail-closed boundary.

It is strictly read-only over the pack — a pure computation, no filesystem or
run-state writes. The evidence pack already did the (isolated, best-effort)
reads; this layer only *derives* counts and rollups from what the pack carries.
The metric data shapes (:class:`~orch_hub.models.RunRollup`,
:class:`~orch_hub.models.RunMetrics`) live in :mod:`orch_hub.models` with the
other serializable domain shapes; the outcome *vocabulary* and the derivation
stay here, mirroring how ``STATE_*`` live in :mod:`orch_hub.runs` while
``RunStatus`` lives in models.

The foundation task deliberately left terminal-state classification to its
consumers ("the pack exposes raw evidence and lets static/semantic consumers
classify; it does not duplicate the run manager's verdict logic"). This module
is that consumer: it derives an **evidence-only** run outcome
(:data:`RUN_OUTCOME_LIVE` … :data:`RUN_OUTCOME_BROKEN`) from the raw persisted
signals a pack carries — ``ended_ts``/``exit_code``/``error`` plus the
``stop_requested`` history event — *not* from ``RunManager._classify_end``
(which also reads the run's control dir directly, a surface the pack does not
carry). The
outcome is coarser than the run manager's live verdict and is labelled so:
``clean`` (exit 0) does not distinguish task-completed from a graceful boundary
stop — that protocol-completion signal comes from the task file, read by the
protocol parser (:func:`_parse_protocol`) via the frontmatter status.

**Layers.** :meth:`StaticEvaluator.run_metrics` is the run/session/event
rollup (:class:`~orch_hub.models.RunMetrics`); :meth:`build_report` adds the
task-file protocol facts (:class:`~orch_hub.models.ProtocolMetrics`, parsed from
the pack's task content), the memory-read rollup
(:class:`~orch_hub.models.ReadMetrics`, distilled from the pack's per-session
read behavior) and the evidence-backed score axes
(:class:`~orch_hub.models.ScoreComponent`), composing a
:class:`~orch_hub.models.StaticMetricsReport`; :meth:`generate_metrics` builds a
fresh evidence pack (delegating to :class:`~orch_hub.evaluation.EvaluationService`)
and persists ``static_metrics.json`` beside its ``evidence.json``.

**Scoring is deliberately transparent, not opaque.** Each axis starts at ``1.0``
and subtracts documented per-issue penalties (the ``_P_*`` constants), flooring
at ``0.0``; every penalty appends a warning naming the evidence, and an axis with
no backing evidence reports ``score=None`` with ``confidence=0.0`` rather than a
misleading ``0.0``. :data:`SCORE_PREFETCH_READS` is the one axis shaped as a
fraction instead — the per-session fact it aggregates is already binary — and it
is also where the difference between *bad* and *unmeasured* is priced most
explicitly: unmeasurable sessions leave the quality denominator entirely and
lower the axis's confidence, because "no transcript" is not "read nothing".
Two knowledge-maintenance signals the scope names are
**not derivable from the evidence pack** and are intentionally omitted (not
guessed): which files a windowed commit touched (the pack carries commit
subjects only — diffs/file-lists are deliberately excluded), and whether a
blocker references an *archived* task (the pack carries only this task's file,
not the archive set or other tasks). The pack-determinable knowledge signals —
close-out/archive reconciliation, malformed prefetch, and repeated evidence
degradation — are scored; the blocked⇔blocker self-consistency rule is scored
under ``protocol``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .config import Config
from .evaluation import (
    SOURCE_ARCHIVED,
    EvaluationDisabledError,
    EvaluationService,
    EvidencePack,
    RunEvidence,
)
from .history import KIND_ANSWER_RECORDED, KIND_STOP_REQUESTED
from .logs import LogSnapshot
from .models import (
    EvaluationTarget,
    ManagedRepo,
    ProtocolMetrics,
    ReadMetrics,
    RunEvent,
    RunMetrics,
    RunRollup,
    ScoreComponent,
    SessionEst,
    SessionReadRollup,
    SessionSummary,
    StaticMetricsReport,
)

# -- session vocabulary -------------------------------------------------------
# The orchestrator log (parsed by :mod:`orch_hub.sessions`) brackets each
# protocol session with a ``{role} {kind}`` pair; these mirror that stable log
# vocabulary (kept as local copies so this module does not depend on the
# sessions parser's private regex literals).
DEV_ROLE = "dev"
REVIEW_ROLE = "review"
SESSION_KIND = "session"
PLAN_GATE_KIND = "plan-gate"

# -- evidence-only run outcome ------------------------------------------------
RUN_OUTCOME_LIVE = "live"
"""No parseable ``ended_ts``: the run had not been observed ended when the pack
was built (a still-live run, or one whose end this hub never observed)."""

RUN_OUTCOME_CLEAN = "clean"
"""Ended with exit code 0 and no stop request. *Not* a task-completion verdict:
exit 0 is ambiguous (task done vs a graceful boundary stop vs a ``--once``
session); the completion signal comes from the task file (session-2)."""

RUN_OUTCOME_FAILED = "failed"
"""Ended with a non-zero exit code and no stop request — an orchestrator run
failure."""

RUN_OUTCOME_STOPPED = "stopped"
"""Ended after a ``stop_requested`` history event (a graceful hub stop or an
abort), regardless of exit code — the human-requested stop takes precedence over
the bare exit code, matching how the run manager weights stop evidence."""

RUN_OUTCOME_ENDED_UNKNOWN = "ended_unknown"
"""Ended with no exit code and no stop request — the run ended while the hub was
down, so its exit status is unrecoverable."""

RUN_OUTCOME_BROKEN = "broken"
"""An unreadable ``run.json`` stub (``record.error`` set). Defensive: related-run
aggregation (``EvaluationService.related_runs``) drops error stubs because they
carry no repo attribution, so a pack from that path never contains one — but the
metric computation stays total over *any* :class:`EvidencePack`, so a hand-built
or future pack with a broken record still classifies rather than crashing."""

_FAILURE_OUTCOMES = (RUN_OUTCOME_FAILED, RUN_OUTCOME_BROKEN)
"""Outcomes that count as a run failure for the reliability/diagnostics signal:
an orchestrator non-zero exit or an unreadable record."""

# -- persisted report ---------------------------------------------------------
STATIC_METRICS_NAME = "static_metrics.json"
"""The persisted static-metrics report, written beside ``evidence.json`` in the
evaluation's artifact dir (one dir per evaluation)."""

STATIC_SCHEMA_VERSION = "4"
"""Version of the ``static_metrics.json`` shape, stamped into every report.

The counterpart of :data:`orch_hub.evaluation.EVIDENCE_SCHEMA_VERSION` for this
layer, and for the same reason: the *score set* is part of the shape, so a batch
of reports spanning a change to it cannot be aggregated as one population. ``1``
is the five-axis report (``protocol``, ``reliability``, ``handoff``,
``estimate``, ``knowledge``); ``2`` adds
:data:`SCORE_PREFETCH_READS` and the :class:`~orch_hub.models.ReadMetrics`
rollup it is computed from. A v1 report does not carry a *bad*
prefetch-reading score, it carries *no* score for an axis that did not exist —
which an aggregate that mixes the two would read as a difference in quality.
``3`` keeps that axis's set but corrects its confidence denominator: a session
past the reader's cap is a candidate nobody measured, so a v2 report over a
capped task claims evidence coverage it did not have. The score itself is
unchanged — only how much of the population it speaks for — and the report
carries no evidence-cohort key of its own, so the stamp is the only place that
difference can be recorded. A persisted report with no ``schema_version`` key is
version 1 and predates the stamp. Additive so far: consumers read named keys and
tolerate absent ones.

``4`` keeps the v3 score set but changes what two of its axes *measure*: a
dev entry's ``pending -> final_review`` edge is legal (so a task finished in its
first session no longer carries an illegal-transition penalty), and the estimate
axis counts dev advancement sessions rather than all work sessions (so
review-driven remediation no longer reads as an overrun). Both corrections raise
scores for well-run tasks, which is exactly why the cohort must be separable: a
population mixing v3 and v4 would read the fix as tasks having improved."""

# -- score axes ---------------------------------------------------------------
SCORE_PROTOCOL = "protocol"
SCORE_RELIABILITY = "reliability"
SCORE_HANDOFF = "handoff"
SCORE_ESTIMATE = "estimate"
SCORE_KNOWLEDGE = "knowledge"
SCORE_PREFETCH_READS = "prefetch_set_read_completeness"
"""Whether the sessions that declared lazy memory docs actually read them — the
deterministic half of the read side, over
:class:`orch_hub.session_reads.ReadBehavior`. Deliberately *not* a count of
reads: more reading is not better, and the axis asks only whether what a session
itself declared it needed reached its context."""

# -- taskfile protocol vocabulary (mirrors .ai-protocol/meta/taskfile.md) ------
# Local literals, not imported: the protocol contract lives outside the codebase
# (it is documentation), so these encode the parts a static check needs.
LEGAL_STATUSES = ("pending", "in_progress", "final_review", "completed", "blocked")
"""The five task lifecycle statuses (taskfile schema §2)."""

STATUS_COMPLETED = "completed"

REQUIRED_SECTIONS = ("## Goal", "## Scope", "## Acceptance", "## Session log")
"""Body headings every task file must carry (taskfile §5). ``## Session plan``
is optional and tracked separately."""

SESSION_PLAN_SECTION = "## Session plan"
SESSION_LOG_SECTION = "## Session log"

_LEGAL_TRANSITIONS = frozenset(
    {
        ("pending", "in_progress"),  # first claim
        ("pending", "final_review"),  # dev only: claim + whole scope, one session
        ("in_progress", "in_progress"),  # dev advancement / interim review
        ("in_progress", "final_review"),  # dev advancement completes scope
        ("final_review", "final_review"),  # final gate requests changes
        ("final_review", "completed"),  # final gate passes
        ("final_review", "in_progress"),  # final_review set in error, reopened
        ("blocked", "pending"),  # unblocked, restored
        ("blocked", "in_progress"),
        ("blocked", "final_review"),
    }
)
"""Legal ``(before, after)`` status edges from the taskfile transition table
(§3), excluding the universal ``any -> blocked`` (handled in
:func:`_transition_legal` since every legal status may be blocked).

Two of these edges are entry-kind-dependent rather than universal, so membership
here is necessary but not sufficient — :func:`_transition_legal` narrows them:
``pending -> final_review`` is dev-only, and the ``final_review -> *`` forward
edges are verdict-aware for review entries."""

# -- score penalties (transparent: score = 1 - sum(penalties), floored at 0) ---
# protocol axis
_P_STATUS_ILLEGAL = 0.30
_P_ID = 0.10
_P_SESSION_EST = 0.10
_P_BLOCKER_RULE = 0.20
_P_FIX_SET = 0.20
_P_MISSING_SECTION = 0.15
_P_ILLEGAL_TRANSITION = 0.20
_P_CONTINUITY = 0.10
_P_FINAL_STATUS = 0.10
_P_FINAL_OPEN = 0.15
# reliability axis
_P_NO_DIAGNOSTIC = 0.20
# handoff axis
_P_MALFORMED_ENTRY = 0.10
_P_INCOMPLETE_ENTRY = 0.10
_P_UNCOVERED_REVIEW = 0.10
_P_DISPUTE = 0.20
_HANDOFF_GROUP_CAP = 0.30
"""Per-group cap so a run of same-kind handoff issues cannot alone zero the axis."""
# estimate axis: overrun uses a ratio (planned/actual), not a flat penalty.
# knowledge axis
_P_CLOSEOUT = 0.40
_P_PREFETCH = 0.10
_P_REPEATED_WARN = 0.20
_WARN_THRESHOLD = 3
"""Evidence-degradation warning count at/above which the knowledge axis flags
'repeated missing evidence'."""

_CONFIDENCE_NO_SESSION_LOG = 0.6
"""Protocol-axis confidence when the task file has no session log (frontmatter
still checkable, but the transition/handoff evidence is absent)."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class StaticEvaluator:
    """Deterministic, non-semantic task-metric computation over an evidence pack.

    Part of the optional evaluation subsystem, so it shares the gate: every
    operation fails closed with
    :class:`~orch_hub.evaluation.EvaluationDisabledError` when
    ``Config.evaluation_enabled`` is off, giving the whole subsystem a single
    backend boundary (the later API gate maps this to a removed/404 surface).

    Metric computation is a pure function of the pack (no clock/subprocess/
    filesystem), so :meth:`run_metrics` and :meth:`build_report` need nothing
    injected but the ``clock`` that stamps a report's ``generated_ts`` (injectable
    so tests are deterministic). :meth:`generate_metrics` is the one path that
    *builds and persists*: it delegates evidence assembly to an
    :class:`~orch_hub.evaluation.EvaluationService` (default constructed from the
    same config; injectable for tests) and writes ``static_metrics.json`` beside
    that evaluation's ``evidence.json``.
    """

    def __init__(
        self,
        config: Config,
        *,
        evaluation_service: EvaluationService | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ):
        self._config = config
        self._service = (
            evaluation_service
            if evaluation_service is not None
            else EvaluationService(config)
        )
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._config.evaluation_enabled

    def _require_enabled(self) -> None:
        if not self._config.evaluation_enabled:
            raise EvaluationDisabledError(
                "task evaluation is disabled (ORCH_HUB_EVAL_ENABLED)"
            )

    def run_metrics(self, pack: EvidencePack) -> RunMetrics:
        """Compute the task-level run/session/event rollup for ``pack``.

        Pure and read-only: it derives counts from the pack's already-assembled
        run evidence (records, metadata-only events, session snapshots, bounded
        log/``spawn.out`` tails) and target, mutating nothing. Total over any
        pack — an empty pack yields all-zero counts, a partial/corrupt run
        degrades to its evidence-only outcome plus a warning count, never a crash.
        """
        self._require_enabled()
        return _compute_run_metrics(pack)

    def build_report(self, pack: EvidencePack) -> StaticMetricsReport:
        """Compute the full static-metrics report for ``pack`` (pure, no writes).

        Composes the run rollup (:meth:`run_metrics`), the task-file protocol
        facts parsed from ``pack.target.content``, and the five evidence-backed
        score axes into a :class:`~orch_hub.models.StaticMetricsReport`. Total
        over any pack: an absent/partial task file yields ``None`` (not-assessable)
        scores plus warnings rather than a crash, and the report ties to
        ``pack.evaluation_id``. ``generated_ts`` comes from the injected clock, so
        the result is deterministic for a fixed clock. This is the on-demand path
        (e.g. a later dashboard scoring an already-persisted pack); persistence is
        :meth:`generate_metrics`.
        """
        self._require_enabled()
        return _build_report(pack, self._clock().isoformat())

    def generate_metrics(
        self, repo: ManagedRepo, task_id: str
    ) -> StaticMetricsReport:
        """Build a fresh evidence pack and persist its static-metrics report.

        The stable pairing the acceptance asks for: it delegates to
        :meth:`EvaluationService.generate_evidence` (which mints the evaluation
        id, reserves the contained dir, and writes ``evidence.json``), scores the
        returned pack, and writes ``static_metrics.json`` beside it — one dir per
        evaluation, both artifacts tied to the same id. The report write goes back
        through :meth:`EvaluationService.write_artifact` rather than opening
        ``pack.evaluation_dir / <name>`` directly: it is atomic the same way
        (same-dir tmp + rename) and, more to the point, reaches the dir through the
        evaluations tree's own no-follow walk, so this layer holds no containment
        rule of its own. If it fails, the whole evaluation dir is
        removed so no half-pair (evidence without metrics) is left behind,
        mirroring ``generate_evidence``'s own failure cleanup — and through
        :meth:`EvaluationService.remove_evaluation`, the same walk again, because a
        cleanup that named the dir instead could be redirected by a link swapped in
        at the repo/task hop while this generation was running. Read-only over
        target repo/run state — the only writes are the two hub-owned artifacts.

        Retention is applied via ``generate_evidence(prune=False)`` +
        :meth:`EvaluationService.auto_prune` *after* the metrics write succeeds, not
        inside ``generate_evidence``: pruning before this write could fail would let
        a failed metrics generation (whose cleanup removes only the new dir) delete
        older retained evaluations first, shrinking the task's history below the cap.
        """
        self._require_enabled()
        pack = self._service.generate_evidence(repo, task_id, prune=False)
        report = _build_report(pack, self._clock().isoformat())
        try:
            self._service.write_artifact(
                repo, task_id, pack.evaluation_id, STATIC_METRICS_NAME, report.to_dict()
            )
        except Exception:
            self._service.remove_evaluation(repo, task_id, pack.evaluation_id)
            raise
        # Only now that the evidence+metrics pair is durable: retention can never
        # have deleted older evaluations for a metrics generation that then failed.
        self._service.auto_prune(repo, task_id, protect=pack.evaluation_id)
        return report


def _compute_run_metrics(pack: EvidencePack) -> RunMetrics:
    """Aggregate every related run's rollup into the task-level :class:`RunMetrics`."""
    rollups = tuple(_run_rollup(run) for run in pack.runs)
    durations = [r.duration_seconds for r in rollups if r.duration_seconds is not None]
    peaks = [r.peak_context_tokens for r in rollups if r.peak_context_tokens is not None]
    return RunMetrics(
        evaluation_id=pack.evaluation_id,
        target_present=pack.target.present,
        target_source=pack.target.source,
        run_count=len(rollups),
        live_count=_count(rollups, RUN_OUTCOME_LIVE),
        clean_count=_count(rollups, RUN_OUTCOME_CLEAN),
        failed_count=_count(rollups, RUN_OUTCOME_FAILED),
        stopped_count=_count(rollups, RUN_OUTCOME_STOPPED),
        ended_unknown_count=_count(rollups, RUN_OUTCOME_ENDED_UNKNOWN),
        broken_count=_count(rollups, RUN_OUTCOME_BROKEN),
        failed_without_diagnostic_count=sum(
            1
            for r in rollups
            if r.outcome in _FAILURE_OUTCOMES and not r.has_diagnostic
        ),
        human_input_count=sum(r.answer_count for r in rollups),
        dev_session_count=sum(r.dev_session_count for r in rollups),
        review_session_count=sum(r.review_session_count for r in rollups),
        plan_gate_count=sum(r.plan_gate_count for r in rollups),
        total_turns=sum(r.total_turns for r in rollups),
        peak_context_tokens=max(peaks) if peaks else None,
        total_duration_seconds=sum(durations) if durations else None,
        warning_count=len(pack.warnings) + sum(r.warning_count for r in rollups),
        runs=rollups,
    )


def _run_rollup(run: RunEvidence) -> RunRollup:
    """Derive one run's rollup from its evidence, read-only."""
    record = run.record
    sessions = run.sessions.sessions if run.sessions is not None else ()
    return RunRollup(
        run_id=record.run_id,
        task_status_at_launch=record.task_status,
        outcome=_classify_outcome(run),
        ended=run.ended,
        exit_code=record.exit_code,
        stop_requested=_stop_requested(run.events),
        duration_seconds=_duration_seconds(record.started_ts, record.ended_ts),
        answer_count=sum(1 for e in run.events if e.kind == KIND_ANSWER_RECORDED),
        dev_session_count=_session_count(sessions, DEV_ROLE, SESSION_KIND),
        review_session_count=_session_count(sessions, REVIEW_ROLE, SESSION_KIND),
        plan_gate_count=sum(1 for s in sessions if s.kind == PLAN_GATE_KIND),
        total_turns=sum(s.turns for s in sessions),
        peak_context_tokens=_peak_context(sessions),
        has_diagnostic=_has_lines(run.log) or _has_lines(run.spawn_out),
        warning_count=len(run.warnings),
    )


def _classify_outcome(run: RunEvidence) -> str:
    """Map a run's raw evidence to one evidence-only outcome (never the run
    manager's live verdict). Order matters: a broken record first, then live,
    then stop evidence outranks the bare exit code, then exit-code buckets."""
    record = run.record
    if record.error is not None:
        return RUN_OUTCOME_BROKEN
    if record.ended_ts is None:
        return RUN_OUTCOME_LIVE
    if _stop_requested(run.events):
        return RUN_OUTCOME_STOPPED
    if record.exit_code is None:
        return RUN_OUTCOME_ENDED_UNKNOWN
    if record.exit_code == 0:
        return RUN_OUTCOME_CLEAN
    return RUN_OUTCOME_FAILED


def _stop_requested(events: Sequence[RunEvent]) -> bool:
    """True when the run has a ``stop_requested`` audit event (a hub stop)."""
    return any(e.kind == KIND_STOP_REQUESTED for e in events)


def _session_count(sessions: Sequence[SessionSummary], role: str, kind: str) -> int:
    return sum(1 for s in sessions if s.role == role and s.kind == kind)


def _peak_context(sessions: Sequence[SessionSummary]) -> int | None:
    """Max reported per-session ``context≈`` across the run, or ``None``."""
    values = [s.context_tokens for s in sessions if s.context_tokens is not None]
    return max(values) if values else None


def _has_lines(snapshot: LogSnapshot | None) -> bool:
    """True when a log/spawn snapshot carries real diagnostic content.

    A present-but-empty log tail (an empty file read successfully) does not count
    — only non-empty lines are usable diagnostics. ``read_spawn_out`` already
    treats blank captures as absent, so ``present`` implies content there; the
    ``lines`` guard makes the rule uniform across both snapshot kinds.
    """
    return snapshot is not None and snapshot.present and bool(snapshot.lines)


def _duration_seconds(started: str | None, ended: str | None) -> float | None:
    """Wall-clock seconds between two ISO run timestamps, or ``None``.

    ``None`` when either timestamp is absent/malformed, or the difference is
    negative (a nonsensical end-before-start). Mirrors
    :func:`orch_hub.evaluation._parse_ts`: a naive timestamp is assumed UTC so
    both are aware and comparable."""
    start = _parse_ts(started)
    end = _parse_ts(ended)
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
    return seconds if seconds >= 0 else None


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _count(rollups: Sequence[RunRollup], outcome: str) -> int:
    return sum(1 for r in rollups if r.outcome == outcome)


# -- memory-read rollup -------------------------------------------------------


def _compute_read_metrics(pack: EvidencePack) -> ReadMetrics:
    """Distil the pack's per-session read behavior into the L1 rollup (pure).

    Carries over only the deterministic facts: which declared docs a session was
    expected to read, which it did, and whether that could be measured at all.
    The ordered trace and its timing anchors stay in the pack — they are evidence
    for a judgment about *when* and *whether it helped*, which is Layer 2's, and
    copying them here would imply this layer scores them.

    Total over any pack: one with no ``reads`` section (a pack predating it, or
    one built where no transcript store exists) yields an unavailable rollup with
    zero counts, which the score reads as not-assessable rather than as zero
    compliance.
    """
    behavior = pack.reads
    if behavior is None:
        return ReadMetrics(
            available=False,
            session_count=0,
            applicable_count=0,
            unknown_applicable_count=0,
            scored_count=0,
            complete_count=0,
        )
    sessions = tuple(
        SessionReadRollup(
            session_id=s.session_id,
            role=s.role,
            kind=s.kind,
            applicable=s.applicable,
            assessable=s.assessable,
            expected_count=len(s.expected),
            read_count=len(s.read),
            missing=s.missing,
            coverage=s.coverage,
            complete=s.complete,
            undeclared_read_count=len(s.undeclared_reads),
            detail=s.detail,
        )
        for s in behavior.sessions
    )
    scored = tuple(s for s in sessions if _scored(s))
    return ReadMetrics(
        available=behavior.available,
        session_count=len(sessions),
        applicable_count=sum(1 for s in sessions if s.applicable is True),
        unknown_applicable_count=sum(1 for s in sessions if s.applicable is None),
        scored_count=len(scored),
        complete_count=sum(1 for s in scored if s.complete),
        # Carried, not derived from ``sessions``: a session past the reader's cap
        # has no rollup here by construction, and reading the denominator off what
        # survived the cap is exactly how an unmeasured candidate disappears.
        dropped_candidate_count=behavior.dropped_candidates,
        sessions=sessions,
        warnings=tuple(behavior.warnings),
    )


def _scored(session: SessionReadRollup) -> bool:
    """Whether this session contributes to the prefetch-reading *score*.

    All three clauses are load-bearing and none implies another: the session must
    be one the measurement is about (``applicable``), its reading must have been
    measurable (``assessable``), and the verdict must actually be present — the
    last defensively, since an unclaimed verdict is exactly what a degraded
    session reports.
    """
    return session.applicable is True and session.assessable and session.complete is not None


# -- report assembly ----------------------------------------------------------


def _build_report(pack: EvidencePack, generated_ts: str | None) -> StaticMetricsReport:
    """Compose the full report from a pack (pure): run rollup + protocol facts +
    scored axes + the pack's evidence-degradation warnings, tied to
    ``pack.evaluation_id``."""
    runs = _compute_run_metrics(pack)
    protocol = _parse_protocol(pack.target)
    reads = _compute_read_metrics(pack)
    scores = (
        _score_protocol(protocol),
        _score_reliability(runs),
        _score_handoff(protocol),
        _score_estimate(protocol),
        _score_knowledge(protocol, runs),
        _score_prefetch_reads(reads),
    )
    # Top-level warnings are the *evidence-degradation* signals (missing/corrupt
    # run artifacts), kept distinct from each axis's quality warnings.
    evidence_warnings = tuple(pack.warnings) + tuple(
        w for run in pack.runs for w in run.warnings
    )
    return StaticMetricsReport(
        evaluation_id=pack.evaluation_id,
        generated_ts=generated_ts,
        repo_name=pack.target.repo_name,
        task_id=pack.target.task_id,
        target_present=pack.target.present,
        target_source=pack.target.source,
        runs=runs,
        protocol=protocol,
        scores=scores,
        warnings=evidence_warnings,
        reads=reads,
        schema_version=STATIC_SCHEMA_VERSION,
    )


# -- protocol parsing ---------------------------------------------------------
# The evaluated task file (``EvidencePack.target.content``) is parsed for the
# protocol-structure facts a static check can verify. Ownership note: the
# inventory reader (:mod:`orch_hub.task_store`) owns *status/summary/blockers*
# for the active task set; this parser owns *protocol-structure* facts (session
# log entries, transitions, review coverage) — an evaluation concern — and keeps
# local frontmatter/list helpers (mirroring task_store's private ones) so the
# evaluation subsystem stays independent of inventory internals.

_ENTRY_HEADING = re.compile(
    r"^###\s+\S+\s*/\s*(?P<sid>.+?)\s*/\s*"
    r"(?:review of\s+(?P<wsid>.+?)\s*/\s*)?"
    r"\(\s*(?P<before>[^)]*?)\s*(?:→|-+>)\s*(?P<after>[^)]*?)\s*\)\s*$"
)
"""A ``### <date> / <sid> [/ review of <wsid>] / (<before> -> <after>)`` heading.
Accepts the protocol's ``→`` arrow or an ASCII ``->`` fallback."""

_FIELD_LINE = re.compile(r"^-\s+(?P<name>[A-Za-z][A-Za-z0-9-]*)\s*:\s*(?P<value>.*)$")
"""An entry field/marker line ``- Name: value`` (taskfile: machine-parsed exact
``- X: ...`` lines). The name is captured; a prose ``-`` bullet is ignored."""

_WORK_REQUIRED = frozenset({"done", "next", "open"})
_REVIEW_REQUIRED = frozenset({"verdict", "group", "findings"})
_VERDICT_CHANGES = "changes-requested"
_VERDICT_PASS = "pass"


@dataclass(frozen=True)
class _LogEntry:
    """One parsed ``## Session log`` entry (internal to the parser)."""

    sid: str | None
    reviews: str | None  # work session id a review entry covers; None for work
    before: str | None
    after: str | None
    verdict: str | None  # lowercased review verdict
    field_names: frozenset[str]  # lowercased field names present
    open_value: str | None  # last `Open:` value in this entry, lowercased
    dispute_count: int
    malformed: bool  # heading did not parse

    @property
    def is_review(self) -> bool:
        return self.reviews is not None


@dataclass(frozen=True)
class _LogFacts:
    """Aggregated session-log facts (internal to the parser)."""

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
    final_open_none: bool


def _parse_protocol(target: EvaluationTarget) -> ProtocolMetrics:
    """Parse the target task file into deterministic protocol facts.

    Total over any content: an absent/empty file yields the all-empty
    :func:`_empty_protocol`; a present file is parsed for frontmatter legality,
    required sections, and the ``## Session log`` entry structure. No scoring
    here — just facts.
    """
    content = target.content
    if not content:
        return _empty_protocol()

    fm = _frontmatter(content)
    status = fm.get("status") or None
    id_value = fm.get("id")
    est_present = "session-est" in fm
    session_est = _session_est_from_raw(fm.get("session-est"))
    blockers = _inline_list(fm.get("blockers"))
    prefetch_present = "prefetch" in fm

    section = _session_log_section(content)
    entries = _parse_entries(section) if section is not None else []
    facts = _aggregate_entries(entries, status)

    return ProtocolMetrics(
        task_content_present=True,
        frontmatter_present=bool(fm),
        frontmatter_status=status,
        status_legal=status in LEGAL_STATUSES,
        id_present=bool(id_value),
        id_matches=bool(id_value) and id_value == target.task_id,
        session_est=session_est,
        session_est_present=est_present,
        session_est_wellformed=session_est is not None,
        blocker_rule_ok=_blocker_rule_ok(status, blockers),
        fix_set_present="fix-set" in fm,
        prefetch_present=prefetch_present,
        prefetch_wellformed=(
            _prefetch_wellformed(fm.get("prefetch")) if prefetch_present else True
        ),
        missing_sections=tuple(
            s for s in REQUIRED_SECTIONS if not _has_section(content, s)
        ),
        has_session_plan=_has_section(content, SESSION_PLAN_SECTION),
        session_log_present=section is not None,
        entry_count=facts.entry_count,
        work_entry_count=facts.work_entry_count,
        review_entry_count=facts.review_entry_count,
        malformed_entry_count=facts.malformed_entry_count,
        uncovered_work_entries=facts.uncovered_work_entries,
        pass_verdict_count=facts.pass_verdict_count,
        changes_requested_count=facts.changes_requested_count,
        remediation_count=facts.remediation_count,
        advancement_entry_count=facts.advancement_entry_count,
        blocked_entry_count=facts.blocked_entry_count,
        dispute_unresolved_count=facts.dispute_unresolved_count,
        incomplete_entries=facts.incomplete_entries,
        illegal_transitions=facts.illegal_transitions,
        transition_continuity_ok=facts.transition_continuity_ok,
        final_status_matches=facts.final_status_matches,
        completed=status == STATUS_COMPLETED,
        final_open_none=facts.final_open_none,
    )


def _empty_protocol() -> ProtocolMetrics:
    """All-empty facts for a task with no file content (an id evaluable only for
    run history). Every count zero, every bool safe-default."""
    return ProtocolMetrics(
        task_content_present=False,
        frontmatter_present=False,
        frontmatter_status=None,
        status_legal=False,
        id_present=False,
        id_matches=False,
        session_est=None,
        session_est_present=False,
        session_est_wellformed=False,
        blocker_rule_ok=True,
        fix_set_present=False,
        prefetch_present=False,
        prefetch_wellformed=True,
        missing_sections=REQUIRED_SECTIONS,
        has_session_plan=False,
        session_log_present=False,
        entry_count=0,
        work_entry_count=0,
        review_entry_count=0,
        malformed_entry_count=0,
        uncovered_work_entries=(),
        pass_verdict_count=0,
        changes_requested_count=0,
        remediation_count=0,
        advancement_entry_count=0,
        blocked_entry_count=0,
        dispute_unresolved_count=0,
        incomplete_entries=(),
        illegal_transitions=(),
        transition_continuity_ok=True,
        final_status_matches=False,
        completed=False,
        final_open_none=False,
    )


def _aggregate_entries(entries: list[_LogEntry], status: str | None) -> _LogFacts:
    """Fold parsed entries into the session-log facts (chronological order)."""
    wellformed = [e for e in entries if not e.malformed]
    work = [e for e in wellformed if not e.is_review]
    review = [e for e in wellformed if e.is_review]
    reviewed = {e.reviews for e in review if e.reviews}
    uncovered = tuple(e.sid for e in work if e.sid and e.sid not in reviewed)

    # Remediation: a work session whose most recent *preceding* review requested
    # changes (the taskfile remediation predicate). Walk in log order. The
    # complement is advancement — work that moved planned scope. The verdict is
    # *not* reset by an intervening work entry, so a fix set split across several
    # sessions stays remediation for its whole run: what ends a convergence chain
    # is the next review, not the next session.
    remediation = 0
    advancement = 0
    last_verdict: str | None = None
    for entry in entries:
        if entry.malformed:
            continue
        if entry.is_review:
            last_verdict = entry.verdict
        elif last_verdict == _VERDICT_CHANGES:
            remediation += 1
        else:
            advancement += 1

    incomplete = tuple(
        (e.sid or "?") for e in wellformed if not _entry_fields_ok(e)
    )

    edged = [e for e in wellformed if e.before and e.after]
    illegal = tuple(
        f"{e.before} -> {e.after}" for e in edged if not _transition_legal(e)
    )
    continuity_ok = all(
        edged[i].after == edged[i + 1].before for i in range(len(edged) - 1)
    )
    final_status_matches = (
        bool(edged) and status is not None and edged[-1].after == status
    )

    # The FINAL session-log entry's forward-looking Open (taskfile §5: a completed
    # task's final entry resolves every forward-looking item, i.e. `Open: none`).
    # Read strictly from the last entry — an earlier entry's `Open: none` must not
    # stand in for a final entry that omits it, and a missing Open is not `none`.
    final_open_none = bool(entries) and entries[-1].open_value == "none"

    return _LogFacts(
        entry_count=len(entries),
        work_entry_count=len(work),
        review_entry_count=len(review),
        malformed_entry_count=sum(1 for e in entries if e.malformed),
        uncovered_work_entries=uncovered,
        pass_verdict_count=sum(1 for e in review if e.verdict == _VERDICT_PASS),
        changes_requested_count=sum(
            1 for e in review if e.verdict == _VERDICT_CHANGES
        ),
        remediation_count=remediation,
        advancement_entry_count=advancement,
        blocked_entry_count=sum(1 for e in wellformed if e.after == "blocked"),
        dispute_unresolved_count=sum(e.dispute_count for e in entries),
        incomplete_entries=incomplete,
        illegal_transitions=illegal,
        transition_continuity_ok=continuity_ok,
        final_status_matches=final_status_matches,
        final_open_none=final_open_none,
    )


def _parse_entries(section: str) -> list[_LogEntry]:
    """Split a session-log section into per-``###``-entry blocks and parse each."""
    entries: list[_LogEntry] = []
    block: list[str] | None = None
    for line in section.splitlines():
        if line.startswith("### "):
            if block is not None:
                entries.append(_parse_one_entry(block))
            block = [line]
        elif block is not None:
            block.append(line)
    if block is not None:
        entries.append(_parse_one_entry(block))
    return entries


def _parse_one_entry(lines: list[str]) -> _LogEntry:
    """Parse one entry block (heading + field lines) into a :class:`_LogEntry`."""
    field_names: set[str] = set()
    open_value: str | None = None
    verdict: str | None = None
    dispute_count = 0
    for line in lines[1:]:
        match = _FIELD_LINE.match(line)
        if match is None:
            continue
        name = match.group("name").lower()
        value = match.group("value").strip()
        field_names.add(name)
        if name == "open":
            open_value = value.lower()
        elif name == "verdict":
            verdict = value.lower()
        elif name == "dispute-unresolved":
            dispute_count += 1

    heading = _ENTRY_HEADING.match(lines[0])
    if heading is None:
        return _LogEntry(
            sid=None,
            reviews=None,
            before=None,
            after=None,
            verdict=verdict,
            field_names=frozenset(field_names),
            open_value=open_value,
            dispute_count=dispute_count,
            malformed=True,
        )
    wsid = heading.group("wsid")
    return _LogEntry(
        sid=heading.group("sid").strip(),
        reviews=wsid.strip() if wsid else None,
        before=(heading.group("before") or "").strip() or None,
        after=(heading.group("after") or "").strip() or None,
        verdict=verdict,
        field_names=frozenset(field_names),
        open_value=open_value,
        dispute_count=dispute_count,
        malformed=False,
    )


def _entry_fields_ok(entry: _LogEntry) -> bool:
    """True when a well-formed entry carries its required fields (Done/Next/Open
    for work; Verdict/Group/Findings for review)."""
    required = _REVIEW_REQUIRED if entry.is_review else _WORK_REQUIRED
    return required <= entry.field_names


def _transition_legal(entry: _LogEntry) -> bool:
    """True when ``entry``'s ``(before, after)`` status edge is legal (taskfile
    §3). Any legal status may transition to ``blocked``.

    Legality is **entry-kind-aware**, because the taskfile table is written per
    session kind and the raw status pair alone cannot say which kind wrote it.

    ``pending -> final_review`` is legal for a **dev** entry only. A dev session
    entering a `pending` task performs the claim (`pending -> in_progress`) as
    mechanical bookkeeping, not as work; when that same session then completes
    the whole scope, the entry's honest `status_before` is the status it *found*,
    and the intermediate `in_progress` never existed as an observable state. The
    collapsed edge is therefore the correct record of a one-session task, not a
    skipped transition — reading it as illegal penalised exactly the tasks that
    went best. For a **review** entry the same pair stays illegal: no review
    session may claim a `pending` task, so the edge could only mean a lost dev
    entry.

    Review **final-gate** entries (``before == final_review``) are verdict-aware:
    the taskfile table and the review contract (§6, "findings never downgrade a
    status") tie the forward edge to the verdict — a ``pass`` completes, a
    ``changes-requested`` leaves status at ``final_review`` — which the raw status
    pair cannot express. So a changes-requested review recorded as
    ``final_review -> completed`` is caught here, where a pair-only check passed it
    (the pair is structurally in :data:`_LEGAL_TRANSITIONS`)."""
    before, after = entry.before, entry.after
    if after == "blocked":
        return before in LEGAL_STATUSES
    if (before, after) not in _LEGAL_TRANSITIONS:
        return False
    if entry.is_review and (before, after) == ("pending", "final_review"):
        return False  # only a dev session collapses the claim into completion
    if entry.is_review and before == "final_review":
        if after == "completed":
            return entry.verdict == _VERDICT_PASS  # only a pass completes
        if after == "final_review":
            return entry.verdict != _VERDICT_PASS  # a pass must complete, not hold
        # after == "in_progress": the documented "final_review set in error"
        # correction (review contract §6), independent of the verdict.
    return True


def _session_log_section(content: str) -> str | None:
    """Return the ``## Session log`` body (heading to the next ``## `` or EOF), or
    ``None`` when the section is absent."""
    lines = content.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == SESSION_LOG_SECTION),
        None,
    )
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("## ") and stripped != SESSION_LOG_SECTION:
            end = j
            break
    return "\n".join(lines[start + 1 : end])


def _has_section(content: str, heading: str) -> bool:
    return any(line.strip() == heading for line in content.splitlines())


def _frontmatter(content: str) -> dict[str, str]:
    """Parse the leading ``---`` frontmatter into raw ``key: value`` strings
    (mirrors ``task_store._parse_frontmatter``; empty when no opening fence)."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def _inline_list(raw: str | None) -> tuple[str, ...]:
    """Parse an inline ``[a, b]`` frontmatter list (mirrors
    ``task_store._parse_inline_list``); absent/empty yields ``()``."""
    if raw is None:
        return ()
    inner = raw.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    return tuple(item.strip() for item in inner.split(",") if item.strip())


def _session_est_from_raw(raw: str | None) -> SessionEst | None:
    """Parse ``session-est: <current>/<total>`` into :class:`SessionEst`, or
    ``None`` for any non ``int/int`` shape (mirrors
    ``task_store._parse_session_est``; never raises)."""
    if raw is None:
        return None
    parts = raw.split("/")
    if len(parts) != 2:
        return None
    current, total = parts[0].strip(), parts[1].strip()
    if not _ascii_digits(current) or not _ascii_digits(total):
        return None
    return SessionEst(current=int(current), total=int(total))


def _ascii_digits(text: str) -> bool:
    return bool(text) and text.isascii() and text.isdigit()


def _blocker_rule_ok(status: str | None, blockers: tuple[str, ...]) -> bool:
    """The blocked⇔≥1-blocker rule (taskfile §1). A ``blocked`` task needs at
    least one blocker; any other *legal* status needs none; an absent/illegal
    status is not a blocker-rule question (the status penalty covers it)."""
    if status == "blocked":
        return len(blockers) >= 1
    if status in LEGAL_STATUSES:
        return len(blockers) == 0
    return True


def _prefetch_wellformed(raw: str | None) -> bool:
    """True when a present ``prefetch`` is a (possibly empty) list of ``.ai/*.md``
    paths. An empty ``[]`` is legitimately 'no prefetch'; a non-list or a
    non-``.ai``/non-``.md`` entry is malformed."""
    items = _inline_list(raw)
    if not items:
        return (raw or "").strip() in ("", "[]")
    return all(item.startswith(".ai/") and item.endswith(".md") for item in items)


# -- scoring ------------------------------------------------------------------
# Each axis: score = 1 - sum(documented penalties), floored at 0; every penalty
# appends a warning naming the evidence. An axis with no backing evidence is
# score=None, confidence=0 (not-assessable), kept distinct from a real 0.0.


def _clamp01(value: float) -> float:
    """Clamp to ``[0, 1]`` and round to 3 places for stable JSON."""
    return round(max(0.0, min(1.0, value)), 3)


def _score_protocol(pm: ProtocolMetrics) -> ScoreComponent:
    """Frontmatter legality, required sections, status-transition path, and
    completion hygiene (taskfile §§2/3/5)."""
    if not pm.task_content_present:
        return ScoreComponent(
            SCORE_PROTOCOL,
            None,
            0.0,
            ("no task file content in evidence; protocol not assessable",),
        )
    warnings: list[str] = []
    penalty = 0.0
    if not pm.frontmatter_present:
        penalty += _P_STATUS_ILLEGAL
        warnings.append("task file has no frontmatter fence")
    else:
        if not pm.status_legal:
            penalty += _P_STATUS_ILLEGAL
            warnings.append(
                f"frontmatter status {pm.frontmatter_status!r} is not a legal"
                " lifecycle status"
            )
        if not pm.id_matches:
            penalty += _P_ID
            warnings.append("frontmatter id missing or does not match the task id")
        if not (pm.session_est_present and pm.session_est_wellformed):
            penalty += _P_SESSION_EST
            warnings.append("frontmatter session-est missing or malformed")
        if not pm.blocker_rule_ok:
            penalty += _P_BLOCKER_RULE
            warnings.append(
                "blocker rule violated (blocked needs >=1 blocker; a non-blocked"
                " status needs none)"
            )
        if pm.fix_set_present:
            penalty += _P_FIX_SET
            warnings.append("fix-set is declared (must be cleared before completion)")
    for section in pm.missing_sections:
        penalty += _P_MISSING_SECTION
        warnings.append(f"required section missing: {section}")
    if pm.illegal_transitions:
        penalty += _P_ILLEGAL_TRANSITION
        warnings.append(
            "illegal status transition(s): " + "; ".join(pm.illegal_transitions)
        )
    if pm.entry_count and not pm.transition_continuity_ok:
        penalty += _P_CONTINUITY
        warnings.append("session-log status transitions are not continuous")
    if pm.entry_count and not pm.final_status_matches:
        penalty += _P_FINAL_STATUS
        warnings.append("final session-log status does not match frontmatter status")
    if pm.completed and not pm.final_open_none:
        penalty += _P_FINAL_OPEN
        warnings.append("completed task's final Open is not 'none'")
    confidence = 1.0 if pm.session_log_present else _CONFIDENCE_NO_SESSION_LOG
    return ScoreComponent(
        SCORE_PROTOCOL, _clamp01(1.0 - penalty), confidence, tuple(warnings)
    )


def _score_reliability(rm: RunMetrics) -> ScoreComponent:
    """Run failures and whether failures left a diagnostic; confidence drops with
    evidence-degradation warnings."""
    if rm.run_count == 0:
        return ScoreComponent(
            SCORE_RELIABILITY,
            None,
            0.0,
            ("no related runs; run reliability not assessable",),
        )
    warnings: list[str] = []
    score = 1.0 - rm.failure_count / rm.run_count
    if rm.failure_count:
        warnings.append(f"{rm.failure_count} of {rm.run_count} run(s) failed")
    if rm.failed_without_diagnostic_count:
        score -= _P_NO_DIAGNOSTIC
        warnings.append(
            f"{rm.failed_without_diagnostic_count} failed run(s) lack diagnostics"
            " (no log or spawn.out)"
        )
    confidence = _clamp01(1.0 - min(0.6, rm.warning_count * 0.15))
    if rm.warning_count:
        warnings.append(
            f"{rm.warning_count} evidence-degradation warning(s) lower confidence"
        )
    return ScoreComponent(SCORE_RELIABILITY, _clamp01(score), confidence, tuple(warnings))


def _score_handoff(pm: ProtocolMetrics) -> ScoreComponent:
    """Session-log entry structure: malformed/incomplete entries, review coverage
    (once completed), and unresolved disputes."""
    if not pm.task_content_present:
        return ScoreComponent(
            SCORE_HANDOFF,
            None,
            0.0,
            ("no task file content; handoff structure not assessable",),
        )
    if not pm.session_log_present:
        return ScoreComponent(
            SCORE_HANDOFF,
            None,
            0.0,
            ("task file has no session log; handoff structure not assessable",),
        )
    if pm.entry_count == 0:
        return ScoreComponent(
            SCORE_HANDOFF, 0.0, 1.0, ("session log present but has no entries",)
        )
    warnings: list[str] = []
    penalty = 0.0
    if pm.malformed_entry_count:
        penalty += min(_HANDOFF_GROUP_CAP, pm.malformed_entry_count * _P_MALFORMED_ENTRY)
        warnings.append(
            f"{pm.malformed_entry_count} malformed session-log entry heading(s)"
        )
    if pm.incomplete_entries:
        penalty += min(
            _HANDOFF_GROUP_CAP, len(pm.incomplete_entries) * _P_INCOMPLETE_ENTRY
        )
        warnings.append(
            "session-log entries missing required fields: "
            + ", ".join(pm.incomplete_entries)
        )
    if pm.completed and pm.uncovered_work_entries:
        penalty += min(
            _HANDOFF_GROUP_CAP, len(pm.uncovered_work_entries) * _P_UNCOVERED_REVIEW
        )
        warnings.append(
            f"{len(pm.uncovered_work_entries)} work session(s) with no review entry"
        )
    if pm.dispute_unresolved_count:
        penalty += _P_DISPUTE
        warnings.append(
            f"{pm.dispute_unresolved_count} unresolved disputed finding(s)"
        )
    return ScoreComponent(SCORE_HANDOFF, _clamp01(1.0 - penalty), 1.0, tuple(warnings))


def _score_estimate(pm: ProtocolMetrics) -> ScoreComponent:
    """Session-est planned total vs actual dev **advancement** sessions. Overrun
    scales the score by the ratio.

    The actual count deliberately excludes remediation entries. ``session-est``
    estimates the work of advancing planned scope, and review sessions do not
    consume it (taskfile §4); a remediation session is the dev half of that same
    review loop — it converges on findings instead of advancing a slice, so
    charging it to the estimate measures how much review happened, not how well
    the work was estimated. That penalised the review loop for doing its job:
    a task whose findings took three sessions to fix scored worse than one
    nobody reviewed closely. Remediation rounds stay in the report as a neutral
    diagnostic warning, and any number of them against a plan of one is now a
    fit. Ordinary continuations and scope-driven splits are advancement and still
    count, so genuine underestimates score exactly as before."""
    est = pm.session_est
    if est is None or est.total <= 0:
        return ScoreComponent(
            SCORE_ESTIMATE,
            None,
            0.0,
            ("session-est missing, malformed, or zero-total; estimate fit not"
             " assessable",),
        )
    if not pm.session_log_present:
        return ScoreComponent(
            SCORE_ESTIMATE,
            None,
            0.0,
            ("no session log to count dev sessions; estimate fit not assessable",),
        )
    warnings: list[str] = []
    planned, actual = est.total, pm.advancement_entry_count
    score = 1.0
    if actual > planned:
        score = planned / actual
        warnings.append(
            f"{actual} dev advancement session(s) exceeded the session-est total"
            f" of {planned}"
        )
    if pm.remediation_count:
        warnings.append(
            f"{pm.remediation_count} remediation round(s) (changes-requested"
            " rework; not charged to the estimate)"
        )
    return ScoreComponent(SCORE_ESTIMATE, _clamp01(score), 1.0, tuple(warnings))


def _score_prefetch_reads(rdm: ReadMetrics) -> ScoreComponent:
    """Fraction of measurable sessions that read their whole declared prefetch set.

    The one axis whose shape is a fraction rather than ``1 - penalties``, because
    the underlying fact is already binary per session (the ruling: a declared doc
    was read, anywhere in the session, or it was not) and a per-issue penalty
    would make two sessions with one miss each indistinguishable from one session
    with two.

    Evidence availability is kept strictly out of the score and put in the
    confidence instead — the task's rule, and the reason the two denominators
    differ. A session whose transcript is gone or whose evidence had a hole is
    *not* a session that read nothing; scoring it as one would manufacture the
    penalizing default this measurement exists to avoid. So the quality
    denominator is the sessions actually measured, while confidence is what
    fraction of the candidates those were — a task measured in one session out of
    four reports its one result honestly, at 0.25 confidence.

    Candidates are sessions the instruction plausibly applied to: known-applicable
    plus unknown-applicable, plus the ones the reader's session cap dropped before
    it could establish either (their role alone makes them candidates). A session
    known *not* applicable (a role the instruction never reaches, or one that
    declared no lazy doc) is in neither denominator: there was nothing to comply
    with, and counting it either way would be an opinion about a session with no
    obligation. A capped-out candidate is the opposite case — an obligation nobody
    looked at — so leaving it out would report full evidence coverage for a task
    that was only partly read.

    **Known limit, measured rather than approximated.** A task may declare a doc
    that did not exist when the session ran, which no session could have read;
    this layer cannot tell that case from a genuine miss, because existence *at
    session time* is not something either the task file or the pack's
    (present-tense) snapshot records. The available fix is worse than the gap:
    excusing a declared doc the snapshot no longer shows would forgive every real
    miss of a doc later split or renamed — the common case in a maintained
    ``.ai/``, and the direction that invents compliance. Measured over the whole
    archived corpus, **0 of 381** missed declared docs were paths with no history
    in their repo at all, so every miss on record is a miss of a readable
    document. The semantic layer, which does see the ``.ai/`` patch stream, is
    told to check it before any missed-retrieval finding.
    """
    if not rdm.available:
        return ScoreComponent(
            SCORE_PREFETCH_READS,
            None,
            0.0,
            ("no session read evidence in the pack; prefetch reading not assessable",),
        )
    candidates = (
        rdm.applicable_count
        + rdm.unknown_applicable_count
        + rdm.dropped_candidate_count
    )
    if candidates == 0:
        return ScoreComponent(
            SCORE_PREFETCH_READS,
            None,
            0.0,
            ("no session declared a lazy prefetch set; prefetch reading not"
             " applicable to this task",),
        )
    if rdm.scored_count == 0:
        return ScoreComponent(
            SCORE_PREFETCH_READS,
            None,
            0.0,
            (f"{candidates} candidate session(s), none of them measured"
             " (no usable read evidence); prefetch reading not assessable",),
        )
    warnings: list[str] = []
    incomplete = rdm.scored_count - rdm.complete_count
    if incomplete:
        warnings.append(
            f"{incomplete} of {rdm.scored_count} measured session(s) did not read"
            " their whole declared prefetch set: "
            + "; ".join(
                f"{s.session_id} missed {', '.join(s.missing)}"
                for s in rdm.sessions
                if _scored(s) and not s.complete
            )
        )
    unmeasured = candidates - rdm.scored_count
    if unmeasured:
        warnings.append(
            f"{unmeasured} of {candidates} applicable session(s) could not be"
            " measured; scored on the remainder with lowered confidence"
        )
    return ScoreComponent(
        SCORE_PREFETCH_READS,
        _clamp01(rdm.complete_count / rdm.scored_count),
        _clamp01(rdm.scored_count / candidates),
        tuple(warnings),
    )


def _score_knowledge(pm: ProtocolMetrics, rm: RunMetrics) -> ScoreComponent:
    """Pack-determinable knowledge-maintenance hygiene: close-out/archive
    reconciliation, malformed prefetch, and repeated evidence degradation. (The
    ``.ai/`` file-change and archived-blocker signals are not derivable from the
    pack — see the module docstring — and are intentionally omitted, not guessed.)"""
    warnings: list[str] = []
    penalty = 0.0
    if pm.completed and rm.target_source != SOURCE_ARCHIVED:
        penalty += _P_CLOSEOUT
        warnings.append(
            "completed task is not archived (close-out/index reconciliation missing)"
        )
    if pm.prefetch_present and not pm.prefetch_wellformed:
        penalty += _P_PREFETCH
        warnings.append(
            "prefetch is present but malformed (expected a list of .ai/*.md paths)"
        )
    if rm.warning_count >= _WARN_THRESHOLD:
        penalty += _P_REPEATED_WARN
        warnings.append(f"repeated missing-evidence warnings ({rm.warning_count})")
    return ScoreComponent(SCORE_KNOWLEDGE, _clamp01(1.0 - penalty), 1.0, tuple(warnings))
