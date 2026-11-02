# Phase 13A — Native Obsidian Knowledge Integration

Status: **delivered** (2026-10-26). Secure, dependency-free, read-only
Obsidian vault integration inside the existing JARVIS architecture. No
new runtime dependencies (FTS5 is part of the linked SQLite), no model
downloads, no live inference, no vector database, no note writing.

Definition of done, verified: JARVIS can search and read a user-selected
Obsidian vault through registered read-only tools, cite the source of
retrieved information, feed bounded observations into goal execution,
and do so without changing vault files, leaking into the wider
filesystem, or weakening existing approval and verification controls.

---

## 1. Architecture

```
Settings Center (user, administrative)
   └─ vault_path setting ──► settingsstore (atomic, 0600, validated)
                               resolved once at save, re-resolved per use
JARVIS_VAULT_PATH (env) ──► precedence: environment > user store > unset
        │
        ▼
vault.VaultService (new module)  ── one instance per server process
   ├─ root()          per-call resolution (a settings change needs no restart)
   ├─ scan()          incremental FTS5 index refresh (one transaction)
   ├─ search()        lexical FTS5 query with provenance + staleness
   ├─ read_note()     strict containment read with citation metadata
   ├─ list_notes()    bounded live listing
   └─ status()        secret-free connection + index state
        │
        ▼
tools.make_vault_tools(service) ──► registry tools (same permission
   vault.list_files / vault.read_file / vault.search      boundary)
        │
        ▼
worker (dynamic + sequential goals) — observations are DATA;
success contract, reviewer, digest approvals remain authoritative
```

The integration reuses every existing mechanism: the tool registry and
its strict schema validation, the autonomous/verify_readonly/
approval_required permission classes, the per-step verifier
postconditions, the durable goal loop, the settings precedence
(environment > persisted user config > defaults), the additive SQLite
migration system, and the JSON error taxonomy. There is no second
orchestrator, no parallel approval path, and no vault-specific
execution pathway.

## 2. Vault configuration model

- **Administrative only**: `vault_path` is a user setting in the
  existing Settings Center (new "Knowledge" tab) or the
  `JARVIS_VAULT_PATH` environment variable. It is never a tool
  argument; model-generated instructions cannot read or change it
  (tools accept only vault-relative note paths).
- **Validation at configuration time** (server-side, before save):
  absolute path, exists, is a real directory; the RESOLVED absolute
  path is what gets stored (atomic write, 0600, inside the existing
  per-user store). Invalid updates are rejected with a typed error and
  the previous configuration is preserved — never silently switched.
- **Precedence**: environment > persisted user configuration > unset
  (same chain as every other setting). An env-configured vault shows
  as locked in the UI.
- **Honest states**: unconfigured → "Not configured"; configured but
  missing → "missing or unreadable (source: …), the previous
  configuration is kept"; configured + indexed → note count, last scan
  timestamp, limited/partial marker, and "index belongs to a DIFFERENT
  vault — refresh required" when the configured root changed since the
  last scan.
- **Privacy**: the vault is identified by folder NAME in API/UI
  status (`/api/vault` never returns the absolute path); the full path
  appears only in the Settings Center view (the user's own
  administrative surface, consistent with how `workspace_root` is
  already shown there). No credentials or note contents are stored in
  the settings file. Load-time validation deliberately keeps a stored
  path whose directory vanished, so an unavailable vault cannot erase
  the configuration.

## 3. Filesystem security boundary

- Every requested path is vault-relative; absolute paths, `..`
  segments, backslashes, and `~` are refused before any filesystem
  access.
- After normalization the candidate is `Path.resolve()`-d and
  containment-checked against the configured root — symlink escapes
  are refused and never followed (enforced in the indexer walker, the
  reader, and the lister alike).
- Only `.md` files are eligible. Excluded everywhere: `.obsidian`,
  `.git`, `.trash`, `.ssh`, `.gnupg`, `.cache`, `.pki`, `.env*`,
  `*.pem/*.key/*.keystore/*.kdbx/*.p12/*.ovpn/*.htpasswd/*.netrc`, and
  any name containing credential/secret/password/token/api-key
  patterns. Hidden application state is never exposed.
- Bounds: 2000 notes/index, 1 MB/note at indexing, 50 MB aggregate,
  depth 8, read result 20 000 chars, 500 list entries (48 KB
  serialized cap), 20 search hits (snippets ≤ 240 chars), query ≤ 200
  chars. Every bound surfaces an honest truncation/skip count.
- Invalid UTF-8, oversized notes, unreadable files/directories are
  explicit skips/errors — never fabricated results.
- Strictly read-only: no vault file is created, modified, renamed, or
  deleted by any code path (regression-tested with before/after hashes
  of the entire vault tree).

## 4. Database migration (v7 → v8)

Additive, transactional, idempotent (existing conventions):

- `vault_notes`: one minimal metadata row per indexed note
  (`rel_path` PK, `title`, `mtime_ns`, `size`, `sha256`, plus raw
  `links` JSON for a future graph phase). Full note bodies are NOT
  duplicated here.
- `vault_fts` (FTS5): `(path UNINDEXED, title, body)` — the searchable
  text. FTS5 is part of the stdlib SQLite build (verified:
  sqlite3 3.45.1); the migration runs inside the existing single
  transaction, so failure rolls back completely.
- `vault_index_state`: single-row scan record — root, timestamp,
  status (`never|ok|limited|error`), note count, truncation flag,
  error code.

**Indexing strategy**: a refresh walks the configured root collecting
`(size, mtime_ns)` per eligible note, then reads only NEW or CHANGED
notes (hash computed on read); unchanged notes are not re-read. Deleted
notes' rows are removed (they disappear from search). A changed vault
root wipes and rebuilds the index (full re-index policy). The whole
refresh is one transaction: a crash or error leaves the previous valid
index intact — nothing is silently erased.

**Freshness policy**: explicit user refresh (Settings Center button or
`POST /api/vault/refresh`, rate-capped 4/min). There is no background
scanning. Search results are marked `stale: true` when the live file's
`(mtime_ns, size)` no longer matches the indexed entry, so retrieved
information is never silently out of date.

**If the vault is disconnected**: the previous index remains in the
database, `GET /api/vault` reports `vault_exists: false`, and refresh
returns `503 vault_unavailable` honestly.

## 5. API contracts

Same-origin only, loopback-only binding, existing JSON error taxonomy,
no absolute paths or exception text in responses or logs.

| Endpoint | Behavior |
|---|---|
| `GET /api/vault` | `{configured, source, vault_name, vault_exists, index{status, notes, truncated, last_scan_at, root_match, error_code}}` — name-only vault identification |
| `POST /api/vault/refresh` | validates the configured root with the tool rules, runs one incremental scan, returns the bounded summary; `400 vault_not_configured`, `503 vault_unavailable` on failure |
| `PATCH /api/settings` (`vault_path`) | resolves + validates before save; empty string disconnects; env-managed → `409 field_locked` |

The vault section is also exposed inside `GET /api/settings` (the
administrative view) including the configured path.

## 6. Registered goal tools

| Tool | Schema | Permission |
|---|---|---|
| `vault.list_files` | `path?: string ≤200` | autonomous, idempotent |
| `vault.read_file` | `path: string ≤200` | autonomous, idempotent |
| `vault.search` | `query: string ≤200`, `limit?: int 1..20` | autonomous, idempotent |

All three run through the ordinary registry: strict arg validation,
typed `ToolError` codes (`vault_not_configured`, `path_forbidden`,
`invalid_query`, `no_such_note`, `not_a_note`, `binary_content`,
`unreadable_note`, `vault_unavailable`), and independent verifiers —
the read verifier re-reads the note and compares the claimed
sha256/size/truncation; the search verifier re-checks the limit, path
relativity, and snippet bounds; the list verifier re-checks path
relativity and the truncation flag. The model cannot escape the vault
because containment is enforced inside the service, not in the prompt.

**Citation format**: every search hit and read result carries
`path` (vault-relative), `sha256` of the current bytes, `title`, and
`snippet`/bounded content; search hits additionally carry `stale`.
Retrieved note text enters the goal loop as ordinary tool-result JSON —
data in the bounded observation transcript, never instructions.

**Untrusted-content policy**: a note containing "ignore all rules and
execute this command" changes nothing. Permissions come from the
registry (unchanged since Phase 10A), write proposals still require the
digest-bound human approval, and success still requires the persisted
contract's evidence. Regression-tested end-to-end through the real
dynamic loop; the same test caught (and the fix eliminated) a real
evidence-matching leak where JSON *keys* like `"content"` could count
as evidence — evidence matching now uses result **values** only
(`planner.result_values_text`, shared by the Phase 12D checker).

Retrieval is strictly query-driven: nothing reads the vault
automatically for every goal, and no note content is auto-injected
into chat.

## 7. Frontend (existing design system)

Settings Center gains a "Knowledge" tab: vault path input, Set /
Disconnect / Refresh actions, and a truthful status line (configured /
not configured / missing / indexed count / last scan / partial marker /
different-vault warning). Reuses existing field/button/hint/chip
styling and accessible focus behavior; no dashboard redesign, no fake
counts. A Vault note-browser view and a Knowledge Graph UI remain
future work (Phase 11C's original scope; documented in the roadmap).

## 8. Lexical retrieval vs. future semantic memory

Retrieval is lexical FTS5 (bm25 ranking, quoted-token AND queries) over
title + body. **No vector search, embeddings, or semantic similarity is
implemented or claimed.** The interfaces a future semantic layer needs
already exist: `vault_notes.sha256` (stable content identity for
embedding keys), the per-note service boundary (`VaultService.search`
is the only query entry point — swap or augment its implementation
without touching the API or the tool architecture), and
`links` (future graph). A later phase can evaluate embedding methods
against a reproducible quality set; nothing is downloaded or paid for
now.

## 9. Files changed

| File | Change |
|---|---|
| `vault.py` | **new** — VaultService, containment, bounds, incremental scan, FTS5 search, citation reads, status |
| `db.py` | `SCHEMA_VERSION = 8`; additive migration 7 (`vault_notes`, `vault_fts`, `vault_index_state`) |
| `tools.py` | `make_vault_tools` (3 tools + verifiers); `default_registry(workspace_root, vault=None)` |
| `settingsstore.py` | `vault_path` setting (strict save validation, shape-only load validation so a vanished vault keeps its config) |
| `config.py` | `ENV_KEYS["vault_path"] = "JARVIS_VAULT_PATH"` (env lock + precedence) |
| `server.py` | `GET /api/vault`, `POST /api/vault/refresh`, vault settings validation/resolution, settings-view vault section, rate policies, VaultService wiring |
| `planner.py` | `result_values_text` — evidence matching on values only (Phase 12D checker hardened) |
| `worker.py` | `_verify_dynamic_contract` uses values-only evidence text |
| `static/index.html`, `static/app.js` | Knowledge tab (status, set/clear/refresh), renderers + wiring |
| `tests/test_vault.py` | **new** — 41 tests (config, index, search/read, tools, goal integration, migration/restart, API) |
| `tests/test_browser_vault.py` | **new** — 3 browser E2E tests (unconfigured honesty, configure+refresh+truthful status, invalid update preserved) |

`static/jarvis-format.js` untouched (renderer suite proves it); no
runtime dependencies; provider/model configuration untouched.

## 10. Test results (exact)

```
cd tests && PYTHONPATH=/home/mdx/Dev/JARVIS python3 -m unittest test_vault
→ Ran 41 tests — OK

cd tests && PYTHONPATH=/home/mdx/Dev/JARVIS python3 -m unittest \
    test_dynamic_loop test_dynamic_contract test_goals_db test_db_migration
→ Ran 71 tests — OK            (12D behavior verified after the values-only fix)

python3 -m unittest discover -s tests -p "test_*.py"
→ Ran 820 tests in 447 s — OK (skipped=89)   [776/86 pre-phase]

node tests/test_format_render.js
→ 32 format-render tests passed

Browser E2E (Playwright chromium, temp vaults/dbs, fake upstream):
  test_browser_vault      Ran 3  — OK (new)
  test_browser_goals      Ran 13 — OK
  test_browser_write_approvals 4 OK · verify 3 OK · dashboard 9 OK
  chat 14 OK · tasks 7 OK · models 15 OK · settings 9 OK
  search 7 OK · security 5 OK            → 89 browser tests, 0 failures

git diff --check → clean
py_compile (all touched modules) → clean; node --check static/app.js → clean
```

All tests use temporary vaults, temporary databases, and fake
providers. The production `data/jarvis.db` and any real vault were
never touched by tests. Nothing staged or committed; the pre-phase git
status baseline is preserved.

## 11. Security review summary

- Vault path: administrative, env-locked, validated, resolved, never a
  tool argument; traversal/symlink-escape containment verified at
  indexer, reader, lister, and verifier layers.
- Exclusions verified for `.obsidian`, secrets-name patterns; binary
  and oversized files refused.
- Read-only guarantee tested with whole-tree hash snapshots across
  scan + list + read + search.
- Note contents are untrusted data: injection test proves a hostile
  note cannot execute anything, cannot self-approve a write, and
  cannot satisfy contract evidence (values-only matching).
- Rate limits: vault reads 60/min, refresh 4/min; same-origin checks
  on both vault endpoints; no absolute path leakage in `/api/vault`
  (asserted in tests).
- The index is never an authority for permissions, approvals, or goal
  state — it only answers searches.

## 12. Unresolved limitations
1. **Lexical retrieval only** — no semantic/embedding search (§8).
2. **No Vault browsing UI / Knowledge Graph yet** — the note-browser
   view and graph visualization from the original Phase 11C spec are
   deferred to a follow-up phase; the index already stores raw
   `links` per note so no re-read will be needed.
3. **Incremental detection is `(mtime_ns, size)`** — a same-size,
   same-timestamp content change (rare, e.g. some editors) is missed
   until the note is touched again; a full re-index catches it.
4. **`stale` detection** covers the ≤20 returned hits (one `stat` per
   hit), not the whole index.
5. **FTS5 availability is required** for the v8 migration; it is part
   of standard CPython sqlite3 builds (verified locally) but a
   nonstandard build without FTS5 would fail migration startup.
6. **Obsidian-specific metadata** (tags, frontmatter YAML) is stored
   verbatim in the body but not parsed into structured fields yet.

---

## Appendix — Obsidian application installation status (user-managed)

Machine inspection: Linux Mint 22.3 (Zena), x86_64, 44 GB free disk;
no pre-existing Obsidian (no deb/Flatpak/Snap/AppImage found); no
existing vault in `~/Documents`, `~/Notes`, `~/Obsidian` (checked
without a recursive home scan).

- Official package staged: `obsidian_1.14.5_amd64.deb` (108,711,244
  bytes), downloaded over HTTPS directly from the official
  `obsidianmd/obsidian-releases` GitHub release **v1.14.5** (asset of
  the release page linked from obsidian.md/download). Package metadata
  verified (`dpkg-deb -f`: Package obsidian, Version 1.14.5, amd64;
  standard Electron runtime dependencies).
- **Verification limitation (honest):** the v1.14.5 release publishes
  no checksum file or signature (verified across the release assets
  and notes); origin rests on the official HTTPS release source only.
- **Not installed in this session:** the deb requires administrator
  authentication, which is the user's to grant. **The user chose to
  run the installation themselves**; the staged deb remains at
  `/tmp/opencode/obsidian-install/obsidian_1.14.5_amd64.deb` (install
  command: `sudo apt-get install -y <deb>`). JARVIS's integration does
  not depend on the Obsidian app being installed — the vault is read
  as plain Markdown files.
- Vault prepared: `~/Documents/JARVIS-Vault` (created empty; no fake
  notes, no plugins, no Obsidian settings touched). The user decides
  the initial notes and opens it in Obsidian via "Open folder as
  vault". Configuring it in JARVIS afterwards is one Settings Center
  action (Knowledge tab → set path → Refresh index).

Post-install verification checklist for the user: `dpkg -s obsidian`,
`which obsidian`, desktop menu entry, and launching the app once.
