"""Tests for launch-readiness checks.

Builds fake target repos in ``tmp_path`` and injects a fake runner for the git
and ``mandrel status`` checks; never touches real repos or subprocesses.
"""

from __future__ import annotations

from pathlib import Path

from orch_hub.config import Config
from orch_hub.models import ManagedRepo
from orch_hub.readiness import check_readiness
from orch_hub.subprocess_utils import CommandNotFoundError, CommandResult


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


def make_runner(*, git_out="", git_rc=0, aii_rc=0, aii_out="", aii_err=""):
    """A fake runner that distinguishes the git and mandrel subprocess calls."""

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if argv[0] == "git":
            return CommandResult(tuple(argv), git_rc, git_out, "")
        return CommandResult(tuple(argv), aii_rc, aii_out, aii_err)

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


def test_aii_status_failure(tmp_path):
    repo = build_ready_repo(tmp_path)
    drift = "repo: drift detected (4 issues)\n  target modified:\n    - a.py\n"
    report = check_readiness(
        repo, config(tmp_path), runner=make_runner(aii_rc=1, aii_out=drift)
    )
    assert not report.ready
    fail = {c.name: c for c in report.failures}
    assert "aii_status" in fail
    # Concise: only the first line of drift output is surfaced.
    assert "drift detected (4 issues)" in fail["aii_status"].message
    assert "target modified" not in fail["aii_status"].message


def test_aii_executable_missing(tmp_path):
    repo = build_ready_repo(tmp_path)

    def runner(args, **kwargs):
        argv = [str(a) for a in args]
        if argv[0] == "git":
            return CommandResult(tuple(argv), 0, "", "")
        raise CommandNotFoundError("mandrel")

    report = check_readiness(repo, config(tmp_path), runner=runner)
    assert "aii_status" in failure_names(report)


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
    assert "aii_status" in names
    assert calls == []  # runner never invoked when the repo path is missing
