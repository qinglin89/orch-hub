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

DEFAULT_CODEX_HOME = Path.home() / ".codex"
"""Default Codex CLI home. The agent-subscription-usage feature reads Codex's
own local rate-limit signal from its session rollouts under ``<codex_home>/
sessions/`` — a read-only, no-network, no-credit-consuming source. Overridable so
a non-default ``CODEX_HOME`` (or a test fixture dir) can be pointed at instead."""

DEFAULT_CLAUDE_HOME = Path.home() / ".claude"
"""Default Claude Code home. The task-evaluation evidence layer reads each dev
session's own conversation transcript from ``<claude_home>/projects/`` to derive
a bounded read-behavior rollup — read-only, and the transcript itself never
enters an artifact. Overridable so a relocated Claude home (or a test fixture
dir) can be pointed at instead."""

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

CODEX_HOME_ENV = "ORCH_HUB_CODEX_HOME"
"""Environment variable overriding the Codex CLI home (default ``~/.codex``).

Points the agent-subscription-usage Codex adapter at the Codex home whose
``sessions/`` rollouts carry the local rate-limit signal. Unset (or blank) means
:data:`DEFAULT_CODEX_HOME`."""

CLAUDE_HOME_ENV = "ORCH_HUB_CLAUDE_HOME"
"""Environment variable overriding the Claude Code home (default ``~/.claude``).

Points the evaluation read-behavior reader at the home whose ``projects/`` tree
holds per-session conversation transcripts. Unset (or blank) means
:data:`DEFAULT_CLAUDE_HOME`."""

BARK_URL_ENV = "ORCH_HUB_BARK_URL"
"""Environment variable holding the Bark push endpoint (server + device key).

The value is a full URL such as ``https://api.day.app/<device-key>``. It embeds
a device key, so treat it as a secret and never log it. Unset (or blank)
disables push notifications entirely."""

INTAKE_MODEL_ENV = "ORCH_HUB_INTAKE_MODEL"
"""Environment variable selecting the model for remote-intake agent turns.

Remote intake drives a Claude CLI ``/intake-task`` session; all three turn kinds
(draft, revise, confirm) share one model. Unset or blank means
:data:`DEFAULT_INTAKE_MODEL`. Independent of the evaluation knobs — intake is not
behind ``ORCH_HUB_EVAL_ENABLED``."""

DEFAULT_INTAKE_MODEL = "claude-opus-5"
"""Default remote-intake model: the same tier the orchestrator runs dev sessions
at. An intake turn decides a task's scope, decomposition, ``session-est``, and
``prefetch`` — judgments every later dev session inherits and pays for — so the
leverage is dev-session grade rather than drafting grade; override with
``ORCH_HUB_INTAKE_MODEL``."""

INTAKE_EFFORT_ENV = "ORCH_HUB_INTAKE_EFFORT"
"""Environment variable selecting the reasoning effort for remote-intake turns.

The companion of :data:`INTAKE_MODEL_ENV` on the same turns. Unset or blank means
:data:`DEFAULT_INTAKE_EFFORT`. Values are whatever the installed ``claude`` CLI
accepts for ``--effort`` — not validated here, so an unusable value surfaces as a
clean failed intake rather than a startup error (an effort typo must not take the
whole service down)."""

DEFAULT_INTAKE_EFFORT = "max"
"""Default reasoning effort for intake turns, matching
:data:`DEFAULT_INTAKE_MODEL`'s tier: the same depth the dev sessions that will
consume the task run at; override with ``ORCH_HUB_INTAKE_EFFORT``."""

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

EVAL_MODEL_ENV = "ORCH_HUB_EVAL_MODEL"
"""Environment variable selecting the model for the semantic task evaluator.

The optional semantic evaluator (Layer 2 of the evaluation subsystem) drives a
Claude CLI backend; this picks which model judges task quality. Unset or blank
means :data:`DEFAULT_EVAL_MODEL`. It only matters when evaluation is enabled and
a semantic evaluation is requested — the deterministic Layer 1 static metrics
make no model call."""

DEFAULT_EVAL_MODEL = "claude-opus-5"
"""Default semantic-evaluator model: the same tier the orchestrator runs dev
sessions at. Judging one task's process quality is the review side of the work
those sessions produced, so it gets the same reasoning tier rather than a
cheaper one; override with ``ORCH_HUB_EVAL_MODEL``."""

EVAL_EFFORT_ENV = "ORCH_HUB_EVAL_EFFORT"
"""Environment variable selecting the reasoning effort for the semantic task
evaluator's Claude CLI turn.

The model-side companion of :data:`EVAL_MODEL_ENV`: same turn, same gate. Unset
or blank means :data:`DEFAULT_EVAL_EFFORT`. Values are whatever the installed
``claude`` CLI accepts for ``--effort`` — not validated here, so an unusable
value surfaces as a clean failed semantic job rather than a startup error (an
effort typo must not take the whole service down)."""

DEFAULT_EVAL_EFFORT = "max"
"""Default reasoning effort for the semantic evaluation turn, matching
:data:`DEFAULT_EVAL_MODEL`'s tier. The judgment is a bounded one-shot analysis
over persisted evidence with no tool loop to recover from a shallow read, so
depth is bought once here; override with ``ORCH_HUB_EVAL_EFFORT``."""

EVAL_RETENTION_ENV = "ORCH_HUB_EVAL_RETENTION"
"""Environment variable bounding how many evaluations are kept per task.

Evaluation artifacts (evidence/static/semantic packs) accumulate one dir per
generation under ``<data_dir>/evaluations/<repo>/<task>/``. This caps how many of
the newest evaluations are retained per task, so the sidecar tree cannot grow
unbounded while enough history is kept for internal trend analysis. Unset or
blank means :data:`DEFAULT_EVAL_RETENTION`; an explicit ``0`` disables retention
(keep everything); a positive integer is the cap. A negative or non-integer value
fails loudly (like a malformed ``ORCH_HUB_PORT``) rather than silently guessing."""

DEFAULT_EVAL_RETENTION = 20
"""Default per-task evaluation retention cap: keep the 20 newest evaluations and
prune older ones on the next generation. Generous enough for trend analysis over
a task's evaluation history, bounded enough to avoid unbounded growth."""

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


def _parse_retention(raw: str | None) -> int:
    """Parse ``EVAL_RETENTION_ENV``: unset/blank means the default; a non-negative
    integer is honored (``0`` disables retention); a negative or non-integer value
    is an explicit error rather than a silent fallback, so a typo surfaces at
    startup instead of quietly unbounding or capping evaluation history."""
    value = (raw or "").strip()
    if not value:
        return DEFAULT_EVAL_RETENTION
    try:
        retention = int(value)
    except ValueError:
        raise ValueError(
            f"{EVAL_RETENTION_ENV} must be a non-negative integer, got {value!r}"
        ) from None
    if retention < 0:
        raise ValueError(
            f"{EVAL_RETENTION_ENV} must be non-negative (0 disables), got {retention}"
        )
    return retention


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

    codex_home: Path = DEFAULT_CODEX_HOME
    """Codex CLI home (``ORCH_HUB_CODEX_HOME``), default ``~/.codex``.

    The agent-subscription-usage Codex adapter reads Codex's local rate-limit
    signal from ``<codex_home>/sessions/`` rollouts — read-only, no network, and
    never the credit-spend path. Display-only; never gates any operation.
    """

    claude_home: Path = DEFAULT_CLAUDE_HOME
    """Claude Code home (``ORCH_HUB_CLAUDE_HOME``), default ``~/.claude``.

    The evaluation evidence layer reads per-session conversation transcripts
    from ``<claude_home>/projects/`` (see :attr:`claude_transcripts_dir`) to
    derive a bounded read-behavior rollup. Read-only, and only ever a source of
    *derived* facts — no transcript text reaches an artifact. Distinct from the
    usage layer's ``~/.claude.json`` cache, which is a different file.
    """

    bark_url: str | None = None
    """Bark push endpoint (``ORCH_HUB_BARK_URL``), or ``None`` when unset.

    Full URL including the device key (a secret — never logged). ``None`` means
    push notifications are disabled; there is no separate on/off flag.
    """

    intake_model: str = DEFAULT_INTAKE_MODEL
    """Model id for the remote-intake agent's Claude CLI turns.

    Selected by ``ORCH_HUB_INTAKE_MODEL``; defaults to
    :data:`DEFAULT_INTAKE_MODEL`. Always a concrete string (never ``None``) so
    the agent can be constructed unconditionally at app startup — it only
    reaches a subprocess when an intake turn actually runs.
    """

    intake_effort: str = DEFAULT_INTAKE_EFFORT
    """Reasoning effort for the remote-intake agent's Claude CLI turns.

    Selected by ``ORCH_HUB_INTAKE_EFFORT``; defaults to
    :data:`DEFAULT_INTAKE_EFFORT`. Not validated against the CLI's accepted
    values — an unusable effort fails that one intake turn (recorded as a
    ``failed`` intake), not service startup.
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

    eval_model: str = DEFAULT_EVAL_MODEL
    """Model id for the semantic task evaluator's Claude CLI backend.

    Selected by ``ORCH_HUB_EVAL_MODEL``; defaults to :data:`DEFAULT_EVAL_MODEL`.
    Always a concrete string (never ``None``) so the backend can be constructed
    unconditionally — it only reaches a subprocess when a semantic evaluation is
    actually requested while evaluation is enabled.
    """

    eval_effort: str = DEFAULT_EVAL_EFFORT
    """Reasoning effort for the semantic task evaluator's Claude CLI turn.

    Selected by ``ORCH_HUB_EVAL_EFFORT``; defaults to :data:`DEFAULT_EVAL_EFFORT`.
    Always a concrete string, for the same reason as ``eval_model``: the backend
    is constructed unconditionally and only reaches a subprocess when a semantic
    evaluation actually runs. Not validated against the CLI's accepted values —
    an unusable effort fails that one evaluation, not service startup.
    """

    eval_retention: int = DEFAULT_EVAL_RETENTION
    """Per-task cap on retained evaluations (``ORCH_HUB_EVAL_RETENTION``).

    The evaluation subsystem keeps the ``eval_retention`` newest evaluations per
    task and prunes older ones on the next generation, so ``<data_dir>/evaluations``
    stays bounded. ``0`` disables retention (keep everything); a positive integer
    is the cap. Only meaningful when evaluation is enabled.
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
        raw_codex_home = (env.get(CODEX_HOME_ENV) or "").strip()
        raw_claude_home = (env.get(CLAUDE_HOME_ENV) or "").strip()
        raw_bark_url = env.get(BARK_URL_ENV)
        bark_url = raw_bark_url.strip() if raw_bark_url else None
        return cls(
            ai_native_path=path,
            token=token or None,
            host=(env.get(HOST_ENV) or "").strip() or DEFAULT_HOST,
            port=_parse_port(env.get(PORT_ENV)),
            data_dir=Path(raw_data_dir).expanduser() if raw_data_dir else DEFAULT_DATA_DIR,
            codex_home=(
                Path(raw_codex_home).expanduser() if raw_codex_home else DEFAULT_CODEX_HOME
            ),
            claude_home=(
                Path(raw_claude_home).expanduser()
                if raw_claude_home
                else DEFAULT_CLAUDE_HOME
            ),
            bark_url=bark_url or None,
            intake_model=(
                (env.get(INTAKE_MODEL_ENV) or "").strip() or DEFAULT_INTAKE_MODEL
            ),
            intake_effort=(
                (env.get(INTAKE_EFFORT_ENV) or "").strip() or DEFAULT_INTAKE_EFFORT
            ),
            evaluation_enabled=_parse_bool(
                env.get(EVAL_ENABLED_ENV),
                default=DEFAULT_EVAL_ENABLED,
                env_name=EVAL_ENABLED_ENV,
            ),
            eval_model=(env.get(EVAL_MODEL_ENV) or "").strip() or DEFAULT_EVAL_MODEL,
            eval_effort=(env.get(EVAL_EFFORT_ENV) or "").strip() or DEFAULT_EVAL_EFFORT,
            eval_retention=_parse_retention(env.get(EVAL_RETENTION_ENV)),
        )

    @property
    def aii2_bin(self) -> Path:
        """Absolute path to the aii-2 executable inside ai-native-development.

        orch-hub invokes aii-2 by absolute path rather than relying on it being
        on PATH, since it ships as a repo-local script, not a system install.
        """
        return self.ai_native_path / "aii-2"

    @property
    def claude_transcripts_dir(self) -> Path:
        """Root of Claude Code's per-project conversation transcript store.

        ``<claude_home>/projects/<slugged-cwd>/<session-id>.jsonl`` is the
        CLI's own layout; the evaluation read-behavior reader resolves a session
        id to its transcript there. Derived rather than configured separately so
        one home override moves both this and any future Claude-local source.
        """
        return self.claude_home / "projects"

    @property
    def registry_file(self) -> Path:
        """Fallback registry file used when the aii-2 command is unavailable."""
        return self.ai_native_path / ".registry" / "repos.local.json"
