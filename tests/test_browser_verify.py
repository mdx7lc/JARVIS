"""Phase 10B browser E2E — read_file evidence + verification recipes
on the Goal Console.

Drives the durable-goal dialog in headless Firefox against the real
worker and a temp workspace: a read-only goal shows the file-content
evidence in the detail panel; an execute ("run tests") proposal previews
its verification metadata before approval and then runs honestly (the
deliberately failing test keeps the goal honest — blocked after the
bounded repair attempt, with diagnostics visible). No live inference:
the fake upstream returns the canned plan fixture; the reviewer is a
fake approver injected into the worker. UI plumbing evidence only.
"""

import json
import tempfile
from pathlib import Path

from browser_harness import Stack, BrowserCase, SKIP_BROWSER

READ_CRITERION = "the inspection records the notes text"
READ_PLAN = {
    "summary": "Inspect the notes",
    "outcome": "The notes text is read and recorded.",
    "criteria": [READ_CRITERION],
    "risk": "low",
    "uncertainties": [],
    "steps": [{
        "purpose": "read the notes file",
        "tool": "workspace.read_file",
        "args": {"path": "notes.txt"},
        "depends_on": [],
        "acceptance": READ_CRITERION,
    }],
}

BROKEN_SUM = "def add(a, b):\n    return a - b\n"
TESTS_CONTENT = (
    "import unittest\n"
    "from src.sum import add\n"
    "class SumTests(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)
RUN_CRITERION = "the unittest run for the sum module is recorded"
RUN_PLAN = {
    "summary": "Run the sum module tests",
    "outcome": "The unittest run is recorded.",
    "criteria": [RUN_CRITERION],
    "risk": "low",
    "uncertainties": [],
    "steps": [
        {
            "purpose": "create the tests file",
            "tool": "workspace.create_file",
            "args": {"path": "tests/test_sum.py",
                     "content": TESTS_CONTENT},
            "depends_on": [],
            "acceptance": "the tests file is written",
        },
        {
            "purpose": "run the tests",
            "tool": "workspace.run_project_tests",
            "args": {"path": "tests/test_sum.py"},
            "depends_on": [1],
            "acceptance": RUN_CRITERION,
        },
    ],
}


class ApprovingReviewer:
    def review(self, plan, registry, objective="", local_check=None):
        return {"status": "approved", "source": "external_review",
                "identity": "fake-approver", "verdict": "approved",
                "risk_level": "low", "findings": [], "missing_criteria": [],
                "recommended_changes": [], "reviewed_at": "now"}


def _stack(self, reply):
    ws_dir = tempfile.mkdtemp(prefix="jarvis-e2e-ws-verify-")
    self.addCleanup(lambda: __import__("shutil").rmtree(ws_dir, True))
    self.workspace = Path(ws_dir) / "workspace"
    prepare_workspace(self.workspace)
    stack = Stack(reply=reply, extra_env={
        "JARVIS_WORKSPACE_ROOT": str(self.workspace),
    })
    stack._httpd.goal_worker.reviewer = ApprovingReviewer()
    return stack


def prepare_workspace(ws):
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "sum.py").write_text(BROKEN_SUM)
    (ws / "notes.txt").write_text("module notes: all checks pass\n")


@SKIP_BROWSER
class ReadOnlyEvidenceBrowserTests(BrowserCase):
    REPLY = json.dumps(READ_PLAN)
    canary = "BROWSER-VERIFY-CANARY-QV1"

    def make_stack(self, reply):
        return _stack(self, reply)

    def open_console(self):
        self.page.evaluate("openGoalsModal()")
        self.page.wait_for_selector("#modal-goals[open]")

    def test_read_only_goal_shows_file_evidence(self):
        self.open_console()
        self.page.fill("#goal-objective", self.canary)
        self.page.click("#goal-submit")
        self.page.wait_for_function(
            "() => { const c = document.querySelector("
            "'#goals-list .goal-item .chip');"
            "return c && c.textContent.indexOf('succeeded') >= 0 }",
            timeout=40000)
        self.page.click("#goals-list .goal-item .goal-open")
        self.page.wait_for_selector("#goal-detail:not([hidden])")
        text = self.page.inner_text("#goal-detail")
        self.assertIn("workspace.read_file", text)
        self.assertIn("module notes: all checks pass", text)
        self.assertIn("sha256", text)


@SKIP_BROWSER
class ExecuteRecipeBrowserTests(BrowserCase):
    REPLY = json.dumps(RUN_PLAN)
    canary = "BROWSER-VERIFY-CANARY-QV2"

    def make_stack(self, reply):
        return _stack(self, reply)

    def open_console(self):
        self.page.evaluate("openGoalsModal()")
        self.page.wait_for_selector("#modal-goals[open]")

    def open_detail(self):
        self.page.click("#goals-list .goal-item .goal-open")
        self.page.wait_for_selector("#goal-detail:not([hidden])")

    def wait_status(self, containing, timeout=40000):
        self.page.wait_for_function(
            "(needle) => { const c = document.querySelector("
            "'#goals-list .goal-item .chip');"
            "return c && c.textContent.toLowerCase()"
            ".indexOf(needle.toLowerCase()) >= 0 }",
            arg=containing, timeout=timeout)

    def submit(self):
        self.open_console()
        self.page.fill("#goal-objective", self.canary)
        self.page.click("#goal-submit")
        self.wait_status("awaiting approval")
        self.open_detail()
        self.page.wait_for_selector(".goal-proposals")

    def approve_all_visible(self):
        for _ in range(6):
            clicked = self.page.evaluate(
                """() => { const row = [...document.querySelectorAll(
                    '.goal-proposals .goal-proposal')].find(
                    p => p.querySelector('.btn-approve'));
                    if (!row) return false;
                    row.querySelector('.btn-approve').click();
                    return true }""")
            if not clicked:
                break
            self.page.wait_for_timeout(400)

    def test_execute_proposal_previews_recipe_metadata(self):
        self.submit()
        panel = self.page.inner_text(".goal-proposals").lower()
        self.assertIn("#2 execute tests — proposed", panel)
        self.assertIn("tests/test_sum.py", panel)
        self.assertFalse((self.workspace / "tests/test_sum.py").exists())
        # the goal list polls and re-renders; retry clicking until the
        # execute preview (async fetch) stays visible with content
        def visible_preview_text():
            return "\n".join(self.page.evaluate(
                """() => [...document.querySelectorAll(
                    '.goal-proposal-preview')]
                    .filter(b => !b.hidden)
                    .map(b => b.textContent)"""))

        for _ in range(20):
            self.page.evaluate(
                """() => { const row = [...document.querySelectorAll(
                    '.goal-proposals .goal-proposal')].find(
                    p => p.textContent.indexOf('execute tests') >= 0);
                if (!row) return false;
                const box = row.querySelector('.goal-proposal-preview');
                if (box && !box.hidden && box.textContent.indexOf(
                        'Verification recipe') >= 0) return false;
                row.querySelector('button').click();
                return true }""")
            self.page.wait_for_timeout(300)
            if "Verification recipe" in visible_preview_text():
                break
        preview = visible_preview_text()
        self.assertIn("Verification recipe", preview)
        self.assertIn("unittest", preview)
        self.assertIn("Executes project code: yes", preview)
        self.assertIn("no OS sandbox is enforced", preview)

    def test_execute_approval_runs_honestly_when_tests_fail(self):
        # the module is broken on purpose; the approved run must fail
        # and the bounded repair attempt gets a canned unparseable
        # reply, so the goal blocks honestly (never a fake success)
        import copy
        base = self.stack.upstream.response[1]
        not_a_plan = copy.deepcopy(base)
        not_a_plan["choices"][0]["message"]["content"] = "not a plan"
        not_a_plan2 = copy.deepcopy(not_a_plan)
        # script order: chat 1 -> the valid plan; the repair chats get
        # unparseable replies (planner retries once per failure round)
        self.stack.upstream.chat_responses = [
            (200, copy.deepcopy(base), "application/json"),
            (200, not_a_plan, "application/json"),
            (200, not_a_plan2, "application/json"),
        ]
        self.submit()
        self.approve_all_visible()
        self.wait_status("blocked", timeout=60000)
        # the detail panel lags one poll behind the chip
        self.page.wait_for_function(
            "() => document.querySelector('#goal-detail') && "
            "document.querySelector('#goal-detail').textContent.indexOf("
            "'AssertionError') >= 0", timeout=40000)
        self.assertTrue((self.workspace / "tests/test_sum.py").is_file())
        text = self.page.inner_text("#goal-detail")
        self.assertIn("workspace.run_project_tests", text)
        self.assertIn("AssertionError", text)
        self.assertIn("Repair cycles used", text)
        self.assertIn("1 of 2", text)
