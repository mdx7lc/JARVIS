"""Phase 10A browser E2E — human write-approval flow on the Goal Console.

Drives a goal whose canned plan contains one workspace.create_file step
through: queue → plan → external review approval → awaiting_approval →
preview inside the console → human Approve → execution to succeeded.
Also exercises the Reject path (goal resolves without running). No live
inference: the fake upstream returns the plan fixture; the "reviewer"
is a fake approver injected into the worker.
"""

import json
import tempfile
import unittest
from pathlib import Path

import fake_upstream
from browser_harness import Stack, BrowserCase, SKIP_BROWSER

NOTE_CRITERION = "create the note file with hello"
WRITE_PLAN = {
    "summary": "Write a note",
    "outcome": "Workspace holds a note.",
    "criteria": [NOTE_CRITERION],
    "risk": "low",
    "uncertainties": [],
    "steps": [{
        "purpose": "create the note file with hello",
        "tool": "workspace.create_file",
        "args": {"path": "docs/note.txt", "content": "hello"},
        "depends_on": [],
        "acceptance": NOTE_CRITERION,
    }],
}
WRITE_REPLY = json.dumps(WRITE_PLAN)

CANARY_OBJECTIVE = "BROWSER-WRITE-CANARY-QW3 create the note file"


class ApprovingReviewer:
    def review(self, plan, registry, objective="", local_check=None):
        return {"status": "approved", "source": "external_review",
                "identity": "fake-approver", "verdict": "approved",
                "risk_level": "low", "findings": [], "missing_criteria": [],
                "recommended_changes": [], "reviewed_at": "now"}


@SKIP_BROWSER
class WriteApprovalBrowserTests(BrowserCase):
    REPLY = WRITE_REPLY

    def make_stack(self, reply):
        ws_dir = tempfile.mkdtemp(prefix="jarvis-e2e-ws-writes-")
        self.addCleanup(
            lambda: __import__("shutil").rmtree(ws_dir, True))
        self.workspace = Path(ws_dir) / "workspace"
        (self.workspace / "docs").mkdir(parents=True)
        stack = Stack(reply=reply, extra_env={
            "JARVIS_WORKSPACE_ROOT": str(self.workspace),
        })
        stack._httpd.goal_worker.reviewer = ApprovingReviewer()
        return stack

    def open_console(self):
        self.page.evaluate("openGoalsModal()")
        self.page.wait_for_selector("#modal-goals[open]")

    def submit(self, text=CANARY_OBJECTIVE):
        self.page.fill("#goal-objective", text)
        self.page.click("#goal-submit")

    def wait_goal_status(self, containing, timeout=30000):
        self.page.wait_for_function(
            "(needle) => { const c = document.querySelector("
            "'#goals-list .goal-item .chip');"
            "return c && c.textContent.indexOf(needle) >= 0 }",
            arg=containing, timeout=timeout)

    def goal_id(self):
        return self.page.evaluate(
            "() => document.querySelector('#goals-list .goal-item')"
            ".dataset.goalId")

    def open_detail(self):
        self.page.click("#goals-list .goal-item .goal-open")
        self.page.wait_for_selector("#goal-detail:not([hidden])")

    def proposal_panel(self):
        return self.page.inner_text(".goal-proposals")

    # ------------------------------------------------------------ tests

    def test_write_goal_reaches_pending_approval_without_execution(self):
        self.open_console()
        self.submit()
        self.wait_goal_status("awaiting approval")
        self.open_detail()
        self.page.wait_for_selector(".goal-proposals")
        panel = self.proposal_panel().lower()
        self.assertIn("write approvals", panel)
        self.assertIn("docs/note.txt", panel)
        self.assertIn("#1 create — proposed", panel)
        self.assertIn("preview diff", panel)
        self.assertIn("digest", panel)
        # nothing is written before approval
        self.assertFalse((self.workspace / "docs/note.txt").exists())

    def test_preview_shows_content_and_digest(self):
        self.open_console()
        self.submit()
        self.wait_goal_status("awaiting approval")
        self.open_detail()
        self.page.wait_for_selector(".goal-proposals")
        self.page.evaluate(
            "() => { const b = [...document.querySelectorAll("
            "'.goal-proposal button')]"
            ".find(b => b.textContent === 'Preview diff');"
            "b.click(); }")
        self.page.wait_for_selector(".goal-proposal-preview:not([hidden])")
        preview_text = self.page.inner_text(".goal-proposal-preview")
        self.assertIn("hello", preview_text)
        self.assertIn("digest", preview_text.lower())
        self.assertFalse((self.workspace / "docs/note.txt").exists())

    def test_full_approve_flow_runs_and_succeeds(self):
        self.open_console()
        self.submit()
        self.wait_goal_status("awaiting approval")
        self.open_detail()
        self.page.wait_for_selector(".goal-proposals")
        self.page.evaluate(
            "() => { const b = [...document.querySelectorAll("
            "'.goal-proposal button')]"
            ".find(b => b.textContent === 'Approve & execute');"
            "b.click(); }")
        self.wait_goal_status("succeeded", timeout=30000)
        note = (self.workspace / "docs/note.txt")
        self.assertTrue(note.exists())
        self.assertEqual(note.read_text(), "hello")

    def test_reject_resolves_without_running(self):
        self.open_console()
        self.submit()
        self.wait_goal_status("awaiting approval")
        self.open_detail()
        self.page.wait_for_selector(".goal-proposals")
        self.page.evaluate(
            "() => { const b = [...document.querySelectorAll("
            "'.goal-proposal button')]"
            ".find(b => b.textContent === 'Reject');"
            "b.click(); }")
        self.wait_goal_status("blocked")
        self.assertFalse((self.workspace / "docs/note.txt").exists())
        self.open_detail()
        detail = self.page.inner_text("#goal-detail")
        self.assertIn("approval_withdrawn", detail)


if __name__ == "__main__":
    unittest.main()
