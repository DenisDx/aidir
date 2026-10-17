// Boot-order probe for the settings editor.
//
// Loads the real front-end sources in the order index.html does, under a minimal
// DOM, and reports what the page shows before and after authentication.
// Prints one JSON object; test_frontend_boot_order.py asserts on it.
//
// Run manually:  node test_frontend_boot_probe.js

'use strict';

const fs = require('fs');
const path = require('path');

const FRONTEND = path.join(__dirname, 'webui', 'frontend');

// ── Minimal DOM ────────────────────────────────────────────────────────────
function stub(extra) {
  return Object.assign({
    style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, appendChild() {}, removeChild() {},
    setAttribute() {}, getAttribute() { return null; }, focus() {}, blur() {},
    remove() {}, click() {}, insertAdjacentHTML() {}, scrollIntoView() {},
    closest() { return null; }, querySelectorAll() { return []; }, contains() { return false; },
    innerHTML: '', textContent: '', value: '', checked: false, options: [],
    children: [], rows: 3, minLength: 0, maxLength: 0, parentNode: null,
  }, extra);
}

const treeNode = { innerHTML: '', className: '' };
const els = {};

function makeSelect(id) {
  // Records what the page really puts into a <select>, which is the reported bug.
  const node = stub({ id, options: [] });
  node.appendChild = child => {
    node.options.push(child);
    node.innerHTML = node.options.length + ' option(s)';
    return child;
  };
  return node;
}

global.document = {
  getElementById(id) {
    if (id === 'cfg-document') return els[id] || (els[id] = makeSelect(id));
    return els[id] || (els[id] = stub({ id }));
  },
  querySelector(sel) { return sel === '#settings-tree' ? treeNode : stub(); },
  querySelectorAll() { return []; },
  createElement(tag) { return stub({ tagName: tag }); },
  addEventListener() {},
  body: stub(),
  documentElement: stub(),
};

global.window = global;
global.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
global.location = { href: 'http://test/', search: '', pathname: '/', hash: '' };
global.URL = URL;
global.Blob = class {};
global.FormData = class {};
global.requestAnimationFrame = fn => setTimeout(fn, 0);
global.EventSource = class { constructor() {} close() {} addEventListener() {} };

// Dashboard responses: any plural key reads as an empty list, so the unrelated
// work in onLoggedIn() runs to the end instead of throwing on stub data.
function body(extra) {
  const target = Object.assign({ ok: true }, extra);
  return new Proxy(target, {
    get(t, key) {
      if (key in t) return t[key];
      if (typeof key === 'string' && /s$/.test(key)) return [];
      return undefined;
    },
  });
}

// ── Recorded traffic ───────────────────────────────────────────────────────
const requested = [];
// app.js declares its own apiGet, which resolves the parsed body through fetch,
// so fetch is the only seam that has to be accurate here.
global.fetch = async (url, init) => {
  const u = String(url);
  requested.push((init && init.method ? init.method + ' ' : '') + u);
  if (u.startsWith('/api/auth/')) {
    return { ok: false, status: 401, json: async () => ({}) };
  }
  if (u.startsWith('/api/settings-info')) {
    return {
      ok: true, status: 200,
      json: async () => body({ documents: ['main', 'worker:tool:web_search'] }),
    };
  }
  return { ok: true, status: 200, json: async () => body({}) };
};
global.apiPost = async () => ({ ok: true, status: 200, data: { ok: true } });

// ── Library assets are plain <script> tags ahead of app.js ────────────────
const mounts = [];
global.aiset = {
  mountTree(selector, options) {
    mounts.push({ selector, document: options && options.document });
    treeNode.innerHTML = '<div class="aiset">editor</div>';
    return { on() {}, destroy() {}, setDocument() {} };
  },
};

// app.js is a classic script, so its top-level declarations are reachable here.
const appSource = fs.readFileSync(path.join(FRONTEND, 'app.js'), 'utf8');
const viewerSource = fs.readFileSync(path.join(FRONTEND, 'task_steps_viewer.js'), 'utf8');

const report = { bootError: null, beforeLogin: null, afterLogin: null };

function snapshot() {
  const select = els['cfg-document'];
  return {
    requestedSettingsInfo: requested.some(u => u.startsWith('/api/settings-info')),
    documentOptions: select ? select.options.map(o => o.value) : [],
    statusLine: (els['cfg-aiset-status'] || { textContent: '' }).textContent || '',
    mountedOn: mounts.length ? mounts[mounts.length - 1].selector : null,
    treeHtml: treeNode.innerHTML || '',
  };
}

(async () => {
  try {
    eval(viewerSource);
    eval(appSource + '\n;global.__probe = { initSettingsBackend, onLoggedIn, doLogout };');
  } catch (err) {
    // A ReferenceError here is exactly defect 1: app.js aborted at load.
    report.bootError = String(err && err.message ? err.message : err);
  }

  await new Promise(resolve => setTimeout(resolve, 30));
  report.beforeLogin = snapshot();

  if (!report.bootError) {
    // The real path: exactly what the app runs once the session probe succeeds.
    // initSettingsBackend() is reached before the dashboard work, so a failure
    // in that unrelated work cannot hide a regression here.
    try {
      global.__probe.onLoggedIn();
    } catch (err) {
      report.onLoggedInError = String(err && err.message ? err.message : err);
    }
    await new Promise(resolve => setTimeout(resolve, 60));
    report.afterLogin = snapshot();
  }

  process.stdout.write(JSON.stringify(report, null, 2));
  // onLoggedIn() starts timers and an event stream; leave explicitly instead of
  // waiting for an event loop that will never drain.
  process.exit(report.bootError ? 1 : 0);
})();