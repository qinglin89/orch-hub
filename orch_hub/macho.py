"""Mach-O facts a macOS release payload has to declare about itself.

A compiled release is only usable on machines whose architecture and OS version
its binaries actually support, and both facts are properties of the *build*, not
of the source: the interpreter and libraries a builder happens to have decide
them. Reading them back out of the produced payload turns "we think this runs on
an Apple Silicon Mac" into a measured statement the release manifest can carry
and an installer can check before unpacking anything.

The reader is a small header/load-command walk rather than a shell out to
``lipo``/``vtool``: those live in the Xcode command line tools, which a build
machine is not otherwise required to have, and a subprocess per binary over a
few hundred bundled libraries is slow for what is a fixed-offset read.

Only what the release contract needs is parsed — architectures and the minimum
macOS version. Anything unrecognised is reported as unknown rather than guessed.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

MH_MAGIC_64 = 0xFEEDFACF
MH_CIGAM_64 = 0xCFFAEDFE
MH_MAGIC_32 = 0xFEEDFACE
MH_CIGAM_32 = 0xCEFAEDFE
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
"""Mach-O and universal-binary magics. ``CIGAM`` spellings are the byte-swapped
forms; fat headers are always big-endian."""

LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32
"""Load commands carrying a deployment target. ``LC_BUILD_VERSION`` is the
modern one; ``LC_VERSION_MIN_MACOSX`` still appears in older dependencies."""

PLATFORM_MACOS = 1

CPU_ARCH_ABI64 = 0x01000000
CPU_TYPE_X86 = 7
CPU_TYPE_ARM = 12
_ARCH_NAMES = {
    CPU_TYPE_X86 | CPU_ARCH_ABI64: "x86_64",
    CPU_TYPE_ARM | CPU_ARCH_ABI64: "arm64",
    CPU_TYPE_X86: "i386",
    CPU_TYPE_ARM: "arm",
}

_HEADER_64_SIZE = 32
_HEADER_32_SIZE = 28


@dataclass(frozen=True)
class MachOFacts:
    """What one Mach-O file declares about where it can run."""

    architectures: tuple[str, ...]
    """Architecture names, in file order; empty when none was recognised."""

    min_macos: str | None
    """Highest minimum macOS version across the file's slices, ``None`` when no
    slice declares one (common for a slice built without a deployment target)."""


@dataclass(frozen=True)
class PayloadFacts:
    """Aggregate of every Mach-O file found under a payload directory."""

    architectures: tuple[str, ...]
    """Sorted union of the architectures the payload's binaries were built for."""

    min_macos: str | None
    """The highest minimum macOS across the payload — the floor the whole
    payload actually requires, since one library built for a newer macOS raises
    the requirement for everything that loads it."""

    binaries: int
    """Number of Mach-O files inspected."""

    def to_dict(self) -> dict:
        return {
            "architectures": list(self.architectures),
            "min_macos": self.min_macos,
            "binaries": self.binaries,
        }


def read_macho(path: Path) -> MachOFacts | None:
    """Read one file's Mach-O facts, or ``None`` when it is not a Mach-O file.

    Unreadable files also answer ``None``: this reader describes what it can
    see, and a caller that needs unreadable members reported has the payload
    audit for that.
    """
    try:
        with path.open("rb") as handle:
            return _read_handle(handle)
    except (OSError, struct.error):
        return None


def scan_payload(root: Path) -> PayloadFacts:
    """Aggregate Mach-O facts across every file under ``root``."""
    architectures: set[str] = set()
    highest: tuple[int, ...] | None = None
    highest_text: str | None = None
    binaries = 0
    for path in _walk_files(root):
        facts = read_macho(path)
        if facts is None:
            continue
        binaries += 1
        architectures.update(facts.architectures)
        if facts.min_macos is None:
            continue
        parsed = _version_key(facts.min_macos)
        if highest is None or parsed > highest:
            highest = parsed
            highest_text = facts.min_macos
    return PayloadFacts(
        architectures=tuple(sorted(architectures)),
        min_macos=highest_text,
        binaries=binaries,
    )


def _walk_files(root: Path) -> Iterator[Path]:
    """Yield regular files under ``root`` without following directory symlinks.

    Framework bundles are full of symlinked directories pointing at a sibling
    version; descending into them would inspect the same binaries repeatedly.
    """
    if root.is_file():
        yield root
        return
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            yield from _walk_files(entry)
        elif entry.is_file():
            yield entry


def _read_handle(handle) -> MachOFacts | None:
    head = handle.read(4)
    if len(head) < 4:
        return None
    magic = struct.unpack(">I", head)[0]
    if magic in (FAT_MAGIC, FAT_MAGIC_64):
        return _read_fat(handle, magic)
    slice_facts = _read_slice(handle, 0)
    return slice_facts


def _read_fat(handle, magic: int) -> MachOFacts | None:
    """Read a universal binary by visiting each slice at its recorded offset."""
    count_bytes = handle.read(4)
    if len(count_bytes) < 4:
        return None
    count = struct.unpack(">I", count_bytes)[0]
    entry_size = 32 if magic == FAT_MAGIC_64 else 20
    offset_format = ">QQ" if magic == FAT_MAGIC_64 else ">II"
    architectures: list[str] = []
    versions: list[str] = []
    for index in range(count):
        handle.seek(8 + index * entry_size)
        entry = handle.read(entry_size)
        if len(entry) < entry_size:
            break
        # Slice offset/size follow cputype/cpusubtype in both fat layouts.
        slice_offset = struct.unpack_from(offset_format, entry, 8)[0]
        facts = _read_slice(handle, slice_offset)
        if facts is None:
            continue
        architectures.extend(facts.architectures)
        if facts.min_macos is not None:
            versions.append(facts.min_macos)
    if not architectures and not versions:
        return None
    return MachOFacts(
        architectures=tuple(architectures), min_macos=_highest(versions)
    )


def _read_slice(handle, offset: int) -> MachOFacts | None:
    handle.seek(offset)
    header = handle.read(_HEADER_64_SIZE)
    if len(header) < _HEADER_32_SIZE:
        return None
    magic = struct.unpack_from("<I", header, 0)[0]
    if magic in (MH_MAGIC_64, MH_MAGIC_32):
        endian = "<"
    elif magic in (MH_CIGAM_64, MH_CIGAM_32):
        endian = ">"
    else:
        return None
    is_64 = magic in (MH_MAGIC_64, MH_CIGAM_64)
    cpu_type, _, _, ncmds, _ = struct.unpack_from(endian + "iiIII", header, 4)
    architecture = _ARCH_NAMES.get(cpu_type)
    commands_offset = offset + (_HEADER_64_SIZE if is_64 else _HEADER_32_SIZE)
    min_macos = _read_min_macos(handle, commands_offset, ncmds, endian)
    return MachOFacts(
        architectures=(architecture,) if architecture else (),
        min_macos=min_macos,
    )


def _read_min_macos(handle, offset: int, ncmds: int, endian: str) -> str | None:
    handle.seek(offset)
    for _ in range(ncmds):
        header = handle.read(8)
        if len(header) < 8:
            return None
        cmd, cmdsize = struct.unpack(endian + "II", header)
        if cmdsize < 8:
            return None
        body = handle.read(cmdsize - 8)
        if cmd == LC_BUILD_VERSION and len(body) >= 8:
            platform, minos = struct.unpack_from(endian + "II", body, 0)
            if platform == PLATFORM_MACOS:
                return _format_version(minos)
        elif cmd == LC_VERSION_MIN_MACOSX and len(body) >= 4:
            return _format_version(struct.unpack_from(endian + "I", body, 0)[0])
    return None


def _format_version(value: int) -> str:
    """Decode the packed ``X.Y.Z`` version Mach-O stores in a uint32."""
    major = (value >> 16) & 0xFFFF
    minor = (value >> 8) & 0xFF
    patch = value & 0xFF
    return f"{major}.{minor}.{patch}" if patch else f"{major}.{minor}"


def _version_key(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _highest(versions: Iterable[str]) -> str | None:
    best: str | None = None
    for version in versions:
        if best is None or _version_key(version) > _version_key(best):
            best = version
    return best
