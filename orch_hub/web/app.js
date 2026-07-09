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
 */
(function () {
  const TOKEN_KEY = "orch-hub-token";
  const LIVE_STATES = new Set(["active", "waiting"]); // run states that badge a repo
  const DEFAULT_LOG_TAIL = 200; // initial log lines to request (matches the API default)
  let activeStream = null; // AbortController for the in-flight SSE log stream, if any
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

  // Build an in-app hash link. Each dynamic segment is encoded so a repo name
  // or task id is always exactly one path segment for the router to split back.
  const repoHash = (repo) => "#/repos/" + encodeURIComponent(repo);
  const taskHash = (repo, taskId) =>
    "#/tasks/" + encodeURIComponent(repo) + "/" + encodeURIComponent(taskId);
  const logsHash = (runId) => "#/runs/" + encodeURIComponent(runId) + "/logs";

  function showChrome(authed) {
    document.getElementById("signout-btn").hidden = !authed;
    document.getElementById("nav").hidden = !authed;
  }

  function setActiveTab(path) {
    let tab = "/repos";
    if (path.startsWith("/tasks")) tab = "/tasks";
    else if (path.startsWith("/runs")) tab = "/runs";
    document.querySelectorAll(".tabbar a").forEach((a) => {
      a.classList.toggle("active", a.dataset.tab === tab);
    });
  }

  function renderError(err) {
    const msg =
      err instanceof ApiError ? err.message : "Something went wrong.";
    view().innerHTML =
      `<div class="center"><p class="error-text">${escapeHtml(msg)}</p>` +
      `<button class="retry-btn" id="retry-btn" type="button">Retry</button></div>`;
    document
      .getElementById("retry-btn")
      .addEventListener("click", () => router());
  }

  // ---- login gate ----
  function renderLogin(message) {
    showChrome(false);
    view().innerHTML = `
      <section class="card login">
        <h2>Connect to orch-hub</h2>
        <p class="muted">Paste the service bearer token
          (<code>ORCH_HUB_TOKEN</code>). It is stored only in this browser.</p>
        ${message ? `<p class="error-text">${escapeHtml(message)}</p>` : ""}
        <form id="login-form">
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
    view().innerHTML = centerHtml("Loading repos…");
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
      view().innerHTML = centerHtml("No managed repos discovered.");
      return;
    }
    view().innerHTML =
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
  // filter never refetches — task inventory is read once per view entry.
  const taskFilters = { repo: "", status: "" };
  // Repo-detail status filter (repo is fixed by the route); reset when it no
  // longer matches the repo's task statuses.
  let repoDetailStatus = "";

  function renderTaskList(repos, liveByRepoTask) {
    const selectedRepos = taskFilters.repo
      ? repos.filter((r) => r.name === taskFilters.repo)
      : repos;
    const cards = [];
    for (const repo of selectedRepos) {
      if (!repo.index_present) cards.push(missingIndexCard(repo.name));
      const byTask = (liveByRepoTask && liveByRepoTask[repo.name]) || {};
      for (const task of repo.tasks || []) {
        if (taskFilters.status && task.status !== taskFilters.status) continue;
        cards.push(taskCard(task, repo.name, byTask[task.task_id]));
      }
    }
    const list = document.getElementById("task-list");
    list.innerHTML = cards.length
      ? cards.join("")
      : `<li class="center">No tasks match these filters.</li>`;
  }

  async function renderTasks() {
    view().innerHTML = centerHtml("Loading tasks…");
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
      <div class="filters">
        <label>Repo
          <select id="f-repo">${optionList(
            repoNames,
            taskFilters.repo,
            "All repos"
          )}</select>
        </label>
        <label>Status
          <select id="f-status">${optionList(
            statuses,
            taskFilters.status,
            "All statuses"
          )}</select>
        </label>
      </div>
      <ul id="task-list" class="list"></ul>`;
    document.getElementById("f-repo").addEventListener("change", (ev) => {
      taskFilters.repo = ev.target.value;
      renderTaskList(repos, liveByRepoTask);
    });
    document.getElementById("f-status").addEventListener("change", (ev) => {
      taskFilters.status = ev.target.value;
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
    view().innerHTML = centerHtml("Loading " + repoName + "…");
    // Required: this repo's task inventory (single-repo envelope). An unknown
    // repo is a 404 the router surfaces as an error view.
    const data = await api("/api/repos/" + encodeURIComponent(repoName) + "/tasks");
    const entry = (data.repos || [])[0];
    if (!entry) {
      view().innerHTML =
        `<a class="back-link" href="#/repos">← Repos</a>` +
        centerHtml("Repo not found.");
      return;
    }
    // Best-effort overlays: readiness (from the all-repos payload) and live
    // runs. Neither failing should blank out the task list; only a dropped
    // token (401) propagates to the login gate.
    let readiness = null;
    let repoPath = entry.path;
    try {
      const reposData = await api("/api/repos");
      const match = (reposData.repos || []).find((r) => r.name === repoName);
      if (match) {
        readiness = match.readiness;
        repoPath = match.path;
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
        <span class="name big">${escapeHtml(repoName)}</span>
        ${readyBadge}
      </div>
      <div class="path muted">${escapeHtml(repoPath || "")}</div>
      ${reasons}
      ${
        entry.index_present
          ? `<div class="filters"><label>Status
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

  // ---- task detail ----
  function runStatusSection(run) {
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
    return `<section class="section">
      <h2>Run ${runBadge(run.state)}</h2>
      <dl class="kv">${kv}</dl>
      ${run.detail ? `<p class="muted">${escapeHtml(run.detail)}</p>` : ""}
      ${run.error ? `<p class="error-text">${escapeHtml(run.error)}</p>` : ""}
      <p><a class="text-link" href="${logsHash(run.run_id)}">View logs →</a></p>
    </section>`;
  }

  function questionForm(runId, q) {
    const prompt = q.banner || q.message || "";
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
          placeholder='Type an answer — "stop" ends the run'></textarea>
        <div class="answer-actions">
          <button type="submit">Send answer</button>
          <span class="answer-status error-text" role="alert"></span>
        </div>
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
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const runId = form.dataset.run;
        const seq = form.dataset.seq;
        const input = form.querySelector(".answer-input");
        const statusEl = form.querySelector(".answer-status");
        const button = form.querySelector("button");
        statusEl.textContent = "";
        const answer = input.value.trim();
        if (!answer) {
          statusEl.textContent = "Enter an answer first.";
          return;
        }
        button.disabled = true;
        try {
          await api(
            "/api/runs/" +
              encodeURIComponent(runId) +
              "/escalations/" +
              encodeURIComponent(seq) +
              "/answer",
            { method: "POST", body: { answer, responder: "dashboard" } }
          );
          router(); // re-render: the question is answered, the run leaves `waiting`
        } catch (err) {
          if (err instanceof ApiError && err.status === 401) return; // login shown
          button.disabled = false;
          statusEl.textContent =
            err instanceof ApiError ? err.message : "Failed to send answer.";
        }
      });
    });
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

  // The Start/Stop control for a task, chosen from the run overlay the backend
  // already enforces. Order matters: a live run for THIS task offers Stop; a
  // live run elsewhere in the repo blocks Start (busy); otherwise Start is
  // offered only when the task status and (best-effort) readiness look launchable
  // — every hidden Start is still backed by the backend 409 if raced.
  function runControlsSection(repoName, detail, run, busyRun, readiness) {
    const head = `<section class="section run-controls"><h2>Run controls</h2>`;
    const foot = `</section>`;

    if (run && LIVE_STATES.has(run.state)) {
      return (
        head +
        `<form class="stop-form" data-run="${escapeHtml(run.run_id)}">
          <button type="submit" class="btn-danger">Stop run</button>
          <span class="control-status" role="status"></span>
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
    return (
      head +
      `<button type="button" class="open-run-options">Start run…</button>
      <dialog class="run-options">
        <form class="start-form" data-repo="${escapeHtml(
          repoName
        )}" data-task="${escapeHtml(detail.task_id)}" data-blocked="${
        blocked ? "1" : ""
      }">
          <h3>Start run</h3>
          <p class="muted opts-note">Backend is fixed to <code>cc-codex</code>.
            Leave a field blank to use the orchestrator default; only changed
            fields are passed.</p>
          <label>Dev agent
            <select class="opt-dev-agent">
              <option value="">default</option>
              <option value="claude">claude</option>
              <option value="codex">codex</option>
            </select>
          </label>
          <label>Dev model
            <input type="text" class="opt-dev-model" autocapitalize="off"
              autocomplete="off" spellcheck="false" placeholder="default" />
          </label>
          <label>Dev effort
            <input type="text" class="opt-dev-effort" autocapitalize="off"
              autocomplete="off" spellcheck="false" placeholder="default" />
          </label>
          <label>Review model
            <input type="text" class="opt-review-model" autocapitalize="off"
              autocomplete="off" spellcheck="false" placeholder="default" />
          </label>
          <label>Review effort
            <input type="text" class="opt-review-effort" autocapitalize="off"
              autocomplete="off" spellcheck="false" placeholder="default" />
          </label>
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
      openBtn.addEventListener("click", () => openRunOptions(dialog));
      const cancel = dialog.querySelector(".cancel-run-options");
      if (cancel) cancel.addEventListener("click", () => dialog.close());
    }
    const startForm = document.querySelector(".start-form");
    if (startForm) startForm.addEventListener("submit", onStartSubmit);
    const stopForm = document.querySelector(".stop-form");
    if (stopForm) stopForm.addEventListener("submit", onStopSubmit);
  }

  // Open the options dialog. Prefer the native modal; fall back to a plain open
  // attribute on the rare browser without <dialog>.showModal so the form is
  // still reachable.
  function openRunOptions(dialog) {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
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
  // orchestrator default and never reaches the argv.
  function buildExtraArgs(form) {
    const args = ["--backend", "cc-codex"];
    for (const [flag, cls] of RUN_OPTION_FIELDS) {
      const field = form.querySelector("." + cls);
      const value = field ? field.value.trim() : "";
      if (value) args.push(flag, value);
    }
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
      if (dialog && dialog.open) dialog.close();
      router(); // now shows the live run + Stop control
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      button.disabled = false;
      statusEl.classList.add("error-text");
      statusEl.textContent = controlErrorText(err, "Failed to start the run.");
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
    }
  }

  async function renderTaskDetail(repoName, taskId) {
    view().innerHTML = centerHtml("Loading task…");
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
        ? `<pre class="task-content">${escapeHtml(detail.content)}</pre>`
        : `<p class="muted">The task file is absent.</p>`
    }</section>`;

    view().innerHTML = `
      <a class="back-link" href="${repoHash(repoName)}">← ${escapeHtml(
        repoName
      )}</a>
      <div class="detail-head">
        <span class="task-id big">${escapeHtml(detail.task_id)}</span>
        ${badge(detail.status, statusBadgeKind(detail.status))}
      </div>
      ${detail.summary ? `<p class="summary">${escapeHtml(detail.summary)}</p>` : ""}
      ${blockers}
      ${unknownNote}
      ${runStatusSection(run)}
      ${runControlsSection(repoName, detail, run, busyRun, readiness)}
      ${answerSection(run ? run.run_id : "", questions)}
      ${contentSection}`;

    wireAnswerForms();
    wireRunControls();
  }

  // ---- run history ----
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

  function historyRunCard(run) {
    const meta = [run.repo_name, run.task_id]
      .filter(Boolean)
      .map(escapeHtml)
      .join(" · ");
    const kv = runMetaKv(run);
    return `
      <li class="card run">
        <a class="card-link" href="${logsHash(run.run_id)}">
          <div class="card-head">
            <span class="run-id">${escapeHtml(run.run_id)}</span>
            <span class="badges">${runBadge(run.state)}</span>
          </div>
          ${meta ? `<div class="muted">${meta}</div>` : ""}
          ${kv ? `<dl class="kv">${kv}</dl>` : ""}
        </a>
      </li>`;
  }

  // Run history: recent runs newest-first from the history endpoint. Each run
  // taps through to its log tail.
  async function renderRuns() {
    view().innerHTML = centerHtml("Loading runs…");
    const data = await api("/api/history/runs");
    const runs = data.runs || [];
    if (!runs.length) {
      view().innerHTML = centerHtml("No runs recorded yet.");
      return;
    }
    view().innerHTML =
      `<ul class="list">` + runs.map(historyRunCard).join("") + `</ul>`;
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
    view().innerHTML = centerHtml("Loading run…");
    const run = await api("/api/runs/" + encodeURIComponent(runId)); // unknown → 404
    const live = LIVE_STATES.has(run.state);
    const back =
      run.repo_name && run.task_id
        ? `<a class="back-link" href="${taskHash(
            run.repo_name,
            run.task_id
          )}">← ${escapeHtml(run.task_id)}</a>`
        : `<a class="back-link" href="#/runs">← Runs</a>`;
    view().innerHTML = `
      ${back}
      <div class="detail-head">
        <span class="run-id big">${escapeHtml(run.run_id)}</span>
        ${runBadge(run.state)}
      </div>
      <div class="muted">${escapeHtml(
        [run.repo_name, run.task_id].filter(Boolean).join(" · ")
      )}</div>
      <div class="log-toolbar">
        <span id="log-status" class="muted"></span>
        <span class="log-actions">
          ${
            live
              ? `<button id="log-pause" class="btn-secondary" type="button">Pause</button>`
              : ""
          }
          <button id="log-reload" class="btn-secondary" type="button">Reload</button>
        </span>
      </div>
      <pre id="log-output" class="log-output"></pre>`;

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

  // ---- router ----
  function currentPath() {
    return location.hash.replace(/^#/, "") || "/repos";
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
      return renderRuns;
    }
    if (seg[0] === "tasks")
      return seg.length >= 3
        ? () => renderTaskDetail(seg[1], seg[2])
        : renderTasks;
    if (seg[0] === "repos" && seg.length >= 2)
      return () => renderRepoDetail(seg[1]);
    return renderRepos;
  }

  async function router() {
    // Leaving any view aborts an in-flight log stream so it stops reading.
    if (activeStream) {
      activeStream.abort();
      activeStream = null;
    }
    if (!getToken()) {
      renderLogin();
      return;
    }
    showChrome(true);
    const path = currentPath();
    setActiveTab(path);
    const handler = matchRoute(path);
    try {
      await handler();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      renderError(err);
    }
  }

  function signOut() {
    setToken("");
    location.hash = "";
    renderLogin();
  }

  document.getElementById("signout-btn").addEventListener("click", signOut);
  window.addEventListener("hashchange", router);
  router();
})();
