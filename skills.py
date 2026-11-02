"""Local skills registry (read-only discovery + operator state).

Discovery scans operator-configured directories only (JARVIS_SKILLS_DIR
and ~/.config/jarvis/skills). A skill is a directory containing a
SKILL.md manifest with minimal front matter:

    ---
    name: my-skill
    description: one line about when this skill applies
    version: 1.0.0
    keywords: csv, import
    ---

Discovery (and the manifest read) NEVER executes skill code and NEVER
writes into the scanned directories — skill content is untrusted input,
exactly like any other file the permission engine allows JARVIS to read.
There is no download, no integrity resolution beyond reading the file,
and no execution path in this phase: a skill is reference material the
planner/operator can consult once a human enables it. Store state
(enabled/disabled/trust) persists in the skills table.
"""

import os
import re
from pathlib import Path

_MAX_SKILLS = 200
_MAX_MANIFEST_BYTES = 64 * 1024
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(\n|$)", re.DOTALL)
_FIELDS = {}


def default_dirs(environ=None):
    env = environ if environ is not None else os.environ
    dirs = []
    raw = env.get("JARVIS_SKILLS_DIR")
    if raw and raw.strip():
        dirs.append(Path(raw).expanduser())
    config_dir = env.get("XDG_CONFIG_HOME")
    if not config_dir:
        home = env.get("HOME")
        config_dir = str(Path(home) / ".config") if home else None
    if config_dir:
        dirs.append(Path(config_dir) / "jarvis" / "skills")
    # deduplicate, keep order
    seen, unique = set(), []
    for d in dirs:
        real = str(d.resolve(strict=False))
        if real not in seen:
            seen.add(real)
            unique.append(d)
    return unique


class SkillError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def parse_manifest(text):
    """Minimal front matter parser. Unknown fields ignored; no YAML
    dependency — flat `key: value` lines only."""
    match = _FRONTMATTER.match(text or "")
    if not match:
        raise SkillError("skill_manifest_missing",
                         "SKILL.md has no --- front matter block")
    fields = {}
    for line in match.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        value = value.strip('"').strip("'")
        if key and value:
            fields[key] = value
    name = fields.get("name") or ""
    description = fields.get("description") or ""
    if not name or len(name) > 120:
        raise SkillError("skill_manifest_invalid",
                         "manifest requires a short name field")
    if len(description) > 1000:
        raise SkillError("skill_manifest_invalid",
                         "description too long")
    return fields


def skill_id(directory, fields):
    name = fields.get("name", directory.name)
    slug = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-") or \
        directory.name
    return slug[:80]


def scan_dirs(directories):
    """Return parsed skill records found under the given roots. Never
    follows symlinked directories into unknown territory: a scanned
    entry must be a real directory whose parent is one of the roots."""
    found = []
    for root in directories:
        try:
            root_resolved = root.resolve(strict=False)
            if not root_resolved.is_dir():
                continue
            for entry in sorted(root_resolved.iterdir())[:_MAX_SKILLS]:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                for manifest_name in ("SKILL.md", "skill.md"):
                    manifest = entry / manifest_name
                    if manifest.is_file() and not manifest.is_symlink():
                        break
                else:
                    continue
                try:
                    stat = manifest.stat()
                    # A manifest can never be a path escape: we only
                    # read a fixed name inside the entry directory.
                    if stat.st_size > _MAX_MANIFEST_BYTES:
                        raise SkillError(
                            "skill_manifest_too_large",
                            "SKILL.md exceeds the 64 KiB manifest bound")
                    fields = parse_manifest(
                        manifest.read_text(encoding="utf-8",
                                           errors="replace"))
                except (OSError, ValueError, SkillError):
                    continue
                found.append({
                    "id": skill_id(entry, fields),
                    "name": fields.get("name", entry.name)[:120],
                    "source": str(entry),
                    "version": (fields.get("version") or None),
                    "description": fields.get("description") or None,
                    "keywords": fields.get("keywords") or None,
                })
        except (OSError, ValueError):
            continue
    return found


def sync_registry(conn, directories, now_iso):
    """Upsert the DB registry from a scan. Existing operator state
    (enabled/disabled/trust) survives re-scans; rows with manifests
    that vanished keep their row (honest: still listed, still says
    where it was installed from) — the source dir re-check is the
    caller's job at read time."""
    found = scan_dirs(directories)
    seen = set()
    for skill in found:
        seen.add(skill["id"])
        conn.execute(
            "INSERT INTO skills(id, name, source, version, description,"
            " keywords, state, trust, scanned_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'available', 'untrusted', ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "  name=excluded.name, source=excluded.source,"
            "  version=excluded.version,"
            "  description=excluded.description,"
            "  keywords=excluded.keywords,"
            "  scanned_at=excluded.scanned_at",
            (skill["id"], skill["name"], skill["source"],
             skill["version"], skill["description"], skill["keywords"],
             now_iso))
    return found


def set_state(conn, skill_id, state):
    if state not in ("enabled", "disabled"):
        raise SkillError("invalid_skill_state",
                         "state must be enabled or disabled")
    cur = conn.execute("UPDATE skills SET state = ? WHERE id = ?",
                       (state, skill_id))
    if cur.rowcount != 1:
        raise SkillError("unknown_skill", "skill is not in the registry")


def set_trust(conn, skill_id, trust):
    if trust not in ("trusted", "untrusted"):
        raise SkillError("invalid_trust", "trust must be trusted/untrusted")
    cur = conn.execute("UPDATE skills SET trust = ? WHERE id = ?",
                       (trust, skill_id))
    if cur.rowcount != 1:
        raise SkillError("unknown_skill", "skill is not in the registry")


def relevant_to(conn, words, limit=5):
    """Deterministic keyword scoring over description + keywords.
    Honest: a lexical match, not semantic understanding —
    recommendations explain why via the matched tokens."""
    tokens = [w for w in re.findall(r"[a-z0-9]+", (words or "").lower())
              if len(w) > 2][:12]
    rows = conn.execute(
        "SELECT id, name, description, keywords, state, trust"
        " FROM skills").fetchall()
    scored = []
    for row in rows:
        hay = ((row["description"] or "") + " "
               + (row["keywords"] or "")).lower()
        matched = [t for t in tokens if t in hay]
        if matched:
            scored.append({"id": row["id"], "name": row["name"],
                           "matched": matched,
                           "state": row["state"],
                           "trust": row["trust"],
                           "score": len(matched)})
    scored.sort(key=lambda s: (-s["score"], s["id"]))
    return scored[:limit]


def missing_ids(conn, objective):
    """Enabled candidates relevant to the objective that are still
    disabled — the honest 'missing capability' signal."""
    return [s for s in relevant_to(conn, objective)
            if s["state"] != "enabled"]
