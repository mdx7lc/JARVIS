"""Phase 3C status API tests — shape, secret hygiene, degraded states."""

import json
import unittest
import urllib.error
import urllib.request

import config
import db
from test_chat_api import ChatAPITestCase, KEY, MODEL


class StatusAPITestCase(ChatAPITestCase):
    def status(self, origin=None, method=None, raw=None):
        req = urllib.request.Request(
            self.base + "/api/status", data=raw, method=method or "GET"
        )
        if origin is not None:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def status_json(self, **kwargs):
        code, body = self.status(**kwargs)
        return code, json.loads(body)


class TestStatusContract(StatusAPITestCase):
    def test_shape_and_values(self):
        code, payload = self.status_json()
        self.assertEqual(code, 200)
        self.assertEqual(
            set(payload),
            {
                "db",
                "schema_version",
                "uptime_seconds",
                "model_policy",
                "provider_configured",
                # Phase 7A extension: effective policy + source, catalog
                # freshness/failure state, last completed inference
                "effective_model",
                "model_source",
                "catalog",
                "last_inference",
            },
        )
        self.assertEqual(payload["db"], "ready")
        self.assertEqual(payload["schema_version"], db.SCHEMA_VERSION)
        self.assertGreaterEqual(payload["uptime_seconds"], 0)
        self.assertEqual(payload["model_policy"], MODEL)
        self.assertEqual(payload["effective_model"], MODEL)
        self.assertEqual(payload["model_source"], "environment_default")
        self.assertIs(payload["provider_configured"], True)
        self.assertIsNone(payload["catalog"]["count"])
        self.assertIsNone(payload["last_inference"])

    def test_model_policy_reflects_env(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._srv_thread.join(timeout=5)
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "JARVIS_MODEL": "auto",
        }
        self.cfg = config.load(environ=env)
        self.start_jarvis(self.cfg)
        _, payload = self.status_json()
        self.assertEqual(payload["model_policy"], "auto")

    def test_unconfigured_provider(self):
        self.stop_jarvis()
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
        }
        self.cfg = config.load(environ=env)
        self.start_jarvis(self.cfg)
        code, payload = self.status_json()
        self.assertEqual(code, 200)
        self.assertIs(payload["provider_configured"], False)

    def test_missing_db_reports_degraded(self):
        self.stop_jarvis()
        env = {"JARVIS_DB_PATH": str(self.db_path) + ".nonexistent"}
        self.cfg = config.load(environ=env)
        import threading
        import server as srv
        httpd = srv.create_server(
            "127.0.0.1", 0, self.cfg.db_path, init_db=False, cfg=self.cfg,
            settings_dir=str(self._tmp.name)
        )
        ok_flag = db.check_ready(self.cfg.db_path)
        self.assertFalse(ok_flag[0])
        self.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self._srv_thread = threading.Thread(
            target=httpd.serve_forever, daemon=True
        )
        self._srv_thread.start()
        self._httpd = httpd
        self.addCleanup(self.stop_jarvis)
        code, payload = self.status_json()
        self.assertEqual(code, 200)
        self.assertEqual(payload["db"], "missing")
        self.assertIsNone(payload["schema_version"])


class TestStatusSafety(StatusAPITestCase):
    def test_no_credentials_in_response(self):
        code, body = self.status()
        self.assertEqual(code, 200)
        text = body.decode("utf-8")
        self.assertNotIn(KEY, text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("api_key", text)

    def test_no_upstream_requests_triggered(self):
        before = len(self.upstream.requests)
        self.status()
        self.assertEqual(len(self.upstream.requests), before)

    def test_cross_origin_rejected(self):
        code, body = self.status(origin="http://evil.test")
        self.assertEqual(code, 403)
        self.assertEqual(json.loads(body)["error"]["code"],
                         "cross_origin_forbidden")

    def test_post_method_rejected(self):
        code, body = self.status(method="POST", raw=b"{")
        self.assertEqual(code, 405)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["code"], "method_not_allowed")
        self.assertIn("/api/status", payload["error"]["message"])

    def test_head_served_without_body(self):
        req = urllib.request.Request(self.base + "/api/status",
                                     method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_queries_ignored(self):
        req = urllib.request.Request(
            self.base + "/api/status?key=should-be-harmless"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            payload = json.loads(resp.read())
        self.assertEqual(payload["db"], "ready")
        self.assertNotIn("key", payload)


if __name__ == "__main__":
    unittest.main()

