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

const TASK_VIEWER_STATUSES = ['created', 'queued', 'running', 'completed', 'failed', 'canceled'];

let taskViewerMetaLoaded = false;
let taskViewerMetaLoading = null;
let taskViewerRows = [];
let taskViewerWorkersMeta = [];
let taskViewerUiBound = false;

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
  if (name === 'task-viewer') ensureTaskViewerMeta();
  if (name === 'llm')      loadWorkersModels();
  if (name === 'mcp')      loadMcpEndpoints();
  if (name === 'agent')    loadAgentEndpoints();
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
  ensureTaskViewerMeta();
  startLiveLogs();
  initTestPages();
  loadAgentCatalog();
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
    const canTerminate = ['created', 'queued', 'running'].includes(String(t.status || '').toLowerCase());
    tr.innerHTML = `
      <td style="font-family:monospace;font-size:11px">${t.id.slice(0, 8)}…</td>
      <td>${t.type}</td>
      <td><span class="badge badge-${t.status}">${t.status}</span></td>
      <td>${t.worker_id || '—'}</td>
      <td style="color:var(--muted);font-size:12px">${fmtTime(t.created_at)}</td>
      <td style="color:var(--muted);font-size:12px">${fmtTaskDuration(t.started_at)}</td>
      <td><button class="btn-sm" data-dashboard-json="${escapeHtml(t.id || '')}">Show JSON</button></td>
      <td>
        <div class="task-viewer-actions">
          <button class="btn-sm" data-dashboard-terminate="${escapeHtml(t.id || '')}" ${canTerminate ? '' : 'disabled'}>Terminate</button>
        </div>
      </td>
    `;
    tr.querySelector('[data-dashboard-json]').addEventListener('click', () => openTaskViewerJson(t.id));
    const terminateBtn = tr.querySelector('[data-dashboard-terminate]');
    if (terminateBtn) {
      terminateBtn.addEventListener('click', () => terminateDashboardTask(t.id, terminateBtn));
    }
    body.appendChild(tr);
  });

  if (status) {
    $('stat-workers').textContent = Object.keys(status.workers).length;
    renderResources(status.resources || []);
  }
}

async function terminateDashboardTask(taskId, buttonEl) {
  if (!taskId) return;

  const confirmed = window.confirm(`Terminate task ${taskId}?`);
  if (!confirmed) return;

  if (buttonEl) buttonEl.disabled = true;

  const res = await apiPost(`/api/tasks/${encodeURIComponent(taskId)}/terminate`, {});
  if (!res.ok) {
    if (buttonEl) buttonEl.disabled = false;
    window.alert(res.data.detail || 'Failed to terminate task');
    return;
  }

  await loadTasks();
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

function fmtDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString();
}

function fmtTaskDuration(startedAt) {
  if (!startedAt) return '—';

  const started = new Date(startedAt);
  if (Number.isNaN(started.getTime())) return '—';

  const elapsedMs = Date.now() - started.getTime();
  if (elapsedMs < 0) return '0s';

  const totalSeconds = Math.floor(elapsedMs / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }

  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

function normalizeTaskViewerDate(value) {
  if (!value) return '';
  const dt = new Date(value);
  return Number.isNaN(dt.getTime()) ? '' : dt.toISOString();
}

function getCheckedValues(containerId) {
  return Array.from(document.querySelectorAll(`#${containerId} input[type=checkbox]:checked`)).map(input => input.value);
}

function setAllTaskViewerChecks(containerId, checked) {
  document.querySelectorAll(`#${containerId} input[type=checkbox]`).forEach(input => {
    input.checked = checked;
  });
}

function renderTaskViewerChecklist(containerId, values, groupName) {
  const box = $(containerId);
  if (!box) return;

  box.innerHTML = values.map(value => {
    const label = typeof value === 'string' ? value : value.label;
    const itemValue = typeof value === 'string' ? value : value.value;
    const extra = typeof value === 'string' ? '' : (value.extra || '');
    return `
      <label>
        <input type="checkbox" name="${groupName}" value="${escapeHtml(itemValue)}" checked>
        <span>${escapeHtml(label)}${extra ? ` <span style="color:var(--muted)">${escapeHtml(extra)}</span>` : ''}</span>
      </label>
    `;
  }).join('');
}

function renderTaskViewerMeta(meta) {
  const statuses = Array.isArray(meta?.status_options) && meta.status_options.length ? meta.status_options : TASK_VIEWER_STATUSES;
  const workers = Array.isArray(meta?.workers) ? meta.workers : [];
  const envids = Array.isArray(meta?.envids) ? meta.envids : [];

  taskViewerWorkersMeta = workers.map(worker => ({
    id: String(worker.id || ''),
    task_type: String(worker.task_type || ''),
    enabled: !!worker.enabled,
  }));

  renderTaskViewerChecklist('task-viewer-status-list', statuses, 'task-viewer-status');

  const workerItems = taskViewerWorkersMeta.map(worker => ({
    label: worker.id,
    value: worker.id,
    extra: worker.enabled ? '' : '[disabled]',
  }));
  renderTaskViewerChecklist('task-viewer-worker-list', workerItems, 'task-viewer-worker');

  applyTaskViewerWorkerTypeFilter();

  const envidList = $('task-viewer-envid-list');
  if (envidList) {
    envidList.innerHTML = envids
      .map(envid => `<option value="${escapeHtml(String(envid))}"></option>`)
      .join('');
  }
}

async function ensureTaskViewerMeta() {
  if (taskViewerMetaLoaded) return true;
  if (taskViewerMetaLoading) return taskViewerMetaLoading;

  taskViewerMetaLoading = (async () => {
    const data = await apiGet('/api/tasks/viewer/meta');
    if (!data) return false;
    renderTaskViewerMeta(data);
    taskViewerMetaLoaded = true;
    return true;
  })();

  try {
    return await taskViewerMetaLoading;
  } finally {
    taskViewerMetaLoading = null;
  }
}

function applyTaskViewerWorkerTypeFilter() {
  const typeSelect = $('task-viewer-worker-type');
  if (!typeSelect) return;

  const selectedType = (typeSelect.value || 'all').trim();
  const wantedType = selectedType === 'all' ? '' : selectedType;
  const allowedTypes = wantedType === 'context' ? new Set(['context', 'context_builder']) : new Set([wantedType]);

  document.querySelectorAll('#task-viewer-worker-list input[type=checkbox]').forEach(input => {
    const workerId = input.value;
    const meta = taskViewerWorkersMeta.find(worker => worker.id === workerId);
    if (!meta) {
      input.checked = selectedType === 'all';
      return;
    }
    if (selectedType === 'all') {
      input.checked = true;
      return;
    }
    input.checked = allowedTypes.has(meta.task_type);
  });
}

function buildTaskViewerQuery() {
  const params = new URLSearchParams();

  getCheckedValues('task-viewer-status-list').forEach(value => params.append('status', value));
  getCheckedValues('task-viewer-worker-list').forEach(value => params.append('worker', value));

  const envid = $('task-viewer-envid').value.trim();
  if (envid) params.set('envid', envid);

  const createdFrom = normalizeTaskViewerDate($('task-viewer-created-from').value);
  const createdTo = normalizeTaskViewerDate($('task-viewer-created-to').value);
  const opFrom = normalizeTaskViewerDate($('task-viewer-op-from').value);
  const opTo = normalizeTaskViewerDate($('task-viewer-op-to').value);

  if (createdFrom) params.set('created_from', createdFrom);
  if (createdTo) params.set('created_to', createdTo);
  if (opFrom) params.set('last_operation_from', opFrom);
  if (opTo) params.set('last_operation_to', opTo);

  params.set('limit', '300');
  return params;
}

function renderTaskViewerRows(tasks) {
  const body = $('task-viewer-body');
  if (!body) return;

  body.innerHTML = '';
  if (!tasks.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="8" style="color:var(--muted)">No tasks match the current filters.</td>';
    body.appendChild(tr);
    return;
  }

  tasks.forEach(task => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td style="font-family:monospace;font-size:11px">${escapeHtml((task.id || '').slice(0, 8))}…</td>
      <td><span class="badge badge-${escapeHtml(task.status || 'created')}">${escapeHtml(task.status || 'created')}</span></td>
      <td>${escapeHtml(task.worker_id || '—')}</td>
      <td>${escapeHtml(task.envid || '—')}</td>
      <td>${escapeHtml(task.type || '—')}</td>
      <td style="color:var(--muted);font-size:12px">${escapeHtml(fmtDateTime(task.created_at))}</td>
      <td style="color:var(--muted);font-size:12px">${escapeHtml(fmtDateTime(task.last_operation_at))}</td>
      <td><button class="btn-sm" data-task-id="${escapeHtml(task.id || '')}">Show JSON</button></td>
    `;
    tr.querySelector('button').addEventListener('click', () => openTaskViewerJson(task.id));
    body.appendChild(tr);
  });
}

async function loadTaskViewerTasks() {
  if (!await ensureTaskViewerMeta()) return;

  const params = buildTaskViewerQuery();
  $('task-viewer-summary').textContent = 'Loading tasks…';

  const data = await apiGet(`/api/tasks/viewer/search?${params.toString()}`);
  if (!data) return;

  taskViewerRows = Array.isArray(data.tasks) ? data.tasks : [];
  renderTaskViewerRows(taskViewerRows);
  $('task-viewer-summary').textContent = `Showing ${taskViewerRows.length} of ${data.count ?? taskViewerRows.length} task(s).`;
}

function resetTaskViewerFilters() {
  $('task-viewer-created-from').value = '';
  $('task-viewer-created-to').value = '';
  $('task-viewer-op-from').value = '';
  $('task-viewer-op-to').value = '';
  $('task-viewer-envid').value = '';
  $('task-viewer-worker-type').value = 'all';
  setAllTaskViewerChecks('task-viewer-status-list', true);
  setAllTaskViewerChecks('task-viewer-worker-list', true);
  $('task-viewer-summary').textContent = 'Filters cleared.';
}

function openTaskViewerModal(task) {
  const modal = $('task-json-modal');
  $('task-json-modal-title').textContent = `Task ${task.id || ''}`;
  $('task-json-modal-subtitle').textContent = `${task.status || 'created'} · ${task.worker_id || 'no worker'}${task.envid ? ` · envid ${task.envid}` : ''}`;
  $('task-json-modal-body').textContent = JSON.stringify(task, null, 2);
  modal.classList.add('is-open');
  modal.setAttribute('aria-hidden', 'false');
}

function closeTaskViewerModal() {
  const modal = $('task-json-modal');
  if (!modal) return;
  modal.classList.remove('is-open');
  modal.setAttribute('aria-hidden', 'true');
}

async function openTaskViewerJson(taskId) {
  if (!taskId) return;
  const data = await apiGet(`/api/tasks/viewer/${encodeURIComponent(taskId)}`);
  if (!data || !data.task) {
    window.alert('Task not found');
    return;
  }
  openTaskViewerModal(data.task);
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

function bindTaskViewerUi() {
  if (taskViewerUiBound) return;
  taskViewerUiBound = true;

  $('task-viewer-show-btn').addEventListener('click', loadTaskViewerTasks);
  $('task-viewer-clear-btn').addEventListener('click', resetTaskViewerFilters);
  $('task-viewer-worker-type').addEventListener('change', applyTaskViewerWorkerTypeFilter);
  $('task-json-modal-close').addEventListener('click', closeTaskViewerModal);
  $('task-json-modal-backdrop').addEventListener('click', closeTaskViewerModal);

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && $('task-json-modal').classList.contains('is-open')) {
      closeTaskViewerModal();
    }
  });
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
bindTaskViewerUi();

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

  const msg = res.data?.message || {};
  const content = msg.content || '';
  const toolCalls = Array.isArray(msg.tool_calls) ? msg.tool_calls : [];

  if (toolCalls.length) {
    llmMessages.push({ role: 'assistant', content: content || '[tool calls]' });

    const toolResults = [];
    toolCalls.forEach(tc => {
      const name = tc?.function?.name || tc?.name || 'unknown_tool';
      let args = {};
      try {
        args = JSON.parse(tc?.function?.arguments || '{}');
      } catch {
        args = { _raw: tc?.function?.arguments || '' };
      }

      llmMessages.push({
        role: 'tool',
        content: JSON.stringify({ called: name, args }),
        tool_call_id: tc.id || name,
        name,
      });

      toolResults.push({
        role: 'tool',
        content: JSON.stringify({ called: name, args }),
        tool_call_id: tc.id || name,
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

  $('agent-send-btn').addEventListener('click', sendAgentRequest);
  $('agent-show-query-btn').addEventListener('click', showAgentQuery);
  $('agent-query-send-btn').addEventListener('click', sendAgentQueryFromPanel);
  $('agent-provider-select').addEventListener('change', renderAgentModelsForProvider);
  $('agent-model-select').addEventListener('change', () => {
    const value = $('agent-model-select').value;
    if (value) $('agent-model-input').value = value;
  });
  $('agent-envid-select').addEventListener('change', () => {
    const value = $('agent-envid-select').value;
    if (value) $('agent-envid-input').value = value;
  });
  $('agent-clear-btn').addEventListener('click', () => {
    agentMessages = [];
    renderAgentChat();
    renderAgentRawResponse(null);
    $('agent-status').textContent = '';
    $('agent-query-card').style.display = 'none';
  });
  $('agent-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendAgentRequest(); }
  });

}

// ── Agent Request ──────────────────────────────────────────────────────────

let agentMessages = [];
let agentSending  = false;
let agentEndpoints = [];
let agentProviders = [];
let agentEnvids = [];

async function loadAgentCatalog() {
  const data = await apiGet('/api/test/agent/catalog');
  if (!data || !Array.isArray(data.providers)) return;
  agentProviders = data.providers;
  agentEnvids = Array.isArray(data.envids) ? data.envids : [];
  renderAgentProviderSelect();
  renderAgentEnvidSelect();
}

function renderAgentEnvidSelect() {
  const envidSel = $('agent-envid-select');
  if (!envidSel) return;

  const currentSelect = envidSel.value;
  const currentInput = $('agent-envid-input').value.trim();

  envidSel.innerHTML = '<option value="">-- from config --</option>';
  agentEnvids.forEach(envid => {
    const opt = document.createElement('option');
    opt.value = envid;
    opt.textContent = envid;
    envidSel.appendChild(opt);
  });

  if (currentSelect && agentEnvids.includes(currentSelect)) {
    envidSel.value = currentSelect;
    return;
  }

  if (currentInput && agentEnvids.includes(currentInput)) {
    envidSel.value = currentInput;
  }
}

function renderAgentProviderSelect() {
  const providerSel = $('agent-provider-select');
  const current = providerSel.value;

  providerSel.innerHTML = '<option value="">-- do not set --</option>';
  agentProviders.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.id;
    providerSel.appendChild(opt);
  });

  if (current && agentProviders.some(p => p.id === current)) {
    providerSel.value = current;
  }

  renderAgentModelsForProvider();
}

function renderAgentModelsForProvider() {
  const providerId = $('agent-provider-select').value;
  const modelSelect = $('agent-model-select');
  const previous = modelSelect.value;

  modelSelect.innerHTML = '<option value="">— from provider —</option>';
  if (!providerId) return;

  const provider = agentProviders.find(p => p.id === providerId);
  const models = Array.isArray(provider?.models) ? provider.models : [];

  models.forEach(modelId => {
    const opt = document.createElement('option');
    opt.value = modelId;
    opt.textContent = modelId;
    modelSelect.appendChild(opt);
  });

  if (previous && models.includes(previous)) {
    modelSelect.value = previous;
    return;
  }

  if (!$('agent-model-input').value.trim() && models.length) {
    modelSelect.value = models[0];
    $('agent-model-input').value = models[0];
  }
}

async function loadAgentEndpoints() {
  if (!agentProviders.length) await loadAgentCatalog();

  const data = await apiGet('/api/test/agent/endpoints');
  if (!data) return;

  const sel = $('agent-endpoint-select');
  sel.innerHTML = '';
  const endpoints = data.endpoints || [];
  agentEndpoints = endpoints;
  if (!endpoints.length) {
    sel.innerHTML = '<option value="">No endpoints configured</option>';
    return;
  }
  endpoints.forEach(ep => {
    const opt = document.createElement('option');
    opt.value = ep.id;
    opt.textContent = `${ep.id}  [${ep.api}  :${ep.port || '?'}]`;
    sel.appendChild(opt);
  });
}

// Build request body from current UI state. user text already in agentMessages if extraUserText is undefined.
function buildAgentBody(extraUserText) {
  const epId      = $('agent-endpoint-select').value;
  const protocol  = $('agent-protocol-select').value;
  const modelText = $('agent-model-input').value.trim();
  const modelPick = $('agent-model-select').value;
  const model     = modelText || modelPick || undefined;
  const provider  = $('agent-provider-select').value || undefined;
  const userTok   = $('agent-token-input').value.trim() || undefined;
  const envid     = $('agent-envid-input').value.trim() || undefined;
  const setTools  = $('agent-set-tools-check').checked;
  const toolsTxt  = $('agent-tools-input').value.trim();
  const sendCtx   = $('agent-ctx-check').checked;
  const ctxWorker = $('agent-ctx-worker-input').value.trim();
  const logSave   = $('agent-log-save-check').checked;
  const logTypes  = $('agent-log-types-input').value.trim();
  const keepHistory = $('agent-keep-history-check').checked;
  const timeoutVal = $('agent-timeout-input').value.trim();

  let messages = [];
  if (keepHistory) {
    messages = agentMessages
      .filter(m => ['user', 'assistant'].includes(m.role))
      .map(m => ({ role: m.role, content: m.content }));
    if (extraUserText) messages.push({ role: 'user', content: extraUserText });
  } else {
    const latestUserText = extraUserText || [...agentMessages].reverse().find(m => m.role === 'user')?.content || '';
    if (latestUserText) {
      messages = [{ role: 'user', content: latestUserText }];
    }
  }

  const body = {
    messages,
    stream: false,
    _endpoint_id: epId,
    _protocol: protocol,
  };

  if (model)   body.model = model;
  if (provider) body.provider = provider;
  if (userTok) body._user_token = userTok;
  if (envid)   body.envid = envid;

  if (sendCtx) {
    body.context_builder = {};
    if (ctxWorker) body.context_builder.worker = ctxWorker;

    if (setTools) {
      if (!toolsTxt) {
        body.context_builder.tools_to_inject = [];
      } else if (toolsTxt === '*' || toolsTxt.toLowerCase() === 'all') {
        body.context_builder.tools_to_inject = [toolsTxt];
      } else {
        body.context_builder.tools_to_inject = toolsTxt.split(',').map(s => s.trim()).filter(Boolean);
      }
    }
  }

  // Build log field when any log option is set
  const logField = {};
  if (logSave) logField.options = { save_llm_request: true };
  if (logTypes) {
    logTypes.split(',').forEach(pair => {
      const [k, v] = pair.split('=').map(s => s.trim());
      if (k && v) logField[k] = v;
    });
  }
  if (Object.keys(logField).length) body.log = logField;

  // Add timeout if specified (prefixed with _ for backend filtering)
  if (timeoutVal) {
    body._timeout = parseInt(timeoutVal, 10) || undefined;
  }

  return body;
}

function renderAgentRawResponse(res) {
  const statusEl = $('agent-raw-status');
  const boxEl = $('agent-raw-box');
  if (!res) {
    statusEl.textContent = 'No requests yet.';
    boxEl.textContent = '';
    return;
  }

  const payload = res.data === undefined ? null : res.data;
  statusEl.textContent = `HTTP ${res.status} ${res.ok ? 'OK' : 'ERROR'}`;
  boxEl.textContent = JSON.stringify(payload, null, 2);
}

function renderAgentChat() {
  const box = $('agent-chat-box');
  box.innerHTML = '';
  agentMessages.forEach(m => {
    const isUser = m.role === 'user';
    const wrap = document.createElement('div');
    wrap.className = `chat-msg ${isUser ? 'chat-msg-user' : 'chat-msg-other'}`;
    const col = document.createElement('div');
    col.className = `chat-col${isUser ? ' chat-col-right' : ''}`;
    const label = document.createElement('div');
    label.className = 'chat-role';
    label.textContent = m.role;
    const bubble = document.createElement('div');
    const bClass = m.role === 'user' ? 'user' : m.role === 'error' ? 'error' : 'assistant';
    bubble.className = `chat-bubble chat-bubble-${bClass}`;
    bubble.textContent = m.content;
    col.appendChild(label);
    col.appendChild(bubble);
    wrap.appendChild(col);
    box.appendChild(wrap);
  });
  box.scrollTop = box.scrollHeight;
}

// Extract response content from response data, handling ollama/openai/anthropic formats.
function handleAgentResponse(res) {
  if (!res.ok) {
    const msg = res.data?.error?.message || res.data?.detail || 'Request failed';
    agentMessages.push({ role: 'error', content: `Error ${res.status}: ${msg}` });
    renderAgentChat();
    return false;
  }

  const data = res.data;
  let content = '';

  if (data.choices) {
    // OpenAI format
    content = data.choices[0]?.message?.content || '';
  } else if (data.message) {
    // Ollama format
    content = data.message.content || '';
  } else if (data.content) {
    // Anthropic format
    const block = Array.isArray(data.content) ? data.content[0] : data.content;
    content = typeof block === 'string' ? block : (block?.text || JSON.stringify(data.content));
  } else {
    content = JSON.stringify(data, null, 2);
  }

  agentMessages.push({ role: 'assistant', content });
  renderAgentChat();
  return true;
}

function showAgentQuery() {
  const text = $('agent-input').value.trim();
  const body = buildAgentBody(text || undefined);
  $('agent-query-editor').value = JSON.stringify(body, null, 2);
  $('agent-query-card').style.display = '';
  $('agent-query-status').textContent = '';
}

async function sendAgentRequest() {
  if (agentSending) return;
  const text = $('agent-input').value.trim();
  const epId = $('agent-endpoint-select').value;
  if (!epId) { $('agent-status').textContent = 'Select an endpoint.'; return; }
  if (!text) return;

  agentMessages.push({ role: 'user', content: text });
  renderAgentChat();
  $('agent-input').value = '';
  $('agent-status').textContent = 'Sending\u2026';
  $('agent-send-btn').disabled = true;
  agentSending = true;

  const body = buildAgentBody();  // user text already pushed to agentMessages above

  try {
    const res = await apiPost('/api/test/agent', body);
    renderAgentRawResponse(res);
    const ok  = handleAgentResponse(res);
    $('agent-status').textContent = ok ? `Done (${res.status})` : `Error (${res.status})`;
  } catch (err) {
    agentMessages.push({ role: 'error', content: String(err) });
    renderAgentChat();
    renderAgentRawResponse({ ok: false, status: 0, data: { error: { message: String(err) } } });
    $('agent-status').textContent = 'Network error.';
  } finally {
    agentSending = false;
    $('agent-send-btn').disabled = false;
  }
}

async function sendAgentQueryFromPanel() {
  if (agentSending) return;
  let body;
  try {
    body = JSON.parse($('agent-query-editor').value);
  } catch {
    $('agent-query-status').textContent = 'Invalid JSON.';
    return;
  }

  $('agent-query-send-btn').disabled = true;
  $('agent-query-status').textContent = 'Sending\u2026';
  agentSending = true;
  $('agent-send-btn').disabled = true;

  try {
    const res = await apiPost('/api/test/agent', body);
    renderAgentRawResponse(res);
    const ok  = handleAgentResponse(res);
    $('agent-query-status').textContent = ok ? `Done (${res.status})` : `Error (${res.status})`;
  } catch (err) {
    agentMessages.push({ role: 'error', content: String(err) });
    renderAgentChat();
    renderAgentRawResponse({ ok: false, status: 0, data: { error: { message: String(err) } } });
    $('agent-query-status').textContent = 'Network error.';
  } finally {
    agentSending = false;
    $('agent-query-send-btn').disabled = false;
    $('agent-send-btn').disabled = false;
  }
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
