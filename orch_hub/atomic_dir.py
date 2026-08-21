"""Replace one directory with another without ever publishing a partial tree.

Two places in orch-hub put a freshly produced directory at a path something
else consumes: the release build publishes a payload under its target-keyed
name, and the installer publishes a version under the install prefix. Both have
the same failure to avoid — a consumer finding a half-written tree at the
canonical path — and the same recovery to guarantee: if the new tree cannot be
placed, the previous one is still there.

The mechanics that make that true are subtle enough to be worth one
implementation rather than two:

- **Only one step may cross a filesystem.** A copy across devices can fail
  partway and leaves as much as it managed to write; ``shutil.copytree`` does
  not even stop at the first error, so the debris includes truncated files.
  That step therefore lands under a sibling *incoming* name, never at the
  target, and the target is reached by a rename within one directory — which
  either happens completely or not at all.
- **The replaced tree is moved aside, not deleted.** It goes to a sibling
  *previous* name and comes back if the swap fails, so an interrupted
  publication cannot leave the target empty, partial, or holding a tree that
  was rejected. It is removed only once the new one is in place.
- **Both scratch names are cleared before anything moves**, so finding one in
  an error path proves this publication put it there and undoing is safe.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

INCOMING_SUFFIX = ".incoming"
"""Suffix the new tree is brought onto the target filesystem under."""

PREVIOUS_SUFFIX = ".previous"
"""Suffix the tree being replaced is held under while it is replaced."""

Transfer = Callable[[Path, Path], None]
"""Moves or copies the source tree to a destination that does not exist yet.

The only step allowed to cross filesystems, and therefore the only step that
can leave debris behind."""


class PlacementError(RuntimeError):
    """Raised when a directory cannot be placed at its target path."""


def discard_path(path: Path, *, best_effort: bool = False) -> None:
    """Remove one of publication's scratch paths, directory or not.

    Both of them are transient by construction: a stale one is debris from an
    interrupted publication and a fresh one belongs to this publication, so
    neither is ever something to keep. Whether it is a directory is not assumed
    — once a filesystem error is in play, what is on disk is whatever the
    failure left.
    """
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.is_symlink() or path.exists():
            path.unlink()
    except OSError as exc:
        if best_effort:
            return
        raise PlacementError(f"cannot clear {path}: {exc}") from exc


def place_directory(
    source: Path,
    target: Path,
    *,
    transfer: Transfer,
    what: str,
) -> Path:
    """Publish ``source`` at ``target``, leaving the previous tree on failure.

    ``transfer`` moves or copies ``source`` onto the target's filesystem; the
    caller chooses because a build consumes its staged tree while an installer
    must leave its source payload intact. ``what`` names the thing being placed
    in error messages ("payload", "version 0.2.0"), so a failure reads as the
    operation the operator ran rather than as a filesystem primitive.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    incoming = target.with_name(f"{target.name}{INCOMING_SUFFIX}")
    previous = target.with_name(f"{target.name}{PREVIOUS_SUFFIX}")
    # Cleared before anything moves, so that finding either of them below means
    # this publication put it there.
    discard_path(incoming)
    discard_path(previous)

    try:
        transfer(source, incoming)
    except OSError as exc:
        discard_path(incoming, best_effort=True)
        raise PlacementError(f"cannot stage the {what} at {incoming}: {exc}") from exc

    replaced = target.exists()
    try:
        if replaced:
            target.rename(previous)
        incoming.rename(target)
    except OSError as exc:
        discard_path(incoming, best_effort=True)
        stranded = ""
        # A rename leaves nothing behind, so the old tree is either still in
        # place or whole under its held name; only the second case needs undoing.
        if replaced and previous.exists() and not target.exists():
            try:
                previous.rename(target)
            except OSError:
                stranded = f"; the previous {what} is left at {previous}"
        raise PlacementError(
            f"cannot place the {what} at {target}: {exc}{stranded}"
        ) from exc
    if replaced:
        discard_path(previous, best_effort=True)
    return target
