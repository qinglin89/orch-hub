"use strict";
/*
 * orch-hub mobile dashboard — vanilla SPA, no build step.
 *
 * Auth model: this shell is served publicly (client code, no secrets). The
 * bearer token lives only in this browser's localStorage; every data fetch —
 * including the SSE log stream — attaches `Authorization: Bearer <token>`. The
 * log stream is read with fetch + a ReadableStream reader rather than
 * `EventSource`, which cannot set that header, so the API keeps a single
 * credential path (no cookies, no query tokens). A 401 clears the stored token
 * and returns to the login gate.
 *
 * Views: login gate; repo list + repo detail; all-task list + task detail
 * (with an inline answer box for waiting questions); run history; and a live/
 * recent log tail per run. The hash router tolerates unknown routes by falling
 * back to the repo list.
 *
 * Accessibility model: every view starts with exactly one `h1.page-title`
 * naming the page. router() mirrors it into document.title and moves focus to
 * it after a navigation, so a keyboard/screen-reader user lands on the new page
 * instead of the control they activated; it is a plain focus move, never a
 * trap. In-place repaints (Overview refresh, intake poll, log append, Reload)
 * deliberately do NOT move focus and are NOT inside a live region — only small
 * regions that a human acted on announce (`role="status"` control lines,
 * `role="alert"` validation errors, the polite filter counts).
 */
(function () {
  const TOKEN_KEY = "orch-hub-token";
  const LIVE_STATES = new Set(["active", "waiting"]); // run states that badge a repo
  const DEFAULT_LOG_TAIL = 200; // initial log lines to request (matches the API default)
  const INTAKE_POLL_MS = 2000; // review-view poll cadence for an in-flight intake turn
  const OVERVIEW_REFRESH_MS = 15000; // Overview background auto-refresh cadence (passive dashboard)
  const OVERVIEW_HISTORY_LIMIT = 12; // recent-run window fetched for the outcomes panel
  const OVERVIEW_OUTCOME_COUNT = 5; // ended runs shown in the outcomes panel
  let activeStream = null; // AbortController for the in-flight SSE log stream, if any
  let activeIntakePoll = null; // { cancelled, timer } for the in-flight intake poll, if any
  let activeOverviewRefresh = null; // { cancelled, timer } for the Overview auto-refresh, if any
  let routeGeneration = 0; // bumped by every router() entry; see router()
  // Known task-file statuses in protocol order; the sentinels are data
  // problems (file missing / status unparseable), not runnable states.
  const KNOWN_STATUSES = [
    "pending",
    "in_progress",
    "final_review",
    "completed",
    "blocked",
  ];
  const SENTINELS = new Set(["missing", "unknown"]);
  // Task statuses the orchestrator attaches to without caveats — mirrors
  // runs.LAUNCHABLE_STATUSES. `blocked` is runnable only with an explicit
  // acknowledgement; the sentinels and any other status are not runnable. This
  // only gates whether the dashboard *offers* Start; the backend re-checks and
  // stays the source of truth (a stale offer is answered by its 409).
  const RUNNABLE_STATUSES = new Set([
    "pending",
    "in_progress",
    "final_review",
    "completed",
  ]);
  // Remote-intake lifecycle labels (mirror orch_hub.intake). The terminal set is
  // a resting outcome; `awaiting_review` is the single reviewable state; the
  // transient trio (drafting/revising/confirming) brackets an in-flight agent
  // turn that runs off the request lock on the hub — so the review view polls
  // GET /api/intakes/{id} through it until it settles (see startIntakePoll).
  // INTAKE_TERMINAL spots a repo's one in-flight intake (any non-terminal one).
  const INTAKE_TERMINAL = new Set([
    "confirmed",
    "cancelled",
    "failed",
    "error",
  ]);
  // The in-flight statuses the review view polls through: an agent turn is
  // running on the hub, so the record self-updates to a resting status when it
  // settles. Mirrors orch_hub.intake._TRANSIENT. (`confirming` is deliberately
  // not cancellable — see the working panel — so no cancel races the write.)
  const INTAKE_TRANSIENT = new Set(["drafting", "revising", "confirming"]);

  // ---- token storage ----
  const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
  function setToken(value) {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  }

  // ---- API client ----
  class ApiError extends Error {
    constructor(message, status, body) {
      super(message);
      this.status = status;
      // Parsed JSON error body when the hub sent one, so callers can render the
      // structured launch-refusal details a plain message drops: `reasons`
      // (LaunchBlocked), `run_id` (RepoBusy), `acknowledge_required` (blocked).
      this.body = body || null;
    }
  }

  // Bearer header for every hub request — the single credential path. Empty
  // when no token is stored (the request then 401s and re-gates).
  function authHeaders() {
    const token = getToken();
    return token ? { Authorization: "Bearer " + token } : {};
  }

  // GET by default; pass { method, body } to POST JSON (e.g. answering a
  // question). The bearer header is attached to every call — the single
  // credential path the whole SPA relies on.
  async function api(path, options) {
    const opts = options || {};
    const headers = authHeaders();
    const init = { method: opts.method || "GET", headers };
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    let resp;
    try {
      resp = await fetch(path, init);
    } catch (err) {
      throw new ApiError("Network error reaching the hub.", 0);
    }
    if (resp.status === 401) {
      // Missing/invalid token: drop it and force the login gate.
      setToken("");
      renderLogin("That token was rejected. Enter a valid one.");
      throw new ApiError("unauthorized", 401);
    }
    if (resp.status === 503) {
      throw new ApiError(
        "The hub has no ORCH_HUB_TOKEN configured, so it cannot serve data.",
        503
      );
    }
    if (!resp.ok) {
      let detail = resp.status + " " + resp.statusText;
      let body = null;
      try {
        body = await resp.json();
        if (body && body.detail) detail = body.detail;
      } catch (_) {
        /* non-JSON error body */
      }
      throw new ApiError(detail, resp.status, body);
    }
    if (resp.status === 204) return null;
    return resp.json();
  }

  // ---- small DOM helpers ----
  const view = () => document.getElementById("view");

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(
      /[&<>"']/g,
      (ch) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        }[ch])
    );
  }

  const badge = (label, kind) =>
    `<span class="badge ${kind || ""}">${escapeHtml(label)}</span>`;

  // A run-state badge, coloured by state: `waiting` warns (needs a human),
  // `active` rides the "run" colour, and finished states map to ok/bad/neutral.
  function runBadgeKind(state) {
    if (state === "waiting") return "warn";
    if (state === "active") return "run";
    if (state === "completed") return "ok";
    if (state === "error") return "bad";
    return ""; // stopped / ended: neutral
  }
  const runBadge = (state) => badge(state, runBadgeKind(state));

  // A compact run-state *dot* that leads the repo name: colour-coded
  // (yellow=waiting, green=active, gray=idle) with the state word carried only
  // on title/aria-label, never as visible text. Run-state and protocol status
  // share warm colours (`waiting` and a `blocked` status are both yellow), so a
  // bare dot — not a `.badge` pill nor a dot+word label — keeps the two apart
  // while never repeating the state as a visible word. `state` is a live run
  // state ("waiting"/"active") or null (idle).
  function runDot(state) {
    const kind =
      state === "waiting" ? "waiting" : state === "active" ? "active" : "idle";
    const label =
      kind === "waiting"
        ? "waiting for an answer"
        : kind === "active"
        ? "running"
        : "no live run";
    return (
      `<span class="run-dot ${kind}" title="${escapeHtml(label)}" ` +
      `aria-label="run: ${escapeHtml(label)}"></span>`
    );
  }

  // Collapse a repo's live runs to a single signal state. Per-repo serialization
  // allows only one live run, but this handles an array defensively; waiting
  // outranks active. Returns "waiting"/"active", or null when nothing is live.
  function repoRunState(liveRuns) {
    let state = null;
    for (const run of liveRuns || []) {
      if (run.state === "waiting") return "waiting";
      if (run.state === "active") state = "active";
    }
    return state;
  }

  // Format a commit's strict-ISO `committed_ts` (git's %cI, passed verbatim by
  // the hub) for at-a-glance display: a compact relative age for recent commits,
  // falling back to the calendar date for older ones and to the raw string if it
  // can't be parsed — trust the hub's value, but never render "NaN".
  function formatCommitTime(iso) {
    if (!iso) return "";
    const then = new Date(iso);
    if (isNaN(then.getTime())) return iso;
    const secs = Math.floor((Date.now() - then.getTime()) / 1000);
    if (secs < 0) return then.toLocaleDateString();
    if (secs < 60) return "just now";
    const mins = Math.floor(secs / 60);
    if (mins < 60) return mins + "m ago";
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + "h ago";
    const days = Math.floor(hrs / 24);
    if (days < 7) return days + "d ago";
    return then.toLocaleDateString();
  }

  const centerHtml = (text) => `<p class="center">${escapeHtml(text)}</p>`;

  // Every view opens with exactly one `h1.page-title` naming the page. It is the
  // document outline's root (section headings are h2, their sub-headings h3),
  // the post-navigation focus target, and the source of document.title — so a
  // route always announces itself once, by name. `tabindex="-1"` makes it
  // programmatically focusable without joining the tab order.
  const pageTitle = (text, extraClass) =>
    `<h1 class="page-title${
      extraClass ? " " + extraClass : ""
    }" tabindex="-1">${escapeHtml(text)}</h1>`;

  // A loading placeholder that already carries the page heading, so the heading
  // exists from the first paint and a slow route still has a focus target.
  const loadingView = (title, text) => pageTitle(title) + centerHtml(text);

  // A scrollable pre block (task file, index entry, …). `.task-content` caps its
  // height and scrolls, so it needs `tabindex="0"` to be scrollable by keyboard
  // at all, and a name to be worth landing on. Content stays escaped — these
  // bodies are untrusted repo data.
  const taskContentBlock = (text, label) =>
    `<pre class="task-content" tabindex="0" aria-label="${escapeHtml(
      label
    )}">${escapeHtml(text)}</pre>`;

  // Build an in-app hash link. Each dynamic segment is encoded so a repo name
  // or task id is always exactly one path segment for the router to split back.
  const repoHash = (repo) => "#/repos/" + encodeURIComponent(repo);
  const taskHash = (repo, taskId) =>
    "#/tasks/" + encodeURIComponent(repo) + "/" + encodeURIComponent(taskId);
  const logsHash = (runId) => "#/runs/" + encodeURIComponent(runId) + "/logs";
  // A run's task file, resolved through the run record (active or archived) so
  // it stays reachable after close-out archives the task — unlike taskHash,
  // which points at the index-gated (active-only) task detail page.
  const runTaskHash = (runId) => "#/runs/" + encodeURIComponent(runId) + "/task";
  // A remote intake's draft-review view, addressed by intake id.
  const intakeHash = (intakeId) => "#/intakes/" + encodeURIComponent(intakeId);

  function showChrome(authed) {
    document.getElementById("signout-btn").hidden = !authed;
    document.getElementById("nav").hidden = !authed;
  }

  // Mark the current tab both visually (.active, a colour change) and
  // semantically: `aria-current="page"` is the accessible-state channel, so the
  // active tab is announced as such instead of relying on colour alone.
  function setActiveTab(path) {
    let tab = "/repos";
    if (path.startsWith("/overview")) tab = "/overview";
    else if (path.startsWith("/tasks")) tab = "/tasks";
    else if (path.startsWith("/runs")) tab = "/runs";
    document.querySelectorAll(".tabbar a").forEach((a) => {
      const active = a.dataset.tab === tab;
      a.classList.toggle("active", active);
      if (active) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }

  // Mirror the rendered page heading into document.title, so the browser/AT
  // window title tracks the route without every view repeating itself.
  function syncDocumentTitle() {
    const head = document.querySelector("#view .page-title");
    const text = head ? head.textContent.trim() : "";
    document.title = text ? text + " · orch-hub" : "orch-hub";
  }

  // Move focus to the routed view's page heading. The heading is
  // `tabindex="-1"`, so this is a plain focus move: Tab continues into the view
  // and Shift+Tab back out to the chrome — nothing is trapped.
  function focusPageStart() {
    const target = document.querySelector("#view .page-title") || view();
    if (target && typeof target.focus === "function") target.focus();
  }

  // Land on a freshly rendered view: name it in document.title and put focus at
  // its start. Called for a navigation (router) and for an action that repaints
  // the whole view (rerender, the login gate) — i.e. exactly when the control
  // the user was on has just been destroyed. Background repaints (the Overview
  // refresh, an intake elapsed tick, a streamed log line) never call this.
  function landOnView() {
    syncDocumentTitle();
    focusPageStart();
  }

  function renderError(err) {
    const msg =
      err instanceof ApiError ? err.message : "Something went wrong.";
    view().innerHTML =
      pageTitle("Error") +
      `<div class="center"><p class="error-text" role="alert">${escapeHtml(
        msg
      )}</p>` +
      `<button class="retry-btn" id="retry-btn" type="button">Retry</button></div>`;
    document
      .getElementById("retry-btn")
      .addEventListener("click", () => router());
  }

  // ---- login gate ----
  // The login gate is reached three ways (no stored token, a mid-flight 401, and
  // sign-out), only one of which goes through router() — so it does its own
  // title sync and focus move rather than leaving them to the router.
  // The token field carries a real <label>; a rejected-token message is an
  // `role="alert"` so it is announced when it replaces the form in place.
  function renderLogin(message) {
    showChrome(false);
    view().innerHTML = `
      <section class="card login">
        ${pageTitle("Connect to orch-hub")}
        <p class="muted">Paste the service bearer token
          (<code>ORCH_HUB_TOKEN</code>). It is stored only in this browser.</p>
        ${
          message
            ? `<p class="error-text" role="alert">${escapeHtml(message)}</p>`
            : ""
        }
        <form id="login-form">
          <label for="token-input">Bearer token</label>
          <input id="token-input" type="password" autocomplete="off"
            autocapitalize="off" spellcheck="false" placeholder="bearer token" />
          <button type="submit">Connect</button>
        </form>
      </section>`;
    const form = document.getElementById("login-form");
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const value = document.getElementById("token-input").value.trim();
      if (!value) return;
      setToken(value);
      router();
    });
    landOnView();
  }

  // ---- run overlay helpers (shared by list + detail views) ----
  function groupLiveRunsByRepo(runs) {
    const map = {};
    for (const run of runs || []) {
      if (!LIVE_STATES.has(run.state)) continue;
      (map[run.repo_name] = map[run.repo_name] || []).push(run);
    }
    return map;
  }

  // Live-run-per-task map for one repo: task_id -> live run (waiting wins over
  // active so a task needing an answer badges accordingly).
  function liveRunsByTask(runs, repoName) {
    const map = {};
    for (const run of runs || []) {
      if (run.repo_name !== repoName || !LIVE_STATES.has(run.state)) continue;
      const cur = map[run.task_id];
      if (!cur || (run.state === "waiting" && cur.state !== "waiting"))
        map[run.task_id] = run;
    }
    return map;
  }

  // Nested live-run lookup for the all-tasks list: repoName -> task_id -> live
  // run (waiting wins over active). Lets that multi-repo view paint run-state
  // accents from one /api/runs fetch instead of a per-card lookup.
  function liveRunsByRepoTask(runs) {
    const map = {};
    for (const run of runs || []) {
      if (!LIVE_STATES.has(run.state)) continue;
      const byTask = (map[run.repo_name] = map[run.repo_name] || {});
      const cur = byTask[run.task_id];
      if (!cur || (run.state === "waiting" && cur.state !== "waiting"))
        byTask[run.task_id] = run;
    }
    return map;
  }

  // The run to surface on a task-detail view: a live run wins; otherwise the
  // most recent finished one (list_runs is oldest-first, so the last match).
  function pickRunForTask(runs, repoName, taskId) {
    const mine = (runs || []).filter(
      (r) => r.repo_name === repoName && r.task_id === taskId
    );
    if (!mine.length) return null;
    return mine.find((r) => LIVE_STATES.has(r.state)) || mine[mine.length - 1];
  }

  const missingIndexCard = (repoName) =>
    `<li class="card problem"><div class="card-head">
       <span class="name">${escapeHtml(repoName)}</span>
       ${badge("no index", "bad")}
     </div>
     <div class="error-text">no <code>.ai-tasks/index.md</code> — task inventory unavailable</div>
     </li>`;

  // The ready-repo metadata line: latest commit time + active task count. Each
  // part is best-effort and optional — a null commit (git unread) or an absent
  // count simply drops out, and an empty line renders nothing. `taskCount` is
  // the task-inventory size (index rows), NOT a raw `.ai-tasks/` file count.
  function repoMetaLine(commit, taskCount) {
    const parts = [];
    if (commit && commit.committed_ts) {
      const when = formatCommitTime(commit.committed_ts);
      if (when) parts.push("last commit " + escapeHtml(when));
    }
    if (typeof taskCount === "number")
      parts.push(
        taskCount + (taskCount === 1 ? " active task" : " active tasks")
      );
    if (!parts.length) return "";
    return `<div class="repo-meta muted">${parts.join(" · ")}</div>`;
  }

  function housekeepingWarning(compact) {
    return (
      `<div class="housekeeping-warning${compact ? " compact" : ""}" role="status">` +
      `<span class="warning-icon" aria-hidden="true">&#9888;</span>` +
      `<span>Housekeeping pending</span>` +
      `</div>`
    );
  }

  function repoCard(repo, liveRuns, taskCount) {
    const readiness = repo.readiness || { ready: false, checks: [] };
    // The tri-state run signal (yellow=waiting / green=active / gray=idle) is a
    // compact dot leading the repo name (see the card head below), always
    // present so "no live run" reads as an explicit gray idle rather than an
    // absence. The readiness pill keeps the right of the head to itself.
    const badges = readiness.ready
      ? badge("ready", "ok")
      : badge("not ready", "bad");
    const reasons = readiness.ready
      ? ""
      : `<ul class="reasons">${(readiness.checks || [])
          .map(
            (c) =>
              `<li><code>${escapeHtml(c.name)}</code> ${escapeHtml(
                c.message
              )}</li>`
          )
          .join("")}</ul>`;
    // Ready repos surface at-a-glance metadata (latest commit + active task
    // count); not-ready repos keep their failure reasons instead.
    const meta = readiness.ready ? repoMetaLine(repo.commit, taskCount) : "";
    return `
      <li class="card repo">
        <a class="card-link" href="${repoHash(repo.name)}">
          <div class="card-head">
            <span class="name">${runDot(
              repoRunState(liveRuns)
            )}${escapeHtml(repo.name)}</span>
            <span class="badges">${badges}</span>
          </div>
          <div class="path muted">${escapeHtml(repo.path)}</div>
          ${repo.housekeeping_pending ? housekeepingWarning(true) : ""}
          ${meta}
          ${reasons}
        </a>
      </li>`;
  }

  // Active task count per repo from the task-inventory payload: the number of
  // index-listed task rows, NOT a raw `.ai-tasks/` directory file count.
  function taskCountsByRepo(repos) {
    const map = {};
    for (const repo of repos || []) map[repo.name] = (repo.tasks || []).length;
    return map;
  }

  async function renderRepos() {
    view().innerHTML = loadingView("Repos", "Loading repos…");
    const reposData = await api("/api/repos");
    const repos = reposData.repos || [];
    let runsByRepo = {};
    try {
      const runsData = await api("/api/runs");
      runsByRepo = groupLiveRunsByRepo(runsData.runs);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
      // Run signals are best-effort; a runs failure must not hide the repo list.
    }
    // Best-effort active-task counts for the ready-repo metadata line, from the
    // task-inventory payload. Like the run overlay, a tasks failure degrades the
    // count (dropped from the line) rather than hiding the repo list.
    let taskCountByRepo = {};
    try {
      const tasksData = await api("/api/tasks");
      taskCountByRepo = taskCountsByRepo(tasksData.repos);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
    }
    if (!repos.length) {
      view().innerHTML =
        pageTitle("Repos") + centerHtml("No managed repos discovered.");
      return;
    }
    view().innerHTML =
      pageTitle("Repos") +
      `<ul class="list">` +
      repos
        .map((r) => repoCard(r, runsByRepo[r.name], taskCountByRepo[r.name]))
        .join("") +
      `</ul>`;
  }

  // ---- all-task list ----
  function statusBadgeKind(status) {
    if (SENTINELS.has(status)) return "bad";
    if (status === "blocked") return "warn";
    if (status === "completed") return "ok";
    return "";
  }

  // Colour an intake status badge: confirmed = ok, awaiting_review needs the
  // human (warn), failed/error = bad, cancelled = neutral, and the in-flight
  // transient states ride the "run" colour.
  function intakeBadgeKind(status) {
    if (status === "confirmed") return "ok";
    if (status === "awaiting_review") return "warn";
    if (status === "failed" || status === "error") return "bad";
    if (status === "cancelled") return "";
    return "run"; // drafting / revising / confirming
  }

  function taskProblemText(status) {
    return status === "missing"
      ? "listed in the index but the task file is absent"
      : "task file has no parseable frontmatter status";
  }

  function taskCard(task, repoName, liveRun) {
    const sentinel = SENTINELS.has(task.status);
    const problem = sentinel
      ? `<div class="error-text">task file problem: ${taskProblemText(
          task.status
        )}</div>`
      : "";
    const blockers =
      task.blockers && task.blockers.length
        ? `<div class="blockers muted">blockers: ${task.blockers
            .map(escapeHtml)
            .join(", ")}</div>`
        : "";
    // Run-state shows only as a colour-coded left-border accent, kept distinct
    // from the status pill so a runtime `waiting` is never read as a protocol
    // `blocked` status (both warm-coloured). The border is the whole runtime
    // signal on a task card — no separate dot/word row that would just repeat
    // it. Idle tasks get no accent at all.
    const runState = liveRun ? liveRun.state : null;
    const accent =
      runState === "waiting"
        ? " run-waiting"
        : runState === "active"
        ? " run-active"
        : "";
    return `
      <li class="card task${sentinel ? " problem" : ""}${accent}">
        <a class="card-link" href="${taskHash(repoName, task.task_id)}">
          <div class="card-head">
            <span class="task-id">${escapeHtml(task.task_id)}</span>
            <span class="badges">${badge(
              task.status,
              statusBadgeKind(task.status)
            )}</span>
          </div>
          <div class="repo-name muted">${escapeHtml(repoName)}</div>
          ${task.summary ? `<div class="summary">${escapeHtml(task.summary)}</div>` : ""}
          ${blockers}
          ${problem}
        </a>
      </li>`;
  }

  // Known statuses in protocol order, then any extras (sentinels / future
  // statuses) sorted — so the filter dropdown reads sensibly.
  function orderedStatusList(seen) {
    const known = KNOWN_STATUSES.filter((s) => seen.has(s));
    const extra = [...seen].filter((s) => !KNOWN_STATUSES.includes(s));
    extra.sort();
    return [...known, ...extra];
  }

  function orderedStatuses(repos) {
    const seen = new Set();
    for (const repo of repos)
      for (const task of repo.tasks || []) seen.add(task.status);
    return orderedStatusList(seen);
  }

  function orderedStatusesFromTasks(tasks) {
    const seen = new Set();
    for (const task of tasks) seen.add(task.status);
    return orderedStatusList(seen);
  }

  function optionList(values, selected, allLabel) {
    return (
      `<option value="">${escapeHtml(allLabel)}</option>` +
      values
        .map(
          (v) =>
            `<option value="${escapeHtml(v)}"${
              v === selected ? " selected" : ""
            }>${escapeHtml(v)}</option>`
        )
        .join("")
    );
  }

  // Filters are held in the closure and applied client-side, so changing a
  // filter never refetches — task inventory is read once per view entry. The
  // free-text `search` composes with repo/status as AND (see matchesTaskSearch)
  // and persists across view re-entry like the run-history task filter.
  const taskFilters = { repo: "", status: "", search: "" };
  // Repo-detail status filter (repo is fixed by the route); reset when it no
  // longer matches the repo's task statuses.
  let repoDetailStatus = "";

  // Case-insensitive substring search over the fields a human scans a task by:
  // id, summary, repo name, status, and any blocker text. Empty/whitespace query
  // is a wildcard. A sentinel task (missing/unknown) still matches on its id,
  // repo, or status text, so a targeted search never silently drops a problem row.
  function matchesTaskSearch(task, repoName) {
    const q = taskFilters.search.trim().toLowerCase();
    if (!q) return true;
    const hay = [task.task_id, task.summary, repoName, task.status]
      .concat(task.blockers || [])
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  }

  // Repaint just the count line + task list from the held filters; the filter
  // controls stay put so the search box keeps focus/caret across keystrokes
  // (mirrors renderRunGroups). Missing-index cards follow the repo filter only —
  // a repo-level data gap, not a task row — so status/search never hide them and
  // they are excluded from the task tally. `total` is the whole inventory (all
  // repos/statuses), the stable denominator behind the "X of Y" count.
  function renderTaskList(repos, liveByRepoTask) {
    const selectedRepos = taskFilters.repo
      ? repos.filter((r) => r.name === taskFilters.repo)
      : repos;
    let total = 0;
    for (const repo of repos) total += (repo.tasks || []).length;
    const cards = [];
    let visible = 0;
    for (const repo of selectedRepos) {
      if (!repo.index_present) cards.push(missingIndexCard(repo.name));
      const byTask = (liveByRepoTask && liveByRepoTask[repo.name]) || {};
      for (const task of repo.tasks || []) {
        if (taskFilters.status && task.status !== taskFilters.status) continue;
        if (!matchesTaskSearch(task, repo.name)) continue;
        cards.push(taskCard(task, repo.name, byTask[task.task_id]));
        visible += 1;
      }
    }
    // No task matched: separate an empty inventory from an active-filter
    // no-match, and name the always-present Clear control as the recovery action.
    // Any missing-index cards stay above this note rather than being replaced.
    if (!visible)
      cards.push(
        total
          ? `<li class="center">No tasks match the current filters. Use Clear to reset.</li>`
          : `<li class="center">No tasks found.</li>`
      );
    document.getElementById("task-list").innerHTML = cards.join("");
    const countEl = document.getElementById("task-count");
    if (countEl)
      countEl.textContent = `Showing ${visible} of ${total} task${
        total === 1 ? "" : "s"
      }`;
  }

  async function renderTasks() {
    view().innerHTML = loadingView("Tasks", "Loading tasks…");
    const data = await api("/api/tasks");
    const repos = data.repos || [];
    // Best-effort run overlay so task cards show run-state accents here too; a
    // runs failure must not hide the task list (a 401 still re-gates).
    let liveByRepoTask = {};
    try {
      const runsData = await api("/api/runs");
      liveByRepoTask = liveRunsByRepoTask(runsData.runs);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
    }
    const repoNames = repos.map((r) => r.name);
    const statuses = orderedStatuses(repos);
    if (taskFilters.repo && !repoNames.includes(taskFilters.repo))
      taskFilters.repo = "";
    if (taskFilters.status && !statuses.includes(taskFilters.status))
      taskFilters.status = "";
    view().innerHTML = `
      ${pageTitle("Tasks")}
      <div class="filters" role="search" aria-label="Filter tasks">
        <label for="f-repo">Repo
          <select id="f-repo">${optionList(
            repoNames,
            taskFilters.repo,
            "All repos"
          )}</select>
        </label>
        <label for="f-status">Status
          <select id="f-status">${optionList(
            statuses,
            taskFilters.status,
            "All statuses"
          )}</select>
        </label>
        <label for="f-search">Search
          <input id="f-search" type="search" placeholder="Search tasks"
                 value="${escapeHtml(taskFilters.search)}" />
        </label>
        <button id="f-clear" class="filters-clear" type="button">Clear
          <span class="visually-hidden"> task filters</span></button>
      </div>
      <p id="task-count" class="task-count muted" aria-live="polite"></p>
      <ul id="task-list" class="list"></ul>`;
    document.getElementById("f-repo").addEventListener("change", (ev) => {
      taskFilters.repo = ev.target.value;
      renderTaskList(repos, liveByRepoTask);
    });
    document.getElementById("f-status").addEventListener("change", (ev) => {
      taskFilters.status = ev.target.value;
      renderTaskList(repos, liveByRepoTask);
    });
    document.getElementById("f-search").addEventListener("input", (ev) => {
      taskFilters.search = ev.target.value;
      renderTaskList(repos, liveByRepoTask);
    });
    // Clear resets every task filter and repaints the full inventory; the DOM
    // control values are reset explicitly since only the count/list repaint.
    document.getElementById("f-clear").addEventListener("click", () => {
      taskFilters.repo = "";
      taskFilters.status = "";
      taskFilters.search = "";
      document.getElementById("f-repo").value = "";
      document.getElementById("f-status").value = "";
      document.getElementById("f-search").value = "";
      renderTaskList(repos, liveByRepoTask);
    });
    renderTaskList(repos, liveByRepoTask);
  }

  // ---- repo detail ----

  // One recent-commit row: short hash, subject, and a formatted commit time.
  function commitRow(commit) {
    const when = formatCommitTime(commit.committed_ts);
    return `<li class="commit">
      <div class="commit-line">
        <code class="commit-hash">${escapeHtml(commit.short_hash)}</code>
        <span class="commit-subject">${escapeHtml(commit.subject)}</span>
      </div>
      ${when ? `<div class="commit-time muted">${escapeHtml(when)}</div>` : ""}
    </li>`;
  }

  // Recent commits at the bottom of repo detail. Best-effort: a null log (fetch
  // failed) or an `available: false` log (git unreadable) degrades to a graceful
  // "unavailable" note. `available` distinguishes an unreadable repo from a
  // readable one with no matching history ("no commits").
  function commitsSection(commitLog) {
    const head = `<section class="section"><h2>Recent commits</h2>`;
    const foot = `</section>`;
    if (!commitLog || !commitLog.available)
      return head + `<p class="muted">Commit history unavailable.</p>` + foot;
    const commits = commitLog.commits || [];
    if (!commits.length)
      return head + `<p class="muted">No commits found.</p>` + foot;
    return head + `<ul class="commits">${commits.map(commitRow).join("")}</ul>` + foot;
  }

  async function renderRepoDetail(repoName) {
    view().innerHTML = loadingView(repoName, "Loading " + repoName + "…");
    // Required: this repo's task inventory (single-repo envelope). An unknown
    // repo is a 404 the router surfaces as an error view.
    const data = await api("/api/repos/" + encodeURIComponent(repoName) + "/tasks");
    const entry = (data.repos || [])[0];
    if (!entry) {
      view().innerHTML =
        `<a class="back-link" href="#/repos">← Repos</a>` +
        pageTitle(repoName) +
        centerHtml("Repo not found.");
      return;
    }
    // Best-effort overlays: readiness (from the all-repos payload) and live
    // runs. Neither failing should blank out the task list; only a dropped
    // token (401) propagates to the login gate.
    let readiness = null;
    let repoPath = entry.path;
    let housekeepingPending = false;
    try {
      const reposData = await api("/api/repos");
      const match = (reposData.repos || []).find((r) => r.name === repoName);
      if (match) {
        readiness = match.readiness;
        repoPath = match.path;
        housekeepingPending = !!match.housekeeping_pending;
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
    }
    let liveByTask = {};
    try {
      const runsData = await api("/api/runs");
      liveByTask = liveRunsByTask(runsData.runs, repoName);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
    }
    // Best-effort recent commits for the bottom-of-detail section. A fetch
    // failure leaves `commitLog` null; the section renders "unavailable" rather
    // than hiding the repo or its tasks (a 401 still re-gates).
    let commitLog = null;
    try {
      commitLog = await api(
        "/api/repos/" + encodeURIComponent(repoName) + "/commits"
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
    }
    // Best-effort in-flight intake for this repo (the backend allows one at a
    // time). A fetch failure just falls back to offering a new intake.
    let inflightIntake = null;
    try {
      const intakesData = await api("/api/intakes");
      inflightIntake =
        (intakesData.intakes || []).find(
          (i) => i.repo_name === repoName && !INTAKE_TERMINAL.has(i.status)
        ) || null;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
    }

    const tasks = entry.tasks || [];
    const statuses = orderedStatusesFromTasks(tasks);
    if (repoDetailStatus && !statuses.includes(repoDetailStatus))
      repoDetailStatus = "";

    const readyBadge = readiness
      ? readiness.ready
        ? badge("ready", "ok")
        : badge("not ready", "bad")
      : "";
    const reasons =
      readiness && !readiness.ready
        ? `<ul class="reasons">${(readiness.checks || [])
            .map(
              (c) =>
                `<li><code>${escapeHtml(c.name)}</code> ${escapeHtml(
                  c.message
                )}</li>`
            )
            .join("")}</ul>`
        : "";

    view().innerHTML = `
      <a class="back-link" href="#/repos">← Repos</a>
      <div class="detail-head">
        ${pageTitle(repoName, "name big")}
        ${readyBadge}
      </div>
      <div class="path muted">${escapeHtml(repoPath || "")}</div>
      ${housekeepingPending ? housekeepingWarning(false) : ""}
      ${reasons}
      ${intakeSection(repoName, inflightIntake, readiness)}
      ${
        entry.index_present
          ? `<div class="filters"><label for="rd-status">Status
              <select id="rd-status">${optionList(
                statuses,
                repoDetailStatus,
                "All statuses"
              )}</select>
            </label></div>
            <ul id="repo-task-list" class="list"></ul>`
          : `<ul class="list">${missingIndexCard(repoName)}</ul>`
      }
      ${commitsSection(commitLog)}`;

    // Wire the intake-start form (present only when no in-flight intake and the
    // repo is not known-not-ready) before the missing-index early return, so it
    // works even for a repo whose task index is absent.
    wireIntakeStart();

    if (!entry.index_present) return;

    const paint = () => {
      const cards = tasks
        .filter((t) => !repoDetailStatus || t.status === repoDetailStatus)
        .map((t) => taskCard(t, repoName, liveByTask[t.task_id]));
      document.getElementById("repo-task-list").innerHTML = cards.length
        ? cards.join("")
        : `<li class="center">No tasks match this filter.</li>`;
    };
    document.getElementById("rd-status").addEventListener("change", (ev) => {
      repoDetailStatus = ev.target.value;
      paint();
    });
    paint();
  }

  // ---- per-session context strips (run detail + run history) ----
  //
  // The orchestrator logs each protocol session's latest context size; the hub
  // parses those into per-session summaries (role/kind, sid, turns, latest
  // context) exposed as a `sessions[]` list plus a soft `context_budget` — both
  // by GET /api/runs/{id}/sessions (a live run) and embedded per run in the
  // history payload (an ended run's persisted snapshot). A strip renders one
  // session compactly, e.g. `plan c05cfe3d… · 1 turn · context≈101k/200k`, with
  // a clamped meter. Every parsed string is escaped; a missing sid or context
  // degrades to a plain omission, never a broken strip. The budget is a display
  // reference, not a cap: context can exceed it, and an over-budget session is
  // flagged rather than hidden.

  // Compact a token count to "k": 101000 -> "101k", 0 -> "0k". Null/undefined
  // (a zero-turn session logs no context figure) -> null so the caller omits it.
  function fmtTokensK(n) {
    if (n === null || n === undefined) return null;
    return Math.round(n / 1000) + "k";
  }

  // Shorten a backend session id for display (first 8 chars + ellipsis, like the
  // `c05cfe3d…` strip example); a short id passes through unchanged.
  function shortSid(sid) {
    const s = String(sid);
    return s.length > 8 ? s.slice(0, 8) + "…" : s;
  }

  // A clamped context meter: fill width = context/budget, capped at 100% even
  // when context exceeds the soft budget (the real figure rides the strip text).
  // Over-budget adds a class for colour. Empty when there is no context or budget.
  function ctxMeter(tokens, budget) {
    if (tokens === null || tokens === undefined) return "";
    if (!budget || budget <= 0) return "";
    const pct = Math.max(0, Math.min(100, (tokens / budget) * 100));
    const over = tokens > budget ? " over" : "";
    return (
      `<span class="ctx-meter${over}" aria-hidden="true">` +
      `<span class="ctx-fill" style="width: ${pct.toFixed(1)}%"></span></span>`
    );
  }

  // One protocol session as a compact strip. `session` is a session summary
  // (role/kind/sid/turns/context_tokens/open); `budget` is the snapshot's
  // context_budget. A plan-gate is labelled "plan"; a normal session by its role.
  function sessionStrip(session, budget) {
    const label =
      session.kind === "plan-gate" ? "plan" : session.role || "session";
    const bits = [`<span class="ss-label">${escapeHtml(label)}</span>`];
    if (session.sid)
      bits.push(`<span class="ss-sid">${escapeHtml(shortSid(session.sid))}</span>`);
    const turns = session.turns || 0;
    bits.push(`${turns} ${turns === 1 ? "turn" : "turns"}`);
    const ctxK = fmtTokensK(session.context_tokens);
    if (ctxK) {
      const over = budget > 0 && session.context_tokens > budget;
      bits.push(
        `<span class="ss-ctx${over ? " over" : ""}">context≈${escapeHtml(
          ctxK
        )}/${escapeHtml(fmtTokensK(budget))}</span>`
      );
    }
    const meter = ctxK ? ctxMeter(session.context_tokens, budget) : "";
    return (
      `<li class="session-strip${session.open ? " open" : ""}">` +
      `<span class="ss-line">${bits.join(" · ")}</span>` +
      meter +
      `</li>`
    );
  }

  // The per-session context strips for a run, in log order (each plan-gate
  // before its dev session). Empty string when the run has no parsed/persisted
  // sessions (no log yet, or a pre-field run) so the caller can drop the heading.
  function sessionStrips(sessions, budget) {
    if (!sessions || !sessions.length) return "";
    return (
      `<ul class="session-strips">` +
      sessions.map((s) => sessionStrip(s, budget)).join("") +
      `</ul>`
    );
  }

  // ---- task detail ----
  // `sessions` is the live run's { sessions, context_budget } payload (run
  // detail is live-only; an ended run's strips live in run history), or null
  // when the best-effort fetch was skipped or failed — then no strip renders.
  function runStatusSection(run, sessions) {
    if (!run)
      return `<section class="section"><h2>Run</h2>
        <p class="muted">No run recorded for this task.</p></section>`;
    const rows = [["run id", run.run_id], ["started", run.started_ts || "—"]];
    if (run.ended_ts) rows.push(["ended", run.ended_ts]);
    if (run.exit_code !== null && run.exit_code !== undefined)
      rows.push(["exit code", String(run.exit_code)]);
    if (run.pending_questions)
      rows.push(["pending questions", String(run.pending_questions)]);
    const kv = rows
      .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`)
      .join("");
    const strips = sessions
      ? sessionStrips(sessions.sessions, sessions.context_budget)
      : "";
    return `<section class="section">
      <h2>Run ${runBadge(run.state)}</h2>
      <dl class="kv">${kv}</dl>
      ${strips ? `<div class="sessions"><h3>Sessions</h3>${strips}</div>` : ""}
      ${run.detail ? `<p class="muted">${escapeHtml(run.detail)}</p>` : ""}
      ${run.error ? `<p class="error-text">${escapeHtml(run.error)}</p>` : ""}
      <p><a class="text-link" href="${logsHash(run.run_id)}">View logs →</a></p>
    </section>`;
  }

  // The two buttons are kind-aware but map to the same two hub answer `mode`s:
  // `discuss` (a clarifying turn / plan feedback) and `answer` (a binding reply
  // / plan approval). A plan-gate confirmation loop (orchestrator README §5.8)
  // reads as Feedback/Confirm; every other escalation as Discuss/Answer. The hub
  // reconciles each mode with the question's kind and refuses a mismatch with a
  // 400 shown inline, so a mistyped marker/confirm can never silently land on
  // the wrong side of the orchestrator's parser — the button carries the intent.
  function questionForm(runId, q) {
    const prompt = q.banner || q.message || "";
    const planGate = q.kind === "plan-gate";
    const discussLabel = planGate ? "Feedback" : "Discuss";
    const answerLabel = planGate ? "Confirm" : "Answer";
    const placeholder = planGate
      ? "Leave empty to confirm, or type feedback"
      : "Type a reply";
    // Names both buttons and calls out the two special inputs: an empty box is a
    // one-tap plan approval (the hub fills the confirm word), and "stop" aborts
    // the run in any mode/kind.
    const hint = planGate
      ? `${answerLabel} approves the plan — an empty box is enough; ${discussLabel} sends plan changes. Type "stop" to end the run.`
      : `${answerLabel} sends a binding reply; ${discussLabel} asks a clarifying question first. Type "stop" to end the run.`;
    return `<div class="question">
      ${prompt ? `<p class="q-prompt">${escapeHtml(prompt)}</p>` : ""}
      ${
        q.error
          ? `<p class="error-text">question file problem: ${escapeHtml(
              q.error
            )}</p>`
          : ""
      }
      ${
        q.answer_error
          ? `<p class="error-text">a malformed answer file is present; sending will overwrite it (${escapeHtml(
              q.answer_error
            )})</p>`
          : ""
      }
      <form class="answer-form" data-run="${escapeHtml(runId)}" data-seq="${q.seq}">
        <textarea class="answer-input" rows="3"
          aria-label="Reply to question ${escapeHtml(q.seq)}"
          placeholder="${escapeHtml(placeholder)}"></textarea>
        <div class="answer-actions">
          <button type="button" class="answer-discuss" data-mode="discuss">${discussLabel}</button>
          <button type="button" class="answer-send" data-mode="answer"${
            planGate ? ' data-allow-empty="1"' : ""
          }>${answerLabel}</button>
          <span class="answer-status error-text" role="alert"></span>
        </div>
        <p class="answer-hint muted">${escapeHtml(hint)}</p>
      </form>
    </div>`;
  }

  function answerSection(runId, questions) {
    if (!questions || !questions.length) return "";
    return `<section class="section waiting">
      <h2>Waiting for an answer</h2>
      ${questions.map((q) => questionForm(runId, q)).join("")}
    </section>`;
  }

  function wireAnswerForms() {
    document.querySelectorAll(".answer-form").forEach((form) => {
      // Each button carries its own `mode`; a click sends that intent. The
      // buttons are type="button" (no implicit form submit), so an accidental
      // Enter can't fire a binding Answer/Confirm on a control surface.
      form.querySelectorAll("button[data-mode]").forEach((button) => {
        button.addEventListener("click", () => sendAnswer(form, button));
      });
    });
  }

  // Post one question's answer with the clicked button's explicit `mode`
  // (discuss / answer). Empty text is only allowed on the plan-gate Confirm
  // button (marked data-allow-empty — the hub fills the confirm word for a
  // one-tap approval); every other empty reply is blocked here to match the
  // server's non-empty refusal without a round-trip. A guard 400 (mode/kind
  // mismatch) rides the existing inline error path.
  async function sendAnswer(form, button) {
    const runId = form.dataset.run;
    const seq = form.dataset.seq;
    const mode = button.dataset.mode;
    const allowEmpty = button.dataset.allowEmpty === "1";
    const input = form.querySelector(".answer-input");
    const statusEl = form.querySelector(".answer-status");
    const buttons = form.querySelectorAll("button[data-mode]");
    statusEl.textContent = "";
    const answer = input.value.trim();
    if (!answer && !allowEmpty) {
      statusEl.textContent = "Enter a reply first.";
      input.focus(); // send the keyboard user to the field they must fill
      return;
    }
    buttons.forEach((b) => (b.disabled = true));
    try {
      await api(
        "/api/runs/" +
          encodeURIComponent(runId) +
          "/escalations/" +
          encodeURIComponent(seq) +
          "/answer",
        { method: "POST", body: { answer, responder: "dashboard", mode } }
      );
      router(); // re-render: the question is answered, the run leaves `waiting`
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      buttons.forEach((b) => (b.disabled = false));
      statusEl.textContent =
        err instanceof ApiError ? err.message : "Failed to send answer.";
      // Disabling the pressed button dropped focus to <body>; put it back so a
      // keyboard user can retry from where they were (the role="alert" status
      // has already announced why it failed).
      restoreFocus(button);
    }
  }

  // Re-focus a control that was disabled while an action was in flight. A
  // disabled element loses focus to <body>, so a failed action would otherwise
  // strand a keyboard user at the top of the document.
  function restoreFocus(el) {
    if (el && !el.disabled && typeof el.focus === "function") el.focus();
  }

  // ---- run controls (start / stop) ----

  // The one live run for this repo, if any. Per-repo serialization means at most
  // one, so this both surfaces the run to Stop and marks the repo "busy" for a
  // new Start (the backend enforces this; the dashboard only pre-reflects it).
  function repoLiveRun(runs, repoName) {
    return (
      (runs || []).find(
        (r) => r.repo_name === repoName && LIVE_STATES.has(r.state)
      ) || null
    );
  }

  // Why this task can't be launched right now (mirrors the run manager's task
  // gate), or null if it looks launchable. `blocked` is launchable with an
  // acknowledgement, so it is not a block here.
  function taskNotRunnableReason(status) {
    if (status === "missing") return "its task file is absent";
    if (status === "unknown") return "its task file has no parseable status";
    if (status === "blocked") return null; // runnable with acknowledgement
    if (!RUNNABLE_STATUSES.has(status))
      return "status " + status + " is not runnable";
    return null;
  }

  // Readiness failure reasons as <li> rows (shared shape with the repo views).
  function reasonItems(checks) {
    return (checks || [])
      .map(
        (c) =>
          `<li><code>${escapeHtml(c.name)}</code> ${escapeHtml(c.message)}</li>`
      )
      .join("");
  }

  // Turn a failed Start/Stop into a one-line message. A LaunchBlocked 409 rides
  // its `reasons` list; a blocked-task 409 nudges toward the acknowledgement;
  // otherwise the plain detail carries it (RepoBusy, RunEnded, …).
  function controlErrorText(err, fallback) {
    if (!(err instanceof ApiError)) return fallback;
    const body = err.body || {};
    if (Array.isArray(body.reasons) && body.reasons.length)
      return err.message + " — " + body.reasons.join("; ");
    if (body.acknowledge_required)
      return err.message + " — tick the acknowledgement box, then retry.";
    return err.message;
  }

  // Re-render a view after an action, routing a hard error to the error view (a
  // 401 already shows the login gate). Used by the intake control re-renders and
  // by the intake poll's settle. The repaint destroys the control that was
  // pressed, so it lands on the new view the same way a navigation does.
  function rerender(promise) {
    promise.then(landOnView).catch((err) => {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      renderError(err);
      landOnView();
    });
  }

  // Toggle a control button's busy state and clear its status line when busy.
  // Re-enabling also restores focus: disabling the pressed button dropped it to
  // <body>, so without this a failed action strands a keyboard user.
  function setControlBusy(button, statusEl, busy) {
    if (button) button.disabled = busy;
    if (statusEl && busy) {
      statusEl.textContent = "";
      statusEl.classList.remove("error-text");
    }
    if (!busy) restoreFocus(button);
  }

  // The Start/Stop control for a task, chosen from the run overlay the backend
  // already enforces. Order matters: a live run for THIS task offers Stop; a
  // live run elsewhere in the repo blocks Start (busy); otherwise Start is
  // offered only when the task status and (best-effort) readiness look launchable
  // — every hidden Start is still backed by the backend 409 if raced.
  function runControlsSection(repoName, detail, run, busyRun, readiness) {
    const head = `<section class="section run-controls"><h2>Run controls</h2>`;
    const foot = `</section>`;

    if (run && LIVE_STATES.has(run.state)) {
      // The stop control is driven by the server's run state, not client memory:
      // once stop_requested is set on the record, every render (a reload, another
      // device) shows the disabled "Stop requested" button until the run ends.
      // onStopSubmit still applies the same disabled+text transiently for
      // immediate feedback before the next fetch confirms it.
      const requested = !!run.stop_requested;
      const stopButton = requested
        ? `<button type="submit" class="btn-danger" disabled>Stop requested</button>`
        : `<button type="submit" class="btn-danger">Stop run</button>`;
      const stopNote = requested
        ? "Stop requested; the run ends at a boundary."
        : "";
      return (
        head +
        `<form class="stop-form" data-run="${escapeHtml(run.run_id)}">
          ${stopButton}
          <span class="control-status" role="status">${stopNote}</span>
        </form>
        <p class="muted">Stops at the next session boundary; a pending
          question's wait is aborted (README §5.5).</p>` +
        foot
      );
    }

    if (busyRun) {
      return (
        head +
        `<p class="muted">This repo already has a live run
          (<a class="text-link" href="${taskHash(
            repoName,
            busyRun.task_id
          )}">${escapeHtml(busyRun.task_id)}</a> ${runBadge(
          busyRun.state
        )}). Only one run per repo at a time.</p>` +
        foot
      );
    }

    const notRunnable = taskNotRunnableReason(detail.status);
    if (notRunnable) {
      return (
        head +
        `<p class="muted">This task can't be launched: ${escapeHtml(
          notRunnable
        )}.</p>` +
        foot
      );
    }

    if (readiness && !readiness.ready) {
      return (
        head +
        `<p class="muted">The repo is not ready to launch a run:</p>
         <ul class="reasons">${reasonItems(readiness.checks)}</ul>` +
        foot
      );
    }

    const blocked = detail.status === "blocked";
    const ack = blocked
      ? `<label class="ack"><input type="checkbox" class="ack-blocked" />
          <span>Acknowledge: resuming a <code>blocked</code> task only works if
          its <code>claimed-by</code> session is resumable, else the orchestrator
          exits with guidance (README §5.4).</span></label>`
      : "";
    // Start opens an options dialog rather than launching immediately. Every
    // dashboard run pins the backend to cc-codex (there is deliberately no
    // backend selector); the fields below are optional overrides for the
    // cc-codex run parameters. A blank field is omitted from the argv, so the
    // orchestrator keeps its own default — only a changed field adds an arg
    // (see buildExtraArgs). The blocked-task acknowledgement moves inside the
    // dialog, so it is confirmed together with the launch.
    // Each field carries an explicit for/id pair (only one options dialog is on
    // the page at a time, so fixed ids are safe) and the dialog is named by its
    // own heading through aria-labelledby, so the modal announces what it is.
    return (
      head +
      `<button type="button" class="open-run-options">Start run…</button>
      <dialog class="run-options" aria-labelledby="run-options-title">
        <form class="start-form" data-repo="${escapeHtml(
          repoName
        )}" data-task="${escapeHtml(detail.task_id)}" data-blocked="${
        blocked ? "1" : ""
      }">
          <h3 id="run-options-title">Start run</h3>
          <p class="muted opts-note">Backend is fixed to <code>cc-codex</code>.
            Leave a field blank to use the orchestrator default; only changed
            fields are passed.</p>
          <label for="opt-dev-agent">Dev agent
            <select id="opt-dev-agent" class="opt-dev-agent">
              <option value="">default</option>
              <option value="claude">claude</option>
              <option value="codex">codex</option>
            </select>
          </label>
          <label for="opt-dev-model">Dev model
            <input type="text" id="opt-dev-model" class="opt-dev-model"
              autocapitalize="off" autocomplete="off" spellcheck="false"
              placeholder="default" />
          </label>
          <label for="opt-dev-effort">Dev effort
            <input type="text" id="opt-dev-effort" class="opt-dev-effort"
              autocapitalize="off" autocomplete="off" spellcheck="false"
              placeholder="default" />
          </label>
          <label for="opt-review-model">Review model
            <input type="text" id="opt-review-model" class="opt-review-model"
              autocapitalize="off" autocomplete="off" spellcheck="false"
              placeholder="default" />
          </label>
          <label for="opt-review-effort">Review effort
            <input type="text" id="opt-review-effort" class="opt-review-effort"
              autocapitalize="off" autocomplete="off" spellcheck="false"
              placeholder="default" />
          </label>
          <label class="opt-toggle"><input type="checkbox" class="opt-plan-gate" />
            <span>Plan-gate: every dev session first proposes its goal and plan
            and blocks for confirmation before implementing (orchestrator
            default: off).</span></label>
          ${ack}
          <div class="control-actions">
            <button type="submit">Launch run</button>
            <button type="button" class="btn-secondary cancel-run-options">Cancel</button>
            <span class="control-status" role="status"></span>
          </div>
        </form>
      </dialog>` +
      foot
    );
  }

  function wireRunControls() {
    const openBtn = document.querySelector(".open-run-options");
    const dialog = document.querySelector(".run-options");
    if (openBtn && dialog) {
      openBtn.addEventListener("click", () => openRunOptions(dialog, openBtn));
      const cancel = dialog.querySelector(".cancel-run-options");
      if (cancel) cancel.addEventListener("click", () => closeRunOptions(dialog));
      // Escape must dismiss the dialog. A native modal does that itself (and
      // fires `close`); the no-showModal fallback below has no such behavior,
      // so handle the key here — closeRunOptions is idempotent either way.
      dialog.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape") closeRunOptions(dialog);
      });
      // `close` fires for every dismissal path (Cancel, Escape, a successful
      // launch), so focus restoration lives here once rather than per path.
      dialog.addEventListener("close", () => restoreFocus(dialogOpener));
    }
    const startForm = document.querySelector(".start-form");
    if (startForm) startForm.addEventListener("submit", onStartSubmit);
    const stopForm = document.querySelector(".stop-form");
    if (stopForm) stopForm.addEventListener("submit", onStopSubmit);
  }

  // The control that opened the current dialog, so closing it can hand focus
  // back instead of dropping the keyboard user at the top of the document.
  let dialogOpener = null;

  // Open the options dialog. Prefer the native modal (which scopes focus to the
  // dialog and handles Escape); fall back to a plain open attribute on the rare
  // browser without <dialog>.showModal so the form is still reachable — there,
  // focus is moved to the first field by hand.
  function openRunOptions(dialog, opener) {
    dialogOpener = opener || null;
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
      return;
    }
    dialog.setAttribute("open", "");
    const first = dialog.querySelector("select, input, textarea, button");
    if (first && typeof first.focus === "function") first.focus();
  }

  // Close it through whichever path opened it, then let the `close` handler
  // restore focus. Closing an already-closed dialog is a no-op.
  function closeRunOptions(dialog) {
    if (typeof dialog.close === "function") dialog.close();
    else {
      dialog.removeAttribute("open");
      restoreFocus(dialogOpener);
    }
  }

  // The launch overrides, each as [orchestrator flag, field class]. Order fixes
  // the argv order; every field is optional.
  const RUN_OPTION_FIELDS = [
    ["--dev-agent", "opt-dev-agent"],
    ["--dev-model", "opt-dev-model"],
    ["--dev-effort", "opt-dev-effort"],
    ["--review-model", "opt-review-model"],
    ["--review-effort", "opt-review-effort"],
  ];

  // Build the orchestrator extra args for a Start. The backend is always pinned
  // to cc-codex (never selectable from the dashboard); each override adds its
  // flag only when the user supplied a value, so a blank field defers to the
  // orchestrator default and never reaches the argv. Plan-gate is the one
  // valueless toggle and is appended last (see below).
  function buildExtraArgs(form) {
    const args = ["--backend", "cc-codex"];
    for (const [flag, cls] of RUN_OPTION_FIELDS) {
      const field = form.querySelector("." + cls);
      const value = field ? field.value.trim() : "";
      if (value) args.push(flag, value);
    }
    // Plan-gate is a valueless store_true flag, not a [flag, value] override, so
    // it can't ride the RUN_OPTION_FIELDS loop. Append a bare "--plan-gate" only
    // when checked; unchecked omits it so the orchestrator keeps its default
    // (off). Pinned to the argv tail, after the overrides, for a deterministic
    // order (…, "--plan-gate").
    const planGate = form.querySelector(".opt-plan-gate");
    if (planGate && planGate.checked) args.push("--plan-gate");
    return args;
  }

  // Start: POST /api/runs with the fixed cc-codex backend plus any overrides. A
  // success closes the dialog and re-renders (the run is now live, so the view
  // flips to Stop); a refusal stays in the open dialog — the backend, not this
  // form, decides whether the launch is allowed.
  async function onStartSubmit(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    const dialog = form.closest("dialog");
    const statusEl = form.querySelector(".control-status");
    const button = form.querySelector('button[type="submit"]');
    const ackBox = form.querySelector(".ack-blocked");
    const acknowledgeBlocked =
      form.dataset.blocked === "1" ? !!(ackBox && ackBox.checked) : false;
    statusEl.textContent = "";
    statusEl.classList.remove("error-text");
    button.disabled = true;
    try {
      await api("/api/runs", {
        method: "POST",
        body: {
          repo_id: form.dataset.repo,
          task_id: form.dataset.task,
          extra_args: buildExtraArgs(form),
          acknowledge_blocked: acknowledgeBlocked,
        },
      });
      if (dialog && dialog.open) closeRunOptions(dialog);
      router(); // now shows the live run + Stop control
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      button.disabled = false;
      statusEl.classList.add("error-text");
      statusEl.textContent = controlErrorText(err, "Failed to start the run.");
      restoreFocus(button); // refusal keeps the dialog open on its submit button
    }
  }

  // Stop: POST /api/runs/{id}/stop. The stop is graceful, so the run stays live
  // until its next session boundary; rather than re-render to the same state, we
  // keep the button disabled and show the caveat the API returns.
  async function onStopSubmit(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    const runId = form.dataset.run;
    const statusEl = form.querySelector(".control-status");
    const button = form.querySelector("button");
    statusEl.textContent = "";
    statusEl.classList.remove("error-text");
    button.disabled = true;
    try {
      const res = await api(
        "/api/runs/" + encodeURIComponent(runId) + "/stop",
        { method: "POST" }
      );
      button.textContent = "Stop requested";
      statusEl.textContent =
        (res && res.detail) || "Stop requested; the run ends at a boundary.";
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      button.disabled = false;
      statusEl.classList.add("error-text");
      statusEl.textContent = controlErrorText(err, "Failed to request stop.");
      restoreFocus(button);
    }
  }

  async function renderTaskDetail(repoName, taskId) {
    view().innerHTML = loadingView(taskId, "Loading task…");
    // Required: the task's inventory fields + raw file body. Unknown repo/task
    // is a 404 the router surfaces as an error view.
    const detail = await api(
      "/api/repos/" +
        encodeURIComponent(repoName) +
        "/tasks/" +
        encodeURIComponent(taskId)
    );

    // Best-effort run overlay; a runs failure must not hide the task file.
    // `busyRun` is any live run in this repo — it decides whether Start is
    // offered (and, when it is this task's run, whether Stop is).
    let run = null;
    let busyRun = null;
    try {
      const runsData = await api("/api/runs");
      run = pickRunForTask(runsData.runs, repoName, taskId);
      busyRun = repoLiveRun(runsData.runs, repoName);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
    }

    // Best-effort readiness so the Start control can pre-explain a not-ready
    // repo; the backend re-checks readiness on launch and stays authoritative.
    let readiness = null;
    try {
      const reposData = await api("/api/repos");
      const match = (reposData.repos || []).find((r) => r.name === repoName);
      if (match) readiness = match.readiness;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
    }

    // Best-effort per-session context for a LIVE run only (an ended run gets its
    // strips in run history, so run detail stays live-only). A sessions failure
    // must never hide the task file, so it degrades to no strip.
    let sessions = null;
    if (run && LIVE_STATES.has(run.state)) {
      try {
        sessions = await api(
          "/api/runs/" + encodeURIComponent(run.run_id) + "/sessions"
        );
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) throw err;
      }
    }

    // Only a live, waiting run can have a pending question to answer.
    let questions = [];
    if (run && run.state === "waiting" && run.pending_questions) {
      try {
        const esc = await api("/api/escalations");
        questions = (esc.escalations || [])
          .filter((e) => e.run_id === run.run_id)
          .map((e) => e.question);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) throw err;
      }
    }

    const blockers =
      detail.blockers && detail.blockers.length
        ? `<div class="blockers muted">blockers: ${detail.blockers
            .map(escapeHtml)
            .join(", ")}</div>`
        : "";
    const unknownNote =
      detail.status === "unknown"
        ? `<p class="error-text">task file problem: ${escapeHtml(
            taskProblemText("unknown")
          )}</p>`
        : "";
    const contentSection = `<section class="section"><h2>Task file</h2>${
      detail.present
        ? taskContentBlock(detail.content, detail.task_id)
        : `<p class="muted">The task file is absent.</p>`
    }</section>`;

    view().innerHTML = `
      <a class="back-link" href="${repoHash(repoName)}">← ${escapeHtml(
        repoName
      )}</a>
      <div class="detail-head">
        ${pageTitle(detail.task_id, "task-id big")}
        ${badge(detail.status, statusBadgeKind(detail.status))}
      </div>
      ${detail.summary ? `<p class="summary">${escapeHtml(detail.summary)}</p>` : ""}
      ${blockers}
      ${unknownNote}
      ${runStatusSection(run, sessions)}
      ${runControlsSection(repoName, detail, run, busyRun, readiness)}
      ${answerSection(run ? run.run_id : "", questions)}
      ${contentSection}`;

    wireAnswerForms();
    wireRunControls();
  }

  // ---- run history ----
  //
  // Runs are grouped client-side by their (repo, task) so a completed or
  // archived task's whole run arc reads as one unit: a header, an est-vs-actual
  // rollup (dev sessions used vs the frontmatter session-est, peak per-session
  // context, summed wall-clock), then each run's card with its context strips.
  // The flat /api/history/runs list is already newest-first and embeds, per run,
  // the persisted session snapshot and the task's typed session_est.

  function runMetaKv(run) {
    const rows = [];
    if (run.started_ts) rows.push(["started", run.started_ts]);
    if (run.ended_ts) rows.push(["ended", run.ended_ts]);
    if (run.exit_code !== null && run.exit_code !== undefined)
      rows.push(["exit code", String(run.exit_code)]);
    if (run.pending_questions)
      rows.push(["pending questions", String(run.pending_questions)]);
    return rows
      .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`)
      .join("");
  }

  // The task's session_est (the backend stamps the same value on every run of a
  // task, reading it once). First non-null wins; null when every run's est is
  // unknown (missing/malformed frontmatter).
  function firstEst(runs) {
    for (const run of runs) if (run.session_est) return run.session_est;
    return null;
  }

  // "Actual" dev sessions observed across a task's runs: role "dev", kind
  // "session". Plan-gates (kind "plan-gate") and review sessions are excluded
  // because frontmatter session-est counts dev sessions only, so this is the
  // faithful mate of the est total. It counts sessions seen in persisted run
  // history, so pruned run dirs undercount (an audit-of-persisted-runs view);
  // a live run contributes nothing until its snapshot is persisted at end.
  function countDevSessions(runs) {
    let n = 0;
    for (const run of runs)
      for (const s of run.sessions || [])
        if (s.role === "dev" && s.kind === "session") n++;
    return n;
  }

  // The peak single-session context across a task's runs (each session's
  // context_tokens is already that conversation's monotonic peak). Null when no
  // session reported a context figure.
  function peakContext(runs) {
    let peak = null;
    for (const run of runs)
      for (const s of run.sessions || [])
        if (
          s.context_tokens != null &&
          (peak === null || s.context_tokens > peak)
        )
          peak = s.context_tokens;
    return peak;
  }

  // Summed wall-clock of a task's runs, in seconds, from the run-record ISO
  // started_ts/ended_ts — never the log's HH:MM:SS (no date, wraps past
  // midnight). A run with no ended_ts (live, or an end the hub never observed)
  // or an unparseable/negative span contributes nothing. Only the duration is
  // derived, so naive local parsing is safe: the zone offset cancels in the
  // subtraction.
  function sumWallClockSecs(runs) {
    let secs = 0;
    for (const run of runs) {
      if (!run.started_ts || !run.ended_ts) continue;
      const start = new Date(run.started_ts).getTime();
      const end = new Date(run.ended_ts).getTime();
      if (isNaN(start) || isNaN(end) || end < start) continue;
      secs += (end - start) / 1000;
    }
    return secs;
  }

  // A compact duration: "45s", "5m", "2h 5m", "3h". Zero/negative -> "0s".
  function fmtDuration(secs) {
    secs = Math.max(0, Math.round(secs));
    if (secs < 60) return secs + "s";
    const mins = Math.floor(secs / 60);
    if (mins < 60) return mins + "m";
    const hrs = Math.floor(mins / 60);
    const rem = mins % 60;
    return rem ? `${hrs}h ${rem}m` : `${hrs}h`;
  }

  // A DOM-attribute-safe, collision-free group identity: repo_path (the est
  // dedup key) with a repo_name fallback, plus task_id, each percent-encoded and
  // joined by a literal "|". Encoding keeps the join injective (a raw "|" inside
  // either part becomes "%7C") and the result plain ASCII, so it doubles as the
  // `data-group-key` the collapse toggle reads back — the internal Map below can
  // afford a raw-NUL separator, but an HTML attribute can't (the parser rewrites
  // U+0000 to U+FFFD). All runs in a group share (repo, task), so the group key
  // is groupKey(any one of its runs).
  function groupKey(run) {
    return (
      encodeURIComponent(run.repo_path || run.repo_name || "") +
      "|" +
      encodeURIComponent(run.task_id || "")
    );
  }

  // Group the flat history list by (repo, task), preserving newest-first order:
  // the list is already newest-first, so a group's first appearance — and each
  // run within it — stays newest-first. Keyed on repo_path (the backend's est
  // dedup key) with a repo_name fallback, joined to task_id by a NUL that can't
  // occur in either.
  function groupRunsByTask(runs) {
    const groups = new Map();
    for (const run of runs) {
      const key =
        (run.repo_path || run.repo_name || "") + "\u0000" + (run.task_id || "");
      let group = groups.get(key);
      if (!group) {
        group = { repo_name: run.repo_name, task_id: run.task_id, runs: [] };
        groups.set(key, group);
      }
      group.runs.push(run);
    }
    return [...groups.values()];
  }

  // The est-vs-actual rollup for one task group: dev sessions used against the
  // frontmatter session-est total (a clamped meter, over-estimate flagged),
  // peak per-session context, and summed wall-clock. Est unknown -> the count
  // shows with an "est unknown" note and no meter.
  function taskRollup(group) {
    const runs = group.runs;
    const est = firstEst(runs);
    const actual = countDevSessions(runs);
    const total = est ? est.total : null;
    const bits = [];
    if (total !== null) {
      const over = actual > total;
      bits.push(
        `<span class="roll-metric${over ? " over" : ""}">${actual}/${total} ` +
          `dev session${total === 1 ? "" : "s"}</span>`
      );
    } else {
      bits.push(
        `<span class="roll-metric">${actual} dev session${
          actual === 1 ? "" : "s"
        } · est unknown</span>`
      );
    }
    const peakK = fmtTokensK(peakContext(runs));
    if (peakK)
      bits.push(
        `<span class="roll-metric">peak context≈${escapeHtml(peakK)}</span>`
      );
    const wall = sumWallClockSecs(runs);
    if (wall > 0)
      bits.push(
        `<span class="roll-metric">${escapeHtml(
          fmtDuration(wall)
        )} wall-clock</span>`
      );
    const meter = total !== null ? ctxMeter(actual, total) : "";
    return (
      `<div class="task-rollup">` +
      `<div class="roll-line">${bits.join(" · ")}</div>` +
      meter +
      `</div>`
    );
  }

  function historyRunCard(run) {
    const kv = runMetaKv(run);
    // Each entry embeds its persisted per-session snapshot (sessions[] +
    // context_budget), so the card renders context strips without a per-run
    // fetch; an ended run with no parsed sessions renders none. The run's
    // (repo, task) is carried by the enclosing group header, not repeated here.
    const strips = sessionStrips(run.sessions, run.context_budget);
    return `
      <li class="card run">
        <a class="card-link" href="${logsHash(run.run_id)}">
          <div class="card-head">
            <span class="run-id">${escapeHtml(run.run_id)}</span>
            <span class="badges">${runBadge(run.state)}</span>
          </div>
          ${kv ? `<dl class="kv">${kv}</dl>` : ""}
          ${strips}
        </a>
      </li>`;
  }

  // One task group: a collapse-toggle header (task id + repo + run count), the
  // est-vs-actual rollup, then the group's run cards newest-first via the shared
  // card builder. The header is a real <button> (aria-expanded) so collapse is
  // keyboard-operable and screen-reader-legible; a collapsed group keeps its
  // header + rollup summary and only hides the run-card list — via the
  // `.collapsed` CSS, not by dropping the cards, and with no effect on ordering
  // or the rollup. Collapsed state is keyed by the stable group key so it
  // survives a filter re-render; the key rides `data-group-key` for the toggle.
  function taskGroupCard(group) {
    const runCount = group.runs.length;
    const key = groupKey(group.runs[0]); // group is non-empty; all runs share it
    const collapsed = collapsedGroups.has(key);
    const cards = group.runs.map(historyRunCard).join("");
    return (
      `<section class="task-group${collapsed ? " collapsed" : ""}" ` +
      `data-group-key="${escapeHtml(key)}">` +
      `<button class="task-group-head" type="button" aria-expanded="${
        collapsed ? "false" : "true"
      }">` +
      `<span class="tg-caret" aria-hidden="true"></span>` +
      `<span class="tg-task">${escapeHtml(
        group.task_id || "(unknown task)"
      )}</span>` +
      `<span class="tg-meta muted">${escapeHtml(
        group.repo_name || ""
      )} · ${runCount} run${runCount === 1 ? "" : "s"}</span>` +
      `</button>` +
      taskRollup(group) +
      `<ul class="list">${cards}</ul>` +
      `</section>`
    );
  }

  // ---- run-history navigation: compose-over-payload filters + collapse ----
  //
  // Discovery controls layered on the same /api/history/runs payload — no API
  // change and no refetch on change: the flat list is fetched once per view
  // entry and re-filtered client-side, mirroring the task-list filters. Filters
  // compose as AND — repo (exact name), run state (exact), and a case-insensitive
  // task-id substring. Filtering the flat, newest-first list *before* grouping
  // keeps ordering intact and lets the per-group rollup and the count line
  // describe exactly the runs shown. Held in the closure and applied client-side,
  // so they persist across view re-entry like the task filters.
  const runFilters = { repo: "", state: "", task: "" };
  // Collapsed groups persist across filter re-renders (and view re-entry), keyed
  // by the stable group key; a stale key for a group no longer present is inert.
  const collapsedGroups = new Set();

  // Canonical run-state order for the state dropdown (live states first, then end
  // classifications); any unknown state (forward compat) sorts after, mirroring
  // the task-status ordering helper.
  const RUN_STATE_ORDER = [
    "active",
    "waiting",
    "completed",
    "stopped",
    "error",
    "ended",
  ];
  function orderedRunStates(runs) {
    const seen = new Set();
    for (const run of runs) if (run.state) seen.add(run.state);
    const known = RUN_STATE_ORDER.filter((s) => seen.has(s));
    const extra = [...seen].filter((s) => !RUN_STATE_ORDER.includes(s)).sort();
    return [...known, ...extra];
  }

  // Distinct repo names present in the history payload, alpha-sorted for a stable
  // dropdown (history rows have no inherent repo order).
  function historyRepoNames(runs) {
    const seen = new Set();
    for (const run of runs) if (run.repo_name) seen.add(run.repo_name);
    return [...seen].sort();
  }

  // AND across the three filters; an empty filter is a wildcard. The task filter
  // is a trimmed, case-insensitive substring on task_id; repo and state are exact.
  function matchesRunFilters(run) {
    if (runFilters.repo && run.repo_name !== runFilters.repo) return false;
    if (runFilters.state && run.state !== runFilters.state) return false;
    const q = runFilters.task.trim().toLowerCase();
    if (q && !String(run.task_id || "").toLowerCase().includes(q)) return false;
    return true;
  }

  // Toggle one group's collapsed state from a delegated header click. Run cards
  // are <a> links inside the same section, so a card tap never resolves to the
  // header button; a click outside any header is ignored.
  function onRunGroupClick(ev) {
    const head = ev.target.closest(".task-group-head");
    if (!head) return;
    const section = head.closest(".task-group");
    if (!section) return;
    const key = section.getAttribute("data-group-key");
    const collapsed = section.classList.toggle("collapsed");
    head.setAttribute("aria-expanded", collapsed ? "false" : "true");
    if (collapsed) collapsedGroups.add(key);
    else collapsedGroups.delete(key);
  }

  // Partial re-render: filter the fetched runs, regroup, and repaint only the
  // count line + group list (the filter controls stay put, keeping focus/caret).
  // Newest-first survives (filter preserves order; grouping keeps first-seen).
  // Empty *history* is handled upstream in renderRuns; an empty result here means
  // the filters excluded everything — a distinct no-match state.
  function renderRunGroups(allRuns) {
    const groupsEl = document.getElementById("run-groups");
    const countEl = document.getElementById("run-count");
    const filtered = allRuns.filter(matchesRunFilters);
    const groups = groupRunsByTask(filtered);
    if (!groups.length) {
      countEl.textContent = "";
      groupsEl.innerHTML = `<p class="center">No runs match these filters.</p>`;
      return;
    }
    const g = groups.length;
    const r = filtered.length;
    countEl.textContent = `${g} group${g === 1 ? "" : "s"} · ${r} run${
      r === 1 ? "" : "s"
    }`;
    groupsEl.innerHTML = groups.map(taskGroupCard).join("");
  }

  // Run history: recent runs newest-first from the history endpoint. Fetched
  // once, then filtered/grouped client-side into collapsible per-task cards, each
  // run tapping through to its log tail. Empty payload and no-match are distinct
  // states; a clear-filters button restores the full newest-first set.
  async function renderRuns() {
    view().innerHTML = loadingView("Run history", "Loading runs…");
    const data = await api("/api/history/runs");
    const runs = data.runs || [];
    if (!runs.length) {
      view().innerHTML =
        pageTitle("Run history") + centerHtml("No runs recorded yet.");
      return;
    }
    const repoNames = historyRepoNames(runs);
    const states = orderedRunStates(runs);
    // Drop a persisted filter the current payload can no longer satisfy, so a
    // stale value never yields a permanent phantom no-match (mirrors the task
    // filters). The free-text task filter is left as typed.
    if (runFilters.repo && !repoNames.includes(runFilters.repo))
      runFilters.repo = "";
    if (runFilters.state && !states.includes(runFilters.state))
      runFilters.state = "";
    view().innerHTML = `
      ${pageTitle("Run history")}
      <div class="filters" role="search" aria-label="Filter runs">
        <label for="rf-repo">Repo
          <select id="rf-repo">${optionList(
            repoNames,
            runFilters.repo,
            "All repos"
          )}</select>
        </label>
        <label for="rf-state">State
          <select id="rf-state">${optionList(
            states,
            runFilters.state,
            "All states"
          )}</select>
        </label>
        <label for="rf-task">Task
          <input id="rf-task" type="search" placeholder="Filter by task id"
                 value="${escapeHtml(runFilters.task)}" />
        </label>
        <button id="rf-clear" class="filters-clear" type="button">Clear
          <span class="visually-hidden"> run filters</span></button>
      </div>
      <p id="run-count" class="run-count muted" aria-live="polite"></p>
      <div id="run-groups"></div>`;
    document.getElementById("rf-repo").addEventListener("change", (ev) => {
      runFilters.repo = ev.target.value;
      renderRunGroups(runs);
    });
    document.getElementById("rf-state").addEventListener("change", (ev) => {
      runFilters.state = ev.target.value;
      renderRunGroups(runs);
    });
    document.getElementById("rf-task").addEventListener("input", (ev) => {
      runFilters.task = ev.target.value;
      renderRunGroups(runs);
    });
    document.getElementById("rf-clear").addEventListener("click", () => {
      runFilters.repo = "";
      runFilters.state = "";
      runFilters.task = "";
      document.getElementById("rf-repo").value = "";
      document.getElementById("rf-state").value = "";
      document.getElementById("rf-task").value = "";
      renderRunGroups(runs);
    });
    // One delegated listener on the stable group container survives the partial
    // re-renders above (they replace its innerHTML, not the node itself).
    document
      .getElementById("run-groups")
      .addEventListener("click", onRunGroupClick);
    renderRunGroups(runs);
  }

  // ---- run-scoped task view (active or archived) ----
  // The task file associated with a run, read through the run record via
  // GET /api/runs/{run_id}/task, so it stays reachable after close-out archives
  // the task (the Tasks page stays active-only). Reached from the run/audit
  // surfaces — e.g. the log view's task back-link — and shows the file body with
  // an active/archived source badge; an absent file renders a muted note.
  async function renderRunTask(runId) {
    view().innerHTML = loadingView("Task file", "Loading task…");
    const data = await api("/api/runs/" + encodeURIComponent(runId) + "/task"); // unknown run → 404
    const taskId = data.task_id || runId;
    const sourceBadge = data.source ? badge(data.source) : "";
    const body = data.present
      ? taskContentBlock(data.content, taskId)
      : `<p class="muted">The task file for this run is not available
         (neither active nor archived).</p>`;
    view().innerHTML = `
      <a class="back-link" href="#/runs">← Runs</a>
      <div class="detail-head">
        ${pageTitle(taskId, "task-id big")}
        ${sourceBadge}
      </div>
      <p><a class="text-link" href="${logsHash(runId)}">View logs →</a></p>
      <section class="section"><h2>Task file</h2>${body}</section>`;
  }

  // ---- remote intake (draft → review → confirm / cancel) ----
  //
  // Repo-scoped: the hub runs a read-only /intake-task session that drafts a
  // complete task for the human to review; only an explicit Confirm writes the
  // task file + index row. The dashboard drives the shipped intake HTTP API and
  // never touches the target repo itself. On confirmation it routes to the new
  // task's detail, where the existing Start control launches it — intake does
  // not duplicate run start/stop.

  // The "new task intake" entry on repo detail. If the repo already has an
  // in-flight intake (the backend allows one at a time), link to its review
  // instead of offering a second. A known-not-ready repo pre-explains the block
  // the backend would 409 on; the submit handler still renders a raced 409.
  function intakeSection(repoName, inflight, readiness) {
    const head = `<section class="section intake"><h2>New task intake</h2>`;
    const foot = `</section>`;
    if (inflight) {
      return (
        head +
        `<p class="muted">A task intake is in progress
          ${badge(inflight.status, intakeBadgeKind(inflight.status))}.</p>
         <p><a class="text-link" href="${intakeHash(
           inflight.intake_id
         )}">Review the draft →</a></p>` +
        foot
      );
    }
    if (readiness && !readiness.ready) {
      return (
        head +
        `<p class="muted">The repo is not ready, so a task intake can't start:</p>
         <ul class="reasons">${reasonItems(readiness.checks)}</ul>` +
        foot
      );
    }
    return (
      head +
      `<p class="muted">Describe a new task in plain language. The hub drafts the
        full task file for you to review; nothing is written until you confirm.</p>
       <form class="intake-form" data-repo="${escapeHtml(repoName)}">
         <label class="visually-hidden" for="intake-request">Describe the new
           task</label>
         <textarea id="intake-request" class="intake-request answer-input" rows="3"
           placeholder="e.g. Add a CSV export button to the run history view"></textarea>
         <div class="control-actions">
           <button type="submit">Draft task…</button>
           <span class="control-status" role="status"></span>
         </div>
       </form>` +
      foot
    );
  }

  function wireIntakeStart() {
    const form = document.querySelector(".intake-form");
    if (form) form.addEventListener("submit", onIntakeStart);
  }

  // Start: POST /api/intakes {repo_id, request} → route to the review view. A
  // 409 with an `intake_id` means the repo already has an in-flight intake (it
  // raced our best-effort check) — link straight to it rather than dead-ending.
  // Other refusals (empty request, not ready) render in place.
  async function onIntakeStart(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    const input = form.querySelector(".intake-request");
    const statusEl = form.querySelector(".control-status");
    const button = form.querySelector("button");
    if (statusEl) {
      statusEl.textContent = "";
      statusEl.classList.remove("error-text");
    }
    const request = input ? input.value.trim() : "";
    if (!request) {
      if (statusEl) statusEl.textContent = "Describe the task first.";
      if (input) input.focus();
      return;
    }
    if (button) button.disabled = true;
    try {
      const rec = await api("/api/intakes", {
        method: "POST",
        body: { repo_id: form.dataset.repo, request },
      });
      location.hash = intakeHash(rec.intake_id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      if (button) button.disabled = false;
      const existing = err instanceof ApiError && err.body && err.body.intake_id;
      if (existing) {
        location.hash = intakeHash(existing);
        return;
      }
      if (statusEl) {
        statusEl.classList.add("error-text");
        statusEl.textContent = controlErrorText(err, "Failed to start intake.");
      }
      restoreFocus(button);
    }
  }

  // The draft the human reviews: the complete proposed task file and the index
  // row confirmation would write, plus any agent notes (duplicate overlap,
  // oversized-scope warnings). Shown verbatim in read-only blocks — it is a
  // proposal, never a task, until confirmed.
  function intakeDraftView(draft) {
    if (!draft) return "";
    const notes = draft.notes
      ? `<div class="intake-notes">${escapeHtml(draft.notes)}</div>`
      : "";
    return `
      <section class="section">
        <h2>Proposed task</h2>
        ${draft.summary ? `<p class="summary">${escapeHtml(draft.summary)}</p>` : ""}
        ${notes}
        <h3 class="intake-sub">Task file
          <code>.ai-tasks/${escapeHtml(draft.task_id)}.md</code></h3>
        ${taskContentBlock(draft.task_file, "Proposed task file")}
        <h3 class="intake-sub">Index entry <code>.ai-tasks/index.md</code></h3>
        ${taskContentBlock(draft.index_entry, "Proposed index entry")}
      </section>`;
  }

  // The review controls for an awaiting_review intake. Confirm writes the task
  // (the only write); Request changes sends feedback back into the same session
  // for a fresh draft (still read-only); Cancel discards it (writes nothing).
  function intakeReviewControls() {
    return `
      <section class="section intake">
        <h2>Review</h2>
        <div class="intake-actions">
          <button type="button" class="intake-confirm">Confirm &amp; create task</button>
          <button type="button" class="btn-danger intake-cancel">Cancel</button>
          <span class="control-status intake-status" role="status"></span>
        </div>
        <form class="intake-revise-form">
          <label class="intake-revise-label" for="intake-feedback">Request changes
            <textarea id="intake-feedback" class="intake-feedback answer-input"
              rows="3"
              placeholder="Describe what to change; the draft is revised in the same session"></textarea>
          </label>
          <div class="control-actions">
            <button type="submit">Send changes</button>
            <span class="control-status intake-revise-status" role="status"></span>
          </div>
        </form>
      </section>`;
  }

  // Metadata-only audit trail: creation, each revision, confirmation,
  // cancellation, failure. Never carries a draft body, so history stays
  // reconstructable without treating an unconfirmed draft as a task.
  function intakeAuditSection(events) {
    if (!events || !events.length) return "";
    const rows = events
      .map((e) => {
        const meta = [
          typeof e.iteration === "number" ? "iteration " + e.iteration : "",
          e.detail || "",
        ]
          .filter(Boolean)
          .map(escapeHtml)
          .join(" · ");
        return `<li class="intake-event">
          <div class="intake-event-head">
            <span class="intake-event-kind">${escapeHtml(e.kind)}</span>
            ${e.ts ? `<span class="muted">${escapeHtml(e.ts)}</span>` : ""}
          </div>
          ${meta ? `<div class="muted">${meta}</div>` : ""}
        </li>`;
      })
      .join("");
    return `<section class="section"><h2>Intake history</h2>
      <ul class="intake-events list">${rows}</ul></section>`;
  }

  // ---- in-flight intake polling ----
  //
  // An intake agent turn runs off the request lock on the hub (task
  // 2026-07-12-async-intake-turns), so a mutating route returns a transient
  // record at once and this view polls GET /api/intakes/{id} until it settles.
  // The teardown discipline mirrors the SSE log stream's activeStream: one
  // module-level handle (activeIntakePoll), cleared by router() on navigation
  // and at the top of renderIntakeReview, with a staleness guard before every
  // paint/reschedule so a poll that outlived its view — or a slow response
  // landing after the view was left — can neither paint nor reschedule.

  // Elapsed since an intake's last status change (its updated_ts, a tz-aware UTC
  // isoformat stamped when the transient status was persisted). Compact, and
  // degrades to "" on a null/unparseable/future stamp so the panel shows just
  // the verb rather than "NaN" (mirrors formatCommitTime's guard).
  function intakeElapsed(updatedTs) {
    if (!updatedTs) return "";
    const then = new Date(updatedTs);
    if (isNaN(then.getTime())) return "";
    const secs = Math.floor((Date.now() - then.getTime()) / 1000);
    if (secs < 0) return "";
    if (secs < 60) return secs + "s";
    const mins = Math.floor(secs / 60);
    if (mins < 60) return mins + "m " + (secs % 60) + "s";
    const hrs = Math.floor(mins / 60);
    return hrs + "h " + (mins % 60) + "m";
  }

  // The elapsed phrase for the working panel's #intake-elapsed span, refreshed in
  // place each poll tick; empty (no phrase) when the stamp can't be read.
  function intakeElapsedPhrase(updatedTs) {
    const e = intakeElapsed(updatedTs);
    return e ? " — running " + e : "";
  }

  function updateIntakeElapsed(updatedTs) {
    const el = document.getElementById("intake-elapsed");
    if (el) el.textContent = intakeElapsedPhrase(updatedTs);
  }

  // The active-turn verb for the working panel's status line (the status itself
  // also rides the detail-head badge). Fixed for the panel's lifetime — a status
  // change re-renders — so only the elapsed span ticks.
  function intakeWorkingVerb(status) {
    if (status === "confirming") return "Creating the task";
    if (status === "revising") return "Revising the draft";
    return "Drafting the task"; // drafting
  }

  // Hand a confirmed intake off to its created task's detail, where the existing
  // Start control launches it — intake never duplicates run start/stop. Shared by
  // the synchronous confirm return and the async confirming→confirmed poll settle
  // so either path ends in the same place.
  function handoffToCreatedTask(rec) {
    location.hash = taskHash(rec.repo_name, rec.created_task_id);
  }

  function stopIntakePoll() {
    if (activeIntakePoll) {
      activeIntakePoll.cancelled = true;
      if (activeIntakePoll.timer) clearTimeout(activeIntakePoll.timer);
      activeIntakePoll = null;
    }
  }

  // Poll one in-flight intake (started only for a transient status) until it
  // settles. A chained setTimeout — never setInterval — so a slow response can't
  // overlap the next fetch; while the status is unchanged it just refreshes the
  // elapsed label in place; on a change it hands off (confirming→confirmed) or
  // re-renders the review, which restarts the poll only if still transient.
  function startIntakePoll(intakeId, currentStatus) {
    stopIntakePoll();
    const poll = { cancelled: false, timer: null };
    activeIntakePoll = poll;
    const tick = async () => {
      if (poll.cancelled) return;
      let rec = null;
      try {
        rec = await api("/api/intakes/" + encodeURIComponent(intakeId));
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return; // login shown
        // A transient read failure must not kill the poll — the turn is still
        // running on the hub; fall through to reschedule and retry.
      }
      if (poll.cancelled || activeIntakePoll !== poll) return; // view left / superseded
      if (rec && rec.status !== currentStatus) {
        if (
          currentStatus === "confirming" &&
          rec.status === "confirmed" &&
          rec.created_task_id &&
          rec.repo_name
        ) {
          handoffToCreatedTask(rec);
        } else {
          rerender(renderIntakeReview(intakeId)); // settled (or a new transient) → repaint
        }
        return;
      }
      if (rec) updateIntakeElapsed(rec.updated_ts); // still working — tick the label
      poll.timer = setTimeout(tick, INTAKE_POLL_MS);
    };
    poll.timer = setTimeout(tick, INTAKE_POLL_MS);
  }

  async function renderIntakeReview(intakeId) {
    // A fresh render supersedes any in-flight poll (this may BE the poll's settle
    // re-render); it restarts below if the new status is still transient.
    stopIntakePoll();
    view().innerHTML = loadingView("Task intake", "Loading intake…");
    // Record + metadata-only audit events in one call. Unknown intake → 404 the
    // router surfaces as an error view.
    const data = await api(
      "/api/intakes/" + encodeURIComponent(intakeId) + "/history"
    );
    const repoName = data.repo_name || "";
    const back = repoName
      ? `<a class="back-link" href="${repoHash(repoName)}">← ${escapeHtml(
          repoName
        )}</a>`
      : `<a class="back-link" href="#/repos">← Repos</a>`;

    let body;
    if (data.status === "awaiting_review") {
      body = intakeDraftView(data.draft) + intakeReviewControls();
    } else if (data.status === "confirmed") {
      const taskId = data.created_task_id;
      body =
        `<section class="section"><h2>Task created</h2>
          <p class="muted">The task file and its index entry were written.</p>
          ${
            taskId && repoName
              ? `<p><a class="text-link" href="${taskHash(
                  repoName,
                  taskId
                )}">Open <code>${escapeHtml(taskId)}</code> →</a></p>`
              : ""
          }
        </section>` + intakeDraftView(data.draft);
    } else if (data.status === "failed" || data.status === "error") {
      body = `<section class="section"><h2>Intake failed</h2>
        <p class="error-text">${escapeHtml(
          data.error || "The intake did not complete."
        )}</p>
        <p class="muted">The target repo was not modified.</p>
        ${
          repoName
            ? `<p><a class="text-link" href="${repoHash(
                repoName
              )}">Back to ${escapeHtml(repoName)} →</a></p>`
            : ""
        }</section>`;
    } else if (data.status === "cancelled") {
      body = `<section class="section"><h2>Intake cancelled</h2>
        <p class="muted">No task was created and the repo is unchanged.</p></section>`;
    } else {
      // drafting / revising / confirming: an agent turn is in flight on the hub,
      // off the request lock. Show live status with a ticking elapsed time; the
      // poll started at the end of this render self-updates the view when the
      // turn settles, so no manual reload is needed and the operator can leave
      // the page. Cancel is offered for the two cancellable states; `confirming`
      // is non-cancellable (mirrors the backend _CANCELLABLE) so no cancel races
      // the write.
      const cancellable =
        data.status === "drafting" || data.status === "revising";
      body = `<section class="section intake intake-working">
        <h2>Working…</h2>
        <p class="muted">${escapeHtml(intakeWorkingVerb(data.status))} on the
          hub<span id="intake-elapsed" class="intake-elapsed">${escapeHtml(
            intakeElapsedPhrase(data.updated_ts)
          )}</span></p>
        <p class="muted intake-away-note">This turn runs on the hub, so it is
          safe to leave this page — it keeps going, and the view updates when
          the draft is ready.</p>
        <div class="intake-actions">
          ${
            cancellable
              ? `<button type="button" class="btn-danger intake-cancel">Cancel</button>`
              : ""
          }
          <span class="control-status intake-status" role="status"></span>
        </div>
      </section>`;
    }

    view().innerHTML = `
      ${back}
      <div class="detail-head">
        ${pageTitle("Task intake", "name big")}
        ${badge(data.status, intakeBadgeKind(data.status))}
      </div>
      ${
        data.request
          ? `<p class="intake-request muted">Request: ${escapeHtml(
              data.request
            )}</p>`
          : ""
      }
      ${body}
      ${intakeAuditSection(data.events)}`;

    wireIntakeReviewControls(intakeId);

    // A transient record self-updates: poll GET /api/intakes/{id} through the
    // in-flight turn until it settles. Resting statuses never poll.
    if (INTAKE_TRANSIENT.has(data.status)) startIntakePoll(intakeId, data.status);
  }

  function wireIntakeReviewControls(intakeId) {
    const confirmBtn = document.querySelector(".intake-confirm");
    if (confirmBtn)
      confirmBtn.addEventListener("click", () =>
        onIntakeConfirm(intakeId, confirmBtn)
      );
    const cancelBtn = document.querySelector(".intake-cancel");
    if (cancelBtn)
      cancelBtn.addEventListener("click", () =>
        onIntakeCancel(intakeId, cancelBtn)
      );
    const reviseForm = document.querySelector(".intake-revise-form");
    if (reviseForm)
      reviseForm.addEventListener("submit", (ev) => onIntakeRevise(ev, intakeId));
  }

  // Confirm: POST /confirm — the only write. The turn runs off the lock, so the
  // response is the transient `confirming` record; re-rendering into the working
  // panel lets its poll observe the settle and hand off to the created task's
  // detail (where the existing Start control launches it). A synchronous
  // `confirmed` settle hands off straight away; a `failed` confirm (lost session
  // / write failure) shows its outcome — no task created, repo unchanged.
  async function onIntakeConfirm(intakeId, button) {
    const statusEl = document.querySelector(".intake-status");
    setControlBusy(button, statusEl, true);
    try {
      const rec = await api(
        "/api/intakes/" + encodeURIComponent(intakeId) + "/confirm",
        { method: "POST" }
      );
      if (rec.status === "confirmed" && rec.created_task_id && rec.repo_name) {
        handoffToCreatedTask(rec); // synchronous settle (e.g. a test executor)
      } else {
        // Async path: the transient `confirming` record re-renders into the
        // working panel, whose poll performs the same handoff once the write
        // settles (or shows a `failed` outcome).
        rerender(renderIntakeReview(intakeId));
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      setControlBusy(button, statusEl, false);
      if (statusEl) {
        statusEl.classList.add("error-text");
        statusEl.textContent = controlErrorText(err, "Failed to confirm.");
      }
    }
  }

  // Cancel: POST /cancel — never writes. Re-renders to the cancelled outcome
  // (with its back-link and audit trail).
  async function onIntakeCancel(intakeId, button) {
    const statusEl = document.querySelector(".intake-status");
    setControlBusy(button, statusEl, true);
    try {
      await api("/api/intakes/" + encodeURIComponent(intakeId) + "/cancel", {
        method: "POST",
      });
      rerender(renderIntakeReview(intakeId));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      setControlBusy(button, statusEl, false);
      if (statusEl) {
        statusEl.classList.add("error-text");
        statusEl.textContent = controlErrorText(err, "Failed to cancel.");
      }
    }
  }

  // Request changes: POST /revise with feedback — still read-only, replaces the
  // draft and bumps the iteration in the same session. Re-renders to the fresh
  // draft (or a failed record on a lost session / agent failure).
  async function onIntakeRevise(ev, intakeId) {
    ev.preventDefault();
    const form = ev.currentTarget;
    const input = form.querySelector(".intake-feedback");
    const statusEl = form.querySelector(".intake-revise-status");
    const button = form.querySelector("button");
    if (statusEl) {
      statusEl.textContent = "";
      statusEl.classList.remove("error-text");
    }
    const feedback = input ? input.value.trim() : "";
    if (!feedback) {
      if (statusEl) statusEl.textContent = "Describe the change first.";
      if (input) input.focus();
      return;
    }
    if (button) button.disabled = true;
    try {
      await api("/api/intakes/" + encodeURIComponent(intakeId) + "/revise", {
        method: "POST",
        body: { feedback },
      });
      rerender(renderIntakeReview(intakeId));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      if (button) button.disabled = false;
      if (statusEl) {
        statusEl.classList.add("error-text");
        statusEl.textContent = controlErrorText(err, "Failed to send changes.");
      }
      restoreFocus(button);
    }
  }

  // ---- run logs (recent tail + live SSE stream) ----

  // Parse one SSE event block (the text between blank-line separators) per the
  // event-stream wire format: ":"-prefixed lines are comments (our heartbeat),
  // a field's single leading space is stripped, and `event:`/`data:` are the
  // only fields the log stream uses. `hasData` distinguishes a real (possibly
  // empty) log line from a comment-only heartbeat block.
  function parseSseBlock(block) {
    let event = "message";
    const data = [];
    for (const line of block.split("\n")) {
      if (line === "" || line[0] === ":") continue;
      const idx = line.indexOf(":");
      const field = idx === -1 ? line : line.slice(0, idx);
      let value = idx === -1 ? "" : line.slice(idx + 1);
      if (value[0] === " ") value = value.slice(1);
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    return { event, data: data.join("\n"), hasData: data.length > 0 };
  }

  // Tail a run's log over the bearer-authenticated SSE endpoint with fetch + a
  // ReadableStream reader — never `EventSource`, which cannot set the
  // Authorization header. Invokes handlers.onLine(text) per completed log line
  // and handlers.onEnd(reason) once (terminal `event: end`, or the connection
  // closing). handlers.signal aborts the read when the view is left; a 401
  // re-gates exactly like api().
  async function streamLog(runId, tail, handlers) {
    const resp = await fetch(
      "/api/runs/" + encodeURIComponent(runId) + "/logs/stream?tail=" + tail,
      { headers: authHeaders(), signal: handlers.signal }
    );
    if (resp.status === 401) {
      setToken("");
      renderLogin("That token was rejected. Enter a valid one.");
      throw new ApiError("unauthorized", 401);
    }
    if (!resp.ok || !resp.body) {
      throw new ApiError(
        "Log stream failed (" + resp.status + ").",
        resp.status
      );
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const evt = parseSseBlock(buffer.slice(0, sep));
        buffer = buffer.slice(sep + 2);
        if (evt.event === "end") {
          handlers.onEnd(evt.data || "stream ended");
          return;
        }
        if (evt.hasData) handlers.onLine(evt.data);
      }
    }
    handlers.onEnd(null); // connection closed without an explicit end frame
  }

  // Append log lines as text nodes (log content is untrusted — never HTML) and
  // keep the view pinned to the bottom only when the user was already there.
  function appendLogLines(pre, lines) {
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    const frag = document.createDocumentFragment();
    for (const line of lines)
      frag.appendChild(document.createTextNode(line + "\n"));
    pre.appendChild(frag);
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }

  // Finished run: a static recent tail, no held connection.
  async function loadRecentLog(runId, pre, statusEl) {
    statusEl.textContent = "Loading…";
    statusEl.classList.remove("error-text");
    let snap;
    try {
      snap = await api(
        "/api/runs/" +
          encodeURIComponent(runId) +
          "/logs?tail=" +
          DEFAULT_LOG_TAIL
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      statusEl.textContent =
        err instanceof ApiError ? err.message : "Failed to load log.";
      statusEl.classList.add("error-text");
      return;
    }
    if (!snap.present) {
      const diag = snap.diagnostic;
      if (diag && diag.lines && diag.lines.length) {
        // No orchestrator log — the run likely failed during startup, before it
        // opened its own log. Show the spawn.out capture so the error isn't an
        // empty view; the `diagnostic` class marks it visually distinct.
        pre.textContent = "";
        pre.classList.add("diagnostic");
        appendLogLines(pre, diag.lines);
        statusEl.textContent =
          "No orchestrator log — showing startup diagnostics (spawn.out)" +
          (diag.truncated ? " (truncated)." : ".");
        return;
      }
      statusEl.textContent = snap.detail || "No log file for this run.";
      return;
    }
    pre.textContent = "";
    const lines = snap.lines || [];
    appendLogLines(pre, lines);
    statusEl.textContent = snap.truncated
      ? "Showing the last " + lines.length + " lines (truncated)."
      : lines.length + (lines.length === 1 ? " line." : " lines.");
  }

  async function renderRunLogs(runId) {
    // Leaving or reloading this view aborts any prior in-flight stream first.
    if (activeStream) {
      activeStream.abort();
      activeStream = null;
    }
    view().innerHTML = loadingView("Run log", "Loading run…");
    const run = await api("/api/runs/" + encodeURIComponent(runId)); // unknown → 404
    const live = LIVE_STATES.has(run.state);
    // The task link goes through the run-scoped task view (resolved from the run
    // record), not taskHash's index-gated task detail, so it still opens the
    // task file after close-out archives the task instead of 404-ing.
    const back = run.task_id
      ? `<a class="back-link" href="${runTaskHash(
          run.run_id
        )}">← ${escapeHtml(run.task_id)}</a>`
      : `<a class="back-link" href="#/runs">← Runs</a>`;
    // The log body is `tabindex="0"` so its scroll region is keyboard-reachable,
    // and deliberately carries no live-region role — the ARIA log role implies
    // one, which would narrate every streamed line. The toolbar's status line
    // carries the few announcements that matter: following, paused, ended,
    // failed.
    view().innerHTML = `
      ${back}
      <div class="detail-head">
        ${pageTitle(run.run_id, "run-id big")}
        ${runBadge(run.state)}
      </div>
      <div class="muted">${escapeHtml(
        [run.repo_name, run.task_id].filter(Boolean).join(" · ")
      )}</div>
      <div class="log-toolbar">
        <span id="log-status" class="muted" role="status"></span>
        <span class="log-actions">
          ${
            live
              ? `<button id="log-pause" class="btn-secondary" type="button">Pause</button>`
              : ""
          }
          <button id="log-reload" class="btn-secondary" type="button">Reload</button>
        </span>
      </div>
      <pre id="log-output" class="log-output" tabindex="0"
        aria-label="Run log output"></pre>`;

    const pre = document.getElementById("log-output");
    const statusEl = document.getElementById("log-status");
    document.getElementById("log-reload").addEventListener("click", () => {
      renderRunLogs(runId).catch((err) => {
        if (err instanceof ApiError && err.status === 401) return; // login shown
        renderError(err);
      });
    });

    if (!live) {
      await loadRecentLog(runId, pre, statusEl);
      return;
    }

    // Live run: follow via SSE. The controller is module-level so the router
    // (or a Reload) aborts the in-flight stream when the view is left.
    const controller = new AbortController();
    activeStream = controller;
    const pauseBtn = document.getElementById("log-pause");
    pauseBtn.addEventListener("click", () => {
      controller.abort();
      if (activeStream === controller) activeStream = null;
      pauseBtn.hidden = true;
      statusEl.textContent = "Paused — Reload to resume.";
      // Hiding the pressed button would drop focus to <body>; hand it to the
      // Reload button beside it, which is the only way to resume.
      restoreFocus(document.getElementById("log-reload"));
    });
    statusEl.textContent = "Following live…";
    let sawLine = false;
    streamLog(runId, DEFAULT_LOG_TAIL, {
      signal: controller.signal,
      onLine: (line) => {
        sawLine = true;
        appendLogLines(pre, [line]);
      },
      onEnd: (reason) => {
        if (activeStream === controller) activeStream = null;
        pauseBtn.hidden = true;
        if (!sawLine) {
          // The run ended without writing an orchestrator log (e.g. it failed
          // during startup). Pull the recent-log route, which falls back to the
          // spawn.out startup diagnostic, rather than leaving an empty view.
          loadRecentLog(runId, pre, statusEl);
          return;
        }
        statusEl.textContent = reason
          ? "Stream ended: " + reason
          : "Stream closed.";
      },
    }).catch((err) => {
      if (controller.signal.aborted) return; // navigated away or paused
      if (err instanceof ApiError && err.status === 401) return; // login shown
      if (activeStream === controller) activeStream = null;
      pauseBtn.hidden = true;
      statusEl.textContent =
        err instanceof ApiError ? err.message : "Log stream error.";
      statusEl.classList.add("error-text");
    });
  }

  // ---- operations overview ----
  //
  // A single landing screen that aggregates the existing protected APIs
  // (/api/repos, /api/tasks, /api/runs, /api/escalations, /api/history/runs)
  // into: a summary-metric row, a prioritized "needs attention" panel
  // (waiting-for-answer runs, then readiness failures, then housekeeping), a
  // "current activity" panel of live runs, and a "recent outcomes" panel of the
  // last few ended runs. It adds no HTTP API — every figure is derived
  // client-side from payloads the other views already consume, and each item
  // deep-links to the operational view that acts on it (a waiting run → its task
  // detail, where the answer form lives; a repo problem → repo detail; a live or
  // ended run → its logs).
  //
  // Aggregation is done by small pure, DOM-free helpers so a count always agrees
  // with its source view (they reuse taskCountsByRepo / LIVE_STATES / repoRunState
  // vocabulary) and so the behaviour is unit-testable off a stubbed browser.
  //
  // Best-effort fetch: each source degrades to null on a non-401 failure so one
  // outage can't blank the unrelated panels (a null source shows "—" / an
  // "unavailable" note, never a wrong "nothing here"); a 401 still re-gates.
  // While the Overview is the routed view a bounded background refresh (a chained
  // setTimeout — never setInterval — torn down by router()) silently repaints it.

  // Fetch one overview source, degrading a non-401 failure to null (its panel
  // then shows a placeholder) while a 401 rethrows so the shared login gate that
  // api() already showed still wins.
  async function overviewSource(path) {
    try {
      return await api(path);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
      return null;
    }
  }

  // Repos ready vs total from the /api/repos payload.
  function readinessSummary(repos) {
    let ready = 0;
    for (const r of repos || []) if (r.readiness && r.readiness.ready) ready++;
    return { ready, total: (repos || []).length };
  }

  // Total active task rows across all repos, reusing the same per-repo inventory
  // count the repo cards show so the overview total agrees with them (NOT a raw
  // `.ai-tasks/` file count).
  function totalActiveTasks(taskRepos) {
    const counts = taskCountsByRepo(taskRepos);
    let n = 0;
    for (const name in counts) n += counts[name];
    return n;
  }

  // The live runs (active or waiting) from the /api/runs payload — the same
  // LIVE_STATES partition the repo/task run signals use.
  function liveRunList(runs) {
    return (runs || []).filter((r) => LIVE_STATES.has(r.state));
  }

  // A one-line, length-bounded prompt for a waiting-item detail: the question
  // banner or message collapsed to one line and clipped so a long plan report
  // doesn't dominate the overview. Empty when there's no readable prompt.
  function shortPrompt(question, max) {
    if (!question) return "";
    const oneLine = String(question.banner || question.message || "")
      .replace(/\s+/g, " ")
      .trim();
    if (!oneLine) return "";
    const limit = max || 120;
    return oneLine.length > limit ? oneLine.slice(0, limit - 1) + "…" : oneLine;
  }

  // The waiting-for-answer attention items: every waiting run (authoritative for
  // "a run is blocked on a human") merged with the escalation question text by
  // run_id. Building from runs first means a waiting run whose question file is
  // unparseable still surfaces (escalations list it too, but this stays robust to
  // a partial fetch); the escalation, when present, fills in the prompt.
  function waitingAttentionItems(runs, escalations) {
    const byRun = new Map();
    for (const run of runs || []) {
      if (run.state !== "waiting") continue;
      byRun.set(run.run_id, {
        run_id: run.run_id,
        repo_name: run.repo_name,
        task_id: run.task_id,
        question: null,
      });
    }
    for (const esc of escalations || []) {
      const item = byRun.get(esc.run_id) || {
        run_id: esc.run_id,
        repo_name: esc.repo_name,
        task_id: esc.task_id,
        question: null,
      };
      if (esc.question) item.question = esc.question;
      byRun.set(esc.run_id, item);
    }
    return [...byRun.values()];
  }

  // One summary-metric tile: a big value over a label, coloured by `kind`
  // (warn/ok/run) via the shared badge colour vocabulary.
  function metricTile(value, label, kind) {
    return (
      `<div class="metric${kind ? " " + kind : ""}">` +
      `<span class="metric-value">${escapeHtml(value)}</span>` +
      `<span class="metric-label">${escapeHtml(label)}</span>` +
      `</div>`
    );
  }

  // The summary-metric row. A null source shows "—" so a failed fetch reads as
  // "unknown", never a misleading zero.
  function overviewMetrics(repos, taskRepos, runs, escalations) {
    const tiles = [];
    if (repos) {
      const r = readinessSummary(repos);
      const kind = r.total && r.ready < r.total ? "warn" : r.total ? "ok" : "";
      tiles.push(metricTile(r.ready + "/" + r.total, "repos ready", kind));
    } else {
      tiles.push(metricTile("—", "repos ready", ""));
    }
    tiles.push(
      metricTile(
        taskRepos ? String(totalActiveTasks(taskRepos)) : "—",
        "active tasks",
        ""
      )
    );
    const liveCount = runs ? liveRunList(runs).length : null;
    tiles.push(
      metricTile(
        liveCount === null ? "—" : String(liveCount),
        "live runs",
        liveCount ? "run" : ""
      )
    );
    const waitingCount =
      runs === null && escalations === null
        ? null
        : waitingAttentionItems(runs, escalations).length;
    tiles.push(
      metricTile(
        waitingCount === null ? "—" : String(waitingCount),
        "awaiting answer",
        waitingCount ? "warn" : ""
      )
    );
    return `<div class="overview-metrics">${tiles.join("")}</div>`;
  }

  // One "needs attention" row: a deep-linked card with a colour-coded left-border
  // accent by `item.kind` (waiting/not-ready/housekeeping), a status badge, and an
  // optional sub-line + detail. `item.badge` is pre-built badge HTML.
  function attentionRow(item) {
    const sub = item.sub
      ? `<span class="attn-sub muted">${escapeHtml(item.sub)}</span>`
      : "";
    const detail = item.detail
      ? `<div class="attn-detail muted">${escapeHtml(item.detail)}</div>`
      : "";
    return (
      `<li class="card attn attn-${item.kind}">` +
      `<a class="card-link" href="${item.href}">` +
      `<div class="card-head">` +
      `<span class="attn-title">${escapeHtml(item.title)}</span>` +
      `<span class="badges">${item.badge}</span>` +
      `</div>${sub}${detail}</a></li>`
    );
  }

  // The prioritized "needs attention" panel. Order is by urgency: waiting-for-
  // answer (a run is blocked on you) first, then readiness failures (they block
  // launching), then housekeeping (a maintenance nudge). When every source failed
  // it says so rather than a false "nothing needs attention".
  function attentionPanel(repos, runs, escalations) {
    const head = `<section class="section overview-attention"><h2>Needs attention</h2>`;
    const foot = `</section>`;
    if (repos === null && runs === null && escalations === null)
      return head + `<p class="muted">Attention data is unavailable.</p>` + foot;
    const items = [];
    for (const w of waitingAttentionItems(runs, escalations)) {
      items.push({
        kind: "waiting",
        badge: badge("waiting", "warn"),
        title: w.task_id || w.run_id,
        sub: w.repo_name,
        detail: shortPrompt(w.question, 120),
        // The actionable target is the task detail (the answer form lives there),
        // falling back to the run's logs when the run carries no task id.
        href:
          w.repo_name && w.task_id
            ? taskHash(w.repo_name, w.task_id)
            : logsHash(w.run_id),
      });
    }
    for (const r of repos || []) {
      if (r.readiness && !r.readiness.ready)
        items.push({
          kind: "not-ready",
          badge: badge("not ready", "bad"),
          title: r.name,
          sub: "readiness failed",
          detail: (r.readiness.checks || []).map((c) => c.name).join(", "),
          href: repoHash(r.name),
        });
    }
    for (const r of repos || []) {
      if (r.housekeeping_pending)
        items.push({
          kind: "housekeeping",
          badge: badge("housekeeping", "warn"),
          title: r.name,
          sub: "housekeeping pending",
          detail: "",
          href: repoHash(r.name),
        });
    }
    if (!items.length)
      return (
        head + `<p class="muted">Nothing needs attention right now.</p>` + foot
      );
    return (
      head +
      `<ul class="list attention-list">${items.map(attentionRow).join("")}</ul>` +
      foot
    );
  }

  // One live run in the current-activity panel: a run-accented card linking to
  // its logs (watching progress); the waiting ones also appear in the attention
  // panel, where their task link answers the question.
  function activityCard(run) {
    const accent = run.state === "waiting" ? " run-waiting" : " run-active";
    const meta = [run.repo_name, run.task_id].filter(Boolean).join(" · ");
    return (
      `<li class="card run${accent}">` +
      `<a class="card-link" href="${logsHash(run.run_id)}">` +
      `<div class="card-head">` +
      `<span class="run-id">${escapeHtml(run.run_id)}</span>` +
      `<span class="badges">${runBadge(run.state)}</span>` +
      `</div>` +
      (meta ? `<div class="muted">${escapeHtml(meta)}</div>` : "") +
      `</a></li>`
    );
  }

  // The "current activity" panel of live runs (active + waiting). A null source
  // reads as "unavailable"; an empty live set as "no live runs".
  function currentActivityPanel(runs) {
    const head = `<section class="section"><h2>Current activity</h2>`;
    const foot = `</section>`;
    if (runs === null)
      return head + `<p class="muted">Run activity is unavailable.</p>` + foot;
    const live = liveRunList(runs);
    if (!live.length)
      return head + `<p class="muted">No live runs.</p>` + foot;
    return head + `<ul class="list">${live.map(activityCard).join("")}</ul>` + foot;
  }

  // One ended run in the recent-outcomes panel: its end-state badge (completed →
  // ok, error → bad, stopped/ended → neutral, via runBadge) over repo · task,
  // linking to its log tail (which itself back-links to the task file). No live
  // accent — these are finished runs; the live ones are in current activity.
  function recentOutcomeCard(run) {
    const meta = [run.repo_name, run.task_id].filter(Boolean).join(" · ");
    return (
      `<li class="card run">` +
      `<a class="card-link" href="${logsHash(run.run_id)}">` +
      `<div class="card-head">` +
      `<span class="run-id">${escapeHtml(run.run_id)}</span>` +
      `<span class="badges">${runBadge(run.state)}</span>` +
      `</div>` +
      (meta ? `<div class="muted">${escapeHtml(meta)}</div>` : "") +
      `</a></li>`
    );
  }

  // The most recent ended runs from /api/history/runs (already newest-first):
  // drop the live ones (they're in current activity) and cap the list so the
  // Overview stays glanceable rather than duplicating the full Runs page.
  function recentOutcomeList(history, max) {
    const ended = (history || []).filter((r) => !LIVE_STATES.has(r.state));
    return ended.slice(0, max || OVERVIEW_OUTCOME_COUNT);
  }

  // The "recent outcomes" panel. A null source reads as "unavailable"; an empty
  // ended set (only live runs, or none yet) as "no recent runs".
  function recentOutcomesPanel(history) {
    const head = `<section class="section"><h2>Recent outcomes</h2>`;
    const foot = `</section>`;
    if (history === null)
      return head + `<p class="muted">Recent outcomes are unavailable.</p>` + foot;
    const outcomes = recentOutcomeList(history, OVERVIEW_OUTCOME_COUNT);
    if (!outcomes.length)
      return head + `<p class="muted">No recent runs.</p>` + foot;
    return (
      head +
      `<ul class="list">${outcomes.map(recentOutcomeCard).join("")}</ul>` +
      foot
    );
  }

  // ---- overview data + paint + bounded refresh ----
  //
  // renderOverview is split into loadOverview (fetch+normalize) and paintOverview
  // (build the DOM) so the background refresh can repaint in place without the
  // initial "Loading…" flash, then a chained-setTimeout refresh (never
  // setInterval — no overlapping requests) keeps it current until navigation.

  // Fetch every overview source in parallel and normalize each to its panel data:
  // null = the source failed (its panel degrades to a placeholder), [] = loaded
  // but empty. overviewSource swallows non-401 errors to null, so Promise.all
  // only rejects on a 401 (api() already re-gated; the caller returns).
  async function loadOverview() {
    const [reposData, tasksData, runsData, escData, historyData] =
      await Promise.all([
        overviewSource("/api/repos"),
        overviewSource("/api/tasks"),
        overviewSource("/api/runs"),
        overviewSource("/api/escalations"),
        overviewSource("/api/history/runs?limit=" + OVERVIEW_HISTORY_LIMIT),
      ]);
    return {
      repos: reposData ? reposData.repos || [] : null,
      taskRepos: tasksData ? tasksData.repos || [] : null,
      runs: runsData ? runsData.runs || [] : null,
      escalations: escData ? escData.escalations || [] : null,
      history: historyData ? historyData.runs || [] : null,
    };
  }

  // Repaint the Overview from a loadOverview() snapshot. Each panel reads its
  // own source independently, so one null source degrades only its panel. The
  // repaint is confined to `#overview-body`: the page heading lives outside it
  // and survives, so a background refresh can neither destroy the focus target
  // a navigation just landed on nor re-announce the page. The lower three panels
  // share a wrapper the responsive CSS lays out two-up on wider screens.
  function paintOverview(data) {
    let body = document.getElementById("overview-body");
    if (!body) {
      view().innerHTML =
        pageTitle("Operations overview") + `<div id="overview-body"></div>`;
      body = document.getElementById("overview-body");
    }
    body.innerHTML =
      overviewMetrics(data.repos, data.taskRepos, data.runs, data.escalations) +
      `<div class="overview-panels">` +
      attentionPanel(data.repos, data.runs, data.escalations) +
      currentActivityPanel(data.runs) +
      recentOutcomesPanel(data.history) +
      `</div>`;
  }

  // True when focus sits on something inside the repaintable Overview body. A
  // background repaint would destroy that node and strand the keyboard user at
  // <body>, so a refresh tick skips the paint (and retries next tick) rather
  // than ripping the list out from under them. Pointer/touch use leaves focus on
  // <body>, so the ordinary mobile case still repaints every tick.
  function focusInsideOverviewBody() {
    const body = document.getElementById("overview-body");
    const active = document.activeElement;
    return !!(
      body &&
      active &&
      active !== document.body &&
      body.contains(active)
    );
  }

  // Tear down the Overview background refresh (mirrors stopIntakePoll): cancel the
  // handle and clear its pending timer so no queued tick paints into a left view.
  function stopOverviewRefresh() {
    if (activeOverviewRefresh) {
      activeOverviewRefresh.cancelled = true;
      if (activeOverviewRefresh.timer) clearTimeout(activeOverviewRefresh.timer);
      activeOverviewRefresh = null;
    }
  }

  // Drive the chained background refresh on the handle renderOverview registered
  // before the initial load. A chained setTimeout — never setInterval — so a
  // slow multi-source load can't overlap the next fetch; a staleness guard
  // (refresh.cancelled || activeOverviewRefresh !== refresh) before each
  // paint/reschedule drops a tick that outlived its view (router() calls
  // stopOverviewRefresh on navigation), and a 401 ends the loop (api() already
  // re-gated) rather than rescheduling. The caller owns handle creation, so this
  // just schedules the first tick — it does not stop/replace the active handle.
  function startOverviewRefresh(refresh) {
    const tick = async () => {
      if (refresh.cancelled) return;
      let data = null;
      try {
        data = await loadOverview();
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return; // login shown
        // Non-401 source failures are already absorbed to null by overviewSource,
        // so this is unexpected; drop this tick and reschedule below rather than
        // kill the refresh.
      }
      if (refresh.cancelled || activeOverviewRefresh !== refresh) return; // left / superseded
      if (data && !focusInsideOverviewBody()) paintOverview(data);
      refresh.timer = setTimeout(tick, OVERVIEW_REFRESH_MS);
    };
    refresh.timer = setTimeout(tick, OVERVIEW_REFRESH_MS);
  }

  // The Overview landing view: an initial load behind a "Loading…" placeholder,
  // then a bounded background refresh that repaints in place until navigation.
  // The refresh handle is registered BEFORE the initial load so a navigation
  // during that first multi-source fetch tears it down (router() ->
  // stopOverviewRefresh cancels it and nulls activeOverviewRefresh); the same
  // staleness guard after the await then drops a stale load. Without it, a load
  // that outlived its view would repaint the destination view and start a
  // detached refresh loop that keeps polling after navigation away.
  async function renderOverview() {
    view().innerHTML = loadingView("Operations overview", "Loading overview…");
    stopOverviewRefresh();
    const refresh = { cancelled: false, timer: null };
    activeOverviewRefresh = refresh;
    let data;
    try {
      data = await loadOverview();
    } catch (err) {
      // A 401 already re-gated via api(); drop our handle so no refresh survives
      // the login gate, then let router() render login (matches the pre-fix
      // behavior where the initial 401 never left a handle).
      if (activeOverviewRefresh === refresh) activeOverviewRefresh = null;
      throw err;
    }
    if (refresh.cancelled || activeOverviewRefresh !== refresh) return; // navigated away mid-load
    paintOverview(data);
    startOverviewRefresh(refresh);
  }

  // ---- router ----
  function currentPath() {
    // The Overview is the default authenticated landing view (an empty hash),
    // so a fresh load / sign-in / `location.hash = ""` opens the ops overview
    // rather than the repo list. An unknown *non-empty* path still falls back to
    // the repo list in matchRoute — only the empty-hash default moved here.
    return location.hash.replace(/^#/, "") || "/overview";
  }

  // Segment router: repo/task detail routes carry ids in the path, so match on
  // the split segments rather than an exact-string table. Unknown paths fall
  // back to the repo list.
  function matchRoute(path) {
    const seg = path
      .split("/")
      .filter(Boolean)
      .map((s) => {
        try {
          return decodeURIComponent(s);
        } catch (_) {
          return s;
        }
      });
    if (seg[0] === "runs") {
      if (seg.length >= 3 && seg[2] === "logs")
        return () => renderRunLogs(seg[1]);
      if (seg.length >= 3 && seg[2] === "task")
        return () => renderRunTask(seg[1]);
      return renderRuns;
    }
    if (seg[0] === "tasks")
      return seg.length >= 3
        ? () => renderTaskDetail(seg[1], seg[2])
        : renderTasks;
    if (seg[0] === "intakes" && seg.length >= 2)
      return () => renderIntakeReview(seg[1]);
    if (seg[0] === "repos" && seg.length >= 2)
      return () => renderRepoDetail(seg[1]);
    if (seg[0] === "overview") return renderOverview;
    return renderRepos;
  }

  async function router() {
    // Claim this navigation's generation before anything can await: a later
    // router() entry (hashchange, login submit, Retry) bumps it and thereby
    // marks this invocation stale. Route handlers await network payloads, so a
    // slow route can still resolve long after the user moved on — and its
    // post-await work would then act on a view it no longer owns.
    const generation = ++routeGeneration;
    // Leaving any view aborts an in-flight log stream and stops the intake poll
    // and the Overview refresh so none keep reading/painting after navigation.
    if (activeStream) {
      activeStream.abort();
      activeStream = null;
    }
    stopIntakePoll();
    stopOverviewRefresh();
    if (!getToken()) {
      renderLogin();
      return;
    }
    showChrome(true);
    const path = currentPath();
    setActiveTab(path);
    const handler = matchRoute(path);
    let failure = null;
    try {
      await handler();
    } catch (err) {
      failure = err;
    }
    // A newer route claimed the generation while this handler was awaiting: that
    // invocation owns the view now, so a stale completion must neither paint its
    // error over the newer view nor move focus out of it — which would rip a
    // keyboard user back to the top of a page they were already operating.
    if (generation !== routeGeneration) return;
    if (failure) {
      // A 401 already rendered (and landed on) the login gate.
      if (failure instanceof ApiError && failure.status === 401) return;
      renderError(failure);
    }
    // Name the new page and put focus at its start, so a keyboard/screen-reader
    // user arrives on the destination instead of the link they activated.
    landOnView();
  }

  function signOut() {
    setToken("");
    location.hash = "";
    renderLogin();
  }

  document.getElementById("signout-btn").addEventListener("click", signOut);
  // Skip link: the href is inert on purpose — letting it navigate to "#view"
  // would be read by the hash router as a route change (and fall back to the
  // repo list), so it moves focus to the main region directly instead.
  document.getElementById("skip-link").addEventListener("click", (ev) => {
    ev.preventDefault();
    focusPageStart();
  });
  window.addEventListener("hashchange", router);
  router();
})();
