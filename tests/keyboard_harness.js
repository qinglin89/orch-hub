/*
 * Keyboard smoke harness for the dashboard SPA, run under macOS JavaScriptCore
 * (`jsc`) by tests/test_web_keyboard.py.
 *
 * There is no browser in this repo's test environment, and the served-asset
 * boundary tests in tests/test_web.py can only assert that a string appears in
 * app.js — they cannot tell whether a route change actually moves focus, whether
 * Escape closes the launch dialog, or whether focus comes back afterwards. So
 * this file stubs just enough of a browser (HTML parse, a small CSS selector
 * engine, bubbling events, focus/tab order, fetch, localStorage, a manual timer
 * queue) to drive the REAL orch_hub/web/app.js against the REAL
 * orch_hub/web/index.html shell and assert keyboard behaviour.
 *
 * Scope is deliberately the acceptance list: login, navigation, task detail,
 * run options, intake review. It is a smoke test, not a browser: unsupported
 * corners fail loudly rather than being silently emulated.
 *
 * Output: one `ok - <name>` / `not ok - <name>: <reason>` line per check and a
 * final `SUMMARY <pass>/<total>` line, which the pytest wrapper asserts on.
 */
"use strict";

/* ------------------------------------------------------------------ results */

var PASSED = 0;
var FAILED = 0;

function check(name, cond, detail) {
  if (cond) {
    PASSED++;
    print("ok - " + name);
  } else {
    FAILED++;
    print("not ok - " + name + (detail ? ": " + detail : ""));
  }
}

function eq(name, actual, expected) {
  check(
    name,
    actual === expected,
    "expected " + JSON.stringify(expected) + ", got " + JSON.stringify(actual)
  );
}

/* -------------------------------------------------------------- HTML parser */

var VOID_TAGS = {
  area: 1, base: 1, br: 1, col: 1, embed: 1, hr: 1, img: 1, input: 1,
  link: 1, meta: 1, param: 1, source: 1, track: 1, wbr: 1,
};

var ENTITIES = {
  "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'",
  "&nbsp;": " ", "&#9888;": "⚠",
};

function decodeEntities(text) {
  return text.replace(/&(?:amp|lt|gt|quot|nbsp|#39|#9888);/g, function (m) {
    return ENTITIES[m] !== undefined ? ENTITIES[m] : m;
  });
}

function TextNode(text) {
  this.nodeType = 3;
  this.data = text;
  this.parentNode = null;
}

var NEXT_ORDER = 1; // monotonic stamp used to compare document order

function Element(tag) {
  this.nodeType = 1;
  this.tagName = tag.toUpperCase();
  this.attributes = {};
  this.childNodes = [];
  this.parentNode = null;
  this.listeners = {};
  this.disabled = false;
  this.hidden = false;
  this.order = NEXT_ORDER++;
  if (this.tagName === "DIALOG") {
    this.open = false;
  }
}

// Parse a fragment of HTML into an array of nodes. Understands tags,
// attributes, comments, void/self-closed elements and text; that is all the
// SPA's generated markup and the shipped index.html contain.
function parseHtml(html) {
  var root = new Element("fragment");
  var stack = [root];
  var i = 0;
  while (i < html.length) {
    var lt = html.indexOf("<", i);
    if (lt === -1) {
      pushText(stack[stack.length - 1], html.slice(i));
      break;
    }
    if (lt > i) pushText(stack[stack.length - 1], html.slice(i, lt));
    if (html.substr(lt, 4) === "<!--") {
      var end = html.indexOf("-->", lt);
      i = end === -1 ? html.length : end + 3;
      continue;
    }
    if (html.substr(lt, 2) === "<!") {
      i = html.indexOf(">", lt) + 1; // doctype
      continue;
    }
    var gt = findTagEnd(html, lt);
    var raw = html.slice(lt + 1, gt);
    if (raw[0] === "/") {
      var closing = raw.slice(1).trim().toLowerCase();
      for (var s = stack.length - 1; s > 0; s--) {
        if (stack[s].tagName.toLowerCase() === closing) {
          stack.length = s;
          break;
        }
      }
      i = gt + 1;
      continue;
    }
    var selfClosed = raw[raw.length - 1] === "/";
    if (selfClosed) raw = raw.slice(0, -1);
    var el = buildElement(raw);
    stack[stack.length - 1].appendChild(el);
    if (!selfClosed && !VOID_TAGS[el.tagName.toLowerCase()]) stack.push(el);
    i = gt + 1;
  }
  return root.childNodes.slice();
}

// The `>` that ends a tag, skipping any inside a quoted attribute value.
function findTagEnd(html, start) {
  var quote = null;
  for (var i = start + 1; i < html.length; i++) {
    var ch = html[i];
    if (quote) {
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === ">") {
      return i;
    }
  }
  return html.length;
}

function pushText(parent, text) {
  if (!text) return;
  parent.appendChild(new TextNode(decodeEntities(text)));
}

var ATTR_RE = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;

function buildElement(raw) {
  var m = /^([a-zA-Z][-a-zA-Z0-9]*)/.exec(raw);
  var el = new Element(m[1]);
  var rest = raw.slice(m[1].length);
  ATTR_RE.lastIndex = 0;
  var a;
  while ((a = ATTR_RE.exec(rest)) !== null) {
    var value = a[2] !== undefined ? a[2] : a[3] !== undefined ? a[3] : a[4];
    el.setAttribute(a[1].toLowerCase(), decodeEntities(value === undefined ? "" : value));
  }
  finishElement(el);
  return el;
}

// Form-control state that a browser derives from markup at parse time.
function finishElement(el) {
  var tag = el.tagName;
  if (tag === "INPUT") {
    el._value = el.getAttribute("value") || "";
    el.checked = el.hasAttribute("checked");
  } else if (tag === "TEXTAREA") {
    el._value = "";
  }
  // A <select> is left without a stored value: the accessor below resolves it
  // from the selected <option>, or the first one — the browser behaviour
  // buildExtraArgs relies on when every override field is left at "default".
  if (el.hasAttribute("disabled")) el.disabled = true;
  if (el.hasAttribute("hidden")) el.hidden = true;
  if (tag === "DIALOG") {
    if (el.hasAttribute("open")) el.open = true;
    addDialogMethods(el);
  }
}

// The native <dialog> API app.js prefers. showModal focuses into the dialog
// (which is why openRunOptions only focuses by hand on the fallback path), and
// close fires a non-bubbling `close` event. It deliberately does NOT restore
// focus to the opener — that restoration is app.js's job and is under test.
function addDialogMethods(el) {
  el.showModal = function () {
    this.open = true;
    this.setAttribute("open", "");
    var first = this.descendants().filter(isTabbable)[0];
    if (first) first.focus();
  };
  el.close = function () {
    if (!this.open) return;
    this.open = false;
    this.removeAttribute("open");
    dispatch(this, new Event("close", { bubbles: false }));
  };
}

/* ----------------------------------------------------------------- Element */

Element.prototype.appendChild = function (node) {
  // A document fragment splices its children in, as in a browser.
  if (node.nodeType === 1 && node.tagName === "FRAGMENT") {
    var kids = node.childNodes.slice();
    for (var i = 0; i < kids.length; i++) this.appendChild(kids[i]);
    return node;
  }
  node.parentNode = this;
  this.childNodes.push(node);
  return node;
};

// `value` is stored for typed-into controls and derived for a <select> that has
// never been assigned: the selected <option>, else the first one.
Object.defineProperty(Element.prototype, "value", {
  get: function () {
    if (this._value !== undefined) return this._value;
    if (this.tagName !== "SELECT") return undefined;
    var options = this.querySelectorAll("option");
    var selected = options.filter(function (o) {
      return o.hasAttribute("selected");
    })[0];
    var chosen = selected || options[0];
    return chosen ? chosen.getAttribute("value") || "" : "";
  },
  set: function (v) {
    this._value = v;
  },
});

Element.prototype.setAttribute = function (name, value) {
  this.attributes[name.toLowerCase()] = String(value);
};
Element.prototype.getAttribute = function (name) {
  var v = this.attributes[name.toLowerCase()];
  return v === undefined ? null : v;
};
Element.prototype.hasAttribute = function (name) {
  return this.attributes[name.toLowerCase()] !== undefined;
};
Element.prototype.removeAttribute = function (name) {
  delete this.attributes[name.toLowerCase()];
};

Object.defineProperty(Element.prototype, "id", {
  get: function () {
    return this.getAttribute("id") || "";
  },
});

Object.defineProperty(Element.prototype, "className", {
  get: function () {
    return this.getAttribute("class") || "";
  },
  set: function (v) {
    this.setAttribute("class", v);
  },
});

Object.defineProperty(Element.prototype, "classList", {
  get: function () {
    var el = this;
    var parts = function () {
      return (el.getAttribute("class") || "").split(/\s+/).filter(Boolean);
    };
    var write = function (list) {
      el.setAttribute("class", list.join(" "));
    };
    return {
      contains: function (c) {
        return parts().indexOf(c) !== -1;
      },
      add: function (c) {
        var p = parts();
        if (p.indexOf(c) === -1) p.push(c);
        write(p);
      },
      remove: function (c) {
        write(parts().filter(function (x) {
          return x !== c;
        }));
      },
      toggle: function (c, force) {
        var has = parts().indexOf(c) !== -1;
        var on = force === undefined ? !has : !!force;
        if (on) this.add(c);
        else this.remove(c);
        return on;
      },
    };
  },
});

Object.defineProperty(Element.prototype, "dataset", {
  get: function () {
    var out = {};
    for (var name in this.attributes) {
      if (name.indexOf("data-") !== 0) continue;
      var key = name.slice(5).replace(/-([a-z])/g, function (_, c) {
        return c.toUpperCase();
      });
      out[key] = this.attributes[name];
    }
    return out;
  },
});

Object.defineProperty(Element.prototype, "children", {
  get: function () {
    return this.childNodes.filter(function (n) {
      return n.nodeType === 1;
    });
  },
});

Object.defineProperty(Element.prototype, "textContent", {
  get: function () {
    var out = "";
    for (var i = 0; i < this.childNodes.length; i++) {
      var n = this.childNodes[i];
      out += n.nodeType === 3 ? n.data : n.textContent;
    }
    return out;
  },
  set: function (text) {
    this.childNodes = [];
    if (text !== "") this.appendChild(new TextNode(String(text)));
  },
});

Object.defineProperty(Element.prototype, "innerHTML", {
  set: function (html) {
    this.childNodes = [];
    var nodes = parseHtml(String(html));
    for (var i = 0; i < nodes.length; i++) this.appendChild(nodes[i]);
  },
});

// Depth-first element descendants, in document order.
Element.prototype.descendants = function () {
  var out = [];
  var walk = function (node) {
    for (var i = 0; i < node.childNodes.length; i++) {
      var c = node.childNodes[i];
      if (c.nodeType !== 1) continue;
      out.push(c);
      walk(c);
    }
  };
  walk(this);
  return out;
};

Element.prototype.querySelectorAll = function (selector) {
  var self = this;
  return this.descendants().filter(function (el) {
    return matchesSelector(el, selector, self);
  });
};

Element.prototype.querySelector = function (selector) {
  return this.querySelectorAll(selector)[0] || null;
};

Element.prototype.closest = function (selector) {
  var node = this;
  while (node && node.nodeType === 1) {
    if (matchesSelector(node, selector, null)) return node;
    node = node.parentNode;
  }
  return null;
};

Element.prototype.contains = function (other) {
  var node = other;
  while (node) {
    if (node === this) return true;
    node = node.parentNode;
  }
  return false;
};

Element.prototype.focus = function () {
  if (this.disabled) return; // a disabled control cannot take focus
  doc.activeElement = this;
};

Element.prototype.addEventListener = function (type, fn) {
  (this.listeners[type] = this.listeners[type] || []).push(fn);
};

Element.prototype.dispatchEvent = function (event) {
  dispatch(this, event);
};

/* --------------------------------------------------------- selector engine */

// Supports the subset app.js uses: comma lists, descendant combinators, and
// compound simple selectors of tag / #id / .class / [attr] / [attr="value"].
function matchesSimple(el, sel) {
  var re = /(^|\.|#|\[)([^.#\[\]]*)(?:=("?)([^\]"]*)\3)?\]?/g;
  var parts = sel.match(/^[a-zA-Z][-a-zA-Z0-9]*|\.[-\w]+|#[-\w]+|\[[^\]]+\]/g);
  if (!parts) return false;
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i];
    if (p[0] === ".") {
      if (!el.classList.contains(p.slice(1))) return false;
    } else if (p[0] === "#") {
      if (el.id !== p.slice(1)) return false;
    } else if (p[0] === "[") {
      var body = p.slice(1, -1);
      var eqAt = body.indexOf("=");
      if (eqAt === -1) {
        if (!el.hasAttribute(body)) return false;
      } else {
        var name = body.slice(0, eqAt);
        var want = body.slice(eqAt + 1).replace(/^["']|["']$/g, "");
        if (el.getAttribute(name) !== want) return false;
      }
    } else if (el.tagName.toLowerCase() !== p.toLowerCase()) {
      return false;
    }
  }
  return true;
}

function matchesSelector(el, selector, scope) {
  var alternatives = selector.split(",");
  for (var a = 0; a < alternatives.length; a++) {
    var steps = alternatives[a].trim().split(/\s+/).filter(Boolean);
    if (!steps.length) continue;
    if (!matchesSimple(el, steps[steps.length - 1])) continue;
    var node = el.parentNode;
    var ok = true;
    for (var s = steps.length - 2; s >= 0; s--) {
      var found = false;
      while (node && node.nodeType === 1) {
        if (matchesSimple(node, steps[s])) {
          found = true;
          node = node.parentNode;
          break;
        }
        node = node.parentNode;
      }
      if (!found) {
        ok = false;
        break;
      }
    }
    if (ok && (!scope || scope.contains(el))) return true;
  }
  return false;
}

/* ------------------------------------------------------------------ events */

function Event(type, init) {
  this.type = type;
  this.defaultPrevented = false;
  this.target = null;
  this.currentTarget = null;
  this.bubbles = !init || init.bubbles !== false;
  if (init) for (var k in init) if (k !== "bubbles") this[k] = init[k];
}
Event.prototype.preventDefault = function () {
  this.defaultPrevented = true;
};

function dispatch(target, event) {
  event.target = target;
  var node = target;
  while (node) {
    var fns = node.listeners ? node.listeners[event.type] : null;
    if (fns) {
      event.currentTarget = node;
      for (var i = 0; i < fns.length; i++) fns[i].call(node, event);
    }
    if (!event.bubbles) break;
    node = node.parentNode;
  }
  return event;
}

/* -------------------------------------------------- document / window stubs */

var doc = new Element("document");
doc.title = "";
doc.activeElement = null;

doc.getElementById = function (id) {
  var all = doc.descendants();
  for (var i = 0; i < all.length; i++) if (all[i].id === id) return all[i];
  return null;
};
doc.createTextNode = function (text) {
  return new TextNode(text);
};
doc.createDocumentFragment = function () {
  return new Element("fragment");
};

var win = new Element("window");

// A manual timer queue: the Overview auto-refresh and the intake poll both use
// setTimeout, and a smoke test must not have them firing mid-assertion. Nothing
// drains this queue, so scheduled work is observable but inert.
var TIMERS = [];
globalThis.setTimeout = function (fn, ms) {
  TIMERS.push({ fn: fn, ms: ms });
  return TIMERS.length;
};
globalThis.clearTimeout = function (id) {
  if (id) TIMERS[id - 1] = null;
};

globalThis.localStorage = (function () {
  var store = {};
  return {
    getItem: function (k) {
      return store[k] === undefined ? null : store[k];
    },
    setItem: function (k, v) {
      store[k] = String(v);
    },
    removeItem: function (k) {
      delete store[k];
    },
  };
})();

globalThis.location = { hash: "" };
globalThis.document = doc;
globalThis.window = win;
globalThis.console = { log: print, error: print, warn: print };
globalThis.AbortController = function () {
  this.signal = { aborted: false };
  var self = this;
  this.abort = function () {
    self.signal.aborted = true;
  };
};
globalThis.TextDecoder = function () {
  this.decode = function () {
    return "";
  };
};

/* ------------------------------------------------------------- fetch stub */

var ROUTES = {};   // "METHOD path" (query stripped) -> payload
var CALLS = [];    // every request, for assertions
var DEFERRED = {}; // "METHOD path" -> a promise holding that route's response open

function route(method, path, payload) {
  ROUTES[method + " " + path] = payload;
}

// Hold a route's response open until the scenario releases it. Every other stub
// resolves immediately, so this is the only way to park a route handler mid-await
// and navigate on top of it — i.e. to exercise a stale route completion.
function defer(method, path) {
  var key = method + " " + path;
  var release;
  DEFERRED[key] = new Promise(function (resolve) {
    release = resolve;
  });
  return function () {
    delete DEFERRED[key];
    release();
    return flush();
  };
}

globalThis.fetch = function (path, init) {
  var method = (init && init.method) || "GET";
  var body = init && init.body ? JSON.parse(init.body) : null;
  var bare = String(path).split("?")[0];
  CALLS.push({ method: method, path: bare, body: body, init: init });
  var payload = ROUTES[method + " " + bare];
  var response =
    payload === undefined
      ? {
          ok: false,
          status: 404,
          statusText: "Not Found",
          json: function () {
            return Promise.resolve({
              detail: "no stub for " + method + " " + bare,
            });
          },
        }
      : {
          ok: true,
          status: 200,
          statusText: "OK",
          json: function () {
            return Promise.resolve(payload);
          },
        };
  var gate = DEFERRED[method + " " + bare];
  return gate
    ? gate.then(function () {
        return response;
      })
    : Promise.resolve(response);
};

/* --------------------------------------------------- keyboard interactions */

// Elements a browser would put in the tab order: natively focusable, or opted
// in with a non-negative tabindex; not disabled and not inside a hidden
// subtree. `tabindex="-1"` targets (the page heading, #view) are reachable
// programmatically but must NOT be in this list.
function isHiddenDeep(el) {
  var node = el;
  while (node && node.nodeType === 1) {
    if (node.hidden) return true;
    node = node.parentNode;
  }
  return false;
}

function isTabbable(el) {
  if (el.disabled || isHiddenDeep(el)) return false;
  var ti = el.getAttribute("tabindex");
  if (ti !== null) return Number(ti) >= 0;
  var tag = el.tagName;
  if (tag === "A") return el.hasAttribute("href");
  return tag === "BUTTON" || tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
}

function tabOrder() {
  return doc.descendants().filter(isTabbable);
}

// Move focus like Tab / Shift+Tab. When focus sits on a programmatic-only
// target (tabindex="-1"), continue from its position in document order — which
// is exactly the "no trap, Tab continues into the view" behaviour under test.
function pressTab(shift) {
  var order = tabOrder();
  var active = doc.activeElement;
  var idx = order.indexOf(active);
  if (idx === -1) {
    var after = order.filter(function (el) {
      return active ? el.order > active.order : true;
    });
    var next = shift ? order[order.length - 1] : after[0];
    if (next) next.focus();
    return doc.activeElement;
  }
  var target = order[idx + (shift ? -1 : 1)];
  if (target) target.focus();
  return doc.activeElement;
}

// Activating a control from the keyboard (Enter/Space) produces a click event;
// a submit button additionally submits its form.
function pressEnter(el) {
  el.focus();
  dispatch(el, new Event("keydown", { key: "Enter" }));
  dispatch(el, new Event("click"));
  if (el.tagName === "BUTTON" && el.getAttribute("type") === "submit") {
    var form = el.closest("form");
    if (form) dispatch(form, new Event("submit"));
  }
  return doc.activeElement;
}

function pressEscape() {
  dispatch(doc.activeElement || doc, new Event("keydown", { key: "Escape" }));
}

function type(el, text) {
  el.focus();
  el.value = text;
  dispatch(el, new Event("input"));
}

function flush() {
  // Drain the microtask queue: every render path is a chain of awaited
  // already-resolved fetch promises, so a bounded number of turns settles it.
  var p = Promise.resolve();
  for (var i = 0; i < 60; i++) p = p.then(function () {});
  return p;
}

function navigate(hash) {
  location.hash = hash;
  dispatch(win, new Event("hashchange"));
  return flush();
}

function activeIs(name, el) {
  check(
    name,
    doc.activeElement === el,
    "focus is on " + describe(doc.activeElement) + ", expected " + describe(el)
  );
}

function describe(el) {
  if (!el) return "<none>";
  return (
    "<" +
    el.tagName.toLowerCase() +
    (el.id ? "#" + el.id : "") +
    (el.className ? "." + el.className.split(/\s+/).join(".") : "") +
    ">"
  );
}

function pageHeading() {
  return doc.getElementById("view").querySelector(".page-title");
}

// Heading levels inside the routed view, in document order. A screen-reader
// user navigates by these, so each view must expose exactly one h1 (the page)
// and descend without skipping a level.
function headingLevels() {
  return doc
    .getElementById("view")
    .descendants()
    .filter(function (el) {
      return /^H[1-6]$/.test(el.tagName);
    })
    .map(function (el) {
      return Number(el.tagName.slice(1));
    });
}

function checkOutline(label) {
  var levels = headingLevels();
  eq(
    "outline (" + label + "): exactly one h1",
    levels.filter(function (l) {
      return l === 1;
    }).length,
    1
  );
  eq("outline (" + label + "): the h1 comes first", levels[0], 1);
  var skip = null;
  for (var i = 1; i < levels.length; i++)
    if (levels[i] > levels[i - 1] + 1) skip = "h" + levels[i - 1] + " → h" + levels[i];
  check("outline (" + label + "): no skipped heading level", skip === null, skip);
}

function lastCall(method, path) {
  for (var i = CALLS.length - 1; i >= 0; i--)
    if (CALLS[i].method === method && CALLS[i].path === path) return CALLS[i];
  return null;
}

/* ------------------------------------------------------------ fixture data */

var REPO = "demo-repo";
var TASK = "2026-07-17-demo";
var RUN = "run-1";

var REPOS = {
  repos: [
    {
      name: REPO,
      path: "/tmp/" + REPO,
      readiness: { ready: true, checks: [] },
      housekeeping_pending: false,
      commit: null,
    },
  ],
};
var TASKS = {
  repos: [
    {
      name: REPO,
      path: "/tmp/" + REPO,
      index_present: true,
      tasks: [
        {
          task_id: TASK,
          status: "pending",
          summary: "Demo task",
          blockers: [],
        },
      ],
    },
  ],
};

// A deterministic Layer 1 static-metrics report (StaticMetricsReport.to_dict
// shape), rich enough to exercise the scores, run/session rollup, protocol
// facts, an n/a (non-assessable) axis, a per-score warning, and a pack-level
// evidence warning — plus a partial-evidence signal (a run that failed with no
// diagnostic). The eval view renders this; the raw evidence pack below is only
// offered as a copy/download artifact.
var EVAL_ID = "20260722T090000Z";
var EVAL_REPORT = {
  evaluation_id: EVAL_ID,
  generated_ts: "2026-07-22T09:00:00Z",
  repo_name: REPO,
  task_id: TASK,
  target_present: true,
  target_source: "active",
  scores: [
    { name: "protocol", score: 0.9, confidence: 1.0, warnings: [] },
    {
      name: "reliability",
      score: 0.5,
      confidence: 0.8,
      warnings: ["1 run failed without a diagnostic"],
    },
    { name: "handoff", score: null, confidence: 0.0, warnings: [] },
    { name: "estimate", score: 1.0, confidence: 1.0, warnings: [] },
    { name: "knowledge", score: 0.75, confidence: 0.6, warnings: [] },
  ],
  warnings: ["run run-1 sessions.json missing"],
  runs: {
    evaluation_id: EVAL_ID,
    target_present: true,
    target_source: "active",
    run_count: 2,
    live_count: 0,
    clean_count: 1,
    failed_count: 1,
    stopped_count: 0,
    ended_unknown_count: 0,
    broken_count: 0,
    failure_count: 1,
    failed_without_diagnostic_count: 1,
    human_input_count: 1,
    dev_session_count: 2,
    review_session_count: 1,
    plan_gate_count: 1,
    total_turns: 12,
    peak_context_tokens: 120000,
    total_duration_seconds: 1800,
    warning_count: 1,
    runs: [],
  },
  protocol: {
    task_content_present: true,
    frontmatter_present: true,
    frontmatter_status: "in_progress",
    status_legal: true,
    id_present: true,
    id_matches: true,
    session_est: { current: 2, total: 3 },
    session_est_present: true,
    session_est_wellformed: true,
    blocker_rule_ok: true,
    fix_set_present: false,
    prefetch_present: true,
    prefetch_wellformed: true,
    missing_sections: [],
    has_session_plan: true,
    session_log_present: true,
    entry_count: 3,
    work_entry_count: 2,
    review_entry_count: 1,
    malformed_entry_count: 0,
    uncovered_work_entries: [],
    pass_verdict_count: 1,
    changes_requested_count: 0,
    remediation_count: 0,
    blocked_entry_count: 0,
    dispute_unresolved_count: 0,
    incomplete_entries: [],
    illegal_transitions: [],
    transition_continuity_ok: true,
    final_status_matches: true,
    completed: false,
    final_open_none: false,
  },
};
var EVAL_EVIDENCE = {
  evaluation_id: EVAL_ID,
  generated_ts: "2026-07-22T09:00:00Z",
  target: { repo_name: REPO, task_id: TASK, source: "active", present: true, content: "# Demo task\n" },
  runs: [],
  git: { available: false },
  warnings: ["run run-1 sessions.json missing"],
  summary: { run_count: 0, runs_ended: 0, has_task_content: true, git_available: false, warning_count: 1 },
};

// A Layer 2 semantic report (SemanticReport.to_dict shape). Findings are given in
// a non-severity order so the render's most-severe-first sort is exercised; one
// score axis is non-assessable (null → n/a); every axis carries a rationale and
// every finding cites evidence, as the schema requires.
var SEM_REPORT = {
  evaluation_id: EVAL_ID,
  generated_ts: "2026-07-22T09:05:00Z",
  repo_name: REPO,
  task_id: TASK,
  target_present: true,
  target_source: "active",
  evaluator: { backend: "claude-cli", model: "claude-opus-4-8", schema_version: "1" },
  summary: "The task handed off cleanly but the decision trace is thin.",
  overall_confidence: 0.7,
  scores: [
    { dimension: "handoff_completeness", score: 0.8, confidence: 0.9, rationale: "Session log carried Next/Open." },
    { dimension: "ai_freshness_utility", score: null, confidence: 0.0, rationale: "Not assessable without .ai/ contents." },
    { dimension: "decision_trace", score: 0.4, confidence: 0.7, rationale: "Rejected alternatives were not recorded." },
  ],
  findings: [
    {
      dimension: "decision_trace",
      severity: "medium",
      summary: "Rejected alternatives absent from the log.",
      detail: "The session log states the choice but not what was rejected.",
      confidence: 0.7,
      evidence_refs: ["session-log entry sid-b"],
      suggestion: "Record rejected alternatives in the Done bullet.",
    },
    {
      dimension: "acceptance_coverage",
      severity: "high",
      summary: "One acceptance criterion has no evidence.",
      detail: null,
      confidence: 0.8,
      evidence_refs: ["task Acceptance line 3"],
      suggestion: "Add a test proving the third criterion.",
    },
    {
      dimension: "protocol_soft_failures",
      severity: "low",
      summary: "session-est was raised late.",
      confidence: 0.5,
      evidence_refs: ["frontmatter session-est"],
      suggestion: null,
    },
  ],
  improvements: ["Prompt dev sessions to record rejected alternatives."],
  input_evidence_id: EVAL_ID,
  input_static_metrics: true,
  warnings: [".ai/ file contents unavailable to the evaluator"],
};
var SEM_REPORT_MD = "# Semantic Task Evaluation — " + TASK + "\n\n- Overall confidence: 0.70\n";
// The detail payload for an evaluation that carries the Layer 2 report too.
var EVAL_DETAIL_SEMANTIC = {
  evaluation_id: EVAL_ID,
  evidence: EVAL_EVIDENCE,
  static_metrics: EVAL_REPORT,
  semantic_report: SEM_REPORT,
  report_md: SEM_REPORT_MD,
};

function installRoutes(opts) {
  ROUTES = {};
  var waiting = opts && opts.waiting;
  route("GET", "/api/repos", REPOS);
  route("GET", "/api/tasks", TASKS);
  route("GET", "/api/escalations", {
    escalations: waiting
      ? [
          {
            run_id: RUN,
            repo_name: REPO,
            task_id: TASK,
            question: { seq: 1, kind: "escalation", message: "Which way?" },
          },
        ]
      : [],
  });
  route("GET", "/api/runs", {
    runs: waiting
      ? [
          {
            run_id: RUN,
            repo_name: REPO,
            task_id: TASK,
            state: "waiting",
            started_ts: "2026-07-20T08:00:00Z",
            pending_questions: 1,
            stop_requested: false,
          },
        ]
      : [],
  });
  route("GET", "/api/history/runs", { runs: [] });
  route("GET", "/api/intakes", { intakes: [] });
  route("GET", "/api/repos/" + REPO + "/tasks", { repos: [TASKS.repos[0]] });
  route("GET", "/api/repos/" + REPO + "/commits", { available: true, commits: [] });
  route("GET", "/api/repos/" + REPO + "/tasks/" + TASK, {
    task_id: TASK,
    status: "pending",
    summary: "Demo task",
    blockers: [],
    present: true,
    content: "# Demo task\n",
  });
  route("GET", "/api/runs/" + RUN + "/sessions", { sessions: [], context_budget: 200000 });
  route("GET", "/api/runs/" + RUN, {
    run_id: RUN,
    repo_name: REPO,
    task_id: TASK,
    state: "completed",
    started_ts: "2026-07-20T08:00:00Z",
    ended_ts: "2026-07-20T08:30:00Z",
  });
  route("GET", "/api/runs/" + RUN + "/logs", {
    present: true,
    truncated: false,
    lines: ["08:00:00 dev session started", "08:30:00 done"],
  });
  route("POST", "/api/runs", { run_id: RUN });
  route("POST", "/api/runs/" + RUN + "/escalations/1/answer", { ok: true });
  route("GET", "/api/intakes/i1/history", {
    intake_id: "i1",
    repo_name: REPO,
    status: "awaiting_review",
    request: "Add a thing",
    updated_ts: "2026-07-20T08:00:00Z",
    draft: {
      task_id: "2026-07-20-thing",
      summary: "Add a thing",
      notes: "",
      task_file: "---\nid: 2026-07-20-thing\n---\n",
      index_entry: "| 2026-07-20-thing | pending | Add a thing |",
    },
    events: [],
  });
  route("POST", "/api/intakes/i1/cancel", { intake_id: "i1", status: "cancelled" });
  route("GET", "/api/intakes/i1", {
    intake_id: "i1",
    repo_name: REPO,
    status: "cancelled",
    request: "Add a thing",
    updated_ts: "2026-07-20T08:00:00Z",
    events: [],
  });
  // Task evaluation: availability (enabled by default; a scenario flips it to
  // exercise the disabled branch and the entry-link gating), the (repo, task)
  // list, one evaluation's detail, and the deterministic generate POST.
  route("GET", "/api/evaluation", { enabled: true });
  // Layer 2 backend readiness: enabled + available by default; a scenario flips
  // `available` to exercise the disabled-trigger gating while Layer 1 stays usable.
  route("GET", "/api/evaluation/doctor", {
    enabled: true,
    semantic: {
      backend: "claude-cli",
      model: "claude-opus-4-8",
      schema_version: "1",
      available: true,
      detail: "claude resolved at /usr/local/bin/claude",
    },
  });
  route("GET", "/api/repos/" + REPO + "/evaluations/" + TASK, {
    repo: REPO,
    task_id: TASK,
    enabled: true,
    target: { present: true, source: "active" },
    latest_evaluation_id: EVAL_ID,
    evaluations: [
      {
        evaluation_id: EVAL_ID,
        has_evidence: true,
        has_static_metrics: true,
        has_semantic_report: false,
      },
    ],
  });
  route("GET", "/api/repos/" + REPO + "/evaluations/" + TASK + "/" + EVAL_ID, {
    evaluation_id: EVAL_ID,
    evidence: EVAL_EVIDENCE,
    static_metrics: EVAL_REPORT,
    semantic_report: null,
    report_md: null,
  });
  route("POST", "/api/repos/" + REPO + "/evaluations/" + TASK + "/metrics", {
    evaluation_id: EVAL_ID,
    static_metrics: EVAL_REPORT,
  });
  route("POST", "/api/repos/" + REPO + "/evaluations/" + TASK + "/semantic", {
    evaluation_id: EVAL_ID,
    semantic_report: SEM_REPORT,
  });
}

/* ------------------------------------------------------------------- boot */

// The real shipped shell, so the harness exercises the markup that is served.
var shellHtml = readFile("orch_hub/web/index.html");
var bodyHtml = /<body[^>]*>([\s\S]*)<\/body>/.exec(shellHtml)[1];
var body = new Element("body");
doc.appendChild(body);
doc.body = body;
body.innerHTML = bodyHtml;

installRoutes({ waiting: true });
(0, eval)(readFile("orch_hub/web/app.js")); // runs the IIFE, which calls router()

/* -------------------------------------------------------------- scenarios */

async function scenarioLogin() {
  await flush();
  var heading = pageHeading();
  check("login: view opens with a page heading", !!heading, "no .page-title");
  eq("login: heading names the gate", heading && heading.textContent.trim(), "Connect to orch-hub");
  eq("login: document.title tracks the page", doc.title, "Connect to orch-hub · orch-hub");
  activeIs("login: focus lands on the heading", heading);
  eq("login: heading is programmatic-only focus", heading.getAttribute("tabindex"), "-1");
  // The gate drops any per-view width class: the login card centres itself and
  // the desktop rail is hidden, so the workspace is the plain full-width view.
  eq("login: the gate uses the plain full-width view", doc.getElementById("view").className, "view");

  var input = doc.getElementById("token-input");
  var label = doc.querySelector('label[for="token-input"]');
  check("login: the token field has a real label", !!label, "no label[for=token-input]");
  // Tab out of the heading: not a trap, and the first stop is the token field.
  activeIs("login: Tab from the heading reaches the token field", (pressTab(), input));
  var connect = doc.querySelector("#login-form button");
  activeIs("login: Tab again reaches Connect", (pressTab(), connect));

  type(input, "secret-token");
  pressEnter(connect); // submit button → form submit
  await flush();
  check("login: connecting leaves the gate", !doc.getElementById("token-input"), "still on the login form");
}

async function scenarioNavigation() {
  eq("nav: landing route is the overview", location.hash, "");
  var heading = pageHeading();
  eq("nav: overview names itself", heading && heading.textContent.trim(), "Operations overview");
  activeIs("nav: focus lands on the overview heading", heading);
  eq("nav: document.title tracks the route", doc.title, "Operations overview · orch-hub");

  var tabs = doc.querySelectorAll(".tabbar a");
  eq("nav: chrome is visible once authed", doc.getElementById("nav").hidden, false);
  var current = tabs.filter(function (a) {
    return a.getAttribute("aria-current") === "page";
  });
  eq("nav: exactly one tab is aria-current", current.length, 1);
  eq("nav: the overview tab is the current one", current[0].getAttribute("data-tab"), "/overview");

  await navigate("#/tasks");
  var tasksHeading = pageHeading();
  eq("nav: the tasks route names itself", tasksHeading && tasksHeading.textContent.trim(), "Tasks");
  activeIs("nav: a route change moves focus to the new heading", tasksHeading);
  eq("nav: document.title follows the route", doc.title, "Tasks · orch-hub");
  var nowCurrent = doc.querySelectorAll(".tabbar a").filter(function (a) {
    return a.getAttribute("aria-current") === "page";
  });
  eq("nav: aria-current moved with the route", nowCurrent.length && nowCurrent[0].getAttribute("data-tab"), "/tasks");

  // Focus is placed before the content, and Tab continues into it (no trap).
  activeIs("nav: Tab from the heading enters the view", (pressTab(), doc.getElementById("f-repo")));
  pressTab();
  eq("nav: Tab continues through the filters", doc.activeElement.id, "f-status");

  // The skip link is the first tab stop of the document and targets the view.
  var skip = doc.getElementById("skip-link");
  check("nav: a skip link is the document's first tab stop", tabOrder()[0] === skip, describe(tabOrder()[0]));
  doc.body.focus ? null : null;
  doc.activeElement = null;
  dispatch(skip, new Event("click"));
  activeIs("nav: the skip link moves focus into the view", pageHeading());
}

async function scenarioAppearance() {
  // The Appearance popover lives in the shell header (outside #view), so it is
  // present on every view including the login gate and survives route repaints. A
  // compact toggle discloses a panel holding the two INDEPENDENT preference selects
  // (mode + palette), so both axes fit a 320px header without overflow.
  var toggle = doc.getElementById("appearance-toggle");
  check("theme: the header exposes an appearance toggle", !!toggle, "no #appearance-toggle");
  eq("theme: the toggle is a real button for full keyboard/touch support", toggle.tagName, "BUTTON");
  check("theme: the toggle carries an accessible name", !!toggle.getAttribute("aria-label"), "no aria-label");
  check("theme: the toggle is keyboard reachable", isTabbable(toggle), "the appearance toggle is not tabbable");

  // The panel starts collapsed; the toggle reports it, and disclosing it opens the
  // panel (so the selects inside become reachable).
  var panel = doc.getElementById("appearance-panel");
  check("theme: the panel starts collapsed", panel.hidden === true, "panel not hidden initially");
  eq("theme: the toggle reports collapsed", toggle.getAttribute("aria-expanded"), "false");
  dispatch(toggle, new Event("click"));
  check("theme: activating the toggle opens the panel", panel.hidden === false, "panel still hidden after activation");
  eq("theme: the toggle reports expanded", toggle.getAttribute("aria-expanded"), "true");

  // ---- mode axis (Dark / Light / System) ----
  var mode = doc.getElementById("theme-select");
  check("theme: the panel exposes a mode control", !!mode, "no #theme-select");
  eq("theme: mode is a native select for full keyboard/touch support", mode.tagName, "SELECT");
  check("theme: mode carries an accessible name", !!mode.getAttribute("aria-label"), "no aria-label");
  check("theme: mode is keyboard reachable when disclosed", isTabbable(mode), "the mode select is not tabbable");
  var modeValues = doc.querySelectorAll("#theme-select option").map(function (o) {
    return o.getAttribute("value");
  });
  eq("theme: it offers Dark, Light and System", modeValues.join(","), "dark,light,system");
  // First run has no stored preference, so the control reflects the Dark default.
  eq("theme: it reflects the Dark default with nothing stored", mode.value, "dark");

  // ---- palette axis (Neon Cyan / Wasteland Orange) ----
  var palette = doc.getElementById("palette-select");
  check("palette: the panel exposes a palette control", !!palette, "no #palette-select");
  eq("palette: it is a native select for full keyboard/touch support", palette.tagName, "SELECT");
  check("palette: it carries an accessible name", !!palette.getAttribute("aria-label"), "no aria-label");
  check("palette: it is keyboard reachable when disclosed", isTabbable(palette), "the palette select is not tabbable");
  var paletteValues = doc.querySelectorAll("#palette-select option").map(function (o) {
    return o.getAttribute("value");
  });
  eq("palette: it offers Neon Cyan and Wasteland Orange", paletteValues.join(","), "neon-cyan,wasteland-orange");
  // First run has no stored preference, so the control reflects the Neon Cyan default.
  eq("palette: it reflects the Neon Cyan default with nothing stored", palette.value, "neon-cyan");

  // The two axes persist INDEPENDENTLY: changing one never rewrites the other's key.
  mode.value = "light";
  dispatch(mode, new Event("change"));
  eq("theme: choosing Light persists it", localStorage.getItem("orch-hub-theme"), "light");
  eq("palette: a mode change leaves the palette key untouched", localStorage.getItem("orch-hub-palette"), null);
  palette.value = "wasteland-orange";
  dispatch(palette, new Event("change"));
  eq("palette: choosing Wasteland Orange persists it", localStorage.getItem("orch-hub-palette"), "wasteland-orange");
  eq("palette: a palette change leaves the mode key untouched", localStorage.getItem("orch-hub-theme"), "light");
  // System is itself a persistable mode choice (its live OS-follow is browser-only).
  mode.value = "system";
  dispatch(mode, new Event("change"));
  eq("theme: choosing System persists it", localStorage.getItem("orch-hub-theme"), "system");

  // Escape closes the panel and returns focus to the toggle.
  dispatch(panel, new Event("keydown", { key: "Escape" }));
  check("theme: Escape closes the panel", panel.hidden === true, "panel still open after Escape");
  activeIs("theme: Escape returns focus to the toggle", toggle);

  // The control is not part of the repainted #view, so a navigation leaves it in
  // place (a keyboard user's appearance choice is never torn out by routing).
  await navigate("#/repos");
  check(
    "theme: the control survives a navigation",
    doc.getElementById("appearance-toggle") === toggle &&
      doc.getElementById("theme-select") === mode &&
      doc.getElementById("palette-select") === palette,
    "the appearance control was replaced by a route repaint"
  );

  // Restore defaults so later scenarios start from a known appearance state.
  mode.value = "dark";
  dispatch(mode, new Event("change"));
  palette.value = "neon-cyan";
  dispatch(palette, new Event("change"));
}

async function scenarioTaskDetail() {
  await navigate("#/tasks/" + REPO + "/" + TASK);
  var heading = pageHeading();
  eq("task: the detail names the task", heading && heading.textContent.trim(), TASK);
  activeIs("task: focus lands on the task heading", heading);

  var textarea = doc.querySelector(".answer-input");
  check("task: the answer box has an accessible name", !!(textarea && textarea.getAttribute("aria-label")), "no aria-label");
  var buttons = doc.querySelectorAll(".answer-form button[data-mode]");
  eq("task: both answer modes are buttons", buttons.length, 2);
  check("task: answer buttons are keyboard-reachable", buttons.every(isTabbable), "a mode button is not tabbable");

  // Empty reply: refused locally, and focus is sent to the field to fix.
  pressEnter(buttons[1]);
  await flush();
  var status = doc.querySelector(".answer-status");
  eq("task: an empty reply is refused inline", status.textContent, "Enter a reply first.");
  eq("task: the refusal is announced", status.getAttribute("role"), "alert");
  activeIs("task: the refusal focuses the field to fix", textarea);

  type(textarea, "go left");
  pressEnter(buttons[1]);
  await flush();
  var call = lastCall("POST", "/api/runs/" + RUN + "/escalations/1/answer");
  check("task: the keyboard-sent answer posts", !!call, "no answer POST recorded");
  eq("task: it carries the button's explicit mode", call && call.body.mode, "answer");
  eq("task: it carries the typed reply", call && call.body.answer, "go left");
}

async function scenarioRunOptions() {
  installRoutes({ waiting: false }); // no live run → the Start control is offered
  await navigate("#/repos");
  await navigate("#/tasks/" + REPO + "/" + TASK);

  var open = doc.querySelector(".open-run-options");
  check("dialog: a Start control is offered", !!open, "no .open-run-options");
  var dialog = doc.querySelector(".run-options");
  eq("dialog: it is named by its own heading", dialog.getAttribute("aria-labelledby"), "run-options-title");
  check("dialog: that heading exists", !!doc.getElementById("run-options-title"), "no #run-options-title");

  pressEnter(open);
  eq("dialog: activating Start opens it", dialog.open, true);
  check("dialog: focus moves inside on open", dialog.contains(doc.activeElement), describe(doc.activeElement));

  pressEscape();
  eq("dialog: Escape closes it", dialog.open, false);
  activeIs("dialog: Escape restores focus to the opener", open);

  pressEnter(open);
  var cancel = dialog.querySelector(".cancel-run-options");
  pressEnter(cancel);
  eq("dialog: Cancel closes it", dialog.open, false);
  activeIs("dialog: Cancel restores focus to the opener", open);

  pressEnter(open);
  var devModel = doc.getElementById("opt-dev-model");
  check("dialog: every field has a label", dialogFieldsAreLabelled(dialog), "an option field has no label[for]");
  type(devModel, "opus");
  var planGate = dialog.querySelector(".opt-plan-gate");
  planGate.focus();
  planGate.checked = true;
  dispatch(planGate, new Event("change"));
  var launch = dialog.querySelector('button[type="submit"]');
  pressEnter(launch);
  await flush();
  var call = lastCall("POST", "/api/runs");
  check("dialog: a keyboard launch posts the run", !!call, "no run POST recorded");
  eq(
    "dialog: it carries the fixed backend plus only changed fields",
    call && JSON.stringify(call.body.extra_args),
    JSON.stringify(["--backend", "cc-codex", "--dev-model", "opus", "--plan-gate"])
  );
  eq("dialog: a successful launch closes it", dialog.open, false);

  // The no-<dialog>.showModal fallback branch: app.js must still open it, focus
  // into it, close on Escape, and hand focus back without the native API.
  await navigate("#/tasks/" + REPO + "/" + TASK);
  var plainOpen = doc.querySelector(".open-run-options");
  var plainDialog = doc.querySelector(".run-options");
  delete plainDialog.showModal;
  delete plainDialog.close;
  pressEnter(plainOpen);
  check(
    "dialog (no showModal): it still opens",
    plainDialog.hasAttribute("open"),
    "the open attribute was not set"
  );
  check(
    "dialog (no showModal): focus still moves inside",
    plainDialog.contains(doc.activeElement),
    describe(doc.activeElement)
  );
  pressEscape();
  check(
    "dialog (no showModal): Escape still closes it",
    !plainDialog.hasAttribute("open"),
    "still open"
  );
  activeIs("dialog (no showModal): focus still returns to the opener", plainOpen);
}

function dialogFieldsAreLabelled(dialog) {
  var fields = dialog.querySelectorAll("input, select");
  return fields.every(function (f) {
    if (f.getAttribute("type") === "checkbox") return !!f.closest("label");
    var id = f.id;
    return !!(id && dialog.querySelector('label[for="' + id + '"]'));
  });
}

async function scenarioIntakeReview() {
  await navigate("#/intakes/i1");
  var heading = pageHeading();
  eq("intake: the review names itself", heading && heading.textContent.trim(), "Task intake");
  activeIs("intake: focus lands on the review heading", heading);

  var feedback = doc.getElementById("intake-feedback");
  var label = doc.querySelector('label[for="intake-feedback"]');
  check("intake: the feedback box has a real label", !!(feedback && label), "no label[for=intake-feedback]");

  var confirm = doc.querySelector(".intake-confirm");
  var cancel = doc.querySelector(".intake-cancel");
  var send = doc.querySelector(".intake-revise-form button");
  check(
    "intake: confirm / cancel / send are all keyboard-reachable",
    !!confirm && !!cancel && !!send && [confirm, cancel, send].every(isTabbable),
    "a review control is not tabbable"
  );

  pressEnter(send); // submit with an empty box
  await flush();
  var status = doc.querySelector(".intake-revise-status");
  eq("intake: an empty revision is refused inline", status.textContent, "Describe the change first.");
  activeIs("intake: the refusal focuses the field to fix", feedback);

  pressEnter(cancel);
  await flush();
  check("intake: cancel posts", !!lastCall("POST", "/api/intakes/i1/cancel"), "no cancel POST");
  activeIs("intake: the repaint lands focus back on the heading", pageHeading());
}

async function scenarioHeadingOutline() {
  installRoutes({ waiting: true });
  await navigate("#/overview");
  checkOutline("overview");
  await navigate("#/repos");
  checkOutline("repos");
  await navigate("#/repos/" + REPO);
  checkOutline("repo detail");
  await navigate("#/tasks");
  checkOutline("tasks");
  await navigate("#/tasks/" + REPO + "/" + TASK);
  checkOutline("task detail");
  await navigate("#/runs");
  checkOutline("run history");
  await navigate("#/intakes/i1");
  checkOutline("intake review");

  await navigate("#/evaluations/" + REPO + "/" + TASK);
  checkOutline("task evaluation");

  await navigate("#/runs/" + RUN + "/logs");
  checkOutline("run log");
  var status = doc.getElementById("log-status");
  eq("log: the toolbar status is announced", status.getAttribute("role"), "status");
  eq("log: it reports what was loaded", status.textContent, "2 lines.");
  var out = doc.getElementById("log-output");
  eq("log: the scroll region is keyboard-reachable", out.getAttribute("tabindex"), "0");
  check("log: the log body carries no live-region role", !out.getAttribute("role"), out.getAttribute("role"));
  check("log: log lines are written as text", out.textContent.indexOf("dev session started") !== -1, out.textContent);

  // The task back-link's target is liveness-aware. This RUN is `completed`
  // (installRoutes), so the ended-run branch keeps the run-scoped task view —
  // which resolves the task active-then-archive, so an archived task stays
  // readable instead of 404-ing on the index-gated task detail.
  var endedBack = doc.querySelector(".back-link");
  eq(
    "log: an ended run's task link keeps the archived-safe run-scoped route",
    endedBack && endedBack.getAttribute("href"),
    "#/runs/" + RUN + "/task"
  );

  // A live (active/waiting) run with repo+task attribution instead links to the
  // full task detail, where the run controls, pending answers, and
  // session/context strips for the run in progress live. Use a distinct live run
  // id: a live log view follows via SSE, and this keeps that stream fetch off
  // RUN's path so the later stale-route "opens no stream" check stays scoped to
  // its own navigation. (No /logs/stream stub — the follow fails closed after
  // the back-link paints, which is fine; the link target is what this checks.)
  var LIVE_RUN = "run-live";
  route("GET", "/api/runs/" + LIVE_RUN, {
    run_id: LIVE_RUN,
    repo_name: REPO,
    task_id: TASK,
    state: "waiting",
    started_ts: "2026-07-20T08:00:00Z",
    pending_questions: 1,
  });
  await navigate("#/runs/" + LIVE_RUN + "/logs");
  var liveBack = doc.querySelector(".back-link");
  eq(
    "log: a live run's task link opens the full task detail",
    liveBack && liveBack.getAttribute("href"),
    "#/tasks/" + REPO + "/" + TASK
  );
}

// The desktop shell reads a per-view width class off #view (styles.css): the
// overview + flat card lists fill the workspace, detail/form views keep a
// bounded reading column, logs + run history get the wider column. The widths
// are CSS (nothing to measure without a layout engine), but the class app.js
// tags per route is the contract the stylesheet rests on, so pin it per
// representative route. The router sets it on entry, before the handler paints,
// so it holds even for a route whose payload errors.
async function scenarioViewLayout() {
  installRoutes({ waiting: true });
  route("GET", "/api/runs/" + RUN + "/task", {
    run_id: RUN,
    task_id: TASK,
    source: "active",
    present: true,
    content: "# Demo task\n",
  });
  var cases = [
    ["#/overview", "overview"],
    ["#/repos", "list"],
    ["#/tasks", "list"],
    ["#/repos/" + REPO, "read"],
    ["#/tasks/" + REPO + "/" + TASK, "read"],
    ["#/intakes/i1", "read"],
    ["#/runs", "wide"],
    ["#/runs/" + RUN + "/logs", "wide"],
    ["#/runs/" + RUN + "/task", "read"],
    ["#/evaluations/" + REPO + "/" + TASK, "read"],
  ];
  for (var i = 0; i < cases.length; i++) {
    await navigate(cases[i][0]);
    var v = doc.getElementById("view");
    check(
      "layout: " + cases[i][0] + " tags view--" + cases[i][1],
      v.classList.contains("view") &&
        v.classList.contains("view--" + cases[i][1]),
      v.className
    );
  }
}

// Task evaluation: the deterministic Layer 1 sidecar view. The keyboard smoke
// covers what served-asset greps can't — that the route renders itself, names
// itself, and lands focus like every other view; that the metrics actually paint
// from the report; that Generate posts the deterministic refresh; that the entry
// link is present when the subsystem is enabled; and that the disabled branch
// replaces the controls with a concise explanation.
async function scenarioTaskEval() {
  installRoutes({ waiting: false });
  var evalPath = "#/evaluations/" + REPO + "/" + TASK;
  var metricsPath = "/api/repos/" + REPO + "/evaluations/" + TASK + "/metrics";
  var semanticPath = "/api/repos/" + REPO + "/evaluations/" + TASK + "/semantic";

  await navigate(evalPath);
  var heading = pageHeading();
  eq("eval: the view names the task", heading && heading.textContent.trim(), TASK);
  activeIs("eval: focus lands on the heading", heading);
  eq("eval: document.title tracks the route", doc.title, TASK + " · orch-hub");
  // Task-scoped, so it keeps the Tasks tab current (like task detail).
  var current = doc.querySelectorAll(".tabbar a").filter(function (a) {
    return a.getAttribute("aria-current") === "page";
  });
  eq(
    "eval: the Tasks tab stays current",
    current.length && current[0].getAttribute("data-tab"),
    "/tasks"
  );

  // Enabled → the generate control shows and the disabled note is absent.
  check("eval: the Generate control is present", !!doc.querySelector(".eval-gen-btn"), "no .eval-gen-btn");
  check(
    "eval: it labels itself deterministic Layer 1 vs semantic Layer 2",
    (doc.querySelector(".eval-kind") || {}).textContent &&
      doc.querySelector(".eval-kind").textContent.indexOf("Layer 1") !== -1 &&
      doc.querySelector(".eval-kind").textContent.indexOf("Layer 2") !== -1,
    "no static-vs-semantic note"
  );

  // The report paints: five score axes (one non-assessable → n/a), the run
  // rollup, the protocol facts, and the pack-level evidence warning.
  eq("eval: all five score axes render", doc.querySelectorAll(".eval-score").length, 5);
  check(
    "eval: a non-assessable axis reads n/a",
    doc.querySelector(".score-list").textContent.indexOf("n/a") !== -1,
    "no n/a score rendered"
  );
  check(
    "eval: the run rollup renders related runs",
    doc.querySelector(".eval-runs").textContent.indexOf("related runs") !== -1,
    "no run rollup"
  );
  check(
    "eval: the failure-diagnostics flag renders",
    doc.querySelector(".eval-runs").textContent.indexOf("failed without diagnostic") !== -1,
    "no failure-diagnostics rollup"
  );
  check("eval: the protocol facts render", !!doc.querySelector(".eval-protocol"), "no .eval-protocol");
  check(
    "eval: pack evidence warnings render",
    !!doc.querySelector(".eval-evidence-warnings"),
    "no evidence warnings section"
  );

  // Local-inspection affordances: a Copy button and a declarative download link.
  check("eval: a copy affordance is present", !!doc.querySelector(".eval-copy"), "no .eval-copy");
  var dl = doc.querySelector(".eval-dl");
  check("eval: a download link is present", !!dl, "no .eval-dl");
  check(
    "eval: download is a declarative data: URI",
    dl && dl.getAttribute("href").indexOf("data:application/json") === 0,
    dl && dl.getAttribute("href")
  );
  check("eval: the evaluation-history picker is present", !!doc.getElementById("eval-pick"), "no #eval-pick");

  // Generate posts the deterministic refresh (writes hub-owned artifacts only).
  pressEnter(doc.querySelector(".eval-gen-btn"));
  await flush();
  check("eval: Generate posts the metrics refresh", !!lastCall("POST", metricsPath), "no metrics POST");

  // Layer 2 semantic controls: the trigger and its cost/read-only copy are present
  // on an enabled hub, and the default evaluation carries no semantic report yet,
  // so the distinct "deterministic metrics only" note stands in for the report.
  check("eval: the semantic trigger is present", !!doc.querySelector(".eval-sem-btn"), "no .eval-sem-btn");
  check(
    "eval: the semantic cost/read-only copy is present",
    !!doc.querySelector(".eval-cost") &&
      doc.querySelector(".eval-cost").textContent.indexOf("read-only") !== -1,
    "no cost/read-only copy"
  );
  check(
    "eval: a report-less evaluation shows the Layer 2 absent note",
    !!doc.querySelector(".sem-absent"),
    "no semantic-absent note"
  );

  // Triggering shows the in-progress state (busy trigger + running note) while the
  // model turn is in flight, then posts to /semantic once.
  var releaseSem = defer("POST", semanticPath);
  pressEnter(doc.querySelector(".eval-sem-btn"));
  check(
    "eval: the semantic run disables its trigger while in flight",
    doc.querySelector(".eval-sem-btn").disabled === true,
    "trigger not disabled during the run"
  );
  check(
    "eval: the semantic run shows an in-progress note",
    (doc.querySelector(".eval-sem-status") || {}).textContent &&
      doc.querySelector(".eval-sem-status").textContent.indexOf("Running") !== -1,
    "no in-progress note"
  );
  await releaseSem();
  await flush();
  check("eval: the semantic run posts to /semantic", !!lastCall("POST", semanticPath), "no semantic POST");

  // A semantic-carrying evaluation paints the Layer 2 report: the model-judgment
  // block, its evaluator identity, findings ordered most-severe first, evidence
  // refs, a suggestion, the improvements, an n/a axis, and the Markdown download.
  route("GET", "/api/repos/" + REPO + "/evaluations/" + TASK + "/" + EVAL_ID, EVAL_DETAIL_SEMANTIC);
  // The list now reports this evaluation carries a semantic report with a known
  // evaluator, so the history picker surfaces the model/version for comparison.
  route("GET", "/api/repos/" + REPO + "/evaluations/" + TASK, {
    repo: REPO,
    task_id: TASK,
    enabled: true,
    target: { present: true, source: "active" },
    latest_evaluation_id: EVAL_ID,
    evaluations: [
      {
        evaluation_id: EVAL_ID,
        has_evidence: true,
        has_static_metrics: true,
        has_semantic_report: true,
        evaluator: { backend: "claude-cli", model: "claude-opus-4-8", schema_version: "1" },
      },
    ],
  });
  await navigate(evalPath + "/" + EVAL_ID);
  var sem = doc.querySelector(".sem-report");
  check("eval: the semantic report block renders", !!sem && sem.className.indexOf("sem-absent") === -1, "no .sem-report");
  check(
    "eval: the report names its evaluator model",
    sem && sem.textContent.indexOf("claude-opus-4-8") !== -1,
    "no evaluator identity"
  );
  var findings = doc.querySelectorAll(".sem-finding");
  eq("eval: all three findings render", findings.length, 3);
  check(
    "eval: findings are ordered most-severe first",
    findings.length && findings[0].className.indexOf("sev-high") !== -1,
    "first finding is not high severity: " + (findings.length && findings[0].className)
  );
  check(
    "eval: a finding cites its evidence refs",
    sem && sem.textContent.indexOf("session-log entry sid-b") !== -1,
    "no evidence refs rendered"
  );
  check("eval: a finding shows its suggestion", !!doc.querySelector(".sem-suggestion"), "no suggestion rendered");
  check("eval: suggested improvements render", !!doc.querySelector(".sem-improvements"), "no improvements list");
  check(
    "eval: a non-assessable semantic axis reads n/a",
    doc.querySelector(".sem-scores").textContent.indexOf("n/a") !== -1,
    "no n/a semantic score"
  );
  var mdLink = doc.querySelectorAll(".eval-dl").filter(function (a) {
    return (a.getAttribute("download") || "").indexOf(".md") !== -1;
  });
  check("eval: the Markdown report is a download artifact", mdLink.length > 0, "no report.md download");
  check(
    "eval: the report.md download is a text/markdown data: URI",
    mdLink.length && mdLink[0].getAttribute("href").indexOf("data:text/markdown") === 0,
    mdLink.length && mdLink[0].getAttribute("href")
  );
  // The history picker doubles as a run-comparison surface: a semantic entry names
  // its evaluator model (and schema version) so runs are comparable without
  // opening each one.
  var pickOpt = doc.querySelector("#eval-pick option");
  check(
    "eval: the history picker names the evaluator model for comparison",
    pickOpt && pickOpt.textContent.indexOf("claude-opus-4-8") !== -1,
    "picker option text: " + (pickOpt && pickOpt.textContent)
  );
  // Restore the default list + report-less detail so later scenarios (and the
  // doctor-gating branch below) see the default fixtures.
  route("GET", "/api/repos/" + REPO + "/evaluations/" + TASK, {
    repo: REPO,
    task_id: TASK,
    enabled: true,
    target: { present: true, source: "active" },
    latest_evaluation_id: EVAL_ID,
    evaluations: [
      {
        evaluation_id: EVAL_ID,
        has_evidence: true,
        has_static_metrics: true,
        has_semantic_report: false,
        evaluator: null,
      },
    ],
  });
  route("GET", "/api/repos/" + REPO + "/evaluations/" + TASK + "/" + EVAL_ID, {
    evaluation_id: EVAL_ID,
    evidence: EVAL_EVIDENCE,
    static_metrics: EVAL_REPORT,
    semantic_report: null,
    report_md: null,
  });

  // The entry point from task detail appears when the subsystem is enabled.
  await navigate("#/tasks/" + REPO + "/" + TASK);
  var entry = doc.querySelector(".eval-entry a");
  check("eval: task detail links to the evaluation view", !!entry, "no task-detail eval link");
  eq(
    "eval: the entry link targets the eval route",
    entry && entry.getAttribute("href"),
    evalPath
  );

  // Doctor gating: the subsystem is enabled but the Layer 2 backend is
  // unconfigured (available:false). The semantic trigger is disabled with an
  // explanation, while the free deterministic Layer 1 control stays usable — the
  // session-1 "502 on click" is pre-empted by the doctor probe.
  route("GET", "/api/evaluation/doctor", {
    enabled: true,
    semantic: {
      backend: "claude-cli",
      model: "claude-opus-4-8",
      schema_version: "1",
      available: false,
      detail: "claude executable not found on PATH",
    },
  });
  await navigate(evalPath);
  check(
    "eval: an unconfigured backend disables the semantic trigger",
    (doc.querySelector(".eval-sem-btn") || {}).disabled === true,
    "the semantic trigger was not disabled for an unavailable backend"
  );
  check(
    "eval: the disabled semantic control explains why",
    !!doc.querySelector(".eval-sem-unavailable") &&
      doc.querySelector(".eval-sem-unavailable").textContent.indexOf("not found") !== -1,
    "no unavailable explanation"
  );
  check(
    "eval: Layer 1 stays usable when Layer 2 is unavailable",
    !!doc.querySelector(".eval-gen-btn") && doc.querySelector(".eval-gen-btn").disabled !== true,
    "the Layer 1 Generate control was blocked by an unavailable Layer 2"
  );
  // Restore an available backend for later scenarios.
  route("GET", "/api/evaluation/doctor", {
    enabled: true,
    semantic: {
      backend: "claude-cli",
      model: "claude-opus-4-8",
      schema_version: "1",
      available: true,
      detail: "claude resolved at /usr/local/bin/claude",
    },
  });

  // Disabled branch: availability 200 {enabled:false} → the controls are
  // replaced by a concise explanation, and no generate control is offered.
  route("GET", "/api/evaluation", { enabled: false });
  await navigate(evalPath);
  check(
    "eval: disabled mode drops the Generate control",
    doc.querySelector(".eval-gen-btn") === null,
    "the Generate control survived disabled mode"
  );
  check(
    "eval: disabled mode explains itself",
    doc.getElementById("view").textContent.indexOf("disabled") !== -1,
    "no disabled explanation"
  );
  // Disabled task detail hides the entry link entirely.
  await navigate("#/tasks/" + REPO + "/" + TASK);
  check(
    "eval: task detail hides the link when disabled",
    doc.querySelector(".eval-entry") === null,
    "the eval entry link showed on a disabled hub"
  );
  route("GET", "/api/evaluation", { enabled: true }); // restore for later scenarios
}

// Every route handler awaits a payload, so a slow route can resolve long after
// the user has navigated on. Such a stale completion must not touch the view it
// no longer owns: not by moving focus (the user is already operating the newer
// page, and being thrown back to its heading is exactly the unpredictable focus
// the task forbids), not by painting its error over it, not by painting its own
// content over it, and not by starting a stream for it. The mirror case is here
// too: re-entering a handler without a navigation is not stale and must paint.
async function scenarioStaleRoute() {
  installRoutes({ waiting: false });

  // A stale success. renderOverview's own guard skips the paint, but the router
  // invocation behind it used to land on the view regardless.
  var releaseOverview = defer("GET", "/api/repos");
  await navigate("#/overview"); // parks: loadOverview awaits the held-open source
  check(
    "stale route: the overview route is parked mid-load",
    doc.getElementById("view").textContent.indexOf("Loading overview") !== -1,
    doc.getElementById("view").textContent.slice(0, 60)
  );

  await navigate("#/tasks");
  eq("stale route: the newer route rendered", pageHeading().textContent.trim(), "Tasks");
  var filter = doc.getElementById("f-repo");
  filter.focus(); // the user has tabbed in and is operating the new page

  await releaseOverview();
  activeIs("stale route: a stale load leaves focus alone", filter);
  eq("stale route: the newer view survives", pageHeading().textContent.trim(), "Tasks");
  eq("stale route: document.title still names it", doc.title, "Tasks · orch-hub");

  // A stale failure: an unknown run 404s, and that error view must not overwrite
  // the route the user moved to.
  var releaseGhost = defer("GET", "/api/runs/ghost");
  await navigate("#/runs/ghost/logs"); // parks awaiting the 404
  await navigate("#/tasks");
  var filterAgain = doc.getElementById("f-repo");
  filterAgain.focus();

  await releaseGhost();
  eq(
    "stale route: a stale failure paints no error over the newer view",
    pageHeading().textContent.trim(),
    "Tasks"
  );
  activeIs("stale route: a stale failure leaves focus alone", filterAgain);

  // A stale handler's OWN content paint. Every routed handler writes #view after
  // its payload resolves; without a guard the repo list lands on top of whatever
  // route the user actually navigated to, leaving the URL, tab bar and
  // document.title naming a page whose content is no longer on screen.
  var releaseRepos = defer("GET", "/api/repos");
  await navigate("#/repos"); // parks: renderRepos awaits the held-open repo list
  await navigate("#/tasks");
  var taskFilter = doc.getElementById("f-repo");
  taskFilter.focus();

  await releaseRepos();
  eq(
    "stale route: a stale handler paints no content over the newer view",
    pageHeading().textContent.trim(),
    "Tasks"
  );
  check(
    "stale route: the newer view's own controls survive",
    doc.getElementById("f-repo") === taskFilter,
    "the tasks filters were replaced"
  );
  eq("stale route: document.title still names the newer view", doc.title, "Tasks · orch-hub");
  var stillCurrent = doc.querySelectorAll(".tabbar a").filter(function (a) {
    return a.getAttribute("aria-current") === "page";
  });
  eq(
    "stale route: the active tab still matches the URL",
    stillCurrent.length && stillCurrent[0].getAttribute("data-tab"),
    "/tasks"
  );
  activeIs("stale route: a stale content paint leaves focus alone", taskFilter);

  // The mirror case: re-entering a handler WITHOUT a navigation is not stale, so
  // it must still repaint. The log view's Reload is the button-driven one.
  await navigate("#/runs/" + RUN + "/logs");
  route("GET", "/api/runs/" + RUN + "/logs", {
    present: true,
    truncated: false,
    lines: ["08:31:00 reloaded"],
  });
  pressEnter(doc.getElementById("log-reload"));
  await flush();
  // Null-safe: a re-entry wrongly treated as stale skips its paint and strands
  // the view on the loading placeholder, which has no log output at all.
  var reloaded = doc.getElementById("log-output");
  check(
    "stale route: a re-render with no navigation still paints",
    !!reloaded && reloaded.textContent.indexOf("reloaded") !== -1,
    reloaded ? reloaded.textContent : "the view never painted its log output"
  );

  // rerender() is the other non-router path into a handler (intake controls, the
  // intake poll's settle). Its landing work must go stale with the render it
  // wraps — otherwise the skipped paint still rips focus to the newer page.
  installRoutes({ waiting: false });
  await navigate("#/intakes/i1");
  var releaseIntake = defer("GET", "/api/intakes/i1/history");
  pressEnter(doc.querySelector(".intake-cancel")); // POST, then rerender()
  await flush(); // the re-render parks on the held-open history read
  await navigate("#/tasks");
  var lastFilter = doc.getElementById("f-repo");
  lastFilter.focus();

  await releaseIntake();
  eq(
    "stale route: a stale re-render paints nothing over the newer view",
    pageHeading().textContent.trim(),
    "Tasks"
  );
  activeIs("stale route: a stale re-render leaves focus alone", lastFilter);

  // The delayed-start case: an intake control re-enters its render only AFTER
  // its own POST resolves. Guarding at that re-entry is too late — by then the
  // navigation has already bumped the generation, so the late re-entry adopts
  // the NEWER route's generation as its own and paints anyway. The guard has to
  // be captured when the button is pressed.
  installRoutes({ waiting: false });
  await navigate("#/intakes/i1");
  var releaseCancel = defer("POST", "/api/intakes/i1/cancel");
  pressEnter(doc.querySelector(".intake-cancel")); // parks on the POST itself
  await flush();
  await navigate("#/tasks");
  var actionFilter = doc.getElementById("f-repo");
  actionFilter.focus();
  var beforeRelease = CALLS.length;

  await releaseCancel();
  eq(
    "stale route: a stale intake action paints nothing over the newer view",
    pageHeading().textContent.trim(),
    "Tasks"
  );
  eq(
    "stale route: document.title survives a stale intake action",
    doc.title,
    "Tasks · orch-hub"
  );
  activeIs("stale route: a stale intake action leaves focus alone", actionFilter);
  check(
    "stale route: a stale intake action never re-enters its render",
    CALLS.slice(beforeRelease).every(function (c) {
      return c.path !== "/api/intakes/i1/history";
    }),
    "the superseded action re-read the intake and repainted the loading view"
  );

  // The same for the confirm handoff, which steals the URL rather than the view:
  // a late success would route the user to the created task's detail, off the
  // page they navigated to.
  route("POST", "/api/intakes/i1/confirm", {
    intake_id: "i1",
    repo_name: REPO,
    status: "confirmed",
    created_task_id: TASK,
  });
  await navigate("#/intakes/i1");
  var releaseConfirm = defer("POST", "/api/intakes/i1/confirm");
  pressEnter(doc.querySelector(".intake-confirm"));
  await flush();
  await navigate("#/tasks");

  await releaseConfirm();
  eq("stale route: a stale confirm hands off nowhere", location.hash, "#/tasks");
  eq(
    "stale route: a stale confirm leaves the newer view alone",
    pageHeading().textContent.trim(),
    "Tasks"
  );

  // Starting an intake routes to the new review view on success, and its POST
  // runs the readiness checks (git / aii-2 subprocesses), so it can easily
  // outlive the repo page it was submitted from.
  route("POST", "/api/intakes", { intake_id: "i1" });
  await navigate("#/repos/" + REPO);
  type(doc.getElementById("intake-request"), "Add a thing");
  var releaseStart = defer("POST", "/api/intakes");
  pressEnter(doc.querySelector(".intake-form button"));
  await flush();
  await navigate("#/tasks");

  await releaseStart();
  eq("stale route: a stale intake start routes nowhere", location.hash, "#/tasks");
  eq(
    "stale route: a stale intake start leaves the newer view alone",
    pageHeading().textContent.trim(),
    "Tasks"
  );

  // A stale log view costs more than a paint: router() aborts the active stream
  // on its way out, so a handler that ran on regardless would open a fresh SSE
  // stream after that teardown — a reader nothing cancels, feeding a <pre> that
  // is no longer in the document.
  route("GET", "/api/runs/" + RUN, {
    run_id: RUN,
    repo_name: REPO,
    task_id: TASK,
    state: "active", // live → this render would start following
    started_ts: "2026-07-20T08:00:00Z",
  });
  var releaseRun = defer("GET", "/api/runs/" + RUN);
  await navigate("#/runs/" + RUN + "/logs"); // parks awaiting the run record
  await navigate("#/tasks");

  await releaseRun();
  eq(
    "stale route: a stale log view paints nothing over the newer view",
    pageHeading().textContent.trim(),
    "Tasks"
  );
  check(
    "stale route: a stale log view opens no stream",
    lastCall("GET", "/api/runs/" + RUN + "/logs/stream") === null,
    "an SSE stream was opened for a view the user had already left"
  );
}

/* --------------------------------------------------------------- run them */

(async function main() {
  try {
    await scenarioLogin();
    await scenarioNavigation();
    await scenarioAppearance();
    await scenarioTaskDetail();
    await scenarioRunOptions();
    await scenarioIntakeReview();
    await scenarioHeadingOutline();
    await scenarioViewLayout();
    await scenarioTaskEval();
    await scenarioStaleRoute();
  } catch (err) {
    FAILED++;
    print("not ok - harness crashed: " + err + "\n" + (err && err.stack));
  }
  print("SUMMARY " + PASSED + "/" + (PASSED + FAILED));
})();
