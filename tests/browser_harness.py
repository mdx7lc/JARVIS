"""Browser end-to-end harness (Phase 6B).

Starts the real JARVIS server against an isolated temp database and a
local fake provider, then drives the frozen static UI in real headless
Playwright browsers. No live AI inference, no real network, no use of
the production data/jarvis.db.

Requires the e2e venv (dev-only, never part of the stdlib suite):
    /home/mdx/.cache/jarvis-e2e/venv/bin/python -m unittest \\
        discover -s tests -p 'test_browser_*.py' -v
(Any interpreter that has playwright installed works: the whole suite
self-skips otherwise. Rebuild after /tmp cleanups:
    python3 -m venv <dir> && <dir>/bin/pip install playwright
    <dir>/bin/python -m playwright install firefox)
"""

import contextlib
import json
import sqlite3
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:  # stdlib discovery reaches this file without playwright
    HAVE_PLAYWRIGHT = False

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))  # reuse fake_upstream like other suites

PROVIDER_KEY = "browser-e2e-canary-not-a-secret"

SKIP_BROWSER = unittest.skipUnless(
    HAVE_PLAYWRIGHT,
    "browser suite requires the e2e venv; see tests/browser_harness.py",
)


class _Capture:
    """Collects what the server would otherwise print (logs stay local)."""

    def __init__(self):
        self._chunks = []

    def write(self, s):
        self._chunks.append(s)
        return len(s)

    def flush(self):
        pass

    def text(self):
        return "".join(self._chunks)


class Stack:
    """Temp workdir + temp DB + fake provider + the real HTTP server.

    setUp() creates it; start(testcase) wires cleanup. stdout and
    stderr are captured so tests can assert on emitted telemetry.
    """

    def __init__(self, reply=None, history_limit=20, status_counts_from=None,
                 extra_env=None):
        self._tmp = tempfile.TemporaryDirectory(prefix="jarvis-browser-")
        self.db_path = Path(self._tmp.name) / "jarvis.db"
        # Phase 11A: isolated persisted settings dir (survives
        # restart_server, never a real operator config dir)
        self.settings_dir = Path(self._tmp.name) / "settings"
        from fake_upstream import FakeUpstream
        self.upstream = FakeUpstream()
        self.upstream.response = (200, reply if reply is not None else self.default_reply(),
                                  "application/json")
        self.config = self._make_config(history_limit, extra_env)
        import server
        self.server_module = server
        self.stdout_cap = _Capture()
        self.stderr_cap = _Capture()
        with contextlib.redirect_stdout(self.stdout_cap), contextlib.redirect_stderr(self.stderr_cap):
            self._httpd = server.create_server(
                "127.0.0.1", 0, str(self.db_path), cfg=self.config,
                settings_dir=str(self.settings_dir))
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def _make_config(self, history_limit, extra_env=None):
        import config as config_module
        env = {
            "JARVIS_DB_PATH": str(self.db_path),
            "FREELLMAPI_URL": self.upstream.base_url,
            "FREELLMAPI_API_KEY": PROVIDER_KEY,
            "JARVIS_HISTORY_MESSAGES": str(history_limit),
        }
        if extra_env:
            env.update(extra_env)
        return config_module.load(environ=env)

    @staticmethod
    def default_reply():
        return {
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "model": "qwen3.8-27b",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello! How can I help?"},
                "finish_reason": "stop",
            }],
        }

    def start(self, testcase):
        import threading
        # keep log capture alive for the server's lifetime (not just creation)
        self._logstack = contextlib.ExitStack()
        self._logstack.enter_context(
            contextlib.redirect_stdout(self.stdout_cap))
        self._logstack.enter_context(
            contextlib.redirect_stderr(self.stderr_cap))
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.upstream.start(testcase)
        testcase.addCleanup(self.stop)

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        self._logstack.close()
        self._tmp.cleanup()

    def crash_server(self):
        """Stop serving (files stay) so the UI sees an unreachable server."""
        self._httpd.shutdown()
        self._httpd.server_close()

    def restart_server(self, testcase):
        """Fresh server process on a fresh port over the same DB files."""
        self.crash_server()
        self._logstack.close()
        self._logstack = contextlib.ExitStack()
        self._logstack.enter_context(
            contextlib.redirect_stdout(self.stdout_cap))
        self._logstack.enter_context(
            contextlib.redirect_stderr(self.stderr_cap))
        self._httpd = self.server_module.create_server(
            "127.0.0.1", 0, str(self.db_path), cfg=self.config,
            settings_dir=str(self.settings_dir))
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        testcase.addCleanup(self.stop_restarted)

    def stop_restarted(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        self._logstack.close()

    # ------------------------------------------------------------ helpers
    def api(self, method, path, payload=None):
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())

    def conversations(self):
        return self.api("GET", "/api/conversations")[1].get("conversations", [])

    def tasks(self):
        return self.api("GET", "/api/tasks?limit=200")[1]["tasks"]

    def db_messages(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            return conn.execute(
                "SELECT role, content FROM messages ORDER BY id").fetchall()

    def stderr_text(self):
        return self.stderr_cap.text()


class BrowserCase(unittest.TestCase):
    """One Playwright browser per class, one fresh page per test."""

    # Canned provider reply consumed verbatim by the fake upstream
    # (choice[0].message.content, raw markdown string).
    REPLY = "Hello! How can I help?"

    def make_stack(self, reply):
        """Hook for subclasses that need custom environment/stack config."""
        return Stack(reply=reply)

    @classmethod
    def setUpClass(cls):
        if not HAVE_PLAYWRIGHT:
            raise unittest.SkipTest("playwright unavailable")
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.firefox.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def setUp(self):
        import fake_upstream
        reply = fake_upstream.OK_RESPONSE
        if self.REPLY != fake_upstream.OK_RESPONSE["choices"][0]["message"]["content"]:
            import copy
            reply = copy.deepcopy(fake_upstream.OK_RESPONSE)
            reply["choices"][0]["message"]["content"] = self.REPLY
        self.stack = self.make_stack(reply)
        self.stack.start(self)
        self._ctx = self._browser.new_context()
        self._ctx.set_default_timeout(15_000)
        self.page = self._ctx.new_page()
        self.page.on("dialog", lambda d: d.accept())  # confirm() switches
        self.page.goto(self.stack.base + "/")
        self.wait_ready()

    def tearDown(self):
        self._ctx.close()

    # ------------------------------------------------------------ waits
    def wait_ready(self):
        # readiness = init() rendered the timeline (no clock in 11B)
        self.page.wait_for_function(
            "() => { const t = document.getElementById('timeline');"
            " return t && t.children.length > 0; }"
        )

    def reload(self):
        self.page.reload()
        self.wait_ready()

    # ------------------------------------------------------------ shortcuts
    def open_chat(self):
        self.page.evaluate("openModal('modal-chat')")
        self.page.wait_for_selector("#modal-chat[open]")

    def send_chat(self, text):
        self.page.fill("#chat-input-modal", text)
        self.page.click("#chat-form-modal .btn-primary")

    def toast_box(self):
        return self.page.locator("#toast-box")
