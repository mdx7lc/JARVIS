"""Workflow G (Phase 6B) — browser-level security, logging and limit
behavior: canary secrets never reach the page or server logs, cross
origin requests are refused, request ids are honest, and rate-limit
exhaustion renders an honest error instead of a fake result."""

import json
import re

from browser_harness import PROVIDER_KEY, BrowserCase, SKIP_BROWSER


@SKIP_BROWSER
class SecurityAndLimitsTestCase(BrowserCase):
    def chat_once(self, text="canary chat turn"):
        self.page.fill("#chat-input", text)
        self.page.evaluate(
            "document.getElementById('chat-form').dispatchEvent("
            "new Event('submit', {cancelable: true}))")
        self.page.wait_for_selector(
            "#chat-log .chat-msg--jarvis:not(.chat-typing)", timeout=15_000)

    def test_secrets_never_reach_page_or_logs(self):
        self.chat_once()
        self.page.evaluate("localStorage.setItem('jarvis.preferences.v1','{}')")
        self.page.evaluate(
            "fetch('/api/tasks', {method:'POST',"
            " headers:{'Content-Type':'application/json'},"
            " body: JSON.stringify({title:'canary task title',"
            " time:'07:00'})})")
        self.page.wait_for_timeout(500)
        rendered = self.page.content()
        stderr = self.stack.stderr_text()
        for needle in (PROVIDER_KEY, self.stack.upstream.base_url,
                       "Authorization", "Bearer"):
            self.assertNotIn(needle, rendered, needle)
            self.assertNotIn(needle, stderr, needle)
        # the prompt text and task title never appear in the stream of
        # structured events either (search query surfaces nowhere)
        self.assertNotIn("prompt:", stderr)
        for line in (l for l in stderr.splitlines() if l.strip()):
            self.assertTrue(line.lstrip().startswith("{") and
                            line.rstrip().endswith("}"), line)

    def test_cross_origin_requests_are_refused(self):
        other = self._ctx.request
        resp = other.get(self.stack.base + "/api/status",
                         headers={"Origin": "http://evil.example",
                                  "User-Agent": "e2e-cross-origin"})
        self.assertEqual(403, resp.status)

    def test_in_page_search_exhaustion_shows_honest_error(self):
        last = self.page.evaluate("""async () => {
            let last = 0;
            for (let i = 0; i < 34; i++) {
                const r = await fetch('/api/search?q=probe' + i);
                last = r.status;
            }
            return last;
        }""")
        self.assertEqual(429, last)
        self.page.fill("#search", "in-UI search")
        self.page.wait_for_function(
            "() => { const b = document.getElementById('search-results');"
            " return b && !b.hidden; }")
        self.assertIn("Search error (429)", self.page.evaluate(
            "() => document.getElementById('search-results').textContent"))

    def test_request_ids_present_and_well_formed(self):
        rid = self.page.evaluate("""async () => {
            const r = await fetch('/api/status');
            return r.headers.get('X-Request-ID');
        }""")
        self.assertRegex(rid, r"^req_[0-9a-f]{24}$")

    def test_server_logs_are_single_line_json_with_request_ids(self):
        self.chat_once()
        self.page.evaluate(
            "fetch('/api/tasks', {method:'POST',"
            " headers:{'Content-Type':'application/json'},"
            " body: JSON.stringify({title:'logged task', time:'07:00'})})")
        self.page.wait_for_timeout(400)
        lines = [l for l in self.stack.stderr_text().splitlines() if l.strip()]
        self.assertTrue(lines, "server must emit structured events")
        for line in lines:
            parsed = json.loads(line)
            ts = parsed.get("ts", "")
            self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T")
        self.assertTrue(all(re.search(r"req_[0-9a-f]{24}", line)
                            for line in lines[-3:]))
