"""Tests for the dashboard static shell and its auth exception.

The dashboard shell is public client-only code (no secrets): the three assets
must serve without a bearer token so the browser can bootstrap, then the SPA
attaches the token to every data fetch. These tests pin that the shell is
public, serves even with no token configured, leaks no server-side token, and
that no data route was accidentally opened while adding the shell.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from orch_hub import api
from orch_hub.config import Config

TOKEN = "test-token"
FAKE_CONFIG = Config(ai_native_path=Path("/fake/ai-native-development"), token=TOKEN)
NO_TOKEN_CONFIG = Config(ai_native_path=Path("/fake/ai-native-development"))
SHELL_PATHS = ("/", "/app.js", "/styles.css")


def unauthed_client(config: Config = FAKE_CONFIG) -> TestClient:
    """Client that sends no Authorization header, to prove the public surface."""
    return TestClient(api.create_app(config))


def test_shell_index_is_public_html():
    resp = unauthed_client().get("/")  # no bearer header
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "orch-hub" in resp.text


def test_shell_script_is_public_javascript():
    resp = unauthed_client().get("/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    # The SPA must talk to the API with a bearer header, not EventSource/cookies.
    assert "Authorization" in resp.text
    assert "Bearer" in resp.text


def test_shell_script_tails_logs_via_fetch_not_eventsource():
    # Session-3 decision: the SSE log stream is consumed with fetch + a
    # ReadableStream reader under the bearer header, never EventSource (which
    # cannot set Authorization). Pin all three at the served-asset boundary so a
    # regression to EventSource / a query-token stream is caught here.
    body = unauthed_client().get("/app.js").text
    assert "/logs/stream" in body  # it does tail the SSE endpoint
    assert "getReader" in body  # via a ReadableStream reader
    # Never *construct* the header-less EventSource (the comment may name it to
    # explain the decision; what must not appear is an actual instantiation).
    assert "new EventSource" not in body


def test_shell_styles_are_public_css():
    resp = unauthed_client().get("/styles.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]


def test_shell_serves_even_without_configured_token():
    # With no ORCH_HUB_TOKEN the data routes fail closed (503), but the operator
    # must still be able to load the app shell to reach the login gate.
    assert unauthed_client(NO_TOKEN_CONFIG).get("/").status_code == 200
    assert unauthed_client(NO_TOKEN_CONFIG).get("/api/repos").status_code == 503


def test_shell_assets_carry_no_server_token():
    # The token is never embedded server-side; it only ever lives in the browser.
    client = unauthed_client()
    for path in SHELL_PATHS:
        assert TOKEN not in client.get(path).text


def test_shell_assets_are_served_no_store():
    # After `orch-hub launchd update` ships a new build, a mobile Chrome/Safari
    # must load the fresh app.js/styles.css on the next request rather than
    # serving a heuristically-cached stale copy (the incognito-mode symptom this
    # task removes). Every shell asset carries an explicit no-store Cache-Control
    # forbidding that caching; FileResponse's own Last-Modified/ETag are inert
    # under no-store. Pinned to the literal policy so weakening it (e.g. to
    # no-cache or a max-age) is caught here, not in the field after a deploy.
    client = unauthed_client()
    for path in SHELL_PATHS:
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"


def test_data_routes_stay_protected_after_adding_shell():
    # Adding the public shell must not have opened any data route.
    client = unauthed_client()
    for path in ("/api/repos", "/api/tasks", "/api/runs", "/openapi.json"):
        assert client.get(path).status_code == 401


def test_public_paths_are_exactly_health_plus_shell():
    # Pin the public allowlist so a future edit can't silently widen it.
    assert api.PUBLIC_PATHS == frozenset({"/api/health", "/", "/app.js", "/styles.css"})


# ---- run controls (start / stop) --------------------------------------------
#
# The dashboard has no JS runtime in this suite, so — like the log-stream
# decision pinned above — the run-control wiring is asserted at the served-asset
# boundary. Each check maps to a scope/acceptance line of the run-controls task:
# Start drives the create call (carrying the blocked-task acknowledgement), Stop
# drives the stop call, launch refusals render their structured 409 detail, and
# the busy / non-runnable / not-ready states are reflected before any POST so the
# backend's per-repo serialization is pre-reflected but never the sole guard.


def app_js() -> str:
    """The served dashboard script (public shell asset)."""
    return unauthed_client().get("/app.js").text


def test_start_control_posts_run_create_with_ack_flag():
    body = app_js()
    # The Start form drives POST /api/runs with the documented body keys,
    # including acknowledge_blocked so a blocked task can still be launched.
    assert "start-form" in body
    assert '"/api/runs"' in body
    for key in ("repo_id", "task_id", "acknowledge_blocked"):
        assert key in body


def test_stop_control_posts_run_stop():
    body = app_js()
    # The Stop form drives POST /api/runs/{run_id}/stop.
    assert "stop-form" in body
    assert "/stop" in body
    assert "Stop run" in body


def test_blocked_task_offers_acknowledgement_checkbox():
    # A blocked task is launchable only with an explicit acknowledgement; the
    # control exposes a checkbox rather than silently acknowledging for the user.
    body = app_js()
    assert "ack-blocked" in body
    assert "data-blocked" in body


def test_launch_refusal_details_are_rendered():
    # A 409 from POST /api/runs carries structured detail a flat message drops:
    # LaunchBlocked `reasons` and the blocked-task `acknowledge_required` nudge.
    # Rendering these is how the UI explains a refused (or raced) launch.
    body = app_js()
    assert "reasons" in body
    assert "acknowledge_required" in body


def test_start_reflects_per_repo_busy_state():
    # Per-repo serialization is the backend's to enforce, but the dashboard
    # pre-reflects it: a live run in the repo replaces Start with a busy note.
    body = app_js()
    assert "repoLiveRun" in body
    assert "one run per repo" in body


def test_start_reflects_non_runnable_and_not_ready_states():
    # Start is offered only for a launchable status on a ready repo; the other
    # states are explained in place instead of offering a launch bound to 409.
    body = app_js()
    assert "RUNNABLE_STATUSES" in body
    assert "not ready to launch" in body


# ---- cc-codex launch options (dialog + extra args) --------------------------
#
# Session-1 of the launch-options task: Start no longer launches immediately —
# it opens an options dialog whose submit builds `extra_args` with a *fixed*
# `--backend cc-codex` plus only the override fields the user changed, then POSTs
# them to the existing /api/runs route. Like the run-control checks above, the
# wiring is asserted at the served-asset boundary (no JS runtime in this suite).


def test_start_run_pins_the_cc_codex_backend():
    # Every dashboard-initiated run fixes the backend to cc-codex; the value is
    # hard-coded in the extra-args builder, never read from a form field.
    body = app_js()
    assert '"--backend"' in body
    assert '"cc-codex"' in body
    assert "buildExtraArgs" in body


def test_start_run_exposes_no_backend_selector():
    # The control must not let the operator pick the alternative (cursor)
    # backend. `cursor` is the only other --backend value, so its total absence
    # from the served script is the proxy for "no backend selector is rendered".
    assert "cursor" not in app_js()


def test_launch_options_dialog_renders_the_supported_overrides():
    # The options dialog exposes exactly the supported cc-codex overrides, each
    # with an input/select field. buildExtraArgs trims and skips blanks, so an
    # unchanged field never reaches the argv and keeps the orchestrator default.
    body = app_js()
    assert "run-options" in body  # the <dialog>
    assert "open-run-options" in body  # the button that opens it
    for flag in (
        "--dev-agent",
        "--dev-model",
        "--dev-effort",
        "--review-model",
        "--review-effort",
    ):
        assert flag in body
    for cls in (
        "opt-dev-agent",
        "opt-dev-model",
        "opt-dev-effort",
        "opt-review-model",
        "opt-review-effort",
    ):
        assert cls in body


def test_launch_dialog_submit_sends_extra_args_and_keeps_ack():
    # Start still carries acknowledge_blocked (blocked-task launch) and now also
    # sends the constructed extra_args to the same /api/runs create route.
    body = app_js()
    assert "extra_args" in body
    assert "acknowledge_blocked" in body
    assert '"/api/runs"' in body


# ---- startup-failure diagnostic (spawn.out) ---------------------------------
#
# Session-2 of the launch-options task: a run that exits before writing an
# orchestrator log exposes its spawn.out capture through the logs route's
# `diagnostic`, and the log view renders that fallback instead of an empty view.
# Asserted at the served-asset boundary (no JS runtime in this suite).


def test_log_view_renders_spawn_out_startup_diagnostic():
    body = app_js()
    # The recent-log renderer reads the route's `diagnostic` fallback...
    assert "diagnostic" in body
    # ...names the capture in the user-facing status note...
    assert "startup diagnostics" in body
    assert "spawn.out" in body


# ---- repo/task status layout (session-2 of dashboard-status-layout) ----------
#
# Frontend-only slice: a tri-state repo run signal, ready-repo metadata (latest
# commit + active task count), a task-card run-state accent kept distinct from
# the status badge, and a recent-commits section with a graceful unavailable
# state. Asserted at the served-asset boundary (no JS runtime in this suite),
# matching the run-control / log-stream checks above; each maps to a scope line.


def test_repo_card_run_signal_is_a_leading_dot_without_text():
    body = app_js()
    # The repo run-state is a compact colour-coded dot *leading the repo name*
    # (yellow=waiting, green=active, gray=idle), matching the orch-hub-test
    # presentation. It replaces the old right-side dot+word signal: the state
    # word rides title/aria-label only, never as visible text.
    assert "run-dot" in body  # the leading dot's class
    assert "runDot" in body  # its builder
    assert "repoRunState" in body  # still collapses the repo's live runs to one state
    for kind in ("waiting", "active", "idle"):
        assert kind in body  # all three dot states are handled
    # The dot is built inside the .name span, before the escaped repo name, so
    # it visually leads the name.
    assert 'class="name">${runDot(' in body
    # The old dot+word signal form is gone entirely (builder + class), which is
    # what guarantees no visible run-state word rides beside the repo name.
    assert "runSignal" not in body
    assert "run-signal" not in body


def test_ready_repo_card_shows_commit_and_active_task_count():
    body = app_js()
    # Ready cards surface the latest commit time (from the /api/repos `commit`)
    # and the active task count. The count is the task-inventory size, sourced
    # from /api/tasks — explicitly not a raw `.ai-tasks/` directory file count.
    assert "repoMetaLine" in body
    assert "committed_ts" in body
    assert "taskCountsByRepo" in body
    assert '"/api/tasks"' in body  # the count's source
    assert "active task" in body


def test_task_card_run_accent_is_border_only():
    body = app_js()
    # Run-state on a task card is ONLY a colour-coded left-border accent, never
    # another status pill and no longer a separate dot/word row, so a runtime
    # `waiting` is not confused with a protocol `blocked` badge and the run
    # state is not redundantly repeated after the border already shows it.
    assert "run-waiting" in body
    assert "run-active" in body
    assert "task-run" not in body  # no separate run-signal row under the head
    # The frontmatter status still drives the pill via statusBadgeKind — the
    # separate channel that keeps status and run-state visually distinct.
    assert "statusBadgeKind" in body


def test_repo_detail_shows_recent_commits_with_graceful_unavailable():
    body = app_js()
    # A recent-commits section at the bottom of repo detail, fed by the commits
    # route, degrading to a graceful note when git can't be read.
    assert "commitsSection" in body
    assert "/commits" in body
    assert "Recent commits" in body
    assert "available" in body  # distinguishes unreadable git from empty history
    assert "unavailable" in body.lower()


def test_housekeeping_warning_renders_on_repo_detail_and_repo_card():
    body = app_js()
    assert "housekeeping_pending" in body
    assert "housekeepingWarning" in body
    assert "Housekeeping pending" in body
    assert "repo.housekeeping_pending ? housekeepingWarning(true)" in body
    assert "housekeepingPending ? housekeepingWarning(false)" in body
