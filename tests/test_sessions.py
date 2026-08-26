"""Tests for the orchestrator-log per-session parser and snapshot IO.

The parser is fed line lists directly (no file, no run) so the marker grammar and
tolerance are pinned in isolation; the IO helpers use ``tmp_path``. The fixtures
mirror the real orchestrator log grammar verified against a captured 4-session
async-intake run (plan-gate brackets, ``(pending)``→real sid reconciliation,
multi-turn context growth, transcript interleave).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from pathlib import Path

from orch_hub import sessions
from orch_hub.models import CONTEXT_BUDGET_TOKENS, SessionsSnapshot
from orch_hub.sessions import parse_sessions, read_live, read_snapshot, write_snapshot
from orch_hub.models import RunRecord

TASK_ID = "2026-07-05-feature-x"


def dev_session(sid: str = "dev-sid", *, status_before: str = "in_progress") -> list[str]:
    """A minimal well-formed dev session bracket (start, one turn, end)."""
    return [
        f"[08:20:48] --- dev session start: sid={sid} "
        f"backend=claude:claude-opus-4-8@max status_before={status_before}",
        "[08:55:29] run finished: status=finished context≈120000 tokens",
        f"[08:55:29] --- dev session end: sid={sid} status=in_progress",
    ]


# -- parse: single session -----------------------------------------------------


def test_parse_single_session():
    snap = parse_sessions(dev_session("bbb"))
    assert len(snap.sessions) == 1
    only = snap.sessions[0]
    assert only.role == "dev"
    assert only.kind == "session"
    assert only.sid == "bbb"
    assert only.turns == 1
    assert only.context_tokens == 120000
    assert only.status_before == "in_progress"
    assert only.end_status == "in_progress"
    assert only.start_hms == "08:20:48"
    assert only.open is False
    assert snap.context_budget == CONTEXT_BUDGET_TOKENS


def test_parse_empty_is_empty_snapshot():
    assert parse_sessions([]).sessions == ()


def test_parse_zero_turn_session_is_valid():
    lines = [
        "[08:20:48] --- review session start: sid=r1 "
        "backend=codex:gpt-5.5@xhigh status_before=final_review",
        "[08:20:59] --- review session end: sid=r1 status=completed+archived",
    ]
    only = parse_sessions(lines).sessions[0]
    assert only.turns == 0
    assert only.context_tokens is None
    assert only.end_status == "completed+archived"
    assert only.open is False


# -- parse: plan-gate + multi-turn + boundaries --------------------------------


def test_parse_plan_gate_then_session_are_two_ordered_entries():
    # The dev plan-gate bracket (looping two turns) precedes its sibling dev
    # session; each is its own entry, distinguished by kind.
    lines = [
        "[06:42:25] --- dev plan-gate start: sid=pg "
        "backend=claude:claude-opus-4-8@max status_before=pending",
        "[06:50:00] run finished: tool=claude exit=0 status=finished context≈116581 tokens",
        "[08:17:30] run finished: tool=claude exit=0 status=finished context≈131295 tokens",
        "[08:20:48] --- dev plan-gate end: sid=pg",
        *dev_session("bbb", status_before="pending"),
    ]
    snap = parse_sessions(lines)
    assert [(s.kind, s.turns) for s in snap.sessions] == [("plan-gate", 2), ("session", 1)]
    plan = snap.sessions[0]
    assert plan.role == "dev"
    assert plan.context_tokens == 131295  # latest of the two turns (monotonic peak)
    assert plan.status_before == "pending"
    assert plan.end_status is None  # a plan-gate end logs no status
    assert plan.open is False
    assert plan.sid == "pg"


def test_parse_multi_turn_keeps_latest_context():
    lines = [
        "[00:00:01] --- dev session start: sid=s backend=claude:x@max status_before=pending",
        "[00:00:02] run finished: status=finished context≈100 tokens",
        "[00:00:03] run finished: status=finished context≈200 tokens",
        "[00:00:04] run finished: status=finished context≈300 tokens",
        "[00:00:05] --- dev session end: sid=s status=in_progress",
    ]
    only = parse_sessions(lines).sessions[0]
    assert only.turns == 3
    assert only.context_tokens == 300


def test_parse_cli_and_sdk_run_finished_variants_both_count():
    # CLI backend: `tool=... exit=... status=... context≈N`; SDK backend:
    # `status=... context≈N`. Both must be matched as turns.
    lines = [
        "[00:00:01] --- dev session start: sid=s backend=x status_before=pending",
        "[00:00:02] run finished: status=finished context≈111 tokens",
        "[00:00:03] run finished: tool=claude exit=0 status=finished context≈222 tokens",
        "[00:00:04] --- dev session end: sid=s status=in_progress",
    ]
    only = parse_sessions(lines).sessions[0]
    assert only.turns == 2
    assert only.context_tokens == 222


# -- parse: sid reconciliation -------------------------------------------------


def test_parse_reconciles_pending_start_sid_to_end_sid():
    # A review session starts logging sid=(pending); the real id appears only at
    # end. The reconciled summary carries the real id.
    lines = [
        "[08:55:30] --- review session start: sid=(pending) "
        "backend=codex:gpt-5.5@xhigh status_before=in_progress",
        "[08:58:19] run finished: tool=codex exit=0 status=finished context≈120372 tokens",
        "[08:58:19] --- review session end: sid=019f5589-real status=in_progress",
    ]
    only = parse_sessions(lines).sessions[0]
    assert only.sid == "019f5589-real"


def test_parse_still_pending_open_session_renders_with_null_sid():
    # A live session whose start logged (pending) and has no end line yet: still a
    # valid open summary, sid unknown.
    lines = [
        "[08:55:30] --- review session start: sid=(pending) "
        "backend=codex:gpt-5.5@xhigh status_before=in_progress",
        "[08:58:19] run finished: tool=codex exit=0 status=finished context≈120372 tokens",
    ]
    only = parse_sessions(lines).sessions[0]
    assert only.sid is None
    assert only.open is True
    assert only.end_status is None
    assert only.turns == 1


def test_parse_prefers_real_start_sid_over_end():
    lines = dev_session("real-start")
    assert parse_sessions(lines).sessions[0].sid == "real-start"


# -- parse: tolerance (transcript interleave, truncation, live tail) -----------


def test_parse_excludes_unanchored_transcript_markers():
    # transcript()/ask_human() write raw text and `--- ... ---` banners WITHOUT
    # the [HH:MM:SS] prefix. A transcript that echoes a marker-shaped line must
    # not be mistaken for a real marker; only anchored lines count.
    lines = [
        "[08:20:48] --- dev session start: sid=real backend=x status_before=pending",
        "--- HUMAN INPUT NEEDED ---",
        "--- dev session end: sid=forged status=completed+archived",  # no anchor
        "run finished: status=finished context≈999999 tokens",  # no anchor
        "--- end banner ---",
        "[08:55:29] run finished: status=finished context≈120000 tokens",
        "[08:55:29] --- dev session end: sid=real status=in_progress",
    ]
    snap = parse_sessions(lines)
    assert len(snap.sessions) == 1
    only = snap.sessions[0]
    assert only.sid == "real"  # not the forged end sid
    assert only.turns == 1  # the unanchored run-finished was ignored
    assert only.context_tokens == 120000
    assert only.end_status == "in_progress"


def test_parse_truncated_tail_line_is_skipped_and_leaves_open_session():
    # The last line was cut mid-write; it fails to match and is skipped. The
    # session it belonged to stays open (no crash, partial data).
    lines = [
        "[08:20:48] --- dev session start: sid=s backend=x status_before=pending",
        "[08:55:29] run finished: status=finished context≈120000 tokens",
        "[08:55:29] --- dev session en",  # truncated end marker
    ]
    only = parse_sessions(lines).sessions[0]
    assert only.open is True
    assert only.end_status is None
    assert only.turns == 1


def test_parse_truncated_run_finished_is_not_counted():
    lines = [
        "[08:20:48] --- dev session start: sid=s backend=x status_before=pending",
        "[08:55:29] run finished: status=finished context≈1200",  # cut before ` tokens`
        "[08:55:30] --- dev session end: sid=s status=in_progress",
    ]
    only = parse_sessions(lines).sessions[0]
    assert only.turns == 0
    assert only.context_tokens is None


def test_parse_start_without_end_finalizes_prior_as_open():
    # Two starts in a row (first session's end line lost): the first is emitted as
    # open, the second continues normally.
    lines = [
        "[00:00:01] --- dev session start: sid=s1 backend=x status_before=pending",
        "[00:00:02] run finished: status=finished context≈100 tokens",
        "[00:00:03] --- dev session start: sid=s2 backend=x status_before=in_progress",
        "[00:00:04] run finished: status=finished context≈200 tokens",
        "[00:00:05] --- dev session end: sid=s2 status=final_review",
    ]
    snap = parse_sessions(lines)
    assert [s.sid for s in snap.sessions] == ["s1", "s2"]
    assert snap.sessions[0].open is True
    assert snap.sessions[1].open is False


def test_parse_orphan_end_is_ignored():
    # An end with nothing open (or a role/kind mismatch) must not crash or emit.
    lines = [
        "[00:00:05] --- dev session end: sid=ghost status=completed+archived",
        *dev_session("real"),
    ]
    snap = parse_sessions(lines)
    assert [s.sid for s in snap.sessions] == ["real"]


def test_parse_ignores_ordinary_log_lines():
    lines = [
        "[00:00:00] LOG_DIR resolved",
        "[00:00:01] --- dev session start: sid=s backend=x status_before=pending",
        "[00:00:02] WARNING: no session id captured from the stream",
        "[00:00:03] [stderr] some noise",
        "[00:00:04] --- dev session end: sid=s status=in_progress",
    ]
    assert len(parse_sessions(lines).sessions) == 1


# -- read_live -----------------------------------------------------------------


def make_record(log_dir: Path | None) -> RunRecord:
    return RunRecord(
        run_id="20260708T030000Z-repo-a-task",
        repo_name="repo-a",
        repo_path=Path("/repos/repo-a"),
        task_id=TASK_ID,
        log_dir=log_dir,
        started_ts=dt.datetime(2026, 7, 8, 3, 0, 0, tzinfo=dt.timezone.utc).isoformat(),
    )


def test_read_live_no_log_is_empty_snapshot():
    assert read_live(make_record(None)).sessions == ()
    assert read_live(make_record(Path("/nope/logs"))).sessions == ()


def test_read_live_parses_located_log(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / f"20260708T030001Z-{TASK_ID}.log").write_text(
        "\n".join(dev_session("bbb")) + "\n", encoding="utf-8"
    )
    snap = read_live(make_record(log_dir))
    assert [s.sid for s in snap.sessions] == ["bbb"]
    assert snap.sessions[0].context_tokens == 120000


# -- read_run_closeout ---------------------------------------------------------


CLOSE_OUT_NATIVE = (
    "[09:12:00] close-out done (performed in-session by the native hook chain; "
    "orchestrator verified archive/index/tree/remaining tasks)"
)


def capture_record(spawn_out: Path | None) -> RunRecord:
    record = make_record(None)
    return replace(record, spawn_out=spawn_out)


def test_read_run_closeout_finds_the_driven_line(tmp_path):
    capture = tmp_path / "spawn.out"
    capture.write_text(
        "\n".join([*dev_session("bbb"), "[09:12:00] close-out done"]) + "\n",
        encoding="utf-8",
    )
    assert sessions.read_run_closeout(capture_record(capture)) is True


def test_read_run_closeout_finds_the_native_hook_chain_line(tmp_path):
    # The blocked-resume completion: no bracket was ever opened for the resumed
    # session, so this line is the run's only statement that it finished its task.
    capture = tmp_path / "spawn.out"
    capture.write_text(
        "\n".join(
            [
                "[09:10:00] resuming blocked review session rev-nc-0001 with answer",
                "[09:11:00] run finished: status=finished context≈90000 tokens",
                "[09:11:30] task file archived mid-session — the native hook chain "
                "ran the close-out inside the resumed session; skipping post-checks",
                CLOSE_OUT_NATIVE,
                "[09:12:01] orchestrator done",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert sessions.read_run_closeout(capture_record(capture)) is True


def test_read_run_closeout_ignores_unanchored_text(tmp_path):
    # Same anchor rule as the bracket markers: agent transcript text quoting the
    # phrase carries no ``[HH:MM:SS] `` prefix and must not fake a completion.
    capture = tmp_path / "spawn.out"
    capture.write_text(
        "close-out done\nthe reviewer wrote: close-out done\n", encoding="utf-8"
    )
    assert sessions.read_run_closeout(capture_record(capture)) is False


def test_read_run_closeout_without_the_line_is_false(tmp_path):
    capture = tmp_path / "spawn.out"
    capture.write_text("\n".join(dev_session("bbb")) + "\n", encoding="utf-8")
    assert sessions.read_run_closeout(capture_record(capture)) is False


def test_read_run_closeout_absent_capture_is_false(tmp_path):
    assert sessions.read_run_closeout(capture_record(None)) is False
    assert sessions.read_run_closeout(capture_record(tmp_path / "nope.out")) is False


# -- snapshot IO ---------------------------------------------------------------


def test_write_read_snapshot_roundtrip(tmp_path):
    snap = parse_sessions(
        [
            "[06:42:25] --- dev plan-gate start: sid=pg backend=x status_before=pending",
            "[06:50:00] run finished: status=finished context≈116581 tokens",
            "[08:20:48] --- dev plan-gate end: sid=pg",
            *dev_session("bbb"),
        ]
    )
    write_snapshot(tmp_path, snap)
    assert sessions.snapshot_path(tmp_path).name == "sessions.json"
    assert read_snapshot(tmp_path) == snap


def test_read_snapshot_absent_is_none(tmp_path):
    assert read_snapshot(tmp_path) is None


def test_read_snapshot_corrupt_is_none(tmp_path):
    sessions.snapshot_path(tmp_path).write_text("{ not json", encoding="utf-8")
    assert read_snapshot(tmp_path) is None


def test_read_snapshot_empty_but_present_is_not_none(tmp_path):
    # An empty snapshot written for a run that ended with no locatable log is a
    # present (not None) result: the caller does not fall back to a live re-parse.
    write_snapshot(tmp_path, SessionsSnapshot())
    result = read_snapshot(tmp_path)
    assert result is not None
    assert result.sessions == ()
    assert result.context_budget == CONTEXT_BUDGET_TOKENS


def test_read_snapshot_tolerates_partial_rows(tmp_path):
    # A row missing role/kind is dropped; a bad budget falls back to the default.
    sessions.snapshot_path(tmp_path).write_text(
        '{"sessions": [{"role": "dev"}, {"role": "dev", "kind": "session", '
        '"sid": "ok", "turns": 2}], "context_budget": -1}',
        encoding="utf-8",
    )
    result = read_snapshot(tmp_path)
    assert result is not None
    assert [s.sid for s in result.sessions] == ["ok"]
    assert result.sessions[0].turns == 2
    assert result.context_budget == CONTEXT_BUDGET_TOKENS
