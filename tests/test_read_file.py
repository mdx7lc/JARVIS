"""Phase 10B — workspace.read_file unit + security tests.

Everything runs against a disposable temp workspace; the tool never
mutates anything it points at. Guards reuse the exact Phase 10A
filesystem primitives (dirfd walk, O_NOFOLLOW, sensitive patterns,
self-root defense).
"""

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

import tools


def sha(data):
    import hashlib
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class ReadFileBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-read-")
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "workspace"
        self.ws.mkdir()
        self.outside = Path(self._tmp.name) / "outside"
        self.outside.mkdir()
        self.tool = tools.make_read_file_tool(lambda: self.ws)
        self.args = {"path": "notes.txt"}


class ReadHappyPathTests(ReadFileBase):
    def test_valid_utf8_text(self):
        (self.ws / "notes.txt").write_text("hello workspace", "utf-8")
        result = self.tool.run(self.args)
        self.assertEqual(result["tool"], "workspace.read_file")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["content"], "hello workspace")
        self.assertEqual(result["path"], "notes.txt")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["sha256"], sha("hello workspace"))
        self.assertEqual(result["total_bytes"],
                         len("hello workspace".encode("utf-8")))
        self.assertTrue(self.tool.verify(self.args, result)["passed"])

    def test_empty_file(self):
        (self.ws / "notes.txt").write_bytes(b"")
        result = self.tool.run(self.args)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["content"], "")
        self.assertEqual(result["total_bytes"], 0)
        self.assertTrue(self.tool.verify(self.args, result)["passed"])

    def test_unicode_text(self):
        (self.ws / "notes.txt").write_text("héllo — Ünïcode ✓",
                                           "utf-8")
        result = self.tool.run(self.args)
        self.assertEqual(result["content"], "héllo — Ünïcode ✓")
        self.assertTrue(self.tool.verify(self.args, result)["passed"])


class ReadBoundsTests(ReadFileBase):
    def test_size_limit_structured_result(self):
        big = Path(self._tmp.name) / "workspace" / "big.txt"
        big.write_bytes(b"x" * (tools._READ_FILE_MAX_BYTES + 1))
        result = self.tool.run({"path": "big.txt"})
        self.assertEqual(result["status"], "too_large")
        self.assertEqual(result["content"], "")
        self.assertGreater(result["total_bytes"],
                           tools._READ_FILE_MAX_BYTES)
        self.assertEqual(result["max_bytes"], tools._READ_FILE_MAX_BYTES)
        verify = self.tool.verify(self.args, result)
        self.assertTrue(verify["passed"], verify)

    def test_truncation_flag(self):
        long_text = "y" * (tools._READ_FILE_MAX_CHARS + 500)
        (self.ws / "notes.txt").write_text(long_text)
        result = self.tool.run(self.args)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["content"]),
                         tools._READ_FILE_MAX_CHARS)
        self.assertEqual(result["total_bytes"],
                         len(long_text.encode("utf-8")))
        self.assertEqual(result["sha256"], sha(long_text))
        self.assertTrue(self.tool.verify(self.args, result)["passed"])

    def test_binary_refused(self):
        (self.ws / "notes.txt").write_bytes(b"\x00\x01\xff\xfebinary")
        with self.assertRaises(tools.ToolError) as ctx:
            self.tool.run(self.args)
        self.assertEqual(ctx.exception.code, "binary_content")

    def test_incomplete_utf8_refused(self):
        (self.ws / "notes.txt").write_bytes(b"ok \xed\xb0\xdd")
        with self.assertRaises(tools.ToolError) as ctx:
            self.tool.run(self.args)
        self.assertEqual(ctx.exception.code, "binary_content")


class ReadRefusalTests(ReadFileBase):
    def test_directory_refused(self):
        (self.ws / "notes.txt").mkdir()
        with self.assertRaises(tools.ToolError) as ctx:
            self.tool.run(self.args)
        self.assertEqual(ctx.exception.code, "not_a_regular_file")

    def test_missing_file(self):
        with self.assertRaises(tools.ToolError) as ctx:
            self.tool.run(self.args)
        self.assertEqual(ctx.exception.code, "no_such_path")

    def test_absolute_path_is_interpreted_relative(self):
        # Same semantics the Phase 10A write tools get from _split_rel:
        # a leading "/" is dropped, so the path stays inside the sandbox.
        (self.ws / "etc").mkdir()
        (self.ws / "etc" / "passwd").write_text("relative")
        result = self.tool.run({"path": "/etc/passwd"})
        self.assertEqual(result["content"], "relative")

    def test_traversal_refused(self):
        (self.outside / "secret.txt").write_text("outside")
        cases = ("../outside/secret.txt", "sub/../secret.txt", "..",
                 "sub\\evil", "a\x00b")
        for rel in cases:
            with self.assertRaises(tools.ToolError) as ctx:
                self.tool.run({"path": rel})
            self.assertEqual(ctx.exception.code,
                             "path_forbidden", msg=rel)

    def test_symlink_final_component_refused(self):
        (self.outside / "key.txt").write_text("secret")
        os.symlink(self.outside / "key.txt", self.ws / "notes.txt")
        with self.assertRaises(tools.ToolError) as ctx:
            self.tool.run(self.args)
        self.assertEqual(ctx.exception.code,
                         "path_forbidden")  # O_NOFOLLOW → ELOOP

    def test_symlink_middle_component_refused(self):
        outside_dir = self.outside / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "notes.txt").write_text("outside secret")
        os.symlink(outside_dir, self.ws / "sub")
        # O_NOFOLLOW + O_DIRECTORY yields ENOTDIR on Linux; ELOOP on
        # other platforms — either way the escape is refused.
        with self.assertRaises(tools.ToolError) as ctx:
            self.tool.run({"path": "sub/notes.txt"})
        self.assertIn(ctx.exception.code,
                      ("path_forbidden", "not_a_directory"))

    def test_symlink_to_file_in_place_of_directory_refused(self):
        (self.outside / "secret").write_text("outside secret")
        os.symlink(self.outside / "secret", self.ws / "sub")
        for rel in ("sub/notes.txt", "sub"):
            with self.assertRaises(tools.ToolError) as ctx:
                self.tool.run({"path": rel})
            self.assertIn(ctx.exception.code,
                          ("path_forbidden", "not_a_directory"),
                          msg=rel)

    def test_sensitive_names_refused(self):
        (self.ws / ".env").write_text("TOP=SECRET")
        (self.ws / "token_store.txt").write_text("s")
        (self.ws / "creds.json").write_text("{}")
        (self.ws / ".jarvis").mkdir()
        (self.ws / ".jarvis" / "backup.txt").write_text("meta")
        for rel in (".env", "token_store.txt", "creds.json",
                    ".jarvis/backup.txt", ".jarvis", "key.pem",
                    "credentials.json", "id_rsa.pub", "db.password"):
            with self.assertRaises(tools.ToolError) as ctx:
                self.tool.run({"path": rel})
            self.assertEqual(ctx.exception.code, "path_forbidden",
                             msg=rel)

    def test_jarvis_source_root_refused(self):
        """Defense in depth: reading with the workspace root pointed at
        JARVIS's own source tree is refused."""
        tool = tools.make_read_file_tool(lambda: Path(tools.__file__))
        with self.assertRaises(tools.ToolError) as ctx:
            tool.run({"path": "tools.py"})
        self.assertEqual(ctx.exception.code, "self_root_forbidden")


class ReadNoMutationTests(ReadFileBase):
    def test_reading_never_modifies_the_file(self):
        content = "immutable"
        target = self.ws / "notes.txt"
        target.write_text(content)
        before_stat = target.stat()
        time.sleep(0.02)  # mtime granularity
        for _ in range(3):
            result = self.tool.run(self.args)
            self.assertEqual(result["content"], content)
        after_stat = target.stat()
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
        self.assertEqual(after_stat.st_size, len(content))
        self.assertTrue((target.read_text()) == content)
        self.assertEqual(before_stat.st_mode, after_stat.st_mode)
        self.assertEqual(after_stat.st_mode & stat.S_IWUSR, stat.S_IWUSR)


class ReadVerifierTests(ReadFileBase):
    def setUp(self):
        super().setUp()
        (self.ws / "notes.txt").write_text("plain text")

    def _run(self):
        return self.tool.run(self.args)

    def test_tampered_hash_fails_verification(self):
        result = self._run()
        tampered = dict(result, sha256=sha("forged"))
        verdict = self.tool.verify(self.args, tampered)
        self.assertFalse(verdict["passed"])
        self.assertIn("hash", verdict["detail"])

    def test_tampered_size_fails_verification(self):
        result = self._run()
        tampered = dict(result, total_bytes=999)
        verdict = self.tool.verify(self.args, tampered)
        self.assertFalse(verdict["passed"])

    def test_tampered_truncation_fails_verification(self):
        result = self._run()  # not truncated here
        tampered = dict(result, truncated=True)
        verdict = self.tool.verify(self.args, tampered)
        self.assertFalse(verdict["passed"])

    def test_escaping_path_result_fails_verification(self):
        result = self._run()
        tampered = dict(result, path="../outside/x")
        verdict = self.tool.verify(self.args, tampered)
        self.assertFalse(verdict["passed"])

    def test_missing_hash_shape_fails(self):
        result = {"tool": "workspace.read_file", "path": 5,
                  "status": "ok"}
        verdict = self.tool.verify(self.args, result)
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["detail"], "read result shape invalid")

    def test_changed_file_invalidates_stale_result(self):
        result = self._run()
        (self.ws / "notes.txt").write_text("changed after read")
        verdict = self.tool.verify(self.args, result)
        self.assertFalse(verdict["passed"])


if __name__ == "__main__":
    unittest.main()
