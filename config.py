"""Application configuration.

Settings are sourced from environment variables (prefix ``TOPLOGGER_``) and/or a
local ``.env`` file, never hardcoded secrets. This module intentionally has NO
token/password/secret field: refresh and access tokens live only in the OS
keychain via ``keyring`` (see AGENTS.md hard rule 1 and CLAUDE.md hard rule 1).
Do not add one here.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the assistant.

    Values are read from environment variables prefixed with ``TOPLOGGER_``
    (e.g. ``TOPLOGGER_GYM_ID``) or from a ``.env`` file in the working
    directory. No secrets live here — see the module docstring.
    """

    model_config = SettingsConfigDict(
        env_prefix="TOPLOGGER_",
        env_file=".env",
        extra="ignore",
    )

    gym_id: str = "6k5d6kkbrp6y4rqmj84ip"
    """TopLogger gym ID. Defaults to Falkors, already public in docs/PROJECT_CONTEXT.md."""

    user_id: str | None = None
    """Personal TopLogger user ID. Env-only, NEVER given a default value."""

    rate_limit_per_second: float = Field(default=1.0, gt=0, le=1.0)
    """Max requests/second to TopLogger. Hard-capped at 1.0 (politeness rule)."""

    data_dir: Path = Path("data")
    """Root directory for local data (gitignored: raw/, db/)."""

    @field_validator("user_id", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat a blank ``TOPLOGGER_USER_ID=`` as unset rather than an empty string.

        A key present but empty in ``.env`` would otherwise arrive as ``""``, which is
        not ``None`` and would be passed on to a query as if it were a real ID.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def raw_dir(self) -> Path:
        """Directory holding immutable raw API responses (``data_dir / "raw"``)."""
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file (``data_dir / "db" / "toplogger.sqlite"``)."""
        return self.data_dir / "db" / "toplogger.sqlite"


@lru_cache
def get_settings() -> Settings:
    """Return a cached, process-wide ``Settings`` instance."""
    return Settings()
