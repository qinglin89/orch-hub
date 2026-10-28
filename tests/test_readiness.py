"""Tests for launch-readiness checks.

Builds fake target repos in ``tmp_path`` and injects a fake runner for the git
and ``mandrel status`` checks; never touches real repos or subprocesses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orch_hub.config import Config
from orch_hub.models import (
    ManagedRepo,
    ReadinessCheck,
    ReadinessReport,
    repos_payload,
)
import orch_hub.readiness as readiness_module
from orch_hub.readiness import check_readiness
from orch_hub.subprocess_utils import CommandNotFoundError, CommandResult, run_command


def build_ready_repo(tmp_path: Path) -> ManagedRepo:
    """Create a fake target repo with every deploy piece present."""
    repo = tmp_path / "repo"
    bin_dir = repo / ".mandrel" / "orchestrator" / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (repo / ".mandrel" / "orchestrator" / "orchestrator.py").write_text("# orch\n")
    (bin_dir / "python").write_text("")
    (repo / ".ai-tasks").mkdir(parents=True)
    (repo / ".ai-tasks" / "index.md").write_text("# tasks\n")
    manifest = repo / ".ai-deploy-manifest.json"
    manifest.write_text("{}\n")
    return ManagedRepo(name="repo", path=repo, manifest=manifest)


def make_runner(*, git_out="", git_rc=0, mandrel_rc=0, mandrel_out="", mandrel_err=""):
    """A fake runner that distinguishes the git and mandrel subprocess calls."""

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if argv[0] == "git":
            return CommandResult(tuple(argv), git_rc, git_out, "")
        return CommandResult(tuple(argv), mandrel_rc, mandrel_out, mandrel_err)

    return runner


def config(tmp_path: Path) -> Config:
    return Config(mandrel_path=tmp_path)


def failure_names(report) -> list[str]:
    return [c.name for c in report.failures]


def test_ready_repo_all_pass(tmp_path):
    repo = build_ready_repo(tmp_path)
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert report.ready
    assert report.failures == ()
    assert report.to_dict() == {"ready": True, "checks": []}
    # Stable seven-check shape.
    assert len(report.checks) == 7


def test_check_names_are_the_stable_user_visible_labels(tmp_path):
    """Check names are product language, not internal identifiers.

    The CLI, repo cards, repo detail, the intake refusal block, and the
    Overview attention rows all print ``name`` verbatim, so both the spelling
    and the order are a user-visible contract. Pinned here so a rename cannot
    reach the dashboard unnoticed.
    """
    repo = build_ready_repo(tmp_path)
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        calls.append(argv)
        return CommandResult(tuple(argv), 0, "", "")

    report = check_readiness(repo, config(tmp_path), runner=runner)
    assert [c.name for c in report.checks] == [
        "repo_path_exists",
        "manifest_exists",
        "orchestrator_exists",
        "venv_python_exists",
        "task_index_exists",
        "git_clean",
        "mandrel_status",
    ]
    # The check is named after the command it runs: `mandrel status <repo>`,
    # invoked by absolute path inside the mandrel checkout.
    assert calls[-1] == [
        str(config(tmp_path).mandrel_bin),
        "status",
        str(repo.path),
    ]


def test_missing_manifest(tmp_path):
    repo = build_ready_repo(tmp_path)
    repo.manifest.unlink()
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert not report.ready
    assert "manifest_exists" in failure_names(report)


def test_missing_orchestrator(tmp_path):
    repo = build_ready_repo(tmp_path)
    (repo.path / ".mandrel" / "orchestrator" / "orchestrator.py").unlink()
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert not report.ready
    assert "orchestrator_exists" in failure_names(report)


def test_missing_venv_python(tmp_path):
    repo = build_ready_repo(tmp_path)
    (repo.path / ".mandrel" / "orchestrator" / ".venv" / "bin" / "python").unlink()
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert not report.ready
    assert "venv_python_exists" in failure_names(report)


def test_missing_task_index(tmp_path):
    repo = build_ready_repo(tmp_path)
    (repo.path / ".ai-tasks" / "index.md").unlink()
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert not report.ready
    assert "task_index_exists" in failure_names(report)


def test_dirty_git_tree(tmp_path):
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo, config(tmp_path), runner=make_runner(git_out=" M file.py\n")
    )
    assert not report.ready
    fail = {c.name: c for c in report.failures}
    assert "git_clean" in fail
    assert "uncommitted" in fail["git_clean"].message


def test_git_status_command_failure(tmp_path):
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo, config(tmp_path), runner=make_runner(git_rc=128)
    )
    assert "git_clean" in failure_names(report)


def test_git_executable_missing(tmp_path):
    repo = build_ready_repo(tmp_path)

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if argv[0] == "git":
            raise CommandNotFoundError("git")
        return CommandResult(tuple(argv), 0, "", "")

    report = check_readiness(repo, config(tmp_path), runner=runner)
    assert "git_clean" in failure_names(report)


def test_mandrel_status_failure(tmp_path):
    repo = build_ready_repo(tmp_path)
    drift = "repo: drift detected (4 issues)\n  target modified:\n    - a.py\n"
    report = check_readiness(
        repo, config(tmp_path), runner=make_runner(mandrel_rc=1, mandrel_out=drift)
    )
    assert not report.ready
    fail = {c.name: c for c in report.failures}
    assert "mandrel_status" in fail
    # Concise: only the first line of drift output is surfaced.
    assert "drift detected (4 issues)" in fail["mandrel_status"].message
    assert "target modified" not in fail["mandrel_status"].message
    # The public payload the CLI and dashboard render carries the same key.
    assert [c["name"] for c in report.to_dict()["checks"]] == ["mandrel_status"]


def test_mandrel_executable_missing(tmp_path):
    repo = build_ready_repo(tmp_path)

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if argv[0] == "git":
            return CommandResult(tuple(argv), 0, "", "")
        raise CommandNotFoundError("mandrel")

    report = check_readiness(repo, config(tmp_path), runner=runner)
    assert "mandrel_status" in failure_names(report)


def test_missing_repo_path_skips_subprocess_checks(tmp_path):
    missing = tmp_path / "gone"
    repo = ManagedRepo(
        name="gone",
        path=missing,
        manifest=missing / ".ai-deploy-manifest.json",
    )

    calls = []

    def runner(args, **kwargs):
        calls.append([str(a) for a in args])
        return CommandResult(("x",), 0, "", "")

    report = check_readiness(repo, config(tmp_path), runner=runner)
    assert not report.ready
    names = failure_names(report)
    assert "repo_path_exists" in names
    # Subprocess checks are skipped but still recorded as failed (fail closed).
    assert "git_clean" in names
    assert "mandrel_status" in names
    assert calls == []  # runner never invoked when the repo path is missing


# ---- fix hints -----------------------------------------------------------------
#
# Every check that can fail carries a one-line, copy-pasteable fix. Hints are an
# additive field: names, pass/fail outcomes, and messages are unchanged (the
# dashboard matches on `git_clean`'s "uncommitted" message), and serialized
# forms carry `hint` only when one is set.


def hints(report) -> dict[str, str | None]:
    return {c.name: c.hint for c in report.failures}


def test_passing_checks_carry_no_hint(tmp_path):
    repo = build_ready_repo(tmp_path)
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert all(c.hint is None for c in report.checks)
    assert all("hint" not in c.to_dict() for c in report.checks)
    assert report.to_dict() == {"ready": True, "checks": []}


def test_missing_repo_path_hint_names_redeploy_and_registry_remove(tmp_path):
    missing = tmp_path / "gone"
    repo = ManagedRepo(
        name="gone", path=missing, manifest=missing / ".ai-deploy-manifest.json"
    )
    cfg = config(tmp_path)
    report = check_readiness(repo, cfg, runner=make_runner())
    got = hints(report)
    assert got["repo_path_exists"] == (
        "if the repo lives on a volume that is not mounted, mount it; otherwise"
        f" drop the stale entry with {cfg.mandrel_bin} registry remove {missing},"
        " and if the repo moved, register it again with"
        f" {cfg.mandrel_bin} deploy --bootstrap-orchestrator /new/path/to/repo"
    )
    # The subprocess checks it skipped point back at the root cause.
    skipped = "fix repo_path_exists first; this check runs once the path exists"
    assert got["git_clean"] == skipped
    assert got["mandrel_status"] == skipped


def test_missing_deploy_pieces_hint_bootstrap_deploy(tmp_path):
    repo = build_ready_repo(tmp_path)
    repo.manifest.unlink()
    (repo.path / ".mandrel" / "orchestrator" / "orchestrator.py").unlink()
    cfg = config(tmp_path)
    report = check_readiness(repo, cfg, runner=make_runner())
    expected = (
        f"run {cfg.mandrel_bin} deploy --bootstrap-orchestrator {repo.path}"
        " then commit .gitignore and .ai-deploy-lock.json"
    )
    got = hints(report)
    assert got["manifest_exists"] == expected
    assert got["orchestrator_exists"] == expected
    # The message is unchanged; the fix is a separate field.
    fail = {c.name: c for c in report.failures}
    assert fail["manifest_exists"].message == f"missing: {repo.manifest}"


def test_missing_venv_python_hint(tmp_path):
    repo = build_ready_repo(tmp_path)
    (repo.path / ".mandrel" / "orchestrator" / ".venv" / "bin" / "python").unlink()
    cfg = config(tmp_path)
    report = check_readiness(repo, cfg, runner=make_runner())
    assert hints(report)["venv_python_exists"] == (
        f"run {cfg.mandrel_bin} deploy --bootstrap-orchestrator {repo.path} to"
        " create the scheduler's Python environment (needs a Python 3.11+;"
        " --orchestrator-python /path/to/python picks one)"
    )


def test_missing_task_index_hint_points_at_ai_init(tmp_path):
    repo = build_ready_repo(tmp_path)
    (repo.path / ".ai-tasks" / "index.md").unlink()
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    assert hints(report)["task_index_exists"] == (
        f"initialize the repo: open your coding agent in {repo.path} and run"
        " /ai-init, then commit .ai/"
    )


def test_dirty_tree_hint_names_first_three_paths_plus_more(tmp_path):
    repo = build_ready_repo(tmp_path)
    porcelain = (
        " M .gitignore\n"  # leading status space must not eat the path
        "?? .ai-deploy-lock.json\n"
        "R  old name.py -> new.py\n"  # a rename stays as git prints it
        "A  four.py\n"
        " D five.py\n"
    )
    report = check_readiness(
        repo, config(tmp_path), runner=make_runner(git_out=porcelain)
    )
    fail = {c.name: c for c in report.failures}
    # The message other code matches on is unchanged.
    assert fail["git_clean"].message == "working tree has uncommitted changes"
    assert fail["git_clean"].hint == (
        "commit or stash the uncommitted changes: .gitignore,"
        " .ai-deploy-lock.json, old name.py -> new.py (+2 more); right after a"
        " mandrel deploy these are usually .gitignore and .ai-deploy-lock.json"
    )


def test_dirty_tree_hint_without_overflow_has_no_more_suffix(tmp_path):
    repo = build_ready_repo(tmp_path)
    report = check_readiness(
        repo, config(tmp_path), runner=make_runner(git_out=" M a.py\n M b.py\n")
    )
    hint = hints(report)["git_clean"]
    assert hint.startswith("commit or stash the uncommitted changes: a.py, b.py;")
    assert "more)" not in hint


def test_git_status_failure_hint(tmp_path):
    repo = build_ready_repo(tmp_path)
    report = check_readiness(repo, config(tmp_path), runner=make_runner(git_rc=128))
    assert hints(report)["git_clean"] == (
        f"check that the repo is a readable git work tree: git -C {repo.path} status"
    )


def test_git_missing_and_unrunnable_share_the_install_hint(tmp_path):
    repo = build_ready_repo(tmp_path)
    install = (
        "install git (xcode-select --install) and make sure it is on the PATH"
        " orch-hub runs with"
    )

    def missing(args, **kwargs):
        if str(args[0]) == "git":
            raise CommandNotFoundError("git")
        return CommandResult(tuple(map(str, args)), 0, "", "")

    def unrunnable(args, **kwargs):
        if str(args[0]) == "git":
            raise PermissionError(13, "Permission denied")
        return CommandResult(tuple(map(str, args)), 0, "", "")

    report = check_readiness(repo, config(tmp_path), runner=missing)
    fail = {c.name: c for c in report.failures}
    assert fail["git_clean"].message == "git executable not found"
    assert fail["git_clean"].hint == install

    # An OSError other than "not found" is a failed check too, never an escape.
    report = check_readiness(repo, config(tmp_path), runner=unrunnable)
    fail = {c.name: c for c in report.failures}
    assert list(fail) == ["git_clean"]
    assert fail["git_clean"].message == "git could not run: Permission denied"
    assert fail["git_clean"].hint == install


def test_mandrel_status_failure_hint(tmp_path):
    repo = build_ready_repo(tmp_path)
    cfg = config(tmp_path)
    report = check_readiness(
        repo, cfg, runner=make_runner(mandrel_rc=1, mandrel_out="drift\n")
    )
    assert hints(report)["mandrel_status"] == (
        f"run {cfg.mandrel_bin} status {repo.path} to see the drift; redeploy"
        f" with {cfg.mandrel_bin} deploy {repo.path} and commit"
        " .ai-deploy-lock.json"
    )


def test_mandrel_missing_hint_points_at_the_configured_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness_module, "is_frozen", lambda: True)
    repo = build_ready_repo(tmp_path)
    cfg = config(tmp_path)

    def runner(args, **kwargs):
        if str(args[0]) == "git":
            return CommandResult(tuple(map(str, args)), 0, "", "")
        raise CommandNotFoundError("mandrel")

    report = check_readiness(repo, cfg, runner=runner)
    fail = {c.name: c for c in report.failures}
    assert fail["mandrel_status"].message == f"mandrel not found: {cfg.mandrel_bin}"
    assert fail["mandrel_status"].hint == (
        f"point orch-hub at your mandrel checkout (ORCH_HUB_MANDREL_PATH is"
        f" {tmp_path}): orch-hub setup --mandrel /absolute/path/to/mandrel"
    )


def test_unrunnable_mandrel_is_a_failed_check_not_an_exception(tmp_path, monkeypatch):
    """A present-but-non-executable bin/mandrel raises PermissionError from the
    real subprocess wrapper; readiness must turn it into a failed check (it used
    to escape check_readiness and 500 GET /api/repos)."""
    monkeypatch.setattr(readiness_module, "is_frozen", lambda: True)
    repo = build_ready_repo(tmp_path)
    mandrel_root = tmp_path / "mandrel"
    cfg = Config(mandrel_path=mandrel_root)
    cfg.mandrel_bin.parent.mkdir(parents=True)
    cfg.mandrel_bin.write_text("#!/bin/sh\nexit 0\n")
    cfg.mandrel_bin.chmod(0o644)  # present, not executable

    def runner(args, **kwargs):
        if str(args[0]) == "git":
            return CommandResult(tuple(map(str, args)), 0, "", "")
        # The real wrapper: exec of a non-executable file, never a real mandrel.
        return run_command(args, **kwargs)

    report = check_readiness(repo, cfg, runner=runner)
    fail = {c.name: c for c in report.failures}
    assert list(fail) == ["mandrel_status"]
    assert fail["mandrel_status"].message.startswith(
        f"mandrel could not run: {cfg.mandrel_bin}: "
    )
    assert fail["mandrel_status"].hint == (
        f"make sure {cfg.mandrel_bin} is the executable mandrel CLI (chmod +x"
        f" {cfg.mandrel_bin}), or point orch-hub at your mandrel checkout"
        f" (ORCH_HUB_MANDREL_PATH is {mandrel_root}): orch-hub setup --mandrel"
        " /absolute/path/to/mandrel"
    )


def test_a_checkout_service_is_reconfigured_with_launchd_update(tmp_path, monkeypatch):
    """`orch-hub setup` needs an installed runtime; a checkout service has none."""
    monkeypatch.setattr(readiness_module, "is_frozen", lambda: False)
    repo = build_ready_repo(tmp_path)

    def runner(args, **kwargs):
        if str(args[0]) == "git":
            return CommandResult(tuple(map(str, args)), 0, "", "")
        raise CommandNotFoundError("mandrel")

    report = check_readiness(repo, config(tmp_path), runner=runner)
    assert hints(report)["mandrel_status"].endswith(
        "orch-hub launchd update --mandrel /absolute/path/to/mandrel"
    )


@pytest.mark.parametrize(
    ("rc", "out", "err"),
    [
        (1, "", "mandrel: needs Python 3.11 or newer and found none (tried ...)\n"),
        (127, "", "bin/mandrel: 17: exec: python: not found\n"),
        (1, "", "mandrel: MANDREL_PYTHON=/x is not a runnable Python 3.11 or newer\n"),
    ],
)
def test_a_mandrel_that_cannot_start_is_not_called_drift(tmp_path, rc, out, err):
    repo = build_ready_repo(tmp_path)
    cfg = config(tmp_path)

    report = check_readiness(
        repo, cfg, runner=make_runner(mandrel_rc=rc, mandrel_out=out, mandrel_err=err)
    )

    hint = hints(report)["mandrel_status"]
    assert hint.startswith("mandrel cannot start under the service's PATH")
    assert f"python3.13 -m venv {tmp_path}/.venv" in hint
    assert f"git -C {tmp_path} pull" in hint
    assert "drift" not in hint


def test_real_drift_keeps_the_redeploy_hint(tmp_path):
    repo = build_ready_repo(tmp_path)
    cfg = config(tmp_path)

    report = check_readiness(
        repo, cfg, runner=make_runner(mandrel_rc=1, mandrel_out="/x: drift detected (2 issues)\n")
    )

    assert hints(report)["mandrel_status"].startswith(f"run {cfg.mandrel_bin} status")


def test_hint_commands_quote_paths_with_spaces(tmp_path):
    spaced = tmp_path / "my repos"
    spaced.mkdir()
    repo = build_ready_repo(spaced)
    repo.manifest.unlink()
    cfg = config(tmp_path)
    report = check_readiness(repo, cfg, runner=make_runner())
    assert hints(report)["manifest_exists"] == (
        f"run {cfg.mandrel_bin} deploy --bootstrap-orchestrator '{repo.path}'"
        " then commit .gitignore and .ai-deploy-lock.json"
    )


def test_serialized_failures_include_hint_only_when_set():
    with_hint = ReadinessCheck("git_clean", False, "dirty", "commit it")
    without = ReadinessCheck("mandrel_status", False, "drift")
    passing = ReadinessCheck("repo_path_exists", True, "ok")
    # Positional construction (name, passed, message) still works.
    assert without.hint is None
    assert with_hint.to_dict() == {
        "name": "git_clean",
        "passed": False,
        "message": "dirty",
        "hint": "commit it",
    }
    assert without.to_dict() == {
        "name": "mandrel_status",
        "passed": False,
        "message": "drift",
    }
    report = ReadinessReport((passing, with_hint, without))
    assert report.to_dict() == {
        "ready": False,
        "checks": [
            {"name": "git_clean", "message": "dirty", "hint": "commit it"},
            {"name": "mandrel_status", "message": "drift"},
        ],
    }


def test_repos_payload_carries_hints_for_cli_and_api(tmp_path):
    # `repos --json` and GET /api/repos both serialize through repos_payload.
    repo = build_ready_repo(tmp_path)
    (repo.path / ".ai-tasks" / "index.md").unlink()
    report = check_readiness(repo, config(tmp_path), runner=make_runner())
    payload = repos_payload([(repo, report)])
    (item,) = payload["repos"][0]["readiness"]["checks"]
    assert item["name"] == "task_index_exists"
    assert item["message"] == f"missing: {repo.path / '.ai-tasks' / 'index.md'}"
    assert item["hint"].startswith("initialize the repo: open your coding agent in")
