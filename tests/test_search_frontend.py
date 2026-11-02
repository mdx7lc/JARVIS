"""Phase 5 frontend checks — source-level assertions (no DOM automation).

Search, notifications and preferences are verified by HTTP integration
on the backend plus source inspection here. A manual browser pass
remains outstanding (documented in docs/audit).
"""

import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLES_CSS = (STATIC / "styles.css").read_text(encoding="utf-8")


class TestRealSearchWiring(unittest.TestCase):
    def test_search_calls_backend(self):
        self.assertIn("fetch('/api/search?q=' + encodeURIComponent(query)",
                      APP_JS)

    def test_debounce_and_stale_guard(self):
        self.assertIn("SEARCH_DEBOUNCE_MS = 300", APP_JS)
        self.assertIn("let searchSeq = 0;", APP_JS)
        self.assertIn("if (seq !== searchSeq) return;", APP_JS)
        self.assertIn("searchAbort.abort()", APP_JS)

    def test_minimum_query_matches_server_validation(self):
        self.assertIn("SEARCH_MIN_SERVER_QUERY = 2", APP_JS)

    def test_results_rendered_with_textcontent_only(self):
        block = APP_JS.split("function renderSearchResults", 1)[1].split(
            "function initSearch", 1)[0]
        self.assertNotIn(".innerHTML +=", block)
        self.assertNotIn("insertAdjacentHTML", block)
        for field in ("innerHTML", "outerHTML"):
            self.assertNotIn(field, block.replace("box.innerHTML = ''; ", "")
                              .replace("box.innerHTML = '';", ""))
        for el in ("title", "snippet", "head"):
            self.assertIn(".textContent =", block)

    def test_result_actions_cover_all_types(self):
        block = APP_JS.split("async function openSearchResult", 1)[1].split(
            "function initSearch", 1)[0]
        self.assertIn("r.type === 'task'", block)
        self.assertIn("selectConversation", block)
        # the task branch re-reads the server list before jumping so the
        # timeline reflects the record even when the page state is stale
        self.assertIn("refreshTasks", block)

    def test_dropdown_safely_escapes_selector(self):
        self.assertIn("CSS.escape(r.id)", APP_JS)

    def test_html_contains_results_container(self):
        self.assertIn('id="search-results"', INDEX_HTML)
        self.assertIn("hidden", INDEX_HTML.split('id="search-results"')[1][:80])


class TestTruthfulNotifications(unittest.TestCase):
    def test_static_array_removed(self):
        self.assertNotIn("const NOTIFICATIONS = [", APP_JS)
        self.assertNotIn('Prototype scope reminder', APP_JS)

    def test_real_event_sources_hooked(self):
        self.assertIn("pushNotification('task-created', 'Task saved'",
                      APP_JS)
        self.assertIn("pushNotification('task-updated', 'Task status updated'",
                      APP_JS)
        self.assertIn("pushNotification('task-failed', 'Task update failed'",
                      APP_JS)

    def test_recurring_failures_deduped(self):
        self.assertIn("function pushNotificationOnce", APP_JS)
        self.assertIn("releaseNotifKey('tasks-error')", APP_JS)
        self.assertIn("releaseNotifKey('task-create-failed')", APP_JS)

    def test_badge_is_dynamic_not_static(self):
        btn = INDEX_HTML.split('id="notif-btn"')[1].split("</button>")[0]
        self.assertNotIn("has-dot", btn,
                         "badge must reflect real unread count")
        self.assertNotIn("has-dot\" id=\"notif-btn", INDEX_HTML)
        self.assertIn('id="notif-badge"', btn)
        self.assertIn("hidden", btn)
        self.assertIn("function updateNotifBadge", APP_JS)
        self.assertIn("badge.hidden = n === 0", APP_JS)

    def test_open_panel_is_the_read(self):
        block = APP_JS.split("function openNotifications", 1)[1].split(
            "function initPops", 1)[0]
        self.assertIn("state.unreadNotifs = 0;", block)

    def test_empty_state_is_truthful(self):
        self.assertIn("No session events yet", APP_JS)

    def test_session_scope_stated_in_ui(self):
        self.assertIn("Session only", INDEX_HTML)

    def test_clear_control_exists(self):
        self.assertIn('id="notif-clear"', INDEX_HTML)
        self.assertIn("function clearNotifications", APP_JS)

    def test_notif_styles_exist(self):
        self.assertIn(".notif-badge", STYLES_CSS)
        self.assertIn(".notif-item--warn", STYLES_CSS)
        self.assertIn(".notif-time", STYLES_CSS)


class TestLiveSearchApiContract(unittest.TestCase):
    """Backend assertions mirrored from /api/search contract."""

    def test_endpoint_exists(self):
        import server
        self.assertTrue(hasattr(server._Handler, "_handle_search"))
        self.assertLessEqual(server.SEARCH_SNIPPET_CHARS, 120)

    def test_like_pattern_is_literal(self):
        import db
        self.assertEqual(db.like_pattern("A_B"), "%a\\_b%")
        self.assertEqual(db.like_pattern("100%"), "%100\\%%")


class TestPreferencePersistence(unittest.TestCase):
    def test_single_versioned_key(self):
        self.assertIn("const PREF_KEY = 'jarvis.preferences.v1';", APP_JS)
        self.assertEqual(APP_JS.count("localStorage.setItem"), 1)
        self.assertEqual(APP_JS.count("localStorage.getItem"), 1)

    def test_values_validated_before_apply(self):
        self.assertIn("PREF_DENSITIES.includes(prefs.density)", APP_JS)
        self.assertIn("typeof prefs.focus === 'boolean'", APP_JS)
        self.assertIn("PREF_DAYS.includes(prefs.day)", APP_JS)

    def test_storage_failures_degrade_gracefully(self):
        self.assertIn("catch (err) { /* private mode / quota", APP_JS)

    def test_restored_before_first_render(self):
        init = APP_JS.index("function init() {")
        self.assertLess(APP_JS.index("loadPrefs();", init),
                        APP_JS.index("renderTimeline();", init))

    def test_saved_only_on_user_changes(self):
        self.assertIn("savePrefs({ density", APP_JS)
        self.assertIn("savePrefs({ focus", APP_JS)
        self.assertIn("savePrefs({ day:", APP_JS)


if __name__ == "__main__":
    unittest.main()
