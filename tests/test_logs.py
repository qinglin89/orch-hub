"""Tests for orchestrator-log location and recent-tail reading.

All tests build fake log dirs in ``tmp_path`` and construct ``RunRecord`` values
directly — no subprocess, no orchestrator, no real repo. The filename stamp
format (``%Y%m%dT%H%M%SZ``) and the ``<utc-ts>-<task-id>.log`` shape mirror the
deployed orchestrator's own ``self.log_file`` construction.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from orch_hub import logs
from orch_hub.logs import MAX_TAIL_LINES, LogSnapshot, locate_log, read_recent
from orch_hub.models import RunRecord

TASK_ID = "2026-07-05-feature-x"
# The run started at exactly 03:00:00Z; its own log stamp is the same second.
STARTED = dt.datetime(2026, 7, 8, 3, 0, 0, tzinfo=dt.timezone.utc)
STARTED_ISO = STARTED.isoformat()
RUN_STAMP = "20260708T030000Z"


def make_record(
    log_dir: Path | None,
    *,
    task_id: str = TASK_ID,
    started_ts: str | None = STARTED_ISO,
) -> RunRecord:
    return RunRecord(
        run_id="20260708T030000Z-repo-a-task",
        repo_name="repo-a",
        repo_path=Path("/repos/repo-a"),
        task_id=task_id,
        log_dir=log_dir,
        started_ts=started_ts,
    )


def write_log(
    log_dir: Path, stamp: str, lines: list[str], *, task_id: str = TASK_ID
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{stamp}-{task_id}.log"
    body = "".join(line + "\n" for line in lines)
    path.write_text(body, encoding="utf-8")
    return path


# -- locate_log ---------------------------------------------------------------


def test_locate_none_when_log_dir_is_none():
    assert locate_log(make_record(None)) is None


def test_locate_none_when_log_dir_missing(tmp_path):
    # Path exists as a record field but the dir was never created.
    assert locate_log(make_record(tmp_path / "logs")) is None


def test_locate_none_when_no_task_id(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, RUN_STAMP, ["x"])
    assert locate_log(make_record(logdir, task_id="")) is None


def test_locate_single_matching_file(tmp_path):
    logdir = tmp_path / "logs"
    expected = write_log(logdir, "20260708T030001Z", ["run started"])
    assert locate_log(make_record(logdir)) == expected


def test_locate_ignores_other_task_and_non_log_files(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, RUN_STAMP, ["other"], task_id="2026-07-05-other-task")
    (logdir / "notes.txt").write_text("nope", encoding="utf-8")
    (logdir / f"not-a-stamp-{TASK_ID}.log").write_text("bad", encoding="utf-8")
    mine = write_log(logdir, RUN_STAMP, ["mine"])
    assert locate_log(make_record(logdir)) == mine


def test_locate_excludes_files_before_run_start(tmp_path):
    logdir = tmp_path / "logs"
    # An earlier run of the same task, stamped well before this run started.
    write_log(logdir, "20260708T025000Z", ["earlier run"])
    mine = write_log(logdir, RUN_STAMP, ["this run"])
    assert locate_log(make_record(logdir)) == mine


def test_locate_attributes_each_rerun_to_its_own_log(tmp_path):
    logdir = tmp_path / "logs"
    first = write_log(logdir, "20260708T030000Z", ["dev 1"])
    second = write_log(logdir, "20260708T033000Z", ["review"])
    third = write_log(logdir, "20260708T040000Z", ["dev 2"])

    # A run that started at 03:00 owns the earliest at-or-after-start file...
    early = make_record(logdir, started_ts="2026-07-08T03:00:00+00:00")
    assert locate_log(early) == first
    # ...one that started at 03:30 skips the 03:00 file (predates it)...
    mid = make_record(logdir, started_ts="2026-07-08T03:30:00+00:00")
    assert locate_log(mid) == second
    # ...and the latest run owns the newest file.
    late = make_record(logdir, started_ts="2026-07-08T04:00:00+00:00")
    assert locate_log(late) == third


def test_locate_tolerates_subsecond_start_floor(tmp_path):
    logdir = tmp_path / "logs"
    mine = write_log(logdir, RUN_STAMP, ["x"])  # stamped 03:00:00 (second)
    # The hub stamped started_ts half a second later; flooring must keep the
    # same-second log in range rather than excluding it as "before start".
    rec = make_record(logdir, started_ts="2026-07-08T03:00:00.500000+00:00")
    assert locate_log(rec) == mine


def test_locate_without_start_returns_newest(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, "20260708T030000Z", ["old"])
    newest = write_log(logdir, "20260708T040000Z", ["new"])
    # A broken record with no started_ts drops the lower bound and best-effort
    # returns the most recent log for the task.
    assert locate_log(make_record(logdir, started_ts=None)) == newest


# -- read_recent --------------------------------------------------------------


def test_read_recent_missing_is_graceful(tmp_path):
    snap = read_recent(make_record(tmp_path / "logs"))
    assert snap.present is False
    assert snap.lines == ()
    assert "no log file" in snap.detail


def test_read_recent_returns_all_lines_for_small_file(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, RUN_STAMP, ["one", "two", "three"])
    snap = read_recent(make_record(logdir))
    assert snap.present is True
    assert snap.lines == ("one", "two", "three")
    assert snap.truncated is False
    assert snap.size > 0


def test_read_recent_tail_limits_lines(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, RUN_STAMP, [f"line-{i}" for i in range(10)])
    snap = read_recent(make_record(logdir), max_lines=3)
    assert snap.lines == ("line-7", "line-8", "line-9")
    assert snap.truncated is True


def test_read_recent_byte_window_drops_partial_head(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, RUN_STAMP, ["aaaa", "bbbb", "cccc"])  # 15 bytes total
    # An 8-byte window starts mid-file, so the partial leading line is dropped
    # and the result is flagged truncated.
    snap = read_recent(make_record(logdir), max_bytes=8)
    assert snap.present is True
    assert snap.lines == ("cccc",)
    assert snap.truncated is True


def test_read_recent_clamps_max_lines(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, RUN_STAMP, ["only", "two"])
    # Below 1 clamps up to 1 (the single most recent line)...
    assert read_recent(make_record(logdir), max_lines=0).lines == ("two",)
    # ...and an absurd request clamps down to the cap without erroring.
    snap = read_recent(make_record(logdir), max_lines=MAX_TAIL_LINES + 5)
    assert snap.lines == ("only", "two")
    assert snap.truncated is False


def test_read_recent_empty_file_is_present_but_blank(tmp_path):
    logdir = tmp_path / "logs"
    (logdir).mkdir(parents=True)
    (logdir / f"{RUN_STAMP}-{TASK_ID}.log").write_text("", encoding="utf-8")
    snap = read_recent(make_record(logdir))
    assert snap.present is True
    assert snap.lines == ()
    assert snap.size == 0


def test_read_recent_located_then_removed(tmp_path, monkeypatch):
    # locate_log finds a path, but the file is gone by the time we open it.
    ghost = tmp_path / "logs" / f"{RUN_STAMP}-{TASK_ID}.log"
    monkeypatch.setattr(logs, "locate_log", lambda record: ghost)
    snap = read_recent(make_record(tmp_path / "logs"))
    assert snap.present is False
    assert "rotated or removed" in snap.detail


def test_snapshot_to_dict_excludes_path(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, RUN_STAMP, ["hello"])
    snap = read_recent(make_record(logdir))
    payload = snap.to_dict()
    assert payload == {
        "present": True,
        "lines": ["hello"],
        "truncated": False,
        "size": snap.size,
        "detail": None,
    }
    assert "path" not in payload
    assert isinstance(snap.path, Path)  # kept on the object for internal use


def test_snapshot_defaults_are_absent():
    snap = LogSnapshot(present=False)
    assert snap.to_dict() == {
        "present": False,
        "lines": [],
        "truncated": False,
        "size": 0,
        "detail": None,
    }
