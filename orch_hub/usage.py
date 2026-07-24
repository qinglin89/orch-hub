"""Read-only agent-subscription-usage signal for Claude and Codex.

The Operations Overview shows how much of each agent CLI's rate-limit budget is
spent and when it resets, so an operator can see available agent capacity at a
glance. This module produces that signal, normalized into the provider-neutral
:class:`~orch_hub.models.ProviderUsage` / :class:`~orch_hub.models.SubscriptionUsage`
shapes. It is **strictly read-only and display-only**: it never starts a model
turn, never consumes a Codex reset credit, never calls a private endpoint, and
its output feeds no readiness / attention / run / intake decision.

Two providers, two different signal sources — because the hub relates to each CLI
differently:

- **Codex** — the hub does not itself drive Codex, so it reads Codex's *own*
  local rate-limit record. Every Codex run writes ``rate_limits`` snapshots into
  its session rollouts under ``<codex_home>/sessions/YYYY/MM/DD/rollout-*.jsonl``;
  :func:`read_codex_usage` finds the newest rollout carrying one and normalizes
  its dynamically-named limit bucket plus optional ``primary`` / ``secondary``
  windows. This is the read-only local rate-limit interface: reading a file the
  user's own Codex usage already wrote — no subprocess, no network, and never the
  credit-spend path. Only the rate-limit fields are read; account / credential /
  reset-credit identifiers are never touched (they live in ``auth.json``, which
  this module never opens).

- **Claude** — the latest observation is retained in a hub-owned store
  (:class:`ClaudeUsageStore`), fed by two carriers, newest wins:

  1. *The statusLine sink* — the one local channel that actually carries the
     five-hour / seven-day windows. Claude Code pipes them to a configured
     statusLine command's stdin, so the operator-installed writer
     (:mod:`orch_hub.statusline`) persists that payload to
     :data:`CLAUDE_STATUSLINE_NAME` and :func:`read_claude_statusline` ingests it
     into the same store on read. This is the Codex pattern applied to Claude:
     read what the operator's own sessions already wrote. Uninstalled, the sink is
     simply absent and Claude reads *unavailable* exactly as before.
  2. *Hub-driven turns* — the hub also drives Claude (intake, semantic eval), and
     :meth:`UsageService.claude_observer` is the best-effort seam those backends
     feed each turn's ``stream-json`` stdout into
     (:func:`parse_claude_stream_usage`). Verified against Claude Code 2.1.218,
     that stdout carries no 5h/7d windows on any event (the section note below
     records the evidence), so on the installed CLI this carrier is structurally
     inert — it recognizes nothing and records nothing. It stays wired because it
     costs one no-op call per turn and is where a future carrier would arrive.

  Both share one parser, one store, and one read path. No Claude turn is ever
  started just to refresh usage, and no interactive UI or private OAuth endpoint
  is scraped — including the CLI's own ``/usage`` command, which fetches from a
  private endpoint and spends a ``quota_check`` API call.

:class:`UsageService` aggregates both behind a small bounded cache so a burst of
Overview refreshes does not re-scan rollout files every time, and degrades each
provider independently: one source failing (or being absent) never suppresses the
other, and a failure is a fail-soft *unavailable* snapshot rather than an error.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .models import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    ProviderUsage,
    SubscriptionUsage,
    UsageCredits,
    UsageWindow,
)

USAGE_DIR_NAME = "usage"
"""Sub-dir of the hub data dir holding subscription-usage state."""

CLAUDE_USAGE_NAME = "claude.json"
"""The latest observed Claude usage snapshot, under ``<data_dir>/usage/``."""

CLAUDE_STATUSLINE_NAME = "claude-statusline.json"
"""Sink the operator-installed statusLine writer (:mod:`orch_hub.statusline`)
publishes the CLI's own ``rate_limits`` payload to, under ``<data_dir>/usage/``.

Separate from :data:`CLAUDE_USAGE_NAME` on purpose: that one is the hub's own
normalized observation store, while this is raw carrier output written by an
external process — untrusted display input, read tolerantly and bounded."""

MAX_STATUSLINE_BYTES = 256 * 1024
"""Skip a statusLine sink larger than this. The writer bounds what it persists,
but the sink is an external file: refusing to slurp an implausibly large one
keeps a corrupted/hand-edited file off the Overview read path (it degrades to
*unavailable*, like any other unreadable source)."""

USAGE_STALE_AFTER_SECONDS = 3600
"""How old an observation may be before it is flagged ``stale``.

Past this the used-percentages are shown as *last observed* rather than current
(the reset time, being absolute, stays meaningful regardless). One hour is short
enough that a stale figure is visibly old against a five-hour window, long enough
that an idle hub (no recent Codex run, no hub-driven Claude turn) still shows the
last real numbers instead of collapsing to unavailable."""

USAGE_CACHE_TTL_SECONDS = 30
"""Bounded cache lifetime for an aggregated usage snapshot.

The Overview refreshes on a timer and several clients may poll at once; caching
the combined snapshot for a few tens of seconds keeps rollout-file scans and
store reads off the hot path without letting the figures drift far from live.
Deliberately not an environment setting (like the run monitor's interval)."""

MAX_ROLLOUT_SCAN = 8
"""Upper bound on how many newest Codex rollouts to open looking for a
``rate_limits`` snapshot. The newest session normally carries the freshest one;
a handful of fallbacks covers a just-started or errored session that wrote none
yet, while bounding the work when *no* rollout carries limits."""

MAX_ROLLOUT_BYTES = 25 * 1024 * 1024
"""Skip a rollout larger than this when scanning for limits — a defensive guard so
a pathologically large session file cannot be slurped into memory on a view load.
Real rollouts are far smaller; a skipped one just falls through to the next."""

# Rate-limit windows are keyed the same way across normalization: the machine key
# is stable (so the UI can target a bucket), the human label is derived from the
# window duration. Codex names its windows ``primary`` / ``secondary``.
_CODEX_WINDOW_KEYS = ("primary", "secondary")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _iso_from_epoch(value: object) -> str | None:
    """An epoch-seconds reset time (Codex ``resets_at``) as an ISO-8601 UTC string.

    ``None`` for a missing or non-numeric value, so a bad field degrades to
    "reset time unknown" rather than raising."""
    if not _is_number(value):
        return None
    try:
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _parse_iso(ts: str | None) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``) to aware UTC.

    ``None`` when absent or unparseable, so staleness simply cannot be computed
    (the observation is still shown) rather than the read failing."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _is_stale(observed_ts: str | None, now: dt.datetime) -> bool:
    """Whether an observation is older than :data:`USAGE_STALE_AFTER_SECONDS`.

    Unknown/unparseable observation time is treated as *not* stale (we cannot say
    it is old), so the figures are shown without an unwarranted stale flag."""
    observed = _parse_iso(observed_ts)
    if observed is None:
        return False
    return (now - observed).total_seconds() > USAGE_STALE_AFTER_SECONDS


def _window_label(window_minutes: object) -> str | None:
    """A short human label for a window duration (``"5h"``, ``"7d"``, ``"30m"``).

    ``None`` for an unknown/zero duration; whole-day/whole-hour durations render
    compactly, otherwise minutes. Purely cosmetic — the machine key is separate."""
    if not _is_number(window_minutes):
        return None
    minutes = int(window_minutes)
    if minutes <= 0:
        return None
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _percent(value: object) -> float | None:
    """A used-percentage as a plain float, or ``None`` when the provider omits it.

    Kept ``None`` (not ``0.0``) on absence so a missing figure never reads as
    "0% used" — the scope's explicit unavailable-not-zero rule."""
    if not _is_number(value):
        return None
    return round(float(value), 2)


def _remaining(used_percent: float | None) -> float | None:
    """Remaining-capacity percentage derived from ``used_percent`` (clamped 0-100)."""
    if used_percent is None:
        return None
    return round(max(0.0, min(100.0, 100.0 - used_percent)), 2)


# -- Codex: read the local rate-limit record ----------------------------------


def _codex_window(key: str, raw: object) -> UsageWindow | None:
    """Normalize one Codex window (``primary``/``secondary``) into a UsageWindow.

    Returns ``None`` for an absent/null window (Codex sets ``secondary`` to null
    on plans with only one window — "optional primary/secondary"). Only the
    rate-limit fields are read (``used_percent`` / ``window_minutes`` /
    ``resets_at``); no numeric message total is invented — Codex reports percent,
    so ``used``/``limit`` stay ``None`` and the UI shows percentages."""
    if not isinstance(raw, dict):
        return None
    used = _percent(raw.get("used_percent"))
    window_minutes = raw.get("window_minutes")
    return UsageWindow(
        key=key,
        label=_window_label(window_minutes),
        window_minutes=int(window_minutes) if _is_number(window_minutes) else None,
        used_percent=used,
        remaining_percent=_remaining(used),
        resets_at=_iso_from_epoch(raw.get("resets_at")),
    )


def _codex_credits(raw: object) -> UsageCredits | None:
    """Normalize the optional Codex ``credits`` object.

    Surfaces only the aggregate has-credits / unlimited / balance fields — never a
    per-credit identifier — so the display can note reset-credit availability
    without exposing a reset-credit id (the scope bars that)."""
    if not isinstance(raw, dict):
        return None
    has_credits = raw.get("has_credits")
    unlimited = raw.get("unlimited")
    balance = raw.get("balance")
    return UsageCredits(
        has_credits=has_credits if isinstance(has_credits, bool) else None,
        unlimited=unlimited if isinstance(unlimited, bool) else None,
        balance=str(balance) if isinstance(balance, (str, int, float)) and not isinstance(balance, bool) else None,
    )


def _codex_plan(rate_limits: dict) -> str | None:
    """The sanitized plan/tier name for the Codex bucket.

    Prefers ``plan_type`` (e.g. ``"prolite"``); falls back to the bucket's
    ``limit_name`` (the "dynamically named limit bucket") when the plan is
    unnamed. Never the account or any credential — only the limit-bucket naming
    Codex already exposes in the rate-limit record."""
    for key in ("plan_type", "limit_name"):
        value = rate_limits.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_codex(rate_limits: dict, observed_ts: str | None, now: dt.datetime) -> ProviderUsage:
    """Normalize a raw Codex ``rate_limits`` object into a ProviderUsage.

    Iterates the optional ``primary``/``secondary`` windows, reads the plan/name
    and optional credits, and flags staleness against the observation time. Only
    whitelisted rate-limit fields cross into the output — anything else in the raw
    object (identifiers, internal flags) is dropped, keeping account/credential/
    reset-credit data out by construction."""
    windows = tuple(
        w
        for key in _CODEX_WINDOW_KEYS
        if (w := _codex_window(key, rate_limits.get(key))) is not None
    )
    if not windows:
        return ProviderUsage.unavailable(
            PROVIDER_CODEX, "codex rate-limit record carried no usable window"
        )
    return ProviderUsage(
        provider=PROVIDER_CODEX,
        available=True,
        plan=_codex_plan(rate_limits),
        windows=windows,
        credits=_codex_credits(rate_limits.get("credits")),
        observed_ts=observed_ts,
        stale=_is_stale(observed_ts, now),
    )


def _newest_rollouts(codex_home: Path) -> list[Path]:
    """Rollout files under ``<codex_home>/sessions`` newest-first, bounded.

    Sorted by filename descending — the rollout name embeds the session's ISO
    start time, so name order is chronological — and capped at
    :data:`MAX_ROLLOUT_SCAN`."""
    sessions = codex_home / "sessions"
    try:
        rollouts = [p for p in sessions.rglob("rollout-*.jsonl") if p.is_file()]
    except OSError:
        return []
    rollouts.sort(key=lambda p: p.name, reverse=True)
    return rollouts[:MAX_ROLLOUT_SCAN]


def _find_rate_limits(path: Path) -> tuple[dict, str | None] | None:
    """The newest ``rate_limits`` snapshot in one rollout, with its timestamp.

    Reverse-scans lines (the last snapshot is the freshest), cheaply pre-filtering
    on the ``"rate_limits"`` substring before parsing, and unwraps the object
    whether it sits at the line top level or under a ``payload`` event. Returns the
    ``(rate_limits, observed_ts)`` pair, or ``None`` when this rollout carries no
    usable snapshot. Skips a file over :data:`MAX_ROLLOUT_BYTES` and treats read/
    parse failures as "no snapshot here" so one bad file never breaks the scan."""
    try:
        if path.stat().st_size > MAX_ROLLOUT_BYTES:
            return None
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        rate_limits = obj.get("rate_limits")
        if not isinstance(rate_limits, dict):
            payload = obj.get("payload")
            rate_limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if isinstance(rate_limits, dict):
            observed_ts = obj.get("timestamp")
            return rate_limits, observed_ts if isinstance(observed_ts, str) else None
    return None


def read_codex_usage(codex_home: Path, *, now: dt.datetime | None = None) -> ProviderUsage:
    """Read Codex's local rate-limit record into a normalized ProviderUsage.

    Scans the newest rollouts (bounded) for the freshest ``rate_limits`` snapshot
    and normalizes it. Fail-soft: an absent sessions dir, no rollout carrying
    limits, or any read error returns an *unavailable* snapshot (never zero usage,
    never a raise) so the Overview degrades gracefully. This never runs Codex and
    never opens anything but the session rollouts — no network, no credential
    read, and never the reset-credit spend path."""
    now = now or _utc_now()
    rollouts = _newest_rollouts(codex_home)
    if not rollouts:
        return ProviderUsage.unavailable(
            PROVIDER_CODEX, "no Codex session rollouts found"
        )
    for path in rollouts:
        found = _find_rate_limits(path)
        if found is not None:
            rate_limits, observed_ts = found
            return _normalize_codex(rate_limits, observed_ts, now)
    return ProviderUsage.unavailable(
        PROVIDER_CODEX, "no Codex rate-limit snapshot in recent sessions"
    )


# -- Claude: retain the latest observation ------------------------------------


@dataclass(frozen=True)
class ClaudeObservation:
    """The latest Claude usage seen from a hub-driven ``claude -p`` session.

    A normalized, persisted snapshot: ``observed_ts`` is when it was captured
    (ISO-8601 UTC), ``plan`` the sanitized subscription tier (or ``None``), and
    ``windows`` the five-hour / seven-day (etc.) buckets. This is what
    :class:`ClaudeUsageStore` round-trips and :func:`claude_usage` reads. It is
    produced by observing the hub's own sessions — never by a probe turn — so the
    store may simply be empty until such a session has run."""

    observed_ts: str
    plan: str | None
    windows: tuple[UsageWindow, ...]

    def to_dict(self) -> dict:
        return {
            "observed_ts": self.observed_ts,
            "plan": self.plan,
            "windows": [w.to_dict() for w in self.windows],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ClaudeObservation":
        return cls(
            observed_ts=data["observed_ts"],
            plan=data.get("plan"),
            windows=tuple(_window_from_dict(w) for w in data.get("windows", [])),
        )


def _window_from_dict(data: dict) -> UsageWindow:
    """Rebuild a :class:`UsageWindow` from its persisted dict (tolerant of missing
    keys, since a stored snapshot may predate a field)."""
    return UsageWindow(
        key=data.get("key", ""),
        label=data.get("label"),
        window_minutes=data.get("window_minutes"),
        used_percent=data.get("used_percent"),
        remaining_percent=data.get("remaining_percent"),
        resets_at=data.get("resets_at"),
        used=data.get("used"),
        limit=data.get("limit"),
    )


class ClaudeUsageStore:
    """Hub-owned store of the latest Claude usage observation.

    Persists a single :class:`ClaudeObservation` to ``<data_dir>/usage/claude.json``
    so the last-seen five-hour / seven-day figures survive a restart and can be
    read without any Claude turn. ``record`` overwrites atomically (latest wins);
    ``latest`` reads it back, returning ``None`` for an unfed or unreadable/corrupt
    store so the reader degrades to *unavailable* rather than failing."""

    def __init__(self, data_dir: Path):
        self._path = data_dir / USAGE_DIR_NAME / CLAUDE_USAGE_NAME

    @property
    def path(self) -> Path:
        return self._path

    def record(self, observation: ClaudeObservation) -> None:
        """Persist ``observation`` as the latest, atomically (same-dir tmp+rename).

        The temp file is *uniquely named* (pid + thread id) rather than a fixed
        ``claude.json.tmp``: hub-driven Claude turns run on daemon threads
        (intake, semantic), so two observers can call ``record`` at once, and a
        shared temp name lets one writer's ``os.replace`` move the file out from
        under the other — raising ``FileNotFoundError`` and losing an otherwise
        valid observation. With a per-writer temp file each ``os.replace`` is an
        independent atomic swap, so concurrent records never collide and the last
        write simply wins. On a failed write the unique temp is cleaned up so it
        cannot accumulate as an orphan in this long-lived service."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(observation.to_dict(), indent=1), encoding="utf-8")
            os.replace(tmp, self._path)
        except BaseException:  # noqa: BLE001 — clean up the unique temp, then re-raise
            tmp.unlink(missing_ok=True)
            raise

    def latest(self) -> ClaudeObservation | None:
        """The latest observation, or ``None`` when unfed / unreadable / corrupt."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "observed_ts" not in data:
            return None
        try:
            return ClaudeObservation.from_dict(data)
        except (KeyError, TypeError):
            return None


def claude_usage(store: ClaudeUsageStore, *, now: dt.datetime | None = None) -> ProviderUsage:
    """Read the latest stored Claude observation into a normalized ProviderUsage.

    Unavailable (never zero usage) when nothing has been observed yet; otherwise
    the stored windows with staleness computed against the observation time."""
    now = now or _utc_now()
    observation = store.latest()
    if observation is None:
        return ProviderUsage.unavailable(
            PROVIDER_CLAUDE, "no Claude usage observed yet"
        )
    return ProviderUsage(
        provider=PROVIDER_CLAUDE,
        available=True,
        plan=observation.plan,
        windows=observation.windows,
        observed_ts=observation.observed_ts,
        stale=_is_stale(observation.observed_ts, now),
    )


# -- Claude: parse a hub-driven ``claude -p`` turn's stream-json for usage -----
#
# VERIFIED — AND THE ANSWER IS "NOT ON THIS CHANNEL" (Claude Code 2.1.218). The hub
# never starts a probe turn (the scope bars it), so Claude usage can only come from
# the stream-json of turns the hub already drives (intake, semantic eval). That
# stdout does not carry the subscription windows. In the CLI's own emitted-message
# schema, neither ``result`` (success and error subtypes) nor ``system/init`` has a
# rate-limit field at all; the single event that does, ``system/api_error``, carries
# only ``error.rate_limits = {resets_at, rate_limit_type} | null``, described there
# as the quota-429 retry-banner headers — a reset time and a window name, never a
# percentage, and only when a turn is actually rejected. Corroborated empirically:
# no local Claude session record carries a rate-limit *field* at all.
#
# So on absence this parser stays exactly what the scope mandates: it recognizes
# nothing in an ordinary turn and returns no observation, leaving the store unfed
# and Claude reading *unavailable*. What verification changed is the target — the
# keys below are no longer guesses but the CLI's real subscription shape, the one it
# publishes to a statusLine command's stdin:
#
#     "rate_limits": {"five_hour": {"used_percentage": <0-100>, "resets_at": <epoch s>},
#                     "seven_day": {"used_percentage": <0-100>, "resets_at": <epoch s>}}
#
# "Only present for subscribers after first API response", each window optional.
# Reading *that* channel needs a feed the hub does not have today (a statusLine hook
# is operator-configured, and it is fed by the operator's own interactive sessions
# rather than by hub-driven ones) — its own task. Until such a feed exists this path
# is correctly inert, but aligned: a carrier appearing on the stream is parsed, not
# missed. Everything below is tolerant and never raises — a malformed line is skipped
# like the orchestrator/intake readers do, and the last usable container wins.

_CLAUDE_CONTAINER_KEYS = ("rate_limits",)
"""The key the rate-limit container arrives under. Snake-case ``rate_limits`` is the
only spelling the CLI writes — statusLine payload, ``/usage`` payload, and the
``system/api_error`` wire schema alike."""

_CLAUDE_WINDOW_KEYS = ("five_hour", "seven_day")
"""The two subscription windows the CLI publishes, keyed by its own names — which
double as the stable machine keys the store and the UI target. Its internal record
also holds ``seven_day_overage_included`` / ``overage`` (the header-derived cache)
and ``seven_day_opus`` / ``seven_day_sonnet`` (the ``/usage`` payload), but no public
surface emits those, so they are deliberately not read."""

_CLAUDE_USED_KEYS = ("used_percentage",)
"""The used figure, as a 0-100 percentage.

Deliberately *only* ``used_percentage``. The CLI also carries ``utilization``, whose
unit is source-dependent: a 0..1 fraction in the header-derived cache (the statusLine
payload builds ``used_percentage`` as ``utilization * 100``) but an already-0-100
number in the ``/usage`` payload. Accepting it would risk rendering ``0.42`` as
"0.42% used" — and an unrecognized shape degrading to unavailable is the mandated
graceful state, whereas a percentage wrong by 100x is not."""

_CLAUDE_RESET_KEYS = ("resets_at",)
"""Window reset time, as Unix epoch seconds (:func:`_claude_reset` also takes ISO)."""

_CLAUDE_PLAN_KEYS = ("subscription_type",)
"""The subscription tier (``"pro"`` / ``"max"`` / ``"team"`` / ``"enterprise"``,
null for API-key sessions) — a sibling of the container rather than a field inside
it, and never an account or credential identifier."""

_CLAUDE_WINDOW_MINUTES = {"five_hour": 300, "seven_day": 10080}
"""Each window's duration. The CLI's shape carries no duration field — the window's
key *is* its duration — so these supply the minutes and label the UI renders."""


def _first(data: dict, keys: Iterable[str]) -> object:
    """The first present, non-``None`` value among ``keys`` in ``data`` (else None)."""
    for key in keys:
        if isinstance(data, dict) and data.get(key) is not None:
            return data[key]
    return None


def _claude_reset(value: object) -> str | None:
    """A window reset time as ISO-8601 UTC. The CLI emits Unix epoch seconds; an ISO
    string is also accepted as tolerance. ``None`` when absent/unusable."""
    if _is_number(value):
        return _iso_from_epoch(value)
    if isinstance(value, str):
        parsed = _parse_iso(value)
        return parsed.isoformat() if parsed is not None else None
    return None


def _claude_window(key: str, raw: object) -> UsageWindow | None:
    """Normalize one Claude window dict into a :class:`UsageWindow`.

    The used-percentage is what makes a window an observation: without it there is
    nothing to show but a reset time, and publishing that would render an
    *available* Claude card carrying no figures. So a window missing it yields
    ``None`` — an empty stub never becomes "0% used", and the stream's one real
    rate-limit frame (a quota-429 ``api_error``, which names a window and its reset
    but no percentage) cannot masquerade as usage. Percent-first like Codex:
    ``used``/``limit`` stay ``None`` — Claude reports percentages, so no numeric
    message total is invented (the scope's unavailable-not-zero /
    no-invented-total rules).

    Decided explicitly when the statusLine carrier landed: that quota-429 frame is
    *not* promoted to a distinct "rate limited" display state either. It would need
    a new provider state threaded through models, the route, and the cards to say
    less than the sink now says (no percentage, a window naming scheme of its own),
    about a condition that is transient and only observable while a hub-driven turn
    is being rejected. Rejecting it here keeps one rule: an available Claude card
    carries figures."""
    if not isinstance(raw, dict):
        return None
    used = _percent(_first(raw, _CLAUDE_USED_KEYS))
    resets = _claude_reset(_first(raw, _CLAUDE_RESET_KEYS))
    if used is None:
        return None
    minutes = _CLAUDE_WINDOW_MINUTES.get(key)
    return UsageWindow(
        key=key,
        label=_window_label(minutes),
        window_minutes=minutes,
        used_percent=used,
        remaining_percent=_remaining(used),
        resets_at=resets,
    )


def _claude_windows(container: object) -> list[UsageWindow]:
    """Extract the 5h/7d windows from a rate-limit container.

    The shape is a dict keyed by window name
    (``{"five_hour": {...}, "seven_day": {...}}``), each window optional — the CLI
    omits one it has no figure for, and an omitted or unusable window simply
    contributes nothing rather than blocking the other."""
    if not isinstance(container, dict):
        return []
    windows: list[UsageWindow] = []
    for key in _CLAUDE_WINDOW_KEYS:
        window = _claude_window(key, container.get(key))
        if window is not None:
            windows.append(window)
    return windows


def _claude_container(event: dict) -> object | None:
    """Locate a rate-limit container on one stream-json event, if any.

    Looks under the container key at the event top level and one level down under
    the wrappers a session payload would plausibly nest in
    (``result``/``usage``/``message``/``payload``). That descent is tolerance, not
    guesswork: no event of the installed CLI carries the container at all, so this
    is what would find it if a future CLI attached the statusLine-shaped payload to
    the stream. Returns the first container found, else ``None``."""
    container = _first(event, _CLAUDE_CONTAINER_KEYS)
    if container is not None:
        return container
    for wrapper_key in ("result", "usage", "message", "payload"):
        wrapper = event.get(wrapper_key)
        if isinstance(wrapper, dict):
            container = _first(wrapper, _CLAUDE_CONTAINER_KEYS)
            if container is not None:
                return container
    return None


def _claude_plan(event: dict, container: object) -> str | None:
    """Sanitized plan/tier name, from the container or the event — never an account
    or credential (only the plan naming, mirroring the Codex whitelist stance)."""
    for source in (container, event):
        if isinstance(source, dict):
            value = _first(source, _CLAUDE_PLAN_KEYS)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def parse_claude_stream_usage(stdout: str, *, observed_ts: str) -> ClaudeObservation | None:
    """Parse a hub-driven ``claude -p`` turn's stream-json stdout for usage.

    Tolerant (see the section note above): scans the JSONL events for a rate-limit
    container in the CLI's subscription shape, extracts the five-hour / seven-day
    windows, and returns a :class:`ClaudeObservation` stamped with ``observed_ts``
    (when the hub saw the turn — the honest observation time, since the hub is
    watching this turn live). Returns ``None`` — meaning *no supported usage was
    seen*, so the store stays unfed and Claude reads unavailable — when the stdout
    carries no such fields (which, on the installed CLI, is every ordinary turn), is
    empty, or is unparseable. Never raises: a malformed line is skipped, and the
    last usable container in the stream wins (freshest)."""
    latest: tuple[list[UsageWindow], str | None] | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        container = _claude_container(event)
        if container is None:
            continue
        windows = _claude_windows(container)
        if windows:
            latest = (windows, _claude_plan(event, container))
    if latest is None:
        return None
    windows, plan = latest
    return ClaudeObservation(observed_ts=observed_ts, plan=plan, windows=tuple(windows))


# -- Claude: read the operator-installed statusLine sink ----------------------
#
# THE CARRIER THAT ACTUALLY WORKS. Everything above establishes that no hub-driven
# turn can observe Claude's subscription windows; the CLI publishes them only to a
# statusLine command's stdin. :mod:`orch_hub.statusline` is the operator-installed
# writer that persists that payload; this is the read half.
#
# The sink is deliberately shaped so the *verified* mapping above parses it
# unchanged — ``rate_limits`` at the top level, ``subscription_type`` beside it —
# so there is exactly one place that decides what a window means. What differs
# from the stream is only the envelope (one JSON object, not JSONL) and the
# observation time: the writer stamps when the CLI published the figures, which is
# the honest observation moment and the basis for staleness. Stale is the *normal
# resting state* here — an idle Mac renders no status line, so nothing is written
# — and it must read as last-observed figures, never as zero usage.


def parse_claude_statusline(text: str, *, observed_ts: str) -> ClaudeObservation | None:
    """Parse the statusLine sink's contents into an observation.

    Reuses the verified stream mapping (:func:`_claude_container` /
    :func:`_claude_windows` / :func:`_claude_plan`), so both carriers agree by
    construction on what a window is. ``observed_ts`` is the fallback observation
    time (the sink's mtime); the record's own stamp wins when it parses, since it
    marks when the CLI published the figures rather than when the file last moved.

    Returns ``None`` — no observation, so the store keeps whatever it had — for
    anything unusable: not JSON, not an object, no rate-limit container, or a
    container whose windows carry no used-percentage. Never raises: this is
    untrusted external input on a display read path."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    container = _claude_container(data)
    if container is None:
        return None
    windows = _claude_windows(container)
    if not windows:
        return None
    stamped = _parse_iso(data.get("observed_ts"))
    return ClaudeObservation(
        observed_ts=stamped.isoformat() if stamped is not None else observed_ts,
        plan=_claude_plan(data, container),
        windows=tuple(windows),
    )


def read_claude_statusline(path: Path) -> ClaudeObservation | None:
    """Read the statusLine sink at ``path``, or ``None`` when it yields nothing.

    ``None`` covers every "no feed" case identically — the writer is not installed,
    no subscriber session has rendered a status line yet, the file is unreadable,
    oversized (:data:`MAX_STATUSLINE_BYTES`), or malformed — so an uninstalled hook
    and a broken one both degrade to the pre-existing *unavailable* card rather
    than to an error or to fabricated figures."""
    try:
        stat = path.stat()
        if stat.st_size > MAX_STATUSLINE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None
    return parse_claude_statusline(text, observed_ts=mtime)


def _is_newer(candidate: str | None, current: str | None) -> bool:
    """Whether ``candidate`` is a strictly later observation time than ``current``.

    Gates sink ingestion so it never regresses the store: equal timestamps (the
    common case — repeated reads of an unchanged sink) write nothing, and an older
    sink cannot overwrite a fresher observation. An unparseable stored timestamp
    yields to the candidate; an unparseable candidate never wins."""
    new = _parse_iso(candidate)
    if new is None:
        return False
    old = _parse_iso(current)
    return old is None or new > old


# -- aggregation service ------------------------------------------------------


def _safe(provider: str, fn: Callable[[], ProviderUsage]) -> ProviderUsage:
    """Run one provider read, turning any failure into a fail-soft *unavailable*
    snapshot so one provider can never break the other or the whole route."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — display-only; degrade, never raise
        return ProviderUsage.unavailable(provider, f"usage read failed: {type(exc).__name__}")


ClaudeUsageObserver = Callable[[str], None]
"""Best-effort seam: given one hub-driven ``claude -p`` turn's raw stdout, record a
Claude usage observation when the supported rate-limit fields are present.

The hub-driven ``claude -p`` backends (the intake agent and the semantic backend)
call it once per turn, so a turn the hub already runs updates Claude usage without a
probe turn — and without those modules importing usage internals (they take a plain
``Callable[[str], None]``). The seam stays wired although the installed CLI puts no
subscription windows on that stdout (see the section note): it costs one no-op call
per turn, and it is the placement a carrier would arrive on. It MUST never raise:
usage bookkeeping can never affect a turn. :meth:`UsageService.claude_observer` is
the production implementation; the backends default to a no-op when no observer is
wired."""


class UsageService:
    """Aggregate Claude + Codex subscription usage behind a bounded cache.

    Reads each provider independently and fail-soft (:func:`_safe`), so a missing
    or broken source degrades only its own card. The combined snapshot is cached
    for :data:`USAGE_CACHE_TTL_SECONDS` to keep rollout scans and store reads off
    a burst of Overview refreshes; :meth:`record_claude_observation` writes a new
    observation and invalidates the cache. Nothing here gates any operation — it
    is a display-only sidecar over the two CLIs' own signals."""

    def __init__(
        self,
        config: Config,
        *,
        claude_store: ClaudeUsageStore | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ):
        self._config = config
        self._store = claude_store if claude_store is not None else ClaudeUsageStore(config.data_dir)
        self._clock = clock
        self._cache: tuple[dt.datetime, SubscriptionUsage] | None = None

    @property
    def store(self) -> ClaudeUsageStore:
        return self._store

    @property
    def statusline_sink(self) -> Path:
        """Where the operator-installed statusLine writer publishes (read-only here)."""
        return self._config.data_dir / USAGE_DIR_NAME / CLAUDE_STATUSLINE_NAME

    def record_claude_observation(self, observation: ClaudeObservation) -> None:
        """Persist a fresh Claude observation and drop the cached snapshot."""
        self._store.record(observation)
        self._cache = None

    def claude_observer(self) -> ClaudeUsageObserver:
        """A best-effort callback that feeds one ``claude -p`` turn's stdout to the store.

        This is the :data:`ClaudeUsageObserver` seam the hub-driven Claude backends
        (intake agent, semantic backend) call each turn, so a turn the hub already
        runs updates Claude usage without a probe turn. The callback parses the stdout
        (:func:`parse_claude_stream_usage`, stamping the observation with this
        service's clock) and records only when the supported rate-limit fields are
        present; unsupported/empty output records nothing, and every exception is
        swallowed so usage bookkeeping can never affect the driving turn. Records run
        through the concurrency-safe store, so intake and semantic turns on separate
        daemon threads can observe at once."""
        def observe(stdout: str) -> None:
            try:
                observation = parse_claude_stream_usage(
                    stdout, observed_ts=self._clock().isoformat()
                )
                if observation is not None:
                    self.record_claude_observation(observation)
            except Exception:  # noqa: BLE001 — display-only; never affect the driving turn
                pass

        return observe

    def get_usage(self) -> SubscriptionUsage:
        """The current combined usage snapshot, served from the bounded cache."""
        now = self._clock()
        if self._cache is not None:
            cached_at, cached = self._cache
            if (now - cached_at).total_seconds() < USAGE_CACHE_TTL_SECONDS:
                return cached
        usage = SubscriptionUsage(providers=tuple(self._read_providers(now)))
        self._cache = (now, usage)
        return usage

    def _read_providers(self, now: dt.datetime) -> Iterable[ProviderUsage]:
        """Read every provider, each fail-soft and independent (Claude, then Codex)."""
        yield _safe(PROVIDER_CLAUDE, lambda: self._read_claude(now))
        yield _safe(PROVIDER_CODEX, lambda: read_codex_usage(self._config.codex_home, now=now))

    def _read_claude(self, now: dt.datetime) -> ProviderUsage:
        """Ingest the statusLine sink, then read the store — the single Claude path.

        Ingestion is a read-path step rather than a background watcher: the sink is
        written by the operator's own sessions at their own cadence, so there is
        nothing to subscribe to, and the aggregate cache already bounds how often
        this runs. It is also isolated — an unreadable sink or a failing store write
        must still yield whatever was observed before, so the card degrades to
        *stale*, never to *unavailable*, on an ingest problem."""
        try:
            self._ingest_statusline()
        except Exception:  # noqa: BLE001 — display-only; the stored observation still reads
            pass
        return claude_usage(self._store, now=now)

    def _ingest_statusline(self) -> None:
        """Record the statusLine sink's observation when it is newer than the stored one.

        Routing the sink *through the store* (rather than reading it as a parallel
        source) keeps one read path, one payload shape, and one staleness rule for
        both carriers, and persists the last-known figures so a rotated, deleted, or
        newly-uninstalled sink still shows them as stale rather than losing them.
        The newness gate makes repeated reads of an unchanged sink a no-op and stops
        a sink from regressing a fresher hub-driven observation."""
        observation = read_claude_statusline(self.statusline_sink)
        if observation is None:
            return
        current = self._store.latest()
        if current is not None and not _is_newer(observation.observed_ts, current.observed_ts):
            return
        self._store.record(observation)
