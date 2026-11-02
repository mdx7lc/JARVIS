"""Phase 9A: scripts/verify_model_pair.py — fake upstream only.

Never performs real inference: every live-mode case here targets the
scriptable fake upstream through the environment variables, exactly
the way the operator would drive the real gateway.
"""

import json
import os
import subprocess
import sys
import unittest

from fake_upstream import FakeUpstream

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "verify_model_pair.py")


def _catalog_payload(rows):
    return {"object": "list", "data": rows}


PAIR_CATALOG = _catalog_payload([
    {"id": "glm-5.3-flash", "name": "GLM 5.3 Flash", "available": True},
    {"id": "glm-5.3", "name": "GLM 5.3", "available": True},
    {"id": "twin", "name": "Twin Display Example", "available": True},
    {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash",
     "available": True},
    {"id": "legacy", "name": "DeepSeek 4.1 Flash", "available": False,
     "unavailable_reason": "no_key"},
])

KEY = "script-test-key"

def _run(upstream, *args, set_key=True):
    env = dict(os.environ)
    env["FREELLMAPI_URL"] = upstream.base_url
    if set_key:
        env["FREELLMAPI_API_KEY"] = KEY
    else:
        env.pop("FREELLMAPI_API_KEY", None)
    env.pop("JARVIS_MODEL", None)
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True, text=True, env=env, timeout=120)


class VerifyModelPairTests(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.upstream.start(self)

    def test_catalog_default_resolves_primary_and_flags_fallback(self):
        self.upstream.models_response = (200, PAIR_CATALOG,
                                         "application/json")
        r = _run(self.upstream)
        self.assertNotIn(KEY, r.stdout + r.stderr)
        self.assertIn("'glm-5.3-flash'", r.stdout)
        self.assertIn("NOT resolved", r.stdout)
        self.assertIn("DeepSeek V4 Flash", r.stdout)  # review-only hint
        self.assertEqual(r.returncode, 2)
        report = json.loads(r.stdout[r.stdout.rindex(chr(10) + "{"):])
        self.assertTrue(report["models"]["primary"]["confirmed"])
        self.assertFalse(report["models"]["fallback"]["confirmed"])
        # catalog mode never POSTs
        self.assertEqual(
            [q["path"] for q in self.upstream.requests],
            ["/v1/models"])

    def test_catalog_missing_key_fails_closed(self):
        self.upstream.models_response = (200, PAIR_CATALOG,
                                         "application/json")
        r = _run(self.upstream, set_key=False)
        self.assertNotIn(KEY, r.stdout + r.stderr)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not set", r.stderr)

    def test_exact_display_match_beats_case(self):
        self.upstream.models_response = (200, _catalog_payload([
            {"id": "pid", "name": "GLM 5.3 FLash", "available": True},
        ]), "application/json")
        r = _run(self.upstream, "--fallback-name", "GLM 5.3 Flash")
        self.assertEqual(r.returncode, 0)
        self.assertIn("'pid'", r.stdout)

    def test_ambiguous_display_name_fails_without_guessing(self):
        self.upstream.models_response = (200, _catalog_payload([
            {"id": "a", "name": "GLM 5.3 Flash", "available": True},
            {"id": "b", "name": "GLM 5.3 Flash", "available": True},
        ]), "application/json")
        r = _run(self.upstream)
        self.assertEqual(r.returncode, 2)
        self.assertIn("share this display name", r.stdout)

    def test_explicit_ids_are_catalog_confirmed(self):
        self.upstream.models_response = (200, PAIR_CATALOG,
                                         "application/json")
        r = _run(self.upstream, "--primary-id", "glm-5.3-flash")
        self.assertEqual(r.returncode, 2)  # fallback still unresolved
        self.assertIn("confirmed in catalog", r.stdout)
        chat_paths = [q["path"] for q in self.upstream.requests
                      if q["path"].startswith("/v1/chat")]
        self.assertEqual(chat_paths, [])

    def test_live_mode_sends_one_request_per_model_and_records_echo(self):
        self.upstream.models_response = (200, PAIR_CATALOG,
                                         "application/json")
        self.upstream.chat_responses = [
            (200, {"choices": [{"message": {"content": "OK"}}],
                   "model": "glm-5.3-flash"}, "application/json"),
            (200, {"choices": [{"message": {"content": "ok"}}],
                   "model": "deepseek-v4-flash"}, "application/json"),
        ]
        r = _run(self.upstream,
                 "--live-inference",
                 "--fallback-name", "DeepSeek V4 Flash")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        paths = [q["path"] for q in self.upstream.requests
                 if q["path"].startswith("/v1/chat")]
        self.assertEqual(len(paths), 2)
        # one bounded smoke prompt per model, no retry bursts
        bodies = [q["body"] for q in self.upstream.requests
                  if q["path"].startswith("/v1/chat")]
        self.assertEqual(
            bodies[0]["model"], "glm-5.3-flash")
        self.assertEqual(
            bodies[1]["model"], "deepseek-v4-flash")
        self.assertIn("one request each", r.stdout)
        self.assertIn("inference-tested, non-empty reply",
                      r.stdout)
        self.assertIn("metadata only", r.stdout)
        # no full reply bodies in the operator output
        self.assertNotIn("Hello", r.stdout)

    def test_live_mode_nonzero_when_a_model_fails(self):
        self.upstream.models_response = (200, PAIR_CATALOG,
                                         "application/json")
        self.upstream.chat_responses = [
            (200, {"choices": [{"message": {"content": "OK"}}]},
             "application/json"),
            (401, {"error": "bad"}, "application/json"),
        ]
        r = _run(self.upstream,
                 "--live-inference",
                 "--fallback-name", "DeepSeek V4 Flash")
        self.assertEqual(r.returncode, 1)
        report = json.loads(r.stdout[r.stdout.rindex(chr(10) + "{"):])
        self.assertTrue(report["results"]["primary"]["ok"])
        self.assertFalse(report["results"]["fallback"]["ok"])
        self.assertEqual(report["results"]["fallback"]["code"],
                         "auth_failed")

    def test_live_mode_refuses_unavailable_catalog_id(self):
        self.upstream.models_response = (200, PAIR_CATALOG,
                                         "application/json")
        r = _run(self.upstream, "--live-inference")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        # ambiguous fallback refused BEFORE any inference request
        chat_paths = [q["path"] for q in self.upstream.requests
                      if q["path"].startswith("/v1/chat")]
        self.assertEqual(chat_paths, [])


if __name__ == "__main__":
    unittest.main()
