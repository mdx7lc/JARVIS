"""Central permission engine: the authority for every tool invocation.

Four levels (spec Section 3):
  L1 observer            — read/inspect; always auto-authorized
  L2 operator            — low-risk reversible ops; auto-authorized
  L3 authorized_executor — consequential; digest-bound human approval
  L4 restricted          — never runs, never grantable through settings

The engine maps the existing per-tool permission classes onto levels
instead of duplicating the registry:
  verify_readonly      -> L1
  autonomous           -> L2
  approval_required    -> L3 (the existing digest gate in worker.py is
                          unchanged and remains the L3 execution path)
  restricted / unknown -> L4

Operator policy (persisted, UI-only — the model can never write it):
  - capability cap (1..3) in `meta`: the highest level JARVIS may use;
    a cap below a tool's required level denies that tool.
  - per-tool disable row in `permission_policies`: revokes one tool.
L4 is outside every policy write. Fail behavior is fail-closed: any
engine/db failure denies the tool run ('engine_unavailable').
"""

import sqlite3
import threading

L1, L2, L3, L4 = 1, 2, 3, 4

LEVEL_NAMES = {
    L1: "observer",
    L2: "operator",
    L3: "authorized_executor",
    L4: "restricted",
}

LEVEL_INFO = [
    (L1, "observer",
     "Read authorized project files, docs, source, configuration "
     "without secrets, indexed Obsidian notes, logs with secrets "
     "redacted, system status. Always auto-authorized."),
    (L2, "operator",
     "Low-risk reversible operations: organize tasks, update indexes "
     "and caches, write reports to designated output rules, safe "
     "housekeeping. Auto-authorized; these tools declare exactly this "
     "scope."),
    (L3, "authorized_executor",
     "Consequential operations (write source files, run test recipes, "
     "installed write tools). Each invocation requires an exact "
     "digest-bound single-use human approval."),
    (L4, "restricted",
     "Unrestricted shell, root/privilege escalation, credential "
     "extraction, security-mechanism changes, self-bypass of this "
     "engine. Never runs and is never grantable."),
]

_CLASS_TO_LEVEL = {
    "verify_readonly": L1,
    "autonomous": L2,
    "approval_required": L3,
    "restricted": L4,
}

_MAX_TOOL_NAME = 80

# Bounded audit retention: keep the newest AUDIT_KEEP rows so the table
# cannot grow without limit.
AUDIT_KEEP = 5000

_CAP_LOCK = threading.Lock()


class PolicyWriteError(Exception):
    """Invalid operator policy write (never a runtime denial)."""
    pass


def level_for_tool(tool):
    """Declared level of a registered tool. L4 is also the answer for
    any unknown/legacy class — fail-closed by construction."""
    return _CLASS_TO_LEVEL.get(getattr(tool, "permission", None), L4)


class PermissionEngine:
    """db_path-backed authority. One cached connection per thread:
    decisions happen per tool run, so repeated connection churn would
    be the dominant cost otherwise."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._local = threading.local()

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        self._local.conn = conn
        return conn

    def cap(self):
        """1..3, or None when the cap cannot be trusted (fail-closed)."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='permission_cap'"
            ).fetchone()
            if row is None:
                return L3  # never configured: declared policy unchanged
            value = int(row["value"])
            return value if L1 <= value <= L3 else None
        except Exception:
            return None

    def authorize(self, tool, *, goal_id=None):
        """The one decision point. Returns
        {"allowed": bool, "level": int, "reason": str|None} and audits
        every call."""
        conn = None
        try:
            name = getattr(tool, "name", None)
            if not isinstance(name, str) or not name or \
                    len(name) > _MAX_TOOL_NAME:
                return self._deny(None, L4, "unregistered_tool", goal_id)
            declared = level_for_tool(tool)
            if declared >= L4:
                return self._deny(name, declared, "restricted_class",
                                  goal_id)
            conn = self._conn()
            row = conn.execute(
                "SELECT state FROM permission_policies WHERE tool_name"
                " = ?", (name,)).fetchone()
            if row is not None and row["state"] == "disabled":
                return self._deny(name, declared,
                                  "tool_disabled_by_operator", goal_id)
            cap = self.cap()
            if cap is None:
                return self._deny(name, declared, "engine_unavailable",
                                  goal_id)
            if declared > cap:
                return self._deny(name, declared,
                                  "capability_cap_exceeded", goal_id)
            return self._allow(name, declared, goal_id)
        except Exception:
            return self._deny(getattr(tool, "name", None), L4,
                              "engine_unavailable", goal_id)

    def _allow(self, name, level, goal_id):
        self._audit(name, level, "allowed", None, goal_id)
        return {"allowed": True, "level": level, "reason": None}

    def _deny(self, name, level, reason, goal_id):
        # name may be None for tainted input; a safe label keeps the
        # audit row honest without echoing untrusted text.
        self._audit(name or "unregistered", level, "denied", reason,
                    goal_id)
        return {"allowed": False, "level": level, "reason": reason}

    def _audit(self, name, level, decision, reason, goal_id):
        try:
            conn = self._conn()
            conn.execute(
                "INSERT INTO permission_audit(ts, source, goal_id,"
                " tool_name, level, decision, reason)"
                " VALUES (datetime('now'), 'engine', ?, ?, ?, ?, ?)",
                (goal_id, name[:_MAX_TOOL_NAME], int(level),
                 decision, reason))
            conn.execute(
                "DELETE FROM permission_audit WHERE id IN ("
                "  SELECT id FROM permission_audit"
                "  ORDER BY id DESC LIMIT -1 OFFSET ?)",
                (AUDIT_KEEP,))
        except Exception:
            # An audit-write failure must not fail the decision: the
            # invocation stays gated; the event log keeps evidence.
            pass

    # -- operator policy writes (UI only; never callable by the model) --

    def set_cap(self, level):
        if isinstance(level, bool) or not isinstance(level, int) or \
                not (L1 <= level <= L3):
            raise PolicyWriteError("cap_out_of_range")
        with _CAP_LOCK:
            conn = self._conn()
            with conn:
                conn.execute(
                    "INSERT INTO meta(key, value)"
                    " VALUES ('permission_cap', ?) ON CONFLICT(key)"
                    " DO UPDATE SET value=excluded.value",
                    (str(level),))
                conn.execute(
                    "INSERT INTO permission_audit(ts, source,"
                    " tool_name, level, decision, reason)"
                    " VALUES (datetime('now'), 'operator_cap',"
                    " 'capability_cap', ?, 'policy_changed', ?)",
                    (level, f"cap={level}"))
        return True

    def set_tool_state(self, tool_name, state, declared_level=None):
        if state not in ("inherit", "disabled"):
            raise PolicyWriteError("invalid_policy_state")
        if not isinstance(tool_name, str) or not tool_name or \
                len(tool_name) > _MAX_TOOL_NAME:
            raise PolicyWriteError("invalid_tool_name")
        with _CAP_LOCK:
            conn = self._conn()
            with conn:
                if state == "inherit":
                    conn.execute(
                        "DELETE FROM permission_policies WHERE"
                        " tool_name = ?", (tool_name,))
                else:
                    conn.execute(
                        "INSERT INTO permission_policies(tool_name,"
                        " level, state, reason, updated_at) VALUES"
                        " (?, ?, 'disabled', ?, datetime('now'))"
                        " ON CONFLICT(tool_name) DO UPDATE SET"
                        " state='disabled', level=excluded.level,"
                        " reason=excluded.reason,"
                        " updated_at=excluded.updated_at",
                        (tool_name, declared_level or L2,
                         "tool disabled by operator"))
                conn.execute(
                    "INSERT INTO permission_audit(ts, source,"
                    " tool_name, level, decision, reason)"
                    " VALUES (datetime('now'), 'operator_policy',"
                    " ?, ?, 'policy_changed', ?)",
                    (tool_name, declared_level or L2,
                     f"policy={state}"))
        return True
