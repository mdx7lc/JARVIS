"""Phase 10A lifecycle tests: the worker around human write approvals.

Covers restart safety (recover() never re-runs or upgrades an approval),
tamper detection at claim time, TOU-style target races, and
execute-at-most-once. No live inference: the provider call is patched.
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import goalstate
import tools
import worker as worker_mod


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class LazyReviewer:
    def review(self, plan, registry, objective="", local_check=None):
        return {"status": "approved", "source": "external_review",
                "identity": "fake-approver", "verdict": "approved",
                "risk_level": "low", "findings": [], "missing_criteria": [],
                "recommended_changes": [], "reviewed_at": "now"}


class LifecycleHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-lc-")
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        (self.workspace / "docs").mkdir(parents=True)
        self.db_path = Path(self._tmp.name) / "jarvis.db"
        db.init_db(self.db_path)
        self.registry = tools.default_registry(self.workspace)
        cfg = type("Cfg", (), {
            "model": "fake-model", "api_base": "http://x",
            "api_key": "k", "fallback_model": None,
            "timeout_seconds": 1, "max_tokens": 100,
        })()
        self.worker = worker_mod.GoalWorker(
            self.db_path, cfg, self.registry, reviewer=LazyReviewer())

    def plan(self, steps=None):
        return {
            "summary": "write a note",
            "outcome": "the note exists",
            "criteria": ["the note file says hello"],
            "risk": "low",
            "uncertainties": [],
            "steps": steps or [{
                "purpose": "create the note file with hello",
                "tool": "workspace.create_file",
                "args": {"path": "docs/note.txt", "content": "hello"},
                "depends_on": [],
                "acceptance": "the note file says hello",
            }],
        }

    def run_with_plan(self, plan):
        patcher = patch.object(
            worker_mod.provider, "complete_with_fallback",
            return_value={"reply": json.dumps(plan)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def new_goal(self):
        conn = db.open_conn(self.db_path)
        try:
            return db.create_goal(conn, "write the note")["id"]
        finally:
            conn.close()

    def goal(self):
        conn = db.open_conn(self.db_path)
        try:
            return db.get_goal(conn, self.gid)
        finally:
            conn.close()

    # shared flows ----------------------------------------------------

    def plan_and_wait(self, plan=None):
        self.run_with_plan(plan or self.plan())
        self.gid = self.new_goal()
        self.worker.process_once(self.gid)
        goal = self.goal()
        self.assertEqual(goal["status"], "awaiting_approval", goal)
        conn = db.open_conn(self.db_path)
        try:
            return db.list_goal_approvals(conn, self.gid)
        finally:
            conn.close()

    def approve_all(self):
        conn = db.open_conn(self.db_path)
        try:
            for approval in db.list_goal_approvals(conn, self.gid):
                if approval["state"] == "proposed":
                    self.assertTrue(db.update_goal_approval_state(
                        conn, self.gid, approval["seq"], "proposed",
                        "approved", {"approved_at": "now"}))
        finally:
            conn.close()


class LifecycleTests(LifecycleHarness):
    def test_restart_recovery_never_upgrades_or_reruns(self):
        proposals = self.plan_and_wait()
        # restart: recovery pass
        self.worker.recover()
        goal = self.goal()
        self.assertEqual(goal["status"], "awaiting_approval")
        for step in self.steps():
            self.assertEqual(step["status"], "pending")
        # nothing has run; approval still fresh works after restart
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.update_goal_approval_state(
                conn, self.gid, proposals[0]["seq"], "proposed",
                "approved", {"approved_at": "after restart"}))
        finally:
            conn.close()
        self.worker.process_once(self.gid)
        self.assertEqual(self.goal()["status"], "succeeded")
        self.assertEqual((self.workspace / "docs/note.txt").read_text(),
                         "hello")

    def test_recover_interrupted_claim_demands_reconciliation(self):
        proposals = self.plan_and_wait()
        self.approve_all()
        # simulate a crash after the claim, mid-execution:
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.update_goal_fields(
                conn, self.gid, "awaiting_approval", {"status":
                                                      "executing"}))
            self.assertTrue(db.update_goal_approval_state(
                conn, self.gid, proposals[0]["seq"], "approved",
                "claimed", {"claimed_at": "now"}))
            db.update_goal_step_status(
                conn, self.gid, 1, "pending", "running", {})
        finally:
            conn.close()
        (self.workspace / "docs/note.txt").write_text("mystery")
        self.worker.recover()
        goal = self.goal()
        self.assertEqual(goal["status"], "blocked")
        self.assertEqual(goal["error_code"], "reconciliation_required")
        self.assertFalse(goalstate.retryable("blocked",
                                             "reconciliation_required"))

    def test_tampered_step_args_refused_at_claim(self):
        proposals = self.plan_and_wait()
        tampered = '{"path": "docs/note.txt", "content": "EVIL PAYLOAD"}'
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.update_goal_approval_state(
                conn, self.gid, 1, "proposed", "approved",
                {"approved_at": "now"}))
            with conn:
                conn.execute(
                    "UPDATE goal_steps SET args_json=? "
                    "WHERE goal_id=? AND seq=1",
                    (tampered, self.gid))
        finally:
            conn.close()
        self.worker.process_once(self.gid)
        goal = self.goal()
        self.assertEqual(goal["status"], "failed", goal)
        self.assertEqual(goal["error_code"], "approval_claim_conflict")
        self.assertFalse((self.workspace / "docs/note.txt").exists())
        # approval was never claimed/executed
        conn = db.open_conn(self.db_path)
        try:
            approval = db.get_goal_approval(conn, self.gid, 1)
            self.assertEqual(approval["state"], "approved")
        finally:
            conn.close()

    def test_execution_race_target_created_first_refused(self):
        self.plan_and_wait()
        self.approve_all()
        (self.workspace / "docs/note.txt").write_text("preexisting")
        self.worker.process_once(self.gid)
        goal = self.goal()
        self.assertEqual(goal["status"], "failed", goal)
        self.assertEqual(goal["error_code"], "already_exists")
        self.assertEqual(
            (self.workspace / "docs/note.txt").read_text(),
            "preexisting")
        self.assertEqual(self.backups(), [])

    def test_execute_at_most_once(self):
        self.plan_and_wait()
        self.approve_all()
        self.worker.process_once(self.gid)
        self.assertEqual(self.goal()["status"], "succeeded")
        self.worker.process_once(self.gid)  # terminal: no-op
        self.worker.process_once(self.gid)
        step = self.steps()[0]
        self.assertEqual(step["status"], "done")
        self.assertEqual(step["attempts"], 1)
        self.assertEqual((self.workspace / "docs/note.txt").read_text(),
                         "hello")
        conn = db.open_conn(self.db_path)
        try:
            approval = db.get_goal_approval(conn, self.gid, 1)
        finally:
            conn.close()
        self.assertEqual(approval["state"], "executed")
        self.assertEqual(approval["executed_sha256"], sha("hello"))

    def test_two_step_mixed_plan_runs_after_each_approval(self):
        two = self.plan(steps=[
            {
                "purpose": "create the note file with hello",
                "tool": "workspace.create_file",
                "args": {"path": "docs/note.txt", "content": "hello"},
                "depends_on": [],
                "acceptance": "the note file says hello",
            },
            {
                "purpose": "rewrite the readme to second",
                "tool": "workspace.edit_file",
                "args": {"path": "docs/readme.md", "new_text": "second",
                         "expected_before_sha256": sha("# readme\n")},
                "depends_on": [1],
                "acceptance": "the note file says hello",
            },
        ])
        (self.workspace / "docs/readme.md").write_text("# readme\n")
        proposals = self.plan_and_wait(two)
        self.assertEqual(len(proposals), 2)
        # approve only the FIRST: scan reports waiting, no second step
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.update_goal_approval_state(
                conn, self.gid, 1, "proposed", "approved",
                {"approved_at": "now"}))
        finally:
            conn.close()
        self.worker.process_once(self.gid)
        self.assertEqual(self.goal()["status"], "awaiting_approval")
        self.assertFalse(
            (self.workspace / "docs/note.txt").exists(),
            "first step must not run while any approval is pending")
        # full approval resumes and completes both steps in order
        self.approve_all()
        self.worker.process_once(self.gid)
        self.assertEqual(self.goal()["status"], "succeeded")
        self.assertEqual((self.workspace / "docs/note.txt").read_text(),
                         "hello")
        self.assertEqual((self.workspace / "docs/readme.md").read_text(),
                         "second")

    def steps(self):
        conn = db.open_conn(self.db_path)
        try:
            return db.get_goal_steps(conn, self.gid)
        finally:
            conn.close()

    def backups(self):
        backup_dir = self.workspace / ".jarvis" / "backup"
        if not backup_dir.exists():
            return []
        return list(backup_dir.iterdir())


if __name__ == "__main__":
    unittest.main()
