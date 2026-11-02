"""Phase 8B integration tests: goal API + execution worker.

Everything runs against the fake upstream (no live inference), a temp
DB, and a temp workspace. The canned plan below is a test fixture that
satisfies the planner contract — it is not evidence of real planning.
"""

import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import config
import db
import server
import tools
from fake_upstream import FakeUpstream
from reviewer import UnavailableReviewer

KEY = "test-key-123"
MODEL = "qwen3.8-27b"
CANARY_OBJECTIVE = "CANARY-OBJECTIVE-QX7 inventory the workspace now"

CRITERION = "accurate inventory of the workspace files"

# Test fixture (satisfies validate_plan + verification by token overlap).
PLAN_TEXT = json.dumps({
    "summary": "Inventory the workspace",
    "outcome": "A workspace inventory is recorded and reported.",
    "criteria": [CRITERION],
    "risk": "low",
    "uncertainties": [],
    "steps": [{
        "purpose": "List the workspace files accurately",
        "tool": "workspace.list_files",
        "args": {"path": ""},
        "depends_on": [],
        "acceptance": CRITERION,
    }],
})
PLANNER_COMPLETION = {
    "id": "chatcmpl-plan",
    "object": "chat.completion",
    "model": MODEL,
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": PLAN_TEXT},
        "finish_reason": "stop",
    }],
}
GARBAGE_REPLY = "I will simply list your files right away, trust me."
BAD_PLAN_REPLY = json.dumps({"summary": "no risk or steps here"})


def _request(url, payload=None, method="POST", origin=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None \
        else None
    req = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    if origin is not None:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read() or b"null")
        except ValueError:
            return err.code, None


class RejectingReviewer:
    def review(self, plan, registry, objective="", local_check=None):
        return {"status": "rejected", "verdict": "rejected",
                "identity": "fake-rejecter"}


class ApprovingReviewer:
    def review(self, plan, registry, objective="", local_check=None):
        return {"status": "approved",
                "source": "external_review",
                "identity": "fake-approver", "verdict": "approved",
                "risk_level": "low", "findings": [], "missing_criteria": [],
                "recommended_changes": [], "reviewed_at": "now"}


class ReviseThenApproveReviewer:
    """Round 1 requests revisions; round 2 approves."""

    def __init__(self):
        self._calls = 0

    def review(self, plan, registry, objective="", local_check=None):
        self._calls += 1
        if self._calls == 1:
            return {"status": "revise", "source": "external_review",
                    "verdict": "revise", "identity": "fake-reviser",
                    "risk_level": "medium",
                    "findings": ["step 2 duplicates step 1"],
                    "missing_criteria": [],
                    "recommended_changes": ["merge the duplicate steps"],
                    "reviewed_at": "t1"}
        return {"status": "approved", "source": "external_review",
                "verdict": "approved", "identity": "fake-approver",
                "risk_level": "low", "findings": [],
                "missing_criteria": [], "recommended_changes": [],
                "reviewed_at": "t2"}


class ReviseForeverReviewer:
    def review(self, plan, registry, objective="", local_check=None):
        return {"status": "revise", "source": "external_review",
                "verdict": "revise", "identity": "fake-reviser",
                "risk_level": "medium",
                "findings": ["not thorough enough"],
                "missing_criteria": [],
                "recommended_changes": ["be more thorough"],
                "reviewed_at": "loop"}


class GoalsHarness(unittest.TestCase):
    """Common setup: temp db, temp workspace, fake upstream, server with
    the worker constructable but NOT auto-started (tests drive
    process_once synchronously)."""

    upstream_reply = (200, PLANNER_COMPLETION, "application/json")
    start_polling_worker = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-goals-")
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "docs").mkdir()
        (self.workspace / "src" / "app.py").write_text("x = 1\n")
        (self.workspace / "docs" / "readme.md").write_text("# docs\n")
        self.db_path = Path(self._tmp.name) / "jarvis.db"
        self.upstream = FakeUpstream(response=self.upstream_reply)
        self.upstream.start(self)
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": KEY,
            "JARVIS_WORKSPACE_ROOT": str(self.workspace),
        }
        self.cfg = config.load(environ=env)
        self._start_server()

    def _start_server(self, init_db=True):
        import http.server  # noqa: F401  (server module imports it)
        httpd = server.create_server(
            "127.0.0.1", 0, self.db_path, init_db=init_db, cfg=self.cfg,
            start_worker=self.start_polling_worker,
            settings_dir=str(self._tmp.name))
        self._httpd = httpd
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self._srv_thread = threading.Thread(
            target=httpd.serve_forever, daemon=True)
        self._srv_thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._srv_thread.join(timeout=5)

    # helpers ------------------------------------------------------------

    def post(self, path, payload=None, origin=None):
        status, body = _request(self.base + path, payload, "POST", origin)
        return status, body

    def get(self, path):
        status, body = _request(self.base + path, method="GET")
        return status, body

    def create_goal(self, objective=CANARY_OBJECTIVE):
        status, body = self.post("/api/goals", {"objective": objective})
        self.assertEqual(status, 202, body)
        return body["id"]

    def drive(self, goal_id):
        self._httpd.goal_worker.process_once(goal_id)

    def drive_to_terminal(self, goal_id, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drive(goal_id)
            _, body = self.get(f"/api/goals/{goal_id}")
            if body["status"] in ("succeeded", "failed", "cancelled",
                                  "blocked"):
                return body
        self.fail("goal did not reach a terminal state")

    def detail(self, goal_id):
        status, body = self.get(f"/api/goals/{goal_id}")
        self.assertEqual(status, 200, body)
        return body

    def forge_state(self, goal_id, status):
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.update_goal_fields(
                conn, goal_id, "queued", {"status": status}))
        finally:
            conn.close()

    def forge_steps(self, goal_id, steps):
        conn = db.open_conn(self.db_path)
        try:
            db.replace_goal_steps(conn, goal_id, steps)
        finally:
            conn.close()

    def forge_step_status(self, goal_id, seq, expected, new, fields=None):
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.update_goal_step_status(
                conn, goal_id, seq, expected, new, fields))
        finally:
            conn.close()


class GoalAPITests(GoalsHarness):
    def test_create_returns_202_queued_without_blocking(self):
        status, body = self.post("/api/goals",
                                 {"objective": CANARY_OBJECTIVE})
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "queued")
        self.assertTrue(body["id"])
        self.assertTrue(body["created_at"])

    def test_updated_at_changes_on_transition(self):
        goal_id = self.create_goal()
        self.drive_to_terminal(goal_id)
        detail = self.detail(goal_id)
        self.assertGreaterEqual(detail["updated_at"],
                                detail["created_at"])

    def test_validation_rejects_bad_payloads(self):
        status, body = self.post("/api/goals", {})
        self.assertEqual((status, body["error"]["code"]),
                         (400, "invalid_objective"))
        status, body = self.post("/api/goals", {"objective": 42})
        self.assertEqual((status, body["error"]["code"]),
                         (400, "invalid_objective"))
        status, body = self.post("/api/goals", {"objective": "   "})
        self.assertEqual((status, body["error"]["code"]),
                         (400, "invalid_objective"))
        status, body = self.post("/api/goals",
                                 {"objective": CANARY_OBJECTIVE,
                                  "priority": "high"})
        self.assertEqual((status, body["error"]["code"]),
                         (400, "invalid_request"))
        status, body = self.post(
            "/api/goals", {"objective": "x" * 501})
        self.assertEqual((status, body["error"]["code"]),
                         (400, "objective_too_long"))

    def test_cross_origin_rejected(self):
        status, body = self.post(
            "/api/goals", {"objective": CANARY_OBJECTIVE},
            origin="https://evil.example")
        self.assertEqual((status, body["error"]["code"]),
                         (403, "cross_origin_forbidden"))
        status, body = self.get("/api/goals?limit=5")
        self.assertEqual(status, 200)  # GETs without headers are fine

    def test_rate_limit_gates_goal_creation(self):
        self._httpd.rate_limiter.add_policy("goals", 0, 60.0)
        status, body = self.post("/api/goals",
                                 {"objective": "one"})
        self.assertEqual(status, 429, body)

    def test_goals_list_shape_and_limit_validation(self):
        self.create_goal()
        status, body = self.get("/api/goals")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["goals"]), 1)
        row = body["goals"][0]
        for key in ("id", "objective", "status", "error_code",
                    "cancel_requested", "created_at", "updated_at",
                    "progress"):
            self.assertIn(key, row)
        self.assertEqual(row["progress"]["total"], 0)
        status, body = self.get("/api/goals?limit=0")
        self.assertEqual((status, body["error"]["code"]),
                         (400, "invalid_request"))
        status, body = self.get("/api/goals?limit=999999")
        self.assertEqual((status, body["error"]["code"]),
                         (400, "invalid_request"))
        status, body = self.get("/api/goals?limit=51")
        self.assertEqual((status, body["error"]["code"]),
                         (400, "invalid_request"))
        status, body = self.get("/api/goals?limit=abc")
        self.assertEqual((status, body["error"]["code"]),
                         (400, "invalid_request"))

    def test_detail_and_404_and_id_guard(self):
        goal_id = self.create_goal()
        detail = self.detail(goal_id)
        self.assertEqual(detail["objective"], CANARY_OBJECTIVE)
        self.assertEqual(detail["plan"], {})
        self.assertEqual(detail["report"], {})
        self.assertEqual(detail["steps"], [])
        self.assertEqual(detail["progress"]["total"], 0)
        self.assertTrue(detail["events"])
        status, body = self.get("/api/goals/zzzz")
        self.assertEqual((status, body["error"]["code"]),
                         (404, "goal_not_found"))
        status, body = self.get("/api/goals/%s" % ("a" * 32))
        self.assertEqual((status, body["error"]["code"]),
                         (404, "goal_not_found"))

    def test_other_goal_mutations_are_405(self):
        goal_id = self.create_goal()
        status, body = self.post(f"/api/goals/{goal_id}")
        self.assertEqual(status, 405)
        status, body = self.post(f"/api/goals/{goal_id}/restart")
        self.assertEqual(status, 405)

    def test_cancel_queued_immediately_and_rejects_after(self):
        goal_id = self.create_goal()
        status, body = self.post(f"/api/goals/{goal_id}/cancel")
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "cancelled")
        first_events = len(self.detail(goal_id)["events"])
        # already terminal: cancel/retry are rejected, worker no-ops
        status, body = self.post(f"/api/goals/{goal_id}/cancel")
        self.assertEqual((status, body["error"]["code"]),
                         (409, "invalid_state"))
        status, body = self.post(f"/api/goals/{goal_id}/retry")
        self.assertEqual((status, body["error"]["code"]),
                         (409, "invalid_state"))
        self.drive(goal_id)
        self.assertEqual(len(self.detail(goal_id)["events"]),
                         first_events)
        self.assertEqual(self.detail(goal_id)["status"], "cancelled")

    def test_cancel_unknown_goal_404(self):
        status, body = self.post("/api/goals/%s/cancel" % ("b" * 32))
        self.assertEqual((status, body["error"]["code"]),
                         (404, "goal_not_found"))

    def test_retry_only_reachably_blocked(self):
        goal_id = self.create_goal()
        status, body = self.post(f"/api/goals/{goal_id}/retry")
        self.assertEqual((status, body["error"]["code"]),
                         (409, "invalid_state"))

    def test_not_configured_503(self):
        env = {
            "JARVIS_DB_PATH": str(Path(self._tmp.name) / "other.db"),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": "",
        }
        cfg = config.load(environ=env)
        httpd = server.create_server(
            "127.0.0.1", 0, env["JARVIS_DB_PATH"], cfg=cfg,
            start_worker=False, settings_dir=str(self._tmp.name))
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = _request(base + "/api/goals",
                                    {"objective": "test goal"})
            self.assertEqual((status, body["error"]["code"]),
                             (503, "not_configured"))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


class GoalPipelineTests(GoalsHarness):
    """Full synchronous pipelines through process_once."""

    def test_full_pipeline_to_succeeded(self):
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "succeeded")
        detail = self.detail(goal_id)
        plan = detail["plan"]
        self.assertEqual(plan["risk"], "low")
        self.assertEqual(plan["criteria"], [CRITERION])
        self.assertEqual(plan["summary"], "Inventory the workspace")
        steps = detail["steps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["seq"], 1)
        self.assertEqual(steps[0]["tool"], "workspace.list_files")
        self.assertEqual(steps[0]["status"], "done")
        self.assertGreaterEqual(steps[0]["attempts"], 1)
        self.assertIn("src/app.py", json.dumps(steps[0]["result"]))
        self.assertIn("entries", steps[0]["verification"].lower())
        result = steps[0]["result"]
        self.assertEqual(result["tool"], "workspace.list_files")
        self.assertFalse(result["truncated"])
        report = detail["report"]
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["steps"][0]["status"], "done")
        self.assertTrue(all(c["passed"] for c in report["criteria"]))
        self.assertEqual(report["review"], "local_validation")
        self.assertEqual(report["uncertainties"], [])
        names = [e["event"] for e in detail["events"]]
        for expected in ("goal_created", "planning_started",
                         "safety_checked", "plan_ready", "step_started",
                         "step_done", "goal_succeeded"):
            self.assertIn(expected, names, expected)
        # real files from the temp workspace got listed (truth, not
        # fabricated)
        self.assertEqual(detail["progress"]["done"], 1)
        self.assertTrue(any(
            "readme" in e["detail"] or "entries" in e["detail"]
            for e in detail["events"]))

    def test_logs_are_json_and_canary_free(self):
        captured = io.StringIO()
        goal_id = self.create_goal()
        with contextlib.redirect_stderr(captured):
            self.drive_to_terminal(goal_id)
            self.post(f"/api/goals/{goal_id}/cancel")
            _request(self.base + "/api/goals", method="GET")
        out = captured.getvalue()
        for line in out.strip().splitlines():
            parsed = json.loads(line)  # every log line is structured
            self.assertNotIn("error", parsed)  # no internal failure noise
        self.assertNotIn(CANARY_OBJECTIVE, out)
        self.assertNotIn(CRITERION, out)

    def test_planning_provider_failure_fails_honestly(self):
        self.upstream.response = (500, b'{"error":"no"}',
                                  "application/json")
        self.upstream.chat_responses = []
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["error_code"], "upstream_error")
        report = self.detail(goal_id)["report"]
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_code"], "upstream_error")
        self.assertEqual(self.detail(goal_id)["progress"]["total"], 0)

    def test_invalid_then_valid_plan_needs_one_repair(self):
        # script: garbage first, valid plan second
        valid = (200, PLANNER_COMPLETION, "application/json")
        garbage = (200, dict(
            PLANNER_COMPLETION,
            choices=[{"message": {"role": "assistant",
                                  "content": GARBAGE_REPLY}}],
        ), "application/json")
        self.upstream.chat_responses = [garbage, valid]
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(len(self.upstream.requests), 2)

    def test_invalid_plan_after_repair_is_blocked(self):
        garbage = (200, dict(
            PLANNER_COMPLETION,
            choices=[{"message": {"role": "assistant",
                                  "content": GARBAGE_REPLY}}],
        ), "application/json")
        badshape = (200, dict(
            PLANNER_COMPLETION,
            choices=[{"message": {"role": "assistant",
                                  "content": BAD_PLAN_REPLY}}],
        ), "application/json")
        self.upstream.chat_responses = [garbage, badshape]
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "blocked")
        self.assertEqual(final["error_code"], "invalid_plan")
        report = self.detail(goal_id)["report"]
        self.assertEqual(report["status"], "blocked")
        # exactly two planner attempts were consumed
        self.assertEqual(len(self.upstream.requests), 2)
        # and retry re-queues the whole planning attempt
        status, body = self.post(f"/api/goals/{goal_id}/retry")
        self.assertEqual(status, 202)
        self.assertEqual(body["status"], "queued")
        self.upstream.chat_responses = []
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "succeeded")

    def test_unknown_tool_in_plan_is_blocked(self):
        bogus = json.loads(PLAN_TEXT)
        bogus["steps"][0]["tool"] = "shell.run"
        self.upstream.chat_responses = [
            (200, dict(PLANNER_COMPLETION, choices=[
                {"message": {"role": "assistant",
                             "content": json.dumps(bogus)}}]),
             "application/json")] * 2
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "blocked")
        self.assertEqual(final["error_code"], "unsupported_capability")

    def test_rejecting_reviewer_blocks_without_execution(self):
        self._httpd.goal_worker.reviewer = RejectingReviewer()
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "blocked")
        self.assertEqual(final["error_code"], "review_rejected")
        # no steps were ever persisted or executed
        self.assertEqual(self.detail(goal_id)["steps"], [])

    def test_approving_reviewer_recorded_in_report(self):
        self._httpd.goal_worker.reviewer = ApprovingReviewer()
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "succeeded")
        report = self.detail(goal_id)["report"]
        # Phase 9B provenance: the source category + the identity the
        # reviewer actually reported, kept separately and truthfully.
        self.assertEqual(report["review"], "external_review")
        self.assertEqual(report["review_identity"], "fake-approver")

    def test_default_reviewer_is_unavailable_not_independent(self):
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "succeeded")
        names = [e["event"] for e in self.detail(goal_id)["events"]]
        self.assertIn("review_unavailable", names)
        self.assertEqual(
            self.detail(goal_id)["report"]["review"],
            "local_validation")
        # no reviewer identity can exist when nothing was invoked
        self.assertIsNone(
            self.detail(goal_id)["report"].get("review_identity"))

    # -- Phase 9B: revise / reject / fail-with-provenance pipelines ------

    def test_revise_then_approve_records_rounds_and_revalidation(self):
        self._httpd.goal_worker.reviewer = ReviseThenApproveReviewer()
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "succeeded")
        report = self.detail(goal_id)["report"]
        self.assertEqual(report["review"], "external_review")
        self.assertEqual(report["review_identity"], "fake-approver")
        self.assertTrue(report["plan_revised"])
        plan = self.detail(goal_id)["plan"]
        self.assertEqual(plan["review"]["round"], 2)
        self.assertEqual(plan["review"]["plan_revised"], True)
        self.assertEqual(plan["review"]["local_validation"], True)
        # exactly one extra planner call (the revision), same upstream
        planner_calls = [q for q in self.upstream.requests
                         if q["path"].startswith("/v1/chat")]
        self.assertEqual(len(planner_calls), 2)

    def test_revise_forever_exhausts_budget_and_blocks(self):
        self._httpd.goal_worker.reviewer = ReviseForeverReviewer()
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "blocked")
        self.assertEqual(final["error_code"], "review_rejected")
        # never executed: no steps, no verify phase
        self.assertEqual(self.detail(goal_id)["steps"], [])
        review = self.detail(goal_id)["plan"]["review"]
        self.assertEqual(review["status"], "revise")
        self.assertEqual(review["round"], 2)
        self.assertTrue(review["findings"])
        # initial planner call + one revision call only (budget: 2)
        planner_calls = [q for q in self.upstream.requests
                         if q["path"].startswith("/v1/chat")]
        self.assertEqual(len(planner_calls), 2)

    def test_rejected_round_one_blocks_with_review_provenance(self):
        self._httpd.goal_worker.reviewer = RejectingReviewer()
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "blocked")
        review = self.detail(goal_id)["plan"]["review"]
        self.assertEqual(review["status"], "rejected")
        self.assertEqual(review["identity"], "fake-rejecter")
        self.assertEqual(review["round"], 1)
        self.assertEqual(final["error_code"], "review_rejected")

    def test_review_failed_is_not_approval_but_local_only(self):
        class SoupReviewer:
            def review(self, plan, registry, objective="",
                       local_check=None):
                raise RuntimeError("reviewer exploded")

        self._httpd.goal_worker.reviewer = SoupReviewer()
        goal_id = self.create_goal()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "succeeded")
        names = [e["event"] for e in self.detail(goal_id)["events"]]
        self.assertIn("review_failed", names)
        # honest local-only record, no fabricated independent approval
        report = self.detail(goal_id)["report"]
        self.assertEqual(report["review"], "local_validation")
        self.assertIsNone(report.get("review_identity"))
        plan = self.detail(goal_id)["plan"]
        self.assertEqual(plan["review"]["status"], "failed")
        self.assertEqual(plan["review"]["failure_reason"],
                         "reviewer_exception")
        # the exception text never reaches the event trail
        for event in self.detail(goal_id)["events"]:
            self.assertNotIn("reviewer exploded", event.get("detail", ""))

    def test_cancel_during_review_is_honored_and_never_approved(self):
        db_path = self.db_path

        class ReviewerForestalls:
            def review(self, plan, registry, objective="",
                       local_check=None):
                # simulate an operator cancelling while review runs
                conn = db.open_conn(db_path)
                try:
                    wrote = db.update_goal_fields(
                        conn, goal_id_ref[0], "planning",
                        {"cancel_requested": 1})
                    if not wrote:
                        raise AssertionError(
                            "cancel flag write returned False")
                finally:
                    conn.close()
                return {"status": "approved", "source": "external_review",
                        "identity": "fake-approver"}

        goal_id = self.create_goal()
        goal_id_ref = [goal_id]
        self._httpd.goal_worker.reviewer = ReviewerForestalls()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "cancelled")
        self.assertNotEqual(final["status"], "succeeded")

    def test_cancel_flag_honored_at_next_checkpoint(self):
        goal_id = self.create_goal()
        # forge a mid-flight state with a cancel request pending
        self.forge_state(goal_id, "executing")
        self.forge_steps(goal_id, [
            {"purpose": "first pass", "tool": "workspace.list_files",
             "args": {"path": ""}, "depends_on": [], "acceptance": "x"},
            {"purpose": "second pass", "tool": "workspace.list_files",
             "args": {"path": "src"}, "depends_on": [1],
             "acceptance": "y"},
        ])
        conn = db.open_conn(self.db_path)
        try:
            db.update_goal_fields(
                conn, goal_id, "executing", {"cancel_requested": 1})
        finally:
            conn.close()
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "cancelled")
        steps = self.detail(goal_id)["steps"]
        self.assertEqual([s["status"] for s in steps],
                         ["pending", "pending"])
        names = [e["event"] for e in self.detail(goal_id)["events"]]
        self.assertNotIn("step_started", names)
        self.assertIn("goal_cancelled", names)

    def test_criteria_unverified_when_steps_incomplete(self):
        goal_id = self.create_goal()
        self.forge_state(goal_id, "executing")
        self.forge_steps(goal_id, [
            {"purpose": "done pass", "tool": "workspace.list_files",
             "args": {"path": ""}, "depends_on": [],
             "acceptance": CRITERION},
        ])
        try:
            self.forge_step_status(goal_id, 1, "pending", "failed",
                                   {"error_code": "tool_error"})
        finally:
            pass
        final = self.drive_to_terminal(goal_id)
        # an unfinished step never lets a goal claim success: the
        # verify phase blocks honestly (retryable) instead of failing
        self.assertEqual(final["status"], "blocked")
        self.assertEqual(final["error_code"], "criteria_unverified")
        names = [e["event"] for e in self.detail(goal_id)["events"]]
        self.assertIn("goal_blocked", names)
        self.assertNotIn("goal_succeeded", names)

    def test_verifying_with_all_steps_done_succeeds(self):
        goal_id = self.create_goal()
        self.forge_state(goal_id, "verifying")
        self.forge_steps(goal_id, [
            {"purpose": "done pass", "tool": "workspace.list_files",
             "args": {"path": ""}, "depends_on": [],
             "acceptance": CRITERION},
        ])
        result = json.dumps({
            "tool": "workspace.list_files",
            "entries": [{"path": "src/app.py", "type": "file"}],
            "count": 1, "truncated": False,
        }, ensure_ascii=True)
        self.forge_step_status(goal_id, 1, "pending", "done", {
            "result_json": result, "verification": "1 entries"})
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "succeeded")
        report = self.detail(goal_id)["report"]
        self.assertEqual(report["status"], "succeeded")
        self.assertTrue(all(c["passed"] for c in report["criteria"]))

    def test_goal_time_budget_enforced(self):
        goal_id = self.create_goal()
        self.forge_state(goal_id, "executing")
        self.forge_steps(goal_id, [
            {"purpose": "slow pass", "tool": "workspace.list_files",
             "args": {"path": ""}, "depends_on": [],
             "acceptance": CRITERION},
        ])
        worker = self._httpd.goal_worker
        worker.goal_ttl_seconds = 0  # deadline already passed
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["error_code"], "goal_time_exceeded")
        self.assertEqual(self.detail(goal_id)["steps"][0]["error_code"],
                         "goal_time_exceeded")

    def test_restart_keeps_persisted_outcome_and_no_duplication(self):
        goal_id = self.create_goal()
        self.drive_to_terminal(goal_id)
        before = self.detail(goal_id)
        self._stop_server()
        self._start_server(init_db=False)  # restart on the SAME db
        after = self.detail(goal_id)
        self.assertEqual(after["status"], "succeeded")
        self.assertEqual(after["steps"], before["steps"])
        self.assertEqual(after["plan"], before["plan"])
        self.assertEqual(len(after["events"]), len(before["events"]))
        # recover() adds nothing for terminal goals
        self._httpd.goal_worker.recover()
        self.assertEqual(self.detail(goal_id), before)
        self.assertEqual(json.dumps(self.detail(goal_id)["events"]),
                         json.dumps(before["events"]))

    def test_review_provenance_survives_restart_without_approval_drift(self):
        # rejected-by-review goal: restart must keep it blocked with the
        # same review record — recovery never upgrades to approval
        self._httpd.goal_worker.reviewer = RejectingReviewer()
        goal_id = self.create_goal()
        self.drive_to_terminal(goal_id)
        before = self.detail(goal_id)
        self.assertEqual(before["plan"]["review"]["status"], "rejected")
        self._stop_server()
        self._start_server(init_db=False)
        self._httpd.goal_worker.recover()
        after = self.detail(goal_id)
        self.assertEqual(after["status"], "blocked")
        self.assertEqual(after["plan"]["review"], before["plan"]["review"])
        self.assertEqual(after["steps"], [])
        # retry of a review-rejected goal re-plans from scratch — the
        # old review record cannot leak into the new attempt
        self._httpd.goal_worker.reviewer = UnavailableReviewer()
        status, body = self.post(f"/api/goals/{goal_id}/retry")
        self.assertEqual(status, 202)
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "succeeded")
        # fresh provenance: unavailable again this round, identity None
        self.assertEqual(final["plan"]["review"]["round"], 1)

    def test_scan_processes_queued_goals_serially(self):
        ids = [self.create_goal() for _ in range(4)]
        worker = self._httpd.goal_worker
        deadline = time.monotonic() + 20
        statuses = {}
        while time.monotonic() < deadline:
            worker.scan()
            conn = db.open_conn(self.db_path)
            try:
                statuses = {g["id"]: g["status"]
                            for g in db.list_goals(conn, 50)}
            finally:
                conn.close()
            if all(s in ("succeeded", "failed")
                   for s in statuses.values()):
                break
        self.assertEqual(
            {statuses[i] for i in ids}, {"succeeded"})

    def test_scan_recovery_of_idempotent_running_step(self):
        goal_id = self.create_goal()
        self.forge_state(goal_id, "executing")
        self.forge_steps(goal_id, [
            {"purpose": "interrupted pass",
             "tool": "workspace.list_files", "args": {"path": ""},
             "depends_on": [], "acceptance": CRITERION},
        ])
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.mark_step_running(conn, goal_id, 1))
        finally:
            conn.close()
        self._httpd.goal_worker.recover()
        names = [e["event"] for e in self.detail(goal_id)["events"]]
        self.assertIn("step_recovered", names)
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(self.detail(goal_id)["steps"][0]["attempts"], 2)

    def test_non_idempotent_running_step_needs_reconciliation(self):
        goal_id = self.create_goal()
        self.forge_state(goal_id, "executing")
        # a hypothetical non-idempotent tool registered in the registry
        fake = tools.Tool(
            "notes.write", "writes a note", {}, "autonomous", False,
            run=lambda args: {"ok": True},
            verifier=lambda args, result: {"passed": True})
        custom = tools.ToolRegistry(
            self._httpd.goal_worker.registry.all() + [fake])
        self._httpd.goal_worker.registry = custom
        self.forge_steps(goal_id, [
            {"purpose": "half-applied update", "tool": "notes.write",
             "args": {}, "depends_on": [],
             "acceptance": "notes updated"},
        ])
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.mark_step_running(conn, goal_id, 1))
        finally:
            conn.close()
        self._httpd.goal_worker.recover()
        final = self.detail(goal_id)
        self.assertEqual(final["status"], "blocked")
        self.assertEqual(final["error_code"],
                         "reconciliation_required")
        report = final["report"]
        self.assertEqual(report["status"], "blocked")
        names = [e["event"] for e in final["events"]]
        self.assertNotIn("step_recovered", names)
        # operator cannot requeue a reconciliation-required goal
        status, body = self.post(f"/api/goals/{goal_id}/retry")
        self.assertEqual((status, body["error"]["code"]),
                         (409, "invalid_state"))

    def test_step_retry_exhausted_budget(self):
        goal_id = self.create_goal()
        self.forge_state(goal_id, "executing")
        self.forge_steps(goal_id, [
            {"purpose": "claimed once already",
             "tool": "workspace.list_files", "args": {"path": ""},
             "depends_on": [], "acceptance": CRITERION},
        ])
        self.forge_step_status(goal_id, 1, "pending", "running",
                               {"attempts": 2})
        worker = self._httpd.goal_worker
        worker.max_step_attempts = 2
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["error_code"], "step_retry_exhausted")

    def test_effective_model_uses_persisted_override(self):
        goal_id = self.create_goal()
        conn = db.open_conn(self.db_path)
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO meta(key,value)"
                             " VALUES ('model_override', ?)", (MODEL,))
        finally:
            conn.close()
        self.drive_to_terminal(goal_id)
        chat_bodies = [
            r["body"] for r in self.upstream.requests
            if r["path"].endswith("/chat/completions")
            and isinstance(r.get("body"), dict)]
        self.assertTrue(chat_bodies)
        self.assertEqual({b["model"] for b in chat_bodies}, {MODEL})
        prompt = chat_bodies[0]["messages"][0]["content"]
        self.assertIn(CANARY_OBJECTIVE, prompt)
        self.assertNotIn(KEY, prompt)

    def _conn(self):
        return db.open_conn(self.db_path)


class GoalWorkerLifecycleTests(GoalsHarness):
    """start_worker=True: the poller thread drives goals to completion,
    and shutdown stops it cleanly."""

    start_polling_worker = True

    def test_poller_completes_goal_and_stop_joins(self):
        status, body = self.post(
            "/api/goals", {"objective": CANARY_OBJECTIVE})
        self.assertEqual(status, 202)
        goal_id = body["id"]
        worker = self._httpd.goal_worker
        thread = worker._thread
        self.assertIsNotNone(thread)
        self.assertTrue(thread.is_alive())
        deadline = time.monotonic() + 30
        final = None
        while time.monotonic() < deadline:
            _, body = self.get(f"/api/goals/{goal_id}")
            if body["status"] in ("succeeded", "failed", "blocked",
                                  "cancelled"):
                final = body["status"]
                break
            time.sleep(0.2)
        self.assertEqual(final, "succeeded")
        self._stop_server()  # server_close() → worker.stop() + join
        self.assertFalse(thread.is_alive())

    def test_recover_ignores_missing_db_and_does_not_create_it(self):
        missing = Path(self._tmp.name) / "nope.sqlite"
        worker = server.goal_worker.GoalWorker(
            db_path=missing, cfg=self.cfg,
            registry=tools.default_registry(self.workspace))
        worker.recover()
        self.assertFalse(missing.exists())
        worker.scan()  # same honesty: scan only reads
        self.assertFalse(missing.exists())

    def test_worker_survives_db_bursts_is_recorded(self):
        # a worker exception during scan is logged as a safe event,
        # then scanning continues on the next tick
        captured = io.StringIO()
        goal_id = self.create_goal()
        with contextlib.redirect_stderr(captured):
            broken = self._httpd.goal_worker
            original_registry = broken.registry
            broken.registry = None  # scan raises → safe log event
            broken.scan()
            broken.registry = original_registry
            broken.scan()
        self.assertIn("goal_worker_error", captured.getvalue())
        final = self.drive_to_terminal(goal_id)
        self.assertEqual(final["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
