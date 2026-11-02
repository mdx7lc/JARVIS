"""Phase 7A provider/model management tests — fake upstream only.

Covers the authenticated model catalog (GET /api/models), runtime model
selection (GET/PATCH/DELETE /api/settings/model), persistence across a
server restart, and chat regression: the selected policy reaches the
real chat payload. No live inference; isolated temporary DBs only.
"""

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
from test_chat_api import ChatAPITestCase, KEY, MODEL
from fake_upstream import FakeUpstream


def request(url, payload=None, method=None, headers=None, raw=None,
            content_type="application/json"):
    data = raw if raw is not None else (
        json.dumps(payload).encode("utf-8")
        if payload is not None else None
    )
    req = urllib.request.Request(url, data=data, method=method)
    if content_type is not None and data is not None:
        req.add_header("Content-Type", content_type)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def as_json(body):
    try:
        return json.loads(body)
    except ValueError:
        return None


def upstream_hits(upstream, path_prefix):
    with upstream.lock:
        return sum(
            1 for r in upstream.requests
            if r["path"].split("?", 1)[0].startswith(path_prefix)
        )


class ModelsAPITestCase(ChatAPITestCase):
    """Adds model catalog accessors shared by the contract tests."""

    def get_json(self, path, **kwargs):
        status, _h, body = request(self.base + path,
                                   method=kwargs.pop("method", "GET"),
                                   **kwargs)
        return status, as_json(body)

    def patch_model(self, selected, **kwargs):
        return request(
            self.base + "/api/settings/model",
            payload=({"model": selected} if not isinstance(selected, dict)
                     else selected),
            method="PATCH",
            **kwargs,
        )

    def clear_model(self):
        return request(self.base + "/api/settings/model", method="DELETE")

    def settings_model(self):
        status, _h, body = request(
            self.base + "/api/settings/model", method="GET"
        )
        return status, as_json(body)

    def restart_jarvis(self, env_overrides=None):
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": KEY,
        }
        env.update(env_overrides or {})
        self.cfg = config.load(environ=env)
        self.stop_jarvis()
        self.start_jarvis(self.cfg)


class ModelCatalogTests(ModelsAPITestCase):
    def test_valid_catalog_is_normalized(self):
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 200)
        ids = [m["id"] for m in payload["models"]]
        self.assertIn(MODEL, ids)
        self.assertIn("auto", ids)
        self.assertEqual(len(ids), len(set(ids)))
        for entry in payload["models"]:
            self.assertIsInstance(entry["available"], bool)
        # Key or auth material never appears in the response
        self.assertNotIn(KEY, json.dumps(payload))

    def test_catalog_comes_from_upstream_not_hardcoded(self):
        self.upstream.models_response = (
            200,
            {"object": "list", "data": [{"id": "custom-model-zz"}]},
            "application/json",
        )
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 200)
        self.assertEqual(
            [m["id"] for m in payload["models"]], ["custom-model-zz"]
        )

    def test_available_flags_pass_through_only_when_present(self):
        self.upstream.models_response = (
            200,
            {
                "object": "list",
                "data": [
                    {"id": "m-plain"},                       # no metadata
                    {"id": "m-off", "available": False,
                     "unavailable_reason": "quota exhausted"},
                ],
            },
            "application/json",
        )
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 200)
        by_id = {m["id"]: m for m in payload["models"]}
        self.assertTrue(by_id["m-plain"]["available"])
        self.assertNotIn("unavailable_reason", by_id["m-plain"])
        self.assertFalse(by_id["m-off"]["available"])
        self.assertEqual(
            by_id["m-off"]["unavailable_reason"], "quota exhausted"
        )

    def test_display_name_passes_through_only_when_present(self):
        self.upstream.models_response = (
            200,
            {
                "object": "list",
                "data": [
                    {"id": "m-plain"},                       # no metadata
                    {"id": "m-named", "name": "GLM 5.3 Flash"},
                    {"id": "m-blank", "name": "   "},
                    {"id": "m-long", "name": "N" * 300},
                ],
            },
            "application/json",
        )
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 200)
        by_id = {m["id"]: m for m in payload["models"]}
        self.assertNotIn("display_name", by_id["m-plain"])
        self.assertEqual(by_id["m-named"]["display_name"], "GLM 5.3 Flash")
        self.assertNotIn("display_name", by_id["m-blank"])
        self.assertEqual(by_id["m-long"]["display_name"], "N" * 200)

    def test_malformed_json_response(self):
        self.upstream.models_response = (200, "{nope", "application/json")
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "malformed_response")

    def test_unexpected_structure_rejected(self):
        for bad in (
            {"object": "list"},                 # missing data
            {"data": "not-a-list"},
            [1, 2, 3],                          # top-level list
            "just-text",
        ):
            self.upstream.models_response = (
                200,
                json.dumps(bad).encode(),
                "application/json",
            )
            status, payload = self.get_json("/api/models")
            self.assertEqual(status, 502)
            self.assertEqual(
                payload["error"]["code"], "malformed_response", bad
            )

    def test_junk_entries_are_skipped_not_echoed(self):
        self.upstream.models_response = (
            200,
            {"object": "list", "data": [
                "junk-string",
                {"no_id": True},
                {"id": "   "},
                {"id": "still-good"},
            ]},
            "application/json",
        )
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 200)
        self.assertEqual(
            [m["id"] for m in payload["models"]], ["still-good"]
        )

    def test_timeout_refused_with_taxonomy(self):
        cfg = config.load(environ={
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": KEY,
            "JARVIS_TIMEOUT_SECONDS": "1",
        })
        self.stop_jarvis()
        self.start_jarvis(cfg)
        self.upstream.delay = 2.0
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 504)
        self.assertEqual(payload["error"]["code"], "provider_timeout")

    def test_connection_failure_refused(self):
        dead = FakeUpstream()
        # Bound-but-unstarted sockets hang in the listen backlog; close
        # the listener so the port is genuinely unreachable.
        dead.server_close()
        cfg = config.load(environ={
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": dead.base_url,
            "FREELLMAPI_API_KEY": KEY,
        })
        self.stop_jarvis()
        self.start_jarvis(cfg)
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "connection_failed")

    def test_auth_failure_is_sanitized(self):
        self.upstream.models_response = (
            401, {"error": "invalid token detail: " + KEY},
            "application/json",
        )
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "auth_failed")
        self.assertNotIn(KEY, json.dumps(payload))
        self.assertNotIn("invalid token detail", json.dumps(payload))

    def test_oversized_catalog_refused(self):
        big = {"object": "list", "data": [
            {"id": "x" * 2_100_000},
        ]}
        self.upstream.models_response = (
            200, json.dumps(big).encode(), "application/json"
        )
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "malformed_response")

    def test_redirects_are_refused(self):
        self.upstream.models_response = (302, "", "text/plain")
        self.upstream.location = "http://127.0.0.1:9/evil"
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "redirect_rejected")

    def test_catalog_never_runs_inference(self):
        status, _payload = self.get_json("/api/models")
        self.assertEqual(status, 200)
        self.assertEqual(
            upstream_hits(self.upstream, "/v1/chat/completions"), 0
        )

    def test_cached_catalog_within_ttl_hits_upstream_once(self):
        first, _p = self.get_json("/api/models")
        second, _p2 = self.get_json("/api/models")
        self.assertEqual((first, second), (200, 200))
        self.assertEqual(upstream_hits(self.upstream, "/v1/models"), 1)

    def test_refresh_param_forces_upstream_fetch(self):
        self.get_json("/api/models")
        status, _h, _body = request(
            self.base + "/api/models?refresh=1", method="GET"
        )
        self.assertEqual(status, 200)
        self.assertEqual(upstream_hits(self.upstream, "/v1/models"), 2)

    def test_not_configured_catalog(self):
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
        }
        self.stop_jarvis()
        self.start_jarvis(config.load(environ=env))
        status, payload = self.get_json("/api/models")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "not_configured")

    def test_models_method_restrictions_and_head(self):
        status, headers, body = request(
            self.base + "/api/models", payload={"x": 1}, method="POST"
        )
        self.assertEqual(status, 405)
        self.assertIn("GET", headers.get("Allow", ""))
        status, _h, body = request(
            self.base + "/api/models", method="HEAD"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_models_cross_origin_rejected(self):
        status, _h, body = request(
            self.base + "/api/models",
            method="GET",
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(
            as_json(body)["error"]["code"], "cross_origin_forbidden"
        )

    def test_models_rate_limited(self):
        # Burn the policy window directly on the server's limiter to
        # avoid dependence on timing.
        status1, _p = self.get_json("/api/models")
        self._httpd.rate_limiter.add_policy("models", 0, 60.0)
        status2, payload = self.get_json("/api/models")
        self.assertEqual(status1, 200)
        self.assertEqual(status2, 429)
        self.assertEqual(payload["error"]["code"], "rate_limited")


class ModelSelectionTests(ModelsAPITestCase):
    def test_default_settings_report_environment_default(self):
        status, payload = self.settings_model()
        self.assertEqual(status, 200)
        self.assertEqual(payload["selected"], MODEL)
        self.assertEqual(payload["model_policy"], MODEL)
        self.assertEqual(payload["source"], "environment_default")
        self.assertTrue(payload["provider_configured"])
        self.assertNotIn(KEY, json.dumps(payload))

    def test_settings_get_performs_no_upstream_request(self):
        before = len(self.upstream.requests)
        status, _p = self.settings_model()
        self.assertEqual(status, 200)
        self.assertEqual(len(self.upstream.requests), before)

    def test_apply_catalog_backed_model_affects_chat(self):
        status, _h, body = self.patch_model("auto")
        self.assertEqual(status, 200)
        applied = as_json(body)
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["source"], "runtime_override")
        chat_status, _h, body = request(
            self.base + "/api/chat",
            payload={"message": "hello after switch"},
            method="POST",
        )
        chat_payload = as_json(body)
        self.assertEqual(chat_status, 200)
        self.assertEqual(chat_payload["model"], "auto")
        # verify via recorded upstream body (the authoritative contract)
        with self.upstream.lock:
            chat_calls = [
                r for r in self.upstream.requests
                if r["path"].startswith("/v1/chat/completions")
            ]
        self.assertEqual(len(chat_calls), 1)
        self.assertEqual(chat_calls[-1]["body"]["model"], "auto")

    def test_apply_valid_model_from_catalog(self):
        self.upstream.models_response = (
            200,
            {"object": "list", "data": [{"id": "m-valid"}]},
            "application/json",
        )
        # The PATCH fetches the catalog itself when the cache is cold
        status, _h, body = self.patch_model("m-valid")
        self.assertEqual(status, 200)
        self.assertTrue(as_json(body)["applied"])
        with self.upstream.lock:
            self.assertTrue(
                any(r["path"].startswith("/v1/models") for r
                    in self.upstream.requests)
            )

    def test_reject_unknown_model(self):
        status, _h, body = self.patch_model("no-such-model-xyz")
        self.assertEqual(status, 400)
        self.assertEqual(as_json(body)["error"]["code"],
                         "model_not_available")
        # Previous policy preserved
        _s, settings = self.settings_model()
        self.assertEqual(settings["selected"], MODEL)
        # chat still uses the default policy
        request(self.base + "/api/chat", payload={"message": "check"},
                method="POST")
        with self.upstream.lock:
            calls = [r for r in self.upstream.requests
                     if r["path"].startswith("/v1/chat/completions")]
        self.assertEqual(calls[-1]["body"]["model"], MODEL)

    def test_reject_unavailable_model(self):
        self.upstream.models_response = (
            200,
            {"object": "list", "data": [
                {"id": "m-off", "available": False,
                 "unavailable_reason": "quota exhausted"},
            ]},
            "application/json",
        )
        status, _h, body = self.patch_model("m-off")
        self.assertEqual(status, 400)
        self.assertEqual(as_json(body)["error"]["code"],
                         "model_not_available")
        _s, settings = self.settings_model()
        self.assertEqual(settings["selected"], MODEL)

    def test_reject_invalid_selections(self):
        for bad in (
            None,                    # key without string value
            {"model": ""},
            {"model": "   "},
            {"model": "has space"},
            {"model": 42},
            {"model": "x" * 129},
            "",                      # sent as the whole payload
        ):
            if isinstance(bad, dict):
                status, _h, body = self.patch_model(bad)
            else:
                status, _h, body = request(
                    self.base + "/api/settings/model",
                    raw=json.dumps(bad).encode() if bad is not None
                    else b"null",
                    method="PATCH",
                )
            payload = as_json(body)
            self.assertEqual(status, 400, repr(bad))
            self.assertIn(payload["error"]["code"],
                          ("invalid_model", "invalid_request"), repr(bad))
        _s, settings = self.settings_model()
        self.assertEqual(settings["selected"], MODEL)

    def test_reject_unknown_fields(self):
        status, _h, body = self.patch_model(
            {"model": MODEL, "base_url": "http://evil:9"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(as_json(body)["error"]["code"], "invalid_request")
        # The provider base URL must never be client-controlled
        _s, settings = self.settings_model()
        self.assertEqual(settings["selected"], MODEL)

    def test_patch_rate_limited_and_cross_origin(self):
        status, _h, body = self.patch_model(
            MODEL, headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(as_json(body)["error"]["code"],
                         "cross_origin_forbidden")
        self._httpd.rate_limiter.add_policy("model_settings", 0, 60.0)
        status, _h, body = self.patch_model(MODEL)
        self.assertEqual(status, 429)
        self.assertEqual(as_json(body)["error"]["code"], "rate_limited")

    def test_clear_override_restores_environment_default(self):
        status, _h, _b = self.patch_model("auto")
        self.assertEqual(status, 200)
        _s, settings = self.settings_model()
        self.assertEqual(settings["source"], "runtime_override")
        status, _h, body = self.clear_model()
        self.assertEqual(status, 200)
        self.assertTrue(as_json(body)["cleared"])
        _s, settings = self.settings_model()
        self.assertEqual(settings["selected"], MODEL)
        self.assertEqual(settings["source"], "environment_default")
        request(self.base + "/api/chat", payload={"message": "after clear"},
                method="POST")
        with self.upstream.lock:
            calls = [r for r in self.upstream.requests
                     if r["path"].startswith("/v1/chat/completions")]
        self.assertEqual(calls[-1]["body"]["model"], MODEL)

    def test_override_persists_across_restart(self):
        self.patch_model("auto")
        self.restart_jarvis()
        _s, settings = self.settings_model()
        self.assertEqual(settings["selected"], "auto")
        self.assertEqual(settings["source"], "runtime_override")
        request(self.base + "/api/chat", payload={"message": "post-restart"},
                method="POST")
        with self.upstream.lock:
            calls = [r for r in self.upstream.requests
                     if r["path"].startswith("/v1/chat/completions")]
        self.assertEqual(calls[-1]["body"]["model"], "auto")

    def test_invalid_saved_override_recovers_safely(self):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) "
                    "VALUES ('model_override', 'bad model with spaces')"
                )
        finally:
            conn.close()
        status, _h, _b = request(
            self.base + "/api/chat", payload={"message": "recovery check"},
            method="POST",
        )
        self.assertEqual(status, 200)
        with self.upstream.lock:
            calls = [r for r in self.upstream.requests
                     if r["path"].startswith("/v1/chat/completions")]
        self.assertEqual(calls[-1]["body"]["model"], MODEL)
        _s, settings = self.settings_model()
        self.assertEqual(settings["selected"], MODEL)
        self.assertEqual(settings["source"], "environment_default")

    def test_existing_conversation_survives_model_switch(self):
        _s, _h, body = request(
            self.base + "/api/chat", payload={"message": "first msg"},
            method="POST",
        )
        first = as_json(body)
        conv_id = first["conversation_id"]
        self.patch_model("auto")
        status, _h, body = request(
            self.base + "/api/chat",
            payload={"message": "second msg", "conversation_id": conv_id},
            method="POST",
        )
        second = as_json(body)
        self.assertEqual(status, 200)
        self.assertEqual(second["conversation_id"], conv_id)
        conn = db.open_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT role, content FROM messages "
                "WHERE conversation_id=? ORDER BY created_at, id",
                (conv_id,),
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(
            [(r["role"], r["content"]) for r in rows],
            [
                ("user", "first msg"),
                ("assistant", "Hello! How can I help?"),
                ("user", "second msg"),
                ("assistant", "Hello! How can I help?"),
            ],
        )

    def test_status_reports_selection_and_catalog_state(self):
        status, _h, body = request(self.base + "/api/status", method="GET")
        payload = as_json(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["effective_model"], MODEL)
        self.assertIsNone(payload["last_inference"])
        _s2, _h2, _b2 = self.patch_model("auto")
        request(self.base + "/api/chat", payload={"message": "hi"},
                method="POST")
        status, _h, body = request(self.base + "/api/status", method="GET")
        payload = as_json(body)
        self.assertEqual(payload["effective_model"], "auto")
        self.assertEqual(payload["model_source"], "runtime_override")
        self.assertEqual(payload["last_inference"]["model"], "auto")
        self.assertNotIn(KEY, json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
