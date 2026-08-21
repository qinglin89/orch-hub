"""Tests for the CLI adapter.

Monkeypatches the application functions so the CLI wiring and JSON serialization
are exercised without touching real repos or subprocesses.
"""

from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from orch_hub import cli
from orch_hub import __version__
from orch_hub.config import (
    BARK_URL_ENV,
    DATA_DIR_ENV,
    DEFAULT_DATA_DIR,
    EVAL_ENABLED_ENV,
    EVAL_RETENTION_ENV,
    LEGACY_MANDREL_PATH_ENV,
    MANDREL_PATH_ENV,
    PORT_ENV,
    TOKEN_ENV,
)
from orch_hub.launchd import LaunchdError, PLIST_NAME
from orch_hub.models import CommitInfo, ManagedRepo, ReadinessCheck, ReadinessReport
from orch_hub.registry import RegistryError
from orch_hub.report_catalog import report_key


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


def _fake_deployment() -> SimpleNamespace:
    return SimpleNamespace(
        plist_path=Path("/tmp/com.qinglin89.orch-hub.plist"),
        api_bin=Path("/tmp/.venv/bin/orch-hub-api"),
        data_dir=DEFAULT_DATA_DIR,
        dashboard_url="http://127.0.0.1:8765/",
        token="generated",
        token_source="generated",
        bark_url=None,
    )


def test_launchd_deploy_cli(monkeypatch, capsys):
    deployment = _fake_deployment()
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


def test_launchd_deploy_help_explains_required_mandrel_path(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["launchd", "deploy", "--help"])

    assert error.value.code == 0
    out = capsys.readouterr().out
    normalized = " ".join(out.split())
    assert "required on first deploy" in normalized
    assert "ORCH_HUB_MANDREL_PATH" in normalized
    assert "ORCH_HUB_AI_NATIVE_DEPLOYMENT" in normalized
    assert "updates reuse the installed plist" in normalized
    assert "built-in default" not in normalized


@pytest.mark.parametrize("action", ["deploy", "update"])
def test_launchd_deploy_asks_for_no_claude_usage_setup(monkeypatch, capsys, tmp_path, action):
    """Deploy/update no longer nag about the statusLine wrapper.

    Claude usage now comes from the CLI's own utilization cache, which needs
    nothing installed into ``~/.claude`` — so an unconfigured wrapper is not a
    setup gap to report, and the deploy summary must not mention one. $HOME is
    redirected at an empty dir to prove the absence is not merely the developer's
    own wrapper being detected as configured.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        cli,
        "deploy_service",
        lambda options: SimpleNamespace(
            deployment=_fake_deployment(), generated_token=True
        ),
    )

    rc = cli.main(["launchd", action, "--skip-install"])

    assert rc == 0
    out = capsys.readouterr().out
    assert f"launchd {action} ok" in out
    assert "statusline" not in out.lower()
    assert "claude usage" not in out.lower()


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


def test_notify_test_falls_back_to_installed_plist_config(
    tmp_path, monkeypatch, capsys
):
    # A plain shell has neither resident setting, but the selected installed
    # plist carries both; the delivery check must recover the whole config.
    monkeypatch.delenv(BARK_URL_ENV, raising=False)
    monkeypatch.delenv(MANDREL_PATH_ENV, raising=False)
    monkeypatch.delenv(LEGACY_MANDREL_PATH_ENV, raising=False)
    plist_path = tmp_path / PLIST_NAME
    mandrel_path = tmp_path / "mandrel"
    with plist_path.open("wb") as fh:
        plistlib.dump(
            {
                "EnvironmentVariables": {
                    TOKEN_ENV: "tok",
                    BARK_URL_ENV: "https://bark.example/PLISTKEY",
                    MANDREL_PATH_ENV: str(mandrel_path),
                }
            },
            fh,
            fmt=plistlib.FMT_XML,
        )
    notifier = RecordingNotifier()
    seen: dict = {}

    def fake_build(config):
        seen["bark_url"] = config.bark_url
        seen["mandrel_path"] = config.mandrel_path
        return notifier

    monkeypatch.setattr(cli, "build_notifier", fake_build)

    rc = cli.main(["notify", "test", "--plist-path", str(plist_path)])

    assert rc == 0
    assert seen["bark_url"] == "https://bark.example/PLISTKEY"
    assert seen["mandrel_path"] == mandrel_path
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


# -- eval catalog verify/repair (the global report catalog's operator surface) --
#
# Driven against a real hub data dir rather than a stubbed catalog: the verbs are
# worth having only if the exit code and the located defects match what the feed
# routes actually do with the same file, and a mock would assert neither.


def catalog_env(monkeypatch, tmp_path: Path) -> Path:
    """Point the CLI at an empty hub data dir and return its catalog path."""
    monkeypatch.setenv(EVAL_ENABLED_ENV, "1")
    monkeypatch.delenv(EVAL_RETENTION_ENV, raising=False)
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    path = tmp_path / "report-catalog" / "catalog.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def catalog_line(seq: int, *, evaluation_id: str = "20260730T100000Z") -> str:
    """One published line, in the exact shape the catalog writer emits."""
    task_id = "2026-07-30-alpha-task"
    return json.dumps(
        {
            "seq": seq,
            "report_key": report_key(
                repo_slug="alpha", task_id=task_id, evaluation_id=evaluation_id
            ),
            "cataloged_ts": "2026-07-31T08:00:00+00:00",
            "repo_name": "alpha",
            "repo_slug": "alpha",
            "repo_path": None,
            "task_id": task_id,
            "evaluation_id": evaluation_id,
            "generated_ts": None,
            "target_source": "archived",
            "archived": True,
        },
        sort_keys=True,
    )


def test_eval_catalog_verify_clean_exits_zero_and_changes_nothing(
    monkeypatch, capsys, tmp_path
):
    path = catalog_env(monkeypatch, tmp_path)
    path.write_text(catalog_line(1) + "\n", encoding="utf-8")
    before = path.read_bytes()

    rc = cli.main(["eval", "catalog", "verify"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "serves feed: yes" in out
    assert "defects: none" in out
    # The artifacts were pruned long ago, which is the feed's designed end state
    # and not a defect — so it is censused, not reported.
    assert "1 pruned" in out
    assert path.read_bytes() == before


def test_eval_catalog_verify_locates_a_duplicate_and_exits_nonzero(
    monkeypatch, capsys, tmp_path
):
    # The exact state that makes the feed routes answer 500. The operator's
    # previous answer was that status code; this is the line number.
    path = catalog_env(monkeypatch, tmp_path)
    path.write_text(
        catalog_line(1)
        + "\n"
        + catalog_line(1, evaluation_id="20260730T110000Z")
        + "\n",
        encoding="utf-8",
    )

    rc = cli.main(["eval", "catalog", "verify"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "serves feed: no" in out
    assert "line 2 seq 1" in out
    assert "already cataloged at line 1" in out


def test_eval_catalog_verify_json_carries_the_census_and_machine_codes(
    monkeypatch, capsys, tmp_path
):
    path = catalog_env(monkeypatch, tmp_path)
    path.write_text(catalog_line(1) + "\n{not json\n", encoding="utf-8")

    rc = cli.main(["eval", "catalog", "verify", "--json"])

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["catalog_path"] == str(path)
    assert (data["present"], data["lines"], data["entries"]) == (True, 2, 1)
    assert data["serves_feed"] is True
    assert data["artifacts"]["pruned"] == 1
    assert data["defects"] == [
        {
            "kind": "unparseable_line",
            "detail": "line does not parse as JSON and is skipped by every read",
            "line": 2,
            "seq": None,
            "report_key": None,
            "path": None,
            "blocks_feed": False,
        }
    ]


def test_eval_catalog_repair_plans_without_writing(monkeypatch, capsys, tmp_path):
    path = catalog_env(monkeypatch, tmp_path)
    line = catalog_line(1)
    path.write_text(line + "\n" + line + "\n", encoding="utf-8")
    before = path.read_bytes()

    rc = cli.main(["eval", "catalog", "repair"])

    # Non-zero: the plan has not run, so the catalog on disk still serves nothing.
    assert rc == 1
    out = capsys.readouterr().out
    assert "drop line 2: byte-identical repeat of line 1" in out
    assert "--confirm" in out
    assert path.read_bytes() == before


def test_eval_catalog_repair_confirm_applies_and_backs_up(
    monkeypatch, capsys, tmp_path
):
    path = catalog_env(monkeypatch, tmp_path)
    line = catalog_line(1)
    path.write_text(line + "\n" + line + "\n", encoding="utf-8")

    rc = cli.main(["eval", "catalog", "repair", "--confirm"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "repaired: dropped 1 line(s)" in out
    assert "serves feed: yes" in out
    assert path.read_text(encoding="utf-8") == line + "\n"
    backups = sorted(path.parent.glob("catalog.jsonl.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == line + "\n" + line + "\n"


def test_eval_catalog_repair_refuses_a_conflict_and_writes_nothing(
    monkeypatch, capsys, tmp_path
):
    path = catalog_env(monkeypatch, tmp_path)
    path.write_text(
        catalog_line(1)
        + "\n"
        + catalog_line(1, evaluation_id="20260730T110000Z")
        + "\n",
        encoding="utf-8",
    )
    before = path.read_bytes()

    rc = cli.main(["eval", "catalog", "repair", "--confirm"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "repair refused" in captured.err
    assert "resolved by hand" in captured.err
    assert "nothing was changed" in captured.out
    assert path.read_bytes() == before
    assert list(path.parent.glob("*.bak")) == []


@pytest.mark.parametrize("argv", [[], ["--confirm"], ["--json", "--confirm"]])
def test_eval_catalog_repair_reports_an_unreadable_catalog(
    monkeypatch, capsys, tmp_path, argv
):
    # Verify can describe a catalog it cannot read — that is a finding. Repair has
    # nothing to say about a file it never read, and an operator verb's answer to
    # that is one `error:` line and a non-zero exit, not a traceback.
    path = catalog_env(monkeypatch, tmp_path)
    path.mkdir()

    rc = cli.main(["eval", "catalog", "repair", *argv])

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: unreadable catalog.jsonl")
    # Nothing half-said on stdout either: no plan, and no JSON a script would parse
    # as a result.
    assert captured.out == ""
    assert list(path.parent.glob("*.bak")) == []


def test_eval_catalog_verify_disabled_exits_nonzero(monkeypatch, capsys, tmp_path):
    catalog_env(monkeypatch, tmp_path)
    monkeypatch.setenv(EVAL_ENABLED_ENV, "0")

    rc = cli.main(["eval", "catalog", "verify"])

    assert rc == 1
    assert "disabled" in capsys.readouterr().err


def test_eval_catalog_verify_reports_config_error(monkeypatch, capsys, tmp_path):
    catalog_env(monkeypatch, tmp_path)
    monkeypatch.setenv(EVAL_RETENTION_ENV, "lots")

    rc = cli.main(["eval", "catalog", "verify"])

    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_version_reports_the_running_runtime(capsys):
    rc = cli.main(["version"])

    assert rc == 0
    out = capsys.readouterr().out
    assert __version__ in out
    # A source checkout must not claim to be a compiled release.
    assert "source" in out


def test_version_json_is_the_runtime_identity(capsys):
    rc = cli.main(["version", "--json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {
        "version": __version__,
        "runtime": "source",
        "python_version": "%d.%d.%d" % sys.version_info[:3],
    }


def test_serve_passes_explicit_bind_arguments(monkeypatch):
    # `serve` exists so a runtime with no console scripts can start the service;
    # it must reach the same entry point `orch-hub-api` uses.
    calls: list[dict] = []
    monkeypatch.setattr(
        "orch_hub.api.run", lambda host=None, port=None: calls.append(
            {"host": host, "port": port}
        )
    )

    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "9999"]) == 0
    assert calls == [{"host": "0.0.0.0", "port": 9999}]


def test_serve_defaults_to_environment_configuration(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        "orch_hub.api.run", lambda host=None, port=None: calls.append(
            {"host": host, "port": port}
        )
    )

    assert cli.main(["serve"]) == 0
    # None means "let the service read ORCH_HUB_HOST/ORCH_HUB_PORT" rather than
    # a CLI default shadowing the deployment's configuration.
    assert calls == [{"host": None, "port": None}]


def test_serve_reports_an_unbindable_address_as_an_operational_error(monkeypatch, capsys):
    def refuse(host=None, port=None):
        raise OSError("address already in use")

    monkeypatch.setattr("orch_hub.api.run", refuse)

    assert cli.main(["serve"]) == 1
    assert capsys.readouterr().err.startswith("error: address already in use")
