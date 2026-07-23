"""Orchestrator launch configuration: query, argument construction, snapshot.

The deployed orchestrator owns all model/effort/profile policy. orch-hub never
reads ``orchestrator.toml`` or hardcodes profile model ids — it *asks* the repo's
own orchestrator through ``--print-config`` and constructs launch arguments from a
UI mode selection. Three responsibilities live here, all read-only w.r.t. the
target repo:

- :func:`query_effective_config` runs ``<python> orchestrator.py --print-config
  [--backend B] [--profile P]`` with ``cwd=<repo>`` and parses one JSON object
  (:func:`parse_effective_config`, strict/fail-closed). A missing flag (an older
  deployment predating ``--print-config``), a nonzero exit, malformed output, or
  an unsupported schema all raise :class:`OrchestratorConfigError` — the caller
  disables launch and surfaces the orchestrator error rather than substituting
  hub-owned defaults.
- :func:`build_launch_args` maps a :class:`~orch_hub.models.LaunchSpec` (the
  four dashboard modes) to the exact orchestrator flags, so command construction
  is one tested function instead of logic duplicated in the API and the browser.
- The ``effective-config:`` snapshot helpers read/persist the authoritative launch
  configuration every real run logs at startup, so run history retains what
  actually launched (it is authoritative if it differs from the preview).

This module deliberately does **not** import :mod:`orch_hub.runs` (which imports
it): the two deployed-orchestrator path constants are mirrored locally, matching
:mod:`orch_hub.readiness` which inlines the same paths.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from . import logs
from .models import EffectiveConfig, LaunchSpec, ManagedRepo, RoleConfig, RunRecord
from .subprocess_utils import CommandNotFoundError, Runner, run_command

# Deployed-orchestrator paths relative to a target repo. Mirrored from
# :mod:`orch_hub.runs` (which imports this module, so importing back would cycle);
# :mod:`orch_hub.readiness` inlines the same fixed deployment paths.
ORCHESTRATOR_SCRIPT = Path(".cursor") / "orchestrator" / "orchestrator.py"
ORCHESTRATOR_PYTHON = Path(".cursor") / "orchestrator" / ".venv" / "bin" / "python"

PRINT_CONFIG_FLAG = "--print-config"

SUPPORTED_SCHEMA_VERSION = 1
"""The ``--print-config`` schema this hub understands. A different value fails
closed (redeploy the orchestrator payload) rather than being parsed on guesswork."""

DEFAULT_QUERY_TIMEOUT = 30.0
"""Bound the config query so a hung orchestrator cannot stall the HTTP request."""

BACKENDS = ("cursor", "cc-codex")
"""The orchestrator's two backends (``--backend`` choices)."""

DEV_AGENTS = ("claude", "codex")
"""cc-codex dev-role CLI agents (``--dev-agent`` choices); never valid for Cursor."""

MODE_DEFAULT = "default"
MODE_STANDARD = "standard"
MODE_EXCELLENT = "excellent"
MODE_CUSTOM = "custom"
LAUNCH_MODES = (MODE_DEFAULT, MODE_STANDARD, MODE_EXCELLENT, MODE_CUSTOM)
"""The four dashboard launch modes. ``standard``/``excellent`` double as the
``--profile`` value; ``default``/``custom`` are UI-only and never passed as a
profile."""

EFFECTIVE_CONFIG_NAME = "effective_config.json"
"""Hub-owned run-dir file holding the parsed ``effective-config:`` snapshot,
written once at the live->ended transition beside ``run.json``/``sessions.json``."""

_HEAD_SCAN_LINES = 200
"""The ``effective-config:`` line is the first thing a run logs, so a bounded head
scan of the located log / ``spawn.out`` finds it without reading the whole file."""

# ``[HH:MM:SS] effective-config: {compact json}`` — the orchestrator logs this
# through ``orch.log`` (which timestamps every line and writes to both the run log
# and stdout, so it also lands in the hub's spawn.out capture). The JSON is a
# single compact line, so a greedy ``{.*}`` to end-of-line is exact.
_EFFECTIVE_CONFIG_LINE = re.compile(
    r"^(?:\[\d{2}:\d{2}:\d{2}\]\s*)?effective-config:\s*(\{.*\})\s*$"
)


class OrchestratorConfigError(Exception):
    """A ``--print-config`` query failed (missing flag, nonzero, malformed, or
    unsupported schema). Fail-closed: the caller disables launch and surfaces this
    rather than substituting hub-owned defaults."""


class LaunchError(ValueError):
    """A :class:`~orch_hub.models.LaunchSpec` could not be turned into valid
    orchestrator arguments (missing required field, or a flag illegal for the
    selected backend/mode). A ``ValueError`` so the API maps it to 400."""


# -- effective-config query ---------------------------------------------------


def query_effective_config(
    repo: ManagedRepo,
    *,
    backend: str | None = None,
    profile: str | None = None,
    runner: Runner = run_command,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
) -> EffectiveConfig:
    """Query the repo's deployed orchestrator for its effective launch config.

    Runs ``<repo>/.cursor/orchestrator/.venv/bin/python orchestrator.py
    --print-config [--backend B] [--profile P]`` with ``cwd=<repo>`` — the same
    resolution a real run uses, but no task id, credentials, clean tree, or
    model-catalog call. Returns the parsed :class:`EffectiveConfig`; a missing
    executable, timeout, nonzero exit (an older deployment without the flag, or an
    unknown ``--profile``), non-JSON output, or an unsupported schema all raise
    :class:`OrchestratorConfigError` with the orchestrator's own reason.
    """
    argv = [
        repo.path / ORCHESTRATOR_PYTHON,
        repo.path / ORCHESTRATOR_SCRIPT,
        PRINT_CONFIG_FLAG,
    ]
    if backend is not None:
        argv += ["--backend", backend]
    if profile is not None:
        argv += ["--profile", profile]
    try:
        result = runner(argv, cwd=repo.path, timeout=timeout)
    except CommandNotFoundError as exc:
        raise OrchestratorConfigError(
            f"orchestrator Python not found ({exc}); redeploy the orchestrator payload"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise OrchestratorConfigError(
            f"orchestrator --print-config timed out after {timeout:g}s"
        ) from exc
    if not result.ok:
        detail = _first_nonempty(result.stderr, result.stdout) or f"exit {result.returncode}"
        raise OrchestratorConfigError(f"orchestrator --print-config failed: {detail}")
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise OrchestratorConfigError(
            f"orchestrator --print-config output was not valid JSON: {exc}"
        ) from exc
    return parse_effective_config(data)


def parse_effective_config(data: object) -> EffectiveConfig:
    """Strictly parse a ``--print-config`` JSON object into an :class:`EffectiveConfig`.

    Fail-closed on every launch-relevant field the orchestrator always emits in its
    ``launch_config_dict``: a non-object, a ``schema_version`` other than
    :data:`SUPPORTED_SCHEMA_VERSION`, a missing/blank ``backend``, a missing
    ``dev``/``review`` role, or any required field of the wrong type or empty —
    ``config_revision``/``effective_revision``/``profile`` (non-empty strings),
    ``available_profiles`` (string list), role ``model``/``effort`` (non-empty
    strings), ``max_sessions``/``context_budget`` (ints), ``sources`` (string map),
    and ``once``/``plan_gate`` (real booleans) — all raise
    :class:`OrchestratorConfigError`. Only the fields the orchestrator itself leaves
    null stay optional: a role ``agent`` (``None`` for a Cursor dev role),
    ``codex_sandbox`` (``None`` for the Cursor backend), and the run-launch fields
    ``task_id``/``control_dir`` (``None`` in a ``--print-config`` preview). This
    keeps a malformed-but-schema-1 payload from being served as a launch config the
    orchestrator never resolved, which would leave the Start dialog previewing
    null/default values instead of disabling launch (the acceptance requires the
    config query to fail closed on malformed responses).
    """
    if not isinstance(data, dict):
        raise OrchestratorConfigError(
            "orchestrator --print-config did not return a JSON object"
        )
    version = data.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise OrchestratorConfigError(
            f"unsupported orchestrator config schema_version {version!r}"
            f" (this hub supports {SUPPORTED_SCHEMA_VERSION}); redeploy the"
            " orchestrator payload"
        )
    return EffectiveConfig(
        schema_version=version,
        config_revision=_req_str(data, "config_revision"),
        effective_revision=_req_str(data, "effective_revision"),
        profile=_req_str(data, "profile"),
        available_profiles=_req_str_tuple(data, "available_profiles"),
        backend=_req_str(data, "backend"),
        dev=_role(data.get("dev"), "dev"),
        review=_role(data.get("review"), "review"),
        max_sessions=_req_int(data, "max_sessions"),
        context_budget=_req_int(data, "context_budget"),
        codex_sandbox=_opt_str(data.get("codex_sandbox")),
        sources=_req_str_map(data, "sources"),
        task_id=_opt_str(data.get("task_id")),
        once=_req_bool(data, "once"),
        plan_gate=_req_bool(data, "plan_gate"),
        control_dir=_opt_str(data.get("control_dir")),
    )


def _role(value: object, name: str) -> RoleConfig:
    if not isinstance(value, dict):
        raise OrchestratorConfigError(
            f"orchestrator --print-config output is missing the '{name}' role config"
        )
    return RoleConfig(
        agent=_opt_str(value.get("agent")),
        model=_req_str(value, "model", label=f"{name}.model"),
        effort=_req_str(value, "effort", label=f"{name}.effort"),
    )


def _first_nonempty(*texts: str) -> str:
    for text in texts:
        for line in (text or "").splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
    return ""


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


# The orchestrator's ``launch_config_dict`` always emits these required fields with
# a fixed type; a missing or wrong-typed one is a malformed response, so each
# extractor fails closed rather than substituting a hub-owned default.


def _req_str(data: dict, key: str, *, label: str | None = None) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise OrchestratorConfigError(
            f"orchestrator --print-config field {label or key!r} must be a"
            " non-empty string"
        )
    return value


def _req_int(data: dict, key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise OrchestratorConfigError(
            f"orchestrator --print-config field {key!r} must be an integer"
        )
    return value


def _req_bool(data: dict, key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise OrchestratorConfigError(
            f"orchestrator --print-config field {key!r} must be a boolean"
        )
    return value


def _req_str_tuple(data: dict, key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise OrchestratorConfigError(
            f"orchestrator --print-config field {key!r} must be a list of strings"
        )
    return tuple(value)


def _req_str_map(data: dict, key: str) -> dict[str, str]:
    value = data.get(key)
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise OrchestratorConfigError(
            f"orchestrator --print-config field {key!r} must be a map of strings"
        )
    return dict(value)


# -- launch argument construction ---------------------------------------------


def build_launch_args(spec: LaunchSpec) -> tuple[str, ...]:
    """Construct the orchestrator flags for a dashboard launch-mode selection.

    Returns only the mode-derived flags (the caller appends them after
    ``<task-id> --control-dir <dir>``), so the run manager's base command is
    unchanged. Per the deployed contract:

    - ``default`` — inherit the orchestrator's configuration; pass no
      profile/model/effort flags. Append ``--backend`` only when a backend was
      explicitly selected (``spec.backend`` set); a fully inherited backend
      (``None``) is omitted too.
    - ``standard``/``excellent`` — require an explicit backend and pass only
      ``--backend <b> --profile <mode>``; the resolved role flags are never
      repeated.
    - ``custom`` — require an explicit backend and complete role values; pass
      explicit ``--dev-model``/``--dev-effort``/``--review-model``/
      ``--review-effort``. ``--dev-agent`` is cc-codex-only and never sent to
      Cursor.

    The orthogonal launch flags ``--plan-gate``/``--once``/``--max-sessions`` are
    appended in every mode from their own toggles, independent of the profile /
    model selection. Raises :class:`LaunchError` on an unknown mode, a missing
    required field, or a backend-illegal flag.
    """
    mode = spec.mode
    if mode not in LAUNCH_MODES:
        raise LaunchError(f"unknown launch mode {mode!r}")
    backend = _clean(spec.backend)
    if backend is not None and backend not in BACKENDS:
        raise LaunchError(
            f"unknown backend {backend!r} (expected one of {', '.join(BACKENDS)})"
        )

    args: list[str] = []
    if mode == MODE_DEFAULT:
        # Inherit everything. Append --backend only when explicitly selected.
        if backend is not None:
            args += ["--backend", backend]
    elif mode in (MODE_STANDARD, MODE_EXCELLENT):
        if backend is None:
            raise LaunchError(f"{mode} launch requires an explicit backend")
        args += ["--backend", backend, "--profile", mode]
    else:  # MODE_CUSTOM
        if backend is None:
            raise LaunchError("custom launch requires an explicit backend")
        args += ["--backend", backend]
        dev_agent = _clean(spec.dev_agent)
        if dev_agent is not None:
            if dev_agent not in DEV_AGENTS:
                raise LaunchError(
                    f"unknown dev agent {dev_agent!r}"
                    f" (expected one of {', '.join(DEV_AGENTS)})"
                )
            if backend != "cc-codex":
                raise LaunchError(
                    "--dev-agent applies only to the cc-codex backend"
                )
            args += ["--dev-agent", dev_agent]
        args += [
            "--dev-model", _require(spec.dev_model, "dev model"),
            "--dev-effort", _require(spec.dev_effort, "dev effort"),
            "--review-model", _require(spec.review_model, "review model"),
            "--review-effort", _require(spec.review_effort, "review effort"),
        ]

    # Orthogonal launch flags, honored in every mode from their own toggles.
    if spec.plan_gate:
        args.append("--plan-gate")
    if spec.once:
        args.append("--once")
    if spec.max_sessions is not None:
        if spec.max_sessions < 1:
            raise LaunchError("max sessions must be a positive integer")
        args += ["--max-sessions", str(spec.max_sessions)]
    return tuple(args)


def _clean(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _require(value: str | None, label: str) -> str:
    cleaned = _clean(value)
    if cleaned is None:
        raise LaunchError(f"custom launch requires a {label}")
    return cleaned


# -- effective-config run snapshot --------------------------------------------


def parse_effective_config_line(lines: Iterable[str]) -> dict | None:
    """Return the parsed ``effective-config:`` JSON from a run's log lines, or None.

    Scans for the first ``[HH:MM:SS] effective-config: {json}`` line (the
    timestamp prefix is optional) and returns its object. Tolerant: a malformed or
    non-object payload is skipped, an absent line yields ``None`` — a missing
    snapshot is a graceful degradation, never an error.
    """
    for line in lines:
        match = _EFFECTIVE_CONFIG_LINE.match(line.rstrip("\n"))
        if match is None:
            continue
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def read_effective_config_log(record: RunRecord) -> dict | None:
    """Read a run's ``effective-config:`` snapshot from its log, else ``spawn.out``.

    The line is logged at startup to both the orchestrator log and stdout, so it is
    near the top of the located log and of the hub's ``spawn.out`` capture. Prefers
    the canonical run log (located from the record alone, never a client path) and
    falls back to ``spawn.out`` for a run that failed before opening its log.
    Bounded head scan; any IO failure degrades to ``None``.
    """
    log_path = logs.locate_log(record)
    if log_path is not None:
        found = _scan_head(log_path)
        if found is not None:
            return found
    if record.spawn_out is not None:
        return _scan_head(record.spawn_out)
    return None


def _scan_head(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return parse_effective_config_line(itertools.islice(handle, _HEAD_SCAN_LINES))
    except OSError:
        return None


def effective_config_snapshot_path(run_dir: Path) -> Path:
    return run_dir / EFFECTIVE_CONFIG_NAME


def write_effective_config_snapshot(run_dir: Path, data: dict) -> None:
    """Persist the parsed ``effective-config:`` snapshot atomically (tmp + rename)."""
    path = effective_config_snapshot_path(run_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_effective_config_snapshot(run_dir: Path) -> dict | None:
    """Load the persisted ``effective_config.json`` snapshot; ``None`` if absent,
    unreadable, or not a JSON object (tolerant, like the sessions snapshot)."""
    path = effective_config_snapshot_path(run_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
