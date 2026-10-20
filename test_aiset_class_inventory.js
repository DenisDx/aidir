// Dump the class inventory the library declares, through its own public API.
//
// The inventory is `aiset.classes()` since aiset 97748c8, so a host can check
// its stylesheet coverage against the contract instead of grepping 2000 lines of
// JavaScript. That is what this probe exists to exercise: the contract is only
// worth having if it is reachable at runtime.
//
// Prints one JSON array; test_settings_styles.py asserts on it.
// Run manually:  node test_aiset_class_inventory.js

'use strict';

const fs = require('fs');
const path = require('path');

const ASSETS = path.join(__dirname, 'aiset', 'src', 'aiset', 'assets');

function stub(extra) {
  return Object.assign({
    style: { setProperty() {}, removeProperty() {} }, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, appendChild() {}, removeChild() {},
    setAttribute() {}, getAttribute() { return null; }, focus() {}, blur() {},
    remove() {}, click() {}, insertAdjacentHTML() {}, scrollIntoView() {},
    closest() { return null; }, querySelectorAll() { return []; }, contains() { return false; },
    innerHTML: '', textContent: '', value: '', checked: false, options: [], children: [],
    rows: 3, minLength: 0, maxLength: 0, offsetHeight: 0,
    getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; },
  }, extra);
}

const doc = {
  getElementById() { return null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement(tag) { const n = stub({ tagName: tag.toUpperCase() }); n.ownerDocument = doc; return n; },
  createDocumentFragment() { return stub(); },
  createTextNode(text) { const n = stub(); n.textContent = text; return n; },
  addEventListener() {}, body: stub(), documentElement: stub(),
};

global.document = doc;
global.window = global;
// Browser globals the page uses that Node does not provide.
global.addEventListener = () => {};
global.matchMedia = query => ({ matches: false, media: query, addListener(){}, removeListener(){} });
// Node 22 defines navigator as a getter-only global; define it instead.
Object.defineProperty(global, 'navigator', { value: { userAgent: 'node' }, configurable: true });
global.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
global.location = { href: 'http://x/', pathname: '/', search: '', hash: '' };
global.fetch = async () => ({ ok: true, status: 200, json: async () => ({ ok: true }), text: async () => '' });
global.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } };
global.Event = global.CustomEvent;
global.MutationObserver = class { observe() {} disconnect() {} };
global.ResizeObserver = class { observe() {} disconnect() {} };
global.requestAnimationFrame = fn => setTimeout(fn, 0);
global.getComputedStyle = () => ({ getPropertyValue() { return ''; } });
global.alert = () => {};
global.URL = URL;
global.Blob = class {};
global.FormData = class {};

try {
  // aiset.js first: aiset-tree.js extends the namespace it publishes.
  eval(fs.readFileSync(path.join(ASSETS, 'aiset.js'), 'utf8'));
  eval(fs.readFileSync(path.join(ASSETS, 'aiset-tree.js'), 'utf8'));
} catch (err) {
  process.stdout.write(JSON.stringify({ error: String(err && err.message ? err.message : err) }));
  process.exit(1);
}

if (!global.aiset || typeof global.aiset.classes !== 'function') {
  process.stdout.write(JSON.stringify({ error: 'aiset.classes() is not available' }));
  process.exit(1);
}

process.stdout.write(JSON.stringify(global.aiset.classes(), null, 2));