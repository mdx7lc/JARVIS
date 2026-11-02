"""Phase 12B tests: the bounded dynamic goal loop.

The decision loop must make decisions from REAL tool observations,
refuse invalid calls, enforce hard persisted budgets, and never bypass
the digest-bound human approval flow. No live inference: the provider
call is patched with deterministic scripted decisions.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db
import planner
import provider
import tools
import worker as worker_mod


def decision(action, args=None, reason="because", summary=None):
    payload = {"action": action, "reason": reason}
    if action != "done":
        payload["args"] = args or {}
    if summary is not None:
        payload["summary"] = summary
    return json.dumps(payload)


def echo_tool(verify_passes=True):
    return tools.Tool(
        name="test.echo",
        description="Echo probe (test fixture).",
        schema={"tag": {"type": "string", "required": False,
                        "default": "", "maxlen": 100}},
        permission="autonomous",
        idempotent=True,
        run=lambda args: {"echo": str(args.get("tag", "")), "ok": True},
        verifier=lambda args, result: (
            {"passed": True, "detail": "echo ok"} if verify_passes
            else {"passed": False, "detail": "forced verification"
                  " failure"}),
    )


class LazyReviewer:
    def review(self, plan, registry, objective="", local_check=None):
        return {"status": "approved", "source": "external_review",
                "identity": "fake-approver", "verdict": "approved",
                "risk_level": "low", "findings": [], "missing_criteria": [],
                "recommended_changes": [], "reviewed_at": "now"}


class DynamicHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-dyn-")
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name) / "workspace"
        (self.workspace / "docs").mkdir(parents=True)
        (self.workspace / "docs" / "note.txt").write_text("hello\n")
        self.db_path = Path(self._tmp.name) / "jarvis.db"
        db.init_db(self.db_path)
        self.registry = tools.default_registry(self.workspace)
        cfg = type("Cfg", (), {
            "model": "fake-model", "api_base": "http://x",
            "api_key": "k", "fallback_model": None,
            "timeout_seconds": 1, "max_tokens": 100,
        })()
        self.worker = worker_mod.GoalWorker(self.db_path, cfg,
                                            self.registry)

    def new_goal(self, mode="dynamic", objective="inspect the docs"):
        conn = db.open_conn(self.db_path)
        try:
            return db.create_goal(conn, objective, mode=mode)["id"]
        finally:
            conn.close()

    def goal(self, gid):
        conn = db.open_conn(self.db_path)
        try:
            return db.get_goal(conn, gid)
        finally:
            conn.close()

    def steps(self, gid):
        conn = db.open_conn(self.db_path)
        try:
            return db.get_goal_steps(conn, gid)
        finally:
            conn.close()

    def events(self, gid, event):
        conn = db.open_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT detail FROM goal_events WHERE goal_id=? AND"
                " event=? ORDER BY id", (gid, event)).fetchall()
            return [r["detail"] for r in rows]
        finally:
            conn.close()

    def scripted(self, fn, contract=None):
        """Patch the provider with a deterministic script. Contract
        prompts (Phase 12D) are answered automatically with a valid
        default contract unless the test supplies its own."""
        default = json.dumps({
            "summary": "list the docs folder",
            "criteria": ["note.txt is listed in the docs folder"],
            "evidence": [{"criterion": 1,
                          "tools": ["workspace.list_files"]}],
        })
        reply = contract or default

        def handler(messages, **kw):
            content = messages[0]["content"]
            if contract != "scripted" \
                    and "contract planner for a JARVIS" in content:
                return {"reply": reply}
            return {"reply": fn(messages)}

        patcher = patch.object(
            worker_mod.provider, "complete_with_fallback",
            side_effect=handler)
        patcher.start()
        self.addCleanup(patcher.stop)

    def approvals(self, gid):
        conn = db.open_conn(self.db_path)
        try:
            return db.list_goal_approvals(conn, gid)
        finally:
            conn.close()

    def set_approval_state(self, gid, seq, expected, new, fields=None):
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.update_goal_approval_state(
                conn, gid, seq, expected, new, fields))
        finally:
            conn.close()


class DecisionValidationTests(unittest.TestCase):
    def setUp(self):
        self.registry = tools.default_registry(Path("/tmp"))

    def validate(self, payload):
        return planner.validate_decision(json.dumps(payload),
                                         self.registry)

    def test_valid_tool_action(self):
        dec, err, _ = self.validate(
            {"action": "workspace.list_files", "args": {"path": "docs"},
             "reason": "list the docs"})
        self.assertIsNone(err)
        self.assertEqual(dec["args"], {"path": "docs"})

    def test_valid_done(self):
        dec, err, _ = self.validate(
            {"action": "done", "reason": "finished",
             "summary": "the docs were inspected"})
        self.assertIsNone(err)
        self.assertEqual(dec["summary"], "the docs were inspected")

    def test_not_json(self):
        dec, err, _ = planner.validate_decision(
            "I will just list the files, trust me.", self.registry)
        self.assertIsNone(dec)
        self.assertEqual(err, "not_json")

    def test_rejects(self):
        bad = [
            ({"args": {}, "reason": "r"}, "invalid_decision"),
            ({"action": 5, "reason": "r"}, "invalid_decision"),
            ({"action": "done", "reason": ""}, "invalid_decision"),
            ({"action": "done", "reason": "x" * 201},
             "invalid_decision"),
            ({"action": "done", "args": {"x": 1}, "reason": "r",
              "summary": "s"}, "invalid_decision"),
            ({"action": "done", "reason": "r"}, "invalid_decision"),
            ({"action": "workspace.list_files", "reason": "r",
              "summary": "s"}, "invalid_decision"),
            ({"action": "not.a.tool", "args": {}, "reason": "r"},
             "unsupported_capability"),
            ({"action": "workspace.read_file", "args": {},
              "reason": "r"}, "invalid_tool_args"),
            ({"action": "workspace.list_files", "args": {"bogus": 1},
              "reason": "r"}, "invalid_tool_args"),
            ({"action": "done", "reason": "r", "summary": "",
              "extra": 1}, "invalid_decision"),
        ]
        for payload, expected in bad:
            dec, err, _ = self.validate(payload)
            self.assertIsNone(dec, payload)
            self.assertEqual(err, expected, payload)


class DecisionLoopTests(DynamicHarness):
    def test_observation_driven_decisions_end_to_end(self):
        calls = []

        def script(messages):
            calls.append(messages[0]["content"])
            if len(calls) == 1:
                return decision("workspace.list_files",
                                {"path": "docs"}, "see what is there")
            # The second decision MUST see the real observation.
            self.assertIn("[1] workspace.list_files", calls[-1])
            self.assertIn("observation:", calls[-1])
            self.assertIn("note.txt", calls[-1])
            return decision("done", reason="objective met",
                            summary="the docs folder was inspected")

        self.scripted(script)
        gid = self.new_goal()
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "succeeded", goal)
        self.assertEqual(goal["mode"], "dynamic")
        steps = self.steps(gid)
        self.assertEqual([s["status"] for s in steps], ["done"])
        self.assertEqual(steps[0]["tool_name"], "workspace.list_files")
        self.assertEqual(
            json.loads(goal["report_json"])["outcome"],
            "the docs folder was inspected")
        # The persisted success contract is bound to the goal (Phase 12D).
        self.assertEqual(json.loads(goal["plan_json"])["mode"], "dynamic")
        self.assertIn("note.txt", goal["plan_json"])
        self.assertEqual(goal["repair_count"], 0)

    def test_invalid_decisions_block_honestly(self):
        replies = ["no json at all",
                   decision("ghost.tool", {}, "not registered"),
                   decision("done", reason="ok", summary="done")]
        self.scripted(lambda messages: replies.pop(0))
        gid = self.new_goal()
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "decision_invalid")
        self.assertEqual(len(self.events(gid, "dynamic_decision_invalid")),
                         2)

    def test_invalid_tool_args_count_as_invalid(self):
        replies = [decision("workspace.read_file", {}, "missing path"),
                   decision("workspace.read_file", {}, "still missing")]
        self.scripted(lambda messages: replies.pop(0))
        gid = self.new_goal()
        self.worker.process_once(gid)
        self.assertEqual(self.goal(gid)["error_code"], "decision_invalid")

    def test_exact_repeat_is_refused(self):
        call = {"n": 0}

        def script(messages):
            call["n"] += 1
            return decision("workspace.list_files", {"path": "docs"},
                            f"look again {call['n']}")

        self.scripted(script)
        gid = self.new_goal()
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "loop_budget_exhausted")
        self.assertEqual(
            len(self.events(gid, "dynamic_repeat_refused")), 1)

    def test_turn_budget_is_enforced(self):
        registry = tools.ToolRegistry([echo_tool()])
        self.worker.registry = registry

        def script(messages):
            n = len(self.steps(self.gid))
            return decision("test.echo", {"tag": f"t{n}"}, f"turn {n}")

        self.scripted(script, contract=json.dumps({
            "summary": "exercise the echo tool within budget",
            "criteria": ["the docs listing steps run via echo"],
            "evidence": [{"criterion": 1, "tools": ["test.echo"]}],
        }))
        self.gid = self.new_goal()
        self.worker.process_once(self.gid)
        goal = self.goal(self.gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "loop_budget_exhausted")
        self.assertEqual(len(self.steps(self.gid)),
                         worker_mod.MAX_DYNAMIC_TURNS)

    def test_done_with_no_verified_steps_is_refused(self):
        self.scripted(lambda messages: decision(
            "done", reason="nothing to do", summary="all done"))
        gid = self.new_goal()
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "criteria_unverified")
        self.assertTrue(self.events(gid, "dynamic_done_refused"))

    def test_done_claim_cannot_bypass_tool_verification(self):
        registry = tools.ToolRegistry([echo_tool(verify_passes=False)])
        self.worker.registry = registry

        def script(messages):
            if not self.steps(self.gid):
                return decision("test.echo", {"tag": "x"}, "try it")
            return decision("done", reason="claim success",
                            summary="claim everything is fine")

        self.scripted(script, contract=json.dumps({
            "summary": "exercise the echo tool within budget",
            "criteria": ["the docs listing steps run via echo"],
            "evidence": [{"criterion": 1, "tools": ["test.echo"]}],
        }))
        self.gid = self.new_goal()
        self.worker.process_once(self.gid)
        goal = self.goal(self.gid)
        # The failing postcondition makes the goal FAILED before the
        # model can ever claim done — a claim is not evidence.
        self.assertEqual(goal["status"], "failed", goal)
        self.assertEqual(goal["error_code"], "verification_failed")

    def test_provider_failure_fails_honestly(self):
        def boom(messages, **kw):
            raise provider.RateLimited("rate limited")

        patcher = patch.object(worker_mod.provider,
                               "complete_with_fallback",
                               side_effect=boom)
        patcher.start()
        self.addCleanup(patcher.stop)
        gid = self.new_goal()
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "failed", goal)
        self.assertEqual(goal["error_code"], "rate_limited")


class DynamicApprovalTests(DynamicHarness):
    def test_write_request_goes_through_digest_approval(self):
        self.worker.reviewer = LazyReviewer()
        call = {"n": 0}

        def script(messages):
            call["n"] += 1
            if call["n"] == 1:
                return decision(
                    "workspace.create_file",
                    {"path": "docs/new.txt", "content": "hi"},
                    "need to create a file")
            return decision("done", reason="finished",
                            summary="the file was created")

        self.scripted(script, contract=json.dumps({
            "summary": "create the requested note file",
            "criteria": ["new.txt is created in the docs folder"],
            "evidence": [{"criterion": 1,
                          "tools": ["workspace.create_file"]}],
        }))
        gid = self.new_goal()
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "awaiting_approval", goal)
        # Nothing was written before the human approved.
        self.assertFalse((self.workspace / "docs" / "new.txt").exists())
        rows = self.approvals(gid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "proposed")
        self.assertEqual(rows[0]["tool_name"], "workspace.create_file")
        self.assertEqual(self.steps(gid)[0]["status"], "pending")
        # The model cannot approve its own request.
        self.worker.process_once(gid)
        self.assertEqual(self.goal(gid)["status"], "awaiting_approval")
        self.assertFalse((self.workspace / "docs" / "new.txt").exists())
        # The human approves the EXACT digest-bound proposal.
        self.set_approval_state(gid, 1, "proposed", "approved",
                                {"approved_at": "now"})
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "succeeded", goal)
        self.assertEqual(
            (self.workspace / "docs" / "new.txt").read_text(), "hi")
        rows = self.approvals(gid)
        self.assertEqual(rows[0]["state"], "executed")
        self.assertEqual(self.steps(gid)[0]["status"], "done")

    def test_rejected_write_request_blocks_without_executing(self):
        self.worker.reviewer = LazyReviewer()
        self.scripted(lambda messages: decision(
            "workspace.create_file",
            {"path": "docs/no.txt", "content": "no"},
            "model wants a write"),
            contract=json.dumps({
                "summary": "create the requested note file",
                "criteria": ["no.txt is created in the docs folder"],
                "evidence": [{"criterion": 1,
                              "tools": ["workspace.create_file"]}],
            }))
        gid = self.new_goal()
        self.worker.process_once(gid)
        self.assertEqual(self.goal(gid)["status"], "awaiting_approval")
        self.set_approval_state(gid, 1, "proposed", "rejected")
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "approval_withdrawn")
        self.assertFalse((self.workspace / "docs" / "no.txt").exists())
        self.assertEqual(self.steps(gid)[0]["status"], "pending")


class DynamicRecoveryTests(DynamicHarness):
    def test_restart_mid_loop_resumes_without_rerunning_steps(self):
        def script(messages):
            if len(self.steps(self.gid)) == 0:
                return decision("workspace.list_files", {"path": "docs"},
                                "first look")
            return decision("done", reason="finished",
                            summary="inspected the docs")

        self.scripted(script)
        self.gid = self.new_goal()
        self.worker.process_once(self.gid)
        self.assertEqual(len(self.steps(self.gid)), 1)
        self.assertEqual(self.steps(self.gid)[0]["attempts"], 1)
        # Simulate a worker restart: a fresh GoalWorker instance takes
        # over the same persisted goal.
        cfg = self.worker.cfg
        self.worker = worker_mod.GoalWorker(self.db_path, cfg,
                                            self.registry)
        self.worker.process_once(self.gid)
        goal = self.goal(self.gid)
        self.assertEqual(goal["status"], "succeeded", goal)
        steps = self.steps(self.gid)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["attempts"], 1)  # never re-run

    def test_running_step_recovery_resets_idempotent_step(self):
        state = {"n": 0}

        def script(messages):
            state["n"] += 1
            if state["n"] == 1:
                return decision("workspace.list_files", {"path": "docs"},
                                "look")
            return decision("done", reason="finished",
                            summary="inspected")

        self.scripted(script)
        gid = self.new_goal()
        self.worker.process_once(gid)
        # Forge an interrupted step, as a crash would leave it.
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.update_goal_step_status(
                conn, gid, 1, "done", "pending"))
            self.assertTrue(db.update_goal_fields(
                conn, gid, "succeeded", {"status": "executing"}))
        finally:
            conn.close()
        self.worker.recover()
        self.assertEqual(self.steps(gid)[0]["status"], "pending")
        self.worker.process_once(gid)
        self.assertEqual(self.goal(gid)["status"], "succeeded")

    def test_blocked_dynamic_goal_retry_resumes_loop(self):
        replies = ["garbage", "still garbage",
                   decision("workspace.list_files", {"path": "docs"},
                            "look for real"),
                   decision("done", reason="ok", summary="recovered")]
        self.scripted(lambda messages: replies.pop(0))
        gid = self.new_goal()
        self.worker.process_once(gid)
        self.assertEqual(self.goal(gid)["error_code"], "decision_invalid")
        # /retry semantics: blocked -> queued (validated by goalstate).
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.update_goal_fields(
                conn, gid, "blocked", {"status": "queued"}))
        finally:
            conn.close()
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "succeeded", goal)


class SequentialUnchangedTests(DynamicHarness):
    def test_sequential_goal_still_plan_then_execute(self):
        plan = {
            "summary": "Inventory the workspace",
            "outcome": "A workspace inventory is recorded.",
            "criteria": ["accurate inventory of the docs files"],
            "risk": "low",
            "uncertainties": [],
            "steps": [{
                "purpose": "List the docs files",
                "tool": "workspace.list_files",
                "args": {"path": "docs"},
                "depends_on": [],
                "acceptance": "accurate inventory of the docs files",
            }],
        }
        self.scripted(lambda messages: json.dumps(plan))
        gid = self.new_goal(mode="sequential")
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "succeeded", goal)
        self.assertEqual(goal["mode"], "sequential")
        self.assertIsNotNone(goal["plan_json"])

    def test_default_mode_is_sequential(self):
        conn = db.open_conn(self.db_path)
        try:
            goal = db.create_goal(conn, "plain objective")
        finally:
            conn.close()
        self.assertEqual(goal["mode"], "sequential")

    def test_unknown_mode_refused(self):
        conn = db.open_conn(self.db_path)
        try:
            with self.assertRaises(ValueError):
                db.create_goal(conn, "x", mode="chaotic")
        finally:
            conn.close()


class DynamicGoalApiTests(DynamicHarness):
    """API contract: POST /api/goals accepts the optional mode field.

    Checked through the db/server contract helpers rather than a full
    HTTP harness: the endpoint validates via the same two rules below
    (mode whitelist + db.create_goal), which the HTTP layer only
    forwards. The full HTTP path is covered by the existing
    test_goals_api suite for the unchanged default mode."""

    def test_dynamic_mode_roundtrip(self):
        gid = self.new_goal(mode="dynamic")
        self.assertEqual(self.goal(gid)["mode"], "dynamic")

    def test_worker_picks_dynamic_handlers(self):
        seen = []

        def script(messages):
            seen.append(messages[0]["content"])
            return decision("done", reason="nothing needed",
                            summary="no steps required")

        self.scripted(script)
        gid = self.new_goal(mode="dynamic")
        self.worker.process_once(gid)
        # The dynamic handlers ran: the decision prompt carries the
        # persisted contract (never the sequential plan prompt).
        self.assertIn("dynamic-mode goal", seen[0])
        self.assertIn("Success contract", seen[0])
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "blocked")
        self.assertEqual(goal["error_code"], "criteria_unverified")


if __name__ == "__main__":
    unittest.main()
