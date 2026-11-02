"""Tool registry: read-only inventory (Phase 8B) and bounded write
capabilities (Phase 10A).

The registry is the permission boundary: only explicitly registered,
personally reviewed capabilities may run autonomously. Plans can only
reference registry names — the model cannot define or extend this.

Bounds and defenses are conservative on purpose. Read-only tools never
read file contents, never execute code, never touch the network. Write
tools (Phase 10A) are permission 'approval_required': execution happens
only after an external reviewer approved the plan AND a human approved
the exact per-step write proposal, then every write is executed
atomically (temp file + rename), backed up, and independently verified.
Writes never enter JARVIS's own source tree and never match the
sensitive-name patterns (credentials/keys/env).
"""

import errno
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

from vault import MAX_SEARCH_RESULTS


class ToolError(Exception):
    """Typed tool failure. `code` is a safe category for the API/report."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

    def to_payload(self):
        return {"code": self.code, "message": str(self)}


class ToolRegistry:
    """Server-owned capability list. Instances get only what is
    registered at construction; tests may register a fixture tool, the
    product build never gets write/execute tools."""

    def __init__(self, tools):
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name):
        return self._tools.get(name)

    def names(self):
        return sorted(self._tools)

    def all(self):
        return [self._tools[name] for name in self.names()]

    def descriptions(self):
        """Model-facing summary: names, descriptions, argument schemas.
        No implementation details, no paths, no secrets."""
        return {
            tool.name: {
                "description": tool.description,
                "schema": tool.schema,
                "permission": tool.permission,
            }
            for tool in self.all()
        }


class Tool:
    def __init__(self, name, description, schema, permission, idempotent,
                 run, verifier, records_check=False):
        self.name = name
        self.description = description
        self.schema = schema  # {"arg": {"type": "...", "required": bool,
        #                                  "maxlen": n?, "maxval": n?}}
        self.permission = permission  # "autonomous" | "verify_readonly"
        # | "approval_required"; unknown classes are refused everywhere.
        self.idempotent = idempotent
        self.records_check = records_check  # verification-driven repair
        self._run = run
        self._verifier = verifier

    def validate_args(self, args):
        """Strict argument validation against the declared schema.
        Returns a normalized dict or raises ToolError('invalid_tool_args')."""
        if not isinstance(args, dict):
            raise ToolError("invalid_tool_args",
                            "tool arguments must be an object")
        unknown = set(args) - set(self.schema)
        if unknown:
            raise ToolError(
                "invalid_tool_args",
                "unexpected tool arguments: " + ", ".join(sorted(unknown)),
            )
        normalized = {}
        for key, spec in self.schema.items():
            value = args.get(key)
            if spec.get("required") and value is not None:
                pass
            elif value is None or value == "":
                if spec.get("required"):
                    raise ToolError(
                        "invalid_tool_args", f"missing argument: {key}"
                    )
                if "default" in spec:
                    normalized[key] = spec["default"]
                continue
            if not self._check_type(value, spec):
                raise ToolError("invalid_tool_args",
                                f"bad type for argument: {key}")
            if isinstance(value, str) and len(value) > spec.get("maxlen", 256):
                raise ToolError("invalid_tool_args",
                                f"argument too long: {key}")
            if isinstance(value, int) and value > spec.get("max", 1 << 31):
                raise ToolError("invalid_tool_args",
                                f"argument too large: {key}")
            if isinstance(value, int) and value < spec.get("min", 0):
                raise ToolError("invalid_tool_args",
                                f"argument too small: {key}")
            normalized[key] = value
        return normalized

    @staticmethod
    def _check_type(value, spec):
        kind = spec.get("type", "string")
        if kind == "string":
            return isinstance(value, str)
        if kind == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        return False

    def run(self, args):
        return self._run(args)

    def verify(self, args, result):
        return self._verifier(args, result)


# ---- workspace.list_files ---------------------------------------------

# Sensitive-name exclusions (checked on every entry name, dir or file).
_EXCLUDED_NAMES = {
    ".git", ".gnupg", ".ssh", ".cache", ".pki",
}
_EXCLUDED_PREFIXES = (".env",)
_EXCLUDED_SUFFIXES = (".pem", ".key", ".keystore", ".kdbx", ".p12", ".ovpn", ".htpasswd", ".htpasswd-old", ".netrc", ".htaccess.bak")
_EXCLUDED_SUBSTRINGS = ("credential", "credentials", "secret", "password",
                        "token", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
                        "creds", "api_key", "api-key")

MAX_DEPTH = 8
MAX_ENTRIES = 1000
MAX_NAME_CHARS = 200
MAX_RESULT_BYTES = 64_000

LIST_TOOL_DESCRIPTION = (
    "List files and directories in the configured workspace root"
    " (bounded depth/count, relative paths, exclusions applied)."
)


def _is_excluded(name):
    lowered = name.lower()
    if lowered in _EXCLUDED_NAMES:
        return True
    if any(lowered.startswith(p) for p in _EXCLUDED_PREFIXES):
        return True
    if any(lowered.endswith(s) for s in _EXCLUDED_SUFFIXES):
        return True
    if any(s in lowered for s in _EXCLUDED_SUBSTRINGS):
        return True
    return False


def _as_tool_error(exc):
    if isinstance(exc, ToolError):
        return exc
    return ToolError("workspace_error", "workspace listing failed")


def _run_workspace_list(args, root_resolver):
    """Traverse the workspace root. Strictly read-only listing."""
    rel = args.get("path", "")
    root = root_resolver()
    base = root.resolve()
    target = (base / rel).resolve() if rel else base
    # Containment check BEFORE touching the entry (path traversal
    # defense). `Path.resolve()` resolves symlinks and `..`.
    if not _contains(base, target):
        raise ToolError("path_forbidden",
                        "requested path escapes the workspace root")
    if not target.exists():
        raise ToolError("no_such_path", "no such workspace path")
    if not target.is_dir():
        raise ToolError("not_a_directory",
                        "requested path is not a workspace directory")
    entries = []
    truncated = False
    stack = [(target, rel)]
    while stack:
        current, relpath = stack.pop()
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        try:
            names = sorted(os.listdir(current))
        except OSError:
            continue  # unreadable dir: skipped honestly (not fabricated)
        for name in names:
            rel_child = f"{relpath}/{name}" if relpath else name
            if _is_excluded(name) \
                    or len(name) > MAX_NAME_CHARS:
                continue
            child = current / name
            try:
                real_child = child.resolve()
            except OSError:
                continue
            if not _contains(base, real_child):
                # symlink escape: not listed and not followed
                continue
            if real_child.is_dir() and len(rel_child.split("/")) < MAX_DEPTH:
                stack.append((real_child, rel_child))
                entries.append({"path": rel_child, "type": "dir"})
            elif real_child.is_file():
                entries.append({"path": rel_child, "type": "file"})
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
        if truncated:
            break
    result = _cap(entries, truncated)
    return result


def _cap(entries, truncated):
    """Serialize-bounded result with honest truncation flags."""
    body = json.dumps(entries, ensure_ascii=True).encode("utf-8")
    if len(body) > MAX_RESULT_BYTES:
        while len(entries) > 1:
            entries = entries[: int(len(entries) * 0.8)]
            body = json.dumps(entries, ensure_ascii=True).encode("utf-8")
            if len(body) <= MAX_RESULT_BYTES:
                break
        truncated = True
    return {
        "tool": "workspace.list_files",
        "entries": entries,
        "count": len(entries),
        "truncated": truncated,
        "excluded_names": sorted(_EXCLUDED_NAMES)
        + [p + "*" for p in _EXCLUDED_PREFIXES]
        + [s + "+" for s in _EXCLUDED_SUFFIXES],
    }


def _contains(base, candidate):
    """True when candidate is base or inside base (post-resolution)."""
    base_str = str(base)
    cand_str = str(candidate)
    return cand_str == base_str or cand_str.startswith(
        base_str.rstrip(os.sep) + os.sep
    )


def _verify_workspace_list(args, result):
    """Independent postcondition: what the tool claimed is enforced
    against its own output BEFORE anyone can pass for truth."""
    if not isinstance(result, dict) \
            or result.get("tool") != "workspace.list_files" \
            or not isinstance(result.get("entries"), list) \
            or not isinstance(result.get("truncated"), bool) \
            or result.get("count") != len(result.get("entries") or []):
        return {"passed": False, "detail": "result shape invalid"}
    rel = args.get("path", "")
    for entry in result["entries"]:
        if not isinstance(entry, dict):
            return {"passed": False, "detail": "entry shape invalid"}
        path = entry.get("path", "")
        if not path or path.startswith("/") \
                or ".." in path.split("/") \
                or "\\" in path:
            return {"passed": False,
                    "detail": "entry path not a relative workspace path"}
        if rel and not path.startswith(rel.rstrip("/") + "/"):
            return {"passed": False,
                    "detail": "entry outside the requested path"}
        name = path.rsplit("/", 1)[-1]
        if _is_excluded(name):
            return {"passed": False,
                    "detail": "excluded path reached the result"}
        if entry.get("type") not in ("dir", "file"):
            return {"passed": False, "detail": "entry type unknown"}
    if len(result["entries"]) == MAX_ENTRIES and not result["truncated"]:
        return {"passed": False, "detail": "full result not flagged truncated"}
    return {"passed": True, "detail": (
        f"{result['count']} entries"
        + (" (truncated)" if result["truncated"] else ""))}


def make_workspace_tool(root_resolver):
    """Build the read-only inventory tool against a configured root
    resolver. Root resolution happens per-call so tests can swap dirs."""
    return Tool(
        name="workspace.list_files",
        description=LIST_TOOL_DESCRIPTION,
        schema={
            "path": {
                "type": "string", "required": False,
                "default": "", "maxlen": 200,
            },
        },
        permission="autonomous",
        idempotent=True,
        run=lambda args: _run_workspace_list(args, root_resolver),
        verifier=_verify_workspace_list,
    )


def default_registry(workspace_root, vault=None):
    """The product registry: read-only capabilities, verification
    recipes, the bounded approval-gated writes (Phases 8B/10A/10B),
    and the read-only Obsidian vault tools (Phase 13A). The vault
    service is optional in tests; its tools raise the honest
    vault_not_configured error when no vault is configured."""
    from verification import (check_javascript_syntax, check_python_syntax,
                              inspect_git_diff, run_project_tests)
    ws = Path(workspace_root)
    registry_tools = [
        make_workspace_tool(lambda: Path(workspace_root)),
        make_read_file_tool(lambda: Path(workspace_root)),
        make_create_file_tool(lambda: Path(workspace_root)),
        make_edit_file_tool(lambda: Path(workspace_root)),
        make_recipe_tool(lambda args: check_python_syntax(
            ws, args.get("path")), "check_python_syntax",
            "Parse a workspace .py file with ast.parse (no"
            " code executes; deterministic).",
            permission="verify_readonly",
            required=True),
        make_recipe_tool(lambda args: check_javascript_syntax(
            ws, args.get("path")), "check_javascript_syntax",
            "Parse a workspace .js file with `node --check`"
            " (no code executes).",
            permission="verify_readonly",
            required=True),
        make_recipe_tool(lambda args: run_project_tests(
            ws, args.get("path")), "run_project_tests",
            "Run the workspace unittest module target via"
            " `python3 -m unittest -v <module>`. This"
            " EXECUTES project-controlled code and requires"
            " explicit human approval of the exact binding.",
            permission="approval_required",
            required=True),
        make_recipe_tool(lambda args: inspect_git_diff(
            ws, args.get("path")), "inspect_git_diff",
            "Bounded `git diff` of the workspace (diff"
            " drivers disabled; informational).",
            permission="verify_readonly",
            required=False),
    ]
    if vault is not None:
        registry_tools.extend(make_vault_tools(vault))
    return ToolRegistry(registry_tools)


# ---- vault.* (Phase 13A: read-only Obsidian knowledge tools) ---------

VAULT_LIST_DESCRIPTION = (
    "List eligible Markdown notes and folders in the user-configured"
    " Obsidian vault (bounded count/depth, vault-relative paths,"
    " exclusions applied; strictly read-only)."
)
VAULT_READ_DESCRIPTION = (
    "Read ONE Markdown note from the user-configured Obsidian vault by"
    " its vault-relative path, with a content hash for citation"
    " (bounded size; strictly read-only; no other filesystem access)."
)
VAULT_SEARCH_DESCRIPTION = (
    "Search the indexed Obsidian vault notes (title + content) and"
    " return matching notes with bounded excerpts, relative paths and"
    " content hashes for citation. Read-only; query-driven."
)


def _vault_guard(fn, *args):
    """Run one vault service call, mapping typed vault errors onto the
    safe ToolError taxonomy. Unexpected errors never leak detail."""
    try:
        return fn(*args)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code:
            raise ToolError(code, str(exc)) from None
        raise ToolError("vault_error", "vault operation failed") from None


def make_vault_tools(service):
    """Build the three read-only vault tools against one VaultService.
    The service resolves the configured root per call; model arguments
    can never change it — only vault-relative note paths are accepted,
    and every path is containment-checked there."""

    def run_list(args):
        result = _vault_guard(service.list_notes, args.get("path", ""))
        result["tool"] = "vault.list_files"
        return result

    def run_read(args):
        result = _vault_guard(service.read_note, args.get("path"))
        result["tool"] = "vault.read_file"
        return result

    def run_search(args):
        result = _vault_guard(service.search, args.get("query"),
                              args.get("limit", 10))
        result["tool"] = "vault.search"
        return result

    def verify_list(args, result):
        if not isinstance(result, dict) \
                or result.get("tool") != "vault.list_files" \
                or not isinstance(result.get("entries"), list) \
                or not isinstance(result.get("truncated"), bool) \
                or result.get("count") != len(result["entries"]):
            return {"passed": False, "detail": "list result shape invalid"}
        for entry in result["entries"]:
            path = entry.get("path", "")
            if not isinstance(path, str) or not path \
                    or path.startswith("/") or ".." in path.split("/") \
                    or "\\" in path:
                return {"passed": False,
                        "detail": "entry path not a vault-relative path"}
            if entry.get("type") not in ("dir", "note"):
                return {"passed": False, "detail": "entry type unknown"}
        if result["count"] > 500 or (len(result["entries"]) >= 500
                                     and not result["truncated"]):
            return {"passed": False,
                    "detail": "result bound not honored honestly"}
        return {"passed": True,
                "detail": f"{result['count']} entries"
                + (" (truncated)" if result["truncated"] else "")}

    def verify_read(args, result):
        if not isinstance(result, dict) \
                or result.get("tool") != "vault.read_file" \
                or result.get("status", "ok") != "ok" \
                or not isinstance(result.get("path"), str):
            return {"passed": False, "detail": "read result shape invalid"}
        try:
            current = service.read_note(result["path"])
        except Exception as exc:
            code = getattr(exc, "code", "")
            if code == "binary_content" and result.get("truncated"):
                return {"passed": True, "detail": "truncated read held"}
            return {"passed": False, "detail": "note unreadable on verify"}
        if current["sha256"] != result.get("sha256"):
            return {"passed": False,
                    "detail": "note hash differs from the read claim"}
        if current["total_bytes"] != result.get("total_bytes"):
            return {"passed": False,
                    "detail": "note size differs from the read claim"}
        if bool(current["truncated"]) != bool(result.get("truncated")):
            return {"passed": False, "detail": "truncation inconsistent"}
        return {"passed": True,
                "detail": f"{result['total_bytes']} bytes at"
                f" {result['path']}"
                + (" (truncated)" if result["truncated"] else "")}

    def verify_search(args, result):
        if not isinstance(result, dict) \
                or result.get("tool") != "vault.search" \
                or not isinstance(result.get("results"), list) \
                or result.get("count") != len(result["results"]):
            return {"passed": False,
                    "detail": "search result shape invalid"}
        limit_arg = args.get("limit", 10)
        limit = max(1, min(limit_arg, MAX_SEARCH_RESULTS)) \
            if isinstance(limit_arg, int) and not isinstance(limit_arg, bool) \
            else 10
        if len(result["results"]) > limit:
            return {"passed": False, "detail": "result limit not honored"}
        for hit in result["results"]:
            path = hit.get("path", "")
            if not isinstance(path, str) or not path \
                    or path.startswith("/") or ".." in path.split("/") \
                    or "\\" in path:
                return {"passed": False,
                        "detail": "hit path not a vault-relative path"}
            snippet = hit.get("snippet", "")
            if not isinstance(snippet, str) or len(snippet) > 260:
                return {"passed": False, "detail": "snippet over the bound"}
        return {"passed": True,
                "detail": f"{result['count']} hits for query"}

    return [
        Tool(
            name="vault.list_files",
            description=VAULT_LIST_DESCRIPTION,
            schema={
                "path": {
                    "type": "string", "required": False,
                    "default": "", "maxlen": 200,
                },
            },
            permission="autonomous",
            idempotent=True,
            run=run_list,
            verifier=verify_list,
        ),
        Tool(
            name="vault.read_file",
            description=VAULT_READ_DESCRIPTION,
            schema={
                "path": {
                    "type": "string", "required": True, "maxlen": 200,
                },
            },
            permission="autonomous",
            idempotent=True,
            run=run_read,
            verifier=verify_read,
        ),
        Tool(
            name="vault.search",
            description=VAULT_SEARCH_DESCRIPTION,
            schema={
                "query": {
                    "type": "string", "required": True, "maxlen": 200,
                },
                "limit": {
                    "type": "integer", "required": False,
                    "default": 10, "max": 20,
                },
            },
            permission="autonomous",
            idempotent=True,
            run=run_search,
            verifier=verify_search,
        ),
    ]


def make_recipe_tool(run_impl, recipe_key, description, permission,
                     required):
    """Wrap one verification recipe as a registry tool. `records_check`
    marks it for the worker's verification-driven repair policy."""
    return Tool(
        name="workspace." + recipe_key,
        description=description,
        schema={"path": {
            "type": "string", "required": required, "maxlen": 200,
        }},
        permission=permission,
        idempotent=recipe_key != "run_project_tests",
        run=run_impl,
        verifier=_make_recipe_verifier(recipe_key),
        records_check=recipe_key != "inspect_git_diff",
    )


def _make_recipe_verifier(recipe_key):
    from verification import recipe_semantics

    def verify(args, result):
        verdict = _verify_recipe_shape(recipe_key, result)
        if verdict is not None:
            return verdict
        semantics = recipe_semantics(recipe_key, result)
        return {"passed": semantics["passed"],
                "detail": semantics["detail"]}

    return verify


def _verify_recipe_shape(recipe_key, result):
    """Independent postcondition: the stored recipe record must be a
    self-consistent honest record (status/exit classification, bounded
    streams). No re-execution happens here."""
    if not isinstance(result, dict) \
            or result.get("tool") != "workspace." + recipe_key \
            or result.get("recipe") != recipe_key \
            or result.get("status") not in (
                "passed", "failed", "unavailable", "timed_out") \
            or not isinstance(result.get("stdout"), str) \
            or not isinstance(result.get("stderr"), str):
        return {"passed": False, "detail": "recipe result shape invalid"}
    status = result["status"]
    exit_code = result.get("exit_code")
    if not (exit_code is None or isinstance(exit_code, int)):
        return {"passed": False, "detail": "recipe exit_code invalid"}
    if status == "passed" and exit_code != 0:
        return {"passed": False,
                "detail": "status passed but exit_code nonzero"}
    if status == "failed" and exit_code in (0, None):
        return {"passed": False,
                "detail": "status failed but exit_code missing"}
    if status in ("unavailable", "timed_out") and exit_code is not None:
        return {"passed": False,
                "detail": "no-exit states recorded an exit code"}
    duration = result.get("duration_seconds")
    if not isinstance(duration, (int, float)) or duration < 0:
        return {"passed": False, "detail": "recipe duration invalid"}
    if len(result["stdout"]) > 6000 or len(result["stderr"]) > 6000:
        return {"passed": False, "detail": "recipe output over the bound"}
    return None


# ---- write tools (Phase 10A) -------------------------------------------

_WRITES_CONTENT_MAX_CHARS = 20_000  # escaped JSON stays under args cap
_WRITES_RESULT_BYTES_MAX = 60_000
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_WORKSPACE_META_DIR = ".jarvis"
_SELF_ROOT = Path(__file__).resolve().parent


def _open_dir_fd(path):
    """Open a directory descriptor without following symlinks in the
    final component (Linux O_NOFOLLOW). Raises ToolError on refusal."""
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | _READ_FLAGS)
    except OSError as exc:
        raise _dirfd_error(exc)


def _dirfd_error(exc):
    code = "path_forbidden"
    err = getattr(exc, "errno", None)
    if isinstance(err, int):
        if err == errno.ENOENT:
            return ToolError("no_such_path", "no such workspace path")
        if err == errno.ENOTDIR:
            return ToolError("not_a_directory",
                             "path component is not a directory")
        if err == errno.ELOOP:
            return ToolError("path_forbidden",
                             "symlinked path components are refused")
        if err == errno.EEXIST:
            return ToolError("already_exists",
                             "refusing to replace an existing path")
    return ToolError(code, "path refused by the write guard")


def _split_rel(rel):
    parts = [p for p in rel.split("/") if p not in ("",)]
    if any(p in (".", "..") or "\\" in p or "\x00" in p for p in parts):
        raise ToolError("path_forbidden",
                        "path escapes the workspace or is malformed")
    return parts


def _guard_write_path(root, rel):
    """All pre-open path guards: form, sensitive patterns, forbidden
    roots. Returns the list of components (never touches the fs)."""
    parts = _split_rel(rel)
    if not parts:
        raise ToolError("path_forbidden", "empty target path")
    for comp in parts:
        if len(comp) > MAX_NAME_CHARS or _is_excluded(comp):
            raise ToolError(
                "path_forbidden",
                "target matches an excluded sensitive name pattern")
    if parts[0] == _WORKSPACE_META_DIR:
        raise ToolError("path_forbidden",
                        "target is inside the workspace meta directory")
    resolved_root = root.resolve()
    # Defense in depth: never write inside JARVIS's own source tree,
    # even if the workspace root is misconfigured to point there.
    if _contains(_SELF_ROOT, resolved_root):
        raise ToolError("self_write_forbidden",
                        "workspace root is JARVIS's own source tree")
    return parts


def _walk_parents(base, parts):
    """Open every parent dir of `parts` under base with dirfd chaining
    (O_NOFOLLOW on each component). Returns the final parent's fd."""
    fd = _open_dir_fd(base)
    try:
        for comp in parts[:-1]:
            fdesc = os.open(comp, os.O_RDONLY | os.O_DIRECTORY
                            | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = fdesc
    except OSError as exc:
        os.close(fd)
        raise _dirfd_error(exc)
    return fd


def _sha_hex(data):
    return hashlib.sha256(data).hexdigest()


def _workspace_backup_dir(root):
    return root / ".jarvis" / "backup"


def _backup_bytes(root, data, target_name):
    """Persist the pre-change bytes under <ws>/.jarvis/backup/. Backup
    failure aborts the write (never lose the old state silently)."""
    backup_dir = _workspace_backup_dir(root)
    backup_dir.mkdir(parents=True, exist_ok=True)
    unique = f"{uuid.uuid4().hex[:12]}_{target_name}"[:MAX_NAME_CHARS + 32]
    target = backup_dir / unique
    with open(target, "xb") as fh:
        fh.write(data)
    return target.name


def _read_file_bytes(dir_fd, name):
    """Read current bytes via dir_fd, refusing symlinks. Returns None
    when the file does not exist (create semantics rely on that)."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _dirfd_error(exc)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > _WRITES_RESULT_BYTES_MAX:
                raise ToolError("too_large",
                                "existing file exceeds the write size bound")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_bytes_oneshot(dir_fd, name, data, o_flags, mode=0o600):
    fd = os.open(name, o_flags, mode, dir_fd=dir_fd)
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_result(tool, rel_path, sha, size, backup=None, before=None):
    payload = {
        "tool": tool,
        "path": rel_path,
        "sha256": sha,
        "bytes": size,
    }
    if backup is not None:
        payload["backup"] = backup
    if before is not None:
        payload["before_sha256"] = before
    return payload


def _run_create_file(args, root_resolver):
    root = Path(root_resolver())
    rel = str(args.get("path") or "").strip()
    parts = _guard_write_path(root, rel)
    data = str(args.get("content") or "").encode("utf-8")
    if len(data) > _WRITES_RESULT_BYTES_MAX:
        raise ToolError("too_large",
                        "content exceeds the write size bound")
    parent_fd = _walk_parents(root, parts)
    try:
        name = parts[-1]
        if _read_file_bytes(parent_fd, name) is not None:
            raise ToolError("already_exists",
                            "refusing to replace an existing path")
        _write_bytes_oneshot(
            parent_fd, name, data, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except OSError as exc:
        raise _dirfd_error(exc)
    finally:
        os.close(parent_fd)
    return _write_result("workspace.create_file", rel, _sha_hex(data),
                         len(data))


def _run_edit_file(args, root_resolver):
    root = Path(root_resolver())
    rel = str(args.get("path") or "").strip()
    parts = _guard_write_path(root, rel)
    new_text = str(args.get("new_text") or "")
    expected_before = str(args.get("expected_before_sha256") or "")
    new_data = new_text.encode("utf-8")
    if len(new_data) > _WRITES_RESULT_BYTES_MAX:
        raise ToolError("too_large",
                        "content exceeds the write size bound")
    parent_fd = _walk_parents(root, parts)
    tmp_name = None
    try:
        name = parts[-1]
        current = _read_file_bytes(parent_fd, name)
        if current is None:
            raise ToolError("no_such_path", "no such file to edit")
        before_sha = _sha_hex(current)
        if before_sha != expected_before:
            raise ToolError(
                "before_mismatch",
                "current file differs from the approved before-state")
        backup_name = _backup_bytes(root, current, name)
        tmp_name = f".{name}.jarvis-tmp-{uuid.uuid4().hex[:12]}"
        _write_bytes_oneshot(parent_fd, tmp_name, new_data,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        os.rename(tmp_name, name, src_dir_fd=parent_fd,
                  dst_dir_fd=parent_fd)
        tmp_name = None
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
        return _write_result("workspace.edit_file", rel,
                             _sha_hex(new_data), len(new_data),
                             backup=backup_name, before=before_sha)
    except ToolError:
        raise
    except OSError as exc:
        raise _dirfd_error(exc)
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def _verify_common(root, args, result, tool):
    """Shared independent postcondition for both write tools: result
    shape is valid, rel path is contained, and the CURRENT file bytes
    hash exactly to the proposed content digest. Runs away from run()."""
    if not isinstance(result, dict) or result.get("tool") != tool \
            or not isinstance(result.get("path"), str) \
            or not isinstance(result.get("sha256"), str) \
            or len(result["sha256"]) != 64 \
            or not isinstance(result.get("bytes"), int):
        return {"passed": False, "detail": "write result shape invalid"}
    rel = result["path"]
    try:
        _split_rel(rel)
        resolved = (root / rel).resolve()
        base = root.resolve()
        if not _contains(base, resolved):
            return {"passed": False, "detail": "result path escapes root"}
    except ToolError as exc:
        return {"passed": False, "detail": str(exc)}
    content = args.get("content") if tool == "workspace.create_file" \
        else args.get("new_text")
    prop_sha = _sha_hex(str(content or "").encode("utf-8"))
    if prop_sha != result.get("sha256"):
        return {"passed": False,
                "detail": "result digest differs from the proposed content"}
    try:
        current = (root / rel).read_bytes()
    except OSError:
        return {"passed": False, "detail": "written file is unreadable"}
    if len(current) != result.get("bytes"):
        return {"passed": False,
                "detail": "file size differs from the write result"}
    if _sha_hex(current) != result["sha256"]:
        return {"passed": False,
                "detail": "file content hash differs from the claim"}
    return {"passed": True,
            "detail": f"{result.get('bytes')} bytes at {rel}"
            + (f" (backup {result['backup']})"
               if result.get("backup") else "")}


def _make_write_tool(name, description, schema, run_impl,
                     root_resolver):
    return Tool(
        name=name,
        description=description,
        schema=schema,
        permission="approval_required",
        idempotent=False,
        run=run_impl,
        verifier=lambda args, result: _verify_common(
            root_resolver(), args, result, name),
    )


def _arg_schema(extra):
    base = {
        "path": {
            "type": "string", "required": True, "maxlen": 200,
        },
    }
    base.update(extra)
    return base


CREATE_FILE_TOOL_DESCRIPTION = (
    "Create a new text file inside the workspace (atomic, bounded"
    " size). Refuses if the path already exists. Requires explicit"
    " human approval of the exact content before execution."
)
EDIT_FILE_TOOL_DESCRIPTION = (
    "Replace the full text of an existing workspace file atomically;"
    " keeps a backup of the prior content. Executed only when the file"
    " still matches the sha256 the human approved."
)


def make_create_file_tool(root_resolver):
    return _make_write_tool(
        "workspace.create_file", CREATE_FILE_TOOL_DESCRIPTION,
        _arg_schema({
            "content": {
                "type": "string", "required": True,
                "maxlen": _WRITES_CONTENT_MAX_CHARS,
            },
        }),
        lambda args: _run_create_file(args, root_resolver),
        root_resolver)


def make_edit_file_tool(root_resolver):
    return _make_write_tool(
        "workspace.edit_file", EDIT_FILE_TOOL_DESCRIPTION,
        _arg_schema({
            "new_text": {
                "type": "string", "required": True,
                "maxlen": _WRITES_CONTENT_MAX_CHARS,
            },
            "expected_before_sha256": {
                "type": "string", "required": True, "maxlen": 64,
            },
        }),
        lambda args: _run_edit_file(args, root_resolver),
        root_resolver)


# ---- workspace.read_file (Phase 10B) ------------------------------------

READ_FILE_TOOL_DESCRIPTION = (
    "Read one workspace text file (bounded: slices are capped, sha256 of"
    " the full file is returned; sensitive paths are refused)."
    " Read-only — never modifies the file."
)

#: Largest file the read tool will decode at all. Bigger files get a
#: structured size-limit result instead of truncated misleading text.
_READ_FILE_MAX_BYTES = 500_000

#: Largest number of characters returned in `content`.
_READ_FILE_MAX_CHARS = 20_000
_READ_RESULT_MAX_BYTES = 64_000


def _read_fd_stat(fd):
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise ToolError("not_a_regular_file",
                        "target is not a regular file")
    return st


def _run_read_file(args, root_resolver):
    root = Path(root_resolver())
    rel = str(args.get("path") or "").strip()
    parts = _guard_read_path(root, rel)
    parent_fd = _walk_parents(root, parts)
    try:
        fd = os.open(parts[-1], _READ_FLAGS, dir_fd=parent_fd)
        try:
            st = _read_fd_stat(fd)
            if st.st_size > _READ_FILE_MAX_BYTES:
                return {
                    "tool": "workspace.read_file",
                    "path": rel,
                    "status": "too_large",
                    "total_bytes": st.st_size,
                    "max_bytes": _READ_FILE_MAX_BYTES,
                    "content": "",
                }
            chunks = []
            view = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
                view += len(chunk)
                if view > _READ_FILE_MAX_BYTES:
                    return {
                        "tool": "workspace.read_file",
                        "path": rel,
                        "status": "too_large",
                        "total_bytes": view,
                        "max_bytes": _READ_FILE_MAX_BYTES,
                        "content": "",
                    }
            data = b"".join(chunks)
        finally:
            os.close(fd)
    except OSError as exc:
        raise _dirfd_error(exc)
    finally:
        os.close(parent_fd)
    try:
        text = data.decode("utf-8")  # strict: binary is never faked
    except UnicodeDecodeError:
        raise ToolError("binary_content",
                        "file is not valid UTF-8 text")
    truncated = len(text) > _READ_FILE_MAX_CHARS
    content = text[:_READ_FILE_MAX_CHARS]
    return {
        "tool": "workspace.read_file",
        "path": rel,
        "status": "ok",
        "content": content,
        "total_bytes": len(data),
        "content_bytes": len(content.encode("utf-8")),
        "max_chars": _READ_FILE_MAX_CHARS,
        "sha256": _sha_hex(data),
        "truncated": truncated,
    }


def _guard_read_path(root, rel):
    """Read variant of the write-path guard: same refusals, no
    filesystem mutation, no self-root writes (self-reading the operator's
    own JARVIS source via a misconfigured root is refused too)."""
    parts = _split_rel(rel)
    if not parts:
        raise ToolError("path_forbidden", "empty target path")
    for comp in parts:
        if len(comp) > MAX_NAME_CHARS or _is_excluded(comp):
            raise ToolError(
                "path_forbidden",
                "target matches an excluded sensitive name pattern")
    if parts[0] == _WORKSPACE_META_DIR:
        raise ToolError("path_forbidden",
                        "target is inside the workspace meta directory")
    resolved_root = root.resolve()
    if _contains(_SELF_ROOT, resolved_root):
        raise ToolError("self_root_forbidden",
                        "workspace root is JARVIS's own source tree")
    return parts


def _verify_read_file(root, args, result):
    """Independent postcondition: re-open and hash the CURRENT bytes;
    the result's sha/size/truncation must be faithful."""
    if not isinstance(result, dict) \
            or result.get("tool") != "workspace.read_file" \
            or result.get("status") not in ("ok", "too_large") \
            or not isinstance(result.get("path"), str):
        return {"passed": False,
                "detail": "read result shape invalid"}
    rel = result["path"]
    try:
        _split_rel(rel)
        resolved = (root / rel).resolve()
        base = root.resolve()
        if not _contains(base, resolved):
            return {"passed": False, "detail": "result path escapes root"}
        if result["status"] == "too_large":
            current_size = os.stat(resolved).st_size
            if current_size <= _READ_FILE_MAX_BYTES:
                return {"passed": False,
                        "detail": "size_limit reported but file is small"}
            return {"passed": True,
                    "detail": f"size limit reported at {current_size} bytes"}
        current = (root / rel).read_bytes()
    except ToolError as exc:
        return {"passed": False, "detail": str(exc)}
    except OSError:
        return {"passed": False, "detail": "read target unreadable"}
    if result["status"] == "too_large":
        return {"passed": False, "detail": "size limit state inconsistent"}
    try:
        current.decode("utf-8")
    except UnicodeDecodeError:
        return {"passed": False,
                "detail": "claimed a text result but file is binary"}
    if _sha_hex(current) != result.get("sha256"):
        return {"passed": False,
                "detail": "file hash differs from the read claim"}
    if len(current) != result.get("total_bytes"):
        return {"passed": False,
                "detail": "file size differs from the read claim"}
    expected_truncated = len(current.decode("utf-8")) > _READ_FILE_MAX_CHARS
    if bool(result.get("truncated")) != expected_truncated:
        return {"passed": False,
                "detail": "truncation flag inconsistent"}
    return {"passed": True,
            "detail": f"{result.get('total_bytes')} bytes at {rel}"
            + (" (truncated)" if result.get("truncated") else "")}


def make_read_file_tool(root_resolver):
    return Tool(
        name="workspace.read_file",
        description=READ_FILE_TOOL_DESCRIPTION,
        schema={
            "path": {
                "type": "string", "required": True, "maxlen": 200,
            },
        },
        permission="autonomous",
        idempotent=True,
        run=lambda args: _run_read_file(args, root_resolver),
        verifier=lambda args, result: _verify_read_file(
            root_resolver(), args, result),
    )
