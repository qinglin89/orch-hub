"""Tests for the agent-subscription-usage backend (Claude + Codex).

Everything here runs against fixtures and fakes: a fake Codex home whose session
rollouts we write by hand, a hub-owned Claude store fed with constructed
observations, and injected clocks. No real Codex/Claude CLI is launched, no
account or network is touched, and the read-only / display-only contract is
asserted (sanitized output, fail-soft unavailable, staleness, bounded caching).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from orch_hub import api
from orch_hub.config import Config
from orch_hub.models import PROVIDER_CLAUDE, PROVIDER_CODEX, UsageWindow
from orch_hub.usage import (
    MAX_CLAUDE_CONFIG_BYTES,
    MAX_STATUSLINE_BYTES,
    REFRESH_BACKOFF,
    REFRESH_CACHE_FRESH,
    REFRESH_IN_FLIGHT,
    REFRESH_MODEL_CALL,
    REFRESH_NO_CACHE,
    REFRESH_OK,
    REFRESH_TIMEOUT,
    REFRESH_UNSUPPORTED,
    USAGE_CACHE_TTL_SECONDS,
    USAGE_POLL_TIMEOUT_SECONDS,
    ClaudeObservation,
    ClaudeUsageCollector,
    ClaudeUsageStore,
    UsageRefreshResult,
    UsageRunResult,
    UsageService,
    WindowBoundary,
    claude_usage,
    merge_observations,
    merge_window_boundaries,
    parse_claude_stream_boundaries,
    parse_claude_stream_usage,
    parse_claude_usage_cache,
    read_claude_statusline,
    read_claude_usage_cache,
    read_codex_usage,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 24, 1, 0, 0, tzinfo=UTC)
TOKEN = "test-token"


# -- fixtures -----------------------------------------------------------------


def write_rollout(codex_home: Path, ts_name: str, lines: list[dict], *, uid: str = "abc") -> Path:
    """Write a Codex session rollout ``<codex_home>/sessions/.../rollout-<ts>-<uid>.jsonl``.

    ``ts_name`` is the ISO-ish timestamp embedded in the filename (what the newest
    scan sorts on); ``lines`` are the JSONL event objects."""
    day_dir = codex_home / "sessions" / "2026" / "07" / "24"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{ts_name}-{uid}.jsonl"
    path.write_text("\n".join(json.dumps(obj) for obj in lines) + "\n", encoding="utf-8")
    return path


def rate_limits_line(
    *,
    ts: str = "2026-07-24T00:59:00Z",
    primary: dict | None = None,
    secondary: dict | None = None,
    plan_type: str | None = "prolite",
    credits: dict | None = None,
    wrap_payload: bool = True,
    **extra: object,
) -> dict:
    """One rollout line carrying a ``rate_limits`` snapshot (real Codex shape)."""
    rate_limits: dict = {
        "limit_id": "codex",
        "limit_name": None,
        "primary": primary
        if primary is not None
        else {"used_percent": 6.0, "window_minutes": 300, "resets_at": 1785287215},
        "secondary": secondary,
        "credits": credits,
        "plan_type": plan_type,
    }
    rate_limits.update(extra)
    if wrap_payload:
        return {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": rate_limits}}
    return {"timestamp": ts, "rate_limits": rate_limits}


# -- Codex adapter ------------------------------------------------------------


def test_codex_normalizes_primary_and_secondary_windows(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_rollout(
        home,
        "2026-07-24T00-59-00",
        [
            rate_limits_line(
                primary={"used_percent": 12.5, "window_minutes": 300, "resets_at": 1785287215},
                secondary={"used_percent": 40.0, "window_minutes": 10080, "resets_at": 1785512951},
            )
        ],
    )
    usage = read_codex_usage(home, now=NOW)
    assert usage.provider == PROVIDER_CODEX
    assert usage.available is True
    assert usage.plan == "prolite"
    keys = {w.key: w for w in usage.windows}
    assert set(keys) == {"primary", "secondary"}
    primary = keys["primary"]
    assert primary.used_percent == 12.5
    assert primary.remaining_percent == 87.5
    assert primary.window_minutes == 300
    assert primary.label == "5h"
    assert primary.resets_at is not None and primary.resets_at.endswith("+00:00")
    secondary = keys["secondary"]
    assert secondary.label == "7d"
    assert secondary.remaining_percent == 60.0
    # Percent-only provider: no invented numeric message total.
    assert primary.used is None and primary.limit is None


def test_codex_optional_secondary_absent(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_rollout(home, "2026-07-24T00-59-00", [rate_limits_line(secondary=None)])
    usage = read_codex_usage(home, now=NOW)
    assert [w.key for w in usage.windows] == ["primary"]


def test_codex_plan_falls_back_to_limit_name(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_rollout(home, "2026-07-24T00-59-00", [rate_limits_line(plan_type=None, limit_name="team-x")])
    usage = read_codex_usage(home, now=NOW)
    assert usage.plan == "team-x"


def test_codex_credits_normalized(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_rollout(
        home,
        "2026-07-24T00-59-00",
        [rate_limits_line(credits={"has_credits": True, "unlimited": False, "balance": "0"})],
    )
    usage = read_codex_usage(home, now=NOW)
    assert usage.credits is not None
    assert usage.credits.has_credits is True
    assert usage.credits.unlimited is False
    assert usage.credits.balance == "0"


def test_codex_sanitizes_account_and_reset_credit_identifiers(tmp_path: Path) -> None:
    """A raw record laced with account/credential/reset-credit ids must not leak
    them: the normalizer whitelists rate-limit fields only."""
    home = tmp_path / "codex"
    write_rollout(
        home,
        "2026-07-24T00-59-00",
        [
            rate_limits_line(
                credits={"has_credits": True, "unlimited": False, "balance": "3", "reset_credit_id": "rc_SECRET"},
                account_id="acct_SECRET",
                access_token="tok_SECRET",
                reset_credit_id="rc_TOPLEVEL_SECRET",
            )
        ],
    )
    usage = read_codex_usage(home, now=NOW)
    blob = json.dumps(usage.to_dict())
    assert "SECRET" not in blob
    assert "reset_credit_id" not in blob
    assert "account_id" not in blob and "access_token" not in blob


def test_codex_newest_rollout_and_last_snapshot_win(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    # Older session (earlier filename ts) with a stale figure.
    write_rollout(
        home,
        "2026-07-23T10-00-00",
        [rate_limits_line(primary={"used_percent": 99.0, "window_minutes": 300, "resets_at": 1785287215})],
        uid="old",
    )
    # Newest session: two snapshots, the LAST one is the freshest.
    write_rollout(
        home,
        "2026-07-24T00-59-00",
        [
            rate_limits_line(primary={"used_percent": 5.0, "window_minutes": 300, "resets_at": 1785287215}),
            rate_limits_line(primary={"used_percent": 7.0, "window_minutes": 300, "resets_at": 1785287215}),
        ],
        uid="new",
    )
    usage = read_codex_usage(home, now=NOW)
    assert usage.windows[0].used_percent == 7.0


def test_codex_top_level_rate_limits_line(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_rollout(home, "2026-07-24T00-59-00", [rate_limits_line(wrap_payload=False)])
    usage = read_codex_usage(home, now=NOW)
    assert usage.available is True and usage.windows


def test_codex_unavailable_without_sessions(tmp_path: Path) -> None:
    usage = read_codex_usage(tmp_path / "codex", now=NOW)
    assert usage.available is False
    assert usage.windows == ()
    assert usage.detail


def test_codex_unavailable_when_no_snapshot(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_rollout(home, "2026-07-24T00-59-00", [{"timestamp": "2026-07-24T00:59:00Z", "type": "message"}])
    usage = read_codex_usage(home, now=NOW)
    assert usage.available is False


def test_codex_staleness(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    # 2h old observation -> stale; the reset time stays meaningful regardless.
    write_rollout(home, "2026-07-24T00-59-00", [rate_limits_line(ts="2026-07-23T23:00:00Z")])
    assert read_codex_usage(home, now=NOW).stale is True
    # Fresh observation -> not stale.
    write_rollout(home, "2026-07-24T00-59-30", [rate_limits_line(ts="2026-07-24T00:59:30Z")], uid="fresh")
    assert read_codex_usage(home, now=NOW).stale is False


# -- Claude stream-json rate-limit parser -------------------------------------
# The window shape below is the CLI's real subscription shape (verified against
# Claude Code 2.1.218 — see the module's section note): a ``rate_limits`` container
# keyed ``five_hour``/``seven_day``, each window carrying a 0-100
# ``used_percentage`` and an epoch-seconds ``resets_at``. No verified CLI version
# puts that container on ``claude -p`` stream-json stdout, so the contract these
# tests pin is mostly the graceful one: that shape -> observation, everything else
# (including the percentage-less rate-limit frames the stream really does carry)
# -> None, never a raise.

OBS_TS = "2026-07-24T01:00:00Z"
SYSTEM_EVENT = {"type": "system", "subtype": "init"}
ASSISTANT_EVENT = {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}}


def claude_stream(*events: dict) -> str:
    """A ``claude -p`` stream-json stdout from the given event objects (one per line)."""
    return "\n".join(json.dumps(e) for e in events) + "\n"


def claude_rate_limits(
    *,
    five_hour: dict | None = None,
    seven_day: dict | None = None,
    **extra: object,
) -> dict:
    """A Claude ``rate_limits`` container: named windows, each one optional."""
    container: dict = {}
    if five_hour is not None:
        container["five_hour"] = five_hour
    if seven_day is not None:
        container["seven_day"] = seven_day
    container.update(extra)
    return container


def claude_result(
    rate_limits: object = None,
    *,
    wrapper: str | None = None,
    plan: str | None = "max",
) -> dict:
    """A ``result`` event, optionally carrying a rate-limit container top-level or
    nested one level under ``wrapper`` (``usage``/``payload``/...). ``plan`` rides
    on the event as ``subscription_type``, a sibling of the container as in the
    CLI's own payload."""
    event: dict = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
    if plan is not None:
        event["subscription_type"] = plan
    if rate_limits is not None:
        if wrapper is None:
            event["rate_limits"] = rate_limits
        else:
            event[wrapper] = {"rate_limits": rate_limits}
    return event


def supported_claude_stdout(*, five: float = 33.0) -> str:
    """A stream carrying a recognized 5h+7d rate-limit container — a *supported* turn."""
    return claude_stream(
        SYSTEM_EVENT,
        ASSISTANT_EVENT,
        claude_result(
            claude_rate_limits(
                five_hour={"used_percentage": five, "resets_at": 1785290400},
                seven_day={"used_percentage": 8.5, "resets_at": 1785790400},
            )
        ),
    )


def test_parse_claude_stream_extracts_5h_and_7d() -> None:
    obs = parse_claude_stream_usage(supported_claude_stdout(five=42.0), observed_ts=OBS_TS)
    assert obs is not None
    assert obs.observed_ts == OBS_TS  # stamped with when the hub saw the turn
    assert obs.plan == "max"
    by_key = {w.key: w for w in obs.windows}
    assert set(by_key) == {"five_hour", "seven_day"}
    five = by_key["five_hour"]
    assert five.used_percent == 42.0
    assert five.remaining_percent == 58.0
    # The CLI's shape carries no duration field — the window's key supplies it.
    assert five.window_minutes == 300 and five.label == "5h"
    assert five.resets_at is not None and five.resets_at.endswith("+00:00")
    assert by_key["seven_day"].label == "7d"
    assert by_key["seven_day"].remaining_percent == 91.5
    # Percent-only provider: no invented numeric message total.
    assert five.used is None and five.limit is None


def test_parse_claude_stream_reads_container_under_wrapper() -> None:
    # The container may sit one level down under a wrapper key rather than top-level.
    stdout = claude_stream(
        claude_result(
            claude_rate_limits(five_hour={"used_percentage": 10.0, "resets_at": 1785290400}),
            wrapper="usage",
        )
    )
    obs = parse_claude_stream_usage(stdout, observed_ts=OBS_TS)
    assert obs is not None
    assert [w.key for w in obs.windows] == ["five_hour"]


def test_parse_claude_stream_reset_accepts_epoch_and_iso() -> None:
    stdout = claude_stream(
        claude_result(
            claude_rate_limits(
                five_hour={"used_percentage": 5.0, "resets_at": 1785290400},
                seven_day={"used_percentage": 5.0, "resets_at": "2026-07-31T00:00:00Z"},
            )
        )
    )
    obs = parse_claude_stream_usage(stdout, observed_ts=OBS_TS)
    by_key = {w.key: w for w in obs.windows}
    assert by_key["five_hour"].resets_at.endswith("+00:00")  # epoch -> ISO UTC
    assert by_key["seven_day"].resets_at == "2026-07-31T00:00:00+00:00"  # ISO normalized to UTC


def test_parse_claude_stream_partial_single_window() -> None:
    # Each window is optional; one present window is a usable observation.
    stdout = claude_stream(
        claude_result(
            claude_rate_limits(
                five_hour={"used_percentage": 12.0, "resets_at": 1785290400},
                seven_day=None,
            )
        )
    )
    obs = parse_claude_stream_usage(stdout, observed_ts=OBS_TS)
    assert obs is not None
    assert [w.key for w in obs.windows] == ["five_hour"]


def test_parse_claude_stream_latest_container_wins() -> None:
    # Two rate-limit events in one turn: the freshest (last) wins.
    stdout = claude_stream(
        claude_result(claude_rate_limits(five_hour={"used_percentage": 10.0})),
        claude_result(claude_rate_limits(five_hour={"used_percentage": 90.0})),
    )
    obs = parse_claude_stream_usage(stdout, observed_ts=OBS_TS)
    assert obs is not None and obs.windows[0].used_percent == 90.0


def test_parse_claude_stream_absent_returns_none() -> None:
    # An ordinary turn carries no usage figure on any verified version -> no
    # observation, so the store stays unfed and Claude reads unavailable.
    stdout = claude_stream(SYSTEM_EVENT, ASSISTANT_EVENT, claude_result())
    assert parse_claude_stream_usage(stdout, observed_ts=OBS_TS) is None


def test_parse_claude_stream_empty_container_returns_none() -> None:
    # A container whose windows carry no usable signal contributes nothing (never a
    # 0%-used stub) -> no observation.
    stdout = claude_stream(claude_result(claude_rate_limits(five_hour={})))
    assert parse_claude_stream_usage(stdout, observed_ts=OBS_TS) is None


def test_parse_claude_stream_quota_429_error_frame_records_nothing() -> None:
    # The ONE rate-limit field the stream really carries: a quota-429 `api_error`
    # frame naming the window and its reset, with no percentage. That is not a usage
    # observation — recording it would publish an "available" Claude card with no
    # figures — so it must leave the store unfed.
    stdout = claude_stream(
        {
            "type": "system",
            "subtype": "api_error",
            "error": {
                "message": "rate limit",
                "status": 429,
                "is_network_down": False,
                "rate_limits": {"resets_at": 1785290400, "rate_limit_type": "five_hour"},
            },
            "retry_in_ms": 500,
            "retry_attempt": 1,
            "max_retries": 10,
        }
    )
    assert parse_claude_stream_usage(stdout, observed_ts=OBS_TS) is None


def test_parse_claude_stream_ignores_ambiguous_utilization() -> None:
    # `utilization` is a 0..1 fraction in the CLI's header-derived cache but an
    # already-0-100 number in its /usage payload. Reading it would risk showing 0.42
    # as "0.42% used"; degrading to unavailable is the mandated graceful state.
    stdout = claude_stream(
        claude_result(claude_rate_limits(five_hour={"utilization": 0.42, "resets_at": 1785290400}))
    )
    assert parse_claude_stream_usage(stdout, observed_ts=OBS_TS) is None


def test_parse_claude_stream_malformed_never_raises() -> None:
    assert parse_claude_stream_usage("", observed_ts=OBS_TS) is None
    # Non-JSON, a bad line, a JSON array, and a bare JSON string are all skipped.
    assert parse_claude_stream_usage('not json\n{bad\n[1,2,3]\n"a string"', observed_ts=OBS_TS) is None


def test_parse_claude_stream_excludes_account_and_credentials() -> None:
    # A container laced with account/credential ids must not leak them: the parser
    # whitelists only window fields + the plan name.
    stdout = claude_stream(
        claude_result(
            claude_rate_limits(
                five_hour={"used_percentage": 7.0, "resets_at": 1785290400},
                account_id="acct_SECRET",
                access_token="tok_SECRET",
                oauth_token="oauth_SECRET",
            )
        )
    )
    obs = parse_claude_stream_usage(stdout, observed_ts=OBS_TS)
    assert obs is not None
    assert obs.plan == "max"
    assert "SECRET" not in json.dumps(obs.to_dict())


# -- Claude stream boundary events (2.1.220) ----------------------------------
# 2.1.220 adds one event to the same stream: camelCase, naming ONE window and when
# it resets, with no percentage. That is a boundary refresh, not an observation, so
# these pin both halves — the parse, and the three merge rules from the module's
# boundary section note: it never creates a window, it only moves `resets_at`
# forward, and it leaves the figures (and their observation time) untouched.

BOUNDARY_EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed",
        "resetsAt": 1785130800,
        "rateLimitType": "five_hour",
        "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled",
        "isUsingOverage": False,
    },
}
"""Verbatim, as captured from Claude Code 2.1.220 on one ordinary ``claude -p`` turn
(``status: allowed`` — not a 429)."""

BOUNDARY_ISO = "2026-07-27T05:40:00+00:00"
"""``resetsAt: 1785130800`` as the parser normalizes it (epoch seconds -> ISO UTC)."""

STORED_FIVE_RESET = "2026-07-27T00:40:00+00:00"  # the window before BOUNDARY_ISO
STORED_SEVEN_RESET = "2026-08-01T00:00:00+00:00"


def boundary_event(**info: object) -> dict:
    """The 2.1.220 boundary event with its ``rate_limit_info`` fields overridden."""
    payload = dict(BOUNDARY_EVENT["rate_limit_info"])  # type: ignore[arg-type]
    payload.update(info)
    return {"type": "rate_limit_event", "rate_limit_info": payload}


def bounded_observation(
    *,
    observed_ts: str = "2026-07-27T02:30:00+00:00",
    five_reset: str | None = STORED_FIVE_RESET,
    used: float = 4.0,
) -> ClaudeObservation:
    """A stored sink observation whose windows already carry reset instants."""
    return ClaudeObservation(
        observed_ts=observed_ts,
        plan="max",
        windows=(
            UsageWindow(
                key="five_hour",
                label="5h",
                window_minutes=300,
                used_percent=used,
                remaining_percent=100 - used,
                resets_at=five_reset,
            ),
            UsageWindow(
                key="seven_day",
                label="7d",
                window_minutes=10080,
                used_percent=11.0,
                remaining_percent=89.0,
                resets_at=STORED_SEVEN_RESET,
            ),
        ),
    )


def test_parse_claude_boundary_event_verbatim_2_1_220() -> None:
    stdout = claude_stream(SYSTEM_EVENT, BOUNDARY_EVENT, ASSISTANT_EVENT, claude_result())
    assert parse_claude_stream_boundaries(stdout) == (
        WindowBoundary(key="five_hour", resets_at=BOUNDARY_ISO),
    )
    # The same stream is still NOT a usage observation: no percentage means no
    # window, so the figure carrier stays the only source of figures.
    assert parse_claude_stream_usage(stdout, observed_ts=OBS_TS) is None


def test_parse_claude_boundary_accepts_the_snake_case_spelling() -> None:
    # camelCase is what 2.1.220 emits; snake_case is accepted as tolerance because
    # the CLI spells the same two fields that way elsewhere.
    stdout = claude_stream(
        {"type": "rate_limit_event", "rate_limit_info": {"rate_limit_type": "seven_day", "resets_at": 1785290400}}
    )
    assert parse_claude_stream_boundaries(stdout) == (
        WindowBoundary(key="seven_day", resets_at="2026-07-29T02:00:00+00:00"),
    )


def test_parse_claude_boundary_needs_a_known_window_and_a_reset() -> None:
    # An unknown window name must not create a bucket, and a boundary with no usable
    # reset instant carries nothing to merge.
    assert parse_claude_stream_boundaries(claude_stream(boundary_event(rateLimitType="five_hour_opus"))) == ()
    assert parse_claude_stream_boundaries(claude_stream(boundary_event(resetsAt=None))) == ()
    assert parse_claude_stream_boundaries(claude_stream(boundary_event(resetsAt="not-a-date"))) == ()


def test_parse_claude_boundary_absent_or_malformed_yields_nothing() -> None:
    # Every turn before 2.1.220, plus the usual garbage: never a raise.
    assert parse_claude_stream_boundaries(claude_stream(SYSTEM_EVENT, claude_result())) == ()
    assert parse_claude_stream_boundaries("") == ()
    assert parse_claude_stream_boundaries('not json\n{bad\n[1,2,3]\n"a string"') == ()


def test_parse_claude_boundary_newest_per_window_wins() -> None:
    stdout = claude_stream(
        boundary_event(resetsAt=1785088200),
        boundary_event(resetsAt=1785130800),
        boundary_event(rateLimitType="seven_day", resetsAt=1785290400),
    )
    assert parse_claude_stream_boundaries(stdout) == (
        WindowBoundary(key="five_hour", resets_at=BOUNDARY_ISO),
        WindowBoundary(key="seven_day", resets_at="2026-07-29T02:00:00+00:00"),
    )


def test_merge_boundary_moves_only_the_reset_instant() -> None:
    stored = bounded_observation()
    merged = merge_window_boundaries(stored, [WindowBoundary(key="five_hour", resets_at=BOUNDARY_ISO)])
    assert merged is not None
    assert merged.windows[0].resets_at == BOUNDARY_ISO
    # Everything that describes usage is untouched — including `observed_ts`, which
    # dates the FIGURES, so staleness keeps measuring their age.
    assert merged.observed_ts == stored.observed_ts
    assert merged.plan == stored.plan
    assert merged.windows[0].used_percent == 4.0
    assert merged.windows[0].remaining_percent == 96.0
    assert merged.windows[1] == stored.windows[1]  # unnamed window untouched


def test_merge_boundary_is_forward_only() -> None:
    stored = bounded_observation()
    # Equal (a second turn inside the same window) and earlier (out-of-order or a
    # duplicate) both change nothing -> nothing is written.
    assert merge_window_boundaries(stored, [WindowBoundary(key="five_hour", resets_at=STORED_FIVE_RESET)]) is None
    assert (
        merge_window_boundaries(stored, [WindowBoundary(key="five_hour", resets_at="2026-07-26T17:50:00+00:00")])
        is None
    )


def test_merge_boundary_never_creates_a_window() -> None:
    # A boundary for a window the figure carrier never observed adds nothing: the
    # "no used_percentage -> no window" rule holds through the merge too.
    stored = ClaudeObservation(
        observed_ts="2026-07-27T02:30:00+00:00",
        plan="max",
        windows=(UsageWindow(key="seven_day", label="7d", window_minutes=10080, used_percent=11.0, resets_at=STORED_SEVEN_RESET),),
    )
    assert merge_window_boundaries(stored, [WindowBoundary(key="five_hour", resets_at=BOUNDARY_ISO)]) is None
    assert merge_window_boundaries(stored, []) is None


def test_merge_boundary_fills_a_window_with_no_stored_reset() -> None:
    # A stored window whose carrier gave no reset instant gains one — new
    # information about an already-observed window, not a new window.
    stored = bounded_observation(five_reset=None)
    merged = merge_window_boundaries(stored, [WindowBoundary(key="five_hour", resets_at=BOUNDARY_ISO)])
    assert merged is not None
    assert merged.windows[0].resets_at == BOUNDARY_ISO
    assert merged.windows[0].used_percent == 4.0


# -- Claude sink per-window merge (the stale-writer fix) -----------------------
# Every concurrent Claude Code session writes the one statusLine sink
# last-write-wins, each publishing the rate_limits of its OWN last API response —
# so a sink's write stamp is not its measurement time. `merge_observations` folds
# a fresh sink read into the store per window, keyed on each figure's own window
# instant (`figure_instant`, distinct from the displayable `resets_at` a boundary
# refresh may advance), with ties broken by max(used_percent). These pin the five
# rules: same-window stale figure ignored, same-window max, rollover replace,
# pre-reset snapshot ignored, and the boundary-advance interaction.

# Two 5h window instants: WIN_B is a later window (a rollover from WIN_A). WIN_A is
# also the epoch the default `write_sink` five-hour reset normalizes to.
WIN_A = "2026-07-29T02:00:00+00:00"  # == epoch 1785290400
WIN_B = "2026-07-29T05:00:00+00:00"  # a later 5h window


def five_obs(
    used: float,
    reset: str | None,
    *,
    instant: str | None = None,
    observed_ts: str = "2026-07-29T00:24:00+00:00",
    plan: str | None = "max",
) -> ClaudeObservation:
    """A single-five-hour-window Claude observation for merge tests.

    ``instant`` (the figure's own window instant) defaults to ``reset`` — the
    fresh-figure invariant — but is set apart to model a stored window whose
    displayed ``resets_at`` a boundary refresh has advanced past its figure."""
    return ClaudeObservation(
        observed_ts=observed_ts,
        plan=plan,
        windows=(
            UsageWindow(
                key="five_hour",
                label="5h",
                window_minutes=300,
                used_percent=used,
                remaining_percent=round(100 - used, 2),
                resets_at=reset,
                figure_instant=reset if instant is None else instant,
            ),
        ),
    )


def test_merge_same_window_ignores_a_lower_figure_whatever_its_stamp() -> None:
    # The reported bug: a refresh dropped 5h from 5% to 3%. A stale concurrent
    # writer republishes an older, LOWER same-window figure with a NEWER write
    # stamp. used% only grows within a window, so the lower figure is the earlier
    # measurement and must not win — nothing changes, so the caller writes nothing.
    current = five_obs(5.0, WIN_A, observed_ts="2026-07-29T00:24:32+00:00")
    candidate = five_obs(3.0, WIN_A, observed_ts="2026-07-29T00:26:10+00:00")
    assert merge_observations(current, candidate) is None


def test_merge_same_window_takes_the_higher_used_percent() -> None:
    # A genuinely later reading in the same window (higher used%) advances the
    # figure; identity (the window instant) and the displayed reset are unchanged.
    current = five_obs(5.0, WIN_A)
    merged = merge_observations(current, five_obs(8.0, WIN_A))
    assert merged is not None
    assert merged.windows[0].used_percent == 8.0
    assert merged.windows[0].resets_at == WIN_A
    assert merged.windows[0].figure_instant == WIN_A


def test_merge_rollover_replaces_even_with_a_lower_figure() -> None:
    # A newer window instant is a rollover: the fresh post-reset reading replaces
    # the old near-full figure, even though its used% is far lower.
    current = five_obs(90.0, WIN_A)
    merged = merge_observations(current, five_obs(2.0, WIN_B))
    assert merged is not None
    assert merged.windows[0].used_percent == 2.0
    assert merged.windows[0].resets_at == WIN_B
    assert merged.windows[0].figure_instant == WIN_B


def test_merge_ignores_a_pre_reset_older_window_snapshot() -> None:
    # The mirror of rollover: a stale writer still on the PREVIOUS window (older
    # instant) reports a high figure. Its window is older, so it is a pre-reset
    # snapshot and is ignored — the current low post-reset figure is not regressed.
    current = five_obs(2.0, WIN_B)
    assert merge_observations(current, five_obs(90.0, WIN_A)) is None


def test_merge_boundary_advance_does_not_freeze_the_pre_reset_figure() -> None:
    # The crux. `merge_window_boundaries` advanced the DISPLAYED resets_at to the
    # next window (WIN_B) while the figure is still the pre-reset one measured in
    # WIN_A. Keying identity on figure_instant (WIN_A), not the displayed resets_at
    # (WIN_B), lets the real post-reset reading win — if identity keyed on the
    # displayed reset, WIN_B == WIN_B would tie and the stale 90 would win the max.
    current = five_obs(90.0, WIN_B, instant=WIN_A)
    merged = merge_observations(current, five_obs(2.0, WIN_B))
    assert merged is not None
    assert merged.windows[0].used_percent == 2.0
    assert merged.windows[0].figure_instant == WIN_B


def test_merge_window_boundary_leaves_the_figure_instant_behind() -> None:
    # The setup above is what merge_window_boundaries actually produces: it advances
    # the displayed resets_at but must NOT touch the figure's own window instant, or
    # the next post-reset reading would lose the comparison and be discarded.
    stored = five_obs(90.0, WIN_A)  # resets_at == figure_instant == WIN_A
    merged = merge_window_boundaries(stored, [WindowBoundary(key="five_hour", resets_at=WIN_B)])
    assert merged is not None
    assert merged.windows[0].resets_at == WIN_B  # displayed boundary advanced
    assert merged.windows[0].figure_instant == WIN_A  # figure identity stays put


def test_merge_keeps_a_window_the_candidate_omits_and_adds_a_new_one() -> None:
    # Per-window: a stored window the fresh read does not mention is kept untouched,
    # and a window the store never had is added — the read never has to carry every
    # window to avoid dropping the others.
    current = ClaudeObservation(
        observed_ts="2026-07-29T00:24:00+00:00",
        plan="max",
        windows=(
            UsageWindow(key="five_hour", label="5h", window_minutes=300, used_percent=5.0, resets_at=WIN_A, figure_instant=WIN_A),
            UsageWindow(key="seven_day", label="7d", window_minutes=10080, used_percent=11.0, resets_at=STORED_SEVEN_RESET, figure_instant=STORED_SEVEN_RESET),
        ),
    )
    # Candidate carries only a higher five_hour; seven_day is absent.
    merged = merge_observations(current, five_obs(9.0, WIN_A))
    assert merged is not None
    by_key = {w.key: w for w in merged.windows}
    assert by_key["five_hour"].used_percent == 9.0
    assert by_key["seven_day"].used_percent == 11.0  # untouched


def test_merge_unchanged_reread_writes_nothing() -> None:
    # An identical re-read of an unchanged sink is a no-op, so repeated reads never
    # churn the store (the old newness gate's job, now value-based).
    current = five_obs(7.0, WIN_A)
    assert merge_observations(current, five_obs(7.0, WIN_A)) is None


# The observed_ts freshness floor: observed_ts is ONE provider-level stamp, honest
# only while the store holds a single observation moment. A full statusLine payload
# keeps it coherent (both windows in one sample, uniform merge winner); a PARTIAL
# payload — each window is optional — keeps one window from a differently-timed
# observation, mixing two moments, so the merge floors the shared stamp to the OLDER
# one and no window is ever dated newer than the observation that supplied its figure.

def _two_window_obs(
    observed_ts: str,
    five_used: float,
    seven_used: float,
    *,
    five_reset: str = WIN_A,
    seven_reset: str = STORED_SEVEN_RESET,
) -> ClaudeObservation:
    """A coherent 5h+7d observation — both figures measured at ``observed_ts``."""
    return ClaudeObservation(
        observed_ts=observed_ts,
        plan="max",
        windows=(
            UsageWindow(key="five_hour", label="5h", window_minutes=300, used_percent=five_used, remaining_percent=round(100 - five_used, 2), resets_at=five_reset, figure_instant=five_reset),
            UsageWindow(key="seven_day", label="7d", window_minutes=10080, used_percent=seven_used, remaining_percent=round(100 - seven_used, 2), resets_at=seven_reset, figure_instant=seven_reset),
        ),
    )


def test_merge_full_payload_advances_observed_ts() -> None:
    # The guard must not touch the real path: a full same-window-set payload is one
    # coherent moment, so a genuinely later reading advances the shared stamp as before.
    older = "2026-07-29T00:24:00+00:00"
    newer = "2026-07-29T02:40:00+00:00"
    merged = merge_observations(
        _two_window_obs(older, 5.0, 5.0), _two_window_obs(newer, 9.0, 6.0)
    )
    assert merged is not None
    assert merged.observed_ts == newer
    by_key = {w.key: w for w in merged.windows}
    assert by_key["five_hour"].used_percent == 9.0
    assert by_key["seven_day"].used_percent == 6.0


def test_merge_partial_candidate_does_not_advance_observed_ts_over_a_kept_window() -> None:
    # The freshness floor. A read carries only five_hour (windows are optional), fresh
    # and higher, with a NEWER stamp. It merges in, but seven_day is kept from the older
    # stored observation, so the result holds two moments. The shared observed_ts must
    # stay the OLDER stamp, or the kept 7d would claim a freshness it never had.
    older = "2026-07-29T00:24:00+00:00"
    newer = "2026-07-29T02:40:00+00:00"
    current = _two_window_obs(older, 5.0, 11.0)
    merged = merge_observations(current, five_obs(9.0, WIN_A, observed_ts=newer))
    assert merged is not None
    by_key = {w.key: w for w in merged.windows}
    assert by_key["five_hour"].used_percent == 9.0  # fresh 5h applied
    assert by_key["seven_day"].used_percent == 11.0  # 7d kept untouched
    assert merged.observed_ts == older  # NOT advanced to the 5h-only read's newer stamp


def test_merge_partial_candidate_adding_a_window_floors_observed_ts_to_the_older() -> None:
    # The mirror edge: the store holds only five_hour (a prior partial read) at a NEWER
    # moment; a candidate carrying both arrives OLDER. Its seven_day is new to the store
    # but from the older moment, so the shared stamp floors to that older time — a naive
    # "candidate covered all stored windows -> take the later stamp" rule would instead
    # over-claim the freshly-added 7d.
    older = "2026-07-29T00:24:00+00:00"
    newer = "2026-07-29T02:40:00+00:00"
    current = five_obs(9.0, WIN_A, observed_ts=newer)  # five_hour only, newer
    candidate = _two_window_obs(older, 5.0, 3.0)  # both windows, older + lower 5h
    merged = merge_observations(current, candidate)
    assert merged is not None
    by_key = {w.key: w for w in merged.windows}
    assert by_key["five_hour"].used_percent == 9.0  # stored 5h kept (candidate older/lower)
    assert by_key["seven_day"].used_percent == 3.0  # 7d added from the older candidate
    assert merged.observed_ts == older  # floored to the older moment, never over-claims 7d


# -- Claude store + read ------------------------------------------------------


def make_observation(observed_ts: str = "2026-07-24T00:59:00Z", used: float = 20.0) -> ClaudeObservation:
    return ClaudeObservation(
        observed_ts=observed_ts,
        plan="max",
        windows=(
            UsageWindow(key="five_hour", label="5h", window_minutes=300, used_percent=used, remaining_percent=100 - used),
            UsageWindow(key="seven_day", label="7d", window_minutes=10080, used_percent=5.0, remaining_percent=95.0),
        ),
    )


def test_claude_store_round_trip(tmp_path: Path) -> None:
    store = ClaudeUsageStore(tmp_path / "data")
    assert store.latest() is None
    obs = make_observation()
    store.record(obs)
    loaded = store.latest()
    assert loaded is not None
    assert loaded.plan == "max"
    assert [w.key for w in loaded.windows] == ["five_hour", "seven_day"]
    assert loaded.windows[0].used_percent == 20.0


def test_claude_store_round_trips_figure_instant(tmp_path: Path) -> None:
    # figure_instant must survive a restart, or the per-window merge loses each
    # figure's window identity and a stale writer could regress a stored figure
    # again. It is store-internal, so it persists through the store yet stays out of
    # the HTTP-facing window dict.
    store = ClaudeUsageStore(tmp_path / "data")
    store.record(five_obs(7.0, WIN_A))
    loaded = store.latest()
    assert loaded is not None
    assert loaded.windows[0].figure_instant == WIN_A
    assert "figure_instant" not in loaded.windows[0].to_dict()


def test_from_dict_leaves_a_legacy_windows_figure_instant_unknown() -> None:
    # A claude.json written before figure_instant existed (pre-93a0e5c) serialized
    # only the public window dict — no figure_instant. Its figure deserializes with an
    # UNKNOWN identity, deliberately NOT fabricated from resets_at: a legacy store's
    # displayed reset may be one the boundary refresh already advanced past the
    # figure's own window, so it cannot be trusted as the figure's identity.
    # _merge_window then yields an unknown-identity stored figure to any candidate that
    # carries a real one, which rolls both a plain and a boundary-advanced legacy
    # window over correctly (below) instead of freezing a stale figure via the max.
    loaded = ClaudeObservation.from_dict(
        {
            "observed_ts": "2026-07-29T00:24:00+00:00",
            "plan": "max",
            "windows": [
                {"key": "five_hour", "used_percent": 90.0, "resets_at": WIN_A},
                {"key": "seven_day", "used_percent": 12.0},  # legacy, no reset
            ],
        }
    )
    by_key = {w.key: w for w in loaded.windows}
    assert by_key["five_hour"].figure_instant is None  # not fabricated from resets_at
    assert by_key["seven_day"].figure_instant is None  # no reset either


def test_legacy_store_without_figure_instant_still_rolls_over(tmp_path: Path) -> None:
    # The plain legacy case. A pre-figure_instant claude.json holds a near-full 5h
    # figure; on the first post-upgrade rollover a sink candidate arrives in a NEWER
    # window with a far lower figure. The legacy window deserializes with an unknown
    # figure_instant (not fabricated from its reset); _merge_window yields an
    # unknown-identity stored figure to the candidate that carries a real one, so the
    # old figure drops instead of freezing into the new window via the max.
    store = ClaudeUsageStore(tmp_path / "data")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    legacy_window = UsageWindow(
        key="five_hour", label="5h", window_minutes=300,
        used_percent=90.0, remaining_percent=10.0, resets_at=WIN_A,
    )
    assert "figure_instant" not in legacy_window.to_dict()  # a faithful pre-upgrade record
    store.path.write_text(
        json.dumps(
            {
                "observed_ts": "2026-07-29T00:24:00+00:00",
                "plan": "max",
                "windows": [legacy_window.to_dict()],
            }
        ),
        encoding="utf-8",
    )
    current = store.latest()
    assert current is not None
    assert current.windows[0].figure_instant is None  # unknown, not fabricated
    merged = merge_observations(current, five_obs(2.0, WIN_B))  # newer window, lower figure
    assert merged is not None
    assert merged.windows[0].used_percent == 2.0  # rolled over, not frozen at 90
    assert merged.windows[0].figure_instant == WIN_B


def test_boundary_advanced_legacy_store_rolls_over_same_reset(tmp_path: Path) -> None:
    # The review finding's harder case. Before figure_instant existed, the 2.1.220
    # boundary refresh (merge_window_boundaries advances resets_at only) could already
    # have moved a legacy window's DISPLAYED reset to the CURRENT window while its
    # figure is the stale one from the PREVIOUS window — so the persisted dict carries
    # the advanced reset (== the sink's own reset) and no figure_instant. Fabricating
    # figure_instant from that advanced reset would tie the fresh SAME-reset sink
    # reading on the equal-instant max and freeze the stale 90. Leaving the legacy
    # figure's identity unknown instead lets the candidate that carries one win.
    store = ClaudeUsageStore(tmp_path / "data")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    legacy_window = UsageWindow(
        key="five_hour", label="5h", window_minutes=300,
        used_percent=90.0, remaining_percent=10.0, resets_at=WIN_B,  # already advanced to the current window
    )
    assert "figure_instant" not in legacy_window.to_dict()  # a faithful pre-upgrade record
    store.path.write_text(
        json.dumps(
            {
                "observed_ts": "2026-07-29T00:24:00+00:00",
                "plan": "max",
                "windows": [legacy_window.to_dict()],
            }
        ),
        encoding="utf-8",
    )
    current = store.latest()
    assert current is not None
    assert current.windows[0].figure_instant is None  # unknown, not the advanced reset
    merged = merge_observations(current, five_obs(2.0, WIN_B))  # fresh reading, SAME reset
    assert merged is not None
    assert merged.windows[0].used_percent == 2.0  # rolled over, not frozen at 90
    assert merged.windows[0].figure_instant == WIN_B


def test_claude_store_latest_handles_corrupt(tmp_path: Path) -> None:
    store = ClaudeUsageStore(tmp_path / "data")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json", encoding="utf-8")
    assert store.latest() is None


def test_claude_store_concurrent_record_is_safe(tmp_path: Path) -> None:
    """Concurrent ``record()`` must not race on a shared temp file.

    Once the intake and semantic observers are wired, hub-driven Claude turns
    record from daemon threads, so two ``record()`` calls can overlap. A fixed
    ``claude.json.tmp`` name lets one writer's ``os.replace`` move the temp out
    from under another (``FileNotFoundError``, lost observation); with per-writer
    temp files no call raises, one observation wins intact, and no orphan temp is
    left behind. This fails against the shared-temp-name implementation."""
    store = ClaudeUsageStore(tmp_path / "data")
    workers = 16
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(i: int) -> None:
        obs = make_observation(used=float(i))
        barrier.wait()  # release all threads together to maximize contention
        try:
            for _ in range(25):
                store.record(obs)
        except BaseException as exc:  # noqa: BLE001 — captured for assertion
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"record() raced under concurrency: {errors!r}"
    latest = store.latest()
    assert latest is not None  # a complete, valid observation survived
    assert latest.windows[0].used_percent in {float(i) for i in range(workers)}
    assert list(store.path.parent.glob("*.tmp")) == []  # no orphaned temp files


def test_claude_usage_unavailable_when_unfed(tmp_path: Path) -> None:
    usage = claude_usage(ClaudeUsageStore(tmp_path / "data"), now=NOW)
    assert usage.provider == PROVIDER_CLAUDE
    assert usage.available is False
    assert usage.windows == () and usage.detail


def test_claude_usage_available_with_staleness(tmp_path: Path) -> None:
    store = ClaudeUsageStore(tmp_path / "data")
    store.record(make_observation(observed_ts="2026-07-24T00:59:00Z"))
    fresh = claude_usage(store, now=NOW)
    assert fresh.available is True and fresh.stale is False
    assert fresh.plan == "max"
    store.record(make_observation(observed_ts="2026-07-23T20:00:00Z"))
    assert claude_usage(store, now=NOW).stale is True


# -- Claude statusLine sink (the carrier that actually works) -----------------
#
# The operator-installed writer (orch_hub.statusline) publishes the CLI's own
# rate-limit payload here; the hub ingests it into the same store, so an
# uninstalled/broken hook is indistinguishable from today's unavailable card.


def write_sink(path: Path, *, five: float = 41.0, observed_ts: str | None = "2026-07-24T00:59:00Z", plan: str | None = "max") -> Path:
    """Write a statusLine sink file in the shape the writer persists."""
    record: dict = {
        "schema": 1,
        "source": "claude-statusline",
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": 1785290400},
            "seven_day": {"used_percentage": 9.5, "resets_at": 1785790400},
        },
    }
    if observed_ts is not None:
        record["observed_ts"] = observed_ts
    if plan is not None:
        record["subscription_type"] = plan
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_statusline_sink_parses_with_the_verified_stream_mapping(tmp_path: Path) -> None:
    # Same mapping as the stream carrier: 0-100 used_percentage, epoch resets_at,
    # plan as a sibling of the container.
    obs = read_claude_statusline(write_sink(tmp_path / "claude-statusline.json", five=41.0))
    assert obs is not None
    assert obs.plan == "max" and obs.observed_ts == "2026-07-24T00:59:00+00:00"
    by_key = {w.key: w for w in obs.windows}
    assert by_key["five_hour"].used_percent == 41.0 and by_key["five_hour"].window_minutes == 300
    assert by_key["seven_day"].remaining_percent == 90.5
    assert by_key["five_hour"].resets_at.endswith("+00:00")


def test_statusline_sink_falls_back_to_mtime(tmp_path: Path) -> None:
    # No stamp (or an unparseable one) -> the file's mtime is the observation time,
    # so staleness is still computable rather than silently absent.
    path = write_sink(tmp_path / "claude-statusline.json", observed_ts=None)
    os.utime(path, (1785290000, 1785290000))
    obs = read_claude_statusline(path)
    assert obs is not None
    assert obs.observed_ts == dt.datetime.fromtimestamp(1785290000, tz=UTC).isoformat()
    bad = write_sink(tmp_path / "bad.json", observed_ts="not a timestamp")
    os.utime(bad, (1785290000, 1785290000))
    assert read_claude_statusline(bad).observed_ts.startswith("2026-")


def test_statusline_sink_absent_or_malformed_yields_nothing(tmp_path: Path) -> None:
    # Hook not installed, unreadable, or an unrecognized shape: all identical to
    # the caller — no observation, hence the pre-existing unavailable card.
    assert read_claude_statusline(tmp_path / "missing.json") is None
    assert read_claude_statusline(tmp_path) is None  # a directory
    for name, text in {
        "empty.json": "",
        "notjson.json": "{ not json",
        "array.json": "[1, 2, 3]",
        "nocontainer.json": json.dumps({"observed_ts": OBS_TS, "cwd": "/x"}),
        "emptywindows.json": json.dumps({"rate_limits": {"five_hour": {}}}),
        "utilization.json": json.dumps({"rate_limits": {"five_hour": {"utilization": 0.42}}}),
    }.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        assert read_claude_statusline(path) is None, name


def test_statusline_sink_oversized_is_skipped(tmp_path: Path) -> None:
    path = write_sink(tmp_path / "claude-statusline.json")
    path.write_text(path.read_text(encoding="utf-8") + " " * (MAX_STATUSLINE_BYTES + 1), encoding="utf-8")
    assert read_claude_statusline(path) is None


def test_service_ingests_statusline_sink(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = UsageService(config, clock=lambda: NOW)
    assert service.get_usage().providers[0].available is False  # no hook installed yet
    write_sink(service.statusline_sink, five=41.0)
    service = UsageService(config, clock=lambda: NOW)  # fresh service = no cached snapshot
    claude = {p.provider: p for p in service.get_usage().providers}[PROVIDER_CLAUDE]
    assert claude.available is True and claude.plan == "max"
    assert claude.windows[0].used_percent == 41.0
    assert claude.stale is False
    # Ingestion goes *through the store*, so the read path/cache/payload are unchanged
    # and the figures survive the sink disappearing (shown stale, never lost).
    assert service.store.latest() is not None
    service.statusline_sink.unlink()
    assert UsageService(config, clock=lambda: NOW).get_usage().providers[0].windows[0].used_percent == 41.0


def test_service_statusline_stale_is_the_resting_state(tmp_path: Path) -> None:
    # An idle Mac renders no status line, so nothing is written: the card must read
    # as last-observed figures, never as zero usage.
    config = make_config(tmp_path)
    service = UsageService(config, clock=lambda: NOW)
    write_sink(service.statusline_sink, five=41.0, observed_ts="2026-07-23T20:00:00Z")
    claude = {p.provider: p for p in service.get_usage().providers}[PROVIDER_CLAUDE]
    assert claude.available is True and claude.stale is True
    assert claude.windows[0].used_percent == 41.0  # last observed, not zeroed


def test_service_statusline_stale_writer_cannot_lower_a_current_figure(tmp_path: Path) -> None:
    # End to end through the sink read path. A current figure is stored, then a
    # stale concurrent writer's OLDER-but-same-window figure lands in the sink with
    # a NEWER write stamp. Write recency must not lower the stored figure — the
    # reported 5h 5% -> 3% regression.
    config = make_config(tmp_path)
    now = dt.datetime(2026, 7, 29, 0, 45, tzinfo=UTC)
    service = UsageService(config, clock=lambda: now)
    write_sink(service.statusline_sink, five=5.0, observed_ts="2026-07-29T00:24:32+00:00")
    assert service.get_usage().providers[0].windows[0].used_percent == 5.0  # current figure stored
    write_sink(service.statusline_sink, five=3.0, observed_ts="2026-07-29T00:26:10+00:00")  # stale, newer stamp
    fresh = UsageService(config, clock=lambda: now)  # no cached snapshot
    assert fresh.get_usage().providers[0].windows[0].used_percent == 5.0  # not lowered to 3
    # A genuinely later reading in the same window (higher used%) still advances it.
    write_sink(service.statusline_sink, five=9.0, observed_ts="2026-07-29T00:40:00+00:00")
    newer = UsageService(config, clock=lambda: now)
    assert newer.get_usage().providers[0].windows[0].used_percent == 9.0


def test_service_legacy_store_rolls_over_through_the_sink(tmp_path: Path) -> None:
    # End to end through the ingest read path. A pre-figure_instant claude.json holds
    # a near-full figure in an EARLIER 5h window; the operator sink then publishes a
    # fresh, far lower figure in a newer window. The rollover must drop the old
    # figure — the legacy store's missing figure_instant stays unknown, so the merge
    # yields it to the sink's identified figure instead of the stale 90 freezing.
    config = make_config(tmp_path)
    now = dt.datetime(2026, 7, 29, 1, 0, tzinfo=UTC)
    store = ClaudeUsageStore(config.data_dir)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    prev_window = "2026-07-28T23:00:00+00:00"  # an earlier 5h window than the sink's WIN_A
    store.path.write_text(
        json.dumps(
            {
                "observed_ts": "2026-07-28T22:30:00+00:00",
                "plan": "max",
                "windows": [
                    UsageWindow(
                        key="five_hour", label="5h", window_minutes=300,
                        used_percent=90.0, remaining_percent=10.0, resets_at=prev_window,
                    ).to_dict()
                ],
            }
        ),
        encoding="utf-8",
    )
    service = UsageService(config, clock=lambda: now)
    write_sink(service.statusline_sink, five=2.0, observed_ts="2026-07-29T00:55:00+00:00")
    claude = {p.provider: p for p in service.get_usage().providers}[PROVIDER_CLAUDE]
    five = {w.key: w for w in claude.windows}["five_hour"]
    assert five.used_percent == 2.0  # rolled over, not frozen at 90


def test_service_boundary_advanced_legacy_rolls_over_through_the_sink(tmp_path: Path) -> None:
    # End to end for the boundary-advanced legacy case (the review finding). A
    # pre-figure_instant claude.json whose resets_at was already advanced to the
    # CURRENT 5h window (the sink's own reset) still holds the stale near-full figure
    # from the previous window. The operator sink then publishes the real, far lower
    # current figure at that SAME reset. It must roll over — the legacy figure's
    # unknown identity yields to the sink's, rather than freezing 90 via a same-reset
    # equal-instant max. write_sink's five_hour reset (1785290400) normalizes to WIN_A.
    config = make_config(tmp_path)
    now = dt.datetime(2026, 7, 29, 1, 0, tzinfo=UTC)
    store = ClaudeUsageStore(config.data_dir)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "observed_ts": "2026-07-28T22:30:00+00:00",
                "plan": "max",
                "windows": [
                    UsageWindow(
                        key="five_hour", label="5h", window_minutes=300,
                        used_percent=90.0, remaining_percent=10.0, resets_at=WIN_A,  # already advanced
                    ).to_dict()
                ],
            }
        ),
        encoding="utf-8",
    )
    service = UsageService(config, clock=lambda: now)
    write_sink(service.statusline_sink, five=2.0, observed_ts="2026-07-29T00:55:00+00:00")
    claude = {p.provider: p for p in service.get_usage().providers}[PROVIDER_CLAUDE]
    five = {w.key: w for w in claude.windows}["five_hour"]
    assert five.used_percent == 2.0  # rolled over, not frozen at 90


def test_service_statusline_ingest_failure_keeps_stored_observation(tmp_path: Path) -> None:
    class BoomStore(ClaudeUsageStore):
        def record(self, observation):  # noqa: ANN001 - test stub
            raise RuntimeError("disk full")

    config = make_config(tmp_path)
    store = BoomStore(config.data_dir)
    ClaudeUsageStore(config.data_dir).record(make_observation(used=20.0))
    service = UsageService(config, claude_store=store, clock=lambda: NOW)
    write_sink(service.statusline_sink, five=41.0, observed_ts="2026-07-24T00:59:30Z")
    claude = {p.provider: p for p in service.get_usage().providers}[PROVIDER_CLAUDE]
    # An ingest that cannot persist degrades to the previous observation — stale at
    # worst — rather than dropping Claude to unavailable.
    assert claude.available is True and claude.windows[0].used_percent == 20.0


def test_statusline_sink_never_gates_anything(tmp_path: Path) -> None:
    # Display-only: a corrupt sink returns 200 with an unavailable Claude card and
    # leaves every other route (readiness, runs, intake) untouched.
    config = make_config(tmp_path)
    sink = config.data_dir / "usage" / "claude-statusline.json"
    sink.parent.mkdir(parents=True, exist_ok=True)
    sink.write_text("{ not json", encoding="utf-8")
    client = build_client(config)
    resp = client.get("/api/usage")
    assert resp.status_code == 200
    assert {p["provider"]: p for p in resp.json()["providers"]}[PROVIDER_CLAUDE]["available"] is False
    assert client.get("/api/health").status_code == 200


# -- UsageService: aggregation, caching, fail-soft ----------------------------


def make_config(tmp_path: Path) -> Config:
    return Config(
        ai_native_path=Path("/fake/ai-native-development"),
        token=TOKEN,
        data_dir=tmp_path / "data",
        codex_home=tmp_path / "codex",
    )


def test_service_aggregates_both_providers(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_rollout(config.codex_home, "2026-07-24T00-59-00", [rate_limits_line()])
    store = ClaudeUsageStore(config.data_dir)
    store.record(make_observation())
    service = UsageService(config, claude_store=store, clock=lambda: NOW)
    usage = service.get_usage()
    by_provider = {p.provider: p for p in usage.providers}
    assert set(by_provider) == {PROVIDER_CLAUDE, PROVIDER_CODEX}
    assert by_provider[PROVIDER_CLAUDE].available is True
    assert by_provider[PROVIDER_CODEX].available is True


def test_service_bounded_cache(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = ClaudeUsageStore(config.data_dir)
    store.record(make_observation(used=10.0))
    clock = {"t": NOW}
    service = UsageService(config, claude_store=store, clock=lambda: clock["t"])
    first = service.get_usage()
    assert first.providers[0].windows[0].used_percent == 10.0
    # Change the underlying source; within TTL the cached snapshot is returned.
    store.record(make_observation(used=80.0))
    clock["t"] = NOW + dt.timedelta(seconds=USAGE_CACHE_TTL_SECONDS - 1)
    assert service.get_usage().providers[0].windows[0].used_percent == 10.0
    # Past the TTL it re-reads.
    clock["t"] = NOW + dt.timedelta(seconds=USAGE_CACHE_TTL_SECONDS + 1)
    assert service.get_usage().providers[0].windows[0].used_percent == 80.0


def test_service_record_invalidates_cache(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = UsageService(config, clock=lambda: NOW)
    assert service.get_usage().providers[0].available is False  # claude unfed
    service.record_claude_observation(make_observation())
    assert service.get_usage().providers[0].available is True


def test_service_fail_soft_isolates_providers(tmp_path: Path) -> None:
    class BoomStore(ClaudeUsageStore):
        def latest(self):  # noqa: ANN001 - test stub
            raise RuntimeError("boom")

    config = make_config(tmp_path)  # empty codex home -> codex unavailable
    service = UsageService(config, claude_store=BoomStore(config.data_dir), clock=lambda: NOW)
    usage = service.get_usage()
    by_provider = {p.provider: p for p in usage.providers}
    assert by_provider[PROVIDER_CLAUDE].available is False
    assert "usage read failed" in (by_provider[PROVIDER_CLAUDE].detail or "")
    assert by_provider[PROVIDER_CODEX].available is False  # independent, still returned


# -- UsageService.claude_observer (the hub-driven feed) -----------------------


def test_service_claude_observer_records_supported_turn(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = UsageService(config, clock=lambda: NOW)
    assert service.get_usage().providers[0].available is False  # claude unfed to start
    service.claude_observer()(supported_claude_stdout(five=33.0))
    claude = {p.provider: p for p in service.get_usage().providers}[PROVIDER_CLAUDE]
    assert claude.available is True  # observing invalidated the cache and re-read
    assert claude.windows[0].used_percent == 33.0
    assert claude.observed_ts == NOW.isoformat()  # stamped from the service clock
    assert claude.stale is False


def test_service_claude_observer_ignores_unsupported_turn(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = UsageService(config, clock=lambda: NOW)
    service.claude_observer()(claude_stream(SYSTEM_EVENT, ASSISTANT_EVENT, claude_result()))
    assert {p.provider: p for p in service.get_usage().providers}[PROVIDER_CLAUDE].available is False
    assert service.store.latest() is None  # unsupported output records nothing


def test_service_claude_observer_refreshes_a_boundary_without_touching_figures(tmp_path: Path) -> None:
    # The 2.1.220 acceptance case: a hub-driven turn carrying only the boundary event
    # moves the stored window's reset instant forward and changes nothing else.
    config = make_config(tmp_path)
    store = ClaudeUsageStore(config.data_dir)
    store.record(bounded_observation())
    later = dt.datetime(2026, 7, 27, 5, 45, tzinfo=UTC)  # past the stored 5h boundary
    service = UsageService(config, claude_store=store, clock=lambda: later)
    assert service.get_usage().providers[0].windows[0].resets_at == STORED_FIVE_RESET  # primes the cache

    service.claude_observer()(claude_stream(SYSTEM_EVENT, BOUNDARY_EVENT, claude_result()))

    stored = store.latest()
    assert stored is not None
    assert stored.windows[0].resets_at == BOUNDARY_ISO
    assert stored.windows[0].used_percent == 4.0  # figures are the sink's, untouched
    assert stored.observed_ts == "2026-07-27T02:30:00+00:00"  # still dates the figures
    claude = {p.provider: p for p in service.get_usage().providers}[PROVIDER_CLAUDE]
    assert claude.windows[0].resets_at == BOUNDARY_ISO  # the refresh invalidated the cache
    assert claude.windows[0].used_percent == 4.0
    assert claude.stale is True  # a boundary refresh is not an observation of figures


def test_service_claude_observer_boundary_alone_records_nothing(tmp_path: Path) -> None:
    # Never observed + a boundary event = still unavailable. The merge creates no
    # window, so a percentage-less "available" card is impossible.
    config = make_config(tmp_path)
    service = UsageService(config, clock=lambda: NOW)
    service.claude_observer()(claude_stream(BOUNDARY_EVENT))
    assert service.store.latest() is None
    assert {p.provider: p for p in service.get_usage().providers}[PROVIDER_CLAUDE].available is False


def test_service_claude_observer_prefers_figures_over_a_boundary(tmp_path: Path) -> None:
    # A stream carrying both is recorded as an observation: it already carries the
    # turn's own boundary, so there is nothing left to merge.
    config = make_config(tmp_path)
    store = ClaudeUsageStore(config.data_dir)
    store.record(bounded_observation())
    service = UsageService(config, claude_store=store, clock=lambda: NOW)
    service.claude_observer()(
        claude_stream(
            BOUNDARY_EVENT,
            claude_result(claude_rate_limits(five_hour={"used_percentage": 60.0, "resets_at": 1785290400})),
        )
    )
    stored = store.latest()
    assert stored is not None
    assert [w.key for w in stored.windows] == ["five_hour"]
    assert stored.windows[0].used_percent == 60.0
    assert stored.windows[0].resets_at == "2026-07-29T02:00:00+00:00"
    assert stored.observed_ts == NOW.isoformat()  # a real observation, so freshly stamped


def test_service_claude_observer_swallows_record_failure(tmp_path: Path) -> None:
    class BoomStore(ClaudeUsageStore):
        def record(self, observation):  # noqa: ANN001 - test stub
            raise RuntimeError("disk full")

    config = make_config(tmp_path)
    service = UsageService(config, claude_store=BoomStore(config.data_dir), clock=lambda: NOW)
    # A supported turn whose record() raises must not propagate out of the observer —
    # usage bookkeeping can never break the driving turn.
    service.claude_observer()(supported_claude_stdout())
    assert {p.provider: p for p in service.get_usage().providers}[PROVIDER_CLAUDE].available is False


# -- HTTP route ---------------------------------------------------------------


def build_client(config: Config, *, authed: bool = True) -> TestClient:
    service = UsageService(config, clock=lambda: NOW)
    client = TestClient(api.create_app(config, usage_service=service))
    if authed:
        client.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return client


def test_usage_route_requires_auth(tmp_path: Path) -> None:
    client = build_client(make_config(tmp_path), authed=False)
    assert client.get("/api/usage").status_code == 401


def test_usage_route_returns_both_providers(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_rollout(config.codex_home, "2026-07-24T00-59-00", [rate_limits_line()])
    client = build_client(config)
    resp = client.get("/api/usage")
    assert resp.status_code == 200
    providers = {p["provider"]: p for p in resp.json()["providers"]}
    assert providers[PROVIDER_CODEX]["available"] is True
    assert providers[PROVIDER_CODEX]["windows"][0]["remaining_percent"] == 94.0
    # Claude store is unfed in session-1 -> unavailable, not zero usage.
    assert providers[PROVIDER_CLAUDE]["available"] is False


def test_usage_route_keeps_the_window_shape_unchanged(tmp_path: Path) -> None:
    # session-1 changes only the ingestion merge, not the public payload: the
    # per-window figure_instant is store-internal and must not appear on GET
    # /api/usage (a window-shape change is deferred Confirm-tier work).
    config = make_config(tmp_path)
    ClaudeUsageStore(config.data_dir).record(five_obs(7.0, WIN_A))
    client = build_client(config)
    providers = {p["provider"]: p for p in client.get("/api/usage").json()["providers"]}
    window = providers[PROVIDER_CLAUDE]["windows"][0]
    assert window["used_percent"] == 7.0
    assert "figure_instant" not in window


def test_usage_route_excludes_secrets(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    write_rollout(
        config.codex_home,
        "2026-07-24T00-59-00",
        [rate_limits_line(account_id="acct_SECRET", access_token="tok_SECRET")],
    )
    client = build_client(config)
    body = client.get("/api/usage").text
    assert "SECRET" not in body


# -- Claude authenticated /usage cache (the primary carrier) -------------------
# The carrier that needs nobody watching: the CLI keeps its own authenticated
# utilization fetch in ~/.claude.json, and `claude -p "/usage"` refreshes it as a
# local command with no model call. These tests pin the parser's whitelist and
# timestamp rule, and drive the collector's whole gate order through a fake runner
# — no real CLI is launched and the operator's real config is never read.

# Verbatim shape observed on Claude Code 2.1.220. Kept as a literal fixture (rather
# than assembled from parts) so a future CLI change shows up as a diff against the
# real thing, including the sibling fields the whitelist must drop.
CACHE_FETCHED_MS = 1785470888223  # 2026-07-31T04:08:08.223+00:00
CACHE_FETCHED_ISO = "2026-07-31T04:08:08.223000+00:00"
CACHE_5H_RESET = "2026-07-31T07:20:00.202915+00:00"
CACHE_7D_RESET = "2026-07-31T22:00:00.202935+00:00"
# The collector's frozen clock, an hour past that fetch — so the fixture cache
# reads as stale by default and a test opts into freshness explicitly. Section-local
# rather than the module's NOW, which predates this verbatim 2.1.220 capture.
CACHE_NOW = dt.datetime(2026, 7, 31, 5, 8, 8, tzinfo=UTC)


def usage_cache(
    *,
    fetched_at_ms: object = CACHE_FETCHED_MS,
    five: object = 8,
    seven: object = 43,
    limits: object = None,
) -> dict:
    """The `cachedUsageUtilization` container as 2.1.220 writes it.

    `limits` defaults to the CLI's own shape with the two window figures mirrored
    into it — that is what a real payload carries, and it is the witness the parser
    checks `utilization` against. Tests that need an unwitnessed, contradicting, or
    differently-shaped list pass their own."""
    return {
        "fetchedAtMs": fetched_at_ms,
        "accountUuid": "12500fbb-90c4-4d35-9dd7-be094ebb2bc8",
        "utilization": {
            "five_hour": {
                "utilization": five,
                "resets_at": CACHE_5H_RESET,
                "limit_dollars": None,
                "used_dollars": None,
                "remaining_dollars": None,
            },
            "seven_day": {
                "utilization": seven,
                "resets_at": CACHE_7D_RESET,
                "limit_dollars": None,
                "used_dollars": None,
                "remaining_dollars": None,
            },
            "seven_day_opus": None,
            "seven_day_sonnet": None,
            # `limits[].percent` mirrors the window figures under an unambiguous
            # name — the in-payload corroboration that `utilization` is 0-100 here.
            "limits": [
                {"kind": "session", "group": "session", "percent": five,
                 "resets_at": CACHE_5H_RESET, "is_active": False},
                {"kind": "weekly_all", "group": "weekly", "percent": seven,
                 "resets_at": CACHE_7D_RESET, "is_active": True},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 6,
                 "scope": {"model": {"id": None, "display_name": "Fable"}},
                 "resets_at": CACHE_7D_RESET, "is_active": False},
            ] if limits is None else limits,
            "extra_usage": {"is_enabled": False, "used_credits": None},
            "spend": {"percent": 0, "enabled": False},
        },
    }


def write_cache(path: Path, cache: object = None, *, extra: dict | None = None) -> Path:
    """Write a ~/.claude.json fixture carrying (or omitting) the usage cache."""
    config: dict = {
        "userID": "user-SECRET",
        "machineID": "machine-SECRET",
        "oauthAccount": {"accountUuid": "acct-SECRET", "emailAddress": "a@b.c"},
        "projects": {"/Users/x/private-repo": {"hasTrustDialogAccepted": True}},
    }
    if extra:
        config.update(extra)
    if cache is not None:
        config["cachedUsageUtilization"] = cache
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


# The verbatim envelope `claude -p "/usage" --output-format json` returns on 2.1.220.
# Its explicit zeros are the per-turn evidence that the slash command stayed inside
# the CLI, so a turn is only a supported success when it reports them.
MODEL_FREE_ENVELOPE = json.dumps(
    {"is_error": False, "subtype": "success", "num_turns": 0,
     "total_cost_usd": 0, "duration_api_ms": 0, "result": "8% used"}
)


class FakeRunner:
    """A recording stand-in for the `claude -p "/usage"` subprocess seam.

    Defaults to the supported model-free envelope, so a test that cares about the
    gate order (not the envelope) gets a turn the collector can verify.

    `effect` may refresh the fixture cache (what a real successful turn does),
    letting a test assert the read-back rather than the call alone."""

    def __init__(self, result: UsageRunResult | None = None, effect=None):
        self.result = result if result is not None else UsageRunResult(stdout=MODEL_FREE_ENVELOPE)
        self.effect = effect
        self.calls: list[tuple[list[str], Path, float]] = []

    def __call__(self, argv, cwd: Path, timeout: float) -> UsageRunResult:
        self.calls.append((list(argv), cwd, timeout))
        if self.effect is not None:
            self.effect()
        return self.result


def collector(path: Path, runner=None, *, now: dt.datetime = CACHE_NOW, **kw):
    """A collector over a fixture cache with a frozen clock and no real subprocess."""
    kw.setdefault("cache_fresh_after", 300)
    kw.setdefault("min_interval", 300)
    return ClaudeUsageCollector(
        cache_path=path, runner=runner or FakeRunner(), clock=lambda: now, **kw
    )


# -- parser --------------------------------------------------------------------


def test_cache_parses_both_windows_verbatim(tmp_path: Path) -> None:
    obs = read_claude_usage_cache(write_cache(tmp_path / "c.json", usage_cache()))
    assert obs is not None
    assert [(w.key, w.used_percent, w.remaining_percent) for w in obs.windows] == [
        ("five_hour", 8.0, 92.0),
        ("seven_day", 43.0, 57.0),
    ]
    five, seven = obs.windows
    assert (five.label, five.window_minutes) == ("5h", 300)
    assert (seven.label, seven.window_minutes) == ("7d", 10080)
    # Percent-first like every other carrier: no message total is invented.
    assert (five.used, five.limit) == (None, None)


def test_cache_observation_time_is_the_cli_fetch_not_now(tmp_path: Path) -> None:
    # The load-bearing timestamp rule: figures are dated by the CLI's own fetch, so
    # a poll that reuses an old cache cannot present it as newly observed.
    obs = read_claude_usage_cache(write_cache(tmp_path / "c.json", usage_cache()))
    assert obs is not None
    assert obs.observed_ts == CACHE_FETCHED_ISO


def test_cache_without_a_usable_fetch_stamp_is_rejected(tmp_path: Path) -> None:
    # Rather than dating it "now" — an undated figure must not become a fresh one.
    for bad in (None, "recently", True, [1]):
        cache = usage_cache(fetched_at_ms=bad)
        assert read_claude_usage_cache(write_cache(tmp_path / "c.json", cache)) is None
    missing = usage_cache()
    assert missing.pop("fetchedAtMs") == CACHE_FETCHED_MS  # the key is really there
    assert read_claude_usage_cache(write_cache(tmp_path / "c.json", missing)) is None


def test_cache_excludes_account_and_credential_fields(tmp_path: Path) -> None:
    # ~/.claude.json is the CLI's whole config: accountUuid sits INSIDE the parsed
    # container, and the account/machine/project siblings are one level up.
    obs = read_claude_usage_cache(write_cache(tmp_path / "c.json", usage_cache()))
    assert obs is not None
    persisted = json.dumps(obs.to_dict())
    assert "SECRET" not in persisted
    assert "12500fbb" not in persisted
    assert "private-repo" not in persisted
    # This container names no tier, and none is invented from the account record.
    assert obs.plan is None


def test_cache_figure_instant_matches_the_window_reset(tmp_path: Path) -> None:
    # Shared identity with the sink carrier, so the same window read from either
    # source merges as one window instead of racing as two.
    obs = read_claude_usage_cache(write_cache(tmp_path / "c.json", usage_cache()))
    assert obs is not None
    assert obs.windows[0].figure_instant == obs.windows[0].resets_at == CACHE_5H_RESET


def test_cache_rejects_out_of_range_and_missing_percentages(tmp_path: Path) -> None:
    # The unit guard this field name cannot make on its own; and a window with no
    # figure drops rather than becoming a stub the card would render as available.
    for bad in (101, -1, None, "8", True):
        cache = usage_cache(five=bad)
        obs = read_claude_usage_cache(write_cache(tmp_path / "c.json", cache))
        assert obs is not None
        assert [w.key for w in obs.windows] == ["seven_day"]


def test_cache_rejects_a_fraction_unit_reading_its_witness_contradicts(tmp_path: Path) -> None:
    # THE unit failure this carrier is exposed to: `utilization` names a field whose
    # unit is source-dependent, and a 0..1 payload would render 8% as "0.08% used" —
    # wrong by 100x, where an unrecognized shape must degrade to unavailable instead.
    # The check is the payload's own `limits[].percent`, not the field name.
    fraction = usage_cache(
        five=0.08,
        seven=0.43,
        limits=[{"kind": "session", "percent": 8}, {"kind": "weekly_all", "percent": 43}],
    )
    assert read_claude_usage_cache(write_cache(tmp_path / "c.json", fraction)) is None


def test_cache_drops_only_the_window_its_witness_contradicts(tmp_path: Path) -> None:
    # Per window, not per payload: a disagreement is evidence about that figure.
    mixed = usage_cache(
        limits=[{"kind": "session", "percent": 8}, {"kind": "weekly_all", "percent": 5}],
    )
    obs = read_claude_usage_cache(write_cache(tmp_path / "c.json", mixed))
    assert obs is not None
    assert [(w.key, w.used_percent) for w in obs.windows] == [("five_hour", 8.0)]


def test_cache_witness_tolerates_rounding_between_the_two_fields(tmp_path: Path) -> None:
    # The two fields were identical in every capture, but they are separately
    # computed; a percentage point of slack keeps ordinary rounding from blanking the
    # card while staying ~99 points away from the fraction reading it must reject.
    close = usage_cache(five=8.4, limits=[{"kind": "session", "percent": 8}])
    obs = read_claude_usage_cache(write_cache(tmp_path / "c.json", close))
    assert obs is not None
    assert [(w.key, w.used_percent) for w in obs.windows] == [("five_hour", 8.4)]


def test_cache_publishes_nothing_without_a_usable_witness(tmp_path: Path) -> None:
    # No witness is not "assume 0-100": an unrecognized schema degrades to the same
    # unavailable card as an absent one.
    for limits in ([], "nope", [{"percent": 8}], [{"kind": "session", "percent": "8"}]):
        cache = usage_cache(limits=limits)
        assert read_claude_usage_cache(write_cache(tmp_path / "c.json", cache)) is None
    missing = usage_cache()
    assert missing["utilization"].pop("limits")  # the key is really there
    assert read_claude_usage_cache(write_cache(tmp_path / "c.json", missing)) is None


def test_cache_witness_is_matched_by_kind_never_by_position(tmp_path: Path) -> None:
    # The list is heterogeneous — it also carries per-model `weekly_scoped` entries —
    # so a positional pairing would witness the 5h window with a Fable-scoped figure.
    scoped_only = usage_cache(
        limits=[{"kind": "weekly_scoped", "percent": 8,
                 "scope": {"model": {"display_name": "Fable"}}}],
    )
    assert read_claude_usage_cache(write_cache(tmp_path / "c.json", scoped_only)) is None


def test_cache_survives_a_number_no_float_can_hold(tmp_path: Path) -> None:
    # JSON bounds no integer, so "valid JSON number" does not mean "convertible":
    # float(10**400) raises OverflowError. This parser promises never to raise, and
    # its caller (the collector) reads it outside any catch-all.
    huge = int("9" * 400)
    undated = usage_cache(fetched_at_ms=huge)
    assert read_claude_usage_cache(write_cache(tmp_path / "c.json", undated)) is None
    obs = read_claude_usage_cache(write_cache(tmp_path / "c.json", usage_cache(five=huge)))
    assert obs is not None and [w.key for w in obs.windows] == ["seven_day"]
    witnessed = usage_cache(limits=[{"kind": "weekly_all", "percent": huge}])
    obs = read_claude_usage_cache(write_cache(tmp_path / "c.json", witnessed))
    assert obs is None


def test_cache_partial_and_absent_windows(tmp_path: Path) -> None:
    cache = usage_cache()
    cache["utilization"]["seven_day"] = None
    obs = read_claude_usage_cache(write_cache(tmp_path / "c.json", cache))
    assert obs is not None and [w.key for w in obs.windows] == ["five_hour"]
    empty = usage_cache()
    empty["utilization"] = {"seven_day_opus": None}
    assert read_claude_usage_cache(write_cache(tmp_path / "c.json", empty)) is None


def test_cache_malformed_never_raises(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    for text in ("", "not json", "[]", '"str"', "null", '{"cachedUsageUtilization": 5}',
                 '{"cachedUsageUtilization": {"fetchedAtMs": 1, "utilization": 7}}'):
        path.write_text(text, encoding="utf-8")
        assert read_claude_usage_cache(path) is None
    assert parse_claude_usage_cache("\x00\xff garbage") is None


def test_cache_absent_or_oversized_file(tmp_path: Path) -> None:
    assert read_claude_usage_cache(tmp_path / "missing.json") is None
    assert read_claude_usage_cache(tmp_path) is None  # a directory
    big = tmp_path / "big.json"
    write_cache(big, usage_cache())
    big.write_text(" " * (MAX_CLAUDE_CONFIG_BYTES + 1), encoding="utf-8")
    assert read_claude_usage_cache(big) is None


def test_cache_observation_merges_with_the_sink_per_window(tmp_path: Path) -> None:
    # The point of returning a plain ClaudeObservation: the cache goes through the
    # existing per-window merge, so an idle sink writer cannot lower a fresher cache
    # figure, and the sink's plan (which this container lacks) is kept.
    obs = read_claude_usage_cache(write_cache(tmp_path / "c.json", usage_cache()))
    assert obs is not None
    stored = ClaudeObservation(
        observed_ts="2026-07-31T03:00:00+00:00",
        plan="max",
        windows=(
            UsageWindow(key="five_hour", label="5h", window_minutes=300,
                        used_percent=3.0, remaining_percent=97.0,
                        resets_at=CACHE_5H_RESET, figure_instant=CACHE_5H_RESET),
        ),
    )
    merged = merge_observations(stored, obs)
    assert merged is not None
    assert merged.plan == "max"
    assert {w.key: w.used_percent for w in merged.windows} == {
        "five_hour": 8.0,
        "seven_day": 43.0,
    }


# -- collector: argv and the no-model-prompt contract --------------------------


def test_collector_argv_is_print_mode_and_isolated(tmp_path: Path) -> None:
    runner = FakeRunner()
    collector(tmp_path / "c.json", runner, cache_fresh_after=0).refresh()
    argv, cwd, timeout = runner.calls[0]
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert argv[-1] == "/usage"
    # Print mode is what skips the workspace-trust dialog and writes no projects
    # entry for the scratch cwd; an interactive PTY session would do neither.
    for flag in ("--safe-mode", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--tools") + 1] == ""
    # The variadic --tools must never sit immediately before the positional prompt.
    assert argv[argv.index("--tools") + 2].startswith("--")
    assert "--dangerously-skip-permissions" not in argv
    assert "--model" not in argv and "--effort" not in argv
    assert timeout == USAGE_POLL_TIMEOUT_SECONDS
    # A fresh scratch dir, never a managed repo, and removed after the turn.
    assert not cwd.exists()


def test_collector_flags_a_turn_that_reached_the_model(tmp_path: Path) -> None:
    # /usage is handled inside the CLI today (num_turns/cost 0), but that is its
    # behavior, not a contract owed to us — so every turn is checked.
    path = write_cache(tmp_path / "c.json", usage_cache())
    for report in ({"num_turns": 1, "total_cost_usd": 0},
                   {"num_turns": 0, "total_cost_usd": 0.02}):
        runner = FakeRunner(UsageRunResult(stdout=json.dumps(report)))
        result = collector(path, runner, cache_fresh_after=0).refresh()
        assert result.refreshed is False
        assert result.detail == REFRESH_MODEL_CALL
        # The real figures still read back — this is a display sidecar.
        assert result.observation is not None


def test_collector_accepts_the_real_model_free_report(tmp_path: Path) -> None:
    # Verbatim success envelope from `claude -p "/usage" --output-format json`.
    path = write_cache(tmp_path / "c.json", usage_cache())
    result = collector(path, FakeRunner(UsageRunResult(stdout=MODEL_FREE_ENVELOPE)),
                       cache_fresh_after=0).refresh()
    assert result.refreshed is True
    assert result.detail == REFRESH_OK


def test_collector_rejects_a_turn_it_cannot_verify_as_model_free(tmp_path: Path) -> None:
    # The other half of the same rule: a turn asked for `--output-format json` and
    # answering with anything that does not report its model-call accounting is a CLI
    # whose behavior we cannot check. Accepting it would leave the guard above
    # enforcing nothing on exactly the versions it exists for.
    path = write_cache(tmp_path / "c.json", usage_cache())
    unverifiable = [
        "5-hour limit: 8% used",                       # human prose, not JSON
        "",                                            # nothing at all
        "[]",                                          # JSON, but not an object
        '"ok"',
        "null",
        json.dumps({"is_error": False, "result": "ok"}),           # no accounting
        json.dumps({"num_turns": 0}),                              # half of it
        json.dumps({"total_cost_usd": 0}),
        json.dumps({"num_turns": "0", "total_cost_usd": "0"}),      # wrong types
        json.dumps({"num_turns": 0, "total_cost_usd": None}),
        json.dumps({"num_turns": True, "total_cost_usd": True}),    # bools are not numbers
    ]
    for stdout in unverifiable:
        result = collector(path, FakeRunner(UsageRunResult(stdout=stdout)),
                           cache_fresh_after=0).refresh()
        assert result.detail == REFRESH_UNSUPPORTED, stdout
        assert result.refreshed is False
        # Still a display sidecar: the last real figures survive, dated by the CLI.
        assert result.observation is not None
        assert result.observation.observed_ts == CACHE_FETCHED_ISO


# -- collector: gate order and failure modes -----------------------------------


def test_collector_refreshes_a_stale_cache_and_reads_it_back(tmp_path: Path) -> None:
    path = write_cache(tmp_path / "c.json", usage_cache(five=8))
    runner = FakeRunner(effect=lambda: write_cache(path, usage_cache(five=41)))
    # CACHE_NOW is an hour past the fixture's fetch stamp, so the cache is stale.
    result = collector(path, runner).refresh()
    assert len(runner.calls) == 1
    assert result.refreshed is True and result.detail == REFRESH_OK
    assert result.observation is not None
    assert result.observation.windows[0].used_percent == 41.0


def test_collector_reuses_a_cache_the_cli_just_fetched(tmp_path: Path) -> None:
    # The CLI re-fetches on its own schedule and serves its cache in between, so
    # polling harder cannot produce fresher data — only spend quota-check calls.
    fresh = int((CACHE_NOW - dt.timedelta(seconds=30)).timestamp() * 1000)
    path = write_cache(tmp_path / "c.json", usage_cache(fetched_at_ms=fresh))
    runner = FakeRunner()
    result = collector(path, runner).refresh()
    assert runner.calls == []
    assert result.refreshed is False and result.detail == REFRESH_CACHE_FRESH
    assert result.observation is not None and result.observation.windows[0].used_percent == 8.0


def test_collector_floors_repeat_attempts_when_there_is_no_fresh_cache(tmp_path: Path) -> None:
    # Distinct from the freshness gate: a missing/broken CLI leaves nothing fresh to
    # reuse, and this is what stops every Overview load re-running a doomed turn.
    path = write_cache(tmp_path / "c.json", usage_cache())
    runner = FakeRunner(UsageRunResult(returncode=1))
    c = collector(path, runner)
    first, second = c.refresh(), c.refresh()
    assert len(runner.calls) == 1
    assert first.detail == "usage refresh failed (exit 1)"
    assert second.detail == REFRESH_BACKOFF
    # Both still surface the last real figures, dated by the CLI's own fetch.
    for result in (first, second):
        assert result.observation is not None
        assert result.observation.observed_ts == CACHE_FETCHED_ISO
        assert result.refreshed is False


def test_collector_retries_once_the_interval_elapses(tmp_path: Path) -> None:
    path = write_cache(tmp_path / "c.json", usage_cache())
    runner = FakeRunner(UsageRunResult(returncode=1))
    clock = {"now": CACHE_NOW}
    c = ClaudeUsageCollector(cache_path=path, runner=runner, clock=lambda: clock["now"],
                             cache_fresh_after=300, min_interval=300)
    c.refresh()
    clock["now"] = CACHE_NOW + dt.timedelta(seconds=301)
    c.refresh()
    assert len(runner.calls) == 2


def test_collector_keeps_previous_figures_on_every_failure_mode(tmp_path: Path) -> None:
    # Authentication failure, an unsupported command, a broken install and a hang
    # all land here; none may erase or re-date the last real observation.
    path = write_cache(tmp_path / "c.json", usage_cache())
    cases = [
        (UsageRunResult(returncode=1, stderr="Invalid API key"), "usage refresh failed (exit 1)"),
        (UsageRunResult(timed_out=True), REFRESH_TIMEOUT),
        (UsageRunResult(stdout=json.dumps({"is_error": True, "num_turns": 0})),
         "usage refresh reported an error"),
    ]
    for run_result, detail in cases:
        result = collector(path, FakeRunner(run_result)).refresh()
        assert result.detail == detail
        assert result.refreshed is False
        assert result.observation is not None
        assert result.observation.observed_ts == CACHE_FETCHED_ISO
        assert result.observation.windows[0].used_percent == 8.0


def test_collector_failure_detail_never_echoes_cli_output(tmp_path: Path) -> None:
    # ~/.claude.json diagnostics can carry account context; stderr stays out.
    path = write_cache(tmp_path / "c.json", usage_cache())
    runner = FakeRunner(UsageRunResult(returncode=1, stderr="auth failed for acct-SECRET"))
    assert "SECRET" not in collector(path, runner).refresh().detail


def test_collector_survives_an_unrunnable_cli(tmp_path: Path) -> None:
    def explode(argv, cwd, timeout):
        raise FileNotFoundError("claude")

    path = write_cache(tmp_path / "c.json", usage_cache())
    result = collector(path, explode).refresh()
    assert result.refreshed is False
    assert "FileNotFoundError" in result.detail
    assert result.observation is not None


def test_collector_reports_no_cache_when_nothing_lands(tmp_path: Path) -> None:
    # A clean turn against a CLI that has never fetched utilization.
    path = write_cache(tmp_path / "c.json")
    result = collector(path, FakeRunner()).refresh()
    assert result.observation is None
    assert result.refreshed is False and result.detail == REFRESH_NO_CACHE


def test_collector_is_single_flight_and_non_blocking(tmp_path: Path) -> None:
    # A refresh triggered from a read path must never make one caller wait on
    # another caller's subprocess: the loser returns immediately, not queued.
    path = write_cache(tmp_path / "c.json", usage_cache())
    entered, release = threading.Event(), threading.Event()

    def blocking(argv, cwd, timeout):
        entered.set()
        release.wait(5)
        return UsageRunResult(stdout=MODEL_FREE_ENVELOPE)

    c = collector(path, blocking)
    outcomes: list[UsageRefreshResult] = []
    worker = threading.Thread(target=lambda: outcomes.append(c.refresh()))
    worker.start()
    assert entered.wait(5)
    contender = c.refresh()  # while the first turn is still running
    assert contender.detail == REFRESH_IN_FLIGHT
    assert contender.refreshed is False and contender.observation is None
    release.set()
    worker.join(5)
    assert [o.detail for o in outcomes] == [REFRESH_OK]


def test_collector_concurrent_callers_run_exactly_one_turn(tmp_path: Path) -> None:
    # A burst of Overview readers must cost one subprocess, not six. Made
    # deterministic rather than timing-dependent: the winner's turn is held open
    # until all five other callers have been shed, so they genuinely contend.
    path = write_cache(tmp_path / "c.json", usage_cache())
    runner, shed, start = FakeRunner(), threading.Semaphore(0), threading.Barrier(6)

    def held(argv, cwd, timeout):
        result = runner(argv, cwd, timeout)
        for _ in range(5):
            assert shed.acquire(timeout=5)
        return result

    c = collector(path, held)
    details: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        start.wait(5)
        result = c.refresh()
        with lock:
            details.append(result.detail)
        if result.detail != REFRESH_OK:
            shed.release()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(runner.calls) == 1
    assert sorted(details) == sorted([REFRESH_OK] + [REFRESH_IN_FLIGHT] * 5)


def test_collector_read_cache_does_not_launch_anything(tmp_path: Path) -> None:
    runner = FakeRunner()
    c = collector(write_cache(tmp_path / "c.json", usage_cache()), runner)
    obs = c.read_cache()
    assert obs is not None and obs.observed_ts == CACHE_FETCHED_ISO
    assert runner.calls == []
