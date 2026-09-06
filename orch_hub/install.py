"""User-local installation of a compiled orch-hub runtime.

A source checkout deploys itself: ``orch-hub launchd deploy`` installs the
package into ``<checkout>/.venv`` and points launchd at the console script it
just created. An engineer who receives a release has none of that — no Python,
no virtualenv, no checkout — so the same operations have to be expressible over
a *payload directory*: the self-contained executable plus its libraries and
resources that :mod:`orch_hub.release_build` produces.

This module is that layer. Its shape is one idea:

    <prefix>/versions/<version>/     one payload per version, never mutated
    <prefix>/current -> versions/X   which one is live, swapped atomically
    <prefix>/previous               which one it was, so rollback is one word
    <bin-dir>/orch-hub -> <prefix>/current/orch-hub

Because the launchd plist names ``<prefix>/current/orch-hub``, selecting a
version is a symlink swap plus a service restart — the installed service
configuration, its token, and the hub's own data are never rewritten to change
which code runs. That is what makes an upgrade cheap and a rollback safe: the
version being replaced is still on disk under its own name, and going back to it
does not need the installer, the network, or the payload it came from.

Nothing here writes into a managed repo, a run directory, or the hub data dir.
Uninstall removes the service and the installed runtime; the hub's data survives
unless it is purged explicitly, because run history and evaluation artifacts are
not re-creatable from a release.
"""

from __future__ import annotations

import fcntl
import json
import os
import platform
import shutil
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .atomic_dir import (
    INCOMING_SUFFIX,
    PREVIOUS_SUFFIX,
    PlacementError,
    discard_path,
    place_directory,
)
from .launchd import (
    LABEL,
    Deployment,
    DeployOptions,
    LaunchdError,
    default_plist_path,
    deploy_service,
    installed_bind,
    installed_data_dir,
    restart_service,
    service_target,
    stop_service,
    wait_for_health,
)
from .resources import is_frozen
from .subprocess_utils import Runner, run_command

PREFIX_RELATIVE_PATH = Path(".local") / "orch-hub"
"""Install prefix under the user's home. User-local on purpose: a release that
needs ``sudo`` to install cannot be handed to an engineer as one command."""

BIN_RELATIVE_PATH = Path(".local") / "bin"
"""Directory the ``orch-hub`` command is linked into."""

VERSIONS_DIR = "versions"
CURRENT_LINK = "current"
PREVIOUS_MARKER = "previous"
COMMAND_NAME = "orch-hub"
MANIFEST_NAME = "runtime.json"
MANIFEST_SCHEMA = "orch-hub-runtime/1"
"""Payload self-description this installer knows how to place.

Deliberately a second copy of :mod:`orch_hub.release_build`'s constant rather
than an import: the build module is excluded from the payload by name, so an
installed runtime cannot import it. A test asserts the two stay equal, which is
the only place the duplication can go wrong."""

INCOMING_LINK = f".{CURRENT_LINK}.incoming"
"""Scratch name the selection symlink is built under before it is swapped in.

A symlink cannot be edited in place, so selection creates the new one beside the
old and renames it over: ``rename`` replaces the entry atomically, which means a
reader never sees ``current`` missing."""

REMOVING_SUFFIX = ".removing"
"""Suffix a version is renamed under before its files are deleted.

Deleting a tree in place can fail halfway and leave a directory that still looks
like an installed version but no longer runs. Moving it out of the version
namespace first is one rename inside ``versions/`` — it happens completely or
not at all — so what is left listed is always whole."""

RESERVED_VERSION_SUFFIXES = (INCOMING_SUFFIX, PREVIOUS_SUFFIX, REMOVING_SUFFIX)
"""Suffixes an installed version's name may not end with.

Placement and pruning both work beside the version they are handling, under a
sibling name built from one of these. That only stays unambiguous if the
reverse is refused: a payload declaring version ``0.1.0.removing`` would install
as a directory this module's own inventory hides as debris, uninstall waves
through as debris, and a prune of some other version could pick as its scratch
name. Reserving them costs a version a suffix nothing numbers with, and makes
"anything under ``versions/`` wearing one of these is not an installed version"
true by construction rather than by luck.

The match is case-insensitive (:func:`is_reserved_version`), because the
shipped target's filesystem is: on APFS ``0.1.0.INCOMING`` and
``0.1.0.incoming`` are one path, so a version allowed to wear the first is
recursively deleted the moment a placement clears the second. A reservation
that held only where names are case-sensitive would lapse on exactly the
filesystem where the collision is possible.

:mod:`orch_hub.release_package` applies the same reservation, so a release
cannot be published under a version this installer would then refuse."""

LOCK_NAME = ".lock"
"""File held exclusively for the length of any mutation of one prefix.

Placing, selecting, and pruning are each several filesystem steps, and retention
in particular decides what to remove from an inventory it read a moment earlier.
Without exclusion a concurrent ``rollback`` landing between that reading and the
removal would leave ``current`` pointing at a version the prune then deleted.
Everything these operations touch lives under the prefix, so one lock file there
is the whole boundary.

The unit is one mutation, not one command: ``upgrade`` releases it before it
restarts the service and ``rollback`` before it waits for health, because those
steps talk to launchd and to the running service rather than to the prefix.
What this guarantees is that no two prefix mutations interleave — not that two
commands cannot overlap, which is an operator's business to avoid."""

KEEP_VERSIONS = 3
"""How many installed versions a prefix keeps, on top of the fail-safes below.

Every version is a full ~30MB payload and nothing used to remove one, so a
prefix grew by that much on every upgrade forever. The alternatives were
keeping only ``current`` + ``previous`` (smallest, but it silently destroys the
older targets ``rollback --to`` can still name) and pruning only on an explicit
verb (safest, but it leaves the default unbounded, which is the thing being
fixed). Three is the middle: an upgrade keeps the version it just installed,
the one it replaced, and one more before that, so the two rollbacks an operator
actually reaches for survive and the prefix stops growing.

The bound is *approximate on purpose*: :func:`prune_versions` also keeps
``current``, the recorded ``previous``, and the payload the running process
executes from even when they fall outside the newest three, because removing
any of those breaks something that is in use rather than reclaiming space. A
version pruned here is not lost — it is reinstallable from its release
artifact, which the older ones are exactly as much as the newer ones."""

OWNED_PREFIX_ENTRIES = frozenset(
    {VERSIONS_DIR, CURRENT_LINK, PREVIOUS_MARKER, INCOMING_LINK, LOCK_NAME}
)
"""Everything this module ever creates directly under a prefix.

Uninstall removes the prefix recursively, so "is this ours" has to be answerable
without trusting the path it was given. Anything else present means the
directory holds something orch-hub did not put there."""


class InstallError(RuntimeError):
    """Raised when an installed runtime cannot be placed, selected, or removed."""


@dataclass(frozen=True)
class Installation:
    """What is installed under one prefix, and which version is live."""

    prefix: Path
    versions: tuple[str, ...]
    current: str | None
    previous: str | None

    @property
    def executable(self) -> Path:
        """Path launchd is pointed at: stable across upgrades by construction."""
        return self.prefix / CURRENT_LINK / COMMAND_NAME

    def to_dict(self) -> dict:
        return {
            "prefix": str(self.prefix),
            "versions": list(self.versions),
            "current": self.current,
            "previous": self.previous,
        }


@dataclass(frozen=True)
class PruneResult:
    """What retention removed from a prefix, and what it left installed."""

    prefix: Path
    removed: tuple[str, ...]
    kept: tuple[str, ...]
    problem: str | None = None
    """Why the prune did less than it set out to, or ``None``.

    ``removed`` names only versions whose files are actually gone, so a prune
    that stopped early is visible in the two lists as well as here: what it did
    not get to is still in ``kept``, and a version whose deletion failed partway
    is in neither — it is not installed any more and its bytes are not
    reclaimed, which is the state this names. Reclaiming disk is never worth
    failing an install over, so this is reported rather than raised — but it is
    reported, because a version left half-deleted, or one that could not be put
    back, is a state the operator has to know about to act on."""

    def to_dict(self) -> dict:
        return {
            "removed": list(self.removed),
            "kept": list(self.kept),
            "problem": self.problem,
        }


@dataclass(frozen=True)
class InstallResult:
    installation: Installation
    version: str
    replaced: bool
    """True when this version was already installed and has been overwritten."""

    link_path: Path | None
    link_on_path: bool
    prune: PruneResult

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "replaced": self.replaced,
            "link": str(self.link_path) if self.link_path else None,
            "link_on_path": self.link_on_path,
            "prune": self.prune.to_dict(),
            **self.installation.to_dict(),
        }


@dataclass(frozen=True)
class SetupOptions:
    """Configuration a fresh install collects, or an upgrade preserves.

    Every field left ``None`` falls back to the resolution the launchd
    deployment already owns — environment first, then the installed plist — so
    an upgrade that passes nothing keeps the configuration the running service
    was using, secrets included.
    """

    prefix: Path | None = None
    mandrel_path: Path | None = None
    token: str | None = None
    host: str | None = None
    port: int | None = None
    data_dir: Path | None = None
    plist_path: Path | None = None
    log_dir: Path | None = None


@dataclass(frozen=True)
class SetupResult:
    installation: Installation
    deployment: Deployment
    generated_token: bool
    health: str


@dataclass(frozen=True)
class UpgradeResult:
    install: InstallResult
    setup: SetupResult
    previous_version: str | None


@dataclass(frozen=True)
class RollbackResult:
    from_version: str | None
    to_version: str
    health: str


@dataclass(frozen=True)
class UninstallResult:
    prefix: Path
    prefix_removed: bool
    plist_path: Path
    plist_removed: bool
    link_path: Path | None
    link_removed: bool
    data_dir: Path | None
    data_removed: bool


def default_prefix() -> Path:
    """Prefix to operate on when the caller does not name one.

    A frozen runtime that is *already* installed answers with the prefix it is
    running from, so ``rollback``/``uninstall`` act on the installation the
    operator invoked rather than on a default path they may never have used. Any
    other runtime — a source checkout, or a payload run straight out of its
    build directory — answers with the standard location, which is also where a
    first install lands.
    """
    if is_frozen():
        executable = Path(sys.executable).resolve()
        version_dir = executable.parent
        if version_dir.parent.name == VERSIONS_DIR:
            return version_dir.parent.parent
    return Path.home() / PREFIX_RELATIVE_PATH


def default_bin_dir() -> Path:
    return Path.home() / BIN_RELATIVE_PATH


def read_payload_manifest(payload_dir: Path) -> dict:
    """Read and validate a payload's ``runtime.json``.

    The manifest is the payload's self-description, so it is also the only
    honest source for what the payload *is*: its version, its target, and the
    executable to run. Reading those instead of assuming them is what keeps a
    second target a matter of another manifest rather than another installer.

    It is also *input*: the version becomes a directory name under the prefix
    and the executable name is assumed by every installed path. Both are
    checked here, before the first filesystem write, because a manifest that
    says ``"version": "../.."`` would otherwise decide where a recursive copy
    lands.
    """
    path = payload_dir / MANIFEST_NAME
    if not path.is_file():
        raise InstallError(f"no {MANIFEST_NAME} in {payload_dir}: not an orch-hub payload")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise InstallError(f"{path} is not a JSON object")
    schema = str(manifest.get("schema") or "").strip()
    if schema != MANIFEST_SCHEMA:
        raise InstallError(
            f"{path} declares schema {schema or 'none'!r}, "
            f"but this installer places {MANIFEST_SCHEMA!r}"
        )
    name = str(manifest.get("name") or COMMAND_NAME).strip()
    if name != COMMAND_NAME:
        raise InstallError(f"{path} describes {name!r}, not an orch-hub payload")
    version = _payload_version(manifest, path)
    declared = str(manifest.get("executable") or COMMAND_NAME).strip()
    # Honouring an arbitrary executable name would mean the command link, the
    # plist program, and the self-replacement guard each had to read it back;
    # they all spell it `orch-hub`, so the contract is enforced instead.
    if declared != COMMAND_NAME:
        raise InstallError(
            f"{path} declares executable {declared!r}; "
            f"an installed runtime must be named {COMMAND_NAME!r}"
        )
    executable = payload_dir / declared
    if not executable.is_file():
        raise InstallError(f"payload has no executable at {executable}")
    return manifest


def _payload_version(manifest: dict, path: Path) -> str:
    """Version from a manifest, proven usable as a single directory name.

    ``<prefix>/versions/<version>`` is built by joining, and joining an absolute
    or ``..``-bearing component silently discards the prefix. A version that
    survives this is also one ``read_current`` can read back out of the
    selection link, which a version carrying a separator would not be.

    It is a name this module's own scratch paths will not collide with, too:
    those are formed by suffixing a version, so a version wearing one of the
    reserved suffixes would install as a directory that reads as debris.
    """
    version = str(manifest.get("version") or "").strip()
    if not version:
        raise InstallError(f"{path} declares no version")
    if version != Path(version).name or "\0" in version or version.startswith("."):
        raise InstallError(
            f"{path} declares version {version!r}; a version must be a single "
            "path component naming a directory under versions/"
        )
    if is_reserved_version(version):
        raise InstallError(
            f"{path} declares version {version!r}; a version may not end with "
            f"{' or '.join(RESERVED_VERSION_SUFFIXES)} in any capitalisation, "
            "which name this installer's own scratch paths beside a version"
        )
    return version


def is_reserved_version(name: str) -> bool:
    """Whether ``name`` falls in the namespace this module keeps for scratch.

    One predicate for two questions that must never disagree: a payload
    declaring such a version is refused, and a directory found under
    ``versions/`` wearing such a name is debris rather than an installed
    version — skipped by :func:`read_installation`, so it is never offered as a
    rollback target or allowed to occupy a retention slot, and waved through by
    uninstall's ownership proof rather than read as evidence of a foreign
    prefix.

    Deciding "debris or version" by name is safe only because the same names
    are refused as versions, and only on a case-insensitive filesystem if the
    refusal covers every capitalisation of them
    (:data:`RESERVED_VERSION_SUFFIXES`).
    """
    return name.lower().endswith(RESERVED_VERSION_SUFFIXES)


def check_target(
    manifest: dict,
    *,
    system: str | None = None,
    machine: str | None = None,
    macos: str | None = None,
) -> None:
    """Refuse a payload this machine cannot run, before anything is written.

    The payload's declared target is compared against the host rather than
    against a hardcoded ``darwin-arm64``: the installer then accepts any target
    it is actually running on, so shipping a second one is a build concern only.
    The minimum macOS is a property of the interpreter the payload carries, read
    out of its Mach-O headers at build time — an older Mac fails here with the
    version it needs instead of on a dynamic-loader error at first launch.
    """
    payload_os = str(manifest.get("os") or "").strip().lower()
    payload_arch = str(manifest.get("arch") or "").strip().lower()
    if not payload_os or not payload_arch:
        raise InstallError("payload manifest does not declare an os/arch target")
    host_os = (system if system is not None else platform.system()).lower()
    host_arch = (machine if machine is not None else platform.machine()).lower()
    if (payload_os, payload_arch) != (host_os, host_arch):
        raise InstallError(
            f"this payload targets {payload_os}-{payload_arch}, "
            f"but this machine is {host_os}-{host_arch}"
        )
    minimum = str(manifest.get("min_macos") or "").strip()
    if payload_os != "darwin" or not minimum:
        return
    host_macos = macos if macos is not None else platform.mac_ver()[0]
    required = _version_tuple(minimum)
    running = _version_tuple(host_macos)
    # An unparseable version on either side is not evidence of a mismatch; the
    # payload still runs or fails on its own terms rather than being refused
    # over a string this check could not read.
    if required and running and running < required:
        raise InstallError(
            f"this payload requires macOS {minimum} or newer, "
            f"but this machine runs {host_macos}"
        )


def read_installation(prefix: Path | None = None) -> Installation:
    """Describe what is installed under ``prefix`` without changing anything.

    Placement and pruning both stage under scratch names beside the version
    they are working on, and a kill mid-operation leaves one behind. Those are
    skipped: listing one as an installed version would offer a partial tree as
    a rollback target and let it occupy a retention slot.
    """
    root = _resolve_prefix(prefix)
    versions_dir = root / VERSIONS_DIR
    versions: list[str] = []
    if versions_dir.is_dir():
        versions = sorted(
            (
                entry.name
                for entry in versions_dir.iterdir()
                if entry.is_dir() and not is_reserved_version(entry.name)
            ),
            key=_version_sort_key,
        )
    return Installation(
        prefix=root,
        versions=tuple(versions),
        current=read_current(root),
        previous=read_previous(root),
    )


def read_current(prefix: Path) -> str | None:
    """Version the ``current`` link selects, or ``None`` when nothing is selected.

    Only a link that points at this prefix's own ``versions/<name>`` counts. A
    dangling or foreign link is reported as "nothing selected" rather than as a
    version, so a broken selection cannot be rolled back *to*.
    """
    link = prefix / CURRENT_LINK
    try:
        raw = Path(os.readlink(link))
    except OSError:
        return None
    if raw.parent.name != VERSIONS_DIR or not raw.name:
        return None
    expected = prefix / VERSIONS_DIR / raw.name
    # An absolute link can name a *different* prefix's identically-numbered
    # version; reading it as ours would report a selection this prefix does not
    # have and offer it as a rollback target.
    if (raw if raw.is_absolute() else prefix / raw) != expected:
        return None
    return raw.name if expected.is_dir() else None


def read_previous(prefix: Path) -> str | None:
    """Version selected before the current one, if it is still installed."""
    marker = prefix / PREVIOUS_MARKER
    try:
        name = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name or not (prefix / VERSIONS_DIR / name).is_dir():
        return None
    return name


def select_version(prefix: Path, version: str) -> str | None:
    """Point ``current`` at ``version`` and return what it pointed at before.

    The swap is a rename over the existing link, so a concurrent reader sees the
    old selection or the new one and never a missing ``current``. Selecting the
    version that is already current is a no-op for the previous marker: it would
    otherwise erase the only thing rollback has to go back to.

    Held against every other mutation of the prefix, so that "this version is
    installed" stays true from the check below until the link names it —
    retention would otherwise be free to remove it in between.
    """
    root = prefix.expanduser()
    with _prefix_lock(root):
        target = root / VERSIONS_DIR / version
        if not target.is_dir():
            raise InstallError(f"version {version} is not installed under {root}")
        link = root / CURRENT_LINK
        if link.exists() and not link.is_symlink():
            raise InstallError(
                f"{link} exists and is not a symlink; remove it and reinstall"
            )
        before = read_current(root)
        incoming = root / INCOMING_LINK
        discard_path(incoming)
        try:
            # Relative, so the whole prefix can be moved or copied without the
            # selection pointing back at where it was created.
            incoming.symlink_to(Path(VERSIONS_DIR) / version)
            os.replace(incoming, link)
        except OSError as exc:
            discard_path(incoming, best_effort=True)
            raise InstallError(f"cannot select version {version}: {exc}") from exc
        if before is not None and before != version:
            _write_previous(root, before)
        return before


def prune_versions(
    prefix: Path | None = None, *, keep: int = KEEP_VERSIONS
) -> PruneResult:
    """Remove installed versions the retention policy no longer keeps.

    The policy is the newest ``keep`` versions, and on top of that three things
    are never candidates however old they are:

    - the selected ``current`` version, which is the program launchd runs;
    - the version the ``previous`` marker names, which is what a bare
      ``rollback`` goes back to;
    - the payload this process is executing from, when it is a frozen runtime —
      deleting it would pull files out from under the running command, and the
      failure would surface later and somewhere else.

    Nothing under the prefix that is not an installed version is touched.
    Candidacy is decided by the same test uninstall uses to prove ownership, so
    a foreign directory, a half-copied tree, or scratch from an interrupted
    install is left alone rather than deleted on the strength of sitting in
    ``versions/``. Such an entry does not occupy a retention slot either.

    Removal is two-phase, because a ``rmtree`` that fails partway would leave a
    directory that still looks installed and no longer runs. Every doomed
    version is first renamed aside — one rename inside ``versions/``, so it
    either leaves the version set completely or not at all — and only then are
    the files deleted. A rename that fails puts back the ones already moved and
    removes nothing, so a refused prune leaves every version selectable.

    A version's files are deleted only after its whole tree has been surveyed
    for the permission the deletion needs, so the ordinary reason a payload
    will not delete is found while it is still whole: it goes back untouched
    and the prune stops there rather than working through the rest. Should a
    deletion fail anyway, the tree is measured against that survey — unchanged
    means nothing was lost and the version goes back too. Only a tree the
    deletion had really started on stays out; it is no longer a version anybody
    could run, it is not reported as removed, and its path is named so the
    bytes can be dealt with by hand.

    The whole transaction runs under the prefix lock. Which versions are in use
    is read once at the top, and a selection arriving in the middle would
    otherwise make that reading wrong precisely where it matters.
    """
    root = _resolve_prefix(prefix)
    try:
        with _prefix_lock(root):
            return _prune_locked(root, keep=keep)
    except InstallError as exc:
        # Reclaiming disk is never worth failing the operation that triggered
        # it, and an unlockable prefix is somebody else's mutation in flight or
        # a prefix this process cannot write: both are "not now", not an error.
        return PruneResult(
            prefix=root,
            removed=(),
            kept=read_installation(root).versions,
            problem=f"kept every version: {exc}",
        )


def _prune_locked(root: Path, *, keep: int) -> PruneResult:
    """Body of :func:`prune_versions`, with the prefix already held."""
    installation = read_installation(root)
    versions_dir = root / VERSIONS_DIR
    doomed = _prunable_versions(installation, keep=keep)
    if not doomed:
        return PruneResult(prefix=root, removed=(), kept=installation.versions)

    staged: list[tuple[str, Path]] = []
    for name in doomed:
        scratch = versions_dir / f"{name}.{uuid.uuid4().hex[:8]}{REMOVING_SUFFIX}"
        try:
            # Nothing is ever cleared to make room: an existing path here is
            # not this prune's, so it is not this prune's to delete. The name
            # carries a fresh token so that debris from an earlier failure —
            # or anything else sitting under `versions/` — neither blocks a
            # prune nor gets recursively removed by one.
            if os.path.lexists(scratch):
                raise OSError(f"{scratch} already exists")
            (versions_dir / name).rename(scratch)
        except OSError as exc:
            stranded = _restore_staged(staged)
            note = (
                "; could not put back " + ", ".join(str(path) for path in stranded)
                if stranded
                else ""
            )
            return PruneResult(
                prefix=root,
                removed=(),
                kept=read_installation(root).versions,
                problem=f"kept every version: cannot remove {name}: {exc}{note}",
            )
        staged.append((name, scratch))

    removed: list[str] = []
    problem: str | None = None
    for index, (name, scratch) in enumerate(staged):
        try:
            _delete_staged(scratch)
        except _RemovalFailed as failure:
            problem = _abandon_removal(name, scratch, failure, staged[index + 1 :])
            break
        removed.append(name)
    return PruneResult(
        prefix=root,
        removed=tuple(removed),
        kept=read_installation(root).versions,
        problem=problem,
    )


class _RemovalFailed(Exception):
    """One staged version would not delete, and whether it is still whole.

    ``intact`` is the one thing the caller has to know and cannot work out for
    itself: whether putting the tree back restores a runnable version or lists
    one that would not run.
    """

    def __init__(self, reason: str, *, intact: bool) -> None:
        super().__init__(reason)
        self.intact = intact


def _delete_staged(scratch: Path) -> None:
    """Delete one moved-aside version, refusing before it damages anything.

    A ``rmtree`` that fails partway leaves a payload nothing can put back, so
    everything knowable beforehand is established first: the tree is walked and
    every directory in it checked for the write and execute permission that
    unlinking its entries needs. The ordinary reason a version will not delete
    — something in it is read-only, or it cannot be read at all — is therefore
    found while the payload is still whole, and it goes back untouched.

    That survey also counts the tree. Nothing else writes into a staged tree —
    it is out of the version namespace and the prefix is locked — and deleting
    only ever removes entries, so an unchanged count after a failure proves the
    payload is still whole. Nothing about the tree itself proves that: an
    executable and a manifest can outlive the libraries they need, so a
    surviving payload *shape* is not evidence that nothing was deleted.
    """
    try:
        counted = _survey_removal(scratch)
    except OSError as exc:
        raise _RemovalFailed(str(exc), intact=True) from exc
    try:
        shutil.rmtree(scratch)
    except OSError as exc:
        raise _RemovalFailed(str(exc), intact=_is_unchanged(scratch, counted)) from exc


def _survey_removal(root: Path) -> int:
    """Count the entries under ``root``, refusing a tree that will not delete.

    Unlinking an entry needs write and execute permission on the *directory*
    holding it, so that is what is checked: every directory in the tree, plus
    the one ``root`` sits in, since the last step of the deletion is removing
    ``root`` from it. A directory the walk cannot read raises here too.

    A pre-check, not a guarantee: it answers for the process's real user id and
    can be stale by the time the deletion runs. Wrong in the permissive
    direction, it falls through to the deletion's own failure path, which
    reports what it finds rather than assuming; wrong in the restrictive
    direction, it refuses a prune, which costs disk and nothing else.
    """
    for directory in (root.parent, root):
        if not os.access(directory, os.W_OK | os.X_OK):
            raise PermissionError(f"{directory} does not allow removing its entries")
    total = 0
    for entry in _tree_entries(root):
        total += 1
        if entry.is_dir(follow_symlinks=False) and not os.access(
            entry.path, os.W_OK | os.X_OK
        ):
            raise PermissionError(f"{entry.path} does not allow removing its entries")
    return total


def _is_unchanged(root: Path, counted: int) -> bool:
    """Whether ``root`` still holds every entry the survey counted."""
    total = 0
    try:
        for _ in _tree_entries(root):
            total += 1
    except OSError:
        # A tree that cannot be walked cannot be proven whole, and that is the
        # side to be wrong on: putting a half-deleted payload back would offer
        # a version that does not run.
        return False
    return total == counted


def _tree_entries(root: Path) -> Iterator[os.DirEntry]:
    """Every entry under ``root``, without following symlinks out of it."""
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                yield entry
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


def _abandon_removal(
    name: str,
    scratch: Path,
    failure: _RemovalFailed,
    pending: list[tuple[str, Path]],
) -> str:
    """Give up a prune at ``name``, returning what to report about it.

    A failed deletion must not cost the operator versions it never got to:
    everything after this one is untouched and goes straight back, and so does
    this one whenever it is still whole — which, after the survey
    :func:`_delete_staged` runs first, is what a refusal normally means. Only a
    tree the deletion had really started on stays out, because putting *that*
    back would list something as installed that cannot run.
    """
    putback = [(name, scratch), *pending] if failure.intact else list(pending)
    stranded = _restore_staged(putback)
    note = f"stopped at {name}: cannot delete it: {failure}"
    if not failure.intact:
        note += f"; it is part-deleted, not selectable, and left at {scratch}"
    elif scratch in stranded:
        note += f"; it is whole at {scratch} but could not be put back"
    else:
        note += "; it is still installed"
    others = [path for path in stranded if path != scratch]
    if others:
        note += "; could not put back " + ", ".join(str(path) for path in others)
    return note


def _prunable_versions(installation: Installation, *, keep: int) -> tuple[str, ...]:
    """Installed versions retention would remove, oldest first."""
    versions_dir = installation.prefix / VERSIONS_DIR
    candidates = [
        name
        for name in installation.versions
        if _installed_version_problem(versions_dir / name) is None
    ]
    protected = {installation.current, installation.previous}
    protected.add(_running_version(installation.prefix, tuple(candidates)))
    if keep > 0:
        protected.update(candidates[-keep:])
    return tuple(name for name in candidates if name not in protected)


def _running_version(prefix: Path, versions: tuple[str, ...]) -> str | None:
    """Installed version this process executes from, when it is a frozen one.

    The same test :func:`_refuse_self_replacement` applies, asked the other way
    round: not "is this the one I am running from" but "which one is it", so
    retention can leave it alone.
    """
    if not is_frozen():
        return None
    running = Path(sys.executable).resolve()
    for name in versions:
        target = (prefix / VERSIONS_DIR / name).resolve()
        if target == running or target in running.parents:
            return name
    return None


def _restore_staged(staged: list[tuple[str, Path]]) -> list[Path]:
    """Put moved-aside versions back, returning the ones that would not go.

    Only ever called with trees that are still whole — nothing deleted yet, or
    proven intact after a deletion refused — so putting one back restores a
    runnable version rather than a half-emptied one.
    """
    stranded: list[Path] = []
    for name, scratch in staged:
        try:
            scratch.rename(scratch.with_name(name))
        except OSError:
            stranded.append(scratch)
    return stranded


def install_version(
    payload_dir: Path,
    *,
    prefix: Path | None = None,
    bin_dir: Path | None = None,
    link: bool = True,
    keep: int = KEEP_VERSIONS,
    environ: dict[str, str] | None = None,
) -> InstallResult:
    """Place a payload under the prefix as a version and select it.

    The payload is copied, not moved: it may be a build output in a checkout, or
    an extraction the installer script still owns, and consuming it would make
    installing to a second prefix impossible.

    Retention runs here rather than in ``upgrade`` so that every path that adds
    a version to a prefix bounds it — the ``.run`` installer reaches this
    through ``install``, not through ``upgrade``. It runs last, after the new
    version is placed, selected, and linked, so a placement that fails removes
    nothing, and it never raises: the version the operator asked for is
    installed and working either way.
    """
    payload = payload_dir.expanduser().resolve()
    if not payload.is_dir():
        raise InstallError(f"payload directory not found: {payload}")
    manifest = read_payload_manifest(payload)
    check_target(manifest)
    version = _payload_version(manifest, payload / MANIFEST_NAME)
    root = _resolve_prefix(prefix)
    target = root / VERSIONS_DIR / version
    _refuse_self_replacement(payload, target, version)
    # One lock for the whole thing: the selection and the prune below take it
    # again and find it already held, so no other mutation of this prefix can
    # land between placing the version and retention deciding what is in use.
    with _prefix_lock(root, create=True):
        replaced = target.is_dir()
        try:
            place_directory(
                payload,
                target,
                # ``symlinks=True`` is load-bearing, not tidiness: the payload's
                # bundled framework is a tree of relative links, and resolving
                # them into copies produces a runtime that is both larger and
                # differently shaped from the one that was built and audited.
                transfer=lambda source, destination: shutil.copytree(
                    source, destination, symlinks=True
                ),
                what=f"version {version}",
            )
        except PlacementError as exc:
            raise InstallError(str(exc)) from exc
        select_version(root, version)
        link_path = _link_command(root, bin_dir) if link else None
        prune = prune_versions(root, keep=keep)
        return InstallResult(
            installation=read_installation(root),
            version=version,
            replaced=replaced,
            link_path=link_path,
            link_on_path=_on_path(link_path, environ),
            prune=prune,
        )


def setup_service(
    options: SetupOptions,
    *,
    runner: Runner = run_command,
    environ: dict[str, str] | None = None,
    token_generator: Callable[[], str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    health_checker: Callable[[str, int], str] | None = None,
) -> SetupResult:
    """Configure and start the resident service from the selected version.

    This is the checkout-independent counterpart of ``launchd deploy``: there is
    no package to install and no virtualenv to create, so the whole operation is
    resolve configuration, write the plist, bootstrap launchd, and confirm the
    service answers. Health is verified rather than assumed, because the first
    thing a fresh install has to establish is that it works at all.
    """
    root = _resolve_prefix(options.prefix)
    installation = read_installation(root)
    if installation.current is None:
        raise InstallError(
            f"no orch-hub version is selected under {root}; "
            "install one with `orch-hub install --from <payload>`"
        )
    deploy_options = DeployOptions(
        program=installation.executable,
        mandrel_path=options.mandrel_path,
        token=options.token,
        host=options.host,
        port=options.port,
        data_dir=options.data_dir,
        plist_path=options.plist_path,
        log_dir=options.log_dir,
        install_package=False,
        verify_mandrel=True,
    )
    result = deploy_service(
        deploy_options,
        runner=runner,
        environ=environ,
        token_generator=token_generator,
        sleeper=sleeper,
    )
    deployment = result.deployment
    health = wait_for_health(
        deployment.host,
        deployment.port,
        checker=health_checker,
        sleeper=sleeper,
    )
    return SetupResult(
        installation=installation,
        deployment=deployment,
        generated_token=result.generated_token,
        health=health,
    )


def upgrade(
    payload_dir: Path,
    *,
    prefix: Path | None = None,
    bin_dir: Path | None = None,
    plist_path: Path | None = None,
    runner: Runner = run_command,
    environ: dict[str, str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    health_checker: Callable[[str, int], str] | None = None,
) -> UpgradeResult:
    """Install a newer payload and restart the service onto it.

    Nothing about the running configuration is passed here: the plist rewrite
    resolves every setting from the environment and the installed plist, so the
    token, Mandrel checkout, bind address, data directory, and push endpoint all
    survive an upgrade without being repeated on the command line.

    A failure after the new version is selected leaves both versions installed —
    the point of the versioned layout — so the previous one is named in the
    error and can be selected again with ``rollback``.
    """
    root = _resolve_prefix(prefix)
    before = read_installation(root).current
    installed = install_version(
        payload_dir, prefix=root, bin_dir=bin_dir, environ=environ
    )
    try:
        setup = setup_service(
            SetupOptions(prefix=root, plist_path=plist_path),
            runner=runner,
            environ=environ,
            sleeper=sleeper,
            health_checker=health_checker,
        )
    except (InstallError, LaunchdError) as exc:
        recovery = (
            f"; the previous version is still installed — "
            f"run `orch-hub rollback --to {before}`"
            if before and before != installed.version
            else ""
        )
        raise InstallError(
            f"installed {installed.version} but could not start it: {exc}{recovery}"
        ) from exc
    return UpgradeResult(
        install=installed, setup=setup, previous_version=before
    )


def rollback(
    *,
    version: str | None = None,
    prefix: Path | None = None,
    plist_path: Path | None = None,
    runner: Runner = run_command,
    sleeper: Callable[[float], None] = time.sleep,
    health_checker: Callable[[str, int], str] | None = None,
) -> RollbackResult:
    """Select another installed version and restart the service onto it.

    The plist is not rewritten: it names ``<prefix>/current/orch-hub``, so
    changing which code runs is a symlink swap plus a restart. That is why a
    rollback keeps the service's configuration and the hub's data exactly as
    they were — it changes the program, and nothing else.
    """
    root = _resolve_prefix(prefix)
    installation = read_installation(root)
    target = version or installation.previous
    if target is None:
        raise InstallError(
            f"no previous version recorded under {root}; "
            f"name one explicitly (installed: {', '.join(installation.versions) or 'none'})"
        )
    if target == installation.current:
        raise InstallError(f"version {target} is already selected")
    # Read where health will be probed *before* the selection moves: a service
    # configuration that cannot be parsed has to fail while the running version
    # is still the selected one, not after a restart onto a new one.
    host, port = installed_bind(plist_path=plist_path)
    select_version(root, target)
    try:
        restart_service(plist_path=plist_path, runner=runner)
    except LaunchdError as exc:
        # The selection already happened, and it is what the operator asked
        # for — undoing it would hide the change. Say what is true instead: the
        # version moved, the service did not come back, and here is the verb
        # that starts it.
        raise InstallError(
            f"selected {target} but could not restart the service: {exc}; "
            "start it with `orch-hub launchd start`"
        ) from exc
    health = wait_for_health(
        host, port, checker=health_checker, sleeper=sleeper
    )
    return RollbackResult(
        from_version=installation.current, to_version=target, health=health
    )


def uninstall(
    *,
    prefix: Path | None = None,
    bin_dir: Path | None = None,
    plist_path: Path | None = None,
    purge_data: bool = False,
    data_dir: Path | None = None,
    runner: Runner = run_command,
) -> UninstallResult:
    """Stop the service and remove the installed runtime.

    The hub's data directory is left alone unless it is purged explicitly: run
    history, audit events, and evaluation artifacts are not re-creatable from a
    release, so removing a program must not remove them by default.

    Two things are established before anything is deleted. The prefix must be
    an installation this installer made — ``--prefix`` takes any path, and a
    recursive removal is the wrong place to find out it was a typo. And the
    service must actually be stopped: ``stop_service`` already answers "there
    was no job" without raising, so a raised error means a live service could
    not be unloaded, and deleting its plist and data around it would leave a
    running process with no configuration and ``KeepAlive`` unable to restart
    it.
    """
    root = _resolve_prefix(prefix)
    resolved_plist = (plist_path or default_plist_path()).expanduser()
    # Ownership is proven before the prefix is locked, because locking writes a
    # file into it: a mistyped `--prefix` must be refused without this module
    # having left anything behind in somebody else's directory.
    prefix_present = _verify_removable_prefix(root)
    resolved_data = _uninstall_data_dir(data_dir, resolved_plist) if purge_data else None

    with _prefix_lock(root):
        try:
            stop_service(runner=runner)
        except LaunchdError as exc:
            raise InstallError(
                f"cannot stop the {LABEL} service, so nothing was removed: {exc}; "
                "stop it and retry, or unload it by hand with "
                f"`launchctl bootout {service_target()}`"
            ) from exc

        plist_removed = _remove_file(resolved_plist)
        link_path, link_removed = _unlink_command(root, bin_dir)
        data_removed = False
        if resolved_data is not None:
            data_removed = _remove_tree(resolved_data)
        # Last, because on a frozen runtime this is the directory the running
        # executable lives in: everything above still needs the payload's files.
        prefix_removed = _remove_tree(root) if prefix_present else False
    return UninstallResult(
        prefix=root,
        prefix_removed=prefix_removed,
        plist_path=resolved_plist,
        plist_removed=plist_removed,
        link_path=link_path,
        link_removed=link_removed,
        data_dir=resolved_data,
        data_removed=data_removed,
    )


def _resolve_prefix(prefix: Path | None) -> Path:
    return (prefix or default_prefix()).expanduser()


_HELD_LOCKS = threading.local()
"""Prefixes this thread already holds, by depth.

Per-thread on purpose: it exists so a call chain that already holds a prefix
does not lock it a second time, and a *different* thread must not read that as
permission to skip the lock. ``flock`` treats two descriptors for one file
independently even inside one process, so the second thread blocks on the file
exactly like a second process would."""


@contextmanager
def _prefix_lock(root: Path, *, create: bool = False) -> Iterator[None]:
    """Hold ``root`` exclusively for the length of one mutation.

    Every operation that changes what is installed under a prefix — placing a
    version, selecting one, pruning, removing the installation — runs inside
    this. Retention is why: it reads the inventory, works out what nothing is
    using, and only then removes it, so a selection landing in that gap would
    make ``current`` name a version the prune is already deleting. Excluding one
    mutation at a time is cheaper to reason about than making each step
    individually race-free, and these are operator-paced commands.

    Nesting is expected — placement locks, then selects and prunes under the
    lock it already holds — and is a no-op after the first acquisition.

    The lock file lives inside the prefix, so it is taken only where an
    installation can exist: ``create`` is for placement, which is entitled to
    make the prefix, and everything else runs unlocked against a prefix that is
    not there, having nothing to exclude anyone from.
    """
    key = Path(os.path.realpath(root))
    depths = getattr(_HELD_LOCKS, "depths", None)
    if depths is None:
        depths = _HELD_LOCKS.depths = {}
    depth = depths.get(key, 0)
    depths[key] = depth + 1
    try:
        if depth:
            yield
            return
        if create:
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise InstallError(f"cannot create the prefix {root}: {exc}") from exc
        if not root.is_dir():
            yield
            return
        handle = _open_prefix_lock(root)
        try:
            yield
        finally:
            # Closing the descriptor releases the lock, including when the
            # prefix it lives in has just been removed by uninstall.
            os.close(handle)
    finally:
        remaining = depths[key] - 1
        if remaining:
            depths[key] = remaining
        else:
            del depths[key]


def _open_prefix_lock(root: Path) -> int:
    """Open and lock ``<prefix>/.lock``, or fail as an operational error.

    ``.lock`` is accepted under the prefix by name, so what sits at that name
    has to be proven rather than assumed. Opening it with ``O_CREAT`` alone
    would follow a symlink planted there and *create* its target: a mutation of
    any prefix would then write and lock an unrelated file outside it. The open
    refuses to follow a link, and what it did open is checked to be a regular
    file — a directory, a device, or a fifo at that name is somebody else's, and
    locking it is not this installer's business either.
    """
    path = root / LOCK_NAME
    try:
        handle = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        # ELOOP reads as "too many levels of symbolic links", which describes
        # neither what is wrong nor what to do about it.
        detail = (
            f"{path} is a symlink; the lock has to be a regular file inside "
            "the prefix. Remove it and retry"
            if path.is_symlink()
            else str(exc)
        )
        raise InstallError(f"cannot lock {root}: {detail}") from exc
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise InstallError(
                f"cannot lock {root}: {path} is not a regular file; "
                "remove it and retry"
            )
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError as exc:
            raise InstallError(f"cannot lock {root}: {exc}") from exc
        locked = True
        return handle
    finally:
        if not locked:
            os.close(handle)


def _verify_removable_prefix(root: Path) -> bool:
    """Prove ``root`` is an orch-hub installation, or refuse to touch it.

    ``--prefix`` accepts any path and uninstall deletes it recursively, so a
    typo — or ``--prefix ~`` — is one command away from removing unrelated
    files. Ownership is established positively, and the proof has to be
    something only this installer produces: the prefix carries the ``versions/``
    directory this module creates, every entry beside it is one of ours, and
    ``versions/`` holds *at least one* directory that is a payload this
    installer would accept, filed under the version that payload declares.

    A bare filename proves nothing — any project may keep a file called
    ``runtime.json`` — so the manifest is parsed and matched to the directory it
    sits in, and an empty ``versions/`` no longer passes by having nothing to
    object to.

    Returns whether there is anything to remove; a prefix that does not exist is
    not an error, because uninstall still has a plist and a command link to
    clean up.
    """
    if root.is_symlink():
        raise InstallError(
            f"{root} is a symlink, not an installation directory; "
            "name the real prefix"
        )
    if not root.exists():
        return False
    if not root.is_dir():
        raise InstallError(f"{root} is not a directory")
    entries = _list_dir(root)
    foreign = sorted(
        entry.name for entry in entries if entry.name not in OWNED_PREFIX_ENTRIES
    )
    if foreign:
        listed = ", ".join(foreign[:5]) + ("..." if len(foreign) > 5 else "")
        raise InstallError(
            f"{root} is not an orch-hub install prefix: it holds {listed}; "
            "refusing to remove it"
        )
    versions_dir = root / VERSIONS_DIR
    if not versions_dir.is_dir():
        raise InstallError(
            f"{root} is not an orch-hub install prefix: no {VERSIONS_DIR}/ "
            "directory; refusing to remove it"
        )
    installed = 0
    for entry in _list_dir(versions_dir):
        problem = _installed_version_problem(entry)
        if problem is None:
            installed += 1
            continue
        # Debris from an interrupted placement or prune is ours, but it is not
        # a version: it is skipped rather than counted, so it can never be the
        # only thing "proving" ownership.
        if is_reserved_version(entry.name):
            continue
        raise InstallError(
            f"{entry} is not an installed orch-hub version ({problem}); "
            f"refusing to remove {root}"
        )
    if not installed:
        raise InstallError(
            f"{root} is not an orch-hub install prefix: {VERSIONS_DIR}/ holds no "
            "installed version; refusing to remove it"
        )
    return True


def _installed_version_problem(entry: Path) -> str | None:
    """Why ``entry`` is not an installed orch-hub version, or ``None``.

    The test is the one :func:`read_payload_manifest` already applies to a
    payload before placing it, plus the binding that placing it creates: the
    version the manifest declares is the directory it was filed under. Reusing
    that function rather than re-listing its rules is deliberate — a second copy
    would decide "is this ours" by different rules than the ones that put it
    there.
    """
    if entry.is_symlink() or not entry.is_dir():
        return "not a directory"
    try:
        manifest = read_payload_manifest(entry)
    except InstallError as exc:
        return str(exc)
    declared = _payload_version(manifest, entry / MANIFEST_NAME)
    if declared != entry.name:
        return f"declares version {declared!r} but is filed as {entry.name!r}"
    return None


def _list_dir(path: Path) -> list[Path]:
    """Directory entries, with an unreadable directory as an operational error.

    An unreadable prefix must not reach the caller as a traceback: this runs on
    the way to a removal the CLI reports as ``error: ...`` and exit 1.
    """
    try:
        return list(path.iterdir())
    except OSError as exc:
        raise InstallError(f"cannot read {path}: {exc}") from exc


def _refuse_self_replacement(payload: Path, target: Path, version: str) -> None:
    """Refuse to overwrite the version this process is running from.

    Replacing a payload directory removes the files the running executable still
    needs for anything it has not imported yet, so the failure would appear
    later and somewhere else. The upgrade path never hits this — the installer
    runs the *new* payload's binary — so failing closed costs nothing and
    removes a class of unexplainable breakage.
    """
    # Both sides are resolved: a prefix under a symlinked directory (``/tmp`` on
    # macOS is one) would otherwise compare unequal to the very path it names.
    resolved = target.resolve()
    if payload == resolved:
        raise InstallError(f"version {version} is already installed at {target}")
    if not is_frozen():
        return
    running = Path(sys.executable).resolve()
    if running == resolved or resolved in running.parents:
        raise InstallError(
            f"cannot replace version {version} while running from it; "
            "run the new payload's own `orch-hub install`"
        )


def _link_command(prefix: Path, bin_dir: Path | None) -> Path:
    """Link ``<bin-dir>/orch-hub`` at the selected version's executable."""
    directory = (bin_dir or default_bin_dir()).expanduser()
    link = directory / COMMAND_NAME
    if link.exists() and not link.is_symlink():
        raise InstallError(
            f"{link} already exists and is not a symlink; "
            "move it aside or install with --no-link"
        )
    directory.mkdir(parents=True, exist_ok=True)
    incoming = directory / f".{COMMAND_NAME}.incoming"
    discard_path(incoming)
    try:
        incoming.symlink_to(prefix / CURRENT_LINK / COMMAND_NAME)
        os.replace(incoming, link)
    except OSError as exc:
        discard_path(incoming, best_effort=True)
        raise InstallError(f"cannot link {link}: {exc}") from exc
    return link


def _unlink_command(prefix: Path, bin_dir: Path | None) -> tuple[Path | None, bool]:
    """Remove the command link, but only if it points into this prefix.

    A same-named command that is not ours — another installation, or something
    the operator put there — is left alone. Uninstall removes what it installed.
    """
    directory = (bin_dir or default_bin_dir()).expanduser()
    link = directory / COMMAND_NAME
    if not link.is_symlink():
        return (link if link.exists() else None), False
    try:
        target = Path(os.readlink(link))
    except OSError:
        return link, False
    if not target.is_absolute():
        target = (directory / target).resolve(strict=False)
    if target != prefix / CURRENT_LINK / COMMAND_NAME:
        return link, False
    return link, _remove_file(link)


def _on_path(link_path: Path | None, environ: dict[str, str] | None) -> bool:
    """Whether the linked command is reachable by name in this environment."""
    if link_path is None:
        return False
    env = os.environ if environ is None else environ
    entries = (env.get("PATH") or "").split(os.pathsep)
    parent = str(link_path.parent)
    return any(entry and Path(entry).expanduser() == Path(parent) for entry in entries)


def _uninstall_data_dir(data_dir: Path | None, plist_path: Path) -> Path:
    """Data directory a purge would remove: explicit, else the service's own."""
    if data_dir is not None:
        return data_dir.expanduser()
    return installed_data_dir(plist_path=plist_path)


def _remove_file(path: Path) -> bool:
    if not path.is_symlink() and not path.exists():
        return False
    try:
        path.unlink()
    except OSError as exc:
        raise InstallError(f"cannot remove {path}: {exc}") from exc
    return True


def _remove_tree(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise InstallError(f"cannot remove {path}: {exc}") from exc
    return True


def _write_previous(prefix: Path, version: str) -> None:
    try:
        (prefix / PREVIOUS_MARKER).write_text(f"{version}\n", encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"cannot record the previous version: {exc}") from exc


def _version_tuple(raw: str) -> tuple[int, ...]:
    """Numeric prefix of a dotted version, or ``()`` when it is not numeric."""
    parts: list[int] = []
    for chunk in str(raw or "").strip().split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts)


def _version_sort_key(name: str) -> tuple[tuple[int, ...], str]:
    """Order versions numerically where possible, by name otherwise."""
    return (_version_tuple(name), name)
