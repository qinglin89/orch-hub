"""Tests for the release build driver's decisions.

Running a real freeze takes tens of seconds and needs a network install, so the
frozen artifact is covered by the opt-in smoke test (tests/test_frozen_runtime.py).
What is pinned here is everything the driver decides *before* and *after* the
freezer runs: what it refuses to build, what it installs, what the payload is
called, and what it treats as forbidden in the payload it produced.
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from unittest import mock

import pytest

from orch_hub import release_build
from orch_hub.macho import PayloadFacts
from orch_hub.release_audit import MAINTAINER_PATH, SECRET_MATERIAL
from orch_hub.release_build import (
    TARGET,
    BuildError,
    BuildOptions,
    build_requirements,
    check_host,
    check_min_macos,
    default_literals,
    read_project,
    read_version,
)
from orch_hub.release_python import PinnedInterpreter, pinned_interpreter

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


def test_a_floor_at_or_below_the_promise_is_accepted():
    pin = pinned_interpreter(TARGET)
    check_min_macos(pin.min_macos, pin)
    check_min_macos("10.15", pin)


def test_a_floor_above_the_promise_names_both_and_the_way_out():
    pin = pinned_interpreter(TARGET)
    with pytest.raises(BuildError) as raised:
        check_min_macos("26.0", pin)
    message = str(raised.value)
    assert "requires macOS 26.0" in message
    assert f"supports macOS {pin.min_macos}" in message
    assert "--allow-unsupported-macos" in message


def test_a_payload_that_declares_no_floor_is_refused():
    # The installer skips its macOS gate when the payload declares nothing, so
    # an unreadable floor removes the check from every machine that installs it.
    with pytest.raises(BuildError, match="declares no minimum macOS"):
        check_min_macos(None, pinned_interpreter(TARGET))


def test_the_floor_is_compared_as_a_version_not_as_text():
    # "11.0" sorts before "9.0" as text, so a string comparison would publish a
    # payload that needs a macOS eleven releases newer than the promise.
    older = PinnedInterpreter(
        target="darwin-arm64",
        python_version="3.14.7",
        url="https://example.invalid/cpython.tar.gz",
        sha256="0" * 64,
        min_macos="9.0",
    )
    with pytest.raises(BuildError, match="requires macOS 11.0"):
        check_min_macos("11.0", older)


def test_an_unsupported_floor_can_be_built_deliberately(capsys):
    pin = pinned_interpreter(TARGET)
    check_min_macos("26.0", pin, allow=True)
    assert "warning:" in capsys.readouterr().err


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
        "orch_hub.release_package",
        "orch_hub.release_python",
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


def test_reusing_a_build_environment_requires_one_to_exist(
    tmp_path: Path, monkeypatch
):
    # The refusal comes before the pin is provisioned, and this test says so
    # rather than only that it is refused: --skip-install means "build from what
    # is already here", so fetching 75 MB of interpreter first replaces an
    # actionable error with the network's, and makes an offline build fail for
    # the wrong reason.
    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("provisioned an interpreter for a build that reuses one")

    monkeypatch.setattr(release_build, "ensure_interpreter", unexpected_fetch)

    with pytest.raises(BuildError, match="no interpreter"):
        release_build.ensure_build_venv(
            options(tmp_path, install=False), read_project(REPO_ROOT)
        )

    assert not (tmp_path / "work" / release_build.INTERPRETER_DIR).exists()


class FakeRuns:
    """Stand-in for the driver's subprocess runner.

    Records what a build would have executed, and answers the interpreter
    identity probe — the only command the environment logic reads output from.
    """

    def __init__(self, identity: str = "/base/bin/python3.14\n3.14.4\n"):
        self.identity = identity
        self.commands: list[list[str]] = []
        self.captured = 0

    def __call__(self, argv, what, *, cwd=None, capture=False):
        self.commands.append(list(argv))
        if capture:
            self.captured += 1
            return self.identity
        return None

    def created_a_venv(self) -> bool:
        return any("venv" in command for command in self.commands)


def fake_venv(
    venv_dir: Path,
    *,
    executable: str,
    version: str = "3.14.4",
    version_key: str = "version",
) -> Path:
    """A build environment that records the interpreter it was created from."""
    (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
    python = venv_dir / "bin" / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    (venv_dir / "pyvenv.cfg").write_text(
        f"home = /base/bin\n{version_key} = {version}\nexecutable = {executable}\n",
        encoding="utf-8",
    )
    (venv_dir / "marker").write_text("previous environment", encoding="utf-8")
    return python


def test_a_pinned_interpreter_replaces_an_environment_built_from_another(
    tmp_path: Path, monkeypatch
):
    # The bundled interpreter sets the payload's minimum macOS, so a pin that
    # silently reused the previous environment would report one interpreter and
    # ship another — visible only to whoever cannot run the release.
    venv_dir = tmp_path / "work" / "venv"
    fake_venv(venv_dir, executable="/old/bin/python3.14")
    runs = FakeRuns("/new/bin/python3.14\n3.14.4\n")
    monkeypatch.setattr(release_build, "_run", runs)

    release_build.ensure_build_venv(
        options(tmp_path, venv_dir=venv_dir, python="/new/bin/python3.14"),
        read_project(REPO_ROOT),
    )
    assert not (venv_dir / "marker").exists()  # rebuilt, not reused
    assert ["/new/bin/python3.14", "-m", "venv", str(venv_dir)] in runs.commands


def test_an_environment_built_from_the_pinned_interpreter_is_reused(
    tmp_path: Path, monkeypatch
):
    venv_dir = tmp_path / "work" / "venv"
    fake_venv(venv_dir, executable="/pinned/bin/python3.14")
    runs = FakeRuns("/pinned/bin/python3.14\n3.14.4\n")
    monkeypatch.setattr(release_build, "_run", runs)

    release_build.ensure_build_venv(
        options(tmp_path, venv_dir=venv_dir, python="/pinned/bin/python3.14"),
        read_project(REPO_ROOT),
    )
    assert (venv_dir / "marker").exists()
    assert not runs.created_a_venv()


def test_an_interpreter_upgraded_in_place_replaces_the_environment(
    tmp_path: Path, monkeypatch
):
    # Same path, different interpreter: the recorded version is the other half
    # of the identity.
    venv_dir = tmp_path / "work" / "venv"
    fake_venv(venv_dir, executable="/opt/bin/python3", version="3.14.4")
    runs = FakeRuns("/opt/bin/python3\n3.15.0\n")
    monkeypatch.setattr(release_build, "_run", runs)

    release_build.ensure_build_venv(
        options(tmp_path, venv_dir=venv_dir, python="/opt/bin/python3"),
        read_project(REPO_ROOT),
    )
    assert not (venv_dir / "marker").exists()


def test_the_version_info_spelling_is_the_same_identity(tmp_path: Path, monkeypatch):
    # venv records `3.14.4` or `3.14.4.final.0` depending on the version that
    # wrote it; reading them as different interpreters would rebuild the
    # environment on every single build.
    venv_dir = tmp_path / "work" / "venv"
    fake_venv(
        venv_dir,
        executable="/base/bin/python3.14",
        version="3.14.4.final.0",
        version_key="version_info",
    )
    runs = FakeRuns("/base/bin/python3.14\n3.14.4\n")
    monkeypatch.setattr(release_build, "_run", runs)

    release_build.ensure_build_venv(
        options(tmp_path, venv_dir=venv_dir, python="/base/bin/python3.14"),
        read_project(REPO_ROOT),
    )
    assert (venv_dir / "marker").exists()
    assert not runs.created_a_venv()


def cached_interpreter(work_dir: Path) -> Path:
    """The pinned interpreter, already provisioned under the scratch directory.

    Provisioning is covered by tests/test_release_python.py; what matters here
    is that a build given no interpreter reaches for exactly this path, so the
    cache is primed rather than the download stubbed.
    """
    pin = pinned_interpreter(TARGET)
    python = work_dir / release_build.INTERPRETER_DIR / pin.name / pin.relative_python
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    return python


def test_a_build_that_names_no_interpreter_uses_the_pinned_one(
    tmp_path: Path, monkeypatch
):
    # The default has to be the pin rather than the host: an interpreter nobody
    # chose is exactly what makes a payload refuse to start on an older Mac.
    venv_dir = tmp_path / "work" / "venv"
    python = cached_interpreter(tmp_path / "work")
    runs = FakeRuns()
    monkeypatch.setattr(release_build, "_run", runs)

    release_build.ensure_build_venv(
        options(tmp_path, venv_dir=venv_dir), read_project(REPO_ROOT)
    )
    assert [str(python), "-m", "venv", str(venv_dir)] in runs.commands


def test_an_environment_built_from_the_pin_is_reused_without_a_flag(
    tmp_path: Path, monkeypatch
):
    venv_dir = tmp_path / "work" / "venv"
    python = cached_interpreter(tmp_path / "work")
    fake_venv(venv_dir, executable=str(python))
    runs = FakeRuns(f"{python}\n3.14.4\n")
    monkeypatch.setattr(release_build, "_run", runs)

    release_build.ensure_build_venv(
        options(tmp_path, venv_dir=venv_dir), read_project(REPO_ROOT)
    )
    assert (venv_dir / "marker").exists()
    assert not runs.created_a_venv()


def test_a_default_build_replaces_an_environment_built_from_another_interpreter(
    tmp_path: Path, monkeypatch
):
    # The environment is a cache, but a cache holding the host interpreter would
    # quietly produce the payload the pin exists to prevent.
    venv_dir = tmp_path / "work" / "venv"
    python = cached_interpreter(tmp_path / "work")
    fake_venv(venv_dir, executable="/opt/homebrew/bin/python3.14")
    runs = FakeRuns(f"{python}\n3.14.7\n")
    monkeypatch.setattr(release_build, "_run", runs)

    release_build.ensure_build_venv(
        options(tmp_path, venv_dir=venv_dir), read_project(REPO_ROOT)
    )
    assert not (venv_dir / "marker").exists()
    assert [str(python), "-m", "venv", str(venv_dir)] in runs.commands


def test_reusing_an_environment_refuses_a_different_pinned_interpreter(
    tmp_path: Path, monkeypatch
):
    # --skip-install may not rebuild anything, so the mismatch has to be said
    # out loud rather than resolved silently.
    venv_dir = tmp_path / "work" / "venv"
    fake_venv(venv_dir, executable="/old/bin/python3.14")
    monkeypatch.setattr(release_build, "_run", FakeRuns("/new/bin/python3.14\n3.14.4\n"))

    with pytest.raises(BuildError, match="not from the requested"):
        release_build.ensure_build_venv(
            options(tmp_path, venv_dir=venv_dir, python="/new/bin/python3.14", install=False),
            read_project(REPO_ROOT),
        )


def test_an_environment_that_cannot_name_its_interpreter_is_rebuilt(
    tmp_path: Path, monkeypatch
):
    # An environment that cannot say what it was built from is not the pinned
    # one until proven otherwise.
    venv_dir = tmp_path / "work" / "venv"
    fake_venv(venv_dir, executable="/base/bin/python3.14")
    (venv_dir / "pyvenv.cfg").unlink()
    monkeypatch.setattr(release_build, "_run", FakeRuns())

    release_build.ensure_build_venv(
        options(tmp_path, venv_dir=venv_dir, python="/base/bin/python3.14"),
        read_project(REPO_ROOT),
    )
    assert not (venv_dir / "marker").exists()


def payload_files(extra: dict[str, bytes] | None = None) -> dict[str, bytes]:
    """A payload holding every member a usable runtime must have.

    Taken from the driver's own list, so a new required member is staged here
    too instead of turning these tests into missing-member tests.
    """
    files = {
        member: b"payload member"
        for member in release_build.REQUIRED_MEMBERS
        if member != release_build.MANIFEST_NAME  # the build writes this one
    }
    files.update(extra or {})
    return files


def prepare_build(
    tmp_path: Path,
    monkeypatch,
    *,
    staged_files: dict[str, bytes],
    literals=(),
    min_macos: str | None = "11.0",
) -> BuildOptions:
    """Run a build for real from the freeze onwards, without freezing anything.

    Everything the driver decides after the freezer returns — describing the
    payload, auditing it, publishing it — runs unmodified; only the freezer,
    the build environment and the Mach-O scan are stood in for.
    """
    checkout = tmp_path / "checkout"
    (checkout / "orch_hub").mkdir(parents=True)
    (checkout / "orch_hub" / "__init__.py").write_text(
        '__version__ = "1.2.3"\n', encoding="utf-8"
    )
    (checkout / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    staged_dir = tmp_path / "work" / "dist" / "orch-hub"

    def fake_freeze(build_options, venv_python):
        for relative, content in staged_files.items():
            path = staged_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return staged_dir

    monkeypatch.setattr(release_build, "check_host", lambda: None)
    monkeypatch.setattr(
        release_build, "ensure_build_venv", lambda build_options, project: tmp_path / "python"
    )
    monkeypatch.setattr(release_build, "_freeze", fake_freeze)
    monkeypatch.setattr(
        release_build,
        "scan_payload",
        lambda root: PayloadFacts(
            architectures=("arm64",), min_macos=min_macos, binaries=1
        ),
    )
    monkeypatch.setattr(release_build, "_interpreter_version", lambda python: "3.14.4")
    monkeypatch.setattr(release_build, "_builder_identity", lambda python: "pyinstaller 6.22.2")
    return options(tmp_path, checkout=checkout, literals=tuple(literals))


def previous_payload(tmp_path: Path) -> Path:
    payload_dir = tmp_path / "dist" / f"orch-hub-{TARGET}"
    payload_dir.mkdir(parents=True)
    (payload_dir / "marker").write_text("last good payload", encoding="utf-8")
    return payload_dir


def test_a_rejected_payload_is_not_published_and_leaves_the_last_one_intact(
    tmp_path: Path, monkeypatch
):
    # The distributable path is what the installer and the smoke test consume,
    # so a candidate that failed its audit must not be sitting there — and the
    # payload that did pass must not have been deleted to make room for it.
    payload_dir = previous_payload(tmp_path)
    build_options = prepare_build(
        tmp_path,
        monkeypatch,
        staged_files=payload_files({"_internal/blob.bin": b"built in /Users/maintainer/x"}),
        literals=release_build.maintainer_path_literals(["/Users/maintainer"]),
    )

    with pytest.raises(BuildError, match="payload audit"):
        release_build.build_runtime(build_options)

    assert (payload_dir / "marker").read_text(encoding="utf-8") == "last good payload"
    assert not (payload_dir / "_internal").exists()
    assert not (payload_dir / "runtime.json").exists()
    assert list(payload_dir.parent.iterdir()) == [payload_dir]


def test_a_missing_required_member_also_stops_publication(tmp_path: Path, monkeypatch):
    # Absence of the forbidden is only half of a distributable payload; an
    # incomplete one must not reach the distributable path either.
    payload_dir = previous_payload(tmp_path)
    build_options = prepare_build(
        tmp_path, monkeypatch, staged_files={"orch-hub": b"executable only"}
    )

    with pytest.raises(BuildError, match="required member is absent"):
        release_build.build_runtime(build_options)
    assert (payload_dir / "marker").read_text(encoding="utf-8") == "last good payload"


def test_a_payload_that_passes_replaces_the_previous_one(tmp_path: Path, monkeypatch):
    payload_dir = previous_payload(tmp_path)
    build_options = prepare_build(tmp_path, monkeypatch, staged_files=payload_files())

    result = release_build.build_runtime(build_options)

    assert result.payload_dir == payload_dir
    assert result.audit.clean
    assert result.manifest["version"] == "1.2.3"
    assert (payload_dir / "runtime.json").is_file()
    # Replaced rather than merged: nothing of the old payload is left behind.
    assert not (payload_dir / "marker").exists()
    assert list(payload_dir.parent.iterdir()) == [payload_dir]


def test_a_payload_above_the_floor_is_not_published(tmp_path: Path, monkeypatch):
    # The installer refuses this payload on every Mac older than the builder's,
    # and refuses it correctly — which is why the build is the last place the
    # mistake can still be caught.
    payload_dir = previous_payload(tmp_path)
    build_options = prepare_build(
        tmp_path, monkeypatch, staged_files=payload_files(), min_macos="26.0"
    )

    with pytest.raises(BuildError, match="requires macOS 26.0"):
        release_build.build_runtime(build_options)

    assert (payload_dir / "marker").exists()  # last good payload untouched
    assert not (payload_dir / "runtime.json").exists()


def test_a_payload_without_a_readable_floor_is_not_published(tmp_path: Path, monkeypatch):
    payload_dir = previous_payload(tmp_path)
    build_options = prepare_build(
        tmp_path, monkeypatch, staged_files=payload_files(), min_macos=None
    )

    with pytest.raises(BuildError, match="declares no minimum macOS"):
        release_build.build_runtime(build_options)

    assert (payload_dir / "marker").exists()


def test_a_payload_below_the_floor_is_published_with_its_own_number(
    tmp_path: Path, monkeypatch
):
    # An older floor is a wider release, not a violation; what ships is what was
    # measured, so the installer keeps gating on the truth.
    build_options = prepare_build(
        tmp_path, monkeypatch, staged_files=payload_files(), min_macos="10.15"
    )

    result = release_build.build_runtime(build_options)

    assert result.manifest["min_macos"] == "10.15"


def test_an_unpublishable_payload_can_still_be_built_on_purpose(
    tmp_path: Path, monkeypatch, capsys
):
    # A local smoke test of the frozen runtime is worth building from the host
    # interpreter; it is only worth refusing when somebody could publish it.
    build_options = prepare_build(
        tmp_path, monkeypatch, staged_files=payload_files(), min_macos="26.0"
    )

    result = release_build.build_runtime(
        BuildOptions(**{**vars(build_options), "allow_unsupported_macos": True})
    )

    assert result.manifest["min_macos"] == "26.0"
    assert "warning:" in capsys.readouterr().err


UNCOPYABLE = "blob.bin"
"""Staged member the cross-filesystem move below refuses to copy."""

_REAL_MOVE = shutil.move
"""Bound before any test patches ``shutil.move``, which is the same module
object the driver calls through — so the stand-in below can still reach the
real implementation it is standing in front of."""


def staged_payload(tmp_path: Path) -> Path:
    """A payload as the freezer leaves it, holding the member the move refuses."""
    staged_dir = tmp_path / "work" / "dist" / "orch-hub"
    (staged_dir / "_internal").mkdir(parents=True)
    (staged_dir / "orch-hub").write_text("new payload", encoding="utf-8")
    (staged_dir / "_internal" / UNCOPYABLE).write_text("library", encoding="utf-8")
    return staged_dir


def _failing_copy(source, destination, *args, **kwargs):
    """A ``copy2`` that refuses one member, the way a full disk would."""
    if Path(source).name == UNCOPYABLE:
        raise OSError(errno.ENOSPC, "no space left on device")
    return shutil.copy2(source, destination, *args, **kwargs)


def cross_filesystem_move(source, destination):
    """``shutil.move`` on its cross-filesystem branch, failing partway through.

    Within one ``tmp_path`` a move is always a rename, so the rename is refused
    for the duration of this call — and only for it, since the swap that
    follows a successful move is a rename too. What runs instead is the
    standard library's own ``copytree`` fallback, so the half-populated
    destination these tests are about is the one a build would really be left
    with: created, partly filled, and abandoned under a ``shutil.Error``.
    """
    with mock.patch("os.rename", side_effect=OSError(errno.EXDEV, "cross-device link")):
        return _REAL_MOVE(source, destination, copy_function=_failing_copy)


def test_a_first_publication_that_fails_mid_copy_publishes_nothing(
    tmp_path: Path, monkeypatch
):
    # A move that crosses filesystems copies, and a copy that fails has already
    # created its destination. On a first build there is no previous payload
    # whose restoration would incidentally clear that debris, so the
    # distributable path the installer and the smoke test consume must simply
    # not come into existence.
    payload_dir = tmp_path / "dist" / f"orch-hub-{TARGET}"
    staged_dir = staged_payload(tmp_path)
    monkeypatch.setattr(release_build.shutil, "move", cross_filesystem_move)

    with pytest.raises(BuildError, match="cannot stage the payload"):
        release_build._place_payload(staged_dir, payload_dir)

    assert not payload_dir.exists()
    assert list(payload_dir.parent.iterdir()) == []


def test_a_replacing_publication_that_fails_mid_copy_keeps_the_previous_payload(
    tmp_path: Path, monkeypatch
):
    # Same partial copy, this time over an existing release: the payload that
    # passed stays whole and stays selectable.
    payload_dir = previous_payload(tmp_path)
    staged_dir = staged_payload(tmp_path)
    monkeypatch.setattr(release_build.shutil, "move", cross_filesystem_move)

    with pytest.raises(BuildError, match="cannot stage the payload"):
        release_build._place_payload(staged_dir, payload_dir)

    assert (payload_dir / "marker").read_text(encoding="utf-8") == "last good payload"
    assert list(payload_dir.parent.iterdir()) == [payload_dir]


def test_a_move_that_fails_before_copying_leaves_the_previous_payload_in_place(
    tmp_path: Path, monkeypatch
):
    # The move can also fail outright — a payload open elsewhere, a read-only
    # output directory — without having created anything.
    payload_dir = previous_payload(tmp_path)
    staged_dir = staged_payload(tmp_path)

    def refuse(source, destination):
        raise OSError("no space left on device")

    monkeypatch.setattr(release_build.shutil, "move", refuse)
    with pytest.raises(BuildError, match="cannot stage the payload"):
        release_build._place_payload(staged_dir, payload_dir)

    assert (payload_dir / "marker").read_text(encoding="utf-8") == "last good payload"
    assert list(payload_dir.parent.iterdir()) == [payload_dir]


def test_a_swap_that_fails_puts_the_previous_payload_back(
    tmp_path: Path, monkeypatch
):
    # The staged payload is on the output filesystem and the previous one has
    # been moved aside; the rename that finishes the swap still fails. The
    # distributable path must not be left empty.
    payload_dir = previous_payload(tmp_path)
    staged_dir = staged_payload(tmp_path)
    incoming_dir = payload_dir.with_name(payload_dir.name + release_build.INCOMING_SUFFIX)
    real_rename = os.rename

    def rename(source, destination, *args, **kwargs):
        if Path(source) == incoming_dir:
            raise OSError(errno.EIO, "input/output error")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(BuildError, match="cannot place the payload"):
        release_build._place_payload(staged_dir, payload_dir)

    assert (payload_dir / "marker").read_text(encoding="utf-8") == "last good payload"
    assert list(payload_dir.parent.iterdir()) == [payload_dir]


def test_a_swap_that_cannot_be_undone_says_where_the_previous_payload_is(
    tmp_path: Path, monkeypatch
):
    # If putting the previous payload back fails too, the distributable path is
    # empty and the operator needs to be told what to move where — not handed
    # the restore's exception in place of the real failure.
    payload_dir = previous_payload(tmp_path)
    staged_dir = staged_payload(tmp_path)
    real_rename = os.rename

    def rename(source, destination, *args, **kwargs):
        if Path(destination) == payload_dir:
            raise OSError(errno.EIO, "input/output error")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(BuildError, match="the previous payload is left at"):
        release_build._place_payload(staged_dir, payload_dir)

    previous_dir = payload_dir.with_name(payload_dir.name + release_build.PREVIOUS_SUFFIX)
    assert (previous_dir / "marker").read_text(encoding="utf-8") == "last good payload"


def test_debris_from_an_interrupted_publication_does_not_become_the_payload(
    tmp_path: Path,
):
    # A move whose destination already exists moves *into* it, so a leftover
    # staging directory would bury the new payload one level down instead of
    # publishing it. Both sibling names are transient by construction and are
    # cleared before anything moves.
    payload_dir = previous_payload(tmp_path)
    staged_dir = staged_payload(tmp_path)
    for suffix in (release_build.INCOMING_SUFFIX, release_build.PREVIOUS_SUFFIX):
        debris = payload_dir.with_name(payload_dir.name + suffix)
        debris.mkdir()
        (debris / "stale").write_text("interrupted build", encoding="utf-8")

    release_build._place_payload(staged_dir, payload_dir)

    assert (payload_dir / "orch-hub").read_text(encoding="utf-8") == "new payload"
    assert not (payload_dir / "stale").exists()
    assert list(payload_dir.parent.iterdir()) == [payload_dir]


def test_entry_point_is_the_module_python_m_runs():
    # One dispatch for both runtimes: whatever `python -m orch_hub` executes is
    # what the freezer compiles as the program's __main__.
    assert release_build.ENTRY_RELATIVE_PATH == Path("orch_hub") / "__main__.py"
    assert (REPO_ROOT / release_build.ENTRY_RELATIVE_PATH).is_file()
