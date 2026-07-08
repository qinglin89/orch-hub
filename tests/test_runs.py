"""Tests for the run manager (launch, list, stop, reattach).

Everything runs against fake repos in ``tmp_path`` with an injected spawner,
readiness checker, pid probe, and clock — no real subprocess is ever started
and no orchestrator code is imported. Task gating uses the real task-store
parser over fake ``.ai-tasks`` files so both sides stay in lockstep.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.models import ManagedRepo, ReadinessCheck, ReadinessReport
from orch_hub.runs import (
    CAFFEINATE,
    BlockedTaskError,
    LaunchBlockedError,
    RepoBusyError,
    RunEndedError,
    RunManager,
    UnknownRunError,
)

FIXED_NOW = dt.datetime(2026, 7, 8, 3, 0, 0, tzinfo=dt.timezone.utc)
TASK_ID = "2026-07-05-feature-x"


class FakeClock:
    def __init__(self, now: dt.datetime = FIXED_NOW):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def tick(self, seconds: int = 60) -> None:
        self.now += dt.timedelta(seconds=seconds)


class FakeProc:
    """The ``ProcessHandle`` slice: tests end a run by setting ``returncode``."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class FakeSpawner:
    def __init__(self):
        self.calls: list[dict] = []
        self.procs: list[FakeProc] = []
        self._pids = itertools.count(4000)

    def __call__(self, argv, *, cwd, stdout, start_new_session):
        self.calls.append(
            {
                "argv": tuple(argv),
                "cwd": Path(cwd),
                "stdout_path": Path(stdout.name),
                "start_new_session": start_new_session,
            }
        )
        proc = FakeProc(next(self._pids))
        self.procs.append(proc)
        return proc


def passing_readiness(repo, config) -> ReadinessReport:
    return ReadinessReport((ReadinessCheck("all", True, "ok"),))


def failing_readiness(repo, config) -> ReadinessReport:
    return ReadinessReport(
        (
            ReadinessCheck("git_clean", False, "working tree has uncommitted changes"),
            ReadinessCheck("aii_status", False, "aii-2 status failed: drift"),
        )
    )


def write_index(repo_path: Path, task_ids: list[str]) -> None:
    rows = "\n".join(f"| {task_id} | x | Summary of {task_id} |" for task_id in task_ids)
    (repo_path / ".ai-tasks" / "index.md").write_text(
        "# Active tasks\n\n| Task | Status | Summary |\n|---|---|---|\n" + rows + "\n",
        encoding="utf-8",
    )


def write_task_file(
    repo_path: Path,
    task_id: str,
    status: str | None = "in_progress",
    blockers: str | None = None,
) -> None:
    lines = ["---", f"id: {task_id}"]
    if status is not None:
        lines.append(f"status: {status}")
    if blockers is not None:
        lines.append(f"blockers: {blockers}")
    lines += ["---", "", "# Task", ""]
    (repo_path / ".ai-tasks" / f"{task_id}.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def make_repo(
    root: Path, name: str = "repo-a", *, task_status: str | None = "in_progress"
) -> ManagedRepo:
    """Fake managed repo with one active task (``TASK_ID``) by default."""
    path = root / "repos" / name
    (path / ".ai-tasks").mkdir(parents=True)
    write_index(path, [TASK_ID])
    if task_status is not None:
        write_task_file(path, TASK_ID, status=task_status)
    return ManagedRepo(
        name=name, path=path, manifest=path / ".ai-deploy-manifest.json"
    )


def make_manager(
    root: Path,
    *,
    spawner=None,
    readiness=passing_readiness,
    pid_probe=None,
    clock: FakeClock | None = None,
) -> tuple[RunManager, FakeSpawner, FakeClock]:
    spawner = spawner if spawner is not None else FakeSpawner()
    clock = clock if clock is not None else FakeClock()
    config = Config(ai_native_path=root / "ai-native", data_dir=root / "hub-data")
    manager = RunManager(
        config,
        readiness_checker=readiness,
        spawner=spawner,
        pid_probe=pid_probe if pid_probe is not None else lambda pid: False,
        clock=clock,
    )
    return manager, spawner, clock


def read_record(manager: RunManager, run_id: str) -> dict:
    return json.loads(
        (manager.runs_dir / run_id / "run.json").read_text(encoding="utf-8")
    )


def only_status(manager: RunManager):
    statuses = manager.list_runs()
    assert len(statuses) == 1
    return statuses[0]


# -- command construction ------------------------------------------------------


def test_start_run_builds_the_documented_command(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)

    record = manager.start_run(repo, TASK_ID)

    orch = repo.path / ".cursor" / "orchestrator"
    call = spawner.calls[0]
    assert call["argv"] == (
        *CAFFEINATE,
        str(orch / ".venv" / "bin" / "python"),
        str(orch / "orchestrator.py"),
        TASK_ID,
        "--control-dir",
        str(record.control_dir),
    )
    assert call["cwd"] == repo.path
    assert call["start_new_session"] is True
    assert call["stdout_path"] == manager.runs_dir / record.run_id / "spawn.out"
    assert record.argv == call["argv"]


def test_extra_args_are_appended_to_the_orchestrator_argv(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)

    manager.start_run(
        repo, TASK_ID, extra_args=("--plan-gate", "--backend", "cc-codex")
    )

    assert spawner.calls[0]["argv"][-3:] == ("--plan-gate", "--backend", "cc-codex")
    assert "--control-dir" in spawner.calls[0]["argv"]


def test_control_dir_is_fresh_and_outside_the_repo_tree(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)

    record = manager.start_run(repo, TASK_ID)

    assert record.control_dir.is_dir()
    assert list(record.control_dir.iterdir()) == []  # fresh: no stale stop.flag
    assert not record.control_dir.is_relative_to(repo.path)
    assert record.control_dir.is_relative_to(manager.runs_dir)


# -- state recording -----------------------------------------------------------


def test_run_record_is_persisted_to_run_json(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, clock = make_manager(tmp_path)

    record = manager.start_run(repo, TASK_ID)

    data = read_record(manager, record.run_id)
    run_dir = manager.runs_dir / record.run_id
    assert data == {
        "run_id": record.run_id,
        "repo_name": "repo-a",
        "repo_path": str(repo.path),
        "task_id": TASK_ID,
        "control_dir": str(run_dir / "control"),
        "log_dir": str(repo.path / ".cursor" / "orchestrator" / "logs"),
        "argv": list(spawner.calls[0]["argv"]),
        "pid": spawner.procs[0].pid,
        "started_ts": clock.now.isoformat(),
        "ended_ts": None,
        "exit_code": None,
    }
    assert not list(run_dir.glob("*.tmp"))


def test_run_id_carries_timestamp_repo_and_task(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)

    record = manager.start_run(repo, TASK_ID)

    assert record.run_id == f"20260708T030000Z-repo-a-{TASK_ID}"


def test_same_second_relaunch_gets_a_collision_suffix(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)

    first = manager.start_run(repo, TASK_ID)
    spawner.procs[0].returncode = 1  # first run ends; repo frees
    manager.list_runs()

    second = manager.start_run(repo, TASK_ID)  # clock did not advance

    assert second.run_id == f"{first.run_id}-2"


# -- per-repo serialization ------------------------------------------------------


def test_second_run_in_same_repo_is_refused_while_alive(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    first = manager.start_run(repo, TASK_ID)

    with pytest.raises(RepoBusyError) as excinfo:
        manager.start_run(repo, TASK_ID)

    assert excinfo.value.run_id == first.run_id
    assert len(spawner.calls) == 1


def test_cross_repo_runs_may_overlap(tmp_path):
    repo_a = make_repo(tmp_path, "repo-a")
    repo_b = make_repo(tmp_path, "repo-b")
    manager, spawner, _ = make_manager(tmp_path)

    manager.start_run(repo_a, TASK_ID)
    manager.start_run(repo_b, TASK_ID)

    assert len(spawner.calls) == 2
    assert len(manager.list_runs()) == 2


def test_repo_frees_after_its_run_ends(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, clock = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)

    spawner.procs[0].returncode = 0
    clock.tick()

    second = manager.start_run(repo, TASK_ID)  # busy check observes the end
    assert second.run_id.startswith("20260708T030100Z-")


def test_busy_check_covers_runs_started_by_a_previous_hub_process(tmp_path):
    repo = make_repo(tmp_path)
    manager_a, spawner, _ = make_manager(tmp_path)
    manager_a.start_run(repo, TASK_ID)
    live_pid = spawner.procs[0].pid

    # Fresh manager, no in-memory handles: only run.json + pid probe remain.
    manager_b, _, _ = make_manager(tmp_path, pid_probe=lambda pid: pid == live_pid)

    with pytest.raises(RepoBusyError):
        manager_b.start_run(repo, TASK_ID)


# -- launch gating ---------------------------------------------------------------


def test_launch_refused_when_readiness_fails(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path, readiness=failing_readiness)

    with pytest.raises(LaunchBlockedError) as excinfo:
        manager.start_run(repo, TASK_ID)

    assert excinfo.value.reasons == (
        "git_clean: working tree has uncommitted changes",
        "aii_status: aii-2 status failed: drift",
    )
    assert spawner.calls == []
    assert not manager.runs_dir.exists()  # gate fires before any dir is created


def test_launch_refused_without_task_index(tmp_path):
    repo = make_repo(tmp_path)
    (repo.path / ".ai-tasks" / "index.md").unlink()
    manager, spawner, _ = make_manager(tmp_path)

    with pytest.raises(LaunchBlockedError, match="index.md"):
        manager.start_run(repo, TASK_ID)
    assert spawner.calls == []


def test_launch_refused_for_unlisted_task(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)

    with pytest.raises(LaunchBlockedError, match="not listed"):
        manager.start_run(repo, "2026-01-01-no-such-task")
    assert spawner.calls == []


def test_launch_refused_for_missing_task_file_sentinel(tmp_path):
    # Listed in the index but the task file itself is gone: sentinel `missing`.
    repo = make_repo(tmp_path, task_status=None)
    manager, spawner, _ = make_manager(tmp_path)

    with pytest.raises(LaunchBlockedError, match="missing"):
        manager.start_run(repo, TASK_ID)
    assert spawner.calls == []


def test_launch_refused_for_unknown_status_sentinel(tmp_path):
    # Task file exists but carries no parseable status: sentinel `unknown`.
    repo = make_repo(tmp_path, task_status=None)
    write_task_file(repo.path, TASK_ID, status=None)
    manager, spawner, _ = make_manager(tmp_path)

    with pytest.raises(LaunchBlockedError, match="no parseable status"):
        manager.start_run(repo, TASK_ID)
    assert spawner.calls == []


def test_launch_refused_for_unrecognized_status(tmp_path):
    repo = make_repo(tmp_path, task_status="wip")
    manager, spawner, _ = make_manager(tmp_path)

    with pytest.raises(LaunchBlockedError, match="unrecognized"):
        manager.start_run(repo, TASK_ID)
    assert spawner.calls == []


def test_blocked_task_requires_explicit_acknowledgement(tmp_path):
    repo = make_repo(tmp_path, task_status=None)
    write_task_file(
        repo.path, TASK_ID, status="blocked", blockers="[external:need ruling]"
    )
    manager, spawner, _ = make_manager(tmp_path)

    with pytest.raises(BlockedTaskError) as excinfo:
        manager.start_run(repo, TASK_ID)

    # The message must carry the §5.4 caveat context for the UI to show.
    assert "external:need ruling" in str(excinfo.value)
    assert "unblock manually" in str(excinfo.value)
    assert spawner.calls == []

    record = manager.start_run(repo, TASK_ID, acknowledge_blocked=True)
    assert record.task_id == TASK_ID
    assert len(spawner.calls) == 1


@pytest.mark.parametrize(
    "status", ["pending", "in_progress", "final_review", "completed"]
)
def test_launchable_statuses_start_without_caveats(tmp_path, status):
    repo = make_repo(tmp_path, task_status=status)
    manager, spawner, _ = make_manager(tmp_path)

    manager.start_run(repo, TASK_ID)

    assert len(spawner.calls) == 1


def test_spawn_failure_cleans_up_the_fresh_run_dir(tmp_path):
    repo = make_repo(tmp_path)

    def exploding_spawner(argv, *, cwd, stdout, start_new_session):
        raise FileNotFoundError("caffeinate")

    manager, _, _ = make_manager(tmp_path, spawner=exploding_spawner)

    with pytest.raises(FileNotFoundError):
        manager.start_run(repo, TASK_ID)

    assert list(manager.runs_dir.iterdir()) == []
    assert manager.list_runs() == []


# -- stop ------------------------------------------------------------------------


def test_stop_run_writes_the_stop_flag_once(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    assert manager.stop_run(record.run_id) is True
    assert (record.control_dir / "stop.flag").is_file()
    assert manager.stop_run(record.run_id) is False  # idempotent

    # A stop is a request, not a kill: the run stays alive until the
    # orchestrator honors the flag.
    assert only_status(manager).state == "active"


def test_stop_run_rejects_unknown_and_ended_runs(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    with pytest.raises(UnknownRunError):
        manager.stop_run("20990101T000000Z-nope-x")
    with pytest.raises(UnknownRunError):
        manager.stop_run("../escape")

    spawner.procs[0].returncode = 3
    with pytest.raises(RunEndedError):
        manager.stop_run(record.run_id)
    # The post-mortem flag was refused, so the run classifies as an error,
    # not as stopped.
    assert only_status(manager).state == "error"


# -- listing, live states, reattach ----------------------------------------------


def test_live_run_toggles_between_active_and_waiting(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    assert only_status(manager).state == "active"

    question = record.control_dir / "001-question.json"
    question.write_text(
        json.dumps({"seq": 1, "kind": "blocked", "banner": "ruling?"}),
        encoding="utf-8",
    )
    status = only_status(manager)
    assert status.state == "waiting"
    assert status.pending == 1

    (record.control_dir / "001-answer.json").write_text(
        json.dumps({"answer": "proceed"}), encoding="utf-8"
    )
    assert only_status(manager).state == "active"


def test_fresh_manager_reattaches_runs_from_disk(tmp_path):
    repo = make_repo(tmp_path)
    manager_a, spawner, _ = make_manager(tmp_path)
    record = manager_a.start_run(repo, TASK_ID)
    live_pid = spawner.procs[0].pid

    manager_b, _, _ = make_manager(tmp_path, pid_probe=lambda pid: pid == live_pid)
    status = only_status(manager_b)

    assert status.state == "active"
    assert status.record.run_id == record.run_id
    assert status.record.pid == live_pid
    assert status.record.control_dir == record.control_dir


def test_get_run_returns_single_status(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    assert manager.get_run(record.run_id).record.run_id == record.run_id
    with pytest.raises(UnknownRunError):
        manager.get_run("20990101T000000Z-nope-x")
    with pytest.raises(UnknownRunError):
        manager.get_run("../../etc")


# -- end-of-run classification ----------------------------------------------------


def end_run(spawner: FakeSpawner, exit_code: int) -> None:
    spawner.procs[0].returncode = exit_code


def test_exit_zero_with_completed_task_classifies_completed(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)

    write_task_file(repo.path, TASK_ID, status="completed")
    end_run(spawner, 0)

    status = only_status(manager)
    assert status.state == "completed"
    assert "close-out" in status.detail


def test_exit_zero_with_archived_task_classifies_completed(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)

    write_index(repo.path, [])  # close-out removed the row
    (repo.path / ".ai-tasks" / f"{TASK_ID}.md").unlink()
    end_run(spawner, 0)

    status = only_status(manager)
    assert status.state == "completed"
    assert "archived" in status.detail


def test_stop_flag_plus_exit_zero_is_a_graceful_stop(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    manager.stop_run(record.run_id)
    end_run(spawner, 0)

    status = only_status(manager)
    assert status.state == "stopped"
    assert "session boundary" in status.detail


def test_stop_flag_plus_nonzero_exit_is_an_aborted_wait(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    manager.stop_run(record.run_id)
    end_run(spawner, 1)

    status = only_status(manager)
    assert status.state == "stopped"
    assert "question was pending" in status.detail


def test_stop_answer_plus_nonzero_exit_is_a_human_stop(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    record.control_dir.joinpath("001-question.json").write_text(
        json.dumps({"seq": 1, "banner": "?"}), encoding="utf-8"
    )
    record.control_dir.joinpath("001-answer.json").write_text(
        json.dumps({"answer": "stop"}), encoding="utf-8"
    )
    end_run(spawner, 1)

    status = only_status(manager)
    assert status.state == "stopped"
    assert "'stop' answer" in status.detail


def test_nonzero_exit_without_stop_evidence_is_an_error(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)

    end_run(spawner, 3)

    status = only_status(manager)
    assert status.state == "error"
    assert status.detail == "exited 3"


def test_exit_zero_without_evidence_is_plain_ended(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)

    end_run(spawner, 0)

    status = only_status(manager)
    assert status.state == "ended"
    assert "--once" in status.detail


def test_end_observed_after_hub_restart_has_unknown_exit(tmp_path):
    repo = make_repo(tmp_path)
    manager_a, _, _ = make_manager(tmp_path)
    record = manager_a.start_run(repo, TASK_ID)

    manager_b, _, _ = make_manager(tmp_path, pid_probe=lambda pid: False)
    status = only_status(manager_b)

    assert status.state == "ended"
    assert "exit status unknown" in status.detail
    data = read_record(manager_b, record.run_id)
    assert data["ended_ts"] is not None
    assert data["exit_code"] is None


def test_stop_requested_end_after_hub_restart_classifies_stopped(tmp_path):
    repo = make_repo(tmp_path)
    manager_a, _, _ = make_manager(tmp_path)
    record = manager_a.start_run(repo, TASK_ID)
    manager_a.stop_run(record.run_id)

    manager_b, _, _ = make_manager(tmp_path, pid_probe=lambda pid: False)
    status = only_status(manager_b)

    assert status.state == "stopped"
    assert "exit status unknown" in status.detail


def test_observed_exit_code_is_persisted_for_later_managers(tmp_path):
    repo = make_repo(tmp_path)
    manager_a, spawner, _ = make_manager(tmp_path)
    record = manager_a.start_run(repo, TASK_ID)

    end_run(spawner, 5)
    manager_a.list_runs()  # observes and persists the exit

    assert read_record(manager_a, record.run_id)["exit_code"] == 5

    # A later manager classifies purely from the persisted record.
    manager_b, _, _ = make_manager(tmp_path, pid_probe=lambda pid: True)
    status = only_status(manager_b)
    assert status.state == "error"
    assert status.detail == "exited 5"


# -- broken run dirs ---------------------------------------------------------------


def test_unreadable_run_json_is_surfaced_not_hidden(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    broken = manager.runs_dir / "20260708T000000Z-repo-a-old"
    broken.mkdir(parents=True)
    (broken / "run.json").write_text("{ not json", encoding="utf-8")

    status = only_status(manager)
    assert status.state == "error"
    assert "unreadable run.json" in status.detail

    # A stub has no repo attribution, so it must not block new launches.
    manager.start_run(repo, TASK_ID)


def test_run_dir_without_record_is_surfaced_as_crashed_launch(tmp_path):
    manager, _, _ = make_manager(tmp_path)
    (manager.runs_dir / "20260708T000000Z-repo-a-old").mkdir(parents=True)

    status = only_status(manager)

    assert status.state == "error"
    assert "missing run.json" in status.detail
    with pytest.raises(RunEndedError):
        manager.stop_run("20260708T000000Z-repo-a-old")
