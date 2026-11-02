"""Phase 2B provider adapter tests — fake upstream only, no real tokens."""

import json
import socket
import unittest

import provider
from fake_upstream import FakeUpstream, OK_RESPONSE

KEY = "test-key-123"
_SMOKE_MESSAGES = [{"role": "user", "content": "hi"}]
MODEL = "qwen3.8-27b"


class ProviderTestCase(unittest.TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        self.upstream.start(self)
        self.base = self.upstream.base_url
        self.kwargs = dict(
            base_url=self.base,
            api_key=KEY,
            model=MODEL,
            timeout_seconds=10,
            max_tokens=512,
        )

    def call(self, messages=None, **overrides):
        kwargs = dict(self.kwargs)
        kwargs.update(overrides)
        return provider.chat_completion(
            messages if messages is not None
            else [{"role": "user", "content": "Hi"}],
            **kwargs,
        )

    def upstream_response(self, status, payload, ctype="application/json"):
        self.upstream.response = (status, payload, ctype)

    def last_request(self):
        with self.upstream.lock:
            return self.upstream.requests[-1]


class NormalizeBaseTests(unittest.TestCase):
    def test_variants_normalize_identically(self):
        expected = "http://localhost:3001"
        for raw in (
            "http://localhost:3001",
            "http://localhost:3001/",
            "http://localhost:3001/v1",
            "http://localhost:3001/v1/",
        ):
            self.assertEqual(provider.normalize_base(raw), expected)

    def test_default_used_when_blank(self):
        self.assertEqual(provider.normalize_base(""), "http://localhost:3001")
        self.assertEqual(provider.normalize_base(None), "http://localhost:3001")

    def test_remote_host_rejected(self):
        for bad in ("http://example.com:3001", "https://api.openai.com/v1"):
            with self.assertRaises(provider.InvalidConfig):
                provider.normalize_base(bad)

    def test_userinfo_query_fragment_and_paths_rejected(self):
        for bad in (
            "http://user:pass@localhost:3001",
            "http://localhost:3001/?x=1",
            "http://localhost:3001/#frag",
            "http://localhost:3001/api/v1",
            "ftp://localhost:3001",
            "not a url",
        ):
            with self.assertRaises(provider.InvalidConfig):
                provider.normalize_base(bad)

    def test_ipv6_loopback_accepted(self):
        self.assertEqual(
            provider.normalize_base("http://[::1]:3001/v1/"),
            "http://[::1]:3001",
        )


class ProviderRequestTests(ProviderTestCase):
    def test_request_structure_and_bearer(self):
        self.call()
        record = self.last_request()
        self.assertEqual(record["path"], "/v1/chat/completions")
        self.assertEqual(
            record["headers"].get("Authorization"), f"Bearer {KEY}"
        )
        self.assertEqual(
            record["headers"].get("Content-Type"), "application/json"
        )
        body = record["body"]
        self.assertEqual(body["model"], MODEL)
        self.assertFalse(body["stream"])
        self.assertEqual(body["max_tokens"], 512)
        self.assertEqual(
            body["messages"], [{"role": "user", "content": "Hi"}]
        )

    def test_valid_response_parsed(self):
        self.assertEqual(self.call(), "Hello! How can I help?")

    def test_include_model_returns_text_and_gateway_echo(self):
        # default contract unchanged; opt-in meta returns the gateway's
        # `model` echo as metadata only (Phase 9A)
        self.upstream_response(200, {"choices": [{"message": {"content": "hi"}}],
                                     "model": "glm-5.3-flash"})
        text, reported = provider.chat_completion(_SMOKE_MESSAGES, include_model=True, **self.kwargs)
        self.assertEqual(text, "hi")
        self.assertEqual(reported, "glm-5.3-flash")

    def test_include_model_reports_none_when_gateway_omits_it(self):
        self.upstream_response(
            200, {"choices": [{"message": {"content": "hi"}}]})
        text, reported = provider.chat_completion(_SMOKE_MESSAGES, include_model=True, **self.kwargs)
        self.assertEqual(text, "hi")
        self.assertIsNone(reported)

    def test_include_model_ignores_non_string_model_echo(self):
        self.upstream_response(200, {"choices": [{"message": {"content": "hi"}}],
                                     "model": 42})
        text, reported = provider.chat_completion(_SMOKE_MESSAGES, include_model=True, **self.kwargs)
        self.assertEqual(text, "hi")
        self.assertIsNone(reported)

    def test_missing_key_never_contacts_upstream(self):
        with self.assertRaises(provider.NotConfigured):
            self.call(api_key="")
        self.assertEqual(len(self.upstream.requests), 0)

    def test_invalid_base_rejected_before_contact(self):
        with self.assertRaises(provider.InvalidConfig):
            self.call(base_url="http://example.com:3001")
        self.assertEqual(len(self.upstream.requests), 0)

    def test_auth_failure_mapped(self):
        self.upstream_response(401, {"error": "bad key"})
        with self.assertRaises(provider.AuthFailed) as ctx:
            self.call()
        self.assertEqual(ctx.exception.http_status, 502)

    def test_rate_limit_mapped(self):
        self.upstream_response(429, {"error": "slow down"})
        with self.assertRaises(provider.RateLimited) as ctx:
            self.call()
        self.assertEqual(ctx.exception.http_status, 429)

    def test_other_4xx_mapped_as_rejected(self):
        self.upstream_response(400, {"error": "bad request"})
        with self.assertRaises(provider.RequestRejected):
            self.call()

    def test_upstream_5xx_mapped(self):
        self.upstream_response(500, {"error": "boom"})
        with self.assertRaises(provider.UpstreamError):
            self.call()

    def test_connection_failure_mapped(self):
        # Bind then close: nothing is listening on that port.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(provider.ConnectionFailed):
            self.call(base_url=f"http://127.0.0.1:{port}")

    def test_timeout_mapped(self):
        self.upstream.delay = 1.0
        with self.assertRaises(provider.ProviderTimeout) as ctx:
            self.call(timeout_seconds=0.2)
        self.assertEqual(ctx.exception.http_status, 504)

    def test_redirect_rejected(self):
        self.upstream_response(
            302, None, "text/plain"
        )
        self.upstream.location = "http://127.0.0.1:1/steal"
        with self.assertRaises(provider.RedirectRejected):
            self.call()

    def test_malformed_json_rejected(self):
        self.upstream_response(200, b"{not json", "application/json")
        with self.assertRaises(provider.MalformedResponse):
            self.call()

    def test_non_json_content_type_rejected(self):
        self.upstream_response(200, "<html>hi</html>", "text/html")
        with self.assertRaises(provider.MalformedResponse):
            self.call()

    def test_missing_choices_rejected(self):
        self.upstream_response(200, {"object": "chat.completion"})
        with self.assertRaises(provider.MalformedResponse):
            self.call()

    def test_missing_content_rejected(self):
        self.upstream_response(
            200, {"choices": [{"message": {"role": "assistant"}}]}
        )
        with self.assertRaises(provider.MalformedResponse):
            self.call()

    def test_empty_assistant_text_rejected(self):
        self.upstream_response(
            200,
            {"choices": [{"message": {"role": "assistant", "content": "  "}}]},
        )
        with self.assertRaises(provider.EmptyResponse):
            self.call()

    def test_oversized_response_rejected(self):
        original = provider.MAX_RESPONSE_BYTES
        provider.MAX_RESPONSE_BYTES = 64
        self.addCleanup(setattr, provider, "MAX_RESPONSE_BYTES", original)
        big = {"choices": [{"message": {"role": "assistant",
                                        "content": "x" * 500}}]}
        self.upstream_response(200, big, "application/json")
        with self.assertRaises(provider.MalformedResponse):
            self.call()

    def test_errors_never_contain_the_key(self):
        failures = []
        scenarios = [
            (401, {"error": "e"}, "application/json"),
            (429, {"error": "e"}, "application/json"),
            (500, {"error": "e"}, "application/json"),
            (200, b"{broken", "application/json"),
            (200, "<html/>", "text/html"),
            (200, {"nope": 1}, "application/json"),
        ]
        for status, payload, ctype in scenarios:
            self.upstream.response = (status, payload, ctype)
            try:
                self.call()
            except provider.ProviderError as exc:
                failures.append(str(exc))
                failures.append(exc.user_message)
        joined = "\n".join(failures)
        self.assertNotIn(KEY, joined)

    def test_invalid_messages_rejected_locally(self):
        for bad in (
            [],
            "nope",
            [{"role": "alien", "content": "x"}],
            [{"role": "user", "content": ""}],
            [{"role": "user"}],
        ):
            with self.assertRaises(ValueError):
                self.call(messages=bad)
        self.assertEqual(len(self.upstream.requests), 0)


if __name__ == "__main__":
    unittest.main()
