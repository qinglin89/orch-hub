"""Tests for the CLI adapter.

Monkeypatches the application functions so the CLI wiring and JSON serialization
are exercised without touching real repos or subprocesses.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from orch_hub import cli
from orch_hub.config import PORT_ENV
from orch_hub.launchd import LaunchdError
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


def test_launchd_stop_cli(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "stop_service",
        lambda: SimpleNamespace(stopped=True, already_stopped=False),
    )

    rc = cli.main(["launchd", "stop"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "launchd service stopped" in out
    assert "orchestrator runs were not stopped" in out


def test_launchd_status_cli(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "status_service",
        lambda plist_path=None: SimpleNamespace(
            label="com.qinglin89.orch-hub",
            plist_path=Path("/tmp/com.qinglin89.orch-hub.plist"),
            plist_installed=True,
            plist_error=None,
            loaded=True,
            program="/tmp/orch-hub-api",
            dashboard_url="http://127.0.0.1:8765/",
            health="ok",
            launchctl_output="",
        ),
    )

    rc = cli.main(["launchd", "status"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "loaded: yes" in out
    assert "health: ok" in out


def test_launchd_status_cli_error(monkeypatch, capsys):
    def boom(plist_path=None):
        raise LaunchdError("launchctl unavailable")

    monkeypatch.setattr(cli, "status_service", boom)

    rc = cli.main(["launchd", "status"])

    assert rc == 1
    assert "error: launchctl unavailable" in capsys.readouterr().err


def test_launchd_deploy_cli(monkeypatch, capsys):
    deployment = SimpleNamespace(
        plist_path=Path("/tmp/com.qinglin89.orch-hub.plist"),
        api_bin=Path("/tmp/.venv/bin/orch-hub-api"),
        dashboard_url="http://127.0.0.1:8765/",
        token="generated",
        token_source="generated",
    )
    monkeypatch.setattr(
        cli,
        "deploy_service",
        lambda options: SimpleNamespace(
            deployment=deployment, generated_token=True
        ),
    )

    rc = cli.main(["launchd", "deploy", "--skip-install"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "launchd deploy ok" in out
    assert "generated ORCH_HUB_TOKEN: generated" in out
