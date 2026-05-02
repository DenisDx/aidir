/* AI Director – frontend application logic */
'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let token = '';
let logWs = null;
let logTimer = null;
let refreshTimer = null;

const TEXT_PICKERS = [
  {
    id: 'cfg-logging-level',
    rootId: 'cfg-logging-level-picker',
    inputId: 'cfg-logging-level',
    toggleId: 'cfg-logging-level-toggle',
    menuId: 'cfg-logging-level-menu',
    options: [
      { label: '"emerg"', value: '"emerg"' },
      { label: '"alert"', value: '"alert"' },
      { label: '"crit"', value: '"crit"' },
      { label: '"error"', value: '"error"' },
      { label: '"warn"', value: '"warn"' },
      { label: '"notice"', value: '"notice"' },
      { label: '"info"', value: '"info"' },
      { label: '"debug"', value: '"debug"' },
      { label: '"${LOG_LEVEL:-info}"', value: '"${LOG_LEVEL:-info}"' },
    ],
  },
  {
    id: 'cfg-logging-levels-workers',
    rootId: 'cfg-logging-levels-workers-picker',
    inputId: 'cfg-logging-levels-workers',
    toggleId: 'cfg-logging-levels-workers-toggle',
    menuId: 'cfg-logging-levels-workers-menu',
    options: [],
  },
  {
    id: 'cfg-logging-levels-http',
    rootId: 'cfg-logging-levels-http-picker',
    inputId: 'cfg-logging-levels-http',
    toggleId: 'cfg-logging-levels-http-toggle',
    menuId: 'cfg-logging-levels-http-menu',
    options: [],
  },
  {
    id: 'cfg-logging-levels-webui',
    rootId: 'cfg-logging-levels-webui-picker',
    inputId: 'cfg-logging-levels-webui',
    toggleId: 'cfg-logging-levels-webui-toggle',
    menuId: 'cfg-logging-levels-webui-menu',
    options: [],
  },
  {
    id: 'cfg-logging-levels-core',
    rootId: 'cfg-logging-levels-core-picker',
    inputId: 'cfg-logging-levels-core',
    toggleId: 'cfg-logging-levels-core-toggle',
    menuId: 'cfg-logging-levels-core-menu',
    options: [],
  },
  {
    id: 'cfg-logging-levels-endpoint',
    rootId: 'cfg-logging-levels-endpoint-picker',
    inputId: 'cfg-logging-levels-endpoint',
    toggleId: 'cfg-logging-levels-endpoint-toggle',
    menuId: 'cfg-logging-levels-endpoint-menu',
    options: [],
  },
  {
    id: 'cfg-logging-levels-middleware',
    rootId: 'cfg-logging-levels-middleware-picker',
    inputId: 'cfg-logging-levels-middleware',
    toggleId: 'cfg-logging-levels-middleware-toggle',
    menuId: 'cfg-logging-levels-middleware-menu',
    options: [],
  },
];

const LEVEL_LIST_OPTIONS = [
  { label: 'NOT DEFINED', value: 'NOT DEFINED' },
  { label: '"emerg"', value: '"emerg"' },
  { label: '"alert"', value: '"alert"' },
  { label: '"crit"', value: '"crit"' },
  { label: '"error"', value: '"error"' },
  { label: '"warn"', value: '"warn"' },
  { label: '"notice"', value: '"notice"' },
  { label: '"info"', value: '"info"' },
  { label: '"debug"', value: '"debug"' },
  { label: '"${LOG_LEVEL:-info}"', value: '"${LOG_LEVEL:-info}"' },
];

const GUI_LEVEL_FIELDS = [
  { inputId: 'cfg-logging-level', key: 'logging.level', allowNotDefined: false },
  { inputId: 'cfg-logging-levels-workers', key: 'logging.levels.workers', allowNotDefined: true },
  { inputId: 'cfg-logging-levels-http', key: 'logging.levels.http', allowNotDefined: true },
  { inputId: 'cfg-logging-levels-webui', key: 'logging.levels.webui', allowNotDefined: true },
  { inputId: 'cfg-logging-levels-core', key: 'logging.levels.core', allowNotDefined: true },
  { inputId: 'cfg-logging-levels-endpoint', key: 'logging.levels.endpoint', allowNotDefined: true },
  { inputId: 'cfg-logging-levels-middleware', key: 'logging.levels.middleware', allowNotDefined: true },
];

for (const picker of TEXT_PICKERS) {
  if (picker.id.startsWith('cfg-logging-levels-')) {
    picker.options = LEVEL_LIST_OPTIONS;
  }
}

// ── DOM helpers ────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

function showScreen(name) {
  $('login-screen').style.display = name === 'login' ? 'flex' : 'none';
  $('app-screen').style.display   = name === 'app'   ? 'flex' : 'none';
}

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('nav button[id^=nav-]').forEach(b => b.classList.remove('active'));
  $(`page-${name}`).classList.add('active');
  $(`nav-${name}`).classList.add('active');

  if (name === 'settings') loadConfig();
}

// ── Auth ───────────────────────────────────────────────────────────────────
async function apiPost(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
    credentials: 'include',
  });
  return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
}

async function apiGet(url) {
  const r = await fetch(url, { credentials: 'include' });
  if (r.status === 401) { doLogout(); return null; }
  return r.ok ? r.json().catch(() => null) : null;
}

$('login-btn').addEventListener('click', async () => {
  $('login-error').textContent = '';
  const res = await apiPost('/api/auth/login', {
    login:    $('login-input').value,
    password: $('pass-input').value,
  });
  if (res.ok) {
    token = res.data.token || '';
    onLoggedIn();
  } else {
    $('login-error').textContent = res.data.detail || 'Invalid credentials';
  }
});

$('login-input').addEventListener('keydown', e => { if (e.key === 'Enter') $('login-btn').click(); });
$('pass-input').addEventListener('keydown',  e => { if (e.key === 'Enter') $('login-btn').click(); });

$('logoff-btn').addEventListener('click', async () => {
  await apiPost('/api/auth/logout', {});
  doLogout();
});

function doLogout() {
  token = '';
  stopLiveLogs();
  clearInterval(refreshTimer);
  showScreen('login');
}

function onLoggedIn() {
  showScreen('app');
  showPage('dashboard');
  loadTasks();
  startLiveLogs();
  refreshTimer = setInterval(loadTasks, 3000);
}

// ── Tasks ──────────────────────────────────────────────────────────────────
async function loadTasks() {
  const [data, status] = await Promise.all([
    apiGet('/api/tasks'),
    apiGet('/api/status'),
  ]);
  if (!data) return;

  $('stat-tasks').textContent = data.tasks.length;

  const body = $('tasks-body');
  body.innerHTML = '';
  data.tasks.forEach(t => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="font-family:monospace;font-size:11px">${t.id.slice(0, 8)}…</td>
      <td>${t.type}</td>
      <td><span class="badge badge-${t.status}">${t.status}</span></td>
      <td>${t.worker_id || '—'}</td>
      <td style="color:var(--muted);font-size:12px">${fmtTime(t.created_at)}</td>
    `;
    body.appendChild(tr);
  });

  if (status) {
    $('stat-workers').textContent = Object.keys(status.workers).length;
    renderResources(status.resources || []);
  }
}

function renderResources(resources) {
  const body = $('resources-body');
  if (!body) return;
  body.innerHTML = '';

  if (!resources.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="4" style="color:var(--muted)">No resources configured</td>';
    body.appendChild(tr);
    return;
  }

  resources.forEach(r => {
    const usageParts = Object.keys(r.limits || {}).map(key => {
      const used = Number((r.used || {})[key] || 0);
      const limit = Number((r.limits || {})[key] || 0);
      return `${key}: ${used}/${limit}`;
    });

    const consumers = (r.consumers || []).map(c => {
      const details = Object.entries(c.usage || {})
        .map(([k, v]) => `${k}:${v}`)
        .join(', ');
      return `<div style="margin-bottom:2px"><span style="font-family:monospace">${escapeHtml(c.id)}</span> <span style="color:var(--muted)">${escapeHtml(details)}</span></div>`;
    }).join('') || '<span style="color:var(--muted)">—</span>';

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHtml(r.id || '—')}</td>
      <td>${escapeHtml(r.type || '—')}</td>
      <td>${escapeHtml(usageParts.join(' | ') || '—')}</td>
      <td>${consumers}</td>
    `;
    body.appendChild(tr);
  });
}

function escapeHtml(s) {
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleTimeString();
}

// ── Live logs ──────────────────────────────────────────────────────────────
function startLiveLogs() {
  connectLogWs();
  $('log-file-select').addEventListener('change', reconnectLogs);
  $('log-live').addEventListener('change', () => {
    if ($('log-live').checked) connectLogWs(); else stopLiveLogs();
  });
  $('log-interval').addEventListener('change', () => {
    if (logWs) { stopLiveLogs(); connectLogWs(); }
  });
}

function stopLiveLogs() {
  if (logWs) { logWs.close(); logWs = null; }
  clearTimeout(logTimer);
}

function reconnectLogs() {
  stopLiveLogs();
  $('log-box').textContent = '';
  if ($('log-live').checked) connectLogWs();
}

function connectLogWs() {
  const file = $('log-file-select').value;
  const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
  // Cookie is sent automatically by the browser; no token in URL needed
  const url = `${wsProto}://${location.host}/ws/logs?file=${file}`;

  logWs = new WebSocket(url);

  logWs.onmessage = e => appendLog(e.data);
  logWs.onclose = () => {
    // Reconnect after interval if live mode still on
    const secs = parseFloat($('log-interval').value) || 1;
    if ($('log-live').checked) {
      logTimer = setTimeout(connectLogWs, secs * 1000);
    }
  };
  logWs.onerror = () => logWs.close();
}

function appendLog(line) {
  const box = $('log-box');
  const div = document.createElement('div');

  // Detect level for coloring
  let cls = 'log-INFO';
  if (line.includes('[DEBUG]'))  cls = 'log-DEBUG';
  else if (line.includes('[WARN]'))  cls = 'log-WARN';
  else if (line.includes('[ERROR]') || line.includes('[CRIT]') || line.includes('[EMERG]')) cls = 'log-ERROR';
  else if (line.includes('[NOTICE]')) cls = 'log-NOTICE';

  div.className = cls;
  div.textContent = line;
  box.appendChild(div);

  // Trim to 2000 lines
  while (box.childElementCount > 2000) box.removeChild(box.firstChild);

  if ($('log-scroll').checked) box.scrollTop = box.scrollHeight;
}

// ── Settings ───────────────────────────────────────────────────────────────
function setCfgStatus(elId, text, ok) {
  const el = $(elId);
  el.textContent = text;
  el.style.color = ok ? 'var(--ok)' : 'var(--err)';
}

function getTextPicker(pickerId) {
  return TEXT_PICKERS.find(picker => picker.id === pickerId) || null;
}

function closeTextPicker(picker) {
  $(picker.menuId).classList.remove('is-open');
  $(picker.toggleId).classList.remove('is-open');
  $(picker.toggleId).setAttribute('aria-expanded', 'false');
}

function closeAllTextPickers(exceptId = '') {
  TEXT_PICKERS.forEach(picker => {
    if (picker.id !== exceptId) closeTextPicker(picker);
  });
}

function openTextPicker(picker) {
  closeAllTextPickers(picker.id);
  syncTextPickerSelection(picker);
  $(picker.menuId).classList.add('is-open');
  $(picker.toggleId).classList.add('is-open');
  $(picker.toggleId).setAttribute('aria-expanded', 'true');
}

function toggleTextPicker(picker) {
  if ($(picker.menuId).classList.contains('is-open')) {
    closeTextPicker(picker);
  } else {
    openTextPicker(picker);
  }
}

function renderTextPickerOptions(picker) {
  const menu = $(picker.menuId);
  menu.innerHTML = picker.options.map(opt => (
    `<button class="field-picker-option" type="button" data-picker-id="${picker.id}" data-value="${opt.value.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;')}">${opt.label.replaceAll('&', '&amp;').replaceAll('<', '&lt;')}</button>`
  )).join('');
}

function syncTextPickerSelection(picker) {
  const currentValue = $(picker.inputId).value;
  document.querySelectorAll(`#${picker.menuId} .field-picker-option`).forEach(option => {
    const isSelected = option.dataset.value === currentValue;
    option.classList.toggle('is-selected', isSelected);
    option.setAttribute('aria-selected', isSelected ? 'true' : 'false');
  });
}

function getPickerOptions(picker) {
  return Array.from(document.querySelectorAll(`#${picker.menuId} .field-picker-option`));
}

function focusTextPickerOption(picker, index) {
  const options = getPickerOptions(picker);
  if (!options.length) return;
  const normalized = ((index % options.length) + options.length) % options.length;
  options[normalized].focus();
}

function selectTextPickerValue(picker, value) {
  $(picker.inputId).value = value;
  syncTextPickerSelection(picker);
  closeTextPicker(picker);
  $(picker.inputId).focus();
}

function bindTextPicker(picker) {
  renderTextPickerOptions(picker);
  syncTextPickerSelection(picker);

  $(picker.toggleId).addEventListener('click', () => toggleTextPicker(picker));

  $(picker.inputId).addEventListener('input', () => {
    syncTextPickerSelection(picker);
  });

  $(picker.inputId).addEventListener('keydown', event => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      openTextPicker(picker);
      const options = getPickerOptions(picker);
      const selectedIndex = options.findIndex(option => option.classList.contains('is-selected'));
      focusTextPickerOption(picker, selectedIndex >= 0 ? selectedIndex : 0);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      openTextPicker(picker);
      const options = getPickerOptions(picker);
      const selectedIndex = options.findIndex(option => option.classList.contains('is-selected'));
      focusTextPickerOption(picker, selectedIndex >= 0 ? selectedIndex : options.length - 1);
    } else if (event.key === 'Escape') {
      closeTextPicker(picker);
    }
  });

  getPickerOptions(picker).forEach((option, index) => {
    option.addEventListener('click', () => {
      selectTextPickerValue(picker, option.dataset.value || '');
    });

    option.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        focusTextPickerOption(picker, index + 1);
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        focusTextPickerOption(picker, index - 1);
      } else if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectTextPickerValue(picker, option.dataset.value || '');
      } else if (event.key === 'Escape') {
        event.preventDefault();
        closeTextPicker(picker);
        $(picker.inputId).focus();
      }
    });
  });
}

function bindSettingsUi() {
  $('cfg-mode-gui').addEventListener('change', () => {
    $('cfg-raw-pane').style.display = 'none';
    $('cfg-gui-pane').style.display = 'block';
    closeAllTextPickers();
  });

  $('cfg-mode-raw').addEventListener('change', () => {
    $('cfg-raw-pane').style.display = 'block';
    $('cfg-gui-pane').style.display = 'none';
    closeAllTextPickers();
  });

  $('config-save-raw-btn').addEventListener('click', saveRawConfig);
  $('config-save-gui-btn').addEventListener('click', saveGuiConfig);

  document.addEventListener('click', event => {
    TEXT_PICKERS.forEach(picker => {
      if (!$(picker.rootId).contains(event.target)) closeTextPicker(picker);
    });
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeAllTextPickers();
  });

  TEXT_PICKERS.forEach(bindTextPicker);
}

async function loadConfig() {
  const keys = GUI_LEVEL_FIELDS.map(field => field.key).join(',');
  const [data, raw, fields] = await Promise.all([
    apiGet('/api/config'),
    apiGet('/api/config/raw'),
    apiGet(`/api/config/fields?keys=${encodeURIComponent(keys)}`),
  ]);

  if (data) {
    $('config-pre').textContent = JSON.stringify(data, null, 2);
  }

  if (raw && typeof raw.text === 'string') {
    $('config-editor').value = raw.text;
  }

  if (fields && fields.fields) {
    GUI_LEVEL_FIELDS.forEach(field => {
      const val = fields.fields[field.key];
      if (typeof val === 'string') {
        $(field.inputId).value = val;
      } else if (field.allowNotDefined) {
        $(field.inputId).value = 'NOT DEFINED';
      } else {
        $(field.inputId).value = '';
      }

      const picker = getTextPicker(field.inputId);
      if (picker) syncTextPickerSelection(picker);
    });
  }
}

async function saveRawConfig() {
  setCfgStatus('config-save-raw-status', '', true);
  const res = await apiPost('/api/config/raw', {
    config_text: $('config-editor').value,
  });
  if (!res.ok) {
    setCfgStatus('config-save-raw-status', res.data.detail || 'Save failed', false);
    return;
  }
  setCfgStatus('config-save-raw-status', 'Saved', true);
  await loadConfig();
}

async function saveGuiConfig() {
  setCfgStatus('config-save-gui-status', '', true);

  const changes = [];
  for (const field of GUI_LEVEL_FIELDS) {
    const valueText = $(field.inputId).value.trim();
    if (field.allowNotDefined && (valueText === 'NOT DEFINED' || valueText === '')) {
      changes.push({ key: field.key, remove: true });
    } else {
      changes.push({ key: field.key, value_text: valueText });
    }
  }

  const res = await apiPost('/api/config/fields', { changes });
  if (!res.ok) {
    setCfgStatus('config-save-gui-status', res.data.detail || 'Save failed', false);
    return;
  }
  setCfgStatus('config-save-gui-status', 'Saved', true);
  await loadConfig();
}

// ── Boot ───────────────────────────────────────────────────────────────────
bindSettingsUi();

// Check for existing session before showing login screen
(async () => {
  const r = await fetch('/api/auth/me', { credentials: 'include' });
  if (r.ok) {
    onLoggedIn();
  } else {
    showScreen('login');
  }
})();
