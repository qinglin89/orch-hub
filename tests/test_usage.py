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
from pathlib import Path

from fastapi.testclient import TestClient

from orch_hub import api
from orch_hub.config import Config
from orch_hub.models import PROVIDER_CLAUDE, PROVIDER_CODEX, UsageWindow
from orch_hub.usage import (
    USAGE_CACHE_TTL_SECONDS,
    ClaudeObservation,
    ClaudeUsageStore,
    UsageService,
    claude_usage,
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
