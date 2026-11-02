"""Server API surface for the Phase 14B/14C/14D additions.

Real HTTP tests over an isolated temp DB and fake provider: sandboxed
skill execution through POST /api/skills/run, run history, automatic
improvement verification through GET /api/improvements, and the
report-only update install default.
"""

import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_chat_api import ChatAPITestCase


def _seed_skills_state(db_path, sid, *, enabled, trusted):
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "UPDATE skills SET state = ?, trust = ? WHERE id = ?",
            ("enabled" if enabled else "available",
             "trusted" if trusted else "untrusted", sid))
    conn.close()


def _sync_skills(db_path, roots, now="t"):
    import skills as skills_mod
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        skills_mod.sync_registry(conn, roots, now)
    conn.close()


class _PostHelperMixin:
    def post_json(self, path, payload):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")

    def get_json(self, path):
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=15) as resp:
            return resp.status, json.loads(resp.read())


class SkillRunAPITests(ChatAPITestCase, _PostHelperMixin):
    def setUp(self):
        super().setUp()
        self._skill_root = Path(self._temp_dir_key()) / "skills"
        self._skill_root.mkdir()
        d = self._skill_root / "hello"
        d.mkdir()
        (d / "SKILL.md").write_text(
            "---\nname: hello\ndescription: prints a greeting\n"
            "version: 1.0.0\nentrypoint: run.py\n---\nnotes")
        (d / "run.py").write_text(
            "import json, os\n"
            "args = json.loads(os.environ.get('SKILL_ARGS') or '{}')\n"
            "print('hello', args.get('name', 'world'))\n")
        self._prev_env = os.environ.get("JARVIS_SKILLS_DIR")
        os.environ["JARVIS_SKILLS_DIR"] = str(self._skill_root)
        self.addCleanup(self._restore_env)

    def _temp_dir_key(self):
        # the harness temp dir that hosts the per-test DB
        return Path(self.db_path).parent

    def _restore_env(self):
        if self._prev_env is None:
            os.environ.pop("JARVIS_SKILLS_DIR", None)
        else:
            os.environ["JARVIS_SKILLS_DIR"] = self._prev_env

    def test_execution_surface_honest(self):
        status, payload = self.get_json("/api/skills")
        self.assertEqual(status, 200)
        self.assertIn(payload["execution"], ("sandboxed", "unavailable"))
        self.assertIn("fail", payload["execution_note"])

    def test_report_only_runs_history_starts_empty(self):
        status, payload = self.get_json("/api/skills/runs?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(payload["runs"], [])

    def test_untrusted_run_refused_and_audited(self):
        _sync_skills(self.db_path, [self._skill_root])
        status, payload = self.post_json("/api/skills/run",
                                         {"id": "hello"})
        self.assertEqual(status, 400)
        # fresh discovery rows are available+untrusted: the enabled gate
        # fires first; either refusal is honest fail-closed
        self.assertIn(payload["error"]["code"],
                      ("skill_disabled", "skill_untrusted"))
        status, payload = self.get_json("/api/skills/runs")
        self.assertEqual(payload["runs"][0]["status"], "refused")

    def test_trusted_enabled_run_is_sandboxed_and_recorded(self):
        _sync_skills(self.db_path, [self._skill_root])
        _seed_skills_state(self.db_path, "hello", enabled=True,
                           trusted=True)
        status, payload = self.post_json(
            "/api/skills/run",
            {"id": "hello", "args": {"name": "operator"}})
        if payload.get("error", {}).get("code") == "sandbox_unavailable":
            self.skipTest("bubblewrap not installed on this host")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "completed")
        self.assertIn("hello operator", payload["stdout"])
        self.assertIn("verified", payload["verification"])
        runs = self.get_json("/api/skills/runs")[1]["runs"]
        self.assertEqual(runs[0]["skill_id"], "hello")

    def test_unknown_skill_404(self):
        status, payload = self.post_json("/api/skills/run",
                                         {"id": "ghost"})
        self.assertEqual(status, 404)

    def test_disabled_run_refused(self):
        _sync_skills(self.db_path, [self._skill_root])
        _seed_skills_state(self.db_path, "hello", enabled=False,
                           trusted=True)
        status, payload = self.post_json("/api/skills/run",
                                         {"id": "hello"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "skill_disabled")


class ImprovementsVerifyAPITests(ChatAPITestCase, _PostHelperMixin):
    def _seed_blocked(self, n=5):
        conn = sqlite3.connect(self.db_path)
        with conn:
            for i in range(n):
                conn.execute(
                    "INSERT INTO goals(id, objective, status)"
                    " VALUES (?, ?, ?)", ("bg%d" % i, "obj", "blocked"))
        conn.close()

    def test_loop_propose_approve_apply_verify(self):
        self._seed_blocked()
        status, _ = self.post_json("/api/improvements/propose", {})
        self.assertEqual(status, 200)
        status, view = self.get_json("/api/improvements")
        pid = view["proposals"][0]["id"]
        self.assertEqual(view["proposals"][0]["status"], "proposed")
        status, _ = self.post_json("/api/improvements/decide",
                                   {"id": pid, "decision": "approve"})
        self.assertEqual(status, 200)
        status, _ = self.post_json("/api/improvements/decide",
                                   {"id": pid, "decision": "apply"})
        self.assertEqual(status, 200)
        # the VERIFY leg runs automatically on the view cycle
        status, view = self.get_json("/api/improvements")
        row = [p for p in view["proposals"] if p["id"] == pid][0]
        self.assertEqual(row["verify_status"], "pending_verification")
        # complete the follow-up task (the human work it willingly
        # cannot do itself), the next cycle verifies
        conn = sqlite3.connect(self.db_path)
        task_id = conn.execute(
            "SELECT task_id FROM improvements WHERE id=?",
            (pid,)).fetchone()[0]
        with conn:
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        conn.close()
        status, view = self.get_json("/api/improvements")
        row = [p for p in view["proposals"] if p["id"] == pid][0]
        self.assertEqual(row["verify_status"], "verified")


class UpdatesReportOnlyDefaultTests(ChatAPITestCase, _PostHelperMixin):
    def test_stage_refused_without_target(self):
        status, payload = self.post_json("/api/updates/stage",
                                         {"source": "https://x.invalid"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"],
                         "target_not_configured")

    def test_updates_view_lists_empty_install_history(self):
        status, payload = self.get_json("/api/updates")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("installs"), [])