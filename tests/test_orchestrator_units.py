"""Phase 8B unit tests: state machine, planner validation, tool registry.

These test the pure-Python modules directly (no server, no network).
Every pass here is a unit check of contract behavior, not AI evidence.
"""
import json
import tempfile
import unittest
import contextlib
import shutil
from pathlib import Path

import goalstate
import planner
import tools
import reviewer


@contextlib.contextmanager
def _tmpdir():
    d = tempfile.mkdtemp(prefix="jarvis-tool-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


class GoalStateMachineTests(unittest.TestCase):
    def test_full_happy_path_transitions(self):
        path = ("queued", "planning", "ready", "executing",
                "verifying", "succeeded")
        for current, nxt in zip(path, path[1:]):
            self.assertEqual(goalstate.transition(current, nxt), nxt)

    def test_idempotent_same_state_allowed(self):
        for status in ("queued", "planning", "ready", "executing",
                       "verifying", "blocked"):
            self.assertEqual(goalstate.transition(status, status), status)

    def test_illegal_transitions_raise(self):
        bad = [
            ("queued", "executing"),
            ("queued", "succeeded"),
            ("queued", "failed"),
            ("planning", "executing"),
            ("ready", "succeeded"),
            ("executing", "succeeded"),
            ("verifying", "executing"),
            ("succeeded", "failed"),
            ("succeeded", "queued"),
            ("cancelled", "queued"),
            ("failed", "queued"),
            ("blocked", "executing"),
            ("blocked", "verifying"),
            ("blocked", "planning"),
            ("unknown", "queued"),
            ("queued", "nonsense"),
        ]
        for current, nxt in bad:
            with self.assertRaises(ValueError):
                goalstate.transition(current, nxt)

    def test_blocked_only_returns_via_queued(self):
        self.assertEqual(goalstate.transition("blocked", "queued"),
                         "queued")

    def test_cancellable_matrix(self):
        for status, expected in (
                ("queued", True), ("planning", True), ("ready", True),
                ("executing", True), ("verifying", True),
                ("blocked", True), ("succeeded", False),
                ("failed", False), ("cancelled", False)):
            self.assertEqual(goalstate.cancellable(status), expected,
                             status)

    def test_retryable_matrix(self):
        for code in goalstate.BLOCKED_CODES_REPORTED:
            expected = code not in ("reconciliation_required",
                                    "approval_withdrawn")
            self.assertEqual(goalstate.retryable("blocked", code),
                             expected, code)
        self.assertFalse(goalstate.retryable("failed", "upstream_error"))
        self.assertFalse(goalstate.retryable("succeeded", None))
        self.assertFalse(goalstate.retryable("queued", None))

    def test_terminal(self):
        for status in ("succeeded", "failed", "cancelled"):
            self.assertTrue(goalstate.is_terminal(status))
        for status in ("queued", "planning", "ready", "executing",
                       "verifying", "blocked"):
            self.assertFalse(goalstate.is_terminal(status))


def _plan(**over):
    plan = {
        "summary": "Inventory the workspace",
        "outcome": "A workspace inventory is recorded and reported.",
        "criteria": ["accurate inventory of workspace source, tests, "
                     "and docs"],
        "risk": "low",
        "uncertainties": [],
        "steps": [
            {"purpose": "List the workspace files accurately",
             "tool": "workspace.list_files", "args": {"path": ""},
             "depends_on": [], "acceptance":
                 "accurate inventory of workspace source, tests, and "
                 "docs"},
        ],
    }
    plan.update(over)
    return plan


class PlannerValidationTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(_tmpdir())
        (tmp / "src").mkdir()
        (tmp / "src" / "a.py").write_text("x = 1\n")
        self.registry = tools.default_registry(tmp)

    def _text(self, plan=None):
        return json.dumps(plan if plan is not None else _plan())

    def test_valid_plan_passes(self):
        plan, reason, _ = planner.validate_plan(self._text(),
                                                self.registry)
        self.assertIsNone(reason)
        self.assertEqual(plan["risk"], "low")
        self.assertEqual(len(plan["steps"]), 1)

    def test_plain_prose_rejected(self):
        _, reason, _ = planner.validate_plan(
            "I will list the files for you!", self.registry)
        self.assertEqual(reason, "not_json")

    def test_fenced_json_is_stripped(self):
        text = "```json\n" + self._text() + "\n```"
        _, reason, _ = planner.validate_plan(text, self.registry)
        self.assertIsNone(reason)

    def test_top_level_not_object_rejected(self):
        for text in ("[]", '"a string"', "42", "null"):
            _, reason, _ = planner.validate_plan(text, self.registry)
            self.assertEqual(reason, "invalid_plan", text)

    def test_unknown_top_level_field_rejected(self):
        plan = _plan(motivation="please just trust me")
        _, reason, _ = planner.validate_plan(self._text(plan),
                                             self.registry)
        self.assertEqual(reason, "invalid_plan")

    def test_step_unknown_field_rejected(self):
        step = dict(_plan()["steps"][0], shell="rm -rf /")
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[step])), self.registry)
        self.assertEqual(reason, "invalid_plan")

    def test_missing_or_empty_fields_rejected(self):
        for drop in ("summary", "outcome", "criteria", "steps"):
            plan = _plan()
            plan.pop(drop)
            _, reason, _ = planner.validate_plan(
                self._text(plan), self.registry)
            self.assertEqual(reason, "invalid_plan", drop)
        plan = _plan(summary="   ")
        _, reason, _ = planner.validate_plan(self._text(plan),
                                             self.registry)
        self.assertEqual(reason, "invalid_plan")
        plan = _plan(outcome="")
        _, reason, _ = planner.validate_plan(self._text(plan),
                                             self.registry)
        self.assertEqual(reason, "invalid_plan")

    def test_missing_risk_is_risk_not_low(self):
        plan = _plan()
        plan.pop("risk")
        _, reason, _ = planner.validate_plan(self._text(plan),
                                             self.registry)
        self.assertEqual(reason, "risk_not_low")

    def test_criteria_shape_enforced(self):
        for criteria in ([], [42], ["  "], "one string", None,
                         ["ok " * 101]):
            _, reason, _ = planner.validate_plan(
                self._text(_plan(criteria=criteria)), self.registry)
            self.assertEqual(reason, "invalid_plan", criteria)
        # a shape-valid criterion whose tokens are all stop/short words
        # passes shape but fails coverage (no step can "cover" noise)
        plan = _plan(criteria=["ok " * 100])
        _, reason, _ = planner.validate_plan(self._text(plan),
                                             self.registry)
        self.assertEqual(reason, "criteria_not_covered")

    def test_risk_must_be_lowercase_low(self):
        for risk in ("high", "medium", "LOW", "none", None, 0):
            _, reason, _ = planner.validate_plan(
                self._text(_plan(risk=risk)), self.registry)
            self.assertEqual(reason, "risk_not_low", risk)

    def test_steps_bounds_and_shape(self):
        for steps in ([], None, ["list files"], [{"tool": "x"}], {}):
            _, reason, _ = planner.validate_plan(
                self._text(_plan(steps=steps)), self.registry)
            self.assertEqual(reason, "invalid_plan", steps)

    def test_twelve_steps_ok_thirteen_rejected(self):
        base = _plan()["steps"][0]
        extra = [dict(base, purpose=f"Listing pass {i}",
                      acceptance=base["acceptance"]) for i in range(1, 12)]
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[base] + extra)), self.registry)
        self.assertIsNone(reason)
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[base] + extra
                             + [dict(base, purpose="One pass too many",
                                    acceptance=base["acceptance"])])),
            self.registry)
        self.assertEqual(reason, "invalid_plan")

    def test_step_text_bounds(self):
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[
                dict(_plan()["steps"][0], purpose="p" * 201)])),
            self.registry)
        self.assertEqual(reason, "invalid_plan")
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[
                dict(_plan()["steps"][0], acceptance="a" * 301)])),
            self.registry)
        self.assertEqual(reason, "invalid_plan")

    def test_unknown_tool_rejected(self):
        step = dict(_plan()["steps"][0], tool="shell.run")
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[step])), self.registry)
        self.assertEqual(reason, "unsupported_capability")

    def test_invalid_args_rejected(self):
        step = dict(_plan()["steps"][0], args={"path": 17})
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[step])), self.registry)
        self.assertEqual(reason, "invalid_tool_args")
        step = dict(_plan()["steps"][0], args={"surprise": True})
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[step])), self.registry)
        self.assertEqual(reason, "invalid_tool_args")

    def test_args_normalized_by_registry(self):
        step = dict(_plan()["steps"][0], args={"path": "src"})
        plan, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[step])), self.registry)
        self.assertIsNone(reason)
        self.assertEqual(plan["steps"][0]["args"], {"path": "src"})

    def test_forward_dependency_rejected(self):
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[
                dict(_plan()["steps"][0], depends_on=[2])])),
            self.registry)
        self.assertEqual(reason, "invalid_dependencies")

    def test_dependency_shape_rejects(self):
        step = dict(_plan()["steps"][0], depends_on=[1, 1])
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[step])), self.registry)
        self.assertEqual(reason, "invalid_dependencies")
        step = dict(_plan()["steps"][0], depends_on=["1"])
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[step])), self.registry)
        self.assertEqual(reason, "invalid_dependencies")
        step = dict(_plan()["steps"][0], depends_on=[True])
        _, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[step])), self.registry)
        self.assertEqual(reason, "invalid_dependencies")

    def test_dependencies_form_dag_by_construction(self):
        base = _plan()["steps"][0]
        second = dict(base, purpose="Second listing pass",
                      depends_on=[1])
        plan, reason, _ = planner.validate_plan(
            self._text(_plan(steps=[base, second])), self.registry)
        self.assertIsNone(reason)
        dep = plan["steps"][1]["depends_on"]
        self.assertTrue(all(1 <= d < 2 for d in dep))

    def test_criteria_coverage_required(self):
        plan = _plan(criteria=["the picnic basket stays locked tonight"])
        _, reason, _ = planner.validate_plan(self._text(plan),
                                             self.registry)
        self.assertEqual(reason, "criteria_not_covered")

    def test_coverage_via_purpose_or_acceptance(self):
        plan = _plan(criteria=["workspace files listed accurately"])
        _, reason, _ = planner.validate_plan(self._text(plan),
                                             self.registry)
        self.assertIsNone(reason)

    def test_oversized_summary_rejected(self):
        _, reason, _ = planner.validate_plan(
            self._text(_plan(summary="word " * 300)), self.registry)
        self.assertEqual(reason, "invalid_plan")

    def test_prompt_carries_objective_and_capabilities(self):
        text = planner.plan_prompt("Count sticky notes", self.registry)
        self.assertIn("Count sticky notes", text)
        self.assertIn("workspace.list_files", text)
        self.assertIn('"risk"', text)

    def test_significant_tokens_contract(self):
        # ≥ 3 chars, letters/digits runs, stop words dropped
        self.assertEqual(
            planner.significant_tokens("a the and-or b bob"),
            {"bob"})
        self.assertEqual(planner.significant_tokens("TESTS tests"),
                         {"tests"})
        self.assertEqual(planner.significant_tokens(""), set())


class ReviewerLocalCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(_tmpdir())
        (tmp / "src").mkdir()
        (tmp / "src" / "a.py").write_text("x = 1\n")
        self.registry = tools.default_registry(tmp)

    def test_registered_autonomous_tool_approved(self):
        result = reviewer.local_safety_check(_plan(), self.registry)
        self.assertTrue(result["approved"])
        self.assertEqual(result["reason"], "local_validation")

    def test_unknown_tool_rejected(self):
        step = dict(_plan()["steps"][0], tool="shell.run")
        result = reviewer.local_safety_check(
            _plan(steps=[step]), self.registry)
        self.assertFalse(result["approved"])
        self.assertEqual(result["reason"], "unsupported_capability")

    def test_non_autonomous_permission_rejected(self):
        custom = tools.Tool(
            "notes.write", "writes a note",
            {}, "autonomous_under_attack", False,
            run=lambda args: {"ok": True},
            verifier=lambda args, result: {"passed": True})
        registry = tools.ToolRegistry([custom])
        step = dict(_plan()["steps"][0], tool="notes.write")
        result = reviewer.local_safety_check(
            _plan(steps=[step]), registry)
        self.assertFalse(result["approved"])
        self.assertEqual(result["reason"], "permission_unsupported")

    def test_approval_required_plan_needs_external_review(self):
        custom = tools.Tool(
            "notes.write", "writes a note",
            {}, "approval_required", False,
            run=lambda args: {"ok": True},
            verifier=lambda args, result: {"passed": True})
        registry = tools.ToolRegistry([custom])
        step = dict(_plan()["steps"][0], tool="notes.write")
        result = reviewer.local_safety_check(
            _plan(steps=[step]), registry)
        self.assertTrue(result["approved"])
        self.assertTrue(result["needs_external_review"])

    def test_read_only_plan_does_not_need_external_review(self):
        result = reviewer.local_safety_check(_plan(), self.registry)
        self.assertTrue(result["approved"])
        self.assertFalse(result["needs_external_review"])

    def test_non_low_risk_rejected(self):
        result = reviewer.local_safety_check(_plan(risk="high"),
                                             self.registry)
        self.assertFalse(result["approved"])

    def test_unavailable_reviewer_never_approves(self):
        result = reviewer.UnavailableReviewer().review(_plan(),
                                                       self.registry)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "not_configured")

    def test_phase_ships_no_external_reviewer(self):
        self.assertIsNone(reviewer.PLANNED_EXTERNAL_REVIEWER)


class WorkspaceToolTests(unittest.TestCase):
    """workspace.list_files: traversal, symlinks, exclusions, bounds,
    verifier postconditions."""

    def setUp(self):
        tmp = self.enterContext(_tmpdir())
        (tmp / "src").mkdir()
        (tmp / "tests").mkdir()
        (tmp / "docs").mkdir()
        (tmp / "src" / "app.py").write_text("x = 1\n")
        (tmp / "src" / "sub").mkdir()
        (tmp / "src" / "sub" / "deep.py").write_text("y = 2\n")
        (tmp / "tests" / "test_a.py").write_text("pass\n")
        (tmp / "docs" / "readme.md").write_text("# hi\n")
        (tmp / "notes.txt").write_text("hello\n")
        self.td = tmp
        self.registry = tools.default_registry(tmp)
        self.tool = self.registry.get("workspace.list_files")
        self.assertIsNotNone(self.tool)

    def _run(self, raw_args):
        args = self.tool.validate_args(raw_args)
        return self.tool.run(args)

    def test_lists_entries_recursive_relative(self):
        result = self._run({"path": ""})
        names = [e["path"] for e in result["entries"]]
        for expected in ("src/app.py", "src/sub/deep.py",
                         "tests/test_a.py", "docs/readme.md",
                         "notes.txt", "src", "src/sub"):
            self.assertIn(expected, names, expected)
        for entry in result["entries"]:
            self.assertIn(entry["type"], ("file", "dir"))
            self.assertTrue(entry["path"])
            self.assertFalse(entry["path"].startswith("/"))
        self.assertFalse(result["truncated"])
        self.assertEqual(result["count"], len(result["entries"]))

    def test_nested_path_scopes_result(self):
        # entries always carry workspace-root-relative paths
        result = self._run({"path": "src"})
        names = [e["path"] for e in result["entries"]]
        self.assertIn("src/app.py", names)
        self.assertIn("src/sub/deep.py", names)
        self.assertNotIn("notes.txt", names)
        self.assertNotIn("tests/test_a.py", names)

    def test_dots_entries_listed_unless_excluded(self):
        (self.td / "src" / ".hidden").write_text("sh\n")
        (self.td / "src" / ".git").mkdir()
        (self.td / "src" / ".git" / "config").write_text("[x]\n")
        result = self._run({"path": "src"})
        names = [e["path"] for e in result["entries"]]
        self.assertIn("src/.hidden", names)
        self.assertTrue(all(".git" not in n for n in names))

    def test_missing_path_is_no_such_path(self):
        try:
            self._run({"path": "no/such/dir"})
        except tools.ToolError as err:
            self.assertEqual(err.code, "no_such_path")
        else:
            self.fail("expected ToolError")

    def test_file_path_is_not_a_directory(self):
        try:
            self._run({"path": "notes.txt"})
        except tools.ToolError as err:
            self.assertEqual(err.code, "not_a_directory")
        else:
            self.fail("expected ToolError (path is a file)")

    def test_traversal_escaped(self):
        try:
            self._run({"path": "../outside"})
        except tools.ToolError as err:
            self.assertEqual(err.code, "path_forbidden")
        else:
            self.fail("expected ToolError")

    def test_symlink_escape_rejected(self):
        outside = Path(tempfile.mkdtemp(prefix="jarvis-out-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "secret.txt").write_text("s")
        (self.td / "link").symlink_to(outside)
        try:
            self._run({"path": "link"})
        except tools.ToolError as err:
            self.assertEqual(err.code, "path_forbidden")
        else:
            self.fail("expected ToolError")
        result = self._run({"path": ""})
        self.assertTrue(all("link" != e["path"]
                            for e in result["entries"]))
        self.assertTrue(all("secret" not in e["path"]
                            for e in result["entries"]))

    def test_names_and_patterns_excluded_everywhere(self):
        (self.td / ".git").mkdir()
        (self.td / ".git" / "config").write_text("[x]\n")
        (self.td / ".env").write_text("K=v\n")
        (self.td / ".env.local").write_text("K=v\n")
        (self.td / "server.pem").write_text("cert")
        (self.td / "id_rsa.key").write_text("key")
        (self.td / "credentials.json").write_text("{}")
        (self.td / "my-secrets_dir").mkdir()
        (self.td / "my-secrets_dir" / "f.txt").write_text("x")
        (self.td / "api_token_store.txt").write_text("x")
        (self.td / "readme.md").write_text("fine\n")
        (self.td / "gun.keychain").write_text("not a key")
        result = self._run({"path": ""})
        blob = " ".join(e["path"] for e in result["entries"])
        for absent in (".git", ".env", ".env.local", "server.pem",
                       "id_rsa.key", "credentials.json",
                       "my-secrets_dir", "api_token_store.txt",
                       "secret.txt"):
            self.assertNotIn(absent, blob)
        self.assertIn("readme.md", blob)
        self.assertIn("gun.keychain", blob)

    def test_depth_limit_8(self):
        deep = self.td
        for i in range(12):
            deep = deep / f"d{i}"
            deep.mkdir()
        (deep / "bottom.txt").write_text("x")
        result = self._run({"path": ""})
        names = [e["path"] for e in result["entries"]]
        self.assertFalse(any("bottom.txt" in n for n in names))
        # shallower levels are listed; deep ones are honestly absent
        self.assertTrue(any("d0/d1/d2" in n for n in names))

    def test_entry_cap_truncates_honestly(self):
        for i in range(1500):
            (self.td / "src" / f"f{i:04d}.py").write_text("x")
        result = self._run({"path": "src"})
        self.assertEqual(len(result["entries"]), tools.MAX_ENTRIES)
        self.assertTrue(result["truncated"])

    def test_byte_bound_holds_with_truncation(self):
        for i in range(1200):
            (self.td / "src" / f"x{i:04d}.py").write_text("x" * 40)
        result = self._run({"path": "src"})
        body = json.dumps(result).encode()
        self.assertLessEqual(len(body), tools.MAX_RESULT_BYTES)
        self.assertTrue(result["truncated"])

    def test_args_validation_by_tool_schema(self):
        for bad in ({"path": 5}, {"path": {"a": 1}}, {"path": [True]},
                    {"flat": True}, {"surprise": 1}, {"path": "a"},
                    ):
            if bad == {"path": "a"}:
                continue
            with self.assertRaises(tools.ToolError, msg=bad):
                self.tool.validate_args(bad)
        # bool path is not a string
        with self.assertRaises(tools.ToolError):
            self.tool.validate_args({"path": False})
        # valid shapes normalize; empty path becomes "" default
        self.assertEqual(self.tool.validate_args({}), {"path": ""})
        self.assertEqual(self.tool.validate_args({"path": "src"}),
                         {"path": "src"})
        self.assertEqual(self.tool.validate_args({"path": ""}),
                         {"path": ""})

    def test_schema_and_metadata_declared(self):
        self.assertEqual(self.tool.permission, "autonomous")
        self.assertTrue(self.tool.idempotent)
        self.assertEqual(self.tool.name, "workspace.list_files")
        self.assertIn("path", self.tool.schema)

    def test_verifier_flags_shape(self):
        args = {"path": ""}
        result = self._run(args)
        post = self.tool.verify(args, result)
        self.assertTrue(post["passed"])
        tampered = dict(result)
        tampered["entries"] = [{"path": "etc/passwd", "type": "file"}]
        post = self.tool.verify(args, tampered)
        self.assertFalse(post["passed"])
        tampered = dict(result, count=77)
        post = self.tool.verify(args, tampered)
        self.assertFalse(post["passed"])
        tampered = dict(result, tool="workspace.write_files")
        post = self.tool.verify(args, tampered)
        self.assertFalse(post["passed"])
        tampered = dict(result, truncated="maybe")
        post = self.tool.verify(args, tampered)
        self.assertFalse(post["passed"])

    def test_verifier_rejects_entries_outside_scoped_path(self):
        args = {"path": "src"}
        result = self._run(args)
        self.assertTrue(self.tool.verify(args, result)["passed"])
        tampered = dict(result, entries=[{"path": "notes.txt",
                                          "type": "file"}])
        post = self.tool.verify(args, tampered)
        self.assertFalse(post["passed"])

    def test_registry_is_capability_boundary(self):
        self.assertEqual(
            self.registry.names(),
            ["workspace.check_javascript_syntax",
             "workspace.check_python_syntax",
             "workspace.create_file", "workspace.edit_file",
             "workspace.inspect_git_diff", "workspace.list_files",
             "workspace.read_file", "workspace.run_project_tests"])
        self.assertIsNone(self.registry.get("shell.run"))
        descriptions = self.registry.descriptions()
        self.assertEqual(
            descriptions["workspace.list_files"]["permission"],
            "autonomous")
        self.assertEqual(
            descriptions["workspace.create_file"]["permission"],
            "approval_required")
        self.assertEqual(
            descriptions["workspace.edit_file"]["permission"],
            "approval_required")
        self.assertNotIn("run", descriptions["workspace.list_files"])
        self.assertNotIn("run", descriptions["workspace.create_file"])


if __name__ == "__main__":
    unittest.main()
