"""Phase 11A browser E2E — Settings Center UI against the fake provider.

Drives the real settings modal: provider list and key handling
(write-only), env-lock display, runtime persistence, and the only
activation path (Apply) switching the chat to OrcaRouter with the
documented cost confirmation. Canaries prove credentials never leak
into the page, storage, or server logs. No real network; the fake
upstream doubles as the OrcaRouter catalog (monkeypatched URLs, so the
live service is never contacted).
"""

from unittest import mock

from browser_harness import Stack, BrowserCase, SKIP_BROWSER, PROVIDER_KEY

ORCA_KEY = "orca-e2e-canary-7890"
ORCA_MODELS = {
    "object": "list",
    "data": [
        {"id": "orcarouter/auto", "owned_by": "orcarouter"},
        {"id": "z-ai/glm-4.6", "owned_by": "z-ai"},
        {"id": "deepseek/deepseek-chat-v3.1-free"},
    ],
}


class _SettingsCenterBase(BrowserCase):
    """Shared helpers + orca fake-upstream plumbing; no tests here."""
    REPLY = "Hello! How can I help?"
    EXTRA_ENV = None

    def make_stack(self, reply):
        return Stack(reply=reply, extra_env=self.EXTRA_ENV)

    def setUp(self):
        super().setUp()
        up = self.stack.upstream
        up.models_response = (200, ORCA_MODELS, "application/json")
        # orca pricing URLs + profile base point at the same fake
        # upstream (resolved per request, so patching after server
        # start is safe) — the live service is never contacted
        patches = [
            mock.patch.object(
                __import__("provider"), "ORCAROUTER_PRICING_URL",
                up.base_url + "/api/pricing"),
            mock.patch.object(
                __import__("provider"), "ORCAROUTER_FREE_URL",
                up.base_url + "/api/free-package/public"),
            mock.patch.dict(
                __import__("provider").PROVIDER_PROFILES["orcarouter"],
                {"base_url": up.base_url}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def open_settings(self):
        self.page.click("#settings-btn")
        self.page.wait_for_selector("#modal-settings[open]")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#provider-list"
            " .provider-item').length > 0")

    def page_text(self):
        return self.page.evaluate(
            "() => document.body.innerText + '||'"
            "+ JSON.stringify(Object.fromEntries("
            "Object.entries(localStorage)))")

    def stack_chat_calls(self):
        with self.stack.upstream.lock:
            return sum(1 for r in self.stack.upstream.requests
                       if r["path"].startswith("/v1/chat"))

    def switch_tab(self, tab):
        self.page.click(f"button[data-tab='{tab}']")
        self.page.wait_for_selector(f"#settings-{tab}-tab:not([hidden])")

    def error_contains(self, text):
        self.page.wait_for_function(
            "(needle) => { const e = document.querySelector('#settings-error');"
            " return e && !e.hidden && e.textContent.includes(needle); }",
            arg=text)

@SKIP_BROWSER
class SettingsCenterBrowserTests(_SettingsCenterBase):
    """Main flow suite (no orca env key): UI key entry is exercised."""

    def test_settings_open_lists_profiles_without_secrets(self):
        self.open_settings()
        items = self.page.inner_text("#provider-list")
        self.assertIn("FreeLLMAPI", items)
        self.assertIn("OrcaRouter", items)
        everything = self.page_text()
        self.assertNotIn(PROVIDER_KEY, everything)
        self.assertNotIn(ORCA_KEY, everything)
        # freellm's key is env-managed (harness env): locked input;
        # orca has no key yet: editable
        self.assertTrue(self.page.is_disabled("#settings-key-freellmapi"))
        self.assertFalse(self.page.is_disabled("#settings-key-orcarouter"))

    def test_save_orca_key_is_write_only(self):
        self.open_settings()
        self.page.fill("#settings-key-orcarouter", ORCA_KEY)
        self.page.click(".settings-key-save[data-provider='orcarouter']")
        self.page.wait_for_function(
            "() => document.querySelector('#provider-list').innerText"
            ".toLowerCase().includes('key stored')")
        # input cleared after save (write-only display)
        self.assertEqual(self.page.input_value("#settings-key-orcarouter"),
                         "")
        everything = self.page_text()
        self.assertNotIn(ORCA_KEY, everything)
        # probe with the stored key: catalog only, never inference
        before = self.stack_chat_calls()
        self.page.click(".settings-probe[data-provider='orcarouter']")
        self.page.wait_for_function(
            "() => (document.querySelector('[data-probe-result=\"orcarouter\"]')"
            " || {textContent: ''}).textContent.includes('OK')")
        self.assertEqual(self.stack_chat_calls(), before)
        probe = self.page.evaluate(
            "() => (document.querySelector('[data-probe-result=\"orcarouter\"]')"
            " || {textContent: ''}).textContent")
        self.assertIn("models listed", probe)

    def test_runtime_tab_persists_across_restart(self):
        self.open_settings()
        self.switch_tab("runtime")
        self.page.fill("#settings-timeout-input", "42")
        self.page.click("#settings-runtime-save")
        self.page.wait_for_function(
            "() => document.getElementById('settings-timeout-input')"
            ".value === '42'")
        # restart moves the server to a new port; the harness runs the
        # same persisted settings dir
        self.stack.restart_server(self)
        self.page.goto(self.stack.base + "/")
        self.wait_ready()
        self.open_settings()
        self.switch_tab("runtime")
        self.assertEqual(
            self.page.input_value("#settings-timeout-input"), "42")

    def test_apply_switches_chat_to_orca_with_confirmation(self):
        # direct key setup (credentials API): UI key entry tested elsewhere
        self.stack.api("POST", "/api/settings/credentials",
                       {"provider": "orcarouter", "api_key": ORCA_KEY})
        self.open_settings()
        self.switch_tab("model")
        self.page.select_option("#settings-provider-select", "orcarouter")
        self.page.fill("#settings-model-input", "orcarouter/auto")
        # paid/unknown model without confirmation → 409 honored
        self.page.click("#settings-apply")
        self.error_contains("cost_confirmation_required")
        # confirm and re-apply
        self.page.check("#settings-confirm-cost")
        self.page.click("#settings-apply")
        self.page.wait_for_function(
            "() => document.getElementById('settings-provider-select')"
            ".value === 'orcarouter'")
        # the applied policy governs the real chat path
        self.page.click("[data-close-modal='modal-settings']")
        self.open_chat()
        self.send_chat("hello after provider switch")
        self.page.wait_for_function(
            "() => Array.from(document.querySelectorAll"
            "('#chat-log .chat-msg--jarvis:not(.chat-typing)'))"
            ".some((t) => t.textContent.includes('Hello! How can I help?'))")
        with self.stack.upstream.lock:
            chats = [r for r in self.stack.upstream.requests
                     if r["path"].startswith("/v1/chat")]
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[-1]["body"]["model"], "orcarouter/auto")

    def test_free_fallback_refused_honestly(self):
        self.stack.api("POST", "/api/settings/credentials",
                       {"provider": "orcarouter", "api_key": ORCA_KEY})
        self.open_settings()
        self.switch_tab("model")
        self.page.select_option("#settings-provider-select", "orcarouter")
        self.page.fill("#settings-model-input", "orcarouter/auto")
        self.page.fill("#settings-fallback-input",
                       "deepseek/deepseek-chat-v3.1-free")
        self.page.check("#settings-confirm-cost")
        self.page.click("#settings-apply")
        self.error_contains("fallback_not_supported")

    def test_probe_failure_is_honest(self):
        self.open_settings()
        self.page.click(".settings-probe[data-provider='orcarouter']")
        self.page.wait_for_function(
            "() => { const n = document.querySelector"
            "('[data-probe-result=\"orcarouter\"]');"
            " return n && n.textContent.length > 0"
            " && n.textContent !== 'Testing…'; }")
        probe = self.page.evaluate(
            "() => document.querySelector('[data-probe-result=\"orcarouter\"]')"
            ".textContent")
        self.assertNotIn("OK:", probe)
        self.assertIn("Failed", probe)

    def test_model_tab_shows_catalog_cost_and_availability(self):
        # Phase 11A: the routing tab's hints come from the cached,
        # pricing-annotated server catalog — three honest cost states
        self.stack.api("POST", "/api/settings/credentials",
                       {"provider": "orcarouter", "api_key": ORCA_KEY})
        self.open_settings()
        self.switch_tab("model")
        self.page.select_option("#settings-provider-select", "orcarouter")
        self.page.fill("#settings-model-input",
                       "deepseek/deepseek-chat-v3.1-free")
        self.page.wait_for_function(
            "() => (document.querySelector('[data-catalog-meta]') ||"
            " {textContent: ''}).textContent.includes"
            "('free (catalog-verified)')")
        # a paid model says so, without forcing any inference
        self.page.fill("#settings-model-input", "z-ai/glm-4.6")
        self.page.wait_for_function(
            "() => document.querySelector('[data-catalog-meta]')"
            ".textContent.includes('paid')")
        # an id outside the fetched catalog stays honest
        self.page.fill("#settings-model-input", "totally/unknown-id")
        self.page.wait_for_function(
            "() => document.querySelector('[data-catalog-meta]')"
            ".textContent.includes('not in the fetched catalog')")
        # the selector suggestions are catalog-sourced ids
        values = self.page.evaluate(
            "() => Array.from(document.querySelectorAll"
            "('#settings-model-options option')).map((o) => o.value)")
        self.assertIn("orcarouter/auto", values)
        self.assertIn("deepseek/deepseek-chat-v3.1-free", values)
        labels = self.page.evaluate(
            "() => Array.from(document.querySelectorAll"
            "('#settings-model-options option')).map((o) => o.label)")
        self.assertTrue(all('cost:' in l for l in labels))


@SKIP_BROWSER
class EnvManagedKeyProfileTests(_SettingsCenterBase):
    """Environment-provided keys: the provider profile shows the lock
    and the key input refuses input outright."""
    EXTRA_ENV = {"JARVIS_ORCAROUTER_API_KEY": ORCA_KEY}

    def setUp(self):
        # pricing URL patches are irrelevant here; skip the base class
        # setUp and only run the browser stack lifecycle
        BrowserCase.setUp(self)
        self.stack.upstream.models_response = (
            200, ORCA_MODELS, "application/json")

    def test_env_key_is_locked_and_refuses_save(self):
        self.open_settings()
        statuses = self.page.evaluate(
            "() => Array.from(document.querySelectorAll"
            "('#provider-list .key-status')).map((el) => el.textContent)")
        self.assertTrue(
            any("environment" in s for s in statuses),
            "orca profile must show the env-managed lock")
        self.assertTrue(self.page.is_disabled("#settings-key-orcarouter"))
        self.assertTrue(self.page.is_disabled(
            ".settings-key-save[data-provider='orcarouter']"))
        self.assertTrue(self.page.is_disabled(
            ".settings-key-clear[data-provider='orcarouter']"))
        # nothing was saved behind the UI's back
        everything = self.page_text()
        self.assertNotIn(ORCA_KEY, everything)

    def test_settings_open_lists_profiles_without_secrets(self):
        # variant under env management: BOTH keys are locked
        self.open_settings()
        everything = self.page_text()
        self.assertNotIn(PROVIDER_KEY, everything)
        self.assertNotIn(ORCA_KEY, everything)
        self.assertTrue(self.page.is_disabled("#settings-key-freellmapi"))
        self.assertTrue(self.page.is_disabled("#settings-key-orcarouter"))
