"""Tests for the CLI adapter.

Monkeypatches the application functions so the CLI wiring and JSON serialization
are exercised without touching real repos or subprocesses.
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

from orch_hub import cli
from orch_hub.config import (
    BARK_URL_ENV,
    EVAL_ENABLED_ENV,
    EVAL_RETENTION_ENV,
    PORT_ENV,
    TOKEN_ENV,
)
from orch_hub.launchd import LaunchdError, PLIST_NAME
from orch_hub.models import CommitInfo, ManagedRepo, ReadinessCheck, ReadinessReport
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
    # No commit found (best-effort git read) → the field is present but null.
    monkeypatch.setattr(cli, "latest_commit", lambda repo: None)

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
                "commit": None,
                "housekeeping_pending": False,
            }
        ]
    }


def test_repos_json_includes_latest_commit(monkeypatch, capsys):
    # `repos --json` carries the latest commit so it stays identical to
    # GET /api/repos (both go through repos_payload).
    repo = make_repo()
    report = ReadinessReport((ReadinessCheck("repo_path_exists", True, "ok"),))
    commit = CommitInfo(
        hash="f" * 40,
        short_hash="fffffff",
        committed_ts="2026-07-09T16:25:00+08:00",
        subject="update ai-protocal",
    )
    monkeypatch.setattr(cli, "discover_repos", lambda config: [repo])
    monkeypatch.setattr(cli, "check_readiness", lambda r, config: report)
    monkeypatch.setattr(cli, "latest_commit", lambda repo: commit)

    cli.main(["repos", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["repos"][0]["commit"] == {
        "hash": "f" * 40,
        "short_hash": "fffffff",
        "committed_ts": "2026-07-09T16:25:00+08:00",
        "subject": "update ai-protocal",
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


def test_launchd_start_cli(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "start_service",
        lambda plist_path=None: SimpleNamespace(
            started=True, already_started=False
        ),
    )

    rc = cli.main(["launchd", "start"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "launchd service started" in out
    assert "deploy/update are required" in out


def test_launchd_restart_cli(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "restart_service",
        lambda plist_path=None: SimpleNamespace(
            stop=SimpleNamespace(stopped=True),
            start=SimpleNamespace(started=True),
        ),
    )

    rc = cli.main(["launchd", "restart"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "launchd service restarted" in out
    assert "use update after code changes" in out


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
        bark_url=None,
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
    assert "bark push: disabled" in out


# -- notify test ---------------------------------------------------------------


class RecordingNotifier:
    """Captures delivered notifications; can be told to raise a failure."""

    def __init__(self, *, fail: Exception | None = None):
        self.sent: list = []
        self._fail = fail

    def notify(self, notification) -> None:
        self.sent.append(notification)
        if self._fail is not None:
            raise self._fail


def _absent_plist(tmp_path: Path) -> list[str]:
    # Point --plist-path at a nonexistent file so tests never read the real
    # user launchd plist (which may carry a live Bark URL on the dev machine).
    return ["--plist-path", str(tmp_path / "absent.plist")]


def test_notify_test_sends_when_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(BARK_URL_ENV, "https://bark.example/KEY")
    notifier = RecordingNotifier()
    seen: dict = {}

    def fake_build(config):
        seen["bark_url"] = config.bark_url
        return notifier

    monkeypatch.setattr(cli, "build_notifier", fake_build)

    rc = cli.main(["notify", "test", *_absent_plist(tmp_path)])

    assert rc == 0
    # The env value flows through to the notifier factory.
    assert seen["bark_url"] == "https://bark.example/KEY"
    assert len(notifier.sent) == 1
    assert notifier.sent[0].title == "orch-hub: test notification"
    assert "test notification sent" in capsys.readouterr().out


def test_notify_test_missing_config_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(BARK_URL_ENV, raising=False)

    # Real build_notifier: with no URL anywhere it returns None -> clear error.
    rc = cli.main(["notify", "test", *_absent_plist(tmp_path)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "no notification backend configured" in err


def test_notify_test_reports_delivery_failure_without_leaking_url(
    tmp_path, monkeypatch, capsys
):
    url = "https://bark.example/SECRETKEY"
    monkeypatch.setenv(BARK_URL_ENV, url)
    # A schemeless/misconfigured URL surfaces inside urllib's error text; the
    # CLI must scrub the exact endpoint before printing.
    failure = URLError(f"unknown url type: '{url}'")
    monkeypatch.setattr(
        cli, "build_notifier", lambda config: RecordingNotifier(fail=failure)
    )

    rc = cli.main(["notify", "test", *_absent_plist(tmp_path)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "notification delivery failed" in err
    assert "URLError" in err  # failure type is surfaced
    assert "SECRETKEY" not in err  # secret device key never leaks
    assert url not in err


def test_notify_test_falls_back_to_installed_plist(tmp_path, monkeypatch, capsys):
    # No ORCH_HUB_BARK_URL in the shell, but the installed launchd plist carries
    # it; the delivery check must resolve the plist value.
    monkeypatch.delenv(BARK_URL_ENV, raising=False)
    plist_path = tmp_path / PLIST_NAME
    with plist_path.open("wb") as fh:
        plistlib.dump(
            {
                "EnvironmentVariables": {
                    TOKEN_ENV: "tok",
                    BARK_URL_ENV: "https://bark.example/PLISTKEY",
                }
            },
            fh,
            fmt=plistlib.FMT_XML,
        )
    notifier = RecordingNotifier()
    seen: dict = {}

    def fake_build(config):
        seen["bark_url"] = config.bark_url
        return notifier

    monkeypatch.setattr(cli, "build_notifier", fake_build)

    rc = cli.main(["notify", "test", "--plist-path", str(plist_path)])

    assert rc == 0
    assert seen["bark_url"] == "https://bark.example/PLISTKEY"
    assert len(notifier.sent) == 1


# -- eval doctor (Layer 2 config/availability smoke check) --------------------
#
# Controls availability through the semantic module's ``_default_which`` resolver
# (resolved at call time), so the real backend path runs without launching
# ``claude``. Env is pinned per test so the check is hermetic.


def test_eval_doctor_available_exits_zero(monkeypatch, capsys):
    monkeypatch.setenv(EVAL_ENABLED_ENV, "1")
    monkeypatch.delenv(EVAL_RETENTION_ENV, raising=False)
    monkeypatch.setattr("orch_hub.semantic._default_which", lambda: "/usr/bin/claude")

    rc = cli.main(["eval", "doctor"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "evaluation enabled: yes" in out
    assert "available: yes" in out
    assert "backend: claude-cli" in out


def test_eval_doctor_unconfigured_exits_nonzero(monkeypatch, capsys):
    # Enabled but ``claude`` unresolved: the exact session-1 gap, now a clear
    # non-zero smoke-check signal instead of a 502 on click.
    monkeypatch.setenv(EVAL_ENABLED_ENV, "1")
    monkeypatch.delenv(EVAL_RETENTION_ENV, raising=False)
    monkeypatch.setattr("orch_hub.semantic._default_which", lambda: None)

    rc = cli.main(["eval", "doctor"])

    assert rc == 1
    assert "available: no" in capsys.readouterr().out


def test_eval_doctor_disabled_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setenv(EVAL_ENABLED_ENV, "0")
    monkeypatch.delenv(EVAL_RETENTION_ENV, raising=False)

    rc = cli.main(["eval", "doctor"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "evaluation enabled: no" in out
    assert "disabled" in out


def test_eval_doctor_reports_config_error(monkeypatch, capsys):
    # A malformed service env surfaces as an operational error, not a traceback.
    monkeypatch.setenv(EVAL_ENABLED_ENV, "1")
    monkeypatch.setenv(EVAL_RETENTION_ENV, "lots")

    rc = cli.main(["eval", "doctor"])

    assert rc == 1
    assert "error:" in capsys.readouterr().err
