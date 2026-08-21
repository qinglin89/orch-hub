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
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .config import (
    BARK_URL_ENV,
    DATA_DIR_ENV,
    DEFAULT_DATA_DIR,
    DEFAULT_HOST,
    DEFAULT_PORT,
    HOST_ENV,
    LEGACY_MANDREL_PATH_ENV,
    MANDREL_BIN_RELATIVE_PATH,
    MANDREL_PATH_ENV,
    PORT_ENV,
    TOKEN_ENV,
    Config,
)
from .resources import bundled_resource, is_frozen
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
PROGRAM_KEY = "ProgramArguments"

CHECKOUT_API_RELATIVE_PATH = Path(".venv") / "bin" / "orch-hub-api"
"""Console script a source-checkout deployment runs, relative to the checkout.

Created by the package install this same deploy performs, which is why a
checkout deploy needs no ``serve`` argument: packaging metadata gives it a
dedicated entry point that a compiled release does not have."""

INHERITED_SERVICE_ENV = (HOST_ENV, PORT_ENV, DATA_DIR_ENV)
"""Settings an update inherits from the service it is updating.

The resident service's configuration lives in its plist, not in the shell that
runs the update — an operator upgrading from a fresh terminal has none of it
exported. Without this, an upgrade would silently move a service off its
configured bind address and away from the data directory holding its run
history, which is the opposite of what re-running an installer should do.

The token, Mandrel checkout, and Bark URL are preserved by their own
resolutions; these three go through the environment mapping instead, so the
same fail-closed parsing applies whether a value came from the shell or from the
installed plist."""

SERVE_ARGV = ("serve",)
"""Argument the installed runtime is launched with.

A compiled release ships one executable and no console scripts, so launchd runs
``<runtime> serve`` where a source checkout runs the ``orch-hub-api`` entry
point installed in its virtualenv."""

HEALTH_TIMEOUT = 20.0
HEALTH_POLL_INTERVAL = 0.5

# Template placeholders look like ``REPLACE-ORCH-HUB-BARK-URL``: the ``REPLACE-``
# marker followed by an uppercase, hyphen-separated token. Matching one token at
# a time lets rendering classify any placeholder still present after
# substitution — a required one fails loudly, an unknown (newer-template) one is
# blanked so an older installed CLI can still update a newer checkout.
_PLACEHOLDER_RE = re.compile(r"REPLACE-[A-Z0-9-]+")

# launchd removes a booted-out job asynchronously; bootstrapping into a domain
# that still lists the label fails transiently with exit 5 (EIO). After an
# actual unload, poll ``launchctl print`` for the label to disappear, then
# retry bootstrap on transient exit-5 / input-output failures with a short
# bounded backoff so ``orch-hub launchd update`` does not require a manual rerun.
UNLOAD_MAX_ATTEMPTS = 10
UNLOAD_POLL_INTERVAL = 0.3
BOOTSTRAP_MAX_ATTEMPTS = 5
BOOTSTRAP_RETRY_INTERVAL = 0.5


class LaunchdError(RuntimeError):
    """Raised when a launchd operation cannot complete."""


@dataclass(frozen=True)
class DeployOptions:
    """User-configurable launchd deployment settings."""

    checkout: Path | None = None
    program: Path | None = None
    """Installed runtime executable launchd should run.

    Set by the installed-release path (``orch-hub setup``), which has no
    checkout and no virtualenv. When it is set, the deployment never looks for
    a checkout and never installs a package: the program is already built."""

    mandrel_path: Path | None = None
    token: str | None = None
    host: str | None = None
    port: int | None = None
    data_dir: Path | None = None
    plist_path: Path | None = None
    log_dir: Path | None = None
    path_env: str = DEFAULT_PATH_ENV
    install_package: bool = True
    verify_mandrel: bool = False
    """Whether the resolved Mandrel checkout must actually exist.

    Set by first-time setup, where a mistyped path is the likely failure and its
    only symptom would be a hub that serves an empty repo list. A checkout
    deploy leaves it off: the maintainer running it supplies the path they
    already work in, and a machine-local mandrel that is temporarily elsewhere
    should not block rewriting the plist."""


@dataclass(frozen=True)
class Deployment:
    """Fully resolved launchd deployment paths and environment."""

    program_argv: tuple[str, ...]
    """Exactly what launchd runs, program first.

    The two runtime shapes differ only here: a source checkout runs the
    ``orch-hub-api`` console script from its virtualenv, an installed release
    runs its own executable with ``serve``. Everything downstream — the plist,
    the CLI summary, the status probe — reads this instead of reconstructing a
    path from a checkout that an installed runtime does not have."""

    checkout: Path | None
    """Source checkout this deployment installs from, or ``None`` when the
    program is an already-built installed runtime."""

    mandrel_path: Path
    token: str
    token_source: str
    host: str
    port: int
    data_dir: Path
    plist_path: Path
    log_dir: Path
    path_env: str
    bark_url: str | None = None
    """Optional Bark push endpoint written into the plist; ``None`` = push off."""

    @property
    def venv_dir(self) -> Path:
        return self._checkout_required() / ".venv"

    @property
    def venv_python(self) -> Path:
        return self.venv_dir / "bin" / "python"

    @property
    def program(self) -> str:
        return self.program_argv[0]

    def _checkout_required(self) -> Path:
        if self.checkout is None:
            raise LaunchdError(
                "this deployment runs an installed runtime, not a checkout "
                "virtualenv"
            )
        return self.checkout

    @property
    def dashboard_url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def template_path(self) -> Path:
        """Plist template this deployment renders from.

        The template ships with the checkout, so a source runtime reads it
        there. A compiled release has no checkout: the build copies the
        template into the bundle under the same relative path, and that copy
        wins whenever it is present. Preferring the bundled copy also keeps a
        frozen runtime from rendering against an unrelated checkout that
        happens to be named as ``checkout``.
        """
        bundled = bundled_resource(TEMPLATE_RELATIVE_PATH)
        if bundled is not None:
            return bundled
        if self.checkout is None:
            raise LaunchdError(
                "no launchd template available: this runtime carries no bundled "
                "copy and the deployment has no checkout"
            )
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


def installed_bark_url(*, plist_path: Path | None = None) -> str | None:
    """Return ``ORCH_HUB_BARK_URL`` from the installed launchd plist, if any.

    The resident service carries its Bark URL in the launchd plist's
    ``EnvironmentVariables``, not in an interactive shell. ``orch-hub notify
    test`` run from a plain shell therefore falls back to this installed value
    so an operator can verify delivery without re-exporting the secret URL.
    Returns ``None`` when the plist is absent, unreadable, or has no non-blank
    value; the URL embeds a device key and is never logged.
    """
    resolved = (plist_path or default_plist_path()).expanduser()
    value = _plist_env(_read_plist(resolved)).get(BARK_URL_ENV, "")
    return value.strip() or None


def installed_mandrel_path(*, plist_path: Path | None = None) -> Path | None:
    """Return the Mandrel checkout configured in an installed launchd plist.

    The current environment key wins over the legacy pre-rename key when both
    are present. Missing, unreadable, and blank-only plists resolve to ``None``.
    This supports plain-shell operator commands without reintroducing a
    maintainer-specific checkout default.
    """
    resolved = (plist_path or default_plist_path()).expanduser()
    return _resolve_mandrel_path(
        explicit=None,
        current_env=None,
        legacy_env=None,
        existing=_read_plist(resolved),
    )


def installed_data_dir(*, plist_path: Path | None = None) -> Path:
    """Return the hub data dir the installed service is configured with.

    The resident service carries its data dir in the plist, so an operator
    command that has to name it — uninstall's optional purge — reads the value
    the service actually uses instead of re-deriving a default that may not be
    the one holding their run history.
    """
    resolved = (plist_path or default_plist_path()).expanduser()
    raw = _plist_env(_read_plist(resolved)).get(DATA_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_DATA_DIR


def installed_bind(*, plist_path: Path | None = None) -> tuple[str, int]:
    """Return the host/port the installed service is configured to serve on."""
    resolved = (plist_path or default_plist_path()).expanduser()
    env = _plist_env(_read_plist(resolved))
    host = env.get(HOST_ENV, DEFAULT_HOST) or DEFAULT_HOST
    return host, _status_port(env.get(PORT_ENV))


def verify_mandrel_checkout(path: Path) -> None:
    """Fail unless ``path`` really is a mandrel checkout.

    The registry — and therefore every managed repo the hub can see — comes from
    ``<mandrel>/bin/mandrel``. A path that does not carry it produces a hub that
    starts, serves, and reports zero repos, which reads as "nothing to do"
    rather than as "misconfigured". Checking the executable's presence when the
    value is supplied turns that into one clear error at the right moment.
    """
    resolved = path.expanduser()
    if not resolved.is_dir():
        raise LaunchdError(f"mandrel checkout not found: {resolved}")
    binary = resolved / MANDREL_BIN_RELATIVE_PATH
    if not binary.is_file():
        raise LaunchdError(
            f"{resolved} does not look like a mandrel checkout: no {binary}"
        )


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
    if is_frozen():
        # An installed release has no checkout by construction, so pointing the
        # operator at --checkout would send them looking for something that
        # deliberately does not exist on their machine.
        raise LaunchdError(
            "a compiled orch-hub release has no source checkout; configure the "
            "service with `orch-hub setup` instead of `orch-hub launchd deploy`"
        )
    raise LaunchdError(
        f"could not find an orch-hub checkout from {start}; pass --checkout"
    )


def resolve_deployment(
    options: DeployOptions,
    *,
    environ: Mapping[str, str] | None = None,
    token_generator: Callable[[], str] | None = None,
) -> Deployment:
    """Resolve deploy options, environment defaults, and installed settings."""
    env = os.environ if environ is None else environ
    checkout, program_argv = _resolve_program(options)
    plist_path = (options.plist_path or default_plist_path()).expanduser()
    existing = _read_plist(plist_path)
    mandrel_path = _resolve_mandrel_path(
        explicit=options.mandrel_path,
        current_env=env.get(MANDREL_PATH_ENV),
        legacy_env=env.get(LEGACY_MANDREL_PATH_ENV),
        existing=existing,
    )
    if mandrel_path is None:
        raise LaunchdError(
            "Mandrel checkout is not configured; pass --mandrel or set "
            f"{MANDREL_PATH_ENV}"
            f" ({LEGACY_MANDREL_PATH_ENV} is also accepted for legacy installs)"
        )
    if options.verify_mandrel:
        verify_mandrel_checkout(mandrel_path)
    config_env = {**dict(env), **_inherited_service_env(env, existing)}
    config_env[MANDREL_PATH_ENV] = str(mandrel_path)
    try:
        config = Config.from_env(config_env)
    except ValueError as exc:
        raise LaunchdError(str(exc)) from exc
    token, source = _resolve_token(
        explicit=options.token,
        env_token=env.get(TOKEN_ENV),
        existing=existing,
        token_generator=token_generator or (lambda: secrets.token_hex(32)),
    )
    bark_url = _resolve_bark_url(
        config.bark_url, _plist_env(existing).get(BARK_URL_ENV)
    )
    port = options.port if options.port is not None else config.port
    if not 1 <= port <= 65535:
        raise LaunchdError(f"{PORT_ENV} must be in 1-65535, got {port}")
    return Deployment(
        program_argv=program_argv,
        checkout=checkout,
        mandrel_path=config.mandrel_path.expanduser().resolve(),
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
        bark_url=bark_url,
    )


def _resolve_program(options: DeployOptions) -> tuple[Path | None, tuple[str, ...]]:
    """Resolve what launchd runs and which checkout (if any) backs it.

    An explicit program is an installed runtime: it is already built, so there
    is nothing to find above the working directory and nothing to install. Only
    the checkout path searches, which keeps ``orch-hub setup`` on a machine
    without a checkout from failing on a search it has no reason to run.
    """
    if options.program is not None:
        program = options.program.expanduser()
        if not program.is_file():
            raise LaunchdError(f"installed runtime not found: {program}")
        return None, (str(program), *SERVE_ARGV)
    checkout = find_checkout(options.checkout or Path.cwd())
    return checkout, (str(checkout / CHECKOUT_API_RELATIVE_PATH),)


def deploy_service(
    options: DeployOptions,
    *,
    runner: Runner = run_command,
    environ: Mapping[str, str] | None = None,
    token_generator: Callable[[], str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> DeployResult:
    """Install/update the package, rewrite plist, and bootstrap the service.

    The operation is idempotent: existing launchd jobs are booted out first;
    an absent job is treated as already stopped. When a live service is
    actually unloaded, the deploy waits for launchd to drop the label before
    bootstrapping and retries a transient post-bootout bootstrap failure
    (exit 5 / input-output error) with a short bounded backoff, so an update
    does not require the operator to rerun ``orch-hub launchd update``.
    """
    deployment = resolve_deployment(
        options, environ=environ, token_generator=token_generator
    )
    plist_data = _render_plist_from_template(deployment)
    commands: list[CommandResult] = []
    if options.install_package and deployment.checkout is not None:
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
    if bootout.ok:
        # A live service was actually unloaded. launchd removes the job
        # asynchronously, so bootstrapping immediately can race the teardown
        # and fail with a transient exit 5; wait for the label to drop first.
        commands.extend(_wait_for_unload(runner, sleeper))
    commands.extend(_bootstrap_with_retry(runner, deployment, sleeper))
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
    # Required substitutions. A leftover of any of these keys after rendering is
    # a template/version integrity error — the plist would carry an invalid or
    # empty required field — so it fails loudly before anything is written.
    required = {
        "REPLACE-BEARER-TOKEN": deployment.token,
        "REPLACE-ORCH-HUB-HOST": deployment.host,
        "REPLACE-ORCH-HUB-PORT": str(deployment.port),
        "REPLACE-MANDREL-CHECKOUT": str(deployment.mandrel_path),
        "REPLACE-ORCH-HUB-DATA-DIR": str(deployment.data_dir),
        "REPLACE-ORCH-HUB-LOG-DIR": str(deployment.log_dir),
        "REPLACE-PATH": deployment.path_env,
    }
    # Optional substitutions render empty when unset ("" = disabled). An unset
    # Bark URL never survives as REPLACE-; it is the same forward-compatible
    # handling any unknown newer-template placeholder gets below.
    optional = {
        "REPLACE-ORCH-HUB-BARK-URL": deployment.bark_url or "",
    }
    data = _replace_placeholders(template, {**required, **optional})
    data = _with_program_arguments(data, deployment.program_argv)
    return _resolve_leftover_placeholders(data, required=frozenset(required))


def _with_program_arguments(data: dict, program_argv: tuple[str, ...]) -> dict:
    """Replace the template's program array with what this deployment runs.

    ``ProgramArguments`` is rendered rather than placeholder-substituted because
    it is a *list* whose length depends on the runtime shape: one element for a
    checkout's console script, two for an installed release's ``serve``. A
    string placeholder can only produce a fixed-length array, and the template
    is the structure's source of truth — so the template documents the shape
    with a placeholder for manual recovery, and rendering supplies the real
    argv. A template that lost the key entirely fails here rather than
    producing a plist launchd cannot start.
    """
    argv = data.get(PROGRAM_KEY)
    if not isinstance(argv, list) or not argv:
        raise LaunchdError(
            f"launchd template has no {PROGRAM_KEY} array to render"
        )
    return {**data, PROGRAM_KEY: list(program_argv)}


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


def _resolve_leftover_placeholders(value, *, required: frozenset[str]):
    """Fail on leftover required placeholders; blank unknown optional ones.

    After the known substitutions run, any ``REPLACE-...`` token still present
    was introduced by a launchd template newer than this installed code. This is
    the ``orch-hub launchd update`` version skew where an older
    ``.venv/bin/orch-hub`` renders a checkout whose template added a placeholder
    it predates (the reported case was ``REPLACE-ORCH-HUB-BARK-URL``).

    A leftover token in ``required`` is a hard error: a required field must be
    substituted before the plist is written or loaded. Any other leftover token
    is treated as an optional entry this older code does not recognize and is
    blanked to ``""`` — the same "empty = unset/disabled" contract the optional
    Bark URL already uses — so a forward-compatible optional field does not abort
    the whole update.
    """
    if isinstance(value, str):
        for token in _PLACEHOLDER_RE.findall(value):
            if token in required:
                raise LaunchdError(
                    f"unreplaced launchd template placeholder: {token}"
                )
        return _PLACEHOLDER_RE.sub("", value)
    if isinstance(value, list):
        return [
            _resolve_leftover_placeholders(item, required=required) for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _resolve_leftover_placeholders(item, required=required)
            for key, item in value.items()
        }
    return value


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


def _resolve_mandrel_path(
    *,
    explicit: Path | None,
    current_env: str | None,
    legacy_env: str | None,
    existing: dict | None,
) -> Path | None:
    """Resolve the Mandrel checkout without a maintainer-specific default.

    A deploy/update flag is most explicit, then the current and legacy shell
    variables win in that order. An installed plist is last so updating an
    existing service remains idempotent even when the interactive shell does
    not export its resident configuration.
    """
    installed = _plist_env(existing)
    for raw in (
        explicit,
        current_env,
        legacy_env,
        installed.get(MANDREL_PATH_ENV),
        installed.get(LEGACY_MANDREL_PATH_ENV),
    ):
        value = str(raw).strip() if raw is not None else ""
        if value:
            return Path(value).expanduser()
    return None


def _inherited_service_env(
    env: Mapping[str, str], existing: dict | None
) -> dict[str, str]:
    """Installed-plist values for settings the current environment leaves unset.

    Blank counts as unset, matching the configuration contract: exporting an
    empty ``ORCH_HUB_PORT`` does not silently reset a configured service.
    """
    installed = _plist_env(existing)
    inherited: dict[str, str] = {}
    for key in INHERITED_SERVICE_ENV:
        if (env.get(key) or "").strip():
            continue
        value = installed.get(key, "").strip()
        if value:
            inherited[key] = value
    return inherited


def _resolve_bark_url(env_value: str | None, existing: str | None) -> str | None:
    """Resolve the optional Bark push URL: environment first, then existing plist.

    Unlike the token there is no generated fallback and no CLI flag — Bark is
    optional config (unset = push disabled), and the URL embeds a device-key
    secret best supplied through the environment (``ORCH_HUB_BARK_URL``).
    Preferring an already-installed value means an ``update`` that forgets to
    re-export the variable keeps pushing instead of silently going dark, the
    same preservation the token gets.
    """
    for raw in (env_value, existing):
        value = (raw or "").strip()
        if value:
            return value
    return None


def _bootout(runner: Runner) -> CommandResult:
    return _run(runner, ["launchctl", "bootout", service_target()])


def _print_service(runner: Runner) -> CommandResult:
    return _run(runner, ["launchctl", "print", service_target()])


def _wait_for_unload(
    runner: Runner, sleeper: Callable[[float], None]
) -> list[CommandResult]:
    """Poll ``launchctl print`` until the booted-out label is gone.

    ``launchctl bootout`` returns before launchd has finished removing the job.
    A successful ``print`` means the label is still in the domain, so we wait
    and re-probe; a failing ``print`` means it is gone and bootstrap is safe.
    The fast path costs nothing: if the label is already absent the first probe
    breaks with no sleep. The poll is bounded — if the label lingers, the
    bootstrap retry is the backstop rather than failing the whole deploy.
    """
    results: list[CommandResult] = []
    for attempt in range(UNLOAD_MAX_ATTEMPTS):
        printed = _print_service(runner)
        results.append(printed)
        if not printed.ok:
            break
        if attempt + 1 < UNLOAD_MAX_ATTEMPTS:
            sleeper(UNLOAD_POLL_INTERVAL)
    return results


def _bootstrap_with_retry(
    runner: Runner,
    deployment: Deployment,
    sleeper: Callable[[float], None],
) -> list[CommandResult]:
    """Bootstrap the installed plist, retrying transient launchd failures.

    Immediately after bootout, ``launchctl bootstrap`` can fail with a
    transient exit 5 (``Input/output error``) while the domain still holds the
    label. Those are retried with a short bounded backoff; non-transient
    failures (bad plist, permissions) raise at once, and exhausting the retries
    raises the last failure so the CLI still reports a clear non-zero error.
    """
    results: list[CommandResult] = []
    args: list[str | Path] = [
        "launchctl",
        "bootstrap",
        service_domain(),
        deployment.plist_path,
    ]
    for attempt in range(BOOTSTRAP_MAX_ATTEMPTS):
        result = _run(runner, args)
        results.append(result)
        if result.ok:
            return results
        last_attempt = attempt + 1 >= BOOTSTRAP_MAX_ATTEMPTS
        if last_attempt or not _is_transient_bootstrap_failure(result):
            raise LaunchdError(_format_failure("bootstrap launchd service", result))
        sleeper(BOOTSTRAP_RETRY_INTERVAL)
    return results  # pragma: no cover - loop returns ok or raises above


def _is_transient_bootstrap_failure(result: CommandResult) -> bool:
    """Recognize launchd's retryable post-bootout bootstrap failure.

    Bootstrapping a domain that still holds the just-booted-out label fails
    with exit 5 (``Input/output error``). That is transient; other failures are
    not retried.
    """
    if result.returncode == 5:
        return True
    text = f"{result.stdout}\n{result.stderr}".lower()
    return "input/output error" in text


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


def wait_for_health(
    host: str,
    port: int,
    *,
    timeout: float = HEALTH_TIMEOUT,
    interval: float = HEALTH_POLL_INTERVAL,
    checker: Callable[[str, int], str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Probe the public health route until it answers, then report what it said.

    ``launchctl bootstrap`` returns as soon as launchd accepts the job, not when
    the service is listening, so a single probe right after it reports a
    connection refusal on a service that is starting normally. Polling turns
    "did the thing I just configured actually come up" into an answer instead of
    a race, which is the whole point of verifying at all.

    The last probe result is returned rather than raised: a service that does
    not answer is worth reporting alongside the configuration that was written,
    and the caller decides whether that is fatal.
    """
    probe = checker or _check_health
    deadline = clock() + timeout
    # Bounded twice on purpose: by wall clock, because each probe can itself
    # block on its own connect timeout, and by attempts, because a caller that
    # injects a probe and a sleeper (every test here) advances no clock at all
    # and would otherwise spin until the deadline passes in real time.
    result = ""
    for _ in range(max(1, int(timeout / interval))):
        result = probe(host, port)
        if result == "ok" or clock() >= deadline:
            return result
        sleeper(interval)
    return result


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
