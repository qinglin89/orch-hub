"""Tests for the shared subprocess wrapper, focused on its capped-output mode.

These are the only tests in the suite that run a real subprocess, and they run
``sys.executable`` with an inline script — never git, a managed repo, or the real
registry. A fake runner cannot show what the *producer* does with a command that
writes far more than the caller asked for, and that bound is the whole point of
the capped mode: the evidence layer's ``git log --patch`` output is repo-content
sized, so the cap has to bound what is read, not what is kept afterwards. The
timeout tests need a live child for the same reason: only a real process can
stall a read, and only a real child can show what environment it inherited or
what its stdin was connected to.

The cancellation tests need one most of all: what a canceller promises is that
another thread's blocked call *ends* and its child is gone, and neither half is
observable against a fake. The canceller's own state machine — the seal that
closes the window once a result is committed — is the exception: it needs no
child at all, only two threads racing for one decision.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from orch_hub.subprocess_utils import (
    CancelOutcome,
    CommandCancelled,
    CommandCanceller,
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


def test_uncapped_run_times_out_rather_than_waiting_for_a_stalled_child():
    # The uncapped mode is what the semantic evaluator's turn goes through, so its
    # deadline has to bind on a child that simply never finishes.
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        run_command(hold(LONG), timeout=0.2)

    assert time.monotonic() - started < PATIENCE


ECHO_ENV = [
    sys.executable,
    "-c",
    "import os, sys; sys.stdout.write(os.environ.get('ORCH_HUB_PROBE', '(absent)'))",
]
"""Argv for a command reporting one environment variable, to show what it inherited."""


@pytest.mark.parametrize("capped", [None, 1000])
def test_env_replaces_the_child_environment_in_both_modes(capped, monkeypatch):
    # The augmented-PATH callers (the semantic turn, launched by name under a
    # launchd-sparse PATH) depend on this reaching the child in whichever mode
    # they use.
    monkeypatch.setenv("ORCH_HUB_PROBE", "inherited")

    passed = run_command(ECHO_ENV, env={"ORCH_HUB_PROBE": "passed"}, max_stdout_bytes=capped)
    assert passed.stdout == "passed"

    # And omitting it still inherits, which is what every probe caller wants.
    inherited = run_command(ECHO_ENV, max_stdout_bytes=capped)
    assert inherited.stdout == "inherited"


READ_STDIN = [
    sys.executable,
    "-c",
    "import sys; sys.stdout.write(repr(sys.stdin.read()))",
]
"""Argv for a command that reads stdin to EOF — a hang, if stdin never closes."""


@pytest.mark.parametrize("capped", [None, 1000])
def test_stdin_is_devnull_so_a_reading_command_cannot_wait_forever(capped):
    # Every command this wrapper runs is a non-interactive probe. An inherited
    # stdin is another way for one to block indefinitely on input nobody will
    # type — the same unbounded wait the timeout exists to remove.
    result = run_command(READ_STDIN, timeout=PATIENCE, max_stdout_bytes=capped)

    assert result.ok is True
    assert result.stdout == "''"  # immediate EOF, not a blocked read


# -- cancellation --------------------------------------------------------------
#
# ``timeout`` is the bound a caller fixes before the call and then waits out; a
# canceller is the other half, and the semantic evaluator's operator cancel is the
# caller that needs it. What it owes: the blocked call ends, the child is gone, and
# the caller is told it was stopped rather than handed a result.


@pytest.mark.parametrize("capped", [None, 1000])
def test_a_cancelled_run_ends_and_leaves_no_child_behind(capped):
    canceller = CommandCanceller()
    timer = threading.Timer(0.1, canceller.cancel)
    timer.start()
    started = time.monotonic()

    try:
        with pytest.raises(CommandCancelled):
            run_command(hold(LONG), max_stdout_bytes=capped, cancel=canceller)
    finally:
        timer.cancel()

    # Ended when it was cancelled, not when the child would have finished.
    assert time.monotonic() - started < PATIENCE


class PidCanceller(CommandCanceller):
    """Reports which child it was handed, and when — the two facts a test needs.

    ``attached`` is what makes the cancel below a decision rather than a race: it
    fires only once a live process exists to kill.
    """

    def __init__(self):
        super().__init__()
        self.pid: int | None = None
        self.attached = threading.Event()

    def attach(self, proc) -> bool:
        adopted = super().attach(proc)
        if adopted:
            self.pid = proc.pid
            self.attached.set()
        return adopted


@pytest.mark.parametrize("capped", [None, 1000])
def test_a_cancelled_child_is_killed_and_reaped(capped):
    # The point of killing rather than merely flagging: the whole reason to cancel
    # a semantic turn is that its process is spending a subscription window, so a
    # cancel that only unblocked the caller would stop nothing that matters.
    canceller = PidCanceller()
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            run_command(hold(LONG), max_stdout_bytes=capped, cancel=canceller)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            outcome.append(exc)

    runner = threading.Thread(target=run)
    runner.start()
    assert canceller.attached.wait(PATIENCE), "no child was ever published"

    canceller.cancel()

    runner.join(PATIENCE)
    assert not runner.is_alive()
    assert outcome and isinstance(outcome[0], CommandCancelled)
    with pytest.raises(ProcessLookupError):
        os.kill(canceller.pid, 0)  # killed and reaped, not left running


@pytest.mark.parametrize("capped", [None, 1000])
def test_a_canceller_fired_before_the_call_starts_nothing_it_cannot_stop(capped):
    # Cancelling between the spawn and the child becoming reachable would otherwise
    # leave a process nobody holds a handle on.
    canceller = CommandCanceller()
    canceller.cancel()

    with pytest.raises(CommandCancelled):
        run_command(hold(LONG), max_stdout_bytes=capped, cancel=canceller)


@pytest.mark.parametrize("capped", [None, 1000])
def test_an_uncancelled_run_with_a_canceller_behaves_exactly_as_before(capped):
    result = run_command(emit(10), max_stdout_bytes=capped, cancel=CommandCanceller())

    assert result.ok is True and result.stdout == "x" * 10


def test_an_uncancelled_run_still_captures_stderr():
    # The uncapped mode is what the semantic turn uses, and its stderr tail is the
    # bounded diagnostic a *failed* turn reports.
    result = run_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('o'); sys.stderr.write('e')"],
        cancel=CommandCanceller(),
    )

    assert result.stdout == "o" and result.stderr == "e"


def test_a_timeout_still_binds_when_a_canceller_is_attached():
    # The two bounds are independent: nobody cancelled, so this is a deadline
    # expiring, and reporting it as a cancel would name the wrong cause.
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(hold(LONG), timeout=0.2, cancel=CommandCanceller())


class LateCanceller(CommandCanceller):
    """Fires exactly as the run finishes — the race window, made deterministic."""

    def detach(self) -> None:
        self.cancel()
        super().detach()


@pytest.mark.parametrize("capped", [None, 1000])
def test_a_cancel_landing_as_the_command_finishes_is_still_a_cancel(capped):
    # The caller has already stopped waiting for this result, and for the semantic
    # turn that matters concretely: a report parsed and published out of a turn the
    # operator cancelled would outlive the record saying it was stopped.
    with pytest.raises(CommandCancelled):
        run_command(emit(10), max_stdout_bytes=capped, cancel=LateCanceller())


# -- the end of the cancellation window ---------------------------------------
#
# A cancel stops being meaningful once the caller holds the result and is acting
# on it: writing it, publishing it, settling a record on it. The seal is that
# end, and it has to be a transition rather than a flag anybody reads, because
# the whole difficulty is the instant where a cancel and a commit arrive
# together. These cover the primitive; the semantic evaluator and job manager
# are the callers that depend on it (tests/test_semantic.py,
# tests/test_semantic_jobs.py).


def test_a_seal_closes_the_window_and_refuses_every_later_cancel():
    canceller = CommandCanceller()

    assert canceller.seal() is True

    # Refused rather than recorded: an asker told TOO_LATE can report the truth
    # ("already finished") instead of a stop that did not happen.
    assert canceller.cancel() is CancelOutcome.TOO_LATE
    assert canceller.cancelled is False


def test_a_cancel_that_arrived_first_refuses_the_seal():
    canceller = CommandCanceller()

    assert canceller.cancel() is CancelOutcome.NOTHING_TO_STOP

    # The caller must discard the result rather than commit to it: the record
    # already says stopped, so persisting it anyway is the divergence.
    assert canceller.seal() is False
    assert canceller.cancelled is True


def test_only_one_of_cancel_and_seal_can_win_under_concurrency():
    """The two transitions are exclusive however many threads race for them.

    Not a timing test: whichever lands first, the other's answer must agree with
    it. A window that could be both sealed and cancelled is exactly the state in
    which a record and the artifacts behind it can disagree.
    """
    for _ in range(200):
        canceller = CommandCanceller()
        outcomes: list[object] = []
        barrier = threading.Barrier(2)

        def do(action):
            barrier.wait()
            outcomes.append(action())

        threads = [
            threading.Thread(target=do, args=(canceller.cancel,)),
            threading.Thread(target=do, args=(canceller.seal,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(PATIENCE)

        sealed = True in outcomes
        cancelled = CancelOutcome.NOTHING_TO_STOP in outcomes
        assert sealed != cancelled  # exactly one, never both and never neither
        assert canceller.cancelled is cancelled
