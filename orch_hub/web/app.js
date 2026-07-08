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

  // GET by default; pass { method, body } to POST JSON (e.g. answering a
  // question). The bearer header is attached to every call — the single
  // credential path the whole SPA relies on.
  async function api(path, options) {
    const opts = options || {};
    const token = getToken();
    const headers = {};
    if (token) headers["Authorization"] = "Bearer " + token;
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
      try {
        const body = await resp.json();
        if (body && body.detail) detail = body.detail;
      } catch (_) {
        /* non-JSON error body */
      }
      throw new ApiError(detail, resp.status);
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

  // A run-state badge: `waiting` warns (needs a human), other live/end states
  // ride the neutral "run" colour.
  const runBadge = (state) =>
    badge(state, state === "waiting" ? "warn" : "run");

  const centerHtml = (text) => `<p class="center">${escapeHtml(text)}</p>`;

  // Build an in-app hash link. Each dynamic segment is encoded so a repo name
  // or task id is always exactly one path segment for the router to split back.
  const repoHash = (repo) => "#/repos/" + encodeURIComponent(repo);
  const taskHash = (repo, taskId) =>
    "#/tasks/" + encodeURIComponent(repo) + "/" + encodeURIComponent(taskId);

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

  function repoCard(repo, liveRuns) {
    const readiness = repo.readiness || { ready: false, checks: [] };
    const badges = [
      readiness.ready ? badge("ready", "ok") : badge("not ready", "bad"),
    ];
    for (const run of liveRuns || []) badges.push(runBadge(run.state));
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
        <a class="card-link" href="${repoHash(repo.name)}">
          <div class="card-head">
            <span class="name">${escapeHtml(repo.name)}</span>
            <span class="badges">${badges.join("")}</span>
          </div>
          <div class="path muted">${escapeHtml(repo.path)}</div>
          ${reasons}
        </a>
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
    const badges =
      (liveRun ? runBadge(liveRun.state) : "") +
      badge(task.status, statusBadgeKind(task.status));
    return `
      <li class="card task ${sentinel ? "problem" : ""}">
        <a class="card-link" href="${taskHash(repoName, task.task_id)}">
          <div class="card-head">
            <span class="task-id">${escapeHtml(task.task_id)}</span>
            <span class="badges">${badges}</span>
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

  function renderTaskList(repos) {
    const selectedRepos = taskFilters.repo
      ? repos.filter((r) => r.name === taskFilters.repo)
      : repos;
    const cards = [];
    for (const repo of selectedRepos) {
      if (!repo.index_present) cards.push(missingIndexCard(repo.name));
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

  // ---- repo detail ----
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
      }`;

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
    let run = null;
    try {
      const runsData = await api("/api/runs");
      run = pickRunForTask(runsData.runs, repoName, taskId);
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
      ${answerSection(run ? run.run_id : "", questions)}
      ${contentSection}`;

    wireAnswerForms();
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
    if (seg[0] === "tasks")
      return seg.length >= 3
        ? () => renderTaskDetail(seg[1], seg[2])
        : renderTasks;
    if (seg[0] === "repos" && seg.length >= 2)
      return () => renderRepoDetail(seg[1]);
    return renderRepos;
  }

  async function router() {
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
