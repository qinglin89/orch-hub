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
from orch_hub import semantic_jobs
from orch_hub.config import (
    BARK_URL_ENV,
    DATA_DIR_ENV,
    DEFAULT_DATA_DIR,
    EVAL_AUTO_ENV,
    EVAL_ENABLED_ENV,
    EVAL_QUEUE_MAX_AGE_ENV,
    EVAL_RETENTION_ENV,
    LEGACY_MANDREL_PATH_ENV,
    MANDREL_PATH_ENV,
    PORT_ENV,
    TOKEN_ENV,
)
from orch_hub.eval_control import EVAL_CONTROL_NAME
from orch_hub.eval_dispatch import QUEUE_ENTRY_LIMIT
from orch_hub.install import InstallError
from orch_hub.launchd import LaunchdError, PLIST_NAME
from orch_hub.models import (
    CommitInfo,
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
    SemanticJob,
)
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
                "placed_line": None,
                "placement_finding": None,
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
        program="/tmp/.venv/bin/orch-hub-api",
        mandrel_path=Path("/p/mandrel"),
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


# -- eval queue (the automatic Layer 2 path's read surface) -------------------
#
# Driven against a real hub data dir through the manager's own record writer, so
# the CLI parses exactly the bytes the service persists rather than a shape a
# fixture and the test agreed on between themselves. The projection underneath
# (``queue_snapshot``) is covered in test_eval_dispatch; what is asserted here is
# this command's own contract — what an operator reads, that the census carries
# its attempts caveat, that both holds on the drain are always stated, and that
# a read changes nothing on disk.


def queue_env(monkeypatch, tmp_path: Path) -> Path:
    """Point the CLI at an empty hub data dir; return its semantic-jobs root."""
    monkeypatch.setenv(EVAL_ENABLED_ENV, "1")
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    for name in (EVAL_RETENTION_ENV, EVAL_AUTO_ENV, EVAL_QUEUE_MAX_AGE_ENV):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "semantic-jobs"


def write_queue_job(jobs_root: Path, repo_name: str, task_id: str, **fields) -> Path:
    """Persist one job record through the writer the manager itself uses."""
    job_dir = jobs_root / repo_name / task_id
    job_dir.mkdir(parents=True, exist_ok=True)
    semantic_jobs._write_record(
        job_dir,
        SemanticJob(
            job_id=f"{task_id}-job",
            repo_name=repo_name,
            repo_path=Path("/repos") / repo_name,
            task_id=task_id,
            **fields,
        ),
    )
    return job_dir


def test_eval_queue_reports_the_census_the_holds_and_the_waiting_jobs(
    monkeypatch, capsys, tmp_path
):
    jobs_root = queue_env(monkeypatch, tmp_path)
    write_queue_job(
        jobs_root,
        "alpha",
        "2026-08-01-a-task",
        status="deferred",
        defer_reason="capacity",
        auto=True,
        created_ts="2026-08-31T20:00:00+00:00",
    )
    write_queue_job(
        jobs_root,
        "beta",
        "2026-08-02-b-task",
        status="completed",
        created_ts="2026-08-30T09:00:00+00:00",
    )
    write_queue_job(
        jobs_root,
        "gamma",
        "2026-07-01-c-task",
        status="skipped",
        skip_reason="run-opt-out",
        created_ts="2026-08-01T09:00:00+00:00",
    )

    rc = cli.main(["eval", "queue"])

    assert rc == 0
    out = capsys.readouterr().out
    # Where it was read from: the data dir is env-resolved, so a shell pointed
    # somewhere other than the service's own hub must be able to tell.
    assert str(jobs_root) in out
    # The full census, zeros included — every status stated so the line is stable
    # and greppable across reads.
    assert "attempts: deferred 1, queued 0, running 0, completed 1, failed 0" in out
    assert "cancelled 0, skipped 1, error 0" in out
    # Both holds on the drain, stated whether or not they are engaged.
    assert "automatic dispatch: on" in out
    assert "pause: not paused" in out
    # A resting outcome is counted, split per repo, and kept out of the rows.
    assert "skipped by repo (Layer 2 declined at launch): gamma 1" in out
    assert "2026-07-01-c-task" not in out
    assert "2026-08-02-b-task" not in out
    # The one job still owed a turn, with the machine reason it is waiting.
    assert "deferred" in out and "alpha/2026-08-01-a-task" in out
    assert "reason=capacity" in out


def test_eval_queue_labels_the_census_as_attempts_not_reports(
    monkeypatch, capsys, tmp_path
):
    """The caveat travels with the figures, on every surface that prints them.

    One record per (repo, task) holding that task's LATEST attempt: a manual
    re-run overwrites a completed record while the report it pointed at stays in
    the task's evaluation history, so reading `completed` as an evaluated-corpus
    denominator is wrong in both directions. A count without that sentence beside
    it is the misreading waiting to happen.
    """
    jobs_root = queue_env(monkeypatch, tmp_path)
    write_queue_job(
        jobs_root,
        "alpha",
        "2026-08-01-a-task",
        status="completed",
        evaluation_id="20260801T090000Z",
        created_ts="2026-08-01T08:00:00+00:00",
    )

    assert cli.main(["eval", "queue"]) == 0

    out = capsys.readouterr().out
    assert "latest attempt" in out
    assert "not a count of tasks carrying a report" in out


def test_eval_queue_counts_a_status_this_build_does_not_know(
    monkeypatch, capsys, tmp_path
):
    """A record from a newer build, or a damaged one, is counted under its own key
    rather than dropped — the census's own rule, and the whole point of a surface
    whose job is to make absences countable."""
    jobs_root = queue_env(monkeypatch, tmp_path)
    write_queue_job(
        jobs_root,
        "alpha",
        "2026-08-01-a-task",
        status="reticulating",
        created_ts="2026-08-31T20:00:00+00:00",
    )

    assert cli.main(["eval", "queue"]) == 0

    out = capsys.readouterr().out
    # The known lifecycle first, in its own order; the unknown after it.
    assert "error 0, reticulating 1" in out
    assert "reticulating  alpha/2026-08-01-a-task" in out


def test_eval_queue_json_is_the_queue_snapshot_payload(monkeypatch, capsys, tmp_path):
    """``--json`` is the HTTP payload's model, minus the ``enabled`` envelope field
    a disabled subsystem never gets this far to contradict — so one parser reads
    the route and the command."""
    jobs_root = queue_env(monkeypatch, tmp_path)
    write_queue_job(
        jobs_root,
        "alpha",
        "2026-08-01-a-task",
        status="deferred",
        defer_reason="pacing",
        auto=True,
        created_ts="2026-08-31T20:00:00+00:00",
    )

    assert cli.main(["eval", "queue", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert set(data) == {
        "auto_enabled",
        "control",
        "queue_max_age_seconds",
        "counts",
        "skipped_by_repo",
        "overdue",
        "entries",
        "truncated",
    }
    assert data["counts"]["deferred"] == 1
    assert data["control"] == {"paused": False, "paused_ts": None, "reason": None}
    entry = data["entries"][0]
    assert entry["job"]["task_id"] == "2026-08-01-a-task"
    assert entry["job"]["defer_reason"] == "pacing"


def test_eval_queue_reports_an_operator_pause_and_when_it_started(
    monkeypatch, capsys, tmp_path
):
    queue_env(monkeypatch, tmp_path)
    (tmp_path / EVAL_CONTROL_NAME).write_text(
        json.dumps(
            {
                "paused": True,
                "paused_ts": "2026-09-01T08:14:00+00:00",
                "reason": "operator",
            }
        ),
        encoding="utf-8",
    )

    assert cli.main(["eval", "queue"]) == 0

    out = capsys.readouterr().out
    assert "pause: paused since 2026-09-01T08:14:00+00:00 (operator)" in out
    # The difference from ORCH_HUB_EVAL_AUTO=false, which is the whole reason the
    # pause exists: a paused queue keeps filling.
    assert "completions still enter the queue" in out


def test_eval_queue_names_an_unreadable_control_as_damage_not_a_decision(
    monkeypatch, capsys, tmp_path
):
    """An unreadable control record holds the queue — the fail-toward-deferral
    direction — but it is nobody's choice, and reporting it as one sends the
    operator looking for a decision they never made."""
    queue_env(monkeypatch, tmp_path)
    (tmp_path / EVAL_CONTROL_NAME).write_text("{not json", encoding="utf-8")

    assert cli.main(["eval", "queue"]) == 0

    out = capsys.readouterr().out
    assert "unreadable-control" in out
    assert "could not read its pause control" in out
    assert "resume rewrites it" in out


def test_eval_queue_flags_a_job_stalled_past_the_configured_bound(
    monkeypatch, capsys, tmp_path
):
    jobs_root = queue_env(monkeypatch, tmp_path)
    monkeypatch.setenv(EVAL_QUEUE_MAX_AGE_ENV, "3600")
    write_queue_job(
        jobs_root,
        "alpha",
        "2026-08-01-a-task",
        status="deferred",
        defer_reason="usage-unknown",
        auto=True,
        # Long before any clock this test could run under.
        created_ts="2026-08-01T09:00:00+00:00",
    )

    assert cli.main(["eval", "queue"]) == 0

    out = capsys.readouterr().out
    assert "overdue: 1 waiting longer than 1h" in out
    assert "the queue is not draining" in out
    assert "[stalled]" in out


def test_eval_queue_says_when_the_row_list_was_capped(monkeypatch, capsys, tmp_path):
    """The rows are capped for readability; the counts are not. A surface that
    silently showed the first N would report a deep queue as calm."""
    jobs_root = queue_env(monkeypatch, tmp_path)
    for index in range(QUEUE_ENTRY_LIMIT + 3):
        write_queue_job(
            jobs_root,
            "alpha",
            f"2026-08-{index + 1:02d}-task",
            status="deferred",
            defer_reason="capacity",
            auto=True,
            created_ts="2026-08-31T20:00:00+00:00",
        )

    assert cli.main(["eval", "queue"]) == 0

    out = capsys.readouterr().out
    assert f"attempts: deferred {QUEUE_ENTRY_LIMIT + 3}" in out
    assert f"showing the first {QUEUE_ENTRY_LIMIT}" in out
    assert "the counts above are complete" in out


def test_eval_queue_on_an_empty_store_reads_calm_and_writes_nothing(
    monkeypatch, capsys, tmp_path
):
    """A read must not be the thing that creates the tree it reports on: an
    operator running this against the wrong data dir should learn that, not
    silently materialize a second hub's worth of empty state."""
    data_dir = tmp_path / "never-used"
    monkeypatch.setenv(EVAL_ENABLED_ENV, "1")
    monkeypatch.setenv(DATA_DIR_ENV, str(data_dir))
    monkeypatch.delenv(EVAL_RETENTION_ENV, raising=False)

    assert cli.main(["eval", "queue"]) == 0

    assert not data_dir.exists()
    out = capsys.readouterr().out
    assert "attempts: deferred 0" in out
    assert "jobs: nothing waiting" in out


@pytest.mark.parametrize("refused", ["the jobs root", "a repo dir"])
def test_eval_queue_says_when_the_store_cannot_be_walked(
    monkeypatch, capsys, tmp_path, refused
):
    """A tree that cannot be traversed is an operational failure, not a calm queue.

    The counterpart to the test above it: an *absent* store is honestly empty, an
    unreadable one is not, and the walk is two ``iterdir()`` levels (repo slug,
    then task id) either of which can fault. Getting this wrong is worse here than
    anywhere else in the CLI — the moment the store will not open is the moment
    this command is being run *because* the hub is misbehaving, and the two ways
    to be wrong are a traceback and, if the error were swallowed, the sentence
    "nothing waiting" about a queue nobody managed to read.

    Injected at the syscall boundary rather than through ``chmod``, which root
    ignores; the real walk, the real reader, and the real command all run.
    """
    jobs_root = queue_env(monkeypatch, tmp_path)
    write_queue_job(
        jobs_root,
        "alpha",
        "2026-08-01-a-task",
        status="deferred",
        defer_reason="capacity",
        auto=True,
        created_ts="2026-08-31T20:00:00+00:00",
    )
    blocked = jobs_root if refused == "the jobs root" else jobs_root / "alpha"
    real_iterdir = Path.iterdir

    def refuse(self):
        if self == blocked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", refuse)

    assert cli.main(["eval", "queue"]) == 1
    assert cli.main(["eval", "queue", "--json"]) == 1

    captured = capsys.readouterr()
    # Nothing on stdout in either form: no census over records that were never
    # counted, and no partial JSON for a parser to read as a queue.
    assert captured.out == ""
    assert captured.err.count("error: cannot read the evaluation queue") == 2
    # The tree it failed on, and what the filesystem said about it.
    assert str(jobs_root) in captured.err
    assert "Permission denied" in captured.err


def test_eval_queue_leaves_every_record_and_the_control_untouched(
    monkeypatch, capsys, tmp_path
):
    jobs_root = queue_env(monkeypatch, tmp_path)
    job_dir = write_queue_job(
        jobs_root,
        "alpha",
        "2026-08-01-a-task",
        status="running",
        started_ts="2026-08-31T20:01:00+00:00",
        deadline_ts="2026-08-31T20:31:00+00:00",
        created_ts="2026-08-31T20:00:00+00:00",
    )
    record = (job_dir / "job.json").read_bytes()
    control = tmp_path / EVAL_CONTROL_NAME
    control.write_text(json.dumps({"paused": False}), encoding="utf-8")
    control_bytes = control.read_bytes()

    assert cli.main(["eval", "queue"]) == 0
    assert cli.main(["eval", "queue", "--json"]) == 0

    assert (job_dir / "job.json").read_bytes() == record
    assert control.read_bytes() == control_bytes
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        EVAL_CONTROL_NAME,
        "semantic-jobs",
    ]


def test_eval_queue_disabled_exits_nonzero(monkeypatch, capsys, tmp_path):
    queue_env(monkeypatch, tmp_path)
    monkeypatch.setenv(EVAL_ENABLED_ENV, "0")

    rc = cli.main(["eval", "queue"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "no evaluation queue" in err


def test_eval_queue_reports_config_error(monkeypatch, capsys, tmp_path):
    queue_env(monkeypatch, tmp_path)
    monkeypatch.setenv(EVAL_QUEUE_MAX_AGE_ENV, "a while")

    rc = cli.main(["eval", "queue"])

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


# --------------------------------------------------------------------------
# installed-runtime commands
# --------------------------------------------------------------------------


def _fake_installation(**overrides) -> SimpleNamespace:
    values = {
        "prefix": Path("/home/u/.local/orch-hub"),
        "versions": ("0.1.0", "0.2.0"),
        "current": "0.2.0",
        "previous": "0.1.0",
    }
    values.update(overrides)
    return SimpleNamespace(
        **values,
        to_dict=lambda: {
            "prefix": str(values["prefix"]),
            "versions": list(values["versions"]),
            "current": values["current"],
            "previous": values["previous"],
        },
    )


def _fake_setup_result(health: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        installation=_fake_installation(),
        deployment=_fake_deployment(),
        generated_token=True,
        health=health,
    )


def test_install_cli_reports_the_command_and_path_hint(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "install_version",
        lambda payload, prefix=None, bin_dir=None, link=True: SimpleNamespace(
            installation=_fake_installation(current="0.2.0"),
            version="0.2.0",
            replaced=False,
            link_path=Path("/home/u/.local/bin/orch-hub"),
            link_on_path=False,
            to_dict=lambda: {"version": "0.2.0"},
        ),
    )

    rc = cli.main(["install", "--from", "/tmp/payload"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "installed orch-hub 0.2.0" in out
    assert "command: /home/u/.local/bin/orch-hub" in out
    assert 'export PATH="/home/u/.local/bin:$PATH"' in out
    assert "orch-hub setup --mandrel" in out


def test_install_cli_json(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "install_version",
        lambda payload, prefix=None, bin_dir=None, link=True: SimpleNamespace(
            to_dict=lambda: {"version": "0.2.0", "replaced": True}
        ),
    )

    rc = cli.main(["install", "--from", "/tmp/payload", "--json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"version": "0.2.0", "replaced": True}


def test_install_cli_error(monkeypatch, capsys):
    def boom(payload, prefix=None, bin_dir=None, link=True):
        raise cli.InstallError("payload directory not found: /tmp/payload")

    monkeypatch.setattr(cli, "install_version", boom)

    rc = cli.main(["install", "--from", "/tmp/payload"])

    assert rc == 1
    assert "error: payload directory not found" in capsys.readouterr().err


def test_setup_cli(monkeypatch, capsys):
    monkeypatch.setattr(cli, "setup_service", lambda options: _fake_setup_result())

    rc = cli.main(["setup", "--mandrel", "/p/mandrel"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "orch-hub setup ok" in out
    assert "version: 0.2.0" in out
    assert "program: /tmp/.venv/bin/orch-hub-api" in out
    assert "generated ORCH_HUB_TOKEN: generated" in out
    assert "bark push: disabled" in out
    assert "health: ok" in out


def test_setup_cli_fails_when_the_service_does_not_answer(monkeypatch, capsys):
    """A scripted install must notice a service that never came up."""
    monkeypatch.setattr(
        cli, "setup_service", lambda options: _fake_setup_result("unreachable: refused")
    )

    rc = cli.main(["setup"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "health: unreachable: refused" in captured.out
    assert "error: service did not become healthy" in captured.err


def test_setup_cli_error(monkeypatch, capsys):
    def boom(options):
        raise LaunchdError("mandrel checkout not found: /p/mandrel")

    monkeypatch.setattr(cli, "setup_service", boom)

    rc = cli.main(["setup", "--mandrel", "/p/mandrel"])

    assert rc == 1
    assert "error: mandrel checkout not found" in capsys.readouterr().err


def test_setup_cli_never_prints_the_bark_url(monkeypatch, capsys):
    secret = "https://api.day.app/DEVICEKEY"
    result = _fake_setup_result()
    result.deployment.bark_url = secret
    monkeypatch.setattr(cli, "setup_service", lambda options: result)

    cli.main(["setup"])

    out = capsys.readouterr().out
    assert secret not in out
    assert "bark push: enabled" in out


def test_upgrade_cli(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "upgrade",
        lambda payload, prefix=None, bin_dir=None, plist_path=None: SimpleNamespace(
            install=SimpleNamespace(version="0.2.0"),
            setup=_fake_setup_result(),
            previous_version="0.1.0",
        ),
    )

    rc = cli.main(["upgrade", "--from", "/tmp/payload"])

    assert rc == 0
    assert "upgraded orch-hub 0.1.0 -> 0.2.0" in capsys.readouterr().out


def test_upgrade_cli_error_names_the_rollback(monkeypatch, capsys):
    def boom(payload, prefix=None, bin_dir=None, plist_path=None):
        raise cli.InstallError(
            "installed 0.2.0 but could not start it: nope; the previous version "
            "is still installed — run `orch-hub rollback --to 0.1.0`"
        )

    monkeypatch.setattr(cli, "upgrade", boom)

    rc = cli.main(["upgrade", "--from", "/tmp/payload"])

    assert rc == 1
    assert "rollback --to 0.1.0" in capsys.readouterr().err


def test_rollback_cli(monkeypatch, capsys):
    seen = {}

    def fake_rollback(version=None, prefix=None, plist_path=None):
        seen["version"] = version
        return SimpleNamespace(from_version="0.2.0", to_version="0.1.0", health="ok")

    monkeypatch.setattr(cli, "rollback", fake_rollback)

    rc = cli.main(["rollback", "--to", "0.1.0"])

    assert rc == 0
    assert seen["version"] == "0.1.0"
    assert "rolled back orch-hub 0.2.0 -> 0.1.0" in capsys.readouterr().out


def test_rollback_cli_error(monkeypatch, capsys):
    def boom(version=None, prefix=None, plist_path=None):
        raise cli.InstallError("no previous version recorded under /p")

    monkeypatch.setattr(cli, "rollback", boom)

    rc = cli.main(["rollback"])

    assert rc == 1
    assert "error: no previous version recorded" in capsys.readouterr().err


def test_versions_cli(monkeypatch, capsys):
    monkeypatch.setattr(cli, "read_installation", lambda prefix: _fake_installation())

    rc = cli.main(["versions"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "0.1.0 (previous)" in out
    assert "0.2.0 (current)" in out


def test_versions_cli_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "read_installation", lambda prefix: _fake_installation())

    rc = cli.main(["versions", "--json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["current"] == "0.2.0"


def test_versions_cli_with_nothing_installed(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "read_installation",
        lambda prefix: _fake_installation(versions=(), current=None, previous=None),
    )

    rc = cli.main(["versions"])

    assert rc == 0
    assert "no versions installed" in capsys.readouterr().out


def test_uninstall_cli_keeps_data_by_default(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "uninstall",
        lambda prefix=None, bin_dir=None, plist_path=None, purge_data=False: SimpleNamespace(
            prefix=Path("/p"),
            prefix_removed=True,
            plist_path=Path("/pl"),
            plist_removed=True,
            link_path=Path("/b/orch-hub"),
            link_removed=True,
            data_dir=None,
            data_removed=False,
        ),
    )

    rc = cli.main(["uninstall"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "orch-hub uninstalled" in out
    assert "data dir: kept" in out
    assert "orchestrator runs were not stopped" in out


def test_uninstall_cli_requires_confirmation_to_purge_data(monkeypatch, capsys):
    def unreached(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("uninstall ran without confirmation")

    monkeypatch.setattr(cli, "uninstall", unreached)

    rc = cli.main(["uninstall", "--purge-data"])

    assert rc == 1
    assert "pass --yes to confirm" in capsys.readouterr().err


def test_uninstall_cli_reports_a_purge_and_a_foreign_command_link(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "uninstall",
        lambda prefix=None, bin_dir=None, plist_path=None, purge_data=False: SimpleNamespace(
            prefix=Path("/p"),
            prefix_removed=True,
            plist_path=Path("/pl"),
            plist_removed=False,
            link_path=Path("/b/orch-hub"),
            link_removed=False,
            data_dir=Path("/d"),
            data_removed=True,
        ),
    )

    rc = cli.main(["uninstall", "--purge-data", "--yes"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "command link: left /b/orch-hub (not ours)" in out
    assert "data dir: removed /d" in out


def test_uninstall_cli_reports_a_refused_removal(monkeypatch, capsys):
    """A refusal is an error exit, not a summary claiming things were removed."""

    def refuse(**kwargs):
        raise InstallError("cannot stop the com.qinglin89.orch-hub service")

    monkeypatch.setattr(cli, "uninstall", refuse)

    rc = cli.main(["uninstall"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "error: cannot stop" in captured.err
    assert "orch-hub uninstalled" not in captured.out
