"""Typed settings for PrimeCut, loaded once at import time.

Values come from the process environment first, then from the ``.env`` in the
current working directory, which is ``api/.env`` when commands are run from
``api/`` as the README quickstart does (create it with ``cp .env.example .env``).
A missing required secret raises :class:`ConfigError` with a message a human
can act on instead of a pydantic traceback. Every construction of
:class:`Settings` goes through :func:`load_settings` so that friendly path is
the only path, and tests can force it on demand with ``_env_file=None``.
"""

from typing import get_args

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "ConfigError",
    "SECRET_HINTS",
    "Settings",
    "load_settings",
    "require",
    "settings",
]

# Where the values are expected to live, relative to the repo root. Named in
# every error message so the reader knows which file to open.
ENV_FILE = "api/.env"

# Where to obtain each credential, keyed by its UPPERCASE env var name. This is
# surfaced in ConfigError messages so nobody has to hunt through dashboards.
SECRET_HINTS: dict[str, str] = {
    "GOOGLE_API_KEY": "https://aistudio.google.com/apikey",
    "HF_TOKEN": "https://huggingface.co/settings/tokens",
    "R2_ACCOUNT_ID": "https://dash.cloudflare.com -> R2 -> Overview (Account ID in the sidebar)",
    "R2_ACCESS_KEY_ID": "https://dash.cloudflare.com -> R2 -> Manage R2 API Tokens",
    "R2_SECRET_ACCESS_KEY": "https://dash.cloudflare.com -> R2 -> Manage R2 API Tokens",
    "UPSTASH_REDIS_REST_URL": "https://console.upstash.com -> your database -> REST API",
    "UPSTASH_REDIS_REST_TOKEN": "https://console.upstash.com -> your database -> REST API",
    "DODO_API_KEY": "https://app.dodopayments.com -> Developer -> API Keys",
    "DODO_WEBHOOK_SECRET": "https://app.dodopayments.com -> Developer -> Webhooks",
    "CLERK_SECRET_KEY": "https://dashboard.clerk.com -> API Keys",
}


class Settings(BaseSettings):
    """All runtime configuration. Field names are the env var names, lowercased."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # .env.example ships every key as `KEY=`. Treat an empty value as unset
        # so a copied-but-unfilled .env still fails loudly on the required
        # secrets and falls back to the defaults for everything else.
        env_ignore_empty=True,
    )

    # --- Required now. No defaults, so a missing one is a startup failure. ---
    # These are SecretStr rather than str so an accidental print(settings) or a
    # logged traceback shows SecretStr('**********') instead of the key. That
    # leak-safe repr is the reason the type exists here.
    google_api_key: SecretStr
    hf_token: SecretStr

    # --- Optional now, filled in by later phases. ---
    # Credentials get the same SecretStr treatment as above. Account IDs and
    # URLs are identifiers, not secrets, so they stay plain str.
    r2_account_id: str | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    upstash_redis_rest_url: str | None = None
    upstash_redis_rest_token: SecretStr | None = None
    dodo_api_key: SecretStr | None = None
    dodo_webhook_secret: SecretStr | None = None
    clerk_secret_key: SecretStr | None = None
    r2_bucket: str = "primecut-media"
    modal_secret_name: str = "primecut-secrets"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/primecut"

    # --- Tunables. Referenced by name in every later task; do not rename. ---
    ranking_model: str = "gemini-3.1-pro-preview"
    summary_model: str = "gemini-3.5-flash"
    whisper_model: str = "large-v3-turbo"
    min_clip_seconds: float = 20.0
    max_clip_seconds: float = 75.0
    default_num_shorts: int = 6
    episode_cut_target_seconds: int = 900
    clip_pad_seconds: float = 0.18
    target_width: int = 1080
    target_height: int = 1920
    target_fps: int = 30
    crf: int = 20
    x264_preset: str = "medium"
    audio_bitrate: str = "192k"
    credits_per_source_minute: int = 1


class ConfigError(RuntimeError):
    """A setting is missing or invalid. The message says which and how to fix it."""


def _is_secret_field(field_name: str) -> bool:
    field = Settings.model_fields.get(field_name)
    if field is None:
        return False
    annotation = field.annotation
    return annotation is SecretStr or SecretStr in get_args(annotation)


def _missing_message(env_names: list[str]) -> str:
    plural = "s" if len(env_names) > 1 else ""
    lines = [
        f"Missing required setting{plural}: {', '.join(env_names)}",
        "",
        f"PrimeCut reads these from {ENV_FILE} (or the shell environment).",
        "If that file does not exist yet, create it from the template:",
        "",
        "    cp .env.example .env    # run from inside api/",
        "",
        "then open .env and fill in:",
    ]
    width = max(len(name) for name in env_names)
    for name in env_names:
        hint = SECRET_HINTS.get(name)
        lines.append(f"    {name:<{width}}  ->  {hint}" if hint else f"    {name}")
    return "\n".join(lines)


def load_settings(**kwargs) -> Settings:
    """Construct Settings, translating pydantic errors into a readable ConfigError.

    Keyword arguments are passed straight to ``Settings``; ``_env_file=None``
    skips the dotenv file entirely, which is how tests exercise the failure
    path without touching the real ``api/.env``.
    """
    try:
        return Settings(**kwargs)
    except ValidationError as exc:
        missing: list[str] = []
        invalid: list[str] = []
        for err in exc.errors():
            loc = err.get("loc") or ()
            field = str(loc[0]) if loc else "<unknown>"
            env_name = field.upper()
            if err.get("type") == "missing":
                missing.append(env_name)
                continue
            detail = f"    {env_name}: {err.get('msg', 'invalid value')}"
            if not _is_secret_field(field) and "input" in err:
                detail += f" (got {err['input']!r})"
            invalid.append(detail)

        parts: list[str] = []
        if missing:
            parts.append(_missing_message(missing))
        if invalid:
            plural = "s" if len(invalid) > 1 else ""
            parts.append(f"Invalid setting{plural} in {ENV_FILE}:\n" + "\n".join(invalid))

        # `from None` drops pydantic's chained traceback so the reader sees one
        # short message instead of a wall of validator internals at 1 AM.
        raise ConfigError("\n\n".join(parts)) from None


settings = load_settings()


def require(field_name: str) -> str:
    """Return an optional setting as a plain string, or raise a friendly ConfigError.

    Lets later phases demand a credential at the moment they need it (for
    example ``require("clerk_secret_key")`` inside an auth handler) instead of
    making every key mandatory at import. SecretStr values are unwrapped.
    """
    if field_name not in Settings.model_fields:
        known = ", ".join(sorted(Settings.model_fields))
        raise ConfigError(f"Settings has no field {field_name!r}. Known fields: {known}")
    value = getattr(settings, field_name)
    if value is None:
        raise ConfigError(_missing_message([field_name.upper()]))
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return str(value)
