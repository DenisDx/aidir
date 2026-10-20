/* AI Director – frontend application logic */
'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let token = '';
let logWs = null;
let logTimer = null;
let logWsGeneration = 0;
let logLiveUiBound = false;
let refreshTimer = null;
let tasksLoadInFlight = false;
let runtimeState = {
  restart_requested: false,
  accepting_new_tasks: true,
  active_tasks: 0,
  restart_wait_timeout: 120,
};
let cronHealth = null;

const SERVER_UNAVAILABLE_STATUSES = new Set([502, 503, 504]);
let loginServerUnavailable = false;

const TASK_VIEWER_STATUSES = ['created', 'queued', 'running', 'completed', 'failed', 'canceled'];

let taskViewerMetaLoaded = false;
let taskViewerMetaLoading = null;
let taskViewerRows = [];
let taskViewerWorkersMeta = [];
let taskViewerUiBound = false;
let taskViewerModalCopyText = '';
let taskViewerModalJsonValue = null;
let taskViewerModalJsonText = '';
let taskViewerModalJsonMode = 'tree';
let taskViewerModalBackAction = null;

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

  if (name === 'settings') { initSettingsBackend(); sizeSettingsWorkspace(); }
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

async function apiDelete(url) {
  try {
    const r = await fetch(url, { method: 'DELETE', credentials: 'include' });
    return { ok: r.ok, status: r.status, data: await r.json().catch(() => ({})) };
  } catch (error) {
    return { ok: false, status: 0, data: { detail: error.message || 'Network error' } };
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
$('repair-cron-btn').addEventListener('click', repairCron);

function doLogout() {
  token = '';
  stopLiveLogs();
  clearInterval(refreshTimer);
  resetSettingsEditor();
  showScreen('login');
}

function resetSettingsEditor() {
  // The next sign-in may be a different account, so the editor and its document
  // list are thrown away rather than reused.
  if (settingsEditor) settingsEditor.destroy();
  settingsEditor = null;
  settingsEditorDocument = '';
  settingsDocuments = [];
  settingsReady = false;
  $('cfg-document').innerHTML = '';
  $('settings-tree').innerHTML = '';
}

function onLoggedIn() {
  showScreen('app');
  showPage('dashboard');
  initSettingsBackend();
  bindChatUi();
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

function fmtAge(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  if (total < 60) return `${total}s ago`;
  const minutes = Math.floor(total / 60);
  const remainingSeconds = total % 60;
  return `${minutes}m ${remainingSeconds}s ago`;
}

function renderSystemHealth(health) {
  const card = $('system-health-card');
  const indicator = $('health-cron-indicator');
  const detail = $('health-cron-detail');
  if (!card || !indicator || !detail) return;

  const cron = health && health.cron;
  cronHealth = cron || null;
  const repairButton = $('repair-cron-btn');
  const embeddedStatus = $('embedded-cron-status');
  const isHealthy = Boolean(cron && cron.healthy);
  card.classList.toggle('alert', !isHealthy);
  indicator.classList.toggle('alert', !isHealthy);

  if (repairButton && embeddedStatus) {
    if (cron && cron.mode === 'embedded') {
      repairButton.hidden = true;
      embeddedStatus.hidden = false;
    } else {
      repairButton.hidden = false;
      embeddedStatus.hidden = true;
      if (cron && !cron.external_available) {
        repairButton.textContent = 'Enable cron.embedded';
        repairButton.title = 'Enable the embedded cron scheduler after restart';
      } else {
        repairButton.textContent = 'Repair cron';
        repairButton.title = 'Add or repair the aidir cron entry';
      }
    }
  }

  if (!cron || !cron.last_success_at) {
    detail.textContent = cron && cron.mode === 'embedded'
      ? (cron.embedded_active ? 'Embedded scheduler is running' : 'Restart required to start embedded scheduler')
      : 'No successful cycle recorded';
    return;
  }

  const threshold = Number(cron.max_age_seconds) || 180;
  const timestamp = fmtDateTime(cron.last_success_at);
  const age = fmtAge(cron.age_seconds);
  detail.textContent = isHealthy
    ? `Last success ${timestamp} (${age})`
    : `Last success ${timestamp} (${age}; threshold ${threshold}s)`;
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

async function repairCron() {
  if (cronHealth && !cronHealth.external_available) {
    const confirmed = window.confirm(
      'Enable the embedded cron scheduler? The service must be restarted before it starts running.'
    );
    if (!confirmed) return;

    const button = $('repair-cron-btn');
    button.disabled = true;
    const res = await apiPost('/api/cron/embedded/enable', {});
    button.disabled = false;

    if (!res.ok) {
      window.alert(res.data.detail || 'Failed to enable embedded cron');
      return;
    }

    window.alert('Embedded cron has been enabled. Restart the service to apply it.');
    await loadTasks();
    return;
  }

  const confirmed = window.confirm(
    'Repair the aidir cron entry? Existing unrelated crontab entries will be preserved.'
  );
  if (!confirmed) return;

  const button = $('repair-cron-btn');
  button.disabled = true;
  const res = await apiPost('/api/cron/repair', {});
  button.disabled = false;

  if (!res.ok) {
    window.alert(res.data.detail || 'Cron repair failed');
    return;
  }

  const action = String(res.data.action || 'completed');
  window.alert(`Cron repair ${action}`);
  await loadTasks();
}

// ── Tasks ──────────────────────────────────────────────────────────────────
// Refresh dashboard tasks without overlapping Redis scans.
async function loadTasks() {
  if (tasksLoadInFlight) return;
  tasksLoadInFlight = true;

  try {
    const [data, status] = await Promise.all([
    apiGet('/api/tasks'),
    apiGet('/api/status'),
  ]);
  if (status && status.runtime) {
    applyRuntimeStatus(status.runtime);
  }
  if (status) {
    renderSystemHealth(status.health);
  }
  if (!data) return;

  $('stat-tasks').textContent = data.tasks.length;

  const body = $('tasks-body');
  body.innerHTML = '';
  data.tasks.forEach(t => {
    const tr = document.createElement('tr');
    const canTerminate = ['created', 'queued', 'running'].includes(String(t.status || '').toLowerCase());
    const timeoutField = t.status === 'queued' ? 'queue_timeout' : t.status === 'running' ? 'run_timeout' : '';
    const timeoutValue = timeoutField ? Number(t[timeoutField]) : NaN;
    const timeoutText = Number.isFinite(timeoutValue) ? `${timeoutValue}s` : '—';
    const requestPreview = escapeHtml(firstMessagePreview(t));
    tr.innerHTML = `
      <td style="font-family:monospace;font-size:11px">${escapeHtml(t.id || '')}</td>
      <td>${t.type}</td>
      <td><span class="badge badge-${t.status}">${t.status}</span></td>
      <td>${t.worker_id || '—'}</td>
      <td style="color:var(--muted);font-size:12px">${fmtTime(t.created_at)}</td>
      <td style="color:var(--muted);font-size:12px">${fmtTaskDuration(t.started_at)}</td>
      <td style="white-space:nowrap">${timeoutText} ${timeoutField ? `<button class="btn-sm" data-dashboard-extend-timeout="${escapeHtml(t.id || '')}">add</button>` : ''}</td>
      <td style="color:var(--muted);font-size:12px">${requestPreview}</td>
      <td>${Number(t.llm_call_count || 0)}</td>
      <td>
        <div class="task-viewer-actions">
          <button class="btn-sm" data-dashboard-json="${escapeHtml(t.id || '')}">Show JSON</button>
          <button class="btn-sm" data-dashboard-steps="${escapeHtml(t.id || '')}">Show steps</button>
        </div>
      </td>
      <td>
        <div class="task-viewer-actions">
          <button class="btn-sm" data-dashboard-terminate="${escapeHtml(t.id || '')}" ${canTerminate ? '' : 'disabled'}>Terminate</button>
        </div>
      </td>
    `;
    tr.querySelector('[data-dashboard-json]').addEventListener('click', () => openTaskViewerJson(t.id));
    tr.querySelector('[data-dashboard-steps]').addEventListener('click', () => openTaskViewerSteps(t));
    const terminateBtn = tr.querySelector('[data-dashboard-terminate]');
    if (terminateBtn) {
      terminateBtn.addEventListener('click', () => terminateDashboardTask(t.id, terminateBtn));
    }
    const extendTimeoutBtn = tr.querySelector('[data-dashboard-extend-timeout]');
    if (extendTimeoutBtn) {
      extendTimeoutBtn.addEventListener('click', () => extendDashboardTaskTimeout(t.id, extendTimeoutBtn));
    }
    body.appendChild(tr);
  });

  if (status) {
    $('stat-workers').textContent = Object.keys(status.workers).length;
    renderResources(status.resources || []);
  }
  } finally {
    tasksLoadInFlight = false;
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

async function extendDashboardTaskTimeout(taskId, buttonEl) {
  if (!taskId) return;

  if (buttonEl) buttonEl.disabled = true;

  const res = await apiPost(`/api/tasks/${encodeURIComponent(taskId)}/extend-timeout`, {});
  if (!res.ok) {
    if (buttonEl) buttonEl.disabled = false;
    window.alert(res.data.detail || 'Failed to extend task timeout');
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
    tr.innerHTML = '<td colspan="8" style="color:var(--muted)">No resources configured</td>';
    body.appendChild(tr);
    return;
  }

  resources.forEach(r => {
    const usageParts = Object.keys(r.limits || {}).map(key => {
      const used = Number((r.used || {})[key] || 0);
      const softUsed = Number((r.soft_used || {})[key] || 0);
      const limit = Number((r.limits || {})[key] || 0);
      return `${key}: ${used + softUsed}/${limit}${softUsed ? ` (${softUsed} idle)` : ''}`;
    });

    const consumers = (r.consumers || []).map(c => {
      const details = Object.entries(c.usage || {})
        .map(([k, v]) => `${k}:${v}`)
        .join(', ');
      return `<div style="margin-bottom:2px"><span style="font-family:monospace">${escapeHtml(c.id)}</span> <span style="color:var(--muted)">${escapeHtml(details)}</span></div>`;
    }).join('');

    const softConsumers = (r.soft_consumers || []).map(c => {
      const details = Object.entries(c.resources || {})
        .map(([k, v]) => `${k}:${v}`)
        .join(', ');
      const state = c.persistent ? 'loaded' : `idle ${c.expires_in}s`;
      return `<div style="margin-bottom:2px"><span style="font-family:monospace">${escapeHtml(c.provider_id || 'provider')}/${escapeHtml(c.model_id)}</span> <span style="color:var(--muted)">${escapeHtml(details)} (${escapeHtml(state)})</span></div>`;
    }).join('');

    const startupErrors = (r.startup_errors || []).map(error => {
      const providerModel = `${error.provider_id || 'provider'}/${error.model_id || 'model'}`;
      const detail = `${error.code || 'LLAMA_CPP_START_FAILED'}: ${error.message || 'llama.cpp startup failed'}`;
      return `<div style="margin-bottom:2px;color:var(--red)"><span style="font-family:monospace">${escapeHtml(providerModel)}</span> ${escapeHtml(detail)}</div>`;
    }).join('');

    const sensors = ((r.monitoring || {}).sensors || []).map(sensor => {
      const unit = sensor.unit ? ` ${sensor.unit}` : '';
      const value = sensor.value == null ? '—' : `${sensor.value}${unit}`;
      const threshold = sensor.threshold && sensor.threshold.value != null
        ? ` (${sensor.threshold.operator || 'above'} ${sensor.threshold.value}${unit})`
        : '';
      const color = sensor.status === 'alert' ? 'var(--red)' : sensor.status === 'error' ? 'var(--yellow)' : 'var(--muted)';
      const detail = sensor.error ? `: ${sensor.error}` : threshold;
      return `<div style="margin-bottom:2px;color:${color}"><span>${escapeHtml(sensor.label || sensor.id || 'sensor')}</span> <strong>${escapeHtml(value)}</strong><span style="color:var(--muted)">${escapeHtml(detail)}</span></div>`;
    }).join('');

    const availability = r.availability || {};
    const availableValues = Object.entries(availability.available || {}).map(([metric, value]) => {
      const unit = ((r.units || {})[metric] || '');
      return `${metric}: ${value}${unit ? ` ${unit}` : ''}`;
    }).join(' | ') || '—';
    const availabilityStatus = availability.status || 'pending';
    const availabilityColor = availabilityStatus === 'error' ? 'var(--yellow)' : availabilityStatus === 'ok' ? 'var(--ok)' : 'var(--muted)';
    const availabilityError = availability.error ? `<div style="color:var(--yellow)">${escapeHtml(availability.error)}</div>` : '';
    const telemetry = ((r.telemetry || {}).sensors || []).map(reading => {
      const unit = reading.unit ? ` ${reading.unit}` : '';
      const value = reading.value == null ? '—' : `${reading.value}${unit}`;
      const color = reading.status === 'error' ? 'var(--yellow)' : 'var(--muted)';
      const error = reading.error ? `: ${reading.error}` : '';
      return `<div style="margin-bottom:2px;color:${color}"><span>${escapeHtml(reading.label || reading.id || 'sensor')}</span> <strong>${escapeHtml(value)}</strong><span style="color:var(--muted)">${escapeHtml(error)}</span></div>`;
    }).join('');
    const consumersCell = consumers + softConsumers + startupErrors || '<span style="color:var(--muted)">—</span>';
    const sensorsCell = sensors + telemetry || '<span style="color:var(--muted)">—</span>';

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHtml(r.id || '—')}</td>
      <td>${escapeHtml(r.type || '—')}</td>
      <td><input type="checkbox" data-resource-use ${r.use !== false ? 'checked' : ''} aria-label="Use resource ${escapeHtml(r.id || '')}"></td>
      <td>${escapeHtml(usageParts.join(' | ') || '—')}</td>
      <td><div style="color:${availabilityColor}">${escapeHtml(availability.source || 'estimated')} / ${escapeHtml(availabilityStatus)}: <strong>${escapeHtml(availableValues)}</strong></div><div style="color:var(--muted)">${escapeHtml(fmtDateTime(availability.observed_at))}</div>${availabilityError}</td>
      <td>${consumersCell}</td>
      <td>${sensorsCell}</td>
      <td><button class="btn-sm" data-resource-force-release>Force release</button></td>
    `;
    const useCheckbox = tr.querySelector('[data-resource-use]');
    useCheckbox.addEventListener('change', () => setResourceUse(r.id, useCheckbox));
    tr.querySelector('[data-resource-force-release]').addEventListener('click', () => forceReleaseResource(r.id));
    body.appendChild(tr);
  });
}

async function setResourceUse(resourceId, checkbox) {
  if (!resourceId || !checkbox) return;

  const use = checkbox.checked;
  checkbox.disabled = true;
  const res = await apiPost(`/api/resources/${encodeURIComponent(resourceId)}/use`, { use });
  if (!res.ok) {
    checkbox.checked = !use;
    window.alert(res.data.detail || 'Failed to update resource use');
  }
  await loadTasks();
}

async function forceReleaseResource(resourceId) {
  if (!resourceId) return;

  const confirmed = window.confirm(`Force release idle models from resource ${resourceId}? Active tasks will not be interrupted.`);
  if (!confirmed) return;

  const res = await apiPost(`/api/resources/${encodeURIComponent(resourceId)}/force-release`, {});
  if (!res.ok) {
    window.alert(res.data.detail || 'Failed to force release resource');
    return;
  }

  const models = res.data.unloaded_models || [];
  window.alert(models.length ? `Released: ${models.join(', ')}` : 'Resource is already free');
  await loadTasks();
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

function fmtTaskDuration(startedAt, finishedAt = null) {
  if (!startedAt) return '—';

  const started = new Date(startedAt);
  if (Number.isNaN(started.getTime())) return '—';

  const finished = finishedAt ? new Date(finishedAt) : null;
  const endMs = finished && !Number.isNaN(finished.getTime()) ? finished.getTime() : Date.now();
  const elapsedMs = endMs - started.getTime();
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

// Extract text while ignoring image and other media-only content fields.
function requestPreviewText(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return value.map(requestPreviewText).filter(Boolean).join(' ');
  }
  if (value && typeof value === 'object') {
    for (const key of ['text', 'input_text']) {
      const text = requestPreviewText(value[key]);
      if (text) return text;
    }
  }
  return '';
}

// Return a bounded request preview for task list columns.
function firstMessagePreview(task, maxLength = 40) {
  const persistedPreview = task && typeof task.first_message_preview === 'string'
    ? task.first_message_preview
    : '';
  const payload = task && typeof task.payload === 'object' ? task.payload : null;
  const messages = Array.isArray(payload && payload.messages) ? payload.messages : [];
  const firstMessage = messages.length > 0 && messages[0] && typeof messages[0] === 'object' ? messages[0] : null;
  let content = persistedPreview || requestPreviewText(firstMessage ? firstMessage.content : '');
  if (!content && payload && payload.input !== undefined) {
    const input = Array.isArray(payload.input) ? payload.input[0] : payload.input;
    content = requestPreviewText(input);
  }
  if (!content && payload && payload.tool) {
    const argumentsText = payload.arguments && Object.keys(payload.arguments).length
      ? ` ${JSON.stringify(payload.arguments)}`
      : '';
    content = `${payload.tool}${argumentsText}`;
  }
  if (!content && payload) {
    content = requestPreviewText(payload.prompt);
  }
  const normalized = content.replace(/\s+/g, ' ').trim();
  if (!normalized) return '—';
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, Math.max(0, maxLength - 1))}…`;
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

function renderTaskViewerChecklist(containerId, values, groupName, checked = true) {
  const box = $(containerId);
  if (!box) return;

  box.innerHTML = values.map(value => {
    const label = typeof value === 'string' ? value : value.label;
    const itemValue = typeof value === 'string' ? value : value.value;
    const extra = typeof value === 'string' ? '' : (value.extra || '');
    return `
      <label>
        <input type="checkbox" name="${groupName}" value="${escapeHtml(itemValue)}"${checked ? ' checked' : ''}>
        <span>${escapeHtml(label)}${extra ? ` <span style="color:var(--muted)">${escapeHtml(extra)}</span>` : ''}</span>
      </label>
    `;
  }).join('');
}

function renderTaskViewerMeta(meta) {
  const statuses = Array.isArray(meta?.status_options) && meta.status_options.length ? meta.status_options : TASK_VIEWER_STATUSES;
  const workers = Array.isArray(meta?.workers) ? meta.workers : [];
  const envids = Array.isArray(meta?.envids) ? meta.envids : [];
  const routes = Array.isArray(meta?.routes) ? meta.routes : [];

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
  renderTaskViewerChecklist('task-viewer-route-list', routes.map(route => ({
    label: route.label || route.value || '',
    value: route.value || '',
    extra: Number(route.count || 0) > 0 ? `[${Number(route.count)}]` : '',
  })), 'task-viewer-route', false);

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
  getCheckedValues('task-viewer-route-list').forEach(value => params.append('route', value));

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

  params.set('limit', $('task-viewer-limit').value);
  return params;
}

function renderTaskViewerRows(tasks) {
  const body = $('task-viewer-body');
  if (!body) return;

  body.innerHTML = '';
  if (!tasks.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="13" style="color:var(--muted)">No tasks match the current filters.</td>';
    body.appendChild(tr);
    return;
  }

  tasks.forEach(task => {
    const tr = document.createElement('tr');
    const requestPreview = escapeHtml(firstMessagePreview(task));
    const errorDetails = task.status === 'failed' ? task.error_details || '' : '';
    tr.innerHTML = `
      <td style="font-family:monospace;font-size:11px">${escapeHtml(task.id || '')}</td>
      <td><span class="badge badge-${escapeHtml(task.status || 'created')}">${escapeHtml(task.status || 'created')}</span></td>
      <td style="color:var(--err);font-size:12px;max-width:320px">${escapeHtml(errorDetails)}</td>
      <td>${escapeHtml(task.worker_id || '—')}</td>
      <td>${escapeHtml(task.envid || '—')}</td>
      <td>${escapeHtml(task.type || '—')}</td>
      <td style="color:var(--muted);font-size:12px">${escapeHtml(fmtDateTime(task.created_at))}</td>
      <td style="color:var(--muted);font-size:12px">${escapeHtml(fmtTaskDuration(task.started_at, task.finished_at))}</td>
      <td style="color:var(--muted);font-size:12px">${requestPreview}</td>
      <td>${escapeHtml(String(Number(task.llm_call_count || 0)))}</td>
      <td style="color:var(--muted);font-size:12px">${escapeHtml(fmtDateTime(task.last_operation_at))}</td>
      <td><button class="btn-sm" data-task-json-id="${escapeHtml(task.id || '')}">Show Request</button></td>
      <td><button class="btn-sm" data-task-steps-id="${escapeHtml(task.id || '')}">Show Task</button></td>
    `;
    tr.querySelector('[data-task-json-id]').addEventListener('click', () => openTaskViewerJson(task.id));
    tr.querySelector('[data-task-steps-id]').addEventListener('click', () => openTaskViewerSteps(task));
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

function setTaskViewerModalCopyText(text) {
  taskViewerModalCopyText = String(text || '');
  const copyButton = $('task-json-modal-copy');
  copyButton.disabled = !taskViewerModalCopyText;
}

function setTaskViewerModalBackAction(action) {
  taskViewerModalBackAction = typeof action === 'function' ? action : null;
  $('task-json-modal-back').hidden = !taskViewerModalBackAction;
}

function renderTaskViewerModalJson() {
  const body = $('task-json-modal-body');
  const textButton = $('task-json-modal-text');
  textButton.hidden = false;
  if (taskViewerModalJsonMode === 'text') {
    textButton.textContent = 'Tree';
    textButton.title = 'Show JSON as tree';
    body.classList.add('is-text');
    body.replaceChildren(document.createTextNode(taskViewerModalJsonText));
    return;
  }
  textButton.textContent = 'Text';
  textButton.title = 'Show JSON as text';
  body.classList.remove('is-text');
  const viewer = window.TaskStepsViewer;
  const tree = viewer && typeof viewer.renderJsonTree === 'function'
    ? viewer.renderJsonTree(taskViewerModalJsonValue, false, true)
    : document.createTextNode(taskViewerModalJsonText);
  body.replaceChildren(tree);
}

function openTaskViewerJsonModal(title, subtitle, value, backAction = null) {
  const modal = $('task-json-modal');
  taskViewerModalJsonValue = value;
  taskViewerModalJsonText = JSON.stringify(value, null, 2);
  taskViewerModalJsonMode = 'tree';
  setTaskViewerModalBackAction(backAction);
  setTaskViewerModalCopyText(taskViewerModalJsonText);
  $('task-json-modal-title').textContent = title;
  $('task-json-modal-subtitle').textContent = subtitle;
  renderTaskViewerModalJson();
  modal.classList.add('is-open');
  modal.setAttribute('aria-hidden', 'false');
}

function toggleTaskViewerModalJsonMode() {
  if (taskViewerModalJsonValue === null) return;
  taskViewerModalJsonMode = taskViewerModalJsonMode === 'tree' ? 'text' : 'tree';
  renderTaskViewerModalJson();
}

function returnToTaskViewerModal() {
  if (taskViewerModalBackAction) taskViewerModalBackAction();
}

async function copyTaskViewerModalJson() {
  if (!taskViewerModalCopyText) return;
  const copyButton = $('task-json-modal-copy');
  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(taskViewerModalCopyText);
      } catch (error) {
        const textarea = document.createElement('textarea');
        textarea.value = taskViewerModalCopyText;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        const copied = document.execCommand('copy');
        textarea.remove();
        if (!copied) throw error;
      }
    } else {
      const textarea = document.createElement('textarea');
      textarea.value = taskViewerModalCopyText;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      const copied = document.execCommand('copy');
      textarea.remove();
      if (!copied) throw new Error('Clipboard copy was rejected');
    }
    copyButton.textContent = 'Copied';
  } catch (error) {
    copyButton.textContent = 'Copy failed';
  }
  window.setTimeout(() => {
    copyButton.textContent = 'Copy';
  }, 1500);
}

function openTaskViewerTextModal(title, subtitle, bodyText, backAction = null) {
  const modal = $('task-json-modal');
  const body = $('task-json-modal-body');
  taskViewerModalJsonValue = null;
  setTaskViewerModalBackAction(backAction);
  $('task-json-modal-text').hidden = true;
  setTaskViewerModalCopyText(bodyText);
  $('task-json-modal-title').textContent = title;
  $('task-json-modal-subtitle').textContent = subtitle;
  body.classList.add('is-text');
  body.textContent = bodyText;
  body.replaceChildren(document.createTextNode(bodyText));
  modal.classList.add('is-open');
  modal.setAttribute('aria-hidden', 'false');
}

function openTaskViewerNodeModal(title, subtitle, bodyNode, copyText = '', backAction = null) {
  const modal = $('task-json-modal');
  const body = $('task-json-modal-body');
  taskViewerModalJsonValue = null;
  setTaskViewerModalBackAction(backAction);
  $('task-json-modal-text').hidden = true;
  setTaskViewerModalCopyText(copyText);
  $('task-json-modal-title').textContent = title;
  $('task-json-modal-subtitle').textContent = subtitle;
  body.classList.remove('is-text');
  body.replaceChildren();
  if (bodyNode) {
    body.appendChild(bodyNode);
  }
  modal.classList.add('is-open');
  modal.setAttribute('aria-hidden', 'false');
}

function openTaskViewerModal(task) {
  const subtitle = `${task.status || 'created'} · ${task.worker_id || 'no worker'}${task.envid ? ` · envid ${task.envid}` : ''} · ${Number(task.llm_call_count || 0)} LLM calls`;
  const taskJson = JSON.stringify(task, null, 2);
  const viewer = window.TaskStepsViewer;
  if (viewer && typeof viewer.renderTaskDetailsView === 'function') {
    openTaskViewerNodeModal(`Task ${task.id || ''} details`, subtitle, viewer.renderTaskDetailsView(task), taskJson);
    return;
  }
  openTaskViewerJsonModal(`Task ${task.id || ''} JSON`, subtitle, task);
}

function closeTaskViewerModal() {
  const modal = $('task-json-modal');
  if (!modal) return;
  setTaskViewerModalBackAction(null);
  modal.classList.remove('is-open');
  modal.setAttribute('aria-hidden', 'true');
}

async function openTaskViewerJson(taskId) {
  if (!taskId) return;
  const data = await apiGet(`/api/tasks/viewer/${encodeURIComponent(taskId)}/request`);
  if (!data || !data.event) {
    window.alert('Request audit data is unavailable');
    return;
  }
  openTaskViewerJsonModal(`Task ${taskId} request`, 'Lazy-loaded audit event', data.event);
}

function taskViewerLogFile(task) {
  const workerId = String(task?.worker_id || '').trim();
  if (!workerId) return 'openaix_call_log.jsonl';
  return `${workerId}_call_log.jsonl`;
}

function formatTaskViewerStepLines(lines) {
  if (!Array.isArray(lines) || !lines.length) {
    return 'No matching log entries found.';
  }

  return lines.map((line, index) => {
    try {
      const parsed = JSON.parse(line);
      return `#${index + 1}\n${JSON.stringify(parsed, null, 2)}`;
    } catch (error) {
      return `#${index + 1} [parse error: ${error.message}]\n${line}`;
    }
  }).join('\n\n============================================================\n\n');
}

async function openTaskViewerSteps(task) {
  const taskId = task?.id;
  if (!taskId) return;

  const data = await apiGet(`/api/tasks/viewer/${encodeURIComponent(taskId)}/detail`);
  if (!data) return;
  const container = document.createElement('div');
  const metadata = document.createElement('pre');
  metadata.textContent = JSON.stringify(data.task || {}, null, 2);
  container.appendChild(metadata);
  const title = `Task ${taskId}`;
  const subtitle = data.active ? 'Task is active; completed audit events only.' : 'Compact task metadata and audit manifest';
  const openManifest = () => openTaskViewerNodeModal(title, subtitle, container);

  for (const event of data.audit_events || []) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn secondary';
    button.textContent = `${event.type || 'audit event'} ${event.recorded_at || ''}`;
    const files = document.createElement('div');
    button.addEventListener('click', async () => {
      const raw = await apiGet(
        `/api/tasks/viewer/${encodeURIComponent(taskId)}/raw?type=${encodeURIComponent(event.type)}&event_id=${encodeURIComponent(event.event_id)}`,
      );
      if (raw?.event) {
        openTaskViewerJsonModal(
          `Task ${taskId} ${event.type}`,
          'Lazy-loaded audit event',
          raw.event,
          openManifest,
        );
        files.replaceChildren();
        const fileReferences = [raw.event.body_file, ...(Array.isArray(raw.event.attachments) ? raw.event.attachments : [])];
        for (const fileReference of fileReferences) {
          const fileId = String(fileReference?.file_id || '').trim();
          if (!fileId) continue;
          const link = document.createElement('a');
          link.href = `/api/tasks/viewer/audit-files/${encodeURIComponent(fileId)}`;
          link.target = '_blank';
          link.rel = 'noopener';
          link.className = 'btn secondary';
          link.textContent = fileReference === raw.event.body_file ? 'Open body file' : 'Open attachment';
          files.appendChild(link);
        }
      }
    });
    container.appendChild(button);
    container.appendChild(files);
  }

  openManifest();
}

// ── Live logs ──────────────────────────────────────────────────────────────
async function startLiveLogs() {
  if (!logLiveUiBound) {
    $('log-file-select').addEventListener('change', reconnectLogs);
    $('log-live').addEventListener('change', () => {
      if ($('log-live').checked) connectLogWs(); else stopLiveLogs();
    });
    $('log-interval').addEventListener('change', () => {
      if (logWs) { stopLiveLogs(); connectLogWs(); }
    });
    logLiveUiBound = true;
  }
  await loadLogFileOptions();
  if ($('log-live').checked) connectLogWs();
}

async function loadLogFileOptions() {
  const data = await apiGet('/api/logs/files');
  if (!data || !Array.isArray(data.files) || data.files.length === 0) return;

  const select = $('log-file-select');
  const previous = select.value;
  const normalizedPrevious = previous.includes('.') ? previous : `${previous}.log`;
  select.replaceChildren();
  for (const file of data.files) {
    const option = document.createElement('option');
    option.value = file;
    option.textContent = file;
    select.appendChild(option);
  }
  select.value = data.files.includes(normalizedPrevious) ? normalizedPrevious : data.files[0];
}

function stopLiveLogs() {
  logWsGeneration += 1;
  clearTimeout(logTimer);
  logTimer = null;
  if (logWs) { logWs.close(); logWs = null; }
}

function reconnectLogs() {
  stopLiveLogs();
  $('log-box').textContent = '';
  if ($('log-live').checked) connectLogWs();
}

function connectLogWs() {
  if (logWs) return;
  clearTimeout(logTimer);
  logTimer = null;

  const file = $('log-file-select').value;
  const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${wsProto}://${location.host}/ws/logs?${new URLSearchParams({ file })}`;
  const generation = logWsGeneration;
  const ws = new WebSocket(url);

  logWs = ws;

  ws.onmessage = e => {
    if (logWsGeneration === generation && logWs === ws) appendLog(e.data);
  };
  ws.onclose = () => {
    if (logWs !== ws) return;
    logWs = null;
    const seconds = parseFloat($('log-interval').value) || 1;
    if (logWsGeneration === generation && $('log-live').checked) {
      logTimer = setTimeout(connectLogWs, seconds * 1000);
    }
  };
  ws.onerror = () => {
    if (logWs === ws) ws.close();
  };
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
//
// `#settings-tree` is the generated aiset tree editor. The whole document is
// described by config.descriptor.json5, so this file only wires it up: pick a
// document, mount the editor, and offer the two process actions the editor
// cannot perform itself (reload, restart). Every field, its description and its
// validation rule come from the descriptor, never from here.
let settingsEditor = null;
let settingsEditorDocument = '';
let settingsDocuments = [];
// The editor talks to an authenticated API, so it is initialised only after the
// user is signed in. Before that, /api/settings-info answers 401 and the
// document selector would stay empty with nothing to retry it.
let settingsReady = false;

function setCfgStatus(elId, text, ok) {
  const el = $(elId);
  el.textContent = text;
  el.style.color = ok ? 'var(--ok)' : 'var(--err)';
}

function bindSettingsUi() {
  $('cfg-document').addEventListener('change', () => mountSettingsEditor($('cfg-document').value));
  $('cfg-show-absent').addEventListener('change', () => {
    if (settingsEditor) settingsEditor.setShowAbsent($('cfg-show-absent').checked);
  });
  $('cfg-values-as-text').addEventListener('change', () => {
    if (settingsEditor) settingsEditor.setValuesAsText($('cfg-values-as-text').checked);
  });
  $('cfg-reload-btn').addEventListener('click', reloadSettings);
  $('cfg-restart-btn').addEventListener('click', restartAidir);
}

/**
 * List the configuration documents and mount the editor on the first one.
 *
 * The document list comes from the backend rather than from a hardcoded array,
 * so a worker that gains its own config.json5 appears without a frontend change.
 */
// Names the script URLs that failed, because the usual cause is not a restart:
// a reverse proxy that forwards the API but not the asset path answers a
// script request with the SPA page, and the browser reports that only as a
// missing global.
function missingAssetsMessage() {
  const urls = Array.from(document.querySelectorAll('script[src*="assets/aiset"]'))
    .map(node => node.getAttribute('src'));
  return 'The settings editor assets did not load, so the settings cannot be ' +
    'edited here. Requested: ' + (urls.length ? urls.join(', ') : 'no asset URL found') +
    '. Check that those URLs answer with JavaScript and not with an HTML page.';
}

// ── Configuration assistant ────────────────────────────────────────────────
// Ported from the aiset demo host. The model reads the configuration and
// proposes operations; they arrive here and are staged on the tree, so the
// file still only changes when the user presses Apply.
let chatInFlight = null;
let chatAssistant = null;

function chatEntry(role, body) {
  const history = $('cfg-chat-history');
  if (!history) return;
  const entry = document.createElement('div');
  entry.className = 'cfg-chat-entry cfg-chat-' + role;
  const who = document.createElement('div');
  who.className = 'cfg-chat-role';
  who.textContent = role;
  const text = document.createElement('div');
  text.className = 'cfg-chat-body';
  text.textContent = body;   // never innerHTML: the answer is model output
  entry.appendChild(who);
  entry.appendChild(text);
  history.appendChild(entry);
  history.scrollTop = history.scrollHeight;
}

function showChatModel(assistant) {
  chatAssistant = assistant || null;
  const note = $('cfg-chat-model');
  const enabled = !!(chatAssistant && chatAssistant.enabled);
  if (note) {
    note.textContent = enabled
      ? 'Model ' + chatAssistant.model + ' \u00b7 document ' + (chatAssistant.document || 'main')
      : 'Disabled by configuration (ai.enabled)';
    note.classList.toggle('cfg-chat-off', !enabled);
  }
  const send = $('cfg-chat-send');
  const input = $('cfg-chat-text');
  if (send) send.disabled = !enabled || !!chatInFlight;
  if (input) input.disabled = !enabled;
}

function refreshChatBusy() {
  const enabled = !!(chatAssistant && chatAssistant.enabled);
  const send = $('cfg-chat-send');
  const input = $('cfg-chat-text');
  if (send) send.disabled = !enabled || !!chatInFlight;
  if (input) input.disabled = !enabled || !!chatInFlight;
}

async function loadChatHistory() {
  const res = await apiGet('/api/settings/chat/history');
  const history = $('cfg-chat-history');
  if (!history) return;
  history.innerHTML = '';
  const messages = (res && res.messages) || [];
  messages.forEach(item => chatEntry(item.role, item.content));
}

function sendChatMessage(message) {
  chatEntry('user', message);
  chatInFlight = apiPost('/api/settings/chat', { message: message });
  refreshChatBusy();
  chatInFlight
    .then(res => {
      const data = (res && res.data) || {};
      if (!res || !res.ok) {
        chatEntry('error', (data.error && data.error.message) || 'The assistant could not be reached');
        return;
      }
      (data.activity || []).forEach(item => {
        chatEntry('tool', item.tool + ' ' + JSON.stringify(item.arguments || {}));
      });
      chatEntry('assistant', data.message || '(no answer)');
      const staged = data.staged_operations || [];
      if (staged.length && settingsEditor) {
        settingsEditor.stageOperations(staged, 'assistant');
        chatEntry('staged', staged.length + ' change(s) staged. Review them in the tree and press Apply.');
      }
    })
    .catch(error => chatEntry('error', String((error && error.message) || error)))
    .then(() => { chatInFlight = null; refreshChatBusy(); });
}

function bindChatUi() {
  const form = $('cfg-chat-form');
  if (!form || form.dataset.bound) return;
  form.dataset.bound = '1';

  form.addEventListener('submit', event => {
    event.preventDefault();
    const input = $('cfg-chat-text');
    const message = input.value.trim();
    if (!message || chatInFlight) return;
    input.value = '';
    sendChatMessage(message);
  });

  $('cfg-chat-text').addEventListener('keydown', event => {
    // Enter sends; Shift+Enter inserts a newline.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (form.requestSubmit) form.requestSubmit();
    }
  });

  $('cfg-chat-clear').addEventListener('click', async () => {
    await apiDelete('/api/settings/chat');
    $('cfg-chat-history').innerHTML = '';
  });
}

// Measure how much of the viewport is left below the workspace, so both
// columns can be exactly that tall. The alternative is a guessed constant, and
// a guess stops matching as soon as the header above grows or shrinks.
function sizeSettingsWorkspace() {
  const workspace = document.querySelector('.cfg-workspace');
  if (!workspace) return;
  // Stacked layout has no single height to fill; let the content decide.
  // matchMedia is absent in some embedded hosts, and layout must not throw.
  const stacked = typeof window.matchMedia === 'function'
    && window.matchMedia('(max-width: 1100px)').matches;
  if (stacked) {
    workspace.style.removeProperty('--cfg-height');
    return;
  }
  const visible = window.innerHeight - workspace.getBoundingClientRect().top;
  workspace.style.setProperty('--cfg-height', Math.max(320, visible - 24) + 'px');
}

async function initSettingsBackend() {
  if (settingsReady) return;
  const res = await apiGet('/api/settings-info');
  if (!res || !res.ok) {
    // apiGet resolves the parsed body and yields null on any failure, so the
    // reason is read from the body rather than from a response envelope.
    const reason = res ? (res.detail || res.error || ('HTTP ' + res.status)) : 'no response';
    $('cfg-aiset-status').textContent = 'The settings service could not be read: ' + reason;
    $('cfg-aiset-status').style.color = 'var(--err)';
    return;
  }
  settingsDocuments = Array.isArray(res.documents) ? res.documents : [];
  showChatModel(res.assistant);

  const select = $('cfg-document');
  select.innerHTML = '';
  settingsDocuments.forEach(id => {
    const option = document.createElement('option');
    option.value = id;
    option.textContent = id === 'main' ? 'Main configuration (config.json5)' : id;
    select.appendChild(option);
  });

  if (!window.aiset || typeof window.aiset.mountTree !== 'function') {
    // Not marked ready, so opening the page again retries.
    $('cfg-aiset-status').textContent = missingAssetsMessage();
    $('cfg-aiset-status').style.color = 'var(--err)';
    return;
  }
  settingsReady = true;
  await mountSettingsEditor(settingsDocuments[0] || 'main');
  sizeSettingsWorkspace();
}

function mountSettingsEditor(documentId) {
  if (!documentId) return Promise.resolve();
  if (settingsEditor) {
    settingsEditor.destroy();
    settingsEditor = null;
  }
  settingsEditorDocument = documentId;
  settingsEditor = window.aiset.mountTree('#settings-tree', {
    document: documentId,
    apiBase: '/api/settings',
    view: 'source',
    showAbsent: $('cfg-show-absent').checked,
    valuesAsText: $('cfg-values-as-text').checked,
  });

  settingsEditor.on('status', event => {
    const node = $('cfg-aiset-status');
    node.textContent = event.message || '';
    node.style.color = event.state === 'error' ? 'var(--err)' : 'var(--muted)';
  });
  settingsEditor.on('applied', () => setCfgStatus('cfg-aiset-lifecycle-status', 'Saved.', true));

  return settingsEditor.ready;
}

async function reloadSettings() {
  setCfgStatus('cfg-aiset-lifecycle-status', 'Reloading…', true);
  const res = await apiPost('/api/settings/reload', {});
  if (!res.ok) {
    setCfgStatus('cfg-aiset-lifecycle-status', (res.data && res.data.detail) || 'Reload failed', false);
    return;
  }
  setCfgStatus('cfg-aiset-lifecycle-status', 'Settings reloaded.', true);
  if (settingsEditor) await settingsEditor.reload(false);
}

async function restartAidir() {
  if (!window.confirm('Restart aidir? Running tasks wait for the graceful timeout first.')) return;
  setCfgStatus('cfg-aiset-lifecycle-status', 'Restart requested…', true);
  const res = await apiPost('/api/restart', {});
  if (!res.ok) {
    setCfgStatus('cfg-aiset-lifecycle-status', (res.data && res.data.detail) || 'Restart failed', false);
  }
}

function bindTaskViewerUi() {
  if (taskViewerUiBound) return;
  taskViewerUiBound = true;

  $('task-viewer-show-btn').addEventListener('click', loadTaskViewerTasks);
  $('task-viewer-clear-btn').addEventListener('click', resetTaskViewerFilters);
  $('task-viewer-worker-type').addEventListener('change', applyTaskViewerWorkerTypeFilter);
  $('task-json-modal-back').addEventListener('click', returnToTaskViewerModal);
  $('task-json-modal-text').addEventListener('click', toggleTaskViewerModalJsonMode);
  $('task-json-modal-copy').addEventListener('click', copyTaskViewerModalJson);
  $('task-json-modal-close').addEventListener('click', closeTaskViewerModal);
  $('task-json-modal-backdrop').addEventListener('click', closeTaskViewerModal);

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && $('task-json-modal').classList.contains('is-open')) {
      closeTaskViewerModal();
    }
  });
}

// ── Boot ───────────────────────────────────────────────────────────────────
bindSettingsUi();
bindTaskViewerUi();
if (window.addEventListener) window.addEventListener('resize', sizeSettingsWorkspace);
// initSettingsBackend() is deliberately absent here: it needs an authenticated
// session and is called from onLoggedIn() and from the first visit to the
// settings page instead.

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
