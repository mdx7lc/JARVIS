"""Update management + bounded self-improvement tests.

Updates: honest defaults (not_configured), report-only policy, real
check history, network failure recorded never raised, no install path.
Improvements: evidence from real aggregates, deterministic rules,
state transitions, apply bounded to exactly one L2 task.
"""

import http.server
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import db
import improvements
import updates


class _DbCase(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="jarvis-upd-"))
        self.addCleanup(self._rmtree, d)
        self.db_path = d / "db.sqlite"
        db.init_db(self.db_path)

    def _rmtree(self, path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


class UpdatesTests(_DbCase):
    def test_current_version_read_from_file(self):
        self.assertEqual(updates.current_version({}), "0.14.0")

    def test_default_honest_not_configured(self):
        result = updates.check(None, "0.14.0")
        self.assertEqual(result["status"], "not_configured")
        self.assertIsNone(result["latest"])

    def test_check_against_local_source(self):
        body = json.dumps({"version": "0.15.0",
                           "notes": "adds a permission report"}).encode()

        class Upstream(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/latest.json"

        result = updates.check(url, "0.14.0")
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["latest"], "0.15.0")

        result = updates.check(url, "0.15.0")
        self.assertEqual(result["status"], "up_to_date")

    def test_network_failure_recorded_never_raised(self):
        result = updates.check("https://127.0.0.1:1/nothing.json",
                               "0.14.0")
        self.assertEqual(result["status"], "error")

    def test_check_history_persisted(self):
        record = updates.check(None, "0.14.0")
        conn = self._conn()
        try:
            with conn:
                updates.record(conn, record, "0.14.0", "now")
            rows = conn.execute(
                "SELECT status FROM update_checks").fetchall()
            self.assertEqual(rows[0]["status"], "not_configured")
        finally:
            conn.close()


class ImprovementsTests(_DbCase):
    def _seed_blocked_goals(self, count=5):
        conn = self._conn()
        try:
            with conn:
                for i in range(count):
                    conn.execute(
                        "INSERT INTO goals(id, objective, status)"
                        " VALUES (?, ?, ?)", ("g%d" % i, "obj",
                                              "blocked"))
        finally:
            conn.close()

    def test_observe_uses_real_aggregates(self):
        self._seed_blocked_goals()
        conn = self._conn()
        try:
            evidence = improvements.observe(conn)
        finally:
            conn.close()
        self.assertEqual(evidence["goals"].get("blocked"), 5)

    def test_no_evidence_means_no_proposals(self):
        conn = self._conn()
        try:
            with conn:
                created = improvements.propose(conn, "now")
        finally:
            conn.close()
        self.assertEqual(created, [])

    def test_blocked_goals_generate_proposal_and_dedupe(self):
        self._seed_blocked_goals()
        conn = self._conn()
        try:
            with conn:
                created = improvements.propose(conn, "t1")
            self.assertTrue(any(p["kind"] == "high_blocked_ratio"
                                for p in created))
            with conn:
                again = improvements.propose(conn, "t2")
            self.assertEqual(again, [])  # still open: no duplicate
        finally:
            conn.close()

    def test_decision_gates_and_apply_creates_one_task(self):
        self._seed_blocked_goals()
        conn = self._conn()
        try:
            with conn:
                created = improvements.propose(conn, "t1")
        finally:
            conn.close()
        pid = created[0]["id"]
        conn = self._conn()
        try:
            # apply before approval: refused
            with conn:
                with self.assertRaises(ValueError):
                    improvements.decide(conn, pid, "apply", "t2")
            with conn:
                self.assertEqual(
                    improvements.decide(conn, pid, "approve", "t3"),
                    "approved")
            with conn:
                status = improvements.decide(conn, pid, "apply", "t4")
                self.assertEqual(status, "applied")
            row = conn.execute(
                "SELECT status, task_id FROM improvements WHERE id = ?",
                (pid,)).fetchone()
            self.assertEqual(row["status"], "applied")
            self.assertTrue(row["task_id"])
            tasks = conn.execute("SELECT title, status FROM tasks"
                                 " WHERE id = ?",
                                 (row["task_id"],)).fetchone()
            self.assertEqual(tasks["status"], "queued")
            self.assertIn("Improvement", tasks["title"])
            # applied proposals are closed: no further decisions
            with conn:
                with self.assertRaises(ValueError):
                    improvements.decide(conn, pid, "apply", "t5")
        finally:
            conn.close()

    def test_unknown_proposal_rejected(self):
        conn = self._conn()
        try:
            with conn:
                with self.assertRaises(ValueError):
                    improvements.decide(conn, "imp-none", "approve",
                                        "t")
        finally:
            conn.close()

    def test_rejection_never_overwrites_applied_history(self):
        self._seed_blocked_goals()
        conn = self._conn()
        try:
            with conn:
                created = improvements.propose(conn, "t1")
        finally:
            conn.close()
        pid = created[0]["id"]
        conn = self._conn()
        try:
            with conn:
                improvements.decide(conn, pid, "approve", "t2")
                improvements.decide(conn, pid, "apply", "t3")
            with conn:
                with self.assertRaises(ValueError):
                    improvements.decide(conn, pid, "reject", "t4")
            row = conn.execute(
                "SELECT status FROM improvements WHERE id = ?",
                (pid,)).fetchone()
            self.assertEqual(row["status"], "applied")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
