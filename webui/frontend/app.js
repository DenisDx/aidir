/* AI Director – frontend application logic */
'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let token = '';
let logWs = null;
let logTimer = null;
let refreshTimer = null;
let runtimeState = {
  restart_requested: false,
  accepting_new_tasks: true,
  active_tasks: 0,
  restart_wait_timeout: 120,
};

const SERVER_UNAVAILABLE_STATUSES = new Set([502, 503, 504]);
let loginServerUnavailable = false;

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
  if (name === 'llm')      loadWorkersModels();
  if (name === 'mcp')      loadMcpEndpoints();
}

function setLoginServerState(isUnavailable, message = '') {
  loginServerUnavailable = isUnavailable;

  $('login-input').disabled = isUnavailable;
  $('pass-input').disabled = isUnavailable;
  $('login-btn').disabled = isUnavailable;

  if (isUnavailable) {
    $('pass-input').value = '';
    $('login-error').textContent = message || 'Server is unavailable. Please try again later.';
  }
}

// ── Auth ───────────────────────────────────────────────────────────────────
async function apiPost(url, body) {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
      credentials: 'include',
    });
    return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
  } catch (error) {
    return { ok: false, status: 0, data: { detail: error.message || 'Network error' } };
  }
}

async function apiGet(url) {
  try {
    const r = await fetch(url, { credentials: 'include' });
    if (r.status === 401) { doLogout(); return null; }
    return r.ok ? r.json().catch(() => null) : null;
  } catch {
    return null;
  }
}

$('login-btn').addEventListener('click', async () => {
  if (loginServerUnavailable) {
    $('login-error').textContent = 'Server is unavailable. Please try again later.';
    return;
  }

  $('login-error').textContent = '';
  const res = await apiPost('/api/auth/login', {
    login:    $('login-input').value,
    password: $('pass-input').value,
  });
  if (res.ok) {
    token = res.data.token || '';
    setLoginServerState(false);
    onLoggedIn();
  } else {
    if (SERVER_UNAVAILABLE_STATUSES.has(res.status) || res.status === 0) {
      setLoginServerState(true, 'Server is unavailable. Please try again later.');
      return;
    }
    $('login-error').textContent = res.data.detail || 'Invalid credentials';
  }
});

$('login-input').addEventListener('keydown', e => { if (e.key === 'Enter') $('login-btn').click(); });
$('pass-input').addEventListener('keydown',  e => { if (e.key === 'Enter') $('login-btn').click(); });

$('logoff-btn').addEventListener('click', async () => {
  await apiPost('/api/auth/logout', {});
  doLogout();
});

$('restart-btn').addEventListener('click', requestRestart);

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
  initTestPages();
  refreshTimer = setInterval(loadTasks, 3000);
}

function applyRuntimeStatus(runtime) {
  if (!runtime) return;

  runtimeState = {
    ...runtimeState,
    ...runtime,
  };

  const isBusy = !!runtimeState.restart_requested;
  const statusEl = $('runtime-status');
  const bannerEl = $('runtime-banner');
  const restartBtn = $('restart-btn');

  statusEl.textContent = isBusy
    ? `Restarting · active ${runtimeState.active_tasks}`
    : 'Ready';
  statusEl.classList.toggle('busy', isBusy);

  restartBtn.disabled = isBusy;
  restartBtn.textContent = isBusy ? 'Restarting...' : 'Restart';

  if (isBusy) {
    bannerEl.textContent = `System is busy restarting. New tasks are disabled while waiting up to ${runtimeState.restart_wait_timeout}s for ${runtimeState.active_tasks} active task(s) to finish.`;
    bannerEl.classList.add('visible');
  } else {
    bannerEl.textContent = '';
    bannerEl.classList.remove('visible');
  }

  // Restart drain card (dashboard)
  const restartCard = $('restart-card');
  if (restartCard) {
    restartCard.style.display = isBusy ? '' : 'none';
    if (isBusy) {
      $('restart-active-count').textContent = runtimeState.active_tasks;
      $('restart-timeout-val').textContent = `${runtimeState.restart_wait_timeout}s`;
    }
  }
}

async function requestRestart() {
  const confirmed = window.confirm(
    'Restart the service now? New external tasks will be blocked, queued external tasks will be canceled, and active tasks will be given time to finish before shutdown.'
  );
  if (!confirmed) return;

  $('restart-btn').disabled = true;

  const res = await apiPost('/api/restart', {});
  if (!res.ok) {
    $('restart-btn').disabled = false;
    window.alert(res.data.detail || 'Failed to request restart');
    return;
  }

  applyRuntimeStatus(res.data.runtime || { restart_requested: true });
}

// ── Tasks ──────────────────────────────────────────────────────────────────
async function loadTasks() {
  const [data, status] = await Promise.all([
    apiGet('/api/tasks'),
    apiGet('/api/status'),
  ]);
  if (status && status.runtime) {
    applyRuntimeStatus(status.runtime);
  }
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

// ── TEST LLM ───────────────────────────────────────────────────────────────

// Built-in echo_call tool definition included in chat requests when checked.
const ECHO_TOOL_DEF = {
  type: 'function',
  function: {
    name: 'echo_call',
    description: 'Test tool that echoes its arguments. Used to verify tool-calling works.',
    parameters: {
      type: 'object',
      properties: {
        message: { type: 'string', description: 'Any string to echo back' },
      },
    },
  },
};

let llmMessages = [];   // {role, content} history shown in dialog
let llmSending  = false;

async function loadWorkersModels() {
  const data = await apiGet('/api/workers/models');
  if (!data) return;

  const ws = $('llm-worker-select');
  ws.innerHTML = '<option value="">\u2014 select worker \u2014</option>';
  (data.workers || []).forEach(w => {
    const opt = document.createElement('option');
    opt.value = w.id;
    opt.textContent = `${w.id} (${w.type})`;
    if (!w.enabled) { opt.textContent += ' [disabled]'; opt.disabled = true; }
    ws.appendChild(opt);
  });

  const ms = $('llm-model-select');
  ms.innerHTML = '<option value="">\u2014 from config \u2014</option>';
  (data.providers || []).forEach(p => {
    if (!p.models || !p.models.length) return;
    const grp = document.createElement('optgroup');
    grp.label = p.id;
    p.models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.name || m.id;
      grp.appendChild(opt);
    });
    ms.appendChild(grp);
  });
}

function llmAppendMessage(role, content) {
  llmMessages.push({ role, content });
  renderLlmChat();
}

function renderLlmChat() {
  const box = $('llm-chat-box');
  box.innerHTML = '';
  llmMessages.forEach(m => {
    const isUser = m.role === 'user';
    const wrap = document.createElement('div');
    wrap.className = `chat-msg ${isUser ? 'chat-msg-user' : 'chat-msg-other'}`;
    const col = document.createElement('div');
    col.className = `chat-col${isUser ? ' chat-col-right' : ''}`;
    const label = document.createElement('div');
    label.className = 'chat-role';
    label.textContent = m.role;
    const bubble = document.createElement('div');
    const bClass = m.role === 'user' ? 'user' : m.role === 'tool' ? 'tool' : m.role === 'error' ? 'error' : 'assistant';
    bubble.className = `chat-bubble chat-bubble-${bClass}`;
    bubble.textContent = m.content;
    col.appendChild(label);
    col.appendChild(bubble);
    wrap.appendChild(col);
    box.appendChild(wrap);
  });
  box.scrollTop = box.scrollHeight;
}

// Build the request body from current UI state + optional extra user text.
// Pure function — no side effects on dialog or UI.
function buildLlmBody(extraUserText) {
  const worker = $('llm-worker-select').value || undefined;
  const model  = $('llm-model-input').value.trim() || undefined;
  const useEchoTool = $('llm-echo-tool-check').checked;

  const apiMessages = llmMessages
    .filter(m => ['user', 'assistant'].includes(m.role))
    .map(m => ({ role: m.role, content: m.content }));

  if (extraUserText) {
    apiMessages.push({ role: 'user', content: extraUserText });
  }

  const body = { messages: apiMessages, stream: false };
  if (model)       body.model  = model;
  if (worker)      body.worker = worker;
  if (useEchoTool) body.tools  = [ECHO_TOOL_DEF];
  return body;
}

// Handle response JSON received from /api/test/llm and update dialog.
async function handleLlmResponse(res, bodyUsed) {
  if (!res.ok) {
    const msg = res.data?.error?.message || res.data?.detail || 'Request failed';
    llmMessages.push({ role: 'error', content: `Error ${res.status}: ${msg}` });
    renderLlmChat();
    return false;
  }

  const assistantMsg = res.data.message || {};
  const content      = assistantMsg.content || '';
  const toolCalls    = assistantMsg.tool_calls || [];

  const useEchoTool = bodyUsed?.tools?.length > 0;
  if (useEchoTool && toolCalls.length) {
    const toolResults = [];
    toolCalls.forEach(tc => {
      const fn   = tc.function || {};
      const name = fn.name || '?';
      let args   = fn.arguments || '';
      if (typeof args === 'string') { try { args = JSON.parse(args); } catch { /**/ } }
      llmMessages.push({ role: 'tool', content: `[tool call] ${name}(\n${JSON.stringify(args, null, 2)}\n)` });
      toolResults.push({
        role:         'tool',
        content:      JSON.stringify({ called: name, args }),
        tool_call_id: tc.id || name,
        name,
      });
    });
    renderLlmChat();

    // Follow-up with tool results
    const followBody = {
      messages: [
        ...(bodyUsed.messages || []),
        { role: 'assistant', content: content || '', tool_calls: toolCalls },
        ...toolResults,
      ],
      stream: false,
    };
    if (bodyUsed.model)  followBody.model  = bodyUsed.model;
    if (bodyUsed.worker) followBody.worker = bodyUsed.worker;
    if (bodyUsed.tools)  followBody.tools  = bodyUsed.tools;

    const fr = await apiPost('/api/test/llm', followBody);
    if (fr.ok) {
      const fm = fr.data?.message || {};
      llmMessages.push({ role: 'assistant', content: fm.content || '' });
      renderLlmChat();
    }
  } else if (content) {
    llmMessages.push({ role: 'assistant', content });
    renderLlmChat();
  }

  const d = res.data;
  return d.done ? `Done \u00b7 eval ${d.eval_count || 0} tokens` : 'Done';
}

// Populate the "Query to send" panel without sending.
function showLlmQuery() {
  const text = $('llm-input').value.trim();
  const body = buildLlmBody(text || undefined);
  $('llm-query-editor').value = JSON.stringify(body, null, 2);
  $('llm-query-card').style.display = '';
  $('llm-query-status').textContent = '';
  $('llm-query-editor').scrollTop = 0;
}

async function sendLlmMessage() {
  if (llmSending) return;
  const text = $('llm-input').value.trim();
  if (!text) return;

  const worker = $('llm-worker-select').value || undefined;
  const model  = $('llm-model-input').value.trim() || undefined;
  if (!model && !worker) {
    $('llm-status').textContent = 'Select a worker or enter a model name.';
    return;
  }

  llmAppendMessage('user', text);
  $('llm-input').value = '';
  $('llm-status').textContent = 'Sending\u2026';
  $('llm-send-btn').disabled = true;
  llmSending = true;

  const body = buildLlmBody();   // user text already appended to llmMessages above

  try {
    const res = await apiPost('/api/test/llm', body);
    const info = await handleLlmResponse(res, body);
    $('llm-status').textContent = typeof info === 'string' ? info : (info ? 'Done' : 'Error.');
  } catch (err) {
    llmMessages.push({ role: 'error', content: String(err) });
    renderLlmChat();
    $('llm-status').textContent = 'Network error.';
  } finally {
    llmSending = false;
    $('llm-send-btn').disabled = false;
  }
}

// Send whatever JSON is in the query panel textarea.
async function sendLlmQueryFromPanel() {
  if (llmSending) return;
  let body;
  try {
    body = JSON.parse($('llm-query-editor').value);
  } catch {
    $('llm-query-status').textContent = 'Invalid JSON.';
    return;
  }

  $('llm-query-send-btn').disabled = true;
  $('llm-query-status').textContent = 'Sending\u2026';
  llmSending = true;
  $('llm-send-btn').disabled = true;

  try {
    const res = await apiPost('/api/test/llm', body);
    const info = await handleLlmResponse(res, body);
    $('llm-query-status').textContent = typeof info === 'string' ? info : (info ? 'Done' : 'Error.');
    $('llm-status').textContent = '';
  } catch (err) {
    llmMessages.push({ role: 'error', content: String(err) });
    renderLlmChat();
    $('llm-query-status').textContent = 'Network error.';
  } finally {
    llmSending = false;
    $('llm-query-send-btn').disabled = false;
    $('llm-send-btn').disabled = false;
  }
}


// ── TEST MCP ───────────────────────────────────────────────────────────────

let mcpEndpoints = [];
let mcpRequestId = 1;

async function loadMcpEndpoints() {
  const data = await apiGet('/api/endpoints/info');
  if (!data) return;

  mcpEndpoints = (data.endpoints || []).filter(e => e.api === 'mcp');
  const sel = $('mcp-endpoint-select');
  sel.innerHTML = '';
  if (!mcpEndpoints.length) {
    sel.innerHTML = '<option value="">No MCP endpoints configured</option>';
  } else {
    mcpEndpoints.forEach(ep => {
      const opt = document.createElement('option');
      opt.value = ep.id;
      opt.textContent = `${ep.id}  (:${ep.port || '?'})`;
      sel.appendChild(opt);
    });
  }
  renderMcpTools();
  updateMcpRequestTemplate();
}

function renderMcpTools() {
  const epId = $('mcp-endpoint-select').value;
  const ep   = mcpEndpoints.find(e => e.id === epId);
  const list = $('mcp-tools-list');
  if (!ep || !ep.tools || !ep.tools.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:13px">No tools defined for this endpoint</div>';
    return;
  }
  list.innerHTML = '';
  ep.tools.forEach(tool => {
    const row = document.createElement('div');
    row.className = 'mcp-tool-row';
    // Store tool name as data attribute to avoid HTML-escaping issues in onclick
    row.innerHTML = `
      <div>
        <div class="mcp-tool-name">${escapeHtml(tool.name)}</div>
        <div class="mcp-tool-desc">${escapeHtml(tool.description || '')}</div>
      </div>
      <button class="btn-sm" data-toolname="${escapeHtml(tool.name)}">Fill</button>
    `;
    row.querySelector('button').addEventListener('click', () => fillMcpTemplate(tool.name));
    list.appendChild(row);
  });
}

function fillMcpTemplate(toolName) {
  const epId = $('mcp-endpoint-select').value;
  const ep   = mcpEndpoints.find(e => e.id === epId);
  const tool = ep?.tools?.find(t => t.name === toolName);
  const schema = tool?.inputSchema || { type: 'object', properties: {} };
  const sampleArgs = {};
  Object.entries(schema.properties || {}).forEach(([key, def]) => {
    const t = def.type || 'string';
    sampleArgs[key] = t === 'number' || t === 'integer' ? 0
                    : t === 'boolean' ? false
                    : t === 'array'   ? []
                    : t === 'object'  ? {}
                    : `<${key}>`;
  });
  const rpc = { jsonrpc: '2.0', id: mcpRequestId++, method: 'tools/call',
                params: { name: toolName, arguments: sampleArgs } };
  $('mcp-method-select').value = 'tools/call';
  $('mcp-request-editor').value = JSON.stringify(rpc, null, 2);
}

function updateMcpRequestTemplate() {
  const method = $('mcp-method-select').value;
  let rpc;
  if (method === 'tools/list') {
    rpc = { jsonrpc: '2.0', id: mcpRequestId++, method: 'tools/list' };
  } else if (method === 'initialize') {
    rpc = { jsonrpc: '2.0', id: mcpRequestId++, method: 'initialize',
      params: { protocolVersion: '2024-11-05', clientInfo: { name: 'aidir-webui', version: '0.1.0' }, capabilities: {} } };
  } else if (method === 'ping') {
    rpc = { jsonrpc: '2.0', id: mcpRequestId++, method: 'ping' };
  } else {
    rpc = { jsonrpc: '2.0', id: mcpRequestId++, method: 'tools/call', params: { name: '', arguments: {} } };
  }
  $('mcp-request-editor').value = JSON.stringify(rpc, null, 2);
}

async function sendMcpRequest() {
  const epId = $('mcp-endpoint-select').value;
  if (!epId) { $('mcp-status').textContent = 'Select an endpoint first.'; return; }

  let body;
  try {
    body = JSON.parse($('mcp-request-editor').value);
  } catch {
    $('mcp-status').textContent = 'Invalid JSON.';
    return;
  }

  body._endpoint_id = epId;
  $('mcp-send-btn').disabled = true;
  $('mcp-status').textContent = 'Sending\u2026';
  $('mcp-result-box').textContent = '';

  const res = await apiPost('/api/test/mcp', body);
  $('mcp-send-btn').disabled = false;
  $('mcp-status').textContent = res.ok ? `OK ${res.status}` : `Error ${res.status}`;
  $('mcp-result-box').textContent = JSON.stringify(res.data, null, 2);
}

// ── Test pages init ────────────────────────────────────────────────────────

function initTestPages() {
  $('llm-send-btn').addEventListener('click', sendLlmMessage);
  $('llm-show-query-btn').addEventListener('click', showLlmQuery);
  $('llm-query-send-btn').addEventListener('click', sendLlmQueryFromPanel);
  $('llm-clear-btn').addEventListener('click', () => {
    llmMessages = [];
    renderLlmChat();
    $('llm-status').textContent = '';
    $('llm-query-card').style.display = 'none';
  });
  $('llm-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendLlmMessage(); }
  });
  $('llm-model-select').addEventListener('change', () => {
    const v = $('llm-model-select').value;
    if (v) $('llm-model-input').value = v;
  });

  $('mcp-send-btn').addEventListener('click', sendMcpRequest);
  $('mcp-endpoint-select').addEventListener('change', () => { renderMcpTools(); updateMcpRequestTemplate(); });
  $('mcp-method-select').addEventListener('change', updateMcpRequestTemplate);
}



// Check for existing session before showing login screen
(async () => {
  try {
    const r = await fetch('/api/auth/me', { credentials: 'include' });
    if (r.ok) {
      setLoginServerState(false);
      onLoggedIn();
      return;
    }

    showScreen('login');
    if (SERVER_UNAVAILABLE_STATUSES.has(r.status)) {
      setLoginServerState(true, 'Server is unavailable. Please try again later.');
      return;
    }

    setLoginServerState(false);
  } catch {
    showScreen('login');
    setLoginServerState(true, 'Server is unavailable. Please try again later.');
  }
})();
