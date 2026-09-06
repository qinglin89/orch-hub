"""Command-line entry point for orch-hub.

The CLI exposes registry/readiness inspection, resident-service management,
notification probes, and task-evaluation operations. Shared domain services
keep its inventory payloads and operational behavior aligned with the HTTP
surface.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .config import (
    DATA_DIR_ENV,
    DEFAULT_DATA_DIR,
    DEFAULT_HOST,
    DEFAULT_PORT,
    HOST_ENV,
    LEGACY_MANDREL_PATH_ENV,
    MANDREL_PATH_ENV,
    PORT_ENV,
    Config,
)
from .launchd import (
    DeployOptions,
    LABEL,
    LaunchdError,
    deploy_service,
    installed_bark_url,
    installed_mandrel_path,
    restart_service,
    start_service,
    status_service,
    stop_service,
)
from .install import (
    InstallError,
    SetupOptions,
    install_version,
    read_installation,
    rollback,
    setup_service,
    uninstall,
    upgrade,
)
from .git_meta import latest_commit
from .housekeeping import housekeeping_pending
from .models import (
    EvaluationQueueEntry,
    EvaluationQueueSnapshot,
    ManagedRepo,
    ReadinessReport,
    repos_payload,
)
from .notifications import build_notifier, probe_notification
from .readiness import check_readiness
from .registry import RegistryError, discover_repos
from .resources import runtime_info

if TYPE_CHECKING:  # imported lazily in the handlers; see `_open_catalog`
    from .models import CatalogAudit
    from .report_catalog import ReportCatalog


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # `statusline` wraps the operator's own status line command, so everything
    # after `--` is that command's argv and must never reach our parser — its
    # flags are not ours to interpret. The split is scoped to that one verb
    # rather than applied to every argv: the top-level parser takes no options
    # before the subcommand, so `raw[0]` already decides it, and no other verb
    # gets a silent change of meaning for a `--` an operator happened to type.
    wrapped: list[str] = []
    if raw[:1] == [STATUSLINE_COMMAND]:
        from .statusline import split_argv

        raw, wrapped = split_argv(raw)
    parser = _build_parser()
    args = parser.parse_args(raw)
    if args.command == STATUSLINE_COMMAND:
        return _cmd_statusline(args, wrapped)
    if args.command == "repos":
        return _cmd_repos(json_output=args.json)
    if args.command == "launchd":
        return args.launchd_handler(args)
    if args.command == "notify":
        return args.notify_handler(args)
    if args.command == "eval":
        return args.eval_handler(args)
    if args.command == "serve":
        return _cmd_serve(host=args.host, port=args.port)
    if args.command == "version":
        return _cmd_version(json_output=args.json)
    if args.command in _INSTALL_COMMANDS:
        return args.install_handler(args)
    parser.error(f"unknown command: {args.command}")  # pragma: no cover
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orch-hub",
        description=(
            "Local control plane for a portfolio of Mandrel-managed "
            "AI coding workflows."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    repos_p = sub.add_parser("repos", help="list managed repos with readiness")
    _add_json_arg(repos_p)

    # `serve` is how a runtime with no console scripts starts the HTTP service:
    # a compiled release ships one executable, so the `orch-hub-api` entry point
    # a source install gets from packaging metadata does not exist there. Both
    # runtimes therefore agree on one spelling of "serve the API".
    serve_p = sub.add_parser(
        "serve", help="serve the HTTP API and dashboard in the foreground"
    )
    serve_p.add_argument(
        "--host",
        default=None,
        help=f"bind address (default: {HOST_ENV} or {DEFAULT_HOST})",
    )
    serve_p.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"bind port (default: {PORT_ENV} or {DEFAULT_PORT})",
    )

    # `version` identifies the runtime an operator is actually running: a
    # compiled release has no importable package to inspect and no checkout to
    # read a version from, so the binary has to be able to say it itself.
    version_p = sub.add_parser("version", help="show the orch-hub runtime version")
    _add_json_arg(version_p)
    _add_install_commands(sub)

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

    # The Claude Code statusLine wrapper, as a verb. Same reason `serve` is one:
    # a compiled release ships one executable, so the `orch-hub-statusline`
    # console script a source install gets from packaging metadata is not
    # reachable there — and the module ships in the payload regardless, because
    # the payload carries the whole first-party package. Both runtimes therefore
    # agree on one spelling of "wrap my status line", and the sink stays an
    # available fallback carrier on a compiled install (it is the only carrier
    # that also names the plan tier). Installing it remains the operator's own
    # edit to their own settings; the hub never writes `~/.claude/**`.
    statusline_p = sub.add_parser(
        STATUSLINE_COMMAND,
        help="capture Claude Code statusLine usage, then run the wrapped command",
        epilog="Everything after `--` is your own statusLine command (argv, no shell).",
    )
    statusline_p.add_argument(
        "--sink",
        help="explicit sink path (default: <data-dir>/usage/claude-statusline.json)",
    )
    statusline_p.add_argument(
        "--data-dir",
        help=f"hub data dir (default: ${DATA_DIR_ENV} or {DEFAULT_DATA_DIR})",
    )
    statusline_p.add_argument(
        "--print-sink",
        action="store_true",
        help="print the resolved sink path and exit (troubleshooting; reads no stdin)",
    )

    # Optional task-evaluation operations. `doctor` is a config/availability smoke
    # check for the Layer 2 semantic evaluator; the verb group is `eval` so later
    # evaluation ops (e.g. prune) can be added without a rename.
    eval_p = sub.add_parser("eval", help="task-evaluation operations")
    eval_sub = eval_p.add_subparsers(dest="eval_command", required=True)
    eval_doctor_p = eval_sub.add_parser(
        "doctor",
        help="check the semantic evaluator is enabled, configured, and available",
    )
    eval_doctor_p.set_defaults(eval_handler=_cmd_eval_doctor)

    # The queue read. It earns a CLI verb because the HTTP one needs a running
    # service and a bearer token, and the moment an operator most wants to see the
    # queue is the moment the hub is misbehaving. This reads the same durable store
    # the service does, so it answers whether or not the service is up — and, like
    # the catalog verbs, it is a read that changes nothing. Starting an evaluation
    # and cancelling one stay on the HTTP/dashboard side: those are writes to live
    # service state, and the dashboard is where the operator reading this is.
    eval_queue_p = eval_sub.add_parser(
        "queue",
        help="report the Layer 2 evaluation job store; changes nothing",
    )
    _add_json_arg(eval_queue_p)
    eval_queue_p.set_defaults(eval_handler=_cmd_eval_queue)

    # The global report catalog's operator surface. Grouped under `catalog`
    # because the two verbs are one workflow — diagnose, then decide — and
    # deliberately CLI-only: `repair` rewrites durable publication state on the
    # local disk, which is not a thing to expose on the remote dashboard.
    eval_catalog_p = eval_sub.add_parser(
        "catalog", help="inspect or repair the global report catalog"
    )
    eval_catalog_sub = eval_catalog_p.add_subparsers(
        dest="catalog_command", required=True
    )
    catalog_verify_p = eval_catalog_sub.add_parser(
        "verify",
        help="report catalog integrity; changes nothing on disk",
    )
    _add_json_arg(catalog_verify_p)
    catalog_verify_p.set_defaults(eval_handler=_cmd_eval_catalog_verify)

    catalog_repair_p = eval_catalog_sub.add_parser(
        "repair",
        help="drop provably redundant catalog lines, preserving every published id",
    )
    catalog_repair_p.add_argument(
        "--confirm",
        action="store_true",
        help="apply the plan (without it, the plan is printed and nothing is written)",
    )
    _add_json_arg(catalog_repair_p)
    catalog_repair_p.set_defaults(eval_handler=_cmd_eval_catalog_repair)
    return parser


STATUSLINE_COMMAND = "statusline"
"""The verb whose trailing argv belongs to a wrapped command, not to us.

Named because `main` has to recognise it *before* building the parser, and a
literal repeated in the dispatch and the split would be a rename away from
silently passing an operator's status-line flags to our own parser."""


# Commands that operate on a user-local installed runtime rather than on a
# source checkout. Grouped only for dispatch: each is a top-level verb because
# an engineer running a release types them directly, and a release has no
# checkout for `launchd deploy` to install from.
_INSTALL_COMMANDS = frozenset(
    {"install", "setup", "upgrade", "rollback", "versions", "uninstall"}
)


def _add_install_commands(sub) -> None:
    install_p = sub.add_parser(
        "install",
        help="place a built payload under the install prefix and select it",
    )
    _add_payload_arg(install_p)
    _add_prefix_arg(install_p)
    _add_bin_dir_arg(install_p)
    install_p.add_argument(
        "--no-link",
        action="store_true",
        help="do not link the orch-hub command into the bin dir",
    )
    _add_json_arg(install_p)
    install_p.set_defaults(install_handler=_cmd_install)

    setup_p = sub.add_parser(
        "setup",
        help="configure and start the resident service from the installed runtime",
    )
    _add_prefix_arg(setup_p)
    setup_p.add_argument(
        "--mandrel",
        type=Path,
        default=None,
        help=(
            "mandrel checkout (required on first setup unless "
            f"{MANDREL_PATH_ENV} is set; later runs reuse the installed plist)"
        ),
    )
    setup_p.add_argument(
        "--token",
        default=None,
        help="bearer token for the service (default: env, existing plist, or generated)",
    )
    setup_p.add_argument(
        "--host", default=None, help=f"bind host (default: {HOST_ENV} or {DEFAULT_HOST})"
    )
    setup_p.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"bind port (default: {PORT_ENV} or {DEFAULT_PORT})",
    )
    setup_p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="hub data dir (default: ORCH_HUB_DATA_DIR or ~/.orch-hub)",
    )
    setup_p.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="launchd stdout/stderr log dir (default: ~/Library/Logs/orch-hub)",
    )
    _add_plist_path_arg(setup_p)
    setup_p.set_defaults(install_handler=_cmd_setup)

    upgrade_p = sub.add_parser(
        "upgrade",
        help="install a newer payload and restart the service onto it",
    )
    _add_payload_arg(upgrade_p)
    _add_prefix_arg(upgrade_p)
    _add_bin_dir_arg(upgrade_p)
    _add_plist_path_arg(upgrade_p)
    upgrade_p.set_defaults(install_handler=_cmd_upgrade)

    rollback_p = sub.add_parser(
        "rollback",
        help="select the previously installed version and restart the service",
    )
    rollback_p.add_argument(
        "--to",
        dest="version",
        default=None,
        help="version to select (default: the previously selected one)",
    )
    _add_prefix_arg(rollback_p)
    _add_plist_path_arg(rollback_p)
    rollback_p.set_defaults(install_handler=_cmd_rollback)

    versions_p = sub.add_parser(
        "versions", help="list installed runtime versions"
    )
    _add_prefix_arg(versions_p)
    _add_json_arg(versions_p)
    versions_p.set_defaults(install_handler=_cmd_versions)

    uninstall_p = sub.add_parser(
        "uninstall", help="stop the service and remove the installed runtime"
    )
    _add_prefix_arg(uninstall_p)
    _add_bin_dir_arg(uninstall_p)
    _add_plist_path_arg(uninstall_p)
    uninstall_p.add_argument(
        "--purge-data",
        action="store_true",
        help="also delete the hub data dir (run history, evaluations); needs --yes",
    )
    uninstall_p.add_argument(
        "--yes",
        action="store_true",
        help="confirm an irreversible --purge-data removal",
    )
    uninstall_p.set_defaults(install_handler=_cmd_uninstall)


def _add_payload_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--from",
        dest="payload",
        type=Path,
        required=True,
        metavar="PAYLOAD",
        help="built payload directory containing the runtime and runtime.json",
    )


def _add_prefix_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--prefix",
        type=Path,
        default=None,
        help="install prefix (default: the running install, else ~/.local/orch-hub)",
    )


def _add_bin_dir_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bin-dir",
        type=Path,
        default=None,
        help="directory the orch-hub command is linked into (default: ~/.local/bin)",
    )


def _add_json_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit stable JSON instead of human-readable text",
    )


def _add_launchd_deploy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--checkout",
        type=Path,
        default=None,
        help="orch-hub checkout to deploy (default: nearest checkout above cwd)",
    )
    parser.add_argument(
        "--mandrel",
        type=Path,
        default=None,
        help=(
            "mandrel checkout (required on first deploy unless "
            "ORCH_HUB_MANDREL_PATH or legacy ORCH_HUB_AI_NATIVE_DEPLOYMENT "
            "is set; updates reuse the installed plist)"
        ),
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


def _cmd_serve(*, host: str | None, port: int | None) -> int:
    """Serve the API/dashboard in the foreground until interrupted.

    The FastAPI stack is imported here rather than at module scope so `repos`,
    `launchd`, and `notify` keep starting without paying for it.
    """
    try:
        from .api import run as run_api

        run_api(host=host, port=port)
    except ValueError as exc:
        # Malformed service settings (e.g. ORCH_HUB_PORT) surface both while
        # importing the module-level app and while the server resolves its own
        # configuration; both are the CLI's usual operational error.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive interrupt
        return 0
    except OSError as exc:
        # A taken port or an unbindable address is an operational error, not a
        # crash: it is the common way `serve` fails on a machine that already
        # runs the resident service.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_version(*, json_output: bool) -> int:
    info = runtime_info()
    if json_output:
        print(json.dumps(info.to_dict(), indent=2))
    else:
        runtime = "frozen" if info.frozen else "source"
        print(f"orch-hub {info.version} ({runtime}, python {info.python_version})")
    return 0


def _cmd_statusline(args: argparse.Namespace, wrapped: Sequence[str]) -> int:
    """Run the Claude Code statusLine writer.

    Imported here, not at module scope: the writer pulls in `orch_hub.usage` and
    through it the run stack, which every other verb would otherwise pay for.
    The behavior itself is the writer's — this verb only supplies the already
    split-off wrapped argv, so the two spellings cannot drift.
    """
    from .statusline import run as run_statusline

    return run_statusline(
        sink=args.sink,
        data_dir=args.data_dir,
        print_sink=args.print_sink,
        wrapped=wrapped,
    )


def _cmd_install(args: argparse.Namespace) -> int:
    try:
        result = install_version(
            args.payload,
            prefix=args.prefix,
            bin_dir=args.bin_dir,
            link=not args.no_link,
        )
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0
    verb = "reinstalled" if result.replaced else "installed"
    print(f"{verb} orch-hub {result.version}")
    print(f"prefix: {result.installation.prefix}")
    print(f"current: {result.installation.current}")
    if result.link_path:
        print(f"command: {result.link_path}")
        if not result.link_on_path:
            print(f"note: {result.link_path.parent} is not on PATH; add it with")
            print(f'  export PATH="{result.link_path.parent}:$PATH"')
    print("next: orch-hub setup --mandrel /absolute/path/to/mandrel")
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    try:
        result = setup_service(
            SetupOptions(
                prefix=args.prefix,
                mandrel_path=args.mandrel,
                token=args.token,
                host=args.host,
                port=args.port,
                data_dir=args.data_dir,
                plist_path=args.plist_path,
                log_dir=args.log_dir,
            )
        )
    except (InstallError, LaunchdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("orch-hub setup ok")
    print(f"version: {result.installation.current}")
    _print_service_summary(result.deployment, result.generated_token)
    print(f"health: {result.health}")
    return _health_exit(result.health)


def _cmd_upgrade(args: argparse.Namespace) -> int:
    try:
        result = upgrade(
            args.payload,
            prefix=args.prefix,
            bin_dir=args.bin_dir,
            plist_path=args.plist_path,
        )
    except (InstallError, LaunchdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    previous = result.previous_version or "none"
    print(f"upgraded orch-hub {previous} -> {result.install.version}")
    _print_service_summary(result.setup.deployment, result.setup.generated_token)
    print(f"health: {result.setup.health}")
    return _health_exit(result.setup.health)


def _cmd_rollback(args: argparse.Namespace) -> int:
    try:
        result = rollback(
            version=args.version, prefix=args.prefix, plist_path=args.plist_path
        )
    except (InstallError, LaunchdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"rolled back orch-hub {result.from_version or 'none'} -> {result.to_version}")
    print(f"health: {result.health}")
    return _health_exit(result.health)


def _cmd_versions(args: argparse.Namespace) -> int:
    try:
        installation = read_installation(args.prefix)
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(installation.to_dict(), indent=2))
        return 0
    print(f"prefix: {installation.prefix}")
    if not installation.versions:
        print("no versions installed")
        return 0
    for version in installation.versions:
        marks = []
        if version == installation.current:
            marks.append("current")
        if version == installation.previous:
            marks.append("previous")
        suffix = f" ({', '.join(marks)})" if marks else ""
        print(f"{version}{suffix}")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    if args.purge_data and not args.yes:
        print(
            "error: --purge-data deletes run history and evaluation artifacts; "
            "pass --yes to confirm",
            file=sys.stderr,
        )
        return 1
    try:
        result = uninstall(
            prefix=args.prefix,
            bin_dir=args.bin_dir,
            plist_path=args.plist_path,
            purge_data=args.purge_data,
        )
    except (InstallError, LaunchdError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("orch-hub uninstalled")
    print(f"plist: {'removed' if result.plist_removed else 'not installed'}")
    if result.link_removed and result.link_path:
        print(f"command link: removed {result.link_path}")
    elif result.link_path:
        print(f"command link: left {result.link_path} (not ours)")
    print(f"prefix: {'removed' if result.prefix_removed else 'not present'}")
    if result.data_removed and result.data_dir:
        print(f"data dir: removed {result.data_dir}")
    else:
        print("data dir: kept (use --purge-data --yes to delete it)")
    print(
        "orchestrator runs were not stopped; detached runs keep their own "
        "process sessions."
    )
    return 0


def _print_service_summary(deployment, generated_token: bool) -> None:
    """Shared post-configuration summary for setup and upgrade.

    Secrets are handled the same way the launchd deploy path handles them: a
    generated token is printed exactly once because it exists nowhere else yet,
    and the Bark URL is never printed at all because it embeds a device key.
    """
    print(f"label: {LABEL}")
    print(f"plist: {deployment.plist_path}")
    print(f"program: {deployment.program}")
    print(f"mandrel: {deployment.mandrel_path}")
    print(f"data dir: {deployment.data_dir}")
    print(f"dashboard: {deployment.dashboard_url}")
    if generated_token:
        print(f"generated ORCH_HUB_TOKEN: {deployment.token}")
    else:
        print(f"token: {deployment.token_source}")
    print(f"bark push: {'enabled' if deployment.bark_url else 'disabled'}")


def _health_exit(health: str) -> int:
    """Non-zero when the service did not answer, so a scripted install notices."""
    if health == "ok":
        return 0
    print(f"error: service did not become healthy: {health}", file=sys.stderr)
    return 1


def _cmd_launchd_deploy(args: argparse.Namespace) -> int:
    try:
        result = deploy_service(
            DeployOptions(
                checkout=args.checkout,
                mandrel_path=args.mandrel,
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
    print(f"program: {deployment.program}")
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
    environ = dict(os.environ)
    configured_mandrel = (environ.get(MANDREL_PATH_ENV) or "").strip() or (
        environ.get(LEGACY_MANDREL_PATH_ENV) or ""
    ).strip()
    if not configured_mandrel:
        mandrel_path = installed_mandrel_path(plist_path=args.plist_path)
        if mandrel_path is not None:
            environ[MANDREL_PATH_ENV] = str(mandrel_path)
    try:
        config = Config.from_env(environ)
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


def _cmd_eval_doctor(args: argparse.Namespace) -> int:
    """Smoke check the Layer 2 semantic evaluator's config and availability.

    A configuration/availability probe, deliberately NOT a full evaluation: it
    runs no ``claude`` turn and makes no model call, so it costs nothing. Exit 0
    only when the subsystem is enabled AND the backend is available (the ``claude``
    CLI resolves on the augmented PATH); a disabled subsystem or an unresolved CLI
    exits non-zero so a script can detect "Layer 2 not ready". The Layer 1 static
    evaluator has no external dependency, so it needs no probe. This never touches
    run/Start/Stop/log state.
    """
    from .semantic import ClaudeSemanticBackend

    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"evaluation enabled: {'yes' if config.evaluation_enabled else 'no'}")
    if not config.evaluation_enabled:
        print(
            "semantic evaluator: unavailable (task evaluation is disabled via "
            "ORCH_HUB_EVAL_ENABLED)"
        )
        return 1
    doctor = ClaudeSemanticBackend(model=config.eval_model).doctor()
    print(f"backend: {doctor.backend}")
    print(f"model: {doctor.model or 'n/a'}")
    print(f"schema version: {doctor.schema_version}")
    print(
        "retention: "
        + (
            f"keep {config.eval_retention} newest per task"
            if config.eval_retention > 0
            else "disabled (unbounded)"
        )
    )
    print(f"available: {'yes' if doctor.available else 'no'}")
    if doctor.detail:
        print(f"detail: {doctor.detail}")
    return 0 if doctor.available else 1


def _cmd_eval_queue(args: argparse.Namespace) -> int:
    """Report the cross-repo state of the semantic-job store. Reads only.

    The same projection the dashboard's *Layer 2 evaluations* section renders
    (:func:`orch_hub.eval_dispatch.queue_snapshot`), reached without a running
    service or a bearer token. That is the point: a semantic job's outcomes are
    absences — a turn that failed hours ago, a legacy record nobody will ever
    promote — and the moment they matter most is the moment the hub itself is the
    thing misbehaving, when the HTTP route is exactly what may not answer. So this
    reads the durable store directly rather than calling the service.

    It reads the store, which is a different claim from reading *the running hub*:
    the records come from whichever ``ORCH_HUB_DATA_DIR`` this shell resolves, so
    the first line prints the tree that was walked. A shell whose data dir differs
    from the launchd service's answers honestly about a hub nobody is running.

    Exit code says whether the queue could be **read**, not what it holds — a
    ``failed`` record is a retained outcome and a legacy held one is inert, and
    either would make a health-style exit code lie. Only an operational failure
    (malformed service env, evaluation disabled, a store that cannot be walked)
    exits non-zero.

    Deliberately read-only. Starting an evaluation and cancelling one are writes to
    live service state and stay on the HTTP/dashboard surface, where the operator
    reading this already is.
    """
    # Function-local for the reason `_open_catalog` imports its catalog locally:
    # `semantic_jobs` pulls in the whole evaluation/semantic chain, which `repos`
    # and the launchd verbs have no use for. The printers below import from the
    # same modules for the same reason — by then it is a `sys.modules` hit.
    from .eval_dispatch import queue_snapshot
    from .semantic_jobs import jobs_root, list_persisted_jobs

    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not config.evaluation_enabled:
        print(
            "error: task evaluation is disabled (ORCH_HUB_EVAL_ENABLED); there is "
            "no evaluation queue",
            file=sys.stderr,
        )
        return 1
    tree = jobs_root(config)
    try:
        records = list_persisted_jobs(config, strict=True)
    except OSError as exc:
        # A walk that could not finish knows nothing about how many jobs are
        # waiting, so it must not answer as though it did: "nothing waiting" is
        # the one reading this command can never be allowed to invent, and an
        # unreadable store is exactly the trouble it exists to report. Named as an
        # operational failure like the two above it, on stderr, with no partial
        # JSON — the shape ``eval catalog repair`` already uses.
        #
        # `strict=True` is what makes that promise reach the whole read rather than
        # only its two ``iterdir`` levels: a store path that is not a directory, or
        # cannot be stat-ed at all, would otherwise be swallowed by the walk's
        # opening ``is_dir()`` and answer here as a zero census.
        #
        # Not the same thing as an unreadable *record*: `_load_record` turns a
        # broken ``job.json`` into its ``error`` record, which the census counts
        # and the rows show. That is a reading of the queue; this is a failure to
        # take one. An absent tree stays the empty queue it honestly is.
        print(
            f"error: cannot read the evaluation queue under {tree}: {exc}",
            file=sys.stderr,
        )
        return 1
    snapshot = queue_snapshot(records)
    if args.json:
        # The HTTP payload minus its ``enabled`` envelope field, which this command
        # cannot emit a value for other than true (a disabled subsystem exited
        # above). Same model, so one parser reads both.
        print(json.dumps(snapshot.to_dict(), indent=2))
        return 0
    _print_queue(snapshot, tree)
    return 0


def _print_queue(snapshot: EvaluationQueueSnapshot, jobs_dir: Path) -> None:
    """Human form: where it was read, the census, the rows.

    No switch lines above the census any more. Two used to be printed
    unconditionally — the completion trigger's gate and the operator pause —
    because a queue that moved by itself could be still for reasons no record
    explained, and a reader scanning for why nothing moves had to find both stated
    rather than inferred from an absent line. Nothing moves by itself now, so
    silence needs no explaining: every record here says what it is. The census is
    complete — every status, zeros included — since a terminal has room for it and
    a stable line is greppable.
    """
    from .semantic_jobs import STATUSES

    print(f"queue: {jobs_dir}")
    counts = dict(snapshot.counts)
    # Known statuses in lifecycle order, then anything a hand-edited record put
    # there — counted rather than dropped, on the census's own rule.
    ordered = [(status, counts.pop(status, 0)) for status in STATUSES]
    ordered += sorted(counts.items())
    print("attempts: " + ", ".join(f"{status} {count}" for status, count in ordered))
    print(
        "  one record per repo/task holding that task's latest attempt; "
        "not a count of tasks carrying a report"
    )
    if not snapshot.entries:
        print("jobs: nothing waiting")
        return
    print("jobs:")
    for entry in snapshot.entries:
        print(f"  {_queue_row(entry)}")
    if snapshot.truncated:
        print(
            f"  showing the first {len(snapshot.entries)}; "
            "the counts above are complete"
        )


def _queue_row(entry: EvaluationQueueEntry) -> str:
    """One job: what state, which task, how long, and — when legacy or failed — why.

    A failed record's diagnostic is bounded at 2000 characters and may be several
    lines, which is a paragraph in the middle of a table; it is collapsed to one
    capped line here and left whole in ``--json``.
    """
    from .semantic_jobs import STATUS_DEFERRED, STATUS_QUEUED

    job = entry.job
    target = "/".join(part for part in (job.repo_name, job.task_id) if part)
    parts = [f"{job.status:9}", target or job.job_id or "?"]
    if entry.age_seconds is not None:
        # Always measured from ``created_ts`` — how long this attempt has been
        # open — so it is a wait only while something is going to pick the job up.
        # ``deferred`` is deliberately not in that set even though it reads like a
        # queue position: it is a legacy acceptance no code path promotes, so
        # "waiting 26d" would promise a turn that is never coming. A settled
        # record's figure is the same interval and not a wait either.
        waiting = job.status == STATUS_QUEUED
        parts.append(
            ("waiting " if waiting else "age ") + _fmt_age(entry.age_seconds)
        )
    if job.status == STATUS_DEFERRED:
        # The status token alone reads as a queue position, so the row has to say
        # what the record actually is: an acceptance the retired automatic
        # dispatcher wrote and never got to. The recorded reason is kept because it
        # says why it was held then, but it is framed as history — nothing is
        # holding it now, and the only thing that moves it is an explicit
        # `POST .../semantic/job` for that task.
        reason = f", reason={job.defer_reason}" if job.defer_reason else ""
        parts.append(f"legacy hold{reason}; never starts on its own")
    elif job.error:
        parts.append(_one_line(job.error))
    return "  ".join(parts)


def _one_line(text: str, limit: int = 100) -> str:
    """Collapse whitespace and cap, so one record cannot own the whole view."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "\u2026"


def _fmt_age(seconds: int) -> str:
    """Seconds as an operator reads them, matching the dashboard's ``fmtDuration``.

    Same rule deliberately: the two surfaces report the same jobs against the same
    bound, and a queue that reads "7h 12m" in one place and "432m" in the other
    invites the reader to wonder whether they are the same figure.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, rem = divmod(minutes, 60)
    return f"{hours}h {rem}m" if rem else f"{hours}h"


def _open_catalog() -> "ReportCatalog | None":
    """Resolve an enabled report catalog, or print why there is none.

    Shared by both catalog verbs so a malformed service env and a disabled
    subsystem read the same either way. ``report_catalog`` is imported here rather
    than at module scope for the reason ``eval doctor`` imports its backend
    locally: it pulls in the whole evaluation/semantic chain, which `repos` and the
    launchd verbs have no use for.
    """
    from .report_catalog import ReportCatalog

    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    if not config.evaluation_enabled:
        print(
            "error: task evaluation is disabled (ORCH_HUB_EVAL_ENABLED); the "
            "report catalog is not served",
            file=sys.stderr,
        )
        return None
    return ReportCatalog(config)


def _cmd_eval_catalog_verify(args: argparse.Namespace) -> int:
    """Report the global report catalog's integrity without changing anything.

    A diagnosis, deliberately not an action: it publishes nothing, evaluates
    nothing, makes no model call, and writes no byte, so it is safe to run against
    a live hub — including while a reconciliation sweep is appending.

    Exit 0 means the feed can be traversed. Non-zero means it cannot: a repeated
    ``seq``/``report_key`` or an unreadable catalog, which is exactly the state
    that makes the feed routes answer 500 and pauses publication. Damaged or
    pruned artifacts are reported but do not change the exit code — they leave the
    traversal whole and each answers for itself at fetch time, and folding them
    into the same signal would make a health check unable to tell "the feed is
    down" from "an operator has a symlink to clear".
    """
    catalog = _open_catalog()
    if catalog is None:
        return 1
    audit = catalog.audit()
    if args.json:
        print(json.dumps(audit.to_dict(), indent=2))
    else:
        _print_audit(audit)
    return 0 if audit.serves_feed else 1


def _cmd_eval_catalog_repair(args: argparse.Namespace) -> int:
    """Apply, or merely plan, the catalog's identity-preserving repair.

    Without ``--confirm`` this only describes what it would drop: the file is
    durable publication state, so an operator sees the plan before it runs. Every
    planned drop leaves each published ``seq``/``report_key`` exactly where it was
    (a byte-identical repeat, or an interrupted append's trailing bytes); a
    conflict between two *distinct* entries is refused instead, with nothing
    written, because resolving it means moving an identity a consumer may already
    have imported.

    Exit code follows the same rule as ``verify``: 0 when the catalog on disk now
    serves the feed. A refusal, or a plan that has not been confirmed yet, is
    non-zero.

    A catalog that cannot be read or backed up at all is reported the way every
    other operational failure in this CLI is — one ``error:`` line and a non-zero
    exit. ``verify`` reaches that state as a finding, since an unreadable file is
    something a diagnosis can still describe; a repair has nothing to say about a
    file it never read, and a traceback is not an operator diagnostic.
    """
    from .report_catalog import ReportCatalogError

    catalog = _open_catalog()
    if catalog is None:
        return 1
    try:
        result = catalog.repair(apply=args.confirm)
    except ReportCatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.serves_feed else 1
    print(f"catalog: {result.catalog_path}")
    if result.refused:
        # Keep the two streams in the order they were written: the refusal is the
        # answer to the line above it, and a buffered stdout would otherwise print
        # it after.
        sys.stdout.flush()
        print(f"error: repair refused: {result.refused}", file=sys.stderr)
        print("nothing was changed; resolve the conflict by hand, then re-verify")
        return 1
    if not result.actions:
        print("nothing to repair")
        return 0 if result.serves_feed else 1
    print("plan:")
    for action in result.actions:
        print(f"  drop line {action.line}: {action.detail}")
    if not result.applied:
        print("dry run: nothing was changed; re-run with --confirm to apply")
        return 0 if result.serves_feed else 1
    print(f"repaired: dropped {len(result.actions)} line(s)")
    print(f"backup: {result.backup_path}")
    print(f"serves feed: {'yes' if result.serves_feed else 'no'}")
    return 0 if result.serves_feed else 1


def _print_audit(audit: "CatalogAudit") -> None:
    """Human form of a catalog audit: the census first, then every located defect.

    Each defect line leads with where it is — the catalog line number, and the
    ``seq``/``report_key`` when the finding is about an entry rather than about
    bytes that would not parse — because that prefix is what an operator edits
    against. Findings repaired in the evaluations tree rather than in the catalog
    file (a damaged, incomplete, or unreadable artifact set) carry the directory on
    a second line, so clearing one is not a path-reconstruction exercise. The
    machine ``kind`` stays in the ``--json`` form.
    """
    print(f"catalog: {audit.catalog_path}")
    print(f"present: {'yes' if audit.present else 'no'}")
    print(f"lines: {audit.lines}")
    print(f"entries: {audit.entries}")
    print(f"serves feed: {'yes' if audit.serves_feed else 'no'}")
    print(
        "artifacts: "
        f"{audit.retained} retained, {audit.pruned} pruned, "
        f"{audit.incomplete} incomplete, {audit.damaged} damaged, "
        f"{audit.unreadable} unreadable, {audit.unaddressable} unaddressable"
    )
    if not audit.defects:
        print("defects: none")
        return
    print("defects:")
    for defect in audit.defects:
        where = []
        if defect.line is not None:
            where.append(f"line {defect.line}")
        if defect.seq is not None:
            where.append(f"seq {defect.seq}")
        if defect.report_key:
            where.append(defect.report_key)
        print(f"  {' '.join(where) or 'catalog'}: {defect.detail}")
        if defect.path:
            print(f"    at: {defect.path}")


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
