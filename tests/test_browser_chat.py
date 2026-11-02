"""Workflow A (Phase 6B) — chat round-trips driven in a real browser.

Dock composer → conversation modal → provider reply rendered by the
frozen renderer, persisted through the real HTTP API and recovered on
reload. Everything the "AI" says comes from the fake upstream.
"""

import copy
import sqlite3

import fake_upstream
from browser_harness import BrowserCase, SKIP_BROWSER

# textContent survives CSS text-transform, so compare on it (inner_text
# uppercases tags/headings).
RAW = "el => el.textContent"


def _reply(text):
    upd = copy.deepcopy(fake_upstream.OK_RESPONSE)
    upd["choices"][0]["message"]["content"] = text
    return (200, upd, "application/json")


MD_REPLY = (
    "### Plan\n\n- item one\n- item two\n\n"
    "Run `jarvis --check` first:\n\n```sh\npython3 app.py\n```"
)
XSS_REPLY = (
    '<script>window.__xss=1</script>'
    '<img src=x onerror="window.__xss=2">'
    '<a href="javascript:window.__xss=3">click</a>'
    '[a link](javascript:window.__xss=4)'
)


@SKIP_BROWSER
class ChatRoundTripTestCase(BrowserCase):
    def reply_set(self, text):
        self.stack.upstream.response = _reply(text)

    def send(self, text="Hello from the dock"):
        self.page.fill("#chat-input", text)
        self.page.evaluate(
            "document.getElementById('chat-form').dispatchEvent("
            "new Event('submit', {cancelable: true}))")

    def send_modal(self, text):
        # the conversation modal is open, so its composer is the active one;
        # the inert background dock input silently refuses fill when a modal
        # dialog is up
        self.page.fill("#chat-input-modal", text)
        self.page.evaluate(
            "document.getElementById('chat-form-modal').dispatchEvent("
            "new Event('submit', {cancelable: true}))")

    def wait_reply(self, plain=False):
        sel = ("#chat-log .chat-msg--jarvis:not(.chat-typing)" if plain
               else "#chat-log .chat-msg--jarvis:not(.chat-typing) .chat-body")
        self.page.wait_for_selector(sel, timeout=15_000)
        return self.page.locator("#chat-log .chat-msg--jarvis:not(.chat-typing)").last

    # ------------------------------------------------------- round trips
    def test_dock_composer_opens_modal_and_renders_reply(self):
        self.send("Hello from the dock")
        self.page.wait_for_selector("#modal-chat[open]")
        reply = self.wait_reply()
        raw = reply.evaluate(RAW)
        self.assertIn("Hello! How can I help?", raw)
        self.assertIn("AI reply", reply.evaluate(RAW))
        self.assertIn("qwen3.8-27b", reply.evaluate(RAW))
        self.assertEqual(
            "Hello from the dock",
            self.page.locator("#chat-log .chat-msg--user").evaluate(RAW))
        self.assertEqual(0, self.page.locator("#chat-log .chat-typing").count())
        with sqlite3.connect(str(self.stack.db_path)) as conn:
            roles = [r for r, in conn.execute(
                "SELECT role FROM messages ORDER BY rowid").fetchall()]
        self.assertEqual(["user", "assistant"], roles)

    def test_markdown_structure_rendered(self):
        self.reply_set(MD_REPLY)
        self.send("show markdown")
        self.wait_reply()
        body = self.page.locator("#chat-log .chat-msg--jarvis .chat-body")
        self.assertIn("Plan", body.evaluate(RAW))
        self.assertEqual(2, body.locator("li").count())
        self.assertEqual("python3 app.py",
                         body.locator("pre code").first.evaluate(RAW))

    def test_script_and_event_handlers_stay_inert(self):
        self.reply_set(XSS_REPLY)
        self.send("attack reply")
        self.wait_reply()
        body = self.page.locator("#chat-log .chat-msg--jarvis .chat-body")
        self.assertEqual(0, body.locator("script").count())
        self.assertEqual(0, body.locator("img").count())
        self.assertEqual(0,
                         self.page.locator("#chat-log a[href^='javascript:']").count())
        self.page.wait_for_timeout(300)
        self.assertTrue(self.page.evaluate("typeof window.__xss === 'undefined'"))
        self.assertIn("javascript:", body.evaluate(RAW))  # shown literally, never a link

    # ------------------------------------------------------- input state
    def test_typing_indicator_and_disabled_composer_during_reply(self):
        self.stack.upstream.delay = 1.0
        self.send("slow reply")
        self.page.wait_for_selector("#chat-log .chat-typing")
        self.assertTrue(self.page.locator("#chat-input-modal").is_disabled())
        self.wait_reply()
        self.assertFalse(self.page.locator("#chat-input-modal").is_disabled())
        self.assertEqual(0, self.page.locator("#chat-log .chat-typing").count())

    def test_duplicate_submit_sends_one_request(self):
        self.stack.upstream.delay = 0.8
        self.open_chat()
        # one user action re-entering while pending (retyped submit) must
        # never produce a second provider request
        self.page.evaluate(
            "() => { const m = document.getElementById('chat-input-modal');"
            " m.value = 'once only';"
            " handleChatSubmit('once only', m);"
            " document.getElementById('chat-form-modal').dispatchEvent("
            " new Event('submit', {cancelable: true})); }")
        self.wait_reply()
        with self.stack.upstream.lock:
            self.assertEqual(1, len(self.stack.upstream.requests))

    def test_blank_message_send_nothing(self):
        self.send("   ")
        self.assertEqual(0, self.page.locator("#chat-log .chat-msg").count())
        self.assertEqual([], self.stack.db_messages())
        with self.stack.upstream.lock:
            self.assertEqual(0, len(self.stack.upstream.requests))

    # ------------------------------------------------------- failure paths
    def test_provider_error_renders_error_bubble(self):
        self.stack.upstream.response = (502, "bad gateway", "text/html")
        self.send("will fail")
        self.wait_reply(plain=True)
        self.assertIn("chat error", self.page.inner_text("#chat-log").lower())
        self.assertEqual("will fail",
                         self.page.inner_text("#chat-log .chat-msg--user"))
        self.assertFalse(self.page.locator("#chat-input-modal").is_disabled())

    def test_unreachable_provider_renders_error_path(self):
        self.stack.upstream.stop()
        self.send("nobody home")
        self.wait_reply(plain=True)
        self.assertFalse(self.page.locator("#chat-input-modal").is_disabled())

    def test_reasoning_only_reply_shows_placeholder(self):
        self.reply_set("<think>internal padding</think>")
        self.send("silent reply")
        self.wait_reply()
        body = self.page.locator("#chat-log .chat-msg--jarvis .chat-body")
        self.assertIn("reasoning", body.evaluate(RAW).lower())
        self.assertIn("chat-silent", body.get_attribute("class"))

    # ------------------------------------------------------- history
    def test_new_conversation_creates_second_and_history_lists_both(self):
        self.send("first conversation")
        self.wait_reply()
        first_id = self.page.evaluate("state.conversationId")
        self.assertTrue(first_id)
        self.page.evaluate("newConversation()")
        self.send_modal("second conversation")
        self.wait_reply()
        second_id = self.page.evaluate("state.conversationId")
        self.assertNotEqual(first_id, second_id)
        convs = [c["id"] for c in self.stack.conversations()]
        self.assertIn(first_id, convs)
        self.assertIn(second_id, convs)

    def test_reload_recovers_last_conversation(self):
        self.send("remember me")
        self.wait_reply()
        self.reload()
        rendered = self.page.inner_text("#chat-log")
        self.assertIn("remember me", rendered)
        self.assertIn("Hello! How can I help?", rendered)

    def test_reload_with_unreachable_messages_api_keeps_saved_conversation(self):
        self.send("offline soon")
        self.wait_reply()
        conv_id = self.page.evaluate("state.conversationId")
        self.page.route("**/api/conversations/**",
                        lambda route: route.abort("connectionrefused"))
        self.reload()
        self.page.wait_for_function(
            "() => document.getElementById('chat-log').textContent"
            ".includes('kept')", timeout=20_000)
        self.assertIn("kept", self.page.inner_text("#chat-log"))
        self.assertNotEqual(0, self.page.locator(
            "#chat-log .chat-retry").count())
        # the JS state stays empty on failure, but the saved id survives
        # in session storage so a later retry can still recover
        self.assertIsNone(self.page.evaluate("state.conversationId"))
        self.assertEqual(conv_id, self.page.evaluate(
            "sessionStorage.getItem('jarvis.conversationId')"))

    def test_misattribution_guard_switch_while_pending(self):
        self.stack.upstream.delay = 1.5
        self.send("first")
        self.wait_reply()
        first_id = self.page.evaluate("state.conversationId")
        self.page.evaluate("newConversation()")
        self.send_modal("pending-switch")     # conv 2, reply is in flight
        self.page.evaluate(f"selectConversation('{first_id}')")
        self.page.wait_for_function(
            "() => { const b = document.getElementById('toast-box');"
            " return b && b.textContent.includes('switched conversations');"
            "}")
        # conv 1's transcript holds exactly one reply; the conv 2 straggler
        # must not have appended a third bubble into this transcript
        replies = self.page.locator(
            "#chat-log .chat-msg--jarvis:not(.chat-typing)")
        self.assertEqual(1, replies.count())

    def test_chat_404_recovery_on_reload(self):
        self.send("will be deleted server side")
        self.wait_reply()
        conv_id = self.page.evaluate("state.conversationId")
        with sqlite3.connect(str(self.stack.db_path)) as conn:
            conn.execute("DELETE FROM messages WHERE conversation_id = ?",
                         (conv_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            conn.commit()
        self.reload()
        self.page.wait_for_function(
            "() => document.getElementById('chat-log').textContent"
            ".includes('no longer exists')", timeout=15_000)
        self.assertIsNone(self.page.evaluate("state.conversationId"))
