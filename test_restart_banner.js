// Restart-banner probe for the WebUI.
//
// The reported symptom: the yellow "the service is restarting" banner appeared
// and then disappeared while the service had not restarted yet.
//
// Two causes, both in the front end:
//
//   1. apiGet() answered 401 -> doLogout() -> the interface switched to the
//      login screen, and the banner lives in the application screen.
//   2. The banner was derived from the last *successful* poll, so a service that
//      stopped answering cleared it.
//
// The probe drives the real front-end sources under a minimal DOM and reports what
// the banner shows at each step of a restart. Prints one JSON object;
// test_restart_banner.py asserts on it.
//
// Run manually:  node test_restart_banner.js

'use strict';

const fs = require('fs');
const path = require('path');

const FRONTEND = path.join(__dirname, 'webui', 'frontend');

function stub(extra) {
  return Object.assign({
    style: { setProperty() {}, removeProperty() {} }, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, appendChild() {}, removeChild() {},
    setAttribute() {}, getAttribute() { return null; }, focus() {}, blur() {},
    remove() {}, click() {}, insertAdjacentHTML() {}, scrollIntoView() {},
    closest() { return null; }, querySelectorAll() { return []; }, contains() { return false; },
    innerHTML: '', textContent: '', value: '', checked: false, options: [],
    children: [], rows: 3, minLength: 0, maxHeight: 0, parentNode: null,
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
  }, extra);
}

const els = {};
global.document = {
  getElementById(id) { return els[id] || (els[id] = stub({ id })); },
  querySelector() { return stub(); },
  querySelectorAll() { return []; },
  createElement(tag) { return stub({ tagName: tag }); },
  addEventListener() {},
  body: stub(),
  documentElement: stub(),
};

global.window = global;
global.addEventListener = () => {};
global.matchMedia = query => ({ matches: false, media: query, addListener(){}, removeListener(){} });
global.innerHeight = 1080;
global.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
global.location = { href: 'http://test/', search: '', pathname: '/', hash: '' };
global.URL = URL;
global.Blob = class {};
global.FormData = class {};
global.requestAnimationFrame = fn => setTimeout(fn, 0);
global.EventSource = class { constructor() {} close() {} addEventListener() {} };
// The Restart button asks for a confirmation first.
global.confirm = () => true;
global.window.confirm = global.confirm;
global.alert = () => {};

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

// ── The fetch seam: the probe decides what the service answers ────────────
let mode = 'healthy';
global.fetch = async (url) => {
  const u = String(url);
  if (u.startsWith('/api/auth/me')) {
    // The operator is signed in.
    return { ok: true, status: 200, json: async () => body({ ok: true, login: 'tester', permissions: ['all'] }) };
  }
  if (mode === 'unreachable') {
    throw new Error('connection refused');
  }
  if (mode === 'unauthorized') {
    return { ok: false, status: 401, json: async () => ({}) };
  }
  if (u.startsWith('/api/status')) {
    return {
      ok: true, status: 200,
      json: async () => body({
        runtime: {
          restart_requested: mode === 'draining',
          accepting_new_tasks: mode !== 'draining',
          active_tasks: mode === 'draining' ? 2 : 0,
          restart_wait_timeout: 120,
        },
      }),
    };
  }
  if (u.startsWith('/api/restart')) {
    return {
      ok: true, status: 200,
      json: async () => body({
        ok: true,
        runtime: { restart_requested: true, active_tasks: 2, restart_wait_timeout: 120 },
      }),
    };
  }
  return { ok: true, status: 200, json: async () => body({}) };
};

const appSource = fs.readFileSync(path.join(FRONTEND, 'app.js'), 'utf8');
const report = { bootError: null, steps: {} };

// The banner lives in the application screen. A logout hides that screen, so the
// banner is not visible any more even though its own class is still set.
function bannerVisible() {
  const banner = els['runtime-banner'];
  const screen = els['app-screen'];
  if (!banner) return null;
  const classes = banner.classList && banner.classList.classes ? banner.classList.classes : null;
  const marked = classes ? classes.has('visible') : Boolean(banner.visible);
  const screenVisible = screen ? screen.style.display !== 'none' : true;
  return marked && screenVisible;
}

function appScreenVisible() {
  const screen = els['app-screen'];
  return screen ? screen.style.display !== 'none' : null;
}

function step(name, extra) {
  report.steps[name] = Object.assign(
    { visible: bannerVisible(), screen: appScreenVisible() },
    extra || {},
  );
}

(async () => {
  let probe = null;
  try {
    eval(appSource + '\n;global.__probe = { applyRuntimeStatus, loadTasks, requestRestart, showScreen, '
      + 'isRestartInProgress: (typeof isRestartInProgress === "function") ? isRestartInProgress : function () { return false; }, '
      + 'debugState: (typeof restartLatched === "undefined") ? function () { return null; } : function () { return { latched: restartLatched, down: serverWasDown, requested: runtimeState.restart_requested }; } };');
    probe = global.__probe;
  } catch (err) {
    report.bootError = String(err && err.message ? err.message : err);
  }

  if (!report.bootError) {
    // The banner element records the "visible" class the way the page does.
    const banner = els['runtime-banner'] = stub({ id: 'runtime-banner' });
    const classes = new Set();
    banner.classList = {
      classes,
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      toggle(name, on) { if (on) { classes.add(name); } else { classes.delete(name); } },
      contains(name) { return classes.has(name); },
    };
    els['app-screen'] = stub({ id: 'app-screen' });
    els['login-screen'] = stub({ id: 'login-screen' });
    els['runtime-status'] = stub({ id: 'runtime-status' });
    els['restart-btn'] = stub({ id: 'restart-btn' });
    els['restart-card'] = stub({ id: 'restart-card' });
    els['restart-active-count'] = stub({ id: 'restart-active-count' });
    els['restart-timeout-val'] = stub({ id: 'restart-timeout-val' });

    // The operator is signed in: the application screen is the visible one.
    probe.showScreen('app');

    // 1. The operator asks for a restart: the banner appears.
    await probe.requestRestart();
    step('afterRequest', { busy: probe.isRestartInProgress() });

    // 2. The service stops answering: the banner must stay.
    mode = 'unreachable';
    await probe.loadTasks();
    step('afterUnreachable', { busy: probe.isRestartInProgress() });

    // 3. The service answers 401: the operator must not be logged out and the
    //    banner must stay.
    mode = 'unauthorized';
    await probe.loadTasks();
    step('afterUnauthorized', { busy: probe.isRestartInProgress(), state: probe.debugState ? probe.debugState() : null });

    // 4. The service is back and idle: the banner goes away.
    mode = 'healthy';
    await probe.loadTasks();
    step('afterRecovery', { busy: probe.isRestartInProgress() });
  }

  process.stdout.write(JSON.stringify(report, null, 2));
  process.exit(report.bootError ? 1 : 0);
})();
