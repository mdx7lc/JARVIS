"""Workflow B (Phase 6B) — task creation, status cycling, persistence
through the real server, verified in the browser."""

import sqlite3

from browser_harness import BrowserCase, SKIP_BROWSER

RAW = "el => el.textContent"


@SKIP_BROWSER
class TasksWorkflowTestCase(BrowserCase):
    def create_task(self, title="Draft launch checklist", day=None, time=None):
        if day:
            self.page.evaluate(
                "d => document.querySelector(`.seg-btn[data-day='${d}']`)"
                ".click()", day)
        self.page.evaluate("openModal('modal-task')")
        self.page.fill("#task-title-input", title)
        if time:
            self.page.select_option("#task-time", time)
        self.page.click("#task-form .btn-primary")
        self.page.wait_for_selector("#modal-task[open]", state="detached",
                                    timeout=15_000)

    def wait_timeline(self):
        self.page.wait_for_function(
            "() => { const t = document.getElementById('timeline');"
            " return t && t.querySelector('.tl-item'); }")

    def timeline_count(self):
        return self.page.evaluate(
            "document.querySelectorAll('#timeline .tl-item').length")

    def status_text(self):
        return self.page.evaluate(
            "document.querySelector('#timeline .tl-status').textContent")

    # ------------------------------------------------------- create
    def test_create_task_via_form_persists_to_server(self):
        self.create_task()
        self.wait_timeline()
        item = self.page.locator("#timeline .tl-item").first
        self.assertEqual("Draft launch checklist",
                         item.locator(".tl-title").evaluate(RAW))
        self.assertIn("14:00", item.locator(".tl-time").evaluate(RAW))
        stored = self.stack.tasks()[0]
        self.assertEqual("Draft launch checklist", stored["title"])
        self.assertEqual("queued", stored["status"])
        self.assertEqual("14:00", stored["time"])
        self.assertEqual("today", stored["day"])
        with sqlite3.connect(str(self.stack.db_path)) as conn:
            rows = conn.execute(
                "SELECT title, time_label, priority, day FROM tasks").fetchall()
        self.assertEqual([("Draft launch checklist", "14:00", "std", "today")],
                         rows)

    def test_created_task_persists_in_timeline(self):
        self.create_task()
        self.wait_timeline()
        # the timeline shows the saved server records; refresh and confirm
        # the first task survived the round trip, then the second
        self.page.evaluate("refreshTasks()")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#timeline .tl-item').length === 1")
        self.create_task(title="Second one")
        self.page.evaluate("refreshTasks()")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#timeline .tl-item').length === 2")

    def test_blank_title_is_rejected_by_the_form(self):
        self.page.evaluate("openModal('modal-task')")
        self.page.click("#task-form .btn-primary")
        self.assertTrue(self.page.locator("#modal-task[open]").count())
        self.assertEqual([], self.stack.tasks())

    # ------------------------------------------------------- cycle + fail path
    def test_status_cycle_persists_each_step(self):
        self.create_task(title="Cycle me")
        self.wait_timeline()
        self.page.click("#timeline .tl-status")
        self.page.wait_for_function(
            "() => document.querySelector('#timeline .tl-status')"
            ".textContent === 'High prio'", timeout=15_000)
        self.assertEqual("warn", self.stack.tasks()[0]["status"])
        self.page.wait_for_function(
            "() => document.getElementById('notif-list').textContent"
            ".includes('Task status updated')")
        self.page.click("#timeline .tl-status")
        self.page.wait_for_function(
            "() => document.querySelector('#timeline .tl-status')"
            ".textContent === 'Done'", timeout=15_000)
        self.assertEqual("done", self.stack.tasks()[0]["status"])

    def test_failed_status_write_reverts_with_feedback(self):
        self.create_task(title="Revert me")
        self.wait_timeline()
        self.page.route("**/api/tasks/**", lambda route: route.fulfill(
            status=500, content_type="application/json",
            body='{"error":{"message":"forced failure"}}'))
        self.page.click("#timeline .tl-status")
        self.page.wait_for_function(
            "() => document.getElementById('toast-box') &&"
            " document.getElementById('toast-box').textContent"
            ".includes('Could not save the task status')")
        self.assertEqual("Queued", self.status_text())
        self.page.wait_for_function(
            "() => document.getElementById('notif-list').textContent"
            ".includes('Task update failed')")
        self.page.unroute("**/api/tasks/**")
        self.assertEqual("queued", self.stack.tasks()[0]["status"])

    # ------------------------------------------------------- days + reload
    def test_day_toggle_and_day_scoping(self):
        self.create_task(title="Today only")
        self.wait_timeline()
        self.page.evaluate(
            "document.querySelector(\".seg-btn[data-day='tomorrow']\").click()")
        self.page.wait_for_function(
            "() => document.getElementById('timeline').textContent"
            ".includes('No tasks scheduled for tomorrow')")
        self.assertEqual(0, self.timeline_count())
        self.page.evaluate(
            "document.querySelector(\".seg-btn[data-day='today']\").click()")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#timeline .tl-item')"
            ".length === 1")
        self.assertEqual("Today only", self.page.evaluate(
            "document.querySelector('#timeline .tl-title').textContent"))

    def test_tasks_survive_reload_and_server_restart(self):
        self.create_task(title="Survivor")
        self.wait_timeline()
        self.reload()
        self.page.wait_for_function(
            "() => document.querySelectorAll('#timeline .tl-item')"
            ".length === 1")
        self.assertEqual("Survivor", self.page.evaluate(
            "document.querySelector('#timeline .tl-title').textContent"))
        self.stack.restart_server(self)
        self.page.goto(self.stack.base + "/")
        self.wait_ready()
        self.page.wait_for_function(
            "() => document.querySelectorAll('#timeline .tl-item')"
            ".length === 1")
        self.assertEqual("Survivor", self.page.evaluate(
            "document.querySelector('#timeline .tl-title').textContent"))
        with sqlite3.connect(str(self.stack.db_path)) as conn:
            self.assertEqual(["Survivor"],
                             [r[0] for r in conn.execute("SELECT title FROM tasks")])
