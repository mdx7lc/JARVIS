"""Phase 3B conversation read-endpoint tests (list + messages + workflow).

No live inference: every assistant reply comes from the fake upstream.
"""

import json
import sqlite3
import unittest
import urllib.error
import urllib.request

from test_chat_api import ChatAPITestCase, get, post

KEY = "test-key-123"
GOOD_ID = "0123456789abcdef0123456789abcdef"


def seed_conversation(db_path, conv_id, n=2, updated_at=None):
    """Insert a conversation directly (bypasses the provider)."""
    tag = conv_id[:4]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            conn.execute(
                "INSERT INTO conversations(id, title) VALUES (?, ?)",
                (conv_id, "seeded"),
            )
            for i in range(n):
                conn.execute(
                    "INSERT INTO messages(id, conversation_id, role,"
                    " content, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        (f"m{conv_id[:8]}{i}").ljust(32, "x"),
                        conv_id,
                        "user" if i % 2 == 0 else "assistant",
                        f"{tag}-msg{i}",
                        f"2026-01-0{1 + i}T00:00:00.000Z",
                    ),
                )
            if updated_at is not None:
                conn.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?",
                    (updated_at, conv_id),
                )
    finally:
        conn.close()
    return conv_id


def fetch_conv_list(base, limit=None):
    url = base + "/api/conversations"
    if limit is not None:
        url += f"?limit={limit}"
    status, body = get(url)
    return status, json.loads(body)


class ConversationListTests(ChatAPITestCase):
    def list_url(self, limit=None):
        url = self.base + "/api/conversations"
        if limit is not None:
            url += f"?limit={limit}"
        return url

    def fetch_list(self, limit=None):
        return fetch_conv_list(self.base, limit)

    def test_empty_database_returns_empty_list(self):
        status, payload = self.fetch_list()
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"conversations": [], "count": 0,
                                   "truncated": False})

    def test_newest_first_ordering(self):
        ids = []
        for msg in ("first", "second", "third"):
            _s, payload = self.chat({"message": msg})
            ids.append(payload["conversation_id"])
        status, payload = self.fetch_list()
        self.assertEqual(status, 200)
        got = [c["id"] for c in payload["conversations"]]
        self.assertEqual(got, list(reversed(ids)))

    def test_stable_tie_break_on_equal_timestamps(self):
        seed_conversation(self.db_path, "x" * 32, updated_at="t")
        seed_conversation(self.db_path, GOOD_ID, updated_at="t")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE conversations SET updated_at='2026-01-01T00:00:00.000Z' "
            "WHERE id IN (?, ?)", ("x" * 32, GOOD_ID)
        )
        conn.commit()
        conn.close()
        conv_y = seed_conversation(self.db_path, "y" * 32, updated_at="t")
        status, payload = self.fetch_list()
        got = [c["id"] for c in payload["conversations"]]
        # y keeps a distinct older/newer marker; x and GOOD_ID share an exact
        # updated_at -> deterministic id DESC tie-break
        self.assertEqual(got, sorted([conv_y, "x" * 32, GOOD_ID],
                                     reverse=True))

    def test_limit_bound_and_truncation_flag(self):
        for msg in ("a", "b", "c", "d"):
            self.chat({"message": msg})
        status, payload = self.fetch_list(limit=2)
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 2)
        self.assertTrue(payload["truncated"])
        status, payload = self.fetch_list()  # default
        self.assertFalse(payload["truncated"])
        self.assertLessEqual(payload["count"], 50)

    def test_invalid_limit_rejected(self):
        for bad in ("0", "-1", "101", "abc", "1.5"):
            status, payload = self.fetch_list(limit=bad)
            self.assertEqual(status, 400, f"limit={bad}")
            self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_metadata_counts_and_preview(self):
        _s, first = self.chat({"message": "hello from the commander"})
        conv_id = first["conversation_id"]
        self.chat({"message": "follow-up", "conversation_id": conv_id})
        status, payload = self.fetch_list()
        self.assertEqual(status, 200)
        entry = payload["conversations"][0]
        self.assertEqual(entry["id"], conv_id)
        self.assertEqual(entry["message_count"], 4)
        self.assertEqual(entry["title"], "hello from the commander")
        self.assertEqual(entry["preview"], "follow-up")
        self.assertTrue(entry["created_at"])
        self.assertTrue(entry["updated_at"])

    def test_preview_isolated_between_conversations(self):
        self.chat({"message": "SECRET-ALPHA-MARKER"})
        self.chat({"message": "SECRET-BETA-MARKER"})
        _status, payload = self.fetch_list()
        previews = json.dumps(payload)
        self.assertIn("SECRET-BETA-MARKER", previews)
        # newest-first list is bounded to the newest conversation preview
        self.assertEqual(payload["conversations"][0]["preview"],
                         "SECRET-BETA-MARKER")

    def test_repeated_calls_do_not_mutate(self):
        self.chat()
        before = self.db_counts()
        for _ in range(3):
            self.fetch_list()
        self.assertEqual(self.db_counts(), before)

    def test_post_to_list_is_405(self):
        status, body = post(self.base + "/api/conversations", {"x": 1})
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body)["error"]["code"],
                         "method_not_allowed")


class ConversationMessagesTests(ChatAPITestCase):
    def fetch_list(self, limit=None):
        return fetch_conv_list(self.base, limit)

    def msg_url(self, conv_id, window=None):
        url = f"{self.base}/api/conversations/{conv_id}/messages"
        if window is not None:
            url += f"?window={window}"
        return url

    def fetch_messages(self, conv_id, window=None):
        status, body = get(self.msg_url(conv_id, window))
        return status, json.loads(body)

    def test_full_workflow_via_fake_upstream(self):
        # 1-2. first message -> reply persisted
        status, first = self.chat({"message": "ALPHA-START"})
        self.assertEqual(status, 200)
        conv_id = first["conversation_id"]
        # 3. conversation appears in list
        status, listing = self.fetch_list()
        self.assertEqual(status, 200)
        self.assertEqual([c["id"] for c in listing["conversations"]],
                         [conv_id])
        # 4. transcript contains both messages
        status, transcript = self.fetch_messages(conv_id)
        self.assertEqual(status, 200)
        self.assertEqual(
            [(m["role"], m["content"]) for m in transcript["messages"]],
            [("user", "ALPHA-START"), ("assistant", "Hello! How can I help?")],
        )
        self.assertFalse(transcript["older_omitted"])
        # 5. simulated page reload: stateless re-read of the same id
        status, reloaded = self.fetch_messages(conv_id)
        self.assertEqual(status, 200)
        self.assertEqual(reloaded["conversation_id"], conv_id)
        self.assertEqual(len(reloaded["messages"]), 2)
        # 6. continuity: continue the same conversation
        status, second = self.chat(
            {"message": "CONTINUATION", "conversation_id": conv_id}
        )
        self.assertEqual(status, 200)
        self.assertEqual(second["conversation_id"], conv_id)
        _status, transcript = self.fetch_messages(conv_id)
        self.assertEqual(
            [m["content"] for m in transcript["messages"]][-2:],
            ["CONTINUATION", "Hello! How can I help?"],
        )
        # 7-8. new conversation: fresh id, distinct from the old one
        status, fresh = self.chat({"message": "BRAND-NEW"})
        self.assertEqual(status, 200)
        self.assertNotEqual(fresh["conversation_id"], conv_id)
        # 9. old conversation still readable
        status, old = self.fetch_messages(conv_id)
        self.assertEqual(status, 200)
        self.assertIn("ALPHA-START", [m["content"] for m in old["messages"]])
        # 10. no mixing
        self.assertNotIn("BRAND-NEW", [m["content"] for m in old["messages"]])
        status, fresh_t = self.fetch_messages(fresh["conversation_id"])
        self.assertEqual(
            [m["content"] for m in fresh_t["messages"]],
            ["BRAND-NEW", "Hello! How can I help?"],
        )
        self.assertNotIn("ALPHA-START",
                         [m["content"] for m in fresh_t["messages"]])

    def test_bounded_window_reports_omitted_history(self):
        conn = db_conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO conversations(id, title) VALUES (?, ?)",
                    (GOOD_ID, "seeded"),
                )
                for i in range(30):
                    conn.execute(
                        "INSERT INTO messages(id, conversation_id, role,"
                        " content, created_at) VALUES (?, ?, ?, ?, ?)",
                        (f"w{i:028d}", GOOD_ID, "user", f"u{i:02d}",
                         f"2026-02-01T00:00:{i:02d}.000Z"),
                    )
        finally:
            conn.close()
        status, payload = self.fetch_messages(GOOD_ID, window=5)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["messages"]), 5)
        self.assertEqual(payload["messages_total"], 30)
        self.assertTrue(payload["older_omitted"])
        # chronological trailing window: first shown is u25, last is u29
        self.assertEqual(payload["messages"][0]["content"], "u25")
        self.assertEqual(payload["messages"][-1]["content"], "u29")

    def test_unknown_conversation_is_404(self):
        status, payload = self.fetch_messages(GOOD_ID)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "conversation_not_found")

    def test_known_empty_conversation(self):
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO conversations(id, title) VALUES (?, ?)",
                (GOOD_ID, "empty"),
            )
        conn.close()
        status, payload = self.fetch_messages(GOOD_ID)
        self.assertEqual(status, 200)
        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["messages_total"], 0)
        self.assertFalse(payload["older_omitted"])

    def test_malformed_ids_rejected(self):
        for bad in ("", "short", "XYZ" + "0" * 29, "g" * 32, "0" * 31,
                    "0" * 33, "deadbeef"):
            status, payload = self.fetch_messages(bad)
            self.assertEqual(status, 400, f"id={bad!r}")
            self.assertEqual(payload["error"]["code"], "invalid_conversation")

    def test_unknown_conversation_path_shape_is_404(self):
        status, _b = get(self.base + "/api/conversations/" + GOOD_ID)
        self.assertEqual(status, 404)
        status, _b = get(self.base + "/api/conversations/" + GOOD_ID
                         + "/messages/extra")
        self.assertEqual(status, 404)

    def test_invalid_window_rejected(self):
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO conversations(id, title) VALUES (?, ?)",
                (GOOD_ID, "seeded"),
            )
        conn.close()
        for bad in ("0", "-3", "501", "xyz"):
            status, payload = self.fetch_messages(GOOD_ID, window=bad)
            self.assertEqual(status, 400, f"window={bad}")
            self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_methods_other_than_get_rejected(self):
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO conversations(id, title) VALUES (?, ?)",
                (GOOD_ID, "seeded"),
            )
        conn.close()
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            req = urllib.request.Request(
                self.msg_url(GOOD_ID), data=b"{}", method=method,
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail(f"{method} should be rejected")
            except urllib.error.HTTPError as err:
                self.assertEqual(err.code, 405)

    def test_cross_origin_rejected_on_read_endpoints(self):
        for url in (self.base + "/api/conversations",
                    self.msg_url(GOOD_ID)):
            req = urllib.request.Request(url, headers={
                "Origin": "http://evil.example",
            })
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail(f"cross-origin read should be rejected: {url}")
            except urllib.error.HTTPError as err:
                self.assertEqual(err.code, 403)

    def test_no_secrets_in_any_response(self):
        self.chat({"message": "probe"})
        for url in (self.base + "/api/conversations?limit=100",
                    self.msg_url("deadbeef" * 4),
                    self.base + "/api/conversations?limit=99999",
                    self.msg_url(GOOD_ID)):
            _status, body = get(url)
            self.assertNotIn(KEY, body.decode("utf-8", "replace"))
            self.assertNotIn("test-key", body.decode("utf-8", "replace"))

    def test_error_responses_do_not_leak_internals(self):
        for url in (self.base + "/api/conversations?limit=abc",
                    self.msg_url("malformed-id")):
            _status, body = get(url)
            text = body.decode("utf-8", "replace")
            self.assertNotIn("Traceback", text)
            self.assertNotIn("/home/", text)
            self.assertNotIn("jarvis.db", text)


if __name__ == "__main__":
    unittest.main()
