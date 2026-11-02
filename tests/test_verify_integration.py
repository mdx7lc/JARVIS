"""Phase 10B integration tests: read tool + verification recipes
driving real goals through the API + worker, including the bounded
verification-driven repair loop.

Temp db + temp workspace + fake upstream (no live inference); the
worker is driven synchronously; recipes run REAL subprocesses against
disposable workspaces only.
"""

import json
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

import config
import db
import server
import verification
from fake_upstream import FakeUpstream

KEY = "test-key-123"
MODEL = "qwen3.8-27b"

BROKEN_SUM = "def add(a, b):\n    return a - b\n"
FIXED_SUM = "def add(a, b):\n    return a + b\n"
FAILING_TESTS = (
    "import unittest\n"
    "from src.sum import add\n"
    "class SumTests(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)
# unchanged from FAILING_TESTS — the fix happens in src/sum.py


def _step(purpose, tool, args, criterion, depends_on=None):
    return {
        "purpose": purpose,
        "tool": tool,
        "args": args,
        "depends_on": depends_on or [],
        "acceptance": criterion,
    }


READ_TEXT = (
    "self review notes:\n"
    "the module and its documentation both read fine\n"
    "the workspace inspection finished\n"
    "done\n"
)


def READ_NOTES_PLAN():
    return {
        "summary": "summarizing: module and docs read fine",
        "outcome": "Workspace notes inspected and verified.",
        "criteria": ["notes file inspected"],
        "risk": "low",
        "uncertainties": [],
        "steps": [
            _step("inspect notes", "workspace.read_file",
                  {"path": "notes.txt"}, "notes file inspected"),
        ],
    }


def list_and_read_plan():
    return {
        "summary": "summarizing: module and docs read fine",
        "outcome": "Workspace notes inspected.",
        "criteria": ["notes file inspected"],
        "risk": "low",
        "uncertainties": [],
        "steps": [
            _step("list the workspace", "workspace.list_files", {},
                  "entries listed"),
            _step("inspect notes", "workspace.read_file",
                  {"path": "notes.txt"}, "notes file inspected"),
        ],
    }


def create_run_plan(test_rel="tests/test_sum.py"):
    return {
        "summary": "prove the existing sum module with tests",
        "outcome": "The unittest for src.sum runs and passes.",
        "criteria": ["unittest passes for the sum module"],
        "risk": "low",
        "uncertainties": [],
        "steps": [
            _step("create the tests", "workspace.create_file",
                  {"path": test_rel, "content": FAILING_TESTS},
                  "tests created"),
            _step("run the tests", "workspace.run_project_tests",
                  {"path": test_rel},
                  "unittest passes for the sum module"),
        ],
    }


def fix_and_test_plan():
    return {
        "summary": "correct add() and re-run the same tests",
        "outcome": "add() returns the sum and the unittest passes.",
        "criteria": ["unittest passes for the sum module"],
        "risk": "low",
        "uncertainties": [],
        "steps": [
            _step("fix the module", "workspace.edit_file",
                  {"path": "src/sum.py", "new_text": FIXED_SUM,
                   "expected_before_sha256":
                       {"__sha__": "src/sum.py"}},
                  "module fixed"),
            _step("re-run the tests", "workspace.run_project_tests",
                  {"path": "tests/test_sum.py"},
                  "unittest passes for the sum module"),
        ],
    }


def js_syntax_plan():
    return {
        "summary": "create an app and check it parses",
        "outcome": "app.js parses with a syntax check.",
        "criteria": ["javascript syntax check ran"],
        "risk": "low",
        "uncertainties": [],
        "steps": [
            _step("create the app", "workspace.create_file",
                  {"path": "app.js", "content": "var hi = 'x';\n"},
                  "app created"),
            _step("run the syntax check",
                  "workspace.check_javascript_syntax",
                  {"path": "app.js"}, "javascript syntax check ran"),
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"null")
        except ValueError:
            return err.code, None


class ApprovalApprover:
    def review(self, plan, registry, objective="", local_check=None):
        return {"status": "approved", "source": "external_review",
                "identity": "fake-approver", "verdict": "approved",
                "risk_level": "low", "findings": [], "missing_criteria": [],
                "recommended_changes": [], "reviewed_at": "now"}


def sha_of(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def replace_sha(plan, workspace):
    """Resolve {"__sha__": path} markers into the file's current sha."""
    for step in plan["steps"]:
        args = step.get("args") or {}
        marker = args.get("expected_before_sha256")
        if isinstance(marker, dict) and "__sha__" in marker:
            rel = marker["__sha__"]
            import hashlib
            args["expected_before_sha256"] = hashlib.sha256(
                (workspace / rel).read_bytes()).hexdigest()
    return plan


class VerifyHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-verify-")
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir()
        self.db_path = Path(self._tmp.name) / "jarvis.db"
        self.upstream = FakeUpstream(
            response=(200, plan_completion("{}"), "application/json"))
        self.upstream.start(self)
        self.prime_plans()
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": KEY,
            "JARVIS_WORKSPACE_ROOT": str(self.workspace),
        }
        self.cfg = config.load(environ=env)
        self._start_server(init_db=True)
        self.goal_worker.reviewer = ApprovalApprover()

    def _start_server(self, init_db):
        self._httpd = server.create_server(
            "127.0.0.1", 0, self.db_path, init_db=init_db,
            cfg=self.cfg, start_worker=False,
            settings_dir=str(self._tmp.name))
        thread = threading.Thread(target=self._httpd.serve_forever,
                                  daemon=True)
        thread.start()
        self.addCleanup(self._stop_http)
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self.goal_worker = self._httpd.goal_worker
        self.goal_worker.reviewer = ApprovalApprover()

    def _stop_http(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def prime_plans(self, *plans):
        self.upstream.chat_responses = [
            (200, plan_completion(json.dumps(
                replace_sha(plan, self.workspace))),
             "application/json")
            for plan in plans
        ]

    def get(self, path):
        return _request(self.base + path, method="GET")

    def post(self, path, payload=None):
        return _request(self.base + path, payload, "POST")

    def create_goal(self, objective="verify the workspace code"):
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

    def approve_all(self, goal_id):
        results = []
        for proposal in self.proposals(goal_id):
            if proposal["state"] != "proposed":
                continue
            status, body = self.post(
                f"/api/goals/{goal_id}/approve",
                {"seq": proposal["seq"],
                 "digest": proposal["digest"]})
            results.append((proposal["seq"], status, body))
        return results

    def events_of(self, goal_id):
        return [e["event"] for e in self.detail(goal_id)["events"]]


class ReadOnlyReadToolFlowTests(VerifyHarness):
    def test_read_only_goal_succeeds_with_evidence(self):
        (self.workspace / "notes.txt").write_text(READ_TEXT)
        self.prime_plans(READ_NOTES_PLAN())
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "succeeded", detail)
        step = next(s for s in detail["steps"]
                    if s["tool"] == "workspace.read_file")
        result = step["result"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["content"], READ_TEXT)
        self.assertFalse(result["truncated"])
        self.assertTrue(result["sha256"])
        # no proposals were ever needed: read tools are autonomous
        self.assertEqual(self.proposals(goal_id), [])

    def test_read_file_rides_along_in_step_manifest(self):
        (self.workspace / "notes.txt").write_text(READ_TEXT)
        self.prime_plans(list_and_read_plan())
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "succeeded", detail)
        tools_used = [s["tool"] for s in detail["steps"]]
        self.assertEqual(tools_used,
                         ["workspace.list_files", "workspace.read_file"])

    def test_read_of_missing_file_gives_honest_error(self):
        self.prime_plans(READ_NOTES_PLAN())
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "failed", detail)
        self.assertIn("no_such_path", json.dumps(detail["steps"]))


class VerifyRecipeGoalTests(VerifyHarness):
    def test_create_and_unit_testing_goal_succeeds(self):
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "sum.py").write_text(BROKEN_SUM)
        (self.workspace / "tests").mkdir()
        # round 1 fails on purpose (BROKEN_SUM); the repair round fixes
        # the module and the same unittest passes
        self.prime_plans(create_run_plan(), fix_and_test_plan())
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        self.assertEqual(self.detail(goal_id)["status"],
                         "awaiting_approval")
        states = [(p["seq"], p["kind"]) for p in self.proposals(goal_id)]
        self.assertEqual(states, [(1, "create"), (2, "execute")])
        self.approve_all(goal_id)
        self.goal_worker.process_once(goal_id)
        # repair round: fresh approval required for the new plan
        self.assertEqual(self.detail(goal_id)["status"],
                         "awaiting_approval")
        self.assertEqual([(p["seq"], p["kind"])
                          for p in self.proposals(goal_id)],
                         [(1, "edit"), (2, "execute")])
        self.approve_all(goal_id)
        self.goal_worker.process_once(goal_id)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertEqual(detail["repair_count"], 1)
        self.assertEqual(
            (self.workspace / "src" / "sum.py").read_text(), FIXED_SUM)
        step = next(s for s in detail["steps"]
                    if s["tool"] == "workspace.run_project_tests"
                    and s["status"] == "done")
        result = step["result"]
        self.assertEqual(result["recipe"], "run_project_tests")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("OK", result["stderr"])

    def test_execute_preview_carries_recipe_metadata(self):
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "sum.py").write_text(BROKEN_SUM)
        (self.workspace / "tests").mkdir()
        self.prime_plans(create_run_plan())
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        proposals = self.proposals(goal_id)
        preview_seq = next(p["seq"] for p in proposals
                           if p["kind"] == "execute")
        status, body = self.get(
            f"/api/goals/{goal_id}/proposals/{preview_seq}")
        self.assertEqual(status, 200, body)
        preview = body["preview"]
        self.assertEqual(preview["kind"], "execute")
        info = preview["verification"]
        self.assertIs(info["executes_project_code"], True)
        self.assertIn("risk", info)
        self.assertIn("cwd", info)
        self.assertIn("timeout_seconds", info)
        self.assertIn("runner", info)
        self.assertIn("target", info)


class RepairLoopTests(VerifyHarness):
    def setUp(self):
        super().setUp()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "sum.py").write_text(BROKEN_SUM)
        (self.workspace / "tests").mkdir()

    def prime_repair(self):
        self.prime_plans(create_run_plan(), fix_and_test_plan())

    def test_failing_tests_trigger_repair_then_succeed(self):
        self.prime_repair()
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        self.assertEqual(self.detail(goal_id)["status"],
                         "awaiting_approval", self.proposals(goal_id))
        self.assertEqual([(p["seq"], p["kind"])
                          for p in self.proposals(goal_id)],
                         [(1, "create"), (2, "execute")])
        self.approve_all(goal_id)
        self.goal_worker.process_once(goal_id)
        # repair round: the fix plan needs its own approvals
        self.assertEqual(self.detail(goal_id)["status"],
                         "awaiting_approval")
        self.assertEqual([(p["seq"], p["kind"])
                          for p in self.proposals(goal_id)],
                         [(1, "edit"), (2, "execute")])
        self.approve_all(goal_id)
        self.goal_worker.process_once(goal_id)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertEqual(detail["repair_count"], 1)
        # the fix landed on disk
        self.assertEqual(
            (self.workspace / "src" / "sum.py").read_text(), FIXED_SUM)
        # the failing attempt is preserved honestly
        last = detail["last_failure"]
        self.assertIn("seq", last)
        self.assertEqual(last["tool"], "workspace.run_project_tests")
        self.assertEqual(last["status"], "failed")
        self.assertIn("AssertionError", last["stderr"])
        self.assertEqual(
            [s["tool"] for s in detail["steps"]],
            ["workspace.edit_file", "workspace.run_project_tests"])
        self.assertEqual(
            [s["status"] for s in detail["steps"]],
            ["done", "done"])
        events = self.events_of(goal_id)
        self.assertIn("verification_failed", events)
        self.assertIn("repair_started", events)
        self.assertIn("proposals_superseded", events)
        # the second round of proposals replaced the first (superseded
        # approvals are removed; only the new plan's bids remain)
        self.assertEqual(
            [(p["seq"], p["kind"], p["state"])
             for p in self.proposals(goal_id)],
            [(1, "edit", "executed"), (2, "execute", "executed")])

    def test_repair_of_failing_test_counted_and_recorded(self):
        self.prime_repair()
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        self.approve_all(goal_id)
        self.goal_worker.process_once(goal_id)
        self.approve_all(goal_id)
        self.goal_worker.process_once(goal_id)
        # verify the persisted step rows via a restart server
        self._httpd.shutdown()
        self._httpd.server_close()
        self._start_server(init_db=False)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "succeeded")
        self.assertEqual(detail["repair_count"], 1)
        self.assertIsNotNone(detail["last_failure"])
        # no new executions: the evidence is preserved as-is (the goal
        # keeps only the active plan's step rows)
        self.assertEqual([s["attempts"] for s in detail["steps"]],
                         [1, 1])

    def test_repair_budget_exhausted_then_blocked(self):
        self.prime_plans(
            create_run_plan("tests/t1.py"),
            create_run_plan("tests/t2.py"),
            create_run_plan("tests/t3.py"),
        )
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        for _ in range(4):
            self.approve_all(goal_id)
            self.goal_worker.process_once(goal_id)
            if self.detail(goal_id)["status"] == "blocked":
                break
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "blocked", detail)
        self.assertEqual(detail["error_code"],
                         "repair_budget_exhausted")
        self.assertEqual(detail["repair_count"], 2)

    def test_verification_unavailable_blocks_without_silent_success(self):
        self.prime_plans(js_syntax_plan())
        with unittest.mock.patch.object(verification.shutil, "which",
                                        return_value=None):
            goal_id = self.create_goal()
            self.goal_worker.process_once(goal_id)
            self.approve_all(goal_id)
            self.goal_worker.process_once(goal_id)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "blocked", detail)
        self.assertEqual(detail["error_code"], "verification_unavailable")
        self.assertEqual(detail["repair_count"], 0)
        # the approved file write still happened
        self.assertTrue((self.workspace / "app.js").is_file())
        self.assertEqual(
            [s["tool"] for s in detail["steps"]],
            ["workspace.create_file", "workspace.check_javascript_syntax"])

    def test_bad_plan_is_refused_before_any_execution(self):
        plan = create_run_plan()
        plan["steps"] = [_step("missing", "workspace.create_file",
                               {"path": ".env", "content": "x"},
                               "should fail")]
        self.prime_plans(plan)
        goal_id = self.create_goal()
        self.goal_worker.process_once(goal_id)
        detail = self.detail(goal_id)
        self.assertEqual(detail["status"], "blocked", detail)
        self.assertEqual(detail["error_code"], "invalid_plan")
        self.assertEqual(detail["repair_count"], 0)
        self.assertFalse((self.workspace / ".env").exists())
