"""Hand a packaged release to the people who install it, under a version tag.

Run from a checkout, after :mod:`orch_hub.release_package`::

    python3 -m orch_hub.release_publish --tag v0.1.0 --to ~/Releases/orch-hub
    python3 -m orch_hub.release_publish --tag v0.1.0 --to github:qinglin89/orch-hub

Packaging leaves ``dist/release`` — three files that happen to be sitting on
the maintainer's disk. Publication is what turns that into something an
engineer somewhere else can be pointed at, and it is the last moment anything
checks. Three properties are the whole point:

- **The tag is a gate, not a label.** ``--tag v0.1.0`` is refused unless the
  release really is version ``0.1.0``, that tag exists in this checkout, the
  checkout is *at* it, and the working tree is clean. Without those, "the
  artifacts for v0.1.0" means "whatever happened to be in ``dist/`` when
  somebody typed a tag name".
- **The bytes are re-verified against their own inventory.** Every digest,
  size, and file name in ``release.json`` is recomputed from the files on disk,
  ``SHA256SUMS`` must agree with it and cover exactly the same set, and the
  ``.run`` header must name the version and target it is filed under. The
  release's own chain stops at ``SHA256SUMS`` — nothing inside a release
  attests to the checksum file itself — so publication pins its digest too, in
  a ``publication.json`` bound to the tag and the commit.
- **A published tag is final.** Publication refuses a destination that already
  holds this tag rather than replacing it, because the one guarantee a consumer
  cannot verify locally is that the bytes behind a version did not change after
  they were handed out.

The build host has to be the target — there is no cross-building a macOS ARM64
payload — so the artifacts are produced on a machine, not by the tag. What the
tag drives is this step: publication takes a *built release directory* as its
input, so moving production onto a CI runner later changes who fills
``dist/release`` and nothing about what is checked before it leaves.

Destinations are named by scheme so that "private storage now, public release
hosting later" is one flag rather than one rewrite:

- a filesystem path — the artifact set lands in ``<path>/<tag>/``. This is the
  channel that hands somebody files with no GitHub involved at all, which is
  what a private engineering release needs: a consumer of a private repo's
  GitHub release still needs read access *to the repository* to download it.
- ``github:<owner>/<name>`` — a GitHub release at the tag, for the day the
  artifacts are meant to be public (or for a separate public releases repo,
  which is the same flag with a different value).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .atomic_dir import PlacementError, discard_path, place_directory
from .release_build import BuildError, read_version
from .release_package import (
    CHECKSUM_KIND,
    CHECKSUM_NAME,
    INSTALLER_KIND,
    PackageError,
    RELEASE_KIND,
    RELEASE_NAME,
    RELEASE_SCHEMA,
    sha256_file,
)
from .subprocess_utils import CommandNotFoundError, CommandResult, Runner, run_command

PUBLICATION_SCHEMA = "orch-hub-publication/1"
"""Schema of ``publication.json``.

Separate from ``orch-hub-release/1`` because it answers a different question.
``release.json`` describes what the artifacts *are* and is a pure function of
the payload; this describes what was published, under which tag, from which
commit — facts that do not exist until somebody decides to hand the release
out."""

PUBLICATION_NAME = "publication.json"

TAG_PREFIX = "v"
"""How a tag names a version. ``v0.1.0`` publishes ``0.1.0`` and nothing else."""

GIT_BIN = "git"
GH_BIN = "gh"

GITHUB_SCHEME = "github:"

_GIT_TIMEOUT = 30.0
_GH_TIMEOUT = 900.0
"""Uploading a release asset is a multi-megabyte transfer over somebody's
network; the git probes are local ref reads."""

SHA256_RE = re.compile(r"[0-9a-f]{64}")
"""A digest as ``shasum -a 256`` writes it, and as this module recomputes it.

Lowercase hex only: a release that spelled its digests any other way would
compare unequal to every recomputation here and to ``shasum -c`` on the far
side, so accepting one would only move the failure to a machine that cannot
report it."""

COMMIT_RE = re.compile(r"[0-9a-f]{40}")

_HEADER_BYTES = 8192
"""How much of a ``.run`` is read to find its embedded identity.

The assignments live in the first thirty lines of the checked-in template and
the payload is appended after the script, so this is a small fixed prefix
rather than a bounded read of an unbounded file."""

_HEADER_FIELDS = ("VERSION", "TARGET", "MIN_MACOS")

_CHECKSUM_LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")
"""``shasum -a 256`` output: digest, two spaces, name.

Spelled as the format is written rather than as ``split()`` would read it, so a
name with leading spaces cannot be silently renamed on the way in."""


class PublishError(RuntimeError):
    """Raised when a release cannot be published."""


@dataclass(frozen=True)
class ReleaseFile:
    """One file of a verified release, described the way it was checked."""

    name: str
    kind: str
    size: int
    sha256: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class VerifiedRelease:
    """A release directory that has been re-derived from its own bytes."""

    directory: Path
    name: str
    version: str
    release: dict
    files: tuple[ReleaseFile, ...]
    """Every file in the directory, checksum file included."""

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(sorted(self.release.get("targets", {})))


@dataclass(frozen=True)
class Revision:
    """The tag being published and the commit it names."""

    tag: str
    commit: str


@dataclass(frozen=True)
class Destination:
    """Where a publication goes, parsed from one ``--to`` value."""

    scheme: str
    """``directory`` or ``github``."""

    location: str
    """A filesystem path, or ``owner/name``."""

    def describe(self) -> str:
        return self.location if self.scheme == "directory" else f"github:{self.location}"


@dataclass(frozen=True)
class PublishOptions:
    checkout: Path
    release_dir: Path
    work_dir: Path
    tag: str
    destination: Destination


@dataclass(frozen=True)
class PublishResult:
    tag: str
    commit: str
    version: str
    destination: str
    published: str
    """Where the artifacts ended up, as a consumer would be told to look."""

    files: tuple[ReleaseFile, ...]

    def to_dict(self) -> dict:
        return {
            "tag": self.tag,
            "commit": self.commit,
            "version": self.version,
            "destination": self.destination,
            "published": self.published,
            "files": [item.to_dict() for item in self.files],
        }


def default_release_dir(checkout: Path) -> Path:
    """Where :mod:`orch_hub.release_package` leaves the artifact set."""
    return checkout / "dist" / "release"


def parse_destination(value: str) -> Destination:
    """Read one ``--to`` value into the channel it names.

    A bare path is the filesystem channel because that is the one that needs no
    account, no network, and no permission model — the default a private
    engineering release actually uses. Anything else has to say so with a
    scheme, so a typo in a repository name can never be mistaken for a
    directory to create.
    """
    text = value.strip()
    if not text:
        raise PublishError("no destination given")
    if text.startswith(GITHUB_SCHEME):
        repo = text[len(GITHUB_SCHEME) :].strip()
        if repo.count("/") != 1 or not all(part for part in repo.split("/")):
            raise PublishError(
                f"github destination {value!r} is not owner/name"
            )
        if repo.startswith("-"):
            raise PublishError(f"github destination {value!r} starts with a dash")
        return Destination(scheme="github", location=repo)
    if ":" in text.split("/", 1)[0]:
        raise PublishError(
            f"destination {value!r} names an unsupported scheme; use a path or "
            f"{GITHUB_SCHEME}<owner>/<name>"
        )
    return Destination(
        scheme="directory", location=str(Path(text).expanduser().resolve())
    )


def check_tag(tag: str, version: str) -> None:
    """Refuse a tag that does not name this release, or names a path.

    The tag becomes a directory under the destination and an argument to
    ``gh``, on top of being the identity a consumer quotes. All three want the
    same thing from it, so it is checked once, here, before any of them see it.
    """
    if not tag:
        raise PublishError("no tag given")
    if tag != Path(tag).name or tag.startswith(".") or "\0" in tag:
        raise PublishError(
            f"tag {tag!r} is not usable as a single directory name under the "
            "destination"
        )
    if tag.startswith("-"):
        raise PublishError(f"tag {tag!r} starts with a dash and would read as an option")
    if not tag.startswith(TAG_PREFIX):
        raise PublishError(
            f"tag {tag!r} does not start with {TAG_PREFIX!r}; a release tag is "
            f"{TAG_PREFIX}<version>"
        )
    named = tag[len(TAG_PREFIX) :]
    if named != version:
        raise PublishError(
            f"tag {tag!r} names version {named!r}, but this release is {version!r}"
        )


def check_checkout_version(checkout: Path, version: str) -> None:
    """Refuse an artifact set that is not this checkout's version.

    ``dist/`` survives every build, so what is sitting there is whatever was
    built last — which is the *previous* version's artifact set exactly when
    the release being tagged is a version bump, and the most likely way to
    publish bytes nobody meant to publish.

    This is the strongest binding available on this side. The tag proves the
    checkout is the tag; nothing in a compiled payload records the revision it
    was frozen from, so a rebuild that never happened *at the same version*
    still passes here. Closing that needs the build to record its source
    revision, which is a build-side change and a separate piece of work.
    """
    try:
        declared = read_version(checkout)
    except (BuildError, OSError) as exc:
        raise PublishError(f"cannot read the version {checkout} declares: {exc}") from exc
    if declared != version:
        raise PublishError(
            f"{checkout} declares version {declared!r} but the release is "
            f"{version!r}; these artifacts were not built from the source being "
            "tagged — rebuild and repackage before publishing"
        )


def resolve_tag(
    checkout: Path, tag: str, *, runner: Runner = run_command
) -> Revision:
    """Bind the tag to the checkout that produced the artifacts.

    Three questions, and the third is the one that is easy to skip: does the
    tag exist, is the checkout at it, and is the working tree clean. A tagged
    commit with edits on top is a checkout whose state has no name, and
    publishing from it files unnamed bytes under a name.

    This proves the *checkout* is the tag. It cannot prove the payload was
    frozen from that checkout — nothing in a compiled payload records the
    revision it came from — which is why the artifact set is re-verified
    against its own inventory as well: the two checks bound different failures
    and neither replaces the other.
    """
    commit = _git(
        runner,
        checkout,
        ["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
        allow_failure=True,
    )
    if commit is None or not COMMIT_RE.fullmatch(commit):
        raise PublishError(
            f"{checkout} has no tag {tag!r}; create and push it before publishing"
        )
    head = _git(runner, checkout, ["rev-parse", "--verify", "HEAD^{commit}"])
    if head != commit:
        raise PublishError(
            f"{checkout} is at {head[:12]}, not at tag {tag!r} ({commit[:12]}); "
            "check out the tag before publishing it"
        )
    status = _git(runner, checkout, ["status", "--porcelain"], strip=False)
    if status.strip():
        raise PublishError(
            f"{checkout} has uncommitted changes, so its state is not tag "
            f"{tag!r}; commit or stash them before publishing"
        )
    return Revision(tag=tag, commit=commit)


def _git(
    runner: Runner,
    checkout: Path,
    args: Sequence[str],
    *,
    allow_failure: bool = False,
    strip: bool = True,
) -> str | None:
    try:
        result: CommandResult = runner(
            [GIT_BIN, *args], cwd=checkout, timeout=_GIT_TIMEOUT
        )
    except CommandNotFoundError as exc:
        raise PublishError(f"{GIT_BIN} is not installed") from exc
    except OSError as exc:
        raise PublishError(f"cannot run {GIT_BIN} in {checkout}: {exc}") from exc
    if not result.ok:
        if allow_failure:
            return None
        detail = (result.stderr or result.stdout or "").strip()
        raise PublishError(
            f"git {' '.join(args)} failed in {checkout}"
            + (f": {detail}" if detail else "")
        )
    return result.stdout.strip() if strip else result.stdout


def verify_release(directory: Path) -> VerifiedRelease:
    """Re-derive a packaged release from the files on disk.

    Everything ``release.json`` claims is recomputed rather than trusted, and
    the directory has to hold exactly the files it accounts for: an extra file
    is a stale artifact from an earlier packaging or something a hand copy
    brought along, and it would be published as part of the release without
    appearing in any inventory.

    ``SHA256SUMS`` is checked against the same recomputation, not against
    ``release.json``. Two documents that were written from one set of digests
    agree with each other whatever happened to the files afterwards; agreeing
    with the bytes is the property worth having.
    """
    release_dir = directory.expanduser().resolve()
    if not release_dir.is_dir():
        raise PublishError(f"release directory not found: {release_dir}")

    release = _read_release_document(release_dir / RELEASE_NAME)
    name = _text(release, "name", RELEASE_NAME)
    version = _text(release, "version", RELEASE_NAME)
    _safe_name(version, f"{RELEASE_NAME} version")
    checksums = _text(release, "checksums", RELEASE_NAME)
    if checksums != CHECKSUM_NAME:
        raise PublishError(
            f"{RELEASE_NAME} names its checksum file {checksums!r}, not {CHECKSUM_NAME!r}"
        )

    declared = _declared_artifacts(release, release_dir)
    present = _present_files(release_dir)
    accounted = {RELEASE_NAME, CHECKSUM_NAME, *declared}
    _compare_sets(present, accounted, release_dir)

    files: list[ReleaseFile] = []
    digests: dict[str, str] = {}
    for artifact_name, entry in sorted(declared.items()):
        path = release_dir / artifact_name
        digest = _verify_file(path, entry["size"], entry["sha256"])
        digests[artifact_name] = digest
        files.append(
            ReleaseFile(
                name=artifact_name,
                kind=str(entry["kind"]),
                size=int(entry["size"]),
                sha256=digest,
            )
        )
        if entry["kind"] == INSTALLER_KIND:
            _check_installer_header(path, entry["target"], version, release)

    release_path = release_dir / RELEASE_NAME
    digests[RELEASE_NAME] = _digest(release_path)
    _check_checksums(release_dir / CHECKSUM_NAME, digests)

    files.append(
        ReleaseFile(
            name=RELEASE_NAME,
            kind=RELEASE_KIND,
            size=release_path.stat().st_size,
            sha256=digests[RELEASE_NAME],
        )
    )
    checksum_path = release_dir / CHECKSUM_NAME
    files.append(
        ReleaseFile(
            name=CHECKSUM_NAME,
            kind=CHECKSUM_KIND,
            # Recorded here and nowhere inside the release: the checksum file
            # is the one artifact its own release cannot vouch for.
            size=checksum_path.stat().st_size,
            sha256=_digest(checksum_path),
        )
    )
    return VerifiedRelease(
        directory=release_dir,
        name=name,
        version=version,
        release=release,
        files=tuple(sorted(files, key=lambda item: item.name)),
    )


def _read_release_document(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PublishError(
            f"{path} not found; run python3 -m orch_hub.release_package first"
        ) from exc
    except OSError as exc:
        raise PublishError(f"cannot read {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PublishError(f"malformed {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise PublishError(f"{path} is not a JSON object")
    schema = document.get("schema")
    if schema != RELEASE_SCHEMA:
        raise PublishError(
            f"{path} declares schema {schema!r}; this publisher understands "
            f"{RELEASE_SCHEMA!r}"
        )
    return document


def _declared_artifacts(release: dict, release_dir: Path) -> dict[str, dict]:
    """Flatten the target-keyed inventory into the files it accounts for."""
    targets = release.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise PublishError(f"{release_dir / RELEASE_NAME} declares no targets")
    declared: dict[str, dict] = {}
    for target, entry in sorted(targets.items()):
        if not isinstance(entry, dict):
            raise PublishError(f"target {target!r} is not an object")
        artifacts = entry.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise PublishError(f"target {target!r} declares no artifacts")
        for item in artifacts:
            if not isinstance(item, dict):
                raise PublishError(f"target {target!r} has a non-object artifact")
            name = _safe_name(str(item.get("name", "")), f"target {target!r} artifact")
            if name in declared:
                raise PublishError(
                    f"artifact {name!r} is declared by two targets; one file "
                    "cannot be two targets' release"
                )
            size = item.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise PublishError(f"artifact {name!r} declares a non-numeric size")
            digest = str(item.get("sha256", ""))
            if not SHA256_RE.fullmatch(digest):
                raise PublishError(
                    f"artifact {name!r} declares sha256 {digest!r}, which is not "
                    "a lowercase hex digest"
                )
            declared[name] = {
                "kind": str(item.get("kind", "")),
                "size": size,
                "sha256": digest,
                "target": target,
            }
    return declared


def _present_files(release_dir: Path) -> set[str]:
    """Names of the regular files in the release directory.

    A directory or a symlink is refused rather than skipped: publication copies
    what it finds, and a symlink out of the release is a file nothing here
    verified.
    """
    names: set[str] = set()
    try:
        entries = sorted(release_dir.iterdir())
    except OSError as exc:
        raise PublishError(f"cannot read {release_dir}: {exc}") from exc
    for entry in entries:
        if entry.is_symlink():
            raise PublishError(f"{entry} is a symlink; a release is plain files")
        if not entry.is_file():
            raise PublishError(f"{entry} is not a regular file")
        names.add(entry.name)
    return names


def _compare_sets(present: set[str], accounted: set[str], release_dir: Path) -> None:
    missing = sorted(accounted - present)
    if missing:
        raise PublishError(
            f"{release_dir} is missing {len(missing)} file(s) its inventory "
            f"names: {', '.join(missing)}"
        )
    extra = sorted(present - accounted)
    if extra:
        raise PublishError(
            f"{release_dir} holds {len(extra)} file(s) no inventory accounts "
            f"for: {', '.join(extra)}; repackage rather than publish them"
        )


def _verify_file(path: Path, size: int, sha256: str) -> str:
    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise PublishError(f"cannot read {path}: {exc}") from exc
    if actual_size != size:
        raise PublishError(
            f"{path} is {actual_size} bytes; its inventory says {size}"
        )
    digest = _digest(path)
    if digest != sha256:
        raise PublishError(
            f"{path} hashes to {digest}; its inventory says {sha256}"
        )
    return digest


def _check_checksums(path: Path, digests: dict[str, str]) -> None:
    """Hold ``SHA256SUMS`` to the same recomputation the inventory was held to."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublishError(f"cannot read {path}: {exc}") from exc
    listed: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        match = _CHECKSUM_LINE_RE.fullmatch(line)
        if match is None:
            raise PublishError(f"{path} line {number} is not a sha256 checksum line")
        digest, name = match.group(1), match.group(2)
        if name in listed:
            raise PublishError(f"{path} lists {name!r} twice")
        listed[name] = digest
    missing = sorted(set(digests) - set(listed))
    if missing:
        raise PublishError(
            f"{path} does not cover {', '.join(missing)}, so `shasum -a 256 -c` "
            "would report nothing about them"
        )
    extra = sorted(set(listed) - set(digests))
    if extra:
        raise PublishError(
            f"{path} covers files this release does not have: {', '.join(extra)}"
        )
    for name, digest in sorted(listed.items()):
        if digest != digests[name]:
            raise PublishError(
                f"{path} records {digest} for {name}, which hashes to {digests[name]}"
            )


def _check_installer_header(
    path: Path, target: str, version: str, release: dict
) -> None:
    """Prove the installer agrees with the entry it is filed under.

    The digests prove each file is the file its inventory describes; this
    proves the *set* belongs together. An installer built for one target and
    listed under another passes every checksum, and then refuses every machine
    the release told the consumer it was for.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(_HEADER_BYTES)
    except OSError as exc:
        raise PublishError(f"cannot read {path}: {exc}") from exc
    header = head.decode("utf-8", errors="replace")
    fields: dict[str, str] = {}
    for field in _HEADER_FIELDS:
        match = re.search(rf"^{field}='([^']*)'$", header, re.MULTILINE)
        if match is None:
            raise PublishError(
                f"{path} has no {field} assignment in its first "
                f"{_HEADER_BYTES} bytes; it is not an orch-hub installer"
            )
        fields[field] = match.group(1)
    expected_floor = str(release["targets"][target].get("min_macos") or "")
    for field, expected in (
        ("VERSION", version),
        ("TARGET", target),
        ("MIN_MACOS", expected_floor),
    ):
        if fields[field] != expected:
            raise PublishError(
                f"{path} declares {field}={fields[field]!r}, but the release "
                f"files it under {expected!r}"
            )


def _safe_name(value: str, what: str) -> str:
    """Prove one inventory string is safe as a file name and as an argument.

    It becomes a path under the destination and a positional argument to
    ``gh release create``, which reads a leading dash as an option and splits
    an asset name on ``#`` to take a display label. Neither is a case to
    support; both are a case to refuse before a release is assembled around it.
    """
    if not value:
        raise PublishError(f"{what} has no name")
    if value != Path(value).name or value.startswith(".") or "\0" in value:
        raise PublishError(f"{what} {value!r} is not a plain file name")
    if value.startswith("-") or "#" in value:
        raise PublishError(
            f"{what} {value!r} cannot be passed to a release host as a file name"
        )
    return value


def _text(document: dict, field: str, what: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PublishError(f"{what} declares no {field}")
    return value.strip()


def _digest(path: Path) -> str:
    try:
        return sha256_file(path)
    except PackageError as exc:
        raise PublishError(str(exc)) from exc


def publication_document(release: VerifiedRelease, revision: Revision) -> dict:
    """What is recorded beside the artifacts, and why each field is in it.

    ``commit`` is the provenance the artifacts themselves cannot carry: a
    compiled payload records the interpreter and the freezer that made it, not
    the revision it was made from. ``files`` repeats the inventory with the
    checksum file's own digest included, which closes the one link
    ``SHA256SUMS`` cannot close for itself.

    No timestamp, deliberately, and for the same reason the release archive
    writes a fixed mtime: a publication that is a pure function of its tag and
    its bytes can be produced twice and compared, and a field nobody can verify
    would only make two identical publications look different.
    """
    return {
        "schema": PUBLICATION_SCHEMA,
        "name": release.name,
        "version": release.version,
        "tag": revision.tag,
        "commit": revision.commit,
        "checksums": CHECKSUM_NAME,
        "files": [item.to_dict() for item in release.files],
    }


def release_notes(release: VerifiedRelease, revision: Revision) -> str:
    """Body of a hosted release, built from what was verified.

    Deliberately facts and not instructions: how to install is documented once,
    where it can be corrected, and a copy pasted into an immutable release
    would outlive the wording it was copied from.
    """
    lines = [f"{release.name} {release.version} ({revision.commit[:12]})", ""]
    targets = release.release.get("targets", {})
    for target in sorted(targets):
        entry = targets[target]
        floor = entry.get("min_macos")
        requirement = f"macOS {floor} or newer" if floor else "no declared minimum macOS"
        lines.append(f"- {target}: {requirement}")
    lines += ["", "Verify with `shasum -a 256 -c SHA256SUMS`:", "", "```"]
    lines += [f"{item.sha256}  {item.name}" for item in release.files]
    lines += ["```", ""]
    return "\n".join(lines)


def publish(options: PublishOptions, *, runner: Runner = run_command) -> PublishResult:
    """Verify, bind to the tag, stage, and place — in that order.

    Staging first and placing last is what makes the two destinations one
    operation with two endings: the same directory of files is assembled and
    checked once, and only then does anything leave the machine or become
    visible at a path a consumer reads.
    """
    _check_layout(options)
    release = verify_release(options.release_dir)
    check_tag(options.tag, release.version)
    check_checkout_version(options.checkout, release.version)
    revision = resolve_tag(options.checkout, options.tag, runner=runner)

    stage = _stage(options, release, revision)
    try:
        if options.destination.scheme == "github":
            published = _publish_to_github(options, release, revision, stage, runner)
        else:
            published = _publish_to_directory(options, revision, stage)
    finally:
        discard_path(stage, best_effort=True)
    return PublishResult(
        tag=revision.tag,
        commit=revision.commit,
        version=release.version,
        destination=options.destination.describe(),
        published=published,
        files=release.files,
    )


def _check_layout(options: PublishOptions) -> None:
    """Keep the three directories out of each other.

    Staging or publishing inside the release directory would add files to the
    set that was just verified — and for the filesystem destination, publishing
    into it would then copy the copy.
    """
    release_dir = options.release_dir.expanduser().resolve()
    work_dir = options.work_dir.expanduser().resolve()
    _refuse_nested(release_dir, work_dir, "the scratch directory is inside the release")
    if options.destination.scheme == "directory":
        target = Path(options.destination.location)
        _refuse_nested(
            release_dir, target, "the destination is inside the release directory"
        )
        _refuse_nested(
            target, release_dir, "the release directory is inside the destination"
        )
        _refuse_nested(
            target, work_dir, "the scratch directory is inside the destination"
        )


def _refuse_nested(outer: Path, inner: Path, message: str) -> None:
    if inner == outer or outer in inner.parents:
        raise PublishError(f"{message}: {inner} under {outer}")


def _stage(
    options: PublishOptions, release: VerifiedRelease, revision: Revision
) -> Path:
    """Assemble exactly what gets published, on the destination's side of the
    verification."""
    stage = options.work_dir / f"publish-{revision.tag}"
    discard_path(stage)
    try:
        stage.mkdir(parents=True)
        for item in release.files:
            shutil.copy2(release.directory / item.name, stage / item.name)
        (stage / PUBLICATION_NAME).write_text(
            json.dumps(publication_document(release, revision), indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        discard_path(stage, best_effort=True)
        raise PublishError(f"cannot stage the publication at {stage}: {exc}") from exc
    return stage


def _publish_to_directory(
    options: PublishOptions, revision: Revision, stage: Path
) -> str:
    target = Path(options.destination.location) / revision.tag
    if target.exists() or target.is_symlink():
        raise PublishError(
            f"{target} already exists; a published tag is not republished. "
            "Remove it deliberately, or publish a new version."
        )
    try:
        place_directory(
            stage,
            target,
            transfer=lambda source, destination: shutil.move(
                str(source), str(destination)
            ),
            what=f"release {revision.tag}",
        )
    except PlacementError as exc:
        raise PublishError(str(exc)) from exc
    return str(target)


def _publish_to_github(
    options: PublishOptions,
    release: VerifiedRelease,
    revision: Revision,
    stage: Path,
    runner: Runner,
) -> str:
    """Create the release at the tag the remote already has.

    ``--verify-tag`` is not optional here. Without it ``gh`` creates a missing
    tag from the default branch's head, which would publish artifacts under a
    tag pointing at a commit that is not the one they were verified against —
    the exact binding this whole step exists to make.
    """
    repo = options.destination.location
    files = [str(stage / item.name) for item in release.files]
    files.append(str(stage / PUBLICATION_NAME))
    args = [
        GH_BIN,
        "release",
        "create",
        revision.tag,
        "--repo",
        repo,
        "--verify-tag",
        "--title",
        f"{release.name} {release.version}",
        "--notes",
        release_notes(release, revision),
        *files,
    ]
    try:
        result: CommandResult = runner(args, timeout=_GH_TIMEOUT)
    except CommandNotFoundError as exc:
        raise PublishError(
            f"{GH_BIN} is not installed; it is how a GitHub release is created"
        ) from exc
    except OSError as exc:
        raise PublishError(f"cannot run {GH_BIN}: {exc}") from exc
    if not result.ok:
        detail = (result.stderr or result.stdout or "").strip()
        raise PublishError(
            f"gh release create failed for {repo} {revision.tag}"
            + (f": {detail}" if detail else "")
        )
    url = result.stdout.strip().splitlines()
    return url[-1] if url else f"https://github.com/{repo}/releases/tag/{revision.tag}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m orch_hub.release_publish",
        description="Publish a packaged orch-hub release under its version tag.",
    )
    checkout_default = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--checkout",
        type=Path,
        default=checkout_default,
        help="source checkout holding the tag (default: the one holding this module)",
    )
    parser.add_argument(
        "--release",
        type=Path,
        default=None,
        help="packaged release directory (default: <checkout>/dist/release)",
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=None,
        help="scratch directory (default: <checkout>/build/release)",
    )
    parser.add_argument(
        "--tag",
        required=True,
        help=f"version tag to publish, as {TAG_PREFIX}<version>",
    )
    parser.add_argument(
        "--to",
        required=True,
        help=(
            "destination: a directory for private storage, or "
            f"{GITHUB_SCHEME}<owner>/<name> for a GitHub release"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    args = parser.parse_args(argv)

    checkout = args.checkout.expanduser().resolve()
    try:
        options = PublishOptions(
            checkout=checkout,
            release_dir=(args.release or default_release_dir(checkout))
            .expanduser()
            .resolve(),
            work_dir=(args.work or checkout / "build" / "release")
            .expanduser()
            .resolve(),
            tag=args.tag,
            destination=parse_destination(args.to),
        )
        result = publish(options)
    except (PublishError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        _print_human(result)
    return 0


def _print_human(result: PublishResult) -> None:
    print(f"tag:        {result.tag}")
    print(f"commit:     {result.commit}")
    print(f"version:    {result.version}")
    print(f"published:  {result.published}")
    for item in result.files:
        print(f"  {item.name}  ({item.kind})")
    # Published alongside them and in neither inventory, because it is the
    # record of this publication rather than part of the release.
    print(f"  {PUBLICATION_NAME}  (publication record)")



if __name__ == "__main__":
    sys.exit(main())
