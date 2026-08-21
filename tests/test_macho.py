"""Tests for the Mach-O facts a release payload declares about itself.

The architecture and minimum-macOS values decide whether an installer accepts
an artifact, so they are read from real header bytes rather than inferred from
the build host. Synthetic headers pin the parsing rules (including the
byte-swapped and universal-binary spellings); a check against a system binary
pins that the reader agrees with what macOS actually ships.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from orch_hub import macho

ARM64 = macho.CPU_TYPE_ARM | macho.CPU_ARCH_ABI64
X86_64 = macho.CPU_TYPE_X86 | macho.CPU_ARCH_ABI64


def packed_version(major: int, minor: int, patch: int = 0) -> int:
    return (major << 16) | (minor << 8) | patch


def build_version_command(major: int, minor: int, patch: int = 0, platform: int = 1) -> bytes:
    return struct.pack(
        "<IIIIII",
        macho.LC_BUILD_VERSION,
        24,
        platform,
        packed_version(major, minor, patch),
        packed_version(major, minor, patch),
        0,
    )


def version_min_command(major: int, minor: int) -> bytes:
    return struct.pack(
        "<IIII",
        macho.LC_VERSION_MIN_MACOSX,
        16,
        packed_version(major, minor),
        packed_version(major, minor),
    )


def slice_bytes(cpu_type: int, commands: bytes, ncmds: int) -> bytes:
    header = struct.pack(
        "<IiiIIII", macho.MH_MAGIC_64, cpu_type, 0, 2, ncmds, len(commands), 0
    )
    return header + struct.pack("<I", 0) + commands


def write_macho(path: Path, cpu_type: int, commands: bytes, ncmds: int = 1) -> Path:
    path.write_bytes(slice_bytes(cpu_type, commands, ncmds))
    return path


def write_fat(path: Path, slices: list[bytes]) -> Path:
    header = struct.pack(">II", macho.FAT_MAGIC, len(slices))
    entry_size = 20
    offset = len(header) + entry_size * len(slices)
    entries = b""
    body = b""
    for payload in slices:
        cpu_type = struct.unpack_from("<i", payload, 4)[0]
        entries += struct.pack(">iiIII", cpu_type, 0, offset, len(payload), 0)
        body += payload
        offset += len(payload)
    path.write_bytes(header + entries + body)
    return path


def test_reads_architecture_and_min_macos(tmp_path: Path):
    path = write_macho(tmp_path / "bin", ARM64, build_version_command(15, 4))
    assert macho.read_macho(path) == macho.MachOFacts(
        architectures=("arm64",), min_macos="15.4"
    )


def test_reads_the_legacy_version_min_command(tmp_path: Path):
    path = write_macho(tmp_path / "bin", X86_64, version_min_command(10, 13))
    facts = macho.read_macho(path)
    assert facts.architectures == ("x86_64",)
    assert facts.min_macos == "10.13"


def test_patch_component_is_kept_when_present(tmp_path: Path):
    path = write_macho(tmp_path / "bin", ARM64, build_version_command(14, 6, 1))
    assert macho.read_macho(path).min_macos == "14.6.1"


def test_non_macos_platform_is_not_reported_as_a_macos_minimum(tmp_path: Path):
    # PLATFORM_IOS and friends share the load command; only the macOS target
    # says anything about where this payload can run.
    path = write_macho(tmp_path / "bin", ARM64, build_version_command(17, 0, platform=2))
    facts = macho.read_macho(path)
    assert facts.architectures == ("arm64",)
    assert facts.min_macos is None


def test_universal_binary_reports_every_slice(tmp_path: Path):
    path = write_fat(
        tmp_path / "fat",
        [
            slice_bytes(X86_64, build_version_command(11, 0), 1),
            slice_bytes(ARM64, build_version_command(12, 0), 1),
        ],
    )
    facts = macho.read_macho(path)
    assert set(facts.architectures) == {"x86_64", "arm64"}
    # The highest slice minimum is the floor for the file as a whole.
    assert facts.min_macos == "12.0"


def test_non_macho_files_are_not_mistaken_for_binaries(tmp_path: Path):
    text = tmp_path / "notes.txt"
    text.write_text("just text", encoding="utf-8")
    assert macho.read_macho(text) is None
    assert macho.read_macho(tmp_path / "absent") is None


def test_truncated_header_is_reported_as_not_macho(tmp_path: Path):
    stub = tmp_path / "stub"
    stub.write_bytes(struct.pack("<I", macho.MH_MAGIC_64)[:3])
    assert macho.read_macho(stub) is None


def test_scan_payload_aggregates_the_highest_minimum(tmp_path: Path):
    write_macho(tmp_path / "orch-hub", ARM64, build_version_command(11, 0))
    (tmp_path / "_internal").mkdir()
    write_macho(tmp_path / "_internal" / "libssl.dylib", ARM64, build_version_command(26, 0))
    (tmp_path / "_internal" / "notes.txt").write_text("x", encoding="utf-8")
    facts = macho.scan_payload(tmp_path)
    assert facts.architectures == ("arm64",)
    # One library built for a newer macOS raises the floor for the whole payload.
    assert facts.min_macos == "26.0"
    assert facts.binaries == 2
    assert facts.to_dict() == {
        "architectures": ["arm64"],
        "min_macos": "26.0",
        "binaries": 2,
    }


def test_scan_payload_reports_nothing_for_a_binary_free_tree(tmp_path: Path):
    (tmp_path / "readme.txt").write_text("x", encoding="utf-8")
    assert macho.scan_payload(tmp_path) == macho.PayloadFacts(
        architectures=(), min_macos=None, binaries=0
    )


def test_scan_payload_does_not_follow_directory_symlinks(tmp_path: Path):
    real = tmp_path / "Versions" / "A"
    real.mkdir(parents=True)
    write_macho(real / "Python", ARM64, build_version_command(11, 0))
    (tmp_path / "Versions" / "Current").symlink_to(real)
    assert macho.scan_payload(tmp_path).binaries == 1


def test_agrees_with_a_real_system_binary():
    system_binary = Path("/bin/ls")
    if not system_binary.exists():  # pragma: no cover - non-macOS host
        pytest.skip("no /bin/ls to read as a real Mach-O file")
    facts = macho.read_macho(system_binary)
    assert facts is not None
    assert "arm64" in facts.architectures or "x86_64" in facts.architectures
    assert facts.min_macos is not None
