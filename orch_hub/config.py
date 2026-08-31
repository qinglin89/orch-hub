"""Configuration defaults and environment overrides for orch-hub."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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

MANDREL_PATH_ENV = "ORCH_HUB_MANDREL_PATH"
"""Environment variable naming the Mandrel checkout path."""

LEGACY_MANDREL_PATH_ENV = "ORCH_HUB_AI_NATIVE_DEPLOYMENT"

MANDREL_BIN_RELATIVE_PATH = Path("bin") / "mandrel"
"""Where the mandrel executable sits inside its checkout.

Named once because two callers need it for different reasons: the registry
adapter runs it, and first-time setup uses its presence to tell a real mandrel
checkout from a path the operator merely typed."""
"""Pre-rename spelling of :data:`MANDREL_PATH_ENV`, still read as a fallback.

The launchd plist installed before the rename carries the old key, and only
``orch-hub launchd update`` rewrites it — so dropping the name outright would
make a resident service lose its checkout configuration on the first package
upgrade. :data:`MANDREL_PATH_ENV` wins when both are set (a blank value counts
as unset either way). Remove once every install has been updated.
"""

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

EVAL_AUTO_ENV = "ORCH_HUB_EVAL_AUTO"
"""Environment variable gating *automatic* evaluation of completed tasks.

The trigger gate, orthogonal to :data:`EVAL_ENABLED_ENV` (the subsystem gate):
with it on, a run whose end classifies as ``completed`` evaluates its task
without operator action, so the evaluated corpus is mechanical rather than a
product of which tasks felt worth measuring. Turning it off leaves the whole
evaluation subsystem — routes, dashboard controls, manual generation — intact and
returns it to on-demand-only operation; turning the *subsystem* off disables this
too, since there is nothing to trigger. Falsy tokens disable, unset/blank means
the default, an unrecognized value fails loudly."""

DEFAULT_EVAL_AUTO = True
"""Automatic completion-triggered evaluation is on by default.

The point of the trigger is that nobody decides which completed tasks get a
report; a default of *off* would leave that selection with the operator, which is
the bias the trigger exists to remove. Opting out is explicit and per-deployment
(``ORCH_HUB_EVAL_AUTO=false``)."""

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

EVAL_TURN_TIMEOUT_ENV = "ORCH_HUB_EVAL_TURN_TIMEOUT"
"""Seconds one Layer 2 evaluator CLI turn may run before it is killed.

A semantic evaluation is a single ``claude -p`` subprocess, and a subprocess that
*fails* is already an outcome the job store records well. A subprocess that
neither finishes nor fails — a wedged backend, a network read that never returns,
a child holding the pipe open — is the state nothing recovers from: the job record
stays ``running``, holds the task's duplicate slot, and every retry hands back the
stuck job. This bound turns that hang into the failure it should have been. Unset
or blank means :data:`DEFAULT_EVAL_TURN_TIMEOUT`; ``0`` removes the deadline (and
with it the in-flight stuck detection derived from it, which has no other basis to
rest on); a negative or non-integer value fails loudly."""

DEFAULT_EVAL_TURN_TIMEOUT = 1800
"""Default Layer 2 turn deadline: 30 minutes.

Sized to be unreachable by a legitimate turn rather than to be tight. The
evaluator sends one bounded prompt (~32k input tokens) to a max-effort model and
reads back one report; observed turns run minutes, and the pessimistic case — CLI
retries against a degraded provider — is still well inside this. Cutting a real
turn short would spend the window and lose the report, which is worse than
waiting, so the deadline is only meant to catch the case where nothing is
progressing at all."""

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

EVAL_USAGE_CEILING_ENV = "ORCH_HUB_EVAL_USAGE_CEILING"
"""Subscription-utilization ceiling above which an automatic Layer 2 turn waits.

Cost is not what makes an automatic semantic evaluation risky; *contention* is.
One Layer 2 turn is a bounded ``claude -p`` call, but it draws on the same
subscription rate window a dev session needs, so a turn that fires while the
operator is rationing the window spends the work budget on measuring the work.
This is the observed used-percentage (0-100) at or above which the automatic
dispatcher holds a job in the deferred queue instead of running it; below it,
the turn is invisible against the window it shares.

Compared against the *highest* currently-measured window of the evaluator's
provider, so an exhausted weekly window holds a job even while the five-hour one
is fresh. A malformed or out-of-range value fails loudly rather than silently
picking a policy. Only the automatic path consults it: an operator asking for an
evaluation now is not the production-side selection this gate exists to shape."""

DEFAULT_EVAL_USAGE_CEILING = 80.0
"""Default utilization ceiling for automatic Layer 2 dispatch, in percent.

One Layer 2 turn measures ~32k input tokens against a window that absorbs
several dev sessions of up to 200k context each — under 1% of the work it
measures — so 20 points of headroom is roughly twenty times the turn's own cost.
The ceiling is therefore not sized to make the turn *fit*; it is sized so the
turn never *competes*: below 80% the operator has not started rationing, and
above it measurement can wait for the rollover. Keeping it high also matters in
the other direction — a low ceiling would hold every job until the window rolled
and then hand the queue a whole backlog at once, recreating the herd the
dispatch spacing exists to prevent."""

EVAL_DISPATCH_SPACING_ENV = "ORCH_HUB_EVAL_DISPATCH_SPACING"
"""Minimum seconds between two automatic Layer 2 turns (the rollover-herd guard).

A ceiling plus a queue has a failure mode the ceiling alone does not: under
sustained pressure the deferred queue accumulates, and the moment the window
rolls every held job becomes dispatchable at once — spending the fresh window's
opening capacity exactly when the operator comes back to work. Single-flight
prevents double-spending one task, not a herd across tasks. The dispatcher
therefore drains at most one held job per this interval, measured from the last
turn that actually started (manual turns included — they consume the same
window). Unset or blank means :data:`DEFAULT_EVAL_DISPATCH_SPACING`; ``0``
drains as fast as the queue allows; a negative or non-integer value fails
loudly."""

DEFAULT_EVAL_DISPATCH_SPACING = 900
"""Default spacing between automatic Layer 2 turns: 15 minutes.

Drains a post-rollover backlog at four turns an hour — roughly four points of
window budget an hour, a trickle against the fresh window rather than a burst.
It is also longer than the usage collector's freshness horizon (300s), so the
capacity read that admits the next turn already reflects the previous one
instead of deciding on pre-dispatch figures. On an ordinary week completions
arrive further apart than this, so the spacing binds only during the burst it
exists for."""

EVAL_QUEUE_MAX_AGE_ENV = "ORCH_HUB_EVAL_QUEUE_MAX_AGE"
"""Seconds a job may sit in the deferred queue before it is surfaced as stalled.

Deferral is meant to be temporary, and every reason it is not — an exhausted
weekly window, a usage feed that stopped reporting, a ``claude`` binary that can
no longer refresh it — looks identical from inside the queue: nothing happens,
quietly, and the corpus stops growing. Past this age the dispatcher stops
treating a held job as normal backpressure and says so. It changes no dispatch
decision; it only bounds how long a stalled queue can stay invisible. Unset or
blank means :data:`DEFAULT_EVAL_QUEUE_MAX_AGE`; ``0`` disables the bound; a
negative or non-integer value fails loudly."""

DEFAULT_EVAL_QUEUE_MAX_AGE = 21600
"""Default deferred-queue age bound: six hours — one full five-hour window plus
slack.

A job that sat through an entire five-hour window without dispatching did not
merely lose a busy afternoon: either the weekly window is exhausted or the usage
signal itself is broken. The second is the likelier and worse failure, because
unavailable usage defers *forever* by design, so the bound is set short enough to
reveal it within a working day rather than after a week of silently missing
reports."""

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


def _parse_percent(raw: str | None, *, default: float, env_name: str) -> float:
    """Parse a 0-100 percentage env var: unset/blank is ``default``, bad is loud.

    Mirrors :func:`_parse_retention`: a typo in a dispatch threshold must surface
    at startup, because the quiet failure mode is a policy nobody chose — a
    ceiling read as ``0`` would hold every automatic evaluation forever, and one
    read as ``1000`` would disable the gate the operator asked for."""
    value = (raw or "").strip()
    if not value:
        return default
    try:
        percent = float(value)
    except ValueError:
        raise ValueError(
            f"{env_name} must be a number between 0 and 100, got {value!r}"
        ) from None
    if not 0.0 <= percent <= 100.0:
        raise ValueError(f"{env_name} must be between 0 and 100, got {percent}")
    return percent


def _parse_seconds(raw: str | None, *, default: int, env_name: str) -> int:
    """Parse a non-negative seconds env var: unset/blank is ``default``, bad is loud.

    Same contract as :func:`_parse_retention` for the two duration thresholds
    (dispatch spacing, deferred-queue age bound); ``0`` is a meaningful value for
    both (no spacing / no age bound) and a negative one is a typo, not a policy."""
    value = (raw or "").strip()
    if not value:
        return default
    try:
        seconds = int(value)
    except ValueError:
        raise ValueError(
            f"{env_name} must be a non-negative integer number of seconds,"
            f" got {value!r}"
        ) from None
    if seconds < 0:
        raise ValueError(
            f"{env_name} must be non-negative (0 disables), got {seconds}"
        )
    return seconds


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

    mandrel_path: Path
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

    eval_auto: bool = DEFAULT_EVAL_AUTO
    """Whether a completed task is evaluated automatically (``ORCH_HUB_EVAL_AUTO``).

    The trigger gate: independent of ``evaluation_enabled`` so manual-only
    operation stays available without removing the subsystem, and subordinate to
    it because a disabled subsystem has nothing to trigger. Only the
    completion-triggered path reads it — every manual generation route is
    unaffected.
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

    eval_turn_timeout: int = DEFAULT_EVAL_TURN_TIMEOUT
    """Seconds one Layer 2 evaluator CLI turn may run (``ORCH_HUB_EVAL_TURN_TIMEOUT``).

    Passed to the semantic backend as its subprocess deadline, and — plus the two
    fixed allowances for the evidence build before the turn and the report
    persistence after it — the whole-attempt deadline a semantic job is stamped
    with and reaped against (:mod:`orch_hub.semantic_jobs`). ``0`` disables both:
    with no deadline there is nothing to distinguish a hung turn from a long one.
    """

    eval_retention: int = DEFAULT_EVAL_RETENTION
    """Per-task cap on retained evaluations (``ORCH_HUB_EVAL_RETENTION``).

    The evaluation subsystem keeps the ``eval_retention`` newest evaluations per
    task and prunes older ones on the next generation, so ``<data_dir>/evaluations``
    stays bounded. ``0`` disables retention (keep everything); a positive integer
    is the cap. Only meaningful when evaluation is enabled.
    """

    eval_usage_ceiling: float = DEFAULT_EVAL_USAGE_CEILING
    """Utilization percent at or above which automatic Layer 2 defers.

    Selected by ``ORCH_HUB_EVAL_USAGE_CEILING``. Read only by the automatic
    dispatcher, against the evaluator provider's highest currently-measured
    window; a manual evaluation ignores it entirely.
    """

    eval_dispatch_spacing: int = DEFAULT_EVAL_DISPATCH_SPACING
    """Minimum seconds between two automatic Layer 2 turns (herd guard).

    Selected by ``ORCH_HUB_EVAL_DISPATCH_SPACING``; ``0`` drains the deferred
    queue as fast as capacity allows.
    """

    eval_queue_max_age: int = DEFAULT_EVAL_QUEUE_MAX_AGE
    """Seconds after which a still-deferred Layer 2 job is surfaced as stalled.

    Selected by ``ORCH_HUB_EVAL_QUEUE_MAX_AGE``; ``0`` disables the bound. It
    affects visibility only, never a dispatch decision.
    """

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        """Build config from the environment, honoring the overrides.

        An empty or whitespace-only value counts as unset for every variable,
        so a stray ``export ORCH_HUB_TOKEN=""`` cannot make the empty string a
        valid credential; surrounding whitespace is stripped for the same
        reason. A malformed ``ORCH_HUB_PORT`` raises instead of silently
        falling back to the default port. The mandrel checkout has no portable
        default: :data:`MANDREL_PATH_ENV` must name it, with
        :data:`LEGACY_MANDREL_PATH_ENV` still accepted as a fallback.
        """
        env = os.environ if environ is None else environ
        raw_path = (env.get(MANDREL_PATH_ENV) or "").strip() or (
            env.get(LEGACY_MANDREL_PATH_ENV) or ""
        ).strip()
        if not raw_path:
            raise ValueError(
                "Mandrel checkout is not configured; set "
                f"{MANDREL_PATH_ENV} to its path"
                f" ({LEGACY_MANDREL_PATH_ENV} is also accepted for legacy installs)"
            )
        path = Path(raw_path).expanduser()
        raw_token = env.get(TOKEN_ENV)
        token = raw_token.strip() if raw_token else None
        raw_data_dir = (env.get(DATA_DIR_ENV) or "").strip()
        raw_codex_home = (env.get(CODEX_HOME_ENV) or "").strip()
        raw_claude_home = (env.get(CLAUDE_HOME_ENV) or "").strip()
        raw_bark_url = env.get(BARK_URL_ENV)
        bark_url = raw_bark_url.strip() if raw_bark_url else None
        return cls(
            mandrel_path=path,
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
            eval_auto=_parse_bool(
                env.get(EVAL_AUTO_ENV),
                default=DEFAULT_EVAL_AUTO,
                env_name=EVAL_AUTO_ENV,
            ),
            eval_model=(env.get(EVAL_MODEL_ENV) or "").strip() or DEFAULT_EVAL_MODEL,
            eval_effort=(env.get(EVAL_EFFORT_ENV) or "").strip() or DEFAULT_EVAL_EFFORT,
            eval_retention=_parse_retention(env.get(EVAL_RETENTION_ENV)),
            eval_turn_timeout=_parse_seconds(
                env.get(EVAL_TURN_TIMEOUT_ENV),
                default=DEFAULT_EVAL_TURN_TIMEOUT,
                env_name=EVAL_TURN_TIMEOUT_ENV,
            ),
            eval_usage_ceiling=_parse_percent(
                env.get(EVAL_USAGE_CEILING_ENV),
                default=DEFAULT_EVAL_USAGE_CEILING,
                env_name=EVAL_USAGE_CEILING_ENV,
            ),
            eval_dispatch_spacing=_parse_seconds(
                env.get(EVAL_DISPATCH_SPACING_ENV),
                default=DEFAULT_EVAL_DISPATCH_SPACING,
                env_name=EVAL_DISPATCH_SPACING_ENV,
            ),
            eval_queue_max_age=_parse_seconds(
                env.get(EVAL_QUEUE_MAX_AGE_ENV),
                default=DEFAULT_EVAL_QUEUE_MAX_AGE,
                env_name=EVAL_QUEUE_MAX_AGE_ENV,
            ),
        )

    @property
    def mandrel_bin(self) -> Path:
        """Absolute path to the mandrel executable inside its checkout.

        orch-hub invokes it by absolute path rather than relying on it being on
        PATH, since it ships as a repo-local script, not a system install.
        """
        return self.mandrel_path / MANDREL_BIN_RELATIVE_PATH

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
        """Fallback registry file used when the mandrel command is unavailable."""
        return self.mandrel_path / ".registry" / "repos.local.json"

    @property
    def auto_evaluation_enabled(self) -> bool:
        """Whether the completion-triggered evaluation path may act at all.

        The conjunction of the subsystem gate and the trigger gate, derived here
        rather than in each consumer because the automatic path is more than one
        step: the completion hook accepts a job, and a background sweep later
        turns it into a model turn. Two steps re-deriving the same predicate is
        exactly how one of them ends up ungated — and the ungated half is the
        *dispatching* one, so the failure mode is an automatic model turn in a
        deployment that switched automatic evaluation off.

        Read by :class:`~orch_hub.completion_eval.CompletionEvaluationTrigger` and
        :class:`~orch_hub.eval_dispatch.DeferredDispatcher`. Every manual
        evaluation route is unaffected: it is gated by ``evaluation_enabled``
        alone, which is what "manual-only operation" means.
        """
        return self.evaluation_enabled and self.eval_auto
