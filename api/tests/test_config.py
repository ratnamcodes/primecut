"""Settings load with the documented defaults and fail with a human-readable
ConfigError when a required secret is missing.

Offline: no network. Every construction passes ``_env_file=None`` so the real
``api/.env`` (present during a normal test run) never fills in a secret and
masks the failure path, and so the friendly error comes from the
``load_settings`` factory rather than pydantic's raw lowercase field error.
"""

import pytest

import primecut.config as config
from primecut.config import ConfigError, Settings, load_settings, require

# Deliberately shaped so they can never resemble a real key: test_no_secrets.py
# scans this file too, and "AIza..." or "hf_..." prefixes would trip it.
FAKE_GOOGLE = "test-google-key-not-real"
FAKE_HF = "test-hf-token-not-real"


@pytest.fixture
def required_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", FAKE_GOOGLE)
    monkeypatch.setenv("HF_TOKEN", FAKE_HF)


def test_defaults(required_env):
    s = Settings(_env_file=None)
    assert s.ranking_model == "gemini-3.1-pro-preview"
    assert s.summary_model == "gemini-3.5-flash"
    assert s.whisper_model == "large-v3-turbo"
    assert s.min_clip_seconds == 20.0
    assert s.max_clip_seconds == 75.0
    assert s.default_num_shorts == 6
    assert s.episode_cut_target_seconds == 900
    assert s.clip_pad_seconds == 0.18
    assert s.target_width == 1080
    assert s.target_height == 1920
    assert s.target_fps == 30
    assert s.crf == 20
    assert s.x264_preset == "medium"
    assert s.audio_bitrate == "192k"
    assert s.credits_per_source_minute == 1
    assert s.r2_bucket == "primecut-media"
    assert s.modal_secret_name == "primecut-secrets"
    assert s.database_url == "postgresql+psycopg://postgres:postgres@localhost:5432/primecut"
    assert s.r2_account_id is None
    assert s.upstash_redis_rest_url is None
    assert s.clerk_secret_key is None


def test_env_overrides_are_case_insensitive(required_env, monkeypatch):
    monkeypatch.setenv("target_height", "1080")
    monkeypatch.setenv("RANKING_MODEL", "gemini-test-override")
    s = Settings(_env_file=None)
    assert s.target_height == 1080
    assert s.ranking_model == "gemini-test-override"


def test_missing_required_is_friendly(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(ConfigError) as exc:
        load_settings(_env_file=None)
    msg = str(exc.value)
    assert "GOOGLE_API_KEY" in msg
    assert "HF_TOKEN" in msg
    assert "api/.env" in msg
    assert "cp .env.example .env" in msg
    assert "https://aistudio.google.com/apikey" in msg
    assert "https://huggingface.co/settings/tokens" in msg
    # No pydantic internals: the class name and lowercase field names would
    # only appear if the raw ValidationError leaked through.
    assert "ValidationError" not in msg
    assert "google_api_key" not in msg
    # Raised `from None`, so no chained traceback wall either.
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True


def test_raw_settings_still_raises_pydantic_error(monkeypatch):
    # The friendly path exists only through the factory; document that.
    from pydantic import ValidationError

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_invalid_value_is_readable(required_env, monkeypatch):
    monkeypatch.setenv("MIN_CLIP_SECONDS", "twenty")
    with pytest.raises(ConfigError) as exc:
        load_settings(_env_file=None)
    msg = str(exc.value)
    assert "MIN_CLIP_SECONDS" in msg
    assert "twenty" in msg
    assert "ValidationError" not in msg


def test_empty_value_counts_as_unset(required_env, monkeypatch):
    # .env.example ships `KEY=` for every key. A copied-but-unfilled .env must
    # keep the defaults for tunables and still fail loudly on the secrets.
    monkeypatch.setenv("TARGET_HEIGHT", "")
    assert Settings(_env_file=None).target_height == 1920
    monkeypatch.setenv("HF_TOKEN", "")
    with pytest.raises(ConfigError) as exc:
        load_settings(_env_file=None)
    assert "HF_TOKEN" in str(exc.value)


def test_secrets_are_masked_in_str_and_repr(required_env):
    r2_secret = "test-r2-secret-not-real"
    s = Settings(_env_file=None, r2_secret_access_key=r2_secret)
    for rendered in (str(s), repr(s), str(s.google_api_key), repr(s.hf_token)):
        assert FAKE_GOOGLE not in rendered
        assert FAKE_HF not in rendered
        assert r2_secret not in rendered
    assert "**********" in repr(s.google_api_key)
    assert "**********" in str(s)
    # The value is still there for code that asks for it explicitly.
    assert s.google_api_key.get_secret_value() == FAKE_GOOGLE
    assert s.r2_secret_access_key is not None
    assert s.r2_secret_access_key.get_secret_value() == r2_secret


def test_require_returns_plain_string(required_env, monkeypatch):
    loaded = load_settings(_env_file=None, dodo_api_key="test-dodo-key-not-real")
    monkeypatch.setattr(config, "settings", loaded)
    assert require("dodo_api_key") == "test-dodo-key-not-real"
    assert require("google_api_key") == FAKE_GOOGLE
    assert require("r2_bucket") == "primecut-media"
    assert isinstance(require("google_api_key"), str)


def test_require_raises_friendly_when_none(required_env, monkeypatch):
    monkeypatch.setattr(config, "settings", load_settings(_env_file=None))
    with pytest.raises(ConfigError) as exc:
        require("clerk_secret_key")
    msg = str(exc.value)
    assert "CLERK_SECRET_KEY" in msg
    assert "api/.env" in msg
    assert "cp .env.example .env" in msg
    assert "https://dashboard.clerk.com" in msg


def test_require_unknown_field(required_env, monkeypatch):
    monkeypatch.setattr(config, "settings", load_settings(_env_file=None))
    with pytest.raises(ConfigError) as exc:
        require("not_a_real_setting")
    assert "not_a_real_setting" in str(exc.value)
