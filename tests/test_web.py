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


def test_stop_control_renders_requested_state_from_the_record():
    # A requested stop is observable server state, not transient button text: the
    # run control reads run.stop_requested and renders the disabled "Stop
    # requested" button from it, so a reload keeps showing it instead of
    # re-enabling Stop (the post-POST transient text in onStopSubmit remains the
    # immediate-feedback path). Asserted at the served-asset boundary.
    body = app_js()
    assert "run.stop_requested" in body
    assert "Stop requested" in body
    assert "disabled>Stop requested" in body


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


# ---- per-session context strips (session-context-usage, session-2) -----------
#
# Run detail (live runs) and run-history cards render a per-session context strip
# from the sessions payload — GET /api/runs/{id}/sessions for a live run, and the
# per-run snapshot embedded in /api/history/runs for an ended one — each session
# shown against the soft context budget with a clamped meter. Asserted at the
# served-asset boundary (no JS runtime in this suite), like the checks above.


def test_session_strip_builder_renders_context_against_budget():
    body = app_js()
    # The strip builder + list wrapper, the escaped `context≈` figure, and the
    # clamped meter that visualises context against the budget.
    assert "sessionStrip" in body
    assert "sessionStrips" in body
    assert "context≈" in body
    assert "session-strip" in body
    assert "ctx-meter" in body
    # Tokens are compacted to "k" and the sid is shortened for the strip.
    assert "fmtTokensK" in body
    assert "shortSid" in body


def test_run_detail_fetches_sessions_for_live_runs_only():
    body = app_js()
    # Run detail pulls the per-session context for a LIVE run only (an ended run
    # gets its strips in history), best-effort so a failure never hides the task.
    assert "/sessions" in body
    assert "context_budget" in body
    assert "LIVE_STATES.has(run.state)" in body
    # The fetched payload is threaded into the run status section for rendering.
    assert "runStatusSection(run, sessions)" in body


def test_run_history_card_embeds_session_strips():
    body = app_js()
    # History cards render each run's embedded sessions snapshot (no extra
    # per-run fetch) via the same strip builder.
    assert "sessionStrips(run.sessions, run.context_budget)" in body


# ---- completed-task history: grouping + est-vs-actual rollup (session-2-cont) -
#
# Run history groups the flat, newest-first /api/history/runs list by (repo,
# task) — archived tasks included, since run history is independent of task-file
# archival — into per-task cards with an est-vs-actual rollup: dev sessions used
# (dev role, session kind; plan-gate/review excluded) vs the embedded
# session_est total, peak per-session context, and summed wall-clock from the
# run-record ISO stamps. Asserted at the served-asset boundary (no JS runtime).


def test_run_history_groups_runs_by_task_with_est_rollup():
    body = app_js()
    # renderRuns groups by (repo, task) and renders a per-task rollup + card list.
    assert "groupRunsByTask" in body
    assert "taskGroupCard" in body
    assert "taskRollup" in body
    assert "task-group" in body
    assert "task-rollup" in body
    # The runs view builds grouped task cards rather than a flat run list; the
    # grouping now runs over the filtered set inside the partial re-render.
    assert "groupRunsByTask(filtered)" in body
    assert "groups.map(taskGroupCard)" in body


def test_run_history_rollup_counts_dev_sessions_against_est():
    body = app_js()
    # "Actual" = dev sessions only (role dev, kind session); a plan-gate and a
    # review session are excluded, matching what frontmatter session-est counts.
    assert "countDevSessions" in body
    assert 's.role === "dev" && s.kind === "session"' in body
    # The est total comes from the per-run embedded session_est field.
    assert "session_est" in body
    assert "firstEst" in body


def test_run_history_rollup_shows_peak_context_and_wall_clock():
    body = app_js()
    # Peak per-session context and summed wall-clock from run-record ISO
    # started_ts/ended_ts (never the log HH:MM:SS), via a compact duration fmt.
    assert "peakContext" in body
    assert "sumWallClockSecs" in body
    assert "fmtDuration" in body
    assert "wall-clock" in body
    assert "started_ts" in body and "ended_ts" in body


# ---- run-history navigation: filters + counts + collapse ---------------------
# (2026-07-17-run-history-navigation)
#
# The Runs view gains client-side discovery over the same /api/history/runs
# payload (no API change, no refetch): repo + run-state + task-id-substring
# filters that compose as AND, an accurate filtered group/run count, per-group
# collapse that keeps the summary, a clear-filters reset, and a no-match state
# distinct from empty history — while every visible run stays linked to its log
# view and newest-first grouping is preserved. Asserted here at the served-asset
# boundary (no JS runtime in this suite), like the run-control / rollup checks
# above; the actual filter/collapse *dispatch* was driven under a jsc
# stubbed-browser harness recorded in the task session log. Each check maps to a
# scope/acceptance line.


def test_run_history_offers_repo_state_task_filters():
    body = app_js()
    # Three discovery controls on the runs view.
    assert 'id="rf-repo"' in body
    assert 'id="rf-state"' in body
    assert 'id="rf-task"' in body
    # Composed client-side over the fetched payload (held filter state, one
    # predicate), so changing a filter re-filters without a refetch.
    assert "runFilters" in body
    assert "matchesRunFilters" in body
    # Dropdown domains come from the payload itself, not a new API.
    assert "historyRepoNames" in body
    assert "orderedRunStates" in body


def test_run_history_filters_compose_as_and_over_the_three_fields():
    body = app_js()
    # All three fields are consulted in the same predicate (AND; empty =
    # wildcard). Task is a trimmed case-insensitive substring on task_id;
    # repo/state are exact matches.
    assert "runFilters.repo" in body
    assert "runFilters.state" in body
    assert "runFilters.task" in body
    assert "run.repo_name !== runFilters.repo" in body
    assert "run.state !== runFilters.state" in body
    assert 'String(run.task_id || "").toLowerCase().includes(q)' in body


def test_run_history_shows_filtered_group_and_run_counts():
    body = app_js()
    # A count line whose values come from the filtered groups/runs, so it tracks
    # the active filters rather than the whole payload.
    assert 'id="run-count"' in body
    assert "groups.length" in body
    assert "filtered.length" in body
    assert "group" in body and "run" in body


def test_run_history_groups_are_collapsible_without_losing_summary():
    body = app_js()
    css = styles_css()
    # Each group header is a real button toggling collapsed state, tracked by a
    # stable per-group key so a filter re-render preserves it.
    assert 'class="task-group-head" type="button"' in body
    assert "aria-expanded" in body
    assert "data-group-key" in body
    assert "collapsedGroups" in body
    # Delegated toggle on the stable container (survives partial re-renders).
    assert "onRunGroupClick" in body
    assert 'classList.toggle("collapsed")' in body
    # Collapse hides only the run-card list; header + rollup summary stay.
    assert ".task-group.collapsed .list" in css
    assert ".tg-caret" in css


def test_run_history_offers_a_clear_filters_action():
    body = app_js()
    # A clear control that resets all three filters back to the full set.
    assert 'id="rf-clear"' in body
    assert ".filters-clear" in styles_css()


def test_run_history_distinguishes_empty_history_from_no_match():
    body = app_js()
    # Two distinct states: an empty payload vs. a non-empty payload the filters
    # exclude entirely.
    assert "No runs recorded yet." in body
    assert "No runs match these filters." in body


def test_run_history_runs_stay_linked_to_logs_after_navigation():
    body = app_js()
    # Every rendered run card still taps through to its existing log tail.
    assert "logsHash(run.run_id)" in body


# ---- all-tasks search + result controls (2026-07-17-task-search-controls) -----
#
# The all-tasks view gains a free-text search that composes with the existing
# repo/status dropdowns (client-side, no API change, no refetch): a case-
# insensitive substring over task id, summary, repo name, status, and blocker
# text; a visible-of-total count; a one-action Clear; a no-match state distinct
# from an empty inventory; and preserved missing-index / sentinel visibility.
# Asserted at the served-asset boundary (no JS runtime in this suite), like the
# run-history filter checks above; the actual search/compose/count/clear
# *dispatch* was driven under a jsc stubbed-browser harness recorded in the task
# session log (39/39). Each check maps to a scope/acceptance line.


def test_all_tasks_offers_a_search_control():
    body = app_js()
    # A text search on the all-tasks view, composed client-side over the held
    # filter state (one predicate), so typing re-filters without a refetch.
    assert 'id="f-search"' in body
    assert 'type="search"' in body
    assert "taskFilters.search" in body
    assert "matchesTaskSearch" in body
    # Live filtering on input, and the free-text value survives view re-entry.
    assert 'document.getElementById("f-search").addEventListener("input"' in body


def test_task_search_matches_id_summary_repo_status_and_blockers():
    body = app_js()
    # One case-insensitive, trimmed substring over exactly the human-scanned
    # fields: id, summary, repo name, status, and every blocker entry.
    assert "[task.task_id, task.summary, repoName, task.status]" in body
    assert ".concat(task.blockers || [])" in body
    assert "taskFilters.search.trim().toLowerCase()" in body
    assert ".join(\" \")" in body and ".toLowerCase()" in body


def test_task_search_composes_with_repo_and_status_filters():
    body = app_js()
    # AND across repo (selectedRepos), status (exact), and search (substring):
    # all three gate the same task loop, empty = wildcard.
    assert "repos.filter((r) => r.name === taskFilters.repo)" in body
    assert "taskFilters.status && task.status !== taskFilters.status" in body
    assert "if (!matchesTaskSearch(task, repo.name)) continue;" in body


def test_all_tasks_shows_visible_and_total_counts():
    body = app_js()
    # A live count line reporting how many tasks are shown out of the whole
    # inventory (the stable denominator), announced politely for assistive tech.
    assert 'id="task-count"' in body
    assert "aria-live=\"polite\"" in body
    assert "Showing ${visible} of ${total} task" in body


def test_all_tasks_offers_a_clear_filters_action():
    body = app_js()
    # One Clear control that resets every task filter back to the full set.
    assert 'id="f-clear"' in body
    assert ".filters-clear" in styles_css()
    assert 'taskFilters.repo = "";' in body
    assert 'taskFilters.status = "";' in body
    assert 'taskFilters.search = "";' in body


def test_all_tasks_no_match_names_a_clear_recovery_distinct_from_empty():
    body = app_js()
    # Active-filter no-match identifies filters are active and points at Clear;
    # a truly empty inventory is a separate message.
    assert "No tasks match the current filters. Use Clear to reset." in body
    assert "No tasks found." in body


def test_all_tasks_preserves_missing_index_and_sentinel_visibility():
    body = app_js()
    # Missing-index cards follow the repo filter only (pushed before the search-
    # gated task loop), so status/search never silently hide a repo-level gap.
    assert "if (!repo.index_present) cards.push(missingIndexCard(repo.name));" in body
    # The no-match note is appended, not a replacement, so problem cards remain.
    assert "cards.push(" in body


# ---- kind-aware answer-mode buttons (2026-07-16-escalation-mode-buttons) ------
#
# Session-2 web slice: the single "Send answer" button is replaced by a
# kind-aware two-button form. Each button carries an explicit answer `mode` the
# hub normalizes against the question's kind — Discuss/Answer for an ordinary
# escalation, Feedback/Confirm for a plan-gate. A plan-gate approval is one tap
# on an empty box; a guard 400 rides the existing inline error path; a hint names
# "stop". Asserted at the served-asset boundary (no JS runtime in this suite),
# like the run-control / intake checks above; each maps to a scope/acceptance
# line. The mode *dispatch* itself (which button → which mode, empty-confirm) was
# verified locally with a jsc stubbed-browser harness (task session log); the
# committed suite deliberately does not depend on that runtime.


def styles_css() -> str:
    """The served dashboard stylesheet (public shell asset)."""
    return unauthed_client().get("/styles.css").text


def test_answer_form_sends_two_explicit_modes_through_one_sender():
    body = app_js()
    # Two buttons, each declaring its intent as an explicit `mode` (the two
    # ANSWER_MODES the hub normalizes), wired to one sender that threads that
    # mode into the POST body. The buttons are clicked, not submitted, so the
    # single pre-mode "Send answer" submit button is gone.
    assert 'data-mode="discuss"' in body
    assert 'data-mode="answer"' in body
    assert "sendAnswer(form, button)" in body  # per-button click dispatch
    assert "const mode = button.dataset.mode;" in body
    assert 'body: { answer, responder: "dashboard", mode }' in body  # mode in body
    assert "Send answer" not in body  # the pre-mode single button is gone


def test_answer_buttons_are_kind_aware():
    body = app_js()
    # Labels switch on the plan-gate kind: an ordinary escalation reads as
    # Discuss/Answer, a plan-gate confirmation loop as Feedback/Confirm.
    assert 'q.kind === "plan-gate"' in body
    assert '"Discuss"' in body and '"Answer"' in body
    assert '"Feedback"' in body and '"Confirm"' in body


def test_plan_gate_approval_is_one_tap_on_empty():
    body = app_js()
    # The plan-gate answer (Confirm) button is the one empty-allowed input: it
    # carries data-allow-empty and the sender lets an empty box through only for
    # it (the hub fills the confirm word). Every other empty reply is blocked
    # locally to match the server's non-empty refusal without a round-trip.
    assert 'data-allow-empty="1"' in body
    assert 'button.dataset.allowEmpty === "1"' in body
    assert "if (!answer && !allowEmpty)" in body
    assert "Enter a reply first." in body


def test_answer_form_hint_names_stop_ending_the_run():
    body = app_js()
    # A hint line under the buttons names that "stop" ends the run (task scope);
    # the escaped hint keeps the literal word so it survives escapeHtml.
    assert "answer-hint" in body
    assert "to end the run" in body
    assert "stop" in body


def test_answer_guard_400_uses_the_inline_error_path():
    body = app_js()
    # A fail-closed guard 400 (mode/kind mismatch) surfaces through the same
    # inline answer-status path as before: the ApiError message (the 400 detail)
    # is written into the status span, not swallowed to a generic failure.
    assert "answer-status" in body
    assert (
        'err instanceof ApiError ? err.message : "Failed to send answer."'
        in body
    )


def test_discuss_button_has_a_distinct_secondary_style():
    css = styles_css()
    # The discuss/feedback (secondary) button is visually distinct from the
    # filled primary Answer/Confirm button so the binding action is unambiguous.
    assert ".answer-discuss" in css


# ---- operations overview (2026-07-17-operations-overview-dashboard, session-1) --
#
# A client-side Overview aggregates the four existing protected APIs (/api/repos,
# /api/tasks, /api/runs, /api/escalations) into a summary-metric row, a
# prioritized "needs attention" panel (waiting-for-answer runs, then readiness
# failures, then housekeeping), and a current-activity panel of live runs. It is a
# nav entry and the default authenticated landing view, and each urgent item
# deep-links to the operational view that acts on it. No new HTTP API. Asserted at
# the served-asset boundary (no JS runtime in this suite); the pure aggregation
# itself was verified with a jsc stubbed-browser harness (session log). Recent
# outcomes + bounded refresh are session-2. Each check maps to a scope line.


def index_html() -> str:
    """The served dashboard shell HTML (public shell asset)."""
    return unauthed_client().get("/").text


def test_overview_is_nav_entry_and_default_landing():
    body = app_js()
    html = index_html()
    # A first-position Overview nav tab...
    assert 'data-tab="/overview"' in html
    assert 'href="#/overview"' in html
    # ...routed to renderOverview, marked active, and the empty-hash default view.
    assert "renderOverview" in body
    assert 'seg[0] === "overview"' in body
    assert 'path.startsWith("/overview")' in body
    assert '|| "/overview"' in body  # currentPath's empty-hash default moved here


def test_overview_aggregates_the_four_existing_apis_best_effort():
    body = app_js()
    # It consumes exactly the existing protected payloads — no new route added.
    for path in ('"/api/repos"', '"/api/tasks"', '"/api/runs"', '"/api/escalations"'):
        assert path in body
    # Parallel fetch through one best-effort wrapper: a non-401 source failure
    # degrades to null (that panel only); a 401 rethrows and re-gates.
    assert "overviewSource" in body
    assert "Promise.all" in body


def test_overview_summary_metrics_reuse_source_counts():
    body = app_js()
    assert "overviewMetrics" in body
    assert "overview-metrics" in body
    assert "readinessSummary" in body  # repos ready/total
    assert "taskCountsByRepo" in body  # active-task count agrees with the repo cards
    assert "LIVE_STATES" in body  # live-run count from the shared run partition
    assert "awaiting answer" in body  # the waiting-for-answer metric


def test_overview_attention_panel_prioritizes_actionable_items():
    body = app_js()
    assert "attentionPanel" in body
    assert "Needs attention" in body
    # Waiting-for-answer is built from waiting runs merged with escalations, so a
    # waiting run whose question file is unparseable still surfaces (not only the
    # live-run-scoped /api/escalations list).
    assert "waitingAttentionItems" in body
    assert 'run.state !== "waiting"' in body
    # Readiness failures and housekeeping are the other actionable repo problems,
    # surfaced with their own overview sub-labels.
    assert "readiness failed" in body
    assert "housekeeping pending" in body


def test_overview_urgent_items_deep_link_to_operational_views():
    body = app_js()
    # The attention list deep-links each item to the view that acts on it: a
    # waiting item to the task detail (where the answer form lives), a repo
    # problem to repo detail, a live run to its logs.
    assert "attention-list" in body
    assert "taskHash(" in body
    assert "repoHash(" in body
    assert "logsHash(" in body


def test_overview_current_activity_lists_live_runs():
    body = app_js()
    assert "currentActivityPanel" in body
    assert "Current activity" in body
    assert "No live runs" in body


def test_overview_metric_tiles_use_the_shared_status_colours():
    css = styles_css()
    # The summary-metric row is a responsive grid, and a tile's figure is coloured
    # by the shared ok/warn/run vocabulary so the overview reads consistently with
    # the badges elsewhere.
    assert ".overview-metrics" in css
    assert ".metric-value" in css
    for kind in (".metric.ok", ".metric.warn", ".metric.run"):
        assert kind in css
    # Attention rows reuse the run-accent left border, colour-coded by kind.
    assert ".overview-attention .attn.attn-waiting" in css
    assert ".attn.attn-not-ready" in css


# ---- operations overview (2026-07-17-operations-overview-dashboard, session-2) --
#
# session-2 completes the Overview: a "recent outcomes" panel (the last few ended
# runs from /api/history/runs), a bounded background refresh that repaints in
# place while the Overview is routed and tears down on navigation (a chained
# setTimeout — never setInterval — so requests never overlap), refined per-panel
# partial-error placeholders, and Overview-scoped responsive polish. Still no new
# HTTP API and still asserted at the served-asset boundary (no JS runtime in this
# suite); the refresh lifecycle + aggregation were verified with a jsc
# stubbed-browser harness (session log). Each check maps to a scope/acceptance
# line.


def test_overview_recent_outcomes_from_history_api():
    body = app_js()
    # Recent outcomes come from the existing history payload — no new route — as a
    # fifth best-effort source with a bounded window.
    assert '"/api/history/runs?limit="' in body
    assert "OVERVIEW_HISTORY_LIMIT" in body
    assert "recentOutcomesPanel" in body
    assert "recentOutcomeCard" in body
    assert "recentOutcomeList" in body
    assert "Recent outcomes" in body
    # Only ended runs are shown (live runs stay in current activity), newest-first
    # and capped so the Overview stays glanceable.
    assert "!LIVE_STATES.has(r.state)" in body
    assert "OVERVIEW_OUTCOME_COUNT" in body
    # Each outcome deep-links to its log tail (which back-links to the task file).
    assert "logsHash(run.run_id)" in body


def test_overview_bounded_refresh_lifecycle():
    body = app_js()
    # A single module-level refresh handle with start/stop, mirroring the intake
    # poll's teardown discipline.
    assert "activeOverviewRefresh" in body
    assert "startOverviewRefresh" in body
    assert "stopOverviewRefresh" in body
    assert "OVERVIEW_REFRESH_MS" in body
    # A chained setTimeout, never setInterval, so a slow multi-source load can't
    # overlap the next fetch.
    assert "setTimeout(" in body
    assert "setInterval(" not in body
    # The refresh is torn down on navigation, right beside the intake poll.
    assert "stopIntakePoll();\n    stopOverviewRefresh();" in body
    # render is split so a refresh tick repaints without the initial loader flash.
    assert "function loadOverview" in body
    assert "function paintOverview" in body


def test_overview_panels_degrade_independently():
    body = app_js()
    # Each panel reads its own source and shows its own "unavailable" placeholder,
    # so one failed source never blanks the unrelated panels.
    assert "Attention data is unavailable." in body
    assert "Run activity is unavailable." in body
    assert "Recent outcomes are unavailable." in body
    # An empty (not failed) ended set is distinct from an unavailable source.
    assert "No recent runs." in body


def test_overview_panels_are_responsive():
    css = styles_css()
    # The lower panels share a wrapper laid out two-up on a wider screen, while the
    # priority "needs attention" panel spans the full width.
    assert ".overview-panels" in css
    assert "min-width: 640px" in css
    assert "grid-column: 1 / -1" in css
    assert ".overview-panels > .overview-attention" in css


# ---- dashboard accessibility (2026-07-17-dashboard-accessibility) -------------
#
# Structural a11y wiring pinned at the served-asset boundary, like every other
# SPA check above. The *behaviour* these enable — focus actually moving on a
# route change, Escape closing the launch dialog, focus returning to its opener,
# every control reachable by Tab — is driven for real against this app.js under
# a stubbed browser in tests/test_web_keyboard.py; these tests guard the markup
# and stylesheet contract that behaviour rests on. Each maps to a scope line.


def test_shell_offers_a_skip_link_into_a_focusable_main():
    html = index_html()
    body = app_js()
    # A skip link is the document's first tab stop...
    assert 'class="skip-link"' in html
    assert 'id="skip-link"' in html
    # ...whose href is inert on purpose: a real "#view" navigation would be read
    # by the hash router as a route, so app.js preventDefaults and focuses #view.
    assert 'document.getElementById("skip-link")' in body
    assert "ev.preventDefault();" in body
    # The main region is a programmatic focus target (the skip-link destination
    # and the fallback for a view that renders no heading).
    assert 'id="view" class="view" tabindex="-1"' in html


def test_view_region_is_not_a_live_region():
    # The whole view is repainted by the Overview auto-refresh, the intake poll,
    # and every streamed log line. As an aria-live region that made assistive
    # tech narrate the entire page every few seconds, so announcements moved to
    # the small regions a human actually acted on. Pinned as a negative: the
    # <main> element must carry no aria-live.
    html = index_html()
    main_tag = html[html.index("<main") : html.index(">", html.index("<main")) + 1]
    assert "aria-live" not in main_tag
    # The narrow polite regions (filter result counts) are still live.
    assert 'aria-live="polite"' in app_js()


def test_primary_navigation_is_named_and_marks_the_current_tab():
    html = index_html()
    body = app_js()
    assert '<nav id="nav" class="tabbar" aria-label="Primary"' in html
    # Active tab state is exposed semantically, not by colour alone.
    assert 'a.setAttribute("aria-current", "page")' in body
    assert 'a.removeAttribute("aria-current")' in body


def test_every_view_opens_with_one_page_heading():
    html = index_html()
    body = app_js()
    # The banner title is no longer an h1, so the per-view heading is the only
    # one and always describes the current page.
    assert '<span class="app-title">orch-hub</span>' in html
    assert "<h1" not in html
    # One shared builder emits `h1.page-title` as a programmatic focus target.
    assert 'const pageTitle = (text, extraClass)' in body
    assert '<h1 class="page-title' in body
    assert 'tabindex="-1"' in body
    # Every routed view names itself, including the loading placeholder.
    assert "function loadingView" in body or "const loadingView" in body
    for title in (
        '"Operations overview"',
        '"Repos"',
        '"Tasks"',
        '"Run history"',
        '"Task intake"',
        '"Connect to orch-hub"',
    ):
        assert title in body


def test_navigation_moves_focus_and_names_the_document():
    body = app_js()
    # One landing helper: name the page in document.title, then focus its start.
    assert "function syncDocumentTitle" in body
    assert "function focusPageStart" in body
    assert "function landOnView" in body
    assert 'document.querySelector("#view .page-title")' in body
    assert '" · orch-hub"' in body
    # Called by the router (a navigation) and by rerender (an action that
    # repaints the whole view) — the two moments the focused control is gone.
    assert "landOnView();\n  }\n\n  function signOut" in body
    # Scoped to rerender's body rather than pinned to one expression: whether it
    # lands is guarded (a navigation during the re-render's fetch makes the
    # landing stale), and that guard is behaviour the keyboard harness owns.
    assert "landOnView();" in body[body.index("function rerender(") :][:400]


def test_background_repaints_do_not_steal_focus():
    body = app_js()
    # The Overview heading lives outside the repainted region, so the refresh
    # can neither destroy the focus target nor re-announce the page...
    assert '`<div id="overview-body"></div>`' in body
    assert 'document.getElementById("overview-body")' in body
    # ...and a tick skips the repaint entirely while focus is inside that region.
    assert "function focusInsideOverviewBody" in body
    assert "if (data && !focusInsideOverviewBody()) paintOverview(data);" in body


def test_disabled_controls_hand_focus_back():
    body = app_js()
    # Disabling the pressed button drops focus to <body>; a failed action must
    # put it back rather than strand the keyboard user at the top of the page.
    assert "function restoreFocus" in body
    assert "if (!busy) restoreFocus(button);" in body
    # A local validation refusal sends focus to the field that must be fixed.
    assert "input.focus(); // send the keyboard user to the field they must fill" in body


def test_launch_dialog_is_named_cancellable_and_restores_focus():
    body = app_js()
    # Named by its own heading...
    assert '<dialog class="run-options" aria-labelledby="run-options-title">' in body
    assert '<h3 id="run-options-title">Start run</h3>' in body
    # ...dismissable from the keyboard (the no-showModal fallback has no native
    # Escape, so app.js owns the key)...
    assert 'if (ev.key === "Escape") closeRunOptions(dialog);' in body
    # ...and focus returns to whatever opened it, on every dismissal path.
    assert "let dialogOpener = null;" in body
    assert 'dialog.addEventListener("close", () => restoreFocus(dialogOpener));' in body
    # The fallback path focuses into the dialog by hand (showModal does it natively).
    assert 'dialog.querySelector("select, input, textarea, button")' in body


def test_form_controls_carry_accessible_names():
    body = app_js()
    # Every free-text control is named: explicit label/for where a visible label
    # exists, aria-label where the design carries the context another way.
    assert '<label for="token-input">Bearer token</label>' in body
    assert 'aria-label="Reply to question ' in body
    assert 'for="intake-request"' in body
    assert 'for="intake-feedback"' in body
    for field in ("f-repo", "f-status", "f-search", "rf-repo", "rf-state", "rf-task"):
        assert f'<label for="{field}"' in body
    for field in ("opt-dev-agent", "opt-dev-model", "opt-review-effort"):
        assert f'<label for="{field}"' in body


def test_scrollable_regions_are_keyboard_reachable():
    body = app_js()
    # A capped, scrolling block is unreachable by keyboard without tabindex.
    assert '<pre class="task-content" tabindex="0" aria-label=' in body
    assert 'id="log-output" class="log-output" tabindex="0"' in body
    # The log body is deliberately not a live region — the ARIA log role implies
    # one, which would narrate every streamed line. As with the EventSource check
    # above, the negative is on the literal attribute; app.js's comment explains
    # the decision without spelling it, so this stays a real guard.
    assert 'role="log"' not in body
    assert 'id="log-status" class="muted" role="status"' in body


def test_focus_is_visible_everywhere_from_one_rule():
    css = styles_css()
    # A single zero-specificity rule covers links, buttons, form controls and
    # programmatic targets, so no control can ship without an indicator.
    assert ":where(a, button, input, select, textarea, summary, [tabindex]):focus-visible" in css
    assert "outline: 2px solid var(--focus)" in css
    assert "outline-offset: 2px" in css
    # A card link fills its card, so its ring is drawn inside to avoid colliding
    # with the neighbouring card.
    assert ".card-link:focus-visible" in css
    assert "outline-offset: -2px" in css
    # The utilities the semantics rely on.
    assert ".visually-hidden" in css
    assert ".skip-link" in css
    assert ".skip-link:focus" in css
    assert ".page-title" in css


def test_contrast_tokens_cover_filled_controls_in_both_themes():
    css = styles_css()
    # Text on an --accent fill needs its own token: dark mode's accent is light
    # enough that white-on-accent failed AA.
    assert "--accent-text: #ffffff" in css
    assert "--accent-text: #0f1115" in css
    assert "color: #fff" not in css  # no hard-coded white left on a themed fill
    # Destructive fills need one colour that carries its text in BOTH themes;
    # --bad-fg is a light pink in dark mode and was unusable as a button fill.
    assert "--danger: #dc2626" in css
    assert "background: var(--danger)" in css
    assert "background: var(--bad-fg)" not in css
    # Muted body text was just under 4.5:1 on the light background.
    assert "--muted: #5c6370" in css
    assert "--focus:" in css


def test_interactive_controls_meet_a_minimum_target_size():
    css = styles_css()
    # Every compact control gets a floor; the tabbar/cards are already large.
    assert css.count("min-height: 40px") >= 4  # primary + secondary + group head
    assert css.count("min-height: 36px") >= 2  # icon + shared secondary buttons
    assert "min-height: 32px" in css  # inline back/text links
    # The only animated affordance honours a reduced-motion preference.
    assert "prefers-reduced-motion: reduce" in css


# ---- adaptive dashboard layout (2026-07-21-adaptive-dashboard-layout) ---------
#
# The desktop shell anchors the nav rail to the left viewport edge and lets each
# view own its content width via a view-kind class app.js tags on #view: overview
# and the flat card lists fill the workspace (their grids add balanced columns),
# detail/form views keep a bounded reading column, logs and run history get a
# wider column — all left-anchored after the rail, so there is no centred outer
# gutter. Touch/programmatic navigation no longer paints a focus ring on the page
# title, while keyboard navigation still shows one. Asserted at the served-asset
# boundary (no JS runtime in this suite); the per-route view-kind tagging and the
# focus-move behaviour are driven for real under the jsc harness
# (tests/test_web_keyboard.py). Each check maps to a scope/acceptance line.


def test_desktop_rail_is_flush_left_and_workspace_fills_remaining():
    css = styles_css()
    assert "min-width: 1024px" in css
    desktop = css[css.index("min-width: 1024px") :]
    # The rail is anchored to the left viewport edge (left: 0), sidebar-wide, with
    # a right border — not the old centred-block offset.
    assert "left: 0;" in desktop
    assert "width: var(--sidebar-w)" in desktop
    assert "border-right: 1px solid var(--border)" in desktop
    # The centred-block machinery that produced the artificial outer gutter is gone.
    assert "--app-max" not in css
    assert "margin-left: max(" not in css
    # The workspace clears the rail and fills the remaining width by default
    # (max-width: none), left-anchored (margin-right: 0), so a capped view leaves
    # trailing space rather than a centred gutter.
    assert "margin-left: var(--sidebar-w)" in desktop
    assert "margin-right: 0;" in desktop


def test_desktop_views_have_page_appropriate_width_caps():
    css = styles_css()
    body = app_js()
    # Per-view caps: a bounded reading column for detail/form, a wider column for
    # logs/history, and an overview cap that keeps its fixed tile/panel set
    # balanced instead of stretching ultra-wide. Flat list/overview grids fill.
    for cap in ("--read-max", "--overview-max", "--wide-max"):
        assert cap in css
    for rule in (".view--read", ".view--overview", ".view--wide"):
        assert rule in css
    # app.js tags #view with the view kind per route (derived from the path) in
    # one place, wired into the router before the handler paints.
    assert "function viewKind" in body
    assert "function setViewKind" in body
    assert "setViewKind(path);" in body
    assert 'view().className = "view view--"' in body
    for kind in ('"read"', '"wide"', '"overview"', '"list"'):
        assert kind in body


def test_chronological_feeds_stay_single_column():
    css = styles_css()
    # The multi-column card grid is scoped to the *direct* children of #view (the
    # flat collections: repos, all-tasks, repo-detail tasks) via the child
    # combinator. Chronological feeds (recent commits, session strips, overview
    # activity/outcomes, run-history run cards) are nested deeper, so they keep the
    # base single-column flex flow and newest-to-oldest stays unambiguous.
    assert "#view > .list {" in css
    assert "repeat(auto-fill, minmax(320px, 1fr))" in css
    assert "flex-direction: column" in css  # the base list flow + commits feed


def test_page_title_focus_ring_is_gated_by_input_modality():
    css = styles_css()
    body = app_js()
    # The heuristic :focus-visible ring is suppressed on the two programmatic
    # focus targets (the routed heading and the main region), then re-enabled on
    # the heading only when app.js marks a keyboard-driven navigation.
    assert ".page-title:focus-visible" in css
    assert "outline: none" in css
    assert ".page-title.nav-focus-ring:focus" in css
    # app.js tracks the last input modality and toggles that class on the heading
    # it focuses, so a touch/mouse navigation lands without a ring.
    assert "navByKeyboard" in body
    assert 'classList.toggle("nav-focus-ring", navByKeyboard)' in body
    assert '"keydown"' in body
    assert '"pointerdown"' in body
    # The general focus-visible rule (real controls + tabindex=0 scroll regions)
    # is untouched, so keyboard focus stays visible everywhere else.
    assert (
        ":where(a, button, input, select, textarea, summary, [tabindex]):focus-visible"
        in css
    )


def test_mobile_and_tablet_shell_is_preserved():
    css = styles_css()
    # The phone base (bottom tab bar + safe-area insets, usable at 320px) and the
    # tablet tier (bottom bar kept, flat lists two-up) are untouched by the desktop
    # refinement.
    assert "env(safe-area-inset-bottom)" in css
    assert "env(safe-area-inset-top)" in css
    assert "min-width: 720px" in css
    tablet = css[css.index("min-width: 720px") : css.index("min-width: 1024px")]
    assert "#view > .list" in tablet  # tablet keeps the same direct-child card grid


# ---- appearance: mode + palette (2026-07-21-dashboard-color-palettes) ---------
#
# Appearance is two INDEPENDENT axes behind one compact header popover: a
# Dark / Light / System *mode* (data-theme) and a Neon Cyan / Wasteland Orange
# *palette* (data-palette). Dark + Neon Cyan is the first-visit default. Both
# persist under their own localStorage keys and resolve to <html> attributes
# before the first paint (an inline script in index.html), so neither flashes;
# app.js owns the live controls, mode's system-change following, and the
# browser-chrome meta. styles.css keys mode off `data-theme` (dark the bare-:root
# default, light an override) and layers the palette off `data-palette` as a token
# preset (Neon Cyan = the untouched default sets, Wasteland Orange = overrides).
# The two selects live in a disclosure popover so both fit a 320px header without
# overflow, even post-auth with sign-out visible. Asserted at the served-asset
# boundary (no JS runtime in this suite); the control's keyboard behaviour is
# driven for real under the jsc harness (tests/test_web_keyboard.py, `theme:` and
# `palette:` scenarios). Each check maps to a scope/acceptance line.


def test_appearance_control_is_a_header_popover_with_both_axes():
    html = index_html()
    # A compact, accessible Appearance control in the shell header — present on
    # every view including the login gate, because the header sits outside the
    # auth-gated #view. One toggle button discloses a panel holding both selects, so
    # two axes never overflow the mobile header.
    assert 'class="header-actions"' in html
    assert 'class="appearance"' in html
    assert 'id="appearance-toggle"' in html
    assert 'aria-expanded="false"' in html
    assert 'aria-controls="appearance-panel"' in html
    assert 'aria-label="Appearance"' in html
    assert 'id="appearance-panel"' in html
    # The mode axis: Dark / Light / System, named for assistive tech.
    assert '<select id="theme-select" class="theme-select" aria-label="Mode">' in html
    for value, label in (("dark", "Dark"), ("light", "Light"), ("system", "System")):
        assert f'<option value="{value}">{label}</option>' in html
    # The palette axis: Neon Cyan / Wasteland Orange.
    assert 'id="palette-select"' in html
    assert 'class="palette-select"' in html
    assert 'aria-label="Palette"' in html
    for value, label in (
        ("neon-cyan", "Neon Cyan"),
        ("wasteland-orange", "Wasteland Orange"),
    ):
        assert f'<option value="{value}">{label}</option>' in html
    # The control precedes #view, so it renders before authentication too.
    assert html.index('id="appearance-toggle"') < html.index('id="view"')


def test_theme_is_applied_before_first_paint_defaulting_to_dark():
    html = index_html()
    # An inline head script resolves the saved preference and sets data-theme on
    # <html> BEFORE the stylesheet paints a frame, so a dark-default visit never
    # flashes light. It reads the same key app.js uses and must precede the
    # stylesheet link so the first computed style already sees the resolved theme.
    assert 'localStorage.getItem("orch-hub-theme")' in html
    assert "document.documentElement.setAttribute(" in html
    assert '"data-theme"' in html
    assert html.index('localStorage.getItem("orch-hub-theme")') < html.index(
        'href="/styles.css"'
    )
    # It fails safe to dark when storage is blocked or the stored value is invalid.
    catch = html[html.index("catch (e)") :]
    assert '"dark"' in catch[:140]


def test_theme_defaults_to_dark_and_persists_the_choice():
    body = app_js()
    # One storage key, three choices; the saved preference persists and restores.
    assert 'const THEME_KEY = "orch-hub-theme";' in body
    assert 'new Set(["dark", "light", "system"])' in body
    # A missing / invalid stored value resolves to Dark; a storage error also fails
    # safe to Dark rather than throwing.
    assert 'return THEME_CHOICES.has(stored) ? stored : "dark";' in body
    assert 'return "dark"; // storage unavailable' in body
    # The mode control persists the chosen preference and re-applies the theme.
    assert "function setThemePreference" in body
    assert 'themeSelect.addEventListener("change"' in body
    # applyTheme drives <html data-theme> from the resolved preference, and the
    # bootstrap wires the Appearance control once at startup.
    assert 'root.setAttribute("data-theme", resolveTheme(getThemePreference()))' in body
    assert "initAppearance();" in body


def test_theme_system_choice_follows_the_os_and_updates_live():
    body = app_js()
    # System resolves from the current OS scheme...
    assert 'if (pref === "system") return systemPrefersDark() ? "dark" : "light";' in body
    assert 'window.matchMedia("(prefers-color-scheme: dark)")' in body
    # ...and a live OS-scheme change re-applies only while System is the active
    # preference (an explicit Dark/Light choice is left untouched).
    assert 'if (getThemePreference() === "system") applyTheme();' in body
    assert 'mq.addEventListener("change", follow)' in body


def test_theme_tokens_key_off_data_theme_with_a_dark_default():
    css = styles_css()
    # Dark is the bare-:root default (and safe fallback); Light is the higher-
    # specificity override applied when data-theme="light" is resolved. The
    # OS-driven media scheme block is gone — JS owns resolution now.
    assert ':root[data-theme="light"]' in css
    assert "prefers-color-scheme" not in css
    # Both full token sets survive the move: dark now in :root, light in the
    # attribute block (spot-check the background token of each).
    assert "--bg: #090c11" in css  # dark default
    assert "--bg: #e6eaf0" in css  # light override


def test_theme_keeps_color_scheme_and_browser_chrome_consistent():
    html = index_html()
    body = app_js()
    css = styles_css()
    # The document color-scheme is set per resolved theme, so native controls and
    # scrollbars follow: dark by default, light under the light attribute.
    assert "color-scheme: dark;" in css
    assert "color-scheme: light;" in css
    # A theme-color meta tints the mobile browser chrome; app.js keeps it in step
    # with the resolved theme's surface colour, read from the live CSS token.
    assert '<meta name="theme-color"' in html
    assert 'meta[name="theme-color"]' in body
    assert '.getPropertyValue("--surface")' in body
    # The pre-CSS canvas is biased to the dark default.
    assert '<meta name="color-scheme" content="dark light"' in html


def test_appearance_control_is_touch_sized_and_grouped():
    css = styles_css()
    # The compact controls meet the touch-target floor and group with sign-out at
    # the header edge. The icon-sized toggle keeps the header narrow; the disclosed
    # mode + palette selects share the same floor.
    assert ".header-actions" in css
    assert ".appearance-toggle" in css
    assert ".theme-select" in css
    assert ".palette-select" in css
    toggle = css[css.index(".appearance-toggle") :]
    assert "min-height: 36px" in toggle[:220]
    selects = css[css.index(".theme-select,") :]
    assert "min-height: 36px" in selects[:240]
    # The panel is a floating disclosure so the two axes never widen the header.
    assert ".appearance-panel" in css
    assert "position: absolute" in css[css.index(".appearance-panel") :][:220]


def test_palette_is_applied_before_first_paint_defaulting_to_neon_cyan():
    html = index_html()
    # The same pre-paint script resolves the palette axis too, so a Wasteland Orange
    # visit never flashes the default palette. It reads its own key and must precede
    # the stylesheet link so the first computed style already sees the palette.
    assert 'localStorage.getItem("orch-hub-palette")' in html
    assert '"data-palette"' in html
    assert html.index('localStorage.getItem("orch-hub-palette")') < html.index(
        'href="/styles.css"'
    )
    # An unknown / missing stored value normalises to Neon Cyan, and a storage error
    # fails safe to Neon Cyan rather than throwing.
    assert 'if (palette !== "wasteland-orange") palette = "neon-cyan";' in html
    assert (
        'document.documentElement.setAttribute("data-palette", "neon-cyan");' in html
    )


def test_palette_defaults_to_neon_cyan_and_persists_independently():
    body = app_js()
    # A second axis with its OWN key, so it never collides with the mode key — that
    # separation is what lets an existing Dark/Light/System user migrate untouched.
    assert 'const THEME_KEY = "orch-hub-theme";' in body
    assert 'const PALETTE_KEY = "orch-hub-palette";' in body
    assert 'new Set(["neon-cyan", "wasteland-orange"])' in body
    # A missing / invalid stored value resolves to Neon Cyan; a storage error also
    # fails safe rather than throwing.
    assert 'return PALETTE_CHOICES.has(stored) ? stored : "neon-cyan";' in body
    assert 'return "neon-cyan"; // storage unavailable' in body
    # The palette control persists the chosen preference and applies it to
    # <html data-palette>; the bootstrap wires it once at startup.
    assert "function setPalettePreference" in body
    assert 'paletteSelect.addEventListener("change"' in body
    assert 'root.setAttribute("data-palette", getPalettePreference())' in body
    assert "applyPalette();" in body


def test_palette_is_a_token_preset_layered_over_mode():
    css = styles_css()
    # Neon Cyan is the DEFAULT and needs no block: it is the bare :root (dark) and
    # the light override, untouched — so there is deliberately no data-palette
    # selector for it, which keeps the default appearance byte-for-byte unchanged.
    assert 'data-palette="neon-cyan"' not in css
    assert "--accent: #26e4fb" in css  # the untouched Neon Cyan dark accent
    # Wasteland Orange is a token preset per mode, overriding the hue-carrying
    # tokens. Its light variant needs its own combined selector to win over both the
    # light-mode block and the dark-orange palette block (equal specificity).
    assert ':root[data-palette="wasteland-orange"]' in css
    assert ':root[data-theme="light"][data-palette="wasteland-orange"]' in css
    assert "--accent: #ff8a3d" in css  # dark ember
    assert "--accent: #9a4a17" in css  # light burnt-orange


def test_appearance_popover_is_toggled_and_dismissible():
    body = app_js()
    # The disclosure is built by hand so the toggle is a real focusable button:
    # aria-expanded tracks state and the panel's hidden flag mirrors it.
    assert "function initAppearancePopover" in body
    assert 'toggle.setAttribute("aria-expanded", open ? "true" : "false")' in body
    assert "panel.hidden = !open;" in body
    assert 'toggle.addEventListener("click", () => setOpen(!isOpen()))' in body
    # It dismisses on Escape (focus returns to the toggle) and on an outside click.
    assert 'if (ev.key === "Escape" && isOpen())' in body
    assert "toggle.focus()" in body
    assert "!root.contains(ev.target)" in body


# ---- task evaluation (deterministic Layer 1 sidecar view) -------------------
#
# The evaluation view has a JS runtime behaviour test in the keyboard harness
# (tests/test_web_keyboard.py, scenario `eval:`) that drives the real render
# against stubbed endpoints — that is where "the metrics actually paint" and "the
# disabled branch replaces the controls" are proven. These served-asset checks
# pin the wiring at the shell boundary: each maps to a scope/acceptance line of
# the dashboard-evaluation task, so a regression that drops a route, a metric, an
# entry point, or the disabled handling is caught here even without a JS engine.


def test_evaluation_route_and_hash_are_wired():
    body = app_js()
    # A dedicated /evaluations/{repo}/{task}[/{id}] route → renderTaskEval, an
    # encoded hash builder, its own bounded-width view kind, and the Tasks tab.
    assert 'seg[0] === "evaluations" && seg.length >= 3' in body
    assert "renderTaskEval(seg[1], seg[2], seg[3])" in body
    assert "const evalHash = (repo, taskId) =>" in body
    assert '"#/evaluations/" + encodeURIComponent(repo)' in body
    assert 'if (seg[0] === "evaluations") return "read";' in body
    assert 'else if (path.startsWith("/evaluations")) tab = "/tasks";' in body


def test_evaluation_view_reads_availability_then_list_then_detail():
    body = app_js()
    assert "async function renderTaskEval(repoName, taskId, evaluationId)" in body
    # Availability first (the one route that answers while disabled), then the
    # (repo, task) list, then the selected/latest evaluation's artifacts.
    assert 'const avail = await api("/api/evaluation");' in body
    assert "const evalListPath = (repo, taskId) =>" in body
    assert "const evalDetailPath = (repo, taskId, id) =>" in body
    assert "list.latest_evaluation_id" in body
    assert "await api(evalDetailPath(repoName, taskId, selectedId))" in body


def test_evaluation_disabled_mode_is_explained_not_probed():
    body = app_js()
    # Disabled → a concise explanation, not a probe of an action route that 404s.
    assert "if (!avail || !avail.enabled)" in body
    assert "Task evaluation is disabled on this hub" in body
    assert "ORCH_HUB_EVAL_ENABLED is not set" in body


def test_evaluation_view_renders_layer1_metrics():
    body = app_js()
    # The five score axes with a banded meter, the run/session + protocol rollups
    # the scope names, and the pack-level evidence warnings.
    assert "const SCORE_LABELS = {" in body
    assert "function scoreMeter(score)" in body
    assert 'class="eval-score"' in body
    for label in ("related runs", "failed without diagnostic", "plan gates",
                  "dev / review sessions", "peak context"):
        assert label in body
    assert "function evalProtocolSection(p)" in body
    for label in ("pass / changes-requested", "remediations", "log entries"):
        assert label in body
    assert "unreviewed work entries: " in body
    assert "illegal transitions: " in body
    assert "Evidence warnings" in body


def test_evaluation_view_distinguishes_static_missing_and_semantic():
    body = app_js()
    # Static-deterministic vs future-semantic, and the three distinct empty
    # states (none yet / evidence-only / disabled) kept apart.
    assert "Deterministic static metrics (Layer 1)." in body
    assert "Semantic" in body and "is not yet available" in body
    assert "No evaluation has been generated for this task yet." in body
    assert "This evaluation has an evidence pack but no computed" in body


def test_evaluation_generate_posts_deterministic_refresh():
    body = app_js()
    # Generate → POST /metrics (writes hub-owned artifacts only), then re-render
    # to the fresh latest under the route guard captured before the POST.
    assert "const evalMetricsPath = (repo, taskId) =>" in body
    assert 'evalListPath(repo, taskId) + "/metrics"' in body
    assert "async function onEvalGenerate(repo, taskId, button)" in body
    assert "await api(evalMetricsPath(repo, taskId), { method: \"POST\" });" in body
    assert "rerender(renderTaskEval(repo, taskId, null))" in body


def test_evaluation_offers_copy_download_and_never_inlines_raw_logs():
    body = app_js()
    # Local-inspection affordances: a Copy button (Clipboard API) and a
    # declarative data: URI download — the only place the raw evidence is offered.
    assert 'class="eval-copy"' in body
    assert 'class="eval-dl text-link"' in body
    assert '"data:application/json;charset=utf-8," + encodeURIComponent(json)' in body
    assert "nav.clipboard.writeText(text)" in body
    # The evidence pack is offered as an artifact, not rendered inline, so bounded
    # log tails stay out of the UI.
    assert "run logs are not shown in the dashboard" in body


def test_evaluation_history_picker_navigates_to_the_id_route():
    body = app_js()
    assert "function evalPickerSection(repo, taskId, evals, selectedId)" in body
    assert 'const pick = document.getElementById("eval-pick");' in body
    assert 'evalHash(repo, taskId) + "/" + encodeURIComponent(ev.target.value)' in body


def test_task_detail_links_to_evaluation_only_when_enabled():
    body = app_js()
    # The entry point is gated on a best-effort availability probe, so a disabled
    # hub shows no dead control; the probe never blocks the task view.
    assert "let evalEnabled = false;" in body
    assert 'const avail = await api("/api/evaluation");' in body
    assert 'class="eval-entry"' in body
    assert "Task evaluation " in body


def test_run_history_links_to_evaluation_only_when_enabled():
    body = app_js()
    # Run-history task groups (where archived/completed tasks appear) get a
    # per-task Evaluation link, gated on the same availability probe, sitting
    # outside the collapse <button> so it is a real navigable link.
    assert "let historyEvalEnabled = false;" in body
    assert "historyEvalEnabled && group.repo_name && group.task_id" in body
    assert 'class="text-link tg-eval"' in body
    assert "Evaluation " in body


def test_evaluation_styles_are_present():
    css = styles_css()
    # Score meters/rows, the banded value colours, the primary Generate button,
    # the outlined Copy control, warning lists, and the two entry-link anchors.
    for selector in (
        ".score-meter",
        ".score-fill",
        ".eval-score",
        ".es-score.good",
        ".es-score.mid",
        ".es-score.low",
        ".es-score.na",
        ".score-meter.low .score-fill",
        ".eval-gen-btn",
        ".eval-copy",
        ".eval-warnings",
        ".eval-flags",
        ".eval-entry",
        ".tg-actions",
    ):
        assert selector in css, selector
