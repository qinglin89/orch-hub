"""Tests for config defaults and environment overrides.

Passes explicit mappings to ``Config.from_env`` so the process environment is
never mutated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orch_hub.config import (
    AI_NATIVE_PATH_ENV,
    BARK_URL_ENV,
    CODEX_HOME_ENV,
    DATA_DIR_ENV,
    DEFAULT_AI_NATIVE_PATH,
    DEFAULT_CODEX_HOME,
    DEFAULT_DATA_DIR,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_EVAL_EFFORT,
    DEFAULT_EVAL_MODEL,
    DEFAULT_EVAL_RETENTION,
    EVAL_EFFORT_ENV,
    EVAL_ENABLED_ENV,
    EVAL_MODEL_ENV,
    EVAL_RETENTION_ENV,
    HOST_ENV,
    PORT_ENV,
    TOKEN_ENV,
    Config,
)


def test_default_path_when_env_absent():
    cfg = Config.from_env({})
    assert cfg.ai_native_path == DEFAULT_AI_NATIVE_PATH


def test_env_override():
    cfg = Config.from_env({AI_NATIVE_PATH_ENV: "/custom/ai-native"})
    assert cfg.ai_native_path == Path("/custom/ai-native")


def test_env_override_expands_user():
    cfg = Config.from_env({AI_NATIVE_PATH_ENV: "~/ai-native"})
    assert cfg.ai_native_path == Path("~/ai-native").expanduser()
    assert not str(cfg.ai_native_path).startswith("~")


def test_ai_native_path_strips_surrounding_whitespace():
    cfg = Config.from_env({AI_NATIVE_PATH_ENV: " /custom/ai-native \n"})
    assert cfg.ai_native_path == Path("/custom/ai-native")


def test_ai_native_path_empty_or_whitespace_counts_as_unset():
    assert Config.from_env({AI_NATIVE_PATH_ENV: ""}).ai_native_path == (
        DEFAULT_AI_NATIVE_PATH
    )
    assert Config.from_env({AI_NATIVE_PATH_ENV: "   "}).ai_native_path == (
        DEFAULT_AI_NATIVE_PATH
    )


def test_derived_paths():
    cfg = Config(ai_native_path=Path("/root/ai-native"))
    assert cfg.aii2_bin == Path("/root/ai-native/aii-2")
    assert cfg.registry_file == Path("/root/ai-native/.registry/repos.local.json")


def test_token_defaults_to_none():
    assert Config.from_env({}).token is None
    assert Config(ai_native_path=Path("/root/ai-native")).token is None


def test_token_from_env():
    cfg = Config.from_env({TOKEN_ENV: "s3cret-token"})
    assert cfg.token == "s3cret-token"


def test_token_strips_surrounding_whitespace():
    cfg = Config.from_env({TOKEN_ENV: " s3cret-token\n"})
    assert cfg.token == "s3cret-token"


def test_token_empty_or_whitespace_counts_as_unset():
    assert Config.from_env({TOKEN_ENV: ""}).token is None
    assert Config.from_env({TOKEN_ENV: "   "}).token is None


def test_service_settings_default_to_loopback():
    cfg = Config.from_env({})
    assert cfg.host == DEFAULT_HOST == "127.0.0.1"
    assert cfg.port == DEFAULT_PORT == 8765
    assert cfg.data_dir == DEFAULT_DATA_DIR


def test_host_from_env_strips_whitespace():
    assert Config.from_env({HOST_ENV: " 100.64.0.7 "}).host == "100.64.0.7"


def test_host_empty_or_whitespace_counts_as_unset():
    assert Config.from_env({HOST_ENV: ""}).host == DEFAULT_HOST
    assert Config.from_env({HOST_ENV: "   "}).host == DEFAULT_HOST


def test_port_from_env():
    assert Config.from_env({PORT_ENV: "9000"}).port == 9000
    assert Config.from_env({PORT_ENV: " 9000 "}).port == 9000


def test_port_empty_or_whitespace_counts_as_unset():
    assert Config.from_env({PORT_ENV: ""}).port == DEFAULT_PORT
    assert Config.from_env({PORT_ENV: "   "}).port == DEFAULT_PORT


def test_port_malformed_is_explicit_error():
    # A typo'd port must fail at startup, not silently bind the default.
    with pytest.raises(ValueError, match=PORT_ENV):
        Config.from_env({PORT_ENV: "eight"})


def test_port_out_of_range_is_explicit_error():
    with pytest.raises(ValueError, match=PORT_ENV):
        Config.from_env({PORT_ENV: "0"})
    with pytest.raises(ValueError, match=PORT_ENV):
        Config.from_env({PORT_ENV: "65536"})


def test_data_dir_from_env_expands_user():
    cfg = Config.from_env({DATA_DIR_ENV: "~/orch-data"})
    assert cfg.data_dir == Path("~/orch-data").expanduser()
    assert not str(cfg.data_dir).startswith("~")


def test_data_dir_empty_or_whitespace_counts_as_unset():
    assert Config.from_env({DATA_DIR_ENV: ""}).data_dir == DEFAULT_DATA_DIR
    assert Config.from_env({DATA_DIR_ENV: "   "}).data_dir == DEFAULT_DATA_DIR


def test_codex_home_defaults_when_env_absent():
    assert Config.from_env({}).codex_home == DEFAULT_CODEX_HOME


def test_codex_home_from_env_expands_user():
    cfg = Config.from_env({CODEX_HOME_ENV: "~/alt-codex"})
    assert cfg.codex_home == Path("~/alt-codex").expanduser()
    assert not str(cfg.codex_home).startswith("~")


def test_codex_home_empty_or_whitespace_counts_as_unset():
    assert Config.from_env({CODEX_HOME_ENV: ""}).codex_home == DEFAULT_CODEX_HOME
    assert Config.from_env({CODEX_HOME_ENV: "   "}).codex_home == DEFAULT_CODEX_HOME


def test_bark_url_defaults_to_none():
    assert Config.from_env({}).bark_url is None
    assert Config(ai_native_path=Path("/root/ai-native")).bark_url is None


def test_bark_url_from_env_strips_whitespace():
    cfg = Config.from_env({BARK_URL_ENV: " https://bark.example/KEY \n"})
    assert cfg.bark_url == "https://bark.example/KEY"


def test_bark_url_empty_or_whitespace_counts_as_unset():
    assert Config.from_env({BARK_URL_ENV: ""}).bark_url is None
    assert Config.from_env({BARK_URL_ENV: "   "}).bark_url is None


def test_evaluation_enabled_defaults_to_true():
    # Enabled by default for local use; a deployment disables it explicitly.
    assert Config.from_env({}).evaluation_enabled is True
    assert Config(ai_native_path=Path("/root/ai-native")).evaluation_enabled is True


def test_evaluation_enabled_falsy_tokens_disable():
    for token in ("0", "false", "no", "off", "FALSE", " Off ", "No\n"):
        assert Config.from_env({EVAL_ENABLED_ENV: token}).evaluation_enabled is False


def test_evaluation_enabled_truthy_tokens_enable():
    for token in ("1", "true", "yes", "on", "TRUE", " On ", "Yes\n"):
        assert Config.from_env({EVAL_ENABLED_ENV: token}).evaluation_enabled is True


def test_evaluation_enabled_empty_or_whitespace_counts_as_unset():
    assert Config.from_env({EVAL_ENABLED_ENV: ""}).evaluation_enabled is True
    assert Config.from_env({EVAL_ENABLED_ENV: "   "}).evaluation_enabled is True


def test_evaluation_enabled_malformed_is_explicit_error():
    # A typo'd gate must fail at startup, not silently guess on/off.
    with pytest.raises(ValueError, match=EVAL_ENABLED_ENV):
        Config.from_env({EVAL_ENABLED_ENV: "maybe"})


def test_eval_model_defaults_and_overrides():
    # Always a concrete string so the semantic backend is unconditionally
    # constructible; overridden by ORCH_HUB_EVAL_MODEL.
    assert Config.from_env({}).eval_model == DEFAULT_EVAL_MODEL
    assert Config.from_env({EVAL_MODEL_ENV: "claude-sonnet-5"}).eval_model == "claude-sonnet-5"
    # Empty/whitespace-only counts as unset and falls back to the default.
    assert Config.from_env({EVAL_MODEL_ENV: "  "}).eval_model == DEFAULT_EVAL_MODEL
    assert Config.from_env({EVAL_MODEL_ENV: " claude-x "}).eval_model == "claude-x"


def test_eval_effort_defaults_and_overrides():
    # The model's companion knob on the same Layer 2 turn, same unset rules.
    assert Config.from_env({}).eval_effort == DEFAULT_EVAL_EFFORT
    assert Config.from_env({EVAL_EFFORT_ENV: "low"}).eval_effort == "low"
    # Empty/whitespace-only counts as unset and falls back to the default.
    assert Config.from_env({EVAL_EFFORT_ENV: ""}).eval_effort == DEFAULT_EVAL_EFFORT
    assert Config.from_env({EVAL_EFFORT_ENV: "   "}).eval_effort == DEFAULT_EVAL_EFFORT
    assert Config.from_env({EVAL_EFFORT_ENV: " high "}).eval_effort == "high"


def test_eval_default_tier_matches_dev_session_tier():
    # The Layer 2 defaults are a deliberate tier decision (judge task process at
    # the tier that produced it), not incidental constants — pin the literals so a
    # silent downgrade is a test failure rather than a quieter, cheaper evaluation.
    assert (DEFAULT_EVAL_MODEL, DEFAULT_EVAL_EFFORT) == ("claude-opus-5", "max")


def test_eval_retention_defaults_to_bounded_cap():
    # A bounded default so evaluation artifacts cannot grow unbounded, without an
    # env var; also the dataclass default when constructed directly.
    assert Config.from_env({}).eval_retention == DEFAULT_EVAL_RETENTION
    assert Config(ai_native_path=Path("/root/ai-native")).eval_retention == (
        DEFAULT_EVAL_RETENTION
    )


def test_eval_retention_override_and_disable():
    # A positive integer is the per-task cap; 0 disables retention (keep all).
    assert Config.from_env({EVAL_RETENTION_ENV: "5"}).eval_retention == 5
    assert Config.from_env({EVAL_RETENTION_ENV: " 42 "}).eval_retention == 42
    assert Config.from_env({EVAL_RETENTION_ENV: "0"}).eval_retention == 0


def test_eval_retention_empty_or_whitespace_counts_as_unset():
    assert Config.from_env({EVAL_RETENTION_ENV: ""}).eval_retention == (
        DEFAULT_EVAL_RETENTION
    )
    assert Config.from_env({EVAL_RETENTION_ENV: "   "}).eval_retention == (
        DEFAULT_EVAL_RETENTION
    )


def test_eval_retention_malformed_or_negative_is_explicit_error():
    # A typo must surface at startup rather than silently unbounding or capping.
    with pytest.raises(ValueError, match=EVAL_RETENTION_ENV):
        Config.from_env({EVAL_RETENTION_ENV: "lots"})
    with pytest.raises(ValueError, match=EVAL_RETENTION_ENV):
        Config.from_env({EVAL_RETENTION_ENV: "-1"})
