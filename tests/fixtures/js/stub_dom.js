// Shared stub DOM for the JavaScriptCore runtime tests (tests/fixtures/js/*_smoke.js).
// Run as: jsc stub_dom.js <scenario>.js -- <args>; the scenario defines ROUTE(url, opts)
// (returns a fetch Promise) and then load()s the real app.js.
const LOG = [];
const errors = [];
function check(cond, msg) { LOG.push((cond ? "PASS " : "FAIL ") + msg); if (!cond) errors.push(msg); }

class ClassList {
  constructor(el) { this.el = el; }
  _get() { return (this.el.className || "").split(/\s+/).filter(Boolean); }
  add(...c) { const s = new Set(this._get()); c.forEach((x) => s.add(x)); this.el.className = [...s].join(" "); }
  remove(...c) { this.el.className = this._get().filter((x) => !c.includes(x)).join(" "); }
  toggle(c, on) { const has = this.contains(c); const want = on === undefined ? !has : on; want ? this.add(c) : this.remove(c); return want; }
  contains(c) { return this._get().includes(c); }
}
class Node_ {
  constructor(tag) {
    this.tagName = (tag || "div").toUpperCase(); this.children = []; this.parent = null;
    this.attrs = {}; this.dataset = {}; this.style = {}; this.className = ""; this._text = "";
    this.hidden = false; this.value = ""; this.disabled = false; this.checked = false; this.open = false;
    this.handlers = {}; this.classList = new ClassList(this); this.nodeType = 1;
    this.clientWidth = 600; this.clientHeight = 300;
  }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this.children = []; this._text = String(v); }
  append(...kids) { for (const k of kids) { const n = k.nodeType ? k : new Text_(String(k)); n.parent = this; this.children.push(n); } }
  appendChild(k) { this.append(k); return k; }
  replaceChildren(...kids) { this.children = []; this._text = ""; this.append(...kids); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k.startsWith("data-")) this.dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = String(v);
    if (k === "open") this.open = true;
    if (k === "hidden") this.hidden = true;
    if (k === "id") { this.id = String(v); byId[this.id] = this; } // like the real getElementById
    if (k === "value") this.value = String(v);
    if (k === "disabled") this.disabled = true;
  }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; if (k === "open") this.open = false; if (k === "hidden") this.hidden = false; }
  addEventListener(t, fn) { (this.handlers[t] = this.handlers[t] || []).push(fn); }
  fire(t, extra) {
    const ev = Object.assign({ type: t, target: this, preventDefault() {}, stopPropagation() {} }, extra || {});
    for (let n = this; n; n = n.parent) for (const fn of n.handlers[t] || []) fn(Object.assign(ev, { currentTarget: n }));
  }
  matches(sel) {
    const m = sel.match(/^([a-z]+)?((?:\.[\w-]+)*)(?:\[([\w-]+)\])?$/i);
    if (!m) return false;
    if (m[1] && this.tagName !== m[1].toUpperCase()) return false;
    for (const c of (m[2] || "").split(".").filter(Boolean)) if (!this.classList.contains(c)) return false;
    if (m[3] && !(m[3] in this.attrs)) return false;
    return true;
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (c.nodeType === 1) { if (c.matches(sel)) out.push(c); walk(c); } } };
    walk(this);
    return out;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { for (let n = this; n && n.nodeType === 1; n = n.parent) if (n.matches(sel)) return n; return null; }
  focus() { focused = this; }
  blur() {}
  scrollIntoView() { this.scrolled = true; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 600, height: 300, right: 600, bottom: 300 }; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
class Text_ { constructor(t) { this.nodeType = 3; this.textContent = t; } }
let focused = null;
const byId = {};
const document = {
  getElementById(id) { if (!byId[id]) { byId[id] = new Node_(id.includes("dialog") ? "dialog" : "div"); byId[id].id = id; } return byId[id]; },
  createElement: (t) => new Node_(t),
  createElementNS: (_, t) => new Node_(t),
  createTextNode: (t) => new Text_(t),
  handlers: {},
  addEventListener(t, fn) { (this.handlers[t] = this.handlers[t] || []).push(fn); },
};
// static markup the code queries by selector
const provBox = document.getElementById("pricing-provider");
for (const p of ["", "Claude", "Gemini", "GPT", "Other"]) {
  const b = new Node_("button"); b.setAttribute("data-provider", p); b.textContent = p || "All";
  if (p === "Other") b.hidden = true;
  provBox.append(b);
}
const tabBox = document.getElementById("pricing-tabs");
for (const tab of ["models", "subs"]) {
  const b = new Node_("button"); b.setAttribute("data-tab", tab); b.textContent = tab; tabBox.append(b);
}
const rankTabs = document.getElementById("rank-tabs");
for (const by of ["model", "tool", "project"]) {
  const b = new Node_("button"); b.setAttribute("data-by", by); b.textContent = by; rankTabs.append(b);
}
document.getElementById("rank-table").append(new Node_("tbody"));
for (const id of ["price-model-input", "price-model"]) document.getElementById(id);
document.getElementById("price-model-input").hidden = true;

class URLSearchParams {
  constructor(init) {
    this.m = [];
    if (typeof init === "string") for (const kv of init.replace(/^[?#]/, "").split("&").filter(Boolean)) { const [k, v = ""] = kv.split("="); this.m.push([decodeURIComponent(k), decodeURIComponent(v)]); }
    else if (init) for (const [k, v] of Object.entries(init)) this.m.push([k, String(v)]);
  }
  get(k) { const e = this.m.find((x) => x[0] === k); return e ? e[1] : null; }
  set(k, v) { this.delete(k); this.m.push([k, String(v)]); }
  has(k) { return this.m.some((x) => x[0] === k); }
  delete(k) { this.m = this.m.filter((x) => x[0] !== k); }
  toString() { return this.m.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&"); }
}
const requests = [];
function fetch(url, opts) {
  requests.push({ url, opts });
  return ROUTE(url, opts || {});
}
const jsonResponse = (body, status = 200) => Promise.resolve({
  ok: status >= 200 && status < 300, status, statusText: String(status), json: () => Promise.resolve(body) });
function keydown(key, target, extra) {
  const ev = Object.assign({ type: "keydown", key, target: target || document.body, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} }, extra || {});
  for (const fn of document.handlers.keydown || []) fn(ev);
  return ev;
}
document.body = new Node_("body");
const location = { hash: "" };
const history = { last: null, replaceState(_s, _t, url) { this.last = url; location.hash = url; } };
const window = { addEventListener() {}, innerWidth: 1200, innerHeight: 800 };
const timers = [];
function setTimeout(fn) { timers.push(fn); return timers.length; }
function clearTimeout() {}
const console = { log: (...a) => LOG.push("log " + a.join(" ")), error: (...a) => { const s = a.join(" "); if (!s.includes("offline stub")) errors.push("console.error " + s); }, warn() {} };

