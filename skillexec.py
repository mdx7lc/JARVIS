"""Isolated skill execution (Phase 14B).

A skill that declares an `entrypoint` (a single .py file inside its own
directory) can be *executed* — but only:

1. by an explicit operator HTTP action (a human POST, never model
   initiated), 2. only when the skill is BOTH enabled AND trusted,
3. never inside the JARVIS server process, 4. inside a bubblewrap
   sandbox when one is available: the skill tree binds read-only,
   a designated empty workspace binds read-write, the network is
   unshared, the environment is cleared (no JARVIS_* / credential
   variables ever exported), and every other path is either absent,
   tmpfs, or read-only system bindings needed to run the interpreter,
5. under a hard wall-clock timeout with clamped output capture,
6. recorded in skill_runs (bounded history) with the permission
   decision from the central engine.

Fail-closed rules: unknown entrypoint shapes (path separators, `..`,
symlink escape, oversized file, non-.py) are refused before the
sandbox; a missing bubblewrap binary refuses execution entirely
('sandbox_unavailable') — untrusted executable skills NEVER fall back
to running in-process. Running a skill proves nothing beyond its own
exit status; 'verified' means exit 0 inside the sandbox, nothing more.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import skills

_MAX_ENTRYPOINT_BYTES = 256 * 1024
_MAX_OUTPUT = 8096
_MAX_ARGS_JSON = 2048
_TIMEOUT_DEFAULT = 30
_TIMEOUT_MIN, _TIMEOUT_MAX = 1, 120
_SECRETS_RE = re.compile(r"key|token|password|secret|credential|auth",
                         re.I)
_INTERPRETER = "python3"  # only stdlib system interpreter is allowed

_ENTRY_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.py\Z")


class ExecError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def sandbox_available():
    return shutil.which("bwrap") is not None


def interpreter_path():
    return shutil.which(_INTERPRETER)


def _manifest_execution(fields):
    """Extract execution metadata from validated manifest fields."""
    entrypoint = fields.get("entrypoint") or ""
    timeout_raw = fields.get("timeout") or ""
    try:
        timeout = max(_TIMEOUT_MIN, min(_TIMEOUT_MAX,
                                        int(float(timeout_raw))))
    except ValueError:
        timeout = _TIMEOUT_DEFAULT
    return {"entrypoint": entrypoint, "timeout": timeout}


def validate_execution(record):
    """Validate the execution surface of a discovered/registry skill
    record. Returns the execution metadata dict or raises ExecError."""
    source = record.get("source") or ""
    fields = record.get("fields") or {}
    exec_meta = _manifest_execution(fields)
    entrypoint = exec_meta["entrypoint"]
    if not entrypoint:
        raise ExecError("no_entrypoint",
                        "skill manifest declares no entrypoint; this "
                        "skill is reference material, not executable")
    if not _ENTRY_NAME_RE.match(entrypoint) or "/" in entrypoint or \
            "\\" in entrypoint or ".." in entrypoint:
        raise ExecError("invalid_entrypoint",
                        "entrypoint must be a simple .py filename in "
                        "the skill directory")
    source_dir = Path(source)
    if not source_dir.is_dir() or source_dir.is_symlink():
        raise ExecError("skill_source_missing",
                        "skill directory is missing or a symlink")
    resolved_root = source_dir.resolve(strict=False)
    candidate = (source_dir / entrypoint).resolve(strict=False)
    # containment: the resolved entrypoint must stay inside the
    # resolved skill directory (symlink/traversal escape refused)
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        raise ExecError("entrypoint_escape",
                        "entrypoint resolves outside the skill "
                        "directory")
    if candidate.is_symlink():
        raise ExecError("entrypoint_escape",
                        "entrypoint is a symlink")
    try:
        stat = candidate.stat()
    except OSError:
        raise ExecError("entrypoint_missing",
                        "entrypoint file is missing")
    if not stat.st_size or stat.st_size > _MAX_ENTRYPOINT_BYTES:
        raise ExecError("entrypoint_invalid",
                        "entrypoint exceeds the 256 KiB bound")
    exec_meta["resolved_path"] = str(candidate)
    exec_meta["sandbox"] = sandbox_available()
    return exec_meta


def redact_args(args):
    """Operator-supplied args, with secret-named values redacted before
    any persistence or echo. Bounded to _MAX_ARGS_JSON."""
    if args is None:
        return {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            raise ExecError("invalid_args", "args must be a JSON object")
    if not isinstance(args, dict) or len(args) > 24:
        raise ExecError("invalid_args",
                        "args must be a JSON object (max 24 keys)")
    clean = {}
    for key, value in list(args.items())[:24]:
        if not isinstance(key, str) or len(key) > 64:
            raise ExecError("invalid_args", "invalid args key")
        if isinstance(value, str):
            value = value[:400]
            if _SECRETS_RE.search(key):
                value = "***redacted***"
        elif isinstance(value, bool) or value is None:
            pass
        elif isinstance(value, (int, float)):
            value = value
        else:
            raise ExecError("invalid_args",
                            "args values must be scalar")
        clean[key] = value
    encoded = json.dumps(clean, ensure_ascii=True)
    if len(encoded) > _MAX_ARGS_JSON:
        raise ExecError("invalid_args", "args payload too large")
    return clean


class _SkillTool:
    """Adapter so the central permission engine can authorize a skill
    run like any other tool invocation (audited, cap-checked). Skill
    execution is classed `approval_required`: the L3 gate is the
    operator's explicit record-verified POST action, and the engine
    refuses when the cap is below 3 or the operator disabled the skill
    globally."""

    def __init__(self, skill_id):
        self.name = "skill." + skill_id[:64]
        self.permission = "approval_required"


def run(registry_row, args, workspace, engine=None, env=None):
    """Execute one validated skill inside the sandbox.

    registry_row: dict with id/source/state/trust and manifest fields.
    args: validated operator args (dict). workspace: writable runtime
    directory (fresh per run). engine: PermissionEngine for the audited
    capability decision. Returns the completed run record dict.
    """
    # 1..7: trust/enabled/permission/manifest/sandbox gates BEFORE any
    # file is opened or process launched (fail-closed ordering).
    if (registry_row.get("state") or "") != "enabled":
        raise ExecError("skill_disabled",
                        "skill is not enabled by the operator")
    if (registry_row.get("trust") or "") != "trusted":
        raise ExecError("skill_untrusted",
                        "operator must mark the skill trusted first")
    decision = None
    if engine is not None:
        decision = engine.authorize(_SkillTool(registry_row['id']))
        if not decision["allowed"]:
            raise ExecError("permission_denied",
                            "permission engine denied the skill run: "
                            + str(decision["reason"]))
    exec_meta = validate_execution({"source": registry_row["source"],
                                    "fields": registry_row.get("fields")
                                    or {}})
    if not exec_meta["sandbox"]:
        raise ExecError("sandbox_unavailable",
                        "bubblewrap is not installed; executable "
                        "skills fail closed")
    interpreter = interpreter_path()
    if not interpreter:
        raise ExecError("interpreter_missing",
                        "system python3 interpreter not found")
    ws = Path(workspace)
    ws.mkdir(mode=0o700, exist_ok=True)
    sandboxed = [
        "bwrap",
        "--die-with-parent",
        "--unshare-net",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/workspace",
        "--setenv", "SKILL_ARGS",
        json.dumps(redact_args(args), ensure_ascii=True),
        "--ro-bind", str(Path(registry_row["source"]).resolve()),
        "/skill",
        "--bind", str(ws), "/workspace",
        "--dev-bind", "/dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind-try", "/bin", "/bin",
        interpreter, "/skill/" + exec_meta["entrypoint"],
    ]
    import time as _time
    started = _time.time()
    try:
        proc = subprocess.run(
            sandboxed, capture_output=True, timeout=exec_meta["timeout"],
            text=True, errors="replace")
        out = (proc.stdout or "")[:_MAX_OUTPUT]
        stderr = (proc.stderr or "")[:_MAX_OUTPUT]
        exit_code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") .decode("utf-8", "replace")[:_MAX_OUTPUT] \
            if isinstance(exc.stdout, bytes) else \
            (exc.stdout or "")[:_MAX_OUTPUT]
        stderr = "<timed out>"
        exit_code = -1
        timed_out = True
    duration_ms = int((_time.time() - started) * 1000)
    # 13: verification — honest and narrow: exit 0 inside the sandbox
    # with captured output; nothing stronger is claimed.
    if timed_out:
        status = "timeout"
        verification = ("process exceeded the sandbox timeout; no "
                        "result verified")
    elif exit_code == 0:
        status = "completed"
        verification = "verified: exit 0 within sandbox limits"
    else:
        status = "failed"
        verification = f"nonzero exit {exit_code}; nothing verified"
    return {
        "status": status,
        "exit_code": exit_code,
        "stdout": out,
        "stderr": stderr,
        "duration_ms": duration_ms,
        "timeout": exec_meta["timeout"],
        "verification": verification,
        "permission": decision,
        "args": redact_args(args),
    }
