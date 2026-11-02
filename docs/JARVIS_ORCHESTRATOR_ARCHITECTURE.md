# JARVIS Orchestrator Architecture (Phase 8B; Phase 10A adds writes; Phase 11A adds the configuration center & OrcaRouter)

One coherent execution architecture for durable goals. This document
describes the *actual* implementation — components, contracts, state
ownership, and failure propagation. Future phases (specialist agents,
sales operations, coding tools, web research, memory, scheduled work)
must extend these components, not bypass them.

No live AI inference was used to build or validate this design; all
behavior in this phase is covered by the fake upstream and temp DBs.

## Component map

| Component | Module | Owns |
|---|---|---|
| Goal manager | `db.py` (goal tables) + `server.py` endpoints | Durable goal/step/event rows, IDs, timestamps |
| Lifecycle state machine | `goalstate.py` | Which goal/step transitions are legal; validates every write |
| Planner | `planner.py` | One-step LLM planning call + strict structured-plan schema |
| Plan validator | `planner.validate_plan` | Rejects malformed plans, unknown/unregistered tools, bad deps, bad sizes, non-low risk |
| Reviewer interface | `reviewer.py` | Deterministic local safety check (always runs) + injectable external reviewer (none configured in 8B → honestly recorded) |
| Tool registry | `tools.py` | Explicitly registered typed tools: schema, permission, idempotency, verifier |
| Permission policy | `tools.py` server-side registry + `worker.py` enforcement | The server decides autonomy; the model never can |
| Execution engine | `worker.py` | Sequential bounded step execution w/ cancellation + retry caps |
| Verifier | registry per-tool verifier + `worker.finalize_goal` | Postcondition checks; success only on all-passed criteria coverage |
| Persistent job state | `goal_events` + goal/step rows | Recoverable progress across restarts |
| Reporting | `worker.finalize_goal` → `goals.report_json` | Honest outcome, failures, evidence refs, remaining work |

Reused unchanged: `provider.complete_with_fallback` (the ONLY model
interface; plans and any future model calls go through it), `db.open_conn`
transactions, `applog.event` sanitized logging, `server.py` HTTP
conventions, existing chat/tasks metrics endpoints.

## Configuration & provider architecture (Phase 11A)

One settings system, not a second one: the same precedence chain the
runtime has always used, now writable through the UI.

| Component | Module | Owns |
|---|---|---|
| Runtime config load | `config.py` | Env parsing + `Config.env_managed` (which settings the environment explicitly set) |
| Persisted user configuration | `settingsstore.py` | `<config dir>/settings.json` + `credentials.json`, atomic 0600 writes under a 0700 dir, write-only key surface |
| Effective config snapshot | `settingsstore.effective_config` | env > user store > defaults merged into one frozen `Config` the whole app reads |
| Provider profiles | `provider.PROVIDER_PROFILES` | Fixed server-side base URLs (`freellmapi` loopback, `orcarouter` https://api.orcarouter.ai/v1) — no client payload can set a URL |
| Catalog & pricing | `provider.list_models` / `classify_pricing` / `fetch_pricing_catalog` / `fetch_free_package` | Authenticated catalog + public pricing/free-package merge (public endpoints never see the key) |
| Settings HTTP surface | `server.py` `/api/settings*` | Validation, env-lock enforcement, rate limits, test-connection/apply/model-test semantics |
| Per-request snapshot binding | worker/chat request paths | Each request resolves one `Config` snapshot up front — a concurrent settings change never splits an in-flight request across model policies |

**Precedence (per setting):** explicit environment variable → persisted
user configuration → application default. Environment-managed fields
are reported `*_locked` in `/api/settings` and refuse writes with 409
`field_locked`; the UI shows the effective value honestly instead of
pretending an update applied.

**Credential storage:** per-user directory outside the repository
(`JARVIS_CONFIG_DIR` or `XDG_CONFIG_HOME`/`~/.config/jarvis`);
`credentials.json` holds profile-keyed secrets written atomically
(temp + fsync + `os.replace`) at 0600 under a 0700 directory. The
store exposes only boolean presence and the source of the active key
(`environment` | `user_store`); no read API, log, error, HTML, or
browser storage ever receives a value. Keys are never written to
SQLite or any tracked file. At-rest protection is filesystem
permissions only — no custom encryption is attempted, and no OS
keyring is integrated (documented limitation).

**Provider activation:** saving a key, testing a connection, or
fetching a catalog never changes production behavior. Only
`POST /api/settings/apply` activates a provider/model policy, after
credential, catalog-availability, and (for OrcaRouter `paid`/`unknown`
models) explicit cost-confirmation validation. Any failure keeps the
previous working configuration; the persisted store is never replaced
with an invalid state.

**Cost classification (`pricing_class`):** `free_verified` only when
the authoritative catalog marks the model free or returns a supported
zero-cost pricing entry; `paid` when reliable catalog pricing
establishes nonzero cost; `unknown` otherwise. Never inferred from the
model id; the OrcaRouter pricing/free-package public endpoints are
fixed allowlisted URLs fetched without the API key.

## Persistence (schema v5, additive on v4 — plus two table rebuilds)

`goals(id, objective, status, created_at, updated_at, plan_json,
report_json, error_code, cancel_requested)`; `goal_steps(id, goal_id,
seq UNIQUE-per-goal, purpose, tool_name, args_json, depends_on,
acceptance, status, attempts, result_json, verification, error_code,
started_at, finished_at)`; `goal_events(id, goal_id, ts, event,
detail)` — events are bounded (detail ≤ 300 chars, retention-capped per
goal). Migration: additive-only EXCEPT two Phase 10A table rebuilds
(determined by CHECK constraints that SQLite cannot ALTER): the goals
status CHECK gains `awaiting_approval` and the goal_steps args cap is
raised for write payloads — both rebuilds copy every existing row
unchanged inside the same explicit transaction as the version stamp
(FK checks are disabled for the duration of the rebuild so the DROP of
the old shaped tables cannot cascade-delete child rows). Adds a
digest-bound `goal_approvals` table (below). One transaction, version
stamped, refuses downgrade; preserves conversations/messages/tasks/
meta and all goal rows.

Phase 10B (v4→v5) extends `goals` with `repair_count INTEGER NOT NULL
DEFAULT 0` (bounded 0..9) and `last_failure_json TEXT` (≤ 4000 chars,
schema-shaped diagnostics of the most recent verification failure);
widens the `goal_approvals.kind` CHECK to also cover `execute` (test
runs); both via two more measured table rebuilds in one transaction.

## Goal lifecycle & transition table (enforced in `goalstate.transition`)

```
queued            → planning | cancelled
planning          → ready | blocked | failed | cancelled
ready             → executing | cancelled
executing         → verifying | awaiting_approval | blocked | failed | cancelled
awaiting_approval → executing | blocked | cancelled
verifying         → succeeded | failed | blocked | cancelled
blocked           → queued (explicit /retry only) | failed | cancelled
succeeded, failed, cancelled   (terminal — never reactivated)
```

- `cancel_requested` is a durable flag set by the cancel endpoint; the
  transition to `cancelled` happens at the next cancellation checkpoint
  (immediately for `queued`; between phases/steps otherwise). A worker-
  held state cannot be hijacked mid-transition because transitions are
  conditional writes: `UPDATE ... WHERE status=<expected>`.
- Progress is derived from persisted steps (`{total, done, failed,
  running}`) — never a fabricated percentage.
- States owned by the worker: `planning, executing, verifying`. States
  owned by API actions: `queued` (create), `cancelled` requests,
  `blocked → queued` (retry).

## Planner contract

Input: the user objective + bounded registry summary (names, schemas,
descriptions — no secrets). Model call: `provider.complete_with_fallback`
with the configured model policy (primary ≤ 1 fallback; bounded by the
Phase 8A policy). The reply must be a single JSON object (code fences
stripped once; nothing else).

Schema (all bounds enforced):
`{summary ≤ 300, outcome ≤ 500, criteria[1..10 each ≤ 300],
 steps[1..12] each {purpose ≤ 200, tool ∈ registry, args ŝchema-validated
 ≤ 2KB, depends_on strictly-ascending seq refs (acyclic by
 construction; forward/missing/duplicated refs rejected),
 acceptance ≤ 300}, risk ∈ {"low"}, uncertainties? ≤ 5 × 200}`
Invalid first plan → exactly one repair attempt (same prompt + the
validator's rejection reason). Still invalid → `blocked`
(`invalid_plan`). Critical missing information must surface as
`uncertainties`, never invented facts.

## Review & approval

- `reviewer.local_safety_check(plan, registry)` ALWAYS runs
  independently of the planner: re-validates structure, tool
  permission/registry membership, and reject criteria — distinct from
  (and after) the planner's own parse.
- External reviewer: injectable callable on the worker
  (`reviewer=...`). Default stays `UnavailableReviewer` — nothing
  configured → goal records say exactly that
  (`review_unavailable` event, `local_validation` source); there is no
  same-model self-critique masquerading as independent review, and no
  fabricated approval. Plans requiring unavailable reviewers are not
  silently approved: for the shipped capability class (read-only
  `workspace.list_files`) the established local-validation-only policy
  applies, recorded in the plan.
- **Phase 9B adds a real independent reviewer; Phase 9B-R replaces its
  backend with the OpenCode CLI** in `reviewer.OpenCodeReviewer`:
  each review is a fresh non-interactive run —

  `opencode run <static one-line message> --pure --format json
  -f <review input file> --dir <bare work dir> [-m <provider/model>]`

  Immut and restriction properties (verified against the INSTALLED
  binary v1.19.0, never assumed):
  - Fresh session per review: no `--continue`, `--session`, `--fork`,
    `--attach`, no sharing, and never `--auto` (the documented
    auto-approval bypass is banned from the argv).
  - Per-run isolated temporary HOME: only a restrictive
    `~/.config/opencode/opencode.json` —
    `{"permission": {"*": "deny"}, "mcp": {}, "share": "disabled"}` —
    plus a SYMLINK to the operator's `auth.json` (never a copy, never
    printed). `opencode debug config` run under that HOME is verified
    in tests to resolve zero plugins, zero agents, no skills, no
    project instructions and exactly those deny values — the CLI's
    permissive defaults are overridden, not relied upon.
  - A second deny layer: `OPENCODE_PERMISSION='{"*":"deny"}'` (the
    CLI's documented inlined-permissions override) plus
    `OPENCODE_DISABLE_AUTOUPDATE=1` and
    `OPENCODE_DISABLE_MODELS_FETCH=1` in the child environment, which
    is a strict whitelist (PATH/LANG/TZ/TERM/HOME/TMPDIR) — all
    `JARVIS_*`/`FREELLMAPI_*` credentials and inherited `OPENCODE_*`
    overrides are stripped, so the reviewer can neither read JARVIS
    secrets nor un-restrict itself through the environment.
  - The bounded review prompt (goal, plan, tool manifest with declared
    capabilities, local-validation result) travels as a 0600 temp file
    attached via `-f` — never argv, never logs — inside a 0700 temp
    tree; the whole tree is deleted in `finally`.
  - `--dir` points at an empty dedicated work directory (and the
    subprocess cwd matches), so no JARVIS-owned file or hidden
    project instruction can be discovered.
  The reviewer cannot execute the goal's tools and holds no authority:
  its verdict/risk are advisory; deterministic server policy stays
  authoritative.
- Output parsing (Phase 9B-R): the CLI's `--format json` stream is a
  bounded NDJSON event stream; the adapter tolerates framing and
  extracts only final assistant TEXT events. Status, tool-call, and
  reasoning events are NEVER a review; a zero exit code never approves
  anything — the last text event must pass the strict validator below.
  The real event shape was pinned live in Phase 9B-R
  (`step_start` → one or more `{"type":"text","part":{"type":"text",
  "text":...}}` → `step_finish`; error invocations emit `{"type":
  "error",...}`). Reviewer identity comes from the CLI's own reported
  model id when the stream carries one (`opencode CLI (<model>)`) —
  the observed stream reports none, so records truthfully carry plain
  `opencode CLI`. Never invented.
- Reviewer response contract (strictly validated; anything else is
  `review_failed`, never coerced):
  `{"verdict": "approved"|"revise"|"rejected",
    "risk_level": "low"|"medium"|"high", "findings": [≤10 × ≤300c],
    "missing_criteria": [≤10 × ≤300c],
    "recommended_changes": [≤10 × ≤300c]}`
  Unexpected keys, unknown verdicts/risk values, oversized strings, and
  non-string items are invalid. The exit status, timeout, oversized
  output, bad envelope, and unparseable result each map to a safe
  failure reason; CLI stderr is never copied anywhere.
- Worker review gate (`worker._review_gate`): bounded to **two review
  rounds** (one revision). `revise` ⇒ one bounded revision round (a new
  planner call carrying the reviewer's concrete feedback, re-validated
  + re-checked locally, then re-reviewed; final-round `revise` ⇒ block
  `review_rejected`). `rejected` ⇒ block immediately. `approved` ⇒
  `external_review` with the identity the CLI itself reported.
  `failed`/`unavailable` ⇒ the goal proceeds ONLY under the recorded
  local-validation-only policy (current read-only class) — approval is
  never fabricated, and the plan carries the exact status + reason
  (`reviewer_missing`, `reviewer_timeout`, `reviewer_cli_error`,
  `reviewer_output_oversized`, `reviewer_bad_envelope`,
  `invalid_review`, `reviewer_exception` …).
- Persistence (no schema change): the review provenance block rides in
  the existing `plan_json` (`{"review": {status, verdict, identity,
  risk_advisory, findings, missing_criteria, recommended_changes,
  failure_reason, reviewed_at, round, plan_revised,
  local_validation}}`) and success reports expose `review`,
  `review_identity`, `review_rounds`, `plan_revised`. Restarts cannot
  turn unavailable/failed/rejected reviews into approvals: recovery
  only resumes persisted state; a review-rejected goal stays blocked
  with its record, and a retry replans from scratch with fresh
  provenance.
- Configuration (`config.py`): `JARVIS_REVIEWER` = empty (default) or
  `opencode`; `JARVIS_REVIEWER_PATH` overrides the executable and
  `JARVIS_REVIEWER_MODEL` pins the reviewer model as `-m` (the CLI's
  default model cannot be used non-interactively: its free-tier
  gateway rejects such runs — observed live; pin a catalog-verified
  provider/model, e.g. `openrouter/deepseek/deepseek-v4-flash`).
  Recorded identities remain distinguishable from installed /
  configured / successfully-invoked / genuinely-independent — only a
  valid externally produced review is ever labeled `external_review`.
- **Phase 10A adds the human approval layer on top of the reviewer**:
  plans containing any `approval_required` tool are NEVER allowed to
  proceed with a failed/unavailable review (`worker._review_gate`
  blocks with `reviewer_unavailable` before any proposal exists) —
  the read-only local-validation-only path above applies only to
  plans that are entirely `autonomous`. After a real external
  `approved` verdict, every write step is materialized as a
  digest-bound proposal (next section) and the goal stops in
  `awaiting_approval` until the operator approves each exact proposal.
  Neither the model, the reviewer, nor any API call can flip a write
  into execution without that approval record.

## Write proposals & human approval (Phase 10A)

- A proposal row per approval step in `goal_approvals`: `(goal_id,
  seq UNIQUE, tool_name, path, kind ∈ create|edit, before_sha256,
  after_sha256 (content hash), bytes, plan_sha256, digest, state ∈
  proposed|approved|claimed|executed|rejected)`. State moves are
  conditional UPDATEs: `proposed→approved` (operator decision),
  `approved→claimed` (exactly one execution wins), `claimed→executed`
  (verified result). Rejection is terminal for the proposal; the goal
  then blocks (`approval_withdrawn`, not retryable — a rejected write
  never silently re-runs; a NEW goal may be created instead).
- The approval digest is exactly: sha256 over canonical JSON of
  `{goal_id, seq, tool, path, kind, before_sha256, content_sha256,
  plan_sha256}` (`approvals.digest_for`). It binds the proposal to the
  persisted step args (which hold the full proposed content) and the
  persisted plan snapshot; it is recomputed BOTH before the server
  accepts an approval and again in the worker at claim time — any
  drift between the approved content and what would execute refuses
  the run dishonesty-free (`digest_mismatch` / `approval_claim_conflict`).
- Approval acceptance requires ALL of: goal in `awaiting_approval`,
  proposal `proposed`, digest match (constant-time), the plan snapshot
  hash unchanged, and the CURRENT filesystem still matching the
  approved before-state (edit: file hash equal; create: path absent).
  Anything else refuses (`403/409`) with the target left untouched.
- Execution uses the tool's own atomicity: writes go through dirfd
  `O_NOFOLLOW` parent walking, create is `O_CREAT|O_EXCL` (existing
  targets are refused), edit re-checks the before-hash at run time,
  writes a temp file plus atomic rename, keeps a pre-change backup
  under `<ws>/.jarvis/backup/` (backup failure aborts the write), and
  the independent verifier re-opens the file and re-hashes the bytes.
- Restart safety: `recover()` never touches `awaiting_approval` state
  or pending write steps; an interrupted post-claim execution leaves a
  `running` non-idempotent step, which recovery reports as
  `reconciliation_required` — the outcome is never repeated or
  fabricated. A restart can never "upgrade" an unapproved proposal
  (only scanned approval states resume execution).

## Tools

Registry entries: `{name, description, schema (JSON-schema-ish
property map), permission ∈ {"autonomous", "approval_required"},
idempotent: bool, run(args) → typed result,
verify(args, result) → {"passed"|"failed", detail?}}`.

`workspace.list_files` (autonomous, 8B): read-only listing under the
configured workspace root (`JARVIS_WORKSPACE_ROOT`, default = the
JARVIS project root — explicit, and never the user's home). Path
resolution via `Path.resolve()` + containment check; symlinks resolving
outside the root are skipped; `.git`, `.env*`, `.pem`, `.key`,
credential/secret-named entries excluded; depth ≤ 8, entries ≤ 1000,
path-strings ≤ 400 chars, serialized output ≤ 64 KB; honest
`truncated` flag; returns relative paths only; never reads file
contents; deterministic and idempotent.

`workspace.create_file` / `workspace.edit_file` (approval_required,
10A): bounded TEXT writes inside the workspace root only.
`approve_required` means: modeled + externally reviewed plan → then
one explicit human approval of the exact digest → then execution (see
the approval section). Bounds and defenses: content ≤ 20k chars /
60k bytes; directories are never created implicitly (parent must
exist); sensitive-name patterns are refused for writes outright
(credential/secret/key/env styles — stricter than listing); the
`<ws>/.jarvis` meta directory is non-writable by plans; and defense
in depth refuses any write if the workspace root itself resolves
inside JARVIS's own source tree (`self_write_forbidden`) — the
orchestrator can never edit its own code. All path walking uses
dirfd + `O_NOFOLLOW` so symlink components are never followed;
create uses `O_CREAT|O_EXCL`; edit requires the approved
`expected_before_sha256` (TOU-scale protection) and swaps via temp
file + atomic rename after a mandatory backup. Non-atomicity risk
residual to an attacker with local foothold is documented, not hidden:
the executor re-checks every hash before and after writing, and a
failed verification leaves the goal `failed`.

`workspace.read_file` (autonomous, 10B): reads ONE workspace text
file, bounded and read-only. Slicing caps: files > 500 KB get a
structured `too_large` result (real size reported, no misleading
partial text); returned content ≤ 20k chars with an honest
`truncated` flag plus the sha256 and byte length of the FULL file.
Strict UTF-8 decode — binary content is refused (`binary_content`),
never faked. Full read-path defenses of the write tools apply:
dirfd + `O_NOFOLLOW` walking (symlinked components refused), the
sensitive-name patterns (`.env*`, `.pem`/`.key`, credential/secret/
token-named — the same list writes use), the `.jarvis` meta dir, and
the self-root refusal. The verify pass re-opens the file and re-hashes
the CURRENT bytes; a tampered/stale result fails verification. Reads
never modify the target (verified in tests, mtime + content + size).

### Verification recipes (Phase 10B)

Four registered check recipes replace any generic shell idea
(NOT a shell tool; the model only names a guarded target, the recipe
fixes everything else):

- `workspace.check_python_syntax` (verify_readonly,
  idempotent): `python -c` ast.parse of the target via a dirfd-opened
  stdin fd (`pass_fds`); parse only — project code is NOT executed by
  this recipe even for valid files. The target is re-guarded (dirfd +
  `O_NOFOLLOW`) immediately pre-spawn; the residual pre-spawn swap
  window is a documented limitation, distinct from the two recipes
  that exec project code.
- `workspace.check_javascript_syntax` (verify_readonly,
  idempotent): `node --check` (parse-only); missing node → honest
  `unavailable` result, interpreted as "nothing was verified", never
  as pass.
- `workspace.run_project_tests` (approval_required — RUNS project
  controlled code): `[python3, -m, unittest, -v, <module>]` where the
  module is derived from the guarded workspace-relative `.py` path.
  cwd = workspace root. The exact runner+target binding is
  digest-bound in the goal approval; reviewer approval alone never
  authorizes it (type checks at execution confirm kind `execute`
  requires an approved proposal).
- `workspace.inspect_git_diff` (verify_readonly, idempotent):
  bounded `git diff --no-pager --no-textconv --no-ext-diff` of the
  whole workspace (file-scoped targets refused); child env strips
  everything but PATH (no secrets, no proxies), sets
  `PYTHONDONTWRITEBYTECODE=1`, `HOME=<workspace>` and defaults
  `GIT_CONFIG_GLOBAL/SYSTEM=/dev/null` so repo/user config diff
  drivers can't hijack the run; diff sections of sensitive-named
  files are dropped from output (flagged honestly via `truncated`).

Recipe records carry `{"recipe", "target", "cwd", "status" ∈
{passed, failed, unavailable, timed_out}, "exit_code", "stdout",
"stderr", "duration_seconds", "timeout_seconds"(30), "truncated"}`
with per-stream output clamped to 6000 chars; one child process at a
time per process (BoundedSemaphore(1)); `shell=False` argv only; a
post-verify re-hash keeps the record honest vs a swapped file.

Semantics (`recipe_semantics`): a recipe status `passed` (exit 0)
counts as "a check declared what it checks passes" in the step
verification record — honest failure is a REPAIR input; `unavailable`
/`timed_out` means "nothing was verified" and blocks the goal (no
silent pass, fail, or retry).

### Verification-driven repair (Phase 10B)

When a recipe honestly reports a failing check on a `records_check`
tool step: the failed attempt is persisted in `last_failure_json`
(with ≤ 1200-char diagnosis tails), events record
`verification_failed` + `repair_started`, the goal transitions
back to `planning`, and the next planner call is seeded with a
bounded (≤ 2200 chars) context of the recorded failure.
`max_repair_cycles = 2` per goal; budget exhausted → goal blocked
`repair_budget_exhausted`. Plan replacement (supersede) drops the
old goal's proposals (`proposals_superseded` event); fresh digest-
bound approvals are required for the replacement plan. Known
limitation: the `execute` digest binds runner+target but NOT the test
file bytes, so an approved run digest stays valid across repair
rounds within the 24h window even though the file changed — the
human approves the same test binding, the diagnostics prove what ran
through it.

## Execution engine & worker

One daemon thread per HTTP server (`create_server` starts it; joined in
`server_close()` via explicit `stop_worker()` — test teardown is clean).
Bounded poll: `Event.wait(0.5)` between scans; no unbounded thread
fan-out (single-threaded executor, one goal at a time).
`process_once(goal_id)` is the testable unit: planning → local safety
→ (execute steps in seq order, checking `cancel_requested` before every
step) → verify → finalize with report.

Restart recovery (startup pass, persisted state is the only truth):
- `planning` with no persisted plan → re-plan (deterministic, no side
  effects).
- Steps `running`: tool idempotent → reset to `pending` and re-run;
  tool NOT idempotent → goal `blocked` (`reconciliation_required`) —
  uncertain outcomes are REPORTED, never fabricated or repeated.
- `done`/`failed`/verified steps are never re-run (`attempts`, bounded
  by 2 per step; total goal wall-time budget via per-step timeouts from
  the provider config).

## Failure propagation

Tool exception / bounds violation → step `failed` (+ safe code) → step
results recorded → goal `executing → failed` with `error_code` and
report. Verification failure → step `failed`, goal `failed`
(`verification_failed`). Unknown tool in a plan → `blocked`
(`unsupported_capability`) at validation time, before any execution.
Anything uncertain → `blocked` with the actionable code surfaced in the
API/UI.

## API surface (Phase 8B)

- `POST /api/goals` `{≤ 500-char objective}` → **202**
  `{id, status:"queued"}`; rate `goals` 10/60 s; never blocks on the
  worker.
- `GET /api/goals?limit=≤ 50` → `{goals[], truncated}` newest-first.
- `GET /api/goals/{id}` → goal + plan + ordered steps + progress +
  latest events (bounded).
- `POST /api/goals/{id}/cancel` → set `cancel_requested`; immediate
  `cancelled` when state is `queued`.
- `POST /api/goals/{id}/retry` → only `blocked → queued` (not for
  `approval_withdrawn`/`reconciliation_required` — those need a human
  decision or a NEW goal).
- Approval endpoints (Phase 10A):
  `GET  /api/goals/{id}/proposals` → digest-bound write proposals
  (state + hashes; no content).
  `GET  /api/goals/{id}/proposals/{seq}` → the exact preview the human
  approves against (proposed content, current sha/state, bounded
  unified diff).
  `POST /api/goals/{id}/approve` `{seq, digest}` → the single human
  authorization; validates goal state, proposal state, digest (also
  re-derived from persisted rows), plan snapshot, and the current
  filesystem before state; then the proposal is `approved` (409 on
  double decisions or races, 403 on digest mismatch).
  `POST /api/goals/{id}/reject` `{seq}` → the human declines; the goal
  then resolves `blocked: approval_withdrawn` via the worker scan.
  Execution is impossible without an `approved` proposal AND the
  worker's own digest replay at claim time.

Same-origin/loopback/405/404/413/415/rate rules identical to existing
endpoints. Errors use the existing taxonomy. Untrusted goal text and
tool output are rendered as text (`textContent`) in the UI. Secrets and
environment values are never part of goal payloads or logs.

## Frontend contract

`#modal-goals` console (existing modal system): objective input +
submit, bounded recent-goals list with real state chips, per-goal detail
(plan summary, step statuses, verification, honest blocked/failed
reasons, event history), cancel + retry actions — and, Phase 10A, a
"Write approvals" panel whenever proposals exist: per-proposal state,
path, hashes, exact-approval digest, a Preview button that renders the
bounded diff + proposed content against the current file (textContent
only — proposal content is untrusted data and never enters the DOM as
markup), and Approve/Reject actions carrying the digest. Single guarded
polling loop (interval cleared on close, stale-response seq guard,
closed-modal writes ignored). Works across reloads/restarts because
state comes from SQLite only.

## Explicit non-goals (8B, still enforced; 10A rescinded only the
first item, now replaced by approval-gated bounded writes; 10B kept
verification recipe-shaped, still no generic shell)

Arbitrary shell/external-effect tools (verification is recipe-based
and target-guarded via `argv` lists; nothing else is spawnable);
file deletion; autonomous approval; agent spawning; scheduled/cron
execution; cross-tool workflows; live model verification of GLM/DeepSeek
ids (Phase 8A open item); memory integration; writing outside
`JARVIS_WORKSPACE_ROOT` or into JARVIS's own source tree; OS sandboxing
of recipe children (the `run_project_tests` recipe runs with this
process's OS permissions, bound by digest-bound human approval — that
residual is documented, not hidden).

## Phase 13A addendum — read-only Obsidian vault integration

A user-selected Obsidian vault is now a registered capability surface,
not a separate application. The configured vault root
(`vault_path` setting / `JARVIS_VAULT_PATH`; administrative, never a
tool argument) is resolved per call by `vault.VaultService` with the
standard precedence and is the hard security boundary: vault-relative
paths only, resolve-and-contain after normalization, symlink escapes
refused, `.obsidian`/secret-name exclusions, and conservative bounds
(2000 notes, 1 MB/note, depth 8, 20k-char reads). Three tools register
in the ordinary registry — `vault.list_files`, `vault.read_file`,
`vault.search` (all `autonomous`, idempotent, verifier-checked) — and
execute through the same goal loop, budget, reviewer, and success
contract as every other tool. Note contents are untrusted DATA:
evidence matching for the Phase 12D success contract uses tool-result
VALUES only (`planner.result_values_text`), so JSON keys or note text
can never act as permission, approval, or success authority. The FTS5
index (`db.py` schema v8, additive migration) stores minimal metadata
per note plus searchable text; refreshes are explicit, incremental
(mtime+size), transactional (failure preserves the previous index),
and honest about truncation and staleness. Retrieval is lexical; no
vector/embedding memory exists (documented in
`docs/PHASE_13A_OBSIDIAN_INTEGRATION_REPORT.md`).
