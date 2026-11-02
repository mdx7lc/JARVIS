"""Phase 6A logging security tests — real HTTP traffic with canary
secrets; verifies the structured stderr stream never carries private
data, routes never include query parameters, request ids are bounded,
server-generated and well formed, and unexpected failures keep
internals out of both client responses and logs."""

import contextlib
import io
import json
import re
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path

import config
import db
import server
import applog
from fake_upstream import FakeUpstream

CANARY_API_KEY = "sk-canary-cleanroom-key"
CANARY_PROMPT = "unreleased product codename secret prompt"
CANARY_SEARCH = "ndasecretsearchneedle"
CANARY_TASK = "secret-task-title-needle"
CANARY_HEADER = "Bearer bearer-canary-needle"


def post(url, payload, headers=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


class LoggingSecurityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-log-")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "db.sqlite"
        self.upstream = FakeUpstream()
        self.upstream.start(self)
        cfg = config.load(
            environ={
                "JARVIS_DB_PATH": str(self.db_path),
                "FREELLMAPI_URL": self.upstream.base_url,
                "FREELLMAPI_API_KEY": CANARY_API_KEY,
            }
        )
        httpd = server.create_server(
            "127.0.0.1", 0, self.db_path, cfg=cfg,
            settings_dir=str(self._tmp.name))
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.limiter = httpd.rate_limiter
        self._thread = threading.Thread(
            target=httpd.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self._thread.join, 5)
        self.addCleanup(httpd.server_close)

    def exercise_and_capture(self):
        buf = io.StringIO()
        rid_header = {"chat": None, "rate": None, "error": None}
        # Interpreter hygiene warnings (e.g. ResourceWarning from GC'd
        # test sockets under memory pressure) are not application log
        # output; keep them out of the captured stream so the
        # structured-JSON assertion evaluates actual app logging.
        warnings.filterwarnings("ignore", category=ResourceWarning)
        self.addCleanup(warnings.resetwarnings)
        with contextlib.redirect_stderr(buf):
            status, headers, body = post(
                self.base + "/api/chat", {"message": CANARY_PROMPT},
                {"Authorization": CANARY_HEADER})
            self.assertEqual(status, 200, body)
            rid_header["chat"] = headers.get("X-Request-ID")
            # make the fake upstream fail to exercise provider errors
            self.upstream.response = (502, b"{}",
                                      "application/json")
            status, headers, body = post(
                self.base + "/api/chat", {"message": "second prompt"})
            self.assertEqual(status, 502)
            self.upstream.response = (200, b'{"id":"1","choices":[{"message":{"content":"ok"}}],"model":"m"}',
                                      "application/json")
            status, headers, body = get(
                self.base + "/api/search?q=" + CANARY_SEARCH)
            self.assertEqual(status, 200)
            status, headers, body = post(
                self.base + "/api/tasks", {"title": CANARY_TASK})
            self.assertEqual(status, 201)
            task_id = json.loads(body)["id"]
            req = urllib.request.Request(
                self.base + f"/api/tasks/{task_id}",
                data=json.dumps({"status": "done"}).encode(),
                method="PATCH",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
            # a client-supplied request id is never trusted
            status, headers, _ = get(
                self.base + "/api/status", {"X-Request-ID": "req_forged_by_client"})
            self.assertEqual(status, 200)
            self.assertNotEqual(headers.get("X-Request-ID"),
                                "req_forged_by_client")
            # 429 keep-alive of id chemistry
            self.limiter.add_policy("search", 0, 60.0)
            status, headers, _ = get(
                self.base + "/api/search?q=rate-is-blocked")
            self.assertEqual(status, 429)
            rid_header["rate"] = headers.get("X-Request-ID")
            # unexpected internal failure with canaries inside
            class CanaryDbFailure(RuntimeError):
                pass

            def boom(*a, **k):
                raise CanaryDbFailure(
                    "canary-internal-db-text rejected query details")

            original = server.db.open_conn
            server.db.open_conn = boom
            try:
                status, headers, body = get(self.base + "/api/tasks?limit=5")
                self.assertEqual(status, 500)
                rid_header["error"] = headers.get("X-Request-ID")
            finally:
                server.db.open_conn = original
        return buf.getvalue(), rid_header

    def test_stream_is_structured_json_and_canary_free(self):
        out, rid = self.exercise_and_capture()
        for token in (CANARY_API_KEY, CANARY_PROMPT, CANARY_SEARCH,
                      CANARY_TASK, CANARY_HEADER,
                      "canary-internal-db-text", "sk-canary"):
            self.assertNotIn(token, out, f"{token!r} leaked into logs")
        events = [json.loads(line) for line in out.strip().splitlines()]
        names = {e["event"] for e in events}
        for expected in ("http_request", "provider_error", "rate_limited",
                         "handler_error"):
            self.assertIn(expected, names, f"missing {expected}")
        http_events = [e for e in events if e["event"] == "http_request"]
        self.assertGreaterEqual(len(http_events), 6)
        for e in http_events:
            self.assertRegex(e["request_id"], r"^req_[0-9a-f]{24}$")
            self.assertIn(e["method"], ("GET", "POST", "PATCH", "HEAD"))
            self.assertNotIn("?", e["route"])
            self.assertIn("status", e)
            self.assertIn("duration_ms", e)
        # the rate-limited event carries the policy, not the query
        rate_event = next(
            e for e in events if e["event"] == "rate_limited")
        self.assertEqual(rate_event["policy"], "search")
        self.assertNotIn("rate-is-blocked", out)
        # handler errors expose a stable category + class name only
        error_events = [
            e for e in events if e["event"] == "handler_error"]
        self.assertTrue(error_events)
        self.assertIn("error_class", error_events[-1])
        self.assertNotIn("canary-internal-db-text", json.dumps(error_events))
        # the observed events are distinct ids, none forged
        ids = {e["request_id"] for e in http_events}
        self.assertFalse("req_forged_by_client" in ids)
        self.assertGreater(len(ids), 1)

    def test_request_id_header_correlates_with_log_events(self):
        out, rid = self.exercise_and_capture()
        events = [json.loads(line) for line in out.strip().splitlines()
                  if '"http_request"' in line]
        chat_events = [
            e for e in events
            if e["route"] == "/api/chat" and e["method"] == "POST"]
        self.assertTrue(chat_events)
        self.assertTrue(rid["chat"])
        self.assertEqual(chat_events[0]["request_id"], rid["chat"])
        rid_429 = rid["rate"]
        self.assertTrue(re.fullmatch(r"req_[0-9a-f]{24}", rid_429 or ""))
        self.assertNotEqual(rid_429, rid["chat"])

    def test_log_fields_reject_newline_injection(self):
        forged = "bad\nforged-evil-event\n\"ts\": \"1999\""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            applog.event("test_event", note=forged)
        data = buf.getvalue()
        self.assertEqual(len(data.strip().splitlines()), 1,
                         "a single untrusted string must not split the log")
        rec = json.loads(data)
        self.assertNotIn("\n", json.dumps(rec))

    def test_common_event_schema_has_safety_bounds(self):
        long_value = "x" * 500
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            applog.event("bounded", note=long_value)
        rec = json.loads(buf.getvalue())
        self.assertEqual(len(rec["note"]), 200)


if __name__ == "__main__":
    unittest.main()
