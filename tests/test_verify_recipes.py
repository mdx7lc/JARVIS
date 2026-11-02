"""Phase 10B — verification recipe unit tests.

These recipes run REAL subprocesses (python syntax check, node --check,
unittest, git diff) against disposable temp workspaces. The '-v
unittest' recipe executes project code by design — only thrown-away
built test files are ever executed here.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

import verification


def workspace(tmp):
    ws = Path(tmp) / "workspace"
    ws.mkdir()
    return ws


class CommonRecipeChecks:
    def assert_recipe_shape(self, result, expect_status=None):
        for key in ("tool", "recipe", "target", "cwd", "status",
                    "exit_code", "stdout", "stderr", "duration_seconds"):
            self.assertIn(key, result, msg=key)
        self.assertIn(result["status"],
                      ("passed", "failed", "unavailable", "timed_out"))
        self.assertIsInstance(result["duration_seconds"], float)
        self.assertGreaterEqual(result["duration_seconds"], 0.0)
        for stream in ("stdout", "stderr"):
            self.assertLessEqual(len(result[stream]),
                                 verification.MAX_OUTPUT_CHARS)
        if expect_status:
            self.assertEqual(result["status"], expect_status)
        semantics = verification.recipe_semantics(result["recipe"], result)
        self.assertIn("passed", semantics)
        if expect_status == "passed":
            self.assertTrue(semantics["passed"])
        elif expect_status == "unavailable":
            self.assertFalse(semantics["passed"])


class PythonSyntaxRecipeTests(unittest.TestCase, CommonRecipeChecks):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-recipe-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = workspace(self._tmp.name)

    def test_valid_python_passes(self):
        (self.ws / "mod.py").write_text("def add(a, b):\n"
                                        "    return a + b\n")
        result = verification.check_python_syntax(self.ws, "mod.py")
        self.assert_recipe_shape(result, "passed")
        self.assertEqual(result["exit_code"], 0)

    def test_syntax_error_fails_with_line_number(self):
        (self.ws / "mod.py").write_text("def broken(:\n  pass\n")
        result = verification.check_python_syntax(self.ws, "mod.py")
        self.assert_recipe_shape(result, "failed")
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("SyntaxError", result["stderr"])

    def test_variant_of_python_is_not_executed(self):
        code = "import os\nos.system('echo EXPLOITED')"
        (self.ws / "bad.py").write_text(code)
        result = verification.check_python_syntax(self.ws, "bad.py")
        self.assertEqual(result["status"], "passed")
        self.assertNotIn("EXPLOITED", result["stdout"] + result["stderr"])

    def test_missing_target_raises_guard_error(self):
        with self.assertRaises(verification.ToolError) as ctx:
            verification.check_python_syntax(self.ws, "ghost.py")
        self.assertEqual(ctx.exception.code, "no_such_path")

    def test_directory_target_raises_guard_error(self):
        (self.ws / "pkg").mkdir()
        with self.assertRaises(verification.ToolError) as ctx:
            verification.check_python_syntax(self.ws, "pkg")
        self.assertEqual(ctx.exception.code, "not_a_regular_file")


class JavaScriptSyntaxRecipeTests(unittest.TestCase, CommonRecipeChecks):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-recipe-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = workspace(self._tmp.name)

    def test_valid_js_passes(self):
        (self.ws / "app.js").write_text(
            "function greet(name) {\n"
            "  return `hello ${name}`;\n"
            "}\n")
        result = verification.check_javascript_syntax(self.ws, "app.js")
        self.assert_recipe_shape(result, "passed")

    def test_syntax_error_fails_with_error(self):
        (self.ws / "app.js").write_text("function broken( {\n")
        result = verification.check_javascript_syntax(self.ws, "app.js")
        self.assert_recipe_shape(result, "failed")
        self.assertTrue(result["stderr"].strip())

    def test_missing_node_is_unavailable(self):
        (self.ws / "app.js").write_text("var x = 1;\n")
        with unittest.mock.patch.object(verification.shutil, "which",
                                        return_value=None):
            result = verification.check_javascript_syntax(self.ws,
                                                          "app.js")
        self.assert_recipe_shape(result, "unavailable")
        self.assertIn("node", result["stderr"])
        semantics = verification.recipe_semantics(result["recipe"], result)
        self.assertFalse(semantics["passed"])


class ProjectTestRecipeTests(unittest.TestCase, CommonRecipeChecks):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-recipe-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = workspace(self._tmp.name)
        (self.ws / "src").mkdir()
        (self.ws / "tests").mkdir()
        (self.ws / "src" / "sum.py").write_text(
            "def sum(values):\n    return sum(values) if values else 0\n"
            if False else
            "def add(a, b):\n    return a + b\n")
        (self.ws / "tests" / "test_sum.py").write_text(
            "import unittest\n"
            "import sys, os\n"
            "sys.path.insert(0, os.path.join(os.getcwd(), 'src'))\n"
            "from sum import add\n"
            "class SumTests(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n")

    def test_passing_tests(self):
        result = verification.run_project_tests(self.ws,
                                                "tests/test_sum.py")
        self.assert_recipe_shape(result, "passed")
        self.assertIn("OK", result["stderr"])

    def test_failing_tests_report_with_diagnostics(self):
        (self.ws / "tests" / "test_sum.py").write_text(
            "import unittest\n"
            "class BrokenTests(unittest.TestCase):\n"
            "    def test_always_fails(self):\n"
            "        self.assertEqual(1, 2)\n")
        result = verification.run_project_tests(self.ws,
                                                "tests/test_sum.py")
        self.assert_recipe_shape(result, "failed")
        self.assertIn("test_always_fails", result["stderr"])
        self.assertIn("AssertionError", result["stderr"])

    def test_import_error_is_fail_not_crash(self):
        (self.ws / "tests" / "test_sum.py").write_text(
            "import unittest\n"
            "from no_such_module import x\n")
        result = verification.run_project_tests(self.ws,
                                                "tests/test_sum.py")
        self.assert_recipe_shape(result, "failed")
        self.assertIn("no_such_module", result["stderr"])

    def test_crashing_code_is_recorded(self):
        (self.ws / "tests" / "test_sum.py").write_text(
            "import os\n"
            "os._exit(7)\n")
        result = verification.run_project_tests(self.ws,
                                                "tests/test_sum.py")
        self.assert_recipe_shape(result, "failed")
        self.assertEqual(result["exit_code"], 7)

    def test_non_py_target_fails_guard(self):
        (self.ws / "notes.md").write_text("not a module\n")
        with self.assertRaises(verification.ToolError) as ctx:
            verification.run_project_tests(self.ws, "notes.md")
        self.assertEqual(ctx.exception.code, "invalid_tool_args")

    def test_bad_module_name_fails_guard(self):
        (self.ws / "tests" / "test-Weird!.py").write_text("x = 1\n")
        with self.assertRaises(verification.ToolError) as ctx:
            verification.run_project_tests(
                self.ws, "tests/test-Weird!.py")
        self.assertEqual(ctx.exception.code, "invalid_tool_args")

    def test_command_is_argv_not_shell(self):
        cmd = verification.project_tests_command("tests/test_sum.py")
        self.assertIsInstance(cmd, list)
        self.assertFalse(any(" " in arg
                             for arg in cmd[1:] if arg != "-v"),
                         msg=cmd)
        self.assertIn("-m", cmd)
        self.assertIn("unittest", cmd)


class GitDiffRecipeTests(unittest.TestCase, CommonRecipeChecks):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-recipe-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = workspace(self._tmp.name)

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.ws, check=True,
                       capture_output=True,
                       env={**os.environ,
                            "GIT_AUTHOR_NAME": "t",
                            "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t"})

    def test_dirty_tree_reports_diff(self):
        self._git("init", "-q")
        (self.ws / "hello.txt").write_text("one\n")
        self._git("add", "hello.txt")
        self._git("commit", "-q", "-m", "first")
        (self.ws / "hello.txt").write_text("one\ntwo\n")
        (self.ws / "new.txt").write_text("staged")
        self._git("add", "new.txt")
        result = verification.inspect_git_diff(self.ws, "")
        self.assert_recipe_shape(result, "passed")
        self.assertIn("two", result["stdout"])

    def test_clean_tree_is_empty_diff(self):
        self._git("init", "-q")
        result = verification.inspect_git_diff(self.ws, "")
        self.assert_recipe_shape(result, "passed")
        self.assertEqual(result["stdout"], "")

    def test_no_repo_is_failed_not_crash(self):
        result = verification.inspect_git_diff(self.ws, "")
        self.assertEqual(result["status"], "failed")

    def test_empty_target_costs_nothing_if_unsupported(self):
        # inspect_git_diff only accepts the workspace root — a file
        # target is a guard refusal.
        (self.ws / "hello.txt").write_text("x\n")
        with self.assertRaises(verification.ToolError) as ctx:
            verification.inspect_git_diff(self.ws, "hello.txt")
        self.assertEqual(ctx.exception.code, "path_forbidden")

    def test_children_receive_the_config_free_env(self):
        self._git("init", "-q")
        (self.ws / "hello.txt").write_text("secret\n")
        self._git("add", "hello.txt")
        captured = {}
        real_popen = verification.subprocess.Popen

        def spy(parts, **kwargs):
            captured.update(kwargs.get("env") or {})
            return real_popen(parts, **kwargs)

        with unittest.mock.patch.object(verification.subprocess,
                                        "Popen", spy):
            verification.inspect_git_diff(self.ws, "")
        self.assertEqual(captured.get("GIT_CONFIG_GLOBAL"), "/dev/null")
        self.assertEqual(captured.get("GIT_CONFIG_SYSTEM"), "/dev/null")
        self.assertEqual(captured.get("GIT_CONFIG_NOSYSTEM"), "1")


class TimeoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-timeout-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = workspace(self._tmp.name)

    def test_slow_tests_time_out(self):
        (self.ws / "tests").mkdir()
        (self.ws / "tests" / "test_slow.py").write_text(
            "import unittest, time\n"
            "class SlowTests(unittest.TestCase):\n"
            "    def test_slow(self):\n"
            "        time.sleep(30)\n")
        with unittest.mock.patch.object(
                verification, "DEFAULT_TIMEOUT_SECONDS", 1):
            result = verification.run_project_tests(
                self.ws, "tests/test_slow.py")
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["exit_code"], None)
        semantics = verification.recipe_semantics(result["recipe"], result)
        self.assertFalse(semantics["passed"])


class EnvSandboxTests(unittest.TestCase):
    def test_child_env_is_minimal(self):
        ws = "/tmp/not-a-real-workspace"
        env = verification._child_env("check_python_syntax", ws)
        self.assertIn("PATH", env)
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["HOME"], ws)
        # Nothing beyond PATH/language/bytecode slips in from the
        # parent environment (secrets, proxies, PYTHONPATH, ...).
        for key in ("OPENAI_API_KEY", "HTTP_PROXY", "PYTHONPATH",
                    "GIT_CONFIG_GLOBAL"):
            if key in os.environ:
                self.assertNotIn(key, env)

    def test_git_env_gets_config_lockdown(self):
        ws = "/tmp/not-a-real-workspace"
        env = verification._child_env("inspect_git_diff", ws)
        self.assertEqual(env.get("GIT_CONFIG_GLOBAL"), "/dev/null")
        self.assertEqual(env.get("GIT_CONFIG_SYSTEM"), "/dev/null")
        self.assertEqual(env.get("GIT_CONFIG_NOSYSTEM"), "1")


class SensitiveOutputTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-secrets-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = workspace(self._tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.ws, check=True,
                       capture_output=True)
        (self.ws / "README.md").write_text("base\n")
        for name in ("openai_token.txt", "secrets.txt",
                     "api_keys.txt", ".env"):
            (self.ws / name).write_text("base\n")
        subprocess.run(["git", "add", "-A"], cwd=self.ws, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=self.ws,
                       check=True, capture_output=True)
        # Unstaged secret modifications — these land in `git diff`.
        for name in ("openai_token.txt", "secrets.txt", "api_keys.txt"):
            (self.ws / name).write_text("very-secret\n")
        (self.ws / "README.md").write_text("public edit\n")

    def _run(self):
        return verification.inspect_git_diff(self.ws, "")

    def test_uncommitted_sensitive_files_never_leak_through(self):
        result = self._run()
        shown = result["stdout"]
        for name in ("openai_token.txt", "secrets.txt", "api_keys.txt"):
            self.assertNotIn(name, shown, msg=name)
        self.assertNotIn("very-secret", shown)
        self.assertIn("public edit", shown)
        # Filtered sections are only flagged honestly via `truncated`.
        self.assertTrue(result["truncated"])
