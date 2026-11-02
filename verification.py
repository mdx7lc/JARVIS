"""Controlled verification recipes (Phase 10B).

NOT a generic shell tool: there is no `shell.execute`. The model can
only name one of the explicit recipes below; every other part of each
command (executable, argument template, working directory, timeout,
environment policy, output bounds) is fixed here in this module.

Security boundary (recorded honestly in the docs):
- `cwd` inside the workspace does NOT sandbox a subprocess. Recipes run
  with this process's OS permissions; the environment is reduced
  (credentials never reach the child) and output is bounded, but
  executing project-controlled code must therefore require the same
  digest-bound human approval as a write (see run_project_tests).
- check_python_syntax / check_javascript_syntax only PARSE a target
  file (ast.parse / node --check); nothing project-controlled executes.
  ast-parse reads the file through an O_NOFOLLOW fd handed to the child
  (no replacement race); `node --check` re-guards the target before the
  call (its residual pre-call swap window is documented).
- inspect_git_diff runs `git diff` with --no-textconv / --no-ext-diff
  (blocks repo-config diff drivers), a config-free env
  (GIT_CONFIG_GLOBAL/SYSTEM pointed at /dev/null), and filtered
  diagnostics: diff sections naming excluded sensitive paths are
  dropped before anything is stored.
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

import tools as tools_mod
from tools import ToolError

DEFAULT_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 6_000           # per stream, post-collection
_RAW_CAPTURE_BYTES = 64_000

_PY_CHECK_SRC = (
    "import ast, sys\n"
    "src = sys.stdin.buffer.read()\n"
    "ast.parse(src)\n"
    "print('syntax ok')\n"
)

_CONCURRENCY = threading.BoundedSemaphore(1)

RECIPES = {
    "check_python_syntax": {
        "exec": "python3",
        "executes_project_code": False,
    },
    "check_javascript_syntax": {
        "exec": "node",
        "executes_project_code": False,
    },
    "run_project_tests": {
        "exec": "python3",
        "executes_project_code": True,
        "authorization": "digest-bound human approval required",
    },
    "inspect_git_diff": {
        "exec": "git",
        "executes_project_code": False,
    },
}


def _tool_name(recipe_key):
    return "workspace." + recipe_key


def _child_env(recipe_key, workspace_root):
    """Minimal, secret-free child environment: nothing inherited except
    PATH. Bytecode writing and git config hijacks are disabled."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(workspace_root),
    }
    if recipe_key == "inspect_git_diff":
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_SYSTEM"] = "/dev/null"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _clamp(text):
    if not isinstance(text, str):
        text = str(text or "")
    truncated = len(text) > MAX_OUTPUT_CHARS
    return text[-MAX_OUTPUT_CHARS:] if truncated else text, truncated


_EMPTY_DIAG_PREFIXES = ("diff ",)


def _drop_sensitive_sections(text):
    """inspect_git_diff extra filter: drop diff sections whose file
    header names an excluded sensitive path."""
    kept, skip, changed = [], False, False
    for line in (text or "").splitlines(keepends=True):
        if any(line.startswith(p) for p in _EMPTY_DIAG_PREFIXES):
            name = ""
            for sep in (" b/", " a/"):
                if sep in line:
                    name = line.split(sep, 1)[-1]
                    break
            name = name.split("\t")[0].strip()
            old_skip, skip = skip, bool(name) and tools_mod._is_excluded(
                os.path.basename(name))
            changed = changed or skip != old_skip
        if not skip:
            kept.append(line)
    return "".join(kept), changed


def _guard_recipe_target(root, rel, require_file=True):
    """Subprocess-target guard: the same path/sensitive/edge rules the
    file tools use; confirms a real regular file through dirfd +
    O_NOFOLLOW. Working dir is always the workspace root; the root being
    JARVIS's own source tree is refused (self_root_forbidden).
    `rel == ""` is only legal for workspace-level recipes (git diff)."""
    if not str(rel or "").strip():
        if require_file:
            raise ToolError("path_forbidden", "empty target path")
        resolved_root = root.resolve()
        if tools_mod._contains(tools_mod._SELF_ROOT, resolved_root):
            raise ToolError("self_root_forbidden",
                            "workspace root is JARVIS's own source tree")
        return ""
    parts = tools_mod._guard_read_path(root, rel)
    parent_fd = tools_mod._walk_parents(root, parts)
    try:
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW,
                         dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise ToolError("no_such_path",
                            "verification target does not exist"
                            ) from exc
        except OSError as exc:
            raise tools_mod._dirfd_error(exc) from exc
        try:
            if require_file and not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ToolError("not_a_regular_file",
                                "verification target is not a file")
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    return rel


def _empty_result(recipe_key, target, status, detail):
    return {
        "tool": _tool_name(recipe_key),
        "recipe": recipe_key,
        "exec": RECIPES[recipe_key]["exec"],
        "target": target,
        "cwd": "workspace-root",
        "status": status,
        "exit_code": None,
        "duration_seconds": 0.0,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "stdout": "",
        "stderr": detail,
        "truncated": False,
    }


def _reap_and_collect(proc, recipe_key, target, started):
    """Wait bounded, kill on timeout, spool bounded output, classify."""
    out_f, err_f = proc.stdout, proc.stderr
    timed_out = False
    try:
        proc.wait(timeout=DEFAULT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover (rare)
            proc.terminate()
            proc.wait(timeout=5)
    duration = round(time.monotonic() - started, 3)
    out_text = out_f.read(_RAW_CAPTURE_BYTES).decode("utf-8", "replace")
    err_text = err_f.read(_RAW_CAPTURE_BYTES).decode("utf-8", "replace")
    stdout, stdout_trunc = _clamp(out_text)
    stderr, stderr_trunc = _clamp(err_text)
    truncated = stdout_trunc or stderr_trunc
    if recipe_key == "inspect_git_diff":
        stdout, changed_out = _drop_sensitive_sections(stdout)
        stderr, changed_err = _drop_sensitive_sections(stderr)
        truncated = truncated or changed_out or changed_err
    exit_code = None if timed_out else proc.returncode
    status = ("timed_out" if timed_out else
              "passed" if exit_code == 0 else "failed")
    return {
        "tool": _tool_name(recipe_key),
        "recipe": recipe_key,
        "exec": RECIPES[recipe_key]["exec"],
        "target": target,
        "cwd": "workspace-root",
        "status": status,
        "exit_code": exit_code,
        "duration_seconds": duration,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
    }


def _run_locked(parts, executable_name, recipe_key, target, root, *,
                stdin_fd=None, pass_fds=(), env=None):
    """Serialize recipe runs (at most one child process per process),
    spawn without any shell, and reap bounded."""
    if not _CONCURRENCY.acquire(blocking=False):
        return _empty_result(recipe_key, target, "unavailable",
                             "another verification recipe is still "
                             "running; try again after it finishes")
    try:
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                parts, shell=False, cwd=str(root),
                env=env if env is not None
                else _child_env(recipe_key, root),
                stdin=stdin_fd if stdin_fd is not None
                else subprocess.DEVNULL,
                pass_fds=pass_fds,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            return _empty_result(
                recipe_key, target, "unavailable",
                _clamp(str(exc).split(":")[0])[0])
        return _reap_and_collect(proc, recipe_key, target, started)
    finally:
        _CONCURRENCY.release()


def check_python_syntax(root, rel):
    """Parse the target with ast.parse — nothing executes. The parsed
    bytes come through an O_NOFOLLOW fd handed to the child, so a
    replacement race cannot redirect them."""
    target = _guard_recipe_target(root, rel)
    parts = rel.split("/")
    parent_fd = tools_mod._walk_parents(root, parts)
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW,
                     dir_fd=parent_fd)
        try:
            return _run_locked(
                [sys.executable, "-c", _PY_CHECK_SRC], "python3",
                "check_python_syntax", target, root,
                stdin_fd=fd, pass_fds=(fd,))
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def check_javascript_syntax(root, rel):
    """node --check — parse-only. The target is re-guarded (dirfd +
    O_NOFOLLOW) immediately before the spawn; the residual pre-spawn
    swap window is a documented limitation."""
    node = shutil.which("node")
    if node is None:
        return _empty_result("check_javascript_syntax", rel,
                             "unavailable",
                             "the node executable was not found")
    target = _guard_recipe_target(root, rel)
    return _run_locked([node, "--check", target], "node",
                       "check_javascript_syntax", target, root)


def project_tests_command(rel):
    """The exact, recipe-fixed command for a unittest target; the model
    only supplies the guarded module path."""
    if not rel.endswith(".py"):
        raise ToolError("invalid_tool_args",
                        "test target must be a workspace-relative .py "
                        "module path (for the unittest recipe)")
    module = rel[:-3].replace("/", ".")
    if not module.isidentifier() \
            and not all(part.isidentifier() for part in module.split(".")):
        raise ToolError("invalid_tool_args",
                        "test target is not a valid python module path")
    return [sys.executable, "-m", "unittest", "-v", module]


def run_project_tests(root, rel):
    """EXECUTES project-controlled code (python -m unittest module).
    Permission 'approval_required': the digest-bound human approval of
    the exact runner+target binding is mandatory — the reviewer's plan
    approval alone never authorizes this."""
    target = _guard_recipe_target(root, rel)
    return _run_locked(project_tests_command(target), "python3",
                       "run_project_tests", target, root)


def inspect_git_diff(root, rel=""):
    """Bounded `git diff` of the workspace with repo-config diff
    drivers disabled and a config-free environment; missing git is an
    honest unavailable result."""
    target = _guard_recipe_target(root, rel, require_file=False)
    if target:
        raise ToolError(
            "path_forbidden",
            "inspect_git_diff reports the whole workspace; a file-"
            "scoped recipe target is not supported")
    git = shutil.which("git")
    if git is None:
        return _empty_result("inspect_git_diff", "git diff", "unavailable",
                             "the git executable was not found")
    return _run_locked(
        [git, "--no-pager", "diff", "--no-textconv", "--no-ext-diff"],
        "git", "inspect_git_diff", "git diff", root)


def recipe_semantics(recipe_key, result):
    """Verification verdict for a completed recipe record. Returns
    {"passed": bool, "detail": str}:
    - 'passed' semantics only when the honest result says so.
    - failed exit = a verification failure (repairable evidence).
    - unavailable / timed_out = honest unverified stop (not silent).
    - inspect_git_diff is informational: any completed (exit 0) run
      passes; its content is evidence for the human."""
    status = result.get("status")
    if status == "passed" and result.get("exit_code") == 0:
        return {"passed": True,
                "detail": f"{recipe_key} passed (exit 0)"}
    if status == "failed":
        return {"passed": False,
                "detail": f"{recipe_key} failed (exit "
                          f"{result.get('exit_code')}); evidence"
                          " recorded"}
    if status in ("unavailable", "timed_out"):
        return {"passed": False,
                "detail": f"{recipe_key} {status}: {result.get('stderr','')}"}
    return {"passed": False, "detail": "recipe result unknown"}
