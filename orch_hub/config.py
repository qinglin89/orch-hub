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

DEFAULT_HOST = "127.0.0.1"
"""Default HTTP bind address: loopback only; see the deployment docs."""

DEFAULT_PORT = 8765
"""Default HTTP port."""

DEFAULT_DATA_DIR = Path.home() / ".orch-hub"
"""Default data directory: run dirs live under ``<data_dir>/runs/``."""

AI_NATIVE_PATH_ENV = "ORCH_HUB_AI_NATIVE_DEPLOYMENT"
"""Environment variable overriding the ai-native-development path."""

TOKEN_ENV = "ORCH_HUB_TOKEN"
"""Environment variable holding the v1 bearer token for the HTTP service."""

HOST_ENV = "ORCH_HUB_HOST"
"""Environment variable overriding the HTTP bind address."""

PORT_ENV = "ORCH_HUB_PORT"
"""Environment variable overriding the HTTP port."""

DATA_DIR_ENV = "ORCH_HUB_DATA_DIR"
"""Environment variable overriding the data directory."""


def _parse_port(raw: str | None) -> int:
    """Parse ``PORT_ENV``: unset/blank means the default; malformed is an
    explicit error rather than a silent fallback, so a typo in the service
    environment surfaces at startup instead of binding the wrong port."""
    value = (raw or "").strip()
    if not value:
        return DEFAULT_PORT
    try:
        port = int(value)
    except ValueError:
        raise ValueError(f"{PORT_ENV} must be an integer, got {value!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"{PORT_ENV} must be in 1-65535, got {port}")
    return port


@dataclass(frozen=True)
class Config:
    """Resolved orch-hub settings shared by the CLI and API routes."""

    ai_native_path: Path
    token: str | None = None
    """Bearer token required by the HTTP service.

    ``None`` means unconfigured: protected routes fail closed (503) rather
    than serving unauthenticated. There is no value that disables auth.
    """

    host: str = DEFAULT_HOST
    """HTTP bind address.

    Loopback by default; any non-loopback bind is only meant for private,
    Tailscale-reachable interfaces — never a public one.
    """

    port: int = DEFAULT_PORT
    """HTTP port."""

    data_dir: Path = DEFAULT_DATA_DIR
    """Directory holding hub-owned run state (and future audit history).

    The run manager creates one ``<data_dir>/runs/<run_id>/`` dir per
    orchestrator run — deliberately outside every target repo working tree,
    since an in-repo control dir would dirty the tree and fail the
    orchestrator's post-checks.
    """

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        """Build config from the environment, honoring the overrides.

        An empty or whitespace-only value counts as unset for every variable,
        so a stray ``export ORCH_HUB_TOKEN=""`` cannot make the empty string a
        valid credential; surrounding whitespace is stripped for the same
        reason. A malformed ``ORCH_HUB_PORT`` raises instead of silently
        falling back to the default port.
        """
        env = os.environ if environ is None else environ
        raw_path = (env.get(AI_NATIVE_PATH_ENV) or "").strip()
        path = Path(raw_path).expanduser() if raw_path else DEFAULT_AI_NATIVE_PATH
        raw_token = env.get(TOKEN_ENV)
        token = raw_token.strip() if raw_token else None
        raw_data_dir = (env.get(DATA_DIR_ENV) or "").strip()
        return cls(
            ai_native_path=path,
            token=token or None,
            host=(env.get(HOST_ENV) or "").strip() or DEFAULT_HOST,
            port=_parse_port(env.get(PORT_ENV)),
            data_dir=Path(raw_data_dir).expanduser() if raw_data_dir else DEFAULT_DATA_DIR,
        )

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
