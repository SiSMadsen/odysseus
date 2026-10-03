// A small fake browser page for driving the real Creator window
// (static/js/creator/panel.js) under plain node, in the style of
// tests/helpers/test_settings_shell.js: no jsdom, nothing to install.
//
// It provides only what panel.js uses: elements with attributes, classes,
// children, events and simple selectors; document and window; fetch (routed
// to a table the test gives); EventSource (the test pushes messages);
// localStorage; requestAnimationFrame; Notification. The chat's markdown
// renderer and the theme module are replaced by stand-ins (they load most of
// the app), through a module loader hook.
//
// Use (from tests/test_creator_window_dom.py):
//   const h = await import('<this file>');
//   const env = h.setup({ admin: true, routes: { 'GET /api/creator/jobs?limit=100': {jobs: []} } });
//   const panel = await env.loadPanel();
//   panel.openPanel(); await env.settle();

import { register } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PANEL = pathToFileURL(path.join(HERE, '..', '..', 'static', 'js', 'creator', 'panel.js')).href;

const STUBS = {
  'markdown.js': 'export default { mdToHtml: (s) => "<md>" + String(s) + "</md>" };',
  'theme.js': 'export default { makeDraggable() {} };',
};

const HOOKS = `
const STUBS = ${JSON.stringify(STUBS)};
export async function resolve(specifier, context, next) {
  const parent = context.parentURL || '';
  if (parent.endsWith('/static/js/creator/panel.js')) {
    for (const [name, src] of Object.entries(STUBS)) {
      if (specifier === '../' + name) {
        return { url: 'data:text/javascript,' + encodeURIComponent(src), shortCircuit: true };
      }
    }
  }
  return next(specifier, context);
}
`;
register('data:text/javascript,' + encodeURIComponent(HOOKS));

// ---------------------------------------------------------------------------
// Elements
// ---------------------------------------------------------------------------

class ClassList {
  constructor(el) { this.el = el; this.values = new Set(); }
  add(...names) { names.filter(Boolean).forEach(n => this.values.add(n)); }
  remove(...names) { names.forEach(n => this.values.delete(n)); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.contains(name) : !!force;
    if (on) this.add(name); else this.remove(name);
    return on;
  }
}

class Style {
  constructor() { this.display = ''; this.cssText = ''; }
}

export class Element {
  constructor(tagName, doc) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = doc;
    this.children = [];
    this.parentElement = null;
    this.attributes = {};
    this.classList = new ClassList(this);
    this.dataset = {};
    this.style = new Style();
    this.listeners = {};
    this._text = '';
    this._html = '';
    this._value = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.open = false;
    this.title = '';
    this.placeholder = '';
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.focused = false;
  }

  // -- attributes --------------------------------------------------------
  get id() { return this.attributes.id || ''; }
  set id(v) { this.attributes.id = String(v); }
  get className() { return [...this.classList.values].join(' '); }
  set className(v) { this.classList.values = new Set(String(v || '').split(/\s+/).filter(Boolean)); }
  setAttribute(name, value) {
    const v = String(value);
    if (name === 'class') { this.className = v; return; }
    if (name === 'hidden') { this.hidden = true; return; }
    if (name === 'disabled') { this.disabled = true; return; }
    if (name === 'title') this.title = v;
    if (name === 'placeholder') this.placeholder = v;
    if (name === 'value') this._value = v;
    if (name === 'open') this.open = true;
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
    this.attributes[name] = v;
  }
  getAttribute(name) {
    if (name === 'class') return this.className;
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  hasAttribute(name) { return this.getAttribute(name) !== null; }
  removeAttribute(name) { delete this.attributes[name]; }

  // -- value (inputs, textareas, selects) ---------------------------------
  get options() { return this.tagName === 'SELECT' ? this.children.filter(c => c.tagName === 'OPTION') : undefined; }
  get value() {
    if (this.tagName === 'SELECT') {
      const opts = this.options;
      const chosen = opts.find(o => o.selected) || opts[0];
      return chosen ? chosen.value : '';
    }
    if (this.tagName === 'OPTION') return this.attributes.value !== undefined ? this.attributes.value : this.textContent;
    return this._value;
  }
  set value(v) {
    if (this.tagName === 'SELECT') {
      this.options.forEach(o => { o.selected = o.value === String(v); });
      return;
    }
    this._value = String(v ?? '');
  }

  // -- text --------------------------------------------------------------
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this._detachAll(); this._text = String(v ?? ''); this._html = ''; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._detachAll(); this._html = String(v ?? ''); this._text = ''; }
  get firstChild() { return this.children[0] || null; }

  // -- tree ----------------------------------------------------------------
  _detachAll() { this.children.forEach(c => { c.parentElement = null; }); this.children = []; }
  appendChild(child) {
    if (child.parentElement) child.parentElement.removeChild(child);
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  append(...nodes) { nodes.forEach(n => this.appendChild(n)); }
  removeChild(child) {
    this.children = this.children.filter(c => c !== child);
    child.parentElement = null;
    return child;
  }
  replaceChildren(...nodes) { this._detachAll(); nodes.forEach(n => this.appendChild(n)); }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  contains(other) {
    for (let n = other; n; n = n.parentElement) if (n === this) return true;
    return false;
  }
  descendants() {
    const out = [];
    const walk = (el) => el.children.forEach(c => { out.push(c); walk(c); });
    walk(this);
    return out;
  }
  querySelectorAll(selector) { return selectAll(this, selector); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let n = this; n; n = n.parentElement) if (matchesSelector(n, selector)) return n;
    return null;
  }
  matches(selector) { return matchesSelector(this, selector); }

  // -- events --------------------------------------------------------------
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(f => f !== fn); }
  dispatchEvent(event) {
    event.target = event.target || this;
    (this.listeners[event.type] || []).slice().forEach(fn => fn(event));
    return !event.defaultPrevented;
  }
  click() {
    if (this.disabled) return;
    const doc = this.ownerDocument;
    if (doc && this.tagName === 'A') doc.clickedLinks.push({ href: this.attributes.href, download: this.attributes.download });
    this.dispatchEvent(makeEvent('click', { target: this }));
  }
  focus() { this.focused = true; if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  blur() { this.focused = false; }
}

export function makeEvent(type, props = {}) {
  return {
    type, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
    ...props,
  };
}

// Selectors: compound parts (tag, #id, .class, [attr], [attr="v"]) joined by
// spaces (descendant). Enough for what panel.js and the tests ask for.
function parseCompound(text) {
  const parts = { tag: null, id: null, classes: [], attrs: [] };
  const re = /([a-zA-Z][a-zA-Z0-9-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let m;
  while ((m = re.exec(text))) {
    if (m[1]) parts.tag = m[1].toUpperCase();
    else if (m[2]) parts.id = m[2];
    else if (m[3]) parts.classes.push(m[3]);
    else parts.attrs.push([m[4], m[5]]);
  }
  return parts;
}

function matchesCompound(el, p) {
  if (p.tag && el.tagName !== p.tag) return false;
  if (p.id && el.id !== p.id) return false;
  if (!p.classes.every(c => el.classList.contains(c))) return false;
  return p.attrs.every(([name, val]) => {
    const got = el.getAttribute(name);
    return val === undefined ? got !== null : got === val;
  });
}

function matchesSelector(el, selector) {
  return selector.split(',').some((one) => {
    const chain = one.trim().split(/\s+/).map(parseCompound);
    if (!matchesCompound(el, chain[chain.length - 1])) return false;
    let i = chain.length - 2;
    for (let n = el.parentElement; n && i >= 0; n = n.parentElement) {
      if (matchesCompound(n, chain[i])) i--;
    }
    return i < 0;
  });
}

function selectAll(root, selector) {
  return root.descendants().filter(el => matchesSelector(el, selector));
}

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

class Document {
  constructor() {
    this.documentElement = new Element('html', this);
    this.head = new Element('head', this);
    this.body = new Element('body', this);
    this.documentElement.append(this.head, this.body);
    this.listeners = {};
    this.hidden = false;
    this.activeElement = null;
    this.clickedLinks = [];
  }
  createElement(tag) { return new Element(tag, this); }
  getElementById(id) { return this.documentElement.descendants().find(el => el.id === id) || null; }
  querySelector(selector) { return this.documentElement.querySelector(selector); }
  querySelectorAll(selector) { return this.documentElement.querySelectorAll(selector); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(f => f !== fn); }
  dispatchEvent(event) { (this.listeners[event.type] || []).slice().forEach(fn => fn(event)); }
}

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.onopen = this.onmessage = this.onerror = null;
    FakeEventSource.instances.push(this);
  }
  close() { this.closed = true; }
  // Test side:
  emit(data) { if (!this.closed && this.onmessage) this.onmessage({ data: JSON.stringify(data) }); }
  fail() { if (!this.closed && this.onerror) this.onerror({}); }
  opened() { if (!this.closed && this.onopen) this.onopen({}); }
}
FakeEventSource.instances = [];

/** Not hidden itself, nor inside anything hidden or display:none. */
export function visible(el) {
  for (let n = el; n; n = n.parentElement) {
    if (n.hidden || n.style.display === 'none') return false;
  }
  return true;
}

/**
 * Installs the fake page as globals. `routes` maps "METHOD /path" (the path
 * as fetched, query included) to a reply: a plain value (200 JSON), or
 * {status, body}, or a function (body, url) returning either. Unrouted
 * requests get 404. Returns helpers for the test.
 */
export function setup({ admin = false, routes = {}, innerWidth = 1280, notification = null } = {}) {
  const doc = new Document();
  const calls = [];
  const storage = new Map();
  for (const id of ['tool-creator-btn', 'rail-creator']) {
    const btn = doc.createElement('button');
    btn.id = id;
    doc.body.appendChild(btn);
  }
  const notifications = [];
  class Notification {
    constructor(title, opts) { notifications.push({ title, ...(opts || {}) }); }
  }
  Notification.permission = notification || 'default';

  const win = globalThis;
  win.document = doc;
  win.window = win;
  win._isAdmin = admin;
  win.innerWidth = innerWidth;
  win.EventSource = FakeEventSource;
  if (notification) win.Notification = Notification; else delete win.Notification;
  win.requestAnimationFrame = (fn) => setTimeout(fn, 0);
  Object.defineProperty(win, 'localStorage', {
    configurable: true,
    value: {
      getItem: k => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: k => storage.delete(k),
    },
  });
  win.fetch = async (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    let body = null;
    if (options.body) { try { body = JSON.parse(options.body); } catch (_) { body = options.body; } }
    calls.push({ method, url, body });
    let reply = routes[`${method} ${url}`];
    if (typeof reply === 'function') reply = reply(body, url);
    if (reply === undefined) reply = { status: 404, body: { detail: `no route for ${method} ${url}` } };
    const wrapped = reply && typeof reply === 'object' && 'status' in reply && 'body' in reply
      ? reply : { status: 200, body: reply };
    return {
      ok: wrapped.status >= 200 && wrapped.status < 300,
      status: wrapped.status,
      json: async () => JSON.parse(JSON.stringify(wrapped.body)),
    };
  };

  return {
    doc, calls, routes, notifications, storage,
    streams: FakeEventSource.instances,
    async loadPanel() { return import(PANEL); },
    /** Let pending promises, timers at 0 ms and animation frames run. */
    async settle(rounds = 6) {
      for (let i = 0; i < rounds; i++) await new Promise(r => setTimeout(r, 0));
    },
    byId: id => doc.getElementById(id),
    $: sel => doc.querySelector(sel),
    $$: sel => doc.querySelectorAll(sel),
    text: sel => (doc.querySelector(sel) || { textContent: null }).textContent,
    visible,
    /** Labels of the visible buttons (in `sel`, or the whole page). */
    buttons(sel) {
      const root = sel ? doc.querySelector(sel) : doc.documentElement;
      return root ? root.querySelectorAll('button').filter(visible).map(b => b.textContent) : [];
    },
    clickButton(label, sel) {
      const root = sel ? doc.querySelector(sel) : doc.documentElement;
      const btn = root && root.querySelectorAll('button').find(b => b.textContent === label && visible(b));
      if (!btn) throw new Error(`no button "${label}"`);
      btn.click();
      return btn;
    },
    type(id, value) {
      const el = doc.getElementById(id);
      el.value = value;
      el.dispatchEvent(makeEvent('input', { target: el }));
      return el;
    },
    key(target, key, extra = {}) {
      const el = typeof target === 'string' ? doc.getElementById(target) : target;
      const ev = makeEvent('keydown', { key, target: el, ...extra });
      if (el) el.dispatchEvent(ev); else doc.dispatchEvent(ev);
      return ev;
    },
    lastCall(method, prefix) {
      return calls.filter(c => c.method === method && c.url.startsWith(prefix)).pop() || null;
    },
    /** Timeline items as [class, text] pairs, for assertions. */
    timeline() {
      const tl = doc.getElementById('creator-timeline');
      return tl ? tl.children.map(c => [c.className, c.textContent]) : [];
    },
  };
}
