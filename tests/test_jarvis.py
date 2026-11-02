"""Phase 1 acceptance tests for JARVIS (stdlib unittest only).

All tests use temporary directories outside the repository so no runtime
data is ever created in the project.
"""

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import config
import db
import server


def get(url_path, base):
    url = base + url_path
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


class DBTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-dbtest-")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "jarvis.db"

    def test_init_creates_versioned_schema(self):
        self.assertEqual(db.init_db(self.db_path), db.SCHEMA_VERSION)
        conn = sqlite3.connect(self.db_path)
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()
        self.assertEqual(int(version), db.SCHEMA_VERSION)
        self.assertEqual(user_version, db.SCHEMA_VERSION)
        self.assertTrue({"meta", "conversations", "messages"} <= tables)

    def test_init_is_idempotent(self):
        db.init_db(self.db_path)
        db.init_db(self.db_path)  # must not raise or duplicate schema
        self.assertTrue(db.check_ready(self.db_path)[0])

    def test_newer_schema_is_not_silently_downgraded(self):
        db.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        # beyond the supported schema (v3 is current); init must refuse
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with self.assertRaises(db.SchemaError):
            db.init_db(self.db_path)

    def test_persistence_of_conversations_and_messages(self):
        db.init_db(self.db_path)
        conn = db.open_conn(self.db_path)
        conv = db.create_conversation(conn, "First")
        db.add_message(conn, conv, "user", "hello")
        db.add_message(conn, conv, "assistant", "hi")
        msgs = db.get_messages(conn, conv)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "hello")
        conn.close()

        # reopen from scratch: data must persist
        conn = db.open_conn(self.db_path)
        self.assertEqual(len(db.get_messages(conn, conv)), 2)
        conn.close()

    def test_invalid_role_rejected(self):
        db.init_db(self.db_path)
        conn = db.open_conn(self.db_path)
        conv = db.create_conversation(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            db.add_message(conn, conv, "alien", "should fail")
        conn.close()

    def test_cascade_delete_removes_messages(self):
        db.init_db(self.db_path)
        conn = db.open_conn(self.db_path)
        conv = db.create_conversation(conn)
        db.add_message(conn, conv, "user", "hello")
        conn.execute("DELETE FROM conversations WHERE id=?", (conv,))
        conn.commit()
        self.assertEqual(len(db.get_messages(conn, conv)), 0)
        count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_check_ready_missing_db_creates_nothing(self):
        self.assertFalse(self.db_path.exists())
        ok, detail = db.check_ready(self.db_path)
        self.assertFalse(ok)
        self.assertEqual(detail, "missing")
        self.assertFalse(self.db_path.exists())  # still no file created

    def test_check_ready_corrupt_db(self):
        self.db_path.write_bytes(b"this is not a sqlite database" * 4)
        ok, detail = db.check_ready(self.db_path)
        self.assertFalse(ok)
        self.assertNotEqual(detail, "ready")


class ConfigTestCase(unittest.TestCase):
    def test_defaults(self):
        cfg = config.load(environ={})
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 8080)
        self.assertEqual(cfg.db_path, config.PROJECT_ROOT / "data" / "jarvis.db")

    def test_invalid_port_rejected(self):
        for bad in ("not-a-port", "0", "70000", "1.5"):
            with self.assertRaises(config.ConfigError):
                config.load(environ={"JARVIS_PORT": bad})

    def test_remote_bind_rejected(self):
        for bad in ("0.0.0.0", "192.168.1.10", "example.com"):
            with self.assertRaises(config.ConfigError):
                config.load(environ={"JARVIS_HOST": bad})

    def test_valid_overrides(self):
        with tempfile.TemporaryDirectory(prefix="jarvis-cfg-") as tmp:
            cfg = config.load(
                environ={
                    "JARVIS_HOST": "localhost",
                    "JARVIS_PORT": "9443",
                    "JARVIS_DB_PATH": str(Path(tmp) / "x.db"),
                }
            )
        self.assertEqual(cfg.host, "localhost")
        self.assertEqual(cfg.port, 9443)


class ServerTestCase(unittest.TestCase):
    """Exercises the real server on an ephemeral port with a temp DB."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-srvtest-")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "jarvis.db"

    def start_server(self, db_path, init=True):
        httpd = server.create_server("127.0.0.1", 0, db_path, init_db=init)
        port = httpd.server_address[1]
        self.base = f"http://127.0.0.1:{port}"
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.stop_server, httpd, thread)
        return port

    def stop_server(self, httpd, thread):
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_static_frontend_from_real_files(self):
        self.start_server(self.db_path)
        status, headers, body = get("/", self.base)
        self.assertEqual(status, 200)
        self.assertIn(b"JARVIS", body)
        self.assertIn(b'<script src="app.js">', body)
        ctype = headers.get("Content-Type", "")
        self.assertIn("text/html", ctype)

        status, html_headers, body = get("/index.html", self.base)
        self.assertEqual(status, 200)

        status, headers, body = get("/app.js", self.base)
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertIn(b"mockReply", body)  # the real, unmodified frontend code

        status, headers, body = get("/styles.css", self.base)
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("Content-Type", ""))

    def test_root_asset_webp_is_served(self):
        self.start_server(self.db_path)
        status, headers, body = get(
            "/assets/jarvis-command-center-real.webp", self.base
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/webp")
        self.assertGreater(len(body), 10000)

    def test_health_ready(self):
        self.start_server(self.db_path)
        status, headers, body = get("/api/health", self.base)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["db"], "ready")
        self.assertEqual(payload["schema_version"], db.SCHEMA_VERSION)
        self.assertGreaterEqual(payload["uptime_seconds"], 0)

    def test_health_missing_db_is_accurate_and_creates_nothing(self):
        missing = Path(self._tmp.name) / "no-such-dir" / "jarvis.db"
        self.start_server(missing, init=False)
        status, headers, body = get("/api/health", self.base)
        self.assertEqual(status, 200)  # process is alive...
        payload = json.loads(body)
        self.assertFalse(payload["ok"])  # ...but DB readiness is false
        self.assertEqual(payload["db"], "missing")
        self.assertIsNone(payload["schema_version"])
        self.assertFalse(missing.parent.exists(), "health must not create dirs")
        self.assertFalse(missing.exists(), "health must not create the database")

    def test_unknown_api_returns_404(self):
        self.start_server(self.db_path)
        status, headers, body = get("/api/nope", self.base)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})
        self.assertIn("application/json", headers.get("Content-Type", ""))

    def test_unknown_static_returns_404(self):
        self.start_server(self.db_path)
        status, _h, body = get("/no-such-file.html", self.base)
        self.assertEqual(status, 404)

    def test_unsupported_methods_rejected(self):
        self.start_server(self.db_path)
        for method in ("POST", "PUT", "DELETE"):
            req = urllib.request.Request(
                self.base + "/", data=b"x", method=method
            )
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail(f"{method} should be rejected")
            except urllib.error.HTTPError as err:
                self.assertEqual(err.code, 405)
                self.assertEqual(err.headers.get("Allow"), "GET, HEAD")

    def test_path_traversal_rejected(self):
        self.start_server(self.db_path)
        encoded = "/%2e%2e/"  # decoded to "/../" by the handler
        status, _h, body = get(encoded + "server.py", self.base)
        self.assertEqual(status, 404)
        self.assertNotIn(b"import sys", body)
        status, _h, body = get("/..%2fserver.py", self.base)
        self.assertEqual(status, 404)
        self.assertNotIn(b"import sys", body)

    def test_no_stack_trace_or_fs_details_in_errors(self):
        self.start_server(self.db_path)
        for url in ("/%2e%2e/db.py", "/api/none", "/missing.css"):
            status, _h, body = get(url, self.base)
            self.assertNotIn(b"/home/", body)
            self.assertNotIn(b"Traceback", body)


if __name__ == "__main__":
    unittest.main()
