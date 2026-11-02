"""Read-only Obsidian vault integration (Phase 13A) — stdlib only.

The configured vault root is an explicit security boundary, resolved
per call with the existing precedence: environment (JARVIS_VAULT_PATH)
> persisted user configuration (settingsstore "vault_path") > unset.
Only the user (Settings Center / deployment config) can select it; it
is never a tool argument or model-controlled value.

Everything here is strictly read-only: no vault file is ever created,
modified, renamed, or deleted. The FTS5 index lives in the existing
JARVIS database (schema v8, additive migration) and stores only minimal
metadata — relative path, title, hash, size, mtime — plus the
searchable text. The index is NEVER an authority for permissions or
approvals; it only answers search queries.

Containment: every requested path is normalized and resolved; symlinks
that resolve outside the configured root are refused (never followed).
"""

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

import db

# Bounds (conservative, like the workspace tools).
MAX_VAULT_NOTES = 2000          # indexed note ceiling
MAX_NOTE_BYTES = 1_000_000      # per-note size cap at indexing
MAX_INDEX_TOTAL_BYTES = 50_000_000
MAX_DEPTH = 8                   # directory traversal depth
MAX_READ_CHARS = 20_000         # read result content cap (chars)
MAX_LIST_ENTRIES = 500
MAX_LIST_RESULT_BYTES = 48_000  # serialized list result cap
MAX_NAME_CHARS = 200
MAX_QUERY_CHARS = 200
MAX_SEARCH_RESULTS = 20
DEFAULT_SEARCH_RESULTS = 10
MAX_SNIPPET_CHARS = 240
MAX_PATH_SETTING_CHARS = 512

# Vault-specific exclusions on top of the generic sensitive-name rules.
# .obsidian is Obsidian's own configuration dir — never indexed.
_EXCLUDED_DIRS = frozenset({".obsidian", ".git", ".gnupg", ".ssh",
                            ".cache", ".trash", ".pki"})
_EXCLUDED_PREFIXES = (".env",)
_EXCLUDED_SUFFIXES = (".pem", ".key", ".keystore", ".kdbx", ".p12",
                      ".ovpn", ".htpasswd", ".netrc")
_EXCLUDED_SUBSTRINGS = ("credential", "credentials", "secret",
                        "password", "token", "id_rsa", "id_dsa",
                        "id_ecdsa", "id_ed25519", "creds", "api_key",
                        "api-key")

_MD_SUFFIXES = (".md",)

# Wikilink + Markdown-link extraction (for future graph work; stored
# raw per note, resolved only against the index — never fabricated).
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:[|#][^\]]*)?\]\]")
_MD_LINK_RE = re.compile(
    r"\[[^\[\]]*\]\(([^)\s]+)\)")


class VaultError(Exception):
    """Typed vault failure. `code` is a safe category for the API."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _contains(base, candidate):
    base_str = str(base)
    cand_str = str(candidate)
    return cand_str == base_str or cand_str.startswith(
        base_str.rstrip(os.sep) + os.sep)


def _is_excluded_component(name):
    lowered = name.lower()
    if lowered in _EXCLUDED_DIRS:
        return True
    if any(lowered.startswith(p) for p in _EXCLUDED_PREFIXES):
        return True
    if any(lowered.endswith(s) for s in _EXCLUDED_SUFFIXES):
        return True
    if any(s in lowered for s in _EXCLUDED_SUBSTRINGS):
        return True
    return False


def _is_eligible_name(name):
    """A name is an eligible note file: Markdown only, bounded length,
    not a sensitive/excluded name."""
    if not name or len(name) > MAX_NAME_CHARS:
        return False
    if not name.lower().endswith(_MD_SUFFIXES):
        return False
    return not _is_excluded_component(name)


def validate_root(raw):
    """Validate a user-provided vault path for configuration: absolute,
    existing, a real directory. Returns the RESOLVED path (the stored
    setting is the resolved root). Raises VaultError otherwise."""
    if not isinstance(raw, str):
        raise VaultError("invalid_path", "vault path must be a string")
    value = raw.strip()
    if not value:
        raise VaultError("invalid_path", "vault path is empty")
    if len(value) > MAX_PATH_SETTING_CHARS:
        raise VaultError(
            "invalid_path",
            f"vault path must be at most {MAX_PATH_SETTING_CHARS}"
            " characters")
    path = Path(value)
    if not path.is_absolute():
        raise VaultError(
            "invalid_path", "vault path must be an absolute path")
    try:
        resolved = path.resolve()
    except OSError:
        raise VaultError("invalid_path", "vault path cannot be resolved")
    if not resolved.exists():
        raise VaultError("no_such_path", "vault path does not exist")
    if not resolved.is_dir():
        raise VaultError("not_a_directory", "vault path is not a directory")
    return resolved


class VaultService:
    """Server-owned vault accessor. The root is resolved at every call
    so a settings change takes effect without a restart."""

    def __init__(self, db_path, environ=None, store=None):
        self.db_path = Path(db_path)
        self.environ = environ if environ is not None else os.environ
        self.store = store

    # ---------------- root resolution ----------------

    def root(self):
        """(resolved Path | None, source) with env > user store > none.
        Returns the resolved absolute root; a configured-but-missing
        directory still resolves (honest 'unavailable' state later)."""
        env_value = (self.environ.get("JARVIS_VAULT_PATH", "")
                     or "").strip()
        if env_value:
            return Path(env_value).resolve(), "environment"
        if self.store is not None:
            stored = (self.store.load().get("vault_path") or "").strip()
            if stored:
                return Path(stored).resolve(), "user_store"
        return None, ""

    # ---------------- traversal ----------------

    def _walk_notes(self, base):
        """Collect eligible notes as {rel_path: (size, mtime_ns)} with
        containment enforced on every component. Symlinks are resolved
        and refused if they escape; unreadable entries are skipped."""
        found = {}
        skipped = 0
        total_bytes = 0
        truncated = False
        stack = [(base, "")]
        while stack:
            current, relpath = stack.pop()
            depth = 0 if not relpath else relpath.count("/") + 1
            try:
                names = sorted(os.listdir(current))
            except OSError:
                skipped += 1  # unreadable dir: skipped, not fabricated
                continue
            for name in names:
                if len(name) > MAX_NAME_CHARS:
                    continue
                child = current / name
                rel_child = f"{relpath}/{name}" if relpath else name
                try:
                    real = child.resolve()
                except OSError:
                    skipped += 1
                    continue
                if not _contains(base, real):
                    continue  # symlink escape: never listed or followed
                if real.is_dir():
                    if _is_excluded_component(name):
                        continue  # .obsidian et al.: never traversed
                    if depth < MAX_DEPTH:
                        stack.append((real, rel_child))
                    else:
                        truncated = True  # depth bound honestly noted
                    continue
                if real.is_file() and _is_eligible_name(name):
                    if len(found) >= MAX_VAULT_NOTES:
                        truncated = True
                        break
                    try:
                        st = os.stat(real)
                    except OSError:
                        skipped += 1
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    found[rel_child] = (st.st_size, st.st_mtime_ns)
                    total_bytes += st.st_size
                    if total_bytes > MAX_INDEX_TOTAL_BYTES:
                        truncated = True
                        break
            if truncated:
                break
        return found, skipped, truncated

    @staticmethod
    def _note_title(text, stem):
        for line in text.splitlines()[:20]:
            line = line.strip()
            if line.startswith("# ") and len(line) > 2:
                return line[2:].strip()[:300]
        return stem[:300]

    @staticmethod
    def _note_links(text):
        """Raw link targets found in the note (wikilinks + relative
        Markdown links). Resolution happens later against the index —
        stored raw so a future graph phase never needs a re-read."""
        links = []
        seen = set()
        for match in _WIKILINK_RE.findall(text):
            target = match.strip()
            if target and target not in seen and len(target) <= 200:
                seen.add(target)
                links.append(target)
        for target in _MD_LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:",
                                  "#")):
                continue
            clean = target.split("#", 1)[0].strip()
            if clean and clean not in seen and len(clean) <= 200:
                seen.add(clean)
                links.append(clean)
        return links[:100]

    def _fts_upsert(self, conn, rel_path, title, body):
        conn.execute("DELETE FROM vault_fts WHERE path = ?", (rel_path,))
        conn.execute(
            "INSERT INTO vault_fts(path, title, body) VALUES (?,?,?)",
            (rel_path, title, body[:200_000]))

    def index_state(self, conn):
        row = conn.execute(
            "SELECT root, last_scan_at, status, notes, truncated,"
            " error_code FROM vault_index_state WHERE id = 1").fetchone()
        if row is None:
            return {"root": "", "last_scan_at": None, "status": "never",
                    "notes": 0, "truncated": False, "error_code": None}
        return {"root": row["root"] or "",
                "last_scan_at": row["last_scan_at"],
                "status": row["status"] or "never",
                "notes": int(row["notes"] or 0),
                "truncated": bool(row["truncated"]),
                "error_code": row["error_code"]}

    # ---------------- indexing ----------------

    def scan(self):
        """Incremental index refresh. Walks the configured root, reads
        only NEW/CHANGED notes (mtime_ns+size compare), removes stale
        entries, and records the single-row state. One transaction per
        refresh: a crash or error leaves the previous valid index
        intact. Returns a bounded summary dict."""
        root, _source = self.root()
        if root is None:
            raise VaultError("vault_not_configured",
                             "no vault is configured")
        base = root.resolve()
        if not base.is_dir():
            raise VaultError(
                "vault_unavailable",
                "configured vault directory is missing or unreadable")
        found, walk_skipped, truncated = self._walk_notes(base)
        conn = db.open_conn(self.db_path)
        try:
            existing = {row["rel_path"]: row for row in conn.execute(
                "SELECT rel_path, mtime_ns, size FROM vault_notes")}
            # Root changed → the whole index belongs to another vault:
            # wipe it and rebuild (full re-index policy).
            state = self.index_state(conn)
            root_changed = state["root"] not in ("", str(base))
            if root_changed:
                existing = {}
                with conn:
                    conn.execute("DELETE FROM vault_notes")
                    conn.execute("DELETE FROM vault_fts")
            removed = [rel for rel in existing if rel not in found]
            changed = []
            for rel, (size, mtime_ns) in found.items():
                old = existing.get(rel)
                if old is None or int(old["size"]) != size \
                        or int(old["mtime_ns"]) != mtime_ns:
                    changed.append(rel)
            skipped = {"unreadable": walk_skipped, "invalid_utf8": 0,
                       "oversized": 0, "read_error": 0}
            indexed = 0
            with conn:  # one transaction: failure preserves old index
                for rel in removed:
                    conn.execute("DELETE FROM vault_notes"
                                 " WHERE rel_path = ?", (rel,))
                    conn.execute("DELETE FROM vault_fts WHERE path = ?",
                                 (rel,))
                for rel in changed:
                    try:
                        data = (base / rel).read_bytes()
                    except OSError:
                        skipped["read_error"] += 1
                        continue  # previous entry (if any) is kept
                    if len(data) > MAX_NOTE_BYTES:
                        skipped["oversized"] += 1
                        continue
                    try:
                        text = data.decode("utf-8")  # strict: no faking
                    except UnicodeDecodeError:
                        skipped["invalid_utf8"] += 1
                        continue
                    sha = hashlib.sha256(data).hexdigest()
                    stem = rel.rsplit("/", 1)[-1][:-3]
                    title = self._note_title(text, stem)
                    links = json.dumps(self._note_links(text),
                                       ensure_ascii=True)
                    conn.execute(
                        "INSERT INTO vault_notes(rel_path, title,"
                        " mtime_ns, size, sha256, links)"
                        " VALUES (?,?,?,?,?,?)"
                        " ON CONFLICT(rel_path) DO UPDATE SET"
                        " title=excluded.title, mtime_ns=excluded.mtime_ns,"
                        " size=excluded.size, sha256=excluded.sha256,"
                        " links=excluded.links",
                        (rel, title, found[rel][1], len(data), sha, links))
                    self._fts_upsert(conn, rel, title, text)
                    indexed += 1
            notes = conn.execute(
                "SELECT COUNT(*) AS c FROM vault_notes").fetchone()["c"]
            status = "limited" if truncated else "ok"
            with conn:
                conn.execute(
                    "UPDATE vault_index_state SET root=?,"
                    " last_scan_at=?, status=?, notes=?, truncated=?,"
                    " error_code=NULL WHERE id = 1",
                    (str(base), now_iso(), status, notes,
                     1 if truncated else 0))
            return {"status": status, "indexed": indexed,
                    "updated": len(changed), "removed": len(removed),
                    "skipped": skipped, "notes": notes,
                    "truncated": truncated, "root_changed": root_changed}
        finally:
            conn.close()

    # ---------------- search ----------------

    @staticmethod
    def _fts_query(text):
        """User text → a safe FTS5 MATCH expression: every whitespace
        token is quoted (implicit AND), internal quotes doubled. No
        FTS5 operator syntax from user input survives."""
        tokens = [t for t in text.split() if t]
        return " ".join('"' + t.replace('"', '""') + '"'
                        for t in tokens)

    def search(self, query, limit=DEFAULT_SEARCH_RESULTS):
        """Lexical FTS5 search over the index. Results carry provenance
        (rel path, title, hash, staleness). The index must be current
        with the configured root; anything else is an honest error."""
        root, _source = self.root()
        if root is None:
            raise VaultError("vault_not_configured",
                             "no vault is configured")
        if not isinstance(query, str) or not query.strip():
            raise VaultError("invalid_query", "empty search query")
        if len(query) > MAX_QUERY_CHARS:
            raise VaultError("invalid_query",
                             f"query must be at most {MAX_QUERY_CHARS}"
                             " characters")
        limit = DEFAULT_SEARCH_RESULTS if limit is None else limit
        if not isinstance(limit, int) or isinstance(limit, bool) \
                or not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise VaultError("invalid_query",
                             f"limit must be 1..{MAX_SEARCH_RESULTS}")
        match = self._fts_query(query)
        if not match:
            return {"query": query, "results": [], "count": 0,
                    "truncated": False}
        base = root.resolve()
        conn = db.open_conn(self.db_path)
        try:
            try:
                rows = conn.execute(
                    "SELECT path, title,"
                    " snippet(vault_fts, 2, '[', ']', '…', 12) AS snip,"
                    " bm25(vault_fts) AS rank"
                    " FROM vault_fts WHERE vault_fts MATCH ?"
                    " ORDER BY rank LIMIT ?",
                    (match, limit)).fetchall()
            except db.sqlite3.OperationalError:
                raise VaultError("invalid_query",
                                 "query produced no safe search form")
            results = []
            for row in rows:
                rel = row["path"]
                stale = False
                try:
                    st = (base / rel).stat()
                    note = conn.execute(
                        "SELECT sha256, mtime_ns, size FROM vault_notes"
                        " WHERE rel_path = ?", (rel,)).fetchone()
                    if note is not None and (
                            st.st_mtime_ns != int(note["mtime_ns"])
                            or st.st_size != int(note["size"])):
                        stale = True
                except OSError:
                    stale = True  # gone or unreadable since indexing
                snippet = (row["snip"] or "")[:MAX_SNIPPET_CHARS]
                results.append({
                    "path": rel,
                    "title": row["title"] or rel.rsplit("/", 1)[-1],
                    "snippet": snippet,
                    "rank": round(float(row["rank"] or 0.0), 4),
                    "stale": stale,
                })
            return {"query": query, "results": results,
                    "count": len(results), "truncated": False}
        finally:
            conn.close()

    # ---------------- read ----------------

    def read_note(self, rel_path):
        """Read ONE note through strict containment. The path argument
        is vault-relative; absolute paths, traversal, and symlink
        escapes are refused. Returns the note text (bounded) with full
        citation metadata. Strictly read-only."""
        root, _source = self.root()
        if root is None:
            raise VaultError("vault_not_configured",
                             "no vault is configured")
        if not isinstance(rel_path, str):
            raise VaultError("invalid_path", "note path must be a string")
        rel = rel_path.strip().lstrip("/")
        if not rel or rel.startswith("~") or os.path.isabs(rel) \
                or ".." in rel.split("/") or "\\" in rel:
            raise VaultError("path_forbidden",
                             "note path must be a vault-relative path")
        parts = rel.split("/")
        if any(not p or len(p) > MAX_NAME_CHARS for p in parts) \
                or len(rel) > 400:
            raise VaultError("invalid_path", "note path is invalid")
        if not _is_eligible_name(parts[-1]):
            raise VaultError("not_a_note",
                             "only Markdown notes (.md) can be read")
        if any(_is_excluded_component(p) for p in parts):
            raise VaultError("path_forbidden",
                             "target matches an excluded name")
        base = root.resolve()
        target = (base / rel).resolve()
        if not _contains(base, target):
            raise VaultError("path_forbidden",
                             "note path escapes the configured vault")
        if not target.exists():
            raise VaultError("no_such_note", "no such note in the vault")
        try:
            data = target.read_bytes()
        except OSError:
            raise VaultError("unreadable_note",
                             "the note could not be read")
        try:
            text = data.decode("utf-8")  # strict: binary is never faked
        except UnicodeDecodeError:
            raise VaultError("binary_content",
                             "note is not valid UTF-8 Markdown")
        truncated = len(text) > MAX_READ_CHARS
        return {
            "path": rel,
            "title": self._note_title(text, rel.rsplit("/", 1)[-1][:-3]),
            "content": text[:MAX_READ_CHARS],
            "total_bytes": len(data),
            "content_chars": len(text[:MAX_READ_CHARS]),
            "max_chars": MAX_READ_CHARS,
            "sha256": hashlib.sha256(data).hexdigest(),
            "truncated": truncated,
            "links": self._note_links(text),
        }

    # ---------------- listing ----------------

    def list_notes(self, rel_prefix=""):
        """Live bounded listing of eligible notes (relative paths only).
        Containment and exclusions are identical to the indexer."""
        root, _source = self.root()
        if root is None:
            raise VaultError("vault_not_configured",
                             "no vault is configured")
        base = root.resolve()
        if not base.is_dir():
            raise VaultError("vault_unavailable",
                             "configured vault directory is missing")
        prefix = (rel_prefix or "").strip().strip("/")
        if prefix:
            if os.path.isabs(prefix) or ".." in prefix.split("/") \
                    or "\\" in prefix or len(prefix) > 400:
                raise VaultError("path_forbidden",
                                 "listing path must be vault-relative")
            target = (base / prefix).resolve()
            if not _contains(base, target):
                raise VaultError("path_forbidden",
                                 "listing path escapes the vault")
            if not target.is_dir():
                raise VaultError("not_a_directory",
                                 "listing path is not a folder")
        else:
            target = base
        entries = []
        truncated = False
        stack = [(target, prefix)]
        while stack:
            current, relpath = stack.pop()
            depth = 0 if not relpath else relpath.count("/") + 1
            try:
                names = sorted(os.listdir(current))
            except OSError:
                continue
            for name in names:
                if len(entries) >= MAX_LIST_ENTRIES:
                    truncated = True
                    break
                child = current / name
                rel_child = f"{relpath}/{name}" if relpath else name
                try:
                    real = child.resolve()
                except OSError:
                    continue
                if not _contains(base, real):
                    continue
                if real.is_dir():
                    if _is_excluded_component(name):
                        continue
                    if depth < MAX_DEPTH:
                        stack.append((real, rel_child))
                        entries.append({"path": rel_child, "type": "dir"})
                    continue
                if real.is_file() and _is_eligible_name(name):
                    entries.append({"path": rel_child, "type": "note"})
            if truncated:
                break
        body = json.dumps(entries, ensure_ascii=True).encode("utf-8")
        while len(body) > MAX_LIST_RESULT_BYTES and len(entries) > 1:
            entries = entries[:int(len(entries) * 0.8)]
            body = json.dumps(entries, ensure_ascii=True).encode("utf-8")
            truncated = True
        return {"entries": entries, "count": len(entries),
                "truncated": truncated}

    # ---------------- status ----------------

    def status(self):
        """Secret-free connection + index status for the API/UI. Never
        includes the absolute path or note contents."""
        root, source = self.root()
        if root is None:
            return {"configured": False, "source": "", "vault_name": None,
                    "vault_exists": None, "index": None}
        vault_exists = False
        try:
            vault_exists = root.is_dir()
        except OSError:
            vault_exists = False
        index = None
        try:
            conn = db.open_conn(self.db_path)
            try:
                index = self.index_state(conn)
                index["root_match"] = index["root"] in (
                    "", str(root.resolve()))
                index.pop("root", None)  # absolute path never leaves
            finally:
                conn.close()
        except db.sqlite3.Error:
            index = None
        return {"configured": True, "source": source,
                "vault_name": root.name, "vault_exists": vault_exists,
                "index": index}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
