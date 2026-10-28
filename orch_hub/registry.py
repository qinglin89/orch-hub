"""Discover managed repos from mandrel.

Preferred source is ``mandrel registry list --json``; the fallback is
``<mandrel>/.registry/repos.local.json``. Both return the same shape: a JSON
array of ``{"name", "path", "manifest"}`` objects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config
from .models import ManagedRepo
from .subprocess_utils import CommandNotFoundError, Runner, run_command


class RegistryError(Exception):
    """Raised when registry data exists but cannot be parsed or is malformed."""


def discover_repos(
    config: Config, *, runner: Runner = run_command
) -> list[ManagedRepo]:
    """Return managed repos, preferring ``mandrel`` and falling back to the file.

    Order:
      1. ``mandrel registry list --json`` — used when it runs and exits 0.
      2. ``<mandrel>/.registry/repos.local.json`` — used when the command is
         unavailable (missing executable, or one the OS refuses to run, such as
         a non-executable ``bin/mandrel``) or exits non-zero.

    A missing registry file yields an empty list. Malformed JSON, or an entry
    missing a required key, raises :class:`RegistryError`. An exit-0 command is
    trusted: unparseable output from it is an explicit error, not a silent
    fall-through to the file.
    """
    raw = _load_from_command(config, runner)
    source = "mandrel registry list --json"
    if raw is None:
        raw = _load_from_file(config)
        source = str(config.registry_file)
    return _parse_entries(raw, source)


def _load_from_command(config: Config, runner: Runner) -> Any | None:
    """Run the mandrel registry command; return parsed JSON, or None to fall back."""
    try:
        result = runner([config.mandrel_bin, "registry", "list", "--json"])
    except CommandNotFoundError:
        return None
    except OSError:
        # Present but unrunnable (e.g. PermissionError on a non-executable
        # bin/mandrel): same fallback as a missing command, so discovery — and
        # GET /api/repos — degrades to the registry file instead of erroring.
        return None
    if not result.ok:
        return None
    return _decode_json(result.stdout, "mandrel registry list --json")


def _load_from_file(config: Config) -> Any:
    """Read the fallback registry file; a missing file means no managed repos."""
    path = config.registry_file
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"cannot read registry file {path}: {exc}") from exc
    return _decode_json(text, str(path))


def _decode_json(text: str, source: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"invalid registry JSON from {source}: {exc}") from exc


def _parse_entries(raw: Any, source: str) -> list[ManagedRepo]:
    if not isinstance(raw, list):
        raise RegistryError(f"registry data from {source} is not a JSON array")
    return [_parse_entry(entry, source, index) for index, entry in enumerate(raw)]


def _parse_entry(entry: Any, source: str, index: int) -> ManagedRepo:
    if not isinstance(entry, dict):
        raise RegistryError(
            f"registry entry {index} from {source} is not a JSON object"
        )
    try:
        name = entry["name"]
        path = entry["path"]
        manifest = entry["manifest"]
    except KeyError as exc:
        raise RegistryError(
            f"registry entry {index} from {source} missing key {exc}"
        ) from exc
    return ManagedRepo(name=str(name), path=Path(path), manifest=Path(manifest))
