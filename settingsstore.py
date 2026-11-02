"""JARVIS persisted user configuration (Phase 11A) — stdlib only.

A narrowly scoped, per-user configuration store OUTSIDE the repository:
<config_dir>/settings.json holds user-chosen values, <config_dir>/
credentials.json holds provider API keys. Both are written atomically
(temp file + fsync + os.replace) with restrictive permissions
(0700 directory, 0600 files). No custom encryption is attempted — at
rest the store is protected by filesystem permissions only, which is
documented honestly in the architecture notes.

Precedence everywhere (each step wins over the later ones):
  1. explicit deployment configuration (environment variable set)
  2. persisted user configuration (this module)
  3. application defaults

The API key is never returned by any read API: `credentials.json` is
opened only by exact write/read-key paths inside this module, is never
logged, and is never echoed — only per-profile boolean presence leaves
this module.

Neither SQLite nor any tracked file ever holds a credential.
"""

import json
import os
import stat
import tempfile
import threading
import time
from pathlib import Path

import provider

SETTINGS_VERSION = 1

#: persisted user settings schema: value, validator, env key reference.
#: Reviewer model stays a bounded free string ("provider/model" id);
#: everything numeric uses exact existing bounds.
BOUNDS = {
    "timeout_seconds": (1, 300),
    "max_tokens": (1, 4096),
    "history_messages": (1, 100),
}

PROVIDERS = frozenset(provider.PROVIDER_PROFILES)
REVIEWER_CHOICES = frozenset(("", "opencode"))

MAX_MODEL_ID = 128
MAX_REVIEWER_MODEL = 128
MAX_API_KEY_CHARS = 512
MAX_VAULT_PATH_CHARS = 512

_USER_SETTINGS_KEYS = frozenset(
    {"provider", "model", "fallback_model", "timeout_seconds",
     "max_tokens", "history_messages", "reviewer", "reviewer_model",
     "vault_path"})


class SettingsError(Exception):
    def __init__(self, status, code, message):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


def invalid(message, code="invalid_value"):
    return SettingsError(400, code, message)


def conflict(message, code="field_locked"):
    return SettingsError(409, code, message)


def config_dir(environ=None):
    """Resolve the per-user configuration directory (never in-repo)."""
    env = os.environ if environ is None else environ
    raw = env.get("JARVIS_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw)
    xdg = env.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "jarvis"


def validate_model_id(value, field="model"):
    if not isinstance(value, str):
        return invalid(f"'{field}' must be a string")
    value = value.strip()
    if not value:
        return None  # empty clears the stored value
    if len(value) > MAX_MODEL_ID or any(ch.isspace() for ch in value):
        return invalid(
            f"'{field}' must be at most {MAX_MODEL_ID} characters without "
            "whitespace")
    return None


def validate_setting(key, value):
    """Validate one user setting. Returns None or a SettingsError."""
    if key == "provider":
        if value not in PROVIDERS:
            return invalid(
                "'provider' must be one of: " + ", ".join(sorted(PROVIDERS)))
        return None
    if key in ("model", "fallback_model"):
        err = validate_model_id(value, field=key)
        if err is not None:
            return err
        if key == "model" and value == "" and isinstance(value, str):
            # allow storing provider default by clearing
            return None
        return None
    if key in BOUNDS:
        if not isinstance(value, int) or isinstance(value, bool):
            return invalid(f"'{key}' must be an integer")
        lo, hi = BOUNDS[key]
        if not lo <= value <= hi:
            return invalid(
                f"'{key}' must be between {lo} and {hi}")
        return None
    if key == "reviewer":
        if value not in REVIEWER_CHOICES:
            return invalid("'reviewer' must be empty or 'opencode'")
        return None
    if key == "reviewer_model":
        if not isinstance(value, str):
            return invalid("'reviewer_model' must be a string")
        value = value.strip()
        if value and (len(value) > MAX_REVIEWER_MODEL
                      or any(ch.isspace() for ch in value)):
            return invalid(
                "'reviewer_model' must be empty or a 'provider/model' id "
                "without whitespace")
        return None
    if key == "vault_path":
        # Phase 13A: the Obsidian vault root. Administrative user
        # configuration — validated for existence/directory-ness here
        # and re-resolved at every use (vault.validate_root). Empty
        # clears the setting (honest "not configured" state).
        if not isinstance(value, str):
            return invalid("'vault_path' must be a string")
        value = value.strip()
        if not value:
            return None
        if len(value) > MAX_VAULT_PATH_CHARS:
            return invalid(
                f"'vault_path' must be at most {MAX_VAULT_PATH_CHARS}"
                " characters")
        import vault as vault_module
        try:
            vault_module.validate_root(value)
        except vault_module.VaultError as exc:
            return invalid(str(exc), code=exc.code)
        return None
    return invalid("unsupported setting", code="unsupported_field")


def _atomic_write(path, data, mode=0o600):
    """Atomic write with restrictive permissions; never partial."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _harden_dir(path):
    """Tighten configuration directory permissions (0700)."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else default
    except FileNotFoundError:
        return default
    except (ValueError, OSError):
        # corrupt store: treat as absent, never crash the server for it
        return default


class SettingsStore:
    """Per-user persisted settings + credentials (thread-safe)."""

    def __init__(self, directory, environ=None):
        self.dir = Path(directory)
        self.settings_path = self.dir / "settings.json"
        self.credentials_path = self.dir / "credentials.json"
        self.lock = threading.Lock()
        self.environ = environ if environ is not None else os.environ

    # ---------------- settings ----------------

    def load(self):
        with self.lock:
            return self._load_locked()

    def _load_locked(self):
        data = _read_json(self.settings_path, {})
        stored = data.get("settings")
        if not isinstance(stored, dict):
            return {}
        cleaned = {}
        for key, value in stored.items():
            if key not in _USER_SETTINGS_KEYS:
                continue
            if key == "vault_path":
                # Load-time shape check only (Phase 13A): a configured
                # vault may legitimately disappear later; the stored
                # setting must survive that so the UI can show the
                # honest unavailable state. Existence is enforced at
                # write time and re-checked at every use.
                if isinstance(value, str) and value.strip() \
                        and len(value) <= MAX_VAULT_PATH_CHARS \
                        and Path(value.strip()).is_absolute():
                    cleaned[key] = value
                continue
            if validate_setting(key, value) is None:
                cleaned[key] = value
        return cleaned

    def save_fields(self, fields, *, env_managed=frozenset(),
                    preserve=None):
        """Merge validated fields into the persisted settings atomically.

        `preserve` allows saving a subset without touching unrelated
        keys. Returns the full persisted mapping.
        """
        unknown = set(fields) - _USER_SETTINGS_KEYS
        if unknown:
            raise invalid(
                "unsupported setting: " + ", ".join(sorted(unknown)),
                code="unsupported_field")
        for key, value in fields.items():
            err = validate_setting(key, value)
            if err is not None:
                raise err
            if not value and value != "":
                raise invalid(f"'{key}' is missing")
        locked = [k for k in fields
                  if k not in ("provider",)
                  and k in env_managed]
        if locked:
            raise conflict(
                "environment-managed: " + ", ".join(sorted(locked)),
                code="field_locked")
        with self.lock:
            current = self._load_locked()
            if preserve is not None:
                fields = {k: v for k, v in fields.items() if k in preserve}
            next_values = dict(current)
            next_values.update(fields)
            for key, value in list(next_values.items()):
                # empty string means "clear back to default" for these
                if value == "" and key in ("fallback_model",
                                           "reviewer_model", "model",
                                           "vault_path"):
                    next_values.pop(key)
            _atomic_write(self.settings_path, {
                "version": SETTINGS_VERSION,
                "saved_at": time.time(),
                "settings": next_values,
            })
            return next_values

    def clear_setting(self, key, env_managed=frozenset()):
        if key not in _USER_SETTINGS_KEYS:
            raise invalid("unsupported setting", code="unsupported_field")
        if key in env_managed:
            raise conflict(
                f"'{key}' is managed by an environment variable")
        with self.lock:
            current = self._load_locked()
            current.pop(key, None)
            _atomic_write(self.settings_path, {
                "version": SETTINGS_VERSION,
                "saved_at": time.time(),
                "settings": current,
            })
            return current

    # ---------------- credentials (write-only surface) ----------------

    def key_configured(self, profile):
        return bool(self.stored_key(profile))

    def stored_key(self, profile):
        if profile not in PROVIDERS:
            return None
        data = _read_json(self.credentials_path, {})
        cred = data.get(profile)
        if isinstance(cred, dict) and isinstance(cred.get("api_key"), str) \
                and cred["api_key"].strip():
            return cred["api_key"].strip()
        return ""

    def save_key(self, profile, api_key):
        """Persist one credential atomically. Never returns or logs it."""
        if profile not in PROVIDERS:
            raise invalid("unknown provider", code="invalid_provider")
        if not isinstance(api_key, str) \
                or not api_key.strip() \
                or len(api_key.strip()) > MAX_API_KEY_CHARS \
                or any(ord(ch) < 32 for ch in api_key):
            raise invalid(
                "'api_key' must be a credential of at most "
                f"{MAX_API_KEY_CHARS} characters without control "
                "characters", code="invalid_credential")
        with self.lock:
            data = _read_json(self.credentials_path, {})
            data[profile] = {
                "api_key": api_key.strip(),
                "saved_at": time.time(),
            }
            data.pop("__note__", None)
            _atomic_write(self.credentials_path, data)

    def clear_key(self, profile):
        if profile not in PROVIDERS:
            raise invalid("unknown provider", code="invalid_provider")
        with self.lock:
            data = _read_json(self.credentials_path, {})
            if profile in data:
                data.pop(profile)
                _atomic_write(self.credentials_path, data)

    # ---------------- effective resolution ----------------

    def resolve_key_source(self, profile, cfg):
        """Which source holds the active credential (no value leaves).

        'environment' | 'user_store' | '' (not configured).
        """
        env_name = "FREELLMAPI_API_KEY" if profile == "freellmapi" \
            else "JARVIS_ORCAROUTER_API_KEY"
        if (self.environ.get(env_name, "") or "").strip():
            return "environment"
        if self.key_configured(profile):
            return "user_store"
        return ""

    def active_api_key(self, profile, cfg):
        env_name = "FREELLMAPI_API_KEY" if profile == "freellmapi" \
            else "JARVIS_ORCAROUTER_API_KEY"
        env_value = (self.environ.get(env_name, "") or "").strip()
        if env_value:
            return env_value
        return self.stored_key(profile) or ""

    def resolve_provider(self, cfg=None):
        """Active provider profile: persisted choice, else the legacy
        FreeLLMAPI local gateway (backward compatibility)."""
        return self._load_locked().get("provider") or "freellmapi"


def effective_config(base_cfg, store):
    """Merge environment config + persisted user config → one frozen
    Config snapshot (the whole application reads exactly this).

    Precedence per setting, descending:
      1. environment (base_cfg.env_managed)
      2. persisted user configuration
      3. application default (base_cfg value / provider default)

    The provider base URL is NEVER user-editable free text: it is the
    fixed profile base (loopback gateway or the allowlisted OrcaRouter
    base). The credential for the ACTIVE profile resolves
    environment > user_store > none.
    """
    import config as config_module
    values = store.load() if isinstance(store, SettingsStore) else {}
    env_managed = base_cfg.env_managed

    def pick(name, env_value, default):
        if name in env_managed:
            return env_value
        if name in values and values[name] is not None:
            return values[name]
        return default

    provider_name = values.get("provider") or "freellmapi"
    profile = provider.PROVIDER_PROFILES[provider_name]

    if provider_name == "freellmapi":
        base_url = base_cfg.api_base or provider.DEFAULT_API_BASE
    else:
        base_url = profile["base_url"]

    if "model" in env_managed:
        model = base_cfg.model
    elif values.get("model"):
        model = values["model"]
    else:
        model = provider.PROVIDER_DEFAULT_MODEL[provider_name]

    if "fallback_model" in env_managed:
        fallback = base_cfg.fallback_model
    elif values.get("fallback_model"):
        fallback = values["fallback_model"]
    else:
        fallback = ""

    api_key = store.active_api_key(provider_name, base_cfg)

    return config_module.Config(
        host=base_cfg.host,
        port=base_cfg.port,
        db_path=base_cfg.db_path,
        provider=provider_name,
        api_base=base_url,
        api_key=api_key,
        model=model,
        fallback_model=fallback,
        reviewer=pick(
            "reviewer", base_cfg.reviewer,
            base_cfg.reviewer if "reviewer" in env_managed else ""),
        reviewer_path=base_cfg.reviewer_path,
        reviewer_model=pick(
            "reviewer_model", base_cfg.reviewer_model,
            base_cfg.reviewer_model if "reviewer_model" in env_managed
            else ""),
        workspace_root=base_cfg.workspace_root,
        timeout_seconds=pick(
            "timeout_seconds", base_cfg.timeout_seconds,
            base_cfg.timeout_seconds if "timeout_seconds" in env_managed
            else config_module.DEFAULT_TIMEOUT_SECONDS),
        max_tokens=pick(
            "max_tokens", base_cfg.max_tokens,
            base_cfg.max_tokens if "max_tokens" in env_managed
            else config_module.DEFAULT_MAX_TOKENS),
        history_messages=pick(
            "history_messages", base_cfg.history_messages,
            base_cfg.history_messages if "history_messages" in env_managed
            else config_module.DEFAULT_HISTORY_MESSAGES),
        env_managed=env_managed,
    )
