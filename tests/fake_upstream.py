"""Reusable fake OpenAI-compatible upstream server for tests.

Listens on 127.0.0.1:0 only, records received requests, and returns a
configurable canned response. Never touches the real network.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OK_RESPONSE = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "qwen3.8-27b",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello! How can I help?",
            },
            "finish_reason": "stop",
        }
    ],
}


MODELS_RESPONSE = {
    "object": "list",
    "data": [
        {"id": "qwen3.8-27b"},
        {"id": "auto"},
        {"id": "lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF"},
    ],
}


# Phase 11A: scripted public OrcaRouter-style endpoints (no auth, plain
# JSON). Set on the FakeUpstream instance; the tests point provider's
# ORCAROUTER_* URL constants at this server.
PRICING_RESPONSE = {
    "success": True,
    "data": [
        {"model_name": "orcarouter/auto", "display_name": "Auto",
         "quota_type": 0, "model_ratio": 1.0, "completion_ratio": 3.0,
         "is_free_tier": False},
        {"model_name": "deepseek/deepseek-chat-v3.1-free",
         "display_name": "DeepSeek Chat v3.1 (free)",
         "quota_type": 0, "model_ratio": 0.0, "completion_ratio": 0.0,
         "is_free_tier": True, "free_base_model":
             "deepseek/deepseek-chat-v3.1"},
        {"model_name": "z-ai/glm-4.6", "display_name": "GLM 4.6",
         "quota_type": 0, "model_ratio": 0.614, "completion_ratio": 4.61,
         "is_free_tier": False},
        {"model_name": "per-minute-billing-thing",
         "display_name": "Per minute", "quota_type": 1,
         "model_price": 0.5, "is_free_tier": False},
        {"model_name": "zero-cost-thing", "quota_type": 0,
         "model_ratio": 0.0, "completion_ratio": 0.0,
         "is_free_tier": False},
    ],
}

FREE_PACKAGE_RESPONSE = {
    "success": True,
    "data": {
        "models": [
            {"free_alias": "deepseek/deepseek-chat-v3.1-free"},
        ],
    },
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        srv = self.server
        if srv.delay:
            time.sleep(srv.delay)
        with srv.lock:
            srv.requests.append(
                {"path": self.path, "headers": dict(self.headers),
                 "body": None}
            )
        path = self.path.split("?", 1)[0]
        if path.startswith("/v1/models") \
                and srv.models_response is not None:
            status, payload, ctype = srv.models_response
        elif path == "/api/pricing" and srv.pricing_response is not None:
            status, payload, ctype = srv.pricing_response
        elif path == "/api/free-package/public" \
                and srv.free_package_response is not None:
            status, payload, ctype = srv.free_package_response
        else:
            status, payload, ctype = srv.response
        self._send(srv, status, payload, ctype)

    def do_POST(self):
        srv = self.server
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length else b""
        except OSError:
            return
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            parsed = None
        with srv.lock:
            srv.requests.append(
                {"path": self.path, "headers": dict(self.headers),
                 "body": parsed}
            )
            # Phase 8A: per-request scriptable sequences
            if srv.chat_responses:
                response = srv.chat_responses.pop(0)
            else:
                response = srv.response
            if srv.delay_queue:
                delay = srv.delay_queue.pop(0)
            else:
                delay = srv.delay
        if delay:
            time.sleep(delay)
        self._send(srv, *response)

    def _send(self, srv, status, payload, ctype):
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload).encode("utf-8")
        elif payload is None:
            data = b""
        else:
            # raw payload (bytes or str); str must be encoded or wfile raises
            data = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if srv.location:
                self.send_header("Location", srv.location)
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass  # client (timeout tests) may already be gone

    def log_message(self, *args):
        pass


class FakeUpstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, response=(200, OK_RESPONSE, "application/json")):
        ThreadingHTTPServer.__init__(self, ("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.requests = []
        self.response = response
        self.models_response = (200, MODELS_RESPONSE, "application/json")
        # Phase 11A: fake OrcaRouter public endpoints (None -> falls
        # through to `response`).
        self.pricing_response = (200, PRICING_RESPONSE, "application/json")
        self.free_package_response = (
            200, FREE_PACKAGE_RESPONSE, "application/json")
        # Phase 8A: per-POST /v1/chat/completions scripted sequences
        # (consumed in order; empty list falls back to `response`).
        self.chat_responses = []
        # Per-request pre-response delays, in order (empty -> `delay`).
        self.delay_queue = []
        self.delay = 0.0
        self.location = None

    @property
    def base_url(self):
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start(self, testcase):
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        testcase.addCleanup(self.stop)

    def stop(self):
        self.shutdown()
        self.server_close()
