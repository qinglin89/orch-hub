"""Tests for the static (deterministic, non-semantic) task quality metrics.

Covers the session-1 acceptance subset: run/session/event/diagnostic rollups are
deterministic and total over any evidence pack. Most tests construct
:class:`EvidencePack`/:class:`RunEvidence` objects directly (the metrics are a
pure function of the pack, so no filesystem or run manager is needed); one
integration test builds a real pack through :class:`EvaluationService` over
``tmp_path`` run dirs to prove the end-to-end wiring. The taskfile/session-log
protocol metrics, score components, and persistence are session-2.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.evaluation import (
    EVIDENCE_NAME,
    SOURCE_ACTIVE,
    SOURCE_ARCHIVED,
    EvaluationDisabledError,
    EvaluationService,
    EvidencePack,
    RunEvidence,
)
from orch_hub.history import KIND_ANSWER_RECORDED, KIND_RUN_STARTED, KIND_STOP_REQUESTED
from orch_hub.logs import LogSnapshot
from orch_hub.session_reads import MemoryRead, PrefetchDoc, ReadBehavior, SessionReads
from orch_hub.models import (
    EvaluationTarget,
    ManagedRepo,
    RunEvent,
    RunMetrics,
    RunRecord,
    SessionEst,
    SessionsSnapshot,
    SessionSummary,
)
from orch_hub.static_metrics import (
    DEV_ROLE,
    PLAN_GATE_KIND,
    REVIEW_ROLE,
    RUN_OUTCOME_BROKEN,
    RUN_OUTCOME_CLEAN,
    RUN_OUTCOME_ENDED_UNKNOWN,
    RUN_OUTCOME_FAILED,
    RUN_OUTCOME_LIVE,
    RUN_OUTCOME_STOPPED,
    SCORE_ESTIMATE,
    SCORE_HANDOFF,
    SCORE_KNOWLEDGE,
    SCORE_PREFETCH_READS,
    SCORE_PROTOCOL,
    SCORE_RELIABILITY,
    SESSION_KIND,
    STATIC_METRICS_NAME,
    STATIC_SCHEMA_VERSION,
    StaticEvaluator,
)
from orch_hub.subprocess_utils import CommandResult

TASK = "2026-07-22-task-static-quality-metrics"
REPO_PATH = Path("/repos/alpha")
START = "2026-07-22T09:00:00+00:00"
END = "2026-07-22T09:30:00+00:00"  # START + 30 min == 1800s


# -- builders (pure, no filesystem) -------------------------------------------


def target(*, present: bool = True, source: str | None = SOURCE_ACTIVE) -> EvaluationTarget:
    return EvaluationTarget(
        repo_name="alpha",
        repo_path=REPO_PATH,
        task_id=TASK,
        source=source if present else None,
        present=present,
        content="# body" if present else None,
    )


def run_ev(
    run_id: str = "r1",
    *,
    task_status: str | None = None,
    started_ts: str | None = None,
    ended_ts: str | None = None,
    exit_code: int | None = None,
    error: str | None = None,
    events=(),
    sessions: SessionsSnapshot | None = None,
    log: LogSnapshot | None = None,
    spawn_out: LogSnapshot | None = None,
    warnings=(),
) -> RunEvidence:
    record = RunRecord(
        run_id=run_id,
        repo_name="alpha",
        repo_path=REPO_PATH,
        task_id=TASK,
        task_status=task_status,
        started_ts=started_ts,
        ended_ts=ended_ts,
        exit_code=exit_code,
        error=error,
    )
    return RunEvidence(
        record=record,
        events=tuple(events),
        sessions=sessions,
        log=log,
        spawn_out=spawn_out,
        warnings=tuple(warnings),
    )


def pack(*runs: RunEvidence, evaluation_id: str = "e1", tgt=None, warnings=()) -> EvidencePack:
    return EvidencePack(
        evaluation_id=evaluation_id,
        generated_ts="2026-07-22T09:00:00+00:00",
        target=tgt if tgt is not None else target(),
        runs=tuple(runs),
        git=None,
        warnings=tuple(warnings),
    )


def event(kind: str, **kw) -> RunEvent:
    return RunEvent(ts="2026-07-22T09:00:00+00:00", kind=kind, run_id="r1", **kw)


def session(role: str = DEV_ROLE, kind: str = SESSION_KIND, turns: int = 0,
            context_tokens: int | None = None) -> SessionSummary:
    return SessionSummary(role=role, kind=kind, turns=turns, context_tokens=context_tokens)


def snapshot(*sessions: SessionSummary) -> SessionsSnapshot:
    return SessionsSnapshot(sessions=tuple(sessions))


def logsnap(*lines: str, present: bool = True) -> LogSnapshot:
    return LogSnapshot(present=present, lines=tuple(lines))


FIXED_TS = dt.datetime(2026, 7, 22, 12, 0, 0, tzinfo=dt.timezone.utc)


def evaluator(*, enabled: bool = True, service=None, clock=None) -> StaticEvaluator:
    # run_metrics/build_report do no filesystem access, so fixed dummy paths are
    # fine; a fixed clock keeps a report's generated_ts deterministic.
    return StaticEvaluator(
        Config(
            ai_native_path=Path("/ai-native"),
            data_dir=Path("/hub-data"),
            evaluation_enabled=enabled,
        ),
        evaluation_service=service,
        clock=clock or (lambda: FIXED_TS),
    )


# -- empty / absent -----------------------------------------------------------


def test_empty_pack_has_zero_counts():
    m = evaluator().run_metrics(pack())

    assert m.run_count == 0
    assert m.live_count == m.clean_count == m.failed_count == 0
    assert m.stopped_count == m.ended_unknown_count == m.broken_count == 0
    assert m.failure_count == 0 and m.failed_without_diagnostic_count == 0
    assert m.human_input_count == 0 and m.total_turns == 0
    assert m.dev_session_count == m.review_session_count == m.plan_gate_count == 0
    assert m.peak_context_tokens is None
    assert m.total_duration_seconds is None  # "unknown", not 0
    assert m.warning_count == 0
    assert m.runs == ()


def test_absent_target_still_reports():
    m = evaluator().run_metrics(pack(tgt=target(present=False)))

    assert m.target_present is False
    assert m.target_source is None


def test_archive_state_from_target():
    m = evaluator().run_metrics(pack(tgt=target(source=SOURCE_ARCHIVED)))

    assert m.target_source == SOURCE_ARCHIVED  # close-out moved the file to archive


# -- per-run outcome classification (evidence-only) ---------------------------


def test_clean_run():
    r = evaluator().run_metrics(pack(run_ev(ended_ts=END, exit_code=0))).runs[0]

    assert r.outcome == RUN_OUTCOME_CLEAN
    assert r.ended is True and r.exit_code == 0 and r.stop_requested is False


def test_failed_run():
    m = evaluator().run_metrics(pack(run_ev(ended_ts=END, exit_code=2)))

    assert m.runs[0].outcome == RUN_OUTCOME_FAILED
    assert m.failed_count == 1 and m.failure_count == 1


def test_live_run_has_no_ended_ts():
    r = evaluator().run_metrics(pack(run_ev(started_ts=START))).runs[0]

    assert r.outcome == RUN_OUTCOME_LIVE
    assert r.ended is False


def test_ended_unknown_when_no_exit_code():
    # Ended while the hub was down: ended_ts set, exit_code unrecoverable.
    r = evaluator().run_metrics(pack(run_ev(ended_ts=END, exit_code=None))).runs[0]

    assert r.outcome == RUN_OUTCOME_ENDED_UNKNOWN


def test_stop_request_outranks_exit_code():
    # A non-zero exit that would otherwise be "failed" is a graceful/aborted stop
    # when a stop_requested audit event is present.
    m = evaluator().run_metrics(
        pack(run_ev(ended_ts=END, exit_code=1, events=[event(KIND_STOP_REQUESTED)]))
    )

    assert m.runs[0].outcome == RUN_OUTCOME_STOPPED
    assert m.runs[0].stop_requested is True
    assert m.stopped_count == 1 and m.failed_count == 0 and m.failure_count == 0


def test_broken_record_is_defensive_outcome():
    # related_runs excludes error stubs, so a real pack never carries one; the
    # computation must still classify a hand-built broken record, not crash.
    m = evaluator().run_metrics(pack(run_ev(error="unreadable run.json")))

    assert m.runs[0].outcome == RUN_OUTCOME_BROKEN
    assert m.runs[0].ended is False  # ended is raw ended_ts presence, not outcome
    assert m.broken_count == 1 and m.failure_count == 1


# -- session / turn / context rollups -----------------------------------------


def test_session_counts_by_role_and_kind():
    m = evaluator().run_metrics(
        pack(
            run_ev(
                sessions=snapshot(
                    session(DEV_ROLE, PLAN_GATE_KIND),
                    session(DEV_ROLE, SESSION_KIND),
                    session(DEV_ROLE, SESSION_KIND),
                    session(REVIEW_ROLE, SESSION_KIND),
                )
            )
        )
    )

    assert m.dev_session_count == 2  # kind == "session", role dev
    assert m.review_session_count == 1
    assert m.plan_gate_count == 1  # counted by kind, separate from dev sessions


def test_missing_session_snapshot_is_zero_counts():
    # A live/pre-field run persisted no sessions.json -> RunEvidence.sessions is
    # None; counts degrade to zero, not a crash.
    r = evaluator().run_metrics(pack(run_ev(sessions=None))).runs[0]

    assert r.dev_session_count == r.review_session_count == r.plan_gate_count == 0
    assert r.total_turns == 0 and r.peak_context_tokens is None


def test_total_turns_summed_across_sessions_and_runs():
    m = evaluator().run_metrics(
        pack(
            run_ev("r1", sessions=snapshot(session(turns=2), session(turns=3))),
            run_ev("r2", sessions=snapshot(session(turns=4))),
        )
    )

    assert m.runs[0].total_turns == 5
    assert m.total_turns == 9


def test_peak_context_is_max_across_sessions_and_runs():
    m = evaluator().run_metrics(
        pack(
            run_ev(
                "r1",
                sessions=snapshot(
                    session(context_tokens=1000),
                    session(context_tokens=None),  # no reported context: ignored
                    session(context_tokens=50000),
                ),
            ),
            run_ev("r2", sessions=snapshot(session(context_tokens=30000))),
        )
    )

    assert m.runs[0].peak_context_tokens == 50000
    assert m.runs[1].peak_context_tokens == 30000
    assert m.peak_context_tokens == 50000  # max across runs


# -- human input --------------------------------------------------------------


def test_human_input_counts_only_answer_events():
    m = evaluator().run_metrics(
        pack(
            run_ev(
                "r1",
                events=[
                    event(KIND_RUN_STARTED),  # ignored
                    event(KIND_ANSWER_RECORDED, seq=1),
                    event(KIND_ANSWER_RECORDED, seq=2),
                    event(KIND_STOP_REQUESTED),  # ignored
                ],
            ),
            run_ev("r2", events=[event(KIND_ANSWER_RECORDED, seq=1)]),
        )
    )

    assert m.runs[0].answer_count == 2
    assert m.human_input_count == 3


# -- duration windows ---------------------------------------------------------


def test_duration_seconds_and_total():
    m = evaluator().run_metrics(
        pack(
            run_ev("r1", started_ts=START, ended_ts=END),  # 1800s
            run_ev("r2", started_ts=START, ended_ts="2026-07-22T09:10:00+00:00"),  # 600s
        )
    )

    assert m.runs[0].duration_seconds == 1800.0
    assert m.runs[1].duration_seconds == 600.0
    assert m.total_duration_seconds == 2400.0


def test_duration_none_when_unmeasurable():
    m = evaluator().run_metrics(
        pack(
            run_ev("r1", started_ts=START),  # no end
            run_ev("r2", started_ts="not-a-ts", ended_ts=END),  # malformed start
            run_ev("r3", started_ts=END, ended_ts=START),  # end before start
        )
    )

    assert all(r.duration_seconds is None for r in m.runs)
    assert m.total_duration_seconds is None  # no run contributed a duration


# -- diagnostics availability -------------------------------------------------


def test_failed_without_diagnostic_count():
    m = evaluator().run_metrics(
        pack(
            run_ev("f1", ended_ts=END, exit_code=1),  # failed, no diag -> counted
            run_ev("f2", ended_ts=END, exit_code=1, spawn_out=logsnap("boom")),  # diag -> not
            run_ev("f3", ended_ts=END, exit_code=1, log=logsnap(present=True)),  # empty log
            run_ev("c1", ended_ts=END, exit_code=0),  # clean, no diag -> not a failure
        )
    )

    assert m.failed_count == 3
    # f1 (nothing) and f3 (present but empty log) lack usable diagnostics; f2 has
    # a spawn.out; c1 is not a failure.
    assert m.failed_without_diagnostic_count == 2
    by_id = {r.run_id: r for r in m.runs}
    assert by_id["f2"].has_diagnostic is True
    assert by_id["f1"].has_diagnostic is False
    assert by_id["f3"].has_diagnostic is False


def test_log_lines_count_as_diagnostic():
    r = evaluator().run_metrics(
        pack(run_ev(ended_ts=END, exit_code=1, log=logsnap("[09:00] traceback")))
    ).runs[0]

    assert r.has_diagnostic is True
    # A failure with a diagnostic is not flagged.
    m = evaluator().run_metrics(
        pack(run_ev(ended_ts=END, exit_code=1, log=logsnap("[09:00] traceback")))
    )
    assert m.failed_without_diagnostic_count == 0


# -- warnings -----------------------------------------------------------------


def test_warning_count_aggregates_pack_and_run_warnings():
    m = evaluator().run_metrics(
        pack(
            run_ev("r1", warnings=["corrupt history"]),
            run_ev("r2", warnings=["log unreadable", "session snapshot unreadable"]),
            warnings=["pack-level warning"],
        )
    )

    assert m.runs[0].warning_count == 1
    assert m.runs[1].warning_count == 2
    assert m.warning_count == 4  # 1 pack + 3 run


# -- multi-run partition ------------------------------------------------------


def test_multi_run_outcome_partition_and_failure_count():
    m = evaluator().run_metrics(
        pack(
            run_ev("a", ended_ts=END, exit_code=0),  # clean
            run_ev("b", ended_ts=END, exit_code=1),  # failed
            run_ev("c", started_ts=START),  # live
            run_ev("d", ended_ts=END, exit_code=1, events=[event(KIND_STOP_REQUESTED)]),  # stopped
            run_ev("e", ended_ts=END, exit_code=None),  # ended_unknown
            run_ev("f", error="broken"),  # broken (defensive)
        )
    )

    assert m.run_count == 6
    # Every run lands in exactly one bucket.
    assert (
        m.live_count
        + m.clean_count
        + m.failed_count
        + m.stopped_count
        + m.ended_unknown_count
        + m.broken_count
    ) == m.run_count
    assert (m.clean_count, m.failed_count, m.live_count) == (1, 1, 1)
    assert (m.stopped_count, m.ended_unknown_count, m.broken_count) == (1, 1, 1)
    assert m.failure_count == 2  # failed + broken


# -- evaluation-id tie + serialization ----------------------------------------


def test_metrics_tie_to_evaluation_id():
    m = evaluator().run_metrics(pack(evaluation_id="20260722T090000Z"))

    assert m.evaluation_id == "20260722T090000Z"


def test_to_dict_is_json_serializable_and_stable():
    m = evaluator().run_metrics(
        pack(
            run_ev(
                "r1",
                task_status="in_progress",
                started_ts=START,
                ended_ts=END,
                exit_code=0,
                events=[event(KIND_ANSWER_RECORDED, seq=1)],
                sessions=snapshot(session(turns=3, context_tokens=50000)),
                log=logsnap("hello"),
            )
        )
    )

    data = m.to_dict()
    # Round-trips through JSON (deterministic, no exotic types).
    assert json.loads(json.dumps(data)) == data
    # Derived convenience key is serialized.
    assert data["failure_count"] == 0 and data["target_source"] == SOURCE_ACTIVE
    assert data["run_count"] == 1 and data["evaluation_id"] == "e1"
    run = data["runs"][0]
    assert run["run_id"] == "r1" and run["outcome"] == RUN_OUTCOME_CLEAN
    assert run["duration_seconds"] == 1800.0 and run["has_diagnostic"] is True
    assert run["task_status_at_launch"] == "in_progress"


# -- disabled-mode gate -------------------------------------------------------


def test_enabled_flag_reports_config():
    assert evaluator(enabled=True).enabled is True
    assert evaluator(enabled=False).enabled is False


def test_disabled_evaluator_refuses_run_metrics():
    p = pack(run_ev(ended_ts=END, exit_code=0))

    with pytest.raises(EvaluationDisabledError):
        evaluator(enabled=False).run_metrics(p)


# -- integration: metrics over a pack built by EvaluationService --------------


def _fake_git(args, **kwargs):
    return CommandResult(tuple(str(a) for a in args), 128, "", "")  # unavailable


def _write_repo_and_run(config: Config, tmp_path: Path) -> ManagedRepo:
    repo_path = tmp_path / "repos" / "alpha"
    (repo_path / ".ai-tasks" / "archive").mkdir(parents=True)
    (repo_path / ".ai-tasks" / f"{TASK}.md").write_text(
        f"---\nid: {TASK}\nstatus: completed\n---\n\n# body\n", encoding="utf-8"
    )
    repo = ManagedRepo(
        name="alpha", path=repo_path, manifest=repo_path / ".ai-deploy-manifest.json"
    )
    run_id = "20260722T090000Z-alpha-task"
    run_dir = config.data_dir / "runs" / run_id
    (run_dir / "control").mkdir(parents=True)
    log_dir = repo_path / ".cursor" / "orchestrator" / "logs"
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "repo_name": "alpha",
                "repo_path": str(repo_path),
                "task_id": TASK,
                "task_status": "in_progress",
                "started_ts": START,
                "ended_ts": END,
                "exit_code": 0,
                "log_dir": str(log_dir),
                "spawn_out": str(run_dir / "spawn.out"),
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        json.dumps({"ts": "t", "kind": KIND_RUN_STARTED, "run_id": run_id})
        + "\n"
        + json.dumps({"ts": "t", "kind": KIND_ANSWER_RECORDED, "run_id": run_id, "seq": 1})
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "sessions.json").write_text(
        json.dumps(
            {
                "sessions": [
                    {"role": "dev", "kind": "plan-gate", "turns": 1, "context_tokens": 1000},
                    {"role": "dev", "kind": "session", "turns": 3, "context_tokens": 50000},
                    {"role": "review", "kind": "session", "turns": 1, "context_tokens": 20000},
                ],
                "context_budget": 200000,
            }
        ),
        encoding="utf-8",
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromisoformat(START).strftime("%Y%m%dT%H%M%SZ")
    (log_dir / f"{stamp}-{TASK}.log").write_text("[09:00:00] hello\n", encoding="utf-8")
    return repo


def test_run_metrics_over_built_pack(tmp_path):
    config = Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=True,
    )
    repo = _write_repo_and_run(config, tmp_path)
    built = EvaluationService(
        config,
        git_runner=_fake_git,
        clock=lambda: dt.datetime(2026, 7, 22, 9, 0, 0, tzinfo=dt.timezone.utc),
    ).build_evidence(repo, TASK, evaluation_id="e1")

    m = StaticEvaluator(config).run_metrics(built)

    assert isinstance(m, RunMetrics)
    assert m.evaluation_id == "e1"
    assert m.run_count == 1 and m.clean_count == 1
    assert m.target_source == SOURCE_ACTIVE  # active file present, not archived
    assert m.dev_session_count == 1 and m.review_session_count == 1
    assert m.plan_gate_count == 1
    assert m.total_turns == 5 and m.peak_context_tokens == 50000
    assert m.human_input_count == 1
    assert m.total_duration_seconds == 1800.0
    r = m.runs[0]
    assert r.outcome == RUN_OUTCOME_CLEAN and r.has_diagnostic is True  # orchestrator log


# =============================================================================
# session-2: taskfile/session-log protocol metrics, score components, persistence
# =============================================================================


# -- task-file builders (pure strings) ----------------------------------------

# A well-formed completed task: legal transition path pending -> in_progress ->
# final_review -> completed, every dev entry reviewed, final Open: none.
COMPLETED = """---
id: 2026-07-22-task-static-quality-metrics
status: completed
session-est: 2/2
blockers: []
prefetch: [.ai/modules/core.md, .ai/glossary.md]
claimed-by: sid-b@2026-07-22T10:00:00Z
---

# Static task quality metrics

## Goal
Compute metrics.

## Scope
- one

## Acceptance
- done

## Session plan
### session-1
Scope:
- a

## Session log

### 2026-07-22 / sid-a / (pending → in_progress)
- Done: first slice
- Next: session-2
- Open: nothing

### 2026-07-22 / sid-r1 / review of sid-a / (in_progress → in_progress)
- Verdict: pass
- Group: sid-a
- Findings: none

### 2026-07-22 / sid-b / (in_progress → final_review)
- Done: finished scope
- Next: none
- Open: none

### 2026-07-22 / sid-r2 / review of sid-b / (final_review → completed)
- Verdict: pass
- Group: sid-b
- Findings: none
- Open: none
"""


def mk_target(content, *, source=SOURCE_ACTIVE, present=True, task_id=TASK):
    return EvaluationTarget(
        repo_name="alpha",
        repo_path=REPO_PATH,
        task_id=task_id,
        source=source if present else None,
        present=present,
        content=content if present else None,
    )


def report_of(content, *runs, source=SOURCE_ACTIVE, warnings=(), present=True):
    p = pack(*runs, tgt=mk_target(content, source=source, present=present), warnings=warnings)
    return evaluator().build_report(p)


def score(report, name):
    return next(s for s in report.scores if s.name == name)


# -- protocol facts (parsed from the task file) -------------------------------


def test_protocol_facts_on_clean_completed_task():
    pm = report_of(COMPLETED, run_ev(ended_ts=END, exit_code=0), source=SOURCE_ARCHIVED).protocol

    assert pm.task_content_present and pm.frontmatter_present
    assert pm.status_legal and pm.id_matches and pm.completed
    assert pm.session_est == SessionEst(current=2, total=2)
    assert pm.session_est_wellformed and pm.prefetch_wellformed
    assert pm.missing_sections == () and pm.has_session_plan
    assert pm.entry_count == 4
    assert pm.work_entry_count == 2 and pm.review_entry_count == 2
    assert pm.malformed_entry_count == 0 and pm.incomplete_entries == ()
    assert pm.uncovered_work_entries == ()  # both dev entries reviewed
    assert pm.pass_verdict_count == 2 and pm.changes_requested_count == 0
    assert pm.remediation_count == 0 and pm.blocked_entry_count == 0
    assert pm.illegal_transitions == () and pm.transition_continuity_ok
    assert pm.final_status_matches and pm.final_open_none


def test_clean_completed_task_scores_all_perfect():
    report = report_of(
        COMPLETED, run_ev(ended_ts=END, exit_code=0), source=SOURCE_ARCHIVED
    )

    assert {s.name for s in report.scores} == {
        SCORE_PROTOCOL, SCORE_RELIABILITY, SCORE_HANDOFF, SCORE_ESTIMATE,
        SCORE_KNOWLEDGE, SCORE_PREFETCH_READS,
    }
    # The task-file/run axes are clean. Prefetch reading is a different kind of
    # axis: this pack carries no read evidence, so it is *not assessable* rather
    # than perfect — which is the distinction the axis exists to keep.
    for s in report.scores:
        if s.name == SCORE_PREFETCH_READS:
            assert s.score is None and s.confidence == 0.0 and s.warnings
            continue
        assert s.score == 1.0, s.name
        assert s.confidence == 1.0 and s.warnings == ()


def test_ascii_arrow_transition_also_parses():
    # Robustness: the protocol writes `→` but an ASCII `->` must not read as a
    # malformed heading.
    ascii_log = COMPLETED.replace("→", "->")
    pm = report_of(ascii_log, source=SOURCE_ARCHIVED).protocol

    assert pm.malformed_entry_count == 0
    assert pm.illegal_transitions == () and pm.final_status_matches


# -- protocol score: frontmatter / sections / transitions ---------------------


def test_protocol_penalizes_illegal_status_and_missing_sections_and_fixset():
    bad = """---
id: wrong-id
status: frozen
session-est: 2
fix-set: open
---

# Bad

## Session log

### 2026-07-22 / s1 / (pending → frozen)
- Done: x
- Next: y
- Open: z
"""
    report = report_of(bad)
    pm = report.protocol
    assert not pm.status_legal and not pm.id_matches
    assert not pm.session_est_wellformed and pm.fix_set_present
    assert set(pm.missing_sections) == {"## Goal", "## Scope", "## Acceptance"}
    assert pm.illegal_transitions == ("pending -> frozen",)

    sp = score(report, SCORE_PROTOCOL)
    assert sp.score == 0.0  # many penalties floor the axis
    joined = " ".join(sp.warnings)
    assert "frozen" in joined and "fix-set" in joined
    assert "## Goal" in joined and "session-est" in joined


def test_protocol_flags_completed_task_with_open_not_none():
    # A completed task whose final Open is not `none` is a protocol slip.
    leftover = COMPLETED.replace("- Open: none\n", "- Open: revisit caching later\n")
    report = report_of(leftover, source=SOURCE_ARCHIVED)

    assert report.protocol.final_open_none is False
    sp = score(report, SCORE_PROTOCOL)
    assert sp.score < 1.0
    assert any("final Open" in w for w in sp.warnings)


def test_protocol_flags_discontinuous_transition_path():
    # Second entry starts from final_review though the first ended in_progress.
    broken = """---
id: 2026-07-22-task-static-quality-metrics
status: in_progress
session-est: 1/1
blockers: []
---
## Goal
g
## Scope
s
## Acceptance
a
## Session log

### 2026-07-22 / d1 / (pending → in_progress)
- Done: x
- Next: y
- Open: z

### 2026-07-22 / d2 / (final_review → in_progress)
- Done: x
- Next: y
- Open: z
"""
    report = report_of(broken)
    assert report.protocol.transition_continuity_ok is False
    sp = score(report, SCORE_PROTOCOL)
    assert any("not continuous" in w for w in sp.warnings)


def test_blocked_task_needs_a_blocker():
    blocked_no_blocker = """---
id: 2026-07-22-task-static-quality-metrics
status: blocked
session-est: 1/1
blockers: []
---
## Goal
g
## Scope
s
## Acceptance
a
## Session log

### 2026-07-22 / d1 / (in_progress → blocked)
- Done: x
- Next: y
- Open: waiting on decision
"""
    report = report_of(blocked_no_blocker)
    assert report.protocol.blocker_rule_ok is False
    assert report.protocol.blocked_entry_count == 1
    assert any("blocker rule" in w for w in score(report, SCORE_PROTOCOL).warnings)


# -- protocol score: verdict-aware final-gate transitions ---------------------

# A task at final_review after a final-gate review requested changes: the review
# entry legally leaves status at final_review (findings never downgrade a status;
# review contract §6), so the transition is verdict-consistent, not illegal.
FINAL_REVIEW_CHANGES = """---
id: 2026-07-22-task-static-quality-metrics
status: final_review
session-est: 1/1
blockers: []
---
## Goal
g
## Scope
s
## Acceptance
a
## Session log

### 2026-07-22 / d1 / (pending → in_progress)
- Done: first slice
- Next: finish scope
- Open: none

### 2026-07-22 / d2 / (in_progress → final_review)
- Done: finished scope
- Next: none
- Open: none

### 2026-07-22 / r1 / review of d2 / (final_review → final_review)
- Verdict: changes-requested
- Group: d2
- Findings: fix it
- Next: address the finding
- Open: none
"""


def test_protocol_changes_requested_final_review_holds_status_is_legal():
    # Verdict-aware positive: a changes-requested final gate that correctly leaves
    # status at final_review is consistent, not an illegal transition.
    report = report_of(FINAL_REVIEW_CHANGES)
    pm = report.protocol
    assert pm.changes_requested_count == 1 and pm.final_status_matches
    assert pm.illegal_transitions == ()
    assert not any(
        "illegal status transition" in w
        for w in score(report, SCORE_PROTOCOL).warnings
    )


def test_protocol_flags_changes_requested_review_reaching_completed():
    # The false-negative: the taskfile table ties the final-gate forward edge to
    # the verdict, so a review with Verdict: changes-requested recorded as
    # (final_review -> completed) is illegal even though the raw status pair is
    # structurally legal. A pair-only check passed this with a perfect score.
    bad = FINAL_REVIEW_CHANGES.replace(
        "status: final_review", "status: completed"
    ).replace(
        "review of d2 / (final_review → final_review)",
        "review of d2 / (final_review → completed)",
    )
    report = report_of(bad, source=SOURCE_ARCHIVED)
    assert "final_review -> completed" in report.protocol.illegal_transitions
    sp = score(report, SCORE_PROTOCOL)
    assert sp.score < 1.0
    assert any("illegal status transition" in w for w in sp.warnings)


def test_protocol_flags_pass_final_review_that_does_not_complete():
    # Corollary of the same rule: a final-gate `pass` must complete; recording it
    # as (final_review -> final_review) is verdict-inconsistent and illegal.
    held = FINAL_REVIEW_CHANGES.replace(
        "- Verdict: changes-requested", "- Verdict: pass"
    )
    pm = report_of(held).protocol
    assert "final_review -> final_review" in pm.illegal_transitions


def test_protocol_pass_final_review_reaching_completed_stays_legal():
    # Guard against over-correction: the normal pass path (final_review ->
    # completed with Verdict: pass) must remain legal.
    assert report_of(COMPLETED, source=SOURCE_ARCHIVED).protocol.illegal_transitions == ()


# -- protocol score: the collapsed first-session edge --------------------------

# A task finished in its first dev session: the session found the task `pending`,
# performed the mechanical claim, and completed the whole scope, so its entry
# records the status it found. The final gate then passes it.
SINGLE_SESSION = """---
id: 2026-07-22-task-static-quality-metrics
status: completed
session-est: 1/1
blockers: []
prefetch: [.ai/modules/core.md]
claimed-by: sid-a@2026-07-22T10:00:00Z
---

# One-session task

## Goal
Compute metrics.

## Scope
- one

## Acceptance
- done

## Session log

### 2026-07-22 / sid-a / (pending → final_review)
- Done: whole scope
- Next: none
- Open: none

### 2026-07-22 / sid-r1 / review of sid-a / (final_review → completed)
- Verdict: pass
- Group: sid-a
- Findings: none
- Open: none
"""


def test_protocol_dev_pending_to_final_review_is_legal():
    # The false positive: a task completed in its first session is the best case,
    # not a skipped transition. The claim (pending -> in_progress) is mechanical
    # bookkeeping, never an observable state of its own, so the collapsed edge is
    # the honest record — and must cost nothing.
    report = report_of(SINGLE_SESSION, source=SOURCE_ARCHIVED)
    pm = report.protocol

    assert pm.illegal_transitions == ()
    assert pm.transition_continuity_ok and pm.final_status_matches
    sp = score(report, SCORE_PROTOCOL)
    assert sp.score == 1.0 and sp.warnings == ()
    # ...and the one advancement session fits its budget of one.
    assert pm.advancement_entry_count == 1
    assert score(report, SCORE_ESTIMATE).score == 1.0


def test_protocol_review_pending_to_final_review_stays_illegal():
    # Entry-kind-aware, not blanket: no review session may claim a pending task,
    # so the same pair from a review entry can only mean a lost dev entry.
    review_claimed = SINGLE_SESSION.replace(
        "### 2026-07-22 / sid-a / (pending → final_review)",
        "### 2026-07-22 / sid-a / review of sid-z / (pending → final_review)",
    ).replace("- Done: whole scope", "- Verdict: pass\n- Group: sid-z\n- Findings: none")
    pm = report_of(review_claimed, source=SOURCE_ARCHIVED).protocol

    assert "pending -> final_review" in pm.illegal_transitions


def test_protocol_final_open_none_reads_the_final_entry():
    # `final_open_none` reflects the FINAL session-log entry only. A completed task
    # whose final (review) entry omits Open must NOT be rescued by an earlier work
    # entry's `Open: none`.
    no_final_open = COMPLETED.replace(
        "- Verdict: pass\n- Group: sid-b\n- Findings: none\n- Open: none\n",
        "- Verdict: pass\n- Group: sid-b\n- Findings: none\n",
    )
    # The earlier work entry sid-b still carries `Open: none`; only the final
    # review entry's Open was dropped (the old walk-to-last-Open would be rescued).
    assert "- Open: none" in no_final_open
    report = report_of(no_final_open, source=SOURCE_ARCHIVED)
    assert report.protocol.final_open_none is False
    sp = score(report, SCORE_PROTOCOL)
    assert sp.score < 1.0
    assert any("final Open" in w for w in sp.warnings)


# -- reliability score --------------------------------------------------------


def test_reliability_drops_with_failures_and_missing_diagnostics():
    report = report_of(
        COMPLETED,
        run_ev("a", ended_ts=END, exit_code=0),  # clean
        run_ev("b", ended_ts=END, exit_code=1),  # failed, no diagnostic
        source=SOURCE_ARCHIVED,
    )
    sr = score(report, SCORE_RELIABILITY)
    # 1 of 2 failed -> 0.5, minus the no-diagnostic penalty (0.2) -> 0.3.
    assert sr.score == 0.3
    joined = " ".join(sr.warnings)
    assert "1 of 2" in joined and "diagnostic" in joined


def test_reliability_not_assessable_without_runs():
    sr = score(report_of(COMPLETED, source=SOURCE_ARCHIVED), SCORE_RELIABILITY)

    assert sr.score is None and sr.confidence == 0.0
    assert any("no related runs" in w for w in sr.warnings)


def test_reliability_confidence_drops_with_evidence_warnings():
    report = report_of(
        COMPLETED,
        run_ev(ended_ts=END, exit_code=0, warnings=["log unreadable"]),
        warnings=["pack warning a", "pack warning b"],
        source=SOURCE_ARCHIVED,
    )
    sr = score(report, SCORE_RELIABILITY)
    # No failures -> score stays 1.0, but 3 evidence warnings lower confidence.
    assert sr.score == 1.0
    assert sr.confidence < 1.0


# -- handoff score ------------------------------------------------------------


def test_handoff_penalizes_malformed_and_incomplete_entries():
    messy = """---
id: 2026-07-22-task-static-quality-metrics
status: in_progress
session-est: 1/1
blockers: []
---
## Goal
g
## Scope
s
## Acceptance
a
## Session log

### not a real heading
- Done: x

### 2026-07-22 / d2 / (pending → in_progress)
- Done: only done, no Next/Open
"""
    report = report_of(messy)
    pm = report.protocol
    assert pm.malformed_entry_count == 1
    assert pm.incomplete_entries == ("d2",)
    sh = score(report, SCORE_HANDOFF)
    assert sh.score < 1.0
    joined = " ".join(sh.warnings)
    assert "malformed" in joined and "missing required fields" in joined


def test_handoff_uncovered_review_only_penalized_once_completed():
    # d2 is not reviewed. While in_progress this is legitimate (awaiting review);
    # once completed it is a coverage gap.
    two_dev_one_review = """---
id: 2026-07-22-task-static-quality-metrics
status: {status}
session-est: 2/2
blockers: []
---
## Goal
g
## Scope
s
## Acceptance
a
## Session log

### 2026-07-22 / d1 / (pending → in_progress)
- Done: x
- Next: y
- Open: z

### 2026-07-22 / r1 / review of d1 / (in_progress → in_progress)
- Verdict: pass
- Group: d1
- Findings: none

### 2026-07-22 / d2 / (in_progress → {end})
- Done: x
- Next: none
- Open: none
"""
    in_progress = report_of(two_dev_one_review.format(status="in_progress", end="in_progress"))
    assert in_progress.protocol.uncovered_work_entries == ("d2",)
    assert score(in_progress, SCORE_HANDOFF).score == 1.0  # not penalized mid-flight

    done = report_of(
        two_dev_one_review.format(status="completed", end="completed"),
        source=SOURCE_ARCHIVED,
    )
    assert done.protocol.uncovered_work_entries == ("d2",)
    sh = score(done, SCORE_HANDOFF)
    assert sh.score < 1.0
    assert any("no review entry" in w for w in sh.warnings)


def test_handoff_flags_unresolved_disputes():
    disputed = COMPLETED.replace(
        "- Findings: none\n\n### 2026-07-22 / sid-b",
        "- Findings: bad\n- Dispute-unresolved: I still hold X is wrong\n\n### 2026-07-22 / sid-b",
    )
    report = report_of(disputed, source=SOURCE_ARCHIVED)
    assert report.protocol.dispute_unresolved_count == 1
    sh = score(report, SCORE_HANDOFF)
    assert sh.score == 0.8  # single dispute penalty
    assert any("disputed" in w for w in sh.warnings)


# -- estimate score (planned vs actual dev sessions) --------------------------


def _est_log(*entries: str, est: str = "1/1") -> str:
    """A minimal in-progress task file carrying `est` and the given log entries."""
    return f"""---
id: 2026-07-22-task-static-quality-metrics
status: in_progress
session-est: {est}
blockers: []
---
## Goal
g
## Scope
s
## Acceptance
a
## Session log
""" + "".join(entries)


_DEV = """
### 2026-07-22 / {sid} / ({before} → {after})
- Done: x
- Next: y
- Open: z
"""
_REVIEW = """
### 2026-07-22 / {sid} / review of {of} / ({before} → {after})
- Verdict: {verdict}
- Group: {of}
- Findings: f
"""


def test_estimate_excludes_remediation_from_the_actual_count():
    # One advancement session plus a changes-requested round it then fixed. The
    # remediation session is dev work, but it converges on review findings rather
    # than advancing planned scope, so it must not read as an estimate overrun.
    log = _est_log(
        _DEV.format(sid="d1", before="pending", after="in_progress"),
        _REVIEW.format(
            sid="r1", of="d1", before="in_progress", after="in_progress",
            verdict="changes-requested",
        ),
        _DEV.format(sid="d2", before="in_progress", after="in_progress"),
    )
    report = report_of(log)
    pm = report.protocol

    assert pm.work_entry_count == 2 and pm.changes_requested_count == 1
    assert pm.remediation_count == 1 and pm.advancement_entry_count == 1
    se = score(report, SCORE_ESTIMATE)
    assert se.score == 1.0  # planned 1 / 1 advancement session
    joined = " ".join(se.warnings)
    assert "exceeded" not in joined  # no overrun...
    assert "1 remediation round(s)" in joined  # ...but the round stays visible


def test_estimate_remediation_chain_split_across_sessions_is_not_an_overrun():
    # A fix set too large for one session: both dev sessions after the
    # changes-requested verdict are remediation, because what ends a convergence
    # chain is the next review, not the next session. Splitting a fix must not
    # cost estimate score.
    log = _est_log(
        _DEV.format(sid="d1", before="pending", after="in_progress"),
        _REVIEW.format(
            sid="r1", of="d1", before="in_progress", after="in_progress",
            verdict="changes-requested",
        ),
        _DEV.format(sid="d2", before="in_progress", after="in_progress"),
        _DEV.format(sid="d3", before="in_progress", after="in_progress"),
    )
    report = report_of(log)
    pm = report.protocol

    assert pm.work_entry_count == 3
    assert pm.remediation_count == 2 and pm.advancement_entry_count == 1
    se = score(report, SCORE_ESTIMATE)
    assert se.score == 1.0
    assert any("2 remediation round(s)" in w for w in se.warnings)


def test_estimate_still_penalizes_advancement_overrun():
    # Guard against over-correction: two *advancement* sessions against a total of
    # one keep the existing proportional overrun, with no remediation involved.
    log = _est_log(
        _DEV.format(sid="d1", before="pending", after="in_progress"),
        _REVIEW.format(
            sid="r1", of="d1", before="in_progress", after="in_progress",
            verdict="pass",
        ),
        _DEV.format(sid="d2", before="in_progress", after="in_progress"),
    )
    report = report_of(log)
    pm = report.protocol

    assert pm.remediation_count == 0 and pm.advancement_entry_count == 2
    se = score(report, SCORE_ESTIMATE)
    assert se.score == 0.5  # planned 1 / actual 2 advancement sessions
    joined = " ".join(se.warnings)
    assert "exceeded" in joined and "remediation" not in joined


def test_estimate_within_budget_is_perfect():
    se = score(report_of(COMPLETED, source=SOURCE_ARCHIVED), SCORE_ESTIMATE)

    assert se.score == 1.0 and se.warnings == ()  # 2 dev sessions == est total 2


def test_estimate_not_assessable_without_session_est():
    no_est = COMPLETED.replace("session-est: 2/2\n", "")
    se = score(report_of(no_est, source=SOURCE_ARCHIVED), SCORE_ESTIMATE)

    assert se.score is None and se.confidence == 0.0


# -- knowledge score (close-out / prefetch / repeated warnings) ---------------


def test_knowledge_flags_completed_task_not_archived():
    # A completed task still resolving as an *active* file means close-out did
    # not move it to archive.
    still_active = report_of(COMPLETED, source=SOURCE_ACTIVE)
    sk = score(still_active, SCORE_KNOWLEDGE)
    assert sk.score == 0.6  # -0.4 close-out
    assert any("not archived" in w for w in sk.warnings)

    # The same task, archived, is clean.
    archived = report_of(COMPLETED, source=SOURCE_ARCHIVED)
    assert score(archived, SCORE_KNOWLEDGE).score == 1.0


def test_knowledge_flags_malformed_prefetch():
    bad_prefetch = COMPLETED.replace(
        "prefetch: [.ai/modules/core.md, .ai/glossary.md]", "prefetch: [notes.txt]"
    )
    report = report_of(bad_prefetch, source=SOURCE_ARCHIVED)
    assert report.protocol.prefetch_wellformed is False
    sk = score(report, SCORE_KNOWLEDGE)
    assert any("prefetch" in w for w in sk.warnings)


def test_knowledge_flags_repeated_evidence_warnings():
    report = report_of(
        COMPLETED,
        run_ev("a", ended_ts=END, exit_code=0, warnings=["w1", "w2"]),
        warnings=["p1", "p2"],  # 4 total >= threshold 3
        source=SOURCE_ARCHIVED,
    )
    sk = score(report, SCORE_KNOWLEDGE)
    assert any("missing-evidence" in w for w in sk.warnings)


# -- prefetch-set read completeness -------------------------------------------
# The read side's deterministic axis. Its whole point is the difference between
# a session that did not read what it declared and one whose reading could not
# be measured, so most of these fix the score and vary only the evidence.


_AUTO = object()
"""Sentinel: derive `applicable` rather than state it. Distinct from `None`,
which is the reader's real "applicability unknown" value."""


def session_reads(
    sid,
    *,
    role=DEV_ROLE,
    expected=(),
    read=(),
    assessable=True,
    applicable=_AUTO,
    detail=None,
    undeclared=(),
):
    """One `SessionReads` rollup as the reader would emit it.

    `applicable` defaults to the reader's own rule (a dev session that declared a
    non-empty set) so a test only states it when contradicting that.
    """
    docs = tuple(PrefetchDoc(path=p, read=p in read) for p in expected)
    if applicable is _AUTO:
        applicable = bool(expected) and role == DEV_ROLE
    found = sum(1 for d in docs if d.read)
    scored = assessable and applicable
    return SessionReads(
        session_id=sid,
        role=role,
        kind=SESSION_KIND,
        available=True,
        assessable=assessable,
        applicable=applicable,
        expected=tuple(expected),
        docs=docs,
        coverage=(found / len(expected)) if scored else None,
        complete=(found == len(expected)) if scored else None,
        trace=tuple(
            MemoryRead(path=p, order=i, actor="root", method="read",
                       declared_prefetch=p in expected)
            for i, p in enumerate(tuple(read) + tuple(undeclared))
        ),
        detail=detail,
    )


def reads_report(*sessions, available=True, warnings=(), dropped=0, dropped_candidates=0):
    p = pack(tgt=mk_target(COMPLETED, source=SOURCE_ARCHIVED))
    p = replace(
        p,
        reads=ReadBehavior(
            available=available,
            sessions=tuple(sessions),
            warnings=tuple(warnings),
            dropped_sessions=dropped,
            dropped_candidates=dropped_candidates,
        ),
    )
    return evaluator().build_report(p)


A, B, C = ".ai/modules/core.md", ".ai/glossary.md", ".ai/features/index.md"


def test_prefetch_read_score_is_the_fraction_of_complete_sessions():
    # Four measured sessions, two of which read their whole declared set. The
    # score is that fraction — not the fraction of *documents* read, which would
    # let one thorough session cover for two that read nothing.
    report = reads_report(
        session_reads("s1", expected=(A, B), read=(A, B)),
        session_reads("s2", expected=(A, B), read=(A,)),
        session_reads("s3", expected=(A,), read=()),
        session_reads("s4", expected=(A, B, C), read=(A, B, C)),
    )
    sc = score(report, SCORE_PREFETCH_READS)

    assert sc.score == 0.5 and sc.confidence == 1.0
    assert report.reads.scored_count == 4 and report.reads.complete_count == 2
    # The warning names which session missed which doc — a bare 0.5 is not
    # actionable.
    assert any("s2 missed .ai/glossary.md" in w for w in sc.warnings)
    assert any("s3 missed .ai/modules/core.md" in w for w in sc.warnings)


def test_prefetch_read_rollup_keeps_per_session_coverage():
    report = reads_report(
        session_reads("s1", expected=(A, B), read=(A, B), undeclared=(C,)),
        session_reads("s2", expected=(A, B, C, ".ai/map.md"), read=(A,)),
    )
    by_id = {s.session_id: s for s in report.reads.sessions}

    assert by_id["s1"].coverage == 1.0 and by_id["s1"].complete is True
    assert by_id["s1"].undeclared_read_count == 1
    assert by_id["s2"].coverage == 0.25 and by_id["s2"].complete is False
    assert by_id["s2"].missing == (B, C, ".ai/map.md")
    assert by_id["s2"].expected_count == 4 and by_id["s2"].read_count == 1
    # The ordered trace and its timing anchors are Layer 2 evidence and stay in
    # the pack; the deterministic report must not imply it scored them.
    assert "trace" not in json.dumps(report.reads.to_dict())


def test_unmeasurable_sessions_lower_confidence_not_the_score():
    # One session read its whole set; a second could not be measured at all. The
    # measured result stands, at half confidence — the unmeasured session must
    # not read as one that ignored its memory.
    report = reads_report(
        session_reads("s1", expected=(A, B), read=(A, B)),
        session_reads("s2", expected=(), assessable=False, applicable=None,
                      detail="no session transcript found"),
    )
    sc = score(report, SCORE_PREFETCH_READS)

    assert sc.score == 1.0 and sc.confidence == 0.5
    assert report.reads.scored_count == 1 and report.reads.unknown_applicable_count == 1
    assert any("could not be measured" in w for w in sc.warnings)


def test_sessions_past_the_readers_cap_lower_confidence_not_the_score():
    # Every session the reader looked at read its whole declared set, but its
    # session cap dropped two more dev sessions it never opened. The measured
    # result stands as measured; the claim to have seen the whole task does not.
    report = reads_report(
        session_reads("s1", expected=(A,), read=(A,)),
        session_reads("s2", expected=(A, B), read=(A, B)),
        dropped=3,
        dropped_candidates=2,
        warnings=["read behavior: 3 session(s) dropped past the 2-session cap"],
    )
    sc = score(report, SCORE_PREFETCH_READS)

    assert sc.score == 1.0 and sc.confidence == 0.5  # 2 measured of 4 candidates
    assert report.reads.dropped_candidate_count == 2
    assert report.reads.session_count == 2  # the cap left only these behind
    assert any("could not be measured" in w for w in sc.warnings)


def test_a_dropped_session_with_no_obligation_leaves_confidence_alone():
    # The third dropped session was a review session: outside the measurement, so
    # the cap losing it costs the score nothing to be uncertain about.
    report = reads_report(
        session_reads("s1", expected=(A,), read=(A,)),
        dropped=2,
        dropped_candidates=0,
    )
    sc = score(report, SCORE_PREFETCH_READS)

    assert sc.score == 1.0 and sc.confidence == 1.0


def test_sessions_with_nothing_to_comply_with_are_in_neither_denominator():
    # A review session (the instruction never reaches it) and a dev session that
    # declared no lazy doc: both have no obligation, so neither the score nor its
    # confidence may express an opinion about them.
    report = reads_report(
        session_reads("s1", expected=(A,), read=(A,)),
        session_reads("s2", role=REVIEW_ROLE, expected=(), applicable=False),
        session_reads("s3", expected=(), applicable=False),
    )
    sc = score(report, SCORE_PREFETCH_READS)

    assert sc.score == 1.0 and sc.confidence == 1.0
    assert report.reads.session_count == 3 and report.reads.applicable_count == 1


def test_prefetch_read_axis_unassessable_without_read_evidence():
    # No transcript store at all: the axis reports "not assessable", never 0.0.
    report = reads_report(available=False)
    sc = score(report, SCORE_PREFETCH_READS)

    assert sc.score is None and sc.confidence == 0.0
    assert any("not assessable" in w for w in sc.warnings)


def test_prefetch_read_axis_not_applicable_when_no_session_declared_a_set():
    report = reads_report(
        session_reads("s1", role=REVIEW_ROLE, expected=(), applicable=False),
    )
    sc = score(report, SCORE_PREFETCH_READS)

    assert sc.score is None and sc.confidence == 0.0
    assert any("not applicable" in w for w in sc.warnings)


def test_prefetch_read_axis_unassessable_when_no_candidate_was_measured():
    # Every candidate degraded: there is a question to answer here, unlike the
    # case above, and the honest answer is that the evidence could not answer it.
    report = reads_report(
        session_reads("s1", expected=(A,), assessable=False,
                      detail="evidence incomplete (2 unreadable row(s))"),
        session_reads("s2", expected=(), assessable=False, applicable=None,
                      detail="session did not read its task file"),
    )
    sc = score(report, SCORE_PREFETCH_READS)

    assert sc.score is None and sc.confidence == 0.0
    assert report.reads.scored_count == 0
    assert any("none of them measured" in w for w in sc.warnings)


def test_read_axis_does_not_reward_reading_more_than_declared():
    # Two sessions, both complete; one also consulted three undeclared docs. Read
    # count is not quality, so the axis cannot tell them apart.
    lean = reads_report(session_reads("s1", expected=(A,), read=(A,)))
    eager = reads_report(
        session_reads("s1", expected=(A,), read=(A,), undeclared=(B, C, ".ai/map.md"))
    )
    assert score(lean, SCORE_PREFETCH_READS) == score(eager, SCORE_PREFETCH_READS)


def test_read_section_warnings_travel_into_the_rollup():
    report = reads_report(
        session_reads("s1", expected=(A,), read=(A,)),
        warnings=["read behavior: session s2: no session transcript found"],
    )
    assert report.reads.warnings == (
        "read behavior: session s2: no session transcript found",
    )
    # ...but they are the read section's own degradation notes, not the pack's
    # evidence-degradation list, which stays what it was.
    assert report.warnings == ()


# -- absent task content: report still built ----------------------------------


def test_report_over_absent_task_content_does_not_crash():
    # A task id with no file (evaluable for run history alone): protocol/handoff/
    # estimate are not assessable; reliability still scores the runs.
    report = report_of(None, run_ev(ended_ts=END, exit_code=0), present=False)

    assert report.protocol.task_content_present is False
    assert score(report, SCORE_PROTOCOL).score is None
    assert score(report, SCORE_HANDOFF).score is None
    assert score(report, SCORE_ESTIMATE).score is None
    assert score(report, SCORE_RELIABILITY).score == 1.0  # runs still assessed
    assert score(report, SCORE_KNOWLEDGE).score == 1.0  # no completed/prefetch signal


# -- report identity + serialization ------------------------------------------


def test_report_ties_to_pack_and_serializes():
    report = report_of(
        COMPLETED, run_ev(ended_ts=END, exit_code=0), source=SOURCE_ARCHIVED
    )

    assert report.evaluation_id == "e1"
    assert report.task_id == TASK and report.repo_name == "alpha"
    assert report.target_source == SOURCE_ARCHIVED and report.target_present
    assert report.generated_ts == FIXED_TS.isoformat()

    data = report.to_dict()
    assert json.loads(json.dumps(data)) == data  # deterministic, no exotic types
    assert data["evaluation_id"] == "e1"
    assert data["schema_version"] == STATIC_SCHEMA_VERSION
    assert {s["name"] for s in data["scores"]} == {
        SCORE_PROTOCOL, SCORE_RELIABILITY, SCORE_HANDOFF, SCORE_ESTIMATE,
        SCORE_KNOWLEDGE, SCORE_PREFETCH_READS,
    }
    assert data["runs"]["run_count"] == 1
    assert data["protocol"]["entry_count"] == 4
    assert data["reads"] == {
        "available": False,
        "session_count": 0,
        "applicable_count": 0,
        "unknown_applicable_count": 0,
        "scored_count": 0,
        "complete_count": 0,
        "dropped_candidate_count": 0,
        "sessions": [],
        "warnings": [],
    }


def test_top_level_warnings_are_evidence_degradation_only():
    report = report_of(
        COMPLETED,
        run_ev("a", warnings=["run a: history events unreadable"]),
        warnings=["pack-level warning"],
        source=SOURCE_ARCHIVED,
    )
    assert set(report.warnings) == {
        "pack-level warning",
        "run a: history events unreadable",
    }


# -- disabled gate ------------------------------------------------------------


def test_disabled_evaluator_refuses_build_report():
    with pytest.raises(EvaluationDisabledError):
        evaluator(enabled=False).build_report(pack(tgt=mk_target(COMPLETED)))


def test_disabled_evaluator_refuses_generate_metrics(tmp_path):
    config = Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=False,
    )
    repo = ManagedRepo(
        name="alpha", path=tmp_path / "repo", manifest=tmp_path / "repo" / "m.json"
    )
    with pytest.raises(EvaluationDisabledError):
        StaticEvaluator(config).generate_metrics(repo, TASK)


# -- persistence: static_metrics.json beside evidence.json --------------------


def _write_archived_completed_task(config: Config, tmp_path: Path) -> ManagedRepo:
    """A repo whose task is completed and already archived, with one clean run."""
    repo_path = tmp_path / "repos" / "alpha"
    (repo_path / ".ai-tasks" / "archive").mkdir(parents=True)
    (repo_path / ".ai-tasks" / "archive" / f"{TASK}.md").write_text(
        COMPLETED, encoding="utf-8"
    )
    repo = ManagedRepo(
        name="alpha", path=repo_path, manifest=repo_path / ".ai-deploy-manifest.json"
    )
    run_id = "20260722T090000Z-alpha-task"
    run_dir = config.data_dir / "runs" / run_id
    (run_dir / "control").mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "repo_name": "alpha",
                "repo_path": str(repo_path),
                "task_id": TASK,
                "started_ts": START,
                "ended_ts": END,
                "exit_code": 0,
            }
        ),
        encoding="utf-8",
    )
    return repo


def _failing_metrics_write(service):
    """A ``write_artifact`` that fails the metrics write only.

    Both artifacts now reach the tree through the same contained service seam, so
    a blanket failure would exercise ``generate_evidence``'s own cleanup instead of
    the metrics-write cleanup these tests are about. The evidence write passes
    through to the real method.
    """
    real = service.write_artifact

    def write(repo, task_id, evaluation_id, name, data):
        if name == STATIC_METRICS_NAME:
            raise OSError("disk full")
        real(repo, task_id, evaluation_id, name, data)

    return write


def test_generate_metrics_persists_report_beside_evidence(tmp_path):
    config = Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=True,
    )
    repo = _write_archived_completed_task(config, tmp_path)
    ev = StaticEvaluator(
        config,
        evaluation_service=EvaluationService(config, git_runner=_fake_git, clock=lambda: FIXED_TS),
        clock=lambda: FIXED_TS,
    )

    report = ev.generate_metrics(repo, TASK)

    # Both artifacts land in one per-evaluation dir, tied to the same id.
    eval_dir = tmp_path / "hub-data" / "evaluations" / "alpha" / TASK / report.evaluation_id
    metrics_path = eval_dir / STATIC_METRICS_NAME
    assert (eval_dir / EVIDENCE_NAME).is_file()
    assert metrics_path.is_file()

    persisted = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert persisted["evaluation_id"] == report.evaluation_id
    assert persisted["task_id"] == TASK and persisted["target_source"] == SOURCE_ARCHIVED
    assert persisted["runs"]["run_count"] == 1 and persisted["runs"]["clean_count"] == 1
    # The archived completed task scores clean across every axis it has evidence
    # for; there is no transcript store under this tmp_path, so the read axis is
    # unassessable rather than zero.
    assert all(
        s["score"] == 1.0
        for s in persisted["scores"]
        if s["name"] != SCORE_PREFETCH_READS
    )
    assert persisted["reads"]["available"] is False


def test_generate_metrics_cleans_up_dir_on_write_failure(tmp_path, monkeypatch):
    config = Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=True,
    )
    repo = _write_archived_completed_task(config, tmp_path)
    service = EvaluationService(config, git_runner=_fake_git, clock=lambda: FIXED_TS)
    ev = StaticEvaluator(config, evaluation_service=service, clock=lambda: FIXED_TS)

    monkeypatch.setattr(service, "write_artifact", _failing_metrics_write(service))

    with pytest.raises(OSError):
        ev.generate_metrics(repo, TASK)

    # No half-pair left behind: the whole evaluation dir (incl. evidence.json) is
    # removed when the metrics write fails.
    eval_id = FIXED_TS.strftime("%Y%m%dT%H%M%SZ")
    eval_dir = tmp_path / "hub-data" / "evaluations" / "alpha" / TASK / eval_id
    assert not eval_dir.exists()


def test_generate_metrics_failure_preserves_existing_retained_history(tmp_path, monkeypatch):
    # Regression: retention must not fire until the metrics pair is durable. With
    # retention=1 and one existing evaluation, a failed metrics write must leave
    # that existing evaluation intact — pruning it for the about-to-fail generation
    # and then rolling back the new dir would leave the task with ZERO evaluations.
    config = Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=True,
        eval_retention=1,
    )
    repo = _write_archived_completed_task(config, tmp_path)
    service = EvaluationService(config, git_runner=_fake_git, clock=lambda: FIXED_TS)
    ev = StaticEvaluator(config, evaluation_service=service, clock=lambda: FIXED_TS)

    first = ev.generate_metrics(repo, TASK)  # the existing retained evaluation
    assert len(service.list_evaluations(repo, TASK)) == 1

    monkeypatch.setattr(service, "write_artifact", _failing_metrics_write(service))

    with pytest.raises(OSError):
        ev.generate_metrics(repo, TASK)

    # The pre-existing evaluation survives the failed second generation (retention
    # never ran for it), and no failed-attempt dir leaked.
    remaining = service.list_evaluations(repo, TASK)
    assert [s.evaluation_id for s in remaining] == [first.evaluation_id]
    assert service.artifact_dir(repo, TASK, first.evaluation_id).is_dir()


def test_generate_metrics_success_still_auto_prunes_to_cap(tmp_path):
    # The fix moved retention out of generate_evidence but must NOT drop it: a
    # successful metrics generation still bounds history. With retention=1, a second
    # successful generation prunes the older evaluation, leaving exactly the newest.
    config = Config(
        ai_native_path=tmp_path / "ai-native",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=True,
        eval_retention=1,
    )
    repo = _write_archived_completed_task(config, tmp_path)
    service = EvaluationService(config, git_runner=_fake_git, clock=lambda: FIXED_TS)
    ev = StaticEvaluator(config, evaluation_service=service, clock=lambda: FIXED_TS)

    first = ev.generate_metrics(repo, TASK)
    second = ev.generate_metrics(repo, TASK)

    assert first.evaluation_id != second.evaluation_id  # same-second rerun bumps -N
    remaining = service.list_evaluations(repo, TASK)
    assert [s.evaluation_id for s in remaining] == [second.evaluation_id]
    assert not service.artifact_dir(repo, TASK, first.evaluation_id).exists()
