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
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.evaluation import (
    SOURCE_ACTIVE,
    SOURCE_ARCHIVED,
    EvaluationDisabledError,
    EvaluationService,
    EvidencePack,
    RunEvidence,
)
from orch_hub.history import KIND_ANSWER_RECORDED, KIND_RUN_STARTED, KIND_STOP_REQUESTED
from orch_hub.logs import LogSnapshot
from orch_hub.models import (
    EvaluationTarget,
    ManagedRepo,
    RunEvent,
    RunMetrics,
    RunRecord,
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
    SESSION_KIND,
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


def evaluator(*, enabled: bool = True) -> StaticEvaluator:
    # No filesystem access in session-1, so fixed dummy paths are fine.
    return StaticEvaluator(
        Config(
            ai_native_path=Path("/ai-native"),
            data_dir=Path("/hub-data"),
            evaluation_enabled=enabled,
        )
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
