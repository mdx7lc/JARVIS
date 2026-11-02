"""JARVIS local HTTP server — stdlib only.

Serves the frozen frontend from static/ and the legacy root assets/ dir,
plus a JSON API under /api/. Binds loopback only (enforced by config).
No stack traces or filesystem paths are ever returned to clients.
"""

import datetime
import difflib
import hashlib
import hmac
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import config
import db
import metrics
import provider
import settingsstore
import applog
import approvals
import permissions as permissions_mod
import goalstate
import tools
import vault as vault_mod
import ratelimit
import skills as skills_mod
import skillexec as skillexec_mod
import updates as updates_mod
import improvements as improvements_mod
import worker as goal_worker
import reviewer as reviewer_mod

PROJECT_ROOT = Path(__file__).resolve().parent
STATIC_DIR = PROJECT_ROOT / "static"
ASSETS_DIR = PROJECT_ROOT / "assets"
START_TIME = time.time()
MAX_BODY_BYTES = 1_048_576
MAX_CHAT_BODY_BYTES = 32 * 1024
MAX_MESSAGE_CHARS = 8000
MAX_HISTORY_CHARS = 24000
CHAT_ALLOWED_KEYS = {"message", "conversation_id"}

# Rate limiting (Phase 6A) — bounded, in-memory, per-process; state
# resets whenever the server restarts. Policies target only the
# expensive/mutation endpoints; harmless dashboard polls (metrics,
# status, task/conversation listing every 6–30 s) stay unrestricted so
# the Phase 4 refresh loop never breaks. Defaults are conservative for
# a single local user: they cannot be reached by normal interaction
# (typing one search at ~2 req/300 ms debounce, sending chat messages
# conversationally) and correspond to ~3x the fastest sustained
# legitimate activity.
RATE_CHAT_PER_MINUTE = 20
RATE_SEARCH_PER_MINUTE = 30
RATE_TASKS_WRITE_PER_MINUTE = 60
RATE_MODELS_PER_MINUTE = 10
RATE_MODEL_SETTINGS_PER_MINUTE = 30
RATE_SETTINGS_PER_MINUTE = 30
RATE_SETTINGS_PROBE_PER_MINUTE = 10
RATE_MODEL_TEST_PER_MINUTE = 6
RATE_VAULT_READ_PER_MINUTE = 60
RATE_VAULT_REFRESH_PER_MINUTE = 4
RATE_LIMIT_POLICIES = {
    # POST /api/chat — consumes external provider quota
    "chat": (RATE_CHAT_PER_MINUTE, 60.0),
    # GET /api/search — may scan SQLite rows
    "search": (RATE_SEARCH_PER_MINUTE, 60.0),
    # POST/PATCH/DELETE /api/tasks[/{id}] — bounded repeated writes
    "tasks_write": (RATE_TASKS_WRITE_PER_MINUTE, 60.0),
    # GET /api/models — authenticated gateway fetch (Phase 7A); the
    # catalog cache absorbs USUAL refresh traffic, this caps worst case
    "models": (RATE_MODELS_PER_MINUTE, 60.0),
    # PATCH/DELETE /api/settings/model — model policy changes (Phase 7A)
    "model_settings": (RATE_MODEL_SETTINGS_PER_MINUTE, 60.0),
    # Phase 11A: GET/PATCH /api/settings (view + bounded writes)
    "settings": (RATE_SETTINGS_PER_MINUTE, 60.0),
    # Phase 11A: catalog fetches / test-connection (network) + credentials
    "settings_probe": (RATE_SETTINGS_PROBE_PER_MINUTE, 60.0),
    # Phase 11A: POST /api/settings/model-test — real bounded inference
    "model_test": (RATE_MODEL_TEST_PER_MINUTE, 60.0),
    # POST /api/goals + /cancel + /retry (Phase 8B): creation starts
    # durable (model-consuming) work, so it is bounded conservatively
    "goals": (10, 60.0),
    # Phase 13A: vault status reads + explicit index refresh (filesystem
    # scan is the expensive part; refresh is strictly rate-capped)
    "vault_read": (RATE_VAULT_READ_PER_MINUTE, 60.0),
    "vault_refresh": (RATE_VAULT_REFRESH_PER_MINUTE, 60.0),
}
# Model catalog cache (Phase 7A): explicitly refreshed by user action,
# never background-fetched. A stale cache simply triggers one fresh
# fetch at the next explicit request instead of a refresh loop.
CATALOG_FRESH_SECONDS = 60.0
MAX_SETTINGS_BODY_BYTES = 4 * 1024
RATE_LIMITED_CODE = "rate_limited"
# Phase 11A: settings center bounds.
MAX_SETTINGS_API_KEY_CHARS = settingsstore.MAX_API_KEY_CHARS
SETTINGS_PATCH_KEYS = frozenset(
    {"timeout_seconds", "max_tokens", "history_messages",
     "fallback_model", "reviewer", "reviewer_model", "vault_path"}
)
MAX_MODEL_TEST_TOKENS = 16
MODEL_TEST_PROMPT = [{"role": "user", "content": "Reply with exactly: ok"}]
SHOW_CATALOG_TIERS = ("free_verified", "paid", "unknown")
# Conversation read endpoints (Phase 3B)
DEFAULT_CONVERSATIONS_LIMIT = 50
MAX_CONVERSATIONS_LIMIT = 100
DEFAULT_MESSAGE_WINDOW = 200
MAX_MESSAGE_WINDOW = 500
CONV_ID_CHARS = frozenset("0123456789abcdef")
PREVIEW_CHARS = 120

# Task endpoints (Phase 3D)
DEFAULT_TASKS_LIMIT = 50
MAX_TASKS_LIMIT = 200
MAX_TASK_BODY_BYTES = 16 * 1024

# Phase 8B goal console bounds.
MAX_GOAL_BODY_BYTES = 2 * 1024
MAX_GOAL_CHARS = 500
DEFAULT_GOALS_LIMIT = 25
MAX_GOALS_LIMIT = 50
MAX_GOAL_DETAIL_EVENTS = 50
MAX_PREVIEW_DIFF_LINES = 600
MAX_PREVIEW_DIFF_BYTES = 32_000
MAX_TASK_TITLE_CHARS = 200
MAX_TASK_DESC_CHARS = 2000
MAX_TASK_TIME_CHARS = 20
TASK_CREATE_KEYS = frozenset(
    {"title", "description", "status", "priority", "time", "day"}
)
TASK_PATCH_KEYS = frozenset({"title", "description", "status"})
TASK_STATUSES = frozenset(("queued", "warn", "done", "planned"))
TASK_PRIORITIES = frozenset(("high", "std"))
TASK_DAYS = frozenset(("today", "tomorrow"))

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

# Metrics endpoint (Phase 4) — stdlib sampling only; scope strings and
# availability semantics live in metrics.py

# Search endpoint (Phase 5) — literal substring search over persisted
# tasks, conversation titles and message content; parameterized SQL,
# escaped LIKE wildcards, bounded results
SEARCH_MIN_QUERY_CHARS = 2
SEARCH_MAX_QUERY_CHARS = 120
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100
SEARCH_SNIPPET_CHARS = 90


def find_static_file(url_path):
    """Resolve a URL path to a file inside static/ or assets/.

    Returns a resolved Path, or None (bad request/traversal/not found).
    Rejects anything escaping the permitted directories.
    """
    root = ASSETS_DIR if url_path.startswith("/assets/") else STATIC_DIR
    rel = unquote(url_path).strip()
    if "\x00" in rel:
        return None
    if rel == "/" or rel == "":
        rel = "index.html"
    else:
        rel = rel[1:] if rel.startswith("/") else rel
        if url_path.startswith("/assets/"):
            rel = url_path[len("/assets/"):]

    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        return None
    if target.is_dir():
        return None
    if not target.is_file():
        return None
    return target


class _Handler(BaseHTTPRequestHandler):
    server_version = "JARVIS/0.1.0"

    # ---- Phase 6A structured logging + request ids ---------------------
    # request_id is generated server-side per connection attempt; a
    # client-supplied X-Request-ID is never used as the authoritative id.
    def parse_request(self):
        self.request_id = applog.new_request_id()
        self.request_started = time.monotonic()
        return super().parse_request()

    def _route(self):
        """Normalized route: no query parameters, path ids masked."""
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/api/tasks/"):
            return "/api/tasks/<id>"
        if path.startswith("/api/conversations/") and path.endswith(
            "/messages"
        ):
            return "/api/conversations/<id>/messages"
        if path.startswith("/api/conversations/"):
            return "/api/conversations/<id>"
        return path

    def log_request(self, code="-", size="-"):
        """Structured http_request event: no query strings, no paths with
        user content, one line per response."""
        duration = None
        started = getattr(self, "request_started", None)
        if started is not None:
            duration = round((time.monotonic() - started) * 1000)
        applog.event(
            "http_request",
            request_id=getattr(self, "request_id", None),
            method=self.command,
            route=self._route(),
            status=code,
            duration_ms=duration,
        )

    def log_error(self, fmt, *args):
        """Internal diagnostic category only — message strings in this
        codebase are fixed labels; untrusted fragments are sanitized."""
        note = fmt % args if args else fmt
        applog.event(
            "handler_error",
            request_id=getattr(self, "request_id", None),
            method=getattr(self, "command", None),
            route=self._route() if getattr(self, "path", None) else None,
            note=note,
        )

    def _log_failure(self, label, exc):
        """Stable operational category + exception class name only —
        never exception text, which may echo untrusted request data."""
        applog.event(
            "handler_error",
            request_id=getattr(self, "request_id", None),
            method=getattr(self, "command", None),
            route=self._route() if getattr(self, "path", None) else None,
            note=label,
            error_class=type(exc).__name__,
        )

    def log_message(self, fmt, *args):
        # Any stray low-level logging follows the same safe path
        applog.event("http_misc", note=fmt % args if args else fmt)

    # ---- Phase 6A rate limiting ----------------------------------------
    def _rate_limit_gate(self, policy):
        """Run before expensive work. Returns True when one must stop (a
        429 response has then been sent). Never blocks or sleeps."""
        limiter = getattr(self.server, "rate_limiter", None)
        if limiter is None:
            return False
        allowed, retry_after = limiter.hit(policy)
        if allowed:
            return False
        self._send_json(
            429,
            {
                "error": {
                    "code": RATE_LIMITED_CODE,
                    "message": "Too many requests. Wait for the interval "
                    "given in Retry-After and try again.",
                }
            },
            retry_after=retry_after,
        )
        applog.event(
            "rate_limited",
            request_id=getattr(self, "request_id", None),
            method=self.command,
            route=self._route(),
            policy=policy,
            retry_after=retry_after,
        )
        return True

    def do_GET(self):
        self._dispatch(send_body=True)

    def do_HEAD(self):
        self._dispatch(send_body=False)

    def do_POST(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/api/chat":
            self._handle_chat()
        elif path == "/api/permissions/cap":
            self._handle_permissions_cap()
        elif path == "/api/permissions/policy":
            self._handle_permissions_policy()
        elif path == "/api/skills":
            self._handle_skills_manage()
        elif path == "/api/skills/run":
            self._handle_skills_run()
        elif path == "/api/improvements/propose":
            self._handle_improvements_propose()
        elif path == "/api/improvements/decide":
            self._handle_improvements_decide()
        elif path == "/api/improvements/rollback":
            self._handle_improvements_rollback()
        elif path == "/api/updates/check":
            self._handle_updates_check()
        elif path == "/api/updates/stage":
            self._handle_updates_stage()
        elif path == "/api/updates/apply":
            self._handle_updates_apply()
        elif path == "/api/updates/rollback":
            self._handle_updates_rollback()
        elif path.startswith("/api/skills/"):
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use POST /api/skills.",
                    }
                },
                allow="POST",
            )
        elif path.startswith("/api/permissions/"):
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use POST /api/permissions/cap or "
                        "/api/permissions/policy.",
                    }
                },
                allow="POST",
            )
        elif path == "/api/vault/refresh":
            self._handle_vault_refresh()
        elif path == "/api/vault":
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use GET for /api/vault.",
                    }
                },
                allow="GET, HEAD",
            )
        elif path == "/api/tasks":
            self._handle_tasks_create()
        elif path == "/api/status":
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use GET for /api/status.",
                    }
                },
                allow="GET, HEAD",
            )
        elif path == "/api/metrics":
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use GET for /api/metrics.",
                    }
                },
                allow="GET, HEAD",
            )
        elif path == "/api/search":
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use GET for /api/search.",
                    }
                },
                allow="GET, HEAD",
            )
        elif path == "/api/conversations" or (
            path.startswith("/api/conversations/") and path.endswith("/messages")
        ):
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use GET for conversation resources.",
                    }
                },
                allow="GET, HEAD",
            )
        elif path.startswith("/api/tasks/"):
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use PATCH or DELETE on /api/tasks/{id}.",
                    }
                },
                allow="PATCH, DELETE",
            )
        elif path == "/api/models":
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use GET for /api/models.",
                    }
                },
                allow="GET, HEAD",
            )
        elif path == "/api/settings/model":
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use GET, PATCH or DELETE on /api/settings/model.",
                    }
                },
                allow="GET, HEAD, PATCH, DELETE",
            )
        elif path == "/api/settings":
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use PATCH on /api/settings.",
                    }
                },
                allow="GET, HEAD, PATCH",
            )
        elif path == "/api/settings/credentials":
            if self._rate_limit_gate("settings_probe"):
                return
            self._handle_settings_credentials_post()
        elif path == "/api/settings/test-connection":
            if self._rate_limit_gate("settings_probe"):
                return
            self._handle_settings_test_connection()
        elif path == "/api/settings/apply":
            if self._rate_limit_gate("settings"):
                return
            self._handle_settings_apply()
        elif path == "/api/settings/model-test":
            if self._rate_limit_gate("model_test"):
                return
            self._handle_settings_model_test()
        elif path == "/api/settings/catalog":
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use GET on /api/settings/catalog.",
                    }
                },
                allow="GET, HEAD",
            )
        elif path == "/api/goals":
            self._handle_goals_create()
        elif path.startswith("/api/goals/") and path.endswith("/cancel"):
            self._handle_goal_action(path, "cancel")
        elif path.startswith("/api/goals/") and path.endswith("/retry"):
            self._handle_goal_action(path, "retry")
        elif path.startswith("/api/goals/") and path.endswith("/approve"):
            self._handle_goal_approval(path, "approve")
        elif path.startswith("/api/goals/") and path.endswith("/reject"):
            self._handle_goal_approval(path, "reject")
        elif path.startswith("/api/goals/"):
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use POST /api/goals/cancel or "
                        "/api/goals/{id}/retry; other goal mutations "
                        "are not accepted.",
                    }
                },
                allow="GET, HEAD",
            )
        else:
            self._reject_method("POST")

    def do_PATCH(self):
        if self._route() == "/api/settings/model":
            self._settings_mutation_entry("PATCH")
            return
        if self._route() == "/api/settings":
            if self._rate_limit_gate("settings"):
                return
            self._handle_settings_patch()
            return
        self._task_mutation_entry("PATCH")

    def do_DELETE(self):
        if self._route() == "/api/settings/model":
            self._settings_mutation_entry("DELETE")
            return
        if self._route() == "/api/settings/credentials":
            if self._rate_limit_gate("settings_probe"):
                return
            self._handle_settings_credentials_delete()
            return
        self._task_mutation_entry("DELETE")

    def do_PUT(self):
        self._reject_method("PUT")

    def do_OPTIONS(self):
        self._reject_method("OPTIONS")

    def _task_mutation_entry(self, method):
        """PATCH/DELETE dispatch: task resources use the taxonomy 405s."""
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/api/tasks/"):
            self._handle_task_id(method)
            return
        if path.startswith("/api/settings/"):
            self._send_json(
                405,
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use PATCH /api/settings or "
                        "DELETE /api/settings/credentials?provider=….",
                    }
                },
                allow="GET, HEAD, PATCH, DELETE",
            )
            return
        allow = "GET, HEAD, POST" if path == "/api/tasks" else "GET, HEAD"
        self._send_json(
            405,
            {
                "error": {
                    "code": "method_not_allowed",
                    "message": (
                        "Use PATCH or DELETE on /api/tasks/{id} "
                        f"({method} applies to task resources only)."
                    ),
                }
            },
            allow=allow,
        )

    def _reject_method(self, method):
        self._send_json(405, {"error": "method not allowed"}, allow="GET, HEAD")
        if self.command != "HEAD":
            self.log_message("rejected %s %s", method, self._route())

    def _dispatch(self, send_body):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        try:
            if path == "/api/health":
                self._handle_health(send_body)
                return
            if path == "/api/status":
                self._handle_status(send_body)
                return
            if path == "/api/metrics":
                self._handle_metrics(send_body)
                return
            if path == "/api/search":
                self._handle_search(send_body)
                return
            if path == "/api/vault":
                if self._rate_limit_gate("vault_read"):
                    return
                self._handle_vault_get(send_body)
                return
            if path == "/api/vault/refresh":
                self._send_json(
                    405,
                    {
                        "error": {
                            "code": "method_not_allowed",
                            "message": "Use POST for /api/vault/refresh.",
                        }
                    },
                    send_body,
                    allow="POST",
                )
                return
            if path == "/api/tasks":
                self._handle_tasks_list(send_body)
                return
            if path.startswith("/api/tasks/"):
                # GET on a single task is not part of the contract; make
                # the supported methods explicit instead of a bare 404
                self._send_json(
                    405,
                    {
                        "error": {
                            "code": "method_not_allowed",
                            "message": "Use PATCH or DELETE on /api/tasks/{id}.",
                        }
                    },
                    send_body,
                    allow="PATCH, DELETE",
                )
                return
            if path == "/api/conversations":
                self._handle_conversations(send_body)
                return
            if path.startswith("/api/conversations/") \
                    and path.endswith("/messages"):
                conv_id = path[len("/api/conversations/"):-len("/messages")]
                self._handle_conversation_messages(conv_id, send_body)
                return
            if path == "/api/permissions":
                self._handle_permissions_view(send_body)
                return
            if path == "/api/skills":
                self._handle_skills_view(send_body)
                return
            if path == "/api/skills/runs":
                self._handle_skill_runs(send_body)
                return
            if path == "/api/improvements":
                self._handle_improvements_view(send_body)
                return
            if path == "/api/updates":
                self._handle_updates_view(send_body)
                return
            if path == "/api/skills/":
                pass  # fall through to the shared /api/skills guard
            elif path.startswith("/api/skills"):
                self._send_json(
                    405,
                    {
                        "error": {
                            "code": "method_not_allowed",
                            "message": "Use GET /api/skills.",
                        }
                    },
                    send_body,
                    allow="GET, HEAD",
                )
                return
            if path == "/api/permissions/audit":
                self._handle_permissions_audit(send_body)
                return
            if path.startswith("/api/permissions/"):
                self._send_json(
                    405,
                    {
                        "error": {
                            "code": "method_not_allowed",
                            "message": "Use GET /api/permissions or "
                            "/api/permissions/audit.",
                        }
                    },
                    send_body,
                    allow="GET, HEAD",
                )
                return
            if path == "/api/models":
                self._handle_models(send_body)
                return
            if path == "/api/settings/model":
                self._handle_settings_model_get(send_body)
                return
            if path == "/api/settings":
                if self._rate_limit_gate("settings"):
                    return
                self._handle_settings_view(send_body)
                return
            if path == "/api/settings/catalog":
                if self._rate_limit_gate("settings_probe"):
                    return
                self._handle_settings_catalog(send_body)
                return
            if path == "/api/goals":
                self._handle_goals_list(send_body)
                return
            if path.startswith("/api/goals/") \
                    and len(path) > len("/api/goals/") \
                    and "/" not in path[len("/api/goals/"):]:
                self._handle_goal_detail(
                    path[len("/api/goals/"):], send_body)
                return
            segments = path[len("/api/goals/"):].split("/")
            if len(segments) == 2 and segments[0] \
                    and segments[1] == "proposals":
                self._handle_goal_proposals(segments[0], None, send_body)
                return
            if len(segments) == 3 and segments[0] \
                    and segments[1] == "proposals":
                self._handle_goal_proposal_preview(
                    segments[0], segments[2], send_body)
                return
            if path == "/api/chat":
                self._send_json(
                    405,
                    {
                        "error": {
                            "code": "method_not_allowed",
                            "message": "Use POST for /api/chat.",
                        }
                    },
                    send_body,
                    allow="POST",
                )
                return
            if path.startswith("/api/") or path.startswith("/api"):
                self._send_json(404, {"error": "not found"}, send_body)
                return
            content_length = int(self.headers.get("Content-Length", "0") or 0)
            if content_length > MAX_BODY_BYTES:
                self._send_json(413, {"error": "payload too large"}, send_body)
                return
            target = find_static_file(path)
            if target is None:
                self._send_json(404, {"error": "not found"}, send_body)
                return
            self._send_file(target, send_body)
        except Exception as exc:
            self._log_failure("dispatch handler error", exc)
            self._send_json(500, {"error": "internal error"}, send_body=False)

    def _handle_health(self, send_body):
        ok, detail = db.check_ready(self.db_path)
        payload = {
            "ok": ok,
            "db": detail,
            "schema_version": db.SCHEMA_VERSION if ok else None,
            "uptime_seconds": round(time.time() - START_TIME, 3),
        }
        self._send_json(200, payload, send_body)

    # ---- GET /api/status (status visibility; no secrets, no network calls)

    def _handle_status(self, send_body):
        try:
            if self._reject_cross_origin():
                return
            ok, detail = db.check_ready(self.db_path)
            override = self._model_override()
            catalog = self._catalog_state() or {}
            last = getattr(self.server, "last_inference", None)
            self._send_json(
                200,
                {
                    "db": detail,
                    "schema_version": db.SCHEMA_VERSION if ok else None,
                    "uptime_seconds": round(time.time() - START_TIME, 3),
                    # model policy id only (JARVIS_MODEL); never the
                    # concrete upstream model chosen by routing
                    "model_policy": self.cfg.model,
                    # credential presence only; the key itself never
                    # leaves the server
                    "provider_configured": bool(
                        (self.cfg.api_key or "").strip()
                    ),
                    # Phase 7A: effective policy + its source, catalog
                    # freshness/failure state, and whether this process
                    # has actually completed an upstream inference (a
                    # catalog entry is never proof of inference).
                    "effective_model": override or self.cfg.model,
                    "model_source": "runtime_override" if override
                    else "environment_default",
                    "catalog": {
                        "count": len(catalog["models"])
                        if catalog.get("models") is not None else None,
                        "fetched_at": catalog.get("fetched_at"),
                        "error": catalog.get("error"),
                    },
                    "last_inference": dict(last) if last else None,
                },
                send_body,
            )
        except Exception as exc:
            self._log_failure("status handler error", exc)
            self._send_json(
                500, {"error": {"code": "internal_error",
                                "message": "Internal error."}}, send_body=False
            )

    # ---- GET /api/metrics (local telemetry; Phase 4) --------------------
    # stdlib sampling only: /proc/stat + /proc/meminfo + /proc/uptime +
    # os.statvfs on the data directory's filesystem. No network calls, no
    # provider calls, no secrets, no absolute paths in the response.
    # CPU needs two consecutive samples; the first answer marks itself
    # as sampling instead of inventing a percentage.

    def _handle_metrics(self, send_body):
        try:
            if self._reject_cross_origin():
                return
            self._send_json(200, self.metrics.collect(), send_body)
        except Exception as exc:
            self._log_failure("metrics handler error", exc)
            self._send_json(
                500, {"error": {"code": "internal_error",
                                "message": "Internal error."}}, send_body=False
            )

    # ---- GET /api/search (persisted records; Phase 5) -------------------
    # Literal substring search over tasks (title/description), stored
    # conversation titles and message content. Parameterized SQL with
    # escaped LIKE wildcards; results are bounded at the SQL level and
    # interleaved deterministically. Message search has no full-text
    # index: SQLite may scan the messages table (documented limitation,
    # fine at local scale — no FTS dependency without product demand).

    def _handle_search(self, send_body):
        try:
            if self._reject_cross_origin():
                return
            if self._rate_limit_gate("search"):
                return
            query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
            query = query.strip()
            if len(query) < SEARCH_MIN_QUERY_CHARS:
                self._send_json(
                    400,
                    {"error": {"code": "invalid_request",
                               "message": "Query must be at least "
                               f"{SEARCH_MIN_QUERY_CHARS} characters."}},
                    send_body,
                )
                return
            if len(query) > SEARCH_MAX_QUERY_CHARS:
                self._send_json(
                    400,
                    {"error": {"code": "invalid_request",
                               "message": f"Query exceeds "
                               f"{SEARCH_MAX_QUERY_CHARS} characters."}},
                    send_body,
                )
                return
            limit = DEFAULT_SEARCH_LIMIT
            raw_limit = parse_qs(
                urlsplit(self.path).query).get("limit", [None])[0]
            if raw_limit is not None:
                try:
                    limit = int(raw_limit)
                except ValueError:
                    limit = -1
                if not 1 <= limit <= MAX_SEARCH_LIMIT:
                    self._send_json(
                        400,
                        {"error": {"code": "invalid_request",
                                   "message": f"limit must be an "
                                   f"integer between 1 and "
                                   f"{MAX_SEARCH_LIMIT}."}},
                        send_body,
                    )
                    return
            conn = db.open_conn(self.db_path, readonly=True)
            try:
                pattern = db.like_pattern(query)
                tasks, t_trunc = db.search_tasks(conn, pattern, limit)
                messages, m_trunc = db.search_messages(
                    conn, pattern, limit)
                conversations, c_trunc = db.search_conversations(
                    conn, pattern, limit)
            finally:
                conn.close()
            results = []
            # round-robin interleave for deterministic category fairness;
            # per-row builders receive the trimmed query for snippet focus
            bases = (
                (tasks, self._task_search_result),
                (messages, self._message_search_result),
                (conversations, self._conversation_search_result),
            )
            best = max(len(tasks), len(messages), len(conversations))
            for i in range(best):
                for rows, fn in bases:
                    if i < len(rows):
                        results.append(fn(rows[i], query=query))
            truncated_any = t_trunc or m_trunc or c_trunc
            self._send_json(
                200,
                {
                    "query": query,
                    "results": results[:limit],
                    "count": min(len(results), limit),
                    "truncated": truncated_any,
                    "category_truncated": {
                        "tasks": t_trunc,
                        "messages": m_trunc,
                        "conversations": c_trunc,
                    },
                },
                send_body,
            )
        except Exception as exc:
            # class name only — unrestricted tracebacks could echo
            # untrusted input into logs
            self._log_failure("search handler error", exc)
            self._send_json(
                500, {"error": {"code": "internal_error",
                                "message": "Internal error."}}, send_body=False
            )

    @staticmethod
    def _clip(text, around, window=SEARCH_SNIPPET_CHARS):
        """A bounded plain-text snippet around the first match."""
        text = text or ""
        if around:
            idx = text.lower().find(around.lower())
            if idx < 0:
                idx = 0
        else:
            idx = 0
        start = max(0, idx - 30)
        prefix = "…" if start > 0 else ""
        return prefix + text[start:start + window]

    def _task_search_result(self, row, query=""):
        return {
            "type": "task",
            "id": row["id"],
            "title": row["title"],
            "snippet": self._clip(row["description"], query),
            "status": row["status"],
            "day": row["day"],
            "updated_at": row["updated_at"],
        }

    def _message_search_result(self, row, query=""):
        return {
            "type": "message",
            "conversation_id": row["conversation_id"],
            "conversation_title": row["conversation_title"],
            "snippet": self._clip(row["content"] or "", query),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _conversation_search_result(row, query=""):
        return {
            "type": "conversation",
            "id": row["id"],
            "title": row["title"],
            "updated_at": row["updated_at"],
        }

    # ---- GET /api/conversations (read-only, same-origin) ----------------

    def _conversation_guard(self, send_body):
        """Same-origin policy, shared with /api/chat. True = rejected."""
        if self._reject_cross_origin():
            return True
        return False

    def _handle_conversations(self, send_body):
        try:
            if self._conversation_guard(send_body):
                return
            limit = DEFAULT_CONVERSATIONS_LIMIT
            query = urlsplit(self.path).query
            if query:
                params = parse_qs(query)
                raw_limit = params.get("limit", [None])[0]
                if raw_limit is not None:
                    try:
                        limit = int(raw_limit)
                    except ValueError:
                        limit = -1
                    if not 1 <= limit <= MAX_CONVERSATIONS_LIMIT:
                        self._send_json(
                            400,
                            {
                                "error": {
                                    "code": "invalid_request",
                                    "message": (
                                        "limit must be an integer between "
                                        f"1 and {MAX_CONVERSATIONS_LIMIT}."
                                    ),
                                }
                            },
                            send_body,
                        )
                        return
            # fetch one extra row to report truncation without a second query
            conn = db.open_conn(self.db_path, readonly=True)
            try:
                rows = db.list_conversations(conn, limit + 1)
            finally:
                conn.close()
            truncated = len(rows) > limit
            rows = rows[:limit]
            conversations = []
            for row in rows:
                preview_text = row["last_user"] or row["title"] or ""
                conversations.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "preview": preview_text[:PREVIEW_CHARS],
                        "message_count": row["message_count"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                )
            self._send_json(
                200,
                {
                    "conversations": conversations,
                    "count": len(conversations),
                    "truncated": truncated,
                },
                send_body,
            )
        except Exception as exc:
            self._log_failure("conversations handler error", exc)
            self._send_json(
                500, {"error": {"code": "internal_error",
                                "message": "Internal error."}}, send_body=False
            )

    # ---- GET /api/conversations/{id}/messages ---------------------------

    def _handle_conversation_messages(self, conversation_id, send_body):
        try:
            if self._conversation_guard(send_body):
                return
            if not isinstance(conversation_id, str) \
                    or len(conversation_id) != 32 \
                    or not set(conversation_id) <= CONV_ID_CHARS:
                self._send_json(
                    400,
                    {
                        "error": {
                            "code": "invalid_conversation",
                            "message": "Malformed conversation id.",
                        }
                    },
                    send_body,
                )
                return
            window = DEFAULT_MESSAGE_WINDOW
            query = urlsplit(self.path).query
            if query:
                params = parse_qs(query)
                raw_window = params.get("window", [None])[0]
                if raw_window is not None:
                    try:
                        window = int(raw_window)
                    except ValueError:
                        window = -1
                    if not 1 <= window <= MAX_MESSAGE_WINDOW:
                        self._send_json(
                            400,
                            {
                                "error": {
                                    "code": "invalid_request",
                                    "message": (
                                        "window must be an integer between "
                                        f"1 and {MAX_MESSAGE_WINDOW}."
                                    ),
                                }
                            },
                            send_body,
                        )
                        return
            conn = db.open_conn(self.db_path, readonly=True)
            try:
                if not db.conversation_exists(conn, conversation_id):
                    self._send_json(
                        404,
                        {
                            "error": {
                                "code": "conversation_not_found",
                                "message": "Unknown conversation.",
                            },
                            "conversation_id": conversation_id,
                        },
                        send_body,
                    )
                    return
                rows, total = db.get_message_window(conn, conversation_id,
                                                    window)
            finally:
                conn.close()
            self._send_json(
                200,
                {
                    "conversation_id": conversation_id,
                    "messages": [
                        {
                            "id": row["id"],
                            "role": row["role"],
                            "content": row["content"],
                            "created_at": row["created_at"],
                        }
                        for row in rows
                    ],
                    "messages_total": total,
                    "older_omitted": total > len(rows),
                },
                send_body,
            )
        except Exception as exc:
            self._log_failure("conversation messages handler error", exc)
            self._send_json(
                500, {"error": {"code": "internal_error",
                                "message": "Internal error."}}, send_body=False
            )

    # ---- task endpoints (Phase 3D) ---------------------------------------

    def _task_error(self, status, code, message):
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _task_id_of(self, path):
        task_id = path[len("/api/tasks/"):]
        return task_id if task_id and all(
            ch in CONV_ID_CHARS for ch in task_id
        ) and len(task_id) == 32 else None

    def _read_json_payload(self, max_bytes):
        """Validate Content-Type/Length/size and parse a JSON object.

        Sends the taxonomy error and returns None when the request is
        unusable; returns the parsed dict otherwise. Shared body
        protections mirror the /api/chat handler.
        """
        content_type = (self.headers.get("Content-Type") or "")
        content_type = content_type.split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._task_error(
                415, "unsupported_media_type", "Content-Type must be application/json."
            )
            return None
        try:
            content_length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            content_length = -1
        if content_length < 0:
            self._task_error(400, "invalid_request", "Invalid Content-Length.")
            return None
        if content_length > max_bytes:
            self._task_error(413, "payload_too_large", "Request body too large.")
            return None
        raw = self.rfile.read(content_length) if content_length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._task_error(400, "invalid_json", "Request body is not valid JSON.")
            return None
        if not isinstance(payload, dict):
            self._task_error(400, "invalid_request", "Payload must be a JSON object.")
            return None
        return payload

    def _handle_tasks_list(self, send_body):
        try:
            if self._reject_cross_origin():
                return
            limit = DEFAULT_TASKS_LIMIT
            query = urlsplit(self.path).query
            if query:
                params = parse_qs(query)
                raw_limit = params.get("limit", [None])[0]
                if raw_limit is not None:
                    try:
                        limit = int(raw_limit)
                    except ValueError:
                        limit = -1
                    if not 1 <= limit <= MAX_TASKS_LIMIT:
                        self._send_json(
                            400,
                            {
                                "error": {
                                    "code": "invalid_request",
                                    "message": (
                                        "limit must be an integer between "
                                        f"1 and {MAX_TASKS_LIMIT}."
                                    ),
                                }
                            },
                            send_body,
                        )
                        return
            conn = db.open_conn(self.db_path, readonly=True)
            try:
                rows = db.list_tasks(conn, limit + 1)
                # exact global counts by status/day (one aggregate query
                # each); `truncated` only flags the paged list, never these
                status_counts = db.task_status_counts(conn)
                day_counts = db.task_day_counts(conn)
            finally:
                conn.close()
            truncated = len(rows) > limit
            tasks = rows[:limit]
            self._send_json(
                200,
                {
                    "tasks": tasks,
                    "count": len(tasks),
                    "truncated": truncated,
                    "status_counts": status_counts,
                    "day_counts": day_counts,
                },
                send_body,
            )
        except Exception as exc:
            self._log_failure("tasks list handler error", exc)
            self._send_json(
                500, {"error": {"code": "internal_error",
                                "message": "Internal error."}}, send_body=False
            )

    def _task_fields_common(self, payload):
        """Validate creation/patch task fields; returns (errors, values)."""
        errors = {}
        values = {}
        title = payload.get("title")
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                errors["title"] = "'title' must be a non-empty string."
            elif len(title.strip()) > MAX_TASK_TITLE_CHARS:
                errors["title"] = (
                    f"'title' exceeds {MAX_TASK_TITLE_CHARS} characters."
                )
            else:
                values["title"] = title.strip()
        description = payload.get("description")
        if description is not None:
            if not isinstance(description, str):
                errors["description"] = "'description' must be a string."
            elif len(description) > MAX_TASK_DESC_CHARS:
                errors["description"] = (
                    f"'description' exceeds {MAX_TASK_DESC_CHARS} characters."
                )
            else:
                values["description"] = description.strip()
        status = payload.get("status")
        if status is not None:
            if not isinstance(status, str) or status not in TASK_STATUSES:
                errors["status"] = (
                    "'status' must be one of: queued, warn, done, planned."
                )
            else:
                values["status"] = status
        return errors, values

    def _handle_tasks_create(self):
        try:
            if self._reject_cross_origin():
                return
            if self._rate_limit_gate("tasks_write"):
                return
            payload = self._read_json_payload(MAX_TASK_BODY_BYTES)
            if payload is None:
                return
            if set(payload) - TASK_CREATE_KEYS:
                self._task_error(400, "invalid_request",
                                 "Unsupported request fields.")
                return
            errors, values = self._task_fields_common(payload)
            if "title" not in values and "title" not in errors:
                errors["title"] = "'title' is required."
            priority = payload.get("priority")
            if priority is not None:
                if not isinstance(priority, str) or priority not in TASK_PRIORITIES:
                    errors["priority"] = ("'priority' must be one of: "
                                          "high, std.")
                else:
                    values["priority"] = priority
            time_label = payload.get("time")
            if time_label is not None:
                if not isinstance(time_label, str) \
                        or len(time_label) > MAX_TASK_TIME_CHARS:
                    errors["time"] = (
                        f"'time' must be a string of at most "
                        f"{MAX_TASK_TIME_CHARS} characters."
                    )
                else:
                    values["time_label"] = time_label.strip()
            day = payload.get("day")
            if day is not None:
                if not isinstance(day, str) or day not in TASK_DAYS:
                    errors["day"] = "'day' must be one of: today, tomorrow."
                else:
                    values["day"] = day
            if errors:
                self._send_json(
                    400,
                    {"error": {
                        "code": "invalid_task",
                        "message": "; ".join(
                            f"{field}: {msg}" for field, msg in errors.items()
                        ),
                    }},
                )
                return
            conn = db.open_conn(self.db_path)
            try:
                task = db.create_task(
                    conn,
                    title=values["title"],
                    description=values.get("description", ""),
                    status=values.get("status", "queued"),
                    priority=values.get("priority", "std"),
                    time_label=values.get("time_label", ""),
                    day=values.get("day", "today"),
                )
            finally:
                conn.close()
            self._send_json(201, task)
        except Exception as exc:
            self._log_failure("task create handler error", exc)
            self._send_json(
                500, {"error": {"code": "internal_error",
                                "message": "Internal error."}}
            )

    def _task_not_found(self, send_body=True):
        self._send_json(
            404,
            {
                "error": {
                    "code": "task_not_found",
                    "message": "Unknown task.",
                }
            },
            send_body,
        )

    def _handle_task_id(self, method):
        """PATCH /api/tasks/{id} and DELETE /api/tasks/{id}."""
        try:
            if self._reject_cross_origin():
                return
            if self._rate_limit_gate("tasks_write"):
                return
            task_id = self._task_id_of(self.path.split("?", 1)[0])
            if task_id is None:
                self._task_error(400, "invalid_task",
                                 "Task id must be 32 lowercase hex characters.")
                return
            if method == "DELETE":
                conn = db.open_conn(self.db_path)
                try:
                    deleted = db.delete_task(conn, task_id)
                finally:
                    conn.close()
                if not deleted:
                    self._task_not_found()
                    return
                self._send_json(200, {"deleted": True, "id": task_id})
                return
            # PATCH
            payload = self._read_json_payload(MAX_TASK_BODY_BYTES)
            if payload is None:
                return
            if set(payload) - TASK_PATCH_KEYS:
                self._task_error(400, "invalid_request",
                                 "Unsupported request fields.")
                return
            if not payload:
                self._task_error(400, "empty_update",
                                 "Provide at least one field to update.")
                return
            errors, values = self._task_fields_common(payload)
            if errors:
                self._send_json(
                    400,
                    {"error": {
                        "code": "invalid_task",
                        "message": "; ".join(
                            f"{field}: {msg}" for field, msg in errors.items()
                        ),
                    }},
                )
                return
            if not values:
                self._task_error(400, "empty_update",
                                 "Provide at least one field to update.")
                return
            conn = db.open_conn(self.db_path)
            try:
                task = db.update_task(conn, task_id, values)
            finally:
                conn.close()
            if task is None:
                self._task_not_found()
                return
            self._send_json(200, task)
        except Exception as exc:
            self._log_failure("task mutation handler error", exc)
            self._send_json(
                500, {"error": {"code": "internal_error",
                                "message": "Internal error."}}
            )

    def _handle_chat(self):
        self._chat_conversation_id = None
        try:
            self._chat_inner()
        except provider.ProviderError as exc:
            self._send_chat_error(
                exc.http_status, exc.code, exc.user_message,
                self._chat_conversation_id,
                attempts=getattr(exc, "attempts", None),
            )
        except Exception as exc:
            self._log_failure("chat handler error", exc)
            self._send_json(
                500,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "Internal error.",
                    }
                },
            )

    def _chat_bad_request(self, code, message):
        self._send_json(400, {"error": {"code": code, "message": message}})

    def _send_chat_error(self, status, code, message, conversation_id,
                         attempts=None):
        payload = {"error": {"code": code, "message": message}}
        if conversation_id is not None:
            payload["conversation_id"] = conversation_id
        if attempts:
            # Phase 8A observability: safe codes + model ids only.
            payload["attempts"] = [
                {"model": item["model"], "code": item["code"]}
                for item in attempts
            ]
        self._send_json(status, payload)

    def _chat_inner(self):
        # One snapshot per request: a concurrent settings change can
        # never split this chat lifecycle across provider/model policies.
        cfg = self.cfg
        # 429 must be answered before body reads or DB/provider work
        if self._rate_limit_gate("chat"):
            return
        if self._reject_cross_origin():
            return
        if not (cfg.api_key or "").strip():
            raise provider.NotConfigured()
        payload = self._read_json_payload(MAX_CHAT_BODY_BYTES)
        if payload is None:
            return
        unknown = set(payload) - CHAT_ALLOWED_KEYS
        if unknown:
            self._chat_bad_request(
                "invalid_request",
                "Unknown request fields: " + ", ".join(sorted(unknown))
                + ".",
            )
            return
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            self._chat_bad_request(
                "invalid_message",
                "'message' must be a non-empty string.",
            )
            return
        if len(message) > MAX_MESSAGE_CHARS:
            self._chat_bad_request(
                "message_too_long",
                f"'message' exceeds {MAX_MESSAGE_CHARS} characters.",
            )
            return

        conversation_id = payload.get("conversation_id")
        if conversation_id is not None:
            if not isinstance(conversation_id, str) \
                    or not conversation_id.strip():
                self._chat_bad_request(
                    "invalid_conversation",
                    "'conversation_id' must be a non-empty string or null.",
                )
                return
            conversation_id = conversation_id.strip()

        # Persist user message first (short transaction, closed before the
        # network call) so it survives a provider failure.
        conn = db.open_conn(cfg.db_path)
        try:
            if conversation_id is not None:
                if not db.conversation_exists(conn, conversation_id):
                    self._send_json(
                        404,
                        {
                            "error": {
                                "code": "conversation_not_found",
                                "message": "Unknown conversation.",
                            },
                            "conversation_id": conversation_id,
                        },
                    )
                    return
            else:
                conversation_id = db.create_conversation(
                    conn, title=message[:60]
                )
            db.add_message(conn, conversation_id, "user", message)
            history = [
                {"role": row["role"], "content": row["content"]}
                for row in db.get_recent_messages(
                    conn, conversation_id, cfg.history_messages
                )
            ]
        finally:
            conn.close()

        total = sum(len(item["content"]) for item in history)
        while len(history) > 1 and total > MAX_HISTORY_CHARS:
            total -= len(history[0]["content"])
            history.pop(0)

        # One effective model policy per request, resolved once up front
        # so a concurrent settings change can never split this lifecycle.
        # Precedence (Phase 8A): a runtime override is an explicit pin —
        # failures surface as their own taxonomy error and the fallback
        # policy is NOT silently substituted. Without an override, the
        # environment primary (JARVIS_MODEL) gets at most ONE fallback
        # attempt (JARVIS_FALLBACK_MODEL) on transient/per-model
        # failures; the user message stays persisted exactly once and
        # the assistant message is stored only after a successful call.
        override = self._model_override()
        primary = override or cfg.model
        fallback_policy = "" if override else cfg.fallback_model
        self._chat_conversation_id = conversation_id
        result = provider.complete_with_fallback(
            history,
            base_url=cfg.api_base,
            api_key=cfg.api_key,
            primary_model=primary,
            fallback_model=fallback_policy,
            timeout_seconds=cfg.timeout_seconds,
            max_tokens=cfg.max_tokens,
        )
        reply = result["reply"]
        model = result["model"]
        if result["fallback_used"] and result["attempts"]:
            applog.event(
                "model_fallback",
                request_id=getattr(self, "request_id", None),
                primary_model=primary,
                primary_code=result["attempts"][0]["code"],
                fallback_model=model,
                outcome="fallback_ok",
            )

        conn = db.open_conn(cfg.db_path)
        try:
            db.add_message(conn, conversation_id, "assistant", reply)
        finally:
            conn.close()

        setattr(
            self.server,
            "last_inference",
            {"model": model, "at": time.time()},
        )
        self._send_json(
            200,
            {
                "conversation_id": conversation_id,
                "reply": reply,
                "model": model,
            },
        )

    def _reject_cross_origin(self):
        """Reject browser cross-origin requests.

        Requests without an Origin header (curl, scripts, the test suite)
        pass. A loopback Origin with the exact same origin as the request
        Host header is what the page itself sends; anything else gets a
        403 taxonomy response. Returns True when a 403 was already sent.
        """
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return False
        try:
            parts = urlsplit(origin)
        except ValueError:
            parts = None
        host = (parts.hostname or "").lower() if parts else ""
        if host in ("127.0.0.1", "localhost", "::1") \
                and (parts.netloc or "").lower() \
                == (self.headers.get("Host") or "").lower():
            return False
        self._send_json(
            403,
            {
                "error": {
                    "code": "cross_origin_forbidden",
                    "message": "Cross-origin requests are not allowed.",
                }
            },
        )
        applog.event(
            "origin_rejected",
            request_id=getattr(self, "request_id", None),
            route=self._route() if getattr(self, "path", None) else None,
        )
        return True

    def _send_file(self, target, send_body):
        ctype = CONTENT_TYPES.get(target.suffix.lower(),
                                  "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _send_json(self, code, payload, send_body=True, allow=None,
                   retry_after=None):
        body = b"" if not send_body else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if allow:
            self.send_header("Allow", allow)
        if retry_after is not None:
            self.send_header("Retry-After", str(int(retry_after)))
        rid = getattr(self, "request_id", None)
        if rid:
            self.send_header("X-Request-ID", rid)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    # ---- Phase 7A provider/model management -----------------------------
    # Catalog fetches go exclusively through provider.list_models; the
    # response bodies never reach clients and credentials never leave
    # this module.

    def _model_override(self):
        """Validated persisted override or None (invalid values ignored)."""
        try:
            conn = db.open_conn(self.cfg.db_path)
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='model_override'"
                ).fetchone()
            finally:
                conn.close()
        except Exception as exc:
            self._log_failure("model override read error", exc)
            return None
        if row is None:
            return None
        value = row[0]
        if isinstance(value, str):
            value = value.strip()
            if value and len(value) <= 128 \
                    and not any(ch.isspace() for ch in value):
                return value
        applog.event(
            "model_override_invalid",
            request_id=getattr(self, "request_id", None),
            note="persistent model override failed validation; "
                 "environment default restored",
        )
        return None

    def _effective_model(self):
        """Effective model for this moment: override if valid, else env."""
        return self._model_override() or self.cfg.model

    def _catalog_state(self):
        """Return the per-server catalog cache dict (or None)."""
        cache = getattr(self.server, "model_catalog", None)
        return cache if isinstance(cache, dict) else None

    def _catalog_fresh_models(self):
        """Fresh cached catalog entries, or None when a fetch is needed."""
        cache = self._catalog_state()
        if cache is None or cache.get("models") is None:
            return None
        if time.time() - cache.get("fetched_at", 0.0) \
                > CATALOG_FRESH_SECONDS:
            return None
        return cache["models"]

    def _refresh_catalog(self):
        """Fetch the catalog once and store it; raises ProviderError.

        On failure the previously fetched entries are kept in the cache
        and the taxonomy code is recorded for honest status reporting.
        Phase 11A: delegated to the provider-aware fetch (pricing
        classification for OrcaRouter included)."""
        return self._refresh_catalog_for(
            self.cfg.provider or "freellmapi", with_pricing=True)

    def _handle_models(self, send_body=True):
        """GET /api/models — authenticated catalog from the gateway.

        Served from a small per-server cache (CATALOG_FRESH_SECONDS); a
        live gateway fetch happens only when the cache is stale/empty or
        the client explicitly asks (?refresh=1), i.e. on explicit user
        action. Never runs inference.
        """
        if self._rate_limit_gate("models"):
            return
        if self._reject_cross_origin():
            return
        try:
            if not (self.cfg.api_key or "").strip():
                self._send_json(
                    503,
                    {
                        "error": {
                            "code": "not_configured",
                            "message": "AI provider is not configured "
                                       "(missing credential).",
                        }
                    },
                    send_body,
                )
                return
            force_refresh = bool(
                parse_qs(urlsplit(self.path).query).get("refresh")
            )
            fresh_models = (
                None if force_refresh else self._catalog_fresh_models()
            )
            if fresh_models is None:
                try:
                    fresh_models = self._refresh_catalog()
                except provider.ProviderError as exc:
                    cache = self._catalog_state()
                    if cache is not None and cache.get("models") is None:
                        cache["error"] = exc.code
                    self._send_json(
                        exc.http_status,
                        {
                            "error": {
                                "code": exc.code,
                                "message": exc.user_message,
                            }
                        },
                        send_body,
                    )
                    return
            cache = self._catalog_state()
            self._send_json(
                200,
                {
                    "models": fresh_models,
                    "fetched_at": cache.get("fetched_at")
                    if cache else None,
                },
                send_body,
            )
        except Exception as exc:
            self._log_failure("model catalog error", exc)
            self._send_json(
                500,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "Internal error.",
                    }
                },
                send_body=False,
            )

    def _handle_settings_model_get(self, send_body=True):
        """GET /api/settings/model — current policy; no network calls."""
        if self._reject_cross_origin():
            return
        override = self._model_override()
        catalog = self._catalog_state() or {}
        self._send_json(
            200,
            {
                "selected": self._effective_model(),
                "model_policy": self.cfg.model,
                "fallback": self.cfg.fallback_model or None,
                "source": "runtime_override" if override
                else "environment_default",
                "provider_configured": bool(
                    (self.cfg.api_key or "").strip()
                ),
                "catalog": {
                    "fetched_at": catalog.get("fetched_at"),
                    "error": catalog.get("error"),
                },
            },
            send_body,
        )

    def _settings_mutation_entry(self, method):
        """PATCH/DELETE /api/settings/model entry: gates then dispatch."""
        if self._rate_limit_gate("model_settings"):
            return
        if self._reject_cross_origin():
            return
        if method == "PATCH":
            self._handle_settings_model_patch()
        else:
            self._clear_model_override()

    def _handle_settings_model_patch(self):
        """PATCH /api/settings/model — validate and apply a selection.

        Phase 11A: on a metered provider (OrcaRouter) a paid or
        unknown-cost model needs confirm_cost: true, exactly as the
        settings-center apply path requires."""
        payload = self._read_json_payload(MAX_SETTINGS_BODY_BYTES)
        if payload is None:
            return
        unknown = set(payload) - {"model", "confirm_cost"}
        if unknown:
            self._send_json(
                400,
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "Unknown request fields: "
                        + ", ".join(sorted(unknown)) + ".",
                    }
                },
            )
            return
        selected = payload.get("model")
        if isinstance(selected, str) \
                and selected.strip() \
                and any(ch.isspace() for ch in selected):
            self._send_json(
                400,
                {
                    "error": {
                        "code": "invalid_model",
                        "message": "Model identifier must not contain "
                                   "whitespace.",
                    }
                },
            )
            return
        if not isinstance(selected, str) or not selected.strip() \
                or len(selected) > 128:
            self._send_json(
                400,
                {
                    "error": {
                        "code": "invalid_model",
                        "message": "Model must be a non-empty identifier "
                                   "of at most 128 characters.",
                    }
                },
            )
            return
        selected = selected.strip()
        # The environment default and the gateway's own 'auto' routing
        # alias are always acceptable; anything else must come from the
        # catalog as currently available.
        if selected not in (self.cfg.model, "auto"):
            fresh = self._catalog_fresh_models()
            if fresh is None:
                # Applying a selection is an explicit user action, so one
                # validating fetch against the just-configured gateway is
                # justified; failures keep the previous policy untouched.
                try:
                    fresh = self._refresh_catalog()
                except provider.ProviderError as exc:
                    self._send_json(
                        exc.http_status,
                        {
                            "error": {
                                "code": exc.code,
                                "message": exc.user_message,
                            }
                        },
                    )
                    return
            available = [
                m["id"] for m in fresh if m.get("available")
            ]
            if selected not in available:
                self._send_json(
                    400,
                    {
                        "error": {
                            "code": "model_not_available",
                            "message": "Model '" + selected + "' is not "
                                       "available in the current provider "
                                       "catalog.",
                        }
                    },
                )
                return
        # metered provider: paid/unknown-cost selections need one
        # explicit confirmation (Phase 11A)
        if self.cfg.provider == "orcarouter" \
                and payload.get("confirm_cost") is not True:
            pricing_class = None
            svc = self._settings_service()
            if svc:
                try:
                    fresh = self._catalog_fresh_for(
                        "orcarouter", max_age=CATALOG_FRESH_SECONDS)
                    if fresh is None:
                        fresh = self._refresh_catalog_for(
                            "orcarouter", with_pricing=True)
                    pricing_class = self._pricing_class_of(fresh, selected)
                except provider.ProviderError as exc:
                    pricing_class = "unknown"
            pricing_class = pricing_class or "unknown"
            confirm_err = self._cost_confirmation_required(
                "orcarouter", pricing_class, False)
            self._send_json(
                confirm_err.status,
                {"error": {"code": confirm_err.code,
                           "message": confirm_err.message,
                           "pricing_class": pricing_class}},
            )
            return
        try:
            conn = db.open_conn(self.cfg.db_path)
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO meta (key, value) "
                        "VALUES ('model_override', ?)",
                        (selected,),
                    )
            finally:
                conn.close()
        except Exception as exc:
            self._log_failure("model override write error", exc)
            self._send_json(
                500,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "Failed to persist the model "
                                   "selection.",
                    }
                },
            )
            return
        self._send_json(
            200,
            {
                "applied": True,
                "selected": selected,
                "source": "runtime_override",
            },
        )

    def _clear_model_override(self):
        """DELETE /api/settings/model — back to the environment default."""
        try:
            conn = db.open_conn(self.cfg.db_path)
            try:
                with conn:
                    conn.execute(
                        "DELETE FROM meta WHERE key='model_override'"
                    )
            finally:
                conn.close()
        except Exception as exc:
            self._log_failure("model override clear error", exc)
            self._send_json(
                500,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "Failed to clear the model override.",
                    }
                },
            )
            return
        self._send_json(
            200,
            {
                "cleared": True,
                "selected": self.cfg.model,
                "source": "environment_default",
            },
        )

    # ---- Phase 11A configuration center --------------------------------
    # One explicit, validated path touches an active setting. The key
    # never leaves a write-only surface; catalog fetches never infer.

    def _settings_service(self):
        return getattr(self.server, "settings_service", None)

    def _provider_catalog_state(self, provider_name):
        catalogs = getattr(self.server, "provider_catalogs", None)
        if not isinstance(catalogs, dict):
            catalogs = {"freellmapi": self.server.model_catalog,
                        "orcarouter": {}}
        state = catalogs.get(provider_name)
        if not isinstance(state, dict):
            state = {"models": None, "fetched_at": 0.0, "error": None}
            catalogs[provider_name] = state
        return state

    def _profile_env_key_name(self, profile_name):
        return "FREELLMAPI_API_KEY" if profile_name == "freellmapi" \
            else "JARVIS_ORCAROUTER_API_KEY"

    def _profile_active_key(self, profile_name):
        """Active credential for a profile: environment first (its
        precedence), then the user store. Value never leaves the
        process; only this helper touches it."""
        svc = self._settings_service()
        env_name = self._profile_env_key_name(profile_name)
        env_value = (getattr(svc, "environ", os.environ).get(env_name, "")
                     or "").strip() if svc else ""
        if env_value:
            return env_value
        if svc:
            return svc.store.stored_key(profile_name)
        return ""

    def _profile_base_url(self, profile_name, summary=False):
        profile = provider.PROVIDER_PROFILES[profile_name]
        if profile_name == "freellmapi":
            # the loopback gateway keeps its (env-or-default) base
            svc = self._settings_service()
            if svc and summary:
                return profile["base_url"]
            return self.cfg.api_base
        return profile["base_url"]

    def _settings_view(self):
        """Complete, secret-free settings state for the UI."""
        svc = self._settings_service()
        cfg = self.cfg
        base = svc.base_cfg if svc else cfg
        managed = base.env_managed
        stored = svc.store.load() if svc else {}
        profiles = []
        for pid, profile in provider.PROVIDER_PROFILES.items():
            key_source = svc.store.resolve_key_source(pid, base) if svc else ""
            profiles.append({
                "id": pid,
                "display": profile["display"],
                "base_url": self._profile_base_url(pid, summary=True),
                "key_configured": bool(self._profile_active_key(pid)),
                "key_source": key_source,
                "key_locked": key_source == "environment",
            })
        override = self._model_override()
        if override:
            model, model_source = override, "runtime_override"
        elif "model" in managed:
            model, model_source = base.model, "environment_default"
        elif stored.get("model"):
            model, model_source = stored["model"], "user_config"
        elif cfg.model == provider.PROVIDER_DEFAULT_MODEL.get(cfg.provider):
            model, model_source = cfg.model, "provider_default"
        else:
            model, model_source = cfg.model, "environment_default"
        fallback_env = "fallback_model" in managed
        fallback = base.fallback_model if fallback_env \
            else (stored.get("fallback_model") or "")
        fallback_source = ("environment_default" if fallback_env
                           else ("user_config" if fallback else "default"))
        catalog_state = self._catalog_state() or {}
        reviewer_value = base.reviewer if "reviewer" in managed else \
            (stored.get("reviewer", "") or "")
        reviewer_model = cfg.reviewer_model if "reviewer_model" in managed \
            else (stored.get("reviewer_model", "") or "")
        vault_service = getattr(self.server, "vault_service", None)
        vault_state = vault_service.status() if vault_service else None
        return {
            "provider": {
                "active": cfg.provider,
                "profiles": profiles,
            },
            "model": {
                "selected": self._effective_model(),
                "policy": model,
                "source": model_source,
                "fallback": fallback or None,
                "fallback_source": fallback_source,
                "provider_default": provider.PROVIDER_DEFAULT_MODEL.get(
                    cfg.provider),
                "model_locked": "model" in managed,
                "fallback_locked": fallback_env,
            },
            "runtime": {
                "timeout_seconds": cfg.timeout_seconds,
                "max_tokens": cfg.max_tokens,
                "history_messages": cfg.history_messages,
                "timeout_locked": "timeout_seconds" in managed,
                "max_tokens_locked": "max_tokens" in managed,
                "history_locked": "history_messages" in managed,
            },
            "reviewer": {
                "value": reviewer_value,
                "model": reviewer_model,
                "model_locked": "reviewer_model" in managed,
                "locked": "reviewer" in managed,
            },
            "catalog": {
                "fetched_at": catalog_state.get("fetched_at"),
                "count": len(catalog_state["models"])
                if catalog_state.get("models") is not None else None,
                "error": catalog_state.get("error"),
            },
            "system": {
                "host": cfg.host,
                "port": cfg.port,
                "db_path": str(cfg.db_path),
                "workspace_root": str(cfg.workspace_root),
                "reviewer_path": self._reviewer_path_hint(base),
            },
            "vault": {
                "configured": bool(vault_state and vault_state["configured"]),
                "source": vault_state["source"] if vault_state else "",
                "vault_name": vault_state["vault_name"]
                if vault_state else None,
                "vault_exists": vault_state["vault_exists"]
                if vault_state else None,
                "index": vault_state["index"] if vault_state else None,
                "configured_path": (svc.store.load().get("vault_path")
                                    or "") if svc else "",
                "locked": "vault_path" in managed,
            },
        }

    @staticmethod
    def _reviewer_path_hint(base):
        # never a filesystem.executable target — display only, env-locked
        return {"configured": bool(base.reviewer_path),
                "source": "environment_default"}

    # ---- Phase 13A: Obsidian vault (read-only) -------------------------

    def _vault_service(self):
        return getattr(self.server, "vault_service", None)

    def _vault_error(self, exc, send_body=True):
        """Map typed vault failures onto the honest JSON error taxonomy.
        Never leaks exception text beyond the typed safe message."""
        applog.event("vault_error", code=exc.code)
        status = {"vault_not_configured": 400,
                  "vault_unavailable": 503,
                  "invalid_path": 400,
                  "no_such_path": 400,
                  "not_a_directory": 400,
                  "path_forbidden": 400,
                  "invalid_query": 400,
                  "no_such_note": 404,
                  "not_a_note": 400,
                  "unreadable_note": 503,
                  "binary_content": 400}.get(exc.code, 500)
        self._send_json(status, {"error": {"code": exc.code,
                                           "message": str(exc)}},
                        send_body)

    def _handle_vault_get(self, send_body=True):
        """GET /api/vault — connection + index status. Secret-free: the
        vault is identified by NAME only, never by absolute path."""
        if self._reject_cross_origin():
            return
        service = self._vault_service()
        if service is None:
            self._send_json(200, {"configured": False, "source": "",
                                  "vault_name": None, "vault_exists": None,
                                  "index": None}, send_body)
            return
        try:
            self._send_json(200, service.status(), send_body)
        except Exception as exc:
            self._log_failure("vault status error", exc)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}},
                            send_body)

    def _handle_vault_refresh(self):
        """POST /api/vault/refresh — explicit user-triggered index
        refresh. The configured root is validated with the same rules
        as the tools; a failed refresh preserves the previous index."""
        if self._reject_cross_origin():
            return
        if self._rate_limit_gate("vault_refresh"):
            return
        service = self._vault_service()
        if service is None:
            self._send_json(
                400, {"error": {"code": "vault_not_configured",
                                "message": "No vault is configured."}})
            return
        try:
            summary = service.scan()
        except vault_mod.VaultError as exc:
            self._vault_error(exc)
            return
        except Exception as exc:
            self._log_failure("vault refresh error", exc)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Failed to refresh"
                                            " the vault index."}})
            return
        applog.event("vault_refreshed", status=summary["status"],
                     notes=summary["notes"])
        self._send_json(200, summary)

    def _errors(self, err):
        applog.event("settings_rejected", code=err.code)
        self._send_json(err.status,
                        {"error": {"code": err.code,
                                   "message": err.message}})

    # ------------------------------------------------------------------
    # Permission engine (central authority; UI = Settings > Permissions)

    def _permission_engine(self):
        return permissions_mod.PermissionEngine(self.db_path)

    def _permission_tool_rows(self, engine):
        """Declared registry inventory joined with persisted policy and
        the effective availability under the current cap. Unregistered-
        state policy rows are reported as orphaned, never applied."""
        rows = []
        worker = getattr(self.server, "goal_worker", None)
        reg = getattr(worker, "registry", None) if worker else None
        tool_list = reg.all() if reg is not None else []
        conn = None
        try:
            conn = db.open_conn(self.db_path, readonly=True)
            policy_rows = conn.execute(
                "SELECT tool_name, state FROM permission_policies"
            ).fetchall()
        except Exception:
            policy_rows = []
        finally:
            if conn is not None:
                conn.close()
        state_of = {r["tool_name"]: r["state"] for r in policy_rows}
        cap = engine.cap()
        for tool in tool_list:
            name = tool.name
            declared = permissions_mod.level_for_tool(tool)
            state = state_of.pop(name, "inherit")
            if declared >= permissions_mod.L4:
                effective = "restricted"
            elif state == "disabled":
                effective = "disabled"
            elif cap is not None and declared <= cap:
                effective = "authorized"
            else:
                effective = "denied_by_cap"
            rows.append({
                "name": name,
                "class": tool.permission,
                "level": declared,
                "description": tool.description,
                "approval_digest_gate":
                    tool.permission == "approval_required",
                "policy": state,
                "effective": effective,
            })
        orphans = sorted(state_of)
        return rows, orphans

    def _permissions_payload(self):
        engine = self._permission_engine()
        cap = engine.cap()
        if cap is None:
            cap = permissions_mod.L3
        tools_rows, orphans = self._permission_tool_rows(engine)
        return {
            "cap": cap,
            "levels": [
                {"level": level, "name": name,
                 "description": desc}
                for level, name, desc in permissions_mod.LEVEL_INFO
            ],
            "tools": tools_rows,
            "orphaned_policies": orphans,
            "restricted": {
                "grantable": False,
                "reason": "Level 4 operations are blocked for the "
                          "product build; no such tool may be granted "
                          "through settings, policy or model output.",
            },
        }

    def _handle_permissions_view(self, send_body):
        if self._reject_cross_origin():
            return
        try:
            self._send_json(200, self._permissions_payload(), send_body)
        except Exception:
            self._log_failure("permissions view error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}},
                            send_body)

    def _handle_permissions_audit(self, send_body):
        if self._reject_cross_origin():
            return
        query = urlsplit(self.path).query
        params = parse_qs(query)
        limit = 200
        raw = params.get("limit", [None])[0]
        if raw is not None:
            try:
                limit = max(1, min(int(raw), 500))
            except ValueError:
                limit = 200
        conn = None
        try:
            conn = db.open_conn(self.db_path, readonly=True)
            rows = conn.execute(
                "SELECT id, ts, source, goal_id, tool_name, level,"
                " decision, reason FROM permission_audit"
                " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            self._send_json(200, {"limit": limit, "entries": [
                {k: row[k] for k in row.keys()} for row in rows]},
                send_body)
        except Exception:
            self._log_failure("permissions audit error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}},
                            send_body)
        finally:
            if conn is not None:
                conn.close()

    def _handle_permissions_cap(self):
        """POST /api/permissions/cap {level: 1..3} — the operator's
        global grant level. L4 is not grantable and refused here."""
        if self._reject_cross_origin():
            return
        payload = self._read_json_payload(4096)
        if payload is None:
            return
        level = payload.get("level")
        if not isinstance(level, int) or isinstance(level, bool) or \
                not (permissions_mod.L1 <= level <= permissions_mod.L3):
            self._send_json(400, {"error": {"code": "invalid_cap",
                                            "message": "level must be an "
                                            "integer between 1 and 3."}})
            return
        engine = self._permission_engine()
        try:
            engine.set_cap(level)
        except permissions_mod.PolicyWriteError as exc:
            self._send_json(400, {"error": {"code": str(exc),
                                            "message": "policy write "
                                            "refused."}})
            return
        except Exception:
            self._log_failure("cap write error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
            return
        self._send_json(200, {"ok": True,
                              "state": self._permissions_payload()})

    def _handle_permissions_policy(self):
        """POST /api/permissions/policy {tool, state} — per-tool
        inherit/disabled. Unknown tools and non-policy states refuse."""
        if self._reject_cross_origin():
            return
        payload = self._read_json_payload(4096)
        if payload is None:
            return
        name = payload.get("tool")
        state = payload.get("state")
        if not isinstance(name, str) or len(name) > 80 or \
                state not in ("inherit", "disabled"):
            self._send_json(
                400, {"error": {"code": "invalid_policy",
                                "message": "tool (string) and state"
                                " ('inherit'|'disabled') required."}})
            return
        tool = None
        worker = getattr(self.server, "goal_worker", None)
        if worker is not None and getattr(worker, "registry", None):
            tool = worker.registry.get(name)
        if tool is None:
            self._send_json(404, {"error": {"code": "unknown_tool",
                                            "message": "tool is not "
                                            "registered."}})
            return
        engine = self._permission_engine()
        try:
            engine.set_tool_state(
                name, state,
                declared_level=permissions_mod.level_for_tool(tool))
        except Exception:
            self._log_failure("policy write error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
            return
        self._send_json(200, {"ok": True,
                              "state": self._permissions_payload()})

    # ------------------------------------------------------------------
    # Skills registry (local discovery, operator state, no execution)

    def _skills_dirs(self):
        return skills_mod.default_dirs()

    def _handle_skills_view(self, send_body):
        """GET /api/skills — discovered registry + operator state +
        optional keyword relevance against ?relevant=<objective text>."""
        if self._reject_cross_origin():
            return
        relevant = parse_qs(urlsplit(self.path).query).get(
            "relevant", [None])[0]
        conn = None
        try:
            conn = db.open_conn(self.db_path)
            with conn:
                skills_mod.sync_registry(conn, self._skills_dirs(), None)
        except Exception:
            self._log_failure("skills scan error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}},
                            send_body)
            return
        finally:
            if conn is not None:
                conn.close()
        conn = db.open_conn(self.db_path, readonly=True)
        try:
            rows = conn.execute(
                "SELECT id, name, source, version, description, keywords,"
                " state, trust, scanned_at FROM skills ORDER BY id"
            ).fetchall()
            payload = {
                "dirs": [str(d) for d in self._skills_dirs()],
                "skills": [{k: row[k] for k in row.keys()}
                           for row in rows],
                "execution": "sandboxed"
                if skillexec_mod.sandbox_available() else "unavailable",
                "execution_note": ("Executable skills (manifest"
                                   " entrypoint) run only on explicit"
                                   " operator action, only when trusted"
                                   " and enabled, and only inside a"
                                   " bubblewrap sandbox with the network"
                                   " unshared and an isolated writable"
                                   " workspace. Without bwrap, execution"
                                   " fails closed.")
                if skillexec_mod.sandbox_available()
                else ("bubblewrap is not available on this system;"
                      " executable skills fail closed."),
            }
            if relevant:
                payload["relevant"] = skills_mod.relevant_to(conn,
                                                             relevant)
            self._send_json(200, payload, send_body)
        except Exception:
            self._log_failure("skills view error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}},
                            send_body)
        finally:
            conn.close()

    def _handle_skills_manage(self):
        """POST /api/skills {id, action: enable|disable|trust|untrust}"""
        if self._reject_cross_origin():
            return
        payload = self._read_json_payload(4096)
        if payload is None:
            return
        sid = payload.get("id")
        action = payload.get("action")
        if not isinstance(sid, str) or len(sid) > 80 or \
                action not in ("enable", "disable", "trust", "untrust"):
            self._send_json(400, {"error": {"code": "invalid_skill_action",
                                            "message": "id (string) and "
                                            "action (enable|disable|"
                                            "trust|untrust) required."}})
            return
        conn = None
        try:
            conn = db.open_conn(self.db_path)
            with conn:
                if action == "enable":
                    skills_mod.set_state(conn, sid, "enabled")
                elif action == "disable":
                    skills_mod.set_state(conn, sid, "disabled")
                elif action == "trust":
                    skills_mod.set_trust(conn, sid, "trusted")
                else:
                    skills_mod.set_trust(conn, sid, "untrusted")
        except skills_mod.SkillError as exc:
            code = 404 if exc.code == "unknown_skill" else 400
            self._send_json(code, {"error": {"code": exc.code,
                                             "message": str(exc)}})
            return
        except Exception:
            self._log_failure("skills manage error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
            return
        finally:
            if conn is not None:
                conn.close()
        self._send_json(200, {"ok": True})

    # ------------------------------------------------------------------
    # Isolated skill execution (operator-initiated ONLY)

    def _handle_skills_run(self):
        """POST /api/skills/run {id, args} — the ONLY path that runs a
        skill. The model can never call this: it is a human same-origin
        POST, and every gate (enabled + trusted + engine decision +
        manifest + sandbox) is re-validated here right before the
        sandboxed process starts."""
        if self._reject_cross_origin():
            return
        payload = self._read_json_payload(4096)
        if payload is None:
            return
        sid = payload.get("id")
        if not isinstance(sid, str) or len(sid) > 80:
            self._send_json(400, {"error": {"code": "invalid_skill",
                                            "message": "id (string)"
                                            " required."}})
            return
        try:
            args = skillexec_mod.redact_args(payload.get("args"))
        except skillexec_mod.ExecError as exc:
            self._send_json(400, {"error": {"code": exc.code,
                                            "message": str(exc)}})
            return
        conn = None
        workspace = None
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            conn = db.open_conn(self.db_path)
            with conn:
                # fresh scan validates the source dir still exists
                skills_mod.sync_registry(conn, self._skills_dirs(),
                                         now_iso)
                row = conn.execute(
                    "SELECT * FROM skills WHERE id = ?", (sid,)
                ).fetchone()
                if row is None:
                    self._send_json(404, {"error": {"code":
                                                    "unknown_skill",
                                                    "message": "skill is"
                                                    " not in the"
                                                    " registry."}})
                    return
                source = Path(row["source"])
                manifest_path = source / "SKILL.md"
                if not source.is_dir() or not manifest_path.is_file():
                    self._send_json(404, {"error": {"code":
                                                    "skill_source_missing",
                                                    "message": "skill"
                                                    " manifest is not"
                                                    " present."}})
                    return
                fields = skills_mod.parse_manifest(
                    manifest_path.read_text(encoding="utf-8",
                                            errors="replace"))
                registry_row = {k: row[k] for k in row.keys()}
                registry_row["fields"] = fields
                workspace = Path(tempfile.mkdtemp(prefix="jarvis-skill-"
                                                  "ws-"))
                run_record = skillexec_mod.run(
                    registry_row, args, workspace,
                    engine=self._permission_engine())
        except skills_mod.SkillError as exc:
            self._send_json(400, {"error": {"code": exc.code,
                                            "message": str(exc)}})
            return
        except skillexec_mod.ExecError as exc:
            self._record_skill_run_db(sid, None,
                                      {"status": "refused",
                                       "exit_code": None, "args": args,
                                       "verification": None,
                                       "duration_ms": 0,
                                       "permission": None,
                                       "stdout": None,
                                       "stderr": None},
                                      now_iso)
            self._send_json(400, {"error": {"code": exc.code,
                                            "message": str(exc)}})
            return
        except Exception:
            self._log_failure("skill run error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
            return
        finally:
            if workspace is not None:
                shutil.rmtree(workspace, ignore_errors=True)
            if conn is not None:
                conn.close()
        self._record_skill_run_db(
            sid, registry_row.get("version"), run_record, now_iso)
        perm = (run_record.get("permission") or {})
        self._send_json(200, {
            "skill": sid,
            "status": run_record["status"],
            "exit_code": run_record["exit_code"],
            "stdout": run_record["stdout"],
            "stderr": run_record["stderr"],
            "args": run_record["args"],
            "duration_ms": run_record["duration_ms"],
            "timeout": run_record["timeout"],
            "verification": run_record["verification"],
            "permission_level": perm.get("level"),
            "permission_reason": perm.get("reason"),
        })

    def _record_skill_run_db(self, sid, version, run_record, now_iso):
        """Persist one run (executed or refused) into the bounded
        skill_runs history; args are already redacted at this point."""
        conn = None
        try:
            conn = db.open_conn(self.db_path)
            with conn:
                conn.execute(
                    "INSERT INTO skill_runs(skill_id, version, status,"
                    " exit_code, args_json, stdout, stderr,"
                    " verification, duration_ms, permission,"
                    " started_at, finished_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sid, version, run_record["status"],
                     run_record["exit_code"],
                     json.dumps(run_record.get("args") or {},
                                ensure_ascii=True)[:2048],
                     run_record.get("stdout"),
                     run_record.get("stderr"),
                     run_record.get("verification"),
                     run_record.get("duration_ms") or 0,
                     json.dumps(run_record.get("permission") or None,
                                ensure_ascii=True)[:200],
                     now_iso,
                     time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime())))
        except Exception:
            self._log_failure("skill run record error", None)
        finally:
            if conn is not None:
                conn.close()

    def _handle_skill_runs(self, send_body):
        """GET /api/skills/runs?limit= — real execution history."""
        if self._reject_cross_origin():
            return
        limit_raw = parse_qs(urlsplit(self.path).query).get(
            "limit", ["20"])[0]
        try:
            limit = max(1, min(100, int(limit_raw)))
        except ValueError:
            self._send_json(400, {"error": {"code": "invalid_request",
                                            "message": "limit must be"
                                            " an integer."}})
            return
        conn = None
        try:
            conn = db.open_conn(self.db_path, readonly=True)
            rows = conn.execute(
                "SELECT id, skill_id, version, status, exit_code,"
                " args_json, stdout, stderr, error_code, verification,"
                " duration_ms, permission, started_at, finished_at"
                " FROM skill_runs ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
            self._send_json(200, {"runs": [{k: row[k] for k in
                                           row.keys()} for row in rows],
                                  "sandbox": (skillexec_mod.
                                              sandbox_available())},
                          send_body)
        except Exception:
            self._log_failure("skill runs error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}},
                            send_body)
        finally:
            if conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # Updates: honest, report-only version awareness

    def _handle_updates_view(self, send_body):
        if self._reject_cross_origin():
            return
        conn = None
        try:
            conn = db.open_conn(self.db_path, readonly=True)
            rows = conn.execute(
                "SELECT id, ts, source, current, latest, status, detail"
                " FROM update_checks ORDER BY id DESC LIMIT 20"
            ).fetchall()
            history = [{k: row[k] for k in row.keys()} for row in rows]
            conn.close()
            conn = None
            source = os.environ.get("JARVIS_UPDATE_SOURCE", "")
            installs = []
            try:
                conn = db.open_conn(self.db_path, readonly=True)
                for row in conn.execute(
                        "SELECT id, ts, from_version, to_version,"
                        " manifest_sha, status, detail, backup_dir"
                        " FROM update_installs ORDER BY id DESC"
                        " LIMIT 20").fetchall():
                    installs.append({k: row[k] for k in row.keys()})
                conn.close()
                conn = None
            except Exception:
                if conn is not None:
                    conn.close()
                    conn = None
            self._send_json(200, {
                "current": updates_mod.current_version(),
                "status": history[0]["status"] if history
                else "not_configured",
                "latest": history[0]["latest"] if history else None,
                "detail": (history[0]["detail"] if history else None)
                or (f"no check recorded yet — configure"
                    f" JARVIS_UPDATE_SOURCE" if not source else ""),
                "policy": "CHECK and REPORT by default; installation"
                          " is a separate explicit operator action"
                          " (verified staging, digest-bound apply,"
                          " recorded rollback).",
                "check_history": history,
                "installs": installs,
            }, send_body)
        except Exception:
            self._log_failure("updates view error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}},
                            send_body)
        finally:
            if conn is not None:
                conn.close()

    def _handle_updates_check(self):
        if self._reject_cross_origin():
            return
        conn = None
        try:
            source = os.environ.get("JARVIS_UPDATE_SOURCE", "")
            result = updates_mod.check(source,
                                       updates_mod.current_version())
            conn = db.open_conn(self.db_path)
            with conn:
                updates_mod.record(conn, result,
                                   updates_mod.current_version(),
                                   time.strftime(
                                       "%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime()))
            payload = {"result": result,
                       "current": updates_mod.current_version()}
            self._send_json(200, payload)
        except Exception:
            self._log_failure("updates check error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
        finally:
            if conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # Verified staged installation (explicit operator action ONLY)

    def _update_target(self):
        return os.environ.get("JARVIS_UPDATE_TARGET", "")

    def _handle_updates_stage(self):
        """POST /api/updates/stage {source?} — CHECK -> VERIFY -> STAGE.
        Downloads nothing until integrity metadata verifies; staging is
        disposable; nothing outside the staging dir changes. Refuses
        when no target root is configured (report-only default)."""
        if self._reject_cross_origin():
            return
        payload = self._read_json_payload(4096)
        source = None
        if payload is not None:
            source = payload.get("source")
        if not (isinstance(source, str) and source):
            source = os.environ.get("JARVIS_UPDATE_SOURCE", "")
        target = self._update_target()
        try:
            staged = updates_mod.stage(
                source, updates_mod.current_version(), target)
        except updates_mod.UpdateError as exc:
            self._send_json(400, {"error": {"code": exc.code,
                                            "message": str(exc)}})
            return
        except Exception:
            self._log_failure("updates stage error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
            return
        if staged.get("status") == "up_to_date":
            self._send_json(200, staged)
            return
        conn = None
        try:
            updates_mod.persist_stage_index(staged)
            conn = db.open_conn(self.db_path)
            with conn:
                updates_mod.record_stage(
                    conn, staged,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    updates_mod.current_version())
        except Exception:
            self._log_failure("updates stage persist error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
            return
        finally:
            if conn is not None:
                conn.close()
        self._send_json(200, staged)

    def _handle_updates_apply(self):
        """POST /api/updates/apply {manifest_sha, allow_overwrite} —
        the ONLINE UPDATE operational confirmation gate: applying a
        verified staged update is an explicit human action bound to the
        staged digest. Modified local files are never replaced without
        allow_overwrite:true."""
        if self._reject_cross_origin():
            return
        payload = self._read_json_payload(4096)
        if payload is None:
            return
        sha = payload.get("manifest_sha")
        allow_overwrite = payload.get("allow_overwrite") is True
        if not (isinstance(sha, str) and len(sha) == 64 and
                all(c in "0123456789abcdef" for c in sha.lower())):
            self._send_json(400, {"error": {"code": "invalid_digest",
                                            "message": "manifest_sha"
                                            " (64 hex) must echo the"
                                            " staged digest."}})
            return
        target = self._update_target()
        if not target:
            self._send_json(400, {"error": {"code":
                                            "target_not_configured",
                                            "message": "no update target"
                                            " configured."}})
            return
        conn = None
        try:
            conn = db.open_conn(self.db_path)
            with conn:
                result = updates_mod.apply(
                    conn, target, sha,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    allow_overwrite=allow_overwrite)
            self._send_json(200, result)
        except updates_mod.UpdateError as exc:
            self._send_json(400, {"error": {"code": exc.code,
                                            "message": str(exc)}})
        except Exception:
            self._log_failure("updates apply error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
        finally:
            if conn is not None:
                conn.close()

    def _handle_updates_rollback(self):
        """POST /api/updates/rollback {id} — restore the recorded
        pre-update contents of one applied install (recorded-footprint
        only; refuses when a backup went missing)."""
        if self._reject_cross_origin():
            return
        payload = self._read_json_payload(4096)
        if payload is None:
            return
        install_id = payload.get("id")
        if not isinstance(install_id, int) or install_id < 1:
            self._send_json(400, {"error": {"code": "invalid_request",
                                            "message": "id (integer)"
                                            " required."}})
            return
        conn = None
        try:
            conn = db.open_conn(self.db_path)
            with conn:
                result = updates_mod.rollback(conn, install_id)
            self._send_json(200, result)
        except updates_mod.UpdateError as exc:
            self._send_json(400, {"error": {"code": exc.code,
                                            "message": str(exc)}})
        except Exception:
            self._log_failure("updates rollback error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
        finally:
            if conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # Bounded self-improvement: evidence -> proposals -> operator
    # decision -> one L2-safe application action

    def _handle_improvements_view(self, send_body):
        if self._reject_cross_origin():
            return
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn = None
        try:
            # writable connection: the automatic VERIFY leg runs as part
            # of every observation cycle (never claims success without
            # its deterministic check)
            conn = db.open_conn(self.db_path)
            with conn:
                improvements_mod.evaluate_applied(conn, now_iso)
            rows = conn.execute(
                "SELECT id, created, kind, problem, evidence, expected,"
                " risk, status, task_id, decided, baseline_json,"
                " verify_status, verify_json, verify_at FROM improvements"
                " ORDER BY created DESC LIMIT 100").fetchall()
            self._send_json(200, {
                "proposals": [{k: row[k] for k in row.keys()}
                              for row in rows],
                "cycle": "OBSERVE -> PROPOSE -> EVALUATE -> APPROVE ->"
                         " APPLY (one task, L2) -> VERIFY (automatic,"
                         " evidence-based) -> MEASURE -> ACCEPT or"
                         " ROLLBACK.",
            }, send_body)
        except Exception:
            self._log_failure("improvements view error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}},
                            send_body)
        finally:
            if conn is not None:
                conn.close()

    def _handle_improvements_propose(self):
        if self._reject_cross_origin():
            return
        conn = None
        try:
            conn = db.open_conn(self.db_path)
            with conn:
                created = improvements_mod.propose(
                    conn,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            self._send_json(200, {"created": created})
        except Exception:
            self._log_failure("improvements propose error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
        finally:
            if conn is not None:
                conn.close()

    def _handle_improvements_decide(self):
        if self._reject_cross_origin():
            return
        payload = self._read_json_payload(4096)
        if payload is None:
            return
        pid = payload.get("id")
        decision = payload.get("decision")
        if not isinstance(pid, str) or len(pid) > 64 or \
                decision not in ("approve", "reject", "apply"):
            self._send_json(400, {"error": {"code": "invalid_decision",
                                            "message": "id (string) and"
                                            " decision (approve|reject|"
                                            "apply) required."}})
            return
        conn = None
        try:
            conn = db.open_conn(self.db_path)
            with conn:
                new_status = improvements_mod.decide(
                    conn, pid, decision,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            self._send_json(200, {"ok": True, "status": new_status})
        except ValueError as exc:
            self._send_json(400, {"error": {"code": "invalid_decision",
                                            "message": str(exc)}})
        except Exception:
            self._log_failure("improvements decide error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
        finally:
            if conn is not None:
                conn.close()

    def _handle_improvements_rollback(self):
        """POST /api/improvements/rollback {id} — after a failed_checked
        verification, the applied improvement's SINGLE queued follow-up
        task can be undone; worked-on work refuses and requires a
        human."""
        if self._reject_cross_origin():
            return
        payload = self._read_json_payload(4096)
        if payload is None:
            return
        pid = payload.get("id")
        if not isinstance(pid, str) or len(pid) > 64:
            self._send_json(400, {"error": {"code": "invalid_request",
                                            "message": "id (string)"
                                            " required."}})
            return
        conn = None
        try:
            conn = db.open_conn(self.db_path)
            with conn:
                improvements_mod.rollback_failed(
                    conn, pid,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            self._send_json(200, {"ok": True, "status": "rolled_back"})
        except ValueError as exc:
            self._send_json(400, {"error": {"code": "invalid_request",
                                            "message": str(exc)}})
        except Exception:
            self._log_failure("improvements rollback error", None)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
        finally:
            if conn is not None:
                conn.close()

    def _handle_settings_view(self, send_body=True):
        """GET /api/settings — full secret-free settings state."""
        if self._reject_cross_origin():
            return
        try:
            self._send_json(200, self._settings_view(), send_body)
        except Exception as exc:
            self._log_failure("settings view error", exc)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}},
                            send_body)

    def _handle_settings_patch(self):
        """PATCH /api/settings — validated partial update of the user
        store. Environment-managed fields refuse with field_locked;
        unrelated values are preserved on any failure."""
        svc = self._settings_service()
        payload = self._read_json_payload(MAX_SETTINGS_BODY_BYTES)
        if payload is None:
            return
        unknown = set(payload) - SETTINGS_PATCH_KEYS
        if unknown:
            self._send_json(
                400, {"error": {"code": "unsupported_field",
                                "message": "Unsupported request fields: "
                                + ", ".join(sorted(unknown)) + "."}})
            return
        if not payload:
            self._send_json(
                400, {"error": {"code": "empty_update",
                                "message": "Provide at least one field "
                                "to update."}})
            return
        if "vault_path" in payload:
            # Phase 13A: resolve the vault root explicitly at
            # configuration time; the stored setting is the resolved
            # absolute path. Nonexistent/non-directory paths are
            # rejected here — never stored and never silently switched.
            raw = payload["vault_path"]
            if isinstance(raw, str) and raw.strip():
                try:
                    payload["vault_path"] = str(
                        vault_mod.validate_root(raw))
                except vault_mod.VaultError as exc:
                    self._send_json(
                        400, {"error": {"code": exc.code,
                                        "message": str(exc)}})
                    return
        managed = svc.base_cfg.env_managed
        locked = sorted(k for k in payload if k in managed)
        if locked:
            self._send_json(
                409,
                {"error": {
                    "code": "field_locked",
                    "message": "Environment-managed setting(s): "
                    + ", ".join(locked)
                    + " — managed by a JARVIS_* environment variable.",
                }})
            return
        try:
            svc.store.save_fields(payload, env_managed=managed)
            svc.apply_values()
        except settingsstore.SettingsError as err:
            self._errors(err)
            return
        except Exception as exc:
            self._log_failure("settings persist error", exc)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Failed to persist "
                                            "settings."}})
            return
        self._send_json(200, self._settings_view())

    def _handle_settings_credentials_post(self):
        """POST /api/settings/credentials — write-only key save.

        The key is validated, stored atomically (0600) and never echoed,
        logged or returned. Nothing about the active provider/policy
        changes here: activating is a separate, explicit apply."""
        svc = self._settings_service()
        payload = self._read_json_payload(MAX_SETTINGS_BODY_BYTES)
        if payload is None:
            return
        unknown = set(payload) - {"provider", "api_key"}
        if unknown:
            self._send_json(
                400, {"error": {"code": "unsupported_field",
                                "message": "Unsupported request fields: "
                                + ", ".join(sorted(unknown)) + "."}})
            return
        profile = payload.get("provider")
        api_key = payload.get("api_key")
        if profile not in provider.PROVIDER_PROFILES:
            self._send_json(
                400, {"error": {"code": "invalid_provider",
                                "message": "Unknown provider profile."}})
            return
        if (getattr(svc, "environ", os.environ).get(
                self._profile_env_key_name(profile),
                "").strip()):
            self._send_json(
                409,
                {"error": {
                    "code": "field_locked",
                    "message":
                    f"The {profile} credential is managed by "
                    f"{self._profile_env_key_name(profile)}; unset it to "
                    "manage the key here.",
                }})
            return
        try:
            svc.store.save_key(profile, api_key)
        except settingsstore.SettingsError as err:
            self._errors(err)
            return
        except Exception as exc:
            self._log_failure("credential save error", exc)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Failed to store "
                                            "the credential."}})
            return
        svc.apply_values()  # not_configured state may change; policy not
        self._send_json(200, {
            "provider": profile,
            "configured": True,
            "key_source": "user_store",
        })

    def _handle_settings_credentials_delete(self):
        svc = self._settings_service()
        profile = parse_qs(urlsplit(self.path).query).get(
            "provider", [""])[0].strip()
        if profile not in provider.PROVIDER_PROFILES:
            self._send_json(
                400, {"error": {"code": "invalid_provider",
                                "message": "Unknown provider profile."}})
            return
        if (getattr(svc, "environ", os.environ).get(
                self._profile_env_key_name(profile), "").strip()):
            self._send_json(
                409,
                {"error": {
                    "code": "field_locked",
                    "message": f"The {profile} credential is managed by "
                    f"{self._profile_env_key_name(profile)}.",
                }})
            return
        svc.store.clear_key(profile)
        svc.apply_values()
        self._send_json(200, {"provider": profile,
                              "configured": False,
                              "key_source": ""})

    def _refresh_catalog_for(self, profile_name, *, with_pricing=True,
                             timeout_cap=30, api_key=None):
        """Fetch one provider's catalog (+ documented public pricing for
        OrcaRouter) into its per-provider cache. Raises ProviderError.
        Never runs inference and never persists anything. `api_key`
        overrides the stored credential (probe semantics: a provided
        key is tested, never saved)."""
        profile = provider.PROVIDER_PROFILES[profile_name]
        svc = self._settings_service()
        api_key = api_key if api_key is not None \
            else self._profile_active_key(profile_name)
        if not api_key:
            raise provider.NotConfigured()
        effective = svc.snapshot() if svc else self.cfg
        timeout = min(effective.timeout_seconds or 60, timeout_cap)
        base_url = self._profile_base_url(profile_name)
        models = provider.list_models(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout,
        )
        state = self._provider_catalog_state(profile_name)
        pricing_fetched = state.get("pricing_fetched_at", 0.0)
        if with_pricing and profile_name == "orcarouter":
            try:
                pricing = provider.fetch_pricing_catalog(
                    timeout_seconds=timeout)
                free_ids = provider.fetch_free_package(
                    timeout_seconds=timeout)
            except provider.ProviderError:
                # pricing evidence unavailable — every entry stays an
                # honest 'unknown', the catalog itself is still usable
                models = provider.merge_catalog_entries(models)
            else:
                models = provider.merge_catalog_entries(
                    models, pricing=pricing, free_ids=free_ids)
                pricing_fetched = time.time()
        state["models"] = models
        state["fetched_at"] = time.time()
        state["error"] = None
        state["pricing_fetched_at"] = pricing_fetched
        # the ACTIVE provider's legacy cache mirrors this (GET /api/models
        # and /api/status read it)
        if profile_name == (effective.provider or "freellmapi"):
            cache = self._catalog_state()
            if cache is not None:
                cache["models"] = models
                cache["fetched_at"] = state["fetched_at"]
                cache["error"] = None
        return models

    @staticmethod
    def _pricing_summary(models):
        counts = {tier: 0 for tier in SHOW_CATALOG_TIERS}
        for m in models or []:
            klass = m.get("pricing_class", "unknown")
            counts[klass] = counts.get(klass, 0) + 1
        return counts

    def _handle_settings_test_connection(self):
        """POST /api/settings/test-connection — credential check + model
        catalog fetch. NO chat inference request is ever sent here; a
        provided key is tested, never saved."""
        svc = self._settings_service()
        payload = self._read_json_payload(MAX_SETTINGS_BODY_BYTES)
        if payload is None:
            return
        unknown = set(payload) - {"provider", "api_key", "with_pricing"}
        if unknown:
            self._send_json(
                400, {"error": {"code": "unsupported_field",
                                "message": "Unsupported request fields: "
                                + ", ".join(sorted(unknown)) + "."}})
            return
        profile_name = payload.get("provider")
        if profile_name not in provider.PROVIDER_PROFILES:
            self._send_json(
                400, {"error": {"code": "invalid_provider",
                                "message": "Unknown provider profile."}})
            return
        provided = payload.get("api_key")
        if isinstance(provided, str):
            provided = provided.strip()
            if provided and (len(provided) > MAX_SETTINGS_API_KEY_CHARS
                             or any(ord(ch) < 32 for ch in provided)):
                self._send_json(
                    400, {"error": {"code": "invalid_credential",
                                    "message": "The provided credential "
                                    "is not a usable API key."}})
                return
        else:
            provided = ""
        if provided:
            api_key, key_source = provided, "provided"
        else:
            api_key = self._profile_active_key(profile_name)
            key_source = "environment" \
                if api_key and (svc.environ.get(
                    self._profile_env_key_name(profile_name), "")
                    or "").strip() \
                else ("user_store" if api_key else "")
        if not api_key:
            self._send_json(
                503, {"error": {"code": "not_configured",
                                "message": f"The {profile_name} provider "
                                "has no credential to test."}})
            return
        try:
            models = self._refresh_catalog_for(
                profile_name,
                with_pricing=bool(payload.get("with_pricing", True)),
                api_key=provided or None)
        except provider.ProviderError as exc:
            self._provider_catalog_state(profile_name)["error"] = exc.code
            self._send_json(exc.http_status,
                            {"error": {"code": exc.code,
                                       "message": exc.user_message}})
            return
        except Exception as exc:
            self._log_failure("test connection error", exc)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
            return
        state = self._provider_catalog_state(profile_name)
        self._send_json(200, {
            "ok": True,
            "provider": profile_name,
            "key_source": key_source,
            "catalog": {
                "count": len(models),
                "fetched_at": state.get("fetched_at"),
            },
            "pricing_summary": self._pricing_summary(models),
        })

    def _catalog_fresh_for(self, provider_name, *, max_age):
        state = self._provider_catalog_state(provider_name)
        if state.get("models") is None:
            return None
        if time.time() - state.get("fetched_at", 0.0) > max_age:
            return None
        return state["models"]

    def _handle_settings_catalog(self, send_body=True):
        """GET /api/settings/catalog?provider=&refresh= — cached or one
        fresh validated fetch; never inference."""
        svc = self._settings_service()
        query = parse_qs(urlsplit(self.path).query)
        profile_name = query.get("provider", [""])[0].strip()
        if profile_name not in provider.PROVIDER_PROFILES:
            self._send_json(
                400, {"error": {"code": "invalid_provider",
                                "message": "Unknown provider profile."}},
                send_body)
            return
        force = bool(query.get("refresh"))
        try:
            models = None if force \
                else self._catalog_fresh_for(profile_name,
                                             max_age=CATALOG_FRESH_SECONDS)
            if models is None:
                models = self._refresh_catalog_for(
                    profile_name, with_pricing=True)
        except provider.ProviderError as exc:
            self._provider_catalog_state(profile_name)["error"] = exc.code
            self._send_json(exc.http_status,
                            {"error": {"code": exc.code,
                                       "message": exc.user_message}},
                            send_body)
            return
        except Exception as exc:
            self._log_failure("settings catalog error", exc)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}},
                            send_body)
            return
        state = self._provider_catalog_state(profile_name)
        self._send_json(
            200,
            {
                "provider": profile_name,
                "models": models,
                "fetched_at": state.get("fetched_at"),
                "pricing_fetched_at": state.get("pricing_fetched_at", 0.0)
                or None,
                "pricing_summary": self._pricing_summary(models),
            },
            send_body,
        )

    @staticmethod
    def _pricing_class_of(models, model_id):
        for entry in models or []:
            if entry.get("id") == model_id:
                return entry.get("pricing_class", "unknown")
        return None

    def _cost_confirmation_required(
            self, provider_name, model_class, confirm_cost):
        """Explicit cost confirmation policy (Phase 11A).

        OrcaRouter bills by token, so paid AND unknown-cost selections
        require one explicit confirmation. The FreeLLMAPI local gateway
        is a localhost service without metered billing (its entries are
        honestly 'unknown' but confirmation is not required there)."""
        if confirm_cost is True:
            return None
        if provider_name == "orcarouter" \
                and model_class in ("paid", "unknown"):
            return settingsstore.SettingsError(
                409, "cost_confirmation_required",
                f"This model's cost classification is '{model_class}'. "
                "Confirm explicitly to apply or test it.")
        return None

    def _handle_settings_apply(self):
        """POST /api/settings/apply — the ONLY path that activates a
        provider/model policy. Validates against the target provider's
        catalog first; any failure leaves the working setup untouched."""
        svc = self._settings_service()
        payload = self._read_json_payload(MAX_SETTINGS_BODY_BYTES)
        if payload is None:
            return
        unknown = set(payload) - {"provider", "model", "fallback_model",
                                  "confirm_cost"}
        if unknown:
            self._send_json(
                400, {"error": {"code": "unsupported_field",
                                "message": "Unsupported request fields: "
                                + ", ".join(sorted(unknown)) + "."}})
            return
        confirm_cost = payload.get("confirm_cost")
        if not isinstance(confirm_cost, bool):
            confirm_cost = False
        current = svc.snapshot()
        managed = svc.base_cfg.env_managed
        target_provider = payload.get("provider") or current.provider
        if target_provider not in provider.PROVIDER_PROFILES:
            self._send_json(
                400, {"error": {"code": "invalid_provider",
                                "message": "Unknown provider profile."}})
            return
        # credential gate: never activate a provider without a key
        if not self._profile_active_key(target_provider):
            self._send_json(
                503, {"error": {"code": "not_configured",
                                "message": f"The {target_provider} "
                                "provider has no credential yet. Save "
                                "and test a key first."}})
            return
        # ---- model resolution + catalog validation ----
        env_locked = "model" in managed
        fresh = None
        model_class = None
        desired_model = None
        if not env_locked:
            desired_model = payload.get("model")
            if desired_model in (None, ""):
                desired_model = provider.PROVIDER_DEFAULT_MODEL.get(
                    target_provider)
            else:
                err = settingsstore.validate_model_id(desired_model)
                if err is not None:
                    self._errors(err)
                    return
            try:
                fresh = self._catalog_fresh_for(
                    target_provider, max_age=CATALOG_FRESH_SECONDS)
                if fresh is None:
                    fresh = self._refresh_catalog_for(
                        target_provider, with_pricing=True)
            except provider.ProviderError as exc:
                self._send_json(exc.http_status,
                                {"error": {"code": exc.code,
                                           "message": exc.user_message}})
                return
            if target_provider != "freellmapi":
                available = [m["id"] for m in fresh
                             if m.get("available", True)]
                if desired_model not in available:
                    self._send_json(
                        400,
                        {"error": {
                            "code": "model_not_available",
                            "message": "Model '" + desired_model
                            + "' is not available in the "
                            + target_provider + " catalog.",
                        }})
                    return
            else:
                # legacy vocabulary: env policy + gateway 'auto' alias
                if desired_model not in \
                        (svc.base_cfg.model, "auto") and not any(
                        m.get("available", True) and m["id"] == desired_model
                        for m in fresh):
                    self._send_json(
                        400, {"error": {
                            "code": "model_not_available",
                            "message": "Model '" + desired_model
                            + "' is not available in the current "
                            "provider catalog.",
                        }})
                    return
            model_class = self._pricing_class_of(fresh, desired_model) \
                or "unknown"
            confirm_err = self._cost_confirmation_required(
                target_provider, model_class, confirm_cost)
            if confirm_err is not None:
                self._send_json(
                    confirm_err.status,
                    {"error": {"code": confirm_err.code,
                               "message": confirm_err.message,
                               "pricing_class": model_class}})
                return
        # ---- fallback resolution (optional; empty string clears) ----
        desired_fallback = payload.get("fallback_model")
        fallback_clear = desired_fallback == ""
        fallback_store_key = "fallback_model" not in managed
        if desired_fallback and not fallback_clear:
            err = settingsstore.validate_model_id(desired_fallback,
                                                  field="fallback_model")
            if err is not None:
                self._errors(err)
                return
            if not env_locked:
                fb_class = self._pricing_class_of(
                    fresh, desired_fallback) or "unknown"
                if target_provider == "orcarouter" \
                        and fb_class == "free_verified":
                    self._send_json(
                        400, {"error": {
                            "code": "fallback_not_supported",
                            "message": "The provider silently drops "
                            "free ids from fallback chains; choose a "
                            "paid-cost model as fallback.",
                        }})
                    return
                confirm_err = self._cost_confirmation_required(
                    target_provider, fb_class, confirm_cost)
                if confirm_err is not None:
                    self._send_json(
                        confirm_err.status,
                        {"error": {"code": confirm_err.code,
                                   "message": confirm_err.message,
                                   "pricing_class": fb_class}})
                    return
        # ---- persist + swap: only on full validation success ----
        try:
            fields = {}
            if target_provider != current.provider:
                fields["provider"] = target_provider
            if not env_locked:
                if desired_model:
                    fields["model"] = desired_model
            if fallback_clear and fallback_store_key:
                svc.store.clear_setting("fallback_model",
                                        env_managed=managed)
            elif desired_fallback and fallback_store_key:
                fields["fallback_model"] = desired_fallback
            if fields:
                svc.store.save_fields(fields, env_managed=managed)
            elif target_provider == current.provider and env_locked \
                    and not fallback_clear and not desired_fallback:
                self._send_json(
                    409, {"error": {"code": "field_locked",
                                    "message": "The model policy is "
                                    "environment-managed; nothing to "
                                    "apply."}})
                return
            svc.apply_values()
        except settingsstore.SettingsError as err:
            self._errors(err)
            return
        except Exception as exc:
            self._log_failure("settings apply error", exc)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Failed to apply "
                                            "settings."}})
            return
        # one explicit apply replaces a stale runtime override so the
        # applied policy truly governs subsequent requests
        try:
            conn = db.open_conn(self.cfg.db_path)
            try:
                with conn:
                    conn.execute(
                        "DELETE FROM meta WHERE key='model_override'")
            finally:
                conn.close()
        except Exception as exc:
            self._log_failure("apply override clear error", exc)
        applog.event(
            "settings_applied",
            request_id=getattr(self, "request_id", None),
            provider=target_provider,
            model=self.cfg.model,
            fallback=self.cfg.fallback_model or None,
        )
        self._send_json(200, self._settings_view())

    def _handle_settings_model_test(self):
        """POST /api/settings/model-test — ONE bounded inference request
        against the selected model, only on an explicit operator call,
        with pricing confirmation required for paid/unknown-cost models
        before any token can be billed."""
        svc = self._settings_service()
        payload = self._read_json_payload(MAX_SETTINGS_BODY_BYTES)
        if payload is None:
            return
        unknown = set(payload) - {"provider", "model", "confirm_cost"}
        if unknown:
            self._send_json(
                400, {"error": {"code": "unsupported_field",
                                "message": "Unsupported request fields: "
                                + ", ".join(sorted(unknown)) + "."}})
            return
        effective = svc.snapshot()
        provider_name = payload.get("provider") or effective.provider
        if provider_name not in provider.PROVIDER_PROFILES:
            self._send_json(
                400, {"error": {"code": "invalid_provider",
                                "message": "Unknown provider profile."}})
            return
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip() \
                or len(model) > 128 or any(ch.isspace() for ch in model):
            self._send_json(
                400, {"error": {"code": "invalid_model",
                                "message": "'model' must be a non-empty "
                                "identifier without whitespace."}})
            return
        model = model.strip()
        confirm_cost = payload.get("confirm_cost")
        if not isinstance(confirm_cost, bool):
            confirm_cost = False
        # cost classification BEFORE any inference: from the classified
        # cache, or one bounded catalog fetch if the cache is cold
        try:
            fresh = self._catalog_fresh_for(
                provider_name, max_age=CATALOG_FRESH_SECONDS)
            if fresh is None:
                fresh = self._refresh_catalog_for(
                    provider_name, with_pricing=True)
        except provider.ProviderError as exc:
            self._send_json(exc.http_status,
                            {"error": {"code": exc.code,
                                       "message": exc.user_message}})
            return
        model_class = self._pricing_class_of(fresh, model) or "unknown"
        confirm_err = self._cost_confirmation_required(
            provider_name, model_class, confirm_cost)
        if confirm_err is not None:
            self._send_json(
                confirm_err.status,
                {"error": {"code": confirm_err.code,
                           "message": confirm_err.message,
                           "pricing_class": model_class}})
            return
        if not self._profile_active_key(provider_name):
            self._send_json(
                503, {"error": {"code": "not_configured",
                                "message": f"The {provider_name} provider "
                                "has no credential."}})
            return
        started = time.monotonic()
        try:
            reply, reported_model = provider.chat_completion(
                MODEL_TEST_PROMPT,
                base_url=self._profile_base_url(provider_name),
                api_key=self._profile_active_key(provider_name),
                model=model,
                timeout_seconds=min(effective.timeout_seconds or 60, 30),
                max_tokens=MAX_MODEL_TEST_TOKENS,
                include_model=True,
            )
        except provider.ProviderError as exc:
            self._send_json(exc.http_status,
                            {"error": {"code": exc.code,
                                       "message": exc.user_message}})
            return
        except Exception as exc:
            self._log_failure("model test error", exc)
            self._send_json(500, {"error": {"code": "internal_error",
                                            "message": "Internal error."}})
            return
        self._send_json(200, {
            "ok": True,
            "model": model,
            "pricing_class": model_class,
            "reported_model": reported_model,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "provider": provider_name,
        })

    # ---- Phase 8B goal console --------------------------------------

    def _goal_id_of(self, goal_id):
        if not goal_id or len(goal_id) != 32 \
                or any(ch not in CONV_ID_CHARS for ch in goal_id):
            return None
        return goal_id

    @staticmethod
    def _goal_json_field(raw):
        if not raw:
            return {}
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except ValueError:
            return {"unavailable": "stored value failed to parse"}

    def _goal_step_payload(self, row):
        result = None
        if row["result_json"]:
            try:
                parsed = json.loads(row["result_json"])
            except ValueError:
                parsed = None
            if parsed is None:
                result = {"unavailable": "stored result failed to parse"}
            elif len(row["result_json"]) <= 26000:
                result = parsed
            else:
                result = {
                    "omitted":
                    f"result stored ({len(row['result_json'])} bytes)",
                    "truncated": bool(
                        parsed.get("truncated")) if
                    isinstance(parsed, dict) else False,
                }
        return {
            "id": row["id"],
            "seq": row["seq"],
            "purpose": row["purpose"],
            "tool": row["tool_name"],
            "args": self._goal_json_field(row["args_json"]),
            "depends_on": self._goal_json_field(row["depends_on"]),
            "acceptance": row["acceptance"],
            "status": row["status"],
            "attempts": row["attempts"],
            "verification": row["verification"],
            "error_code": row["error_code"],
            "result": result,
        }

    def _goal_progress(self, conn, goal_id):
        steps = db.get_goal_steps(conn, goal_id)
        return db.goal_progress(steps)

    def _handle_goals_create(self):
        """POST /api/goals — persist a durable goal, answer 202 fast.

        Planning/execution happens on the lifecycle-managed worker; the
        HTTP request never blocks on model calls."""
        if self._rate_limit_gate("goals"):
            return
        if self._reject_cross_origin():
            return
        if not (self.cfg.api_key or "").strip():
            self._task_error(
                503, "not_configured",
                "Goals require the AI provider to be configured.")
            return
        payload = self._read_json_payload(MAX_GOAL_BODY_BYTES)
        if payload is None:
            return
        unknown = set(payload) - {"objective", "mode"}
        if unknown:
            self._task_error(
                400, "invalid_request",
                "Unknown request fields: " + ", ".join(sorted(unknown)) + ".")
            return
        objective = payload.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            self._task_error(
                400, "invalid_objective",
                "'objective' must be a non-empty string.")
            return
        objective = objective.strip()
        if len(objective) > MAX_GOAL_CHARS:
            self._task_error(
                400, "objective_too_long",
                f"'objective' exceeds {MAX_GOAL_CHARS} characters.")
            return
        mode = payload.get("mode", "sequential")
        if mode not in db.GOAL_MODES:
            self._task_error(
                400, "invalid_request",
                "'mode' must be 'sequential' or 'dynamic'.")
            return
        try:
            conn = db.open_conn(self.cfg.db_path)
            try:
                goal = db.create_goal(conn, objective, status="queued",
                                      mode=mode)
                db.insert_goal_event(conn, goal["id"], "goal_created",
                                     "goal accepted for planning")
            finally:
                conn.close()
        except db.sqlite3.Error as exc:
            self._log_failure("goal create db error", exc)
            self._task_error(500, "internal_error",
                             "Failed to persist the goal.")
            return
        self._send_json(202, {
            "id": goal["id"],
            "status": goal["status"],
            "created_at": goal["created_at"],
        })

    def _handle_goals_list(self, send_body):
        try:
            if self._reject_cross_origin():
                return
            limit = DEFAULT_GOALS_LIMIT
            query = urlsplit(self.path).query
            if query:
                raw_limit = parse_qs(query).get("limit", [None])[0]
                if raw_limit is not None:
                    try:
                        limit = int(raw_limit)
                    except ValueError:
                        limit = -1
                    if not 1 <= limit <= MAX_GOALS_LIMIT:
                        self._task_error(
                            400, "invalid_request",
                            f"limit must be 1..{MAX_GOALS_LIMIT}.")
                        return
            conn = db.open_conn(self.cfg.db_path)
            try:
                rows = db.list_goals(conn, limit)
                goals = [{
                    "id": g["id"], "objective": g["objective"],
                    "status": g["status"], "mode": g["mode"],
                    "error_code": g["error_code"],
                    "cancel_requested": g["cancel_requested"],
                    "created_at": g["created_at"],
                    "updated_at": g["updated_at"],
                    "progress": self._goal_progress(conn, g["id"]),
                } for g in rows]
            finally:
                conn.close()
        except Exception as exc:
            self._log_failure("goal list error", exc)
            self._send_json(
                500, {"error": {"code": "internal_error",
                                "message": "Failed to list goals."}},
                send_body,
            )
            return
        self._send_json(200, {"goals": goals}, send_body)

    def _handle_goal_detail(self, goal_id, send_body):
        try:
            if self._reject_cross_origin():
                return
            goal_id = self._goal_id_of(goal_id)
            conn = db.open_conn(self.cfg.db_path)
            try:
                goal = db.get_goal(conn, goal_id) if goal_id else None
                if goal is None:
                    self._send_json(
                        404,
                        {"error": {"code": "goal_not_found",
                                   "message": "Unknown goal id."}},
                        send_body,
                    )
                    return
                steps = db.get_goal_steps(conn, goal["id"])
                payload = {
                    "id": goal["id"],
                    "objective": goal["objective"],
                    "status": goal["status"],
                    "mode": goal["mode"],
                    "dynamic_invalid_count":
                        goal.get("dynamic_invalid_count") or 0,
                    "error_code": goal["error_code"],
                    "cancel_requested": goal["cancel_requested"],
                    "created_at": goal["created_at"],
                    "updated_at": goal["updated_at"],
                    "repair_count": goal.get("repair_count") or 0,
                    "last_failure": self._goal_json_field(
                        goal.get("last_failure_json"))
                    if goal.get("last_failure_json") else None,
                    "plan": self._goal_json_field(goal["plan_json"]),
                    "report": self._goal_json_field(goal["report_json"]),
                    "progress": db.goal_progress(steps),
                    "steps": [self._goal_step_payload(r) for r in steps],
                    "events": db.get_goal_events(
                        conn, goal["id"], MAX_GOAL_DETAIL_EVENTS),
                }
            finally:
                conn.close()
        except Exception as exc:
            self._log_failure("goal detail error", exc)
            self._send_json(
                500, {"error": {"code": "internal_error",
                                "message": "Failed to read the goal."}},
                send_body,
            )
            return
        self._send_json(200, payload, send_body)

    def _handle_goal_action(self, path, action):
        """POST /api/goals/{id}/cancel|retry — state-machine gated."""
        if self._rate_limit_gate("goals"):
            return
        if self._reject_cross_origin():
            return
        goal_id = self._goal_id_of(
            path[len("/api/goals/"):].split("/", 1)[0])
        try:
            conn = db.open_conn(self.cfg.db_path)
            try:
                goal = db.get_goal(conn, goal_id) if goal_id else None
                if goal is None:
                    self._task_error(404, "goal_not_found",
                                     "Unknown goal id.")
                    return
                if action == "cancel":
                    self._goal_cancel(conn, goal)
                else:
                    self._goal_retry(conn, goal)
            finally:
                conn.close()
        except Exception as exc:
            self._log_failure("goal action error", exc)
            self._task_error(500, "internal_error",
                             "Goal action failed.")

    def _goal_cancel(self, conn, goal):
        fresh = db.get_goal(conn, goal["id"])
        status = fresh["status"]
        if goalstate.is_terminal(status):
            self._task_error(
                409, "invalid_state",
                f"Goal is already {status}.")
            return
        if status in ("queued", "blocked"):
            # both are immediately cancellable: no worker holds them
            if not db.update_goal_fields(conn, goal["id"], status,
                                         {"status": "cancelled"}):
                self._task_error(409, "invalid_state",
                                 "Goal changed state concurrently.")
                return
            db.insert_goal_event(conn, goal["id"], "goal_cancelled",
                                 "cancelled by operator")
            self._send_json(202, {"id": goal["id"],
                                  "status": "cancelled"})
            return
        # worker-held states: flag now; the next checkpoint in the
        # worker (between phases/steps) completes the transition
        db.update_goal_fields(conn, goal["id"], status,
                              {"cancel_requested": 1})
        self._send_json(202, {"id": goal["id"], "status": status,
                              "cancel_requested": True})

    def _goal_retry(self, conn, goal):
        fresh = db.get_goal(conn, goal["id"])
        if not goalstate.retryable(fresh["status"], fresh["error_code"]):
            self._task_error(
                409, "invalid_state",
                "Only blocked goals with a recoverable reason may be "
                "requeued (reconciliation and terminal goals are not).")
            return
        if not db.update_goal_fields(conn, fresh["id"], "blocked",
                                     {"status": "queued",
                                      "error_code": None}):
            self._task_error(409, "invalid_state",
                             "Goal changed state concurrently.")
            return
        db.insert_goal_event(conn, fresh["id"], "goal_retry",
                             "operator requeued a blocked goal")
        self._send_json(202, {"id": fresh["id"], "status": "queued"})

    # -- human write approvals (Phase 10A) ----------------------------------

    def _handle_goal_proposals(self, goal_id, seq, send_body):
        """GET /api/goals/{id}/proposals — digest-bound write proposal
        register (hashes and state only; content lives in preview)."""
        try:
            if self._reject_cross_origin():
                return
            goal_id = self._goal_id_of(goal_id)
            conn = db.open_conn(self.cfg.db_path)
            try:
                goal = db.get_goal(conn, goal_id) if goal_id else None
                if goal is None or seq is not None:
                    self._send_json(
                        404,
                        {"error": {"code": "goal_not_found",
                                   "message": "Unknown goal id."}},
                        send_body,
                    )
                    return
                proposals = [{
                    "seq": p["seq"],
                    "tool": p["tool_name"],
                    "path": p["path"],
                    "kind": p["kind"],
                    "before_sha256": p["before_sha256"],
                    "after_sha256": p["after_sha256"],
                    "bytes": p["bytes"],
                    "digest": p["digest"],
                    "state": p["state"],
                    "created_at": p["created_at"],
                    "approved_at": p["approved_at"],
                    "executed_at": p["executed_at"],
                } for p in db.list_goal_approvals(conn, goal["id"])]
            finally:
                conn.close()
        except Exception as exc:
            self._log_failure("goal proposals error", exc)
            self._send_json(
                500,
                {"error": {"code": "internal_error",
                           "message": "Failed to list proposals."}},
                send_body,
            )
            return
        self._send_json(200, {"goal_id": goal_id, "proposals": proposals},
                        send_body)

    def _handle_goal_proposal_preview(self, goal_id, seq_raw, send_body):
        """GET /api/goals/{id}/proposals/{seq} — the exact proposal
        preview: proposal content vs CURRENT filesystem, bounded."""
        try:
            if self._reject_cross_origin():
                return
            goal_id = self._goal_id_of(goal_id)
            try:
                seq = int(seq_raw)
            except ValueError:
                self._send_json(
                    404, {"error": {"code": "proposal_not_found",
                                    "message": "Malformed seq."}},
                    send_body)
                return
            if not 1 <= seq <= 12:
                self._send_json(
                    404, {"error": {"code": "proposal_not_found",
                                    "message": "Unknown proposal."}},
                    send_body)
                return
            conn = db.open_conn(self.cfg.db_path)
            try:
                goal = db.get_goal(conn, goal_id) if goal_id else None
                approval = (db.get_goal_approval(conn, goal_id, seq)
                            if goal is not None else None)
                if goal is None or approval is None:
                    self._send_json(
                        404,
                        {"error": {"code": "proposal_not_found",
                                   "message": "Unknown proposal."}},
                        send_body,
                    )
                    return
                step = next(
                    (s for s in db.get_goal_steps(conn, goal_id)
                     if s["seq"] == seq), None)
                payload = self._proposal_preview_payload(
                    approval, step, goal)
            finally:
                conn.close()
        except Exception as exc:
            self._log_failure("proposal preview error", exc)
            self._send_json(
                500,
                {"error": {"code": "internal_error",
                           "message": "Failed to build preview."}},
                send_body,
            )
            return
        self._send_json(200, payload, send_body)

    def _proposal_preview_payload(self, approval, step, goal):
        """Build the human-facing preview from the persisted proposal
        and the CURRENT file state. Bounded: diff lines/bytes capped."""
        content = ""
        if step is not None:
            try:
                args = json.loads(step["args_json"] or "{}")
            except ValueError:
                args = {}
            content = str(args.get("content") or args.get("new_text") or "")
        current_bytes = None
        current_exists = False
        ws_root = Path(self.cfg.workspace_root)
        kind = approval["kind"]
        if kind == "execute":
            # The recipe executes the workspace's CURRENT state by
            # design (that is what running verification means); the
            # binding approves the runner + the exact target path under
            # the exact plan snapshot, never a frozen file version.
            target_probe = ws_root / (approval["path"] or "")
            try:
                current_exists = bool(target_probe.is_file())
            except OSError:
                current_exists = False
            preview = {
                "seq": approval["seq"],
                "tool": approval["tool_name"],
                "path": approval["path"],
                "kind": kind,
                "state": approval["state"],
                "digest": approval["digest"],
                "bytes": approval["bytes"],
                "verification": {
                    "runner": "python3 -m unittest -v <module>",
                    "target": approval["path"] or "(workspace)",
                    "cwd": "workspace root",
                    "timeout_seconds": 30,
                    "executes_project_code": True,
                    "risk": "the recipe runs with this process's OS "
                            "permissions; no OS sandbox is enforced.",
                    "environment": "minimal (no secrets inherited; PATH "
                                   "only)",
                    "authorized_by": "digest-bound human approval",
                },
                "current": {
                    "exists": current_exists,
                    "matches_approved_before": None,
                },
                "content": "",
                "diff": [],
                "diff_truncated": False,
            }
            return {"goal_id": goal["id"], "goal_status": goal["status"],
                    "preview": preview}
        target = ws_root / approval["path"]
        try:
            if target.is_file():
                current_bytes = target.read_bytes()[:60000]
                current_exists = True
        except OSError:
            current_bytes = None
        current_sha = (hashlib.sha256(current_bytes).hexdigest()
                       if current_bytes is not None else None)
        before_match = None
        if approval["kind"] == "create":
            # approved state assumes the path does not exist yet
            before_match = not current_exists
        else:
            before_match = bool(current_sha)
            if current_sha is not None:
                before_match = True if hmac.compare_digest(
                    current_sha, approval["before_sha256"] or "") else False
        ai_before = None
        ai_after = None
        if current_bytes is not None:
            ai_before = current_bytes.decode("utf-8", "replace")
        if content:
            ai_after = content
        diff_lines = []
        truncated = False
        if ai_before is not None or ai_after is not None:
            diff = difflib.unified_diff(
                (ai_before or "").splitlines(keepends=True),
                (ai_after or "").splitlines(keepends=True),
                fromfile=f"current:{approval['path']}",
                tofile=f"proposed:{approval['path']}",
            )
            for line in diff:
                if len(diff_lines) >= MAX_PREVIEW_DIFF_LINES:
                    truncated = True
                    break
                diff_lines.append(line.rstrip("\r"))
                if len("\n".join(diff_lines)) > MAX_PREVIEW_DIFF_BYTES:
                    diff_lines.pop()
                    truncated = True
                    break
        preview = {
            "seq": approval["seq"],
            "tool": approval["tool_name"],
            "path": approval["path"],
            "kind": approval["kind"],
            "state": approval["state"],
            "digest": approval["digest"],
            "before_sha256": approval["before_sha256"],
            "after_sha256": approval["after_sha256"],
            "bytes": approval["bytes"],
            "content": content,
            "current": {
                "exists": current_exists,
                "sha256": current_sha,
                "matches_approved_before": before_match,
            },
            "diff": diff_lines,
            "diff_truncated": truncated,
        }
        return {"goal_id": goal["id"], "goal_status": goal["status"],
                "preview": preview}

    def _handle_goal_approval(self, path, action):
        """POST /api/goals/{id}/approve|reject — the human decision.

        Approval is accepted ONLY for the exact digest bound to the
        persisted proposal, only in status awaiting_approval, only in
        state 'proposed', and only when the CURRENT filesystem still
        matches the approved before-state. Anything else refuses."""
        if self._rate_limit_gate("goals"):
            return
        if self._reject_cross_origin():
            return
        goal_id = self._goal_id_of(
            path[len("/api/goals/"):].split("/", 1)[0])
        payload = self._read_json_payload(MAX_GOAL_BODY_BYTES)
        if payload is None:
            return
        unknown = set(payload) - {"seq", "digest"}
        if unknown:
            self._task_error(
                400, "invalid_request",
                "Unknown request fields: "
                + ", ".join(sorted(unknown)) + ".")
            return
        seq = payload.get("seq")
        digest = payload.get("digest", "")
        if not isinstance(seq, int) or isinstance(seq, bool) \
                or not 1 <= seq <= 12:
            self._task_error(
                400, "invalid_request",
                "'seq' must be an integer 1..12.")
            return
        if action == "approve" and \
                (not isinstance(digest, str) or len(digest) != 64):
            self._task_error(
                400, "invalid_request",
                "'digest' must be the 64-char proposal digest.")
            return
        try:
            conn = db.open_conn(self.cfg.db_path)
            try:
                goal = db.get_goal(conn, goal_id) if goal_id else None
                if goal is None:
                    self._task_error(404, "goal_not_found",
                                     "Unknown goal id.")
                    return
                if action == "approve":
                    self._approve_proposal(conn, goal, seq, digest)
                else:
                    self._reject_proposal(conn, goal, seq)
            finally:
                conn.close()
        except Exception as exc:
            self._log_failure("goal approval error", exc)
            self._task_error(500, "internal_error",
                             "Approval decision failed.")

    def _approve_proposal(self, conn, goal, seq, requested_digest):
        if goal["status"] != "awaiting_approval":
            self._task_error(
                409, "invalid_state",
                "Approvals are accepted only while the goal is "
                "awaiting_approval.")
            return
        approval = db.get_goal_approval(conn, goal["id"], seq)
        if approval is None:
            self._task_error(404, "proposal_not_found",
                             "Unknown proposal for this goal.")
            return
        if approval["state"] != "proposed":
            self._task_error(
                409, "invalid_state",
                "This proposal was already decided.")
            return
        # Rebuild the digest from the PERSISTED step arguments + the
        # persisted plan; drift anywhere refuses the approval.
        step = next((s for s in db.get_goal_steps(conn, goal["id"])
                     if s["seq"] == seq), None)
        if step is None:
            self._task_error(409, "plan_drifted",
                             "The proposed step is missing.")
            return
        plan_sha = ""
        if goal.get("plan_json"):
            plan_sha = hashlib.sha256(goal["plan_json"].encode()).hexdigest()
        if plan_sha != approval["plan_sha256"]:
            self._task_error(
                409, "plan_drifted",
                "The plan snapshot changed since this proposal was "
                "recorded.")
            return
        try:
            args = json.loads(step["args_json"] or "{}")
        except ValueError:
            args = {}
        rebuilt = approvals.digest_for(
            goal["id"], seq, approval["tool_name"], args,
            approval["plan_sha256"])
        if rebuilt["digest"] != approval["digest"] \
                or not approvals.digests_equal(
                    requested_digest, approval["digest"]):
            db.insert_goal_event(
                conn, goal["id"], "approval_refused",
                f"seq {seq}: digest mismatch; proposal unchanged")
            self._task_error(
                403, "digest_mismatch",
                "Digest does not match the exact proposal on record.")
            return
        # Current filesystem state check: the approved write must see
        # exactly the before-state the human signed off on.
        current_error = self._proposal_target_current_error(
            approval, goal)
        if current_error is not None:
            self._task_error(
                409, "target_changed",
                current_error)
            return
        if not db.update_goal_approval_state(
                conn, goal["id"], seq, "proposed", "approved",
                {"approved_at": self._approval_now()}):
            self._task_error(409, "invalid_state",
                             "Approval raced with another decision.")
            return
        db.insert_goal_event(
            conn, goal["id"], "proposal_approved",
            f"seq {seq}: approved (digest-bound) by operator")
        remaining = [
            p["seq"] for p in db.list_goal_approvals(conn, goal["id"])
            if p["state"] == "proposed"]
        self._send_json(200, {
            "id": goal["id"], "seq": seq, "state": "approved",
            "remaining_proposals": remaining,
        })

    def _reject_proposal(self, conn, goal, seq):
        if goal["status"] != "awaiting_approval":
            self._task_error(
                409, "invalid_state",
                "This goal is not waiting for approvals.")
            return
        approval = db.get_goal_approval(conn, goal["id"], seq)
        if approval is None:
            self._task_error(404, "proposal_not_found",
                             "Unknown proposal for this goal.")
            return
        if approval["state"] not in ("proposed",):
            self._task_error(409, "invalid_state",
                             "This proposal was already decided.")
            return
        if not db.update_goal_approval_state(
                conn, goal["id"], seq, "proposed", "rejected"):
            self._task_error(409, "invalid_state",
                             "Rejection raced with another decision.")
            return
        db.insert_goal_event(
            conn, goal["id"], "proposal_rejected",
            f"seq {seq}: rejected by operator; goal will not run")
        remaining = [
            p["seq"] for p in db.list_goal_approvals(conn, goal["id"])
            if p["state"] == "proposed"]
        self._send_json(200, {
            "id": goal["id"], "seq": seq, "state": "rejected",
            "remaining_proposals": remaining,
        })

    def _proposal_target_current_error(self, approval, goal):
        """Return an error string when the CURRENT file state no longer
        matches what the human is approving; None when it matches.
        'execute' bindings approve running verification against the
        workspace's CURRENT state (that is the semantics of running
        tests), so their gate is the digest + plan snapshot, not a
        frozen byte state."""
        ws_root = Path(self.cfg.workspace_root)
        target = ws_root / approval["path"]
        if approval["kind"] == "execute":
            if approval["tool_name"] == "workspace.run_project_tests":
                # the recipe re-guards its own target at run time
                return None
            return None
        if approval["kind"] == "create":
            if target.exists():
                return ("Target path already exists; the approved "
                        "create proposal is no longer valid.")
            return None
        try:
            current_bytes = target.read_bytes()
        except OSError:
            return ("Target file is missing; the approved edit "
                    "proposal is no longer valid.")
        current_sha = hashlib.sha256(current_bytes).hexdigest()
        if not hmac.compare_digest(
                current_sha, approval["before_sha256"] or ""):
            return ("Target file changed since the approved preview; "
                    "the edit proposal is no longer valid.")
        return None

    @staticmethod
    def _approval_now():
        return datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")



def build_handler(db_path, cfg=None):
    class Handler(_Handler):
        pass
    Handler.db_path = db_path
    base = cfg if cfg is not None else config.load(environ={})
    Handler.base_cfg = base
    Handler.metrics = metrics.MetricsService(
        data_dir=Path(base.db_path).parent,
        cpu_sampler=metrics.CpuSampler(),
    )

    def live_cfg(self):
        svc = getattr(self.server, "settings_service", None)
        if svc is None:
            return self.base_cfg
        return svc.snapshot()

    Handler.cfg = property(live_cfg)
    return Handler


class EffectiveConfig:
    """Thread-safe swap point for the merged effective configuration.

    Reads are plain attribute reads of one immutable Config; only an
    explicit settings apply replaces it (atomically for every reader).
    """

    def __init__(self, base_cfg, store):
        self.base_cfg = base_cfg
        self.store = store
        self.environ = store.environ
        self._cfg = settingsstore.effective_config(base_cfg, store)

    def snapshot(self):
        return self._cfg

    def apply_values(self):
        self._cfg = settingsstore.effective_config(self.base_cfg, self.store)
        return self._cfg


class _ConfigAttributeProxy:
    """Attribute-passthrough view of the current configuration snapshot
    for components that read `self.cfg.<field>` at use time (goal worker).
    One model call inside a worker phase pins its own local snapshot."""

    def __init__(self, service):
        object.__setattr__(self, "_service", service)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_service").snapshot(),
                       name)


class JARVISServer(ThreadingHTTPServer):
    """ThreadingHTTPServer whose close also stops the goal worker first
    (a clean worker teardown belongs to the server lifecycle)."""

    goal_worker = None

    def server_close(self):
        if self.goal_worker is not None:
            self.goal_worker.stop()
            self.goal_worker = None
        ThreadingHTTPServer.server_close(self)


def create_server(host, port, db_path, init_db=True, cfg=None,
                  start_worker=True, settings_dir=None):
    if init_db:
        db.init_db(db_path)
    handler = build_handler(db_path, cfg)
    httpd = JARVISServer((host, port), handler)
    # bounded, in-memory limiter; resets when the process ends
    httpd.rate_limiter = ratelimit.RateLimiter(RATE_LIMIT_POLICIES)
    # Phase 7A per-server model catalog cache; filled only on explicit
    # user action, never background-refreshed
    httpd.model_catalog = {"models": None, "fetched_at": 0.0, "error": None}
    httpd.last_inference = None
    # Phase 11A: persisted user configuration outside the repository,
    # then one merged effective configuration every reader resolves.
    # The store resolves resolution against the exact environ the base
    # configuration was built from (never a wider secret universe).
    directory = settings_dir if settings_dir is not None \
        else str(settingsstore.config_dir(
            getattr(handler.base_cfg, "environ_source", None) or os.environ))
    environ = getattr(handler.base_cfg, "environ_source", None) or os.environ
    store = settingsstore.SettingsStore(directory, environ=environ)
    service = EffectiveConfig(handler.base_cfg, store)
    httpd.settings_service = service
    # Phase 13A: the vault service resolves the configured root at every
    # call (env > user store), so a settings change applies without a
    # restart; its tools register in the same permission boundary.
    vault_service = vault_mod.VaultService(
        service.snapshot().db_path, environ=environ, store=store)
    httpd.vault_service = vault_service
    httpd.provider_catalogs = {
        "freellmapi": httpd.model_catalog,
        "orcarouter": {"models": None, "fetched_at": 0.0, "error": None},
    }
    # Phase 8B: lifecycle-managed execution worker over the SAME db.
    # The worker resolves cfg attributes at use time so an applied
    # settings change reaches subsequent goals without a restart.
    httpd.goal_worker = goal_worker.GoalWorker(
        db_path=service.snapshot().db_path,
        cfg=_ConfigAttributeProxy(service),
        registry=tools.default_registry(
            service.snapshot().workspace_root, vault_service),
        # Phase 9B: the independent plan reviewer is configured via
        # JARVIS_REVIEWER only (opt-in); absence keeps the honest
        # local-validation-only policy.
        reviewer=reviewer_mod.default_reviewer(service.snapshot()),
    )
    if start_worker:
        httpd.goal_worker.recover()
        httpd.goal_worker.start()
    return httpd


def main():
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    try:
        httpd = create_server(cfg.host, cfg.port, cfg.db_path, cfg=cfg)
    except (config.ConfigError, db.SchemaError, OSError) as exc:
        print(f"startup failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"JARVIS listening on http://{cfg.host}:{cfg.port}", flush=True)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("JARVIS stopped cleanly", flush=True)


if __name__ == "__main__":
    main()
