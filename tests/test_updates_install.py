"""Verified update staging / apply / rollback tests (Phase 14D).

Integrity metadata is mandatory (pinned sha256 per file, total size
bounds); protected user-data paths are refused; locally modified target
files are never replaced without an explicit overwrite confirmation;
apply is bound to the staged digest, re-verifies every byte, records a
restorable backup footprint, and rollback restores/removes exactly the
recorded content.
"""

import http.server
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import db
import updates


def sha(data):
    return hashlib.sha256(data).hexdigest()


class _Case(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jarvis-upd-install-"))
        self.addCleanup(self._rmtree, self.dir)
        self.db_path = self.dir / "db.sqlite"
        db.init_db(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.target = self.dir / "target"
        self.target.mkdir()

    def _rmtree(self, path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def _start_source(self, manifest, files):
        """A loopback JSON+octet-stream source (http is allowed ONLY
        for 127.0.0.1 hosts — the same trust model as the rest of
        JARVIS; remote sources require https)."""

        class Source(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/manifest.json":
                    body = json.dumps(manifest).encode()
                    ctype = "application/json"
                else:
                    rel = self.path.lstrip("/")
                    payload = files.get(rel)
                    if payload is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = payload
                    ctype = "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Source)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def _wrap(self, manifest, files):
        url = self._start_source(manifest, files)
        for entry in manifest.get("files") or []:
            if isinstance(entry, dict) and entry.get("path"):
                entry.setdefault("url", url + "/" + entry["path"])
        return url

    def _manifest(self, files_dict, security="normal", **extra):
        entries = []
        for rel, content in files_dict.items():
            entries.append({
                "path": rel,
                "sha256": sha(content),
                "bytes": len(content),
            })
        return {"version": "1.1.0", "notes": "tiny staged release",
                "security": security, "files": entries, **extra}


class StageIntegrity(_Case):
    def test_no_target_refused_report_only_default(self):
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.stage("https://example.invalid/latest.json",
                          "1.0.0", None)
        self.assertEqual(ctx.exception.code, "target_not_configured")

    def test_remote_http_source_refused(self):
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.stage("http://example.com/latest.json", "1.0.0",
                          self.target)
        self.assertEqual(ctx.exception.code, "invalid_source")

    def test_missing_integrity_metadata_refused(self):
        manifest = {"version": "1.1.0", "files": [{"path": "a.txt"}]}
        url = self._wrap(manifest, {})
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.stage(url + "/manifest.json", "1.0.0", self.target)
        self.assertEqual(ctx.exception.code, "integrity_missing")

    def test_traversal_path_refused(self):
        bad = self._manifest({"../escape.txt": b"x"})
        url = self._wrap(bad, {})
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.stage(url + "/manifest.json", "1.0.0", self.target)
        self.assertEqual(ctx.exception.code, "invalid_manifest_path")

    def test_protected_user_data_refused(self):
        bad = self._manifest({"data/jarvis.db": b"hands-off"})
        url = self._wrap(bad, {})
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.stage(url + "/manifest.json", "1.0.0", self.target)
        self.assertEqual(ctx.exception.code, "protected_path")

    def test_sha_mismatch_refused(self):
        files = {"pkg/a.txt": b"tampered bytes"}
        manifest = self._manifest({"pkg/a.txt": b"expected bytes"})
        url = self._wrap(manifest, files)
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.stage(url + "/manifest.json", "1.0.0", self.target)
        self.assertEqual(ctx.exception.code, "integrity_mismatch")

    def test_up_to_date_no_stage(self):
        manifest = self._manifest(
            {"pkg/installed.txt": b"same"})
        # version equals current -> up_to_date, nothing staged
        manifest["version"] = "1.0.0"
        url = self._wrap(manifest, {})
        result = updates.stage(url + "/manifest.json", "1.0.0",
                               self.target)
        self.assertEqual(result["status"], "up_to_date")

    def test_successful_stage_records_manifest_digest(self):
        files = {"pkg/a.txt": b"alpha", "pkg/sub/b.txt": b"beta"}
        manifest = self._manifest(files)
        url = self._wrap(manifest, files)
        staged = updates.stage(url + "/manifest.json", "1.0.0",
                               self.target)
        self.assertEqual(staged["status"], "staged")
        self.assertEqual(len(staged["files"]), 2)
        self.assertTrue(all(not f["existing_differs"]
                            for f in staged["files"]))
        digest = staged["manifest_sha"]
        updates_mod_persist_index_and_stage(digest, staged,
                                            self.target)


def updates_mod_persist_index_and_stage(digest, staged, target):
    updates.persist_stage_index(staged)


class ApplyRollback(_Case):
    def _staged(self, files=None, security="normal"):
        files = files or {"pkg/new.txt": b"fresh",
                          "pkg/replace.txt": b"new content"}
        manifest = self._manifest(files, security=security)
        url = self._wrap(manifest, files)
        staged = updates.stage(url + "/manifest.json", "1.0.0",
                               self.target)
        updates.persist_stage_index(staged)
        return staged

    def _insert_stage_record(self, staged):
        with self.conn:
            updates.record_stage(self.conn, staged, "t0", "1.0.0")

    def _existing_replaced_file(self):
        (self.target / "pkg").mkdir(exist_ok=True)
        (self.target / "pkg" / "replace.txt").write_bytes(b"old content")

    def test_apply_blocked_by_modified_file_without_confirmation(self):
        staged = self._staged()
        self._insert_stage_record(staged)
        self._existing_replaced_file()
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.apply(self.conn, self.target,
                          staged["manifest_sha"], "t1")
        self.assertEqual(ctx.exception.code, "modified_files_present")
        # target file untouched
        self.assertEqual((self.target / "pkg" / "replace.txt")
                         .read_bytes(), b"old content")

    def test_apply_verifies_replaces_backs_up_and_records(self):
        staged = self._staged()
        self._insert_stage_record(staged)
        self._existing_replaced_file()
        result = updates.apply(self.conn, self.target,
                               staged["manifest_sha"], "t1",
                               allow_overwrite=True)
        self.assertEqual(result["status"], "applied")
        self.assertEqual((self.target / "pkg" / "new.txt").read_bytes(),
                         b"fresh")
        self.assertEqual((self.target / "pkg" / "replace.txt")
                         .read_bytes(), b"new content")
        backup_entry = [f for f in result["files"] if f.get("backup")][0]
        backup_file = (Path(result["backup_dir"])
                       / backup_entry["path"])
        self.assertEqual(backup_file.read_bytes(), b"old content")
        row = self.conn.execute(
            "SELECT status, to_version, target_root FROM"
            " update_installs WHERE id = ?",
            (result["install_id"],)).fetchone()
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["to_version"], "1.1.0")
        self.assertEqual(row["target_root"], str(self.target))

    def test_digest_mismatch_refused(self):
        staged = self._staged()
        self._insert_stage_record(staged)
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.apply(self.conn, self.target, "f" * 64, "t1")
        self.assertEqual(ctx.exception.code, "digest_mismatch")

    def test_staging_symlink_swap_refused(self):
        staged = self._staged()
        self._insert_stage_record(staged)
        staging = self.target / ".jarvis-update-staging"
        import shutil
        shutil.rmtree(staging)
        other = self.dir / "sneaky"
        other.mkdir()
        (other / ".manifest.json").write_text(json.dumps(
            {"manifest_sha": staged["manifest_sha"],
             "version": staged["version"], "from_version": "1.0.0",
             "notes": "", "files": staged["files"]}))
        staging.symlink_to(other)
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.apply(self.conn, self.target,
                          staged["manifest_sha"], "t1")
        self.assertEqual(ctx.exception.code, "staging_tampered")

    def test_rollback_restores_and_removes(self):
        staged = self._staged()
        self._insert_stage_record(staged)
        self._existing_replaced_file()
        result = updates.apply(self.conn, self.target,
                               staged["manifest_sha"], "t1",
                               allow_overwrite=True)
        rolled = updates.rollback(self.conn, result["install_id"])
        self.assertEqual(rolled["status"], "rolled_back")
        self.assertEqual((self.target / "pkg" / "replace.txt")
                         .read_bytes(), b"old content")
        self.assertFalse((self.target / "pkg" / "new.txt").exists())
        row = self.conn.execute(
            "SELECT status FROM update_installs WHERE id = ?",
            (result["install_id"],)).fetchone()
        self.assertEqual(row["status"], "rolled_back")

    def test_rollback_with_missing_backup_refused(self):
        staged = self._staged()
        self._insert_stage_record(staged)
        self._existing_replaced_file()
        result = updates.apply(self.conn, self.target,
                               staged["manifest_sha"], "t1",
                               allow_overwrite=True)
        # sabotage the backup to prove the two-phase refusal
        import shutil
        for entry in result["files"]:
            if entry.get("backup"):
                shutil.rmtree(
                    Path(result["backup_dir"]) / "pkg", ignore_errors=True)
        with self.assertRaises(updates.UpdateError) as ctx:
            updates.rollback(self.conn, result["install_id"])
        self.assertIn(ctx.exception.code,
                      ("backup_missing", "backup_corrupt"))
        row = self.conn.execute(
            "SELECT status FROM update_installs WHERE id = ?",
            (result["install_id"],)).fetchone()
        self.assertEqual(row["status"], "applied")


if __name__ == "__main__":
    unittest.main()
