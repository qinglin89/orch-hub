"""Tests for user-local installation of a compiled runtime.

No test here builds or runs a real payload — that is what
``tests/test_frozen_runtime.py`` does. These drive the layout logic over a fake
payload directory (a manifest, an executable file, and a relative symlink) and
fake launchctl/health probes, so the version placement, selection, upgrade, and
rollback behavior is checked without a 30MB artifact or a resident service.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
from pathlib import Path

import pytest

from orch_hub import install, launchd
from orch_hub.atomic_dir import INCOMING_SUFFIX
from orch_hub.config import DATA_DIR_ENV, HOST_ENV, MANDREL_PATH_ENV, PORT_ENV, TOKEN_ENV
from orch_hub.install import (
    InstallError,
    SetupOptions,
    check_target,
    install_version,
    read_current,
    read_installation,
    read_payload_manifest,
    read_previous,
    rollback,
    select_version,
    setup_service,
    uninstall,
    upgrade,
)
from orch_hub.launchd import (
    LABEL,
    PLIST_NAME,
    TEMPLATE_RELATIVE_PATH,
    LaunchdError,
)
from orch_hub.subprocess_utils import CommandResult

HOST_OS = platform.system().lower()
HOST_ARCH = platform.machine().lower()


class FakeLaunchctl:
    """Records launchctl calls against a domain that holds no orch-hub job.

    ``bootout`` and ``print`` answer "not loaded" so the deploy and restart
    paths take their real branches (no unload wait, an actual bootstrap) instead
    of short-circuiting on a job the fake claims is already running.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args, **kwargs) -> CommandResult:
        argv = tuple(str(arg) for arg in args)
        self.calls.append(argv)
        if argv[:2] == ("launchctl", "bootout"):
            return CommandResult(argv, 113, "", "No such process")
        if argv[:2] == ("launchctl", "print"):
            return CommandResult(argv, 113, "", "Could not find service")
        return CommandResult(argv, 0, "ok", "")

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "launchctl"]


def make_payload(tmp_path: Path, version: str = "0.1.0", **manifest_extra) -> Path:
    """A directory shaped like a built payload, without being one."""
    payload = tmp_path / f"payload-{version}"
    if payload.is_dir():
        return payload  # same version, same content: reuse it
    (payload / "_internal" / "framework" / "Versions").mkdir(parents=True)
    executable = payload / "orch-hub"
    executable.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    executable.chmod(0o755)
    (payload / "_internal" / "framework" / "Versions" / "1.0").mkdir()
    # A relative link like the bundled Python framework carries, so a copy that
    # resolved links instead of preserving them would be visible.
    (payload / "_internal" / "framework" / "Current").symlink_to(
        Path("Versions") / "1.0"
    )
    manifest = {
        "schema": "orch-hub-runtime/1",
        "name": "orch-hub",
        "version": version,
        "target": f"{HOST_OS}-{HOST_ARCH}",
        "os": HOST_OS,
        "arch": HOST_ARCH,
        "executable": "orch-hub",
        "python_version": "3.14.4",
    }
    manifest.update(manifest_extra)
    (payload / "runtime.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return payload


def make_mandrel(tmp_path: Path) -> Path:
    mandrel = tmp_path / "mandrel"
    (mandrel / "bin").mkdir(parents=True, exist_ok=True)
    entry = mandrel / "bin" / "mandrel"
    entry.write_text("#!/bin/sh\n", encoding="utf-8")
    # Executable, because the registry adapter runs it. A fixture that skipped
    # this would be asserting setup accepts a checkout the hub cannot read.
    entry.chmod(0o755)
    return mandrel


@pytest.fixture(autouse=True)
def bundled_template(monkeypatch, tmp_path):
    """Make the checked-in plist template resolve as if it were bundled.

    Setup deploys from an installed runtime, which has no checkout — in a real
    release the template comes from the bundle. Pointing the resource lookup at
    the repository's own template keeps these tests on the file that ships,
    rather than on a copy that could drift from it.
    """
    template = Path(__file__).parents[1] / TEMPLATE_RELATIVE_PATH
    monkeypatch.setattr(
        launchd,
        "bundled_resource",
        lambda relative: template if Path(relative) == TEMPLATE_RELATIVE_PATH else None,
    )


def read_plist(path: Path) -> dict:
    with path.open("rb") as fh:
        return plistlib.load(fh)


# --------------------------------------------------------------------------
# payload manifest and target checks
# --------------------------------------------------------------------------


def test_read_payload_manifest(tmp_path):
    payload = make_payload(tmp_path)
    assert read_payload_manifest(payload)["version"] == "0.1.0"


def test_manifest_missing(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(InstallError, match="not an orch-hub payload"):
        read_payload_manifest(tmp_path / "empty")


def test_manifest_unreadable(tmp_path):
    payload = make_payload(tmp_path)
    (payload / "runtime.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(InstallError, match="cannot read"):
        read_payload_manifest(payload)


def test_manifest_not_an_object(tmp_path):
    payload = make_payload(tmp_path)
    (payload / "runtime.json").write_text("[]", encoding="utf-8")
    with pytest.raises(InstallError, match="not a JSON object"):
        read_payload_manifest(payload)


def test_manifest_without_version(tmp_path):
    payload = make_payload(tmp_path)
    (payload / "runtime.json").write_text(
        '{"schema": "orch-hub-runtime/1", "executable": "orch-hub"}', encoding="utf-8"
    )
    with pytest.raises(InstallError, match="declares no version"):
        read_payload_manifest(payload)


def test_manifest_without_executable(tmp_path):
    payload = make_payload(tmp_path)
    (payload / "orch-hub").unlink()
    with pytest.raises(InstallError, match="no executable"):
        read_payload_manifest(payload)


def test_manifest_schema_must_match_the_installer(tmp_path):
    payload = make_payload(tmp_path, "0.9.0", schema="orch-hub-runtime/2")

    with pytest.raises(InstallError, match="this installer places"):
        read_payload_manifest(payload)


def test_manifest_must_describe_orch_hub(tmp_path):
    payload = make_payload(tmp_path, "0.9.1", name="something-else")

    with pytest.raises(InstallError, match="not an orch-hub payload"):
        read_payload_manifest(payload)


@pytest.mark.parametrize(
    "version",
    ["/etc", "../escape", "a/b", "..", ".", ".hidden", "0.1.0/"],
)
def test_manifest_version_must_be_one_safe_path_component(tmp_path, version):
    """A version becomes a directory name; joining an unsafe one escapes."""
    payload = tmp_path / "payload-unsafe"
    payload.mkdir()
    (payload / "orch-hub").write_text("#!/bin/sh\n", encoding="utf-8")
    (payload / "runtime.json").write_text(
        json.dumps(
            {
                "schema": "orch-hub-runtime/1",
                "name": "orch-hub",
                "version": version,
                "os": HOST_OS,
                "arch": HOST_ARCH,
                "executable": "orch-hub",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(InstallError, match="version"):
        read_payload_manifest(payload)


def test_install_refuses_a_version_that_escapes_the_prefix(tmp_path):
    """The escape a bad version would perform, proven not to happen."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("mine", encoding="utf-8")
    payload = make_payload(tmp_path, "0.1.0")
    manifest = json.loads((payload / "runtime.json").read_text(encoding="utf-8"))
    manifest["version"] = f"../../{outside.name}"
    (payload / "runtime.json").write_text(json.dumps(manifest), encoding="utf-8")
    prefix = tmp_path / "prefix"

    with pytest.raises(InstallError, match="single path component"):
        install_version(payload, prefix=prefix, link=False)

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert not prefix.exists()


def test_install_refuses_a_mismatched_executable_name(tmp_path):
    """Every installed path spells the command `orch-hub`; so must the payload."""
    payload = make_payload(tmp_path, "0.9.2", executable="orch-hub-api")
    (payload / "orch-hub-api").write_text("#!/bin/sh\n", encoding="utf-8")
    prefix = tmp_path / "prefix"

    with pytest.raises(InstallError, match="must be named"):
        install_version(payload, prefix=prefix, link=False)

    assert not prefix.exists()


def test_installer_and_builder_agree_on_the_payload_manifest():
    """The manifest constants are duplicated on purpose; drift is not."""
    from orch_hub import release_build

    assert install.MANIFEST_SCHEMA == release_build.MANIFEST_SCHEMA
    assert install.MANIFEST_NAME == release_build.MANIFEST_NAME
    assert install.COMMAND_NAME == release_build.EXECUTABLE_NAME


def test_check_target_rejects_other_arch():
    manifest = {"os": "darwin", "arch": "arm64"}
    with pytest.raises(InstallError, match="targets darwin-arm64"):
        check_target(manifest, system="Darwin", machine="x86_64")


def test_check_target_rejects_other_os():
    manifest = {"os": "darwin", "arch": "arm64"}
    with pytest.raises(InstallError, match="this machine is linux-arm64"):
        check_target(manifest, system="Linux", machine="arm64")


def test_check_target_requires_declared_target():
    with pytest.raises(InstallError, match="does not declare an os/arch target"):
        check_target({"version": "0.1.0"}, system="Darwin", machine="arm64")


def test_check_target_rejects_older_macos():
    manifest = {"os": "darwin", "arch": "arm64", "min_macos": "26.0"}
    with pytest.raises(InstallError, match="requires macOS 26.0 or newer"):
        check_target(manifest, system="Darwin", machine="arm64", macos="14.5")


def test_check_target_accepts_newer_macos():
    manifest = {"os": "darwin", "arch": "arm64", "min_macos": "11.0"}
    check_target(manifest, system="Darwin", machine="arm64", macos="26.1")


def test_check_target_skips_unreadable_macos_version():
    """An unparseable version is not evidence of a mismatch, so it must not block."""
    manifest = {"os": "darwin", "arch": "arm64", "min_macos": "unknown"}
    check_target(manifest, system="Darwin", machine="arm64", macos="")


# --------------------------------------------------------------------------
# placing and selecting versions
# --------------------------------------------------------------------------


def test_install_places_and_selects_version(tmp_path):
    payload = make_payload(tmp_path)
    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"

    result = install_version(
        payload, prefix=prefix, bin_dir=bin_dir, environ={"PATH": str(bin_dir)}
    )

    assert result.version == "0.1.0"
    assert result.replaced is False
    assert (prefix / "versions" / "0.1.0" / "orch-hub").is_file()
    assert os.readlink(prefix / "current") == "versions/0.1.0"
    assert result.installation.current == "0.1.0"
    assert result.installation.previous is None
    assert result.link_path == bin_dir / "orch-hub"
    assert result.link_on_path is True
    assert (prefix / "current" / "orch-hub").is_file()


def test_install_preserves_payload_and_symlinks(tmp_path):
    payload = make_payload(tmp_path)
    prefix = tmp_path / "prefix"

    install_version(payload, prefix=prefix, bin_dir=tmp_path / "bin")

    installed = prefix / "versions" / "0.1.0"
    link = installed / "_internal" / "framework" / "Current"
    assert link.is_symlink()
    assert os.readlink(link) == "Versions/1.0"
    assert (installed / "orch-hub").stat().st_mode & 0o111
    # The source is copied, not consumed: a second prefix must remain possible.
    assert (payload / "orch-hub").is_file()


def test_install_second_version_records_previous(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    result = install_version(
        make_payload(tmp_path, "0.2.0"), prefix=prefix, link=False
    )

    assert result.installation.current == "0.2.0"
    assert result.installation.previous == "0.1.0"
    assert result.installation.versions == ("0.1.0", "0.2.0")
    assert read_previous(prefix) == "0.1.0"


def test_reinstalling_the_current_version_keeps_previous(tmp_path):
    """Rollback must survive a re-run of the installer for the same version."""
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    install_version(make_payload(tmp_path, "0.2.0"), prefix=prefix, link=False)

    result = install_version(
        make_payload(tmp_path, "0.2.0"), prefix=prefix, link=False
    )

    assert result.replaced is True
    assert result.installation.current == "0.2.0"
    assert result.installation.previous == "0.1.0"


def test_versions_sort_numerically(tmp_path):
    prefix = tmp_path / "prefix"
    for version in ("0.9.0", "0.10.0", "0.2.0"):
        install_version(make_payload(tmp_path, version), prefix=prefix, link=False)

    assert read_installation(prefix).versions == ("0.2.0", "0.9.0", "0.10.0")


def test_install_refuses_missing_payload(tmp_path):
    with pytest.raises(InstallError, match="payload directory not found"):
        install_version(tmp_path / "nope", prefix=tmp_path / "prefix")


def test_install_refuses_installing_a_version_over_itself(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)

    with pytest.raises(InstallError, match="already installed"):
        install_version(prefix / "versions" / "0.1.0", prefix=prefix, link=False)


def test_install_refuses_replacing_the_running_version(tmp_path, monkeypatch):
    """A frozen runtime must not delete the files it is still executing from."""
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    monkeypatch.setattr(install, "is_frozen", lambda: True)
    monkeypatch.setattr(
        install.sys, "executable", str(prefix / "versions" / "0.1.0" / "orch-hub")
    )

    with pytest.raises(InstallError, match="while running from it"):
        install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)


def test_install_without_link(tmp_path):
    result = install_version(
        make_payload(tmp_path), prefix=tmp_path / "prefix", link=False
    )
    assert result.link_path is None
    assert result.link_on_path is False


def test_install_refuses_to_clobber_a_real_command(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "orch-hub").write_text("someone else's\n", encoding="utf-8")

    with pytest.raises(InstallError, match="not a symlink"):
        install_version(
            make_payload(tmp_path), prefix=tmp_path / "prefix", bin_dir=bin_dir
        )


def test_install_replaces_its_own_command_link(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "orch-hub").symlink_to(tmp_path / "stale")

    result = install_version(
        make_payload(tmp_path), prefix=tmp_path / "prefix", bin_dir=bin_dir
    )

    assert os.readlink(result.link_path) == str(
        tmp_path / "prefix" / "current" / "orch-hub"
    )


def test_link_on_path_is_false_when_absent(tmp_path):
    result = install_version(
        make_payload(tmp_path),
        prefix=tmp_path / "prefix",
        bin_dir=tmp_path / "bin",
        environ={"PATH": "/usr/bin:/bin"},
    )
    assert result.link_on_path is False


def test_select_unknown_version(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path), prefix=prefix, link=False)

    with pytest.raises(InstallError, match="version 9.9.9 is not installed"):
        select_version(prefix, "9.9.9")


def test_select_refuses_a_current_that_is_not_a_symlink(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path), prefix=prefix, link=False)
    (prefix / "current").unlink()
    (prefix / "current").mkdir()

    with pytest.raises(InstallError, match="is not a symlink"):
        select_version(prefix, "0.1.0")


def test_read_current_ignores_a_foreign_link(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "current").symlink_to(tmp_path / "elsewhere")

    assert read_current(prefix) is None


def test_read_current_ignores_a_dangling_link(tmp_path):
    prefix = tmp_path / "prefix"
    (prefix / "versions").mkdir(parents=True)
    (prefix / "current").symlink_to(Path("versions") / "0.1.0")

    assert read_current(prefix) is None


def test_read_previous_ignores_a_removed_version(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    install_version(make_payload(tmp_path, "0.2.0"), prefix=prefix, link=False)
    shutil.rmtree(prefix / "versions" / "0.1.0")

    assert read_previous(prefix) is None


def test_read_installation_of_an_empty_prefix(tmp_path):
    installation = read_installation(tmp_path / "nothing")
    assert installation.versions == ()
    assert installation.current is None
    assert installation.to_dict()["prefix"] == str(tmp_path / "nothing")


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------


def setup_from(tmp_path, prefix, runner, *, health="ok", **overrides):
    return setup_service(
        SetupOptions(
            prefix=prefix,
            mandrel_path=overrides.pop("mandrel_path", make_mandrel(tmp_path)),
            plist_path=overrides.pop("plist_path", tmp_path / PLIST_NAME),
            log_dir=overrides.pop("log_dir", tmp_path / "logs"),
            data_dir=overrides.pop("data_dir", tmp_path / "data"),
            **overrides,
        ),
        runner=runner,
        environ={},
        token_generator=lambda: "generated-token",
        sleeper=lambda seconds: None,
        health_checker=lambda host, port: health,
    )


def test_setup_points_launchd_at_the_selected_version(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path), prefix=prefix, link=False)
    runner = FakeLaunchctl()

    result = setup_from(tmp_path, prefix, runner, port=9876)

    plist = read_plist(tmp_path / PLIST_NAME)
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == [
        str(prefix / "current" / "orch-hub"),
        "serve",
    ]
    assert plist["EnvironmentVariables"][TOKEN_ENV] == "generated-token"
    assert plist["EnvironmentVariables"][PORT_ENV] == "9876"
    assert "REPLACE-" not in (tmp_path / PLIST_NAME).read_text(encoding="utf-8")
    assert result.generated_token is True
    assert result.health == "ok"
    # No package install: a compiled release has no checkout and no venv.
    assert not any(call[0].endswith("pip") for call in runner.calls)
    assert "bootstrap" in runner.verbs()


def test_setup_requires_a_selected_version(tmp_path):
    with pytest.raises(InstallError, match="no orch-hub version is selected"):
        setup_from(tmp_path, tmp_path / "prefix", FakeLaunchctl())


def test_setup_requires_a_real_mandrel_checkout(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path), prefix=prefix, link=False)
    empty = tmp_path / "not-mandrel"
    empty.mkdir()

    with pytest.raises(LaunchdError, match="does not look like a mandrel checkout"):
        setup_from(tmp_path, prefix, FakeLaunchctl(), mandrel_path=empty)

    assert not (tmp_path / PLIST_NAME).exists()


def test_setup_requires_the_mandrel_path_to_exist(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path), prefix=prefix, link=False)

    with pytest.raises(LaunchdError, match="mandrel checkout not found"):
        setup_from(tmp_path, prefix, FakeLaunchctl(), mandrel_path=tmp_path / "gone")


def test_setup_reports_an_unhealthy_service(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path), prefix=prefix, link=False)

    result = setup_from(
        tmp_path, prefix, FakeLaunchctl(), health="unreachable: refused"
    )

    assert result.health == "unreachable: refused"


def test_setup_preserves_installed_configuration(tmp_path):
    """A second setup with no options keeps the token and the Mandrel path."""
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path), prefix=prefix, link=False)
    mandrel = make_mandrel(tmp_path)
    setup_from(tmp_path, prefix, FakeLaunchctl(), mandrel_path=mandrel, port=9876)

    result = setup_service(
        SetupOptions(prefix=prefix, plist_path=tmp_path / PLIST_NAME),
        runner=FakeLaunchctl(),
        environ={},
        token_generator=lambda: "second-token",
        sleeper=lambda seconds: None,
        health_checker=lambda host, port: "ok",
    )

    plist = read_plist(tmp_path / PLIST_NAME)
    assert result.generated_token is False
    assert plist["EnvironmentVariables"][TOKEN_ENV] == "generated-token"
    assert plist["EnvironmentVariables"][MANDREL_PATH_ENV] == str(mandrel)
    assert plist["EnvironmentVariables"][PORT_ENV] == "9876"


# --------------------------------------------------------------------------
# upgrade and rollback
# --------------------------------------------------------------------------


def test_upgrade_installs_and_restarts(tmp_path):
    prefix = tmp_path / "prefix"
    plist_path = tmp_path / PLIST_NAME
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    setup_from(tmp_path, prefix, FakeLaunchctl(), plist_path=plist_path)
    runner = FakeLaunchctl()

    result = upgrade(
        make_payload(tmp_path, "0.2.0"),
        prefix=prefix,
        bin_dir=tmp_path / "bin",
        plist_path=plist_path,
        runner=runner,
        environ={},
        sleeper=lambda seconds: None,
        health_checker=lambda host, port: "ok",
    )

    assert result.previous_version == "0.1.0"
    assert result.install.version == "0.2.0"
    assert read_current(prefix) == "0.2.0"
    assert read_previous(prefix) == "0.1.0"
    plist = read_plist(plist_path)
    # The plist still names `current`, so the upgrade changed no configuration.
    assert plist["ProgramArguments"][0] == str(prefix / "current" / "orch-hub")
    assert plist["EnvironmentVariables"][TOKEN_ENV] == "generated-token"
    assert "bootstrap" in runner.verbs()


def test_failed_upgrade_leaves_the_previous_version_selectable(tmp_path):
    prefix = tmp_path / "prefix"
    plist_path = tmp_path / PLIST_NAME
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    setup_from(tmp_path, prefix, FakeLaunchctl(), plist_path=plist_path)

    def refuse_bootstrap(args, **kwargs):
        argv = tuple(str(arg) for arg in args)
        if argv[:2] == ("launchctl", "bootstrap"):
            return CommandResult(argv, 1, "", "Load failed")
        return CommandResult(argv, 0, "ok", "")

    with pytest.raises(InstallError, match="rollback --to 0.1.0"):
        upgrade(
            make_payload(tmp_path, "0.2.0"),
            prefix=prefix,
            bin_dir=tmp_path / "bin",
            plist_path=plist_path,
            runner=refuse_bootstrap,
            environ={},
            sleeper=lambda seconds: None,
            health_checker=lambda host, port: "ok",
        )

    assert (prefix / "versions" / "0.1.0").is_dir()
    assert read_previous(prefix) == "0.1.0"


def test_rollback_selects_the_previous_version(tmp_path):
    prefix = tmp_path / "prefix"
    plist_path = tmp_path / PLIST_NAME
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    install_version(make_payload(tmp_path, "0.2.0"), prefix=prefix, link=False)
    setup_from(tmp_path, prefix, FakeLaunchctl(), plist_path=plist_path, port=9876)
    runner = FakeLaunchctl()

    result = rollback(
        prefix=prefix,
        plist_path=plist_path,
        runner=runner,
        sleeper=lambda seconds: None,
        health_checker=lambda host, port: "ok",
    )

    assert (result.from_version, result.to_version) == ("0.2.0", "0.1.0")
    assert read_current(prefix) == "0.1.0"
    assert read_previous(prefix) == "0.2.0"
    assert result.health == "ok"
    assert runner.verbs() == ["bootout", "print", "bootstrap"]


def test_rollback_probes_the_configured_bind(tmp_path):
    """Health is checked where the installed service actually listens."""
    prefix = tmp_path / "prefix"
    plist_path = tmp_path / PLIST_NAME
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    install_version(make_payload(tmp_path, "0.2.0"), prefix=prefix, link=False)
    setup_from(
        tmp_path, prefix, FakeLaunchctl(), plist_path=plist_path,
        host="127.0.0.9", port=9876,
    )
    probes: list[tuple[str, int]] = []

    rollback(
        prefix=prefix,
        plist_path=plist_path,
        runner=FakeLaunchctl(),
        sleeper=lambda seconds: None,
        health_checker=lambda host, port: probes.append((host, port)) or "ok",
    )

    assert probes == [("127.0.0.9", 9876)]


def test_rollback_refuses_an_unreadable_service_configuration(tmp_path):
    """Health is probed at the plist's bind, so an unreadable plist is fatal.

    Defaulting would restart the service and then probe 127.0.0.1:8765 — quite
    possibly some other listener — and report its answer as this service's
    health. The refusal happens before the selection moves, so the version that
    is running stays the version that is selected.
    """
    prefix = tmp_path / "prefix"
    plist_path = tmp_path / PLIST_NAME
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    install_version(make_payload(tmp_path, "0.2.0"), prefix=prefix, link=False)
    setup_from(tmp_path, prefix, FakeLaunchctl(), plist_path=plist_path)
    plist_path.write_text("<plist>truncated", encoding="utf-8")
    runner = FakeLaunchctl()

    with pytest.raises(LaunchdError, match="cannot read the installed launchd plist"):
        rollback(prefix=prefix, plist_path=plist_path, runner=runner)

    assert read_current(prefix) == "0.2.0"
    assert runner.calls == []


def test_purge_refuses_an_unreadable_service_configuration(tmp_path):
    """A purge deletes what the plist names; an unreadable one names nothing."""
    prefix = tmp_path / "prefix"
    plist_path = tmp_path / PLIST_NAME
    data_dir = tmp_path / "data"
    install_version(make_payload(tmp_path), prefix=prefix, link=False)
    setup_from(tmp_path, prefix, FakeLaunchctl(), plist_path=plist_path)
    (data_dir / "runs").mkdir(parents=True, exist_ok=True)
    plist_path.write_text("<plist>truncated", encoding="utf-8")

    with pytest.raises(LaunchdError, match="cannot read the installed launchd plist"):
        uninstall(
            prefix=prefix,
            bin_dir=tmp_path / "bin",
            plist_path=plist_path,
            purge_data=True,
            runner=FakeLaunchctl(),
        )

    assert (data_dir / "runs").is_dir()
    assert (prefix / "current" / "orch-hub").is_file()


def test_rollback_without_a_previous_version(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)

    with pytest.raises(InstallError, match="installed: 0.1.0"):
        rollback(prefix=prefix, runner=FakeLaunchctl())


def test_rollback_to_an_unknown_version(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)

    with pytest.raises(InstallError, match="version 9.9.9 is not installed"):
        rollback(version="9.9.9", prefix=prefix, runner=FakeLaunchctl())


def test_rollback_to_the_current_version(tmp_path):
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)

    with pytest.raises(InstallError, match="already selected"):
        rollback(version="0.1.0", prefix=prefix, runner=FakeLaunchctl())


# --------------------------------------------------------------------------
# uninstall
# --------------------------------------------------------------------------


def test_uninstall_removes_the_service_and_runtime_but_keeps_data(tmp_path):
    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"
    plist_path = tmp_path / PLIST_NAME
    data_dir = tmp_path / "data"
    install_version(make_payload(tmp_path), prefix=prefix, bin_dir=bin_dir)
    setup_from(tmp_path, prefix, FakeLaunchctl(), plist_path=plist_path)
    data_dir.mkdir(exist_ok=True)
    (data_dir / "runs").mkdir()
    runner = FakeLaunchctl()

    result = uninstall(
        prefix=prefix, bin_dir=bin_dir, plist_path=plist_path, runner=runner
    )

    assert result.prefix_removed is True
    assert result.plist_removed is True
    assert result.link_removed is True
    assert result.data_removed is False
    assert not prefix.exists()
    assert not plist_path.exists()
    assert not (bin_dir / "orch-hub").is_symlink()
    assert (data_dir / "runs").is_dir()
    assert runner.verbs() == ["bootout"]


def test_uninstall_purges_the_configured_data_dir(tmp_path):
    prefix = tmp_path / "prefix"
    plist_path = tmp_path / PLIST_NAME
    data_dir = tmp_path / "data"
    install_version(make_payload(tmp_path), prefix=prefix, link=False)
    setup_from(tmp_path, prefix, FakeLaunchctl(), plist_path=plist_path)
    data_dir.mkdir(exist_ok=True)
    (data_dir / "runs").mkdir()

    result = uninstall(
        prefix=prefix,
        bin_dir=tmp_path / "bin",
        plist_path=plist_path,
        purge_data=True,
        runner=FakeLaunchctl(),
    )

    assert result.data_dir == data_dir
    assert result.data_removed is True
    assert not data_dir.exists()


def test_uninstall_reads_the_purge_target_before_removing_the_plist(tmp_path):
    """The data dir is only knowable from the plist, which uninstall deletes."""
    prefix = tmp_path / "prefix"
    plist_path = tmp_path / PLIST_NAME
    install_version(make_payload(tmp_path), prefix=prefix, link=False)
    setup_from(
        tmp_path, prefix, FakeLaunchctl(), plist_path=plist_path,
        data_dir=tmp_path / "elsewhere",
    )
    (tmp_path / "elsewhere").mkdir(exist_ok=True)

    result = uninstall(
        prefix=prefix,
        bin_dir=tmp_path / "bin",
        plist_path=plist_path,
        purge_data=True,
        runner=FakeLaunchctl(),
    )

    assert result.data_dir == tmp_path / "elsewhere"
    assert not (tmp_path / "elsewhere").exists()


def test_uninstall_leaves_a_foreign_command_alone(tmp_path):
    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"
    install_version(make_payload(tmp_path), prefix=prefix, link=False)
    bin_dir.mkdir()
    (bin_dir / "orch-hub").symlink_to(tmp_path / "another-install" / "orch-hub")

    result = uninstall(
        prefix=prefix,
        bin_dir=bin_dir,
        plist_path=tmp_path / PLIST_NAME,
        runner=FakeLaunchctl(),
    )

    assert result.link_removed is False
    assert (bin_dir / "orch-hub").is_symlink()


def test_uninstall_aborts_when_the_service_cannot_be_stopped(tmp_path):
    """A live service that will not unload must keep its plist and its files.

    ``stop_service`` answers "there was no job" without raising, so a raised
    error means the service is there and still running. Deleting its plist, its
    data, and its executable around it would leave a live process with no
    configuration and KeepAlive unable to bring it back.
    """
    prefix = tmp_path / "prefix"
    bin_dir = tmp_path / "bin"
    plist_path = tmp_path / PLIST_NAME
    data_dir = tmp_path / "data"
    install_version(make_payload(tmp_path), prefix=prefix, bin_dir=bin_dir)
    setup_from(tmp_path, prefix, FakeLaunchctl(), plist_path=plist_path)
    (data_dir / "runs").mkdir(parents=True, exist_ok=True)

    def refuse(args, **kwargs):
        argv = tuple(str(arg) for arg in args)
        return CommandResult(argv, 1, "", "Operation not permitted")

    with pytest.raises(InstallError, match="nothing was removed"):
        uninstall(
            prefix=prefix,
            bin_dir=bin_dir,
            plist_path=plist_path,
            purge_data=True,
            runner=refuse,
        )

    assert plist_path.is_file()
    assert (prefix / "current" / "orch-hub").is_file()
    assert (bin_dir / "orch-hub").is_symlink()
    assert (data_dir / "runs").is_dir()


def test_uninstall_refuses_a_prefix_it_does_not_own(tmp_path):
    """`--prefix ~` must not become a recursive delete of unrelated files."""
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "notes.txt").write_text("keep me", encoding="utf-8")
    plist_path = tmp_path / PLIST_NAME
    plist_path.write_text("installed", encoding="utf-8")
    runner = FakeLaunchctl()

    with pytest.raises(InstallError, match="not an orch-hub install prefix"):
        uninstall(
            prefix=home,
            bin_dir=tmp_path / "bin",
            plist_path=plist_path,
            runner=runner,
        )

    assert (home / "Documents" / "notes.txt").read_text(encoding="utf-8") == "keep me"
    # Refused before every side effect, launchctl included.
    assert plist_path.read_text(encoding="utf-8") == "installed"
    assert runner.calls == []


def test_uninstall_refuses_a_versions_dir_full_of_foreign_directories(tmp_path):
    prefix = tmp_path / "prefix"
    (prefix / "versions" / "0.1.0" / "src").mkdir(parents=True)

    with pytest.raises(InstallError, match="not an installed orch-hub version"):
        uninstall(
            prefix=prefix,
            bin_dir=tmp_path / "bin",
            plist_path=tmp_path / PLIST_NAME,
            runner=FakeLaunchctl(),
        )

    assert (prefix / "versions" / "0.1.0" / "src").is_dir()


def test_uninstall_refuses_an_impostor_prefix_with_an_arbitrary_manifest(tmp_path):
    """A file named `runtime.json` proves nothing — many runtimes ship one.

    The prefix here is shaped exactly like an installation from the outside: a
    lone ``versions/`` directory whose child carries a ``runtime.json``. Only
    reading that file, and matching what it declares to where it sits, tells the
    two apart — and getting it wrong deletes somebody's project.
    """
    prefix = tmp_path / "prefix"
    project = prefix / "versions" / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.js").write_text("console.log(1)\n", encoding="utf-8")
    (project / "runtime.json").write_text(
        json.dumps({"runtime": "node", "version": "22.3.0"}), encoding="utf-8"
    )
    plist_path = tmp_path / PLIST_NAME
    plist_path.write_text("installed", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "orch-hub").symlink_to(prefix / "current" / "orch-hub")
    runner = FakeLaunchctl()

    with pytest.raises(InstallError, match="not an installed orch-hub version"):
        uninstall(
            prefix=prefix,
            bin_dir=bin_dir,
            plist_path=plist_path,
            runner=runner,
        )

    kept = (project / "src" / "main.js").read_text(encoding="utf-8")
    assert kept == "console.log(1)\n"
    assert json.loads((project / "runtime.json").read_text(encoding="utf-8")) == {
        "runtime": "node",
        "version": "22.3.0",
    }
    # Refused before every side effect, launchctl and the command link included.
    assert plist_path.read_text(encoding="utf-8") == "installed"
    assert (bin_dir / "orch-hub").is_symlink()
    assert runner.calls == []


def test_uninstall_refuses_a_version_filed_under_another_name(tmp_path):
    """The manifest has to describe the directory it is in, not just exist."""
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    (prefix / "versions" / "0.1.0").rename(prefix / "versions" / "9.9.9")
    runner = FakeLaunchctl()

    with pytest.raises(InstallError, match="filed as '9.9.9'"):
        uninstall(
            prefix=prefix,
            bin_dir=tmp_path / "bin",
            plist_path=tmp_path / PLIST_NAME,
            runner=runner,
        )

    assert (prefix / "versions" / "9.9.9" / "orch-hub").is_file()
    assert runner.calls == []


def test_uninstall_refuses_a_prefix_with_no_installed_version(tmp_path):
    """An empty `versions/` used to pass by having nothing to object to."""
    prefix = tmp_path / "prefix"
    (prefix / "versions").mkdir(parents=True)
    runner = FakeLaunchctl()

    with pytest.raises(InstallError, match="holds no installed version"):
        uninstall(
            prefix=prefix,
            bin_dir=tmp_path / "bin",
            plist_path=tmp_path / PLIST_NAME,
            runner=runner,
        )

    assert (prefix / "versions").is_dir()
    assert runner.calls == []


def test_uninstall_refuses_a_prefix_holding_only_install_debris(tmp_path):
    """Scratch is tolerated beside a version, never accepted *as* one."""
    prefix = tmp_path / "prefix"
    debris = prefix / "versions" / f"0.1.0{INCOMING_SUFFIX}"
    debris.mkdir(parents=True)
    (debris / "half-copied").write_text("x", encoding="utf-8")

    with pytest.raises(InstallError, match="holds no installed version"):
        uninstall(
            prefix=prefix,
            bin_dir=tmp_path / "bin",
            plist_path=tmp_path / PLIST_NAME,
            runner=FakeLaunchctl(),
        )

    assert (debris / "half-copied").is_file()


def test_uninstall_tolerates_debris_from_an_interrupted_install(tmp_path):
    """A kill mid-copy leaves scratch behind; that is ours, not a foreign tree."""
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    debris = prefix / "versions" / f"0.2.0{INCOMING_SUFFIX}"
    debris.mkdir()
    (debris / "half-copied").write_text("x", encoding="utf-8")

    result = uninstall(
        prefix=prefix,
        bin_dir=tmp_path / "bin",
        plist_path=tmp_path / PLIST_NAME,
        runner=FakeLaunchctl(),
    )

    assert result.prefix_removed is True
    assert not prefix.exists()


def test_uninstall_reports_an_unreadable_prefix_as_an_error(tmp_path):
    """Not a traceback: the CLI contract is `error: ...` and a non-zero exit."""
    prefix = tmp_path / "prefix"
    install_version(make_payload(tmp_path), prefix=prefix, link=False)
    prefix.chmod(0o000)
    try:
        with pytest.raises(InstallError, match="cannot read"):
            uninstall(
                prefix=prefix,
                bin_dir=tmp_path / "bin",
                plist_path=tmp_path / PLIST_NAME,
                runner=FakeLaunchctl(),
            )
    finally:
        prefix.chmod(0o755)


def test_uninstall_refuses_a_symlinked_prefix(tmp_path):
    real = tmp_path / "real"
    install_version(make_payload(tmp_path), prefix=real, link=False)
    linked = tmp_path / "linked"
    linked.symlink_to(real)

    with pytest.raises(InstallError, match="is a symlink"):
        uninstall(
            prefix=linked,
            bin_dir=tmp_path / "bin",
            plist_path=tmp_path / PLIST_NAME,
            runner=FakeLaunchctl(),
        )

    assert (real / "current" / "orch-hub").is_file()


def test_uninstall_of_nothing_is_not_an_error(tmp_path):
    result = uninstall(
        prefix=tmp_path / "prefix",
        bin_dir=tmp_path / "bin",
        plist_path=tmp_path / PLIST_NAME,
        runner=FakeLaunchctl(),
    )

    assert result.prefix_removed is False
    assert result.plist_removed is False
    assert result.link_path is None


def test_rollback_reports_a_restart_failure_after_selecting(tmp_path):
    """The selection stands; the operator is told the service is down."""
    prefix = tmp_path / "prefix"
    plist_path = tmp_path / PLIST_NAME
    install_version(make_payload(tmp_path, "0.1.0"), prefix=prefix, link=False)
    install_version(make_payload(tmp_path, "0.2.0"), prefix=prefix, link=False)
    setup_from(tmp_path, prefix, FakeLaunchctl(), plist_path=plist_path)

    def refuse_bootstrap(args, **kwargs):
        argv = tuple(str(arg) for arg in args)
        if argv[:2] == ("launchctl", "bootstrap"):
            return CommandResult(argv, 1, "", "Load failed")
        if argv[:2] == ("launchctl", "print"):
            return CommandResult(argv, 113, "", "Could not find service")
        return CommandResult(argv, 113, "", "No such process")

    with pytest.raises(InstallError, match="orch-hub launchd start"):
        rollback(prefix=prefix, plist_path=plist_path, runner=refuse_bootstrap)

    assert read_current(prefix) == "0.1.0"


def test_read_current_ignores_a_link_into_another_prefix(tmp_path):
    """An absolute link can name another prefix's identically-numbered version."""
    other = tmp_path / "other"
    (other / "versions" / "0.1.0").mkdir(parents=True)
    prefix = tmp_path / "prefix"
    (prefix / "versions" / "0.1.0").mkdir(parents=True)
    (prefix / "current").symlink_to(other / "versions" / "0.1.0")

    assert read_current(prefix) is None


def test_install_refuses_a_self_install_through_a_symlinked_prefix(tmp_path):
    """The same directory reached by two spellings is still the same directory."""
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real)
    install_version(make_payload(tmp_path, "0.1.0"), prefix=real, link=False)

    # Source spelled through the real path, prefix spelled through the link: a
    # comparison of unresolved paths sees two directories and copies one onto
    # the other.
    with pytest.raises(InstallError, match="already installed"):
        install_version(real / "versions" / "0.1.0", prefix=linked, link=False)
