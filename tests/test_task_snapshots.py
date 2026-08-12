"""Tests for per-session task-file snapshots (``task_snapshots.json``).

The module under test never reads a log: it takes an already-parsed
:class:`SessionsSnapshot` plus a UTC instant, so these build session brackets
directly and keep log parsing in ``test_sessions.py``. Task files are real files
in ``tmp_path`` read through the real task store, since containment and the
partial-write guard are part of the contract.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from orch_hub.models import ManagedRepo, RunRecord, SessionSummary, SessionsSnapshot
from orch_hub.task_snapshots import (
    CAPTURE_POINT_POST_CLAIM,
    ENTRY_LAG_MAX_SECONDS,
    MAX_CONTENT_SNAPSHOTS,
    TASK_FILE_MAX_BYTES,
    SessionTaskSnapshot,
    TaskSnapshots,
    artifact_path,
    capture_entry,
    read_snapshots,
    reconcile_sids,
    write_snapshots,
)

TASK_ID = "2026-07-05-feature-x"
NOW = dt.datetime(2026, 7, 8, 3, 0, 30, tzinfo=dt.timezone.utc)
START_HMS = "03:00:10"  # 20s before NOW: comfortably inside the entry window

TASK_BODY = """---
id: 2026-07-05-feature-x
status: in_progress
session-est: 1/2
prefetch: [.ai/modules/runtime.md]
claimed-by: sid-1@2026-07-08T03:00:12Z
---

# Feature x

## Session log
"""


def make_repo(root: Path, *, body: str | None = TASK_BODY) -> ManagedRepo:
    path = root / "repos" / "repo-a"
    (path / ".ai-tasks").mkdir(parents=True, exist_ok=True)
    if body is not None:
        (path / ".ai-tasks" / f"{TASK_ID}.md").write_text(body, encoding="utf-8")
    return ManagedRepo(
        name="repo-a", path=path, manifest=path / ".ai-deploy-manifest.json"
    )


def make_record(repo: ManagedRepo) -> RunRecord:
    return RunRecord(
        run_id="20260708T030000Z-repo-a-feature",
        repo_name=repo.name,
        repo_path=repo.path,
        task_id=TASK_ID,
        started_ts="2026-07-08T03:00:00+00:00",
    )


def make_run_dir(root: Path) -> Path:
    run_dir = root / "hub-data" / "runs" / "20260708T030000Z-repo-a-feature"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def session(
    *,
    role: str = "dev",
    kind: str = "session",
    sid: str | None = "dev1",
    open_: bool = True,
    start_hms: str = START_HMS,
    status_before: str | None = "in_progress",
) -> SessionSummary:
    return SessionSummary(
        role=role,
        kind=kind,
        sid=sid,
        status_before=status_before,
        start_hms=start_hms,
        open=open_,
    )


def snapshot(*sessions: SessionSummary) -> SessionsSnapshot:
    return SessionsSnapshot(sessions=tuple(sessions))


# -- capturing -----------------------------------------------------------------


def test_captures_the_open_sessions_task_file(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)

    entry = capture_entry(run_dir, make_record(repo), snapshot(session()), now=NOW)

    assert entry is not None
    assert entry.present is True
    assert entry.content == TASK_BODY
    assert entry.size == len(TASK_BODY.encode("utf-8"))
    assert entry.truncated is False
    assert entry.index == 0
    assert entry.sid == "dev1"
    assert entry.role == "dev"
    assert entry.kind == "session"
    assert entry.status_before == "in_progress"
    assert entry.session_start_hms == START_HMS
    assert entry.captured_at == NOW.isoformat()
    # The marker the whole design rests on: this is the file *after* the session's
    # mechanical claim, not the pre-entry one.
    assert entry.capture_point == CAPTURE_POINT_POST_CLAIM
    assert entry.entry_lag_seconds == 20
    assert read_snapshots(run_dir).snapshots == (entry,)


def test_a_second_scan_does_not_recapture_the_same_session(tmp_path):
    # The point of the artifact: entry state, not latest state. Once a session is
    # recorded, later scans must leave its bytes alone however the file evolves.
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    record = make_record(repo)
    capture_entry(run_dir, record, snapshot(session()), now=NOW)

    (repo.path / ".ai-tasks" / f"{TASK_ID}.md").write_text("---\nrewritten\n---\n")
    again = capture_entry(
        run_dir, record, snapshot(session()), now=NOW + dt.timedelta(seconds=30)
    )

    assert again is None
    kept = read_snapshots(run_dir).snapshots
    assert len(kept) == 1
    assert kept[0].content == TASK_BODY


def test_each_session_of_a_run_gets_its_own_entry(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    record = make_record(repo)
    capture_entry(run_dir, record, snapshot(session()), now=NOW)

    second_body = TASK_BODY.replace("session-est: 1/2", "session-est: 2/3")
    (repo.path / ".ai-tasks" / f"{TASK_ID}.md").write_text(second_body)
    later = NOW + dt.timedelta(minutes=20)
    entry = capture_entry(
        run_dir,
        record,
        snapshot(
            session(open_=False),
            session(role="review", sid="rev1", start_hms="03:20:15"),
        ),
        now=later,
    )

    assert entry is not None
    assert entry.index == 1
    assert entry.sid == "rev1"
    assert entry.entry_lag_seconds == 15
    kept = read_snapshots(run_dir).snapshots
    assert [(e.index, e.sid) for e in kept] == [(0, "dev1"), (1, "rev1")]
    # Each session's own entry state, not one shared copy of the newest file.
    assert kept[0].content == TASK_BODY
    assert kept[1].content == second_body


def test_a_closed_bracket_is_not_captured(tmp_path):
    # Its entry window is gone: by the time a session's end line is in the log,
    # the file on disk is that session's *output*, not its input.
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)

    entry = capture_entry(
        run_dir, make_record(repo), snapshot(session(open_=False)), now=NOW
    )

    assert entry is None
    assert not artifact_path(run_dir).exists()


def test_a_plan_gate_bracket_is_not_captured(tmp_path):
    # A plan-gate does not claim the task, and its sibling dev session — which
    # does — gets its own entry moments later.
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)

    entry = capture_entry(
        run_dir, make_record(repo), snapshot(session(kind="plan-gate")), now=NOW
    )

    assert entry is None
    assert not artifact_path(run_dir).exists()


def test_no_sessions_yet_captures_nothing(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)

    assert capture_entry(run_dir, make_record(repo), snapshot(), now=NOW) is None
    assert not artifact_path(run_dir).exists()


# -- the entry window ----------------------------------------------------------


def test_a_session_observed_past_the_window_records_a_miss(tmp_path):
    # A hub started well inside a running session must not pass that session's own
    # mid-flight edits off as its entry state.
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    late = NOW + dt.timedelta(seconds=ENTRY_LAG_MAX_SECONDS)

    entry = capture_entry(run_dir, make_record(repo), snapshot(session()), now=late)

    assert entry is not None
    assert entry.present is False
    assert entry.content is None
    assert entry.entry_lag_seconds == ENTRY_LAG_MAX_SECONDS + 20
    assert "entry window" in entry.detail
    assert read_snapshots(run_dir).snapshots == (entry,)


def test_a_capture_at_the_window_edge_still_counts(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    edge = NOW + dt.timedelta(seconds=ENTRY_LAG_MAX_SECONDS - 20)

    entry = capture_entry(run_dir, make_record(repo), snapshot(session()), now=edge)

    assert entry.present is True
    assert entry.entry_lag_seconds == ENTRY_LAG_MAX_SECONDS


def test_a_non_utc_clock_is_converted_before_the_lag_is_taken(tmp_path):
    # The log stamp is UTC time-of-day; comparing a local-time clock against it
    # would read as an hours-wide lag and refuse every capture.
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    local = NOW.astimezone(dt.timezone(dt.timedelta(hours=8)))

    entry = capture_entry(run_dir, make_record(repo), snapshot(session()), now=local)

    assert entry.present is True
    assert entry.entry_lag_seconds == 20


def test_an_unparseable_start_stamp_does_not_block_the_capture(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)

    entry = capture_entry(
        run_dir, make_record(repo), snapshot(session(start_hms="")), now=NOW
    )

    assert entry.present is True
    assert entry.entry_lag_seconds is None


# -- degraded task files -------------------------------------------------------


def test_a_missing_task_file_is_retried_inside_the_window(tmp_path):
    # No entry is recorded, so the next scan can still capture the real thing;
    # nothing raises, and the caller's scan is unaffected.
    repo = make_repo(tmp_path, body=None)
    run_dir = make_run_dir(tmp_path)
    record = make_record(repo)

    assert capture_entry(run_dir, record, snapshot(session()), now=NOW) is None
    assert not artifact_path(run_dir).exists()

    (repo.path / ".ai-tasks" / f"{TASK_ID}.md").write_text(TASK_BODY, encoding="utf-8")
    entry = capture_entry(
        run_dir, record, snapshot(session()), now=NOW + dt.timedelta(seconds=30)
    )

    assert entry.present is True
    assert entry.content == TASK_BODY


def test_a_task_file_still_missing_when_the_window_closes_records_a_miss(tmp_path):
    repo = make_repo(tmp_path, body=None)
    run_dir = make_run_dir(tmp_path)
    late = NOW + dt.timedelta(seconds=ENTRY_LAG_MAX_SECONDS)

    entry = capture_entry(run_dir, make_record(repo), snapshot(session()), now=late)

    assert entry.present is False
    assert entry.content is None


def test_an_unterminated_frontmatter_is_treated_as_a_write_in_progress(tmp_path):
    repo = make_repo(tmp_path, body="---\nid: 2026-07-05-feature-x\nstatus: in_pr")
    run_dir = make_run_dir(tmp_path)
    record = make_record(repo)

    assert capture_entry(run_dir, record, snapshot(session()), now=NOW) is None

    (repo.path / ".ai-tasks" / f"{TASK_ID}.md").write_text(TASK_BODY, encoding="utf-8")
    entry = capture_entry(
        run_dir, record, snapshot(session()), now=NOW + dt.timedelta(seconds=30)
    )
    assert entry.content == TASK_BODY


def test_a_file_with_no_frontmatter_at_all_is_still_captured(tmp_path):
    # Not a conforming task file, but not evidence of a partial write either —
    # dropping it would lose the only copy of what that session read.
    repo = make_repo(tmp_path, body="# Feature x\n\nno frontmatter here\n")
    run_dir = make_run_dir(tmp_path)

    entry = capture_entry(run_dir, make_record(repo), snapshot(session()), now=NOW)

    assert entry.present is True
    assert entry.content == "# Feature x\n\nno frontmatter here\n"


def test_an_empty_task_file_is_not_captured(tmp_path):
    repo = make_repo(tmp_path, body="")
    run_dir = make_run_dir(tmp_path)

    entry = capture_entry(run_dir, make_record(repo), snapshot(session()), now=NOW)

    assert entry is None
    assert not artifact_path(run_dir).exists()


def test_an_unreadable_task_file_never_raises(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)

    def boom(_repo, _task_id):
        raise OSError("permission denied")

    entry = capture_entry(
        run_dir, make_record(repo), snapshot(session()), now=NOW, read_task=boom
    )

    assert entry is None
    assert not artifact_path(run_dir).exists()


def test_a_record_with_no_repo_path_captures_nothing(tmp_path):
    run_dir = make_run_dir(tmp_path)
    stub = RunRecord(run_id="20260708T030000Z-broken", error="unreadable run.json")

    assert capture_entry(run_dir, stub, snapshot(session()), now=NOW) is None


def test_an_oversize_task_file_keeps_a_truncated_head(tmp_path):
    body = TASK_BODY + "x" * TASK_FILE_MAX_BYTES
    repo = make_repo(tmp_path, body=body)
    run_dir = make_run_dir(tmp_path)

    entry = capture_entry(run_dir, make_record(repo), snapshot(session()), now=NOW)

    assert entry.truncated is True
    assert entry.size == len(body.encode("utf-8"))
    assert len(entry.content.encode("utf-8")) == TASK_FILE_MAX_BYTES
    # The head is what matters: frontmatter carries the unrecoverable fields.
    assert entry.content.startswith("---\nid: 2026-07-05-feature-x\n")


def test_the_content_cap_records_the_session_without_content(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    write_snapshots(
        run_dir,
        TaskSnapshots(
            snapshots=tuple(
                SessionTaskSnapshot(index=i, present=True, content="x")
                for i in range(MAX_CONTENT_SNAPSHOTS)
            )
        ),
    )

    entry = capture_entry(
        run_dir,
        make_record(repo),
        snapshot(
            *(session(open_=False) for _ in range(MAX_CONTENT_SNAPSHOTS)), session()
        ),
        now=NOW,
    )

    assert entry.present is False
    assert str(MAX_CONTENT_SNAPSHOTS) in entry.detail  # a cap is never silent
    assert len(read_snapshots(run_dir).snapshots) == MAX_CONTENT_SNAPSHOTS + 1


# -- pending session ids -------------------------------------------------------


def test_a_pending_sid_is_backfilled_when_the_log_reveals_it(tmp_path):
    # Codex names its session id on the first turn, so the start line can log
    # ``(pending)`` and only the end line carries the real id.
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    pending = session(role="review", sid=None)
    captured = capture_entry(run_dir, make_record(repo), snapshot(pending), now=NOW)
    assert captured.sid is None

    settled = reconcile_sids(
        run_dir,
        snapshot(session(role="review", sid="rev1", open_=False)),
    )

    assert settled is not None
    assert settled.snapshots[0].sid == "rev1"
    assert settled.snapshots[0].content == TASK_BODY  # content is untouched
    assert read_snapshots(run_dir).for_sid("rev1") == settled.snapshots[0]


def test_reconcile_leaves_a_shifted_bracket_alone(tmp_path):
    # A rotated or truncated log could move a bracket; adopting whatever sid now
    # sits at that position would stamp the entry with another session's id.
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    capture_entry(run_dir, make_record(repo), snapshot(session(sid=None)), now=NOW)

    assert (
        reconcile_sids(run_dir, snapshot(session(sid="other", start_hms="09:99:99")))
        is None
    )
    assert read_snapshots(run_dir).snapshots[0].sid is None


def test_reconcile_is_a_no_op_when_nothing_is_pending(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    capture_entry(run_dir, make_record(repo), snapshot(session()), now=NOW)

    assert reconcile_sids(run_dir, snapshot(session(open_=False))) is None


def test_reconcile_without_an_artifact_is_a_no_op(tmp_path):
    assert reconcile_sids(make_run_dir(tmp_path), snapshot(session())) is None


# -- persistence ---------------------------------------------------------------


def test_snapshots_round_trip_through_the_artifact(tmp_path):
    repo = make_repo(tmp_path)
    run_dir = make_run_dir(tmp_path)
    entry = capture_entry(run_dir, make_record(repo), snapshot(session()), now=NOW)

    reloaded = read_snapshots(run_dir)

    assert reloaded.snapshots == (entry,)
    assert reloaded.for_index(0) == entry
    assert reloaded.for_sid("dev1") == entry
    assert reloaded.for_sid("nope") is None
    persisted = json.loads(artifact_path(run_dir).read_text())
    assert persisted["snapshots"][0]["sid"] == "dev1"


def test_an_absent_or_corrupt_artifact_reads_as_none(tmp_path):
    run_dir = make_run_dir(tmp_path)
    assert read_snapshots(run_dir) is None

    artifact_path(run_dir).write_text("{not json", encoding="utf-8")
    assert read_snapshots(run_dir) is None


def test_a_partial_artifact_keeps_the_entries_it_can_parse(tmp_path):
    run_dir = make_run_dir(tmp_path)
    artifact_path(run_dir).write_text(
        json.dumps(
            {"snapshots": ["nonsense", {"no": "index"}, {"index": 3, "sid": "dev3"}]}
        ),
        encoding="utf-8",
    )

    kept = read_snapshots(run_dir)

    assert [(e.index, e.sid) for e in kept.snapshots] == [(3, "dev3")]
    assert kept.snapshots[0].capture_point == CAPTURE_POINT_POST_CLAIM
    assert kept.snapshots[0].present is False
