"""Tests for tag-driven publication of a packaged release.

The releases under test are produced by the real packager over a synthetic
payload, not hand-written: publication's job is to re-derive an artifact set
from its own bytes, and a hand-written set would only prove that this module
agrees with the fixture that was written to satisfy it. Every "refuses X" test
therefore starts from a release the packager actually emitted and breaks
exactly one thing about it.

Git and ``gh`` are injected runners. What matters about them is the argv — that
the tag is resolved against the checkout rather than assumed, and that
``gh release create`` cannot invent the tag it publishes at — and argv is
exactly what a fake runner can pin without a network or a repository.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from orch_hub import release_publish as publish_module
from orch_hub.release_build import MANIFEST_NAME, MANIFEST_SCHEMA, STATUS_ARGV
from orch_hub.release_package import (
    CHECKSUM_NAME,
    RELEASE_NAME,
    PackageOptions,
    package_release,
    sha256_file,
)
from orch_hub.release_publish import (
    GH_BIN,
    GIT_BIN,
    PUBLICATION_NAME,
    PUBLICATION_SCHEMA,
    Destination,
    PublishError,
    PublishOptions,
    Revision,
    check_payload_revision,
    check_tag,
    default_release_dir,
    main,
    parse_destination,
    publication_document,
    publish,
    release_notes,
    resolve_tag,
    verify_release,
)
from orch_hub.subprocess_utils import CommandNotFoundError, CommandResult

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "deploy" / "release" / "installer.sh"

COMMIT = "1" * 40
OTHER_COMMIT = "2" * 40

FAKE_RUNTIME = """#!/bin/sh
exit 0
"""


def make_payload(
    root: Path,
    *,
    version: str = "9.9.9",
    target: str = "darwin-arm64",
    label: str = "",
    source_revision: str | None = COMMIT,
) -> Path:
    """A payload directory shaped like a real one, minus the interpreter.

    ``label`` changes the runtime's bytes without changing anything the release
    format describes, which is how a test gets two equally valid releases of one
    version that a digest can tell apart.

    ``source_revision`` defaults to the commit :class:`FakeRunner` resolves the
    tag to, so the ordinary payload is one the tag can actually be bound to;
    every other value is a test about that binding. ``None`` omits the field —
    a payload from a build that could not name its source.
    """
    declared_os, _, declared_arch = target.partition("-")
    payload = root / f"orch-hub-{target}"
    (payload / "_internal").mkdir(parents=True, exist_ok=True)
    binary = payload / "orch-hub"
    binary.write_text(
        FAKE_RUNTIME + (f"# {label}\n" if label else ""), encoding="utf-8"
    )
    binary.chmod(0o755)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "name": "orch-hub",
        "version": version,
        "target": target,
        "os": declared_os,
        "arch": declared_arch,
        "executable": "orch-hub",
        "min_macos": "11.0",
        "architectures": [declared_arch],
        "binaries": 1,
        "python_version": "3.14.7",
        "builder": "pyinstaller 6.22.2",
    }
    if source_revision is not None:
        manifest["source_revision"] = source_revision
    (payload / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return payload


def make_release(
    tmp_path: Path,
    *,
    version: str = "9.9.9",
    targets=("darwin-arm64",),
    label: str = "",
    source_revision: str | None = COMMIT,
) -> Path:
    """Package synthetic payloads into a real release directory.

    ``label`` gives a release its own payload bytes *and* its own directories,
    so several of them can sit side by side at one version — what a publisher
    racing another publisher of the same tag actually looks like.
    """
    suffix = f"-{label}" if label else ""
    source = tmp_path / f"payloads{suffix}"
    payloads = tuple(
        make_payload(
            source,
            version=version,
            target=target,
            label=label,
            source_revision=source_revision,
        )
        for target in targets
    )
    result = package_release(
        PackageOptions(
            payload_dirs=payloads,
            out_dir=tmp_path / f"dist{suffix}" / "release",
            work_dir=tmp_path / f"build{suffix}",
            template_path=TEMPLATE,
            # The synthetic payload carries none of the frozen runtime's
            # required members; the audit gate has its own tests.
            allow_findings=True,
        )
    )
    return result.out_dir


def read_release(release_dir: Path) -> dict:
    return json.loads((release_dir / RELEASE_NAME).read_text(encoding="utf-8"))


def write_release(release_dir: Path, document: dict) -> None:
    (release_dir / RELEASE_NAME).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def resync_checksums(release_dir: Path) -> None:
    """Rewrite ``SHA256SUMS`` from what is actually on disk.

    Used by tests that break something *other* than integrity: without it every
    such test would fail at the checksum comparison and prove nothing about the
    check it was written for.
    """
    names = sorted(
        entry.name for entry in release_dir.iterdir() if entry.name != CHECKSUM_NAME
    )
    (release_dir / CHECKSUM_NAME).write_text(
        "".join(f"{sha256_file(release_dir / name)}  {name}\n" for name in names),
        encoding="utf-8",
    )


def installer_of(release_dir: Path) -> Path:
    return next(release_dir.glob("*.run"))


class FakeRunner:
    """Answers the three git probes and ``gh release create``."""

    def __init__(
        self,
        *,
        tag_commit: str | None = COMMIT,
        head: str = COMMIT,
        status: str = "",
        gh_result: CommandResult | None = None,
        missing: str | None = None,
    ):
        self.tag_commit = tag_commit
        self.head = head
        self.status = status
        self.gh_result = gh_result
        self.missing = missing
        self.calls: list[tuple[str, ...]] = []
        self.kwargs: list[dict] = []

    def __call__(self, args, **kwargs) -> CommandResult:
        argv = tuple(str(item) for item in args)
        self.calls.append(argv)
        self.kwargs.append(kwargs)
        if self.missing is not None and argv[0] == self.missing:
            raise CommandNotFoundError(argv[0])
        if argv[0] == GH_BIN:
            return self.gh_result or CommandResult(
                argv, 0, "https://github.com/o/n/releases/tag/v9.9.9\n", ""
            )
        if argv[1:3] == ("rev-parse", "--verify") and argv[3] == "--quiet":
            if self.tag_commit is None:
                return CommandResult(argv, 1, "", "")
            return CommandResult(argv, 0, f"{self.tag_commit}\n", "")
        if argv[1:3] == ("rev-parse", "--verify"):
            return CommandResult(argv, 0, f"{self.head}\n", "")
        if argv[1:] == STATUS_ARGV:
            return CommandResult(argv, 0, self.status, "")
        raise AssertionError(f"unexpected command {argv}")

    def git_calls(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] == GIT_BIN]

    def gh_call(self) -> tuple[str, ...]:
        return next(call for call in self.calls if call[0] == GH_BIN)


def stub_checkout(root: Path, version: str = "9.9.9") -> Path:
    """A checkout that declares a version, which is all publication reads.

    Publication holds the artifact set to the source it is being tagged from,
    so a test checkout has to declare a version for the accepting path to be
    reachable at all.
    """
    checkout = root / f"checkout-{version}"
    (checkout / "orch_hub").mkdir(parents=True, exist_ok=True)
    (checkout / "orch_hub" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    return checkout


def options(tmp_path: Path, release_dir: Path, destination: str, **overrides):
    defaults = dict(
        checkout=stub_checkout(tmp_path),
        release_dir=release_dir,
        work_dir=tmp_path / "work",
        tag="v9.9.9",
        destination=parse_destination(destination),
    )
    defaults.update(overrides)
    return PublishOptions(**defaults)


def publish_to(tmp_path: Path, release_dir: Path, destination: str, **overrides):
    runner = overrides.pop("runner", None) or FakeRunner()
    return publish(
        options(tmp_path, release_dir, destination, **overrides), runner=runner
    )


def tree_state(root: Path) -> dict[str, str]:
    state: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[key] = f"link -> {os.readlink(path)}"
        elif path.is_dir():
            state[key] = "dir"
        else:
            state[key] = sha256_file(path)
    return state


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def test_verifies_a_freshly_packaged_release(tmp_path: Path):
    release_dir = make_release(tmp_path)

    verified = verify_release(release_dir)

    assert verified.version == "9.9.9"
    assert verified.name == "orch-hub"
    assert verified.targets == ("darwin-arm64",)
    assert {item.name for item in verified.files} == {
        RELEASE_NAME,
        CHECKSUM_NAME,
        "orch-hub-9.9.9-darwin-arm64.run",
    }
    for item in verified.files:
        assert item.sha256 == sha256_file(release_dir / item.name)
        assert item.size == (release_dir / item.name).stat().st_size


def test_records_the_checksum_files_own_digest(tmp_path: Path):
    # The release's chain of custody stops at SHA256SUMS: it covers every other
    # file and nothing covers it. Publication is the step that can close that.
    release_dir = make_release(tmp_path)

    verified = verify_release(release_dir)

    checksums = next(item for item in verified.files if item.name == CHECKSUM_NAME)
    assert checksums.sha256 == sha256_file(release_dir / CHECKSUM_NAME)
    assert CHECKSUM_NAME not in (release_dir / CHECKSUM_NAME).read_text(encoding="utf-8")


def test_verifies_a_multi_target_release(tmp_path: Path):
    release_dir = make_release(tmp_path, targets=("darwin-arm64", "darwin-x86_64"))

    verified = verify_release(release_dir)

    assert verified.targets == ("darwin-arm64", "darwin-x86_64")
    assert len(verified.files) == 4


def test_refuses_a_missing_release_directory(tmp_path: Path):
    with pytest.raises(PublishError, match="release directory not found"):
        verify_release(tmp_path / "nowhere")


def test_refuses_a_directory_with_no_release_document(tmp_path: Path):
    empty = tmp_path / "release"
    empty.mkdir()

    with pytest.raises(PublishError, match="release_package"):
        verify_release(empty)


def test_refuses_a_malformed_release_document(tmp_path: Path):
    release_dir = make_release(tmp_path)
    (release_dir / RELEASE_NAME).write_text("{", encoding="utf-8")

    with pytest.raises(PublishError, match="malformed"):
        verify_release(release_dir)


def test_refuses_an_unknown_release_schema(tmp_path: Path):
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    document["schema"] = "orch-hub-release/2"
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="orch-hub-release/2"):
        verify_release(release_dir)


def test_refuses_a_release_naming_another_checksum_file(tmp_path: Path):
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    document["checksums"] = "CHECKSUMS.txt"
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="checksum file"):
        verify_release(release_dir)


def test_refuses_a_release_with_no_targets(tmp_path: Path):
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    installer = installer_of(release_dir)
    document["targets"] = {}
    write_release(release_dir, document)
    installer.unlink()
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="declares no targets"):
        verify_release(release_dir)


def test_refuses_a_target_with_no_artifacts(tmp_path: Path):
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    document["targets"]["darwin-arm64"]["artifacts"] = []
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="declares no artifacts"):
        verify_release(release_dir)


def test_refuses_two_targets_claiming_one_artifact(tmp_path: Path):
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    entry = document["targets"]["darwin-arm64"]
    document["targets"]["darwin-x86_64"] = json.loads(json.dumps(entry))
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="two targets"):
        verify_release(release_dir)


def test_refuses_an_artifact_whose_size_disagrees(tmp_path: Path):
    release_dir = make_release(tmp_path)
    installer = installer_of(release_dir)
    with installer.open("ab") as handle:
        handle.write(b"\n")

    with pytest.raises(PublishError, match="its inventory says"):
        verify_release(release_dir)


def test_refuses_an_artifact_whose_bytes_changed_without_its_size(tmp_path: Path):
    release_dir = make_release(tmp_path)
    installer = installer_of(release_dir)
    data = bytearray(installer.read_bytes())
    data[-1] ^= 0xFF
    installer.write_bytes(bytes(data))

    with pytest.raises(PublishError, match="hashes to"):
        verify_release(release_dir)


def test_refuses_a_release_missing_a_file_it_names(tmp_path: Path):
    release_dir = make_release(tmp_path)
    installer_of(release_dir).unlink()

    with pytest.raises(PublishError, match="is missing 1 file"):
        verify_release(release_dir)


def test_refuses_a_release_holding_an_unaccounted_file(tmp_path: Path):
    # A stale artifact from an earlier packaging, or something a hand copy
    # brought along: it would be published as part of the release while
    # appearing in no inventory a consumer can check.
    release_dir = make_release(tmp_path)
    (release_dir / "orch-hub-9.9.8-darwin-arm64.run").write_bytes(b"stale\n")

    with pytest.raises(PublishError, match="no inventory accounts for"):
        verify_release(release_dir)


def test_refuses_a_release_directory_holding_a_subdirectory(tmp_path: Path):
    release_dir = make_release(tmp_path)
    (release_dir / "extra").mkdir()

    with pytest.raises(PublishError, match="not a regular file"):
        verify_release(release_dir)


def test_refuses_a_release_directory_holding_a_symlink(tmp_path: Path):
    release_dir = make_release(tmp_path)
    (release_dir / "latest.run").symlink_to(installer_of(release_dir).name)

    with pytest.raises(PublishError, match="symlink"):
        verify_release(release_dir)


@pytest.mark.parametrize(
    "name",
    ["../escape.run", "sub/inner.run", "-rf.run", "asset.run#label", ".hidden.run", ""],
)
def test_refuses_an_artifact_name_that_is_not_a_plain_file_name(
    tmp_path: Path, name: str
):
    # The name becomes a path under the destination and a positional argument to
    # `gh release create`, which reads a leading dash as an option and splits an
    # asset name on `#` to take a display label.
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    document["targets"]["darwin-arm64"]["artifacts"][0]["name"] = name
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(
        PublishError, match="(has no name|not a plain file name|cannot be passed)"
    ):
        verify_release(release_dir)


@pytest.mark.parametrize("name", [RELEASE_NAME, CHECKSUM_NAME, PUBLICATION_NAME])
def test_refuses_an_artifact_taking_a_name_the_format_reserves(
    tmp_path: Path, name: str
):
    """Two of these describe the release; one is written beside it at publication.

    An artifact filed under any of them is published as a *different* file than
    the one every digest in the release describes: the publication record is
    written into the set last and would overwrite it, and neither inventory
    document can carry a digest that depends on itself. The refusal lands while
    the inventory is being read, before any release file is opened, which is
    why the declaration alone is enough to trigger it.
    """
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    document["targets"]["darwin-arm64"]["artifacts"][0]["name"] = name
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="name the release format reserves"):
        verify_release(release_dir)


def test_refuses_a_target_declaring_more_than_one_artifact(tmp_path: Path):
    """``orch-hub-release/1`` files exactly one installer per target.

    A second artifact is not a smaller release to check — it is a file with no
    checks attached at all, since every check here is selected by what an
    artifact is.
    """
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    extra = release_dir / "notes.txt"
    extra.write_text("release notes\n", encoding="utf-8")
    document["targets"]["darwin-arm64"]["artifacts"].append(
        {
            "name": extra.name,
            "kind": "notes",
            "size": extra.stat().st_size,
            "sha256": sha256_file(extra),
        }
    )
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="declares 2 artifacts"):
        verify_release(release_dir)


def test_refuses_an_artifact_of_a_kind_this_publisher_cannot_check(tmp_path: Path):
    """Fail closed on the kind itself, not only on what it hides.

    The installer here is honest — same bytes, same header — and only its kind
    is unknown. That is still refused: every check in this module is selected
    by what an artifact is, so a kind with no checks attached is a file that
    would be published unchecked, and the next release format to add one has to
    teach this publisher what to do with it first.
    """
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    document["targets"]["darwin-arm64"]["artifacts"][0]["kind"] = "opaque"
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="declares kind 'opaque'"):
        verify_release(release_dir)


def test_refuses_an_installer_relabelled_as_an_uncheckable_kind(tmp_path: Path):
    """A self-consistent release whose only lie is the artifact's kind.

    The installer is built for one target and filed under another, its kind is
    changed to something with no checks attached, and the inventory and the
    checksum file are rewritten so every digest agrees. What still disagrees is
    the header inside the installer — and reading the declared kind to decide
    whether to look at that header is what let the release opt out of the one
    check that proves the set belongs together.
    """
    release_dir = make_release(tmp_path)
    installer = installer_of(release_dir)
    installer.write_bytes(
        installer.read_bytes().replace(
            b"TARGET='darwin-arm64'", b"TARGET='darwin-x86_64'"
        )
    )
    document = read_release(release_dir)
    artifact = document["targets"]["darwin-arm64"]["artifacts"][0]
    artifact["kind"] = "opaque"
    artifact["size"] = installer.stat().st_size
    artifact["sha256"] = sha256_file(installer)
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="declares kind 'opaque'"):
        verify_release(release_dir)


def test_refuses_an_artifact_declaring_a_non_hex_digest(tmp_path: Path):
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    document["targets"]["darwin-arm64"]["artifacts"][0]["sha256"] = "Z" * 64
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="lowercase hex"):
        verify_release(release_dir)


def test_refuses_an_artifact_declaring_a_non_numeric_size(tmp_path: Path):
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    document["targets"]["darwin-arm64"]["artifacts"][0]["size"] = "17"
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="non-numeric size"):
        verify_release(release_dir)


def test_refuses_a_checksum_file_that_misses_a_release_file(tmp_path: Path):
    release_dir = make_release(tmp_path)
    checksums = release_dir / CHECKSUM_NAME
    kept = [
        line
        for line in checksums.read_text(encoding="utf-8").splitlines(keepends=True)
        if RELEASE_NAME not in line
    ]
    checksums.write_text("".join(kept), encoding="utf-8")

    with pytest.raises(PublishError, match="does not cover"):
        verify_release(release_dir)


def test_refuses_a_checksum_file_covering_a_file_that_is_not_there(tmp_path: Path):
    release_dir = make_release(tmp_path)
    checksums = release_dir / CHECKSUM_NAME
    checksums.write_text(
        checksums.read_text(encoding="utf-8") + f"{'0' * 64}  ghost.run\n",
        encoding="utf-8",
    )

    with pytest.raises(PublishError, match="does not have"):
        verify_release(release_dir)


def test_refuses_a_checksum_file_that_disagrees_with_the_bytes(tmp_path: Path):
    # The case a self-consistent forgery reaches: release.json is rewritten to
    # match tampered bytes, and SHA256SUMS — which no longer agrees with either
    # — is what says so.
    release_dir = make_release(tmp_path)
    installer = installer_of(release_dir)
    installer.write_bytes(installer.read_bytes() + b"\n")
    document = read_release(release_dir)
    artifact = document["targets"]["darwin-arm64"]["artifacts"][0]
    artifact["size"] = installer.stat().st_size
    artifact["sha256"] = sha256_file(installer)
    write_release(release_dir, document)

    with pytest.raises(PublishError, match="records .* which hashes to"):
        verify_release(release_dir)


def test_refuses_a_malformed_checksum_line(tmp_path: Path):
    release_dir = make_release(tmp_path)
    checksums = release_dir / CHECKSUM_NAME
    checksums.write_text(
        checksums.read_text(encoding="utf-8") + "not a checksum line\n", encoding="utf-8"
    )

    with pytest.raises(PublishError, match="line 3 is not a sha256 checksum line"):
        verify_release(release_dir)


def test_refuses_a_checksum_file_listing_a_name_twice(tmp_path: Path):
    release_dir = make_release(tmp_path)
    checksums = release_dir / CHECKSUM_NAME
    first = checksums.read_text(encoding="utf-8").splitlines(keepends=True)[0]
    checksums.write_text(checksums.read_text(encoding="utf-8") + first, encoding="utf-8")

    with pytest.raises(PublishError, match="twice"):
        verify_release(release_dir)


def test_refuses_an_installer_filed_under_another_targets_floor(tmp_path: Path):
    # Digests prove each file is the file its entry describes; this proves the
    # set belongs together. The header is what every installing machine reads.
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    document["targets"]["darwin-arm64"]["min_macos"] = "26.0"
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="MIN_MACOS='11.0'"):
        verify_release(release_dir)


def test_refuses_an_installer_filed_under_another_target(tmp_path: Path):
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    document["targets"]["darwin-x86_64"] = document["targets"].pop("darwin-arm64")
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="TARGET='darwin-arm64'"):
        verify_release(release_dir)


def test_refuses_an_installer_with_no_recognisable_header(tmp_path: Path):
    release_dir = make_release(tmp_path)
    installer = installer_of(release_dir)
    installer.write_bytes(b"#!/bin/sh\nexit 0\n")
    document = read_release(release_dir)
    artifact = document["targets"]["darwin-arm64"]["artifacts"][0]
    artifact["size"] = installer.stat().st_size
    artifact["sha256"] = sha256_file(installer)
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="not an orch-hub installer"):
        verify_release(release_dir)


# --------------------------------------------------------------------------
# The tag gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("9.9.9", "does not start with"),
        ("v9.9.8", "names version"),
        ("v9.9.9/1", "single directory name"),
        (".v9.9.9", "single directory name"),
        ("-v9.9.9", "starts with a dash"),
        ("", "no tag given"),
    ],
)
def test_refuses_a_tag_that_does_not_name_this_release(tag: str, expected: str):
    with pytest.raises(PublishError, match=expected):
        check_tag(tag, "9.9.9")


def test_refuses_artifacts_built_from_another_version_of_the_source(tmp_path: Path):
    # `dist/` survives every build, so what is sitting there after a version
    # bump is the previous version's artifact set.
    release_dir = make_release(tmp_path)

    with pytest.raises(PublishError, match="declares version '9.9.10'"):
        publish_to(
            tmp_path,
            release_dir,
            str(tmp_path / "storage"),
            tag="v9.9.9",
            checkout=stub_checkout(tmp_path, "9.9.10"),
        )


def test_refuses_a_checkout_that_declares_no_version(tmp_path: Path):
    release_dir = make_release(tmp_path)

    with pytest.raises(PublishError, match="cannot read the version"):
        publish_to(
            tmp_path,
            release_dir,
            str(tmp_path / "storage"),
            checkout=tmp_path / "not-a-checkout",
        )


def test_accepts_the_tag_naming_this_release():
    assert check_tag("v9.9.9", "9.9.9") is None


def test_resolves_a_tag_the_checkout_is_at(tmp_path: Path):
    runner = FakeRunner()

    revision = resolve_tag(tmp_path, "v9.9.9", runner=runner)

    assert revision == Revision(tag="v9.9.9", commit=COMMIT)
    assert runner.git_calls() == [
        (GIT_BIN, "rev-parse", "--verify", "--quiet", "refs/tags/v9.9.9^{commit}"),
        (GIT_BIN, "rev-parse", "--verify", "HEAD^{commit}"),
        (GIT_BIN, *STATUS_ARGV),
    ]
    assert all(call["cwd"] == tmp_path for call in runner.kwargs)


def test_refuses_a_tag_the_checkout_does_not_have(tmp_path: Path):
    with pytest.raises(PublishError, match="has no tag"):
        resolve_tag(tmp_path, "v9.9.9", runner=FakeRunner(tag_commit=None))


def test_refuses_a_checkout_that_is_not_at_the_tag(tmp_path: Path):
    with pytest.raises(PublishError, match="not at tag"):
        resolve_tag(tmp_path, "v9.9.9", runner=FakeRunner(head=OTHER_COMMIT))


def test_refuses_a_checkout_with_uncommitted_changes(tmp_path: Path):
    # A tagged commit with edits on top is a checkout whose state has no name.
    with pytest.raises(PublishError, match="uncommitted changes"):
        resolve_tag(tmp_path, "v9.9.9", runner=FakeRunner(status=" M orch_hub/api.py\n"))


def test_refuses_a_tag_object_that_does_not_resolve_to_a_commit(tmp_path: Path):
    with pytest.raises(PublishError, match="has no tag"):
        resolve_tag(tmp_path, "v9.9.9", runner=FakeRunner(tag_commit="not-a-sha"))


def test_reports_a_missing_git(tmp_path: Path):
    with pytest.raises(PublishError, match="git is not installed"):
        resolve_tag(tmp_path, "v9.9.9", runner=FakeRunner(missing=GIT_BIN))


def test_resolves_a_tag_against_a_real_repository(tmp_path: Path):
    """One end-to-end pass over real git, so the argv is known to be real argv."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=checkout, env=env, check=True, capture_output=True
        )

    run("init", "--quiet")
    (checkout / "file.txt").write_text("one\n", encoding="utf-8")
    run("add", "file.txt")
    run("commit", "--quiet", "-m", "one")
    run("tag", "-a", "v9.9.9", "-m", "release")

    revision = resolve_tag(checkout, "v9.9.9")

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True
    ).stdout.strip()
    assert revision == Revision(tag="v9.9.9", commit=head)

    (checkout / "file.txt").write_text("two\n", encoding="utf-8")
    with pytest.raises(PublishError, match="uncommitted changes"):
        resolve_tag(checkout, "v9.9.9")

    # An untracked file is a state the tag does not name either, and whether git
    # volunteers it is a configuration setting. The argv states the question.
    run("checkout", "--", "file.txt")
    run("config", "status.showUntrackedFiles", "no")
    (checkout / "extra.txt").write_text("three\n", encoding="utf-8")
    with pytest.raises(PublishError, match="uncommitted changes"):
        resolve_tag(checkout, "v9.9.9")


# --------------------------------------------------------------------------
# The payload-to-source gate
# --------------------------------------------------------------------------


def test_binds_every_payload_to_the_commit_the_tag_names(tmp_path: Path):
    release_dir = make_release(tmp_path, targets=("darwin-arm64", "darwin-x86_64"))
    release = verify_release(release_dir)

    assert check_payload_revision(release, Revision(tag="v9.9.9", commit=COMMIT)) is None


def test_refuses_artifacts_frozen_from_another_commit(tmp_path: Path):
    # Everything else about this release is correct: the version matches, the
    # inventory recomputes, the checkout is at the tag. The bytes are simply
    # from a build that happened before the source changed.
    release_dir = make_release(tmp_path, source_revision=OTHER_COMMIT)

    with pytest.raises(PublishError) as raised:
        publish_to(tmp_path, release_dir, str(tmp_path / "storage"))

    message = str(raised.value)
    assert OTHER_COMMIT[:12] in message and COMMIT[:12] in message
    assert not (tmp_path / "storage").exists()


def test_refuses_artifacts_frozen_from_a_modified_tree(tmp_path: Path):
    # The recorded revision is the tag's commit; what it is not is the source
    # that produced these bytes, and one comparison catches both.
    release_dir = make_release(tmp_path, source_revision=f"{COMMIT}-dirty")

    with pytest.raises(PublishError, match="uncommitted changes"):
        publish_to(tmp_path, release_dir, str(tmp_path / "storage"))


def test_refuses_a_payload_that_recorded_no_revision(tmp_path: Path):
    release_dir = make_release(tmp_path, source_revision=None)

    with pytest.raises(PublishError, match="records no source revision"):
        publish_to(tmp_path, release_dir, str(tmp_path / "storage"))


def test_refuses_a_payload_whose_recorded_revision_is_not_a_commit(tmp_path: Path):
    # Packaging refuses this shape, so reaching publication means the document
    # was edited afterwards — the moment to be least trusting, not most.
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    document["targets"]["darwin-arm64"]["runtime"]["source_revision"] = "HEAD"
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="not a commit"):
        publish_to(tmp_path, release_dir, str(tmp_path / "storage"))


def test_refuses_a_target_carrying_no_runtime_manifest(tmp_path: Path):
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    del document["targets"]["darwin-arm64"]["runtime"]
    write_release(release_dir, document)
    resync_checksums(release_dir)

    with pytest.raises(PublishError, match="carries no runtime manifest"):
        publish_to(tmp_path, release_dir, str(tmp_path / "storage"))


def test_one_stale_target_stops_a_multi_target_release(tmp_path: Path):
    # One version published as one set: a stale payload for the second target is
    # exactly the artifact nobody would look at twice.
    source = tmp_path / "payloads"
    payloads = (
        make_payload(source, target="darwin-arm64"),
        make_payload(source, target="darwin-x86_64", source_revision=OTHER_COMMIT),
    )
    result = package_release(
        PackageOptions(
            payload_dirs=payloads,
            out_dir=tmp_path / "dist" / "release",
            work_dir=tmp_path / "build",
            template_path=TEMPLATE,
            allow_findings=True,
        )
    )

    with pytest.raises(PublishError, match="darwin-x86_64 payload"):
        publish_to(tmp_path, result.out_dir, str(tmp_path / "storage"))


# --------------------------------------------------------------------------
# Destinations
# --------------------------------------------------------------------------


def test_parses_a_path_as_the_filesystem_destination(tmp_path: Path):
    destination = parse_destination(str(tmp_path / "releases"))

    assert destination.scheme == "directory"
    assert destination.location == str(tmp_path / "releases")


def test_parses_a_github_destination():
    assert parse_destination("github:qinglin89/orch-hub") == Destination(
        scheme="github", location="qinglin89/orch-hub"
    )


@pytest.mark.parametrize(
    "value, expected",
    [
        ("github:qinglin89", "not owner/name"),
        ("github:qinglin89/orch-hub/extra", "not owner/name"),
        ("github:/orch-hub", "not owner/name"),
        ("s3://bucket/path", "unsupported scheme"),
        ("   ", "no destination given"),
    ],
)
def test_refuses_a_destination_it_cannot_reach(value: str, expected: str):
    with pytest.raises(PublishError, match=expected):
        parse_destination(value)


# --------------------------------------------------------------------------
# Publication to private storage
# --------------------------------------------------------------------------


def test_publishes_the_artifact_set_under_the_tag(tmp_path: Path):
    release_dir = make_release(tmp_path)
    destination = tmp_path / "storage"

    result = publish_to(tmp_path, release_dir, str(destination))

    published = destination / "v9.9.9"
    assert Path(result.published).resolve() == published.resolve()
    assert result.tag == "v9.9.9"
    assert result.commit == COMMIT
    assert sorted(path.name for path in published.iterdir()) == [
        CHECKSUM_NAME,
        "orch-hub-9.9.9-darwin-arm64.run",
        PUBLICATION_NAME,
        RELEASE_NAME,
    ]


def test_published_artifacts_are_byte_identical_to_the_release(tmp_path: Path):
    release_dir = make_release(tmp_path)
    destination = tmp_path / "storage"

    publish_to(tmp_path, release_dir, str(destination))

    for path in release_dir.iterdir():
        assert sha256_file(destination / "v9.9.9" / path.name) == sha256_file(path)


def test_publication_records_the_tag_commit_and_every_digest(tmp_path: Path):
    release_dir = make_release(tmp_path)
    destination = tmp_path / "storage"

    publish_to(tmp_path, release_dir, str(destination))

    document = json.loads(
        (destination / "v9.9.9" / PUBLICATION_NAME).read_text(encoding="utf-8")
    )
    assert document["schema"] == PUBLICATION_SCHEMA
    assert document["tag"] == "v9.9.9"
    assert document["commit"] == COMMIT
    assert document["version"] == "9.9.9"
    recorded = {item["name"]: item["sha256"] for item in document["files"]}
    assert recorded == {
        path.name: sha256_file(path) for path in release_dir.iterdir()
    }


def test_a_published_tag_is_not_republished(tmp_path: Path):
    release_dir = make_release(tmp_path)
    destination = tmp_path / "storage"
    publish_to(tmp_path, release_dir, str(destination))
    before = tree_state(destination)

    with pytest.raises(PublishError, match="already exists"):
        publish_to(tmp_path, release_dir, str(destination))

    assert tree_state(destination) == before


def test_a_second_tag_publishes_beside_the_first(tmp_path: Path):
    release_dir = make_release(tmp_path)
    other = make_release(tmp_path / "other", version="9.9.10")
    destination = tmp_path / "storage"

    publish_to(tmp_path, release_dir, str(destination))
    publish_to(
        tmp_path,
        other,
        str(destination),
        tag="v9.9.10",
        checkout=stub_checkout(tmp_path, "9.9.10"),
    )

    assert sorted(path.name for path in destination.iterdir()) == ["v9.9.10", "v9.9.9"]


def test_publication_leaves_no_scratch_directory(tmp_path: Path):
    release_dir = make_release(tmp_path)

    publish_to(tmp_path, release_dir, str(tmp_path / "storage"))

    work = tmp_path / "work"
    assert not work.exists() or list(work.iterdir()) == []


def test_a_refused_release_publishes_nothing(tmp_path: Path):
    release_dir = make_release(tmp_path)
    installer_of(release_dir).unlink()
    destination = tmp_path / "storage"

    with pytest.raises(PublishError, match="is missing"):
        publish_to(tmp_path, release_dir, str(destination))

    assert not destination.exists()
    assert not (tmp_path / "work").exists()


def test_a_refused_tag_publishes_nothing(tmp_path: Path):
    release_dir = make_release(tmp_path)
    destination = tmp_path / "storage"

    with pytest.raises(PublishError, match="uncommitted changes"):
        publish_to(
            tmp_path,
            release_dir,
            str(destination),
            runner=FakeRunner(status=" M orch_hub/api.py\n"),
        )

    assert not destination.exists()


def test_refuses_a_destination_inside_the_release(tmp_path: Path):
    release_dir = make_release(tmp_path)

    with pytest.raises(PublishError, match="destination is inside the release"):
        publish_to(tmp_path, release_dir, str(release_dir / "published"))


def test_refuses_a_release_inside_the_destination(tmp_path: Path):
    release_dir = make_release(tmp_path)

    with pytest.raises(PublishError, match="release directory is inside the destination"):
        publish_to(tmp_path, release_dir, str(release_dir.parent))


def test_refuses_a_scratch_directory_inside_the_release(tmp_path: Path):
    release_dir = make_release(tmp_path)

    with pytest.raises(PublishError, match="scratch directory is inside the release"):
        publish_to(
            tmp_path,
            release_dir,
            str(tmp_path / "storage"),
            work_dir=release_dir / "work",
        )


def test_refuses_a_release_inside_the_scratch_directory(tmp_path: Path):
    """The scratch tree is where this run clears, so the release cannot be in it.

    Which concrete path gets emptied is not knowable to a caller: the staging
    directory is taken inside the scratch directory under a name ``mkdtemp``
    picks, because a predictable one is shared by two publishers of the same
    tag. Holding the whole tree apart is what makes that unknowable path
    harmless — and it is the safe side of the comparison, since every path this
    run can possibly destroy on its own side lies below it.

    Deliberately stricter than the destruction it prevents: a release that
    happens not to collide with today's ``mkdtemp`` name is still a release
    living inside another command's working area, and refusing it costs
    nothing (packaging leaves ``dist/release``, publication works in
    ``build/release``).
    """
    release_dir = make_release(tmp_path)
    work = tmp_path / "work"
    nested = work / "release"
    nested.parent.mkdir(parents=True)
    shutil.move(str(release_dir), str(nested))
    before = tree_state(nested)

    with pytest.raises(PublishError, match="release directory is inside the scratch"):
        publish_to(tmp_path, nested, str(tmp_path / "storage"), work_dir=work)

    assert tree_state(nested) == before


def test_refuses_a_destination_inside_the_scratch_directory(tmp_path: Path):
    """Same rule, and what it protects here is a tag already published.

    A destination below the scratch tree puts tags that were handed out into
    the directory this run creates and clears in, which would make "a published
    tag is never replaced" depend on which name ``mkdtemp`` happened to pick.
    """
    release_dir = make_release(tmp_path)
    work = tmp_path / "work"
    destination = work / "storage"
    publish_to(
        tmp_path, release_dir, str(destination), work_dir=tmp_path / "other-work"
    )
    before = tree_state(destination)

    with pytest.raises(PublishError, match="destination is inside the scratch"):
        publish_to(tmp_path, release_dir, str(destination), work_dir=work)

    assert tree_state(destination) == before


def test_refuses_a_scratch_directory_inside_the_destination(tmp_path: Path):
    """The remaining direction: working files among the tags.

    Not destructive, but the destination is the one directory whose contents
    are a promise to somebody else, and a staging directory that is mid-fill —
    or left behind by a run that died — is not part of that promise.
    """
    release_dir = make_release(tmp_path)
    destination = tmp_path / "storage"

    with pytest.raises(
        PublishError, match="scratch directory is inside the destination"
    ):
        publish_to(
            tmp_path, release_dir, str(destination), work_dir=destination / "work"
        )

    assert not destination.exists()


def test_refuses_a_release_that_changed_after_it_was_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The published bytes are the verified bytes, not the named ones.

    Verification reads the release directory and the copy happens after it. A
    release that changes across that gap — a rebuild finishing in another
    terminal, a hand edit — would otherwise be published under a checksum file
    describing what used to be there, leaving the published copy as the only
    witness and disagreeing with itself.
    """
    release_dir = make_release(tmp_path)
    destination = tmp_path / "storage"
    verified_bytes = installer_of(release_dir).read_bytes()
    real = publish_module.verify_release

    def verify_then_tamper(directory: Path):
        verified = real(directory)
        installer = installer_of(release_dir)
        installer.write_bytes(installer.read_bytes() + b"\n# smuggled\n")
        return verified

    monkeypatch.setattr(publish_module, "verify_release", verify_then_tamper)

    with pytest.raises(PublishError, match="its inventory says"):
        publish_to(tmp_path, release_dir, str(destination))

    assert not destination.exists()
    assert installer_of(release_dir).read_bytes() != verified_bytes


def test_a_release_naming_the_publication_record_is_never_published(tmp_path: Path):
    """The whole collision, end to end: declared, verified, copied, overwritten.

    Publishing it succeeded before, and the file that landed under the release's
    own inventory digest was the publication record instead of the artifact.
    """
    release_dir = make_release(tmp_path)
    document = read_release(release_dir)
    installer_of(release_dir).rename(release_dir / PUBLICATION_NAME)
    document["targets"]["darwin-arm64"]["artifacts"][0]["name"] = PUBLICATION_NAME
    write_release(release_dir, document)
    resync_checksums(release_dir)
    destination = tmp_path / "storage"

    with pytest.raises(PublishError, match="name the release format reserves"):
        publish_to(tmp_path, release_dir, str(destination))

    assert not destination.exists()


def test_a_tag_claimed_after_verification_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Checking that a tag is free and then placing it are two operations.

    A publisher that wins the tag in between had its release replaced by a
    check that had already passed — silently, since the losing run reported
    success. The claim has to be the same operation as the creation.
    """
    release_dir = make_release(tmp_path)
    destination = tmp_path / "storage"
    real = publish_module._bring_alongside

    def claim_it_first(stage: Path, parent: Path, name: str) -> Path:
        incoming = real(stage, parent, name)
        winner = parent / name
        winner.mkdir(parents=True)
        (winner / "orch-hub-9.9.9-darwin-arm64.run").write_bytes(b"the winner\n")
        return incoming

    monkeypatch.setattr(publish_module, "_bring_alongside", claim_it_first)

    with pytest.raises(PublishError, match="already exists"):
        publish_to(tmp_path, release_dir, str(destination))

    published = destination / "v9.9.9"
    assert [path.name for path in published.iterdir()] == [
        "orch-hub-9.9.9-darwin-arm64.run"
    ]
    assert (published / "orch-hub-9.9.9-darwin-arm64.run").read_bytes() == b"the winner\n"
    assert sorted(path.name for path in destination.iterdir()) == ["v9.9.9"]


def test_a_publication_never_hands_out_a_stage_another_one_is_filling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two publications of one tag out of one build directory — the default.

    The staging path used to be ``<work>/publish-<tag>`` for both of them, and
    the second one clears and refills it. Interleaved here at the only point
    where that is silent — after the first has checked its stage and before it
    moves it — the first published the second's artifacts, reported success,
    and returned its own digests. Nothing downstream disagreed with anything,
    because the published set was somebody else's and internally consistent:
    ``shasum -c`` passes inside it, and the only witness is a result nobody
    compares against the bytes.

    The stage is this invocation's own now, so the interleaved publication
    fills a different directory and neither one can see the other's.
    """
    mine = make_release(tmp_path, label="mine")
    theirs = make_release(tmp_path, label="theirs")
    assert sha256_file(installer_of(mine)) != sha256_file(installer_of(theirs))
    destination = tmp_path / "storage"
    work = tmp_path / "work"
    before = tree_state(theirs)
    real = publish_module._check_stage
    interleaved: list[Path] = []

    def check_then_let_the_other_stage(stage: Path, release) -> None:
        real(stage, release)
        if interleaved:
            return
        layout = publish_module._resolve_layout(
            options(tmp_path, theirs, str(destination), work_dir=work)
        )
        interleaved.append(
            publish_module._stage(
                layout,
                publish_module.verify_release(theirs),
                publish_module.Revision(tag="v9.9.9", commit=COMMIT),
            )
        )

    monkeypatch.setattr(publish_module, "_check_stage", check_then_let_the_other_stage)

    result = publish_to(tmp_path, mine, str(destination), work_dir=work)

    assert interleaved, "the competing publication never staged"
    published = destination / "v9.9.9"
    for item in result.files:
        assert sha256_file(published / item.name) == item.sha256
        assert sha256_file(mine / item.name) == item.sha256
    # Each side kept its own: neither stage was the other's, and the release
    # that lost the tag was never read out of.
    other_stage = interleaved[0]
    assert sha256_file(other_stage / installer_of(theirs).name) == sha256_file(
        installer_of(theirs)
    )
    assert tree_state(theirs) == before


def test_concurrent_publishers_of_one_tag_leave_exactly_one_release(tmp_path: Path):
    """Four publishers, one tag, one build directory, no coordination.

    One build directory because that is the one the command defaults to:
    publishers handed separate scratch directories never touch each other's
    staging, so they cannot say whether sharing one is safe. And a release of
    its own bytes each, so "the winner published its own artifacts" is a claim
    with a failing case rather than a tautology.

    The assertion holds for every interleaving rather than for a lucky one:
    whoever creates the directory owns the tag, and everyone else is refused
    before they have written anything into the destination.
    """
    releases = [make_release(tmp_path, label=f"r{index}") for index in range(4)]
    assert len({sha256_file(installer_of(item)) for item in releases}) == len(releases)
    destination = tmp_path / "storage"
    destination.mkdir()
    work = tmp_path / "work"
    # Built before the race: the shared helpers write a stub checkout, and a
    # thread reading it mid-write would make this test fail for a reason that
    # has nothing to do with the tag.
    plans = [
        (release_dir, options(tmp_path, release_dir, str(destination), work_dir=work))
        for release_dir in releases
    ]
    start = threading.Barrier(len(plans))
    lock = threading.Lock()
    outcomes: list[tuple[Path, object]] = []

    def attempt(release_dir: Path, plan) -> None:
        start.wait()
        try:
            outcome: object = publish(plan, runner=FakeRunner())
        except PublishError as exc:
            outcome = exc
        with lock:
            outcomes.append((release_dir, outcome))

    threads = [threading.Thread(target=attempt, args=pair) for pair in plans]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [pair for pair in outcomes if not isinstance(pair[1], PublishError)]
    refused = [item for _, item in outcomes if isinstance(item, PublishError)]
    assert len(winners) == 1
    assert all("already exists" in str(item) for item in refused)
    assert sorted(path.name for path in destination.iterdir()) == ["v9.9.9"]

    source, result = winners[0]
    published = destination / "v9.9.9"
    assert sorted(path.name for path in published.iterdir()) == sorted(
        [*(item.name for item in result.files), PUBLICATION_NAME]
    )
    for item in result.files:
        assert sha256_file(published / item.name) == item.sha256
        assert sha256_file(source / item.name) == item.sha256
    # Every publisher, winner and refused alike, took its stage away with it.
    assert list(work.iterdir()) == []


def test_publication_leaves_no_scratch_beside_the_published_tag(tmp_path: Path):
    """The transfer lands under a name no competing publisher can be holding,
    and it is gone by the time the tag is there."""
    release_dir = make_release(tmp_path)
    destination = tmp_path / "storage"

    publish_to(tmp_path, release_dir, str(destination))

    assert sorted(path.name for path in destination.iterdir()) == ["v9.9.9"]


def test_publishing_one_release_twice_produces_the_same_publication(tmp_path: Path):
    """A publication is a pure function of its tag and its bytes.

    Which is what makes "the artifacts behind v9.9.9" checkable at all: two
    publications of one release can be compared file for file, so a difference
    is evidence rather than noise.
    """
    release_dir = make_release(tmp_path)

    publish_to(tmp_path, release_dir, str(tmp_path / "storage-a"))
    publish_to(tmp_path, release_dir, str(tmp_path / "storage-b"))

    assert tree_state(tmp_path / "storage-a") == tree_state(tmp_path / "storage-b")


# --------------------------------------------------------------------------
# Publication to a release host
# --------------------------------------------------------------------------


def test_creates_a_github_release_at_the_existing_tag(tmp_path: Path):
    release_dir = make_release(tmp_path)
    runner = FakeRunner()

    result = publish_to(
        tmp_path, release_dir, "github:qinglin89/orch-hub", runner=runner
    )

    argv = runner.gh_call()
    assert argv[:6] == (
        GH_BIN, "release", "create", "v9.9.9", "--repo", "qinglin89/orch-hub"
    )
    # Without --verify-tag, `gh` creates a missing tag from the default branch's
    # head — publishing artifacts under a tag pointing at a commit they were
    # never verified against.
    assert "--verify-tag" in argv
    assert result.published == "https://github.com/o/n/releases/tag/v9.9.9"
    assert result.destination == "github:qinglin89/orch-hub"


def test_uploads_every_release_file_and_the_publication_record(tmp_path: Path):
    release_dir = make_release(tmp_path)
    runner = FakeRunner()

    publish_to(tmp_path, release_dir, "github:qinglin89/orch-hub", runner=runner)

    uploaded = [
        item
        for item in runner.gh_call()
        if item.endswith((".run", ".json", CHECKSUM_NAME))
    ]
    assert sorted(Path(item).name for item in uploaded) == [
        CHECKSUM_NAME,
        "orch-hub-9.9.9-darwin-arm64.run",
        PUBLICATION_NAME,
        RELEASE_NAME,
    ]
    # Absolute paths, so no file name can be read as an option by the host CLI.
    assert all(Path(item).is_absolute() for item in uploaded)


def test_release_notes_state_the_floor_and_the_digests(tmp_path: Path):
    release_dir = make_release(tmp_path)
    verified = verify_release(release_dir)

    notes = release_notes(verified, Revision(tag="v9.9.9", commit=COMMIT))

    assert "darwin-arm64: macOS 11.0 or newer" in notes
    installer = installer_of(release_dir)
    assert f"{sha256_file(installer)}  {installer.name}" in notes


def test_reports_a_failed_release_host(tmp_path: Path):
    release_dir = make_release(tmp_path)
    runner = FakeRunner(
        gh_result=CommandResult(("gh",), 1, "", "release already exists\n")
    )

    with pytest.raises(PublishError, match="release already exists"):
        publish_to(tmp_path, release_dir, "github:qinglin89/orch-hub", runner=runner)


def test_reports_a_missing_release_host_cli(tmp_path: Path):
    release_dir = make_release(tmp_path)

    with pytest.raises(PublishError, match="gh is not installed"):
        publish_to(
            tmp_path,
            release_dir,
            "github:qinglin89/orch-hub",
            runner=FakeRunner(missing=GH_BIN),
        )


def test_a_failed_upload_leaves_no_scratch_directory(tmp_path: Path):
    release_dir = make_release(tmp_path)
    runner = FakeRunner(gh_result=CommandResult(("gh",), 1, "", "nope\n"))

    with pytest.raises(PublishError):
        publish_to(tmp_path, release_dir, "github:qinglin89/orch-hub", runner=runner)

    work = tmp_path / "work"
    assert not work.exists() or list(work.iterdir()) == []


def test_a_release_that_changed_after_verification_is_never_uploaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The staged bytes are checked before the destination, whichever it is.

    An upload is worse than a copy here: the artifacts are then somewhere this
    machine cannot take them back from, under a tag that promises they will not
    change.
    """
    release_dir = make_release(tmp_path)
    runner = FakeRunner()
    real = publish_module.verify_release

    def verify_then_tamper(directory: Path):
        verified = real(directory)
        installer = installer_of(release_dir)
        installer.write_bytes(installer.read_bytes() + b"\n# smuggled\n")
        return verified

    monkeypatch.setattr(publish_module, "verify_release", verify_then_tamper)

    with pytest.raises(PublishError, match="its inventory says"):
        publish_to(tmp_path, release_dir, "github:qinglin89/orch-hub", runner=runner)

    assert [call for call in runner.calls if call[0] == GH_BIN] == []


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def test_default_release_directory_is_where_packaging_leaves_one(tmp_path: Path):
    assert default_release_dir(tmp_path) == tmp_path / "dist" / "release"


def real_checkout(root: Path, tag: str) -> Path:
    """A repository on ``tag`` with a clean tree, for the paths that run git."""
    checkout = stub_checkout(root, tag.lstrip("v"))
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=checkout, env=env, check=True, capture_output=True
        )

    git("init", "--quiet")
    git("add", "orch_hub/__init__.py")
    git("commit", "--quiet", "-m", tag)
    git("tag", "-a", tag, "-m", tag)
    return checkout


def head_of(checkout: Path) -> str:
    """The commit a real test repository is at.

    The payloads published from it have to record this, so the release built for
    such a test is built *after* the repository — the same order a maintainer
    works in, and the reason these two tests cannot share the module's fixed
    :data:`COMMIT`.
    """
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_cli_publishes_and_names_everything_it_wrote(tmp_path: Path, capsys):
    checkout = real_checkout(tmp_path, "v9.9.9")
    release_dir = make_release(tmp_path, source_revision=head_of(checkout))
    destination = tmp_path / "storage"

    code = main(
        [
            "--checkout",
            str(checkout),
            "--release",
            str(release_dir),
            "--work",
            str(tmp_path / "work"),
            "--tag",
            "v9.9.9",
            "--to",
            str(destination),
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    # Everything that landed is named, the publication record included: it is in
    # neither inventory, so nothing else would tell the operator it is there.
    for name in sorted(path.name for path in (destination / "v9.9.9").iterdir()):
        assert name in out
    assert "tag:" in out and "commit:" in out


def test_cli_emits_the_result_as_json(tmp_path: Path, capsys):
    checkout = real_checkout(tmp_path, "v9.9.9")
    release_dir = make_release(tmp_path, source_revision=head_of(checkout))

    code = main(
        [
            "--checkout",
            str(checkout),
            "--release",
            str(release_dir),
            "--work",
            str(tmp_path / "work"),
            "--tag",
            "v9.9.9",
            "--to",
            str(tmp_path / "storage"),
            "--json",
        ]
    )

    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["tag"] == "v9.9.9"
    assert document["version"] == "9.9.9"
    assert Path(document["published"]).is_dir()


def test_cli_reports_a_refusal_without_a_traceback(tmp_path: Path, capsys):
    code = main(
        [
            "--checkout",
            str(tmp_path),
            "--release",
            str(tmp_path / "nowhere"),
            "--tag",
            "v9.9.9",
            "--to",
            str(tmp_path / "storage"),
        ]
    )

    assert code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert not captured.out


def test_publication_document_carries_no_timestamp(tmp_path: Path):
    # Two publications of one release must be comparable; a field nobody can
    # verify would only make identical publications look different.
    release_dir = make_release(tmp_path)
    document = publication_document(
        verify_release(release_dir), Revision(tag="v9.9.9", commit=COMMIT)
    )

    assert "created" not in document
    assert "timestamp" not in document
    assert json.dumps(document, sort_keys=True) == json.dumps(
        publication_document(
            verify_release(release_dir), Revision(tag="v9.9.9", commit=COMMIT)
        ),
        sort_keys=True,
    )
