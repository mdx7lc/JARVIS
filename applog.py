"""Privacy-safe structured operational logging for JARVIS.

Every event is exactly one JSON line on stderr:
{"ts": ..., "event": ..., ...fields}. Field values are sanitized
(control characters removed, strings truncated) so untrusted input
cannot forge extra log lines.

Fixed field contract:
- ts          UTC ISO-8601 timestamp (server clock)
- event       event name (http_request, handler_error, rate_limited, ...)
- request_id  `req_` + 24 lowercase hex, generated server-side per
              request; client headers are never accepted as the
              authoritative id and it is never an authentication token
- method      HTTP method
- route       normalized path WITHOUT query parameters (path ids are
              masked to bound cardinality and keep content out)
- status      response status
- duration_ms rounded request duration
- note        short fixed diagnostic category (never exception text)

Never logged: secrets/authorization headers, prompts or message
content, task titles/descriptions, search queries, request/response
bodies, full provider responses, cookies/session ids, absolute
filesystem paths. stderr is a stream — no persistent log store.
"""

import json
import sys
import threading
import time
import uuid

_MAX_FIELD_CHARS = 200
_WRITE_LOCK = threading.Lock()

_CONTROL_TRANSLATION = {i: "?" for i in range(32)}


def _sanitize(value, limit=_MAX_FIELD_CHARS):
    """Bound and clean an untrusted value for one log field."""
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value).translate(_CONTROL_TRANSLATION)[:limit]


def event(name, **fields):
    """Emit one structured JSON log line (best-effort, never raises)."""
    record = {"ts": _utc_now(), "event": _sanitize(name, 40)}
    record.update(
        {_sanitize(k, 40): _sanitize(v) for k, v in fields.items()}
    )
    try:
        with _WRITE_LOCK:
            sys.stderr.write(json.dumps(record, ensure_ascii=True) + "\n")
            sys.stderr.flush()
    except Exception:
        # logging must never take the request path down
        try:
            sys.stderr.write(
                '{"ts": "%s", "event": "log_failure"}\n' % _utc_now()
            )
        except Exception:
            pass


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_request_id():
    """Server-generated bounded-format request id."""
    return "req_" + uuid.uuid4().hex[:24]
