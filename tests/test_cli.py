"""Tests for the CLI adapter.

Monkeypatches the application functions so the CLI wiring and JSON serialization
are exercised without touching real repos or subprocesses.
"""

from __future__ import annotations

import json
from pathlib import Path

from orch_hub import cli
from orch_hub.config import PORT_ENV
from orch_hub.models import ManagedRepo, ReadinessCheck, ReadinessReport
from orch_hub.registry import RegistryError


def make_repo() -> ManagedRepo:
    return ManagedRepo(
        name="r",
        path=Path("/p/r"),
        manifest=Path("/p/r/.ai-deploy-manifest.json"),
    )


def test_repos_json_ready(monkeypatch, capsys):
    repo = make_repo()
    report = ReadinessReport((ReadinessCheck("repo_path_exists", True, "ok"),))
    monkeypatch.setattr(cli, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(cli, "check_readiness", lambda r, config: report)

    rc = cli.main(["repos", "--json"])
    assert rc == 0

    data = json.loads(capsys.readouterr().out)
    assert data == {
        "repos": [
            {
                "name": "r",
                "path": "/p/r",
                "manifest": "/p/r/.ai-deploy-manifest.json",
                "readiness": {"ready": True, "checks": []},
            }
        ]
    }


def test_repos_json_reports_failures(monkeypatch, capsys):
    repo = make_repo()
    report = ReadinessReport(
        (
            ReadinessCheck("repo_path_exists", True, "ok"),
            ReadinessCheck("git_clean", False, "working tree has uncommitted changes"),
        )
    )
    monkeypatch.setattr(cli, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(cli, "check_readiness", lambda r, config: report)

    rc = cli.main(["repos", "--json"])
    assert rc == 0

    data = json.loads(capsys.readouterr().out)
    readiness = data["repos"][0]["readiness"]
    assert readiness["ready"] is False
    assert readiness["checks"] == [
        {"name": "git_clean", "message": "working tree has uncommitted changes"}
    ]


def test_malformed_port_env_returns_1(monkeypatch, capsys):
    # A service-only env typo must exit cleanly, not escape as a traceback.
    monkeypatch.setenv(PORT_ENV, "eight")

    rc = cli.main(["repos", "--json"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert PORT_ENV in err


def test_registry_error_returns_1(monkeypatch, capsys):
    def boom(config):
        raise RegistryError("invalid registry JSON")

    monkeypatch.setattr(cli, "discover_repos", boom)

    rc = cli.main(["repos", "--json"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "invalid registry JSON" in err


def test_human_output_lists_status(monkeypatch, capsys):
    repo = make_repo()
    report = ReadinessReport(
        (ReadinessCheck("git_clean", False, "working tree has uncommitted changes"),)
    )
    monkeypatch.setattr(cli, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(cli, "check_readiness", lambda r, config: report)

    rc = cli.main(["repos"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NOT READY" in out
    assert "r" in out
    assert "git_clean" in out


def test_human_output_empty(monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover_repos", lambda config: [])

    rc = cli.main(["repos"])
    assert rc == 0
    assert "No managed repos found." in capsys.readouterr().out
