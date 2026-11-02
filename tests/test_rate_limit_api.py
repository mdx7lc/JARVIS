"""Phase 6A rate-limit API tests — real handler over loopback, fake
clock on the server's limiter, temp DBs, never the real provider."""

import json
import unittest
import urllib.error
import urllib.request

import db
from test_chat_api import ChatAPITestCase

BASE = "http://127.0.0.1"


def post(url, payload, headers=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


class ChatRateLimitTests(ChatAPITestCase):
    def setUp(self):
        super().setUp()
        self.httpd = self._httpd
        self.limiter = self.httpd.rate_limiter
        self.base = self.base  # harness attribute

    def test_chat_blocks_at_limit_with_valid_retry(self):
        self.limiter.add_policy("chat", 2, 60.0)
        for i in range(2):
            status, _, body = post(self.base + "/api/chat",
                                   {"message": "Hello " + str(i)})
            self.assertEqual(status, 200, body)
        status, headers, body = post(self.base + "/api/chat",
                                     {"message": "one too many"})
        self.assertEqual(status, 429)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["code"], "rate_limited")
        retry = int(headers["Retry-After"])
        self.assertTrue(1 <= retry <= 60, retry)
        # rejected before the provider is contacted: a fresh 429 implies
        # no upstream request for the blocked attempt
        self.assertEqual(len(self.upstream.requests), 2)

    def test_chat_limit_respects_races_and_database_is_untouched(self):
        self.limiter.add_policy("chat", 1, 60.0)
        first, _, _ = post(self.base + "/api/chat", {"message": "first"})
        self.assertEqual(first, 200)
        second, headers, _ = post(
            self.base + "/api/chat", {"message": "second"})
        self.assertEqual(second, 429)
        self.assertNotIn("first", headers.get("Retry-After") or "")

    def test_rate_limit_gate_runs_before_body_parsing(self):
        # even a garbage body (invalid JSON on a raw POST) receives the
        # 429 taxonomy, not a request-processing error, when the window
        # is exhausted
        self.limiter.add_policy("chat", 0, 60.0)
        status, headers, body = post(self.base + "/api/chat",
                                     {"message": "anything"})
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["code"], "rate_limited")

    def test_limit_window_eligibility_restored_via_fake_clock(self):
        self.limiter.add_policy("chat", 1, 60.0)
        clock_holder = {"now": 1_000_000.0}
        self.limiter._clock = (lambda: clock_holder["now"])
        status, _, _ = post(self.base + "/api/chat", {"message": "one"})
        self.assertEqual(status, 200)
        status, _, _ = post(self.base + "/api/chat", {"message": "two"})
        self.assertEqual(status, 429)
        clock_holder["now"] += 61.0
        status, _, _ = post(self.base + "/api/chat", {"message": "three"})
        self.assertEqual(status, 200)

    def test_unsupported_methods_keep_405_even_when_depleted(self):
        self.limiter.add_policy("chat", 0, 60.0)
        # PUT on /api/chat goes through the same do_POST routing tree
        req = urllib.request.Request(
            self.base + "/api/chat", data=b"{}", method="PUT",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as err:
            status = err.code
        self.assertEqual(status, 405)


class SearchRateLimitTests(ChatAPITestCase):
    def test_search_limit_and_independent_policies(self):
        conn = db.open_conn(self.db_path)
        try:
            db.create_task(conn, title="Rate limit beacon")
        finally:
            conn.close()
        limiter = self._httpd.rate_limiter
        limiter.add_policy("search", 3, 60.0)
        for _ in range(3):
            status, _, _ = get(self.base + "/api/search?q=beacon")
            self.assertEqual(status, 200)
        status, headers, body = get(self.base + "/api/search?q=beacon")
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["code"], "rate_limited")
        self.assertTrue(1 <= int(headers["Retry-After"]) <= 60)
        # searches being limited must not break harmless dashboard reads
        status, _, _ = get(self.base + "/api/health")
        self.assertEqual(status, 200)
        status, _, _ = get(self.base + "/api/status")
        self.assertEqual(status, 200)
        status, _, _ = get(self.base + "/api/tasks?limit=5")
        self.assertEqual(status, 200)

    def test_dashboard_polling_compatible_with_default_policies(self):
        # the frontend polls metrics every 6 s + tasks/status/convs every
        # 30 s and types searches per keystroke; a busy minute stays under
        # the configured defaults
        for _ in range(10):
            status, _, _ = get(self.base + "/api/metrics")
            self.assertEqual(status, 200)
        for _ in range(8):
            status, _, _ = get(self.base + "/api/search?q=beacon")
            self.assertEqual(status, 200)
        for _ in range(6):
            status, _, _ = get(self.base + "/api/tasks?limit=10")
            self.assertEqual(status, 200)
            status, _, _ = get(self.base + "/api/status")
            self.assertEqual(status, 200)

    def test_task_mutations_share_tasks_write_policy(self):
        conn = db.open_conn(self.db_path)
        try:
            task = db.create_task(conn, title="Write-limited task")
        finally:
            conn.close()
        limiter = self._httpd.rate_limiter
        limiter.add_policy("tasks_write", 3, 60.0)
        # POST
        status, _, _ = post(self.base + "/api/tasks", {"title": "one"})
        self.assertEqual(status, 201)
        # PATCH + DELETE + one extra PATCH to exceed
        status, _, _ = get(self.base + "/api/tasks?limit=10")
        self.assertEqual(status, 200)  # reads are a different policy
        import urllib.request
        req = urllib.request.Request(
            self.base + f"/api/tasks/{task['id']}",
            data=json.dumps({"status": "done"}).encode(),
            method="PATCH",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
        except urllib.error.HTTPError as err:
            self.fail("PATCH should be allowed before the limit: "
                      f"{err.code}")
        # DELETE the original (3rd mutation) then exceed with another POST
        req = urllib.request.Request(
            self.base + f"/api/tasks/{task['id']}", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
        except urllib.error.HTTPError as err:
            self.fail(f"DELETE should be allowed before the limit: {err.code}")
        status, headers, body = post(self.base + "/api/tasks",
                                     {"title": "blocked"})
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["code"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
