"""Workflows D/E/F (Phase 6B) — notifications feed, session
preferences, live telemetry and the executive briefing, verified in the
browser through the real server API."""

from browser_harness import BrowserCase, SKIP_BROWSER

RAW = "el => el.textContent"


@SKIP_BROWSER
class NotificationsPrefsDashboardTestCase(BrowserCase):
    # ------------------------------------------------- notifications (D)
    def create_task(self, title):
        """Create through the real form so the page learns about it."""
        self.page.evaluate("openModal('modal-task')")
        self.page.fill("#task-title-input", title)
        self.page.click("#task-form .btn-primary")
        self.page.wait_for_selector("#modal-task[open]", state="detached")

    def notif_text(self):
        return self.page.evaluate(
            "() => document.getElementById('notif-list').textContent")

    def badge_text(self):
        return self.page.evaluate(
            "() => { const b = document.getElementById('notif-badge');"
            " return b && !b.hidden ? b.textContent : null; }")

    def test_task_save_produces_notifications_and_badge(self):
        self.page.evaluate("openModal('modal-task')")
        self.page.fill("#task-title-input", "Notify me")
        self.page.click("#task-form .btn-primary")
        self.page.wait_for_function(
            "() => document.getElementById('notif-list').textContent"
            ".includes('Task saved')")
        self.assertIn('"Notify me"', self.notif_text())
        self.assertEqual("1", self.badge_text())

    def test_opening_feed_marks_read_and_clear_empties(self):
        self.create_task("Read marker")
        self.page.wait_for_function(
            "() => document.getElementById('notif-badge')"
            " && document.getElementById('notif-badge').textContent === '1'")
        self.page.evaluate("openNotifications()")
        self.assertIsNone(self.badge_text())
        self.page.evaluate(
            "document.getElementById('notif-clear').click()")
        self.assertIn("No session events yet", self.notif_text())
        self.assertIsNone(self.badge_text())

    def test_failed_updates_land_in_feed_as_warnings(self):
        self.create_task("Failing updater")
        self.page.wait_for_selector("#timeline .tl-item")
        self.page.route("**/api/tasks/**", lambda route: route.fulfill(
            status=500, content_type="application/json",
            body='{"error":{"message":"boom"}}'))
        self.page.click("#timeline .tl-status")
        self.page.wait_for_function(
            "() => document.getElementById('notif-list').textContent"
            ".includes('Task update failed')")
        item = self.page.locator("#notif-list .notif-item--warn")
        self.assertNotEqual(0, item.count())

    # ------------------------------------------------- preferences (E)
    def test_day_choice_persists_across_reload(self):
        self.page.evaluate(
            "document.querySelector(\".seg-btn[data-day='tomorrow']\").click()")
        self.page.wait_for_function(
            "() => state.day === 'tomorrow'")
        self.reload()
        self.assertEqual("tomorrow", self.page.evaluate("state.day"))
        prefs = self.page.evaluate("localStorage.getItem('jarvis.preferences.v1')")
        self.assertIn("tomorrow", prefs)

    def test_density_choice_persists_independently(self):
        self.page.evaluate(
            "document.querySelector('.seg-btn[data-density=\\'compact\\']')"
            ".click()")
        self.page.wait_for_function(
            "() => localStorage.getItem('jarvis.preferences.v1')"
            ".includes('compact')")
        self.page.evaluate(
            "document.querySelector(\".seg-btn[data-day='tomorrow']\").click()")
        self.reload()
        self.assertEqual("tomorrow", self.page.evaluate("state.day"))
        self.assertEqual("compact",
                         self.page.evaluate(
                             "document.getElementById('app')"
                             ".dataset.density"))

    def test_invalid_saved_prefs_do_not_break_the_page(self):
        self.page.evaluate(
            "localStorage.setItem('jarvis.preferences.v1','{bogus json')")
        self.reload()
        self.wait_ready()
        # validated read falls back to defaults; page keeps working
        self.assertIn(self.page.evaluate(
            "document.getElementById('app').dataset.density"),
            ("cozy", None))
        self.create_task("Alive check")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#timeline .tl-item')"
            ".length === 1")

    # ------------------------------------------------- dashboard + briefing (F)
    def test_gauges_reach_live_values(self):
        # CPU needs two samples: the UI says so until the second answer
        self.page.wait_for_function(
            "() => { const n = document.querySelector('#gauges .gauge-note');"
            " return n && n.textContent === 'Live'; }", timeout=30_000)
        for key in ("cpu", "memory", "disk"):
            val = self.page.evaluate(
                f"() => gaugeRefs['{key}'].val.textContent")
            self.assertIn("%", val)
            num = int(val.split("%")[0])
            self.assertTrue(0 <= num <= 100, val)
        chip = self.page.evaluate(
            "() => { const c = document.getElementById('h-monitor-chip');"
            " return c ? c.textContent : ''; }")
        self.assertEqual("Live · local sampling", chip)

    def test_briefing_counts_saved_tasks(self):
        self.create_task("Briefing counter")
        self.page.evaluate("refreshTasks()")
        self.page.wait_for_function(
            "() => state.statusCounts && state.statusCounts.queued === 1")
        self.page.evaluate("renderBriefing()")
        self.assertIn("1 task(s) saved",
                      self.page.inner_text("#brief-body"))
        self.assertIn("Not AI-generated",
                      self.page.inner_text("#brief-body"))

    def test_status_line_names_model_policy(self):
        self.page.evaluate("renderBriefing()")
        self.assertIn("AI provider credentials present",
                      self.page.inner_text("#brief-body"))
        self.assertIn("database ready", self.page.inner_text("#brief-body"))
