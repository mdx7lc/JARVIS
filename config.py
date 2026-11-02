"""JARVIS runtime configuration.

Phase 1: JARVIS_HOST, JARVIS_PORT, JARVIS_DB_PATH.
Phase 2B: FreeLLMAPI provider settings (base URL, model, limits). The API
key is read from the environment only and is never logged or exposed.
Missing credentials are not an error at startup: the server starts and
chat requests report that AI chat is not configured.

Phase 11A: explicit environment-managed keys. `Config.env_managed` lists
the canonical setting names whose value was EXPLICITLY set through an
environment variable (deployment configuration). The precedence used
everywhere is: environment > persisted user configuration
(settingsstore) > application default. Secrets are still read from the
environment only at this layer; the user store owns its own
credential file.
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

ORCAROUTER_KEY_ENV = "JARVIS_ORCAROUTER_API_KEY"

# canonical setting name -> environment variable (Phase 11A precedence)
ENV_KEYS = {
    "freellmapi_url": "FREELLMAPI_URL",
    "freellmapi_api_key": "FREELLMAPI_API_KEY",
    "orcarouter_api_key": ORCAROUTER_KEY_ENV,
    "model": "JARVIS_MODEL",
    "fallback_model": "JARVIS_FALLBACK_MODEL",
    "reviewer": "JARVIS_REVIEWER",
    "reviewer_path": "JARVIS_REVIEWER_PATH",
    "reviewer_model": "JARVIS_REVIEWER_MODEL",
    "timeout_seconds": "JARVIS_TIMEOUT_SECONDS",
    "max_tokens": "JARVIS_MAX_TOKENS",
    "history_messages": "JARVIS_HISTORY_MESSAGES",
    "host": "JARVIS_HOST",
    "port": "JARVIS_PORT",
    "db_path": "JARVIS_DB_PATH",
    "workspace_root": "JARVIS_WORKSPACE_ROOT",
    "config_dir": "JARVIS_CONFIG_DIR",
    "vault_path": "JARVIS_VAULT_PATH",
}

DEFAULT_API_BASE = "http://localhost:3001"
DEFAULT_MODEL = "qwen3.8-27b"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_TOKENS = 512
DEFAULT_HISTORY_MESSAGES = 20


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: Path
    api_base: str = DEFAULT_API_BASE
    api_key: str = ""  # empty means chat is not configured
    model: str = DEFAULT_MODEL
    fallback_model: str = ""  # empty disables the fallback policy
    reviewer: str = ""  # "" | "opencode" (Phase 9B-R); never auto-on
    reviewer_path: str = ""  # optional executable override
    reviewer_model: str = ""  # optional reviewer model id (provider/model)
    workspace_root: Path = None  # resolved by __post_init__
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_tokens: int = DEFAULT_MAX_TOKENS
    history_messages: int = DEFAULT_HISTORY_MESSAGES
    # Phase 11A: active provider profile ("freellmapi" | "orcarouter")
    provider: str = "freellmapi"
    # Phase 11A: which settings were explicitly configured via env vars
    # (deployment configuration; first in the precedence chain)
    env_managed: frozenset = field(default=frozenset())
    # Phase 11A: the environ dict this Config was loaded from (the store
    # resolves environment/credential precedence against exactly this
    # environment, so a test's isolated environ can never see operator
    # secrets, and vice versa). Not part of equality.
    environ_source: dict = field(default=None, repr=False, compare=False)

def _int_setting(env, name, default, lo, hi):
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return value


def _model_setting(env):
    model = env.get("JARVIS_MODEL", DEFAULT_MODEL).strip()
    if not model or len(model) > 128 or any(ch.isspace() for ch in model):
        raise ConfigError(
            "JARVIS_MODEL must be a non-empty model id without whitespace"
        )
    return model


def _fallback_model_setting(env):
    """Optional fallback model policy (Phase 8A).

    Empty/unset means fallback is disabled — the primary model is the
    only attempt and failures surface honestly. The exact model ids for
    the intended GLM/DeepSeek defaults must come from the gateway's
    authenticated catalog (live-smoke-verified); nothing is guessed.
    """
    model = env.get("JARVIS_FALLBACK_MODEL", "").strip()
    if len(model) > 128 or any(ch.isspace() for ch in model):
        raise ConfigError(
            "JARVIS_FALLBACK_MODEL must be empty or a model id without "
            "whitespace"
        )
    return model


def _reviewer_setting(env):
    """Independent plan reviewer selection (Phase 9B-R, opt-in only).

    Empty/absent keeps the honest local-validation-only policy with
    review recorded as unavailable — an approval is never fabricated.
    "opencode" enables the separately invoked, restricted OpenCode CLI
    adapter (see reviewer.OpenCodeReviewer): a fresh non-interactive
    run in an isolated temporary home with all tools permission-denied.
    Any other value is refused — unknown integrations must not be
    half-configured.
    """
    raw = env.get("JARVIS_REVIEWER", "").strip()
    if raw in ("", "opencode"):
        return raw
    raise ConfigError(
        "JARVIS_REVIEWER must be empty or 'opencode'"
    )


def _reviewer_path_setting(env):
    # Optional executable override; if the path is wrong the worker
    # honestly records the reviewer as missing (never as approved).
    return env.get("JARVIS_REVIEWER_PATH", "").strip()


def _reviewer_model_setting(env):
    """Optional reviewer model id ("provider/model" form). Unset lets
    the reviewer CLI use its own configured default; a failed default
    yields an honest reviewer failure, never an approval."""
    model = env.get("JARVIS_REVIEWER_MODEL", "").strip()
    if len(model) > 128 or any(ch.isspace() for ch in model):
        raise ConfigError(
            "JARVIS_REVIEWER_MODEL must be empty or a 'provider/model' "
            "id without whitespace"
        )
    return model


def _workspace_root_setting(env):
    """Read-only tool root (Phase 8B). Default: the JARVIS project
    directory itself — an explicit, known workspace; never the caller's
    home. Absolute paths are honored; relative resolve to project root.
    The directory must already exist (goal execution reports the tool
    error honestly otherwise)."""
    raw = env.get("JARVIS_WORKSPACE_ROOT", "").strip()
    if not raw:
        return PROJECT_ROOT
    root = Path(raw)
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    return root.resolve()


def load(environ=None):
    env = os.environ if environ is None else environ

    host = env.get("JARVIS_HOST", "127.0.0.1")
    if host not in _LOOPBACK_HOSTS:
        raise ConfigError(
            f"refusing non-loopback bind host {host!r}; "
            "JARVIS must listen on 127.0.0.1/localhost/::1 only"
        )

    raw_port = env.get("JARVIS_PORT", "8080")
    try:
        port = int(raw_port)
    except ValueError:
        raise ConfigError(f"JARVIS_PORT must be an integer, got {raw_port!r}")
    if not 1 <= port <= 65535:
        raise ConfigError(f"JARVIS_PORT {port} out of range (1-65535)")

    db_raw = env.get("JARVIS_DB_PATH", "data/jarvis.db")
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    # Provider settings: base URL/key are validated later by provider.py
    # (at request time) so the server can always start.
    api_base = env.get("FREELLMAPI_URL", DEFAULT_API_BASE).strip()
    api_key = env.get("FREELLMAPI_API_KEY", "").strip()

    # Phase 11A: record which settings were EXPLICITLY environment-
    # configured (non-empty value). These lock the corresponding UI
    # control with an "environment-managed" state instead of pretending
    # a UI change took effect.
    managed = frozenset(
        name for name, var in ENV_KEYS.items()
        if env.get(var, "").strip()
    )

    return Config(
        host=host,
        port=port,
        db_path=db_path.resolve(),
        api_base=api_base,
        api_key=api_key,
        model=_model_setting(env),
        fallback_model=_fallback_model_setting(env),
        reviewer=_reviewer_setting(env),
        reviewer_path=_reviewer_path_setting(env),
        reviewer_model=_reviewer_model_setting(env),
        workspace_root=_workspace_root_setting(env),
        timeout_seconds=_int_setting(
            env, "JARVIS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, 1, 300
        ),
        max_tokens=_int_setting(
            env, "JARVIS_MAX_TOKENS", DEFAULT_MAX_TOKENS, 1, 4096
        ),
        history_messages=_int_setting(
            env, "JARVIS_HISTORY_MESSAGES", DEFAULT_HISTORY_MESSAGES, 1, 100
        ),
        env_managed=managed,
        environ_source=environ,
    )


if __name__ == "__main__":
    try:
        cfg = load()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    print("configuration ok (secrets are not displayed)")
