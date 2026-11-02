#!/usr/bin/env python3
"""Operator tool: verify the intended GLM/DeepSeek model pair (Phase 9A).

Two strictly separated modes:

  Catalog verification (default)
      Fetches the authenticated model catalog through the existing
      provider adapter and maps the requested display names to exact
      model ids. No inference, ever.

  Live inference verification (--live-inference)
      Operator-invoked only. Sends ONE harmless, bounded prompt per
      configured model — individually, never through the fallback
      layer, so a primary failure cannot trigger a second request.

Security rules enforced here:
  - The API key is read from FREELLMAPI_API_KEY once and never printed,
    never embedded in error output, never echoed in exceptions.
  - Full provider responses are never printed: output is limited to
    pass/fail, reply length, the exact-keyword check, and the gateway's
    own `model` echo (metadata — may be a routing alias; it is never
    presented as proof of a concrete upstream model).
  - No retry loops: each requested model gets exactly one request.
  - Exit status is nonzero whenever any requested model fails
    verification (catalog ambiguity counts as failure in both modes).

Requires the environment of the normal server (FREELLMAPI_URL,
FREELLMAPI_API_KEY); a real user database is never touched.
"""

import argparse
import difflib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.fspath(Path(__file__).resolve().parent.parent))
import provider  # noqa: E402

PRIMARY_NAME = "GLM 5.3 Flash"
FALLBACK_NAME = "DeepSeek 4.1 Flash"
SMOKE_MESSAGES = [{"role": "user", "content": "Respond with exactly the word OK"}]
SMOKE_MAX_TOKENS = 16
DEFAULT_SMOKE_TIMEOUT = 45
DISPLAY_NAME_MAX = 200


class Ambiguous(Exception):
    def __init__(self, message, candidates):
        super().__init__(message)
        self.message = message
        self.candidates = candidates


def _catalog():
    base = os.environ.get("FREELLMAPI_URL", provider.DEFAULT_API_BASE).strip()
    key = (os.environ.get("FREELLMAPI_API_KEY") or "").strip()
    if not key:
        print("FREELLMAPI_API_KEY is not set — cannot reach an "
              "authenticated catalog.", file=sys.stderr)
        raise SystemExit(2)
    try:
        base = provider.normalize_base(base)
        return provider.list_models(base_url=base, api_key=key)
    except provider.ProviderError as exc:
        print(f"catalog fetch failed: {exc.code} — {exc.user_message}",
              file=sys.stderr)
        raise SystemExit(2) from None


def _resolve(catalog, display_name):
    """Map a display name to exactly one catalog entry — no guessing.

    Only exact (case-insensitive) display-name equality resolves an id.
    Zero or several matches raise Ambiguous, listing near matches for
    the operator's eyes only (never auto-selected).
    """
    matches = [
        m for m in catalog
        if isinstance(m.get("display_name"), str)
        and m["display_name"].strip().lower() == display_name.strip().lower()
    ]
    if len(matches) == 1 and matches[0]["available"]:
        return matches[0]
    names = [m.get("display_name") for m in catalog
             if isinstance(m.get("display_name"), str)]
    near = difflib.get_close_matches(
        display_name, names, n=5, cutoff=0.4)
    if not matches:
        reason = "no catalog entry carries this exact display name"
    elif not any(m["available"] for m in matches):
        reason = (f"{len(matches)} catalog entries carry this display "
                  "name but all are marked unavailable")
    else:
        reason = f"{len(matches)} available entries share this display name"
    raise Ambiguous(reason, near)


def _row(m):
    out = {"id": m["id"], "available": m["available"],
           "display_name": m.get("display_name")}
    if m.get("unavailable_reason"):
        out["unavailable_reason"] = m["unavailable_reason"]
    return out


def catalog_mode(args):
    catalog = _catalog()
    print(f"catalog entries: {len(catalog)}")
    report = {"mode": "catalog", "entries": len(catalog), "models": {}}
    failed = False
    for label, name, explicit in (
        ("primary", args.primary_name, args.primary_id),
        ("fallback", args.fallback_name, args.fallback_id),
    ):
        if explicit:
            row = next((m for m in catalog if m["id"] == explicit), None)
            if row is None:
                print(f"{label}: id {explicit!r} is NOT in the catalog")
                report["models"][label] = {"confirmed": False, "id": explicit}
                failed = True
            else:
                print(f"{label}: id {explicit!r} confirmed in catalog "
                      f"(available={row['available']}) — {_row(row)}")
                ok = row["available"]
                report["models"][label] = {"confirmed": ok, **_row(row)}
                failed = failed or not ok
                if not ok:
                    print(f"  caveat: catalog marks it "
                          f"unavailable ({row.get('unavailable_reason')})")
            continue
        try:
            match = _resolve(catalog, name)
            print(f"{label}: {name!r} -> {_row(match)}")
            report["models"][label] = {"confirmed": True, **_row(match)}
        except Ambiguous as exc:
            print(f"{label}: {name!r} NOT resolved — {exc.message}")
            if exc.candidates:
                print(f"  near matches (review only, not selected): "
                      f"{exc.candidates}")
            report["models"][label] = {
                "confirmed": False, "requested": name,
                "near_matches": exc.candidates, "reason": exc.message,
            }
            failed = True
    print(json.dumps(report, indent=2))
    if failed:
        print("catalog verification: INCOMPLETE — "
              "resolve the items above before any live smoke.",
              file=sys.stderr)
        return 2
    print("catalog verification: OK (no inference was performed)")
    return 0


def _live_one(label, model_id, timeout):
    base = provider.normalize_base(
        os.environ.get("FREELLMAPI_URL", provider.DEFAULT_API_BASE).strip())
    key = (os.environ.get("FREELLMAPI_API_KEY") or "").strip()
    print(f"\n[{label}] one bounded inference request on model "
          f"{model_id!r} (timeout {timeout}s, max_tokens "
          f"{SMOKE_MAX_TOKENS}, no retries, no fallback layer)")
    try:
        reply, reported = provider.chat_completion(
            SMOKE_MESSAGES,
            base_url=base,
            api_key=key,
            model=model_id,
            timeout_seconds=timeout,
            max_tokens=SMOKE_MAX_TOKENS,
            include_model=True,
        )
    except provider.ProviderError as exc:
        print(f"  FAIL: {exc.code} — {exc.user_message}")
        return {"model": model_id, "ok": False, "code": exc.code}
    exact = reply.strip().upper() in ("OK", '"OK"', "'OK'")
    print(f"  PASS: non-empty reply ({len(reply)} chars, "
          f"strict keyword check {'matched' if exact else 'did not match'})")
    if reported:
        print(f"  gateway-reported model echo: {reported!r} "
              "(metadata only — aliases are common; never proof of the "
              "concrete upstream model)")
    else:
        print("  gateway did not report a model identifier in the response")
    return {"model": model_id, "ok": True, "reply_chars": len(reply),
            "exact_match": exact,
            "reported_model": reported or None}


def live_mode(args):
    catalog = _catalog()
    resolved = {}
    failed_pre = False
    for label, name, explicit in (
        ("primary", args.primary_name, args.primary_id),
        ("fallback", args.fallback_name, args.fallback_id),
    ):
        if explicit:
            resolved[label] = explicit
        else:
            try:
                resolved[label] = _resolve(catalog, name)["id"]
            except Ambiguous as exc:
                print(f"{label}: {name!r} NOT resolved — {exc.message}",
                      file=sys.stderr)
                if exc.candidates:
                    print(f"  near matches (review only): {exc.candidates}",
                          file=sys.stderr)
                failed_pre = True
        if resolved.get(label) and not explicit:
            row = next(m for m in catalog if m["id"] == resolved[label])
            if not row["available"]:
                print(f"{label}: {resolved[label]!r} is marked unavailable "
                      f"({row.get('unavailable_reason')}) — refusing.",
                      file=sys.stderr)
                failed_pre = True
    if failed_pre:
        print("\nlive smoke aborted: resolve catalog ambiguities first "
              "(run without --live-inference, or pass explicit "
              "--primary-id/--fallback-id after operator confirmation).",
              file=sys.stderr)
        return 2

    print(f"models to verify by live inference: "
          f"primary={resolved['primary']!r}, "
          f"fallback={resolved['fallback']!r} "
          f"(one request each; fallback policy itself is NOT exercised)")
    results = {
        "primary": _live_one("primary", resolved["primary"],
                             timeout=args.timeout),
        "fallback": _live_one("fallback", resolved["fallback"],
                              timeout=args.timeout),
    }
    report = {
        "mode": "live-inference",
        "models_to_test": resolved,
        "results": results,
        "claims": {
            "primary": "inference-tested, non-empty reply"
            if results["primary"]["ok"] else "FAILED",
            "fallback": "inference-tested, non-empty reply"
            if results["fallback"]["ok"] else "FAILED",
        },
    }
    print(json.dumps(report, indent=2))
    if all(r["ok"] for r in results.values()):
        print("live smoke: OK — both models returned a bounded reply. "
              "The pair may be configured as primary/fallback per Step 4.")
        return 0
    print("live smoke: FAILED for at least one model — keep the existing "
          "configuration.", file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser(
        description="Verify the intended GLM/DeepSeek model pair. "
                    "Default: catalog only. Inference requires "
                    "explicit --live-inference.")
    parser.add_argument("--primary-name", default=PRIMARY_NAME,
                        help=f"display name to resolve (default: "
                             f"{PRIMARY_NAME!r})")
    parser.add_argument("--fallback-name", default=FALLBACK_NAME,
                        help=f"display name to resolve (default: "
                             f"{FALLBACK_NAME!r})")
    parser.add_argument("--primary-id", default=None,
                        help="operator-confirmed primary model id "
                             "(bypasses display-name resolution; must "
                             "still exist in the catalog)")
    parser.add_argument("--fallback-id", default=None,
                        help="operator-confirmed fallback model id")
    parser.add_argument("--live-inference", action="store_true",
                        help="EXPLICIT ONLY: send one bounded prompt per "
                             "model. Without this flag no inference ever "
                             "happens.")
    parser.add_argument("--timeout", type=int,
                        default=DEFAULT_SMOKE_TIMEOUT,
                        help=f"per-request timeout in live mode "
                             f"(default {DEFAULT_SMOKE_TIMEOUT}s)")
    args = parser.parse_args()
    if args.live_inference:
        raise SystemExit(live_mode(args))
    if args.primary_id or args.fallback_id:
        print("--primary-id/--fallback-id are checked against the "
              "catalog; add --live-inference to also request inference.",
              file=sys.stderr)
    raise SystemExit(catalog_mode(args))


if __name__ == "__main__":
    main()
