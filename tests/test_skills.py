"""Skills registry tests: manifest parsing, scanning bounds, DB sync,
recommendation scoring, and the HTTP surface (read-only discovery with
operator enable/disable/trust state). No network, no execution."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
import skills


MANIFEST = """---
name: csv-reporter
description: build a markdown report from a CSV file in the workspace
version: 1.2.0
keywords: csv, report, table
---

Use to convert tabular CSV data into a markdown report.
"""


def write_skill(root, dirname, text=MANIFEST):
    d = root / dirname
    d.mkdir()
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d


class ManifestTests(unittest.TestCase):
    def test_parses_fields(self):
        fields = skills.parse_manifest(MANIFEST)
        self.assertEqual(fields["name"], "csv-reporter")
        self.assertEqual(fields["version"], "1.2.0")
        self.assertIn("CSV", fields["description"])

    def test_missing_frontmatter_refused(self):
        with self.assertRaises(skills.SkillError):
            skills.parse_manifest("no front matter here")

    def test_missing_name_refused(self):
        bad = "---\ndescription: x\n---\nbody"
        with self.assertRaises(skills.SkillError):
            skills.parse_manifest(bad)


class ScanTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="jarvis-skills-"))
        self.addCleanup(self._rmtree_helper, d)
        self.root = d / "skills"
        self.root.mkdir()
        self.other = d / "outside"
        self.other.mkdir()

    def _rmtree_helper(self, path=None):
        import shutil
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def test_scan_finds_manifests(self):
        write_skill(self.root, "csv-reporter")
        found = skills.scan_dirs([self.root])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "csv-reporter")

    def test_scan_ignores_files_and_missing_root(self):
        (self.root / "loose.md").write_text(MANIFEST)
        missing = skills.scan_dirs([self.root / "nope", self.other])
        self.assertEqual(missing, [])
        self.assertEqual(skills.scan_dirs([self.root]), [])

    def test_scan_never_follows_entry_symlinks(self):
        target = self.other / "escape"
        target.mkdir()
        (self.root / "sneaky").symlink_to(target)
        self.assertEqual(skills.scan_dirs([self.root]), [])

    def test_manifest_cap(self):
        write_skill(self.root, "big",
                    "---\nname: big\ndescription: x\n---\n"
                    + "y" * (64 * 1024 + 1))
        self.assertEqual(skills.scan_dirs([self.root]), [])


class RegistryTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix="jarvis-skills-db-"))
        self.addCleanup(self._rmtree_helper, d)
        self.db_path = d / "db.sqlite"
        db.init_db(self.db_path)
        self.root = d / "skills"
        self.root.mkdir()
        write_skill(self.root, "csv-reporter")

    def _rmtree_helper(self, path=None):
        import shutil
        shutil.rmtree(self.db_path.parent, ignore_errors=True)

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_sync_and_state_survives_rescan(self):
        conn = self._conn()
        try:
            with conn:
                found = skills.sync_registry(conn, [self.root], None)
                self.assertEqual(len(found), 1)
                skills.set_state(conn, "csv-reporter", "enabled")
                skills.sync_registry(conn, [self.root], None)
        finally:
            conn.close()
        row = self._conn().execute(
            "SELECT state, trust FROM skills WHERE id='csv-reporter'"
        ).fetchone()
        self.assertEqual(row["state"], "enabled")
        self.assertEqual(row["trust"], "untrusted")

    def test_state_and_trust_validation(self):
        conn = self._conn()
        try:
            with conn:
                skills.sync_registry(conn, [self.root], None)
                for action in (lambda c: skills.set_state(
                        c, "csv-reporter", "execute_everything"),
                        lambda c: skills.set_trust(
                            c, "csv-reporter", "maybe"),
                        lambda c: skills.set_state(c, "nope", "enabled")):
                    with self.assertRaises(skills.SkillError):
                        with conn:
                            action(conn)
        finally:
            conn.close()


class RecommendationTests(RegistryTests):
    def _rows(self):
        conn = self._conn()
        try:
            with conn:
                skills.sync_registry(conn, [self.root], None)
        finally:
            conn.close()
        return self._conn()

    def test_relevance_matches_description_and_keywords(self):
        conn = self._rows()
        try:
            scored = skills.relevant_to(conn, "prepare a csv report")
            self.assertTrue(scored)
            self.assertEqual(scored[0]["id"], "csv-reporter")
            self.assertIn("csv", scored[0]["matched"])
            # unrelated objective: no recommendation
            self.assertEqual(skills.relevant_to(conn, "refactor css"),
                             [])
        finally:
            conn.close()


try:
    from test_chat_api import ChatAPITestCase

    class SkillsAPITests(ChatAPITestCase):
        def _get(self, path):
            from test_models_api import request, as_json
            status, _h, body = request(self.base + path)
            return status, as_json(body)

        def _post(self, path, payload):
            from test_models_api import request, as_json
            status, _h, body = request(self.base + path,
                                       payload=payload, method="POST")
            return status, as_json(body)

        def _write_local_skill(self):
            d = Path(self._tmp.name) / "my-skills" / "csv-reporter"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(MANIFEST, encoding="utf-8")
            return d.parent

        def test_view_lists_strategy_and_discovery_dirs(self):
            skills_dir = self._write_local_skill()
            from unittest import mock
            with mock.patch.object(skills, "default_dirs",
                                   return_value=[skills_dir]):
                status, payload = self._get("/api/skills")
            self.assertEqual(status, 200)
            self.assertIn(payload["execution"], ("sandboxed",
                                                 "unavailable"))
            self.assertEqual([s["id"] for s in payload["skills"]],
                             ["csv-reporter"])

        def test_enable_disable_trust_flow(self):
            skills_dir = self._write_local_skill()
            from unittest import mock
            self._get("/api/skills")  # sync once with default dirs
            with mock.patch.object(skills, "default_dirs",
                                   return_value=[skills_dir]):
                self._get("/api/skills")
                status, _p = self._post("/api/skills",
                                        {"id": "csv-reporter",
                                         "action": "enable"})
                self.assertEqual(status, 200)
                status, payload = self._get("/api/skills")
                row = next(s for s in payload["skills"]
                           if s["id"] == "csv-reporter")
                self.assertEqual(row["state"], "enabled")
                status, _p = self._post("/api/skills",
                                        {"id": "csv-reporter",
                                         "action": "trust"})
                self.assertEqual(status, 200)
                status, payload = self._get("/api/skills")
                row = next(s for s in payload["skills"]
                           if s["id"] == "csv-reporter")
                self.assertEqual(row["trust"], "trusted")

        def test_unknown_skill_404(self):
            status, payload = self._post("/api/skills",
                                         {"id": "nope",
                                          "action": "enable"})
            self.assertEqual(status, 404)

        def test_invalid_action_400(self):
            status, payload = self._post("/api/skills",
                                         {"id": "x",
                                          "action": "grant_admin"})
            self.assertEqual(status, 400)
except ImportError:  # pragma: no cover
    SkillsAPITests = None


if __name__ == "__main__":
    unittest.main()
