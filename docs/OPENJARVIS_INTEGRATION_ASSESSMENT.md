# OpenJarvis Integration Feasibility Assessment (Phase 12A)

**Date:** 2026-10-26 · **Scope:** READ-ONLY audit — no installation, no source
changes, no configuration or database changes, no live inference.
**Sources inspected:** JARVIS working tree at `/home/mdx/Dev/JARVIS` (actual
implementations, tests, and docs) and the official
`open-jarvis/OpenJarvis` repository (`main`, cloned read-only to
`/tmp/opencode/openjarvis-src` for inspection only; never installed).

---

## 1. Actual JARVIS capabilities and gaps

### 1.1 What the code implements today

| Component | Module | Verified behavior (source-inspected) |
|---|---|---|
| Goal lifecycle | `goalstate.py`, `db.py` (schema v5), `server.py` `/api/goals*` | Explicit state machine: `queued → planning → ready → executing → awaiting_approval → verifying → succeeded/failed/blocked/cancelled`; every transition validated; terminal states cannot be re-entered |
| Planner | `planner.py` | ONE model call → strict single-JSON plan: ≤12 steps, only registered tool names, schema-validated args, `depends_on` strictly backward (acyclic by construction), `risk` must be exactly `"low"`, criteria token-coverage enforced; exactly one bounded repair attempt on rejection |
| Execution | `worker.py` `GoalWorker` | Single-threaded, bounded (300 s wall budget, 2 attempts/step), sequential over the **pre-generated persisted plan**; cancellation checkpoints; restart recovery re-runs only idempotent steps and reports interrupted non-idempotent steps for human reconciliation |
| Model interface | `provider.py` `complete_with_fallback` | Plain role/content messages only — **no native function/tool calling**; one pinned config snapshot per call; ≤1 fallback attempt; typed error taxonomy |
| Tools | `tools.py` `ToolRegistry` | Permission boundary: `autonomous` / `verify_readonly` / `approval_required`; strict `validate_args` against declared schema; per-tool `verify()` postconditions; `records_check` flag drives repair eligibility |
| Approvals | `approvals.py`, `db.goal_approvals` | Digest-bound (SHA-256 over goal+seq+tool+args+plan) proposals; atomic `approved→claimed` conditional UPDATE (no double-run); digest replay verified against persisted rows before execution; Phase 10A: write plans refuse to proceed without external review |
| Reviewer | `reviewer.py` | Deterministic `local_safety_check` always runs; injectable external reviewer (`UnavailableReviewer` default, `OpenCodeReviewer` subprocess adapter); verdict advisory; ≤2 rounds; failed/unavailable review never fabricates approval |
| Verification repair | `worker.py` Phase 10B | Failing check recipe → bounded evidence (1200-char tails) → re-plan; max 2 repair cycles, then honest `repair_budget_exhausted` |
| Settings | `settingsstore.py` + `/api/settings*` | env > user store > defaults; env-locked fields; atomic 0600/0700; write-only keys; apply-only activation |
| Logging | `applog.py` | Safe categories only; untrusted text never enters log streams |

### 1.2 The critical loop question

**JARVIS is plan-then-execute, not an iterative agent loop.** Evidence from
`worker.py`:

- `_phase_plan` makes the model call(s) **before** any tool runs; the full
  step list is persisted (`replace_goal_steps`) and then executed.
- `_run_steps` iterates persisted steps calling `tool.run(args)` and
  `tool.verify(args, result)`; **tool observations never reach the model**.
  The only post-plan model invocations are:
  1. one bounded plan-repair attempt when the plan JSON is rejected,
  2. reviewer-driven revision rounds (≤2, plan-level, not step-level),
  3. verification-driven repair cycles (≤2, re-plans from recorded recipe
     evidence).

So today JARVIS cannot decide mid-run, based on an actual tool result, that
a *different* unanticipated tool call is needed. It can only re-plan
(heal) within bounded budgets around a finite pre-decided plan. This is the
single biggest architectural gap on the path to an autonomous business
operator, and it is exactly the gap OpenJarvis's agent loops address.

### 1.3 Implemented vs. verified end-to-end

| Capability | In code | Verified |
|---|---|---|
| Chat via provider (+ fallback policy) | yes | browser E2E vs fake upstream (85/85); **no live provider verification** (no credential configured) |
| Tasks/conversations/search/settings/metrics | yes | stdlib + browser E2E (temp DBs) |
| Goal planning→execution→verification | yes | stdlib (`test_goals_*`, `test_orchestrator_units`, `test_write_*`, `test_verify_*`) + browser (`test_browser_goals`, `test_browser_write_approvals`, `test_browser_verify`) — all fake-provider, temp DBs |
| Digest-bound approvals + atomic claim | yes | stdlib + browser E2E |
| External reviewer (OpenCode adapter) | yes | `test_reviewer_opencode.py` with a stubbed runtime; no live reviewer run |
| **Model-driven mid-run decisions** | **no** | — |

### 1.4 Gaps for "autonomous business operator"

1. No observe→decide→act loop between steps (§1.2).
2. No persisted long-term knowledge retrieval layer (the planned Phase 11C
   Obsidian work) — nothing for the planner to consult beyond its prompt.
3. Criteria verification is lexical token-coverage, not model judgment —
   safe, but weak for fuzzy business outcomes.
4. Single-threaded worker: fine for one operator, but no scheduling of
   long-horizon/recurring work.

---

## 2. OpenJarvis audit (official repository, current `main`)

**Identity:** Stanford Hazy Research / Scaling Intelligence Lab (SAIL);
Apache-2.0; ~687 source files, ~716 test files; Python `>=3.10,<3.14`
(host system is 3.12.3 — compatible); arXiv 2605.17172; 12.1k stars.
Tagline: "Personal AI, On Personal Devices."

### 2.1 Relevant modules and interfaces

| Area | Module(s) | What it actually does |
|---|---|---|
| ReAct loop | `agents/native_react.py` | Thought-Action-Observation loop: model replies parsed (`Action`/`Action Input`/`Final Answer`, JSON or regex), `Observation: <tool result>` appended as a **user message**, loops to `max_turns`; every result collected into `AgentResult.tool_results` |
| Orchestrator agent | `agents/orchestrator.py` | Multi-turn reasoning with automatic tool selection; `_default_max_turns = 10`; stops on max turns with an honest result |
| Loop guard | `agents/loop_guard.py` | `check_call` blocks repetitive/failing tool calls; `check_response` detects degeneration (incl. ping-pong); `compress_context` bounds transcript growth — **directly relevant to a JARVIS iterative loop** |
| Tool executor | `agents/executor.py` + `core/registry.py` | Decorator-based typed registries (agents/engines/tools/fact-stores); `AgentExecutor` resolves tool sets and threads `confirm_callback` through agents for human confirmation |
| Approval store | `tools/approval_store.py` | SQLite `PendingAction` + `PermissionRule` queues — conceptually parallel to JARVIS `goal_approvals`, but **not digest-bound to plan content** |
| Engine abstraction | `engine/_base.py` + ollama/vllm/litellm/cloud engines | `InferenceEngine` interface; OpenAI-compatible and local engines; context-length error taxonomy |
| Event bus | `core/events.py` | Thread-safe pub/sub (`INFERENCE_*`, `TOOL_CALL_*`, `TRACE_*`, `SECURITY_*`, loop-guard events) |
| Traces | `traces/store.py` | SQLite + **FTS5** trace store with collector/analyzer; drives learning policies |
| Memory | `memory/store.py`, `extractor.py`, `service.py` | Auto-extracted durable "facts" (JSONL + pluggable `FactStore`), trust tiers (`auto`/scanner-clean), file-locked; connectors use SQLite/FTS5 |
| Skills | `skills/*` | agentskills.io standard; trust tiers (bundled > workspace > indexed > unreviewed); dangerous-capability gate (`shell:execute`, `network:listen`, `filesystem:write`); importers (Hermes, OpenClaw) |
| Scheduler | `scheduler/*` | croniter-backed persistent scheduled agents with state |
| Sandbox | `sandbox/*` | Docker `ContainerRunner` with mount allowlists; wasm runner |
| Security | `security/*` | Injection scanner, taint tracking, guardrails, SSRF checks, credential stripper, file policy, signing |
| Server | `server/*` | **FastAPI** + websockets API server (separate surface from the agents) |

### 2.2 Dependency / resource footprint (from `pyproject.toml`)

- **Hard deps:** click, croniter, **datasets≥4.5** (pulls numpy/pandas),
  ddgs, httpx, openai, nvidia-ml-py, **posthog≥3.0 (third-party
  telemetry)**, psutil, **python-telegram-bot**, rich, tomlkit,
  websockets. Optional extras: faiss/sentence-transformers, vllm, mlx,
  litellm (pinned 1.104.2 for a known CVE window), tavily.
- **Runtime companions:** Ollama + a starter model (installer), a Rust
  extension built via maturin (1.5 MB of Rust source + toolchain), Node 22+
  for the GUI, Docker for the sandbox.
- Install path is `curl … | bash` (uv + venv + Ollama + model download,
  ~3 min) — **explicitly excluded by this audit's constraints**.

### 2.3 License

Apache-2.0 — compatible with reuse; attribution required for any copied
component. (This audit copies nothing.)

---

## 3. Integration options compared

| Criterion | **A. Native iterative loop in JARVIS** | **B. OpenJarvis as isolated engine behind an adapter** | **C. Selective pattern/component reuse** |
|---|---|---|---|
| New dependencies | **none** | uv + ~15 hard deps (incl. `datasets`, `posthog`, telegram), Rust toolchain, Ollama, Node 22+, Docker for sandbox | none |
| Memory footprint | unchanged; loop adds bounded prompt growth only | large (pandas/numpy/datasets; local models via Ollama; FastAPI+ws server) — poor fit for the older AMD laptop | none |
| Provider compatibility | reuses `complete_with_fallback` (text-in/JSON-out) as-is — OpenJarvis's own ReAct proves JSON-over-text works without native tool-calling | engine layer would need to talk to JARVIS's fixed FreeLLMAPI/OrcaRouter bases; separate engine config to maintain | n/a |
| Database separation | stays in JARVIS schema v5 (additive if needed) | second SQLite (traces/FTS5, approvals, scheduler) beside `jarvis.db` — two sources of truth | none |
| Execution security | tool execution stays inside JARVIS registry, permission classes, digest approvals, and verification — **no bypass path exists** | OpenJarvis agents can execute skills/shell (trust-tiered) and its own approval queue; JARVIS approvals would have to wrap an external process that has its own tool surface — high bypass risk, needs container sandboxing (Docker) to be equivalent | none |
| Approval/recovery compatibility | native: digest gate + restart recovery already surround any new step execution | adapter must translate OpenJarvis `PendingAction` ↔ JARVIS digest approvals and map its turn loop onto the goal state machine — substantial, lossy | none |
| Maintenance cost | one codebase; ~1 worker phase + tests | track an alpha-stage upstream (Development Status :: 3 - Alpha, 1,295 commits, fast-moving) + dependency/pin churn (e.g., litellm CVE pin) | low but limited payoff |
| Time to value | one bounded phase | weeks of integration + hardening before trust parity | immediate but shallow |

**Exact useful integration points found:** `LoopGuard` semantics
(repetition/ping-pong detection + context compression) and the
observation-as-user-message loop shape from `native_react.py`; the
trace-event taxonomy idea (`core/events.py`) for observability; skill
trust-tier concepts for any future skill system. These are **patterns to
re-implement in ~100–200 lines**, not dependencies to import.

**Conclusion:** Option B imports an entire parallel platform to gain one
capability (the iterative loop) while defeating two of JARVIS's core
properties — zero dependencies and a hard-wired human approval boundary.
Option C is free but does not by itself close the loop gap. **Option A is
the only option that adds the missing capability without weakening
security.**

---

## 4. Security and compatibility risks

- **Prompt injection via tool output (Option A's main new risk):** tool
  results would enter the model context as observations. A note/file saying
  "ignore all rules…" must not be able to change permissions. Mitigations
  (already in JARVIS's favor): only registered tools with validated args
  can run; permission classes are server-side; approval-class tools still
  require digest-bound human approval regardless of what the model asks;
  observations must be length-clamped and logged as data, and the
  validation grammar must reject anything but one tool call + args.
- **Unbounded loops:** the natural failure of ReAct loops. Mitigation:
  hard `max_turns` budget persisted per goal, wall-clock deadline
  (already exists), OpenJarvis-style loop-guard (repeat/failure detection)
  → honest `blocked: loop_budget_exhausted`.
- **Option B risks:** `posthog` telemetry conflicts with JARVIS's
  loopback/no-phoning-home posture; Docker sandbox adds a privileged
  component; two approval systems invite bypass; alpha-stage upstream
  churn; SQLite/FTS5 files multiply state.
- **Compatibility:** Python 3.12.3 satisfies OpenJarvis's range, so the
  blocker is not language — it is the operational footprint and the
  approval bypass surface.

---

## 5. Recommended architecture

**Adopt Option A now; harvest Option C patterns; keep Option B off the
table until JARVIS ever needs local model serving, which `provider.py`
already reaches via OpenAI-compatible HTTP.**

Concretely, add a bounded **dynamic execution mode** alongside the existing
plan-then-execute mode — no replacement:

1. **New goal mode:** `mode: "sequential"` (today, default) or
   `"dynamic"` (new). Same state machine; `executing` gains a step-loop
   handler that, after each tool observation, may ask the model for the
   next action.
2. **Decision call:** one strict JSON decision per turn —
   `{"action": "<registered tool>"|"done", "args": {...},
   "reason": "≤200 chars"}` — validated exactly like plans today
   (`planner.validate_plan`-style: registry name check, schema-validated
   args, clamped sizes). Unknown/invalid → bounded retry → honest block.
3. **Observation transcript:** per-goal, bounded (e.g., last N turns ×
   clamped observation chars), persisted to `goal_events` so restart
   recovery keeps working; observations enter prompts as data with a
   fixed delimiter and are never interpreted as instructions.
4. **Budgets (all persisted, all honest):** max turns per goal
   (e.g., 8), wall-clock deadline (exists), per-step attempt cap (exists),
   loop-guard → block on repetition/failure patterns.
5. **Security invariants unchanged:** read-only tools run autonomously;
   any `approval_required` tool still moves the goal to
   `awaiting_approval` with a digest-bound proposal — the model can only
   *request*, never approve; verification recipes still gate success.
6. **Tests:** fake-provider only, temp DBs — decision-call validation,
   budget exhaustion, injection-replay attempt, approval interception
   mid-loop, restart mid-loop, and the existing 730-test suite unchanged.

---

## 6. One narrowly scoped implementation phase (12B proposal)

**Phase 12B — "Read-only dynamic goal slice."**

*Vertical slice:* a dynamic-mode goal that (1) calls a registered
read-only tool, (2) receives the actual result as an observation,
(3) asks the configured model whether another registered call is needed,
and (4) terminates when a declared acceptance criterion is verifiably met
or a bounded blocker (`loop_budget_exhausted`, `decision_invalid`) occurs.

**Measurable acceptance criteria:**
- A dynamic goal over registered read-only tools succeeds end-to-end with
  ≥1 observation-driven decision (fake upstream proves the loop, not the
  model).
- Tool execution passes exclusively through `ToolRegistry` + permission
  checks; a decision naming an unregistered/non-low-permission tool is
  rejected with a recorded event.
- A malicious observation attempting policy override changes nothing
  (asserted: approval-class step still requires digest approval; registry
  set unchanged).
- Max-turns/loop-guard exhaustion ends in `blocked` with the persisted
  evidence, never a fabricated success.
- Restart mid-loop resumes from persisted state; non-idempotent steps are
  still reported for reconciliation.
- Full stdlib suite green; new tests ≥ ~25 cases; browser E2E for the
  Goal Console unchanged (85/85).
- No new runtime dependencies; no changes to `jarvis-format.js`, chat,
  settings, or verification recipes.

**Explicitly out of scope for 12B:** write tools in dynamic mode, skills,
scheduler, any OpenJarvis installation, vector/semantic memory.

---

## 7. Evidence and official references

- JARVIS source: `worker.py` (phases and `_run_steps`), `planner.py`
  (single-plan validation), `provider.py` (`complete_with_fallback`,
  plain messages), `tools.py` (registry/permissions), `approvals.py`
  (digests), `reviewer.py`, `db.py` (schema v5), tests as listed in §1.3,
  `docs/JARVIS_ORCHESTRATOR_ARCHITECTURE.md`.
- OpenJarvis official repository:
  https://github.com/open-jarvis/OpenJarvis (Apache-2.0;
  `requires-python = ">=3.10,<3.14"`); docs
  https://open-jarvis.github.io/OpenJarvis/; paper
  https://arxiv.org/abs/2605.17172; project site
  https://openjarvis.stanford.edu/. Specific files inspected:
  `agents/native_react.py`, `agents/orchestrator.py`,
  `agents/loop_guard.py`, `agents/executor.py`, `core/registry.py`,
  `core/events.py`, `engine/_base.py`, `memory/store.py`,
  `tools/approval_store.py`, `skills/security.py`, `sandbox/runner.py`,
  `traces/store.py`, `pyproject.toml`.

---

## 8. Decisive recommendation

**Do not install or embed OpenJarvis.** Implement the iterative
observe→decide→act loop natively in `worker.py` (Phase 12B slice above),
borrowing only the *patterns* OpenJarvis proves out — ReAct-over-JSON with
strict parsing, loop-guard budgets, and event-trace observability. This
closes JARVIS's one true autonomy gap while preserving what makes JARVIS
trustworthy: zero dependencies, digest-bound human approvals, honest
bounded failure, and restart-safe persistence. Re-evaluate Option B only
if JARVIS later needs capabilities OpenJarvis uniquely provides (e.g.,
local model serving via Ollama) — and even then prefer OpenJarvis's
engines over its agent runtime.
