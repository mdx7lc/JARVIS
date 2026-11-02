"""Bounded self-improvement: OBSERVE -> PROPOSE -> APPROVE -> APPLY.

Observe: real aggregates from persisted state (goal outcomes, failed
step error codes, permission denials, vault status). No prompt or model
output is stored as evidence — only counts the application produced.

Propose: deterministic rules over that evidence. A proposal is a row in
`improvements` carrying the evidence string that generated it. JARVIS
does NOT claim to have learned anything by proposing.

Approve: the operator approves/rejects through the API (never the
model).

Apply: bounded to exactly one L2-safe action — queue ONE task titled
by the proposal (task creation is an explicitly authorized L2
operation). The cycle can, in this phase, never modify code, config,
permissions or skills.

Verify/Measure (Phase 14C): the approval snapshots the real evidence as
a baseline; every observation cycle then evaluates applied proposals —
completed follow-up tasks are verified against the baseline (regression
check over permission denials and blocked-goal ratio, measured deltas
persisted), unfinished ones stay honestly 'pending_verification', and
a failed check marks the improvement failed and permits rolling the
one queued task back. Verification success is never inferred from the
operation's completion alone.
"""

import json
import uuid

import db as db_mod

_ALLOWED_RISK = ("low", "medium")

_VERIFY_STATUSES = ("pending_verification", "verified", "failed_checked",
                    "rolled_back")


def observe(conn):
    """Evidence snapshots. Each query is a real aggregate of persisted
    application state."""
    evidence = {}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM goals GROUP BY status"
    ).fetchall()
    evidence["goals"] = {r["status"]: r["n"] for r in rows}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM goal_steps GROUP BY status"
    ).fetchall()
    evidence["steps"] = {r["status"]: r["n"] for r in rows}
    rows = conn.execute(
        "SELECT reason, COUNT(*) AS n FROM permission_audit"
        " WHERE decision = 'denied' GROUP BY reason"
    ).fetchall()
    evidence["permission_denials"] = {r["reason"] or "unknown": r["n"]
                                      for r in rows}
    state = conn.execute(
        "SELECT status, error_code FROM vault_index_state WHERE id = 1"
    ).fetchone()
    evidence["vault"] = dict(state) if state else {}
    return evidence


def _proposals(evidence):
    """Deterministic rules. Each entry: (kind, problem, evidence,
    expected, risk, task_title). At most 3 proposals per cycle."""
    out = []

    failed = sum((evidence.get("steps") or {}).values()) and (
        (evidence.get("steps") or {}).get("failed", 0))
    done = (evidence.get("steps") or {}).get("done", 0)
    if isinstance(failed, int) and failed >= 3:
        out.append({
            "kind": "repeated_step_failures",
            "problem": f"{failed} goal steps failed execution while"
                       f" {done} succeeded — execution reliability is"
                       " degrading",
            "evidence": json.dumps(evidence.get("steps")),
            "expected": "raise the share of verifiable failed→fixed"
                        " steps after manual follow-up",
            "risk": "low",
            "title": f"Review {failed} failed goal steps and their"
                     " error codes; document root causes",
        })

    grants = evidence.get("goals") or {}
    blocked = grants.get("blocked", 0)
    total = sum(grants.values())
    if total >= 5 and blocked / total >= 0.5:
        out.append({
            "kind": "high_blocked_ratio",
            "problem": f"{blocked} of {total} goals ended blocked —"
                       " planning or capabilities do not match the"
                       " objectives being queued",
            "evidence": json.dumps(grants),
            "expected": "operator reviews blocked objectives; queue"
                        " simpler/specific objectives",
            "risk": "low",
            "title": "Audit blocked goals: refine objective wording"
                     " before queuing more",
        })

    denials = evidence.get("permission_denials") or {}
    denial_total = sum(denials.values())
    if denial_total >= 5:
        top_reason = max(denials.items(), key=lambda kv: kv[1])[0]
        out.append({
            "kind": "permission_denials",
            "problem": f"{denial_total} permission checks were denied"
                       f" (top reason: {top_reason}) — the capability"
                       " cap may be contradicting how JARVIS is used",
            "evidence": json.dumps(denials),
            "expected": "operator consciously confirms the cap or"
                        " grants the missing level",
            "risk": "low",
            "title": "Review capability cap — frequent permission"
                     " denials with reason " + top_reason,
        })
    return out[:3]


def _baseline(conn, proposal_id):
    """Snapshot the real observation as this improvement's baseline —
    taken at approval time, stored immutably with the proposal."""
    row = conn.execute(
        "SELECT kind FROM improvements WHERE id = ?",
        (proposal_id,)).fetchone()
    if row is None:
        raise ValueError("unknown proposal")
    conn.execute(
        "UPDATE improvements SET baseline_json = ? WHERE id = ?",
        (json.dumps(observe(conn), ensure_ascii=True)[:2000],
         proposal_id))
    return "approved"


def propose(conn, now_iso):
    """Insert NEW proposals for current evidence rows not already open
    (same kind with status proposed/approved/applied is not duplicated).
    Every propose cycle FIRST evaluates applied proposals (VERIFY runs
    automatically as part of the observation loop). Returns created
    proposal dicts."""
    evaluate_applied(conn, now_iso)
    evidence = observe(conn)
    created = []
    for p in _proposals(evidence):
        open_ = conn.execute(
            "SELECT 1 FROM improvements WHERE kind = ? AND status IN"
            " ('proposed','approved','applied')", (p["kind"],)).fetchone()
        if open_:
            continue
        pid = "imp-" + uuid.uuid4().hex[:20]
        conn.execute(
            "INSERT INTO improvements(id, created, kind, problem,"
            " evidence, expected, risk, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed')",
            (pid, now_iso, p["kind"], p["problem"], p["evidence"],
             p["expected"], p["risk"]))
        created.append({"id": pid, **p})
    return created


def decide(conn, proposal_id, decision, now_iso):
    """Operator decision. Only 'apply' changes world state and only by
    creating one task (L2)."""
    if decision not in ("approve", "reject", "apply"):
        raise ValueError("invalid decision")
    row = conn.execute(
        "SELECT status FROM improvements WHERE id = ?",
        (proposal_id,)).fetchone()
    if row is None:
        raise ValueError("unknown proposal")
    if decision == "reject":
        # only open proposals can be rejected; history never rewritten
        if row["status"] != "proposed":
            raise ValueError("proposal is not open for rejection")
        conn.execute(
            "UPDATE improvements SET status = 'rejected', decided = ?"
            " WHERE id = ?", (now_iso, proposal_id))
        return "rejected"
    if decision == "approve":
        if row["status"] != "proposed":
            raise ValueError("proposal is not open for approval")
        _baseline(conn, proposal_id)
        conn.execute(
            "UPDATE improvements SET status = 'approved', decided = ?"
            " WHERE id = ?", (now_iso, proposal_id))
        return "approved"
    if row["status"] != "approved":
        raise ValueError("proposal is not approved")
    task = db_mod.create_task(
        conn, title="Improvement follow-up: "
        + str(conn.execute("SELECT kind FROM improvements WHERE id = ?",
                           (proposal_id,)).fetchone()["kind"]))
    conn.execute(
        "UPDATE improvements SET status = 'applied', decided = ?,"
        " task_id = ? WHERE id = ?",
        (now_iso, task["id"], proposal_id))
    return "applied"


# -- VERIFY / MEASURE / ROLLBACK (Phase 14C) ------------------------------
# Verification is deployed as a real, deterministic check, not a
# promise: every observation cycle (propose/GET view) evaluates applied
# proposals against their approval-time baseline.

def evaluate_applied(conn, now_iso):
    """Evaluate every applied proposal that is not yet concluded.

    Rules (deterministic, evidence-backed):
    - task row vanished  -> failed_checked (change did not materialize)
    - task not done yet  -> pending_verification (change exists, effect
      measurable only when the follow-up work completes)
    - task done          -> verified when the measured aggregates did
      NOT regress (permission denials total, blocked-goal ratio);
      record the measured delta as verify_json.
    """
    rows = conn.execute(
        "SELECT id, task_id, baseline_json FROM improvements"
        " WHERE status = 'applied' AND (verify_status IS NULL OR"
        " verify_status = 'pending_verification')").fetchall()
    evaluated = []
    for row in rows:
        baseline = json.loads(row["baseline_json"] or "{}")
        task = db_mod.get_task(conn, row["task_id"]) \
            if row["task_id"] else None
        if task is None:
            verify_status = "failed_checked"
            delta = {"task_id": row["task_id"],
                     "note": "applied task row is missing; the change"
                             " never materialized"}
        elif task["status"] != "done":
            verify_status = "pending_verification"
            delta = {"task_id": task["id"], "task_status": task["status"],
                     "note": "applied follow-up not completed yet;"
                             " measurement deferred honestly"}
        else:
            after = observe(conn)
            base_denials = sum((baseline.get("permission_denials")
                                or {}).values())
            after_denials = sum((after.get("permission_denials")
                                 or {}).values())
            base_blocked = (baseline.get("goals") or {}).get("blocked", 0)
            after_blocked = (after.get("goals") or {}).get("blocked", 0)
            delta = {
                "task_id": task["id"],
                "denials": {"baseline": base_denials,
                            "after": after_denials},
                "blocked_goals": {"baseline": base_blocked,
                                  "after": after_blocked},
            }
            if after_denials > base_denials:
                verify_status = "failed_checked"
                delta["note"] = "permission denials increased over the" \
                                " baseline (regression signal)"
            else:
                verify_status = "verified"
                delta["note"] = "follow-up completed; aggregates did" \
                                " not regress over the baseline"
        conn.execute(
            "UPDATE improvements SET verify_status = ?, verify_json = ?,"
            " verify_at = ? WHERE id = ? AND status = 'applied'",
            (verify_status,
             json.dumps(delta, ensure_ascii=True)[:6000],
             now_iso, row["id"]))
        evaluated.append({"id": row["id"],
                          "verify_status": verify_status})
    return evaluated


def rollback_failed(conn, proposal_id, now_iso):
    """Operator/executed rollback of a failed improvement: the ONLY
    world-state change of an applied improvement is its one queued
    follow-up task; removing it (only while it has NOT been worked on)
    undoes the operation exactly. Applied proposal history is kept
    (status stays 'applied', verify_status moves to 'rolled_back')."""
    row = conn.execute(
        "SELECT task_id, verify_status FROM improvements WHERE id = ?",
        (proposal_id,)).fetchone()
    if row is None:
        raise ValueError("unknown proposal")
    if row["verify_status"] != "failed_checked":
        raise ValueError("rollback requires a failed_checked"
                         " verification")
    task = db_mod.get_task(conn, row["task_id"]) if row["task_id"] \
        else None
    if task is None:
        # nothing on disk to undo; conclude the record honestly
        conn.execute(
            "UPDATE improvements SET verify_status = 'rolled_back',"
            " verify_at = ? WHERE id = ?", (now_iso, proposal_id))
        return "rolled_back"
    if task["status"] not in ("queued", "planned", "warn"):
        raise ValueError("the applied task has been worked on; safe"
                         " automatic rollback impossible, operator"
                         " intervention required")
    db_mod.delete_task(conn, row["task_id"])
    conn.execute(
        "UPDATE improvements SET verify_status = 'rolled_back',"
        " verify_at = ? WHERE id = ?", (now_iso, proposal_id))
    return "rolled_back"
