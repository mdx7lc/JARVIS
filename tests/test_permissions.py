"""Permission engine tests (4-level architecture).

Unit: level mapping, cap policy, per-tool disable, fail-closed
behavior, audit history, operator policy validation. API: /api/
permissions view, cap + policy writes, audit feed, L4 refusal.
Worker integration: a denied tool call fails the step with
permission_denied instead of running.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
import permissions as permissions_mod
import tools


def probe_tool(name="test.read", permission_class="autonomous"):
    return tools.Tool(
        name=name,
        description="Probe (test fixture).",
        schema={"tag": {"type": "string", "required": False,
                        "default": "", "maxlen": 40}},
        permission=permission_class,
        idempotent=True,
        run=lambda args: {"ok": True},
        verifier=lambda args, result: {"passed": True, "detail": "ok"},
    )


class _DbCase(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="jarvis-perm-"))
        self.addCleanup(_rmtree, d)
        self.db_path = d / "db.sqlite"
        db.init_db(self.db_path)

    def engine(self):
        return permissions_mod.PermissionEngine(self.db_path)


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


class LevelMappingTests(unittest.TestCase):
    def test_class_to_level(self):
        self.assertEqual(permissions_mod.level_for_tool(
            probe_tool(permission_class="verify_readonly")),
            permissions_mod.L1)
        self.assertEqual(permissions_mod.level_for_tool(
            probe_tool(permission_class="autonomous")),
            permissions_mod.L2)
        self.assertEqual(permissions_mod.level_for_tool(
            probe_tool(permission_class="approval_required")),
            permissions_mod.L3)

    def test_unknown_class_lands_on_l4(self):
        self.assertEqual(permissions_mod.level_for_tool(
            probe_tool(permission_class="mystery")),
            permissions_mod.L4)


class EngineDecisionTests(_DbCase):
    def test_cap_exceed_denies_l2(self):
        engine = self.engine()
        engine.set_cap(permissions_mod.L1)
        self.assertFalse(engine.authorize(
            probe_tool())["allowed"])

    def test_cap_l1_allows_l1_tool(self):
        engine = self.engine()
        engine.set_cap(permissions_mod.L1)
        decision = engine.authorize(
            probe_tool(permission_class="verify_readonly"))
        self.assertTrue(decision["allowed"])
        self.assertEqual(decision["level"], permissions_mod.L1)

    def test_disabled_tool_denied(self):
        engine = self.engine()
        engine.set_cap(permissions_mod.L3)
        engine.set_tool_state("test.read", "disabled", declared_level=2)
        decision = engine.authorize(probe_tool())
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "tool_disabled_by_operator")
        # inherit restores it
        engine.set_tool_state("test.read", "inherit")
        self.assertTrue(engine.authorize(probe_tool())["allowed"])

    def test_restricted_class_never_runs(self):
        engine = self.engine()
        decision = engine.authorize(
            probe_tool(permission_class="restricted"))
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "restricted_class")

    def test_l4_never_grantable_via_cap(self):
        engine = self.engine()
        with self.assertRaises(permissions_mod.PolicyWriteError):
            engine.set_cap(4)

    def test_default_cap_unchanged_policy(self):
        engine = self.engine()
        self.assertEqual(engine.cap(), permissions_mod.L3)
        self.assertTrue(engine.authorize(probe_tool())["allowed"])

    def test_fail_closed_on_corrupt_db(self):
        # A DB path that exists but is garbage must deny, never allow.
        corrupt = self.db_path.parent / "corrupt.sqlite"
        corrupt.write_bytes(b"not a database at all")
        engine = permissions_mod.PermissionEngine(corrupt)
        self.assertFalse(engine.authorize(probe_tool())["allowed"])

    def test_audit_records_allow_and_deny(self):
        engine = self.engine()
        engine.authorize(probe_tool())  # allowed
        engine.set_cap(permissions_mod.L1)
        engine.authorize(probe_tool())  # denied_by_cap
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT tool_name, decision, reason"
                " FROM permission_audit ORDER BY id").fetchall()
        finally:
            conn.close()
        decisions = [tuple(r) for r in rows if r[0] == "test.read"]
        self.assertEqual(decisions[0][1], "allowed")
        self.assertEqual(decisions[-1][1], "denied")
        self.assertEqual(decisions[-1][2], "capability_cap_exceeded")

    def test_audit_history_bounded(self):
        engine = self.engine()
        for _ in range(permissions_mod.AUDIT_KEEP + 25):
            engine.authorize(probe_tool())
        conn = sqlite3.connect(self.db_path)
        try:
            count = conn.execute(
                "SELECT count(*) FROM permission_audit").fetchone()[0]
        finally:
            conn.close()
        self.assertLessEqual(count, permissions_mod.AUDIT_KEEP)

    def test_api_failure_deny_does_not_crash(self):
        # Audit insert failure must not fail the decision itself.
        engine = self.engine()

        def boom(*_a, **_k):
            raise sqlite3.OperationalError("disk error")

        engine._conn = boom
        decision = engine.authorize(probe_tool())
        self.assertEqual(decision["reason"], "engine_unavailable")


# The cap disable path exercises set_tool_state validation only.
class PolicyWriteValidationTests(_DbCase):
    def test_invalid_state_refused(self):
        engine = self.engine()
        with self.assertRaises(permissions_mod.PolicyWriteError):
            engine.set_tool_state("test.read", "allowed_for_friday")

    def test_invalid_tool_name_refused(self):
        engine = self.engine()
        with self.assertRaises(permissions_mod.PolicyWriteError):
            engine.set_tool_state("", "disabled")
        with self.assertRaises(permissions_mod.PolicyWriteError):
            engine.set_tool_state("x" * 81, "disabled")

    def test_invalid_cap_refused(self):
        engine = self.engine()
        for bad in (0, 4, -1, "2", 1.5, True):
            with self.assertRaises(permissions_mod.PolicyWriteError):
                engine.set_cap(bad)


try:
    from test_chat_api import ChatAPITestCase

    class PermissionsAPITests(ChatAPITestCase):
        """HTTP surface: view, cap, policy, audit."""

        def _engine(self):
            return permissions_mod.PermissionEngine(self.db_path)

        def test_view_lists_registered_tools_with_levels(self):
            status, payload = self._get_json("/api/permissions")
            self.assertEqual(status, 200)
            self.assertEqual(payload["cap"], 3)
            levels = {entry["level"]: entry["name"]
                      for entry in payload["levels"]}
            self.assertEqual(
                levels,
                {1: "observer", 2: "operator",
                 3: "authorized_executor", 4: "restricted"})
            tool_names = {t["name"] for t in payload["tools"]}
            self.assertIn("workspace.list_files", tool_names)
            writes = [t for t in payload["tools"]
                      if t["class"] == "approval_required"]
            self.assertTrue(writes)
            for tool in writes:
                self.assertEqual(tool["level"], 3)
                self.assertTrue(tool["approval_digest_gate"])
            self.assertFalse(payload["restricted"]["grantable"])

        def test_cap_write_gates_l2_tools(self):
            status, payload = self._post_json(
                "/api/permissions/cap", {"level": 1})
            self.assertEqual(status, 200)
            self.assertEqual(payload["state"]["cap"], 1)
            view = self._engine().authorize(tools.Tool(
                name="workspace.list_files",
                description="s", schema={}, permission="autonomous",
                idempotent=True, run=lambda a: {}, verifier=lambda a, r:
                {"passed": True, "detail": ""}))
            # L2 under a cap of 1: denied with the cap reason.
            self.assertFalse(view["allowed"])

        def test_cap_l4_refused(self):
            status, payload = self._post_json(
                "/api/permissions/cap", {"level": 4})
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "invalid_cap")

        def test_policy_disable_and_audit_feed(self):
            status, payload = self._post_json(
                "/api/permissions/policy",
                {"tool": "workspace.list_files", "state": "disabled"})
            self.assertEqual(status, 200)
            tool_row = next(t for t in payload["state"]["tools"]
                            if t["name"] == "workspace.list_files")
            self.assertEqual(tool_row["effective"], "disabled")
            self.assertEqual(tool_row["policy"], "disabled")
            status, audit = self._get_json("/api/permissions/audit")
            self.assertEqual(status, 200)
            sources = {e["source"] for e in audit["entries"]}
            self.assertIn("operator_policy", sources)
            decisions = {e["decision"] for e in audit["entries"]}
            self.assertIn("policy_changed", decisions)

        def test_policy_unknown_tool_404(self):
            status, payload = self._post_json(
                "/api/permissions/policy",
                {"tool": "no.such.tool", "state": "disabled"})
            self.assertEqual(status, 404)
            self.assertEqual(payload["error"]["code"], "unknown_tool")

        def test_policy_invalid_payload_400(self):
            status, _p = self._post_json(
                "/api/permissions/policy", {"tool": "workspace.list_files",
                                            "state": "grant_everything"})
            self.assertEqual(status, 400)

        def test_rehydrated_cap_persists(self):
            # the cap row lives in the DB, so a fresh engine sees it —
            # that is the persistence-across-restart contract.
            status, _p = self._post_json("/api/permissions/cap",
                                         {"level": 2})
            self.assertEqual(status, 200)
            self.assertEqual(self._engine().cap(), 2)

        def _get_json(self, path):
            from test_models_api import request, as_json
            status, _h, body = request(self.base + path)
            return status, as_json(body)

        def _post_json(self, path, payload):
            from test_models_api import request, as_json
            status, _h, body = request(self.base + path,
                                       payload=payload, method="POST")
            return status, as_json(body)
except ImportError:  # pragma: no cover - chat harness unavailable
    PermissionsAPITests = None


class WorkerIntegrationTests(_DbCase):
    """A tool the operator revoked must fail the invocation cleanly —
    the worker denies through the engine, never executes."""

    def _make_worker_and_goal(self):
        import worker as worker_mod
        reg = tools.ToolRegistry([
            probe_tool("test.echo"),
        ])
        cfg = type("Cfg", (), {"model": "m", "fallback_model": None,
                               "max_tokens": 32,
                               "history_messages": 4,
                               "timeout_seconds": 5})()
        w = worker_mod.GoalWorker(self.db_path, cfg, reg)
        conn = db.open_conn(self.db_path)
        try:
            with conn:
                goal = db.create_goal(conn, "probe objective",
                                      status="executing")
                goal_id = goal["id"]
                db.append_goal_step(conn, goal_id, purpose="evidence",
                                    tool_name="test.echo", args={"tag": "x"},
                                    acceptance="echoed")
        finally:
            conn.close()
        return w, goal_id

    def _step(self, goal_id, seq=1):
        conn = db.open_conn(self.db_path)
        try:
            steps = db.get_goal_steps(conn, goal_id)
        finally:
            conn.close()
        return next(s for s in steps if s["seq"] == seq)

    def _goal(self, goal_id):
        conn = db.open_conn(self.db_path)
        try:
            g = db.get_goal(conn, goal_id)
        finally:
            conn.close()
        return g

    def test_disabled_tool_denies_execution(self):
        w, goal_id = self._make_worker_and_goal()
        w.permissions.set_tool_state(
            "test.echo", "disabled", declared_level=2)
        deadline = 1e12
        conn = db.open_conn(self.db_path)
        try:
            goal = self._goal(goal_id)
            with conn:
                db.mark_step_running(conn, goal_id, 1)
            step = self._step(goal_id)
            # re-pend it so _run_step sees a revokeable invocation
            w._run_step(conn, goal, step, deadline)
        finally:
            conn.close()
        final_goal = self._goal(goal_id)
        self.assertEqual(final_goal["error_code"], "permission_denied")
        self.assertEqual(final_goal["status"], "blocked")
        events = []
        conn = db.open_conn(self.db_path, readonly=True)
        try:
            rows = conn.execute(
                "SELECT event, detail FROM goal_events WHERE goal_id=?"
                " AND event='step_failed'"
                " ORDER BY id DESC LIMIT 1",
                (goal_id,)).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows[0][0], "step_failed")
        self.assertIn("permission_denied", rows[0][1])

    def test_cap_below_level_denies_execution(self):
        w, goal_id = self._make_worker_and_goal()
        w.permissions.set_cap(permissions_mod.L1)
        conn = db.open_conn(self.db_path)
        try:
            goal = self._goal(goal_id)
            w._run_step(conn, goal, self._step(goal_id), 1e12)
        finally:
            conn.close()
        self.assertEqual(self._goal(goal_id)["error_code"],
                         "permission_denied")

    def test_cap_l3_runs_normally(self):
        w, goal_id = self._make_worker_and_goal()
        conn = db.open_conn(self.db_path)
        try:
            goal = self._goal(goal_id)
            w._run_step(conn, goal, self._step(goal_id), 1e12)
            steps = db.get_goal_steps(conn, goal_id)
        finally:
            conn.close()
        self.assertEqual(steps[0]["status"], "done")


if __name__ == "__main__":
    unittest.main()
