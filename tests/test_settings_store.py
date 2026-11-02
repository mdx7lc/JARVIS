"""Phase 11A settings store unit tests — file behavior and precedence.

Isolated temp directories only; never touches a real config dir.
"""

import json
import os
import stat
import tempfile
import unittest

import config
import settingsstore


class SettingsStoreUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="store-unit-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.environ = {}
        self.store = self.make_store()

    def make_store(self):
        return settingsstore.SettingsStore(self.dir, environ=self.environ)

    def test_empty_store_loads_defaults_effected(self):
        base = config.load(environ={
            "JARVIS_DB_PATH": self.dir + "/x.db"})
        effective = settingsstore.effective_config(base, self.store)
        self.assertEqual(effective.provider, "freellmapi")
        self.assertEqual(effective.timeout_seconds, 60)

    def test_save_fields_and_roundtrip(self):
        self.store.save_fields({"timeout_seconds": 42})
        self.assertEqual(self.store.load()["timeout_seconds"], 42)

    def test_save_validates_bounds(self):
        with self.assertRaises(settingsstore.SettingsError) as ctx:
            self.store.save_fields({"max_tokens": 0})
        self.assertEqual(ctx.exception.status, 400)

    def test_save_unknown_field_rejected(self):
        with self.assertRaises(settingsstore.SettingsError) as ctx:
            self.store.save_fields({"workspace_root": "/tmp"})
        self.assertEqual(ctx.exception.code, "unsupported_field")

    def test_env_locked_setting_write_refused(self):
        with self.assertRaises(settingsstore.SettingsError) as ctx:
            self.store.save_fields({"timeout_seconds": 42},
                                   env_managed=frozenset(
                                       {"timeout_seconds"}))
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "field_locked")

    def test_clear_env_managed_refused_but_user_ok(self):
        with self.assertRaises(settingsstore.SettingsError):
            self.store.clear_setting("max_tokens",
                                     env_managed=frozenset({"max_tokens"}))
        self.store.save_fields({"reviewer": "opencode"})
        self.store.clear_setting("reviewer", env_managed=frozenset())

    def test_env_precedence_beats_store(self):
        self.environ["JARVIS_TIMEOUT_SECONDS"] = "77"
        base = config.load(environ=self.environ)
        self.store.save_fields({"timeout_seconds": 42})
        effective = settingsstore.effective_config(base, self.store)
        self.assertEqual(effective.timeout_seconds, 77)
        self.assertIn("timeout_seconds", base.env_managed)

    def test_user_store_beats_default_not_env(self):
        base = config.load(environ={})
        self.store.save_fields({"timeout_seconds": 42})
        effective = settingsstore.effective_config(base, self.store)
        self.assertEqual(effective.timeout_seconds, 42)

    def test_credential_roundtrip_is_private(self):
        self.store.save_key("orcarouter", "sk-store-canary")
        # file permissions restrictive
        mode = stat.S_IMODE(os.stat(self.store.credentials_path).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(self.store.stored_key("orcarouter"),
                         "sk-store-canary")
        self.assertEqual(self.store.resolve_key_source(
            "orcarouter", config.load(environ={})), "user_store")
        # cleared → gone
        self.store.clear_key("orcarouter")
        self.assertEqual(self.store.stored_key("orcarouter"), "")

    def test_corrupt_files_load_as_empty(self):
        for name in ("settings.json", "credentials.json"):
            with open(f"{self.dir}/{name}", "w", encoding="utf-8") as fh:
                fh.write("{not json")
        self.assertEqual(self.store.load(), {})
        self.assertEqual(self.store.stored_key("orcarouter"), "")

    def test_invalid_entries_are_dropped_not_trusted(self):
        with open(self.store.settings_path, "w", encoding="utf-8") as fh:
            json.dump({"settings": {"timeout_seconds": "banana",
                                    "max_tokens": 128}},
                      fh)
        loaded = self.store.load()
        self.assertEqual(loaded.get("timeout_seconds"), None)
        self.assertEqual(loaded.get("max_tokens"), 128)

    def test_unknown_stored_keys_drop(self):
        with open(self.store.settings_path, "w", encoding="utf-8") as fh:
            json.dump({"settings": {"evil_key": "<script>"}},
                      fh)
        self.assertEqual(self.store.load(), {})


if __name__ == "__main__":
    unittest.main()
