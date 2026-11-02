"""Phase 12D tests: verified goal completion for dynamic mode.

Covers the persisted success contract (propose → deterministic
validation → independent review where required → evidence-gated
success), the durable invalid-decision budget (survives event churn,
restart, and retry), and the mode selection API contract. All
deterministic: fake provider, temp DBs, no live inference.
"""

import json
import unittest
from unittest.mock import patch

import db
import planner
import provider
import worker as worker_mod

from test_dynamic_loop import (DynamicHarness, LazyReviewer, decision,
                               echo_tool)


def contract(summary="evidence the objective", criteria=None, evidence=None):
    return json.dumps({
        "summary": summary,
        "criteria": criteria if criteria is not None else [],
        "evidence": evidence if evidence is not None else [],
    })


def list_contract(objective_token, result_tokens, tool="workspace.list_files"):
    """A contract whose criterion shares tokens with the objective
    (validity) and with a list_files result (evidence match)."""
    return contract(
        summary="list and report",
        criteria=[f"{objective_token}: {result_tokens} are listed"],
        evidence=[{"criterion": 1, "tools": [tool]}])


class ContractValidationTests(unittest.TestCase):
    """Deterministic rejection of empty, trivial, malformed, or
    goal-unrelated success contracts."""

    def setUp(self):
        self.registry = None  # set in tests via harness registries
        import tools
        self.registry = tools.ToolRegistry([echo_tool()])

    def validate(self, payload, objective="the echo tool runs"):
        return planner.validate_contract(
            payload if isinstance(payload, str) else json.dumps(payload),
            self.registry, objective)

    def test_valid_contract_normalizes(self):
        got, err, _ = self.validate({
            "summary": "prove the echo ran",
            "criteria": ["the echo tool output contains done"],
            "evidence": [{"criterion": 1, "tools": ["test.echo"]}]})
        self.assertIsNone(err)
        self.assertEqual(got["evidence"][0]["tools"], ["test.echo"])

    def test_not_json(self):
        got, err, _ = planner.validate_contract(
            "I promise the work will be done.", self.registry,
            "the echo tool runs")
        self.assertIsNone(got)
        self.assertEqual(err, "not_json")

    def test_unknown_fields(self):
        got, err, _ = self.validate({
            "summary": "s",
            "criteria": ["the echo tool output has done"],
            "evidence": [{"criterion": 1, "tools": ["test.echo"]}],
            "complete": True})
        self.assertIsNone(got)
        self.assertEqual(err, "invalid_contract")

    def test_empty_and_malformed_criteria(self):
        bad = [
            {"summary": "s", "criteria": [], "evidence": []},
            {"summary": "s", "criteria": ["", "x"], "evidence": []},
            {"summary": "s",
             "criteria": ["the echo tool output has done"] * 11,
             "evidence": []},
            {"summary": "", "criteria": ["the echo tool output has"],
             "evidence": []},
            {"summary": "s", "criteria": "the echo tool output",
             "evidence": []},
        ]
        for payload in bad:
            got, err, _ = self.validate(payload)
            self.assertIsNone(got, payload)
            self.assertEqual(err, "invalid_contract", payload)

    def test_trivial_generic_criterion_rejected(self):
        got, err, _ = self.validate({
            "summary": "s",
            "criteria": ["the task is complete and successful"],
            "evidence": [{"criterion": 1, "tools": ["test.echo"]}]})
        self.assertIsNone(got)
        self.assertEqual(err, "invalid_contract")

    def test_unrelated_criterion_rejected(self):
        got, err, _ = self.validate({
            "summary": "s",
            "criteria": ["the quarterly revenue report was filed"],
            "evidence": [{"criterion": 1, "tools": ["test.echo"]}]},
            objective="the echo tool runs")
        self.assertIsNone(got)
        self.assertEqual(err, "invalid_contract")

    def test_duplicate_criteria_rejected(self):
        crit = "the echo tool output has done"
        got, err, _ = self.validate({
            "summary": "s", "criteria": [crit, crit],
            "evidence": [{"criterion": 1, "tools": ["test.echo"]},
                         {"criterion": 2, "tools": ["test.echo"]}]})
        self.assertIsNone(got)
        self.assertEqual(err, "invalid_contract")

    def test_evidence_binding_rejections(self):
        crit = "the echo tool output has done"
        bad = [
            # evidence count must match criteria count
            {"summary": "s", "criteria": [crit], "evidence": []},
            # reference out of range
            {"summary": "s", "criteria": [crit],
             "evidence": [{"criterion": 2, "tools": ["test.echo"]}]},
            # duplicate reference
            {"summary": "s", "criteria": [crit, crit + " now"],
             "evidence": [{"criterion": 1, "tools": ["test.echo"]},
                          {"criterion": 1, "tools": ["test.echo"]}]},
            # unregistered evidence tool
            {"summary": "s", "criteria": [crit],
             "evidence": [{"criterion": 1, "tools": ["ghost.tool"]}]},
            # too many tools
            {"summary": "s", "criteria": [crit],
             "evidence": [{"criterion": 1,
                           "tools": ["test.echo"] * 4}]},
            # duplicated tools
            {"summary": "s", "criteria": [crit],
             "evidence": [{"criterion": 1,
                           "tools": ["test.echo", "test.echo"]}]},
            # bad shape
            {"summary": "s", "criteria": [crit],
             "evidence": [{"ref": 1, "tools": ["test.echo"]}]},
        ]
        for payload in bad:
            got, err, _ = self.validate(payload)
            self.assertIsNone(got, payload)
            self.assertIn(err, ("invalid_contract",
                                "unsupported_capability"), payload)


class ContractFlowTests(DynamicHarness):
    def test_success_requires_contract_evidence(self):
        """Req 1: nontrivial criteria + actual supporting evidence."""
        def script(messages):
            if not self.steps(self.gid):
                return decision("workspace.list_files",
                                {"path": "docs"}, "look")
            return decision("done", reason="evidence collected",
                            summary="the docs listing demonstrates it")

        self.scripted(script, contract=list_contract(
            "docs", "note txt"))
        self.gid = self.new_goal(objective="find the docs note txt")
        self.worker.process_once(self.gid)
        goal = self.goal(self.gid)
        self.assertEqual(goal["status"], "succeeded", goal)
        report = json.loads(goal["report_json"])
        self.assertEqual(report["criteria"][0]["passed"], True)
        self.assertEqual(report["criteria"][0]["evidence_seq"], 1)
        self.assertEqual(report["outcome"],
                         "the docs listing demonstrates it")

    def test_tool_success_alone_does_not_achieve_outcome(self):
        """Req 2: the tool works, but the contract-declared evidence
        (read_file content) was never produced — no success."""
        def script(messages):
            if not self.steps(self.gid):
                return decision("workspace.list_files",
                                {"path": "docs"}, "just look")
            return decision("done", reason="claiming done",
                            summary="claim it all worked")

        self.scripted(script, contract=contract(
            summary="prove the content",
            criteria=["hello is contained in the note txt content"],
            evidence=[{"criterion": 1,
                       "tools": ["workspace.read_file"]}]))
        self.gid = self.new_goal(objective="read the docs note txt")
        self.worker.process_once(self.gid)
        goal = self.goal(self.gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "criteria_unverified")
        self.assertIn("lack supporting evidence",
                      json.loads(goal["report_json"])["detail"])

    def test_irrelevant_evidence_prevents_success(self):
        """Req 3: evidence tool ran, but its result does not match the
        criterion — stale or irrelevant results are refused."""
        def script(messages):
            if not self.steps(self.gid):
                return decision("workspace.list_files",
                                {"path": "docs"}, "look around")
            return decision("done", reason="claiming",
                            summary="claim finished")

        self.scripted(script, contract=contract(
            summary="prove the summary report",
            criteria=["the docs folder shows a summary report"],
            evidence=[{"criterion": 1,
                       "tools": ["workspace.list_files"]}]))
        self.gid = self.new_goal(objective="find the docs summary report")
        self.worker.process_once(self.gid)
        goal = self.goal(self.gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "criteria_unverified")

    def test_malformed_contract_blocks_honestly(self):
        """Req 4: malformed/trivial contracts are refused twice, then an
        honest block — never silently weakened."""
        replies = ["not json at all", json.dumps({"complete": True})]
        self.scripted(lambda messages: replies.pop(0),
                      contract="scripted")
        gid = self.new_goal()
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "contract_unverifiable")

    def test_objective_unverifiable_with_available_tools(self):
        """Req 5: an objective whose proposed evidence tools are not
        registered must NOT proceed with weakened criteria."""
        replies = [contract(
            summary="external work",
            criteria=["the vendored contract terms are drafted"],
            evidence=[{"criterion": 1, "tools": ["email.send"]}]),
            contract(
            summary="external work",
            criteria=["the vendored contract terms are drafted"],
            evidence=[{"criterion": 1, "tools": ["phone.call"]}])]
        self.scripted(lambda messages: replies.pop(0))
        gid = self.new_goal(
            objective="draft the vendored contract terms")
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "contract_unverifiable")

    def test_contract_review_required_and_persisted(self):
        """Req 6: a contract executing approval-class evidence tools
        needs independent review; none available → honest block."""
        self.scripted(lambda messages: contract(
            summary="prove the note",
            criteria=["note txt is created in the docs folder"],
            evidence=[{"criterion": 1,
                       "tools": ["workspace.create_file"]}]),
            contract="scripted")
        gid = self.new_goal(objective="create the docs note txt")
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "reviewer_unavailable")
        # No approval was fabricated; nothing executed.
        self.assertEqual(self.steps(gid), [])

    def test_approved_contract_is_reviewed_and_persisted(self):
        """Req 6: with an independent reviewer, the contract is
        approved, persisted with provenance, and the loop proceeds."""
        self.worker.reviewer = LazyReviewer()
        replies = [
            contract(
                summary="prove the note",
                criteria=["note txt is created in the docs folder"],
                evidence=[{"criterion": 1,
                           "tools": ["workspace.create_file"]},
                          ]),
            decision("workspace.create_file",
                     {"path": "docs/note.txt", "content": "hi"},
                     "create the requested note"),
        ]
        self.scripted(lambda messages: replies.pop(0),
                      contract="scripted")
        gid = self.new_goal(objective="create the docs note txt")
        self.worker.process_once(gid)
        goal = self.goal(gid)
        # The contract itself passed review; the loop now waits for the
        # human digest approval of the actual write decision.
        self.assertEqual(goal["status"], "awaiting_approval", goal)
        stored = json.loads(goal["plan_json"])
        self.assertEqual(stored["review_source"], "external_review")
        self.assertEqual(stored["review"]["identity"], "fake-approver")

    def test_contract_survives_restart_and_stays_bound(self):
        """Req 7: the contract and collected evidence survive a worker
        restart; steps are not re-run; success uses persisted evidence."""
        def script(messages):
            if not self.steps(self.gid):
                return decision("workspace.list_files",
                                {"path": "docs"}, "look")
            return decision("done", reason="evidence collected",
                            summary="listed")

        self.scripted(script, contract=list_contract("docs", "note txt"))
        self.gid = self.new_goal(objective="find the docs note txt")
        self.worker.process_once(self.gid)
        self.assertEqual(len(self.steps(self.gid)), 1)
        contract_before = self.goal(self.gid)["plan_json"]
        # Restart: a fresh GoalWorker instance continues the same goal.
        self.worker = worker_mod.GoalWorker(
            self.db_path, self.worker.cfg, self.registry)
        self.worker.process_once(self.gid)
        goal = self.goal(self.gid)
        self.assertEqual(goal["status"], "succeeded", goal)
        self.assertEqual(goal["plan_json"], contract_before)
        self.assertEqual(len(self.steps(self.gid)), 1)
        self.assertEqual(self.steps(self.gid)[0]["attempts"], 1)


class DurableBudgetTests(DynamicHarness):
    CHURN = 150  # > MAX_GOAL_EVENTS (100): forces event retention churn

    def churn_events(self, gid):
        conn = db.open_conn(self.db_path)
        try:
            for i in range(self.CHURN):
                db.insert_goal_event(conn, gid, "churn", f"noise {i}")
        finally:
            conn.close()

    def test_budget_survives_event_retention_churn(self):
        """Req 8: invalid-decision enforcement no longer reads the
        retention-bounded event history."""
        replies = ["garbage one", "garbage two"]
        self.scripted(lambda messages: replies.pop(0))
        gid = self.new_goal()
        self.churn_events(gid)
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "decision_invalid")
        # The durable counter is on the goal row, past event churn.
        self.assertEqual(goal["dynamic_invalid_count"], 2)

    def test_budget_survives_restart_and_retry(self):
        """Req 9: the counter persists across restart and /retry — old
        invalid decisions can never be laundered away."""
        replies = ["garbage one", "garbage two", "garbage three"]
        self.scripted(lambda messages: replies.pop(0))
        gid = self.new_goal()
        self.worker.process_once(gid)
        self.assertEqual(self.goal(gid)["error_code"], "decision_invalid")
        # /retry semantics: blocked -> queued; simulate a restart too.
        conn = db.open_conn(self.db_path)
        try:
            self.assertTrue(db.update_goal_fields(
                conn, gid, "blocked", {"status": "queued"}))
        finally:
            conn.close()
        self.worker = worker_mod.GoalWorker(
            self.db_path, self.worker.cfg, self.registry)
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "blocked", goal)
        self.assertEqual(goal["error_code"], "decision_invalid")
        self.assertEqual(goal["dynamic_invalid_count"], 3)

    def test_first_invalid_after_churn_does_not_block_early(self):
        """The counter (not churned event rows) drives the threshold."""
        replies = ["garbage one",
                   decision("workspace.list_files", {"path": "docs"},
                            "look"),
                   decision("done", reason="evidence collected",
                            summary="listed")]
        self.scripted(lambda messages: replies.pop(0))
        gid = self.new_goal()
        self.churn_events(gid)
        self.worker.process_once(gid)
        goal = self.goal(gid)
        self.assertEqual(goal["status"], "succeeded", goal)
        self.assertEqual(goal["dynamic_invalid_count"], 1)
        self.assertIsNone(goal["error_code"])


class ModeSelectionTests(DynamicHarness):
    """Req 10/11: mode is chosen explicitly; sequential stays default;
    mode is immutable after creation."""

    def test_mode_cannot_be_changed_after_creation(self):
        gid = self.new_goal(mode="sequential")
        conn = db.open_conn(self.db_path)
        try:
            with self.assertRaises(KeyError):
                db.update_goal_fields(conn, gid, "queued",
                                      {"mode": "dynamic"})
            self.assertEqual(self.goal(gid)["mode"], "sequential")
        finally:
            conn.close()


class DynamicGoalApiTests(DynamicHarness):
    """HTTP contract for the mode field (fake upstream, no live calls)."""

    def setUp(self):
        super().setUp()
        import tempfile
        import threading
        from pathlib import Path as _Path
        import config
        import server
        from fake_upstream import FakeUpstream
        self._api_tmp = tempfile.TemporaryDirectory(prefix="jarvis-api-")
        self.addCleanup(self._api_tmp.cleanup)
        workspace = _Path(self._api_tmp.name) / "workspace"
        workspace.mkdir()
        self.api_db = _Path(self._api_tmp.name) / "api.db"
        self.upstream = FakeUpstream(
            response=(200, {"choices": [{"message": {"role": "assistant",
                                                    "content": "{}"}}]},
                       "application/json"))
        self.upstream.start(self)
        env = {
            "JARVIS_DB_PATH": str(self.api_db),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": "test-key-123",
            "JARVIS_WORKSPACE_ROOT": str(workspace),
        }
        cfg = config.load(environ=env)
        import http.server  # noqa: F401
        httpd = server.create_server(
            "127.0.0.1", 0, self.api_db, init_db=True, cfg=cfg,
            start_worker=False, settings_dir=str(self._api_tmp.name))
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, 5)

    def _post(self, payload):
        import urllib.request
        req = urllib.request.Request(
            self.base + "/api/goals",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _get(self, path):
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return json.loads(resp.read())

    def test_dynamic_mode_selected_through_api(self):
        status, body = self._post(
            {"objective": "inspect the docs", "mode": "dynamic"})
        self.assertEqual(status, 202, body)
        detail = self._get(f"/api/goals/{body['id']}")
        self.assertEqual(detail["mode"], "dynamic")

    def test_invalid_mode_rejected(self):
        status, body = self._post(
            {"objective": "inspect the docs", "mode": "chaotic"})
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_default_mode_is_sequential_through_api(self):
        status, body = self._post({"objective": "inspect the docs"})
        self.assertEqual(status, 202, body)
        listing = self._get("/api/goals?limit=10")
        self.assertEqual(listing["goals"][0]["mode"], "sequential")


if __name__ == "__main__":
    unittest.main()
