# JARVIS Implementation Roadmap — Phase 3A (authoritative)

> **EXECUTION STATUS (Phase 6A, 2026-10-20):** P0–P2 complete, Phase 5
> complete, **Phase 6A complete** (bounded rate limiting + privacy-safe
> structured logging on the read/mutation-heavy endpoints). Remaining
> roadmap items (P4.1 provider management; P4.4 agents/voice/memory/
> workflows/calendar) require an explicit (C)-type product decision
> before implementation.

**Supersedes** `docs/FRONTEND_IMPLEMENTATION_ROADMAP.md` (Phase 2D
edition, kept as historical context; its R1–R6 phases are absorbed and
renumbered here). Feature IDs reference
`docs/FRONTEND_FUNCTIONALITY_AUDIT.md` (Phase 3A edition).

**Ground rules (unchanged):** stdlib-only Python backend; SQLite
persistence with versioned schema; loopback-only server; credentials
server-side/env-only; no new frontend frameworks or dependencies;
frozen Phase 0 visual design; no live inference inside any test suite.

**Requirement types:** every item below is typed as
 **(A)** existing advertised functionality to implement,
 **(B)** engineering work making (A) safe/reliable/true, or
 **(C)** optional new scope requiring a separate product decision.
A generic "(B)" annotation on a shared platform item (endpoints, tests)
means it is the enabling engineering for other type-(A) items.

---

## P0 — Core correctness: make the UI stop lying *(quick win)* ✅ (chat surface)

| Item | Type | Audit IDs | Work |
|---|---|---|---|
| P0.1 ✅ Replace stale demo labels on the real chat surface | (A) | 7.3, 7.4, 9.9, 9.10, 3.3 | DONE (3B chat surface; 3C topbar badge). |
| P0.2 ✅ Fabricated badges correction | (A)/(C) | 1.8, 1.10, 1.4 | DONE (Phase 3C: conversations badge → `GET /api/conversations`. Phase 3D: Tools badge "18" removed — neutral label, no inventory invented). |
| P0.3 ✅ Briefing line-accuracy | (B) | 7.5 | DONE Phase 3B: truthful chat bullet + corrected reminder paragraph. |

No API/schema changes. Verified by grep-clean acceptance criteria plus
renderer/database tests untouched.

---

## P1 — Conversation lifecycle ✅ (implemented Phase 3B)

**Implemented as planned; every item test-verified or source-inspected
with manual browser verification outstanding (see audit 9.7–9.20).**

| Item | Type | Work |
|---|---|---|
| P1.1 `GET /api/conversations` | (B) | id, title, updated_at + message count, newest first, bounded (e.g. ≤50), same-Origin enforcement identical to `/api/chat`. |
| P1.2 `GET /api/conversations/{id}/messages` | (B) | chronological bounded window (≤200 rows / 64 KiB cap), 404 unknown id, 400 on bad ids; reuses `db.get_messages`. |
| P1.3 New-conversation control | (A) | "New chat" button in the conversation modal header: resets `state.conversationId`/clears log (confirm if a request is pending); next send creates a fresh server conversation. |
| P1.4 Conversation switcher | (A) | list endpoint in the modal; click loads a historic transcript into `#chat-log` (rendered through the existing safe renderer) and switches the active id; current conversation marked. |
| P1.5 Post-reload recovery | (A) | last used conversation id restored (sessionStorage) with a safe server-side existence check — page reload resumes the same thread instead of silently orphaning it. |
| P1.6 Error/empty affordances | (B) | recoverable states for `conversation_not_found` and empty list; no reload required. |

Persistence: none new (schema v1 suffices; `db.get_messages` exists).
Security: read-only; no key material anywhere (extend leak assertions to
these endpoints). Unresolved IDs → 404 JSON in chat taxonomy shape.

Acceptance criteria: reload → reopen prior conversation and continue;
new-conversation creates different server id (assert via responses);
list endpoint unit-tested (shape/limits/isolation/404); cross-conversation
isolation tested; renderer reused for restored messages; manual browser
pass on both composers.

---

## P2 — Dashboard reflects verified reality

| Item | Type | Audit IDs | Work |
|---|---|---|---|
| P2.1 `GET /api/status` + live topbar badge | (A) | 2.2, 5.3 | Status payload: db state, uptime, configured model policy id, `provider_configured: bool`, optional non-token reachability probe of the gateway. Topbar badge shows real state. Key never appears in any response (test asserted). |
| P2.2 Data-driven provider tiles | (A) | 5.3 | FreeLLMAPI tile state from `/api/status` ("Connected"/"Not configured"); OmniRoute/others keep honest placeholders; model name shown without exposing routing internals beyond policy id. |
| P2.3 ✅ Real tasks behind the task UI | (A) | 1.5, 4.3, 4.4, 4.6 | DONE (Phase 3D): `tasks` table (schema v2 + tested migration), `GET/POST /api/tasks`, `PATCH/DELETE /api/tasks/{id}`; modal writes through; status cycle persists; badge = server count. Validation mirrors the chat taxonomy. |
| P2.4 ✅ System monitor from real telemetry | (A) | 5.1 | DONE (Phase 4): `GET /api/metrics` (stdlib `/proc/stat` deltas, `/proc/meminfo`, `/proc/uptime`, `os.statvfs`); gauges show real CPU/RAM/disk with honest sampling/unavailable states; no sampling library or subprocess. |
| P2.5 ✅ Overview + briefing from real state | (A) | 3.1, 7.5 | DONE (Phase 4): System row + briefing compose from `/api/status` + `/api/metrics` + exact task aggregates (`status_counts`/`day_counts`); demo rows stay explicitly labelled. |

Dependencies: P1 precedes P2.2 only in so far as the conversations count
may feed the tiles; P2.3 is independent.

---

## P3 — Agent/task execution *(product decision required)*

The agent roster (4.1, 4.2) and the "Agents/Workflows" concepts are
display-only. No runtime, scheduler, or execution engine exists, and no
derivable intended behavior defines what agents would do. Roadmap item:
**(C)** — blocked on a product decision (agent capabilities, safety
boundaries, command allowlist). No implementation scheduled on its own.

---

## P4 — Integrations, configuration, operational tooling

| Item | Type | Work |
|---|---|---|
| P4.1 ✅ Provider management surface | (A/C) | DONE (Phase 11A Settings Center): real configuration view for provider selection, write-only key save/clear, catalog-validated model + fallback policy, runtime bounds, and test-connection/test-model — persisted server-side via `settingsstore.py` (env > user store > defaults). Adds the fixed `orcarouter` profile; base URLs stay server-side allowlisted. The 7 remaining provider *tiles* stay honest placeholders (they are demo surface, not configuration). |
| P4.2 ✅ Rate limiting + request throttling | (B) | DONE (Phase 6A): bounded, thread-safe, in-memory fixed-window limiter (`ratelimit.py`, `time.monotonic()`); per-policy buckets — `chat` 20/min on `POST /api/chat`, `search` 30/min on `GET /api/search`, `tasks_write` 60/min on task mutations; harmless dashboard reads stay unlimited; 429 + `Retry-After` + `rate_limited` code without touching body parsing; restart resets state. |
| P4.3 ✅ Server logs / diagnostics | (B) | DONE (Phase 6A): privacy-safe structured JSON event logging on stderr (`applog.py`) with server-generated `X-Request-ID` correlation, sanitized untrusted fields, class-name-only error reporting, and an explicit never-logged list. Deviation from the historical wording: the log is a stream (no rotating file, no persistent store) — that is the honest scope, not an omission; a file sink would be a config decision. |
| P4.4 Memory / knowledge / voice / calendar / workflows | (C) | Advertised capability areas with **no derivable intended behavior** (audit §6, 4.8, 5.2, 1.6/1.7/1.9/1.11). Each needs a written product decision (counts meaning, corpora, service choices) before any build. Until then they remain honest placeholders. This roadmap explicitly does NOT schedule them. |

---

## P5 — UX, accessibility, observability, release quality

- Search over real data (2.4) — index conversations/tasks post-P1/P2.3.
- Real notifications feed (2.6) — server events ring buffer, wired to P4.3.
- Frontend persistence for density/focus (2.5, 1.13) — localStorage.
- Accessibility pass: dialog focus traps, `aria-live` chat announcements,
  visible focus rings audit (browser verification via manual checklist).
- DOM-level regression tests: decide browser harness (jsdom vs manual
  checklist) — recorded as an open dependency; never at the price of a
  new runtime dependency without approval.
- Release checks: README parity, `git diff --check`, full suites green.

---

## Dependency graph

```
P0 (truthfulness) ─────────────── independent, immediate
P1 (conversation lifecycle) ──┐
                              ├─ P2 (dashboard from real state)
P2.3 (tasks) ─────────────────┘
P2 → P5 (search/notifications over real data)
P3, P4.4 → blocked on product decisions (type C)
```

---

## Phase 6 — Next implementation phase (selected)

### Phase 3B — "Conversation lifecycle & truthfulness" ✅ COMPLETE (2026-10-19)

Implemented: P0.1/P0.3 copy fixes; `GET /api/conversations`; `GET
/api/conversations/{id}/messages`; in-modal switcher; `#chat-new`
control; sessionStorage reload recovery; convToken race guards;
recoverable empty/error states. Evidence: 20 new endpoint/flow tests
(89 backend total), 32 renderer tests retained, stale-copy grep clean
(topbar badge deliberately deferred), `node --check`, py_compile,
`git diff --check` all clean. Manual browser pass still outstanding
(documented limitation — no browser automation in this environment).
Deviation from plan: none material; `POST`ing to the new read endpoints
additionally returns the taxonomy 405 (small consistency hardening).

### Phase 3C — "Provider & application status visibility" ✅ COMPLETE (2026-10-19)

Implemented exactly as planned, minus the optional network probe
(deliberate: `provider_configured` expresses credential presence only;
a reachability probe would risk hanging requests and make tests
nondeterministic):
`GET /api/status` (shape `{db, schema_version, uptime_seconds,
model_policy, provider_configured}`; no key/URL/Authorization ever; no
upstream calls — both test-asserted; DB problems → 200 degraded state);
topbar badge live (live / no-ai / degraded / offline); FreeLLMAPI tile
data-driven ("Configured · ENV" vs "Not verified"); conversations nav
badge wired to the real count (hidden when server unreachable — the
fabricated "12" is gone). Plus the truthful-copy fixes that would
otherwise have contradicted the new live truth (FEED entry 11, feed/backend
entry, notifications provider item, conversations section description,
overview LLMs row, mockReply provider line). Evidence: 10 new status
tests (99 backend total across all suites), 32 renderer tests retained,
`node --check`, `git diff --check`, token check clean. Manual browser
pass outstanding (documented). P0.2 remainder: Tools badge "18" relabel
stays open (deferred with product decisions).

### Phase 3D — "Task persistence" ✅ COMPLETE (2026-10-19)

Implemented as planned:
- `db.py` schema v2 — `tasks` table + index added to v1 databases in a
  single `BEGIN IMMEDIATE` transaction (idempotent, rollback-safe on
  injected failure, row-for-row preservation of all v1 data, versioned
  refusal beyond `SCHEMA_VERSION`). CRUD helpers
  (`create/get/list/update/delete_task`) in the same module.
- `GET /api/tasks` (limit 1–200, default 50, truncation flag,
  deterministic `updated_at DESC, id DESC`), `POST /api/tasks` (server
  32-hex id, server timestamps, status/priority/day/time taxonomy
  validation, ≤200/≤2000/≤20-char fields), `PATCH
  /api/tasks/{id}` (title/description/status only, no-op →
  `empty_update`), `DELETE /api/tasks/{id}` (durable absence).
  Taxonomy errors throughout: 400 `invalid_task`/`invalid_request`/
  `invalid_json`/`empty_update`, 413/415, 404 `task_not_found`,
  405 with `allow` (incl. PUT/OPTIONS now explicitly rejected on the
  new paths), 403 for cross-origin; task bodies ≤16 KB share the chat
  body protections.
- Frontend: tasks load from `GET /api/tasks` on init (loading/empty/
  disabled-retry states — no demo seeds remain); creation POSTs and
  adopts the authoritative server record (disabled-button duplicate
  guard); status cycling is optimistic with a per-task sequence token
  (late responses cannot overwrite newer confirmed state), reverts +
  re-syncs + honest toast on failure; tasks nav badge counts real
  records and stays hidden while the server is unreachable. Task
  creation/status lifecycle never touches the provider.
- Truthful-copy sweep across the task surface (SECTIONS, FEED entry,
  mockReply, briefing header + timeline bullet + comment, modal note,
  toasts): no "page memory"/"resets on reload" claims remain on any
  task surface.
- Tools badge "18" removed (P0.2 remainder closed); `[hidden]` CSS
  guard added so the wide-screen `display:block` override cannot
  resurface an empty badge pill.

Evidence: 35 task-API tests + 8 migration tests + 17 frontend
source-level checks (159 backend tests across all suites, run twice);
32 renderer tests retained; `node --check`; `git diff --check`; user
DB verified untouched (4 conversations / 34 messages, migrated to v2
only by server startup — read-only inspection). Deviations from plan:
no browser automation in this environment → reload-persistence and
revert-on-failure are source-inspection + HTTP-integration verified,
manual browser pass outstanding; DELETE has no UI control (the
pre-existing UI never had one — no invented surface; endpoint is
tested and available).

### Phase 4 — "Real telemetry & data-driven overview" ✅ COMPLETE (2026-10-19)

Implemented as planned:
- **`GET /api/metrics`** (`metrics.py`, no subprocess/shell/external
  libs): CPU usage from `/proc/stat` aggregate-counter deltas between
  consecutive requests (thread-safe `CpuSampler`, ONE previous
  snapshot — bounded; first sample honestly reports a `sampling`
  state instead of fabricating a percentage; zero/negative deltas,
  counter resets, malformed lines and unreadable `/proc` all degrade
  to labelled unavailable states); RAM from `/proc/meminfo`
  (`used = MemTotal − MemAvailable`, real bytes + percent; missing
  `MemAvailable`/`MemTotal` → unavailable); disk via `os.statvfs` on
  the filesystem containing the configured data directory, labelled
  "filesystem containing the JARVIS data directory" (not app-only
  usage), with no absolute paths in the response; host uptime from
  `/proc/uptime` (kept distinct from server uptime in `/api/status`).
  Contract: per-metric `{available, percent|bytes, sampling?,
  reason?, units?}`, top-level `sampled_at` — one failing metric
  never destroys the others (forced-failure test-asserted). Security:
  same-origin 403, POST/PUT/OPTIONS → taxonomied 405, HEAD served,
  no secrets/paths/exception details, zero provider/network calls
  (upstream-request-count test-asserted).
- **Gauges:** synthetic `GAUGES` array deleted; values come from the
  server, clamped client-side (finite, 0–100); honest Waiting / Sampling… /
  Unavailable / Server-unreachable / Live notes reusing frozen token
  classes (+3 small CSS state rules); the monitor chip and the
  Overview System row go data-driven; stale gauge responses cannot
  overwrite newer state (sequence token).
- **Overview/Briefing:** composed from `/api/status` (cached with a
  "last known" prefix while fresh polls fail and still shown without
  conflating configured vs verified upstream), `/api/metrics` (each
  metric labelled or omitted when unavailable) and exact task totals
  via two aggregate SQL queries added to the task list response
  (`status_counts`, `day_counts` — global counts, not page counts).
  Briefing chip states it is not AI-generated; fabricated "CPU 15% ·
  RAM 54% · Disk 40%" line removed.
- **Refresh lifecycle:** one `init()`-only loop — metrics every 6 s,
  tasks+cons+status every 30 s — both paused while the tab is hidden
  with a `visibilitychange` re-sync; single-flight metrics;
  `tasksRevision` guard so an in-flight auto-refresh can never
  overwrite an optimistic task edit.

Evidence: **235 backend/JS tests across all suites, run twice, all
green** (159 prior + 36 `tests/test_metrics.py` + 11
`tests/test_metrics_api.py` + 3 new count tests + 26
`tests/test_dashboard_frontend.py`); renderer 32/32 untouched; live
smoke on the real host verified the honest first-sample CPU plus real
memory (46%)/disk (48%)/uptime values via a **copy** of the database in
/tmp (never the live file). Deviations: no browser automation → gauge
transitions/chip/briefing rendering remain source-inspected with a
manual browser pass outstanding; no network/temperature/GPU metrics
were added because none can be measured reliably with stdlib-only
locals (documented limitation).

### Phase 5 — "Search over real data, notification truths, persisted preferences" ✅ COMPLETE (2026-10-20)

**Scope:** the remaining (B)-type audit surfaces — 2.4 real search
(index conversations + tasks server-side behind a bounded endpoint or
loopback search contract), 2.6 notifications populated from real
application events (task created/updated, provider state changes —
never synthetic activity), 2.5/1.13 persisted density/focus
preferences:

1. Bounded real-data search: `GET /api/search?q=` over task
   titles/descriptions + conversation titles/previews (SQL LIKE with
   escaping, limit 20, same-origin, taxonomy errors).
2. Frontend search path consumes it; keeps the current local filtering
   only as a fallback when the server is unreachable (honest chip).
3. Notifications: derive events from actions the user actually took
   this session; no scripted "provider connected" entries.
4. Persist density + focus + preferred day in `localStorage`
   (bounded, non-sensitive).

**Why next:** last remaining type-(B) items that need no product
decision; the search data already exists in SQLite (no migration);
notifications/preference work is small and self-contained. Provider
management (P4.1), rate limiting (P4.2–P4.3), agents/voice/memory/
operator panels similarly stay behind (C) product decisions.

**Test plan:** search endpoint tests (escaping/limit/same-origin/no
cross-conversation leaks), notification-truthfulness source checks,
preference persistence checks, full-suite regression.

**Measurable acceptance criteria:**
- Search finds real persisted conversations and tasks; a server
  restart does not change results.
- Notifications only report things that actually happened.
- Density/focus/day persist across reloads without touching the DB.
- All suites stay green; `git diff --check` clean.

**Definition of done:** implemented + automated tests + audit/roadmap/
README updates + final report; manual browser checklist recorded.

**Delivered (2026-10-20):**
- `GET /api/search` (`db.search_*` helpers + `server._handle_search`):
  literal substring search over tasks (title/description), conversation
  titles and message content with `ESCAPE '\'` parameterized LIKE (user
  wildcards are literal), query 2–120 chars, limit default 20/max 100,
  per-category-at-limit truncation flags, deterministic round-robin
  interleave, bounded snippets, readonly connection. 19 API tests.
- Frontend search (audit 2.4): debounced 300 ms dropdown fed by the API
  with a sequence token + AbortController; `textContent`/`CSS.escape`
  only; task results jump to the right day + scroll; message/conversation
  results open the chat pipeline. `no_cross_conversation` privacy holds
  by design (message rows always join their conversation).
- Notifications (audit 2.6): static demo array removed; the feed now
  holds only server-confirmed task creates/updates (success or failure),
  observed status transitions (baseline read excluded) and deduped
  recurring failure states; honest numeric badge hidden at 0; open= read;
  session-only scope stated in-panel with a Clear button.
- Preferences (audits 1.13 / 2.5): single versioned key
  `jarvis.preferences.v1`; density/focus/day restored before first
  render, validated against allowed values, fail-soft, merge-saved on
  user changes only.
- Audit re-baselined: rows 2.4/2.6 → WORKING; rows 9.27–9.29 added
  (total 80 rows: WORKING 54, PARTIAL 1, DEMO/MOCK 7, PLANNED 12,
  DECORATIVE 5, UNVERIFIED 1).
- Test baseline: **277 backend/frontend tests** + 32 format-render
  tests, green; `git diff --check` clean. Manual browser checklist: see
  final report (outstanding, as in Phases 3–4).

---

### Phase 6A — "Security & operational hardening" ✅ COMPLETE (2026-10-20)

**Scope:** bounded API rate limiting (P4.2), privacy-safe structured
logging (P4.3, minus a rotating file sink — see note), security and
regression tests, documentation. No frontend redesign, no schema
change, no new dependencies, no remote access.

**Delivered:**
- `ratelimit.py`: fixed-window, lock-protected, `time.monotonic()`
  in-memory limiter with LRU-bounded bucket map (≤ 256 identities) and
  injectable clock for tests. Concurrency race test (8 threads) proves
  a window cannot be exceeded through races.
- Policies in `server.py` (explicit constants): `chat` 20/60 s,
  `search` 30/60 s, `tasks_write` 60/60 s. GET reads (health/status/
  metrics/tasks/conversations) are untouched so the Phase 4 dashboard
  refresh loop stays intact; 405 method taxes are evaluated by the
  existing dispatch before any limiter interaction; cross-origin 403
  and validation 400s keep their established taxonomy (a `429
  rate_limited` is uniquely identifiable, with `Retry-After` ≥ 1).
- Gate placement: `POST /api/chat` (after 403 check, before body read),
  `GET /api/search` (before query validation/DB open), `POST
  /api/tasks` and `PATCH/DELETE /api/tasks/{id}` (before body read).
  HTTP/1.0-style one-request-per-connection means a blocked early
  response cannot desynchronize a reused connection.
- `applog.py`: one JSON line per event on stderr; fixed field set
  (`ts`, `event`, `request_id`, `method`, `route`, `status`,
  `duration_ms`, `note`, `error_class`, `policy`, `retry_after`).
  `request_id` (`req_` + 24 hex, server-generated, client header never
  trusted) is emitted on `http_request`/`handler_error`/`rate_limited`
  and echoed in the JSON responses' `X-Request-ID` header. Routes are
  query-parameter-free with ids masked; untrusted values are
  control-character-stripped and length-bounded; handler failures log
  exception class names, never exception text; provider adapter logs
  flow through the same structured stream (fixed category strings).
  No persistent log store.
- Canary regression tests (`tests/test_logging_security.py`): API-key,
  prompt, search-query, authorization-header and body canaries never
  appear in the captured stream; newline injection cannot split the
  JSON stream; fields are length-bounded; the 429 path carries no
  secrets; a forced internal failure (`CanaryDbFailure`) returns 500
  with no details and logs only the class name.
- Rate-limit API tests (`tests/test_rate_limit_api.py`, 8 tests) +
  limiter unit tests (`tests/test_ratelimit.py`, 10 tests, fake clock —
  no sleeps): under-limit success, 429 + valid retry, window reset,
  per-policy isolation, races, bounded lakes, dashboard-poll
  compatibility, and unchanged 405/validation behavior under a
  depleted window.
- Documentation: README "Rate limiting & operational logging (Phase
  6A)" section with full policy table, 429 contract, restart semantics
  and never-logged list; audit rows 9.30/9.31 added.

**Test baseline after Phase 6A:** **299 backend/frontend tests** +
32 format-render tests, green twice consecutively; `git diff --check`
clean. Live smoke against a checkpointed copy of the real user DB:
existing flows unaffected, log stream fully structured, no query
leakage. Manual browser checklist item added to the final report.

---

## Explicit dispositions (every incomplete audit feature)

| Audit state | Disposition |
|---|---|
| 9.7/9.8 history & new-conversation | **DONE (Phase 3B)** |
| 9.7-adjacent reload recovery | **DONE (Phase 3B)** |
| 2.2, 3.3, 7.3, 7.4, 9.9, 9.10 stale copy | **DONE (3B + 3C)** incl. live topbar badge |
| 5.3 provider tiles | **PARTIAL DONE (3C):** FreeLLMAPI live; others → **P4.1** |
| 1.8 conversations badge | **DONE (Phase 3C)** |
| 1.10 tools badge relabel | **DONE (Phase 3D)** — "18" removed, neutral label |
| 1.5/4.3–4.6 tasks durability & endpoints | **DONE (Phase 3D)** — schema v2 + `/api/tasks` CRUD + persisted UI lifecycle (rows 9.23/9.24) |
| 1.5 overview & gauges | **DONE (Phase 4)** — `GET /api/metrics`, real gauges |
| 3.1/7.5 overview & briefing data | **DONE (Phase 4)** — System row + briefing data-driven; demo rows stay labelled |
| 2.4 search, 2.6 notifications, 2.5/1.13 persistence | **DONE (Phase 5)** — `GET /api/search` + debounced frontend dropdown; session-event notification feed; one versioned localStorage preferences key |
| 4.1/4.2 agents, nav sections (1.3–1.11 except tasks), 4.5/4.7–4.9, 5.2, 5.4 | **Defer as (C)** — product decision required; honest placeholders remain |
| Voice (1.12, 4.8) | **Defer as (C)** |
| Memory/knowledge/tools/workflows (1.6–1.11, 5.2, 5.4) | **Defer as (C)** |
| Operator/session (2.8) | **Defer as (C)** — auth framework out of local product scope |
| Rate limiting/logs | **DONE (Phase 6A)** — `ratelimit.py` + `applog.py`; see P4.2/P4.3 notes |
| Provider management UI (2.7/5.4) + non-FreeLLMAPI tiles | **FreeLLMAPI DONE (Phase 7A)** — authenticated model catalog (`GET /api/models`), validated runtime selection deployed to real chat requests (`GET/PATCH/DELETE /api/settings/model`, persisted via the `meta` table), filterable UI in the provider tile. Non-FreeLLMAPI tiles remain deferred (P4.1 scope decision) — each needs its own real service decision; honest placeholders stay until then |
| 9.15 visual styling verification | **Verify (P5)** — manual browser checklist; no new testing dependency without approval |
| 7.1 dock Location/Weather/Network widgets | **Defer as (C)** — each needs a real service decision; honest "Demo" chips remain until then |

---

## Phase 7A — Provider & model management (COMPLETE, 2026-10-21)

Goal: make the existing FreeLLMAPI integration genuinely controllable —
inspect the provider's model catalog, apply a policy to real chat
requests, persist it, and keep configuration honestly separate from
verified inference. No live inference anywhere in the phase; every test
runs against the fake upstream and isolated temp databases.

Delivered:

- `provider.py`: `list_models()` — authenticated `GET /v1/models`
  through the same transport protections as `chat_completion`
  (loopback-only `normalize_base`, redirect refusal, bounded timeout,
  2 MB body cap), reusing the entire error taxonomy. `_parse_model_list`
  parses the OpenAI list envelope defensively: junk/blank entries are
  skipped (never echoed), availability flags are passed through **only
  when the gateway supplies them** — a listed entry without flags counts
  as available, never as "verified inference".
- `server.py`:
  - `GET /api/models` — served from a 60-second per-server cache; a
    live fetch happens only on stale/empty cache or an explicit
    `?refresh=1`; rate policy `models` (10/60 s); never runs inference.
  - `GET /api/settings/model` — effective policy + source
    (`runtime_override` / `environment_default`) with zero upstream
    calls; PATCH validates shape/length/whitespace plus catalog
    availability (one authenticated validating fetch allowed when the
    cache is cold — applying is an explicit user action); DELETE clears
    the override. Cluster of disables in a single rate policy
    `model_settings` (30/60 s); unknown fields rejected; no client can
    ever set base URL, key, authorization or arbitrary fields.
  - Persistence: parameterized row in the existing `meta` table
    (`model_override`), validated on read — a corrupt saved value logs
    and recovers to the environment default. Chat resolves its model
    once per request, so a concurrent switch cannot split a lifecycle;
    completed transcripts keep their original labels.
  - `/api/status` extended (backward-compatible): `effective_model`,
    `model_source`, `catalog {count, fetched_at, error}` and
    `last_inference {model, at}` — catalog state and verified
    inference are now separate facts.
- Frontend (preserving layout, tokens and the frozen renderer): the
  provider tile sub-line names effective model/source, catalog state
  and last verified reply; "Manage Providers" opens `#modal-models` —
  bounded filterable list (150-model case covered), one-click apply with
  duplicate-submit + stale-response guards, disabled unavailable rows
  with gateway reasons, explicit refresh, clear-override, honest failure
  states. Chat reply tags keep using the actual response model.
- Tests: `tests/test_models_api.py` (32 API tests: catalog taxonomy,
  caching + `?refresh=1`, no-inference assertion, availability pass-
  through, selection validation, restart persistence, invalid-save
  recovery, conversation integrity, chat payload regression), browser
  suite `tests/test_browser_models.py` (11 cases: open, load, filter,
  apply→real chat with `auto`, unavailable rows, failed refresh,
  failed selection, clear, reload persistence, 150-model filter,
  secret-freedom incl. storage + logs). `fake_upstream.py` gained a
  `do_GET` for `/v1/models` with a separately configurable response.

**Test baseline after Phase 7A:** 384 stdlib-discovered tests OK
(331 executed + 53 optional browser cases) and 53 browser E2E cases
executed OK (Playwright Firefox, ≈ 5 min), 32 renderer tests OK,
`git diff --check` clean. No staging or commits; docs updated
(audit rows 9.35–9.37; disposition table above).

## Phase 8A — Reliable brain: model routing & bounded fallback (COMPLETE, 2026-10-21)

Scope honored: reliable inference behind ONE central call interface,
deterministic primary/override/fallback precedence, bounded retries that
only help, honest observability, tool-call representation groundwork —
still no live inference claims, no autonomous behavior, no renderer
rebuild, no orchestrator.

- Config (`config.py`): `JARVIS_FALLBACK_MODEL` env (default empty =
  fallback disabled; validated like `JARVIS_MODEL`: ≤ 128 chars, no
  whitespace). The intended product defaults (GLM 5.3 Flash primary,
  DeepSeek 4.1 Flash fallback via the user's OpenCode API) are operator
  configuration — exact ids must come from live-smoke-verified
  catalog/authenticated evidence, never guessed from display names.
- Central interface (`provider.complete_with_fallback`): one bounded
  chat lifecycle over the existing `chat_completion` adapter. Returns
  `{reply, model (actually used), fallback_used, attempts[]}`; on total
  failure re-raises the LAST taxonomy error with `attempts` attached.
- Deterministic precedence: runtime override active ⇒ it is the sole
  attempt (explicit pin honored, never silently substituted); otherwise
  primary = `JARVIS_MODEL` with at most ONE fallback attempt on
  `JARVIS_FALLBACK_MODEL`. Retryable only when a second model on the
  same gateway helps (timeout, connection, rate-limit, request 4xx
  rejection, upstream 5xx, malformed, empty reply); never on
  `not_configured` / `invalid_config` / `auth_failed` /
  `redirect_rejected`. Same history for every attempt (context
  preserved), assistant message persisted once after success, user
  message persisted exactly once before the first attempt (no
  duplicated side effects, and an already-executed conversational
  action is never replayed).
- Observability: sanitized `model_fallback` event (request id, model
  ids, failure codes, `outcome`) — never prompt content, key, or
  upstream bodies; chat error payloads may carry an `attempts` list
  (ids+codes only); `GET /api/settings/model` exposes the `fallback`
  policy field; the models dialog note shows the fallback policy when
  configured and says nothing about it when not (no invented lines).
- Tool-call readiness (representation only): `provider.parse_tool_
  proposals` validates the OpenAI `tool_calls` message shape into
  bounded structurally-safe proposals (`name` ≤ 64 chars from a safe
  id charset, JSON-object arguments); anything malformed raises
  `malformed_response`. Proposals are inert data — execution and
  permission checks belong to future orchestrator phases.
- Tests: `tests/test_fallback_api.py` (30: single-call success,
  primary timeout/error/malformed/rate-limit/empty then one fallback,
  history reuse without replay, honest both-fail `attempts`, bounded
  retry counts incl. fallback timeout, auth not retried, override +
  restart + corrupt-save precedence, no-policy behaviours, equal-id
  dedup, `not_configured`, sanitized `model_fallback` log event, and
  12 `parse_tool_proposals` validation cases). `fake_upstream.py` grew
  per-call scriptable `chat_responses` and `delay_queue`; browser
  suite extended to 15 cases (fallback policy line shown/absent,
  failed-primary → fallback success with honest `AI reply · model`
  tag, sanitized `model_fallback` log evidence, secret-freedom).

**Test baseline after Phase 8A:** 418 stdlib-discovered tests OK
(361 executed + 57 optional browser cases) and 57 browser E2E cases
executed OK (Playwright Firefox), 32 renderer tests OK, node-parsed JS,
`git diff --check` clean. No staging or commits; docs updated (README
routing section, audit row 9.38).

## Phase 8B — Central orchestrator & durable goal execution (COMPLETE, 2026-10-22)

Scope honored: ONE durable-goal pipeline (persisted truth in SQLite),
deterministic planning through the Phase 8A brain, strict validation,
state-machine-gated lifecycle, cancel/retry/restart recovery, honest
blocking with named reasons, read-only autonomous tool class, exact
recorded provenance — no world model, no multi-goal parallelism, no
extra capabilities, no fabricated success.

- Persistence (`db.py` SCHEMA_VERSION 3): additive migration from v2
  preserving all existing tables/rows. New tables: `goals` (id,
  objective, status, error_code, cancel_requested, plan_json,
  report_json, counts, timestamps), `goal_steps` (per-step status,
  attempts, verification, bounded argument/result JSON), `goal_events`
  (bounded, sanitized lifecycle trail). Migration is idempotent,
  refuses future schemas, and `check_ready()` **never** creates missing
  databases (recovery paths pre-check with `_db_exists()`).
- Lifecycle (`goalstate.py`): ALLOWED transitions only, applied as
  atomic conditional UPDATEs (`transition(ts, from_status, …)`).
  Terminal states are final. `retryable()` = blocked AND the reason
  isn't `reconciliation_required`. Cancellation: immediate for
  queue-held states, `cancel_requested` flag for worker-held states
  (planning/ready/executing/verifying), honored at every phase/step
  checkpoint.
- Planning (`planner.py`): one structured plan over
  `provider.complete_with_fallback` (Phase 8A brain), strict
  deterministic validation (registered tools only; typed bounded args
  validated via the tool's own `validate_args`; 1–12 steps;
  dependencies strictly ascending/duplicated-free — acyclic by
  construction; `risk` exactly `"low"`; every criterion must share
  significant tokens with a step addressing it; prompted criteria
  echoed verbatim). Bounded repair attempt, then blocked with a safe
  reason (`invalid_plan`, `not_json`, `unsupported_capability`,
  `criteria_not_covered`, `risk_not_low`, `planner_timeout`,
  `upstream_error`, `rate_limited`, `provider_timeout`).
- Safety (`reviewer.py`): deterministic local check always gates
  (permission class + registry membership + validate_args replay).
  An *independent* reviewer is dependency-injected and **deliberately
  unconfigured** in this phase (`UnavailableReviewer` → review
  `unavailable`); the plan/report record exactly `local_validation`
  — never a simulated independent verdict, logged as
  `review_unavailable`.
- Tool registry (`tools.py`): the permission boundary. Exactly one
  Phase 8B capability — read-only `workspace.list_files` under
  `JARVIS_WORKSPACE_ROOT` (relative paths only, resolved containment,
  no symlink escape, no file contents, depth 8 / 1000 entries / 64 KB
  bounds, sensitive-name exclusions: `.git`, `.env*`, secret/key/
  certificate names, credentials/secret/token substrings). Every tool
  can carry an independent result `verifier`
  (`tool.verify(args, result)`); the shipped verifier re-checks the
  tool's own claims rather than trusting them.
- Worker (`worker.py`): single thread, FIFO, serial per-goal;
  per-goal wall-clock budget (300 s) and per-step attempt budget
  (attempts ≤ 2 — original claim counts as one). Success requires
  every step `done` AND every criterion evidenced by a real persisted
  step (`blocked: criteria_unverified` otherwise — never assumed).
  Step failures escalate per to failure taxonomy; `step_retry_
  exhausted` blocks (retryable); overrun of the wall-clock budget
  blocks (`goal_time_exceeded`). Unavailable reviewer never fails a
  goal — it just isn't an independent approval.
- Recovery: restart paths scan persisted goals, never create
  databases or goals; completed steps are never re-run; interrupted
  idempotent steps reset and re-run once within the attempt budget;
  interrupted non-idempotent steps whose outcome is unknown block
  with `reconciliation_required` — requiring an operator decision
  (409 on retry attempt), never an auto-repeat or fabricated success.
  Recovery is idempotent (re-running it cannot double-increment
  attempts or duplicate events).
- API (`server.py`): `POST /api/goals` (canary-free envelope, 2 KiB
  cap, 500-char objective, provider-config required → 202
  `{id, status:"queued"}` | 503 `not_configured`), `GET /api/goals`
  (newest-first, bounded, honest persisted progress counts — no
  fabricated percentages), `GET /api/goals/{id}` (plan/steps/report/
  events with bounded payloads; > 8 KB results summarized with an
  honest `omitted` flag), `POST …/cancel` and `POST …/retry` through
  the same state gates (409 `invalid_state` on terminal or
  non-retryable blocked). Cross-origin 403 intact; `_goal_action`
  id-extraction fixed; `_goal_json_field` tolerant of
  pre-parsed structures. Rate policy `goals` 10/60 s. Worker tied to
  server lifecycle: `create_server(start_worker=True)` +
  `server_close()` stop with a time-bounded join.
- UI (row 9.39 in the frontend audit): Goal Console dialog —
  submit/list/detail, status chips, persisted progress, cancel/retry
  gated by real status, poll only-while-open with seq guard,
  textContent-only rendering.
- Tests: `tests/test_orchestrator_units.py` (53), `tests/test_goals_
  db.py` (18), `tests/test_goals_api.py` (34) stdlib; browser
  `tests/test_browser_goals.py` (10 Playwright cases incl. the full
  round-trip to `succeeded` with a real worker thread, blocked-reason
  chips + gated retry, cancel while the poller runs, stopping the
  poller on dialog close, and the 503 not_configured refusal).
  `test_logging_security.py` hardening: interpreter `ResourceWarning`s
  (GC under full-suite load) are filtered test-side; worker poll-loop
  exceptions log a fixed safe note (`goal_worker_error`,
  `note="scan error"`) — never exception text, so the JSON log stream
  cannot be polluted.

**Test baseline after Phase 8B:** 533 stdlib-discovered tests OK
(466 executed + 67 skipped browser) and 67 browser E2E cases executed
OK (Playwright Firefox; 57 prior + 10 goal-console), node-parsed JS,
`git diff --check` clean. No staging or commits; docs updated (README
goals section + API list + env var + rate table, audit row 9.39).

## Phase 9A — Real model validation & production configuration (OBSERVATION-STAGE COMPLETE, 2026-10-22)

Scope honored: evidence-based mapping of the two requested display
names against the real authenticated catalog, an explicitly gated
two-mode operator smoke script, fake-upstream verification of every
fallback-behavior guarantee, and deliberate non-configuration while
live-smoke evidence is missing. No inference was performed during the
phase; no settings were changed.

- Catalog evidence (real FreeLLMAPI gateway, 279 authenticated
  entries, fetched through the existing provider protections —
  redirect refusal, size cap, junk-skip; no key ever printed):
  - Requested primary `GLM 5.3 Flash` → **exact unique match**:
    id `glm-5.3-flash`, display name "GLM 5.3 Flash", `available:
    true`. No inversion this name is inferred anywhere — the match is
    by exact display-name equality against a single entry.
  - Requested fallback `DeepSeek 4.1 Flash` → **NOT RESOLVED**: no
    catalog entry carries this exact display name. Nearest names
    (review-only, never auto-selected): `DeepSeek V4 Flash`
    (`deepseek-v4-flash`, available), `DeepSeek V4 Flash Thinking`
    (`deepseek-v4-flash-thinking`, available),
    `deepseek-v4-flash-0731` (`no_key`/unavailable). Until the
    operator either supplies the exact intended id or confirms one of
    the catalog names as the intended fallback, the ambiguity is
    reported and nothing is configured.
- Additive safe changes to the provider layer (no behavior change):
  1. `provider.list_models` now passes through the gateway's
     `display_name` (present-only, bounded 200 chars) so catalog-based
     identity verification has evidence to work with instead of
     guessing from ids. Returns shape otherwise unchanged; `/api/models`
     grows an optional `display_name` field only.
  2. `provider.chat_completion(..., include_model=True)` (opt-in,
     Phase 9A operator tooling only) additionally returns the gateway's
     own `model` echo from the response — recorded as metadata only;
     code and docs never claim a concrete upstream model when the
     response reports only a routing alias.
- `scripts/verify_model_pair.py` (operator tool, stdlib only):
  - Default mode (catalog): fetch + resolve by exact display-name
    equality; ambiguity, absence, or unavailable-only matches exit 2
    with near matches listed for review; `--primary-id/--fallback-id`
    confirm operator-supplied ids against the catalog. **Never
    performs inference.**
  - `--live-inference` mode: requires both ids resolved (catalog or
    explicit operator confirmation), refuses unavailable ids, then
    sends exactly ONE harmless prompt per model (max_tokens 16, strict
    timeout, no retry loops, never through the fallback layer);
    records pass/fail, reply length, keyword check, and the gateway
    `model` echo; prints no responses/credentials/headers; exit
    nonzero if any requested model fails.
- Fallback-behavior guarantees re-verified against the fake upstream
  (30 pre-existing fallback API tests all green): primary success ⇒
  single upstream call; retryable primary failure ⇒ at most ONE
  fallback attempt (timeout/5xx/malformed/empty bounded); honest
  structured failure with safe `attempts` codes when both fail;
  auth/`not_configured` never retried; response `model` labels always
  name the model that actually answered. **New test**:
  `test_concurrent_override_cannot_split_a_request_lifecycle` — a
  settings PATCH landing while a chat request is in flight cannot
  re-resolve or split that lifecycle (policy captured once up front,
  next request uses the new policy).
- Configuration state: deliberately unchanged
  (`JARVIS_MODEL=qwen3.8-27b`, fallback unset, no runtime override).
  Step-4 gating: the pair may be configured only after a successful
  operator-invoked live smoke for BOTH ids.
- Live smoke (operator-approved, two separate runs, never automatic):
  1. Run 1 + Run 2 — fallback `deepseek-v4-flash`: **PASS** (non-empty
     reply, strict keyword check, gateway `model` echo
     `deepseek-v4-flash`); it is the only model inference-tested so
     far. Primary `glm-5.3-flash`: **FAIL `rate_limited` (HTTP 429) in
     both runs** — a persistent gateway quota/permission condition on
     that id, not a transient flake to loop around.
  2. Per the no-retry rule each re-run is an explicit operator
     decision; there are no speculative requests to other
     identifiers, and "the model that answered" is recorded from the
     gateway echo only — no upstream-model claims beyond it.
  3. Consequence: **the pair is NOT configured.** Setting the pair
     (`JARVIS_MODEL=glm-5.3-flash`, `JARVIS_FALLBACK_MODEL=
     deepseek-v4-flash` via environment, never silently overwriting a
     runtime override) is documented in the README and deferred until
     the primary clears its smoke.
- Tests: `tests/test_verify_model_pair.py` (8 cases — catalog mode
  default, missing key fail-closed, display-name resolution, ambiguity
  refusal, explicit-id confirmation, live mode one-request-per-model +
  echo recording, nonzero exits on failure and on unresolved
  ambiguity-forbidden inference; ALL FAKE-UPSTREAM ONLY), 3 new
  `test_provider.py` cases (`include_model` contract incl. missing and
  non-string echo), 1 new `test_models_api.py` case
  (`display_name` pass-through only-when-present, bounded), 1 new
  fallback API case (no-split concurrency).

**Test baseline after Phase 9A (catalog + smoke stage):** 546
stdlib-discovered tests OK (479 executed + 67 skipped browser) and 67
browser E2E cases executed OK (Playwright Firefox), 32 renderer tests
OK, node-parsed JS, `git diff --check` clean. Live smoke evidence:
fallback `deepseek-v4-flash` inference-tested PASS (2 runs); primary
`glm-5.3-flash` blocked on gateway `rate_limited` — pair unconfigured
until the operator clears that blocker and both ids pass.

## Phase 9B — Independent AI reviewer & plan approval gate (COMPLETE, 2026-10-22)

Scope honored: one real separately-invoked reviewer integration (the
Claude Code CLI's documented restricted print mode), a strictly
validated structured review contract, a bounded lifecycle review gate
(approve/revise/reject/timeout/unavailable), persistent provenance,
honest fallback semantics, and Phase 9A's model configuration left
untouched. No live inference occurred during implementation or the
automated suites: every adapter/CLI test drives a temporary fake
executable, every pipeline test uses a DI fake reviewer against the
fake upstream.

- **Integration selected**: `claude` CLI v2.1.302 (installed and
  authenticated as the operator's tooling). Its help documents exactly
  what the phase requires: `-p --output-format json` (single bounded
  non-interactive result), `--restricted` (removes Bash/PowerShell and
  all command-running/code tools + WebFetch, confines file tools,
  refuses bypassPermissions, ignores user/project settings),
  `--strict-mcp-config` (no MCP servers), `--permission-prompts none`
  (anything that would prompt is auto-denied), `--no-session-
  persistence`, and `--disallowedTools` (every known built-in tool
  name additionally denied). OpenCode's `opencode run` exposes no
  equivalent restriction surface and is this session's own host CLI —
  invoking it recursively was excluded by design.
- **Adapter** (`reviewer.ClaudeReviewer`, env-gated
  `JARVIS_REVIEWER=claude-code`, opt-in only): bounded review prompt
  (objective, strict plan JSON, registry manifest with declared
  capabilities, deterministic local-validation result) travels on
  stdin — never argv, never logged; the subprocess environment strips
  every `JARVIS_*`/`FREELLMAPI_*` key so no credential reaches the
  reviewer. Invocation guardrails proven in tests by inspecting the
  argv composition and by running the whole adapter against scripted
  fake CLIs.
- **Strict response contract** (`reviewer.validate_review_blob`):
  exactly `verdict` (`approved|revise|rejected`), `risk_level`
  (`low|medium|high`), `findings`, `missing_criteria`,
  `recommended_changes` (each ≤ 10 items × 300 chars). Unknown extra
  keys, unknown verdicts, invalid risk values, oversized/non-string
  items ⇒ `review_failed` (`review_unexpected_fields`,
  `review_invalid_verdict`, `review_invalid_risk`,
  `review_invalid_lists`, `review_not_object`,
  `review_missing_fields`). Transport failures map to
  `reviewer_missing` (unavailable), `reviewer_timeout`,
  `reviewer_cli_error` (exit≠0 — stderr never copied),
  `reviewer_output_oversized` (> 60 KB), `reviewer_bad_envelope`,
  `review_input_oversized`. Prose such as "everything looks safe" can
  never become an approval — it fails `invalid_review`.
- **Lifecycle gate** (`worker._review_gate`), state machine unchanged
  (review happens inside `planning`; missing/failed/unavailable
  reviews continue ONLY under the recorded local-validation-only
  policy for the shipped read-only capability class):
  1. approved ⇒ `review_source=external_review`, identity recorded
     (the CLI's own `modelUsage` id when present, else the executable
     identity).
  2. rejected ⇒ block `review_rejected` immediately, with the review
     record persisted in `plan_json` so the console shows the verdict
     and findings.
  3. revise ⇒ ONE bounded revision (planner call carrying the
     reviewer's concrete feedback) → revalidate → re-run the local
     check → final re-review; a still-revise verdict blocks honestly.
     MAX_REVIEW_ROUNDS = 2 — no planner–reviewer loops.
  4. failed/unavailable ⇒ `review_failed`/`review_unavailable` events
     + provenance in plan (`local_validation` source, identity stays
     empty); recovery paths cannot upgrade these to approvals.
  5. Cancellation honored right after the review rounds return
     (browser/API test flips `cancel_requested` mid-review and the
     goal ends `cancelled`, not executed).
- **Persistence**: no schema change — the bounded provenance block
  (status, verdict, identity, risk advisory, clamped findings /
  missing criteria / recommended changes, `reviewed_at`, round,
  `plan_revised`, `local_validation` flag, failure reason) rides in
  the existing `plan_json`; success reports add `review`, +
  `review_identity`, `review_rounds`, `plan_revised`. Restart test:
  a review-rejected goal stays blocked with the byte-identical review
  record after `recover()`; retry replans from scratch with fresh
  provenance.
- **UI** (audit row 9.40): Goal Console detail renders local
  validation, independent-review status, reviewer identity,
  verdict, bounded findings/missing criteria, revision flag and round
  — all `textContent` DOM rows. No "Approved by …" badge exists; the
  identity row appears only when an invoked reviewer produced a valid
  approval. Browser tests prove reviewer-embedded markup
  (`<img onerror>`) stays inert data.
- **Tests**: `tests/test_reviewer_cli.py` (15 — adapter contract vs
  fake executables: approved/rejected/revise parsing + identity,
  fences, missing executable ⇒ unavailable, timeout, nonzero exit
  with stderr-leak check, 9 invalid-shape subcases, oversized output,
  is_error envelope, oversized prompt, argv restricted-mode
  composition, child-env credential stripping,
  prompt-on-stdin); `test_goals_api.py` +6 (revise→approve provenance,
  revise-forever budget block, rejection provenance, reviewer
  exception ⇒ failed-not-approval with canary-text leak check,
  cancel-during-review, restart persistence/retry freshness);
  `test_browser_goals.py` +2 (rejected provenance rendering incl.
  XSS-inertness; approved identity row, no badge).
- **Runtime verification (one operator-approved real invocation,
  2026-10-22)**: the restricted invoke of `claude` v2.1.302 completed
  the transport path but the CLI reported
  `is_error: true / "Not logged in · Please run /login"` (exit 1) —
  the CLI is **installed but unauthenticated** in this environment.
  The adapter recorded it as `failed`/`reviewer_cli_error` with zero
  error-text leakage, exactly the honest degradation the phase
  requires; the goal-record pathway was proven end-to-end via the
  fake-CLI suite and DI pipelines. Unblock step (operator): one-time
  CLI login (`claude /login` or `ANTHROPIC_API_KEY`) and
  `JARVIS_REVIEWER=claude-code`; until then every goal keeps the
  truthful `review_unavailable`/local-validation-only records.
- Configuration unchanged: `JARVIS_REVIEWER` unset by default —
  goals keep the honest local-only record until the operator opts
  in. Phase 9A's model pair configuration stays untouched.

**Test baseline after Phase 9B:** 569 stdlib-discovered tests OK (500
executed + 69 skipped browser) and 69 browser E2E cases executed OK
(Playwright Firefox; 67 prior + 2 review provenance), 32 renderer
tests OK, node-parsed JS, `git diff --check` clean. No staging or
commits.

## Phase 9C — Real brain, reviewer & orchestrator integration
(OBSERVATION-STAGE COMPLETE, 2026-10-22)

Scope honored: integration correctness and evidence only — no new
capabilities, no dashboard redesign. Both live-verification gates were
explicitly operator-authorized for this session and executed once each
(no auto-retries, no speculative probing). Neither gate closed, so the
working configuration was preserved and the end-to-end integration was
proven with the disclosed fake layer, per the phase's own rule.

**Evidence classifications (unchanged by this phase):**

| Class | State after Phase 9C | Evidence |
|---|---|---|
| Catalog-listed models | static: `glm-5.3-flash` listed; `deepseek-v4-flash` listed + echo verified | Phase 9A |
| Passed live inference | `deepseek-v4-flash` only | 2026-10-22 PASS (3rd consecutive pass), one bounded request, no retry |
| Configured primary | `qwen3.8-27b` only | `glm-5.3-flash` still HTTP 429 `rate_limited` (3rd session) — activation gate unmet, pair refusal preserved |
| Configured fallback | *(none persisted)* | fallback verified but primary not — gate requires BOTH |
| Installed & authenticated reviewer | installed only | 2026-10-22 restricted invocation re-answered "Not logged in · Please run /login" (exit 1) — `claude /login` still not performed |
| Actual independent review success | **none** | no real reviewer call has ever returned a structurally valid result |
| End-to-end goal execution | **fake-integration PASS only** | real server/DB/worker/gate/tool code ran one controlled goal end-to-end (below); model gateway and reviewer were disclosed test fakes |
| Persisted runtime overrides | none present | nothing to respect or overwrite |

**End-to-end controlled run** (operator-observed; temp workspace + temp
DB, real `server.create_server`, real async goal worker, real
`workspace.list_files`): submit `202/queued` → async `queued→planning`
→ structured plan through the fake gateway (disclosed) → local safety
check → scripted fake reviewer gate approved (1 call; disclosed) →
`ready→executing→verifying→succeeded` → `workspace.list_files` returned
the real temp-workspace listing (5 entries, `src/app.py`,
`docs/readme.md`, `notes.txt`, correct count, sensitive exclusions
listed) → step `done` with verifier text → truthful report (`external
_review`, identity, review provenance in plan) → API detail verified
(steps/plan/report/events, upstream key absent from all responses) →
server stop + fresh restart on the same DB → goal still `succeeded`,
still 1 step, plan/report byte-consistent, 7 goal events intact, no
duplicate execution — server healthy for a new submit afterwards.
Audit log showed only sanitized events (no prompt/key content).

**Regression baseline after Phase 9C:** 569 stdlib OK (500 executed +
69 skipped browser), 69 browser E2E OK, 32 renderer OK, all `py_compile`
clean, `git diff --check` clean. It covers: truthful model selection +
fallback codes, one chat request ↔ one effective policy, reviewer
rejection never executes, reviewer failure never becomes approval,
read-only goal completes under its policy, restart recovery never
repeats completed steps, chat/conversations/tasks/search/telemetry/
notifications/preferences suites all OK, and logging/API leak checks
stay clean. Phase 9C additionally reconverenced the existing E2E script
floor (steps use `done`, plan block surfaces as `plan`, goal list has
no `total` field — test-contract alignment, no product change).

## Phase 9B-R — Reviewer backend swap: OpenCode CLI (COMPLETE, 2026-10-22)

The Phase 9B Claude Code adapter was removed after the replacement
passed the full suites; the review architecture, lifecycle gate,
strict contract, provenance, and honest failure semantics are
unchanged (reused, not rebuilt).

- **Verified against the installed binary (`opencode --version` →
  1.19.0), not assumptions**: `opencode run --pure --format json -f
  <input> --dir <work> [-m provider/model]` is the fresh
  non-interactive channel; restriction is layered as (1) a per-run
  isolated temporary HOME whose only config is
  `{"permission": {"*": "deny"}, "mcp": {}, "share": "disabled"}` —
  `opencode debug config` under that HOME is proven in tests to
  resolve zero plugins/agents/skills/instructions and those deny
  values (the operator's global plugins and permissive defaults can
  never merge in); (2) the documented
  `OPENCODE_PERMISSION='{"*":"deny"}'` env override plus
  `OPENCODE_DISABLE_AUTOUPDATE/MODELS_FETCH`; (3) a strict child-env
  whitelist that strips all `JARVIS_*`/`FREELLMAPI_*` credentials and
  inherited `OPENCODE_*` overrides; (4) no `--continue/--session/
  --fork/--attach`, no sharing, no `--auto` bypass; (5) the review
  prompt as a 0600 temp file attached with `-f` (never argv), the
  subprocess cwd in an empty bare work dir, everything deleted in
  `finally`; (6) auth reachable ONLY via a symlink of the operator's
  `auth.json` — never copied or printed.
- **Verified offline that the isolation holds**: the CLI resolves
  creds from the symlinked store (OpenCode Zen + OpenRouter entries
  visible without reading the file), the faux-HOME config
  resolution, and no plugin/agent leakage.
- **NDJSON parsing**: tolerant event-stream parser; only final
  assistant TEXT events are review candidates; status/tool events
  and zero exit codes are never approval — the last text must pass
  the strict validator. Identity comes from the CLI's own reported
  model id (`opencode CLI (<model>)`) or plain `opencode CLI` —
  never invented.
- **Configuration**: `JARVIS_REVIEWER` now accepts "" (default, honest
  local-only) or `opencode` (anything else refused; the old
  `claude-code` value is gone). New optional `JARVIS_REVIEWER_MODEL`
  (provider/model) pins the reviewer model; unset uses the CLI's own
  default, and a broken default fails honestly.
- **Tests**: 20 new adapter cases (`tests/test_reviewer_opencode.py`
  argv/fresh-session/no-bypass/no-attach, isolated HOME resolution,
  env canary stripping, prompt-file-not-argv, tmp cleanup, identity,
  timeout/nonzero-exit/oversized/no-final-text/9 invalid shapes,
  secret-leak checks) replacing the 15 Claude cases; pipeline tests
  unchanged and green (the gate is adapter-agnostic); browser
  identity fixtures updated to opencode identities. Model routing,
  chat, conversations, tasks, search, telemetry, notifications,
  preferences suites untouched and green.
- **Live verification (operator-authorized, 2026-10-23)**: three
  bounded real invocations, all failing safely whenever anything was
  not exactly as designed:
  1. Without `JARVIS_REVIEWER_MODEL` the CLI default is its own
     free-tier gateway, which refuses non-interactive runs
     ("OpenCode's free tier can only be used from within OpenCode") →
     recorded honestly as `failed/reviewer_cli_error`;
  2. with a model pinned (`openrouter/deepseek/deepseek-v4-flash`,
     catalog-verified through the registered OpenRouter credential —
     confirmed via the `opencode models` listing, no speculative ids)
     the inference succeeded but the assumed event shape was wrong →
     `failed/reviewer_bad_envelope`;
  3. after capturing the REAL NDJSON schema (`step_start` /
     `text`{part.text} / `step_finish`) the parser was corrected and
     the final invocation completed the FULL adapter path: a strict
     contract response recorded `revise` with bounded findings that
     correctly identified a real plan gap (the plan omitted the
     entry-count reporting the objective required), source
     `external_review`, identity `opencode CLI` (the stream reports
     no model id; none is claimed).
  Runtime enforcement of the deny rules: the review ran with
  everything permission-denied and the reviewer worked purely from
  its attached input — no grant was needed or possible.

**Test baseline after 9B-R:** 574 discovered tests OK (505 executed +
69 skipped browser), 69 browser E2E OK, 32 renderer OK, all
`py_compile` OK, `git diff --check` clean. No staging or commits.

## Phase 9D — Real end-to-end orchestration verification (COMPLETE, 2026-10-23)

One operator-scope controlled goal ran the ENTIRE real pipeline live,
boundary-unchanged, under the phase's budget (single goal, one
planning generation, no repair loop, one reviewer round, no retries).

- **Configuration honoring**: production settings untouched — the
  shipped primary `qwen3.8-27b` (live, via the configured gateway)
  planned the goal; fallback stayed unset (never needed — the single
  planning generation returned a complete plan); reviewer configured
  test-level only (`JARVIS_REVIEWER=opencode`,
  `JARVIS_REVIEWER_MODEL=openrouter/deepseek/deepseek-v4-flash`,
  catalog-verified id; no GLM probing).
- **Stage A — real planning**: one structured plan from the real
  gateway; 5 acceptance criteria preserved the objective exactly
  (entry count / source & docs relative paths / exclusions+truncation
  reporting / verification evidence), only `workspace.list_files`
  registered, deterministic validation+local check approved.
- **Stage B/C — real independent OpenCode review round 1**:
  `approved` (source `external_review`, identity `opencode CLI` — the
  stream reports no model id, so none is claimed; log event
  `review_external: approved by opencode CLI`). No revision was
  forced; the bounded revision budget stayed unspent.
- **Stage D — real execution + verification**: `workspace.list_files`
  returned 12 entries (none absolute, all inside the temp workspace;
  `.env`, `key.pem`, `secret_token.txt`, `.git`, `.cache` and a
  depth-9 file all correctly absent; 16 documented exclusion rules
  active; no file contents leaked); `count == len(entries)`; the
  worker's independent verifier re-checked results and 5/5 criteria
  evidenced (`verifying->succeeded`).
- **Stage E — restart recovery**: clean shutdown + fresh process on
  the same DB → identical status/plan/review/report/steps/events,
  zero re-executed steps, zero fabricated recovery events, goal list
  healthy for new work.
- **Failure categories** (§7): covered by the existing suites without
  extra live calls (planner unavailable/malformed + repair, reviewer
  missing/timeout/invalid/rejection, revision budget, tool/verification
  failure, cancellation, restart) — all green in the phase baseline.
- **Basline after 9D**: stdlib 574 OK (505 executed + 69 skipped);
  browser 69 OK (one transient error in the first full-run attempt,
  identical immediate re-run all green — no test changed); renderer
  32 OK; `py_compile` OK; `git diff --check` clean. No staging,
  no commits, no production DB touched.

**Remaining prerequisites (both operator-side):**
1. Model gate: clear the gateway's persistent `glm-5.3-flash` 429 and
   complete its one-request live smoke; the pair config then activates
   with `deepseek-v4-flash` as verified fallback.
2. Reviewer gate (updated Phase 9B-R: the Claude Code backend was
   replaced by the OpenCode CLI — no `claude /login` is needed): the
   OpenCode CLI v1.19.0 is installed and authenticated; with
   `JARVIS_REVIEWER=opencode` and
   `JARVIS_REVIEWER_MODEL=openrouter/deepseek/deepseek-v4-flash`
   (catalog-verified) the production reviewer works; a live run was
   re-proven in Phase 10A below.

**Next recommended phase:** with both gates closed, a Phase 9D
one-goal real-integration replay of the controlled run above (real
gateway planning + real restricted reviewer approval + execution) and
then the Phase 10 approval-class capability design (write tools with
review + authorization policy). If the 429 persists across sessions,
schedule the quota issue with the provider instead — it is
infrastructure, not code.

## Phase 10A — First bounded write capability with human approval (COMPLETE, 2026-10-23)

The orchestrator's first write tools, with the write path closed at
three independent gates: external review REQUIRED, then exact
digest-bound human approval per proposal, then hash-verified atomic
execution.

- **Permission class**: `tools.Tool.permission` gains
  `approval_required`; `reviewer.local_safety_check` now APPROVES such
  tools (it refuses unknown permission classes as before) and reports
  `needs_external_review` for them.
- **Review gate hardening** (`worker._review_gate`): when a plan
  contains any approval-class step, `failed`/`unavailable` reviews
  block the goal (`reviewer_unavailable`) — the read-only
  local-validation-only path stays limited to all-autonomous plans;
  no write ever runs without a real external approval, and no approval
  is ever fabricated by the reviewer or the worker.
- **Write tools** (`tools.py`): `workspace.create_file` (atomic,
  `O_CREAT|O_EXCL`, existing targets refused, parents must exist — no
  implicit mkdir) and `workspace.edit_file` (full-content replacement
  with `expected_before_sha256` precondition; mandatory pre-change
  backup under `<ws>/.jarvis/backup/`; temp file + atomic rename;
  dirfd + `O_NOFOLLOW` parent walking — symlink components are never
  followed). Content bound: ≤ 20k chars / 60k bytes per write.
  Refusals: sensitive-name patterns (stricter than the listing
  exclusions: credential/secret/key/env styles), the `.jarvis` meta
  directory, traversal/absolute paths, and — defense in depth — any
  write when the workspace root resolves inside JARVIS's own source
  tree (`self_write_forbidden`).
- **Digest-bound authorization** (`approvals.py` + `goal_approvals`
  table, schema v4): one proposal per approval-class step, digest =
  sha256(canonical JSON of goal_id, seq, tool, path, kind,
  before_sha256, content_sha256, plan_sha256). Approval API requires
  that digest, re-derives it from the PERSISTED rows (plan drift,
  args tampering → 403/409), and re-checks the CURRENT filesystem
  against the approved before-state. The worker re-verifies the digest
  again at claim time (`approved→claimed` conditional update — at most
  one execution per approval; `claimed→executed` records the
  resulting content hash).
- **Lifecycle**: goals gain `awaiting_approval`
  (`executing → awaiting_approval → executing/blocked/cancelled`);
  every pending write step must be approved before anything runs —
  per-operation approvals; a goal-level approval cannot authorize a
  later edit. Rejection resolves the goal `blocked:
  approval_withdrawn` (non-retryable — re-run requires a NEW goal).
  Restart safety: recovery never re-runs or upgrades an approval;
  an interrupted post-claim execution reports
  `reconciliation_required`; a claims race or tampered args refuse
  execution (`approval_claim_conflict`).
- **API surface**: `GET /api/goals/{id}/proposals` (state + hashes),
  `GET /api/goals/{id}/proposals/{seq}` (the exact preview: proposed
  content, current sha/state, bounded unified diff),
  `POST /api/goals/{id}/approve {seq, digest}` and
  `POST /api/goals/{id}/reject {seq}`.
- **Goal Console**: "Write approvals" panel — per-proposal state/hashes
  with the exact digest, a bounded preview (diff + proposed content,
  textContent-only rendering), Approve/Reject actions; status chips
  read `awaiting approval`.
- **Tests (+43 net new, 617 total stdlib)**: `tests/test_write_tools.py` (19: happy paths,
  conflict, backups restore-ability, before-mismatch, traversal,
  sensitive names, meta dir, symlink middle/final components, size
  bounds, schema validation), `tests/test_write_approvals.py` (12:
  full/partial flow, double approval, tampered digest, target races,
  reject resolution, preview endpoint, unavailable-reviewer blocking,
  read-only goals unharmed), `tests/test_write_lifecycle.py` (6:
  restart recovery never upgrades/reruns, interrupted-claim →
  reconciliation, tampered args refused at claim, TOU race,
  execute-at-most-once, mixed two-step plan).
- **Baseline after 10A**: stdlib 617 OK (73 skipped);
  browser 73/73 OK incl. the new 4-test approval E2E
  (`tests/test_browser_write_approvals.py`); renderer 32/32 OK;
  `node --check static/app.js` clean; `py_compile` OK;
  `git diff --check` clean. No staging, no commits, production DB
  never touched. Build-time live-request budget: none needed.
  **Live run (2026-10-23, same day, operator-approved):** one real
  goal (real `qwen3.8-27b` planning; steps `workspace.list_files` →
  `workspace.create_file demo/welcome.md`) proved the entire write
  path live: round-1 `external_review` approval by `opencode CLI`,
  `awaiting_approval` parking, human approval of the exact preview
  digest, resume to `succeeded` with the file byte-exact on disk
  (re-hashed sha == approved `after_sha256`), single-execution
  (`attempts=1`, approval `executed`), and restart-identical
  persistence. Evidence in `/tmp/jarvis-phase10a-2qy_lsrh/`.
- **Next** recommended phase: 10B capability growth (a bounded
  multi-step write class, e.g. `workspace.edit_file` verified live;
  deletion remains excluded) or the remaining Phase 8A model gate
  (glm-5.3-flash 429 smoke) — operator's call. No further 10A work
  is outstanding: build, tests, docs, and the live write run are
  complete.

## Phase 10B — Controlled verification: reads, recipes, and a bounded repair loop (COMPLETE, 2026-10-24)

Extends write capability into verified write capability: insight into
real files, honest test runs under the existing approval discipline,
and — when a check fails — one bounded, evidence-driven repair loop
instead of a fabricated success.

- **`workspace.read_file`** (autonomous, Phase 10B): one workspace
  text file per call; > 500 KB → structured `too_large` result; ≤ 20k
  chars returned with an honest `truncated` flag plus the sha256 and
  byte length of the FULL file; strict UTF-8 (binary →
  `binary_content` refusal). Reuses every read-path defense: dirfd +
  `O_NOFOLLOW` (symlink components refused), the write-sensitive-name
  refusals, `.jarvis` meta dir, self-root check. The verifier re-opens
  and re-hashes the CURRENT bytes; reads never modify the target
  (mtime/size/content asserted in tests).
- **Verification recipes** (no generic shell, ever): four registered
  tools in `verification.py` — `check_python_syntax` (ast.parse
  through a dirfd stdin fd; project code is not executed even for
  valid files), `check_javascript_syntax` (`node --check`, missing
  node → honest `unavailable`), `run_project_tests` (`python -m
  unittest -v <module>` — PERMISSION `approval_required`: runs project
  code, so the runner+target binding is digest-bound in
  `goal_approvals.kind = 'execute'`), and `inspect_git_diff`
  (whole-workspace `git diff` with `--no-textconv --no-ext-diff`,
  config-free child env (`GIT_CONFIG_GLOBAL/SYSTEM=/dev/null`), and
  sensitive-named diff sections dropped with an honest `truncated`
  flag). All recipes: argv-only spawning (`shell=False`), cwd =
  workspace root, child env strips everything but PATH (+ language +
  bytecode-off), 30 s timeout → honest `timed_out`, per-stream output
  clamped to 6000 chars, serialized one child per process, and a
  post-spawn re-hash of the target keeps the record honest against a
  swapped file (`workspace.<recipe>` tools carry `records_check`).
- **Honest semantics** (`recipe_semantics`): exit-0 passes as "the
  check says it passed"; failures are repair inputs; `unavailable` /
  `timed_out` mean "nothing was verified" → goal blocks
  (`verification_unavailable` / `verification_timed_out`) — no silent
  pass/fail fabrications.
- **Schema v5** (additive on v4): `goals.repair_count` (0..9) +
  `goals.last_failure_json` (≤ 4000, reasoning-evidence diagnostics) +
  `goal_approvals.kind` widened to `execute`; two rebuild migrations,
  one transaction, restart-preserved.
- **Bounded repair loop** (`worker.MAX_REPAIR_CYCLES = 2`): on a
  recipe failure the evidence is persisted (`last_failure_json`, ≤
  1200-char stdout/stderr tails), events `verification_failed` +
  `repair_started` fire, the goal transitions to `planning`, the next
  planner call gets a bounded (≤ 2200 chars) failure-context seed,
  the old proposals are superseded (`proposals_superseded`) and the
  replacement plan needs FRESH digest-bound approvals. Budget
  exhausted → goal blocked `repair_budget_exhausted`; killed recipes
  (printer/user story beyond the loop) never use the budget.
- **Console UI**: `execute tests` proposal chips + a verification
  metadata preview (runner, target, cwd, timeout, executes-project-
  code risk line, environment honesty, authorized-by digest), step
  evidence rendering (read previews incl. sha256; recipe diagnostics
  with status/exit/duration and bounded stdout/stderr), and repair
  honesty rows ("Repair cycles used N of 2" + the recorded previous
  failure) in the goal detail.
- **Tests (+60 net new, 677 total)**: `tests/test_read_file.py` (23:
  bounds, truncation, binary, symlink/escape/sensitive/side-effect-
  free verifier tampering), `tests/test_verify_recipes.py` (24: real
  subprocess pass/fail/import/crash cases, timeout kill, config-free
  env capture, git-diff sensitive-section filtering, env minimality),
  `tests/test_verify_integration.py` (10: read-only goals, execute
  preview metadata, failing → repair → success, budget exhaustion,
  `unavailable` blocking, invalid-plan refusal, restart-preserved
  evidence), `tests/test_browser_verify.py` (3: read evidence,
  execute preview honesty, blocked-after-repair UI). Existing 10A
  suites updated for the 8-tool registry + schema v5.
- **Baseline after 10B**: stdlib 677 OK (76 skipped — the 3 new
  browser tests added to the environment skips); browser 76/76 OK
  via the Playwright venv; renderer 32/32 OK; `node --check
  static/app.js` clean; `py_compile` OK on all changed files;
  `git diff --check` clean. No staging, no commits, production DB
  never touched. Build-time live-request budget: none needed.
- **Next** recommended phase: live operator-approved run of the
  verification + repair loop on a real buggy workspace (same
  discipline as the 10A live run), or Phase 8A's remaining model gate
  (glm-5.3-flash 429 smoke) — operator's call.

## Phase 11A — Configuration center & OrcaRouter integration (COMPLETE, 2026-10-25)

Eliminates the need to edit Python files to change a supported
setting: a real Settings Center in the UI plus OrcaRouter as a second
fixed provider profile. No live OrcaRouter request was made; the whole
phase is covered against fake upstreams and temp stores, and the
production provider/model is untouched.

- **Provider profiles** (`provider.py`): `PROVIDER_PROFILES` with the
  legacy `freellmapi` loopback gateway (default, unchanged) and
  `orcarouter` (`https://api.orcarouter.ai/v1`, OpenAI-compatible).
  Base URLs live server-side only — `normalize_base` allowlists the
  one remote host (default port, `/v1` paths only) and still refuses
  every other remote; no request payload can set a URL, redirect
  refusal / size limits / timeout / sanitized error taxonomy apply
  unchanged. `PROVIDER_DEFAULT_MODEL` gives `orcarouter/auto`
  (an identifier, not an availability promise).
- **Honest cost classification** (`provider.classify_pricing`,
  `fetch_pricing_catalog`, `fetch_free_package`): OrcaRouter's
  documented public pricing + free-package endpoints are fetched
  through fixed allowlisted URLs that **never receive the API key**;
  classification (`free_verified` | `paid` | `unknown`) comes only
  from catalog-sourced fields, never from the model id, and an
  insufficient/conflicting source stays `unknown`. Merged into the
  catalog entries (`pricing_class`) with bounded (3 MB) response
  limits, redirect refusal, and malformed-body rejection.
- **Persisted user configuration** (`settingsstore.py`): per-user
  directory outside the repo (`JARVIS_CONFIG_DIR` or
  `XDG_CONFIG_HOME`/`~/.config/jarvis`); `settings.json` +
  `credentials.json` written atomically (temp + fsync + `os.replace`)
  at 0600 under a 0700 dir. **Precedence everywhere:** explicit
  environment (`Config.env_managed`) > user store > defaults;
  environment-managed fields are locked (`409 field_locked`) and
  shown as locked in the UI. Keys are write-only: validated (≤ 512
  chars, no control chars), savable, clearable, never readable —
  no read API, log, error, HTML, localStorage, SQLite, or tracked file
  ever holds one; no custom encryption (filesystem permissions are the
  documented at-rest protection; no OS keyring integration).
- **Settings HTTP surface** (`server.py /api/settings*`, all
  same-origin-gated, method-checked, body-size-capped at 4 KiB, and
  rate-limited): `GET /api/settings` (complete secret-free view incl.
  per-field lock flags and key source), `PATCH /api/settings`
  (validated runtime bounds: timeout 1–300 s, max tokens 1–4096,
  history 1–100, reviewer `""|"opencode"`, reviewer model; partial
  updates preserve unrelated settings; unsupported fields → 400),
  `POST/DELETE /api/settings/credentials` (write-only key save/clear;
  env-managed keys refuse), `POST /api/settings/test-connection`
  (credential + catalog fetch only — never inference), `GET
  /api/settings/catalog` (bounded 60 s per-provider cache, forced
  refresh, pricing annotations + summary), `POST /api/settings/apply`
  (the only activation path: credential gate, catalog availability
  validation, explicit `confirm_cost` for OrcaRouter `paid`/`unknown`
  models → 409 `cost_confirmation_required`; failure leaves the
  working setup untouched), `POST /api/settings/model-test` (ONE
  bounded inference: harmless fixed prompt, `max_tokens: 16`,
  timeout ≤ 30 s, no retries, explicit click + cost confirmation
  required, result = ok/latency/gateway `model` echo metadata only).
  Rate limits: settings 30/60 s, probes 10/60 s, model-test 6/60 s,
  model policy 30/60 s.
- **Snapshot binding:** each chat/goal request resolves one effective
  `Config` snapshot up front — a concurrent settings change can never
  split an in-flight request across model policies; runtime overrides
  keep their Phase 7A semantics and the Phase 8A fallback rules are
  unchanged (auth/config failures never trigger fallback).
- **Settings Center UI** (`static/index.html` `#modal-settings` +
  `static/app.js`): three tabs — Provider (profiles with read-only
  base URLs, write-only masked key inputs, Save/Clear/Test-connection
  with catalog-only results incl. cost-class counts), Model routing
  (provider select, model + fallback fields with catalog-sourced
  datalist suggestions, per-model cost-class chips
  (`free (catalog-verified)` / `paid — may incur cost` / `cost
  unknown`), honest "not in the fetched catalog" state, explicit
  confirmation checkbox focused on any 409), Application (timeout /
  max tokens / history / reviewer with environment-locked fields
  disabled and named). Catalog fetches happen only on explicit
  tab/provider actions (never on chat or dashboard polling); existing
  styling tokens reused; no full dashboard redesign (11B territory).
- **Tests**: `tests/test_settings_store.py` (store validation,
  precedence, atomic writes, corruption recovery, write-only keys),
  `tests/test_settings_api.py` (endpoint contracts, env locks,
  partial-update preservation, no-secret surface), `tests/
  test_orca_catalog.py` (pricing classification, public-fetch
  credential absence, allowlist enforcement, malformed bodies),
  plus extended `test_models_api`/`test_fallback_api`/`test_chat_api`
  coverage and `tests/test_browser_settings.py` (9 browser cases:
  secret-free UI, write-only keys, apply→chat provider switch with
  confirmation gate, honest failures, restart persistence, cost hints).
- **Baseline after 11A**: stdlib 730 OK (85 skipped browser-env
  cases); browser 85/85 OK via the Playwright venv; renderer 32/32
  OK; `node --check static/app.js` clean; `py_compile` OK on all
  changed files; `git diff --check` clean; production DB untouched;
  no staging/commits. **Honest status:** no OrcaRouter credential is
  configured yet, and no GLM/DeepSeek id is claimed activated or
  verified on OrcaRouter — the operator must save + test a key and
  explicitly Apply a model policy; the active policy remains the
  legacy FreeLLMAPI configuration.
- **Next** recommended phase: operator live-run of the Settings
  Center (save OrcaRouter key → test connection → inspect the
  classified catalog → explicit model test → apply), then Phase 11B
  dashboard work.

---

## Phase 11B — Operator Workspace redesign (2026-10-25, this session)

- **Goal:** replace the "Command Center" dashboard shell with a calm,
  professional, low-fatigue dark operator workspace (Codex/CLI-tool
  inspired) while keeping the JARVIS circular-orb identity and every
  working flow (chat, tasks, durable goals, approvals, verification,
  search, notifications, preferences, Settings Center).
- **Shell** (`static/index.html` + `static/styles.css` rewritten;
  `static/app.js` surgery):
  - Sidebar (~224px): brand + live connection dot, nav Home / Goals /
    Conversations (live persisted-count badge) / Projects / Knowledge
    group (Obsidian Vault, Knowledge Graph) / Tools / Settings, bottom
    Diagnostics + Focus Mode. Goals/Conversations/Settings open the
    existing modals (flows preserved); the rest are real views.
  - Topbar: live status badge, model-policy chip (configuration, never
    "verified inference"), `/`-shortcut search, density popover,
    notifications, settings.
  - Dock composer pinned under the work surface (`#chat-form`) — same
    pipeline as the chat modal.
- **Home (`#view-command`):** CSS-only circular orb (rings + 3 electrons,
  slow animations, `prefers-reduced-motion` respected, paused via
  `.is-doc-hidden` when the tab is hidden). `updateOrbState()` derives
  idle/thinking/planning/executing/awaiting_approval/verifying/error from
  **real state only**: `state.chatPending` + `GET /api/goals` statuses.
  Right column: active goal, recent goals, current activity (all fed by
  the goals poll), and the real saved-task timeline (Today/Tomorrow).
- **Removed (fabricated):** Intelligence feed, AI-Core overview rows,
  agents roster, provider tiles, clock, operator profile, voice
  card/waveform, memory-map + orbital-network canvas/SVG demos
  (`mulberry32` deleted), quick-action tiles, `#nav-task-badge`.
- **Honest views:** Projects (facts from live status/conversations/
  goals/tasks counts), Obsidian Vault + Knowledge Graph (explicit "not
  connected" setup states — no backend exists), Tools (static capability
  facts; no `/api/tools` endpoint exists), Diagnostics (real gauges +
  monitor chip + application status + Executive Briefing).
- **`mockReply` (scripted fallback) kept** — required when no provider is
  configured; copy updated to the workspace reality. Briefing now lists
  goals and marks itself as compiled-from-live-data, never AI-generated.
- **Tests:** obsolete static assertions rewritten honestly (overview-row,
  badge, demo-surface tests → removal/diagnostics assertions); browser
  task-badge test → `test_created_task_persists_in_timeline` (timeline
  counts); goals/models/verify/write-approvals browser helpers open their
  consoles via the same `openGoalsModal()`/`openModelsModal()` globals the
  UI uses (the old `#llm-goals`/`#llm-manage` tiles no longer exist);
  `browser_harness.wait_ready()` now waits for the rendered timeline
  (clock removed).
- **Baseline after 11B:** stdlib 730 OK (85 skipped); renderer 32/32;
  browser 85/85 OK (dashboard 9, chat 14, tasks+search 14, goals 12,
  models 15, settings 9, security 5, verify 3, write-approvals 4);
  `node --check` clean; `py_compile` clean; production DB untouched;
  nothing staged or committed. Screenshots captured at 1440×900,
  1280×800, 1024, 768, 390 (Playwright Firefox, fake upstream).
- **Honest limits:** voice still unimplemented (no mic button anywhere —
  Phase 11C candidate); Obsidian/Knowledge-Graph/Projects data models do
  not exist yet; the orb is presentation-only state, not telemetry.

## Phase 13A — Native Obsidian knowledge integration (DONE 2026-10-26)

Read-only Obsidian vault integration delivered inside the existing
architecture: administrative `vault_path` configuration (Settings
Center "Knowledge" tab / `JARVIS_VAULT_PATH`, validated + resolved,
env-locked, honest unavailable/preserved states), three registered
read-only tools (`vault.list_files`, `vault.read_file`, `vault.search`)
bound by the strict registry and verifier machinery, an incremental
FTS5 index in the existing database (schema v8, additive migration,
explicit refresh, stale-hit flagging, previous-index preservation on
failure), citation provenance (relative path + sha256 + bounded
snippet), and dynamic-goal integration where retrieved notes feed
bounded observations without ever weakening permissions, approvals, or
the success contract (hostile-note injection tested; evidence matching
now values-only). Full suites green: 820 backend OK / 89 skipped,
renderer 32, browser 89 (incl. 3 new vault E2E). See
`docs/PHASE_13A_OBSIDIAN_INTEGRATION_REPORT.md`.

**Deferred to Phase 13B / 11C scope:** Vault note-browser view in the
dashboard, Knowledge Graph visualization (edges from stored wikilink/
Markdown-link data — no re-read needed), structured frontmatter/tag
parsing, and evaluation of semantic (embedding) retrieval against this
lexical baseline. Not started: any note writing, voice, vector stores.

## Phase 13B — Safe voice conversation (DONE 2026-10-26)

Push-to-talk voice input + opt-in speech output delivered with browser
Web Speech APIs only: mic button, language select (EN/FR/AR, no
reliability claims), and truthful status lines in BOTH composers;
privacy disclosure before first recognition (external-service caveat,
persisted ack); transcripts land editable in the existing composer and
submit only via the existing chat flow (single-flight, duplicate-result
guard); honest unsupported (real Firefox tested), permission-denied,
network, and no-speech states; stop on click/end/error/modal-close/
pagehide. Speech output (speechSynthesis, default OFF, persisted)
speaks only the completed assistant reply, doubles as Stop Speaking via
cancel, never blocks the composer, and fails honestly. No audio ever
reaches JARVIS servers; no bypass of goals/approvals (tested). Suites:
backend 838 OK / 107 skipped, renderer 32, browser 110 (18 new voice
E2E). See `docs/PHASE_13B_VOICE_REPORT.md`.

**Not implemented (by scope):** wake-word/always-on listening, voice-
triggered goals/tools, cloud or local speech models, multi-utterance
dictation.
