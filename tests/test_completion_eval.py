"""Tests for completion-triggered task evaluation (:mod:`orch_hub.completion_eval`).

Covers the trigger's own contract over fake layers — both gates (subsystem and
``ORCH_HUB_EVAL_AUTO``) checked before any work is dispatched, work handed to the
executor rather than run on the caller's thread, an unattributable run record
refused, Layer 1 run immediately, Layer 2 enqueued through the once-per-task
guard, and every failure isolated — plus one end-to-end integration test that
drives a *real* run manager, evaluation service, static evaluator, semantic
evaluator (fake backend, so no model call), and job manager: a run that finishes
its task lands both layers' artifacts with no operator action, and a second
completed run of the same task adds no second semantic turn.
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
    STATUS_FAILED,
    STATUS_QUEUED,
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


class FakeJobs:
    """Stands in for :class:`SemanticJobManager`: the ``start_job_if_absent`` seam."""

    def __init__(self, *, error: Exception | None = None, already_recorded: bool = False):
        self.calls: list[tuple[ManagedRepo, str]] = []
        self._error = error
        self._already_recorded = already_recorded

    def start_job_if_absent(self, r: ManagedRepo, task_id: str) -> SemanticJob | None:
        self.calls.append((r, task_id))
        if self._error is not None:
            raise self._error
        if self._already_recorded:
            return None
        return SemanticJob(
            job_id="20260809T030000Z",
            repo_name=r.name,
            repo_path=r.path,
            task_id=task_id,
            status="queued",
        )


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
    jobs=None,
    executor=sync_executor,
    enabled: bool = True,
    auto: bool = True,
) -> tuple[CompletionEvaluationTrigger, FakeStatic, FakeJobs]:
    static = static if static is not None else FakeStatic()
    jobs = jobs if jobs is not None else FakeJobs()
    return (
        CompletionEvaluationTrigger(
            config(tmp_path, enabled=enabled, auto=auto),
            static_evaluator=static,
            job_manager=jobs,
            executor=executor,
        ),
        static,
        jobs,
    )


# -- the happy path -----------------------------------------------------------


def test_a_completed_task_gets_layer_1_now_and_layer_2_enqueued(tmp_path):
    trig, static, jobs = trigger(tmp_path)

    trig.on_task_completed(completed_record())

    assert [(r.name, t) for r, t in static.calls] == [("alpha", TASK)]
    assert [(r.name, t) for r, t in jobs.calls] == [("alpha", TASK)]


def test_the_repo_is_reconstructed_from_the_run_record_alone(tmp_path):
    """No registry lookup: the record carries everything the evaluation needs."""
    trig, static, _ = trigger(tmp_path)

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
    trig, static, jobs = trigger(
        tmp_path, executor=executor, enabled=enabled, auto=auto
    )

    assert trig.enabled is expect_enabled

    trig.on_task_completed(completed_record())

    # A closed gate does no work at all — not even a dispatch.
    assert bool(executor.pending) is expect_enabled
    executor.run_all()
    assert bool(static.calls) is expect_enabled
    assert bool(jobs.calls) is expect_enabled


def test_automatic_triggering_is_switchable_without_disabling_the_subsystem(tmp_path):
    """``ORCH_HUB_EVAL_AUTO=false`` leaves manual evaluation completely intact."""
    trig, static, jobs = trigger(tmp_path, auto=False)
    cfg = config(tmp_path, auto=False)

    trig.on_task_completed(completed_record())

    assert static.calls == [] and jobs.calls == []
    assert cfg.evaluation_enabled is True  # the subsystem, and its routes, stay on


# -- dispatch -----------------------------------------------------------------


def test_the_layer_2_job_is_recorded_before_the_hook_returns(tmp_path):
    """The completion signal is one-shot: acceptance may not wait on a thread.

    The caller holds the run-manager lock, so only O(1) work may run inline — and
    the durable Layer 2 record is exactly that, while Layer 1's evidence build is
    what gets detached.
    """
    executor = DeferredExecutor()
    trig, static, jobs = trigger(tmp_path, executor=executor)

    trig.on_task_completed(completed_record())

    assert len(jobs.calls) == 1  # durable acceptance, before returning
    assert static.calls == []  # the slow layer did not run on the caller's thread
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
    trig, static, jobs = trigger(tmp_path, executor=executor)

    trig.on_task_completed(record)  # must not raise

    assert executor.pending == []
    assert static.calls == [] and jobs.calls == []


def test_a_layer_1_failure_leaves_the_durable_layer_2_job_standing(tmp_path):
    """The two layers are separate artifacts over separately generated evidence."""
    trig, static, jobs = trigger(
        tmp_path, static=FakeStatic(error=OSError("evidence unreadable"))
    )

    trig.on_task_completed(completed_record())  # must not raise

    assert len(static.calls) == 1
    assert len(jobs.calls) == 1  # already accepted, and untouched by the failure


def test_a_layer_2_enqueue_failure_is_isolated(tmp_path):
    trig, static, jobs = trigger(
        tmp_path, jobs=FakeJobs(error=RuntimeError("job store broken"))
    )

    trig.on_task_completed(completed_record())  # must not raise

    assert len(jobs.calls) == 1
    # A broken job store costs this task its Layer 2, not its deterministic layer.
    assert len(static.calls) == 1


def test_a_task_that_already_has_a_job_record_enqueues_no_second_turn(tmp_path):
    """The once-per-task guard lives in the job manager; the trigger just obeys it."""
    trig, static, jobs = trigger(tmp_path, jobs=FakeJobs(already_recorded=True))

    trig.on_task_completed(completed_record())
    trig.on_task_completed(completed_record())

    assert len(jobs.calls) == 2  # asked twice
    # Layer 1 is never skipped: it makes no model call, so a repeated completion
    # signal costs nothing and the deterministic layer stays complete.
    assert len(static.calls) == 2


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

    def evaluate(self, prompt: str, r: ManagedRepo) -> str:
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
        cfg, static_evaluator=static, job_manager=jobs, executor=executor
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


def test_a_finished_run_evaluates_its_task_without_operator_action(tmp_path):
    cfg, service, jobs, manager, spawner = _wire(tmp_path)
    target = _write_repo(tmp_path)
    before = _snapshot_tree(target.path)

    manager.start_run(target, TASK)
    _write_orch_log(target)
    spawner.procs[0].returncode = 0
    assert manager.list_runs()[0].state == "completed"

    # Layer 2 settled (synchronous executor) and links a complete report set.
    job = jobs.get_job(target, TASK)
    assert job.status == STATUS_COMPLETED and job.evaluation_id
    semantic_dir = (
        cfg.data_dir / "evaluations" / "alpha" / TASK / job.evaluation_id
    )
    assert (semantic_dir / EVIDENCE_NAME).is_file()
    assert (semantic_dir / STATIC_METRICS_NAME).is_file()
    assert (semantic_dir / SEMANTIC_REPORT_NAME).is_file()
    assert (semantic_dir / SEMANTIC_REPORT_MD_NAME).is_file()

    # Layer 1 ran on its own, immediately: a second, deterministic-only evaluation.
    stored = service.list_evaluations(target, TASK)
    assert len(stored) == 2
    layer_1 = [s for s in stored if s.evaluation_id != job.evaluation_id]
    assert len(layer_1) == 1
    assert set(layer_1[0].artifacts) == {EVIDENCE_NAME, STATIC_METRICS_NAME}

    # Read-only: the target repo is byte-identical apart from the log we wrote.
    after = _snapshot_tree(target.path)
    assert {k: v for k, v in after.items() if not k.startswith(".mandrel/")} == before


def test_a_second_completed_run_of_the_same_task_spends_no_second_model_turn(tmp_path):
    cfg, service, jobs, manager, spawner = _wire(tmp_path)
    target = _write_repo(tmp_path)

    manager.start_run(target, TASK)
    _write_orch_log(target)
    spawner.procs[0].returncode = 0
    manager.list_runs()
    first_job = jobs.get_job(target, TASK)

    # The close-out-only re-run: the same task completes again from its own
    # evidence, because the completion signal is per run, not per task.
    manager.start_run(target, TASK)
    spawner.procs[1].returncode = 0
    assert [s.state for s in manager.list_runs()] == ["completed", "completed"]

    assert jobs.get_job(target, TASK).job_id == first_job.job_id
    # One semantic report set, ever; the extra L1-only dir is deterministic and free.
    stored = service.list_evaluations(target, TASK)
    assert sum(1 for s in stored if SEMANTIC_REPORT_NAME in s.artifacts) == 1
    assert sum(1 for s in stored if SEMANTIC_REPORT_NAME not in s.artifacts) == 2


def test_a_completion_survives_a_hub_that_stops_before_the_evaluation_runs(tmp_path):
    """The completion signal settles once, so acceptance must outlive the process.

    ``_refresh`` persists ``ended_ts`` and never revisits that run: a completion
    held only in a pending closure would be gone when the hub stops — silently,
    with nothing on disk left to reconcile — and the restarted manager returns
    early for the already-ended run rather than announcing it again. Here neither
    layer ever runs: both executors are left holding their work, exactly as an
    interpreter exit leaves its daemon threads.
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

    # No evaluation work has run at all — and the completion is durable anyway.
    assert stack.service.list_evaluations(target, TASK) == []
    assert stack.jobs.get_job(target, TASK).status == STATUS_QUEUED
    assert (cfg.data_dir / "semantic-jobs" / "alpha" / TASK / "job.json").is_file()

    # The hub stops: whatever those threads were holding is dropped, never run.
    held.pending.clear()

    # Restart. The ended run is settled, so it is never announced a second time...
    announced: list[RunRecord] = []
    restarted = _run_manager(cfg, hook=announced.append)
    assert [s.state for s in restarted.list_runs()] == ["completed"]
    assert announced == []

    # ...which leaves the durable record as the only thing still speaking for this
    # completion. The startup sweep settles it to a visible, retryable failure —
    # a recorded outcome rather than a task that quietly left the corpus.
    fresh = _stack(cfg, executor=held)
    reconciled = fresh.jobs.reconcile_orphans()
    assert [j.task_id for j in reconciled] == [TASK]
    settled = fresh.jobs.get_job(target, TASK)
    assert settled.status == STATUS_FAILED
    assert "hub restarted" in (settled.error or "")


def test_a_hub_that_dies_before_the_job_record_still_evaluates_the_task(tmp_path):
    """The window between the irreversible end write and durable acceptance.

    ``run.json`` gets ``ended_ts`` first — and nothing re-observes a run that
    carries one — while the job record is written by the hook several settles
    later. A process that died in between used to leave *nothing*: the restarted
    manager returned early for the already-ended run, no record existed for the
    startup sweep to reconcile, and the task left the corpus with no trace that
    it had ever completed. The end is two-phase now, so the restarted hub resumes
    the interrupted tail instead. Interrupting here is the real thing rather than
    a held executor: this dies *before* Layer 2 is enqueued at all.
    """
    cfg = config(tmp_path)
    spawner = FakeSpawner()

    class Interrupt(BaseException):
        """A hub dying: nothing on the end tail catches BaseException."""

    def dying_hook(record: RunRecord) -> None:
        raise Interrupt("killed between the end write and the job record")

    manager = _run_manager(cfg, hook=dying_hook, spawner=spawner)
    target = _write_repo(tmp_path)
    record = manager.start_run(target, TASK)
    _write_orch_log(target)
    spawner.procs[0].returncode = 0
    with pytest.raises(Interrupt):
        manager.list_runs()

    # The end is already permanent, and nothing anywhere has accepted it.
    run_json = cfg.data_dir / "runs" / record.run_id / "run.json"
    assert json.loads(run_json.read_text(encoding="utf-8"))["ended_ts"] is not None
    assert not (cfg.data_dir / "semantic-jobs" / "alpha" / TASK / "job.json").exists()

    # Restart: fresh managers, same data dir, same run records.
    fresh = _stack(cfg)
    restarted = _run_manager(cfg, hook=fresh.trigger.on_task_completed)
    assert [s.state for s in restarted.list_runs()] == ["completed"]

    # The completion is durable and fully evaluated — both layers, no operator.
    job = fresh.jobs.get_job(target, TASK)
    assert job.status == STATUS_COMPLETED and job.evaluation_id
    semantic_dir = cfg.data_dir / "evaluations" / "alpha" / TASK / job.evaluation_id
    assert (semantic_dir / SEMANTIC_REPORT_NAME).is_file()
    stored = fresh.service.list_evaluations(target, TASK)
    assert len(stored) == 2  # the resumed tail ran the whole announcement

    # Settled for good: rescanning spends no second turn.
    restarted.list_runs()
    assert fresh.jobs.get_job(target, TASK).job_id == job.job_id
    assert len(fresh.service.list_evaluations(target, TASK)) == 2


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

    job = fresh.jobs.get_job(target, TASK)
    assert job.status == STATUS_COMPLETED
    evidence = json.loads(
        (
            cfg.data_dir / "evaluations" / "alpha" / TASK / job.evaluation_id
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
