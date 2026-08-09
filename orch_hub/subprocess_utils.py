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
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Optional, Union

_STREAM_CHUNK = 64 * 1024
"""Read size while draining a capped command's stdout."""


class CommandNotFoundError(FileNotFoundError):
    """Raised when the executable for a command does not exist."""


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
) -> CommandResult:
    """Run ``args`` capturing text output; never raises on non-zero exit.

    With ``max_stdout_bytes`` set the command runs in capped mode
    (:func:`_run_capped`): at most that many stdout bytes are retained, the
    remainder is drained and counted, and the result carries the true output
    size plus a truncation flag. Use it whenever the output size is attacker- or
    drift-controlled; the unbounded default stays for the small, fixed-shape
    probes (``git status``, ``aii-2 status``) whose stderr callers report.

    Raises :class:`CommandNotFoundError` when the executable is missing.
    """
    argv = [str(a) for a in args]
    if max_stdout_bytes is not None:
        return _run_capped(argv, cwd=cwd, timeout=timeout, limit=max(0, max_stdout_bytes))
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
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


def _run_capped(
    argv: list[str],
    *,
    cwd: Optional[Union[str, Path]],
    timeout: Optional[float],
    limit: int,
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
    caller's responsibility; this bounds memory, not runtime.
    """
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise CommandNotFoundError(argv[0]) from exc
    try:
        with proc.stdout as stream:  # type: ignore[union-attr] - stdout=PIPE
            head, total = _drain(stream, limit)
        returncode = proc.wait(timeout=timeout)
    except BaseException:
        # A timeout (or an interrupt) leaves a live child holding the pipe; kill
        # it before propagating so no orphan outlives this call.
        proc.kill()
        proc.wait()
        raise
    return CommandResult(
        args=tuple(argv),
        returncode=returncode,
        stdout=head.decode("utf-8", errors="replace"),
        stderr="",
        stdout_bytes=total,
        stdout_truncated=total > len(head),
    )


def _drain(stream: IO[bytes], limit: int) -> tuple[bytes, int]:
    """Read ``stream`` to EOF; return its first ``limit`` bytes and its full size.

    Decoding is left to the caller because the cut lands at an arbitrary byte
    offset and may split a multi-byte character.
    """
    head = bytearray()
    total = 0
    while True:
        chunk = stream.read(_STREAM_CHUNK)
        if not chunk:
            return bytes(head), total
        total += len(chunk)
        if len(head) < limit:
            head += chunk[: limit - len(head)]


# A runner has the same call shape as :func:`run_command`. Tests inject fakes.
Runner = Callable[..., CommandResult]
