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
import os
import shutil
from pathlib import Path

import pytest

from orch_hub import runs as runs_module
from orch_hub.config import Config
from orch_hub.history import (
    KIND_RUN_ENDED,
    KIND_RUN_STARTED,
    KIND_STOP_REQUESTED,
    read_events,
)
from orch_hub.models import (
    HISTORY_SUMMARY_MAX,
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
    RunRecord,
    SessionEst,
)
from orch_hub.orch_config import read_effective_config_snapshot
from orch_hub.sessions import read_snapshot
from orch_hub.task_snapshots import read_snapshots as read_task_snapshots
from orch_hub.runs import (
    CAFFEINATE,
    BlockedTaskError,
    LaunchBlockedError,
    RepoBusyError,
    RunEndedError,
    RunManager,
    UnknownRunError,
    load_records,
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

    def __call__(self, argv, *, cwd, stdout, env, start_new_session):
        self.calls.append(
            {
                "argv": tuple(argv),
                "cwd": Path(cwd),
                "stdout_path": Path(stdout.name),
                "env": dict(env),
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
    notifier=None,
    usage_refresh=None,
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
        notifier=notifier,
        # Deliver inline (not on the production daemon thread) so notification
        # assertions are deterministic; it is only invoked when a notifier is set.
        notify_dispatch=lambda deliver: deliver(),
        usage_refresh=usage_refresh,
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


def test_start_run_passes_agent_cli_paths_to_orchestrator_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/existing/bin:/usr/bin")
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)

    manager.start_run(repo, TASK_ID)

    path_parts = spawner.calls[0]["env"]["PATH"].split(os.pathsep)
    assert path_parts[:4] == [
        str(home / ".local" / "bin"),
        str(home / ".claude" / "local"),
        str(home / ".npm-global" / "bin"),
        str(home / ".cargo" / "bin"),
    ]
    assert "/existing/bin" in path_parts
    assert "/opt/homebrew/bin" in path_parts
    assert path_parts.count("/usr/bin") == 1


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
        "task_status": "in_progress",
        "control_dir": str(run_dir / "control"),
        "log_dir": str(repo.path / ".cursor" / "orchestrator" / "logs"),
        "spawn_out": str(run_dir / "spawn.out"),
        "argv": list(spawner.calls[0]["argv"]),
        "pid": spawner.procs[0].pid,
        "started_ts": clock.now.isoformat(),
        "ended_ts": None,
        "exit_code": None,
    }
    assert not list(run_dir.glob("*.tmp"))


def test_run_record_carries_launch_time_task_status(tmp_path):
    repo = make_repo(tmp_path, task_status="final_review")
    manager, _, _ = make_manager(tmp_path)

    record = manager.start_run(repo, TASK_ID)

    assert record.task_status == "final_review"
    assert read_record(manager, record.run_id)["task_status"] == "final_review"


def test_run_record_spawn_out_path_is_set_and_reattaches(tmp_path):
    # spawn.out is the subprocess stdout+stderr capture; the logs route falls
    # back to it for a run that fails before writing an orchestrator log. Its
    # path is on the record and survives reattach from disk.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    run_dir = manager.runs_dir / record.run_id
    assert record.spawn_out == run_dir / "spawn.out"
    assert read_record(manager, record.run_id)["spawn_out"] == str(run_dir / "spawn.out")

    # A fresh manager loading only run.json resolves the same capture path.
    manager_b, _, _ = make_manager(tmp_path, pid_probe=lambda pid: True)
    assert manager_b.get_run(record.run_id).record.spawn_out == run_dir / "spawn.out"


def test_load_record_without_spawn_out_field_falls_back_to_fixed_path(tmp_path):
    # A run.json persisted before the spawn_out field still resolves the capture
    # from the fixed run-dir layout (mirrors the control_dir fallback).
    manager, _, _ = make_manager(tmp_path)
    run_dir = manager.runs_dir / "20260708T030000Z-repo-a-old"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": run_dir.name, "repo_path": "/repos/repo-a"}),
        encoding="utf-8",
    )

    record = manager.get_run(run_dir.name).record
    assert record.spawn_out == run_dir / "spawn.out"


def test_stub_record_for_broken_dir_still_points_at_spawn_out(tmp_path):
    # A crashed launch (unreadable run.json) may still have a spawn.out; the stub
    # keeps the fixed path so the logs route can surface that crash diagnostic.
    manager, _, _ = make_manager(tmp_path)
    broken = manager.runs_dir / "20260708T000000Z-repo-a-old"
    broken.mkdir(parents=True)
    (broken / "run.json").write_text("{ not json", encoding="utf-8")

    record = manager.get_run(broken.name).record
    assert record.error is not None
    assert record.spawn_out == broken / "spawn.out"


def test_run_started_event_is_persisted(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)

    record = manager.start_run(repo, TASK_ID)

    events = read_events(manager.runs_dir / record.run_id)
    assert len(events) == 1
    assert events[0].kind == KIND_RUN_STARTED
    assert events[0].run_id == record.run_id
    assert events[0].repo_name == "repo-a"
    assert events[0].task_id == TASK_ID
    assert events[0].task_status == "in_progress"


def test_started_ts_is_stamped_before_the_spawn(tmp_path):
    """started_ts must be a lower bound on the child's own log stamp.

    The orchestrator picks its second-precision log filename during its startup
    — after the spawn — so the run manager must read started_ts *before*
    spawning. Otherwise a launch that crosses a one-second boundary records
    started_ts a second ahead of the child's stamp, and ``logs.locate_log``
    (lower bound ``floor(started_ts)``) hides the run's own log. A strictly
    advancing clock crosses a second between the pre-spawn capture and the
    spawner call: with the capture after the spawn, ``started_ts`` (03:00:01)
    would floor past the child stamp (03:00:00); with it before, it stays at or
    below the child's stamp.
    """
    repo = make_repo(tmp_path)

    class AdvancingClock:
        """Returns a strictly later time on every read (0.3s step)."""

        def __init__(self, start: dt.datetime):
            self.now = start

        def __call__(self) -> dt.datetime:
            current = self.now
            self.now = current + dt.timedelta(milliseconds=300)
            return current

    # 03:00:00.5 → create-run-dir 03:00:00.5, started_ts 03:00:00.8,
    # spawner-observed 03:00:01.1: the spawn crosses into the next second.
    clock = AdvancingClock(dt.datetime(2026, 7, 8, 3, 0, 0, 500_000, tzinfo=dt.timezone.utc))
    spawn_observed: list[dt.datetime] = []

    def recording_spawner(argv, *, cwd, stdout, env, start_new_session):
        # The child's log stamp is taken no earlier than this spawn call.
        spawn_observed.append(clock())
        return FakeProc(4000)

    manager, _, _ = make_manager(tmp_path, spawner=recording_spawner, clock=clock)
    record = manager.start_run(repo, TASK_ID)

    started = dt.datetime.fromisoformat(record.started_ts)
    # Core invariant: started_ts is read before the spawner runs.
    assert started <= spawn_observed[0]
    # So the floored lower bound never exceeds the child's second-precision stamp.
    child_stamp = spawn_observed[0].replace(microsecond=0)
    assert started.replace(microsecond=0) <= child_stamp


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

    def exploding_spawner(argv, *, cwd, stdout, env, start_new_session):
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
    events = read_events(manager.runs_dir / record.run_id)
    assert [event.kind for event in events] == [KIND_RUN_STARTED, KIND_STOP_REQUESTED]

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


def test_live_run_exposes_stop_requested_across_reads(tmp_path):
    # A requested stop is observable server state, not transient button text: it
    # is derived on every status read, so it survives a reload. A fresh live run
    # has none; after stop_run every render carries stop_requested=True while the
    # run stays live (a stop is a request, not a kill).
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    status = only_status(manager)
    assert status.state == "active"
    assert status.stop_requested is False
    assert status.to_dict()["stop_requested"] is False

    manager.stop_run(record.run_id)
    status = only_status(manager)
    assert status.state == "active"
    assert status.stop_requested is True
    assert status.to_dict()["stop_requested"] is True


def test_terminal_run_leaves_stop_requested_false(tmp_path):
    # A finished run carries its stop in the classified state/detail
    # (STATE_STOPPED), not in the running-run stop_requested overlay, which stays
    # False for terminal records — no new field semantics past the live window.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    manager.stop_run(record.run_id)
    spawner.procs[0].returncode = 0  # honored at a session boundary

    status = only_status(manager)
    assert status.state == "stopped"
    assert status.stop_requested is False
    assert status.to_dict()["stop_requested"] is False


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


def test_recent_runs_returns_newest_first_with_events(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, clock = make_manager(tmp_path)
    first = manager.start_run(repo, TASK_ID)
    spawner.procs[0].returncode = 0
    manager.list_runs()  # observe first end
    clock.tick()
    second = manager.start_run(repo, TASK_ID)

    entries = manager.recent_runs(limit=2)

    assert [entry.status.record.run_id for entry in entries] == [
        second.run_id,
        first.run_id,
    ]
    assert [event.kind for event in entries[0].events] == [KIND_RUN_STARTED]
    assert [event.kind for event in entries[1].events] == [
        KIND_RUN_STARTED,
        KIND_RUN_ENDED,
    ]
    assert manager.recent_runs(limit=1)[0].status.record.run_id == second.run_id


# -- run-history paging ------------------------------------------------------------
# (2026-07-30-run-history-pagination-collapse)
#
# Every persisted run must be reachable by walking the same stable newest-first
# order, so the checks below are about the *window*: no duplicate, no omission,
# fixed size, honest totals at both boundaries, and a bounded read cost per page.
# History mutates while it is being walked (a run starts, an old run dir is
# pruned), so several of these assert what a mutation must NOT do to a window.


def seed_ended_runs(manager, repo, count: int, *, first: int = 0) -> list[str]:
    """Persist ``count`` ended run records directly; return ids oldest-first.

    Paging is a property of persisted run dirs, so seeding records is both the
    honest fixture and what makes exercising the real fifty-run page cheap (a
    start/end arc per run is O(n^2) file work and, under a fixed test clock,
    produces collision-suffixed ids whose order is not chronological). The
    production writer is reached through the module so the record format stays
    single-sourced; each id carries its own timestamp, like a real one.
    """
    ids = []
    for index in range(first, first + count):
        stamp = (FIXED_NOW + dt.timedelta(minutes=index)).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{repo.name}-{TASK_ID}"
        run_dir = manager.runs_dir / run_id
        (run_dir / "control").mkdir(parents=True)
        runs_module._write_record(
            run_dir,
            RunRecord(
                run_id=run_id,
                repo_name=repo.name,
                repo_path=repo.path,
                task_id=TASK_ID,
                control_dir=run_dir / "control",
                started_ts=stamp,
                ended_ts=stamp,
                exit_code=0,
            ),
        )
        ids.append(run_id)
    return ids


def page_ids(page) -> list[str]:
    return [entry.status.record.run_id for entry in page.entries]


def test_history_page_holds_fifty_runs_and_anchors_the_rest(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    newest_first = list(reversed(seed_ended_runs(manager, repo, 51)))

    first = manager.history_page()
    second = manager.history_page(cursor=first.next_cursor)
    back = manager.history_page(cursor=second.prev_cursor)

    # The page size is the contract's, not the caller's, and the two windows
    # concatenate back into the exact newest-first order — every run reachable
    # exactly once.
    assert page_ids(first) == newest_first[:50]
    assert page_ids(second) == newest_first[50:]
    assert first.page_size == 50
    # Totals describe the whole history, not the window, on every page.
    assert [(page.total_runs, page.total_pages) for page in (first, second)] == [
        (51, 2),
        (51, 2),
    ]
    assert (first.page, second.page) == (1, 2)
    # Boundaries are the absence of an adjacent window, and stepping back lands
    # on exactly the page stepped away from.
    assert (first.has_prev, first.has_next) == (False, True)
    assert (second.has_prev, second.has_next) == (True, False)
    assert page_ids(back) == page_ids(first)


def test_history_page_window_is_unshifted_by_a_run_persisted_mid_walk(tmp_path):
    # Why the anchor is a run id: an offset into a freshly read list slides down
    # by one for every newer run, so the next page would repeat the previous
    # page's last run — and enough insertions would keep the oldest runs out of
    # reach entirely.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    oldest_first = seed_ended_runs(manager, repo, 51)
    first = manager.history_page()

    seed_ended_runs(manager, repo, 2, first=500)  # newer runs, above the window

    second = manager.history_page(cursor=first.next_cursor)
    assert page_ids(second) == [oldest_first[0]]  # the one run below page 1, once
    assert not set(page_ids(second)) & set(page_ids(first))
    assert second.total_runs == 53  # the totals move; the window does not


def test_history_page_window_survives_runs_pruned_mid_walk(tmp_path):
    # The mirror case: a positional offset pulls the next page up when a run
    # above it is pruned, skipping that page's own first run. An anchored window
    # cannot see a deletion above it — including the deletion of the anchor
    # itself, which resolves to the next run below rather than to nothing.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    newest_first = list(reversed(seed_ended_runs(manager, repo, 52)))
    first = manager.history_page()
    anchor = newest_first[50]  # the next window's first run
    assert first.next_cursor == runs_module._cursor(runs_module._OLDER, anchor)

    shutil.rmtree(manager.runs_dir / newest_first[0])  # a run on page 1

    assert page_ids(manager.history_page(cursor=first.next_cursor)) == newest_first[50:]

    shutil.rmtree(manager.runs_dir / anchor)  # the anchor itself

    assert page_ids(manager.history_page(cursor=first.next_cursor)) == newest_first[51:]


def test_history_page_steps_back_without_overlap_after_a_prune(tmp_path):
    # Why Previous is a direction, not a position: derived from the current
    # window's offset, a prune above that window pulls the previous window down
    # over it, so stepping back re-served a run the pager was already showing.
    # A newer-direction cursor is anchored on this window's own first run, so the
    # window above is whatever is strictly newer than it — never itself.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    newest_first = list(reversed(seed_ended_runs(manager, repo, 52)))
    first = manager.history_page()

    shutil.rmtree(manager.runs_dir / newest_first[10])  # a run on page 1
    surviving = [run_id for run_id in newest_first if run_id != newest_first[10]]

    second = manager.history_page(cursor=first.next_cursor)
    back = manager.history_page(cursor=second.prev_cursor)

    assert page_ids(second) == newest_first[50:]
    # Stepping back lands on the surviving page 1 — one run shorter, no run of the
    # window it came from, and the two windows still cover the history exactly.
    assert page_ids(back) == [r for r in newest_first[:50] if r != newest_first[10]]
    assert not set(page_ids(back)) & set(page_ids(second))
    assert page_ids(back) + page_ids(second) == surviving
    # The prune moved the window off the page grid; the indicator must not then
    # claim page 1 while offering a Previous, nor a position past its own count.
    assert (second.page, second.total_pages, second.has_prev) == (2, 2, True)
    assert (back.page, back.total_pages, back.has_prev) == (1, 2, False)


def test_history_page_steps_back_without_overlap_after_an_insert(tmp_path):
    # The insertion mirror: a positional Previous offered an extra step above the
    # window it came from that re-served most of it. Anchored on a real run, each
    # step back is the bounded set of runs strictly newer than the last, so the
    # walk stays a partition — the runs added mid-walk are the ragged top window.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    newest_first = list(reversed(seed_ended_runs(manager, repo, 52)))
    first = manager.history_page()

    added = seed_ended_runs(manager, repo, 1, first=500)  # newer, above page 1
    newest_first = added + newest_first

    second = manager.history_page(cursor=first.next_cursor)
    back = manager.history_page(cursor=second.prev_cursor)
    top = manager.history_page(cursor=back.prev_cursor)

    assert page_ids(second) == newest_first[51:]
    assert page_ids(back) == newest_first[1:51]
    assert page_ids(top) == added  # the run persisted mid-walk, reachable, once
    assert page_ids(top) + page_ids(back) + page_ids(second) == newest_first
    assert (top.page, top.has_prev) == (1, False)
    assert page_ids(manager.history_page(cursor=top.next_cursor)) == page_ids(back)
    # The traversal genuinely has one window more than the snapshot's page grid
    # (the ragged top one), and every window of it agrees on that: `page` counts
    # the windows above — so it never contradicts Previous — and `total_pages`
    # counts the walk's windows rather than the grid's slots, so no window reports
    # a position past its own page count.
    assert [(p.page, p.total_pages) for p in (top, back, second)] == [
        (1, 3),
        (2, 3),
        (3, 3),
    ]
    assert (second.has_prev, second.has_next) == (True, False)


def test_history_page_indicator_counts_the_windows_of_its_own_traversal(tmp_path):
    # Raggedness is not only a mutation story: any cursor between two grid slots
    # (a stale one, a hand-typed one) starts a window that the snapshot's
    # runs-over-page-size arithmetic has no slot for. What the metadata must never
    # do is put the position outside its own count — "page 3 of 2" — so the count
    # is of the windows this traversal holds, and every step of the walk agrees on
    # it however the walk is entered.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    newest_first = list(reversed(seed_ended_runs(manager, repo, 52)))

    last = manager.history_page(cursor=newest_first[51])  # one run, off the grid
    middle = manager.history_page(cursor=last.prev_cursor)
    top = manager.history_page(cursor=middle.prev_cursor)

    assert [(p.page, p.total_pages) for p in (top, middle, last)] == [
        (1, 3),
        (2, 3),
        (3, 3),
    ]
    # And the three windows the count claims are exactly the history, once each.
    assert page_ids(top) + page_ids(middle) + page_ids(last) == newest_first
    assert (top.has_prev, last.has_next) == (False, False)


def test_history_page_reads_only_its_own_windows_runs(tmp_path, monkeypatch):
    # Paging exists to bound the cost of a history read: asking for the second
    # page must not walk the first page's runs (nor any other page's).
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    oldest_first = seed_ended_runs(manager, repo, 51)
    first = manager.history_page()
    read: list[str] = []
    original = runs_module.read_events

    def spy(run_dir):
        read.append(run_dir.name)
        return original(run_dir)

    monkeypatch.setattr(runs_module, "read_events", spy)

    second = manager.history_page(cursor=first.next_cursor)

    assert page_ids(second) == [oldest_first[0]]
    assert read == page_ids(second)


def test_history_page_empty_history_is_a_first_page_with_no_pages(tmp_path):
    manager, _, _ = make_manager(tmp_path)

    page = manager.history_page()

    assert page.entries == ()
    assert (page.page, page.page_size, page.total_runs, page.total_pages) == (
        1,
        50,
        0,
        0,
    )
    assert (page.prev_cursor, page.next_cursor) == (None, None)


def test_history_page_past_the_end_stays_navigable(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    newest_first = list(reversed(seed_ended_runs(manager, repo, 3)))

    page = manager.history_page(cursor="0")  # below every run id

    # Past the end is not an error: an empty window that still reports the real
    # totals and anchors Previous on the last runs that do exist, so a client
    # navigates back rather than stranding where its runs used to be. There is no
    # entry to anchor on, so the requested anchor names that edge itself.
    assert page.entries == ()
    assert (page.page, page.total_runs, page.total_pages) == (2, 3, 1)
    assert page.next_cursor is None
    assert page_ids(manager.history_page(cursor=page.prev_cursor)) == newest_first


def test_history_page_resolves_an_unknown_cursor_by_position(tmp_path):
    # A cursor is a position in the ordering, not a lookup: one above every run
    # (a stale id from a pruned newest run) starts at the newest run instead of
    # erroring or emptying the view. It is only ever *compared*, never used to
    # reach the filesystem, so a path-shaped one is just a string below every id.
    # An unprefixed value keeps meaning what a bare run id meant — the older
    # direction — so a hand-typed cursor stays usable.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    newest_first = list(reversed(seed_ended_runs(manager, repo, 3)))

    above = manager.history_page(cursor="9999-later-than-every-run")
    escaping = manager.history_page(cursor="../../etc")
    bare = manager.history_page(cursor=newest_first[1])

    assert page_ids(above) == newest_first
    assert (above.page, above.has_prev) == (1, False)
    assert (escaping.entries, escaping.total_runs) == ((), 3)
    assert page_ids(bare) == newest_first[1:]
    prefixed = runs_module._cursor(runs_module._OLDER, newest_first[1])
    assert page_ids(bare) == page_ids(manager.history_page(cursor=prefixed))


def test_recent_runs_summary_is_bounded_and_not_a_page(tmp_path):
    # The summary is the other contract on this data: N newest runs for a
    # glanceable strip. It hands back a plain bounded list — no cursor, no
    # totals, nothing to walk — so a caller-chosen size can never become a page
    # size, and it is clamped besides.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    seeded = seed_ended_runs(manager, repo, HISTORY_SUMMARY_MAX + 5)
    newest_first = list(reversed(seeded))

    assert [e.status.record.run_id for e in manager.recent_runs(limit=2)] == (
        newest_first[:2]
    )
    assert manager.recent_runs(limit=0) == []
    assert len(manager.recent_runs(limit=500)) == HISTORY_SUMMARY_MAX


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
    events = read_events(manager_a.runs_dir / record.run_id)
    assert [event.kind for event in events] == [KIND_RUN_STARTED, KIND_RUN_ENDED]
    assert events[-1].exit_code == 5

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


# -- terminal-state notifications ------------------------------------------------


class RecordingNotifier:
    """Captures every delivered notification (synchronous dispatch in tests)."""

    def __init__(self):
        self.notifications: list = []

    def notify(self, notification) -> None:
        self.notifications.append(notification)


class RaisingNotifier:
    """A backend that always fails, to prove failures are isolated."""

    def __init__(self):
        self.calls = 0

    def notify(self, notification) -> None:
        self.calls += 1
        raise OSError("bark unreachable")


def test_run_end_fires_one_notification_with_the_classified_state(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, spawner, _ = make_manager(tmp_path, notifier=notifier)
    manager.start_run(repo, TASK_ID)

    write_task_file(repo.path, TASK_ID, status="completed")
    end_run(spawner, 0)
    manager.list_runs()  # observe the end -> fires

    assert len(notifier.notifications) == 1
    note = notifier.notifications[0]
    assert note.title == "repo-a: run completed"
    assert note.group == "repo-a"
    assert TASK_ID in note.body


def test_error_end_notifies_run_failed(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, spawner, _ = make_manager(tmp_path, notifier=notifier)
    manager.start_run(repo, TASK_ID)

    end_run(spawner, 3)  # nonzero exit, no stop evidence -> error
    manager.list_runs()

    assert [n.title for n in notifier.notifications] == ["repo-a: run failed"]


def test_no_notification_while_the_run_is_live(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)
    manager.start_run(repo, TASK_ID)

    manager.list_runs()  # still active

    assert notifier.notifications == []


def test_notification_fires_exactly_once_across_rescans(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, spawner, _ = make_manager(tmp_path, notifier=notifier)
    record = manager.start_run(repo, TASK_ID)

    end_run(spawner, 0)
    manager.list_runs()  # transition observed here
    manager.list_runs()  # settled record: no re-fire
    manager.get_run(record.run_id)
    manager.recent_runs(limit=10)

    assert len(notifier.notifications) == 1


def test_notifier_failure_does_not_corrupt_run_state(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RaisingNotifier()
    manager, spawner, _ = make_manager(tmp_path, notifier=notifier)
    record = manager.start_run(repo, TASK_ID)

    end_run(spawner, 0)
    status = only_status(manager)  # must not raise despite delivery failing

    assert notifier.calls == 1
    assert status.state == "ended"
    # The end is still recorded and audited exactly as without a notifier.
    data = read_record(manager, record.run_id)
    assert data["ended_ts"] is not None
    assert data["exit_code"] == 0
    events = read_events(manager.runs_dir / record.run_id)
    assert [event.kind for event in events] == [KIND_RUN_STARTED, KIND_RUN_ENDED]


def test_no_notifier_configured_is_a_silent_no_op(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)  # notifier=None
    manager.start_run(repo, TASK_ID)

    end_run(spawner, 0)
    status = only_status(manager)  # no notifier, no error

    assert status.state == "ended"


# -- usage-refresh hook at the terminal transition ---------------------------------
# A finished run has just stopped spending agent subscription quota, so the run
# manager asks the display-only usage sidecar for a fresh reading. It is a plain
# callable (no usage import here) and is isolated exactly like notification
# delivery: once per run, never twice, and never able to touch run state.


def test_run_end_asks_for_a_usage_refresh(tmp_path):
    repo = make_repo(tmp_path)
    asked: list[int] = []
    manager, spawner, _ = make_manager(tmp_path, usage_refresh=lambda: asked.append(1))
    manager.start_run(repo, TASK_ID)

    manager.list_runs()  # still live -> nothing to refresh for yet
    assert asked == []

    end_run(spawner, 0)
    manager.list_runs()  # the live->ended transition

    assert asked == [1]


def test_usage_refresh_is_asked_exactly_once_across_rescans(tmp_path):
    repo = make_repo(tmp_path)
    asked: list[int] = []
    manager, spawner, _ = make_manager(tmp_path, usage_refresh=lambda: asked.append(1))
    record = manager.start_run(repo, TASK_ID)

    end_run(spawner, 0)
    manager.list_runs()  # transition observed here
    manager.list_runs()  # settled record: no re-ask
    manager.get_run(record.run_id)
    manager.recent_runs(limit=10)

    assert asked == [1]


def test_usage_refresh_failure_does_not_corrupt_run_state(tmp_path):
    repo = make_repo(tmp_path)

    def boom() -> None:
        raise OSError("usage sidecar unavailable")

    manager, spawner, _ = make_manager(tmp_path, usage_refresh=boom)
    record = manager.start_run(repo, TASK_ID)

    end_run(spawner, 0)
    status = only_status(manager)  # must not raise despite the hook failing

    assert status.state == "ended"
    data = read_record(manager, record.run_id)
    assert data["ended_ts"] is not None
    assert data["exit_code"] == 0
    events = read_events(manager.runs_dir / record.run_id)
    assert [event.kind for event in events] == [KIND_RUN_STARTED, KIND_RUN_ENDED]


def test_no_usage_hook_configured_is_a_silent_no_op(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)  # usage_refresh=None

    manager.start_run(repo, TASK_ID)
    end_run(spawner, 0)

    assert only_status(manager).state == "ended"


# -- pending-question notifications ----------------------------------------------


def write_question(control_dir: Path, seq: int, **fields) -> None:
    """Drop a control-dir question file the way the orchestrator would."""
    control_dir.joinpath(f"{seq:03d}-question.json").write_text(
        json.dumps({"seq": seq, **fields}), encoding="utf-8"
    )


def test_pending_question_fires_a_needs_input_notification(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)
    record = manager.start_run(repo, TASK_ID)

    write_question(record.control_dir, 1, kind="blocked", banner="Pick A or B?")
    status = only_status(manager)  # observe the question -> fires

    assert status.state == "waiting"
    note = notifier.notifications[0]
    assert len(notifier.notifications) == 1
    assert note.title == "repo-a: needs input"
    assert note.group == "repo-a"
    assert TASK_ID in note.body
    assert "Pick A or B?" in note.body


def test_pending_question_notifies_once_per_seq_across_rescans(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)
    record = manager.start_run(repo, TASK_ID)
    write_question(record.control_dir, 1, banner="ruling?")

    manager.list_runs()
    manager.get_run(record.run_id)
    manager.recent_runs(limit=10)
    manager.list_runs()

    assert len(notifier.notifications) == 1


def test_a_second_question_seq_fires_a_second_notification(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)
    record = manager.start_run(repo, TASK_ID)

    write_question(record.control_dir, 1, banner="first?")
    manager.list_runs()
    write_question(record.control_dir, 2, banner="second?")
    manager.list_runs()

    assert [n.title for n in notifier.notifications] == [
        "repo-a: needs input",
        "repo-a: needs input",
    ]
    assert "second?" in notifier.notifications[1].body


def test_answered_question_never_notifies(tmp_path):
    # pending_questions filters answered questions, so one answered before the
    # hub ever scans it never pushes.
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)
    record = manager.start_run(repo, TASK_ID)

    write_question(record.control_dir, 1, banner="ruling?")
    record.control_dir.joinpath("001-answer.json").write_text(
        json.dumps({"answer": "proceed"}), encoding="utf-8"
    )
    manager.list_runs()

    assert notifier.notifications == []


def test_question_notifier_failure_is_isolated(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RaisingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)
    record = manager.start_run(repo, TASK_ID)
    write_question(record.control_dir, 1, banner="ruling?")

    status = only_status(manager)  # must not raise despite delivery failing

    assert notifier.calls == 1
    assert status.state == "waiting"
    assert status.pending == 1


def test_no_notifier_configured_skips_question_push(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)  # notifier=None
    record = manager.start_run(repo, TASK_ID)
    write_question(record.control_dir, 1, banner="ruling?")

    status = only_status(manager)  # no notifier, no error, state still computed

    assert status.state == "waiting"
    assert status.pending == 1


def test_fresh_manager_re_notifies_a_still_open_question(tmp_path):
    # The de-dup set is in-memory (per hub process). A restart re-notifies a
    # still-pending question exactly once — an accepted reminder, not a persisted
    # guarantee (an answered question is no longer pending, so it never re-fires).
    repo = make_repo(tmp_path)
    spawner = FakeSpawner()
    notifier_a = RecordingNotifier()
    manager_a, _, _ = make_manager(tmp_path, spawner=spawner, notifier=notifier_a)
    record = manager_a.start_run(repo, TASK_ID)
    live_pid = spawner.procs[0].pid
    write_question(record.control_dir, 1, banner="ruling?")

    manager_a.list_runs()
    manager_a.list_runs()
    assert len(notifier_a.notifications) == 1  # once within the process

    notifier_b = RecordingNotifier()
    manager_b, _, _ = make_manager(
        tmp_path, pid_probe=lambda pid: pid == live_pid, notifier=notifier_b
    )
    manager_b.list_runs()  # fresh process, empty de-dup set

    assert len(notifier_b.notifications) == 1  # re-notifies the still-open question


def test_plan_gate_question_fires_one_compact_notification(tmp_path):
    # A plan-gate banner carries the full ~12 KB plan report; the push must stay
    # deliverable, so it drops the plan for a fixed compact summary.
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)
    record = manager.start_run(repo, TASK_ID)

    plan = "PLAN CONFIRMATION (--plan-gate):\n" + ("consider the tradeoffs " * 800)
    write_question(record.control_dir, 1, kind="plan-gate", banner=plan, message=plan)
    status = only_status(manager)

    assert status.state == "waiting"
    assert len(notifier.notifications) == 1
    note = notifier.notifications[0]
    assert note.title == "repo-a: needs input"
    assert TASK_ID in note.body
    assert "plan ready for review" in note.body
    assert "consider the tradeoffs" not in note.body  # plan not leaked into push
    assert len(note.body) < 200  # comfortably under any push payload ceiling


def test_repeated_scans_of_a_plan_gate_seq_do_not_duplicate(tmp_path):
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)
    record = manager.start_run(repo, TASK_ID)
    write_question(record.control_dir, 1, kind="plan-gate", banner="the plan")

    manager.list_runs()
    manager.get_run(record.run_id)
    manager.list_runs()

    assert len(notifier.notifications) == 1


def test_multiple_plan_revisions_each_notify(tmp_path):
    # Each plan-gate round writes a new seq'd question; per-(run_id, seq) de-dup
    # lets every revision notify while rescans of one revision stay single.
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    manager, _, _ = make_manager(tmp_path, notifier=notifier)
    record = manager.start_run(repo, TASK_ID)

    write_question(record.control_dir, 1, kind="plan-gate", banner="plan rev 1")
    manager.list_runs()
    write_question(record.control_dir, 2, kind="plan-gate", banner="plan rev 2")
    manager.list_runs()

    assert [n.body for n in notifier.notifications] == [
        f"{TASK_ID}\nplan ready for review",
        f"{TASK_ID}\nplan ready for review",
    ]


# -- per-session context snapshot (sessions.json) ------------------------------


def write_orch_log(repo: ManagedRepo, lines: list[str], *, stamp: str = "20260708T030000Z") -> Path:
    """Drop an orchestrator-style log into the run's real ``log_dir``.

    The stamp matches ``FIXED_NOW`` (the run's ``started_ts``) so the log locator
    attributes it to the run, and the filename shape mirrors the deployed
    orchestrator's ``<utc-ts>-<task-id>.log``.
    """
    log_dir = repo.path / ".cursor" / "orchestrator" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{stamp}-{TASK_ID}.log"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


ORCH_LOG_LINES = [
    "[03:00:10] --- dev session start: sid=dev1 backend=claude:x@max status_before=in_progress",
    "[03:10:00] run finished: status=finished context≈150000 tokens",
    "[03:10:00] --- dev session end: sid=dev1 status=final_review",
    "[03:11:00] --- review session start: sid=(pending) backend=codex:y@xhigh status_before=final_review",
    "[03:15:00] run finished: tool=codex exit=0 status=finished context≈90000 tokens",
    "[03:15:00] --- review session end: sid=rev1 status=completed+archived",
]


def sessions_json(manager: RunManager, run_id: str) -> Path:
    return manager.runs_dir / run_id / "sessions.json"


def test_run_end_persists_sessions_snapshot(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES)

    end_run(spawner, 0)
    manager.list_runs()  # observe the end -> snapshot written once

    snap = read_snapshot(manager.runs_dir / record.run_id)
    assert snap is not None
    assert [(s.role, s.kind, s.sid, s.context_tokens) for s in snap.sessions] == [
        ("dev", "session", "dev1", 150000),
        ("review", "session", "rev1", 90000),
    ]


def test_read_sessions_survives_repo_side_log_deletion(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    log_path = write_orch_log(repo, ORCH_LOG_LINES)

    end_run(spawner, 0)
    manager.list_runs()  # persists the snapshot
    log_path.unlink()  # repo-side log cleaned up afterward

    snap = manager.read_sessions(record.run_id)
    assert [s.sid for s in snap.sessions] == ["dev1", "rev1"]  # served from snapshot


def test_read_sessions_live_parses_current_log(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    # Only the first (still-open) dev session has been written so far.
    write_orch_log(repo, ORCH_LOG_LINES[:2])

    snap = manager.read_sessions(record.run_id)
    assert len(snap.sessions) == 1
    only = snap.sessions[0]
    assert only.sid == "dev1"
    assert only.open is True  # no end line yet: a live, open session
    assert only.context_tokens == 150000
    assert not sessions_json(manager, record.run_id).exists()  # no snapshot while live


def test_read_sessions_unknown_run_raises(tmp_path):
    manager, _, _ = make_manager(tmp_path)
    with pytest.raises(UnknownRunError):
        manager.read_sessions("20990101T000000Z-nope-x")


def test_read_sessions_falls_back_to_live_parse_when_no_snapshot(tmp_path):
    # A run that ended before this field shipped has no sessions.json; the read
    # falls back to parsing whatever repo-side log remains.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES)
    end_run(spawner, 0)
    manager.list_runs()
    sessions_json(manager, record.run_id).unlink()  # simulate a pre-field run

    snap = manager.read_sessions(record.run_id)
    assert [s.sid for s in snap.sessions] == ["dev1", "rev1"]


def test_ended_run_with_no_log_persists_empty_snapshot(tmp_path):
    # Ended while the hub was down and the log was already cleaned: the snapshot
    # is legitimately empty, and a present-empty snapshot is not re-parsed.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)  # no orchestrator log written

    end_run(spawner, 0)
    manager.list_runs()

    assert sessions_json(manager, record.run_id).exists()
    assert manager.read_sessions(record.run_id).sessions == ()


def test_recent_runs_embeds_persisted_sessions_snapshot(tmp_path):
    # An ended run's history entry carries its persisted per-session snapshot, so
    # the history view renders context strips without a per-run follow-up read.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES)
    end_run(spawner, 0)
    manager.list_runs()  # observe end -> snapshot persisted

    entry = manager.recent_runs(limit=1)[0]
    assert [(s.role, s.sid, s.context_tokens) for s in entry.sessions.sessions] == [
        ("dev", "dev1", 150000),
        ("review", "rev1", 90000),
    ]
    assert entry.sessions.context_budget == 200_000


def test_recent_runs_sessions_empty_for_live_run(tmp_path):
    # History is snapshot-only (no live parse under the lock): a still-running run
    # has no persisted snapshot yet, so its embedded sessions are empty. The live
    # strip is served on run detail via read_sessions instead.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:2])  # a log exists, but the run is live

    entry = manager.recent_runs(limit=1)[0]
    assert entry.sessions.sessions == ()


def write_task_with_est(repo: ManagedRepo, current: int, total: int) -> None:
    """Overwrite ``TASK_ID``'s task file with a ``session-est: current/total``."""
    (repo.path / ".ai-tasks" / f"{TASK_ID}.md").write_text(
        f"---\nid: {TASK_ID}\nstatus: in_progress\n"
        f"session-est: {current}/{total}\n---\n\n# Task\n",
        encoding="utf-8",
    )


def test_recent_runs_embeds_session_est(tmp_path):
    # A run's history entry carries its task's typed session-est for the
    # est-vs-actual rollup, serialized as a top-level session_est object.
    repo = make_repo(tmp_path)
    write_task_with_est(repo, 1, 3)
    manager, _, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)

    entry = manager.recent_runs(limit=1)[0]
    assert entry.session_est == SessionEst(current=1, total=3)
    assert entry.to_dict()["session_est"] == {"current": 1, "total": 3}


def test_recent_runs_session_est_unknown_without_frontmatter(tmp_path):
    # The default task file carries no session-est, so the est is unknown (None),
    # serialized as session_est: null.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)

    entry = manager.recent_runs(limit=1)[0]
    assert entry.session_est is None
    assert entry.to_dict()["session_est"] is None


def test_recent_runs_session_est_reads_archived_task_after_closeout(tmp_path):
    # After close-out archives the completed task, recent_runs still reads its est
    # from archive/ via the run record's id, so a completed task's rollup keeps
    # its estimate (mirrors read_session_est's active-then-archive fallback).
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)
    end_run(spawner, 0)
    manager.list_runs()  # observe the end
    # Simulate close-out: drop the active file + index row, archive with est.
    tasks_dir = repo.path / ".ai-tasks"
    (tasks_dir / f"{TASK_ID}.md").unlink()
    (tasks_dir / "index.md").write_text(
        "# Active tasks\n\n| Task | Status | Summary |\n|---|---|---|\n",
        encoding="utf-8",
    )
    archive = tasks_dir / "archive"
    archive.mkdir()
    (archive / f"{TASK_ID}.md").write_text(
        f"---\nid: {TASK_ID}\nstatus: completed\nsession-est: 3/3\n---\n\n# Task\n",
        encoding="utf-8",
    )

    entry = manager.recent_runs(limit=1)[0]
    assert entry.session_est == SessionEst(3, 3)


def test_recent_runs_session_est_isolated_on_read_failure(tmp_path, monkeypatch):
    # A target-repo est read failure must degrade to unknown, never raise into
    # the history read (mirrors the snapshot/notifier isolation).
    repo = make_repo(tmp_path)
    write_task_with_est(repo, 1, 3)
    manager, _, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)

    def boom(_repo, _task_id):
        raise OSError("task file unreadable")

    monkeypatch.setattr("orch_hub.runs.read_session_est", boom)

    entry = manager.recent_runs(limit=1)[0]
    assert entry.session_est is None  # unknown, not a crash
    assert entry.status.record.task_id == TASK_ID  # run still in history


def test_recent_runs_session_est_deduped_per_task(tmp_path, monkeypatch):
    # Two runs of the same (repo, task) read the frontmatter once, deduped within
    # the call so a task with many runs does not re-read its task file per run.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)
    end_run(spawner, 0)
    manager.list_runs()  # free the repo
    manager.start_run(repo, TASK_ID)  # second run, same task

    calls: list[str] = []

    def counting(_repo, task_id):
        calls.append(task_id)
        return SessionEst(1, 2)

    monkeypatch.setattr("orch_hub.runs.read_session_est", counting)

    entries = manager.recent_runs(limit=10)
    assert len(entries) == 2
    assert all(e.session_est == SessionEst(1, 2) for e in entries)
    assert calls == [TASK_ID]  # read once despite two runs of the task


def test_snapshot_failure_does_not_corrupt_run_end(tmp_path, monkeypatch):
    # A parse/IO failure in snapshotting must be swallowed like a notifier
    # failure: the run still ends and classifies, only the snapshot is missing.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES)

    def boom(_record):
        raise OSError("disk gone")

    monkeypatch.setattr("orch_hub.runs.read_live", boom)
    end_run(spawner, 0)
    status = only_status(manager)  # must not raise

    assert status.state == "ended"
    assert read_record(manager, record.run_id)["ended_ts"] is not None
    assert not sessions_json(manager, record.run_id).exists()


# -- session-entry task snapshots (task_snapshots.json) ------------------------


def task_snapshots_json(manager: RunManager, run_id: str) -> Path:
    return manager.runs_dir / run_id / "task_snapshots.json"


def task_file(repo: ManagedRepo) -> Path:
    return repo.path / ".ai-tasks" / f"{TASK_ID}.md"


def test_list_runs_captures_the_live_sessions_entry_task_file(tmp_path):
    # The scan the resident monitor drives is the capture trigger: a live run with
    # an open session bracket gets that session's task file recorded.
    repo = make_repo(tmp_path)
    manager, _, clock = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:1])  # dev session start, no end yet
    clock.tick(30)  # 03:00:30, i.e. 20s after the 03:00:10 start line

    manager.list_runs()

    kept = read_task_snapshots(manager.runs_dir / record.run_id)
    assert [(e.index, e.sid, e.role, e.present) for e in kept.snapshots] == [
        (0, "dev1", "dev", True)
    ]
    assert kept.snapshots[0].content == task_file(repo).read_text(encoding="utf-8")
    assert kept.snapshots[0].entry_lag_seconds == 20
    assert kept.snapshots[0].capture_point == "post_claim"


def test_a_multi_session_run_captures_one_snapshot_per_session(tmp_path):
    # The acceptance case: each session's own entry state, not one shared copy of
    # whatever the file looked like when the run ended.
    repo = make_repo(tmp_path)
    manager, _, clock = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:1])
    clock.tick(30)
    manager.list_runs()

    # The dev session works the task and hands off; review opens at 03:11:00.
    write_task_file(repo.path, TASK_ID, status="final_review")
    write_orch_log(repo, ORCH_LOG_LINES[:4])
    clock.tick(11 * 60)  # 03:11:30
    manager.list_runs()

    kept = read_task_snapshots(manager.runs_dir / record.run_id)
    assert [(e.index, e.role, e.present) for e in kept.snapshots] == [
        (0, "dev", True),
        (1, "review", True),
    ]
    assert "status: in_progress" in kept.snapshots[0].content
    assert "status: final_review" in kept.snapshots[1].content
    assert kept.snapshots[1].status_before == "final_review"


def test_run_end_settles_a_session_id_the_log_revealed_late(tmp_path):
    # Codex logs sid=(pending) at start; the review session's real id appears only
    # on its end line, which the end transition reconciles.
    repo = make_repo(tmp_path)
    manager, spawner, clock = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:4])  # review session open, sid=(pending)
    clock.tick(11 * 60 + 30)
    manager.list_runs()
    assert read_task_snapshots(manager.runs_dir / record.run_id).for_sid("rev1") is None

    write_orch_log(repo, ORCH_LOG_LINES)  # end line reveals sid=rev1
    end_run(spawner, 0)
    manager.list_runs()

    kept = read_task_snapshots(manager.runs_dir / record.run_id)
    assert kept.for_sid("rev1") is not None
    assert kept.for_sid("rev1").index == 1


def test_a_missing_task_file_does_not_fail_the_scan(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, clock = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    task_file(repo).unlink()
    write_orch_log(repo, ORCH_LOG_LINES[:1])
    clock.tick(30)

    statuses = manager.list_runs()  # must not raise

    assert [status.state for status in statuses] == ["active"]
    assert not task_snapshots_json(manager, record.run_id).exists()


def test_an_ended_run_captures_nothing(tmp_path):
    # A terminal run's file is that run's output, never a session's entry state.
    repo = make_repo(tmp_path)
    manager, spawner, clock = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:1])
    end_run(spawner, 0)
    clock.tick(30)

    manager.list_runs()

    assert not task_snapshots_json(manager, record.run_id).exists()


def test_task_snapshot_failure_does_not_fail_a_scan(tmp_path, monkeypatch):
    # Isolation, like the sessions snapshot: capture is best-effort bookkeeping and
    # must never propagate into a status read.
    repo = make_repo(tmp_path)
    manager, _, clock = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:1])
    clock.tick(30)

    def boom(*_args, **_kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr("orch_hub.runs.capture_entry", boom)
    statuses = manager.list_runs()  # must not raise

    assert [status.state for status in statuses] == ["active"]
    assert not task_snapshots_json(manager, record.run_id).exists()


# -- effective-config launch snapshot (effective_config.json) ------------------


EFFECTIVE_CONFIG = {
    "schema_version": 1,
    "config_revision": "cfg-sha",
    "effective_revision": "eff-sha",
    # A split launch leaves the run-wide `profile` at its default and reports the
    # authoritative per-role selections; the snapshot keeps both verbatim.
    "profile": "default",
    "profiles": {"dev": "standard", "review": "excellent"},
    "available_profiles": ["excellent", "standard"],
    "backend": "cc-codex",
    "dev": {"agent": "claude", "model": "claude-opus-4-8", "effort": "max"},
    "review": {"agent": "codex", "model": "gpt-5.5", "effort": "xhigh"},
    "max_sessions": 40,
    "context_budget": 200000,
    "codex_sandbox": "danger-full-access",
    "task_id": TASK_ID,
    "once": False,
    "plan_gate": False,
    "control_dir": "/hub/run/control",
    "sources": {"backend": "config"},
}


def effective_config_line() -> str:
    """The startup line the orchestrator logs (timestamp prefix + compact JSON)."""
    return "[03:00:00] effective-config: " + json.dumps(
        EFFECTIVE_CONFIG, sort_keys=True, separators=(",", ":")
    )


def effective_config_json(manager: RunManager, run_id: str) -> Path:
    return manager.runs_dir / run_id / "effective_config.json"


def test_run_end_persists_effective_config_snapshot(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, [effective_config_line()] + ORCH_LOG_LINES)

    end_run(spawner, 0)
    manager.list_runs()  # observe end -> snapshot written once

    snap = read_effective_config_snapshot(manager.runs_dir / record.run_id)
    assert snap == EFFECTIVE_CONFIG


def test_read_effective_config_live_parses_current_log(tmp_path):
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, [effective_config_line()] + ORCH_LOG_LINES[:2])

    assert manager.read_effective_config(record.run_id) == EFFECTIVE_CONFIG
    # No snapshot file while the run is live — parsed live on demand.
    assert not effective_config_json(manager, record.run_id).exists()


def test_read_effective_config_survives_repo_side_log_deletion(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    log_path = write_orch_log(repo, [effective_config_line()] + ORCH_LOG_LINES)

    end_run(spawner, 0)
    manager.list_runs()  # persists the snapshot
    log_path.unlink()  # repo-side log cleaned up afterward

    assert manager.read_effective_config(record.run_id) == EFFECTIVE_CONFIG  # from snapshot


def test_read_effective_config_falls_back_to_spawn_out(tmp_path):
    # A run whose orchestrator log never appeared still has the line in the hub's
    # own spawn.out capture (orch.log prints to stdout too), so the snapshot is
    # recovered from there.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)  # no orchestrator log written
    (manager.runs_dir / record.run_id / "spawn.out").write_text(
        effective_config_line() + "\n", encoding="utf-8"
    )

    end_run(spawner, 0)
    manager.list_runs()

    assert read_effective_config_snapshot(manager.runs_dir / record.run_id) == EFFECTIVE_CONFIG


def test_read_effective_config_none_when_line_absent(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES)  # sessions but no effective-config line

    assert manager.read_effective_config(record.run_id) is None  # live: not found
    end_run(spawner, 0)
    manager.list_runs()
    assert not effective_config_json(manager, record.run_id).exists()  # nothing to persist
    assert manager.read_effective_config(record.run_id) is None


def test_read_effective_config_unknown_run_raises(tmp_path):
    manager, _, _ = make_manager(tmp_path)
    with pytest.raises(UnknownRunError):
        manager.read_effective_config("20990101T000000Z-nope-x")


def test_recent_runs_embeds_effective_config(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)
    write_orch_log(repo, [effective_config_line()] + ORCH_LOG_LINES)
    end_run(spawner, 0)
    manager.list_runs()  # observe end -> snapshot persisted

    entry = manager.recent_runs(limit=1)[0]
    assert entry.effective_config == EFFECTIVE_CONFIG
    assert entry.to_dict()["effective_config"] == EFFECTIVE_CONFIG


def test_recent_runs_effective_config_none_for_live_run(tmp_path):
    # History is snapshot-only: a still-running run has no persisted snapshot yet.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)
    write_orch_log(repo, [effective_config_line()] + ORCH_LOG_LINES[:2])

    entry = manager.recent_runs(limit=1)[0]
    assert entry.effective_config is None
    assert entry.to_dict()["effective_config"] is None


def test_effective_config_snapshot_failure_does_not_corrupt_run_end(tmp_path, monkeypatch):
    # Isolation, like the sessions snapshot: a parse/IO failure is swallowed and the
    # run still ends and classifies.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, [effective_config_line()] + ORCH_LOG_LINES)

    def boom(_record):
        raise OSError("disk gone")

    monkeypatch.setattr("orch_hub.runs.read_effective_config_log", boom)
    end_run(spawner, 0)
    status = only_status(manager)  # must not raise

    assert status.state == "ended"
    assert read_record(manager, record.run_id)["ended_ts"] is not None
    assert not effective_config_json(manager, record.run_id).exists()


# -- load_records (read-only historical reader) -------------------------------


def test_load_records_reads_persisted_runs_without_mutating(tmp_path):
    # load_records is the read-only entry point for evidence aggregation: unlike
    # list_runs it must not refresh, so a dead pid does NOT flip the run to ended
    # or append a run_ended event.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)  # pid_probe returns False (dead)
    record = manager.start_run(repo, TASK_ID)
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data")
    run_dir = manager.runs_dir / record.run_id
    run_json_before = (run_dir / "run.json").read_text(encoding="utf-8")
    events_before = (run_dir / "events.jsonl").read_text(encoding="utf-8")

    pairs = load_records(config)

    assert [p[0] for p in pairs] == [run_dir]
    loaded = pairs[0][1]
    assert loaded.run_id == record.run_id
    assert loaded.repo_path == repo.path
    assert loaded.task_id == TASK_ID
    assert loaded.ended_ts is None  # never observed the end (no pid probe)
    # No write of any kind happened.
    assert (run_dir / "run.json").read_text(encoding="utf-8") == run_json_before
    assert (run_dir / "events.jsonl").read_text(encoding="utf-8") == events_before


def test_load_records_missing_runs_dir_is_empty(tmp_path):
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "nowhere")
    assert load_records(config) == []


def test_load_records_surfaces_broken_dir_as_error_stub(tmp_path):
    config = Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data")
    broken = config.data_dir / "runs" / "20260708T030000Z-broken"
    broken.mkdir(parents=True)  # no run.json

    pairs = load_records(config)

    assert len(pairs) == 1
    run_dir, record = pairs[0]
    assert run_dir == broken
    assert record.error is not None
    assert record.repo_path is None  # unattributable, so it never matches a repo
