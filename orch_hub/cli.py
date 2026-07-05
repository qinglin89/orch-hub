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

from .config import Config
from .models import ManagedRepo, ReadinessReport, repos_payload
from .readiness import check_readiness
from .registry import RegistryError, discover_repos


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "repos":
        return _cmd_repos(json_output=args.json)
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
    return parser


def _cmd_repos(*, json_output: bool) -> int:
    config = Config.from_env()
    try:
        repos = discover_repos(config)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    pairs = [(repo, check_readiness(repo, config)) for repo in repos]
    if json_output:
        print(json.dumps(repos_payload(pairs), indent=2))
    else:
        _print_human(pairs)
    return 0


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
