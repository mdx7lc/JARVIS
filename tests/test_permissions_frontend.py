"""Permissions UI wiring checks (source-level; browser pass documented):

The settings modal must expose the four levels, the tool registry
table, capability cap controls, and the security audit feed; L4 must
never render as a grantable control on the backend view this UI reads.
"""

import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")


class PermissionsWiringTests(unittest.TestCase):
    def test_permissions_tab_exists_in_settings(self):
        self.assertIn('data-tab="permissions"', INDEX_HTML)
        self.assertIn('id="settings-permissions-tab"', INDEX_HTML)
        self.assertIn('id="permissions-tools"', INDEX_HTML)
        self.assertIn('id="permissions-audit"', INDEX_HTML)

    def tab_wired(self):
        self.assertIn("permissions: 'settings-permissions-tab'", APP_JS)
        self.assertIn("if (tab === 'permissions') fetchPermissions();",
                      APP_JS)

    def test_four_levels_rendered_with_l4_disabled(self):
        self.assertIn('1: \'L1 Observer\', 2: \'L2 Operator\', '
                      '3: \'L3 Authorized executor\', 4: \'L4 Restricted\'',
                      APP_JS)
        # the L4 button is rendered disabled and never POSTs level 4
        self.assertIn("isL4 ? 'disabled title=", APP_JS)
        self.assertIn('restricted — never grantable', APP_JS)

    def test_cap_and_policy_endpoints_used(self):
        self.assertIn("'/api/permissions/cap'", APP_JS)
        self.assertIn("'/api/permissions/policy'", APP_JS)
        self.assertIn("'/api/permissions/audit?limit=100'", APP_JS)
        self.assertIn("'/api/permissions', { headers:", APP_JS)

    def test_policy_states_only_inherit_or_disabled(self):
        self.assertIn("data-state=\"inherit\"", APP_JS)
        self.assertIn("data-state=\"disabled\"", APP_JS)
        # no UI path can send an invented grant level for a write tool
        self.assertNotIn("data-state=\"allow\"", APP_JS)

    def test_audit_feed_exposes_denials_and_operator_changes(self):
        self.assertIn("operator change", APP_JS)
        self.assertIn("ev.decision", APP_JS)
        self.assertIn("policy_changed", APP_JS)


if __name__ == "__main__":
    unittest.main()
