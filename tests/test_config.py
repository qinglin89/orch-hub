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
    DATA_DIR_ENV,
    DEFAULT_AI_NATIVE_PATH,
    DEFAULT_DATA_DIR,
    DEFAULT_HOST,
    DEFAULT_PORT,
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


def test_bark_url_defaults_to_none():
    assert Config.from_env({}).bark_url is None
    assert Config(ai_native_path=Path("/root/ai-native")).bark_url is None


def test_bark_url_from_env_strips_whitespace():
    cfg = Config.from_env({BARK_URL_ENV: " https://bark.example/KEY \n"})
    assert cfg.bark_url == "https://bark.example/KEY"


def test_bark_url_empty_or_whitespace_counts_as_unset():
    assert Config.from_env({BARK_URL_ENV: ""}).bark_url is None
    assert Config.from_env({BARK_URL_ENV: "   "}).bark_url is None
