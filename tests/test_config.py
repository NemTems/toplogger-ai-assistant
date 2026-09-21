"""Tests for config.Settings and config.get_settings().

These tests rely on the autouse `isolated_settings` fixture in conftest.py to
strip TOPLOGGER_* env vars, avoid the developer's real .env, and clear the
get_settings() cache — so nothing here depends on, or can leak, the
developer's real personal data (hard rule 4).
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from config import Settings, get_settings


def test_defaults():
    settings = Settings()
    assert settings.gym_id == "6k5d6kkbrp6y4rqmj84ip"
    assert settings.rate_limit_per_second == 1.0
    assert settings.data_dir == Path("data")


def test_user_id_defaults_to_none():
    """No baked-in personal id: user_id must default to None, never a real id."""
    settings = Settings()
    assert settings.user_id is None


def test_env_override_data_dir(monkeypatch):
    monkeypatch.setenv("TOPLOGGER_DATA_DIR", "custom-data")
    settings = Settings()
    assert settings.data_dir == Path("custom-data")


def test_env_override_gym_id(monkeypatch):
    monkeypatch.setenv("TOPLOGGER_GYM_ID", "some-other-fake-gym-id")
    settings = Settings()
    assert settings.gym_id == "some-other-fake-gym-id"


def test_raw_dir_derives_from_custom_data_dir(monkeypatch):
    monkeypatch.setenv("TOPLOGGER_DATA_DIR", "custom-data")
    settings = Settings()
    assert settings.raw_dir == Path("custom-data") / "raw"


def test_db_path_derives_from_custom_data_dir(monkeypatch):
    monkeypatch.setenv("TOPLOGGER_DATA_DIR", "custom-data")
    settings = Settings()
    assert settings.db_path == Path("custom-data") / "db" / "toplogger.sqlite"


def test_rate_limit_above_one_raises():
    """Hard rule 5 (politeness) is a hard ceiling of 1 req/s — not just a default."""
    with pytest.raises(ValidationError):
        Settings(rate_limit_per_second=1.5)


def test_rate_limit_zero_or_below_raises():
    with pytest.raises(ValidationError):
        Settings(rate_limit_per_second=0)
    with pytest.raises(ValidationError):
        Settings(rate_limit_per_second=-1.0)


def test_get_settings_is_cached():
    first = get_settings()
    second = get_settings()
    assert first is second


def test_blank_user_id_env_var_is_none(monkeypatch):
    """A present-but-empty TOPLOGGER_USER_ID reads as unset, not as "".

    `.env.example` ships the key commented out, but a user who uncomments it and
    leaves it blank must not get an empty string passed on as if it were an ID.
    """
    monkeypatch.setenv("TOPLOGGER_USER_ID", "")
    assert Settings().user_id is None


def test_whitespace_user_id_env_var_is_none(monkeypatch):
    """Whitespace-only is unset too — it is never a valid ID."""
    monkeypatch.setenv("TOPLOGGER_USER_ID", "   ")
    assert Settings().user_id is None


def test_real_user_id_env_var_is_preserved(monkeypatch):
    """A genuine value must still come through untouched."""
    monkeypatch.setenv("TOPLOGGER_USER_ID", "fake-user-id-123")
    assert Settings().user_id == "fake-user-id-123"
