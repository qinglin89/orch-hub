"use strict";
/*
 * orch-hub mobile dashboard — vanilla SPA, no build step.
 *
 * Auth model: this shell is served publicly (client code, no secrets). The
 * bearer token lives only in this browser's localStorage; every data fetch
 * attaches `Authorization: Bearer <token>`. This keeps the API on a single
 * credential path (no cookies, no query tokens), matching the SSE log stream
 * design. A 401 clears the stored token and returns to the login gate.
 *
 * Session-1 scope: login gate + repo list + all-task list. Repo/task detail,
 * answers, and logs land in later sessions; the router already tolerates the
 * not-yet-built routes by falling back to the repo list.
 */
(function () {
  const TOKEN_KEY = "orch-hub-token";
  const LIVE_STATES = new Set(["active", "waiting"]); // run states that badge a repo
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

  // ---- token storage ----
  const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
  function setToken(value) {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  }

  // ---- API client ----
  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  async function api(path) {
    const token = getToken();
    const headers = {};
    if (token) headers["Authorization"] = "Bearer " + token;
    let resp;
    try {
      resp = await fetch(path, { headers });
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
      try {
        const body = await resp.json();
        if (body && body.detail) detail = body.detail;
      } catch (_) {
        /* non-JSON error body */
      }
      throw new ApiError(detail, resp.status);
    }
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

  const centerHtml = (text) => `<p class="center">${escapeHtml(text)}</p>`;

  function showChrome(authed) {
    document.getElementById("signout-btn").hidden = !authed;
    document.getElementById("nav").hidden = !authed;
  }

  function setActiveTab(path) {
    const tab = path.startsWith("/tasks") ? "/tasks" : "/repos";
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

  // ---- repo list ----
  function groupLiveRunsByRepo(runs) {
    const map = {};
    for (const run of runs || []) {
      if (!LIVE_STATES.has(run.state)) continue;
      (map[run.repo_name] = map[run.repo_name] || []).push(run);
    }
    return map;
  }

  function repoCard(repo, liveRuns) {
    const readiness = repo.readiness || { ready: false, checks: [] };
    const badges = [
      readiness.ready ? badge("ready", "ok") : badge("not ready", "bad"),
    ];
    for (const run of liveRuns || []) {
      badges.push(badge(run.state, run.state === "waiting" ? "warn" : "run"));
    }
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
    return `
      <li class="card repo">
        <div class="card-head">
          <span class="name">${escapeHtml(repo.name)}</span>
          <span class="badges">${badges.join("")}</span>
        </div>
        <div class="path muted">${escapeHtml(repo.path)}</div>
        ${reasons}
      </li>`;
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
      // Run badges are best-effort; a runs failure must not hide the repo list.
    }
    if (!repos.length) {
      view().innerHTML = centerHtml("No managed repos discovered.");
      return;
    }
    view().innerHTML =
      `<ul class="list">` +
      repos.map((r) => repoCard(r, runsByRepo[r.name])).join("") +
      `</ul>`;
  }

  // ---- all-task list ----
  function statusBadgeKind(status) {
    if (SENTINELS.has(status)) return "bad";
    if (status === "blocked") return "warn";
    if (status === "completed") return "ok";
    return "";
  }

  function taskCard(task, repoName) {
    const sentinel = SENTINELS.has(task.status);
    const problem = sentinel
      ? `<div class="error-text">task file problem: ${
          task.status === "missing"
            ? "listed in the index but the task file is absent"
            : "task file has no parseable frontmatter status"
        }</div>`
      : "";
    const blockers =
      task.blockers && task.blockers.length
        ? `<div class="blockers muted">blockers: ${task.blockers
            .map(escapeHtml)
            .join(", ")}</div>`
        : "";
    return `
      <li class="card task ${sentinel ? "problem" : ""}">
        <div class="card-head">
          <span class="task-id">${escapeHtml(task.task_id)}</span>
          ${badge(task.status, statusBadgeKind(task.status))}
        </div>
        <div class="repo-name muted">${escapeHtml(repoName)}</div>
        ${task.summary ? `<div class="summary">${escapeHtml(task.summary)}</div>` : ""}
        ${blockers}
        ${problem}
      </li>`;
  }

  function orderedStatuses(repos) {
    const seen = new Set();
    for (const repo of repos)
      for (const task of repo.tasks || []) seen.add(task.status);
    const known = KNOWN_STATUSES.filter((s) => seen.has(s));
    const extra = [...seen].filter(
      (s) => !KNOWN_STATUSES.includes(s)
    ); // sentinels + any future status
    extra.sort();
    return [...known, ...extra];
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

  function renderTaskList(repos) {
    const selectedRepos = taskFilters.repo
      ? repos.filter((r) => r.name === taskFilters.repo)
      : repos;
    const cards = [];
    for (const repo of selectedRepos) {
      if (!repo.index_present) {
        cards.push(
          `<li class="card problem"><div class="card-head">
             <span class="name">${escapeHtml(repo.name)}</span>
             ${badge("no index", "bad")}
           </div>
           <div class="error-text">no <code>.ai-tasks/index.md</code> — task inventory unavailable</div>
           </li>`
        );
      }
      for (const task of repo.tasks || []) {
        if (taskFilters.status && task.status !== taskFilters.status) continue;
        cards.push(taskCard(task, repo.name));
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
      renderTaskList(repos);
    });
    document.getElementById("f-status").addEventListener("change", (ev) => {
      taskFilters.status = ev.target.value;
      renderTaskList(repos);
    });
    renderTaskList(repos);
  }

  // ---- router ----
  const ROUTES = { "/repos": renderRepos, "/tasks": renderTasks };

  function currentPath() {
    return location.hash.replace(/^#/, "") || "/repos";
  }

  async function router() {
    if (!getToken()) {
      renderLogin();
      return;
    }
    showChrome(true);
    const path = currentPath();
    setActiveTab(path);
    const handler = ROUTES[path] || renderRepos; // unknown/not-yet-built → repos
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
