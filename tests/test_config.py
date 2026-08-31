"""Tests for config defaults and environment overrides.

Passes explicit mappings to ``Config.from_env`` so the process environment is
never mutated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orch_hub.config import (
    BARK_URL_ENV,
    CLAUDE_HOME_ENV,
    CODEX_HOME_ENV,
    DATA_DIR_ENV,
    DEFAULT_CLAUDE_HOME,
    DEFAULT_CODEX_HOME,
    DEFAULT_DATA_DIR,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_EVAL_EFFORT,
    DEFAULT_EVAL_MODEL,
    DEFAULT_EVAL_DISPATCH_SPACING,
    DEFAULT_EVAL_QUEUE_MAX_AGE,
    DEFAULT_EVAL_RETENTION,
    DEFAULT_EVAL_TURN_TIMEOUT,
    DEFAULT_EVAL_USAGE_CEILING,
    DEFAULT_INTAKE_EFFORT,
    DEFAULT_INTAKE_MODEL,
    EVAL_AUTO_ENV,
    EVAL_DISPATCH_SPACING_ENV,
    EVAL_EFFORT_ENV,
    EVAL_ENABLED_ENV,
    EVAL_MODEL_ENV,
    EVAL_QUEUE_MAX_AGE_ENV,
    EVAL_RETENTION_ENV,
    EVAL_TURN_TIMEOUT_ENV,
    EVAL_USAGE_CEILING_ENV,
    HOST_ENV,
    INTAKE_EFFORT_ENV,
    INTAKE_MODEL_ENV,
    LEGACY_MANDREL_PATH_ENV,
    MANDREL_PATH_ENV,
    PORT_ENV,
    TOKEN_ENV,
    Config,
)


def _configured_from_env(environ: dict[str, str] | None = None) -> Config:
    """Resolve non-path settings against a synthetic configured checkout."""
    env = {MANDREL_PATH_ENV: "/test/mandrel"}
    env.update(environ or {})
    return Config.from_env(env)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {MANDREL_PATH_ENV: ""},
        {MANDREL_PATH_ENV: "   "},
        {LEGACY_MANDREL_PATH_ENV: "   "},
        {MANDREL_PATH_ENV: " ", LEGACY_MANDREL_PATH_ENV: "\n"},
    ],
)
def test_mandrel_path_is_required_when_env_absent_or_blank(env):
    with pytest.raises(ValueError, match=MANDREL_PATH_ENV) as error:
        Config.from_env(env)
    assert LEGACY_MANDREL_PATH_ENV in str(error.value)


def test_env_override():
    cfg = Config.from_env({MANDREL_PATH_ENV: "/custom/mandrel"})
    assert cfg.mandrel_path == Path("/custom/mandrel")


def test_env_override_expands_user():
    cfg = Config.from_env({MANDREL_PATH_ENV: "~/mandrel"})
    assert cfg.mandrel_path == Path("~/mandrel").expanduser()
    assert not str(cfg.mandrel_path).startswith("~")


def test_mandrel_path_strips_surrounding_whitespace():
    cfg = Config.from_env({MANDREL_PATH_ENV: " /custom/mandrel \n"})
    assert cfg.mandrel_path == Path("/custom/mandrel")


def test_legacy_mandrel_path_env_still_resolves():
    """A launchd plist installed before the rename carries the old key; it must
    keep pointing the service at its checkout across a package upgrade."""
    cfg = Config.from_env({LEGACY_MANDREL_PATH_ENV: "/legacy/checkout"})
    assert cfg.mandrel_path == Path("/legacy/checkout")


def test_current_mandrel_path_env_wins_over_legacy():
    cfg = Config.from_env(
        {MANDREL_PATH_ENV: "/current", LEGACY_MANDREL_PATH_ENV: "/legacy"}
    )
    assert cfg.mandrel_path == Path("/current")


def test_blank_current_mandrel_path_env_falls_back_to_legacy():
    """Blank counts as unset for the current name too, so a plist carrying both
    an empty new key and a real old one resolves to the old one, not the default."""
    cfg = Config.from_env(
        {MANDREL_PATH_ENV: "  ", LEGACY_MANDREL_PATH_ENV: " /legacy "}
    )
    assert cfg.mandrel_path == Path("/legacy")


def test_derived_paths():
    cfg = Config(mandrel_path=Path("/root/mandrel"))
    assert cfg.mandrel_bin == Path("/root/mandrel/bin/mandrel")
    assert cfg.registry_file == Path("/root/mandrel/.registry/repos.local.json")


def test_token_defaults_to_none():
    assert _configured_from_env().token is None
    assert Config(mandrel_path=Path("/root/mandrel")).token is None


def test_token_from_env():
    cfg = _configured_from_env({TOKEN_ENV: "s3cret-token"})
    assert cfg.token == "s3cret-token"


def test_token_strips_surrounding_whitespace():
    cfg = _configured_from_env({TOKEN_ENV: " s3cret-token\n"})
    assert cfg.token == "s3cret-token"


def test_token_empty_or_whitespace_counts_as_unset():
    assert _configured_from_env({TOKEN_ENV: ""}).token is None
    assert _configured_from_env({TOKEN_ENV: "   "}).token is None


def test_service_settings_default_to_loopback():
    cfg = _configured_from_env()
    assert cfg.host == DEFAULT_HOST == "127.0.0.1"
    assert cfg.port == DEFAULT_PORT == 8765
    assert cfg.data_dir == DEFAULT_DATA_DIR


def test_host_from_env_strips_whitespace():
    assert _configured_from_env({HOST_ENV: " 100.64.0.7 "}).host == "100.64.0.7"


def test_host_empty_or_whitespace_counts_as_unset():
    assert _configured_from_env({HOST_ENV: ""}).host == DEFAULT_HOST
    assert _configured_from_env({HOST_ENV: "   "}).host == DEFAULT_HOST


def test_port_from_env():
    assert _configured_from_env({PORT_ENV: "9000"}).port == 9000
    assert _configured_from_env({PORT_ENV: " 9000 "}).port == 9000


def test_port_empty_or_whitespace_counts_as_unset():
    assert _configured_from_env({PORT_ENV: ""}).port == DEFAULT_PORT
    assert _configured_from_env({PORT_ENV: "   "}).port == DEFAULT_PORT


def test_port_malformed_is_explicit_error():
    # A typo'd port must fail at startup, not silently bind the default.
    with pytest.raises(ValueError, match=PORT_ENV):
        _configured_from_env({PORT_ENV: "eight"})


def test_port_out_of_range_is_explicit_error():
    with pytest.raises(ValueError, match=PORT_ENV):
        _configured_from_env({PORT_ENV: "0"})
    with pytest.raises(ValueError, match=PORT_ENV):
        _configured_from_env({PORT_ENV: "65536"})


def test_data_dir_from_env_expands_user():
    cfg = _configured_from_env({DATA_DIR_ENV: "~/orch-data"})
    assert cfg.data_dir == Path("~/orch-data").expanduser()
    assert not str(cfg.data_dir).startswith("~")


def test_data_dir_empty_or_whitespace_counts_as_unset():
    assert _configured_from_env({DATA_DIR_ENV: ""}).data_dir == DEFAULT_DATA_DIR
    assert _configured_from_env({DATA_DIR_ENV: "   "}).data_dir == DEFAULT_DATA_DIR


def test_codex_home_defaults_when_env_absent():
    assert _configured_from_env().codex_home == DEFAULT_CODEX_HOME


def test_codex_home_from_env_expands_user():
    cfg = _configured_from_env({CODEX_HOME_ENV: "~/alt-codex"})
    assert cfg.codex_home == Path("~/alt-codex").expanduser()
    assert not str(cfg.codex_home).startswith("~")


def test_codex_home_empty_or_whitespace_counts_as_unset():
    assert _configured_from_env({CODEX_HOME_ENV: ""}).codex_home == DEFAULT_CODEX_HOME
    assert _configured_from_env({CODEX_HOME_ENV: "   "}).codex_home == DEFAULT_CODEX_HOME


def test_claude_home_defaults_when_env_absent():
    assert _configured_from_env().claude_home == DEFAULT_CLAUDE_HOME


def test_claude_home_from_env_expands_user():
    cfg = _configured_from_env({CLAUDE_HOME_ENV: "~/alt-claude"})
    assert cfg.claude_home == Path("~/alt-claude").expanduser()
    assert not str(cfg.claude_home).startswith("~")


def test_claude_home_empty_or_whitespace_counts_as_unset():
    assert _configured_from_env({CLAUDE_HOME_ENV: ""}).claude_home == DEFAULT_CLAUDE_HOME
    assert _configured_from_env({CLAUDE_HOME_ENV: "   "}).claude_home == DEFAULT_CLAUDE_HOME


def test_claude_transcripts_dir_derives_from_the_home():
    cfg = _configured_from_env({CLAUDE_HOME_ENV: "/tmp/claude"})
    assert cfg.claude_transcripts_dir == Path("/tmp/claude/projects")


def test_bark_url_defaults_to_none():
    assert _configured_from_env().bark_url is None
    assert Config(mandrel_path=Path("/root/mandrel")).bark_url is None


def test_bark_url_from_env_strips_whitespace():
    cfg = _configured_from_env({BARK_URL_ENV: " https://bark.example/KEY \n"})
    assert cfg.bark_url == "https://bark.example/KEY"


def test_bark_url_empty_or_whitespace_counts_as_unset():
    assert _configured_from_env({BARK_URL_ENV: ""}).bark_url is None
    assert _configured_from_env({BARK_URL_ENV: "   "}).bark_url is None


def test_intake_model_defaults_and_overrides():
    # Always a concrete string so the intake agent is unconditionally
    # constructible at app startup; overridden by ORCH_HUB_INTAKE_MODEL.
    assert _configured_from_env().intake_model == DEFAULT_INTAKE_MODEL
    assert Config(mandrel_path=Path("/root/mandrel")).intake_model == DEFAULT_INTAKE_MODEL
    assert (
        _configured_from_env({INTAKE_MODEL_ENV: "claude-sonnet-5"}).intake_model
        == "claude-sonnet-5"
    )
    # Empty/whitespace-only counts as unset and falls back to the default.
    assert _configured_from_env({INTAKE_MODEL_ENV: "  "}).intake_model == DEFAULT_INTAKE_MODEL
    assert _configured_from_env({INTAKE_MODEL_ENV: " claude-x "}).intake_model == "claude-x"


def test_intake_effort_defaults_and_overrides():
    # The model's companion knob on the same intake turns, same unset rules.
    assert _configured_from_env().intake_effort == DEFAULT_INTAKE_EFFORT
    direct = Config(mandrel_path=Path("/root/mandrel"))
    assert direct.intake_effort == DEFAULT_INTAKE_EFFORT
    assert _configured_from_env({INTAKE_EFFORT_ENV: "low"}).intake_effort == "low"
    # Empty/whitespace-only counts as unset and falls back to the default.
    assert _configured_from_env({INTAKE_EFFORT_ENV: ""}).intake_effort == DEFAULT_INTAKE_EFFORT
    assert _configured_from_env({INTAKE_EFFORT_ENV: "   "}).intake_effort == DEFAULT_INTAKE_EFFORT
    assert _configured_from_env({INTAKE_EFFORT_ENV: " high "}).intake_effort == "high"


def test_intake_default_tier_matches_dev_session_tier():
    # Intake's defaults are a deliberate tier decision (the scope/decomposition/
    # session-est/prefetch calls made here are inherited by every dev session on the
    # task), not incidental constants — pin the literals so a silent downgrade to a
    # cheaper drafting tier is a test failure.
    assert (DEFAULT_INTAKE_MODEL, DEFAULT_INTAKE_EFFORT) == ("claude-opus-5", "max")


def test_intake_and_eval_tiers_are_independent_knobs():
    # Two subsystems, two turn kinds: an operator downgrading one must not move the
    # other (and intake is not behind the evaluation gate).
    cfg = _configured_from_env({INTAKE_MODEL_ENV: "claude-x", INTAKE_EFFORT_ENV: "low"})
    assert (cfg.eval_model, cfg.eval_effort) == (DEFAULT_EVAL_MODEL, DEFAULT_EVAL_EFFORT)
    cfg = _configured_from_env({EVAL_MODEL_ENV: "claude-y", EVAL_EFFORT_ENV: "low"})
    assert (cfg.intake_model, cfg.intake_effort) == (
        DEFAULT_INTAKE_MODEL,
        DEFAULT_INTAKE_EFFORT,
    )


def test_evaluation_enabled_defaults_to_true():
    # Enabled by default for local use; a deployment disables it explicitly.
    assert _configured_from_env().evaluation_enabled is True
    assert Config(mandrel_path=Path("/root/mandrel")).evaluation_enabled is True


def test_evaluation_enabled_falsy_tokens_disable():
    for token in ("0", "false", "no", "off", "FALSE", " Off ", "No\n"):
        assert _configured_from_env({EVAL_ENABLED_ENV: token}).evaluation_enabled is False


def test_evaluation_enabled_truthy_tokens_enable():
    for token in ("1", "true", "yes", "on", "TRUE", " On ", "Yes\n"):
        assert _configured_from_env({EVAL_ENABLED_ENV: token}).evaluation_enabled is True


def test_evaluation_enabled_empty_or_whitespace_counts_as_unset():
    assert _configured_from_env({EVAL_ENABLED_ENV: ""}).evaluation_enabled is True
    assert _configured_from_env({EVAL_ENABLED_ENV: "   "}).evaluation_enabled is True


def test_evaluation_enabled_malformed_is_explicit_error():
    # A typo'd gate must fail at startup, not silently guess on/off.
    with pytest.raises(ValueError, match=EVAL_ENABLED_ENV):
        _configured_from_env({EVAL_ENABLED_ENV: "maybe"})


def test_eval_auto_defaults_to_true():
    # Automatic completion-triggered evaluation is on by default: a default of
    # off would leave the "which tasks get measured" selection with the operator,
    # which is exactly the bias the trigger removes.
    assert _configured_from_env().eval_auto is True
    assert Config(mandrel_path=Path("/root/mandrel")).eval_auto is True


def test_eval_auto_is_independent_of_the_subsystem_gate():
    # Manual-only operation: automatic triggering off, subsystem (and every
    # manual route) untouched.
    cfg = _configured_from_env({EVAL_AUTO_ENV: "false"})
    assert cfg.eval_auto is False and cfg.evaluation_enabled is True


def test_eval_auto_tokens_and_blank():
    for token in ("0", "false", "no", "off", "FALSE", " Off "):
        assert _configured_from_env({EVAL_AUTO_ENV: token}).eval_auto is False
    for token in ("1", "true", "yes", "on", " On "):
        assert _configured_from_env({EVAL_AUTO_ENV: token}).eval_auto is True
    assert _configured_from_env({EVAL_AUTO_ENV: "   "}).eval_auto is True


def test_eval_auto_malformed_is_explicit_error():
    with pytest.raises(ValueError, match=EVAL_AUTO_ENV):
        _configured_from_env({EVAL_AUTO_ENV: "sometimes"})


def test_eval_model_defaults_and_overrides():
    # Always a concrete string so the semantic backend is unconditionally
    # constructible; overridden by ORCH_HUB_EVAL_MODEL.
    assert _configured_from_env().eval_model == DEFAULT_EVAL_MODEL
    assert _configured_from_env({EVAL_MODEL_ENV: "claude-sonnet-5"}).eval_model == "claude-sonnet-5"
    # Empty/whitespace-only counts as unset and falls back to the default.
    assert _configured_from_env({EVAL_MODEL_ENV: "  "}).eval_model == DEFAULT_EVAL_MODEL
    assert _configured_from_env({EVAL_MODEL_ENV: " claude-x "}).eval_model == "claude-x"


def test_eval_effort_defaults_and_overrides():
    # The model's companion knob on the same Layer 2 turn, same unset rules.
    assert _configured_from_env().eval_effort == DEFAULT_EVAL_EFFORT
    assert _configured_from_env({EVAL_EFFORT_ENV: "low"}).eval_effort == "low"
    # Empty/whitespace-only counts as unset and falls back to the default.
    assert _configured_from_env({EVAL_EFFORT_ENV: ""}).eval_effort == DEFAULT_EVAL_EFFORT
    assert _configured_from_env({EVAL_EFFORT_ENV: "   "}).eval_effort == DEFAULT_EVAL_EFFORT
    assert _configured_from_env({EVAL_EFFORT_ENV: " high "}).eval_effort == "high"


def test_eval_default_tier_matches_dev_session_tier():
    # The Layer 2 defaults are a deliberate tier decision (judge task process at
    # the tier that produced it), not incidental constants — pin the literals so a
    # silent downgrade is a test failure rather than a quieter, cheaper evaluation.
    assert (DEFAULT_EVAL_MODEL, DEFAULT_EVAL_EFFORT) == ("claude-opus-5", "max")


def test_eval_retention_defaults_to_bounded_cap():
    # A bounded default so evaluation artifacts cannot grow unbounded, without an
    # env var; also the dataclass default when constructed directly.
    assert _configured_from_env().eval_retention == DEFAULT_EVAL_RETENTION
    assert Config(mandrel_path=Path("/root/mandrel")).eval_retention == (
        DEFAULT_EVAL_RETENTION
    )


def test_eval_retention_override_and_disable():
    # A positive integer is the per-task cap; 0 disables retention (keep all).
    assert _configured_from_env({EVAL_RETENTION_ENV: "5"}).eval_retention == 5
    assert _configured_from_env({EVAL_RETENTION_ENV: " 42 "}).eval_retention == 42
    assert _configured_from_env({EVAL_RETENTION_ENV: "0"}).eval_retention == 0


def test_eval_retention_empty_or_whitespace_counts_as_unset():
    assert _configured_from_env({EVAL_RETENTION_ENV: ""}).eval_retention == (
        DEFAULT_EVAL_RETENTION
    )
    assert _configured_from_env({EVAL_RETENTION_ENV: "   "}).eval_retention == (
        DEFAULT_EVAL_RETENTION
    )


def test_eval_retention_malformed_or_negative_is_explicit_error():
    # A typo must surface at startup rather than silently unbounding or capping.
    with pytest.raises(ValueError, match=EVAL_RETENTION_ENV):
        _configured_from_env({EVAL_RETENTION_ENV: "lots"})
    with pytest.raises(ValueError, match=EVAL_RETENTION_ENV):
        _configured_from_env({EVAL_RETENTION_ENV: "-1"})


# -- automatic Layer 2 dispatch policy ----------------------------------------
# The thresholds are configuration rather than literals so the policy can be
# tuned without a code change; the defaults themselves are pinned because they
# encode a judgement (see the constants' own docstrings) rather than a constant.


def test_dispatch_policy_defaults_are_configuration_with_pinned_values():
    cfg = _configured_from_env()
    assert cfg.eval_usage_ceiling == DEFAULT_EVAL_USAGE_CEILING
    assert cfg.eval_dispatch_spacing == DEFAULT_EVAL_DISPATCH_SPACING
    assert cfg.eval_queue_max_age == DEFAULT_EVAL_QUEUE_MAX_AGE
    # Also the dataclass defaults, so a directly-constructed Config agrees.
    direct = Config(mandrel_path=Path("/root/mandrel"))
    assert direct.eval_usage_ceiling == DEFAULT_EVAL_USAGE_CEILING
    assert direct.eval_dispatch_spacing == DEFAULT_EVAL_DISPATCH_SPACING
    assert direct.eval_queue_max_age == DEFAULT_EVAL_QUEUE_MAX_AGE
    # 80% leaves ~20x one turn's cost in headroom, so measurement never competes
    # with work; 15 minutes trickles a post-rollover backlog instead of bursting
    # it; six hours is one full five-hour window plus slack.
    assert (
        DEFAULT_EVAL_USAGE_CEILING,
        DEFAULT_EVAL_DISPATCH_SPACING,
        DEFAULT_EVAL_QUEUE_MAX_AGE,
    ) == (80.0, 900, 21600)


def test_dispatch_policy_overrides_are_honored():
    assert _configured_from_env(
        {EVAL_USAGE_CEILING_ENV: "55.5"}
    ).eval_usage_ceiling == 55.5
    assert _configured_from_env({EVAL_USAGE_CEILING_ENV: " 0 "}).eval_usage_ceiling == 0.0
    assert _configured_from_env({EVAL_DISPATCH_SPACING_ENV: "60"}).eval_dispatch_spacing == 60
    assert _configured_from_env({EVAL_DISPATCH_SPACING_ENV: "0"}).eval_dispatch_spacing == 0
    assert _configured_from_env({EVAL_QUEUE_MAX_AGE_ENV: "3600"}).eval_queue_max_age == 3600
    assert _configured_from_env({EVAL_QUEUE_MAX_AGE_ENV: "0"}).eval_queue_max_age == 0


@pytest.mark.parametrize(
    "env_name, attr, default",
    [
        (EVAL_USAGE_CEILING_ENV, "eval_usage_ceiling", DEFAULT_EVAL_USAGE_CEILING),
        (EVAL_DISPATCH_SPACING_ENV, "eval_dispatch_spacing", DEFAULT_EVAL_DISPATCH_SPACING),
        (EVAL_QUEUE_MAX_AGE_ENV, "eval_queue_max_age", DEFAULT_EVAL_QUEUE_MAX_AGE),
    ],
)
def test_dispatch_policy_blank_counts_as_unset(env_name, attr, default):
    for blank in ("", "   "):
        assert getattr(_configured_from_env({env_name: blank}), attr) == default


@pytest.mark.parametrize(
    "env_name, bad",
    [
        (EVAL_USAGE_CEILING_ENV, "high"),
        (EVAL_USAGE_CEILING_ENV, "-1"),
        (EVAL_USAGE_CEILING_ENV, "101"),
        (EVAL_DISPATCH_SPACING_ENV, "quarter-hour"),
        (EVAL_DISPATCH_SPACING_ENV, "-1"),
        (EVAL_QUEUE_MAX_AGE_ENV, "forever"),
        (EVAL_QUEUE_MAX_AGE_ENV, "-60"),
    ],
)
def test_dispatch_policy_malformed_value_is_an_explicit_error(env_name, bad):
    """A mistyped threshold is a policy nobody chose; it must not start the hub."""
    with pytest.raises(ValueError, match=env_name):
        _configured_from_env({env_name: bad})


# -- Layer 2 turn deadline -----------------------------------------------------


def test_turn_timeout_default_is_configuration_with_a_pinned_value():
    cfg = _configured_from_env()
    assert cfg.eval_turn_timeout == DEFAULT_EVAL_TURN_TIMEOUT
    assert Config(mandrel_path=Path("/root/mandrel")).eval_turn_timeout == (
        DEFAULT_EVAL_TURN_TIMEOUT
    )
    # 30 minutes: unreachable by a real turn (observed turns run minutes), so the
    # deadline only ever catches a turn that is not progressing at all.
    assert DEFAULT_EVAL_TURN_TIMEOUT == 1800


def test_turn_timeout_overrides_and_disable_token():
    assert _configured_from_env({EVAL_TURN_TIMEOUT_ENV: "60"}).eval_turn_timeout == 60
    # 0 removes the deadline — and with it the in-flight stuck sweep derived from it.
    assert _configured_from_env({EVAL_TURN_TIMEOUT_ENV: "0"}).eval_turn_timeout == 0
    for blank in ("", "   "):
        assert _configured_from_env(
            {EVAL_TURN_TIMEOUT_ENV: blank}
        ).eval_turn_timeout == DEFAULT_EVAL_TURN_TIMEOUT


@pytest.mark.parametrize("bad", ["half-hour", "-1"])
def test_turn_timeout_malformed_value_is_an_explicit_error(bad):
    with pytest.raises(ValueError, match=EVAL_TURN_TIMEOUT_ENV):
        _configured_from_env({EVAL_TURN_TIMEOUT_ENV: bad})
