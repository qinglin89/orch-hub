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
    MAX_STATUSLINE_BYTES,
    USAGE_CACHE_TTL_SECONDS,
    ClaudeObservation,
    ClaudeUsageStore,
    UsageService,
    WindowBoundary,
    claude_usage,
    merge_window_boundaries,
    parse_claude_stream_boundaries,
    parse_claude_stream_usage,
    read_claude_statusline,
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


def test_service_statusline_never_regresses_a_fresher_observation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = ClaudeUsageStore(config.data_dir)
    store.record(make_observation(observed_ts="2026-07-24T00:59:00Z", used=20.0))
    service = UsageService(config, claude_store=store, clock=lambda: NOW)
    write_sink(service.statusline_sink, five=41.0, observed_ts="2026-07-24T00:00:00Z")  # older
    assert service.get_usage().providers[0].windows[0].used_percent == 20.0
    write_sink(service.statusline_sink, five=55.0, observed_ts="2026-07-24T00:59:30Z")  # newer
    assert UsageService(config, claude_store=store, clock=lambda: NOW).get_usage().providers[0].windows[
        0
    ].used_percent == 55.0


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
