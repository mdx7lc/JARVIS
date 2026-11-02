# Phase 12C — OpenJarvis Installation, Side-by-Side Comparison, Selective Integration, and Safe Evolution

**Date:** 2026-10-26
**JARVIS:** `/home/mdx/Dev/JARVIS` (branch `main`, HEAD `531ffcba`, all work uncommitted — preserved)
**OpenJarvis reference:** `/home/mdx/Dev/OpenJarvis-reference` (official
`https://github.com/open-jarvis/OpenJarvis.git`, branch `main`, commit
`05839014127772bd077ba9ba2a2393a007f63ac6`, Apache-2.0, working tree clean)
**Isolated state root:** `/home/mdx/Dev/.openjarvis-reference-state` (0700)
**Basis:** `docs/OPENJARVIS_INTEGRATION_ASSESSMENT.md` (Phase 12A) — its
recommendation (Option A: native bounded dynamic loop; no OpenJarvis
dependency) was confirmed by this phase's source and runtime evidence, not
revised.

---

## 1. Installation — commands, location, verification

### 1.1 What was done

Pre-flight (recorded before any change): JARVIS git status snapshot saved to
`/tmp/opencode/jarvis-git-baseline.txt` (26 entries, unchanged through this
phase); disk 46 GiB free; RAM 4.0 GiB available; Python 3.12.3; no `uv`, no
Rust toolchain; **no `~/.openjarvis` directory existed** (verified again at
the end — still does not exist).

Stage A — clone (nothing inside JARVIS):

```
cd /home/mdx/Dev && git clone https://github.com/open-jarvis/OpenJarvis.git OpenJarvis-reference
```

Verified: remote URL, branch `main`, commit `0583901`, Apache-2.0 `LICENSE`,
clean status.

Stage B — isolated install (dedicated venv, no optional extras, no Rust
group, no global installs, no PATH changes):

```
cd /home/mdx/Dev/OpenJarvis-reference
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install .            # no [extras]; openjarvis-rust NOT installed
.venv/bin/pip install pytest       # test-only, still inside the venv
```

Isolation mechanism (from `src/openjarvis/core/paths.py`):
`OPENJARVIS_HOME` overrides the default `~/.openjarvis` and is **passed
explicitly on every command**:

```
mkdir -p /home/mdx/Dev/.openjarvis-reference-state && chmod 700
OPENJARVIS_HOME=/home/mdx/Dev/.openjarvis-reference-state .venv/bin/jarvis --help
OPENJARVIS_HOME=/home/mdx/Dev/.openjarvis-reference-state .venv/bin/jarvis doctor
OPENJARVIS_HOME=/home/mdx/Dev/.openjarvis-reference-state .venv/bin/jarvis config show
```

### 1.2 Verification results (actual)

| Check | Result |
|---|---|
| `jarvis --help` | CLI loads; entry point is `openjarvis.cli:main` (installed as venv-local `jarvis`, **never on the global PATH**) |
| `jarvis doctor` (isolated home) | **6 passed, 24 warnings, 0 failures.** Local engines unreachable as expected (no Ollama installed/started, no models downloaded); one cloud catalog (NIM) answered an unauthenticated reachability probe — no inference, no keys, no quota |
| `jarvis config show` (isolated home) | Reads/would write `/home/mdx/Dev/.openjarvis-reference-state/config.toml` — no config file was created (none needed) |
| Default-home safety | `~/.openjarvis` still does not exist after every command; `~/.cargo` not created; no stray `~/.config/openjarvis` or `~/.local/share/openjarvis` |
| Background Rust build | `doctor` offered a background Rust build; verified **no maturin/cargo/rustc process was ever spawned** (none exists on the machine) |

### 1.3 Footprint

| Item | Size / note |
|---|---|
| venv (`.venv`, 44 wheel packages) | **516 MB** (numpy/pandas/pyarrow via the mandatory `datasets` dependency dominate) |
| Isolated state dir | **empty (4 KB)** — doctor/CLI inspection persisted nothing |
| Repo checkout | ~300 MB incl. `.git` (2,202 files) |
| Disk after | 44 GiB free (was 46) |
| **Not installed** | `openjarvis-rust` (PyO3 extension; no toolchain by design), faiss/sentence-transformers, vllm, mlx, litellm, torch, FastAPI server extras, Ollama + models, Docker, any GUI/Node stack |

### 1.4 Rust extension — the honest limitation

`src/openjarvis/_rust_bridge.py` hard-requires `openjarvis_rust` ("there is
no Python fallback") for the modules that use it: security scanners
(`security/{capabilities,file_policy,injection_scanner,ssrf,rate_limiter,scanner}.py`),
`agents/loop_guard.py` internals, and the `tools/storage/{bm25,sqlite}.py`
backends. No Rust toolchain may be installed under this phase's constraints,
so **all Rust-delegating runtime paths are UNVERIFIED at runtime**; they were
compared at source level only. This is reflected exactly in the test results
(§6) — nothing was claimed as verified that is not.

---

## 2. Side-by-side comparison matrix (source-grounded)

Legend — **Decision:** keep = JARVIS stays as-is; adapt = pattern adapted
(this phase: the dynamic loop, §4); separate = OpenJarvis capability stays in
the reference checkout only.

| # | Area | OpenJarvis (source) | JARVIS (source) | Comparison and decision |
|---|---|---|---|---|
| 1 | Inference | `engine/_base.py` `InferenceEngine`; 15+ engines (ollama, vllm, litellm, cloud, apple_fm); model router | `provider.py` `complete_with_fallback` (plain messages, ≤1 fallback, pinned snapshot, typed error taxonomy) | Different goals: OJ abstracts *local model serving*; JARVIS abstracts *one disciplined remote call*. JARVIS has no streaming and no native tool-calling — acceptable; the dynamic loop proves JSON-over-text suffices (OJ's own ReAct does the same). **Keep.** |
| 2 | Agent loop | `agents/native_react.py` (Thought/Action/Observation as user message, `max_turns`), `agents/orchestrator.py` (`_default_max_turns = 10`), `agents/loop_guard.py` (repeat/ping-pong detection, `compress_context`) | **was:** plan-then-execute only (`worker.py` `_run_steps`, observations never reached the model); **now:** `_phase_execute_dynamic` + `_dynamic_decide` (Phase 12B, §4) | The core gap, now closed natively. JARVIS's loop is stricter than OJ's: strict JSON decision grammar, args normalized through the tool schema, exact-repeat refusal, persisted turn/invalid budgets, `done` still gated by tool verification. **Adapt (done this phase).** |
| 3 | Tool registry | `core/registry.py` (decorator registries), `tools/*` (80+ tools incl. shell, browser, docker), `skills/tool_adapter.py` | `tools.py` `ToolRegistry`/`Tool` (schema validation, permission classes `autonomous`/`verify_readonly`/`approval_required`, per-tool `verify()`, `idempotent`, `records_check`) | OJ's breadth is exactly what JARVIS must NOT import (shell/browser/docker widen the attack surface). JARVIS's registry is the permission boundary and stays one. **Keep.** |
| 4 | Approvals | `tools/approval_store.py`: SQLite `PendingAction` + `PermissionRule` queue; `confirm_callback` threading | `approvals.py` + `db.goal_approvals`: digest-bound (goal+seq+tool+args+plan_sha), atomic `approved→claimed` conditional UPDATE, replay re-verification before execution | JARVIS's is strictly stronger (content-bound digests vs a stateful queue). The dynamic loop routes write requests through this exact flow (§4, tested). **Keep.** |
| 5 | Verification | No independent verifier; agents self-report `Final Answer` | `verification.py` recipes + per-tool `verify()` postconditions; `worker._phase_verify` criteria evidence; bounded repair cycles | OJ trusts the model's final answer; JARVIS does not (a `done` claim is refused without verified step evidence — tested). **Keep.** |
| 6 | Memory | `memory/store.py` JSONL FactStore + trust tiers; `memory/extractor.py` background extraction; SQLite/FTS5 connectors (`connectors/store.py`, `tools/storage/sqlite.py`) | none yet (Phase 11C+ Obsidian work) | Real gap, addressed via the **Obsidian roadmap (§7)**, not by importing OJ's store (deps: numpy/pandas/pyarrow tier). |
| 7 | Data storage | Many small stores: `traces/store.py` (SQLite+FTS5), `tools/approval_store.py`, `scheduler/store.py`, `connectors/store.py`, `config` telemetry.db + JSONL facts | one `jarvis.db` (schema **v6**): conversations, messages, tasks, goals, goal_steps, goal_events, goal_approvals; WAL; additive transactional migrations; size-CHECKed columns; bounded event retention | One source of truth vs seven. JARVIS keeps one DB; useful OJ *concepts* (FTS index, persistent scheduled-job store) noted in §7. **Keep.** |
| 8 | Configuration | `core/config.py` + `config.toml` under `OPENJARVIS_HOME`; env > file precedence; hardware-generated template (`jarvis init`) | `settingsstore.py` + `/api/settings*`: env > user store > defaults, env-locked fields, atomic 0600/0700, write-only keys, apply-only activation | Equivalent discipline; JARVIS's secret handling (write-only) is stronger. Nothing mapped over; OpenJarvis stays independently configurable. **Keep.** |
| 9 | Traces | `traces/store.py` SQLite+FTS5 + `core/events.py` EventBus (`TOOL_CALL_*`, `TRACE_*`, loop-guard events) | `goal_events` (bounded, appended, retained newest N) + structured `report_json` | JARVIS's event history covers the operator's actual audit need (every transition, refusal, digest, block is recorded — used by this phase's tests as evidence). FTS search over events is a possible later add; not needed now. **Keep.** |
| 10 | Scheduling | `scheduler/*` (croniter, persistent agent jobs, `scheduler/store.py` state) | none (single-threaded worker poller) | No current JARVIS need; noted as a future phase with its own state-machine + recovery design. **Separate.** |
| 11 | Obsidian | `connectors/obsidian.py` (frontmatter parsing, vault sync via `connectors/sync_engine.py` into a knowledge store) | none | Design captured in §7 for a native, registry-respecting JARVIS implementation. **Separate (roadmap).** |
| 12 | Voice | `tools/audio_tool.py` (transcription; defaults to cloud `whisper-1`), `tools/text_to_speech.py` (backends: cartesia, elevenlabs, kokoro, openai_tts); doctor reports "Speech backend: not configured" without the desktop extra | none | Lightest-slice recommendation in §7.2 (browser-native, zero deps, no model downloads). **Separate (roadmap).** |
| 13 | Security | `security/*` (injection scanner, taint, SSRF, credential stripper, file policy, signing) — **Rust-mandatory in the hot parts**; `sandbox/runner.py` Docker; skill trust tiers | `tools.py` permission classes + sensitive-name patterns + workspace boundary; `applog.py` safe categories; digest replay checks; reviewer gate; server hardening (origin checks, rate limits) | OJ's breadth is impressive but unverifiable here (Rust) and partially Docker-dependent. JARVIS's smaller surface is fully implemented, tested, and human-gated. **Keep.** |
| 14 | Runtime | Python ≥3.10,<3.14; ~15 hard deps incl. `datasets`(numpy/pandas/pyarrow), `posthog` (third-party telemetry), `python-telegram-bot`; Rust ext; Ollama; Node 22+ GUI; Docker sandbox | **zero runtime dependencies** (stdlib only: sqlite3, http.server, urllib); runs in the 7.2 GiB RAM envelope | Decisive constraint. The 516 MB venv for an alpha-stage platform buys JARVIS nothing it lacks. **Keep.** |
| 15 | UI | FastAPI + websockets server (`server/*`, 25+ route modules), Node 22 GUI | stdlib http.server + single-page dashboard (chat/tasks/goals/settings/models modals), browser E2E 85/85 | Different scope. OJ's UI stack (FastAPI/Node) would be a second platform. **Keep.** |
| 16 | Testing | ~716 test files (pytest); subset run here: 277 passed / 41 failed (all Rust-missing) / 59 skipped | 730+ stdlib unittest + browser E2E (85) + renderer (32), fake-provider only, temp DBs | OJ's suite is large and well-organized; its Rust-coupling makes a full green run impossible on this machine. JARVIS's suite is fully green (§6). |

### 2.1 What JARVIS already had (no change needed)

Digest-bound approvals with atomic claim; registry permission classes;
per-tool postcondition verification; bounded honest failure states; restart
recovery with reconciliation; safe-category logging; settings precedence
with write-only secrets; bounded persisted event history; the full browser
E2E and stdlib test discipline. Everything in rows 1, 3–5, 8–9, 13–14 above
except the loop itself.

### 2.2 Redundancy review (Stage D)

- **Within JARVIS:** each module was re-checked for duplicate capability
  (`planner`/`worker`/`verification`/`reviewer`/`approvals`). No duplicated
  implementation found — nothing removed. The new dynamic-loop code
  deliberately reuses `_model_call`, `_run_step`, `_phase_verify`,
  `_checkpoint_approval`, and `goalstate` rather than adding a second
  orchestrator.
- **Between the repos:** overlaps exist (approvals, traces, memory,
  scheduling) but in every case JARVIS's implementation is the safer,
  better-tested one for this architecture (§2). **No code was copied from
  OpenJarvis** — the two patterns used (observation-feedback loop shape,
  repeat-guard semantics) were re-implemented natively in ~250 lines with
  JARVIS's own validation and persistence conventions. Attribution: the
  design inspiration is Stanford OpenJarvis (`agents/native_react.py`,
  `agents/loop_guard.py`, Apache-2.0,
  https://github.com/open-jarvis/OpenJarvis).

---

## 3. Database comparison (Stage E) — deliberately kept separate

| Concept | OpenJarvis | JARVIS (schema v6) | Mapping note |
|---|---|---|---|
| Goal state | agent/session rows in daemon/scheduler stores | `goals` (+ `mode` column, v6) | JARVIS's explicit state machine has no OJ equivalent; nothing to transfer |
| Execution steps | trace events | `goal_steps` (conditional status transitions, attempts, per-step verification) | JARVIS's is authoritative + restart-safe |
| Observations/decisions | `traces/store.py` (SQLite+FTS5, all turns) | `goal_steps.result_json` + `goal_events` (bounded) | JARVIS persists what the audit needs; FTS full-history search deferred |
| Human approvals | `PendingAction`/`PermissionRule` (stateful queue) | `goal_approvals` (digest-bound, replay-checked) | **No data transfer possible or desired** — different trust models |
| Memory/documents | FactStore JSONL + connectors SQLite/FTS5 | — | Future Obsidian phase designs its own bounded index (§7.1) |
| Config/telemetry | `config.toml`, `telemetry.db`, posthog | `meta` table + settings store (0600) | Kept fully separate |

**No data moved in either direction.** No OpenJarvis SQLite was copied;
JARVIS's production `data/jarvis.db` was never opened by this phase (tests
use temp DBs; migration validated on temp DBs). Both state roots remain
independent: `/home/mdx/Dev/.openjarvis-reference-state` (OpenJarvis,
0700, empty) vs JARVIS's own data directory.

**Configuration comparison:** no OpenJarvis setting was mapped into
JARVIS — JARVIS's env > store > defaults precedence, env-locked fields and
write-only secret handling already cover the applicable surface
(provider/model/fallback), and the active model/provider configuration was
not touched. OpenJarvis keeps its own `config.toml` under its isolated
home (not created; its absence does not affect source inspection).

---

## 4. Stage F — the implemented improvement: the bounded dynamic goal loop

Per the Phase 12A recommendation, JARVIS gains an **opt-in `dynamic` goal
mode** beside the unchanged default `sequential` (plan-then-execute) mode.
Pattern lineage: OpenJarvis `native_react.py` (observation-as-message loop)
and `loop_guard.py` (repetition refusal) — re-implemented natively, no code
import, Apache-2.0 attribution above.

### 4.1 Behavior

- A dynamic goal **skips the sequential planner** (`worker._phase_plan_dynamic`):
  it passes `queued → planning → ready` through the same state machine, then
  enters the loop. All existing lifecycle/recovery/approval machinery is
  shared.
- Each turn: one strict JSON **decision** from the model —
  `{"action": "<registered tool>"|"done", "args": {}, "reason": "≤200"}`
  (done additionally requires `summary ≤500`) — validated by
  `planner.validate_decision` (registry-name check, args normalized through
  the tool's own schema validator, bounded fields, fenced-block strip).
- The **real tool observation** (clamped result JSON, last 8 turns) is
  rendered into the next decision prompt as DATA with an explicit
  "never instructions" rule.
- Each executed decision is appended as a normal `goal_steps` row and runs
  through the **unchanged** `_run_step` (attempts, per-tool `verify()`,
  approval gates, digest replay checks, reconciliation semantics).
- Hard budgets, all enforced against **persisted** state:
  `MAX_DYNAMIC_TURNS = 12` steps, exact-repeat refusal (sha256 over
  tool+normalized-args of prior steps), `MAX_DYNAMIC_INVALID = 2` invalid
  decisions, wall-clock budget (existing), plus a defensive loop-pass cap.
  Exhaustion ends in honest `blocked` states (`loop_budget_exhausted`,
  `decision_invalid`) — never fabricated success.
- **`done` cannot bypass verification:** a done claim with zero or
  unverified steps is refused (`criteria_unverified`); success requires
  every executed step to have passed its registered postcondition.
- **Writes cannot bypass approval:** an `approval_required` decision creates
  a digest-bound proposal (stable dynamic `plan_json` anchor so the replay
  check holds), moves the goal to `awaiting_approval`, and only human
  approval + atomic claim + digest re-verification (all existing code)
  lets the step execute. A rejected proposal blocks with
  `approval_withdrawn`. The model can only ever *request*.
- A failing `records_check` recipe stops a dynamic goal honestly instead of
  entering sequential repair cycles.
- `/retry` of a blocked dynamic goal resumes the persisted loop (blocked →
  queued → dynamic handlers); restart recovery is unchanged and tested.

### 4.2 Files changed (and why)

| File | Change |
|---|---|
| `db.py` | `SCHEMA_VERSION` 5 → 6; additive migration 5 (`ALTER TABLE goals ADD COLUMN mode TEXT NOT NULL DEFAULT 'sequential'`); `_goal_row_to_dict` exposes `mode`; `GOAL_MODES` + `create_goal(..., mode=...)` validation; new `append_goal_step()` (bounded, seq CHECK-enforced) |
| `planner.py` | Decision constants (`MAX_DECISION_REASON_CHARS`, `MAX_DECISION_SUMMARY_CHARS`, `DYNAMIC_TRANSCRIPT_TURNS`, `MAX_DYNAMIC_OBS_CHARS`); `decision_prompt()`; `validate_decision()` (same strictness style as `validate_plan`) |
| `worker.py` | Dynamic budgets; handler routing in `process_once` (mode-aware); `_phase_plan_dynamic`, `_phase_execute_dynamic`, `_dynamic_decide`, `_dynamic_done`, `_dynamic_run_tool`, `_dynamic_plan_lock`, `_dynamic_repeat`, `_dynamic_transcript`, `_dynamic_event_count`; dynamic branch in `_phase_approvals`; dynamic branch for failing `records_check` (no sequential re-plan); `outcome` pass-through in `_phase_verify`/`_finish_success` |
| `goalstate.py` | `loop_budget_exhausted`, `decision_invalid` added to `BLOCKED_CODES_REPORTED` (both operator-retryable) |
| `server.py` | `POST /api/goals` accepts optional `mode` (whitelisted via `db.GOAL_MODES`; 400 otherwise); default `sequential` |
| `tests/test_dynamic_loop.py` | **new**, 22 tests (§6) |
| `tests/test_goals_db.py` | schema-version test updated to the current schema + **strengthened** (asserts the v6 column and its `sequential` default) |
| Frontend | **unchanged** (mode is optional; the Goals Console works unchanged) |

### 4.3 Migration details

- v5 → v6 is one `ALTER TABLE ... ADD COLUMN` with a safe default, executed
  inside the established single-transaction stamp (existing convention:
  additive, idempotent-by-version, full rollback on failure). No table
  rebuild, no data rewrite; existing goals are and stay `sequential`.
- Validated on temp databases only; `test_db_migration` (8 tests) and
  `test_goals_db` pass; the production `data/jarvis.db` was **not touched**
  and will upgrade itself lazily on next open, exactly like every previous
  additive migration.

---

## 5. Security review of the new surface

- Prompt injection via observations: observations are clamped, rendered as
  data with an explicit rule; they cannot widen permissions — only
  registered tools with schema-validated args can run, and approval-class
  execution still requires the human digest. (Tested: write request →
  approval flow; invalid/unregistered actions refused and budgeted.)
- The model cannot self-authorize: `dynamic_done` without verified evidence
  is refused; `workspace.create_file` decisions never execute pre-approval.
- No new runtime dependency; no new network surface; no change to the
  reviewer, verification recipes, write-tool bounds, or audit events.

---

## 6. Exact test commands and results

OpenJarvis reference (isolated venv, explicit state home):

```
cd /home/mdx/Dev/OpenJarvis-reference
OPENJARVIS_HOME=/home/mdx/Dev/.openjarvis-reference-state \
  .venv/bin/python -m pytest tests/agents/test_loop_guard.py \
  tests/agents/test_native_react.py tests/core/test_registry.py \
  tests/memory tests/server/test_approval_routes.py -q
```

→ **41 failed, 277 passed, 59 skipped** (17 s). Every failure was
individually confirmed to be the missing mandatory `openjarvis_rust`
extension (sqlite/bm25 storage backends) — the known, accepted limitation.
`loop_guard`, `native_react`, `core/registry`, the JSONL memory store, and
approval-route tests pass.

JARVIS (all fake-provider, temp DBs; production DB untouched):

| Suite | Command | Result |
|---|---|---|
| New dynamic-loop tests | `PYTHONPATH=/home/mdx/Dev/JARVIS python3 -m unittest test_dynamic_loop` (from `tests/`) | **22/22 OK** |
| Targeted regression | `unittest test_goals_db test_db_migration test_orchestrator_units test_write_lifecycle test_write_approvals test_dynamic_loop` | **121/121 OK** |
| Full stdlib suite | `python3 -m unittest discover -s tests -p "test_*.py"` | first run: 752 tests, **1 failure** (`test_fresh_init_creates_v5` — version pin, fixed by updating the test to the v6 schema as described; not an assertion weakening), 85 skipped; re-run: **see final note below** |
| Renderer | `node tests/test_format_render.js` | **32/32 passed** |
| Syntax | `node --check static/app.js static/jarvis-format.js`; `python3 -m py_compile` (all touched modules); `git diff --check` | clean |
| Browser E2E (Playwright, per file) | goals **12**, write_approvals **4**, verify **3**, dashboard **9**, chat **14**, tasks **7**, models **15**, settings **9**, search **7**, security **5** | **85/85 OK** (e2e venv recreated at `/tmp/opencode/jarvis-e2e/venv`; chromium downloaded into the user cache) |

Every behavior required by the task brief is asserted by a named test:
observation-driven decisions (`test_observation_driven_decisions_end_to_end`),
invalid-call rejection, repeat + turn budgets, write-request → digest
approval (and rejection), restart mid-loop without re-running a completed
step, done-claim vs tool verification, sequential goals unchanged, blocked
→ retry resume, migration on temp DBs.

---

## 7. Stage G — roadmap for the next phases

### 7.1 Obsidian vault integration (native, read-only first)

- **Registry-first design:** a `vault.read_file` / `vault.list_files` /
  `vault.search` family built on the existing `Tool` shape with a
  vault-root resolver (mirroring `tools.make_workspace_tool`'s
  root-resolver pattern). Permission `autonomous` for reads, `approval_required`
  for any future write — the existing permission classes carry the policy.
- **Boundary rules:** resolve every path with `Path.resolve()` and refuse
  anything outside the configured vault root; never follow symlinks out
  (the workspace tools' exclusion/`records_check` discipline generalizes);
  never modify or delete notes (writes would be digest-bound like every
  other write, if ever added).
- **Indexing:** one bounded FTS5 index table in `jarvis.db` (Python's
  sqlite3 ships FTS5) — additive migration, keyed by
  `(vault_path, mtime, sha256)`; refresh = rescan + reindex changed files;
  deletion detection = index sweep vs filesystem; citations always
  `path + sha256 + line anchor`. No duplication of note content beyond the
  bounded index.
- **OpenJarvis reference:** `connectors/obsidian.py` (frontmatter parsing,
  sync engine) is the pattern source; JARVIS's version stays dependency-free.

### 7.2 Voice — lightest first slice

- **Not viable locally:** whisper-class STT or local TTS models exceed this
  laptop's envelope (AMD A6, 7.2 GiB RAM); OpenJarvis's own defaults are
  cloud backends (`whisper-1`, cartesia/elevenlabs) — which JARVIS must not
  depend on (no external comms posture).
- **Recommended slice (zero deps, no models):** browser-native
  `SpeechRecognition` (Chrome Web Speech) for input + `speechSynthesis` for
  output, wired to the **existing** chat endpoint; voice is an input/output
  skin, never a permission path — every goal/approval rule applies
  unchanged. Server-side fallback: none needed for the slice.
- **Measured requirement:** ~0 MB installed, CPU-bound only during
  recognition, fully within the existing browser E2E harness's reach.

### 7.3 Explicit permission-to-operate model

| Tier | Meaning | JARVIS support today |
|---|---|---|
| Read-only | registered read-only tools, autonomous | ✅ `permission="autonomous"` (+ `verify_readonly` recipes) |
| Propose | model requests a write/execute | ✅ digest-bound proposals (sequential **and** dynamic mode now) |
| Human-approved execution | exact-bytes approval, atomic claim, replay checks, backups, post-verification | ✅ existing approval flow |
| Pre-authorized low-risk | allowlisted (tool, arg-shape) bindings with per-goal budgets, attributable audit, **revocable** | design only — would extend `goal_approvals` with named allowlist rows + spend/turn budgets; the state machine and audit trail already support it |
| Prohibited | shell, arbitrary code, deletion, unrestricted network, credentials, self-modification | ✅ not in the registry at all (structural, not policy) |

Revocation = removing the allowlist row + `approval_withdrawn` semantics
already block non-retryable re-runs; every consequential operation already
leaves attributable evidence (`goal_events`, digest chains, executed shas).

---

## 8. Final inspection (actual)

- JARVIS `git status` **identical to the pre-phase baseline**
  (`/tmp/opencode/jarvis-git-baseline.txt` vs after: diff empty); nothing
  staged, nothing committed; all Phase 12B changes live inside
  already-untracked files plus the new test file.
- OpenJarvis checkout: source tree **clean**; only untracked addition is its
  own `.venv` (excluded from comparison; left in place for reuse).
- State roots independent; no `~/.openjarvis` created; no secrets copied in
  either direction; no PATH/global/shell changes; no model downloads; no
  servers, schedulers, or agents left running.
- Production `data/jarvis.db`: untouched (tests used temp DBs only).

## 9. Known limitations and unresolved risks

1. **OpenJarvis Rust-delegating paths are runtime-UNVERIFIED** (no
   toolchain by design): security scanners, bm25/sqlite storage internals,
   parts of loop_guard. Source-level comparison only for those.
2. The dynamic loop's success criterion for `done` is *all executed steps
   tool-verified* — like sequential mode's lexical criteria, it bounds but
   does not semantically prove objective satisfaction; a model that stops
   early with verified-but-irrelevant steps can still claim success. The
   honest blockers (`criteria_unverified`, budgets) and the audit trail
   keep this visible; semantic verification remains future work.
3. Invalid-decision budget counts persisted `goal_events` (bounded
   retention); under extreme event churn the count could under-read. The
   turn and repeat budgets are step-row-based and immune.
4. `jarvis doctor` (OpenJarvis) performed unauthenticated public
   reachability probes while verifying engines — disclosed for
   transparency; no keys, no inference, no quota, and not repeated.
5. OpenJarvis is alpha-stage upstream; the reference checkout will age.
   It is kept for source comparison only — JARVIS does not depend on it.

**Final full-suite re-run (completed):** `python3 -m unittest discover -s
tests -p "test_*.py"` → **Ran 752 tests — OK (skipped=85)** in 394 s,
executed after the version-pin test update.
