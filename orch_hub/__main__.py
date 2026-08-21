"""Executable entry point shared by ``python -m orch_hub`` and compiled releases.

The freezer that builds a standalone release compiles *this* file as the
program's ``__main__``, so a compiled runtime and ``python -m orch_hub`` run the
same dispatch over the same command surface — there is no release-only code
path to keep in step with the CLI.

Imports here are absolute, not package-relative: a frozen entry script runs as
a top-level ``__main__`` with no package context, where a relative import has
nothing to resolve against.
"""

from __future__ import annotations

import multiprocessing
import sys

from orch_hub.cli import main


def _run() -> int:
    # A frozen process re-executes its own binary to create a child; without
    # this call that child would re-run the CLI instead of the worker body.
    # orch-hub spawns orchestrators with subprocess, not multiprocessing, so
    # this is a guard against a future import pulling one in, and a no-op
    # everywhere else.
    multiprocessing.freeze_support()
    return main()


if __name__ == "__main__":
    sys.exit(_run())
