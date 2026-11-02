"""SQLite persistence for JARVIS.

Schema is versioned via a single-row `meta` table (and PRAGMA user_version).
v1: conversations and messages. v2: additive `tasks` table (Phase 3D).
v3: additive orchestration tables — goals, goal_steps, goal_events
(Phase 8B). v4 (Phase 10A): additive `goal_approvals` (digest-bound
write-approval records); the `goals` status CHECK gains the state
`awaiting_approval` and the `goal_steps` args cap is raised for write
payloads — those two constraints are enforced by table definitions, so
the two tables are rebuilt row-by-row inside the same explicit
transaction as every other migration. Migrations run inside one
transaction, so a failed migration leaves the previous schema
untouched. No AI memory, tools, or model communication lives here.
"""

import json
import sqlite3
import uuid
from pathlib import Path

SCHEMA_VERSION = 12

_TASK_STATUSES = ("queued", "warn", "done", "planned")
_TASK_PRIORITIES = ("high", "std")
_TASK_DAYS = ("today", "tomorrow")

# Goal lifecycle states (Phase 8B; enforced again in goalstate.py —
# the DB CHECK is a last-ditch integrity net, the state machine is the
#authority).
_GOAL_STATUSES = (
    "queued", "planning", "ready", "executing", "awaiting_approval",
    "verifying", "succeeded", "blocked", "failed", "cancelled",
)
_GOAL_STEP_STATUSES = ("pending", "running", "done", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
  id         TEXT PRIMARY KEY,
  title      TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS messages (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
  content         TEXT NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
  ON messages(conversation_id, created_at);
"""

# Additive migrations keyed by the version they upgrade FROM.
# MIGRATION 1 (v1 -> v2, Phase 3D): add the tasks table. Additive only —
# existing conversations/messages/meta rows are never rewritten. The
# statements are idempotent but run inside one explicit transaction with
# the version stamp, so a failed migration rolls back completely.
# MIGRATION 2 (v2 -> v3, Phase 8B): durable orchestration state — goals,
# goal_steps (bounded plan holds), goal_events (bounded operational
# history). JSON payloads are TEXT columns with size CHECKs; lists are
# stored as JSON arrays of small integers/strings, read back with
# json.loads on trusted, size-checked data.
# (Updated in Phase 10A for fresh databases: goals statuses include the
# approval state and goal_steps carries the raised args cap — migration
# 3 upgrades databases that were created with the original v3 shapes.)
_MIGRATIONS = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS tasks (
          id          TEXT PRIMARY KEY,
          title       TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
          description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
          status      TEXT NOT NULL CHECK (status IN ('queued','warn','done','planned')),
          priority    TEXT NOT NULL DEFAULT 'std' CHECK (priority IN ('high','std')),
          time_label  TEXT NOT NULL DEFAULT '' CHECK (length(time_label) <= 20),
          day         TEXT NOT NULL DEFAULT 'today' CHECK (day IN ('today','tomorrow')),
          created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at, id)",
    ],
    2: [
        """
        CREATE TABLE IF NOT EXISTS goals (
          id              TEXT PRIMARY KEY,
          objective       TEXT NOT NULL CHECK (length(objective) BETWEEN 1 AND 500),
          status          TEXT NOT NULL CHECK (status IN (
                            'queued','planning','ready','executing',
                            'awaiting_approval','verifying',
                            'succeeded','blocked','failed','cancelled')),
          plan_json       TEXT CHECK (plan_json IS NULL OR length(plan_json) <= 32000),
          report_json     TEXT CHECK (report_json IS NULL OR length(report_json) <= 32000),
          error_code      TEXT CHECK (error_code IS NULL OR length(error_code) <= 64),
          cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
          repair_count    INTEGER NOT NULL DEFAULT 0 CHECK (repair_count BETWEEN 0 AND 9),
          last_failure_json TEXT CHECK (last_failure_json IS NULL
                             OR length(last_failure_json) <= 4000),
          created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_goals_order "
        "ON goals(updated_at DESC, id DESC)",
        """
        CREATE TABLE IF NOT EXISTS goal_steps (
          id          TEXT PRIMARY KEY,
          goal_id     TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
          seq         INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 12),
          purpose     TEXT NOT NULL DEFAULT '' CHECK (length(purpose) <= 300),
          tool_name   TEXT NOT NULL DEFAULT '' CHECK (length(tool_name) <= 64),
          args_json   TEXT NOT NULL DEFAULT '{}' CHECK (length(args_json) <= 65536),
          depends_on  TEXT NOT NULL DEFAULT '[]' CHECK (length(depends_on) <= 128),
          acceptance  TEXT NOT NULL DEFAULT '' CHECK (length(acceptance) <= 300),
          status      TEXT NOT NULL CHECK (status IN
                            ('pending','running','done','failed')),
          attempts    INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 9),
          result_json TEXT CHECK (result_json IS NULL OR length(result_json) <= 65000),
          verification TEXT CHECK (verification IS NULL OR length(verification) <= 300),
          error_code  TEXT CHECK (error_code IS NULL OR length(error_code) <= 64),
          started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          finished_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          UNIQUE (goal_id, seq)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_goal_steps_goal "
        "ON goal_steps(goal_id, seq)",
        """
        CREATE TABLE IF NOT EXISTS goal_events (
          id      INTEGER PRIMARY KEY AUTOINCREMENT,
          goal_id TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
          event   TEXT NOT NULL CHECK (length(event) <= 40),
          detail  TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 300),
          ts      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_goal_events_goal "
        "ON goal_events(goal_id, id)",
    ],
    # MIGRATION 3 (v3 -> v4, Phase 10A): digest-bound human write
    # approvals (additive) plus the two table rebuilds that carry the
    # CHECK-constraint changes described in the header. The rebuilds
    # copy every existing row unchanged and run in the same single
    # transaction as the stamp; a failure leaves the v3 schema intact.
    3: [
        # goal_approvals: one row per proposal at first; exactly one
        # per (goal, seq) — the proposal is created once with the goal.
        """
        CREATE TABLE IF NOT EXISTS goal_approvals (
          id              TEXT PRIMARY KEY,
          goal_id         TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
          seq             INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 12),
          tool_name       TEXT NOT NULL CHECK (length(tool_name) <= 64),
          path            TEXT NOT NULL CHECK (length(path) <= 220),
          kind            TEXT NOT NULL CHECK (kind IN ('create','edit',
                            'execute')),
          before_sha256   TEXT CHECK (before_sha256 IS NULL OR length(before_sha256) = 64),
          after_sha256    TEXT NOT NULL CHECK (length(after_sha256) = 64),
          bytes           INTEGER NOT NULL CHECK (bytes BETWEEN 0 AND 60000),
          plan_sha256     TEXT NOT NULL CHECK (length(plan_sha256) = 64),
          digest          TEXT NOT NULL CHECK (length(digest) = 64),
          state           TEXT NOT NULL CHECK (state IN
                            ('proposed','approved','claimed','executed',
                             'rejected')),
          created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          approved_at     TEXT,
          claimed_at      TEXT,
          executed_at     TEXT,
          executed_sha256 TEXT CHECK (executed_sha256 IS NULL OR length(executed_sha256) = 64),
          UNIQUE (goal_id, seq)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_goal_approvals_goal "
        "ON goal_approvals(goal_id, seq)",
        # goals rebuild: status CHECK gains 'awaiting_approval'.
        """
        CREATE TABLE IF NOT EXISTS goals_v4_rebuild (
          id              TEXT PRIMARY KEY,
          objective       TEXT NOT NULL CHECK (length(objective) BETWEEN 1 AND 500),
          status          TEXT NOT NULL CHECK (status IN (
                            'queued','planning','ready','executing',
                            'awaiting_approval','verifying',
                            'succeeded','blocked','failed','cancelled')),
          plan_json       TEXT CHECK (plan_json IS NULL OR length(plan_json) <= 32000),
          report_json     TEXT CHECK (report_json IS NULL OR length(report_json) <= 32000),
          error_code      TEXT CHECK (error_code IS NULL OR length(error_code) <= 64),
          cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
          repair_count    INTEGER NOT NULL DEFAULT 0 CHECK (repair_count BETWEEN 0 AND 9),
          last_failure_json TEXT CHECK (last_failure_json IS NULL
                             OR length(last_failure_json) <= 4000),
          created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """,
        """
        INSERT INTO goals_v4_rebuild
          (id, objective, status, plan_json, report_json, error_code,
           cancel_requested, created_at, updated_at)
        SELECT id, objective, status, plan_json, report_json, error_code,
               cancel_requested, created_at, updated_at FROM goals
        """,
        "DROP TABLE goals",
        "ALTER TABLE goals_v4_rebuild RENAME TO goals",
        "CREATE INDEX IF NOT EXISTS idx_goals_order "
        "ON goals(updated_at DESC, id DESC)",
        # goal_steps rebuild: args cap raised for write payloads.
        """
        CREATE TABLE IF NOT EXISTS goal_steps_v4_rebuild (
          id          TEXT PRIMARY KEY,
          goal_id     TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
          seq         INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 12),
          purpose     TEXT NOT NULL DEFAULT '' CHECK (length(purpose) <= 300),
          tool_name   TEXT NOT NULL DEFAULT '' CHECK (length(tool_name) <= 64),
          args_json   TEXT NOT NULL DEFAULT '{}' CHECK (length(args_json) <= 65536),
          depends_on  TEXT NOT NULL DEFAULT '[]' CHECK (length(depends_on) <= 128),
          acceptance  TEXT NOT NULL DEFAULT '' CHECK (length(acceptance) <= 300),
          status      TEXT NOT NULL CHECK (status IN
                            ('pending','running','done','failed')),
          attempts    INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 9),
          result_json TEXT CHECK (result_json IS NULL OR length(result_json) <= 65000),
          verification TEXT CHECK (verification IS NULL OR length(verification) <= 300),
          error_code  TEXT CHECK (error_code IS NULL OR length(error_code) <= 64),
          started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          finished_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          UNIQUE (goal_id, seq)
        )
        """,
        """
        INSERT INTO goal_steps_v4_rebuild
          (id, goal_id, seq, purpose, tool_name, args_json, depends_on,
           acceptance, status, attempts, result_json, verification,
           error_code, started_at, finished_at)
        SELECT id, goal_id, seq, purpose, tool_name, args_json,
               depends_on, acceptance, status, attempts, result_json,
               verification, error_code, started_at, finished_at
        FROM goal_steps
        """,
        "DROP TABLE goal_steps",
        "ALTER TABLE goal_steps_v4_rebuild RENAME TO goal_steps",
        "CREATE INDEX IF NOT EXISTS idx_goal_steps_goal "
        "ON goal_steps(goal_id, seq)",
    ],
    # MIGRATION 4 (v4 -> v5, Phase 10B): verification-driven repair
    # bookkeeping on goals (repair_count + last_failure_json, added with
    # defaults) plus the goal_approvals rebuild that widens `kind` to
    # include 'execute' (the digest-bound approval for executable
    # verification recipes such as workspace.run_project_tests).
    # Same FK-off rebuild dance as migration 3: rows are copied unchanged
    # (new columns take their defaults) inside one transaction.
    4: [
        """
        CREATE TABLE IF NOT EXISTS goals_v5_rebuild (
          id              TEXT PRIMARY KEY,
          objective       TEXT NOT NULL CHECK (length(objective) BETWEEN 1 AND 500),
          status          TEXT NOT NULL CHECK (status IN (
                            'queued','planning','ready','executing',
                            'awaiting_approval','verifying',
                            'succeeded','blocked','failed','cancelled')),
          plan_json       TEXT CHECK (plan_json IS NULL OR length(plan_json) <= 32000),
          report_json     TEXT CHECK (report_json IS NULL OR length(report_json) <= 32000),
          error_code      TEXT CHECK (error_code IS NULL OR length(error_code) <= 64),
          cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
          repair_count    INTEGER NOT NULL DEFAULT 0 CHECK (repair_count BETWEEN 0 AND 9),
          last_failure_json TEXT CHECK (last_failure_json IS NULL
                             OR length(last_failure_json) <= 4000),
          created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """,
        """
        INSERT INTO goals_v5_rebuild
          (id, objective, status, plan_json, report_json, error_code,
           cancel_requested, repair_count, last_failure_json,
           created_at, updated_at)
        SELECT id, objective, status, plan_json, report_json, error_code,
               cancel_requested, 0, NULL, created_at, updated_at FROM goals
        """,
        "DROP TABLE goals",
        "ALTER TABLE goals_v5_rebuild RENAME TO goals",
        "CREATE INDEX IF NOT EXISTS idx_goals_order "
        "ON goals(updated_at DESC, id DESC)",
        # goal_approvals: kind CHECK widens to accept 'execute'.
        """
        CREATE TABLE IF NOT EXISTS goal_approvals_v5_rebuild (
          id              TEXT PRIMARY KEY,
          goal_id         TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
          seq             INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 12),
          tool_name       TEXT NOT NULL CHECK (length(tool_name) <= 64),
          path            TEXT NOT NULL CHECK (length(path) <= 220),
          kind            TEXT NOT NULL CHECK (kind IN ('create','edit',
                            'execute')),
          before_sha256   TEXT CHECK (before_sha256 IS NULL OR length(before_sha256) = 64),
          after_sha256    TEXT NOT NULL CHECK (length(after_sha256) = 64),
          bytes           INTEGER NOT NULL CHECK (bytes BETWEEN 0 AND 60000),
          plan_sha256     TEXT NOT NULL CHECK (length(plan_sha256) = 64),
          digest          TEXT NOT NULL CHECK (length(digest) = 64),
          state           TEXT NOT NULL CHECK (state IN
                            ('proposed','approved','claimed','executed',
                             'rejected')),
          created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          approved_at     TEXT,
          claimed_at      TEXT,
          executed_at     TEXT,
          executed_sha256 TEXT CHECK (executed_sha256 IS NULL OR length(executed_sha256) = 64),
          UNIQUE (goal_id, seq)
        )
        """,
        """
        INSERT INTO goal_approvals_v5_rebuild
          (id, goal_id, seq, tool_name, path, kind, before_sha256,
           after_sha256, bytes, plan_sha256, digest, state, created_at,
           approved_at, claimed_at, executed_at, executed_sha256)
        SELECT id, goal_id, seq, tool_name, path, kind, before_sha256,
               after_sha256, bytes, plan_sha256, digest, state,
               COALESCE(created_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')),
               approved_at, claimed_at, executed_at, executed_sha256
        FROM goal_approvals
        """,
        "DROP TABLE goal_approvals",
        "ALTER TABLE goal_approvals_v5_rebuild RENAME TO goal_approvals",
        "CREATE INDEX IF NOT EXISTS idx_goal_approvals_goal "
        "ON goal_approvals(goal_id, seq)",
    ],
    # MIGRATION 5 (v5 -> v6, Phase 12B): opt-in dynamic goal mode. One
    # additive column with a safe default — existing goals are and stay
    # 'sequential'; no table rebuild needed (no CHECK constrains mode
    # beyond the DEFAULT; validation lives in create_goal/the server).
    5: [
        "ALTER TABLE goals ADD COLUMN mode TEXT NOT NULL"
        " DEFAULT 'sequential'",
    ],
    # MIGRATION 6 (v6 -> v7, Phase 12D): durable invalid-decision
    # accounting for dynamic goals. Lives on the goal row — never in the
    # retention-bounded goal_events — so event churn or a restart can
    # never reset the budget. Additive column with a safe default.
    6: [
        "ALTER TABLE goals ADD COLUMN dynamic_invalid_count INTEGER"
        " NOT NULL DEFAULT 0 CHECK (dynamic_invalid_count BETWEEN 0"
        " AND 9)",
    ],
    # MIGRATION 7 (v7 -> v8, Phase 13A): read-only Obsidian vault
    # index. vault_notes holds one row of MINIMAL metadata per indexed
    # note (never full contents); vault_fts (FTS5) holds the searchable
    # title/body; vault_index_state is the single-row scan record
    # (status, note count, freshness). All additive; existing tables
    # are untouched. FTS5 is part of the linked SQLite (verified at
    # Phase 13A planning: sqlite3 3.45.1 builds it by default).
    7: [
        """
        CREATE TABLE IF NOT EXISTS vault_notes (
          rel_path TEXT PRIMARY KEY
                     CHECK (length(rel_path) BETWEEN 1 AND 400),
          title    TEXT NOT NULL DEFAULT '' CHECK (length(title) <= 300),
          mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
          size     INTEGER NOT NULL CHECK (size >= 0),
          sha256   TEXT NOT NULL CHECK (length(sha256) = 64),
          links    TEXT CHECK (links IS NULL OR length(links) <= 8000)
        )
        """,
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(
          path UNINDEXED, title, body
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS vault_index_state (
          id            INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
          root          TEXT NOT NULL DEFAULT ''
                         CHECK (length(root) <= 1024),
          last_scan_at  TEXT CHECK (last_scan_at IS NULL
                         OR length(last_scan_at) <= 40),
          status        TEXT CHECK (status IS NULL OR status IN
                         ('never','ok','limited','error')),
          notes         INTEGER NOT NULL DEFAULT 0 CHECK (notes >= 0),
          truncated     INTEGER NOT NULL DEFAULT 0
                         CHECK (truncated IN (0,1)),
          error_code    TEXT CHECK (error_code IS NULL
                         OR length(error_code) <= 64)
        )
        """,
        "INSERT OR IGNORE INTO vault_index_state(id) VALUES (1)",
    ],
    # MIGRATION 8 (v8 -> v9): central permission engine. Policies are
    # operator-written tool-disable rows plus a one-row capability cap
    # in meta (existing table; additive). permission_audit records every
    # engine decision as security audit history. L4 never appears in
    # grant paths: policy level is bounded to 1..3 while audit evidence
    # covers 1..4.
    8: [
        """
        CREATE TABLE IF NOT EXISTS permission_policies (
          tool_name  TEXT PRIMARY KEY
                     CHECK (length(tool_name) BETWEEN 1 AND 80),
          level      INTEGER NOT NULL CHECK (level BETWEEN 1 AND 3),
          state      TEXT NOT NULL CHECK (state = 'disabled'),
          reason     TEXT CHECK (reason IS NULL
                                 OR length(reason) <= 300),
          updated_at TEXT CHECK (updated_at IS NULL
                                 OR length(updated_at) <= 40)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS permission_audit (
          id        INTEGER PRIMARY KEY AUTOINCREMENT,
          ts        TEXT NOT NULL CHECK (length(ts) <= 40),
          source    TEXT NOT NULL CHECK (source IN
                      ('engine','operator_cap','operator_policy')),
          goal_id   TEXT CHECK (goal_id IS NULL
                                OR length(goal_id) <= 64),
          tool_name TEXT NOT NULL
                      CHECK (length(tool_name) BETWEEN 1 AND 80),
          level     INTEGER NOT NULL CHECK (level BETWEEN 1 AND 4),
          decision  TEXT NOT NULL CHECK (decision IN
                      ('allowed','denied','error','policy_changed')),
          reason    TEXT CHECK (reason IS NULL
                                OR length(reason) <= 300)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_permission_audit_ts"
        " ON permission_audit(ts)",
        "INSERT OR IGNORE INTO meta(key, value) VALUES"
        " ('permission_cap', '3')",
    ],
    # MIGRATION 9 (v9 -> v10): local skills registry. One row per
    # discovered skill manifest under an operator-configured directory.
    # Discovery is read-only; enable/disable/trust are operator state.
    # No code execution field exists by design: skills are reference
    # instructions until a future phase binds them to registered tools.
    9: [
        """
        CREATE TABLE IF NOT EXISTS skills (
          id          TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 80),
          name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
          source      TEXT NOT NULL CHECK (length(source) <= 1024),
          version     TEXT CHECK (version IS NULL OR length(version) <= 40),
          description TEXT CHECK (description IS NULL
                                 OR length(description) <= 1000),
          keywords    TEXT CHECK (keywords IS NULL
                                  OR length(keywords) <= 400),
          state       TEXT NOT NULL DEFAULT 'available' CHECK (state IN
                        ('available','enabled','disabled')),
          trust       TEXT NOT NULL DEFAULT 'untrusted' CHECK (trust IN
                        ('untrusted','trusted')),
          scanned_at  TEXT CHECK (scanned_at IS NULL
                                  OR length(scanned_at) <= 40)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_skills_state ON skills(state)",
    ],
    # MIGRATION 10 (v10 -> v11): update management + bounded
    # self-improvement. update_checks is an append-only check history
    # (report-only policy: nothing is ever auto-installed).
    # improvements records the OBSERVE -> PROPOSE -> APPROVE -> APPLY
    # cycle with real evidence; 'apply' only ever creates one task
    # (an L2-safe action) and never edits code or policy.
    10: [
        """
        CREATE TABLE IF NOT EXISTS update_checks (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          ts           TEXT NOT NULL CHECK (length(ts) <= 40),
          source       TEXT NOT NULL DEFAULT '' CHECK (length(source) <= 400),
          current      TEXT NOT NULL CHECK (length(current) <= 40),
          latest       TEXT CHECK (latest IS NULL OR length(latest) <= 40),
          status       TEXT NOT NULL CHECK (status IN
                         ('not_configured','up_to_date','available',
                          'error')),
          detail       TEXT CHECK (detail IS NULL OR length(detail) <= 400)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_update_checks_ts"
        " ON update_checks(ts)",
        """
        CREATE TABLE IF NOT EXISTS improvements (
          id        TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 64),
          created   TEXT NOT NULL CHECK (length(created) <= 40),
          kind      TEXT NOT NULL CHECK (length(kind) <= 60),
          problem   TEXT NOT NULL CHECK (length(problem) <= 400),
          evidence  TEXT NOT NULL DEFAULT '' CHECK (length(evidence) <= 600),
          expected  TEXT NOT NULL DEFAULT '' CHECK (length(expected) <= 400),
          risk      TEXT NOT NULL DEFAULT 'low' CHECK (risk IN
                      ('low','medium')),
          status    TEXT NOT NULL CHECK (status IN
                      ('proposed','approved','applied','rejected')),
          task_id   TEXT CHECK (task_id IS NULL OR length(task_id) <= 64),
          decided   TEXT CHECK (decided IS NULL OR length(decided) <= 40)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_improvements_status"
        " ON improvements(status)",
    ],
    # MIGRATION 11 (v11 -> v12, Phase 14B/14C/14D): real execution and
    # closed-loop knowledge.
    # - skill_runs: bounded execution history for sandboxed executable
    #   skills (operator-initiated, never the server process itself).
    # - improvements gains a verification leg: baseline evidence is
    #   snapshotted at approval; after apply, evaluate() compares fresh
    #   observation and records verify_status/verify_json. Statuses of
    #   the historical cycle stay untouched (history never rewritten).
    # - update_installs: staged/verified/applicable update operations
    #   with rollback bookkeeping. Default policy stays report-only:
    #   an install only ever happens through an explicit operator
    #   action against an explicitly configured target root.
    11: [
        """
        CREATE TABLE IF NOT EXISTS skill_runs (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          skill_id     TEXT NOT NULL CHECK (length(skill_id) BETWEEN 1 AND 80),
          version      TEXT CHECK (version IS NULL OR length(version) <= 40),
          status       TEXT NOT NULL CHECK (status IN
                         ('completed','failed','timeout','refused')),
          exit_code    INTEGER CHECK (exit_code IS NULL OR
                          (exit_code BETWEEN -1 AND 255)),
          args_json    TEXT NOT NULL DEFAULT '{}' CHECK (length(args_json) <= 2048),
          stdout       TEXT CHECK (stdout IS NULL OR length(stdout) <= 8192),
          stderr       TEXT CHECK (stderr IS NULL OR length(stderr) <= 8192),
          error_code   TEXT CHECK (error_code IS NULL OR length(error_code) <= 60),
          verification TEXT CHECK (verification IS NULL
                                   OR length(verification) <= 400),
          duration_ms  INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
          permission   TEXT CHECK (permission IS NULL OR length(permission) <= 200),
          started_at   TEXT NOT NULL CHECK (length(started_at) <= 40),
          finished_at  TEXT NOT NULL CHECK (length(finished_at) <= 40)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_skill_runs_skill"
        " ON skill_runs(skill_id, id)",
        "ALTER TABLE improvements ADD COLUMN baseline_json"
        " TEXT CHECK (baseline_json IS NULL OR length(baseline_json) <= 2000)",
        "ALTER TABLE improvements ADD COLUMN verify_status TEXT"
        " CHECK (verify_status IS NULL OR verify_status IN"
        " ('pending_verification','verified','failed_checked',"
        "  'rolled_back'))",
        "ALTER TABLE improvements ADD COLUMN verify_json TEXT"
        " CHECK (verify_json IS NULL OR length(verify_json) <= 6000)",
        "ALTER TABLE improvements ADD COLUMN verify_at TEXT"
        " CHECK (verify_at IS NULL OR length(verify_at) <= 40)",
        """
        CREATE TABLE IF NOT EXISTS update_installs (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          ts            TEXT NOT NULL CHECK (length(ts) <= 40),
          from_version  TEXT NOT NULL CHECK (length(from_version) <= 40),
          to_version    TEXT NOT NULL CHECK (length(to_version) <= 40),
          manifest_sha  TEXT CHECK (manifest_sha IS NULL
                                    OR length(manifest_sha) <= 64),
          files_json    TEXT NOT NULL DEFAULT '[]'
                        CHECK (length(files_json) <= 20000),
          target_root   TEXT CHECK (target_root IS NULL
                                    OR length(target_root) <= 1024),
          backup_dir    TEXT CHECK (backup_dir IS NULL
                                    OR length(backup_dir) <= 1024),
          status        TEXT NOT NULL CHECK (status IN
                          ('staged','applied','rolled_back')),
          detail        TEXT CHECK (detail IS NULL OR length(detail) <= 400)
        )
        """,
    ],
}


class SchemaError(Exception):
    pass


def open_conn(db_path, *, readonly=False):
    """Open a per-thread connection. Readonly opens never create files."""
    path = Path(db_path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=1")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _guard_add_column(conn, statement):
    """ALTER TABLE .. ADD COLUMN is not idempotent in SQL; a downgraded
    (older-version) database that re-runs a migration must skip columns
    it already carries. Controlled tables only ever ADD single columns,
    so a tiny parse + PRAGMA check keeps every migration re-runnable
    without touching anything else."""
    parts = statement.split()
    if (len(parts) >= 6 and parts[0].upper() == "ALTER"
            and parts[1].upper() == "TABLE"
            and parts[3].upper() == "ADD"
            and parts[4].upper() == "COLUMN"):
        table, column = parts[2], parts[5]
        existing = {r[1] for r in conn.execute(
            f"PRAGMA table_info({table})")}
        if column in existing:
            return "SELECT 1"
    return statement


def init_db(db_path):
    """Create/verify/upgrade the schema. Idempotent; refuses to downgrade.

    Base v1 objects are created first (idempotent, additive, touches no
    user data). Then, inside ONE explicit transaction, any pending
    additive migrations run together with the schema_version stamp, so a
    failure rolls the whole upgrade back completely.
    """
    conn = open_conn(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        # Migration-time FK handling: the v4 table rebuilds DROP the
        # old shaped tables, and cascading FKs would silently delete
        # child rows during that drop. FK checks are disabled for this
        # connection only (outside any transaction — the pragma is a
        # no-op inside one) and restored after the stamp. Every copied
        # row already satisfied the old FKs, so disabling during the
        # copy cannot introduce violations.
        conn.execute("PRAGMA foreign_keys=OFF")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if existing is None:
                from_version = 1
            else:
                from_version = int(existing["value"])
                if from_version > SCHEMA_VERSION:
                    raise SchemaError(
                        f"database schema version {existing['value']} "
                        f"is incompatible with supported version {SCHEMA_VERSION}"
                    )
            # run every missing upgrade, then stamp the version atomically
            for step in range(from_version, SCHEMA_VERSION):
                for statement in _MIGRATIONS.get(step, []):
                    conn.execute(_guard_add_column(conn, statement))
            conn.execute(
                "INSERT INTO meta(key,value) VALUES ('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None or int(row["value"]) != SCHEMA_VERSION:
                raise SchemaError("schema_version was not persisted correctly")
        conn.execute("PRAGMA foreign_keys=ON")
    finally:
        conn.close()
    return SCHEMA_VERSION


def check_ready(db_path):
    """Health probe. Never creates the database; read-only verification."""
    path = Path(db_path)
    if not path.exists():
        return False, "missing"
    try:
        conn = open_conn(path, readonly=True)
    except sqlite3.Error as exc:
        detail = str(exc).split(":", 1)[-1].strip() or "sqlite error"
        return False, detail
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            return False, "uninitialized"
        if int(row["value"]) != SCHEMA_VERSION:
            return False, f"schema mismatch (found {row['value']})"
        conn.execute("SELECT 1 FROM conversations LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        return False, "corrupt: " + str(exc).split("\n")[-1].strip()
    except sqlite3.Error:
        return False, "schema.missing"
    finally:
        conn.close()
    return True, "ready"


def new_id():
    return uuid.uuid4().hex


def create_conversation(conn, title=""):
    conv_id = new_id()
    with conn:
        conn.execute(
            "INSERT INTO conversations(id, title) VALUES (?, ?)",
            (conv_id, title),
        )
    return conv_id


def add_message(conn, conversation_id, role, content):
    with conn:  # transaction: message + parent timestamp update together
        msg_id = new_id()
        conn.execute(
            "INSERT INTO messages(id, conversation_id, role, content) "
            "VALUES (?, ?, ?, ?)",
            (msg_id, conversation_id, role, content),
        )
        conn.execute(
            "UPDATE conversations SET updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id=?",
            (conversation_id,),
        )
    return msg_id


def get_messages(conn, conversation_id):
    return conn.execute(
        "SELECT id, role, content, created_at FROM messages "
        "WHERE conversation_id=? ORDER BY created_at, id",
        (conversation_id,),
    ).fetchall()


def conversation_exists(conn, conversation_id):
    row = conn.execute(
        "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    return row is not None


def get_recent_messages(conn, conversation_id, limit):
    """Last `limit` messages of one conversation, in chronological order."""
    rows = conn.execute(
        "SELECT id, role, content, created_at FROM messages "
        "WHERE conversation_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
        (conversation_id, int(limit)),
    ).fetchall()
    return list(reversed(rows))


def list_conversations(conn, limit):
    """Newest-first conversation page: metadata + derived preview.

    Single statement (correlated subqueries, parameterized SQL-level
    LIMIT). `preview` is derived from the most recent user message and is
    distinct from the stored `title` (first words of the first message).
    """
    return conn.execute(
        """
        SELECT c.id, c.title, c.created_at, c.updated_at,
          (SELECT COUNT(*) FROM messages m
            WHERE m.conversation_id = c.id) AS message_count,
          (SELECT m.content FROM messages m
            WHERE m.conversation_id = c.id AND m.role = 'user'
            ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_user
        FROM conversations c
        ORDER BY c.updated_at DESC, c.id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()


def count_messages(conn, conversation_id):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    return row["n"]


def get_message_window(conn, conversation_id, limit):
    """Bounded most-recent window in chronological order + total count.

    Returns (rows_oldest_first, total_count); rows are a strict trailing
    slice of the full transcript, so callers can report omitted history.
    """
    rows = conn.execute(
        "SELECT id, role, content, created_at FROM messages "
        "WHERE conversation_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
        (conversation_id, int(limit)),
    ).fetchall()
    total = count_messages(conn, conversation_id)
    return list(reversed(rows)), total


# ---- tasks (Phase 3D) --------------------------------------------------

def _task_row_to_dict(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "priority": row["priority"],
        "time": row["time_label"],
        "day": row["day"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_task(conn, *, title, description="", status="queued",
                priority="std", time_label="", day="today"):
    """Insert a task; id/timestamps are server-generated. Returns the row."""
    task_id = new_id()
    with conn:  # one statement; transaction keeps single-row atomicity
        conn.execute(
            "INSERT INTO tasks(id, title, description, status, priority, "
            "time_label, day) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, title, description, status, priority, time_label, day),
        )
    row = conn.execute(
        "SELECT * FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    return _task_row_to_dict(row)


def get_task(conn, task_id):
    row = conn.execute(
        "SELECT * FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    return _task_row_to_dict(row) if row is not None else None


def task_day_counts(conn):
    """Exact per-day totals over ALL tasks (one aggregate query)."""
    counts = dict.fromkeys(("today", "tomorrow"), 0)
    for row in conn.execute(
        "SELECT day, COUNT(*) AS n FROM tasks GROUP BY day"
    ):
        if row["day"] in counts:
            counts[row["day"]] = row["n"]
    return counts


def task_status_counts(conn):
    """Exact per-status totals over ALL tasks (one aggregate query).

    The paged list response can be truncated; these counts are global,
    so callers must never present a bounded page count as a total.
    """
    counts = dict.fromkeys(("queued", "warn", "done", "planned"), 0)
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
    ):
        if row["status"] in counts:
            counts[row["status"]] = row["n"]
    return counts


def list_tasks(conn, limit):
    """Newest-updated tasks, SQL-level LIMIT with stable tie-breaker."""
    return [
        _task_row_to_dict(row)
        for row in conn.execute(
            "SELECT * FROM tasks ORDER BY updated_at DESC, id DESC LIMIT ?",
            (int(limit),),
        )
    ]


_TASK_COLUMNS = {"title": "title", "description": "description",
                 "status": "status"}


def update_task(conn, task_id, fields):
    """Update whitelisted columns + updated_at inside one transaction.

    Returns the updated task, or None when the id is unknown. `fields`
    keys must already be server-validated against _TASK_COLUMNS.
    """
    assignments = [f"{_TASK_COLUMNS[key]}=?" for key in fields]
    assignments.append("updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    params = list(fields.values()) + [task_id]
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET " + ", ".join(assignments) + " WHERE id=?",
            params,
        )
        if cur.rowcount == 0:
            return None
    row = conn.execute(
        "SELECT * FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    return _task_row_to_dict(row) if row is not None else None


def delete_task(conn, task_id):
    """Remove one task durably. Returns True when a row was deleted."""
    with conn:
        cur = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        return cur.rowcount > 0


# ---- search (Phase 5) --------------------------------------------------
# Literal-substring matching: user-entered % and _ are escaped so they
# match themselves; LIKE with ESCAPE '\' keeps every query parameterized.

def like_pattern(query):
    """Lowercase LIKE pattern for a literal substring search."""
    escaped = (query.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_"))
    return f"%{escaped.lower()}%"


def search_tasks(conn, pattern, limit):
    """Literal substring match on task title/description, newest first.

    Returns (rows, truncated_flag): `limit + 1` is fetched to detect
    truncation with one query (no second count).
    """
    rows = conn.execute(
        "SELECT id, title, description, status, day, updated_at "
        "FROM tasks "
        "WHERE title LIKE ? ESCAPE '\\' "
        "   OR description LIKE ? ESCAPE '\\' "
        "ORDER BY updated_at DESC, id DESC LIMIT ?",
        (pattern, pattern, int(limit) + 1),
    ).fetchall()
    truncated = len(rows) > limit
    return [dict(r) for r in rows[:limit]], truncated


def search_conversations(conn, pattern, limit):
    """Match on stored conversation titles, newest first."""
    rows = conn.execute(
        "SELECT id, title, updated_at FROM conversations "
        "WHERE title LIKE ? ESCAPE '\\' "
        "ORDER BY updated_at DESC, id DESC LIMIT ?",
        (pattern, int(limit) + 1),
    ).fetchall()
    truncated = len(rows) > limit
    return [dict(r) for r in rows[:limit]], truncated


def search_messages(conn, pattern, limit):
    """Literal substring match inside persisted message content.

    Joins the owning conversation's title. SQL-level LIMIT only bounds
    the RESULT SIZE — SQLite may still scan the messages table fine
    (no FTS index by design; documented limitation for local scale).
    Snippet extraction happens in Python on the matched rows only.
    """
    rows = conn.execute(
        "SELECT m.id, m.conversation_id, m.content, m.created_at, "
        "       c.title AS conversation_title "
        "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE m.content LIKE ? ESCAPE '\\' "
        "ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
        (pattern, int(limit) + 1),
    ).fetchall()
    truncated = len(rows) > limit
    return [dict(r) for r in rows[:limit]], truncated


# ---- goals (Phase 8B) --------------------------------------------------
# Durable orchestration state. All writes happen inside transactions and
# every lifecycle transition is a conditional UPDATE on the expected
# current status, so the worker and API can never double-apply a change.

MAX_GOAL_EVENTS = 100  # retention cap per goal


def _goal_row_to_dict(row):
    return {
        "id": row["id"],
        "objective": row["objective"],
        "status": row["status"],
        "mode": row["mode"],
        "dynamic_invalid_count": row["dynamic_invalid_count"] or 0,
        "plan_json": row["plan_json"],
        "report_json": row["report_json"],
        "error_code": row["error_code"],
        "cancel_requested": bool(row["cancel_requested"]),
        "repair_count": row["repair_count"] or 0,
        "last_failure_json": row["last_failure_json"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


GOAL_MODES = ("sequential", "dynamic")


def create_goal(conn, objective, status="queued", mode="sequential"):
    if mode not in GOAL_MODES:
        raise ValueError(f"unknown goal mode {mode!r}")
    goal_id = new_id()
    with conn:  # transaction: single-row atomicity
        conn.execute(
            "INSERT INTO goals(id, objective, status, mode)"
            " VALUES (?, ?, ?, ?)",
            (goal_id, objective, status, mode),
        )
    return get_goal(conn, goal_id)


def get_goal(conn, goal_id):
    row = conn.execute(
        "SELECT * FROM goals WHERE id=?", (goal_id,)
    ).fetchone()
    return _goal_row_to_dict(row) if row is not None else None


def update_goal_fields(conn, goal_id, expected_status, fields):
    """Conditional goal update; `fields` keys are server-validated.

    Returns True when exactly one row still matched the expected status
    (that IS the validated transition), else False.
    """
    columns = {
        "status": "status",
        "plan_json": "plan_json",
        "report_json": "report_json",
        "error_code": "error_code",
        "cancel_requested": "cancel_requested",
        "repair_count": "repair_count",
        "dynamic_invalid_count": "dynamic_invalid_count",
        "last_failure_json": "last_failure_json",
    }
    assignments = [f"{columns[key]}=?" for key in fields]
    assignments.append("updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    params = list(fields.values()) + [goal_id]
    with conn:
        cur = conn.execute(
            "UPDATE goals SET " + ", ".join(assignments)
            + " WHERE id=? AND status=?",
            params + [expected_status],
        )
        return cur.rowcount == 1


def step_row_to_dict(row):
    return {
        "id": row["id"],
        "goal_id": row["goal_id"],
        "seq": row["seq"],
        "purpose": row["purpose"],
        "tool_name": row["tool_name"],
        "args_json": row["args_json"],
        "depends_on": json.loads(row["depends_on"]),
        "acceptance": row["acceptance"],
        "status": row["status"],
        "attempts": row["attempts"],
        "result_json": row["result_json"],
        "verification": row["verification"],
        "error_code": row["error_code"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def replace_goal_steps(conn, goal_id, steps):
    """Persist a validated plan as ordered step rows (classmethod of the
    whole step set: any previous rows for the goal are replaced inside
    one transaction, so a goal can never end up with mixed plans)."""
    with conn:
        conn.execute("DELETE FROM goal_steps WHERE goal_id=?", (goal_id,))
        for idx, step in enumerate(steps, start=1):
            conn.execute(
                "INSERT INTO goal_steps("
                "  id, goal_id, seq, purpose, tool_name, args_json,"
                "  depends_on, acceptance, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (
                    new_id(), goal_id, idx,
                    step.get("purpose", ""),
                    step.get("tool", ""),
                    json.dumps(step.get("args", {}), ensure_ascii=True),
                    json.dumps(step.get("depends_on", []),
                               ensure_ascii=True),
                    step.get("acceptance", ""),
                ),
            )


def append_goal_step(conn, goal_id, *, purpose, tool_name, args,
                     acceptance):
    """Dynamic mode (Phase 12B): append ONE validated step with the next
    seq. The seq CHECK (1..12) is the hard schema bound; the worker's
    turn budget stops before that. Returns the inserted step row."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM goal_steps WHERE goal_id=?",
        (goal_id,),
    ).fetchone()
    seq = int(row["m"]) + 1
    with conn:
        conn.execute(
            "INSERT INTO goal_steps("
            "  id, goal_id, seq, purpose, tool_name, args_json,"
            "  depends_on, acceptance, status)"
            " VALUES (?, ?, ?, ?, ?, ?, '[]', ?, 'pending')",
            (
                new_id(), goal_id, seq,
                str(purpose)[:300],
                str(tool_name)[:64],
                json.dumps(args or {}, ensure_ascii=True),
                str(acceptance)[:300],
            ),
        )
    return step_row_to_dict(conn.execute(
        "SELECT * FROM goal_steps WHERE goal_id=? AND seq=?",
        (goal_id, seq),
    ).fetchone())


def get_goal_steps(conn, goal_id):
    return [
        step_row_to_dict(row)
        for row in conn.execute(
            "SELECT * FROM goal_steps WHERE goal_id=? ORDER BY seq",
            (goal_id,),
        )
    ]


def update_goal_step_status(conn, goal_id, seq, expected_status,
                            new_status, fields=None):
    """Conditional step-status transition with server-set timestamps."""
    columns = {
        "attempts": "attempts",
        "result_json": "result_json",
        "verification": "verification",
        "error_code": "error_code",
    }
    assignments = [f"status=?", f"finished_at="
                   f"strftime('%Y-%m-%dT%H:%M:%fZ','now')"]
    params = [new_status]
    for key, value in (fields or {}).items():
        assignments.append(f"{columns[key]}=?")
        params.append(value)
    with conn:
        cur = conn.execute(
            "UPDATE goal_steps SET " + ", ".join(assignments)
            + " WHERE goal_id=? AND seq=? AND status=?",
            params + [goal_id, seq, expected_status],
        )
        return cur.rowcount == 1


def mark_step_running(conn, goal_id, seq):
    """pending → running (+attempt bump, bounded by the planner's limit
    and the worker's retry cap). Returns True on the claimed step."""
    with conn:
        cur = conn.execute(
            "UPDATE goal_steps SET status='running',"
            " attempts=attempts+1,"
            " started_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
            " finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            " WHERE goal_id=? AND seq=? AND status='pending'",
            (goal_id, seq),
        )
        return cur.rowcount == 1


def insert_goal_event(conn, goal_id, event, detail="", limit=MAX_GOAL_EVENTS):
    """Append one bounded event, then retain only the newest `limit`
    rows for the goal — deterministic bounded operational history."""
    with conn:
        conn.execute(
            "INSERT INTO goal_events(goal_id, event, detail) VALUES (?, ?, ?)",
            (goal_id, event, detail[:300]),
        )
        conn.execute(
            "DELETE FROM goal_events WHERE goal_id=? AND id NOT IN ("
            "  SELECT id FROM goal_events WHERE goal_id=?"
            "  ORDER BY id DESC LIMIT ?)",
            (goal_id, goal_id, int(limit)),
        )


def get_goal_events(conn, goal_id, limit=50):
    """Latest `limit` events, chronological (oldest of the window first)."""
    rows = conn.execute(
        "SELECT id, event, detail, ts FROM goal_events WHERE goal_id=?"
        " ORDER BY id DESC LIMIT ?",
        (goal_id, int(limit)),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def list_goals(conn, limit):
    """Newest-first bounded page of goals (SQL-level LIMIT)."""
    return [
        _goal_row_to_dict(row)
        for row in conn.execute(
            "SELECT * FROM goals ORDER BY updated_at DESC, id DESC LIMIT ?",
            (int(limit),),
        )
    ]


def list_actionable_goals(conn):
    """Goals the worker can act on, oldest first (fair FIFO).

    Actionable: queued (start), planning without persisted plan (start or
    re-plan after restart), ready (execute), executing/verifying
    mid-flight states (only reachable after a crash — recovery above
    handles them), awaiting_approval (polled so a fully approved write
    goal resumes promptly), and running-block recovery is handled in
    worker code. Re-picked only if still actionable on the next scan
    after a crash.
    """
    return [
        _goal_row_to_dict(row)
        for row in conn.execute(
            "SELECT * FROM goals WHERE status IN "
            "('queued','planning','ready','executing','verifying',"
            "'awaiting_approval') "
            "ORDER BY created_at, id"
        )
    ]


def goal_progress(steps):
    """Persisted-step truth only: counts, never fabricated percentages."""
    counts = dict.fromkeys(("total", "done", "failed", "running"), 0)
    counts["total"] = len(steps)
    for step in steps:
        if step["status"] in ("done", "failed", "running"):
            counts[step["status"]] += 1
    return counts


# ---- human write-approvals (Phase 10A) ---------------------------------

_APPROVAL_STATES = ("proposed", "approved", "claimed", "executed",
                    "rejected")


def delete_goal_approvals(conn, goal_id):
    """Drop all approval rows for a goal (used when a repair cycle
    supersedes the previous plan's proposals with new digests)."""
    with conn:
        conn.execute(
            "DELETE FROM goal_approvals WHERE goal_id=?", (goal_id,))


def create_goal_approval(
        conn, goal_id, seq, tool_name, kind, path,
        before_sha256, after_sha256, bytes_, plan_sha256, digest):
    """Insert the initial proposal record (state 'proposed')."""
    aid = new_id()
    conn.execute(
        "INSERT INTO goal_approvals "
        "(id, goal_id, seq, tool_name, path, kind, before_sha256,"
        " after_sha256, bytes, plan_sha256, digest, state, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,strftime("
        "'%Y-%m-%dT%H:%M:%fZ','now'))",
        (aid, goal_id, seq, tool_name, path, kind, before_sha256,
         after_sha256, int(bytes_), plan_sha256, digest, "proposed"),
    )
    return aid


def _approval_row_to_dict(row):
    return {
        "id": row["id"],
        "goal_id": row["goal_id"],
        "seq": row["seq"],
        "tool_name": row["tool_name"],
        "path": row["path"],
        "kind": row["kind"],
        "before_sha256": row["before_sha256"],
        "after_sha256": row["after_sha256"],
        "bytes": row["bytes"],
        "plan_sha256": row["plan_sha256"],
        "digest": row["digest"],
        "state": row["state"],
        "created_at": row["created_at"],
        "approved_at": row["approved_at"],
        "claimed_at": row["claimed_at"],
        "executed_at": row["executed_at"],
        "executed_sha256": row["executed_sha256"],
    }


def get_goal_approval(conn, goal_id, seq):
    row = conn.execute(
        "SELECT * FROM goal_approvals WHERE goal_id=? AND seq=?",
        (goal_id, int(seq))
    ).fetchone()
    return _approval_row_to_dict(row) if row else None


def list_goal_approvals(conn, goal_id):
    return [
        _approval_row_to_dict(row)
        for row in conn.execute(
            "SELECT * FROM goal_approvals WHERE goal_id=? ORDER BY seq",
            (goal_id,))
    ]


def update_goal_approval_state(conn, goal_id, seq, expected_state,
                               new_state, extra=None):
    """Conditional approval-state move. Returns True when the row was
    still in `expected_state`; the pair (approve/claim/execute) is
    therefore guaranteed to succeed at most once."""
    fields = {"state": new_state, **(extra or {})}
    assignments = ", ".join(f"{k}=?" for k in fields)
    values = [str(v) if isinstance(v, bool) else v
              for v in fields.values()]
    values.extend([goal_id, int(seq), expected_state])
    with conn:
        cur = conn.execute(
            f"UPDATE goal_approvals SET {assignments} "
            f"WHERE goal_id=? AND seq=? AND state=?",
            values,
        )
        return cur.rowcount == 1
