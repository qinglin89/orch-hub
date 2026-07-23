"""Tests for the orchestrator launch-config adapter, arg builder, and snapshot.

Everything runs against an injected fake runner (no real ``--print-config``
subprocess) and fake repos in ``tmp_path``; the deployed orchestrator is never
launched and its code is never imported. The builder and snapshot parser are pure
and tested directly.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from orch_hub.models import LaunchSpec, ManagedRepo
from orch_hub.orch_config import (
    ORCHESTRATOR_PYTHON,
    ORCHESTRATOR_SCRIPT,
    LaunchError,
    OrchestratorConfigError,
    build_launch_args,
    parse_effective_config,
    parse_effective_config_line,
    query_effective_config,
)
from orch_hub.subprocess_utils import CommandNotFoundError, CommandResult

# A representative --print-config object (the handoff sample), used across tests.
SAMPLE = {
    "schema_version": 1,
    "config_revision": "cfg-sha",
    "effective_revision": "eff-sha",
    "profile": "default",
    "available_profiles": ["excellent", "standard"],
    "backend": "cc-codex",
    "dev": {"agent": "claude", "model": "claude-opus-4-8", "effort": "max"},
    "review": {"agent": "codex", "model": "gpt-5.5", "effort": "xhigh"},
    "max_sessions": 40,
    "context_budget": 200000,
    "codex_sandbox": "danger-full-access",
    "task_id": None,
    "once": False,
    "plan_gate": False,
    "control_dir": None,
    "sources": {"backend": "config", "dev.model": "config"},
}


class FakeRunner:
    """Records calls and returns a preset result (or raises a preset error)."""

    def __init__(self, *, stdout="", returncode=0, stderr="", raises=None):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, args, *, cwd=None, timeout=None):
        self.calls.append(
            {"argv": tuple(str(a) for a in args), "cwd": cwd, "timeout": timeout}
        )
        if self.raises is not None:
            raise self.raises
        return CommandResult(
            args=tuple(str(a) for a in args),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def make_repo(tmp_path: Path, name: str = "repo-a") -> ManagedRepo:
    path = tmp_path / "repos" / name
    return ManagedRepo(name=name, path=path, manifest=path / ".ai-deploy-manifest.json")


def _json(obj) -> str:
    import json

    return json.dumps(obj)


# -- query: success paths ------------------------------------------------------


def test_query_builds_print_config_command_with_repo_cwd(tmp_path):
    repo = make_repo(tmp_path)
    runner = FakeRunner(stdout=_json(SAMPLE))

    config = query_effective_config(repo, runner=runner)

    call = runner.calls[0]
    assert call["argv"] == (
        str(repo.path / ORCHESTRATOR_PYTHON),
        str(repo.path / ORCHESTRATOR_SCRIPT),
        "--print-config",
    )
    assert call["cwd"] == repo.path
    assert call["timeout"] is not None  # bounded so a hung query cannot stall
    assert config.backend == "cc-codex"
    assert config.profile == "default"
    assert config.available_profiles == ("excellent", "standard")
    assert config.config_revision == "cfg-sha"
    assert config.effective_revision == "eff-sha"
    assert config.dev.model == "claude-opus-4-8"
    assert config.dev.effort == "max"
    assert config.review.agent == "codex"
    assert config.sources == {"backend": "config", "dev.model": "config"}


def test_query_passes_backend_and_profile_flags(tmp_path):
    repo = make_repo(tmp_path)
    runner = FakeRunner(stdout=_json({**SAMPLE, "backend": "cursor", "profile": "standard"}))

    query_effective_config(repo, backend="cursor", profile="standard", runner=runner)

    assert runner.calls[0]["argv"][2:] == (
        "--print-config",
        "--backend",
        "cursor",
        "--profile",
        "standard",
    )


def test_query_round_trips_the_effective_config_shape(tmp_path):
    repo = make_repo(tmp_path)
    runner = FakeRunner(stdout=_json(SAMPLE))

    config = query_effective_config(repo, runner=runner)

    # to_dict reproduces the orchestrator's known fields (typed, hub-owned shape).
    assert config.to_dict() == SAMPLE


# -- query: fail-closed paths --------------------------------------------------


def test_query_nonzero_exit_raises_with_stderr(tmp_path):
    repo = make_repo(tmp_path)
    runner = FakeRunner(returncode=2, stderr="error: unrecognized arguments: --print-config")

    with pytest.raises(OrchestratorConfigError) as exc:
        query_effective_config(repo, runner=runner)
    assert "unrecognized arguments" in str(exc.value)


def test_query_missing_executable_raises(tmp_path):
    repo = make_repo(tmp_path)
    runner = FakeRunner(raises=CommandNotFoundError("python"))

    with pytest.raises(OrchestratorConfigError) as exc:
        query_effective_config(repo, runner=runner)
    assert "not found" in str(exc.value)


def test_query_timeout_raises(tmp_path):
    repo = make_repo(tmp_path)
    runner = FakeRunner(raises=subprocess.TimeoutExpired(cmd="orchestrator.py", timeout=30))

    with pytest.raises(OrchestratorConfigError) as exc:
        query_effective_config(repo, runner=runner)
    assert "timed out" in str(exc.value)


def test_query_malformed_json_raises(tmp_path):
    repo = make_repo(tmp_path)
    runner = FakeRunner(stdout="not json {")

    with pytest.raises(OrchestratorConfigError) as exc:
        query_effective_config(repo, runner=runner)
    assert "not valid JSON" in str(exc.value)


def test_query_unsupported_schema_raises(tmp_path):
    repo = make_repo(tmp_path)
    runner = FakeRunner(stdout=_json({**SAMPLE, "schema_version": 99}))

    with pytest.raises(OrchestratorConfigError) as exc:
        query_effective_config(repo, runner=runner)
    assert "schema_version" in str(exc.value)


def test_parse_missing_backend_raises():
    data = {k: v for k, v in SAMPLE.items() if k != "backend"}
    with pytest.raises(OrchestratorConfigError):
        parse_effective_config(data)


def test_parse_non_object_raises():
    with pytest.raises(OrchestratorConfigError):
        parse_effective_config(["not", "an", "object"])


def test_parse_missing_role_raises():
    data = {k: v for k, v in SAMPLE.items() if k != "review"}
    with pytest.raises(OrchestratorConfigError):
        parse_effective_config(data)


def test_parse_tolerates_cursor_null_codex_sandbox():
    data = {**SAMPLE, "backend": "cursor", "codex_sandbox": None,
            "dev": {"agent": None, "model": "m", "effort": "e"}}
    config = parse_effective_config(data)
    assert config.codex_sandbox is None
    assert config.dev.agent is None


_OMIT = object()
"""Sentinel for :func:`_malformed`: delete the key rather than set a bad value."""


def _malformed(**changes) -> dict:
    """Deep-copy the valid ``SAMPLE`` and apply top-level overrides; ``_OMIT`` deletes."""
    data = copy.deepcopy(SAMPLE)
    for key, value in changes.items():
        if value is _OMIT:
            data.pop(key, None)
        else:
            data[key] = value
    return data


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(_malformed(config_revision=_OMIT), id="missing-config-revision"),
        pytest.param(_malformed(config_revision=7), id="wrong-typed-config-revision"),
        pytest.param(_malformed(effective_revision=_OMIT), id="missing-effective-revision"),
        pytest.param(_malformed(effective_revision=""), id="blank-effective-revision"),
        pytest.param(_malformed(profile=_OMIT), id="missing-profile"),
        pytest.param(_malformed(profile=7), id="wrong-typed-profile"),
        pytest.param(_malformed(available_profiles=_OMIT), id="missing-available-profiles"),
        pytest.param(_malformed(available_profiles="standard"), id="available-profiles-not-a-list"),
        pytest.param(
            _malformed(available_profiles=["standard", 3]),
            id="available-profiles-non-string-item",
        ),
        pytest.param(_malformed(max_sessions=_OMIT), id="missing-max-sessions"),
        pytest.param(_malformed(max_sessions="40"), id="wrong-typed-max-sessions"),
        pytest.param(_malformed(max_sessions=True), id="bool-max-sessions"),
        pytest.param(_malformed(context_budget=_OMIT), id="missing-context-budget"),
        pytest.param(_malformed(context_budget=1.5), id="wrong-typed-context-budget"),
        pytest.param(_malformed(sources=_OMIT), id="missing-sources"),
        pytest.param(_malformed(sources=["backend"]), id="sources-not-a-map"),
        pytest.param(_malformed(sources={"backend": 1}), id="sources-non-string-value"),
        pytest.param(_malformed(once=_OMIT), id="missing-once"),
        pytest.param(_malformed(once="false"), id="wrong-typed-once"),
        pytest.param(_malformed(once=0), id="int-once"),
        pytest.param(_malformed(plan_gate=1), id="int-plan-gate"),
        pytest.param(_malformed(dev={"agent": "claude", "effort": "max"}), id="missing-dev-model"),
        pytest.param(
            _malformed(dev={"agent": "claude", "model": None, "effort": "max"}),
            id="null-dev-model",
        ),
        pytest.param(
            _malformed(dev={"agent": "claude", "model": 7, "effort": "max"}),
            id="wrong-typed-dev-model",
        ),
        pytest.param(_malformed(dev={"agent": "claude", "model": "m"}), id="missing-dev-effort"),
        pytest.param(
            _malformed(review={"agent": "codex", "model": None, "effort": "xhigh"}),
            id="null-review-model",
        ),
        pytest.param(
            _malformed(review={"agent": "codex", "model": "gpt-5.5"}),
            id="missing-review-effort",
        ),
    ],
)
def test_parse_rejects_malformed_schema_v1_field(data):
    """A schema-version-1 payload with a missing/wrong-typed required field fails
    closed, so the config API surfaces the orchestrator error instead of a 200
    carrying null/default preview data that would leave the Start dialog launchable."""
    with pytest.raises(OrchestratorConfigError):
        parse_effective_config(data)


def test_parse_accepts_empty_profiles_and_orchestrator_nulls():
    """Guard against over-tightening: an empty ``available_profiles`` (a repo with
    no named profiles) plus the genuinely-optional nulls the orchestrator itself
    emits (role ``agent``, ``codex_sandbox``, ``task_id``, ``control_dir``) stay
    valid input."""
    data = {
        **SAMPLE,
        "backend": "cursor",
        "available_profiles": [],
        "codex_sandbox": None,
        "task_id": None,
        "control_dir": None,
        "dev": {"agent": None, "model": "m", "effort": "e"},
    }
    config = parse_effective_config(data)
    assert config.available_profiles == ()
    assert config.dev.agent is None
    assert config.codex_sandbox is None
    assert config.task_id is None
    assert config.control_dir is None


# -- build_launch_args: the four modes x backends ------------------------------


def test_default_inherited_backend_emits_nothing():
    assert build_launch_args(LaunchSpec(mode="default")) == ()


def test_default_explicit_backend_emits_only_backend():
    args = build_launch_args(LaunchSpec(mode="default", backend="cc-codex"))
    assert args == ("--backend", "cc-codex")


def test_default_never_emits_profile_or_role_flags():
    # Even if role fields leak in, default mode ignores them.
    args = build_launch_args(
        LaunchSpec(mode="default", backend="cursor", dev_model="x", review_model="y")
    )
    assert args == ("--backend", "cursor")
    for flag in ("--profile", "--dev-model", "--dev-effort", "--review-model", "--review-effort"):
        assert flag not in args


def test_standard_emits_backend_and_profile_only():
    args = build_launch_args(LaunchSpec(mode="standard", backend="cursor"))
    assert args == ("--backend", "cursor", "--profile", "standard")


def test_excellent_emits_backend_and_profile_only():
    args = build_launch_args(LaunchSpec(mode="excellent", backend="cc-codex"))
    assert args == ("--backend", "cc-codex", "--profile", "excellent")
    for flag in ("--dev-model", "--review-model"):
        assert flag not in args


def test_standard_without_backend_is_launch_error():
    with pytest.raises(LaunchError):
        build_launch_args(LaunchSpec(mode="standard"))


def test_custom_cc_codex_emits_explicit_fields_with_dev_agent():
    args = build_launch_args(
        LaunchSpec(
            mode="custom",
            backend="cc-codex",
            dev_agent="claude",
            dev_model="claude-opus-4-8",
            dev_effort="max",
            review_model="gpt-5.5",
            review_effort="xhigh",
        )
    )
    assert args == (
        "--backend", "cc-codex",
        "--dev-agent", "claude",
        "--dev-model", "claude-opus-4-8",
        "--dev-effort", "max",
        "--review-model", "gpt-5.5",
        "--review-effort", "xhigh",
    )


def test_custom_cursor_omits_dev_agent():
    args = build_launch_args(
        LaunchSpec(
            mode="custom",
            backend="cursor",
            dev_model="claude-opus-4-8",
            dev_effort="max",
            review_model="gpt-5.5",
            review_effort="xhigh",
        )
    )
    assert "--dev-agent" not in args
    assert args[:2] == ("--backend", "cursor")


def test_custom_dev_agent_on_cursor_is_launch_error():
    with pytest.raises(LaunchError) as exc:
        build_launch_args(
            LaunchSpec(
                mode="custom",
                backend="cursor",
                dev_agent="claude",
                dev_model="m",
                dev_effort="e",
                review_model="rm",
                review_effort="re",
            )
        )
    assert "dev-agent" in str(exc.value)


def test_custom_missing_model_is_launch_error():
    with pytest.raises(LaunchError):
        build_launch_args(
            LaunchSpec(
                mode="custom",
                backend="cc-codex",
                dev_model="",  # blank counts as missing
                dev_effort="max",
                review_model="gpt-5.5",
                review_effort="xhigh",
            )
        )


def test_custom_without_backend_is_launch_error():
    with pytest.raises(LaunchError):
        build_launch_args(
            LaunchSpec(mode="custom", dev_model="m", dev_effort="e",
                       review_model="rm", review_effort="re")
        )


def test_unknown_mode_and_backend_are_launch_errors():
    with pytest.raises(LaunchError):
        build_launch_args(LaunchSpec(mode="turbo"))
    with pytest.raises(LaunchError):
        build_launch_args(LaunchSpec(mode="default", backend="banana"))


# -- build_launch_args: orthogonal flags in every mode -------------------------


def test_plan_gate_appended_in_every_mode():
    assert build_launch_args(LaunchSpec(mode="default", plan_gate=True)) == ("--plan-gate",)
    assert build_launch_args(
        LaunchSpec(mode="standard", backend="cursor", plan_gate=True)
    ) == ("--backend", "cursor", "--profile", "standard", "--plan-gate")


def test_once_and_max_sessions_appended():
    args = build_launch_args(
        LaunchSpec(mode="default", backend="cc-codex", once=True, max_sessions=12, plan_gate=True)
    )
    assert args == ("--backend", "cc-codex", "--plan-gate", "--once", "--max-sessions", "12")


def test_non_positive_max_sessions_is_launch_error():
    with pytest.raises(LaunchError):
        build_launch_args(LaunchSpec(mode="default", max_sessions=0))


# -- effective-config log-line snapshot parsing --------------------------------


def test_parse_effective_config_line_with_timestamp_prefix():
    line = '[03:00:01] effective-config: {"backend":"cc-codex","profile":"standard"}'
    assert parse_effective_config_line([line]) == {
        "backend": "cc-codex",
        "profile": "standard",
    }


def test_parse_effective_config_line_without_prefix():
    line = 'effective-config: {"backend":"cursor"}'
    assert parse_effective_config_line([line]) == {"backend": "cursor"}


def test_parse_effective_config_line_skips_noise_and_malformed():
    lines = [
        "[03:00:00] orchestrator start: task=x",
        "[03:00:01] effective-config: {bad json",
        '[03:00:02] effective-config: {"backend":"cc-codex"}',
    ]
    assert parse_effective_config_line(lines) == {"backend": "cc-codex"}


def test_parse_effective_config_line_absent_is_none():
    assert parse_effective_config_line(["[03:00:00] orchestrator done"]) is None
