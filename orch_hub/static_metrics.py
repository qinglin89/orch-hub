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
(which needs live control-dir/task-store reads the pack does not contain). The
outcome is coarser than the run manager's live verdict and is labelled so:
``clean`` (exit 0) does not distinguish task-completed from a graceful boundary
stop — that protocol-completion signal comes from the task file, parsed in
session-2.

**Session split.** Session-1 (this file) delivers the schema plus the
run/session/event/diagnostic rollups. Session-2 adds task-file/session-log
protocol metrics, score components, evidence-backed warnings, and persistence of
a ``static_metrics.json`` report tied to the pack's ``evaluation_id``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from .config import Config
from .evaluation import EvaluationDisabledError, EvidencePack, RunEvidence
from .history import KIND_ANSWER_RECORDED, KIND_STOP_REQUESTED
from .logs import LogSnapshot
from .models import RunEvent, RunMetrics, RunRollup, SessionSummary

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


class StaticEvaluator:
    """Deterministic, non-semantic task-metric computation over an evidence pack.

    Part of the optional evaluation subsystem, so it shares the gate: every
    operation fails closed with
    :class:`~orch_hub.evaluation.EvaluationDisabledError` when
    ``Config.evaluation_enabled`` is off, giving the whole subsystem a single
    backend boundary (the later API gate maps this to a removed/404 surface). It
    needs no injectable collaborators in session-1 — metric computation is a pure
    function of the pack, requiring no clock, subprocess, or filesystem; the
    config is held for the gate now and for persistence (session-2).
    """

    def __init__(self, config: Config):
        self._config = config

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
