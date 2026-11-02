"""Phase 10A unit tests: bounded write tools (workspace.create_file /
workspace.edit_file) and their guards. Filesystem-only; no DB, no
provider, no browser."""

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

import tools


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class WriteToolBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-write-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        os.mkdir(self.workspace / "docs")
        self.create = tools.make_create_file_tool(lambda: self.workspace)
        self.edit = tools.make_edit_file_tool(lambda: self.workspace)

    def ws_path(self, rel):
        return self.workspace / rel

    def backups(self):
        backup_dir = self.workspace / ".jarvis" / "backup"
        if not backup_dir.exists():
            return []
        return list(backup_dir.iterdir())


class CreateFileTests(WriteToolBase):
    def test_create_then_verify_independently(self):
        result = self.create.run({"path": "docs/note.txt",
                                  "content": "hello\n"})
        self.assertEqual(result["tool"], "workspace.create_file")
        self.assertEqual(result["sha256"], sha("hello\n"))
        self.assertEqual(result["bytes"], 6)
        self.assertEqual(self.ws_path("docs/note.txt").read_text(),
                         "hello\n")
        verdict = self.create.verify({"content": "hello\n"}, result)
        self.assertTrue(verdict["passed"], verdict)

    def test_verifier_catches_result_digest_tampering(self):
        result = self.create.run({"path": "docs/note.txt",
                                  "content": "hello\n"})
        tampered = dict(result, sha256=sha("WRONG"))
        verdict = self.create.verify({"content": "hello\n"}, tampered)
        self.assertFalse(verdict["passed"])
        tampered2 = dict(result, bytes=99)
        self.assertFalse(self.create.verify(
            {"content": "hello\n"}, tampered2)["passed"])

    def test_verify_catches_post_hoc_file_change(self):
        result = self.create.run({"path": "docs/note.txt",
                                  "content": "hello\n"})
        self.ws_path("docs/note.txt").write_text("hello\nextra")
        verdict = self.create.verify({"content": "hello\n"}, result)
        self.assertFalse(verdict["passed"])

    def test_create_refuses_existing_target(self):
        self.create.run({"path": "docs/note.txt", "content": "one"})
        with self.assertRaises(tools.ToolError) as ctx:
            self.create.run({"path": "docs/note.txt", "content": "two"})
        self.assertEqual(ctx.exception.code, "already_exists")
        self.assertEqual(self.ws_path("docs/note.txt").read_text(), "one")

    def test_create_requires_existing_parent_directory(self):
        with self.assertRaises(tools.ToolError) as ctx:
            self.create.run({"path": "missing/deep/note.txt",
                             "content": "x"})
        self.assertEqual(ctx.exception.code, "no_such_path")
        self.assertFalse(self.ws_path("missing").exists())


class EditFileTests(WriteToolBase):
    def test_edit_replaces_content_and_keeps_backup(self):
        self.create.run({"path": "docs/note.txt", "content": "first"})
        before = sha("first")
        result = self.edit.run({"path": "docs/note.txt",
                                "new_text": "second",
                                "expected_before_sha256": before})
        self.assertEqual(self.ws_path("docs/note.txt").read_text(),
                         "second")
        self.assertEqual(result["before_sha256"], before)
        self.assertIn("backup", result)
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "first")

    def test_edit_precondition_mismatch_refuses(self):
        self.create.run({"path": "docs/note.txt", "content": "first"})
        with self.assertRaises(tools.ToolError) as ctx:
            self.edit.run({"path": "docs/note.txt", "new_text": "second",
                           "expected_before_sha256": sha("other")})
        self.assertEqual(ctx.exception.code, "before_mismatch")
        self.assertEqual(self.ws_path("docs/note.txt").read_text(),
                         "first")
        backups = self.backups()
        self.assertEqual(backups, [])

    def test_edit_missing_target_refuses(self):
        with self.assertRaises(tools.ToolError) as ctx:
            self.edit.run({"path": "docs/absent.txt", "new_text": "x",
                           "expected_before_sha256": sha("x")})
        self.assertEqual(ctx.exception.code, "no_such_path")

    def test_verifier_confirms_new_state(self):
        self.create.run({"path": "docs/note.txt", "content": "first"})
        result = self.edit.run({"path": "docs/note.txt", "new_text": "2nd",
                                "expected_before_sha256": sha("first")})
        verdict = self.edit.verify({"new_text": "2nd"}, result)
        self.assertTrue(verdict["passed"], verdict)


class PathGuardTests(WriteToolBase):
    def test_traversal_refused(self):
        for rel in ("../escape.txt", "docs/../../escape.txt",
                    "docs/..", "", "docs\\win"):
            with self.assertRaises(tools.ToolError) as ctx:
                self.create.run({"path": rel, "content": "x"})
            self.assertEqual(ctx.exception.code, "path_forbidden", rel)

    def test_absolute_path_refused(self):
        with self.assertRaises(tools.ToolError):
            self.create.run({"path": str(self.root / "out.txt"),
                             "content": "x"})
        self.assertFalse(self.ws_path("..") .exists() is None)

    def test_sensitive_names_refused(self):
        for rel in (".ssh/config", ".env", "docs/server.key",
                    "secret_token.txt", "gpg/.gnupg/pubring",
                    "a/credentials.json"):
            with self.assertRaises(tools.ToolError) as ctx:
                self.create.run({"path": rel, "content": "x"})
            self.assertEqual(ctx.exception.code, "path_forbidden", rel)

    def test_meta_directory_refused(self):
        with self.assertRaises(tools.ToolError):
            self.create.run({"path": ".jarvis/evil.txt", "content": "x"})
        with self.assertRaises(tools.ToolError):
            self.edit.run({"path": ".jarvis/backup/victim",
                           "new_text": "x",
                           "expected_before_sha256": sha("x")})

    def test_long_names_refused(self):
        with self.assertRaises(tools.ToolError):
            self.create.run({"path": "d/" + "x" * 201, "content": "x"})


class DefenseInDepthTests(WriteToolBase):
    def test_symlinked_final_component_refused_for_edit(self):
        # a symlink at the target position: never followed
        outside = self.root / "outside.txt"
        outside.write_text("orig")
        os.symlink(outside, self.ws_path("docs/link.txt"))
        with self.assertRaises(tools.ToolError) as ctx:
            self.edit.run({"path": "docs/link.txt", "new_text": "evil",
                           "expected_before_sha256": sha("orig")})
        self.assertIn(ctx.exception.code, ("path_forbidden",
                                           "no_such_path",
                                           "not_a_directory",
                                           "before_mismatch"))
        self.assertEqual(outside.read_text(), "orig")

    def test_symlinked_middle_component_refused(self):
        os.symlink(self.root, self.ws_path("docs/lasymlink"))
        with self.assertRaises(tools.ToolError) as ctx:
            self.create.run({"path": "docs/lasymlink/evil.txt",
                             "content": "x"})
        self.assertIn(ctx.exception.code,
                      ("path_forbidden", "not_a_directory",
                       "no_such_path"))
        self.assertFalse((self.root / "evil.txt").exists())

    def test_content_size_bounds(self):
        with self.assertRaises(tools.ToolError) as ctx:
            self.create.run({"path": "docs/big.txt",
                             "content": "x" * 60001})
        self.assertEqual(ctx.exception.code, "too_large")


class RegistryTests(unittest.TestCase):
    def test_default_registry_exposes_write_tools_correctly(self):
        d = tempfile.mkdtemp()
        registry = tools.default_registry(d)
        self.assertEqual(
            registry.names(),
            ["workspace.check_javascript_syntax",
             "workspace.check_python_syntax",
             "workspace.create_file", "workspace.edit_file",
             "workspace.inspect_git_diff", "workspace.list_files",
             "workspace.read_file", "workspace.run_project_tests"])
        descriptions = registry.descriptions()
        create = descriptions["workspace.create_file"]
        self.assertEqual(create["permission"], "approval_required")
        self.assertEqual(
            set(create["schema"]), {"path", "content"})
        edit = descriptions["workspace.edit_file"]
        self.assertEqual(
            set(edit["schema"]),
            {"path", "new_text", "expected_before_sha256"})

    def test_schema_validation_enforced(self):
        d = tempfile.mkdtemp()
        create = tools.default_registry(d).get("workspace.create_file")
        with self.assertRaises(tools.ToolError):
            create.validate_args({"path": "a.txt"})
        with self.assertRaises(tools.ToolError):
            create.validate_args({"path": "a.txt", "content": "x",
                                  "extra": 1})
        with self.assertRaises(tools.ToolError):
            create.validate_args({"path": "a.txt", "content": 4})
        normalized = create.validate_args({"path": "a.txt",
                                           "content": "x"})
        self.assertEqual(normalized, {"path": "a.txt", "content": "x"})


if __name__ == "__main__":
    unittest.main()
