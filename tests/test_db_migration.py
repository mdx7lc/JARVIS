"""Phase 3D database migration tests — v1 -> v2 additive tasks table.

Only temporary databases are used here; the user's live database is
never opened for writing.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402

# Snapshot of the v1 schema exactly as the v1 code created it.
_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
  content TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
  ON messages(conversation_id, created_at);
"""

_A, _B, _C = "a" * 32, "b" * 32, "c" * 32
_MSGS = [
    ("m1", _A, "user", "hello"),
    ("m2", _A, "assistant", "hi there"),
    ("m3", _B, "user", "second thread"),
]


def _make_v1_db(path):
    """Create a database in the exact v1 shape with realistic user data."""
    conn = sqlite3.connect(path)
    conn.executescript(_V1_SCHEMA)
    convs = [(_A, "First title", "2026-10-01T10:00:00.000Z",
              "2026-10-01T10:01:00.000Z"),
             (_B, "Second title", "2026-10-02T09:00:00.000Z",
              "2026-10-02T09:05:00.000Z")]
    conn.executemany(
        "INSERT INTO conversations(id,title,created_at,updated_at) "
        "VALUES (?,?,?,?)", convs)
    conn.executemany(
        "INSERT INTO messages(id,conversation_id,role,content,created_at) "
        "VALUES (?,?,?,?,?)",
        [(mid, cid, role, content, "2026-10-03T12:00:00.000Z")
         for mid, cid, role, content in _MSGS])
    conn.execute("INSERT INTO meta(key,value) VALUES ('schema_version','1')")
    conn.commit()
    conn.close()


def _snapshot(path):
    """Return every conversation/message row of a database file."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    convs = [dict(r) for r in conn.execute(
        "SELECT id,title,created_at,updated_at FROM conversations "
        "ORDER BY id")]
    msgs = [dict(r) for r in conn.execute(
        "SELECT id,conversation_id,role,content,created_at FROM messages "
        "ORDER BY id")]
    conn.close()
    return convs, msgs


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-migrate-")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "migrating.db"

    def test_fresh_db_initializes_at_current_version(self):
        self.assertEqual(db.init_db(self.db_path), db.SCHEMA_VERSION)
        conn = sqlite3.connect(self.db_path)
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(int(version), db.SCHEMA_VERSION)
        self.assertEqual(user_version, db.SCHEMA_VERSION)
        self.assertIn("tasks", tables)

    def test_v1_migrates_and_preserves_everything(self):
        _make_v1_db(self.db_path)
        before = _snapshot(self.db_path)
        self.assertEqual(db.init_db(self.db_path), db.SCHEMA_VERSION)
        after = _snapshot(self.db_path)
        self.assertEqual(before, after)  # not one row changed
        conn = sqlite3.connect(self.db_path)
        version = int(conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0])
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
        conn.close()
        self.assertEqual(version, db.SCHEMA_VERSION)
        self.assertEqual(user_version, db.SCHEMA_VERSION)
        self.assertIn("idx_tasks_updated", indexes)
        self.assertEqual(
            cols,
            ["id", "title", "description", "status", "priority",
             "time_label", "day", "created_at", "updated_at"],
        )

    def test_v1_server_functions_work_after_migration(self):
        _make_v1_db(self.db_path)
        db.init_db(self.db_path)
        self.assertTrue(db.check_ready(self.db_path)[0])
        conn = db.open_conn(self.db_path)
        tasks = db.list_tasks(conn, 50)
        self.assertEqual(tasks, [])
        created = db.create_task(conn, title="After migration",
                                 time_label="14:00")
        self.assertEqual(
            [t["id"] for t in db.list_tasks(conn, 50)], [created["id"]])
        conn.close()
        _, messages_after = _snapshot(self.db_path)
        self.assertEqual(len(messages_after), len(_MSGS))

    def test_repeated_init_is_idempotent(self):
        db.init_db(self.db_path)
        conn = db.open_conn(self.db_path)
        t1 = db.create_task(conn, title="Keep me")
        conn.close()
        db.init_db(self.db_path)
        db.init_db(self.db_path)
        conn = db.open_conn(self.db_path, readonly=True)
        tasks = db.list_tasks(conn, 50)
        conn.close()
        self.assertEqual([t["id"] for t in tasks], [t1["id"]])

    def test_tasks_survive_close_and_reopen(self):
        db.init_db(self.db_path)
        conn = db.open_conn(self.db_path)
        created = db.create_task(conn, title="Durable", status="done")
        db.update_task(conn, created["id"], {"title": "Durable 2"})
        listed = db.list_tasks(conn, 50)
        conn.close()
        conn = db.open_conn(self.db_path)  # fresh connection
        again = db.list_tasks(conn, 50)
        self.assertEqual(
            [{k: t[k] for k in ("id", "title", "status")} for t in again],
            [{k: t[k] for k in ("id", "title", "status")} for t in listed],
        )
        self.assertEqual(again[0]["title"], "Durable 2")
        conn.close()

    def test_failed_migration_rolls_back_completely(self):
        _make_v1_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO meta(key,value) VALUES ('probe','x')")
        conn.commit()
        conn.close()
        original = dict(db._MIGRATIONS)
        try:
            db._MIGRATIONS[1] = [
                "CREATE TABLE tasks_ok (id TEXT PRIMARY KEY)",
                "this statement is not valid sql",
            ]
            with self.assertRaises(sqlite3.OperationalError):
                db.init_db(self.db_path)
        finally:
            db._MIGRATIONS.clear()
            db._MIGRATIONS.update(original)
        # version stays 1 and nothing from the migration persisted
        conn = sqlite3.connect(self.db_path)
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertEqual(version, "1")
        self.assertNotIn("tasks_ok", tables)
        self.assertNotIn("tasks", tables)
        convs, msgs = _snapshot(self.db_path)
        self.assertEqual(len(convs), 2)
        self.assertEqual(len(msgs), 3)

    def test_database_upgrades_from_beyond_are_refused(self):
        _make_v1_db(self.db_path)
        db.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with self.assertRaises(db.SchemaError):
            db.init_db(self.db_path)

    def test_constraints_are_enforced(self):
        db.init_db(self.db_path)
        conn = db.open_conn(self.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tasks(id,title,status) VALUES (?,?,?)",
                (_C, "x" * 201, "queued"))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tasks(id,title,status) VALUES (?,?,?)",
                (newid(), "ok", "alien-status"))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tasks(id,title,day) VALUES (?,?,?)",
                (newid(), "ok", "yesterday"))
        conn.close()


def newid():
    import uuid
    return uuid.uuid4().hex


if __name__ == "__main__":
    unittest.main()
