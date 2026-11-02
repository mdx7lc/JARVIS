"""Phase 8A tests — reliable brain: model routing, bounded fallback,
tool-call proposal representation, fallback observability.

Runs the real server against the scripted fake upstream. Temp DB only;
no live AI inference; secrets never asserted-visible.
"""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
import unittest.mock
import urllib.request
from pathlib import Path

import config
import provider
from fake_upstream import OK_RESPONSE, FakeUpstream
from test_chat_api import ChatAPITestCase, KEY, MODEL, get, post

FALLBACK = "deepseek-4.1-flash-test"

UPSTREAM_ERROR = {"error": {"message": "boom"}}


def ok_response(model=OK_RESPONSE.get("model")):
    resp = json.loads(json.dumps(OK_RESPONSE))
    resp["model"] = model
    return resp


def empty_reply_response():
    resp = ok_response()
    resp["choices"][0]["message"]["content"] = ""
    return resp


class FallbackTestCase(ChatAPITestCase):
    """Base class with the fallback policy configured."""

    extra_env = {"JARVIS_FALLBACK_MODEL": FALLBACK}

    def restart(self, timeout=None):
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": KEY,
            "JARVIS_FALLBACK_MODEL": FALLBACK,
        }
        if timeout is not None:
            env["JARVIS_TIMEOUT_SECONDS"] = str(timeout)
        self.stop_jarvis()
        self.start_jarvis(config.load(environ=env))

    def upstream_chat_calls(self):
        with self.upstream.lock:
            return [
                req for req in self.upstream.requests
                if req["path"].startswith("/v1/chat")
            ]

    def upstream_models_seen(self):
        return [
            (req["body"] or {}).get("model")
            for req in self.upstream_chat_calls()
        ]


class FallbackSuccessTests(FallbackTestCase):
    def test_concurrent_override_cannot_split_a_request_lifecycle(self):
        # While the chat request is in flight (stubbed provider call),
        # a concurrent settings PATCH lands. The lifecycle must keep the
        # policy it resolved at request start: one change can never
        # re-resolve this request or mix primary/fallback generations.
        original = provider.complete_with_fallback
        override_row = {}

        def stub_completing_then_override_lands(messages, **kwargs):
            override_row.update(kwargs)   # policy resolved at start
            conn = sqlite3.connect(str(self.db_path))
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO meta (key, value) "
                        "VALUES ('model_override', ?)",
                        ("mid-request-override-model",),
                    )
            finally:
                conn.close()
            return {
                "reply": "stub reply",
                "model": kwargs["primary_model"],
                "fallback_used": False,
                "attempts": [],
            }

        with unittest.mock.patch.object(
                provider, "complete_with_fallback",
                stub_completing_then_override_lands):
            status, body = post(
                self.base + "/api/chat",
                {"message": "hello", "conversation_id": None})
        self.assertEqual(self.upstream_models_seen(), [])
        chat_body = json.loads(body)
        self.assertEqual(chat_body["model"], MODEL)
        self.assertEqual(override_row["primary_model"], MODEL)
        self.assertEqual(override_row["fallback_model"], FALLBACK)
        # override now applied — must not affect the lifecycle above,
        # and must apply from the NEXT request on
        status, raw = get(self.base + "/api/settings/model")
        settings = json.loads(raw)
        self.assertEqual(settings["source"], "runtime_override")
        self.assertEqual(
            settings["selected"], "mid-request-override-model")
    def test_primary_success_makes_single_call(self):
        status, payload = self.chat()
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], MODEL)
        self.assertEqual(payload["reply"], "Hello! How can I help?")
        self.assertEqual(len(self.upstream_chat_calls()), 1)
        users, assistants, _ = self.db_counts()
        self.assertEqual((users, assistants), (1, 1))

    def test_fallback_after_primary_timeout(self):
        self.restart(timeout=1)
        self.upstream.delay_queue = [2.0, 0.0]
        status, payload = self.chat({"message": "slow primary"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], FALLBACK)
        self.assertEqual(payload["reply"], "Hello! How can I help?")
        # Exactly two attempts: primary then the one allowed fallback,
        # each reusing the same history (no duplicated side effects).
        self.assertEqual(self.upstream_models_seen(), [MODEL, FALLBACK])
        conv = sqlite3.connect(self.db_path)
        rows = conv.execute(
            "SELECT role FROM messages WHERE conversation_id=?",
            (payload["conversation_id"],),
        ).fetchall()
        conv.close()
        self.assertEqual([r[0] for r in rows], ["user", "assistant"])
        users, assistants, _ = self.db_counts()
        self.assertEqual((users, assistants), (1, 1))

    def test_fallback_after_primary_upstream_error(self):
        self.upstream.chat_responses = [
            (500, UPSTREAM_ERROR, "application/json"),
            (200, ok_response(), "application/json"),
        ]
        status, payload = self.chat()
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], FALLBACK)
        self.assertEqual(self.upstream_models_seen(), [MODEL, FALLBACK])

    def test_fallback_after_malformed_json(self):
        self.upstream.chat_responses = [
            (200, "this is not json", "application/json"),
            (200, ok_response(), "application/json"),
        ]
        status, payload = self.chat()
        self.assertEqual(status, 200)
        self.assertEqual(payload["model"], FALLBACK)

    def test_fallback_after_rate_limit_then_empty_reply(self):
        # Even the fallback is only allowed ONE attempt: the rate-limit
        # triggers the single fallback, and a subsequent empty reply on
        # the fallback surfaces honestly (no unbounded retry chain).
        self.upstream.chat_responses = [
            (429, UPSTREAM_ERROR, "application/json"),
            (200, empty_reply_response(), "application/json"),
            (200, ok_response(), "application/json"),
        ]
        status, payload = self.chat()
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "empty_response")
        self.assertEqual(
            payload["attempts"],
            [
                {"model": MODEL, "code": "rate_limited"},
                {"model": FALLBACK, "code": "empty_response"},
            ],
        )
        self.assertEqual(len(self.upstream_chat_calls()), 2)

    def test_fallback_history_is_reused_not_replayed_from_client(self):
        self.upstream.chat_responses = [
            (500, UPSTREAM_ERROR, "application/json"),
            (200, ok_response(), "application/json"),
        ]
        status, payload = self.chat({"message": "remember this"})
        self.assertEqual(status, 200)
        first, second = (
            req["body"] for req in self.upstream_chat_calls()
        )
        self.assertEqual(first["messages"], second["messages"])
        self.assertEqual(first["messages"][-1]["content"], "remember this")
        self.assertEqual(second["model"], FALLBACK)


class FallbackFailureTests(FallbackTestCase):
    def test_both_attempts_fail_is_honest_structured_failure(self):
        self.upstream.chat_responses = [
            (500, UPSTREAM_ERROR, "application/json"),
            (500, UPSTREAM_ERROR, "application/json"),
        ]
        status, payload = self.chat()
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "upstream_error")
        self.assertEqual(
            payload["attempts"],
            [
                {"model": MODEL, "code": "upstream_error"},
                {"model": FALLBACK, "code": "upstream_error"},
            ],
        )
        self.assertTrue(payload["conversation_id"])
        users, assistants, _ = self.db_counts()
        self.assertEqual((users, assistants), (1, 0))

    def test_fallback_also_fails_is_bounded(self):
        self.upstream.chat_responses = [
            (429, UPSTREAM_ERROR, "application/json"),
            (429, UPSTREAM_ERROR, "application/json"),
        ]
        status, payload = self.chat()
        self.assertEqual(status, 429)
        self.assertEqual(payload["error"]["code"], "rate_limited")
        self.assertEqual(len(payload["attempts"]), 2)
        self.assertEqual(len(self.upstream_chat_calls()), 2)

    def test_fallback_also_times_out_is_bounded(self):
        self.restart(timeout=1)
        self.upstream.delay_queue = [2.0, 2.0]
        status, payload = self.chat()
        self.assertEqual(status, 504)
        self.assertEqual(payload["error"]["code"], "provider_timeout")
        self.assertEqual(
            payload["attempts"],
            [
                {"model": MODEL, "code": "provider_timeout"},
                {"model": FALLBACK, "code": "provider_timeout"},
            ],
        )
        self.assertEqual(len(self.upstream_chat_calls()), 2)

    def test_auth_failure_is_not_retried_on_fallback(self):
        self.upstream.chat_responses = [
            (401, UPSTREAM_ERROR, "application/json"),
            (200, ok_response(), "application/json"),
        ]
        status, payload = self.chat()
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "auth_failed")
        # Gateway-level failure: the fallback cannot help, one call only.
        self.assertEqual(len(self.upstream_chat_calls()), 1)
        self.assertEqual(
            payload["attempts"],
            [{"model": MODEL, "code": "auth_failed"}],
        )
        self.assertNotIn(KEY, json.dumps(payload))


class FallbackPrecedenceTests(FallbackTestCase):
    def test_runtime_override_disables_fallback(self):
        self.upstream.chat_responses = [
            (500, UPSTREAM_ERROR, "application/json"),
            (200, ok_response(), "application/json"),
        ]
        status, _ = post(
            self.base + "/api/settings/model",
            {"model": "auto"},
            method="PATCH",
        )
        self.assertEqual(status, 200)
        status, payload = self.chat()
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "upstream_error")
        # Explicit pin is honored strictly: errors surface honestly
        # instead of silently substituting the environment fallback.
        self.assertEqual(self.upstream_models_seen(), ["auto"])
        self.assertEqual(
            payload["attempts"],
            [{"model": "auto", "code": "upstream_error"}],
        )

    def test_override_persistence_survives_restart_with_fallback_env(self):
        _status, _ = post(
            self.base + "/api/settings/model",
            {"model": "auto"},
            method="PATCH",
        )
        self.restart()
        status, payload = get(self.base + "/api/settings/model")
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertEqual(body["selected"], "auto")
        self.assertEqual(body["source"], "runtime_override")
        self.assertEqual(body["fallback"], FALLBACK)

    def test_corrupt_saved_override_recovers_to_primary_plus_fallback(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("model_override", "bad model id with spaces"),
        )
        conn.commit()
        conn.close()
        self.upstream.chat_responses = [
            (500, UPSTREAM_ERROR, "application/json"),
            (500, UPSTREAM_ERROR, "application/json"),
        ]
        status, payload = self.chat()
        self.assertEqual(status, 502)
        self.assertEqual(
            [item["model"] for item in payload["attempts"]],
            [MODEL, FALLBACK],
        )

    def test_settings_reports_fallback_policy(self):
        status, payload = get(self.base + "/api/settings/model")
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertEqual(body["fallback"], FALLBACK)
        self.assertEqual(body["model_policy"], MODEL)

    def test_no_fallback_policy_configured(self):
        # Simulate an environment without the fallback env var.
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": KEY,
        }
        self.stop_jarvis()
        self.upstream.chat_responses = [
            (500, UPSTREAM_ERROR, "application/json"),
            (200, ok_response(), "application/json"),
        ]
        self.start_jarvis(config.load(environ=env))
        status, payload = self.chat()
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "upstream_error")
        # Single attempt still reports what was tried.
        self.assertEqual(
            payload["attempts"],
            [{"model": MODEL, "code": "upstream_error"}],
        )
        self.assertEqual(len(self.upstream_chat_calls()), 1)

    def test_fallback_equal_to_primary_single_attempt(self):
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": KEY,
            "JARVIS_FALLBACK_MODEL": MODEL,
        }
        self.stop_jarvis()
        self.upstream.chat_responses = [
            (500, UPSTREAM_ERROR, "application/json"),
            (200, ok_response(), "application/json"),
        ]
        self.start_jarvis(config.load(environ=env))
        status, payload = self.chat()
        self.assertEqual(status, 502)
        self.assertEqual(len(self.upstream_chat_calls()), 1)

    def test_not_configured_single_attempt(self):
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "JARVIS_FALLBACK_MODEL": FALLBACK,
        }
        self.stop_jarvis()
        self.start_jarvis(config.load(environ=env))
        status, payload = self.chat()
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "not_configured")
        self.assertEqual(self.upstream_chat_calls(), [])


class FallbackObservabilityTests(FallbackTestCase):
    def test_fallback_event_logged_without_payload_or_secrets(self):
        self.upstream.chat_responses = [
            (500, UPSTREAM_ERROR, "application/json"),
            (200, ok_response(), "application/json"),
        ]
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            status, payload = self.chat({"message": "secret prompt body"})
        self.assertEqual(status, 200)
        lines = [
            line for line in captured.getvalue().splitlines()
            if line.startswith("{")
        ]
        events = [json.loads(line) for line in lines]
        fallback_events = [
            item for item in events if item.get("event") == "model_fallback"
        ]
        self.assertEqual(len(fallback_events), 1)
        event = fallback_events[0]
        self.assertEqual(event["primary_model"], MODEL)
        self.assertEqual(event["primary_code"], "upstream_error")
        self.assertEqual(event["fallback_model"], FALLBACK)
        self.assertEqual(event["outcome"], "fallback_ok")
        flat = json.dumps(event)
        self.assertNotIn("secret prompt body", flat)
        self.assertNotIn(KEY, flat)
        # The prompt itself must never appear in any log line either.
        self.assertNotIn("secret prompt body", captured.getvalue())


class ToolProposalTests(unittest.TestCase):
    """parse_tool_proposals: representation only, never executed."""

    def test_no_tool_calls_returns_empty(self):
        self.assertEqual(provider.parse_tool_proposals({"content": "hi"}), [])

    def test_valid_proposals_extracted(self):
        message = {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {"name": "search_notes",
                                 "arguments": '{"query": "triage"}'},
                },
            ],
        }
        proposals = provider.parse_tool_proposals(message)
        self.assertEqual(
            proposals, [{"name": "search_notes", "arguments":
                         {"query": "triage"}}]
        )

    def test_arguments_object_direct_is_accepted(self):
        message = {
            "tool_calls": [
                {"function": {"name": "ping", "arguments": {"x": 1}}},
            ],
        }
        self.assertEqual(
            provider.parse_tool_proposals(message),
            [{"name": "ping", "arguments": {"x": 1}}],
        )

    def assertMalformed(self, message):
        with self.assertRaises(provider.MalformedResponse):
            provider.parse_tool_proposals(message)

    def test_tool_calls_not_a_list(self):
        self.assertMalformed({"tool_calls": {"function": {"name": "x"}}})

    def test_call_entry_not_object(self):
        self.assertMalformed({"tool_calls": ["nope"]})

    def test_missing_function_object(self):
        self.assertMalformed({"tool_calls": [{"id": "call_1"}]})

    def test_empty_name(self):
        self.assertMalformed(
            {"tool_calls": [{"function": {"name": "", "arguments": "{}"}}]}
        )

    def test_unsafe_name_rejected(self):
        for bad in ("eval;rm", "drop table", "tool/x", "../escape", "a\nb"):
            self.assertMalformed(
                {"tool_calls": [
                    {"function": {"name": bad, "arguments": "{}"}}
                ]}
            )

    def test_name_too_long_rejected(self):
        self.assertMalformed(
            {"tool_calls": [
                {"function": {
                    "name": "a" * (provider.MAX_TOOL_NAME_CHARS + 1),
                    "arguments": "{}",
                }}
            ]}
        )

    def test_arguments_not_valid_json_string(self):
        self.assertMalformed(
            {"tool_calls": [
                {"function": {"name": "ok_name", "arguments": "{oops"}}
            ]}
        )

    def test_arguments_not_an_object(self):
        self.assertMalformed(
            {"tool_calls": [
                {"function": {"name": "ok_name", "arguments": "[1, 2]"}}
            ]}
        )

    def test_name_with_safe_charset_accepted(self):
        message = {
            "tool_calls": [
                {"function": {
                    "name": "odbtr-search.v2_y",
                    "arguments": "{}",
                }}
            ],
        }
        self.assertEqual(
            provider.parse_tool_proposals(message),
            [{"name": "odbtr-search.v2_y", "arguments": {}}],
        )


if __name__ == "__main__":
    unittest.main()
