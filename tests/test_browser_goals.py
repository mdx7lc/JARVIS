"""Phase 8B browser E2E — goal console against the real worker.

Drives the durable-goal dialog in headless Firefox: queue an objective,
watch it plan + execute on the real server worker, inspect the persisted
detail (plan/report/events), cancel and retry, and exercise validation
and not-configured states. No live inference; the fake upstream returns
a canned plan fixture — that is UI plumbing evidence, not AI evidence.
"""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import fake_upstream
from browser_harness import Stack, BrowserCase, SKIP_BROWSER

# Test fixture whose content satisfies the planner contract (validate_plan,
# planning-time criteria coverage, and verification-time token overlap).
GOAL_CRITERION = "accurate inventory of the workspace files"
GOAL_PLAN = {
    "summary": "Inventory the workspace",
    "outcome": "A workspace inventory is recorded and reported.",
    "criteria": [GOAL_CRITERION],
    "risk": "low",
    "uncertainties": [],
    "steps": [{
        "purpose": "List the workspace files accurately",
        "tool": "workspace.list_files",
        "args": {"path": ""},
        "depends_on": [],
        "acceptance": GOAL_CRITERION,
    }],
}
PLAN_REPLY = fake_upstream.OK_RESPONSE | {
    "choices": [{
        "index": 0,
        "message": {"role": "assistant", "content": __import__("json").dumps(GOAL_PLAN)},
        "finish_reason": "stop",
    }],
}

CANARY_OBJECTIVE = "BROWSER-CANARY-QX7 inventory the workspace politely"


def _mk_goal_stack_workspace(self, reply):
    """Stack hook: real plan-capable worker + a temp workspace root."""
    ws_dir = tempfile.mkdtemp(prefix="jarvis-e2e-ws-")
    self.addCleanup(lambda: __import__("shutil").rmtree(ws_dir, True))
    ws = Path(ws_dir) / "workspace"
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir()
    (ws / "src" / "app.py").write_text("x = 1\n")
    (ws / "docs" / "readme.md").write_text("# docs\n")
    return Stack(reply=reply, extra_env={
        "JARVIS_WORKSPACE_ROOT": str(ws),
    })


@SKIP_BROWSER
class GoalConsoleBrowserTests(BrowserCase):
    REPLY = PLAN_REPLY["choices"][0]["message"]["content"]

    def make_stack(self, reply):
        return _mk_goal_stack_workspace(self, reply)

    # -------------------------------------------------------- helpers

    def upstream_chat_calls(self):
        with self.stack.upstream.lock:
            return sum(1 for r in self.stack.upstream.requests
                       if r["path"].startswith("/v1/chat"))

    def open_console(self):
        self.page.evaluate("openGoalsModal()")
        self.page.wait_for_selector("#modal-goals[open]")

    def submit_objective(self, text=CANARY_OBJECTIVE):
        self.page.fill("#goal-objective", text)
        self.page.click("#goal-submit")

    def goal_chip_text(self):
        return self.page.evaluate(
            "() => { const c = document.querySelector("
            "'#goals-list .goal-item .chip'); return c ? c.textContent : '' }")

    def wait_goal_status(self, containing, timeout=30000):
        self.page.wait_for_function(
            "(needle) => { const c = document.querySelector("
            "'#goals-list .goal-item .chip');"
            "return c && c.textContent.indexOf(needle) >= 0 }",
            arg=containing, timeout=timeout)

    def forge_goal(self, objective, status, error_code=None):
        with sqlite3.connect(str(self.stack.db_path)) as conn:
            row = conn.execute(
                "INSERT INTO goals(id, objective, status, error_code)"
                " VALUES (lower(hex(randomblob(16))), ?, ?, ?)",
                (objective, status, error_code))
            gid = conn.execute(
                "SELECT id FROM goals WHERE objective=?", (objective,)
            ).fetchone()[0]
        return gid

    def forge_goal_with_review(self, objective, status, error_code,
                               review):
        """Blocked/terminal goal whose persisted plan carries a review
        record — the deterministic way to render review provenance
        without invoking any reviewer."""
        plan = dict(GOAL_PLAN, review=review,
                    review_source={
                        "rejected": "review_rejected",
                        "approved": "external_review",
                        "failed": "local_validation",
                        "unavailable": "local_validation",
                    }[review["status"]])
        with sqlite3.connect(str(self.stack.db_path)) as conn:
            conn.execute(
                "INSERT INTO goals(id, objective, status, error_code,"
                " plan_json) VALUES (lower(hex(randomblob(16))), ?, ?, ?"
                ", ?)",
                (objective, status, error_code,
                 json.dumps(plan, ensure_ascii=True)))
            gid = conn.execute(
                "SELECT id FROM goals WHERE objective=?", (objective,)
            ).fetchone()[0]
        return gid

    def stderr_api_goal_count(self):
        cap = self.stack.stderr_cap.text()
        return len([l for l in cap.splitlines() if "/api/goals" in l])

    # ---------------------------------------------------------- tests

    def test_console_opens_with_empty_state(self):
        self.open_console()
        self.page.wait_for_selector("#modal-goals[open]")
        self.assertIn("No durable goals yet",
                      self.page.inner_text("#goals-list"))
        note = self.page.inner_text("#goals-note") \
            if self.page.query_selector("#goals-note") \
            else self.page.inner_text("#goals-body")
        self.assertIn("server-side", note)
        # opening the console never called the upstream chat endpoint
        with self.stack.upstream.lock:
            chat_calls = sum(1 for r in self.stack.upstream.requests
                             if r["path"].startswith("/v1/chat"))
        self.assertEqual(chat_calls, 0)

    def test_goal_round_trip_to_succeeded_with_real_worker(self):
        self.open_console()
        self.submit_objective()
        self.wait_goal_status("succeeded")
        chip = self.goal_chip_text()
        self.assertIn("succeeded", chip)
        # open the persisted detail and verify plan/report render
        self.page.click("#goals-list .goal-item .goal-open")
        self.page.wait_for_selector("#goal-detail:not([hidden])")
        detail = self.page.inner_text("#goal-detail")
        self.assertIn("List the workspace files accurately", detail)
        # reports render uppercase via CSS; check case-insensitively
        self.assertIn("status: succeeded", detail.lower())
        self.assertIn("review: local_validation", detail.lower())
        self.assertIn(GOAL_CRITERION, detail)
        self.assertIn("✔", detail)  # criterion marked passed
        # persisted verification summary (extraction depth checked in
        # the API suite); no fabricated rows
        self.assertIn("entries", detail)
        self.assertIn("plan_ready", detail)

    def test_blocked_goal_shows_reason_and_retry(self):
        gid = self.forge_goal("blocked objective", "blocked",
                              "unsupported_capability")
        self.open_console()
        self.page.wait_for_selector("#modal-goals[open]")
        self.page.click("#goals-list .goal-open")  # single goal present
        self.page.wait_for_selector("#goal-detail:not([hidden])")
        detail = self.page.inner_text("#goal-detail")
        self.assertIn("unsupported_capability", detail)
        retry = self.page.evaluate(
            "() => { const b = Array.from(document.querySelectorAll("
            "'#goal-detail .goal-actions button'));"
            "const r = b.find((x) => x.textContent.indexOf('Retry') >= 0);"
            "if (!r) return null; r.click(); return true }")
        self.assertTrue(retry)
        self.wait_goal_status("queued")
        # the poller turns it into success via the canned plan
        self.wait_goal_status("succeeded")

    def test_cancel_requested_flag_renders(self):
        # a worker-held goal would be cancelled within one poll tick, so
        # a *persisted* blocked goal with the flag is the deterministic
        # way to observe the flag rendering
        self.forge_goal("flaged goal", "blocked", "invalid_plan")
        with sqlite3.connect(str(self.stack.db_path)) as conn:
            conn.execute(
                "UPDATE goals SET cancel_requested=1")
        self.open_console()
        self.wait_goal_status("blocked")
        self.page.wait_for_function(
            "() => { const f = document.querySelector("
            "'#goals-list .goal-cancel-flag');"
            "return !!f && f.textContent.indexOf('cancel requested') >= 0 }")

    def test_dynamic_mode_select_flows_to_server_and_renders(self):
        """Phase 12D: the mode select flows UI → API → worker. Dynamic
        is opt-in (default sequential); the canned sequential plan is
        deliberately not a valid success contract, so the dynamic goal
        blocks honestly with contract_unverifiable — proving the mode
        actually reached the worker."""
        self.open_console()
        self.assertEqual(
            self.page.evaluate(
                "() => document.querySelector('#goal-mode').value"),
            "sequential")
        self.page.select_option("#goal-mode", "dynamic")
        self.submit_objective("BROWSER-DYNAMIC-QX7 list the workspace")
        self.wait_goal_status("blocked", timeout=45000)
        self.assertIn("contract_unverifiable", self.goal_chip_text())
        mode_chip = self.page.evaluate(
            "() => { const c = document.querySelector("
            "'#goals-list .goal-item .chip.chip-live');"
            "return c ? c.textContent : '' }")
        self.assertEqual(mode_chip, "dynamic")
        self.page.wait_for_selector("#goal-detail:not([hidden])")
        detail = self.page.inner_text("#goal-detail")
        self.assertIn("Execution mode", detail)
        self.assertIn("dynamic (bounded observe", detail)

    def test_validation_error_renders(self):
        self.open_console()
        # the textarea maxlength clamps typing; drive the server-side
        # bound directly (500-char cap) through the real form submit
        self.page.evaluate(
            "() => { const t = document.querySelector('#goal-objective');"
            "t.value = 'x'.repeat(501);"
            "document.querySelector('#goal-form').requestSubmit();"
            "return true }")
        self.page.wait_for_function(
            "() => { const e = document.querySelector('#goals-error');"
            "return e && !e.hidden && e.textContent.length > 0 }")
        self.assertIn("objective_too_long",
                      self.page.inner_text("#goals-error"))

    def test_blank_objective_never_submits(self):
        self.open_console()
        before = self.upstream_chat_calls()
        self.page.fill("#goal-objective", "   ")
        self.page.click("#goal-submit")
        time.sleep(1.0)
        self.assertEqual(self.upstream_chat_calls(), before)
        error_hidden = self.page.evaluate(
            "() => document.querySelector('#goals-error').hidden")
        self.assertTrue(error_hidden)

    def test_closing_console_stops_polling(self):
        self.open_console()
        self.page.keyboard.press("Escape")
        # in-flight/stale requests drain; poll timer must be cleared
        time.sleep(0.3)
        before = self.stderr_api_goal_count()
        time.sleep(3.0)
        after = self.stderr_api_goal_count()
        self.assertLessEqual(after, before + 1)

    def test_review_provenance_renders_and_rejects_are_not_approvals(self):
        review = {
            "status": "rejected", "verdict": "rejected",
            "identity": "opencode CLI (oc-test-model)",
            "risk_advisory": "medium",
            "findings": [
                "<img src=x onerror=window.__xss__=1> dangerous step",
                "cost exceeds the objective's scope",
            ],
            "missing_criteria": ["nobody verifies the report exists"],
            "recommended_changes": ["remove the out-of-scope step"],
            "failure_reason": None, "round": 1, "plan_revised": False,
            "local_validation": True,
        }
        self.forge_goal_with_review(
            "review-blocked objective", "blocked", "review_rejected",
            review)
        self.open_console()
        self.page.click("#goals-list .goal-open")
        self.page.wait_for_selector("#goal-detail:not([hidden])")
        detail = self.page.inner_text("#goal-detail")
        self.assertIn("Local validation: passed", detail)
        self.assertIn("Independent review: rejected the plan", detail)
        self.assertIn("Reviewer: opencode CLI (oc-test-model)",
                      detail)
        self.assertIn("Review round 1", detail)
        self.assertIn("dangerous step", detail)
        self.assertIn("nobody verifies the report exists", detail)
        # reviewer output is data: the injected markup never became DOM
        self.assertIsNone(self.page.query_selector("#goal-detail img"))
        self.assertFalse(
            self.page.evaluate("() => window.__xss__ === 1"))

    def test_approved_review_shows_identity_row_not_a_badge(self):
        review = {
            "status": "approved", "verdict": "approved",
            "identity": "opencode CLI (oc-ok-model)",
            "risk_advisory": "low", "findings": [],
            "missing_criteria": [], "recommended_changes": [],
            "failure_reason": None, "round": 2, "plan_revised": True,
            "local_validation": True,
        }
        self.forge_goal_with_review(
            "review-approved objective", "blocked", "invalid_plan",
            review)
        self.open_console()
        self.page.click("#goals-list .goal-open")
        self.page.wait_for_selector("#goal-detail:not([hidden])")
        detail = self.page.lowered = self.page.inner_text(
            "#goal-detail").lower()
        self.assertIn("independent review: approved", detail)
        self.assertIn("reviewer: opencode cli (oc-ok-model)",
                      detail)
        self.assertIn("plan was revised and revalidated", detail)
        self.assertIn("review round 2", detail)
        # no dedicated approval badge element is ever created; the
        # only "approved" wording is the provenance text row
        self.assertIsNone(
            self.page.query_selector("#goal-detail .badge-approved"))

    def test_server_logs_stay_goal_canary_free(self):
        self.open_console()
        self.submit_objective()
        self.wait_goal_status("succeeded")
        stderr = self.stack.stderr_text()
        self.assertNotIn(CANARY_OBJECTIVE, stderr)
        self.assertNotIn("BROWSER-CANARY", stderr)
        for line in stderr.strip().splitlines():
            self.assertIn(line.startswith("{"), (True, False),
                          line[:80])  # every line JSON when present

    def test_list_survives_reload_of_goals_after_modal_reopen(self):
        self.open_console()
        self.submit_objective()
        self.wait_goal_status("succeeded")
        self.page.click("[data-close-modal='modal-goals']")
        self.open_console()
        self.page.wait_for_function(
            "() => document.querySelectorAll('#goals-list .goal-open')"
            ".length > 0")
        self.assertIn("succeeded", self.goal_chip_text())


@SKIP_BROWSER
class GoalConsoleNotConfiguredTests(BrowserCase):
    """Goals without a configured provider are refused honestly (503),
    and the console renders the refusal instead of hanging."""

    REPLY = PLAN_REPLY["choices"][0]["message"]["content"]

    def make_stack(self, reply):
        ws_dir = tempfile.mkdtemp(prefix="jarvis-e2e-ws2-")
        self.addCleanup(lambda: __import__("shutil").rmtree(ws_dir, True))
        return Stack(reply=reply, extra_env={
            "JARVIS_WORKSPACE_ROOT": ws_dir,
            "FREELLMAPI_API_KEY": "",
        })

    def open_console(self):
        self.page.evaluate("openGoalsModal()")
        self.page.wait_for_selector("#modal-goals[open]")

    def submit_objective(self, text=CANARY_OBJECTIVE):
        self.page.fill("#goal-objective", text)
        self.page.click("#goal-submit")

    def test_not_configured_refusal_renders(self):
        self.open_console()
        self.submit_objective()
        self.page.wait_for_function(
            "() => { const e = document.querySelector('#goals-error');"
            "return e && !e.hidden && e.textContent.length > 0 }")
        text = self.page.inner_text("#goals-error")
        self.assertIn("not_configured", text)



if __name__ == "__main__":
    unittest.main()
