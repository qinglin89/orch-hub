"""Tests for runtime resource resolution across source and frozen runtimes.

A compiled release resolves its dashboard assets and plist template from a
bundle directory instead of from the package's source location, and it must not
hand its bootloader's private variables to the processes it spawns. Both
behaviours only happen in a frozen process, so the tests simulate one by
setting the same markers a freezer sets — that is the whole contract
``orch_hub.resources`` is written against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orch_hub import resources


@pytest.fixture
def frozen(monkeypatch, tmp_path: Path) -> Path:
    """Present the process as a frozen runtime whose bundle is ``tmp_path``."""
    monkeypatch.setattr(resources.sys, "frozen", True, raising=False)
    monkeypatch.setattr(resources.sys, "_MEIPASS", str(tmp_path), raising=False)
    return tmp_path


def test_source_runtime_is_not_frozen():
    assert resources.is_frozen() is False


def test_source_web_dir_sits_next_to_the_package():
    assert resources.web_dir() == resources.PACKAGE_DIR / "web"
    # The real assets have to be there, otherwise the shell routes 404 in every
    # source install.
    assert (resources.web_dir() / "index.html").is_file()


def test_frozen_runtime_resolves_resources_from_the_bundle(frozen: Path):
    assert resources.is_frozen() is True
    assert resources.runtime_root() == frozen
    assert resources.web_dir() == frozen / "orch_hub" / "web"


def test_frozen_runtime_without_a_bundle_dir_falls_back_to_the_package(monkeypatch):
    # A freezer that marks sys.frozen without publishing a bundle directory must
    # not break imports of modules that resolve a resource at import time; the
    # missing file surfaces at the point of use instead.
    monkeypatch.setattr(resources.sys, "frozen", True, raising=False)
    monkeypatch.delattr(resources.sys, "_MEIPASS", raising=False)
    assert resources.runtime_root() == resources.PACKAGE_DIR.parent


def test_bundled_resource_is_absent_in_a_source_runtime():
    assert resources.bundled_resource(Path("deploy") / "launchd" / "x.plist") is None


def test_bundled_resource_returns_the_bundled_copy_when_present(frozen: Path):
    relative = Path("deploy") / "launchd" / "x.plist"
    target = frozen / relative
    target.parent.mkdir(parents=True)
    target.write_text("<plist/>", encoding="utf-8")
    assert resources.bundled_resource(relative) == target


def test_bundled_resource_is_absent_when_the_bundle_lacks_it(frozen: Path):
    # None keeps the caller's own "not found" reporting authoritative rather
    # than handing back a path that cannot exist.
    assert resources.bundled_resource(Path("deploy") / "missing.plist") is None


def test_strip_bootloader_env_drops_only_bootloader_variables():
    env = {
        "PATH": "/usr/bin",
        "ORCH_HUB_TOKEN": "secret",
        "_PYI_ARCHIVE_FILE": "/tmp/orch-hub",
        "_PYI_PARENT_PROCESS_LEVEL": "1",
    }
    assert resources.strip_bootloader_env(env) == {
        "PATH": "/usr/bin",
        "ORCH_HUB_TOKEN": "secret",
    }


def test_strip_bootloader_env_copies_rather_than_mutates():
    env = {"PATH": "/usr/bin"}
    result = resources.strip_bootloader_env(env)
    result["PATH"] = "/changed"
    assert env["PATH"] == "/usr/bin"


def test_runtime_info_reports_the_source_runtime():
    info = resources.runtime_info()
    assert info.frozen is False
    assert info.to_dict()["runtime"] == "source"
    assert info.to_dict()["version"] == info.version


def test_runtime_info_reports_a_frozen_runtime(frozen: Path):
    assert resources.runtime_info().to_dict()["runtime"] == "frozen"
