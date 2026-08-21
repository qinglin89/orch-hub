"""Tests for provisioning the pinned build interpreter.

Nothing here touches the network: the pin's own fetch is the one seam, so every
test hands the provisioner an archive it builds itself. What is pinned is the
part that decides whether a release can be trusted — that unverified bytes never
become a build interpreter, that a failed attempt leaves nothing a later build
could reuse, and that the cache is keyed by the bytes rather than by the version
they claim to be.

The declaration itself is checked against its own rules too. A pin is data
somebody edits by hand, and a release compiled around a wrong one is not
recoverable after publication.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from orch_hub.macho import version_key
from orch_hub.release_python import (
    PINNED,
    InterpreterError,
    PinnedInterpreter,
    ensure_interpreter,
    pinned_interpreter,
)

INTERPRETER = "python/bin/python3"


def make_archive(
    path: Path, members: dict[str, bytes] | None = None, *, escaping: bool = False
) -> str:
    """Write a tarball standing in for a redistributable interpreter."""
    files = {INTERPRETER: b"#!/bin/sh\n", "python/lib/libpython.dylib": b"\xcf\xfa\xed\xfe"}
    if members is not None:
        files = members
    with tarfile.open(path, "w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(content))
        if escaping:
            # What the `data` filter exists for: a member that unpacks outside
            # the directory it was given.
            info = tarfile.TarInfo("../escaped")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"out!"))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin(sha256: str, **overrides) -> PinnedInterpreter:
    values = dict(
        target="darwin-arm64",
        python_version="3.14.7",
        url="https://example.invalid/downloads/cpython-3.14.7-darwin.tar.gz",
        sha256=sha256,
        min_macos="11.0",
    )
    values.update(overrides)
    return PinnedInterpreter(**values)


class FakeFetch:
    """Stand-in for the download, serving a prepared archive."""

    def __init__(self, source: Path | None = None, error: Exception | None = None):
        self.source = source
        self.error = error
        self.urls: list[str] = []

    def __call__(self, url: str, destination: Path) -> None:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        destination.write_bytes(self.source.read_bytes())


def leftovers(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir())


def test_every_pinned_interpreter_declares_what_it_promises():
    # A pin is hand-edited data compiled into an unrecoverable artifact, so the
    # shape it has to hold is checked rather than assumed.
    assert PINNED, "at least the shipped target must be pinned"
    for target, entry in PINNED.items():
        assert entry.target == target
        assert entry.url.startswith("https://"), target
        assert len(entry.sha256) == 64, target
        assert set(entry.sha256) <= set("0123456789abcdef"), target
        assert version_key(entry.min_macos) >= (11, 0, 0), target
        assert entry.sha256[:12] in entry.name
        assert entry.python_version.split(".")[0] == "3"


def test_the_shipped_target_is_pinned_to_a_supported_floor():
    from orch_hub.release_build import TARGET

    # The whole point of pinning: the payload must not inherit the builder's
    # macOS. Any floor at or below Big Sur is a redistributable interpreter.
    assert version_key(pinned_interpreter(TARGET).min_macos) <= (11, 0, 0)


def test_an_unpinned_target_names_the_ones_that_are_pinned():
    with pytest.raises(InterpreterError, match="linux-x86_64.*pinned: darwin-arm64"):
        pinned_interpreter("linux-x86_64")


def test_a_provisioned_interpreter_is_unpacked_under_its_digest(tmp_path: Path):
    digest = make_archive(tmp_path / "source.tar.gz")
    fetch = FakeFetch(tmp_path / "source.tar.gz")
    entry = pin(digest)

    python = ensure_interpreter(entry, tmp_path / "root", fetch=fetch)

    assert python == tmp_path / "root" / entry.name / INTERPRETER
    assert python.read_bytes() == b"#!/bin/sh\n"
    assert fetch.urls == [entry.url]
    # The archive and the unpacking directory are scratch; only the interpreter
    # survives, or the build directory grows a copy of every pin ever fetched.
    assert leftovers(tmp_path / "root") == [entry.name]


def test_a_provisioned_interpreter_is_not_fetched_twice(tmp_path: Path):
    digest = make_archive(tmp_path / "source.tar.gz")
    fetch = FakeFetch(tmp_path / "source.tar.gz")
    entry = pin(digest)
    ensure_interpreter(entry, tmp_path / "root", fetch=fetch)

    again = ensure_interpreter(entry, tmp_path / "root", fetch=fetch)

    assert again.is_file()
    assert fetch.urls == [entry.url]  # answered from the cache


def test_a_changed_pin_is_never_answered_from_the_previous_interpreter(tmp_path: Path):
    # Same URL, different bytes: a cache keyed by version alone would hand this
    # build the interpreter it used to have, and the payload would name one
    # interpreter while carrying another.
    old_digest = make_archive(tmp_path / "old.tar.gz")
    new_digest = make_archive(
        tmp_path / "new.tar.gz", {INTERPRETER: b"#!/bin/sh\n# newer\n"}
    )
    assert old_digest != new_digest
    root = tmp_path / "root"
    ensure_interpreter(pin(old_digest), root, fetch=FakeFetch(tmp_path / "old.tar.gz"))

    fetch = FakeFetch(tmp_path / "new.tar.gz")
    python = ensure_interpreter(pin(new_digest), root, fetch=fetch)

    assert fetch.urls  # refetched rather than reused
    assert python.read_bytes() == b"#!/bin/sh\n# newer\n"
    assert sorted(leftovers(root)) == sorted([pin(old_digest).name, pin(new_digest).name])


def test_an_archive_that_is_not_the_pinned_one_is_refused_and_deleted(tmp_path: Path):
    make_archive(tmp_path / "source.tar.gz")
    entry = pin("0" * 64)
    fetch = FakeFetch(tmp_path / "source.tar.gz")

    with pytest.raises(InterpreterError, match="not the pinned one"):
        ensure_interpreter(entry, tmp_path / "root", fetch=fetch)

    # Nothing kept: an unverified archive on disk is the one thing a later run
    # must not be able to treat as provisioned.
    assert leftovers(tmp_path / "root") == []


def test_a_failed_download_provisions_nothing(tmp_path: Path):
    entry = pin("0" * 64)
    fetch = FakeFetch(error=OSError("connection reset"))

    with pytest.raises(OSError):
        ensure_interpreter(entry, tmp_path / "root", fetch=fetch)

    assert leftovers(tmp_path / "root") == []


def test_an_archive_without_the_declared_interpreter_is_refused(tmp_path: Path):
    digest = make_archive(tmp_path / "source.tar.gz", {"python/lib/README": b"docs"})
    entry = pin(digest)

    with pytest.raises(InterpreterError, match="holds no interpreter"):
        ensure_interpreter(entry, tmp_path / "root", fetch=FakeFetch(tmp_path / "source.tar.gz"))

    assert leftovers(tmp_path / "root") == []


def test_a_member_that_unpacks_outside_the_destination_is_refused(tmp_path: Path):
    digest = make_archive(tmp_path / "source.tar.gz", escaping=True)
    root = tmp_path / "root"

    with pytest.raises(InterpreterError, match="cannot unpack"):
        ensure_interpreter(pin(digest), root, fetch=FakeFetch(tmp_path / "source.tar.gz"))

    assert leftovers(root) == []
    assert not (tmp_path / "escaped").exists()
    assert not (root.parent / "escaped").exists()


def test_a_pin_that_is_not_fetched_over_https_is_refused(tmp_path: Path):
    digest = make_archive(tmp_path / "source.tar.gz")
    fetch = FakeFetch(tmp_path / "source.tar.gz")
    entry = pin(digest, url="http://example.invalid/cpython-3.14.7-darwin.tar.gz")

    with pytest.raises(InterpreterError, match="not fetched over https"):
        ensure_interpreter(entry, tmp_path / "root", fetch=fetch)

    # Refused before the download, not after: a digest cannot vouch for an
    # archive that something in the middle was free to substitute.
    assert fetch.urls == []


def test_an_archive_form_the_provisioner_cannot_unpack_is_a_pin_error():
    entry = pin("0" * 64, url="https://example.invalid/cpython-3.14.7-darwin.zip")

    with pytest.raises(InterpreterError, match="is not one of"):
        entry.name
