"""Tests for the automatic Layer 2 dispatch policy (:mod:`orch_hub.eval_dispatch`).

Covers the capacity gate — a utilization ceiling read from the evaluator
provider's highest currently-measured window, and fail-toward-deferral for every
way that read can be unusable (unavailable, stale, figure-less, rolled over, a
figure measured in an earlier window, a raising reader) — and the dispatcher that
drains the deferred queue: oldest first, at most one per sweep, spaced from the
last turn that actually started (manual turns included), each held job carrying
the bounded machine reason it is still waiting, an unattributable record failed
rather than left resident, and the queue-age bound warning once per job rather
than once per sweep. Every usage snapshot is synthetic; nothing here reads the
operator's real subscription data or spends a model turn.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.eval_dispatch import (
    REASON_CAPACITY,
    REASON_PACING,
    REASON_USAGE_UNKNOWN,
    CapacityGate,
    DeferredDispatcher,
)
from orch_hub.models import (
    PROVIDER_CLAUDE,
    ManagedRepo,
    ProviderUsage,
    SemanticEvaluatorInfo,
    SemanticReport,
    UsageWindow,
)
from orch_hub.semantic_jobs import (
    STATUS_COMPLETED,
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_QUEUED,
    SemanticJobManager,
)

TASK = "2026-08-09-completion-triggered-evaluation"
REPO_PATH = Path("/repos/alpha")
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

    def evaluate(self, r: ManagedRepo, task_id: str) -> SemanticReport:
        self.calls.append((r, task_id))
        return mk_report(f"EVAL-{task_id}")


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

        def evaluate(self, r, task_id):
            reentered.append(self._box["dispatcher"].sweep())
            return super().evaluate(r, task_id)

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
    import json

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
