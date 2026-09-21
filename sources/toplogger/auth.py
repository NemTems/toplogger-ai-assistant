"""Refresh-token auth for TopLogger.

The flow (docs/PROJECT_CONTEXT.md §1):

* ``authSigninRefreshToken`` takes a refresh token and returns a 10-minute **access**
  token plus a **brand-new refresh token**. The token just sent is dead.
* So every call rotates the stored credential, and the new one must be persisted
  *before anything else* — a crash between the API call and the write leaves a dead
  token behind and costs a manual browser login (AGENTS.md hard rule 3).

Storage: the OS keychain via ``keyring``. As a one-time bootstrap, a refresh token
may be pasted into ``.env`` as ``TOPLOGGER_REFRESH_TOKEN``; the first run imports it
into the keychain and tells you to delete the line. ``.env`` is never written to.

``authSignin`` is never called from here. It needs a reCAPTCHA and is manual-only
(hard rule 2).
"""

from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import keyring
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from config import get_settings
from sources.toplogger.client import GraphQLError, TopLoggerError, post_graphql

__all__ = [
    "AuthError",
    "RefreshTokenMissing",
    "clear_stored_token",
    "RefreshTokenRotationFailed",
    "SyncAlreadyRunning",
    "get_access_token",
    "sync_lock",
]

KEYRING_USERNAME = "refresh_token"

_QUERY_PATH = Path(__file__).parent / "queries" / "auth_signin_refresh_token.graphql"

# GraphQL error codes that mean "this refresh token is no longer good".
_DEAD_TOKEN_CODES = frozenset({"UNAUTHENTICATED", "UNAUTHORIZED", "FORBIDDEN"})

_MANUAL_LOGIN_INSTRUCTIONS = (
    "Log in to TopLogger in your browser, copy the refresh token from devtools, and "
    "paste it into .env as TOPLOGGER_REFRESH_TOKEN=... then re-run. "
    "(Logging in is manual by design — it needs a reCAPTCHA.) "
    "A token already in the keychain normally wins over .env; pasting a different one "
    "there overrides it, or call clear_stored_token() to wipe the keychain copy."
)


class AuthError(TopLoggerError):
    """Base class for auth failures. Never carries a token value."""


class RefreshTokenMissing(AuthError):
    """No refresh token in the keychain and none offered for bootstrap."""


class RefreshTokenExpired(AuthError):
    """The stored refresh token was rejected — expired, revoked or already used."""


class RefreshTokenRotationFailed(AuthError):
    """A new refresh token was issued but could not be stored.

    The worst case this module has: the old token is now dead and the new one was
    not saved, so the next run has nothing to use. Raised loudly with recovery
    instructions rather than swallowed.
    """


class SyncAlreadyRunning(AuthError):
    """Another process holds the sync lock."""


class _Bootstrap(BaseSettings):
    """Reads ``TOPLOGGER_REFRESH_TOKEN`` from the environment or ``.env``.

    Kept separate from ``config.Settings`` on purpose: ``Settings`` must stay
    credential-free so its ``repr`` can never leak one (there is a test asserting
    exactly that). This class exists only for the one-time import into the keychain.
    """

    model_config = SettingsConfigDict(
        env_prefix="TOPLOGGER_",
        env_file=".env",
        extra="ignore",
    )

    refresh_token: SecretStr | None = None


# Re-entrancy: Phase 2 wraps a whole sync in sync_lock(), and get_access_token()
# acquires it again from inside. flock() on a second file object in the same process
# would not block, but the bookkeeping would be wrong, so track depth explicitly.
_lock_state = threading.local()
_lock_guard = threading.Lock()


@contextmanager
def sync_lock(lock_path: Path | None = None):
    """Hold an exclusive lock for the duration of a sync (hard rule 3).

    Re-entrant within a process; a second *process* fails fast with
    :class:`SyncAlreadyRunning` rather than queueing up behind the first.

    POSIX only — ``fcntl.flock`` does not exist on Windows.
    """
    depth = getattr(_lock_state, "depth", 0)
    if depth:
        _lock_state.depth = depth + 1
        try:
            yield
        finally:
            _lock_state.depth -= 1
        return

    path = lock_path or (get_settings().data_dir / "sync.lock")
    path.parent.mkdir(parents=True, exist_ok=True)

    with _lock_guard:
        handle = path.open("w")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise SyncAlreadyRunning(
                f"another sync is already running (lock held on {path}). "
                "Wait for it to finish, or remove the lock file if no sync is active."
            ) from None

    _lock_state.depth = 1
    _lock_state.handle = handle
    try:
        yield
    finally:
        _lock_state.depth = 0
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            _lock_state.handle = None


def _read_stored_token() -> SecretStr | None:
    """Return the refresh token from the OS keychain, if one is stored."""
    stored = keyring.get_password(get_settings().keyring_service, KEYRING_USERNAME)
    return SecretStr(stored) if stored else None


def _store_token(token: SecretStr) -> None:
    """Write the refresh token to the OS keychain, replacing any previous value."""
    keyring.set_password(get_settings().keyring_service, KEYRING_USERNAME, token.get_secret_value())


def _bootstrap_token() -> SecretStr | None:
    """Return the refresh token pasted into ``.env``, if there is a non-blank one."""
    token = _Bootstrap().refresh_token
    if token is not None and token.get_secret_value().strip():
        return token
    return None


def clear_stored_token() -> None:
    """Delete the refresh token from the keychain.

    Recovery hatch: after this, the next run bootstraps from ``.env`` again.
    """
    try:
        keyring.delete_password(get_settings().keyring_service, KEYRING_USERNAME)
    except Exception:  # noqa: BLE001 - nothing stored is a fine outcome here
        pass


def _resolve_refresh_token() -> SecretStr:
    """Get the current refresh token, importing a bootstrap value if needed."""
    stored = _read_stored_token()
    if stored is not None:
        return stored

    bootstrap = _bootstrap_token()
    if bootstrap is not None:
        _store_token(bootstrap)
        print(
            "Imported refresh token into the keychain. "
            "You can now delete TOPLOGGER_REFRESH_TOKEN from .env — "
            "it rotates on every run and the value in .env is already stale."
        )
        return bootstrap

    raise RefreshTokenMissing(f"No refresh token stored. {_MANUAL_LOGIN_INSTRUCTIONS}")


def _refresh(refresh_token: SecretStr, **post_kwargs: Any) -> dict[str, Any]:
    """Exchange a refresh token for a new token pair."""
    query = _QUERY_PATH.read_text()
    try:
        data = post_graphql(
            query,
            {"refreshToken": refresh_token.get_secret_value()},
            # The web app sends the refresh token in both places. Whether the header
            # is needed at all is open question 6 in docs/PROJECT_CONTEXT.md §6 —
            # this mirrors the known-working shape rather than guessing.
            bearer=refresh_token,
            **post_kwargs,
        )
    except GraphQLError as exc:
        if _DEAD_TOKEN_CODES.intersection(exc.codes):
            raise RefreshTokenExpired(
                f"TopLogger rejected the stored refresh token. {_MANUAL_LOGIN_INSTRUCTIONS}"
            ) from None
        raise

    result = data.get("authSigninRefreshToken") or {}
    access = (result.get("access") or {}).get("token")
    refreshed = (result.get("refresh") or {}).get("token")
    if not access or not refreshed:
        raise AuthError("TopLogger's response did not contain both tokens")
    return {"access": SecretStr(access), "refresh": SecretStr(refreshed)}


def get_access_token(**post_kwargs: Any) -> SecretStr:
    """Return a fresh access token, rotating the stored refresh token.

    The new refresh token is persisted **before** this returns, so an interruption
    after the API call cannot leave the keychain holding a dead token.

    ``post_kwargs`` are forwarded to :func:`post_graphql` — tests use it to inject a
    mock transport. Production callers pass nothing.

    Raises:
        RefreshTokenMissing: Nothing stored and nothing to bootstrap from.
        RefreshTokenExpired: The stored token was rejected.
        RefreshTokenRotationFailed: A new token was issued but could not be stored.
    """
    with sync_lock():
        current = _resolve_refresh_token()
        try:
            tokens = _refresh(current, **post_kwargs)
        except RefreshTokenExpired:
            # The keychain copy takes precedence over .env, which is right in normal
            # operation but traps you after a failed run: pasting a fresh token into
            # .env would have no effect while a dead one sits in the keychain. If
            # .env offers a *different* token, that is a deliberate act — use it.
            fallback = _bootstrap_token()
            if fallback is None or fallback.get_secret_value() == current.get_secret_value():
                raise
            print(
                "Stored refresh token was rejected; retrying with the one in .env "
                "(TOPLOGGER_REFRESH_TOKEN)."
            )
            tokens = _refresh(fallback, **post_kwargs)

        try:
            _store_token(tokens["refresh"])
        except Exception as exc:
            raise RefreshTokenRotationFailed(
                "A new refresh token was issued but could not be saved to the keychain, "
                f"and the previous one is now dead ({type(exc).__name__}). "
                f"{_MANUAL_LOGIN_INSTRUCTIONS}"
            ) from None

        return tokens["access"]
