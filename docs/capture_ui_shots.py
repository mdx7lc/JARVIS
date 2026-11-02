"""Capture operator screenshots of the Phase 14B UI for the final report.

Starts the real server against an isolated temp DB + demo skill, drives
the frozen static UI in headless Firefox, screenshots:
  - skills tab with a completed sandboxed run + run history
  - diagnostics view (improvements verify status + update installs)
  - permissions tab (audit feed)
Run only from the e2e venv (dev-only):
  /tmp/opencode/jarvis-e2e/venv/bin/python docs/capture_ui_shots.py
"""

import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
IMG = REPO / "docs" / "img"


def main():
    tmp = tempfile.mkdtemp(prefix="jarvis-shots-")
    skills_root = pathlib.Path(tmp) / "skills" / "greet"
    skills_root.mkdir(parents=True)
    (skills_root / "SKILL.md").write_text(
        "---\nname: greet\ndescription: writes a greeting file in the"
        " sandbox workspace\nversion: 1.0.0\nentrypoint: run.py\n"
        "timeout: 15\n---\ndemo skill for the operator report\n")
    (skills_root / "run.py").write_text(
        "import json, os\n"
        "args = json.loads(os.environ.get('SKILL_ARGS') or '{}')\n"
        "print('hello', args.get('name', 'operator'))\n")

    target = pathlib.Path(tmp) / "target"
    target.mkdir()

    db_path = pathlib.Path(tmp) / "jarvis.db"
    env = {
        "JARVIS_DB_PATH": str(db_path),
        "JARVIS_SKILLS_DIR": str(pathlib.Path(tmp) / "skills"),
        "JARVIS_UPDATE_TARGET": str(target),
    }
    os.environ.update(env)  # skills dirs (and update target) are read
    # at request time from the process environment, not from config.load
    cfg = config.load(environ=env)
    httpd = server.create_server("127.0.0.1", 0, db_path, cfg=cfg,
                                 settings_dir=tmp)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def api(path, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(base + path, data=data, method=method,
                                   headers={"Content-Type":
                                            "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError:
            return None, None

    db = sqlite3.connect(db_path)
    with db:
        db.execute("UPDATE skills SET state='enabled', trust='trusted'"
                   " WHERE id='greet'")
        for i in range(6):
            db.execute("INSERT INTO goals(id, objective, status) VALUES"
                       " (?, 'demo', 'blocked')", (f"shot-g{i}",))
        db.execute("INSERT INTO permission_audit(ts, source, tool_name,"
                   " level, decision, reason) VALUES (datetime('now'),"
                   " 'engine', 'skill.greet', 3, 'denied',"
                   " 'capability_cap_exceeded')")
    db.close()

    # one refused run (unknown skill) recorded honestly before the UI
    # flow; the approved run happens through the real UI (screenshot)
    api("/api/skills/run", "POST", {"id": "ghost"})
    api("/api/improvements/propose", "POST", {})
    pid = api("/api/improvements")[1]["proposals"][0]["id"]
    api("/api/improvements/decide", "POST", {"id": pid, "decision":
                                             "approve"})
    api("/api/improvements/decide", "POST", {"id": pid, "decision":
                                             "apply"})

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440,
                                            "height": 2400})
        page = ctx.new_page()
        page.goto(base, wait_until="networkidle")
        page.wait_for_timeout(600)

        # open Settings Center -> Skills tab via the real UI
        page.click("#settings-btn")
        page.wait_for_timeout(500)
        page.click("#settings-tabs button[data-tab='skills']")
        page.wait_for_timeout(1500)
        # real operator flow: Enable -> Set trusted -> run in sandbox
        page.click("#skills-list button[data-action='enable']")
        page.wait_for_timeout(700)
        page.click("#skills-list button[data-action='trust']")
        page.wait_for_timeout(900)
        rid = page.query_selector("#skill-run-id")
        if rid:
            rid.fill("greet")
            page.click("#skill-run-btn")
            page.wait_for_selector("#skill-runs .provider-item",
                                   timeout=15000)
        page.locator("#settings-skills-tab").screenshot(
            path=str(IMG / "phase14b_skills_runs.png"))

        page.click("#settings-tabs button[data-tab='permissions']")
        page.wait_for_timeout(1500)
        page.locator("#settings-permissions-tab").screenshot(
            path=str(IMG / "phase14b_permissions_tab.png"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)

        # Diagnostics view (with the improvement pending verification)
        page.click("#diag-btn")
        page.wait_for_timeout(1800)
        page.locator("#view-diagnostics").scroll_into_view_if_needed()
        page.locator("#view-diagnostics").screenshot(
            path=str(IMG / "phase14b_diagnostics_verify.png"))

        browser.close()

    httpd.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print("screenshots written to", IMG)


if __name__ == "__main__":
    main()
