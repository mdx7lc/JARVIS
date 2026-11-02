"""Phase 10A integration tests: the human approval layer.

Goal API + digest-bound approve/reject endpoints + preview. Everything
runs against a temp DB, a temp workspace and a fake upstream (no live
inference); the worker is driven synchronously.
"""

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import config
import db
import server
from fake_upstream import FakeUpstream

KEY = "test-key-123"
MODEL = "qwen3.8-27b"

NOTE_CRITERION = "create the note file with hello"
EDIT_CRITERION = "rewrite the readme to second"
README_CONTENT = "# readme\n"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write_plan():
    return {
        "summary": "Write a note and rewrite the readme",
        "outcome": "Workspace holds a note and a rewritten readme.",
        "criteria": [NOTE_CRITERION, EDIT_CRITERION],
        "risk": "low",
        "uncertainties": [],
        "steps": [
            {
                "purpose": "create the note file with hello",
                "tool": "workspace.create_file",
                "args": {"path": "docs/note.txt", "content": "hello"},
                "depends_on": [],
                "acceptance": NOTE_CRITERION,
            },
            {
                "purpose": "rewrite the readme to second",
                "tool": "workspace.edit_file",
                "args": {
                    "path": "docs/readme.md", "new_text": "second",
                    "expected_before_sha256": sha(README_CONTENT),
                },
                "depends_on": [],
                "acceptance": EDIT_CRITERION,
            },
        ],
    }


def plan_completion(text):
    return {
        "id": "chatcmpl-plan",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
    }


def _request(url, payload=None, method="POST"):
    data = json.dumps(payload).encode("utf-8") if payload is not None \
        else None
    req = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"null")
        except ValueError:
            return err.code, None


class ApprovalApprover:
    """External reviewer that approves; the hman layer is separate."""

    def review(self, plan, registry, objective="", local_check=None):
        return {"status": "approved", "source": "external_review",
                "identity": "fake-approver", "verdict": "approved",
                "risk_level": "low", "findings": [], "missing_criteria": [],
                "recommended_changes": [], "reviewed_at": "now"}


class UnavailableReviewer:
    def review(self, plan, registry, objective="", local_check=None):
        return {"status": "unavailable", "source": "unavailable",
                "identity": None, "verdict": None, "risk_level": None,
                "findings": [], "missing_criteria": [],
                "recommended_changes": [], "reason": "not_configured",
                "reviewed_at": None}


class ApprovalHarness(unittest.TestCase):
    """Temp db/workspace + server with the worker constructable but not
    polling; tests drive process_once synchronously."""

    plan = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-appr-")
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        (self.workspace / "docs").mkdir(parents=True)
        (self.workspace / "docs" / "readme.md").write_text(README_CONTENT)
        self.db_path = Path(self._tmp.name) / "jarvis.db"
        plan = self.plan if self.plan is not None else write_plan()
        self.upstream = FakeUpstream(response=(
            200, plan_completion(json.dumps(plan)), "application/json"))
        self.upstream.start(self)
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": KEY,
            "JARVIS_WORKSPACE_ROOT": str(self.workspace),
        }
        self.cfg = config.load(environ=env)
        self._httpd = server.create_server(
            "127.0.0.1", 0, self.db_path, init_db=True, cfg=self.cfg,
            start_worker=False, settings_dir=str(self._tmp.name))
        base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop_http)
        self.base = base
        self.goal_worker = self._httpd.goal_worker

    def _stop_http(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def reviewer(self, reviewer_cls):
        self.goal_worker.reviewer = reviewer_cls()

    def get(self, path):
        return _request(self.base + path, method="GET")

    def post(self, path, payload=None):
        return _request(self.base + path, payload, "POST")

    def create_goal(self, objective="write the files we agreed"):
        status, body = self.post("/api/goals", {"objective": objective})
        self.assertEqual(status, 202, body)
        return body["id"]

    def detail(self, goal_id):
        status, body = self.get(f"/api/goals/{goal_id}")
        self.assertEqual(status, 200, body)
        return body

    def proposals(self, goal_id):
        status, body = self.get(f"/api/goals/{goal_id}/proposals")
        self.assertEqual(status, 200, body)
        return body["proposals"]


class ProposalFlowTests(ApprovalHarness):
    def test_full_approval_flow(self):
        self.reviewer(ApprovalApprover)
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        self.assertEqual(self.detail(goal_id)["status"],
                         "awaiting_approval")
        self.assertFalse((self.workspace / "docs/note.txt").exists())
        self.assertEqual((self.workspace / "docs/readme.md").read_text(),
                         README_CONTENT)
        proposals = self.proposals(goal_id)
        self.assertEqual(len(proposals), 2)
        self.assertEqual([p["state"] for p in proposals],
                         ["proposed", "proposed"])
        for proposal in proposals:
            status, body = self.post(
                f"/api/goals/{goal_id}/approve",
                {"seq": proposal["seq"], "digest": proposal["digest"]})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["state"], "approved")
        self.goal_worker.process_once(goal_id)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertEqual((self.workspace / "docs/note.txt").read_text(),
                         "hello")
        self.assertEqual((self.workspace / "docs/readme.md").read_text(),
                         "second")
        states = [
            (p["seq"], p["state"]) for p in self.proposals(goal_id)]
        self.assertEqual(states, [(1, "executed"), (2, "executed")])

    def test_partial_then_all_approval_flow(self):
        self.reviewer(ApprovalApprover)
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        proposals = self.proposals(goal_id)
        # approve only the first: worker stays waiting; nothing runs
        status, _ = self.post(
            f"/api/goals/{goal_id}/approve",
            {"seq": proposals[0]["seq"],
             "digest": proposals[0]["digest"]})
        self.assertEqual(status, 200)
        self.goal_worker.process_once(goal_id)
        self.assertEqual(self.detail(goal_id)["status"],
                         "awaiting_approval")
        self.assertFalse((self.workspace / "docs/note.txt").exists())
        # second approval resumes the whole run
        status, _ = self.post(
            f"/api/goals/{goal_id}/approve",
            {"seq": proposals[1]["seq"],
             "digest": proposals[1]["digest"]})
        self.assertEqual(status, 200)
        self.goal_worker.process_once(goal_id)
        self.assertEqual(self.detail(goal_id)["status"], "succeeded")

    def test_double_approve_refused(self):
        self.reviewer(ApprovalApprover)
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        first = self.proposals(goal_id)[0]
        status, _ = self.post(
            f"/api/goals/{goal_id}/approve",
            {"seq": first["seq"], "digest": first["digest"]})
        self.assertEqual(status, 200)
        status, body = self.post(
            f"/api/goals/{goal_id}/approve",
            {"seq": first["seq"], "digest": first["digest"]})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "invalid_state")

    def test_tampered_digest_refused(self):
        self.reviewer(ApprovalApprover)
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        first = self.proposals(goal_id)[0]
        wrong = "0" * 64 if not first["digest"].startswith("0") \
            else "1" * 64
        status, body = self.post(
            f"/api/goals/{goal_id}/approve",
            {"seq": first["seq"], "digest": wrong})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "digest_mismatch")
        self.assertFalse((self.workspace / "docs/note.txt").exists())
        self.assertEqual(self.proposals(goal_id)[0]["state"], "proposed")

    def test_create_target_conflict_refuses_approval(self):
        self.reviewer(ApprovalApprover)
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        (self.workspace / "docs/note.txt").write_text("preexisting")
        first = self.proposals(goal_id)[0]
        status, body = self.post(
            f"/api/goals/{goal_id}/approve",
            {"seq": first["seq"], "digest": first["digest"]})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "target_changed")

    def test_edit_target_changed_refuses_approval(self):
        self.reviewer(ApprovalApprover)
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        (self.workspace / "docs/readme.md").write_text("# CHANGED\n")
        second = self.proposals(goal_id)[1]
        status, body = self.post(
            f"/api/goals/{goal_id}/approve",
            {"seq": second["seq"], "digest": second["digest"]})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "target_changed")

    def test_reject_blocks_resolution(self):
        self.reviewer(ApprovalApprover)
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        first = self.proposals(goal_id)[0]
        status, body = self.post(
            f"/api/goals/{goal_id}/reject", {"seq": first["seq"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "rejected")
        self.goal_worker.process_once(goal_id)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "blocked")
        self.assertEqual(detail["error_code"], "approval_withdrawn")
        self.assertFalse((self.workspace / "docs/note.txt").exists())
        # further scans change nothing
        self.goal_worker.process_once(goal_id)
        self.assertFalse((self.workspace / "docs/note.txt").exists())

    def test_preview_endpoint(self):
        self.reviewer(ApprovalApprover)
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        for seq in (1, 2):
            status, body = self.get(
                f"/api/goals/{goal_id}/proposals/{seq}")
            self.assertEqual(status, 200, body)
            preview = body["preview"]
            if preview["kind"] == "create":
                self.assertEqual(preview["content"], "hello")
                self.assertFalse(preview["current"]["exists"])
                self.assertTrue(
                    preview["current"]["matches_approved_before"])
            else:
                self.assertEqual(preview["content"], "second")
                self.assertTrue(preview["current"]["exists"])
                self.assertTrue(
                    preview["current"]["matches_approved_before"])
                joined = "\n".join(preview["diff"])
                self.assertIn("second", joined)
        status, _ = self.get(f"/api/goals/{goal_id}/proposals/9")
        self.assertEqual(status, 404)

    def test_approve_rejected_when_not_awaiting(self):
        self.reviewer(ApprovalApprover)
        goal_id = self.create_goal()
        self.assertEqual(self.proposals(goal_id), [])
        status, body = self.post(f"/api/goals/{goal_id}/approve",
                                 {"seq": 1, "digest": "x" * 64})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "invalid_state")

    def test_bad_payloads(self):
        self.reviewer(ApprovalApprover)
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        status, body = self.post(f"/api/goals/{goal_id}/approve",
                                 {"seq": "one", "digest": "x" * 64})
        self.assertEqual(status, 400)
        status, body = self.post(f"/api/goals/{goal_id}/approve",
                                 {"seq": 1, "digest": "x" * 63})
        self.assertEqual(status, 400)
        status, body = self.post(f"/api/goals/{goal_id}/approve",
                                 {"seq": 1, "digest": "x" * 64,
                                  "extra": 1})
        self.assertEqual(status, 400)


class ReviewerPolicyTests(ApprovalHarness):
    def test_unavailable_reviewer_blocks_write_goals(self):
        self.reviewer(UnavailableReviewer)
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "blocked")
        self.assertEqual(detail["error_code"], "reviewer_unavailable")
        self.assertFalse((self.workspace / "docs/note.txt").exists())
        self.assertEqual((self.workspace / "docs/readme.md").read_text(),
                         README_CONTENT)
        # no proposals were created either
        self.assertEqual(self.proposals(goal_id), [])

    def test_unavailable_reviewer_leaves_read_only_goals_local(self):
        read_only = {
            "summary": "Inventory the workspace",
            "outcome": "Listed.",
            "criteria": ["accurate inventory of the workspace files"],
            "risk": "low", "uncertainties": [],
            "steps": [{
                "purpose": "List the files",
                "tool": "workspace.list_files",
                "args": {"path": ""}, "depends_on": [],
                "acceptance":
                    "accurate inventory of the workspace files",
            }],
        }
        self.plan = read_only
        self.setUp()
        self.goal_worker.reviewer = UnavailableReviewer()
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertEqual(self.proposals(goal_id), [])


if __name__ == "__main__":
    unittest.main()
