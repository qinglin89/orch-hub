"""Tests for the Claude Code statusLine usage writer.

The writer is the only local carrier of Claude's 5h/7d subscription windows, so
these cover both halves of its contract: it captures the CLI's rate-limit payload
into the hub sink, and it stays *invisible* — the operator's own statusLine
command receives the same stdin and its stdout/exit code are relayed untouched.

Nothing here launches Claude: the payload is a fixture on stdin and the wrapped
command is a small Python script written into ``tmp_path``. No account,
credential, or network access is involved.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
from pathlib import Path

from orch_hub.config import DATA_DIR_ENV, DEFAULT_DATA_DIR
from orch_hub.statusline import (
    MAX_SINK_BYTES,
    build_record,
    capture,
    main,
    passthrough,
    resolve_sink,
)
from orch_hub.usage import CLAUDE_STATUSLINE_NAME, USAGE_DIR_NAME, read_claude_statusline

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 24, 2, 0, 0, tzinfo=UTC)


def statusline_payload(*, rate_limits: object = None, plan: str | None = "max", **extra: object) -> dict:
    """A Claude Code statusLine stdin payload.

    Beyond the rate-limit container it carries session/cwd/model/cost context —
    included here precisely because none of it may reach the sink."""
    payload: dict = {
        "hook_event_name": "Status",
        "session_id": "sess_SECRET",
        "transcript_path": "/Users/someone/.claude/projects/x/SECRET.jsonl",
        "cwd": "/Users/someone/private-repo",
        "model": {"id": "claude-opus-4-8", "display_name": "Opus"},
        "workspace": {"current_dir": "/Users/someone/private-repo"},
        "version": "2.1.218",
        "cost": {"total_cost_usd": 1.23},
    }
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    if plan is not None:
        payload["subscription_type"] = plan
    payload.update(extra)
    return payload


def subscriber_rate_limits(*, five: float = 41.0) -> dict:
    """The subscriber-only container: 0-100 percentages plus epoch reset times."""
    return {
        "five_hour": {"used_percentage": five, "resets_at": 1785290400},
        "seven_day": {"used_percentage": 9.5, "resets_at": 1785790400},
    }


# -- sink location ------------------------------------------------------------


def test_sink_defaults_to_hub_data_dir() -> None:
    # The writer and the reader must agree on one path; both derive it from the
    # same constants rather than spelling it out twice.
    assert resolve_sink(env={}) == DEFAULT_DATA_DIR / USAGE_DIR_NAME / CLAUDE_STATUSLINE_NAME


def test_sink_follows_data_dir_env_and_flags(tmp_path: Path) -> None:
    assert resolve_sink(env={DATA_DIR_ENV: str(tmp_path)}) == tmp_path / USAGE_DIR_NAME / CLAUDE_STATUSLINE_NAME
    # Explicit flags win over the environment, in flag-then-env order.
    assert resolve_sink(data_dir=str(tmp_path / "d"), env={DATA_DIR_ENV: "/other"}).parent.parent == tmp_path / "d"
    assert resolve_sink(sink=str(tmp_path / "s.json"), data_dir=str(tmp_path)) == tmp_path / "s.json"


def test_sink_ignores_blank_data_dir() -> None:
    assert resolve_sink(env={DATA_DIR_ENV: "   "}) == DEFAULT_DATA_DIR / USAGE_DIR_NAME / CLAUDE_STATUSLINE_NAME


# -- what is (and is not) persisted -------------------------------------------


def test_record_carries_rate_limits_and_plan_only() -> None:
    record = build_record(statusline_payload(rate_limits=subscriber_rate_limits()), observed_ts=NOW.isoformat())
    assert record is not None
    assert record["rate_limits"] == subscriber_rate_limits()
    assert record["subscription_type"] == "max"
    assert record["observed_ts"] == NOW.isoformat()
    # Session id, transcript path, cwd, model, and cost are not capacity data and
    # must not be persisted into the hub data dir.
    text = json.dumps(record)
    assert "SECRET" not in text
    for leaked in ("cwd", "transcript_path", "session_id", "model", "cost", "workspace"):
        assert leaked not in record


def test_record_none_without_rate_limits() -> None:
    # Non-subscriber sessions, and subscriber sessions before their first API
    # response, carry no container: record nothing rather than an empty snapshot.
    assert build_record(statusline_payload(), observed_ts=NOW.isoformat()) is None
    assert build_record(statusline_payload(rate_limits={}), observed_ts=NOW.isoformat()) is None
    assert build_record(statusline_payload(rate_limits="nope"), observed_ts=NOW.isoformat()) is None
    assert build_record("not an object", observed_ts=NOW.isoformat()) is None


def test_capture_writes_sink_readable_by_the_hub(tmp_path: Path) -> None:
    sink = tmp_path / "usage" / CLAUDE_STATUSLINE_NAME
    raw = json.dumps(statusline_payload(rate_limits=subscriber_rate_limits(five=41.0))).encode()
    assert capture(raw, sink, now=NOW) is True
    # End to end: what the writer persists is what the hub's reader accepts.
    observation = read_claude_statusline(sink)
    assert observation is not None
    assert observation.plan == "max"
    assert observation.observed_ts == NOW.isoformat()
    by_key = {w.key: w for w in observation.windows}
    assert by_key["five_hour"].used_percent == 41.0
    assert by_key["seven_day"].remaining_percent == 90.5


def test_capture_preserves_previous_observation_when_nothing_to_record(tmp_path: Path) -> None:
    sink = tmp_path / CLAUDE_STATUSLINE_NAME
    capture(json.dumps(statusline_payload(rate_limits=subscriber_rate_limits(five=41.0))).encode(), sink, now=NOW)
    # A later render with no container (e.g. a non-subscriber session on the same
    # Mac) must not clobber real figures with an empty snapshot.
    assert capture(json.dumps(statusline_payload()).encode(), sink, now=NOW) is False
    assert read_claude_statusline(sink).windows[0].used_percent == 41.0


def test_capture_never_raises(tmp_path: Path) -> None:
    sink = tmp_path / "usage" / CLAUDE_STATUSLINE_NAME
    # Malformed, non-UTF-8, empty, oversized, and unwritable inputs are all just
    # "nothing recorded" — a status line renders regardless of hub bookkeeping.
    assert capture(b"", sink) is False
    assert capture(b"not json", sink) is False
    assert capture(b"\xff\xfe garbage", sink) is False
    assert capture(b"x" * (1024 * 1024 + 1), sink) is False
    blocked = tmp_path / "blocked"
    blocked.write_text("i am a file, not a dir", encoding="utf-8")
    payload = json.dumps(statusline_payload(rate_limits=subscriber_rate_limits())).encode()
    assert capture(payload, blocked / "usage" / CLAUDE_STATUSLINE_NAME) is False


def test_capture_refuses_oversized_record(tmp_path: Path) -> None:
    # A future container of implausible size must not grow an unbounded file.
    sink = tmp_path / CLAUDE_STATUSLINE_NAME
    fat = subscriber_rate_limits()
    fat["padding"] = "x" * (MAX_SINK_BYTES + 1)
    assert capture(json.dumps(statusline_payload(rate_limits=fat)).encode(), sink) is False
    assert not sink.exists()


def test_capture_leaves_no_temp_files(tmp_path: Path) -> None:
    sink = tmp_path / "usage" / CLAUDE_STATUSLINE_NAME
    capture(json.dumps(statusline_payload(rate_limits=subscriber_rate_limits())).encode(), sink, now=NOW)
    assert list(sink.parent.glob("*.tmp")) == []


# -- pass-through: installing the wrapper costs no visible behavior ------------


def write_fake_statusline(tmp_path: Path, body: str) -> Path:
    """A stand-in for the operator's own statusLine command."""
    script = tmp_path / "operator-statusline.py"
    script.write_text(body, encoding="utf-8")
    return script


def test_passthrough_relays_stdin_stdout_and_exit_code(tmp_path: Path, capfd) -> None:
    # The operator's command must see the same payload and own the rendered line.
    script = write_fake_statusline(
        tmp_path,
        "import json,sys\n"
        "p = json.load(sys.stdin)\n"
        "sys.stdout.write('\\x1b[32m' + p['model']['display_name'] + ' | ' + p['cwd'] + '\\x1b[0m')\n"
        "sys.exit(3)\n",
    )
    raw = json.dumps(statusline_payload(rate_limits=subscriber_rate_limits())).encode()
    assert passthrough([sys.executable, str(script)], raw) == 3
    out = capfd.readouterr().out
    assert out == "\x1b[32mOpus | /Users/someone/private-repo\x1b[0m"  # byte-identical, colours intact


def test_passthrough_without_command_prints_nothing(capfd) -> None:
    assert passthrough([], b"{}") == 0
    assert capfd.readouterr().out == ""


def test_passthrough_missing_command_does_not_print_a_partial_line(tmp_path: Path, capfd) -> None:
    assert passthrough([str(tmp_path / "does-not-exist")], b"{}") == 127
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "cannot run" in captured.err


# -- CLI entry point ----------------------------------------------------------


def run_main(argv: list[str], stdin: bytes, monkeypatch) -> int:
    """Drive ``main`` with a fake stdin buffer."""
    stream = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stream)
    return main(argv)


def test_main_captures_then_passes_through(tmp_path: Path, monkeypatch, capfd) -> None:
    script = write_fake_statusline(tmp_path, "import sys\nsys.stdout.write('my status line')\n")
    sink = tmp_path / "usage" / CLAUDE_STATUSLINE_NAME
    raw = json.dumps(statusline_payload(rate_limits=subscriber_rate_limits(five=12.5))).encode()
    code = run_main(["--sink", str(sink), "--", sys.executable, str(script)], raw, monkeypatch)
    assert code == 0
    assert capfd.readouterr().out == "my status line"
    assert read_claude_statusline(sink).windows[0].used_percent == 12.5


def test_main_exit_code_is_the_wrapped_command_s(tmp_path: Path, monkeypatch, capfd) -> None:
    # Capture success/failure never changes the status line's exit code.
    script = write_fake_statusline(tmp_path, "import sys\nsys.exit(2)\n")
    code = run_main(["--sink", str(tmp_path / "s.json"), "--", sys.executable, str(script)], b"not json", monkeypatch)
    capfd.readouterr()
    assert code == 2


def test_main_without_wrapped_command_still_captures(tmp_path: Path, monkeypatch, capfd) -> None:
    sink = tmp_path / CLAUDE_STATUSLINE_NAME
    raw = json.dumps(statusline_payload(rate_limits=subscriber_rate_limits())).encode()
    assert run_main(["--sink", str(sink)], raw, monkeypatch) == 0
    assert capfd.readouterr().out == ""  # no wrapped command -> no status line
    assert read_claude_statusline(sink) is not None


def test_main_print_sink_reports_the_resolved_path(tmp_path: Path, monkeypatch, capfd) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    assert run_main(["--print-sink"], b"", monkeypatch) == 0
    printed = capfd.readouterr().out.strip()
    assert printed == str(tmp_path / USAGE_DIR_NAME / CLAUDE_STATUSLINE_NAME)


def test_main_never_writes_claude_config(tmp_path: Path, monkeypatch, capfd) -> None:
    # Installation is operator-driven: nothing in this path may touch ~/.claude.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    before = sorted(p.name for p in (home / ".claude").iterdir())
    raw = json.dumps(statusline_payload(rate_limits=subscriber_rate_limits())).encode()
    run_main(["--sink", str(tmp_path / "s.json")], raw, monkeypatch)
    capfd.readouterr()
    assert sorted(p.name for p in (home / ".claude").iterdir()) == before
    assert os.path.exists(tmp_path / "s.json")
