"""Tests for the shared subprocess wrapper, focused on its capped-output mode.

These are the only tests in the suite that run a real subprocess, and they run
``sys.executable`` with an inline script — never git, a managed repo, or the real
registry. A fake runner cannot show what the *producer* does with a command that
writes far more than the caller asked for, and that bound is the whole point of
the capped mode: the evidence layer's ``git log --patch`` output is repo-content
sized, so the cap has to bound what is read, not what is kept afterwards. The
timeout tests need a live child for the same reason: only a real process can
stall a read.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from orch_hub.subprocess_utils import (
    CommandNotFoundError,
    run_command,
)

HUGE = 2_000_000
"""Output size that would be obvious in memory if the cap did not bind."""

LONG = 10
"""Child runtime dwarfing every timeout below, so a test that waits one out fails.

Bounded rather than endless on purpose: a reintroduced bug should make these
tests fail in seconds, not hang a suite.
"""

PATIENCE = 3
"""Upper bound on a timed-out call — well under ``LONG``, well over a scheduling hiccup."""


def emit(size: int) -> list[str]:
    """Argv for a command writing ``size`` bytes to stdout and nothing to stderr."""
    return [
        sys.executable,
        "-c",
        f"import sys; sys.stdout.write('x' * {size})",
    ]


def hold(seconds: float) -> list[str]:
    """Argv for a command that announces its pid, then holds stdout open."""
    return [
        sys.executable,
        "-c",
        "import os, sys, time; sys.stdout.write(str(os.getpid())); sys.stdout.flush();"
        f" time.sleep({seconds})",
    ]


def trickle(chunks: int, pause: float) -> list[str]:
    """Argv for a command that keeps writing, slowly, for a long time."""
    return [
        sys.executable,
        "-c",
        f"import sys, time\nfor _ in range({chunks}):\n"
        f"    sys.stdout.write('x' * 1000); sys.stdout.flush(); time.sleep({pause})",
    ]


def test_capped_run_keeps_only_the_head_and_reports_the_full_size():
    result = run_command(emit(HUGE), max_stdout_bytes=1000)

    assert result.ok is True
    assert result.stdout == "x" * 1000  # the head, not the whole stream
    assert result.stdout_bytes == HUGE  # drained and counted, so the cut is visible
    assert result.stdout_truncated is True


def test_capped_run_below_the_cap_is_whole_and_not_truncated():
    result = run_command(emit(10), max_stdout_bytes=1000)

    assert result.stdout == "x" * 10
    assert result.stdout_bytes == 10
    assert result.stdout_truncated is False


def test_capped_run_returns_non_zero_exits_rather_than_raising():
    result = run_command([sys.executable, "-c", "raise SystemExit(3)"], max_stdout_bytes=64)

    assert result.ok is False and result.returncode == 3


def test_capped_run_of_a_missing_executable_is_command_not_found(tmp_path):
    with pytest.raises(CommandNotFoundError):
        run_command([str(tmp_path / "definitely-not-here")], max_stdout_bytes=64)


def test_capped_run_survives_a_command_that_writes_to_stderr():
    # stderr is discarded in capped mode (bounding one pipe while the other fills
    # deadlocks), so a command that writes plenty of it must still complete.
    result = run_command(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('e' * 500000); sys.stdout.write('ok')",
        ],
        max_stdout_bytes=1000,
    )

    assert result.ok is True and result.stdout == "ok" and result.stderr == ""


def test_capped_run_times_out_while_the_child_still_holds_stdout():
    # Where a stuck command stalls is the read, not the exit: a timeout applied
    # only to the wait is checked once the command has already taken as long as
    # it liked, and then passes because the child is by then gone.
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        run_command(hold(LONG), timeout=0.1, max_stdout_bytes=64)

    assert time.monotonic() - started < PATIENCE


def test_capped_run_timeout_is_a_deadline_not_a_per_read_wait():
    # A command trickling output leaves no quiet gap long enough to trip a
    # per-read timeout, so only an absolute deadline stops it.
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        run_command(trickle(chunks=200, pause=0.05), timeout=0.2, max_stdout_bytes=64)

    assert time.monotonic() - started < PATIENCE


def test_capped_run_leaves_no_child_behind_when_the_timeout_expires():
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_command(hold(LONG), timeout=0.5, max_stdout_bytes=64)

    # The head read before the deadline, reported the way `subprocess.run` reports
    # partial output — here it is the child's pid.
    head = caught.value.output
    assert head, "the child never wrote its pid: too tight a timeout for this machine"
    with pytest.raises(ProcessLookupError):
        os.kill(int(head), 0)  # killed and reaped, not left running


def test_capped_run_with_a_timeout_still_caps_a_command_that_finishes():
    result = run_command(emit(HUGE), timeout=PATIENCE, max_stdout_bytes=1000)

    assert result.ok is True
    assert result.stdout == "x" * 1000
    assert result.stdout_bytes == HUGE and result.stdout_truncated is True


def test_uncapped_run_still_captures_everything_including_stderr():
    result = run_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('o'); sys.stderr.write('e')"]
    )

    assert result.stdout == "o" and result.stderr == "e"
    # Unmeasured: no cap was asked for, so no size/truncation claim is made.
    assert result.stdout_bytes is None and result.stdout_truncated is False
