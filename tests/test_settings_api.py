"""Phase 11A settings center API tests — FakeUpstream + temp store.

Covers the settings view (secret-free), PATCH validation + env-lock,
credential write-only handling (never echoed everywhere: responses,
operational logs, and the synthetic log stream), test-connection vs
model-test semantics, the cost-confirmation gate, OrcaRouter apply
flows against a monkeypatched fake orca catalog, and persistence across
a server restart. No live inference in CI drift: inference goes to the
FakeUpstream only.
"""

import io
import json
import sys as _sys
import tempfile
import unittest
from unittest import mock

import config
import provider
import settingsstore
from test_chat_api import ChatAPITestCase, KEY
from test_models_api import request, as_json, upstream_hits

ORCA_KEY = "orca-test-key-7"


class SettingsAPITestCase(ChatAPITestCase):
    def get_json(self, path, **kwargs):
        status, _h, body = request(self.base + path,
                                   method=kwargs.pop("method", "GET"),
                                   **kwargs)
        return status, as_json(body)

    def send_json(self, path, payload, method):
        status, _h, body = request(self.base + path, payload=payload,
                                   method=method)
        return status, body if body is None else as_json(body)

    def patch(self, values, **kwargs):
        return self.send_json("/api/settings", values, "PATCH")

    def view(self):
        return self.get_json("/api/settings")

    def settings_dir(self):
        return str(self._httpd.settings_service.store.dir)

    def enable_orca(self):
        """All orca URLs (models catalog included) point at the fake
        upstream with provider-prefixed ids."""
        patches = [
            mock.patch.object(provider, "ORCAROUTER_PRICING_URL",
                              self.upstream.base_url + "/api/pricing"),
            mock.patch.object(provider, "ORCAROUTER_FREE_URL",
                              self.upstream.base_url
                              + "/api/free-package/public"),
            mock.patch.dict(provider.PROVIDER_PROFILES["orcarouter"],
                            {"base_url": self.upstream.base_url}),
            mock.patch.object(
                self.upstream, "models_response",
                (200, {"object": "list", "data": [
                    {"id": "orcarouter/auto", "owned_by": "orcarouter"},
                    {"id": "z-ai/glm-4.6", "owned_by": "z-ai",
                     "context_length": 200000},
                    {"id": "deepseek/deepseek-chat-v3.1-free"},
                ]}, "application/json")),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def orca_env(self):
        return {"JARVIS_DB_PATH": str(self.db_path),
                "FREELLMAPI_URL": self.upstream.base_url,
                "FREELLMAPI_API_KEY": KEY,
                "JARVIS_ORCAROUTER_API_KEY": ORCA_KEY}


class SecretFreeViewTests(SettingsAPITestCase):
    def test_view_shape_and_no_secret_material(self):
        secret = KEY
        status, payload = self.view()
        self.assertEqual(status, 200)
        blob = json.dumps(payload)
        self.assertNotIn(secret, blob)
        for profile in payload["provider"]["profiles"]:
            self.assertIn("id", profile)
            self.assertIn("key_configured", profile)
            self.assertNotIn("api_key", profile)
            self.assertNotIn("key_value", profile)
        self.assertIn("runtime", payload)

    def test_credential_save_is_write_only_and_masked(self):
        canary = "sk-jarvis-credential-canary"
        status, resp = self.send_json(
            "/api/settings/credentials",
            {"provider": "orcarouter", "api_key": canary}, "POST")
        self.assertEqual(status, 200)
        self.assertEqual(resp.get("key_source"), "user_store")
        # the view must describe, never echo
        status, payload = self.view()
        self.assertNotIn(canary, json.dumps(payload))
        orca = [p for p in payload["provider"]["profiles"]
                if p["id"] == "orcarouter"][0]
        self.assertTrue(orca["key_configured"])
        self.assertEqual(orca["key_source"], "user_store")
        # delete then confirm the stored file no longer holds it
        status, _ = self.send_json(
            "/api/settings/credentials?provider=orcarouter", None, "DELETE")
        self.assertEqual(status, 200)
        status, payload = self.view()
        orca = [p for p in payload["provider"]["profiles"]
                if p["id"] == "orcarouter"][0]
        self.assertFalse(orca["key_configured"])

    def test_credential_save_refused_when_env_managed(self):
        env = dict(self.extra_env or {})
        env["JARVIS_ORCAROUTER_API_KEY"] = ORCA_KEY
        self.cfg = config.load(
            environ={**env, "JARVIS_DB_PATH": str(self.db_path),
                     "FREELLMAPI_URL": self.upstream.base_url,
                     "FREELLMAPI_API_KEY": KEY,
                     "JARVIS_HISTORY_MESSAGES": "20"})
        self.stop_jarvis()
        self.start_jarvis(self.cfg)
        status, resp = self.send_json(
            "/api/settings/credentials",
            {"provider": "orcarouter", "api_key": "other-key"}, "POST")
        self.assertEqual(status, 409)
        self.assertEqual(resp["error"]["code"], "field_locked")
        status, payload = self.view()
        orca = [p for p in payload["provider"]["profiles"]
                if p["id"] == "orcarouter"][0]
        self.assertTrue(orca["key_locked"])

    def test_credential_never_reaches_operational_logs(self):
        # the probe targets the ORCA URL; point it at the fake upstream
        self.enable_orca()
        canary = "sk-log-canary-999"
        log_capture = io.StringIO()
        with mock.patch(_sys.__name__ + ".stderr", log_capture):
            self.send_json("/api/settings/credentials",
                           {"provider": "orcarouter", "api_key": canary},
                           "POST")
            self.view()
            self.send_json("/api/settings/test-connection",
                           {"provider": "orcarouter"}, "POST")
        self.assertNotIn(canary, log_capture.getvalue())


class PatchValidationTests(SettingsAPITestCase):
    def test_valid_patch_reflected_in_runtime(self):
        status, resp = self.patch({"timeout_seconds": 45,
                                   "max_tokens": 256,
                                   "reviewer": "opencode"})
        self.assertEqual(status, 200)
        self.assertEqual(resp["runtime"]["timeout_seconds"], 45)
        self.assertEqual(resp["runtime"]["max_tokens"], 256)
        _status, payload = self.view()
        self.assertEqual(payload["runtime"]["timeout_seconds"], 45)
        self.assertEqual(payload["reviewer"]["value"], "opencode")

    def test_unknown_field_rejected(self):
        status, resp = self.patch({"workspace_root": "/somewhere"})
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"]["code"], "unsupported_field")

    def test_out_of_bounds_rejected(self):
        status, resp = self.patch({"timeout_seconds": 99999})
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"]["code"], "invalid_value")

    def test_env_lock_blocks_patch_with_field_locked(self):
        env = {"JARVIS_DB_PATH": str(self.db_path),
               "FREELLMAPI_URL": self.upstream.base_url,
               "FREELLMAPI_API_KEY": KEY,
               "JARVIS_TIMEOUT_SECONDS": "90"}
        self.stop_jarvis()
        self.start_jarvis(config.load(environ=env))
        status, resp = self.patch({"timeout_seconds": 45})
        self.assertEqual(status, 409)
        self.assertEqual(resp["error"]["code"], "field_locked")
        _status, payload = self.view()
        self.assertTrue(payload["runtime"]["timeout_locked"])
        self.assertEqual(payload["runtime"]["timeout_seconds"], 90)


class ApplyFlowTests(SettingsAPITestCase):
    def test_apply_switches_provider_and_chat_uses_it(self):
        self.enable_orca()
        self.stop_jarvis()
        self.start_jarvis(config.load(environ=self.orca_env()))
        status, resp = self.send_json(
            "/api/settings/apply",
            {"provider": "orcarouter", "model": "orcarouter/auto",
             "confirm_cost": True}, "POST")
        self.assertEqual(status, 200)
        self.assertEqual(resp["provider"]["active"], "orcarouter")
        # one chat: payload must carry the orca base + prefixed model id
        status, body = self.send_json("/api/chat", {"message": "hi"}, "POST")
        self.assertEqual(status, 200)
        chat_hits = upstream_hits(self.upstream, "/v1/chat/completions")
        self.assertEqual(chat_hits, 1)
        with self.upstream.lock:
            req = [r for r in self.upstream.requests
                   if r["path"].startswith("/v1/chat/completions")][0]
        self.assertEqual(req["body"]["model"], "orcarouter/auto")

    def test_apply_paid_model_requires_cost_confirmation(self):
        self.enable_orca()
        self.stop_jarvis()
        self.start_jarvis(config.load(environ=self.orca_env()))
        status, resp = self.send_json(
            "/api/settings/apply",
            {"provider": "orcarouter", "model": "orcarouter/auto"},
            "POST")
        self.assertEqual(status, 409)
        self.assertEqual(resp["error"]["code"], "cost_confirmation_required")
        self.assertIn("pricing_class", resp["error"])
        # nothing changed
        _status, payload = self.view()
        self.assertNotEqual(payload["provider"]["active"], "orcarouter")

    def test_apply_failure_preserves_working_setup(self):
        self.enable_orca()
        self.stop_jarvis()
        self.start_jarvis(config.load(environ=self.orca_env()))
        status, resp = self.send_json(
            "/api/settings/apply",
            {"provider": "orcarouter",
             "model": "does-not-exist-anywhere",
             "confirm_cost": True}, "POST")
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"]["code"], "model_not_available")
        _status, payload = self.view()
        self.assertNotEqual(payload["provider"]["active"], "orcarouter")

    def test_apply_free_fallback_refused_on_orca(self):
        self.enable_orca()
        self.stop_jarvis()
        self.start_jarvis(config.load(environ=self.orca_env()))
        status, resp = self.send_json(
            "/api/settings/apply",
            {"provider": "orcarouter", "model": "orcarouter/auto",
             "fallback_model": "deepseek/deepseek-chat-v3.1-free",
             "confirm_cost": True}, "POST")
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"]["code"], "fallback_not_supported")

    def test_apply_clears_stale_runtime_override(self):
        status, _h, _b = request(self.base + "/api/settings/model",
                                 payload={"model": "auto"},
                                 method="PATCH")
        self.assertEqual(status, 200)
        status, resp = self.send_json(
            "/api/settings/apply", {"model": "auto"}, "POST")
        self.assertEqual(status, 200)
        self.assertEqual(resp["model"]["source"], "user_config")

    def test_env_locked_model_provider_still_applies(self):
        env = {"JARVIS_DB_PATH": str(self.db_path),
               "FREELLMAPI_URL": self.upstream.base_url,
               "FREELLMAPI_API_KEY": KEY,
               "JARVIS_MODEL": "auto"}
        self.stop_jarvis()
        self.start_jarvis(config.load(environ=env))
        _status, payload = self.view()
        self.assertEqual(payload["model"]["source"], "environment_default")
        self.assertTrue(payload["model"]["model_locked"])
        # an apply of a different model cannot override the env policy
        status, resp = self.send_json(
            "/api/settings/apply", {"model": "auto"}, "POST")
        self.assertEqual(status, 409)
        self.assertEqual(resp["error"]["code"], "field_locked")
        _status, payload = self.view()
        self.assertEqual(payload["model"]["policy"], "auto")


class ProbeTests(SettingsAPITestCase):
    def test_connection_probe_never_runs_inference(self):
        before = upstream_hits(self.upstream, "/v1/chat/completions")
        status, resp = self.send_json(
            "/api/settings/test-connection",
            {"provider": "freellmapi"}, "POST")
        self.assertEqual(status, 200)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["catalog"]["count"], 3)
        after = upstream_hits(self.upstream, "/v1/chat/completions")
        self.assertEqual(before, after,
                         "test-connection must not send a chat request")

    def test_connection_probe_tests_provided_key_not_saved(self):
        self.enable_orca()
        probe_key = "probe-key-only-123"
        status, resp = self.send_json(
            "/api/settings/test-connection",
            {"provider": "orcarouter", "api_key": probe_key}, "POST")
        self.assertEqual(status, 200)
        self.assertEqual(resp["key_source"], "provided")
        with self.upstream.lock:
            auths = [r["headers"].get("Authorization", "")
                     for r in self.upstream.requests
                     if r["path"] == "/v1/models"]
        self.assertTrue(any(probe_key in a for a in auths))
        _status, payload = self.view()
        orca = [p for p in payload["provider"]["profiles"]
                if p["id"] == "orcarouter"][0]
        self.assertFalse(orca["key_configured"])

    def test_model_test_is_single_bounded_inference(self):
        before = upstream_hits(self.upstream, "/v1/chat/completions")
        status, resp = self.send_json(
            "/api/settings/model-test", {"model": "auto"}, "POST")
        self.assertEqual(status, 200)
        self.assertEqual(resp["ok"], True)
        self.assertEqual(resp["reported_model"], "qwen3.8-27b")
        after = upstream_hits(self.upstream, "/v1/chat/completions")
        self.assertEqual(after - before, 1)
        with self.upstream.lock:
            req = [r for r in self.upstream.requests
                   if r["path"].startswith("/v1/chat/completions")][0]
        self.assertLessEqual(req["body"]["max_tokens"], 16)

    def test_model_test_paid_orca_requires_confirmation(self):
        self.enable_orca()
        self.stop_jarvis()
        self.start_jarvis(config.load(environ={
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": KEY,
            "JARVIS_ORCAROUTER_API_KEY": ORCA_KEY}))
        # switch active provider first so the probe resolves orca
        status, _ = self.send_json(
            "/api/settings/apply",
            {"provider": "orcarouter", "model": "orcarouter/auto",
             "confirm_cost": True}, "POST")
        self.assertEqual(status, 200)
        before = upstream_hits(self.upstream, "/v1/chat/completions")
        status, resp = self.send_json(
            "/api/settings/model-test",
            {"model": "orcarouter/auto"}, "POST")
        self.assertEqual(status, 409)
        self.assertEqual(resp["error"]["code"], "cost_confirmation_required")
        self.assertEqual(
            upstream_hits(self.upstream, "/v1/chat/completions"), before,
            "a refused model-test must not have billed anything")
        status, resp = self.send_json(
            "/api/settings/model-test",
            {"model": "orcarouter/auto", "confirm_cost": True}, "POST")
        self.assertEqual(status, 200)
        self.assertEqual(resp["ok"], True)


class PersistenceTests(SettingsAPITestCase):
    def test_user_store_survives_restart_and_env_wins(self):
        status, resp = self.patch({"timeout_seconds": 42,
                                   "max_tokens": 128})
        self.assertEqual(status, 200)
        env = {"JARVIS_DB_PATH": str(self.db_path),
               "FREELLMAPI_URL": self.upstream.base_url,
               "FREELLMAPI_API_KEY": KEY,
               "JARVIS_MAX_TOKENS": "777"}
        self.stop_jarvis()
        # fresh config object built from the process env dict — the
        # persisted store rides on the settings dir
        self.start_jarvis(config.load(environ=env))
        _status, payload = self.view()
        self.assertEqual(payload["runtime"]["timeout_seconds"], 42)
        # env still wins on a managed field
        self.assertEqual(payload["runtime"]["max_tokens"], 777)
        self.assertTrue(payload["runtime"]["max_tokens_locked"])

    def test_corrupt_store_file_is_tolerated(self):
        self.patch({"timeout_seconds": 42})
        store_dir = self.settings_dir()
        settings_path = f"{store_dir}/settings.json"
        with open(settings_path, "r", encoding="utf-8") as fh:
            self.assertIn("timeout_seconds", fh.read())
        with open(settings_path, "w", encoding="utf-8") as fh:
            fh.write("{corrupt!!!")
        self.stop_jarvis()
        self.start_jarvis(self.cfg)
        _status, payload = self.view()
        # corrupt file dropped: server stays healthy on defaults
        self.assertEqual(payload["runtime"]["timeout_seconds"], 60)
        self.assertEqual(payload["runtime"]["timeout_locked"], False)


if __name__ == "__main__":
    unittest.main()
