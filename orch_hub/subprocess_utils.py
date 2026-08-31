"""Thin, consistent wrapper around subprocess execution.

Callers pass an argument list (never a shell string) and receive a
:class:`CommandResult`. Non-zero exits are returned, not raised, so readiness
and registry callers can decide how to report them; a missing executable is
surfaced as :class:`CommandNotFoundError` so "command unavailable" is
distinguishable from "command failed".

Output is captured whole by default, which is right for the status/registry
probes this wrapper was built for. A command whose output size is a function of
repo content — ``git log --patch`` for the evidence layer — passes
``max_stdout_bytes`` instead: the capped mode streams stdout, retains only that
many bytes, and drains the rest, so a caller's byte cap bounds what is *read*
rather than what is kept after an unbounded read.

A caller that must be able to stop a call it already started passes a
:class:`CommandCanceller`. ``timeout`` is the bound a caller sets in advance and
then waits out; a canceller is the other half — somebody deciding mid-call that
the work should stop now — and the two are kept apart because they end the call
with different exceptions and mean different things to whoever reads the result.
"""

from __future__ import annotations

import os
import selectors
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Optional, Union

_STREAM_CHUNK = 64 * 1024
"""Read size while draining a capped command's stdout."""


class CommandNotFoundError(FileNotFoundError):
    """Raised when the executable for a command does not exist."""


class CommandCancelled(Exception):
    """Raised when a run was stopped by its canceller instead of finishing.

    Deliberately not a :class:`subprocess.TimeoutExpired`: a timeout is the bound
    the caller set expiring on work nobody asked to stop, while this is somebody
    asking. They are reported differently at every level above — the semantic
    evaluator names the deadline in one case and the operator's cancel in the
    other — so a single exception would force every reader to guess which
    happened.

    Its message carries the executable name only, never the argv: one caller's
    last argument is a whole evidence prompt, and a diagnostic that stringified it
    would write task content into a persisted job record (the same leak
    :meth:`orch_hub.semantic.ClaudeSemanticBackend.evaluate` already translates
    ``TimeoutExpired`` to avoid).
    """


class CommandCanceller:
    """Handle letting one thread stop a command another thread is blocked running.

    :func:`subprocess.run` hands back no handle on its child, so a thread blocked
    in it cannot be reached: the only lever anyone else has is the ``timeout``
    that was fixed before the call. This publishes the missing one. The running
    thread attaches its child (:meth:`attach`), any other thread may
    :meth:`cancel`, and the run raises :class:`CommandCancelled` rather than
    returning a result.

    One canceller belongs to exactly one call (the semantic job manager mints one
    per evaluation attempt), and a fired one stays fired: :meth:`attach` refuses
    after a cancel rather than adopting a child nobody wants, which is what closes
    the window between the spawn and the child becoming reachable.

    Cancelling is also meaningful *before* a child exists — a caller may be doing
    expensive work of its own on the way to the command — so :attr:`cancelled` is
    the cooperative half of the same signal, checked by callers at whatever points
    they can abort cheaply.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        """Whether :meth:`cancel` has fired. Safe from any thread."""
        with self._lock:
            return self._cancelled

    def cancel(self) -> bool:
        """Stop the run; return whether a live child was actually signalled.

        ``False`` means there was nothing to kill — the command had not started
        yet, or had already finished — which is not a failure: the flag is set
        either way, so a run that has not reached its child refuses to start one
        and a caller between phases aborts at its next checkpoint.

        The kill happens under the lock so it cannot race an :meth:`attach` or
        :meth:`detach` from the running thread; ``os.kill`` does not block, so the
        section stays short. A child that has already exited raises
        :class:`ProcessLookupError`, which is the goal reached by other means and
        reads as ``False``. Any other :class:`OSError` propagates: the caller asked
        for the work to stop and it demonstrably did not, and reporting that as a
        successful cancel would be the lie this returns a value to avoid.
        """
        with self._lock:
            self._cancelled = True
            proc = self._proc
            if proc is None:
                return False
            try:
                proc.kill()
            except ProcessLookupError:
                return False
            return True

    def attach(self, proc: subprocess.Popen) -> bool:
        """Publish the running child; ``False`` when a cancel already fired.

        A ``False`` return means the caller must not run the child it just spawned
        — the decision to stop was taken while it was starting — and is the reason
        this returns anything at all.
        """
        with self._lock:
            if self._cancelled:
                return False
            self._proc = proc
            return True

    def detach(self) -> None:
        """Withdraw the child once it can no longer be signalled usefully."""
        with self._lock:
            self._proc = None


@dataclass(frozen=True)
class CommandResult:
    """Result of a finished subprocess.

    ``stdout_bytes`` and ``stdout_truncated`` are populated only by the capped
    mode (:func:`run_command` with ``max_stdout_bytes``): the first is the total
    number of bytes the command wrote, the second says ``stdout`` holds just the
    head of it. ``stdout_bytes`` of ``None`` therefore means "nobody measured" —
    an unbounded capture, or a test fake — which is why consumers of a capped
    read must still bound the string themselves when it is absent
    (:func:`orch_hub.git_meta.read_path_diff_between`).
    """

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    stdout_bytes: Optional[int] = None
    stdout_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(
    args: Sequence[Union[str, Path]],
    *,
    cwd: Optional[Union[str, Path]] = None,
    timeout: Optional[float] = None,
    max_stdout_bytes: Optional[int] = None,
    env: Optional[Mapping[str, str]] = None,
    cancel: Optional[CommandCanceller] = None,
) -> CommandResult:
    """Run ``args`` capturing text output; never raises on non-zero exit.

    With ``max_stdout_bytes`` set the command runs in capped mode
    (:func:`_run_capped`): at most that many stdout bytes are retained, the
    remainder is drained and counted, and the result carries the true output
    size plus a truncation flag. Use it whenever the output size is attacker- or
    drift-controlled; the unbounded default stays for the small, fixed-shape
    probes (``git status``, ``mandrel status``) whose stderr callers report.

    ``timeout`` means the same thing in both modes: it bounds the whole call and
    raises :class:`subprocess.TimeoutExpired` when it expires. Callers can
    therefore combine it with ``max_stdout_bytes`` to bound memory and runtime
    at once.

    ``env`` replaces the child's whole environment (``None`` inherits this
    process's, the default every probe wants). It exists for the one caller class
    that cannot rely on inheritance: a hub running under launchd has a sparse
    PATH while the tools it launches by name live under the user's home, so those
    callers pass the augmented environment
    (:func:`orch_hub.runs._orchestrator_env`) the detached run paths already use.

    stdin is always ``DEVNULL``. Every command this wrapper runs is a
    non-interactive probe, and an inherited stdin is a way for one to wait
    forever on input nobody will type — the same unbounded wait ``timeout``
    exists to remove. EOF makes such a command fail promptly instead.

    ``cancel`` makes the call stoppable from another thread: the child is
    published on the canceller while it runs, and a fired canceller raises
    :class:`CommandCancelled` instead of returning a result — including when it
    fires while the command was finishing, so a caller that asked to stop never
    receives output it has already stopped acting on. Passing one switches the
    uncapped mode off :func:`subprocess.run`, which owns its child privately;
    the no-canceller path is left on it deliberately, so the wrapper every probe
    in the hub uses keeps exactly the semantics it always had.

    Raises :class:`CommandNotFoundError` when the executable is missing.
    """
    argv = [str(a) for a in args]
    if max_stdout_bytes is not None:
        return _run_capped(
            argv,
            cwd=cwd,
            timeout=timeout,
            limit=max(0, max_stdout_bytes),
            env=env,
            cancel=cancel,
        )
    if cancel is not None:
        return _run_cancellable(argv, cwd=cwd, timeout=timeout, env=env, cancel=cancel)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CommandNotFoundError(argv[0]) from exc
    return CommandResult(
        args=tuple(argv),
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _run_cancellable(
    argv: list[str],
    *,
    cwd: Optional[Union[str, Path]],
    timeout: Optional[float],
    env: Optional[Mapping[str, str]],
    cancel: CommandCanceller,
) -> CommandResult:
    """:func:`subprocess.run`'s whole-output capture, over a killable child.

    Reached only when a caller supplies a canceller. It is a re-implementation
    rather than a wrapper because :func:`subprocess.run` keeps its
    :class:`subprocess.Popen` private, and it is a *separate path* rather than a
    replacement because every readiness/registry/git probe in the hub goes through
    the other one: putting all of them behind this change to serve one caller
    would trade a contained addition for a hub-wide behavior risk.

    ``communicate`` needs no cancellation polling of its own — killing the child
    closes its pipes, so the blocked read returns at once — which is why the
    canceller kills rather than merely flagging.
    """
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CommandNotFoundError(argv[0]) from exc
    with proc:
        if not cancel.attach(proc):
            # Cancelled between the spawn and the publish: no other thread can see
            # this child yet, so this one kills it.
            proc.kill()
            proc.wait()
            raise CommandCancelled(_cancelled_detail(argv))
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Same shape `subprocess.run` produces: kill, drain what was written,
            # and re-raise carrying the partial output.
            proc.kill()
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(
                argv, timeout, output=stdout, stderr=stderr  # type: ignore[arg-type]
            )
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            cancel.detach()
    if cancel.cancelled:
        raise CommandCancelled(_cancelled_detail(argv))
    return CommandResult(
        args=tuple(argv), returncode=proc.returncode, stdout=stdout, stderr=stderr
    )


def _cancelled_detail(argv: list[str]) -> str:
    """Cancellation diagnostic naming the executable and nothing else.

    The argv is deliberately absent: one caller's last argument is a whole
    evidence prompt, and this string reaches a persisted job record.
    """
    return f"{argv[0]}: cancelled before it finished"


def _run_capped(
    argv: list[str],
    *,
    cwd: Optional[Union[str, Path]],
    timeout: Optional[float],
    limit: int,
    env: Optional[Mapping[str, str]] = None,
    cancel: Optional[CommandCanceller] = None,
) -> CommandResult:
    """Run ``argv``, retaining only the first ``limit`` bytes of its stdout.

    Streams stdout in chunks and keeps the head, so peak memory is the cap plus
    one chunk no matter how much the command produces. The tail is *drained*
    rather than abandoned: closing the pipe early would kill the command with
    SIGPIPE and turn a successful-but-large read into a failed one, discarding
    the head already in hand. Draining also yields the true output size, which
    is what tells a consumer its evidence was cut.

    stderr goes to ``DEVNULL``, and that is a deliberate limitation of this mode
    rather than an oversight: bounding one pipe while the other fills deadlocks
    (the command blocks writing stderr, so it stops writing the stdout we are
    blocked reading). Callers that need stderr diagnostics use the unbounded
    mode. The command's own bounds — for ``git log`` a commit cap — remain the
    caller's responsibility.

    ``timeout`` becomes an absolute deadline covering the drain *and* the wait,
    because the drain is where a stuck command actually stalls: a child that
    sleeps, trickles, or merely holds stdout open blocks the read, and a timeout
    checked only afterwards would be reached once the command had already
    finished taking as long as it liked.
    """
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise CommandNotFoundError(argv[0]) from exc
    deadline = None if timeout is None else time.monotonic() + timeout
    if cancel is not None and not cancel.attach(proc):
        proc.kill()
        proc.wait()
        raise CommandCancelled(_cancelled_detail(argv))
    try:
        with proc.stdout as stream:  # type: ignore[union-attr] - stdout=PIPE
            head, total, expired = _drain(stream, limit, deadline)
        if expired:
            # Same exception the uncapped mode raises, carrying the head read so
            # far: partial evidence, like `subprocess.run`'s own `output`.
            raise subprocess.TimeoutExpired(
                argv,
                timeout,  # type: ignore[arg-type] - a deadline implies a timeout
                output=head.decode("utf-8", errors="replace"),
            )
        returncode = proc.wait(timeout=_remaining(deadline))
    except BaseException:
        # A timeout (or an interrupt) leaves a live child holding the pipe; kill
        # it before propagating so no orphan outlives this call.
        proc.kill()
        proc.wait()
        raise
    finally:
        if cancel is not None:
            cancel.detach()
    # A cancelled child closes its pipe, so the drain above ends at EOF like any
    # other finished command; only the canceller says which of the two it was.
    if cancel is not None and cancel.cancelled:
        raise CommandCancelled(_cancelled_detail(argv))
    return CommandResult(
        args=tuple(argv),
        returncode=returncode,
        stdout=head.decode("utf-8", errors="replace"),
        stderr="",
        stdout_bytes=total,
        stdout_truncated=total > len(head),
    )


def _drain(
    stream: IO[bytes], limit: int, deadline: Optional[float]
) -> tuple[bytes, int, bool]:
    """Read ``stream`` to EOF; return its first ``limit`` bytes, its full size,
    and whether ``deadline`` expired first.

    Every read waits for readability first, so ``deadline`` bounds the whole
    drain rather than any single read. It is absolute, not refreshed per chunk:
    a command trickling output forever stops at the caller's timeout, not at the
    first quiet gap. Readability is answered with exactly one ``os.read`` — a
    buffered ``read(n)`` blocks until it has all ``n`` bytes, which would put an
    unbounded blocking read back inside the bounded loop.

    Decoding is left to the caller because the cut lands at an arbitrary byte
    offset and may split a multi-byte character.
    """
    head = bytearray()
    total = 0
    fd = stream.fileno()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            if not selector.select(_remaining(deadline)):
                return bytes(head), total, True
            chunk = os.read(fd, _STREAM_CHUNK)
            if not chunk:
                return bytes(head), total, False
            total += len(chunk)
            if len(head) < limit:
                head += chunk[: limit - len(head)]


def _remaining(deadline: Optional[float]) -> Optional[float]:
    """Seconds left until ``deadline``; ``None`` when there is no deadline.

    Clamped at zero so a deadline already passed becomes a non-blocking poll
    instead of a negative timeout.
    """
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


# A runner has the same call shape as :func:`run_command`. Tests inject fakes.
Runner = Callable[..., CommandResult]
