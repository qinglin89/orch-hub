"""Tests for the release payload audit.

The audit is the last check before a payload is handed to someone outside this
repository, so its failure modes matter in both directions: a missed finding
ships a maintainer's token or absolute path inside a binary, and a false
positive fails every build against ordinary third-party content. The tests
build synthetic payloads for both — including the archive case, where a
freezer's own module naming looks nothing like an on-disk package.
"""

from __future__ import annotations

import marshal
import struct
import zipfile
import zlib
from pathlib import Path

from orch_hub.release_audit import (
    FIRST_PARTY_SOURCE,
    MAINTAINER_PATH,
    MISSING_REQUIRED,
    REPOSITORY_STATE,
    SECRET_MATERIAL,
    UNREADABLE,
    ForbiddenLiteral,
    audit_payload,
    maintainer_path_literals,
)


def write(root: Path, relative: str, content: bytes | str = b"") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def kinds(report) -> list[str]:
    return [finding.kind for finding in report.findings]


def paths_for(report, kind: str) -> list[str]:
    return [finding.path for finding in report.findings if finding.kind == kind]


def test_clean_payload_has_no_findings(tmp_path: Path):
    write(tmp_path, "orch-hub", b"\xcf\xfa\xed\xfe binary")
    write(tmp_path, "_internal/orch_hub/web/index.html", "<html>orch-hub</html>")
    write(tmp_path, "_internal/libssl.dylib", b"\x00\x01")
    report = audit_payload(tmp_path)
    assert report.clean
    assert report.findings == ()
    assert report.files_scanned == 3


def test_first_party_source_on_disk_is_reported(tmp_path: Path):
    write(tmp_path, "_internal/orch_hub/api.py", "print('hi')")
    write(tmp_path, "_internal/orch_hub/api.pyc", b"\x00")
    report = audit_payload(tmp_path)
    assert paths_for(report, FIRST_PARTY_SOURCE) == [
        "_internal/orch_hub/api.py",
        "_internal/orch_hub/api.pyc",
    ]


def test_first_party_test_tree_is_reported(tmp_path: Path):
    write(tmp_path, "_internal/tests/test_api.py", "def test(): pass")
    assert paths_for(audit_payload(tmp_path), FIRST_PARTY_SOURCE) == [
        "_internal/tests/test_api.py"
    ]


def test_loose_script_at_the_payload_root_is_reported(tmp_path: Path):
    write(tmp_path, "build_runtime.py", "raise SystemExit")
    assert paths_for(audit_payload(tmp_path), FIRST_PARTY_SOURCE) == ["build_runtime.py"]


def test_third_party_python_data_file_is_not_first_party(tmp_path: Path):
    # A dependency may ship a .py as data inside its own tree; only first-party
    # locations count, otherwise the audit fails every real build.
    write(tmp_path, "_internal/somepkg/templates/example.py", "x = 1")
    assert audit_payload(tmp_path).clean


def test_dotted_module_name_inside_an_archive_is_reported(tmp_path: Path):
    # A freezer stores modules under their dotted name with no directories, so
    # the directory-component rule alone would miss first-party source here.
    archive = tmp_path / "_internal" / "modules.pyz"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("orch_hub.api.pyc", b"\x00")
        zf.writestr("orch_hub.pyc", b"\x00")
    report = audit_payload(tmp_path)
    assert paths_for(report, FIRST_PARTY_SOURCE) == [
        "_internal/modules.pyz!orch_hub.api.pyc",
        "_internal/modules.pyz!orch_hub.pyc",
    ]
    assert report.archives_scanned == 1


def test_stdlib_modules_at_an_archive_root_are_not_first_party(tmp_path: Path):
    # The freezer's base_library.zip holds the standard library's own modules at
    # the archive root; the loose-script rule must not fire on them.
    archive = tmp_path / "_internal" / "base_library.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("os.pyc", b"\x00")
        zf.writestr("collections/__init__.pyc", b"\x00")
    assert audit_payload(tmp_path).clean


def test_repository_state_is_reported(tmp_path: Path):
    write(tmp_path, "_internal/.ai-tasks/2026-01-01-x.md", "task")
    write(tmp_path, "CLAUDE.md", "protocol")
    report = audit_payload(tmp_path)
    assert sorted(paths_for(report, REPOSITORY_STATE)) == [
        "CLAUDE.md",
        "_internal/.ai-tasks",
        "_internal/.ai-tasks/2026-01-01-x.md",
    ]


def test_credential_shaped_files_are_reported(tmp_path: Path):
    write(tmp_path, "_internal/certs/client.pem", "-----BEGIN-----")
    write(tmp_path, "_internal/.env", "TOKEN=x")
    assert sorted(paths_for(audit_payload(tmp_path), SECRET_MATERIAL)) == [
        "_internal/.env",
        "_internal/certs/client.pem",
    ]


def test_forbidden_literal_is_reported_without_quoting_it(tmp_path: Path):
    write(tmp_path, "_internal/blob.bin", b"prefix sk-live-secret suffix")
    literal = ForbiddenLiteral(label="bearer token", value="sk-live-secret")
    report = audit_payload(tmp_path, literals=[literal])
    assert paths_for(report, SECRET_MATERIAL) == ["_internal/blob.bin"]
    # The whole point of the label: the value must not reach a report or a log.
    assert "sk-live-secret" not in report.findings[0].detail
    assert "bearer token" in report.findings[0].detail


def test_literal_spanning_a_chunk_boundary_is_found(tmp_path: Path, monkeypatch):
    # Big binaries are exactly where a straddling match happens, so the chunked
    # scan overlaps; without the overlap this payload would audit clean.
    monkeypatch.setattr("orch_hub.release_audit._CHUNK_SIZE", 16)
    write(tmp_path, "_internal/blob.bin", b"A" * 14 + b"/Users/maintainer" + b"B" * 40)
    literals = maintainer_path_literals(["/Users/maintainer"])
    assert paths_for(audit_payload(tmp_path, literals=literals), MAINTAINER_PATH) == [
        "_internal/blob.bin"
    ]


def test_literals_are_found_inside_archive_members(tmp_path: Path):
    archive = tmp_path / "_internal" / "base_library.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("os.pyc", b"compiled from /Users/maintainer/checkout/x.py")
    literals = maintainer_path_literals(["/Users/maintainer"])
    assert paths_for(audit_payload(tmp_path, literals=literals), MAINTAINER_PATH) == [
        "_internal/base_library.zip!os.pyc"
    ]


def module_archive(modules: dict[str, bytes]) -> bytes:
    """A freezer's module archive: each module zlib-compressed on its own.

    Written here rather than imported from the freezer, for the same reason the
    audit parses it by hand: the format is what a released payload contains, and
    the build driver runs on an interpreter that does not have the freezer
    installed. A change in the format has to fail this test.
    """
    header = _PYZ_MAGIC + b"\x00\x01\x02\x03"  # magic, then a bytecode version
    body = b""
    entries = []
    for name, source in modules.items():
        blob = zlib.compress(source)
        entries.append((name, (0, len(header) + 4 + len(body), len(blob))))
        body += blob
    entries.append(("namespace_pkg", (3, 0, 0)))  # a package with no data blob
    toc_offset = len(header) + 4 + len(body)
    return header + struct.pack("!i", toc_offset) + body + marshal.dumps(entries)


def freezer_archive(entries: list[tuple[str, bytes, str, bool]]) -> bytes:
    """An executable-shaped file with a freezer archive appended to it.

    The archive is neither at the start of the file nor at its end: real
    payloads carry the program's own code in front of it and, on macOS, a code
    signature behind it.
    """
    body = b""
    toc = b""
    for name, blob, typecode, compress in entries:
        stored = zlib.compress(blob) if compress else blob
        raw_name = name.encode("utf-8") + b"\0"
        entry_length = _TOC_LENGTH + len(raw_name)
        padding = -entry_length % 16  # the freezer pads names to a 16-byte stride
        toc += struct.pack(
            "!IIIIBc",
            entry_length + padding,
            len(_PROGRAM_CODE) + len(body),
            len(stored),
            len(blob),
            int(compress),
            typecode.encode("ascii"),
        )
        toc += raw_name + b"\0" * padding
        body += stored
    archive = body + toc
    cookie = struct.pack(
        "!8sIIII64s",
        _MAGIC,
        len(_PROGRAM_CODE) + len(archive) + _COOKIE_LENGTH,
        len(_PROGRAM_CODE) + len(body),
        len(toc),
        314,
        b"Python",
    )
    return _PROGRAM_CODE + archive + cookie + _SIGNATURE


_MAGIC = b"MEI\014\013\012\013\016"
_PYZ_MAGIC = b"PYZ\0"
_COOKIE_LENGTH = struct.calcsize("!8sIIII64s")
_TOC_LENGTH = struct.calcsize("!IIIIBc")
_PROGRAM_CODE = b"\xcf\xfa\xed\xfe" + b"bootloader" * 200
_SIGNATURE = b"\xfa\xde\x0c\xc0" + b"signature" * 100


def test_literal_inside_a_frozen_module_is_reported(tmp_path: Path):
    # The payload's Python is not in any file: it is a compressed code object
    # inside an archive inside the executable. Scanning the file's bytes proves
    # nothing about it, which is exactly where a build path or a token hides.
    frozen = b"secret lives at /Users/maintainer/checkout in this code object"
    executable = write(
        tmp_path,
        "orch-hub",
        freezer_archive(
            [
                ("PYZ.pyz", module_archive({"orch_hub.api": frozen}), "z", False),
                ("pyi-contents-directory _internal", b"", "o", False),
            ]
        ),
    )
    assert b"/Users/maintainer" not in executable.read_bytes()  # invisible on disk

    literals = maintainer_path_literals(["/Users/maintainer"])
    report = audit_payload(tmp_path, literals=literals)
    assert paths_for(report, MAINTAINER_PATH) == ["orch-hub!PYZ.pyz!orch_hub.api"]
    # The executable's archive and the module archive nested inside it.
    assert report.archives_scanned == 2


def test_literal_inside_a_frozen_entry_script_is_reported(tmp_path: Path):
    # Not every frozen code object lives in the module archive: the program's
    # entry script and the freezer's own bootstrap sit in the outer archive,
    # compressed one by one.
    write(
        tmp_path,
        "orch-hub",
        freezer_archive([("__main__", b"token sk-live-secret", "s", True)]),
    )
    literal = ForbiddenLiteral(label="bearer token", value="sk-live-secret")
    report = audit_payload(tmp_path, literals=[literal])
    assert paths_for(report, SECRET_MATERIAL) == ["orch-hub!__main__"]
    assert "sk-live-secret" not in report.findings[0].detail


def test_frozen_module_names_are_not_read_as_source_files(tmp_path: Path):
    # A frozen module is a code object named `orch_hub.api`, not a shipped
    # source file — flagging it would fail every real build.
    write(
        tmp_path,
        "orch-hub",
        freezer_archive(
            [("PYZ.pyz", module_archive({"orch_hub.api": b"", "os": b""}), "z", False)]
        ),
    )
    assert audit_payload(tmp_path).clean


def test_source_file_inside_a_freezer_archive_is_reported(tmp_path: Path):
    # A data entry, though, is a file the payload carries under its own name.
    write(
        tmp_path,
        "orch-hub",
        freezer_archive([("orch_hub/api.pyc", b"\x00", "x", False)]),
    )
    assert paths_for(audit_payload(tmp_path), FIRST_PARTY_SOURCE) == [
        "orch-hub!orch_hub/api.pyc"
    ]


def test_corrupt_freezer_archive_is_reported_rather_than_skipped(tmp_path: Path):
    # A structurally sound header commits the file to archive handling, so a
    # truncated archive behind it is an unproven payload, not a clean one.
    intact = freezer_archive([("PYZ.pyz", module_archive({"orch_hub.api": b""}), "z", False)])
    write(tmp_path, "orch-hub", intact.replace(_PYZ_MAGIC, b"XXX\0", 1))
    assert paths_for(audit_payload(tmp_path), UNREADABLE) == ["orch-hub!PYZ.pyz"]


def test_a_member_declared_outside_the_file_is_reported(tmp_path: Path):
    # A table of contents is data, and a corrupt one can claim a member of any
    # size at any offset. Reading it as declared would end the build in an
    # allocation instead of a finding.
    archive = bytearray(freezer_archive([("CORRUPT_ME", b"data", "x", False)]))
    entry = archive.rfind(b"CORRUPT_ME") - _TOC_LENGTH
    archive[entry + 4 : entry + 12] = struct.pack("!II", 0xFFFFFF00, 0xFFFFFF00)
    write(tmp_path, "orch-hub", bytes(archive))

    literal = ForbiddenLiteral(label="bearer token", value="sk-live-secret")
    report = audit_payload(tmp_path, literals=[literal])
    assert paths_for(report, UNREADABLE) == ["orch-hub!CORRUPT_ME"]


def test_magic_bytes_without_a_valid_header_are_not_an_archive(tmp_path: Path):
    # The pattern can occur in ordinary binary content; treating that as a
    # corrupt archive would report a finding on a clean payload.
    write(tmp_path, "_internal/libssl.dylib", b"\x00" * 64 + _MAGIC + b"\x01" * 64)
    assert audit_payload(tmp_path).clean


def test_missing_required_member_is_reported(tmp_path: Path):
    write(tmp_path, "orch-hub", b"\x00")
    report = audit_payload(tmp_path, required=("orch-hub", "runtime.json"))
    assert paths_for(report, MISSING_REQUIRED) == ["runtime.json"]


def test_absent_payload_root_is_a_finding_not_a_crash(tmp_path: Path):
    report = audit_payload(tmp_path / "missing")
    assert kinds(report) == [MISSING_REQUIRED]
    assert not report.clean


def test_unreadable_archive_is_reported_rather_than_skipped(tmp_path: Path):
    # An unscanned member is an unproven one; a corrupt archive must not read as
    # a clean payload.
    write(tmp_path, "_internal/broken.zip", b"not a zip at all")
    assert paths_for(audit_payload(tmp_path), UNREADABLE) == ["_internal/broken.zip"]


def test_maintainer_path_literals_skip_empty_and_root_paths():
    literals = maintainer_path_literals(["", "/", "/Users/a", "/Users/a/", Path("/Users/a")])
    assert [literal.value for literal in literals] == ["/Users/a"]
    assert literals[0].kind == MAINTAINER_PATH


def test_directory_symlinks_are_not_followed(tmp_path: Path):
    write(tmp_path, "_internal/real/orch_hub/api.py", "x = 1")
    (tmp_path / "_internal" / "link").symlink_to(tmp_path / "_internal" / "real")
    report = audit_payload(tmp_path)
    # Reported once, through the real path — not a second time through the link.
    assert paths_for(report, FIRST_PARTY_SOURCE) == ["_internal/real/orch_hub/api.py"]
