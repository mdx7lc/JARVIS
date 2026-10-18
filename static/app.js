/* ============================================================
   JARVIS Command Center — frontend prototype logic
   In-memory demo state only. No network, no backend, no storage.
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

/* deterministic PRNG so visualizations render identically each load */
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* ============================================================
   DEMO STATE (in-memory only)
   ============================================================ */
const state = {
  day: 'today',
  feedExpanded: false,
  tasks: {
    today: [
      { time: '09:30', title: 'Daily standup', status: 'done' },
      { time: '12:00', title: 'Review dashboard design', status: 'done' },
      { time: '02:00 PM', title: 'Test command interface', status: 'queued' },
      { time: '04:30 PM', title: 'Plan model integration', status: 'planned' },
    ],
    tomorrow: [],
  },
  chat: [],
};

const FEED = [
  { sev: 'info', icon: 'i-info', title: 'Command center prototype initialized', desc: 'Frontend shell loaded from static files. No backend is attached to this build.', time: 'just now' },
  { sev: 'ok', icon: 'i-globe', title: 'Network status sampled from the browser', desc: 'Connection indicator reflects navigator online state — not a server probe.', time: '1m' },
  { sev: 'warn', icon: 'i-zap', title: 'LLM providers not connected', desc: 'OmniRoute, FreeLLMAPI and local runtimes are planned tiles only.', time: '2m' },
  { sev: 'info', icon: 'i-memory', title: 'Memory subsystem is a placeholder', desc: 'No vector store or vault is attached. Counts shown are fictional.', time: '4m' },
  { sev: 'ok', icon: 'i-agents', title: 'Demo task awaiting approval', desc: '“Test command interface” sits in the timeline as a queued demo entry.', time: '6m' },
  { sev: 'info', icon: 'i-chart', title: 'System monitor values are simulated', desc: 'CPU, RAM and disk gauges display fixed demo data, not live telemetry.', time: '9m' },
  { sev: 'demo', icon: 'i-mic', title: 'Voice interface is decorative', desc: 'Microphone is never requested; the waveform is a CSS animation.', time: '12m' },
  { sev: 'demo', icon: 'i-agents', title: 'Agents are display-only', desc: 'Six planned roles are shown. None can execute tasks in this prototype.', time: '15m' },
  { sev: 'warn', icon: 'i-flow', title: 'Workflow execution not implemented', desc: 'Run Workflow opens an explanatory panel instead of dispatching work.', time: '18m' },
  { sev: 'info', icon: 'i-calendar', title: 'Mission timeline uses fictional events', desc: 'Entries are hand-written demo data. No calendar service is involved.', time: '21m' },
  { sev: 'demo', icon: 'i-chat', title: 'Chat replies are deterministic', desc: 'The dock answers with scripted local text — no model inference occurs.', time: '24m' },
  { sev: 'ok', icon: 'i-focus', title: 'Focus Mode ready', desc: 'Toggle it in the left rail to dim secondary panels and demo chips.', time: '27m' },
];
const FEED_VISIBLE_COLLAPSED = 5;

const OVERVIEW = [
  { icon: 'i-core', name: 'AI Core', value: 'Reasoning core', status: 'Demo · Active', sub: 'frontend representation', tone: 'ok' },
  { icon: 'i-memory', name: 'Memory', value: '3,180 stored', status: 'Placeholder', sub: 'no store attached', tone: 'warn' },
  { icon: 'i-mic', name: 'Voice', value: 'Wake-word input', status: 'Not connected', sub: 'no microphone', tone: 'off' },
  { icon: 'i-agents', name: 'Agents', value: '6 roles listed', status: 'Demo', sub: 'display-only', tone: 'ok' },
  { icon: 'i-zap', name: 'LLMs', value: '8 providers', status: '0 connected', sub: 'integration pending', tone: 'warn' },
  { icon: 'i-shield', name: 'System', value: 'Browser sandbox', status: 'Stable', sub: 'demo harness', tone: 'ok' },
];

const AGENTS = [
  { icon: 'i-code', name: 'Coding Agent', status: 'Not connected', key: 'standby', role: 'Pair-programming layer that would inspect repositories, draft patches and review pull requests through local CLI tools.', caps: ['Repository inspection', 'Patch drafting', 'PR review assistance'] },
  { icon: 'i-book', name: 'Research Agent', status: 'Demo', key: 'demo', role: 'Summarizes user-supplied documents and demo corpora offline. Would gain live retrieval once a provider layer exists.', caps: ['Document summarization', 'Topic outlines', 'Citation formatting'] },
  { icon: 'i-memory', name: 'Memory Agent', status: 'Planned', key: 'planned', role: 'Would curate a local memory graph — deduplicating facts, refreshing stale notes and linking related sessions.', caps: ['Fact extraction', 'Graph linking', 'Recall ranking'] },
  { icon: 'i-globe', name: 'Browser Agent', status: 'Not connected', key: 'standby', role: 'Would drive an embedded browser for lookups and form flows. Requires an execution backend that this prototype deliberately omits.', caps: ['Page reading', 'Form filling', 'Screenshot capture'] },
  { icon: 'i-tasks', name: 'Task Agent', status: 'Demo', key: 'demo', role: 'Represents task orchestration. In this build it only demonstrates how queued, active and done states would appear.', caps: ['Task queueing', 'Progress states', 'Demo scheduling'] },
  { icon: 'i-shield', name: 'System Agent', status: 'Planned', key: 'planned', role: 'Would watch local resource telemetry and raise maintenance proposals before thresholds are hit.', caps: ['Resource watch', 'Threshold alerts', 'Maintenance proposals'] },
];

const PROVIDERS = [
  { name: 'OmniRoute', kind: 'Router', state: 'not-connected' },
  { name: 'FreeLLMAPI', kind: 'Model pool', state: 'notverified' },
  { name: 'Claude Code', kind: 'CLI agent', state: 'not-connected' },
  { name: 'OpenCode', kind: 'CLI agent', state: 'planned' },
  { name: 'Codex', kind: 'CLI agent', state: 'not-connected' },
  { name: 'Gemini', kind: 'Cloud model', state: 'planned' },
  { name: 'Groq', kind: 'Cloud model', state: 'notverified' },
  { name: 'Ollama', kind: 'Local runtime', state: 'not-connected' },
];

const NOTIFICATIONS = [
  { icon: 'i-warn', title: 'Visual review pending', desc: 'Two demo timeline entries wait for a manual pass.' },
  { icon: 'i-info', title: 'Prototype scope reminder', desc: 'Every tile in this build is simulated data.' },
  { icon: 'i-mic', title: 'Voice status unchanged', desc: 'Voice input stays unavailable until an audio stack exists.' },
  { icon: 'i-zap', title: 'Provider integration not connected', desc: 'Provider credentials are out of scope for the frontend task.' },
];

const SECTIONS = {
  'ai-core': { name: 'AI Core', desc: 'Runtime telemetry for the reasoning core will live here. In this prototype the Command Center panels already demonstrate the planned status rows.' },
  'agents': { name: 'Agents', desc: 'The agent roster shown on the Command Center is display-only. No agent runtime, scheduler or execution engine exists in this frontend task.' },
  'tasks': { name: 'Tasks', desc: 'Task creation currently stores demo entries in page memory — they reset on reload and are never sent anywhere.' },
  'calendar': { name: 'Calendar', desc: 'The calendar is intentionally not connected to any real calendar service. The Mission Timeline shows fictional demo events.' },
  'memory': { name: 'Memory', desc: 'Memory insights are placeholders. No vector database, vault or Supabase instance is attached to this build.' },
  'conversations': { name: 'Conversations', desc: 'Chat history lives in page memory only. The dock conversation panel demonstrates the planned transcript view.' },
  'knowledge': { name: 'Knowledge Base', desc: 'A curated knowledge store is planned. This prototype ships no documents, indexes or retrieval code.' },
  'tools': { name: 'Tools & Skills', desc: 'A skill registry is planned. No tool can be invoked from this interface yet.' },
  'workflows': { name: 'Workflows', desc: 'Workflow execution is not implemented — running one requires an orchestration backend outside the frontend-only scope.' },
};

/* ============================================================
   CLOCK
   ============================================================ */
function tickClock() {
  const now = new Date();
  const d = byId('clock-date');
  const t = byId('clock-time');
  if (d) {
    d.textContent = now.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
    d.setAttribute('datetime', now.toISOString());
  }
  if (t) {
    t.textContent = now.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true });
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
   RENDERERS
   ============================================================ */
function renderOverview() {
  const list = byId('overview-list');
  if (!list) return;
  list.innerHTML = '';
  OVERVIEW.forEach((row) => {
    const li = document.createElement('li');
    li.className = `overview-row ov-${row.tone}`;
    const icon = document.createElement('span');
    icon.className = 'overview-ic';
    icon.appendChild(svgIcon(row.icon));
    const mid = document.createElement('div');
    const label = document.createElement('span');
    label.className = 'ov-label';
    label.textContent = row.name;
    const value = document.createElement('span');
    value.className = 'ov-value';
    value.textContent = `${row.value} · ${row.sub}`;
    mid.append(label, value);
    const status = document.createElement('span');
    status.className = 'ov-status';
    status.textContent = row.status;
    li.append(icon, mid, status);
    list.appendChild(li);
  });
}

function renderFeed() {
  const list = byId('feed-list');
  if (!list) return;
  list.innerHTML = '';
  const tpl = byId('tpl-feed-item');
  const items = state.feedExpanded ? FEED : FEED.slice(0, FEED_VISIBLE_COLLAPSED);
  items.forEach((ev) => {
    const node = tpl.content.cloneNode(true);
    const li = node.querySelector('li');
    li.dataset.sev = ev.sev;
    li.querySelector('.use-target').setAttribute(SVG_HREF, `#${ev.icon}`);
    li.querySelector('.feed-title').textContent = ev.title;
    li.querySelector('.feed-desc').textContent = ev.desc;
    li.querySelector('.feed-time').textContent = ev.time;
    li.querySelector('.feed-tag').textContent = ev.sev === 'ok' ? 'Live' : ev.sev;
    list.appendChild(node);
  });
  const more = byId('feed-more');
  if (more) more.firstChild.textContent = state.feedExpanded ? 'Show less ' : 'View All Intelligence ';
  rebuildSearchIndex();
}

function renderAgents() {
  const grid = byId('agent-grid');
  if (!grid) return;
  grid.innerHTML = '';
  AGENTS.forEach((agent, i) => {
    const btn = document.createElement('button');
    btn.className = 'agent-tile';
    btn.dataset.status = agent.key;
    btn.dataset.index = String(i);
    btn.setAttribute('aria-haspopup', 'dialog');
    btn.setAttribute('aria-label', `${agent.name} — ${agent.status}. Open details`);
    const ic = document.createElement('span');
    ic.className = 'agent-ic';
    ic.appendChild(svgIcon(agent.icon));
    const meta = document.createElement('div');
    meta.className = 'agent-meta';
    const name = document.createElement('div');
    name.className = 'agent-name';
    name.textContent = agent.name;
    const st = document.createElement('div');
    st.className = 'agent-state';
    st.textContent = agent.status;
    meta.append(name, st);
    btn.append(ic, meta);
    grid.appendChild(btn);
  });
}

function renderProviders() {
  const grid = byId('llm-grid');
  if (!grid) return;
  grid.innerHTML = '';
  PROVIDERS.forEach((p) => {
    const tile = document.createElement('div');
    tile.className = 'llm-tile';
    tile.dataset.state = p.state;
    tile.dataset.name = p.name.toLowerCase();
    const name = document.createElement('div');
    name.className = 'llm-name';
    name.textContent = p.name;
    const kind = document.createElement('div');
    kind.className = 'llm-provider';
    kind.textContent = p.kind;
    const st = document.createElement('div');
    st.className = 'llm-state';
    st.textContent = p.state === 'notverified' ? 'Not verified' : p.state.replace('-', ' ');
    tile.append(name, kind, st);
    grid.appendChild(tile);
  });
}

const STATUS_LABEL = { done: 'Done', queued: 'Queued', planned: 'Planned', warn: 'High prio' };

function renderTimeline() {
  const box = byId('timeline');
  if (!box) return;
  box.innerHTML = '';
  const items = state.tasks[state.day];
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'feed-empty';
    empty.textContent = 'No demo events for tomorrow. The timeline is a fictional frontend placeholder — no calendar is connected.';
    box.appendChild(empty);
  } else {
    items.forEach((t, i) => {
      const li = document.createElement('li');
      li.className = 'tl-item';
      li.dataset.status = t.status === 'queued' && t.priority === 'high' ? 'warn' : t.status;
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
      status.textContent = STATUS_LABEL[t.status] || t.status;
      status.title = 'Click to cycle status (demo)';
      status.setAttribute('aria-label', `Cycle status of ${t.title}, currently ${status.textContent}`);
      status.addEventListener('click', () => {
        const order = ['queued', 'warn', 'done', 'planned'];
        t.status = order[(order.indexOf(t.status) + 1) % order.length];
        renderTimeline();
        updateTaskBadge();
      });
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

function updateTaskBadge() {
  const badge = byId('nav-task-badge');
  if (badge) badge.textContent = String(state.tasks.today.length);
}

/* ============================================================
   GAUGES
   ============================================================ */
const GAUGES = [
  { label: 'CPU', value: 15 },
  { label: 'RAM', value: 54 },
  { label: 'Disk', value: 40 },
];

function renderGauges() {
  const box = byId('gauges');
  if (!box) return;
  box.innerHTML = '';
  GAUGES.forEach((g) => {
    const C = 2 * Math.PI * 42;
    const cell = document.createElement('div');
    cell.className = 'gauge';
    cell.innerHTML = `
      <svg class="gauge-svg" viewBox="0 0 100 100" role="img" aria-label="${g.label} usage ${g.value} percent, demo data">
        <circle cx="50" cy="50" r="42" class="gauge-arc-bg"/>
        <circle cx="50" cy="50" r="42" class="gauge-arc-progress" style="--c:${C.toFixed(1)}"/>
        <text x="50" y="55" text-anchor="middle" class="gauge-val">${g.value}<tspan font-size="8" fill="#8AA7B9">%</tspan></text>
      </svg>
      <span class="gauge-label">${g.label}</span>
      <span class="gauge-note">Demo data</span>`;
    box.appendChild(cell);
    const arc = cell.querySelector('.gauge-arc-progress');
    arc.style.setProperty('--p', '0');
    requestAnimationFrame(() => requestAnimationFrame(() => {
      arc.style.setProperty('--p', String(g.value / 100));
    }));
  });
}

/* ============================================================
   VISUALIZATIONS — orbital network & memory map
   ============================================================ */
function buildOrbitalNet() {
  const nodesG = byId('o-nodes');
  const linksG = byId('o-links');
  if (!nodesG || !linksG) return;
  const rnd = mulberry32(20260615);
  const CX = 450, CY = 320;
  const nodes = [];
  const svgNS = 'http://www.w3.org/2000/svg';
  for (let i = 0; i < 30; i++) {
    const ang = rnd() * Math.PI * 2;
    const r = 120 + rnd() * 130;
    nodes.push({
      x: CX + Math.cos(ang) * r * 0.98,
      y: CY + Math.sin(ang) * r * 0.62,
      r: 1 + rnd() * 2.4,
      bright: rnd() > 0.82,
    });
  }
  nodes.forEach((n) => {
    if (n.bright) return;
    const c = document.createElementNS(svgNS, 'circle');
    c.setAttribute('cx', n.x.toFixed(1));
    c.setAttribute('cy', n.y.toFixed(1));
    c.setAttribute('r', n.r.toFixed(1));
    c.setAttribute('fill', n.bright ? '#7FE7F8' : 'rgba(32,221,245,.75)');
    nodesG.appendChild(c);
  });
  nodes.forEach((a, i) => {
    nodes.slice(i + 1).forEach((b) => {
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      if (d < 92) {
        const l = document.createElementNS(svgNS, 'line');
        l.setAttribute('x1', a.x.toFixed(1)); l.setAttribute('y1', a.y.toFixed(1));
        l.setAttribute('x2', b.x.toFixed(1)); l.setAttribute('y2', b.y.toFixed(1));
        l.setAttribute('stroke', 'rgba(32,221,245,.16)');
        l.setAttribute('stroke-width', '0.7');
        linksG.appendChild(l);
      }
    });
  });
}

/* luminous dots that travel exactly along each inclined orbital ring */
const ORBITS = [
  { rx: 300, ry: 96, tilt: -16, dur: 38, dots: [{ r: 4.5, begin: 0 }, { r: 3, begin: 11, small: true }] },
  { rx: 252, ry: 168, tilt: 24, dur: 52, dots: [{ r: 3.8, begin: 5 }, { r: 2.6, begin: 23, small: true }] },
  { rx: 345, ry: 72, tilt: 9, dur: 64, dots: [{ r: 3.4, begin: 8 }] },
];

function buildOrbiters() {
  const g = byId('o-orbiters');
  if (!g) return;
  const svgNS = 'http://www.w3.org/2000/svg';
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  ORBITS.forEach((o) => {
    const tiltG = document.createElementNS(svgNS, 'g');
    tiltG.setAttribute('transform', `rotate(${o.tilt})`);
    o.dots.forEach((d) => {
      const c = document.createElementNS(svgNS, 'circle');
      c.setAttribute('r', d.r);
      c.setAttribute('class', d.small ? 'orbit-node orbit-node--sm' : 'orbit-node');
      if (reduceMotion) {
        const th = 2 * Math.PI * (d.begin / o.dur);
        c.setAttribute('cx', (o.rx * Math.cos(th)).toFixed(1));
        c.setAttribute('cy', (o.ry * Math.sin(th)).toFixed(1));
      } else {
        const am = document.createElementNS(svgNS, 'animateMotion');
        am.setAttribute('dur', `${o.dur}s`);
        am.setAttribute('repeatCount', 'indefinite');
        am.setAttribute('begin', `${-d.begin}s`);
        am.setAttribute('path', `M ${-o.rx} 0 A ${o.rx} ${o.ry} 0 1 0 ${o.rx} 0 A ${o.rx} ${o.ry} 0 1 0 ${-o.rx} 0`);
        c.appendChild(am);
      }
      tiltG.appendChild(c);
    });
    g.appendChild(tiltG);
  });
}

function buildMemoryNet() {
  const svg = byId('memory-net');
  if (!svg) return;
  const rnd = mulberry32(777);
  const svgNS = 'http://www.w3.org/2000/svg';
  const nodes = [];
  for (let i = 0; i < 16; i++) {
    nodes.push({ x: 12 + rnd() * 196, y: 10 + rnd() * 100, r: 1.5 + rnd() * 2.2 });
  }
  nodes.forEach((a, i) => {
    nodes.slice(i + 1).forEach((b) => {
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      if (d < 62) {
        const l = document.createElementNS(svgNS, 'line');
        l.setAttribute('x1', a.x.toFixed(1)); l.setAttribute('y1', a.y.toFixed(1));
        l.setAttribute('x2', b.x.toFixed(1)); l.setAttribute('y2', b.y.toFixed(1));
        l.setAttribute('class', 'mn-link');
        svg.appendChild(l);
      }
    });
  });
  nodes.forEach((n) => {
    const c = document.createElementNS(svgNS, 'circle');
    c.setAttribute('cx', n.x.toFixed(1));
    c.setAttribute('cy', n.y.toFixed(1));
    c.setAttribute('r', n.r.toFixed(1));
    c.setAttribute('class', 'mn-node');
    svg.appendChild(c);
  });
}

function initWaveforms() {
  const rnd = mulberry32(42);
  $$('.voice-wave span').forEach((s, i) => {
    s.style.setProperty('--h', `${25 + Math.round(rnd() * 70)}%`);
    s.style.setProperty('--i', String(i));
  });
  $$('.dock-dots span').forEach((s, i) => s.style.setProperty('--i', String(i)));
}

/* ============================================================
   NAVIGATION
   ============================================================ */
function showCommand() {
  byId('view-command').hidden = false;
  byId('view-command').classList.add('is-visible');
  byId('view-secondary').hidden = true;
  $$('.nav-item').forEach((b) => {
    const active = b.dataset.nav === 'command';
    b.classList.toggle('is-active', active);
    if (active) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  closeRail();
}

function showSection(key) {
  const sec = SECTIONS[key];
  if (!sec) return showCommand();
  byId('view-command').hidden = true;
  byId('view-command').classList.remove('is-visible');
  const view = byId('view-secondary');
  view.hidden = false;
  byId('sec-title').textContent = sec.name;
  byId('sec-name').textContent = sec.name;
  byId('sec-desc').textContent = `${sec.desc}`;
  byId('sec-back').focus();
}

function initNav() {
  $$('.nav-item').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (btn.dataset.nav === 'command') showCommand();
      else showSection(btn.dataset.nav);
    });
  });
  byId('sec-back').addEventListener('click', showCommand);
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
function initFocus() {
  const btn = byId('focus-toggle');
  btn.addEventListener('click', () => {
    const on = byId('app').classList.toggle('is-focus');
    btn.setAttribute('aria-pressed', String(on));
    toast(on ? 'Focus Mode on — secondary demo content dimmed' : 'Focus Mode off');
  });
}

function initDensity() {
  const app = byId('app');
  $$('.seg-btn[data-density]').forEach((b) => {
    b.addEventListener('click', () => {
      app.dataset.density = b.dataset.density;
      $$('.seg-btn[data-density]').forEach((x) => {
        const active = x === b;
        x.classList.toggle('is-active', active);
        x.setAttribute('aria-selected', String(active));
      });
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

function renderNotifications() {
  const list = byId('notif-list');
  if (!list) return;
  list.innerHTML = '';
  NOTIFICATIONS.forEach((n) => {
    const li = document.createElement('li');
    li.className = 'notif-item';
    li.appendChild(svgIcon(n.icon));
    const body = document.createElement('div');
    const t = document.createElement('p');
    t.className = 'notif-title';
    t.textContent = n.title;
    const d = document.createElement('p');
    d.className = 'notif-desc';
    d.textContent = n.desc;
    body.append(t, d);
    li.appendChild(body);
    list.appendChild(li);
  });
}

function initPops() {
  byId('notif-btn').addEventListener('click', (e) => { e.stopPropagation(); togglePop('pop-notif', true); });
  byId('settings-btn').addEventListener('click', (e) => { e.stopPropagation(); togglePop('pop-settings', true); });
  byId('operator-btn').addEventListener('click', (e) => { e.stopPropagation(); togglePop('pop-operator', true); });
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
   SEARCH
   ============================================================ */
let searchHits = [];
let searchCursor = 0;

function buildSearchIndex() {
  return $$('#view-command .panel').flatMap((panel) =>
    $$('.feed-title, .feed-desc, .agent-name, .agent-state, .llm-name, .tl-title, .quick-btn span, .ov-label, .panel-title', panel)
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

function initSearch() {
  const input = byId('search');
  input.addEventListener('input', () => {
    const n = runSearch(input.value);
    if (n) jumpToHit(0);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && searchHits.length) {
      e.preventDefault();
      searchCursor = (searchCursor + 1) % searchHits.length;
      jumpToHit(searchCursor);
      toast(`Match ${searchCursor + 1} of ${searchHits.length}`);
    }
    if (e.key === 'Escape') {
      input.value = '';
      clearSearchHits();
      input.blur();
    }
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
  showCommand();
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

const INFO_CALENDAR = `
  <p><strong>Calendar is not connected.</strong> The Mission Timeline shows fictional demo events only.</p>
  <ul>
    <li>No calendar service is queried.</li>
    <li>Schedule data is hand-written demo content.</li>
    <li>A real scheduler belongs to a later system milestone.</li>
  </ul>`;

const INFO_VOICE = `
  <p><strong>Voice chat is a demo placeholder.</strong> This prototype never requests microphone permission and never records audio.</p>
  <ul>
    <li>The waveform is a decorative CSS animation.</li>
    <li>Voice replies would require a speech stack outside the frontend scope.</li>
    <li>Use the text input to see the deterministic demo replies.</li>
  </ul>`;

const INFO_WORKFLOW = `
  <p><strong>Workflow execution is not implemented.</strong> Dispatching multi-step agent workflows requires an orchestration backend, which is out of scope for this frontend-only build.</p>
  <ul>
    <li>No workflow engine exists here.</li>
    <li>The button is kept visible to show the planned placement.</li>
  </ul>`;

const INFO_MEMORY = `
  <p><strong>Memory map is a placeholder.</strong> Counts (3,180 memories · 22 session turns · 14 tool calls) are fictional demo values.</p>
  <ul>
    <li>No vector database or vault is attached.</li>
    <li>The network drawing is decorative, generated with a fixed random seed.</li>
  </ul>`;

const INFO_PROVIDERS = `
  <p><strong>Provider configuration is outside this frontend task.</strong> Keys, endpoints, routing and quotas belong to the future backend.</p>
  <ul>
    <li>Tiles show honest planned states only.</li>
    <li>No credentials, endpoints or quotas are displayed anywhere.</li>
  </ul>`;

function showAgentsOverview() {
  showInfo('Agent Roster — Demo', AGENTS.map((a) =>
    `<li><strong>${a.name}</strong> — ${a.status}. ${a.role}</li>`
  ).join('') + `<p style="margin-top:10px">Display-only roster. No agent can execute work in this prototype.</p>`);
}

/* ============================================================
   AGENT DETAIL
   ============================================================ */
function showAgentDetail(agent) {
  byId('agent-title').textContent = agent.name;
  const body = byId('agent-body');
  body.innerHTML = '';
  const chip = document.createElement('span');
  chip.className = `chip ${agent.key === 'planned' ? 'chip-plan' : agent.key === 'demo' ? 'chip-ok' : 'chip-off'}`;
  chip.textContent = agent.status;
  body.appendChild(chip);
  const role = document.createElement('p');
  role.style.marginTop = '10px';
  role.innerHTML = '<strong>Planned role.</strong> ';
  role.appendChild(document.createTextNode(agent.role));
  body.appendChild(role);
  const ul = document.createElement('ul');
  agent.caps.forEach((cap) => {
    const li = document.createElement('li');
    li.textContent = cap;
    ul.appendChild(li);
  });
  body.appendChild(ul);
  const note = document.createElement('p');
  note.className = 'modal-note';
  note.style.margin = '12px 0 0';
  note.textContent = 'This agent cannot execute tasks. The frontend prototype is display-only.';
  body.appendChild(note);
  openModal('modal-agent');
}

/* ============================================================
   QUICK COMMANDS & TASKS
   ============================================================ */
function initQuick() {
  byId('qc-task').addEventListener('click', () => {
    openModal('modal-task');
    setTimeout(() => byId('task-title-input').focus(), 50);
  });
  byId('qc-calendar').addEventListener('click', () => showInfo('Calendar', INFO_CALENDAR));
  byId('qc-voice').addEventListener('click', () => showInfo('Start Voice Chat', INFO_VOICE));
  byId('qc-workflow').addEventListener('click', () => showInfo('Run Workflow', INFO_WORKFLOW));
  byId('timeline-more').addEventListener('click', () => showInfo('Full Schedule', INFO_CALENDAR));
  byId('memory-map').addEventListener('click', () => showInfo('Memory Map', INFO_MEMORY));
  byId('llm-manage').addEventListener('click', () => showInfo('Manage Providers', INFO_PROVIDERS));
  byId('agents-viewall').addEventListener('click', showAgentsOverview);

  const voiceCard = byId('voice-status');
  voiceCard.setAttribute('role', 'button');
  voiceCard.setAttribute('tabindex', '0');
  voiceCard.setAttribute('aria-haspopup', 'dialog');
  const openVoice = () => showInfo('Voice Status', INFO_VOICE);
  voiceCard.addEventListener('click', openVoice);
  voiceCard.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openVoice(); }
  });

  $$('.seg-btn[data-day]').forEach((b) => b.addEventListener('click', () => {
    state.day = b.dataset.day;
    renderTimeline();
  }));

  byId('feed-more').addEventListener('click', () => {
    state.feedExpanded = !state.feedExpanded;
    renderFeed();
  });

  byId('agent-grid').addEventListener('click', (e) => {
    const tile = e.target.closest('.agent-tile');
    if (tile) showAgentDetail(AGENTS[Number(tile.dataset.index)]);
  });

  byId('task-close').addEventListener('click', () => closeModal('modal-task'));
  byId('task-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const title = byId('task-title-input').value.trim();
    if (!title) return;
    const priority = byId('task-priority').value;
    state.tasks.today.push({
      time: byId('task-time').value,
      title,
      status: 'queued',
      priority: priority === 'high' ? 'high' : undefined,
    });
    byId('task-title-input').value = '';
    closeModal('modal-task');
    state.day = 'today';
    renderTimeline();
    updateTaskBadge();
    toast('Demo task added to timeline (page memory only)');
  });
}

/* ============================================================
   CHAT — deterministic demo replies
   ============================================================ */
function mockReply(text) {
  const t = text.toLowerCase();
  const has = (words) => words.split('|').some((w) => t.includes(w));
  if (has('hello|hi|hey|greet')) return 'Good to see you, Commander. This console is a frontend prototype — every reply you read here is scripted locally in your browser.';
  if (has('time|date|clock')) return `The browser clock reads ${new Date().toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}. Timeline entries are fictional demo items, not real calendar events.`;
  if (has('task|todo|timeline|schedule')) return `The timeline currently holds ${state.tasks.today.length} demo entries. You can add more via “Start New Task” — they persist only in page memory.`;
  if (has('status|system|cpu|ram|disk|health|monitor')) return 'System monitor shows CPU 15% · RAM 54% · Disk 40% — all fixed demo values. Nothing here measures the actual machine.';
  if (has('provider|llm|model|omniroute|freellm|groq|ollama|gemini|codex|claude|opencode')) return 'All 8 provider tiles are honest placeholders — none are connected, and no keys or endpoints exist in this build.';
  if (has('memory|vault|vector|obsidian|supabase')) return 'Memory insights (3,180 · 22 · 14) are fictional placeholders. No vector store or vault is attached.';
  if (has('voice|listen|speak|talk|microphone')) return 'Voice stays offline in this prototype: no microphone permission is requested, and the waveform is decorative.';
  if (has('agent')) return 'Six agent roles are on display — Coding, Research, Memory, Browser, Task and System. None can execute tasks in this prototype.';
  if (has('workflow|automat')) return 'Workflow execution needs an orchestration backend. The panel you can open explains this honestly.';
  if (has('focus')) return 'Focus Mode lives in the left rail. It dims secondary demo content — presentation only, no system changes.';
  if (has('brief|report|summar')) return 'Press “Executive Briefing” on the dock for a summary compiled from the visible demo data.';
  if (has('help|what can|command')) return 'Try asking about: tasks, system status, providers, memory, agents, voice, or the briefing. Everything I answer is scripted demo text.';
  const seeds = [
    'Understood. As a demo shell I can walk you through the panels on screen — ask about tasks, status, providers or memory.',
    'Noted, Commander. This console replies with scripted demo text only; no model is attached to this build.',
    'Logged in page memory. For real outcomes, this prototype would need the execution backend that is deliberately out of scope.',
  ];
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) >>> 0;
  return seeds[h % seeds.length];
}

function appendChat(text, who) {
  const log = byId('chat-log');
  const msg = document.createElement('div');
  msg.className = `chat-msg chat-msg--${who}`;
  msg.textContent = text;
  if (who === 'jarvis') {
    const tag = document.createElement('span');
    tag.className = 'chat-msg--tag';
    tag.textContent = 'UI-only demo reply — no model attached';
    msg.appendChild(tag);
  }
  log.appendChild(msg);
  log.scrollTop = log.scrollHeight;
}

function handleChatSubmit(text, inputEl) {
  const clean = text.trim();
  if (!clean) return;
  inputEl.value = '';
  appendChat(clean, 'user');
  const typing = document.createElement('div');
  typing.className = 'chat-msg chat-msg--jarvis chat-typing';
  typing.innerHTML = '<span></span><span></span><span></span>';
  byId('chat-log').appendChild(typing);
  byId('chat-log').scrollTop = byId('chat-log').scrollHeight;
  setTimeout(() => {
    typing.remove();
    appendChat(mockReply(clean), 'jarvis');
  }, 650);
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
}

/* ============================================================
   EXECUTIVE BRIEFING — compiled from visible demo data only
   ============================================================ */
function renderBriefing() {
  const doneCount = state.tasks.today.filter((t) => t.status === 'done').length;
  const warnCount = FEED.filter((f) => f.sev === 'warn').length;
  const connected = PROVIDERS.filter((p) => p.state === 'demo').length;
  byId('brief-body').innerHTML = `
    <p><strong>Command Center status</strong> — compiled at ${new Date().toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })} from visible demo data.</p>
    <ul>
      <li><strong>Timeline.</strong> ${state.tasks.today.length} fictional demo entries today, ${doneCount} marked done.</li>
      <li><strong>Intelligence feed.</strong> ${FEED.length} simulated events on record, ${warnCount} flagged as warnings.</li>
      <li><strong>Agents.</strong> 6 roles on display — demo, planned or not connected. None can execute work.</li>
      <li><strong>Providers.</strong> ${connected} of ${PROVIDERS.length} represented as connected; the rest are honest placeholders.</li>
      <li><strong>System monitor.</strong> CPU 15% · RAM 54% · Disk 40% — fixed demo values, not live telemetry.</li>
      <li><strong>Memory.</strong> 3,180 memories · 22 session turns · 14 tool calls — fictional placeholder counts.</li>
      <li><strong>Voice.</strong> Not connected. No microphone access exists in this build.</li>
    </ul>
    <p style="margin-top:12px"><strong>Reminder.</strong> This briefing summarizes the prototype itself. No backend, model or integration participated.</p>`;
}

/* ============================================================
   INIT
   ============================================================ */
let SEARCH_INDEX = [];

function init() {
  renderOverview();
  renderFeed();
  renderAgents();
  renderProviders();
  renderTimeline();
  renderGauges();
  renderNotifications();
  buildOrbitalNet();
  buildMemoryNet();
  initWaveforms();
  initNav();
  initRailDrawer();
  initFocus();
  initDensity();
  initPops();
  initSearch();
  initModals();
  initQuick();
  initChat();
  SEARCH_INDEX = buildSearchIndex();

  byId('brief-btn').addEventListener('click', () => {
    renderBriefing();
    openModal('modal-brief');
  });
  byId('brief-close').addEventListener('click', () => closeModal('modal-brief'));

  buildOrbiters();
  tickClock();
  setInterval(tickClock, 1000);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && byId('app').classList.contains('has-rail-open')) closeRail();
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
