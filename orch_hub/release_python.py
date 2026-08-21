"""Provision the redistributable interpreter a release payload is built from.

Freezing bundles the interpreter that runs the freezer, so whichever Python the
build machine happens to have decides the minimum macOS of the shipped artifact.
A payload frozen against an interpreter compiled for the maintainer's own macOS
refuses to start on every older Mac, and the installer — which gates on the floor
the payload declares — reports that refusal precisely and uselessly. The declared
number is not the bug; the interpreter behind it is.

So the interpreter is pinned like a dependency, for the same reason
``pyproject.toml`` pins the freezer: both decide what the artifact *is*. The pin
names one redistributable build, addressed by URL and proven by digest, unpacked
into the build directory. Nothing is installed on the machine and nothing needs
privileges, which is what keeps "build a release" a command rather than a
machine setup, on a maintainer's Mac and on a CI runner alike.

Two properties are worth more than the mechanics:

- **The cache key is the bytes.** The unpacked tree is named after the archive
  *and its digest*, so a bumped pin can never be answered out of the previous
  interpreter's directory. A cache keyed by version alone would hand a build the
  interpreter it used to have, and the payload would name one interpreter while
  carrying another.
- **A pin that cannot be proven is not used.** A download whose digest differs
  from the declaration is deleted rather than kept, and an archive that does not
  contain the declared interpreter is discarded. Neither leaves a directory a
  later build could mistake for a provisioned interpreter.
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .atomic_dir import PlacementError, discard_path, place_directory

DOWNLOAD_TIMEOUT = 300.0
"""Seconds one interpreter download may take before the build gives up.

Generous rather than tight: the archive is tens of megabytes and this runs once
per pin, so the only thing worth catching is a connection that has stopped
answering entirely."""

_CHUNK_SIZE = 1 << 20

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")
"""Archive forms the provisioner can unpack. The pin declares a URL, so an
unreadable extension is a maintainer error and is reported as one."""


class InterpreterError(RuntimeError):
    """Raised when the pinned interpreter cannot be provisioned or trusted."""


@dataclass(frozen=True)
class PinnedInterpreter:
    """One target's build interpreter, named exactly enough to be reproduced."""

    target: str
    python_version: str
    url: str
    sha256: str
    """Digest of the archive at ``url``, as published by its build.

    The whole trust chain: the archive is a third-party binary the release is
    compiled into, so it is verified against a value living in this checkout
    rather than against whatever the host currently serves."""

    min_macos: str
    """The oldest macOS a payload built from this interpreter must still run on.

    Declared here because the interpreter is what sets it, but it is a promise
    about the *payload*: the build measures the produced binaries and refuses a
    payload that demands more than this, which catches a dependency wheel
    raising the floor just as well as a wrong interpreter."""

    relative_python: str = "python/bin/python3"
    """Where the interpreter sits inside the unpacked archive."""

    @property
    def archive_name(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @property
    def name(self) -> str:
        """Directory name for the unpacked interpreter — archive plus digest.

        The digest is in the name so that the cache cannot answer a changed pin:
        different bytes are a different directory, always.
        """
        stem = self.archive_name
        for suffix in ARCHIVE_SUFFIXES:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        else:
            raise InterpreterError(
                f"pinned interpreter archive {self.archive_name} is not one of "
                f"{', '.join(ARCHIVE_SUFFIXES)}"
            )
        return f"{stem}-{self.sha256[:12]}"


PINNED: dict[str, PinnedInterpreter] = {
    "darwin-arm64": PinnedInterpreter(
        target="darwin-arm64",
        python_version="3.14.7",
        # python-build-standalone: redistributable CPython builds compiled with
        # MACOSX_DEPLOYMENT_TARGET=11.0. Chosen over python.org's universal2
        # framework because it unpacks into the build directory instead of
        # needing an administrator install into /Library/Frameworks, which a CI
        # runner and an unprivileged maintainer both do without ceremony.
        url=(
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            "20260814/cpython-3.14.7+20260814-aarch64-apple-darwin-install_only.tar.gz"
        ),
        sha256="eded2b762929116a4e68ad1128e55f0728f502e93c90454e3b5f79da7a2cda40",
        min_macos="11.0",
    ),
}
"""The interpreter each shipped target is built from, keyed the way the release
keys everything else. A second target is another entry, not another mechanism."""


def pinned_interpreter(target: str) -> PinnedInterpreter:
    pin = PINNED.get(target)
    if pin is None:
        known = ", ".join(sorted(PINNED)) or "none"
        raise InterpreterError(
            f"no build interpreter is pinned for target {target!r} (pinned: {known})"
        )
    return pin


def download(url: str, destination: Path) -> None:
    """Fetch ``url`` into ``destination``.

    Streamed rather than read whole: the archive is tens of megabytes, and a
    build machine has no reason to hold it in memory.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "orch-hub-release"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            with destination.open("wb") as handle:
                shutil.copyfileobj(response, handle, _CHUNK_SIZE)
    except OSError as exc:
        raise InterpreterError(f"cannot download {url}: {exc}") from exc


def ensure_interpreter(
    pin: PinnedInterpreter,
    root: Path,
    *,
    fetch: Callable[[str, Path], None] = download,
) -> Path:
    """Return the pinned interpreter under ``root``, provisioning it if absent.

    A provisioned tree is only ever published under a digest-derived name by the
    code below, so finding one is proof that these bytes were verified — which
    is why a cache hit re-checks nothing. Re-hashing a few hundred megabytes of
    unpacked interpreter on every build would buy a guarantee against nothing
    but someone editing the build directory by hand.
    """
    if not pin.url.startswith("https://"):
        # A pin is checked-in data, so this can only fail by being edited; it
        # fails here anyway, because the digest below is worth nothing if the
        # archive it verifies can be substituted in transit.
        raise InterpreterError(
            f"pinned interpreter for {pin.target} is not fetched over https: {pin.url}"
        )
    directory = root / pin.name
    python = directory / pin.relative_python
    if python.is_file():
        return python

    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"{pin.name}.archive"
    unpacked = root / f"{pin.name}.unpacked"
    discard_path(archive, best_effort=True)
    discard_path(unpacked)
    try:
        fetch(pin.url, archive)
        _verify(archive, pin)
        _unpack(archive, unpacked)
        staged = unpacked / pin.relative_python
        if not staged.is_file():
            raise InterpreterError(
                f"the archive for {pin.target} holds no interpreter at "
                f"{pin.relative_python}"
            )
        try:
            place_directory(
                unpacked,
                directory,
                transfer=lambda source, destination: shutil.move(
                    str(source), str(destination)
                ),
                what=f"python {pin.python_version}",
            )
        except PlacementError as exc:
            raise InterpreterError(str(exc)) from exc
    finally:
        # Neither survives a failure: a half-written archive or a partly
        # unpacked tree left beside the real one is exactly what a later build
        # must not be able to mistake for a provisioned interpreter.
        discard_path(archive, best_effort=True)
        discard_path(unpacked, best_effort=True)
    return python


def _verify(archive: Path, pin: PinnedInterpreter) -> None:
    actual = _sha256(archive)
    if actual != pin.sha256:
        raise InterpreterError(
            f"the interpreter downloaded for {pin.target} is not the pinned one: "
            f"expected sha256 {pin.sha256}, got {actual}"
        )


def _unpack(archive: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive, "r:*") as tar:
            # `data` refuses members that escape the destination, along with
            # ownership, setuid bits, and device nodes. An interpreter is a
            # tree of files and symlinks; nothing it legitimately carries needs
            # more than that.
            tar.extractall(destination, filter="data")
    except (OSError, tarfile.TarError, ValueError) as exc:
        raise InterpreterError(f"cannot unpack {archive.name}: {exc}") from exc


def _sha256(path: Path) -> str:
    # Not shared with the packager's identical helper: that module imports this
    # one's caller, so borrowing it would close an import cycle around a digest.
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()
