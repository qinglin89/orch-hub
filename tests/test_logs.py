"""Tests for orchestrator-log location and recent-tail reading.

All tests build fake log dirs in ``tmp_path`` and construct ``RunRecord`` values
directly — no subprocess, no orchestrator, no real repo. The filename stamp
format (``%Y%m%dT%H%M%SZ``) and the ``<utc-ts>-<task-id>.log`` shape mirror the
deployed orchestrator's own ``self.log_file`` construction.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

from orch_hub import logs
from orch_hub.logs import (
    MAX_TAIL_LINES,
    LogSnapshot,
    locate_log,
    read_recent,
    read_spawn_out,
)
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


def test_locate_fast_rerun_one_second_apart_attributes_later_log(tmp_path):
    logdir = tmp_path / "logs"
    # A fast --once/error/retry re-run: an earlier run stamped 03:00:00 and this
    # run stamped 03:00:01, only one second later. Per-repo serialization blocks
    # concurrent *live* runs but enforces no time gap after one ends, so this is
    # reachable. This run started at 03:00:01, so its own log is the 03:00:01
    # file; any pre-start slack would re-admit the 03:00:00 file and, as the
    # earliest match, mis-attribute the earlier run's log to this run.
    write_log(logdir, "20260708T030000Z", ["earlier run"])
    mine = write_log(logdir, "20260708T030001Z", ["this run"])
    rec = make_record(logdir, started_ts="2026-07-08T03:00:01+00:00")
    assert locate_log(rec) == mine


def test_locate_tolerates_subsecond_start_floor(tmp_path):
    logdir = tmp_path / "logs"
    mine = write_log(logdir, RUN_STAMP, ["x"])  # stamped 03:00:00 (second)
    # The hub stamped started_ts half a second later; flooring must keep the
    # same-second log in range rather than excluding it as "before start".
    rec = make_record(logdir, started_ts="2026-07-08T03:00:00.500000+00:00")
    assert locate_log(rec) == mine


def test_locate_finds_log_stamped_in_next_second_after_start(tmp_path):
    logdir = tmp_path / "logs"
    # The hub stamps started_ts just before spawning; the child then picks its
    # own second-precision log filename during startup, which lands in the
    # *next* second when the launch crosses a boundary. floor(started_ts) is
    # still a valid lower bound (<= the child stamp), so the run's own log must
    # be found rather than hidden as "no log yet".
    mine = write_log(logdir, "20260708T030001Z", ["this run"])
    rec = make_record(logdir, started_ts="2026-07-08T03:00:00.900000+00:00")
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


# -- read_spawn_out (startup-failure diagnostic) ------------------------------
#
# spawn.out is the subprocess stdout+stderr capture. It is the only evidence of
# a run that died before the orchestrator opened its own log, so the logs route
# falls back to it when locate_log finds nothing. read_spawn_out reads the path
# straight off the record (no locate step), and treats an empty capture — the
# normal case for a run that logged cleanly — as "nothing to show".


def spawn_record(spawn_out: Path | None) -> RunRecord:
    return RunRecord(run_id="20260708T030000Z-repo-a-task", spawn_out=spawn_out)


def write_spawn_out(run_dir: Path, text: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "spawn.out"
    path.write_text(text, encoding="utf-8")
    return path


def test_read_spawn_out_none_path_is_absent():
    snap = read_spawn_out(spawn_record(None))
    assert snap.present is False
    assert snap.lines == ()
    assert "no startup diagnostic" in snap.detail


def test_read_spawn_out_missing_file_is_absent(tmp_path):
    # The record has a path but nothing was ever written there.
    snap = read_spawn_out(spawn_record(tmp_path / "run" / "spawn.out"))
    assert snap.present is False
    assert "no startup diagnostic" in snap.detail


def test_read_spawn_out_empty_capture_is_absent(tmp_path):
    # A run whose orchestrator logged normally leaves spawn.out empty: there is
    # no useful diagnostic, so it reads as absent (not a blank "present" view).
    path = write_spawn_out(tmp_path / "run", "")
    snap = read_spawn_out(spawn_record(path))
    assert snap.present is False
    assert "empty" in snap.detail


def test_read_spawn_out_blank_lines_only_is_absent(tmp_path):
    path = write_spawn_out(tmp_path / "run", "\n\n")
    snap = read_spawn_out(spawn_record(path))
    assert snap.present is False
    assert "empty" in snap.detail


def test_read_spawn_out_returns_the_startup_error_tail(tmp_path):
    # A credential/PATH/import failure: the traceback is all we have.
    path = write_spawn_out(
        tmp_path / "run",
        "Traceback (most recent call last):\n  File ...\nRuntimeError: no cc-codex on PATH\n",
    )
    snap = read_spawn_out(spawn_record(path))
    assert snap.present is True
    assert snap.lines[0] == "Traceback (most recent call last):"
    assert snap.lines[-1] == "RuntimeError: no cc-codex on PATH"
    assert snap.size > 0


def test_read_spawn_out_tail_limits_lines(tmp_path):
    path = write_spawn_out(tmp_path / "run", "".join(f"line-{i}\n" for i in range(10)))
    snap = read_spawn_out(spawn_record(path), max_lines=3)
    assert snap.lines == ("line-7", "line-8", "line-9")
    assert snap.truncated is True


# -- tail_events (SSE) --------------------------------------------------------
#
# The async tail generator is driven to completion on a private event loop with
# an injected ``sleep`` (so no real time passes) and an injected ``is_live``
# probe (so the test decides when the run ends). ``sleep`` doubles as the hook
# that mutates the log file *between* polls — appending, creating, or removing
# it — which deterministically simulates a growing/rotating live log.


async def _collect(agen) -> list[str]:
    return [frame async for frame in agen]


def drive(agen) -> list[str]:
    """Run a tail generator to completion and return the emitted SSE frames."""
    return asyncio.run(_collect(agen))


async def _instant(_seconds) -> None:
    """A sleep that never actually waits."""
    return None


def data_frames(frames: list[str]) -> list[str]:
    """Just the ``data:`` line events (drops heartbeats and the end event)."""
    return [f for f in frames if f.startswith("data: ")]


def test_tail_ended_run_emits_existing_lines_then_end(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, RUN_STAMP, ["run started", "dev turn", "done"])
    frames = drive(
        logs.tail_events(make_record(logdir), is_live=lambda: False, sleep=_instant)
    )
    assert frames == [
        "data: run started\n\n",
        "data: dev turn\n\n",
        "data: done\n\n",
        "event: end\ndata: run ended\n\n",
    ]


def test_tail_streams_appended_lines(tmp_path):
    logdir = tmp_path / "logs"
    path = write_log(logdir, RUN_STAMP, ["start"])
    batches = [["line-2", "line-3"], ["line-4"]]

    async def sleep(_seconds):
        # Each poll wait appends the next batch, so the follow phase sees them.
        if batches:
            with path.open("a", encoding="utf-8") as handle:
                for line in batches.pop(0):
                    handle.write(line + "\n")

    frames = drive(
        logs.tail_events(
            make_record(logdir), is_live=lambda: bool(batches), sleep=sleep
        )
    )
    assert data_frames(frames) == [
        "data: start\n\n",
        "data: line-2\n\n",
        "data: line-3\n\n",
        "data: line-4\n\n",
    ]
    assert frames[-1] == "event: end\ndata: run ended\n\n"


def test_tail_waits_for_missing_log_then_streams(tmp_path):
    logdir = tmp_path / "logs"
    logdir.mkdir(parents=True)
    state = {"created": False}

    async def sleep(_seconds):
        if not state["created"]:
            write_log(logdir, RUN_STAMP, ["first line"])
            state["created"] = True

    frames = drive(
        logs.tail_events(
            make_record(logdir), is_live=lambda: not state["created"], sleep=sleep
        )
    )
    # Heartbeat while the file is absent, then the line once it appears, then end.
    assert frames[0] == ": keep-alive\n\n"
    assert "data: first line\n\n" in frames
    assert frames[-1].startswith("event: end")


def test_tail_run_ends_without_log_emits_end(tmp_path):
    logdir = tmp_path / "logs"
    logdir.mkdir(parents=True)
    frames = drive(
        logs.tail_events(make_record(logdir), is_live=lambda: False, sleep=_instant)
    )
    assert frames == ["event: end\ndata: no log file was written for this run\n\n"]


def test_tail_emits_heartbeat_while_idle(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, RUN_STAMP, ["only line"])
    checks = {"n": 0}

    def is_live():
        checks["n"] += 1
        return checks["n"] <= 1  # live for one idle poll, then ended

    frames = drive(
        logs.tail_events(
            make_record(logdir),
            is_live=is_live,
            sleep=_instant,
            poll_interval=1.0,
            heartbeat_seconds=1.0,
        )
    )
    assert "data: only line\n\n" in frames
    assert ": keep-alive\n\n" in frames
    assert frames[-1].startswith("event: end")


def test_tail_rotation_ends_gracefully(tmp_path):
    logdir = tmp_path / "logs"
    path = write_log(logdir, RUN_STAMP, ["line one"])

    async def sleep(_seconds):
        if path.exists():
            path.unlink()  # the located file rotates away between polls

    frames = drive(
        logs.tail_events(make_record(logdir), is_live=lambda: True, sleep=sleep)
    )
    assert "data: line one\n\n" in frames
    assert frames[-1] == "event: end\ndata: log file rotated or removed\n\n"


def test_tail_initial_lines_bounds_context(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, RUN_STAMP, [f"line-{i}" for i in range(5)])
    frames = drive(
        logs.tail_events(
            make_record(logdir),
            is_live=lambda: False,
            initial_lines=2,
            sleep=_instant,
        )
    )
    assert data_frames(frames) == ["data: line-3\n\n", "data: line-4\n\n"]
    assert frames[-1].startswith("event: end")


def test_tail_initial_byte_window_drops_partial_head(tmp_path):
    logdir = tmp_path / "logs"
    write_log(logdir, RUN_STAMP, ["aaaa", "bbbb", "cccc"])  # 15 bytes total
    frames = drive(
        logs.tail_events(
            make_record(logdir), is_live=lambda: False, max_bytes=8, sleep=_instant
        )
    )
    # An 8-byte tail window starts mid-file; the partial head line is dropped.
    assert data_frames(frames) == ["data: cccc\n\n"]
