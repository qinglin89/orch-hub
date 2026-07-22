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

BARK_URL_ENV = "ORCH_HUB_BARK_URL"
"""Environment variable holding the Bark push endpoint (server + device key).

The value is a full URL such as ``https://api.day.app/<device-key>``. It embeds
a device key, so treat it as a secret and never log it. Unset (or blank)
disables push notifications entirely."""

EVAL_ENABLED_ENV = "ORCH_HUB_EVAL_ENABLED"
"""Environment variable gating the optional task-evaluation subsystem.

Task evaluation (read-only evidence packs, later static/semantic metrics) is a
bolt-on kept separable from run orchestration so it can be omitted from customer
deployments. It defaults to *enabled* for local use; setting this to a falsy
token (``0``/``false``/``no``/``off``) disables it, so evaluation services fail
closed and the future evaluation API/dashboard surfaces are removed/404. Unset
or blank means the default; an unrecognized value fails loudly (like a malformed
``ORCH_HUB_PORT``) rather than silently guessing on/off."""

DEFAULT_EVAL_ENABLED = True
"""Task evaluation is enabled by default (local use); disable it explicitly."""

_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "off"})


def _parse_bool(raw: str | None, *, default: bool, env_name: str) -> bool:
    """Parse a boolean env var: unset/blank is ``default``, malformed fails loud.

    Recognizes ``1``/``true``/``yes``/``on`` and ``0``/``false``/``no``/``off``
    case-insensitively. An unrecognized explicit value raises ``ValueError``
    instead of silently falling back, mirroring :func:`_parse_port`, so a typo in
    the service environment surfaces at startup rather than quietly picking a
    gate state.
    """
    value = (raw or "").strip().lower()
    if not value:
        return default
    if value in _TRUE_TOKENS:
        return True
    if value in _FALSE_TOKENS:
        return False
    raise ValueError(
        f"{env_name} must be a boolean"
        f" (one of {', '.join(sorted(_TRUE_TOKENS | _FALSE_TOKENS))}), got {raw!r}"
    )


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

    bark_url: str | None = None
    """Bark push endpoint (``ORCH_HUB_BARK_URL``), or ``None`` when unset.

    Full URL including the device key (a secret — never logged). ``None`` means
    push notifications are disabled; there is no separate on/off flag.
    """

    evaluation_enabled: bool = DEFAULT_EVAL_ENABLED
    """Whether the optional task-evaluation subsystem is enabled.

    Gates the read-only evidence-pack service (and its later static/semantic
    metrics and API/dashboard surfaces). ``True`` by default for local use; a
    deployment that omits evaluation sets ``ORCH_HUB_EVAL_ENABLED`` falsy so the
    evaluation services fail closed and the routes are removed/404. It gates a
    *feature*, unlike ``token``/``bark_url`` whose ``None`` encodes both absence
    and disable — hence a real boolean, not a nullable value.
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
        raw_bark_url = env.get(BARK_URL_ENV)
        bark_url = raw_bark_url.strip() if raw_bark_url else None
        return cls(
            ai_native_path=path,
            token=token or None,
            host=(env.get(HOST_ENV) or "").strip() or DEFAULT_HOST,
            port=_parse_port(env.get(PORT_ENV)),
            data_dir=Path(raw_data_dir).expanduser() if raw_data_dir else DEFAULT_DATA_DIR,
            bark_url=bark_url or None,
            evaluation_enabled=_parse_bool(
                env.get(EVAL_ENABLED_ENV),
                default=DEFAULT_EVAL_ENABLED,
                env_name=EVAL_ENABLED_ENV,
            ),
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
