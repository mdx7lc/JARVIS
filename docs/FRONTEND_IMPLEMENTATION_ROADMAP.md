# JARVIS Frontend Implementation Roadmap

> **SUPERSEDED (Phase 3A, 2026-10-19):** this Phase 2D edition is kept as
> historical context. The authoritative roadmap is
> `docs/IMPLEMENTATION_ROADMAP.md` (R1–R6 are absorbed and renumbered
> there as P0–P5; the selected next phase is 3B "Conversation lifecycle &
> truthfulness"). Do not execute against this file.

Derived from `docs/FRONTEND_FUNCTIONALITY_AUDIT.md` (Phase 2D).
Dependency-aware and prioritized. Every incomplete item in the audit is
mapped to exactly one phase. Ordering follows the mandate: chat
reliability first, then real functionality behind existing high-priority
controls, then correct data/settings/provider visibility, then the rest.

**Ground rules.** Stdlib-only backend; SQLite persistence; loopback-only
server; credentials server-side only; no new frontend frameworks. Backend
schema changes bump `db.SCHEMA_VERSION` with an explicit migration
(current DB refuses any other version — the migration path handles that).

---

## Phase R1 — Chat conversation completeness *(highest priority)*

**Goal:** the real chat becomes the primary, durable interface, and the UI
stops claiming it is simulated.

Features included:
1. **Conversation history loading** — the conversation modal lists and
   reloads previous conversations from the server; `#chat-log` renders a
   restored conversation instead of page-memory only. *(audit 9.7)*
2. **"New conversation" control** in the modal header — resets
   `state.conversationId` to `null` and clears the log after explicit
   confirmation when a pending request is active. *(audit 9.8)*
3. **Copy truthfulness** — `index.html` labels updated: "Demo mode" chip on
   the dock prompt/title, "UI-only · demo" modal chip, "simulated"
   placeholders, and the stale feed entry describing scripted chat.
   *(audit 2.2, 3.3, 7.3, 9.9, 9.10)*
4. Empty-state and error-state affordances for the log (e.g. "conversation
   not found" recoverable without reload).

Involved files: `static/index.html`, `static/app.js` (plus optional read-
only CSS additions in `styles.css` — none required by contract).

Backend work:
- `GET /api/conversations` — id, title, updated_at (bounded list, newest
  first, loopback-only, same-origin enforced like `/api/chat`).
- `GET /api/conversations/{id}/messages` — bounded recent messages,
  chronological; 404 on unknown id.
- DB: **no schema change** — existing `conversations`/`messages` tables
  suffice (`db.get_messages` already provides chronological fetch).

Persistence/security: read-only endpoints; no secrets; conversation IDs
are 32-hex UUIDs (no enumeration risk beyond localhost scope); request
size limits mirror chat.

Dependencies: none (Phase 2B/2C complete).

Acceptance criteria:
- Reload JARVIS, open a previous conversation → full transcript renders
  from SQLite (roles and order correct; size-bounded).
- Starting a new conversation immediately changes the active ID; next
  message creates a fresh server-side conversation (verified via API
  response ID difference).
- Unit tests: list/read endpoints, 404 path, cross-conversation isolation.
- Flow test: frontend sequence (open list → load → continue → new).
- **Stale copy eliminated** (grep-clean of "simulated"/"demo" on the chat
  surface).
- Manual browser test: full loop on both composers.

## Phase R2 — Provider & application status visibility

**Goal:** the dashboard reflects verified reality instead of stale demo
data. *(audit 2.2, 5.3)*

Features included:
1. Provider tiles become data-driven: render from a server status snapshot
   instead of the static `PROVIDERS` array; FreeLLMAPI tile shows the
   real state (configured model policy, reachability — **without** ever
   exposing the API key).
2. Toolbar "System Status" badge shows actual `/api/health` result (DB
   ready / uptime / version) instead of the blanket "Demo Mode" label.
3. No model-switching UI (explicitly out of scope; model remains
   env-configured).

Involved files: `static/app.js`, `server.py`, `config.py` (status only),
`static/index.html` (minimal marker changes).

Backend work:
- `GET /api/status` — `{db, schema_version, uptime_seconds, model_policy,
  provider_configured: bool}` (never the key; provider reachability
  exposed only as booleans/latency of the health check, not bodies).
- Health check must stay non-inferring (`GET /api/health` unchanged; the
  status endpoint may additionally do a non-token **models-list** check if
  desired, but **no chat-completions calls**).

DB: none. Dependencies: none.

Acceptance criteria:
- FreeLLMAPI tile reads "Connected" (or accurate reason, e.g. "Not
  configured") after page load, sourced from `/api/status`; tile for
  OmniRoute keeps its honest not-connected state.
- Status badge reflects a stopped database (e.g. moved `JARVIS_DB_PATH`)
  within one refresh.
- Unit tests: status endpoint shape, no-credential-safe response, no key
  leakage (assert key value absent from all responses).
- Manual browser check on tile state.

## Phase R3 — Real tasks behind the existing task UI

**Goal:** the timeline/task modal manipulates durable tasks.
*(audit 1.5, 4.3, 4.4, 4.6)*

Features included:
1. `state.tasks` hydration from the server on load.
2. Task modal "Add Task" → `POST /api/tasks`; status cycling →
   `PATCH /api/tasks/{id}`; today/tomorrow tabs → filtered `GET`.
3. In-memory fallback disappears; badge reflects server count.

Files: `app.js`, `index.html` (minimal hints only), backend modules.

Backend work:
- Tables `tasks(id, title, day, time_label, status, priority, created_at,
  updated_at)`; **schema version 2** with an explicit, tested migration
  (version gate currently refuses non-1 DBs → migration function writes
  v2 marker; refuses older than v1).
- Endpoints: `GET/POST /api/tasks`, `PATCH /api/tasks/{id}`; same
  validation style as chat (bounded sizes, strict field whitelist — the
  chat 400/404/413/415 taxonomy is the template).

Security: role-free local app; strict enum validation for status/priority;
size limits; no CORS relaxation.

Acceptance criteria:
- Add a task → present after server restart (SQLite row verified).
- Status cycle persists; badge updates from server data.
- Migration test from v1 DB with existing chat data intact.
- Unit tests: CRUD paths, validation failures, persistence across
  connections; flow replay for the UI sequence.

## Phase R4 — Remaining dashboard sections & systems monitor

**Features:** system monitor from real `/proc`-derived telemetry
(read-only endpoint, CPU/RAM/disk), replace gauge demo values; Overview
rows reflect real subsystem states (R2 data); briefing modal compiled
from real data; honest removal/adjustment of memory-count placeholders
(*memory feature itself is genuinely unscoped — see R6*).

Backend: `GET /api/metrics` (psutil-free, stdlib `/proc` + `os.statvfs`),
loopback-only. Security: read-only, no process cmdline leakage (index
only). Acceptance: gauges move between refreshes matching `top`/`df`
within tolerance; tests for metric shape/bounds/no secrets.

## Phase R5 — Search, notifications and secondary sections on real data

- Search indexes real conversations/tasks (post-R1/R3) with versioned
  results; pure frontend change + existing endpoints.
- Notifications: server-generated meaningful events (chat failures,
  task creation) instead of static array; small `GET /api/events` with a
  ring-buffer of recent app events.
- Secondary preview sections that remain genuinely unscoped (Calendar,
  Knowledge, Tools, Workflows, Voice) stay as-is with honest chips until
  a real feature decision exists (see R6).

## Phase R6 — Genuinely missing requirements (explicit non-inventions)

These were audited as absent with **no derivable intended behavior** from
existing UI or code; they require a product decision before any
implementation work:

- **Memory subsystem** (vector store, what "memories"/"session turns"/
  "tool calls" would actually count).
- **Agent execution** (roles are display-only; no runtime exists).
- **Tool/skill registry** (18-badge implies inventory with no definition).
- **Workflow orchestration** (no engine, no definition of what runs).
- **Voice I/O** (no audio stack decision).
- **Calendar integration** (no service choice; timeline suggest aTasks
  model instead).
- **Knowledge base** (no corpus/index decision).
- **Operator accounts/sessions** (auth framework deliberately out of
  scope for localhost; not even a data model exists).

No phase in this roadmap covers these until their behavior is defined.

---

## Summary dependency graph

```
R1 (chat) ──────────┐
                    ├─ R5 (search/notifications over real data)
R2 (status) ────────┼─ R4 (metrics/overview/briefing use R2 flags)
R3 (tasks) ─────────┘
R6: deferred to product decisions
```

Testing conventions carried through all phases: suite must stay live-
inference-free; every new endpoint gets validation + persistence tests.

---

## Phase R7 — Browser E2E verification (COMPLETE — Phase 6B)

R1–R5 shipped with browser verification marked "outstanding" because the
toolchain could not drive a real browser. Phase 6B resolved this:

- Toolchain: dev-only Python venv at `/tmp/opencode/jarvis-e2e/venv`
  (Playwright 1.64 + its bundled Firefox in a user-local cache). The
  repo, the runtime and the stdlib test suite stay dependency-free;
  discovery skips nothing (browser files self-skip when Playwright is
  absent — 42 explicit `s` skips in the 341-test stdlib count).
- `tests/browser_harness.py`: per-test temp dir + temp DB + fake
  provider + real `create_server` on an ephemeral loopback port, then a
  fresh browser context/page. No production data files, no live AI.
- Suites: `test_browser_chat.py` (14), `test_browser_tasks.py` (7),
  `test_browser_search.py` (7), `test_browser_dashboard.py` (9),
  `test_browser_security.py` (5) — 42 cases, all green (≈ 4 min run).
- Workflows exercised end to end in Firefox: chat round-trips incl.
  error/outage/reasoning-only paths, duplicate-submit guard, switch-
  while-pending misattribution toast, reload recovery (live + refused
  messages API), 404 conversation recovery; task create/cycle/revert/
  badge/day toggle/reload/server-restart; search kinds, jump navigation,
  keyboard flow, honest empty + 429-bucket states; notification feed
  badge/read/clear; day+density prefs incl. corrupt-storage fallback;
  gauges to `Live` and briefing/status truthfulness; canary-secret
  absence from page HTML *and* server stderr; cross-origin 403; JSON
  log stream with request ids.
- Two defects found and fixed with regression tests:
  1. `openSearchResult` jumped against a stale page list (search hit
     invisible → silent no-op) — now `await refreshTasks()` first, with
     an honest toast when the page window still cannot show the row.
  2. `pushNotification` never re-rendered the unread bell badge —
     now calls `updateNotifBadge()`.
- Not asserted headlessly and still manual: pixel-level styling/animation
  (row 9.15) and the real `document.hidden` tab-pause behavior.

---

## Phase R8 — Model management UI (COMPLETE — Phase 7A)

The provider tile was configuration-only ("Configured · ENV" from
`/api/status`). Phase 7A made the model policy genuinely controllable
from the UI, using the existing dialog/tile patterns only:

- Entry point unchanged: the provider tile's "Manage Providers" button
  (#llm-manage) now opens `#modal-models` instead of the static info
  modal (the 5.4 placeholder is retired for FreeLLMAPI).
- UI behaviors: policy header from `GET /api/settings/model`
  (selected id + `runtime override` / `environment default` source);
  bounded scrollable list with a search filter (large catalogs stay
  usable: a 150-model response renders as a scroll container, not a
  wall); per-model rows show only server-supplied data — unavailable
  rows are disabled with the gateway's own `unavailable_reason`, never
  invented descriptions; Apply = `PATCH /api/settings/model` with
  duplicate-submit + seq guards; Refresh = `GET /api/models?refresh=1`;
  "Back to environment default" = `DELETE /api/settings/model`.
- Honesty rules enforced in UI copy: catalog listing is configuration,
  not verified inference; chat reply tags use the model field from the
  actual chat response; failures keep the previous policy and surface
  the taxonomy code (`429 rate_limited`, `502 upstream_error`, ...);
  the provider tile sub-line names effective model/source, catalog
  state and the last verified reply.
- Browser coverage: `tests/test_browser_models.py`, 11 cases
  (open/load/filter/apply→real-chat/unavailable/failed-refresh/failed-
  select/clear/reload/150-model/secrets), all green; all 53 browser
  cases pass in the Phase 7A baseline run.

Not changed: chat composer/focus behavior, duplicate-submission guard,
frozen renderer (`static/jarvis-format.js`), dashboard layout, and the
other 7 provider tiles (still honest placeholders — P4.1 scope
decision pending).

## Phase R9 — Model routing & fallback metadata (COMPLETE — Phase 8A)

Backend reliability work reframed as honest metadata only; the layout
and all existing flows are untouched:

- The models dialog policy note gains one server-truth line when a
  fallback policy exists (`Fallback policy: <id> (one extra attempt on
  transient failures only)`), driven by the new `fallback` field of
  `GET /api/settings/model`; with no policy the line is simply absent
  (nothing invented).
- Chat reply tags keep coming from the actual response `model` field —
  under the fallback policy they may name the fallback model because
  that is what actually answered (browser test asserts the honest tag
  after a failed primary).
- No new UI controls: fallback policy is operator configuration
  (environment only), never a client-supplied field.

All 15 `test_browser_models.py` cases (11 Phase 7A + 4 Phase 8A) green
in the Phase 8A baseline run.
