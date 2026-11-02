"""Digest-bound write authorization (Phase 10A).

A write proposal is authorized against an exact digest over: the goal,
the step number, the tool, the target path, the operation kind, the
exact before-state hash (edit), the exact content hash, and the plan
snapshot hash. The digest is computed the same way at proposal time and
at approval time — if anything persisted has drifted, the recomputed
digest stops matching and the approval is refused. Model output and the
reviewer can influence `args`, but nothing else decides authorization:
the human must send the digest for the exact bytes they previewed.
"""

import hashlib
import hmac
import json


def canonical_bytes(goal_id, seq, tool_name, path, kind, before_sha256,
                    content_sha256, plan_sha256):
    payload = {
        "goal_id": goal_id,
        "seq": int(seq),
        "tool": tool_name,
        "path": path,
        "kind": kind,
        "before_sha256": before_sha256 or "",
        "content_sha256": content_sha256,
        "plan_sha256": plan_sha256 or "",
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode("utf-8")


def digest_for(goal_id, seq, tool_name, args, plan_sha256):
    """The one digest definition. `args` are the normalized step
    arguments; a restart re-reads them from the persisted step row and
    must reproduce the exact same digest.

    Phase 10B adds the 'execute' kind for `workspace.run_project_tests`:
    the binding covers the runner, the exact workspace-relative target
    and the plan snapshot (the recipe executes the workspace's CURRENT
    code by design — that is what running tests means; the runner,
    timeout, arguments and environment policy are fixed server side and
    cannot be influenced by the model)."""
    kind = {
        "workspace.create_file": "create",
        "workspace.edit_file": "edit",
        "workspace.run_project_tests": "execute",
    }.get(tool_name, "edit")
    if kind == "create":
        content = str(args.get("content") or "")
        before = ""
    elif kind == "execute":
        binding = {"runner": "python-unittest", "path":
                   str(args.get("path") or "")}
        content = json.dumps(binding, sort_keys=True)
        before = ""
    else:
        content = str(args.get("new_text") or "")
        before = str(args.get("expected_before_sha256") or "")
    content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    blob = canonical_bytes(goal_id, seq, tool_name, str(args.get("path")),
                           kind, before, content_sha, plan_sha256)
    return {
        "kind": kind,
        "path": str(args.get("path")),
        "before_sha256": before or None,
        "content_sha256": content_sha,
        "bytes": len(content.encode("utf-8")),
        "digest": hashlib.sha256(blob).hexdigest(),
    }


def digests_equal(requested, stored):
    """Constant-time comparison; both are opaque strings."""
    if not isinstance(requested, str) or not isinstance(stored, str):
        return False
    return hmac.compare_digest(requested.encode("utf-8"),
                               stored.encode("utf-8"))
