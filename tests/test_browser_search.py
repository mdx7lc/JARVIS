"""Workflow C (Phase 6B) — dashboard search: panel highlight + server
results for tasks/conversations/messages, keyboard handling, honest
empty states. Driven in the browser against the real server."""

from browser_harness import BrowserCase, SKIP_BROWSER

RAW = "el => el.textContent"


@SKIP_BROWSER
class SearchWorkflowTestCase(BrowserCase):
    def create_task(self, title, time="07:00"):
        self.page.evaluate(
            "t => fetch('/api/tasks', {method:'POST',"
            " headers:{'Content-Type':'application/json'},"
            " body: JSON.stringify(t)})",
            {"title": title, "time": time})

    def send_dock(self, text):
        self.page.fill("#chat-input", text)
        self.page.evaluate(
            "document.getElementById('chat-form').dispatchEvent("
            "new Event('submit', {cancelable: true}))")

    def type_query(self, text):
        self.page.fill("#search", text)

    def results(self):
        return self.page.locator("#search-results .search-result")

    def wait_results(self, minimum=1, timeout=15_000):
        self.page.wait_for_function(
            "() => { const b = document.getElementById('search-results');"
            " return b && !b.hidden &&"
            " b.querySelectorAll('.search-result').length >= "
            + str(minimum) + "; }", timeout=timeout)

    def toast_text(self):
        return self.page.evaluate(
            "() => { const b = document.getElementById('toast-box');"
            " return b ? b.textContent : ''; }")

    # ------------------------------------------------------- task results
    def test_task_found_and_click_navigates(self):
        self.create_task("Alpha recon")
        self.create_task("Bravo recon strike")
        self.type_query("Bravo")
        self.wait_results()
        item = self.results().first
        self.assertIn("task",
                      item.locator(".search-result-kind").evaluate(RAW).lower())
        self.assertIn("Bravo recon strike",
                      item.locator(".search-result-title").evaluate(RAW))
        item.click()
        self.page.wait_for_function(
            "() => state.day === 'today' && !!document.querySelector("
            "'#timeline [data-task-id]')")
        self.assertIn("Task found on", self.toast_text())
        self.assertEqual("", self.page.evaluate(
            "document.getElementById('search').value"))

    def test_enter_opens_first_matching_result(self):
        self.create_task("Solo survivor mission")
        self.type_query("Solo survivor")
        self.wait_results()
        self.page.keyboard.press("Enter")
        self.page.wait_for_function(
            "() => !!document.querySelector('#timeline [data-task-id]')")
        self.assertIn("Task found on", self.toast_text())

    # ------------------------------------------------------- persisted chats
    def test_message_and_conversation_results_open_the_chat(self):
        self.send_dock("quantum entanglement briefing")
        self.page.wait_for_selector(
            "#chat-log .chat-msg--jarvis:not(.chat-typing) .chat-body")
        conv_id = self.page.evaluate("state.conversationId")
        self.page.evaluate("closeModal('modal-chat')")
        self.type_query("entanglement")
        self.wait_results(2)
        kinds = self.results().locator(".search-result-kind").evaluate_all(
            "els => els.map(el => el.textContent)")
        self.assertTrue(any(k.lower() in ("chat", "message") for k in kinds),
                        f"expected chat/message kinds, got {kinds}")
        transcript = self.page.inner_text("#chat-log")
        self.assertIn("quantum entanglement briefing", transcript)
        self.assertEqual(conv_id, self.page.evaluate("state.conversationId"))

    # ------------------------------------------------------- safety + limits
    def test_special_characters_stay_safe_and_visible(self):
        self.create_task("O'Brien & <b>bold</b>")
        self.type_query("O'Brien")
        self.wait_results()
        title = self.results().first.locator(
            ".search-result-title").evaluate(RAW)
        self.assertEqual("O'Brien & <b>bold</b>", title)
        self.assertEqual(0, self.page.locator("#search-results b").count())

    def test_short_queries_skip_server_and_escape_clears(self):
        calls = []

        def track(route):
            calls.append(route.request.url)
            route.continue_()

        self.page.route("**/api/search*", track)
        self.type_query("a")
        self.page.wait_for_timeout(600)
        self.assertEqual([], calls)
        self.assertTrue(self.page.evaluate(
            "document.getElementById('search-results').hidden"))
        self.page.keyboard.press("Escape")
        self.assertEqual("", self.page.evaluate(
            "document.getElementById('search').value"))
        self.assertEqual("body", self.page.evaluate(
            "document.activeElement.tagName").lower())

    def test_no_matches_head_is_honest(self):
        self.type_query("zzqqxx")
        self.page.wait_for_function(
            "() => { const b = document.getElementById('search-results');"
            " return b && !b.hidden; }")
        self.assertIn("No server records match",
                      self.page.evaluate(
                          "(document.querySelector('.search-results-head')"
                          " || {}).textContent || ''"))

    # ------------------------------------------------------- keyboard flow
    def test_slash_focuses_search(self):
        self.page.keyboard.press("/")
        self.assertTrue(self.page.evaluate(
            "document.activeElement === document.getElementById('search')"))
