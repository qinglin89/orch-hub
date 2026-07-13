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


def test_launch_options_dialog_exposes_the_plan_gate_toggle():
    # Plan-gate is a valueless store_true flag, not a [flag, value] override, so
    # it rides its own checkbox (opt-plan-gate) and a dedicated buildExtraArgs
    # branch that appends a bare `--plan-gate` only when checked. Asserted at the
    # served-asset boundary like the overrides above.
    body = app_js()
    assert "opt-plan-gate" in body
    assert "--plan-gate" in body


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


# ---- run-scoped task view (run-archived-task-access) -------------------------
#
# A run's task file is opened through GET /api/runs/{id}/task, which resolves the
# task from the run record (active or archived) instead of the index-gated
# inventory — so the link keeps working after close-out archives the task. The
# Tasks page stays active-only. Asserted at the served-asset boundary (no JS
# runtime in this suite), like the run-control / log-stream checks above.


def test_run_task_view_resolves_task_through_the_run_record():
    body = app_js()
    # A dedicated run-scoped task view + its hash builder + route wiring...
    assert "renderRunTask" in body
    assert "runTaskHash" in body
    assert "renderRunTask(seg[1])" in body
    assert 'seg[2] === "task"' in body
    # ...reading the run-scoped endpoint (never an index-gated /api/repos task
    # route), and surfacing the active/archived source to the reader.
    assert '"/task"' in body
    assert "data.source" in body


def test_log_view_task_link_uses_run_scoped_route():
    body = app_js()
    # The log view's task back-link routes through the run-scoped task view
    # (runTaskHash), not taskHash's active-only task detail, so clicking a run's
    # task link after archival opens the archived file instead of 404-ing.
    assert "runTaskHash(" in body


# ---- remote intake (draft → review → confirm / cancel) -----------------------
#
# Session-3 of remote-intake-task: the dashboard intake + review loop over the
# shipped protected intake HTTP API. Like the run-control checks above, the
# wiring is asserted at the served-asset boundary (no JS runtime in this suite);
# each check maps to a scope/acceptance line. The concrete CLI-backed agent is a
# later slice — these pin the UI contract independent of which agent is wired.


def test_intake_entry_posts_create_on_repo_detail():
    body = app_js()
    # Repo detail carries a repo-scoped "new task intake" entry whose form drives
    # POST /api/intakes with the request text and the repo id.
    assert "intakeSection" in body
    assert "intake-form" in body
    assert '"/api/intakes"' in body
    assert "repo_id" in body  # the create body names the repo...
    assert "data-repo" in body  # ...sourced from the form's dataset


def test_intake_entry_reflects_inflight_and_not_ready():
    body = app_js()
    # The entry pre-reflects the backend gates the create would 409 on: a repo's
    # one in-flight intake links to its review instead of offering a second, and
    # a known-not-ready repo explains the block rather than offering a dead Start.
    assert "INTAKE_TERMINAL" in body  # spots the non-terminal in-flight intake
    assert "inflightIntake" in body
    assert "Review the draft" in body
    assert "not ready, so a task intake" in body
    # A busy 409 carries the existing intake id, so the racing Start links to it.
    assert "intake_id" in body


def test_intake_review_view_is_routed():
    body = app_js()
    # A dedicated intake-review view, its hash builder, and route wiring.
    assert "renderIntakeReview" in body
    assert "intakeHash" in body
    assert 'seg[0] === "intakes"' in body
    assert "renderIntakeReview(seg[1])" in body
    # It reads the record + metadata-only audit events from the history route.
    assert '"/history"' in body


def test_intake_review_shows_full_draft_before_any_write():
    body = app_js()
    # The full proposed task file AND the index row are shown for review before
    # anything is written — the whole point of the confirmation gate.
    assert "intakeDraftView" in body
    assert "draft.task_file" in body
    assert "draft.index_entry" in body
    # Agent clarifications (duplicate overlap / oversized-scope) surface too.
    assert "draft.notes" in body
    assert "intake-notes" in body


def test_intake_review_offers_confirm_revise_cancel():
    body = app_js()
    # The three review outcomes, each wired to its route. Revise sends feedback
    # back into the same session; only Confirm reaches the write route.
    assert "intake-confirm" in body
    assert '"/confirm"' in body
    assert "intake-revise-form" in body
    assert '"/revise"' in body
    assert "feedback" in body
    assert "intake-cancel" in body
    assert '"/cancel"' in body


def test_intake_confirm_hands_off_to_the_existing_task_start_flow():
    body = app_js()
    # Only a confirmed intake routes onward, and it routes to the created task's
    # detail (taskHash) — where the EXISTING Start control launches it. Intake
    # never duplicates run start/stop. The confirm turn now runs off the lock, so
    # this handoff fires both on a synchronous `confirmed` return and when the
    # poll observes the async `confirming`→`confirmed` settle; one shared helper
    # keeps the taskHash target identical for both paths.
    assert "created_task_id" in body
    assert "taskHash(rec.repo_name, rec.created_task_id)" in body


def test_intake_audit_trail_is_metadata_only():
    body = app_js()
    # A metadata-only audit trail (kinds + iteration + detail) renders from the
    # history events, so intake iterations are auditable without the review view
    # depending on a draft body being copied into history.
    assert "intakeAuditSection" in body
    assert "intake-events" in body
    assert "e.kind" in body


# ---- async intake turns (2026-07-12-async-intake-turns, session-2) -----------
#
# The intake agent turn now runs off the request lock on the hub, so a mutating
# route returns the transient record at once and the review view polls
# GET /api/intakes/{id} through the in-flight turn instead of assuming a
# synchronous draft. These pin the polling / elapsed / navigate-away wiring at
# the served-asset boundary (no JS runtime in this suite), like the checks above.


def test_intake_review_polls_a_transient_record_and_stops_when_resting():
    body = app_js()
    # An in-flight intake is polled through a single module-level handle that the
    # router tears down on navigation (mirrors the SSE activeStream discipline),
    # and the poll is started ONLY for a transient status, so a resting record
    # never polls and a left view never keeps reading.
    assert "startIntakePoll" in body
    assert "stopIntakePoll" in body
    assert "activeIntakePoll" in body
    assert "INTAKE_TRANSIENT" in body
    assert "INTAKE_TRANSIENT.has(data.status)" in body  # gates the poll start
    # A chained timeout, never a leak-prone setInterval. Assert the *call* forms
    # so a comment naming setInterval doesn't trip the negative check (as the
    # log-stream test does for `new EventSource`).
    assert "setTimeout(" in body
    assert "setInterval(" not in body


def test_intake_review_shows_live_elapsed_and_navigate_away_note():
    body = app_js()
    # The working panel shows a ticking elapsed time (from the record's
    # updated_ts) and tells the operator the turn keeps running on the hub, so
    # leaving the page is safe — the whole point of moving turns off the lock.
    assert "intakeElapsed" in body  # the elapsed-time builder
    assert "intake-elapsed" in body  # the span it refreshes in place
    assert "updated_ts" in body  # the elapsed anchor (new to the SPA)
    assert "safe to leave this page" in body
    # The old static "Reload to refresh" panel (which assumed a synchronous turn)
    # is gone, replaced by the live poll.
    assert "intake-reload" not in body


def test_intake_confirm_handoff_shared_by_sync_and_polled_settle():
    body = app_js()
    # The created-task handoff lives in one shared helper used by both the
    # synchronous confirm return and the async confirming→confirmed poll settle,
    # so an off-lock confirm ends at the same created-task detail.
    assert "handoffToCreatedTask" in body
    assert "confirming" in body  # the poll's confirmed-settle branch keys on it
