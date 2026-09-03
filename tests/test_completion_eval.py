"""Tests for completion-triggered task evaluation (:mod:`orch_hub.completion_eval`).

Covers the trigger's own contract over a fake Layer 1 — both gates (subsystem and
``ORCH_HUB_EVAL_AUTO``) checked before any work is dispatched, work handed to the
executor rather than run on the caller's thread, an unattributable run record
refused, and a failure isolated from run state — plus end-to-end tests that drive
a *real* run manager, evaluation service, static evaluator and job manager.

The load-bearing half of this file is negative. Layer 2 is no longer part of the
automatic path, so these tests assert what a completion must **not** do: no job
record is written, no model turn opens, and none of the things that used to be
able to start one — a restart, a resident sweep, a legacy ``deferred`` record
left over from the automatic era — can start one now. The fake backend below
exists so that a test which *does* reach the evaluator would produce a report
rather than a network call; that no test reaches it without asking is the point.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
from pathlib import Path
from typing import NamedTuple

import pytest

from orch_hub.completion_eval import CompletionEvaluationTrigger
from orch_hub.config import Config
from orch_hub.evaluation import EVIDENCE_NAME, EvaluationService
from orch_hub.models import (
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
    RunRecord,
    SemanticEvaluatorInfo,
    SemanticJob,
)
from orch_hub.runs import RunManager
from orch_hub.semantic import (
    SEMANTIC_REPORT_MD_NAME,
    SEMANTIC_REPORT_NAME,
    SemanticEvaluator,
)
from orch_hub.semantic_jobs import (
    STATUS_COMPLETED,
    STATUS_DEFERRED,
    SemanticJobManager,
)
from orch_hub.static_metrics import STATIC_METRICS_NAME, StaticEvaluator
from orch_hub.subprocess_utils import CommandResult

TASK = "2026-08-09-completion-triggered-evaluation"
REPO_PATH = Path("/repos/alpha")
FIXED_TS = dt.datetime(2026, 8, 9, 3, 0, 0, tzinfo=dt.timezone.utc)


# -- helpers ------------------------------------------------------------------


def repo(name: str = "alpha", path: Path = REPO_PATH) -> ManagedRepo:
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def config(
    tmp_path: Path, *, enabled: bool = True, auto: bool = True
) -> Config:
    return Config(
        mandrel_path=tmp_path / "mandrel",
        data_dir=tmp_path / "hub-data",
        # Point the transcript reader at an empty tree so no test ever reads the
        # operator's real ``~/.claude``.
        claude_home=tmp_path / "claude-home",
        evaluation_enabled=enabled,
        eval_auto=auto,
    )


def completed_record(
    *,
    repo_path: Path | None = REPO_PATH,
    task_id: str = TASK,
    repo_name: str = "alpha",
    auto_semantic_eval: bool = True,
) -> RunRecord:
    """A run record as the manager hands it to the hook: ended, task attributed."""
    return RunRecord(
        run_id="20260809T030000Z-alpha-completion",
        repo_name=repo_name,
        repo_path=repo_path,
        task_id=task_id,
        started_ts=FIXED_TS.isoformat(),
        ended_ts=FIXED_TS.isoformat(),
        exit_code=0,
        auto_semantic_eval=auto_semantic_eval,
    )


class StubReport:
    """The slice of ``StaticMetricsReport`` the trigger reads back."""

    def __init__(self, evaluation_id: str = "20260809T030000Z"):
        self.evaluation_id = evaluation_id


class FakeStatic:
    """Stands in for :class:`StaticEvaluator`: the ``generate_metrics`` seam."""

    def __init__(self, *, error: Exception | None = None):
        self.calls: list[tuple[ManagedRepo, str]] = []
        self._error = error

    def generate_metrics(self, r: ManagedRepo, task_id: str) -> StubReport:
        self.calls.append((r, task_id))
        if self._error is not None:
            raise self._error
        return StubReport()


class DeferredExecutor:
    """Captures submitted work without running it (the production thread's stand-in)."""

    def __init__(self):
        self.pending: list = []

    def __call__(self, run) -> None:
        self.pending.append(run)

    def run_all(self) -> None:
        while self.pending:
            self.pending.pop(0)()


def sync_executor(run) -> None:
    run()


def trigger(
    tmp_path: Path,
    *,
    static=None,
    executor=sync_executor,
    enabled: bool = True,
    auto: bool = True,
) -> tuple[CompletionEvaluationTrigger, FakeStatic]:
    static = static if static is not None else FakeStatic()
    return (
        CompletionEvaluationTrigger(
            config(tmp_path, enabled=enabled, auto=auto),
            static_evaluator=static,
            executor=executor,
        ),
        static,
    )


# -- the happy path -----------------------------------------------------------


def test_a_completed_task_gets_layer_1_and_only_layer_1(tmp_path):
    trig, static = trigger(tmp_path)

    trig.on_task_completed(completed_record())

    assert [(r.name, t) for r, t in static.calls] == [("alpha", TASK)]
    # Structural, not behavioural: the trigger holds no semantic collaborator at
    # all, so there is nothing here that a completion could ask for a model turn.
    assert not hasattr(trig, "_jobs") and not hasattr(trig, "_dispatcher")


def test_the_repo_is_reconstructed_from_the_run_record_alone(tmp_path):
    """No registry lookup: the record carries everything the evaluation needs."""
    trig, static = trigger(tmp_path)

    trig.on_task_completed(completed_record())

    reached, _ = static.calls[0]
    assert reached.path == REPO_PATH
    assert reached.name == "alpha"


# -- the two gates ------------------------------------------------------------


@pytest.mark.parametrize(
    "enabled, auto, expect_enabled",
    [
        (True, True, True),
        (True, False, False),  # trigger gate off: manual-only operation
        (False, True, False),  # subsystem off: nothing to trigger
        (False, False, False),
    ],
)
def test_gates_compose_as_subsystem_and_trigger(tmp_path, enabled, auto, expect_enabled):
    executor = DeferredExecutor()
    trig, static = trigger(tmp_path, executor=executor, enabled=enabled, auto=auto)

    assert trig.enabled is expect_enabled

    trig.on_task_completed(completed_record())

    # A closed gate does no work at all — not even a submission.
    assert bool(executor.pending) is expect_enabled
    executor.run_all()
    assert bool(static.calls) is expect_enabled


def test_automatic_triggering_is_switchable_without_disabling_the_subsystem(tmp_path):
    """``ORCH_HUB_EVAL_AUTO=false`` leaves manual evaluation completely intact."""
    trig, static = trigger(tmp_path, auto=False)
    cfg = config(tmp_path, auto=False)

    trig.on_task_completed(completed_record())

    assert static.calls == []
    assert cfg.evaluation_enabled is True  # the subsystem, and its routes, stay on


# -- dispatch -----------------------------------------------------------------


def test_nothing_runs_on_the_callers_thread(tmp_path):
    """The caller holds the run-manager lock, so all of it goes to the executor.

    Layer 1 walks the task file, every related run's records/events/sessions, a
    bounded git window and the ``.ai/`` snapshot — none of which may happen under
    the lock every run read in the process queues behind. There is nothing left to
    do inline: Layer 2's durable acceptance was the one O(1) step that had to
    settle before the hook returned, and it is gone with the automatic path.
    """
    executor = DeferredExecutor()
    trig, static = trigger(tmp_path, executor=executor)

    trig.on_task_completed(completed_record())

    assert static.calls == []
    assert len(executor.pending) == 1

    executor.run_all()
    assert len(static.calls) == 1


# -- refusals and isolation ---------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        completed_record(repo_path=None),  # stub from an unreadable run.json
        completed_record(task_id=""),  # no task to evaluate
    ],
)
def test_an_unattributable_record_evaluates_nothing(tmp_path, record):
    executor = DeferredExecutor()
    trig, static = trigger(tmp_path, executor=executor)

    trig.on_task_completed(record)  # must not raise

    assert executor.pending == []
    assert static.calls == []


def test_a_layer_1_failure_never_reaches_run_state(tmp_path):
    """Isolation is the invariant: the run is already persisted when this runs."""
    trig, static = trigger(
        tmp_path, static=FakeStatic(error=OSError("evidence unreadable"))
    )

    trig.on_task_completed(completed_record())  # must not raise

    assert len(static.calls) == 1


def test_a_repeated_completion_regenerates_layer_1(tmp_path):
    """Per completion signal, not task-once — deliberately, and cheaply.

    The signal is per *run*: a task that reaches ``completed`` without being
    archived is still launchable, and the close-out-only re-run classifies
    ``completed`` again. There is no durable once-guard here because the guard
    only ever existed to stop a *model turn* running twice; a free deterministic
    artifact is simply regenerated fresher.
    """
    trig, static = trigger(tmp_path)

    trig.on_task_completed(completed_record())
    trig.on_task_completed(completed_record())

    assert len(static.calls) == 2


# -- the retired per-run Layer 2 opt-out --------------------------------------


def test_the_launch_time_opt_out_no_longer_changes_anything(tmp_path):
    """``auto_semantic_eval=False`` is inert: there is nothing left to opt out of.

    The field selected between an accepted Layer 2 job and a recorded ``skipped``
    one. Both halves are gone, so a run launched with the box unchecked and one
    launched with it checked now take the identical path — Layer 1, nothing else.
    Kept as a regression guard while the field itself is still persisted: it must
    not grow a second meaning on the way out.
    """
    opted_out, static_out = trigger(tmp_path)
    opted_out.on_task_completed(completed_record(auto_semantic_eval=False))

    opted_in, static_in = trigger(tmp_path)
    opted_in.on_task_completed(completed_record(auto_semantic_eval=True))

    reached = [(r.name, t) for r, t in static_out.calls]
    assert reached == [("alpha", TASK)]
    assert [(r.name, t) for r, t in static_in.calls] == reached


# -- integration: a real completed run through the real layers ----------------


VALID_ENVELOPE_PAYLOAD = {
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


class FakeBackend:
    """Minimal read-only semantic backend returning a canned report envelope."""

    @property
    def info(self) -> SemanticEvaluatorInfo:
        return SemanticEvaluatorInfo(
            backend="fake-cli", model="fake-model", schema_version="1"
        )

    def evaluate(self, prompt: str, r: ManagedRepo, *, cancel=None) -> str:
        from orch_hub.semantic import SEMANTIC_END, SEMANTIC_START

        return f"{SEMANTIC_START}\n{json.dumps(VALID_ENVELOPE_PAYLOAD)}\n{SEMANTIC_END}"

    def doctor(self):  # pragma: no cover - not exercised here
        raise NotImplementedError


def _fake_git(args, **kwargs):
    return CommandResult(tuple(str(a) for a in args), 128, "", "")  # git unavailable


class FakeProc:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class FakeSpawner:
    def __init__(self):
        self.procs: list[FakeProc] = []
        self._pids = itertools.count(4000)

    def __call__(self, argv, *, cwd, stdout, env, start_new_session):
        proc = FakeProc(next(self._pids))
        self.procs.append(proc)
        return proc


COMPLETING_LOG = [
    "[03:00:10] --- dev session start: sid=dev1 backend=claude:x@max status_before=in_progress",
    "[03:10:00] run finished: status=finished context≈150000 tokens",
    "[03:10:00] --- dev session end: sid=dev1 status=final_review",
    "[03:11:00] --- review session start: sid=(pending) backend=codex:y@xhigh status_before=final_review",
    "[03:15:00] run finished: tool=codex exit=0 status=finished context≈90000 tokens",
    "[03:15:00] --- review session end: sid=rev1 status=completed+archived",
]


def _write_repo(tmp_path: Path) -> ManagedRepo:
    repo_path = tmp_path / "repos" / "alpha"
    (repo_path / ".ai-tasks").mkdir(parents=True)
    (repo_path / ".ai-tasks" / f"{TASK}.md").write_text(
        f"---\nid: {TASK}\nstatus: completed\nsession-est: 1/1\n---\n\n"
        "# Completion trigger\n\n## Goal\ngo\n\n## Scope\n- x\n\n## Acceptance\n- y\n\n"
        "## Session log\n\n### 2026-08-09 / dev1 / (pending → final_review)\n"
        "- Done: work\n- Next: none\n- Open: none\n\n"
        "### 2026-08-09 / rev1 / review of dev1 / (final_review → completed)\n"
        "- Verdict: pass\n- Group: dev1\n- Findings: none\n",
        encoding="utf-8",
    )
    (repo_path / ".ai-tasks" / "index.md").write_text(
        "# Active tasks\n\n| Task | Status | Summary |\n|---|---|---|\n"
        f"| {TASK} | completed | Trigger |\n",
        encoding="utf-8",
    )
    return ManagedRepo(
        name="alpha", path=repo_path, manifest=repo_path / ".ai-deploy-manifest.json"
    )


def _write_orch_log(
    target: ManagedRepo,
    stamp: str = "20260809T030000Z",
    lines: list[str] | None = None,
) -> Path:
    log_dir = target.path / ".mandrel" / "orchestrator" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{stamp}-{TASK}.log"
    path.write_text(
        "".join(line + "\n" for line in (lines or COMPLETING_LOG)), encoding="utf-8"
    )
    return path


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class Stack(NamedTuple):
    """The evaluation collaborators one hub process builds over one data dir."""

    service: EvaluationService
    static: StaticEvaluator
    semantic: SemanticEvaluator
    jobs: SemanticJobManager
    trigger: CompletionEvaluationTrigger


def _stack(cfg: Config, *, executor=sync_executor) -> Stack:
    """Build the real evaluation stack, with only the model call faked.

    A function of the config alone, so a test can build it twice over the same
    data dir and get what a *restarted* hub gets: fresh in-memory managers over
    the same persisted records.

    The job manager is built and the semantic evaluator behind it works — a
    manual ``start_job`` in these tests produces a real report set. It is simply
    not wired to the trigger, which is the whole change: nothing connects a run
    ending to a model turn.
    """
    service = EvaluationService(cfg, git_runner=_fake_git, clock=lambda: FIXED_TS)
    static = StaticEvaluator(cfg, evaluation_service=service, clock=lambda: FIXED_TS)
    semantic = SemanticEvaluator(
        cfg,
        evaluation_service=service,
        static_evaluator=static,
        backend=FakeBackend(),
        clock=lambda: FIXED_TS,
    )
    jobs = SemanticJobManager(
        cfg, evaluator=semantic, clock=lambda: FIXED_TS, executor=executor
    )
    trig = CompletionEvaluationTrigger(
        cfg,
        static_evaluator=static,
        executor=executor,
    )
    return Stack(service, static, semantic, jobs, trig)


def _run_manager(
    cfg: Config, *, hook, spawner: FakeSpawner | None = None, clock=None
) -> RunManager:
    return RunManager(
        cfg,
        readiness_checker=lambda r, c: ReadinessReport((ReadinessCheck("all", True, "ok"),)),
        spawner=spawner if spawner is not None else FakeSpawner(),
        pid_probe=lambda pid: False,
        clock=clock if clock is not None else (lambda: FIXED_TS),
        task_completed_hook=hook,
    )


def _wire(tmp_path: Path):
    """Real evaluation stack + real run manager, with only the model call faked."""
    cfg = config(tmp_path)
    stack = _stack(cfg)
    spawner = FakeSpawner()
    manager = _run_manager(cfg, hook=stack.trigger.on_task_completed, spawner=spawner)
    return cfg, stack.service, stack.jobs, manager, spawner


def _jobs_root(cfg: Config) -> Path:
    return cfg.data_dir / "semantic-jobs"


def test_a_finished_run_evaluates_its_task_without_operator_action(tmp_path):
    cfg, service, jobs, manager, spawner = _wire(tmp_path)
    target = _write_repo(tmp_path)
    before = _snapshot_tree(target.path)

    manager.start_run(target, TASK)
    _write_orch_log(target)
    spawner.procs[0].returncode = 0
    assert manager.list_runs()[0].state == "completed"

    # Exactly one evaluation, and it is the deterministic pair.
    stored = service.list_evaluations(target, TASK)
    assert len(stored) == 1
    assert set(stored[0].artifacts) == {EVIDENCE_NAME, STATIC_METRICS_NAME}

    # Nothing asked the semantic layer for anything: no job record was written at
    # all, so there is not even a deferred one for a later sweep to find.
    assert jobs.list_jobs() == []
    assert not _jobs_root(cfg).exists()

    # Read-only: the target repo is byte-identical apart from the log we wrote.
    after = _snapshot_tree(target.path)
    assert {k: v for k, v in after.items() if not k.startswith(".mandrel/")} == before


def test_a_second_completed_run_of_the_same_task_regenerates_layer_1_only(tmp_path):
    """The close-out-only re-run: the same task completes again, from its own evidence."""
    cfg, service, jobs, manager, spawner = _wire(tmp_path)
    target = _write_repo(tmp_path)

    manager.start_run(target, TASK)
    _write_orch_log(target)
    spawner.procs[0].returncode = 0
    manager.list_runs()

    manager.start_run(target, TASK)
    spawner.procs[1].returncode = 0
    assert [s.state for s in manager.list_runs()] == ["completed", "completed"]

    stored = service.list_evaluations(target, TASK)
    assert len(stored) == 2
    assert all(SEMANTIC_REPORT_NAME not in s.artifacts for s in stored)
    assert jobs.list_jobs() == []


@pytest.mark.parametrize("opt_out", [True, False])
def test_the_launch_flag_does_not_change_what_a_completion_produces(tmp_path, opt_out):
    """End to end: the retired opt-out reaches nothing, in either position."""
    cfg, service, jobs, manager, spawner = _wire(tmp_path)
    target = _write_repo(tmp_path)

    manager.start_run(target, TASK, auto_semantic_eval=not opt_out)
    _write_orch_log(target)
    spawner.procs[0].returncode = 0
    assert manager.list_runs()[0].state == "completed"

    stored = service.list_evaluations(target, TASK)
    assert len(stored) == 1
    assert set(stored[0].artifacts) == {EVIDENCE_NAME, STATIC_METRICS_NAME}
    # Neither an accepted job nor a recorded skip: the whole vocabulary is unused.
    assert jobs.list_jobs() == []


def test_layer_2_still_produces_a_complete_report_when_somebody_asks(tmp_path):
    """The manual path is untouched, and it is now the only path.

    The same completed task the trigger measured deterministically becomes a full
    L1+L2 report set the moment an operator starts a job for it — which is what
    keeps "automatic Layer 2 is gone" from meaning "Layer 2 is gone".
    """
    cfg = config(tmp_path)
    stack = _stack(cfg)
    spawner = FakeSpawner()
    manager = _run_manager(cfg, hook=stack.trigger.on_task_completed, spawner=spawner)
    target = _write_repo(tmp_path)

    manager.start_run(target, TASK)
    _write_orch_log(target)
    spawner.procs[0].returncode = 0
    assert manager.list_runs()[0].state == "completed"
    assert stack.jobs.list_jobs() == []

    stack.jobs.start_job(target, TASK)  # the explicit manual action

    job = stack.jobs.get_job(target, TASK)
    assert job.status == STATUS_COMPLETED and job.evaluation_id
    assert job.auto is False
    report_dir = cfg.data_dir / "evaluations" / "alpha" / TASK / job.evaluation_id
    assert (report_dir / EVIDENCE_NAME).is_file()
    assert (report_dir / STATIC_METRICS_NAME).is_file()
    assert (report_dir / SEMANTIC_REPORT_NAME).is_file()
    assert (report_dir / SEMANTIC_REPORT_MD_NAME).is_file()


def test_a_completion_survives_a_hub_that_stops_before_the_evaluation_runs(tmp_path):
    """A hub that stops mid-hook loses a free artifact and promises nothing else.

    ``_refresh`` persists ``ended_ts`` and never revisits that run, so a completion
    held only in a pending closure is gone when the hub stops. That used to be the
    argument for writing Layer 2's acceptance inline: a promise that outlived the
    process. With nothing durable owed, the loss is one deterministic artifact that
    the next run of this task — or one click — regenerates, and crucially the
    restarted hub finds no record that would make it spend anything.
    """
    cfg = config(tmp_path)
    held = DeferredExecutor()
    stack = _stack(cfg, executor=held)
    spawner = FakeSpawner()
    manager = _run_manager(cfg, hook=stack.trigger.on_task_completed, spawner=spawner)
    target = _write_repo(tmp_path)

    manager.start_run(target, TASK)
    _write_orch_log(target)
    spawner.procs[0].returncode = 0
    assert manager.list_runs()[0].state == "completed"

    assert stack.service.list_evaluations(target, TASK) == []
    assert stack.jobs.list_jobs() == []

    # The hub stops: whatever that thread was holding is dropped, never run.
    held.pending.clear()

    # Restart. The ended run is settled, so it is never announced a second time.
    announced: list[RunRecord] = []
    restarted = _run_manager(cfg, hook=announced.append)
    assert [s.state for s in restarted.list_runs()] == ["completed"]
    assert announced == []

    # Startup reconciliation finds nothing to settle and starts nothing.
    fresh = _stack(cfg)
    assert fresh.jobs.reconcile_orphans() == []
    assert fresh.jobs.reconcile_stuck() == []
    assert fresh.jobs.list_jobs() == []
    assert fresh.service.list_evaluations(target, TASK) == []


def test_a_legacy_deferred_job_is_never_started_by_a_restart_or_a_sweep(tmp_path):
    """The records the retired automatic path left behind stay exactly where they are.

    A hub that ran the automatic era holds one ``deferred`` record per task it
    accepted and never dispatched — jobs that were, by construction, waiting for a
    hub-side decision to spend. Nothing may make that decision now: not startup
    reconciliation, not the resident tick's sweeps, not a usage feed coming back,
    not a network reconnecting. The only thing that turns such a record into a
    turn is a person starting one.
    """
    cfg = config(tmp_path)
    target = _write_repo(tmp_path)
    legacy = SemanticJob(
        job_id="20260808T120000Z-legacy",
        repo_name=target.name,
        repo_path=target.path,
        task_id=TASK,
        status=STATUS_DEFERRED,
        defer_reason="capacity",
        auto=True,
        created_ts=(FIXED_TS - dt.timedelta(days=2)).isoformat(),
        updated_ts=(FIXED_TS - dt.timedelta(days=2)).isoformat(),
    )
    job_dir = _jobs_root(cfg) / "alpha" / TASK
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(
        json.dumps(
            {
                "job_id": legacy.job_id,
                "repo_name": legacy.repo_name,
                "repo_path": str(legacy.repo_path),
                "task_id": legacy.task_id,
                "status": legacy.status,
                "defer_reason": legacy.defer_reason,
                "auto": True,
                "created_ts": legacy.created_ts,
                "updated_ts": legacy.updated_ts,
            }
        ),
        encoding="utf-8",
    )

    # A restarted hub: both reconciliations plus the resident tick's sweeps.
    fresh = _stack(cfg)
    assert fresh.jobs.reconcile_orphans() == []
    assert fresh.jobs.reconcile_stuck() == []
    held = fresh.jobs.get_job(target, TASK)
    assert held.status == STATUS_DEFERRED and held.job_id == legacy.job_id
    assert fresh.service.list_evaluations(target, TASK) == []

    # And a completion arriving afterwards measures Layer 1 without touching it.
    spawner = FakeSpawner()
    manager = _run_manager(cfg, hook=fresh.trigger.on_task_completed, spawner=spawner)
    manager.start_run(target, TASK)
    _write_orch_log(target)
    spawner.procs[0].returncode = 0
    assert manager.list_runs()[0].state == "completed"
    assert fresh.jobs.get_job(target, TASK).status == STATUS_DEFERRED
    assert len(fresh.service.list_evaluations(target, TASK)) == 1

    # The one door out: an operator asking, which supersedes the held record.
    fresh.jobs.start_job(target, TASK)
    settled = fresh.jobs.get_job(target, TASK)
    assert settled.status == STATUS_COMPLETED
    assert settled.job_id != legacy.job_id and settled.auto is False


def test_a_hub_that_dies_before_the_evaluation_still_measures_the_task(tmp_path):
    """The window between the irreversible end write and the evaluation.

    ``run.json`` gets ``ended_ts`` first — and nothing re-observes a run that
    carries one — while the evaluation happens several settles later. The end is
    two-phase, so a restarted hub resumes the interrupted tail and the completion
    still gets measured. Interrupting here is the real thing rather than a held
    executor: this dies before any evaluation work begins.
    """
    cfg = config(tmp_path)
    spawner = FakeSpawner()

    class Interrupt(BaseException):
        """A hub dying: nothing on the end tail catches BaseException."""

    def dying_hook(record: RunRecord) -> None:
        raise Interrupt("killed between the end write and the evaluation")

    manager = _run_manager(cfg, hook=dying_hook, spawner=spawner)
    target = _write_repo(tmp_path)
    record = manager.start_run(target, TASK)
    _write_orch_log(target)
    spawner.procs[0].returncode = 0
    with pytest.raises(Interrupt):
        manager.list_runs()

    # The end is already permanent, and nothing has been evaluated.
    run_json = cfg.data_dir / "runs" / record.run_id / "run.json"
    assert json.loads(run_json.read_text(encoding="utf-8"))["ended_ts"] is not None

    # Restart: fresh managers, same data dir, same run records.
    fresh = _stack(cfg)
    restarted = _run_manager(cfg, hook=fresh.trigger.on_task_completed)
    assert [s.state for s in restarted.list_runs()] == ["completed"]

    stored = fresh.service.list_evaluations(target, TASK)
    assert len(stored) == 1  # the resumed tail ran the whole announcement
    assert set(stored[0].artifacts) == {EVIDENCE_NAME, STATIC_METRICS_NAME}
    assert fresh.jobs.list_jobs() == []

    # Settled for good: rescanning re-announces nothing.
    restarted.list_runs()
    assert len(fresh.service.list_evaluations(target, TASK)) == 1


def test_a_resumed_tail_hands_over_settled_entry_evidence(tmp_path, monkeypatch):
    """A tail resumed after the repo-side log is gone still evaluates settled evidence.

    A session-entry snapshot is keyed by bracket position until the log names the
    session on that bracket's *end* line, which only the end transition can
    settle. Interrupt the tail between the sessions snapshot and that settle,
    clean the repo-side log while the hub is down, and a resumed tail re-reading
    the log would settle nothing — silently, since settling is a no-op when it
    finds nothing — clear the still-settling mark, and announce a completion
    whose evidence cannot name the session it captured exactly. It settles from
    the run's own durable parse instead.
    """
    cfg = config(tmp_path)
    spawner = FakeSpawner()
    target = _write_repo(tmp_path)
    # Launch at the log's own start stamp, then scan inside the review session's
    # entry window — a fixed clock would refuse the capture as observed too late.
    at = [FIXED_TS]
    manager = _run_manager(
        cfg, hook=lambda record: None, spawner=spawner, clock=lambda: at[0]
    )
    manager.start_run(target, TASK)

    # A live scan captures the open review session's entry file — while its sid
    # is still `(pending)`, which is the join the end transition owes it.
    log = _write_orch_log(target, lines=COMPLETING_LOG[:4])
    at[0] = FIXED_TS + dt.timedelta(seconds=11 * 60 + 30)
    assert manager.list_runs()[0].state == "active"

    class Interrupt(BaseException):
        """A hub dying: nothing on the end tail catches BaseException."""

    log = _write_orch_log(target)  # the end line reveals sid=rev1
    monkeypatch.setattr(
        "orch_hub.runs.settle_snapshots",
        lambda *a, **k: (_ for _ in ()).throw(Interrupt("killed mid-tail")),
    )
    spawner.procs[0].returncode = 0
    with pytest.raises(Interrupt):
        manager.list_runs()

    monkeypatch.undo()
    log.unlink()  # the repo-side log is cleaned while the hub is down

    fresh = _stack(cfg)
    restarted = _run_manager(cfg, hook=fresh.trigger.on_task_completed)
    assert [s.state for s in restarted.list_runs()] == ["completed"]

    stored = fresh.service.list_evaluations(target, TASK)
    assert len(stored) == 1
    evidence = json.loads(
        (
            cfg.data_dir / "evaluations" / "alpha" / TASK / stored[0].evaluation_id
            / EVIDENCE_NAME
        ).read_text(encoding="utf-8")
    )
    entries = evidence["runs"][0]["task_snapshots"]["snapshots"]
    # Both sessions accounted for, and the captured one joined to its real sid —
    # the evidence the pack's entry fidelity is computed from.
    assert [(e["index"], e["sid"]) for e in entries] == [(0, "dev1"), (1, "rev1")]
    assert entries[1]["present"] and not entries[1]["truncated"]
    # The counts beside it are what the pack's fidelity signal is computed from;
    # the sid above is what lets a consumer reach those captured bytes at all.
    assert evidence["summary"]["task_entry_sessions"] == 2
    assert evidence["summary"]["task_entries_exact"] == 1
    assert evidence["summary"]["task_entry_fidelity"] == "partial"
