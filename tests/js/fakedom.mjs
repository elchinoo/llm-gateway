// Just enough DOM for the kit helpers (h, fill, $) and the admin pages: elements, text nodes, listeners, a fake clock.
let active = null;   // document.activeElement
export class Node_ { constructor() { this.parent = null; } }
export class Text extends Node_ { constructor(t) { super(); this.nodeType = 3; this.data = String(t); } get textContent() { return this.data; } }
export class El extends Node_ {
  constructor(tag) { super(); this.nodeType = 1; this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this.listeners = {}; this.className = ""; this.value = ""; this.hidden = false; this.id = ""; this.disabled = false; this.classes = new Set();
    this.classList = { add: (c) => this.classes.add(c), remove: (c) => this.classes.delete(c), contains: (c) => this.classes.has(c) }; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "id") this.id = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  append(...kids) { for (const k of kids) { k.parent = this; this.children.push(k); } }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(t) { this.replaceChildren(new Text(t)); }
  dispatch(type, extra = {}) { if (type === "click" && "disabled" in this.attrs) return null; const ev = { type, target: this, preventDefault() {}, ...extra }; for (const fn of this.listeners[type] || []) fn(ev); return ev; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  matches() { return false; }
  // what the kit's confirmDialog/promptDialog use: focus tracking, remove, contains
  focus() { if (!this.disabled) active = this; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; if (active && !active.isConnectedTo(globalThis.document.body)) active = null; }
  walk(fn) { fn(this); for (const c of this.children) if (c instanceof El) c.walk(fn); }
  all(pred) { const out = []; this.walk((e) => { if (pred(e)) out.push(e); }); return out; }
  isConnectedTo(root) { for (let n = this; n; n = n.parent) if (n === root) return true; return false; }
}

export function install() {
  const byId = {};
  const doc = {
    createElement: (t) => new El(t), createTextNode: (t) => new Text(t),
    getElementById: (id) => byId[id] ?? null,
    body: new El("body"),
    get activeElement() { return active; },
  };
  active = null;
  for (const id of ["view-models", "form", "form-body", "toast"]) { const e = new El(id === "form" ? "dialog" : "div"); e.id = id; byId[id] = e; }
  globalThis.document = doc;
  // A fake clock: nothing runs until run() is called, then every pending timer fires (intervals fire `rounds` times).
  const timers = [];
  globalThis.setTimeout = (fn) => { timers.push({ fn, every: false }); return timers.length; };
  globalThis.setInterval = (fn) => { timers.push({ fn, every: true }); return timers.length; };
  globalThis.clearTimeout = globalThis.clearInterval = () => {};
  globalThis.__calls = [];
  return { byId, fireTimers(rounds = 3) { for (let r = 0; r < rounds; r++) for (const t of timers.slice()) t.fn(); }, timerCount: () => timers.length };
}
