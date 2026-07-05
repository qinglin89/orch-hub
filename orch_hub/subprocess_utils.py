"""Thin, consistent wrapper around subprocess execution.

Callers pass an argument list (never a shell string) and receive a
:class:`CommandResult`. Non-zero exits are returned, not raised, so readiness
and registry callers can decide how to report them; a missing executable is
surfaced as :class:`CommandNotFoundError` so "command unavailable" is
distinguishable from "command failed".
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union


class CommandNotFoundError(FileNotFoundError):
    """Raised when the executable for a command does not exist."""


@dataclass(frozen=True)
class CommandResult:
    """Result of a finished subprocess."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_command(
    args: Sequence[Union[str, Path]],
    *,
    cwd: Optional[Union[str, Path]] = None,
    timeout: Optional[float] = None,
) -> CommandResult:
    """Run ``args`` capturing text output; never raises on non-zero exit.

    Raises :class:`CommandNotFoundError` when the executable is missing.
    """
    argv = [str(a) for a in args]
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


# A runner has the same call shape as :func:`run_command`. Tests inject fakes.
Runner = Callable[..., CommandResult]
