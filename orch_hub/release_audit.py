"""Audit of a built release payload before it is packaged for distribution.

A compiled release is handed to engineers who have no access to this
repository, so the payload has to be checked for what it must *not* contain:
first-party Python source, repository/task/protocol state, and any literal that
belongs to the machine that built it — an absolute home/checkout path, a bearer
token, a Bark endpoint. The freezer's own output is trustworthy in the normal
case; this module is what makes that a checked property instead of an
assumption, because the failure mode (a maintainer's token shipped inside a
release) is silent and unrecoverable once published.

The check has to reach the code that actually runs, which is not in any file:
the freezer appends its own archives, and the payload's Python lives inside
them as compressed code objects. So the audit opens zip archives, the archive
appended to an executable, and the module archive nested in that — a scan of
the bytes on disk would report a clean payload no matter what a frozen module
contains.

The audit is build-time tooling. It is deliberately not reachable from the CLI
and not part of a release payload: nothing in the served surface needs it, and
a release should not carry the list of things a release must not carry.

Findings never quote what they matched. A finding says *which file* contains a
forbidden literal and under which label; the value itself stays out of the
report, the build log, and any JSON a release pipeline may keep.
"""

from __future__ import annotations

import marshal
import struct
import zipfile
import zlib
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

_CARCHIVE_MAGIC = b"MEI\014\013\012\013\016"
_CARCHIVE_COOKIE_FORMAT = "!8sIIII64s"
_CARCHIVE_COOKIE_LENGTH = struct.calcsize(_CARCHIVE_COOKIE_FORMAT)
_CARCHIVE_TOC_FORMAT = "!IIIIBc"
_CARCHIVE_TOC_LENGTH = struct.calcsize(_CARCHIVE_TOC_FORMAT)
_CARCHIVE_PYZ_TYPECODE = "z"
_CARCHIVE_OPTION_TYPECODE = "o"
_PYZ_MAGIC = b"PYZ\0"
_PYZ_HEADER_LENGTH = 12
"""Layout of the archive a freezer appends to its executable.

The payload's real Python code does not sit in any file the operating system
can see: the executable carries an appended archive whose table of contents
points at zlib-compressed members, one of which is a second archive holding
every module as a compressed code object. A scan of the file's bytes therefore
proves nothing about the code that actually runs — a maintainer path or a token
baked into a module is compressed out of sight. These constants are read from
the format the freezer writes rather than from its library, because the audit
runs on the bare interpreter that drives a build, before the build environment
that holds the freezer exists.
"""


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
    if not _scan_embedded_archive(collector, path, posix, literals, package_name):
        return  # unreadable and already reported; the byte scan would fail alike
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
            _check_archive_member(collector, member, name, package_name)
            if not literals:
                continue
            try:
                data = archive.read(name)
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                collector.add(
                    UNREADABLE, member, f"could not read archive member: {type(exc).__name__}"
                )
                continue
            _check_member_literals(collector, member, data, literals)


def _check_archive_member(
    collector: _Collector, member: str, name: str, package_name: str
) -> None:
    """Apply the name-based rules to one member of any archive.

    Archive members are named by the tool that wrote them, so the same module
    appears as ``orch_hub.api.pyc`` in a zip, ``orch_hub.api`` in a freezer's
    module archive, and ``orch_hub/api.pyc`` on disk. All three spellings reach
    the same rules from here.
    """
    member_path = PurePosixPath(name)
    if _is_first_party_source(member_path, package_name, root_scripts=False):
        collector.add(FIRST_PARTY_SOURCE, member, "first-party python source in archive")
    for part in member_path.parts:
        if part in REPOSITORY_STATE_NAMES:
            collector.add(REPOSITORY_STATE, member, f"repository state member: {part}")
            break


def _check_member_literals(
    collector: _Collector,
    member: str,
    data: bytes,
    literals: Sequence[ForbiddenLiteral],
) -> None:
    for literal in literals:
        if literal.encoded() in data:
            collector.add(literal.kind, member, f"contains {literal.label}")


def _scan_embedded_archive(
    collector: _Collector,
    path: Path,
    posix: str,
    literals: Sequence[ForbiddenLiteral],
    package_name: str,
) -> bool:
    """Audit the freezer archive appended to ``path``, if it carries one.

    Returns whether the file could be read at all, so the caller can skip a
    byte scan that would fail the same way rather than reporting it twice.

    A file that merely happens to contain the magic bytes is not treated as an
    archive: the header is range-checked first, and only a structurally sound
    one commits the file to archive handling, where a later failure is an
    ``UNREADABLE`` finding instead of a silent skip.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            offset = _find_cookie(handle, size)
            if offset < 0:
                return True
            cookie = _read_cookie(handle, offset)
            if cookie is None:
                return True
            collector.archives_scanned += 1
            _scan_carchive(
                collector, handle, cookie, size, posix, literals, package_name
            )
    except OSError as exc:
        collector.add(UNREADABLE, posix, f"could not read payload file: {type(exc).__name__}")
        return False
    return True


def _find_cookie(handle, size: int) -> int:
    """Offset of the archive header in ``handle``, or ``-1`` when absent.

    Searched back-to-front because the header sits at the end of the archive,
    which itself sits behind the executable's own code — and, on macOS, in
    front of an appended code signature, so it is not at the end of the file
    either. Chunks overlap so a header split across two reads is still found.
    """
    end = size
    while end >= len(_CARCHIVE_MAGIC):
        start = max(end - _CHUNK_SIZE, 0)
        length = end - start
        if length < len(_CARCHIVE_MAGIC):
            break
        handle.seek(start)
        buffer = handle.read(length)
        found = buffer.rfind(_CARCHIVE_MAGIC)
        if found != -1:
            return start + found
        end = start + len(_CARCHIVE_MAGIC) - 1
    return -1


def _read_cookie(handle, offset: int) -> tuple[int, int, int] | None:
    """Parse the archive header into ``(start, toc_offset, toc_length)``."""
    handle.seek(offset)
    data = handle.read(_CARCHIVE_COOKIE_LENGTH)
    if len(data) < _CARCHIVE_COOKIE_LENGTH:
        return None
    _, length, toc_offset, toc_length, _, library = struct.unpack(
        _CARCHIVE_COOKIE_FORMAT, data
    )
    end = offset + _CARCHIVE_COOKIE_LENGTH
    start = end - length
    if not library.rstrip(b"\0") or start < 0 or toc_length <= 0:
        return None
    if start + toc_offset + toc_length > end:
        return None
    return start, toc_offset, toc_length


def _scan_carchive(
    collector: _Collector,
    handle,
    cookie: tuple[int, int, int],
    size: int,
    posix: str,
    literals: Sequence[ForbiddenLiteral],
    package_name: str,
) -> None:
    """Audit every member of the archive appended to a payload file."""
    start, toc_offset, toc_length = cookie
    try:
        entries = _carchive_entries(handle, start, toc_offset, toc_length)
    except (ValueError, struct.error) as exc:
        collector.add(
            UNREADABLE, posix, f"could not read embedded archive: {type(exc).__name__}"
        )
        return
    for name, offset, length, compressed, typecode in entries:
        if typecode == _CARCHIVE_OPTION_TYPECODE:
            continue  # a bootloader option, not a payload member
        member = f"{posix}!{name}"
        _check_archive_member(collector, member, name, package_name)
        if typecode == _CARCHIVE_PYZ_TYPECODE:
            _scan_pyz(
                collector, handle, start + offset, size, member, literals, package_name
            )
            continue
        if not literals or length <= 0:
            continue
        try:
            data = _read_entry(handle, start + offset, length, compressed, size)
        except (OSError, ValueError, zlib.error) as exc:
            collector.add(
                UNREADABLE, member, f"could not read archive member: {type(exc).__name__}"
            )
            continue
        _check_member_literals(collector, member, data, literals)


def _carchive_entries(
    handle, start: int, toc_offset: int, toc_length: int
) -> list[tuple[str, int, int, bool, str]]:
    handle.seek(start + toc_offset)
    data = handle.read(toc_length)
    entries = []
    cursor = 0
    while cursor + _CARCHIVE_TOC_LENGTH <= len(data):
        entry_length, offset, length, _, compressed, typecode = struct.unpack(
            _CARCHIVE_TOC_FORMAT, data[cursor : cursor + _CARCHIVE_TOC_LENGTH]
        )
        if entry_length < _CARCHIVE_TOC_LENGTH or cursor + entry_length > len(data):
            raise ValueError("archive table of contents is malformed")
        name = data[cursor + _CARCHIVE_TOC_LENGTH : cursor + entry_length]
        cursor += entry_length
        entries.append(
            (
                name.rstrip(b"\0").decode("utf-8", "replace"),
                offset,
                length,
                bool(compressed),
                typecode.decode("ascii", "replace"),
            )
        )
    return entries


def _scan_pyz(
    collector: _Collector,
    handle,
    start: int,
    size: int,
    member: str,
    literals: Sequence[ForbiddenLiteral],
    package_name: str,
) -> None:
    """Audit the module archive holding the payload's frozen code objects.

    Only each entry's decompressed bytes are scanned; the code objects are
    never unmarshalled. A literal lives in them as plain UTF-8 either way, and
    unmarshalling would tie the audit to the bytecode version of the
    interpreter that got bundled rather than the one running the build.
    """
    try:
        entries = _pyz_entries(handle, start)
    except (ValueError, EOFError, TypeError, struct.error) as exc:
        collector.add(
            UNREADABLE, member, f"could not read module archive: {type(exc).__name__}"
        )
        return
    collector.archives_scanned += 1
    for name, offset, length in entries:
        nested = f"{member}!{name}"
        _check_archive_member(collector, nested, name, package_name)
        if not literals or length <= 0:
            continue  # a namespace package has a table entry but no data
        try:
            data = _read_entry(handle, start + offset, length, True, size)
        except (OSError, ValueError, zlib.error) as exc:
            collector.add(
                UNREADABLE, nested, f"could not read archive member: {type(exc).__name__}"
            )
            continue
        _check_member_literals(collector, nested, data, literals)


def _pyz_entries(handle, start: int) -> list[tuple[str, int, int]]:
    handle.seek(start)
    header = handle.read(_PYZ_HEADER_LENGTH)
    if len(header) < _PYZ_HEADER_LENGTH or not header.startswith(_PYZ_MAGIC):
        raise ValueError("not a module archive")
    (toc_offset,) = struct.unpack("!i", header[8:_PYZ_HEADER_LENGTH])
    if toc_offset < 0:
        raise ValueError("module archive table of contents is malformed")
    handle.seek(start + toc_offset)
    entries = []
    for entry in marshal.load(handle):
        name, (_, offset, length) = entry
        entries.append((str(name), int(offset), int(length)))
    return entries


def _read_entry(handle, offset: int, length: int, compressed: bool, size: int) -> bytes:
    """Read one archive member, never further than the file it lives in.

    A table of contents is data, and a corrupt one can declare a member of any
    size at any offset. Clamping to the file keeps a damaged payload a finding
    rather than an allocation the build dies on.
    """
    if offset < 0 or offset >= size:
        raise ValueError("archive member lies outside the file")
    handle.seek(offset)
    data = handle.read(min(length, size - offset))
    return zlib.decompress(data) if compressed else data


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
