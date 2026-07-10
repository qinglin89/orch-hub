"""Command-line entry point for orch-hub.

The CLI is a thin adapter over the same application functions a future FastAPI
service will call: :func:`orch_hub.registry.discover_repos` and
:func:`orch_hub.readiness.check_readiness`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from .config import Config
from .launchd import (
    DeployOptions,
    LABEL,
    LaunchdError,
    deploy_service,
    installed_bark_url,
    restart_service,
    start_service,
    status_service,
    stop_service,
)
from .git_meta import latest_commit
from .housekeeping import housekeeping_pending
from .models import ManagedRepo, ReadinessReport, repos_payload
from .notifications import build_notifier, probe_notification
from .readiness import check_readiness
from .registry import RegistryError, discover_repos


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "repos":
        return _cmd_repos(json_output=args.json)
    if args.command == "launchd":
        return args.launchd_handler(args)
    if args.command == "notify":
        return args.notify_handler(args)
    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orch_hub.cli",
        description="orch-hub: discover managed repos and report launch readiness.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    repos_p = sub.add_parser("repos", help="list managed repos with readiness")
    repos_p.add_argument(
        "--json",
        action="store_true",
        help="emit stable JSON instead of human-readable text",
    )
    launchd_p = sub.add_parser(
        "launchd", help="manage the macOS launchd service"
    )
    launchd_sub = launchd_p.add_subparsers(dest="launchd_command", required=True)

    deploy_p = launchd_sub.add_parser(
        "deploy",
        help="install/update the venv, write the plist, and bootstrap launchd",
    )
    _add_launchd_deploy_args(deploy_p)
    deploy_p.set_defaults(launchd_handler=_cmd_launchd_deploy)

    update_p = launchd_sub.add_parser(
        "update",
        help="alias for deploy; reinstall package and reload launchd",
    )
    _add_launchd_deploy_args(update_p)
    update_p.set_defaults(launchd_handler=_cmd_launchd_deploy)

    start_p = launchd_sub.add_parser(
        "start",
        help="bootstrap the installed launchd service without reinstalling",
    )
    _add_plist_path_arg(start_p)
    start_p.set_defaults(launchd_handler=_cmd_launchd_start)

    stop_p = launchd_sub.add_parser(
        "stop",
        help="unload the launchd service without stopping orchestrator runs",
    )
    stop_p.set_defaults(launchd_handler=_cmd_launchd_stop)

    restart_p = launchd_sub.add_parser(
        "restart",
        help="stop and start the installed launchd service without reinstalling",
    )
    _add_plist_path_arg(restart_p)
    restart_p.set_defaults(launchd_handler=_cmd_launchd_restart)

    status_p = launchd_sub.add_parser(
        "status", help="show launchd load state and health probe"
    )
    _add_plist_path_arg(status_p)
    status_p.set_defaults(launchd_handler=_cmd_launchd_status)

    # Backend-agnostic notification commands. `test` is a delivery check; the
    # top-level verb is `notify`, not the concrete backend (Bark), so a later
    # backend does not force a CLI rename.
    notify_p = sub.add_parser(
        "notify", help="send and verify push notifications"
    )
    notify_sub = notify_p.add_subparsers(dest="notify_command", required=True)
    notify_test_p = notify_sub.add_parser(
        "test",
        help="send a test notification to verify push delivery works",
    )
    _add_plist_path_arg(notify_test_p)
    notify_test_p.set_defaults(notify_handler=_cmd_notify_test)
    return parser


def _add_launchd_deploy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--checkout",
        type=Path,
        default=None,
        help="orch-hub checkout to deploy (default: nearest checkout above cwd)",
    )
    parser.add_argument(
        "--ai-native",
        type=Path,
        default=None,
        help="ai-native-development checkout (default: ORCH_HUB_AI_NATIVE_DEPLOYMENT or built-in default)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="bearer token for the service (default: env, existing plist, or generated)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="bind host (default: ORCH_HUB_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="bind port (default: ORCH_HUB_PORT or 8765)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="hub data dir (default: ORCH_HUB_DATA_DIR or ~/.orch-hub)",
    )
    parser.add_argument(
        "--plist-path",
        type=Path,
        default=None,
        help="installed plist path (default: ~/Library/LaunchAgents/com.qinglin89.orch-hub.plist)",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="launchd stdout/stderr log dir (default: ~/Library/Logs/orch-hub)",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="rewrite/reload launchd without running venv creation or pip install",
    )


def _add_plist_path_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--plist-path",
        type=Path,
        default=None,
        help="installed plist path (default: ~/Library/LaunchAgents/com.qinglin89.orch-hub.plist)",
    )


def _cmd_repos(*, json_output: bool) -> int:
    try:
        # Config.from_env raises ValueError for malformed service settings
        # (e.g. ORCH_HUB_PORT); report it as an operational error, not a crash.
        config = Config.from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        repos = discover_repos(config)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    pairs = [(repo, check_readiness(repo, config)) for repo in repos]
    if json_output:
        # `repos --json` and GET /api/repos are pinned identical, so the JSON
        # form carries display-only status metadata too. Commit lookup is
        # best-effort (None on any git failure) and computed only here — the
        # human output does not show it, so it pays no git-log cost.
        entries = [
            (repo, report, latest_commit(repo), housekeeping_pending(repo))
            for repo, report in pairs
        ]
        print(json.dumps(repos_payload(entries), indent=2))
    else:
        _print_human(pairs)
    return 0


def _cmd_launchd_deploy(args: argparse.Namespace) -> int:
    try:
        result = deploy_service(
            DeployOptions(
                checkout=args.checkout,
                ai_native_path=args.ai_native,
                token=args.token,
                host=args.host,
                port=args.port,
                data_dir=args.data_dir,
                plist_path=args.plist_path,
                log_dir=args.log_dir,
                install_package=not args.skip_install,
            )
        )
    except LaunchdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    action = args.launchd_command
    deployment = result.deployment
    print(f"launchd {action} ok")
    print(f"label: {LABEL}")
    print(f"plist: {deployment.plist_path}")
    print(f"program: {deployment.api_bin}")
    print(f"dashboard: {deployment.dashboard_url}")
    if result.generated_token:
        print(f"generated ORCH_HUB_TOKEN: {deployment.token}")
    else:
        print(f"token: {deployment.token_source}")
    # Never echo the Bark URL: it embeds a device-key secret.
    print(f"bark push: {'enabled' if deployment.bark_url else 'disabled'}")
    print(
        "stop semantics: launchd stop unloads the hub service only; detached "
        "orchestrator runs are not signaled."
    )
    return 0


def _cmd_launchd_stop(args: argparse.Namespace) -> int:
    try:
        result = stop_service()
    except LaunchdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if result.stopped:
        print("launchd service stopped")
    elif result.already_stopped:
        print("launchd service already stopped")
    print(
        "orchestrator runs were not stopped; stop individual runs through the "
        "dashboard/API if needed."
    )
    return 0


def _cmd_launchd_start(args: argparse.Namespace) -> int:
    try:
        result = start_service(plist_path=args.plist_path)
    except LaunchdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if result.started:
        print("launchd service started")
    elif result.already_started:
        print("launchd service already started")
    print("deploy/update are required after code or plist changes.")
    return 0


def _cmd_launchd_restart(args: argparse.Namespace) -> int:
    try:
        result = restart_service(plist_path=args.plist_path)
    except LaunchdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    stop_state = "stopped" if result.stop.stopped else "already stopped"
    start_state = "started" if result.start.started else "already started"
    print(f"launchd service restarted ({stop_state}, {start_state})")
    print("restart does not reinstall package code; use update after code changes.")
    return 0


def _cmd_launchd_status(args: argparse.Namespace) -> int:
    try:
        result = status_service(plist_path=args.plist_path)
    except LaunchdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"label: {result.label}")
    print(f"plist: {result.plist_path}")
    print(f"installed: {'yes' if result.plist_installed else 'no'}")
    if result.plist_error:
        print(f"plist_error: {result.plist_error}")
    print(f"loaded: {'yes' if result.loaded else 'no'}")
    if result.program:
        print(f"program: {result.program}")
    print(f"dashboard: {result.dashboard_url}")
    print(f"health: {result.health}")
    if not result.loaded and result.launchctl_output:
        print(f"launchctl: {result.launchctl_output}")
    return 0


def _cmd_notify_test(args: argparse.Namespace) -> int:
    """Send one test notification through the configured backend.

    Exits 0 only after the backend accepts delivery, so it distinguishes "push
    is configured" from "push delivery actually works". Delivery is direct (not
    through the run manager), so it touches no run/history/control-dir state.
    """
    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # The resident service keeps ORCH_HUB_BARK_URL in its launchd plist, not in
    # an interactive shell. When the env is unset, fall back to the installed
    # plist so `notify test` can verify the service config from a plain shell.
    if not config.bark_url:
        config = replace(
            config, bark_url=installed_bark_url(plist_path=args.plist_path)
        )
    notifier = build_notifier(config)
    if notifier is None:
        print(
            "error: no notification backend configured "
            "(set ORCH_HUB_BARK_URL or deploy the launchd service with it)",
            file=sys.stderr,
        )
        return 1
    try:
        notifier.notify(probe_notification())
    except Exception as exc:  # delivery is inherently unreliable
        # Surface the failure but never the endpoint: the Bark URL embeds a
        # device-key secret, and some transport errors echo the URL back.
        detail = _redact_secret(f"{type(exc).__name__}: {exc}", config.bark_url)
        print(f"error: notification delivery failed: {detail}", file=sys.stderr)
        return 1
    print("test notification sent; check your device")
    return 0


def _redact_secret(text: str, secret: str | None) -> str:
    """Blank the configured endpoint out of an error string.

    A misconfigured URL can surface in a raised exception's text (for example a
    schemeless value in a urllib ``unknown url type`` error), so scrub the exact
    resolved URL before printing it.
    """
    if secret and secret in text:
        return text.replace(secret, "<redacted>")
    return text


def _print_human(
    pairs: Sequence[tuple[ManagedRepo, ReadinessReport]],
) -> None:
    if not pairs:
        print("No managed repos found.")
        return
    for repo, report in pairs:
        status = "READY" if report.ready else "NOT READY"
        print(f"{status:9}  {repo.name}  {repo.path}")
        for check in report.failures:
            print(f"             - {check.name}: {check.message}")


if __name__ == "__main__":
    sys.exit(main())
