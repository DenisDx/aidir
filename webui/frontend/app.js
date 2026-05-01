/* AI Director – frontend application logic */
'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let token = '';
let logWs = null;
let logTimer = null;
let refreshTimer = null;

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
  const url = `${wsProto}://${location.host}/ws/logs?file=${file}&token=${encodeURIComponent(token)}`;

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
async function loadConfig() {
  const data = await apiGet('/api/config');
  if (data) {
    $('config-pre').textContent = JSON.stringify(data, null, 2);
  }
}

// ── Boot ───────────────────────────────────────────────────────────────────
showScreen('login');
