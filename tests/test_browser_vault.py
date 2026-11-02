"""Phase 13A browser E2E — the Knowledge tab against a temporary vault.

Drives the real Settings Center Knowledge tab: honest unconfigured
state, configuring a temporary vault through the actual form, refresh
through the real API, and the truthful indexed-count status. No live
inference; no real vault; isolated temporary database.
"""

import tempfile
from pathlib import Path

from browser_harness import Stack, BrowserCase, SKIP_BROWSER

VAULT_MARKDOWN = """# Roadmap
Quarterly plan with milestones and owners.
"""


def make_vault(root):
    (root / "projects").mkdir(parents=True)
    (root / "projects" / "alpha.md").write_text(
        "# Alpha\nThe pricing strategy is value based.\n")
    (root / "Roadmap.md").write_text(VAULT_MARKDOWN)
    return root


class _VaultBrowserBase(BrowserCase):
    def make_stack(self, reply):
        return Stack(reply=reply)

    def open_knowledge_tab(self):
        self.page.click("#settings-btn")
        self.page.wait_for_selector("#modal-settings[open]")
        self.page.wait_for_function(
            "() => document.querySelectorAll('#provider-list"
            " .provider-item').length > 0")
        self.page.click("button[data-tab='knowledge']")
        self.page.wait_for_selector("#settings-knowledge-tab:not([hidden])")

    def vault_status(self):
        return self.page.evaluate(
            "() => document.querySelector('#settings-vault-status')"
            ".textContent")

    def error_contains(self, text):
        self.page.wait_for_function(
            "(needle) => { const e = document.querySelector("
            "'#settings-error'); return e && !e.hidden"
            " && e.textContent.includes(needle); }", arg=text)

    def wait_vault_status(self, needle):
        self.page.wait_for_function(
            "(needle) => { const e = document.querySelector("
            "'#settings-vault-status'); return e && e.textContent"
            ".indexOf(needle) >= 0 }", arg=needle)


@SKIP_BROWSER
class VaultBrowserTests(_VaultBrowserBase):
    def test_unconfigured_vault_is_honest(self):
        self.open_knowledge_tab()
        self.assertIn("Not configured", self.vault_status())

    def test_configure_refresh_and_truthful_status(self):
        self.open_knowledge_tab()
        tmp = tempfile.TemporaryDirectory(prefix="jarvis-vault-e2e-")
        self.addCleanup(tmp.cleanup)
        root = make_vault(Path(tmp.name) / "E2EVault")
        self.page.fill("#settings-vault-input", str(root))
        self.page.click("#settings-vault-save")
        self.wait_vault_status("not indexed yet")
        self.page.click("#settings-vault-refresh")
        self.wait_vault_status("2 note(s) indexed")
        self.assertIn("E2EVault", self.vault_status())
        # the absolute path never leaks into the page
        body = self.page.evaluate("() => document.body.innerText")
        self.assertNotIn(str(root), body)
        self.assertNotIn(tmp.name, body)

    def test_invalid_vault_shows_error_and_keeps_config(self):
        self.open_knowledge_tab()
        tmp = tempfile.TemporaryDirectory(prefix="jarvis-vault-e2e-")
        self.addCleanup(tmp.cleanup)
        root = make_vault(Path(tmp.name) / "KeepVault")
        self.page.fill("#settings-vault-input", str(root))
        self.page.click("#settings-vault-save")
        self.wait_vault_status("KeepVault")
        self.page.fill("#settings-vault-input",
                       str(Path(tmp.name) / "ghost"))
        self.page.click("#settings-vault-save")
        self.error_contains("does not exist")
        # previous configuration preserved after the failed update
        self.page.fill("#settings-vault-input", "")
        self.page.click("button[data-tab='knowledge']")
        self.wait_vault_status("KeepVault")


if __name__ == "__main__":
    import unittest
    unittest.main()
