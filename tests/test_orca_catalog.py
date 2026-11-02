"""Phase 11A public OrcaRouter catalog tests — FakeUpstream only.

Covers the honest cost classification contract and the bounded public
fetch helpers (no credentials ever sent, redirects refused, malformed
responses rejected). The pricing/free-package URL constants are
monkeypatched onto the fake upstream; no real network access happens.
"""

import json
import tempfile
import unittest
import urllib.error
from unittest import mock

import provider
from fake_upstream import (FakeUpstream, PRICING_RESPONSE,
                           FREE_PACKAGE_RESPONSE)


class OrcaCatalogTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="orca-cat-")
        self.addCleanup(self._tmp.cleanup)
        self.upstream = FakeUpstream()
        self.upstream.start(self)
        # point every documented pricing URL at the fake upstream
        self._patched = [
            ("ORCAROUTER_PRICING_URL",
             self.upstream.base_url + "/api/pricing"),
            ("ORCAROUTER_FREE_URL",
             self.upstream.base_url + "/api/free-package/public"),
        ]
        for name, value in self._patched:
            patcher = mock.patch.object(provider, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- classification semantics (pure, no network) ----

    def test_classify_pricing_semantics(self):
        classify = provider.classify_pricing
        self.assertEqual(classify({"model_ratio": 1.0,
                                   "quota_type": 0}), "paid")
        self.assertEqual(classify({"model_ratio": 0.0,
                                   "quota_type": 0}), "free_verified")
        self.assertEqual(classify({"model_price": 0.5,
                                   "quota_type": 1}), "paid")
        self.assertEqual(classify({"model_price": 0.0,
                                   "quota_type": 1}), "free_verified")
        self.assertEqual(classify({"is_free_tier": True}), "free_verified")
        self.assertEqual(classify({"is_free_tier": True,
                                   "model_ratio": 1.0, "quota_type": 0}),
                         "free_verified")
        self.assertEqual(classify({}, in_free_package=True),
                         "free_verified")
        self.assertEqual(classify({"model_ratio": 0.614,
                                   "completion_ratio": 4.61,
                                   "quota_type": 0}), "paid")
        # no evidence at all is 'unknown', never guessed free
        self.assertEqual(classify(None), "unknown")
        self.assertEqual(classify({"quota_type": 3}), "unknown")
        self.assertEqual(classify({"is_free_tier": False}), "paid")

    # ---- bounded public fetches against the fake upstream ----

    def test_fetch_pricing_catalog_parses_verified_fields(self):
        pricing = provider.fetch_pricing_catalog(timeout_seconds=5)
        self.assertIn("orcarouter/auto", pricing)
        entry = pricing["orcarouter/auto"]
        self.assertEqual(entry["quota_type"], 0)
        self.assertEqual(entry["model_ratio"], 1.0)
        self.assertEqual(entry["is_free_tier"], False)
        self.assertEqual(pricing["z-ai/glm-4.6"]["model_ratio"], 0.614)
        self.assertEqual(
            pricing["deepseek/deepseek-chat-v3.1-free"]["free_base_model"],
            "deepseek/deepseek-chat-v3.1")

    def test_public_fetches_never_send_a_credential(self):
        provider.fetch_pricing_catalog(timeout_seconds=5)
        provider.fetch_free_package(timeout_seconds=5)
        with self.upstream.lock:
            headers = [r["headers"] for r in self.upstream.requests]
        for h in headers:
            auth = h.get("Authorization", "")
            self.assertEqual(
                auth.strip(), "",
                "a public catalog fetch must not carry a credential")

    def test_fetch_free_package_parses_aliases(self):
        free_ids = provider.fetch_free_package(timeout_seconds=5)
        self.assertEqual(
            free_ids, {"deepseek/deepseek-chat-v3.1-free"})

    def test_merge_attaches_honest_classes(self):
        models = [{"id": "orcarouter/auto", "available": True},
                  {"id": "deepseek/deepseek-chat-v3.1-free",
                   "available": True},
                  {"id": "unpriced-model", "available": False}]
        pricing = provider.fetch_pricing_catalog(timeout_seconds=5)
        free_ids = provider.fetch_free_package(timeout_seconds=5)
        merged = provider.merge_catalog_entries(
            models, pricing=pricing, free_ids=free_ids)
        classes = {m["id"]: m["pricing_class"] for m in merged}
        self.assertEqual(classes["orcarouter/auto"], "paid")
        # free package alias overrides even a missing/zero pricing row
        self.assertEqual(
            classes["deepseek/deepseek-chat-v3.1-free"], "free_verified")
        self.assertEqual(classes["unpriced-model"], "unknown")
        # nothing was dropped and availability never invented
        self.assertTrue(all(m["available"] == src["available"]
                            for m, src in zip(merged, models)))

    def test_pricing_failure_leaves_everything_unknown(self):
        self.upstream.pricing_response = (500, {"error": "boom"},
                                          "application/json")
        self.upstream.free_package_response = (500, {"error": "boom"},
                                               "application/json")
        with self.assertRaises(provider.ProviderError):
            provider.fetch_pricing_catalog(timeout_seconds=5)
        models = provider.merge_catalog_entries([{"id": "x"}])
        self.assertEqual(models[0]["pricing_class"], "unknown")

    def test_malformed_bodies_are_rejected(self):
        cases = [
            (200, "not json", "application/json"),
            (200, {"no": "data"}, "application/json"),
            (200, {}, "text/html"),
        ]
        for status, payload, ctype in cases:
            self.upstream.pricing_response = (status, payload, ctype)
            with self.assertRaises(provider.ProviderError):
                provider.fetch_pricing_catalog(timeout_seconds=5)

    def test_redirects_are_refused(self):
        self.upstream.response = (302, {}, "application/json")
        self.upstream.location = "http://127.0.0.1:1/elsewhere"
        self.upstream.pricing_response = (302, {}, "application/json")
        with self.assertRaises(provider.ProviderError):
            provider.fetch_pricing_catalog(timeout_seconds=5)

        def _restore():
            self.upstream.response = (200, {}, "application/json")
            self.upstream.location = None
        self.addCleanup(_restore)

    def test_oversized_pricing_body_is_rejected(self):
        huge = b'{"data": ["' + b"x" * (
            provider.MAX_PRICING_RESPONSE_BYTES + 10) + b'"]}'
        self.upstream.pricing_response = (200, huge, "application/json")
        with self.assertRaises(provider.ProviderError):
            provider.fetch_pricing_catalog(timeout_seconds=5)
        self.upstream.pricing_response = (
            200, PRICING_RESPONSE, "application/json")


class NormalizeBaseOrcaTests(unittest.TestCase):
    """The orca host allowlist: exact documented origin, default port,
    no path games; loopback keeps its existing semantics."""

    def allow(self, url):
        return provider.normalize_base(url)

    def test_exact_orca_origin_allowed(self):
        self.assertEqual(self.allow("https://api.orcarouter.ai/v1"),
                         "https://api.orcarouter.ai")

    def test_orca_with_port_or_other_host_rejected(self):
        for bad in ("https://api.orcarouter.ai:8443/v1",
                    "http://api.orcarouter.ai/v1",
                    "https://evil.api.orcarouter.ai/v1",
                    "https://api.orcarouter.ai.evil.com/v1",
                    "https://evil.example.com/v1",
                    "ftp://api.orcarouter.ai"):
            with self.assertRaises(Exception):
                self.allow(bad)

    def test_loopback_semantics_unchanged(self):
        for ok in ("http://localhost:3001", "http://127.0.0.1:1234"):
            self.allow(ok)


if __name__ == "__main__":
    unittest.main()
