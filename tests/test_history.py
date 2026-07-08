"""Tests for file-backed run history events."""

from __future__ import annotations

from pathlib import Path

from orch_hub.history import KIND_HISTORY_ERROR, append_event, read_events
from orch_hub.models import RunEvent


def test_append_and_read_run_events(tmp_path: Path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)

    append_event(
        run_dir,
        RunEvent(
            ts="2026-07-08T03:00:00+00:00",
            kind="run_started",
            run_id="run-1",
            repo_name="repo-a",
            repo_path=tmp_path / "repo-a",
            task_id="2026-07-05-feature-x",
            task_status="in_progress",
        ),
    )
    append_event(
        run_dir,
        RunEvent(
            ts="2026-07-08T03:01:00+00:00",
            kind="run_ended",
            run_id="run-1",
            exit_code=0,
        ),
    )

    events = read_events(run_dir)

    assert [event.kind for event in events] == ["run_started", "run_ended"]
    assert events[0].repo_path == tmp_path / "repo-a"
    assert events[0].task_status == "in_progress"
    assert events[1].exit_code == 0


def test_read_events_reports_malformed_history_line(tmp_path: Path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text("{not json\n", encoding="utf-8")

    events = read_events(run_dir)

    assert len(events) == 1
    assert events[0].kind == KIND_HISTORY_ERROR
    assert "invalid JSON" in events[0].detail
