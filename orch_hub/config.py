"""Configuration defaults and environment overrides for orch-hub."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AI_NATIVE_PATH = Path(
    "/Users/linqing/workplace/github.com/qinglin89/ai-native-development"
)
"""Default ai-native-development checkout that owns the managed-repo registry."""

AI_NATIVE_PATH_ENV = "ORCH_HUB_AI_NATIVE_DEPLOYMENT"
"""Environment variable overriding the ai-native-development path."""

TOKEN_ENV = "ORCH_HUB_TOKEN"
"""Environment variable holding the v1 bearer token for the HTTP service."""


@dataclass(frozen=True)
class Config:
    """Resolved orch-hub settings shared by the CLI and API routes."""

    ai_native_path: Path
    token: str | None = None
    """Bearer token required by the HTTP service.

    ``None`` means unconfigured: protected routes fail closed (503) rather
    than serving unauthenticated. There is no value that disables auth.
    """

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        """Build config from the environment, honoring the overrides.

        An empty or whitespace-only token counts as unset, so a stray
        ``export ORCH_HUB_TOKEN=""`` cannot make the empty string a valid
        credential; surrounding whitespace is stripped for the same reason.
        """
        env = os.environ if environ is None else environ
        raw = env.get(AI_NATIVE_PATH_ENV)
        path = Path(raw).expanduser() if raw else DEFAULT_AI_NATIVE_PATH
        raw_token = env.get(TOKEN_ENV)
        token = raw_token.strip() if raw_token else None
        return cls(ai_native_path=path, token=token or None)

    @property
    def aii2_bin(self) -> Path:
        """Absolute path to the aii-2 executable inside ai-native-development.

        orch-hub invokes aii-2 by absolute path rather than relying on it being
        on PATH, since it ships as a repo-local script, not a system install.
        """
        return self.ai_native_path / "aii-2"

    @property
    def registry_file(self) -> Path:
        """Fallback registry file used when the aii-2 command is unavailable."""
        return self.ai_native_path / ".registry" / "repos.local.json"
