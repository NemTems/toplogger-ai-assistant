"""Shared pytest fixtures.

Tests must never depend on, or leak, the developer's real environment. The
developer running these tests locally has a real ``.env`` file (and/or real
``TOPLOGGER_*`` env vars) in the repo root, which may contain a real personal
``TOPLOGGER_USER_ID`` (hard rule 4 — never persist other people's data, and by
extension personal data must never leak into or influence a test run).
"""

import os

import pytest

from config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    """Isolate ``Settings``/``get_settings`` from the developer's real environment.

    - Strips every ``TOPLOGGER_*`` env var from the test process so no real
      value (e.g. a real ``user_id``) can leak into a test via inherited
      shell state.
    - Changes the working directory to an empty ``tmp_path`` so
      pydantic-settings' ``env_file=".env"`` lookup can never find the
      developer's real ``.env`` in the repo root — only a ``.env`` a test
      explicitly writes into ``tmp_path`` itself would be picked up.
    - Clears the ``get_settings()`` ``lru_cache`` before and after each test
      so a ``Settings`` instance built in one test can never leak into
      another via the cache.
    """
    for key in list(os.environ):
        if key.startswith("TOPLOGGER_"):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.chdir(tmp_path)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
