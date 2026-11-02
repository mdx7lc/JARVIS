"""Plan review (Phases 8B + 9B + 9B-R).

Two distinct layers, clearly recorded as different things:
1. reviewer.local_safety_check — deterministic re-validation that ALWAYS
   runs (structure, registry membership, permission, risk). This is
   machine validation, NOT independent AI review.
2. An external reviewer, separately invoked. Nothing is configured by
   default (`PLANNED_EXTERNAL_REVIEWER` stays None — operators opt in
   via JARVIS_REVIEWER); results record exactly what happened:
   'not_configured', 'external_review', 'review_failed'. A same-model
   self-critique is never labeled as independent review, and no approval
   is fabricated.

Phase 9B-R replaces the earlier Claude Code adapter with a real,
separately invoked OpenCode CLI reviewer: a fresh non-interactive
`opencode run` per review, in a fresh isolated temporary HOME (no
operator config, plugins, agents, skills, sessions, or instructions can
reach it), every tool action permission-DENIED, the bounded review
prompt attached as a restrictive-permission temp file (never argv),
prompts/logs carrying no secrets, and a tolerant NDJSON-event parser
whose final content must pass the strict review validator — a zero
exit code is never approval by itself.
"""

import datetime
import json
import os
import subprocess

PLANNED_EXTERNAL_REVIEWER = None  # no default reviewer; operators opt in

#: Verdicts and risk levels the external reviewer may report. Anything
#: else is invalid output, never silently coerced.
REVIEW_VERDICTS = ("approved", "revise", "rejected")
REVIEW_RISK_LEVELS = ("low", "medium", "high")

#: Bounded review-response shape (strict: unexpected keys are invalid).
REVIEW_ALLOWED_KEYS = (
    "verdict", "risk_level", "findings",
    "missing_criteria", "recommended_changes",
)
MAX_REVIEW_ITEMS = 10
MAX_REVIEW_ITEM_CHARS = 300


def local_safety_check(plan, registry):
    """Deterministic post-parse safety gate. Returns
    {"approved": bool, "reason": safe-code-or-None,
     "needs_external_review": bool}."""
    if not isinstance(plan, dict) or plan.get("risk") != "low":
        return {"approved": False, "reason": "risk_not_low",
                "needs_external_review": False}
    needs_external = False
    for step in plan.get("steps", []):
        tool = registry.get(step.get("tool"))
        if tool is None:
            return {"approved": False, "reason": "unsupported_capability",
                    "needs_external_review": needs_external}
        if tool.permission == "approval_required":
            # Phase 10A/10B: writes (and executable verification
            # recipes) always need a real external review plus the
            # digest-bound human approval.
            needs_external = True
        elif tool.permission not in ("autonomous", "verify_readonly"):
            # Unknown permission classes are refused, never approved.
            return {"approved": False, "reason": "permission_unsupported",
                    "needs_external_review": needs_external}
    if not plan.get("criteria"):
        return {"approved": False, "reason": "invalid_plan",
                "needs_external_review": needs_external}
    return {"approved": True, "reason": "local_validation",
            "needs_external_review": needs_external}


def _bounded_str_list(value, max_items=MAX_REVIEW_ITEMS,
                      max_chars=MAX_REVIEW_ITEM_CHARS):
    return (isinstance(value, list)
            and len(value) <= max_items
            and all(isinstance(x, str) and len(x) <= max_chars
                    for x in value))


def validate_review_blob(blob):
    """Strict validation of the reviewer's structured response.

    Returns (normalized_dict, None) or (None, safe_reason_code).
    Unknown extra keys, missing required fields, invalid verdict/risk
    values, and oversized lists/strings are all invalid — never coerced
    and never treated as approval.
    """
    if not isinstance(blob, dict):
        return None, "review_not_object"
    unexpected = set(blob) - set(REVIEW_ALLOWED_KEYS)
    if unexpected:
        return None, "review_unexpected_fields"
    for key in REVIEW_ALLOWED_KEYS:
        if key not in blob:
            return None, "review_missing_fields"
    verdict = blob["verdict"]
    if verdict not in REVIEW_VERDICTS:
        return None, "review_invalid_verdict"
    risk = blob["risk_level"]
    if risk not in REVIEW_RISK_LEVELS:
        return None, "review_invalid_risk"
    for key in ("findings", "missing_criteria", "recommended_changes"):
        if not _bounded_str_list(blob[key]):
            return None, "review_invalid_lists"
    return {
        "verdict": verdict,
        "risk_level": risk,
        "findings": blob["findings"],
        "missing_criteria": blob["missing_criteria"],
        "recommended_changes": blob["recommended_changes"],
    }, None


def _strip_fences(text):
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            lines = lines[1:]
            if lines and lines[-1].strip().endswith("```"):
                lines = lines[:-1]
            return "\n".join(lines).strip()
    return stripped


def _utc_now():
    return datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_reviewer(cfg):
    """Select the external reviewer from configuration (Phase 9B-R). Only
    an explicitly configured integration is returned; anything else
    keeps UnavailableReviewer, so review stays honestly unavailable."""
    if getattr(cfg, "reviewer", "") == "opencode":
        executable = getattr(cfg, "reviewer_path", "") or "opencode"
        return OpenCodeReviewer(
            executable=executable,
            model=getattr(cfg, "reviewer_model", "") or None,
        )
    return UnavailableReviewer()


class UnavailableReviewer:
    """Placeholder implementing the external-reviewer contract while no
    reviewer is configured: never approves, never executes anything."""

    def review(self, plan, registry, objective="", local_check=None):
        return {"status": "unavailable", "reason": "not_configured"}


class OpenCodeReviewer:
    """Independent review through a fresh, isolated OpenCode CLI run
    (Phase 9B-R).

    Verified against the INSTALLED binary (`opencode --version` → 1.19.0)
    and its documented surface — never assumed:

    - `opencode run --pure --format json`: fresh non-interactive run
      that emits a bounded NDJSON event stream (`--pure` also skips
      external plugin loading; under the isolated HOME below there are
      no plugins to load in the first place).
    - NO `-c/--session/--fork`, NO `--attach`: each review is a brand
      new session; the running interactive/session state of any other
      OpenCode process is never continued, forked, or attached to.
    - NO `--auto`: the auto-approval bypass documented by the CLI is
      never used — a bypass flag would be a policy hole.
    - Fresh isolated temporary HOME, created per review:
        HOME/.config/opencode/opencode.json  —
            {"permission": {"*": "deny"}, "mcp": {}, "share": "disabled"}
          (verified via `opencode debug config` under that HOME: the
          resolved config carries no plugins, no agents, no skills, no
          project instructions, and these permission/MCP/share values)
        HOME/.local/share/opencode/auth.json — a SYMLINK to the
          operator's own auth store so no credential content is ever
          copied, printed, or duplicated.
      The CLI's permissive "allow" defaults are thus fully overridden
      rather than relied on, and `OPENCODE_PERMISSION='{"*":"deny"}'`
      (documented "inlined json permissions config") adds a second deny
      layer.
    - Child env is a strict whitelist (PATH/LANG/TZ/TERM + the isolated
      HOME/TMPDIR + our explicit opencode settings) — every
      JARVIS_*/FREELLMAPI_* credential and every inherited OPENCODE_*
      override is stripped, so the reviewer can neither read JARVIS
      secrets nor override its own restrictions via the environment.
    - The bounded review prompt travels as a 0600 temp file attached
      with `-f`; it is never in argv, never in logs; the whole temp
      tree is removed in `finally`.
    - `--dir`: an empty dedicated work directory (no `.opencode`
      project config, no JARVIS-owned files) — the reviewer cannot
      discover unrelated project instructions or secrets, and the
      subprocess cwd is the same isolated directory.
    - Output parsing: tolerant NDJSON events; only a final assistant
      TEXT event is extracted as the review candidate; status, tool
      and reasoning events are never treated as a review. The final
      text must pass the existing strict validator — a zero exit code
      alone approves nothing.

    Every failure maps to a safe category — missing executable →
    unavailable/reviewer_missing; timeout, nonzero exit, oversized
    output, no final text, or invalid structure → failed/<safe-reason>.
    Nothing simulates approval.
    """

    REQUEST_MAX_CHARS = 100_000   # prompt budget (plans are validated small)
    OUTPUT_MAX_CHARS = 60_000     # oversized review -> failed, honest

    _MESSAGE = (
        "Review the attached plan-review request file and reply with "
        "only the one required JSON object."
    )

    def __init__(self, executable="opencode", model=None,
                 timeout_seconds=90):
        """`model` is the explicit "provider/model" id (from
        JARVIS_REVIEWER_MODEL). When unset the CLI uses its own default;
        a broken default surfaces as an honest failure, never approval."""
        self.executable = executable
        self.model = model
        self.timeout_seconds = timeout_seconds

    # -- isolated invocation surface -------------------------------------

    @staticmethod
    def _reviewer_config():
        return json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "permission": {"*": "deny"},
            "mcp": {},
            "share": "disabled",
        }, ensure_ascii=False)

    @staticmethod
    def _base_env_values():
        """Non-secret whitelist inherited from the parent process."""
        return {key: os.environ[key] for key in
                ("PATH", "LANG", "TZ", "TERM") if key in os.environ}

    @staticmethod
    def _prepare_runtime(parent_dir):
        """Create the per-review isolated runtime: fake HOME with the
        deny-all config, symlinked auth store, bare work dir, and the
        0600 attached input file. Returns (paths-dict, argv-side
        values). All contained in parent_dir; caller removes it."""
        import tempfile
        home = os.path.join(parent_dir, "home")
        work = os.path.join(parent_dir, "work")
        tmp = os.path.join(parent_dir, "tmp")
        for path in (
            home,
            os.path.join(home, ".config", "opencode"),
            os.path.join(home, ".local", "share", "opencode"),
            work,
            tmp,
        ):
            os.makedirs(path, exist_ok=True)
            os.chmod(path, 0o700)
        config_path = os.path.join(
            home, ".config", "opencode", "opencode.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(OpenCodeReviewer._reviewer_config())
        os.chmod(config_path, 0o600)
        auth_source = os.path.join(
            os.path.expanduser("~"), ".local", "share", "opencode",
            "auth.json")
        auth_link = os.path.join(
            home, ".local", "share", "opencode", "auth.json")
        if os.path.exists(auth_source):
            os.symlink(auth_source, auth_link)
        input_path = os.path.join(parent_dir, "review_input.txt")
        return {
            "home": home, "work": work, "tmp": tmp,
            "config": config_path, "auth_link": auth_link,
            "input": input_path, "auth_source": auth_source,
        }

    @staticmethod
    def child_env(runtime):
        """Strict-whitelist environment for the reviewer subprocess with
        the isolated HOME/TMPDIR and explicit restriction overrides.
        JARVIS_*/FREELLMAPI_* keys and inherited OPENCODE_* overrides
        can never appear here."""
        env = OpenCodeReviewer._base_env_values()
        env.update({
            "HOME": runtime["home"],
            "TMPDIR": runtime["tmp"],
            "OPENCODE_PERMISSION": json.dumps({"*": "deny"}),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            # no remote model-list fetches from the isolated review run
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
        })
        return env

    def argv(self, input_path, work_dir):
        """Fresh, non-interactive, restricted argument list. No
        continuation, no fork, no attach, no bypass, no sharing; the
        review content rides the attached file, never argv."""
        argv = [
            self.executable, "run",
            self._MESSAGE,
            "--pure",
            "--format", "json",
            "-f", input_path,
            "--dir", work_dir,
        ]
        if self.model:
            argv += ["-m", self.model]
        return argv

    # -- prompt ------------------------------------------------------------

    # Note: review_prompt is shared with the old adapter contract and
    # stays byte-compatible with the Phase 9B bounded input.

    @staticmethod
    def review_prompt(objective, plan, registry, local_check):
        """Bounded one-shot instruction set. Contains no secrets: the
        objective and plan are operator-entered/validated data."""
        return (
            "You are an INDEPENDENT external plan reviewer for the "
            "JARVIS orchestrator. This is a separate invocation from "
            "planning. Review the following proposed plan and return "
            "ONLY one JSON object — no prose, no markdown fences, nothing "
            "outside the JSON.\n\n"
            "Original goal: " + objective.strip() + "\n\n"
            "Registered tools, with their declared capabilities "
            "(unknown names cannot be executed):\n"
            + json.dumps(registry.descriptions(), ensure_ascii=True)
            + "\n\nDeterministic local validation already applied:\n"
            + json.dumps(local_check or {}, ensure_ascii=True)
            + "\n\nProposed plan under review:\n"
            + json.dumps(plan, ensure_ascii=True)
            + "\n\nEvaluate: does every step contribute to the original "
            "goal; are any goal requirements omitted; is each chosen "
            "tool appropriate; are the dependencies logical; can each "
            "claimed result actually be verified; does any step exceed "
            "the declared scope; do assumptions or invented facts "
            "materially affect execution; is there avoidable risk or "
            "unnecessary work.\n\n"
            "Required JSON shape (exactly these keys, no extras):\n"
            "{\n"
            '  "verdict": "approved" | "revise" | "rejected",\n'
            '  "risk_level": "low" | "medium" | "high",\n'
            '  "findings": ["<= 300 chars each"],\n'
            '  "missing_criteria": ["<= 300 chars each"],\n'
            '  "recommended_changes": ["<= 300 chars each, concrete '
            'edits for revise/rejected"]\n'
            "}\n"
            "Rules: verdict \"approved\" only when the plan fully and "
            "safely addresses the goal with the registered read-only "
            "tools; risk_level is YOUR assessment of the plan itself; "
            "do not execute anything; you have no tool authority."
        )

    # -- NDJSON event parsing ----------------------------------------------

    @staticmethod
    def _candidate_texts(obj):
        """Safe text candidates from one event; never trusts status or
        tool events as review content by themselves — everything that
        comes back must still pass validate_review_blob. Shape is the
        INSTALLED CLI's real `--format json` stream (verified live in
        Phase 9B-R): {"type": "text", "part": {"type": "text",
        "text": ...}, "sessionID", "timestamp"}; step_start/
        step_finish carry part dicts without text payloads."""
        texts = []
        if not isinstance(obj, dict):
            return texts
        kind = obj.get("type")
        part = obj.get("part")
        if kind == "text":
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
            if isinstance(obj.get("text"), str):
                texts.append(obj["text"])
        if kind in ("assistant", "message") and isinstance(
                obj.get("text"), str):
            texts.append(obj["text"])
        return texts

    @staticmethod
    def _model_from_events(events):
        """First verifiable model key found in the event stream (the
        CLI's own reporting). None when absent — never invented."""
        for obj in events:
            if not isinstance(obj, dict):
                continue
            for value in (obj, obj.get("part")):
                if not isinstance(value, dict):
                    continue
                for key in ("modelID", "model"):
                    if isinstance(value.get(key), str) \
                            and value[key].strip():
                        return value[key].strip()
        return None

    @staticmethod
    def _final_text(stdout):
        """Parse the bounded NDJSON event stream: returns
        (final_text_or_None, identity_model)."""
        events = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # tolerate non-JSON framing lines
        best = ""
        for obj in events:
            for text in OpenCodeReviewer._candidate_texts(obj):
                if isinstance(text, str) and text.strip():
                    best = text
        return (best.strip() or None), OpenCodeReviewer._model_from_events(
            events)

    # -- contract ----------------------------------------------------------

    def review(self, plan, registry, objective="", local_check=None):
        """Run one bounded review invocation. Returns the worker
        contract dict:

        {"status": "approved" | "revise" | "rejected" | "failed"
                   | "unavailable",
         "source": "external_review" | "review_failed" | "reviewer_absent",
         "identity": str | None, "verdict": ... , "risk_level": ...,
         "findings": [...], "missing_criteria": [...],
         "recommended_changes": [...], "reviewed_at": iso, "reason":
         safe-code | None}
        """
        prompt = self.review_prompt(objective, plan, registry, local_check)
        if len(prompt) > self.REQUEST_MAX_CHARS:
            return self._failed("review_input_oversized")
        import tempfile
        import shutil
        parent = tempfile.mkdtemp(prefix="jarvis-oc-review-")
        os.chmod(parent, 0o700)
        try:
            runtime = self._prepare_runtime(parent)
            input_path = runtime["input"]
            with open(input_path, "w", encoding="utf-8") as handle:
                handle.write(prompt)
            os.chmod(input_path, 0o600)
            try:
                completed = subprocess.run(
                    self.argv(input_path, runtime["work"]),
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    env=self.child_env(runtime),
                    cwd=runtime["work"],
                )
            except FileNotFoundError:
                return {"status": "unavailable",
                        "source": "reviewer_absent",
                        "reason": "reviewer_missing",
                        "identity": None, "reviewed_at": _utc_now()}
            except subprocess.TimeoutExpired:
                return self._failed("reviewer_timeout")
            if completed.returncode != 0:
                # Exit status only — stderr may hold untrusted CLI text
                # and is never copied into the record or logs.
                return self._failed("reviewer_cli_error")
            if len(completed.stdout) > self.OUTPUT_MAX_CHARS:
                return self._failed("reviewer_output_oversized")
            try:
                stdout = completed.stdout.decode("utf-8")
            except UnicodeDecodeError:
                return self._failed("reviewer_bad_envelope")
            final_text, model = self._final_text(stdout)
            if not final_text:
                # Zero exit code is NOT approval: no final assistant
                # text means no review happened.
                return self._failed("reviewer_bad_envelope")
            try:
                blob = json.loads(_strip_fences(final_text))
            except ValueError:
                return self._failed("invalid_review")
            normalized, reason = validate_review_blob(blob)
            if normalized is None:
                return self._failed(reason)
            return {
                "status": normalized["verdict"],
                "source": "external_review",
                "identity": self._identity(model),
                "verdict": normalized["verdict"],
                "risk_level": normalized["risk_level"],
                "findings": normalized["findings"],
                "missing_criteria": normalized["missing_criteria"],
                "recommended_changes": normalized["recommended_changes"],
                "reason": None,
                "reviewed_at": _utc_now(),
            }
        finally:
            # The isolated tree (fake HOME, auth symlink, work dir,
            # 0600 input file) is removed after every run.
            shutil.rmtree(parent, ignore_errors=True)

    @staticmethod
    def _identity(model):
        """Truthful identity: executable + the model id the CLI reported
        in its own events (if any). Never claims more than observed."""
        return "opencode CLI" + (f" ({model})" if model else "")

    @staticmethod
    def _failed(reason):
        return {"status": "failed", "source": "review_failed",
                "identity": None, "verdict": None, "risk_level": None,
                "findings": [], "missing_criteria": [],
                "recommended_changes": [],
                "reason": reason, "reviewed_at": _utc_now()}
