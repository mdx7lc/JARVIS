"""Phase 7A browser E2E — model management UI against the fake provider.

Drives the real dialog UI: open via the provider tile entry point, load
the catalog from the configured gateway, filter, apply a selection that
reaches real chat payloads, clear back to the environment default, and
recover from failures. Canaries prove credentials never leak into the
page, storage or server logs.
"""

import json

from browser_harness import Stack, BrowserCase, SKIP_BROWSER, PROVIDER_KEY

CATALOG = {
    "object": "list",
    "data": [
        {"id": "qwen3.8-27b"},
        {"id": "auto"},
        {"id": "m-gone", "available": False,
         "unavailable_reason": "quota exhausted"},
    ],
}


@SKIP_BROWSER
class ModelManagementBrowserTests(BrowserCase):
    REPLY = "Hello! How can I help?"

    def setUp(self):
        super().setUp()
        self.stack.upstream.models_response = (
            200, CATALOG, "application/json")

    def open_models(self):
        self.page.evaluate("openModelsModal()")
        self.page.wait_for_selector("#modal-models[open]")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#models-list .model-select')"
            ".length > 0")

    def model_ids(self):
        return self.page.evaluate(
            "() => Array.from(document.querySelectorAll"
            "('models-list .model-select .model-id')"
            "|| Array.from(document.querySelectorAll('#models-list"
            " .model-id')).map((el) => el.textContent)")

    def test_catalog_loads_from_gateway(self):
        self.open_models()
        ids = self.page.evaluate(
            "() => Array.from(document.querySelectorAll('#models-list"
            " .model-id')).map((el) => el.textContent)")
        self.assertEqual(ids, ["qwen3.8-27b", "auto", "m-gone"])
        note = self.page.inner_text("#models-note")
        self.assertIn("environment default", note)
        self.assertIn("qwen3.8-27b", note)
        # no inference request was made just by opening management
        self.assertEqual(self.upstream_chat_calls(), 0)

    def upstream_chat_calls(self):
        with self.stack.upstream.lock:
            return sum(1 for r in self.stack.upstream.requests
                       if r["path"].startswith("/v1/chat"))

    def test_filter_narrows_catalog(self):
        self.open_models()
        self.page.fill("#models-filter", "auto")
        remaining = self.page.evaluate(
            "() => Array.from(document.querySelectorAll('#models-list"
            " .model-id')).map((el) => el.textContent)")
        self.assertEqual(remaining, ["auto"])
        self.page.fill("#models-filter", "zzz-no-match")
        self.assertIn("No model matches this filter",
                      self.page.inner_text("#models-list"))

    def test_apply_model_updates_policy_and_chat(self):
        self.open_models()
        self.page.click("#models-list .model-select[data-model='auto']")
        self.page.wait_for_function(
            "() => document.querySelector('#models-list"
            " .model-select[data-model=\"auto\"]')"
            ".closest('li').classList.contains('is-active')")
        note = self.page.inner_text("#models-note")
        self.assertIn("auto", note)
        self.assertIn("runtime override", note)
        # the applied policy reaches the real chat path
        self.open_chat()
        self.send_chat("hello with the new model")
        self.page.wait_for_function(
            "() => Array.from(document.querySelectorAll"
            "('#chat-log .chat-msg--jarvis:not(.chat-typing)"
            " .chat-msg--tag'))"
            ".some((t) => t.textContent.includes('AI reply · auto'))")
        with self.stack.upstream.lock:
            calls = [r for r in self.stack.upstream.requests
                     if r["path"].startswith("/v1/chat")]
            self.assertEqual(calls[-1]["body"]["model"], "auto")

    def test_unavailable_model_rows_are_disabled_with_reason(self):
        self.open_models()
        gone = self.page.locator(
            "#models-list .model-select[data-model='m-gone']")
        self.assertTrue(gone.is_disabled())
        self.assertIn("quota exhausted",
                      self.page.inner_text("#models-list"))

    def test_failed_catalog_refresh_keeps_policy_and_explains(self):
        self.open_models()
        self.stack.upstream.models_response = (
            500, {"object": "list", "data": []}, "application/json")
        self.page.click("#models-refresh")
        self.page.wait_for_selector("#models-error:not([hidden])")
        err = self.page.inner_text("#models-error")
        self.assertIn("upstream_error", err)
        # previous catalog entries remain; policy never silently changes
        self.assertEqual(
            len(self.page.query_selector_all("#models-list .model-select")),
            3)
        self.assertIn("qwen3.8-27b", self.page.inner_text("#models-note"))

    def test_failed_selection_retains_previous_policy(self):
        self.stack._httpd.rate_limiter.add_policy("model_settings", 0, 60.0)
        self.open_models()
        self.page.click("#models-list .model-select[data-model='auto']")
        self.page.wait_for_selector("#models-error:not([hidden])")
        self.assertIn("rate_limited",
                      self.page.inner_text("#models-error"))
        note = self.page.inner_text("#models-note")
        self.assertIn("qwen3.8-27b", note)
        self.assertIn("environment default", note)
        # buttons usable again after the failure
        self.assertFalse(self.page.locator(
            "#models-list .model-select[data-model='auto']"
        ).is_disabled())

    def test_clear_override_restores_environment_default(self):
        self.open_models()
        self.page.click("#models-list .model-select[data-model='auto']")
        self.page.wait_for_function(
            "() => document.querySelector('#models-note').textContent"
            ".includes('runtime override')")
        self.page.click("#models-clear")
        self.page.wait_for_function(
            "() => document.querySelector('#models-note').textContent"
            ".includes('environment default')")
        note = self.page.inner_text("#models-note")
        self.assertIn("qwen3.8-27b", note)
        status = self.stack.api("GET", "/api/settings/model")[1]
        self.assertEqual(status["source"], "environment_default")

    def test_selection_survives_page_reload(self):
        self.open_models()
        self.page.click("#models-list .model-select[data-model='auto']")
        self.page.wait_for_function(
            "() => document.querySelector('#models-note').textContent"
            ".includes('runtime override')")
        self.reload()
        self.open_models()
        note = self.page.inner_text("#models-note")
        self.assertIn("auto", note)
        self.assertIn("runtime override", note)

    def test_large_catalog_is_filterable_not_a_wall(self):
        big = {
            "object": "list",
            "data": ([{"id": f"model-{i}"} for i in range(150)]
                     + [{"id": "auto"}]),
        }
        self.stack.upstream.models_response = (
            200, big, "application/json")
        self.open_models()
        self.page.wait_for_function(
            "() => document.querySelectorAll('#models-list .model-select')"
            ".length > 100")
        # the list stays inside a bounded scroll container
        height = self.page.evaluate(
            "() => document.getElementById('models-list').offsetHeight")
        self.assertLess(height, 800)
        self.page.fill("#models-filter", "model-149")
        ids = self.page.evaluate(
            "() => Array.from(document.querySelectorAll('#models-list"
            " .model-id')).map((el) => el.textContent)")
        self.assertEqual(ids, ["model-149"])

    def test_no_secrets_reach_page_or_storage(self):
        self.open_models()
        self.open_chat()
        self.send_chat("leak probe")
        self.page.wait_for_selector(
            "#chat-log .chat-msg--jarvis:not(.chat-typing)")
        rendered = self.page.content()
        self.assertNotIn(PROVIDER_KEY, rendered)
        self.assertNotIn(self.stack.upstream.base_url, rendered)
        self.assertNotIn("Bearer", rendered)
        storage = self.page.evaluate(
            "() => JSON.stringify(Object.entries(localStorage))")
        self.assertNotIn(PROVIDER_KEY, storage)

    def test_server_log_stays_secret_free(self):
        self.open_models()
        self.page.click("#models-list .model-select[data-model='auto']")
        self.page.wait_for_function(
            "() => document.querySelector('#models-note').textContent"
            ".includes('runtime override')")
        stderr = self.stack.stderr_cap.text()
        self.assertNotIn(PROVIDER_KEY, stderr)
        self.assertNotIn("Bearer", stderr)
        for line in (l for l in stderr.splitlines() if l.strip()):
            self.assertTrue(line.lstrip().startswith("{")
                            and line.rstrip().endswith("}"), line)


@SKIP_BROWSER
class FallbackPolicyBrowserTests(BrowserCase):
    """Phase 8A — honest routing/fallback metadata in the real UI."""

    FALLBACK = "fallback-model-e2e"

    def make_stack(self, reply):
        return Stack(reply=reply,
                     extra_env={"JARVIS_FALLBACK_MODEL": self.FALLBACK})

    def open_models(self):
        self.page.evaluate("openModelsModal()")
        self.page.wait_for_selector("#modal-models[open]")
        self.page.wait_for_function(
            "() => document.querySelector('#models-note').textContent"
            ".includes('Current model policy')")

    def fallback_reply(self, content):
        return (200, {
            "object": "chat.completion",
            "model": self.FALLBACK,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
        }, "application/json")

    def test_models_note_reports_fallback_policy(self):
        self.open_models()
        note = self.page.inner_text("#models-note")
        self.assertIn(self.FALLBACK, note)
        self.assertIn("Fallback policy", note)
        self.assertIn("environment default", note)

    def test_failed_primary_falls_back_and_tag_is_honest(self):
        self.stack.upstream.chat_responses = [
            (500, {"error": {"message": "boom"}}, "application/json"),
            self.fallback_reply("recovered via fallback"),
        ]
        self.open_chat()
        self.send_chat("trigger the fallback path")
        self.page.wait_for_selector(
            "#chat-log .chat-msg--jarvis:not(.chat-typing)")
        tag = self.page.inner_text(
            "#chat-log .chat-msg--jarvis:not(.chat-typing) .chat-msg--tag")
        self.assertIn(self.FALLBACK, tag.lower())
        self.assertIn("recovered via fallback",
                      self.page.inner_text("#chat-log"))

    def test_fallback_event_in_server_log_stays_secret_free(self):
        self.stack.upstream.chat_responses = [
            (500, {"error": {"message": "boom"}}, "application/json"),
            self.fallback_reply("recovered via fallback"),
        ]
        self.open_chat()
        self.send_chat("log probe")
        self.page.wait_for_selector(
            "#chat-log .chat-msg--jarvis:not(.chat-typing)")
        stderr = self.stack.stderr_cap.text()
        events = [
            json.loads(line) for line in stderr.splitlines()
            if line.startswith("{")
        ]
        self.assertTrue(
            any(e.get("event") == "model_fallback"
                and e.get("primary_code") == "upstream_error"
                and e.get("fallback_model") == self.FALLBACK
                for e in events),
            stderr)
        self.assertNotIn(PROVIDER_KEY, stderr)
        self.assertNotIn("log probe", stderr)


@SKIP_BROWSER
class NoFallbackPolicyBrowserTests(BrowserCase):
    """Without a fallback policy the UI must not invent one."""

    def test_models_note_has_no_fallback_line(self):
        self.page.evaluate("openModelsModal()")
        self.page.wait_for_selector("#modal-models[open]")
        self.page.wait_for_function(
            "() => document.querySelector('#models-note').textContent"
            ".includes('Current model policy')")
        note = self.page.inner_text("#models-note")
        self.assertNotIn("Fallback policy", note)


if __name__ == "__main__":
    import unittest
    unittest.main()
