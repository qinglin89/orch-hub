"""Tests for the async semantic-evaluation job manager (:mod:`orch_hub.semantic_jobs`).

Covers the queued→running→terminal lifecycle over an injected fake evaluator:
start persists ``queued`` and settles to ``completed`` (linking the fresh
``evaluation_id`` and stamping the evaluator identity) or ``failed`` (with a
bounded diagnostic and no ``evaluation_id``); the duplicate guard reuses an
in-flight job for the same ``(repo, task)`` instead of launching a second model
turn; the automatic ``enqueue_if_absent`` entry accepts a task into the
``deferred`` queue without spending a turn and refuses any task that already has a
record (deferred, transient, completed, or failed) so a repeated completion signal
mints no second report while the operator's manual retry stays open; a terminal
job is overwritten by a fresh attempt; state reloads from disk
across manager instances; orphaned transient jobs are reconciled to ``failed`` at
startup while a held ``deferred`` one survives the restart it exists to survive;
the resident in-flight sweep settles a transient record past the turn deadline
plus its grace — freeing the slot without a restart — while leaving a slower turn,
a held job, a settled one, and a record of unprovable age alone;
the deferred-queue operations (promote, hold with a bounded reason, fail an
unattributable record) each act only on the same still-deferred attempt; unsafe
task ids fail closed; a corrupt record surfaces as ``error``
without holding the duplicate slot; and the manager writes only under the
hub-owned ``semantic-jobs/`` subtree. Terminal-outcome notifications are covered
over a fake notifier (never a real Bark backend): one push per settled job,
nothing for transient transitions, duplicate settles, or the startup reconcile
sweep, and delivery failures isolated from the persisted record; an automatic job
completing is silent while an automatic failure still pushes. One integration
test drives a *real* :class:`~orch_hub.semantic.SemanticEvaluator` (fake backend)
through the manager to confirm the completed job links a real evaluation dir and
the target repo is untouched, and a second drives the real Claude backend over a
hanging subprocess to confirm a turn that never returns settles ``failed`` with a
deadline diagnostic. Real-stack tests also pin the post-model boundary: a
settlement the hub cannot write down keeps the attempt non-cancellable, and the
reap — never a cancel — frees its slot. The protected HTTP surfaces live in
``tests/test_api.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from orch_hub import semantic as semantic_module
from orch_hub import semantic_jobs
from orch_hub.config import DEFAULT_EVAL_TURN_TIMEOUT, Config
from orch_hub.evaluation import EvaluationService
from orch_hub.models import ManagedRepo, SemanticEvaluatorInfo, SemanticReport
from orch_hub.notifications import Notification
from orch_hub.semantic import (
    ClaudeSemanticBackend,
    SemanticEvaluationError,
    SemanticEvaluator,
)
from orch_hub.semantic_jobs import (
    SEMANTIC_JOB_NAME,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DEFERRED,
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUSES,
    EVIDENCE_GRACE_SECONDS,
    SETTLE_GRACE_SECONDS,
    SemanticJobManager,
    SemanticJobStateError,
    UnknownSemanticJobError,
    _ERROR_MAX_CHARS,
)
from orch_hub.subprocess_utils import CommandCanceller, CommandResult

TASK = "2026-07-23-semantic-evaluation-async-status"
REPO_PATH = Path("/repos/alpha")
FIXED_TS = dt.datetime(2026, 7, 23, 12, 0, 0, tzinfo=dt.timezone.utc)


# -- helpers ------------------------------------------------------------------


def repo(name: str = "alpha", path: Path = REPO_PATH) -> ManagedRepo:
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def mk_info(backend: str = "fake", model: str | None = "fake-model") -> SemanticEvaluatorInfo:
    return SemanticEvaluatorInfo(backend=backend, model=model, schema_version="1")


def mk_report(evaluation_id: str = "20260723T120000Z", *, info: SemanticEvaluatorInfo | None = None) -> SemanticReport:
    return SemanticReport(
        evaluation_id=evaluation_id,
        generated_ts="2026-07-23T12:00:00+00:00",
        repo_name="alpha",
        task_id=TASK,
        target_present=True,
        target_source="active",
        evaluator=info or mk_info(),
        summary="ok",
        overall_confidence=0.7,
    )


class Clock:
    """A mutable UTC clock so a second attempt can carry a distinct stamp."""

    def __init__(self, now: dt.datetime = FIXED_TS):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + dt.timedelta(seconds=seconds)


class FakeEvaluator:
    """Stands in for :class:`SemanticEvaluator`: the (evaluator_info, evaluate) seam.

    ``on_evaluate`` is a spy hook invoked inside ``evaluate`` (off the manager
    lock) so a test can observe the on-disk ``running`` state mid-turn.
    ``turn_start_by`` deadlines are recorded rather than honoured: enforcing the
    pre-model window is the real evaluator's job (``tests/test_semantic.py``);
    here it is only the value the manager hands down.
    """

    def __init__(self, *, report: SemanticReport | None = None, error: Exception | None = None,
                 on_evaluate=None, info: SemanticEvaluatorInfo | None = None):
        self._report = report if report is not None else mk_report()
        self._error = error
        self._on_evaluate = on_evaluate
        self._info = info or mk_info()
        self.calls: list[tuple[ManagedRepo, str]] = []
        self.deadlines: list[dt.datetime | None] = []
        self.cancels: list[CommandCanceller | None] = []

    @property
    def evaluator_info(self) -> SemanticEvaluatorInfo:
        return self._info

    def evaluate(
        self,
        r: ManagedRepo,
        task_id: str,
        *,
        turn_start_by: dt.datetime | None = None,
        cancel: CommandCanceller | None = None,
    ) -> SemanticReport:
        self.calls.append((r, task_id))
        self.deadlines.append(turn_start_by)
        self.cancels.append(cancel)
        if self._on_evaluate is not None:
            self._on_evaluate(r, task_id)
        if self._error is not None:
            raise self._error
        return self._report


class DeferredExecutor:
    """Captures submitted turns without running them, so a job stays ``queued``."""

    def __init__(self):
        self.pending: list = []

    def __call__(self, run) -> None:
        self.pending.append(run)

    def run_all(self) -> None:
        while self.pending:
            self.pending.pop(0)()


def sync_executor(run) -> None:
    run()


class RecordingNotifier:
    """Stands in for the Bark backend: records pushes, optionally raising."""

    def __init__(self, *, fail: Exception | None = None):
        self.sent: list[Notification] = []
        self._fail = fail

    def notify(self, notification: Notification) -> None:
        self.sent.append(notification)
        if self._fail is not None:
            raise self._fail


class RecordingDispatch:
    """A synchronous notify dispatch that also counts how often it was invoked.

    Counting matters for the disabled case: a ``None`` notifier must spawn *no*
    delivery work at all, not merely deliver nothing.
    """

    def __init__(self):
        self.calls = 0

    def __call__(self, deliver) -> None:
        self.calls += 1
        deliver()


def sync_dispatch(deliver) -> None:
    deliver()


def config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    turn_timeout: int = DEFAULT_EVAL_TURN_TIMEOUT,
) -> Config:
    return Config(
        mandrel_path=tmp_path / "mandrel",
        data_dir=tmp_path / "hub-data",
        evaluation_enabled=enabled,
        eval_turn_timeout=turn_timeout,
    )


def manager(
    tmp_path: Path,
    *,
    evaluator=None,
    clock=None,
    executor=sync_executor,
    notifier=None,
    notify_dispatch=sync_dispatch,
) -> SemanticJobManager:
    return SemanticJobManager(
        config(tmp_path),
        evaluator=evaluator if evaluator is not None else FakeEvaluator(),
        clock=clock or Clock(),
        executor=executor,
        notifier=notifier,
        notify_dispatch=notify_dispatch,
    )


#: The whole-attempt bound :meth:`SemanticJobManager.reconcile_stuck` enforces:
#: the backend's turn deadline plus the two enforced allowances bracketing it —
#: the evidence build before the model turn and the report persistence after it.
STUCK_LIMIT = EVIDENCE_GRACE_SECONDS + DEFAULT_EVAL_TURN_TIMEOUT + SETTLE_GRACE_SECONDS


def dispatch_now(mgr: SemanticJobManager, r: ManagedRepo | None = None, task_id: str = TASK):
    """Promote the task's held job the way the dispatch policy would.

    The policy itself (ceiling, spacing, unknown-usage deferral) is
    ``tests/test_eval_dispatch.py``; here it is only the manager's promote seam.
    """
    return mgr.dispatch_deferred(mgr.get_job(r or repo(), task_id))


def read_job(cfg: Config, repo_name: str, task_id: str) -> dict:
    path = cfg.data_dir / "semantic-jobs" / repo_name / task_id / SEMANTIC_JOB_NAME
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# -- lifecycle: start / running / completed -----------------------------------


def test_start_returns_queued_and_settles_completed(tmp_path):
    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-42"))
    mgr = manager(tmp_path, evaluator=ev)

    returned = mgr.start_job(repo(), TASK)

    # The immediate return is the transient queued record...
    assert returned.status == STATUS_QUEUED
    assert returned.task_id == TASK and returned.repo_name == "alpha"
    # ...and the synchronous executor has already settled it on disk to completed.
    settled = mgr.get_job(repo(), TASK)
    assert settled.status == STATUS_COMPLETED
    assert settled.evaluation_id == "EVAL-42"
    assert ev.calls == [(repo(), TASK)]  # exactly one evaluation turn


def test_queued_job_stamps_evaluator_identity_before_any_report(tmp_path):
    # The queued record names the backend/model so the UI can show what is running
    # before a report exists — stamped from evaluator_info at creation.
    ev = FakeEvaluator(info=mk_info(backend="claude-cli", model="claude-opus-4-8"))
    mgr = manager(tmp_path, evaluator=ev, executor=DeferredExecutor())

    job = mgr.start_job(repo(), TASK)

    assert job.status == STATUS_QUEUED
    assert (job.backend, job.model, job.schema_version) == ("claude-cli", "claude-opus-4-8", "1")


def test_completed_job_reaffirms_identity_from_report_and_sets_timestamps(tmp_path):
    ev = FakeEvaluator(report=mk_report(info=mk_info(backend="claude-cli", model="claude-opus-4-8")))
    clock = Clock()
    mgr = manager(tmp_path, evaluator=ev, clock=clock)

    mgr.start_job(repo(), TASK)
    done = mgr.get_job(repo(), TASK)

    assert done.status == STATUS_COMPLETED
    assert (done.backend, done.model) == ("claude-cli", "claude-opus-4-8")
    # Lifecycle timestamps are all set on a completed run (fixed clock → equal here).
    assert done.created_ts and done.started_ts and done.ended_ts and done.updated_ts


def test_running_state_is_observable_during_the_turn(tmp_path):
    observed: dict = {}
    mgr_ref: dict = {}

    def spy(r, task_id):
        observed["job"] = mgr_ref["mgr"].get_job(r, task_id)

    ev = FakeEvaluator(on_evaluate=spy)
    mgr = manager(tmp_path, evaluator=ev)
    mgr_ref["mgr"] = mgr

    mgr.start_job(repo(), TASK)

    # Mid-turn (spy runs inside evaluate, off the lock) the persisted state is running.
    assert observed["job"].status == STATUS_RUNNING
    assert observed["job"].started_ts is not None and observed["job"].ended_ts is None
    # And it settles to completed afterwards.
    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED


# -- duplicate guard ----------------------------------------------------------


def test_duplicate_while_queued_reuses_job_and_runs_once(tmp_path):
    ev = FakeEvaluator()
    executor = DeferredExecutor()
    mgr = manager(tmp_path, evaluator=ev, executor=executor)

    first = mgr.start_job(repo(), TASK)
    second = mgr.start_job(repo(), TASK)  # still queued: dedup, no second submit

    assert second.job_id == first.job_id
    assert len(executor.pending) == 1  # only the first attempt was submitted
    assert ev.calls == []  # deferred: nothing ran yet

    executor.run_all()
    assert ev.calls == [(repo(), TASK)]  # exactly one model turn overall
    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED


def test_duplicate_while_running_returns_same_job_no_second_turn(tmp_path):
    mgr_ref: dict = {}
    seen: dict = {}

    def spy(r, task_id):
        # Re-entrant start while this turn is running (lock is free here): dedup.
        seen["reused"] = mgr_ref["mgr"].start_job(r, task_id)

    ev = FakeEvaluator(on_evaluate=spy)
    mgr = manager(tmp_path, evaluator=ev)
    mgr_ref["mgr"] = mgr

    started = mgr.start_job(repo(), TASK)

    assert seen["reused"].job_id == started.job_id
    assert seen["reused"].status == STATUS_RUNNING
    assert len(ev.calls) == 1  # the re-entrant start launched no second turn


def test_different_tasks_get_independent_jobs(tmp_path):
    ev = FakeEvaluator()
    mgr = manager(tmp_path, evaluator=ev, executor=DeferredExecutor())
    other = "2026-07-23-other-task"

    j1 = mgr.start_job(repo(), TASK)
    j2 = mgr.start_job(repo(), other)  # different task: not deduped

    assert j1.task_id == TASK and j2.task_id == other
    assert (mgr._config.data_dir / "semantic-jobs" / "alpha" / TASK / SEMANTIC_JOB_NAME).is_file()
    assert (mgr._config.data_dir / "semantic-jobs" / "alpha" / other / SEMANTIC_JOB_NAME).is_file()


def test_new_attempt_after_terminal_overwrites_single_record(tmp_path):
    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-1"))
    clock = Clock()
    mgr = manager(tmp_path, evaluator=ev, clock=clock)

    mgr.start_job(repo(), TASK)
    first = mgr.get_job(repo(), TASK)
    assert first.status == STATUS_COMPLETED and first.evaluation_id == "EVAL-1"

    # A fresh attempt is legal now the prior one is terminal; advance the clock so
    # the new attempt carries a distinct id, and hand back a new evaluation id.
    clock.advance(60)
    ev2 = FakeEvaluator(report=mk_report(evaluation_id="EVAL-2"))
    mgr2 = SemanticJobManager(mgr._config, evaluator=ev2, clock=clock, executor=sync_executor)
    mgr2.start_job(repo(), TASK)
    second = mgr2.get_job(repo(), TASK)

    assert second.status == STATUS_COMPLETED and second.evaluation_id == "EVAL-2"
    assert second.job_id != first.job_id
    # Still exactly one record dir for this (repo, task) — the attempt overwrote it.
    task_dir = mgr._config.data_dir / "semantic-jobs" / "alpha" / TASK
    assert [p.name for p in task_dir.iterdir()] == [SEMANTIC_JOB_NAME]


# -- the automatic (completion-triggered) accept path --------------------------
# ``enqueue_if_absent`` is the once-per-task guard the automatic trigger uses:
# any existing record — held, transient, or terminal — means the task's L2 story
# is already told, in flight, or queued, so it accepts nothing and returns None.
# Stricter than ``start_job``, which is an operator explicitly asking for a fresh
# attempt. It also does not *start* anything: acceptance is durable, admission is
# the dispatch policy's call.


def test_enqueue_if_absent_accepts_the_first_attempt_without_spending_a_turn(tmp_path):
    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-AUTO"))
    mgr = manager(tmp_path, evaluator=ev)

    job = mgr.enqueue_if_absent(repo(), TASK)

    assert job is not None and job.status == STATUS_DEFERRED
    assert job.auto is True
    # Durably accepted, and no model turn: admission is a separate decision.
    assert mgr.get_job(repo(), TASK).status == STATUS_DEFERRED
    assert ev.calls == []

    dispatch_now(mgr)

    settled = mgr.get_job(repo(), TASK)
    assert settled.status == STATUS_COMPLETED
    assert settled.evaluation_id == "EVAL-AUTO"
    assert settled.auto is True  # provenance survives the promotion
    assert ev.calls == [(repo(), TASK)]


def test_a_promoted_job_keeps_its_acceptance_identity(tmp_path):
    """One acceptance is one attempt, however long the policy holds it."""
    clock = Clock()
    executor = DeferredExecutor()
    mgr = manager(tmp_path, clock=clock, executor=executor)
    accepted = mgr.enqueue_if_absent(repo(), TASK)

    clock.advance(4 * 3600)
    promoted = dispatch_now(mgr)

    assert promoted is not None
    assert promoted.job_id == accepted.job_id
    assert promoted.created_ts == accepted.created_ts
    assert promoted.status == STATUS_QUEUED
    assert promoted.defer_reason is None


@pytest.mark.parametrize("second_call_is_absent_path", [True, False])
def test_enqueue_if_absent_refuses_a_task_with_a_completed_record(
    tmp_path, second_call_is_absent_path
):
    """A repeated completion signal must not mint a second report for one task.

    The signal is per run, not per task: a task that completes without being
    archived stays launchable, and the close-out-only re-run that follows
    classifies ``completed`` again. The durable record is the guard — and the
    operator's ``start_job`` deliberately still overwrites it (the parametrized
    control), so a manual re-run remains possible.
    """
    ev = FakeEvaluator()
    mgr = manager(tmp_path, evaluator=ev)
    first = mgr.enqueue_if_absent(repo(), TASK)
    dispatch_now(mgr)
    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED

    if second_call_is_absent_path:
        assert mgr.enqueue_if_absent(repo(), TASK) is None
        assert len(ev.calls) == 1  # no second model turn
        assert mgr.get_job(repo(), TASK).job_id == first.job_id
    else:
        again = mgr.start_job(repo(), TASK)
        assert again is not None
        assert len(ev.calls) == 2  # the manual path is still a fresh attempt


def test_enqueue_if_absent_refuses_while_a_job_is_held_or_in_flight(tmp_path):
    ev = FakeEvaluator()
    executor = DeferredExecutor()
    mgr = manager(tmp_path, evaluator=ev, executor=executor)

    assert mgr.enqueue_if_absent(repo(), TASK) is not None
    assert mgr.enqueue_if_absent(repo(), TASK) is None  # still held
    assert executor.pending == []  # acceptance starts nothing

    dispatch_now(mgr)
    assert mgr.enqueue_if_absent(repo(), TASK) is None  # now queued

    assert len(executor.pending) == 1
    executor.run_all()
    assert len(ev.calls) == 1


def test_enqueue_if_absent_refuses_after_a_failed_record(tmp_path):
    """Recorded, not retried forever: the automatic path never re-attempts.

    A failed record still holds the task's slot, so the trigger will not spend a
    second turn on it; retrying is the operator's explicit ``start_job``.
    """
    ev = FakeEvaluator(error=SemanticEvaluationError("backend down"))
    mgr = manager(tmp_path, evaluator=ev)

    mgr.enqueue_if_absent(repo(), TASK)
    dispatch_now(mgr)
    assert mgr.get_job(repo(), TASK).status == STATUS_FAILED

    assert mgr.enqueue_if_absent(repo(), TASK) is None
    assert len(ev.calls) == 1


def test_enqueue_if_absent_sees_a_record_written_by_an_earlier_process(tmp_path):
    """The guard is the persisted record, so it survives a hub restart."""
    mgr = manager(tmp_path)
    mgr.enqueue_if_absent(repo(), TASK)

    fresh_evaluator = FakeEvaluator()
    reborn = SemanticJobManager(
        mgr._config,
        evaluator=fresh_evaluator,
        clock=Clock(),
        executor=sync_executor,
    )

    assert reborn.enqueue_if_absent(repo(), TASK) is None
    assert fresh_evaluator.calls == []


def test_enqueue_if_absent_rejects_an_unsafe_task_id(tmp_path):
    mgr = manager(tmp_path)
    with pytest.raises(ValueError):
        mgr.enqueue_if_absent(repo(), "../evil")


# ``skip_if_absent`` is the opt-out twin of the above: same once-per-task guard,
# same durable write, but the record comes to rest terminal instead of waiting for
# a turn. A skip has to leave a record — an opt-out whose entries simply vanished
# would give back, at production time, the selection automatic evaluation exists
# to remove, and on the dimension (quota spend) that makes skips cluster on the
# expensive tail rather than thinning a corpus evenly.


def test_skip_if_absent_records_a_terminal_skip_without_a_turn(tmp_path):
    ev = FakeEvaluator()
    mgr = manager(tmp_path, evaluator=ev)

    job = mgr.skip_if_absent(repo(), TASK, semantic_jobs.SKIP_RUN_OPT_OUT)

    assert job is not None
    assert job.status == semantic_jobs.STATUS_SKIPPED
    assert job.skip_reason == semantic_jobs.SKIP_RUN_OPT_OUT
    assert job.auto is True  # produced mechanically, like every completion record
    assert job.started_ts is None  # no turn ever began
    assert job.ended_ts == job.created_ts  # nothing later will settle it
    assert ev.calls == []
    # Durable and reloadable, in the same store as every other record.
    assert mgr.get_job(repo(), TASK).status == semantic_jobs.STATUS_SKIPPED
    assert read_job(mgr._config, "alpha", TASK)["skip_reason"] == "run-opt-out"


def test_a_skip_carries_the_evaluator_identity_it_declined(tmp_path):
    """Which evaluator's denominator lost this task is the useful reading."""
    mgr = manager(tmp_path, evaluator=FakeEvaluator())

    job = mgr.skip_if_absent(repo(), TASK, semantic_jobs.SKIP_RUN_OPT_OUT)

    assert (job.backend, job.model, job.schema_version) == ("fake", "fake-model", "1")


def test_a_skip_reason_outside_the_closed_vocabulary_is_refused(tmp_path):
    """The count is the point; free text would make it unanalyzable.

    Operator prose especially: it leaks repo content into a persisted surface and
    invites recording the rationalization that is itself the bias.
    """
    mgr = manager(tmp_path)

    with pytest.raises(ValueError):
        mgr.skip_if_absent(repo(), TASK, "we were busy this week")

    assert mgr.find_job(repo(), TASK) is None  # nothing persisted


def test_a_skip_is_not_pushed(tmp_path):
    """A failure is the corpus shrinking unexpectedly; a skip is what was asked for."""
    notifier = RecordingNotifier()
    mgr = manager(tmp_path, notifier=notifier)

    mgr.skip_if_absent(repo(), TASK, semantic_jobs.SKIP_RUN_OPT_OUT)

    assert notifier.sent == []


@pytest.mark.parametrize(
    "seed",
    ["deferred", "queued", "completed", "failed", "skipped"],
)
def test_skip_if_absent_refuses_any_task_that_already_has_a_record(tmp_path, seed):
    """Whichever run completes the task first writes the record and decides.

    The same guard in both directions is what keeps a multi-run task from being
    decided twice: the box on a session that does not finish the task never
    reaches this path at all, and a later completing run cannot overwrite what the
    first one settled.
    """
    executor = DeferredExecutor()
    ev = FakeEvaluator(
        error=SemanticEvaluationError("backend down") if seed == "failed" else None
    )
    mgr = manager(tmp_path, evaluator=ev, executor=executor)
    if seed == "skipped":
        mgr.skip_if_absent(repo(), TASK, semantic_jobs.SKIP_RUN_OPT_OUT)
    else:
        mgr.enqueue_if_absent(repo(), TASK)
        if seed != "deferred":
            dispatch_now(mgr)
        if seed in ("completed", "failed"):
            executor.run_all()
    before = read_job(mgr._config, "alpha", TASK)

    assert mgr.skip_if_absent(repo(), TASK, semantic_jobs.SKIP_RUN_OPT_OUT) is None
    assert read_job(mgr._config, "alpha", TASK) == before


def test_enqueue_if_absent_refuses_a_task_already_recorded_as_skipped(tmp_path):
    """The reverse direction of the same guard: a later run cannot silently undo it.

    Un-skipping is an operator decision (``start_job``), never a mechanical one —
    otherwise a second completing run of the same task would quietly reverse the
    first one's choice.
    """
    ev = FakeEvaluator()
    mgr = manager(tmp_path, evaluator=ev)
    mgr.skip_if_absent(repo(), TASK, semantic_jobs.SKIP_RUN_OPT_OUT)

    assert mgr.enqueue_if_absent(repo(), TASK) is None
    assert mgr.get_job(repo(), TASK).status == semantic_jobs.STATUS_SKIPPED
    assert ev.calls == []


def test_an_operator_can_still_evaluate_a_skipped_task(tmp_path):
    """Asking undoes a skip in the only direction that matters: a report exists.

    That is the corpus gaining an entry, not losing one, so the manual path treats
    a skipped record like any other terminal one.
    """
    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-AFTER-SKIP"))
    mgr = manager(tmp_path, evaluator=ev)
    mgr.skip_if_absent(repo(), TASK, semantic_jobs.SKIP_RUN_OPT_OUT)

    job = mgr.start_job(repo(), TASK)

    assert job.auto is False  # somebody chose this one
    settled = mgr.get_job(repo(), TASK)
    assert settled.status == STATUS_COMPLETED
    assert settled.evaluation_id == "EVAL-AFTER-SKIP"
    assert settled.skip_reason is None  # a fresh attempt, not an annotated skip


def test_the_startup_sweep_leaves_a_skipped_record_alone(tmp_path):
    """It is terminal: there is no abandoned turn to settle."""
    mgr = manager(tmp_path)
    mgr.skip_if_absent(repo(), TASK, semantic_jobs.SKIP_RUN_OPT_OUT)
    before = read_job(mgr._config, "alpha", TASK)

    assert mgr.reconcile_orphans() == []
    assert read_job(mgr._config, "alpha", TASK) == before


def test_a_manual_start_overrides_a_held_job(tmp_path):
    """The documented escape hatch: an operator asking now is never capacity-gated.

    A held job is waiting on a policy over subscription usage. An operator who
    clicks evaluate is not the production-side selection that policy exists to
    remove, and a broken usage feed holds the queue forever — so ``start_job``
    overwrites a ``deferred`` record exactly as it overwrites a terminal one, and
    stamps the fresh attempt as operator-chosen.
    """
    ev = FakeEvaluator()
    mgr = manager(tmp_path, evaluator=ev)
    mgr.enqueue_if_absent(repo(), TASK)
    assert mgr.get_job(repo(), TASK).auto is True

    started = mgr.start_job(repo(), TASK)

    assert started.status == STATUS_QUEUED
    assert started.auto is False
    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED
    assert len(ev.calls) == 1


# -- the deferred-queue seams -------------------------------------------------
# Mechanism only: every one of these acts on the *same still-deferred attempt* or
# does nothing, so the policy driving them can never clobber a record something
# else already moved on.


def test_holding_records_a_bounded_reason_and_does_not_rewrite_an_unchanged_one(tmp_path):
    clock = Clock()
    mgr = manager(tmp_path, clock=clock)
    job = mgr.enqueue_if_absent(repo(), TASK)

    clock.advance(60)
    held = mgr.hold_deferred(job, "capacity")
    assert held is not None and held.defer_reason == "capacity"
    assert held.status == STATUS_DEFERRED
    first_update = held.updated_ts

    clock.advance(60)
    assert mgr.hold_deferred(job, "capacity") is None  # unchanged: no write
    assert mgr.get_job(repo(), TASK).updated_ts == first_update

    changed = mgr.hold_deferred(job, "pacing")
    assert changed is not None and changed.defer_reason == "pacing"


def test_the_deferred_seams_refuse_a_record_that_moved_on(tmp_path):
    """A manual attempt that superseded a held job must not be clobbered."""
    executor = DeferredExecutor()
    mgr = manager(tmp_path, executor=executor)
    stale = mgr.enqueue_if_absent(repo(), TASK)
    mgr.start_job(repo(), TASK)  # supersedes it with a fresh queued attempt

    assert mgr.dispatch_deferred(stale) is None
    assert mgr.hold_deferred(stale, "capacity") is None
    assert mgr.fail_deferred(stale, "gone") is None
    assert mgr.get_job(repo(), TASK).status == STATUS_QUEUED


def test_an_unattributable_held_job_is_failed_rather_than_left_in_the_queue(tmp_path):
    """A record whose repo is gone can never dispatch; it must not stay invisible."""
    mgr = manager(tmp_path)
    job = mgr.enqueue_if_absent(repo(), TASK)
    orphan = replace(job, repo_path=None)

    assert mgr.dispatch_deferred(orphan) is None  # refuses to guess a target

    failed = mgr.fail_deferred(job, "no repo attribution")
    assert failed is not None and failed.status == STATUS_FAILED
    assert failed.error == "no repo attribution"
    assert failed.ended_ts is not None
    assert mgr.get_job(repo(), TASK).status == STATUS_FAILED


def test_list_jobs_walks_every_record_once(tmp_path):
    mgr = manager(tmp_path)
    mgr.enqueue_if_absent(repo("alpha"), TASK)
    mgr.enqueue_if_absent(repo("beta", Path("/repos/beta")), "2026-08-01-other")

    listed = mgr.list_jobs()

    assert {(j.repo_name, j.task_id, j.status) for j in listed} == {
        ("alpha", TASK, STATUS_DEFERRED),
        ("beta", "2026-08-01-other", STATUS_DEFERRED),
    }


def test_the_manager_free_read_returns_the_same_records_as_the_manager(tmp_path):
    """``list_persisted_jobs`` is the walk a process owning no manager takes.

    It exists for the CLI queue read: the manager's constructor requires an
    evaluator, so a read-only surface would otherwise have to build the whole
    Layer 2 chain to walk a directory of small JSON files — and would then be
    holding a manager that could start a turn it has no business starting. The
    guarantee worth pinning is that it is the *same* read, so the two surfaces
    can never report different queues.
    """
    mgr = manager(tmp_path)
    mgr.enqueue_if_absent(repo("alpha"), TASK)
    mgr.enqueue_if_absent(repo("beta", Path("/repos/beta")), "2026-08-01-other")
    # An unreadable record must surface as its `error` record here too: a broken
    # dir that hides from the operator surface is the failure this store's own
    # loader refuses to allow.
    broken = config(tmp_path).data_dir / "semantic-jobs" / "gamma" / "2026-08-02-x"
    broken.mkdir(parents=True)
    (broken / "job.json").write_text("{not json", encoding="utf-8")

    assert semantic_jobs.list_persisted_jobs(config(tmp_path)) == mgr.list_jobs()
    assert {j.status for j in mgr.list_jobs()} == {STATUS_DEFERRED, STATUS_ERROR}


def test_the_manager_free_read_of_an_absent_store_creates_nothing(tmp_path):
    cfg = config(tmp_path)

    assert semantic_jobs.list_persisted_jobs(cfg) == []
    assert not semantic_jobs.jobs_root(cfg).exists()
    assert not cfg.data_dir.exists()


@pytest.mark.parametrize("blocked", ["the jobs root", "the data dir"])
def test_a_strict_read_refuses_a_store_path_that_is_not_a_directory(tmp_path, blocked):
    """Strict narrows the empty result to genuine absence, nothing else.

    ``_job_dirs_under`` opens on ``Path.is_dir()``, which is False for an absent
    tree *and* for a path it could not stat *and* for a non-directory standing at
    the path — so without this gate a store nobody could walk reads as a store
    holding nothing. A caller reporting an absence as its headline answer (the CLI
    queue read) has to be able to tell those apart.
    """
    cfg = config(tmp_path)
    root = semantic_jobs.jobs_root(cfg)
    if blocked == "the jobs root":
        root.parent.mkdir(parents=True)
        root.write_text("not a queue", encoding="utf-8")
    else:
        cfg.data_dir.write_text("not a hub data dir", encoding="utf-8")

    with pytest.raises(OSError):
        semantic_jobs.list_persisted_jobs(cfg, strict=True)


@pytest.mark.parametrize("blocked", ["the jobs root", "the data dir"])
def test_the_default_read_is_unchanged_for_the_manager_s_callers(tmp_path, blocked):
    """The gate is opt-in: the in-process readers behave exactly as before.

    The dispatch sweep, the HTTP queue route, and the startup reconcile all reach
    the store through the manager, and all three want the records they can see —
    a store path that will not open is a no-op for them, not a crash on a monitor
    tick or at lifespan start. Turning the shared walk strict would have made it
    one, which is why the strictness is a parameter the CLI asks for.
    """
    cfg = config(tmp_path)
    root = semantic_jobs.jobs_root(cfg)
    if blocked == "the jobs root":
        root.parent.mkdir(parents=True)
        root.write_text("not a queue", encoding="utf-8")
    else:
        cfg.data_dir.write_text("not a hub data dir", encoding="utf-8")

    assert semantic_jobs.list_persisted_jobs(cfg) == []


# -- operator cancel ----------------------------------------------------------
#
# The per-job lever, modelled on the intake manager's cancel. What it must get
# right: freeing the (repo, task) slot without a restart, actually preventing the
# model turn where one has not opened yet, refusing a resting record rather than
# rewriting it, and staying quiet — a cancel is the operator's own action, not a
# corpus loss they need to be told about.


def test_cancelling_a_held_job_settles_it_and_frees_the_slot(tmp_path):
    mgr = manager(tmp_path)
    mgr.enqueue_if_absent(repo(), TASK)

    cancelled = mgr.cancel_job(repo(), TASK)

    assert cancelled.status == STATUS_CANCELLED
    assert cancelled.ended_ts is not None
    # The waiting reason is no longer true and would read as the cause of the
    # cancel; the status is the cause.
    assert cancelled.defer_reason is None
    assert mgr.get_job(repo(), TASK).status == STATUS_CANCELLED


def test_cancelling_a_queued_job_stops_the_model_turn_before_it_opens(tmp_path):
    """The case where cancelling is a real cancellation rather than bookkeeping.

    A queued record's worker is already submitted but has not begun. The
    queued→running guard refuses a record this settled, so no turn opens and no
    window is spent.
    """
    ev = FakeEvaluator()
    executor = DeferredExecutor()
    mgr = manager(tmp_path, evaluator=ev, executor=executor)
    mgr.start_job(repo(), TASK)

    mgr.cancel_job(repo(), TASK)
    executor.run_all()  # the worker finally gets its slot

    assert ev.calls == []
    assert mgr.get_job(repo(), TASK).status == STATUS_CANCELLED


def test_cancelling_a_running_job_stops_the_turn_it_reports_stopping(tmp_path):
    """From ``running`` the cancel must reach the turn, not just the record.

    A record settled ``cancelled`` while its model turn ran on would free the slot
    and report the spending stopped while the subscription window kept draining —
    and a manual retry could then run beside it. So the manager registers each
    in-flight attempt's canceller and fires it here; the worker's own finalize then
    finds a non-transient record and returns, which is what keeps the cancel
    standing. ``started_ts`` still tells a turn that had opened from one that never
    did.
    """
    box: dict = {}
    ev = FakeEvaluator(
        on_evaluate=lambda r, t: box.setdefault("cancelled", mgr.cancel_job(r, t))
    )
    mgr = manager(tmp_path, evaluator=ev)

    mgr.start_job(repo(), TASK)  # synchronous executor: the turn runs inline

    assert box["cancelled"].status == STATUS_CANCELLED
    assert box["cancelled"].started_ts is not None
    # The handle the turn was given is fired, so a real backend's child is killed
    # rather than left spending the window the cancel was protecting.
    assert ev.cancels[-1] is not None and ev.cancels[-1].cancelled is True
    settled = mgr.get_job(repo(), TASK)
    assert settled.status == STATUS_CANCELLED
    assert settled.evaluation_id is None


def test_a_cancel_after_the_turn_returned_has_nothing_left_to_stop(tmp_path):
    """A settled record refuses a cancel on its status alone, handle or no handle.

    The plainest shape of "too late": by the time the operator asked, the job was
    at rest. Nothing was ever fired at the attempt, which is also why a stale
    registration can never reach a *later* attempt's process.
    """
    ev = FakeEvaluator()
    mgr = manager(tmp_path, evaluator=ev)
    mgr.start_job(repo(), TASK)  # synchronous executor: the turn runs to completion

    with pytest.raises(SemanticJobStateError):
        mgr.cancel_job(repo(), TASK)  # already at rest

    assert ev.cancels[-1].cancelled is False


class ReleaseWatchingManager(SemanticJobManager):
    """Records the record's status at the instant each attempt handle is withdrawn."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.status_at_release: list[str] = []

    def _release_attempt(self, job_dir, job_id):
        self.status_at_release.append(self.get_job(repo(), TASK).status)
        super()._release_attempt(job_dir, job_id)


def test_an_attempt_holds_its_handle_until_its_record_settles(tmp_path):
    """The handle is the attempt's cancel boundary, not just a way to kill a child.

    It carries the seal the turn takes as it returns, so a cancel that finds it
    is refused while the worker persists and publishes. Withdrawing it when the
    *turn* ends rather than when the *record* does would therefore re-open, for
    exactly the width of a finalize, the window this whole boundary closes: a
    cancel landing there would find nothing registered, be accepted, and write
    ``cancelled`` over an evaluation already on disk and in the feed.
    """
    mgr = ReleaseWatchingManager(
        config(tmp_path),
        evaluator=FakeEvaluator(),
        clock=Clock(),
        executor=sync_executor,
        notify_dispatch=sync_dispatch,
    )

    mgr.start_job(repo(), TASK)  # synchronous executor: the turn runs inline

    assert mgr.status_at_release == [STATUS_COMPLETED]


def test_a_cancelled_attempt_cannot_settle_the_retry_that_replaced_it(tmp_path):
    """Cancel, then ask again in the same second, while the old worker finishes.

    The abandoned worker still finalizes, and it finds the *replacement's* record
    sitting transient in the same place. What must stop it is per-attempt identity:
    with the id minted from the clock alone, two attempts inside one second are
    indistinguishable, so the late finalize settles the retry with the cancelled
    attempt's evaluation and the retry's own worker then aborts against a record it
    no longer owns — the corpus keeping a report nobody asked for and losing the
    one somebody did.
    """
    executor = DeferredExecutor()
    clock = Clock()  # never advanced: both attempts are minted in one second
    box: dict = {}

    class Evaluator:
        """Cancels its own first turn and immediately asks for another."""

        def __init__(self):
            self.calls: list[str] = []

        @property
        def evaluator_info(self) -> SemanticEvaluatorInfo:
            return mk_info()

        def evaluate(self, r, task_id, *, turn_start_by=None, cancel=None):
            self.calls.append(task_id)
            if len(self.calls) == 1:
                box["cancelled"] = mgr.cancel_job(r, task_id)
                # Queued behind this worker in the same executor, so the retry is
                # still transient when the cancelled attempt finalizes below.
                box["retry"] = mgr.start_job(r, task_id)
            return mk_report(evaluation_id=f"EVAL-{len(self.calls)}")

    ev = Evaluator()
    mgr = SemanticJobManager(
        config(tmp_path), evaluator=ev, clock=clock, executor=executor
    )
    mgr.start_job(repo(), TASK)

    executor.run_all()  # attempt 1 finalizes late, then attempt 2 runs

    assert box["cancelled"].job_id != box["retry"].job_id
    assert ev.calls == [TASK, TASK]  # the retry really ran its own turn
    settled = mgr.get_job(repo(), TASK)
    assert settled.job_id == box["retry"].job_id
    assert settled.status == STATUS_COMPLETED
    assert settled.evaluation_id == "EVAL-2"  # never the cancelled attempt's


def test_every_attempt_has_its_own_id_inside_one_clock_second(tmp_path):
    """The id is per attempt, not per second: the finalize guards key on it."""
    clock = Clock()
    mgr = manager(tmp_path, clock=clock)

    first = mgr.start_job(repo(), TASK)
    second = mgr.start_job(repo(), TASK)

    assert first.job_id != second.job_id
    assert first.job_id.startswith("20260723T120000Z-")


def test_an_unrecognized_status_is_refused_rather_than_rewritten(tmp_path):
    """A status this build does not know is preserved by the loader on purpose —
    a record from a newer build, or a damaged one, stays visible instead of being
    normalized away. Cancelling from an allowlist keeps it that way; "not terminal"
    would have overwritten it with a lifecycle nobody here understands."""
    mgr = manager(tmp_path)
    mgr.enqueue_if_absent(repo(), TASK)
    job_dir = config(tmp_path).data_dir / "semantic-jobs" / "alpha" / TASK
    semantic_jobs._write_record(
        job_dir, replace(mgr.get_job(repo(), TASK), status="reticulating")
    )

    with pytest.raises(SemanticJobStateError):
        mgr.cancel_job(repo(), TASK)

    assert mgr.get_job(repo(), TASK).status == "reticulating"


@pytest.mark.parametrize(
    "seed",
    [STATUS_COMPLETED, STATUS_FAILED, STATUS_SKIPPED, STATUS_CANCELLED],
)
def test_cancelling_a_resting_record_is_refused_rather_than_rewritten(tmp_path, seed):
    """A resting record has nothing to stop. Refusing is 409 territory — the job is
    right there, so this is about lifecycle, not identity."""
    mgr = manager(tmp_path)
    mgr.enqueue_if_absent(repo(), TASK)
    job_dir = config(tmp_path).data_dir / "semantic-jobs" / "alpha" / TASK
    semantic_jobs._write_record(
        job_dir, replace(mgr.get_job(repo(), TASK), status=seed)
    )

    with pytest.raises(SemanticJobStateError):
        mgr.cancel_job(repo(), TASK)

    assert mgr.get_job(repo(), TASK).status == seed


def test_cancelling_a_task_with_no_job_is_unknown_not_a_new_record(tmp_path):
    mgr = manager(tmp_path)

    with pytest.raises(UnknownSemanticJobError):
        mgr.cancel_job(repo(), TASK)

    assert mgr.find_job(repo(), TASK) is None


def test_cancelling_rejects_an_unsafe_task_id(tmp_path):
    mgr = manager(tmp_path)

    with pytest.raises(ValueError):
        mgr.cancel_job(repo(), "../escape")


def test_a_cancel_is_not_pushed(tmp_path):
    """Unlike a failure, a cancel is exactly what the operator asked for, at the
    surface that already shows the outcome. The same silence a skip keeps."""
    notifier = RecordingNotifier()
    mgr = manager(tmp_path, notifier=notifier)
    mgr.enqueue_if_absent(repo(), TASK)

    mgr.cancel_job(repo(), TASK)

    assert notifier.sent == []


def test_a_cancelled_record_holds_the_once_per_task_guard(tmp_path):
    """The automatic path must not quietly re-enqueue what was just stopped: the
    record is the guard, exactly as a skip is."""
    mgr = manager(tmp_path)
    mgr.enqueue_if_absent(repo(), TASK)
    mgr.cancel_job(repo(), TASK)

    assert mgr.enqueue_if_absent(repo(), TASK) is None
    assert mgr.get_job(repo(), TASK).status == STATUS_CANCELLED


def test_an_operator_can_still_evaluate_a_cancelled_task(tmp_path):
    """Cancelling is not a verdict on the task. Asking again produces the report the
    cancel declined — the corpus gaining an entry, not losing one."""
    ev = FakeEvaluator()
    mgr = manager(tmp_path, evaluator=ev)
    mgr.enqueue_if_absent(repo(), TASK)
    mgr.cancel_job(repo(), TASK)

    started = mgr.start_job(repo(), TASK)

    assert started.auto is False  # somebody chose this one
    settled = mgr.get_job(repo(), TASK)
    assert settled.status == STATUS_COMPLETED
    assert ev.calls == [(repo(), TASK)]


def test_the_startup_sweep_leaves_a_cancelled_record_alone(tmp_path):
    """Terminal records are not orphans; only a transient one is."""
    mgr = manager(tmp_path)
    mgr.enqueue_if_absent(repo(), TASK)
    mgr.cancel_job(repo(), TASK)

    assert mgr.reconcile_orphans() == []
    assert mgr.get_job(repo(), TASK).status == STATUS_CANCELLED


# -- failure paths ------------------------------------------------------------


def test_failure_records_bounded_diagnostic_and_no_evaluation_id(tmp_path):
    ev = FakeEvaluator(error=SemanticEvaluationError("model process crashed"), info=mk_info(backend="claude-cli"))
    mgr = manager(tmp_path, evaluator=ev)

    mgr.start_job(repo(), TASK)
    failed = mgr.get_job(repo(), TASK)

    assert failed.status == STATUS_FAILED
    assert failed.evaluation_id is None
    assert "model process crashed" in (failed.error or "")
    # Identity stamped at creation is preserved on failure (which model was tried).
    assert failed.backend == "claude-cli"
    assert failed.ended_ts is not None


def test_failure_diagnostic_is_length_bounded(tmp_path):
    huge = "x" * (_ERROR_MAX_CHARS * 3)
    ev = FakeEvaluator(error=RuntimeError(huge))
    mgr = manager(tmp_path, evaluator=ev)

    mgr.start_job(repo(), TASK)
    failed = mgr.get_job(repo(), TASK)

    assert failed.status == STATUS_FAILED
    assert len(failed.error) <= _ERROR_MAX_CHARS  # truncated, never unbounded output


def test_retry_after_failure_starts_fresh_attempt(tmp_path):
    clock = Clock()
    failing = FakeEvaluator(error=SemanticEvaluationError("boom"))
    mgr = SemanticJobManager(config(tmp_path), evaluator=failing, clock=clock, executor=sync_executor)
    mgr.start_job(repo(), TASK)
    assert mgr.get_job(repo(), TASK).status == STATUS_FAILED

    # A retry (the scope's retry path) is just a fresh start over the terminal job.
    clock.advance(30)
    ok = FakeEvaluator(report=mk_report(evaluation_id="EVAL-OK"))
    mgr2 = SemanticJobManager(mgr._config, evaluator=ok, clock=clock, executor=sync_executor)
    mgr2.start_job(repo(), TASK)
    assert mgr2.get_job(repo(), TASK).status == STATUS_COMPLETED
    assert mgr2.get_job(repo(), TASK).evaluation_id == "EVAL-OK"


# -- persistence / reload -----------------------------------------------------


def test_state_reloads_from_disk_across_manager_instances(tmp_path):
    cfg = config(tmp_path)
    mgr = SemanticJobManager(cfg, evaluator=FakeEvaluator(report=mk_report(evaluation_id="EVAL-R")), clock=Clock(), executor=sync_executor)
    mgr.start_job(repo(), TASK)

    # A completely fresh manager over the same data dir sees the persisted job.
    fresh = SemanticJobManager(cfg, evaluator=FakeEvaluator(), clock=Clock(), executor=sync_executor)
    reloaded = fresh.get_job(repo(), TASK)
    assert reloaded.status == STATUS_COMPLETED
    assert reloaded.evaluation_id == "EVAL-R"


def test_find_job_returns_none_for_never_evaluated_task(tmp_path):
    mgr = manager(tmp_path)
    assert mgr.find_job(repo(), "2026-07-23-nope") is None
    with pytest.raises(UnknownSemanticJobError):
        mgr.get_job(repo(), "2026-07-23-nope")


# -- reconciliation -----------------------------------------------------------


def _write_raw_job(cfg: Config, repo_name: str, task_id: str, status: str, **extra) -> Path:
    job_dir = cfg.data_dir / "semantic-jobs" / repo_name / task_id
    job_dir.mkdir(parents=True)
    payload = {"job_id": "J1", "repo_name": repo_name, "repo_path": str(REPO_PATH),
               "task_id": task_id, "status": status, **extra}
    (job_dir / SEMANTIC_JOB_NAME).write_text(json.dumps(payload), encoding="utf-8")
    return job_dir


@pytest.mark.parametrize("status", [STATUS_QUEUED, STATUS_RUNNING])
def test_reconcile_fails_transient_orphans(tmp_path, status):
    cfg = config(tmp_path)
    _write_raw_job(cfg, "alpha", TASK, status)
    mgr = SemanticJobManager(cfg, evaluator=FakeEvaluator(), clock=Clock(), executor=sync_executor)

    reconciled = mgr.reconcile_orphans()

    assert [j.status for j in reconciled] == [STATUS_FAILED]
    on_disk = mgr.get_job(repo(), TASK)
    assert on_disk.status == STATUS_FAILED
    assert "hub restarted" in (on_disk.error or "")
    assert on_disk.ended_ts is not None


def test_reconcile_leaves_terminal_jobs_untouched(tmp_path):
    cfg = config(tmp_path)
    _write_raw_job(cfg, "alpha", TASK, STATUS_COMPLETED, evaluation_id="EVAL-K")
    mgr = SemanticJobManager(cfg, evaluator=FakeEvaluator(), clock=Clock(), executor=sync_executor)

    assert mgr.reconcile_orphans() == []
    assert mgr.get_job(repo(), TASK).evaluation_id == "EVAL-K"


def test_reconciled_orphan_frees_the_slot_for_a_fresh_attempt(tmp_path):
    cfg = config(tmp_path)
    _write_raw_job(cfg, "alpha", TASK, STATUS_RUNNING)
    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-NEW"))
    mgr = SemanticJobManager(cfg, evaluator=ev, clock=Clock(), executor=sync_executor)
    mgr.reconcile_orphans()

    # The failed orphan is terminal, so a new start is a fresh attempt (not dedup).
    mgr.start_job(repo(), TASK)
    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED
    assert ev.calls == [(repo(), TASK)]


def test_reconcile_no_tree_is_noop(tmp_path):
    mgr = manager(tmp_path)
    assert mgr.reconcile_orphans() == []


def test_reconcile_leaves_a_held_job_in_the_queue_it_exists_to_survive(tmp_path):
    """The deferred queue's whole value is surviving a restart.

    A held job has no abandoned turn to clean up, so failing it at startup would
    delete exactly the queue the deferral built — every waiting evaluation lost to
    the first hub restart, which for a corpus that must not be selection-biased is
    the silent gap automatic triggering exists to close.
    """
    mgr = manager(tmp_path)
    mgr.enqueue_if_absent(repo(), TASK)
    mgr.hold_deferred(mgr.get_job(repo(), TASK), "capacity")

    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-AFTER-RESTART"))
    reborn = SemanticJobManager(
        mgr._config, evaluator=ev, clock=Clock(), executor=sync_executor
    )
    assert reborn.reconcile_orphans() == []

    held = reborn.get_job(repo(), TASK)
    assert held.status == STATUS_DEFERRED
    assert held.defer_reason == "capacity"
    # And it is still dispatchable: the restart cost the queue nothing.
    assert dispatch_now(reborn) is not None
    assert reborn.get_job(repo(), TASK).evaluation_id == "EVAL-AFTER-RESTART"


# -- in-flight stuck reconciliation -------------------------------------------


def _stuck_manager(
    tmp_path: Path,
    *,
    clock: Clock,
    turn_timeout: int = DEFAULT_EVAL_TURN_TIMEOUT,
    evaluator=None,
    notifier=None,
) -> SemanticJobManager:
    return SemanticJobManager(
        config(tmp_path, turn_timeout=turn_timeout),
        evaluator=evaluator if evaluator is not None else FakeEvaluator(),
        clock=clock,
        executor=sync_executor,
        notifier=notifier,
        notify_dispatch=sync_dispatch,
    )


def test_a_running_record_carries_the_deadline_its_worker_is_held_to(tmp_path):
    """The attempt's bound is persisted, not recomputed per sweep.

    Persisting it is what lets the worker and the sweep read the *same* instant:
    the worker is refused a model turn late enough to outlive it, so the sweep can
    treat "past the deadline" as proof the model turn is already dead.
    """
    clock = Clock()
    ev = FakeEvaluator()
    mgr = SemanticJobManager(
        config(tmp_path), evaluator=ev, clock=clock, executor=sync_executor
    )

    mgr.start_job(repo(), TASK)

    started = dt.datetime.fromisoformat(read_job(mgr._config, "alpha", TASK)["started_ts"])
    deadline = dt.datetime.fromisoformat(read_job(mgr._config, "alpha", TASK)["deadline_ts"])
    assert deadline == started + dt.timedelta(seconds=STUCK_LIMIT)
    # And the same flip handed the evaluator the window it must start its model
    # turn inside — one mark on one timeline, both derived from ``started_ts``.
    assert ev.deadlines == [started + dt.timedelta(seconds=EVIDENCE_GRACE_SECONDS)]


def test_the_model_turn_cannot_outlive_the_deadline_it_is_reaped_against(tmp_path):
    """The property the finding asked for, stated as arithmetic over real values.

    The worker may open a model turn no later than ``turn_start_by``, and that turn
    carries the same configured deadline the whole-attempt bound was built from. So
    the latest instant a live model subprocess can still exist is a whole
    ``SETTLE_GRACE_SECONDS`` before the record becomes reapable — which is why the
    sweep can never settle a turn that is still inside its own deadline, and why
    the worker that follows it still has room to persist its report.
    """
    clock = Clock()
    ev = FakeEvaluator()
    cfg = config(tmp_path)
    mgr = SemanticJobManager(cfg, evaluator=ev, clock=clock, executor=sync_executor)

    mgr.start_job(repo(), TASK)

    deadline = dt.datetime.fromisoformat(read_job(cfg, "alpha", TASK)["deadline_ts"])
    latest_turn_end = ev.deadlines[0] + dt.timedelta(seconds=cfg.eval_turn_timeout)
    assert latest_turn_end == deadline - dt.timedelta(seconds=SETTLE_GRACE_SECONDS)
    assert latest_turn_end < deadline


def test_stuck_sweep_reads_the_persisted_deadline_not_the_start_stamp(tmp_path):
    """The finding: dating the bound from ``started_ts`` reaps live turns.

    ``started_ts`` marks when the *attempt* began, and the pre-model work between
    it and the model turn is not fixed — a slow evidence build pushes the turn (and
    its unexpired deadline) past any offset from the start stamp. So the sweep asks
    the record what bound it is actually under.
    """
    cfg = config(tmp_path)
    clock = Clock()
    _write_raw_job(
        cfg, "alpha", TASK, STATUS_RUNNING,
        started_ts=clock.now.isoformat(),
        deadline_ts=(clock.now + dt.timedelta(seconds=STUCK_LIMIT * 3)).isoformat(),
    )
    mgr = _stuck_manager(tmp_path, clock=clock)

    # Well past what an offset from ``started_ts`` would have allowed, and still
    # inside the deadline this attempt was actually handed.
    clock.advance(STUCK_LIMIT + 1)
    assert mgr.reconcile_stuck() == []
    assert mgr.get_job(repo(), TASK).status == STATUS_RUNNING

    clock.advance(STUCK_LIMIT * 2)
    assert [j.status for j in mgr.reconcile_stuck()] == [STATUS_FAILED]


def test_stuck_sweep_settles_a_passed_deadline_on_a_freshly_stamped_record(tmp_path):
    # The converse reading: a recent ``started_ts`` does not shelter a record whose
    # own deadline has passed (a shortened bound, or a clock the record predates).
    cfg = config(tmp_path)
    clock = Clock()
    _write_raw_job(
        cfg, "alpha", TASK, STATUS_RUNNING,
        started_ts=clock.now.isoformat(),
        deadline_ts=(clock.now - dt.timedelta(seconds=1)).isoformat(),
    )
    mgr = _stuck_manager(tmp_path, clock=clock)

    assert [j.status for j in mgr.reconcile_stuck()] == [STATUS_FAILED]


def test_a_running_record_carries_no_deadline_without_a_turn_deadline(tmp_path):
    # ``ORCH_HUB_EVAL_TURN_TIMEOUT=0`` is the fully unbounded mode: nothing bounds
    # the model turn, so there is no window to hand the evaluator and no instant at
    # which the attempt could be called overdue.
    clock = Clock()
    ev = FakeEvaluator()
    cfg = config(tmp_path, turn_timeout=0)
    mgr = SemanticJobManager(cfg, evaluator=ev, clock=clock, executor=sync_executor)

    mgr.start_job(repo(), TASK)

    assert read_job(cfg, "alpha", TASK)["deadline_ts"] is None
    assert ev.deadlines == [None]


def test_a_worker_does_not_start_a_turn_against_a_reaped_record(tmp_path):
    """A record settled while its turn waited in the executor gets no model call.

    The other half of containing a reap: the late worker must not spend a window on
    a task whose record already reports failure. The ``queued`` guard in the
    running flip is what stops it, before any evaluation work.
    """
    clock = Clock()
    executor = DeferredExecutor()
    ev = FakeEvaluator()
    mgr = SemanticJobManager(
        config(tmp_path), evaluator=ev, clock=clock, executor=executor
    )
    mgr.start_job(repo(), TASK)  # queued; its turn is captured, not run
    clock.advance(STUCK_LIMIT + 1)
    assert [j.status for j in mgr.reconcile_stuck()] == [STATUS_FAILED]

    executor.run_all()

    assert ev.calls == []
    assert mgr.get_job(repo(), TASK).status == STATUS_FAILED


@pytest.mark.parametrize("status", [STATUS_QUEUED, STATUS_RUNNING])
def test_stuck_sweep_fails_an_in_flight_job_past_the_bound(tmp_path, status):
    """The finding: a hung turn holds its slot until somebody restarts the hub.

    The startup sweep cannot help — it only runs when no worker can exist — so the
    resident sweep has to settle the record while the service is up, on a clock.
    """
    cfg = config(tmp_path)
    clock = Clock()
    _write_raw_job(
        cfg, "alpha", TASK, status,
        started_ts=clock.now.isoformat() if status == STATUS_RUNNING else None,
        updated_ts=clock.now.isoformat(),
    )
    mgr = _stuck_manager(tmp_path, clock=clock)
    clock.advance(STUCK_LIMIT + 1)

    settled = mgr.reconcile_stuck()

    assert [j.status for j in settled] == [STATUS_FAILED]
    on_disk = mgr.get_job(repo(), TASK)
    assert on_disk.status == STATUS_FAILED
    assert "stuck" in (on_disk.error or "") and "deadline" in (on_disk.error or "")
    assert on_disk.ended_ts is not None


def test_stuck_sweep_leaves_a_legitimately_long_turn_alone(tmp_path):
    """A slow turn is not a stuck one: right up to the bound, nothing is settled.

    This is the failure the bound is derived rather than guessed for — killing a
    real evaluation spends the subscription window and loses the report.
    """
    cfg = config(tmp_path)
    clock = Clock()
    _write_raw_job(cfg, "alpha", TASK, STATUS_RUNNING, started_ts=clock.now.isoformat())
    mgr = _stuck_manager(tmp_path, clock=clock)

    clock.advance(STUCK_LIMIT - 1)
    assert mgr.reconcile_stuck() == []
    assert mgr.get_job(repo(), TASK).status == STATUS_RUNNING


def test_stuck_sweep_measures_a_queued_job_from_its_admitting_write(tmp_path):
    # A queued record has no started_ts — its worker never ran — so the sweep
    # dates it from the write that admitted it, not from nothing.
    cfg = config(tmp_path)
    clock = Clock()
    _write_raw_job(
        cfg, "alpha", TASK, STATUS_QUEUED,
        created_ts=clock.now.isoformat(), updated_ts=clock.now.isoformat(),
    )
    mgr = _stuck_manager(tmp_path, clock=clock)

    clock.advance(STUCK_LIMIT - 1)
    assert mgr.reconcile_stuck() == []
    clock.advance(2)
    assert [j.status for j in mgr.reconcile_stuck()] == [STATUS_FAILED]


def test_stuck_sweep_frees_the_slot_without_a_restart(tmp_path):
    # The acceptance this exists for: after the sweep the task is retryable in the
    # same process, with no hub restart between.
    cfg = config(tmp_path)
    clock = Clock()
    _write_raw_job(cfg, "alpha", TASK, STATUS_RUNNING, started_ts=clock.now.isoformat())
    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-AFTER-STUCK"))
    mgr = _stuck_manager(tmp_path, clock=clock, evaluator=ev)
    clock.advance(STUCK_LIMIT + 1)
    mgr.reconcile_stuck()

    mgr.start_job(repo(), TASK)

    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED
    assert mgr.get_job(repo(), TASK).evaluation_id == "EVAL-AFTER-STUCK"


def test_stuck_sweep_leaves_deferred_and_terminal_records_alone(tmp_path):
    # Only in-flight records are candidates: a held job has no turn to abandon
    # (failing it would delete the queue), and a settled one is already an outcome.
    cfg = config(tmp_path)
    clock = Clock()
    _write_raw_job(cfg, "alpha", "held", STATUS_DEFERRED, created_ts=clock.now.isoformat())
    _write_raw_job(cfg, "alpha", "done", STATUS_COMPLETED, evaluation_id="EVAL-K",
                   updated_ts=clock.now.isoformat())
    mgr = _stuck_manager(tmp_path, clock=clock)
    clock.advance(STUCK_LIMIT * 10)

    assert mgr.reconcile_stuck() == []
    assert mgr.get_job(repo(), "held").status == STATUS_DEFERRED
    assert mgr.get_job(repo(), "done").evaluation_id == "EVAL-K"


def test_stuck_sweep_is_disabled_with_no_turn_deadline(tmp_path):
    """``ORCH_HUB_EVAL_TURN_TIMEOUT=0`` removes the deadline, and with it the
    only non-arbitrary basis for calling an in-flight turn stuck."""
    cfg = config(tmp_path, turn_timeout=0)
    clock = Clock()
    _write_raw_job(cfg, "alpha", TASK, STATUS_RUNNING, started_ts=clock.now.isoformat())
    mgr = _stuck_manager(tmp_path, clock=clock, turn_timeout=0)
    clock.advance(STUCK_LIMIT * 100)

    assert mgr.reconcile_stuck() == []
    assert mgr.get_job(repo(), TASK).status == STATUS_RUNNING


def test_stuck_sweep_leaves_a_record_with_no_readable_timestamp(tmp_path):
    # Unprovable age is not evidence of being stuck. The startup sweep, which
    # needs no clock, is what catches such a record.
    cfg = config(tmp_path)
    clock = Clock()
    _write_raw_job(cfg, "alpha", TASK, STATUS_RUNNING, started_ts="not-a-timestamp")
    mgr = _stuck_manager(tmp_path, clock=clock)
    clock.advance(STUCK_LIMIT * 10)

    assert mgr.reconcile_stuck() == []
    assert mgr.get_job(repo(), TASK).status == STATUS_RUNNING


def test_stuck_sweep_notifies_unlike_the_startup_sweep(tmp_path):
    # A reap is a fresh outcome arriving while the operator is running — the same
    # event a backend failure would have pushed had the turn managed to fail.
    cfg = config(tmp_path)
    clock = Clock()
    _write_raw_job(cfg, "alpha", TASK, STATUS_RUNNING, started_ts=clock.now.isoformat())
    notifier = RecordingNotifier()
    mgr = _stuck_manager(tmp_path, clock=clock, notifier=notifier)
    clock.advance(STUCK_LIMIT + 1)

    mgr.reconcile_stuck()

    assert len(notifier.sent) == 1


def test_a_late_worker_cannot_clobber_a_record_reaped_mid_turn(tmp_path):
    """The one real cost of reaping on a clock: the worker may still be alive.

    Only a worker that has outlived its own bound reaches this — the deadline is
    the record's, and the worker is held to it — but such a worker does eventually
    return, and it must not overwrite the outcome the sweep already reported. The
    reap here happens from *inside* the turn, so the record is genuinely
    ``running`` when it is settled, and the finalize that follows is the real late
    one: it finds a non-transient record and writes nothing.
    """
    clock = Clock()
    box: dict = {}

    def reap_from_inside_the_turn(r, task_id):
        clock.advance(STUCK_LIMIT + 1)
        assert [j.status for j in box["mgr"].reconcile_stuck()] == [STATUS_FAILED]

    mgr = SemanticJobManager(
        config(tmp_path),
        evaluator=FakeEvaluator(
            report=mk_report(evaluation_id="EVAL-LATE"),
            on_evaluate=reap_from_inside_the_turn,
        ),
        clock=clock,
        executor=sync_executor,
    )
    box["mgr"] = mgr

    mgr.start_job(repo(), TASK)

    settled = mgr.get_job(repo(), TASK)
    assert settled.status == STATUS_FAILED
    assert settled.evaluation_id is None


# -- corrupt record -----------------------------------------------------------


def test_corrupt_record_surfaces_as_error_without_holding_slot(tmp_path):
    cfg = config(tmp_path)
    job_dir = cfg.data_dir / "semantic-jobs" / "alpha" / TASK
    job_dir.mkdir(parents=True)
    (job_dir / SEMANTIC_JOB_NAME).write_text("{not json", encoding="utf-8")
    mgr = SemanticJobManager(cfg, evaluator=FakeEvaluator(report=mk_report(evaluation_id="EVAL-FRESH")), clock=Clock(), executor=sync_executor)

    broken = mgr.get_job(repo(), TASK)
    assert broken.status == STATUS_ERROR and broken.repo_path is None

    # error is terminal → a start overwrites it with a fresh attempt (not blocked).
    mgr.start_job(repo(), TASK)
    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED
    # A corrupt orphan is skipped by reconciliation (already terminal).
    assert mgr.reconcile_orphans() == []


# -- terminal-outcome notifications -------------------------------------------


def test_completed_job_fires_one_push_linking_the_evaluation(tmp_path):
    notifier = RecordingNotifier()
    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-42"))
    mgr = manager(tmp_path, evaluator=ev, notifier=notifier)

    mgr.start_job(repo(), TASK)

    assert len(notifier.sent) == 1
    note = notifier.sent[0]
    assert note.title == "alpha: evaluation completed"
    assert note.group == "alpha"
    assert note.body == f"{TASK}\nreport EVAL-42"


def test_an_automatic_completion_is_silent_but_an_automatic_failure_still_pushes(tmp_path):
    """Routine mechanical success is noise; a shrinking corpus is not.

    Every completed task now produces a semantic job, so pushing each success
    would put a second, unactionable notification right behind the run-end push
    that is actionable. A failure is the opposite: nothing else would ever tell
    the operator a report silently did not happen.
    """
    notifier = RecordingNotifier()
    mgr = manager(tmp_path, evaluator=FakeEvaluator(), notifier=notifier)
    mgr.enqueue_if_absent(repo(), TASK)
    dispatch_now(mgr)
    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED
    assert notifier.sent == []

    failing = manager(
        tmp_path,
        evaluator=FakeEvaluator(error=SemanticEvaluationError("backend down")),
        notifier=notifier,
    )
    other = "2026-08-02-second-task"
    failing.enqueue_if_absent(repo(), other)
    dispatch_now(failing, task_id=other)

    assert failing.get_job(repo(), other).status == STATUS_FAILED
    assert [n.title for n in notifier.sent] == ["alpha: evaluation failed"]


def test_failed_job_fires_one_push_without_the_diagnostic(tmp_path):
    notifier = RecordingNotifier()
    ev = FakeEvaluator(error=SemanticEvaluationError("SECRET-MODEL-STDERR"))
    mgr = manager(tmp_path, evaluator=ev, notifier=notifier)

    mgr.start_job(repo(), TASK)

    assert mgr.get_job(repo(), TASK).status == STATUS_FAILED
    assert len(notifier.sent) == 1
    note = notifier.sent[0]
    assert note.title == "alpha: evaluation failed"
    # The bounded diagnostic stays in job.json and the dashboard, not in the push.
    assert "SECRET-MODEL-STDERR" not in f"{note.title}\n{note.body}"


def test_no_notifier_settles_the_job_and_spawns_no_delivery_work(tmp_path):
    dispatch = RecordingDispatch()
    mgr = manager(tmp_path, notifier=None, notify_dispatch=dispatch)

    mgr.start_job(repo(), TASK)

    assert mgr.get_job(repo(), TASK).status == STATUS_COMPLETED
    assert dispatch.calls == 0  # disabled push does no dispatch work at all


def test_delivery_failure_leaves_the_settled_record_intact(tmp_path):
    notifier = RecordingNotifier(fail=RuntimeError("bark unreachable"))
    ev = FakeEvaluator(report=mk_report(evaluation_id="EVAL-OK"))
    mgr = manager(tmp_path, evaluator=ev, notifier=notifier)

    mgr.start_job(repo(), TASK)  # must not raise: delivery is best-effort

    assert len(notifier.sent) == 1  # attempted, and the failure was swallowed
    persisted = read_job(mgr._config, "alpha", TASK)
    assert persisted["status"] == STATUS_COMPLETED
    assert persisted["evaluation_id"] == "EVAL-OK"
    assert persisted["error"] is None


def test_transient_transitions_do_not_notify(tmp_path):
    notifier = RecordingNotifier()
    mgr_ref: dict = {}
    seen: dict = {}

    def spy(r, task_id):
        # Mid-turn the job is persisted as running; nothing has been pushed yet.
        seen["status"] = mgr_ref["mgr"].get_job(r, task_id).status
        seen["sent"] = list(notifier.sent)

    executor = DeferredExecutor()
    mgr = manager(
        tmp_path, evaluator=FakeEvaluator(on_evaluate=spy), executor=executor, notifier=notifier
    )
    mgr_ref["mgr"] = mgr

    mgr.start_job(repo(), TASK)
    assert mgr.get_job(repo(), TASK).status == STATUS_QUEUED
    assert notifier.sent == []  # queued alone never notifies

    executor.run_all()
    assert seen["status"] == STATUS_RUNNING and seen["sent"] == []
    assert len(notifier.sent) == 1  # only the terminal settle pushed


def test_duplicate_start_and_re_finalize_do_not_re_notify(tmp_path):
    notifier = RecordingNotifier()
    executor = DeferredExecutor()
    mgr = manager(tmp_path, executor=executor, notifier=notifier)

    first = mgr.start_job(repo(), TASK)
    mgr.start_job(repo(), TASK)  # deduped while queued: one turn, one outcome
    executor.run_all()
    assert len(notifier.sent) == 1

    # A stale finalize for the same attempt (the defensive guard's case) is a
    # no-op on an already-terminal record, so it must not push a second time.
    job_dir = mgr._config.data_dir / "semantic-jobs" / "alpha" / TASK
    mgr._finalize(job_dir, first.job_id, status=STATUS_COMPLETED, evaluation_id="EVAL-DUP")
    assert len(notifier.sent) == 1
    assert mgr.get_job(repo(), TASK).evaluation_id != "EVAL-DUP"


def test_reconciled_orphans_do_not_notify(tmp_path):
    # A startup sweep is not a fresh outcome the operator is waiting on: failing
    # abandoned jobs at boot must stay silent.
    cfg = config(tmp_path)
    _write_raw_job(cfg, "alpha", TASK, STATUS_RUNNING)
    notifier = RecordingNotifier()
    mgr = SemanticJobManager(
        cfg,
        evaluator=FakeEvaluator(),
        clock=Clock(),
        executor=sync_executor,
        notifier=notifier,
        notify_dispatch=sync_dispatch,
    )

    assert [j.status for j in mgr.reconcile_orphans()] == [STATUS_FAILED]
    assert notifier.sent == []


# -- containment / no mutation ------------------------------------------------


@pytest.mark.parametrize("bad", ["../evil", "a/b", "..", ".", "", "x\x00y"])
def test_unsafe_task_id_is_rejected(tmp_path, bad):
    mgr = manager(tmp_path)
    with pytest.raises(ValueError):
        mgr.start_job(repo(), bad)
    with pytest.raises(ValueError):
        mgr.find_job(repo(), bad)


@pytest.mark.parametrize("bad_repo", [".", "-.-", ".."])
def test_dotlike_repo_name_is_rejected_not_silently_collapsed(tmp_path, bad_repo):
    """A repo whose slug is a bare dot reference must fail closed, not write a job
    to a collapsed/escaping path the two-level reconcile scan can never reach.

    Regression for the ``ManagedRepo(name=".")`` gap: ``_slug(".")`` (and
    ``_slug("-.-")``) is ``.``, and ``base / "." / task`` *collapses* the
    ``<repo-slug>/`` level, so the record landed at ``semantic-jobs/<task>/job.json``
    — outside the two-level walk :meth:`reconcile_orphans` scans, so a hub crash
    left that transient job stranded and its ``(repo, task)`` duplicate slot held
    forever. (``".."`` instead *escapes* the tree.) The slugged repo segment is now
    validated, so every such attempt is refused before any dir is created.
    """
    mgr = manager(tmp_path)
    dot_repo = repo(name=bad_repo)
    with pytest.raises(ValueError):
        mgr.start_job(dot_repo, TASK)
    with pytest.raises(ValueError):
        mgr.find_job(dot_repo, TASK)
    # Fail-closed before any filesystem write: no job record was persisted, so the
    # stuck-forever orphan can never exist.
    assert snapshot_tree(mgr.jobs_dir) == {}


def test_manager_only_writes_semantic_jobs_subtree(tmp_path):
    cfg = config(tmp_path)
    mgr = SemanticJobManager(cfg, evaluator=FakeEvaluator(), clock=Clock(), executor=sync_executor)

    mgr.start_job(repo(), TASK)

    files = list(snapshot_tree(cfg.data_dir).keys())
    assert files  # something was written
    assert all(f.startswith("semantic-jobs/") for f in files)


# -- integration: real evaluator through the manager --------------------------


VALID_ENVELOPE_PAYLOAD = {
    "summary": "clean handoff",
    "confidence": 0.8,
    "scores": [{"dimension": "handoff_completeness", "score": 0.9, "confidence": 0.8, "rationale": "log complete"}],
    "findings": [],
    "improvements": [],
}


class FakeBackend:
    """Minimal read-only semantic backend returning a canned report envelope."""

    @property
    def info(self) -> SemanticEvaluatorInfo:
        return mk_info(backend="claude-cli", model="claude-opus-4-8")

    def evaluate(self, prompt: str, r: ManagedRepo, *, cancel=None) -> str:
        from orch_hub.semantic import SEMANTIC_END, SEMANTIC_START

        body = json.dumps(VALID_ENVELOPE_PAYLOAD)
        return f"{SEMANTIC_START}\n{body}\n{SEMANTIC_END}"

    def doctor(self):  # pragma: no cover - not exercised here
        raise NotImplementedError


def _fake_git(args, **kwargs):
    return CommandResult(tuple(str(a) for a in args), 128, "", "")  # git unavailable


def _write_repo(cfg: Config, tmp_path: Path) -> ManagedRepo:
    repo_path = tmp_path / "repos" / "alpha"
    (repo_path / ".ai-tasks").mkdir(parents=True)
    (repo_path / ".ai-tasks" / f"{TASK}.md").write_text(
        f"---\nid: {TASK}\nstatus: in_progress\nsession-est: 1/2\nblockers: []\n---\n\n"
        "# Async status\n\n## Goal\ngo\n\n## Scope\n- x\n\n## Acceptance\n- y\n\n## Session log\n\n"
        "### 2026-07-23 / sid-a / (pending → in_progress)\n- Done: work\n- Next: more\n- Open: none\n",
        encoding="utf-8",
    )
    (repo_path / ".ai-tasks" / "index.md").write_text("# Active tasks\n", encoding="utf-8")
    return ManagedRepo(name="alpha", path=repo_path, manifest=repo_path / ".ai-deploy-manifest.json")


def test_manager_drives_real_evaluator_links_evaluation_and_leaves_repo_intact(tmp_path):
    cfg = config(tmp_path)
    target_repo = _write_repo(cfg, tmp_path)
    service = EvaluationService(cfg, git_runner=_fake_git, clock=Clock())
    evaluator = SemanticEvaluator(cfg, evaluation_service=service, backend=FakeBackend(), clock=Clock())
    mgr = SemanticJobManager(cfg, evaluator=evaluator, clock=Clock(), executor=sync_executor)

    before = snapshot_tree(target_repo.path)
    mgr.start_job(target_repo, TASK)
    job = mgr.get_job(target_repo, TASK)

    assert job.status == STATUS_COMPLETED
    # The completed job links a real evaluation dir holding the persisted report.
    eval_dir = cfg.data_dir / "evaluations" / "alpha" / TASK / job.evaluation_id
    assert (eval_dir / "semantic_report.json").is_file()
    assert job.backend == "claude-cli"
    # The target repo is byte-identical: the async wrapper adds no repo mutation.
    assert snapshot_tree(target_repo.path) == before


class RecordingCatalog:
    """Records each publication the evaluator offers the cross-repo report feed.

    The real feed's mechanics are ``tests/test_report_catalog.py``; what these
    tests need is only whether the hook fired, because "the record says cancelled
    and the report is in the global feed" is the divergence under test.
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def record_evaluation(self, r: ManagedRepo, task_id: str, evaluation_id: str):
        self.calls.append((task_id, evaluation_id))
        return None


def _real_stack(tmp_path, cfg: Config, *, backend, catalog=None, clock=None):
    """Real evaluation service + evaluator + job manager over a fake backend.

    ``clock`` is the *manager's*, so a test can advance past an attempt deadline
    and drive the reap; the evidence and report stamps keep their own fixed ones.
    """
    target_repo = _write_repo(cfg, tmp_path)
    service = EvaluationService(cfg, git_runner=_fake_git, clock=Clock())
    evaluator = SemanticEvaluator(
        cfg, evaluation_service=service, backend=backend, catalog=catalog,
        clock=Clock(),
    )
    mgr = SemanticJobManager(
        cfg, evaluator=evaluator, clock=clock or Clock(), executor=sync_executor
    )
    return target_repo, mgr


def test_a_cancel_during_settlement_is_refused_not_written(tmp_path, monkeypatch):
    """The post-model race, end to end: settlement wins and the cancel says so.

    Real manager, real evaluator, real artifact writes and publication hook, with
    the cancel landing where it hurts — after the backend returned, while the
    report is being parsed on its way to disk and to the feed. Accepting it there
    leaves a terminal ``cancelled`` record standing beside a persisted
    ``semantic_report.json`` and a published entry: the operator reads the attempt
    as stopped while its output goes on existing everywhere else.

    So the attempt seals as its turn returns and the cancel is refused instead —
    409, naming the settling, which is the one answer that is true. The turn is
    unstoppable for the seconds it takes to finish writing, and that is bounded by
    the settle grace the reap already allows for.
    """
    cfg = config(tmp_path)
    catalog = RecordingCatalog()
    target_repo, mgr = _real_stack(tmp_path, cfg, backend=FakeBackend(), catalog=catalog)
    real_parse = semantic_module.parse_report
    refusals: list[Exception] = []

    def cancel_then_parse(*args, **kwargs):
        # Inside evaluate(), off the manager lock: the turn has returned, the seal
        # is taken, and nothing is persisted yet. That is the whole window.
        try:
            mgr.cancel_job(target_repo, TASK)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            refusals.append(exc)
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(semantic_module, "parse_report", cancel_then_parse)

    mgr.start_job(target_repo, TASK)

    assert len(refusals) == 1 and isinstance(refusals[0], SemanticJobStateError)
    assert "settling" in str(refusals[0])
    job = mgr.get_job(target_repo, TASK)
    assert job.status == STATUS_COMPLETED  # never rewritten to cancelled
    eval_dir = cfg.data_dir / "evaluations" / "alpha" / TASK / job.evaluation_id
    assert (eval_dir / "semantic_report.json").is_file()
    assert catalog.calls == [(TASK, job.evaluation_id)]


class CancellingBackend(FakeBackend):
    """Runs ``on_return`` as its turn returns — the same window, from the inside."""

    def __init__(self, on_return):
        self._on_return = on_return

    def evaluate(self, prompt: str, r: ManagedRepo, *, cancel=None) -> str:
        text = super().evaluate(prompt, r, cancel=cancel)
        self._on_return()
        return text


def test_a_cancel_racing_the_turns_return_leaves_no_report_anywhere(tmp_path):
    """And when the cancel gets there first, the paid-for report is thrown away.

    The mirror of the test above, at the same instant: the turn produced a report
    but the operator's cancel reached the attempt before it sealed. The record is
    ``cancelled`` — and that reading has to be true everywhere, so the evaluation
    dir is removed by the all-or-nothing cleanup and the publication hook is never
    reached. A cancelled attempt leaves nothing behind it to contradict itself.
    """
    cfg = config(tmp_path)
    catalog = RecordingCatalog()
    # Resolved when the turn runs, so the hook can name the manager built from it.
    backend = CancellingBackend(lambda: mgr.cancel_job(target_repo, TASK))
    target_repo, mgr = _real_stack(tmp_path, cfg, backend=backend, catalog=catalog)

    mgr.start_job(target_repo, TASK)

    job = mgr.get_job(target_repo, TASK)
    assert job.status == STATUS_CANCELLED
    assert job.evaluation_id is None
    assert catalog.calls == []
    # Nothing half-written for a later reconciliation to publish either.
    assert list((cfg.data_dir / "evaluations" / "alpha" / TASK).glob("*")) == []


def test_a_settle_that_cannot_be_written_keeps_the_attempt_non_cancellable(
    tmp_path, monkeypatch
):
    """The seal has to outlive a terminal write that fails, or the window reopens.

    The turn returned, sealed, and its report is on disk and in the global feed —
    and then ``job.json`` could not be replaced. Withdrawing the attempt's handle
    there (which an unconditional ``finally`` did) left the durable record reading
    ``running`` with nothing registered behind it, so the next cancel met no
    handle, was accepted, and wrote terminal ``cancelled`` beside the persisted,
    published report: exactly the divergence the seal exists to prevent, reached
    by the one path that stepped around it.

    So the handle is withdrawn only once the terminal record is durable. A
    settlement the hub could not record keeps the attempt non-cancellable, and the
    slot is freed by the sweep that already bounds every other wedged settlement —
    as ``failed``, which is true, rather than as a cancel that stopped nothing.
    """
    cfg = config(tmp_path, turn_timeout=30)
    clock = Clock()
    catalog = RecordingCatalog()
    target_repo, mgr = _real_stack(
        tmp_path, cfg, backend=FakeBackend(), catalog=catalog, clock=clock
    )
    real_write = semantic_jobs._write_record
    failed_writes: list[str] = []

    def fail_the_terminal_write(job_dir: Path, record) -> None:
        # One shot, and only the settle: the queued/running writes that put the
        # attempt in this state have to land, and so does the sweep's below.
        if record.status == STATUS_COMPLETED and not failed_writes:
            failed_writes.append(record.job_id)
            raise OSError("no space left on device")
        real_write(job_dir, record)

    monkeypatch.setattr(semantic_jobs, "_write_record", fail_the_terminal_write)

    mgr.start_job(target_repo, TASK)  # inline turn; its settle cannot be written

    assert len(failed_writes) == 1  # the terminal write is the one that failed
    job = mgr.get_job(target_repo, TASK)
    assert job.status == STATUS_RUNNING  # the record never left the turn
    assert len(catalog.calls) == 1  # ...while the report was published anyway
    evaluation_id = catalog.calls[0][1]
    report = cfg.data_dir / "evaluations" / "alpha" / TASK / evaluation_id
    assert (report / "semantic_report.json").is_file()

    with pytest.raises(SemanticJobStateError) as err:
        mgr.cancel_job(target_repo, TASK)

    assert "settling" in str(err.value)  # refused by the seal, not by the status
    assert mgr.get_job(target_repo, TASK).status == STATUS_RUNNING  # not cancelled
    assert (report / "semantic_report.json").is_file()
    assert len(catalog.calls) == 1

    # Still bounded: the reap settles the wedged attempt and frees its slot.
    clock.advance(EVIDENCE_GRACE_SECONDS + 30 + SETTLE_GRACE_SECONDS + 1)
    assert [settled.status for settled in mgr.reconcile_stuck()] == [STATUS_FAILED]
    assert mgr.get_job(target_repo, TASK).status == STATUS_FAILED


def test_a_hung_turn_settles_failed_and_frees_the_slot_without_a_restart(tmp_path):
    """The headline: end-to-end, a ``claude`` process that never returns.

    Real manager, real evaluator, real Claude backend — only the subprocess is
    faked, and it hangs rather than fails. Before the deadline this blocked its
    worker forever and left the record ``running``, so the task's slot was held
    until somebody restarted the hub. Now the turn is killed at the bound and the
    record settles into the same terminal shape any backend failure produces, with
    a diagnostic that names the deadline and a slot open for the next attempt.
    """
    cfg = config(tmp_path, turn_timeout=30)
    target_repo = _write_repo(cfg, tmp_path)
    service = EvaluationService(cfg, git_runner=_fake_git, clock=Clock())
    backend = ClaudeSemanticBackend(
        model="claude-x",
        timeout=cfg.eval_turn_timeout,
        runner=_hanging_runner,
    )
    evaluator = SemanticEvaluator(
        cfg, evaluation_service=service, backend=backend, clock=Clock()
    )
    mgr = SemanticJobManager(cfg, evaluator=evaluator, clock=Clock(), executor=sync_executor)

    mgr.start_job(target_repo, TASK)

    job = mgr.get_job(target_repo, TASK)
    assert job.status == STATUS_FAILED
    assert "30s deadline" in (job.error or "")
    assert job.evaluation_id is None
    # Terminal, so the slot is free: the next start is a fresh attempt, not the
    # stuck job handed back.
    assert mgr.start_job(target_repo, TASK).job_id is not None
    assert mgr.get_job(target_repo, TASK).status == STATUS_FAILED


def _hanging_runner(argv, cwd, timeout, cancel=None):
    """A ``claude`` turn that neither finishes nor fails, killed at the deadline.

    Exactly what :func:`orch_hub.subprocess_utils.run_command` raises once its
    deadline expires, so no real process has to hang for the test to be honest
    about what the backend sees."""
    raise subprocess.TimeoutExpired(list(argv), timeout)


def test_the_status_enumeration_covers_every_declared_status():
    """A new status must join ``STATUSES`` or it drops out of every census.

    The operator's queue surface counts by status over this tuple precisely so
    that the uninteresting zeroes ("nothing failed", "nothing is held") are
    stated rather than inferred from an absence. A status the tuple has not
    learned about would be counted only where it happens to occur, which is the
    silent shrinking denominator automatic evaluation exists to prevent.
    """
    declared = {
        value
        for name, value in vars(semantic_jobs).items()
        if name.startswith("STATUS_") and isinstance(value, str)
    }

    assert set(STATUSES) == declared
    assert len(STATUSES) == len(declared)  # no duplicates
