"""Goal execution worker (Phase 8B).

Single-threaded orchestrator: planning → local safety check → (optional
injected reviewer) → bounded sequential step execution → verification →
honest report. All state changes go through the goalstate machine with
conditional DB writes; progress lives in SQLite, so restarts recover
exactly what was persisted — completed steps are NEVER re-run, and an
interrupted non-idempotent operation is reported as needing human
reconciliation instead of being blindly repeated.

Determinism/testability: `process_once(goal_id)` drives one goal
synchronously; the poller thread just keeps calling scans. Tests can
drive process_once directly without any threads.

No live inference is performed by tests: the model interface is the
existing provider adapter, exercised only against the fake upstream.
"""

import datetime
import hashlib
import json
import threading
import time
from pathlib import Path

import approvals
import permissions
import applog
import db
import goalstate
import planner
import provider
import reviewer as reviewer_mod


MAX_STEP_ATTEMPTS = 2          # bounded retry budget per step
GOAL_MAX_WALL_SECONDS = 300    # per-goal time budget, checked per step
POLL_INTERVAL_SECONDS = 0.5

#: Bounded repair budget (Phase 10B): a failing VERIFICATION recipe can
#: trigger at most this many full re-plan → re-approve cycles. Beyond
#: that the goal stops honestly as blocked: repair_budget_exhausted.
MAX_REPAIR_CYCLES = 2

#: Bounded diagnostic tail carried into the repair prompt / failure
#: record (never a full child-output dump).
REPAIR_DIAG_CHARS = 1200

#: Dynamic mode (Phase 12B) hard budgets. Every one is enforced against
#: PERSISTED state so a restart cannot reset them.
MAX_DYNAMIC_TURNS = 12         # max executed decisions (goal_steps rows)
MAX_DYNAMIC_INVALID = 2        # invalid model decisions before blocking
DYNAMIC_MAX_LOOP_PASSES = 48   # defensive in-process iteration cap


def effective_model(conn, cfg):
    """Same precedence as the chat path: persisted runtime override
    first, then the environment policy (read-only mirror of
    server._model_override; garbage overrides are ignored)."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key='model_override'"
    ).fetchone()
    if row is not None:
        override = row["value"]
        if isinstance(override, str) and override.strip() \
                and len(override) <= 128 \
                and not any(ch.isspace() for ch in override):
            return override
    return cfg.model


class GoalWorker:
    def __init__(self, db_path, cfg, registry, reviewer=None,
                 max_step_attempts=MAX_STEP_ATTEMPTS,
                 goal_ttl_seconds=GOAL_MAX_WALL_SECONDS,
                 max_repair_cycles=MAX_REPAIR_CYCLES):
        self.db_path = db_path
        self.cfg = cfg
        self.registry = registry
        # Central permission engine — fail-closed authority for every
        # tool invocation (worker sequential + dynamic modes).
        self.permissions = permissions.PermissionEngine(db_path)
        # External reviewer = dependency injected. Unavailable unless a
        # phase explicitly configures one; never same-model self-review.
        self.reviewer = reviewer if reviewer is not None \
            else reviewer_mod.UnavailableReviewer()
        self.max_step_attempts = max_step_attempts
        self.goal_ttl_seconds = goal_ttl_seconds
        self.max_repair_cycles = max_repair_cycles
        self._stop = threading.Event()
        self._thread = None

    def _db_exists(self):
        return Path(self.db_path).exists()

    def _conn(self):
        return db.open_conn(self.db_path)

    # ------------------------------------------------------------------
    # thread lifecycle

    def start(self):
        """Lifecycle-managed poller: bounded, interruptible, cleanly
        joined by stop() (tests and server shutdown both call this)."""
        self._thread = threading.Thread(
            target=self._poll_loop, name="goal-worker", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=5)

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:
                # safe category only — never exception text (it may
                # echo untrusted goal content into the log stream)
                applog.event("goal_worker_error", note="scan error")
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def scan(self):
        """One bounded scan: actionable goals are processed serially to
        their next boundary — one goal at a time, no thread fan-out."""
        if not self._db_exists():
            return  # nothing to scan; never create the database here
        conn = self._conn()
        try:
            reachable = [
                g for g in db.list_actionable_goals(conn)
                if not goalstate.is_terminal(g["status"])]
        finally:
            conn.close()
        for goal in reachable:
            if self._stop.is_set():
                return
            try:
                self.process_once(goal["id"])
            except Exception:
                applog.event("goal_worker_error", note="process error")

    # ------------------------------------------------------------------
    # transitions + events

    def _transition(self, conn, goal, new_status, fields=None):
        goalstate.transition(goal["status"], new_status)
        ok = db.update_goal_fields(
            conn, goal["id"], goal["status"],
            {"status": new_status, **(fields or {})})
        if ok:
            db.insert_goal_event(
                conn, goal["id"], "goal_transition",
                f"{goal['status']}->{new_status}")
        return ok

    def _cancelled(self, conn, goal):
        """Cancellation checkpoint: flag honored between phases."""
        fresh = db.get_goal(conn, goal["id"])
        if not fresh.get("cancel_requested") \
                or goalstate.is_terminal(fresh["status"]):
            return fresh["status"] in goalstate.TERMINAL_STATUSES
        if fresh["status"] != "cancelled":
            self._transition(conn, fresh, "cancelled")
            db.insert_goal_event(conn, goal["id"], "goal_cancelled",
                                 "cancelled at the next checkpoint")
        return True

    # ------------------------------------------------------------------
    # goal processing

    def process_once(self, goal_id):
        """Drive one goal through its next phase(s), synchronously."""
        conn = self._conn()
        try:
            goal = db.get_goal(conn, goal_id)
            if goal is None or goalstate.is_terminal(goal["status"]):
                return
            handlers = {
                "queued": self._phase_plan,
                "planning": self._phase_plan,
                "ready": self._phase_execute,
                "executing": self._phase_execute,
                "awaiting_approval": self._phase_approvals,
                "verifying": self._phase_verify,
            }
            handler = handlers.get(goal["status"])
            if goal.get("mode") == "dynamic":
                handler = {
                    "queued": self._phase_plan_dynamic,
                    "planning": self._phase_plan_dynamic,
                    "ready": self._phase_execute_dynamic,
                    "executing": self._phase_execute_dynamic,
                    "awaiting_approval": self._phase_approvals,
                    "verifying": self._phase_verify,
                }.get(goal["status"])
            if handler is None:
                applog.event("goal_worker_error",
                             note=f"no handler for {goal['status']}")
                return
            handler(conn, goal)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # planning

    def _phase_plan(self, conn, goal):
        if goal["status"] == "queued":
            if not self._transition(conn, goal, "planning"):
                return
            db.insert_goal_event(conn, goal["id"], "planning_started",
                                 "planner call begins")
        goal = db.get_goal(conn, goal["id"])
        prompt = planner.plan_prompt(goal["objective"], self.registry)
        repair_context = self._repair_context(conn, goal)
        if repair_context:
            prompt = prompt + "\n" + repair_context
        plan = last_reject = None
        for attempt in (0, 1):  # exactly one bounded repair attempt
            if self._cancelled(conn, goal):
                return
            try:
                reply = self._model_call(conn, prompt)
            except provider.ProviderError as exc:
                fresh = db.get_goal(conn, goal["id"])
                self._finish_failure(conn, fresh, exc.code,
                                     "planner call failed")
                return
            plan, err, detail = planner.validate_plan(reply, self.registry)
            if plan is not None:
                break
            last_reject = (err, detail)
            prompt = (planner.plan_prompt(
                goal["objective"], self.registry)
                + "\nPrevious attempt rejected (code " + err
                + "). Return a corrected single JSON plan now.")
        if plan is None:
            fresh = db.get_goal(conn, goal["id"])
            self._finish_blocked(conn, fresh, last_reject[0],
                                 "plan rejected after one repair attempt")
            return
        goal = db.get_goal(conn, goal["id"])
        if self._cancelled(conn, goal):
            return
        check = reviewer_mod.local_safety_check(plan, self.registry)
        db.insert_goal_event(
            conn, goal["id"], "safety_checked",
            "approved" if check["approved"] else check["reason"])
        if not check["approved"]:
            self._finish_blocked(conn, goal, check["reason"],
                                 "local safety check rejected the plan")
            return
        outcome = self._review_gate(conn, goal, plan, check)
        if outcome is None:
            return
        review_record, review_source, plan = outcome
        if self._cancelled(conn, db.get_goal(conn, goal["id"])):
            return
        plan_json_string = json.dumps(self._plan_block(
            plan, review_source=review_source, review=review_record,
            repair_cycle=goal.get("repair_count") or 0),
            ensure_ascii=True)
        plan_sha = hashlib.sha256(plan_json_string.encode()).hexdigest()
        with conn:
            db.replace_goal_steps(conn, goal["id"], plan["steps"])
            # A repair cycle supersedes the previous plan's proposals:
            # the digests bound the old plan; fresh content must obtain
            # fresh approvals (the human never authorized the change).
            superseded = db.list_goal_approvals(conn, goal["id"])
            if superseded:
                db.delete_goal_approvals(conn, goal["id"])
                db.insert_goal_event(
                    conn, goal["id"], "proposals_superseded",
                    f"{len(superseded)} prior proposal(s) dropped with"
                    " the superseded plan; new approvals required")
            proposal_count = self._create_proposals(
                conn, goal, plan, plan_sha)
        ok = self._transition(conn, goal, "ready",
                              {"plan_json": plan_json_string})
        if ok:
            db.insert_goal_event(conn, goal["id"], "plan_ready",
                                 f'{plan["risk"]} risk; '
                                 f'{len(plan["steps"])} steps'
                                 + (f"; {proposal_count} write"
                                    f" proposal(s) awaiting approval"
                                    if proposal_count else ""))
            self._phase_execute(conn, db.get_goal(conn, goal["id"]))

    def _repair_context(self, conn, goal):
        """Bounded, persisted failure evidence for a repair-cycle
        planner prompt. Read from the durable last_failure_json so a
        restart mid-repair keeps the same context."""
        if not (goal.get("repair_count") or 0):
            return ""
        try:
            failure = json.loads(goal.get("last_failure_json") or "{}")
        except ValueError:
            failure = {}
        if not isinstance(failure, dict) or not failure:
            return ""
        context = {
            "failed_step_seq": failure.get("seq"),
            "recipe": failure.get("tool"),
            "status": failure.get("status"),
            "exit_code": failure.get("exit_code"),
            "stderr_tail": failure.get("stderr", ""),
        }
        return ("Verification-driven repair context (Phase 10B): the"
                " previous plan's verification recipe returned this"
                " honest evidence (token tail only):\n"
                + json.dumps(context, ensure_ascii=True)[:2200]
                + "\nFix the underlying content; keep every material"
                " acceptance criterion the objective requires.")

    def _create_proposals(self, conn, goal, plan, plan_sha):
        """Persist one digest-bound approval row per approval-class
        step. Content lives in the persisted step args; the row holds
        hashes only. Returns the number of proposals created."""
        count = 0
        for idx, step in enumerate(plan.get("steps") or [], start=1):
            tool = self.registry.get(step.get("tool"))
            if tool is None or tool.permission != "approval_required":
                continue
            binding = approvals.digest_for(
                goal["id"], idx, step["tool"],
                step.get("args") or {}, plan_sha)
            db.create_goal_approval(
                conn, goal["id"], idx, step["tool"],
                binding["kind"], binding["path"],
                binding["before_sha256"], binding["content_sha256"],
                binding["bytes"], plan_sha, binding["digest"])
            count += 1
        if count:
            db.insert_goal_event(
                conn, goal["id"], "proposals_created",
                f"{count} write proposal(s) bound to exact digests")
        return count

    def _model_call(self, conn, prompt):
        """Phase 8A routing: effective model (override first) with the
        configured fallback policy; identical prompt for both.
        One pinned snapshot per call: a settings apply mid-goal can
        never split a single model request across policies."""
        cfg = self.cfg
        model = effective_model(conn, cfg)
        result = provider.complete_with_fallback(
            [{"role": "user", "content": prompt}],
            base_url=self.cfg.api_base,
            api_key=self.cfg.api_key,
            primary_model=model,
            fallback_model=self.cfg.fallback_model,
            timeout_seconds=self.cfg.timeout_seconds,
            max_tokens=self.cfg.max_tokens,
        )
        return result["reply"]

    # ------------------------------------------------------------------
    # execution

    def _phase_approvals(self, conn, goal):
        """Polling handler for awaiting_approval: resume only when EVERY
        write proposal of the goal state is 'approved' (claimed by the
        run itself); block honestly if the human rejected one."""
        proposals = db.list_goal_approvals(conn, goal["id"])
        unresolved, any_rejected, any_missing = [], False, False
        for proposal in proposals:
            state = proposal["state"]
            if state == "rejected":
                any_rejected = True
            elif state == "proposed":
                unresolved.append(proposal["seq"])
            elif state not in ("approved", "claimed", "executed"):
                any_missing = True
        if any_rejected:
            self._finish_blocked(
                conn, goal, "approval_withdrawn",
                "the human rejected the exact write proposal; the goal"
                " will not run")
            return
        if any_missing:
            self._finish_blocked(
                conn, goal, "approval_claim_conflict",
                "an approval record is in an unusable state")
            return
        if unresolved:
            return  # still waiting; the poller re-visits next scan
        # Missing rows: only possible for a tampered database; verify
        # every approval-class step has a proposal before resuming.
        for step in db.get_goal_steps(conn, goal["id"]):
            tool = self.registry.get(step["tool_name"])
            if tool is None or tool.permission != "approval_required":
                continue
            if db.get_goal_approval(conn, goal["id"], step["seq"]) is None:
                self._finish_blocked(
                    conn, goal, "approval_claim_conflict",
                    "approval records missing for a write step")
                return
        ok = self._transition(conn, goal, "executing")
        if ok:
            db.insert_goal_event(
                conn, goal["id"], "approval_granted_all",
                "all proposals approved; execution resumes")
            fresh = db.get_goal(conn, goal["id"])
            if fresh.get("mode") == "dynamic":
                self._phase_execute_dynamic(conn, fresh)
            else:
                self._run_steps(conn, fresh)

    def _phase_execute(self, conn, goal):
        if goal["status"] == "ready":
            if not self._transition(conn, goal, "executing"):
                return
            goal = db.get_goal(conn, goal["id"])
        self._run_steps(conn, goal)

    # ------------------------------------------------------------------
    # dynamic mode (Phase 12B): bounded observe → decide → act loop.
    # Pattern attribution: observation-feedback loop shape and
    # repeat-guard semantics after Stanford OpenJarvis's native_react /
    # loop_guard (Apache-2.0), re-implemented natively — no code import.
    # Every budget below is enforced against PERSISTED state, and every
    # tool call goes through the same registry, permission classes, and
    # digest-bound approvals as the sequential mode.

    def _phase_plan_dynamic(self, conn, goal):
        """Dynamic goals skip the sequential planner. Phase 12D: the
        first model call proposes a SUCCESS CONTRACT (what must be
        demonstrated, with which registered tools it will be
        evidenced); it is validated deterministically, safety-checked,
        independently reviewed when the existing policy requires it,
        and persisted on the goal. The model proposes the contract —
        it can never approve it, and it can never declare the work
        successful by assertion."""
        if goal["status"] == "queued":
            if not self._transition(conn, goal, "planning"):
                return
        goal = db.get_goal(conn, goal["id"])
        existing = self._read_plan(conn, goal)
        if existing.get("mode") == "dynamic" and existing.get("criteria"):
            # A validated contract is already persisted: it survives
            # retry and restart and stays bound to this goal.
            contract = existing
        else:
            contract = self._build_contract(conn, goal)
            if contract is None:
                return
        db.insert_goal_event(
            conn, goal["id"], "dynamic_mode",
            "success contract persisted; the bounded decision loop"
            " owns execution")
        goal = db.get_goal(conn, goal["id"])
        if self._transition(conn, goal, "ready"):
            self._phase_execute_dynamic(
                conn, db.get_goal(conn, goal["id"]))

    def _build_contract(self, conn, goal):
        """One bounded contract negotiation: propose → deterministic
        validation → exactly one repair attempt. Returns the validated
        contract dict, or None after resolving the goal honestly."""
        prompt = planner.contract_prompt(goal["objective"], self.registry)
        contract = last_reject = None
        for attempt in (0, 1):  # exactly one bounded repair attempt
            if self._cancelled(conn, goal):
                return None
            try:
                reply = self._model_call(conn, prompt)
            except provider.ProviderError as exc:
                fresh = db.get_goal(conn, goal["id"])
                self._finish_failure(conn, fresh, exc.code,
                                     "contract call failed")
                return None
            contract, err, detail = planner.validate_contract(
                reply, self.registry, goal["objective"])
            if contract is not None:
                break
            last_reject = (err, detail)
            prompt = (planner.contract_prompt(
                goal["objective"], self.registry)
                + "\nPrevious contract rejected (code " + err
                + "). Return a corrected single JSON contract now.")
        if contract is None:
            fresh = db.get_goal(conn, goal["id"])
            self._finish_blocked(
                conn, fresh, "contract_unverifiable",
                f"the success contract was rejected twice"
                f" ({last_reject[0]}); the objective needs"
                " clarification or verifiable evidence tools")
            return None
        # Deterministic safety gate over the contract, plan-shaped: the
        # evidence tools are exactly the surface this goal will run.
        plan_shape = {
            "risk": "low",
            "criteria": contract["criteria"],
            "steps": [{"tool": tool}
                      for entry in contract["evidence"]
                      for tool in entry["tools"]],
        }
        check = reviewer_mod.local_safety_check(plan_shape, self.registry)
        db.insert_goal_event(
            conn, goal["id"], "safety_checked",
            "contract " + ("approved" if check["approved"]
                           else check["reason"]))
        if not check["approved"]:
            fresh = db.get_goal(conn, goal["id"])
            self._finish_blocked(conn, fresh, check["reason"],
                                 "local safety check rejected the"
                                 " contract")
            return None
        review_record = None
        review_source = "local_validation"
        if check.get("needs_external_review"):
            outcome = self._contract_review(conn, goal, plan_shape, check)
            if outcome is None:
                return None
            review_record, review_source = outcome
        block = {"mode": "dynamic", **contract,
                 "review_source": review_source}
        if review_record is not None:
            block["review"] = review_record
        plan_json = json.dumps(block, ensure_ascii=True)
        if not db.update_goal_fields(conn, goal["id"], goal["status"],
                                     {"plan_json": plan_json}):
            return None
        return contract

    def _contract_review(self, conn, goal, plan_shape, check):
        """Independent review for contracts that execute approval-class
        evidence tools (Phase 10A policy parity). One bounded round:
        approved → proceed; rejected/revise → blocked; failed or
        unavailable → blocked reviewer_unavailable. No approval is ever
        fabricated. Returns (review_record, source) or None."""
        try:
            external = self.reviewer.review(
                plan_shape, self.registry,
                objective=goal["objective"], local_check=check)
        except Exception:
            # No exception text (may hold untrusted reviewer data).
            applog.event("review_failed", note="reviewer exception")
            external = {"status": "failed", "reason": "reviewer_exception"}
        record = self._review_record(external, 1, False, check)
        status = record["status"]
        if status == "approved":
            db.insert_goal_event(
                conn, goal["id"], "review_external",
                "contract approved by "
                + (record["identity"] or "external reviewer"))
            return record, "external_review"
        code = ("reviewer_unavailable"
                if status in ("unavailable", "failed")
                else "review_rejected")
        db.insert_goal_event(
            conn, goal["id"],
            "review_unavailable" if code == "reviewer_unavailable"
            else "review_failed",
            record["failure_reason"] or "contract review did not"
            " approve it")
        self._finish_blocked(
            conn, db.get_goal(conn, goal["id"]), code,
            "the contract requires external review and none approved"
            " it; no approval is fabricated")
        return None

    def _phase_execute_dynamic(self, conn, goal):
        if goal["status"] == "ready":
            if not self._transition(conn, goal, "executing"):
                return
        deadline = time.monotonic() + self.goal_ttl_seconds
        for _pass in range(DYNAMIC_MAX_LOOP_PASSES):
            fresh = db.get_goal(conn, goal["id"])
            if goalstate.is_terminal(fresh["status"]) \
                    or fresh["status"] != "executing":
                return
            if self._cancelled(conn, fresh):
                return
            steps = db.get_goal_steps(conn, fresh["id"])
            if len(steps) >= MAX_DYNAMIC_TURNS:
                self._finish_blocked(
                    conn, fresh, "loop_budget_exhausted",
                    f"dynamic turn budget ({MAX_DYNAMIC_TURNS})"
                    " exhausted; persisted evidence preserved")
                return
            pending = next(
                (s for s in steps if s["status"] == "pending"), None)
            if pending is not None:
                # An already-decided step awaiting execution: restart
                # recovery, or an approved write proposal resuming.
                self._run_step(conn, fresh, pending, deadline)
                continue
            if time.monotonic() > deadline:
                self._finish_blocked(
                    conn, fresh, "goal_time_exceeded",
                    "dynamic goal exceeded its wall-clock budget")
                return
            self._dynamic_decide(conn, fresh, steps, deadline)
        else:
            self._finish_blocked(
                conn, db.get_goal(conn, goal["id"]),
                "loop_budget_exhausted",
                "dynamic loop did not converge; defensive stop")

    def _dynamic_decide(self, conn, goal, steps, deadline):
        contract = self._read_plan(conn, goal)
        prompt = planner.decision_prompt(
            goal["objective"], self.registry,
            self._dynamic_transcript(steps),
            criteria=contract.get("criteria") or ())
        try:
            reply = self._model_call(conn, prompt)
        except provider.ProviderError as exc:
            fresh = db.get_goal(conn, goal["id"])
            self._finish_failure(conn, fresh, exc.code,
                                 "dynamic decision call failed")
            return
        decision, err, detail = planner.validate_decision(
            reply, self.registry)
        fresh = db.get_goal(conn, goal["id"])
        if goalstate.is_terminal(fresh["status"]) \
                or fresh["status"] != "executing" \
                or self._cancelled(conn, fresh):
            return
        if decision is None:
            # Durable accounting (Phase 12D): the invalid-decision count
            # lives on the goal row itself — bounded event retention and
            # restarts can never reset or undercount the budget.
            count = (fresh.get("dynamic_invalid_count") or 0) + 1
            if not db.update_goal_fields(
                    conn, fresh["id"], fresh["status"],
                    {"dynamic_invalid_count": count}):
                return  # another writer moved the goal; never double-count
            db.insert_goal_event(
                conn, fresh["id"], "dynamic_decision_invalid",
                f"rejected decision ({err}); {detail}"[:300])
            if count >= MAX_DYNAMIC_INVALID:
                self._finish_blocked(
                    conn, db.get_goal(conn, fresh["id"]),
                    "decision_invalid",
                    f"the model produced {count} invalid decisions;"
                    " stopping honestly")
            return
        if decision["action"] == "done":
            self._dynamic_done(conn, fresh, decision)
            return
        tool = self.registry.get(decision["action"])
        if tool is None:
            self._finish_blocked(conn, fresh, "unsupported_capability",
                                 "decision names an unregistered tool")
            return
        if self._dynamic_repeat(conn, fresh, steps, decision):
            return
        self._dynamic_run_tool(conn, fresh, tool, decision, deadline)

    def _dynamic_done(self, conn, goal, decision):
        steps = db.get_goal_steps(conn, goal["id"])
        if not steps or any(s["status"] != "done" for s in steps):
            db.insert_goal_event(
                conn, goal["id"], "dynamic_done_refused",
                "done claimed without verified step evidence; refused")
            self._finish_blocked(
                conn, db.get_goal(conn, goal["id"]),
                "criteria_unverified",
                "done was claimed but the executed steps are missing"
                " or unverified; a claim alone is not evidence")
            return
        db.insert_goal_event(conn, goal["id"], "dynamic_done",
                             decision["summary"][:300])
        if self._transition(conn, goal, "verifying"):
            self._phase_verify(conn, db.get_goal(conn, goal["id"]),
                               outcome=decision["summary"])

    def _dynamic_run_tool(self, conn, goal, tool, decision, deadline):
        seq = len(db.get_goal_steps(conn, goal["id"])) + 1
        # Deny before any proposal is created: the model cannot even
        # request a consequential action the engine would refuse.
        perm = self.permissions.authorize(tool, goal_id=goal["id"])
        if not perm["allowed"]:
            db.insert_goal_event(
                conn, goal["id"], "tool_denied",
                f"{tool.name}: permission_denied ({perm['reason']})")
            return
        if tool.permission == "approval_required":
            # The model can REQUEST a write; it can never authorize
            # one. The exact binding becomes a digest-bound proposal;
            # until a human approves it, execution never starts.
            plan_json = self._dynamic_plan_lock(conn, goal)
            if plan_json is None:
                return
            plan_sha = hashlib.sha256(plan_json.encode()).hexdigest()
            binding = approvals.digest_for(
                goal["id"], seq, tool.name, decision["args"], plan_sha)
            db.append_goal_step(
                conn, goal["id"], purpose=decision["reason"],
                tool_name=tool.name, args=decision["args"],
                acceptance=decision["reason"])
            db.create_goal_approval(
                conn, goal["id"], seq, tool.name,
                binding["kind"], binding["path"],
                binding["before_sha256"], binding["content_sha256"],
                binding["bytes"], plan_sha, binding["digest"])
            db.insert_goal_event(
                conn, goal["id"], "proposals_created",
                "dynamic write request bound to exact digest; human"
                " approval required before execution")
            self._checkpoint_approval(
                conn, db.get_goal(conn, goal["id"]))
            return
        step = db.append_goal_step(
            conn, goal["id"], purpose=decision["reason"],
            tool_name=tool.name, args=decision["args"],
            acceptance=decision["reason"])
        self._run_step(conn, db.get_goal(conn, goal["id"]), step,
                       deadline)

    def _dynamic_plan_lock(self, conn, goal):
        """Stable plan_json anchor for dynamic-mode digest binding: the
        replay check recomputes sha256(plan_json), so this block is
        written exactly once and never mutated afterwards."""
        if goal.get("plan_json"):
            return goal["plan_json"]
        plan_json = json.dumps({"mode": "dynamic"}, ensure_ascii=True)
        if not db.update_goal_fields(conn, goal["id"], goal["status"],
                                     {"plan_json": plan_json}):
            return None
        return plan_json

    def _dynamic_repeat(self, conn, goal, steps, decision):
        """Loop guard: an exact repeat of an already-executed
        (tool, args) pair is refused — the loop cannot spin. Returns
        True when the goal was blocked."""
        digest = self._decision_digest(decision["action"],
                                       decision["args"])
        for s in steps:
            try:
                args = json.loads(s["args_json"] or "{}")
            except ValueError:
                continue
            if self._decision_digest(s["tool_name"], args) == digest:
                db.insert_goal_event(
                    conn, goal["id"], "dynamic_repeat_refused",
                    f"seq {s['seq']} already ran this exact call;"
                    " refusing to loop")
                self._finish_blocked(
                    conn, db.get_goal(conn, goal["id"]),
                    "loop_budget_exhausted",
                    "the model repeated an already-executed call")
                return True
        return False

    @staticmethod
    def _decision_digest(tool_name, args):
        return hashlib.sha256(json.dumps(
            [tool_name, args], ensure_ascii=True,
            sort_keys=True).encode()).hexdigest()

    def _dynamic_transcript(self, steps):
        """Bounded, persisted observation transcript for the next
        decision prompt. Tool results are DATA here, never code."""
        lines = []
        for s in steps[-planner.DYNAMIC_TRANSCRIPT_TURNS:]:
            if s["status"] == "done":
                try:
                    observation = json.dumps(
                        json.loads(s["result_json"] or "{}"),
                        ensure_ascii=True)
                except ValueError:
                    observation = ""
                label = "observation"
            elif s["status"] == "failed":
                observation = s["error_code"] or "failed"
                label = "error"
            else:
                observation = s["status"]
                label = "state"
            lines.append(
                f"[{s['seq']}] {s['tool_name']} ({s['purpose']})"
                f"\n{label}: "
                f"{observation[:planner.MAX_DYNAMIC_OBS_CHARS]}")
        return "\n".join(lines)

    @staticmethod
    def _dynamic_event_count(conn, goal_id, event):
        """Audit helper only (Phase 12D): budget ENFORCEMENT uses the
        durable goal-row counter, never this retention-bounded count."""
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM goal_events"
            " WHERE goal_id=? AND event=?",
            (goal_id, event),
        ).fetchone()
        return int(row["c"])

    def _run_steps(self, conn, goal):
        deadline = time.monotonic() + self.goal_ttl_seconds
        for step in db.get_goal_steps(conn, goal["id"]):
            fresh = db.get_goal(conn, goal["id"])
            if goalstate.is_terminal(fresh["status"]) \
                    or fresh["status"] in ("awaiting_approval",
                                           "planning"):
                return
            if self._cancelled(conn, fresh):
                return
            self._run_step(conn, fresh, step, deadline)
            latest = db.get_goal(conn, goal["id"])
            if goalstate.is_terminal(latest["status"]) \
                    or latest["status"] in ("blocked", "planning"):
                return
        fresh = db.get_goal(conn, goal["id"])
        if fresh["status"] == "executing":
            self._phase_verify(conn, fresh)

    def _now(self):
        return datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _checkpoint_approval(self, conn, goal):
        """Move the goal to the human-approval wait state. The write
        step itself stays 'pending' and is never touched here."""
        db.insert_goal_event(
            conn, goal["id"], "approval_requested",
            "write proposal waits for explicit human approval")
        return self._transition(conn, goal, "awaiting_approval")

    def _approval_gate_before_run(self, conn, goal, step, tool):
        """Pre-execution authorization gate for approval-class tools.

        Returns True when execution may proceed (approval already
        claimed atomically by this call); False means the goal moved to
        awaiting_approval (this scan stops), or an integrity failure
        was recorded. The approve->claim move is a conditional UPDATE:
        at most one writer ever turns 'approved' into 'claimed', and
        the digest is re-verified against the PERSISTED rows first — a
        restart or tampering cannot silently swap the executed content
        for something the human never saw."""
        approval = db.get_goal_approval(conn, goal["id"], step["seq"])
        if approval is None:
            self._fail_step(conn, goal, step, step["status"],
                            "approval_claim_conflict")
            return False
        if approval["state"] != "approved":
            self._checkpoint_approval(conn, goal)
            return False
        plan_sha = hashlib.sha256(
            (goal.get("plan_json") or "").encode()).hexdigest()
        try:
            args = json.loads(step["args_json"] or "{}")
        except ValueError:
            args = {}
        rebuilt = approvals.digest_for(
            goal["id"], step["seq"], step["tool_name"], args, plan_sha)
        if rebuilt["digest"] != approval["digest"] \
                or plan_sha != approval["plan_sha256"]:
            db.insert_goal_event(
                conn, goal["id"], "approval_refused",
                f"seq {step['seq']}: digest replay mismatch before"
                " execution; write refused")
            self._fail_step(conn, goal, step, step["status"],
                            "approval_claim_conflict")
            return False
        claimed = db.update_goal_approval_state(
            conn, goal["id"], step["seq"], "approved", "claimed",
            {"claimed_at": self._now()})
        if not claimed:
            # Another writer got there first: never double-run.
            self._checkpoint_approval(conn, goal)
            return False
        return True

    def _run_step(self, conn, goal, step, deadline):
        tool = self.registry.get(step["tool_name"])
        if tool is None:
            # The registry changed under a validated plan: refuse
            # execution, do not improvise.
            db.update_goal_step_status(
                conn, goal["id"], step["seq"], step["status"], "failed",
                {"error_code": "unsupported_capability"})
            db.insert_goal_event(conn, goal["id"], "step_failed",
                                 f"seq {step['seq']}: unsupported_capability")
            self._finish_blocked(conn, goal, "unsupported_capability",
                                 "planned tool is not registered")
            return
        # Central permission check (also covers dynamic mode: every run
        # passes through here). Fail-closed on engine/db failure.
        perm = self.permissions.authorize(tool, goal_id=goal["id"])
        if not perm["allowed"]:
            db.update_goal_step_status(
                conn, goal["id"], step["seq"], step["status"], "failed",
                {"error_code": "permission_denied"})
            db.insert_goal_event(
                conn, goal["id"], "step_failed",
                f"seq {step['seq']}: {tool.name} permission_denied"
                f" ({perm['reason']})")
            self._finish_blocked(conn, goal, "permission_denied",
                                 "tool invocation denied by the"
                                 f" permission engine ({perm['reason']})")
            return
        if time.monotonic() > deadline:
            self._fail_step(conn, goal, step, "pending",
                            "goal_time_exceeded")
            return
        if step["attempts"] >= self.max_step_attempts:
            self._fail_step(conn, goal, step, step["status"],
                            "step_retry_exhausted")
            return
        if tool.permission == "approval_required" \
                and not self._approval_gate_before_run(conn, goal, step,
                                                       tool):
            return
        try:
            args = tool.validate_args(
                json.loads(step["args_json"] or "{}"))
        except Exception:
            self._fail_step(conn, goal, step, step["status"],
                            "invalid_tool_args")
            return
        if not db.mark_step_running(conn, goal["id"], step["seq"]):
            return  # another writer claimed/completed it; never re-run
        db.insert_goal_event(conn, goal["id"], "step_started",
                             f"seq {step['seq']}: {tool.name}")
        try:
            result = tool.run(args)
        except Exception as exc:
            code = getattr(exc, "code", "tool_error")
            if code.startswith("invalid_tool_args"):
                code = "invalid_tool_args"
            self._fail_step(conn, goal, step, "running", code)
            return
        verdict = tool.verify(args, result)
        result_json = json.dumps(result, ensure_ascii=True)
        detail = verdict.get("detail") if isinstance(verdict, dict) else None
        if verdict.get("passed"):
            db.update_goal_step_status(
                conn, goal["id"], step["seq"], "running", "done",
                {"result_json": result_json,
                 "verification": str(detail)[:300]})
            db.insert_goal_event(conn, goal["id"], "step_done",
                                 f"seq {step['seq']}: "
                                 f"{str(detail)[:120]}")
            if tool.permission == "approval_required" \
                    and isinstance(result, dict):
                executed_sha = str(result.get("sha256") or "")
                extra = {"executed_at": self._now()}
                if len(executed_sha) == 64:
                    extra["executed_sha256"] = executed_sha
                db.update_goal_approval_state(
                    conn, goal["id"], step["seq"], "claimed", "executed",
                    extra)
        else:
            db.update_goal_step_status(
                conn, goal["id"], step["seq"], "running", "failed",
                {"result_json": result_json,
                 "verification": str(detail)[:300],
                 "error_code": "verification_failed"})
            db.insert_goal_event(conn, goal["id"], "step_failed",
                                 f"seq {step['seq']}: verification_failed")
            fresh = db.get_goal(conn, goal["id"])
            if getattr(tool, "records_check", False):
                if fresh.get("mode") == "dynamic":
                    self._finish_blocked(
                        conn, fresh, "criteria_unverified",
                        "the verification recipe reported a failure;"
                        " dynamic mode stops instead of re-planning")
                else:
                    self._verification_failed(conn, fresh, step, result,
                                              detail)
            else:
                self._finish_failure(conn, fresh, "verification_failed",
                                     "a step failed its declared"
                                     " postcondition")

    # ------------------------------------------------------------------
    # verification-driven repair (Phase 10B)

    def _verification_failed(self, conn, goal, step, result, detail):
        """A registered check recipe honestly reported a failure.
        Route through the bounded repair policy instead of a terminal
        failure: the evidence is kept, and within MAX_REPAIR_CYCLES a
        new planner round gets the bounded diagnostics as context."""
        if not isinstance(goal, dict) or goalstate.is_terminal(
                goal["status"]):
            return
        status = result.get("status") if isinstance(result, dict) else None
        db.insert_goal_event(
            conn, goal["id"], "verification_failed",
            f"{result.get('tool') if isinstance(result, dict) else ''}"
            f" {status}: {str(detail)[:200]}")
        if status in ("unavailable", "timed_out"):
            # Missing interpreter / runaway process: re-planning cannot
            # create the tool; stop honestly without repair budget.
            self._finish_blocked(
                conn, goal,
                "verification_unavailable" if status == "unavailable"
                else "verification_timed_out",
                f"the verification recipe reported {status}; nothing"
                " was verified")
            return
        used = goal.get("repair_count") or 0
        if used >= self.max_repair_cycles:
            self._finish_blocked(
                conn, goal, "repair_budget_exhausted",
                f"verification still failing after {used} repair"
                " cycle(s); evidence preserved for review")
            return
        failure = {
            "seq": step["seq"],
            "tool": result.get("tool"),
            "status": status,
            "exit_code": result.get("exit_code"),
            "stderr": (result.get("stderr") or "")[:REPAIR_DIAG_CHARS],
            "stdout": (result.get("stdout") or "")[:REPAIR_DIAG_CHARS],
            "detail": str(detail)[:300],
        }
        ok = db.update_goal_fields(
            conn, goal["id"], goal["status"],
            {"repair_count": used + 1,
             "last_failure_json": json.dumps(failure, ensure_ascii=True)
             [:4000]})
        if not ok:
            self._finish_failure(conn, goal, "verification_failed",
                                 "repair could not be recorded")
            return
        db.insert_goal_event(
            conn, goal["id"], "repair_started",
            f"repair cycle {used + 1}/{self.max_repair_cycles}:"
            " re-planning from recorded verification evidence")
        if self._transition(conn, goal, "planning"):
            self._phase_plan(conn, db.get_goal(conn, goal["id"]))
            return
        fresh = db.get_goal(conn, goal["id"])
        if goalstate.is_terminal(fresh["status"]):
            return  # another writer finished the goal; nothing to do
        self._finish_failure(conn, fresh, "verification_failed",
                             "a step failed its declared postcondition")

    def _fail_step(self, conn, goal, step, expected_status, code):
        """Record a step failure against its ACTUAL current status."""
        db.update_goal_step_status(
            conn, goal["id"], step["seq"], expected_status, "failed",
            {"error_code": code})
        db.insert_goal_event(conn, goal["id"], "step_failed",
                             f"seq {step['seq']}: {code}")
        self._finish_failure(conn, goal, code, "step execution failed")

    # ------------------------------------------------------------------
    # verification + reports

    def _phase_verify(self, conn, goal, outcome=None):
        if goal["status"] == "executing":
            if not self._transition(conn, goal, "verifying"):
                return
            goal = db.get_goal(conn, goal["id"])
        steps = db.get_goal_steps(conn, goal["id"])
        if not steps or not all(s["status"] == "done" for s in steps):
            self._finish_blocked(conn, goal, "criteria_unverified",
                                 "steps are missing or unfinished")
            return
        if goal.get("mode") == "dynamic":
            # Phase 12D: dynamic success is gated by the persisted
            # success contract — tool calls having worked is not enough.
            self._verify_dynamic_contract(conn, goal, steps, outcome)
            return
        criteria = (self._read_plan(conn, goal) or {}).get("criteria") or []
        step_texts = [s["purpose"] + " " + s["acceptance"] for s in steps]
        step_tokens = [planner.significant_tokens(t) for t in step_texts]
        criteria_report, all_pass = [], True
        for criterion in criteria:
            tokens = planner.significant_tokens(criterion)
            evidence = next(
                (s for s, toks in zip(steps, step_tokens)
                 if len(tokens & toks) >= planner.MIN_COVER_TOKENS),
                None)
            passed = evidence is not None
            all_pass = all_pass and passed
            criteria_report.append({
                "criterion": criterion,
                "evidence_seq": evidence["seq"] if evidence else None,
                "detail": evidence["verification"] if evidence else "",
                "passed": passed})
        fresh = db.get_goal(conn, goal["id"])
        if not all_pass:
            self._finish_blocked(conn, fresh, "criteria_unverified",
                                 "a criterion has no verified evidence")
            return
        self._finish_success(conn, fresh, criteria_report, outcome=outcome)

    def _verify_dynamic_contract(self, conn, goal, steps, outcome=None):
        """Phase 12D: dynamic success is gated by the PERSISTED success
        contract. Each criterion needs evidence that is (a) an actual
        recorded tool result from this goal's execution, (b) produced by
        a contract-declared evidence tool, and (c) deterministically
        matched against the criterion text. A successful tool call does
        not automatically satisfy every criterion, and a model's done
        claim is not evidence."""
        contract = self._read_plan(conn, goal)
        criteria = contract.get("criteria") or []
        evidence_map = {}
        for entry in contract.get("evidence") or []:
            if isinstance(entry, dict) \
                    and isinstance(entry.get("criterion"), int) \
                    and not isinstance(entry.get("criterion"), bool):
                evidence_map[entry["criterion"]] = [
                    t for t in entry.get("tools") or []
                    if isinstance(t, str)]
        done_steps = [s for s in steps if s["status"] == "done"]
        criteria_report, all_pass, missing = [], True, []
        for idx, criterion in enumerate(criteria, start=1):
            tools_needed = evidence_map.get(idx) or []
            matched = None
            for s in done_steps:
                if s["tool_name"] not in tools_needed:
                    continue
                # Values only: JSON structural keys are never evidence.
                result_tokens = planner.significant_tokens(
                    planner.result_values_text(s["result_json"]))
                if len(result_tokens & planner.significant_tokens(
                        criterion)) >= planner.MIN_COVER_TOKENS:
                    matched = s
                    break
            passed = matched is not None
            all_pass = all_pass and passed
            if not passed:
                missing.append(idx)
            criteria_report.append({
                "criterion": criterion,
                "evidence_seq": matched["seq"] if matched else None,
                "detail": matched["verification"] if matched else "",
                "passed": passed})
        fresh = db.get_goal(conn, goal["id"])
        if not all_pass:
            self._finish_blocked(
                conn, fresh, "criteria_unverified",
                f"{len(missing)} of {len(criteria)} contract"
                f" criteria lack supporting evidence from recorded"
                f" tool results (criterion "
                f"{', '.join(map(str, missing))}); the goal does not"
                " enter succeeded")
            return
        self._finish_success(conn, fresh, criteria_report, outcome=outcome)

    def _read_plan(self, conn, goal):
        try:
            return json.loads(goal.get("plan_json") or "{}")
        except ValueError:
            return {}

    def _base_report(self, conn, goal):
        steps = db.get_goal_steps(conn, goal["id"])
        return [
            {"seq": s["seq"], "tool": s["tool_name"],
             "status": s["status"],
             "verification": s["verification"],
             "attempts": s["attempts"],
             "error_code": s["error_code"]}
            for s in steps]

    def _finish_success(self, conn, goal, criteria_report, outcome=None):
        plan = self._read_plan(conn, goal)
        review = plan.get("review") or {}
        report = {
            "status": "succeeded",
            "outcome": outcome if outcome is not None
            else plan.get("outcome", ""),
            "criteria": criteria_report,
            "steps": self._base_report(conn, goal),
            "uncertainties": plan.get("uncertainties", []),
            "review": plan.get("review_source", "local_validation"),
            "review_identity": review.get("identity"),
            "review_rounds": review.get("round"),
            "plan_revised": review.get("plan_revised"),
            "truncated_results": [
                s["seq"] for s in db.get_goal_steps(conn, goal["id"])
                if self._step_truncated(s)],
        }
        if self._transition(conn, goal, "succeeded", {
                "report_json": json.dumps(report, ensure_ascii=True)}):
            passed = sum(1 for c in criteria_report if c["passed"])
            db.insert_goal_event(
                conn, goal["id"], "goal_succeeded",
                f"{passed}/{len(criteria_report)} criteria evidenced")

    def _finish_failure(self, conn, goal, code, detail):
        fresh = db.get_goal(conn, goal["id"])
        if goalstate.is_terminal(fresh["status"]):
            return
        report = {
            "status": "failed",
            "error_code": code,
            "detail": detail,
            "steps": self._base_report(conn, fresh),
        }
        if self._transition(conn, fresh, "failed", {
                "report_json": json.dumps(report, ensure_ascii=True),
                "error_code": code}):
            db.insert_goal_event(conn, fresh["id"], "goal_failed",
                                 f"{code}: {detail}")

    def _finish_blocked(self, conn, goal, code, detail, extra_fields=None):
        fresh = db.get_goal(conn, goal["id"])
        if goalstate.is_terminal(fresh["status"]):
            return
        report = {
            "status": "blocked",
            "error_code": code,
            "detail": detail,
            "steps": self._base_report(conn, fresh),
        }
        fields = {"report_json": json.dumps(report, ensure_ascii=True),
                  "error_code": code}
        if extra_fields:
            fields.update(extra_fields)
        if self._transition(conn, fresh, "blocked", fields):
            db.insert_goal_event(conn, fresh["id"], "goal_blocked",
                                 f"{code}: {detail}")

    # -- independent review gate (Phase 9B) --------------------------------

    #: At most two review invocations per plan (one revision round).
    MAX_REVIEW_ROUNDS = 2

    @staticmethod
    def _review_record(external, round_no, plan_revised, check):
        """Bounded, DB-safe snapshot of one review round. Raw reviewer
        output never enters here unsanitized; strings are clamped."""
        clamp = (lambda items: [
            (x if isinstance(x, str) else str(x))[:200]
            for x in list(items or [])[:6]])
        status = external.get("status") if isinstance(external, dict) \
            else "failed"
        identity = (str(external.get("identity"))[:128]
                    if isinstance(external, dict) and external.get("identity")
                    else None)
        return {
            "status": status if status in
            ("approved", "revise", "rejected", "failed", "unavailable")
            else "failed",
            "identity": identity,
            "verdict": external.get("verdict") if isinstance(external, dict)
            else None,
            "risk_advisory": external.get("risk_level")
            if isinstance(external, dict) else None,
            "findings": clamp(external.get("findings")
                              if isinstance(external, dict) else None),
            "missing_criteria": clamp(
                external.get("missing_criteria")
                if isinstance(external, dict) else None),
            "recommended_changes": clamp(
                external.get("recommended_changes")
                if isinstance(external, dict) else None),
            "failure_reason": external.get("reason")
            if isinstance(external, dict) else "reviewer_exception",
            "reviewed_at": external.get("reviewed_at")
            if isinstance(external, dict) else None,
            "round": round_no,
            "plan_revised": plan_revised,
            "local_validation": bool(check.get("approved")),
        }

    @staticmethod
    def _plan_block(plan, **extras):
        block = {k: plan.get(k) for k in
                 ("summary", "outcome", "criteria", "risk",
                  "uncertainties")}
        block.update(extras)
        return {k: v for k, v in block.items() if v is not None}

    def _review_gate(self, conn, goal, plan, check):
        """Run the external review stage (Phase 9B).

        Returns (review_record, review_source, plan) for the ready
        transition, or None when the goal was resolved inside (blocked
        by the reviewer or cancelled). The reviewer's verdict is
        ADVISORY: only deterministic server policy executes anything,
        and a failed/unavailable review is never an approval — for the
        current read-only capability it proceeds under the
        local-validation-only policy, recorded verbatim in plan_json.
        """
        plan_revised = False
        for round_no in range(1, self.MAX_REVIEW_ROUNDS + 1):
            if self._cancelled(conn, db.get_goal(conn, goal["id"])):
                return None
            try:
                external = self.reviewer.review(
                    plan, self.registry,
                    objective=goal["objective"], local_check=check)
            except Exception:
                # No exception text (may hold untrusted reviewer data).
                applog.event("review_failed", note="reviewer exception")
                external = {"status": "failed", "source": "review_failed",
                            "identity": None, "verdict": None,
                            "risk_level": None, "findings": [],
                            "missing_criteria": [],
                            "recommended_changes": [],
                            "reason": "reviewer_exception",
                            "reviewed_at": None}
            record = self._review_record(external, round_no, plan_revised,
                                         check)
            status = record["status"]
            if status in ("unavailable", "failed") \
                    and check.get("needs_external_review"):
                # Phase 10A policy: a plan proposing workspace writes is
                # NEVER allowed to proceed without a real external
                # review. No approval is fabricated; the human approval
                # layer comes after this and presupposes it.
                db.insert_goal_event(
                    conn, goal["id"],
                    "review_unavailable" if status == "unavailable"
                    else "review_failed",
                    "write plan requires external review; proceeding"
                    " without one is refused")
                self._finish_blocked(
                    conn, goal, "reviewer_unavailable",
                    "this plan proposes workspace writes; an external"
                    " review is required and none is available")
                return None
            if status == "approved":
                db.insert_goal_event(
                    conn, goal["id"], "review_external",
                    "approved by "
                    + (record["identity"] or "external reviewer"))
                return record, "external_review", plan
            if status == "rejected":
                self._finish_blocked(
                    conn, goal, "review_rejected",
                    f"external reviewer rejected the plan "
                    f"(round {round_no})",
                    extra_fields={"plan_json": json.dumps(
                        self._plan_block(
                            plan, review_source="review_rejected",
                            review=record), ensure_ascii=True)},
                )
                return None
            if status == "unavailable":
                db.insert_goal_event(
                    conn, goal["id"], "review_unavailable",
                    record["failure_reason"] or "reviewer unavailable")
                # Local-only pathway stays available for the current
                # read-only, locally-approved capability. The record
                # says exactly that; no approval is fabricated.
                return record, "local_validation", plan
            if status == "failed":
                db.insert_goal_event(
                    conn, goal["id"], "review_failed",
                    record["failure_reason"] or "review failed")
                return record, "local_validation", plan
            if status == "revise" and round_no < self.MAX_REVIEW_ROUNDS:
                plan = self._revised_plan(conn, goal, plan, record)
                if plan is None:
                    return None
                plan_revised = True
                check = reviewer_mod.local_safety_check(plan, self.registry)
                db.insert_goal_event(
                    conn, goal["id"], "safety_checked",
                    "approved (revalidated)"
                    if check["approved"] else check["reason"])
                if not check["approved"]:
                    self._finish_blocked(
                        conn, goal, check["reason"],
                        "local safety check rejected the revised plan")
                    return None
                continue
            # revise on the final round, or an unrecognized status:
            # cannot proceed without a decision; block honestly.
            self._finish_blocked(
                conn, goal, "review_rejected",
                f"reviewer still requests revisions after the revision "
                f"budget (round {round_no})",
                extra_fields={"plan_json": json.dumps(
                    self._plan_block(
                        plan, review_source="review_rejected",
                        review=record), ensure_ascii=True)},
            )
            return None
        return None  # unreachable; loops return above

    def _revised_plan(self, conn, goal, plan, record):
        """One bounded revision: new planner call with the reviewer's
        concrete feedback. Returns the re-validated plan or None."""
        feedback = json.dumps({
            "verdict": record["verdict"],
            "findings": record["findings"],
            "missing_criteria": record["missing_criteria"],
            "recommended_changes": record["recommended_changes"],
        }, ensure_ascii=True)
        prompt = (planner.plan_prompt(goal["objective"], self.registry)
                  + "\nYour previous plan needs revision. The independent "
                  "reviewer returned this feedback "
                  "(fix every material item; return one corrected single "
                  "JSON plan now):\n" + feedback)
        try:
            reply = self._model_call(conn, prompt)
        except provider.ProviderError as exc:
            fresh = db.get_goal(conn, goal["id"])
            self._finish_failure(conn, fresh, exc.code,
                                 "revision planner call failed")
            return None
        revise_plan, err, _detail = planner.validate_plan(reply, self.registry)
        if revise_plan is None:
            fresh = db.get_goal(conn, goal["id"])
            self._finish_blocked(
                conn, fresh, "invalid_plan",
                f"revised plan rejected ({err})")
            return None
        return revise_plan

    @staticmethod
    def _step_truncated(step):
        try:
            return bool(json.loads(
                step["result_json"] or "{}").get("truncated"))
        except (ValueError, TypeError):
            return False

    # ------------------------------------------------------------------
    # restart recovery

    def recover(self):
        """Startup pass: infer work purely from persisted state.
        Uncertain non-idempotent outcomes are REPORTED for human
        reconciliation, never auto-repeated."""
        if not self._db_exists():
            return  # never create a database just to recover state
        conn = self._conn()
        try:
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name='goals'").fetchone()
            if table is None:
                return  # schema not initialized yet (init=False setups)
        finally:
            conn.close()
        conn = self._conn()
        try:
            for reachable in db.list_actionable_goals(conn):
                goal = db.get_goal(conn, reachable["id"])
                if goal is None or goalstate.is_terminal(goal["status"]):
                    continue
                stuck = False
                for step in db.get_goal_steps(conn, goal["id"]):
                    if step["status"] != "running":
                        continue
                    tool = self.registry.get(step["tool_name"])
                    if tool is None or not tool.idempotent:
                        stuck = True
                        break
                    # Idempotent read-only tool: safe to re-run once.
                    db.update_goal_step_status(
                        conn, goal["id"], step["seq"], "running", "pending")
                    db.insert_goal_event(
                        conn, goal["id"], "step_recovered",
                        f"seq {step['seq']} reset for one clean re-run")
                if stuck:
                    self._finish_blocked(
                        conn, goal, "reconciliation_required",
                        "an interrupted step's outcome is unknown")
                    continue
                # planning -> re-planned naturally on the next scan
                # (no durable artifact exists in that state);
                # ready/executing/verifying continue from persisted
                # step statuses.
        finally:
            conn.close()
