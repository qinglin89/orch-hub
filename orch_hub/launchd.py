"""macOS launchd operations for the orch-hub resident service.

The service has one launchd user-agent label, ``com.qinglin89.orch-hub``.
Stopping that label only unloads the hub HTTP service; it deliberately does not
call :class:`orch_hub.runs.RunManager`, write any run control files, or signal
orchestrator pids recorded under ``ORCH_HUB_DATA_DIR``. Orchestrators are
launched by the hub as detached subprocess sessions, so launchd operations stay
scoped to the hub process.
"""

from __future__ import annotations

import os
import plistlib
import secrets
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import (
    AI_NATIVE_PATH_ENV,
    DATA_DIR_ENV,
    DEFAULT_DATA_DIR,
    DEFAULT_HOST,
    DEFAULT_PORT,
    HOST_ENV,
    PORT_ENV,
    TOKEN_ENV,
    Config,
)
from .subprocess_utils import (
    CommandNotFoundError,
    CommandResult,
    Runner,
    run_command,
)

LABEL = "com.qinglin89.orch-hub"
PLIST_NAME = f"{LABEL}.plist"
DEFAULT_PATH_ENV = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TEMPLATE_RELATIVE_PATH = Path("deploy") / "launchd" / PLIST_NAME


class LaunchdError(RuntimeError):
    """Raised when a launchd operation cannot complete."""


@dataclass(frozen=True)
class DeployOptions:
    """User-configurable launchd deployment settings."""

    checkout: Path | None = None
    ai_native_path: Path | None = None
    token: str | None = None
    host: str | None = None
    port: int | None = None
    data_dir: Path | None = None
    plist_path: Path | None = None
    log_dir: Path | None = None
    path_env: str = DEFAULT_PATH_ENV
    install_package: bool = True


@dataclass(frozen=True)
class Deployment:
    """Fully resolved launchd deployment paths and environment."""

    checkout: Path
    ai_native_path: Path
    token: str
    token_source: str
    host: str
    port: int
    data_dir: Path
    plist_path: Path
    log_dir: Path
    path_env: str

    @property
    def venv_dir(self) -> Path:
        return self.checkout / ".venv"

    @property
    def venv_python(self) -> Path:
        return self.venv_dir / "bin" / "python"

    @property
    def api_bin(self) -> Path:
        return self.venv_dir / "bin" / "orch-hub-api"

    @property
    def dashboard_url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def template_path(self) -> Path:
        return self.checkout / TEMPLATE_RELATIVE_PATH


@dataclass(frozen=True)
class DeployResult:
    deployment: Deployment
    generated_token: bool
    commands: tuple[CommandResult, ...]


@dataclass(frozen=True)
class StopResult:
    stopped: bool
    already_stopped: bool
    command: CommandResult


@dataclass(frozen=True)
class StartResult:
    started: bool
    already_started: bool
    command: CommandResult
    plist_path: Path


@dataclass(frozen=True)
class RestartResult:
    stop: StopResult
    start: StartResult


@dataclass(frozen=True)
class StatusResult:
    label: str
    plist_path: Path
    plist_installed: bool
    loaded: bool
    launchctl_output: str
    program: str | None
    host: str
    port: int
    health: str
    plist_error: str | None = None

    @property
    def dashboard_url(self) -> str:
        return f"http://{self.host}:{self.port}/"


def default_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / PLIST_NAME


def default_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "orch-hub"


def service_target(uid: int | None = None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}/{LABEL}"


def service_domain(uid: int | None = None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}"


def find_checkout(start: Path) -> Path:
    """Find the orch-hub checkout at or above ``start``."""
    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "orch_hub"
        ).is_dir():
            return candidate
    raise LaunchdError(
        f"could not find an orch-hub checkout from {start}; pass --checkout"
    )


def resolve_deployment(
    options: DeployOptions,
    *,
    environ: Mapping[str, str] | None = None,
    token_generator: Callable[[], str] | None = None,
) -> Deployment:
    """Resolve deploy options, environment defaults, and existing plist token."""
    env = os.environ if environ is None else environ
    try:
        config = Config.from_env(env)
    except ValueError as exc:
        raise LaunchdError(str(exc)) from exc
    checkout = find_checkout(options.checkout or Path.cwd())
    plist_path = (options.plist_path or default_plist_path()).expanduser()
    existing = _read_plist(plist_path)
    token, source = _resolve_token(
        explicit=options.token,
        env_token=env.get(TOKEN_ENV),
        existing=existing,
        token_generator=token_generator or (lambda: secrets.token_hex(32)),
    )
    port = options.port if options.port is not None else config.port
    if not 1 <= port <= 65535:
        raise LaunchdError(f"{PORT_ENV} must be in 1-65535, got {port}")
    return Deployment(
        checkout=checkout,
        ai_native_path=(options.ai_native_path or config.ai_native_path)
        .expanduser()
        .resolve(),
        token=token,
        token_source=source,
        host=(options.host or config.host or DEFAULT_HOST).strip() or DEFAULT_HOST,
        port=port,
        data_dir=(options.data_dir or config.data_dir or DEFAULT_DATA_DIR)
        .expanduser()
        .resolve(),
        plist_path=plist_path,
        log_dir=(options.log_dir or default_log_dir()).expanduser(),
        path_env=options.path_env,
    )


def deploy_service(
    options: DeployOptions,
    *,
    runner: Runner = run_command,
    environ: Mapping[str, str] | None = None,
    token_generator: Callable[[], str] | None = None,
) -> DeployResult:
    """Install/update the package, rewrite plist, and bootstrap the service.

    The operation is idempotent: existing launchd jobs are booted out first;
    an absent job is treated as already stopped.
    """
    deployment = resolve_deployment(
        options, environ=environ, token_generator=token_generator
    )
    plist_data = _render_plist_from_template(deployment)
    commands: list[CommandResult] = []
    if options.install_package:
        commands.extend(_install_package(deployment, runner))
    deployment.log_dir.mkdir(parents=True, exist_ok=True)
    deployment.plist_path.parent.mkdir(parents=True, exist_ok=True)
    _write_plist(deployment.plist_path, plist_data)
    deployment.plist_path.chmod(0o600)
    commands.append(
        _run_required(
            runner,
            ["plutil", "-lint", deployment.plist_path],
            "validate launchd plist",
        )
    )
    bootout = _bootout(runner)
    commands.append(bootout)
    if not bootout.ok and not _is_not_loaded(bootout):
        raise LaunchdError(_format_failure("stop existing launchd service", bootout))
    commands.append(
        _run_required(
            runner,
            ["launchctl", "bootstrap", service_domain(), deployment.plist_path],
            "bootstrap launchd service",
        )
    )
    return DeployResult(
        deployment=deployment,
        generated_token=deployment.token_source == "generated",
        commands=tuple(commands),
    )


def stop_service(*, runner: Runner = run_command) -> StopResult:
    """Unload the single launchd-managed orch-hub service.

    This does not stop active orchestrator runs; use the run API/dashboard for
    per-run stops.
    """
    result = _bootout(runner)
    if result.ok:
        return StopResult(stopped=True, already_stopped=False, command=result)
    if _is_not_loaded(result):
        return StopResult(stopped=False, already_stopped=True, command=result)
    raise LaunchdError(_format_failure("stop launchd service", result))


def start_service(
    *, plist_path: Path | None = None, runner: Runner = run_command
) -> StartResult:
    """Bootstrap the installed launchd plist without rewriting it."""
    resolved_plist = (plist_path or default_plist_path()).expanduser()
    if not resolved_plist.is_file():
        raise LaunchdError(f"launchd plist is not installed: {resolved_plist}")
    current = _print_service(runner)
    if current.ok:
        return StartResult(
            started=False,
            already_started=True,
            command=current,
            plist_path=resolved_plist,
        )
    if not _is_not_loaded(current):
        raise LaunchdError(_format_failure("check launchd service", current))
    started = _run_required(
        runner,
        ["launchctl", "bootstrap", service_domain(), resolved_plist],
        "bootstrap launchd service",
    )
    return StartResult(
        started=True,
        already_started=False,
        command=started,
        plist_path=resolved_plist,
    )


def restart_service(
    *, plist_path: Path | None = None, runner: Runner = run_command
) -> RestartResult:
    """Unload and then bootstrap the installed plist without reinstalling."""
    stopped = stop_service(runner=runner)
    started = start_service(plist_path=plist_path, runner=runner)
    return RestartResult(stop=stopped, start=started)


def status_service(
    *,
    plist_path: Path | None = None,
    runner: Runner = run_command,
    health_checker: Callable[[str, int], str] | None = None,
) -> StatusResult:
    """Return launchd load state plus a public health probe result."""
    resolved_plist = (plist_path or default_plist_path()).expanduser()
    launchctl = _print_service(runner)
    loaded = launchctl.ok
    plist, plist_error = _read_plist_with_error(resolved_plist)
    env = _plist_env(plist)
    program = _plist_program(plist)
    host = env.get(HOST_ENV, DEFAULT_HOST) or DEFAULT_HOST
    port = _status_port(env.get(PORT_ENV))
    checker = health_checker or _check_health
    health = checker(host, port)
    return StatusResult(
        label=LABEL,
        plist_path=resolved_plist,
        plist_installed=resolved_plist.is_file(),
        loaded=loaded,
        launchctl_output=(launchctl.stdout or launchctl.stderr).strip(),
        program=program,
        host=host,
        port=port,
        health=health,
        plist_error=plist_error,
    )


def _install_package(deployment: Deployment, runner: Runner) -> list[CommandResult]:
    commands: list[CommandResult] = []
    if not deployment.venv_python.exists():
        commands.append(
            _run_required(
                runner,
                [sys.executable, "-m", "venv", deployment.venv_dir],
                "create virtual environment",
            )
        )
    commands.append(
        _run_required(
            runner,
            [deployment.venv_python, "-m", "pip", "install", deployment.checkout],
            "install orch-hub package",
        )
    )
    return commands


def _render_plist_from_template(deployment: Deployment) -> dict:
    template = _load_template(deployment.template_path)
    data = _replace_placeholders(
        template,
        {
            "REPLACE-ORCH-HUB-CHECKOUT": str(deployment.checkout),
            "REPLACE-BEARER-TOKEN": deployment.token,
            "REPLACE-ORCH-HUB-HOST": deployment.host,
            "REPLACE-ORCH-HUB-PORT": str(deployment.port),
            "REPLACE-AI-NATIVE-DEVELOPMENT-CHECKOUT": str(deployment.ai_native_path),
            "REPLACE-ORCH-HUB-DATA-DIR": str(deployment.data_dir),
            "REPLACE-ORCH-HUB-LOG-DIR": str(deployment.log_dir),
            "REPLACE-PATH": deployment.path_env,
        },
    )
    _require_no_placeholders(data)
    return data


def _write_plist(path: Path, data: dict) -> None:
    with path.open("wb") as fh:
        plistlib.dump(data, fh, fmt=plistlib.FMT_XML, sort_keys=False)


def _load_template(path: Path) -> dict:
    if not path.is_file():
        raise LaunchdError(f"launchd template not found: {path}")
    plist, error = _read_plist_with_error(path)
    if error:
        raise LaunchdError(f"invalid launchd template {path}: {error}")
    if plist is None:
        raise LaunchdError(f"invalid launchd template {path}")
    label = plist.get("Label")
    if label != LABEL:
        raise LaunchdError(
            f"launchd template label must be {LABEL!r}, got {label!r}"
        )
    return plist


def _replace_placeholders(value, replacements: Mapping[str, str]):
    if isinstance(value, str):
        result = value
        for placeholder, replacement in replacements.items():
            result = result.replace(placeholder, replacement)
        return result
    if isinstance(value, list):
        return [_replace_placeholders(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_placeholders(item, replacements)
            for key, item in value.items()
        }
    return value


def _require_no_placeholders(value) -> None:
    if isinstance(value, str):
        if "REPLACE-" in value:
            raise LaunchdError(f"unreplaced launchd template placeholder: {value}")
        return
    if isinstance(value, list):
        for item in value:
            _require_no_placeholders(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _require_no_placeholders(item)


def _read_plist(path: Path) -> dict | None:
    plist, _ = _read_plist_with_error(path)
    return plist


def _read_plist_with_error(path: Path) -> tuple[dict | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        with path.open("rb") as fh:
            data = plistlib.load(fh)
    except Exception as exc:  # plistlib exposes several parse/value failures.
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "plist root is not a dictionary"
    return data, None


def _plist_env(plist: dict | None) -> dict[str, str]:
    if not plist:
        return {}
    env = plist.get("EnvironmentVariables")
    if not isinstance(env, dict):
        return {}
    return {str(key): str(value) for key, value in env.items()}


def _plist_program(plist: dict | None) -> str | None:
    if not plist:
        return None
    argv = plist.get("ProgramArguments")
    if not isinstance(argv, list) or not argv:
        return None
    return str(argv[0])


def _resolve_token(
    *,
    explicit: str | None,
    env_token: str | None,
    existing: dict | None,
    token_generator: Callable[[], str],
) -> tuple[str, str]:
    for source, raw in (
        ("explicit", explicit),
        ("environment", env_token),
        ("existing plist", _plist_env(existing).get(TOKEN_ENV)),
    ):
        token = (raw or "").strip()
        if token:
            return token, source
    return token_generator(), "generated"


def _bootout(runner: Runner) -> CommandResult:
    return _run(runner, ["launchctl", "bootout", service_target()])


def _print_service(runner: Runner) -> CommandResult:
    return _run(runner, ["launchctl", "print", service_target()])


def _run_required(
    runner: Runner, args: list[str | Path], action: str
) -> CommandResult:
    result = _run(runner, args)
    if not result.ok:
        raise LaunchdError(_format_failure(action, result))
    return result


def _run(runner: Runner, args: list[str | Path]) -> CommandResult:
    try:
        return runner(args)
    except CommandNotFoundError as exc:
        raise LaunchdError(f"command not found: {exc}") from exc
    except OSError as exc:
        raise LaunchdError(str(exc)) from exc


def _format_failure(action: str, result: CommandResult) -> str:
    output = (result.stderr or result.stdout).strip()
    detail = f": {output}" if output else ""
    return f"{action} failed with exit {result.returncode}{detail}"


def _is_not_loaded(result: CommandResult) -> bool:
    text = f"{result.stdout}\n{result.stderr}".lower()
    return any(
        marker in text
        for marker in (
            "could not find service",
            "no such process",
            "service is not loaded",
            "does not exist",
            "not found",
        )
    )


def _status_port(raw: str | None) -> int:
    try:
        port = int((raw or "").strip() or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def _check_health(host: str, port: int) -> str:
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            code = getattr(response, "status", response.getcode())
            return "ok" if code == 200 else f"http {code}"
    except urllib.error.URLError as exc:
        return f"unreachable: {exc.reason}"
    except Exception as exc:
        return f"unreachable: {exc}"
