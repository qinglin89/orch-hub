"""Tests for the release build driver's decisions.

Running a real freeze takes tens of seconds and needs a network install, so the
frozen artifact is covered by the opt-in smoke test (tests/test_frozen_runtime.py).
What is pinned here is everything the driver decides *before* and *after* the
freezer runs: what it refuses to build, what it installs, what the payload is
called, and what it treats as forbidden in the payload it produced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orch_hub import release_build
from orch_hub.release_audit import MAINTAINER_PATH, SECRET_MATERIAL
from orch_hub.release_build import (
    TARGET,
    BuildError,
    BuildOptions,
    build_requirements,
    check_host,
    default_literals,
    read_project,
    read_version,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def options(tmp_path: Path, **overrides) -> BuildOptions:
    defaults = dict(
        checkout=tmp_path / "checkout",
        out_dir=tmp_path / "dist",
        work_dir=tmp_path / "work",
        venv_dir=tmp_path / "work" / "venv",
    )
    defaults.update(overrides)
    return BuildOptions(**defaults)


def test_reads_the_checkout_version_without_importing_the_package(tmp_path: Path):
    # The driver runs before the build environment exists, so it cannot import
    # orch_hub to ask for its version.
    package = tmp_path / "orch_hub"
    package.mkdir()
    (package / "__init__.py").write_text('"""doc"""\n\n__version__ = "9.9.9"\n', "utf-8")
    assert read_version(tmp_path) == "9.9.9"


def test_missing_version_declaration_is_a_build_error(tmp_path: Path):
    package = tmp_path / "orch_hub"
    package.mkdir()
    (package / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(BuildError, match="__version__"):
        read_version(tmp_path)


def test_reads_this_checkout_version():
    from orch_hub import __version__

    assert read_version(REPO_ROOT) == __version__


def test_build_requirements_are_runtime_deps_plus_the_pinned_toolchain():
    requirements = build_requirements(read_project(REPO_ROOT))
    assert any(entry.startswith("fastapi") for entry in requirements)
    assert any(entry.startswith("uvicorn") for entry in requirements)
    # The freezer decides the payload's layout and bootloader, so it is pinned
    # exactly rather than floated.
    assert any("==" in entry and "pyinstaller" in entry for entry in requirements)


def test_build_requirements_refuse_an_undeclared_toolchain():
    with pytest.raises(BuildError, match="release"):
        build_requirements({"project": {"dependencies": ["fastapi"]}})


def test_malformed_pyproject_is_a_build_error(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    with pytest.raises(BuildError, match="malformed"):
        read_project(tmp_path)


def test_missing_pyproject_is_a_build_error(tmp_path: Path):
    with pytest.raises(BuildError, match="cannot read"):
        read_project(tmp_path)


def test_host_matching_the_target_is_accepted():
    assert check_host(system="Darwin", machine="arm64") is None


@pytest.mark.parametrize(
    "system,machine", [("Linux", "arm64"), ("Darwin", "x86_64"), ("Windows", "AMD64")]
)
def test_other_hosts_are_refused_before_a_build_starts(system: str, machine: str):
    # A freezer bundles the running interpreter, so a build is always for the
    # host; producing a payload labelled darwin-arm64 elsewhere would lie.
    with pytest.raises(BuildError, match="no cross-building"):
        check_host(system=system, machine=machine)


def test_payload_is_named_by_target(tmp_path: Path):
    assert options(tmp_path).payload_dir.name == f"orch-hub-{TARGET}"
    assert TARGET == "darwin-arm64"


def test_default_literals_cover_the_checkout_and_the_builder_home(tmp_path: Path):
    literals = default_literals(tmp_path / "checkout", {}, home=tmp_path / "home")
    assert [literal.kind for literal in literals] == [MAINTAINER_PATH, MAINTAINER_PATH]
    assert {literal.value for literal in literals} == {
        str(tmp_path / "checkout"),
        str(tmp_path / "home"),
    }


def test_default_literals_include_exported_service_secrets(tmp_path: Path):
    environ = {"ORCH_HUB_TOKEN": "live-token", "ORCH_HUB_BARK_URL": "https://bark/key"}
    literals = default_literals(tmp_path, environ, home=tmp_path / "home")
    secrets = [literal for literal in literals if literal.kind == SECRET_MATERIAL]
    assert {literal.value for literal in secrets} == {"live-token", "https://bark/key"}
    # Labels are what findings print, so they must not restate the value.
    assert {literal.label for literal in secrets} == {"bearer token", "bark endpoint"}


def test_blank_service_secrets_are_not_audited_for(tmp_path: Path):
    # An empty literal would match every file in the payload.
    environ = {"ORCH_HUB_TOKEN": "   ", "ORCH_HUB_BARK_URL": ""}
    literals = default_literals(tmp_path, environ, home=tmp_path / "home")
    assert all(literal.kind == MAINTAINER_PATH for literal in literals)


def test_required_members_track_the_modules_that_own_them():
    # The bundle destinations are imported from the server and launchd modules,
    # so moving an asset cannot silently drop it from the payload.
    from orch_hub import launchd, resources

    assert release_build.WEB_RELATIVE_PATH == resources.WEB_RELATIVE_PATH
    assert release_build.LAUNCHD_TEMPLATE_RELATIVE_PATH == launchd.TEMPLATE_RELATIVE_PATH
    assert (
        f"_internal/{launchd.TEMPLATE_RELATIVE_PATH.as_posix()}"
        in release_build.REQUIRED_MEMBERS
    )


def test_build_only_modules_are_kept_out_of_the_payload():
    # A release has no use for the tooling that produces and checks it — and the
    # audit's list of forbidden material should not ship inside the artifact.
    assert set(release_build.BUILD_ONLY_MODULES) == {
        "orch_hub.release_build",
        "orch_hub.release_audit",
        "orch_hub.macho",
    }


def test_first_party_modules_cover_the_whole_package(tmp_path: Path):
    from orch_hub.release_build import first_party_modules

    modules = first_party_modules(REPO_ROOT)
    on_disk = {
        f"orch_hub.{path.stem}"
        for path in (REPO_ROOT / "orch_hub").glob("*.py")
        if not path.stem.startswith("__")
    }
    # The payload carries the whole first-party package except its own tooling —
    # a rule simple enough to check, unlike "whatever the freezer can reach".
    assert set(modules) == on_disk - set(release_build.BUILD_ONLY_MODULES)
    # Modules reached only through a lazy import must be named too, or they are
    # silently absent from a runtime that otherwise starts fine.
    assert "orch_hub.report_catalog" in modules
    assert "orch_hub.statusline" in modules


def test_first_party_modules_exclude_the_package_and_entry_files():
    from orch_hub.release_build import first_party_modules

    modules = first_party_modules(REPO_ROOT)
    assert "orch_hub.__init__" not in modules
    assert "orch_hub.__main__" not in modules


def test_first_party_modules_refuse_a_checkout_without_the_package(tmp_path: Path):
    with pytest.raises(BuildError, match="no orch_hub package"):
        release_build.first_party_modules(tmp_path)


def test_first_party_modules_refuse_an_empty_package(tmp_path: Path):
    # An empty result would freeze an executable with no application in it.
    (tmp_path / "orch_hub").mkdir()
    with pytest.raises(BuildError, match="no first-party modules"):
        release_build.first_party_modules(tmp_path)


def test_reusing_a_build_environment_requires_one_to_exist(tmp_path: Path):
    with pytest.raises(BuildError, match="no interpreter"):
        release_build.ensure_build_venv(
            options(tmp_path, install=False), read_project(REPO_ROOT)
        )


def test_entry_point_is_the_module_python_m_runs():
    # One dispatch for both runtimes: whatever `python -m orch_hub` executes is
    # what the freezer compiles as the program's __main__.
    assert release_build.ENTRY_RELATIVE_PATH == Path("orch_hub") / "__main__.py"
    assert (REPO_ROOT / release_build.ENTRY_RELATIVE_PATH).is_file()
