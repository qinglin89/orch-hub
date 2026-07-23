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
  usage, and no interactive UI or private OAuth endpoint is scraped.
  :func:`parse_claude_stream_usage` turns one turn's ``stream-json`` stdout into an
  observation, and :meth:`UsageService.claude_observer` is the best-effort seam the
  hub-driven ``claude -p`` backends (intake agent, semantic backend) feed each turn
  into. The exact ``stream-json`` field names for the 5h/7d windows are the one
  piece not yet confirmed against a real turn (see the task session log), so the
  parser targets documented best-effort candidates and degrades to *no observation*
  — the store stays unfed and Claude reads *unavailable* — on anything it does not
  recognize, exactly the graceful state the scope mandates.

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


# -- Claude: parse a hub-driven ``claude -p`` turn's stream-json for usage -----
#
# THE ONE UNVERIFIED MAPPING. The hub never starts a probe turn (the scope bars it),
# so Claude usage can only be read from the stream-json of turns the hub already
# drives (intake, semantic eval). The exact field names ``claude -p`` emits for the
# five-hour / seven-day subscription windows are not yet confirmed against a real
# turn — no stored stream-json ``result`` event carries them, and running a turn
# just to learn the shape is barred. So the parser targets these best-effort
# *candidates* and, on anything it does not recognize, returns ``None`` (no
# observation) — the store stays unfed and Claude reads *unavailable*, the scope's
# mandated graceful state. When a real turn is captured, correcting these constants
# (and the matching fixture in ``tests/test_usage.py``) is the whole fix: the store,
# the read side, the wiring, and the UI do not change. Everything below is tolerant
# and never raises — a malformed line is skipped like the orchestrator/intake
# readers do, and the freshest usable container in the stream wins.

_CLAUDE_CONTAINER_KEYS = (
    "rate_limits", "rateLimits", "usage_limits", "usageLimits",
    "subscription", "subscription_usage", "limits",
)
"""Candidate keys under which a stream event may carry the rate-limit container."""

_CLAUDE_WINDOW_KEYS = {
    "five_hour": ("five_hour", "fiveHour", "five_hourly", "5h", "hourly"),
    "seven_day": ("seven_day", "sevenDay", "weekly", "7d"),
}
"""Candidate keys for each named window, mapped to the stable machine key the UI
targets (``five_hour`` / ``seven_day``). A window that arrives unnamed (in a list)
is classified by duration instead (:func:`_classify_claude_window`)."""

_CLAUDE_USED_KEYS = ("used_percent", "usedPercent", "utilization", "percent_used", "used_pct")
_CLAUDE_RESET_KEYS = ("resets_at", "resetsAt", "reset_at", "resetAt", "reset")
_CLAUDE_WINDOW_MINUTES_KEYS = ("window_minutes", "windowMinutes", "window_size_minutes", "duration_minutes")
_CLAUDE_PLAN_KEYS = ("plan", "plan_type", "planType", "subscription_type", "subscriptionType", "tier")
_CLAUDE_WINDOW_LIST_KEYS = ("windows", "buckets", "rate_limits", "limits")

_CLAUDE_DEFAULT_MINUTES = {"five_hour": 300, "seven_day": 10080}
"""Fallback duration for a named window that omits its own — the 5h / 7d its key
already implies, so a window label still renders when only the percent/reset came."""


def _first(data: dict, keys: Iterable[str]) -> object:
    """The first present, non-``None`` value among ``keys`` in ``data`` (else None)."""
    for key in keys:
        if isinstance(data, dict) and data.get(key) is not None:
            return data[key]
    return None


def _claude_reset(value: object) -> str | None:
    """A window reset time as ISO-8601 UTC, from either epoch seconds or an ISO
    string (both plausible in the unverified shape). ``None`` when absent/unusable."""
    if _is_number(value):
        return _iso_from_epoch(value)
    if isinstance(value, str):
        parsed = _parse_iso(value)
        return parsed.isoformat() if parsed is not None else None
    return None


def _claude_window(key: str, raw: object) -> UsageWindow | None:
    """Normalize one Claude window dict into a :class:`UsageWindow`.

    ``None`` when the window carries no usable signal (neither a used-percent nor a
    reset time), so an empty stub never renders as "0% used". Percent-first like
    Codex: ``used``/``limit`` stay ``None`` — Claude reports percentages, so no
    numeric message total is invented (the scope's unavailable-not-zero /
    no-invented-total rules)."""
    if not isinstance(raw, dict):
        return None
    used = _percent(_first(raw, _CLAUDE_USED_KEYS))
    resets = _claude_reset(_first(raw, _CLAUDE_RESET_KEYS))
    if used is None and resets is None:
        return None
    minutes_raw = _first(raw, _CLAUDE_WINDOW_MINUTES_KEYS)
    minutes = int(minutes_raw) if _is_number(minutes_raw) else _CLAUDE_DEFAULT_MINUTES.get(key)
    return UsageWindow(
        key=key,
        label=_window_label(minutes),
        window_minutes=minutes,
        used_percent=used,
        remaining_percent=_remaining(used),
        resets_at=resets,
    )


def _classify_claude_window(raw: object) -> str | None:
    """Classify an unnamed window dict as ``five_hour``/``seven_day`` by duration.

    For a container that lists windows without naming them, the duration separates
    them: under a day is the five-hour window, a day or more the seven-day. ``None``
    when no duration is present to classify on."""
    if not isinstance(raw, dict):
        return None
    minutes = _first(raw, _CLAUDE_WINDOW_MINUTES_KEYS)
    if not _is_number(minutes):
        return None
    return "five_hour" if int(minutes) < 1440 else "seven_day"


def _claude_windows(container: object) -> list[UsageWindow]:
    """Extract the 5h/7d windows from a rate-limit container (freshest-first order).

    Handles both plausible shapes: a dict keyed by window name
    (``{"five_hour": {...}, "seven_day": {...}}``) and a list of unnamed window
    dicts classified by duration. Windows are keyed by the stable machine key and
    de-duplicated (first per key wins)."""
    windows: list[UsageWindow] = []
    seen: set[str] = set()

    def _add(key: str, raw: object) -> None:
        if key in seen:
            return
        window = _claude_window(key, raw)
        if window is not None:
            windows.append(window)
            seen.add(key)

    if isinstance(container, dict):
        for key, candidates in _CLAUDE_WINDOW_KEYS.items():
            _add(key, _first(container, candidates))
        listish = _first(container, _CLAUDE_WINDOW_LIST_KEYS)
        if isinstance(listish, list):
            for raw in listish:
                key = _classify_claude_window(raw)
                if key is not None:
                    _add(key, raw)
    elif isinstance(container, list):
        for raw in container:
            key = _classify_claude_window(raw)
            if key is not None:
                _add(key, raw)
    return windows


def _claude_container(event: dict) -> object | None:
    """Locate a rate-limit container on one stream-json event, if any.

    Looks under the candidate container keys at the event top level and one level
    down under the common wrappers (``result``/``usage``/``message``/``payload``),
    since the shape is unverified. Returns the first container found, else ``None``."""
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

    Tolerant and best-effort (see the module-level mapping note above): scans the
    JSONL events for a rate-limit container in the candidate shape, extracts the
    five-hour / seven-day windows, and returns a :class:`ClaudeObservation` stamped
    with ``observed_ts`` (when the hub saw the turn — the honest observation time,
    since the hub is watching this turn live). Returns ``None`` — meaning *no
    supported usage was seen*, so the store stays unfed and Claude reads unavailable
    — when the stdout carries no recognizable rate-limit fields (the common case
    until the shape is confirmed against a real turn), is empty, or is unparseable.
    Never raises: a malformed line is skipped, and the last usable container in the
    stream wins (freshest)."""
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
``Callable[[str], None]``). It MUST never raise: usage bookkeeping can never affect a
turn. :meth:`UsageService.claude_observer` is the production implementation; the
backends default to a no-op when no observer is wired."""


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
        yield _safe(PROVIDER_CLAUDE, lambda: claude_usage(self._store, now=now))
        yield _safe(PROVIDER_CODEX, lambda: read_codex_usage(self._config.codex_home, now=now))
