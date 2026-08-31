"""Tests for the automatic Layer 2 dispatch policy (:mod:`orch_hub.eval_dispatch`).

Covers the capacity gate — a utilization ceiling read from the evaluator
provider's highest currently-measured window, and fail-toward-deferral for every
way that read can be unusable (unavailable, stale, figure-less, rolled over, a
figure measured in an earlier window, a raising reader) — and the dispatcher that
drains the deferred queue: oldest first, at most one per sweep, spaced from the
last turn *admitted* to spend (manual turns included, and counted from admission
rather than from the worker's later start stamp), each held job carrying the
bounded machine reason it is still waiting, an unattributable record failed
rather than left resident, and the queue-age bound warning once per job rather
than once per sweep. Plus the two switches, checked against the durable queue a
restart inherits: a closed subsystem or trigger gate holds every accepted record
untouched, and reopening both drains it with no recovery step. Then the operator
pause, which is deliberately none of those: read fresh on every sweep, holding the
drain exactly as a closed gate does while leaving *acceptance* open, failing
toward held when its own record is unreadable, and travelling to readers through
the snapshot rather than being stamped on records. Then the operator view
``queue_snapshot`` folds over: a complete census the row cap never shrinks, the
two-group order, the deferred-only overdue flag, a read that moves nothing — and
what the census is *not*, pinned from a real completed-then-failed retry.
Every usage snapshot is synthetic; nothing here reads the operator's real
subscription data or spends a model turn.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.evaluation import EvaluationService
from orch_hub.semantic import (
    SEMANTIC_END,
    SEMANTIC_START,
    SemanticEvaluationError,
    SemanticEvaluator,
)
from orch_hub.subprocess_utils import CommandResult
from orch_hub.eval_dispatch import (
    REASON_CAPACITY,
    REASON_PACING,
    REASON_USAGE_UNKNOWN,
    CapacityGate,
    DeferredDispatcher,
    queue_snapshot,
)
from orch_hub.eval_control import (
    PAUSE_OPERATOR,
    PAUSE_UNREADABLE,
    EvaluationControlStore,
)
from orch_hub.models import (
    PROVIDER_CLAUDE,
    EvaluationControl,
    ManagedRepo,
    SemanticJob,
    ProviderUsage,
    SemanticEvaluatorInfo,
    SemanticReport,
    UsageWindow,
)
from orch_hub.semantic_jobs import (
    SKIP_RUN_OPT_OUT,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DEFERRED,
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    SemanticJobManager,
)

TASK = "2026-08-09-completion-triggered-evaluation"
REPO_PATH = Path("/repos/alpha")

RACE_PATIENCE = 5
"""Seconds a threaded test waits on the other thread before calling it stuck.

Generous, because the assertion never depends on winning a timing race — the
barrier decides the outcome, and this only keeps a regression from hanging the
suite instead of failing it."""
NOW = dt.datetime(2026, 8, 9, 12, 0, 0, tzinfo=dt.timezone.utc)


# -- helpers ------------------------------------------------------------------


def repo(name: str = "alpha", path: Path = REPO_PATH) -> ManagedRepo:
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


class Clock:
    def __init__(self, now: dt.datetime = NOW):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + dt.timedelta(seconds=seconds)


def window(
    key: str = "five_hour",
    *,
    used: float | None = 10.0,
    resets_in: float | None = 3600.0,
    figure_offset: float = 0.0,
    now: dt.datetime = NOW,
) -> UsageWindow:
    """One provider window.

    ``figure_offset`` shifts ``figure_instant`` *behind* ``resets_at`` — the shape
    a percentage-less boundary refresh produces, where the displayed boundary has
    moved on but the figure was measured in an earlier window.
    """
    resets = None if resets_in is None else now + dt.timedelta(seconds=resets_in)
    figure = None if resets is None else resets - dt.timedelta(seconds=figure_offset)
    return UsageWindow(
        key=key,
        used_percent=used,
        resets_at=None if resets is None else resets.isoformat(),
        figure_instant=None if figure is None else figure.isoformat(),
    )


def usage(*windows: UsageWindow, stale: bool = False, available: bool = True) -> ProviderUsage:
    return ProviderUsage(
        provider=PROVIDER_CLAUDE,
        available=available,
        windows=tuple(windows),
        observed_ts=NOW.isoformat(),
        stale=stale,
    )


def config(tmp_path: Path, **overrides) -> Config:
    return Config(
        mandrel_path=tmp_path / "mandrel",
        data_dir=tmp_path / "hub-data",
        **overrides,
    )


def gate(cfg: Config, snapshot, *, clock: Clock | None = None) -> CapacityGate:
    reader = snapshot if callable(snapshot) else (lambda: snapshot)
    return CapacityGate(cfg, usage_reader=reader, clock=clock or Clock())


def mk_report(evaluation_id: str) -> SemanticReport:
    return SemanticReport(
        evaluation_id=evaluation_id,
        generated_ts=NOW.isoformat(),
        repo_name="alpha",
        task_id=TASK,
        target_present=True,
        target_source="archive",
        evaluator=SemanticEvaluatorInfo(backend="fake", model="fake-model", schema_version="1"),
        summary="ok",
        overall_confidence=0.7,
    )


class FakeEvaluator:
    def __init__(self):
        self.calls: list[tuple[ManagedRepo, str]] = []

    @property
    def evaluator_info(self) -> SemanticEvaluatorInfo:
        return SemanticEvaluatorInfo(backend="fake", model="fake-model", schema_version="1")

    def evaluate(
        self, r: ManagedRepo, task_id: str, *, turn_start_by=None, cancel=None
    ) -> SemanticReport:
        self.calls.append((r, task_id))
        return mk_report(f"EVAL-{task_id}")


class FlippingBackend:
    """A real semantic backend seam that succeeds, then fails when told to.

    The two-attempt shape one test needs: a first turn that produces a genuine
    persisted report, and a retry that fails the way a broken CLI does. Only the
    ``(info, evaluate, doctor)`` seam, so the evaluation/static/persistence chain
    above it is the real one.
    """

    def __init__(self):
        self.fail = False

    @property
    def info(self) -> SemanticEvaluatorInfo:
        return SemanticEvaluatorInfo(
            backend="claude-cli", model="fake-model", schema_version="1"
        )

    def evaluate(self, prompt: str, r: ManagedRepo, *, cancel=None) -> str:
        if self.fail:
            raise SemanticEvaluationError("claude backend exited 1")
        body = json.dumps(
            {
                "summary": "clean handoff",
                "confidence": 0.8,
                "scores": [
                    {
                        "dimension": "handoff_completeness",
                        "score": 0.9,
                        "confidence": 0.8,
                        "rationale": "log complete",
                    }
                ],
                "findings": [],
                "improvements": [],
            }
        )
        return f"{SEMANTIC_START}\n{body}\n{SEMANTIC_END}"

    def doctor(self):  # pragma: no cover - not exercised here
        raise NotImplementedError


def _git_unavailable(args, **kwargs) -> CommandResult:
    """No git in the fixture tree; the pack degrades to best-effort, as designed."""
    return CommandResult(tuple(str(a) for a in args), 128, "", "")


def sync_executor(run) -> None:
    run()


class DeferredExecutor:
    """Captures turns without running them, so a promoted job stays ``queued``."""

    def __init__(self):
        self.pending: list = []

    def __call__(self, run) -> None:
        self.pending.append(run)


def stack(
    tmp_path: Path,
    *,
    snapshot=None,
    clock: Clock | None = None,
    executor=sync_executor,
    **cfg_overrides,
):
    """A real job manager plus the policy over it (no HTTP, no model, no usage IO)."""
    cfg = config(tmp_path, **cfg_overrides)
    clock = clock or Clock()
    evaluator = FakeEvaluator()
    jobs = SemanticJobManager(
        cfg, evaluator=evaluator, clock=clock, executor=executor
    )
    dispatcher = DeferredDispatcher(
        cfg,
        job_manager=jobs,
        gate=gate(cfg, snapshot if snapshot is not None else usage(window()), clock=clock),
        clock=clock,
    )
    return cfg, jobs, dispatcher, evaluator, clock


# -- the capacity gate --------------------------------------------------------


def test_a_quiet_window_admits_a_turn(tmp_path):
    assert gate(config(tmp_path), usage(window(used=10.0))).check() is None


def test_the_ceiling_holds_a_turn_and_is_configuration(tmp_path):
    cfg = config(tmp_path)  # default ceiling
    assert gate(cfg, usage(window(used=cfg.eval_usage_ceiling))).check() == REASON_CAPACITY
    assert gate(cfg, usage(window(used=cfg.eval_usage_ceiling - 0.1))).check() is None

    tuned = config(tmp_path, eval_usage_ceiling=25.0)
    assert gate(tuned, usage(window(used=30.0))).check() == REASON_CAPACITY
    assert gate(tuned, usage(window(used=20.0))).check() is None


def test_the_ceiling_reads_the_most_exhausted_window(tmp_path):
    """An exhausted weekly window is not hidden by a fresh five-hour one."""
    snapshot = usage(
        window("five_hour", used=5.0),
        window("seven_day", used=95.0, resets_in=3 * 86400),
    )
    assert gate(config(tmp_path), snapshot).check() == REASON_CAPACITY


@pytest.mark.parametrize(
    "snapshot_kwargs, label",
    [
        (dict(available=False), "provider unavailable"),
        (dict(stale=True), "observation stale"),
    ],
)
def test_unusable_provider_state_defers(tmp_path, snapshot_kwargs, label):
    snapshot = usage(window(used=1.0), **snapshot_kwargs)
    assert gate(config(tmp_path), snapshot).check() == REASON_USAGE_UNKNOWN, label


@pytest.mark.parametrize(
    "windows, label",
    [
        ((), "no windows at all"),
        ((window(used=None),), "window carries no figure"),
        ((window(resets_in=None),), "window boundary unknown"),
        ((window(resets_in=-60.0),), "boundary already passed: no window is open"),
        ((window(figure_offset=6 * 3600),), "figure measured in an earlier window"),
        ((window(used=1.0), window("seven_day", used=None)), "one window unreadable"),
    ],
)
def test_an_unknown_window_is_not_an_empty_window(tmp_path, windows, label):
    """Fail toward deferral: every degraded read holds the turn, never admits it."""
    assert gate(config(tmp_path), usage(*windows)).check() == REASON_USAGE_UNKNOWN, label


def test_a_raising_usage_reader_defers(tmp_path):
    def boom():
        raise OSError("usage store unreadable")

    assert gate(config(tmp_path), boom).check() == REASON_USAGE_UNKNOWN


# -- the dispatcher -----------------------------------------------------------


def test_an_empty_queue_sweeps_to_nothing(tmp_path):
    _, _, dispatcher, evaluator, _ = stack(tmp_path)

    assert dispatcher.sweep() is None
    assert evaluator.calls == []


def test_a_held_job_dispatches_when_capacity_allows(tmp_path):
    _, jobs, dispatcher, evaluator, _ = stack(tmp_path, snapshot=usage(window(used=5.0)))
    jobs.enqueue_if_absent(repo(), TASK)

    started = dispatcher.sweep()

    assert started is not None and started.task_id == TASK
    assert jobs.get_job(repo(), TASK).status == STATUS_COMPLETED
    assert evaluator.calls == [(repo(), TASK)]


@pytest.mark.parametrize(
    "snapshot, expected",
    [
        (usage(window(used=99.0)), REASON_CAPACITY),
        (usage(available=False), REASON_USAGE_UNKNOWN),
    ],
)
def test_a_refused_sweep_holds_the_job_with_its_reason(tmp_path, snapshot, expected):
    _, jobs, dispatcher, evaluator, _ = stack(tmp_path, snapshot=snapshot)
    jobs.enqueue_if_absent(repo(), TASK)

    assert dispatcher.sweep() is None

    held = jobs.get_job(repo(), TASK)
    assert held.status == STATUS_DEFERRED
    assert held.defer_reason == expected
    assert evaluator.calls == []  # deferred, never dropped and never spent


def test_a_deferred_job_dispatches_on_its_own_when_capacity_returns(tmp_path):
    """Defer rather than drop: nothing re-enqueues, the queue simply retries."""
    snapshots = [usage(window(used=99.0)), usage(window(used=99.0)), usage(window(used=4.0))]
    reads = iter(snapshots)
    _, jobs, dispatcher, evaluator, _ = stack(tmp_path, snapshot=lambda: next(reads))
    jobs.enqueue_if_absent(repo(), TASK)

    assert dispatcher.sweep() is None
    assert dispatcher.sweep() is None
    assert jobs.get_job(repo(), TASK).status == STATUS_DEFERRED

    assert dispatcher.sweep() is not None
    assert jobs.get_job(repo(), TASK).status == STATUS_COMPLETED
    assert evaluator.calls == [(repo(), TASK)]


def test_a_held_queue_survives_a_hub_restart_and_drains_after_it(tmp_path):
    """The restart-time re-dispatch is the resident sweep, not a startup step."""
    cfg, jobs, dispatcher, _, clock = stack(tmp_path, snapshot=usage(window(used=99.0)))
    jobs.enqueue_if_absent(repo(), TASK)
    dispatcher.sweep()
    assert jobs.get_job(repo(), TASK).defer_reason == REASON_CAPACITY

    # A fresh process over the same data dir, capacity now free.
    reborn_eval = FakeEvaluator()
    reborn_jobs = SemanticJobManager(
        cfg, evaluator=reborn_eval, clock=clock, executor=sync_executor
    )
    assert reborn_jobs.reconcile_orphans() == []
    reborn = DeferredDispatcher(
        cfg,
        job_manager=reborn_jobs,
        gate=gate(cfg, usage(window(used=3.0)), clock=clock),
        clock=clock,
    )

    assert reborn.sweep() is not None
    assert reborn_jobs.get_job(repo(), TASK).status == STATUS_COMPLETED
    assert reborn_eval.calls == [(repo(), TASK)]


# -- the rollover herd --------------------------------------------------------


def test_a_backlog_drains_one_job_per_spacing_interval(tmp_path):
    """A ceiling plus a queue makes the whole backlog dispatchable at the rollover.

    Single-flight prevents double-spending one task, never a herd across tasks —
    so the fresh window's opening capacity must not be handed the entire queue at
    the moment the operator comes back to work.
    """
    clock = Clock()
    _, jobs, dispatcher, evaluator, _ = stack(
        tmp_path, snapshot=usage(window(used=2.0)), clock=clock, eval_dispatch_spacing=900
    )
    tasks = ["2026-08-01-a", "2026-08-02-b", "2026-08-03-c"]
    for index, task in enumerate(tasks):
        clock.advance(1)
        jobs.enqueue_if_absent(repo(), task)
        assert jobs.get_job(repo(), task).status == STATUS_DEFERRED
        assert index >= 0

    first = dispatcher.sweep()
    assert first is not None and first.task_id == tasks[0]

    # Inside the spacing: the rest stay held, with the pacing reason recorded.
    clock.advance(60)
    assert dispatcher.sweep() is None
    assert [jobs.get_job(repo(), t).defer_reason for t in tasks[1:]] == [
        REASON_PACING,
        REASON_PACING,
    ]

    clock.advance(900)
    second = dispatcher.sweep()
    assert second is not None and second.task_id == tasks[1]
    assert len(evaluator.calls) == 2


def test_two_overlapping_sweeps_do_not_start_two_turns(tmp_path):
    """Concurrent sweeps are the one way the spacing rule could be bypassed.

    The resident tick and a just-landed completion can ask at the same instant.
    Both would pass the spacing check against the same pre-dispatch state and then
    promote a *different* held job — one turn per sweep, two turns at once. The
    second sweep therefore declines outright rather than queueing.
    """
    reentered = []

    class ReentrantEvaluator(FakeEvaluator):
        """Sweeps again from inside the turn, i.e. while the first sweep is running."""

        def __init__(self, dispatcher_box):
            super().__init__()
            self._box = dispatcher_box

        def evaluate(self, r, task_id, *, turn_start_by=None, cancel=None):
            reentered.append(self._box["dispatcher"].sweep())
            return super().evaluate(r, task_id, turn_start_by=turn_start_by)

    cfg = config(tmp_path, eval_dispatch_spacing=0)
    clock = Clock()
    box: dict = {}
    evaluator = ReentrantEvaluator(box)
    jobs = SemanticJobManager(cfg, evaluator=evaluator, clock=clock, executor=sync_executor)
    box["dispatcher"] = DeferredDispatcher(
        cfg, job_manager=jobs, gate=gate(cfg, usage(window(used=1.0)), clock=clock), clock=clock
    )
    jobs.enqueue_if_absent(repo(), "2026-08-01-a")
    jobs.enqueue_if_absent(repo(), "2026-08-02-b")

    assert box["dispatcher"].sweep() is not None
    assert reentered == [None]  # the overlapping sweep declined
    assert len(evaluator.calls) == 1
    assert jobs.get_job(repo(), "2026-08-02-b").status == STATUS_DEFERRED


def test_the_oldest_held_job_goes_first(tmp_path):
    clock = Clock()
    _, jobs, dispatcher, _, _ = stack(tmp_path, clock=clock, eval_dispatch_spacing=0)
    jobs.enqueue_if_absent(repo(), "2026-08-01-older")
    clock.advance(120)
    jobs.enqueue_if_absent(repo(), "2026-08-02-newer")

    assert dispatcher.sweep().task_id == "2026-08-01-older"
    assert dispatcher.sweep().task_id == "2026-08-02-newer"


def test_pacing_counts_a_manual_turn_too(tmp_path):
    """A manual evaluation draws on the same window, so it paces the queue."""
    clock = Clock()
    _, jobs, dispatcher, _, _ = stack(tmp_path, clock=clock, eval_dispatch_spacing=900)
    jobs.start_job(repo(), "2026-08-01-manual")
    clock.advance(60)
    jobs.enqueue_if_absent(repo(), TASK)

    assert dispatcher.sweep() is None
    assert jobs.get_job(repo(), TASK).defer_reason == REASON_PACING

    clock.advance(900)
    assert dispatcher.sweep() is not None


def test_a_skipped_task_does_not_pace_the_queue(tmp_path):
    """A skip spends nothing, so it must not hold back the jobs that would.

    Otherwise a week of opted-out completions would pace the queue as if each one
    had drawn on the window — starving exactly the tasks the operator did want
    measured, on the strength of turns that never happened.
    """
    clock = Clock()
    _, jobs, dispatcher, _, _ = stack(tmp_path, clock=clock, eval_dispatch_spacing=900)
    jobs.skip_if_absent(repo(), "2026-08-01-declined", SKIP_RUN_OPT_OUT)
    clock.advance(60)
    jobs.enqueue_if_absent(repo(), TASK)

    assert dispatcher.sweep() is not None
    assert jobs.get_job(repo(), TASK).status != STATUS_DEFERRED


def test_zero_spacing_drains_as_fast_as_capacity_allows(tmp_path):
    _, jobs, dispatcher, evaluator, _ = stack(tmp_path, eval_dispatch_spacing=0)
    jobs.enqueue_if_absent(repo(), "2026-08-01-a")
    jobs.enqueue_if_absent(repo(), "2026-08-02-b")

    assert dispatcher.sweep() is not None
    assert dispatcher.sweep() is not None
    assert len(evaluator.calls) == 2


def test_spacing_is_checked_before_capacity_is_read(tmp_path):
    """A usage read can trigger background work; do not ask when it cannot be used."""
    reads = []

    def reader():
        reads.append(True)
        return usage(window(used=1.0))

    cfg = config(tmp_path, eval_dispatch_spacing=900)
    clock = Clock()
    jobs = SemanticJobManager(
        cfg, evaluator=FakeEvaluator(), clock=clock, executor=sync_executor
    )
    dispatcher = DeferredDispatcher(
        cfg, job_manager=jobs, gate=gate(cfg, reader, clock=clock), clock=clock
    )
    jobs.start_job(repo(), "2026-08-01-manual")
    jobs.enqueue_if_absent(repo(), TASK)

    assert dispatcher.sweep() is None
    assert reads == []


# -- queue hygiene ------------------------------------------------------------


def test_only_the_held_job_is_touched_while_others_run(tmp_path):
    """A promoted job is transient, so a later sweep neither re-runs nor holds it."""
    executor = DeferredExecutor()
    _, jobs, dispatcher, _, _ = stack(
        tmp_path, executor=executor, eval_dispatch_spacing=0
    )
    jobs.enqueue_if_absent(repo(), TASK)

    promoted = dispatcher.sweep()
    assert promoted is not None and promoted.status == STATUS_QUEUED
    assert dispatcher.sweep() is None  # nothing deferred remains
    assert jobs.get_job(repo(), TASK).status == STATUS_QUEUED
    assert len(executor.pending) == 1


def test_an_unattributable_held_job_is_failed_and_the_next_one_runs(tmp_path):
    """A record that can never dispatch must not become a permanent resident."""
    cfg, jobs, dispatcher, evaluator, clock = stack(tmp_path, eval_dispatch_spacing=0)
    jobs.enqueue_if_absent(repo(), "2026-08-01-orphan")
    jobs.enqueue_if_absent(repo(), "2026-08-02-fine")
    # Strip the repo attribution the way a damaged record would have lost it.
    path = cfg.data_dir / "semantic-jobs" / "alpha" / "2026-08-01-orphan" / "job.json"
    data = json.loads(path.read_text())
    data["repo_path"] = None
    path.write_text(json.dumps(data))

    started = dispatcher.sweep()

    assert jobs.get_job(repo(), "2026-08-01-orphan").status == STATUS_FAILED
    assert started is not None and started.task_id == "2026-08-02-fine"
    assert evaluator.calls == [(repo(), "2026-08-02-fine")]


def test_holding_rewrites_nothing_when_the_reason_is_unchanged(tmp_path):
    """A queue legitimately waiting for hours must not rewrite itself every sweep."""
    clock = Clock()
    # A weekly window, so an hour of waiting does not itself change the reason.
    _, jobs, dispatcher, _, _ = stack(
        tmp_path,
        snapshot=usage(window("seven_day", used=99.0, resets_in=7 * 86400)),
        clock=clock,
    )
    jobs.enqueue_if_absent(repo(), TASK)
    dispatcher.sweep()
    first = jobs.get_job(repo(), TASK).updated_ts
    assert jobs.get_job(repo(), TASK).defer_reason == REASON_CAPACITY

    clock.advance(3600)
    dispatcher.sweep()

    assert jobs.get_job(repo(), TASK).updated_ts == first


def test_a_stalled_queue_is_surfaced_once_per_job(tmp_path, caplog):
    """The age bound bounds how long a stalled queue can stay invisible.

    It changes no dispatch decision — a stalled job is still waiting for capacity,
    not failed — and it warns once per job, not once per 30-second sweep.
    """
    clock = Clock()
    _, jobs, dispatcher, _, _ = stack(
        tmp_path,
        snapshot=usage(available=False),
        clock=clock,
        eval_queue_max_age=21600,
    )
    jobs.enqueue_if_absent(repo(), TASK)

    with caplog.at_level(logging.WARNING, logger="orch_hub.eval_dispatch"):
        dispatcher.sweep()
        assert caplog.records == []  # fresh: normal backpressure

        clock.advance(21600)
        dispatcher.sweep()
        dispatcher.sweep()
        dispatcher.sweep()

    stalled = [r for r in caplog.records if "not draining" in r.getMessage()]
    assert len(stalled) == 1
    assert TASK in stalled[0].getMessage()
    assert jobs.get_job(repo(), TASK).status == STATUS_DEFERRED  # still just waiting


def test_a_zero_age_bound_surfaces_nothing(tmp_path, caplog):
    clock = Clock()
    _, jobs, dispatcher, _, _ = stack(
        tmp_path,
        snapshot=usage(available=False),
        clock=clock,
        eval_queue_max_age=0,
    )
    jobs.enqueue_if_absent(repo(), TASK)

    with caplog.at_level(logging.WARNING, logger="orch_hub.eval_dispatch"):
        clock.advance(30 * 86400)
        dispatcher.sweep()

    assert [r for r in caplog.records if "not draining" in r.getMessage()] == []


# -- the two switches, against a durable queue --------------------------------


def enqueued_queue(tmp_path: Path, tasks: tuple[str, ...]):
    """A data dir holding already-accepted ``deferred`` records.

    Models what every restart actually finds: the queue outlives the process that
    accepted it, so the switches a *later* process runs under have to be honored
    against records nobody is going to re-decide.
    """
    cfg, jobs, _, _, clock = stack(tmp_path)
    for task in tasks:
        jobs.enqueue_if_absent(repo(), task)
    return cfg, jobs, clock


@pytest.mark.parametrize(
    "gates, label",
    [
        ({"eval_auto": False}, "trigger gate closed: manual-only operation"),
        ({"evaluation_enabled": False}, "subsystem gate closed: evaluation is inert"),
    ],
)
def test_a_pre_existing_queue_is_never_promoted_while_a_gate_is_closed(
    tmp_path, gates, label
):
    """A durable queue must not outlive the switch that stopped producing it.

    Restart shape: an earlier enabled process accepted the record, this one runs
    with a gate closed. Promoting it would be an automatic model turn in a
    deployment that said manual-only — and with the *subsystem* gate closed it is
    worse than a stray turn, because the evaluator refuses a disabled subsystem
    and the promotion would settle the record ``failed``, consuming the task's one
    automatic attempt.
    """
    _, jobs, clock = enqueued_queue(tmp_path, (TASK,))
    cfg = config(tmp_path, **gates)
    evaluator = FakeEvaluator()
    reborn_jobs = SemanticJobManager(
        cfg, evaluator=evaluator, clock=clock, executor=sync_executor
    )
    reborn = DeferredDispatcher(
        cfg,
        job_manager=reborn_jobs,
        gate=gate(cfg, usage(window(used=1.0)), clock=clock),
        clock=clock,
    )
    before = reborn_jobs.get_job(repo(), TASK)

    assert reborn.enabled is False, label
    assert reborn.sweep() is None
    assert evaluator.calls == []
    # Held exactly as it was: nothing walked, nothing written, nothing failed.
    assert reborn_jobs.get_job(repo(), TASK) == before


def test_a_queue_held_by_a_closed_gate_drains_when_both_switches_reopen(tmp_path):
    """Deferral already means "not now", so reopening needs no recovery step."""
    _, _, clock = enqueued_queue(tmp_path, (TASK,))
    off = config(tmp_path, eval_auto=False)
    evaluator = FakeEvaluator()
    jobs = SemanticJobManager(
        off, evaluator=evaluator, clock=clock, executor=sync_executor
    )
    DeferredDispatcher(
        off,
        job_manager=jobs,
        gate=gate(off, usage(window(used=1.0)), clock=clock),
        clock=clock,
    ).sweep()
    assert jobs.get_job(repo(), TASK).status == STATUS_DEFERRED

    on = config(tmp_path)
    reopened = DeferredDispatcher(
        on,
        job_manager=SemanticJobManager(
            on, evaluator=evaluator, clock=clock, executor=sync_executor
        ),
        gate=gate(on, usage(window(used=1.0)), clock=clock),
        clock=clock,
    )

    assert reopened.sweep() is not None
    assert evaluator.calls == [(repo(), TASK)]


def test_a_closed_gate_never_fails_an_unattributable_record(tmp_path):
    """The one write a sweep makes to a record it cannot dispatch is gated too."""
    cfg_on, jobs, clock = enqueued_queue(tmp_path, (TASK,))
    path = cfg_on.data_dir / "semantic-jobs" / "alpha" / TASK / "job.json"
    data = json.loads(path.read_text())
    data["repo_path"] = None
    path.write_text(json.dumps(data))

    off = config(tmp_path, eval_auto=False)
    off_jobs = SemanticJobManager(
        off, evaluator=FakeEvaluator(), clock=clock, executor=sync_executor
    )
    DeferredDispatcher(
        off,
        job_manager=off_jobs,
        gate=gate(off, usage(window(used=1.0)), clock=clock),
        clock=clock,
    ).sweep()

    assert off_jobs.get_job(repo(), TASK).status == STATUS_DEFERRED


# -- the operator pause -------------------------------------------------------
#
# The lever that is deliberately NOT the trigger gate. ``ORCH_HUB_EVAL_AUTO``
# needs a restart and stops completions being *accepted*, so every task finishing
# while it is off is permanently absent from the corpus. A pause holds only the
# drain, live, and the queue keeps filling behind it — which is what makes it
# usable as a routine control rather than a decision.


def paused(reason: str = PAUSE_OPERATOR, ts: str = "2026-08-09T11:00:00+00:00"):
    return lambda: EvaluationControl(paused=True, paused_ts=ts, reason=reason)


def test_a_paused_queue_is_held_exactly_as_it_is(tmp_path):
    """Nothing walked, nothing written, nothing failed — the same treatment a
    closed gate gets, and for the same reason: a pause is one global fact, not a
    per-job one, so stamping it on records would churn the whole queue on a toggle
    and leave them claiming a reason that stopped being true on resume."""
    cfg, jobs, clock = enqueued_queue(tmp_path, (TASK,))
    before = jobs.get_job(repo(), TASK)
    evaluator = FakeEvaluator()
    live = SemanticJobManager(cfg, evaluator=evaluator, clock=clock, executor=sync_executor)
    dispatcher = DeferredDispatcher(
        cfg,
        job_manager=live,
        gate=gate(cfg, usage(window(used=1.0)), clock=clock),
        control=paused(),
        clock=clock,
    )

    assert dispatcher.sweep() is None
    assert evaluator.calls == []
    assert live.get_job(repo(), TASK) == before


def test_resuming_drains_the_queue_that_was_held_with_no_recovery_step(tmp_path):
    """The pause costs the corpus nothing but time: the same records, in the same
    completion order, the moment it is released."""
    cfg, jobs, clock = enqueued_queue(tmp_path, (TASK,))
    evaluator = FakeEvaluator()
    live = SemanticJobManager(cfg, evaluator=evaluator, clock=clock, executor=sync_executor)
    state = {"paused": True}
    dispatcher = DeferredDispatcher(
        cfg,
        job_manager=live,
        gate=gate(cfg, usage(window(used=1.0)), clock=clock),
        control=lambda: EvaluationControl(paused=state["paused"]),
        clock=clock,
    )
    assert dispatcher.sweep() is None

    state["paused"] = False  # no restart, no reconstruction

    assert dispatcher.sweep() is not None
    assert evaluator.calls == [(repo(), TASK)]


def test_the_pause_is_read_fresh_on_every_sweep(tmp_path):
    """A dispatcher that cached it would reintroduce the restart the pause exists
    to remove."""
    cfg, _, clock = enqueued_queue(tmp_path, (TASK,))
    reads = {"n": 0}

    def control() -> EvaluationControl:
        reads["n"] += 1
        return EvaluationControl(paused=True)

    dispatcher = DeferredDispatcher(
        cfg,
        job_manager=SemanticJobManager(
            cfg, evaluator=FakeEvaluator(), clock=clock, executor=sync_executor
        ),
        gate=gate(cfg, usage(window(used=1.0)), clock=clock),
        control=control,
        clock=clock,
    )
    dispatcher.sweep()
    dispatcher.sweep()

    assert reads["n"] == 2


def test_a_pause_never_stops_a_completion_being_accepted(tmp_path):
    """The whole difference from ``ORCH_HUB_EVAL_AUTO``.

    The trigger reads ``enabled`` to decide whether to accept a completion at all;
    folding the pause into that predicate would make a paused hub drop completions
    permanently through the once-per-task guard — exactly the loss the pause exists
    to avoid.
    """
    cfg, jobs, clock = enqueued_queue(tmp_path, ())
    dispatcher = DeferredDispatcher(
        cfg,
        job_manager=jobs,
        gate=gate(cfg, usage(window(used=1.0)), clock=clock),
        control=paused(),
        clock=clock,
    )

    assert dispatcher.enabled is True  # acceptance stays open
    assert dispatcher.paused is True  # only the drain is held
    assert jobs.enqueue_if_absent(repo(), TASK) is not None
    assert jobs.get_job(repo(), TASK).status == STATUS_DEFERRED


def test_an_unreadable_control_holds_the_drain(tmp_path):
    """The store fails toward paused; the dispatcher consumes that rather than
    re-deciding it. An unaccountable switch must not spend the window."""
    cfg, jobs, clock = enqueued_queue(tmp_path, (TASK,))
    evaluator = FakeEvaluator()
    live = SemanticJobManager(cfg, evaluator=evaluator, clock=clock, executor=sync_executor)
    dispatcher = DeferredDispatcher(
        cfg,
        job_manager=live,
        gate=gate(cfg, usage(window(used=1.0)), clock=clock),
        control=paused(PAUSE_UNREADABLE, ts=None),
        clock=clock,
    )

    assert dispatcher.sweep() is None
    assert evaluator.calls == []


def test_the_promotion_boundary_re_reads_the_pause_it_pre_checked(tmp_path):
    """The gap between the sweep's pause check and its promotion is real time.

    Between them sit a store walk, a spacing check, and a capacity read — all of
    which an operator can press Pause during. A sweep that carried its first
    reading down to the promotion would spend a turn out of a queue that is, by
    then, held. So the check that decides is taken at the promotion boundary.
    """
    cfg, _, clock = enqueued_queue(tmp_path, (TASK,))
    evaluator = FakeEvaluator()
    reads: list[bool] = []

    def control() -> EvaluationControl:
        # Running when the sweep pre-checks; paused by the time it promotes.
        reads.append(True)
        return EvaluationControl() if len(reads) == 1 else EvaluationControl(paused=True)

    live = SemanticJobManager(cfg, evaluator=evaluator, clock=clock, executor=sync_executor)
    dispatcher = DeferredDispatcher(
        cfg,
        job_manager=live,
        gate=gate(cfg, usage(window(used=1.0)), clock=clock),
        control=control,
        clock=clock,
    )

    assert dispatcher.sweep() is None
    assert len(reads) == 2  # the pre-filter, then the one that decides
    assert evaluator.calls == []
    assert live.get_job(repo(), TASK).status == STATUS_DEFERRED


def test_a_pause_write_cannot_be_overtaken_by_a_sweep_already_deciding(tmp_path):
    """The live race, driven through the real control store and the real barrier.

    The pause lives in one store and the queue in another, written by two
    different threads, so "paused" is only a hold if the two cannot interleave. A
    sweep that read the control just before ``POST .../queue/pause`` would
    otherwise go on to promote a job after the pause is durable — and after the
    route has answered that the queue is held.
    """
    cfg, _, clock = enqueued_queue(tmp_path, (TASK,))
    evaluator = FakeEvaluator()
    live = SemanticJobManager(cfg, evaluator=evaluator, clock=clock, executor=sync_executor)
    store = EvaluationControlStore(cfg.data_dir)
    read_an_unpaused_control = threading.Event()

    def control() -> EvaluationControl:
        state = store.state()
        if not state.paused:
            read_an_unpaused_control.set()
        return state

    dispatcher = DeferredDispatcher(
        cfg,
        job_manager=live,
        gate=gate(cfg, usage(window(used=1.0)), clock=clock),
        control=control,
        clock=clock,
    )
    swept: list = []
    sweeper = threading.Thread(target=lambda: swept.append(dispatcher.sweep()))

    with dispatcher.admission_barrier():  # what the pause route holds
        sweeper.start()
        assert read_an_unpaused_control.wait(RACE_PATIENCE), "the sweep never ran"
        store.pause()
    sweeper.join(RACE_PATIENCE)

    assert not sweeper.is_alive()
    assert swept == [None]
    assert evaluator.calls == []
    assert live.get_job(repo(), TASK).status == STATUS_DEFERRED


def test_a_dispatcher_built_without_a_control_store_is_not_paused(tmp_path):
    """"No control supplied" is a different thing from "the control could not be
    read" — only the store decides the second, and only it fails closed."""
    cfg, _, clock = enqueued_queue(tmp_path, (TASK,))
    evaluator = FakeEvaluator()
    dispatcher = DeferredDispatcher(
        cfg,
        job_manager=SemanticJobManager(
            cfg, evaluator=evaluator, clock=clock, executor=sync_executor
        ),
        gate=gate(cfg, usage(window(used=1.0)), clock=clock),
        clock=clock,
    )

    assert dispatcher.paused is False
    assert dispatcher.sweep() is not None


def test_the_pause_reaches_a_reader_through_the_queue_snapshot(tmp_path):
    """A pause must never become a second silent condition on a queue whose failure
    modes are already silent. It travels the way ``auto_enabled`` does."""
    control = EvaluationControl(
        paused=True, paused_ts="2026-08-09T11:00:00+00:00", reason=PAUSE_OPERATOR
    )

    snap = queue_snapshot(
        config(tmp_path), [job("a", STATUS_DEFERRED)], control=control, now=NOW
    )

    assert snap.control == control
    assert snap.to_dict()["control"] == {
        "paused": True,
        "paused_ts": "2026-08-09T11:00:00+00:00",
        "reason": PAUSE_OPERATOR,
    }
    # The held record is still reported: hiding the queue would answer "why is
    # nothing draining?" with the same silence that made the question necessary.
    assert len(snap.entries) == 1


def test_both_holds_are_reported_independently(tmp_path):
    """A queue can be off *and* paused; telling an operator only one of those sends
    them to fix the wrong switch."""
    snap = queue_snapshot(
        config(tmp_path, eval_auto=False),
        [job("a", STATUS_DEFERRED)],
        control=EvaluationControl(paused=True, reason=PAUSE_OPERATOR),
        now=NOW,
    )

    assert snap.auto_enabled is False
    assert snap.control.paused is True


def test_a_snapshot_with_no_control_supplied_reads_as_running(tmp_path):
    snap = queue_snapshot(config(tmp_path), [], now=NOW)

    assert snap.control == EvaluationControl()
    assert snap.to_dict()["control"]["paused"] is False


def test_a_cancelled_record_is_counted_but_never_listed_as_actionable(tmp_path):
    """A resting, deliberate outcome — the same treatment ``skipped`` gets. The
    count is what keeps a deliberate gap in the corpus answerable; a row would only
    crowd out the jobs that are genuinely stuck."""
    snap = queue_snapshot(
        config(tmp_path),
        [job("a", STATUS_CANCELLED), job("b", STATUS_DEFERRED)],
        now=NOW,
    )

    assert snap.counts[STATUS_CANCELLED] == 1
    assert [e.job.task_id for e in snap.entries] == ["b"]
    # And it is not folded into the skip tally: a task nobody declined at launch.
    assert snap.skipped_by_repo == {}


# -- spacing spans admission, not just the worker's own start -----------------


def test_two_back_to_back_sweeps_promote_only_one_turn(tmp_path):
    """The spacing gap between an admitted turn and its worker's start stamp.

    A promoted job is persisted ``queued`` and its turn submitted off the lock;
    ``started_ts`` appears only when that worker flips it to ``running``. A second
    *sequential* sweep — the resident tick right behind the completion hook's own —
    lands in between, and spacing measured from ``started_ts`` alone would see no
    turn at all and promote a second one at once. Not the concurrent herd the
    single-flight lock refuses: a sequential one, and just as expensive.
    """
    held = DeferredExecutor()
    _, jobs, dispatcher, _, _ = stack(tmp_path, executor=held)  # default 900s spacing
    jobs.enqueue_if_absent(repo(), "2026-08-01-a")
    jobs.enqueue_if_absent(repo(), "2026-08-02-b")

    first = dispatcher.sweep()
    second = dispatcher.sweep()

    assert first is not None and first.task_id == "2026-08-01-a"
    assert second is None
    assert len(held.pending) == 1
    assert jobs.get_job(repo(), "2026-08-02-b").defer_reason == REASON_PACING


def test_an_admitted_manual_turn_paces_the_queue_before_it_starts(tmp_path):
    """Same gap on the manual side, which the queue is also paced against."""
    held = DeferredExecutor()
    clock = Clock()
    _, jobs, dispatcher, _, _ = stack(tmp_path, executor=held, clock=clock)
    manual = jobs.start_job(repo(), "2026-08-01-manual")
    assert manual.status == STATUS_QUEUED and manual.started_ts is None
    jobs.enqueue_if_absent(repo(), TASK)

    assert dispatcher.sweep() is None
    assert jobs.get_job(repo(), TASK).defer_reason == REASON_PACING

    clock.advance(900)
    assert dispatcher.sweep() is not None


# -- the operator view --------------------------------------------------------
#
# `queue_snapshot`'s projection. Most of it runs on hand-built records, since the
# fold is pure; the last test drives the real manager and evaluator instead,
# because what it pins is a divergence only the real overwrite produces — the
# census counts each task's latest *attempt*, which is not the set of tasks
# carrying a report.


def job(
    task_id: str,
    status: str,
    *,
    created: dt.datetime | None = None,
    updated: dt.datetime | None = None,
    repo_name: str = "alpha",
    **fields,
) -> SemanticJob:
    """One persisted-shaped record, without going through the manager."""
    return SemanticJob(
        job_id="J-" + task_id,
        repo_name=repo_name,
        repo_path=REPO_PATH,
        task_id=task_id,
        status=status,
        created_ts=(created or NOW).isoformat(),
        updated_ts=(updated or created or NOW).isoformat(),
        **fields,
    )


def test_the_census_counts_every_status_including_the_zeroes(tmp_path):
    """A count keyed only on observed statuses hides the figures worth stating.

    "Nothing is held" and "nothing failed" are the reassuring readings; if they
    are simply absent from the payload a reader cannot tell them from a surface
    that forgot to look.
    """
    snap = queue_snapshot(
        config(tmp_path),
        [job("a", STATUS_COMPLETED), job("b", STATUS_COMPLETED), job("c", STATUS_DEFERRED)],
        now=NOW,
    )

    assert snap.counts == {
        STATUS_DEFERRED: 1,
        STATUS_QUEUED: 0,
        STATUS_RUNNING: 0,
        STATUS_COMPLETED: 2,
        STATUS_FAILED: 0,
        STATUS_CANCELLED: 0,
        STATUS_SKIPPED: 0,
        STATUS_ERROR: 0,
    }


def test_a_skipped_task_is_counted_and_broken_down_per_repo(tmp_path):
    """The count is what makes the launch-time opt-out admissible at all.

    An opt-out whose entries left no countable trace would hand back, at
    production time, exactly the selection automatic evaluation exists to remove.
    Per repo because that is the shape a thinning corpus has — skips cluster on the
    expensive, remediation-heavy tail rather than spreading evenly.
    """
    snap = queue_snapshot(
        config(tmp_path),
        [
            job("a", STATUS_SKIPPED, repo_name="alpha"),
            job("b", STATUS_SKIPPED, repo_name="beta"),
            job("c", STATUS_SKIPPED, repo_name="beta"),
            job("d", STATUS_COMPLETED, repo_name="beta"),
        ],
        now=NOW,
    )

    assert snap.counts[STATUS_SKIPPED] == 3
    # Most-skipped first, so the repo losing the most reports leads.
    assert list(snap.skipped_by_repo.items()) == [("beta", 2), ("alpha", 1)]


def test_skipped_records_stay_out_of_the_actionable_rows(tmp_path):
    """A deliberate resting outcome has nothing to act on, and there can be many.

    One row per opted-out task would crowd out the jobs that are genuinely stuck —
    the question this list exists to answer.
    """
    snap = queue_snapshot(
        config(tmp_path),
        [job(f"s{i}", STATUS_SKIPPED) for i in range(30)]
        + [job("held", STATUS_DEFERRED)],
        now=NOW,
    )

    assert [e.job.task_id for e in snap.entries] == ["held"]
    assert snap.truncated is False  # the skips are not a truncated page, they are counted
    assert snap.counts[STATUS_SKIPPED] == 30


def test_a_store_with_no_skips_states_nothing_per_repo(tmp_path):
    """Only repos holding a skipped record appear: a zero row would be an invention.

    The job store knows nothing about repos it has no record for, so it cannot
    honestly render "0 skipped" for them.
    """
    snap = queue_snapshot(
        config(tmp_path), [job("a", STATUS_COMPLETED), job("b", STATUS_DEFERRED)], now=NOW
    )

    assert snap.skipped_by_repo == {}
    assert snap.counts[STATUS_SKIPPED] == 0  # the census still states the zero


def test_a_skip_with_no_repo_attribution_is_still_counted(tmp_path):
    """An unreadable record must not shrink the tally silently."""
    snap = queue_snapshot(config(tmp_path), [job("a", STATUS_SKIPPED, repo_name="")], now=NOW)

    assert snap.skipped_by_repo == {"(unattributed)": 1}


def test_an_unknown_status_is_counted_rather_than_dropped(tmp_path):
    """A hand-edited record must not shrink the census silently."""
    snap = queue_snapshot(config(tmp_path), [job("a", "improvised")], now=NOW)

    assert snap.counts["improvised"] == 1
    assert len(snap.entries) == 1


def test_completed_jobs_are_counted_but_not_listed(tmp_path):
    """The healthy outcome is a figure, not a row; rows are what may need action."""
    snap = queue_snapshot(
        config(tmp_path), [job("a", STATUS_COMPLETED), job("b", STATUS_FAILED)], now=NOW
    )

    assert snap.counts[STATUS_COMPLETED] == 1
    assert [e.job.task_id for e in snap.entries] == ["b"]


def test_waiting_jobs_lead_oldest_first_then_failures_newest_first(tmp_path):
    """Two groups with opposite senses: a queue, then a log.

    The waiting group is shown in the dispatcher's own promotion order, so the
    top row is what actually runs next; the settled group leads with the newest
    failure, which is the one still worth reading.
    """
    older = NOW - dt.timedelta(hours=3)
    newer = NOW - dt.timedelta(hours=1)
    snap = queue_snapshot(
        config(tmp_path),
        [
            job("fail-old", STATUS_FAILED, created=older, updated=older),
            job("held-new", STATUS_DEFERRED, created=newer),
            job("running", STATUS_RUNNING, created=newer),
            job("fail-new", STATUS_FAILED, created=older, updated=newer),
            job("held-old", STATUS_DEFERRED, created=older),
        ],
        now=NOW,
    )

    assert [e.job.task_id for e in snap.entries] == [
        "held-old",
        "held-new",
        "running",
        "fail-new",
        "fail-old",
    ]


def test_the_age_is_measured_from_acceptance_not_from_a_turn(tmp_path):
    """How long the task has gone unmeasured, which is the corpus's question."""
    snap = queue_snapshot(
        config(tmp_path),
        [job("a", STATUS_DEFERRED, created=NOW - dt.timedelta(hours=2))],
        now=NOW,
    )

    assert snap.entries[0].age_seconds == 7200


def test_an_unparseable_stamp_reads_as_unknown_age_not_zero(tmp_path):
    record = SemanticJob(
        job_id="J",
        repo_name="alpha",
        repo_path=REPO_PATH,
        task_id="a",
        status=STATUS_DEFERRED,
        created_ts="not-a-timestamp",
    )
    snap = queue_snapshot(config(tmp_path), [record], now=NOW)

    assert snap.entries[0].age_seconds is None
    assert snap.entries[0].overdue is False
    assert snap.overdue == 0


def test_the_overdue_flag_comes_from_the_configured_bound(tmp_path):
    """The threshold is configuration, and the flag is measured against it."""
    cfg = config(tmp_path, eval_queue_max_age=3600)
    records = [
        job("young", STATUS_DEFERRED, created=NOW - dt.timedelta(seconds=3599)),
        job("old", STATUS_DEFERRED, created=NOW - dt.timedelta(seconds=3600)),
    ]

    snap = queue_snapshot(cfg, records, now=NOW)

    assert snap.queue_max_age_seconds == 3600
    assert snap.overdue == 1
    assert {e.job.task_id: e.overdue for e in snap.entries} == {
        "young": False,
        "old": True,
    }


def test_a_disabled_bound_flags_nothing(tmp_path):
    """``0`` disables the bound, per the config contract — not "everything is overdue"."""
    cfg = config(tmp_path, eval_queue_max_age=0)
    snap = queue_snapshot(
        cfg, [job("a", STATUS_DEFERRED, created=NOW - dt.timedelta(days=30))], now=NOW
    )

    assert snap.overdue == 0
    assert snap.entries[0].overdue is False


def test_only_held_jobs_can_be_overdue(tmp_path):
    """The bound has exactly one meaning, shared with the dispatcher's own warning.

    A hung ``queued``/``running`` turn is a different failure with its own
    recovery (restart reconciliation settles it), so it renders its age without
    borrowing the stalled-queue flag.
    """
    cfg = config(tmp_path, eval_queue_max_age=60)
    stale = NOW - dt.timedelta(days=1)
    snap = queue_snapshot(
        cfg,
        [
            job("q", STATUS_QUEUED, created=stale),
            job("r", STATUS_RUNNING, created=stale),
            job("f", STATUS_FAILED, created=stale),
            job("d", STATUS_DEFERRED, created=stale),
        ],
        now=NOW,
    )

    assert snap.overdue == 1
    assert {e.job.task_id for e in snap.entries if e.overdue} == {"d"}


def test_the_stalled_population_matches_the_dispatchers_own_warning(tmp_path):
    """One population, two surfaces: the dashboard and the log must not disagree."""
    clock = Clock()
    cfg, jobs, dispatcher, _, _ = stack(
        tmp_path,
        clock=clock,
        snapshot=usage(window(used=99.0)),  # held on capacity
        eval_queue_max_age=3600,
    )
    jobs.enqueue_if_absent(repo(), "2026-08-01-old")
    clock.advance(3600)
    jobs.enqueue_if_absent(repo(), "2026-08-02-new")
    clock.advance(1)
    dispatcher.sweep()

    warned = {
        task_id for (_repo, task_id, _job_id) in dispatcher._stall_logged
    }
    snap = queue_snapshot(cfg, jobs.list_jobs(), now=clock.now)

    assert warned == {"2026-08-01-old"}
    assert {e.job.task_id for e in snap.entries if e.overdue} == warned


def test_the_entry_cap_never_shrinks_the_counts(tmp_path):
    """The cap bounds the page, never the census — a count that quietly meant "the
    first twenty" would report a twenty-deep queue as calm."""
    records = [
        job(f"t{i:02d}", STATUS_DEFERRED, created=NOW - dt.timedelta(days=1, seconds=i))
        for i in range(5)
    ]

    snap = queue_snapshot(config(tmp_path), records, now=NOW, limit=2)

    assert len(snap.entries) == 2
    assert snap.truncated is True
    assert snap.counts[STATUS_DEFERRED] == 5
    assert snap.overdue == 5  # counted over every held job, not just the shown ones
    assert [e.job.task_id for e in snap.entries] == ["t04", "t03"]  # oldest first


def test_an_uncapped_page_is_not_marked_truncated(tmp_path):
    snap = queue_snapshot(config(tmp_path), [job("a", STATUS_DEFERRED)], now=NOW)

    assert snap.truncated is False
    assert len(snap.entries) == 1


def test_the_view_reports_a_closed_gate_rather_than_hiding_the_queue(tmp_path):
    """A reader asking why nothing drains must see the held records AND the switch.

    A closed gate leaves no trace on the records by design (it is global config,
    not a per-job fact), so it can only be explained from the config echo — and
    hiding the queue while the gate is closed would answer the question with the
    same silence that prompted it.
    """
    records = [job("a", STATUS_DEFERRED)]
    for cfg in (
        config(tmp_path, eval_auto=False),
        config(tmp_path, evaluation_enabled=False),
    ):
        snap = queue_snapshot(cfg, records, now=NOW)
        assert snap.auto_enabled is False
        assert [e.job.task_id for e in snap.entries] == ["a"]

    assert queue_snapshot(config(tmp_path), records, now=NOW).auto_enabled is True


def test_an_empty_store_is_a_calm_snapshot_not_an_error(tmp_path):
    snap = queue_snapshot(config(tmp_path), [], now=NOW)

    assert snap.entries == ()
    assert snap.overdue == 0
    assert snap.truncated is False
    assert set(snap.counts.values()) == {0}


def test_the_snapshot_reads_the_store_without_moving_it(tmp_path):
    """Asking about the queue must never dispatch, hold, or rewrite a record."""
    cfg, jobs, _dispatcher, evaluator, clock = stack(
        tmp_path, snapshot=usage(window(used=99.0))
    )
    jobs.enqueue_if_absent(repo(), TASK)
    path = jobs.jobs_dir / "alpha" / TASK / "job.json"
    before = path.read_bytes()

    snap = queue_snapshot(cfg, jobs.list_jobs(), now=clock.now)

    assert [e.job.status for e in snap.entries] == [STATUS_DEFERRED]
    assert path.read_bytes() == before
    assert evaluator.calls == []


def test_a_report_outlives_the_record_that_produced_it(tmp_path):
    """The census counts attempts, not reports, and the two really do diverge.

    One real evaluator, two attempts: the first completes and persists a report,
    then a manual retry hits a failing backend. The task's single job record is
    overwritten (``start_job`` does that deliberately — an operator asking again
    is a fresh attempt), so the census reads ``0 completed · 1 failed`` while the
    first attempt's ``semantic_report.json`` is still sitting in that task's
    retained evaluation history, reachable from its evaluation view and, once the
    task is archived, from the report feed.

    Both facts are correct and they are about different things: the queue's latest
    attempt failed, and the corpus holds a report for this task. Reading the
    ``completed`` figure as "tasks carrying a report" conflates them and makes the
    displayed number shrink and regrow with retry state, which is why nothing in
    the payload, the docstrings, or the dashboard label claims it — see
    :func:`orch_hub.eval_dispatch.queue_snapshot`. This test is the pin on that
    separation, from the real overwrite rather than a hand-built record.
    """
    cfg = config(tmp_path)
    target = tmp_path / "repos" / "alpha"
    (target / ".ai-tasks").mkdir(parents=True)
    (target / ".ai-tasks" / f"{TASK}.md").write_text(
        f"---\nid: {TASK}\nstatus: in_progress\nsession-est: 1/2\nblockers: []\n---\n\n"
        "# Census honesty\n\n## Goal\ngo\n\n## Scope\n- x\n\n## Acceptance\n- y\n\n"
        "## Session log\n\n### 2026-08-09 / sid-a / (pending → in_progress)\n"
        "- Done: work\n- Next: more\n- Open: none\n",
        encoding="utf-8",
    )
    (target / ".ai-tasks" / "index.md").write_text("# Active tasks\n", encoding="utf-8")
    target_repo = ManagedRepo(
        name="alpha", path=target, manifest=target / ".ai-deploy-manifest.json"
    )
    clock = Clock()
    backend = FlippingBackend()
    service = EvaluationService(cfg, git_runner=_git_unavailable, clock=clock)
    jobs = SemanticJobManager(
        cfg,
        evaluator=SemanticEvaluator(
            cfg, evaluation_service=service, backend=backend, clock=clock
        ),
        clock=clock,
        executor=sync_executor,
    )

    jobs.start_job(target_repo, TASK)
    completed = jobs.get_job(target_repo, TASK)
    assert completed.status == STATUS_COMPLETED
    report = (
        cfg.data_dir
        / "evaluations"
        / "alpha"
        / TASK
        / completed.evaluation_id
        / "semantic_report.json"
    )
    assert report.is_file()

    backend.fail = True
    clock.advance(3600)
    jobs.start_job(target_repo, TASK)
    retried = jobs.get_job(target_repo, TASK)

    assert retried.status == STATUS_FAILED
    # One record per task, so the completed attempt is gone from the store...
    snap = queue_snapshot(cfg, jobs.list_jobs(), now=clock.now)
    assert snap.counts[STATUS_COMPLETED] == 0
    assert snap.counts[STATUS_FAILED] == 1
    assert [e.job.status for e in snap.entries] == [STATUS_FAILED]
    # ...while the report that attempt produced is untouched. The failed retry's
    # cleanup removes only its own reserved dir, never the task's history.
    assert report.is_file()
