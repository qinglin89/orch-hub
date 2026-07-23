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

- **Claude** — the hub *does* drive Claude (intake, semantic eval), so Claude's
  five-hour / seven-day usage is captured by *observing the hub's own*
  ``claude -p`` sessions and retaining the latest observation in a hub-owned store
  (:class:`ClaudeUsageStore`). No Claude turn is ever started just to refresh
  usage, and no interactive UI or private OAuth endpoint is scraped. (The parser
  that turns a session's stream-json into an observation, and the wiring that
  feeds it, land in a follow-up slice; this module owns the store, the staleness
  model, and the read side, so an unfed store simply reads as *unavailable*.)

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


# -- aggregation service ------------------------------------------------------


def _safe(provider: str, fn: Callable[[], ProviderUsage]) -> ProviderUsage:
    """Run one provider read, turning any failure into a fail-soft *unavailable*
    snapshot so one provider can never break the other or the whole route."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — display-only; degrade, never raise
        return ProviderUsage.unavailable(provider, f"usage read failed: {type(exc).__name__}")


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

    def record_claude_observation(self, observation: ClaudeObservation) -> None:
        """Persist a fresh Claude observation and drop the cached snapshot."""
        self._store.record(observation)
        self._cache = None

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
        yield _safe(PROVIDER_CLAUDE, lambda: claude_usage(self._store, now=now))
        yield _safe(PROVIDER_CODEX, lambda: read_codex_usage(self._config.codex_home, now=now))
