"""Tests for the release packager and the installer it renders.

Two halves, deliberately different in kind.

The first half pins what the packager *decides*: what it refuses to package,
what the produced metadata says, and that the artifact set is a pure function
of the payload. The second half runs the produced ``.run`` file with a real
POSIX shell against a synthetic payload whose executable only records its argv.
That is the only way to establish what the released script actually does — the
substitution collision between ``REPLACE-ORCH-HUB-ARCH`` and
``REPLACE-ORCH-HUB-ARCHIVE-SHA256`` produced a script that parsed fine, was
byte-identical in structure to a working one, and failed its own checksum on
every machine.

The real frozen payload is packaged and exercised by the opt-in smoke test
(tests/test_frozen_runtime.py); it needs a build, so it lives there.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from orch_hub import install, release_package
from orch_hub.release_build import MANIFEST_NAME, MANIFEST_SCHEMA, REQUIRED_MEMBERS
from orch_hub.release_package import (
    CHECKSUM_NAME,
    RELEASE_NAME,
    RELEASE_SCHEMA,
    PackageError,
    PackageOptions,
    build_archive,
    package_release,
    read_payload_manifest,
    render_installer,
    sha256_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / release_package.TEMPLATE_RELATIVE_PATH

# The synthetic executable stands in for the compiled runtime: it appends its
# argv to a log and, for `setup`, can be made to fail. Everything the installer
# script is responsible for is visible in that log.
FAKE_RUNTIME = """#!/bin/sh
printf '%s\\n' "$@" >> "$FAKE_ARGV_LOG"
if [ "$1" = setup ] && [ -n "${FAKE_SETUP_FAILS:-}" ]; then
    exit 3
fi
exit 0
"""


def make_payload(
    root: Path,
    *,
    version: str = "9.9.9",
    target: str = "darwin-arm64",
    os_name: str | None = None,
    arch: str | None = None,
    min_macos: str | None = "11.0",
    name: str = "orch-hub",
    executable: str = "orch-hub",
    manifest_overrides: dict | None = None,
) -> Path:
    """A payload directory shaped like a real one, minus 30 MB of interpreter.

    ``os_name``/``arch`` default to the halves of ``target``: the packager
    refuses a manifest whose target key contradicts the os/arch it carries, so
    a test that is not about that contradiction must not state one by accident.
    """
    declared_os, _, declared_arch = target.partition("-")
    os_name = declared_os if os_name is None else os_name
    arch = declared_arch if arch is None else arch
    payload = root / f"{name}-{target}"
    (payload / "_internal").mkdir(parents=True, exist_ok=True)
    binary = payload / executable
    binary.write_text(FAKE_RUNTIME, encoding="utf-8")
    binary.chmod(0o755)
    (payload / "_internal" / "asset.dat").write_bytes(b"payload bytes\n")
    link = payload / "_internal" / "asset-link.dat"
    if not link.is_symlink():
        link.symlink_to("asset.dat")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "name": name,
        "version": version,
        "target": target,
        "os": os_name,
        "arch": arch,
        "executable": executable,
        "min_macos": min_macos,
        "architectures": [arch],
        "binaries": 1,
        "python_version": "3.14.4",
        "builder": "pyinstaller 6.22.2",
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    (payload / MANIFEST_NAME).write_text(
        json.dumps({k: v for k, v in manifest.items() if v is not None}, indent=2),
        encoding="utf-8",
    )
    return payload


def options(tmp_path: Path, *payloads: Path, **overrides) -> PackageOptions:
    defaults = dict(
        payload_dirs=tuple(payloads),
        out_dir=tmp_path / "release",
        work_dir=tmp_path / "work",
        template_path=TEMPLATE,
        # The synthetic payload has none of the required frozen members, so the
        # audit gate is exercised on purpose in its own tests and stood down
        # here — every other test is about something else.
        allow_findings=True,
    )
    defaults.update(overrides)
    return PackageOptions(**defaults)


def package(tmp_path: Path, *payloads: Path, **overrides) -> Path:
    result = package_release(options(tmp_path, *payloads, **overrides))
    return result.out_dir


def tree_state(root: Path) -> dict[str, str]:
    """Every member of a tree, keyed by relative path and content-addressed.

    Used to prove a refused packaging changed *nothing*. Symlinks are recorded
    as their target instead of being followed, and modes are part of the state:
    the payload is a tree of relative links around one executable file, so
    "unchanged" has to mean unchanged for those too.
    """
    state: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[key] = f"link -> {os.readlink(path)}"
        elif path.is_dir():
            state[key] = "dir"
        else:
            state[key] = f"{sha256_file(path)} {path.stat().st_mode & 0o777:o}"
    return state


# --------------------------------------------------------------------------
# What the packager refuses
# --------------------------------------------------------------------------


def test_a_directory_without_a_manifest_is_not_a_payload(tmp_path: Path):
    (tmp_path / "nothing").mkdir()
    with pytest.raises(PackageError, match="not an orch-hub payload"):
        read_payload_manifest(tmp_path / "nothing")


def test_a_manifest_from_another_schema_is_refused(tmp_path: Path):
    payload = make_payload(tmp_path, manifest_overrides={"schema": "orch-hub-runtime/2"})
    with pytest.raises(PackageError, match="declares schema"):
        read_payload_manifest(payload)


@pytest.mark.parametrize(
    "field", ["name", "version", "target", "os", "arch", "executable"]
)
def test_a_manifest_missing_an_embedded_field_is_refused(tmp_path: Path, field: str):
    payload = make_payload(tmp_path, manifest_overrides={field: ""})
    with pytest.raises(PackageError, match=f"declares no {field}"):
        read_payload_manifest(payload)


@pytest.mark.parametrize("hostile", ["9.9.9'; rm -rf /; '", "9.9.9\nrm -rf /"])
def test_a_manifest_value_that_would_break_out_of_the_shell_is_refused(
    tmp_path: Path, hostile: str
):
    # Every embedded value lands inside a single-quoted shell assignment in a
    # file other people run, so a quote or newline is not a rendering problem.
    payload = make_payload(tmp_path, manifest_overrides={"version": hostile})
    with pytest.raises(PackageError, match="cannot be embedded"):
        read_payload_manifest(payload)


@pytest.mark.parametrize("field", ["name", "executable"])
def test_a_payload_that_is_not_orch_hub_is_refused(tmp_path: Path, field: str):
    # The installer enforces both names too, so packaging something else would
    # only produce an artifact that installation then refuses.
    payload = make_payload(tmp_path, manifest_overrides={field: "something-else"})
    with pytest.raises(PackageError, match="a release payload is named"):
        read_payload_manifest(payload)


def test_a_payload_without_its_declared_executable_is_refused(tmp_path: Path):
    payload = make_payload(tmp_path)
    (payload / "orch-hub").unlink()
    with pytest.raises(PackageError, match="no executable at"):
        read_payload_manifest(payload)


def test_a_payload_executable_without_its_executable_bit_is_refused(tmp_path: Path):
    # The archive keeps the mode it finds. Nothing before installation notices:
    # `--check` proves only host and digest, so the artifact would announce
    # itself installable and then fail its own post-unpack `-x` test on every
    # machine that ran it.
    payload = make_payload(tmp_path)
    (payload / "orch-hub").chmod(0o644)
    with pytest.raises(PackageError, match="is not executable"):
        package(tmp_path, payload)
    assert not (tmp_path / "release").exists()


def test_a_payload_executable_that_is_not_a_regular_file_is_refused(tmp_path: Path):
    # Symlinks are archived as symlinks, so the declared runtime being a link
    # means the real binary is some other member under some other name.
    payload = make_payload(tmp_path)
    (payload / "orch-hub").rename(payload / "orch-hub.real")
    (payload / "orch-hub").symlink_to("orch-hub.real")
    with pytest.raises(PackageError, match="not a regular file"):
        package(tmp_path, payload)
    assert not (tmp_path / "release").exists()


@pytest.mark.parametrize("version", [".hidden", "a/b", "..", "9.9.9/", "9.9.9\0"])
def test_a_version_the_runtime_could_not_install_is_refused(tmp_path: Path, version: str):
    """The packager may not be more permissive than the thing installing its output.

    A release is immutable: an artifact whose version the runtime refuses can
    only be withdrawn, never corrected. Both readers are run over the same
    payload so the two rules cannot drift apart unnoticed.
    """
    payload = make_payload(tmp_path, version=version)
    with pytest.raises(install.InstallError, match="single path component"):
        install.read_payload_manifest(payload)
    with pytest.raises(PackageError, match="single path component"):
        read_payload_manifest(payload)
    with pytest.raises(PackageError):
        package(tmp_path, payload)
    assert not (tmp_path / "release").exists()


def test_a_target_that_contradicts_the_payload_it_carries_is_refused(tmp_path: Path):
    # `target` is the release key and the artifact's file name; `os`/`arch` are
    # what the installer's host gate compares. Letting them disagree publishes
    # an arm64 runtime under the x86_64 key — and the artifact still answers
    # `--check` with "installable" on the arm64 Mac it was really built for.
    payload = make_payload(tmp_path, target="darwin-x86_64", arch="arm64")
    with pytest.raises(PackageError, match="under another target's key"):
        read_payload_manifest(payload)
    with pytest.raises(PackageError):
        package(tmp_path, payload)
    assert not (tmp_path / "release").exists()


def test_a_target_that_is_not_a_single_path_component_is_refused(tmp_path: Path):
    # The target names the artifact file and the archive's root directory.
    payload = make_payload(
        tmp_path, target="darwin/arm64", os_name="darwin", arch="arm64"
    )
    with pytest.raises(PackageError, match="single path component"):
        read_payload_manifest(payload)


@pytest.mark.parametrize("unnormalised", [{"os_name": "Darwin"}, {"arch": "ARM64"}])
def test_an_os_or_arch_the_installer_cannot_compare_is_refused(
    tmp_path: Path, unnormalised: dict
):
    # The header's gate lowercases `uname -s` but takes `uname -m` verbatim, so
    # an unnormalised manifest renders an installer that refuses every machine
    # on earth — while the runtime's own check_target, which lowercases both
    # sides, would have accepted the very same payload.
    payload = make_payload(tmp_path, **unnormalised)
    with pytest.raises(PackageError, match="against the host verbatim"):
        read_payload_manifest(payload)
    install.check_target(
        json.loads((payload / MANIFEST_NAME).read_text(encoding="utf-8")),
        system="darwin",
        machine="arm64",
        macos="26.0",
    )


def test_a_missing_min_macos_is_allowed_and_renders_empty(tmp_path: Path):
    # Absence means "no declared floor", which the installer treats as "do not
    # compare" — not as a floor of zero and not as a reason to refuse. Only a
    # target the release promises a floor for is held to one, so this is asked
    # of a target that carries no promise.
    payload = make_payload(tmp_path, target="darwin-x86_64", min_macos=None)
    assert "min_macos" not in read_payload_manifest(payload)
    out = package(tmp_path, payload)
    header = _header(next(out.glob("*.run")))
    assert "MIN_MACOS=''" in header
    release = json.loads((out / RELEASE_NAME).read_text(encoding="utf-8"))
    assert release["targets"]["darwin-x86_64"]["min_macos"] is None


def test_the_shipped_target_is_held_to_the_floor_it_promises(tmp_path: Path):
    # The build refuses this payload, and can be told not to; packaging is the
    # step whose output somebody downloads, so it asks again.
    payload = make_payload(tmp_path, min_macos="26.0")

    with pytest.raises(PackageError, match="requires macOS 26.0"):
        package(tmp_path, payload)

    assert not (tmp_path / "release").exists()


def test_the_shipped_target_must_declare_a_floor_at_all(tmp_path: Path):
    # The installer skips its macOS gate on an empty floor, so publishing one
    # removes the check from every machine that installs it.
    payload = make_payload(tmp_path, min_macos=None)

    with pytest.raises(PackageError, match="declares no minimum macOS"):
        package(tmp_path, payload)


@pytest.mark.parametrize("declared", ["unknown", "11.x", "10..15", ".11", "11."])
@pytest.mark.parametrize("target", ["darwin-arm64", "darwin-x86_64"])
def test_a_floor_that_cannot_be_read_is_not_published(
    tmp_path: Path, target: str, declared: str
):
    # Two routes to the same published artifact: against the pinned target each
    # of these compares at or below the 11.0 promise, because a reader scoring
    # unreadable components as zero cannot do otherwise, and a target with no
    # pinned floor has nothing to compare them against at all. What arrives is
    # identical either way — the installer's own numeric test rejects all five,
    # so it skips the macOS refusal and installs on a Mac that cannot run it.
    payload = make_payload(tmp_path, target=target, min_macos=declared)

    with pytest.raises(PackageError, match="not a dotted numeric version"):
        package(tmp_path, payload)

    assert not (tmp_path / "release").exists()


def test_an_unreadable_floor_is_not_what_the_escape_hatch_is_for(tmp_path: Path):
    # --allow-unsupported-macos publishes a payload whose floor is known and
    # higher than the release supports. A floor that cannot be read is not a
    # higher one; it is a manifest no build here produces, so there is nothing
    # to wave through.
    payload = make_payload(tmp_path, min_macos="unknown")

    with pytest.raises(PackageError, match="not a dotted numeric version"):
        package(tmp_path, payload, allow_unsupported_macos=True)


def test_an_unsupported_floor_can_be_packaged_deliberately(tmp_path: Path, capsys):
    payload = make_payload(tmp_path, min_macos="26.0")

    out = package(tmp_path, payload, allow_unsupported_macos=True)

    assert "MIN_MACOS='26.0'" in _header(next(out.glob("*.run")))
    assert "warning:" in capsys.readouterr().err


def test_payloads_of_different_versions_are_not_one_release(tmp_path: Path):
    first = make_payload(tmp_path / "a", target="darwin-arm64")
    second = make_payload(tmp_path / "b", target="darwin-x86_64", version="9.9.10")
    with pytest.raises(PackageError, match="one release is one version"):
        package(tmp_path, first, second)


def test_two_payloads_for_the_same_target_are_refused(tmp_path: Path):
    first = make_payload(tmp_path / "a")
    second = make_payload(tmp_path / "b")
    with pytest.raises(PackageError, match="same target"):
        package(tmp_path, first, second)


def test_packaging_re_audits_the_payload_and_fails_closed(tmp_path: Path):
    # The build audits what it froze; this audits what is about to become a
    # downloadable file. Only the second covers a payload built earlier and
    # tampered with (or simply copied from) since.
    payload = make_payload(tmp_path)
    (payload / "leaked.py").write_text("secret = 1\n", encoding="utf-8")
    with pytest.raises(PackageError, match="first_party_source"):
        package_release(options(tmp_path, payload, allow_findings=False))
    assert not (tmp_path / "release").exists()


def test_the_audit_gate_reports_what_the_real_build_requires(tmp_path: Path):
    # The packager passes release_build's own required-member list, so a payload
    # missing the dashboard assets or the plist template is rejected by the same
    # rule the build uses rather than by a second, drifting copy of it.
    payload = make_payload(tmp_path)
    with pytest.raises(PackageError) as excinfo:
        package_release(options(tmp_path, payload, allow_findings=False))
    message = str(excinfo.value)
    absent = [member for member in REQUIRED_MEMBERS if not (payload / member).exists()]
    assert absent and all(member in message for member in absent)


# Layout refusals. A payload is read and audited and must come out unchanged;
# the scratch directory is emptied and rewritten; the release directory is
# replaced wholesale. Every pair is refused in both directions, because each
# direction applies one of those fates to the other's contents.


def test_an_output_directory_that_contains_the_payload_is_refused(tmp_path: Path):
    # Publishing replaces the release directory, so `--out dist` (one level too
    # high) would delete the payload the build just produced.
    payload = make_payload(tmp_path / "dist")
    before = tree_state(payload)
    with pytest.raises(PackageError, match="contains the payload"):
        package(tmp_path, payload, out_dir=tmp_path / "dist")
    assert tree_state(payload) == before


def test_a_payload_that_contains_the_output_directory_is_refused(tmp_path: Path):
    # The quiet direction: the release lands *inside* the payload, so the
    # artifact keeps the clean audit it was given a moment earlier while the
    # audited payload now carries a release, and packaging it again archives
    # the previous release inside the next one.
    payload = make_payload(tmp_path)
    before = tree_state(payload)
    with pytest.raises(PackageError, match="contains the release directory"):
        package(tmp_path, payload, out_dir=payload / "release")
    assert tree_state(payload) == before


def test_a_payload_inside_the_scratch_directory_is_refused(tmp_path: Path):
    # The staging area is deleted and recreated before anything is archived,
    # which took the payload with it and then failed with the input gone.
    work = tmp_path / "work"
    (work / "package-stage").mkdir(parents=True)
    payload = make_payload(work / "package-stage")
    before = tree_state(payload)
    with pytest.raises(PackageError, match="contains the payload"):
        package(tmp_path, payload, work_dir=work)
    assert tree_state(payload) == before


def test_a_payload_that_contains_the_scratch_directory_is_refused(tmp_path: Path):
    payload = make_payload(tmp_path)
    before = tree_state(payload)
    with pytest.raises(PackageError, match="contains the scratch directory"):
        package(tmp_path, payload, work_dir=payload / "work")
    assert tree_state(payload) == before


def test_an_output_directory_that_contains_the_scratch_area_is_refused(tmp_path: Path):
    payload = make_payload(tmp_path)
    with pytest.raises(PackageError, match="contains the scratch directory"):
        package(
            tmp_path,
            payload,
            out_dir=tmp_path / "release",
            work_dir=tmp_path / "release" / "work",
        )


def test_a_scratch_directory_that_contains_the_output_is_refused(tmp_path: Path):
    payload = make_payload(tmp_path)
    with pytest.raises(PackageError, match="disposable scratch"):
        package(
            tmp_path,
            payload,
            out_dir=tmp_path / "work" / "release",
            work_dir=tmp_path / "work",
        )


def test_a_refused_layout_changes_neither_the_payload_nor_the_last_release(
    tmp_path: Path,
):
    # The refusal has to come before the audit and before staging: everything
    # after that point either reads the payload as ground truth or replaces the
    # release directory, and both are irreversible by the time they run.
    payload = make_payload(tmp_path / "payload")
    out = package(tmp_path, payload)
    payload_before = tree_state(payload)
    release_before = tree_state(out)
    with pytest.raises(PackageError):
        package(tmp_path, payload, out_dir=payload / "release")
    with pytest.raises(PackageError):
        package(tmp_path, payload, work_dir=payload / "work")
    assert tree_state(payload) == payload_before
    assert tree_state(out) == release_before


def test_a_missing_payload_directory_is_an_error(tmp_path: Path):
    with pytest.raises(PackageError, match="payload directory not found"):
        package(tmp_path, tmp_path / "absent")


def test_no_payload_at_all_is_an_error(tmp_path: Path):
    with pytest.raises(PackageError, match="no payload directory"):
        package(tmp_path)


def test_a_missing_template_is_an_error(tmp_path: Path):
    payload = make_payload(tmp_path)
    with pytest.raises(PackageError, match="cannot read the installer template"):
        package(tmp_path, payload, template_path=tmp_path / "absent.sh")


# --------------------------------------------------------------------------
# Rendering the installer header
# --------------------------------------------------------------------------


VALUES = {
    "REPLACE-ORCH-HUB-VERSION": "9.9.9",
    "REPLACE-ORCH-HUB-TARGET": "darwin-arm64",
    "REPLACE-ORCH-HUB-OS": "darwin",
    "REPLACE-ORCH-HUB-ARCH": "arm64",
    "REPLACE-ORCH-HUB-MIN-MACOS": "11.0",
    "REPLACE-ORCH-HUB-PAYLOAD-DIR": "orch-hub-darwin-arm64",
    "REPLACE-ORCH-HUB-COMMAND": "orch-hub",
}
DIGEST = "a" * 64


def test_a_shorter_placeholder_does_not_eat_a_longer_one():
    # REPLACE-ORCH-HUB-ARCH is a prefix of REPLACE-ORCH-HUB-ARCHIVE-SHA256.
    # Substituting token by token rewrote the inside of the longer one and
    # produced an installer that failed its own checksum everywhere.
    rendered = render_installer(TEMPLATE.read_text(encoding="utf-8"), VALUES, DIGEST)
    assert "ARCH='arm64'" in rendered
    assert f"ARCHIVE_SHA256='{DIGEST}'" in rendered
    assert "arm64IVE" not in rendered


def test_the_archive_line_points_just_past_the_header():
    rendered = render_installer(TEMPLATE.read_text(encoding="utf-8"), VALUES, DIGEST)
    line = int(rendered.split("ARCHIVE_LINE='")[1].split("'")[0])
    assert line == rendered.count("\n") + 1


def test_a_placeholder_the_packager_cannot_fill_is_an_error():
    template = TEMPLATE.read_text(encoding="utf-8") + "\nFUTURE='REPLACE-ORCH-HUB-NEW'\n"
    with pytest.raises(PackageError, match="cannot fill: REPLACE-ORCH-HUB-NEW"):
        render_installer(template, VALUES, DIGEST)


def test_a_value_with_no_placeholder_left_in_the_template_is_an_error():
    # A removed placeholder means the released script no longer carries a value
    # it reads under `set -u`; silence there ships a broken installer.
    template = TEMPLATE.read_text(encoding="utf-8").replace(
        "MIN_MACOS='REPLACE-ORCH-HUB-MIN-MACOS'", "MIN_MACOS=''"
    )
    with pytest.raises(PackageError, match="missing required placeholders"):
        render_installer(template, VALUES, DIGEST)


def test_a_template_without_the_archive_offset_is_an_error():
    template = TEMPLATE.read_text(encoding="utf-8").replace(
        "ARCHIVE_LINE='REPLACE-ORCH-HUB-ARCHIVE-LINE'", "ARCHIVE_LINE='1'"
    )
    with pytest.raises(PackageError, match="REPLACE-ORCH-HUB-ARCHIVE-LINE"):
        render_installer(template, VALUES, DIGEST)


def test_the_checked_in_template_is_valid_posix_shell():
    # The template is not runnable (its placeholders are not values), but it
    # must parse — a syntax error would only surface in a released artifact.
    completed = subprocess.run(
        ["sh", "-n", str(TEMPLATE)], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr


# --------------------------------------------------------------------------
# The payload archive
# --------------------------------------------------------------------------


def test_the_archive_is_byte_identical_across_runs(tmp_path: Path):
    payload = make_payload(tmp_path)
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    build_archive(payload, "orch-hub-darwin-arm64", first)
    os.utime(payload / "_internal" / "asset.dat", (1, 1))
    build_archive(payload, "orch-hub-darwin-arm64", second)
    assert first.read_bytes() == second.read_bytes()


def test_the_archive_keeps_symlinks_modes_and_sorted_order(tmp_path: Path):
    payload = make_payload(tmp_path)
    archive_path = tmp_path / "payload.tar.gz"
    build_archive(payload, "root-name", archive_path)
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
    names = [member.name for member in members]
    assert names == sorted(names)
    assert all(name.startswith("root-name/") for name in names)
    by_name = {member.name: member for member in members}
    # Resolving the bundled framework's relative links would change the
    # runtime's shape and size, so they stay links.
    assert by_name["root-name/_internal/asset-link.dat"].issym()
    assert by_name["root-name/_internal/asset-link.dat"].linkname == "asset.dat"
    assert by_name["root-name/orch-hub"].mode == 0o755
    assert by_name["root-name/_internal/asset.dat"].mode == 0o644
    assert all(member.mtime == 0 for member in members)
    assert all(member.uid == 0 and member.uname == "" for member in members)


def test_the_gzip_wrapper_records_neither_name_nor_time(tmp_path: Path):
    payload = make_payload(tmp_path)
    archive_path = tmp_path / "payload.tar.gz"
    build_archive(payload, "root-name", archive_path)
    header = archive_path.read_bytes()[:10]
    assert header[4:8] == b"\0\0\0\0"  # MTIME field
    assert not header[3] & 0x08  # FNAME flag


def test_the_archive_root_comes_from_the_manifest_not_the_directory(tmp_path: Path):
    # A payload someone copied elsewhere must still produce the same archive, or
    # reproducibility would depend on where the build was left lying around.
    payload = make_payload(tmp_path)
    copied = tmp_path / "somewhere-else"
    shutil.copytree(payload, copied, symlinks=True)
    original = package(tmp_path / "a", payload)
    relocated = package(tmp_path / "b", copied)
    assert sha256_file(next(original.glob("*.run"))) == sha256_file(
        next(relocated.glob("*.run"))
    )


# --------------------------------------------------------------------------
# The artifact set
# --------------------------------------------------------------------------


def test_packaging_emits_an_installer_metadata_and_checksums(tmp_path: Path):
    payload = make_payload(tmp_path)
    out = package(tmp_path, payload)
    assert sorted(path.name for path in out.iterdir()) == [
        CHECKSUM_NAME,
        "orch-hub-9.9.9-darwin-arm64.run",
        RELEASE_NAME,
    ]
    assert os.access(out / "orch-hub-9.9.9-darwin-arm64.run", os.X_OK)


def test_a_payload_manifest_is_carried_into_the_release_verbatim(tmp_path: Path):
    # A consumer comparing an installed runtime.json against the published
    # release has to find the same document, key for key.
    payload = make_payload(tmp_path)
    out = package(tmp_path, payload)
    release = json.loads((out / RELEASE_NAME).read_text(encoding="utf-8"))
    written = json.loads((payload / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert release["targets"]["darwin-arm64"]["runtime"] == written
    assert release["targets"]["darwin-arm64"]["min_macos"] == "11.0"


def test_release_metadata_keys_artifacts_by_target(tmp_path: Path):
    payload = make_payload(tmp_path)
    out = package(tmp_path, payload)
    release = json.loads((out / RELEASE_NAME).read_text(encoding="utf-8"))
    assert release["schema"] == RELEASE_SCHEMA
    assert release["version"] == "9.9.9"
    assert release["checksums"] == CHECKSUM_NAME
    entry = release["targets"]["darwin-arm64"]
    assert (entry["os"], entry["arch"], entry["min_macos"]) == ("darwin", "arm64", "11.0")
    assert entry["runtime"]["builder"] == "pyinstaller 6.22.2"
    installer = out / "orch-hub-9.9.9-darwin-arm64.run"
    assert entry["artifacts"] == [
        {
            "name": installer.name,
            "kind": "installer",
            "size": installer.stat().st_size,
            "sha256": sha256_file(installer),
        }
    ]


def test_a_second_target_is_one_more_key(tmp_path: Path):
    first = make_payload(tmp_path / "a", target="darwin-arm64", arch="arm64")
    second = make_payload(tmp_path / "b", target="darwin-x86_64", arch="x86_64")
    out = package(tmp_path, first, second)
    release = json.loads((out / RELEASE_NAME).read_text(encoding="utf-8"))
    assert sorted(release["targets"]) == ["darwin-arm64", "darwin-x86_64"]
    assert sorted(path.name for path in out.glob("*.run")) == [
        "orch-hub-9.9.9-darwin-arm64.run",
        "orch-hub-9.9.9-darwin-x86_64.run",
    ]


def test_checksums_cover_every_other_artifact_and_verify(tmp_path: Path):
    payload = make_payload(tmp_path)
    out = package(tmp_path, payload)
    lines = (out / CHECKSUM_NAME).read_text(encoding="utf-8").splitlines()
    listed = {line.split("  ", 1)[1] for line in lines}
    # A checksum file cannot contain its own digest; everything else is covered.
    assert listed == {path.name for path in out.iterdir()} - {CHECKSUM_NAME}
    for line in lines:
        digest, name = line.split("  ", 1)
        assert sha256_file(out / name) == digest
    completed = subprocess.run(
        ["shasum", "-a", "256", "-c", CHECKSUM_NAME],
        cwd=out,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_whole_artifact_set_is_a_pure_function_of_the_payload(tmp_path: Path):
    payload = make_payload(tmp_path)
    first = package(tmp_path / "a", payload)
    second = package(tmp_path / "b", payload)
    digests = {
        path.name: sha256_file(path) for path in sorted(first.iterdir())
    }
    assert digests == {path.name: sha256_file(path) for path in sorted(second.iterdir())}


def test_a_failed_packaging_leaves_the_previous_release_in_place(tmp_path: Path):
    payload = make_payload(tmp_path)
    out = package(tmp_path, payload)
    marker = sha256_file(out / RELEASE_NAME)
    (payload / "leaked.py").write_text("secret = 1\n", encoding="utf-8")
    with pytest.raises(PackageError):
        package_release(options(tmp_path, payload, allow_findings=False))
    assert sha256_file(out / RELEASE_NAME) == marker
    assert not list(out.parent.glob("release.incoming"))


def test_a_second_packaging_replaces_the_release_directory(tmp_path: Path):
    payload = make_payload(tmp_path)
    out = package(tmp_path, payload)
    (out / "stale-artifact.run").write_text("old\n", encoding="utf-8")
    package(tmp_path, payload)
    assert not (out / "stale-artifact.run").exists()
    assert (out / RELEASE_NAME).is_file()


# --------------------------------------------------------------------------
# The CLI driver
# --------------------------------------------------------------------------


def test_the_cli_packages_the_default_payload_location(tmp_path: Path, capsys):
    checkout = tmp_path / "checkout"
    (checkout / "dist").mkdir(parents=True)
    make_payload(checkout / "dist")
    code = release_package.main(
        [
            "--checkout",
            str(checkout),
            "--work",
            str(tmp_path / "work"),
            # The default template is checkout-relative; this synthetic checkout
            # holds a payload and nothing else.
            "--template",
            str(TEMPLATE),
            "--allow-findings",
            "--json",
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["version"] == "9.9.9"
    assert Path(report["out"]) == (checkout / "dist" / "release").resolve()
    assert (checkout / "dist" / "release" / RELEASE_NAME).is_file()


def test_the_cli_reports_a_refusal_as_an_error_line(tmp_path: Path, capsys):
    code = release_package.main(
        [
            "--checkout",
            str(tmp_path),
            "--work",
            str(tmp_path / "work"),
            "--template",
            str(TEMPLATE),
        ]
    )
    assert code == 1
    assert capsys.readouterr().err.startswith("error: payload directory not found")


def test_the_cli_creates_nothing_before_refusing_a_layout(tmp_path: Path, capsys):
    # The scratch tree is created by the staging step, which runs after the
    # layout check. Creating it up front left an empty directory inside the
    # payload the CLI then refused to package — and an empty directory is an
    # archive member, so the refused run would have changed what that payload
    # packages to on the next, legal run.
    payload = make_payload(tmp_path)
    before = tree_state(payload)
    code = release_package.main(
        [
            "--payload",
            str(payload),
            "--out",
            str(tmp_path / "release"),
            "--work",
            str(payload / "work"),
            "--template",
            str(TEMPLATE),
            "--allow-findings",
        ]
    )
    assert code == 1
    assert "contains the scratch directory" in capsys.readouterr().err
    assert tree_state(payload) == before


# --------------------------------------------------------------------------
# What the released installer actually does
# --------------------------------------------------------------------------


def run_installer(
    installer: Path, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment.update(env or {})
    return subprocess.run(
        ["sh", str(installer), *args],
        capture_output=True,
        text=True,
        env=environment,
        cwd=None if cwd is None else str(cwd),
    )


@pytest.fixture
def installer(tmp_path: Path) -> Path:
    payload = make_payload(tmp_path)
    return next(package(tmp_path, payload).glob("*.run"))


@pytest.fixture
def argv_log(tmp_path: Path) -> Path:
    return tmp_path / "argv.log"


def shim(tmp_path: Path, name: str, body: str) -> str:
    """Put a stand-in for a system command first on PATH, and return that PATH.

    ``uname`` and ``sw_vers`` are the two facts the installer refuses on, and
    neither can be varied any other way on the machine running the tests.
    """
    directory = tmp_path / "shim"
    directory.mkdir(exist_ok=True)
    script = directory / name
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return f"{directory}:{os.environ['PATH']}"


def test_version_is_answered_without_touching_the_machine(installer: Path, tmp_path: Path):
    # Identifying a download must work on the machine that turns out not to be
    # able to run it, so --version precedes every check including reading back
    # this file.
    path = shim(tmp_path, "uname", "#!/bin/sh\necho nonsense\n")
    result = run_installer(installer, "--version", env={"PATH": path})
    assert result.returncode == 0
    assert result.stdout.strip() == "orch-hub 9.9.9 (darwin-arm64)"


def test_help_names_the_verbs_an_operator_needs(installer: Path):
    result = run_installer(installer, "--help")
    assert result.returncode == 0
    for flag in (
        "--prefix",
        "--bin-dir",
        "--mandrel",
        "--token",
        "--host",
        "--port",
        "--no-service",
        "--extract",
    ):
        assert flag in result.stdout


def test_an_unknown_option_is_refused(installer: Path):
    result = run_installer(installer, "--nope")
    assert result.returncode == 1
    assert "unknown option '--nope'" in result.stderr


def test_an_option_without_its_value_is_refused(installer: Path):
    result = run_installer(installer, "--prefix")
    assert result.returncode == 1
    assert "--prefix needs a value" in result.stderr


def test_an_option_given_an_empty_value_is_refused(installer: Path):
    # `--prefix=` silently meaning "the default" would install somewhere the
    # operator did not name; both spellings answer the same way.
    for spelling in (["--extract="], ["--prefix", ""]):
        result = run_installer(installer, *spelling)
        assert result.returncode == 1
        assert "needs a value" in result.stderr


def test_check_confirms_the_host_and_the_payload(installer: Path):
    result = run_installer(installer, "--check")
    assert result.returncode == 0
    assert "is installable on this machine" in result.stdout


def test_a_foreign_architecture_is_refused_before_anything_is_unpacked(
    installer: Path, tmp_path: Path
):
    path = shim(
        tmp_path,
        "uname",
        "#!/bin/sh\ncase $1 in -m) echo x86_64 ;; *) echo Darwin ;; esac\n",
    )
    result = run_installer(installer, "--prefix", str(tmp_path / "prefix"), env={"PATH": path})
    assert result.returncode == 1
    assert "but this machine is darwin-x86_64" in result.stderr
    assert not (tmp_path / "prefix").exists()


def test_an_older_macos_is_refused_before_anything_is_unpacked(
    installer: Path, tmp_path: Path
):
    path = shim(tmp_path, "sw_vers", "#!/bin/sh\necho 10.15.7\n")
    result = run_installer(installer, "--prefix", str(tmp_path / "prefix"), env={"PATH": path})
    assert result.returncode == 1
    assert "requires macOS 11.0 or newer" in result.stderr
    assert not (tmp_path / "prefix").exists()


def test_a_macos_version_that_cannot_be_read_does_not_refuse(
    installer: Path, tmp_path: Path
):
    # Same rule as the runtime's own target check: an unreadable version is not
    # evidence of a mismatch, so it is not grounds for refusing to install.
    path = shim(tmp_path, "sw_vers", "#!/bin/sh\necho 15.0-beta\n")
    result = run_installer(installer, "--check", env={"PATH": path})
    assert result.returncode == 0


def test_a_newer_macos_passes_the_floor(installer: Path, tmp_path: Path):
    path = shim(tmp_path, "sw_vers", "#!/bin/sh\necho 26.0\n")
    assert run_installer(installer, "--check", env={"PATH": path}).returncode == 0


def test_a_truncated_installer_is_refused(installer: Path, tmp_path: Path):
    truncated = tmp_path / "truncated.run"
    truncated.write_bytes(installer.read_bytes()[:-500])
    result = run_installer(truncated, "--check")
    assert result.returncode == 1
    assert "truncated or corrupt" in result.stderr


def test_a_single_flipped_byte_is_refused(installer: Path, tmp_path: Path):
    data = bytearray(installer.read_bytes())
    data[-20] ^= 0xFF
    corrupt = tmp_path / "corrupt.run"
    corrupt.write_bytes(bytes(data))
    result = run_installer(corrupt, "--check")
    assert result.returncode == 1
    assert "truncated or corrupt" in result.stderr


def test_an_installer_that_arrived_through_a_pipe_says_so(installer: Path):
    # `curl ... | sh` cannot work: the archive is read back out of the file.
    result = subprocess.run(
        f"cat {installer} | sh -s -- --check",
        shell=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "run it by path" in result.stderr


def test_extract_unpacks_the_payload_and_installs_nothing(installer: Path, tmp_path: Path):
    destination = tmp_path / "unpacked"
    result = run_installer(installer, "--extract", str(destination))
    assert result.returncode == 0
    payload = destination / "orch-hub-darwin-arm64"
    assert (payload / MANIFEST_NAME).is_file()
    assert os.access(payload / "orch-hub", os.X_OK)
    assert (payload / "_internal" / "asset-link.dat").is_symlink()
    assert not (tmp_path / "prefix").exists()


def test_extract_works_on_a_machine_the_payload_does_not_fit(
    installer: Path, tmp_path: Path
):
    # Extraction installs nothing and runs nothing; refusing it would only stop
    # someone from inspecting the artifact that just told them it does not fit.
    path = shim(
        tmp_path,
        "uname",
        "#!/bin/sh\ncase $1 in -m) echo x86_64 ;; *) echo Darwin ;; esac\n",
    )
    result = run_installer(
        installer, "--extract", str(tmp_path / "unpacked"), env={"PATH": path}
    )
    assert result.returncode == 0


def test_installing_delegates_to_the_extracted_runtime(
    installer: Path, tmp_path: Path, argv_log: Path
):
    prefix = tmp_path / "prefix"
    result = run_installer(
        installer,
        "--prefix",
        str(prefix),
        "--bin-dir",
        str(tmp_path / "bin"),
        "--mandrel",
        str(tmp_path / "mandrel"),
        env={"FAKE_ARGV_LOG": str(argv_log)},
    )
    assert result.returncode == 0, result.stderr
    calls = _calls(argv_log)
    assert calls[0][:2] == ["install", "--from"]
    assert calls[0][2].endswith("/orch-hub-darwin-arm64")
    assert calls[0][3:] == ["--prefix", str(prefix), "--bin-dir", str(tmp_path / "bin")]
    # `setup` with no further flags is exactly what `upgrade` runs, which is why
    # re-running the installer preserves the installed configuration.
    assert calls[1] == ["setup", "--prefix", str(prefix), "--mandrel", str(tmp_path / "mandrel")]


def test_the_service_flags_reach_setup_and_nothing_else(
    installer: Path, tmp_path: Path, argv_log: Path
):
    # A first install has to be able to bind somewhere reachable; every one of
    # these overrides a value `setup` would otherwise resolve from the
    # environment or the installed plist.
    result = run_installer(
        installer,
        "--token",
        "a-token",
        "--host",
        "100.64.0.1",
        "--port",
        "9100",
        env={"FAKE_ARGV_LOG": str(argv_log)},
    )
    assert result.returncode == 0, result.stderr
    calls = _calls(argv_log)
    assert calls[0] == ["install", "--from", calls[0][2]]
    assert calls[1] == [
        "setup",
        "--token",
        "a-token",
        "--host",
        "100.64.0.1",
        "--port",
        "9100",
    ]


def test_paths_with_spaces_survive_the_delegation(
    installer: Path, tmp_path: Path, argv_log: Path
):
    prefix = tmp_path / "an install dir"
    result = run_installer(
        installer, "--prefix", str(prefix), env={"FAKE_ARGV_LOG": str(argv_log)}
    )
    assert result.returncode == 0, result.stderr
    assert _calls(argv_log)[0][3:] == ["--prefix", str(prefix)]


def test_the_equals_spelling_of_an_option_works_too(
    installer: Path, tmp_path: Path, argv_log: Path
):
    prefix = tmp_path / "prefix"
    result = run_installer(
        installer, f"--prefix={prefix}", env={"FAKE_ARGV_LOG": str(argv_log)}
    )
    assert result.returncode == 0, result.stderr
    assert _calls(argv_log)[0][3:] == ["--prefix", str(prefix)]


def test_no_service_installs_the_runtime_and_stops(
    installer: Path, tmp_path: Path, argv_log: Path
):
    result = run_installer(
        installer,
        "--prefix",
        str(tmp_path / "prefix"),
        "--no-service",
        env={"FAKE_ARGV_LOG": str(argv_log)},
    )
    assert result.returncode == 0, result.stderr
    assert [call[0] for call in _calls(argv_log)] == ["install"]
    assert "orch-hub setup --mandrel" in result.stdout


def test_a_service_that_does_not_start_names_the_way_back(
    installer: Path, tmp_path: Path, argv_log: Path
):
    result = run_installer(
        installer,
        "--prefix",
        str(tmp_path / "prefix"),
        env={"FAKE_ARGV_LOG": str(argv_log), "FAKE_SETUP_FAILS": "1"},
    )
    assert result.returncode == 1
    assert "orch-hub rollback" in result.stderr
    assert "orch-hub launchd status" in result.stderr


def test_the_scratch_directory_is_removed_afterwards(
    installer: Path, tmp_path: Path, argv_log: Path
):
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    result = run_installer(
        installer,
        "--prefix",
        str(tmp_path / "prefix"),
        env={"FAKE_ARGV_LOG": str(argv_log), "TMPDIR": str(scratch)},
    )
    assert result.returncode == 0, result.stderr
    assert list(scratch.iterdir()) == []


def test_the_scratch_directory_is_removed_after_a_failure(
    installer: Path, tmp_path: Path, argv_log: Path
):
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    result = run_installer(
        installer,
        "--prefix",
        str(tmp_path / "prefix"),
        env={
            "FAKE_ARGV_LOG": str(argv_log),
            "FAKE_SETUP_FAILS": "1",
            "TMPDIR": str(scratch),
        },
    )
    assert result.returncode == 1
    assert list(scratch.iterdir()) == []


def _calls(argv_log: Path) -> list[list[str]]:
    """Group the recorded argv lines into one list per invocation."""
    calls: list[list[str]] = []
    for line in argv_log.read_text(encoding="utf-8").splitlines():
        if line in {"install", "setup"}:
            calls.append([line])
        else:
            calls[-1].append(line)
    return calls


def _header(installer: Path) -> str:
    """The shell part of a rendered installer, up to the appended archive."""
    data = installer.read_bytes()
    return data[: data.index(b"\nexit 0\n") + len(b"\nexit 0\n")].decode("utf-8")


def test_the_installer_is_the_header_followed_by_the_archive(installer: Path):
    header = _header(installer)
    line = int(header.split("ARCHIVE_LINE='")[1].split("'")[0])
    assert header.count("\n") + 1 == line
    body = installer.read_bytes()[len(header.encode("utf-8")) :]
    assert body[:2] == b"\x1f\x8b"  # gzip magic
    with tarfile.open(fileobj=gzip.GzipFile(fileobj=io.BytesIO(body))) as tar:
        assert "orch-hub-darwin-arm64/orch-hub" in tar.getnames()
