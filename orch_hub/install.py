"""User-local installation of a compiled orch-hub runtime.

A source checkout deploys itself: ``orch-hub launchd deploy`` installs the
package into ``<checkout>/.venv`` and points launchd at the console script it
just created. An engineer who receives a release has none of that — no Python,
no virtualenv, no checkout — so the same operations have to be expressible over
a *payload directory*: the self-contained executable plus its libraries and
resources that :mod:`orch_hub.release_build` produces.

This module is that layer. Its shape is one idea:

    <prefix>/versions/<version>/     one payload per version, never mutated
    <prefix>/current -> versions/X   which one is live, swapped atomically
    <prefix>/previous               which one it was, so rollback is one word
    <bin-dir>/orch-hub -> <prefix>/current/orch-hub

Because the launchd plist names ``<prefix>/current/orch-hub``, selecting a
version is a symlink swap plus a service restart — the installed service
configuration, its token, and the hub's own data are never rewritten to change
which code runs. That is what makes an upgrade cheap and a rollback safe: the
version being replaced is still on disk under its own name, and going back to it
does not need the installer, the network, or the payload it came from.

Nothing here writes into a managed repo, a run directory, or the hub data dir.
Uninstall removes the service and the installed runtime; the hub's data survives
unless it is purged explicitly, because run history and evaluation artifacts are
not re-creatable from a release.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .atomic_dir import PlacementError, discard_path, place_directory
from .launchd import (
    Deployment,
    DeployOptions,
    LaunchdError,
    default_plist_path,
    deploy_service,
    installed_bind,
    installed_data_dir,
    restart_service,
    stop_service,
    verify_mandrel_checkout,
    wait_for_health,
)
from .resources import is_frozen
from .subprocess_utils import Runner, run_command

PREFIX_RELATIVE_PATH = Path(".local") / "orch-hub"
"""Install prefix under the user's home. User-local on purpose: a release that
needs ``sudo`` to install cannot be handed to an engineer as one command."""

BIN_RELATIVE_PATH = Path(".local") / "bin"
"""Directory the ``orch-hub`` command is linked into."""

VERSIONS_DIR = "versions"
CURRENT_LINK = "current"
PREVIOUS_MARKER = "previous"
COMMAND_NAME = "orch-hub"
MANIFEST_NAME = "runtime.json"

INCOMING_LINK = f".{CURRENT_LINK}.incoming"
"""Scratch name the selection symlink is built under before it is swapped in.

A symlink cannot be edited in place, so selection creates the new one beside the
old and renames it over: ``rename`` replaces the entry atomically, which means a
reader never sees ``current`` missing."""


class InstallError(RuntimeError):
    """Raised when an installed runtime cannot be placed, selected, or removed."""


@dataclass(frozen=True)
class Installation:
    """What is installed under one prefix, and which version is live."""

    prefix: Path
    versions: tuple[str, ...]
    current: str | None
    previous: str | None

    @property
    def executable(self) -> Path:
        """Path launchd is pointed at: stable across upgrades by construction."""
        return self.prefix / CURRENT_LINK / COMMAND_NAME

    def to_dict(self) -> dict:
        return {
            "prefix": str(self.prefix),
            "versions": list(self.versions),
            "current": self.current,
            "previous": self.previous,
        }


@dataclass(frozen=True)
class InstallResult:
    installation: Installation
    version: str
    replaced: bool
    """True when this version was already installed and has been overwritten."""

    link_path: Path | None
    link_on_path: bool

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "replaced": self.replaced,
            "link": str(self.link_path) if self.link_path else None,
            "link_on_path": self.link_on_path,
            **self.installation.to_dict(),
        }


@dataclass(frozen=True)
class SetupOptions:
    """Configuration a fresh install collects, or an upgrade preserves.

    Every field left ``None`` falls back to the resolution the launchd
    deployment already owns — environment first, then the installed plist — so
    an upgrade that passes nothing keeps the configuration the running service
    was using, secrets included.
    """

    prefix: Path | None = None
    mandrel_path: Path | None = None
    token: str | None = None
    host: str | None = None
    port: int | None = None
    data_dir: Path | None = None
    plist_path: Path | None = None
    log_dir: Path | None = None


@dataclass(frozen=True)
class SetupResult:
    installation: Installation
    deployment: Deployment
    generated_token: bool
    health: str


@dataclass(frozen=True)
class UpgradeResult:
    install: InstallResult
    setup: SetupResult
    previous_version: str | None


@dataclass(frozen=True)
class RollbackResult:
    from_version: str | None
    to_version: str
    health: str


@dataclass(frozen=True)
class UninstallResult:
    prefix: Path
    prefix_removed: bool
    plist_path: Path
    plist_removed: bool
    link_path: Path | None
    link_removed: bool
    service_error: str | None
    data_dir: Path | None
    data_removed: bool


def default_prefix() -> Path:
    """Prefix to operate on when the caller does not name one.

    A frozen runtime that is *already* installed answers with the prefix it is
    running from, so ``rollback``/``uninstall`` act on the installation the
    operator invoked rather than on a default path they may never have used. Any
    other runtime — a source checkout, or a payload run straight out of its
    build directory — answers with the standard location, which is also where a
    first install lands.
    """
    if is_frozen():
        executable = Path(sys.executable).resolve()
        version_dir = executable.parent
        if version_dir.parent.name == VERSIONS_DIR:
            return version_dir.parent.parent
    return Path.home() / PREFIX_RELATIVE_PATH


def default_bin_dir() -> Path:
    return Path.home() / BIN_RELATIVE_PATH


def read_payload_manifest(payload_dir: Path) -> dict:
    """Read and sanity-check a payload's ``runtime.json``.

    The manifest is the payload's self-description, so it is also the only
    honest source for what the payload *is*: its version, its target, and the
    executable to run. Reading those instead of assuming them is what keeps a
    second target a matter of another manifest rather than another installer.
    """
    path = payload_dir / MANIFEST_NAME
    if not path.is_file():
        raise InstallError(f"no {MANIFEST_NAME} in {payload_dir}: not an orch-hub payload")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise InstallError(f"{path} is not a JSON object")
    version = str(manifest.get("version") or "").strip()
    if not version:
        raise InstallError(f"{path} declares no version")
    executable = payload_dir / str(manifest.get("executable") or COMMAND_NAME)
    if not executable.is_file():
        raise InstallError(f"payload has no executable at {executable}")
    return manifest


def check_target(
    manifest: dict,
    *,
    system: str | None = None,
    machine: str | None = None,
    macos: str | None = None,
) -> None:
    """Refuse a payload this machine cannot run, before anything is written.

    The payload's declared target is compared against the host rather than
    against a hardcoded ``darwin-arm64``: the installer then accepts any target
    it is actually running on, so shipping a second one is a build concern only.
    The minimum macOS is a property of the interpreter the payload carries, read
    out of its Mach-O headers at build time — an older Mac fails here with the
    version it needs instead of on a dynamic-loader error at first launch.
    """
    payload_os = str(manifest.get("os") or "").strip().lower()
    payload_arch = str(manifest.get("arch") or "").strip().lower()
    if not payload_os or not payload_arch:
        raise InstallError("payload manifest does not declare an os/arch target")
    host_os = (system if system is not None else platform.system()).lower()
    host_arch = (machine if machine is not None else platform.machine()).lower()
    if (payload_os, payload_arch) != (host_os, host_arch):
        raise InstallError(
            f"this payload targets {payload_os}-{payload_arch}, "
            f"but this machine is {host_os}-{host_arch}"
        )
    minimum = str(manifest.get("min_macos") or "").strip()
    if payload_os != "darwin" or not minimum:
        return
    host_macos = macos if macos is not None else platform.mac_ver()[0]
    required = _version_tuple(minimum)
    running = _version_tuple(host_macos)
    # An unparseable version on either side is not evidence of a mismatch; the
    # payload still runs or fails on its own terms rather than being refused
    # over a string this check could not read.
    if required and running and running < required:
        raise InstallError(
            f"this payload requires macOS {minimum} or newer, "
            f"but this machine runs {host_macos}"
        )


def read_installation(prefix: Path | None = None) -> Installation:
    """Describe what is installed under ``prefix`` without changing anything."""
    root = _resolve_prefix(prefix)
    versions_dir = root / VERSIONS_DIR
    versions: list[str] = []
    if versions_dir.is_dir():
        versions = sorted(
            (entry.name for entry in versions_dir.iterdir() if entry.is_dir()),
            key=_version_sort_key,
        )
    return Installation(
        prefix=root,
        versions=tuple(versions),
        current=read_current(root),
        previous=read_previous(root),
    )


def read_current(prefix: Path) -> str | None:
    """Version the ``current`` link selects, or ``None`` when nothing is selected.

    Only a link that points at this prefix's own ``versions/<name>`` counts. A
    dangling or foreign link is reported as "nothing selected" rather than as a
    version, so a broken selection cannot be rolled back *to*.
    """
    link = prefix / CURRENT_LINK
    try:
        raw = Path(os.readlink(link))
    except OSError:
        return None
    if raw.parent.name != VERSIONS_DIR or not raw.name:
        return None
    expected = prefix / VERSIONS_DIR / raw.name
    # An absolute link can name a *different* prefix's identically-numbered
    # version; reading it as ours would report a selection this prefix does not
    # have and offer it as a rollback target.
    if (raw if raw.is_absolute() else prefix / raw) != expected:
        return None
    return raw.name if expected.is_dir() else None


def read_previous(prefix: Path) -> str | None:
    """Version selected before the current one, if it is still installed."""
    marker = prefix / PREVIOUS_MARKER
    try:
        name = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name or not (prefix / VERSIONS_DIR / name).is_dir():
        return None
    return name


def select_version(prefix: Path, version: str) -> str | None:
    """Point ``current`` at ``version`` and return what it pointed at before.

    The swap is a rename over the existing link, so a concurrent reader sees the
    old selection or the new one and never a missing ``current``. Selecting the
    version that is already current is a no-op for the previous marker: it would
    otherwise erase the only thing rollback has to go back to.
    """
    root = prefix.expanduser()
    target = root / VERSIONS_DIR / version
    if not target.is_dir():
        raise InstallError(f"version {version} is not installed under {root}")
    link = root / CURRENT_LINK
    if link.exists() and not link.is_symlink():
        raise InstallError(
            f"{link} exists and is not a symlink; remove it and reinstall"
        )
    before = read_current(root)
    incoming = root / INCOMING_LINK
    discard_path(incoming)
    try:
        # Relative, so the whole prefix can be moved or copied without the
        # selection pointing back at where it was created.
        incoming.symlink_to(Path(VERSIONS_DIR) / version)
        os.replace(incoming, link)
    except OSError as exc:
        discard_path(incoming, best_effort=True)
        raise InstallError(f"cannot select version {version}: {exc}") from exc
    if before is not None and before != version:
        _write_previous(root, before)
    return before


def install_version(
    payload_dir: Path,
    *,
    prefix: Path | None = None,
    bin_dir: Path | None = None,
    link: bool = True,
    environ: dict[str, str] | None = None,
) -> InstallResult:
    """Place a payload under the prefix as a version and select it.

    The payload is copied, not moved: it may be a build output in a checkout, or
    an extraction the installer script still owns, and consuming it would make
    installing to a second prefix impossible.
    """
    payload = payload_dir.expanduser().resolve()
    if not payload.is_dir():
        raise InstallError(f"payload directory not found: {payload}")
    manifest = read_payload_manifest(payload)
    check_target(manifest)
    version = str(manifest["version"]).strip()
    root = _resolve_prefix(prefix)
    target = root / VERSIONS_DIR / version
    _refuse_self_replacement(payload, target, version)
    replaced = target.is_dir()
    try:
        place_directory(
            payload,
            target,
            # ``symlinks=True`` is load-bearing, not tidiness: the payload's
            # bundled framework is a tree of relative links, and resolving them
            # into copies produces a runtime that is both larger and differently
            # shaped from the one that was built and audited.
            transfer=lambda source, destination: shutil.copytree(
                source, destination, symlinks=True
            ),
            what=f"version {version}",
        )
    except PlacementError as exc:
        raise InstallError(str(exc)) from exc
    select_version(root, version)
    link_path = _link_command(root, bin_dir) if link else None
    return InstallResult(
        installation=read_installation(root),
        version=version,
        replaced=replaced,
        link_path=link_path,
        link_on_path=_on_path(link_path, environ),
    )


def setup_service(
    options: SetupOptions,
    *,
    runner: Runner = run_command,
    environ: dict[str, str] | None = None,
    token_generator: Callable[[], str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    health_checker: Callable[[str, int], str] | None = None,
) -> SetupResult:
    """Configure and start the resident service from the selected version.

    This is the checkout-independent counterpart of ``launchd deploy``: there is
    no package to install and no virtualenv to create, so the whole operation is
    resolve configuration, write the plist, bootstrap launchd, and confirm the
    service answers. Health is verified rather than assumed, because the first
    thing a fresh install has to establish is that it works at all.
    """
    root = _resolve_prefix(options.prefix)
    installation = read_installation(root)
    if installation.current is None:
        raise InstallError(
            f"no orch-hub version is selected under {root}; "
            "install one with `orch-hub install --from <payload>`"
        )
    deploy_options = DeployOptions(
        program=installation.executable,
        mandrel_path=options.mandrel_path,
        token=options.token,
        host=options.host,
        port=options.port,
        data_dir=options.data_dir,
        plist_path=options.plist_path,
        log_dir=options.log_dir,
        install_package=False,
        verify_mandrel=True,
    )
    result = deploy_service(
        deploy_options,
        runner=runner,
        environ=environ,
        token_generator=token_generator,
        sleeper=sleeper,
    )
    deployment = result.deployment
    health = wait_for_health(
        deployment.host,
        deployment.port,
        checker=health_checker,
        sleeper=sleeper,
    )
    return SetupResult(
        installation=installation,
        deployment=deployment,
        generated_token=result.generated_token,
        health=health,
    )


def upgrade(
    payload_dir: Path,
    *,
    prefix: Path | None = None,
    bin_dir: Path | None = None,
    plist_path: Path | None = None,
    runner: Runner = run_command,
    environ: dict[str, str] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    health_checker: Callable[[str, int], str] | None = None,
) -> UpgradeResult:
    """Install a newer payload and restart the service onto it.

    Nothing about the running configuration is passed here: the plist rewrite
    resolves every setting from the environment and the installed plist, so the
    token, Mandrel checkout, bind address, data directory, and push endpoint all
    survive an upgrade without being repeated on the command line.

    A failure after the new version is selected leaves both versions installed —
    the point of the versioned layout — so the previous one is named in the
    error and can be selected again with ``rollback``.
    """
    root = _resolve_prefix(prefix)
    before = read_installation(root).current
    installed = install_version(
        payload_dir, prefix=root, bin_dir=bin_dir, environ=environ
    )
    try:
        setup = setup_service(
            SetupOptions(prefix=root, plist_path=plist_path),
            runner=runner,
            environ=environ,
            sleeper=sleeper,
            health_checker=health_checker,
        )
    except (InstallError, LaunchdError) as exc:
        recovery = (
            f"; the previous version is still installed — "
            f"run `orch-hub rollback --to {before}`"
            if before and before != installed.version
            else ""
        )
        raise InstallError(
            f"installed {installed.version} but could not start it: {exc}{recovery}"
        ) from exc
    return UpgradeResult(
        install=installed, setup=setup, previous_version=before
    )


def rollback(
    *,
    version: str | None = None,
    prefix: Path | None = None,
    plist_path: Path | None = None,
    runner: Runner = run_command,
    sleeper: Callable[[float], None] = time.sleep,
    health_checker: Callable[[str, int], str] | None = None,
) -> RollbackResult:
    """Select another installed version and restart the service onto it.

    The plist is not rewritten: it names ``<prefix>/current/orch-hub``, so
    changing which code runs is a symlink swap plus a restart. That is why a
    rollback keeps the service's configuration and the hub's data exactly as
    they were — it changes the program, and nothing else.
    """
    root = _resolve_prefix(prefix)
    installation = read_installation(root)
    target = version or installation.previous
    if target is None:
        raise InstallError(
            f"no previous version recorded under {root}; "
            f"name one explicitly (installed: {', '.join(installation.versions) or 'none'})"
        )
    if target == installation.current:
        raise InstallError(f"version {target} is already selected")
    select_version(root, target)
    try:
        restart_service(plist_path=plist_path, runner=runner)
    except LaunchdError as exc:
        # The selection already happened, and it is what the operator asked
        # for — undoing it would hide the change. Say what is true instead: the
        # version moved, the service did not come back, and here is the verb
        # that starts it.
        raise InstallError(
            f"selected {target} but could not restart the service: {exc}; "
            "start it with `orch-hub launchd start`"
        ) from exc
    host, port = installed_bind(plist_path=plist_path)
    health = wait_for_health(
        host, port, checker=health_checker, sleeper=sleeper
    )
    return RollbackResult(
        from_version=installation.current, to_version=target, health=health
    )


def uninstall(
    *,
    prefix: Path | None = None,
    bin_dir: Path | None = None,
    plist_path: Path | None = None,
    purge_data: bool = False,
    data_dir: Path | None = None,
    runner: Runner = run_command,
) -> UninstallResult:
    """Stop the service and remove the installed runtime.

    The hub's data directory is left alone unless it is purged explicitly: run
    history, audit events, and evaluation artifacts are not re-creatable from a
    release, so removing a program must not remove them by default.

    A service that cannot be stopped is reported rather than fatal. The common
    reason is that it was never installed, and refusing to clean up an
    installation because launchd has nothing to unload would leave the operator
    with no way to finish.
    """
    root = _resolve_prefix(prefix)
    resolved_plist = (plist_path or default_plist_path()).expanduser()
    resolved_data = _uninstall_data_dir(data_dir, resolved_plist) if purge_data else None

    service_error: str | None = None
    try:
        stop_service(runner=runner)
    except LaunchdError as exc:
        service_error = str(exc)

    plist_removed = _remove_file(resolved_plist)
    link_path, link_removed = _unlink_command(root, bin_dir)
    data_removed = False
    if resolved_data is not None:
        data_removed = _remove_tree(resolved_data)
    # Last, because on a frozen runtime this is the directory the running
    # executable lives in: everything above still needs the payload's files.
    prefix_removed = _remove_tree(root)
    return UninstallResult(
        prefix=root,
        prefix_removed=prefix_removed,
        plist_path=resolved_plist,
        plist_removed=plist_removed,
        link_path=link_path,
        link_removed=link_removed,
        service_error=service_error,
        data_dir=resolved_data,
        data_removed=data_removed,
    )


def _resolve_prefix(prefix: Path | None) -> Path:
    return (prefix or default_prefix()).expanduser()


def _refuse_self_replacement(payload: Path, target: Path, version: str) -> None:
    """Refuse to overwrite the version this process is running from.

    Replacing a payload directory removes the files the running executable still
    needs for anything it has not imported yet, so the failure would appear
    later and somewhere else. The upgrade path never hits this — the installer
    runs the *new* payload's binary — so failing closed costs nothing and
    removes a class of unexplainable breakage.
    """
    # Both sides are resolved: a prefix under a symlinked directory (``/tmp`` on
    # macOS is one) would otherwise compare unequal to the very path it names.
    resolved = target.resolve()
    if payload == resolved:
        raise InstallError(f"version {version} is already installed at {target}")
    if not is_frozen():
        return
    running = Path(sys.executable).resolve()
    if running == resolved or resolved in running.parents:
        raise InstallError(
            f"cannot replace version {version} while running from it; "
            "run the new payload's own `orch-hub install`"
        )


def _link_command(prefix: Path, bin_dir: Path | None) -> Path:
    """Link ``<bin-dir>/orch-hub`` at the selected version's executable."""
    directory = (bin_dir or default_bin_dir()).expanduser()
    link = directory / COMMAND_NAME
    if link.exists() and not link.is_symlink():
        raise InstallError(
            f"{link} already exists and is not a symlink; "
            "move it aside or install with --no-link"
        )
    directory.mkdir(parents=True, exist_ok=True)
    incoming = directory / f".{COMMAND_NAME}.incoming"
    discard_path(incoming)
    try:
        incoming.symlink_to(prefix / CURRENT_LINK / COMMAND_NAME)
        os.replace(incoming, link)
    except OSError as exc:
        discard_path(incoming, best_effort=True)
        raise InstallError(f"cannot link {link}: {exc}") from exc
    return link


def _unlink_command(prefix: Path, bin_dir: Path | None) -> tuple[Path | None, bool]:
    """Remove the command link, but only if it points into this prefix.

    A same-named command that is not ours — another installation, or something
    the operator put there — is left alone. Uninstall removes what it installed.
    """
    directory = (bin_dir or default_bin_dir()).expanduser()
    link = directory / COMMAND_NAME
    if not link.is_symlink():
        return (link if link.exists() else None), False
    try:
        target = Path(os.readlink(link))
    except OSError:
        return link, False
    if not target.is_absolute():
        target = (directory / target).resolve(strict=False)
    if target != prefix / CURRENT_LINK / COMMAND_NAME:
        return link, False
    return link, _remove_file(link)


def _on_path(link_path: Path | None, environ: dict[str, str] | None) -> bool:
    """Whether the linked command is reachable by name in this environment."""
    if link_path is None:
        return False
    env = os.environ if environ is None else environ
    entries = (env.get("PATH") or "").split(os.pathsep)
    parent = str(link_path.parent)
    return any(entry and Path(entry).expanduser() == Path(parent) for entry in entries)


def _uninstall_data_dir(data_dir: Path | None, plist_path: Path) -> Path:
    """Data directory a purge would remove: explicit, else the service's own."""
    if data_dir is not None:
        return data_dir.expanduser()
    return installed_data_dir(plist_path=plist_path)


def _remove_file(path: Path) -> bool:
    if not path.is_symlink() and not path.exists():
        return False
    try:
        path.unlink()
    except OSError as exc:
        raise InstallError(f"cannot remove {path}: {exc}") from exc
    return True


def _remove_tree(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise InstallError(f"cannot remove {path}: {exc}") from exc
    return True


def _write_previous(prefix: Path, version: str) -> None:
    try:
        (prefix / PREVIOUS_MARKER).write_text(f"{version}\n", encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"cannot record the previous version: {exc}") from exc


def _version_tuple(raw: str) -> tuple[int, ...]:
    """Numeric prefix of a dotted version, or ``()`` when it is not numeric."""
    parts: list[int] = []
    for chunk in str(raw or "").strip().split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return tuple(parts)


def _version_sort_key(name: str) -> tuple[tuple[int, ...], str]:
    """Order versions numerically where possible, by name otherwise."""
    return (_version_tuple(name), name)
