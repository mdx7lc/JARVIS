/* ============================================================
   JARVIS Operator Workspace — frontend logic
   Server-backed chat, tasks, goals and telemetry; no demo panels.
   ============================================================ */
'use strict';

/* ---------- tiny helpers ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const byId = (id) => document.getElementById(id);
const SVG_HREF = 'href';

function svgIcon(id, cls = 'ic') {
  const wrap = document.createElement('span');
  wrap.innerHTML = `<svg class="${cls}"><use ${SVG_HREF}="#${id}"></use></svg>`;
  return wrap.firstChild;
}

/* ============================================================
   STATE (server-backed data only)
   ============================================================ */
const state = {
  day: 'today',
  /* Phase 3D — tasks are server-backed records (GET/POST/PATCH /api/tasks).
     `taskError` distinguishes unreachable server from a genuinely empty list. */
  tasks: { today: [], tomorrow: [] },
  tasksLoaded: false,
  tasksError: null,
   chat: [],
  conversationId: null, // active server conversation (Phase 2C /api/chat)
  chatPending: false,
  convToken: 0, // bumped on every conversation switch; in-flight reads/results retire when it changes
  tasksRevision: 0, // bumped by every optimistic task edit; list fetches that finish stale retire
  // Phase 4 dashboard lifecycle data
  metrics: null, // last valid GET /api/metrics payload
  metricsLoaded: false,
  metricsError: false,
  statusCounts: null, // exact global counts by task status (aggregate)
  dayCounts: null, // exact global counts per day (aggregate)
  convCount: null, // exact persisted conversation count
  convCountBounded: null, // true when the badge limit truncated the list
  lastStatus: null, // last valid GET /api/status payload
  // Phase 5 — live session notifications (no persistence: opening the
  // panel intentionally discards history, so storage would lie)
  notifications: [], // newest first, capped
  unreadNotifs: 0,
  notifSeq: 0, // client-side sequence for stable keys
  statusToneSeen: null, // last rendered system status tone (transition dedup)
  notifKeys: {}, // active dedupe keys for recurring failure states
};

/* Phase 3B — saved active conversation for this browser tab only */
const CONV_ID_RE = /^[0-9a-f]{32}$/;
const CONV_STORAGE_KEY = 'jarvis.conversationId';

function isValidConvId(id) {
  return typeof id === 'string' && CONV_ID_RE.test(id);
}
function saveActiveConv(id) {
  if (!isValidConvId(id)) return;
  try { sessionStorage.setItem(CONV_STORAGE_KEY, id); } catch (err) { /* storage unavailable */ }
}
function clearActiveConv() {
  try { sessionStorage.removeItem(CONV_STORAGE_KEY); } catch (err) { /* storage unavailable */ }
}
function readSavedConv() {
  try { return sessionStorage.getItem(CONV_STORAGE_KEY); } catch (err) { return null; }
}

/* Phase 8 — localStorage preferences (Phase 5): one versioned key.
   All values are validated against allowed options before they are
   applied; storage failures degrade to session-only settings. */

const PREF_KEY = 'jarvis.preferences.v1';
const PREF_DENSITIES = ['cozy', 'compact'];
const PREF_DAYS = ['today', 'tomorrow'];

function readPrefs() {
  try {
    const raw = localStorage.getItem(PREF_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed
      : {};
  } catch (err) {
    return {};
  }
}

function savePrefs(patch) {
  // merge so independent writes (density vs focus vs day) coexist
  const next = Object.assign(readPrefs(), patch);
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify(next));
  } catch (err) { /* private mode / quota: preferences stay local-only */ }
}

function loadPrefs() {
  const prefs = readPrefs();
  if (PREF_DENSITIES.includes(prefs.density)) applyDensity(prefs.density);
  if (typeof prefs.focus === 'boolean') applyFocus(prefs.focus);
  if (PREF_DAYS.includes(prefs.day)) {
    state.day = prefs.day;
    renderTimeline();
  }
}

/* ============================================================
   TOASTS
   ============================================================ */
function toast(msg) {
  let box = byId('toast-box');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toast-box';
    box.className = 'toast-box';
    box.setAttribute('role', 'status');
    box.setAttribute('aria-live', 'polite');
    document.body.appendChild(box);
  }
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add('is-in'));
  setTimeout(() => {
    el.classList.remove('is-in');
    setTimeout(() => el.remove(), 300);
  }, 2600);
}

/* ============================================================
   LIVE STATUS (Phase 3C) — GET /api/status + conversation count
   ============================================================ */
const CONV_BADGE_LIMIT = 100;

function applyProviderStates(payload) {
  // Effective model policy goes to the topbar chip; server reachability
  // goes to the sidebar connection dot. Configuration is never presented
  // as proof of inference — the chip says "policy", not "verified".
  const dot = byId('conn-dot');
  if (dot) dot.dataset.state = payload ? 'live' : 'offline';

  const chip = byId('topbar-model');
  const text = byId('topbar-model-text');
  if (chip && text) {
    const configured = !!(payload && payload.provider_configured);
    if (configured) {
      const bits = [payload.effective_model || 'provider default'];
      if (payload.model_source === 'runtime_override') bits.push('override');
      text.textContent = bits.join(' · ');
      chip.title = 'Effective model policy (configuration, not proof of verified inference)';
    } else {
      text.textContent = payload ? 'AI not configured' : 'Model policy unknown';
      chip.title = payload
        ? 'No provider credentials configured — chat replies use the scripted fallback'
        : 'Server unreachable — model policy unknown';
    }
    chip.hidden = false;
  }

  if (payload && payload.provider_configured) {
    modelState.selected = payload.effective_model || modelState.selected;
    modelState.policy = payload.model_policy || modelState.policy;
    modelState.source = payload.model_source || modelState.source;
    const catState = payload.catalog || {};
    modelState.fetchedAt = catState.fetched_at || modelState.fetchedAt;
    modelState.error = catState.error || (catState.fetched_at ? null : modelState.error);
  }
}

function applySystemStatus(payload) {
  const badge = byId('sys-status');
  const text = byId('sys-status-text');
  if (!badge || !text) return;
  let tone = 'offline';
  let label = 'System Status · Offline';
  if (payload) {
    if (payload.db === 'ready') {
      tone = payload.provider_configured ? 'live' : 'no-ai';
      label = payload.provider_configured
        ? 'System Status · Live'
        : 'System Status · Live · AI not configured';
    } else {
      tone = 'degraded';
      label = 'System Status · DB degraded';
    }
  }
  badge.dataset.state = tone;
  text.textContent = label;
  // Phase 5: notify on verified transitions (never on the first read —
  // that is a baseline, not a transition)
  if (state.statusToneSeen !== null && payload) {
    if (tone !== state.statusToneSeen) {
      pushNotification('status', 'System status changed',
        `Now: ${label.replace('System Status · ', '')}.`);
    }
  }
  state.statusToneSeen = tone;
}

async function refreshSystemStatus() {
  let payload = null;
  try {
    const resp = await fetch('/api/status', { headers: { Accept: 'application/json' } });
    if (resp.ok) payload = await resp.json();
  } catch (err) { /* server unreachable */ }
  state.lastStatusError = !payload; // cached copy (if any) is stale now
  if (payload) releaseNotifKey('status-unreachable');
  else {
    pushNotificationOnce('status-unreachable', 'error', 'Server unreachable',
      'GET /api/status failed; the last known status is displayed.');
  }
  applyProviderStates(payload);
  applySystemStatus(payload);
  updateOrbState();
  // keep the last valid status for the briefing (the briefing marks it
  // "last known" while fresher requests are failing)
  if (payload) state.lastStatus = payload;
}

/* ============================================================
   LIVE METRICS (Phase 4) — GET /api/metrics driving gauges, the
   System overview row and the briefing. Single-flight + sequence
   guard so a slow response can never overwrite a newer one.
   ============================================================ */
const METRICS_INTERVAL_MS = 6000;
const TASKS_INTERVAL_MS = 30000;
let metricsInFlight = false;

async function refreshMetrics() {
  if (metricsInFlight) return; // never pipeline overlapping polls
  metricsInFlight = true;
  const seq = ++gaugeSeq;
  let payload = null;
  try {
    const resp = await fetch('/api/metrics', { headers: { Accept: 'application/json' } });
    if (resp.ok) payload = await resp.json();
  } catch (err) { /* server unreachable */ }
  metricsInFlight = false;
  if (seq !== gaugeSeq) return; // stale response: newer state exists
  state.metrics = payload;
  state.metricsLoaded = true;
  state.metricsError = !payload;
  updateGauges(payload, seq);
  applyMonitorChip(payload);
  renderDiagnosticsStatus();
  updateOrbState();
  rerenderBriefingIfOpen();
}

function startDashboardRefresh() {
  // ONE polling loop, created once; timers never multiply on revisit.
  setInterval(() => {
    if (!document.hidden) refreshMetrics();
  }, METRICS_INTERVAL_MS);
  setInterval(() => {
    if (document.hidden) return;
    // never auto-refetch tasks while a status change is optimistic-flying
    const saving = state.tasks.today.concat(state.tasks.tomorrow)
      .some((t) => t.saving);
    if (!saving) {
      refreshTasks();
      refreshConvBadge();
    }
    // Home shows live goal truth: refresh the list when the console is
    // closed (loadGoals is a bounded list read, never execution).
    const goalsModal = byId('modal-goals');
    if (goalsModal && !goalsModal.open) {
      goalState.selected = null; // Home poll never drives detail loads
      loadGoals();
    }
    refreshSystemStatus();
  }, TASKS_INTERVAL_MS);
  document.addEventListener('visibilitychange', () => {
    byId('app').classList.toggle('is-doc-hidden', document.hidden);
    if (!document.hidden) {
      refreshMetrics();
      refreshConvBadge();
      updateOrbState();
    }
  });
}

async function refreshConvBadge() {
  const badge = byId('nav-conv-badge');
  if (!badge) return;
  try {
    const resp = await fetch(
      `/api/conversations?limit=${CONV_BADGE_LIMIT}`,
      { headers: { Accept: 'application/json' } }
    );
    if (!resp.ok) throw new Error(String(resp.status));
    const data = await resp.json();
    badge.textContent = data.truncated ? `${CONV_BADGE_LIMIT - 1}+` : String(data.count);
    badge.hidden = false;
    state.convCount = data.truncated ? null : data.count; // exact only
    state.convCountBounded = data.truncated;
  } catch (err) {
    badge.hidden = true;
    state.convCount = null;
    state.convCountBounded = null;
  }
}

const STATUS_LABEL = { done: 'Done', queued: 'Queued', planned: 'Planned', warn: 'High prio' };

function renderTimeline() {
  const box = byId('timeline');
  if (!box) return;
  box.innerHTML = '';
  const items = state.tasks[state.day];
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'timeline-empty';
    if (state.tasksError) {
      empty.textContent = 'Could not load tasks from the local server.';
    } else if (state.day === 'tomorrow') {
      empty.textContent = 'No tasks scheduled for tomorrow.';
    } else if (state.tasksLoaded) {
      empty.textContent = 'No tasks yet. Create one with the “New task” button — tasks are saved on the local server and survive reloads.';
    } else {
      empty.textContent = 'Loading tasks from the local server…';
    }
    box.appendChild(empty);
    if (state.tasksError) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'chat-retry';
      retry.textContent = 'Retry';
      retry.onclick = () => refreshTasks();
      box.appendChild(retry);
    }
  } else {
    items.forEach((t, i) => {
      const li = document.createElement('li');
      li.className = 'tl-item';
      li.dataset.status = t.status === 'queued' && t.priority === 'high' ? 'warn' : t.status;
      if (t.id) li.dataset.taskId = t.id; // searchable jump target
      const time = document.createElement('span');
      time.className = 'tl-time';
      time.textContent = t.time;
      const node = document.createElement('span');
      node.className = 'tl-node';
      node.setAttribute('aria-hidden', 'true');
      const body = document.createElement('div');
      body.className = 'tl-body';
      const title = document.createElement('span');
      title.className = 'tl-title';
      title.textContent = t.title;
      const status = document.createElement('button');
      status.type = 'button';
      status.className = 'tl-status';
      status.textContent = t.saving ? 'Saving…' : (STATUS_LABEL[t.status] || t.status);
      status.title = 'Click to cycle status (saved to the local server)';
      status.setAttribute('aria-label', `Cycle status of ${t.title}, currently ${status.textContent}`);
    status.addEventListener('click', () => cycleTaskStatus(t));
      body.append(title, status);
      li.append(time, node, body);
      box.appendChild(li);
    });
  }
  $$('.seg-btn[data-day]').forEach((b) => {
    const active = b.dataset.day === state.day;
    b.classList.toggle('is-active', active);
    b.setAttribute('aria-selected', String(active));
  });
  rebuildSearchIndex();
}

/* ============================================================
   TASKS — server persistence (Phase 3D /api/tasks)
   ============================================================ */
const TASK_STATUS_ORDER = ['queued', 'warn', 'done', 'planned'];
const TASKS_PAGE_LIMIT = 200;

async function refreshTasks() {
  state.tasksError = null;
  const revisionAtFetch = state.tasksRevision; // edits bump this
  try {
    const resp = await fetch(
      `/api/tasks?limit=${TASKS_PAGE_LIMIT}`,
      { headers: { Accept: 'application/json' } }
    );
    if (!resp.ok) throw new Error(String(resp.status));
    const data = await resp.json();
    if (state.tasksRevision !== revisionAtFetch) {
      return; // a task edit happened while this fetch was in flight:
      // discarding stale server data beats overwriting newer UI state
    }
    const all = Array.isArray(data.tasks) ? data.tasks : [];
    // server order: updated_at DESC — keep it (deterministic)
    state.tasks.today = all.filter((t) => t.day !== 'tomorrow');
    state.tasks.tomorrow = all.filter((t) => t.day === 'tomorrow');
    // exact global counts (server aggregate); the page list may be
    // truncated — never present its length as a total (Phase 4)
    state.statusCounts = data.status_counts || null;
    state.dayCounts = data.day_counts || null;
    state.tasksLoaded = true;
    state.tasksError = null;
    releaseNotifKey('tasks-error');
  } catch (err) {
    state.tasksError = err;
    pushNotificationOnce('tasks-error', 'error', 'Task list unreachable',
      'Could not load tasks from the local server; the timeline shows the last fetched data.');
  }
  renderTimeline();
}

function cycleTaskStatus(task) {
  const prev = task.status;
  const next = TASK_STATUS_ORDER[(TASK_STATUS_ORDER.indexOf(task.status) + 1)
    % TASK_STATUS_ORDER.length];
  state.tasksRevision += 1; // lets in-flight auto-refreshes know to retire
  task.seq = (task.seq || 0) + 1;
  const seqSent = task.seq;
  task.status = next;
  task.saving = true;
  renderTimeline();
  fetch(`/api/tasks/${task.id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ status: next }),
  }).then(async (resp) => {
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || data.status !== next) throw new Error('bad response');
    // a newer click superseded this request: do not overwrite it
    if (task.seq === seqSent) {
      Object.assign(task, data, { seq: task.seq, saving: false });
      renderTimeline();
      pushNotification('task-updated', 'Task status updated',
        `${task.title} → ${data.status}.`);
    }
  }).catch(async () => {
    if (task.seq !== seqSent) return;
    // restore the previous status and re-read the authoritative list
    task.saving = false;
    task.status = prev;
    renderTimeline();
      pushNotification('task-failed', 'Task update failed',
      `Could not save "${task.title}"; status reverted to ${prev}.`);
    toast('Could not save the task status — please try again.');
    await refreshTasks();
  });
}

async function submitTaskForm() {
  const submitBtn = byId('task-form').querySelector('.btn-primary');
  if (submitBtn && submitBtn.disabled) return; // duplicate-submit guard
  const title = byId('task-title-input').value.trim();
  if (!title) return;
  if (submitBtn) submitBtn.disabled = true;
  try {
    const resp = await fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({
        title,
        time: byId('task-time').value,
        priority: byId('task-priority').value,
      }),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !data.id) throw new Error('save failed');
    const stored = Object.assign({ seq: 0 }, data);
    state.tasks[data.day === 'tomorrow' ? 'tomorrow' : 'today'].unshift(stored);
    byId('task-title-input').value = '';
    closeModal('modal-task');
    state.day = 'today';
    state.tasksLoaded = true;
    state.tasksError = null;
    renderTimeline();
      releaseNotifKey('task-create-failed');
    pushNotification('task-created', 'Task saved',
      `"${title}"; filed under ${data.day === 'tomorrow' ? 'Tomorrow' : 'Today'}.`);
    toast('Task saved on the local server.');
  } catch (err) {
    pushNotificationOnce('task-create-failed', 'task-failed', 'Task not saved',
      'The local server did not accept the task. Your text is kept in the form.');
    toast('Could not save the task — the local server did not accept it. Your text is kept in the form.');
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

/* ============================================================
   GAUGES — live from GET /api/metrics (Phase 4).
   CPU is only meaningful after two samples: the honest pending state
   is "Sampling…", never a fabricated number. Unavailable metrics are
   labelled, not zeroed. Values are server-computed and clamped
   (0-100) before anything touches the DOM.
   ============================================================ */
const GAUGE_KEYS = ['cpu', 'memory', 'disk'];
const GAUGE_LABELS = { cpu: 'CPU', memory: 'RAM', disk: 'Disk' };
const gaugeRefs = {};
let gaugeSeq = 0; // bumped by every metrics response; stale ones retire

function renderGauges() {
  const box = byId('gauges');
  if (!box) return;
  box.innerHTML = '';
  GAUGE_KEYS.forEach((key) => {
    const cell = document.createElement('div');
    cell.className = 'gauge';
    cell.innerHTML = `
      <svg class="gauge-svg" viewBox="0 0 100 100" role="img"
        aria-label="${GAUGE_LABELS[key]} — waiting for first measurement">
        <circle cx="50" cy="50" r="42" class="gauge-arc-bg"/>
        <circle cx="50" cy="50" r="42" class="gauge-arc-progress" style="--c:${(2 * Math.PI * 42).toFixed(1)}"/>
        <text x="50" y="55" text-anchor="middle" class="gauge-val">…<tspan font-size="8" fill="#8AA7B9">%</tspan></text>
      </svg>
      <span class="gauge-label">${GAUGE_LABELS[key]}</span>
      <span class="gauge-note">Waiting for server…</span>`;
    box.appendChild(cell);
    const arc = cell.querySelector('.gauge-arc-progress');
    arc.style.setProperty('--p', '0');
    const val = cell.querySelector('.gauge-val');
    const note = cell.querySelector('.gauge-note');
    gaugeRefs[key] = { cell, arc, val, note };
  });
}

function fmtInt(n) {
  return Math.round(Number(n)).toLocaleString('en-US');
}

function applyMonitorChip(payload) {
  const chip = byId('h-monitor-chip');
  if (!chip) return;
  if (!payload) {
    chip.className = 'chip chip-inline chip-off';
    chip.textContent = 'Telemetry unreachable';
    return;
  }
  const secs = GAUGE_KEYS.map((k) => payload[k])
    .filter(Boolean);
  const liveCount = secs.filter((s) => s.available).length;
  const sampling = secs.some((s) => s.sampling);
  const unavail = secs.filter((s) => !s.available && !s.sampling).length;
  if (liveCount === GAUGE_KEYS.length) {
    chip.className = 'chip chip-inline chip-live';
    chip.textContent = 'Live · local sampling';
  } else if (liveCount === 0 && unavail === GAUGE_KEYS.length) {
    chip.className = 'chip chip-inline chip-off';
    chip.textContent = 'Telemetry unavailable';
  } else {
    chip.className = 'chip chip-inline chip-off';
    chip.textContent = sampling ? 'Live · sampling CPU…' : 'Live · partial';
  }
}

function updateGauges(payload, seq) {
  const offline = !payload;
  GAUGE_KEYS.forEach((key) => {
    const ref = gaugeRefs[key];
    if (!ref) return;
    const section = offline ? null : payload[key];
    let pct = null;
    let note;
    let state = '';
    if (offline || !section) {
      note = 'Server unreachable';
      state = 'is-unavail';
    } else if (section.available) {
      pct = typeof section.percent === 'number'
        && Number.isFinite(section.percent)
        ? Math.max(0, Math.min(100, section.percent)) : null;
      if (pct === null) {
        note = 'Unavailable';
        state = 'is-unavail';
      } else {
        note = 'Live';
        if (pct >= 90) state = 'gauge-err';
        else if (pct >= 75) state = 'gauge-warn';
      }
    } else if (section.sampling) {
      note = 'Sampling…';
      state = 'is-sampling';
    } else {
      note = 'Unavailable';
      state = 'is-unavail';
    }
    // a newer update supersedes this one (stale fetch guard)
    if (seq !== gaugeSeq) return;
    ref.cell.className = `gauge${state ? ' ' + state : ''}`;
    ref.cell.querySelector('.gauge-svg').setAttribute(
      'aria-label',
      `${GAUGE_LABELS[key]} usage ${pct === null ? 'unavailable'
        : Math.round(pct) + ' percent'} (${note})`);
    ref.val.innerHTML = `${pct === null ? '—'
      : String(Math.round(pct))}<tspan font-size="8" fill="#8AA7B9">%</tspan>`;
    ref.note.textContent = note;
    if (pct !== null) {
      const target = String(pct / 100);
      requestAnimationFrame(() => ref.arc.style.setProperty('--p', target));
    }
  });
}

/* ============================================================
   NAVIGATION — Home is a view; Goals/Conversations/Settings open
   their existing consoles (preserved flows); the rest are pages.
   ============================================================ */
const VIEWS = ['command', 'projects', 'obsidian', 'graph', 'tools', 'diagnostics'];

function showView(key) {
  if (!VIEWS.includes(key)) key = 'command';
  for (const v of VIEWS) {
    const el = byId(`view-${v}`);
    if (el) {
      el.hidden = v !== key;
      el.classList.toggle('is-visible', v === key);
    }
  }
  $$('.nav-item').forEach((b) => {
    const active = b.dataset.nav === key;
    b.classList.toggle('is-active', active);
    if (active) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  if (key === 'diagnostics') refreshMetrics();
  if (key === 'diagnostics') refreshUpdImprovements();
  if (key === 'command') updateOrbState();
  closeRail();
}

function initNav() {
  $$('.nav-item').forEach((btn) => {
    btn.addEventListener('click', () => {
      const nav = btn.dataset.nav;
      if (nav === 'goals') { openGoalsModal(); return; }
      if (nav === 'conversations') { openModal('modal-chat'); return; }
      if (nav === 'settings') { openSettingsModal(); return; }
      showView(nav);
    });
  });
  byId('diag-btn').addEventListener('click', () => showView('diagnostics'));
}

/* ============================================================
   HOME — orb state, active/recent goals, real activity
   ============================================================ */
const ORB_STATES = ['idle', 'thinking', 'planning', 'executing',
  'awaiting_approval', 'verifying', 'error'];

const ORB_NOTES = {
  idle: 'All clear — no active operations.',
  thinking: 'Composing a reply from the configured model…',
  planning: 'A goal is being planned by the server worker.',
  executing: 'A goal is executing registered tools.',
  awaiting_approval: 'A goal is waiting for your approval.',
  verifying: 'A goal is running its verification recipes.',
  error: 'A goal failed or is blocked — open Goals for details.',
};

function updateOrbState() {
  const orb = byId('core-orb');
  const text = byId('core-state-text');
  const note = byId('core-state-note');
  const chip = byId('activity-chip');
  let next = 'idle';

  if (state.chatPending) {
    next = 'thinking';
  } else {
    const active = (goalState.goals || []).find((g) => ![
      'succeeded', 'failed', 'cancelled', 'blocked', 'queued', 'ready',
    ].includes(g.status));
    if (active) {
      if (active.status === 'awaiting_approval') next = 'awaiting_approval';
      else if (active.status === 'verifying') next = 'verifying';
      else if (active.status === 'planning') next = 'planning';
      else if (active.status === 'executing') next = 'executing';
      else next = 'executing';
    }
  }
  if (next !== 'error' && (goalState.goals || []).some(
    (g) => g.status === 'failed' || g.status === 'blocked'
  )) next = 'error';

  if (orb) {
    orb.classList.remove(...ORB_STATES.map((s) => `is-${s}`));
    orb.classList.add(`is-${next}`);
    orb.dataset.state = next;
    orb.setAttribute('aria-label', `JARVIS core — ${next.replace('_', ' ')}`);
  }
  if (text) text.textContent = next.replace('_', ' ');
  if (note) note.textContent = ORB_NOTES[next];
  if (chip) {
    chip.textContent = next === 'idle' ? 'idle' : next.replace('_', ' ');
    chip.className = `chip chip-inline ${next === 'idle' ? 'chip-off' : 'chip-live'}`;
  }
}

function renderHomeGoals() {
  const goals = goalState.goals || [];
  const active = goals.find((g) => ![
    'succeeded', 'failed', 'cancelled', 'blocked', 'queued', 'ready',
  ].includes(g.status));

  const activeBox = byId('home-active-goal');
  if (activeBox) {
    activeBox.innerHTML = '';
    if (!active) {
      const p = document.createElement('p');
      p.className = 'activity-empty';
      p.textContent = 'No goal is running. Queue one from the Goal Console — planning, tool execution and verification happen on the local server.';
      activeBox.appendChild(p);
    } else {
      const title = document.createElement('p');
      title.className = 'home-goal-objective';
      title.textContent = active.objective;
      const meta = document.createElement('p');
      meta.className = 'goal-progress';
      meta.textContent = `Status: ${GOAL_STATUS_LABELS[active.status] || active.status}`
        + (typeof active.progress === 'number' ? ` · ${active.progress}% complete` : '');
      activeBox.append(title, meta);
      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'btn';
      open.textContent = 'Open in Goal Console';
      open.addEventListener('click', openGoalsModal);
      activeBox.appendChild(open);
    }
  }

  const recent = byId('home-recent-goals');
  if (recent) {
    recent.innerHTML = '';
    if (!goals.length) {
      const li = document.createElement('li');
      li.className = 'goal-empty';
      li.textContent = 'No durable goals yet.';
      recent.appendChild(li);
    }
    goals.slice(0, 4).forEach((g) => {
      const li = document.createElement('li');
      li.className = 'home-goal-item';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'home-goal-objective';
      btn.textContent = g.objective;
      btn.title = g.objective;
      btn.addEventListener('click', () => {
        openGoalsModal();
        loadGoalDetail(g.id);
      });
      const status = document.createElement('span');
      status.className = `chip chip-inline ${goalStatusChipClass(g.status)}`;
      status.textContent = GOAL_STATUS_LABELS[g.status] || g.status;
      const when = document.createElement('span');
      when.className = 'home-goal-when';
      when.textContent = g.created_at ? String(g.created_at).slice(0, 10) : '';
      li.append(btn, status, when);
      recent.appendChild(li);
    });
  }

  renderHomeActivity(goals);
  updateOrbState();
}

function goalStatusChipClass(status) {
  if (status === 'succeeded') return 'chip-ok';
  if (status === 'failed' || status === 'blocked') return 'chip-err';
  if (status === 'awaiting_approval') return 'chip-demo';
  if (status === 'planning' || status === 'verifying') return 'chip-plan';
  if (status === 'queued' || status === 'ready') return 'chip-off';
  return 'chip-live';
}

function renderHomeActivity(goals) {
  const list = byId('home-activity');
  if (!list) return;
  list.innerHTML = '';
  const items = [];
  const running = (goals || []).filter((g) => ![
    'succeeded', 'failed', 'cancelled', 'blocked', 'queued', 'ready',
  ].includes(g.status));
  for (const g of running.slice(0, 3)) {
    items.push({ strong: 'Goal', text: `${g.objective} — ${GOAL_STATUS_LABELS[g.status] || g.status}` });
  }
  const failed = (goals || []).filter((g) => g.status === 'failed' || g.status === 'blocked');
  for (const g of failed.slice(0, 2)) {
    items.push({ strong: 'Needs attention', text: `${g.objective} — ${GOAL_STATUS_LABELS[g.status] || g.status}` });
  }
  if (state.chatPending) {
    items.push({ strong: 'Chat', text: 'Waiting for the configured model…' });
  }
  if (!items.length) {
    const li = document.createElement('li');
    li.className = 'activity-empty';
    li.textContent = 'No operations in progress.';
    list.appendChild(li);
    return;
  }
  for (const it of items) {
    const li = document.createElement('li');
    const label = document.createElement('strong');
    label.textContent = `${it.strong}: `;
    li.append(label, document.createTextNode(it.text));
    list.appendChild(li);
  }
}

/* ============================================================
   HONEST PAGE VIEWS — Projects / Tools / Diagnostics.
   Obsidian + Graph are static setup states in the HTML itself.
   ============================================================ */
function renderProjectsFacts() {
  const list = byId('projects-facts');
  if (!list) return;
  const facts = [];
  if (state.lastStatus && state.lastStatus.db) {
    facts.push(['Local server', `Database status: ${state.lastStatus.db}.`]);
  }
  if (state.convCount !== null) {
    facts.push(['Conversations', `${state.convCount} persisted conversation${state.convCount === 1 ? '' : 's'} on the local server.`]);
  }
  const goalCount = (goalState.goals || []).length;
  facts.push(['Durable goals', goalCount
    ? `${goalCount} goal${goalCount === 1 ? '' : 's'} currently listed.`
    : 'No goals queued yet — create them in the Goal Console.']);
  const taskCount = state.statusCounts
    ? Object.values(state.statusCounts).reduce((a, b) => a + b, 0) : null;
  if (taskCount !== null) {
    facts.push(['Tasks', `${taskCount} saved task${taskCount === 1 ? '' : 's'} (all days).`]);
  }
  list.innerHTML = '';
  for (const [label, text] of facts) {
    const li = document.createElement('li');
    const strong = document.createElement('strong');
    strong.textContent = `${label}. `;
    li.append(strong, document.createTextNode(text));
    list.appendChild(li);
  }
}

function renderToolsList() {
  // The tool registry is server-internal; no /api/tools endpoint exists
  // yet, so this lists capability categories truthfully instead of a
  // live registry dump.
  const list = byId('tools-list');
  if (!list) return;
  const rows = [
    ['Read-only workspace inspection', 'Goal steps can list and read workspace files (sensitive names excluded) while planning and executing.'],
    ['Atomic file write — approval-gated', 'Writes land through a temp-file + rename with backup; every proposal needs a digest-bound human approval before execution.'],
    ['Verification recipes', 'Bounded syntax checks, project tests and diff inspections run as child processes; timeouts and missing interpreters are reported honestly.'],
  ];
  list.innerHTML = '';
  for (const [title, text] of rows) {
    const li = document.createElement('li');
    const strong = document.createElement('strong');
    strong.textContent = `${title} — `;
    li.append(strong, document.createTextNode(text));
    list.appendChild(li);
  }
}

function renderDiagnosticsStatus() {
  const list = byId('diag-status');
  if (!list) return;
  const rows = [];
  const st = state.lastStatus;
  if (st) {
    rows.push(['Database', st.db === 'ready' ? 'ready' : st.db]);
    rows.push(['AI provider', st.provider_configured
      ? `configured${st.effective_model ? ` — policy: ${st.effective_model}` : ''}`
      : 'not configured (chat uses the scripted fallback)']);
    if (st.catalog && typeof st.catalog.count === 'number') {
      rows.push(['Model catalog', `${st.catalog.count} listed${st.catalog.error ? ' (catalog fetch error — see Settings)' : ''}`]);
    }
  } else {
    rows.push(['Server', 'unreachable — showing no numbers rather than stale ones']);
  }
  if (state.convCount !== null) {
    rows.push(['Conversations', `${state.convCount} persisted`]);
  }
  const goalCount = (goalState.goals || []).length;
  rows.push(['Durable goals', goalCount ? `${goalCount} listed` : 'none queued']);
  if (state.metricsLoaded) {
    rows.push(['Local telemetry', state.metricsError
      ? 'unreachable'
      : 'live local sampling (CPU · RAM · disk)']);
  }
  list.innerHTML = '';
  for (const [label, text] of rows) {
    const li = document.createElement('li');
    const strong = document.createElement('strong');
    strong.textContent = `${label}: `;
    li.append(strong, document.createTextNode(text));
    list.appendChild(li);
  }
}

/* mobile rail drawer */
function openRail() {
  byId('app').classList.add('has-rail-open');
  const scrim = byId('rail-scrim');
  scrim.hidden = false;
  byId('menu-btn').setAttribute('aria-expanded', 'true');
}
function closeRail() {
  byId('app').classList.remove('has-rail-open');
  const scrim = byId('rail-scrim');
  if (scrim) scrim.hidden = true;
  const m = byId('menu-btn');
  if (m) m.setAttribute('aria-expanded', 'false');
}
function initRailDrawer() {
  byId('menu-btn').addEventListener('click', () => {
    if (byId('app').classList.contains('has-rail-open')) closeRail();
    else openRail();
  });
  byId('rail-scrim').addEventListener('click', closeRail);
}

/* ============================================================
   FOCUS MODE & DENSITY
   ============================================================ */
function applyFocus(on) {
  const btn = byId('focus-toggle');
  byId('app').classList.toggle('is-focus', on);
  if (btn) btn.setAttribute('aria-pressed', String(on));
}

function applyDensity(choice) {
  const app = byId('app');
  app.dataset.density = choice;
  $$('.seg-btn[data-density]').forEach((x) => {
    const active = x.dataset.density === choice;
    x.classList.toggle('is-active', active);
    x.setAttribute('aria-selected', String(active));
  });
}

function initFocus() {
  const btn = byId('focus-toggle');
  btn.addEventListener('click', () => {
    const on = !byId('app').classList.contains('is-focus');
    applyFocus(on);
    savePrefs({ focus: on });
    toast(on ? 'Focus Mode on — secondary home panels hidden' : 'Focus Mode off');
  });
}

function initDensity() {
  $$('.seg-btn[data-density]').forEach((b) => {
    b.addEventListener('click', () => {
      applyDensity(b.dataset.density);
      savePrefs({ density: b.dataset.density });
      toast(`Layout density: ${b.dataset.density}`);
    });
  });
  byId('layout-btn').addEventListener('click', () => togglePop('pop-layout', true));
}

/* ============================================================
   POPOVERS
   ============================================================ */
function togglePop(id, forceOpen) {
  const pop = byId(id);
  if (!pop) return;
  const willOpen = forceOpen === true ? true : pop.hidden;
  $$('.pop').forEach((p) => { p.hidden = true; });
  pop.hidden = !willOpen;
}

/* ============================================================
   NOTIFICATIONS (Phase 5) — real session events only. Sources are
   server-confirmed task create/update results, failures that need
   attention, and observed status transitions. No demo entries, and
   no persistence: the feed is this tab's session, stated as such.
   ============================================================ */
const NOTIF_MAX = 30;

function pushNotification(kind, title, desc) {
  state.notifSeq += 1;
  const event = {
    id: state.notifSeq,
    kind,
    title,
    desc,
    time: new Date(),
  };
  state.notifications.unshift(event);
  if (state.notifications.length > NOTIF_MAX) state.notifications.length = NOTIF_MAX;
  state.unreadNotifs += 1;
  renderNotifications();
  updateNotifBadge();
}

/* Recurring states (server unreachable…) must notify once, not once
   per poll. An active key suppresses repeats; releaseNotifKey re-arms
   it when the condition clears. */
function pushNotificationOnce(key, kind, title, desc) {
  if (state.notifKeys[key]) return;
  state.notifKeys[key] = true;
  pushNotification(kind, title, desc);
}

function releaseNotifKey(key) {
  delete state.notifKeys[key];
}

const NOTIF_ICONS = {
  'task-created': 'i-tasks',
  'task-updated': 'i-play',
  'task-failed': 'i-warn',
  status: 'i-info',
  error: 'i-warn',
};

function fmtNotifTime(d) {
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function updateNotifBadge() {
  const btn = byId('notif-btn');
  const badge = byId('notif-badge');
  if (!btn || !badge) return;
  const n = state.unreadNotifs;
  badge.hidden = n === 0;
  badge.textContent = n > 99 ? '99+' : String(n);
  btn.classList.toggle('has-dot', n > 0);
}

function clearNotifications() {
  state.notifications = [];
  state.unreadNotifs = 0;
  renderNotifications();
}

function renderNotifications() {
  const list = byId('notif-list');
  if (!list) return;
  list.innerHTML = '';
  if (!state.notifications.length) {
    const li = document.createElement('li');
    li.className = 'notif-item notif-empty';
    const body = document.createElement('p');
    body.className = 'notif-desc';
    body.textContent = 'No session events yet. Saving or updating a task, provider state changes and server errors will appear here.';
    li.appendChild(body);
    list.appendChild(li);
    return;
  }
  state.notifications.forEach((n) => {
    const li = document.createElement('li');
    li.className = 'notif-item' + (n.kind === 'task-failed' || n.kind === 'error' ? ' notif-item--warn' : '');
    li.appendChild(svgIcon(NOTIF_ICONS[n.kind] || 'i-info'));
    const body = document.createElement('div');
    const t = document.createElement('p');
    t.className = 'notif-title';
    t.textContent = n.title;
    const d = document.createElement('p');
    d.className = 'notif-desc';
    d.textContent = n.desc;
    const time = document.createElement('p');
    time.className = 'notif-time';
    time.textContent = fmtNotifTime(n.time);
    body.append(t, d, time);
    li.appendChild(body);
    list.appendChild(li);
  });
}

function openNotifications() {
  togglePop('pop-notif', true);
  // opening the panel is the explicit read; refreshing never marks read
  state.unreadNotifs = 0;
  updateNotifBadge();
}

function initPops() {
  byId('notif-btn').addEventListener('click', (e) => { e.stopPropagation(); openNotifications(); });
  const clearBtn = byId('notif-clear');
  if (clearBtn) clearBtn.addEventListener('click', () => { clearNotifications(); togglePop('pop-notif', true); });
  byId('settings-btn').addEventListener('click', (e) => { e.stopPropagation(); openSettingsModal(); });
  byId('layout-btn').addEventListener('click', (e) => { e.stopPropagation(); togglePop('pop-layout', true); });
  $$('.pop [data-close]').forEach((btn) => btn.addEventListener('click', () => { byId(btn.dataset.close).hidden = true; }));
  document.addEventListener('click', (e) => {
    if (e.target.closest('.pop') || e.target.closest('.icon-btn')) return;
    $$('.pop').forEach((p) => { p.hidden = true; });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $$('.pop').forEach((p) => { p.hidden = true; });
  });
}

/* ============================================================
   SEARCH (Phase 5 reworked)
   Two layers, both honest:
   1. Panel highlight — indexes the DOM labels as before.
   2. Server results — GET /api/search over persisted tasks,
      conversation titles and message content (debounced, sequence-
      guarded so a slow stale response can never replace a newer
      one). Actions open the matching view/modal.
   ============================================================ */
let searchHits = [];
let searchCursor = 0;
let searchResults = []; // parsed API results for the active query
let searchOption = -1; // keyboard cursor into the server results dropdown
let searchSeq = 0; // bumped per user input; stale responses retire
let searchAbort = null;
let searchTimer = 0;
const SEARCH_DEBOUNCE_MS = 300;
const SEARCH_MIN_SERVER_QUERY = 2; // matches server validation
const SEARCH_RESULT_LABELS = { task: 'Task', message: 'Message', conversation: 'Chat' };

function buildSearchIndex() {
  return $$('#view-command .panel').flatMap((panel) =>
    $$('.tl-title, .goal-progress, .home-goal-objective, .activity-list li, .panel-title', panel)
      .map((el) => ({ text: el.textContent.toLowerCase(), el, panel }))
  );
}

function clearSearchHits() {
  searchHits.forEach((h) => h.panel.classList.remove('is-hit'));
  searchHits = [];
}

function runSearch(q) {
  clearSearchHits();
  const query = q.trim().toLowerCase();
  if (!query) return 0;
  const seen = new Set();
  searchHits = SEARCH_INDEX.filter((e) => e.text.includes(query)).filter((e) => {
    if (seen.has(e.panel)) return false;
    seen.add(e.panel);
    return true;
  });
  searchHits.forEach((h) => h.panel.classList.add('is-hit'));
  searchCursor = 0;
  return searchHits.length;
}

function rebuildSearchIndex() {
  SEARCH_INDEX = buildSearchIndex();
}

function hideSearchResults() {
  const box = byId('search-results');
  if (box) {
    box.hidden = true;
    box.innerHTML = '';
  }
  searchResults = [];
  searchOption = -1;
}

async function fetchSearchResults(query, seq) {
  if (searchAbort) searchAbort.abort();
  searchAbort = new AbortController();
  let status = 0;
  let payload = null;
  try {
    const resp = await fetch('/api/search?q=' + encodeURIComponent(query),
      { headers: { Accept: 'application/json' }, signal: searchAbort.signal });
    status = resp.status;
    if (resp.ok) payload = await resp.json();
  } catch (err) {
    if (err && err.name === 'AbortError') return; // superseded input
    payload = null;
  }
  if (seq !== searchSeq) return; // a newer query is authoritative now
  renderSearchResults(payload, query, status);
}

function renderSearchResults(payload, query, status) {
  const box = byId('search-results');
  if (!box) return;
  box.innerHTML = '';
  const head = document.createElement('p');
  head.className = 'search-results-head';
  if (!payload) {
    head.textContent = status === 0
      ? 'Search unavailable — the local server did not answer.'
      : `Search error (${status}). Check the query and try again.`;
  } else if (!payload.results.length) {
    head.textContent = `No server records match "${query}".`;
  } else {
    head.textContent = `${payload.results.length} server record${payload.results.length === 1 ? '' : 's'} matching "${query}"`;
  }
  box.appendChild(head);
  searchResults = payload ? payload.results : [];
  searchOption = -1;
  if (!searchResults.length) {
    box.hidden = false;
    return;
  }
  searchResults.forEach((r) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'search-result';
    const chip = document.createElement('span');
    chip.className = 'search-result-kind';
    chip.textContent = SEARCH_RESULT_LABELS[r.type] || r.type;
    const body = document.createElement('span');
    body.className = 'search-result-body';
    const title = document.createElement('span');
    title.className = 'search-result-title';
    title.textContent = r.type === 'message'
      ? (r.conversation_title || '(untitled conversation)')
      : (r.title || '(untitled)');
    const snippet = document.createElement('span');
    snippet.className = 'search-result-snippet';
    snippet.textContent = r.type === 'conversation' ? '' : (r.snippet || '');
    body.append(title, snippet);
    btn.append(chip, body);
    btn.dataset.searchIndex = String(searchResults.indexOf(r));
    btn.addEventListener('click', () => openSearchResult(r));
    box.appendChild(btn);
  });
  box.hidden = false;
}

async function openSearchResult(r) {
  clearSearchHits();
  const input = byId('search');
  input.value = '';
  searchSeq += 1;
  hideSearchResults();
  input.blur();
  showView('command');
  if (r.type === 'task' && r.day) {
    state.day = r.day === 'tomorrow' ? 'tomorrow' : 'today';
    /* the page list can be stale (record created elsewhere / beyond the
       page window): refresh from the server before jumping */
    await refreshTasks();
    const li = document.querySelector(
      `#timeline [data-task-id="${CSS.escape(r.id)}"]`);
    if (li) {
      li.scrollIntoView({ behavior: 'smooth', block: 'center' });
      toast(`Task found on ${r.day === 'tomorrow' ? 'Tomorrow' : 'Today'}.`);
    } else {
      toast(`Task found on ${r.day === 'tomorrow' ? 'Tomorrow' : 'Today'}; `
        + 'the timeline page list may not include every saved task.');
    }
  } else {
    // conversation + message both land in the chat transcript modal
    openModal('modal-chat');
    await selectConversation(r.conversation_id || r.id);
  }
}

function initSearch() {
  const input = byId('search');
  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const q = input.value.trim();
    runSearch(q);
    if (q.length < SEARCH_MIN_SERVER_QUERY) {
      searchSeq += 1;
      if (searchAbort) searchAbort.abort();
      hideSearchResults();
      return;
    }
    const seq = ++searchSeq;
    searchTimer = setTimeout(() => fetchSearchResults(q, seq), SEARCH_DEBOUNCE_MS);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (searchResults.length) {
        e.preventDefault();
        const btns = $$('#search-results .search-result');
        searchOption = e.key === 'ArrowDown'
          ? Math.min(searchOption + 1, btns.length - 1)
          : Math.max(searchOption - (searchOption < 0 ? 0 : 1), 0);
        (btns[searchOption] || btns[0])?.focus();
      }
      return;
    }
    if (e.key === 'Enter') {
      if (searchResults.length) {
        const next = $$('#search-results .search-result')[Math.max(searchOption, 0)];
        if (next) { e.preventDefault(); next.click(); return; }
      }
      if (searchHits.length) {
        e.preventDefault();
        searchCursor = (searchCursor + 1) % searchHits.length;
        jumpToHit(searchCursor);
        toast(`Match ${searchCursor + 1} of ${searchHits.length}`);
      }
    }
    if (e.key === 'Escape') {
      input.value = '';
      searchSeq += 1;
      if (searchAbort) searchAbort.abort();
      clearSearchHits();
      hideSearchResults();
      input.blur();
    }
  });
  document.addEventListener('click', (e) => {
    if (e.target.closest('.topbar-search')) return;
    hideSearchResults();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== '/' || e.defaultPrevented) return;
    if (document.querySelector('dialog[open]')) return;
    const a = document.activeElement;
    const tag = a && a.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (a && a.isContentEditable)) return;
    e.preventDefault();
    input.focus();
    input.select();
  });
}

function jumpToHit(i) {
  const hit = searchHits[i];
  if (!hit) return;
  // reveal the view the hit lives in (may be a page view, not Home)
  const owner = hit.el.closest('.view');
  if (owner && owner.hidden) {
    for (const v of VIEWS) {
      const el = byId(`view-${v}`);
      if (el) el.hidden = el !== owner;
    }
    $$('.nav-item').forEach((b) => {
      const active = b.dataset.nav === owner.id.replace('view-', '');
      b.classList.toggle('is-active', active);
    });
  }
  hit.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  hit.panel.classList.add('is-hit');
}

/* ============================================================
   MODALS
   ============================================================ */
function openModal(id) {
  const dlg = byId(id);
  if (dlg && typeof dlg.showModal === 'function' && !dlg.open) dlg.showModal();
}
function closeModal(id) {
  const dlg = byId(id);
  if (dlg && dlg.open) dlg.close();
}
function initModals() {
  $$('[data-close-modal]').forEach((btn) => btn.addEventListener('click', () => closeModal(btn.dataset.closeModal)));
}

/* generic information modal */
function showInfo(title, bodyHTML) {
  byId('info-title').textContent = title;
  byId('info-body').innerHTML = bodyHTML; /* static strings only — never user input */
  openModal('modal-info');
}

/* ============================================================
   MODEL MANAGEMENT (Phase 7A) — GET /api/models (explicit user
   action only), GET/PATCH/DELETE /api/settings/model. A model in
   the catalog is configuration, never proof of verified inference.
   ============================================================ */
const modelState = {
  catalog: null,      // last fetched normalized entries
  fetchedAt: null,
  loading: false,     // single-flight guard for catalog requests
  applying: false,    // duplicate-submit guard for apply/clear
  error: null,        // last catalog or apply failure text
  filter: '',
  selected: null,     // effective policy from the server
  policy: null,       // environment default policy
  fallback: null,     // environment fallback policy (Phase 8A)
  source: null,       // 'runtime_override' | 'environment_default'
  seq: 0,             // stale-response guard
};

async function fetchModelsCatalog(force) {
  const seq = ++modelState.seq;
  modelState.loading = true;
  renderModels();
  try {
    const resp = await fetch(force ? '/api/models?refresh=1' : '/api/models',
      { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (seq !== modelState.seq) return; // stale response, newer state exists
    if (resp.ok && payload && Array.isArray(payload.models)) {
      modelState.catalog = payload.models;
      modelState.fetchedAt = payload.fetched_at;
      modelState.error = null;
    } else if (payload && payload.error) {
      const code = payload.error.code || `HTTP ${resp.status}`;
      // error codes are a closed taxonomy; text must be honest, never fabricated
      modelState.error = `${code}: ${payload.error.message || `Model catalog request failed (HTTP ${resp.status})`}`;
      if (modelState.selected === null) await reloadModelPolicy();
    }
  } catch (err) {
    if (seq === modelState.seq) {
      modelState.error = 'Network error — the model catalog could not be fetched. The current policy is unchanged.';
    }
  }
  modelState.loading = false;
  renderModels();
}

async function reloadModelPolicy() {
  try {
    const resp = await fetch('/api/settings/model', { headers: { Accept: 'application/json' } });
    if (!resp.ok) return;
    const payload = await resp.json();
    if (payload && payload.selected) {
      modelState.selected = payload.selected;
      modelState.policy = payload.model_policy;
      modelState.fallback = payload.fallback || null;
      modelState.source = payload.source;
    }
  } catch (err) { /* server unreachable: catalog modal keeps last known policy */ }
}

function modelAvailability(entry) {
  // The server already normalized availability; absence of flags
  // means "listed" — never invented metadata is displayed.
  return entry && entry.available !== false;
}

function renderModels() {
  const list = byId('models-list');
  const errBox = byId('models-error');
  const filterBox = byId('models-filter');
  if (!list || !errBox || !filterBox) return;

  // current policy summary (always from the authoritative server state)
  const note = byId('models-note');
  if (note) {
    const source = modelState.source === 'runtime_override'
      ? 'runtime override' : 'environment default';
    const fetched = modelState.catalog && modelState.fetchedAt
      ? ` Catalog last fetched ${new Date(modelState.fetchedAt * 1000).toLocaleTimeString()}.`
      : (modelState.error ? ' Catalog fetch failed; the current policy is unchanged.' : '');
    const fallback = modelState.fallback
      ? ` Fallback policy: ${modelState.fallback} (one extra attempt on transient failures only).`
      : '';
    note.textContent =
      `Current model policy: ${modelState.selected || 'unknown'} — ${source}.${fetched}${fallback}`
      + ' A model being listed in the catalog is configuration, not proof of verified inference — every chat reply is what proves a model worked.';
  }

  if (modelState.error) {
    errBox.hidden = false;
    errBox.textContent = modelState.error;
  } else {
    errBox.hidden = true;
    errBox.textContent = '';
  }

  const needle = modelState.filter.trim().toLowerCase();
  const entries = (modelState.catalog || []).filter((m) =>
    !needle || String(m.id || '').toLowerCase().includes(needle));
  list.innerHTML = '';
  if (modelState.loading && !modelState.catalog) {
    const li = document.createElement('li');
    li.className = 'model-item model-loading';
    li.textContent = 'Fetching model catalog…';
    list.appendChild(li);
    return;
  }
  if (modelState.catalog && !entries.length) {
    const li = document.createElement('li');
    li.className = 'model-item model-empty';
    li.textContent = needle ? 'No model matches this filter.' : 'The catalog contains no models.';
    list.appendChild(li);
    return;
  }
  if (!modelState.catalog && !modelState.loading) {
    const li = document.createElement('li');
    li.className = 'model-item model-empty';
    li.textContent = modelState.error
      ? 'Catalog unavailable — retry with “Refresh catalog”.'
      : 'Catalog not fetched yet.';
    list.appendChild(li);
  }
  entries.forEach((m) => {
    const id = String(m.id || '');
    const active = modelState.selected === id;
    const usable = modelAvailability(m);
    const li = document.createElement('li');
    li.className = 'model-item'
      + (active ? ' is-active' : '')
      + (usable ? '' : ' is-unavailable');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'model-select';
    btn.dataset.model = id;
    const label = document.createElement('span');
    label.className = 'model-id';
    label.textContent = id; // gateway-controlled text: never injected as HTML
    const st = document.createElement('span');
    st.className = 'model-state';
    if (active) st.textContent = 'Active policy';
    else if (!usable) st.textContent = m.unavailable_reason || 'unavailable';
    else if (modelState.applying) st.textContent = '…';
    else st.textContent = 'Select';
    btn.append(label, st);
    if (active || !usable || modelState.applying) btn.disabled = true;
    btn.addEventListener('click', () => applyModel(id));
    li.appendChild(btn);
    if (!usable && m.unavailable_reason) {
      // reason is gateway text: shown as-is in a dedicated node
      const reason = document.createElement('span');
      reason.className = 'model-reason';
      reason.textContent = m.unavailable_reason;
      li.appendChild(reason);
    }
    list.appendChild(li);
  });
}

async function applyModel(id) {
  if (modelState.applying) return; // duplicate-submit guard
  modelState.applying = true;
  renderModels();
  let ok = false;
  try {
    const resp = await fetch('/api/settings/model', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ model: id }),
    });
    const payload = await resp.json().catch(() => null);
    if (payload && payload.error) {
      modelState.error = `${payload.error.code || `HTTP ${resp.status}`}: ${payload.error.message || 'Model selection failed'}`;
      if (!modelState.catalog) await reloadModelPolicy();
    } else if (resp.ok && payload && payload.applied) {
      // authoritative state comes from the server response
      modelState.selected = payload.selected;
      modelState.source = payload.source;
      modelState.error = null;
      ok = true;
    } else {
      modelState.error = `Model selection failed (HTTP ${resp.status}). The previous policy is unchanged.`;
    }
  } catch (err) {
    modelState.error = 'Network error — model selection aborted. The previous policy is unchanged.';
  }
  modelState.applying = false;
  renderModels();
  if (ok) {
    toast(`Model applied: ${id}`);
    refreshSystemStatus();
  } else {
    toast('Model selection failed — policy unchanged');
  }
}

async function clearModelOverride() {
  if (modelState.applying) return;
  modelState.applying = true;
  renderModels();
  let ok = false;
  try {
    const resp = await fetch('/api/settings/model', { method: 'DELETE', headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (resp.ok && payload && payload.cleared) {
      modelState.selected = payload.selected;
      modelState.source = 'environment_default';
      modelState.error = null;
      ok = true;
    } else if (payload && payload.error) {
      modelState.error = `${payload.error.code || `HTTP ${resp.status}`}: ${payload.error.message}`;
    } else {
      modelState.error = `Clearing the override failed (HTTP ${resp.status}).`;
    }
  } catch (err) {
    modelState.error = 'Network error — override not cleared.';
  }
  modelState.applying = false;
  renderModels();
  if (ok) {
    toast(`Policy reset to environment default (${modelState.selected})`);
    refreshSystemStatus();
  }
}

function openModelsModal() {
  openModal('modal-models');
  reloadModelPolicy().then(renderModels);
  if (modelState.filter) {
    const filterBox = byId('models-filter');
    if (filterBox) filterBox.value = modelState.filter;
  }
  if (!modelState.catalog && !modelState.loading) fetchModelsCatalog();
  else renderModels();
}

function initModelsModal() {
  const filterBox = byId('models-filter');
  const refresh = byId('models-refresh');
  const clear = byId('models-clear');
  // the current model should never wait on typing: refresh on every open
  if (filterBox) {
    filterBox.addEventListener('input', () => {
      modelState.filter = filterBox.value;
      renderModels();
    });
  }
  if (refresh && !refresh.dataset.wired) {
    refresh.dataset.wired = '1';
    refresh.addEventListener('click', () => {
      modelState.error = null;
      if (!modelState.loading) fetchModelsCatalog(true);
    });
  }
  if (clear && !clear.dataset.wired) {
    clear.dataset.wired = '1';
    clear.addEventListener('click', clearModelOverride);
  }
}

/* ============================================================
   SETTINGS CENTER (Phase 11A) — GET/PATCH /api/settings,
   POST/DELETE /api/settings/credentials, POST /api/settings/
   test-connection (catalog only, never inference), POST
   /api/settings/apply (the only activation path), POST
   /api/settings/model-test (one bounded reply). Secrets are
   write-only: never echoed, never displayed.
   ============================================================ */
const settingsState = { view: null, loading: false, seq: 0,
  catalog: {} }; // per-provider cached catalog meta (explicit fetches only)

function settingsErrorText(code, status, fallback) {
  return `${code || `HTTP ${status}`}: ${fallback}`;
}

/* Phase 11A: the model routing tab's suggestions + cost/availability
   hints come from the server's cached, pricing-annotated catalog
   (GET /api/settings/catalog?provider=…). Fetched only on explicit
   tab/provider actions — never on chat, polling, or modal open. */
async function fetchSettingsCatalog(pid, force) {
  if (!pid) return;
  const cur = settingsState.catalog[pid] || { entries: null, fetchedAt: null, loading: false, error: null };
  if (cur.loading || (cur.entries && !force)) {
    renderModelCatalogMeta();
    return;
  }
  settingsState.catalog[pid] = { ...cur, loading: true, error: null };
  renderModelCatalogMeta();
  try {
    const resp = await fetch('/api/settings/catalog?provider='
      + encodeURIComponent(pid) + (force ? '&refresh=1' : ''),
      { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (resp.ok && payload && Array.isArray(payload.models)) {
      settingsState.catalog[pid] = {
        entries: payload.models, fetchedAt: payload.fetched_at,
        loading: false, error: null,
      };
    } else if (payload && payload.error) {
      settingsState.catalog[pid] = {
        ...cur, loading: false,
        error: settingsErrorText(payload.error.code, resp.status,
          payload.error.message),
      };
    } else {
      settingsState.catalog[pid] = {
        ...cur, loading: false, error: `Catalog request failed (HTTP ${resp.status}).`,
      };
    }
  } catch (err) {
    const c = settingsState.catalog[pid];
    settingsState.catalog[pid] = {
      ...c, loading: false,
      error: 'Network error — catalog unavailable; selection is unchanged.',
    };
  }
  renderModelCatalogMeta();
}

function selectedProviderId() {
  // the operator's in-progress selection wins over the persisted one
  const sel = byId('settings-provider-select');
  return (sel && sel.value) || settingsState.view?.provider?.active || '';
}

function catalogEntryFor(modelId) {
  const cat = settingsState.catalog[selectedProviderId()];
  if (!cat || !cat.entries || !modelId) return null;
  return cat.entries.find((m) => m && m.id === modelId) || null;
}

const COST_CHIP_CLASS = { free_verified: 'chip-ok', paid: 'chip-demo' };
function renderModelCatalogMeta() {
  const datalist = byId('settings-model-options');
  const meta = document.querySelector('[data-catalog-meta]');
  const cat = settingsState.catalog[selectedProviderId()];
  if (datalist) {
    datalist.innerHTML = '';
    (cat && cat.entries ? cat.entries : []).forEach((m) => {
      // server-normalized entries only; unavailable entries are labeled
      // honestly (apply refuses them server-side anyway)
      const opt = document.createElement('option');
      opt.value = String(m.id || '');
      const bits = [`cost: ${m.pricing_class || 'unknown'}`];
      if (m.available === false) bits.push(m.unavailable_reason || 'unavailable');
      opt.label = bits.join(' · ');
      datalist.appendChild(opt);
    });
  }
  if (!meta) return;
  meta.innerHTML = ''; // rebuilt from textContent-only nodes below
  const modelInput = byId('settings-model-input');
  const v = settingsState.view;
  if (!v || !modelInput) return;
  const modelId = modelInput.value.trim() || v.model.provider_default;
  const line = document.createElement('span');
  if (!modelId) {
    // no policy resolved yet — nothing honest to claim
  } else if (cat && cat.loading) {
    line.className = 'field-hint';
    line.textContent = 'Fetching catalog…';
  } else if (cat && cat.error && !cat.entries) {
    line.className = 'field-hint probe-fail';
    line.textContent = `Catalog: ${cat.error} The current selection is unchanged.`;
  } else {
    const entry = catalogEntryFor(modelId);
    if (entry) {
      const cls = entry.pricing_class || 'unknown';
      const chip = document.createElement('span');
      chip.className = 'chip chip-inline '
        + (COST_CHIP_CLASS[cls] || 'chip-disc');
      chip.textContent = cls === 'free_verified'
        ? 'free (catalog-verified)'
        : (cls === 'paid' ? 'paid — may incur cost' : 'cost unknown');
      meta.appendChild(chip);
      if (entry.available === false) {
        const un = document.createElement('span');
        un.className = 'chip chip-inline chip-err';
        un.textContent = `unavailable${entry.unavailable_reason ? ` — ${entry.unavailable_reason}` : ''}`;
        meta.appendChild(un);
      }
    } else if (cat && cat.entries) {
      line.className = 'field-hint';
      line.textContent =
        'not in the fetched catalog — apply validates it against the provider';
    } else {
      line.className = 'field-hint';
      line.textContent = 'No catalog fetched yet for this provider.';
    }
  }
  if (line.textContent) meta.appendChild(line);
  const age = document.createElement('span');
  if (cat && cat.entries && cat.fetchedAt) {
    age.className = 'field-hint';
    age.textContent = ` Catalog fetched ${new Date(cat.fetchedAt * 1000).toLocaleTimeString()}.`;
    meta.appendChild(age);
  }
}

async function fetchSettingsView() {
  const seq = ++settingsState.seq;
  settingsState.loading = true;
  renderSettingsCenter();
  try {
    const resp = await fetch('/api/settings', { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (seq !== settingsState.seq) return;
    if (resp.ok && payload && payload.provider) {
      settingsState.view = payload;
      settingsState.errorText = null;
    } else if (payload && payload.error) {
      settingsState.errorText = settingsErrorText(
        payload.error.code, resp.status, payload.error.message);
    }
  } catch (err) {
    if (seq === settingsState.seq) {
      settingsState.errorText = 'Network error — settings state unavailable.';
    }
  }
  settingsState.loading = false;
  renderSettingsCenter();
}

function openSettingsModal() {
  openModal('modal-settings');
  if (!settingsState.view && !settingsState.loading) fetchSettingsView();
  else renderSettingsCenter();
}

function findProfile(pid) {
  const v = settingsState.view;
  if (!v) return null;
  return (v.provider.profiles || []).find((p) => p.id === pid) || null;
}

function renderProviders() {
  const list = byId('provider-list');
  const v = settingsState.view;
  if (!list) return;
  list.innerHTML = '';
  if (!v) {
    const li = document.createElement('li');
    li.className = 'provider-item';
    li.textContent = settingsState.loading ? 'Loading settings…' : 'Settings unavailable.';
    list.appendChild(li);
    return;
  }
  v.provider.profiles.forEach((p) => {
    const li = document.createElement('li');
    li.className = 'provider-item';

    const head = document.createElement('div');
    head.className = 'provider-head';
    const name = document.createElement('span');
    name.className = 'provider-name';
    name.textContent = p.display;
    const status = document.createElement('span');
    status.className = 'key-status' + (p.key_source === 'environment' ? ' is-env' : '');
    status.textContent = p.key_locked
      ? `key managed by environment${p.key_configured ? ' (active)' : ''}`
      : (p.key_configured ? `key stored (${p.key_source.replace('_', ' ')})`
                          : 'no key yet');
    head.append(name, status);
    li.appendChild(head);

    const base = document.createElement('p');
    base.className = 'provider-base';
    base.textContent = p.base_url;
    li.appendChild(base);

    // key entry is always write-only
    const row = document.createElement('div');
    row.className = 'key-row';
    const input = document.createElement('label');
    input.className = 'field';
    input.style.flex = '1';
    input.innerHTML = '<span class="field-label"></span>';
    input.firstChild.textContent = `API key — ${p.id}`;
    const keyInput = document.createElement('input');
    keyInput.type = 'password';
    keyInput.id = `settings-key-${p.id}`;
    keyInput.autocomplete = 'off';
    keyInput.placeholder = p.key_locked
      ? 'managed by environment variables' : 'paste key (write-only)';
    keyInput.disabled = p.key_locked;
    input.appendChild(keyInput);
    row.appendChild(input);
    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'btn settings-key-save';
    saveBtn.textContent = 'Save key';
    saveBtn.dataset.provider = p.id;
    saveBtn.disabled = p.key_locked;
    row.appendChild(saveBtn);
    const clearBtn = document.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'btn settings-key-clear';
    clearBtn.textContent = 'Clear key';
    clearBtn.dataset.provider = p.id;
    clearBtn.disabled = p.key_locked;
    row.appendChild(clearBtn);
    const probeBtn = document.createElement('button');
    probeBtn.type = 'button';
    probeBtn.className = 'btn settings-probe';
    probeBtn.textContent = 'Test connection';
    probeBtn.dataset.provider = p.id;
    row.appendChild(probeBtn);
    li.appendChild(row);

    const result = document.createElement('p');
    result.className = 'field-hint';
    result.dataset.probeResult = p.id;
    li.appendChild(result);
    list.appendChild(li);
  });
}

function renderModelTab() {
  const v = settingsState.view;
  if (!v) return;
  // a failed apply must never silently discard what the operator chose:
  // elements the user modified since the last render keep their values
  const select = byId('settings-provider-select');
  if (select && !select.dataset.dirty) {
    select.innerHTML = '';
    v.provider.profiles.forEach((p) => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.display;
      if (p.id === v.provider.active) opt.selected = true;
      select.appendChild(opt);
    });
  }
  const modelInput = byId('settings-model-input');
  if (modelInput && !modelInput.dataset.dirty) {
    modelInput.value = v.model.policy || '';
    modelInput.disabled = v.model.model_locked;
    modelInput.placeholder = v.model.provider_default || 'provider default';
  } else if (modelInput) {
    modelInput.disabled = v.model.model_locked;
  }
  const hint = byId('settings-model-hint');
  if (hint) {
    hint.textContent = v.model.model_locked
      ? 'Locked by environment variables (JARVIS_MODEL).'
      : `Provider default: ${v.model.provider_default}. Source: ${v.model.source.replace('_', ' ')}.`;
  }
  renderModelCatalogMeta();
  const fallbackInput = byId('settings-fallback-input');
  if (fallbackInput && !fallbackInput.dataset.dirty) {
    fallbackInput.value = v.model.fallback || '';
  }
  if (fallbackInput) fallbackInput.disabled = v.model.fallback_locked;
}

function renderRuntimeTab() {
  const v = settingsState.view;
  if (!v) return;
  const map = [
    ['settings-timeout-input', v.runtime.timeout_seconds, v.runtime.timeout_locked],
    ['settings-max-tokens-input', v.runtime.max_tokens, v.runtime.max_tokens_locked],
    ['settings-history-input', v.runtime.history_messages, v.runtime.history_locked],
    ['settings-reviewer-input', v.reviewer.value || '', v.reviewer.locked],
  ];
  map.forEach(([id, value, locked]) => {
    const input = byId(id);
    if (input) {
      if (!input.dataset.dirty) input.value = value;
      input.disabled = locked;
    }
  });
  const note = byId('settings-runtime-note');
  if (note) {
    const locked = [
      v.runtime.timeout_locked && 'timeout',
      v.runtime.max_tokens_locked && 'max tokens',
      v.runtime.history_locked && 'history',
      v.reviewer.locked && 'reviewer',
    ].filter(Boolean);
    note.textContent = locked.length
      ? `Environment-managed fields: ${locked.join(', ')}.`
      : '';
  }
}

function clearSettingsDirty() {
  ['settings-provider-select', 'settings-model-input',
   'settings-fallback-input', 'settings-timeout-input',
   'settings-max-tokens-input', 'settings-history-input',
   'settings-reviewer-input'].forEach((id) => {
    const node = byId(id);
    if (node) node.dataset.dirty = '';
  });
}

function renderSettingsCenter() {
  const errBox = byId('settings-error');
  if (errBox) {
    if (settingsState.errorText) {
      errBox.hidden = false;
      errBox.textContent = settingsState.errorText;
    } else {
      errBox.hidden = true;
      errBox.textContent = '';
    }
  }
  renderProviders();
  renderModelTab();
  renderRuntimeTab();
  renderVaultTab();
}

// ---- Knowledge tab (Phase 13A: read-only Obsidian vault) -----------

let vaultState = { status: null, error: '' };

// ---- Permissions tab (central permission engine) --------------------

const LEVEL_LABELS = { 1: 'L1 Observer', 2: 'L2 Operator', 3: 'L3 Authorized executor', 4: 'L4 Restricted' };
const EFFECTIVE_LABELS = {
  authorized: 'authorized',
  denied_by_cap: 'denied by cap',
  disabled: 'revoked by operator',
  restricted: 'restricted — never grantable',
};
const DECISION_LABELS = {
  allowed: 'allowed',
  denied: 'denied',
  policy_changed: 'policy change',
  error: 'engine error',
};

let permissionsState = { view: null, audit: null, error: '', busy: false };

async function permissionsCall(path, payload) {
  try {
    const resp = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: payload == null ? '{}' : JSON.stringify(payload),
    });
    return { status: resp.status, payload: await resp.json().catch(() => null) };
  } catch (err) {
    return { status: 0, payload: null };
  }
}

async function fetchPermissions() {
  try {
    const resp = await fetch('/api/permissions', { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (resp.ok && payload) {
      permissionsState.view = payload;
      permissionsState.error = '';
    } else {
      permissionsState.error = (payload && payload.error && payload.error.message) || `HTTP ${resp.status}`;
    }
  } catch (err) {
    permissionsState.error = 'Network error — permissions unavailable.';
  }
  fetchPermissionsAudit();
  renderPermissionsTab();
}

async function fetchPermissionsAudit() {
  try {
    const resp = await fetch('/api/permissions/audit?limit=100', { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (resp.ok && payload)permissionsState.audit = payload;
  } catch (err) { /* audit feed optional; view holds the main truth */ }
  renderPermissionsTab();
}

async function setPermissionsCap(level) {
  if (permissionsState.busy) return;
  permissionsState.busy = true;
  const { status, payload } = await permissionsCall('/api/permissions/cap', { level });
  permissionsState.busy = false;
  if (status === 200 && payload && payload.state) {
    permissionsState.view = payload.state;
    pushNotification('ok', 'Capability cap updated', `JARVIS may now use up to ${LEVEL_LABELS[level]}.`);
  } else {
    permissionsState.error = (payload && payload.error && payload.error.message) || `Cap change failed (HTTP ${status}).`;
  }
  renderPermissionsTab();
}

async function setPermissionsToolPolicy(tool, state) {
  if (permissionsState.busy) return;
  permissionsState.busy = true;
  const { status, payload } = await permissionsCall('/api/permissions/policy', { tool, state });
  permissionsState.busy = false;
  if (status === 200 && payload && payload.state) {
    permissionsState.view = payload.state;
  } else {
    permissionsState.error = (payload && payload.error && payload.error.message) || `Policy change failed (HTTP ${status}).`;
  }
  renderPermissionsTab();
}

function renderPermissionsTab() {
  const row = byId('permissions-cap-row');
  if (!row) return;
  const view = permissionsState.view;
  const err = byId('settings-error');
  if (err && permissionsState.error) {
    err.textContent = permissionsState.error;
    err.hidden = false;
  }
  if (!view) {
    row.innerHTML = '<span class="field-hint">Loading permission state…</span>';
    return;
  }
  row.innerHTML = [1, 2, 3, 4].map((level) => {
    const active = view.cap === level;
    const isL4 = level === 4;
    const cls = active ? 'btn btn-primary' : 'btn';
    const title = isL4
      ? 'Level 4 is restricted: never grantable through settings or model output'
      : `Grant JARVIS up to ${LEVEL_LABELS[level]}`;
    return `<button type="button" class="${cls}" data-level="${level}" ${isL4 ? 'disabled title="' + title + '"' : 'title="' + title + '"'}>${LEVEL_LABELS[level]}${active ? ' (active)' : ''}</button>`;
  }).join('');
  const note = byId('permissions-cap-note');
  if (note) note.textContent = `Current cap: ${LEVEL_LABELS[view.cap]} — L1 reads always auto-authorized; L3 write actions additionally require an exact digest-bound approval per operation.`;

  const toolsList = byId('permissions-tools');
  if (toolsList) {
    toolsList.innerHTML = view.tools.map((tool) => {
      const eff = EFFECTIVE_LABELS[tool.effective] || tool.effective;
      const gate = tool.approval_digest_gate ? ' · digest approval gate' : '';
      const disabled = tool.effective === 'restricted';
      const stateBtn = tool.policy === 'disabled'
        ? `<button type="button" class="btn" data-tool="${tool.name}" data-state="inherit" ${disabled ? 'disabled' : ''}>Restore</button>`
        : `<button type="button" class="btn" data-tool="${tool.name}" data-state="disabled" ${disabled ? 'disabled' : ''}>Revoke</button>`;
      return `<li class="provider-item"><div class="provider-head"><span class="provider-name">${tool.name}</span><span class="key-status ${tool.effective === 'authorized' ? 'is-env' : ''}">${LEVEL_LABELS[tool.level]} · ${eff}${gate}</span></div><p class="provider-base">${tool.description}</p><div class="key-row">${stateBtn}</div></li>`;
    }).join('');
  }

  const auditBox = byId('permissions-audit');
  const auditState = byId('permissions-audit-state');
  if (auditBox && permissionsState.audit) {
    const entries = permissionsState.audit.entries || [];
    if (auditState) auditState.textContent = `${entries.length} most recent decisions`;
    auditBox.innerHTML = entries.slice(0, 30).map((ev) => `<li class="provider-item"><div class="provider-head"><span class="provider-name">${DECISION_LABELS[ev.decision] || ev.decision}</span><span class="provider-base">${ev.tool_name} · ${LEVEL_LABELS[ev.level] || 'L' + ev.level}${ev.goal_id ? ' · goal ' + ev.goal_id.slice(0, 8) : ''}${ev.source !== 'engine' ? ' · operator change' : ''} · ${ev.ts}</span></div></li>`).join('') || '<li class="provider-item"><span class="provider-base">No recorded decisions yet.</span></li>';
  }
}

// ---- Skills tab (local registry) --------------------------------------
// Skill fields come from untrusted SKILL.md files: every interpolated
// value MUST pass through esc() — a malicious manifest can never inject
// markup into the dashboard.

let skillsTabState = { view: null, error: '', busy: false };
let skillRunsState = { runs: null, sandbox: null };

function esc(value) {
  return String(value === undefined || value === null ? ''
    : value).replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
      "'": '&#39;',
    }[ch] || ch));
}

// ---- Diagnostics: updates + self-improvement (real backend data) -----

let updImprovementsState = { updates: null, improvements: null };

async function refreshUpdImprovements() {
  const results = await Promise.allSettled([
    fetch('/api/updates', { headers: { Accept: 'application/json' } }).then(
      (r) => r.json().catch(() => null)),
    fetch('/api/improvements', { headers: { Accept: 'application/json' } }).then(
      (r) => r.json().catch(() => null)),
  ]);
  updImprovementsState.updates = results[0].status === 'fulfilled'
    ? results[0].value : null;
  updImprovementsState.improvements = results[1].status === 'fulfilled'
    ? results[1].value : null;
  renderUpdImprovements();
}

function renderUpdImprovements() {
  const upd = byId('diag-updates');
  if (upd) {
    const u = updImprovementsState.updates;
    upd.innerHTML = '';
    if (!u) {
      upd.innerHTML = '<li class="conv-note">Update status unavailable.</li>';
    } else {
      const item = document.createElement('li');
      item.className = 'conv-note';
      item.textContent = u.status === 'available'
        ? `Update available: ${u.latest} (current ${u.current}) — stage & verify fetches and hash-verifies it; installation stays a separate explicit, digest-bound operator action.`
        : u.status === 'not_configured'
          ? 'No update source configured — JARVIS reports “not configured” instead of guessing. Set JARVIS_UPDATE_SOURCE to enable honest checks.'
          : u.status === 'up_to_date'
            ? `Up to date (version ${u.current}).`
            : u.status === 'error'
              ? `The last check failed: ${u.detail || 'source unreachable'}.`
              : 'No check recorded yet.';
      upd.appendChild(item);
      (u.installs || []).slice(0, 5).forEach((inst) => {
        const li = document.createElement('li');
        li.className = 'conv-note';
        li.textContent = `Install #${inst.id} ${inst.status}:`
          + ` ${inst.from_version || '?'} →`
          + ` ${inst.to_version || '?'} at ${inst.ts}`
          + (inst.backup_dir ? ' (backup recorded)' : '');
        upd.appendChild(li);
      });
    }
  }
  const imp = byId('diag-improvements');
  if (imp) {
    const i = updImprovementsState.improvements;
    imp.innerHTML = '';
    const proposals = i && i.proposals ? i.proposals : null;
    if (!proposals) {
      imp.innerHTML = '<li class="conv-note">Improvement history unavailable.</li>';
    } else if (!proposals.length) {
      imp.innerHTML = '<li class="conv-note">No improvement proposals yet — run a scan to analyze real activity aggregates.</li>';
    } else {
      proposals.slice(0, 8).forEach((p) => {
        const li = document.createElement('li');
        li.className = 'conv-note';
        const verify = p.verify_status
          ? ` · verify: ${p.verify_status}` : '';
        li.textContent = `${p.status}: ${p.kind} — ${p.problem}${verify}`
          + (p.status === 'applied' && p.verify_status === 'pending_verification'
            ? ' (verification pending: follow-up task not completed yet)'
            : '');
        imp.appendChild(li);
      });
    }
  }
}
async function fetchSkills() {
  try {
    const resp = await fetch('/api/skills', { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (resp.ok && payload) {
      skillsTabState.view = payload;
      skillsTabState.error = '';
    } else {
      skillsTabState.error = (payload && payload.error && payload.error.message) || `HTTP ${resp.status}`;
    }
  } catch (err) {
    skillsTabState.error = 'Network error — skills unavailable.';
  }
  renderSkillsTab();
}

async function postSkillsAction(id, action) {
  if (skillsTabState.busy) return;
  skillsTabState.busy = true;
  try {
    const resp = await fetch('/api/skills', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ id, action }),
    });
    if (!resp.ok) {
      const payload = await resp.json().catch(() => null);
      toast((payload && payload.error && payload.error.message) || `Skill ${action} failed (HTTP ${resp.status}).`);
    }
  } catch (err) {
    toast('Network error — skill action failed.');
  }
  skillsTabState.busy = false;
  await fetchSkills();
}

async function runSkill(id) {
  if (skillsTabState.busy) return;
  const argsInput = byId('skill-run-args');
  const note = byId('skill-run-note');
  let args = {};
  if (argsInput && argsInput.value.trim()) {
    try {
      args = JSON.parse(argsInput.value);
    } catch (err) {
      if (note) note.textContent = 'Run arguments must be a valid JSON object.';
      return;
    }
  }
  skillsTabState.busy = true;
  if (note) note.textContent = 'Running skill in the sandbox…';
  try {
    const resp = await fetch('/api/skills/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ id, args }),
    });
    const payload = await resp.json().catch(() => null);
    if (resp.ok && payload) {
      if (note) {
        note.textContent = `${payload.skill}: ${payload.status}`
          + ` (exit ${payload.exit_code}, ${payload.duration_ms} ms)`
          + ` — ${payload.verification}`;
      }
      await fetchSkillRuns();
    } else {
      if (note) note.textContent = (payload && payload.error && payload.error.message)
        || `Skill run refused (HTTP ${resp.status}).`;
      await fetchSkillRuns();
    }
  } catch (err) {
    if (note) note.textContent = 'Network error — skill run failed.';
  }
  skillsTabState.busy = false;
}

async function fetchSkillRuns() {
  try {
    const resp = await fetch('/api/skills/runs?limit=20',
      { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (resp.ok && payload) skillRunsState = payload;
  } catch (err) {
    /* keep last known history honestly */
  }
  renderSkillRuns();
}

function renderSkillRuns() {
  const box = byId('skill-runs');
  if (!box) return;
  const state = skillRunsState;
  if (!state.runs) {
    box.innerHTML = '<li class="conv-note">Execution history unavailable.</li>';
    return;
  }
  if (!state.runs.length) {
    box.innerHTML = '<li class="conv-note">No skill executions recorded yet.</li>';
    return;
  }
  box.innerHTML = state.runs.map((run) => {
    const sandboxNote = state.sandbox ? 'sandboxed (bwrap)' : 'NO SANDBOX';
    const argsFlat = Object.keys(run.args_json && JSON.parse(run.args_json) || {})
      .join(', ') || '—';
    const out = (run.stdout || '').slice(0, 400);
    return `<li class="provider-item"><div class="provider-head"><span class="provider-name">${esc(run.skill_id)}</span><span class="key-status">${esc(run.status)} · exit ${esc(run.exit_code === null ? '—' : run.exit_code)} · ${esc(run.duration_ms)} ms · ${esc(run.started_at)} · ${sandboxNote}</span></div><p class="provider-base">args: ${esc(argsFlat)}${run.verification ? ' · verify: ' + esc(run.verification) : ''}</p>${out ? `<p class="provider-base">out: ${esc(out)}</p>` : ''}</li>`;
  }).join('');
}

function renderSkillsTab() {
  const list = byId('skills-list');
  if (!list) return;
  const view = skillsTabState.view;
  const stateNote = byId('skills-state-note');
  if (skillsTabState.error && stateNote) {
    stateNote.textContent = skillsTabState.error;
  }
  if (!view) {
    if (stateNote) stateNote.textContent = 'Loading skills…';
    return;
  }
  const dirs = byId('skills-dirs-note');
  if (dirs) dirs.textContent = `Scanning: ${esc(view.dirs.join(' · '))}`;
  if (stateNote) {
    stateNote.textContent = view.skills.length
      ? `${view.skills.length} skill(s) discovered locally. Trust must be set before a skill can be executed; execution happens only through the sandboxed run form below (operator action, audited).`
      : 'No skills discovered yet. Add a folder with a SKILL.md manifest to the scan directories above, then reopen this tab.';
  }
  const execState = view.execution === 'sandboxed' ? view.execution_note : '';
  const runSection = byId('skills-run-section');
  if (runSection && view.execution) {
    runSection.hidden = false;
    const execNote = byId('skills-exec-note');
    if (execNote) execNote.textContent = execState || 'Executable skills require: trusted + enabled manifest entrypoint, and bubblewrap isolation; without a sandbox, execution fails closed.';
  }
  list.innerHTML = view.skills.map((skill) => {
    const stateLabel = skill.state === 'enabled' ? 'enabled'
      : skill.state === 'disabled' ? 'disabled by operator'
      : 'installed (not enabled)';
    const trust = skill.trust === 'trusted' ? 'trusted by operator' : 'untrusted (operator review required)';
    const canRun = skill.trust === 'trusted' && skill.state === 'enabled';
    const actions = [skill.state === 'enabled'
      ? `<button type="button" class="btn" data-skills-id="${esc(skill.id)}" data-action="disable">Disable</button>`
      : `<button type="button" class="btn btn-primary" data-skills-id="${esc(skill.id)}" data-action="enable" ${skill.trust === 'trusted' ? '' : 'title="Enable marks this skill as usable reference material for the planner"'}>Enable</button>`];
    // trust: the second gate the sandboxed run requires
    actions.push(skill.trust === 'trusted'
      ? `<button type="button" class="btn" data-skills-id="${esc(skill.id)}" data-action="untrust">Untrust</button>`
      : `<button type="button" class="btn" data-skills-id="${esc(skill.id)}" data-action="trust">Set trusted</button>`);
    const runHint = canRun
      ? '<p class="provider-base">Ready for the sandboxed run form below.</p>'
      : '';
    return `<li class="provider-item"><div class="provider-head"><span class="provider-name">${esc(skill.name)}${skill.version ? ` · v${esc(skill.version)}` : ''}</span><span class="key-status ${skill.state === 'enabled' ? 'is-env' : ''}">${stateLabel} · ${trust}</span></div><p class="provider-base">${esc(skill.description || '')}</p><p class="provider-base">${esc(skill.source)}</p>${runHint}<div class="key-row">${actions.join('')}</div></li>`;
  }).join('');
}
async function fetchVaultStatus() {
  try {
    const resp = await fetch('/api/vault', { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (resp.ok && payload) {
      vaultState = { status: payload, error: '' };
    } else if (payload && payload.error) {
      vaultState = { status: null, error: payload.error.message };
    }
  } catch (err) {
    vaultState = { status: null, error: 'Network error — vault status unavailable.' };
  }
  renderVaultTab();
}

function renderVaultTab() {
  const box = byId('settings-vault-status');
  if (!box) return;
  const input = byId('settings-vault-input');
  const view = settingsState.view;
  if (input && view && view.vault && !input.dataset.dirty) {
    input.value = view.vault.configured_path || '';
    input.disabled = view.vault.locked;
  }
  if (vaultState.error) {
    box.textContent = `Vault error: ${vaultState.error}`;
    box.classList.add('is-error');
  } else if (!vaultState.status) {
    box.textContent = '';
    box.classList.remove('is-error');
  } else {
    const s = vaultState.status;
    const idx = s.index;
    if (!s.configured) {
      box.textContent = 'Not configured — no vault folder selected.';
      box.classList.remove('is-error');
    } else if (!s.vault_exists) {
      box.textContent = `Configured vault "${s.vault_name}" is missing or unreadable (source: ${s.source}). The previous configuration is kept.`;
      box.classList.add('is-error');
    } else if (!idx || idx.status === 'never') {
      box.textContent = `Connected to "${s.vault_name}" — not indexed yet. Refresh the index to enable search.`;
      box.classList.remove('is-error');
    } else {
      const freshness = idx.root_match === false
        ? 'index belongs to a DIFFERENT vault — refresh required'
        : `last scan ${idx.last_scan_at || 'unknown'}`;
      const limited = idx.status === 'limited'
        ? ' (partial — bound by safety limits)' : '';
      box.textContent = `Connected to "${s.vault_name}" — ${idx.notes} note(s) indexed, ${freshness}${limited}.`;
      box.classList.remove('is-error');
    }
  }
  const note = byId('settings-vault-index-note');
  if (note) {
    const viewV = settingsState.view && settingsState.view.vault;
    const notes = viewV && viewV.index ? viewV.index.notes : null;
    if (notes !== null) {
      note.hidden = false;
      note.textContent = `Indexed notes: ${notes}. Knowledge tools (vault.list_files, vault.read_file, vault.search) use this index; goals can query it read-only.`;
    } else {
      note.hidden = true;
      note.textContent = '';
    }
  }
}

async function saveVaultPath() {
  const input = byId('settings-vault-input');
  const raw = input ? input.value.trim() : '';
  const { status, payload } = await settingsRequest(
    '/api/settings', 'PATCH', { vault_path: raw });
  if (status === 200) {
    toast('Vault folder set');
    settingsState.view = payload;
    await fetchVaultStatus();
  } else if (payload && payload.error) {
    showSettingsError(settingsErrorText(
      payload.error.code, status, payload.error.message));
  } else {
    showSettingsError(`Vault update failed (HTTP ${status}).`);
  }
}

async function clearVaultPath() {
  const { status, payload } = await settingsRequest(
    '/api/settings', 'PATCH', { vault_path: '' });
  if (status === 200) {
    toast('Vault disconnected');
    settingsState.view = payload;
    await fetchVaultStatus();
  } else if (payload && payload.error) {
    showSettingsError(settingsErrorText(
      payload.error.code, status, payload.error.message));
  }
}

async function refreshVaultIndex() {
  const btn = byId('settings-vault-refresh');
  if (btn) btn.disabled = true;
  try {
    const resp = await fetch('/api/vault/refresh', {
      method: 'POST',
      headers: { Accept: 'application/json' },
    });
    const payload = await resp.json().catch(() => null);
    if (resp.ok && payload) {
      toast(`Index refreshed — ${payload.notes} note(s)`);
    } else if (payload && payload.error) {
      vaultState = { status: null, error: payload.error.message };
    }
  } catch (err) {
    vaultState = { status: null, error: 'Network error — refresh failed.' };
  }
  if (btn) btn.disabled = false;
  await fetchVaultStatus();
}

function showSettingsError(text) {
  settingsState.errorText = text;
  renderSettingsCenter();
}

async function settingsRequest(path, method, payload) {
  const body = payload === undefined ? undefined : JSON.stringify(payload);
  const resp = await fetch(path, {
    method,
    headers: body === undefined
      ? { Accept: 'application/json' }
      : { 'Content-Type': 'application/json', Accept: 'application/json' },
    body,
  });
  const data = await resp.json().catch(() => null);
  return { status: resp.status, payload: data };
}

async function saveProviderKey(pid) {
  const input = byId(`settings-key-${pid}`);
  const key = input && input.value.trim();
  if (!key) {
    toast('Paste a key first');
    return;
  }
  const { status, payload } = await settingsRequest(
    '/api/settings/credentials', 'POST', { provider: pid, api_key: key });
  if (input) input.value = '';
  if (status === 200) {
    toast(`Key saved for ${pid}`);
    await fetchSettingsView();
  } else if (payload && payload.error) {
    showSettingsError(settingsErrorText(
      payload.error.code, status, payload.error.message));
  } else {
    showSettingsError(`Key save failed (HTTP ${status}). Nothing stored.`);
  }
}

async function clearProviderKey(pid) {
  const { status, payload } = await settingsRequest(
    `/api/settings/credentials?provider=${encodeURIComponent(pid)}`, 'DELETE');
  if (status === 200) {
    toast(`Key cleared for ${pid}`);
    await fetchSettingsView();
  } else if (payload && payload.error) {
    showSettingsError(settingsErrorText(
      payload.error.code, status, payload.error.message));
  } else {
    showSettingsError(`Key clear failed (HTTP ${status}).`);
  }
}

async function testProviderConnection(pid) {
  const resultNode = document.querySelector(`[data-probe-result="${pid}"]`);
  if (resultNode) resultNode.textContent = 'Testing…';
  // an entered-but-unsaved key is tested, never stored
  const input = byId(`settings-key-${pid}`);
  const probePayload = { provider: pid };
  if (input && input.value.trim() && !input.disabled) {
    probePayload.api_key = input.value.trim();
  }
  const { status, payload } = await settingsRequest(
    '/api/settings/test-connection', 'POST', probePayload);
  if (input) input.value = '';
  // the view is re-fetched after a failure (provider state may have
  // changed); re-resolve the result node after each re-render
  let node = resultNode;
  if (status !== 200) {
    await fetchSettingsView();
    node = document.querySelector(`[data-probe-result="${pid}"]`);
  }
  if (node) {
    node.classList.remove('probe-ok', 'probe-fail');
    if (status === 200 && payload && payload.ok) {
      const counts = payload.pricing_summary || {};
      node.classList.add('probe-ok');
      node.textContent = `OK: credentials accepted, ${payload.catalog.count} models listed.`
        + ` Cost classes: free ${counts.free_verified || 0}, paid ${counts.paid || 0},`
        + ` unknown ${counts.unknown || 0}. No inference was run.`;
    } else if (payload && payload.error) {
      node.classList.add('probe-fail');
      node.textContent = `Failed: ${payload.error.message || payload.error.code} (nothing was saved)`;
    } else {
      node.classList.add('probe-fail');
      node.textContent = `Failed (HTTP ${status}).`;
    }
  }
}

async function applyRoutingSettings() {
  const select = byId('settings-provider-select');
  const modelInput = byId('settings-model-input');
  const fallbackInput = byId('settings-fallback-input');
  if (!select) return;
  const payload = { provider: select.value };
  if (modelInput && !modelInput.disabled) payload.model = modelInput.value.trim();
  if (fallbackInput && !fallbackInput.disabled) {
    payload.fallback_model = fallbackInput.value.trim();
  }
  const confirmBox = byId('settings-confirm-cost');
  if (confirmBox && confirmBox.checked) payload.confirm_cost = true;
  const { status, payload: result } = await settingsRequest(
    '/api/settings/apply', 'POST', payload);
  if (status === 200) {
    settingsState.view = null;
    clearSettingsDirty();
    toast(`Model routing applied: ${select.value}`);
    await fetchSettingsView();
    // legacy views (Models modal, status) follow the applied policy
    reloadModelPolicy();
    fetchModelsCatalog();
    refreshSystemStatus();
  } else if (result && result.error) {
    showSettingsError(settingsErrorText(result.error.code, status, result.error.message));
    if (result.error.code === 'cost_confirmation_required' && confirmBox) {
      confirmBox.focus();
    }
  } else {
    showSettingsError(`Apply failed (HTTP ${status}). Current setup unchanged.`);
  }
}

async function testModel() {
  const select = byId('settings-provider-select');
  const modelInput = byId('settings-model-input');
  const resultNode = byId('settings-probe-result');
  if (!resultNode) return;
  resultNode.hidden = false;
  resultNode.textContent = 'Running one bounded test reply…';
  const payload = { provider: select ? select.value : undefined };
  if (modelInput && modelInput.value.trim()) payload.model = modelInput.value.trim();
  const confirmBox = byId('settings-confirm-cost');
  if (confirmBox && confirmBox.checked) payload.confirm_cost = true;
  const { status, payload: result } = await settingsRequest(
    '/api/settings/model-test', 'POST', payload);
  if (status === 200 && result && result.ok) {
    resultNode.className = 'field-hint probe-ok';
    resultNode.textContent = `Test reply OK (${result.latency_ms} ms, reported model:`
      + ` ${result.reported_model || 'unreported'}). This was one bounded inference request.`;
  } else if (result && result.error) {
    resultNode.className = 'field-hint probe-fail';
    resultNode.textContent =
      `Test failed: ${result.error.message || result.error.code}. Nothing changed.`;
  } else {
    resultNode.className = 'field-hint probe-fail';
    resultNode.textContent = `Test failed (HTTP ${status}). Nothing changed.`;
  }
}

async function saveRuntimeSettings() {
  const tiles = [
    ['settings-timeout-input', 'timeout_seconds'],
    ['settings-max-tokens-input', 'max_tokens'],
    ['settings-history-input', 'history_messages'],
    ['settings-reviewer-input', 'reviewer'],
  ];
  const values = {};
  tiles.forEach(([id, key]) => {
    const input = byId(id);
    if (input && !input.disabled) {
      const raw = String(input.value).trim();
      if (raw !== '') values[key] = key === 'reviewer' ? raw : Number(raw);
    }
  });
  if (!Object.keys(values).length) {
    toast('Nothing to save');
    return;
  }
  const { status, payload } = await settingsRequest(
    '/api/settings', 'PATCH', values);
  if (status === 200) {
    settingsState.view = null;
    clearSettingsDirty();
    toast('Application settings saved');
    await fetchSettingsView();
  } else if (payload && payload.error) {
    showSettingsError(settingsErrorText(payload.error.code, status, payload.error.message));
  } else {
    showSettingsError(`Save failed (HTTP ${status}).`);
  }
}

function initSettingsModal() {
  // remember which fields an operator touched since the last render,
  // so re-renders never clobber in-progress edits
  ['input', 'change'].forEach((evt) => {
    byId('modal-settings').addEventListener(evt, (e) => {
      if (e.target && 'dataset' in e.target) e.target.dataset.dirty = '1';
    }, true);
  });
  $$('button[data-tab]', byId('settings-tabs') || document).forEach((btn) => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      const map = { provider: 'settings-provider-tab', model: 'settings-model-tab', runtime: 'settings-runtime-tab', knowledge: 'settings-knowledge-tab', permissions: 'settings-permissions-tab', skills: 'settings-skills-tab' };
      Object.entries(map).forEach(([name, id]) => {
        const section = byId(id);
        if (section) section.hidden = name !== tab;
      });
      $$('#settings-tabs .seg-btn').forEach((b) => {
        const active = b.dataset.tab === tab;
        b.classList.toggle('is-active', active);
        b.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      if (tab === 'provider' || tab === 'model') fetchSettingsView();
      if (tab === 'knowledge') fetchVaultStatus();
      if (tab === 'permissions') fetchPermissions();
      if (tab === 'skills') { fetchSkills(); fetchSkillRuns(); }
      // Phase 11A: catalog-sourced suggestions are fetched only on this
      // explicit tab action (server caches; no refresh here by default)
      if (tab === 'model') {
        const sel = byId('settings-provider-select');
        fetchSettingsCatalog(sel ? sel.value : '');
      }
    });
  });
  const providerSelect = byId('settings-provider-select');
  if (providerSelect) {
    providerSelect.addEventListener('change', () => {
      fetchSettingsCatalog(providerSelect.value);
    });
  }
  const modelHintInput = byId('settings-model-input');
  if (modelHintInput) {
    modelHintInput.addEventListener('input', renderModelCatalogMeta);
  }
  const list = byId('provider-list');
  if (list) {
    list.addEventListener('click', (e) => {
      const cls = e.target.classList;
      const pid = e.target.dataset.provider;
      if (!pid) return;
      if (cls.contains('settings-key-save')) saveProviderKey(pid);
      else if (cls.contains('settings-key-clear')) clearProviderKey(pid);
      else if (cls.contains('settings-probe')) testProviderConnection(pid);
    });
  }
  const wire = (id, fn) => {
    const node = byId(id);
    if (node && !node.dataset.wired) {
      node.dataset.wired = '1';
      node.addEventListener('click', fn);
    }
  };
  wire('settings-apply', applyRoutingSettings);
  wire('settings-model-test', testModel);
  wire('settings-runtime-save', saveRuntimeSettings);
  wire('settings-vault-save', saveVaultPath);
  wire('settings-vault-clear', clearVaultPath);
  wire('settings-vault-refresh', refreshVaultIndex);
  wire('permissions-audit-refresh', () => fetchPermissionsAudit());
  const capRow = byId('permissions-cap-row');
  if (capRow && !capRow.dataset.wired) {
    capRow.dataset.wired = '1';
    capRow.addEventListener('click', (e) => {
      const level = Number(e.target.dataset.level);
      if (e.target.closest('#permissions-cap-row') && e.target.dataset.level) {
        setPermissionsCap(level);
      }
    });
  }
  const toolsList = byId('permissions-tools');
  if (toolsList && !toolsList.dataset.wired) {
    toolsList.dataset.wired = '1';
    toolsList.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-tool]');
      if (!btn) return;
      setPermissionsToolPolicy(btn.dataset.tool, btn.dataset.state);
    });
  }
  const skillsList = byId('skills-list');
  if (skillsList && !skillsList.dataset.wired) {
    skillsList.dataset.wired = '1';
    skillsList.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-skills-id]');
      if (!btn) return;
      postSkillsAction(btn.dataset.skillsId, btn.dataset.action);
    });
  }
  const runBtn = byId('skill-run-btn');
  if (runBtn && !runBtn.dataset.wired) {
    runBtn.dataset.wired = '1';
    runBtn.addEventListener('click', () => {
      const idInput = byId('skill-run-id');
      const id = idInput ? idInput.value.trim() : '';
      if (id) runSkill(id);
      else if (byId('skill-run-note')) {
        byId('skill-run-note').textContent =
          'Enter the exact skill id from the list above.';
      }
    });
  }
}

/* ============================================================
   GOAL CONSOLE (durable goals — server-worker backed)
   ============================================================ */
const GOAL_POLL_MS = 2500;
const goalState = {
  goals: [],
  selected: null,
  detail: null,
  proposals: [],
  proposalsError: null,
  error: null,
  loading: false,
  submitLock: false,
  pollTimer: null,
  pollSeq: 0, // response guard: stale answers are dropped
};

const GOAL_STATUS_LABELS = {
  queued: 'queued', planning: 'planning', ready: 'ready',
  executing: 'executing', awaiting_approval: 'awaiting approval',
  verifying: 'verifying',
  succeeded: 'succeeded', blocked: 'blocked',
  failed: 'failed', cancelled: 'cancelled',
};

function goalStatusClass(status) {
  if (status === 'succeeded') return 'chip chip-ok';
  if (status === 'failed') return 'chip chip-off';
  if (status === 'blocked' || status === 'awaiting_approval') {
    return 'chip chip-plan';
  }
  if (status === 'cancelled') return 'chip chip-off';
  return 'chip chip-live';
}

function goalErrorText(payload, fallback) {
  const err = payload && payload.error;
  if (err) return `${err.code || 'error'}: ${err.message || 'request failed'}`;
  return fallback;
}

async function loadGoals() {
  goalState.loading = true;
  const seq = ++goalState.pollSeq;
  try {
    const resp = await fetch('/api/goals', { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (seq !== goalState.pollSeq) return; // stale response — drop it
    if (resp.ok && payload && Array.isArray(payload.goals)) {
      goalState.goals = payload.goals;
      goalState.error = null;
    } else {
      goalState.error = goalErrorText(payload, `Goal list failed (HTTP ${resp.status}).`);
    }
  } catch (err) {
    if (seq !== goalState.pollSeq) return;
    goalState.error = 'Network error — goal list unavailable.';
  }
  goalState.loading = false;
  renderGoals();
  if (goalState.selected) await loadGoalDetail(goalState.selected);
}

async function loadGoalDetail(id) {
  const seq = ++goalState.pollSeq;
  try {
    const resp = await fetch(`/api/goals/${id}`, { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (seq !== goalState.pollSeq) return;
    if (resp.ok && payload && payload.id === id) {
      goalState.selected = id;
      goalState.detail = payload;
      goalState.error = null;
      await loadGoalProposals(id);
    } else {
      goalState.error = goalErrorText(payload, `Goal detail failed (HTTP ${resp.status}).`);
      goalState.detail = null;
    }
  } catch (err) {
    if (seq !== goalState.pollSeq) return;
    goalState.error = 'Network error — goal detail unavailable.';
  }
  renderGoals();
}

async function loadGoalProposals(id) {
  goalState.proposals = [];
  goalState.proposalsError = null;
  try {
    const resp = await fetch(`/api/goals/${id}/proposals`, { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    if (resp.ok && payload && Array.isArray(payload.proposals)) {
      goalState.proposals = payload.proposals;
    } else if (resp.status !== 404) {
      goalState.proposalsError = goalErrorText(payload,
        `Write proposals unavailable (HTTP ${resp.status}).`);
    }
  } catch (err) {
    goalState.proposalsError = 'Network error — write proposals unavailable.';
  }
}

function renderGoals() {
  const list = byId('goals-list');
  const errBox = byId('goals-error');
  if (!list || !errBox) return;
  errBox.hidden = !goalState.error;
  if (goalState.error) errBox.textContent = goalState.error;

  list.innerHTML = '';
  if (!goalState.goals.length) {
    const li = document.createElement('li');
    li.className = 'goal-item goal-empty';
    li.textContent = 'No durable goals yet — queue one above or from the console.';
    list.appendChild(li);
  }
  for (const goal of goalState.goals) {
    const li = document.createElement('li');
    li.className = 'goal-item';
    li.dataset.goalId = goal.id;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'goal-open';
    if (goal.id === goalState.selected) btn.classList.add('is-active');
    btn.textContent = goal.objective;
    btn.addEventListener('click', () => loadGoalDetail(goal.id));
    li.appendChild(btn);

    const meta = document.createElement('div');
    meta.className = 'goal-meta';
    const chip = document.createElement('span');
    chip.className = goalStatusClass(goal.status);
    chip.textContent = (GOAL_STATUS_LABELS[goal.status] || goal.status)
      + (goal.error_code ? `: ${goal.error_code}` : '');
    meta.appendChild(chip);
    const prog = document.createElement('span');
    prog.className = 'goal-progress';
    prog.textContent = `${goal.progress.done}/${goal.progress.total} steps`;
    meta.appendChild(prog);
    if (goal.mode === 'dynamic') {
      const mode = document.createElement('span');
      mode.className = 'chip chip-inline chip-live';
      mode.textContent = 'dynamic';
      meta.appendChild(mode);
    }
    if (goal.cancel_requested) {
      const cancel = document.createElement('span');
      cancel.className = 'goal-cancel-flag';
      cancel.textContent = 'cancel requested';
      meta.appendChild(cancel);
    }
    const when = document.createElement('span');
    when.className = 'goal-when';
    when.textContent = goal.updated_at || goal.created_at || '';
    meta.appendChild(when);
    li.appendChild(meta);
    list.appendChild(li);
  }
  if (goalState.selected) renderGoalDetail();
  renderHomeGoals();
}

// Phase 9B: independent-review provenance block, textContent only —
// reviewer output is untrusted data and never enters the DOM as markup.
const GOAL_REVIEW_STATUS_LABELS = {
  approved: 'approved',
  revise: 'requested revisions',
  rejected: 'rejected the plan',
  failed: 'review failed',
  unavailable: 'not available',
};

function appendGoalReview(box, review) {
  if (!review) return;
  const label = GOAL_REVIEW_STATUS_LABELS[review.status]
    || review.status || '';
  const bits = [];
  if (review.local_validation !== undefined) {
    bits.push('Local validation: '
      + (review.local_validation ? 'passed' : 'failed'));
  }
  bits.push('Independent review: ' + label);
  if (review.identity) {
    bits.push('Reviewer: ' + review.identity);
  }
  if (review.failure_reason) bits.push('Reason: ' + review.failure_reason);
  if (review.plan_revised) bits.push('Plan was revised and revalidated');
  if (review.round) bits.push('Review round ' + review.round);
  if (Array.isArray(review.findings) && review.findings.length) {
    box.appendChild(goalDetailRow('Review findings',
      review.findings.join(' | ')));
  }
  if (Array.isArray(review.missing_criteria)
    && review.missing_criteria.length) {
    box.appendChild(goalDetailRow('Missing criteria',
      review.missing_criteria.join(' | ')));
  }
  if (Array.isArray(review.recommended_changes)
    && review.recommended_changes.length) {
    box.appendChild(goalDetailRow('Recommended changes',
      review.recommended_changes.join(' | ')));
  }
  if (review.risk_advisory) {
    box.appendChild(goalDetailRow('Reviewer risk (advisory)',
      review.risk_advisory));
  }
  box.appendChild(goalDetailRow('Review', bits.join(' — ')));
}

function goalDetailRow(label, value) {
  const p = document.createElement('p');
  p.className = 'goal-row';
  const strong = document.createElement('strong');
  strong.textContent = label + ': ';
  p.appendChild(strong);
  p.appendChild(document.createTextNode(String(value)));
  return p;
}

// Phase 10A: human write approvals. Proposal content, digests, diffs and
// reviewer-or-model text are untrusted: everything is textContent only.
const PROPOSAL_STATE_LABELS = {
  proposed: 'awaiting your approval',
  approved: 'approved (waiting to run)',
  claimed: 'running',
  executed: 'executed',
  rejected: 'rejected by you',
};

const PROPOSAL_KIND_LABELS = {
  create: 'create',
  edit: 'edit',
  execute: 'execute tests',
};

function renderProposals(body, goal) {
  const box = document.createElement('div');
  box.className = 'goal-proposals';
  const h4 = document.createElement('h4');
  h4.textContent = 'Write approvals';
  box.appendChild(h4);
  const sub = document.createElement('p');
  sub.className = 'goal-row';
  sub.textContent = 'Each proposal is bound to an exact digest. Approve '
    + 'only what you have previewed — execution happens only with your '
    + 'approval of this exact content.';
  box.appendChild(sub);
  for (const proposal of goalState.proposals) {
    const row = document.createElement('div');
    row.className = 'goal-proposal';
    row.dataset.proposalSeq = String(proposal.seq);
    const head = document.createElement('div');
    head.className = 'goal-proposal-head';
    const chip = document.createElement('span');
    chip.className = 'chip chip-plan';
    chip.textContent = `#${proposal.seq} `
      + (PROPOSAL_KIND_LABELS[proposal.kind] || proposal.kind)
      + ` — ${proposal.state}`;
    head.appendChild(chip);
    const path = document.createElement('span');
    path.className = 'goal-proposal-path';
    path.textContent = proposal.path;
    head.appendChild(path);
    row.appendChild(head);
    const meta = document.createElement('p');
    meta.className = 'goal-row goal-proposal-meta';
    meta.textContent = `${proposal.tool} · ${proposal.bytes} bytes · `
      + `before ${proposal.before_sha256 ? proposal.before_sha256.slice(0, 12) : '—'}`
      + ` · after ${proposal.after_sha256.slice(0, 12)}` ;
    row.appendChild(meta);
    if (proposal.state === 'proposed') {
      const btns = document.createElement('div');
      btns.className = 'goal-actions';
      const previewBtn = document.createElement('button');
      previewBtn.type = 'button';
      previewBtn.className = 'btn';
      previewBtn.textContent = 'Preview diff';
      previewBtn.addEventListener('click', () =>
        toggleProposalPreview(row, goal.id, proposal));
      btns.appendChild(previewBtn);
      const approveBtn = document.createElement('button');
      approveBtn.type = 'button';
      approveBtn.className = 'btn btn-approve';
      approveBtn.textContent = 'Approve & execute';
      approveBtn.addEventListener('click', () =>
        proposalAction(goal.id, proposal, 'approve'));
      btns.appendChild(approveBtn);
      const rejectBtn = document.createElement('button');
      rejectBtn.type = 'button';
      rejectBtn.className = 'btn';
      rejectBtn.textContent = 'Reject';
      rejectBtn.addEventListener('click', () =>
        proposalAction(goal.id, proposal, 'reject'));
      btns.appendChild(rejectBtn);
      row.appendChild(btns);
      const preview = document.createElement('div');
      preview.className = 'goal-proposal-preview';
      preview.hidden = true;
      row.appendChild(preview);
    } else {
      const done = document.createElement('p');
      done.className = 'goal-row';
      done.textContent = PROPOSAL_STATE_LABELS[proposal.state]
        || proposal.state;
      row.appendChild(done);
    }
    box.appendChild(row);
  }
  body.appendChild(box);
}

async function toggleProposalPreview(row, goalId, proposal) {
  const box = row.querySelector('.goal-proposal-preview');
  if (!box) return;
  if (!box.hidden) { box.hidden = true; return; }
  box.textContent = 'Loading preview…';
  box.hidden = false;
  try {
    const resp = await fetch(`/api/goals/${goalId}/proposals/${proposal.seq}`,
      { headers: { Accept: 'application/json' } });
    const payload = await resp.json().catch(() => null);
    box.textContent = '';
    if (!resp.ok || !payload || !payload.preview) {
      const p = document.createElement('p');
      p.className = 'goal-row';
      p.textContent = goalErrorText(payload, `Preview failed (HTTP ${resp.status}).`);
      box.appendChild(p);
      return;
    }
    renderProposalPreview(box, payload.preview);
  } catch (err) {
    box.textContent = 'Network error — preview unavailable.';
  }
}

function renderProposalPreview(box, preview) {
  const status = document.createElement('p');
  status.className = 'goal-row';
  const current = preview.current || {};
  const matching = current.matches_approved_before === true;
  status.textContent = `Current file: ${current.exists ? 'exists' : 'absent'}`
    + (matching ? ' — matches the approved before-state'
                : (preview.kind === 'create' ? '' : ' — differs! approval may be refused'));
  box.appendChild(status);
  if (preview.verification) {
    const label = document.createElement('p');
    label.className = 'goal-row goal-proposal-verification';
    const v = preview.verification;
    label.textContent = `Verification recipe: ${v.runner} on ${v.target} `
      + `(cwd ${v.cwd}, timeout ${v.timeout_seconds}s). `
      + `Executes project code: ${v.executes_project_code ? 'yes — '
        + v.risk : 'no'}. Environment: ${v.environment}.`
      + ` Authorized by: ${v.authorized_by}.`;
    box.appendChild(label);
  }
  const contentLabel = document.createElement('p');
  contentLabel.className = 'goal-row';
  if (preview.verification) {
    contentLabel.textContent = 'This approval authorizes the exact test run above — ';
  } else {
    contentLabel.textContent = `Proposed content (${preview.bytes} bytes):`;
  }
  if (preview.verification) {
    box.appendChild(contentLabel);
  }
  if (!preview.verification) {
    const contentPre = document.createElement('pre');
    contentPre.className = 'proposal-content';
    contentPre.textContent = preview.content || '(empty)';
    box.appendChild(contentPre);
  }
  if (preview.diff && preview.diff.length) {
    const diffLabel = document.createElement('p');
    diffLabel.className = 'goal-row';
    diffLabel.textContent = 'Diff (current → proposed):';
    box.appendChild(diffLabel);
    const diffPre = document.createElement('pre');
    diffPre.className = 'proposal-diff';
    diffPre.textContent = preview.diff.join('\n');
    box.appendChild(diffPre);
  }
  if (preview.diff_truncated) {
    const trunc = document.createElement('p');
    trunc.className = 'goal-row';
    trunc.textContent = 'Diff truncated for display only.';
    box.appendChild(trunc);
  }
  const digest = document.createElement('p');
  digest.className = 'goal-row goal-proposal-digest';
  digest.textContent = `Approval digest: ${preview.digest}`;
  box.appendChild(digest);
}

function appendStepEvidence(li, step) {
  const result = step.result;
  if (!result || typeof result !== 'object') return;
  if (result.tool === 'workspace.read_file'
      && typeof result.content === 'string') {
    const p = document.createElement('p');
    p.className = 'goal-row goal-step-evidence';
    p.textContent = `read ${result.total_bytes} bytes`
      + (result.truncated ? ' (truncated preview shown)' : '')
      + ` · sha256 ${result.sha256}`;
    li.appendChild(p);
    const pre = document.createElement('pre');
    pre.className = 'goal-diagnostics';
    pre.textContent = result.content.slice(0, 1600);
    li.appendChild(pre);
    return;
  }
  if (result.recipe) {
    const p = document.createElement('p');
    p.className = 'goal-row goal-step-evidence';
    p.textContent = `recipe ${result.recipe} · target ${result.target}`
      + ` · exit ${result.exit_code === null ? '—' : result.exit_code}`
      + ` · ${result.duration_seconds}s`
      + ` · state: ${result.status}`
      + (result.truncated ? ' · output truncated' : '');
    li.appendChild(p);
    const diag = [result.stdout, result.stderr]
      .filter((text) => text && text.trim()).join('\n');
    if (diag) {
      const pre = document.createElement('pre');
      pre.className = 'goal-diagnostics';
      pre.textContent = diag.slice(0, 2000);
      li.appendChild(pre);
    }
  }
}

async function proposalAction(goalId, proposal, action) {
  if (goalState.submitLock) return;
  goalState.submitLock = true;
  try {
    const resp = await fetch(`/api/goals/${goalId}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({
        seq: proposal.seq,
        digest: proposal.digest,
      }),
    });
    const payload = await resp.json().catch(() => null);
    if (resp.ok) {
      goalState.error = null;
      toast(action === 'approve'
        ? 'Proposal approved — the worker will execute it'
        : 'Proposal rejected — the goal will not run');
    } else if (action === 'reject') {
      goalState.error = goalErrorText(payload, `Rejection failed (HTTP ${resp.status}).`);
      toast('Rejection failed');
    } else {
      goalState.error = goalErrorText(payload, `Approval failed (HTTP ${resp.status}).`);
      toast('Approval refused — see the message');
    }
  } catch (err) {
    goalState.error = `Network error — proposal ${action} failed.`;
    toast('Proposal action failed');
  }
  goalState.submitLock = false;
  await loadGoals();
}

function renderGoalDetail() {
  const body = byId('goal-detail');
  const goal = goalState.detail;
  if (!body || !goal) { body.hidden = true; return; }
  body.hidden = false;
  body.innerHTML = '';

  const head = document.createElement('header');
  head.className = 'goal-detail-head';
  const title = document.createElement('h3');
  title.textContent = goal.objective;
  head.appendChild(title);
  const chip = document.createElement('span');
  chip.className = goalStatusClass(goal.status);
  chip.textContent = GOAL_STATUS_LABELS[goal.status] || goal.status;
  head.appendChild(chip);
  body.appendChild(head);

  body.appendChild(goalDetailRow('Goal id', goal.id));
  body.appendChild(goalDetailRow(
    'Execution mode', goal.mode === 'dynamic'
      ? 'dynamic (bounded observe → decide loop with a verified success contract)'
      : 'sequential (plan first, then execute)'));
  if (goal.error_code) body.appendChild(goalDetailRow('Blocked/failed reason', goal.error_code));
  const prog = goal.progress || { total: 0, done: 0 };
  body.appendChild(goalDetailRow('Steps done', `${prog.done}/${prog.total}`));

  const actions = document.createElement('div');
  actions.className = 'goal-actions';
  const cancellable = !['succeeded', 'failed', 'cancelled'].includes(goal.status);
  const retryable = goal.status === 'blocked'
    && goal.error_code !== 'reconciliation_required';
  if (cancellable) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn';
    btn.textContent = 'Cancel goal';
    btn.addEventListener('click', () => goalAction(goal.id, 'cancel'));
    actions.appendChild(btn);
  }
  if (retryable) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn';
    btn.textContent = 'Retry from blocked';
    btn.addEventListener('click', () => goalAction(goal.id, 'retry'));
    actions.appendChild(btn);
  }
  if (cancellable || retryable) body.appendChild(actions);

  if (goalState.proposalsError) {
    const err = document.createElement('p');
    err.className = 'goal-row';
    err.textContent = goalState.proposalsError;
    body.appendChild(err);
  }
  if (goalState.proposals.length) renderProposals(body, goal);

  if (goal.plan && goal.plan.summary) {
    const box = document.createElement('div');
    box.className = 'goal-plan';
    const h4 = document.createElement('h4');
    h4.textContent = 'Plan';
    box.appendChild(h4);
    box.appendChild(goalDetailRow('Summary', goal.plan.summary));
    box.appendChild(goalDetailRow('Outcome', goal.plan.outcome));
    box.appendChild(goalDetailRow('Risk', goal.plan.risk));
    if (typeof goal.repair_count === 'number' && goal.repair_count > 0) {
      box.appendChild(goalDetailRow(
        'Repair cycles used',
        `${goal.repair_count} of 2 — previous verification attempt is`
        + ' preserved in the goal record and events'));
      if (goal.last_failure) {
        box.appendChild(goalDetailRow(
          'Previous verification failure recorded',
          `seq ${goal.last_failure.seq}: ${goal.last_failure.tool}`
          + ` — ${goal.last_failure.status} (exit`
          + ` ${goal.last_failure.exit_code})`));
      }
    }
    (goal.plan.criteria || []).forEach((crit) => {
      const li = document.createElement('li');
      li.textContent = crit;
      box.appendChild(li);
    });
    appendGoalReview(box, goal.plan.review);
    body.appendChild(box);
  }

  if (Array.isArray(goal.steps) && goal.steps.length) {
    const box = document.createElement('div');
    box.className = 'goal-steps';
    const h4 = document.createElement('h4');
    h4.textContent = 'Steps';
    box.appendChild(h4);
    const ul = document.createElement('ul');
    for (const step of goal.steps) {
      const li = document.createElement('li');
      const chip = document.createElement('span');
      chip.className = goalStatusClass(step.status);
      chip.textContent = `#${step.seq} ${step.status}`;
      li.appendChild(chip);
      const span = document.createElement('span');
      span.className = 'goal-step-purpose';
      span.textContent = step.purpose;
      li.appendChild(span);
      if (step.verification) {
        const v = document.createElement('span');
        v.className = 'goal-step-verification';
        v.textContent = ` — ${step.verification}`;
        li.appendChild(v);
      }
      appendStepEvidence(li, step);
      ul.appendChild(li);
    }
    box.appendChild(ul);
    body.appendChild(box);
  }

  if (goal.report) {
    const box = document.createElement('div');
    box.className = 'goal-report';
    const h4 = document.createElement('h4');
    h4.textContent = 'Report';
    box.appendChild(h4);
    box.appendChild(goalDetailRow('Status', goal.report.status));
    if (goal.report.outcome) box.appendChild(goalDetailRow('Outcome', goal.report.outcome));
    if (goal.report.review) box.appendChild(goalDetailRow('Review', goal.report.review));
    (goal.report.criteria || []).forEach((crit) => {
      const li = document.createElement('li');
      const mark = crit.passed ? '✔' : '✗';
      li.textContent = `${mark} ${crit.criterion || 'criterion'}`;
      box.appendChild(li);
    });
    body.appendChild(box);
  }

  if (Array.isArray(goal.events) && goal.events.length) {
    const box = document.createElement('div');
    box.className = 'goal-events';
    const h4 = document.createElement('h4');
    h4.textContent = 'Events';
    box.appendChild(h4);
    const ul = document.createElement('ul');
    for (const ev of goal.events.slice(-12)) {
      const li = document.createElement('li');
      li.textContent = `${ev.ts} ${ev.event}${ev.detail ? ` — ${ev.detail}` : ''}`;
      ul.appendChild(li);
    }
    box.appendChild(ul);
    body.appendChild(box);
  }
}

async function goalAction(id, action) {
  if (goalState.submitLock) return; // duplicate-submit guard
  goalState.submitLock = true;
  try {
    const resp = await fetch(`/api/goals/${id}/${action}`, {
      method: 'POST',
      headers: { Accept: 'application/json' },
    });
    const payload = await resp.json().catch(() => null);
    if (resp.ok) {
      goalState.error = null;
      if ((payload && payload.status) === 'cancelled') toast('Goal cancelled');
      else toast(`Goal ${action} accepted`);
    } else {
      goalState.error = goalErrorText(
        payload, `Goal ${action} failed (HTTP ${resp.status}).`);
      toast('Goal action failed');
    }
  } catch (err) {
    goalState.error = `Network error — goal ${action} failed.`;
    toast('Goal action failed');
  }
  goalState.submitLock = false;
  await loadGoals();
}

async function submitGoal(event) {
  event.preventDefault();
  if (goalState.submitLock) return;
  const box = byId('goal-objective');
  const objective = (box.value || '').trim();
  if (!objective) return;
  goalState.submitLock = true;
  const btn = byId('goal-submit');
  if (btn) btn.disabled = true;
  const modeSelect = byId('goal-mode');
  const mode = modeSelect ? modeSelect.value : 'sequential';
  try {
    const resp = await fetch('/api/goals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ objective, mode }),
    });
    const payload = await resp.json().catch(() => null);
    if (resp.status === 202 && payload && payload.id) {
      box.value = '';
      toast('Goal queued — the worker will plan and execute it');
      goalState.selected = payload.id;
      await loadGoals();
    } else {
      goalState.error = goalErrorText(
        payload,
        resp.status === 503
          ? 'The AI provider is not configured — goals need it for planning.'
          : `Goal submission failed (HTTP ${resp.status}).`);
      renderGoals();
    }
  } catch (err) {
    goalState.error = 'Network error — goal not queued.';
    renderGoals();
  }
  goalState.submitLock = false;
  if (btn) btn.disabled = false;
}

function goalsPollTick() {
  const dlg = byId('modal-goals');
  if (!dlg || !dlg.open) return; // polling only while the console is open
  const active = goalState.goals.some(
    (g) => !['succeeded', 'failed', 'cancelled'].includes(g.status));
  // refresh when anything is in motion, or lazily when a detail is open
  if (goalState.goals.length === 0 || active || goalState.selected) {
    loadGoals();
  }
}

function startGoalsPolling() {
  if (goalState.pollTimer) return;
  goalState.pollTimer = setInterval(goalsPollTick, GOAL_POLL_MS);
}

function stopGoalsPolling() {
  // closing the dialog also invalidates in-flight polls via pollSeq
  goalState.pollSeq++;
  if (goalState.pollTimer) {
    clearInterval(goalState.pollTimer);
    goalState.pollTimer = null;
  }
}

function openGoalsModal() {
  initModelsModal();
  openModal('modal-goals');
  goalState.selected = null;
  goalState.detail = null;
  loadGoals();
  startGoalsPolling();
}

function initGoalsModal() {
  const legacy = byId('llm-goals'); // removed in 11B; guard kept for safety
  if (legacy) legacy.addEventListener('click', openGoalsModal);
  const form = byId('goal-form');
  if (form) form.addEventListener('submit', submitGoal);
  const dlg = byId('modal-goals');
  if (dlg) dlg.addEventListener('close', stopGoalsPolling);
}

/* ============================================================
   QUICK ACTIONS & TASKS
   ============================================================ */
function initQuick() {
  byId('task-new-btn').addEventListener('click', () => {
    openModal('modal-task');
    setTimeout(() => byId('task-title-input').focus(), 50);
  });
  byId('home-new-goal').addEventListener('click', openGoalsModal);
  byId('home-open-chat').addEventListener('click', () => openModal('modal-chat'));
  byId('home-goals-all').addEventListener('click', openGoalsModal);
  byId('brief-btn').addEventListener('click', openBriefing);
  byId('updates-check-btn').addEventListener('click', async () => {
    try {
      await fetch('/api/updates/check', { method: 'POST' });
    } catch (err) { /* silent: the re-render shows the honest state */ }
    refreshUpdImprovements();
  });
  byId('improvements-scan-btn').addEventListener('click', async () => {
    try {
      await fetch('/api/improvements/propose', { method: 'POST' });
    } catch (err) { /* the re-render shows the honest state */ }
    refreshUpdImprovements();
  });

  initModelsModal();
  initSettingsModal();
  initGoalsModal();

  $$('.seg-btn[data-day]').forEach((b) => b.addEventListener('click', () => {
    state.day = b.dataset.day;
    savePrefs({ day: b.dataset.day });
    renderTimeline();
  }));

  byId('task-close').addEventListener('click', () => closeModal('modal-task'));
  byId('task-form').addEventListener('submit', (e) => {
    e.preventDefault();
    submitTaskForm();
  });
}

/* ============================================================
   CHAT — conversation lifecycle (real inference + persisted history)
   ============================================================ */
function mockReply(text) {
  const t = text.toLowerCase();
  const has = (words) => words.split('|').some((w) => t.includes(w));
  if (has('hello|hi|hey|greet')) return 'Good to see you, Commander. This workspace talks to your local JARVIS server — without configured provider credentials every reply you read here is this scripted fallback.';
  if (has('time|date|clock')) return `The browser clock reads ${new Date().toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}. Tasks in the timeline are real saved records from the local server — there is no calendar integration.`;
  if (has('task|todo|timeline|schedule')) {
    const c = state.statusCounts;
    return c
      ? `The task store currently holds ${fmtInt(Object.values(c).reduce((a, b) => a + b, 0))} task(s): ${fmtInt(c.queued)} queued, ${fmtInt(c.warn)} high-prio, ${fmtInt(c.done)} done, ${fmtInt(c.planned)} planned — exact counts from the local server. Add more with the “New task” button; they persist across reloads.`
      : 'Task counts are loading from the local server (or the server is unreachable). Tasks added with the “New task” button persist across reloads.';
  }
  if (has('status|system|cpu|ram|disk|health|monitor')) {
    if (state.metricsError && state.metricsLoaded) {
      return 'System telemetry is currently unreachable from the local server — no numbers are shown rather than stale or invented ones.';
    }
    if (!state.metricsLoaded) return 'System telemetry is loading from the local server…';
    const s = state.metrics;
    const res = (key) => ((s && s[key] && s[key].available)
      ? `${Math.round(Math.max(0, Math.min(100, s[key].percent)))}%`
      : 'pending');
    return `System monitor shows live local sampling — CPU ${res('cpu')} · RAM ${res('memory')} · Disk ${res('disk')} (${s.disk.available ? 'filesystem containing the data directory' : 'disk pending'}). CPU needs two samples before its first percentage appears.`;
  }
  if (has('provider|llm|model|omniroute|freellm|groq|ollama|gemini|codex|claude|opencode')) return 'The topbar model chip shows the effective model policy from the local /api/status endpoint. Configure providers and keys in Settings — keys are write-only and never displayed.';
  if (has('memory|vault|vector|obsidian|supabase')) return 'No memory store or Obsidian vault is connected. The Obsidian Vault and Knowledge Graph views show honest setup states — backend integration is documented in the roadmap.';
  if (has('voice|listen|speak|talk|microphone')) return 'Voice is not implemented in this build: no microphone permission is requested and no audio is recorded. That is planned for a later phase.';
  if (has('agent')) return 'There are no demo agent roles in this build. Real durable goals run on the local server worker with registered tools, human approval gates and verification.';
  if (has('workflow|automat')) return 'Multi-goal workflows are not implemented. Durable goals are the working unit — queue one from the Goal Console.';
  if (has('focus')) return 'Focus Mode lives at the bottom of the sidebar. It hides the secondary home panels — presentation only, no system changes.';
  if (has('brief|report|summar')) return 'Open the Executive Briefing from the Diagnostics view — it is compiled from live status, telemetry, tasks and goals, never generated by AI.';
  if (has('help|what can|command')) return 'Try asking about: tasks, system status, providers, goals, or the briefing. Everything I answer is scripted fallback text until a provider is configured.';
  const seeds = [
    'Understood. I can walk you through tasks, goals, telemetry and configuration — ask about any of them.',
    'Noted, Commander. These replies are the scripted fallback; configure a provider in Settings for real model replies.',
    'Logged. For executed outcomes, queue a durable goal — planning, tools and verification run on the local server.',
  ];
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) >>> 0;
  return seeds[h % seeds.length];
}

function buildChatMsg(text, who, tagText, opts) {
  const msg = document.createElement('div');
  msg.className = `chat-msg chat-msg--${who}`;
  const canRender = who === 'jarvis'
    && !(opts && opts.plain)
    && typeof window.JARVIS_FORMAT === 'object';
  if (canRender) {
    const html = window.JARVIS_FORMAT.renderMarkdown(text);
    const body = document.createElement('div');
    body.className = 'chat-body';
    if (html) {
      body.innerHTML = html;
    } else {
      /* assistant output was reasoning/control tokens only */
      body.className = 'chat-body chat-silent';
      body.textContent = 'The assistant returned internal reasoning only, with no visible reply. Try rephrasing.';
    }
    msg.appendChild(body);
  } else {
    /* user + error messages stay plain text (frozen bubble design) */
    msg.textContent = text;
  }
  if (who === 'jarvis' && tagText) {
    const tag = document.createElement('span');
    tag.className = 'chat-msg--tag';
    tag.textContent = tagText;
    msg.appendChild(tag);
  }
  return msg;
}

function appendChat(text, who, tagText, opts) {
  const log = byId('chat-log');
  log.appendChild(buildChatMsg(text, who, tagText, opts));
  log.scrollTop = log.scrollHeight;
}

/* ---------- chat log states (empty / recoverable error / transcript) ---- */
function clearChatLog() {
  byId('chat-log').innerHTML = '';
}

function showChatEmpty(message) {
  clearChatLog();
  const note = document.createElement('div');
  note.className = 'chat-empty';
  note.textContent = message;
  byId('chat-log').appendChild(note);
}

function showChatError(message, retryLabel, retryFn) {
  clearChatLog();
  const box = document.createElement('div');
  box.className = 'chat-empty';
  const p = document.createElement('p');
  p.textContent = message;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'chat-retry';
  btn.textContent = retryLabel || 'Retry';
  btn.addEventListener('click', retryFn);
  box.append(p, btn);
  byId('chat-log').appendChild(box);
}

/* Restores a persisted conversation through the same safe rendering
   pipeline as live replies. No per-message model label is fabricated:
   the backend does not store which upstream model answered. */
function renderTranscript(payload) {
  const log = byId('chat-log');
  clearChatLog();
  if (payload.older_omitted) {
    const note = document.createElement('div');
    note.className = 'chat-empty chat-window-note';
    note.textContent = `Showing the most recent ${payload.messages.length} of `
      + `${payload.messages_total} messages — older history remains on the server.`;
    log.appendChild(note);
  }
  payload.messages.forEach((m) => {
    log.appendChild(buildChatMsg(m.content, m.role === 'assistant' ? 'jarvis' : 'user'));
  });
  if (!log.children.length) {
    showChatEmpty('This conversation has no messages yet.');
  }
  log.scrollTop = log.scrollHeight;
}

async function fetchMessages(convId) {
  const res = await fetch(`/api/conversations/${encodeURIComponent(convId)}/messages`);
  let payload = null;
  try { payload = await res.json(); } catch (err) { payload = null; }
  return { ok: res.ok, status: res.status, payload };
}

async function loadConversation(convId, { saveSession = true } = {}) {
  const token = ++state.convToken;
  const log = byId('chat-log');
  clearChatLog();
  const loading = document.createElement('div');
  loading.className = 'chat-empty';
  loading.textContent = 'Loading conversation…';
  log.appendChild(loading);
  log.scrollTop = log.scrollHeight;
  try {
    const { ok, status, payload } = await fetchMessages(convId);
    if (token !== state.convToken) return { applied: false };
    if (ok && payload && Array.isArray(payload.messages)) {
      state.conversationId = convId;
      if (saveSession) saveActiveConv(convId);
      renderTranscript(payload);
      return { applied: true };
    }
    if (status === 404) {
      state.conversationId = null;
      clearActiveConv();
      showChatEmpty('That conversation no longer exists on the server. '
        + 'Start a new one, or pick another from history.');
      return { applied: true };
    }
    showChatError(payload && payload.error && payload.error.message
      ? payload.error.message
      : 'The conversation could not be loaded.', 'Retry',
      () => loadConversation(convId, { saveSession }));
    return { applied: true };
  } catch (err) {
    if (token !== state.convToken) return { applied: false };
    /* server unreachable: keep the saved id so a retry can still recover */
    showChatError('Could not reach the JARVIS server. Your saved '
      + 'conversation was kept.', 'Retry',
      () => loadConversation(convId, { saveSession }));
    return { applied: true };
  }
}

/* ---------- conversation history panel ---------- */
function formatConvWhen(iso) {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return '';
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  });
}

async function refreshHistory() {
  const list = byId('conv-list');
  list.innerHTML = '';
  const loading = document.createElement('div');
  loading.className = 'conv-note';
  loading.textContent = 'Loading conversations…';
  list.appendChild(loading);
  let payload = null;
  try {
    const res = await fetch('/api/conversations');
    try { payload = await res.json(); } catch (err) { payload = null; }
    if (!res.ok || !payload || !Array.isArray(payload.conversations)) {
      throw new Error('bad response');
    }
  } catch (err) {
    list.innerHTML = '';
    const li = document.createElement('li');
    li.className = 'conv-note conv-recoverable';
    const p = document.createElement('p');
    p.textContent = 'Conversation history could not be loaded.';
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'chat-retry';
    retry.textContent = 'Retry';
    retry.addEventListener('click', refreshHistory);
    li.append(p, retry);
    list.appendChild(li);
    return;
  }
  list.innerHTML = '';
  if (!payload.conversations.length) {
    const li = document.createElement('li');
    li.className = 'conv-note';
    li.textContent = 'No saved conversations yet. Send a message to begin.';
    list.appendChild(li);
    return;
  }
  payload.conversations.forEach((conv) => {
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'conv-item'
      + (conv.id === state.conversationId ? ' is-active' : '');
    const title = document.createElement('span');
    title.className = 'conv-title';
    title.textContent = conv.title || '(untitled)';
    const meta = document.createElement('span');
    meta.className = 'conv-meta';
    meta.textContent = `${formatConvWhen(conv.updated_at)} · ${conv.message_count} messages`;
    const preview = document.createElement('span');
    preview.className = 'conv-preview';
    preview.textContent = conv.preview || '';
    btn.append(title, meta, preview);
    btn.addEventListener('click', () => selectConversation(conv.id));
    li.appendChild(btn);
    list.appendChild(li);
  });
  if (payload.truncated) {
    const li = document.createElement('li');
    li.className = 'conv-note';
    li.textContent = 'Older conversations exist beyond this list.';
    list.appendChild(li);
  }
}

function showHistory() {
  byId('conv-panel').hidden = false;
  byId('chat-history').setAttribute('aria-expanded', 'true');
  refreshHistory();
}
function hideHistory() {
  byId('conv-panel').hidden = true;
  byId('chat-history').setAttribute('aria-expanded', 'false');
}
function toggleHistory() {
  if (byId('conv-panel').hidden) showHistory();
  else hideHistory();
}

const PENDING_SWITCH_WARNING = 'A reply is still being prepared. Switch '
  + 'anyway? It will be filed under its own conversation in history.';

async function selectConversation(convId) {
  if (convId === state.conversationId) { hideHistory(); return; }
  if (state.chatPending) {
    if (!window.confirm(PENDING_SWITCH_WARNING)) return;
  }
  const result = await loadConversation(convId);
  if (result.applied) hideHistory();
}

/* ---------- new conversation ---------- */
function newConversation() {
  if (state.chatPending) {
    if (!window.confirm(PENDING_SWITCH_WARNING)) return;
  }
  state.convToken++; // retire any in-flight transcript load
  state.conversationId = null;
  clearActiveConv();
  hideHistory();
  showChatEmpty('New conversation. Send a message to begin.');
}

/* Phase 3B — deterministic reload recovery for this browser tab.
   Never fabricates content; an unreachable server keeps the saved id
   with an explicit retry path. */
async function restoreConversation() {
  const saved = readSavedConv();
  if (!saved) {
    showChatEmpty('Start the conversation — replies come from the locally configured model.');
    return;
  }
  if (!isValidConvId(saved)) {
    clearActiveConv();
    showChatEmpty('Start the conversation — replies come from the locally configured model.');
    return;
  }
  await loadConversation(saved);
}

function setChatPending(pending) {
  ['chat-input', 'chat-input-modal'].forEach((id) => {
    const el = byId(id);
    if (el) el.disabled = pending;
  });
  $$('#chat-form button, #chat-form-modal button').forEach((btn) => {
    btn.disabled = pending;
  });
}

/* Disabling the focused composer drops focus to <body>; give it back on
   completion unless focus was deliberately moved elsewhere. When the
   conversation modal is open (dock submissions open it), that composer is
   the appropriate continuation point. */
function restoreComposerFocus(inputEl) {
  const modal = byId('modal-chat');
  if (modal && modal.open && inputEl !== byId('chat-input-modal')) {
    inputEl = byId('chat-input-modal');
  }
  if (!inputEl || inputEl.disabled) return;
  const dlg = inputEl.closest('dialog');
  if (dlg && !dlg.open) return;
  const visible = typeof inputEl.checkVisibility === 'function'
    ? inputEl.checkVisibility()
    : inputEl.offsetParent !== null;
  if (!visible) return;
  const active = document.activeElement;
  if (active && active !== document.body) {
    const insideModal = modal && modal.open && modal.contains(active);
    if (!(dlg && dlg.contains(inputEl) && insideModal)) return;
  }
  inputEl.focus();
  const end = inputEl.value.length;
  inputEl.setSelectionRange(end, end);
}

async function handleChatSubmit(text, inputEl) {
  const clean = text.trim();
  if (!clean || state.chatPending) return;
  state.chatPending = true;
  setChatPending(true);
  inputEl.value = '';
  appendChat(clean, 'user');
  /* token snapshot: a conversation switch while this request is in flight
     retires it visually so a late reply can never be misattributed */
  const submitToken = state.convToken;
  const typing = document.createElement('div');
  typing.className = 'chat-msg chat-msg--jarvis chat-typing';
  typing.innerHTML = '<span></span><span></span><span></span>';
  const log = byId('chat-log');
  log.appendChild(typing);
  log.scrollTop = log.scrollHeight;
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: clean,
        conversation_id: state.conversationId,
      }),
    });
    let payload = null;
    try { payload = await res.json(); } catch (err) { payload = null; }
    const switched = state.convToken !== submitToken;
    if (res.ok && payload && typeof payload.reply === 'string' && payload.reply) {
      if (!switched) {
        if (payload.conversation_id) {
          state.conversationId = payload.conversation_id;
          saveActiveConv(payload.conversation_id);
        }
        typing.remove();
        appendChat(payload.reply, 'jarvis', `AI reply · ${payload.model || 'model'}`);
      } else {
        typing.remove();
        toast('Reply arrived after you switched conversations — it is in history.');
      }
      // Phase 13B: opt-in speech output of the COMPLETED reply only.
      // Never partials, errors, or history replays.
      if (!switched) speakReply(payload.reply);
    } else {
      const message = payload && payload.error && payload.error.message
        ? payload.error.message
        : 'JARVIS could not complete the chat request.';
      if (!switched && payload && payload.conversation_id) {
        state.conversationId = payload.conversation_id;
      }
      typing.remove();
      appendChat(message, 'jarvis', 'Chat error', { plain: true });
    }
  } catch (err) {
    typing.remove();
    appendChat('JARVIS could not reach its chat backend.', 'jarvis', 'Chat error', { plain: true });
  } finally {
    state.chatPending = false;
    setChatPending(false);
    restoreComposerFocus(inputEl);
    log.scrollTop = log.scrollHeight;
  }
}

function initChat() {
  byId('chat-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = byId('chat-input');
    if (!input.value.trim()) return;
    openModal('modal-chat');
    handleChatSubmit(input.value, input);
  });
  byId('chat-form-modal').addEventListener('submit', (e) => {
    e.preventDefault();
    handleChatSubmit(byId('chat-input-modal').value, byId('chat-input-modal'));
  });
  byId('chat-close').addEventListener('click', () => closeModal('modal-chat'));
  byId('chat-history').addEventListener('click', toggleHistory);
  byId('chat-new').addEventListener('click', newConversation);
  restoreConversation();
}

/* ============================================================
   EXECUTIVE BRIEFING (Phase 4) — deterministic application-status
   summary. Facts come from GET /api/status, /api/metrics and the
   persisted task aggregates; unavailable metrics are stated, never
   substituted with invented numbers. It is not AI-generated.
   ============================================================ */
function metricsSummaryLine() {
  if (state.metricsError && state.metricsLoaded) {
    return 'Telemetry is currently unreachable — showing no numbers rather than old or made-up ones.';
  }
  const m = state.metrics;
  const boot = !state.metricsLoaded;
  const part = (key, suffix) => {
    const s = m && m[key];
    if (!s) return `${suffix} pending…`;
    if (!s.available) return s.sampling ? `${suffix} sampling…` : `${suffix} unavailable`;
    return `${suffix} ${Math.round(Math.max(0, Math.min(100, s.percent)))}%`;
  };
  if (boot) {
    return 'Loading local telemetry from the server…';
  }
  return `${part('cpu', 'CPU')} · ${part('memory', 'RAM')} · ${part('disk', 'Disk')} (live local sampling; disk is the filesystem containing the JARVIS data directory, not app-only usage).`;
}

function taskSummaryLine() {
  if (state.tasksError) {
    return 'Task data is currently unreachable — the local server did not answer.';
  }
  const c = state.statusCounts;
  if (!c) {
    return 'Loading saved task counts from the server…';
  }
  const total = Object.values(c).reduce((a, b) => a + b, 0);
  if (total === 0) {
    return 'No tasks saved yet — create one with Start New Task; it will persist on the local server.';
  }
  return `${fmtInt(total)} task(s) saved — ${fmtInt(c.queued)} queued, ${fmtInt(c.warn)} high-prio, ${fmtInt(c.done)} done, ${fmtInt(c.planned)} planned (exact totals from the server, not a page slice).`;
}

function statusSummaryLine() {
  if (!state.lastStatus) return 'Application status is loading…';
  const prefix = state.lastStatusError
    ? 'Last known application status (fresher polls are failing): '
    : '';
  const s = state.lastStatus;
  const dbState = s.db === 'ready' ? 'database ready' : `database degraded (${s.db})`;
  // configured means credentials exist server-side — presence is never
  // presented as a verified upstream connection
  const ai = s.provider_configured
    ? 'AI provider credentials present (configuration only, not a verified upstream connection)'
    : 'no AI provider credentials configured — replies come from the scripted demo fallback';
  // Phase 7A: name the effective policy + its source, and verified
  // inference separately (a catalog listing is never proof of inference)
  let modelLine = `Model policy: “${s.effective_model ?? s.model_policy ?? 'unknown'}”`
    + (s.model_source === 'runtime_override' ? ' (runtime override)' : '');
  if (s.last_inference && s.last_inference.model) {
    modelLine += ` · last upstream reply verified via ${s.last_inference.model}`;
  }
  return `${prefix}Local server is reachable; ${dbState}; schema v${s.schema_version ?? '?'}; ${ai}. ${modelLine}.`;
}

function renderBriefing() {
  const conv = state.convCountBounded
    ? 'more conversations than the badge shows are persisted (badge capped)'
    : (typeof state.convCount === 'number'
      ? `${fmtInt(state.convCount)} conversation(s) persisted`
      : 'conversation count currently unavailable');
  const goals = (goalState.goals || []).length;
  byId('brief-body').innerHTML = `
    <p><strong>Operator briefing</strong> — compiled at ${new Date().toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })} from live application data (status, telemetry, saved tasks, goals). Not AI-generated.</p>
    <ul>
      <li><strong>Application.</strong> ${statusSummaryLine()}</li>
      <li><strong>Saved tasks.</strong> ${taskSummaryLine()}</li>
      <li><strong>Local system.</strong> ${metricsSummaryLine()}</li>
      <li><strong>Conversations.</strong> ${conv}. Transcripts reopen from history.</li>
      <li><strong>Durable goals.</strong> ${goals ? `${fmtInt(goals)} goal(s) currently listed — see the Goal Console for persisted step state.` : 'No durable goals queued yet.'}</li>
      <li><strong>Chat.</strong> Live via the configured local model; transcripts persist in the local database.</li>
    </ul>
    <p style="margin-top:12px"><strong>Reminder.</strong> Chat performs real inference through the local backend. Status, tasks, goals and system telemetry are real server data; there are no demo panels in this build.</p>`;
}

function openBriefing() {
  renderBriefing();
  openModal('modal-brief');
}

function rerenderBriefingIfOpen() {
  const modal = byId('modal-brief');
  if (modal && modal.open) renderBriefing();
}

/* ============================================================
   INIT
   ============================================================ */
let SEARCH_INDEX = [];

/* ============================================================
   VOICE (Phase 13B) — push-to-talk browser speech input and
   opt-in speech output. Frontend-only: recognition and synthesis
   are browser APIs, never a JARVIS server feature; audio is never
   uploaded or stored by JARVIS. A transcript lands editable in
   the existing composer and is submitted only through the normal
   chat flow — voice can never bypass approvals, goals, or tools.
   ============================================================ */

const voiceState = {
  supported: null,       // SpeechRecognition present?
  rec: null,             // live recognition instance (one at a time)
  listening: false,      // truthful UI state
  inputEl: null,         // composer the current session writes into
  statusEl: null,
  consumed: 0,           // result events already appended (duplicate guard)
  speakAckTimer: null,
};

function voicePrefs() {
  const prefs = readPrefs();
  return {
    lang: typeof prefs.voiceLang === 'string' ? prefs.voiceLang : 'en-US',
    speakReplies: prefs.speakReplies === true,
    privacyAck: prefs.voicePrivacyAck === true,
  };
}

function voiceSetStatus(text, tone) {
  [byId('voice-status'), byId('voice-status-modal')].forEach((el) => {
    if (!el) return;
    el.textContent = text || '';
    el.classList.toggle('is-error', tone === 'error');
    el.classList.toggle('is-active', tone === 'active');
  });
}

function syncSpeakButtons() {
  const on = voicePrefs().speakReplies;
  ['voice-speak', 'voice-speak-modal'].forEach((id) => {
    const btn = byId(id);
    if (!btn) return;
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.setAttribute('aria-label', on ? 'Speak replies: on (click to stop speaking)' : 'Speak replies: off');
  });
}

function setListeningUi(on, inputEl, keepStatus) {
  voiceState.listening = on;
  ['voice-mic', 'voice-mic-modal'].forEach((id) => {
    const btn = byId(id);
    if (!btn) return;
    btn.classList.toggle('is-listening', on);
    btn.setAttribute('aria-label', on ? 'Stop voice input' : 'Start voice input');
  });
  if (on) {
    const where = inputEl && inputEl.id === 'chat-input-modal'
      ? 'conversation panel' : 'composer';
    voiceSetStatus(`Listening (${where})… speak now — the transcript stays editable and nothing is sent until you press Send.`, 'active');
  } else if (!keepStatus) {
    voiceSetStatus('');
  }
}

function stopVoiceRecognition(silent) {
  const rec = voiceState.rec;
  voiceState.rec = null;
  if (voiceState.listening || rec) setListeningUi(false);
  if (rec) {
    try { rec.stop(); } catch (err) { /* already ended */ }
  }
  if (!silent) voiceSetStatus('');
}

function startVoiceRecognition(inputEl, lang) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    voiceSetStatus('Voice input is not supported in this browser. Type your message instead — text chat is fully functional.', 'error');
    return;
  }
  if (voiceState.listening) { stopVoiceRecognition(); return; }
  const prefs = voicePrefs();
  if (!prefs.privacyAck) {
    // Privacy disclosure BEFORE first recognition: the browser speech
    // feature may use an external service; JARVIS never receives or
    // stores audio either way.
    const ok = window.confirm(
      'Voice input uses your browser\u2019s speech recognition. Depending on your browser, audio may be sent to an external recognition service \u2014 it is not guaranteed to stay on this device or work offline. JARVIS itself never records, uploads, or stores audio; only text you review and explicitly send enters the chat.\n\nEnable voice input?'
    );
    if (!ok) {
      voiceSetStatus('Voice input stays off. You can enable it anytime from the microphone button.', 'error');
      return;
    }
    savePrefs({ voicePrivacyAck: true });
  }
  let rec;
  try {
    rec = new SR();
  } catch (err) {
    voiceSetStatus('Voice input could not start in this browser. Text chat is fully functional.', 'error');
    return;
  }
  rec.lang = lang || prefs.lang;
  rec.continuous = false;   // push-to-talk: one utterance, never always-on
  rec.interimResults = true;
  rec.maxAlternatives = 1;
  voiceState.rec = rec;
  voiceState.inputEl = inputEl;
  voiceState.consumed = 0;
  rec.onresult = (event) => {
    // Result events resend earlier results; only consume new indices so
    // duplicate callbacks never duplicate transcript text.
    let appended = false;
    for (let i = voiceState.consumed; i < event.results.length; i += 1) {
      const result = event.results[i];
      if (!result || !result[0]) continue;
      const transcript = String(result[0].transcript || '');
      if (result.isFinal && transcript.trim()) {
        const current = inputEl.value;
        const needsSpace = current && !/\s$/.test(current);
        inputEl.value = current + (needsSpace ? ' ' : '') + transcript.trim();
        inputEl.dataset.dirty = '1';
        appended = true;
      } else if (!result.isFinal && transcript.trim()) {
        voiceSetStatus(`Hearing: ${transcript.trim()} — final text lands in the composer for review.`, 'active');
      }
    }
    voiceState.consumed = event.results.length;
    if (appended) {
      voiceSetStatus('Speech recognized — review or edit the text in the composer, then press Send to submit.', 'active');
    }
  };
  rec.onerror = (event) => {
    // Map to honest states. Attempt stop() so onend cleanup always runs.
    const code = event && event.error ? event.error : 'unknown';
    const messages = {
      'not-allowed': 'Microphone permission denied — voice input is off. Your browser blocks recording; text chat is fully functional.',
      'service-not-allowed': 'The recognition service refused access — voice input is unavailable. Text chat is fully functional.',
      network: 'The recognition service is unreachable (it may require an external service). Text chat is fully functional.',
      'audio-capture': 'No microphone was found. Text chat is fully functional.',
      'no-speech': 'No speech was detected. Click the microphone to try again.',
      aborted: '', // user-initiated stop — not an error
    };
    const message = Object.prototype.hasOwnProperty.call(messages, code)
      ? messages[code] : `Voice recognition error (${code}). Text chat is fully functional.`;
    if (message) voiceSetStatus(message, code === 'no-speech' ? undefined : 'error');
    try { rec.stop(); } catch (err) { /* onend handles cleanup */ }
  };
  rec.onend = () => {
    if (voiceState.rec === rec) voiceState.rec = null;
    // keepStatus: an error message set just before end must survive
    // cleanup (the UI keeps showing the honest failure state).
    if (voiceState.listening) setListeningUi(false, null, true);
  };
  try {
    rec.start();
    setListeningUi(true, inputEl);
  } catch (err) {
    voiceState.rec = null;
    setListeningUi(false);
    voiceSetStatus('Voice input could not start in this browser. Text chat is fully functional.', 'error');
  }
}

function pickVoiceLang() {
  // The browser resolves the best actually-installed voice for this
  // language; JARVIS never assumes a specific voice exists.
  return voicePrefs().lang;
}

function speechSynth() {
  const synth = window.speechSynthesis;
  return synth && typeof synth.speak === 'function' ? synth : null;
}

function speakReply(text) {
  if (!voicePrefs().speakReplies) return;
  const synth = speechSynth();
  if (!synth || typeof SpeechSynthesisUtterance !== 'function') {
    voiceSetStatus('Speech output is not available in this browser — replies stay as text.', 'error');
    return;
  }
  if (!text || typeof text !== 'string') return;
  try {
    // One utterance at a time: cancel stale/pending speech first, and
    // only ever the completed assistant reply (never partials/status).
    synth.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = pickVoiceLang();
    utterance.onstart = () => {
      voiceSetStatus('Speaking — click the sound button to stop.', 'active');
    };
    utterance.onend = () => { voiceSetStatus(''); };
    utterance.onerror = () => {
      voiceSetStatus('Speech output failed in this browser — replies stay as text.', 'error');
    };
    synth.speak(utterance);
  } catch (err) {
    voiceSetStatus('Speech output failed in this browser — replies stay as text.', 'error');
  }
}

function toggleSpeakReplies() {
  const on = !voicePrefs().speakReplies;
  savePrefs({ speakReplies: on });
  const synth = speechSynth();
  if (!on && synth) {
    synth.cancel(); // Stop Speaking: nothing pending survives
  }
  syncSpeakButtons();
  if (!synth && on) {
    voiceSetStatus('Speech output is not available in this browser — replies stay as text.', 'error');
    return;
  }
  voiceSetStatus(on
    ? 'Replies will be spoken aloud. Click the sound button again to stop speaking.'
    : 'Speech output is off.');
}

function initVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  voiceState.supported = Boolean(SR);
  if (!SR) {
    // Honest unsupported state: buttons stay visible but inert, with a
    // truthful label; text chat remains fully functional.
    ['voice-mic', 'voice-mic-modal'].forEach((id) => {
      const btn = byId(id);
      if (!btn) return;
      btn.disabled = true;
      btn.setAttribute('aria-label', 'Voice input is not supported in this browser');
      btn.title = 'Voice input is not supported in this browser';
    });
  }
  const langOf = (id) => {
    const sel = byId(id);
    return sel ? sel.value : undefined;
  };
  [['voice-mic', 'chat-input', 'voice-lang'],
   ['voice-mic-modal', 'chat-input-modal', 'voice-lang-modal']].forEach(
    ([micId, inputId, langId]) => {
      const mic = byId(micId);
      const input = byId(inputId);
      if (!mic || !input) return;
      mic.addEventListener('click', () => {
        if (!voiceState.supported) {
          voiceSetStatus('Voice input is not supported in this browser. Type your message instead — text chat is fully functional.', 'error');
          return;
        }
        startVoiceRecognition(input, langOf(langId));
      });
    });
  ['voice-lang', 'voice-lang-modal'].forEach((id) => {
    const sel = byId(id);
    if (!sel) return;
    const prefs = voicePrefs();
    if (sel.querySelector(`option[value="${prefs.lang}"]`)) sel.value = prefs.lang;
    sel.addEventListener('change', () => {
      savePrefs({ voiceLang: sel.value });
      // keep both selects consistent; a live session keeps its language
      const other = byId(id === 'voice-lang' ? 'voice-lang-modal' : 'voice-lang');
      if (other) other.value = sel.value;
      voiceSetStatus(`Recognition language set to ${sel.value}. Applies to the next listening session.`);
    });
  });
  ['voice-speak', 'voice-speak-modal'].forEach((id) => {
    const btn = byId(id);
    if (btn) btn.addEventListener('click', toggleSpeakReplies);
  });
  syncSpeakButtons();
  // Recognition must never outlive its UI state: closing the
  // conversation panel or leaving the page stops it immediately.
  const chatModal = byId('modal-chat');
  if (chatModal) {
    chatModal.addEventListener('close', () => stopVoiceRecognition(true));
  }
  window.addEventListener('pagehide', () => {
    stopVoiceRecognition(true);
    const synth = speechSynth();
    if (synth) synth.cancel();
  });
}

function init() {
  loadPrefs(); // restores density/focus/day before first renders
  renderTimeline();
  renderGauges();
  renderNotifications();
  updateNotifBadge();
  renderHomeGoals();
  renderProjectsFacts();
  renderToolsList();
  renderDiagnosticsStatus();
  initNav();
  initRailDrawer();
  initFocus();
  initDensity();
  initPops();
  initSearch();
  initModals();
  initQuick();
  initChat();
  initVoice();
  SEARCH_INDEX = buildSearchIndex();

  byId('brief-close').addEventListener('click', () => closeModal('modal-brief'));

  refreshSystemStatus();
  refreshConvBadge();
  refreshTasks();
  refreshMetrics();
  loadGoals();
  startDashboardRefresh();

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && byId('app').classList.contains('has-rail-open')) closeRail();
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
