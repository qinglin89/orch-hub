"""Tests for the shared subprocess wrapper, focused on its capped-output mode.

These are the only tests in the suite that run a real subprocess, and they run
``sys.executable`` with an inline script — never git, a managed repo, or the real
registry. A fake runner cannot show what the *producer* does with a command that
writes far more than the caller asked for, and that bound is the whole point of
the capped mode: the evidence layer's ``git log --patch`` output is repo-content
sized, so the cap has to bound what is read, not what is kept afterwards.
"""

from __future__ import annotations

import sys

import pytest

from orch_hub.subprocess_utils import (
    CommandNotFoundError,
    run_command,
)

HUGE = 2_000_000
"""Output size that would be obvious in memory if the cap did not bind."""


def emit(size: int) -> list[str]:
    """Argv for a command writing ``size`` bytes to stdout and nothing to stderr."""
    return [
        sys.executable,
        "-c",
        f"import sys; sys.stdout.write('x' * {size})",
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


def test_uncapped_run_still_captures_everything_including_stderr():
    result = run_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('o'); sys.stderr.write('e')"]
    )

    assert result.stdout == "o" and result.stderr == "e"
    # Unmeasured: no cap was asked for, so no size/truncation claim is made.
    assert result.stdout_bytes is None and result.stdout_truncated is False
