# JARVIS Frontend Functionality Audit

> ## Phase 11B addendum (2026-10-25) — Operator Workspace redesign
>
> Phase 11B redesigned the static frontend into a calm, dark operator
> workspace (sidebar + work surface + dock composer, Codex/CLI-inspired).
> **This addendum supersedes the row-level inventory below** for removed
> and relocated surfaces; rows that are not listed here still describe
> working behavior (chat, tasks, goals, search, notifications,
> preferences, settings — all flows preserved; modal IDs unchanged:
> `#modal-goals`, `#modal-chat`, `#modal-settings`, `#modal-models`,
> `#modal-task`, `#modal-brief`).
>
> **Removed entirely (fabricated/decorative):**
> - "Live Intelligence Feed" (`#feed-list`, `tpl-feed-item`, `FEED` array) — all 12 hand-written demo events.
> - AI Core Overview list (`#overview-list`, `OVERVIEW` array) and its demo Memory/Voice/Agents rows.
> - Agents roster (`AGENTS`, `#agent-grid`, agent detail modal) — display-only roles.
> - Provider tiles (`PROVIDERS`, `#llm-grid`, `#llm-manage`) — superseded by the topbar model-policy chip + Settings Center (11A).
> - Clock (`#clock-date`/`#clock-time`), operator profile button (`#operator-btn`, `#pop-operator`), voice card + waveform, memory-map SVG, orbital-network SVG (`mulberry32` visualizations), decorative quick-action tiles (`qc-*`), `#nav-task-badge` (fabricated count).
>
> **Relocated (still real):**
> - Live CPU/RAM/disk gauges (`#gauges`) and monitor chip → new **Diagnostics** view.
> - Executive Briefing (`#brief-btn` → `#modal-brief`) → Diagnostics view; compiled from live status/telemetry/tasks/goals, never AI-generated.
> - Real saved-task timeline (`#timeline`) → Home right column (Today/Tomorrow segment preserved).
> - Durable-goal truth (active goal, recent goals, activity) → Home side panels, fed by `GET /api/goals` polling; goals/conversations/settings nav entries open the existing consoles.
>
> **New honest views (no fabricated data):** Home (`#view-command` with the
> CSS orb — state driven only by real chat/goal state), Projects, Obsidian
> Vault (setup state — no backend exists), Knowledge Graph (setup state),
> Tools (static capability facts; no `/api/tools` endpoint exists), and
> Diagnostics. Voice remains unimplemented: no microphone button anywhere
> (planned Phase 11C).
>
> **Nav set:** Home · Goals (console) · Conversations (chat) · Projects ·
> Knowledge (Obsidian Vault, Knowledge Graph) · Tools · Settings (modal) ·
> bottom rail: Diagnostics + Focus Mode (presentational, preserved).
>
> **Test baseline after 11B:** stdlib 730 OK (85 skipped); renderer 32/32;
> browser suites 85/85 OK; `node --check` clean; screenshots at 1440×900,
> 1280×800, 1024, 768, 390 (Playwright Firefox, fake upstream — no live
> inference, production DB untouched).

**Scope:** every visible and interactive element in `static/index.html`,
wired or unwired, as of Phase 3A (2026-10-19). This edition updates the
Phase 2D audit for the Phase 2B/2C/2E work: real chat via `POST /api/chat`,
composer focus restore, and ChatGPT-style Markdown rendering
(`static/jarvis-format.js`). The inventory rows and classifications below
are the source of truth; counts in §Summary are computed from these rows.
**Method:** full source inspection of `static/index.html` (538 lines),
`static/app.js` (1 064 lines), `static/styles.css` (1 032 lines),
`static/jarvis-format.js` (361 lines), `server.py` (403), `provider.py`
(269), `db.py` (169), `config.py` (118), plus a read-only SQLite
inspection of `data/jarvis.db` (4 conversations / 34 messages persisted)
and the automated suites. Since Phase 6B, real browser automation runs in
this environment (headless Firefox via Playwright, installed in a
dev-only venv that is never part of the runtime or the stdlib suite; see
`tests/browser_harness.py`): the whole chat / task / search /
notification / preferences / telemetry surface is exercised against the
real HTTP server with a fake provider — no live AI inference, no
production database. Rows carry browser evidence per feature.
Legacy UNVERIFIED markers remain only where a claim is genuinely
non-assertable (pixel-level visuals, 9.15).

**Test baseline (Phase 3A rerun):** `python3 -m unittest discover` →
**69/69 OK**; `node tests/test_format_render.js` → **32/32 OK**;
`node --check static/app.js` and `git diff --check` clean.

Legend: **WORKING** / **PARTIAL** / **DEMO/MOCK** / **PLANNED/PLACEHOLDER**
/ **BROKEN** / **DECORATIVE** / **UNVERIFIED** — definitions per the phase
brief. "S.I." = verified by source inspection only; "tested" = covered by
automated tests or a reproducible workflow run.

Backend surface available to the frontend (complete list, from
`server.py`): `GET /` + static files, `GET /api/health`, `POST /api/chat`
(same-origin enforced). **No other API exists.** Everything below is
classified against that reality.

---

## 1. Left navigation rail (`aside.rail`)

| # | Element | Source | Interaction | Status | Notes |
|---|---|---|---|---|---|
| 1.1 | Brand mark + "JARVIS Command Center" | `index.html` `.rail-brand` | none (not a control) | DECORATIVE | Simple SVG rings + text; no claims. |
| 1.2 | Nav item "Command Center" | `.nav-item[data-nav=command]` | switches to the real dashboard view (`showCommand()`) | WORKING | `app.js#initNav`; real view swap, `aria-current` maintained. S.I.; trivial and stable. |
| 1.3 | Nav item "AI Core" | `[data-nav=ai-core]` | shows generic secondary "frontend preview only" view | PLANNED/PLACEHOLDER | Honest placeholder card; `SECTIONS['ai-core'].desc` states no runtime. S.I. |
| 1.4 | Nav "Agents" (+ badge "6") | `[data-nav=agents]` | secondary preview view; badge 6 = actual demo roster length | PLANNED/PLACEHOLDER | Badge number is real *for the demo data* only. S.I. |
| 1.5 | Nav "Tasks" (+ live badge) | `[data-nav=tasks]`, `#nav-task-badge` | secondary preview view; badge = real task count from `GET /api/tasks` | **IMPLEMENTED (Phase 3D)** | Badge now counts persisted tasks (hidden while the server is unreachable). |
| 1.6 | Nav "Calendar" | `[data-nav=calendar]` | secondary preview view | PLANNED/PLACEHOLDER | Honest copy: no calendar service. |
| 1.7 | Nav "Memory" | `[data-nav=memory]` | secondary preview view | PLANNED/PLACEHOLDER | Honest copy: no store. |
| 1.8 | Nav "Conversations" (+ badge) | `[data-nav=conversations]`, `#nav-conv-badge` | secondary preview view | **FIXED badge (Phase 3C)** | Badge was fabricated "12"; now fetched from `GET /api/conversations?limit=100` (`count`/`count-1+"+"` when truncated), hidden while the server is unreachable. The nav section itself remains a planned placeholder. |
| 1.9 | Nav "Knowledge Base" | `[data-nav=knowledge]` | secondary preview view | PLANNED/PLACEHOLDER | Honest copy. |
| 1.10 | Nav "Tools & Skills" | `[data-nav=tools]` | secondary preview view | **FIXED (Phase 3D)** | Fabricated badge "18" removed; neutral navigation label only, styling/spacing preserved. No tool inventory was invented. |
| 1.11 | Nav "Workflows" | `[data-nav=workflows]` | secondary preview view | PLANNED/PLACEHOLDER | Honest copy; see also 5.4. |
| 1.12 | Voice Status card (rail bottom) | `#voice-status` | click/Enter opens honest info modal ("not connected") | DEMO/MOCK (functional dialog) | Waveform is a CSS animation (`initWaveforms`); never records audio. Dialog works; feature itself does not exist. |
| 1.13 | Focus Mode toggle | `#focus-toggle` | toggles `app.is-focus`, toast; persists in the versioned localStorage preferences key (`jarvis.preferences.v1`) and restores on reload (Phase 5) | WORKING (presentational) | Does exactly what it says (dims demo content); no backend meaning. S.I.; CSS `[data-focus]` rules in styles.css. |

## 2. Top command bar

| # | Element | Source | Interaction | Status | Notes |
|---|---|---|---|---|---|
| 2.1 | Hamburger `#menu-btn` | topbar | opens/closes mobile rail drawer + scrim | WORKING | `initRailDrawer`; responsive-only visibility. S.I. |
| 2.2 | "System Status" badge | topbar, `#sys-status`, live from `GET /api/status` (Phase 3C) | live state updates | **FIXED (Phase 3C, browser-verified in Phase 6B (Playwright Firefox E2E))** | Reflects actual backend state: Live / Live · AI not configured / DB degraded / Offline (fetch failure). "Demo Mode" wording removed. |
| 2.3 | Clock | `#clock-date`, `#clock-time` | ticking real local time every second | WORKING | Genuine data. S.I. |
| 2.4 | Search input + `/` shortcut | `#search` + `#search-results` | **REWORKED (Phase 5):** debounced (300 ms) `GET /api/search?q=` over persisted tasks/conversation titles/message content; results dropdown opens the matching view (task → timeline day + scroll, message/conversation → chat modal via the existing conversation pipeline); panel-highlight behavior and `/` shortcut retained; panel highlights stay honest local indexing | WORKING (browser-verified in Phase 6B (Playwright Firefox E2E)) | Backend: parameterized SQL with escaped LIKE wildcards (user `%`/`_` are literal), query 2–120 chars, limit default 20/max 100, 400 taxonomy, cross-origin 403, POST→405; result interleave deterministic; snippets bounded client-side; sequence token so stale responses cannot replace newer ones; results rendered with `textContent` only (no HTML injection). Message search scans the table (no FTS — documented limitation). 19 API tests + frontend source checks. Browser pass outstanding. |
| 2.5 | Layout density `#layout-btn` → popover Cozy/Compact | `#pop-layout` | sets `app[data-density]`; persisted in `jarvis.preferences.v1` and restored on reload (Phase 5) | WORKING (presentational) | styles.css implements `.app[data-density="compact"]` spacing — verified present. Persistence validated against allowed values; storage failure degrades to session-only. |
| 2.6 | Notifications bell `#notif-btn` → `#pop-notif` | **REWORKED (Phase 5):** live session-event feed | WORKING (browser-verified in Phase 6B (Playwright Firefox E2E)) | Real events only: server-confirmed task create/update (success or failure), observed system-status transitions (never the baseline first read), and recurring failures (single notification per active condition via dedupe keys, re-armed on recovery). Honest numeric unread badge (`#notif-badge`, hidden at 0 — static demo dot removed); opening the panel is the read; session-only scope stated in-panel ("Session only") with a Clear control; no persistence by design (opening the panel discards history, so storage would lie). No demo entries remain. |
| 2.7 | Settings gear `#settings-btn` → `#pop-settings` | static list of "presentation-only concepts" | WORKING panel, PLANNED content | PLANNED/PLACEHOLDER | Fully honest in its own copy: nothing is wired. |
| 2.8 | Operator button `#operator-btn` → `#pop-operator` | static profile card | WORKING panel, DEMO content | DEMO/MOCK | No account/session backend ("local demo profile"). |
| 2.9 | `pulse-dot` indicators (badge, live chip) | CSS animation | none | DECORATIVE | |

## 3. Command Center — Row 1

| # | Element | Source | Interaction | Status | Notes |
|---|---|---|---|---|---|
| 3.1 | AI Core Overview list `#overview-list` | static `OVERVIEW` array + **live System row from `GET /api/metrics` (Phase 4)** + live LLMs row from `/api/status` (3C) | none (non-interactive) | **PARTIALLY IMPLEMENTED (Phase 4, browser-verified in Phase 6B (Playwright Firefox E2E))** | System row composes real CPU/RAM/Disk (or honest "unavailable/pending" states); LLMs row stays live (3C). Memory/Voice/Agents/rows remain explicitly-labelled demo placeholders. |
| 3.2 | Orbital AI Core visualization | inline SVG + `#o-links/#o-nodes/#o-orbiters` filled by JS | none | DECORATIVE | Generated with seeded PRNG (`mulberry32`) — deterministic. Honored `prefers-reduced-motion` (orbiters become static). Visually excellent, claims nothing. |
| 3.3 | "Live Intelligence Feed" `#feed-list` + "View All Intelligence" `#feed-more` | static `FEED` array (12 items) | expand/collapse toggle | DEMO/MOCK (toggle WORKING) | All 12 entries describe the prototype itself (self-aware, mostly honest) — but nothing "live" arrives; no event source. The chat entry was **rewritten truthfully in Phase 3B** ("Chat runs on the local model"); entries 5/11 remain demo-only framing. |
| 3.4 | Panel hero caption "Prototype visualization…" | static | none | DECORATIVE (honest) | |

## 4. Command Center — Row 2

| # | Element | Source | Interaction | Status | Notes |
|---|---|---|---|---|---|
| 4.1 | Active Agents grid `#agent-grid` (6 tiles) | static `AGENTS` array | tile click/Enter opens detail modal | DEMO/MOCK (modal WORKING) | Detail modal shows honest "cannot execute tasks" note. No backend agents. |
| 4.2 | "View All" (agents) `#agents-viewall` | info modal listing the 6 demo agents | dialogue opens | DEMO/MOCK (dialogue WORKING) | |
| 4.3 | Mission Timeline `#timeline` | real tasks from `GET /api/tasks` (Phase 3D) | Today/Tomorrow tabs filter | **IMPLEMENTED (Phase 3D)** | Server-backed records, deterministic `updated_at DESC` order; honest loading/empty/disable(retry) states. No fabricated demo seeds remain. |
| 4.4 | Task status cycle buttons | `.tl-status` click → `PATCH /api/tasks/{id}` (queued→warn→done→planned) | badge recomputed optimistically | **IMPLEMENTED (Phase 3D)** | Optimistic with per-task sequence token: late responses cannot overwrite newer confirmed state; failure reverts + re-syncs + honest toast; server validates the status enum (not just JS). |
| 4.5 | "View Full Schedule" `#timeline-more` | honest info modal (calendar not connected) | dialogue opens | PLANNED/PLACEHOLDER (honest) | Underlying calendar feature does not exist — out of scope for 3D. |
| 4.6 | Quick Command "Start New Task" `#qc-task` | opens `#modal-task` form | submit → `POST /api/tasks` | **IMPLEMENTED (Phase 3D)** | Server-generated id; authoritative timestamps returned; duplicate-submit guard; failure keeps form values with an honest toast; task persists across reloads and server restarts. Client `maxlength=80` complements server 200-char validation; time/priority validated server-side. Autonomous scheduling/execution remains nonexistent (by design). |
| 4.7 | Quick Command "Open Calendar" `#qc-calendar` | honest info modal | dialogue opens | PLANNED/PLACEHOLDER | |
| 4.8 | Quick Command "Start Voice Chat" `#qc-voice` | honest info modal | dialogue opens | PLANNED/PLACEHOLDER | |
| 4.9 | Quick Command "Run Workflow" `#qc-workflow` | honest info modal ("workflow execution not implemented") | dialogue opens | PLANNED/PLACEHOLDER | |

## 5. Command Center — Row 3 & misc

| # | Element | Source | Interaction | Status | Notes |
|---|---|---|---|---|---|
| 5.1 | System Monitor gauges `#gauges` | **live `GET /api/metrics` sampling (Phase 4)**: CPU `/proc/stat` deltas (first sample = "Sampling…"), RAM `/proc/meminfo`, Disk `os.statvfs` on the data-directory filesystem | none | **IMPLEMENTED (Phase 4, browser-verified in Phase 6B (Playwright Firefox E2E))** | Values server-computed, clamped 0–100 client-side; honest Waiting/Sampling/Unavailable/Server-unreachable states; live/warn/err threshold styling reuses frozen token classes; monitor chip reflects live/partial/unreachable. Synthetic `GAUGES` array fully removed. No network/temperature/GPU gauge exists (and none was invented). |
| 5.2 | Memory Insights `#m-mem/#m-turns/#m-calls` | static text 3,180 / 22 / 14 | "View Memory Map" → honest info modal | DEMO/MOCK (modal WORKING) | Counts are fabricated. `#memory-net` SVG decorative. |
| 5.3 | LLM & Integrations grid `#llm-grid` (8 tiles) | static `PROVIDERS` array + live `GET /api/status` overlay (Phase 3C) | none (non-clickable tiles) | **FIXED for FreeLLMAPI (Phase 3C, browser-verified in Phase 6B (Playwright Firefox E2E))** | FreeLLMAPI tile now reads `provider_configured` from `/api/status` ("Configured · ENV" vs "Not verified"). Remaining 7 tiles stay honest placeholders ("not connected"/"planned") — wiring more providers remains a roadmap item (P4.1). |
| 5.4 | "Manage Providers" `#llm-manage` | honest info modal | dialogue opens | PLANNED/PLACEHOLDER | |

## 6. Secondary preview view (`#view-secondary`)

| # | Element | Source | Interaction | Status | Notes |
|---|---|---|---|---|---|
| 6.1 | Section preview card + title/desc | filled by `showSection()` from honest `SECTIONS` copy |—"Back to Command Center" `#sec-back` | WORKING (as an honest placeholder) | Back button restores real dashboard. |

## 7. Bottom command dock

| # | Element | Source | Interaction | Status | Notes |
|---|---|---|---|---|---|
| 7.1 | Dock widgets Location/Weather/Network | static text | none | DEMO/MOCK | All three fabricated ("22°C Overcast", "Local · Good"). |
| 7.2 | Dock dots + pulse puck | CSS/SVG animation | none | DECORATIVE | |
| 7.3 | "Talk to JARVIS" title + chip | static | none | **FIXED (Phase 3B)** | Chip now truthfully reads "Local model" with a live pulse indicator. |
| 7.4 | Dock composer `#chat-form` / `#chat-input` + send button | form submit → opens conversation modal, calls real `handleChatSubmit` | **WORKING** | Places real `POST /api/chat` (also opens modal). Placeholder fixed in Phase 3B ("replies are generated by the locally configured model"). Focus-restore fixed in Phase 2D. |
| 7.5 | "Executive Briefing" `#brief-btn` → `#modal-brief` | **data-driven (Phase 4):** composes application status (`/api/status` incl. "last known" marking while fresh polls fail), saved task totals by status (exact server aggregates), live telemetry (per-metric availability), persisted conversation count, and the unchanged truthful chat/agents/voice/memory lines | WORKING (browser-verified in Phase 6B (Playwright Firefox E2E)) | Briefing is deterministic and **not AI-generated** (chip states it). Provider "configured" is never presented as a verified upstream connection. Unavailable metrics are stated, never substituted. Feed/agents/voice/memory bullets stay honest demo placeholders. |

## 8. Dialogs / popovers (interaction itself)

| # | Element | Interaction | Status | Notes |
|---|---|---|---|---|
| 8.1 | Popover open/close/outside-click/Esc | `togglePop`, doc keydown | WORKING | S.I. |
| 8.2 | `dialog.showModal/close` + close buttons | `openModal/closeModal`, `[data-close-modal]` | WORKING | S.I. |
| 8.3 | `#pop-notif` close | WORKING | panel content is now real session events (2.6); Clear button is session-scoped. |
| 8.4 | `#pop-settings` close | WORKING | content PLANNED (2.7). |
| 8.5 | `#pop-operator` close | WORKING | content demo (2.8). |
| 8.6 | `#pop-layout` close + density buttons | WORKING | presentational (2.5). |

## 9. Conversation modal & chat system (the real feature)

| # | Element | Source | Interaction | Status | Notes |
|---|---|---|---|---|---|
| 9.1 | Chat submission (either composer) | `handleChatSubmit` → `POST /api/chat` | **WORKING** | Verified: 48 server-side unit tests, fake-upstream end-to-end flow, and the user's manual Phase 2C browser verification. Live `auto` inference succeeded in 2C. |
| 9.2 | Conversation ID reuse | `state.conversationId` reused every send | **WORKING** | Tested (fake-upstream flow replay + unit tests). Not persisted across reloads (page memory) — see 9.7. |
| 9.3 | Loading state + duplicate guard | typing dots; inputs/buttons disabled while pending | **WORKING** | S.I. + flow tests; visual dots use existing CSS. |
| 9.4 | **Composer focus restore** (Phase 2D fix) | `restoreComposerFocus` in `finally` | **WORKING** | Root cause: disabling the focused input drops focus to `<body>`. Fix restores the originating (or modal, if open) composer, caret at end, unless the user deliberately focused something else or the modal was closed. Phase 2E did not touch this function. **Source-inspected; manual browser confirmation (user side, Phase 2C flow) plus regression risk covered by code review in 3A.** |
| 9.5 | Safe error display | backend `error.message` shown with `Chat error` tag via `textContent` (`{plain:true}`) | **WORKING** | Server error taxonomy tested in 2B suite; network failure path covered by flow replay. |
| 9.6 | Safe rendering | user + error text via `textContent`; assistant text via `JARVIS_FORMAT.renderMarkdown()` only | **WORKING** | 2E: message path no longer uses blanket `textContent` (that flattened Markdown). Assistant HTML comes exclusively from `jarvis-format.js` (esc-by-construction; see 9.12–9.14). typing-dots `innerHTML` is a fixed string of `<span>`s; no user data. |
| 9.7 | Conversation history load / persistence in UI | **IMPLEMENTED (Phase 3B):** `GET /api/conversations` + `GET /api/conversations/{id}/messages`; in-modal history panel restores real SQLite transcripts through the safe renderer; `sessionStorage` recovery after reload | **WORKING** | 20 endpoint/flow tests (`tests/test_conversations_api.py`): ordering, bounds, truncation flags, 404/400/405/403 paths, fake-upstream end-to-end workflow, no-secret assertions. UI flows source-inspected; manual browser-verified in Phase 6B (Playwright Firefox E2E). |
| 9.8 | "New conversation" control | **IMPLEMENTED (Phase 3B):** `#chat-new` button in the modal header; resets `state.conversationId` + sessionStorage, clears the log, bumps the operation token | **WORKING** | Pending-request policy: `window.confirm` before switching/starting while a reply is in flight; the in-flight reply is never lost (persists server-side, visible in history) and can never be misattributed (token guard in `handleChatSubmit`). Source-inspected; manual browser-verified in Phase 6B (Playwright Firefox E2E). |
| 9.9 | Chat title chip in modal header | **FIXED (Phase 3B):** "UI-only · demo" replaced with truthful "Local model" live chip | WORKING | |
| 9.10 | Modal composer input placeholder | **FIXED (Phase 3B):** "simulated" wording removed; now "Message JARVIS" | WORKING | |
| 9.11 | Chat close button `#chat-close` | closes modal | WORKING | During a pending request the request continues server-side; on completion the modal-targeted restore correctly skips if the dialog is closed. S.I. |
| 9.12 | Markdown rendering of assistant replies (Phase 2E) | `static/jarvis-format.js` `renderMarkdown()` → `.chat-body` `.innerHTML` | **WORKING** | Scoped renderer (paragraphs, h1–h3, nested/ordered lists, fenced code w/ language label, inline code/bold/italic, http(s)/anchor links, blockquote, hr, GFM tables). 32/32 Node regression tests (`tests/test_format_render.js`) incl. XSS attack strings. Browser visual result source-inspected; user-side manual testing in 2E session. |
| 9.13 | Reasoning/control-token filtering + empty-reply fallback | `filterModelArtifacts()` inside `renderMarkdown()` | **WORKING** | Model reasoning blocks are stripped (regex built from char codes, no literal tokens in source); unterminated open tag truncates; if nothing user-facing remains, honest notice rendered ("returned internal reasoning only…"). Tested (5 cases); live `auto` policy leak observed in 2C and handled. |
| 9.14 | Link/URL safety | `safeHref()` — `http://`/`https://`/`#` only, after stripping control chars/whitespace | **WORKING** | `javascript:`/`vbscript:`/tab-crafted schemes reject to plain text; links carry `target="_blank" rel="noopener noreferrer"`. Tested (7 cases). |
| 9.15 | Rich-content chat styling | `styles.css` appendix (`.chat-body`, `.code-block`, tables, etc.) + jarvis bubble gradient | **UNVERIFIED (visual)** | CSS is deterministic and present; no browser automation to confirm rendering pixel-level. Text semantics verified via 9.12's HTML-string tests. |
| 9.16 | `GET /api/conversations` (Phase 3B) | `server.py::_handle_conversations`, `db.list_conversations` | **WORKING** | Newest-first (`updated_at DESC, id DESC`), SQL-level limit (default 50, max 100, `limit+1` truncation probe without N+1), bounded derived preview (`preview`, distinct from stored `title`), true `message_count`, same-origin enforced, taxonomy errors (400/403/405). 9 dedicated tests. |
| 9.17 | `GET /api/conversations/{id}/messages` (Phase 3B) | `server.py::_handle_conversation_messages`, `db.get_message_window` | **WORKING** | 32-lowercase-hex id validation (400 malformed), 404 unknown (distinguished from known-empty), SQL-bounded window (default 200, max 500), chronological output, `messages_total` + `older_omitted` truncation honesty, no model metadata synthesized (none is stored per message). 11 dedicated tests incl. full fake-upstream workflow. |
| 9.18 | In-modal conversation switcher (Phase 3B) | `#chat-history` → `#conv-panel`/`#conv-list`, `refreshHistory`/`selectConversation`/`loadConversation` | **WORKING (browser-verified in Phase 6B (Playwright Firefox E2E))** | Real records newest-first, genuine metadata/previews, active marking, loading + empty + recoverable-error states, truncation notice, no fabricated sample data. Rapid-selection race guarded by `state.convToken`. |
| 9.19 | New-conversation control (Phase 3B) | `#chat-new` → `newConversation()` | **WORKING (browser-verified in Phase 6B (Playwright Firefox E2E))** | Clears active id + sessionStorage; next send creates a fresh server conversation via existing `POST /api/chat`; uses the existing empty-chat state; both composers share the single active id. |
| 9.20 | Reload recovery (Phase 3B) | `restoreConversation()` on init, `sessionStorage['jarvis.conversationId']` | **WORKING (browser-verified in Phase 6B (Playwright Firefox E2E))** | Format-validated id; server-verified existence; transcript restored through the same pipeline; 404 clears the stale id; unreachable server keeps the id with an explicit retry; only a successful chat response persists a new id; no startup focus theft. |
| 9.21 | `GET /api/status` (Phase 3C) | `server.py::_handle_status` | **WORKING** | Read-only loopback status: `{db, schema_version, uptime_seconds, model_policy, provider_configured}`. Credential presence only — key/url/Authorization never included (test-asserted); no upstream/network calls (test-asserted upstream request count unchanged); `db=missing` degrades to 200 with `schema_version: null`; cross-origin 403; POST→405 taxonomy; HEAD served. 10 dedicated tests. |
| 9.22 | Live status wiring (Phase 3C) | `app.js refreshSystemStatus/refreshConvBadge` | **WORKING (browser-verified in Phase 6B (Playwright Firefox E2E))** | On init: `/api/status` → topbar badge states (live / no-ai / degraded / offline), FreeLLMAPI tile state, overview LLMs row; `/api/conversations?limit=100` → conversations nav badge (hidden on failure — never fabricates a count). Initial HTML carries no fabricated numbers; truthful pre-data states. |
| 9.23 | Task persistence backend (Phase 3D) | `db.py` tasks table + migration, `GET/POST /api/tasks`, `PATCH/DELETE /api/tasks/{id}` | **WORKING** | Additive v1→v2 migration in one transaction (8 dedicated migration tests: fresh init, idempotency, full v1-data preservation, rollback on failure, constraints). API: list with limit bounds/truncation/deterministic ordering; create with server id + taxonomy validation; PATCH allowed-fields-only/'empty_update' rejection; DELETE with durable absence; 404 vs malformed ids; 405 taxonomy for wrong methods (incl. PUT/OPTIONS now rejected); same-origin 403; body-size 413; no secrets/exception details. 35 dedicated API tests incl. the 8-step restart workflow. |
| 9.24 | Task UI persistence (Phase 3D) | `refreshTasks`/`submitTaskForm`/`cycleTaskStatus`, timeline render + badge | **WORKING (browser-verified in Phase 6B (Playwright Firefox E2E))** | Loads persisted tasks on init (loading/empty/error/retry states), create via POST with disabled-button duplicate guard, optimistic status cycling with per-task sequence token (stale responses cannot overwrite newer state) and revert + re-sync on failure, honest reload reconstruction from server records. No task operation touches the provider. 17 source-level frontend checks; browser-verified in Phase 6B (Playwright Firefox E2E). Phase 4 additions: exact `status_counts`/`day_counts` aggregates in the list response drive the badge and Overview/Briefing; a `tasksRevision` guard stops in-flight auto-refreshes from overwriting optimistic edits. |
| 9.25 | `GET /api/metrics` (Phase 4) | `metrics.py` + `server.py::_handle_metrics` | **WORKING** | stdlib sampling only: CPU from `/proc/stat` deltas between consecutive requests (thread-safe single snapshot, bounded; counter resets/zero deltas/malformed input → honest "sampling"/unavailable, never a fabricated %); memory from `/proc/meminfo` (`used = MemTotal − MemAvailable`, real bytes + percent; missing fields → unavailable); disk via `os.statvfs` on the data directory's filesystem (scope labelled honestly — not app-only usage; no absolute paths exposed); host uptime from `/proc/uptime` (distinct from server uptime in `/api/status`). Per-metric availability flags (`available`/`sampling`/`reason`), server `sampled_at`; one failed metric never kills the others (test-asserted with a forced `os.statvfs` failure). Same-origin 403, POST/PUT/OPTIONS → 405, HEAD served, no secrets/exception details, no provider/network calls (upstream-request-count test). 11 API tests + 36 module tests. Live smoke run verified honest first-sample CPU, real memory/disk/uptime values. |
| 9.26 | Dashboard refresh lifecycle (Phase 4) | `app.js refreshMetrics/startDashboardRefresh` | **WORKING (browser-verified in Phase 6B (Playwright Firefox E2E))** | Metrics poll every 6 s, tasks+status+conversations every 30 s, both paused while the tab is hidden; `visibilitychange` re-syncs on return; single-flight metrics guard + sequence token prevent stale overwrites; the auto task refresh retires when an optimistic task edit happened mid-flight (`tasksRevision`). Timer creation happens once in `init()` only. 26 source-level dashboard checks; browser-verified in Phase 6B (Playwright Firefox E2E). |
| 9.27 | `GET /api/search` (Phase 5) | `server.py::_handle_search`, `db.search_tasks/search_messages/search_conversations`, `db.like_pattern` | **WORKING** | Literal substring search over tasks (title/description), stored conversation titles and message content, newest-first per category, deterministic round-robin interleave. Parameterized SQL only; `like_pattern` escapes `\`, `%`, `_` with `ESCAPE '\'` so user input is literal (wildcard injection impossible); query 2–120 chars (400 invalid_request); limit default 20, cap 100, non-integer/out-of-range → 400 (SQL-level LIMIT); truncation flags honest per category and combined; bounded snippets around the first match (+1 fetch detects truncation without a count query); same-origin 403, POST→405, HEAD served, no secrets/exception details, no provider calls. `readonly=True` connection; message search scans without FTS (documented). 19 API tests incl. add/patch/delete reflectivity and category interleaving. |
| 9.28 | Phase 5 frontend: search wiring, notifications, preferences | `app.js fetchSearchResults/renderSearchResults/openSearchResult`, `pushNotification(Once)/updateNotifBadge`, `readPrefs/savePrefs/loadPrefs` | **WORKING (browser-verified in Phase 6B (Playwright Firefox E2E))** | Search: input debounced 300 ms with sequence token + AbortController (stale/superseded responses retire), dropdown built purely with DOM APIs + `textContent`/`CSS.escape` (no HTML injection surface), conversation/message results open the existing chat pipeline (`selectConversation`), task results re-render the right day and scroll to the record. Notifications: server-confirmed events only, dedupe keys collapse recurring failure states, badge hidden at 0 and numeric otherwise, open=read, Clear wipes the session list, no fabrication, no persistence. Preferences: single versioned key `jarvis.preferences.v1`, merge writes, strict value validation before restore, fail-soft to session-only; density/focus/day restored before first render; programmatic state changes (task modal auto-switch) do not write preferences. 23 source-level checks. |
| 9.29 | Preference persistence backend | `localStorage` only, no server surface | WORKING (client-only by design) | Exactly as scoped in Phase 5: density/focus/day survive reloads locally; nothing about preferences goes over the network, and no notification/task data is persisted by the UI. |
| 9.30 | Bounded API rate limiting (Phase 6A) | `ratelimit.py`, `server.py::_rate_limit_gate`, `RATE_LIMIT_POLICIES` | **WORKING** | Fixed-window, lock-protected, `time.monotonic()` in-memory limiter; per-policy buckets with an LRU-bounded identity map (≤ 256). Policies: `chat` 20/60 s (`POST /api/chat`), `search` 30/60 s (`GET /api/search`), `tasks_write` 60/60 s (`POST/PATCH/DELETE` tasks). Gates run after the existing same-origin 403 and before body reads/expensive work; unchanged 405 and 400 taxonomies; harmless dashboard reads (health/status/metrics/listings, polled every 6–30 s) stay un-limited (compat test asserts a full poll mix works). 429 response: `{"error": {"code": "rate_limited", ...}}` + integer `Retry-After` (≥ 1); no sleeps; 8-thread race test cannot overrun a window; limiter state resets on restart (documented; not multi-process). 18 dedicated tests (10 unit with fake clock + 8 API). | 
| 9.31 | Privacy-safe structured logging (Phase 6A) | `applog.py`, `server.py` log overrides + `_log_failure`, `provider.py::_log` | **WORKING** | One JSON line per event on stderr: `ts` (UTC), `event` (`http_request`/`handler_error`/`rate_limited`/`provider_error`), `request_id` (`req_`+24 hex, server-generated, client header never trusted; echoed in the JSON `X-Request-ID` header), `method`, `route` (query strings stripped; ids masked), `status`, `duration_ms`, `note`, `error_class`, `policy`, `retry_after`. Handler failures log exception **class names**, never exception text (forced-failure test with a canary exception message). Canary tests prove API keys, authorization headers, prompts, search queries and task titles never reach the stream; newline injection cannot split the stream; fields are length-bounded (200 chars); no persistent log store. 4 dedicated tests. |
| 9.32 | Browser E2E suite (Phase 6B) | `tests/browser_harness.py` + `tests/test_browser_{chat,tasks,search,dashboard,security}.py` | **WORKING** | Dev-only Playwright venv (never in the stdlib run): 42 test cases drive headless Firefox against the real server + temp DB + fake provider. Covers the full user surface: chat round-trips (dock → modal, markdown structure, XSS-inertness, typing state, duplicate-submit guard, provider 502 + unreachable, reasoning-only fallback, new-conversation id fork, reload recovery with live/unreachable server, late-reply misattribution toast, 404 recovery); tasks (form → server row, badge aggregates, status persistence per click, forced-500 revert with toast + warning feed, day toggle, reload + server-restart durability); search (task/message/chat kinds, result navigation with text-safe rendering, `/` focus, Enter jump, Esc clear, short-query no-call, honest empty state); notifications/prefs/dashboard (feed events + badge, read-on-open + clear, day/density persistence, invalid-prefs fallback, gauges reaching `Live` with 0–100 bounds, monitor chip, briefing counts/status honesty); security (canary key/URL/headers absent from page HTML and server stderr, single-line JSON logs with request ids, cross-origin 403 via non-app Origin, in-browser 34-request search bucket → honest `Search error (429)` UI). Suite runtime ≈ 4 min; hidden-tab pause and pixel-level styling remain non-assertable in headless runs (documented below). |
| 9.33 | Search-result jump refresh fix (Phase 6B) | `app.js openSearchResult` | **WORKING — defect fixed** | Browser test found: clicking a task hit relied on the page's task list state (stale when the task was created elsewhere or beyond the page window) — jump silently did nothing. Fix: `await refreshTasks()` before locating the timeline row; when the row still is not in the rendered (≤ 200 window) page list, an honest toast says so instead of silence. Regression browser tests + updated wiring test (`refreshTasks` replaces the stale `renderTimeline` expectation — it renders + re-reads server state). |
| 9.34 | Notification badge refresh fix (Phase 6B) | `app.js pushNotification` | **WORKING — defect fixed** | Browser test found: `pushNotification` incremented `state.unreadNotifs` but never re-rendered the badge, so the bell badge stayed hidden/`0` until some later code path refreshed it. Fix: `pushNotification` now calls `updateNotifBadge()` after `renderNotifications()`. Regression browser tests assert badge `1`/`2` after feed events and read-on-open behavior. |
| 9.35 | `GET /api/models` — authenticated catalog (Phase 7A) | `provider.py list_models/_parse_model_list`, `server.py::_handle_models` | **WORKING** | Catalog comes from the configured FreeLLMAPI gateway only (`/v1/models` through `provider.list_models`; Phase 9A: entries may carry a bounded gateway-supplied `display_name` used for evidence-based model verification — present only when the gateway provides it); no client-controlled URLs, no key in responses, redirect refusal + bounded timeout + 2 MB body cap, defensive OpenAI-format parsing (`data` list; junk entries skipped, never echoed; availability flags passed through only when the gateway supplies them). Small per-server cache (60 s TTL) absorbs repeat answers; `?refresh=1` forces one honest refresh; rate policy `models` 10/60 s caps the worst case. Error taxonomy reused verbatim (503 `not_configured`, 504 `provider_timeout`, 502 `auth_failed`/`connection_failed`/`redirect_rejected`/`malformed_response`, 429 `rate_limited`); POST → 405 with `Allow`, HEAD served, cross-origin 403, no secrets, no raw upstream bodies. Never runs inference (upstream chat-request count asserted unchanged). 17 dedicated API tests. |
| 9.36 | Model selection & persistence — `GET/PATCH/DELETE /api/settings/model` (Phase 7A) | `server.py::_handle_settings_model_get/_handle_settings_model_patch/_clear_model_override`, `meta` table | **WORKING** | GET reports effective policy + source (`runtime_override` vs `environment_default`) with **no upstream calls** (test-asserted). PATCH validates: shape/length/whitespace (config precedent), unknown fields rejected (never trusts client-supplied URLs or extra request fields), non-default selections validated against the current/fresh catalog (auto-allowed per gateway contract; unavailable entries rejected). Applied atomically via parameterized `INSERT OR REPLACE INTO meta('model_override')`; one chat request resolves its model once — a concurrent switch can never split a lifecycle. Persistence survives server restart; corrupt/invalid saved values recover to the environment default safely. DELETE clears the override (`PATCH {model: null}` is not accepted). Rate policy `model_settings` 30/60 s; cross-origin 403; taxonomy errors. 14 dedicated selection tests incl. restart, invalid-recovery, and conversation integrity. |
| 9.37 | Model management UI (Phase 7A) | `static/app.js` Phase 7A module + `index.html #modal-models` + `styles.css`, `llm-manage` entry point | **WORKING (browser-verified)** | The provider tile's "Manage Providers" button now opens a real modal: current policy + source line (authoritative server state), accessible filter for long lists (bounded scroll container; "no match" state), one-click select with duplicate-submit + stale-response (seq) guards, unavailable rows disabled with the gateway's own reason, explicit "Refresh catalog" (`?refresh=1`), "Back to environment default", and honest failures (catalog error keeps list + policy, selection failure keeps the previous policy; both surface the taxonomy code). No fabricated metadata; `chat-msg--tag` labels come from the actual chat response (`AI reply · <model>`), historical labels untouched. Browser suite drives all flows incl. apply→real chat with `auto`, 150-model filtering, reload persistence; canary key/URL never reach page, storage or logs (11 browser tests in `test_browser_models.py`). `/api/status` (9.21) extended: `effective_model`, `model_source`, `catalog {count, fetched_at, error}`, `last_inference` — the provider tile shows configuration, catalog state and "last verified reply" separately (listing ≠ verified inference). |
| 9.38 | Model routing & bounded fallback — honest UI metadata (Phase 8A) | `server.py` chat routing (`provider.complete_with_fallback`), `config.py JARVIS_FALLBACK_MODEL`, `static/app.js` policy line + reply tags | **WORKING (browser-verified)** | Chat inference is routed through one central bounded lifecycle: runtime override ⇒ single attempt (explicit pin, never silently substituted); otherwise primary = `JARVIS_MODEL` with **at most one** fallback attempt on `JARVIS_FALLBACK_MODEL` for transient/per-model failures only (auth/`not_configured`/`invalid_config`/`redirect_rejected` are never retried); same history every attempt; assistant persisted once after success; user message persisted exactly once. The UI always names the model that actually answered (`chat-msg--tag` from the real response), the models-dialog note adds a fallback-policy line when one is configured (and none when not), `GET /api/settings/model` exposes `fallback`, and chat error payloads can include a safe `attempts` list (ids + taxonomy codes; no prompt/key/upstream bodies). A successful fallback emits one sanitized `model_fallback` log event (browser-asserted). Provider-extracted tool-call proposals are validated structured data only — never executed. Tests: 30 fallback/routing API cases + 4 new browser cases (57 total browser suite). |
| 9.39 | Durable goal console — server-worker backed (Phase 8B) | `static/app.js` Phase 8B module + `index.html #modal-goals` + `styles.css`, `llm-goals` entry point | **WORKING (browser-verified)** | The LLM & Integrations panel gains a "Goal Console" tile button opening a real dialog over the durable-goal API: one objective textarea (`POST /api/goals`, duplicate-submit guard, 500-char cap, 503 `not_configured` refusal rendered verbatim), a bounded list with status chips + persisted `done/total` step counts, and full persisted detail (plan summary/criteria/risk, ordered steps with verification evidence, report with per-criterion ✔ marks and the review source, bounded events). Cancel/retry buttons exist only where the state machine allows (`reconciliation_required` rows never offer retry). Polling runs **only while the dialog is open**, every fetch carries a seq guard dropping stale responses, and closing the dialog stops the timer immediately. All rendering is DOM-built with `textContent` only (no `innerHTML` for any server value); the objective canary never reaches server logs; the console never calls the model endpoint itself (upstream chat-call count asserted). 10 browser cases in `test_browser_goals.py`. |
| 9.40 | Independent review provenance in the Goal Console (Phase 9B) | `static/app.js appendGoalReview` + worker `_review_gate` provenance | **WORKING (browser-verified)** | Goal detail renders the full review provenance as text rows: deterministic local validation result, independent-review status (`approved`/`requested revisions`/`rejected the plan`/`review failed`/`not available`), the reviewer identity **only** when that reviewer was actually invoked and produced a valid structured approval (rejections show the rejecting identity), bounded findings / missing criteria / recommended changes, advisory risk level, revision flag, and review round. There is no "Approved by Claude/OpenCode" badge element of any kind — an approval exists only as the truthful provenance text of an actually invoked reviewer. All reviewer output is rendered with the `textContent` primitives; browser-verified that reviewer-embedded `<img onerror>` markup never becomes DOM and cannot execute. Blocked-by-review goals keep their verdict/findings visible, and polling stays single-guarded. 2 browser cases in `test_browser_goals.py`. |
| 9.41 | Human write-approval flow in the Goal Console (Phase 10A) | `static/app.js renderProposals/renderProposalPreview/proposalAction` + `server.py` proposal endpoints + `worker.py` approval gates + `approvals.py` + `goal_approvals` (schema v4) | **WORKING (browser-verified)** | Goals proposing `workspace.create_file`/`workspace.edit_file` wait in `awaiting_approval`; the console detail renders a "Write approvals" panel: per-proposal chip (`#1 CREATE — PROPOSED` style), path, tool, byte count, before/after hash prefixes, and the exact 64-char approval digest. `Preview diff` fetches the bounded preview (proposed content + unified diff vs the CURRENT file, current sha match flag) and renders it in a scrollable `<pre>` with textContent only (proposal content is untrusted and never enters the DOM as markup). `Approve & execute` POSTs `{seq, digest}` — the server re-derives the digest from the persisted rows (plan drift / args tampering → 403/409), re-checks the current filesystem against the approved before-state, and the worker re-verifies everything at claim time before atomic execution (`approved→claimed` conditional update, `claimed→executed` on verification). `Reject` resolves the goal `blocked: approval_withdrawn` (non-retryable). Approvals are per-operation (goal-level approval cannot authorize a later edit); nothing is written before approval (file-absence asserted in browser tests). Chips show "awaiting approval". 4 browser cases in `test_browser_write_approvals.py`. |
| 9.42 | Verified-write + repair surface in the Goal Console (Phase 10B) | `static/app.js appendStepEvidence/renderProposalPreview/repair rows` + `verification.py` recipes + `worker.py` repair policy + `tools.py make_read_file_tool/make_recipe_tool` | **WORKING (browser-verified)** | New in Phase 10B: (a) `workspace.read_file` step evidence renders the bounded file preview (content in a `<pre>`, byte count, honest `truncated` flag, full-file sha256); (b) `execute tests` proposals display as `#2 execute tests — proposed` chips with a verification-metadata preview block before approval (runner `python3 -m unittest -v <module>`, guarded target, cwd=workspace root, 30 s timeout, "Executes project code: yes — runs with this process's OS permissions; no OS sandbox is enforced", minimal-environment note, and the same digest-bound authorization banner); (c) recipe steps show honest diagnostics (`recipe X · target · exit · duration · state: passed/failed/unavailable/timed_out`, bounded stdout/stderr rendering); (d) after the bounded repair loop the detail shows "Repair cycles used N of 2" plus the recorded previous verification failure (seq/tool/status/exit) — a blocked-after-repair goal never renders as success (browser test asserts `AssertionError` diagnostics + the repair row on a `blocked` goal). 3 browser cases in `test_browser_verify.py`. |
| 9.43 | Settings Center — real server-backed configuration (Phase 11A) | `static/index.html #modal-settings` + `static/app.js settingsState/fetchSettingsView/applyRoutingSettings/...` + `server.py /api/settings*` + `settingsstore.py` + `config.py env_managed` | **WORKING (browser-verified)** | The settings button opens a three-tab modal, all wired to the server (no local-only settings state): **Provider** — both fixed profiles with read-only base URLs, write-only password inputs for keys (saved → input cleared; only `key stored`/`no key yet`/environment-lock status is displayed, never a value; env-managed keys render disabled with "managed by environment variables"), Save/Clear/Test-connection (catalog-only, no inference — the result reports model count + the free/paid/unknown cost-class counts); **Model routing** — provider select, model + fallback fields with catalog-sourced suggestions (`#settings-model-options` datalist), a live catalog meta line showing per-model cost classification as distinct chips (`free (catalog-verified)` / `paid — may incur cost` / `cost unknown`), honest "not in the fetched catalog" for unknown ids, and unavailability; apply requires explicit cost confirmation for paid/unknown OrcaRouter models (409 `cost_confirmation_required` focuses the checkbox); **Application** — timeout/max-tokens/history/reviewer with environment-locked fields disabled and named in a "Environment-managed fields" note. Browser tests prove keys never appear in page text or localStorage, apply switches the real chat to OrcaRouter with confirmation, probe failures are honest, runtime saves persist across restart, and the new cost/availability hints resolve from the classified catalog. 9 browser cases in `test_browser_settings.py`. |

**Phase 9D note (2026-10-23):** real end-to-end orchestration
verified once (real gateway planning, real OpenCode reviewer approval
of round 1, real read-only execution with 5/5 criteria evidenced,
restart-identical persistence). No UI change; every row's status
stands as verified — the Goal Console now has one real
review-provenance record behind it (round 1, `external_review`,
`opencode CLI`) instead of browser-fixture-only evidence.

**Phase 9B-R note (2026-10-22):** the reviewer backend swapped from the
Claude Code CLI to the OpenCode CLI adapter (isolated HOME, deny-all
permissions, fresh session; browser identity fixtures updated). Goal
Console provenance rendering is unchanged — row 9.40 stays accurate for
the new backend — and no status is upgraded: a real review invocation
(2026-10-23, operator-authorized) proved the adapter end-to-end with
an `external_review`/revise record, but no goal in production has yet
relied on it; console rows keep rendering whatever the worker truly
recorded.

**Phase 10A note (2026-10-23):** first bounded write capability ships
(row 9.41): goals can propose `workspace.create_file` /
`workspace.edit_file` writes, and the console renders a "Write
approvals" panel with per-proposal state, hashes, exact digest, a
bounded preview (proposed content + diff vs the current file) and
Approve/Reject actions bound to the digest. Approval is impossible for
the model/reviewer to self-grant: the human must POST the digest, the
server re-derives it from persisted rows, and the worker re-verifies it
again before atomic execution. Everything renders textContent-only.

**Phase 9C note (2026-10-22):** no UI changes shipped; no row added and
no status upgraded. Runtime evidence classes (catalog / live inference /
configured / installed / authenticated / independently reviewed) are
tracked in `docs/IMPLEMENTATION_ROADMAP.md` and `README.md`; in
Phase 9C the model pair and reviewer stays unactivated (glm 429,
claude unauthenticated), so the console's provenance rows remain the
honest local-only record — exactly as rows 8.1–8.6 and 9.40 document.

## Phase 6B — browser verification evidence

- Real *Playwright* Firefox (headless) in `/tmp/opencode/jarvis-e2e/venv`
  with the browser build under `~/.cache/ms-playwright` (dev-only,
  user-local; the repo and its stdlib suite stay dependency-free — the
  299-test stdlib discovery skips the browser files when Playwright is
  absent).
- Every stack is per-test: temp directory, temp SQLite DB
  (`JARVIS_DB_PATH`), fake OpenAI-compatible upstream on an ephemeral
  port, `create_server` on loopback ephemeral port — identical to the
  HTTP-suite discipline with the browser loop added.
- Total: **42 browser tests, all green** (≈ 4 min);
  stdlib discovery: **341 tests** = 299 stdlib + 42 skipped browser
  cases; `node tests/test_format_render.js` **32/32**.
- Two user-facing defects found and fixed with regression tests (rows
  9.33, 9.34). No third defect found across the remaining workflows —
  in particular the misattribution token guard, the failed-write
  revert, the XSS-inert rendering, the reload-recovery paths and the
  rate-limit exhaustion path behaved exactly as designed.
- Not assertable in this headless harness and unchanged in scope:
  hidden-tab pause (real `document.hidden` cannot be emulated here;
  covered by source inspection + unit checks), pixel-level gauge/alert
  styling (row 9.15 remains the honest visual exception).

## Phase 11A — browser verification evidence (Settings Center)

- Same harness discipline (real server + temp DB + fake provider +
  headless Firefox per test); the fake upstream doubles as the
  OrcaRouter catalog with the live OrcaRouter URLs monkeypatched to it,
  so the real service is never contacted.
- `test_browser_settings.py`: **9 cases** — profiles list without
  secrets, write-only key save/clear, env-locked key display,
  test-connection without inference (chat-call count asserted
  unchanged), apply→chat actually served by `orcarouter/auto` with the
  paid/unknown 409 confirmation gate, fallback honesty (free-only
  fallback refusal), probe-failure honesty, runtime persistence across
  restart, and the catalog-sourced cost/availability hints (three
  honest cost states + datalist suggestions). Canary strings assert no
  credential ever reaches page text, HTML, or localStorage.
- Browser suite total after 11A: **85 cases** (84 previous + 1 new).

## 10. Elements with no handler but actionable-looking labels

Verified exhaustively against `app.js` wiring: **none found beyond what
is already classified above.** Every `<button>` in `index.html` has an
event listener (the two `chip` spans are non-interactive badges by
design). The provider tiles (5.3) and dock widgets (7.1) are
intentionally non-interactive divs/spans. The only "silent" elements are
decorative SVGs and the pulse dots.

Cross-check of all `id` attributes referenced in `app.js` resolves in
`index.html` (no dangling `byId()` targets found — a null-guarded path
exists only where legitimately optional). `index.html` loads
`jarvis-format.js` before `app.js` (the 2E-permitted one-line addition).

---

## Data authenticity matrix

| Data shown | Authenticity |
|---|---|
| Clock time | **Genuine** |
| Chat replies + conversation IDs (via `/api/chat`) | **Genuine (server-verified)** |
| Rendered Markdown of assistant replies | Genuine source → HTML produced client-side by `jarvis-format.js` (escaped by construction; tested) |
| Task list/status/creation (via `/api/tasks`) | **Genuine and durable (server-verified, Phase 3D)** — SQLite row-for-row, survives reload and server restart |
| System Monitor CPU/RAM/Disk (via `/api/metrics`) | **Genuine (host measurements, Phase 4)** — CPU deltas (`/proc/stat`), memory (`/proc/meminfo`), disk (`os.statvfs` on the JARVIS data filesystem); unavailable values always labelled, never invented |
| Overview System row, Briefing status/tasks/telemetry bullets | **Genuine (composed from `/api/status`, `/api/metrics`, exact task aggregates, Phase 4)** |
| Overview list (Memory/Voice/Agents), agent roster, agent modals, feed, notifications, provider tiles, memory counts, dock widgets, search corpus, memory map visualization, orbit/waveform visuals | **Fabricated/static demo data** (largely labelled as such in the UI) |
| Provider statuses (5.3) | FreeLLMAPI tile **live** (Phase 3C); remaining 7 tiles honest placeholders |
| Server-side chat history (SQLite) | **Real and durable, but unreachable from the UI** (no GET endpoint, 9.7) |

## Workflow-tracing findings (updated Phase 3B)

- **Conversation lifecycle:** *IMPLEMENTED in Phase 3B.* Reopening a
  historical conversation: `GET /api/conversations` (list) +
  `GET /api/conversations/{id}/messages` (transcript window) with the
  in-modal switcher. Starting a genuinely new conversation: explicit
  `#chat-new` control; the next message creates a new server
  conversation through the unchanged `POST /api/chat`. Persisted
  transcripts verified in SQLite (4 conversations / 34 messages in
  `data/jarvis.db`) are now reachable from the UI.
- **Task lifecycle:** *IMPLEMENTED in Phase 3D (rows 9.23/9.24).*
  Create through the modal → `POST /api/tasks` (server id, server
  timestamps); status cycle → `PATCH /api/tasks/{id}` per rendered
  button; the timeline and the Tasks nav badge both reconstruct from a
  single `GET /api/tasks?limit=200` on load. Deletion exists on the API
  (DELETE, tested durable) but no UI control precedes it — no invented
  surface. A late PATCH response cannot overwrite a newer confirmed
  status (sequence token), and every network failure surfaces an
  honest toast + revert + re-sync rather than optimistic-only state.
- **Provider/model configuration:** *fully configurable in the UI
  (Phase 11A, row 9.43; configuration visibility since Phases 3C/7A).*
  The topbar badge and provider tiles read the real state from
  `GET /api/status` (`provider_configured` = credential presence only;
  `model_policy` = the effective routing policy with its source). The
  Settings Center now owns provider selection, write-only key
  management, catalog-validated model + optional fallback selection,
  and the runtime bounds — with environment-managed fields shown as
  locked. The 7 non-FreeLLMAPI provider tiles remain honest
  placeholders (roadmap P4.1); OrcaRouter is reachable through the
  Settings Center, not through those tiles.
- **Persistence/recovery:** SQLite survives restarts (WAL, versioned
  schema). Restored transcripts now survive page reloads via
  sessionStorage + server verification (9.20). Tasks are persisted
  server-side and reconstructed from `/api/tasks` on every load
  (Phase 3D: optimistic status updates roll back and re-sync on
  failure, so UI state never silently diverges from local disk).
  Remaining page-memory surfaces: badges (agents roster count),
  density, focus. Chat requests completing after a user switches
  conversations are filed server-side under their own conversation and
  are visible in history; a token guard prevents visually
  misattributing them (9.8/9.18).
- **System telemetry:** *IMPLEMENTED in Phase 4 (rows 9.25/9.26).* CPU
  usage is computed from counter deltas between consecutive metrics
  polls (the first poll reports a sampling state instead of a number);
  RAM/disk/uptime read real host interfaces. Partial telemetry
  failures degrade per metric; stale responses can never overwrite
  newer gauge state (sequence token), and polling pauses when the tab
  is hidden.

## Summary — counts by status

Counts computed row-by-row from the tables above (77 classified rows
+ integrity findings; Phase 4 re-baselined).

| Status | Count | Rows |
|---|---:|---|
| WORKING | 66 | 1.2, 1.5, 1.8, 1.10, 1.13, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 4.3, 4.4, 4.6, 5.1, 5.3, 6.1, 7.3, 7.4, 7.5, 8.1–8.6, 9.1–9.8, 9.9–9.14, 9.16–9.26, 9.27–9.29, **9.30, 9.31 (Phase 6A)**, **9.32–9.34 (Phase 6B)**, **9.35–9.37 (Phase 7A)**, **9.38 (Phase 8A)**, **9.39 (Phase 8B)**, **9.40 (Phase 9B)**, **9.41 (Phase 10A)**, **9.42 (Phase 10B)**, **9.43 (Phase 11A)** |
| PARTIAL | 1 | 3.1 (System+LLMs rows live; Memory/Voice/Agents rows remain demo placeholders) |
| DEMO/MOCK | 7 | 1.12, 2.8, 3.3, 4.1, 4.2, 5.2, 7.1 |
| PLANNED/PLACEHOLDER | 12 | 1.3, 1.4, 1.6, 1.7, 1.9, 1.11, 2.7, 4.5, 4.7, 4.8, 4.9, 5.4 |
| BROKEN | 0 | |
| DECORATIVE | 5 | 1.1, 2.9, 3.2, 3.4, 7.2 |
| UNVERIFIED | 1 | 9.15 (visual styling only) |
| **Total classified rows** | **94** (80 through Phase 5 + rows 9.30–9.31 added in Phase 6A + rows 9.32–9.34 added in Phase 6B + rows 9.35–9.37 added in Phase 7A + row 9.38 added in Phase 8A + row 9.39 added in Phase 8B + row 9.40 added in Phase 9B + row 9.41 added in Phase 10A + row 9.42 added in Phase 10B + row 9.43 added in Phase 11A) | |
| **Remaining stale-copy surfaces** | **0** | All audited stale-copy findings are fixed. Truthful demo labels remain only where the feature genuinely is a demo (memory counts, PRNG visuals, feed, operator profile, weather/location dock). |

*Phase 3A listed 6 stale-copy surfaces; Phase 3B fixed 5 (dock title chip,
both composer placeholders, modal header chip, feed chat entry, briefing
reminder, voice-modal hint) and the switcher/new-chat/reload-recovery
features moved 9.7–9.8 from PARTIAL/PLACEHOLDER to WORKING.*

### Highest-risk nonfunctional elements

1. **Memory map / Memory Insights (5.2)** — fabricated counts remain.
   No memory subsystem is attached (deferred as (C); no store exists).
2. **Agents/voice/workflows (4.1/4.2/1.12, 2.6)** — still display-only;
   honest chips and copy stay until product decisions land.
3. **Dock widgets (7.1) and feed (3.3)** — hand-written demo data;
   presentation-only risk.
4. **9.15 pixel-level styling only** — gauge transitions and chip
   styling are functionally browser-verified (Phase 6B asserts gauge
   values, chips and feeds), but visual fidelity (CSS transitions,
   animations) remains eyeball-only. All functional browser claims are
   now test-covered.

*(Phases 3A–4 resolved the previous top risks: task durability 1.5/4.6,
fabricated conversations/tools badges 1.8/1.10, unreachable history
9.7/9.8, provider-tile contradiction 5.3 for FreeLLMAPI, synthetic
gauges 5.1, and the demo-data briefing 7.5. Phase 6B closed the browser
verification gap across all user workflows and fixed the two defects
it exposed — see rows 9.32–9.34.)*

---

*Inventory compiled entirely from source inspection + test-suite + DB
evidence (Phase 7A baseline: 384 stdlib-discovered tests OK (331 run +
53 optional browser cases), 53 browser E2E cases executed OK with real
Playwright Firefox, 32 renderer tests OK). Phase 7A added rows 9.35
through 9.37 and made the FreeLLMAPI tile genuinely controllable:
authenticated catalog, validated runtime model selection that reaches
real chat payloads, filterable management UI, and catalog-state /
last-verified-inference accuracy in `/api/status`. 9.15 pixel-level
styling remains the single explicit UNVERIFIED row.*

### Phase 13A update — Knowledge tab (Settings Center)

New Settings Center tab (row 9.4x family): Knowledge. Verified with the
existing Playwright browser suite (`test_browser_vault.py`, 3 tests,
temporary vault + isolated DB, fake upstream): the tab shows the honest
"Not configured" state; configuring a temporary vault through the real
input + Save button persists it via `PATCH /api/settings` (server-side
existence/directory validation; invalid updates show the typed error
and preserve the previous configuration); the Refresh action calls
`POST /api/vault/refresh` and the status line reports the truthful
indexed count, vault name, last-scan timestamp, and partial/different-
vault markers; the absolute filesystem path never leaks into the page
(asserted). Vault reading itself stays out of the UI for this phase —
no note browser or graph widgets are faked; those remain documented
future work. Frozen renderer untouched (32 renderer tests OK).

### Phase 13B update — voice controls in the composers

Both composers (dock + conversation panel) gained verified voice
controls: push-to-talk mic (explicit start/stop, truthful listening
state, aria-live status lines), recognition-language select persisted in
preferences, and a speak-replies toggle (aria-pressed, default OFF,
persisted). Verified with `test_browser_voice.py` (18 tests, real
Firefox): unsupported-browser honesty with fully functional text chat;
transcript-into-composer without auto-send (interim results status-only);
duplicate-callback guard; explicit one-message submission; permission/
network/no-speech states; cleanup on stop, modal close, and pagehide;
opt-in speech of the completed reply only with working Stop Speaking;
graceful unavailable/failed synthesis; no composer blocking while
speaking; a hostile voice transcript creates zero goals. No fake
waveforms or activity indicators exist anywhere. Frozen renderer
untouched (32 renderer tests OK).
