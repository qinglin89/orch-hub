"""Launch-readiness checks for a managed repo.

Readiness fails closed: any missing deploy piece, a dirty working tree, a
failing ``mandrel status``, or a missing tool blocks launch. The report always
carries the same seven checks in a stable order so callers (CLI today, API and
dashboard later) can render a consistent shape.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .models import ManagedRepo, ReadinessCheck, ReadinessReport
from .subprocess_utils import CommandNotFoundError, Runner, run_command


def check_readiness(
    repo: ManagedRepo,
    config: Config,
    *,
    runner: Runner = run_command,
) -> ReadinessReport:
    """Run all readiness checks for ``repo`` and aggregate a report.

    Filesystem checks always run. The git-clean and ``mandrel status`` checks run
    only when the repo path is a directory; otherwise they are recorded as
    failed (skipped) so the report keeps its seven-check shape and fails closed.
    """
    path = repo.path
    checks: list[ReadinessCheck] = []

    path_exists = path.is_dir()
    checks.append(
        ReadinessCheck(
            "repo_path_exists",
            path_exists,
            f"repo path present: {path}"
            if path_exists
            else f"repo path missing: {path}",
        )
    )
    checks.append(_file_check("manifest_exists", repo.manifest))
    checks.append(
        _file_check(
            "orchestrator_exists",
            path / ".cursor" / "orchestrator" / "orchestrator.py",
        )
    )
    checks.append(
        _file_check(
            "venv_python_exists",
            path / ".cursor" / "orchestrator" / ".venv" / "bin" / "python",
        )
    )
    checks.append(_file_check("task_index_exists", path / ".ai-tasks" / "index.md"))

    if path_exists:
        checks.append(_git_clean_check(path, runner))
        checks.append(_aii_status_check(repo, config, runner))
    else:
        reason = "skipped: repo path missing"
        checks.append(ReadinessCheck("git_clean", False, reason))
        checks.append(ReadinessCheck("aii_status", False, reason))

    return ReadinessReport(tuple(checks))


def _file_check(name: str, target: Path) -> ReadinessCheck:
    exists = target.exists()
    return ReadinessCheck(
        name,
        exists,
        f"found: {target}" if exists else f"missing: {target}",
    )


def _git_clean_check(repo_path: Path, runner: Runner) -> ReadinessCheck:
    try:
        result = runner(["git", "status", "--porcelain"], cwd=repo_path)
    except CommandNotFoundError:
        return ReadinessCheck("git_clean", False, "git executable not found")
    if not result.ok:
        detail = (
            _first_line(result.stderr)
            or _first_line(result.stdout)
            or f"exit {result.returncode}"
        )
        return ReadinessCheck("git_clean", False, f"git status failed: {detail}")
    clean = result.stdout.strip() == ""
    return ReadinessCheck(
        "git_clean",
        clean,
        "working tree clean" if clean else "working tree has uncommitted changes",
    )


def _aii_status_check(
    repo: ManagedRepo, config: Config, runner: Runner
) -> ReadinessCheck:
    try:
        result = runner([config.mandrel_bin, "status", repo.path])
    except CommandNotFoundError:
        return ReadinessCheck(
            "aii_status", False, f"mandrel not found: {config.mandrel_bin}"
        )
    if result.ok:
        return ReadinessCheck("aii_status", True, "mandrel status ok")
    # mandrel prints drift detail to stdout; prefer it for the concise reason.
    detail = (
        _first_line(result.stdout)
        or _first_line(result.stderr)
        or f"exit {result.returncode}"
    )
    return ReadinessCheck("aii_status", False, f"mandrel status failed: {detail}")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
