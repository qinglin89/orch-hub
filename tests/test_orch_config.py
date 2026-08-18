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
    read_effective_config_snapshot,
    write_effective_config_snapshot,
)
from orch_hub.subprocess_utils import CommandNotFoundError, CommandResult

# The orchestrator's published effort vocabularies, per parameter axis: a claude
# agent and a codex agent do NOT accept the same words, which is the whole reason
# the catalog is keyed by agent.
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CODEX_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"]


def _model(model_id: str, axis: str, efforts: list[str]) -> dict:
    return {"id": model_id, "effort_axis": axis, "efforts": list(efforts)}


# cc-codex fixes the axis per agent, so the agent entry states it once and every
# model repeats it.
CC_AGENTS = {
    "claude": {
        "effort_axis": "effort",
        "efforts": CLAUDE_EFFORTS,
        "models": [
            _model("claude-opus-4-8", "effort", CLAUDE_EFFORTS),
            _model("claude-opus-5", "effort", CLAUDE_EFFORTS),
        ],
    },
    "codex": {
        "effort_axis": "reasoning",
        "efforts": CODEX_EFFORTS,
        "models": [_model("gpt-5.5", "reasoning", CODEX_EFFORTS)],
    },
}
# Cursor runs both roles on the one SDK and takes either model family, so the
# agent fixes no axis (null) and each model states its own.
CURSOR_AGENTS = {
    "cursor": {
        "effort_axis": None,
        "efforts": None,
        "models": [
            _model("claude-opus-4-8", "effort", CLAUDE_EFFORTS),
            _model("gpt-5.5", "reasoning", CODEX_EFFORTS),
        ],
    }
}
# The full `options` object as the orchestrator publishes it, including the
# top-level keys the hub does not render — so the parse's projection is
# exercised rather than assumed.
CATALOG = {
    "backends": ["cc-codex", "cursor"],
    "run_profiles": ["excellent", "standard"],
    "role_profiles": ["default", "excellent", "standard"],
    "codex_sandbox": ["danger-full-access", "read-only", "workspace-write"],
    "efforts": {"effort": CLAUDE_EFFORTS, "reasoning": CODEX_EFFORTS},
    "effort_aliases": {"effort": {}, "reasoning": {"extra-high": "xhigh"}},
    "agents": {
        "cc-codex": {"dev": CC_AGENTS, "review": CC_AGENTS},
        "cursor": {"dev": CURSOR_AGENTS, "review": CURSOR_AGENTS},
    },
}
# What the hub keeps: the per-backend/role/agent axis, verbatim.
CATALOG_PROJECTION = {"agents": CATALOG["agents"]}

# A representative --print-config object (the handoff sample), used across tests.
SAMPLE = {
    "schema_version": 3,
    "config_revision": "cfg-sha",
    "effective_revision": "eff-sha",
    "profile": "default",
    "profiles": {"dev": "standard", "review": "excellent"},
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
    "options": CATALOG,
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
    assert (config.dev_profile, config.review_profile) == ("standard", "excellent")
    assert config.split_profiles is True
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


def test_query_passes_both_role_profile_flags_in_one_call(tmp_path):
    """Both roles are resolved by one subprocess, so the preview is coherent."""
    repo = make_repo(tmp_path)
    runner = FakeRunner(stdout=_json(SAMPLE))

    query_effective_config(
        repo,
        backend="cc-codex",
        dev_profile="standard",
        review_profile="excellent",
        runner=runner,
    )

    assert len(runner.calls) == 1
    assert runner.calls[0]["argv"][2:] == (
        "--print-config",
        "--backend",
        "cc-codex",
        "--dev-profile",
        "standard",
        "--review-profile",
        "excellent",
    )


def test_query_omits_unselected_role_profile_flags(tmp_path):
    """A capability probe / one-sided selection sends only what was selected, so an
    older deployment can still answer the flag-free query."""
    repo = make_repo(tmp_path)
    runner = FakeRunner(stdout=_json(SAMPLE))

    query_effective_config(repo, dev_profile="excellent", runner=runner)

    argv = runner.calls[0]["argv"]
    assert argv[2:] == ("--print-config", "--dev-profile", "excellent")
    assert "--review-profile" not in argv
    assert "--profile" not in argv


def test_query_round_trips_the_effective_config_shape(tmp_path):
    repo = make_repo(tmp_path)
    runner = FakeRunner(stdout=_json(SAMPLE))

    config = query_effective_config(repo, runner=runner)

    # to_dict reproduces the orchestrator's known fields (typed, hub-owned shape)
    # plus the hub-owned capability marker the UI gates split selection on. The
    # option catalog is the one projected field: what a caller renders, not the
    # whole published object.
    assert config.to_dict() == {
        **SAMPLE,
        "split_profiles": True,
        "options": CATALOG_PROJECTION,
    }


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


def test_a_target_ahead_of_the_hub_is_told_to_update_the_hub(tmp_path):
    """The remedy has to follow the direction of the mismatch.

    A target above this hub's version means the canonical suite moved and the hub
    has not caught up. Redeploying that target — the advice for the other
    direction — pushes it further away, so the message must not give it.
    """
    repo = make_repo(tmp_path)
    runner = FakeRunner(stdout=_json({**SAMPLE, "schema_version": 99}))

    with pytest.raises(OrchestratorConfigError) as exc:
        query_effective_config(repo, runner=runner)
    message = str(exc.value)
    assert "update orch-hub" in message
    assert "redeploy" not in message


def test_a_target_behind_the_hub_is_told_to_redeploy_it(tmp_path):
    repo = make_repo(tmp_path)
    runner = FakeRunner(stdout=_json({**SAMPLE, "schema_version": 1}))

    with pytest.raises(OrchestratorConfigError) as exc:
        query_effective_config(repo, runner=runner)
    message = str(exc.value)
    assert "redeploy the orchestrator payload" in message
    assert "update orch-hub" not in message


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
        # `profiles` is additive, but a deployment that emits it emits both roles:
        # a half-present object is malformed, not an old deployment.
        pytest.param(_malformed(profiles="standard"), id="profiles-not-an-object"),
        pytest.param(_malformed(profiles={"dev": "standard"}), id="missing-profiles-review"),
        pytest.param(
            _malformed(profiles={"dev": "standard", "review": 7}),
            id="wrong-typed-profiles-review",
        ),
        pytest.param(
            _malformed(profiles={"dev": "", "review": "standard"}),
            id="blank-profiles-dev",
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


# -- parse: split-profile capability -------------------------------------------


@pytest.mark.parametrize("absent", [_OMIT, None], ids=["omitted", "null"])
def test_parse_derives_role_profiles_from_legacy_profile(absent):
    """An older deployment without `profiles` stays readable: both roles derive
    from the run-wide profile, but the capability flag stays false so a UI cannot
    mistake the derivation for split support."""
    config = parse_effective_config(_malformed(profile="standard", profiles=absent))

    assert config.profile == "standard"
    assert (config.dev_profile, config.review_profile) == ("standard", "standard")
    assert config.split_profiles is False


def test_parse_prefers_authoritative_profiles_over_legacy_profile():
    """A split launch leaves the run-wide `profile` at its default; the role
    selections are the authoritative ones and must not be re-derived from it."""
    config = parse_effective_config(
        _malformed(profile="default", profiles={"dev": "excellent", "review": "standard"})
    )

    assert (config.dev_profile, config.review_profile) == ("excellent", "standard")
    assert config.split_profiles is True


def test_legacy_response_round_trips_derived_profiles():
    """The serialized shape is uniform for both deployments: a reader always finds
    `profiles`, and only `split_profiles` says whether it was observed or derived."""
    payload = parse_effective_config(
        _malformed(profile="excellent", profiles=_OMIT)
    ).to_dict()

    assert payload["profiles"] == {"dev": "excellent", "review": "excellent"}
    assert payload["split_profiles"] is False


# -- parse: the launch option catalog ------------------------------------------


def test_parse_keeps_the_catalog_keyed_by_backend_role_and_agent():
    """The dialog filters by all three at once: the SAME model list is legal for
    one agent and refused by the other, so a flattened catalog could not answer
    'what may this role offer now'."""
    config = parse_effective_config(SAMPLE)

    agents = config.options["agents"]
    assert sorted(agents) == ["cc-codex", "cursor"]
    assert sorted(agents["cc-codex"]) == ["dev", "review"]
    claude = agents["cc-codex"]["review"]["claude"]
    codex = agents["cc-codex"]["review"]["codex"]
    assert [m["id"] for m in claude["models"]] == ["claude-opus-4-8", "claude-opus-5"]
    assert claude["efforts"] == CLAUDE_EFFORTS
    assert codex["efforts"] == CODEX_EFFORTS  # a different axis for the same role


def test_parse_keeps_a_cursor_agent_that_fixes_no_axis():
    """Cursor takes either model family, so the agent publishes no axis and each
    model carries its own — a caller that read only the agent level would show
    the wrong effort words for half the models."""
    cursor = parse_effective_config(SAMPLE).options["agents"]["cursor"]["dev"]["cursor"]

    assert cursor["effort_axis"] is None and cursor["efforts"] is None
    assert [(m["id"], m["efforts"]) for m in cursor["models"]] == [
        ("claude-opus-4-8", CLAUDE_EFFORTS),
        ("gpt-5.5", CODEX_EFFORTS),
    ]


@pytest.mark.parametrize(
    "options",
    [
        pytest.param(_OMIT, id="absent"),
        pytest.param(None, id="null"),
        pytest.param("claude-opus-5", id="not-an-object"),
        pytest.param({"backends": ["cursor"]}, id="no-agents-key"),
        pytest.param({"agents": ["cursor"]}, id="agents-not-an-object"),
        pytest.param({"agents": {}}, id="agents-empty"),
        pytest.param({"agents": {"cc-codex": "claude"}}, id="backend-not-an-object"),
        pytest.param({"agents": {"cc-codex": {"dev": []}}}, id="role-not-an-object"),
        pytest.param({"agents": {"cc-codex": {"dev": {"claude": "opus"}}}}, id="agent-not-an-object"),
    ],
)
def test_parse_reports_no_catalog_rather_than_inventing_one(options):
    """A payload that publishes nothing usable yields None — the caller must then
    keep free-text entry. Substituting the hub's own idea of the valid models is
    the failure this parse exists to prevent: orch-hub owns no model policy, and
    a wrong list is a startup refusal the operator cannot see coming."""
    config = parse_effective_config(_malformed(options=options))

    assert config.options is None
    assert config.to_dict()["options"] is None


def test_parse_drops_only_the_malformed_part_of_a_catalog():
    """Normalization only ever drops. A junk model entry must not take the rest of
    its agent — nor the other backend — down with it, and must not survive as a
    half-built choice either."""
    catalog = copy.deepcopy(CATALOG)
    catalog["agents"]["cc-codex"]["dev"] = {
        "claude": {
            "effort_axis": "effort",
            "efforts": CLAUDE_EFFORTS,
            "models": [
                _model("claude-opus-5", "effort", CLAUDE_EFFORTS),
                {"effort_axis": "effort"},  # no id at all
                {"id": "", "efforts": CLAUDE_EFFORTS},  # blank id
                {"id": "claude-opus-5"},  # a repeat of the first
                "claude-opus-4-8",  # not even an object
            ],
        },
        "codex": {"effort_axis": "reasoning", "models": "gpt-5.5"},
    }

    agents = parse_effective_config(_malformed(options=catalog)).options["agents"]
    dev = agents["cc-codex"]["dev"]

    assert [m["id"] for m in dev["claude"]["models"]] == ["claude-opus-5"]
    # An agent whose models are unusable keeps its entry: it still publishes an
    # axis, and "no models listed" is the same free-text degradation as no
    # catalog at all, decided once by the caller.
    assert dev["codex"]["models"] == [] and dev["codex"]["effort_axis"] == "reasoning"
    # The untouched backend is unaffected.
    assert agents["cursor"] == CATALOG["agents"]["cursor"]


def test_parse_treats_an_empty_effort_list_as_no_list():
    """An effort control offering zero choices is worse than a text box: it makes
    a legal launch unreachable. Publishing nothing usable degrades to free text."""
    catalog = copy.deepcopy(CATALOG)
    catalog["agents"]["cc-codex"]["dev"]["claude"]["efforts"] = []
    catalog["agents"]["cc-codex"]["dev"]["claude"]["models"][0]["efforts"] = [" ", 7]

    claude = parse_effective_config(_malformed(options=catalog)).options["agents"]["cc-codex"]["dev"]["claude"]

    assert claude["efforts"] is None
    assert claude["models"][0]["efforts"] is None


def test_parse_never_fails_the_query_over_the_advisory_catalog():
    """Every other field is fail-closed because a wrong value launches the wrong
    run. The catalog only says what a caller MAY offer — the orchestrator still
    launches an uncatalogued model — so a malformed one must not disable
    launching."""
    config = parse_effective_config(_malformed(options={"agents": 7}))

    assert config.options is None
    assert config.dev.model == "claude-opus-4-8"  # the launch config still resolved


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


def test_legacy_custom_cc_codex_keeps_dev_agent_optional():
    """Compatibility: the complete-tuple rule is new to the split shape only, so a
    pre-split caller that omitted `dev_agent` still launches exactly as before."""
    args = build_launch_args(
        LaunchSpec(
            mode="custom",
            backend="cc-codex",
            dev_model="claude-opus-4-8",
            dev_effort="max",
            review_model="gpt-5.5",
            review_effort="xhigh",
        )
    )
    assert "--dev-agent" not in args
    assert args[:2] == ("--backend", "cc-codex")


# -- build_launch_args: independent dev/review role selection ------------------


def test_split_default_default_emits_no_flags():
    args = build_launch_args(LaunchSpec(dev_mode="default", review_mode="default"))
    assert args == ()


def test_split_default_default_with_explicit_backend():
    args = build_launch_args(
        LaunchSpec(dev_mode="default", review_mode="default", backend="cursor")
    )
    assert args == ("--backend", "cursor")


def test_split_named_dev_only():
    """Handoff row: Excellent / Default — only the dev role's profile flag."""
    args = build_launch_args(
        LaunchSpec(dev_mode="excellent", review_mode="default", backend="cc-codex")
    )
    assert args == ("--backend", "cc-codex", "--dev-profile", "excellent")


def test_split_named_review_only():
    """Handoff row: Default / Standard — only the review role's profile flag."""
    args = build_launch_args(
        LaunchSpec(dev_mode="default", review_mode="standard", backend="cc-codex")
    )
    assert args == ("--backend", "cc-codex", "--review-profile", "standard")


def test_split_mixed_named_roles_emit_both_profile_flags():
    args = build_launch_args(
        LaunchSpec(dev_mode="standard", review_mode="excellent", backend="cc-codex")
    )
    assert args == (
        "--backend", "cc-codex",
        "--dev-profile", "standard",
        "--review-profile", "excellent",
    )


def test_split_never_emits_the_run_wide_profile_shorthand():
    """The split path resolves each role independently; a run-wide `--profile`
    would re-introduce the coupling it exists to remove."""
    args = build_launch_args(
        LaunchSpec(dev_mode="standard", review_mode="standard", backend="cursor")
    )
    assert "--profile" not in args
    assert args.count("--dev-profile") == 1 and args.count("--review-profile") == 1


def test_split_omitted_role_is_default():
    args = build_launch_args(LaunchSpec(dev_mode="excellent", backend="cc-codex"))
    assert args == ("--backend", "cc-codex", "--dev-profile", "excellent")


def test_split_custom_dev_with_named_review():
    """Handoff row: Custom / Excellent — explicit dev flags plus the review profile."""
    args = build_launch_args(
        LaunchSpec(
            dev_mode="custom",
            review_mode="excellent",
            backend="cc-codex",
            dev_agent="codex",
            dev_model="gpt-5.6-sol",
            dev_effort="xhigh",
        )
    )
    assert args == (
        "--backend", "cc-codex",
        "--dev-agent", "codex",
        "--dev-model", "gpt-5.6-sol",
        "--dev-effort", "xhigh",
        "--review-profile", "excellent",
    )


def test_split_named_dev_with_custom_review():
    """Handoff row: Standard / Custom — the dev profile plus explicit review flags,
    and no dev model/effort leaking from the other role's editable fields."""
    args = build_launch_args(
        LaunchSpec(
            dev_mode="standard",
            review_mode="custom",
            backend="cc-codex",
            review_agent="codex",
            review_model="gpt-5.5",
            review_effort="xhigh",
        )
    )
    assert args == (
        "--backend", "cc-codex",
        "--dev-profile", "standard",
        "--review-agent", "codex",
        "--review-model", "gpt-5.5",
        "--review-effort", "xhigh",
    )
    assert "--dev-model" not in args


def test_split_custom_both_roles():
    args = build_launch_args(
        LaunchSpec(
            dev_mode="custom",
            review_mode="custom",
            backend="cc-codex",
            dev_agent="claude",
            dev_model="claude-opus-4-8",
            dev_effort="max",
            review_agent="codex",
            review_model="gpt-5.5",
            review_effort="xhigh",
        )
    )
    assert args == (
        "--backend", "cc-codex",
        "--dev-agent", "claude",
        "--dev-model", "claude-opus-4-8",
        "--dev-effort", "max",
        "--review-agent", "codex",
        "--review-model", "gpt-5.5",
        "--review-effort", "xhigh",
    )


def test_split_custom_cc_codex_review_requires_the_complete_agent_tuple():
    """The review role names its own CLI agent now, so the dev rule applies to it
    unchanged: an omitted `--review-agent` resolves from profile/env/config and
    would pair the operator's model and effort with an agent the preview never
    showed — on an effort axis that agent may not even accept."""
    with pytest.raises(LaunchError) as exc:
        build_launch_args(
            LaunchSpec(
                dev_mode="default",
                review_mode="custom",
                backend="cc-codex",
                review_model="gpt-5.5",
                review_effort="xhigh",
            )
        )
    assert "review agent" in str(exc.value)


def test_split_custom_cursor_review_omits_review_agent():
    """Cursor runs both roles on the one SDK: no review agent to send."""
    args = build_launch_args(
        LaunchSpec(
            dev_mode="default",
            review_mode="custom",
            backend="cursor",
            review_model="gpt-5.5",
            review_effort="xhigh",
        )
    )
    assert args == (
        "--backend", "cursor",
        "--review-model", "gpt-5.5",
        "--review-effort", "xhigh",
    )


def test_split_custom_cursor_review_agent_is_launch_error():
    with pytest.raises(LaunchError) as exc:
        build_launch_args(
            LaunchSpec(
                dev_mode="default",
                review_mode="custom",
                backend="cursor",
                review_agent="codex",
                review_model="m",
                review_effort="e",
            )
        )
    assert "review-agent" in str(exc.value)


def test_split_custom_unknown_review_agent_is_launch_error():
    with pytest.raises(LaunchError):
        build_launch_args(
            LaunchSpec(
                dev_mode="default",
                review_mode="custom",
                backend="cc-codex",
                review_agent="gemini",
                review_model="m",
                review_effort="e",
            )
        )


def test_legacy_mode_refuses_a_review_agent_rather_than_dropping_it():
    """The legacy run-wide shape has no --review-agent. Ignoring the field would
    run review on whatever profile/env/config resolves while the caller believes
    it selected an agent — refuse instead."""
    with pytest.raises(LaunchError) as exc:
        build_launch_args(
            LaunchSpec(
                mode="custom",
                backend="cc-codex",
                dev_model="claude-opus-4-8",
                dev_effort="max",
                review_agent="codex",
                review_model="gpt-5.5",
                review_effort="xhigh",
            )
        )
    assert "review_agent" in str(exc.value)


def test_split_custom_cc_codex_dev_requires_the_complete_agent_tuple():
    """The orchestrator resolves an omitted `--dev-agent` from profile/env/config,
    which would pair the operator's model/effort with an agent the preview never
    showed. Split-mode custom dev must send the whole tuple."""
    with pytest.raises(LaunchError) as exc:
        build_launch_args(
            LaunchSpec(
                dev_mode="custom",
                review_mode="default",
                backend="cc-codex",
                dev_model="claude-opus-4-8",
                dev_effort="max",
            )
        )
    assert "dev agent" in str(exc.value)


def test_split_custom_cursor_dev_omits_dev_agent():
    args = build_launch_args(
        LaunchSpec(
            dev_mode="custom",
            review_mode="default",
            backend="cursor",
            dev_model="claude-opus-4-8",
            dev_effort="max",
        )
    )
    assert args == (
        "--backend", "cursor",
        "--dev-model", "claude-opus-4-8",
        "--dev-effort", "max",
    )


def test_split_custom_cursor_dev_agent_is_launch_error():
    with pytest.raises(LaunchError) as exc:
        build_launch_args(
            LaunchSpec(
                dev_mode="custom",
                review_mode="default",
                backend="cursor",
                dev_agent="claude",
                dev_model="m",
                dev_effort="e",
            )
        )
    assert "dev-agent" in str(exc.value)


def test_split_custom_unknown_dev_agent_is_launch_error():
    with pytest.raises(LaunchError):
        build_launch_args(
            LaunchSpec(
                dev_mode="custom",
                review_mode="default",
                backend="cc-codex",
                dev_agent="gemini",
                dev_model="m",
                dev_effort="e",
            )
        )


def test_split_custom_missing_role_value_is_launch_error():
    with pytest.raises(LaunchError):
        build_launch_args(
            LaunchSpec(
                dev_mode="default",
                review_mode="custom",
                backend="cc-codex",
                review_model="gpt-5.5",
                review_effort="   ",  # blank counts as missing
            )
        )


def test_split_named_role_without_backend_is_launch_error():
    """Backend may be inherited only when nothing but Default is selected: a named
    or custom role resolves against a backend, so it must be the previewed one."""
    with pytest.raises(LaunchError):
        build_launch_args(LaunchSpec(dev_mode="default", review_mode="standard"))
    with pytest.raises(LaunchError):
        build_launch_args(
            LaunchSpec(dev_mode="custom", dev_model="m", dev_effort="e")
        )


def test_split_unknown_role_mode_is_launch_error():
    with pytest.raises(LaunchError):
        build_launch_args(LaunchSpec(dev_mode="turbo", backend="cursor"))
    with pytest.raises(LaunchError):
        build_launch_args(LaunchSpec(review_mode="turbo", backend="cursor"))


def test_mixing_legacy_mode_with_role_modes_is_launch_error():
    """The two shapes map to different flags; guessing which the caller meant could
    launch a configuration nobody selected."""
    with pytest.raises(LaunchError) as exc:
        build_launch_args(
            LaunchSpec(mode="standard", dev_mode="excellent", backend="cc-codex")
        )
    assert "not both" in str(exc.value)


def test_no_selection_at_all_is_launch_error():
    with pytest.raises(LaunchError):
        build_launch_args(LaunchSpec(backend="cc-codex"))


def test_split_orthogonal_flags_compose_with_role_selection():
    args = build_launch_args(
        LaunchSpec(
            dev_mode="excellent",
            review_mode="standard",
            backend="cc-codex",
            plan_gate=True,
            once=True,
            max_sessions=12,
        )
    )
    assert args == (
        "--backend", "cc-codex",
        "--dev-profile", "excellent",
        "--review-profile", "standard",
        "--plan-gate",
        "--once",
        "--max-sessions", "12",
    )


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


def test_snapshot_round_trips_both_role_profile_shapes(tmp_path):
    """Snapshots stay raw on purpose: a split run keeps `profiles` verbatim and an
    old snapshot keeps its single `profile`, so the display layer — not the store —
    decides how a legacy run's roles are derived."""
    split = {"profile": "default", "profiles": {"dev": "standard", "review": "excellent"}}
    legacy = {"profile": "excellent"}

    for name, data in (("split", split), ("legacy", legacy)):
        run_dir = tmp_path / name
        run_dir.mkdir()
        write_effective_config_snapshot(run_dir, data)
        assert read_effective_config_snapshot(run_dir) == data
