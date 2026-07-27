"""Tests for repo-level admission: a live run and a remote intake never coexist.

Everything runs against fake repos in ``tmp_path`` with injected spawners,
agents, readiness checkers, and clocks — no orchestrator subprocess is ever
started and no intake model is ever invoked. Both managers are wired to ONE
:class:`~orch_hub.admission.RepoAdmission`, exactly as the app wires them, so
the tests exercise the real cross-subsystem gate rather than a stand-in.

The concurrency tests are deterministic, not timing-hopeful: the winner is
parked *inside* the admission section by a blocking readiness gate, and the
loser's blocked-ness is proven by a bounded ``join`` that must time out.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import threading
from pathlib import Path

import pytest

from orch_hub.admission import (
    AdmissionConflictError,
    IntakeInFlightError,
    RepoAdmission,
    RunLiveError,
)
from orch_hub.config import Config
from orch_hub.intake import (
    AgentConfirmResult,
    AgentDraftResult,
    IntakeManager,
    STATUS_AWAITING_REVIEW,
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_DRAFTING,
    STATUS_FAILED,
)
from orch_hub.models import (
    IntakeDraft,
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
)
from orch_hub.runs import RunManager

FIXED_NOW = dt.datetime(2026, 7, 24, 6, 0, 0, tzinfo=dt.timezone.utc)
TASK_ID = "2026-07-05-feature-x"
DRAFT_TASK_ID = "2026-07-24-add-widget"
REQUEST = "Add a widget endpoint to the dashboard"
BLOCKED_JOIN = 0.25
"""How long a thread that must be blocked is given to prove otherwise."""


# -- fakes ---------------------------------------------------------------------


class FakeClock:
    def __init__(self, now: dt.datetime = FIXED_NOW):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def tick(self, seconds: int = 60) -> None:
        self.now += dt.timedelta(seconds=seconds)


class FakeProc:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class FakeSpawner:
    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.procs: list[FakeProc] = []
        self._pids = itertools.count(4000)

    def __call__(self, argv, *, cwd, stdout, env, start_new_session):
        self.calls.append(tuple(argv))
        proc = FakeProc(next(self._pids))
        self.procs.append(proc)
        return proc


class FakePids:
    """Pid liveness for runs a manager did not spawn (the reattach path).

    A manager that holds the process handle never consults this; it matters when
    a second manager over the same data dir reads another's run — the shape of a
    hub restart, and of the recovered-overlap fixture below.
    """

    def __init__(self):
        self.dead: set[int] = set()

    def __call__(self, pid: int) -> bool:
        return pid not in self.dead

    def kill(self, pid: int) -> None:
        self.dead.add(pid)


class FakeSession:
    def __init__(self, request: str):
        self.request = request
        self.feedback: list[str] = []
        self.cancelled = False


def _draft(iteration: int = 0) -> IntakeDraft:
    return IntakeDraft(
        task_id=DRAFT_TASK_ID,
        summary="Add a widget",
        task_file=(
            f"---\nid: {DRAFT_TASK_ID}\nstatus: pending\nsession-est: 0/2\n"
            f"---\n\n# Add a widget\n\nDraft iteration {iteration}.\n"
        ),
        index_entry=f"| {DRAFT_TASK_ID} | Add a widget | pending | 0/2 | [] |",
        notes=None,
    )


class FakeIntakeAgent:
    """Read-only draft/revise; ``confirm`` performs the protocol-defined write."""

    def __init__(self):
        self.draft_calls = 0
        self.revise_calls = 0
        self.confirm_calls = 0
        self.cancel_calls = 0

    def draft(self, request, repo) -> AgentDraftResult:
        self.draft_calls += 1
        return AgentDraftResult(handle=FakeSession(request), ref="sess-1", draft=_draft())

    def revise(self, handle, feedback, repo) -> AgentDraftResult:
        self.revise_calls += 1
        handle.feedback.append(feedback)
        return AgentDraftResult(
            handle=handle, ref="sess-1", draft=_draft(len(handle.feedback))
        )

    def confirm(self, handle, repo) -> AgentConfirmResult:
        self.confirm_calls += 1
        draft = _draft()
        tasks = repo.path / ".ai-tasks"
        (tasks / f"{draft.task_id}.md").write_text(draft.task_file, encoding="utf-8")
        index = tasks / "index.md"
        index.write_text(
            index.read_text(encoding="utf-8") + draft.index_entry + "\n",
            encoding="utf-8",
        )
        return AgentConfirmResult(task_id=draft.task_id)

    def cancel(self, handle) -> None:
        self.cancel_calls += 1
        handle.cancelled = True


def passing_readiness(repo, config) -> ReadinessReport:
    return ReadinessReport((ReadinessCheck("all", True, "ok"),))


def dirty_readiness(repo, config) -> ReadinessReport:
    """What readiness really reports while an orchestrator run is rewriting the
    tree. Used to prove admission is not reported through this channel."""
    return ReadinessReport(
        (ReadinessCheck("git_clean", False, "working tree has uncommitted changes"),)
    )


def sync_executor(run) -> None:
    run()


# -- fixtures ------------------------------------------------------------------


def make_repo(root: Path, name: str = "repo-a") -> ManagedRepo:
    """Fake managed repo with one launchable active task (``TASK_ID``)."""
    path = root / "repos" / name
    (path / ".ai-tasks").mkdir(parents=True)
    (path / ".ai-tasks" / "index.md").write_text(
        "# Active tasks\n\n| Task | Status | Summary |\n|---|---|---|\n"
        f"| {TASK_ID} | x | Summary of {TASK_ID} |\n",
        encoding="utf-8",
    )
    (path / ".ai-tasks" / f"{TASK_ID}.md").write_text(
        f"---\nid: {TASK_ID}\nstatus: in_progress\n---\n\n# Task\n",
        encoding="utf-8",
    )
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


class Hub:
    """The two managers the hub runs, sharing one admission coordinator."""

    def __init__(self, config, runs, intakes, spawner, agent, clock, pids):
        self.config = config
        self.runs = runs
        self.intakes = intakes
        self.spawner = spawner
        self.agent = agent
        self.clock = clock
        self.pids = pids

    def end_run(self, index: int = 0) -> None:
        """End a live run the way the manager observes it: the process exits, and
        the next status scan persists ``ended_ts``.

        Both liveness channels are settled — the handle a spawning manager holds
        and the pid another manager would probe — so an end is an end regardless
        of which manager observes it.
        """
        proc = self.spawner.procs[index]
        proc.returncode = 0
        self.pids.kill(proc.pid)
        self.runs.list_runs()

    def settle(self, transient):
        return self.intakes.get_intake(transient.intake_id)


def make_hub(
    root: Path,
    *,
    run_readiness=passing_readiness,
    intake_readiness=passing_readiness,
) -> Hub:
    """Both managers over one data dir and one shared admission coordinator."""
    config = Config(ai_native_path=root / "ai-native", data_dir=root / "hub-data")
    clock = FakeClock()
    spawner = FakeSpawner()
    agent = FakeIntakeAgent()
    pids = FakePids()
    admission = RepoAdmission()
    runs = RunManager(
        config,
        readiness_checker=run_readiness,
        spawner=spawner,
        pid_probe=pids,
        clock=clock,
        admission=admission,
    )
    intakes = IntakeManager(
        config,
        agent=agent,
        readiness_checker=intake_readiness,
        clock=clock,
        executor=sync_executor,
        admission=admission,
    )
    return Hub(config, runs, intakes, spawner, agent, clock, pids)


def write_question(control_dir: Path, seq: int, **fields) -> None:
    """Drop a control-dir question file the way the orchestrator would."""
    control_dir.joinpath(f"{seq:03d}-question.json").write_text(
        json.dumps({"seq": seq, **fields}), encoding="utf-8"
    )


def snapshot(repo: ManagedRepo) -> dict[str, bytes]:
    tasks = repo.path / ".ai-tasks"
    return {p.name: p.read_bytes() for p in sorted(tasks.iterdir()) if p.is_file()}


# -- the coordinator itself ----------------------------------------------------


def test_unbound_coordinator_admits_both_kinds(tmp_path):
    """A process with no peer subsystem has nothing to conflict with."""
    repo = make_repo(tmp_path)
    admission = RepoAdmission()

    with admission.admit_run(repo):
        pass
    with admission.admit_intake(repo):
        pass
    admission.require_no_live_run(repo, "revise an intake")


def test_admit_run_and_admit_intake_are_mutually_exclusive_per_repo(tmp_path):
    repo = make_repo(tmp_path)
    admission = RepoAdmission()
    entered: list[str] = []

    def take_intake_section() -> None:
        with admission.admit_intake(repo):
            entered.append("intake")

    with admission.admit_run(repo):
        thread = threading.Thread(target=take_intake_section)
        thread.start()
        thread.join(BLOCKED_JOIN)
        assert thread.is_alive()  # blocked on the repo's admission section
        assert entered == []

    thread.join(5)
    assert entered == ["intake"]


def test_admission_sections_for_different_repos_do_not_block(tmp_path):
    """Cross-repo runs may overlap; the section is per repo, not global."""
    repo_a = make_repo(tmp_path, "repo-a")
    repo_b = make_repo(tmp_path, "repo-b")
    admission = RepoAdmission()

    with admission.admit_run(repo_a):
        with admission.admit_intake(repo_b):
            pass  # would deadlock under a single global lock


def test_bound_probes_refuse_the_other_kind(tmp_path):
    repo = make_repo(tmp_path)
    admission = RepoAdmission()
    admission.bind_run_source(lambda r: "run-1")
    admission.bind_intake_source(lambda r: "intake-1")

    with pytest.raises(RunLiveError) as intake_exc:
        with admission.admit_intake(repo):
            pass
    with pytest.raises(IntakeInFlightError) as run_exc:
        with admission.admit_run(repo):
            pass

    assert intake_exc.value.run_id == "run-1"
    assert run_exc.value.intake_id == "intake-1"
    assert isinstance(intake_exc.value, AdmissionConflictError)
    assert isinstance(run_exc.value, AdmissionConflictError)


def test_a_refused_admission_releases_the_section(tmp_path):
    """A conflict must not leave the repo's section held forever."""
    repo = make_repo(tmp_path)
    admission = RepoAdmission()
    admission.bind_run_source(lambda r: "run-1")

    with pytest.raises(RunLiveError):
        with admission.admit_intake(repo):
            pass

    admission.bind_run_source(lambda r: None)
    with admission.admit_intake(repo):  # section is free again
        pass


def test_require_no_live_run_reports_the_action_and_run(tmp_path):
    repo = make_repo(tmp_path)
    admission = RepoAdmission()
    admission.bind_run_source(lambda r: "run-7")

    with pytest.raises(RunLiveError) as exc:
        admission.require_no_live_run(repo, "confirm an intake")

    assert exc.value.run_id == "run-7"
    assert exc.value.action == "confirm an intake"
    assert "confirm an intake" in str(exc.value)


# -- occupancy probes ----------------------------------------------------------


def test_live_run_probe_reports_active_waiting_and_clears_on_end(tmp_path):
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    assert hub.runs.live_run_for(repo) is None

    record = hub.runs.start_run(repo, TASK_ID)
    assert hub.runs.live_run_for(repo) == record.run_id  # active

    write_question(record.control_dir, 1, kind="blocked", banner="Pick A or B?")
    assert hub.runs.list_runs()[0].state == "waiting"
    assert hub.runs.live_run_for(repo) == record.run_id  # waiting is still live

    hub.end_run()
    assert hub.runs.live_run_for(repo) is None


def test_live_run_probe_holds_a_dead_run_until_its_end_is_persisted(tmp_path):
    """Occupancy is the persisted record, not current process liveness.

    Between an orchestrator exiting and the hub's next status scan, ``run.json``
    still reads ``ended_ts: null`` — the run's own state machine has not ended
    it, and the dying process may still be writing the tree. Reading liveness
    here instead would report the repo free for that whole window and admit an
    intake into it.
    """
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    record = hub.runs.start_run(repo, TASK_ID)
    run_json = hub.runs.runs_dir / record.run_id / "run.json"

    hub.spawner.procs[0].returncode = 0  # the process exits...
    hub.pids.kill(hub.spawner.procs[0].pid)
    assert json.loads(run_json.read_text())["ended_ts"] is None  # ...unobserved

    assert hub.runs.live_run_for(repo) == record.run_id
    with pytest.raises(RunLiveError) as exc:
        hub.intakes.start_intake(repo, REQUEST)
    assert exc.value.run_id == record.run_id
    assert hub.intakes.list_intakes() == []

    hub.runs.list_runs()  # the scan persists the end
    assert json.loads(run_json.read_text())["ended_ts"] is not None
    assert hub.runs.live_run_for(repo) is None
    assert hub.settle(hub.intakes.start_intake(repo, REQUEST)).status == (
        STATUS_AWAITING_REVIEW
    )


def test_live_run_probe_is_scoped_to_the_repo(tmp_path):
    repo_a = make_repo(tmp_path, "repo-a")
    repo_b = make_repo(tmp_path, "repo-b")
    hub = make_hub(tmp_path)

    record = hub.runs.start_run(repo_a, TASK_ID)

    assert hub.runs.live_run_for(repo_a) == record.run_id
    assert hub.runs.live_run_for(repo_b) is None


@pytest.mark.parametrize("terminal", [STATUS_CANCELLED, STATUS_CONFIRMED])
def test_active_intake_probe_clears_on_terminal_status(tmp_path, terminal):
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    assert hub.intakes.active_intake_for(repo) is None

    transient = hub.intakes.start_intake(repo, REQUEST)
    assert hub.settle(transient).status == STATUS_AWAITING_REVIEW
    assert hub.intakes.active_intake_for(repo) == transient.intake_id

    if terminal == STATUS_CANCELLED:
        hub.intakes.cancel_intake(transient.intake_id)
    else:
        hub.settle(hub.intakes.confirm_intake(transient.intake_id))
    assert hub.intakes.get_intake(transient.intake_id).status == terminal
    assert hub.intakes.active_intake_for(repo) is None


def test_active_intake_probe_is_scoped_to_the_repo(tmp_path):
    repo_a = make_repo(tmp_path, "repo-a")
    repo_b = make_repo(tmp_path, "repo-b")
    hub = make_hub(tmp_path)

    transient = hub.intakes.start_intake(repo_a, REQUEST)

    assert hub.intakes.active_intake_for(repo_a) == transient.intake_id
    assert hub.intakes.active_intake_for(repo_b) is None


# -- intake start is refused during a live run ---------------------------------


@pytest.mark.parametrize("waiting", [False, True], ids=["active", "waiting"])
def test_intake_start_is_refused_while_a_run_is_live(tmp_path, waiting):
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    record = hub.runs.start_run(repo, TASK_ID)
    if waiting:
        write_question(record.control_dir, 1, kind="blocked", banner="Pick A or B?")
        assert hub.runs.list_runs()[0].state == "waiting"

    with pytest.raises(RunLiveError) as exc:
        hub.intakes.start_intake(repo, REQUEST)

    assert exc.value.run_id == record.run_id
    # Refused BEFORE any record or agent turn existed (acceptance: no intake
    # record is created and the intake agent is never invoked).
    assert hub.intakes.list_intakes() == []
    assert hub.agent.draft_calls == 0


def test_intake_start_is_admitted_after_the_run_ends(tmp_path):
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    hub.runs.start_run(repo, TASK_ID)
    hub.end_run()

    transient = hub.intakes.start_intake(repo, REQUEST)

    assert hub.settle(transient).status == STATUS_AWAITING_REVIEW


def test_intake_start_in_another_repo_is_unaffected_by_a_live_run(tmp_path):
    repo_a = make_repo(tmp_path, "repo-a")
    repo_b = make_repo(tmp_path, "repo-b")
    hub = make_hub(tmp_path)
    hub.runs.start_run(repo_a, TASK_ID)

    transient = hub.intakes.start_intake(repo_b, REQUEST)

    assert hub.settle(transient).status == STATUS_AWAITING_REVIEW


def test_live_run_conflict_is_reported_instead_of_a_dirty_tree(tmp_path):
    """Admission must not be surfaced through readiness.

    A live run dirties the working tree, so readiness genuinely fails with
    ``git_clean`` during one. The refusal must still name the run.
    """
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path, intake_readiness=dirty_readiness)
    record = hub.runs.start_run(repo, TASK_ID)

    with pytest.raises(RunLiveError) as exc:
        hub.intakes.start_intake(repo, REQUEST)

    assert exc.value.run_id == record.run_id
    assert "git_clean" not in str(exc.value)


# -- run start is refused during a non-terminal intake -------------------------


def test_run_start_is_refused_while_an_intake_is_drafting(tmp_path):
    """``drafting`` is transient: the turn has not settled yet."""
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    # A never-running executor leaves the record parked in ``drafting``, which is
    # what an in-flight (slow, real) model turn looks like from outside.
    hub.intakes._executor = lambda run: None
    transient = hub.intakes.start_intake(repo, REQUEST)
    assert hub.intakes.get_intake(transient.intake_id).status == STATUS_DRAFTING

    with pytest.raises(IntakeInFlightError) as exc:
        hub.runs.start_run(repo, TASK_ID)

    assert exc.value.intake_id == transient.intake_id
    assert hub.spawner.calls == []  # refused before spawning
    assert hub.runs.list_runs() == []  # and before creating a run record


def test_run_start_is_refused_while_an_intake_awaits_review(tmp_path):
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    transient = hub.intakes.start_intake(repo, REQUEST)
    assert hub.settle(transient).status == STATUS_AWAITING_REVIEW

    with pytest.raises(IntakeInFlightError) as exc:
        hub.runs.start_run(repo, TASK_ID)

    assert exc.value.intake_id == transient.intake_id
    assert hub.spawner.calls == []
    assert hub.runs.list_runs() == []


@pytest.mark.parametrize("action", ["revise", "confirm"])
def test_run_start_is_refused_while_an_intake_turn_is_in_flight(tmp_path, action):
    """``revising``/``confirming`` are transient too: both hold the repo."""
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    transient = hub.intakes.start_intake(repo, REQUEST)
    hub.settle(transient)
    hub.intakes._executor = lambda run: None  # park the next turn mid-flight
    if action == "revise":
        parked = hub.intakes.revise_intake(transient.intake_id, "tighten the scope")
    else:
        parked = hub.intakes.confirm_intake(transient.intake_id)
    assert parked.status in ("revising", "confirming")

    with pytest.raises(IntakeInFlightError) as exc:
        hub.runs.start_run(repo, TASK_ID)

    assert exc.value.intake_id == transient.intake_id
    assert hub.spawner.calls == []


@pytest.mark.parametrize("terminal", [STATUS_CANCELLED, STATUS_CONFIRMED])
def test_run_start_is_admitted_once_the_intake_settles(tmp_path, terminal):
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    transient = hub.intakes.start_intake(repo, REQUEST)
    hub.settle(transient)
    if terminal == STATUS_CANCELLED:
        hub.intakes.cancel_intake(transient.intake_id)
    else:
        assert hub.settle(hub.intakes.confirm_intake(transient.intake_id)).status == (
            STATUS_CONFIRMED
        )

    record = hub.runs.start_run(repo, TASK_ID)

    assert hub.runs.get_run(record.run_id).state == "active"


def test_run_start_is_admitted_after_a_failed_intake(tmp_path):
    """A failed intake is a resting outcome and must not hold the repo."""
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    transient = hub.intakes.start_intake(repo, REQUEST)
    hub.settle(transient)
    # A hub restart drops the conversational handle: the next revise fails the
    # intake as a lost session, which is terminal.
    hub.intakes._handles.clear()
    assert hub.intakes.revise_intake(transient.intake_id, "more detail").status == (
        STATUS_FAILED
    )

    record = hub.runs.start_run(repo, TASK_ID)

    assert hub.runs.get_run(record.run_id).state == "active"


def test_run_start_in_another_repo_is_unaffected_by_an_intake(tmp_path):
    repo_a = make_repo(tmp_path, "repo-a")
    repo_b = make_repo(tmp_path, "repo-b")
    hub = make_hub(tmp_path)
    hub.settle(hub.intakes.start_intake(repo_a, REQUEST))

    record = hub.runs.start_run(repo_b, TASK_ID)

    assert hub.runs.get_run(record.run_id).state == "active"


# -- existing same-kind guarantees stay intact ---------------------------------


def test_second_live_run_in_one_repo_is_still_refused(tmp_path):
    from orch_hub.runs import RepoBusyError

    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    first = hub.runs.start_run(repo, TASK_ID)

    with pytest.raises(RepoBusyError) as exc:
        hub.runs.start_run(repo, TASK_ID)

    assert exc.value.run_id == first.run_id


def test_second_in_flight_intake_in_one_repo_is_still_refused(tmp_path):
    from orch_hub.intake import IntakeBusyError

    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    first = hub.intakes.start_intake(repo, REQUEST)

    with pytest.raises(IntakeBusyError) as exc:
        hub.intakes.start_intake(repo, "a second request")

    assert exc.value.intake_id == first.intake_id


# -- recovered overlap ---------------------------------------------------------


def overlapped(tmp_path) -> tuple[Hub, ManagedRepo, str, str]:
    """A repo carrying BOTH an awaiting_review intake and a live run.

    Admission prevents this pair from forming, but cannot undo one that already
    exists: a hub restart reattaches a live run while a persisted
    ``awaiting_review`` intake survives (startup reconciliation only fails the
    transient trio). Built here by starting the intake first and launching the
    run through a manager that is not yet admission-coupled — the same state a
    pre-feature hub leaves on disk.
    """
    repo = make_repo(tmp_path)
    hub = make_hub(tmp_path)
    transient = hub.intakes.start_intake(repo, REQUEST)
    assert hub.settle(transient).status == STATUS_AWAITING_REVIEW

    uncoupled = RunManager(
        hub.config,
        readiness_checker=passing_readiness,
        spawner=hub.spawner,
        pid_probe=hub.pids,
        clock=hub.clock,
    )
    record = uncoupled.start_run(repo, TASK_ID)

    assert hub.runs.live_run_for(repo) == record.run_id  # the coupled hub sees it
    return hub, repo, transient.intake_id, record.run_id


def test_recovered_overlap_refuses_revise(tmp_path):
    hub, repo, intake_id, run_id = overlapped(tmp_path)

    with pytest.raises(RunLiveError) as exc:
        hub.intakes.revise_intake(intake_id, "tighten the scope")

    assert exc.value.run_id == run_id
    assert hub.agent.revise_calls == 0
    # A refusal is a guard, not a state change: the intake stays reviewable.
    assert hub.intakes.get_intake(intake_id).status == STATUS_AWAITING_REVIEW


def test_recovered_overlap_refuses_confirm_and_writes_nothing(tmp_path):
    hub, repo, intake_id, run_id = overlapped(tmp_path)
    before = snapshot(repo)

    with pytest.raises(RunLiveError) as exc:
        hub.intakes.confirm_intake(intake_id)

    assert exc.value.run_id == run_id
    assert hub.agent.confirm_calls == 0
    assert snapshot(repo) == before  # no .ai-tasks/ write raced the run
    assert hub.intakes.get_intake(intake_id).status == STATUS_AWAITING_REVIEW


def test_recovered_overlap_still_allows_cancel(tmp_path):
    """Cancel is the escape hatch that clears the overlap."""
    hub, repo, intake_id, _ = overlapped(tmp_path)
    before = snapshot(repo)

    cancelled = hub.intakes.cancel_intake(intake_id)

    assert cancelled.status == STATUS_CANCELLED
    assert snapshot(repo) == before
    assert hub.intakes.active_intake_for(repo) is None  # repo slot freed


def test_overlap_resolves_once_the_run_ends(tmp_path):
    hub, repo, intake_id, _ = overlapped(tmp_path)
    hub.end_run()

    settled = hub.settle(hub.intakes.confirm_intake(intake_id))

    assert settled.status == STATUS_CONFIRMED
    assert settled.created_task_id == DRAFT_TASK_ID


# -- concurrency: exactly one of two simultaneous starts is admitted -----------


class Parked:
    """A readiness gate that parks its caller inside the admission section.

    ``entered`` fires once the caller is holding the section; the caller stays
    there until ``release`` is set. This is what makes the race deterministic:
    the competing thread provably reaches the section while it is held.
    """

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, repo, config) -> ReadinessReport:
        self.entered.set()
        assert self.release.wait(5), "parked gate was never released"
        return passing_readiness(repo, config)


def _capture(target):
    """Run ``target`` on a thread, recording its result or admission conflict."""
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["result"] = target()
        except AdmissionConflictError as exc:
            box["conflict"] = exc

    return threading.Thread(target=run, daemon=True), box


def test_concurrent_intake_start_loses_to_an_admitted_run(tmp_path):
    repo = make_repo(tmp_path)
    parked = Parked()
    hub = make_hub(tmp_path, run_readiness=parked)

    run_thread, run_box = _capture(lambda: hub.runs.start_run(repo, TASK_ID))
    run_thread.start()
    assert parked.entered.wait(5)  # the run holds the repo's admission section

    intake_thread, intake_box = _capture(
        lambda: hub.intakes.start_intake(repo, REQUEST)
    )
    intake_thread.start()
    intake_thread.join(BLOCKED_JOIN)
    assert intake_thread.is_alive()  # blocked on the section, not yet refused

    parked.release.set()
    run_thread.join(5)
    intake_thread.join(5)

    assert "result" in run_box  # exactly one admitted
    assert "conflict" in intake_box
    conflict = intake_box["conflict"]
    assert isinstance(conflict, RunLiveError)
    assert conflict.run_id == run_box["result"].run_id
    assert hub.intakes.list_intakes() == []
    assert hub.agent.draft_calls == 0


def test_concurrent_run_start_loses_to_an_admitted_intake(tmp_path):
    repo = make_repo(tmp_path)
    parked = Parked()
    hub = make_hub(tmp_path, intake_readiness=parked)

    intake_thread, intake_box = _capture(
        lambda: hub.intakes.start_intake(repo, REQUEST)
    )
    intake_thread.start()
    assert parked.entered.wait(5)  # the intake holds the section

    run_thread, run_box = _capture(lambda: hub.runs.start_run(repo, TASK_ID))
    run_thread.start()
    run_thread.join(BLOCKED_JOIN)
    assert run_thread.is_alive()

    parked.release.set()
    intake_thread.join(5)
    run_thread.join(5)

    assert "result" in intake_box
    assert "conflict" in run_box
    conflict = run_box["conflict"]
    assert isinstance(conflict, IntakeInFlightError)
    assert conflict.intake_id == intake_box["result"].intake_id
    assert hub.spawner.calls == []
    assert hub.runs.list_runs() == []


def test_concurrent_starts_for_different_repos_both_succeed(tmp_path):
    """The section is per repo: an unrelated repo is never serialized behind it."""
    repo_a = make_repo(tmp_path, "repo-a")
    repo_b = make_repo(tmp_path, "repo-b")
    parked = Parked()
    hub = make_hub(tmp_path, run_readiness=parked)

    run_thread, run_box = _capture(lambda: hub.runs.start_run(repo_a, TASK_ID))
    run_thread.start()
    assert parked.entered.wait(5)

    transient = hub.intakes.start_intake(repo_b, REQUEST)  # not blocked

    parked.release.set()
    run_thread.join(5)
    assert "result" in run_box
    assert hub.settle(transient).status == STATUS_AWAITING_REVIEW
