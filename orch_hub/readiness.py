"""Launch-readiness checks for a managed repo.

Readiness fails closed: any missing deploy piece, a dirty working tree, a
failing ``mandrel status``, or a missing tool blocks launch. The report always
carries the same seven checks in a stable order so callers (CLI today, API and
dashboard later) can render a consistent shape.

Every failed check also carries a one-line fix ``hint`` (see the "fix hints"
section below, the single home of that wording). Hints are additive guidance:
they never change a check's name, pass/fail outcome, or message, which other
code matches on. They name commands by the configured absolute mandrel path and
stay free of secrets.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from .config import MANDREL_PATH_ENV, Config
from .models import ManagedRepo, ReadinessCheck, ReadinessReport
from .resources import is_frozen
from .subprocess_utils import CommandNotFoundError, Runner, run_command

# How many dirty paths the git_clean hint names before summarizing the rest.
_DIRTY_PATHS_SHOWN = 3


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
            None if path_exists else _repo_path_hint(path, config),
        )
    )
    deploy_hint = _deploy_hint(path, config)
    checks.append(_file_check("manifest_exists", repo.manifest, deploy_hint))
    checks.append(
        _file_check(
            "orchestrator_exists",
            path / ".mandrel" / "orchestrator" / "orchestrator.py",
            deploy_hint,
        )
    )
    checks.append(
        _file_check(
            "venv_python_exists",
            path / ".mandrel" / "orchestrator" / ".venv" / "bin" / "python",
            _venv_hint(path, config),
        )
    )
    checks.append(
        _file_check(
            "task_index_exists",
            path / ".ai-tasks" / "index.md",
            _task_index_hint(path),
        )
    )

    if path_exists:
        checks.append(_git_clean_check(path, runner))
        checks.append(_mandrel_status_check(repo, config, runner))
    else:
        reason = "skipped: repo path missing"
        checks.append(ReadinessCheck("git_clean", False, reason, _SKIPPED_HINT))
        checks.append(ReadinessCheck("mandrel_status", False, reason, _SKIPPED_HINT))

    return ReadinessReport(tuple(checks))


def _file_check(name: str, target: Path, hint: str) -> ReadinessCheck:
    exists = target.exists()
    return ReadinessCheck(
        name,
        exists,
        f"found: {target}" if exists else f"missing: {target}",
        None if exists else hint,
    )


def _git_clean_check(repo_path: Path, runner: Runner) -> ReadinessCheck:
    try:
        result = runner(["git", "status", "--porcelain"], cwd=repo_path)
    except CommandNotFoundError:
        # More specific than OSError (it subclasses FileNotFoundError): keep the
        # established "not found" wording for a missing executable.
        return ReadinessCheck(
            "git_clean", False, "git executable not found", _GIT_TOOL_HINT
        )
    except OSError as exc:
        # Present but unrunnable (permissions, bad interpreter, ...): fail the
        # check rather than let the error escape and 500 the repo listing.
        return ReadinessCheck(
            "git_clean",
            False,
            f"git could not run: {_os_error_text(exc)}",
            _GIT_TOOL_HINT,
        )
    if not result.ok:
        detail = (
            _first_line(result.stderr)
            or _first_line(result.stdout)
            or f"exit {result.returncode}"
        )
        return ReadinessCheck(
            "git_clean",
            False,
            f"git status failed: {detail}",
            _git_status_failed_hint(repo_path),
        )
    clean = result.stdout.strip() == ""
    return ReadinessCheck(
        "git_clean",
        clean,
        "working tree clean" if clean else "working tree has uncommitted changes",
        None if clean else _dirty_tree_hint(result.stdout),
    )


def _mandrel_status_check(
    repo: ManagedRepo, config: Config, runner: Runner
) -> ReadinessCheck:
    try:
        result = runner([config.mandrel_bin, "status", repo.path])
    except CommandNotFoundError:
        return ReadinessCheck(
            "mandrel_status",
            False,
            f"mandrel not found: {config.mandrel_bin}",
            _mandrel_location_hint(config),
        )
    except OSError as exc:
        # E.g. a present-but-non-executable bin/mandrel (PermissionError).
        return ReadinessCheck(
            "mandrel_status",
            False,
            f"mandrel could not run: {config.mandrel_bin}: {_os_error_text(exc)}",
            _mandrel_unrunnable_hint(config),
        )
    if result.ok:
        return ReadinessCheck("mandrel_status", True, "mandrel status ok")
    # mandrel prints drift detail to stdout; prefer it for the concise reason.
    detail = (
        _first_line(result.stdout)
        or _first_line(result.stderr)
        or f"exit {result.returncode}"
    )
    return ReadinessCheck(
        "mandrel_status",
        False,
        f"mandrel status failed: {detail}",
        _mandrel_runtime_hint(config)
        if _mandrel_did_not_start(result)
        else _mandrel_drift_hint(repo.path, config),
    )


def _mandrel_did_not_start(result) -> bool:
    """Whether the failure came from bin/mandrel's interpreter, not from status.

    Drift is reported on stdout with exit 1. A wrapper that cannot find (or an
    older one that cannot run) a Python prints only to stderr, or exits 126/127
    from the shell; redeploying a target would not help either.
    """
    if result.returncode in (126, 127):
        return True
    if result.stdout.strip():
        return False
    stderr = result.stderr
    return any(
        marker in stderr
        for marker in ("needs Python 3.11", "MANDREL_PYTHON=", "python: not found")
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _os_error_text(exc: OSError) -> str:
    return exc.strerror or str(exc) or type(exc).__name__


# ---- fix hints ----------------------------------------------------------------
#
# One line each, imperative, copy-pasteable where a command applies. Commands use
# the configured absolute mandrel path and shell-quote every path so a repo under
# a directory with spaces still pastes correctly. Nothing here reads secrets.

_SKIPPED_HINT = "fix repo_path_exists first; this check runs once the path exists"

_GIT_TOOL_HINT = (
    "install git (xcode-select --install) and make sure it is on the PATH"
    " orch-hub runs with"
)

def _orch_hub_setup_cmd() -> str:
    # A compiled install reconfigures with `setup`; a checkout-run service with
    # `launchd update` (setup needs an installed runtime it does not have).
    if is_frozen():
        return "orch-hub setup --mandrel /absolute/path/to/mandrel"
    return "orch-hub launchd update --mandrel /absolute/path/to/mandrel"


def _cmd(*parts: object) -> str:
    """A shell command line with every argument quoted only when it needs it."""
    return " ".join(shlex.quote(str(part)) for part in parts)


def _repo_path_hint(path: Path, config: Config) -> str:
    # Remove first either way: the registry de-duplicates by path, so a redeploy
    # from a new location alone would leave a second entry under the same name.
    remove = _cmd(config.mandrel_bin, "registry", "remove", path)
    redeploy = (
        _cmd(config.mandrel_bin, "deploy", "--bootstrap-orchestrator")
        + " /new/path/to/repo"
    )
    return (
        f"if the repo lives on a volume that is not mounted, mount it; otherwise"
        f" drop the stale entry with {remove}, and if the repo moved, register"
        f" it again with {redeploy}"
    )


def _deploy_hint(path: Path, config: Config) -> str:
    deploy = _cmd(config.mandrel_bin, "deploy", "--bootstrap-orchestrator", path)
    return f"run {deploy} then commit .gitignore and .ai-deploy-lock.json"


def _venv_hint(path: Path, config: Config) -> str:
    deploy = _cmd(config.mandrel_bin, "deploy", "--bootstrap-orchestrator", path)
    return (
        f"run {deploy} to create the scheduler's Python environment"
        " (needs a Python 3.11+; --orchestrator-python /path/to/python picks one)"
    )


def _task_index_hint(path: Path) -> str:
    return (
        f"initialize the repo: open your coding agent in {shlex.quote(str(path))}"
        " and run /ai-init, then commit .ai/"
    )


def _git_status_failed_hint(path: Path) -> str:
    status = _cmd("git", "-C", path, "status")
    return f"check that the repo is a readable git work tree: {status}"


def _dirty_tree_hint(porcelain: str) -> str:
    paths = _porcelain_paths(porcelain)
    shown = ", ".join(paths[:_DIRTY_PATHS_SHOWN])
    extra = len(paths) - _DIRTY_PATHS_SHOWN
    if extra > 0:
        shown += f" (+{extra} more)"
    listing = f": {shown}" if shown else ""
    return (
        f"commit or stash the uncommitted changes{listing}; right after a"
        " mandrel deploy these are usually .gitignore and .ai-deploy-lock.json"
    )


def _porcelain_paths(porcelain: str) -> list[str]:
    """Paths named by ``git status --porcelain`` output, in order.

    Each v1 line is a two-character status, a space, then the path, so the path
    is ``line[3:]``. A rename stays as git prints it (``old -> new``) and a quoted
    path keeps its quotes; both are display text here, never re-parsed. Lines are
    not stripped first: the leading status column is often a space.
    """
    return [line[3:] for line in porcelain.splitlines() if len(line) > 3]


def _mandrel_drift_hint(path: Path, config: Config) -> str:
    status = _cmd(config.mandrel_bin, "status", path)
    deploy = _cmd(config.mandrel_bin, "deploy", path)
    return (
        f"run {status} to see the drift; redeploy with {deploy}"
        " and commit .ai-deploy-lock.json"
    )


def _mandrel_runtime_hint(config: Config) -> str:
    venv = _cmd("python3.13", "-m", "venv", config.mandrel_path / ".venv")
    pull = _cmd("git", "-C", config.mandrel_path, "pull")
    return (
        "mandrel cannot start under the service's PATH: give its checkout an"
        f" interpreter with {venv} (any Python 3.11+); a mandrel that still runs"
        f" an unversioned python needs updating first: {pull}"
    )


def _mandrel_location_hint(config: Config) -> str:
    return (
        f"point orch-hub at your mandrel checkout ({MANDREL_PATH_ENV} is"
        f" {shlex.quote(str(config.mandrel_path))}): {_orch_hub_setup_cmd()}"
    )


def _mandrel_unrunnable_hint(config: Config) -> str:
    return (
        f"make sure {shlex.quote(str(config.mandrel_bin))} is the executable"
        f" mandrel CLI ({_cmd('chmod', '+x', config.mandrel_bin)}), or "
        + _mandrel_location_hint(config)
    )
