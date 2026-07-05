"""Tests for config defaults and environment overrides.

Passes explicit mappings to ``Config.from_env`` so the process environment is
never mutated.
"""

from __future__ import annotations

from pathlib import Path

from orch_hub.config import AI_NATIVE_PATH_ENV, DEFAULT_AI_NATIVE_PATH, Config


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


def test_derived_paths():
    cfg = Config(ai_native_path=Path("/root/ai-native"))
    assert cfg.aii2_bin == Path("/root/ai-native/aii-2")
    assert cfg.registry_file == Path("/root/ai-native/.registry/repos.local.json")
