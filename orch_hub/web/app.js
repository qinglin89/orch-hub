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
  const SEMANTIC_JOB_POLL_MS = 3000; // eval-view poll cadence for an in-flight semantic-eval job
  const OVERVIEW_REFRESH_MS = 15000; // Overview background auto-refresh cadence (passive dashboard)
  const OVERVIEW_HISTORY_LIMIT = 12; // recent-run window fetched for the outcomes panel
  const OVERVIEW_OUTCOME_COUNT = 5; // ended runs shown in the outcomes panel
  let activeStream = null; // AbortController for the in-flight SSE log stream, if any
  let activeIntakePoll = null; // { cancelled, timer } for the in-flight intake poll, if any
  let activeSemanticJobPoll = null; // { cancelled, timer } for the in-flight semantic-job poll, if any
  let activeOverviewRefresh = null; // { cancelled, timer } for the Overview auto-refresh, if any
  let routeGeneration = 0; // bumped by every router() entry; see routeGuard()
  let navByKeyboard = false; // last input was a key press → focusPageStart shows the heading ring
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
  // Async semantic-evaluation job lifecycle (mirror orch_hub.semantic_jobs). The
  // transient pair brackets an in-flight model turn the eval view polls through;
  // `completed`/`failed`/`error` are terminal. A `completed` job links the fresh
  // evaluation the view then selects; `error` (a corrupt job record) is handled as
  // terminal/idle-with-retry like `failed`.
  const SEMANTIC_JOB_TRANSIENT = new Set(["queued", "running"]);

  // ---- token storage ----
  const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
  function setToken(value) {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  }

  // ---- appearance: mode (theme) + palette ----
  // Appearance has two INDEPENDENT axes. Mode (Dark/Light/System) is below; palette
  // (Neon Cyan / Wasteland Orange) is further down. Each persists under its own key
  // and resolves to a concrete `<html>` attribute (data-theme / data-palette) —
  // matching styles.css and the pre-paint script in index.html that applies both
  // before the first frame (no startup flash). This code re-applies the same logic,
  // owns the header Appearance popover, and follows OS scheme changes while Mode is
  // System. Every browser reach is guarded so the jsc keyboard harness — which stubs
  // no matchMedia, documentElement, or getComputedStyle — can drive this unchanged.
  //
  // Mode: Dark is the first-visit default and the safe fallback.
  const THEME_KEY = "orch-hub-theme";
  const THEME_CHOICES = new Set(["dark", "light", "system"]);

  // The saved preference, defaulting to Dark when unset, invalid, or unreadable.
  function getThemePreference() {
    let stored = null;
    try {
      stored = localStorage.getItem(THEME_KEY);
    } catch (_) {
      return "dark"; // storage unavailable → safe default
    }
    return THEME_CHOICES.has(stored) ? stored : "dark";
  }

  function setThemePreference(pref) {
    try {
      if (THEME_CHOICES.has(pref)) localStorage.setItem(THEME_KEY, pref);
    } catch (_) {
      /* storage unavailable: the in-memory applyTheme below still takes effect */
    }
  }

  function systemPrefersDark() {
    try {
      return (
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-color-scheme: dark)").matches
      );
    } catch (_) {
      return false;
    }
  }

  // Resolve a preference to the scheme actually painted: System follows the OS,
  // Light/Dark are themselves, and anything unexpected falls back to Dark.
  function resolveTheme(pref) {
    if (pref === "light") return "light";
    if (pref === "system") return systemPrefersDark() ? "dark" : "light";
    return "dark";
  }

  // Point the mobile browser-chrome tint at the resolved theme's surface colour,
  // read from the live CSS token so it never drifts from the stylesheet.
  function applyThemeColorMeta() {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (!meta || typeof window.getComputedStyle !== "function") return;
    let color = "";
    try {
      color = window
        .getComputedStyle(document.documentElement)
        .getPropertyValue("--surface")
        .trim();
    } catch (_) {
      return;
    }
    if (color) meta.setAttribute("content", color);
  }

  // Apply the current preference to `<html data-theme>` (which drives the CSS token
  // set and color-scheme) and keep the browser-chrome meta in step.
  function applyTheme() {
    const root = document.documentElement;
    if (root && typeof root.setAttribute === "function")
      root.setAttribute("data-theme", resolveTheme(getThemePreference()));
    applyThemeColorMeta();
  }

  // Palette: a SECOND, independent appearance axis. Neon Cyan is the first-visit
  // default and safe fallback; an explicit choice (Neon Cyan / Wasteland Orange)
  // persists under its own key and resolves to `<html data-palette>`, which
  // styles.css layers over the mode as a token preset. Mode owns light/dark; palette
  // owns the accent hue and atmosphere — a change to one never touches the other.
  // Migration is implicit: an existing Dark/Light/System user has no palette key, so
  // they get Neon Cyan while their mode stays exactly as saved.
  const PALETTE_KEY = "orch-hub-palette";
  const PALETTE_CHOICES = new Set(["neon-cyan", "wasteland-orange"]);

  function getPalettePreference() {
    let stored = null;
    try {
      stored = localStorage.getItem(PALETTE_KEY);
    } catch (_) {
      return "neon-cyan"; // storage unavailable → safe default
    }
    return PALETTE_CHOICES.has(stored) ? stored : "neon-cyan";
  }

  function setPalettePreference(pref) {
    try {
      if (PALETTE_CHOICES.has(pref)) localStorage.setItem(PALETTE_KEY, pref);
    } catch (_) {
      /* storage unavailable: the in-memory applyPalette below still takes effect */
    }
  }

  // Apply the current palette to `<html data-palette>` (which selects the token
  // preset). The palette can shift --surface (warm vs graphite), so keep the
  // browser-chrome meta in step too.
  function applyPalette() {
    const root = document.documentElement;
    if (root && typeof root.setAttribute === "function")
      root.setAttribute("data-palette", getPalettePreference());
    applyThemeColorMeta();
  }

  // Wire the header Appearance popover (mode + palette selects) and follow OS scheme
  // changes while Mode is System. The controls live in the static shell (outside
  // #view), so they are wired once at startup and survive every route repaint.
  function initAppearance() {
    const themeSelect = document.getElementById("theme-select");
    if (themeSelect) {
      themeSelect.value = getThemePreference();
      themeSelect.addEventListener("change", () => {
        setThemePreference(themeSelect.value);
        applyTheme();
      });
    }
    const paletteSelect = document.getElementById("palette-select");
    if (paletteSelect) {
      paletteSelect.value = getPalettePreference();
      paletteSelect.addEventListener("change", () => {
        setPalettePreference(paletteSelect.value);
        applyPalette();
      });
    }
    initAppearancePopover();
    try {
      if (typeof window.matchMedia === "function") {
        const mq = window.matchMedia("(prefers-color-scheme: dark)");
        const follow = () => {
          if (getThemePreference() === "system") applyTheme();
        };
        if (typeof mq.addEventListener === "function")
          mq.addEventListener("change", follow);
        else if (typeof mq.addListener === "function") mq.addListener(follow); // older Safari
      }
    } catch (_) {
      /* no matchMedia: System just won't live-update while the page is open */
    }
    applyTheme();
    applyPalette();
  }

  // Disclosure behaviour for the Appearance popover: an icon-sized toggle reveals a
  // panel holding the two preference selects, so both axes fit a 320px header. Built
  // by hand (a real <button aria-expanded> + a hidden panel) rather than <details>
  // so the toggle is an ordinary focusable button; it closes on Escape (focus
  // returns to the toggle) and on an outside click. Every browser reach is guarded
  // so the jsc harness can drive it.
  function initAppearancePopover() {
    const toggle = document.getElementById("appearance-toggle");
    const panel = document.getElementById("appearance-panel");
    const root = document.querySelector(".appearance");
    if (!toggle || !panel) return;
    const isOpen = () => toggle.getAttribute("aria-expanded") === "true";
    const setOpen = (open) => {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      panel.hidden = !open;
    };
    toggle.addEventListener("click", () => setOpen(!isOpen()));
    if (root && typeof root.addEventListener === "function") {
      root.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape" && isOpen()) {
          setOpen(false);
          if (typeof toggle.focus === "function") toggle.focus();
        }
      });
    }
    if (typeof document.addEventListener === "function") {
      // An outside click closes it. The toggle's own click runs first (target
      // phase) and opens the panel; this bubble-phase handler then sees the target
      // inside .appearance and leaves it open.
      document.addEventListener("click", (ev) => {
        if (isOpen() && root && !root.contains(ev.target)) setOpen(false);
      });
    }
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

  // Format an ISO timestamp the hub passed through verbatim (a commit's %cI, a
  // usage observation, a report's catalog stamp) for at-a-glance display: a
  // compact relative age for recent ones, falling back to the calendar date for
  // older ones and to the raw string if it can't be parsed — trust the hub's
  // value, but never render "NaN".
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

  // One paging control, shared by the two paged views (run history's
  // server-anchored window walk and the report feed's forward-only cursor). They
  // differ in what enables a direction and in what their status line says, not in
  // what a page button IS — so the markup lives here once rather than being
  // copied per pager. The arrow is decorative (aria-hidden) and the
  // visually-hidden " page" makes the accessible name "Previous page" / "Next
  // page" while the visible label stays short enough for a phone.
  const pagerButton = (id, label, arrow, enabled, leading) =>
    `<button id="${id}" class="pager-btn" type="button"${
      enabled ? "" : " disabled"
    }>${leading ? `<span aria-hidden="true">${arrow}</span> ` : ""}${label}${
      leading ? "" : ` <span aria-hidden="true">${arrow}</span>`
    }<span class="visually-hidden"> page</span></button>`;

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
  // A task's evaluation view, keyed by (repo, task). Not index-gated: the backend
  // resolves the task active-then-archive, so a completed/archived task stays
  // evaluable. An optional trailing evaluation id selects a specific past
  // evaluation; omitting it shows the latest.
  const evalHash = (repo, taskId) =>
    "#/evaluations/" + encodeURIComponent(repo) + "/" + encodeURIComponent(taskId);

  // Where selecting a run should land: its TASK, not its log tail. A run is a
  // dev/review loop over one task file, so the task is the thing being worked
  // on — controls, pending questions, session strips, the file itself — while
  // the log tail is one detail of it, reachable onward from either task
  // destination's "View logs" link. Every run surface (Overview current
  // activity, Run history, the log view's own back-link) resolves the
  // destination here so the rule is stated once.
  //
  // Which task route depends on liveness. A live (active/waiting) run with full
  // repo+task attribution opens the complete task detail, which is index-gated
  // (active tasks only) but carries the run controls and answer forms a live run
  // needs. Anything else — an ended run, whose task close-out may already have
  // archived, or a live run missing repo attribution — uses the run-scoped task
  // view, which resolves the file through the run record active-then-archive and
  // so keeps opening after archival instead of 404-ing.
  //
  // A run carrying no task id has no task destination at all: null, leaving each
  // caller to fall back to what it can still show.
  function runTaskDestination(run) {
    if (!run || !run.task_id) return null;
    if (LIVE_STATES.has(run.state) && run.repo_name)
      return taskHash(run.repo_name, run.task_id);
    return runTaskHash(run.run_id);
  }

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
    // Task evaluation is task-scoped and reached from a Run-history task group,
    // but the sidecar itself stays under the Tasks information architecture; the
    // cross-repo report feed is the same subsystem seen from above, so it keeps
    // the same tab rather than claiming one of its own.
    else if (path.startsWith("/evaluations")) tab = "/tasks";
    else if (path.startsWith("/reports")) tab = "/tasks";
    else if (path.startsWith("/runs")) tab = "/runs";
    document.querySelectorAll(".tabbar a").forEach((a) => {
      const active = a.dataset.tab === tab;
      a.classList.toggle("active", active);
      if (active) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
  }

  // The per-view width class the desktop shell reads off #view (styles.css):
  // overview + the flat card lists fill the workspace, detail/form views keep a
  // bounded reading column, logs and run history get the wider column. Derived
  // from the same path segments matchRoute keys on (the fixed route keywords, not
  // the encoded ids), so the whole view adopts its width in one place per
  // navigation instead of each render function repeating it. Below the desktop
  // breakpoint every kind renders at the single mobile/tablet column, so the
  // class is inert there.
  function viewKind(path) {
    const seg = path.split("/").filter(Boolean);
    if (seg[0] === "runs")
      return seg.length >= 3 && seg[2] === "task" ? "read" : "wide";
    if (seg[0] === "tasks") return seg.length >= 3 ? "read" : "list";
    if (seg[0] === "repos") return seg.length >= 2 ? "read" : "list";
    if (seg[0] === "intakes") return "read";
    if (seg[0] === "evaluations") return "read";
    // A paged chronological feed, like run history: the wider column, and NOT the
    // "list" kind, whose top-level card grid would break a strictly ordered feed
    // into columns.
    if (seg[0] === "reports") return "wide";
    if (seg[0] === "overview") return "overview";
    return "list"; // default repo list
  }

  function setViewKind(path) {
    view().className = "view view--" + viewKind(path);
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
  //
  // Show the landing focus ring only for a keyboard-driven navigation. A
  // touch/mouse navigation still moves focus here (Tab order + assistive tech
  // need a real landing point), but iOS Safari would otherwise paint a
  // :focus-visible ring on this programmatic focus even after a tap — a stray
  // blue box on the title. `.nav-focus-ring` gates the ring in styles.css; the
  // class is re-evaluated on every landing (navByKeyboard is set from the last
  // real input, below), so a heading focused after a tap stays ring-free.
  function focusPageStart() {
    const target = document.querySelector("#view .page-title") || view();
    if (!target || typeof target.focus !== "function") return;
    if (target.classList) target.classList.toggle("nav-focus-ring", navByKeyboard);
    target.focus();
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

  // Staleness guard for a view that paints after an await. Every route handler
  // fetches before it paints, so a slow route can resolve long after the user
  // navigated on — and writing `#view` then puts the old route's content under
  // the new route's URL, tab bar and document.title. A handler captures the
  // route generation at entry (before its first await) and calls the returned
  // predicate before painting; router() bumps the generation on every entry, so
  // a superseded handler drops its paint.
  //
  // Re-entering a handler WITHOUT a navigation (the log view's Reload, the
  // intake control re-renders, the intake poll's settle) captures the live
  // generation and is therefore never superseded — those repaints still land.
  //
  // WHERE the capture happens is the whole contract. An action that re-enters a
  // handler only AFTER its own request resolves (the intake controls post first,
  // then re-render) has to capture before that request: a guard taken at the
  // late re-entry captures the generation of the route the user has meanwhile
  // moved to, so it can never see itself as stale. Capture when the user acts.
  function routeGuard() {
    const generation = routeGeneration;
    return () => generation !== routeGeneration;
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
    // Clear any prior route's width class: the login card centres itself and the
    // desktop rail is hidden, so the workspace should be the plain full-width view.
    view().className = "view";
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

  // Whether a repo has a live (active or waiting) run in a raw /api/runs list.
  // The display-readiness softening below keys off this: a dirty working tree is
  // only excused while the orchestrator is actually running.
  function repoHasLiveRun(runs, repoName) {
    return (runs || []).some(
      (r) => r.repo_name === repoName && LIVE_STATES.has(r.state)
    );
  }

  // The one shared READY/NOT READY *presentation* rule, derived from raw repo
  // readiness plus live-run state. A repo displays as ready when it is genuinely
  // raw-ready, OR when a live run is in flight and its ONLY raw failure is
  // `git_clean`: a dirty tree is an expected transient while a run edits the
  // working copy, not an operator problem. This softens the display ONLY — every
  // backend/launch gate (run start, intake, the task-detail Start eligibility)
  // keeps reading raw `readiness.ready` and is never relaxed here. Missing
  // readiness (an unavailable /api/repos overlay) stays not-ready, matching the
  // raw default; once the run ends `hasLiveRun` is false, so a still-dirty tree
  // reverts to NOT READY. Active-task presence is deliberately NOT an input —
  // only a live run qualifies.
  function displayReady(readiness, hasLiveRun) {
    if (!readiness) return false;
    if (readiness.ready) return true;
    if (!hasLiveRun) return false;
    const checks = readiness.checks || [];
    return checks.length === 1 && checks[0] && checks[0].name === "git_clean";
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
    // Present readiness through the shared display rule: a live run whose sole
    // raw failure is a dirty tree reads as READY (the run signal below is the
    // independent dimension that says a run is in flight). `liveRuns` is already
    // this repo's live-run slice, so a non-empty array is "has a live run".
    const ready = displayReady(readiness, !!(liveRuns && liveRuns.length));
    // The tri-state run signal (yellow=waiting / green=active / gray=idle) is a
    // compact dot leading the repo name (see the card head below), always
    // present so "no live run" reads as an explicit gray idle rather than an
    // absence. The readiness pill keeps the right of the head to itself.
    const badges = ready ? badge("ready", "ok") : badge("not ready", "bad");
    const reasons = ready
      ? ""
      : `<ul class="reasons">${(readiness.checks || [])
          .map(
            (c) =>
              `<li><code>${escapeHtml(c.name)}</code> ${escapeHtml(
                c.message
              )}</li>`
          )
          .join("")}</ul>`;
    // Display-ready repos surface at-a-glance metadata (latest commit + active
    // task count); not-ready repos keep their failure reasons instead.
    const meta = ready ? repoMetaLine(repo.commit, taskCount) : "";
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
    const superseded = routeGuard();
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
    if (superseded()) return; // a newer route owns the view
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
    const superseded = routeGuard();
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
    // Before the filter reconciliation below, so a superseded route neither
    // paints nor edits the shared filter state the live route is showing.
    if (superseded()) return;
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
    const superseded = routeGuard();
    view().innerHTML = loadingView(repoName, "Loading " + repoName + "…");
    // Required: this repo's task inventory (single-repo envelope). An unknown
    // repo is a 404 the router surfaces as an error view.
    const data = await api("/api/repos/" + encodeURIComponent(repoName) + "/tasks");
    if (superseded()) return; // guards the not-found paint just below
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
    let liveRun = null;
    try {
      const runsData = await api("/api/runs");
      liveByTask = liveRunsByTask(runsData.runs, repoName);
      // `repoLiveRun` and `repoHasLiveRun` share one LIVE_STATES predicate, so
      // this is the same boolean the readiness softening always consumed — it is
      // just kept alongside the run itself, which the intake section links to.
      liveRun = repoLiveRun(runsData.runs, repoName);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
    }
    const hasLiveRun = !!liveRun;
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
      inflightIntake = repoInflightIntake(intakesData.intakes, repoName);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
    }
    // The best-effort overlays above added more awaits, so re-check before the
    // paint (and before the shared status filter is reconciled).
    if (superseded()) return;

    const tasks = entry.tasks || [];
    const statuses = orderedStatusesFromTasks(tasks);
    if (repoDetailStatus && !statuses.includes(repoDetailStatus))
      repoDetailStatus = "";

    // Present readiness through the shared display rule; the lower intake
    // section still re-derives from raw `readiness` so its gate stays unchanged.
    const ready = readiness ? displayReady(readiness, hasLiveRun) : null;
    const readyBadge = readiness
      ? ready
        ? badge("ready", "ok")
        : badge("not ready", "bad")
      : "";
    const reasons =
      readiness && !ready
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
      ${intakeSection(repoName, inflightIntake, readiness, liveRun)}
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

  // ---- repo admission (a live run and a task intake never share a repo) ----
  //
  // Backend contract (orch_hub.admission): every repo is claimed by EITHER a
  // live run OR a non-terminal remote intake, decided in one per-repo critical
  // section. The loser of a race gets a 409 carrying a stable `conflict`
  // discriminator plus the blocking id — `run_live`/`run_id` for a refused
  // intake, `intake_in_flight`/`intake_id` for a refused run. The dashboard
  // pre-reflects that claim so an operator is never offered a control the hub
  // would refuse; the 409 stays the authority for anything raced.
  //
  // Admission is NOT readiness. A live run rewrites the working tree, so the
  // repo's raw readiness genuinely fails `git_clean` for its whole duration —
  // explaining a refused intake with that failure would make readiness a proxy
  // for "claimed", which it isn't. So each note below is rendered BEFORE its
  // view's readiness branch (mirroring the backend, which checks admission
  // before its readiness gate), and none of this touches READY/NOT READY, the
  // Needs-attention list, or the idle/running/waiting run signals.

  // The repo's one non-terminal intake, if any — the intake-side mirror of
  // repoLiveRun. Every status outside INTAKE_TERMINAL holds the repo: a draft
  // resting in `awaiting_review` claims it exactly like an in-flight turn does,
  // because confirming it writes `.ai-tasks/`.
  function repoInflightIntake(intakes, repoName) {
    return (
      (intakes || []).find(
        (i) => i.repo_name === repoName && !INTAKE_TERMINAL.has(i.status)
      ) || null
    );
  }

  // "A run holds this repo, so <refused>". Links to the run's task detail, which
  // is where the Stop control lives, so the note also names the way out.
  function runHoldsRepoNote(repoName, run, refused) {
    return `<p class="muted admission-note">A run is live on this repo
      (<a class="text-link" href="${taskHash(
        repoName,
        run.task_id
      )}">${escapeHtml(run.task_id)}</a> ${runBadge(
      run.state
    )}), so ${refused}: a run and a task intake can't share a repo.</p>`;
  }

  // "A task intake holds this repo, so <refused>". Links to its review view,
  // where Confirm or Cancel settles the intake and frees the repo.
  function intakeHoldsRepoNote(intake, refused) {
    return `<p class="muted admission-note">A task intake is in flight for this
      repo ${badge(
        intake.status,
        intakeBadgeKind(intake.status)
      )}, so ${refused}: a run and a task intake can't share a repo.
      <a class="text-link" href="${intakeHash(
        intake.intake_id
      )}">Review the intake →</a></p>`;
  }

  // A refusal that came from repo admission, recognized by the stable `conflict`
  // discriminator rather than by parsing the message. Both start paths repaint
  // on one: the blocking id is only useful as a link, which a text status line
  // can't carry, and the repaint renders exactly the pre-reflected explanation
  // the view would have shown had the claim been visible on load.
  function isAdmissionConflict(err) {
    return !!(
      err instanceof ApiError &&
      err.body &&
      (err.body.conflict === "run_live" ||
        err.body.conflict === "intake_in_flight")
    );
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
  //
  // A navigation during the re-render's fetch makes it stale like any route
  // completion: the handler then skips its own paint, so landing on (or erroring
  // over) the newer view would be the same theft the route guard exists to stop.
  function rerender(promise) {
    const superseded = routeGuard();
    promise
      .then(() => {
        if (superseded()) return; // a navigation owns the view now
        landOnView();
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) return; // login shown
        if (superseded()) return;
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
  // live run elsewhere in the repo blocks Start (busy); a non-terminal remote
  // intake blocks Start (repo admission); otherwise Start is offered only when
  // the task status and (best-effort) readiness look launchable — every hidden
  // Start is still backed by the backend 409 if raced.
  function runControlsSection(repoName, detail, run, busyRun, readiness, intake) {
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

    // Repo admission: a non-terminal intake claims the repo, so a start would be
    // refused with 409 `intake_in_flight`. Placed after the two live-run branches
    // on purpose — when both hold the repo (a recovered overlap the backend can
    // refuse but not undo) the run is the stronger fact and Stop, offered above,
    // is what clears it. This branch precedes the readiness one because a claimed
    // repo is not an unready repo.
    if (intake) {
      return (
        head +
        intakeHoldsRepoNote(intake, "a run can't start") +
        `<p class="muted">Confirming or cancelling the intake frees the repo.</p>` +
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
    // Start opens a launch-options dialog rather than launching immediately. The
    // dialog is driven by the repo's OWN deployed orchestrator: on open it queries
    // GET /api/repos/{repo}/orchestrator-config (`--print-config`) and shows the
    // resolved model/effort values for the selected Backend + role profiles, so
    // orch-hub never hardcodes model policy. DEV and REVIEW select independently
    // from the same four modes: Default (inherit the orchestrator config),
    // Standard / Excellent (named profiles), and Custom (explicit overrides). A
    // preset role is read-only; only a Custom role exposes editable fields, and
    // only for that role. Backend stays shared — one run has one backend. The
    // launch payload is the structured `launch` object carrying
    // `dev_mode`/`review_mode`; the server turns it into argv via
    // build_launch_args, so the per-role/backend rules live in one tested place.
    // The plan-gate toggle is orthogonal to the role selections and stays
    // available in every mode. The blocked-task acknowledgement moves inside
    // the dialog, so it is confirmed together with the launch. Only one options
    // dialog is on the page at a time, so fixed field ids are safe; the modal is
    // named by its own heading through aria-labelledby.
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
          <p class="muted opts-note">Values come from this repo's deployed
            orchestrator. DEV and REVIEW pick their profile independently; preset
            profiles are read-only and Custom overrides that role's applicable
            fields.</p>
          <label for="opt-backend">Backend
            <select id="opt-backend" class="opt-backend">
              <option value="">Inherit (orchestrator default)</option>
              <option value="cursor">Cursor</option>
              <option value="cc-codex">cc-codex</option>
            </select>
          </label>
          <div class="role-modes">
            ${roleModeField("dev", "DEV")}
            ${roleModeField("review", "REVIEW")}
          </div>
          <p class="muted profiles-note" role="status"></p>
          <div class="config-panel" data-state="idle"></div>
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
    if (startForm) {
      startForm.addEventListener("submit", onStartSubmit);
      // A role or backend change re-queries the orchestrator and repaints the
      // panel; the Custom fields inside the panel deliberately do not (below).
      startForm.querySelectorAll(".opt-role-mode").forEach((sel) => {
        sel.addEventListener("change", onLaunchRoleChange);
      });
      const backendSel = startForm.querySelector(".opt-backend");
      if (backendSel)
        backendSel.addEventListener("change", onLaunchBackendChange);
    }
    const stopForm = document.querySelector(".stop-form");
    if (stopForm) stopForm.addEventListener("submit", onStopSubmit);
  }

  // The control that opened the current dialog, so closing it can hand focus
  // back instead of dropping the keyboard user at the top of the document.
  let dialogOpener = null;

  // Live launch-dialog state, reset on each open. `dev`/`review`/`backend` mirror
  // the three selects (backend "" = inherit); `config` is the last EffectiveConfig
  // the orchestrator returned for the current selection; `available` is its
  // available_profiles; `split` is the deployment's per-role-profile capability
  // as reported by the query (`false` gates the whole split dialog); `resolved` is
  // the orchestrator's own configured backend (learned from the inherit query),
  // used to seed an explicit backend when a role stops being Default; `custom`
  // holds the operator's local per-role edits so a repaint cannot wipe them.
  // `gen` guards against a slow earlier query painting over a newer selection;
  // `cache` avoids re-querying an already-seen (backend, dev, review) within one
  // dialog session.
  let launchCfg = null;

  // The four launch modes, offered per role. Mirrors orch_config.LAUNCH_MODES.
  const LAUNCH_MODES = ["default", "standard", "excellent", "custom"];

  // The two modes that name an orchestrator profile; the other two are UI-only
  // and are never sent as --dev-profile/--review-profile.
  const NAMED_PROFILES = ["standard", "excellent"];

  // The committed orchestrator default backend (per the deployed handoff), used
  // only to seed a non-Default role before the first query resolves a real one.
  const DEFAULT_BACKEND = "cc-codex";

  // One role's profile selector. Both roles offer the same four modes and are
  // independent; `data-role` carries which role a change belongs to.
  function roleModeField(key, name) {
    const options = LAUNCH_MODES.map(
      (m) =>
        `<option value="${m}">${m.charAt(0).toUpperCase()}${m.slice(1)}</option>`
    ).join("");
    return (
      `<label for="opt-${key}-mode">${name} profile` +
      `<select id="opt-${key}-mode" class="opt-role-mode" data-role="${key}">` +
      `${options}</select></label>`
    );
  }

  const orchConfigPath = (repo) =>
    "/api/repos/" + encodeURIComponent(repo) + "/orchestrator-config";

  // Open the options dialog. Reset the three selects + state, kick the initial
  // Default query (fire-and-forget — the panel paints when it resolves), then
  // prefer the native modal (which scopes focus and handles Escape); fall back to
  // a plain open attribute on the rare browser without <dialog>.showModal,
  // focusing the first field by hand.
  function openRunOptions(dialog, opener) {
    dialogOpener = opener || null;
    const form = dialog.querySelector(".start-form");
    if (form) {
      form.querySelectorAll(".opt-role-mode").forEach((sel) => {
        sel.value = "default";
        sel.disabled = false;
      });
      const backendSel = form.querySelector(".opt-backend");
      if (backendSel) backendSel.value = "";
      launchCfg = {
        repo: form.dataset.repo,
        dev: "default",
        review: "default",
        backend: "",
        config: null,
        available: [],
        split: null, // unknown until the first query answers
        resolved: DEFAULT_BACKEND,
        loading: false,
        error: null,
        gen: 0,
        cache: {},
        custom: { dev: {}, review: {} },
      };
      syncInheritOption();
      // Both roles start Default, so this first query sends no role parameters:
      // an older deployment that cannot take --dev-profile/--review-profile can
      // still answer it, and its response is what reveals the capability.
      refreshLaunchConfig();
    }
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

  // One role's profile changed. Roles are independent, so only this role's
  // selection moves; leaving Custom drops that role's local edits so re-entering
  // Custom re-seeds from the orchestrator instead of resurrecting stale text.
  // Then re-query for the whole (backend, dev, review) selection at once.
  function onLaunchRoleChange(ev) {
    if (!launchCfg) return;
    const select = ev.currentTarget;
    const role = select.dataset.role;
    if (role !== "dev" && role !== "review") return;
    launchCfg[role] = select.value;
    if (select.value !== "custom") launchCfg.custom[role] = {};
    seedBackend();
    syncInheritOption();
    refreshLaunchConfig();
  }

  // Backend changed: re-query for the new backend. Inherit is only valid when
  // both roles are Default, so a manual pick of it otherwise reverts to an
  // explicit backend (belt-and-suspenders with the disabled Inherit option).
  function onLaunchBackendChange() {
    if (!launchCfg) return;
    const backendSel = document.querySelector(".opt-backend");
    launchCfg.backend = backendSel ? backendSel.value : "";
    seedBackend();
    refreshLaunchConfig();
  }

  // Both roles Default is the only selection that may inherit the backend
  // (build_launch_args rejects an inherited backend once any role is named or
  // custom, since that role resolves against one). Pin the resolved backend the
  // moment a role leaves Default.
  function allRolesDefault() {
    return (
      !!launchCfg && launchCfg.dev === "default" && launchCfg.review === "default"
    );
  }

  function seedBackend() {
    if (!launchCfg || allRolesDefault() || launchCfg.backend) return;
    launchCfg.backend = launchCfg.resolved || DEFAULT_BACKEND;
    const backendSel = document.querySelector(".opt-backend");
    if (backendSel) backendSel.value = launchCfg.backend;
  }

  function syncInheritOption() {
    const opt = document.querySelector('.opt-backend option[value=""]');
    if (opt) opt.disabled = !!launchCfg && !allRolesDefault();
  }

  // The profile flag a role selection sends, or "" for the two UI-only modes:
  // Default inherits and Custom overrides after resolving against the inherited
  // role, so neither names a profile.
  function namedProfile(mode) {
    return NAMED_PROFILES.indexOf(mode) === -1 ? "" : mode;
  }

  // Query the orchestrator for the current (backend, dev, review) and repaint the
  // panel. Both role selections go into ONE query, so the preview is a single
  // coherent configuration: a Custom role is asked for as inherited while the
  // other role's named profile still applies, which is exactly what its editable
  // fields are seeded from. A newer selection (bumped gen) makes a slower earlier
  // response inert. A query failure disables Launch and surfaces the
  // orchestrator's own error — never a hub default.
  async function refreshLaunchConfig() {
    const st = launchCfg;
    if (!st) return;
    const gen = ++st.gen;
    const backend = st.backend; // "" = inherit
    const devProfile = namedProfile(st.dev);
    const reviewProfile = namedProfile(st.review);
    st.loading = true;
    st.error = null;
    renderConfigPanel();
    const key = backend + "|" + devProfile + "|" + reviewProfile;
    if (st.cache[key]) {
      applyConfig(st, st.cache[key], backend, devProfile, reviewProfile);
      renderConfigPanel();
      return;
    }
    let url = orchConfigPath(st.repo);
    const qs = [];
    if (backend) qs.push("backend=" + encodeURIComponent(backend));
    if (devProfile) qs.push("dev_profile=" + encodeURIComponent(devProfile));
    if (reviewProfile)
      qs.push("review_profile=" + encodeURIComponent(reviewProfile));
    if (qs.length) url += "?" + qs.join("&");
    try {
      const config = await api(url);
      if (gen !== st.gen) return; // a newer selection owns the panel now
      st.cache[key] = config;
      applyConfig(st, config, backend, devProfile, reviewProfile);
      renderConfigPanel();
    } catch (err) {
      if (gen !== st.gen) return;
      if (err instanceof ApiError && err.status === 401) return; // login shown
      st.loading = false;
      st.config = null;
      st.error = controlErrorText(
        err,
        "Could not read this repo's orchestrator configuration."
      );
      renderConfigPanel();
    }
  }

  // Fold a successful query into state: store the config + available profiles,
  // record the per-role-profile capability, remember the resolved backend from
  // the inherit query (to seed a role leaving Default), and refresh the role
  // selectors. `split_profiles` is a capability, not a value: an older
  // deployment omits the orchestrator's `profiles` object entirely and the
  // server reports false after DERIVING both roles from the run-wide profile, so
  // anything but an explicit true keeps split selection closed.
  function applyConfig(st, config, backend, devProfile, reviewProfile) {
    st.loading = false;
    st.error = null;
    st.config = config;
    st.available = Array.isArray(config.available_profiles)
      ? config.available_profiles
      : [];
    st.split = config.split_profiles === true;
    if (!backend && !devProfile && !reviewProfile && config.backend)
      st.resolved = config.backend;
    syncModeAvailability();
  }

  // A read-only role value in orchestrator notation: `agent:model @ effort`, with
  // the `agent:` prefix only for a cc-codex role (a Cursor role's agent is absent
  // or the bare "cursor", which the orchestrator's own notation omits).
  function roleValueText(role) {
    role = role || {};
    const agent =
      role.agent && role.agent !== "cursor" ? role.agent + ":" : "";
    return agent + (role.model || "—") + " @ " + (role.effort || "—");
  }

  // The values a Custom role's fields show: the orchestrator's resolved config
  // for that role, overlaid with whatever the operator has already typed. The
  // overlay is what survives a repaint triggered by an unrelated selection
  // change; leaving Custom clears it, so re-entering re-seeds from the query.
  function customRoleValues(key, role) {
    return Object.assign({}, role || {}, launchCfg.custom[key] || {});
  }

  // Remember a Custom field edit as local state. Deliberately does NOT re-query:
  // the orchestrator resolves profiles, not the operator's explicit overrides.
  function onCustomFieldInput(ev) {
    const el = ev.currentTarget;
    if (!launchCfg || !el.dataset.role || !el.dataset.field) return;
    const edits = launchCfg.custom[el.dataset.role];
    if (edits) edits[el.dataset.field] = el.value;
  }

  // One role as a single compact row. A preset role shows static
  // `model @ effort` text; a Custom role shares the row between individually-
  // labelled model and effort inputs (a dev-agent select leads the row for
  // cc-codex), with a decorative `@` between them. The labels are visually
  // hidden but real, so each control is named for assistive tech without
  // stacking the row. Only the role whose own selector says Custom becomes
  // editable — the other role stays read-only in the same panel.
  function roleRow(name, key, role, custom) {
    role = role || {};
    if (!custom) {
      return (
        `<div class="role-row"><span class="role-name">${name}</span>` +
        `<span class="role-val">${escapeHtml(roleValueText(role))}</span></div>`
      );
    }
    const values = customRoleValues(key, role);
    const backend = launchCfg.backend || launchCfg.resolved;
    // A split-mode custom cc-codex DEV must send its complete agent/model/effort
    // tuple (build_launch_args requires it), so there is no "inherit" choice
    // here: an unshown agent resolved from profile/env/config would silently
    // pair with the typed model. Cursor has no separate dev agent at all.
    const agentSel =
      key === "dev" && backend === "cc-codex"
        ? `<label class="visually-hidden" for="opt-dev-agent">Dev agent</label>` +
          `<select id="opt-dev-agent" class="opt-dev-agent role-agent role-edit"` +
          ` data-role="dev" data-field="agent">` +
          ["claude", "codex"]
            .map(
              (a) =>
                `<option value="${a}"${
                  values.agent === a ? " selected" : ""
                }>${a}</option>`
            )
            .join("") +
          `</select>`
        : "";
    const field = (suffix, label, value, placeholder) =>
      `<label class="visually-hidden" for="opt-${key}-${suffix}">${name} ${label}</label>` +
      `<input id="opt-${key}-${suffix}" class="opt-${key}-${suffix} role-${suffix} role-edit"` +
      ` data-role="${key}" data-field="${suffix}" type="text"` +
      ` autocapitalize="off" autocomplete="off" spellcheck="false"` +
      ` value="${escapeHtml(value || "")}" placeholder="${placeholder}" />`;
    return (
      `<div class="role-row role-row-custom"><span class="role-name">${name}</span>` +
      agentSel +
      field("model", "model", values.model, "model id") +
      `<span class="at" aria-hidden="true">@</span>` +
      field("effort", "effort", values.effort, "effort") +
      `</div>`
    );
  }

  // The `sources` provenance map (cli / profile:<name> / env:<NAME> / config) in
  // an advanced disclosure — handy when an environment override changes Default.
  function sourcesDetails(sources) {
    const keys = sources ? Object.keys(sources).sort() : [];
    if (!keys.length) return "";
    const rows = keys
      .map(
        (k) =>
          `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(sources[k]))}</dd>`
      )
      .join("");
    return (
      `<details class="cfg-sources"><summary>Provenance</summary>` +
      `<dl class="kv">${rows}</dl></details>`
    );
  }

  // Repaint the config panel for the current state: loading, a fail-closed query
  // error (Launch disabled), or the resolved DEV/REVIEW rows plus budget and
  // provenance. Custom rows are editable; preset rows are static.
  function renderConfigPanel() {
    const panel = document.querySelector(".config-panel");
    if (!panel || !launchCfg) return;
    const st = launchCfg;
    if (st.loading) {
      panel.dataset.state = "loading";
      panel.innerHTML =
        '<p class="muted cfg-status" role="status">Querying the orchestrator…</p>';
      setLaunchEnabled(false);
      return;
    }
    if (st.error) {
      panel.dataset.state = "error";
      panel.innerHTML =
        `<p class="error-text cfg-status" role="status">${escapeHtml(
          st.error
        )}</p>` +
        `<p class="muted">Start is disabled until the query succeeds — an older
         deployment without <code>--print-config</code> must be redeployed.</p>`;
      setLaunchEnabled(false);
      return;
    }
    const cfg = st.config;
    if (!cfg) {
      panel.dataset.state = "idle";
      panel.innerHTML = "";
      setLaunchEnabled(false);
      return;
    }
    // An orchestrator that does not report per-role `profiles` cannot take
    // --dev-profile/--review-profile at all, so the split dialog stays closed:
    // its legacy configuration still renders (and run history stays readable),
    // but Launch is disabled rather than guessing at unsupported flags.
    const unsupported = st.split === false;
    panel.dataset.state = unsupported ? "unsupported" : "ready";
    const budget =
      `<p class="muted cfg-meta">max sessions ${
        cfg.max_sessions == null ? "—" : escapeHtml(String(cfg.max_sessions))
      } · context budget ${escapeHtml(fmtTokensK(cfg.context_budget) || "—")}</p>`;
    panel.innerHTML =
      '<div class="role-rows">' +
      roleRow("DEV", "dev", cfg.dev, !unsupported && st.dev === "custom") +
      roleRow(
        "REVIEW",
        "review",
        cfg.review,
        !unsupported && st.review === "custom"
      ) +
      "</div>" +
      budget +
      sourcesDetails(cfg.sources) +
      (unsupported
        ? `<p class="error-text cfg-status" role="status">Launch is disabled:
           redeploy this repo's orchestrator payload to start runs from the
           dashboard.</p>`
        : "");
    // Custom fields are local state: keep typed values in the dialog state so an
    // unrelated re-query's repaint cannot wipe them, without re-querying on input.
    panel.querySelectorAll(".role-edit").forEach((el) => {
      el.addEventListener("input", onCustomFieldInput);
      el.addEventListener("change", onCustomFieldInput);
    });
    setLaunchEnabled(!unsupported);
  }

  // Reflect the query onto the two role selectors. A deployment without per-role
  // profile capability disables both outright (with redeploy guidance); otherwise
  // Standard/Excellent the orchestrator does not offer are disabled per role,
  // never silently falling back to Default, and the note spells out what is
  // available.
  function syncModeAvailability() {
    const st = launchCfg;
    if (!st) return;
    const selects = document.querySelectorAll(".opt-role-mode");
    const note = document.querySelector(".profiles-note");
    if (st.split === false) {
      selects.forEach((sel) => {
        sel.disabled = true;
      });
      if (note)
        note.textContent =
          "This repo's deployed orchestrator does not report per-role profiles;" +
          " redeploy it to select DEV and REVIEW independently.";
      return;
    }
    selects.forEach((sel) => {
      sel.disabled = false;
      for (const m of NAMED_PROFILES) {
        const opt = sel.querySelector('option[value="' + m + '"]');
        if (!opt) continue;
        const ok = st.available.indexOf(m) !== -1;
        opt.disabled = !ok;
        opt.textContent =
          m.charAt(0).toUpperCase() + m.slice(1) + (ok ? "" : " (unavailable)");
      }
    });
    if (note) {
      const offered = NAMED_PROFILES.filter(
        (m) => st.available.indexOf(m) !== -1
      );
      note.textContent = offered.length
        ? "Named profiles available per role: " + offered.join(", ") + "."
        : "This orchestrator exposes no named profiles; Standard and Excellent are unavailable.";
    }
  }

  function setLaunchEnabled(enabled) {
    const btn = document.querySelector('.start-form button[type="submit"]');
    if (btn) btn.disabled = !enabled;
  }

  // Build the structured `launch` payload for POST /api/runs from the dialog
  // state. The dialog is a split-shape caller: it always sends the independent
  // `dev_mode`/`review_mode` (never the legacy run-wide `mode`, which stays for
  // pre-split API callers), and the server turns the selection into orchestrator
  // argv through build_launch_args, so the per-role/backend rules live in one
  // tested place, not here. Backend is shared and omitted only while both roles
  // inherit; each Custom role contributes its own explicit values. Plan-gate is
  // orthogonal and honored in every mode. Returns null (after writing an inline
  // message) when a required Custom field is blank, so a run is never posted with
  // an incomplete override set.
  function buildLaunchPayload(form, statusEl) {
    const st = launchCfg;
    const launch = { dev_mode: st.dev, review_mode: st.review };
    if (!allRolesDefault()) {
      launch.backend = st.backend || st.resolved || DEFAULT_BACKEND;
    } else if (st.backend) {
      launch.backend = st.backend;
    }
    const refuse = (text) => {
      if (statusEl) {
        statusEl.classList.add("error-text");
        statusEl.textContent = text;
      }
      return null;
    };
    const val = (cls) => {
      const el = form.querySelector("." + cls);
      return el ? el.value.trim() : "";
    };
    if (st.dev === "custom") {
      // The agent select is rendered only for cc-codex, and it always resolves to
      // one of its two options — that is how a custom cc-codex DEV carries the
      // complete tuple build_launch_args requires. Cursor renders no select and
      // never receives --dev-agent.
      const devAgent = val("opt-dev-agent");
      if (devAgent) launch.dev_agent = devAgent;
      const model = val("opt-dev-model");
      const effort = val("opt-dev-effort");
      if (!model || !effort)
        return refuse("Custom DEV needs a model and an effort.");
      launch.dev_model = model;
      launch.dev_effort = effort;
    }
    if (st.review === "custom") {
      const model = val("opt-review-model");
      const effort = val("opt-review-effort");
      if (!model || !effort)
        return refuse("Custom REVIEW needs a model and an effort.");
      launch.review_model = model;
      launch.review_effort = effort;
    }
    const planGate = form.querySelector(".opt-plan-gate");
    if (planGate && planGate.checked) launch.plan_gate = true;
    return launch;
  }

  // Start: POST /api/runs with the structured `launch` selection (the server
  // builds the argv). A success closes the dialog and re-renders (the run is now
  // live, so the view flips to Stop); a refusal — a gate 409, a bad launch 400,
  // or a blank Custom field caught locally — stays in the open dialog.
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
    const launch = buildLaunchPayload(form, statusEl);
    if (launch === null) {
      restoreFocus(button); // a local validation stop keeps the dialog open
      return;
    }
    button.disabled = true;
    try {
      await api("/api/runs", {
        method: "POST",
        body: {
          repo_id: form.dataset.repo,
          task_id: form.dataset.task,
          launch: launch,
          acknowledge_blocked: acknowledgeBlocked,
        },
      });
      if (dialog && dialog.open) closeRunOptions(dialog);
      router(); // now shows the live run + Stop control
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      // An intake claimed the repo while this dialog was open. Repaint (the same
      // way a successful launch does) so the view renders the admission
      // explanation with its link to the blocking intake — the dialog's status
      // line is text-only and would drop exactly the part worth acting on.
      if (isAdmissionConflict(err)) {
        if (dialog && dialog.open) closeRunOptions(dialog);
        router();
        return;
      }
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
    const superseded = routeGuard();
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

    // Best-effort repo-admission overlay: a non-terminal remote intake claims
    // the repo until it is confirmed or cancelled, so Start would 409. A fetch
    // failure degrades to offering Start — the backend still refuses it, and
    // hiding the control on unknown state would be worse than a raced refusal.
    let inflightIntake = null;
    try {
      const intakesData = await api("/api/intakes");
      inflightIntake = repoInflightIntake(intakesData.intakes, repoName);
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

    if (superseded()) return; // a newer route owns the view

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
      ${runControlsSection(
        repoName,
        detail,
        run,
        busyRun,
        readiness,
        inflightIntake
      )}
      ${answerSection(run ? run.run_id : "", questions)}
      ${contentSection}`;

    wireAnswerForms();
    wireRunControls();
  }

  // ---- run history ----
  //
  // One page of runs (50, server-windowed) grouped client-side by their
  // (repo, task) so a completed or archived task's run arc reads as one unit: a
  // header, an est-vs-actual rollup (dev sessions used vs the frontmatter
  // session-est, peak per-session context, summed wall-clock), then each run's
  // card with its context strips. The flat /api/history/runs list is already
  // newest-first and embeds, per run, the persisted session snapshot and the
  // task's typed session_est. Rollups therefore describe the runs of that task
  // *on this page* — a task whose runs straddle a page boundary rolls up each
  // part separately, which is the honest reading of a page-local group.

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
  // shows with an "est unknown" note and no meter. `action` is optional trailing
  // HTML (the per-task Evaluation link) that rides the summary row, right-aligned
  // and visually tied to the metrics; omit/empty leaves just the metrics.
  function taskRollup(group, action) {
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
    // The metrics and the optional action share one row (`.roll-top`): the
    // metrics grow on the left, the action is pinned right and wraps below only
    // when the width can't hold both. The meter spans the full width beneath. An
    // absent action collapses the row to just the metrics — no empty spacing.
    return (
      `<div class="task-rollup">` +
      `<div class="roll-top">` +
      `<div class="roll-line">${bits.join(" · ")}</div>` +
      (action || "") +
      `</div>` +
      meter +
      `</div>`
    );
  }

  // Shorten a sha256 revision for display (first 12 chars + ellipsis); a short or
  // absent value passes through / degrades to an em dash.
  function shortRev(rev) {
    if (!rev) return "—";
    const s = String(rev);
    return s.length > 12 ? s.slice(0, 12) + "…" : s;
  }

  // The profile each role actually launched with, as a display label. The
  // orchestrator's `profiles` object is authoritative when present; an older
  // snapshot carries only the run-wide `profile`, so both roles derive from it
  // (a legacy run applied one selection to both roles, so that reads correctly).
  // The new top-level `profile` is NOT reinterpreted — it stays the run-wide
  // selector and is normally "default" for a split launch.
  //
  // Custom is an orch-hub UI mode, not an orchestrator profile: a custom role
  // reports `profiles.<role>: "default"` and is distinguishable only by
  // provenance, where its model came from the command line. Without that check a
  // Custom run would render as Default.
  function roleProfileLabels(cfg) {
    const profiles =
      cfg.profiles && typeof cfg.profiles === "object" ? cfg.profiles : null;
    const sources = cfg.sources && typeof cfg.sources === "object" ? cfg.sources : {};
    const label = (role) => {
      if (sources[role + ".model"] === "cli") return "custom";
      const own = profiles ? profiles[role] : null;
      return (typeof own === "string" && own) || cfg.profile || "—";
    };
    return { dev: label("dev"), review: label("review") };
  }

  // The run's authoritative `effective-config:` snapshot — the startup line every
  // real run logs, embedded per-run in the history payload and treated as
  // authoritative if it differs from the Start-dialog preview. Rendered as a
  // disclosure so a card stays scannable: the role profiles · backend in the
  // summary (collapsed to one label when both roles match, which is every legacy
  // run), then each role's profile + resolved values, config/effective revisions,
  // and provenance when opened. An absent snapshot (a pre-field or unparsed run)
  // renders nothing.
  function effectiveConfigBlock(cfg) {
    if (!cfg || typeof cfg !== "object") return "";
    const profiles = roleProfileLabels(cfg);
    const roleSummary =
      profiles.dev === profiles.review
        ? profiles.dev
        : "dev " + profiles.dev + " · review " + profiles.review;
    const summary = escapeHtml(roleSummary + " · " + (cfg.backend || "?"));
    const rows = [
      ["backend", cfg.backend || "—"],
      ["dev", profiles.dev + " · " + roleValueText(cfg.dev)],
      ["review", profiles.review + " · " + roleValueText(cfg.review)],
      ["config rev", shortRev(cfg.config_revision)],
      ["effective rev", shortRev(cfg.effective_revision)],
    ];
    const kv = rows
      .map(
        ([k, v]) =>
          `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`
      )
      .join("");
    return (
      `<details class="run-config">` +
      `<summary>Launch config: ${summary}</summary>` +
      `<dl class="kv">${kv}</dl>` +
      sourcesDetails(cfg.sources) +
      `</details>`
    );
  }

  function historyRunCard(run) {
    const kv = runMetaKv(run);
    // Each entry embeds its persisted per-session snapshot (sessions[] +
    // context_budget), so the card renders context strips without a per-run
    // fetch; an ended run with no parsed sessions renders none. The run's
    // (repo, task) is carried by the enclosing group header, not repeated here.
    const strips = sessionStrips(run.sessions, run.context_budget);
    // The effective-config snapshot rides the history payload per run; render it
    // OUTSIDE the card-link — interactive content like <details>/<summary> is
    // invalid inside an <a> and its click would fight the card's own link.
    const config = effectiveConfigBlock(run.effective_config);
    // History spans live and ended runs, so the destination splits accordingly:
    // a live run opens its active task detail, an ended one the run-scoped task
    // view (the task may already be archived), and a run with no task id keeps
    // its logs. Logs stay reachable from either task destination.
    const href = runTaskDestination(run) || logsHash(run.run_id);
    return `
      <li class="card run">
        <a class="card-link" href="${href}">
          <div class="card-head">
            <span class="run-id">${escapeHtml(run.run_id)}</span>
            <span class="badges">${runBadge(run.state)}</span>
          </div>
          ${kv ? `<dl class="kv">${kv}</dl>` : ""}
          ${strips}
        </a>
        ${config}
      </li>`;
  }

  // The way into the cross-repo report feed. It lives on Run history because that
  // is already the evaluation subsystem's entry surface (the per-task
  // `Evaluation →` links are on the same page), and because the gate it needs —
  // whether evaluation is enabled at all — rides the history payload the view has
  // just fetched, so a disabled hub hides the entry with no extra request. The
  // per-task links are unchanged: this one goes UP to the published feed, they go
  // DOWN into one task's evaluation.
  function reportsEntryLink(enabled) {
    if (!enabled) return "";
    return (
      `<p class="reports-entry">` +
      `<a class="text-link" href="#/reports">All evaluation reports →</a></p>`
    );
  }

  // Every Run-history page opens the same way, whether or not it has runs to
  // show. One shell rather than three call sites, because the feed entry is a
  // property of the *page*, not of the page having runs: an evaluation needs no
  // related run (an evidence pack accepts an empty related-run list), and a
  // published catalog entry deliberately outlives the artifacts and run records
  // it was built from — so an evaluation-enabled hub can hold a non-empty report
  // feed and zero persisted runs. That hub is precisely the one whose only
  // dashboard path to the feed is this link, so the empty states are where it
  // matters most.
  function historyHead(data) {
    return pageTitle("Run history") + reportsEntryLink(data.evaluation_enabled);
  }

  // The newest evaluation opened by the group's link, expressed as one compact
  // status light: grey for no report, amber for Layer 1, green for both layers.
  // Visible words would crowd and misalign this already-dense summary row, so
  // the exact state rides the link title and aria-label instead.
  // A missing batch entry means its best-effort server read failed; keep the
  // link but claim no state.
  function historyEvalSignal(state) {
    if (!state) return null;
    if (state.level === "l1_l2")
      return { label: "L1 + L2", kind: "l1-l2" };
    if (state.level === "l1") return { label: "L1", kind: "l1" };
    if (state.level === "none")
      return { label: "not evaluated", kind: "none" };
    return null;
  }

  // One task group: a collapse-toggle header (task id + repo + run count), the
  // est-vs-actual rollup, then the group's run cards newest-first via the shared
  // card builder. The header is a real <button> (aria-expanded) so collapse is
  // keyboard-operable and screen-reader-legible; a collapsed group keeps its
  // header + rollup summary and only hides the run-card list — via the
  // `.collapsed` CSS, not by dropping the cards, and with no effect on ordering
  // or the rollup.
  //
  // Collapsed is the DEFAULT: a page of 50 runs is only scannable if each task
  // reads as one header + rollup line until asked to open. So the tracked state
  // is the *expansion the user asked for*, keyed by the stable group key — a
  // group not in the set renders collapsed, which is exactly what a group first
  // seen on this page (or on a page just navigated to) is. The key rides
  // `data-group-key` for the toggle.
  function taskGroupCard(group) {
    const runCount = group.runs.length;
    const key = groupKey(group.runs[0]); // group is non-empty; all runs share it
    const collapsed = !expandedGroups.has(key);
    const cards = group.runs.map(historyRunCard).join("");
    // A per-task Evaluation link (eval-enabled hubs only), handed to taskRollup
    // so it rides the rollup summary row inline and right-aligned instead of a
    // standalone vertical row. It stays OUTSIDE the collapse-toggle <button> —
    // nesting an <a> in a <button> is invalid, and keeping it clear of
    // `.task-group-head` means the delegated toggle (which matches only that
    // header) never fires on an Evaluation click, so activating it navigates
    // without collapsing the group. It stays visible when the group collapses,
    // giving archived/completed tasks a reachable evaluation entry point.
    const evalSignal = historyEvalSignal(historyEvalStates.get(key) || null);
    const evalMarker = evalSignal
      ? `<span class="eval-state-dot ${evalSignal.kind}" aria-hidden="true"></span>`
      : "";
    const evalAction =
      historyEvalEnabled && group.repo_name && group.task_id
        ? `<a class="text-link tg-eval" href="${evalHash(
            group.repo_name,
            group.task_id
          )}"${
            evalSignal
              ? ` title="Evaluation status: ${escapeHtml(
                  evalSignal.label
                )}" aria-label="Evaluation, status: ${escapeHtml(
                  evalSignal.label
                )}"`
              : ""
          }>${evalMarker}Evaluation →</a>`
        : "";
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
      taskRollup(group, evalAction) +
      `<ul class="list">${cards}</ul>` +
      `</section>`
    );
  }

  // ---- run-history navigation: paging + compose-over-page filters + collapse ----
  //
  // Two layers, deliberately different. PAGING is a server window: each page is
  // its own /api/history/runs request, because the whole point is that the
  // response never grows with history. A step is a cursor the server handed
  // back, never a page number this side computes — each cursor is opaque here
  // (the server anchors it on a real run id and a direction), so a run started or
  // pruned mid-traversal cannot shift the window either Previous or Next lands on.
  // FILTERS are client-side over whatever page is loaded — no refetch on change,
  // mirroring the task-list filters — which means they are page-local, and every
  // count/empty state below says so rather than implying it searched the whole
  // history.
  //
  // Filters compose as AND — repo (exact name), run state (exact), and a
  // case-insensitive task-id substring. Filtering the flat, newest-first list
  // *before* grouping keeps ordering intact and lets the per-group rollup and the
  // count line describe exactly the runs shown. Held in the closure, so they
  // persist across view re-entry like the task filters. The position does NOT:
  // route entry always requests the newest page, so no stale cursor can survive
  // into a later visit.
  const runFilters = { repo: "", state: "", task: "" };
  // Groups the user explicitly expanded, keyed by the stable group key; absent =
  // collapsed, which is the default for every newly encountered group. Held in
  // the closure so an expansion survives a filter re-render and a page trip
  // whenever the same group is still on screen; a stale key for a group no
  // longer present is inert.
  const expandedGroups = new Set();
  // Evaluation enablement + one newest-evaluation state per task group arrive
  // in the same /api/history/runs response. Keeping the group-keyed map here
  // lets filter-only re-renders reuse the batch with no network fan-out.
  let historyEvalEnabled = false;
  let historyEvalStates = new Map();

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
    if (collapsed) expandedGroups.delete(key);
    else expandedGroups.add(key);
  }

  // Partial re-render: filter the loaded page's runs, regroup, and repaint only
  // the count line + group list (the filter controls and the pager stay put,
  // keeping focus/caret). Newest-first survives (filter preserves order; grouping
  // keeps first-seen). Empty *history* and an out-of-range page are handled
  // upstream in renderRuns; an empty result here means the filters excluded
  // everything on this page — a distinct no-match state.
  //
  // Both the count and the no-match line name their scope ("on this page"): the
  // filters only ever saw the loaded window, so reporting a bare total would read
  // as a search across all of history.
  function renderRunGroups(allRuns) {
    const groupsEl = document.getElementById("run-groups");
    const countEl = document.getElementById("run-count");
    const filtered = allRuns.filter(matchesRunFilters);
    const groups = groupRunsByTask(filtered);
    if (!groups.length) {
      countEl.textContent = "";
      groupsEl.innerHTML =
        `<p class="center">No runs on this page match these filters.</p>`;
      return;
    }
    const g = groups.length;
    const r = filtered.length;
    const scope =
      r === allRuns.length
        ? `${r} run${r === 1 ? "" : "s"}`
        : `${r} of ${allRuns.length} runs`;
    countEl.textContent = `${g} group${g === 1 ? "" : "s"} · ${scope} on this page`;
    groupsEl.innerHTML = groups.map(taskGroupCard).join("");
  }

  // The Previous/Next pager for run history, rendered from the response's own
  // pagination metadata — never inferred from a short page. Each control is
  // enabled exactly when the server handed back a cursor for that direction, so
  // what the button offers and where it would go are the same fact and cannot
  // drift apart. The status line carries the scope the page-local filters do
  // not: which page this is and how many runs the whole history holds.
  //
  // "Page N of M" is rendered verbatim because both count the windows of *this*
  // traversal: a run persisted mid-walk makes the walk one window longer than the
  // page grid, and the server counts that window rather than handing back a
  // position past its own page count. The exception is a window with no runs —
  // there `page` is deliberately one past everything that exists, so pairing the
  // two would read as a contradiction; that state's own line names its position
  // and the real page count, leaving the pager just the whole-history total.
  //
  // The buttons come from the shared `pagerButton` helper; only the direction
  // sources and this status line are run-history's own.
  function historyPager(data) {
    const page = Number(data.page) || 1;
    const totalPages = Number(data.total_pages) || 0;
    const total = Number(data.total_runs) || 0;
    const position = (data.runs || []).length
      ? `Page ${page} of ${totalPages} · `
      : "";
    return (
      `<nav class="pager" aria-label="Run history pages">` +
      pagerButton("rp-prev", "Previous", "←", !!data.prev_cursor, true) +
      `<span class="pager-status">${position}${total} run${
        total === 1 ? "" : "s"
      } total</span>` +
      pagerButton("rp-next", "Next", "→", !!data.next_cursor, false) +
      `</nav>`
    );
  }

  // Page navigation refetches: a page is a server window, so unlike the filters
  // it cannot be composed from what is already loaded. The repaint destroys the
  // button that was pressed, so it lands like any action-driven full re-render
  // (rerender -> document.title sync + focus at the page start).
  //
  // Each direction navigates to the cursor the response carried for it, so the
  // client never computes a position: no cursor means the boundary, and the
  // handler re-checks that rather than trusting the disabled attribute alone, so
  // a boundary control is inert however it is activated. It also means Previous
  // from a page that no longer exists lands on the last page that does — the
  // server anchored that cursor when it found the window empty.
  function wireHistoryPager(data) {
    const wire = (id, cursor) => {
      const btn = document.getElementById(id);
      if (!btn) return;
      btn.addEventListener("click", () => {
        if (!cursor) return;
        rerender(renderRuns(cursor));
      });
    };
    wire("rp-prev", data.prev_cursor);
    wire("rp-next", data.next_cursor);
  }

  // Run history: one newest-first page of runs from the history endpoint,
  // filtered/grouped client-side into collapsible per-task cards, each run
  // tapping through to its log tail. `cursor` is an opaque value the server handed
  // back for an adjacent window; the router calls this with nothing, so
  // entering the view always lands on the newest page, and only the pager
  // navigates off it. Empty history, a page whose runs are gone, and a no-match
  // filter are three distinct states.
  async function renderRuns(cursor) {
    const superseded = routeGuard();
    view().innerHTML = loadingView("Run history", "Loading runs…");
    const data = await api(
      "/api/history/runs" +
        (cursor ? "?cursor=" + encodeURIComponent(cursor) : "")
    );
    if (superseded()) return; // a newer route owns the view
    historyEvalEnabled = !!data.evaluation_enabled;
    historyEvalStates = new Map(
      (data.evaluation_states || []).map((state) => [groupKey(state), state])
    );
    const runs = data.runs || [];
    const totalRuns = Number(data.total_runs) || 0;
    if (!runs.length && !totalRuns) {
      view().innerHTML =
        historyHead(data) + centerHtml("No runs recorded yet.");
      return;
    }
    if (!runs.length) {
      // Everything at and below this cursor was pruned since the page that
      // offered it (or the cursor was hand-typed). Say so plainly and keep the
      // pager, whose Previous carries the server's anchor for the last page that
      // does exist — never a dead end.
      view().innerHTML =
        historyHead(data) +
        historyPager(data) +
        centerHtml(
          `No runs on page ${Number(data.page) || 1}. History holds ` +
            `${totalRuns} run${totalRuns === 1 ? "" : "s"} on ${
              Number(data.total_pages) || 0
            } page${(Number(data.total_pages) || 0) === 1 ? "" : "s"}.`
        );
      wireHistoryPager(data);
      return;
    }
    const repoNames = historyRepoNames(runs);
    const states = orderedRunStates(runs);
    // Drop a persisted filter this page can no longer satisfy, so a stale value
    // never yields a permanent phantom no-match (mirrors the task filters). The
    // free-text task filter is left as typed. The dropdown domains come from the
    // loaded page, which is the same scope the filters act on.
    if (runFilters.repo && !repoNames.includes(runFilters.repo))
      runFilters.repo = "";
    if (runFilters.state && !states.includes(runFilters.state))
      runFilters.state = "";
    view().innerHTML = `
      ${historyHead(data)}
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
      <p class="filters-scope muted">Filters apply to this page's runs.</p>
      ${historyPager(data)}
      <p id="run-count" class="run-count muted" aria-live="polite"></p>
      <div id="run-groups"></div>`;
    wireHistoryPager(data);
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
    const superseded = routeGuard();
    view().innerHTML = loadingView("Task file", "Loading task…");
    const data = await api("/api/runs/" + encodeURIComponent(runId) + "/task"); // unknown run → 404
    if (superseded()) return; // a newer route owns the view
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

  // ---- task evaluation (deterministic Layer 1 metrics) ----
  //
  // A read-only sidecar over the optional evaluation subsystem. The view resolves
  // a task active-then-archive (so a completed/archived task stays evaluable),
  // shows the run/session, protocol, and score rollups a `static_metrics` report
  // carries, and lets the operator (re)generate a fresh deterministic evaluation.
  // It never runs a semantic model and never touches task/run state — generation
  // writes only hub-owned artifacts. Raw run logs are never rendered inline; the
  // full evidence pack is reachable only as a copy/download JSON artifact.

  const evalListPath = (repo, taskId) =>
    "/api/repos/" +
    encodeURIComponent(repo) +
    "/evaluations/" +
    encodeURIComponent(taskId);
  const evalDetailPath = (repo, taskId, id) =>
    evalListPath(repo, taskId) + "/" + encodeURIComponent(id);
  const evalMetricsPath = (repo, taskId) =>
    evalListPath(repo, taskId) + "/metrics";
  // The async semantic-evaluation job: POST starts (or reuses) it, GET reads its
  // current state. Both return the same {"job": <record>|null} envelope. This
  // replaced the retired synchronous /semantic route, which left the page looking
  // idle for the whole model turn and offered no reload-survivable state.
  const evalSemanticJobPath = (repo, taskId) =>
    evalListPath(repo, taskId) + "/semantic/job";

  // Human labels for the five deterministic score axes (machine keys from the
  // report); an unknown key passes through so a new axis still renders.
  const SCORE_LABELS = {
    protocol: "Protocol",
    reliability: "Reliability",
    handoff: "Handoff",
    estimate: "Estimate",
    knowledge: "Knowledge",
  };
  const scoreLabel = (name) => SCORE_LABELS[name] || name;

  // Human labels for the semantic (Layer 2) score/finding dimensions. The model
  // is offered this vocabulary but may add a relevant one, so an unknown key
  // passes through humanized rather than being dropped.
  const SEMANTIC_DIM_LABELS = {
    knowledge_signal_density: "Knowledge signal density",
    ai_freshness_utility: ".ai/ freshness & utility",
    handoff_completeness: "Handoff completeness",
    decision_trace: "Decision trace",
    acceptance_coverage: "Acceptance coverage",
    scope_slice_fit: "Scope / slice fit",
    rediscovery_risk: "Rediscovery risk",
    protocol_soft_failures: "Protocol soft-failures",
  };
  const semDimLabel = (name) =>
    SEMANTIC_DIM_LABELS[name] || String(name || "").replace(/_/g, " ");

  // Finding severity, low→high; the rank orders findings most-severe first and
  // the kind maps to a status badge colour (high=bad, medium=warn, low/info stay
  // neutral so only the two urgent bands carry an alarm colour).
  const SEMANTIC_SEVERITY_RANK = { info: 0, low: 1, medium: 2, high: 3 };
  function semRank(sev) {
    const r = SEMANTIC_SEVERITY_RANK[sev];
    return r === undefined ? -1 : r;
  }
  function semSeverityKind(sev) {
    if (sev === "high") return "bad";
    if (sev === "medium") return "warn";
    return ""; // low / info: neutral
  }

  // A score is 0..1 (1 = clean) or null when the axis is not assessable from the
  // evidence; the level drives the meter/percentage colour, kept distinct from a
  // real 0.0.
  function scoreLevel(score) {
    if (score === null || score === undefined) return "na";
    if (score >= 0.8) return "good";
    if (score >= 0.5) return "mid";
    return "low";
  }

  // A clamped 0..100% score meter, coloured by band. Empty for a non-assessable
  // (null) score — the row shows "n/a" text instead of a misleading empty bar.
  function scoreMeter(score) {
    if (score === null || score === undefined) return "";
    const pct = Math.max(0, Math.min(100, score * 100));
    return (
      `<span class="score-meter ${scoreLevel(score)}" aria-hidden="true">` +
      `<span class="score-fill" style="width: ${pct.toFixed(0)}%"></span></span>`
    );
  }

  function scoreRow(s) {
    const na = s.score === null || s.score === undefined;
    const scoreTxt = na ? "n/a" : Math.round(s.score * 100) + "%";
    const conf = Math.round((s.confidence || 0) * 100) + "%";
    const warnings =
      s.warnings && s.warnings.length
        ? `<ul class="eval-warnings">${s.warnings
            .map((w) => `<li>${escapeHtml(w)}</li>`)
            .join("")}</ul>`
        : "";
    return (
      `<li class="eval-score">` +
      `<div class="eval-score-head">` +
      `<span class="es-name">${escapeHtml(scoreLabel(s.name))}</span>` +
      `<span class="es-score ${scoreLevel(s.score)}">${scoreTxt}</span>` +
      `<span class="es-conf muted">confidence ${conf}</span>` +
      `</div>` +
      scoreMeter(s.score) +
      warnings +
      `</li>`
    );
  }

  // A small key/value block that drops rows whose value is genuinely absent
  // (null/undefined/"") but keeps a real 0 or "no".
  function evalKv(rows) {
    return rows
      .filter(([, v]) => v !== null && v !== undefined && v !== "")
      .map(
        ([k, v]) =>
          `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`
      )
      .join("");
  }

  // The resolved-target signal: active/archived source badge, or a warning pill
  // when the task file is genuinely absent (still evaluable for run history).
  function evalTargetBadge(target) {
    if (!target) return "";
    if (target.source) return badge(target.source);
    if (target.present === false) return badge("no task file", "warn");
    return ""; // present:null → a target IO error; no misleading badge
  }

  function evalScoresSection(report) {
    const scores = report.scores || [];
    if (!scores.length)
      return `<section class="section"><h2>Scores</h2>
        <p class="muted">No score axes were computed.</p></section>`;
    return `<section class="section eval-scores"><h2>Scores</h2>
      <ul class="score-list">${scores.map(scoreRow).join("")}</ul></section>`;
  }

  // The run/session rollup plus the scope-named facts: task status/archive state,
  // related-run outcome partition, the failure-diagnostics red flag, session and
  // plan-gate counts, review/remediation counts, and the context/session-est
  // rollup. `—` marks a value no run reported (kept distinct from 0).
  function evalRunsSection(report) {
    const r = report.runs || {};
    const p = report.protocol || {};
    const est = p.session_est;
    const sourceTxt =
      report.target_source || (report.target_present ? "present" : "no task file");
    const rows = [
      ["task status", p.frontmatter_status],
      ["archive state", sourceTxt],
      ["related runs", r.run_count],
      ["clean / failed", r.clean_count + " / " + r.failure_count],
      [
        "stopped / other",
        r.stopped_count + " / " + (r.ended_unknown_count + r.broken_count),
      ],
      ["live runs", r.live_count],
      ["failed without diagnostic", r.failed_without_diagnostic_count],
      ["human inputs", r.human_input_count],
      ["dev / review sessions", r.dev_session_count + " / " + r.review_session_count],
      ["plan gates", r.plan_gate_count],
      [
        "dev sessions vs est",
        est
          ? r.dev_session_count + " / " + est.total
          : r.dev_session_count + " / est unknown",
      ],
      ["peak context", fmtTokensK(r.peak_context_tokens) || "—"],
      [
        "total wall-clock",
        r.total_duration_seconds != null
          ? fmtDuration(r.total_duration_seconds)
          : "—",
      ],
      ["total turns", r.total_turns],
    ];
    return `<section class="section eval-runs"><h2>Runs &amp; sessions</h2>
      <dl class="kv">${evalKv(rows)}</dl></section>`;
  }

  // The deterministic taskfile/session-log protocol facts. Absent task file →
  // a note (every downstream fact would just be its empty default). Structural
  // problems (missing sections, unreviewed work, illegal transitions, incomplete
  // entries) are surfaced as a flags list, not buried in the key/value grid.
  function evalProtocolSection(p) {
    if (!p) return "";
    if (!p.task_content_present)
      return `<section class="section eval-protocol"><h2>Protocol</h2>
        <p class="muted">No task file was found, so protocol metrics are not
           applicable.</p></section>`;
    const yn = (b) => (b ? "yes" : "no");
    const rows = [
      ["frontmatter status", p.frontmatter_status],
      ["status legal", yn(p.status_legal)],
      ["id matches", yn(p.id_matches)],
      ["session plan", yn(p.has_session_plan)],
      ["log entries", p.entry_count],
      [
        "work / review / malformed",
        p.work_entry_count + " / " + p.review_entry_count + " / " + p.malformed_entry_count,
      ],
      ["pass / changes-requested", p.pass_verdict_count + " / " + p.changes_requested_count],
      ["remediations", p.remediation_count],
      ["blocked entries", p.blocked_entry_count],
      ["dispute-unresolved", p.dispute_unresolved_count],
      ["transition continuity", yn(p.transition_continuity_ok)],
      ["final status matches", yn(p.final_status_matches)],
      ["completed", yn(p.completed)],
      ["final Open none", yn(p.final_open_none)],
      ["blocker rule ok", yn(p.blocker_rule_ok)],
      ["fix-set open", yn(p.fix_set_present)],
    ];
    const flags = [];
    if (p.missing_sections && p.missing_sections.length)
      flags.push("missing sections: " + p.missing_sections.join(", "));
    if (p.uncovered_work_entries && p.uncovered_work_entries.length)
      flags.push("unreviewed work entries: " + p.uncovered_work_entries.join(", "));
    if (p.illegal_transitions && p.illegal_transitions.length)
      flags.push("illegal transitions: " + p.illegal_transitions.join(", "));
    if (p.incomplete_entries && p.incomplete_entries.length)
      flags.push("incomplete entries: " + p.incomplete_entries.join(", "));
    const flagsHtml = flags.length
      ? `<ul class="eval-flags">${flags
          .map((f) => `<li>${escapeHtml(f)}</li>`)
          .join("")}</ul>`
      : "";
    return `<section class="section eval-protocol"><h2>Protocol</h2>
      <dl class="kv">${evalKv(rows)}</dl>${flagsHtml}</section>`;
  }

  // Pack-level evidence-degradation warnings (missing/corrupt run artifacts),
  // kept separate from the per-score quality warnings shown inline above.
  function evalEvidenceWarnings(warnings) {
    if (!warnings || !warnings.length) return "";
    return `<section class="section eval-evidence-warnings">
      <h2>Evidence warnings</h2>
      <ul class="eval-warnings">${warnings
        .map((w) => `<li>${escapeHtml(w)}</li>`)
        .join("")}</ul></section>`;
  }

  function evalMetaSection(id, generatedTs) {
    const rows = [
      ["evaluation id", id],
      ["generated", generatedTs || "—"],
    ];
    return `<section class="section eval-meta"><h2>Artifact</h2>
      <dl class="kv">${evalKv(rows)}</dl></section>`;
  }

  // One copy/download row for a raw JSON artifact. Download is a declarative
  // data: URI anchor (no runtime Blob/clipboard needed); Copy is wired to the
  // closed-over JSON string in wireEvalControls. This is the only place the raw
  // evidence is offered — it is never rendered inline, so unbounded log tails
  // stay out of the UI.
  function evalArtifactRow(label, artifactKey, json, taskId, fileStem) {
    const filename = fileStem + "-" + taskId + ".json";
    const href =
      "data:application/json;charset=utf-8," + encodeURIComponent(json);
    return (
      `<div class="eval-artifact">` +
      `<span class="ea-label">${escapeHtml(label)}</span>` +
      `<button type="button" class="eval-copy" data-artifact="${escapeHtml(
        artifactKey
      )}">Copy</button>` +
      `<a class="eval-dl text-link" download="${escapeHtml(
        filename
      )}" href="${escapeHtml(href)}">Download</a>` +
      `</div>`
    );
  }

  // The report.md counterpart of evalArtifactRow: a text/markdown data: URI so the
  // human-readable semantic report exports/opens directly, alongside the raw JSON.
  function evalMdArtifactRow(label, artifactKey, text, taskId, fileStem) {
    const filename = fileStem + "-" + taskId + ".md";
    const href =
      "data:text/markdown;charset=utf-8," + encodeURIComponent(text);
    return (
      `<div class="eval-artifact">` +
      `<span class="ea-label">${escapeHtml(label)}</span>` +
      `<button type="button" class="eval-copy" data-artifact="${escapeHtml(
        artifactKey
      )}">Copy</button>` +
      `<a class="eval-dl text-link" download="${escapeHtml(
        filename
      )}" href="${escapeHtml(href)}">Download</a>` +
      `</div>`
    );
  }

  function evalArtifactsSection(taskId, reportJson, evidenceJson, semanticJson, reportMd) {
    const items = [];
    if (reportJson)
      items.push(
        evalArtifactRow("Static metrics", "report", reportJson, taskId, "static_metrics")
      );
    if (semanticJson)
      items.push(
        evalArtifactRow("Semantic report", "semantic", semanticJson, taskId, "semantic_report")
      );
    if (reportMd)
      items.push(
        evalMdArtifactRow("Semantic report (Markdown)", "report_md", reportMd, taskId, "report")
      );
    if (evidenceJson)
      items.push(
        evalArtifactRow("Evidence pack", "evidence", evidenceJson, taskId, "evidence")
      );
    if (!items.length) return "";
    return `<section class="section eval-artifacts"><h2>Local inspection</h2>
      <p class="muted">Copy or download the raw JSON or Markdown for local
         inspection. Raw run logs are not shown in the dashboard.</p>
      ${items.join("")}
      <span class="control-status eval-copy-status" role="status"></span>
    </section>`;
  }

  // ---- semantic (Layer 2 model judgment) rendering ----
  //
  // A semantic report is the model's judgment over the same bounded evidence the
  // static layer scored deterministically. It is rendered as a visually distinct
  // Layer 2 block (a "model judgment" tag, its own accent) so a reader never
  // confuses a model score/finding with a deterministic one. Every axis carries a
  // rationale, every finding cites evidence refs — the prompt/schema require it —
  // so the render surfaces both rather than showing naked numbers.

  // One semantic score axis: dimension label, banded score meter (reusing the
  // Layer 1 meter), confidence, and the required rationale beneath.
  function semanticScoreRow(s) {
    const na = s.score === null || s.score === undefined;
    const scoreTxt = na ? "n/a" : Math.round(s.score * 100) + "%";
    const conf = Math.round((s.confidence || 0) * 100) + "%";
    return (
      `<li class="eval-score sem-score">` +
      `<div class="eval-score-head">` +
      `<span class="es-name">${escapeHtml(semDimLabel(s.dimension))}</span>` +
      `<span class="es-score ${scoreLevel(s.score)}">${scoreTxt}</span>` +
      `<span class="es-conf muted">confidence ${conf}</span>` +
      `</div>` +
      scoreMeter(s.score) +
      (s.rationale
        ? `<p class="sem-rationale">${escapeHtml(s.rationale)}</p>`
        : "") +
      `</li>`
    );
  }

  // One finding: a severity badge, the dimension, its confidence, the one-line
  // summary, optional detail, the cited evidence refs (what keeps it auditable),
  // and an optional concrete suggestion.
  function semanticFindingItem(f) {
    const conf = Math.round((f.confidence || 0) * 100) + "%";
    const refs = (f.evidence_refs || []).filter(Boolean);
    const refsHtml = refs.length
      ? `<p class="sem-refs"><span class="sem-label">Evidence</span> ${escapeHtml(
          refs.join("; ")
        )}</p>`
      : "";
    const detailHtml = f.detail
      ? `<p class="sem-finding-detail">${escapeHtml(f.detail)}</p>`
      : "";
    const suggestionHtml = f.suggestion
      ? `<p class="sem-suggestion"><span class="sem-label">Suggestion</span> ${escapeHtml(
          f.suggestion
        )}</p>`
      : "";
    return (
      `<li class="sem-finding sev-${escapeHtml(f.severity)}">` +
      `<div class="sem-finding-head">` +
      badge(f.severity, semSeverityKind(f.severity)) +
      `<span class="sem-dim">${escapeHtml(semDimLabel(f.dimension))}</span>` +
      `<span class="es-conf muted">confidence ${conf}</span>` +
      `</div>` +
      `<p class="sem-finding-summary">${escapeHtml(f.summary)}</p>` +
      detailHtml +
      refsHtml +
      suggestionHtml +
      `</li>`
    );
  }

  // Findings most-severe first (high→info); a copy is sorted so the source order
  // is untouched. An unknown severity ranks below info rather than throwing.
  function semanticFindingsSorted(findings) {
    return findings.slice().sort((a, b) => semRank(b.severity) - semRank(a.severity));
  }

  // The full Layer 2 report block for the selected evaluation. Distinct from the
  // static sections above it: a provenance line (which model/version judged this),
  // the narrative summary that replaces a naked overall score, per-axis scores
  // with rationales, severity-ordered findings, suggested improvements, and the
  // evidence-degradation notes that explain a lowered confidence.
  function semanticReportSection(report) {
    const ev = report.evaluator || {};
    const conf = Math.round((report.overall_confidence || 0) * 100) + "%";
    const evaluatorTxt =
      (ev.backend || "?") +
      " / " +
      (ev.model || "n/a") +
      " · schema v" +
      (ev.schema_version || "?");
    const overall = `<dl class="kv sem-overall">${evalKv([
      ["overall confidence", conf],
      ["evaluator", evaluatorTxt],
      ["static metrics input", report.input_static_metrics ? "yes" : "no"],
    ])}</dl>`;
    const summary = report.summary
      ? `<p class="sem-summary">${escapeHtml(report.summary)}</p>`
      : `<p class="muted">No narrative summary was provided.</p>`;
    const scores = report.scores || [];
    const scoresHtml = scores.length
      ? `<h3>Score axes</h3><ul class="score-list sem-scores">${scores
          .map(semanticScoreRow)
          .join("")}</ul>`
      : `<h3>Score axes</h3><p class="muted">No score axes were returned.</p>`;
    const findings = report.findings || [];
    const findingsHtml = findings.length
      ? `<h3>Findings</h3><ul class="sem-findings">${semanticFindingsSorted(findings)
          .map(semanticFindingItem)
          .join("")}</ul>`
      : `<h3>Findings</h3><p class="muted">No findings were raised.</p>`;
    const improvements = report.improvements || [];
    const impHtml = improvements.length
      ? `<h3>Suggested improvements</h3><ul class="sem-improvements">${improvements
          .map((i) => `<li>${escapeHtml(i)}</li>`)
          .join("")}</ul>`
      : "";
    const warnings = report.warnings || [];
    const warnHtml = warnings.length
      ? `<h3>Evidence notes</h3><ul class="eval-warnings sem-warnings">${warnings
          .map((w) => `<li>${escapeHtml(w)}</li>`)
          .join("")}</ul>`
      : "";
    return (
      `<section class="section sem-report">` +
      `<h2>Semantic evaluation <span class="sem-layer-tag">Layer 2 · model judgment</span></h2>` +
      overall +
      summary +
      scoresHtml +
      findingsHtml +
      impHtml +
      warnHtml +
      `</section>`
    );
  }

  // Shown for a full Layer 1 evaluation that carries no semantic report yet, so
  // the operator sees that Layer 2 is available but not run for THIS evaluation
  // (the two layers are the same evidence judged two ways).
  function semanticAbsentSection() {
    return `<section class="section sem-report sem-absent">
      <h2>Semantic evaluation <span class="sem-layer-tag">Layer 2 · model judgment</span></h2>
      <p class="muted">This evaluation has deterministic metrics only. Run a semantic
         evaluation above to add the model-judged Layer 2 report.</p></section>`;
  }

  // The metrics body for the selected evaluation. Distinct states, kept
  // deliberately separate: no evaluation yet, an evidence-only evaluation with no
  // computed metrics, a full Layer 1 report without a semantic layer, and a full
  // report carrying a Layer 2 semantic judgment.
  function evalDetailHtml(taskId, detail, reportJson, evidenceJson, semanticJson, reportMd) {
    if (!detail)
      return `<section class="section"><h2>Metrics</h2>
        <p class="muted">No evaluation has been generated for this task yet. Use
           Generate metrics above to compute the first one.</p></section>`;
    const report = detail.static_metrics;
    const semantic = detail.semantic_report;
    const artifacts = evalArtifactsSection(
      taskId,
      reportJson,
      evidenceJson,
      semanticJson,
      reportMd
    );
    if (!report)
      return (
        `<section class="section"><h2>Metrics</h2>
          <p class="muted">This evaluation has an evidence pack but no computed
             Layer 1 metrics. Generate metrics to compute them, or download the
             evidence below.</p></section>` + artifacts
      );
    const semanticHtml = semantic
      ? semanticReportSection(semantic)
      : semanticAbsentSection();
    return (
      evalScoresSection(report) +
      evalRunsSection(report) +
      evalProtocolSection(report.protocol) +
      evalEvidenceWarnings(report.warnings) +
      semanticHtml +
      evalMetaSection(detail.evaluation_id, report.generated_ts) +
      artifacts
    );
  }

  function evalGenerateSection() {
    return `<section class="section eval-generate"><h2>Layer 1 · Deterministic metrics</h2>
      <p class="muted">Recompute deterministic evidence and Layer 1 metrics from
         the task file and its run history. This is free and instant: it writes
         only hub-owned artifacts, never modifies the task or run records, and
         invokes no model.</p>
      <div class="eval-actions">
        <button type="button" class="eval-gen-btn">Generate metrics</button>
        <span class="control-status eval-status" role="status"></span>
      </div></section>`;
  }

  // Elapsed since a running/queued job's clock started (started_ts once running,
  // else created_ts) — a compact "obviously alive, not stuck" signal for the
  // running panel. Mirrors intakeElapsed: degrades to "" on a null/unparseable/
  // future stamp so the panel shows just the verb rather than "NaN".
  function evalSemElapsed(job) {
    const ts = (job && (job.started_ts || job.created_ts)) || null;
    if (!ts) return "";
    const then = new Date(ts);
    if (isNaN(then.getTime())) return "";
    const secs = Math.floor((Date.now() - then.getTime()) / 1000);
    if (secs < 0) return "";
    if (secs < 60) return secs + "s";
    const mins = Math.floor(secs / 60);
    if (mins < 60) return mins + "m " + (secs % 60) + "s";
    const hrs = Math.floor(mins / 60);
    return hrs + "h " + (mins % 60) + "m";
  }
  function evalSemElapsedPhrase(job) {
    const e = evalSemElapsed(job);
    return e ? " — running for " + e : "";
  }
  // Tick the running panel's elapsed span in place each poll, so a still-running
  // job's label advances without a full re-render (mirrors updateIntakeElapsed).
  function updateEvalSemElapsed(job) {
    const el = document.getElementById("eval-sem-elapsed");
    if (el) el.textContent = evalSemElapsedPhrase(job);
  }

  const evalSemModel = (job) => (job && job.model) || "the semantic evaluator";

  // The idle/retry trigger button (and its live-region status span), or — when the
  // doctor reports the backend unconfigured — a disabled trigger with the reason.
  // The doctor gates the trigger so an operator is not left to discover an
  // unconfigured backend as a failed job on click, while the free deterministic
  // Layer 1 metrics stay usable. A null doctor (probe failed / older hub) keeps the
  // default enabled control — the sidecar never blocks on its own readiness check.
  function evalSemTrigger(label, unavailable, doctor) {
    if (unavailable)
      return `<button type="button" class="eval-sem-btn" disabled
           aria-describedby="eval-sem-unavailable">${label}</button>
        <span class="control-status eval-sem-status eval-sem-unavailable"
           id="eval-sem-unavailable" role="status">Layer 2 is unavailable on this hub: ${escapeHtml(
             doctor.detail || "the semantic evaluator backend is not configured"
           )}. The deterministic Layer 1 metrics above still work.</span>`;
    return `<button type="button" class="eval-sem-btn">${label}</button>
        <span class="control-status eval-sem-status" role="status"></span>`;
  }

  // The in-flight (queued/running) panel content: no trigger button at all, so a
  // duplicate model call cannot be started from the page while one is running (the
  // backend dedups regardless). A polite live-region status names the model and
  // ticks the elapsed span; the state is read from the persisted job on every load,
  // so it survives reload, navigation, and another browser session. The
  // `.eval-sem-running` modifier is applied to the single wrapper by
  // evalSemanticSection.
  function evalSemRunning(job) {
    return `<span class="control-status eval-sem-status" role="status" aria-live="polite">
          Running the semantic evaluator with ${escapeHtml(evalSemModel(job))}…
          this uses a model and can take a little while. You can leave this page —
          the job keeps running.<span id="eval-sem-elapsed">${escapeHtml(
            evalSemElapsedPhrase(job)
          )}</span></span>`;
  }

  // The terminal-failed (or corrupt-record `error`) panel content: the bounded
  // backend diagnostic — never full prompt text or unbounded model output, the
  // manager caps it — plus a Retry that re-POSTs the job (a fresh attempt is
  // allowed once the prior one is terminal). Retry respects the doctor: an
  // unconfigured backend disables it with the same explanation as the idle trigger.
  // The `.eval-sem-failed` modifier is applied to the single wrapper by
  // evalSemanticSection.
  function evalSemFailed(job, unavailable, doctor) {
    const detail = (job && job.error) || "The semantic evaluation failed.";
    return `<p class="eval-sem-error error-text" role="status">Semantic evaluation
           failed: ${escapeHtml(detail)}</p>
        ${evalSemTrigger("Retry semantic evaluation…", unavailable, doctor)}`;
  }

  // The Layer 2 section, deliberately separated from the free Layer 1 generate so
  // the cost/read-only boundary is unmissable: a semantic evaluation runs a Claude
  // model (a real cost, and slow) but is a read-only analysis bounded to the
  // persisted evidence — not an orchestrator run, and it mutates nothing. Its
  // action region is state-driven by the current async job: a transient job shows
  // the running panel (survives reload); a failed/error job shows the bounded
  // diagnostic + Retry; no job or a completed one shows the idle trigger. All three
  // share one `.eval-actions` wrapper with a state modifier so the layout is a
  // single (never nested) flex container.
  function evalSemanticSection(doctor, job) {
    const unavailable = !!(doctor && doctor.available === false);
    const status = job ? job.status : null;
    let modifier = "";
    let actions;
    if (status && SEMANTIC_JOB_TRANSIENT.has(status)) {
      // A running job wins over the doctor gate: a turn is genuinely in flight, so
      // show its live state rather than the (now moot) start control.
      modifier = " eval-sem-running";
      actions = evalSemRunning(job);
    } else if (status === "failed" || status === "error") {
      modifier = " eval-sem-failed";
      actions = evalSemFailed(job, unavailable, doctor);
    } else {
      // No job yet, or a completed one (its report shows below): offer the trigger.
      actions = evalSemTrigger("Run semantic evaluation…", unavailable, doctor);
    }
    return `<section class="section eval-generate eval-semantic-generate"><h2>Layer 2 · Semantic evaluation</h2>
      <p class="muted">A model reads the bounded evidence and static metrics and
         judges what a deterministic check cannot — knowledge-signal density,
         handoff and decision-trace completeness, acceptance coverage, and protocol
         soft-failures — returning scored axes and findings with suggested
         improvements.</p>
      <p class="eval-cost"><strong>Cost &amp; scope:</strong> this runs a Claude
         model, so it takes a little while and consumes model usage (a real cost),
         unlike the free Layer 1 metrics. It is a <strong>read-only analysis
         session</strong> bounded to the persisted evidence: it does not read live
         repo files, does not start an orchestrator task run, and never modifies the
         task or run state. Each run creates a fresh evaluation (evidence + metrics +
         semantic report).</p>
      <div class="eval-actions${modifier}">
        ${actions}
      </div></section>`;
  }

  // The evaluation-history picker: a select of past evaluations (newest first).
  // Changing it navigates to the id-scoped route so the full view re-renders
  // under the route guard — no in-place repaint bookkeeping.
  function evalPickerSection(repo, taskId, evals, selectedId) {
    if (!evals.length) return "";
    const opts = evals
      .map((e) => {
        let mark = e.has_semantic_report
          ? "semantic"
          : e.has_static_metrics
          ? "metrics"
          : e.has_evidence
          ? "evidence only"
          : "empty";
        // Surface the evaluator model/version on semantic entries so the picker
        // doubles as a run-comparison surface — an operator can tell which model
        // and schema judged each run without opening every one. The list route
        // reads this best-effort; a null evaluator (older/degraded report) just
        // omits the suffix.
        if (e.has_semantic_report && e.evaluator) {
          const bits = [
            e.evaluator.model,
            e.evaluator.schema_version ? "v" + e.evaluator.schema_version : "",
          ].filter(Boolean);
          if (bits.length) mark += " · " + bits.join(" · ");
        }
        const sel = e.evaluation_id === selectedId ? " selected" : "";
        return `<option value="${escapeHtml(e.evaluation_id)}"${sel}>${escapeHtml(
          e.evaluation_id + " · " + mark
        )}</option>`;
      })
      .join("");
    return `<section class="section eval-history"><h2>Evaluations</h2>
      <label for="eval-pick">Evaluation
        <select id="eval-pick">${opts}</select>
      </label>
      <p class="muted eval-count">${evals.length} evaluation${
      evals.length === 1 ? "" : "s"
    } · newest first</p></section>`;
  }

  // Copy an artifact's JSON to the clipboard, degrading with a status hint when
  // the Clipboard API is unavailable (the Download link still works).
  function evalCopy(text) {
    const statusEl = document.querySelector(".eval-copy-status");
    if (statusEl) statusEl.classList.remove("error-text");
    if (!text) {
      if (statusEl) statusEl.textContent = "Nothing to copy.";
      return;
    }
    const nav = globalThis.navigator;
    if (nav && nav.clipboard && typeof nav.clipboard.writeText === "function") {
      nav.clipboard.writeText(text).then(
        () => {
          if (statusEl) statusEl.textContent = "Copied to clipboard.";
        },
        () => {
          if (statusEl) {
            statusEl.classList.add("error-text");
            statusEl.textContent = "Copy failed — use Download.";
          }
        }
      );
    } else if (statusEl) {
      statusEl.classList.add("error-text");
      statusEl.textContent = "Clipboard unavailable — use Download.";
    }
  }

  // Generate a fresh deterministic evaluation: POST /metrics writes hub-owned
  // artifacts only, then re-render to the new latest. Mirrors the intake control
  // guard — captured before the POST so a navigation during the (slow) generation
  // drops the follow-up instead of painting over the newer view.
  async function onEvalGenerate(repo, taskId, button) {
    const statusEl = document.querySelector(".eval-status");
    const superseded = routeGuard();
    setControlBusy(button, statusEl, true);
    try {
      await api(evalMetricsPath(repo, taskId), { method: "POST" });
      if (superseded()) return;
      rerender(renderTaskEval(repo, taskId, null)); // land on the fresh latest
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      if (superseded()) return; // the button and its status line are gone
      setControlBusy(button, statusEl, false);
      if (statusEl) {
        statusEl.classList.add("error-text");
        statusEl.textContent = controlErrorText(err, "Failed to generate metrics.");
      }
    }
  }

  // Start (or retry) a Layer 2 semantic evaluation: POST the async job route
  // (returns the transient queued job at once; the long model turn runs off-thread
  // on the hub and the record self-updates to completed/failed). This is the same
  // handler for the idle trigger and the failed-state Retry — both re-POST. The
  // button goes busy with a running note synchronously (before the re-render), then
  // a re-render reads the queued/running job and paints the running panel + starts
  // the poll. A navigation during the POST drops the follow-up via the guard
  // captured before it. The POST itself only errors on a hard refusal (disabled →
  // 404, unsafe id → 400); an unavailable backend is NOT a start error — it becomes
  // a persisted failed job the poll then surfaces — so a backend/model failure
  // never returns to the idle view. The duplicate guard is server-side: a POST while
  // a job is transient returns that same job and starts no second model call.
  async function onEvalSemantic(repo, taskId, button) {
    const statusEl = document.querySelector(".eval-sem-status");
    const superseded = routeGuard();
    setControlBusy(button, statusEl, true);
    if (statusEl)
      statusEl.textContent =
        "Running the semantic evaluator… this uses a model and can take a little while.";
    try {
      await api(evalSemanticJobPath(repo, taskId), { method: "POST" });
      if (superseded()) return;
      // Read the now-transient job → the running panel, and start the poll.
      rerender(renderTaskEval(repo, taskId, null));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      if (superseded()) return; // the button and its status line are gone
      setControlBusy(button, statusEl, false);
      if (statusEl) {
        statusEl.classList.add("error-text");
        statusEl.textContent = controlErrorText(
          err,
          "Failed to start the semantic evaluation."
        );
      }
    }
  }

  // Teardown for the semantic-job poll, mirroring stopIntakePoll: one module-level
  // handle, cleared by router() on navigation and at the top of renderTaskEval so a
  // poll that outlived its view can neither read, paint, nor reschedule.
  function stopSemanticJobPoll() {
    if (activeSemanticJobPoll) {
      activeSemanticJobPoll.cancelled = true;
      if (activeSemanticJobPoll.timer) clearTimeout(activeSemanticJobPoll.timer);
      activeSemanticJobPoll = null;
    }
  }

  // Poll one in-flight semantic job (started only for a transient status) until it
  // settles. A chained setTimeout — never setInterval — so a slow response can't
  // overlap the next fetch; while the status stays transient it just ticks the
  // elapsed label in place; on settle it re-renders: a completed job selects its
  // freshly written evaluation (so the report paints), while failed/error/gone
  // lands on the idle+retry view via a plain re-render. A transient read failure
  // is swallowed and retried — the turn is still running on the hub.
  function startSemanticJobPoll(repo, taskId) {
    stopSemanticJobPoll();
    const poll = { cancelled: false, timer: null };
    activeSemanticJobPoll = poll;
    const tick = async () => {
      if (poll.cancelled) return;
      let job = null;
      let read = false;
      try {
        const res = await api(evalSemanticJobPath(repo, taskId));
        job = res ? res.job : null;
        read = true;
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return; // login shown
        // Transient read failure — the turn is still running on the hub, so keep
        // polling rather than mistaking the failed read for a settled/gone job.
      }
      if (poll.cancelled || activeSemanticJobPoll !== poll) return; // view left / superseded
      if (!read || (job && SEMANTIC_JOB_TRANSIENT.has(job.status))) {
        // Read failed (retry), or still queued/running (advance the elapsed label).
        if (read) updateEvalSemElapsed(job);
        poll.timer = setTimeout(tick, SEMANTIC_JOB_POLL_MS);
        return;
      }
      // Read succeeded and the job is terminal or gone → repaint the whole view.
      if (job && job.status === "completed" && job.evaluation_id) {
        rerender(renderTaskEval(repo, taskId, job.evaluation_id)); // select the new eval
      } else {
        rerender(renderTaskEval(repo, taskId, null)); // failed/error/gone → idle + retry
      }
    };
    poll.timer = setTimeout(tick, SEMANTIC_JOB_POLL_MS);
  }

  function wireEvalControls(repo, taskId, strings) {
    const genBtn = document.querySelector(".eval-gen-btn");
    if (genBtn)
      genBtn.addEventListener("click", () => onEvalGenerate(repo, taskId, genBtn));
    const semBtn = document.querySelector(".eval-sem-btn");
    if (semBtn)
      semBtn.addEventListener("click", () => onEvalSemantic(repo, taskId, semBtn));
    const pick = document.getElementById("eval-pick");
    if (pick)
      pick.addEventListener("change", (ev) => {
        location.hash =
          evalHash(repo, taskId) + "/" + encodeURIComponent(ev.target.value);
      });
    document.querySelectorAll(".eval-copy").forEach((btn) => {
      btn.addEventListener("click", () => evalCopy(strings[btn.dataset.artifact]));
    });
  }

  async function renderTaskEval(repoName, taskId, evaluationId) {
    const superseded = routeGuard();
    // A fresh render supersedes any in-flight semantic-job poll (this may BE the
    // poll's own settle re-render); it restarts below only if the current job is
    // still transient. router() also stops it on navigation.
    stopSemanticJobPoll();
    view().innerHTML = loadingView(taskId, "Loading evaluation…");

    // Availability first: it is the one evaluation route that answers while the
    // subsystem is disabled (200 {enabled:false}), so the view renders a concise
    // disabled explanation instead of probing an action route that would 404.
    const avail = await api("/api/evaluation"); // 401 re-gates; 5xx → error view
    if (superseded()) return;
    if (!avail || !avail.enabled) {
      view().innerHTML =
        `<a class="back-link" href="${repoHash(repoName)}">← ${escapeHtml(
          repoName
        )}</a>` +
        `<div class="detail-head">${pageTitle(taskId, "task-id big")}</div>` +
        `<section class="section"><h2>Task evaluation</h2>` +
        `<p class="muted">Task evaluation is disabled on this hub ` +
        `(ORCH_HUB_EVAL_ENABLED is set to a disabled value). No evidence or metrics can be ` +
        `generated or viewed here.</p></section>`;
      return;
    }

    // Layer 2 backend readiness — a cheap config/availability probe (no model
    // call, no cost): it gates the semantic control so an enabled-but-unconfigured
    // backend (no `claude` on PATH) disables Layer 2 with an explanation instead of
    // 502-ing on click, while Layer 1 stays usable. Best-effort — a probe failure
    // (or an older hub with no doctor route) leaves the control in its default
    // enabled state rather than blanking the view, since the sidecar is never
    // load-bearing for run control.
    let semanticDoctor = null;
    try {
      const health = await api("/api/evaluation/doctor");
      if (superseded()) return;
      semanticDoctor = health && health.semantic ? health.semantic : null;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err; // re-gate
      if (superseded()) return;
      semanticDoctor = null; // probe failed → keep Layer 2 available (session-1)
    }

    // The current async semantic-evaluation job (queued/running/completed/failed/
    // error), or null when the task never had one. Read on every load so a running
    // or recently-failed job renders its state across reload/navigation/another
    // session. Best-effort like the doctor probe — a job-read failure degrades to
    // no job (the idle trigger) rather than blanking the whole sidecar; 401 re-gates.
    let semanticJob = null;
    try {
      const jobRes = await api(evalSemanticJobPath(repoName, taskId));
      if (superseded()) return;
      semanticJob = jobRes ? jobRes.job : null;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err; // re-gate
      if (superseded()) return;
      semanticJob = null;
    }

    // The task's evaluation history + resolved target (active/archived/absent).
    const list = await api(evalListPath(repoName, taskId));
    if (superseded()) return;
    const evals = list.evaluations || [];
    const selectedId = evaluationId || list.latest_evaluation_id || null;

    // The selected (or latest) evaluation's artifacts, when one exists.
    let detail = null;
    if (selectedId) {
      detail = await api(evalDetailPath(repoName, taskId, selectedId));
      if (superseded()) return;
    }

    // Pretty JSON for the copy/download affordances, computed once and closed
    // over by the copy handlers. report.md is passed through as raw text. No raw
    // evidence is rendered inline.
    const report = detail ? detail.static_metrics : null;
    const evidence = detail ? detail.evidence : null;
    const semantic = detail ? detail.semantic_report : null;
    const reportMd = detail ? detail.report_md : null;
    const reportJson = report ? JSON.stringify(report, null, 2) : "";
    const evidenceJson = evidence ? JSON.stringify(evidence, null, 2) : "";
    const semanticJson = semantic ? JSON.stringify(semantic, null, 2) : "";
    const strings = {
      report: reportJson,
      evidence: evidenceJson,
      semantic: semanticJson,
      report_md: reportMd || "",
    };

    view().innerHTML = `
      <a class="back-link" href="${repoHash(repoName)}">← ${escapeHtml(
      repoName
    )}</a>
      <div class="detail-head">
        ${pageTitle(taskId, "task-id big")}
        ${evalTargetBadge(list.target)}
      </div>
      <p class="eval-kind muted">Two layers over the same evidence: deterministic
         static metrics (Layer 1), computed from the task file and run history, and a
         model-judged semantic evaluation (Layer 2). Layer 2 scores and findings are a
         model's judgment, kept visually distinct from the deterministic ones.</p>
      ${evalGenerateSection()}
      ${evalSemanticSection(semanticDoctor, semanticJob)}
      ${evalPickerSection(repoName, taskId, evals, selectedId)}
      ${evalDetailHtml(taskId, detail, reportJson, evidenceJson, semanticJson, reportMd)}`;

    wireEvalControls(repoName, taskId, strings);

    // A transient job means a model turn is in flight (started here or from another
    // session): poll it to its terminal state. On settle the poll re-renders — a
    // completed job selects its fresh evaluation, a failed one lands on retry.
    if (semanticJob && SEMANTIC_JOB_TRANSIENT.has(semanticJob.status))
      startSemanticJobPoll(repoName, taskId);
  }

  // ---- central evaluation-report feed (cross-repo, global order) ------------
  //
  // The sidecar above answers "how did THIS task go?"; this view answers "what has
  // been published?" across every managed repo, reading the hub's append-only
  // report catalog through GET /api/evaluation/reports. It is the operator's face
  // of the very feed an external protocol-evolution consumer polls — same global
  // order, same identities — so an operator can see exactly what a consumer would
  // import. The hub publishes reports; deciding what to change in the protocol is
  // the consumer's job, so this view shows and links and adds no verdict of its
  // own.
  //
  // It does not replace per-task evaluation history: every row links INTO the
  // existing #/evaluations/{repo}/{task}/{id} view, which keeps the history
  // picker, the artifacts, and the Layer 1 / Layer 2 rendering. Nothing here
  // publishes, generates, or can cause a task to be evaluated — the feed and the
  // bundle route are reads.

  const REPORTS_PATH = "/api/evaluation/reports";

  // The cursors of the pages walked to reach the current one, so Previous can go
  // back. The feed is forward-only by design (`after=<seq>` names a suffix), and
  // published entries are immutable and never renumbered — so a cursor visited
  // earlier names the same page forever and a remembered trail is exact.
  // Arithmetic on `seq` would not be: a torn append leaves a hole in the
  // numbering, so "first_seq - limit" can name a different window than the one
  // that was shown. Reset on every entry to the route (below), like any pager
  // that starts at the beginning.
  let reportTrail = [];

  const reportDetailHash = (entry) =>
    evalHash(entry.repo_name, entry.task_id) +
    "/" +
    encodeURIComponent(entry.evaluation_id);

  // One published report. Everything except `seq`/`report_key` can be absent in a
  // catalog line an older/newer hub wrote (the reader degrades rather than
  // dropping the entry), so a row without a full source identity still renders —
  // the entry is the auditable record — but carries no link, because there is no
  // per-task view to send it to.
  function reportCard(entry) {
    const evaluator = entry.evaluator || {};
    const prov = entry.provenance || {};
    const runs = prov.runs || {};
    const git = prov.git || {};
    const linkable = !!(entry.repo_name && entry.task_id && entry.evaluation_id);
    const kv = evalKv([
      ["repo", entry.repo_name || "(unknown repo)"],
      ["published", formatCommitTime(entry.cataloged_ts)],
      ["generated", formatCommitTime(entry.generated_ts)],
      [
        "evaluator",
        evaluator.model
          ? evaluator.model +
            (evaluator.schema_version ? " · schema " + evaluator.schema_version : "")
          : "unknown",
      ],
      // The evaluation-time target state, kept beside the publication-time fact
      // that the task is archived: a report of live work is evaluated `active`
      // and published later, once close-out archives its task.
      ["evaluated while", entry.target_source || "unknown"],
      // Provenance the consumer cohorts by. Absent provenance is said, not
      // omitted — a blank row would read as "no runs" / "no commits".
      ["runs", runs.available ? String(runs.count || 0) : "unavailable"],
      ["commits", git.available ? String(git.commit_count || 0) : "unavailable"],
    ]);
    // Publication requires the whole L1 + L2 artifact set to be durable, so every
    // feed item is complete by construction. Say it once per row in the same
    // vocabulary Run history's state light uses (green = both layers) rather than
    // leaving the reader to ask.
    const head =
      `<div class="card-head">` +
      `<span class="report-seq">#${escapeHtml(entry.seq)}</span>` +
      `<span class="task-id">${escapeHtml(
        entry.task_id || "(unknown task)"
      )}</span>` +
      `<span class="badges">${badge("L1 + L2", "ok")}</span>` +
      `</div>` +
      (kv ? `<dl class="kv">${kv}</dl>` : "");
    const body = linkable
      ? `<a class="card-link" href="${reportDetailHash(entry)}">${head}</a>`
      : head;
    // The global key is what a consumer stores and re-fetches by, so it is shown
    // rather than hidden — truncated to stay scannable on a phone, with the whole
    // value on the title and selectable in full from the element text.
    const key = entry.report_key || "";
    return (
      `<li class="card report">` +
      body +
      `<p class="report-key muted" title="${escapeHtml(key)}">` +
      `key <span class="rk">${escapeHtml(key.slice(0, 16))}${
        key.length > 16 ? "…" : ""
      }</span></p>` +
      `</li>`
    );
  }

  // Forward-only pager over the feed. Unlike Run history's server-anchored
  // bidirectional cursors, the server offers only "after N": `has_more` says
  // whether more is already available (never inferred from a short page), and
  // Previous is served by the client-side trail above.
  function reportPager(data, entries) {
    const first = entries.length ? entries[0].seq : null;
    const last = entries.length ? entries[entries.length - 1].seq : null;
    const span =
      first === null
        ? ""
        : first === last
        ? `report #${escapeHtml(first)} · `
        : `reports #${escapeHtml(first)}–#${escapeHtml(last)} · `;
    return (
      `<nav class="pager" aria-label="Report feed pages">` +
      pagerButton("fp-prev", "Previous", "←", reportTrail.length > 0, true) +
      `<span class="pager-status">${span}${
        data.has_more ? "more available" : "end of feed"
      }</span>` +
      pagerButton("fp-next", "Next", "→", !!data.has_more, false) +
      `</nav>`
    );
  }

  // Page navigation refetches (a page is a server window) and repaints the whole
  // view, so it lands like any action-driven re-render. Next remembers the cursor
  // it came from; Previous consumes it. Each handler re-checks its own boundary
  // rather than trusting the disabled attribute, so a boundary control is inert
  // however it is activated.
  function wireReportPager(data) {
    const next = document.getElementById("fp-next");
    if (next)
      next.addEventListener("click", () => {
        if (!data.has_more) return;
        reportTrail.push(data.cursor);
        rerender(renderReports(data.next_cursor));
      });
    const prev = document.getElementById("fp-prev");
    if (prev)
      prev.addEventListener("click", () => {
        if (!reportTrail.length) return;
        rerender(renderReports(reportTrail.pop()));
      });
  }

  async function renderReports(after) {
    const superseded = routeGuard();
    view().innerHTML = loadingView("Evaluation reports", "Loading reports…");

    // Availability first, for the same reason the per-task view probes it: this
    // is the one evaluation route that answers while the subsystem is disabled,
    // so a disabled hub gets a concise explanation instead of a 404 error view.
    const avail = await api("/api/evaluation");
    if (superseded()) return;
    if (!avail || !avail.enabled) {
      view().innerHTML =
        pageTitle("Evaluation reports") +
        `<section class="section"><h2>Report feed</h2>` +
        `<p class="muted">Task evaluation is disabled on this hub ` +
        `(ORCH_HUB_EVAL_ENABLED is set to a disabled value), so no reports are ` +
        `published and there is no feed to read.</p></section>`;
      return;
    }

    const data = await api(
      REPORTS_PATH +
        (after === null || after === undefined
          ? ""
          : "?after=" + encodeURIComponent(after))
    );
    if (superseded()) return;
    const entries = data.reports || [];

    // Two different emptinesses. Nothing has ever been published is its own
    // state — no pager, just the note, and it explains what publishes a report
    // because that rule is not guessable (matching how empty run history reads).
    // A page walked past the end of a feed that does hold reports keeps its
    // pager, so Previous is never a dead end.
    const empty = !entries.length && !reportTrail.length;
    const body = entries.length
      ? `<ul class="list">${entries.map(reportCard).join("")}</ul>`
      : reportTrail.length
      ? centerHtml("No further reports after this point in the feed.")
      : centerHtml(
          "No evaluation reports have been published yet. A report is " +
            "published once a task is archived and its complete Layer 1 + " +
            "Layer 2 evaluation is durable."
        );
    view().innerHTML =
      pageTitle("Evaluation reports") +
      `<p class="reports-intro muted">Completed Layer 1 + Layer 2 reports from
         every managed repo, in the order the hub published them. This is the same
         feed an external consumer imports from; orch-hub publishes the artifacts
         and does not decide what to change in the protocol. Each report opens its
         existing per-task evaluation view.</p>` +
      (empty ? "" : reportPager(data, entries)) +
      body;
    if (!empty) wireReportPager(data);
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
  // instead of offering a second. A live run claims the repo for the run
  // subsystem (repo admission), and a known-not-ready repo pre-explains the
  // readiness block — in that order, because a running repo is claimed, not
  // unready, and its dirty tree must not be reported as the reason. The submit
  // handler still handles a raced refusal of either kind.
  function intakeSection(repoName, inflight, readiness, liveRun) {
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
        // A live run alongside an in-flight intake is a recovered overlap
        // (legacy state, or a run reattached after a restart): admission stops
        // one from forming but cannot undo one. Say so here, because the review
        // view's write controls stay refused until the run ends.
        (liveRun
          ? runHoldsRepoNote(
              repoName,
              liveRun,
              "the draft can't be revised or confirmed"
            )
          : "") +
        foot
      );
    }
    if (liveRun) {
      return (
        head +
        runHoldsRepoNote(repoName, liveRun, "a task intake can't start") +
        `<p class="muted">Task intake becomes available once the run ends.</p>` +
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
    const superseded = routeGuard(); // captured before the POST, not after it
    try {
      const rec = await api("/api/intakes", {
        method: "POST",
        body: { repo_id: form.dataset.repo, request },
      });
      // Readiness checks shell out to git/aii-2, so this POST can outlive the
      // view. Routing after the user moved on would drag them off the page they
      // chose; the intake is not lost — repo detail surfaces the repo's
      // in-flight intake, and the draft still pushes when it is ready.
      if (superseded()) return;
      location.hash = intakeHash(rec.intake_id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      if (superseded()) return; // the form and its status line are gone
      if (button) button.disabled = false;
      const existing = err instanceof ApiError && err.body && err.body.intake_id;
      if (existing) {
        location.hash = intakeHash(existing);
        return;
      }
      // A run claimed the repo while this form was open (409 `run_live`).
      // Repaint: the section then explains the run and links to it, instead of
      // a status line that names a run id the operator can't click.
      if (isAdmissionConflict(err)) {
        router();
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
  //
  // A recovered overlap (`liveRun`) removes both agent turns: revising re-reads
  // a tree the run is rewriting and confirming writes `.ai-tasks/` behind it, so
  // the backend fails both closed with 409 `run_live`. Cancel survives because it
  // writes nothing — and it is the only way to clear the overlap from this side,
  // which otherwise keeps blocking the repo's next run start.
  function intakeReviewControls(repoName, liveRun) {
    if (liveRun) {
      return `
      <section class="section intake">
        <h2>Review</h2>
        ${runHoldsRepoNote(
          repoName,
          liveRun,
          "this draft can't be revised or confirmed"
        )}
        <p class="muted">Stop the run to resume the review, or cancel the
          intake — cancelling writes nothing to the repo.</p>
        <div class="intake-actions">
          <button type="button" class="btn-danger intake-cancel">Cancel</button>
          <span class="control-status intake-status" role="status"></span>
        </div>
      </section>`;
    }
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
    const superseded = routeGuard();
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
    // Best-effort repo-admission probe, only for the one state that offers the
    // write controls: a live run on this repo means revise/confirm are refused
    // (409 `run_live`) until it ends. A fetch failure degrades to offering them
    // — the backend still fails them closed.
    let liveRun = null;
    if (data.status === "awaiting_review" && repoName) {
      try {
        const runsData = await api("/api/runs");
        liveRun = repoLiveRun(runsData.runs, repoName);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) throw err;
      }
    }
    // A newer route owns the view: drop the paint, and with it the poll this
    // render would otherwise restart for a view that is gone.
    if (superseded()) return;
    const back = repoName
      ? `<a class="back-link" href="${repoHash(repoName)}">← ${escapeHtml(
          repoName
        )}</a>`
      : `<a class="back-link" href="#/repos">← Repos</a>`;

    let body;
    if (data.status === "awaiting_review") {
      body =
        intakeDraftView(data.draft) + intakeReviewControls(repoName, liveRun);
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
    const superseded = routeGuard(); // captured before the POST, not after it
    setControlBusy(button, statusEl, true);
    try {
      const rec = await api(
        "/api/intakes/" + encodeURIComponent(intakeId) + "/confirm",
        { method: "POST" }
      );
      // The user navigated away while the write was in flight. The turn runs on
      // the hub either way and its outcome lands in the intake record, so
      // dropping the follow-up costs nothing — whereas handing off would yank
      // them off the page they chose, and re-rendering would paint the intake
      // over it.
      if (superseded()) return;
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
      if (superseded()) return; // the button and its status line are gone
      // A run claimed the repo while this draft sat in review (409 `run_live`).
      // A status line would leave the refused write controls on screen, still
      // offering an action that now always fails; repaint into the overlap view,
      // which withdraws them and keeps the Cancel that still works.
      if (isAdmissionConflict(err)) {
        rerender(renderIntakeReview(intakeId));
        return;
      }
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
    const superseded = routeGuard(); // captured before the POST, not after it
    setControlBusy(button, statusEl, true);
    try {
      await api("/api/intakes/" + encodeURIComponent(intakeId) + "/cancel", {
        method: "POST",
      });
      if (superseded()) return; // a newer route owns the view
      rerender(renderIntakeReview(intakeId));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      if (superseded()) return; // the button and its status line are gone
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
    const superseded = routeGuard(); // captured before the POST, not after it
    try {
      await api("/api/intakes/" + encodeURIComponent(intakeId) + "/revise", {
        method: "POST",
        body: { feedback },
      });
      if (superseded()) return; // a newer route owns the view
      rerender(renderIntakeReview(intakeId));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // login shown
      if (superseded()) return; // the form and its status line are gone
      // Same raced admission refusal as confirm: repaint rather than leave a
      // revision form the backend will refuse until the run ends.
      if (isAdmissionConflict(err)) {
        rerender(renderIntakeReview(intakeId));
        return;
      }
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
    const superseded = routeGuard();
    // Leaving or reloading this view aborts any prior in-flight stream first.
    if (activeStream) {
      activeStream.abort();
      activeStream = null;
    }
    view().innerHTML = loadingView("Run log", "Loading run…");
    const run = await api("/api/runs/" + encodeURIComponent(runId)); // unknown → 404
    // A newer route owns the view. Returning here also matters beyond the paint:
    // router() already aborted the stream on its way out, so continuing would
    // open a fresh SSE stream nothing tears down, feeding a detached <pre>.
    if (superseded()) return;
    const live = LIVE_STATES.has(run.state);
    // The task back-link resolves through the shared run destination rule (live
    // run → full task detail, otherwise the run-scoped active-then-archive
    // view). Only the no-task-id fallback differs from a run card's: the logs
    // are already on screen, so there is nothing to fall back to but the Runs
    // list.
    const taskDest = runTaskDestination(run);
    const back = taskDest
      ? `<a class="back-link" href="${taskDest}">← ${escapeHtml(
          run.task_id
        )}</a>`
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
      // Captured per press, not per render: a reload that fails after the user
      // navigated away must not paint its error over the newer route either.
      const reloadSuperseded = routeGuard();
      renderRunLogs(runId).catch((err) => {
        if (err instanceof ApiError && err.status === 401) return; // login shown
        if (reloadSuperseded()) return;
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

  // Repos display-ready vs total, using the shared presentation rule so the
  // count agrees with the repo cards: a live run whose sole raw failure is a
  // dirty tree counts as ready. `runs` may be null (its source failed), in which
  // case repoHasLiveRun is false and every repo falls back to raw readiness.
  function readinessSummary(repos, runs) {
    let ready = 0;
    for (const r of repos || [])
      if (displayReady(r.readiness, repoHasLiveRun(runs, r.name))) ready++;
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
      const r = readinessSummary(repos, runs);
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
    // Readiness failures, minus any repo the shared display rule now reads as
    // ready (live run + dirty tree only). A waiting run still surfaces above for
    // its pending answer; this only drops the redundant NOT READY row.
    for (const r of repos || []) {
      if (r.readiness && !displayReady(r.readiness, repoHasLiveRun(runs, r.name)))
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

  // One live run in the current-activity panel: a run-accented card opening the
  // run's task (runTaskDestination) — where its controls, pending question and
  // session strips are, with the log tail one "View logs" link further — and
  // falling back to the logs directly when the run carries no task attribution.
  // The waiting ones also appear in the attention panel, where the same task
  // link answers the question.
  function activityCard(run) {
    const accent = run.state === "waiting" ? " run-waiting" : " run-active";
    const meta = [run.repo_name, run.task_id].filter(Boolean).join(" · ");
    const href = runTaskDestination(run) || logsHash(run.run_id);
    return (
      `<li class="card run${accent}">` +
      `<a class="card-link" href="${href}">` +
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

  // ---- subscription usage (agent capacity) ----
  //
  // A display-only, best-effort view of Claude and Codex subscription rate-limit
  // usage from GET /api/usage. It is deliberately its OWN section, kept out of the
  // attention/activity/outcomes grid: capacity/quota is ambient information and
  // must never read as a health signal — it feeds no readiness, attention, or
  // run/intake decision (the API is display-only and this panel adds no attention
  // item). Each provider is independent; one unavailable never suppresses the
  // other. Percent-first throughout: a missing figure reads as unknown/unavailable,
  // never a misleading 0% (the scope's unavailable-not-zero rule).

  // Human provider name from the stable provider key; falls back to a capitalized
  // raw key so an unknown future provider still renders sanely.
  const USAGE_PROVIDER_NAMES = { claude: "Claude", codex: "Codex" };
  function usageProviderName(provider) {
    if (USAGE_PROVIDER_NAMES[provider]) return USAGE_PROVIDER_NAMES[provider];
    if (!provider) return "Provider";
    return provider.charAt(0).toUpperCase() + provider.slice(1);
  }

  // A window's short title: the provider's own duration label ("5h"/"7d") when it
  // supplied one, else a humanized machine key. Never invented from nothing.
  const USAGE_WINDOW_KEYS = {
    five_hour: "5h",
    seven_day: "7d",
    primary: "primary",
    secondary: "secondary",
  };
  function usageWindowTitle(w) {
    if (w.label) return w.label;
    return USAGE_WINDOW_KEYS[w.key] || w.key || "window";
  }

  // Absolute reset time in the viewer's local zone: "resets 09:06 on 29 Jul".
  // A relative "resets in 4d" floors away up to a day of lead time — a reset
  // nearly five days out reads as merely four — so show the exact local
  // wall-clock instant instead. An unparseable value falls back to the raw
  // string (never "NaN"), mirroring formatCommitTime's trust-the-hub-value
  // stance. A boundary that has already PASSED never reaches here:
  // usageWindowPhase is the single place that compares a boundary to the clock,
  // and it routes that case to the rolled-over state below — where a "resets"
  // phrase, and the reset-is-due phrasing this used to fall back to, are both
  // wrong, because no window is open at all.
  const RESET_MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];
  function formatResetTime(iso) {
    if (!iso) return "";
    const then = new Date(iso);
    if (isNaN(then.getTime())) return "resets " + iso;
    const hh = String(then.getHours()).padStart(2, "0");
    const mm = String(then.getMinutes()).padStart(2, "0");
    return (
      "resets " + hh + ":" + mm + " on " +
      then.getDate() + " " + RESET_MONTHS[then.getMonth()]
    );
  }

  // Is a window's stored percentage still the CURRENT window's figure?
  //
  // `resets_at` closes the window that is open now and `window_minutes` gives its
  // length, so `resets_at - window_minutes` is when that window opened. A stored
  // percentage is only as new as the last carrier reading (the CLI's own cached
  // utilization fetch, refreshed on its schedule and by the hub's poller; an
  // operator statusLine render if one is installed), so a figure routinely
  // outlives the window it was taken in — and the five-hour window is anchored to
  // the first call OF THAT WINDOW, not to a clock grid, so a boundary that has
  // passed does NOT mean "a fresh window carrying the same figures":
  //
  //   * boundary passed → no window is open; the next call opens one.
  //   * boundary still ahead but the observation predates the window's start → a
  //     window IS open (a hub-driven turn refreshed its boundary) and nothing has
  //     observed its usage yet.
  //
  // In both, the stored figure is not this window's: the meter must not fill to
  // it, and "N% left" must not be derived from it — an inferred "96% left" reads
  // as free capacity when the truth is unknown. The figure is still shown,
  // explicitly labelled last-observed, because a real figure from a closed window
  // beats no figure at all. Percent-only there even for a count-reporting
  // provider, so one phrase covers both shapes.
  //
  // Unknowable inputs stay CURRENT rather than degrading: no boundary, no window
  // length, or an unparseable/absent observation time cannot prove a figure is out
  // of window, and claiming "unknown" without evidence is its own kind of wrong.
  const UW_CURRENT = "current";
  const UW_ROLLED = "rolled"; // boundary passed: no window is open
  const UW_UNOBSERVED = "unobserved"; // window open, figure predates its start
  function usageWindowPhase(w, observedTs) {
    const reset = w.resets_at ? new Date(w.resets_at) : null;
    if (!reset || isNaN(reset.getTime())) return UW_CURRENT;
    if (reset.getTime() <= Date.now()) return UW_ROLLED;
    if (!w.window_minutes || !observedTs) return UW_CURRENT;
    const observed = new Date(observedTs);
    if (isNaN(observed.getTime())) return UW_CURRENT;
    return observed.getTime() < reset.getTime() - w.window_minutes * 60000
      ? UW_UNOBSERVED
      : UW_CURRENT;
  }

  // The primary readout for a window. Percent-first: an exact count is shown ONLY
  // when the provider gives a real numeric limit (neither Claude nor Codex does
  // today) — the UI never invents a fixed message total. With no percentage
  // either, it reads "usage unknown", never a misleading 0%. A window whose figure
  // is not current reads as its state instead of as a figure.
  function usageWindowReadout(w, phase) {
    if (phase === UW_ROLLED) return "no active window";
    if (phase === UW_UNOBSERVED) return "usage unknown";
    if (w.limit != null && w.used != null)
      return escapeHtml(w.used + " / " + w.limit);
    if (w.used_percent != null) return escapeHtml(w.used_percent + "% used");
    return "usage unknown";
  }

  // A used/remaining meter for one window. Fill width is the used percentage
  // (derived from an exact count only when the provider gave a real limit); with
  // no current percentage the track carries an explicit "unknown" state instead of
  // rendering as 0% used — that covers a provider that reported no figure and a
  // figure that no longer belongs to the open window alike. The bar is aria-hidden
  // — the readout + meta text already carry the figures for a screen reader
  // (mirrors the .ctx-meter idiom).
  function usageWindow(w, observedTs) {
    const phase = usageWindowPhase(w, observedTs);
    const current = phase === UW_CURRENT;
    let pct = w.used_percent;
    if (pct == null && w.limit != null && w.used != null && w.limit > 0)
      pct = Math.round((w.used / w.limit) * 1000) / 10;
    const known = current && pct != null;
    const clamped = known ? Math.max(0, Math.min(100, pct)) : 0;
    const high = known && clamped >= 90 ? " high" : "";
    const meta = (current
      ? [
          w.remaining_percent != null ? w.remaining_percent + "% left" : "",
          formatResetTime(w.resets_at),
        ]
      : [
          phase === UW_ROLLED
            ? "starts at next call"
            : formatResetTime(w.resets_at),
          pct != null ? "last observed " + pct + "% used" : "",
        ]
    )
      .filter(Boolean)
      .join(" · ");
    return (
      `<div class="usage-window${current ? "" : " uw-" + phase}">` +
      `<div class="uw-head">` +
      `<span class="uw-label">${escapeHtml(usageWindowTitle(w))}</span>` +
      `<span class="uw-readout">${usageWindowReadout(w, phase)}</span>` +
      `</div>` +
      `<span class="usage-bar${known ? "" : " unknown"}${high}" aria-hidden="true">` +
      (known ? `<span class="usage-bar-fill" style="width: ${clamped}%"></span>` : "") +
      `</span>` +
      (meta ? `<div class="uw-meta muted">${escapeHtml(meta)}</div>` : "") +
      `</div>`
    );
  }

  // Optional Codex reset-credit note: only the aggregate has-credits / unlimited /
  // balance the provider exposes — never a reset-credit identifier (the model
  // drops those by construction). Reading this never spends a credit. Rendered
  // only when there is something positive to say, so it stays out of the way.
  function usageCredits(c) {
    if (!c) return "";
    let text = "";
    if (c.unlimited === true) text = "reset credits: unlimited";
    else if (c.has_credits === true)
      text =
        "reset credits available" +
        (c.balance != null ? " (" + c.balance + ")" : "");
    if (!text) return "";
    return `<div class="usage-credits muted">${escapeHtml(text)}</div>`;
  }

  // The observation-freshness line: when the hub last saw this provider's figures,
  // plus an explicit "last observed" flag when the data is past the staleness
  // threshold (the figures stay shown — flagged, not hidden). Absent when the
  // provider supplied no observation time.
  function usageFreshness(p) {
    if (!p.observed_ts) return "";
    const when = formatCommitTime(p.observed_ts);
    const label = (p.stale ? "last observed " : "observed ") + when;
    const flag = p.stale ? ` <span class="usage-flag">stale</span>` : "";
    return `<div class="usage-fresh muted">${escapeHtml(label)}${flag}</div>`;
  }

  // One provider card. Available → plan, per-window meters, optional credits, and
  // freshness (with a "last observed" flag when stale — still shown). Unavailable
  // → the provider's own reason (e.g. "no Claude usage observed yet"), explicitly,
  // instead of a zero-usage bar. Both are display-only.
  function usageCard(p) {
    const head =
      `<div class="usage-head">` +
      `<span class="usage-provider">${escapeHtml(usageProviderName(p.provider))}</span>` +
      (p.available && p.plan
        ? `<span class="usage-plan">${escapeHtml(p.plan)}</span>`
        : "") +
      `</div>`;
    if (!p.available)
      return (
        `<li class="card usage-card usage-unavailable">` +
        head +
        `<p class="usage-detail muted">${escapeHtml(
          p.detail || "Usage is unavailable."
        )}</p></li>`
      );
    // Each window is rendered against the provider's observation time: whether a
    // stored percentage is still the open window's figure is a per-window question
    // about when it was observed (usageWindowPhase), so the meter cannot be built
    // from the window alone.
    const windows = (p.windows || []).length
      ? (p.windows || []).map((w) => usageWindow(w, p.observed_ts)).join("")
      : `<p class="usage-detail muted">No usage windows reported.</p>`;
    return (
      `<li class="card usage-card${p.stale ? " usage-stale" : ""}">` +
      head +
      `<div class="usage-windows">${windows}</div>` +
      usageCredits(p.credits) +
      usageFreshness(p) +
      `</li>`
    );
  }

  // The Subscription usage section: one card per provider from GET /api/usage. A
  // null source (the fetch failed) degrades ONLY this section to unavailable — it
  // never blocks the other panels and never adds a Needs attention item (capacity
  // is not an operational problem). Its own section, kept out of the
  // attention/activity/outcomes grid, so quota never reads as health.
  function subscriptionUsagePanel(usage) {
    const head = `<section class="section overview-usage"><h2>Subscription usage</h2>`;
    const foot = `</section>`;
    if (usage === null)
      return (
        head + `<p class="muted">Subscription usage is unavailable.</p>` + foot
      );
    if (!usage.length)
      return head + `<p class="muted">No subscription usage reported.</p>` + foot;
    return (
      head +
      `<ul class="list usage-cards">${usage.map(usageCard).join("")}</ul>` +
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
    const [reposData, tasksData, runsData, escData, historyData, usageData] =
      await Promise.all([
        overviewSource("/api/repos"),
        overviewSource("/api/tasks"),
        overviewSource("/api/runs"),
        overviewSource("/api/escalations"),
        overviewSource("/api/history/runs?limit=" + OVERVIEW_HISTORY_LIMIT),
        overviewSource("/api/usage"),
      ]);
    return {
      repos: reposData ? reposData.repos || [] : null,
      taskRepos: tasksData ? tasksData.repos || [] : null,
      runs: runsData ? runsData.runs || [] : null,
      escalations: escData ? escData.escalations || [] : null,
      history: historyData ? historyData.runs || [] : null,
      // The whole /api/usage payload is `{providers:[...]}`; a failed fetch is
      // null (this section only). [] = loaded but no providers reported.
      usage: usageData ? usageData.providers || [] : null,
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
      `</div>` +
      // Its own section AFTER the operational grid: display-only capacity, kept
      // clear of the attention/activity/outcomes flow so quota never reads as
      // health. A failed usage source degrades only this section.
      subscriptionUsagePanel(data.usage);
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
    // /evaluations/{repo}/{task}[/{evaluation_id}] — the task-evaluation view.
    // A 3-segment path shows the latest evaluation; a 4th segment selects a
    // specific past one (the history picker navigates there).
    if (seg[0] === "evaluations" && seg.length >= 3)
      return () => renderTaskEval(seg[1], seg[2], seg[3]);
    // /reports — the cross-repo evaluation-report feed. Paging stays out of the
    // hash (like Run history) and is driven by rerender, so entering the route is
    // always the start of the feed; the trail resets with it.
    if (seg[0] === "reports")
      return () => {
        reportTrail = [];
        return renderReports(null);
      };
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
    // post-await work would then act on a view it no longer owns. Each handler
    // guards its own paint against the same counter through routeGuard(); this
    // one guards the landing/error work the router itself owns.
    const generation = ++routeGeneration;
    // Leaving any view aborts an in-flight log stream and stops the intake poll,
    // the semantic-job poll, and the Overview refresh so none keep reading/painting
    // after navigation.
    if (activeStream) {
      activeStream.abort();
      activeStream = null;
    }
    stopIntakePoll();
    stopSemanticJobPoll();
    stopOverviewRefresh();
    if (!getToken()) {
      renderLogin();
      return;
    }
    showChrome(true);
    const path = currentPath();
    setActiveTab(path);
    setViewKind(path);
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
  // Track the input modality that drives navigation, so focusPageStart shows the
  // routed-heading ring for a keyboard user but not after a touch/mouse tap (see
  // focusPageStart and the .page-title focus rules in styles.css). Capture phase
  // and window scope so the flag is set before the navigation the input triggers
  // (keydown Enter / a tap) reaches the router. pointerdown covers mouse, touch
  // and pen on current browsers; touchstart is the fallback for an engine without
  // Pointer Events.
  window.addEventListener("keydown", () => {
    navByKeyboard = true;
  }, true);
  const markPointerNav = () => {
    navByKeyboard = false;
  };
  window.addEventListener("pointerdown", markPointerNav, true);
  window.addEventListener("touchstart", markPointerNav, true);
  window.addEventListener("hashchange", router);
  // Reflect the saved mode + palette in the header Appearance popover, re-apply them
  // (the pre-paint script in index.html already set data-theme/data-palette), and
  // start following OS changes while Mode is System.
  initAppearance();
  router();
})();
