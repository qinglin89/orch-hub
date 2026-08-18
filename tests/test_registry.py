"""Tests for managed-repo discovery.

Uses temporary directories and an injected fake runner; never touches the real
mandrel registry or the real ``mandrel`` command.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.registry import RegistryError, discover_repos
from orch_hub.subprocess_utils import CommandNotFoundError, CommandResult

SAMPLE = [
    {
        "name": "repo-a",
        "path": "/x/repo-a",
        "manifest": "/x/repo-a/.ai-deploy-manifest.json",
    },
    {
        "name": "repo-b",
        "path": "/x/repo-b",
        "manifest": "/x/repo-b/.ai-deploy-manifest.json",
    },
]


def make_config(tmp_path: Path) -> Config:
    return Config(mandrel_path=tmp_path)


def ok_result(stdout: str) -> CommandResult:
    return CommandResult(args=("mandrel",), returncode=0, stdout=stdout, stderr="")


def fail_result(returncode: int = 1, stderr: str = "boom") -> CommandResult:
    return CommandResult(
        args=("mandrel",), returncode=returncode, stdout="", stderr=stderr
    )


def write_registry_file(tmp_path: Path, data) -> Path:
    reg_dir = tmp_path / ".registry"
    reg_dir.mkdir(parents=True, exist_ok=True)
    path = reg_dir / "repos.local.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_discover_parses_command_json(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append([str(a) for a in args])
        return ok_result(json.dumps(SAMPLE))

    repos = discover_repos(make_config(tmp_path), runner=runner)

    assert [r.name for r in repos] == ["repo-a", "repo-b"]
    assert repos[0].path == Path("/x/repo-a")
    assert repos[0].manifest == Path("/x/repo-a/.ai-deploy-manifest.json")
    # The command source is preferred and invoked with the expected argv.
    assert calls[0][1:] == ["registry", "list", "--json"]


def test_command_unavailable_falls_back_to_file(tmp_path):
    write_registry_file(tmp_path, SAMPLE)

    def runner(args, **kwargs):
        raise CommandNotFoundError("mandrel")

    repos = discover_repos(make_config(tmp_path), runner=runner)
    assert [r.name for r in repos] == ["repo-a", "repo-b"]


def test_command_failure_falls_back_to_file(tmp_path):
    write_registry_file(tmp_path, SAMPLE[:1])

    def runner(args, **kwargs):
        return fail_result()

    repos = discover_repos(make_config(tmp_path), runner=runner)
    assert [r.name for r in repos] == ["repo-a"]


def test_missing_registry_file_is_empty_list(tmp_path):
    def runner(args, **kwargs):
        raise CommandNotFoundError("mandrel")

    repos = discover_repos(make_config(tmp_path), runner=runner)
    assert repos == []


def test_invalid_file_json_raises(tmp_path):
    reg_dir = tmp_path / ".registry"
    reg_dir.mkdir()
    (reg_dir / "repos.local.json").write_text("{not json", encoding="utf-8")

    def runner(args, **kwargs):
        return fail_result()

    with pytest.raises(RegistryError):
        discover_repos(make_config(tmp_path), runner=runner)


def test_invalid_command_json_raises(tmp_path):
    # Exit 0 is trusted: unparseable output is an explicit error, not a fallback.
    def runner(args, **kwargs):
        return ok_result("{not json")

    with pytest.raises(RegistryError):
        discover_repos(make_config(tmp_path), runner=runner)


def test_malformed_entry_missing_key_raises(tmp_path):
    def runner(args, **kwargs):
        return ok_result(json.dumps([{"name": "x", "path": "/x"}]))  # no manifest

    with pytest.raises(RegistryError):
        discover_repos(make_config(tmp_path), runner=runner)


def test_non_array_json_raises(tmp_path):
    def runner(args, **kwargs):
        return ok_result(json.dumps({"repos": []}))

    with pytest.raises(RegistryError):
        discover_repos(make_config(tmp_path), runner=runner)


def test_empty_command_array_is_empty_list(tmp_path):
    def runner(args, **kwargs):
        return ok_result("[]")

    repos = discover_repos(make_config(tmp_path), runner=runner)
    assert repos == []
