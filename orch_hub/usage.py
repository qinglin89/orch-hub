"""Read-only agent-subscription-usage signal for Claude and Codex.

The Operations Overview shows how much of each agent CLI's rate-limit budget is
spent and when it resets, so an operator can see available agent capacity at a
glance. This module produces that signal, normalized into the provider-neutral
:class:`~orch_hub.models.ProviderUsage` / :class:`~orch_hub.models.SubscriptionUsage`
shapes. It is **strictly read-only**: it never starts a model turn, never
consumes a Codex reset credit, and never calls a private endpoint. Its output is
display-only with respect to everything an operator does — it feeds no readiness
/ attention / run / intake decision, and a failing source degrades one card and
nothing else.

Nothing reads it as a *decision*. There was one consumer that did — the automatic
Layer 2 evaluation dispatcher held a hub-scheduled ``claude -p`` turn while the
Claude window it would draw on was near a ceiling — and it is gone with the
automatic path itself (:mod:`orch_hub.completion_eval`). The hub no longer
schedules any model spend of its own, so there is nothing left for a capacity
reading to gate: this module is display-only without exception again.

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
  (:class:`ClaudeUsageStore`), fed by three carriers of decreasing authority:

  1. *The CLI's own utilization cache* — the primary carrier, and the only one
     that works with nobody watching. Claude Code keeps its authenticated
     utilization fetch in ``~/.claude.json``; :func:`read_claude_usage_cache`
     reads the whitelisted rate-limit fields out of it, and
     :class:`ClaudeUsageCollector` refreshes that file by running the CLI's own
     ``/usage`` command in print mode — a local command handled inside the CLI,
     verified per run to reach no model (section note below). It is the Codex
     pattern in its strongest form: read what the CLI itself wrote down, and ask
     it to write it down again when nobody else has.
  2. *The statusLine sink* — a transitional, opportunistic fallback. Claude Code
     pipes the five-hour / seven-day windows to a configured statusLine command's
     stdin, so an operator-installed writer (:mod:`orch_hub.statusline`) persists
     that payload to :data:`CLAUDE_STATUSLINE_NAME` and
     :func:`read_claude_statusline` ingests it into the same store on read. It is
     no longer required for Claude usage to work — carrier 1 covers an operator who
     never installs it — but an existing installation keeps feeding the store for
     free, and its figures merge with carrier 1's rather than competing (below).
  3. *Hub-driven turns* — the hub also drives Claude (intake, semantic eval), and
     :meth:`UsageService.claude_observer` is the best-effort seam those backends
     feed each turn's ``stream-json`` stdout into. No verified version puts a
     *used-percentage* there (2.1.218: no rate-limit field at all; 2.1.220: a
     ``rate_limit_event`` naming one window and its reset instant, with no
     figure — the two section notes below carry the evidence), so this carrier
     never originates Claude usage. What 2.1.220 does give is a free *boundary*
     refresh: a turn the hub already runs can move a stored window's
     ``resets_at`` forward (:func:`parse_claude_stream_boundaries`,
     :func:`merge_window_boundaries`) without touching the percentages the figure
     carriers observed.

  All three share one store and one read path, and every figure merges *per
  window* (:func:`merge_observations`) keyed on the window instant it was measured
  against, ties by ``max(used_percent)``. That is what lets two figure carriers
  coexist without racing: a sink write and a cache read of the same window are one
  window, an idle writer cannot lower a current figure, and a genuine rollover
  still replaces. No Claude turn is ever started just to refresh usage — the
  refresh runs the CLI's local ``/usage`` command, which reaches no model — and no
  interactive UI or private OAuth endpoint is scraped.

:class:`UsageService` aggregates both behind a small bounded cache so a burst of
Overview refreshes does not re-scan rollout files every time, and degrades each
provider independently: one source failing (or being absent) never suppresses the
other, and a failure is a fail-soft *unavailable* snapshot rather than an error.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
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
from .runs import _orchestrator_env

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
    "0% used" — the scope's explicit unavailable-not-zero rule.

    ``_is_number`` is not enough to make the conversion safe: JSON has no integer
    bound, so a corrupt or hostile source can hold a perfectly valid number that no
    float can represent (``float(10**400)`` raises ``OverflowError``). Every parser
    above this promises not to raise into a display read, so an unrepresentable
    figure degrades like an absent one."""
    if not _is_number(value):
        return None
    try:
        return round(float(value), 2)
    except (OverflowError, ValueError):
        return None


def _remaining(used_percent: float | None) -> float | None:
    """Remaining-capacity percentage derived from ``used_percent`` (clamped 0-100)."""
    if used_percent is None:
        return None
    return round(max(0.0, min(100.0, 100.0 - used_percent)), 2)


def current_used_percent(
    usage: ProviderUsage, *, now: dt.datetime | None = None
) -> float | None:
    """The highest *currently measured* used-percentage across a provider's windows.

    The read a consumer needs when it wants to act on capacity rather than render
    it, and it is deliberately all-or-nothing: ``None`` means "this provider's
    utilization is not known right now", and a caller that must fail toward
    caution treats that exactly like a full window. Everything the display shows
    as an explicit degraded state collapses to ``None`` here —

    * the provider is unavailable, or carries no windows at all;
    * the observation is :data:`USAGE_STALE_AFTER_SECONDS` old (``stale``), so the
      figures are last-observed rather than current;
    * a window has no ``used_percent`` — an absent figure is not zero usage;
    * a window's ``resets_at`` has already passed, so no window is open and the
      retained figure describes a window that has since rolled;
    * a window's figure was measured in an *earlier* window than the one
      ``resets_at`` now names (``figure_instant`` behind the boundary, which a
      percentage-less boundary refresh can produce), so the open window's usage
      has never actually been observed.

    ANY unusable window makes the whole read unknown, not just that window: a
    consumer asking "is there room" cannot answer it from the windows that happen
    to be readable while another is unreadable. When every window is usable the
    answer is the highest of them, so an exhausted weekly window is not hidden by
    a fresh five-hour one.

    Display code must not use this — the Overview renders per-window state with
    its own vocabulary. This exists for the decision path (automatic Layer 2
    dispatch), which needs one number or an honest unknown.

    One consequence to know before pointing a decision path at a new provider:
    ``figure_instant`` is only populated by the Claude carriers today, so a Codex
    snapshot always reads unknown here. That is the fail-closed direction and it
    is deliberate — a provider whose figures carry no window identity cannot prove
    they describe the open window — but it means a decision path over Codex must
    first give its windows that identity rather than relax the rule."""
    now = now or _utc_now()
    if not usage.available or usage.stale or not usage.windows:
        return None
    highest: float | None = None
    for window in usage.windows:
        if window.used_percent is None:
            return None
        resets = _parse_iso(window.resets_at)
        if resets is None or resets <= now:
            return None
        figure = _parse_iso(window.figure_instant)
        if figure is None or abs(resets - figure) > _CLAUDE_WINDOW_IDENTITY_TOLERANCE:
            return None
        highest = window.used_percent if highest is None else max(highest, window.used_percent)
    return highest


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
        # ``figure_instant`` is store-internal (kept out of ``UsageWindow.to_dict``
        # so the HTTP payload is unchanged) but must persist, or the per-window
        # merge would lose each figure's window identity across a restart and let a
        # stale writer regress a stored figure again. So the store serializes it
        # alongside the public window dict.
        return {
            "observed_ts": self.observed_ts,
            "plan": self.plan,
            "windows": [
                {**w.to_dict(), "figure_instant": w.figure_instant} for w in self.windows
            ],
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
    keys, since a stored snapshot may predate a field).

    A snapshot written before ``figure_instant`` existed (pre-``93a0e5c``: the old
    ``to_dict`` serialized only the public window dict) has no such key, so its figure
    deserializes with an *unknown* window identity (``figure_instant=None``). It is
    deliberately NOT backfilled from ``resets_at``: a legacy store's displayed reset
    may be one a boundary refresh (:func:`merge_window_boundaries`) already advanced
    past the figure's own window, so ``resets_at`` cannot be trusted as the figure's
    identity — fabricating one from it freezes a stale near-full figure into the new
    window on the first post-upgrade rollover (its lower same-reset post-reset reading
    losing the equal-instant ``max``). Instead :func:`_merge_window` treats an
    unknown-identity stored figure as yielding to any candidate that carries a real
    one, so both a plain and a boundary-advanced legacy window roll over correctly and
    the store self-heals a real ``figure_instant`` on the first write."""
    return UsageWindow(
        key=data.get("key", ""),
        label=data.get("label"),
        window_minutes=data.get("window_minutes"),
        used_percent=data.get("used_percent"),
        remaining_percent=data.get("remaining_percent"),
        resets_at=data.get("resets_at"),
        used=data.get("used"),
        limit=data.get("limit"),
        figure_instant=data.get("figure_instant"),
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
# VERIFIED — NO USED-PERCENTAGE ON THIS CHANNEL, ON ANY VERSION CHECKED. The hub
# never starts a probe turn (the scope bars it), so Claude *figures* can only come
# from the stream-json of turns the hub already drives (intake, semantic eval) —
# and they do not. Verified against Claude Code 2.1.218: in the CLI's own
# emitted-message schema neither ``result`` (success and error subtypes) nor
# ``system/init`` has a rate-limit field at all, and the single event that does,
# ``system/api_error``, carries only
# ``error.rate_limits = {resets_at, rate_limit_type} | null`` — the quota-429
# retry-banner headers, a reset time and a window name, never a percentage, and
# only when a turn is actually rejected. Corroborated empirically on that version:
# no local Claude session record carried a rate-limit field at all.
#
# 2.1.220 adds one event to this channel, ``rate_limit_event`` — still no
# percentage, so nothing here changes; it is a *boundary* signal and is parsed by
# the next section, not by this one.
#
# So this parser stays exactly what the scope mandates: it recognizes no usage in
# an ordinary turn and returns no observation, leaving the store unfed and Claude
# reading *unavailable*. What verification changed is the target — the keys below
# are no longer guesses but the CLI's real subscription shape, the one it publishes
# to a statusLine command's stdin:
#
#     "rate_limits": {"five_hour": {"used_percentage": <0-100>, "resets_at": <epoch s>},
#                     "seven_day": {"used_percentage": <0-100>, "resets_at": <epoch s>}}
#
# "Only present for subscribers after first API response", each window optional.
# That channel is read by the statusLine sink section further down — the carrier that
# actually delivers figures. On the stream this path stays inert but aligned: the
# mapping is shared, so a figure-carrying container appearing here on some future
# version is parsed, not missed. Everything below is tolerant and never raises — a
# malformed line is skipped like the orchestrator/intake readers do, and the last
# usable container wins.

_CLAUDE_CONTAINER_KEYS = ("rate_limits",)
"""The key a *figure-carrying* rate-limit container arrives under. Snake-case
``rate_limits`` is the only spelling the CLI uses for one — statusLine payload,
``/usage`` payload, and the ``system/api_error`` wire schema alike. 2.1.220's
boundary event is a different container under a different key, with camelCase
fields (see :data:`_CLAUDE_BOUNDARY_KEYS`); it carries no figure, so it is
deliberately not reachable from here."""

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

_CLAUDE_WINDOW_IDENTITY_TOLERANCE = dt.timedelta(minutes=5)
"""Maximum reset-instant jitter still treated as one Claude rate-limit window.

Observed cache and statusLine carriers can describe one logical reset a few seconds
apart. Five minutes covers that recomputation jitter while remaining far below the
shortest real window separation (five hours)."""


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
    carries figures. 2.1.220's percentage-less ``rate_limit_event`` is rejected by
    the same rule and reaches the store only as a boundary refresh onto a window
    the sink already established (next section)."""
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
        # A freshly-parsed figure and its reset are measured together, so the
        # figure's own window instant *is* this reset. It only diverges from the
        # displayed ``resets_at`` later, when a boundary-only refresh advances the
        # latter (see ``merge_window_boundaries`` / ``merge_observations``).
        figure_instant=resets,
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


def _claude_container(event: dict, keys: tuple[str, ...] = _CLAUDE_CONTAINER_KEYS) -> object | None:
    """Locate a rate-limit container on one stream-json event, if any.

    Looks under ``keys`` at the event top level and one level down under the
    wrappers a session payload would plausibly nest in
    (``result``/``usage``/``message``/``payload``). That descent is tolerance, not
    guesswork: no verified event carries the *figure* container at all, so this is
    what would find it if a future CLI attached the statusLine-shaped payload to the
    stream — and the 2.1.220 boundary event puts its own container at the top level,
    where the same lookup finds it. Returns the first container found, else
    ``None``."""
    container = _first(event, keys)
    if container is not None:
        return container
    for wrapper_key in ("result", "usage", "message", "payload"):
        wrapper = event.get(wrapper_key)
        if isinstance(wrapper, dict):
            container = _first(wrapper, keys)
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


# -- Claude: the stream's boundary-only rate-limit event (2.1.220) -------------
#
# VERIFIED ON CLAUDE CODE 2.1.220 — a free boundary refresh, and nothing more. One
# ordinary ``claude -p`` turn (``status: "allowed"``, not a 429) puts this on the
# stream the hub already drives:
#
#     {"type": "rate_limit_event", "rate_limit_info": {
#        "status": "allowed", "resetsAt": 1785130800, "rateLimitType": "five_hour",
#        "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
#        "isUsingOverage": false}}
#
# camelCase, its own container key, and *no percentage* — so it is not usage. It is
# the one thing the figure carrier cannot keep current: the statusLine sink is only
# written while an interactive session renders its status line, so a stored window's
# ``resets_at`` goes stale between renders, while a hub-driven turn (intake,
# semantic eval) touches the live quota and learns the current boundary for free —
# no probe turn, no private endpoint, no extra quota.
#
# WHY THE BOUNDARY IS WORTH REFRESHING AT ALL. Also verified: the five-hour window
# is anchored to the *first call of that window*, not to a fixed clock grid
# (``resetsAt`` 1785130800 = 05:40Z with a session started 08:40 CST; 1785088200 =
# 17:50Z with one started 20:50 CST the prior evening). So a passed ``resets_at``
# does not mean "a fresh window just started with the same figures" — it means *no
# window is open*, and the next call opens one. That is what the Overview renders
# from these fields, and it is why a percentage may not be presented as the current
# window's once its boundary has moved.
#
# PARTIAL-UPDATE SEMANTICS against the single-snapshot store. The store keeps one
# newest-wins :class:`ClaudeObservation`; a boundary observation carries no figures,
# so recording it as an observation would erase the percentages, and dropping it
# would waste the only fresh signal the hub can get. It is therefore merged, not
# recorded (:func:`merge_window_boundaries`), under three rules:
#
#   * It never creates anything. No stored observation, or no stored window with
#     that key, means nothing is written — "no ``used_percentage`` -> no window"
#     still holds, and Claude stays *unavailable* until the sink observes figures.
#   * It touches only ``resets_at``, and only forward (:func:`_is_newer`). The
#     percentages, plan, and ``observed_ts`` are left exactly as observed —
#     ``observed_ts`` dates the *figures*, so staleness keeps measuring their age
#     and a boundary refresh cannot make an old figure look fresh.
#   * Out-of-order or duplicate events are inert: an equal or earlier boundary
#     writes nothing (a window boundary only advances), so repeated turns cost
#     nothing.
#
# The quota-429 ``system/api_error`` frame from the section above carries the same
# information in the older snake-case shape, and is deliberately *not* read here as
# a second source: it only appears while a hub-driven turn is being rejected, and
# the boundary it would report is the one the next ordinary turn's
# ``rate_limit_event`` carries anyway. One verified carrier, one code path.

_CLAUDE_BOUNDARY_KEYS = ("rate_limit_info",)
"""The key 2.1.220's boundary container arrives under (event ``rate_limit_event``).

Matched on the container rather than on ``type``: the container is the payload the
CLI documents, and keying on it means a re-envelope (a rename, a nesting change)
still parses. Deliberately distinct from :data:`_CLAUDE_CONTAINER_KEYS` so a
figure-less boundary can never be mistaken for a usage container."""

_CLAUDE_BOUNDARY_WINDOW_KEYS = ("rateLimitType", "rate_limit_type")
"""Which window the boundary belongs to. camelCase is what 2.1.220 emits; the
snake-case spelling is accepted as tolerance because the CLI uses it for the same
field elsewhere (the ``api_error`` frame). The value must be one of
:data:`_CLAUDE_WINDOW_KEYS` — an unknown window name is ignored rather than
creating a bucket."""

_CLAUDE_BOUNDARY_RESET_KEYS = ("resetsAt", "resets_at")
"""The reset instant, as Unix epoch seconds (:func:`_claude_reset` also takes ISO).
Both spellings for the same reason as the window key."""


@dataclass(frozen=True)
class WindowBoundary:
    """One window's reset instant, observed *without* any usage figure.

    Deliberately not a :class:`UsageWindow`: it carries no percentage and must not
    be able to become a window on its own. ``key`` is one of
    :data:`_CLAUDE_WINDOW_KEYS` and ``resets_at`` an ISO-8601 UTC instant. Its only
    use is :func:`merge_window_boundaries`, which can move an already-observed
    window's boundary forward and nothing else."""

    key: str
    resets_at: str


def _claude_boundary(raw: object) -> WindowBoundary | None:
    """Normalize one boundary container into a :class:`WindowBoundary`.

    ``None`` unless it names a window we already model *and* an interpretable reset
    instant — the two fields the merge needs. The overage fields
    (``overageStatus`` / ``isUsingOverage`` / ``overageDisabledReason``) and
    ``status`` are read by nobody: no card shows them, so carrying them would add a
    model field, a route field, and a display state for information the operator
    cannot act on. ``status`` is also *not* used to filter — a rejected turn's
    boundary is still the true boundary."""
    if not isinstance(raw, dict):
        return None
    key = _first(raw, _CLAUDE_BOUNDARY_WINDOW_KEYS)
    if not isinstance(key, str) or key.strip() not in _CLAUDE_WINDOW_KEYS:
        return None
    resets_at = _claude_reset(_first(raw, _CLAUDE_BOUNDARY_RESET_KEYS))
    if resets_at is None:
        return None
    return WindowBoundary(key=key.strip(), resets_at=resets_at)


def parse_claude_stream_boundaries(stdout: str) -> tuple[WindowBoundary, ...]:
    """Parse a hub-driven turn's stream-json stdout for window-boundary events.

    Returns one boundary per named window, newest wins within the stream, in
    :data:`_CLAUDE_WINDOW_KEYS` order; an empty tuple when the stdout carries none
    (every turn before 2.1.220), is empty, or is unparseable. Never raises — a
    malformed line is skipped, like the usage parser above."""
    # Substring pre-filter before parsing, like the Codex rollout scan: this runs on
    # every hub-driven turn, and almost every line of a semantic-eval stream is model
    # output that cannot carry the container. Derived from the key tuple so a second
    # spelling cannot be silently skipped here.
    tokens = tuple(f'"{key}"' for key in _CLAUDE_BOUNDARY_KEYS)
    latest: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not any(token in line for token in tokens):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        container = _claude_container(event, _CLAUDE_BOUNDARY_KEYS)
        if container is None:
            continue
        boundary = _claude_boundary(container)
        if boundary is not None:
            latest[boundary.key] = boundary.resets_at
    return tuple(
        WindowBoundary(key=key, resets_at=latest[key])
        for key in _CLAUDE_WINDOW_KEYS
        if key in latest
    )


def merge_window_boundaries(
    observation: ClaudeObservation, boundaries: Iterable[WindowBoundary]
) -> ClaudeObservation | None:
    """``observation`` with each matching window's boundary advanced, or ``None``.

    ``None`` means *nothing changed*, so the caller writes nothing: no boundary
    matched a stored window, or every one was equal to / older than what is stored.
    Per the section note, this only ever moves ``resets_at`` forward on a window the
    figure carrier already established — it adds no window, alters no percentage,
    and leaves ``observed_ts`` dating the figures."""
    by_key = {b.key: b for b in boundaries}
    if not by_key:
        return None
    changed = False
    windows: list[UsageWindow] = []
    for window in observation.windows:
        boundary = by_key.get(window.key)
        if boundary is not None and _is_newer(boundary.resets_at, window.resets_at):
            windows.append(replace(window, resets_at=boundary.resets_at))
            changed = True
        else:
            windows.append(window)
    if not changed:
        return None
    return replace(observation, windows=tuple(windows))


# -- Claude: read the operator-installed statusLine sink ----------------------
#
# THE TRANSITIONAL FALLBACK CARRIER. Everything above establishes that no hub-driven
# turn can observe Claude's subscription windows; for a long time the CLI published
# them only to a statusLine command's stdin, which made this the only figure source
# there was. :mod:`orch_hub.statusline` is the operator-installed writer that
# persists that payload; this is the read half.
#
# The section below supersedes it as the PRIMARY carrier: the CLI's own utilization
# cache needs nothing installed and works with nobody watching. This one stays
# because an existing installation should keep contributing — both feed the same
# store through the same per-window merge, so the sink is an extra reading rather
# than a competitor, and it is the only carrier that also names the plan tier.
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
    """Whether ``candidate`` is a strictly later ISO instant than ``current``.

    One monotonic-forward rule for both store writes, since both are "a later
    observation of the same thing wins": sink ingestion (candidate = the sink's
    observation time) and boundary merging (candidate = an event's ``resets_at``).
    Equal instants — the common case, a repeated read of an unchanged sink or a
    second turn inside the same window — write nothing, and an older value cannot
    regress a fresher one. An unparseable stored value yields to the candidate; an
    unparseable candidate never wins."""
    new = _parse_iso(candidate)
    if new is None:
        return False
    old = _parse_iso(current)
    return old is None or new > old


def _later(a: str | None, b: str | None) -> str | None:
    """The later of two ISO instants (forward-only): ``b`` only when strictly newer.

    Equal or unparseable ``b`` yields ``a``. Used to keep a window's displayed
    reset forward-most and to date a merged observation."""
    return b if _is_newer(b, a) else a


def _earlier(a: str | None, b: str | None) -> str | None:
    """The earlier of two ISO instants — the mirror of :func:`_later`.

    ``b`` wins only when strictly older; equal or unparseable ``b`` yields ``a``.
    The never-over-claim floor for a mixed-moment merge (see
    :func:`merge_observations`): when the merged result holds figures from two
    observations, the older stamp is the only shared one that dates no window ahead
    of the observation that actually supplied its figure."""
    return b if _is_newer(a, b) else a


def _used_at_least(candidate: UsageWindow, current: UsageWindow) -> bool:
    """Whether ``candidate``'s used-percentage is at least ``current``'s.

    Within one window used% only grows, so the higher figure is the later
    measurement; it is also the conservative (never under-report) tiebreaker when
    two windows cannot be told apart. A missing figure ranks lowest."""
    cand = candidate.used_percent if candidate.used_percent is not None else float("-inf")
    cur = current.used_percent if current.used_percent is not None else float("-inf")
    return cand >= cur


def _merge_window(current: UsageWindow, candidate: UsageWindow) -> UsageWindow:
    """Merge one freshly-observed ``candidate`` window into the stored ``current`` one.

    Identity is the figure's *own* window instant (``figure_instant``), never the
    displayable ``resets_at`` a boundary refresh may have advanced ahead of it:

    * stored identity unknown but the candidate's known -> take the candidate. The
      stored figure is from a legacy store (pre-``figure_instant``) whose reset cannot
      be trusted as its identity (a boundary refresh may have advanced it past the
      figure's own window), so a candidate that carries a measured identity is the
      more reliable reading. Both a plain and a boundary-advanced legacy figure thus
      roll over instead of freezing. One-shot: the merged window carries the
      candidate's real ``figure_instant``, and if the stored figure was in fact
      current (an idle reader supplied the lower candidate) the next current reading
      maxes it back;
    * both instants known and farther apart than the Claude identity tolerance ->
      the newer instant is a rollover to a later window, so take that side's figure
      even when its used% is lower (a genuine post-reset reading), and never the
      older one (a pre-reset snapshot);
    * same or tolerance-close instants, or the candidate carries no identity -> take
      the higher used% (the later measurement within one window, and the
      conservative never-under-report choice when the two windows cannot be
      distinguished). Tolerance-close identities independently converge to the
      later instant so subsequent observations compare against the forward-most
      representation of that logical window.

    The displayed ``resets_at`` is always the forward-most of the two, so a
    boundary already advanced past the losing side is never regressed."""
    cur_instant = _parse_iso(current.figure_instant)
    cand_instant = _parse_iso(candidate.figure_instant)
    same_known_window = (
        cur_instant is not None
        and cand_instant is not None
        and abs(cand_instant - cur_instant) <= _CLAUDE_WINDOW_IDENTITY_TOLERANCE
    )
    if cur_instant is None and cand_instant is not None:
        winner = candidate
    elif cur_instant is not None and cand_instant is not None and not same_known_window:
        winner = candidate if cand_instant > cur_instant else current
    else:
        winner = candidate if _used_at_least(candidate, current) else current
    figure_instant = (
        _later(current.figure_instant, candidate.figure_instant)
        if same_known_window
        else winner.figure_instant
    )
    return replace(
        winner,
        resets_at=_later(current.resets_at, candidate.resets_at),
        figure_instant=figure_instant,
    )


def merge_observations(
    current: ClaudeObservation, candidate: ClaudeObservation
) -> ClaudeObservation | None:
    """Merge a freshly-read sink ``candidate`` into the stored ``current`` observation.

    The fix for the stale-writer regression. Every concurrent Claude Code session
    writes the one sink last-write-wins, each publishing the ``rate_limits`` of its
    *own* last API response, so a sink's write stamp is not its measurement time.
    Replacing the store wholesale on write recency therefore lets an idle session's
    hours-old figure lower a current one. Instead each window is merged
    independently by :func:`_merge_window` (jitter-tolerant identity from
    ``figure_instant``, same-window ties by ``max(used_percent)``); a window the
    store never had is added, and one the candidate omits is kept untouched.

    Returns ``None`` when nothing changed — an unchanged re-read, or a wholly
    stale/older candidate — so the caller writes nothing and the stored figures and
    their observation time stand. When something changed, ``observed_ts`` stays one
    provider-level stamp — freshness is a single sampling moment, since both windows
    arrive in one payload: the later stamp for a coherent same-window-set merge, the
    earlier one as a never-over-claim floor when a partial payload mixes two moments
    (see below). Per-window freshness was considered and rejected: the real carrier
    never samples one window without the other, so it would only ever duplicate this
    stamp; the genuine per-window fact — whether a figure is still the open window's —
    is the binary ``figure_instant`` vs ``resets_at`` the card already reads."""
    cand_by_key = {w.key: w for w in candidate.windows}
    merged: list[UsageWindow] = []
    for window in current.windows:
        cand = cand_by_key.get(window.key)
        merged.append(window if cand is None else _merge_window(window, cand))
    stored_keys = {w.key for w in current.windows}
    merged.extend(w for w in candidate.windows if w.key not in stored_keys)
    merged_windows = tuple(merged)
    plan = candidate.plan if candidate.plan is not None else current.plan
    if merged_windows == current.windows and plan == current.plan:
        return None
    # ``observed_ts`` is one provider-level stamp shared by every window, honest only
    # while the store holds a single observation moment. A full statusLine payload
    # keeps it so: both windows arrive together, and the per-window merge picks a
    # uniform winner (used% is monotonic within a window and ``figure_instant`` orders
    # rollovers), so the later stamp dates the whole coherent result. The one
    # exception is a PARTIAL payload — each window is optional (``_claude_windows``),
    # so a read carrying only one window keeps the other from the stored, differently
    # timed observation, mixing two moments. There the never-over-claim floor is the
    # OLDER stamp, so no window is dated newer than the observation that supplied its
    # figure. Same window set -> coherent -> later; different -> mixed -> earlier.
    if stored_keys == {w.key for w in candidate.windows}:
        observed_ts = _later(current.observed_ts, candidate.observed_ts)
    else:
        observed_ts = _earlier(current.observed_ts, candidate.observed_ts)
    return ClaudeObservation(
        observed_ts=observed_ts,
        plan=plan,
        windows=merged_windows,
    )


# -- Claude: the authenticated ``/usage`` cache (the primary carrier) ----------
#
# VERIFIED ON CLAUDE CODE 2.1.220 — the carrier that does not depend on anybody
# rendering a status line. Every section above reads a signal some *other* activity
# happens to leave behind: the sink needs an interactive session drawing its status
# line, the stream needs a hub-driven turn. Both go quiet exactly when the operator
# most wants the number — an idle Mac, or a stretch of orchestrator runs launched
# headless. The CLI, though, keeps its own authenticated utilization fetch on disk:
#
#     ~/.claude.json -> "cachedUsageUtilization": {
#       "fetchedAtMs": 1785470888223,
#       "utilization": {
#         "five_hour":  {"utilization": 9,  "resets_at": "2026-07-31T07:19:59.214956+00:00"},
#         "seven_day":  {"utilization": 43, "resets_at": "2026-07-31T21:59:59.214980+00:00"},
#         "seven_day_opus": null, "seven_day_sonnet": null, ...,
#         "limits": [{"kind": "session",    "percent": 9,
#                     "resets_at": "2026-07-31T07:19:59.214956+00:00", ...},
#                    {"kind": "weekly_all", "percent": 43,
#                     "resets_at": "2026-07-31T21:59:59.214980+00:00", ...},
#                    {"kind": "weekly_scoped", "percent": 6,
#                     "resets_at": "2026-07-31T22:00:00.215292+00:00",
#                     "scope": {"model": {"display_name": "Fable"}}, ...}],
#         "extra_usage": {...}, "spend": {...}},
#       "accountUuid": "<uuid>"}
#
# THE FIGURE IS READ FROM ``limits[].percent``, NEVER FROM ``utilization``. The
# stream/sink parser above rejects ``utilization`` by name because its unit is
# source-dependent — a 0..1 fraction in the header-derived cache (the statusLine
# payload builds ``used_percentage`` as ``utilization * 100``), an already-0-100
# number here. Adjudicating between the two units numerically, by checking the field
# against its ``limits[].percent`` twin, is what this parser first did, and the low
# end of the range is where that fails: at 1% used the two readings are 0.01 and 1, so
# a slack wide enough for two separately-computed fields to round differently also
# admits a reading wrong by 100x. So the ambiguity is not adjudicated, it is avoided —
# ``percent`` is a name that admits one unit, and it carries the same numbers (9 and
# 43 above), so the ambiguous field is simply never read.
#
# What the window map is still for: it is the shape this carrier iterates, and it
# holds ``resets_at`` -> ``figure_instant``, which is what makes a cache reading and a
# sink reading of the same window ONE window in :func:`merge_observations` rather than
# two racing ones. So the two halves are paired: :func:`_cache_figures` keys each
# ``limits[]`` entry by the CLI's own ``kind`` (``session`` -> ``five_hour``,
# ``weekly_all`` -> ``seven_day``; never by position — the list is heterogeneous,
# carrying per-model ``weekly_scoped`` entries too), and a window is published only
# when both halves name the SAME reset instant. That pairing witness is
# unit-independent, and it also catches a ``limits`` list that has gone stale against
# the window map, since it would still name the previous boundary. Unpaired or
# contradicted, the window drops and the card degrades to *unavailable* — the mandated
# graceful state — instead of rendering a figure this file cannot vouch for.
#
# REFRESHED BY ``claude -p "/usage"``, NOT BY A PTY. The task scope proposed driving
# an interactive CLI in a pseudo-terminal and typing the slash command. Print mode
# turned out to run the same command with none of the machinery, and measurement on
# 2.1.220 is what settled it:
#
#   * ``num_turns: 0``, ``total_cost_usd: 0``, ``duration_api_ms: 0`` — ``/usage`` is
#     handled inside the CLI and never reaches the model. That is the scope's
#     "sends no model prompt" rule, verifiable per run rather than argued, and
#     :meth:`ClaudeUsageCollector._inspect` asserts it on every turn.
#   * ~2.3s wall clock, exit 0, and it refreshes ``fetchedAtMs``.
#   * No workspace-trust dialog (print mode skips it) and NO ``projects`` entry
#     written for the scratch cwd. The PTY route failed both: the trust prompt ate
#     the ``/usage`` keystroke, and answering it blind would have written the hub's
#     throwaway directory into the user's own ``~/.claude.json`` — one accumulating
#     entry per poll.
#   * No PTY, so no terminal emulation, no ANSI scraping of a TUI whose layout is not
#     a contract, and no orphaned interactive process to reap.
#
# The prose the command prints (session/skill analytics) is deliberately NOT parsed:
# the structured cache is the source of truth, and the human render is not.
#
# WHAT IS READ. Only the whitelisted rate-limit fields — the two windows' percentage
# and reset instant, plus ``fetchedAtMs``. ``~/.claude.json`` is the CLI's whole
# config file, so the whitelist is doing real work: ``accountUuid`` sits inside the
# very container being parsed, and ``oauthAccount``, ``userID``, ``machineID``,
# ``projects`` (every path the operator has opened) and the rest are siblings. None
# is read, none is persisted, and none is logged. No credential is touched: this
# reads a file, and the CLI does its own authenticated fetch.
#
# WHY ``fetchedAtMs`` IS MANDATORY. It is the CLI's own record of when the figures
# were fetched, so it — never the poll's wall clock — is the observation time. A
# refresh that fails leaves the file untouched, and stamping the unchanged figures
# with "now" would present them as freshly observed and silently reset the staleness
# the Overview exists to show. So a cache with no usable ``fetchedAtMs`` yields no
# observation at all, rather than one the reader has to date itself.
#
# The result is an ordinary :class:`ClaudeObservation` fed through the same store and
# the same per-window :func:`merge_observations` as the sink — one read path, one
# staleness rule, and a rollover/idle-writer race already solved. It carries no plan
# (this container names no tier), which the merge already handles by keeping the
# stored one.

CLAUDE_CONFIG_NAME = ".claude.json"
"""The Claude CLI's config file, in the user's home."""

DEFAULT_CLAUDE_CONFIG = Path.home() / CLAUDE_CONFIG_NAME
"""Default path of the CLI config carrying :data:`CLAUDE_USAGE_CACHE_KEY`.
Injectable at the collector so tests never read the real operator file."""

CLAUDE_USAGE_CACHE_KEY = "cachedUsageUtilization"
"""Key under which the CLI stores its authenticated utilization fetch."""

MAX_CLAUDE_CONFIG_BYTES = 16 * 1024 * 1024
"""Defensive size guard on the CLI config, mirroring the sink/rollout bounds. It
grows with the operator's project history, so this is far above any real file —
refusing an implausible one keeps a corrupt/runaway config off the read path, where
it degrades to *unavailable* like any other unusable source."""

USAGE_CACHE_FRESH_SECONDS = 300
"""How new the CLI's own cached fetch must be for a refresh to be skipped entirely.

The CLI already re-fetches on its own schedule and serves the cache in between
(measured: a second ``/usage`` a minute later left ``fetchedAtMs`` untouched), so
polling harder cannot produce fresher data — it only spends quota-check calls. Well
inside :data:`USAGE_STALE_AFTER_SECONDS`, so a skipped refresh never leaves the card
reading stale.

It is also the threshold :meth:`UsageService._read_claude` asks for a refresh at,
deliberately the same number: a read that requested one any sooner would be asking
for work this gate would refuse, and any later would leave the card older than it
needs to be. Kept an internal constant rather than an environment setting (like
:data:`USAGE_CACHE_TTL_SECONDS` and the run monitor's interval) — it is derived from
the CLI's own measured refresh behavior, not from operator preference."""

USAGE_POLL_MIN_INTERVAL_SECONDS = 300
"""Floor between two refresh *attempts*, whatever their outcome.

Distinct from the freshness gate above, which only skips when there is fresh data to
reuse: when the CLI is missing, unauthenticated, or hanging, there is none, and this
is what stops every Overview load from re-running a doomed subprocess. Internal for
the same reason as the gate above — a bound on hub behavior, not a preference."""

USAGE_POLL_TIMEOUT_SECONDS = 60
"""Hard bound on one refresh turn (measured ~2.3s, so this is headroom for a cold
start, not an expected wait). The turn is a display-only background refresh — it may
never become an unbounded wait behind an Overview read."""

REFRESH_IN_FLIGHT = "refresh already running"
REFRESH_CACHE_FRESH = "cached usage still fresh"
REFRESH_BACKOFF = "retry interval not elapsed"
REFRESH_OK = "usage cache refreshed"
REFRESH_NO_CACHE = "no usable usage cache"
REFRESH_TIMEOUT = "usage refresh timed out"
REFRESH_MODEL_CALL = "usage refresh made a model call"
REFRESH_UNSUPPORTED = "usage refresh returned an unsupported envelope"
"""Stable outcome slugs for one :meth:`ClaudeUsageCollector.refresh` call. Named
constants rather than inline strings so tests and any future display can assert on
an outcome instead of matching prose."""

_CLAUDE_CACHE_WINDOW_CONTAINER = "utilization"
"""Sub-key holding the per-window map — the shape this carrier iterates, and the
source of each window's reset instant.

Confusingly the same name as the per-window figure field the CLI nests inside it
(``utilization.five_hour.utilization``), which this carrier deliberately does not
read: that field is the ambiguous one (section note)."""

_CLAUDE_CACHE_LIMITS_KEY = "limits"
"""Sibling of the per-window map, holding the same usage under a field name that
admits one unit (``percent``) — this carrier's figure source."""

_CLAUDE_CACHE_LIMIT_KINDS = {"five_hour": "session", "seven_day": "weekly_all"}
"""Which ``limits[]`` entry carries which window's figure, by the CLI's own ``kind``.

The list is heterogeneous — it also carries ``weekly_scoped`` per-model entries (6%
for Fable in the capture above) — so the pairing is by kind, never by position, and
an unrecognized kind supplies nothing."""


def _cache_percent(value: object) -> float | None:
    """A used-percentage from the CLI's utilization cache, range-guarded to 0-100.

    The extra guard over :func:`_percent` is cheap insurance on a field this hub does
    not own: a value outside 0-100 is not a percentage in any reading, so it yields
    ``None`` (the window drops, the card degrades) rather than a figure that would
    render as a wrong bar."""
    percent = _percent(value)
    if percent is None or not 0.0 <= percent <= 100.0:
        return None
    return percent


@dataclass(frozen=True)
class _CacheFigure:
    """One ``limits[]`` entry reduced to what a window may be published from: the
    unambiguously-named percentage, and the reset instant it was measured for — which
    is what pairs it to a window of the map beside it."""

    used_percent: float
    resets_at: str


def _cache_figures(container: dict) -> dict[str, _CacheFigure]:
    """The publishable ``limits[]`` figure for each window key it covers.

    Empty when the container carries no usable ``limits`` list, which leaves every
    window unpublishable — an unrecognized schema degrades to *unavailable* rather
    than falling back to the ambiguous ``utilization`` beside it. An entry needs a
    recognized ``kind``, an in-range ``percent``, and a reset instant to pair on;
    without all three it supplies nothing. The first usable entry of a kind wins, so a
    later duplicate cannot displace the figure a window was already published from."""
    entries = container.get(_CLAUDE_CACHE_LIMITS_KEY)
    if not isinstance(entries, list):
        return {}
    by_kind: dict[str, _CacheFigure] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        percent = _cache_percent(entry.get("percent"))
        resets = _claude_reset(_first(entry, _CLAUDE_RESET_KEYS))
        if not isinstance(kind, str) or percent is None or resets is None:
            continue
        by_kind.setdefault(kind, _CacheFigure(used_percent=percent, resets_at=resets))
    return {
        key: by_kind[kind]
        for key, kind in _CLAUDE_CACHE_LIMIT_KINDS.items()
        if kind in by_kind
    }


def _cache_window(key: str, raw: object, figure: _CacheFigure | None) -> UsageWindow | None:
    """Normalize one window of the utilization cache into a :class:`UsageWindow`.

    Same rules as the sink carrier (:func:`_claude_window`): a window without a usable
    percentage yields ``None`` rather than a figure-less stub, ``used``/``limit`` stay
    ``None`` because the provider reports percentages, and ``figure_instant`` is the
    reset this figure was measured against — so a cache-sourced and a sink-sourced
    reading of the same window share one identity and merge as the same window
    instead of racing as two.

    Plus the pairing rule only this carrier needs (section note): the figure comes
    from ``figure`` — the unambiguously-named ``limits[].percent`` of this window's
    kind — and it is published here only when this window and that entry name the same
    reset instant. The map supplies the shape and that instant; the ambiguous
    ``utilization`` sitting inside it is never read, so no unit question reaches the
    display. Unpaired or contradicted, the window drops."""
    if not isinstance(raw, dict) or figure is None:
        return None
    resets = _claude_reset(_first(raw, _CLAUDE_RESET_KEYS))
    if resets is None or resets != figure.resets_at:
        return None
    used = figure.used_percent
    minutes = _CLAUDE_WINDOW_MINUTES.get(key)
    return UsageWindow(
        key=key,
        label=_window_label(minutes),
        window_minutes=minutes,
        used_percent=used,
        remaining_percent=_remaining(used),
        resets_at=resets,
        figure_instant=resets,
    )


def _fetched_at(value: object) -> str | None:
    """The cache's ``fetchedAtMs`` (epoch **milliseconds**) as an ISO-8601 UTC instant.

    Milliseconds, unlike every reset field on these carriers — hence its own
    converter rather than :func:`_claude_reset`. ``None`` for a missing or
    uninterpretable value, which the parser treats as fatal: see the section note on
    why an undated cache must not be dated by the reader. The millisecond scaling
    has to be guarded here rather than left to :func:`_iso_from_epoch`'s own
    boundary: a JSON integer too large for a float raises before that call, in a
    parser that promises never to raise."""
    if not _is_number(value):
        return None
    try:
        seconds = float(value) / 1000.0
    except (OverflowError, ValueError):
        return None
    return _iso_from_epoch(seconds)


def parse_claude_usage_cache(text: str) -> ClaudeObservation | None:
    """Parse the CLI config's utilization cache into an observation.

    Reads only the whitelisted rate-limit fields of
    :data:`CLAUDE_USAGE_CACHE_KEY` — the two windows' percentage and reset, stamped
    with the cache's own ``fetchedAtMs``. Everything else in the file, including the
    ``accountUuid`` sitting in the same container, is dropped by construction.

    Returns ``None`` — no observation, so the store keeps whatever it had — for
    anything unusable: not JSON, not an object, no cache container, no usable
    ``fetchedAtMs``, or no window paired with a ``limits[].percent`` entry of its kind
    naming the same reset instant. ``plan`` is always ``None``:
    this container names no subscription tier, and inventing one from an account
    record is exactly what the whitelist forbids. Never raises — this is a display
    read path over a file another program owns."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cache = data.get(CLAUDE_USAGE_CACHE_KEY)
    if not isinstance(cache, dict):
        return None
    observed_ts = _fetched_at(cache.get("fetchedAtMs"))
    if observed_ts is None:
        return None
    container = cache.get(_CLAUDE_CACHE_WINDOW_CONTAINER)
    if not isinstance(container, dict):
        return None
    figures = _cache_figures(container)
    windows = tuple(
        w
        for key in _CLAUDE_WINDOW_KEYS
        if (w := _cache_window(key, container.get(key), figures.get(key))) is not None
    )
    if not windows:
        return None
    return ClaudeObservation(observed_ts=observed_ts, plan=None, windows=windows)


def read_claude_usage_cache(path: Path) -> ClaudeObservation | None:
    """Read the CLI config at ``path`` for its utilization cache, or ``None``.

    ``None`` covers every no-feed case identically — the CLI is not installed, has
    never fetched utilization, or the file is unreadable, oversized
    (:data:`MAX_CLAUDE_CONFIG_BYTES`), or malformed — so each degrades to the same
    *unavailable* card rather than to an error or to fabricated figures."""
    try:
        if path.stat().st_size > MAX_CLAUDE_CONFIG_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    return parse_claude_usage_cache(text)


@dataclass(frozen=True)
class UsageRunResult:
    """One completed ``claude -p "/usage"`` turn: captured output and how it ended.

    ``timed_out`` is a normal result rather than an exception so the injected seam
    stays a plain function returning data — a test fake expresses a hang by setting
    the flag, exactly as the real runner does when it kills the process."""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False


UsageRunner = Callable[[Sequence[str], Path, float], UsageRunResult]
"""Runs one usage turn to completion in ``cwd``, bounded by a timeout in seconds.

The injected subprocess seam, like the run manager's ``spawner`` and the semantic
backend's ``runner``: tests inject a fake so every mechanic is deterministic, and
the thin real implementation (:func:`_default_usage_runner`) stays untested."""


def _default_usage_runner(argv: Sequence[str], cwd: Path, timeout: float) -> UsageRunResult:
    """Run ``claude`` to completion under a hard timeout (the real runner).

    Uses the augmented PATH shared with detached orchestrator, intake, and semantic
    runs (:func:`orch_hub.runs._orchestrator_env`): the hub normally runs under
    launchd with a sparse PATH while ``claude`` is installed under the user's home,
    and this launches it by name. ``capture_output`` drains both pipes so the turn
    cannot deadlock on a full one, and a timeout is reported as a result rather than
    raised. Untested, like the sibling runners it mirrors."""
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=_orchestrator_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return UsageRunResult(timed_out=True)
    return UsageRunResult(
        stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode
    )


@dataclass(frozen=True)
class UsageRefreshResult:
    """The outcome of one :meth:`ClaudeUsageCollector.refresh` call.

    ``observation`` is the best usage currently on disk — which is deliberately NOT
    ``None`` just because the refresh failed: a stale-but-real reading, still dated
    by its own ``fetchedAtMs``, is what the card should keep showing. The one
    exception is :data:`REFRESH_IN_FLIGHT`, which sheds without reading anything,
    since the caller holding the gate is already doing that work. ``refreshed`` says
    whether a turn actually ran and succeeded, and ``detail`` which of the
    ``REFRESH_*`` outcomes it was."""

    observation: ClaudeObservation | None
    refreshed: bool
    detail: str


class ClaudeUsageCollector:
    """Refreshes Claude's authenticated utilization cache, bounded and single-flight.

    The one carrier that works with nobody watching: :meth:`refresh` runs
    ``claude -p "/usage"`` — a local CLI command, no model call (see the section
    note) — and reads back the structured cache the CLI keeps in its config file.

    Every way this could misbehave as a *display* sidecar is gated:

    * **Single-flight.** The gate is taken non-blocking, so a second caller returns
      :data:`REFRESH_IN_FLIGHT` immediately instead of queueing. A refresh triggered
      from a read path must never make a caller wait on another caller's subprocess.
    * **Cheap when it can be.** A cache the CLI itself refreshed recently is reused
      (:data:`USAGE_CACHE_FRESH_SECONDS`) without launching anything.
    * **Bounded when it cannot.** Attempts are floored by
      :data:`USAGE_POLL_MIN_INTERVAL_SECONDS` and each turn by
      :data:`USAGE_POLL_TIMEOUT_SECONDS`, so a missing, unauthenticated, or hanging
      CLI costs one bounded subprocess per interval, not one per Overview load.
    * **Never destructive.** Failure of any kind returns the previous observation
      with its own timestamp intact; nothing is written to the CLI's config, and the
      scratch cwd is removed after the turn.

    It owns no store: it returns an observation and lets :class:`UsageService` merge
    it, so the cache carrier goes through the same per-window merge as the sink."""

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        runner: UsageRunner = _default_usage_runner,
        clock: Callable[[], dt.datetime] = _utc_now,
        cache_fresh_after: float = USAGE_CACHE_FRESH_SECONDS,
        min_interval: float = USAGE_POLL_MIN_INTERVAL_SECONDS,
        timeout: float = USAGE_POLL_TIMEOUT_SECONDS,
    ):
        self._cache_path = cache_path if cache_path is not None else DEFAULT_CLAUDE_CONFIG
        self._runner = runner
        self._clock = clock
        self._cache_fresh_after = cache_fresh_after
        self._min_interval = min_interval
        self._timeout = timeout
        # Non-reentrant and taken non-blocking (see the class note): this serializes
        # turns, it does not queue callers behind one.
        self._gate = threading.Lock()
        self._last_attempt: dt.datetime | None = None

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    def read_cache(self) -> ClaudeObservation | None:
        """The current on-disk observation, without refreshing anything."""
        return read_claude_usage_cache(self._cache_path)

    def refresh(self) -> UsageRefreshResult:
        """Refresh the utilization cache if it is worth it, and return what is on disk.

        Gated in the order documented on the class: single-flight, then freshness,
        then the attempt floor, then one bounded turn. Whatever happens, the returned
        observation is a real read of the cache file — so a skipped or failed refresh
        yields the previous figures with their own ``fetchedAtMs``, never a
        re-dated copy."""
        if not self._gate.acquire(blocking=False):
            return UsageRefreshResult(None, False, REFRESH_IN_FLIGHT)
        try:
            cached = self.read_cache()
            now = self._clock()
            if cached is not None and not _older_than(
                cached.observed_ts, now, self._cache_fresh_after
            ):
                return UsageRefreshResult(cached, False, REFRESH_CACHE_FRESH)
            if self._last_attempt is not None and (
                (now - self._last_attempt).total_seconds() < self._min_interval
            ):
                return UsageRefreshResult(cached, False, REFRESH_BACKOFF)
            self._last_attempt = now
            failure = self._run_turn()
            # Read back regardless of the verdict: the cache is the source of truth,
            # and a turn can refresh it even on a path we call a failure.
            observation = self.read_cache() or cached
            if failure is not None:
                return UsageRefreshResult(observation, False, failure)
            if observation is None:
                return UsageRefreshResult(None, False, REFRESH_NO_CACHE)
            return UsageRefreshResult(observation, True, REFRESH_OK)
        finally:
            self._gate.release()

    def _run_turn(self) -> str | None:
        """Run one usage turn; ``None`` on success, else the failing ``REFRESH_*`` slug.

        The turn runs from a fresh empty scratch dir rather than any managed repo —
        the same isolation the semantic backend uses, and here it also keeps the CLI
        from attributing the poll to a real project. Print mode writes no workspace
        entry for it (verified), so the directory leaves no trace once removed."""
        sandbox = Path(tempfile.mkdtemp(prefix="orch-hub-usage-"))
        try:
            result = self._runner(self._argv(), sandbox, self._timeout)
        except OSError as exc:
            # The CLI is missing or unrunnable — an ordinary state (Claude need not be
            # installed), so it degrades like any other absent source.
            return f"usage refresh could not start: {type(exc).__name__}"
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
        return self._inspect(result)

    def _inspect(self, result: UsageRunResult) -> str | None:
        """Classify a finished turn; ``None`` when it was a clean, model-free refresh.

        The model-call check is the enforcement half of the scope's no-model-prompt
        rule. ``/usage`` is handled inside the CLI today (``num_turns``/cost 0), but
        that is the CLI's behavior, not a contract it owes us — so every turn is
        checked, and a version that started routing it to the model would be reported
        and then throttled by the attempt floor rather than quietly billing the
        operator's subscription on a display refresh.

        Which is why *silence* is a failure here too. The turn is launched with
        ``--output-format json``, so a supported CLI answers with an envelope
        reporting its own model-call accounting; stdout that is not JSON, not an
        object, or an object whose ``num_turns``/``total_cost_usd`` are absent or
        non-numeric is a version whose turn we cannot show stayed local. Passing that
        as a clean refresh would leave the check enforcing nothing on exactly the
        versions it exists for, so it is :data:`REFRESH_UNSUPPORTED`: the figures on
        disk still surface, and the attempt floor stops the hub re-running an
        unverifiable turn per Overview load. Numeric accounting that is anything but
        an explicit zero is read the other way — as a turn that did reach the model."""
        if result.timed_out:
            return REFRESH_TIMEOUT
        if result.returncode != 0:
            # Authentication failure, an unsupported command, a broken install: all
            # arrive here as a nonzero exit. stderr is deliberately not echoed into
            # the detail — this file's diagnostics can carry account context.
            return f"usage refresh failed (exit {result.returncode})"
        report = _json_object(result.stdout)
        if report is None:
            return REFRESH_UNSUPPORTED
        if report.get("is_error") is True:
            return "usage refresh reported an error"
        turns, cost = report.get("num_turns"), report.get("total_cost_usd")
        if not _is_number(turns) or not _is_number(cost):
            return REFRESH_UNSUPPORTED
        if turns != 0 or cost != 0:
            return REFRESH_MODEL_CALL
        return None

    def _argv(self) -> list[str]:
        """Build the argv for one local, model-free usage turn.

        ``-p`` is what makes this work at all: print mode skips the workspace-trust
        dialog and writes no ``projects`` entry for the scratch cwd, where an
        interactive PTY session would do neither. ``/usage`` is passed as the prompt
        positionally — the CLI resolves the slash command locally.

        The isolation flags mirror :meth:`ClaudeSemanticBackend._argv` so a hub-driven
        Claude turn means the same thing everywhere: ``--tools ""`` leaves the turn no
        built-in tool, ``--safe-mode`` keeps operator/project customizations out,
        ``--no-session-persistence`` leaves no transcript, and ``--permission-mode
        plan`` blocks writes; ``--dangerously-skip-permissions`` is never added. No
        ``--model`` / ``--effort``: nothing here reaches a model, so pinning one would
        only invite the assumption that something does. ``--tools ""`` is followed by
        another flag, never by the positional prompt, so the variadic option cannot
        swallow ``/usage`` as a tool name."""
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--tools",
            "",
            "--safe-mode",
            "--no-session-persistence",
            "--permission-mode",
            "plan",
            "/usage",
        ]


def _json_object(text: str) -> dict | None:
    """``text`` as a JSON object, or ``None`` when it is not one. Never raises."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _older_than(observed_ts: str | None, now: dt.datetime, seconds: float) -> bool:
    """Whether ``observed_ts`` is more than ``seconds`` behind ``now``.

    An unknown/unparseable stamp counts as old: it cannot be shown to be fresh, and
    the refresh it triggers is bounded anyway. The mirror of :func:`_is_stale`, which
    answers the display question against a fixed threshold; this one answers the
    collector's, against a caller-supplied one."""
    observed = _parse_iso(observed_ts)
    if observed is None:
        return True
    return (now - observed).total_seconds() > seconds


# -- aggregation service ------------------------------------------------------


def _safe(provider: str, fn: Callable[[], ProviderUsage]) -> ProviderUsage:
    """Run one provider read, turning any failure into a fail-soft *unavailable*
    snapshot so one provider can never break the other or the whole route."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — display-only; degrade, never raise
        return ProviderUsage.unavailable(provider, f"usage read failed: {type(exc).__name__}")


UsageRefreshDispatch = Callable[[Callable[[], None]], None]
"""Runs one background Claude usage refresh. The default detaches it onto a daemon
thread; tests inject a synchronous dispatch for deterministic assertions."""


def _thread_dispatch(refresh: Callable[[], None]) -> None:
    """Default dispatch: run the refresh on a short-lived daemon thread.

    A refresh is a bounded subprocess (:data:`USAGE_POLL_TIMEOUT_SECONDS`), and the
    two things that ask for one — an Overview read finding stale figures, a run
    reaching its terminal state — must not wait on it. Detaching is what makes the
    trigger non-blocking; the thread is daemon so it never holds up shutdown."""
    threading.Thread(target=refresh, daemon=True, name="orch-hub-usage").start()


ClaudeUsageObserver = Callable[[str], None]
"""Best-effort seam: given one hub-driven ``claude -p`` turn's raw stdout, update
Claude usage from whatever that stream supports.

The hub-driven ``claude -p`` backends (the intake agent and the semantic backend)
call it once per turn, so a turn the hub already runs refreshes Claude usage without
a probe turn — and without those modules importing usage internals (they take a
plain ``Callable[[str], None]``). What it can extract depends on the installed CLI:
no verified version carries figures there, but 2.1.220 carries a window *boundary*,
so on that version an ordinary turn keeps the stored window's ``resets_at`` current
for free. It MUST never raise: usage bookkeeping can never affect a turn.
:meth:`UsageService.claude_observer` is the production implementation; the backends
default to a no-op when no observer is wired."""


class UsageService:
    """Aggregate Claude + Codex subscription usage behind a bounded cache.

    Reads each provider independently and fail-soft (:func:`_safe`), so a missing
    or broken source degrades only its own card. The combined snapshot is cached
    for :data:`USAGE_CACHE_TTL_SECONDS` to keep rollout scans and store reads off
    a burst of Overview refreshes; :meth:`record_claude_observation` writes a new
    observation and invalidates the cache. Nothing here gates an operation anyone
    requested — it is a sidecar over the two CLIs' own signals, and the single
    consumer that reads it as a decision only paces the hub's own automatic
    Layer 2 turns (module docstring).

    ``claude_collector`` is the primary Claude carrier (:class:`ClaudeUsageCollector`)
    and is deliberately NOT default-constructed: a live collector reads the
    operator's real ``~/.claude.json`` and can launch a subprocess, so it is wired
    by the app layer (:func:`orch_hub.api.create_app`) alongside the other concrete
    Claude backends, and a service built without one is inert — it reads the store
    and the statusLine sink and asks for nothing."""

    def __init__(
        self,
        config: Config,
        *,
        claude_store: ClaudeUsageStore | None = None,
        claude_collector: ClaudeUsageCollector | None = None,
        refresh_dispatch: UsageRefreshDispatch = _thread_dispatch,
        clock: Callable[[], dt.datetime] = _utc_now,
    ):
        self._config = config
        self._store = claude_store if claude_store is not None else ClaudeUsageStore(config.data_dir)
        self._collector = claude_collector
        self._refresh_dispatch = refresh_dispatch
        self._clock = clock
        self._cache: tuple[dt.datetime, SubscriptionUsage] | None = None
        # Serializes this process's writes to the Claude store. Two of them are
        # read-modify-write (sink ingestion compares observation times; a boundary
        # merge rewrites one window of the stored snapshot) and both can run
        # concurrently: hub-driven turns observe from daemon threads while an HTTP
        # read ingests the sink. ``record`` itself is atomic, so an unguarded race
        # loses no file — but it can lose a *fresher* observation by writing a
        # snapshot read before it. Non-reentrant: the guarded methods must touch the
        # store directly, never through each other.
        self._claude_write_lock = threading.Lock()

    @property
    def store(self) -> ClaudeUsageStore:
        return self._store

    @property
    def statusline_sink(self) -> Path:
        """Where the operator-installed statusLine writer publishes (read-only here)."""
        return self._config.data_dir / USAGE_DIR_NAME / CLAUDE_STATUSLINE_NAME

    def record_claude_observation(self, observation: ClaudeObservation) -> None:
        """Persist a fresh Claude observation and drop the cached snapshot."""
        with self._claude_write_lock:
            self._store.record(observation)
        self._cache = None

    def request_claude_refresh(self) -> None:
        """Ask for a Claude usage refresh in the background; returns immediately.

        The trigger the whole poller exists for, and it is *non-blocking by
        construction*: the work is handed to :data:`UsageRefreshDispatch` (a daemon
        thread in production) and this returns without waiting, so neither an
        Overview read nor a run reaching its terminal state ever waits on a
        subprocess. Whether a refresh is worth running at all is the collector's
        decision, not this one's (freshness, attempt floor, single-flight), so
        calling this too often is cheap rather than wrong.

        Wired to nothing when no collector was injected, and swallowing even a
        failure to *start* the worker: this is display bookkeeping, and its caller
        is always doing something more important."""
        if self._collector is None:
            return
        try:
            self._refresh_dispatch(self._refresh_claude)
        except Exception:  # noqa: BLE001 — display-only; never disturb the caller
            pass

    def _refresh_claude(self) -> None:
        """Run one refresh and merge whatever the cache holds afterwards.

        The whole body of the background worker. A failed, shed, or skipped refresh
        still returns the observation currently on disk with its own ``fetchedAtMs``
        (:class:`UsageRefreshResult`), which merges to *no change* — so a failure
        cannot re-date old figures as newly observed, and the card degrades to
        stale rather than to unavailable."""
        collector = self._collector
        if collector is None:
            return
        try:
            result = collector.refresh()
        except Exception:  # noqa: BLE001 — display-only; a failed poll changes nothing
            return
        self._merge_claude_observation(result.observation)

    def _merge_claude_observation(self, observation: ClaudeObservation | None) -> None:
        """Merge one carrier's observation into the store, per window.

        The shared write path for both figure carriers (the utilization cache and
        the statusLine sink): each merges into the store rather than replacing it,
        so two carriers reading the same window contribute one window instead of
        racing (:func:`merge_observations`). The lock is held across read-merge-write
        because a concurrent boundary merge is also a read-modify-write and would
        otherwise interleave and drop this carrier's figures. Nothing changed means
        nothing is written, and the stored observation time stands."""
        if observation is None:
            return
        with self._claude_write_lock:
            current = self._store.latest()
            if current is None:
                self._store.record(observation)
            else:
                merged = merge_observations(current, observation)
                if merged is None:
                    return
                self._store.record(merged)
        self._cache = None

    def refresh_claude_boundaries(self, boundaries: Iterable[WindowBoundary]) -> None:
        """Advance stored window boundaries from a hub-driven turn's stream events.

        A partial update, not an observation (see the boundary section note): it
        moves ``resets_at`` forward on windows the figure carrier already observed
        and writes nothing otherwise — no stored observation, no matching window, or
        no forward movement all mean the store is left exactly as it was. Claude
        therefore stays *unavailable* until real figures arrive, and the stored
        percentages and their ``observed_ts`` are never touched."""
        with self._claude_write_lock:
            current = self._store.latest()
            if current is None:
                return
            merged = merge_window_boundaries(current, boundaries)
            if merged is None:
                return
            self._store.record(merged)
        self._cache = None

    def claude_observer(self) -> ClaudeUsageObserver:
        """A best-effort callback that feeds one ``claude -p`` turn's stdout to the store.

        This is the :data:`ClaudeUsageObserver` seam the hub-driven Claude backends
        (intake agent, semantic backend) call each turn, so a turn the hub already
        runs refreshes Claude usage without a probe turn. Figures win when the stream
        carries any (:func:`parse_claude_stream_usage`, stamped with this service's
        clock) — that snapshot already contains the turn's own boundaries, so the
        merge is skipped. Otherwise the turn's boundary events, if any, are merged
        into the stored snapshot. A stream with neither records nothing, and every
        exception is swallowed so usage bookkeeping can never affect the driving turn.
        Writes are serialized and atomic, so intake and semantic turns on separate
        daemon threads can observe at once."""
        def observe(stdout: str) -> None:
            try:
                observation = parse_claude_stream_usage(
                    stdout, observed_ts=self._clock().isoformat()
                )
                if observation is not None:
                    self.record_claude_observation(observation)
                    return
                boundaries = parse_claude_stream_boundaries(stdout)
                if boundaries:
                    self.refresh_claude_boundaries(boundaries)
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

    def provider_usage(self, provider: str) -> ProviderUsage:
        """Read *one* provider's snapshot, fail-soft, bypassing the aggregate cache.

        The aggregate :meth:`get_usage` is the display read: it always touches both
        providers, and Codex's half scans up to :data:`MAX_ROLLOUT_SCAN` session
        rollouts. A consumer that only cares about one provider should not pay for
        the other one, so this reads exactly the asked-for provider through the
        same fail-soft wrapper. It was built for the automatic Layer 2 dispatcher's
        capacity read, which no longer exists; it remains as the single-provider
        read of a service whose aggregate is the expensive one.

        Uncached on purpose: :meth:`get_usage` caches a whole snapshot for the
        Overview's polling clients, while this answers a caller that wants the
        freshest figures the store holds. Both go through the same per-provider
        readers, so a Claude read still ingests both carriers and still requests a
        background refresh when the observation has aged past the collector's
        horizon — reading keeps the figures fresh as a side effect of consulting
        them. An unknown provider key is unavailable, never an error."""
        now = self._clock()
        if provider == PROVIDER_CLAUDE:
            return _safe(PROVIDER_CLAUDE, lambda: self._read_claude(now))
        if provider == PROVIDER_CODEX:
            return _safe(
                PROVIDER_CODEX,
                lambda: read_codex_usage(self._config.codex_home, now=now),
            )
        return ProviderUsage.unavailable(provider, f"unknown usage provider {provider!r}")

    def _read_providers(self, now: dt.datetime) -> Iterable[ProviderUsage]:
        """Read every provider, each fail-soft and independent (Claude, then Codex)."""
        yield _safe(PROVIDER_CLAUDE, lambda: self._read_claude(now))
        yield _safe(PROVIDER_CODEX, lambda: read_codex_usage(self._config.codex_home, now=now))

    def _read_claude(self, now: dt.datetime) -> ProviderUsage:
        """Ingest both figure carriers, read the store, then ask for a refresh if due.

        Ingestion is a read-path step rather than a background watcher: both carriers
        are files somebody else writes at their own cadence, so there is nothing to
        subscribe to, and the aggregate cache already bounds how often this runs.
        Each is isolated — an unreadable file or a failing store write must still
        yield whatever was observed before, so the card degrades to *stale*, never to
        *unavailable*, on an ingest problem.

        The refresh request comes last and blocks nothing: this read returns what is
        on disk *now*, and a refresh it triggers lands in the store for the next one.
        Its threshold is deliberately the collector's freshness horizon
        (:data:`USAGE_CACHE_FRESH_SECONDS`) and not the display's
        :data:`USAGE_STALE_AFTER_SECONDS`: waiting for the card to *look* stale would
        keep it an hour behind when a refresh could keep it minutes behind, and the
        collector re-checks the same horizon against the file itself, so a request it
        cannot improve on costs nothing. An unknown observation time (the empty store
        — no session has ever run, which is the case this carrier exists for) counts
        as due."""
        for ingest in (self._ingest_cache, self._ingest_statusline):
            try:
                ingest()
            except Exception:  # noqa: BLE001 — display-only; the stored observation still reads
                pass
        usage = claude_usage(self._store, now=now)
        if _older_than(usage.observed_ts, now, USAGE_CACHE_FRESH_SECONDS):
            self.request_claude_refresh()
        return usage

    def _ingest_cache(self) -> None:
        """Merge the CLI's own utilization cache into the store, per window.

        The primary carrier's read half, and it stands on its own: the CLI refreshes
        that file during the operator's ordinary interactive sessions too, so reading
        it every time is worthwhile whether or not this hub ever polls. Polling only
        covers the gap when nothing else has (:meth:`request_claude_refresh`)."""
        if self._collector is None:
            return
        self._merge_claude_observation(self._collector.read_cache())

    def _ingest_statusline(self) -> None:
        """Merge the statusLine sink's observation into the store, per window.

        The transitional fallback carrier. Since the utilization cache became the
        primary source the sink is no longer required for Claude usage to work, but
        an existing operator installation still publishes real figures, so it keeps
        being read: routing it *through the store* keeps one read path, one payload
        shape, and one staleness rule for both carriers, and persists the last-known
        figures so a rotated, deleted, or newly-uninstalled sink still shows them as
        stale rather than losing them.

        Merged per window (:func:`merge_observations`), not accepted wholesale on
        write recency: concurrent Claude Code sessions each write the one sink from
        their own last API response, so a newer *write* stamp is not a newer
        *measurement*. Merging by each figure's window instant (ties broken by
        ``max(used_percent)``) stops an idle session's stale figure from lowering a
        current one — including one the cache carrier just contributed — while a
        genuine rollover still replaces."""
        self._merge_claude_observation(read_claude_statusline(self.statusline_sink))
