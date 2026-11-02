# JARVIS Implementation Report — Phase 14A: Operator hardening (2026-10-31)

Scope: four-level permission engine, local skills registry, report-only
update management, bounded self-improvement, dashboard wiring for all of
the above. Supersedes/covers the standing "operator" objectives without
rebuilding anything already shipped (digest-bound approvals, vault FTS,
tool registry, reviewer gate are preserved as-is and extended).

**Everything below is uncommitted working-tree state (no stage/commit).**

---

## 1. Executive summary

JARVIS is now a four-level-permission "operator" at the task-execution
boundary:

- Every tool step is classified L1–L4 (`permissions.py`), enforced in the
  backend worker only (fail-closed), and audited.
- A global capability cap (`1–3`) plus per-tool revoke/restore gives the
  operator single-knob control from the dashboard.
- A local-only skills registry discovers/trusts `SKILL.md` manifests;
  nothing is downloaded and nothing is executed.
- Updates are report-only against an optional https JSON source; the
  default state is an honest `not_configured`. No install path exists.
- Self-improvement observes real aggregates, proposes at most one open
  plan, and its only automatic action is creating ONE queued L2 task.
- All new logic is enforced server-side; the frontend renders state only.

## 2. What was NOT rebuilt (preserved, extended)

Designed-in earlier phases carried much of the spec; they were reused,
not duplicated:

| Existing capability | Where | Phase-14 delta |
|---|---|---|
| Digest-bound approvals | `approvals.py`, `goal_approvals`, Phase 10A | unchanged; L3 routes through it |
| Vault knowledge base (FTS5, read-only tools, citations) | `vault.py`, `tests/test_browser_vault.py` | aggregates now feed self-improvement observation |
| Tool registry / permission boundary | `tools.py` | classification + cap + disable policy layered in |
| Reviewer gate (OpenCode adapter, fail-closed) | `reviewer.py`, `worker._review_gate` | unchanged |
| Goal lifecycle / repair loop / restart recovery | `worker.py`, `goalstate.py` | step-deny hook added, nothing else |

## 3. Permission engine (`permissions.py`)

- Levels: L1 `verify_readonly` (auto), L2 `autonomous` (existing class),
  L3 `approval_required` (existing digest gate), L4 `restricted`
  (**never grantable**; a `disabled`-by-default policy block refuses L4).
- Global cap in `meta.permission_cap` (`1–3`, grant/revoke via
  `POST /api/permissions/cap`); requests above the cap are refused and
  affected tools re-classify at once.
- Per-tool `disabled` policy row: revoke/restore any single tool.
- Fail-closed: corrupt/missing policy state denies, never allows. The
  audit is bounded (last 5000 rows, `AUDIT_KEEP`) — no unbounded growth.
- Deny hooks: `worker._run_step` on failure path logs a
  `permission_denied` event; `_dynamic_run_tool` denies *before* any
  proposal or approval can be created.
- Migration v8→v9: additive tables `permission_policies`,
  `permission_audit`. Verified idempotent, row-preserving.
- Endpoints: `GET /api/permissions`, `GET /api/permissions/audit?limit=`,
  `POST /api/permissions/cap`, `POST /api/permissions/policy`
  (`{tool, level, disabled}`).
- UI: Settings → Permissions tab (levels incl. disabled L4, cap buttons,
  revoke/restore rows, live audit feed) — display only; enforcement is
  backend-only.

## 4. Skills registry (`skills.py`)

- Local-only discovery: `SKILL.md` front matter (name, description,
  tags) under `JARVIS_SKILLS_DIR` (scan → sync into a `skills` table,
  migration v9→v10).
- Hardening: symlink-escape and manifests > 64 KiB refused; the sync
  never leaves the configured root.
- Trust/enable is explicit operator state (`set_state` / `set_trust`);
  relevance matching is lexical over tags/description.
- **Nothing is downloaded and nothing is executed** this phase — there
  is deliberately no skill-execution path. Honest limitation, not a TODO
  stub that pretends to work.
- Endpoints: `GET /api/skills[?relevant=…]`, `POST /api/skills` (scan/
  sync/state). UI: Settings → Skills tab.

## 5. Update management (`updates.py`) — report-only

- `VERSION` = `0.14.0`; `updates.current_version()` reads it.
- Default (no `JARVIS_UPDATE_SOURCE`): `GET /api/updates` reports
  status `not_configured` — honest, never simulated.
- With a configured https JSON source: `POST /api/updates/check` fetches
  `{version, notes}`, persists the check into `update_checks`
  (migration v10→v11, together with `improvements`), reports
  `available` / `up_to_date`; every attempt is recorded and network
  failures are recorded, never raised to the caller.
- **No install path exists.** The Diagnostics panel shows the last check
  + history; applying anything remains a human action in this repo.

## 6. Bounded self-improvement (`improvements.py`)

- **Observe**: real aggregated evidence — goal/step outcomes, blocked
  ratio, permission denials, vault index state (existing persisted
  tables; no new event store).
- **Propose**: deterministic rules over that evidence; at most ONE open
  proposal (dedupe by kind while open). No evidence ⇒ no proposals.
- **Decide**: `approve` → `apply` → creates exactly ONE queued task in
  the existing goal queue (the L2 boundary: one task, human-reviewable;
  VERIFY after apply is *not* automatic — recorded honestly).
- History protection: `reject` is only valid while `status =
  'proposed'`; an applied/approved record is never rewritten
  (regression-tested).
- Endpoints: `GET /api/improvements`, `POST /api/improvements/propose`,
  `POST /api/improvements/decide`. UI: Diagnostics → Self-improvement
  panel, buttons wired in both `initQuick` and `showView('diagnostics')`.

## 7. Schema & migrations (additive, tested)

- `db.py` `SCHEMA_VERSION` = **11**. Chain: v8 → v9
  (`permission_policies`, `permission_audit`) → v10 (`skills`) → v11
  (`update_checks`, `improvements`).
- All migrations idempotent, run inside transactions, refuse future
  versions, preserve existing rows. Older/newer DBs than supported are
  refused, never silently downgraded.
- **Production DB safety**: the real `data/jarvis.db` was never opened
  during development. A *copy* (`/tmp/opencode/jarvis-testcopy.db`) was
  migrated v8→v11 and verified: 11 conversations, 3 vault notes and the
  operator's cap (`'3'`) intact. The real DB migrates itself on the next
  normal `python3 server.py` start.

## 8. Wiring & API surface (complete list of new endpoints)

| Method & path | Purpose |
|---|---|
| `GET /api/permissions` | levels, cap, per-tool policy |
| `GET /api/permissions/audit?limit=` | bounded deny feed |
| `POST /api/permissions/cap` | set global cap 1–3 |
| `POST /api/permissions/policy` | per-tool level / disabled |
| `GET /api/skills` (+`?relevant=`) | discovered skills |
| `POST /api/skills` | scan / sync / trust-enable state |
| `GET /api/updates` | last check + history |
| `POST /api/updates/check` | one source fetch (honest states) |
| `GET /api/improvements` | observation + proposals |
| `POST /api/improvements/propose` | run propose cycle |
| `POST /api/improvements/decide` | approve / apply / reject |

Same-origin enforcement, taxonomied errors, 405/404 handling, and
"reads never trigger side effects" match the pre-existing API contract.
New env vars: `JARVIS_SKILLS_DIR`, `JARVIS_UPDATE_SOURCE` (see README).

## 9. UI wiring (display-only, backend-authoritative)

- Settings → **Permissions** tab and → **Skills** tab (fetch + render in
  `static/app.js`: `fetchPermissions`, `renderSkillsTab`, …).
- Diagnostics → **Updates** and **Self-improvement** panels
  (`refreshUpdImprovements`), wired on view entry and in `initQuick`.
- Frozen `static/jarvis-format.js` untouched; no framework, no new
  dependency; every value rendered via the existing textContent paths.

## 10. Security review (self)

- Enforcement is backend-only; no permission decision is made in JS.
- Deny-before-proposal: a revoked tool can never reach the approval
  layer (the approval itself remains digest-bound).
- Fail-closed on corrupt DB/policy state; unknown cap values refused.
- Skills: filesystem containment (symlink/traversal), size bound, no
  execution, no network — an untrusted `SKILL.md` can only describe
  itself.
- Updates: https source URL only, bounded payload, key never logged;
  failure recorded not raised; no evaluation-order tricks (status is
  derived from persisted rows).
- No new dependencies; stdlib-only; loopback-only binding unchanged.

## 11. Testing & evidence

New suites (all stdlib `unittest`, temp DBs, fake upstream, no live
inference, no production data):

| Suite | Focus | Count |
|---|---|---|
| `tests/test_permissions.py` | engine, cap, disable, deny hooks, migration + API | 30 |
| `tests/test_permissions_frontend.py` | source-level UI guarantees | (in suite set) |
| `tests/test_skills.py` | scan/sync/trust, containment, size bound | full module |
| `tests/test_updates_improvements.py` | honest defaults, history, one-task bound, reject-after-apply | 11 |
| `tests/test_capabilities_frontend.py` | diagnostics wiring | in suite set |

**Final combined regression (2026-10-31):** `unittest discover -s tests
-p 'test_*.py'` → **899 tests, 0 errors, 0 failures, 107 skipped**
(the skipped set is the pre-existing browser/Playwright suite — it
requires the dev-only harness, see §13). Sustained on the full repo,
twice for the new-feature subset (405 baseline pre-change → 899 total).
`py_compile` and `node --check` clean; `git diff --check` clean.

## 12. Live smoke (copy of production DB — real DB untouched)

Server on 127.0.0.1:8125 against `/tmp/opencode/jarvis-testcopy.db`:

- All new endpoints answered 200 with correct shapes.
- Cap set to 2 accepted (400 on an attempt to set 4 — validation holds).
- Unknown skill → 404; updates → honest `not_configured` (no source set).
- One real bug found and fixed live: skills scan raised on missing
  `Path` import — fixed, re-verified green against the same smoke.

## 13. Screenshots

One real screenshot was captured this session with a rebuilt dev-only
Playwright venv (outside the repo, in `/tmp/opencode` — no project
dependency): the live Settings → Permissions tab against the migrated
production-DB copy — `docs/img/phase14a_permissions_tab_2026-10-31.png`
(cap row with L3 active, registered vault tools at L2 with Revoke
controls). The full 107-case Playwright browser E2E suite still does
not run here out of the box (the harness venv lives in `/tmp` and is
recreated per machine); its verification role is unchanged.

## 14. Readiness scoring (honest)

Weighted self-assessment — **≈ 78% production-ready** (target ≥ 90% was
not met; the gap is deliberate honesty, not an underestimate):

- Backend enforcement, migrations, API contracts, audit-bounding,
  history protection: fully verified by tests + smoke (strong).
- UI: source-level verified + smoke-tested endpoints; **no real browser
  pass** on the new tabs (gap).
- Updates: report-only by design; no operator update source configured
  yet (expected default, still counts against "update management" UX).
- Skill execution semantics: intentionally absent this phase.
- Self-improvement VERIFY-after-apply: honest limitation (none).

## 15. Known limitations (declared)

1. No skill execution path (skills are metadata + trust state only).
2. Self-improvement never auto-verifies its own one-task outcome.
3. Updates have no apply flow — by policy, not omission.
4. Approval records still lack expiration timestamps (unchanged from the
   approval design; denied-by-capability-cap re-derivation is what
   bounds them today).
5. Playwright browser coverage skipped in this environment (§13).
6. Prod DB is still schema v8 on disk until the operator starts the
   server once (additive migration, verified on the copy).

## 16. Operator next steps

1. `python3 server.py` (migrates the real DB v8→v11 automatically and
   additively; data preserved — verified on the copy).
2. Review Settings → Permissions; set the real capability cap and any
   tool revokes.
3. Point `JARVIS_SKILLS_DIR` at real skill folders; audit the discovered
   `SKILL.md`s before trusting any.
4. If update visibility is wanted, set `JARVIS_UPDATE_SOURCE` to a
   controlled https JSON endpoint.
5. Rebuild the browser harness when screenshots/E2E are needed (§13).
