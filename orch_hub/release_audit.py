"""Audit of a built release payload before it is packaged for distribution.

A compiled release is handed to engineers who have no access to this
repository, so the payload has to be checked for what it must *not* contain:
first-party Python source, repository/task/protocol state, and any literal that
belongs to the machine that built it — an absolute home/checkout path, a bearer
token, a Bark endpoint. The freezer's own output is trustworthy in the normal
case; this module is what makes that a checked property instead of an
assumption, because the failure mode (a maintainer's token shipped inside a
release) is silent and unrecoverable once published.

The audit is build-time tooling. It is deliberately not reachable from the CLI
and not part of a release payload: nothing in the served surface needs it, and
a release should not carry the list of things a release must not carry.

Findings never quote what they matched. A finding says *which file* contains a
forbidden literal and under which label; the value itself stays out of the
report, the build log, and any JSON a release pipeline may keep.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

PACKAGE_NAME = "orch_hub"
"""First-party package whose compiled source must not appear as files."""

SOURCE_SUFFIXES = frozenset({".py", ".pyc", ".pyo"})
"""Python source/bytecode suffixes that count as shippable source."""

FIRST_PARTY_DIRS = frozenset({PACKAGE_NAME, "tests"})
"""Path components that make a Python file first-party.

A third-party package may legitimately carry a ``.py`` data file deep inside
its own tree, so the rule keys on *where* the file sits rather than on the
suffix alone. A vendored third-party ``tests`` directory is reported too — a
release has no use for one either way.
"""

REPOSITORY_STATE_NAMES = frozenset(
    {
        ".git",
        ".ai",
        ".ai-tasks",
        ".ai-protocol",
        ".mandrel",
        ".claude",
        ".codex",
        ".cursor",
        ".venv",
        ".pytest_cache",
        "CLAUDE.md",
        ".ai-deploy-manifest.json",
        ".ai-deploy-lock.json",
    }
)
"""Names that identify this repository's own working state.

Every entry is unambiguous — a third-party payload has no reason to carry a
``.ai-tasks`` directory or a ``CLAUDE.md`` — so matching by name anywhere in
the tree cannot fire on a dependency. Generic names (``tests``,
``pyproject.toml``) are handled by the first-party source rule instead, where
the location qualifies the match.
"""

SECRET_FILE_SUFFIXES = frozenset({".pem", ".p12", ".key", ".keychain"})
SECRET_FILE_NAMES = frozenset({".env", ".netrc", ".npmrc", "id_rsa", "id_ed25519"})
"""Filenames and suffixes that carry credential material by convention."""

FIRST_PARTY_SOURCE = "first_party_source"
REPOSITORY_STATE = "repository_state"
MAINTAINER_PATH = "maintainer_path"
SECRET_MATERIAL = "secret_material"
MISSING_REQUIRED = "missing_required"
UNREADABLE = "unreadable"
"""Finding kinds. ``UNREADABLE`` reports a payload member the audit could not
open — an unscanned file is an unproven one, so it is a finding rather than a
silent skip."""

ARCHIVE_SUFFIXES = frozenset({".zip", ".pyz", ".whl", ".egg", ".jar"})
"""Archives whose members are audited as if they were payload files.

A freezer stores pure-Python modules inside archives, so a payload that looks
clean from the outside can still carry first-party source one level down.
"""

_CHUNK_SIZE = 1 << 20
"""Read size for the literal scan; files are streamed, not loaded whole."""


@dataclass(frozen=True)
class ForbiddenLiteral:
    """A byte string that must not appear anywhere in a release payload.

    ``label`` is what findings report — ``value`` is never echoed, so a token
    or a Bark URL can be audited for without being written to a log.
    """

    label: str
    value: str
    kind: str = SECRET_MATERIAL

    def encoded(self) -> bytes:
        return self.value.encode("utf-8", "surrogateescape")


@dataclass(frozen=True)
class AuditFinding:
    """One reason a payload is not distributable."""

    kind: str
    path: str
    """Payload-relative path; ``archive.zip!member`` for an archive member."""

    detail: str
    """Why it was flagged. Never contains matched bytes or literal values."""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "detail": self.detail}


@dataclass(frozen=True)
class AuditReport:
    """Result of auditing one payload directory."""

    root: Path
    findings: tuple[AuditFinding, ...] = ()
    files_scanned: int = 0
    archives_scanned: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "files_scanned": self.files_scanned,
            "archives_scanned": self.archives_scanned,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass
class _Collector:
    """Mutable accumulator used while walking a payload."""

    findings: list[AuditFinding] = field(default_factory=list)
    files_scanned: int = 0
    archives_scanned: int = 0

    def add(self, kind: str, path: str, detail: str) -> None:
        self.findings.append(AuditFinding(kind=kind, path=path, detail=detail))


def maintainer_path_literals(paths: Iterable[Path | str]) -> tuple[ForbiddenLiteral, ...]:
    """Build path literals for the machine-specific locations of a build.

    The build driver passes its checkout and the building user's home. Nested
    paths are kept separately rather than deduplicated to their common prefix:
    a finding that names the checkout is a different problem from one that
    names an unrelated part of the home directory, and the labels say which.
    """
    literals: list[ForbiddenLiteral] = []
    seen: set[str] = set()
    for entry in paths:
        text = str(entry).rstrip("/")
        if not text or text == "/" or text in seen:
            continue
        seen.add(text)
        literals.append(
            ForbiddenLiteral(label=f"build path {text}", value=text, kind=MAINTAINER_PATH)
        )
    return tuple(literals)


def audit_payload(
    root: Path,
    *,
    required: Sequence[Path | str] = (),
    literals: Sequence[ForbiddenLiteral] = (),
    package_name: str = PACKAGE_NAME,
) -> AuditReport:
    """Audit the built payload rooted at ``root``.

    ``required`` names payload-relative members that must exist — the audit
    proves absence of the forbidden, so completeness is checked here too rather
    than being assumed from a successful build.

    ``literals`` are byte strings that must not appear anywhere, including
    inside archive members. Path literals come from
    :func:`maintainer_path_literals`; secrets are passed by the caller with a
    label instead of a value.
    """
    collector = _Collector()
    if not root.is_dir():
        collector.add(MISSING_REQUIRED, str(root), "payload root is not a directory")
        return _report(root, collector)

    for relative in required:
        if not (root / relative).exists():
            collector.add(
                MISSING_REQUIRED, str(PurePosixPath(relative)), "required member is absent"
            )

    for path in sorted(_walk(root)):
        relative = path.relative_to(root)
        posix = relative.as_posix()
        _check_names(collector, relative, posix, package_name)
        if path.is_file():
            collector.files_scanned += 1
            _scan_file(collector, path, posix, literals, package_name)

    findings = tuple(sorted(collector.findings, key=lambda f: (f.kind, f.path, f.detail)))
    return _report(root, collector, findings)


def _report(
    root: Path, collector: _Collector, findings: tuple[AuditFinding, ...] | None = None
) -> AuditReport:
    return AuditReport(
        root=root,
        findings=tuple(collector.findings) if findings is None else findings,
        files_scanned=collector.files_scanned,
        archives_scanned=collector.archives_scanned,
    )


def _walk(root: Path) -> Iterator[Path]:
    """Yield every entry under ``root``, without following symlinks out of it.

    Symlinked directories are yielded but not descended into: a freezer creates
    them inside framework bundles, and following them would both duplicate the
    scan and let a link out of the payload widen the audit's own reach.
    """
    for entry in root.iterdir():
        yield entry
        if entry.is_dir() and not entry.is_symlink():
            yield from _walk(entry)


def _check_names(
    collector: _Collector, relative: Path, posix: str, package_name: str
) -> None:
    parts = relative.parts
    for part in parts:
        if part in REPOSITORY_STATE_NAMES:
            collector.add(
                REPOSITORY_STATE, posix, f"repository state member: {part}"
            )
            break
    if _is_first_party_source(relative, package_name, root_scripts=True):
        collector.add(FIRST_PARTY_SOURCE, posix, "first-party python source in payload")
    if relative.name in SECRET_FILE_NAMES or relative.suffix.lower() in SECRET_FILE_SUFFIXES:
        collector.add(SECRET_MATERIAL, posix, f"credential-shaped file: {relative.name}")


def _is_first_party_source(
    relative: PurePosixPath | Path, package_name: str, *, root_scripts: bool
) -> bool:
    """Whether a payload member is compiled or plain first-party source.

    Two spellings have to be recognised, because the same module is named
    differently depending on where it sits. On disk it keeps its package
    directory (``orch_hub/api.pyc``); inside an archive a freezer stores it
    under its *dotted module name* with no directories at all
    (``orch_hub.api.pyc``), which no directory-component rule would catch.

    ``root_scripts`` says whether a bare single-component name is suspicious.
    It is for the payload directory, where a lone ``.py`` file can only be a
    stray build script — and is not inside an archive, where the standard
    library's own modules legitimately sit at the root.
    """
    path = PurePosixPath(str(relative))
    if path.suffix.lower() not in SOURCE_SUFFIXES:
        return False
    parts = path.parts
    directories = frozenset(parts[:-1])
    if directories & (FIRST_PARTY_DIRS | {package_name}):
        return True
    stem = path.stem
    if stem == package_name or stem.startswith(f"{package_name}."):
        return True
    return root_scripts and len(parts) == 1


def _scan_file(
    collector: _Collector,
    path: Path,
    posix: str,
    literals: Sequence[ForbiddenLiteral],
    package_name: str,
) -> None:
    if path.suffix.lower() in ARCHIVE_SUFFIXES:
        _scan_archive(collector, path, posix, literals, package_name)
        return
    if not literals:
        return
    try:
        matched = _scan_stream(path, literals)
    except OSError as exc:
        collector.add(UNREADABLE, posix, f"could not read payload file: {type(exc).__name__}")
        return
    for literal in matched:
        collector.add(literal.kind, posix, f"contains {literal.label}")


def _scan_archive(
    collector: _Collector,
    path: Path,
    posix: str,
    literals: Sequence[ForbiddenLiteral],
    package_name: str,
) -> None:
    """Audit an archive's members as payload members in their own right."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        collector.add(UNREADABLE, posix, f"could not open archive: {type(exc).__name__}")
        return
    collector.archives_scanned += 1
    with archive:
        for name in archive.namelist():
            member = f"{posix}!{name}"
            member_path = PurePosixPath(name)
            if _is_first_party_source(member_path, package_name, root_scripts=False):
                collector.add(
                    FIRST_PARTY_SOURCE, member, "first-party python source in archive"
                )
            for part in member_path.parts:
                if part in REPOSITORY_STATE_NAMES:
                    collector.add(
                        REPOSITORY_STATE, member, f"repository state member: {part}"
                    )
                    break
            if not literals:
                continue
            try:
                data = archive.read(name)
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                collector.add(
                    UNREADABLE, member, f"could not read archive member: {type(exc).__name__}"
                )
                continue
            for literal in literals:
                if literal.encoded() in data:
                    collector.add(literal.kind, member, f"contains {literal.label}")


def _scan_stream(path: Path, literals: Sequence[ForbiddenLiteral]) -> list[ForbiddenLiteral]:
    """Return the literals present in ``path``, reading it in bounded chunks.

    Consecutive chunks overlap by the longest literal minus one byte so a match
    that straddles a chunk boundary is still found; without the overlap the
    audit would pass on exactly the payloads a big binary is most likely to
    produce.
    """
    needles = [(literal, literal.encoded()) for literal in literals]
    if not needles:
        return []
    longest = max(len(needle) for _, needle in needles)
    overlap = max(longest - 1, 0)
    found: list[ForbiddenLiteral] = []
    remaining = list(needles)
    carry = b""
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            window = carry + chunk
            still_missing = []
            for literal, needle in remaining:
                if needle in window:
                    found.append(literal)
                else:
                    still_missing.append((literal, needle))
            remaining = still_missing
            carry = window[-overlap:] if overlap else b""
    return found
