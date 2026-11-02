# Phase 12D — Trustworthy Goals: Verified Completion, Durable Safety Budgets, Dynamic Mode in the Dashboard

Status: **delivered** (2026-10-26). Everything below was executed against the
real source and test suites; no live model calls, no spending, no production
database access, no files staged or committed.

Phase 12D closes the three gaps Phase 12C named for dynamic goals:

1. **A dynamic goal could succeed after merely executing verified-but-irrelevant
   tool calls.** Fix: a persisted, validated, evidence-checked **success
   contract**.
2. **The invalid-decision budget was counted from retention-bounded
   `goal_events`,** so event churn or a restart could undercount/reset it.
   Fix: a durable counter on the goal row itself.
3. **The API accepted `mode` but the dashboard could not select it.** Fix: a
   mode select in the existing Goal Console, sequential still the default.

---

## 1. The success contract (dynamic mode only)

### 1.1 Lifecycle

Exactly **one** model call happens at `queued → planning` (plus exactly one
bounded repair attempt). The model **proposes** the contract; the backend
deterministically validates it, safety-checks it, independently reviews it when
the existing policy requires, and persists it. The model can never approve its
own contract, can never edit it after validation, and can never declare the
work successful by assertion — success is decided only by `_phase_verify`
matching recorded evidence against the persisted criteria.

```
queued ──► planning:
             contract call (planner.contract_prompt)
             └─► planner.validate_contract  (deterministic, §1.2)
                 ├─ invalid ──► ONE repair attempt ──► invalid ──►
                 │                blocked: contract_unverifiable (retryable)
                 └─ valid ──► reviewer.local_safety_check (§1.3)
                                ├─ rejected ──► blocked: <safety reason>
                                ├─ needs external review ──► _contract_review (§1.4)
                                └─ approved ──► persist to plan_json ──► ready
ready ──► executing (bounded decision loop, contract criteria shown
                   in every decision prompt)
executing ──► verifying: _verify_dynamic_contract (§1.5)
              ├─ all criteria evidenced by recorded tool results ──► succeeded
              └─ any criterion lacks evidence ──► blocked: criteria_unverified
```

The contract is stored in `plan_json` as
`{"mode": "dynamic", "summary", "criteria", "evidence", "review_source"[, "review"]}`.
Because it lives on the goal row, it survives restart, `/retry`, and recovery;
`_phase_plan_dynamic` reuses a persisted dynamic contract instead of
re-negotiating, so the criteria that gate success are the criteria that were
validated before execution began, bound to this goal id.

### 1.2 Deterministic validation (`planner.validate_contract`)

Shape accepted (after JSON extraction / fence stripping):

```json
{"summary": "<=300 chars",
 "criteria": ["1..10 items, <=300 chars each"],
 "evidence": [{"criterion": <1-based index>, "tools": ["1..3 registered tool names"]}]}
```

Rejections (all deterministic, no model judgment involved):

- not a single JSON object; unknown top-level fields (a model `complete: true`
  smuggle is rejected here by construction);
- empty/bad `summary`; criteria not a list of 1..10 non-empty strings ≤300 chars;
- **trivial/generic criteria**: fewer than two significant tokens after removing
  generic completion words (`done, complete, success, finished, task, goal, …`
  — `_TRIVIAL_CRITERION_TOKENS`);
- **goal-unrelated criteria**: no significant-token overlap with the actual
  objective (the contract must be grounded in the user's request);
- **duplicate criteria** (identical significant-token sets);
- evidence not exactly one entry per criterion; criterion reference missing,
  out of range, duplicated, or a bool; tools not a list of 1..3 distinct
  strings; **unregistered evidence tool** (`unsupported_capability`).

Blocked contracts return `contract_unverifiable`, which is retryable (in
`goalstate.BLOCKED_CODES_REPORTED`): the honest resolution is clarification,
better evidence tools, or human verification — never a silently weakened
criterion.

### 1.3 Safety gate (existing reviewer interface reused)

`reviewer.local_safety_check` runs on a plan-shaped projection of the contract
(`{"risk": "low", "criteria": [...], "steps": [{"tool": t} for every evidence
tool]}`) — the evidence tools are exactly the surface this goal will run, so
the existing per-tool permission gate applies unchanged. Unknown tools and
non-autonomous permission classes are refused; `approval_required` tools mark
the contract as needing external review.

### 1.4 Independent review (`_contract_review`)

When the safety gate demands external review, the existing `reviewer.review()`
interface is called once. `approved → proceed` (provenance recorded:
`review_source: "external_review"` plus the review record in `plan_json`);
`rejected/revise → blocked: review_rejected`; reviewer failure/unavailability →
`blocked: reviewer_unavailable`. **No approval is ever fabricated** — the same
Phase 10A/10B invariant as sequential writes.

### 1.5 Evidence-gated success (`_verify_dynamic_contract`)

At `verifying`, every criterion must be satisfied by evidence that is:

1. **An actual recorded tool result** — a `done` step row of *this* goal
   (`goal_steps.result_json`), not a model assertion and not a fabricated
   artifact;
2. **Produced by a contract-declared evidence tool** for that criterion — a
   successful tool call from a tool the contract never named satisfies
   nothing;
3. **Deterministically matched**: ≥ `MIN_COVER_TOKENS` (2) significant tokens
   shared between the recorded `result_json` and the criterion text.

Missing, irrelevant, stale (not from this execution), or insufficient evidence
blocks with `criteria_unverified` naming which criteria failed; the goal does
**not** enter `succeeded`. A model `done` decision only ever ends the loop into
verification — it never grants success.

Sequential goals keep their established Phase 10 verify path untouched
(`mode != "dynamic"` branch); the `mode` column defaults to `sequential`
everywhere, so existing goals and flows are bit-for-bit unaffected except for
the additive schema column.

**Honest limitation (lexical evidence matching).** The evidence check is token
overlap, not semantic understanding. A tool result that shares ≥2 significant
tokens with a criterion can satisfy it even if the meaning differs
("note.txt" in a listing satisfies "note.txt is listed"). Conversely a
semantically perfect paraphrase with disjoint tokens would be refused. This is
documented, deterministic, and impossible for the model to argue with — the
right trade-off until a deeper result-level verifier exists.

---

## 2. Durable invalid-decision budget

### 2.1 Defect and fix

Phase 12C counted invalid decisions by `COUNT(*)` over `goal_events` for
`dynamic_decision_invalid`, capped at `MAX_GOAL_EVENTS = 100` per goal. A goal
with enough event churn evicted its own evidence; a restart could see a
different (smaller) count.

Fix: schema **v7** migration 6 adds

```sql
ALTER TABLE goals ADD COLUMN dynamic_invalid_count INTEGER
    NOT NULL DEFAULT 0 CHECK (dynamic_invalid_count BETWEEN 0 AND 9)
```

`_dynamic_decide` increments via a conditional
`db.update_goal_fields(... {"dynamic_invalid_count": count})` — if another
writer moved the goal first, the update fails and the decision is never
double-counted. The explanatory `dynamic_decision_invalid` event is still
recorded (audit trail), and `_dynamic_event_count` is kept but re-documented as
**audit-only**; enforcement reads exclusively from the durable goal-row
counter (`MAX_DYNAMIC_INVALID = 2`, a backend constant the model cannot
influence).

### 2.2 Properties

- **Event churn cannot reset it**: the counter is not derived from
  retention-bounded rows (regression test drives 150 churn events > cap 100).
- **Restart/retry cannot launder it**: the value lives on the goal row;
  `/retry` of a `decision_invalid`-blocked dynamic goal preserves and keeps
  incrementing it.
- **Ceiling is backend-enforced**: 0..9 CHECK plus `MAX_DYNAMIC_INVALID = 2`
  in code.
- Mode is deliberately **not** in the `update_goal_fields` allowed-columns set,
  so an attempted mode flip raises `KeyError` — mode is immutable after
  creation (regression-tested).

Migration is additive with a safe default; v6 databases upgrade transparently
on next open, per the existing migration conventions.

---

## 3. Dynamic mode in the existing dashboard

- `static/index.html`: an `Execution mode` `<select id="goal-mode">` in the
  existing goal form — **`sequential` selected by default**, `dynamic` opt-in
  ("bounded observe → decide loop with a verified success contract").
- `static/app.js` `submitGoal` sends `{objective, mode}` to the **existing**
  `POST /api/goals`; `renderGoals` shows a `chip chip-inline chip-live`
  "dynamic" badge on dynamic goals in the list; `renderGoalDetail` shows an
  honest "Execution mode" row in goal details. No new CSS (existing `field`
  select styling and chip classes); no separate dashboard; blocked/
  awaiting-approval states render through the existing chips/rows.
- Server-side: `server.py` `_handle_goals_create` validates
  `mode ∈ {"sequential","dynamic"}` (400 `invalid_request` otherwise — API
  validation was **not** weakened); list payload exposes `mode`, detail payload
  exposes `mode` and `dynamic_invalid_count`.
- Immutability enforced in the DB layer (`KeyError` on `mode` in
  `update_goal_fields`), so an existing goal can never silently switch mode
  from UI or API.

---

## 4. Files changed (all uncommitted, nothing staged)

| File | Change |
|---|---|
| `db.py` | `SCHEMA_VERSION = 7`; migration 6 (`dynamic_invalid_count`); column in `update_goal_fields` allow-list and `_goal_row_to_dict` (mode deliberately absent → immutable) |
| `planner.py` | `contract_prompt`, `validate_contract`, `_TRIVIAL_CRITERION_TOKENS`, `MAX_CONTRACT_EVIDENCE_TOOLS = 3`; `decision_prompt` now embeds the persisted criteria in every dynamic decision |
| `worker.py` | `_phase_plan_dynamic` rewrite (persisted-contract reuse), new `_build_contract`, `_contract_review`; `_dynamic_decide` durable counter; `_dynamic_event_count` → audit-only; `_phase_verify` dynamic branch → `_verify_dynamic_contract` |
| `goalstate.py` | `contract_unverifiable` added to `BLOCKED_CODES_REPORTED` |
| `server.py` | `mode` in goals list + detail, `dynamic_invalid_count` in detail; `_handle_goals_create` mode validation |
| `static/index.html` | `#goal-mode` select (sequential default) |
| `static/app.js` | `submitGoal` sends mode; `renderGoals` dynamic chip; `renderGoalDetail` "Execution mode" row |
| `tests/test_dynamic_loop.py` | harness `scripted(fn, contract=None|"scripted")` (auto-answers contract prompts via the `"contract planner for a JARVIS"` marker), `LazyReviewer`, expectations updated for persisted contracts — 22 tests |
| `tests/test_dynamic_contract.py` | **new** Phase 12D suite — 23 tests across all 13 mandatory areas (below) |
| `tests/test_goals_db.py`, `tests/test_db_migration.py` | counter column, CHECK bounds, migration 6, immutability — 26 tests |
| `tests/test_browser_goals.py` | new `test_dynamic_mode_select_flows_to_server_and_renders` (UI → API → worker → honest block + badges) — 13 tests |

Frozen files untouched: `static/jarvis-format.js` byte-identical (renderer
suite proves it); provider credentials/model config untouched; no runtime
dependencies added.

---

## 5. Test results (exact commands and counts)

New Phase 12D suites:

```
cd tests && PYTHONPATH=/home/mdx/Dev/JARVIS python3 -m unittest \
    test_dynamic_contract test_dynamic_loop
→ Ran 45 tests — OK (22 dynamic-loop + 23 dynamic-contract)

cd tests && PYTHONPATH=/home/mdx/Dev/JARVIS python3 -m unittest \
    test_goals_db test_db_migration
→ Ran 26 tests — OK
```

The 23 `test_dynamic_contract.py` tests map to the 13 mandatory areas:

1. success with nontrivial criteria + real evidence — `test_success_requires_contract_evidence`
2. tool succeeds technically but outcome not achieved — `test_tool_success_alone_does_not_achieve_outcome`
3. `done` claim with missing/irrelevant evidence — same two tests + `test_irrelevant_evidence_prevents_success`
4. empty/trivial/malformed/unrelated criteria — `ContractValidationTests` (8 tests) + `test_malformed_contract_blocks_honestly`
5. objective unverifiable with available tools — `test_objective_unverifiable_with_available_tools`
6. independent review + contract persistence — `test_contract_review_required_and_persisted`, `test_approved_contract_is_reviewed_and_persisted`
7. state/evidence recovery after restart — `test_contract_survives_restart_and_stays_bound`
8. budget survives event-retention churn — `test_budget_survives_event_retention_churn`
9. budget survives restart + retry — `test_budget_survives_restart_and_retry`, `test_first_invalid_after_churn_does_not_block_early`
10. dynamic mode via real API — `test_dynamic_mode_selected_through_api` (and the browser test below for UI)
11. sequential default — `test_default_mode_is_sequential_through_api` + browser default-select assertion
12. digest-bound write approval intact — `test_write_request_goes_through_digest_approval` / `test_rejected_write_request_blocks_without_executing` (dynamic-loop suite)
13. sequential/DB/verify/recovery intact — full existing suites below

Full backend suite (fake upstream; browser-environment tests skip here):

```
python3 -m unittest discover -s tests -p "test_*.py"
→ Ran 776 tests in 407 s — OK (skipped=86)
```

Renderer (frozen `jarvis-format.js` contract):

```
node tests/test_format_render.js
→ 32 format-render tests passed
```

Browser E2E (Playwright chromium, real server + worker + fake upstream):

| File | Result |
|---|---|
| `test_browser_goals` (incl. new dynamic-mode UI test) | Ran 13 — OK |
| `test_browser_write_approvals` | Ran 4 — OK |
| `test_browser_verify` | Ran 3 — OK |
| `test_browser_dashboard` | Ran 9 — OK |
| `test_browser_chat` | Ran 14 — OK |
| `test_browser_tasks` | Ran 7 — OK |
| `test_browser_models` | Ran 15 — OK |
| `test_browser_settings` | Ran 9 — OK |
| `test_browser_search` | Ran 7 — OK |
| `test_browser_security` | Ran 5 — OK |
| **browser total** | **86 — OK** |

(`test_blocked_goal_shows_reason_and_retry` timed out once in an earlier full
run, passed immediately in isolation and in every subsequent full-file run —
pre-existing poll-timing flake, not a regression; the test was not weakened.)

Hygiene: `git diff --check` clean; `git status --porcelain` byte-identical
(sorted) to the pre-phase baseline — all Phase 12D work lives in files that
were already modified/untracked before the phase; **0 staged files, 0
commits**; OpenJarvis reference checkout untouched (clean at `0583901`);
production `data/jarvis.db` never opened by tests (temp DBs only).

---

## 6. Unresolved limitations and remaining risks

1. **Lexical evidence matching** (§1.5): ≥2 shared significant tokens is a
   proxy for "this result demonstrates this criterion". Deterministic and
   model-proof, but not semantic. A deeper verifier (e.g. per-tool structured
   evidence extractors) is future work.
2. **Contract not re-negotiated**: if a dynamic goal blocks on
   `criteria_unverified`, `/retry` resumes with the same persisted contract
   (by design — criteria cannot drift), but the model cannot request better
   evidence tools mid-flight; the honest escape is cancel + re-create with a
   clarified objective.
3. **Verified-but-irrelevant steps before `done`** still block (correctly) via
   `criteria_unverified` rather than triggering re-planning; a future loop
   could treat contract failure as a bounded re-decision signal.
4. **Evidence freshness is execution-scoped**: evidence must come from this
   goal's steps, but there is no wall-clock staleness check (irrelevant for
   single-run goals today).
5. **Browser poll-timing flake** in one pre-existing goals test (documented
   above) — intermittently slow in packed full-suite runs, never in isolation.

Definition of done, verified: a dynamic goal cannot succeed merely because its
tool calls worked (criterion-level evidence gating); budget enforcement
outlives event retention, restarts, and retries (goal-row counter); and
dynamic mode is an explicit, server-validated opt-in in the existing dashboard
with sequential behavior unchanged as the default.
