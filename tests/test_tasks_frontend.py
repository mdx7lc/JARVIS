"""Phase 3D frontend task-integration checks — source-level assertions.

Browser automation is not available in this environment; these checks
verify the wiring exists and the misleading copy is gone. Rendering and
network behavior must still get a manual browser pass (documented).
"""

import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")


class TestTaskWiring(unittest.TestCase):
    def test_tasks_load_from_api_on_init(self):
        self.assertIn("/api/tasks?", APP_JS)  # list fetch with limit
        self.assertIn("async function refreshTasks", APP_JS)
        # init() must schedule the first load
        self.assertRegex(
            APP_JS, r"refreshSystemStatus\(\);\s*refreshConvBadge\(\);\s*refreshTasks\(\);"
        )

    def test_create_uses_post_api_tasks(self):
        self.assertRegex(
            APP_JS,
            r"fetch\('/api/tasks',\s*\{\s*method:\s*'POST'",
        )

    def test_status_cycle_uses_patch(self):
        self.assertRegex(
            APP_JS,
            r"fetch\(`/api/tasks/\$\{task\.id\}`\s*,\s*\{[^}]*method:\s*'PATCH'",
        )

    def test_optimistic_guard_and_revert_exist(self):
        self.assertIn("const seqSent = task.seq;", APP_JS)
        self.assertIn("if (task.seq !== seqSent) return;", APP_JS)
        self.assertIn("task.status = prev;", APP_JS)
        self.assertIn("Could not save the task status", APP_JS)

    def test_saving_pending_state_exists(self):
        self.assertIn("task.saving = true;", APP_JS)
        self.assertIn("'Saving…'", APP_JS)

    def test_duplicate_submit_guard(self):
        self.assertIn("if (submitBtn && submitBtn.disabled) return;", APP_JS)
        self.assertIn("submitBtn.disabled = true;", APP_JS)

    def test_no_demo_seed_tasks_left(self):
        for gone in ("Daily standup", "Review dashboard design",
                     "Test command interface", "Plan model integration"):
            self.assertNotIn(gone, APP_JS)

    def test_error_empty_loading_states(self):
        self.assertIn("Could not load tasks from the local server.", APP_JS)
        self.assertIn("No tasks yet. Create one with the “New task” button", APP_JS)
        self.assertIn("Loading tasks from the local server", APP_JS)
        self.assertIn("No tasks scheduled for tomorrow.", APP_JS)


class TestTaskCopy(unittest.TestCase):
    def test_no_reset_on_reload_claims(self):
        self.assertNotIn("page memory only", APP_JS)
        # no task surface claims data loss on reload anymore
        self.assertNotIn("resets on reload", INDEX_HTML.split("modal-task")[1])
        # the operator session note legitimately still resets (out of scope)
        self.assertIn("resets on reload", INDEX_HTML)

    def test_modal_note_truthful(self):
        self.assertIn(
            "Tasks are saved on the local JARVIS server and survive reloads",
            INDEX_HTML,
        )

    def test_success_toast_is_honest(self):
        self.assertIn("Task saved on the local server.", APP_JS)
        self.assertNotIn("Demo task added", APP_JS)

    def test_sections_copy_updated(self):
        # the demo "Intelligence feed" entries are gone; honest copy stays
        self.assertIn("Tasks are saved on the local JARVIS server and survive reloads", INDEX_HTML)
        self.assertIn("persist across reloads", APP_JS)

    def test_briefing_mentions_saved_tasks(self):
        # Phase 4: briefing reports the exact server-aggregated totals
        self.assertIn("task(s) saved", APP_JS)
        self.assertIn("exact totals from the server", APP_JS)
        self.assertNotIn("fictional demo entries today", APP_JS)


class TestToolsBadge(unittest.TestCase):
    def test_fabricated_badge_removed(self):
        self.assertNotIn('<span class="nav-badge">18</span>', INDEX_HTML)

    def test_tools_nav_keeps_neutral_label(self):
        self.assertIn('data-nav="tools"', INDEX_HTML)
        quote = INDEX_HTML.split('data-nav="tools"', 1)[1].split("</button>", 1)[0]
        self.assertNotIn('nav-badge', quote)
        self.assertNotIn(">18<", quote)

    def test_other_badges_still_present(self):
        # real data only: the conversations badge stays; the fabricated
        # task badge is gone (tasks live on Home, not as a nav count)
        self.assertIn('id="nav-conv-badge"', INDEX_HTML)
        self.assertNotIn('id="nav-task-badge"', INDEX_HTML)

    def test_conv_badge_starts_hidden(self):
        after = INDEX_HTML.split('id="nav-conv-badge"')[1][:30]
        self.assertIn("hidden", after)


if __name__ == "__main__":
    unittest.main()
