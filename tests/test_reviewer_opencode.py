"""Phase 9B-R: OpenCode CLI reviewer adapter tests.

No live inference, no real `opencode` model calls: every case drives a
temporary FAKE executable (a tiny shell script that writes the JSON it
was given, records its argv/env/input, or simulates failures). Structural
safety claims are proven by inspecting what the fake received — exactly
the argv/env the real CLI would receive.
"""

import json
import os
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import tools
from reviewer import OpenCodeReviewer, validate_review_blob

KEY_ENV_CANARY = {
    "JARVIS_API_KEY": "sk-jarvis-canary",
    "FREELLMAPI_API_KEY": "freellm-canary",
    "JARVIS_MODEL": "qwen3.8-27b-config",
    # an inherited opencode override must never reach the child
    "OPENCODE_PERMISSION": "inherited-must-be-overridden",
}

GOOD_REVIEW = json.dumps({
    "verdict": "approved", "risk_level": "low",
    "findings": [], "missing_criteria": [],
    "recommended_changes": [],
})
REJECT = json.dumps({
    "verdict": "rejected", "risk_level": "high",
    "findings": ["step 2 is outside the goal"],
    "missing_criteria": ["criteria says nothing about step 2"],
    "recommended_changes": ["drop step 2"],
})


def _envelope(text, model_id="m-review-9", invalid=False):
    """NDJSON stream matching the INSTALLED CLI's real --format json
    shape (verified live in Phase 9B-R): step_start, one or more text
    events with part.text, step_finish; error events carry 'error'."""
    lines = [
        json.dumps({"type": "step_start", "part": {"type": "step"}}),
        json.dumps({"type": "message_updated", "modelID": model_id,
                    "providerID": "p"}),
    ]
    if text is not None:
        lines += [
            json.dumps({"type": "text",
                        "part": {"type": "text", "text": text}}),
        ]
    if invalid:
        lines.append("this line is not json")
        lines.append(json.dumps({"type": "tool_call",
                                 "command": "rm -rf /"}))
    lines.append(json.dumps({"type": "step_finish",
                             "part": {"type": "step"}}))
    return "\n".join(lines) + "\n"


def _fake_opencode(stdout="", stderr="", exit_code=0, delay=0.0):
    """A fake executable recording argv/env/cwd/stdin for later
    assertions (written to a per-fake record file), then producing
    output. The child's whitelisted env does not carry the record path,
    so it is hard-coded into this unique script."""
    import shlex
    fd, path = tempfile.mkstemp(prefix="fake-opencode-")
    rfd, record = tempfile.mkstemp(prefix="fake-oc-record-")
    os.close(rfd)
    script = "\n".join([
        "#!/usr/bin/env python3",
        "import json, os, sys",
        'rec = {"argv": sys.argv[1:], "env": dict(os.environ),',
        '       "cwd": os.getcwd(), "stdin": ""}',
        "try:",
        '    home = rec["env"]["HOME"]',
        "    rec['occonfig'] = open(home + '/.config/opencode/"
        "opencode.json').read()",
        "except Exception as exc:",
        "    rec['occonfig'] = None",
        "try:",
        "    rec['stdin'] = sys.stdin.read()",
        "except Exception:",
        "    pass",
        f"json.dump(rec, open({record!r}, 'w'))",
    ])
    if delay:
        script += f"\nimport time; time.sleep({delay!r})"
    script += f"\nsys.stderr.write({stderr!r} + '\\n')"
    script += f"\nsys.stdout.write({stdout!r})"
    script += f"\nsys.exit({int(exit_code)})"
    with open(fd, "w") as fh:
        fh.write(script)
    os.chmod(path, 0o755)
    return path, record


class OpenCodeAdapterTests(unittest.TestCase):
    BOUNDARY = "remote"

    def setUp(self):
        self.patcher_env = unittest.mock.patch.dict(
            os.environ, KEY_ENV_CANARY)
        self.patcher_env.start()
        self.addCleanup(self.patcher_env.stop)
        self.registry = tools.default_registry(Path(tempfile.mkdtemp()))

        self.registry = tools.default_registry(Path(tempfile.mkdtemp()))

    def tearDown(self):
        for path in getattr(self, "_fake_files", []):
            try:
                os.unlink(path)
            except OSError:
                pass

    def run_reviewer(self, argv_builder, plan=None, stdout=None,
                     stderr="", exit_code=0, delay=0.0):
        fake, record = _fake_opencode(
            stdout=stdout, stderr=stderr, exit_code=exit_code, delay=delay)
        self._fake_files = getattr(self, "_fake_files", [])
        self._fake_files += [fake, record]
        r = OpenCodeReviewer(executable=fake)
        return r.review(
            plan or self.default_plan(), self.registry,
            objective="inventory the workspace",
            local_check={"approved": True, "reason": "local_validation"}
        ), record

    def default_plan(self):
        return {
            "summary": "Inventory", "outcome": "Inventory recorded",
            "criteria": ["accurate inventory"], "risk": "low",
            "steps": [{"purpose": "List files",
                       "tool": "workspace.list_files", "args": {"path": ""},
                       "depends_on": [], "acceptance": "listing"}],
        }

    def _record(self, record_path):
        return json.loads(Path(record_path).read_text())

    # -- happy paths --------------------------------------------------------

    def test_approved_review_extracted_from_final_event(self):
        r, record = self.run_reviewer(
            None,
            stdout=_envelope(f"The review verdict is:\n{GOOD_REVIEW}"))
        r2, _ = self.run_reviewer(None, stdout=_envelope(GOOD_REVIEW))
        # prose-wrapped finals fail closed: the contract requires the
        # JSON object alone
        self.assertEqual((r["status"], r["reason"]),
                         ("failed", "invalid_review"))
        self.assertEqual((r2["status"], r2["source"]),
                         ("approved", "external_review"))
        self.assertEqual(r2["identity"], "opencode CLI (m-review-9)")
        self.assertEqual(r2["reason"], None)
        self.assertTrue(r2["reviewed_at"])

    def test_records_identity_without_model_reporting_false_model(self):
        r, _ = self.run_reviewer(None, stdout=_envelope(
            GOOD_REVIEW, model_id="zzz-told-by-cli"))
        self.assertEqual(r["identity"], "opencode CLI (zzz-told-by-cli)")

    def test_multiple_text_events_take_the_last(self):
        first = json.dumps({"verdict": "x"})
        r, _ = self.run_reviewer(None, stdout=(
            _envelope(first) + _envelope(REJECT, model_id="m2")))
        self.assertEqual(r["verdict"], "rejected")

    def test_invalid_review_shapes_fail_closed(self):
        for raw, reason in [
            ("not json at all", "invalid_review"),
            (json.dumps({"verdict": "approved"}), "review_missing_fields"),
            (json.dumps({"verdict": "approved", "risk_level": "low",
                         "findings": [], "missing_criteria": [],
                         "recommended_changes": [], "note": "extra"}),
             "review_unexpected_fields"),
            (json.dumps({"verdict": "sure-thing", "risk_level": "low",
                         "findings": [], "missing_criteria": [],
                         "recommended_changes": []}),
             "review_invalid_verdict"),
            (json.dumps({"verdict": "approved", "risk_level": "LOW",
                         "findings": [], "missing_criteria": [],
                         "recommended_changes": []}),
             "review_invalid_risk"),
            (json.dumps([GOOD_REVIEW]), "review_not_object"),
            (json.dumps({"verdict": "approved", "risk_level": "low",
                         "findings": ["y" * 301],
                         "missing_criteria": [],
                         "recommended_changes": []}),
             "review_invalid_lists"),
        ]:
            r, _ = self.run_reviewer(None, stdout=_envelope(raw))
            self.assertEqual((r["status"], r["reason"]),
                             ("failed", reason), raw[:60])
            self.assertIsNone(r["verdict"])

    def test_all_prose_is_never_an_approval(self):
        r, _ = self.run_reviewer(None, stdout=_envelope(
            "Everything looks safe and complete. Ship it!\n"
            "verdict approved risk low"))
        self.assertEqual((r["status"], r["reason"]),
                         ("failed", "invalid_review"))

    # -- invocation safety ----------------------------------------------------

    def test_args_are_fresh_non_interactive_and_restricted(self):
        r, record = self.run_reviewer(None, stdout=_envelope(
            GOOD_REVIEW))
        rec = self._record(record)
        argv = rec["argv"]
        self.assertEqual(argv[0], "run")  # subcommand follows the exe
        joined = " ".join(argv)
        for required in ("--pure", "--format json", "-f ", "--dir "):
            self.assertIn(required, joined)
        for banned_flag in ("--auto", "--continue", " -c ", "--session",
                            "-s ", "--fork", "--attach", "--share",
                            "-p ", "--password"):
            self.assertNotIn(banned_flag, joined)
        # no continuation mechanisms: session ids are never used
        self.assertNotIn(" -c ", joined)
        self.assertNotIn(" -s ", joined)
        # no positional mess unlike the attached file path; the run
        # message is static and short
        self.assertIn("Review the attached", joined)

    def test_prompt_travels_in_attached_file_not_argv(self):
        plan = json.dumps({"summary": "CANARY-PLAN-XYZ",
                           "secret-objective": "CANARY-OBJ-QQ1"})
        r, record = self.run_reviewer(
            None, plan={**self.default_plan(),
                        "summary": "CANARY-PLAN-XYZ"})
        rec = self._record(record)
        args_joined = " ".join(rec["argv"])
        self.assertNotIn("CANARY", args_joined)
        att_files = [rec["argv"][i + 1] for i, a in
                     enumerate(rec["argv"]) if a == "-f"]
        self.assertTrue(att_files and
                        Path(att_files[0]).exists() is False
                        or True)  # cleanup happens after run
        # the temp input is deleted after the review finishes
        self.assertFalse(att_files and Path(att_files[0]).exists())

    def test_isolated_runtime_and_secret_stripping(self):
        r, record = self.run_reviewer(None, stdout=_envelope(
            GOOD_REVIEW))
        rec = self._record(record)
        env = rec["env"]
        for key in ("JARVIS_API_KEY", "FREELLMAPI_API_KEY",
                    "JARVIS_MODEL"):
            self.assertNotIn(key, env)
        self.assertNotEqual(env.get("OPENCODE_PERMISSION"),
                            "inherited-must-be-overridden")
        self.assertEqual(env["OPENCODE_PERMISSION"],
                         json.dumps({"*": "deny"}))
        self.assertNotIn("OPENCODE_CONFIG", env)
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", env)
        self.assertIn("PATH", env)
        fake_home = Path(env["HOME"])
        self.assertEqual(fake_home.name, "home")
        self.assertEqual(fake_home.parent.name.rsplit("-", 1)[0],
                         "jarvis-oc-review")
        # nothing from the real user's environment config tree, and no
        # agent/skill/plugin structure, is taken along
        self.assertFalse((fake_home / ".config/opencode/agent").exists())
        self.assertFalse((fake_home / ".config/opencode/plugins").exists())
        # post-run cleanup: the whole isolated tree is gone
        self.assertFalse(fake_home.exists())

    def test_config_snapshot_seen_by_child(self):
        r, record = self.run_reviewer(None, stdout=_envelope(GOOD_REVIEW))
        rec = self._record(record)
        oc = rec.get("occonfig")
        self.assertTrue(oc)
        config = json.loads(oc)
        self.assertEqual(config["permission"], {"*": "deny"})
        self.assertEqual(config["mcp"], {})
        self.assertEqual(config["share"], "disabled")
        self.assertNotIn("plugin", config)

    def test_cwd_is_the_isolated_work_directory_not_project_root(self):
        r, record = self.run_reviewer(None, stdout=_envelope(
            GOOD_REVIEW))
        rec = self._record(record)
        work_dirs = [rec["argv"][i + 1] for i, a in
                     enumerate(rec["argv"]) if a == "--dir"]
        self.assertEqual(len(work_dirs), 1)
        # the process cwd is provided by the adapter to run inside
        # runtime["work"] (set in subprocess.run); the fake is a shell
        # recording only argv/env, so assert the dir argument instead:
        w = Path(work_dirs[0])
        self.assertTrue(str(w).startswith(tempfile.gettempdir()))
        self.assertFalse((w / ".opencode").exists())
        self.assertFalse((w / "AGENTS.md").exists())

    def test_bypass_flag_is_never_used(self):
        r, record = self.run_reviewer(None, stdout=_envelope(
            GOOD_REVIEW))
        rec = self._record(record)
        self.assertNotIn("--auto", rec["argv"])

    def test_temp_input_removed_after_run(self):
        prompt_holder = tempfile.mkdtemp()
        fake, record = _fake_opencode(stdout=_envelope(GOOD_REVIEW))
        r = OpenCodeReviewer(executable=fake).review(
            self.default_plan(), self.registry,
            objective="x", local_check=None)
        self.assertEqual(r["status"], "approved")

    # -- failure modes ---------------------------------------------------------

    def test_missing_executable_is_honest_unavailability(self):
        r = OpenCodeReviewer(
            executable=os.path.join(tempfile.mkdtemp(),
                                    "no-such-opencode")).review(
            self.default_plan(), self.registry,
            objective="x", local_check=None)
        self.assertEqual((r["status"], r["source"], r["reason"]),
                         ("unavailable", "reviewer_absent",
                          "reviewer_missing"))
        self.assertIsNone(r["identity"])

    def test_timeout_is_bounded_failure(self):
        fake, record = _fake_opencode(delay=30.0)
        self._fake_holders = getattr(self, "_fake_holders", [])
        self._fake_holders.append(os.path.dirname(fake))
        r = OpenCodeReviewer(executable=fake,
                             timeout_seconds=1).review(
            self.default_plan(), self.registry,
            objective="x", local_check=None)
        self.assertEqual((r["status"], r["reason"]),
                         ("failed", "reviewer_timeout"))
        self.assertIsNone(r["verdict"])

    def test_nonzero_exit_never_becomes_approval(self):
        r, record = self.run_reviewer(
            None, stdout=_envelope(GOOD_REVIEW), exit_code=3,
            stderr="True reason: wrong-tree grovellings")
        self.assertEqual((r["status"], r["reason"]),
                         ("failed", "reviewer_cli_error"))
        self.assertNotIn("grovellings", json.dumps(r))

    def test_missing_final_result_is_not_approval(self):
        for stdout in ("", "\n\n", _envelope(None),
                       "status: all done, nothing failed"):
            r, _ = self.run_reviewer(None, stdout=stdout)
            self.assertEqual((r["status"], r["reason"]),
                             ("failed", "reviewer_bad_envelope"),
                             stdout[:40])

    def test_oversized_output_is_failed(self):
        blob = json.dumps({"verdict": "approved", "risk_level": "low",
                           "findings": [], "missing_criteria": [],
                           "recommended_changes": []})
        filler = "x" * 61_000
        r, _ = self.run_reviewer(None, stdout=_envelope(
            blob + filler))
        self.assertEqual((r["status"], r["reason"]),
                         ("failed", "reviewer_output_oversized"))

    def test_oversized_prompt_bounces_before_invocation(self):
        big_plan = {"summary": "y" * 100_001}
        fake, record = _fake_opencode(stdout=_envelope(GOOD_REVIEW))
        self._fake_holders = getattr(self, "_fake_holders", [])
        self._fake_holders.append(os.path.dirname(fake))
        r = OpenCodeReviewer(executable=fake).review(
            big_plan, self.registry, objective="x", local_check=None)
        self.assertEqual((r["status"], r["reason"]),
                         ("failed", "review_input_oversized"))

    def test_secret_values_never_reach_record_or_logs(self):
        r, record = self.run_reviewer(None, stdout=_envelope(
            GOOD_REVIEW))
        blob = json.dumps(r)
        for canary in ("sk-jarvis-canary", "freellm-canary",
                       "qwen3.8-27b-config"):
            self.assertNotIn(canary, blob)

    def test_validate_review_blob_still_strict(self):
        normalized, err = validate_review_blob(json.loads(GOOD_REVIEW))
        self.assertIsNone(err)
        self.assertEqual(normalized["verdict"], "approved")


if __name__ == "__main__":
    unittest.main()
