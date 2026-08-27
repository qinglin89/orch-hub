"""Tests for the run manager (launch, list, stop, reattach).

Everything runs against fake repos in ``tmp_path`` with an injected spawner,
readiness checker, pid probe, and clock — no real subprocess is ever started
and no orchestrator code is imported. Task gating uses the real task-store
parser over fake ``.ai-tasks`` files so both sides stay in lockstep.
"""

from __future__ import annotations

import datetime as dt
import hashlib
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
from orch_hub.protocol_provenance import (
    DEPLOY_LOCK_NAME,
    DEPLOY_MANIFEST_NAME,
    STABILITY_CHANGED,
    STABILITY_DRIFTED,
    STABILITY_STABLE,
    STABILITY_UNVERIFIED,
    read_protocol,
)
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
            ReadinessCheck("mandrel_status", False, "mandrel status failed: drift"),
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
    task_completed_hook=None,
) -> tuple[RunManager, FakeSpawner, FakeClock]:
    spawner = spawner if spawner is not None else FakeSpawner()
    clock = clock if clock is not None else FakeClock()
    config = Config(mandrel_path=root / "mandrel", data_dir=root / "hub-data")
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
        task_completed_hook=task_completed_hook,
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


def write_orch_log(repo: ManagedRepo, lines: list[str], *, stamp: str = "20260708T030000Z") -> Path:
    """Drop an orchestrator-style log into the run's real ``log_dir``.

    The stamp matches ``FIXED_NOW`` (the run's ``started_ts``) so the log locator
    attributes it to the run, and the filename shape mirrors the deployed
    orchestrator's ``<utc-ts>-<task-id>.log``.
    """
    log_dir = repo.path / ".mandrel" / "orchestrator" / "logs"
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


# -- command construction ------------------------------------------------------


def test_start_run_builds_the_documented_command(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)

    record = manager.start_run(repo, TASK_ID)

    orch = repo.path / ".mandrel" / "orchestrator"
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


def test_start_run_does_not_leak_frozen_bootloader_vars_to_the_orchestrator(
    tmp_path, monkeypatch
):
    # In a compiled release the bootloader exports variables describing *this*
    # process's bundle. They mean nothing to the deployed orchestrator or to the
    # agent CLIs it launches, and another frozen program would read them as its
    # own bootloader state.
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", "/opt/orch-hub/orch-hub")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("ORCH_HUB_DATA_DIR", str(tmp_path / "data"))
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)

    manager.start_run(repo, TASK_ID)

    child_env = spawner.calls[0]["env"]
    assert not [name for name in child_env if name.startswith("_PYI")]
    # Only the bootloader's own variables are dropped; the rest is inherited.
    assert child_env["ORCH_HUB_DATA_DIR"] == str(tmp_path / "data")


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
        "log_dir": str(repo.path / ".mandrel" / "orchestrator" / "logs"),
        "spawn_out": str(run_dir / "spawn.out"),
        "argv": list(spawner.calls[0]["argv"]),
        "pid": spawner.procs[0].pid,
        "started_ts": clock.now.isoformat(),
        "ended_ts": None,
        "exit_code": None,
        # The end-tail marker: false for a run that has not ended.
        "end_settling": False,
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


def test_persisted_log_dir_survives_a_deployment_path_move(tmp_path):
    # The deployed orchestrator dir has moved once and may move again. ``log_dir``
    # is stamped into run.json at launch and loaded back verbatim: the hub never
    # recomputes it from the current constant, so runs recorded under an older
    # layout keep reading their own logs and need no migration.
    manager, _, _ = make_manager(tmp_path)
    run_dir = manager.runs_dir / "20260708T030000Z-repo-a-old"
    run_dir.mkdir(parents=True)
    legacy_log_dir = Path("/repos/repo-a/previous-deploy-dir/orchestrator/logs")
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "repo_path": "/repos/repo-a",
                "log_dir": str(legacy_log_dir),
            }
        ),
        encoding="utf-8",
    )

    record = manager.get_run(run_dir.name).record
    assert record.log_dir == legacy_log_dir
    assert record.log_dir != record.repo_path / runs_module.ORCHESTRATOR_LOG_DIR
    # And the read did not rewrite the stored path either.
    assert read_record(manager, run_dir.name)["log_dir"] == str(legacy_log_dir)


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
        "mandrel_status: mandrel status failed: drift",
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


def end_run(spawner: FakeSpawner, exit_code: int, index: int = 0) -> None:
    spawner.procs[index].returncode = exit_code


def close_out_task(repo: ManagedRepo) -> None:
    """Do to the target repo what close-out does: archive the task, drop its row."""
    write_index(repo.path, [])
    (repo.path / ".ai-tasks" / f"{TASK_ID}.md").unlink()


def answer_stop(record: RunRecord, seq: int = 1) -> None:
    """Write the control-dir question/answer pair for a human ``stop``."""
    record.control_dir.joinpath(f"{seq:03d}-question.json").write_text(
        json.dumps({"seq": seq, "banner": "?"}), encoding="utf-8"
    )
    record.control_dir.joinpath(f"{seq:03d}-answer.json").write_text(
        json.dumps({"answer": "stop"}), encoding="utf-8"
    )


def test_own_session_ending_on_a_completed_task_classifies_completed(tmp_path):
    # The run's *own* log says the final gate passed while it was running. The
    # target repo's task file still reads in_progress and is never consulted.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:-1] + [
        "[03:15:00] --- review session end: sid=rev1 status=completed",
    ])

    end_run(spawner, 0)

    status = only_status(manager)
    assert status.state == "completed"
    assert "close-out" in status.detail


def test_own_session_ending_on_an_archived_task_classifies_completed(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES)  # last bracket: status=completed+archived

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

    answer_stop(record)
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


def test_each_run_of_one_task_keeps_its_own_verdict(tmp_path):
    # A task outlives its runs. Three runs of one task here: the first stopped on
    # a human 'stop' answer, the second died with a nonzero exit, the third
    # finished the task — and close-out then archives it. Back when the shared
    # task state decided the verdict, that last step retroactively repainted all
    # three 'completed' and left the stop and exit branches unreachable behind it.
    repo = make_repo(tmp_path)
    manager, spawner, clock = make_manager(tmp_path)

    stopped = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:3], stamp="20260708T030000Z")
    answer_stop(stopped)
    end_run(spawner, 1, index=0)
    manager.list_runs()  # observe the end: the repo is free for the next run

    clock.tick(60)
    failed = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:3], stamp="20260708T030100Z")
    end_run(spawner, 1, index=1)
    manager.list_runs()

    clock.tick(60)
    completed = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES, stamp="20260708T030200Z")
    end_run(spawner, 0, index=2)
    manager.list_runs()

    close_out_task(repo)  # the shared evidence moves on; the verdicts must not

    assert {s.record.run_id: s.state for s in manager.list_runs()} == {
        stopped.run_id: "stopped",
        failed.run_id: "error",
        completed.run_id: "completed",
    }


def test_a_stopped_run_stays_stopped_when_the_task_later_completes(tmp_path):
    # The run answered 'stop' and went no further. Whatever the task does
    # afterwards — reaches the final gate, gets archived, is re-opened under the
    # same id — is a later run's story, not this one's.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:3])
    answer_stop(record)
    end_run(spawner, 1)

    assert only_status(manager).state == "stopped"

    write_task_file(repo.path, TASK_ID, status="completed")
    assert only_status(manager).state == "stopped"

    close_out_task(repo)
    assert only_status(manager).state == "stopped"

    write_index(repo.path, [TASK_ID])  # re-opened under the same id
    write_task_file(repo.path, TASK_ID, status="in_progress")
    assert only_status(manager).state == "stopped"


def test_a_run_with_no_evidence_of_its_own_degrades_to_ended(tmp_path):
    # This run's dir holds nothing: no session bracket, no stop signal, a bare
    # exit 0. Its task is completed and archived — exactly the reading that used
    # to make it 'completed'. Unknown is the honest verdict; the alternative is
    # borrowing another run's outcome.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    manager.start_run(repo, TASK_ID)

    close_out_task(repo)
    end_run(spawner, 0)

    status = only_status(manager)
    assert status.state == "ended"
    assert "no completion evidence" in status.detail


BLOCKED_RESUME_CLOSEOUT_LINES = [
    "[03:20:00] resuming blocked review session rev1 with answer",
    "[03:25:00] run finished: tool=codex exit=0 status=finished context≈90000 tokens",
    "[03:25:10] task file archived mid-session — the native hook chain ran the "
    "close-out inside the resumed session; skipping post-checks",
    "[03:25:11] close-out done (performed in-session by the native hook chain; "
    "orchestrator verified archive/index/tree/remaining tasks)",
    "[03:25:12] orchestrator done",
]
"""What the orchestrator logs when a *blocked* task's resumed session concludes
its final gate: the resume re-enters the blocked session in place, so no
``--- {role} session start/end`` bracket is ever opened for the turn that
finished the task."""


def write_spawn_capture(manager, record: RunRecord, lines: list[str]) -> Path:
    """Write the run's own stdout capture — every ``log()`` line is printed too."""
    path = manager.runs_dir / record.run_id / "spawn.out"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def test_a_blocked_resume_that_archives_the_task_classifies_completed(tmp_path):
    # Launched against a blocked task: the operator answers the blocker, the
    # resumed reviewer passes the final gate, and its native hook chain archives
    # the task in-session. The run finished its task with no bracket to say so,
    # and brackets alone would have called it merely 'ended'.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    write_task_file(repo.path, TASK_ID, status="blocked")
    record = manager.start_run(repo, TASK_ID, acknowledge_blocked=True)
    write_orch_log(repo, BLOCKED_RESUME_CLOSEOUT_LINES)
    write_spawn_capture(manager, record, BLOCKED_RESUME_CLOSEOUT_LINES)

    end_run(spawner, 0)
    close_out_task(repo)  # the shared evidence moves on; the verdict must not

    status = only_status(manager)
    assert status.state == "completed"
    assert status.detail == "task closed out during this run"


def test_a_blocked_resume_after_earlier_sessions_classifies_completed(tmp_path):
    # Same path, reached mid-run: a review session ends with status=blocked, the
    # loop resumes that session with the human's answer, and the final gate
    # concludes there. The last bracket still reads 'blocked' — an interim status
    # — so the close-out line is the only completion evidence the run holds.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    lines = ORCH_LOG_LINES[:-1] + [
        "[03:15:00] --- review session end: sid=rev1 status=blocked",
        *BLOCKED_RESUME_CLOSEOUT_LINES,
    ]
    write_orch_log(repo, lines)
    write_spawn_capture(manager, record, lines)

    end_run(spawner, 0)

    status = only_status(manager)
    assert status.state == "completed"
    assert status.detail == "task closed out during this run"


def test_a_blocked_run_with_no_close_out_line_stays_ended(tmp_path):
    # The other half of the same shape: the resumed session did *not* conclude
    # the gate, so nothing closed out. Without that line the run holds no
    # completion evidence at all and degrades honestly.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    lines = ORCH_LOG_LINES[:-1] + [
        "[03:15:00] --- review session end: sid=rev1 status=blocked",
        "[03:20:00] resuming blocked review session rev1 with answer",
    ]
    write_orch_log(repo, lines)
    write_spawn_capture(manager, record, lines)

    end_run(spawner, 0)

    assert only_status(manager).state == "ended"


def test_a_stopped_blocked_resume_is_not_repainted_by_a_close_out_line(tmp_path):
    # Completion evidence outranks stop evidence, so pin the case where the two
    # meet: no close-out line, a human 'stop' answer, and a last bracket reading
    # 'blocked'. The run stopped; nothing here says it finished the task.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    lines = ORCH_LOG_LINES[:-1] + [
        "[03:15:00] --- review session end: sid=rev1 status=blocked",
    ]
    write_orch_log(repo, lines)
    write_spawn_capture(manager, record, lines)
    answer_stop(record)

    end_run(spawner, 0)
    close_out_task(repo)

    assert only_status(manager).state == "stopped"


def test_close_out_evidence_is_read_from_the_run_dir_not_the_repo_log(tmp_path):
    # Containment: the repo-side log is located by a name two same-second runs of
    # one task share, so it is not evidence about one run. A close-out line that
    # exists only there must not promote this run.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, BLOCKED_RESUME_CLOSEOUT_LINES)
    write_spawn_capture(manager, record, BLOCKED_RESUME_CLOSEOUT_LINES[:1])

    end_run(spawner, 0)

    assert only_status(manager).state == "ended"


def test_pre_snapshot_run_classifies_from_its_spawn_out_capture(tmp_path):
    # The orchestrator writes its marker stream to stdout as well as to its log,
    # so the run dir's own spawn.out carries the evidence for runs that predate
    # sessions.json — and for any run whose end found no log to snapshot.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    run_dir = manager.runs_dir / record.run_id
    (run_dir / "spawn.out").write_text(
        "".join(line + "\n" for line in ORCH_LOG_LINES), encoding="utf-8"
    )

    end_run(spawner, 0)  # no repo-side log exists: the snapshot lands empty

    assert only_status(manager).state == "completed"
    assert read_snapshot(run_dir).sessions == ()

    (run_dir / "sessions.json").unlink()  # a genuinely pre-field run
    assert only_status(manager).state == "completed"


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

    write_orch_log(repo, ORCH_LOG_LINES)  # this run archived its task
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


# -- task-completion hook at the terminal transition -------------------------------
# The evaluation sidecar's seam: a plain Callable[[RunRecord], None] fired once per
# run, only when that run's own frozen evidence classifies it 'completed'. Isolated
# exactly like notification delivery and the usage refresh — the run is already
# persisted, so a broken sidecar can never touch run state.


def completing_log() -> list[str]:
    """Orchestrator log whose last bracket says the task completed and archived."""
    return list(ORCH_LOG_LINES)


def test_a_completed_run_announces_its_task(tmp_path):
    repo = make_repo(tmp_path)
    seen: list[RunRecord] = []
    manager, spawner, _ = make_manager(tmp_path, task_completed_hook=seen.append)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, completing_log())

    manager.list_runs()  # still live -> nothing completed yet
    assert seen == []

    end_run(spawner, 0)
    manager.list_runs()  # the live->ended transition

    assert [r.run_id for r in seen] == [record.run_id]
    # The record carries the attribution the sidecar needs to reach the task.
    assert seen[0].task_id == TASK_ID
    assert seen[0].managed_repo().path == repo.path


@pytest.mark.parametrize(
    "log_lines, exit_code, expected_state",
    [
        # Exited 0 with no completion evidence at all (e.g. a --once session).
        ([], 0, "ended"),
        # Ended mid-task: the final bracket carries an interim status.
        (ORCH_LOG_LINES[:3], 0, "ended"),
        # Crashed: no completion evidence, nonzero exit.
        ([], 1, "error"),
    ],
)
def test_an_end_that_is_not_a_completion_announces_nothing(
    tmp_path, log_lines, exit_code, expected_state
):
    repo = make_repo(tmp_path)
    seen: list[RunRecord] = []
    manager, spawner, _ = make_manager(tmp_path, task_completed_hook=seen.append)
    manager.start_run(repo, TASK_ID)
    if log_lines:
        write_orch_log(repo, log_lines)

    end_run(spawner, exit_code)

    assert only_status(manager).state == expected_state
    assert seen == []


def test_a_stopped_run_announces_nothing(tmp_path):
    repo = make_repo(tmp_path)
    seen: list[RunRecord] = []
    manager, spawner, _ = make_manager(tmp_path, task_completed_hook=seen.append)
    record = manager.start_run(repo, TASK_ID)
    manager.stop_run(record.run_id)

    end_run(spawner, 0)

    assert only_status(manager).state == "stopped"
    assert seen == []


def test_completion_is_announced_exactly_once_across_rescans(tmp_path):
    repo = make_repo(tmp_path)
    seen: list[RunRecord] = []
    manager, spawner, _ = make_manager(tmp_path, task_completed_hook=seen.append)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, completing_log())

    end_run(spawner, 0)
    manager.list_runs()  # transition observed here
    manager.list_runs()  # settled record: no re-announce
    manager.get_run(record.run_id)
    manager.recent_runs(limit=10)
    manager.history_page()

    assert len(seen) == 1


def test_completion_hook_failure_does_not_corrupt_run_state(tmp_path):
    repo = make_repo(tmp_path)

    def boom(record: RunRecord) -> None:
        raise RuntimeError("evaluation sidecar exploded")

    manager, spawner, _ = make_manager(tmp_path, task_completed_hook=boom)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, completing_log())

    end_run(spawner, 0)
    status = only_status(manager)  # must not raise despite the hook failing

    assert status.state == "completed"
    data = read_record(manager, record.run_id)
    assert data["ended_ts"] is not None
    assert data["exit_code"] == 0
    events = read_events(manager.runs_dir / record.run_id)
    assert [event.kind for event in events] == [KIND_RUN_STARTED, KIND_RUN_ENDED]


def test_no_completion_hook_configured_is_a_silent_no_op(tmp_path):
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)  # task_completed_hook=None
    manager.start_run(repo, TASK_ID)
    write_orch_log(repo, completing_log())

    end_run(spawner, 0)

    assert only_status(manager).state == "completed"


def test_completion_is_announced_after_the_run_settles_its_evidence(tmp_path):
    """Evidence must be settled before the sidecar looks at it.

    The sessions snapshot, session-entry task snapshots, and the protocol
    identity are all written at this same transition. A hook that fired first
    would build an evidence pack against unsettled artifacts and record a
    downgraded pack rather than wait for one that exists moments later.
    """
    repo = make_repo(tmp_path)
    observed: dict = {}
    manager, spawner, _ = make_manager(
        tmp_path,
        task_completed_hook=lambda record: observed.update(
            sessions=read_snapshot(manager.runs_dir / record.run_id) is not None,
            effective_config=(
                manager.runs_dir / record.run_id / "effective_config.json"
            ).is_file(),
            ended_persisted=json.loads(
                (manager.runs_dir / record.run_id / "run.json").read_text(
                    encoding="utf-8"
                )
            )["ended_ts"]
            is not None,
        ),
    )
    manager.start_run(repo, TASK_ID)
    write_orch_log(
        repo,
        completing_log()
        + ['[03:00:00] effective-config: {"schema_version": 1, "profile": "x"}'],
    )

    end_run(spawner, 0)
    manager.list_runs()

    assert observed == {
        "sessions": True,
        "effective_config": True,
        "ended_persisted": True,
    }


# -- two-phase end: an interrupted transition -----------------------------------


class Interrupt(BaseException):
    """A hub dying mid-transition: nothing on the end tail catches BaseException."""


def die(*_args, **_kwargs):
    raise Interrupt("hub killed mid-transition")


def test_the_end_write_marks_the_transition_owed_until_its_tail_runs(tmp_path):
    """``ended_ts`` and "the tail still owed" land in one atomic write.

    They have to: ``ended_ts`` is irreversible (nothing re-observes a run that
    carries one), while everything the end triggers happens afterwards in other
    files. Marking the debt in the same rename is what makes an interrupted
    transition recoverable instead of invisible.
    """
    repo = make_repo(tmp_path)
    owed: list[bool] = []
    manager, spawner, _ = make_manager(
        tmp_path,
        task_completed_hook=lambda record: owed.append(
            read_record(manager, record.run_id)["end_settling"]
        ),
    )
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, completing_log())

    end_run(spawner, 0)
    manager.list_runs()

    assert owed == [True]  # owed while the tail runs...
    assert read_record(manager, record.run_id)["end_settling"] is False  # ...paid after


def test_an_interrupted_end_tail_is_resumed_by_a_restarted_hub(tmp_path):
    """The completion outlives the process that observed it."""
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path, task_completed_hook=die)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, completing_log())

    end_run(spawner, 0)
    with pytest.raises(Interrupt):
        manager.list_runs()

    data = read_record(manager, record.run_id)
    assert data["ended_ts"] is not None  # the end is already permanent
    assert data["end_settling"] is True  # and its tail is on record as owed

    seen: list[RunRecord] = []
    manager_b, _, _ = make_manager(tmp_path, task_completed_hook=seen.append)
    assert only_status(manager_b).state == "completed"

    assert [r.run_id for r in seen] == [record.run_id]
    assert read_record(manager_b, record.run_id)["end_settling"] is False
    # One end, one audit event: the resumed tail replays work, not history.
    events = read_events(manager.runs_dir / record.run_id)
    assert [event.kind for event in events] == [KIND_RUN_STARTED, KIND_RUN_ENDED]
    # And it is settled for good now.
    manager_b.list_runs()
    assert len(seen) == 1


def test_a_run_that_ended_before_the_marker_existed_is_never_resumed(tmp_path):
    """The cohort bound: no marker means settled, so history is never replayed.

    Without it, "ended run whose tail we cannot prove ran" would match every run
    that ever ended — the whole archived backlog would be announced on the first
    scan after this shipped.
    """
    repo = make_repo(tmp_path)
    seen: list[RunRecord] = []
    manager, spawner, _ = make_manager(tmp_path, task_completed_hook=seen.append)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, completing_log())
    end_run(spawner, 0)
    manager.list_runs()
    assert len(seen) == 1

    # Rewrite the record the way a hub without this field left it: ended, unmarked.
    run_json = manager.runs_dir / record.run_id / "run.json"
    data = json.loads(run_json.read_text(encoding="utf-8"))
    del data["end_settling"]
    run_json.write_text(json.dumps(data, indent=1), encoding="utf-8")
    before = run_json.read_bytes()

    manager_b, _, _ = make_manager(tmp_path, task_completed_hook=seen.append)
    assert only_status(manager_b).state == "completed"

    assert len(seen) == 1  # never announced again
    assert run_json.read_bytes() == before  # and never rewritten


def test_a_resumed_tail_replays_the_completion_but_not_the_display_only_work(tmp_path):
    """A push about a run that ended before the hub restarted is stale, not news.

    The notification and the usage refresh belong to the observed moment and fire
    there or not at all. The completion is replayed because it is the only step
    whose absence is permanent — a hole in a corpus whose point is having none.
    """
    repo = make_repo(tmp_path)
    notifier = RecordingNotifier()
    refreshed: list[int] = []
    manager, spawner, _ = make_manager(
        tmp_path,
        notifier=notifier,
        usage_refresh=lambda: refreshed.append(1),
        task_completed_hook=die,
    )
    manager.start_run(repo, TASK_ID)
    write_orch_log(repo, completing_log())

    end_run(spawner, 0)
    with pytest.raises(Interrupt):
        manager.list_runs()
    assert len(notifier.notifications) == 1  # both already fired, before the hook
    assert refreshed == [1]

    resumed_notifier = RecordingNotifier()
    seen: list[RunRecord] = []
    manager_b, _, _ = make_manager(
        tmp_path,
        notifier=resumed_notifier,
        usage_refresh=lambda: refreshed.append(2),
        task_completed_hook=seen.append,
    )
    manager_b.list_runs()

    assert len(seen) == 1
    assert resumed_notifier.notifications == []
    assert refreshed == [1]


def test_a_resumed_tail_keeps_the_sessions_snapshot_it_already_wrote(tmp_path):
    """Re-deriving would read a repo-side log that may not exist any more.

    :func:`read_live` reports an absent log as an *empty* snapshot rather than an
    error, so a blind re-derivation would replace a complete summary with nothing.
    """
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path, task_completed_hook=die)
    record = manager.start_run(repo, TASK_ID)
    log = write_orch_log(repo, completing_log())

    end_run(spawner, 0)
    with pytest.raises(Interrupt):
        manager.list_runs()
    run_dir = manager.runs_dir / record.run_id
    before = read_snapshot(run_dir)
    assert len(before.sessions) == 2

    log.unlink()  # the repo-side log is cleaned while the hub is down

    manager_b, _, _ = make_manager(tmp_path)
    manager_b.list_runs()

    assert read_snapshot(run_dir).sessions == before.sessions


def test_a_tail_interrupted_before_the_entries_settle_still_settles_them(
    tmp_path, monkeypatch
):
    """Settling reads the run's own final parse, not a log the outage removed.

    The hub wrote ``sessions.json``, then died before settling the entries that
    same parse accounts for, and the repo-side log was cleaned while it was down.
    A resumed tail re-reading *that* would get an empty parse — and settling is
    silent when it finds nothing to settle, so the mark would clear and the
    completion be announced carrying no session id for a session the run's own
    durable snapshot could still name.
    """
    repo = make_repo(tmp_path)
    manager, spawner, clock = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    run_dir = manager.runs_dir / record.run_id
    write_orch_log(repo, ORCH_LOG_LINES[:4])  # review session open, sid=(pending)
    clock.tick(11 * 60 + 30)
    manager.list_runs()  # the live scan records the entry; its sid is not knowable yet
    assert read_task_snapshots(run_dir).for_sid("rev1") is None

    log = write_orch_log(repo, ORCH_LOG_LINES)  # the end line reveals sid=rev1
    monkeypatch.setattr("orch_hub.runs.settle_snapshots", die)
    end_run(spawner, 0)
    with pytest.raises(Interrupt):
        manager.list_runs()
    assert len(read_snapshot(run_dir).sessions) == 2  # the parse is already durable
    assert read_task_snapshots(run_dir).for_sid("rev1") is None  # its entries are not

    monkeypatch.undo()
    log.unlink()  # the repo-side log is cleaned while the hub is down

    settled: list = []
    manager_b, _, _ = make_manager(
        tmp_path,
        task_completed_hook=lambda _record: settled.append(
            read_task_snapshots(run_dir)
        ),
    )
    assert only_status(manager_b).state == "completed"

    # Settled *before* the sidecar looked, which is the whole ordering contract:
    # an evidence pack built here can reach this session's captured entry file.
    assert len(settled) == 1
    entry = settled[0].for_sid("rev1")
    assert entry is not None and entry.index == 1
    # And the session no scan ever saw is accounted for in the same pass.
    assert [item.index for item in settled[0].snapshots] == [0, 1]


def test_a_tail_that_lost_its_snapshot_settles_from_the_runs_own_capture(
    tmp_path, monkeypatch
):
    """Interrupted one step earlier, the fallback is still run-owned.

    ``spawn.out`` is this run's stdout capture — written by the run, into its own
    dir, never touched again — and the orchestrator prints its whole marker
    stream to stdout as well as to the log. So a tail that died before
    ``sessions.json`` landed still has a durable parse once the repo-side log is
    gone, and settles from it rather than from nothing. Same run-owned rule
    :meth:`RunManager._classify_end` follows.
    """
    repo = make_repo(tmp_path)
    manager, spawner, clock = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    run_dir = manager.runs_dir / record.run_id
    (run_dir / "spawn.out").write_text(
        "".join(line + "\n" for line in ORCH_LOG_LINES), encoding="utf-8"
    )
    write_orch_log(repo, ORCH_LOG_LINES[:4])
    clock.tick(11 * 60 + 30)
    manager.list_runs()

    log = write_orch_log(repo, ORCH_LOG_LINES)
    monkeypatch.setattr("orch_hub.runs.write_snapshot", die)
    end_run(spawner, 0)
    with pytest.raises(Interrupt):
        manager.list_runs()
    assert read_snapshot(run_dir) is None  # nothing durable landed for this run

    monkeypatch.undo()
    log.unlink()

    seen: list[RunRecord] = []
    manager_b, _, _ = make_manager(tmp_path, task_completed_hook=seen.append)
    assert only_status(manager_b).state == "completed"

    assert len(read_snapshot(run_dir).sessions) == 2
    entry = read_task_snapshots(run_dir).for_sid("rev1")
    assert entry is not None and entry.index == 1
    assert [r.run_id for r in seen] == [record.run_id]


def test_a_tail_interrupted_before_the_protocol_check_resumes_it_uncompared(
    tmp_path, monkeypatch
):
    """A resumed first look is the same unbounded lag as an end observed late.

    The hub saw this run exit, then stopped before comparing the deployment. What
    it finds now cannot separate a mid-run redeployment from any since, so the
    run records that its end could not be compared rather than a verdict it
    cannot support — and still hands over its completion.
    """
    repo = make_repo(tmp_path)
    write_deploy_lock(repo)
    seen: list[RunRecord] = []
    manager, spawner, _ = make_manager(tmp_path, task_completed_hook=seen.append)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, completing_log())
    # Die inside the tail, before the protocol check rather than after it.
    monkeypatch.setattr("orch_hub.runs.settle_snapshots", die)

    end_run(spawner, 0)
    with pytest.raises(Interrupt):
        manager.list_runs()
    run_dir = manager.runs_dir / record.run_id
    assert read_protocol(run_dir).checked_ts is None  # never looked
    assert seen == []  # never announced

    monkeypatch.undo()
    manager_b, _, _ = make_manager(tmp_path, task_completed_hook=seen.append)
    manager_b.list_runs()

    snapshot = read_protocol(run_dir)
    assert snapshot.checked_ts is not None  # the one look this run gets
    assert snapshot.stability == STABILITY_UNVERIFIED
    assert snapshot.verified_revision is None  # publishes nothing, truthfully
    assert [r.run_id for r in seen] == [record.run_id]


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


def test_a_session_that_began_and_ended_between_scans_is_still_recorded(tmp_path):
    # The monitor polls every 30s, so a whole short session can fall inside one
    # gap — and a later open bracket must not make that skip permanent. Its bytes
    # are gone, but the session is accounted for: an absent entry is
    # indistinguishable downstream from a session that never ran.
    repo = make_repo(tmp_path)
    manager, _, clock = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:4])  # dev opened *and closed*, review open
    clock.tick(11 * 60 + 30)  # 03:11:30, i.e. 30s into the review session

    manager.list_runs()

    kept = read_task_snapshots(manager.runs_dir / record.run_id)
    assert [(e.index, e.role, e.present) for e in kept.snapshots] == [
        (0, "dev", False),
        (1, "review", True),
    ]
    assert "moved on" in kept.snapshots[0].detail
    assert kept.snapshots[0].sid == "dev1"  # still joinable to its session summary


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


def test_an_ended_run_records_its_sessions_without_capturing_them(tmp_path):
    # A terminal run's file is that run's output, never a session's entry state —
    # so no bytes. The end transition is the last thing that runs for this run,
    # so it is where a never-observed session is recorded, or nowhere.
    repo = make_repo(tmp_path)
    manager, spawner, clock = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_orch_log(repo, ORCH_LOG_LINES[:1])
    end_run(spawner, 0)
    clock.tick(30)

    manager.list_runs()

    kept = read_task_snapshots(manager.runs_dir / record.run_id)
    assert [(e.index, e.sid, e.present, e.content) for e in kept.snapshots] == [
        (0, "dev1", False, None)
    ]
    assert "never observed" in kept.snapshots[0].detail


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

    monkeypatch.setattr("orch_hub.runs.capture_entries", boom)
    statuses = manager.list_runs()  # must not raise

    assert [status.state for status in statuses] == ["active"]
    assert not task_snapshots_json(manager, record.run_id).exists()


# -- effective-config launch snapshot (effective_config.json) ------------------


EFFECTIVE_CONFIG = {
    "schema_version": 3,
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

    assert status.state == "completed"  # from the run's own session evidence
    assert read_record(manager, record.run_id)["ended_ts"] is not None
    assert not effective_config_json(manager, record.run_id).exists()


# -- protocol provenance (protocol.json) --------------------------------------


COMMIT = "6b62754694c289fc8822562cec22ab774e071b2c"
OTHER_COMMIT = "aa11bb22cc33dd44ee55ff6677889900aabbccdd"
PAYLOAD = "533a87d311bd99d9ae9ba25f1385a3c22396b4e704a8b75a2ad9b9b36614f450"


DEPLOYED_FILE = ".ai-protocol/protocols/conduct.md"


def write_deploy_lock(
    repo: ManagedRepo, *, commit: str = COMMIT, body: bytes = b"# Conduct\n"
) -> None:
    """Deploy a protocol into the fake repo: both receipts and the file they name.

    One helper because a real deploy writes them together — the lock names the
    identity and the payload's file set, the manifest vouches for the bytes, and
    the end check needs both to agree. Re-calling it with another commit is a
    redeployment.
    """
    (repo.path / DEPLOY_LOCK_NAME).write_text(
        json.dumps(
            {
                "canonical_payload_sha256": PAYLOAD,
                "deployed_files": [
                    {
                        "canonical_sha256": "0" * 64,
                        "target_relative_path": DEPLOYED_FILE,
                    }
                ],
                "schema_version": 3,
                "source_git_commit": commit,
                "source_repo": "mandrel",
            }
        ),
        encoding="utf-8",
    )
    deployed = repo.path / DEPLOYED_FILE
    deployed.parent.mkdir(parents=True, exist_ok=True)
    deployed.write_bytes(body)
    (repo.path / DEPLOY_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "files": {
                    DEPLOYED_FILE: {
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "size_bytes": len(body),
                        "target_relative_path": DEPLOYED_FILE,
                    }
                },
                "schema_version": 3,
                "source_git_commit": commit,
            }
        ),
        encoding="utf-8",
    )


def test_start_run_captures_the_deployed_protocol_before_spawning(tmp_path):
    repo = make_repo(tmp_path)
    write_deploy_lock(repo)
    manager, spawner, clock = make_manager(tmp_path)

    record = manager.start_run(repo, TASK_ID)

    snapshot = read_protocol(manager.runs_dir / record.run_id)
    assert snapshot is not None
    assert snapshot.source_git_commit == COMMIT
    assert snapshot.captured_ts == clock.now.isoformat()
    # A capture is not yet a publishable revision: this run is live, and its end
    # check is what confirms the deployment held for all of its sessions.
    assert snapshot.verified_revision is None
    # Captured before the child exists: the run's own sessions load exactly this
    # payload, and nothing later in the run dir can change what it names.
    assert spawner.calls[0]["stdout_path"].parent == manager.runs_dir / record.run_id


def test_a_missing_deployment_lock_records_a_reason_and_still_launches(tmp_path):
    # Provenance is evidence, not a gate: readiness owns the fail-closed
    # deployment checks, so an unrecorded revision costs the report a cohort key,
    # never the run.
    repo = make_repo(tmp_path)
    manager, spawner, _ = make_manager(tmp_path)

    record = manager.start_run(repo, TASK_ID)

    assert len(spawner.calls) == 1
    snapshot = read_protocol(manager.runs_dir / record.run_id)
    assert snapshot is not None
    assert snapshot.available is False
    assert snapshot.verified_revision is None
    assert snapshot.detail


def test_a_failed_capture_write_refuses_the_launch_and_cleans_up(tmp_path, monkeypatch):
    # The write lands in a run dir this call just created; one that cannot take it
    # cannot take run.json either, so it is handled like a failed spawn rather
    # than leaving a launched run with no provenance.
    repo = make_repo(tmp_path)
    write_deploy_lock(repo)
    manager, spawner, _ = make_manager(tmp_path)

    def boom(_run_dir, _repo_path, *, now):
        raise OSError("disk gone")

    monkeypatch.setattr("orch_hub.runs.capture_protocol", boom)

    with pytest.raises(OSError):
        manager.start_run(repo, TASK_ID)

    assert spawner.calls == []
    assert manager.list_runs() == []
    assert not manager.runs_dir.exists() or list(manager.runs_dir.iterdir()) == []


def test_run_end_records_a_stable_deployment(tmp_path):
    repo = make_repo(tmp_path)
    write_deploy_lock(repo)
    manager, spawner, clock = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    end_run(spawner, 0)
    clock.tick(3600)
    manager.list_runs()  # observe the end -> comparison runs once

    snapshot = read_protocol(manager.runs_dir / record.run_id)
    assert snapshot is not None
    assert snapshot.stability == STABILITY_STABLE
    assert snapshot.verified_revision == COMMIT


def test_a_redeploy_during_the_run_degrades_the_capture(tmp_path):
    repo = make_repo(tmp_path)
    write_deploy_lock(repo)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_deploy_lock(repo, commit=OTHER_COMMIT)  # mandrel deploy mid-run

    end_run(spawner, 0)
    manager.list_runs()

    snapshot = read_protocol(manager.runs_dir / record.run_id)
    assert snapshot is not None
    assert snapshot.stability == STABILITY_CHANGED
    assert snapshot.source_git_commit == COMMIT  # still auditable
    assert snapshot.verified_revision is None  # but not publishable


def test_an_edited_protocol_file_degrades_the_capture(tmp_path):
    # The receipts never move — no deploy ran — but the run's later sessions read
    # protocol text the captured identity does not name, so it publishes nothing.
    repo = make_repo(tmp_path)
    write_deploy_lock(repo)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    (repo.path / DEPLOYED_FILE).write_bytes(b"# Conduct\n\nedited mid-run\n")

    end_run(spawner, 0)
    manager.list_runs()

    snapshot = read_protocol(manager.runs_dir / record.run_id)
    assert snapshot is not None
    assert snapshot.stability == STABILITY_DRIFTED
    assert DEPLOYED_FILE in (snapshot.detail or "")
    assert snapshot.source_git_commit == COMMIT  # still auditable
    assert snapshot.verified_revision is None  # but not publishable


def test_a_redeploy_after_the_run_leaves_the_verdict_alone(tmp_path):
    repo = make_repo(tmp_path)
    write_deploy_lock(repo)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    end_run(spawner, 0)
    manager.list_runs()  # the one comparison this run gets
    write_deploy_lock(repo, commit=OTHER_COMMIT)
    manager.list_runs()  # a settled record short-circuits; nothing re-compares

    snapshot = read_protocol(manager.runs_dir / record.run_id)
    assert snapshot is not None
    assert snapshot.stability == STABILITY_STABLE
    assert snapshot.verified_revision == COMMIT


def test_a_run_ended_while_the_hub_was_down_is_not_compared(tmp_path):
    # A fresh manager over the same data dir observes the end at an unbounded lag
    # (exit_code stays None), so a differing deployment cannot be attributed to
    # the run — it records that it could not look, not a false 'changed'.
    repo = make_repo(tmp_path)
    write_deploy_lock(repo)
    manager, _, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    write_deploy_lock(repo, commit=OTHER_COMMIT)  # redeployed after the hub died

    manager_b, _, _ = make_manager(tmp_path)  # pid_probe returns False (dead)
    status = manager_b.get_run(record.run_id)

    assert status.record.exit_code is None
    snapshot = read_protocol(manager.runs_dir / record.run_id)
    assert snapshot is not None
    assert snapshot.stability == STABILITY_UNVERIFIED
    assert snapshot.checked_ts is not None  # the one look this run gets
    assert snapshot.source_git_commit == COMMIT  # auditable
    assert snapshot.verified_revision is None  # but never confirmed


def test_protocol_settle_failure_does_not_corrupt_run_end(tmp_path, monkeypatch):
    # Isolated like the other end-transition snapshots: the capture is already
    # durable, so a failed comparison costs a verdict, not run state.
    repo = make_repo(tmp_path)
    write_deploy_lock(repo)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)

    def boom(_run_dir, _repo_path, *, now, end_observed):
        raise OSError("disk gone")

    monkeypatch.setattr("orch_hub.runs.settle_protocol", boom)
    end_run(spawner, 0)
    status = only_status(manager)  # must not raise

    assert status.state == "ended"
    assert read_record(manager, record.run_id)["ended_ts"] is not None
    snapshot = read_protocol(manager.runs_dir / record.run_id)
    assert snapshot is not None
    assert snapshot.stability == STABILITY_UNVERIFIED


def test_a_run_predating_the_capture_still_ends_cleanly(tmp_path):
    # Legacy compatibility: a run dir with no protocol.json settles to nothing
    # rather than adopting the repo's current lock.
    repo = make_repo(tmp_path)
    write_deploy_lock(repo)
    manager, spawner, _ = make_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    (manager.runs_dir / record.run_id / "protocol.json").unlink()

    end_run(spawner, 0)
    status = only_status(manager)

    assert status.state == "ended"
    assert read_protocol(manager.runs_dir / record.run_id) is None


# -- load_records (read-only historical reader) -------------------------------


def test_load_records_reads_persisted_runs_without_mutating(tmp_path):
    # load_records is the read-only entry point for evidence aggregation: unlike
    # list_runs it must not refresh, so a dead pid does NOT flip the run to ended
    # or append a run_ended event.
    repo = make_repo(tmp_path)
    manager, _, _ = make_manager(tmp_path)  # pid_probe returns False (dead)
    record = manager.start_run(repo, TASK_ID)
    config = Config(mandrel_path=tmp_path / "mandrel", data_dir=tmp_path / "hub-data")
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
    config = Config(mandrel_path=tmp_path / "mandrel", data_dir=tmp_path / "nowhere")
    assert load_records(config) == []


def test_load_records_surfaces_broken_dir_as_error_stub(tmp_path):
    config = Config(mandrel_path=tmp_path / "mandrel", data_dir=tmp_path / "hub-data")
    broken = config.data_dir / "runs" / "20260708T030000Z-broken"
    broken.mkdir(parents=True)  # no run.json

    pairs = load_records(config)

    assert len(pairs) == 1
    run_dir, record = pairs[0]
    assert run_dir == broken
    assert record.error is not None
    assert record.repo_path is None  # unattributable, so it never matches a repo
