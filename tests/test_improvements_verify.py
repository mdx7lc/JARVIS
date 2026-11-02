"""Self-improvement closed-loop tests (Phase 14C).

VERIFY is automatic and deterministic: baseline snapshot at approval,
measured deltas after apply, regression signal over permission denials,
honest pending state while the follow-up task is not done, and a safe,
footprint-bounded rollback.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
import improvements


class _Case(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jarvis-improv-verify-"))
        self.addCleanup(self._rmtree, self.dir)
        self.db_path = self.dir / "db.sqlite"
        db.init_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            for i in range(5):
                self.conn.execute(
                    "INSERT INTO goals(id, objective, status)"
                    " VALUES (?, ?, ?)",
                    ("g%d" % i, "obj", "blocked"))

    def _rmtree(self, path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def _applied(self):
        with self.conn:
            created = improvements.propose(self.conn, "t0")
        pid = created[0]["id"]
        with self.conn:
            improvements.decide(self.conn, pid, "approve", "t1")
            improvements.decide(self.conn, pid, "apply", "t2")
        return pid


class VerifyLeg(_Case):
    def test_approval_snapshots_baseline(self):
        with self.conn:
            created = improvements.propose(self.conn, "t0")
        pid = created[0]["id"]
        with self.conn:
            improvements.decide(self.conn, pid, "approve", "t1")
        row = self.conn.execute(
            "SELECT baseline_json FROM improvements WHERE id = ?",
            (pid,)).fetchone()
        self.assertIsNotNone(row["baseline_json"])
        base = improvements.json.loads(row["baseline_json"])
        self.assertEqual(base["goals"].get("blocked"), 5)

    def test_applied_not_done_is_honestly_pending(self):
        pid = self._applied()
        with self.conn:
            evaluated = improvements.evaluate_applied(self.conn, "t3")
        self.assertEqual(len(evaluated), 1)
        self.assertEqual(evaluated[0]["verify_status"],
                         "pending_verification")

    def test_completed_task_verifies_against_baseline(self):
        pid = self._applied()
        row = self.conn.execute(
            "SELECT task_id FROM improvements WHERE id = ?",
            (pid,)).fetchone()
        with self.conn:
            db.update_task(self.conn, row["task_id"],
                           {"status": "done"})
        with self.conn:
            evaluated = improvements.evaluate_applied(self.conn, "t3")
        self.assertEqual(evaluated[0]["verify_status"], "verified")
        verify = self.conn.execute(
            "SELECT verify_json FROM improvements WHERE id = ?",
            (pid,)).fetchone()
        self.assertIn("not regress", improvements.json.loads(
            verify["verify_json"])["note"])

    def test_denial_growth_is_a_recorded_regression(self):
        pid = self._applied()
        with self.conn:
            for i in range(6):
                self.conn.execute(
                    "INSERT INTO permission_audit(ts, source,"
                    " tool_name, level, decision, reason)"
                    " VALUES (datetime('now'), 'engine', 'tool%d', 2,"
                    " 'denied', 'test_regression_signal')" % i)
        row = self.conn.execute(
            "SELECT task_id FROM improvements WHERE id = ?",
            (pid,)).fetchone()
        with self.conn:
            db.update_task(self.conn, row["task_id"],
                           {"status": "done"})
            evaluated = improvements.evaluate_applied(self.conn, "t4")
        self.assertEqual(evaluated[0]["verify_status"], "failed_checked")
        verify = self.conn.execute(
            "SELECT verify_json FROM improvements WHERE id = ?",
            (pid,)).fetchone()
        self.assertIn("regression", improvements.json.loads(
            verify["verify_json"])["note"])


class RollbackLeg(_Case):
    def test_failed_checked_applies_can_roll_back_the_task(self):
        pid = self._applied()
        with self.conn:
            conn = self.conn
            task_id = conn.execute(
                "SELECT task_id FROM improvements WHERE id = ?",
                (pid,)).fetchone()["task_id"]
            # force the failed conclusion deterministically
            conn.execute(
                "UPDATE improvements SET verify_status ="
                " 'failed_checked' WHERE id = ?", (pid,))
            status = improvements.rollback_failed(
                self.conn, pid, "t5")
        self.assertEqual(status, "rolled_back")
        self.assertIsNone(db.get_task(self.conn, task_id))
        row = self.conn.execute(
            "SELECT verify_status FROM improvements WHERE id = ?",
            (pid,)).fetchone()
        self.assertEqual(row["verify_status"], "rolled_back")

    def test_rollback_refuses_worked_on_tasks(self):
        pid = self._applied()
        conn = self.conn
        task_id = conn.execute(
            "SELECT task_id FROM improvements WHERE id = ?",
            (pid,)).fetchone()["task_id"]
        with self.conn:
            db.update_task(self.conn, task_id, {"status": "done"})
            conn.execute(
                "UPDATE improvements SET verify_status ="
                " 'failed_checked' WHERE id = ?", (pid,))
        with self.assertRaises(ValueError):
            improvements.rollback_failed(self.conn, pid, "t5")
        # the worked-on task is never destroyed
        self.assertIsNotNone(db.get_task(self.conn, task_id))

    def test_rollback_requires_failed_check(self):
        pid = self._applied()
        with self.assertRaises(ValueError):
            with self.conn:
                improvements.rollback_failed(self.conn, pid, "t5")

    def test_evaluation_is_idempotent(self):
        pid = self._applied()
        with self.conn:
            first = improvements.evaluate_applied(self.conn, "t3")
        with self.conn:
            second = improvements.evaluate_applied(self.conn, "t4")
        # pending rows re-evaluate every cycle (that is the point);
        # the conclusion must be identical while nothing changed
        self.assertEqual(first[0]["verify_status"],
                         second[0]["verify_status"])


if __name__ == "__main__":
    unittest.main()
