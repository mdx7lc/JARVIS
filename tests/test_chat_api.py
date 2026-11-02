"""Phase 2B chat API tests — JARVIS server + fake upstream, temp DB only."""

import json
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import config
import db
import server
from fake_upstream import FakeUpstream

KEY = "test-key-123"
MODEL = "qwen3.8-27b"


def post(url, payload=None, content_type="application/json", raw=None,
         method="POST"):
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if content_type is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


class ChatAPITestCase(unittest.TestCase):
    history_limit = 20
    # Phase 8A: subclasses may inject extra env (e.g. fallback policy).
    extra_env = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-chat-")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "jarvis.db"
        self.upstream = FakeUpstream()
        self.upstream.start(self)
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": KEY,
            "JARVIS_HISTORY_MESSAGES": str(self.history_limit),
        }
        if self.extra_env:
            env.update(self.extra_env)
        self.cfg = config.load(environ=env)
        self.start_jarvis(self.cfg)

    def start_jarvis(self, cfg):
        import threading
        httpd = server.create_server(
            "127.0.0.1", 0, self.db_path, cfg=cfg,
            settings_dir=str(self._tmp.name)
        )
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self._srv_thread = threading.Thread(
            target=httpd.serve_forever, daemon=True
        )
        self._srv_thread.start()
        self._httpd = httpd
        self.addCleanup(self.stop_jarvis)

    def stop_jarvis(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._srv_thread.join(timeout=5)

    def chat(self, payload=None, **kwargs):
        if payload is None:
            payload = {"message": "Hi there"}
        status, body = post(self.base + "/api/chat", payload, **kwargs)
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        return status, parsed

    def last_upstream_body(self):
        with self.upstream.lock:
            return self.upstream.requests[-1]["body"]

    def db_counts(self):
        conn = sqlite3.connect(self.db_path)
        try:
            users = conn.execute(
                "SELECT count(*) FROM messages WHERE role='user'"
            ).fetchone()[0]
            assistants = conn.execute(
                "SELECT count(*) FROM messages WHERE role='assistant'"
            ).fetchone()[0]
            convs = conn.execute(
                "SELECT count(*) FROM conversations"
            ).fetchone()[0]
        finally:
            conn.close()
        return users, assistants, convs


class SuccessFlowTests(ChatAPITestCase):
    def test_new_conversation_roundtrip(self):
        status, payload = self.chat({"message": "Hello JARVIS"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["reply"], "Hello! How can I help?")
        conv_id = payload["conversation_id"]
        self.assertIsInstance(conv_id, str)
        self.assertEqual(len(conv_id), 32)
        users, assistants, convs = self.db_counts()
        self.assertEqual((users, assistants, convs), (1, 1, 1))

    def test_persistence_survives_fresh_connection(self):
        _status, payload = self.chat()
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? "
            "ORDER BY created_at, id",
            (payload["conversation_id"],),
        ).fetchall()
        conn.close()
        self.assertEqual(
            [tuple(r) for r in rows],
            [("user", "Hi there"), ("assistant", "Hello! How can I help?")],
        )

    def test_existing_conversation_continuation(self):
        _s, first = self.chat({"message": "First"})
        conv_id = first["conversation_id"]
        status, second = self.chat(
            {"message": "Second", "conversation_id": conv_id}
        )
        self.assertEqual(status, 200)
        self.assertEqual(second["conversation_id"], conv_id)
        body = self.last_upstream_body()
        contents = [m["content"] for m in body["messages"]]
        self.assertEqual(
            contents,
            ["First", "Hello! How can I help?", "Second"],
        )
        self.assertEqual(
            [m["role"] for m in body["messages"]],
            ["user", "assistant", "user"],
        )
        users, assistants, convs = self.db_counts()
        self.assertEqual((users, assistants, convs), (2, 2, 1))

    def test_history_isolated_per_conversation(self):
        _s, conv_a = self.chat({"message": "SECRET-CONV-A-MARKER"})
        _s, _p = self.chat({"message": "Hello B"})
        body = self.last_upstream_body()
        flat = json.dumps(body["messages"])
        self.assertNotIn("SECRET-CONV-A-MARKER", flat)

    def test_history_bounded_and_chronological(self):
        conn = db.open_conn(self.db_path)
        try:
            conv = db.create_conversation(conn, "seeded")
            for i in range(1, 13):
                db.add_message(conn, conv, "user", f"u{i}")
                db.add_message(conn, conv, "assistant", f"a{i}")
        finally:
            conn.close()
        status, payload = self.chat(
            {"message": "new", "conversation_id": conv}
        )
        self.assertEqual(status, 200)
        body = self.last_upstream_body()
        self.assertEqual(len(body["messages"]), 20)  # limit from env
        self.assertEqual(body["messages"][-1]["content"], "new")
        self.assertEqual(body["messages"][-1]["role"], "user")
        # chronological: strictly the trailing slice of the seeded history
        self.assertEqual(body["messages"][0]["content"], "a3")
        self.assertEqual(
            [m["content"] for m in body["messages"]][-4:],
            ["a11", "u12", "a12", "new"],
        )


class RejectionTests(ChatAPITestCase):
    def test_invalid_json_rejected(self):
        status, payload = self.chat(raw=b"{nope")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json")

    def test_non_object_json_rejected(self):
        status, payload = self.chat(raw=b"[1,2,3]")
        self.assertEqual(status, 400)

    def test_wrong_content_type_rejected(self):
        status, payload = self.chat(
            {"message": "x"}, content_type="text/plain"
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

    def test_oversized_body_rejected(self):
        status, payload = self.chat({"message": "x" * 40000})
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "payload_too_large")

    def test_missing_empty_whitespace_and_typed_messages(self):
        for bad in (
            {},
            {"message": ""},
            {"message": "   "},
            {"message": 42},
            {"message": None},
            {"message": ["x"]},
        ):
            status, payload = self.chat(bad)
            self.assertEqual(status, 400, f"payload={bad!r}")
            self.assertIn("error", payload)

    def test_oversized_message_rejected(self):
        status, payload = self.chat({"message": "x" * 8001})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "message_too_long")

    def test_unknown_conversation_rejected(self):
        status, payload = self.chat(
            {"message": "hi", "conversation_id": "deadbeef" * 4}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "conversation_not_found")
        self.assertEqual(payload["conversation_id"], "deadbeef" * 4)
        users, _a, _c = self.db_counts()
        self.assertEqual(users, 0)  # nothing persisted

    def test_unknown_fields_rejected(self):
        status, payload = self.chat(
            {"message": "hi", "model": "evil", "messages": [{"role": "user",
                                                             "content": "x"}]}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_conversation_id_wrong_type_rejected(self):
        status, _p = self.chat({"message": "hi", "conversation_id": 7})
        self.assertEqual(status, 400)

    def test_cross_origin_rejected(self):
        req = urllib.request.Request(
            self.base + "/api/chat",
            data=json.dumps({"message": "hi"}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://evil.example",
            },
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("cross-origin request should be rejected")
        except urllib.error.HTTPError as err:
            self.assertEqual(err.code, 403)

    def test_get_chat_is_405(self):
        status, body = get(self.base + "/api/chat")
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body)["error"]["code"],
                         "method_not_allowed")


class ProviderFailureTests(ChatAPITestCase):
    def failure_response(self, status, payload):
        self.upstream.response = (status, payload, "application/json")

    def test_not_configured_is_safe_503(self):
        cfg = config.load(environ={
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
        })
        self.stop_jarvis()
        self.start_jarvis(cfg)
        status, payload = self.chat()
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "not_configured")
        self.assertNotIn("conversation_id", payload)
        _u, _a, convs = self.db_counts()
        self.assertEqual(convs, 0)  # nothing created before the check

    def test_upstream_failure_preserves_user_message(self):
        self.failure_response(500, {"error": "boom"})
        status, payload = self.chat({"message": "save me"})
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "upstream_error")
        conv_id = payload["conversation_id"]
        self.assertTrue(conv_id)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT role FROM messages WHERE conversation_id=?", (conv_id,)
        ).fetchall()
        conn.close()
        self.assertEqual([r[0] for r in rows], ["user"])  # no fake assistant

    def test_timeout_failure_preserves_conversation(self):
        cfg = config.load(environ={
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": KEY,
            "JARVIS_TIMEOUT_SECONDS": "1",
        })
        self.stop_jarvis()
        self.start_jarvis(cfg)
        self.upstream.delay = 2.0
        status, payload = self.chat({"message": "slow"})
        self.assertEqual(status, 504)
        self.assertEqual(payload["error"]["code"], "provider_timeout")
        self.assertTrue(payload["conversation_id"])
        users, assistants, _c = self.db_counts()
        self.assertEqual((users, assistants), (1, 0))

    def test_auth_failure_response_is_sanitized(self):
        self.failure_response(401, {"error": "invalid token detail"})
        status, payload = self.chat()
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "auth_failed")
        self.assertNotIn(KEY, json.dumps(payload))
        self.assertNotIn("invalid token detail", json.dumps(payload))


class PreservedRoutesTests(ChatAPITestCase):
    def test_health_still_works(self):
        status, body = get(self.base + "/api/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["db"], "ready")

    def test_static_frontend_still_served(self):
        status, body = get(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn(b"JARVIS", body)
        status, body = get(self.base + "/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"mockReply", body)  # frontend untouched

    def test_post_to_root_still_405(self):
        status, body = post(self.base + "/", {"x": 1})
        self.assertEqual(status, 405)

    def test_unknown_api_still_404(self):
        status, body = get(self.base + "/api/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
