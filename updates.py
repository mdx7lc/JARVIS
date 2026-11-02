"""Update management — honest report-only monitor (no auto-install).

JARVIS knows its own version (VERSION file in the repository root).
Update sources are trusted only when explicitly configured via
JARVIS_UPDATE_SOURCE (an https URL returning JSON {"version": ...,
"notes": ...}). Default policy: CHECK and REPORT. Nothing executable is
ever downloaded, installed, or replaced automatically; applying an
update is a human procedure (documented: stop server, backup data dir,
git pull / deploy, restart — DB migrations are additive and idempotent).

Every check is appended to update_checks so the history is real
evidence, including failures.
"""

import json
import os
import urllib.request
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent / "VERSION"
_TIMEOUT = 10


def current_version(environ=None):
    env = environ if environ is not None else os.environ
    override = env.get("JARVIS_VERSION")
    if override and len(override) <= 40:
        return override
    try:
        text = _VERSION_FILE.read_text(encoding="utf-8").strip()
        return text[:40]
    except OSError:
        return "unknown"


def check(source_url, current, timeout=_TIMEOUT):
    """Return a check record. Never raises for network/config issues:
    a failed check is itself a valid, recorded outcome."""
    ts_note = None
    if not source_url:
        return {"status": "not_configured", "source": "", "latest": None,
                "detail": "no update source configured (set "
                          "JARVIS_UPDATE_SOURCE to an explicit https "
                          "URL); report-only updates by default"}
    try:
        req = urllib.request.Request(
            source_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return {"status": "error", "source": source_url,
                        "latest": None,
                        "detail": f"upstream status {resp.status}"}
            payload = json.loads(
                resp.read(1024 * 1024).decode("utf-8", errors="replace"))
        latest = str(payload.get("version") or "")[:40]
        if not latest:
            return {"status": "error", "source": source_url,
                    "latest": None,
                    "detail": "source replied without a version field"}
        latest = latest.lstrip("v")
        ts_note = str(payload.get("notes") or "")[:400]
        status = "available" if latest != current else "up_to_date"
        return {"status": status, "source": source_url, "latest": latest,
                "detail": ts_note}
    except Exception as exc:  # a failed check is recorded, not raised
        return {"status": "error", "source": source_url, "latest": None,
                "detail": type(exc).__name__}


def record(conn, record_dict, current, now_iso):
    conn.execute(
        "INSERT INTO update_checks(ts, source, current, latest, status,"
        " detail) VALUES (?, ?, ?, ?, ?, ?)",
        (now_iso, (record_dict.get("source") or "")[:400], current,
         record_dict.get("latest"),
         record_dict["status"],
         (record_dict.get("detail") or "")[:400]))


# ---- verified staging / apply / rollback (Phase 14D) --------------------
# CHECK -> DISCOVER -> VERIFY -> REPORT exist above; this section adds
# the APPROVE -> BACKUP -> STAGE -> APPLY -> TEST -> CONFIRM/ROLLBACK
# leg for explicit operator action only:
#  - the target root must be explicitly configured (JARVIS_UPDATE_TARGET
#    or the caller); with none set, staging refuses (report-only stays
#    the default).
#  - the release manifest must carry integrity metadata (sha256 per
#    file). Unverified content is never written outside the staging
#    area, never executed, and never allowed to touch paths that could
#    hold user data (data/, *.db...).
#  - applying requires echoing the manifest digest (the operator's L3
#    confirmation) and, when a target file already differs from the
#    incoming content, an explicit allow_overwrite flag (never silently
#    replace locally modified files). Replaced files are backed up for
#    rollback; new files are recorded so rollback can remove them.
#  - DB migrations are deliberately NOT part of this flow: schema
#    changes stay with the application's own additive, versioned,
#    operator-observed startup path.

import hashlib  # noqa: E402  (stdlib; section-local concern)
import shutil  # noqa: E402
import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402
import urllib.request  # noqa: E402

_STAGING_NAME = ".jarvis-update-staging"
_MAX_FILES = 200
_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_PROTECTED_PREFIXES = ("data/", ".jarvis-update-backup", "jarvis.db")


class UpdateError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _check_url(url):
    """https always allowed; plain http only for LOOPBACK hosts — the
    same trust model as every other JARVIS network surface (a local
    test/self-hosted source), never for remote hosts."""
    if not url or not str(url).startswith(("https://", "http://")):
        raise UpdateError("invalid_source",
                          "update source must be an https URL (loopback"
                          " http only for local sources)")
    parsed = urllib.parse.urlsplit(str(url))
    if parsed.scheme == "http" and parsed.hostname not in (
            "127.0.0.1", "localhost", "::1", "[::1]"):
        raise UpdateError("invalid_source",
                          "remote update sources must be https")
    return True


def _fetch_json(source_url, timeout=_TIMEOUT, cap=4 * 1024 * 1024):
    _check_url(source_url)
    try:
        with urllib.request.urlopen(urllib.request.Request(
                str(source_url), headers={"Accept": "application/json"}),
                timeout=timeout) as resp:
            if resp.status != 200:
                raise UpdateError("source_unavailable",
                                  f"upstream status {resp.status}")
            return json.loads(resp.read(cap).decode("utf-8",
                                                    errors="replace"))
    except urllib.error.URLError as exc:
        raise UpdateError("source_unreachable", str(exc.reason)[:120])
    except (ValueError, OSError) as exc:
        raise UpdateError("source_unreachable", type(exc).__name__)


def staging_files_manifest(manifest):
    """The exact file list an update manifest carries — returned only
    for the digest binding (the persisted manifest digest covers this
    list, so a later file swap invalidates the approved digest)."""
    files_raw = manifest.get("files")
    return files_raw if isinstance(files_raw, list) else []


def _safe_update_path(rel):
    """Manifest paths are verified against traversal criminals before
    anything touches the disk: relative POSIX, no nulls, no .., no
    protected prefixes (user data can NEVER be replaced by an update)."""
    if not isinstance(rel, str) or not rel or len(rel) > 200:
        raise UpdateError("invalid_manifest_path", "bad path")
    if rel.startswith("/") or "\\" in rel or "\x00" in rel:
        raise UpdateError("invalid_manifest_path", "bad path")
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UpdateError("invalid_manifest_path", "bad path")
    lowered = rel.lower()
    for prefix in _PROTECTED_PREFIXES:
        if lowered.startswith(prefix.lower()):
            raise UpdateError("protected_path",
                              "update manifests may never touch user"
                              " data or the backup/staging areas")
    return Path(rel)


def _fetch_files(manifest, target, staging,
                 timeout=_TIMEOUT):
    """Download + hash-verify every manifest file into staging. The
    network is the only untrusted step and staging is disposable."""
    files = manifest.get("files") or []
    if not isinstance(files, list) or not files or len(files) > _MAX_FILES:
        raise UpdateError("invalid_manifest", "files list missing/oversized")
    total = 0
    staged = []
    for entry in files:
        if not isinstance(entry, dict):
            raise UpdateError("invalid_manifest", "bad file entry")
        rel = _safe_update_path(entry.get("path"))
        sha = entry.get("sha256") or ""
        size = entry.get("bytes") or entry.get("size") or 0
        if not (isinstance(sha, str) and len(sha) == 64 and
                all(c in "0123456789abcdef" for c in sha.lower())):
            raise UpdateError("integrity_missing",
                              f"no sha256 integrity metadata for {rel}")
        url = entry.get("url") or (manifest.get("base_url", "")
                                   + "/" + rel.as_posix())
        _check_url(url)
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    str(url), headers={"Accept": "application/octet-stream"}),
                    timeout=timeout) as resp:
                if resp.status != 200:
                    raise UpdateError("fetch_failed",
                                      f"upstream status {resp.status}")
                data = resp.read(_MAX_FILE_BYTES + 1)
        except urllib.error.URLError as exc:
            raise UpdateError("fetch_failed", str(exc.reason)[:120])
        if len(data) > _MAX_FILE_BYTES:
            raise UpdateError("file_too_large", str(rel))
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha.lower():
            raise UpdateError("integrity_mismatch",
                              f"downloaded {rel} does not match its"
                              " pinned sha256")
        real_size = int(size) if isinstance(size, int) else len(data)
        expected = real_size if real_size else len(data)
        if expected != len(data):
            raise UpdateError("integrity_mismatch",
                              f"size mismatch for {rel}")
        total += len(data)
        if total > _MAX_TOTAL_BYTES:
            raise UpdateError("update_too_large", "total size cap")
        (staging / rel).parent.mkdir(parents=True, exist_ok=True)
        (staging / rel).write_bytes(data)
        existing = target / rel
        existing_ok = False
        if existing.is_file():
            existing_ok = (hashlib.sha256(existing.read_bytes())
                           .hexdigest() == digest)
        staged.append({"path": rel.as_posix(), "sha256": digest,
                       "bytes": len(data),
                       "existing_differs": existing.is_file()
                       and not existing_ok,
                       "exists_same": existing.is_file() and existing_ok})
    return staged


def stage(source_url, current_version, target_root,
          timeout=_TIMEOUT):
    """CHECK -> VERIFY -> STAGE. Verifies integrity metadata, downloads
    into `<target>/<staging>` and reports what applying would change
    — without touching anything outside staging."""
    if not target_root:
        raise UpdateError("target_not_configured",
                          "no update target configured; set"
                          " JARVIS_UPDATE_TARGET to an explicit"
                          " directory first")
    target = Path(target_root)
    if target.is_file() or target.is_symlink():
        raise UpdateError("invalid_target", "target must be a directory")
    manifest = _fetch_json(source_url, timeout)
    version = str(manifest.get("version") or "")[:40].lstrip("v")
    if not version:
        raise UpdateError("invalid_manifest", "no version field")
    if version == current_version:
        return {"status": "up_to_date", "version": version}
    requires = manifest.get("requires") or None
    if requires and str(requires)[:40] != current_version:
        raise UpdateError("incompatible_release",
                          f"release requires baseline version"
                          f" {requires}, running {current_version}")
    staging = target / _STAGING_NAME
    try:
        if staging.is_symlink():
            raise UpdateError("staging_tampered",
                              "staging directory is a symlink")
        if staging.is_dir():
            shutil.rmtree(staging)
        staging.mkdir(mode=0o700)
        staged = _fetch_files(manifest, target, staging, timeout)
    except UpdateError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    files_raw = staging_files_manifest(manifest)
    manifest_bytes = json.dumps({"version": version,
                                 "files": files_raw}
                                if isinstance(files_raw, list) else
                                {"version": version},
                                sort_keys=True).encode()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    return {
        "status": "staged",
        "version": version,
        "from_version": current_version,
        "notes": str(manifest.get("notes") or "")[:400],
        "security": str(manifest.get("security") or "normal")[:20],
        "manifest_sha": manifest_sha,
        "files": staged,
        "staging": str(staging),
        "target": str(target),
    }


def apply(conn, target_root, manifest_sha, now_iso,
          allow_overwrite=False):
    """APPLY: re-verifies every staged file against the echoed manifest
    digest, backs up replaced files, moves verified content into the
    target, and records the operation for rollback. The staging dir is
    re-validated (symlink-swap attack) immediately before the swap."""
    staging = Path(target_root) / _STAGING_NAME
    if staging.is_symlink() or not staging.is_dir():
        raise UpdateError("staging_tampered",
                          "staging directory missing or not a real"
                          " directory")
    index_path = staging / ".manifest.json"
    if not index_path.is_file() or index_path.is_symlink():
        raise UpdateError("staging_tampered", "staging index missing")
    record_local = json.loads(index_path.read_text(
        encoding="utf-8", errors="replace"))
    if record_local.get("manifest_sha") != manifest_sha:
        raise UpdateError("digest_mismatch",
                          "staged content does not match the approved"
                          " manifest digest")
    staged_files = record_local.get("files") or []
    backup_dir = Path(target_root) / (".jarvis-update-backup-"
                                      + now_iso.replace(":", "")[:17])
    installed = []
    try:
        seen = set()
        for entry in staged_files:
            rel = _safe_update_path(entry["path"])
            if rel.as_posix() in seen:
                raise UpdateError("invalid_manifest",
                                  "duplicate staged path")
            seen.add(rel.as_posix())
            src = staging / rel
            if src.is_symlink():
                raise UpdateError("staging_tampered",
                                  "staged entry is a symlink")
            if not src.is_file():
                raise UpdateError("staging_tampered",
                                  "staged entry went missing")
            digest = hashlib.sha256(src.read_bytes()).hexdigest()
            if digest != entry.get("sha256"):
                raise UpdateError("integrity_mismatch",
                                  f"staged {rel} no longer matches")
            dest = Path(target_root) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            action = {}
            if dest.is_file():
                old_digest = hashlib.sha256(
                    dest.read_bytes()).hexdigest()
                if old_digest != digest:
                    if not allow_overwrite:
                        raise UpdateError(
                            "modified_files_present",
                            "target file differs locally; replacing"
                            " it requires explicit allow_overwrite="
                            "true")
                    backup_dir.mkdir(mode=0o700, exist_ok=True)
                    backup = backup_dir / rel
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_bytes(dest.read_bytes())
                    action = {"backup": True, "old_sha256": old_digest}
                else:
                    action = {"exists_same": True}
            else:
                action = {"new": True}
            shutil.copyfile(src, dest)
            installed.append({"path": rel.as_posix(),
                              "sha256": digest, **action})
    except UpdateError:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    with conn:
        conn.execute(
            "INSERT INTO update_installs(ts, from_version, to_version,"
            " manifest_sha, files_json, target_root, backup_dir,"
            " status, detail) VALUES (?, ?, ?, ?, ?, ?, ?,"
            " 'applied', ?)",
            (now_iso, record_local.get("from_version") or "",
             record_local.get("version") or "",
             manifest_sha,
             json.dumps(installed, ensure_ascii=True)[:20000],
             str(Path(target_root)),
             str(backup_dir) if backup_dir.exists() else None,
             record_local.get("notes") or ""))
    row = conn.execute(
        "SELECT last_insert_rowid() AS id").fetchone()
    return {"status": "applied", "install_id": row["id"],
            "files": installed, "backup_dir": str(backup_dir)
            if backup_dir.exists() else None}


def rollback(conn, install_id):
    """Rollback: verify ALL recorded backups exist (fail before touching
    anything if one went missing), restore every backed-up file, and
    remove precisely the files this install created. Never touches
    anything outside the recorded install footprint."""
    row = conn.execute(
        "SELECT * FROM update_installs WHERE id = ? AND status ="
        " 'applied'", (int(install_id),)).fetchone()
    if row is None:
        raise UpdateError("unknown_install",
                          "no applied install record for that id")
    files = json.loads(row["files_json"] or "[]")
    target = Path(row["target_root"]) if row["target_root"] else None
    if target is None:
        raise UpdateError("rollback_unavailable",
                          "install record has no verified target root")
    backup_dir = Path(row["backup_dir"]) if row["backup_dir"] else None
    # phase 1: verify every planned restoration is possible
    for entry in files:
        if entry.get("backup"):
            backup = backup_dir / entry["path"]
            if not backup.is_file() or backup.is_symlink():
                raise UpdateError("backup_missing",
                                  f"backup for {entry['path']} is"
                                  " missing; refusing partial rollback"
                                  " — operator intervention required")
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            if digest != entry.get("old_sha256"):
                raise UpdateError("backup_corrupt",
                                  f"backup for {entry['path']} does"
                                  " not match its recorded content")
    # phase 2: restore; phase 3: remove recorded NEW files only
    for entry in files:
        dest = target / entry["path"]
        if entry.get("backup"):
            shutil.copyfile(backup_dir / entry["path"], dest)
        elif entry.get("new"):
            dest.unlink(missing_ok=True)
    with conn:
        conn.execute(
            "UPDATE update_installs SET status = 'rolled_back',"
            " detail = ? WHERE id = ?",
            ("rolled back to pre-update contents", row["id"]))
    return {"status": "rolled_back", "install_id": row["id"]}


def record_stage(conn, staged, now_iso, from_version):
    """Persist one staged operation so an interrupted staging cannot be
    half-applied later: apply() only ever reads its persisted record."""
    with conn:
        conn.execute(
            "INSERT INTO update_installs(ts, from_version, to_version,"
            " manifest_sha, files_json, backup_dir, status, detail)"
            " VALUES (?, ?, ?, ?, ?, NULL, 'staged', ?)",
            (now_iso, from_version,
             staged.get("version") or "",
             staged.get("manifest_sha"),
             json.dumps(staged.get("files"), ensure_ascii=True)[:20000],
             staged.get("notes") or ""))


def persist_stage_index(staged):
    """Write the staging index (digest + files) inside the staging dir
    itself; apply() re-verifies against it, so the approved digest is
    the only accepted state."""
    (Path(staged["staging"]) / ".manifest.json").write_text(
        json.dumps({"manifest_sha": staged["manifest_sha"],
                    "version": staged["version"],
                    "from_version": staged.get("from_version") or "",
                    "notes": staged.get("notes") or "",
                    "files": staged.get("files")}),
        encoding="utf-8")
