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
      '"emerg"',
      '"alert"',
      '"crit"',
      '"error"',
      '"warn"',
      '"notice"',
      '"info"',
      '"debug"',
      '"${LOG_LEVEL:-info}"',
    ],
  },
];

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
  const data = await apiGet('/api/tasks');
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

  const status = await apiGet('/api/status');
  if (status) {
    $('stat-workers').textContent = Object.keys(status.workers).length;
  }
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
  menu.innerHTML = picker.options.map(value => (
    `<button class="field-picker-option" type="button" data-picker-id="${picker.id}" data-value="${value.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;')}">${value.replaceAll('&', '&amp;').replaceAll('<', '&lt;')}</button>`
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
  $('cfg-mode-raw').addEventListener('change', () => {
    $('cfg-raw-pane').style.display = 'block';
    $('cfg-gui-pane').style.display = 'none';
    closeAllTextPickers();
  });

  $('cfg-mode-gui').addEventListener('change', () => {
    $('cfg-raw-pane').style.display = 'none';
    $('cfg-gui-pane').style.display = 'block';
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
  const [data, raw, fields] = await Promise.all([
    apiGet('/api/config'),
    apiGet('/api/config/raw'),
    apiGet('/api/config/fields?keys=logging.level'),
  ]);

  if (data) {
    $('config-pre').textContent = JSON.stringify(data, null, 2);
  }

  if (raw && typeof raw.text === 'string') {
    $('config-editor').value = raw.text;
  }

  if (fields && fields.fields && typeof fields.fields['logging.level'] === 'string') {
    $('cfg-logging-level').value = fields.fields['logging.level'];
    const picker = getTextPicker('cfg-logging-level');
    if (picker) syncTextPickerSelection(picker);
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
  const changes = [
    {
      key: 'logging.level',
      value_text: $('cfg-logging-level').value,
    },
  ];
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
