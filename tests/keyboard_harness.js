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
  // from the selected <option>, or the first one — the browser behaviour the
  // launch dialog relies on (e.g. the pre-selected Custom dev-agent option).
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

// `payload` is normally a fixed object. A FUNCTION payload is called with the
// full request url (query included) and the parsed body, and its return value is
// the response — the only way to stub an endpoint whose answer depends on the
// query, i.e. a paged route serving a different window per `?cursor=`.
function route(method, path, payload) {
  ROUTES[method + " " + path] = payload;
}

// Stub a refusal: the route answers `status` with `payload` as its JSON body, so
// api() raises an ApiError carrying that body — the only way to drive a handler's
// structured-409 path (a raced repo-admission conflict, a launch refusal).
function routeError(method, path, status, payload) {
  ROUTES[method + " " + path] = { __status: status, __body: payload };
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
  // `path` keeps the query string (the launch dialog encodes backend/profile
  // there); `bare` is the query-stripped route key the ROUTES table matches on.
  CALLS.push({ method: method, path: bare, url: String(path), body: body, init: init });
  var payload = ROUTES[method + " " + bare];
  if (typeof payload === "function") payload = payload(String(path), body);
  var refusal = payload && payload.__status ? payload : null;
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
      : refusal
      ? {
          ok: false,
          status: refusal.__status,
          statusText: "Conflict",
          json: function () {
            return Promise.resolve(refusal.__body);
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

// Fire every currently-scheduled timer once (a browser would fire them on
// schedule), then flush so the awaited work in each callback settles. Used to
// drive the chained-setTimeout polls (intake, semantic job). Snapshot first: a
// fired callback may reschedule the next tick, and that reschedule must NOT fire
// within the same drain (else a chained poll would spin forever) — it lands in a
// new slot and only fires on the next runTimers(). Null out fired slots like
// clearTimeout so setTimeout's 1-based ids stay stable.
function runTimers() {
  var snapshot = [];
  for (var i = 0; i < TIMERS.length; i++)
    if (TIMERS[i]) snapshot.push({ i: i, fn: TIMERS[i].fn });
  snapshot.forEach(function (t) {
    TIMERS[t.i] = null;
  });
  snapshot.forEach(function (t) {
    t.fn();
  });
  return flush();
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

function countCalls(method, path) {
  var n = 0;
  for (var i = 0; i < CALLS.length; i++)
    if (CALLS[i].method === method && CALLS[i].path === path) n++;
  return n;
}

/* ------------------------------------------------------------ fixture data */

var REPO = "demo-repo";
var TASK = "2026-07-17-demo";
var RUN = "run-1";

// A representative --print-config response for the launch-dialog query. The
// harness fetch strips the query string, so every (backend, dev, review)
// variation resolves to this same object; the recorded request `url` carries the
// params the dialog actually asked for. `profiles` + `split_profiles: true` is a
// deployment that understands --dev-profile/--review-profile.
var ORCH_CONFIG = {
  schema_version: 1,
  config_revision: "cfg00000000aaaa1111",
  effective_revision: "eff00000000bbbb2222",
  profile: "default",
  profiles: { dev: "default", review: "default" },
  split_profiles: true,
  available_profiles: ["standard", "excellent"],
  backend: "cc-codex",
  dev: { agent: "claude", model: "claude-opus-4-8", effort: "max" },
  review: { agent: "codex", model: "gpt-5.5", effort: "xhigh" },
  max_sessions: 40,
  context_budget: 200000,
  codex_sandbox: "danger-full-access",
  sources: { backend: "config", "dev.model": "config" },
  task_id: null,
  once: false,
  plan_gate: false,
  control_dir: null,
};

// The same query answered by an orchestrator deployment that predates split role
// profiles: no `profiles` object, so the server derives both roles from the
// run-wide `profile` and reports the capability as absent.
var ORCH_CONFIG_LEGACY = Object.assign({}, ORCH_CONFIG, {
  profiles: { dev: "default", review: "default" },
  split_profiles: false,
});

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

// The display-only subscription-usage payload (SubscriptionUsage.to_dict shape):
// both providers available, Claude with a 5h+7d window pair, Codex with one
// primary window plus reset credits.
//
// A usage fixture has to be internally COHERENT now that the card asks whether a
// stored figure was observed inside the window its `resets_at` closes — a passed
// boundary means no window is open, and an observation predating the open window's
// start means its usage is unobserved (app.js: usageWindowPhase). A far-future
// boundary with a 2026 observation would describe a figure taken decades before
// the window opened, which correctly renders as "usage unknown". So these
// timestamps are anchored to the harness's own wall clock: observation shortly
// before now, boundary a few hours ahead, which stays "current" whenever the suite
// runs. The scenario overrides this route for the rolled-over, unobserved,
// unavailable, stale, and unknown paths.
var USAGE_CLOCK = Date.now();
var usageIso = function (offsetMinutes) {
  return new Date(USAGE_CLOCK + offsetMinutes * 60000).toISOString();
};
var USAGE = {
  providers: [
    {
      provider: "claude",
      available: true,
      plan: "max",
      windows: [
        { key: "five_hour", label: "5h", window_minutes: 300, used_percent: 42, remaining_percent: 58, resets_at: usageIso(120), used: null, limit: null },
        { key: "seven_day", label: "7d", window_minutes: 10080, used_percent: 12.5, remaining_percent: 87.5, resets_at: usageIso(5 * 1440), used: null, limit: null },
      ],
      credits: null,
      observed_ts: usageIso(-30),
      stale: false,
      detail: null,
    },
    {
      provider: "codex",
      available: true,
      plan: "prolite",
      windows: [
        { key: "primary", label: "5h", window_minutes: 300, used_percent: 73, remaining_percent: 27, resets_at: usageIso(120), used: null, limit: null },
      ],
      credits: { has_credits: true, unlimited: false, balance: "3" },
      observed_ts: usageIso(-45),
      stale: false,
      detail: null,
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

// The (repo, task) evaluation-list payload: one newest evaluation whose artifact
// flags the caller varies. The default is the deterministic Layer 1 fixture —
// evidence + static metrics, no semantic report. The evaluation sidecar view's
// history picker reads this shape.
function evalListPayload(over) {
  var summary = {
    evaluation_id: EVAL_ID,
    has_evidence: true,
    has_static_metrics: true,
    has_semantic_report: false,
    evaluator: null,
  };
  for (var k in over) summary[k] = over[k];
  return {
    repo: REPO,
    task_id: TASK,
    enabled: true,
    target: { present: true, source: "active" },
    latest_evaluation_id: EVAL_ID,
    evaluations: [summary],
  };
}

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
  route("GET", "/api/usage", USAGE);
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
  route("GET", "/api/repos/" + REPO + "/orchestrator-config", ORCH_CONFIG);
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
  route("GET", "/api/repos/" + REPO + "/evaluations/" + TASK, evalListPayload({}));
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
  // Async semantic-evaluation job: GET reads the current job (null by default =
  // never evaluated → the idle trigger), POST starts one (returns a transient
  // queued record). The eval scenario overrides these to drive the running,
  // completed, and failed states and the poll.
  route("GET", "/api/repos/" + REPO + "/evaluations/" + TASK + "/semantic/job", {
    job: null,
  });
  route("POST", "/api/repos/" + REPO + "/evaluations/" + TASK + "/semantic/job", {
    job: {
      job_id: "job-1",
      repo_name: REPO,
      task_id: TASK,
      status: "queued",
      backend: "claude-cli",
      model: "claude-opus-4-8",
      schema_version: "1",
      evaluation_id: null,
      error: null,
      created_ts: "2026-07-20T08:00:00Z",
      updated_ts: "2026-07-20T08:00:00Z",
    },
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
  // Opening kicks the orchestrator-config query; the panel paints when it settles.
  await flush();
  check(
    "dialog: opening queries the deployed orchestrator config",
    !!lastCall("GET", "/api/repos/" + REPO + "/orchestrator-config"),
    "no orchestrator-config GET recorded"
  );
  // The capability probe: the dialog opens with both roles Default, so its FIRST
  // query carries no role parameters — an older deployment that cannot take
  // --dev-profile/--review-profile can still answer it.
  eq(
    "dialog: the opening query sends no role profile parameters",
    lastConfigUrl(),
    "/api/repos/" + REPO + "/orchestrator-config"
  );
  var devMode = doc.getElementById("opt-dev-mode");
  var reviewMode = doc.getElementById("opt-review-mode");
  var backend = doc.getElementById("opt-backend");
  check("dialog: a DEV profile selector is offered", !!devMode, "no #opt-dev-mode");
  check("dialog: a REVIEW profile selector is offered", !!reviewMode, "no #opt-review-mode");
  check("dialog: no combined Mode selector remains", doc.getElementById("opt-mode") === null, "#opt-mode still rendered");
  check("dialog: a Backend selector is offered", !!backend, "no #opt-backend");
  eq(
    "dialog: DEV offers exactly the four launch modes",
    devMode.querySelectorAll("option").map(function (o) { return o.getAttribute("value"); }).join(","),
    "default,standard,excellent,custom"
  );
  eq(
    "dialog: REVIEW offers exactly the four launch modes",
    reviewMode.querySelectorAll("option").map(function (o) { return o.getAttribute("value"); }).join(","),
    "default,standard,excellent,custom"
  );
  eq("dialog: Backend stays shared (one select)", doc.querySelectorAll(".opt-backend").length, 1);
  check("dialog: every field has a label", dialogFieldsAreLabelled(dialog), "an option field has no label[for]");
  // Default is read-only: the resolved DEV value is static text from the query,
  // never a hardcoded model id.
  var devVal = dialog.querySelector(".role-val");
  check(
    "dialog: the resolved DEV value renders from the query",
    !!devVal && devVal.textContent.indexOf("claude-opus-4-8") !== -1,
    devVal ? describe(devVal) : "no .role-val"
  );

  // One role switching to a named profile queries THAT role's profile only, plus
  // an explicit backend (seeded from the resolved default) — the other role stays
  // inherited, never silently dragged along.
  devMode.value = "excellent";
  dispatch(devMode, new Event("change"));
  await flush();
  var cfgUrl = lastConfigUrl();
  check(
    "dialog: a named DEV profile queries --dev-profile alone",
    !!cfgUrl &&
      cfgUrl.indexOf("dev_profile=excellent") !== -1 &&
      cfgUrl.indexOf("review_profile") === -1 &&
      cfgUrl.indexOf("backend=cc-codex") !== -1,
    cfgUrl
  );
  // The second role selects independently; one coherent query carries both.
  reviewMode.value = "standard";
  dispatch(reviewMode, new Event("change"));
  await flush();
  cfgUrl = lastConfigUrl();
  check(
    "dialog: mixed roles query both role profiles at once",
    !!cfgUrl &&
      cfgUrl.indexOf("dev_profile=excellent") !== -1 &&
      cfgUrl.indexOf("review_profile=standard") !== -1,
    cfgUrl
  );
  var launchMixed = dialog.querySelector('button[type="submit"]');
  pressEnter(launchMixed);
  await flush();
  var callMixed = lastCall("POST", "/api/runs");
  eq(
    "dialog: a mixed launch posts independent role modes",
    callMixed && JSON.stringify(callMixed.body.launch),
    JSON.stringify({
      dev_mode: "excellent",
      review_mode: "standard",
      backend: "cc-codex",
    })
  );

  await navigate("#/tasks/" + REPO + "/" + TASK);
  open = doc.querySelector(".open-run-options");
  dialog = doc.querySelector(".run-options");
  pressEnter(open);
  await flush();

  pressEscape();
  eq("dialog: Escape closes it", dialog.open, false);
  activeIs("dialog: Escape restores focus to the opener", open);

  pressEnter(open);
  var cancel = dialog.querySelector(".cancel-run-options");
  pressEnter(cancel);
  eq("dialog: Cancel closes it", dialog.open, false);
  activeIs("dialog: Cancel restores focus to the opener", open);

  // A Default/Default launch with plan-gate posts the structured selection: both
  // role modes (the backend is inherited, so omitted) plus the orthogonal
  // plan-gate flag.
  await navigate("#/tasks/" + REPO + "/" + TASK);
  var openD = doc.querySelector(".open-run-options");
  var dialogD = doc.querySelector(".run-options");
  pressEnter(openD);
  await flush();
  var planGate = dialogD.querySelector(".opt-plan-gate");
  planGate.focus();
  planGate.checked = true;
  dispatch(planGate, new Event("change"));
  var launchD = dialogD.querySelector('button[type="submit"]');
  pressEnter(launchD);
  await flush();
  var callD = lastCall("POST", "/api/runs");
  check("dialog: a keyboard launch posts the run", !!callD, "no run POST recorded");
  check(
    "dialog: it posts a structured launch, not extra_args",
    !!callD && !!callD.body.launch && callD.body.extra_args === undefined,
    callD ? JSON.stringify(callD.body) : "no call"
  );
  eq(
    "dialog: Default/Default inherits the backend (role modes + plan-gate)",
    callD && JSON.stringify(callD.body.launch),
    JSON.stringify({
      dev_mode: "default",
      review_mode: "default",
      plan_gate: true,
    })
  );
  eq("dialog: a successful launch closes it", dialogD.open, false);

  // Custom is per role: only the Custom role becomes editable, the other keeps
  // its read-only preset value, and the query asks for the OTHER role's profile
  // while the custom one resolves inherited — which is what its fields seed from.
  await navigate("#/tasks/" + REPO + "/" + TASK);
  var openC = doc.querySelector(".open-run-options");
  var dialogC = doc.querySelector(".run-options");
  pressEnter(openC);
  await flush();
  var devModeC = doc.getElementById("opt-dev-mode");
  var reviewModeC = doc.getElementById("opt-review-mode");
  devModeC.value = "custom";
  dispatch(devModeC, new Event("change"));
  reviewModeC.value = "standard";
  dispatch(reviewModeC, new Event("change"));
  await flush();
  cfgUrl = lastConfigUrl();
  check(
    "dialog: a Custom role is queried as inherited under the other role's profile",
    !!cfgUrl &&
      cfgUrl.indexOf("dev_profile") === -1 &&
      cfgUrl.indexOf("review_profile=standard") !== -1,
    cfgUrl
  );
  var devModel = doc.getElementById("opt-dev-model");
  check("dialog: Custom DEV exposes an editable model", !!devModel, "no #opt-dev-model in Custom DEV");
  check(
    "dialog: a preset REVIEW stays read-only beside it",
    doc.getElementById("opt-review-model") === null &&
      dialogC.querySelectorAll(".role-val").length === 1,
    "the REVIEW role rendered editable fields"
  );
  eq(
    "dialog: the Custom DEV model seeds from the query",
    devModel.value,
    "claude-opus-4-8"
  );
  check("dialog: Custom controls are labelled", dialogFieldsAreLabelled(dialogC), "a Custom field has no label[for]");
  type(devModel, "opus");
  type(doc.getElementById("opt-dev-effort"), "high");
  // Editing is local state: changing the OTHER role re-queries and repaints the
  // panel, and the typed DEV values must survive that repaint.
  reviewModeC.value = "excellent";
  dispatch(reviewModeC, new Event("change"));
  await flush();
  eq(
    "dialog: typed Custom values survive the other role's re-query",
    doc.getElementById("opt-dev-model").value,
    "opus"
  );
  var launchC = dialogC.querySelector('button[type="submit"]');
  pressEnter(launchC);
  await flush();
  var callC = lastCall("POST", "/api/runs");
  eq(
    "dialog: Custom DEV posts its complete tuple beside a named REVIEW",
    callC && JSON.stringify(callC.body.launch),
    JSON.stringify({
      dev_mode: "custom",
      review_mode: "excellent",
      backend: "cc-codex",
      dev_agent: "claude",
      dev_model: "opus",
      dev_effort: "high",
    })
  );

  // An orchestrator deployment that predates split role profiles: it answers the
  // no-parameter probe, but reports no per-role capability. The dialog must not
  // attempt --dev-profile/--review-profile — it disables role selection AND
  // Launch with redeploy guidance, while the legacy config itself still renders.
  route("GET", "/api/repos/" + REPO + "/orchestrator-config", ORCH_CONFIG_LEGACY);
  await navigate("#/tasks/" + REPO + "/" + TASK);
  var openL = doc.querySelector(".open-run-options");
  var dialogL = doc.querySelector(".run-options");
  pressEnter(openL);
  await flush();
  check(
    "dialog (legacy): both role selectors are disabled",
    doc.getElementById("opt-dev-mode").disabled === true &&
      doc.getElementById("opt-review-mode").disabled === true,
    "a role selector stayed enabled"
  );
  check(
    "dialog (legacy): Launch is disabled",
    dialogL.querySelector('button[type="submit"]').disabled === true,
    "Launch stayed enabled on a deployment without role profiles"
  );
  check(
    "dialog (legacy): the note explains the redeploy",
    doc.querySelector(".profiles-note").textContent.indexOf("redeploy") !== -1,
    doc.querySelector(".profiles-note").textContent
  );
  check(
    "dialog (legacy): the orchestrator's own config still renders",
    dialogL.querySelector(".role-val").textContent.indexOf("claude-opus-4-8") !== -1,
    describe(dialogL.querySelector(".role-val"))
  );
  route("GET", "/api/repos/" + REPO + "/orchestrator-config", ORCH_CONFIG);

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

// The url (query string included) of the most recent orchestrator-config GET,
// so a scenario can assert the backend/profile the dialog actually queried.
function lastConfigUrl() {
  var target = "/api/repos/" + REPO + "/orchestrator-config";
  for (var i = CALLS.length - 1; i >= 0; i--)
    if (CALLS[i].method === "GET" && CALLS[i].path === target) return CALLS[i].url;
  return null;
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
  var listPath = "/api/repos/" + REPO + "/evaluations/" + TASK;
  var metricsPath = listPath + "/metrics";
  var jobPath = listPath + "/semantic/job";

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

  // Triggering starts an ASYNC job, not a blocking model call: POST .../semantic/job
  // returns a transient queued record at once (the long model turn runs off-thread
  // on the hub). The trigger goes busy with a running note synchronously; once the
  // queued job returns, the view re-renders to the running panel.
  var jobRecord = function (over) {
    var base = {
      job_id: "job-1", repo_name: REPO, task_id: TASK, status: "queued",
      backend: "claude-cli", model: "claude-opus-4-8", schema_version: "1",
      evaluation_id: null, error: null,
      created_ts: "2026-07-20T08:00:00Z", updated_ts: "2026-07-20T08:00:00Z",
      started_ts: null, ended_ts: null,
    };
    for (var k in over) base[k] = over[k];
    return base;
  };
  // The post-click re-render (and the poll) read a running job.
  route("GET", jobPath, {
    job: jobRecord({ status: "running", started_ts: "2026-07-20T08:00:01Z" }),
  });
  var releaseStart = defer("POST", jobPath);
  pressEnter(doc.querySelector(".eval-sem-btn"));
  check(
    "eval: the semantic trigger disables while the job starts",
    doc.querySelector(".eval-sem-btn").disabled === true,
    "trigger not disabled while starting the job"
  );
  check(
    "eval: starting the semantic job shows an in-progress note",
    (doc.querySelector(".eval-sem-status") || {}).textContent &&
      doc.querySelector(".eval-sem-status").textContent.indexOf("Running") !== -1,
    "no in-progress note"
  );
  await releaseStart();
  await flush();
  check("eval: the click posts the async semantic-job route", !!lastCall("POST", jobPath), "no job POST");
  // An in-flight job replaces the trigger entirely (no button) → a duplicate model
  // call cannot be started from the page, and the running panel names the model.
  check(
    "eval: an in-flight job replaces the trigger with a running panel",
    !!doc.querySelector(".eval-sem-running") && doc.querySelector(".eval-sem-btn") === null,
    "no running panel, or a duplicate trigger survived"
  );
  check(
    "eval: the running panel names the running model",
    doc.querySelector(".eval-sem-running").textContent.indexOf("claude-opus-4-8") !== -1,
    "running panel does not name the model"
  );

  // Poll resilience: a transient read failure (here a 404 from an unrouted GET)
  // must NOT be mistaken for a settled/gone job — the turn is still running on the
  // hub, so the running panel stays and the poll keeps trying.
  route("GET", jobPath, undefined); // the next poll read 404s
  await runTimers();
  check(
    "eval: a transient poll read failure keeps the running panel",
    !!doc.querySelector(".eval-sem-running") && doc.querySelector(".eval-sem-btn") === null,
    "a failed poll read wrongly settled the running state"
  );
  route("GET", jobPath, {
    job: jobRecord({ status: "running", started_ts: "2026-07-20T08:00:01Z" }),
  }); // restore the running job

  // Reload/navigation survival: the running state is read from the persisted job on
  // every load, so leaving the view and returning re-paints the running panel (not
  // the idle trigger). Leaving also tears down the poll through router().
  await navigate("#/tasks/" + REPO + "/" + TASK);
  await navigate(evalPath);
  check(
    "eval: a running job's state survives navigation",
    !!doc.querySelector(".eval-sem-running") && doc.querySelector(".eval-sem-btn") === null,
    "the running state did not survive navigation"
  );

  // A semantic-carrying evaluation paints the Layer 2 report: the model-judgment
  // block, its evaluator identity, findings ordered most-severe first, evidence
  // refs, a suggestion, the improvements, an n/a axis, and the Markdown download.
  route("GET", "/api/repos/" + REPO + "/evaluations/" + TASK + "/" + EVAL_ID, EVAL_DETAIL_SEMANTIC);
  // The list now reports this evaluation carries a semantic report with a known
  // evaluator, so the history picker surfaces the model/version for comparison.
  route(
    "GET",
    listPath,
    evalListPayload({
      has_semantic_report: true,
      evaluator: { backend: "claude-cli", model: "claude-opus-4-8", schema_version: "1" },
    })
  );
  // The job settles to completed, linking that fresh evaluation. Firing the
  // scheduled poll tick observes the terminal status and re-renders to select the
  // evaluation, so the Layer 2 report paints without a manual navigation.
  route("GET", jobPath, {
    job: jobRecord({
      status: "completed",
      evaluation_id: EVAL_ID,
      started_ts: "2026-07-20T08:00:01Z",
      ended_ts: "2026-07-20T08:02:00Z",
    }),
  });
  await runTimers();
  check(
    "eval: a completed job selects its fresh semantic evaluation",
    !!doc.querySelector(".sem-report") &&
      doc.querySelector(".sem-report").className.indexOf("sem-absent") === -1,
    "the completed job did not land on the semantic report"
  );
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
  // A FAILED job never silently returns to the idle view: the view shows a clear
  // failed state with the bounded backend diagnostic and a Retry that re-posts the
  // job. This state is read from the persisted job, so it survives a fresh load.
  route("GET", jobPath, {
    job: jobRecord({
      status: "failed",
      error: "semantic evaluation failed: claude backend exited 1",
      started_ts: "2026-07-20T08:00:01Z",
      ended_ts: "2026-07-20T08:00:30Z",
    }),
  });
  await navigate(evalPath);
  check(
    "eval: a failed job shows a failed state, not the idle trigger",
    !!doc.querySelector(".eval-sem-failed"),
    "no failed panel"
  );
  check(
    "eval: the failed state shows the bounded backend diagnostic",
    (doc.querySelector(".eval-sem-error") || {}).textContent &&
      doc.querySelector(".eval-sem-error").textContent.indexOf("exited 1") !== -1,
    "no bounded diagnostic"
  );
  var retryBtn = doc.querySelector(".eval-sem-failed .eval-sem-btn");
  check(
    "eval: the failed state offers a Retry control",
    !!retryBtn && retryBtn.textContent.indexOf("Retry") !== -1,
    "no Retry control: " + (retryBtn && retryBtn.textContent)
  );
  // Retry re-posts the async job route (a fresh attempt, allowed once terminal) and
  // re-renders to the running panel.
  route("GET", jobPath, {
    job: jobRecord({ status: "running", started_ts: "2026-07-20T08:01:00Z" }),
  });
  var postsBeforeRetry = countCalls("POST", jobPath);
  var releaseRetry = defer("POST", jobPath);
  pressEnter(retryBtn);
  await releaseRetry();
  await flush();
  check(
    "eval: Retry re-posts the async job route",
    countCalls("POST", jobPath) === postsBeforeRetry + 1,
    "Retry did not post a fresh job"
  );
  check(
    "eval: Retry lands on the running panel",
    !!doc.querySelector(".eval-sem-running"),
    "Retry did not enter the running state"
  );
  // Leave the running view so its poll is torn down before the restore below.
  await navigate("#/tasks/" + REPO + "/" + TASK);

  // Restore the default list + report-less detail + idle (no) job so the
  // doctor-gating branch below sees the default fixtures.
  route("GET", jobPath, { job: null });
  route("GET", listPath, evalListPayload({}));
  route("GET", "/api/repos/" + REPO + "/evaluations/" + TASK + "/" + EVAL_ID, {
    evaluation_id: EVAL_ID,
    evidence: EVAL_EVIDENCE,
    static_metrics: EVAL_REPORT,
    semantic_report: null,
    report_md: null,
  });

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
  // Active task detail has no evaluation entry or list request in either mode;
  // evaluation state belongs to the completed-task Run history surface.
  var listCallsBefore = countCalls("GET", listPath);
  await navigate("#/tasks/" + REPO + "/" + TASK);
  check(
    "eval: task detail has no evaluation entry",
    doc.querySelector(".eval-entry") === null,
    "an evaluation entry rendered on active task detail"
  );
  eq(
    "eval: task detail fetches no evaluation history",
    countCalls("GET", listPath),
    listCallsBefore
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

// Run history: the per-task Evaluation action rides the est-vs-actual rollup row
// with a compact three-state newest-evaluation light (grey / amber / green),
// all batched into the history response. The exact state remains in the hover
// title and screen-reader text. Activating the link navigates WITHOUT collapsing
// the group, while the group header still toggles. Also the eval-disabled branch:
// no action, no leftover row.
async function scenarioRunHistory() {
  installRoutes({ waiting: false });
  var histRun = {
    run_id: RUN,
    repo_name: REPO,
    repo_path: "/repos/" + REPO,
    task_id: TASK,
    state: "completed",
    started_ts: "2026-07-20T08:00:00Z",
    ended_ts: "2026-07-20T08:30:00Z",
    exit_code: 0,
    sessions: [
      {
        role: "dev",
        kind: "session",
        sid: "sess0001",
        turns: 3,
        context_tokens: 50000,
        start_hms: "08:00:00",
        open: "",
      },
    ],
    context_budget: 200000,
    session_est: { current: 1, total: 2 },
    // A split launch: the orchestrator's authoritative `profiles` object plus
    // the provenance that marks DEV as a Custom (cli-sourced) selection. Its
    // run-wide `profile` stays "default" and must NOT be reinterpreted.
    effective_config: {
      profile: "default",
      profiles: { dev: "default", review: "standard" },
      backend: "cc-codex",
      dev: { agent: "claude", model: "claude-opus-4-8", effort: "max" },
      review: { agent: "codex", model: "gpt-5.5", effort: "xhigh" },
      config_revision: "cfg00000000aaaa1111",
      effective_revision: "eff00000000bbbb2222",
      sources: { "dev.model": "cli", "review.model": "profile:standard" },
    },
  };
  // An older run whose snapshot predates split role profiles: only the run-wide
  // `profile`, which applied to both roles, so both display from it.
  var legacyRun = Object.assign({}, histRun, {
    run_id: RUN + "-legacy",
    effective_config: {
      profile: "standard",
      backend: "cursor",
      dev: { agent: null, model: "auto", effort: "high" },
      review: { agent: null, model: "auto", effort: "high" },
      config_revision: "cfg00000000cccc3333",
      effective_revision: "eff00000000dddd4444",
      sources: { "dev.model": "profile:standard" },
    },
  });
  var historyPayload = function (level, enabled) {
    return {
      runs: [histRun, legacyRun],
      page: 1,
      page_size: 50,
      total_runs: 2,
      total_pages: 1,
      has_prev: false,
      has_next: false,
      prev_cursor: null,
      next_cursor: null,
      evaluation_enabled: enabled !== false,
      evaluation_states:
        enabled === false
          ? []
          : [
              {
                repo_name: REPO,
                repo_path: "/repos/" + REPO,
                task_id: TASK,
                latest_evaluation_id:
                  level === "none" ? null : "20260730T010000Z",
                level: level,
              },
            ],
    };
  };
  route("GET", "/api/history/runs", historyPayload("l1"));
  var evalListUrl = "/api/repos/" + REPO + "/evaluations/" + TASK;
  var evalListCallsBeforeHistory = countCalls("GET", evalListUrl);

  await navigate("#/runs");
  checkOutline("run history");
  eq(
    "run history: batched state issues no per-task evaluation request",
    countCalls("GET", evalListUrl),
    evalListCallsBeforeHistory
  );

  // Both role selections are retained and summarized independently; a legacy
  // snapshot collapses to the one label its run-wide profile carried.
  // Each disclosure's OWN summary is its first one in document order; the nested
  // Provenance disclosure carries the other.
  var configs = doc.querySelectorAll(".run-config").map(function (d) {
    return d.querySelectorAll("summary")[0].textContent.trim();
  });
  eq("run history: each run keeps its launch-config disclosure", configs.length, 2);
  eq(
    "run history: a split run summarizes both roles (Custom read from provenance)",
    configs[0],
    "Launch config: dev custom · review standard · cc-codex"
  );
  eq(
    "run history: a legacy snapshot renders both roles from the run-wide profile",
    configs[1],
    "Launch config: standard · cursor"
  );
  var splitRows = doc.querySelectorAll(".run-config .kv dd").map(function (d) {
    return d.textContent;
  });
  check(
    "run history: each role row carries its own profile plus resolved values",
    splitRows.indexOf("custom · claude:claude-opus-4-8 @ max") !== -1 &&
      splitRows.indexOf("standard · codex:gpt-5.5 @ xhigh") !== -1,
    splitRows.join(" | ")
  );

  // Inline placement: the action lives inside the rollup (`.task-rollup`), on the
  // same `.roll-top` flex row as the summary metrics — not a standalone
  // `.tg-actions` row, and not nested in the collapse-toggle header.
  var evalLink = doc.querySelector(".task-rollup .tg-eval");
  check("run history: the eval action rides the rollup", !!evalLink, "no .task-rollup .tg-eval");
  check(
    "run history: the action shares the rollup top row with the metrics",
    !!doc.querySelector(".roll-top .roll-line") && !!doc.querySelector(".roll-top .tg-eval"),
    "metrics and action are not in the same .roll-top row"
  );
  check(
    "run history: the summary metrics render alongside the action",
    doc.querySelector(".roll-top .roll-line").textContent.indexOf("dev session") !== -1,
    doc.querySelector(".roll-top .roll-line").textContent
  );
  check(
    "run history: no standalone tg-actions row remains",
    doc.querySelector(".tg-actions") === null,
    "a .tg-actions row is still rendered"
  );
  check(
    "run history: the eval action is not inside the collapse header",
    evalLink.closest(".task-group-head") === null,
    "the eval link is nested in the toggle button"
  );
  eq(
    "run history: the eval action targets the evaluation view",
    evalLink.getAttribute("href"),
    "#/evaluations/" + REPO + "/" + TASK
  );
  var evalDot = evalLink.querySelector(".eval-state-dot");
  eq(
    "run history: L1 uses the amber indicator class",
    evalDot && evalDot.className,
    "eval-state-dot l1"
  );
  eq(
    "run history: the indicator itself carries no visible text",
    evalDot && evalDot.textContent,
    ""
  );
  eq(
    "run history: L1 remains available as hover text",
    evalLink.getAttribute("title"),
    "Evaluation status: L1"
  );
  check(
    "run history: L1 remains available to screen readers",
    evalLink.getAttribute("aria-label") === "Evaluation, status: L1",
    evalLink.getAttribute("aria-label")
  );

  // Re-entering the view consumes a new batch and advances the same group's
  // light without any per-task request.
  route("GET", "/api/history/runs", historyPayload("l1_l2"));
  await navigate("#/overview");
  await navigate("#/runs");
  evalLink = doc.querySelector(".task-rollup .tg-eval");
  evalDot = evalLink && evalLink.querySelector(".eval-state-dot");
  eq(
    "run history: both evaluation layers use the green indicator class",
    evalDot && evalDot.className,
    "eval-state-dot l1-l2"
  );
  eq(
    "run history: L1 + L2 remains available as hover text",
    evalLink && evalLink.getAttribute("title"),
    "Evaluation status: L1 + L2"
  );

  route("GET", "/api/history/runs", historyPayload("none"));
  await navigate("#/overview");
  await navigate("#/runs");
  evalLink = doc.querySelector(".task-rollup .tg-eval");
  evalDot = evalLink && evalLink.querySelector(".eval-state-dot");
  eq(
    "run history: a task with no report uses the grey indicator class",
    evalDot && evalDot.className,
    "eval-state-dot none"
  );
  eq(
    "run history: not-evaluated remains available as hover text",
    evalLink && evalLink.getAttribute("title"),
    "Evaluation status: not evaluated"
  );
  eq(
    "run history: all three states still use no per-task evaluation request",
    countCalls("GET", evalListUrl),
    evalListCallsBeforeHistory
  );

  // Collapsed is the default for a freshly encountered group, and a collapsed
  // group keeps its rollup + inline action visible (only the card list hides,
  // via CSS).
  check(
    "run history: the group starts collapsed",
    doc.querySelector(".task-group").classList.contains("collapsed"),
    doc.querySelector(".task-group").className
  );
  eq(
    "run history: the collapsed header reports it",
    doc.querySelector(".task-group-head").getAttribute("aria-expanded"),
    "false"
  );
  check(
    "run history: the eval action stays visible when collapsed",
    !!doc.querySelector(".task-rollup .tg-eval"),
    "the eval action vanished on collapse"
  );

  // The header toggles the group open (collapse contract preserved in both
  // directions).
  pressEnter(doc.querySelector(".task-group-head"));
  await flush();
  check(
    "run history: the header expands the group",
    !doc.querySelector(".task-group").classList.contains("collapsed"),
    "the header no longer toggles"
  );
  eq(
    "run history: the expanded header reports it",
    doc.querySelector(".task-group-head").getAttribute("aria-expanded"),
    "true"
  );

  // Non-toggle contract: activating the Evaluation link must NOT change the
  // group's state (the delegated header toggle matches only `.task-group-head`).
  pressEnter(doc.querySelector(".task-rollup .tg-eval"));
  await flush();
  check(
    "run history: activating Evaluation does not collapse the group",
    !doc.querySelector(".task-group").classList.contains("collapsed"),
    "the group collapsed on an Evaluation click"
  );
  eq(
    "run history: the header stays expanded after an Evaluation click",
    doc.querySelector(".task-group-head").getAttribute("aria-expanded"),
    "true"
  );

  // Collapsing again returns it to the default state.
  pressEnter(doc.querySelector(".task-group-head"));
  await flush();
  check(
    "run history: the header collapses the group again",
    doc.querySelector(".task-group").classList.contains("collapsed"),
    "the header no longer toggles closed"
  );

  // Eval-disabled hub: the same batch says off, so no action renders and no
  // empty row is left behind — the rollup still paints its metrics.
  route("GET", "/api/history/runs", historyPayload("none", false));
  await navigate("#/overview");
  await navigate("#/runs");
  check(
    "run history: a disabled hub renders no eval action",
    doc.querySelector(".tg-eval") === null,
    "an eval action rendered on a disabled hub"
  );
  check(
    "run history: a disabled hub leaves no standalone action row",
    doc.querySelector(".tg-actions") === null,
    "a leftover action row rendered on a disabled hub"
  );
  check(
    "run history: the rollup still renders without the action",
    !!doc.querySelector(".task-rollup .roll-line"),
    "the rollup vanished when eval is disabled"
  );
}

// Run-history paging: every persisted run is reachable through Previous/Next
// over server windows, each step anchored on a cursor the server handed back
// (never a page number computed here), boundary controls disable themselves, the
// page-local scope of the filters/counts is stated rather than implied, task
// groups start collapsed, and an explicit expansion survives a page trip while a
// newly-encountered group on the next page still starts collapsed. Also the two
// no-cards states: an empty history and a window whose runs are gone.
async function scenarioRunHistoryPaging() {
  installRoutes({ waiting: false });
  var TASK2 = "2026-07-29-second";
  var mkRun = function (id, task) {
    return {
      run_id: id,
      repo_name: REPO,
      repo_path: "/repos/" + REPO,
      task_id: task,
      state: "completed",
      started_ts: "2026-07-20T08:00:00Z",
      ended_ts: "2026-07-20T08:30:00Z",
      exit_code: 0,
      sessions: [],
      context_budget: 200000,
      session_est: { current: 1, total: 2 },
    };
  };
  // Four runs over two windows of two, keyed by the cursors the server hands
  // back: an anchor run id plus the direction it is read in ("" = no cursor, i.e.
  // the newest). Going older starts a window at its anchor; going newer ends one
  // just above it — which is why stepping back to page 1 is `newer:run-b` rather
  // than page 1's own start. The FIRST task spans both windows (so an expansion
  // has a same-key group to survive onto), the second appears only on the older
  // one (so a first sighting mid-paging still starts collapsed).
  var WINDOWS = {
    "": { runs: [mkRun("run-d", TASK), mkRun("run-c", TASK)], page: 1 },
    "newer:run-b": { runs: [mkRun("run-d", TASK), mkRun("run-c", TASK)], page: 1 },
    "older:run-b": { runs: [mkRun("run-b", TASK), mkRun("run-a", TASK2)], page: 2 },
  };
  // The same history after a fifth, newer run is persisted mid-walk. Reverse
  // windows realign from the bottom, so `run-e` ends up alone in a ragged partial
  // window at the top and the traversal is one window longer than the four-run
  // grid: three, which is what `total_pages` counts (the grid's arithmetic would
  // still say two, and this window would then call itself page 3 of 2). Real
  // server payloads for that state, so the indicator is asserted against what the
  // pager is actually handed.
  var GROWN = {
    "older:run-b": {
      runs: [mkRun("run-b", TASK), mkRun("run-a", TASK2)],
      page: 3,
      prev_cursor: "newer:run-b",
      next_cursor: null,
    },
    "newer:run-b": {
      runs: [mkRun("run-d", TASK), mkRun("run-c", TASK)],
      page: 2,
      prev_cursor: "newer:run-d",
      next_cursor: "older:run-b",
    },
    "newer:run-d": {
      runs: [mkRun("run-e", TASK)],
      page: 1,
      prev_cursor: null,
      next_cursor: "older:run-d",
    },
  };
  var cursorOf = function (url) {
    var m = /[?&]cursor=([^&]*)/.exec(url);
    return m ? decodeURIComponent(m[1]) : ""; // "" = the client sent no cursor
  };
  var shrunk = false;
  var grown = false;
  route("GET", "/api/history/runs", function (url) {
    var cursor = cursorOf(url);
    // The race the vanished-window state exists for: the newest window still
    // advertised an older one, but by the time Next is pressed those run dirs
    // are pruned, so the anchored window comes back empty — with Previous still
    // anchored on the last window that does exist.
    if (shrunk && cursor === "older:run-b")
      return {
        runs: [],
        page: 2,
        page_size: 2,
        total_runs: 2,
        total_pages: 1,
        has_prev: true,
        has_next: false,
        prev_cursor: "newer:run-b",
        next_cursor: null,
      };
    if (grown && GROWN[cursor])
      return Object.assign({}, GROWN[cursor], {
        page_size: 2,
        total_runs: 5,
        total_pages: 3, // windows of this walk, not runs/page_size slots
        has_prev: !!GROWN[cursor].prev_cursor,
        has_next: !!GROWN[cursor].next_cursor,
        evaluation_enabled: false,
        evaluation_states: [],
      });
    var window = WINDOWS[cursor] || { runs: [], page: 3 };
    return {
      runs: window.runs,
      page: window.page,
      page_size: 2,
      total_runs: 4,
      total_pages: 2,
      has_prev: window.page > 1,
      has_next: window.page < 2,
      prev_cursor: window.page > 1 ? "newer:run-b" : null,
      next_cursor: window.page < 2 ? "older:run-b" : null,
      evaluation_enabled: false,
      evaluation_states: [],
    };
  });

  await navigate("#/runs");
  checkOutline("run history paging");
  eq(
    "run history: entering the view asks for the newest window, unanchored",
    cursorOf(lastCall("GET", "/api/history/runs").url),
    ""
  );
  eq(
    "run history: the page indicator names the position and the whole-history total",
    doc.querySelector(".pager-status").textContent.trim(),
    "Page 1 of 2 · 4 runs total"
  );
  check(
    "run history: Previous is unavailable on the first page",
    doc.getElementById("rp-prev").disabled === true,
    "Previous is enabled on page 1"
  );
  check(
    "run history: Next is available while pages remain",
    doc.getElementById("rp-next").disabled === false,
    "Next is disabled on page 1 of 2"
  );
  eq(
    "run history: the pager controls name themselves for screen readers",
    doc.getElementById("rp-prev").textContent.replace(/\s+/g, " ").trim(),
    "← Previous page"
  );
  // The filters ran over this page only, and both the note and the count say so
  // rather than reading as a search across all of history.
  check(
    "run history: the filter scope is stated",
    doc.querySelector(".filters-scope").textContent.indexOf("this page") !== -1,
    doc.querySelector(".filters-scope").textContent
  );
  eq(
    "run history: the count is scoped to the loaded page",
    doc.getElementById("run-count").textContent.trim(),
    "1 group · 2 runs on this page"
  );
  eq("run history: page 1 renders its two runs", doc.querySelectorAll(".card.run").length, 2);

  // Every group starts collapsed; expanding one is an explicit choice.
  eq(
    "run history: page 1 groups start collapsed",
    doc.querySelectorAll(".task-group.collapsed").length,
    1
  );
  pressEnter(doc.querySelector(".task-group-head"));
  await flush();
  check(
    "run history: the group expands on request",
    !doc.querySelector(".task-group").classList.contains("collapsed"),
    "the group did not expand"
  );

  // Next: a real refetch of the next server window, at the anchor the response
  // carried for it — the client echoes a cursor back, it never computes one.
  pressEnter(doc.getElementById("rp-next"));
  await flush();
  eq(
    "run history: Next requests the window the server anchored for it",
    cursorOf(lastCall("GET", "/api/history/runs").url),
    "older:run-b"
  );
  eq(
    "run history: page 2 renders the rest of the history",
    doc.querySelectorAll(".card.run").map(function (c) {
      return c.querySelector(".run-id").textContent;
    }).join(","),
    "run-b,run-a"
  );
  eq(
    "run history: the indicator follows the page",
    doc.querySelector(".pager-status").textContent.trim(),
    "Page 2 of 2 · 4 runs total"
  );
  check(
    "run history: Next is unavailable on the last page",
    doc.getElementById("rp-next").disabled === true,
    "Next is enabled on the last page"
  );
  check(
    "run history: Previous is available off the first page",
    doc.getElementById("rp-prev").disabled === false,
    "Previous is disabled on page 2"
  );
  var groups = doc.querySelectorAll(".task-group");
  eq("run history: page 2 shows both of its task groups", groups.length, 2);
  check(
    "run history: an expanded group stays expanded across a page trip",
    !groups[0].classList.contains("collapsed"),
    "the carried-over group re-collapsed on page 2"
  );
  check(
    "run history: a group first seen on the new page starts collapsed",
    groups[1].classList.contains("collapsed"),
    "a newly encountered group rendered expanded"
  );

  // Previous walks back to the window it came from, again at the server's anchor
  // — a newer-direction cursor, so the window it selects cannot include the run
  // it was handed however history mutated in between.
  pressEnter(doc.getElementById("rp-prev"));
  await flush();
  eq(
    "run history: Previous requests the preceding window's anchor",
    cursorOf(lastCall("GET", "/api/history/runs").url),
    "newer:run-b"
  );
  check(
    "run history: Previous lands back on the first page",
    doc.getElementById("rp-prev").disabled === true,
    "page 1 still offers Previous"
  );

  // A window whose runs are gone (pruned between two requests): explicit, and
  // Previous carries the server's anchor for the last window that does exist
  // instead of dead-ending.
  shrunk = true;
  pressEnter(doc.getElementById("rp-next"));
  await flush();
  check(
    "run history: a vanished page says so instead of showing an empty list",
    doc.getElementById("view").textContent.indexOf("No runs on page 2") !== -1,
    doc.getElementById("view").textContent.slice(0, 200)
  );
  check(
    "run history: the vanished page keeps Next unavailable",
    doc.getElementById("rp-next").disabled === true,
    "Next is offered past the end of history"
  );
  // A window with no runs is a position past everything that exists, so the pager
  // drops the "Page N of M" pairing there rather than rendering a page number past
  // its own count; the state's own line carries the position and the real count.
  eq(
    "run history: a vanished page's pager states the total, not a page of it",
    doc.querySelector(".pager-status").textContent.trim(),
    "2 runs total"
  );
  check(
    "run history: the vanished page names its position and the real page count",
    doc.getElementById("view").textContent.indexOf(
      "No runs on page 2. History holds 2 runs on 1 page."
    ) !== -1,
    doc.getElementById("view").textContent.slice(0, 200)
  );
  pressEnter(doc.getElementById("rp-prev"));
  await flush();
  eq(
    "run history: Previous from a vanished page lands on the last real window",
    cursorOf(lastCall("GET", "/api/history/runs").url),
    "newer:run-b"
  );

  // A run persisted mid-walk (the insert race), now walked at the rendered
  // boundary: the traversal has a window the page grid does not, and the indicator
  // must stay a true "Page N of M" through every step of it rather than reporting
  // a position past its own count.
  shrunk = false;
  grown = true;
  pressEnter(doc.getElementById("rp-next"));
  await flush();
  eq(
    "run history: a window off the page grid renders a coherent indicator",
    doc.querySelector(".pager-status").textContent.trim(),
    "Page 3 of 3 · 5 runs total"
  );
  check(
    "run history: the ragged last window still offers Previous",
    doc.getElementById("rp-prev").disabled === false,
    "Previous is disabled on a window that has one"
  );
  check(
    "run history: the ragged last window offers no Next",
    doc.getElementById("rp-next").disabled === true,
    "Next is offered past the last run"
  );
  pressEnter(doc.getElementById("rp-prev"));
  await flush();
  eq(
    "run history: stepping back through the grown walk keeps the count",
    doc.querySelector(".pager-status").textContent.trim(),
    "Page 2 of 3 · 5 runs total"
  );
  pressEnter(doc.getElementById("rp-prev"));
  await flush();
  eq(
    "run history: the grown walk's top window is its first page",
    doc.querySelector(".pager-status").textContent.trim(),
    "Page 1 of 3 · 5 runs total"
  );
  eq(
    "run history: the run persisted mid-walk is the ragged top window",
    doc.querySelectorAll(".card.run").map(function (c) {
      return c.querySelector(".run-id").textContent;
    }).join(","),
    "run-e"
  );
  check(
    "run history: the grown walk's first page offers no Previous",
    doc.getElementById("rp-prev").disabled === true,
    "Previous is offered above the newest run"
  );
  grown = false;

  // Empty history stays its own state: no pager, no filters, just the note.
  route("GET", "/api/history/runs", {
    runs: [],
    page: 1,
    page_size: 50,
    total_runs: 0,
    total_pages: 0,
    has_prev: false,
    has_next: false,
    prev_cursor: null,
    next_cursor: null,
    evaluation_enabled: false,
    evaluation_states: [],
  });
  await navigate("#/runs");
  check(
    "run history: an empty history reads as empty, not as a missing page",
    doc.getElementById("view").textContent.indexOf("No runs recorded yet.") !== -1,
    doc.getElementById("view").textContent.slice(0, 200)
  );
  check(
    "run history: an empty history renders no pager",
    doc.querySelector(".pager") === null,
    "a pager rendered over an empty history"
  );

  installRoutes({ waiting: false }); // restore defaults for any later scenario
}

// Selecting a run opens its TASK, not its log tail. The run surfaces (Overview
// current activity, Run history) share one destination rule: a live run with
// repo+task attribution goes to the full task detail, an ended run to the
// run-scoped task view (its task may already be archived), and a run with no
// task id keeps its logs. Both task destinations carry "View logs" onward, so
// the log tail stays one step away rather than being the only reachable thing.
// The panels deliberately left alone — Needs attention and Recent outcomes —
// are pinned here too, since this task changed their neighbours.
async function scenarioRunNavigation() {
  // The Overview's lower panels carry no distinguishing class of their own, so
  // find one by its heading rather than by paint order.
  var overviewPanel = function (title) {
    var sections = doc.querySelectorAll(".overview-panels .section");
    for (var i = 0; i < sections.length; i++) {
      var h2 = sections[i].querySelectorAll("h2")[0];
      if (h2 && h2.textContent.trim() === title) return sections[i];
    }
    return null;
  };
  var hrefs = function (root) {
    return (root ? root.querySelectorAll("a.card-link") : []).map(function (a) {
      return a.getAttribute("href");
    });
  };

  var ENDED_RUN = "run-ended";
  var ORPHAN_RUN = "run-orphan";
  var liveRun = {
    run_id: RUN,
    repo_name: REPO,
    repo_path: "/repos/" + REPO,
    task_id: TASK,
    state: "waiting",
    started_ts: "2026-07-20T08:00:00Z",
    pending_questions: 1,
    stop_requested: false,
  };
  // A live run the hub could not attribute to a task: there is no task to open,
  // so its card must still reach the logs.
  var orphanLiveRun = {
    run_id: ORPHAN_RUN,
    repo_name: REPO,
    repo_path: "/repos/" + REPO,
    task_id: "",
    state: "active",
    started_ts: "2026-07-20T09:00:00Z",
    stop_requested: false,
  };
  var endedRun = {
    run_id: ENDED_RUN,
    repo_name: REPO,
    repo_path: "/repos/" + REPO,
    task_id: TASK,
    state: "completed",
    started_ts: "2026-07-19T08:00:00Z",
    ended_ts: "2026-07-19T08:30:00Z",
    exit_code: 0,
  };

  installRoutes({ waiting: true });
  route("GET", "/api/runs", { runs: [liveRun, orphanLiveRun] });
  route("GET", "/api/history/runs", {
    runs: [liveRun, orphanLiveRun, endedRun],
    page: 1,
    page_size: 50,
    total_runs: 3,
    total_pages: 1,
    has_prev: false,
    has_next: false,
    prev_cursor: null,
    next_cursor: null,
    evaluation_enabled: false,
    evaluation_states: [],
  });

  await navigate("#/overview");
  var activity = hrefs(overviewPanel("Current activity"));
  eq("run nav: current activity lists both live runs", activity.length, 2);
  eq(
    "run nav: an attributed live run opens its active task detail",
    activity[0],
    "#/tasks/" + REPO + "/" + TASK
  );
  eq(
    "run nav: an unattributed live run still opens its logs",
    activity[1],
    "#/runs/" + ORPHAN_RUN + "/logs"
  );
  // Recent outcomes is out of scope and stays a deep link to the log tail.
  eq(
    "run nav: recent outcomes still open the log tail",
    hrefs(overviewPanel("Recent outcomes"))[0],
    "#/runs/" + ENDED_RUN + "/logs"
  );
  // The waiting attention row already opened the task detail (its answer form
  // lives there) and is unchanged by this task.
  var attn = doc.querySelector(".overview-attention .attn-waiting a.card-link");
  eq(
    "run nav: the waiting attention row still opens the task detail",
    attn && attn.getAttribute("href"),
    "#/tasks/" + REPO + "/" + TASK
  );

  await navigate("#/runs");
  var history = hrefs(doc.querySelector("#view"));
  eq("run nav: run history renders every run card", history.length, 3);
  eq(
    "run nav: a live run in history opens its active task detail",
    history[0],
    "#/tasks/" + REPO + "/" + TASK
  );
  eq(
    "run nav: an ended run opens the archived-safe run-scoped task view",
    history[1],
    "#/runs/" + ENDED_RUN + "/task"
  );
  eq(
    "run nav: a history run with no task id keeps its logs",
    history[2],
    "#/runs/" + ORPHAN_RUN + "/logs"
  );

  // Both destinations keep the log tail one link away, pointed at the run the
  // reader selected.
  await navigate("#/tasks/" + REPO + "/" + TASK);
  var detailLogs = doc.querySelectorAll("#view .section a.text-link").filter(
    function (a) {
      return a.textContent.indexOf("View logs") !== -1;
    }
  );
  eq(
    "run nav: task detail still links onward to the live run's logs",
    detailLogs.length && detailLogs[0].getAttribute("href"),
    "#/runs/" + RUN + "/logs"
  );

  route("GET", "/api/runs/" + ENDED_RUN + "/task", {
    run_id: ENDED_RUN,
    task_id: TASK,
    source: "archive",
    present: true,
    content: "# Demo task\n",
  });
  await navigate("#/runs/" + ENDED_RUN + "/task");
  var runTaskLogs = doc.querySelectorAll("#view a.text-link").filter(function (a) {
    return a.textContent.indexOf("View logs") !== -1;
  });
  eq(
    "run nav: the run-scoped task view still links onward to that run's logs",
    runTaskLogs.length && runTaskLogs[0].getAttribute("href"),
    "#/runs/" + ENDED_RUN + "/logs"
  );
  check(
    "run nav: the run-scoped view names the archived source",
    doc.getElementById("view").textContent.indexOf("archive") !== -1,
    doc.getElementById("view").textContent.slice(0, 200)
  );

  installRoutes({ waiting: false }); // restore defaults for any later scenario
}

// Live-run readiness display: a repo whose ONLY raw readiness failure is a dirty
// working tree reads as READY *for display* while an active/waiting run is in
// flight (the tree is dirty because the run is editing it), and reverts to NOT
// READY the moment the run ends, gains another failure, or the run overlay is
// unavailable. The softening is presentation-only: the intake gate on repo
// detail still pre-explains the raw block, proving no launch/intake gate relaxed.
async function scenarioLiveRunReadiness() {
  installRoutes({ waiting: false });

  // The exact readiness a live orchestrator run produces. Serialized
  // readiness.checks holds ONLY the failures, so a single git_clean entry means
  // the dirty tree is the sole failure.
  var DIRTY_ONLY = {
    ready: false,
    checks: [{ name: "git_clean", message: "working tree has uncommitted changes" }],
  };
  // A dirty tree PLUS a structural failure: never display-ready, run or not.
  var DIRTY_PLUS_STRUCTURAL = {
    ready: false,
    checks: [
      { name: "git_clean", message: "working tree has uncommitted changes" },
      { name: "aii_status", message: "aii-2 status failed: drift detected" },
    ],
  };
  var reposWith = function (readiness) {
    return {
      repos: [
        {
          name: REPO,
          path: "/tmp/" + REPO,
          readiness: readiness,
          housekeeping_pending: false,
          commit: null,
        },
      ],
    };
  };
  var runsIn = function (state) {
    return state === null
      ? { runs: [] }
      : {
          runs: [
            {
              run_id: RUN,
              repo_name: REPO,
              task_id: TASK,
              state: state,
              started_ts: "2026-07-20T08:00:00Z",
              pending_questions: state === "waiting" ? 1 : 0,
              stop_requested: false,
            },
          ],
        };
  };
  var textOf = function (sel) {
    var el = doc.querySelector(sel);
    return el ? el.textContent.trim() : null;
  };
  var listBadge = function () {
    return textOf("#view .card.repo .badges .badge");
  };
  var listReasons = function () {
    return doc.querySelector("#view .card.repo .reasons");
  };
  // The overview "repos ready" tile value, found by its label so it can't be
  // confused with the other metric tiles.
  var reposReadyMetric = function () {
    var tiles = doc.querySelectorAll("#view .metric");
    for (var i = 0; i < tiles.length; i++) {
      var label = tiles[i].querySelector(".metric-label");
      if (label && label.textContent.trim() === "repos ready")
        return (tiles[i].querySelector(".metric-value") || {}).textContent;
    }
    return null;
  };

  // ---- 1) active run + dirty-tree-only failure → display READY ----
  route("GET", "/api/repos", reposWith(DIRTY_ONLY));
  route("GET", "/api/runs", runsIn("active"));
  await navigate("#/repos");
  eq("readiness: an active-run dirty repo shows READY on the list", listBadge(), "ready");
  check(
    "readiness: the softened list card drops its failure reasons",
    listReasons() === null,
    "failure reasons still rendered on a display-ready card"
  );
  check(
    "readiness: the run signal stays an independent dimension (active dot present)",
    !!doc.querySelector("#view .card.repo .run-dot.active"),
    "the active run-dot vanished from the softened card"
  );

  await navigate("#/repos/" + REPO);
  eq("readiness: repo detail shows READY for a live dirty repo", textOf(".detail-head .badge"), "ready");
  // While a run holds the repo, the intake section reports the ADMISSION claim,
  // never the readiness state — softened or raw. A live run is what makes the
  // tree dirty, so naming `git_clean` here would turn readiness into a proxy for
  // "claimed" (the raw-readiness branch is proven alive in case 4 below, where no
  // run holds the repo).
  check(
    "readiness: a live run's intake block is admission, not a dirty tree",
    (textOf(".section.intake") || "").indexOf("A run is live on this repo") !== -1 &&
      (textOf(".section.intake") || "").indexOf("git_clean") === -1,
    "the intake section explained the live run as a readiness failure: " + textOf(".section.intake")
  );

  await navigate("#/overview");
  eq("readiness: the overview counts the softened repo as ready", reposReadyMetric(), "1/1");
  check(
    "readiness: no NOT READY attention row for the softened repo",
    doc.querySelector(".attn-not-ready") === null,
    "a not-ready attention row rendered for a display-ready repo"
  );

  // ---- 2) waiting run: still READY, but the waiting-answer item remains ----
  route("GET", "/api/runs", runsIn("waiting"));
  route("GET", "/api/escalations", {
    escalations: [
      { run_id: RUN, repo_name: REPO, task_id: TASK, question: { seq: 1, kind: "escalation", message: "Which way?" } },
    ],
  });
  await navigate("#/repos");
  eq("readiness: a waiting-run dirty repo also shows READY", listBadge(), "ready");
  check(
    "readiness: the waiting card carries the waiting run-dot",
    !!doc.querySelector("#view .card.repo .run-dot.waiting"),
    "no waiting run-dot on the waiting card"
  );
  await navigate("#/overview");
  eq("readiness: the waiting repo still counts as ready", reposReadyMetric(), "1/1");
  check(
    "readiness: no NOT READY attention row while waiting-softened",
    doc.querySelector(".attn-not-ready") === null,
    "a not-ready row rendered for a waiting display-ready repo"
  );
  check(
    "readiness: the waiting-for-answer attention item is still shown",
    !!doc.querySelector(".attn-waiting"),
    "the waiting attention item was dropped by the readiness softening"
  );

  // ---- 3) an additional structural failure → NOT READY even with a live run ----
  route("GET", "/api/repos", reposWith(DIRTY_PLUS_STRUCTURAL));
  route("GET", "/api/runs", runsIn("active"));
  route("GET", "/api/escalations", { escalations: [] });
  await navigate("#/repos");
  eq("readiness: a live repo with a structural failure stays NOT READY", listBadge(), "not ready");
  check("readiness: the structural-failure card keeps its reasons", !!listReasons(), "reasons dropped on a genuinely not-ready repo");
  await navigate("#/overview");
  eq("readiness: the overview does not count a structural-failure repo", reposReadyMetric(), "0/1");
  check(
    "readiness: a structural-failure repo still appears in readiness attention",
    !!doc.querySelector(".attn-not-ready"),
    "the structural-failure repo was wrongly dropped from attention"
  );

  // ---- 4) ended/idle: dirty tree, no live run (tasks present) → NOT READY ----
  // /api/tasks still lists an active task for this repo, so a NOT READY result
  // proves active-task presence is not an exemption — only a live run is.
  route("GET", "/api/repos", reposWith(DIRTY_ONLY));
  route("GET", "/api/runs", runsIn(null));
  await navigate("#/repos");
  eq("readiness: a dirty repo with no live run shows NOT READY", listBadge(), "not ready");
  check(
    "readiness: active tasks alone do not soften readiness (reasons shown)",
    !!listReasons(),
    "an idle dirty repo with active tasks was wrongly softened"
  );
  // With no run holding the repo, the intake gate falls through to raw readiness
  // and pre-explains the dirty tree — the branch admission preempts in case 1.
  await navigate("#/repos/" + REPO);
  check(
    "readiness: an unclaimed dirty repo's intake block is raw readiness",
    (textOf(".section.intake") || "").indexOf("not ready, so a task intake") !== -1,
    "the intake section dropped its raw-readiness block: " + textOf(".section.intake")
  );
  await navigate("#/overview");
  eq("readiness: the overview counts an ended/idle dirty repo as not ready", reposReadyMetric(), "0/1");
  check(
    "readiness: an ended/idle dirty repo appears in readiness attention",
    !!doc.querySelector(".attn-not-ready"),
    "no not-ready attention row for an idle dirty repo"
  );

  // ---- 5) run overlay unavailable → fall back to raw readiness (NOT READY) ----
  route("GET", "/api/repos", reposWith(DIRTY_ONLY));
  delete ROUTES["GET /api/runs"]; // fetch → 404: the run overlay degrades to none
  await navigate("#/repos");
  eq("readiness: unavailable run data falls back to raw NOT READY", listBadge(), "not ready");
  await navigate("#/overview");
  eq("readiness: the overview falls back to raw readiness when runs are unavailable", reposReadyMetric(), "0/1");

  installRoutes({ waiting: false }); // restore defaults for any later scenario
}

// Subscription usage: the display-only Overview capacity section over /api/usage.
// Drives the available, high-usage, numeric-limit, unknown-window, unavailable,
// stale, and source-failure paths — pinning percent-first rendering, explicit
// unavailable/stale (never a zero bar), provider independence, and the invariant
// that usage is a separate section that never affects readiness/attention.
async function scenarioSubscriptionUsage() {
  installRoutes({ waiting: false }); // default USAGE fixture: both providers available
  var cards = function () {
    return doc.querySelectorAll("#view .usage-card");
  };
  var textOf = function (sel) {
    var el = doc.querySelector(sel);
    return el ? el.textContent.trim() : null;
  };
  var reposReady = function () {
    var tiles = doc.querySelectorAll("#view .metric");
    for (var i = 0; i < tiles.length; i++) {
      var label = tiles[i].querySelector(".metric-label");
      if (label && label.textContent.trim() === "repos ready")
        return (tiles[i].querySelector(".metric-value") || {}).textContent;
    }
    return null;
  };

  // ---- 1) both providers available ----
  await navigate("#/overview");
  check(
    "usage: the Subscription usage section renders",
    !!doc.querySelector("#view .overview-usage"),
    "no .overview-usage section on the overview"
  );
  eq(
    "usage: the section names itself",
    textOf("#view .overview-usage h2"),
    "Subscription usage"
  );
  // Its OWN section: NOT nested inside the attention/activity/outcomes grid, so
  // capacity never lays out as an operational panel (descendant match → null).
  check(
    "usage: the section sits outside the operational panels grid",
    doc.querySelector(".overview-panels .overview-usage") === null,
    "the usage section is nested inside .overview-panels"
  );
  eq("usage: one card per provider", cards().length, 2);
  var claude = cards()[0];
  var codex = cards()[1];
  eq(
    "usage: the first card is Claude",
    claude.querySelector(".usage-provider").textContent,
    "Claude"
  );
  eq(
    "usage: the second card is Codex",
    codex.querySelector(".usage-provider").textContent,
    "Codex"
  );
  eq(
    "usage: the plan/tier is shown",
    claude.querySelector(".usage-plan").textContent,
    "max"
  );
  // Claude has a 5h + 7d window; Codex a single primary window.
  eq("usage: Claude shows both windows", claude.querySelectorAll(".usage-window").length, 2);
  eq("usage: Codex shows its one window", codex.querySelectorAll(".usage-window").length, 1);
  check(
    "usage: a window draws a used-capacity bar fill",
    !!claude.querySelector(".usage-bar-fill"),
    "no .usage-bar-fill in an available window"
  );
  check(
    "usage: the used percentage is shown as the readout",
    claude.textContent.indexOf("42% used") !== -1,
    claude.textContent
  );
  check(
    "usage: remaining capacity is shown",
    claude.textContent.indexOf("58% left") !== -1,
    claude.textContent
  );
  check(
    "usage: a forward reset time is shown",
    claude.textContent.indexOf("resets") !== -1,
    claude.textContent
  );
  check(
    "usage: Codex reset-credit availability is shown (no identifier)",
    codex.textContent.indexOf("reset credits available (3)") !== -1,
    codex.textContent
  );
  check(
    "usage: an observation-freshness line is shown",
    !!claude.querySelector(".usage-fresh") &&
      claude.querySelector(".usage-fresh").textContent.indexOf("observed ") === 0,
    claude.querySelector(".usage-fresh") && claude.querySelector(".usage-fresh").textContent
  );
  // Display-only: the usage source does not touch the readiness metric or create
  // an attention row (the default repo is ready → attention is empty).
  eq("usage: it does not disturb the repos-ready metric", reposReady(), "1/1");
  eq("usage: it creates no Needs attention row", doc.querySelectorAll("#view .attn").length, 0);

  // ---- 1b) reset time is an absolute viewer-local instant, not a floored age ----
  // A relative "resets in 4d" floored away up to a day of lead time (a reset
  // nearly five days out read as merely four); the card now shows the exact
  // local wall-clock instant. A no-offset ISO value is a wall-clock time in the
  // viewer's own zone, so `getHours()/getDate()/getMonth()` return its literal
  // components regardless of the harness timezone — deterministic. The far-future
  // year keeps both boundaries non-past (the format carries no year) with the
  // observation inside the 5h window that closes at 09:06, and the unparseable
  // window pins the never-"NaN" fallback.
  route("GET", "/api/usage", {
    providers: [
      {
        provider: "claude",
        available: true,
        plan: "max",
        windows: [
          { key: "five_hour", label: "5h", window_minutes: 300, used_percent: 42, remaining_percent: 58, resets_at: "2099-07-29T09:06:00", used: null, limit: null },
          { key: "seven_day", label: "7d", window_minutes: 10080, used_percent: 12.5, remaining_percent: 87.5, resets_at: "2099-08-02T09:06:00", used: null, limit: null },
          { key: "primary", label: "raw", window_minutes: 300, used_percent: 5, remaining_percent: 95, resets_at: "not-a-date", used: null, limit: null },
        ],
        credits: null,
        observed_ts: "2099-07-29T08:00:00",
        stale: false,
        detail: null,
      },
      {
        provider: "codex",
        available: true,
        plan: "pro",
        windows: [
          { key: "primary", label: "5h", window_minutes: 300, used_percent: 73, remaining_percent: 27, resets_at: "2099-07-29T09:06:00", used: null, limit: null },
        ],
        credits: null,
        observed_ts: "2099-07-29T08:00:00",
        stale: false,
        detail: null,
      },
    ],
  });
  await navigate("#/repos"); // leave + return so the panel repaints from the new route
  await navigate("#/overview");
  var resetClaude = cards()[0];
  var resetCodex = cards()[1];
  check(
    "usage: a future reset renders as an absolute viewer-local time (resets HH:mm on D Mon)",
    resetClaude.textContent.indexOf("resets 09:06 on 29 Jul") !== -1,
    resetClaude.textContent
  );
  check(
    "usage: Codex uses the same absolute reset-time format as Claude",
    resetCodex.textContent.indexOf("resets 09:06 on 29 Jul") !== -1,
    resetCodex.textContent
  );
  check(
    "usage: no window renders a floored \"resets in\" duration anymore",
    doc.querySelector("#view .overview-usage").textContent.indexOf("resets in ") === -1,
    doc.querySelector("#view .overview-usage").textContent
  );
  check(
    "usage: an unparseable reset never renders \"NaN\"",
    resetClaude.textContent.indexOf("NaN") === -1,
    resetClaude.textContent
  );

  // ---- 1c) a passed boundary is NO ACTIVE WINDOW, not a current figure ----
  // The reported case: a 5h window whose reset instant went by while nothing
  // observed the new one (the statusLine sink only writes while an interactive
  // session renders). The card used to keep the full meter and degrade the meta
  // line to "reset due", presenting a figure from a closed window as live. Claude's
  // 5h window is anchored to the first call OF THAT WINDOW, so a passed boundary
  // means no window is open at all — the next call opens one — and the retained
  // figure must read as explicitly last-observed, with no fill and no derived
  // "% left" (an inferred "96% left" claims free capacity the hub cannot see).
  route("GET", "/api/usage", {
    providers: [
      {
        provider: "claude",
        available: true,
        plan: "max",
        windows: [
          { key: "five_hour", label: "5h", window_minutes: 300, used_percent: 4, remaining_percent: 96, resets_at: usageIso(-60), used: null, limit: null },
          { key: "seven_day", label: "7d", window_minutes: 10080, used_percent: 12.5, remaining_percent: 87.5, resets_at: usageIso(4 * 1440), used: null, limit: null },
        ],
        credits: null,
        observed_ts: usageIso(-180),
        stale: true,
        detail: null,
      },
    ],
  });
  await navigate("#/repos");
  await navigate("#/overview");
  var rolledCard = cards()[0];
  var rolled = rolledCard.querySelectorAll(".usage-window")[0];
  var stillOpen = rolledCard.querySelectorAll(".usage-window")[1];
  check(
    "usage: a window whose reset instant passed reads \"no active window\"",
    rolled.querySelector(".uw-readout").textContent === "no active window",
    rolled.querySelector(".uw-readout").textContent
  );
  check(
    "usage: the rolled-over window says the next window starts at the next call",
    rolled.textContent.indexOf("starts at next call") !== -1,
    rolled.textContent
  );
  check(
    "usage: the retained percentage is labelled last-observed, not current",
    rolled.textContent.indexOf("last observed 4% used") !== -1,
    rolled.textContent
  );
  check(
    "usage: a rolled-over window no longer reads \"reset due\"",
    rolledCard.textContent.indexOf("reset due") === -1,
    rolledCard.textContent
  );
  check(
    "usage: a rolled-over window draws no fill (not a 0% bar, not the old figure)",
    rolled.querySelector(".usage-bar.unknown") !== null &&
      rolled.querySelector(".usage-bar-fill") === null,
    rolled.innerHTML
  );
  check(
    "usage: a rolled-over window infers no remaining capacity (\"% left\")",
    rolled.textContent.indexOf("% left") === -1,
    rolled.textContent
  );
  check(
    "usage: the rolled-over state is marked on the window",
    rolled.className.indexOf("uw-rolled") !== -1,
    rolled.className
  );
  // Per-window, not per-card: the same card's 7d window is still open, so its
  // figures keep rendering as current.
  check(
    "usage: a sibling window that is still open keeps its live meter",
    stillOpen.textContent.indexOf("12.5% used") !== -1 &&
      stillOpen.textContent.indexOf("87.5% left") !== -1 &&
      !!stillOpen.querySelector(".usage-bar-fill"),
    stillOpen.textContent
  );

  // ---- 1d) boundary refreshed by a hub-driven turn, figures not re-observed ----
  // Claude Code 2.1.220 puts a percentage-less `rate_limit_event` on the stream the
  // hub already drives, so an ordinary intake/semantic turn can move the stored
  // window's boundary forward for free (orch_hub.usage: merge_window_boundaries)
  // while the figures stay as the sink last observed them. A window IS open then —
  // its boundary is ahead — but nothing has observed ITS usage: that reads as
  // "usage unknown" with the boundary shown and the old figure kept as
  // last-observed. Never a percentage attached to a window it did not come from.
  route("GET", "/api/usage", {
    providers: [
      {
        provider: "claude",
        available: true,
        plan: "max",
        windows: [
          { key: "five_hour", label: "5h", window_minutes: 300, used_percent: 4, remaining_percent: 96, resets_at: usageIso(240), used: null, limit: null },
        ],
        credits: null,
        observed_ts: usageIso(-180),
        stale: true,
        detail: null,
      },
    ],
  });
  await navigate("#/repos");
  await navigate("#/overview");
  var refreshed = doc.querySelector("#view .usage-window");
  check(
    "usage: an open window whose figure predates it reads \"usage unknown\"",
    refreshed.querySelector(".uw-readout").textContent === "usage unknown",
    refreshed.querySelector(".uw-readout").textContent
  );
  check(
    "usage: the refreshed boundary is still shown as a forward reset time",
    refreshed.textContent.indexOf("resets ") !== -1 &&
      refreshed.textContent.indexOf("starts at next call") === -1,
    refreshed.textContent
  );
  check(
    "usage: its stale figure is kept as last-observed, never as the window's own",
    refreshed.textContent.indexOf("last observed 4% used") !== -1 &&
      refreshed.textContent.indexOf("% left") === -1,
    refreshed.textContent
  );
  check(
    "usage: an unobserved open window draws no fill (not a 0% bar)",
    refreshed.querySelector(".usage-bar.unknown") !== null &&
      refreshed.querySelector(".usage-bar-fill") === null,
    refreshed.innerHTML
  );
  check(
    "usage: the unobserved state is marked on the window",
    refreshed.className.indexOf("uw-unobserved") !== -1,
    refreshed.className
  );

  // ---- 2) high usage flags amber (neutral→warn capacity idiom, not an alarm) ----
  route("GET", "/api/usage", {
    providers: [
      {
        provider: "claude",
        available: true,
        plan: "max",
        windows: [
          { key: "five_hour", label: "5h", window_minutes: 300, used_percent: 96, remaining_percent: 4, resets_at: usageIso(120), used: null, limit: null },
        ],
        credits: null,
        observed_ts: usageIso(-30),
        stale: false,
        detail: null,
      },
    ],
  });
  await navigate("#/repos"); // leave + return so the panel repaints from the new route
  await navigate("#/overview");
  check(
    "usage: a nearly-exhausted window flags its bar high",
    !!doc.querySelector("#view .usage-bar.high"),
    "no .usage-bar.high for a 96%-used window"
  );

  // ---- 3) percent-first: an exact count shows only with a real numeric limit ----
  route("GET", "/api/usage", {
    providers: [
      {
        provider: "codex",
        available: true,
        plan: "pro",
        windows: [
          // A provider-supplied numeric limit → exact count, never invented.
          { key: "primary", label: "5h", window_minutes: 300, used_percent: null, remaining_percent: null, resets_at: usageIso(120), used: 100, limit: 500 },
          // No count and no percentage → explicit unknown, NOT a 0% bar.
          { key: "secondary", label: "7d", window_minutes: 10080, used_percent: null, remaining_percent: null, resets_at: null, used: null, limit: null },
        ],
        credits: null,
        observed_ts: usageIso(-30),
        stale: false,
        detail: null,
      },
    ],
  });
  await navigate("#/repos");
  await navigate("#/overview");
  check(
    "usage: an exact numeric limit renders as a count",
    doc.querySelector("#view .usage-card").textContent.indexOf("100 / 500") !== -1,
    doc.querySelector("#view .usage-card").textContent
  );
  check(
    "usage: a missing figure reads unknown, never 0% used",
    doc.querySelector("#view .usage-card").textContent.indexOf("usage unknown") !== -1,
    "the unknown window did not render an explicit unknown readout"
  );
  var unknownBar = doc.querySelector("#view .usage-bar.unknown");
  check("usage: the unknown window uses the unknown track", !!unknownBar, "no .usage-bar.unknown");
  check(
    "usage: the unknown track carries no fill (not a 0% bar)",
    unknownBar && unknownBar.querySelector(".usage-bar-fill") === null,
    "an unknown window rendered a bar fill"
  );

  // ---- 4) one provider unavailable → explicit reason, the other still renders ----
  route("GET", "/api/usage", {
    providers: [
      { provider: "claude", available: false, plan: null, windows: [], credits: null, observed_ts: null, stale: false, detail: "no Claude usage observed yet" },
      USAGE.providers[1], // codex still available
    ],
  });
  await navigate("#/repos");
  await navigate("#/overview");
  var unavail = doc.querySelector("#view .usage-card.usage-unavailable");
  check("usage: an unavailable provider renders an unavailable card", !!unavail, "no .usage-unavailable card");
  check(
    "usage: the unavailable card states the provider's own reason",
    unavail && unavail.textContent.indexOf("no Claude usage observed yet") !== -1,
    unavail && unavail.textContent
  );
  check(
    "usage: an unavailable provider draws no capacity bar (not zero usage)",
    unavail && unavail.querySelector(".usage-bar") === null,
    "the unavailable card drew a usage bar"
  );
  check(
    "usage: the other provider still renders (independent providers)",
    doc.querySelectorAll("#view .usage-card").length === 2 &&
      doc.querySelectorAll("#view .usage-card.usage-unavailable").length === 1 &&
      !!doc.querySelectorAll("#view .usage-card")[1].querySelector(".usage-bar-fill"),
    "the available provider was suppressed by the unavailable one"
  );

  // ---- 5) stale → still shown, flagged last-observed ----
  // Stale is about the FIGURES' age, not the window: observed two hours ago, inside
  // a five-hour window that is still open. The figures therefore stay current for
  // that window (full meter) and carry the stale flag — the rolled-over path above
  // is the separate case where the window itself is gone.
  route("GET", "/api/usage", {
    providers: [
      {
        provider: "claude",
        available: true,
        plan: "max",
        windows: [
          { key: "five_hour", label: "5h", window_minutes: 300, used_percent: 42, remaining_percent: 58, resets_at: usageIso(120), used: null, limit: null },
        ],
        credits: null,
        observed_ts: usageIso(-120),
        stale: true,
        detail: null,
      },
    ],
  });
  await navigate("#/repos");
  await navigate("#/overview");
  var staleCard = doc.querySelector("#view .usage-card.usage-stale");
  check("usage: a stale observation marks its card", !!staleCard, "no .usage-card.usage-stale");
  check(
    "usage: a stale card still shows its figures (flagged, not hidden)",
    staleCard && !!staleCard.querySelector(".usage-bar-fill"),
    "the stale card dropped its usage bar"
  );
  check(
    "usage: a stale card carries a last-observed flag",
    staleCard &&
      !!staleCard.querySelector(".usage-flag") &&
      staleCard.querySelector(".usage-fresh").textContent.indexOf("last observed ") === 0,
    staleCard && staleCard.querySelector(".usage-fresh") && staleCard.querySelector(".usage-fresh").textContent
  );

  // ---- 6) source failure → only this section degrades; the rest stays up ----
  delete ROUTES["GET /api/usage"]; // fetch → 404 → overviewSource returns null
  await navigate("#/repos");
  await navigate("#/overview");
  check(
    "usage: a failed usage source degrades only this section",
    (textOf("#view .overview-usage") || "").indexOf("Subscription usage is unavailable.") !== -1,
    textOf("#view .overview-usage")
  );
  check(
    "usage: the other overview panels still load when usage fails",
    !!doc.querySelector("#view .overview-metrics") &&
      !!doc.querySelector("#view .overview-attention"),
    "a usage-source failure blanked the rest of the overview"
  );
  eq("usage: a usage failure does not disturb the repos-ready metric", reposReady(), "1/1");
  eq("usage: a usage failure creates no Needs attention row", doc.querySelectorAll("#view .attn").length, 0);

  installRoutes({ waiting: false }); // restore defaults for any later scenario
}

// Repo admission: a live run and a remote intake are mutually exclusive per repo
// (orch_hub.admission), in both orderings. The dashboard pre-reflects that claim
// in three places — the repo-detail intake entry, the task-detail run controls,
// and the intake review's write controls — and repaints into the same
// explanation when a start loses the race and comes back 409. These checks drive
// each direction, the recovered overlap, the terminal states that must NOT
// block, and the invariant that a conflict is presented as a claim on the repo,
// never as a readiness/attention/run-state change.
async function scenarioRepoAdmission() {
  installRoutes({ waiting: false }); // ready repo, no live run, no intake

  var textOf = function (sel) {
    var el = doc.querySelector(sel);
    return el ? el.textContent.trim() : null;
  };
  var LIVE = {
    runs: [
      {
        run_id: RUN,
        repo_name: REPO,
        task_id: TASK,
        state: "active",
        started_ts: "2026-07-20T08:00:00Z",
        pending_questions: 0,
        stop_requested: false,
      },
    ],
  };
  var intakeIn = function (status) {
    return {
      intakes: [
        {
          intake_id: "i1",
          repo_name: REPO,
          status: status,
          request: "Add a thing",
          updated_ts: "2026-07-20T08:00:00Z",
        },
      ],
    };
  };

  // ---- 1) a live run claims the repo → no intake entry, linked explanation ----
  route("GET", "/api/runs", LIVE);
  await navigate("#/repos/" + REPO);
  var runNote = doc.querySelector(".section.intake .admission-note");
  check("admission: a live run replaces the intake entry with an explanation", !!runNote, "no .admission-note on repo detail");
  check(
    "admission: no intake form is offered while a run holds the repo",
    doc.querySelector(".intake-form") === null,
    "the intake form survived a live run"
  );
  var runLink = runNote && runNote.querySelector("a.text-link");
  eq(
    "admission: the intake block links to the blocking run's task",
    runLink && runLink.getAttribute("href"),
    "#/tasks/" + REPO + "/" + TASK
  );
  check("admission: that link is keyboard-reachable", !!runLink && isTabbable(runLink), "the blocking-run link is not tabbable");
  // Presentation stays neutral: the repo is claimed, not unready, and the run
  // signal keeps its own meaning.
  eq("admission: the readiness badge is untouched by the conflict", textOf(".detail-head .badge"), "ready");
  await navigate("#/overview");
  eq("admission: a claimed repo creates no Needs attention row", doc.querySelectorAll("#view .attn").length, 0);
  eq("admission: the repos-ready metric is untouched by the conflict", textOf("#view .metric .metric-value"), "1/1");

  // ---- 2) an intake claims the repo → Start is not offered, links to it ----
  route("GET", "/api/runs", { runs: [] });
  route("GET", "/api/intakes", intakeIn("awaiting_review"));
  await navigate("#/tasks/" + REPO + "/" + TASK);
  check(
    "admission: an in-flight intake withholds Start",
    doc.querySelector(".open-run-options") === null,
    "Start was offered while an intake held the repo"
  );
  var intakeNote = doc.querySelector(".run-controls .admission-note");
  check("admission: run controls explain the blocking intake", !!intakeNote, "no .admission-note in the run controls");
  var intakeLink = intakeNote && intakeNote.querySelector("a.text-link");
  eq("admission: run controls link to the blocking intake", intakeLink && intakeLink.getAttribute("href"), "#/intakes/i1");
  check("admission: that link is keyboard-reachable", !!intakeLink && isTabbable(intakeLink), "the blocking-intake link is not tabbable");

  // Every non-terminal status claims the repo; every terminal one releases it.
  // (Statuses mirror orch_hub.intake — a resting draft awaiting review holds the
  // repo exactly like an in-flight turn, because confirming it writes.)
  var blocking = ["drafting", "awaiting_review", "revising", "confirming"];
  for (var i = 0; i < blocking.length; i++) {
    route("GET", "/api/intakes", intakeIn(blocking[i]));
    await navigate("#/tasks/" + REPO + "/" + TASK);
    check(
      "admission: a " + blocking[i] + " intake withholds Start",
      doc.querySelector(".open-run-options") === null,
      "Start was offered against a " + blocking[i] + " intake"
    );
  }
  var settled = ["confirmed", "cancelled", "failed", "error"];
  for (var j = 0; j < settled.length; j++) {
    route("GET", "/api/intakes", intakeIn(settled[j]));
    await navigate("#/tasks/" + REPO + "/" + TASK);
    check(
      "admission: a " + settled[j] + " intake does not block Start",
      !!doc.querySelector(".open-run-options"),
      "Start stayed hidden after the intake reached " + settled[j]
    );
  }

  // ---- 3) recovered overlap: the review keeps only Cancel ----
  // A live run alongside an awaiting_review intake is the pre-existing overlap
  // admission can refuse but not undo. Revise/confirm re-read or rewrite a tree
  // the run owns, so the backend fails both closed; Cancel writes nothing and is
  // the only way to clear the claim from this side.
  route("GET", "/api/runs", LIVE);
  route("GET", "/api/intakes", intakeIn("awaiting_review"));
  await navigate("#/intakes/i1");
  check(
    "admission: an overlapped draft offers no Confirm",
    doc.querySelector(".intake-confirm") === null,
    "Confirm survived a live run on the repo"
  );
  check(
    "admission: an overlapped draft offers no revision form",
    doc.querySelector(".intake-revise-form") === null,
    "the revise form survived a live run on the repo"
  );
  var overlapCancel = doc.querySelector(".intake-cancel");
  check("admission: Cancel remains available on an overlap", !!overlapCancel && isTabbable(overlapCancel), "Cancel was withdrawn or is not tabbable");
  var overlapLink = doc.querySelector(".section.intake .admission-note a.text-link");
  eq(
    "admission: the overlap explanation links to the blocking run's task",
    overlapLink && overlapLink.getAttribute("href"),
    "#/tasks/" + REPO + "/" + TASK
  );
  check(
    "admission: the draft itself still renders under an overlap",
    !!doc.querySelector(".task-content"),
    "the proposed task file vanished with the write controls"
  );
  pressEnter(overlapCancel);
  await flush();
  check("admission: the surviving Cancel still posts", !!lastCall("POST", "/api/intakes/i1/cancel"), "no cancel POST");

  // ---- 4) a raced start repaints into the linked explanation ----
  // The dashboard's pre-reflection is best-effort; the 409 is the authority. A
  // start that loses the race must land on the same explanation it would have
  // shown had the claim been visible on load — not a status line naming an id
  // the operator cannot click.
  installRoutes({ waiting: false }); // no run, no intake → Start is offered
  await navigate("#/tasks/" + REPO + "/" + TASK);
  var opener = doc.querySelector(".open-run-options");
  pressEnter(opener);
  await flush();
  route("GET", "/api/intakes", intakeIn("drafting")); // the race: it claims the repo now
  routeError("POST", "/api/runs", 409, {
    detail: "cannot start a run for repo 'demo': intake i1 is in flight",
    conflict: "intake_in_flight",
    intake_id: "i1",
  });
  pressEnter(doc.querySelector('.start-form button[type="submit"]'));
  await flush();
  check(
    "admission: a raced 409 repaints into the blocking-intake explanation",
    !!doc.querySelector(".run-controls .admission-note a.text-link"),
    "the raced refusal left no linked explanation"
  );
  check(
    "admission: the raced refusal closes the launch dialog",
    doc.querySelector(".run-options") === null || !doc.querySelector(".run-options").open,
    "the launch dialog stayed open over a refusal it cannot retry"
  );
  activeIs("admission: the repaint lands focus on the page heading", pageHeading());

  // A run can also claim the repo while a draft sits in review. Confirm then
  // 409s, and leaving the refused write controls on screen would keep offering
  // an action that now always fails — so it repaints into the overlap view.
  installRoutes({ waiting: false }); // no live run yet → the full review controls
  await navigate("#/intakes/i1");
  check("admission: the review offers Confirm while the repo is unclaimed", !!doc.querySelector(".intake-confirm"), "no Confirm on an unclaimed repo");
  route("GET", "/api/runs", LIVE); // the race: a run claims the repo now
  routeError("POST", "/api/intakes/i1/confirm", 409, {
    detail: "cannot confirm an intake for repo 'demo': run r1 is live",
    conflict: "run_live",
    run_id: RUN,
  });
  pressEnter(doc.querySelector(".intake-confirm"));
  await flush();
  check(
    "admission: a raced confirm withdraws the refused write controls",
    doc.querySelector(".intake-confirm") === null && doc.querySelector(".intake-revise-form") === null,
    "the refused write controls survived the raced 409"
  );
  check(
    "admission: the raced confirm still leaves Cancel available",
    !!doc.querySelector(".intake-cancel"),
    "Cancel was withdrawn along with the refused controls"
  );

  // A raced intake start is refused the same way, from the other direction.
  installRoutes({ waiting: false });
  await navigate("#/repos/" + REPO);
  route("GET", "/api/runs", LIVE); // the race: a run claims the repo now
  routeError("POST", "/api/intakes", 409, {
    detail: "cannot start an intake for repo 'demo': run r1 is live",
    conflict: "run_live",
    run_id: RUN,
  });
  type(doc.querySelector(".intake-request"), "Add a thing");
  pressEnter(doc.querySelector(".intake-form button"));
  await flush();
  check(
    "admission: a raced intake 409 repaints into the blocking-run explanation",
    !!doc.querySelector(".section.intake .admission-note a.text-link"),
    "the raced intake refusal left no linked explanation"
  );

  installRoutes({ waiting: false }); // restore defaults for any later scenario
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
    await scenarioRunHistory();
    await scenarioRunHistoryPaging();
    await scenarioRunNavigation();
    await scenarioLiveRunReadiness();
    await scenarioRepoAdmission();
    await scenarioSubscriptionUsage();
  } catch (err) {
    FAILED++;
    print("not ok - harness crashed: " + err + "\n" + (err && err.stack));
  }
  print("SUMMARY " + PASSED + "/" + (PASSED + FAILED));
})();
