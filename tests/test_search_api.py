"""Phase 5 `GET /api/search` tests — temp DB only, no live calls."""

import json
import unittest
from urllib.parse import quote

import db
import server
from fake_upstream import FakeUpstream
from test_chat_api import ChatAPITestCase, get


def seed_task(conn, title, description="", day="today"):
    return db.create_task(conn, title=title, description=description,
                          day=day)


def seed_conv(conn, title, conv_id, messages=()):
    conn.execute(
        "INSERT INTO conversations(id, title) VALUES (?, ?)",
        (conv_id, title),
    )
    for role, content in messages:
        conn.execute(
            "INSERT INTO messages(id, conversation_id, role, content) "
            "VALUES (?, ?, ?, ?)",
            (db.new_id(), conv_id, role, content),
        )
    conn.commit()


class SearchAPITests(ChatAPITestCase):
    def search(self, query, limit=None):
        url = self.base + "/api/search?q=" + quote(query)
        if limit is not None:
            url += "&limit=" + str(limit)
        status, body = get(url)
        try:
            return status, json.loads(body)
        except ValueError:
            return status, None

    def seed(self, conv_id):
        conn = db.open_conn(self.db_path)
        try:
            seed_task(conn, "Ship cargo via turbolift",
                      "Move %dust_ crates to bay two")
            seed_task(conn, "Diagnostics sweep")
            seed_conv(conn, "Docking procedures", conv_id,
                      [("user", "Write about turbolift maintenance "
                                "schedules and cargo bays"),
                       ("assistant", "The turbolift grid keeps the "
                                     "cargo decks connected.")])
        finally:
            conn.close()

    def test_matches_tasks_messages_conversations(self):
        self.seed("0" * 32)
        status, payload = self.search("cargo")
        self.assertEqual(status, 200)
        self.assertEqual(payload["query"], "cargo")
        types = {r["type"] for r in payload["results"]}
        self.assertEqual(types, {"task", "message"})
        self.assertEqual(payload["count"], len(payload["results"]))
        self.assertIn("truncated", payload)
        task = next(r for r in payload["results"] if r["type"] == "task")
        self.assertEqual(task["title"], "Ship cargo via turbolift")
        self.assertEqual(task["day"], "today")
        self.assertIn("status", task)
        msg = next(r for r in payload["results"] if r["type"] == "message")
        self.assertIn("cargo", msg["snippet"].lower())
        self.assertEqual(msg["conversation_id"], "0" * 32)
        self.assertEqual(msg["conversation_title"], "Docking procedures")

    def test_conversation_title_match(self):
        self.seed("1" * 32)
        status, payload = self.search("docking")
        self.assertEqual(status, 200)
        convs = [r for r in payload["results"]
                 if r["type"] == "conversation"]
        self.assertEqual(len(convs), 1)
        self.assertEqual(convs[0]["id"], "1" * 32)
        self.assertEqual(convs[0]["title"], "Docking procedures")

    def test_wildcards_are_literal(self):
        self.seed("0" * 32)
        status, payload = self.search("%dust_")
        self.assertEqual(status, 200)
        tasks = [r for r in payload["results"] if r["type"] == "task"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "Ship cargo via turbolift")
        self.assertNotIn("Diagnostics sweep",
                         [r["title"] for r in tasks])

    def test_snippet_bounded_and_contextual(self):
        self.seed("0" * 32)
        _, payload = self.search("turbolift")
        msgs = [r for r in payload["results"] if r["type"] == "message"]
        for m in msgs:
            self.assertLessEqual(len(m["snippet"]), 100)

    def test_no_results_is_empty_truthful(self):
        self.seed("0" * 32)
        status, payload = self.search("kessel")
        self.assertEqual(status, 200)
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["count"], 0)
        self.assertFalse(payload["truncated"])

    def test_case_insensitive_literal_match(self):
        self.seed("0" * 32)
        status, payload = self.search("CARGO")
        self.assertEqual(status, 200)
        self.assertTrue(any(r["type"] == "task" for r in payload["results"]))

    def test_minimum_query_length(self):
        status, payload = self.search("a")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_maximum_query_length(self):
        status, payload = self.search("y" * 121)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_oversized_query_is_rejected_not_truncated(self):
        self.seed("0" * 32)
        _, payload = self.search("cargo " + "y" * 121)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_invalid_limit_rejected(self):
        for bad in ("0", "-3", "101", "abc", "3.5"):
            status, payload = self.search("cargo", limit=bad)
            self.assertEqual(status, 400, bad)
            self.assertEqual(payload["error"]["code"],
                             "invalid_request", bad)

    def test_limit_bounds_results_count(self):
        conn = db.open_conn(self.db_path)
        try:
            for i in range(5):
                seed_task(conn, "Errand alpha-%02d" % i)
            conn.commit()
        finally:
            conn.close()
        status, payload = self.search("alpha", limit=2)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["results"]), 2)
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["category_truncated"]["tasks"])

    def test_truncation_flags_are_false_under_limit(self):
        self.seed("0" * 32)
        status, payload = self.search("cargo")
        self.assertFalse(payload["truncated"])
        for flag in payload["category_truncated"].values():
            self.assertFalse(flag)

    def test_rejects_post(self):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.base + "/api/search",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as err:
            status = err.code
            payload = json.loads(err.read())
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

    def test_rejects_cross_origin(self):
        res2 = self.cross_origin()
        self.assertEqual(res2, 403)

    def cross_origin(self):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.base + "/api/search?q=cargo",
            headers={"Origin": "http://evil.example"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            return err.code

    def test_task_after_patch_appears_with_new_text(self):
        self.seed("0" * 32)
        conn = db.open_conn(self.db_path)
        try:
            tid = conn.execute(
                "SELECT id FROM tasks LIMIT 1").fetchone()[0]
            db.update_task(conn, tid, {"description": "relay relay relay"})
        finally:
            conn.close()
        status, payload = self.search("relay")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["type"], "task")

    def test_delete_removes_from_search(self):
        self.seed("0" * 32)
        conn = db.open_conn(self.db_path)
        try:
            tid = conn.execute(
                "SELECT id FROM tasks WHERE title='Ship cargo via turbolift' "
                "LIMIT 1").fetchone()[0]
            db.delete_task(conn, tid)
            remaining = conn.execute(
                "SELECT count(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(remaining, 1)  # only "Diagnostics sweep" remains
        _, payload = self.search("turbolift")
        self.assertNotIn(
            "task", {r["type"] for r in payload["results"]})

    def test_empty_query_param_missing(self):
        status, body = get(self.base + "/api/search")
        self.assertEqual(status, 400)


class SearchFormattingTests(unittest.TestCase):
    def test_like_pattern_escapes_and_lowercases(self):
        self.assertEqual(db.like_pattern("Cargo"), "%cargo%")
        self.assertEqual(db.like_pattern("50%dust_\\n"),
                         "%50\\%dust\\_\\\\n%")

    def test_clip_bounds_plain_text(self):
        clip = server._Handler._clip
        out = clip("x" * 500, "q")
        self.assertLessEqual(len(out), server.SEARCH_SNIPPET_CHARS)
        long_text = "A" * 200 + "needlebulb" + "B" * 200
        out = clip(long_text, "needle")
        self.assertLessEqual(len(out), server.SEARCH_SNIPPET_CHARS + 1)
        self.assertIn("needle", out)
