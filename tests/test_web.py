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
FAKE_CONFIG = Config(mandrel_path=Path("/fake/mandrel"), token=TOKEN)
NO_TOKEN_CONFIG = Config(mandrel_path=Path("/fake/mandrel"))
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


# ---- orchestrator-backed launch options (split DEV/REVIEW dialog) -----------
#
# The Start dialog does not hardcode a cc-codex backend or client-built override
# flags. It queries the repo's own deployed orchestrator
# (GET /api/repos/{repo}/orchestrator-config) and POSTs a structured `launch`
# selection; the server turns that into argv through build_launch_args. DEV and
# REVIEW select their profile independently over one shared backend. The wiring
# is asserted at the served-asset boundary (no JS runtime here); the
# JavaScriptCore keyboard harness covers the behaviour.


def test_launch_dialog_offers_independent_role_profile_selectors():
    # One selector per role over exactly Default / Standard / Excellent / Custom;
    # the former single combined Mode select is gone.
    body = app_js()
    assert 'class="opt-mode"' not in body
    assert 'class="opt-role-mode"' in body
    assert 'roleModeField("dev", "DEV")' in body
    assert 'roleModeField("review", "REVIEW")' in body
    assert (
        'const LAUNCH_MODES = ["default", "standard", "excellent", "custom"];' in body
    )


def test_launch_dialog_exposes_a_backend_selector():
    # Unlike session-1, the dashboard now lets the operator pick the backend: an
    # empty-value Inherit option (the orchestrator's configured backend) plus the
    # two explicit backends. So `cursor` now legitimately appears in the shell.
    body = app_js()
    assert 'class="opt-backend"' in body
    assert '<option value="">Inherit' in body
    assert '<option value="cursor">' in body
    assert '<option value="cc-codex">' in body


def test_launch_dialog_reads_values_from_the_orchestrator():
    # Displayed model/effort come from the orchestrator query, never hardcoded
    # model ids: the dialog fetches the protected config route and repaints on a
    # role/backend change, asking for both role profiles in ONE query so the
    # preview is coherent. A query failure fails closed — Launch disabled, the
    # orchestrator's own error shown — instead of falling back to a hub default.
    body = app_js()
    assert "orchestrator-config" in body
    assert "refreshLaunchConfig" in body
    assert "setLaunchEnabled(false)" in body
    assert 'qs.push("dev_profile=" + encodeURIComponent(devProfile));' in body
    assert 'qs.push("review_profile=" + encodeURIComponent(reviewProfile));' in body
    # The old client-side extra-args builder and the pinned backend list are gone.
    assert "buildExtraArgs" not in body
    assert "RUN_OPTION_FIELDS" not in body


def test_launch_dialog_gates_on_split_profile_capability():
    # A deployment that does not report the orchestrator's per-role `profiles`
    # cannot take --dev-profile/--review-profile at all. The dialog reads that
    # capability off the query (an explicit true — never a value the server
    # derived from the run-wide profile) and otherwise closes split selection
    # with redeploy guidance instead of attempting unsupported flags.
    body = app_js()
    assert "config.split_profiles === true" in body
    assert "st.split === false" in body
    assert "redeploy" in body


def test_launch_posts_the_structured_launch_selection():
    # Start POSTs a structured `launch` object carrying the independent
    # `dev_mode`/`review_mode` (never the legacy run-wide `mode`, which stays for
    # pre-split API callers); the server builds the argv. No client `extra_args`.
    body = app_js()
    assert "buildLaunchPayload" in body
    assert "launch: launch," in body
    assert "const launch = { dev_mode: st.dev, review_mode: st.review };" in body


def test_custom_role_exposes_only_its_own_editable_fields():
    # A role becomes editable only when ITS selector says Custom; the other role
    # stays a read-only preset in the same panel. The agent select is cc-codex-only
    # (gated on the backend inside roleRow) and offers no inherit choice, because
    # a split-mode custom cc-codex role must send its complete agent/model/effort
    # tuple — both roles alike, since each names its own CLI agent.
    body = app_js()
    assert 'backend === "cc-codex"' in body
    assert 'choices("agent", CLI_AGENTS, values.agent' in body
    assert 'st.dev === "custom"' in body
    assert 'st.review === "custom"' in body
    assert '<option value="">inherit</option>' not in body
    # The role fields are templated per role and per field, so the ids and their
    # styling hooks (.role-model / .role-effort) come from one `opt-${key}-${suffix}`
    # template rather than a literal block per role.
    assert 'id="opt-${key}-${suffix}"' in body
    assert 'class="opt-${key}-${suffix} role-${suffix} role-edit"' in body
    # Custom edits are local state: kept in the dialog state so an unrelated
    # re-query's repaint cannot wipe them, and never a reason to re-query.
    assert "onCustomFieldInput" in body
    assert "customRoleValues" in body


def test_custom_role_options_come_from_the_published_catalog():
    # Model and effort choices are read out of the orchestrator's own
    # `options.agents[backend][role][agent]`, keyed by all three because the
    # effort vocabularies differ per agent. Nothing in the shell names a model id
    # or an effort word of its own.
    body = app_js()
    assert "cfg.options && cfg.options.agents" in body
    assert "function catalogAgentNames(backend, key)" in body
    assert "function catalogEfforts(entry, model)" in body
    # The per-model list wins over the agent's, so a backend that fixes no axis
    # per agent (Cursor takes either model family) still offers the right words.
    assert "const list = found ? found.efforts : entry.efforts;" in body
    # No hub-owned model or effort vocabulary anywhere in the shell.
    for invented in ("claude-opus", "gpt-5", "xhigh", "minimal"):
        assert invented not in body


def test_custom_role_agent_change_refilters_and_strands_nothing():
    # Switching a role's agent re-filters that role's own options, and a value the
    # new agent does not offer is dropped rather than carried into the launch —
    # the orchestrator would refuse it at startup, and submitting it would mean
    # showing one launch and posting another.
    body = app_js()
    assert "function onCustomAgentChange(ev)" in body
    assert "function pruneRoleEdits(key, role)" in body
    assert 'if (models.length && !listed && !values.other) edits.model = "";' in body
    assert 'edits.effort = "";' in body
    assert 'if (st.dev === "custom") pruneRoleEdits("dev", cfg.dev);' in body
    assert 'if (st.review === "custom") pruneRoleEdits("review", cfg.review);' in body
    # A model, an effort, and the escape-hatch flag belong to the agent they were
    # chosen under, so an actual agent change drops them outright. This is the
    # only guard where the catalog cannot help: an explicit edit under an agent
    # the catalog omits — or under no catalog at all — is unjudgeable, and
    # carrying it into the launch is exactly the startup refusal being prevented.
    assert "if (edits.agentFor !== values.agent) {" in body
    assert "delete edits.model;" in body
    assert "delete edits.effort;" in body
    assert "delete edits.other;" in body
    assert "edits.agentFor = values.agent;" in body


def test_a_payload_without_a_catalog_keeps_free_text_with_a_reason():
    # The hub renders what the payload supplies and nothing else: no catalog means
    # today's free-text inputs plus a visible reason, never a substituted list.
    body = app_js()
    assert "function catalogNote(key, values, entry)" in body
    assert "publishes no launch option catalog" in body
    assert "The catalog lists no models for " in body
    assert 'class="muted role-note" id="opt-${key}-note"' in body
    # The reason is the controls' accessible description too, not decoration a
    # screen reader would never reach.
    assert '`aria-describedby="opt-${key}-note"`' in body or (
        ' aria-describedby="opt-${key}-note"' in body
    )
    # The free-text control is the same templated input as before the catalog.
    assert 'field("model", "model", values.model, "model id")' in body


def test_a_model_outside_the_catalog_stays_launchable():
    # The CLI deliberately still launches an uncatalogued model, so the select
    # keeps an explicit escape hatch that reveals a free-text box. `Other` is a UI
    # choice and never a submitted value.
    body = app_js()
    assert 'const OTHER_MODEL = "__other__";' in body
    assert "if (el.value === OTHER_MODEL) edits.other = true;" in body
    assert '"model (outside the catalog)"' in body or "model outside the catalog" in body
    assert "Other (not in the catalog)" in body


def test_launch_sends_each_custom_cc_codex_role_its_own_agent():
    # Both roles now name their own CLI agent, so both send the complete tuple;
    # a backend without an agent axis sends neither.
    body = app_js()
    assert 'launch[key + "_agent"] = agent;' in body
    assert 'if (launch.backend === "cc-codex")' in body
    assert 'launch[key + "_model"] = model;' in body


def test_plan_gate_toggle_stays_available_and_orthogonal():
    # Plan-gate is its own checkbox, independent of mode/profile, and rides the
    # structured payload as `plan_gate: true` — no client-built `--plan-gate`.
    body = app_js()
    assert "opt-plan-gate" in body
    assert "launch.plan_gate = true;" in body


def test_standard_excellent_gate_on_available_profiles():
    # Standard/Excellent are disabled with a visible reason when the orchestrator
    # does not offer them, per role; selection never silently falls back to
    # Default.
    body = app_js()
    assert "available_profiles" in body
    assert "syncModeAvailability" in body
    assert "(unavailable)" in body


def test_run_history_renders_the_effective_config_snapshot():
    # Run detail/history retains the authoritative effective-config: snapshot
    # (per-role profiles, backend, resolved models/efforts, config/effective
    # revisions, sources) from the embedded per-run history payload.
    body = app_js()
    assert "effectiveConfigBlock" in body
    assert "run.effective_config" in body
    assert "config rev" in body
    assert "effective rev" in body


def test_run_history_summarizes_both_role_profiles():
    # `profiles.dev`/`profiles.review` are authoritative; an older snapshot
    # without them derives both roles from the run-wide `profile`. A Custom role
    # reports profile "default", so only `sources` provenance distinguishes it —
    # without that check a Custom run would render as Default.
    body = app_js()
    assert "roleProfileLabels" in body
    assert 'sources[role + ".model"] === "cli"' in body
    assert "cfg.profiles" in body


def test_launch_dialog_submit_sends_launch_and_keeps_ack():
    # Start still carries acknowledge_blocked (blocked-task launch) and posts to
    # the same /api/runs create route, now with the structured `launch` selection
    # (server-built argv) rather than a client-assembled extra_args list.
    body = app_js()
    assert "acknowledge_blocked" in body
    assert '"/api/runs"' in body
    assert "launch: launch," in body


def test_the_layer_2_opt_out_is_a_body_field_not_a_launch_argument():
    # The boundary this test exists for: the toggle must reach the hub as a
    # start-request field and must never join the `launch` selection, which the
    # server turns into orchestrator argv — an unknown flag aborts every launch.
    body = app_js()
    assert "opt-auto-eval" in body
    assert "auto_semantic_eval: autoSemanticEval," in body
    assert "launch.auto_semantic_eval" not in body


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


def test_log_view_task_link_splits_live_detail_from_archived_fallback():
    body = app_js()
    # The log view's task back-link is liveness-aware, resolved through the one
    # shared run destination rule (runTaskDestination): a live (active/waiting)
    # run with repo+task attribution opens the full task detail, so run controls,
    # pending answers, and session/context strips are reachable; an ended run, or
    # a live run missing attribution, keeps the run-scoped task view, which
    # resolves active-then-archive so an archived task stays readable instead of
    # 404-ing on the index-gated detail. Only the no-task-id fallback is the log
    # view's own — the logs are already on screen, so it offers the Runs list.
    assert "const taskDest = runTaskDestination(run);" in body
    assert '"#/runs">← Runs' in body


# ---- task-first run navigation (2026-07-31-task-detail-first-run-navigation) --
#
# Selecting a run from a run surface (Overview current activity, Run history)
# opens the run's TASK rather than its log tail: a run is a dev/review loop over
# one task file, so the task is the unit of work, and both task destinations link
# onward to the logs. One shared rule serves every run surface, so the liveness
# split is stated once. Asserted at the served-asset boundary (no JS runtime in
# this suite); the destinations themselves are driven end-to-end in the jsc
# harness (`run nav:` checks in tests/keyboard_harness.js).


def test_run_destination_rule_is_shared_and_liveness_aware():
    body = app_js()
    # One helper owns the rule for every run surface...
    assert "function runTaskDestination(run) {" in body
    # ...splitting on liveness plus attribution: a live run with a repo goes to
    # the index-gated active task detail, anything else to the run-scoped
    # active-then-archive view...
    assert "if (LIVE_STATES.has(run.state) && run.repo_name)" in body
    assert "return taskHash(run.repo_name, run.task_id);" in body
    assert "return runTaskHash(run.run_id);" in body
    # ...and a run with no task id has no task destination at all, leaving the
    # fallback to each caller rather than inventing a broken link.
    assert "if (!run || !run.task_id) return null;" in body


def test_run_surfaces_open_the_task_and_fall_back_to_logs():
    body = app_js()
    # Both run-card surfaces — the Overview's current activity and Run history —
    # resolve the same destination and fall back to the run's logs only when the
    # run carries no task attribution.
    assert body.count("runTaskDestination(run) || logsHash(run.run_id)") == 2
    for func in ("function activityCard(run) {", "function historyRunCard(run) {"):
        assert func in body
        section = body[body.index(func) :]
        assert "runTaskDestination(run) || logsHash(run.run_id)" in section.split(
            "\n  function "
        )[0]


def test_recent_outcomes_and_attention_keep_their_destinations():
    body = app_js()
    # Out of scope and unchanged: a recent outcome still deep-links to its log
    # tail, and a waiting attention row still opens the task detail whose answer
    # form resolves it.
    assert "function recentOutcomeCard(run) {" in body
    outcomes = body[body.index("function recentOutcomeCard(run) {") :].split(
        "\n  function "
    )[0]
    assert 'href="${logsHash(run.run_id)}"' in outcomes
    assert "? taskHash(w.repo_name, w.task_id)" in body
    assert ": logsHash(w.run_id)" in body


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


# ---- repo admission: run ⟷ intake exclusion (2026-07-24, session-2) ----------
#
# The backend admits either a live run or a non-terminal remote intake per repo
# (orch_hub.admission) and refuses the loser with a 409 carrying a stable
# `conflict` discriminator plus the blocking id. These pin the dashboard's half
# at the served-asset boundary: the wire contract it consumes, and the fact that
# the conflict is presented as a claim rather than folded into readiness. The
# rendered behaviour in each direction (which control disappears, where its link
# points, what a raced 409 repaints into) is driven for real in the JavaScriptCore
# harness — see scenarioRepoAdmission in tests/keyboard_harness.js.


def test_dashboard_consumes_the_admission_conflict_discriminator():
    body = app_js()
    # A refused start is recognized by the structured discriminator, not by
    # parsing the human message, and both directions are handled.
    assert "isAdmissionConflict" in body
    assert '"run_live"' in body
    assert '"intake_in_flight"' in body


def test_admission_notes_link_to_the_blocking_run_or_intake():
    body = app_js()
    # Each direction explains the claim and links to the thing that settles it:
    # the run's task detail (where Stop lives) and the intake's review view.
    assert "runHoldsRepoNote" in body
    assert "intakeHoldsRepoNote" in body
    assert "a run and a task intake can't share a repo" in body
    assert "repoInflightIntake" in body  # the intake-side occupancy probe
    assert "INTAKE_TERMINAL.has(i.status)" in body  # terminal records never block


def test_admission_is_presented_before_readiness_not_as_readiness():
    body = app_js()
    # A live run dirties the tree, so the repo's raw readiness fails `git_clean`
    # for the run's whole duration. The intake block must name the claim, never
    # that failure — so the live-run branch precedes the readiness branch and the
    # shared display-readiness rule is left alone.
    intake_section = body[body.index("function intakeSection") :]
    intake_section = intake_section[: intake_section.index("function wireIntakeStart")]
    assert intake_section.index("if (liveRun)") < intake_section.index(
        "readiness.ready"
    )
    # The one display-readiness rule keeps its inputs: raw readiness + a live run.
    assert "function displayReady(readiness, hasLiveRun)" in body


def test_recovered_overlap_keeps_only_the_write_free_control():
    body = app_js()
    # A pre-existing overlap can't revise or confirm (both touch a tree the run
    # owns); Cancel writes nothing, so it stays — it is the only way to clear the
    # claim from the intake side.
    assert "intakeReviewControls(repoName, liveRun)" in body
    assert "this draft can't be revised or confirmed" in body
    assert "cancelling writes nothing to the repo" in body


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
    assert "expandedGroups" in body
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
    assert "No runs on this page match these filters." in body


# ---- run-history pagination + collapsed-by-default -----------------------------
# (2026-07-30-run-history-pagination-collapse)
#
# Run history pages the server side (50 runs per page) instead of showing one
# capped list, and task groups start collapsed. The behavioural drive — pressing
# Previous/Next, boundary disabling, expansion surviving a page trip, the
# out-of-range and empty states — is in the jsc harness
# (`scenarioRunHistoryPaging`); these are the served-asset guarantees.


def test_run_history_requests_a_page_by_cursor_and_defaults_to_the_newest():
    body = app_js()
    # A page is a server window, so navigating refetches — anchored on a cursor
    # the server handed back, never on a page number computed here, so a run
    # started or pruned mid-walk cannot shift the next window.
    assert '"/api/history/runs" +' in body
    assert '(cursor ? "?cursor=" + encodeURIComponent(cursor) : "")' in body
    # The router's no-argument call lands on the newest page (so re-entering the
    # view always shows the newest runs, with no stale position).
    assert "async function renderRuns(cursor)" in body


def test_run_history_pager_renders_from_server_metadata():
    body = app_js()
    css = styles_css()
    # Previous/Next + a position indicator, driven by the response's own
    # pagination fields rather than inferred from a short page. The button markup
    # itself is the shared `pagerButton` helper (the report feed pages with the
    # same control); only the direction sources and the status line are local.
    assert "const pagerButton = (id, label, arrow, enabled, leading) =>" in body
    assert 'pagerButton("rp-prev", "Previous"' in body
    assert 'pagerButton("rp-next", "Next"' in body
    assert 'wire("rp-prev"' in body and 'wire("rp-next"' in body
    assert "data.prev_cursor" in body
    assert "data.next_cursor" in body
    assert "data.total_pages" in body
    assert "data.total_runs" in body
    assert "Page ${page} of ${totalPages}" in body
    # A control is enabled exactly when it has somewhere to go, and the handler
    # re-checks that, so a boundary control is inert whatever route it came
    # through and can never offer a step it cannot take.
    assert 'enabled ? "" : " disabled"' in body
    assert "if (!cursor) return;" in body
    # Accessible names: decorative arrows, real per-button labels.
    assert '<span aria-hidden="true">${arrow}</span>' in body
    assert '<span class="visually-hidden"> page</span>' in body
    assert 'aria-label="Run history pages"' in body
    assert ".pager-btn:disabled" in css
    assert ".pager-status" in css


def test_run_history_counts_state_their_page_scope():
    body = app_js()
    css = styles_css()
    # Filters compose over the loaded page only, so nothing may read as a total
    # over all of history: the count, the no-match line, and a note by the
    # controls all name the scope.
    assert "on this page" in body
    assert "No runs on this page match these filters." in body
    assert "Filters apply to this page's runs." in body
    assert ".filters-scope" in css
    # The whole-history total lives on the pager, where it is true.
    assert "} total</span>" in body


def test_run_history_distinguishes_empty_history_from_a_vanished_page():
    body = app_js()
    # Empty history and a window whose runs are gone are different: the first has
    # no pager, the second keeps one so Previous can get back.
    assert "No runs recorded yet." in body
    assert "No runs on page ${Number(data.page) || 1}" in body
    assert "if (!runs.length && !totalRuns)" in body
    # Previous from a vanished page lands on the last page that still exists —
    # the server anchored that cursor when it found the window empty, so the
    # client does no clamping of its own.
    assert 'wire("rp-prev", data.prev_cursor);' in body


def test_run_history_task_groups_start_collapsed():
    body = app_js()
    # The tracked state is the user's explicit expansion; absent = collapsed, so
    # every newly encountered group (new page, new filter result) starts closed.
    assert "const expandedGroups = new Set();" in body
    assert "const collapsed = !expandedGroups.has(key);" in body
    assert "expandedGroups.delete(key);" in body
    assert "expandedGroups.add(key);" in body
    # Held in the view closure, so an expansion survives filter re-renders and a
    # page trip while the same group is still on screen.
    assert "collapsedGroups" not in body


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
    # The refresh is torn down on navigation, right beside the intake and
    # semantic-job polls.
    assert (
        "stopIntakePoll();\n    stopSemanticJobPoll();\n    stopOverviewRefresh();"
        in body
    )
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


# ---- subscription usage on overview -------------------------------------------
#      (2026-07-24-agent-subscription-usage-overview, session-2)
#
# The Overview gains an independent, display-only "Subscription usage" section:
# separate Claude and Codex cards over GET /api/usage (a sixth best-effort
# source), each with per-window used/remaining meters, plan, reset time, and
# freshness, plus explicit unavailable/stale states. It is deliberately its own
# section, laid out below the attention/activity/outcomes grid, and never gates
# anything. Rendering/degradation/stale/independence behaviour is verified by the
# jsc keyboard harness (tests/keyboard_harness.js, scenarioSubscriptionUsage);
# these pin the served-asset wiring/boundary, like the overview checks above.


def test_overview_usage_is_a_sixth_best_effort_source():
    body = app_js()
    # /api/usage joins loadOverview's parallel best-effort sources; a failed fetch
    # normalizes to null (this section only), like the other five.
    assert '"/api/usage"' in body
    assert "subscriptionUsagePanel" in body
    assert "usageData ? usageData.providers" in body


def test_overview_usage_section_is_independent_and_display_only():
    body = app_js()
    # Its own section heading, painted OUTSIDE the overview-panels grid so quota
    # never lays out as an operational/health panel.
    assert "Subscription usage" in body
    assert "overview-usage" in body
    # Two independent provider cards; one unavailable never suppresses the other.
    assert "usageCard" in body
    assert "Claude" in body and "Codex" in body
    # A usage-source failure degrades only this section.
    assert "Subscription usage is unavailable." in body


def test_overview_usage_is_percent_first_and_explicit():
    body = app_js()
    # Percent-first: an exact count only when the provider gives a real numeric
    # limit, otherwise a percentage, and an explicit "usage unknown" instead of a
    # misleading 0% for a missing figure (unavailable-not-zero).
    assert "usageWindowReadout" in body
    assert "% used" in body
    assert "usage unknown" in body
    assert "w.limit != null && w.used != null" in body
    # Unavailable and stale are explicit states, not zero usage.
    assert "usageFreshness" in body
    assert "last observed " in body
    assert "usage-stale" in body


def test_overview_usage_states_a_rolled_over_window():
    body = app_js()
    # A window's reset instant is compared to the clock in exactly one place, so a
    # passed boundary reads as "no window is open" (the 5h window is anchored to the
    # first call of the window) instead of keeping the full meter and degrading the
    # meta line to the old reset-is-due phrasing.
    assert "usageWindowPhase" in body
    assert "no active window" in body
    assert "starts at next call" in body
    assert "last observed " in body
    assert '"reset due"' not in body  # the string literal is gone, not just unused
    # The phase is a per-window question about the provider's observation time, so
    # the meter is built from both.
    assert "usageWindow(w, p.observed_ts)" in body


def test_overview_usage_never_exposes_reset_credit_identifiers():
    body = app_js()
    # Only the aggregate reset-credit fields the model already sanitized are
    # rendered — has_credits / unlimited / balance — never a reset-credit id.
    assert "usageCredits" in body
    assert "reset credits" in body
    assert "c.balance" in body


def test_overview_usage_styles():
    css = styles_css()
    assert ".usage-cards" in css
    assert ".usage-card" in css
    assert ".usage-bar" in css
    assert ".usage-bar-fill" in css
    # Neutral->warn capacity idiom (mirrors .ctx-meter), no red/danger alarm.
    assert ".usage-bar.high .usage-bar-fill" in css
    # An unknown window gets a distinct track, not a 0% fill.
    assert ".usage-bar.unknown" in css
    # A window whose figure is not the open window's reads as a muted state phrase.
    assert ".uw-rolled .uw-readout" in css
    assert ".uw-unobserved .uw-readout" in css
    # Two-up on a wider screen, like the metric row.
    assert "min-width: 520px" in css


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
    # The launch dialog's Backend select carries a visible label and the two role
    # selectors carry templated ones; the Custom role controls carry
    # visually-hidden-but-real labels, templated per role and field (the harness
    # verifies every dialog control resolves a label[for]).
    assert '<label for="opt-backend"' in body
    assert '`<label for="opt-${key}-mode">${name} profile`' in body
    assert 'for="opt-${key}-${suffix}">${name} ${text}</label>' in body


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
    assert "ORCH_HUB_EVAL_ENABLED is set to a disabled value" in body


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


def test_evaluation_view_distinguishes_static_and_semantic_layers():
    body = app_js()
    # The header note frames the two layers as one evidence judged two ways:
    # deterministic Layer 1 vs a model-judged Layer 2, so a reader can tell which
    # numbers are deterministic and which are a model's judgment.
    assert "Two layers over the same evidence" in body
    assert "static metrics (Layer 1)" in body
    assert "model-judged semantic evaluation (Layer 2)" in body
    assert "model's judgment, kept visually distinct" in body
    # The distinct empty/absent states are kept apart: no evaluation yet, an
    # evidence-only evaluation, and a metrics-only evaluation with no Layer 2 report.
    assert "No evaluation has been generated for this task yet." in body
    assert "This evaluation has an evidence pack but no computed" in body
    assert "This evaluation has deterministic metrics only." in body


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


# ---- semantic (Layer 2) dashboard, session-1 -------------------------------
#
# The async semantic-job routes + detail readback (`semantic_report`/`report_md`)
# and the `has_semantic_report` list flag are already API-tested (test_api.py);
# these served-asset checks pin the dashboard wiring the JSC harness then drives
# end-to-end (scenarioTaskEval): the trigger + its cost/read-only copy, the
# running/failed/completed job states, the model-judgment report render, and the
# static-vs-semantic distinction.


def test_evaluation_semantic_trigger_posts_the_async_job_with_cost_copy():
    body = app_js()
    # A distinct Layer 2 section carries the cost + read-only-analysis boundary and
    # a trigger that POSTs the async job route (not the retired synchronous
    # /semantic route), mirroring the metrics trigger's guarded re-render so the
    # transient job's running panel paints.
    assert "const evalSemanticJobPath = (repo, taskId) =>" in body
    assert 'evalListPath(repo, taskId) + "/semantic/job"' in body
    assert "function evalSemanticSection(doctor, job)" in body
    assert "eval-semantic-generate" in body
    assert 'class="eval-sem-btn"' in body
    assert 'class="eval-cost"' in body
    # The cost + not-an-orchestrator-run + no-mutation boundary is stated in copy.
    assert "consumes model usage (a real cost)" in body
    assert "read-only analysis" in body
    assert "does not start an orchestrator task run" in body
    assert "never modifies the" in body
    # Trigger handler: busy/in-progress note, POST the job, guarded re-render, error.
    assert "async function onEvalSemantic(repo, taskId, button)" in body
    assert 'await api(evalSemanticJobPath(repo, taskId), { method: "POST" });' in body
    assert "Running the semantic evaluator" in body
    assert "rerender(renderTaskEval(repo, taskId, null))" in body
    assert "controlErrorText(" in body
    assert '"Failed to start the semantic evaluation."' in body


# ---- semantic (Layer 2) async job states, session-2 ------------------------
#
# The async job routes (start/read) + persistence/dedup/failure/reconcile are
# API- and manager-tested (test_api.py, test_semantic_jobs.py); the JSC harness
# (scenarioTaskEval) drives the running/reload/completed/failed/retry states and
# the poll end-to-end. These served-asset checks pin the wiring those rest on.


def test_evaluation_reads_the_semantic_job_on_load():
    body = app_js()
    # renderTaskEval reads the current async job on every load and passes it to the
    # section, so a running or recently-failed job renders its state across reload,
    # navigation, and another browser session.
    assert "const jobRes = await api(evalSemanticJobPath(repoName, taskId));" in body
    assert "semanticJob = jobRes ? jobRes.job : null;" in body
    assert "evalSemanticSection(semanticDoctor, semanticJob)" in body
    # The transient set (the poll trigger) mirrors the backend lifecycle.
    assert 'const SEMANTIC_JOB_TRANSIENT = new Set(["queued", "running"]);' in body
    assert "SEMANTIC_JOB_TRANSIENT.has(semanticJob.status)" in body


def test_evaluation_semantic_running_state_replaces_the_trigger():
    body = app_js()
    # A queued/running job renders a running panel with NO trigger button, so a
    # duplicate model call cannot be started from the page (the backend also dedups).
    # The state modifier rides the single shared .eval-actions wrapper.
    assert "function evalSemRunning(job)" in body
    assert '<div class="eval-actions${modifier}">' in body
    assert 'modifier = " eval-sem-running";' in body
    assert "SEMANTIC_JOB_TRANSIENT.has(status)" in body
    assert "actions = evalSemRunning(job);" in body
    # The panel names the model and ticks an elapsed label in place while running.
    assert "function updateEvalSemElapsed(job)" in body
    assert 'id="eval-sem-elapsed"' in body


def test_evaluation_semantic_failed_state_shows_diagnostic_and_retry():
    body = app_js()
    # A failed/error job shows the bounded backend diagnostic and a Retry that
    # re-posts the job — never a silent return to the idle view. `error` (a corrupt
    # job record) is handled as terminal/idle-with-retry like `failed`.
    assert "function evalSemFailed(job, unavailable, doctor)" in body
    assert 'status === "failed" || status === "error"' in body
    assert "actions = evalSemFailed(job, unavailable, doctor);" in body
    assert 'modifier = " eval-sem-failed";' in body
    assert 'class="eval-sem-error error-text"' in body
    # The Retry reuses the shared trigger (same .eval-sem-btn class, so
    # wireEvalControls binds it to onEvalSemantic → a re-POST), and respects the
    # doctor gate.
    assert 'evalSemTrigger("Retry semantic evaluation…", unavailable, doctor)' in body


def test_evaluation_semantic_completed_job_selects_its_evaluation():
    body = app_js()
    # On completion the poll re-renders to the job's evaluation_id, selecting the
    # fresh evaluation so the Layer 2 report paints; a failed/gone job lands idle.
    assert 'job.status === "completed" && job.evaluation_id' in body
    assert "rerender(renderTaskEval(repo, taskId, job.evaluation_id))" in body


def test_evaluation_semantic_job_poll_is_chained_and_torn_down():
    body = app_js()
    # The poll mirrors the intake poll: one module-level handle, a chained
    # setTimeout (never setInterval) so responses can't overlap, torn down by
    # router() and at the top of renderTaskEval so it can't outlive its view.
    assert "let activeSemanticJobPoll = null;" in body
    assert "function stopSemanticJobPoll()" in body
    assert "function startSemanticJobPoll(repo, taskId)" in body
    assert "const SEMANTIC_JOB_POLL_MS = " in body
    assert "poll.timer = setTimeout(tick, SEMANTIC_JOB_POLL_MS);" in body
    # router() and renderTaskEval both tear it down (plus start's own reset).
    assert body.count("stopSemanticJobPoll();") >= 2


def test_evaluation_semantic_report_renders_scores_findings_and_provenance():
    body = app_js()
    # The Layer 2 report block: a "model judgment" tag distinct from Layer 1, the
    # evaluator provenance, per-axis scores with the required rationale, findings
    # ordered most-severe first with severity badge + evidence refs + suggestion,
    # and the suggested improvements.
    assert "function semanticReportSection(report)" in body
    assert "Layer 2 · model judgment" in body
    assert "sem-report" in body
    assert "function semanticScoreRow(s)" in body
    assert 'class="sem-rationale"' in body
    assert "function semanticFindingItem(f)" in body
    assert "function semanticFindingsSorted(findings)" in body
    assert "semRank(b.severity) - semRank(a.severity)" in body
    assert "function semSeverityKind(sev)" in body
    assert 'badge(f.severity, semSeverityKind(f.severity))' in body
    assert "sev-${escapeHtml(f.severity)}" in body
    assert ">Evidence</span>" in body
    assert ">Suggestion</span>" in body
    assert 'class="sem-improvements"' in body
    # The overall confidence + evaluator identity are shown (no naked score).
    assert '["overall confidence", conf]' in body
    assert '["evaluator", evaluatorTxt]' in body
    # The dimension vocabulary is humanized, and an absent semantic layer is called
    # out as a distinct state rather than looking like a failed report.
    assert "const SEMANTIC_DIM_LABELS = {" in body
    assert "function semanticAbsentSection()" in body


def test_evaluation_semantic_artifacts_are_copy_downloadable():
    body = app_js()
    # The semantic report JSON and its Markdown render join the local-inspection
    # affordances (a text/markdown data: URI for report.md), keyed so the copy
    # handler resolves each artifact string.
    assert "function evalMdArtifactRow(label, artifactKey, text, taskId, fileStem)" in body
    assert '"data:text/markdown;charset=utf-8," + encodeURIComponent(text)' in body
    assert 'evalArtifactRow("Semantic report", "semantic"' in body
    assert 'evalMdArtifactRow("Semantic report (Markdown)", "report_md"' in body
    assert "semantic: semanticJson" in body
    assert "report_md: reportMd" in body
    # The history picker distinguishes a semantic evaluation from a metrics-only one.
    assert "e.has_semantic_report" in body


def test_evaluation_history_picker_navigates_to_the_id_route():
    body = app_js()
    assert "function evalPickerSection(repo, taskId, evals, selectedId)" in body
    assert 'const pick = document.getElementById("eval-pick");' in body
    assert 'evalHash(repo, taskId) + "/" + encodeURIComponent(ev.target.value)' in body


# ---- semantic (Layer 2) dashboard ops, session-2-cont ----------------------
#
# The doctor gate and the history-picker model/version comparison. The doctor
# route + the evaluator list field are API-tested (test_api.py); these pin the
# dashboard wiring the JSC harness (scenarioTaskEval) then drives end-to-end.


def test_evaluation_semantic_control_gated_by_doctor_probe():
    body = app_js()
    # renderTaskEval probes the Layer 2 readiness doctor and passes it to the
    # semantic section; an unconfigured backend disables the trigger with an
    # explanation while Layer 1 stays usable.
    assert 'const health = await api("/api/evaluation/doctor");' in body
    assert "semanticDoctor = health && health.semantic ? health.semantic : null;" in body
    assert "${evalSemanticSection(semanticDoctor, semanticJob)}" in body
    assert "const unavailable = !!(doctor && doctor.available === false);" in body
    assert 'class="eval-sem-btn" disabled' in body
    assert "eval-sem-unavailable" in body
    assert "Layer 2 is unavailable on this hub" in body
    assert "The deterministic Layer 1 metrics above still work." in body
    # Best-effort: a 401 re-gates, but any other probe failure keeps Layer 2 in its
    # default enabled state rather than blanking the sidecar view.
    assert "semanticDoctor = null; // probe failed" in body


def test_evaluation_history_picker_compares_by_evaluator_model():
    body = app_js()
    # A semantic entry's picker label carries the evaluator model (and schema
    # version) so runs are comparable by model/version without opening each one;
    # the field is best-effort, so a null evaluator just omits the suffix.
    assert "if (e.has_semantic_report && e.evaluator) {" in body
    assert "e.evaluator.model," in body
    assert 'e.evaluator.schema_version ? "v" + e.evaluator.schema_version : ""' in body


def test_task_detail_has_no_evaluation_entry_or_fetch():
    body = app_js()
    # Evaluation state belongs to completed-task Run history, not the active task
    # control page. The detail renderer carries neither an entry nor a sidecar
    # availability/list request.
    task_detail = body[body.index("async function renderTaskDetail") :]
    task_detail = task_detail[: task_detail.index("// ---- run history ----")]
    assert "eval-entry" not in task_detail
    assert "Task evaluation" not in task_detail
    assert 'api("/api/evaluation")' not in task_detail
    assert "evalListPath" not in task_detail


def test_run_history_links_show_batched_three_level_evaluation_signal():
    body = app_js()
    # Run-history task groups get one of three compact signal colours from the
    # history payload, with the exact state retained in title/screen-reader text
    # and no per-group evaluation-list request or second availability probe.
    assert "let historyEvalEnabled = false;" in body
    assert "let historyEvalStates = new Map();" in body
    assert "data.evaluation_enabled" in body
    assert "data.evaluation_states" in body
    assert '(cursor ? "?cursor=" + encodeURIComponent(cursor) : "")' in body
    assert "historyEvalEnabled && group.repo_name && group.task_id" in body
    assert 'class="text-link tg-eval"' in body
    assert 'return { label: "not evaluated", kind: "none" }' in body
    assert 'return { label: "L1", kind: "l1" }' in body
    assert 'return { label: "L1 + L2", kind: "l1-l2" }' in body
    assert 'class="eval-state-dot ${evalSignal.kind}"' in body
    assert "Evaluation status:" in body
    assert 'aria-label="Evaluation, status:' in body
    assert "Evaluation " in body
    render_runs = body[body.index("async function renderRuns(cursor)") :]
    render_runs = render_runs[: render_runs.index("// ---- run-scoped task view")]
    assert 'api("/api/evaluation")' not in render_runs
    assert "evalListPath" not in render_runs
    # Placement contract: the action goes into the rollup (a `.roll-top` flex row
    # with the metrics), not a separate `.tg-actions` row between button and
    # rollup.
    assert "taskRollup(group, evalAction)" in body
    assert '<div class="roll-top">' in body
    assert "tg-actions" not in body


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
        # The run-history Evaluation link now rides the rollup summary row,
        # right-aligned inline, instead of a standalone `.tg-actions` row.
        ".roll-top .tg-eval",
        ".eval-state-dot",
        ".eval-state-dot.l1",
        ".eval-state-dot.l1-l2",
    ):
        assert selector in css, selector
    assert "display: inline-flex" in css
    assert "align-items: center" in css
    assert ".eval-entry" not in css


def test_semantic_layer_styles_are_present():
    css = styles_css()
    # The Layer 2 trigger button, its cost callout, the model-judgment block set
    # apart from the deterministic sections, the "model judgment" tag, the score
    # rationale, and the severity-accented finding cards.
    for selector in (
        ".eval-sem-btn",
        ".eval-sem-btn:disabled",
        ".eval-sem-unavailable",
        # The async-job running/failed states.
        ".eval-sem-running",
        ".eval-sem-failed",
        ".eval-sem-error",
        ".eval-cost",
        ".sem-report",
        ".sem-layer-tag",
        ".sem-summary",
        ".sem-rationale",
        ".sem-findings",
        ".sem-finding",
        ".sem-finding.sev-high",
        ".sem-finding.sev-medium",
        ".sem-improvements",
        ".sem-label",
    ):
        assert selector in css, selector


# ---- central evaluation-report feed (cross-repo, global order) --------------
#
# The feed view's runtime behaviour — that it paints the server's global order,
# that each row opens the existing per-task evaluation, that the forward-only
# pager can walk off the newest page and back — is proven in the keyboard
# harness (tests/test_web_keyboard.py, scenario `feed:`), which drives the real
# render against a stubbed feed. These served-asset checks pin the wiring and the
# boundary guarantees: the route, the entry point and its gate, the read-only
# surface, and the states that must stay distinct.


def test_report_feed_route_and_entry_are_wired():
    body = app_js()
    # Its own route, and re-entering it restarts the walk (the cursor trail is
    # client-side, so a fresh navigation must not resume mid-feed).
    assert 'if (seg[0] === "reports")' in body
    assert "reportTrail = [];" in body
    assert "return renderReports(null);" in body
    # A paged chronological feed: the wide column, never the card-grid "list"
    # kind, which would break a strictly ordered feed into columns.
    assert 'if (seg[0] === "reports") return "wide";' in body
    assert 'else if (path.startsWith("/reports")) tab = "/tasks";' in body
    # The way in is Run history, gated by the flag that payload already carries —
    # so a disabled hub hides it without a second request.
    assert "function reportsEntryLink(enabled)" in body
    assert "if (!enabled) return \"\";" in body
    assert 'href="#/reports"' in body
    # It rides one shared Run-history head rather than the runs-present branch, so
    # the two runless pages keep it: a report needs no related run and outlives
    # the run records it was built from, so a feed-holding hub can have zero runs.
    assert "function historyHead(data) {" in body
    assert "reportsEntryLink(data.evaluation_enabled)" in body
    runs_view = body[body.index("async function renderRuns(cursor)") :]
    runs_view = runs_view[: runs_view.index("function renderRunTask")]
    assert runs_view.count("historyHead(data)") == 3
    assert 'pageTitle("Run history")' not in runs_view


def test_report_feed_reads_the_global_feed_and_writes_nothing():
    body = app_js()
    assert "async function renderReports(after)" in body
    feed = body[body.index("// ---- central evaluation-report feed") :]
    feed = feed[: feed.index("// ---- remote intake")]
    # Availability first (the one route that answers while disabled), then the
    # catalog feed with the consumer's watermark as `after`.
    assert 'const avail = await api("/api/evaluation");' in feed
    assert 'const REPORTS_PATH = "/api/evaluation/reports";' in body
    assert '"?after=" + encodeURIComponent(after)' in feed
    # Reads only: the feed and the bundle route publish nothing, and this view
    # must never generate or trigger an evaluation.
    assert "method: \"POST\"" not in feed
    assert "evalMetricsPath" not in feed
    assert "evalSemanticJobPath" not in feed


def test_report_feed_rows_open_the_existing_task_evaluation():
    body = app_js()
    # Each row links INTO the per-task view at the exact evaluation the feed item
    # names, so the existing history picker/artifacts stay the report detail — the
    # feed adds a way in, it does not replace per-task evaluation navigation.
    assert "const reportDetailHash = (entry) =>" in body
    assert "evalHash(entry.repo_name, entry.task_id) +" in body
    assert 'encodeURIComponent(entry.evaluation_id)' in body
    # Global order, the source identity, the evaluator, and the key a consumer
    # re-fetches by are all on the row.
    assert 'class="report-seq">#${escapeHtml(entry.seq)}' in body
    assert '["repo", entry.repo_name || "(unknown repo)"]' in body
    assert '["evaluated while", entry.target_source || "unknown"]' in body
    assert 'badge("L1 + L2", "ok")' in body
    assert 'class="report-key muted" title="${escapeHtml(key)}"' in body
    # Provenance the evidence pack does not carry is said, not left blank — a
    # missing row would read as "no runs" / "no commits".
    assert 'runs.available ? String(runs.count || 0) : "unavailable"' in body
    assert 'git.available ? String(git.commit_count || 0) : "unavailable"' in body


def test_report_feed_pager_is_forward_only_with_a_client_trail():
    body = app_js()
    # The server offers only "after N", so Previous is served by remembering the
    # cursors walked; `has_more` decides the end, never a short page.
    assert "let reportTrail = [];" in body
    assert "reportTrail.push(data.cursor);" in body
    assert "rerender(renderReports(data.next_cursor));" in body
    assert "rerender(renderReports(reportTrail.pop()));" in body
    assert 'pagerButton("fp-prev", "Previous", "←", reportTrail.length > 0, true)' in body
    assert 'pagerButton("fp-next", "Next", "→", !!data.has_more, false)' in body
    assert 'data.has_more ? "more available" : "end of feed"' in body


def test_report_feed_keeps_its_empty_states_distinct():
    body = app_js()
    # Disabled, never-published, and past-the-end are three different answers,
    # and a never-published feed is its own state (no pager, like empty history)
    # while a page past the end keeps one so Previous is never a dead end.
    assert "const empty = !entries.length && !reportTrail.length;" in body
    assert '(empty ? "" : reportPager(data, entries))' in body
    assert "Task evaluation is disabled on this hub" in body
    assert "so no reports are " in body
    assert "No evaluation reports have been published yet." in body
    assert "No further reports after this point in the feed." in body
    # The publication rule is not guessable, so the empty state states it.
    assert "published once a task is archived and its complete Layer 1 + " in body
    # The ownership boundary the feed exists under, in the view itself.
    assert "orch-hub publishes the artifacts" in body
    assert "does not decide what to change in the protocol" in body


def test_report_feed_styles_are_present():
    css = styles_css()
    for selector in (
        ".reports-entry",
        ".reports-intro",
        ".report-seq",
        ".card.report .card-head .task-id",
        ".report-key",
        ".report-key .rk",
    ):
        assert selector in css, selector


# ---- evaluation queue on overview ---------------------------------------------
#      (2026-08-09-completion-triggered-evaluation, session-2-cont)
#
# The Overview gains a second independent, display-only section over
# GET /api/evaluation/queue: the cross-repo state of automatic Layer 2 evaluation.
# Its whole reason to exist is that the automatic path fails silently — a held
# queue, a failed turn, and a switch left off all look like nothing happening — so
# these pin the served-asset wiring and the boundaries that keep it display-only.
# Rendering/state behaviour is verified by the jsc harness
# (tests/keyboard_harness.js, scenarioEvaluationQueue).


def test_overview_evaluation_queue_is_a_seventh_best_effort_source():
    body = app_js()
    assert '"/api/evaluation/queue"' in body
    assert "evaluationQueuePanel" in body
    # Its own reader, not overviewSource: this is the one panel that must tell a
    # 404 (evaluation switched off — nothing to show) from a failed read (unknown,
    # and an unknown queue must never render as an empty one).
    assert "overviewEvalQueue" in body
    assert "err.status === 404" in body
    assert "Evaluation queue is unavailable." in body


def test_overview_evaluation_queue_is_display_only():
    body = app_js()
    # Its own section heading, painted OUTSIDE the overview-panels grid: an
    # unmeasured corpus is not a blocked run, so it never lays out as an
    # operational panel and never becomes a Needs attention row.
    assert "Evaluation queue" in body
    assert "overview-evalq" in body
    assert "evaluationQueuePanel(data.evalQueue)" in body


def test_overview_evaluation_queue_explains_every_way_it_can_stall():
    body = app_js()
    # The three record-borne reasons, mapped from the hub's bounded machine tokens
    # (orch_hub/eval_dispatch.py) with an unknown token falling through verbatim.
    assert "DEFER_REASON_TEXT" in body
    for token in ("capacity", "usage-unknown", "pacing"):
        assert token in body, token
    # The fourth reason leaves no trace on the records by design, so it can only
    # render from the payload's config echo.
    assert "queue.auto_enabled" in body
    assert "ORCH_HUB_EVAL_AUTO" in body
    # The overdue bound is the hub's configured threshold, never a client literal.
    assert "queue.queue_max_age_seconds" in body
    # A capped page must not read as "this is everything".
    assert "queue.truncated" in body
    assert "counts above are complete" in body


def test_task_evaluation_view_renders_a_held_layer_2_job():
    body = app_js()
    # A deferred job is accepted and durably owed, not absent: without its own
    # branch it fell through to the idle trigger and the page read "never
    # evaluated" for a task the corpus already owes a report.
    assert 'status === "deferred"' in body
    assert "evalSemHeld" in body
    assert "eval-sem-held" in body
    # The manual override stays offered — it is the documented escape from a queue
    # a broken usage feed would otherwise hold forever.
    assert "Run semantic evaluation now…" in body


def test_overview_evaluation_queue_styles():
    css = styles_css()
    for selector in (
        ".evq-summary",
        ".evq-rows",
        ".evq-row",
        ".evq-state",
        ".evq-meta",
        ".evq-flag",
    ):
        assert selector in css, selector
    # An overdue held job uses the same left-accent idiom the task cards use for a
    # run signal, warn-coloured — nothing here is an error state.
    assert ".evq-row.evq-overdue > a" in css
    assert ".evq-warn" in css


def test_shell_assets_resolve_through_the_runtime_resource_root():
    # In a compiled release the first-party modules have no on-disk source file,
    # so `Path(__file__).parent / "web"` would address nothing. The shell assets
    # must come from the runtime's resource root in both shapes.
    from orch_hub import resources

    assert api.WEB_DIR == resources.web_dir()
    for filename, _ in api.SHELL_ASSETS.values():
        assert (api.WEB_DIR / filename).is_file(), filename


# ---- protocol identity coverage ----------------------------------------------
#      (2026-08-29-protocol-identity-coverage, session-2)
#
# Rendering behaviour — the eight coverage labels and the pending/liveness join —
# is verified by the jsc harness (tests/keyboard_harness.js,
# scenarioProtocolCoverage). These pin the boundaries a grep can hold: that the
# view derives its verdict from the payload it already has, and that the vocabulary
# it renders is the hub's, not a second one invented here.


def test_protocol_coverage_is_read_not_re_derived():
    body = app_js()
    # The view reads the `coverage` the hub derived, and makes exactly one
    # addition of its own: splitting a `pending` end check by run liveness, which
    # the artifact alone cannot do. It must not reconstruct the verdict from
    # `available`/`stability`/`checked_ts` — that mapping lives in
    # orch_hub/protocol_provenance.py and having two of it is the defect.
    assert "function protocolCoverage(snap, live) {" in body
    assert 'const coverage = snap ? String(snap.coverage || "") : "absent";' in body
    assert 'if (coverage === "pending" && !live) return "pending-ended";' in body
    assert "protocolCoverage(snap, LIVE_STATES.has(run.state))" in body
    assert "snap.stability" not in body


def test_protocol_coverage_keeps_every_reason_apart():
    from orch_hub.protocol_provenance import COVERAGES

    body = app_js()
    covermap = body[body.index("const PROTOCOL_COVERAGE = {") :]
    covermap = covermap[: covermap.index("\n  };")]
    # The cross-language half of the contract: every value the hub can derive has
    # its own label here. A coverage the SPA does not know renders as its raw name
    # — honest, but unexplained — so the map must track COVERAGES, and this is the
    # only place that can notice when it stops.
    for value in COVERAGES:
        assert f"{value}:" in covermap, value
    # Plus the one key the hub never sends: the reader's join of a `pending` end
    # check with a run that has already finished.
    assert '"pending-ended":' in covermap
    # The three end-check states an operator must not confuse: a check still to
    # come, one that will now never run, and one that ran and concluded nothing.
    assert '"end check pending"' in covermap
    assert '"never checked"' in covermap
    assert '"end check inconclusive"' in covermap
    # The hub's own per-run sentence reaches the reader rather than only the log.
    assert '["detail", snap.detail || "\u2014"]' in body


def test_protocol_coverage_rides_the_history_payload():
    body = app_js()
    # Snapshot-only, like the launch config beside it: no per-run fetch, and
    # rendered outside the card link (a <details> inside an <a> is invalid and its
    # click would fight the card's navigation).
    card = body[body.index("function historyRunCard(run) {") :]
    card = card[: card.index("// The way into the cross-repo report feed")]
    assert "const protocol = protocolBlock(run);" in card
    assert "</a>\n        ${config}\n        ${protocol}" in card
    assert "api(" not in card


def test_report_card_states_protocol_provenance():
    body = app_js()
    # The one provenance block the feed card skipped while rendering the two
    # beside it — and the one an external consumer actually cohorts by. Absent
    # provenance is said with its reason, under the rule already written there.
    assert "const proto = prov.protocol || {};" in body
    assert "shortRev(proto.effective_revision)" in body
    assert '"unavailable" + (proto.detail ? " — " + proto.detail : "")' in body


# ---- protocol identity coverage census ---------------------------------------
#      (2026-08-29-protocol-identity-coverage, session-3)
#
# Rendering behaviour — the buckets, the two unavailable halves, the truncation
# note, the entry links — is verified by the jsc harness (scenarioProtocolCensus).
# These pin the two boundaries a grep can hold, and both are cross-surface: the
# census must not hold a second copy of the vocabulary the run rows use, nor a
# second copy of the order the hub emits.


def test_coverage_census_reuses_the_run_row_vocabulary():
    body = app_js()
    # One label map for both surfaces. A census whose buckets read differently
    # from the rows they count is two opinions, which is the defect this whole
    # feature removes — so the census renders through PROTOCOL_COVERAGE and adds
    # no labels of its own.
    census = body[body.index("function coverageRow(key, count) {") :]
    census = census[: census.index("async function renderProtocolCoverage")]
    assert "PROTOCOL_COVERAGE[key] || [key, \"\", \"\"]" in census
    assert "const PROTOCOL_COVERAGE" not in census


def test_coverage_census_takes_its_order_from_the_payload():
    body = app_js()
    from orch_hub.protocol_provenance import RUN_COVERAGES

    census = body[body.index("function coverageRunsSection(runs) {") :]
    census = census[: census.index("function coverageReportRow(entry)")]
    # The hub emits counts in RUN_COVERAGES order and JSON object key order is
    # preserved, so the page enumerates the payload rather than holding a list
    # that a new bucket would silently fall out of.
    assert "Object.keys(counts)" in census
    for value in RUN_COVERAGES:
        assert f'"{value}"' not in census or value == "verified"
    # `verified` is the one name the section may hold: it is the answer to the
    # page's question and renders at zero, where every other bucket is hidden.
    assert 'key === "verified"' in census
    assert "counts.verified" in census


def test_coverage_census_entry_is_not_gated_on_evaluation():
    body = app_js()
    # The run half is a property of run records, so the way in must survive a hub
    # with the sidecar off — the hub whose every report is unpublished. Its own
    # class, because the feed entry beside it IS gated and a shared selector would
    # make "is the feed offered here" unanswerable.
    entry = body[body.index("function coverageEntryLink() {") :]
    entry = entry[: entry.index("\n  }")]
    assert '<p class="coverage-entry">' in entry
    assert "enabled" not in entry
    assert (
        "reportsEntryLink(data.evaluation_enabled) +\n      coverageEntryLink()"
        in body
    )


def test_coverage_census_is_a_read_with_no_write_surface():
    body = app_js()
    view = body[body.index("async function renderProtocolCoverage() {") :]
    view = view[: view.index("// ---- remote intake")]
    # Asking how many runs carry an identity must never settle one: the page has
    # no POST, no polling, and one read.
    assert 'api("/api/protocol-coverage")' in view or "api(COVERAGE_PATH)" in view
    assert "method:" not in view
    assert "setTimeout" not in view


def test_coverage_census_styles_are_present():
    css = styles_css()
    for selector in (".cov-row", ".cov-count", ".cov-why", ".cov-detail"):
        assert selector in css, selector


def test_protocol_coverage_styles_are_present():
    css = styles_css()
    for selector in (
        ".card.run .run-protocol",
        ".card.run .run-protocol summary",
        ".card.run .run-protocol .pc-rev",
        ".card.run .run-protocol .pc-why",
    ):
        assert selector in css, selector
