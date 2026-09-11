"""Orchestrator launch configuration: query, argument construction, snapshot.

The deployed orchestrator owns all model/effort/profile policy. orch-hub never
reads ``orchestrator.toml`` or hardcodes profile model ids — it *asks* the repo's
own orchestrator through ``--print-config`` and constructs launch arguments from a
UI mode selection. Three responsibilities live here, all read-only w.r.t. the
target repo:

- :func:`query_effective_config` runs ``<python> orchestrator.py --print-config
  [--backend B] [--profile P] [--dev-profile P] [--review-profile P]`` with
  ``cwd=<repo>`` and parses one JSON object (:func:`parse_effective_config`,
  strict/fail-closed). A missing flag (an older deployment predating
  ``--print-config`` or the role-profile flags), a nonzero exit, malformed
  output, or an unsupported schema all raise :class:`OrchestratorConfigError` —
  the caller disables launch and surfaces the orchestrator error rather than
  substituting hub-owned defaults.
- :func:`build_launch_args` maps a :class:`~orch_hub.models.LaunchSpec` (an
  independent per-role selection, or the legacy run-wide one) to the exact
  orchestrator flags, so command construction is one tested function instead of
  logic duplicated in the API and the browser.
- The ``effective-config:`` snapshot helpers read/persist the authoritative launch
  configuration every real run logs at startup, so run history retains what
  actually launched (it is authoritative if it differs from the preview). They
  drop exactly one field on the way through — the ``options`` catalog, which says
  what any launch MAY select and so records nothing about the run it would be
  stored under (:func:`strip_option_catalog`).

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
ORCHESTRATOR_SCRIPT = Path(".mandrel") / "orchestrator" / "orchestrator.py"
ORCHESTRATOR_PYTHON = Path(".mandrel") / "orchestrator" / ".venv" / "bin" / "python"

PRINT_CONFIG_FLAG = "--print-config"

REVIEW_ENABLED_KEY = "review_enabled"
"""The schema-4 launch field carrying the resolved review on/off switch.

A run configuration axis the orchestrator resolves (mandrel
``2026-08-17-optional-review-session``) and publishes beside the other launch
selections. Deliberately absent from the ``options`` catalog: that catalog
carries the flags with a *vocabulary* to offer, and a boolean has none.

orch-hub reads it and never writes it. The hub sends no review-on/off flag, and
the switch gates nothing here — not readiness, not launch, not intake, not
evaluation. This hub accepts the configuration; it does not choose it."""

SUPPORTED_SCHEMA_VERSIONS = (3, 4)
"""The ``--print-config`` schemas this hub understands. Any other value fails
closed rather than being parsed on guesswork.

Normally exactly one version, not a range. A range only buys something when the
other side is out of reach, and it is not: every managed repository is deployed
from one canonical checkout, so the remedy for a mismatch is always to bring the
lagging side forward. Pinning keeps a stale target loud instead of quietly
half-understood.

**Two versions here is a redeploy-wave exception, and it is temporary.** That
rationale assumes one target at a time; this hub consumes *many* at once, and a
canonical schema bump reaches them one redeploy at a time. During the wave a
single pin is wrong in whichever position it takes: pinned at 4 every target not
yet redeployed stops launching, pinned at 3 every target already redeployed
stops launching. The wave also cannot be ordered away — mandrel cannot redeploy
first, because a target on 4 cannot launch until this hub accepts 4. So both
versions are accepted for the duration, and the pin returns to one value
afterwards.

3 is the transitional half: drop it once every managed repo reports 4 (mandrel
``2026-08-17-optional-review-session`` session-2 is the redeploy that makes that
true). Nothing below 3 was ever accepted, so dropping it costs no compatibility
the hub had — only the ability to tell a lagging target apart from an unknown
one, which the direction-aware remedy below already handles.

Which side lags decides the remedy, and the two are not the same action. A target
below this range needs its payload redeployed. A target *above* it means the
canonical suite moved and this hub has not caught up yet — redeploying then pushes
the target further away, so the fix is here, not there."""

DEFAULT_QUERY_TIMEOUT = 30.0
"""Bound the config query so a hung orchestrator cannot stall the HTTP request."""

BACKENDS = ("cursor", "cc-codex")
"""The orchestrator's two backends (``--backend`` choices)."""

CLI_AGENTS = ("claude", "codex")
"""cc-codex CLI agents, offered to both roles (``--dev-agent``/``--review-agent``
choices); never valid for Cursor, which runs both roles on the one SDK."""

MODE_DEFAULT = "default"
MODE_STANDARD = "standard"
MODE_EXCELLENT = "excellent"
MODE_CUSTOM = "custom"
LAUNCH_MODES = (MODE_DEFAULT, MODE_STANDARD, MODE_EXCELLENT, MODE_CUSTOM)
"""The four dashboard launch modes, offered per role (DEV/REVIEW) and by the
legacy run-wide selector. ``standard``/``excellent`` double as the profile value
(``--dev-profile``/``--review-profile``, or legacy ``--profile``);
``default``/``custom`` are UI-only and never passed as a profile — an explicit
``--dev-profile default`` is only needed to *clear* a run-wide ``--profile``,
which the split path never sends."""

NAMED_PROFILE_MODES = (MODE_STANDARD, MODE_EXCELLENT)
"""The launch modes that name an orchestrator profile."""

EFFECTIVE_CONFIG_NAME = "effective_config.json"
"""Hub-owned run-dir file holding the parsed ``effective-config:`` snapshot,
written once at the live->ended transition beside ``run.json``/``sessions.json``.
Holds the snapshot view of the line, not the line: see
:func:`strip_option_catalog`."""

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
    dev_profile: str | None = None,
    review_profile: str | None = None,
    runner: Runner = run_command,
    timeout: float = DEFAULT_QUERY_TIMEOUT,
) -> EffectiveConfig:
    """Query the repo's deployed orchestrator for its effective launch config.

    Runs ``<repo>/.mandrel/orchestrator/.venv/bin/python orchestrator.py
    --print-config [--backend B] [--profile P] [--dev-profile P]
    [--review-profile P]`` with ``cwd=<repo>`` — the same resolution a real run
    uses, but no task id, credentials, clean tree, or model-catalog call. Both
    role selections are asked for in this one subprocess, so the preview a caller
    paints is internally coherent (a role queried on its own would resolve
    against the *other* role's inherited values).

    Returns the parsed :class:`EffectiveConfig`; a missing executable, timeout,
    nonzero exit (an unknown profile, or a deployment predating ``--print-config``
    or the role-profile flags), non-JSON output, or an unsupported schema all
    raise :class:`OrchestratorConfigError` with the orchestrator's own reason.
    Passing the role flags is therefore also a capability probe that *fails* on an
    old deployment: a caller that must stay compatible queries without them first
    and reads :attr:`EffectiveConfig.split_profiles`.
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
    if dev_profile is not None:
        argv += ["--dev-profile", dev_profile]
    if review_profile is not None:
        argv += ["--review-profile", review_profile]
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
    ``launch_config_dict``: a non-object, a ``schema_version`` outside
    :data:`SUPPORTED_SCHEMA_VERSIONS`, a missing/blank ``backend``, a missing
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

    ``options`` is the one field parsed *tolerantly* rather than fail-closed, and
    for a reason the strict fields do not share: it is advisory. Every strict
    field above is a value some launch resolves to, so a wrong one launches the
    wrong run; the catalog only says what a caller MAY offer, and the
    orchestrator still launches an uncatalogued model. Failing the whole query
    on a malformed catalog would therefore disable launching over a control that
    is allowed to be absent. Absent, unusable, or unparseable is ``None`` —
    "offer no list", never "offer the hub's list" (:func:`parse_option_catalog`).

    The ``profiles`` object is *additive*: absent means an older deployment that
    predates split role profiles, and both role selections are derived from the
    run-wide ``profile`` with ``split_profiles=False`` so legacy configs and old
    run snapshots stay readable. Present but not an object of two non-empty
    strings is malformed and fails closed like every other field — a deployment
    that emits ``profiles`` at all emits both roles.

    ``review_enabled`` is additive the same way and for the same reason
    (:func:`_review_enabled`): absent is the schema-3 deployment that has no
    review switch, and resolves to ``True``. Reading it as strictly required on
    schema 4 would turn a field-name drift into "no redeployed target can
    launch", which is precisely the outcome the two-version pin above exists to
    avoid; the cost of the tolerant reading is one mislabelled display row until
    the field name is corrected here.
    """
    if not isinstance(data, dict):
        raise OrchestratorConfigError(
            "orchestrator --print-config did not return a JSON object"
        )
    version = data.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        # Name the remedy for the direction the mismatch actually runs in.
        # Telling an operator to redeploy a target that is already ahead sends
        # them further from a working pair.
        if isinstance(version, int) and version > max(SUPPORTED_SCHEMA_VERSIONS):
            remedy = "update orch-hub; the canonical suite has moved ahead of it"
        else:
            remedy = "redeploy the orchestrator payload to this target"
        supported = ", ".join(str(v) for v in SUPPORTED_SCHEMA_VERSIONS)
        raise OrchestratorConfigError(
            f"unsupported orchestrator config schema_version {version!r}"
            f" (this hub supports {supported}); {remedy}"
        )
    profile = _req_str(data, "profile")
    dev_profile, review_profile, split_profiles = _role_profiles(
        data.get("profiles"), profile
    )
    return EffectiveConfig(
        schema_version=version,
        review_enabled=_review_enabled(data.get(REVIEW_ENABLED_KEY)),
        config_revision=_req_str(data, "config_revision"),
        effective_revision=_req_str(data, "effective_revision"),
        profile=profile,
        available_profiles=_req_str_tuple(data, "available_profiles"),
        dev_profile=dev_profile,
        review_profile=review_profile,
        split_profiles=split_profiles,
        backend=_req_str(data, "backend"),
        dev=_role(data.get("dev"), "dev"),
        review=_role(data.get("review"), "review"),
        max_sessions=_req_int(data, "max_sessions"),
        context_budget=_req_int(data, "context_budget"),
        codex_sandbox=_opt_str(data.get("codex_sandbox")),
        sources=_req_str_map(data, "sources"),
        options=parse_option_catalog(data.get("options")),
        task_id=_opt_str(data.get("task_id")),
        once=_req_bool(data, "once"),
        plan_gate=_req_bool(data, "plan_gate"),
        control_dir=_opt_str(data.get("control_dir")),
    )


def _review_enabled(value: object) -> bool:
    """Resolve the schema-4 review on/off switch from the launch config.

    Additive, like ``profiles``: absent (or null) is a payload that predates the
    axis — schema 3, which has no switch and always dispatches review — so it
    resolves to ``True``, the axis's own default and the only behavior any
    earlier deployment had. Present but not a real boolean is malformed and fails
    closed like every other resolved field.

    Absence deliberately does *not* get a capability marker of the
    ``split_profiles`` kind. That marker exists because the hub SELECTS role
    profiles and must not send a flag an old deployment cannot parse; this switch
    is one the orchestrator resolves and the hub only reads (it never sends a
    review-on/off flag and never gates readiness, launch, or intake on the
    value), so "derived" and "observed" would label a distinction with no
    consequence.
    """
    if value is None:
        return True
    if not isinstance(value, bool):
        raise OrchestratorConfigError(
            f"orchestrator --print-config field {REVIEW_ENABLED_KEY!r} must be a"
            " boolean"
        )
    return value


def _role_profiles(value: object, profile: str) -> tuple[str, str, bool]:
    """Resolve ``(dev_profile, review_profile, split_profiles)`` from ``profiles``.

    Absent (or null) is the older deployment: derive both roles from the run-wide
    ``profile`` and report ``split_profiles=False``, so a caller can render the
    config without being able to mistake derivation for capability.
    """
    if value is None:
        return profile, profile, False
    if not isinstance(value, dict):
        raise OrchestratorConfigError(
            "orchestrator --print-config field 'profiles' must be an object"
        )
    return (
        _req_str(value, "dev", label="profiles.dev"),
        _req_str(value, "review", label="profiles.review"),
        True,
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


def parse_option_catalog(value: object) -> dict | None:
    """Normalize the orchestrator's ``options`` catalog, or ``None`` if it has none.

    The orchestrator publishes, selection-independently, every launch value a
    caller MAY offer, keyed so it can be filtered by backend, role, and agent
    without restating the orchestrator's own axis knowledge (effort vocabularies
    differ per agent: a value that is legal for one is a startup refusal for the
    other). This keeps the subset a caller renders::

        {"agents": {<backend>: {<role>: {<agent>: {
            "effort_axis": str|None,   # the agent's fixed axis, if it fixes one
            "efforts": [str]|None,     # that axis' values, ascending
            "models": [{"id": str, "effort_axis": str|None, "efforts": [str]|None}],
        }}}}}

    Normalization only ever *drops*: a malformed level is skipped, never
    replaced. That direction is the whole contract — a hub-invented model or
    effort list would be orch-hub asserting policy it does not own, and a wrong
    one is a startup refusal the operator cannot see coming. So a payload that
    publishes nothing usable yields ``None``, which callers must render as
    free-text entry, not as an empty or substituted list.

    Tolerant by design (see :func:`parse_effective_config`): the catalog is
    advisory, so its absence or malformation degrades one control instead of
    failing the query that gates launching.
    """
    if not isinstance(value, dict):
        return None
    agents = value.get("agents")
    if not isinstance(agents, dict):
        return None
    catalog: dict[str, dict[str, dict[str, dict]]] = {}
    for backend, roles in agents.items():
        if not isinstance(backend, str) or not isinstance(roles, dict):
            continue
        by_role: dict[str, dict[str, dict]] = {}
        for role, by_agent in roles.items():
            if not isinstance(role, str) or not isinstance(by_agent, dict):
                continue
            entries = {
                agent: _agent_options(options)
                for agent, options in by_agent.items()
                if isinstance(agent, str) and isinstance(options, dict)
            }
            if entries:
                by_role[role] = entries
        if by_role:
            catalog[backend] = by_role
    return {"agents": catalog} if catalog else None


def _agent_options(value: dict) -> dict:
    """One agent's offerable choices, keeping only well-formed entries.

    An agent whose models all drop out keeps its entry with an empty list: the
    agent still publishes its effort axis, and "this agent lists no models" is
    the same free-text degradation as an absent catalog, handled once by the
    caller rather than by a second shape here.
    """
    raw = value.get("models")
    models: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        # A repeated id would render a duplicate choice; the orchestrator rejects
        # duplicates in its own config, so this only catches a hand-edited payload.
        if not isinstance(model_id, str) or not model_id.strip() or model_id in seen:
            continue
        seen.add(model_id)
        models.append(
            {
                "id": model_id,
                "effort_axis": _opt_str(item.get("effort_axis")),
                "efforts": _effort_values(item.get("efforts")),
            }
        )
    return {
        "effort_axis": _opt_str(value.get("effort_axis")),
        "efforts": _effort_values(value.get("efforts")),
        "models": models,
    }


def _effort_values(value: object) -> list[str] | None:
    """A published effort list in the orchestrator's own (ascending) order, or
    ``None`` when nothing usable was published — an empty list would render as an
    effort control offering no choice at all."""
    if not isinstance(value, list):
        return None
    values = [item for item in value if isinstance(item, str) and item.strip()]
    return values or None


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
    """Construct the orchestrator flags for a dashboard launch selection.

    Returns only the selection-derived flags (the caller appends them after
    ``<task-id> --control-dir <dir>``), so the run manager's base command is
    unchanged. The spec carries exactly one of two mutually exclusive selection
    shapes — sending both, or neither, raises :class:`LaunchError` before any
    launch, because the two map to *different* orchestrator flags and there is no
    safe way to guess which the caller meant:

    - ``dev_mode``/``review_mode`` — the independent per-role selection
      (:func:`_split_role_args`), emitting ``--dev-profile``/``--review-profile``
      and only the selected role's explicit flags.
    - ``mode`` — the legacy run-wide selection (:func:`_legacy_mode_args`),
      emitting the ``--profile`` shorthand, kept verbatim for pre-split callers.

    Both shapes share the backend policy: ``--backend`` is appended whenever a
    backend was explicitly selected, and inheriting it (``None``) is legal only
    when nothing but ``default`` was selected — a named/custom role resolves
    against a backend, so the operator must pin the one they previewed.

    The orthogonal launch flags ``--plan-gate``/``--once``/``--max-sessions`` are
    appended in every mode from their own toggles, independent of the profile /
    model selection. Raises :class:`LaunchError` on an unknown mode, a missing
    required field, or a backend-illegal flag.
    """
    split = spec.dev_mode is not None or spec.review_mode is not None
    if spec.mode is not None and split:
        raise LaunchError(
            "send either the legacy 'mode' or 'dev_mode'/'review_mode', not both"
        )
    backend = _clean(spec.backend)
    if backend is not None and backend not in BACKENDS:
        raise LaunchError(
            f"unknown backend {backend!r} (expected one of {', '.join(BACKENDS)})"
        )

    if split:
        args = _split_role_args(spec, backend)
    elif spec.mode is not None:
        args = _legacy_mode_args(spec, backend)
    else:
        raise LaunchError(
            "launch selection requires 'dev_mode'/'review_mode' or legacy 'mode'"
        )

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


def _split_role_args(spec: LaunchSpec, backend: str | None) -> list[str]:
    """Flags for an independent DEV/REVIEW selection (the split Start dialog).

    Each role contributes only its own flags, so every combination composes:
    ``default`` emits nothing (the orchestrator inherits — and since the split
    path never sends the run-wide ``--profile``, there is nothing to clear with
    an explicit ``--dev-profile default``), ``standard``/``excellent`` emit that
    role's profile flag, and ``custom`` emits that role's explicit values.

    A custom cc-codex role must carry its complete ``agent``/``model``/``effort``
    tuple — both roles, symmetrically, since each names its own CLI agent: the
    orchestrator resolves an omitted ``--dev-agent``/``--review-agent`` from
    profile/env/config, which would silently pair the operator's model and effort
    with a *different* agent than the preview showed, on an effort axis that
    agent may not even accept. Cursor runs both roles on the one SDK and rejects
    either flag outright.
    """
    dev_mode = spec.dev_mode or MODE_DEFAULT
    review_mode = spec.review_mode or MODE_DEFAULT
    for role, mode in (("dev", dev_mode), ("review", review_mode)):
        if mode not in LAUNCH_MODES:
            raise LaunchError(f"unknown {role} launch mode {mode!r}")
    if backend is None and (dev_mode != MODE_DEFAULT or review_mode != MODE_DEFAULT):
        raise LaunchError(
            "a named or custom role profile requires an explicit backend"
        )

    args: list[str] = []
    if backend is not None:
        args += ["--backend", backend]

    if dev_mode in NAMED_PROFILE_MODES:
        args += ["--dev-profile", dev_mode]
    elif dev_mode == MODE_CUSTOM:
        args += _custom_role_args("dev", spec.dev_agent, spec.dev_model, spec.dev_effort, backend)

    if review_mode in NAMED_PROFILE_MODES:
        args += ["--review-profile", review_mode]
    elif review_mode == MODE_CUSTOM:
        args += _custom_role_args(
            "review", spec.review_agent, spec.review_model, spec.review_effort, backend
        )
    return args


def _custom_role_args(
    role: str,
    agent: str | None,
    model: str | None,
    effort: str | None,
    backend: str | None,
) -> list[str]:
    """One custom role's explicit flags, identical in shape for both roles."""
    args: list[str] = []
    cleaned_agent = _clean(agent)
    if backend == "cc-codex":
        args += [f"--{role}-agent", _agent(role, _require(cleaned_agent, f"{role} agent"))]
    elif cleaned_agent is not None:
        raise LaunchError(f"--{role}-agent applies only to the cc-codex backend")
    return args + [
        f"--{role}-model", _require(model, f"{role} model"),
        f"--{role}-effort", _require(effort, f"{role} effort"),
    ]


def _legacy_mode_args(spec: LaunchSpec, backend: str | None) -> list[str]:
    """Flags for the legacy run-wide ``mode`` selection, unchanged since split.

    Kept exactly as pre-split API callers rely on it: ``standard``/``excellent``
    stay the ``--profile`` shorthand (which the orchestrator still applies to
    both roles), and ``custom`` keeps its optional ``--dev-agent`` — the stricter
    complete-tuple rule applies only to the new split shape, which is the one the
    dialog previews per role.

    ``review_agent`` postdates this shape and is refused here rather than
    dropped: silently ignoring it would run the review role on whichever agent
    profile/env/config resolves, pairing the caller's model with an agent it
    never asked for — the exact failure the split shape's complete-tuple rule
    exists to prevent.
    """
    mode = spec.mode
    if mode not in LAUNCH_MODES:
        raise LaunchError(f"unknown launch mode {mode!r}")
    if _clean(spec.review_agent) is not None:
        raise LaunchError(
            "'review_agent' requires the 'dev_mode'/'review_mode' selection;"
            " the legacy run-wide 'mode' has no --review-agent"
        )

    args: list[str] = []
    if mode == MODE_DEFAULT:
        # Inherit everything. Append --backend only when explicitly selected.
        if backend is not None:
            args += ["--backend", backend]
        return args
    if backend is None:
        raise LaunchError(f"{mode} launch requires an explicit backend")
    args += ["--backend", backend]
    if mode in NAMED_PROFILE_MODES:
        args += ["--profile", mode]
        return args

    dev_agent = _clean(spec.dev_agent)
    if dev_agent is not None:
        if backend != "cc-codex":
            raise LaunchError("--dev-agent applies only to the cc-codex backend")
        args += ["--dev-agent", _agent("dev", dev_agent)]
    args += [
        "--dev-model", _require(spec.dev_model, "dev model"),
        "--dev-effort", _require(spec.dev_effort, "dev effort"),
        "--review-model", _require(spec.review_model, "review model"),
        "--review-effort", _require(spec.review_effort, "review effort"),
    ]
    return args


def _agent(role: str, value: str) -> str:
    if value not in CLI_AGENTS:
        raise LaunchError(
            f"unknown {role} agent {value!r} (expected one of {', '.join(CLI_AGENTS)})"
        )
    return value


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


OPTION_CATALOG_KEY = "options"
"""The one ``effective-config:`` field a *run snapshot* drops — see
:func:`strip_option_catalog`. Only the snapshot family drops it; the Start
dialog's live query keeps it (:func:`parse_option_catalog`)."""


def strip_option_catalog(data: dict) -> dict:
    """Return the run-snapshot view of an ``effective-config:`` payload: everything
    the orchestrator logged except its ``options`` catalog.

    Every other field records what THIS run resolved to. The catalog is
    selection-independent by construction — it states what any launch MAY select,
    never what this one did — so a per-run snapshot is the single place it carries
    no information, while being the bulk of the bytes: measured against the
    deployed schema-3 orchestrator it is ~2.5 KB of a ~3.4 KB line, stored per run
    and re-sent in every entry of a fixed 50-run history window that renders none
    of it.

    Applied on read at both snapshot sources (:func:`read_effective_config_log`,
    :func:`read_effective_config_snapshot`), which is what makes an
    ``effective_config.json`` written before this drop stay readable: it keeps its
    catalog on disk and simply loses the field on the way out. Newly written
    snapshots hold no catalog at all, because the only writer persists what the log
    read already returned.
    """
    return {key: value for key, value in data.items() if key != OPTION_CATALOG_KEY}


def parse_effective_config_line(lines: Iterable[str]) -> dict | None:
    """Return the parsed ``effective-config:`` JSON from a run's log lines, or None.

    Scans for the first ``[HH:MM:SS] effective-config: {json}`` line (the
    timestamp prefix is optional) and returns its object verbatim. Tolerant: a
    malformed or non-object payload is skipped, an absent line yields ``None`` — a
    missing snapshot is a graceful degradation, never an error. The snapshot
    trim (:func:`strip_option_catalog`) belongs to the readers above this, so the
    parser stays a faithful reading of the line.
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
    Bounded head scan; any IO failure degrades to ``None``. What comes back is the
    snapshot view of the line (:func:`strip_option_catalog`), which is both what a
    live run serves and what the run's persisted snapshot is written from.
    """
    log_path = logs.locate_log(record)
    found = _scan_head(log_path) if log_path is not None else None
    if found is None and record.spawn_out is not None:
        found = _scan_head(record.spawn_out)
    return None if found is None else strip_option_catalog(found)


def _scan_head(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return parse_effective_config_line(itertools.islice(handle, _HEAD_SCAN_LINES))
    except OSError:
        return None


def effective_config_snapshot_path(run_dir: Path) -> Path:
    return run_dir / EFFECTIVE_CONFIG_NAME


def write_effective_config_snapshot(run_dir: Path, data: dict) -> None:
    """Persist the parsed ``effective-config:`` snapshot atomically (tmp + rename).

    Writes what it is given. The one caller hands it a
    :func:`read_effective_config_log` result, so a snapshot written from here is
    already catalog-free; the guarantee that no served payload carries a catalog
    is the readers' (:func:`strip_option_catalog`), which is where it has to live
    anyway for the files written before the drop.
    """
    path = effective_config_snapshot_path(run_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def read_effective_config_snapshot(run_dir: Path) -> dict | None:
    """Load the persisted ``effective_config.json`` snapshot; ``None`` if absent,
    unreadable, or not a JSON object (tolerant, like the sessions snapshot).

    Trimmed on the way out (:func:`strip_option_catalog`) rather than on the way
    in, so a snapshot written before the drop — which still has its catalog on
    disk — reads back like a new one instead of becoming a second served shape.
    """
    path = effective_config_snapshot_path(run_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return strip_option_catalog(data) if isinstance(data, dict) else None
