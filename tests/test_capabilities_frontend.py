"""Source-level wiring checks for Skills/Updates/Improvement UI."""

import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")


class CapabilitiesWiringTests(unittest.TestCase):
    def test_skills_tab_and_discovery_copy(self):
        self.assertIn('data-tab="skills"', INDEX_HTML)
        self.assertIn('id="settings-skills-tab"', INDEX_HTML)
        self.assertIn('id="skills-list"', INDEX_HTML)
        # honest copy: execution only through the sandboxed operator form
        self.assertIn("bubblewrap sandbox", INDEX_HTML.replace(
            "\n", " "))

    def test_diagnostics_hosts_updates_and_improvements(self):
        self.assertIn('id="diag-updates"', INDEX_HTML)
        self.assertIn('id="diag-improvements"', INDEX_HTML)
        self.assertIn('id="updates-check-btn"', INDEX_HTML)
        self.assertIn('id="improvements-scan-btn"', INDEX_HTML)

    def test_real_endpoints_used_without_fabrication(self):
        for endpoint in ("/api/updates", "/api/improvements",
                         "/api/updates/check", "/api/improvements/propose"):
            self.assertIn(endpoint, APP_JS)
        self.assertIn("No update source configured", APP_JS)

    def test_improvement_copy_is_honest(self):
        # No claim of automatic learning; operator decides via buttons.
        self.assertIn("you decide, and approved proposals become"
                      " follow-up tasks", INDEX_HTML)
        self.assertNotIn("self-learning engine", APP_JS
                         + INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
