"""Tests for the resident background run monitor.

The monitor's whole job is to call ``RunManager.list_runs()`` on an interval so
notifications fire without dashboard/API traffic. Cadence, lifecycle,
idempotency, and failure-isolation are tested against a fake scanner (no real
run state needed); two integration tests drive a real :class:`RunManager` to
prove the existing notification path — including its per-``(run_id, seq)``
de-dup — is reused end-to-end.

Threaded assertions are made deterministic with ``Event`` pulses and bounded
polling, never fixed sleeps that assume timing, so the suite stays fast and
non-flaky. Every test that starts a monitor stops it in ``finally`` so no
daemon thread leaks into the next test.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import threading
import time
from pathlib import Path

from orch_hub.config import Config
from orch_hub.models import ManagedRepo, ReadinessCheck, ReadinessReport
from orch_hub.monitor import RunMonitor
from orch_hub.runs import RunManager

# -- fake scanner (lifecycle / cadence / isolation) ----------------------------


class RecordingScanner:
    """Counts ``list_runs`` calls and pulses an event on each for exact waits."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = 0
        self.pinged = threading.Event()

    def list_runs(self) -> list:
        with self._lock:
            self.calls += 1
        self.pinged.set()
        return []


def wait_for(predicate, timeout: float = 1.0) -> bool:
    """Poll ``predicate`` until true or ``timeout`` (wall clock) elapses."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_monitor_scans_periodically():
    # The background thread calls list_runs() repeatedly, not once: two pulses
    # prove the loop keeps going rather than firing a single scan.
    scanner = RecordingScanner()
    monitor = RunMonitor(scanner, interval=0.01)
    monitor.start()
    try:
        assert scanner.pinged.wait(1.0)
        scanner.pinged.clear()
        assert scanner.pinged.wait(1.0)
    finally:
        monitor.stop()
    assert scanner.calls >= 2


def test_start_is_idempotent_no_duplicate_workers():
    # Double start() must not spawn a second worker (the "avoid duplicate
    # workers" guarantee the app lifespan relies on).
    scanner = RecordingScanner()
    monitor = RunMonitor(scanner, interval=0.01)
    monitor.start()
    monitor.start()
    try:
        assert scanner.pinged.wait(1.0)
        workers = [t for t in threading.enumerate() if t.name == "orch-hub-monitor"]
        assert len(workers) == 1
    finally:
        monitor.stop()


def test_stop_halts_scanning_and_joins_the_worker():
    scanner = RecordingScanner()
    monitor = RunMonitor(scanner, interval=0.01)
    monitor.start()
    assert scanner.pinged.wait(1.0)

    monitor.stop()
    assert not monitor.is_running  # stop() joined the worker before returning
    settled = scanner.calls
    # A stray loop iteration would show up within many interval windows; the
    # join guarantees none can, so the count stays put.
    time.sleep(0.05)
    assert scanner.calls == settled
    assert "orch-hub-monitor" not in {t.name for t in threading.enumerate()}


def test_stop_before_start_is_a_safe_no_op():
    monitor = RunMonitor(RecordingScanner(), interval=0.01)
    monitor.stop()  # must not raise
    assert not monitor.is_running


def test_scan_failure_is_isolated_and_the_loop_survives():
    # A scan that raises must not kill the thread: a later scan still runs.
    class FlakyScanner:
        def __init__(self):
            self.calls = 0
            self.recovered = threading.Event()

        def list_runs(self) -> list:
            self.calls += 1
            if self.calls == 1:
                raise OSError("transient scan failure")
            self.recovered.set()
            return []

    scanner = FlakyScanner()
    monitor = RunMonitor(scanner, interval=0.01)
    monitor.start()
    try:
        assert scanner.recovered.wait(1.0)  # a scan after the failure ran
    finally:
        monitor.stop()
    assert scanner.calls >= 2


# -- integration over a real RunManager (notification path is reused) ----------

FIXED_NOW = dt.datetime(2026, 7, 8, 3, 0, 0, tzinfo=dt.timezone.utc)
TASK_ID = "2026-07-05-feature-x"


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


class RecordingNotifier:
    def __init__(self):
        self.notifications: list = []

    def notify(self, notification) -> None:
        self.notifications.append(notification)


def make_real_manager(tmp_path: Path):
    """A real RunManager over fake repo/subprocess plumbing with a notifier.

    Notification dispatch is synchronous so a scan delivers inline; that keeps
    the "fired exactly once" assertions deterministic while still exercising the
    manager's real trigger + de-dup path (mirrors test_runs' make_manager).
    """
    repo_path = tmp_path / "repos" / "repo-a"
    (repo_path / ".ai-tasks").mkdir(parents=True)
    (repo_path / ".ai-tasks" / "index.md").write_text(
        "# Active tasks\n\n| Task | Status | Summary |\n|---|---|---|\n"
        f"| {TASK_ID} | x | Summary |\n",
        encoding="utf-8",
    )
    (repo_path / ".ai-tasks" / f"{TASK_ID}.md").write_text(
        f"---\nid: {TASK_ID}\nstatus: in_progress\n---\n\n# Task\n", encoding="utf-8"
    )
    repo = ManagedRepo(
        name="repo-a", path=repo_path, manifest=repo_path / ".ai-deploy-manifest.json"
    )
    spawner = FakeSpawner()
    notifier = RecordingNotifier()
    manager = RunManager(
        Config(ai_native_path=tmp_path / "ai-native", data_dir=tmp_path / "hub-data"),
        readiness_checker=lambda r, c: ReadinessReport(
            (ReadinessCheck("all", True, "ok"),)
        ),
        spawner=spawner,
        pid_probe=lambda pid: False,
        clock=lambda: FIXED_NOW,
        notifier=notifier,
        notify_dispatch=lambda deliver: deliver(),
    )
    return manager, spawner, notifier, repo


def test_monitor_drives_run_end_notification_without_api_traffic(tmp_path):
    # The acceptance case: a run ends while nothing polls the API, and the
    # background monitor alone drives the terminal push.
    manager, spawner, notifier, repo = make_real_manager(tmp_path)
    manager.start_run(repo, TASK_ID)
    spawner.procs[0].returncode = 0  # the run ends; no one calls list_runs()

    monitor = RunMonitor(manager, interval=0.01)
    monitor.start()
    try:
        assert wait_for(lambda: len(notifier.notifications) >= 1)
    finally:
        monitor.stop()

    assert len(notifier.notifications) == 1
    # exit 0 with no completion/stop evidence classifies as a plain "ended".
    assert notifier.notifications[0].title == "repo-a: run ended"


def test_monitor_reuses_pending_question_dedup_semantics(tmp_path):
    # A pending question appears with no API traffic. The monitor scans many
    # times, but the manager's per-(run_id, seq) de-dup means exactly one push —
    # the monitor drives the existing scan, it does not bypass the de-dup.
    manager, spawner, notifier, repo = make_real_manager(tmp_path)
    record = manager.start_run(repo, TASK_ID)
    (record.control_dir / "001-question.json").write_text(
        json.dumps({"seq": 1, "banner": "Pick A or B?"}), encoding="utf-8"
    )

    monitor = RunMonitor(manager, interval=0.01)
    monitor.start()
    try:
        assert wait_for(lambda: len(notifier.notifications) >= 1)
        # Give the loop room for several more scans over the still-open question.
        time.sleep(0.05)
    finally:
        monitor.stop()

    assert len(notifier.notifications) == 1
    assert notifier.notifications[0].title == "repo-a: needs input"
    assert "Pick A or B?" in notifier.notifications[0].body
