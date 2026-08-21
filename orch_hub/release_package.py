"""Turn a built runtime payload into the files a release actually ships.

Run from a checkout, after :mod:`orch_hub.release_build`::

    python3 -m orch_hub.release_package

The build produces a *payload directory*; nobody can be handed a directory.
This module produces the three files that can be:

- ``orch-hub-<version>-<target>.run`` — a POSIX-shell self-extractor with the
  payload appended to it. One file, one command, no Python on the far side.
- ``release.json`` — target-keyed metadata: what each target is, the minimum
  macOS it needs, the runtime manifest it was built from, and the artifacts
  that carry it, each with its size and SHA-256.
- ``SHA256SUMS`` — the same digests in ``shasum -a 256 -c`` form, covering
  every other file in the release, so verification needs no JSON tooling.

Three properties are worth more than the mechanics:

- **The artifact set is a pure function of the payload.** The archive is built
  with fixed member order, timestamps, ownership, and normalised modes, and
  nothing anywhere records a build time. Packaging the same payload twice
  produces byte-identical files, so "immutable release" is checkable rather
  than promised.
- **The installer refuses before it unpacks.** Target, minimum macOS, and
  payload integrity are baked into the shell header and verified before a byte
  is extracted — a compiled binary for the wrong machine cannot be run to
  report that it is for the wrong machine.
- **Packaging re-audits the payload.** The build audits what it froze; this
  audits what is about to become a downloadable file. They are the same check
  at two different moments, and only the second one covers a payload that was
  built once and packaged later.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .atomic_dir import PlacementError, place_directory
from .release_audit import AuditReport, ForbiddenLiteral, audit_payload
from .release_build import (
    EXECUTABLE_NAME,
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    REQUIRED_MEMBERS,
    TARGET,
    default_literals,
)

RELEASE_SCHEMA = "orch-hub-release/1"
"""Schema of ``release.json``.

Targets are a keyed map from the start, so shipping a second os/arch is one
more key rather than a different document."""

RELEASE_NAME = "release.json"
CHECKSUM_NAME = "SHA256SUMS"
INSTALLER_SUFFIX = ".run"

TEMPLATE_RELATIVE_PATH = Path("deploy") / "release" / "installer.sh"
"""Checked-in self-extractor template — the structure's source of truth.

Kept beside the launchd plist template for the same reason: shell that decides
whether a machine may install a release is reviewable text in the repository,
not a string assembled at runtime."""

EMBEDDED_FIELDS = ("name", "version", "target", "os", "arch", "executable", "min_macos")
"""Manifest fields the installer header carries.

``min_macos`` is the only optional one: a payload with no declared floor is one
the installer does not compare against, rather than one it refuses."""

INSTALLER_KIND = "installer"
RELEASE_KIND = "release-metadata"
CHECKSUM_KIND = "checksums"

ARCHIVE_LINE_TOKEN = "REPLACE-ORCH-HUB-ARCHIVE-LINE"
ARCHIVE_SHA256_TOKEN = "REPLACE-ORCH-HUB-ARCHIVE-SHA256"

PLACEHOLDER_RE = re.compile(r"REPLACE-[A-Z0-9-]+")
"""Shape of a template placeholder, spelled as the launchd plist template
spells it. Unlike that one, an unsubstituted leftover here is never a
compatibility case: the template and the code that renders it ship in the same
checkout, so a leftover is a bug and fails the packaging."""

_CHUNK_SIZE = 1 << 20

_ARCHIVE_MTIME = 0
"""Timestamp written for every archive member.

A real mtime would make two packagings of one payload differ, which is exactly
what an immutable artifact must not do. Nothing reads these back: the installer
extracts into a scratch directory and the runtime never consults them."""


class PackageError(RuntimeError):
    """Raised when a release artifact set cannot be produced."""


@dataclass(frozen=True)
class Artifact:
    """One produced file, described the way a consumer verifies it."""

    name: str
    kind: str
    size: int
    sha256: str | None
    """``None`` only for the checksum file, which cannot contain its own digest."""

    def to_dict(self) -> dict:
        data: dict = {"name": self.name, "kind": self.kind, "size": self.size}
        if self.sha256 is not None:
            data["sha256"] = self.sha256
        return data


@dataclass(frozen=True)
class PackageOptions:
    """Inputs of one packaging run."""

    payload_dirs: tuple[Path, ...]
    out_dir: Path
    work_dir: Path
    template_path: Path
    literals: tuple[ForbiddenLiteral, ...] = ()
    allow_findings: bool = False


@dataclass(frozen=True)
class PackageResult:
    out_dir: Path
    version: str
    release: dict
    artifacts: tuple[Artifact, ...]
    audits: tuple[AuditReport, ...]

    def to_dict(self) -> dict:
        return {
            "out": str(self.out_dir),
            "version": self.version,
            "release": self.release,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


def default_payload_dir(checkout: Path) -> Path:
    """Where :mod:`orch_hub.release_build` leaves the payload for this target."""
    return checkout / "dist" / f"{EXECUTABLE_NAME}-{TARGET}"


def value_of(manifest: dict, field: str) -> str:
    """One manifest field as the string that gets embedded.

    Safe to call unvalidated only after :func:`read_payload_manifest`, which is
    where a value that could break out of the header is rejected.
    """
    raw = manifest.get(field)
    return "" if raw is None else str(raw).strip()


def read_payload_manifest(payload_dir: Path) -> dict:
    """Read a payload's ``runtime.json`` and prove it describes a shippable one.

    The manifest is the only honest source for what the payload *is*, and every
    value packaging embeds in the installer header comes from it. It is checked
    here rather than trusted because those values end up inside single-quoted
    shell in a file other people run.
    """
    path = payload_dir / MANIFEST_NAME
    if not path.is_file():
        raise PackageError(
            f"no {MANIFEST_NAME} in {payload_dir}: not an orch-hub payload"
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"cannot read {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PackageError(f"{path} is not a JSON object")
    schema = str(manifest.get("schema") or "").strip()
    if schema != MANIFEST_SCHEMA:
        raise PackageError(
            f"{path} declares schema {schema or 'none'!r}, "
            f"but this packager reads {MANIFEST_SCHEMA!r}"
        )
    for field in EMBEDDED_FIELDS:
        _check_embeddable(manifest, field, path, required=field != "min_macos")
    # Product identity is enforced; only the *target* is read. The installer
    # that places this payload enforces the same two names, so a payload
    # declaring anything else would produce artifacts it then refuses.
    for field in ("name", "executable"):
        declared = value_of(manifest, field)
        if declared != EXECUTABLE_NAME:
            raise PackageError(
                f"{path} declares {field} {declared!r}, "
                f"but a release payload is named {EXECUTABLE_NAME!r}"
            )
    if not (payload_dir / EXECUTABLE_NAME).is_file():
        raise PackageError(
            f"payload has no executable at {payload_dir / EXECUTABLE_NAME}"
        )
    return manifest


def _check_embeddable(manifest: dict, field: str, path: Path, *, required: bool) -> None:
    """Validate one manifest field that is embedded in the installer header.

    Every one of them lands inside a single-quoted shell assignment, so a value
    carrying a quote or a newline would not merely render badly — it would
    change what the released installer *does*. This is the only place that can
    be caught: nothing downstream re-reads the rendered header as shell.
    """
    value = value_of(manifest, field)
    if not value:
        if required:
            raise PackageError(f"{path} declares no {field}")
        return
    if "'" in value or "\n" in value or "\r" in value:
        raise PackageError(
            f"{path} declares {field}={value!r}, which cannot be embedded in the "
            "installer script"
        )


def package_release(options: PackageOptions) -> PackageResult:
    """Produce one release's artifact set from one or more built payloads.

    Everything is written into a staging directory and only then published, so
    a failure part-way leaves the previous release directory intact rather than
    a half-written one at the path a publication step reads.
    """
    if not options.payload_dirs:
        raise PackageError("no payload directory given")
    _check_output_layout(options)
    template = _load_template(options.template_path)
    payloads = [_describe(directory, options) for directory in options.payload_dirs]
    versions = {payload.version for payload in payloads}
    if len(versions) > 1:
        raise PackageError(
            "payloads declare different versions "
            f"({', '.join(sorted(versions))}); one release is one version"
        )
    duplicate = _first_duplicate([payload.target for payload in payloads])
    if duplicate is not None:
        raise PackageError(f"two payloads declare the same target {duplicate!r}")

    version = versions.pop()
    stage = _fresh_dir(options.work_dir / "package-stage")
    artifacts: list[Artifact] = []
    entries: dict[str, dict] = {}
    for payload in payloads:
        artifact = _write_installer(payload, template, stage, options.work_dir)
        artifacts.append(artifact)
        entries[payload.target] = {
            "os": value_of(payload.manifest, "os"),
            "arch": value_of(payload.manifest, "arch"),
            "min_macos": value_of(payload.manifest, "min_macos") or None,
            # Verbatim, not a restatement: a consumer comparing an installed
            # runtime.json against the release must find the same document.
            "runtime": payload.manifest,
            "artifacts": [artifact.to_dict()],
        }

    release = {
        "schema": RELEASE_SCHEMA,
        "name": EXECUTABLE_NAME,
        "version": version,
        "checksums": CHECKSUM_NAME,
        "targets": entries,
    }
    artifacts.append(_write_release(stage, release))
    artifacts.append(_write_checksums(stage, artifacts))
    _place_release(stage, options.out_dir)
    return PackageResult(
        out_dir=options.out_dir,
        version=version,
        release=release,
        artifacts=tuple(artifacts),
        audits=tuple(payload.audit for payload in payloads),
    )


@dataclass(frozen=True)
class _Payload:
    directory: Path
    manifest: dict
    """The payload's ``runtime.json`` exactly as it was written."""

    audit: AuditReport

    @property
    def version(self) -> str:
        return value_of(self.manifest, "version")

    @property
    def target(self) -> str:
        return value_of(self.manifest, "target")

    @property
    def archive_root(self) -> str:
        """Directory name the archive unpacks to.

        Derived from the manifest, not from the source directory's name: a
        payload someone copied to ``payload-copy/`` must still produce the same
        archive, or reproducibility would depend on where the build was left.
        """
        return f"{EXECUTABLE_NAME}-{self.target}"

    @property
    def installer_name(self) -> str:
        return f"{EXECUTABLE_NAME}-{self.version}-{self.target}{INSTALLER_SUFFIX}"


def _describe(directory: Path, options: PackageOptions) -> _Payload:
    payload_dir = directory.expanduser().resolve()
    if not payload_dir.is_dir():
        raise PackageError(f"payload directory not found: {payload_dir}")
    manifest = read_payload_manifest(payload_dir)
    report = audit_payload(
        payload_dir, required=REQUIRED_MEMBERS, literals=options.literals
    )
    if report.findings and not options.allow_findings:
        raise PackageError(_findings_message(payload_dir, report))
    return _Payload(directory=payload_dir, manifest=manifest, audit=report)


def _check_output_layout(options: PackageOptions) -> None:
    """Refuse an output directory that swallows its own inputs.

    Publishing *replaces* the release directory: the tree already there is
    moved aside and then removed. Aimed one level too high — ``--out dist``
    rather than the default ``dist/release`` — that deletes the payload the
    build just produced, and nothing about the command says so beforehand.
    """
    out = options.out_dir.expanduser().resolve()
    for payload in options.payload_dirs:
        if payload.expanduser().resolve().is_relative_to(out):
            raise PackageError(
                f"the release directory {out} contains the payload {payload}; "
                "publishing the release would replace the payload with it"
            )
    if options.work_dir.expanduser().resolve().is_relative_to(out):
        raise PackageError(
            f"the release directory {out} contains the scratch directory "
            f"{options.work_dir}; the release would replace its own staging area"
        )


def _first_duplicate(values: Sequence[str]) -> str | None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


def _load_template(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PackageError(f"cannot read the installer template {path}: {exc}") from exc


def build_archive(payload_dir: Path, root_name: str, destination: Path) -> None:
    """Write a deterministic gzipped tar of ``payload_dir`` under ``root_name``.

    Every source of variation is pinned: members are emitted in sorted path
    order, timestamps are fixed, ownership is dropped, and modes collapse to
    executable-or-not. The gzip header carries neither a name nor a time.
    Symlinks are stored as symlinks — the payload's bundled framework is a tree
    of relative links, and resolving them would change the runtime's shape.
    """
    try:
        with destination.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw,
                mtime=_ARCHIVE_MTIME,
                compresslevel=9,
            ) as compressed:
                # GNU format: member paths reach ~90 characters today, close
                # enough to ustar's 100-byte name field that a slightly deeper
                # bundle would start depending on the prefix split.
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT
                ) as archive:
                    for relative in _walk(payload_dir):
                        source = payload_dir / relative
                        info = archive.gettarinfo(
                            str(source), arcname=f"{root_name}/{relative.as_posix()}"
                        )
                        content = _normalise(info, source)
                        try:
                            archive.addfile(info, content)
                        finally:
                            if content is not None:
                                content.close()
    except OSError as exc:
        raise PackageError(f"cannot build the payload archive: {exc}") from exc


def _normalise(info: tarfile.TarInfo, source: Path) -> io.BufferedReader | None:
    """Strip a member down to what a release needs, and open its content.

    Returns the reader the member's bytes come from, or ``None`` for members
    that have none (directories and symlinks).
    """
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = _ARCHIVE_MTIME
    if info.issym() or info.islnk():
        info.mode = 0o777
        return None
    if info.isdir():
        info.mode = 0o755
        return None
    # Only the executable bit is meaningful in a payload, and it is the one bit
    # that must survive: the runtime is unrunnable without it.
    info.mode = 0o755 if info.mode & 0o111 else 0o644
    return source.open("rb")


def _walk(root: Path) -> Iterator[Path]:
    """Relative paths under ``root`` in sorted order, never following symlinks."""
    entries: list[Path] = []

    def descend(directory: Path) -> None:
        with os.scandir(directory) as scan:
            children = sorted(scan, key=lambda entry: entry.name)
        for entry in children:
            path = Path(entry.path)
            entries.append(path.relative_to(root))
            if entry.is_dir(follow_symlinks=False):
                descend(path)

    descend(root)
    return iter(sorted(entries, key=lambda path: path.as_posix()))


def render_installer(template: str, values: dict[str, str], archive_sha256: str) -> str:
    """Substitute the template and resolve the archive's starting line.

    Substitution is one regex pass that looks each matched placeholder up,
    never a sequence of ``str.replace`` calls: ``REPLACE-ORCH-HUB-ARCH`` is a
    prefix of ``REPLACE-ORCH-HUB-ARCHIVE-SHA256``, so replacing token by token
    rewrites the inside of a longer token and the outcome depends on dictionary
    order. One pass cannot overlap, and it makes the template and the value set
    check each other: a placeholder with no value and a value with no
    placeholder both fail here rather than in a released file.

    The archive's line number is the exception, resolved in a second pass: it
    can only be counted once every other value is in place, and substituting it
    must not move it. The result is checked to still have the same number of
    lines — a header that grew or shrank at that step would point ``tail`` at
    the wrong offset and produce an installer that fails its own checksum.
    """
    resolved_values = {**values, ARCHIVE_SHA256_TOKEN: archive_sha256}
    found: set[str] = set()

    def substitute(match: re.Match[str]) -> str:
        token = match.group()
        found.add(token)
        if token == ARCHIVE_LINE_TOKEN:
            return token
        return resolved_values.get(token, token)

    text = PLACEHOLDER_RE.sub(substitute, template)
    unknown = sorted(found - set(resolved_values) - {ARCHIVE_LINE_TOKEN})
    if unknown:
        raise PackageError(
            "installer template has placeholders this packager cannot fill: "
            + ", ".join(unknown)
        )
    missing = sorted((set(resolved_values) | {ARCHIVE_LINE_TOKEN}) - found)
    if missing:
        raise PackageError(
            "installer template is missing required placeholders: " + ", ".join(missing)
        )
    if not text.endswith("\n"):
        text += "\n"
    header_lines = text.count("\n")
    resolved = text.replace(ARCHIVE_LINE_TOKEN, str(header_lines + 1))
    if resolved.count("\n") != header_lines:
        raise PackageError("substituting the archive offset changed the header length")
    leftovers = sorted(set(PLACEHOLDER_RE.findall(resolved)))
    if leftovers:
        raise PackageError(
            "installer template has unsubstituted placeholders: " + ", ".join(leftovers)
        )
    return resolved


def _write_installer(
    payload: _Payload, template: str, stage: Path, work_dir: Path
) -> Artifact:
    """Render the header for one payload and append that payload's archive."""
    archive_path = work_dir / f"{payload.installer_name}.tar.gz"
    _discard(archive_path)
    build_archive(payload.directory, payload.archive_root, archive_path)
    manifest = payload.manifest
    header = render_installer(
        template,
        {
            "REPLACE-ORCH-HUB-VERSION": payload.version,
            "REPLACE-ORCH-HUB-TARGET": payload.target,
            "REPLACE-ORCH-HUB-OS": value_of(manifest, "os"),
            "REPLACE-ORCH-HUB-ARCH": value_of(manifest, "arch"),
            "REPLACE-ORCH-HUB-MIN-MACOS": value_of(manifest, "min_macos"),
            "REPLACE-ORCH-HUB-PAYLOAD-DIR": payload.archive_root,
            "REPLACE-ORCH-HUB-COMMAND": EXECUTABLE_NAME,
        },
        sha256_file(archive_path),
    )
    installer = stage / payload.installer_name
    try:
        with installer.open("wb") as out:
            out.write(header.encode("utf-8"))
            with archive_path.open("rb") as archive:
                shutil.copyfileobj(archive, out, _CHUNK_SIZE)
        installer.chmod(0o755)
    except OSError as exc:
        raise PackageError(f"cannot write {installer}: {exc}") from exc
    _discard(archive_path)
    return Artifact(
        name=installer.name,
        kind=INSTALLER_KIND,
        size=installer.stat().st_size,
        sha256=sha256_file(installer),
    )


def _write_release(stage: Path, release: dict) -> Artifact:
    path = stage / RELEASE_NAME
    _write(path, json.dumps(release, indent=2, sort_keys=True) + "\n")
    return Artifact(
        name=path.name,
        kind=RELEASE_KIND,
        size=path.stat().st_size,
        sha256=sha256_file(path),
    )


def _write_checksums(stage: Path, artifacts: Sequence[Artifact]) -> Artifact:
    """Write the ``shasum -a 256 -c`` file covering every other artifact."""
    lines = [
        f"{artifact.sha256}  {artifact.name}\n"
        for artifact in sorted(artifacts, key=lambda item: item.name)
        if artifact.sha256 is not None
    ]
    path = stage / CHECKSUM_NAME
    _write(path, "".join(lines))
    return Artifact(
        name=path.name, kind=CHECKSUM_KIND, size=path.stat().st_size, sha256=None
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PackageError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def _place_release(stage: Path, out_dir: Path) -> Path:
    try:
        return place_directory(
            stage,
            out_dir,
            transfer=lambda source, destination: shutil.move(
                str(source), str(destination)
            ),
            what="release",
        )
    except PlacementError as exc:
        raise PackageError(str(exc)) from exc


def _fresh_dir(path: Path) -> Path:
    _discard(path)
    try:
        path.mkdir(parents=True)
    except OSError as exc:
        raise PackageError(f"cannot create {path}: {exc}") from exc
    return path


def _discard(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.is_symlink() or path.exists():
            path.unlink()
    except OSError as exc:
        raise PackageError(f"cannot clear {path}: {exc}") from exc


def _write(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise PackageError(f"cannot write {path}: {exc}") from exc


def _findings_message(payload_dir: Path, report: AuditReport) -> str:
    lines = [f"payload {payload_dir} has {len(report.findings)} audit problem(s):"]
    lines += [
        f"  {finding.kind}: {finding.path} ({finding.detail})"
        for finding in report.findings
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m orch_hub.release_package",
        description="Package built orch-hub payloads into installable release artifacts.",
    )
    checkout_default = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--checkout",
        type=Path,
        default=checkout_default,
        help="source checkout to package from (default: the one holding this module)",
    )
    parser.add_argument(
        "--payload",
        type=Path,
        action="append",
        default=None,
        dest="payloads",
        help=(
            "built payload directory; repeat for a multi-target release "
            f"(default: <checkout>/dist/{EXECUTABLE_NAME}-{TARGET})"
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="directory receiving the release artifacts (default: <checkout>/dist/release)",
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=None,
        help="scratch directory (default: <checkout>/build/release)",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help=f"installer template (default: <checkout>/{TEMPLATE_RELATIVE_PATH.as_posix()})",
    )
    parser.add_argument(
        "--allow-findings",
        action="store_true",
        help="report payload audit findings without failing the packaging",
    )
    parser.add_argument("--json", action="store_true", help="emit the result as JSON")
    args = parser.parse_args(argv)

    checkout = args.checkout.expanduser().resolve()
    payloads = args.payloads or [default_payload_dir(checkout)]
    work_dir = (args.work or checkout / "build" / "release").expanduser().resolve()
    options = PackageOptions(
        payload_dirs=tuple(path.expanduser().resolve() for path in payloads),
        out_dir=(args.out or checkout / "dist" / "release").expanduser().resolve(),
        work_dir=work_dir,
        template_path=(args.template or checkout / TEMPLATE_RELATIVE_PATH)
        .expanduser()
        .resolve(),
        literals=default_literals(checkout, dict(os.environ)),
        allow_findings=args.allow_findings,
    )
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        result = package_release(options)
    except (PackageError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        _print_human(result)
    return 0


def _print_human(result: PackageResult) -> None:
    print(f"release:    {result.out_dir}")
    print(f"version:    {result.version}")
    print(f"targets:    {', '.join(sorted(result.release['targets'])) or 'none'}")
    for artifact in result.artifacts:
        print(f"  {artifact.name}  ({artifact.kind}, {_size(artifact.size)})")
    findings = sum(len(report.findings) for report in result.audits)
    scanned = sum(report.files_scanned for report in result.audits)
    if findings:
        print(f"audit:      {findings} finding(s) over {scanned} files")
        return
    print(f"audit:      clean ({scanned} files)")


def _size(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f} MB"
    return f"{count} B"


if __name__ == "__main__":
    sys.exit(main())
