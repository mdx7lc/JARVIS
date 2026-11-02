"""FreeLLMAPI provider adapter — stdlib only.

Isolates all communication with the local OpenAI-compatible gateway from
HTTP routing and storage. Loopback destinations only; the API key never
leaves this module's request headers and is never logged.

Error taxonomy (each maps to a stable HTTP status):
  not_configured     503  credential absent
  invalid_config     503  malformed/untrusted provider settings
  auth_failed        502  upstream rejected credentials
  rate_limited       429  upstream throttling
  provider_timeout   504  upstream did not respond in time
  connection_failed  502  network/TLS/socket failure
  request_rejected   502  upstream refused the request (4xx)
  upstream_error     502  upstream server error (5xx)
  redirect_rejected  502  upstream tried to redirect an authenticated call
  malformed_response 502  unusable response structure
  empty_response     502  assistant text missing or blank

Phase 8A adds a bounded primary/fallback routing interface
(`complete_with_fallback`) and a parser for proposed tool calls
(`parse_tool_proposals`) as safe groundwork for the future orchestrator;
proposals are data only and are never executed here.

Phase 11A adds the OrcaRouter provider profile: one fixed, allowlisted
remote base (https://api.orcarouter.ai/v1 — an OpenAI-compatible
gateway; see docs/JARVIS_ORCHESTRATOR_ARCHITECTURE.md) plus bounded
fetchers for its documented PUBLIC pricing/free-package catalogs. These
public endpoints never receive the API key. Pricing classification
(free_verified / paid / unknown) is computed ONLY from fields the
catalog actually returns and honestly reports "unknown" otherwise.
No client request can ever change a provider base URL, a pricing URL
or any other destination.
"""

import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import applog

DEFAULT_API_BASE = "http://localhost:3001"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
MAX_RESPONSE_BYTES = 2_000_000
_ALLOWED_ROLES = {"system", "user", "assistant"}

# ---- Phase 11A: fixed provider profiles ------------------------------
# Base URLs live ONLY here, server-side. HTTP handlers resolve a
# provider profile by name; no request payload can supply a URL.
ORCAROUTER_BASE_URL = "https://api.orcarouter.ai/v1"
ORCAROUTER_HOST = "api.orcarouter.ai"
ORCAROUTER_PRICING_URL = "https://api.orcarouter.ai/api/pricing"
ORCAROUTER_FREE_URL = "https://api.orcarouter.ai/api/free-package/public"

#: Default model policies per provider. The FreeLLMAPI default is the
#: historical gateway id; OrcaRouter's default is its documented auto
#: router. Both are identifiers, not verified-availability promises.
PROVIDER_DEFAULT_MODEL = {
    "freellmapi": "qwen3.8-27b",
    "orcarouter": "orcarouter/auto",
}

PROVIDER_PROFILES = {
    "freellmapi": {
        "display": "FreeLLMAPI (local gateway)",
        "base_url": DEFAULT_API_BASE,
        "loopback": True,
    },
    "orcarouter": {
        "display": "OrcaRouter",
        "base_url": ORCAROUTER_BASE_URL,
        "loopback": False,
    },
}

MAX_PRICING_RESPONSE_BYTES = 3_000_000


class ProviderError(Exception):
    code = "provider_error"
    http_status = 502
    user_message = "JARVIS could not complete the AI request."


class NotConfigured(ProviderError):
    code = "not_configured"
    http_status = 503
    user_message = "AI chat is not configured (missing provider credential)."


class InvalidConfig(ProviderError):
    code = "invalid_config"
    http_status = 503
    user_message = "AI provider configuration is invalid."


class AuthFailed(ProviderError):
    code = "auth_failed"
    http_status = 502
    user_message = "The AI provider rejected the credentials."


class RateLimited(ProviderError):
    code = "rate_limited"
    http_status = 429
    user_message = "The AI provider is rate limiting requests. Try again later."


class ProviderTimeout(ProviderError):
    code = "provider_timeout"
    http_status = 504
    user_message = "The AI provider did not respond in time."


class ConnectionFailed(ProviderError):
    code = "connection_failed"
    http_status = 502
    user_message = "JARVIS could not reach its AI provider."


class RequestRejected(ProviderError):
    code = "request_rejected"
    http_status = 502
    user_message = "The AI provider rejected the request."


class UpstreamError(ProviderError):
    code = "upstream_error"
    http_status = 502
    user_message = "The AI provider reported a server error."


class RedirectRejected(ProviderError):
    code = "redirect_rejected"
    http_status = 502
    user_message = "The AI provider attempted an unauthorized redirect."


class MalformedResponse(ProviderError):
    code = "malformed_response"
    http_status = 502
    user_message = "The AI provider returned an unusable response."


class EmptyResponse(ProviderError):
    code = "empty_response"
    http_status = 502
    user_message = "The AI provider returned an empty response."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: an authenticated request must not follow 30x."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectRejected()


def normalize_base(raw_base, *, default=DEFAULT_API_BASE):
    """Normalize the gateway base URL so '/v1' appears exactly once.

    Accepts '', '/', '/v1', '/v1/' paths on loopback hosts, or the one
    fixed allowlisted remote base (OrcaRouter). Raises InvalidConfig for
    anything else — remote, malformed, or suspicious.
    """
    raw = (raw_base or "").strip() or default
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise InvalidConfig("provider URL scheme must be http or https")
    host = (parts.hostname or "").lower()
    if parts.username is not None or parts.password is not None:
        raise InvalidConfig("provider URL must not contain credentials")
    if parts.query or parts.fragment:
        raise InvalidConfig("provider URL must not contain query or fragment")
    if parts.scheme == "https" and host == ORCAROUTER_HOST:
        # the single allowlisted remote profile: default port only, and
        # only the documented base paths (chat_completion appends /v1)
        if parts.port is not None:
            raise InvalidConfig("provider URL must use the default port")
    elif host in LOOPBACK_HOSTS:
        # loopback local gateway (existing Phase 2B/7A behavior)
        pass
    else:
        raise InvalidConfig(
            "provider URL must point to a loopback host or the "
            "allowlisted OrcaRouter gateway"
        )
    path = parts.path
    if path in ("", "/", "/v1", "/v1/"):
        base_path = ""  # /v1 is appended exactly once by chat_completion
    else:
        raise InvalidConfig("provider URL base path must be empty or /v1")
    port = f":{parts.port}" if parts.port else ""
    host_part = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host_part}{port}{base_path}"


def _validated_payload(messages, model, max_tokens):
    if not isinstance(model, str) or not model.strip():
        raise InvalidConfig("provider model is not configured")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) \
            or not 1 <= max_tokens <= 4096:
        raise InvalidConfig("max_tokens is out of range")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            raise ValueError("each message must be a dict")
        role = msg.get("role")
        content = msg.get("content")
        if role not in _ALLOWED_ROLES or not isinstance(content, str) \
                or not content.strip():
            raise ValueError("invalid role or empty content in messages")
        cleaned.append({"role": role, "content": content})
    return {
        "model": model.strip(),
        "messages": cleaned,
        "stream": False,
        "max_tokens": max_tokens,
    }


def _log(category, detail):
    # Fixed sanitized category strings only; routed through the shared
    # structured logger so the whole operative log stream is uniform.
    applog.event("provider_error", code=category, note=detail)


def _map_http_error(exc):
    status = exc.code
    if status in (401, 403):
        err = AuthFailed()
    elif status == 429:
        err = RateLimited()
    elif 400 <= status < 500:
        err = RequestRejected()
    else:
        err = UpstreamError()
    _log(err.code, f"upstream HTTP {status}")
    return err


def _parse_success(resp, *, include_model=False):
    content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0]
    if content_type.strip().lower() != "application/json":
        _log("malformed_response", "unexpected content type")
        raise MalformedResponse()
    data = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        _log("malformed_response", "response exceeds size limit")
        raise MalformedResponse()
    if not data:
        _log("malformed_response", "empty body")
        raise MalformedResponse()
    try:
        payload = json.loads(data)
    except ValueError:
        _log("malformed_response", "body is not valid JSON")
        raise MalformedResponse() from None
    if not isinstance(payload, dict):
        _log("malformed_response", "payload is not an object")
        raise MalformedResponse()
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices \
            or not isinstance(choices[0], dict):
        _log("malformed_response", "missing choices")
        raise MalformedResponse()
    message = choices[0].get("message")
    if not isinstance(message, dict):
        _log("malformed_response", "missing message object")
        raise MalformedResponse()
    content = message.get("content")
    if not isinstance(content, str):
        _log("malformed_response", "missing message content")
        raise MalformedResponse()
    text = content.strip()
    if not text:
        _log("empty_response", "assistant text is blank")
        raise EmptyResponse()
    if not include_model:
        return text
    # Phase 9A: the gateway's own `model` echo, reported as-is. It is
    # gateway metadata (may be a routing alias) — never proof of which
    # concrete upstream model answered. Callers must not claim more.
    raw_model = payload.get("model")
    return text, raw_model if isinstance(raw_model, str) else None


def list_models(*, base_url, api_key, timeout_seconds=60):
    """Fetch the authenticated model catalog from the gateway (Phase 7A).

    Returns a list of normalized entries:
      {"id": str, "available": bool, ["unavailable_reason": str],
       ["display_name": str]}
    Availability flags and display names pass through only when the
    gateway actually provides them (Phase 9A adds display_name for
    evidence-based catalog matching); a listed entry without flags
    counts as available. Never returns raw upstream bodies and never
    exposes the key.

    Raises a ProviderError subclass on every failure mode, with the same
    taxonomy and transport protections as chat_completion.
    """
    key = (api_key or "").strip()
    if not key:
        raise NotConfigured()
    base = normalize_base(base_url)
    url = base + "/v1/models"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "JARVIS/0.2",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as resp:
            return _parse_model_list(resp)
    except ProviderError:
        raise
    except urllib.error.HTTPError as exc:
        raise _map_http_error(exc) from None
    except (TimeoutError, socket.timeout):
        _log("provider_timeout", f"no response within {timeout_seconds}s")
        raise ProviderTimeout() from None
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            _log("provider_timeout", f"no response within {timeout_seconds}s")
            raise ProviderTimeout() from None
        _log("connection_failed", "could not connect to provider")
        raise ConnectionFailed() from None
    except OSError:
        _log("connection_failed", "socket failure talking to provider")
        raise ConnectionFailed() from None


def _parse_model_list(resp):
    content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0]
    if content_type.strip().lower() != "application/json":
        _log("malformed_response", "unexpected content type")
        raise MalformedResponse()
    data = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        _log("malformed_response", "response exceeds size limit")
        raise MalformedResponse()
    if not data:
        _log("malformed_response", "empty body")
        raise MalformedResponse()
    try:
        payload = json.loads(data)
    except ValueError:
        _log("malformed_response", "body is not valid JSON")
        raise MalformedResponse() from None
    if not isinstance(payload, dict):
        _log("malformed_response", "payload is not an object")
        raise MalformedResponse()
    # OpenAI-compatible list: {"object": "list", "data": [...]}
    entries = payload.get("data")
    if not isinstance(entries, list):
        _log("malformed_response", "model list missing data")
        raise MalformedResponse()
    models = []
    for entry in entries:
        # Defensive: junk entries (non-objects, missing/blank ids) are
        # skipped, never echoed to clients; they are not protocol
        # violations of the list envelope itself.
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if not model_id or len(model_id) > 128 \
                or any(ch.isspace() for ch in model_id):
            continue
        model = {"id": model_id, "available": True}
        if isinstance(entry.get("available"), bool):
            model["available"] = entry["available"]
        reason = entry.get("unavailable_reason")
        if isinstance(reason, str) and reason.strip():
            model["unavailable_reason"] = reason.strip()
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            name = name.strip()
            model["display_name"] = name[:200]
        # Phase 11A: verified catalog metadata passes through ONLY when
        # the gateway actually supplies it (OrcaRouter shape). Nothing
        # is invented here: absent fields stay absent.
        owned = entry.get("owned_by")
        if isinstance(owned, str) and owned.strip() and len(owned) <= 64:
            model["owned_by"] = owned.strip()
        endpoints = entry.get("supported_endpoint_types")
        if isinstance(endpoints, list) and len(endpoints) <= 8:
            safe = [e for e in endpoints
                    if isinstance(e, str) and e.strip()
                    and len(e) <= 32 and all(ch.isalnum() or ch in "-_" for ch in e)]
            if safe:
                model["supported_endpoint_types"] = safe[:4]
        ctx = entry.get("context_length")
        if isinstance(ctx, int) and not isinstance(ctx, bool) and \
                0 < ctx <= 10_000_000:
            model["context_length"] = ctx
        out_tokens = entry.get("max_completion_tokens")
        if isinstance(out_tokens, int) and not isinstance(out_tokens, bool) \
                and 0 < out_tokens <= 10_000_000:
            model["max_completion_tokens"] = out_tokens
        models.append(model)
    return models


# ---- Phase 11A: public pricing/free catalogs (OrcaRouter) -------------
# Documented PUBLIC endpoints (no Authorization is ever sent). These
# fetchers reuse the same transport protections as the catalog: bounded
# reads, redirect refusal, safe taxonomy errors.

def classify_pricing(price_entry_dict, *, in_free_package=False):
    """Honest cost classification from documented catalog evidence only.

    free_verified  entry explicitly marked free (is_free_tier true or
                   listed in the free package catalog) or a zero-cost
                   pricing entry (a per-token ratio of 0 or a per-request
                   price of 0).
    paid           a nonzero per-token ratio or per-request price.
    unknown        no entry / insufficient / contradictory evidence.

    Never infers cost from the model id substring alone.
    """
    if not isinstance(price_entry_dict, dict):
        return "unknown"
    ratios = []
    explicit_zero = False
    nonzero = False
    model_ratio = price_entry_dict.get("model_ratio")
    model_price = price_entry_dict.get("model_price")
    completion_ratio = price_entry_dict.get("completion_ratio")
    for value in (model_ratio, model_price, completion_ratio):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if float(value) > 0:
                ratios.append(float(value))
            nonzero = nonzero or float(value) > 0
        # quota_type 1 + model_price is a per-request price; 0 stays free
        if isinstance(value, (int, float)) and float(value) == 0:
            explicit_zero = True
    is_free_flag = price_entry_dict.get("is_free_tier")
    monetized = (price_entry_dict.get("quota_type") == 0
                 and isinstance(model_ratio, (int, float))
                 and float(model_ratio) > 0)
    can_be_free = not monetized and not nonzero
    if is_free_flag is True or in_free_package or \
            (explicit_zero and can_be_free and not ratios):
        return "free_verified"
    if monetized or nonzero:
        return "paid"
    if is_free_flag is False or price_entry_dict.get("quota_type") == 0:
        return "paid"
    return "unknown"


def _fetch_public_json(url, timeout_seconds):
    """Bounded GET of a documented public catalog URL (no credentials,
    redirects refused). Returns the parsed dict; taxonomy errors only."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "JARVIS/0.2",
            # explicit: a public endpoint must not carry or leak a key
            "Authorization": "",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as resp:
            content_type = (resp.headers.get("Content-Type")
                            or "").split(";", 1)[0]
            if content_type.strip().lower() != "application/json":
                _log("malformed_response", "unexpected content type")
                raise MalformedResponse()
            data = resp.read(MAX_PRICING_RESPONSE_BYTES + 1)
            if len(data) > MAX_PRICING_RESPONSE_BYTES:
                _log("malformed_response", "pricing response exceeds size limit")
                raise MalformedResponse()
            if not data:
                _log("malformed_response", "empty body")
                raise MalformedResponse()
            payload = json.loads(data)
            if not isinstance(payload, dict):
                _log("malformed_response", "payload is not an object")
                raise MalformedResponse()
            return payload
    except ProviderError:
        raise
    except json.JSONDecodeError:
        _log("malformed_response", "body is not valid JSON")
        raise MalformedResponse() from None
    except urllib.error.HTTPError as exc:
        raise _map_http_error(exc) from None
    except (TimeoutError, socket.timeout):
        _log("provider_timeout", f"no response within {timeout_seconds}s")
        raise ProviderTimeout() from None
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            _log("provider_timeout", f"no response within {timeout_seconds}s")
            raise ProviderTimeout() from None
        _log("connection_failed", "could not connect to provider")
        raise ConnectionFailed() from None
    except OSError:
        _log("connection_failed", "socket failure talking to provider")
        raise ConnectionFailed() from None


def fetch_pricing_catalog(*, pricing_url=None, timeout_seconds=30):
    """Parse the public pricing catalog into {model_name: verified fields}.

    Verified fields only, exactly as supplied, bounded; no dollar
    arithmetic (the catalog speaks in provider ratios — those are passed
    through raw as evidence, never converted into invented prices).
    """
    payload = _fetch_public_json(
        (pricing_url or ORCAROUTER_PRICING_URL).strip(), timeout_seconds)
    rows = payload.get("data")
    if not isinstance(rows, list):
        _log("malformed_response", "pricing catalog missing data")
        raise MalformedResponse()
    pricing = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_name = row.get("model_name")
        if not isinstance(model_name, str) or not model_name.strip():
            continue
        model_name = model_name.strip()
        if len(model_name) > 128:
            continue
        entry = {}
        if isinstance(row.get("display_name"), str) and \
                row["display_name"].strip():
            entry["display_name"] = row["display_name"].strip()[:200]
        for key in ("quota_type", "model_ratio", "model_price",
                    "completion_ratio"):
            value = row.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and abs(float(value)) < 1e9:
                entry[key] = value
        if isinstance(row.get("is_free_tier"), bool):
            entry["is_free_tier"] = row["is_free_tier"]
        free_base = row.get("free_base_model")
        if isinstance(free_base, str) and free_base.strip() \
                and len(free_base) <= 128:
            entry["free_base_model"] = free_base.strip()
        pricing[model_name] = entry
    return pricing


def fetch_free_package(*, free_url=None, timeout_seconds=30):
    """Parse the public free-package catalog into a set of free ids."""
    payload = _fetch_public_json(
        (free_url or ORCAROUTER_FREE_URL).strip(), timeout_seconds)
    data = payload.get("data")
    if not isinstance(data, dict):
        _log("malformed_response", "free package missing data")
        raise MalformedResponse()
    rows = data.get("models")
    if not isinstance(rows, list):
        _log("malformed_response", "free package models missing")
        raise MalformedResponse()
    free_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        alias = row.get("free_alias")
        if isinstance(alias, str) and alias.strip() \
                and len(alias) <= 128:
            free_ids.add(alias.strip())
    return free_ids


def merge_catalog_entries(models, *, pricing=None, free_ids=None):
    """Attach honest pricing classification to normalized catalog rows.

    `pricing`/`free_ids` are the parsed public catalogs (may be None
    when the fetch failed — then every model is honestly 'unknown').
    Does not drop unavailable entries and never invents availability.
    """
    pricing = pricing or {}
    free_ids = free_ids or set()
    merged = []
    for m in models:
        out = dict(m)
        pricing_entry = pricing.get(m["id"])
        out["pricing_class"] = classify_pricing(
            pricing_entry, in_free_package=m["id"] in free_ids)
        entry = pricing.get(m["id"]) or {}
        if entry.get("free_base_model"):
            out["free_base_model"] = entry["free_base_model"]
        if isinstance(entry.get("is_free_tier"), bool):
            out["pricing_is_free_tier"] = entry["is_free_tier"]
        display = out.get("display_name") or entry.get("display_name")
        if display:
            out["display_name"] = display
        merged.append(out)
    return merged


def chat_completion(messages, *, base_url, api_key,
                    model, timeout_seconds=60, max_tokens=512,
                    include_model=False):
    """Request one non-streaming assistant reply. Returns assistant text.

    With include_model=True (Phase 9A operator tooling only) returns
    (text, reported_model) where reported_model is the gateway's own
    `model` echo or None — metadata only, never a proof of the
    concrete upstream model.

    Raises a ProviderError subclass on every failure mode; ValueError for
    internal caller-contract violations (server validates client input first).
    """
    key = (api_key or "").strip()
    if not key:
        raise NotConfigured()
    base = normalize_base(base_url)
    payload = _validated_payload(messages, model, max_tokens)
    url = base + "/v1/chat/completions"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "JARVIS/0.2",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout_seconds) as resp:
            return _parse_success(resp, include_model=include_model)
    except ProviderError:
        raise
    except urllib.error.HTTPError as exc:
        raise _map_http_error(exc) from None
    except (TimeoutError, socket.timeout):
        _log("provider_timeout", f"no response within {timeout_seconds}s")
        raise ProviderTimeout() from None
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout)):
            _log("provider_timeout", f"no response within {timeout_seconds}s")
            raise ProviderTimeout() from None
        _log("connection_failed", "could not connect to provider")
        raise ConnectionFailed() from None
    except OSError:
        _log("connection_failed", "socket failure talking to provider")
        raise ConnectionFailed() from None


# ---- Phase 8A routing layer ------------------------------------------

#: Failure classes for which a second, different model on the SAME
#: gateway can reasonably succeed: transient faults, capacity, or a
#: per-model problem. Authentication/config/redirect refusals are
#: gateway-level — a fallback attempt cannot help and is never made.
_RETRYABLE_FALLBACK_ERRORS = (
    ProviderTimeout, ConnectionFailed, RateLimited,
    RequestRejected, UpstreamError, MalformedResponse, EmptyResponse,
)


def complete_with_fallback(messages, *, base_url, api_key, primary_model,
                           fallback_model="", timeout_seconds=60,
                           max_tokens=512):
    """Bounded primary/fallback chat lifecycle (Phase 8A).

    Returns a dict contract for callers:
      {"reply": assistant text,            # str
       "model": <id actually used>,        # str (primary or fallback)
       "fallback_used": bool,
       "attempts": [{"model", "code"}]}    # failure codes, primary first

    Policy: at most ONE fallback attempt with fallback_model when the
    primary fails with a transient/per-model error; every attempt uses
    the same input history and timeout (no loops, no double side
    effects — callers keep persistence outside this function). When
    every attempt fails, the LAST taxonomy error is re-raised with an
    attached `attempts` attribute; the caller surfaces those safe codes.
    Non-retryable failures (auth/configuration/redirect) raise
    immediately with `attempts` attached.
    """
    models = list(
        dict.fromkeys(
            [primary_model]
            + ([fallback_model] if fallback_model else [])
        )
    )
    attempts = []
    last_error = None
    for idx, model in enumerate(models):
        try:
            reply = chat_completion(
                messages,
                base_url=base_url,
                api_key=api_key,
                model=model,
                timeout_seconds=timeout_seconds,
                max_tokens=max_tokens,
            )
        except _RETRYABLE_FALLBACK_ERRORS as exc:
            attempts.append({"model": model, "code": exc.code})
            last_error = exc
            continue
        except ProviderError as exc:
            attempts.append({"model": model, "code": exc.code})
            exc.attempts = attempts
            raise
        return {
            "reply": reply,
            "model": model,
            "fallback_used": idx > 0,
            "attempts": attempts,
        }
    last_error.attempts = attempts
    raise last_error


#: Tool-call readiness (Phase 8A): representation only. Proposals are
#: structured data; execution, name resolution and permission checks
#: belong to the orchestrator/tool-registry phases.
TOOL_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_-."
)
MAX_TOOL_NAME_CHARS = 64


def parse_tool_proposals(message):
    """Extract validated tool-call proposals from a model message.

    Accepts the OpenAI chat-completions message shape (Phase 8A,
    representation only). Returns a list of
    {"name": str, "arguments": dict} — safe, bounded identifiers and
    JSON-object arguments only. Anything structurally invalid raises
    MalformedResponse so malformed tool-call payloads can never reach
    future callers. Responses are never executed here.
    """
    tool_calls = message.get("tool_calls")
    if tool_calls is None:
        return []
    if not isinstance(tool_calls, list):
        _log("malformed_response", "tool_calls is not a list")
        raise MalformedResponse()
    proposals = []
    for call in tool_calls:
        if not isinstance(call, dict):
            _log("malformed_response", "tool call is not an object")
            raise MalformedResponse()
        function = call.get("function")
        if not isinstance(function, dict):
            _log("malformed_response", "tool call missing function object")
            raise MalformedResponse()
        name = function.get("name")
        if not isinstance(name, str) or not name \
                or len(name) > MAX_TOOL_NAME_CHARS \
                or any(ch not in TOOL_SAFE_ID_CHARS for ch in name):
            _log("malformed_response", "unsafe tool call name")
            raise MalformedResponse()
        raw_args = function.get("arguments")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) \
                else raw_args
        except ValueError:
            _log("malformed_response", "tool arguments are not valid JSON")
            raise MalformedResponse() from None
        if not isinstance(args, dict):
            _log("malformed_response", "tool arguments are not an object")
            raise MalformedResponse()
        proposals.append({"name": name, "arguments": args})
    return proposals
