"""Phase 4 GET /api/metrics endpoint tests — temp servers only.

Real /proc files ARE read here (this is Linux and they exist), but no
fragile hardware values are asserted: only shape, units, availability
semantics, taxonomy and security. Partial-failure paths are forced by
replacing the handler's MetricsService with a stub.
"""

import json
import unittest
import unittest.mock
import urllib.error
import urllib.request

from test_chat_api import ChatAPITestCase
from test_tasks_api import do_task_request


def get_raw(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    return do_task_request(req)


class TestMetricsAPI(ChatAPITestCase):
    def metrics(self):
        status, body = get_raw(self.base + "/api/metrics")
        try:
            return status, json.loads(body)
        except ValueError:
            return status, None

    def test_success_shape_and_units(self):
        status, payload = self.metrics()
        self.assertEqual(status, 200)
        self.assertIn("sampled_at", payload)
        self.assertIn("cpu", payload)
        self.assertIn("memory", payload)
        self.assertIn("disk", payload)
        self.assertIn("host_uptime", payload)
        self.assertIn("units", payload["cpu"])
        # availability flags are booleans per section
        for section in ("cpu", "memory", "disk", "host_uptime"):
            self.assertIn("available", payload[section])
            self.assertIsInstance(payload[section]["available"], bool)
        self.assertNotIn("/proc", json.dumps(payload))
        self.assertNotIn(str(self.db_path), json.dumps(payload))

    def test_sample_timestamp_is_iso_z(self):
        status, payload = self.metrics()
        self.assertEqual(status, 200)
        self.assertTrue(payload["sampled_at"].endswith("Z"))

    def test_content_type_is_json(self):
        resp = urllib.request.urlopen(self.base + "/api/metrics",
                                      timeout=10)
        try:
            ctype = resp.headers.get("Content-Type", "")
        finally:
            resp.close()
        self.assertTrue(ctype.startswith("application/json"), ctype)

    def test_post_is_405_tax(self):
        req = urllib.request.Request(
            self.base + "/api/metrics", data=b"{}", method="POST")
        status, body = do_task_request(req)
        self.assertEqual(status, 405)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

    def test_put_and_options_rejected(self):
        for method in ("PUT", "OPTIONS"):
            status, _ = get_raw(self.base + "/api/metrics", method=method)
            self.assertEqual(status, 405)

    def test_head_served(self):
        status, body = get_raw(self.base + "/api/metrics", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_cross_origin_rejected(self):
        req = urllib.request.Request(self.base + "/api/metrics")
        req.add_header("Origin", "https://evil.example")
        status, body = do_task_request(req)
        self.assertEqual(status, 403)

    def test_no_secrets_in_response(self):
        status, payload = self.metrics()
        body = json.dumps(payload)
        self.assertNotIn("test-key-123", body)
        self.assertNotIn("api_key", body)
        self.assertNotIn("FREELLMAPI", body)
        self.assertNotIn(self.upstream.base_url, body)

    def test_partial_metric_failure_keeps_other_metrics(self):
        # force a real disk failure end-to-end; memory and uptime survive
        with unittest.mock.patch(
                "metrics.os.statvfs",
                side_effect=OSError(13, "permission denied")):
            status, payload = self.metrics()
        self.assertEqual(status, 200)
        self.assertFalse(payload["disk"]["available"])
        self.assertTrue(payload["memory"]["available"])
        self.assertTrue(payload["host_uptime"]["available"])

    def test_handler_error_is_internal_error_not_crash(self):
        handler_cls = self._httpd.RequestHandlerClass
        original = getattr(handler_cls, "metrics", None)

        class Boom:
            def collect(self):
                raise RuntimeError("boom")

        handler_cls.metrics = Boom()
        self.addCleanup(setattr, handler_cls, "metrics", original)
        status, body = get_raw(self.base + "/api/metrics")
        # 500 fallback intentionally sends no body (no stack details)
        self.assertEqual(status, 500)
        self.assertEqual(body, b"")

    def test_no_provider_calls(self):
        before = len(self.upstream.requests)
        get_raw(self.base + "/api/metrics")
        self.assertEqual(len(self.upstream.requests), before)


if __name__ == "__main__":
    unittest.main()
