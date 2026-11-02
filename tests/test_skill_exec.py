"""Isolated skill execution tests (Phase 14B).

Covers the fail-closed gates (trust, enable, permission engine, manifest
entrypoint shape, sandbox availability, permission cap) plus real
bubblewrap isolation (network unshared, workspace-only writes, timeout,
secret redaction) and honest verification semantics.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch as mock_patch

import db
import skillexec
import skills as skills_mod

MANIFEST = """---
name: counter
description: writes a counter file in the workspace
version: 1.0.0
entrypoint: run.py
timeout: 10
---
A tiny trusted skill used by the execution tests."""

GOOD_RUN = """
import json, os
args = json.loads(os.environ.get("SKILL_ARGS") or "{}")
print("ARGS-KEY:", args.get("api_key"))
with open("/workspace/out.txt", "w") as fh:
    fh.write("ok")
try:
    import socket
    socket.create_connection(("203.0.113.10", 80), 1)
    print("NET-OK")
except OSError as exc:
    print("NET-BLOCKED", type(exc).__name__)
try:
    open("/usr/bin/jarvis-evil-probe", "w").write("x")
    print("WRITE-OK")
except OSError as exc:
    print("WRITE-BLOCKED", getattr(exc, "errno", "?"))
"""


class _Case(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jarvis-skill-exec-"))
        self.addCleanup(self._rmtree, self.dir)
        self.db_path = self.dir / "db.sqlite"
        db.init_db(self.db_path)
        self.skill_dir = self.dir / "skills" / "counter"
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "SKILL.md").write_text(MANIFEST)
        (self.skill_dir / "run.py").write_text(GOOD_RUN)
        # fresh DMBS connection for registry state
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            skills_mod.sync_registry(self.conn, [self.skill_dir.parent],
                                     "t0")

    def _rmtree(self, path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def _record(self, *, state="enabled", trust="trusted"):
        with self.conn:
            skills_mod.set_state(self.conn, "counter", state)
            skills_mod.set_trust(self.conn, "counter", trust)
        row = dict(self.conn.execute(
            "SELECT * FROM skills WHERE id='counter'").fetchone())
        row["fields"] = skills_mod.parse_manifest(
            (self.skill_dir / "SKILL.md").read_text())
        return row


class TrustAndManifestGates(_Case):
    def test_disabled_skill_refused(self):
        row = self._record(state="disabled")
        with self.assertRaises(skillexec.ExecError) as ctx:
            skillexec.run(row, {}, self.dir / "ws")
        self.assertEqual(ctx.exception.code, "skill_disabled")

    def test_untrusted_skill_refused(self):
        row = self._record(trust="untrusted")
        with self.assertRaises(skillexec.ExecError) as ctx:
            skillexec.run(row, {}, self.dir / "ws")
        self.assertEqual(ctx.exception.code, "skill_untrusted")

    def test_no_entrypoint_is_not_executable(self):
        (self.skill_dir / "SKILL.md").write_text(
            "---\nname: counter\ndescription: no entrypoint\n---\n")
        row = self._record()
        with self.assertRaises(skillexec.ExecError) as ctx:
            skillexec.run(row, {}, self.dir / "ws")
        self.assertEqual(ctx.exception.code, "no_entrypoint")

    def test_traversal_entrypoint_refused(self):
        fields = {"entrypoint": "../escape.py", "timeout": "5"}
        with self.assertRaises(skillexec.ExecError) as ctx:
            skillexec.validate_execution(
                {"source": str(self.skill_dir), "fields": fields})
        self.assertEqual(ctx.exception.code, "invalid_entrypoint")

    def test_symlink_entrypoint_escape_refused(self):
        outside = self.dir / "outside.py"
        outside.write_text("print('escape')\n")
        link = self.skill_dir / "es.py"
        link.symlink_to(outside)
        fields = {"entrypoint": "es.py", "timeout": "5"}
        with self.assertRaises(skillexec.ExecError) as ctx:
            skillexec.validate_execution(
                {"source": str(self.skill_dir), "fields": fields})
        self.assertEqual(ctx.exception.code, "entrypoint_escape")

    def test_missing_entrypoint_refused(self):
        fields = {"entrypoint": "ghost.py", "timeout": "5"}
        with self.assertRaises(skillexec.ExecError) as ctx:
            skillexec.validate_execution(
                {"source": str(self.skill_dir), "fields": fields})
        self.assertEqual(ctx.exception.code, "entrypoint_missing")


class SandboxGates(_Case):
    def test_missing_sandbox_fails_closed(self):
        row = self._record()
        with mock_patch.object(skillexec, "sandbox_available",
                               return_value=False):
            with self.assertRaises(skillexec.ExecError) as ctx:
                skillexec.run(row, {}, self.dir / "ws")
        self.assertEqual(ctx.exception.code, "sandbox_unavailable")

    def test_permission_cap_denies_l3_skill_run(self):
        row = self._record()
        import permissions
        engine = permissions.PermissionEngine(self.db_path)
        engine.set_cap(1)  # cap below the skill's L3 requirement
        with self.assertRaises(skillexec.ExecError) as ctx:
            skillexec.run(row, {}, self.dir / "ws", engine=engine)
        self.assertEqual(ctx.exception.code, "permission_denied")
        # every denial audited by the central engine
        row2 = self.conn.execute(
            "SELECT decision, reason FROM permission_audit"
            " WHERE tool_name LIKE 'skill.%'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row2["decision"], "denied")
        self.assertEqual(row2["reason"], "capability_cap_exceeded")

    def test_invalid_args_refused(self):
        row = self._record()
        with self.assertRaises(skillexec.ExecError) as ctx:
            skillexec.run(row, ["not", "an", "object"], self.dir / "ws")
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_secret_values_redacted(self):
        cleaned = skillexec.redact_args({"api_key": "TOPSECRET-VALUE",
                                         "name": "counter"})
        self.assertEqual(cleaned["api_key"], "***redacted***")
        self.assertEqual(cleaned["name"], "counter")


class RealSandboxExecution(_Case):
    """Only meaningful when bubblewrap is present (documented skip)."""

    def setUp(self):
        super().setUp()
        if not skillexec.sandbox_available():
            self.skipTest("bubblewrap not installed")

    def test_successful_sandbox_run_redacts_and_blocks_network(self):
        import shutil
        ws = self.dir / "ws"
        row = self._record()
        result = skillexec.run(row, {"api_key": "TOPSECRET-VALUE"}, ws)
        out = result["stdout"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("verified", result["verification"].lower())
        # redaction survives into the sandboxed process environment
        self.assertNotIn("TOPSECRET-VALUE", out)
        self.assertIn("***redacted***", out)
        # network is unshared
        self.assertIn("NET-BLOCKED", out)
        # only the workspace is writable
        self.assertIn("WRITE-BLOCKED", out)
        # workspace artifact exists; host skill dir untouched
        self.assertTrue((ws / "out.txt").exists())
        self.assertNotIn("jarvis-evil-probe",
                         os_list_dir("/usr/bin"))
        # the run is persisted into skill_runs by the server layer;
        # module returned a complete record
        self.assertLessEqual(len(result["stdout"]), skillexec._MAX_OUTPUT)
        shutil.rmtree(ws, ignore_errors=True)

    def test_timeout_kills_the_run(self):
        (self.skill_dir / "run.py").write_text(
            "import time; time.sleep(5)\n")
        row = self._record()
        fields = row["fields"]
        fields["timeout"] = "1"
        row["fields"] = fields
        result = skillexec.run(row, {}, self.dir / "ws2")
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["exit_code"], -1)

    def test_failing_exit_is_failed_not_verified(self):
        (self.skill_dir / "run.py").write_text(
            "import sys; sys.exit(3)\n")
        row = self._record()
        result = skillexec.run(row, {}, self.dir / "ws3")
        self.assertEqual(result["status"], "failed")
        # "nothing verified" is honest — but "verified:" wording must
        # not appear on a failed run
        self.assertNotIn("verified: exit 0",
                         result["verification"].lower())


def os_list_dir(path):
    import os
    return os.listdir(path)


if __name__ == "__main__":
    unittest.main()
