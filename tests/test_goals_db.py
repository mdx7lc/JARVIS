"""Phase 8B database tests: v2 → v3 migration, goal/step/event helpers,
conditional-update semantics, retention bounds."""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db

class MigrationTests(unittest.TestCase):
    """v2 → v3 is additive: existing data must survive untouched."""

    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="jarvis-mig-"))
        self.addCleanup(shutil.rmtree, d, True)
        self.db_path = d / "db.sqlite"

    def _v2_database_with_data(self):
        """Build a v2 database and populate it with Phase 3D-era data."""
        conn = db.open_conn(self.db_path)
        try:
            conn.executescript(db._SCHEMA)
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                for statement in db._MIGRATIONS[1]:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO meta(key,value) "
                    "VALUES ('schema_version','2')")
                conn.execute("PRAGMA user_version=2")
        finally:
            conn.close()
        conn = db.open_conn(self.db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO conversations(id, title) VALUES (?, ?)",
                    ("conv1", "first"))
                conn.execute(
                    "INSERT INTO messages(id, conversation_id, role,"
                    " content) VALUES ('m1','conv1','user','hello')")
                conn.execute(
                    "INSERT INTO tasks(id, title, status, day) VALUES"
                    " ('t1','ship the docs','queued','today')")
        finally:
            conn.close()

    def test_migration_preserves_v2_data(self):
        self._v2_database_with_data()
        version = db.init_db(self.db_path)
        self.assertEqual(version, db.SCHEMA_VERSION)
        conn = db.open_conn(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM conversations"
                             ).fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT content FROM messages WHERE"
                             " id='m1'").fetchone()[0], "hello")
            self.assertEqual(
                conn.execute("SELECT day FROM tasks WHERE id='t1'"
                             ).fetchone()[0], "today")
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='"
                             "schema_version'").fetchone()[0],
                str(db.SCHEMA_VERSION))
            # new tables exist and accept goal rows
            conn.execute(
                "INSERT INTO goals(id, objective, status)"
                             " VALUES ('g0','x','queued')")
            self.assertEqual(
                conn.execute("SELECT count(*) FROM goals"
                             ).fetchone()[0], 1)
        finally:
            conn.close()

    def test_migration_is_idempotent(self):
        db.init_db(self.db_path)
        self.assertEqual(db.init_db(self.db_path), db.SCHEMA_VERSION)

    def test_future_schema_refused_without_touching_data(self):
        db.init_db(self.db_path)
        conn = db.open_conn(self.db_path)
        try:
            with conn:
                conn.execute("UPDATE meta SET value='99' WHERE key='"
                             "schema_version'")
        finally:
            conn.close()
        with self.assertRaises(db.SchemaError):
            db.init_db(self.db_path)
        conn = db.open_conn(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='"
                             "schema_version'").fetchone()[0], "99")
        finally:
            conn.close()

    def test_fresh_init_creates_current_schema(self):
        self.assertEqual(db.init_db(self.db_path), db.SCHEMA_VERSION)
        ok, detail = db.check_ready(self.db_path)
        self.assertTrue(ok)
        self.assertEqual(detail, "ready")
        # v6 (Phase 12B): the dynamic-mode column exists and defaults
        # existing-shaped goals to sequential.
        conn = db.open_conn(self.db_path)
        try:
            columns = {r["name"]: r for r in conn.execute(
                "PRAGMA table_info(goals)").fetchall()}
            self.assertIn("mode", columns)
            self.assertEqual(columns["mode"]["dflt_value"],
                             "'sequential'")
        finally:
            conn.close()

    def test_check_ready_never_creates_file(self):
        missing = self.db_path.parent / "elsewhere.sqlite"
        ok, detail = db.check_ready(missing)
        self.assertFalse(ok)
        self.assertEqual(detail, "missing")
        self.assertFalse(missing.exists())
        # corrupted file is reported, not quarantined or auto-healed
        missing.write_bytes(b"not a database at all")
        ok, detail = db.check_ready(missing)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("corrupt:"))
        self.assertEqual(missing.read_bytes(), b"not a database at all")


class GoalHelperTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="jarvis-gh-"))
        self.addCleanup(shutil.rmtree, d, True)
        self.db_path = d / "db.sqlite"
        db.init_db(self.db_path)
        self.conn = db.open_conn(self.db_path)
        self.addCleanup(self.conn.close)

    def _goal(self, objective="Inventory the workspace"):
        return db.create_goal(self.conn, objective)

    def test_create_and_get_roundtrip(self):
        goal = self._goal()
        self.assertEqual(goal["objective"], goal["objective"])
        self.assertEqual(goal["status"], "queued")
        self.assertFalse(goal["cancel_requested"])
        self.assertIsNone(goal["plan_json"])
        self.assertIsNone(goal["error_code"])
        fetched = db.get_goal(self.conn, goal["id"])
        self.assertEqual(fetched["id"], goal["id"])
        self.assertIsNone(db.get_goal(self.conn, "missing"))

    def test_objective_over_500_rejected_by_schema(self):
        with self.assertRaises(sqlite3.IntegrityError):
            db.create_goal(self.conn, "x" * 501)

    def test_conditional_update_matches_expected_status(self):
        goal = self._goal()
        self.assertTrue(db.update_goal_fields(
            self.conn, goal["id"], "queued",
            {"status": "planning", "error_code": None}))
        self.assertEqual(db.get_goal(self.conn, goal["id"])["status"],
                         "planning")
        # stale expectation (queued) no longer matches: rowcount 0
        self.assertFalse(db.update_goal_fields(
            self.conn, goal["id"], "queued", {"status": "cancelled"}))

    def test_update_never_overwrites_protected_columns(self):
        goal = self._goal()
        with self.assertRaises(KeyError):
            db.update_goal_fields(
                self.conn, goal["id"], "queued",
                {"objective": "smuggled"})
        self.assertEqual(db.get_goal(self.conn, goal["id"])["objective"],
                         "Inventory the workspace")

    def test_cancel_flag_roundtrip(self):
        goal = self._goal()
        db.update_goal_fields(self.conn, goal["id"], "queued",
                              {"cancel_requested": 1})
        self.assertTrue(
            db.get_goal(self.conn, goal["id"])["cancel_requested"])

    def test_step_sets_replaced_atomically(self):
        goal = self._goal()
        db.update_goal_fields(self.conn, goal["id"], "queued",
                              {"status": "planning"})
        steps = [
            {"purpose": "step one", "tool": "workspace.list_files",
             "args": {"path": ""}, "depends_on": [],
             "acceptance": "files listed"},
            {"purpose": "step two", "tool": "workspace.list_files",
             "args": {"path": "src"}, "depends_on": [1],
             "acceptance": "src listed"},
        ]
        db.replace_goal_steps(self.conn, goal["id"], steps)
        rows = db.get_goal_steps(self.conn, goal["id"])
        self.assertEqual([r["seq"] for r in rows], [1, 2])
        self.assertEqual([r["status"] for r in rows], ["pending",
                                                       "pending"])
        self.assertEqual(rows[1]["depends_on"], [1])
        self.assertEqual(rows[0]["args_json"], '{"path": ""}')
        # a re-planning replaces the whole set under one transaction
        db.replace_goal_steps(self.conn, goal["id"], steps[:1])
        rows = db.get_goal_steps(self.conn, goal["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["seq"], 1)

    def test_step_status_conditional_transitions(self):
        goal = self._goal()
        db.replace_goal_steps(self.conn, goal["id"], [
            {"purpose": "s", "tool": "workspace.list_files",
             "args": {}, "depends_on": [], "acceptance": "done"}])
        self.assertTrue(db.mark_step_running(self.conn, goal["id"], 1))
        step = db.get_goal_steps(self.conn, goal["id"])[0]
        self.assertEqual(step["status"], "running")
        self.assertEqual(step["attempts"], 1)
        # claiming an already-running step fails (double-run guard)
        self.assertFalse(db.mark_step_running(self.conn, goal["id"], 1))
        self.assertTrue(db.update_goal_step_status(
            self.conn, goal["id"], 1, "running", "done",
            {"result_json": '{"tool": "workspace.list_files"}',
             "verification": "1 entries"}))
        step = db.get_goal_steps(self.conn, goal["id"])[0]
        self.assertEqual(step["status"], "done")
        self.assertEqual(step["verification"], "1 entries")
        self.assertIsNotNone(step["finished_at"])
        # stale expected status cannot rewrite a done step
        self.assertFalse(db.update_goal_step_status(
            self.conn, goal["id"], 1, "running", "failed"))

    def test_step_bounds_enforced_by_schema(self):
        goal = self._goal()
        with self.assertRaises(sqlite3.IntegrityError):
            db.replace_goal_steps(self.conn, goal["id"], [
                {"purpose": "s", "tool": "t",
                 "args": {"path": "x" * 70000},
                 "depends_on": [], "acceptance": "a"}])

    def test_event_retention_keeps_newest_only(self):
        goal = self._goal()
        for i in range(db.MAX_GOAL_EVENTS + 40):
            db.insert_goal_event(self.conn, goal["id"],
                                 f"event_{i}", f"detail {i}")
        events = db.get_goal_events(self.conn, goal["id"], limit=1000)
        ids = [e["id"] for e in events]
        self.assertLessEqual(len(events), db.MAX_GOAL_EVENTS)
        total = self.conn.execute(
            "SELECT count(*) FROM goal_events WHERE goal_id=?",
            (goal["id"],)).fetchone()[0]
        self.assertLessEqual(total, db.MAX_GOAL_EVENTS)
        # the newest window is what survives (oldest pruned)
        self.assertEqual(events[-1]["event"], f"event_{db.MAX_GOAL_EVENTS + 39}")
        self.assertEqual(
            events[0]["event"],
            f"event_{db.MAX_GOAL_EVENTS + 39 - db.MAX_GOAL_EVENTS + 1}")
        self.assertTrue(len(ids) == len(set(ids)))

    def test_event_detail_bounded(self):
        goal = self._goal()
        db.insert_goal_event(self.conn, goal["id"], "bounded",
                             "d" * 500)
        events = db.get_goal_events(self.conn, goal["id"])
        self.assertEqual(len(events[0]["detail"]), 300)

    def test_list_goals_newest_first_and_bounded(self):
        for i in range(30):
            self._goal(f"goal {i:02d}")
        rows = db.list_goals(self.conn, 5)
        self.assertEqual([r["objective"] for r in rows],
                         [f"goal {i:02d}" for i in range(29, 24, -1)])

    def test_actionable_goals_fifo_excludes_terminals(self):
        g1 = self._goal("a")
        g2 = self._goal("b")
        db.update_goal_fields(self.conn, g1["id"], "queued",
                              {"status": "succeeded"})
        rows = db.list_actionable_goals(self.conn)
        self.assertEqual([r["id"] for r in rows], [g2["id"]])
        g3 = self._goal("c")
        g4 = self._goal("d")
        ids = [r["objective"] for r in db.list_actionable_goals(self.conn)]
        self.assertEqual(ids, ["b", "c", "d"])

    def test_progress_reflects_persisted_steps_only(self):
        goal = self._goal()
        db.replace_goal_steps(self.conn, goal["id"], [
            {"purpose": "a", "tool": "t", "args": {},
             "depends_on": [], "acceptance": "a"},
            {"purpose": "b", "tool": "t", "args": {},
             "depends_on": [], "acceptance": "b"},
            {"purpose": "c", "tool": "t", "args": {},
             "depends_on": [], "acceptance": "c"},
        ])
        steps = db.get_goal_steps(self.conn, goal["id"])
        self.assertEqual(db.goal_progress([])["total"], 0)
        self.assertEqual(db.goal_progress(steps)["total"], 3)
        self.assertEqual(db.goal_progress(steps)["done"], 0)
        db.update_goal_step_status(self.conn, goal["id"], 1,
                                   "pending", "done")
        db.update_goal_step_status(self.conn, goal["id"], 2,
                                   "pending", "failed")
        steps = db.get_goal_steps(self.conn, goal["id"])
        progress = db.goal_progress(steps)
        self.assertEqual(progress["done"], 1)
        self.assertEqual(progress["failed"], 1)
        self.assertEqual(progress["running"], 0)


if __name__ == "__main__":
    unittest.main()
