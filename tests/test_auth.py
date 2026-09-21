"""Tests for the refresh-token flow.

The keychain is replaced with an in-memory dict and HTTP with
``httpx.MockTransport``; nothing touches the real keychain or the network
(hard rule 6).

Fixture tokens are deliberately NOT JWT-shaped: ``tests/`` is tracked and scanned by
``test_repo_hygiene.py``, whose ``eyJ...`` pattern would fire on a realistic fake.
"""

import multiprocessing

import httpx
import pytest

from sources.toplogger import auth
from sources.toplogger.client import reset_rate_limiter

OLD_REFRESH = "fake-refresh-token-old"  # noqa: S105
NEW_REFRESH = "fake-refresh-token-new"  # noqa: S105
NEW_ACCESS = "fake-access-token-aaa"  # noqa: S105


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch):
    """Replace the OS keychain with a dict, and reset transport state per test."""
    store: dict[tuple[str, str], str] = {}

    monkeypatch.setattr(auth.keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(auth.keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    reset_rate_limiter()
    yield store
    reset_rate_limiter()


def _ok_handler(request):
    return httpx.Response(
        200,
        json={
            "data": {
                "authSigninRefreshToken": {
                    "access": {"token": NEW_ACCESS, "expiresAt": "2026-01-01T00:10:00Z"},
                    "refresh": {"token": NEW_REFRESH, "expiresAt": "2026-01-15T00:00:00Z"},
                }
            }
        },
    )


def _call(handler, **kwargs):
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        return auth.get_access_token(client=http, sleep=lambda _: None, **kwargs)


def _seed(store, token=OLD_REFRESH):
    from config import get_settings

    store[(get_settings().keyring_service, auth.KEYRING_USERNAME)] = token


def _stored(store):
    from config import get_settings

    return store.get((get_settings().keyring_service, auth.KEYRING_USERNAME))


def test_returns_access_token_and_rotates_refresh(fake_keychain):
    _seed(fake_keychain)

    access = _call(_ok_handler)

    assert access.get_secret_value() == NEW_ACCESS
    assert _stored(fake_keychain) == NEW_REFRESH, "new refresh token must replace the old one"


def test_old_refresh_token_is_gone_after_rotation(fake_keychain):
    _seed(fake_keychain)
    _call(_ok_handler)
    assert _stored(fake_keychain) != OLD_REFRESH


def test_new_token_is_persisted_before_returning(fake_keychain, monkeypatch):
    """The 'killed mid-run' case: crash right after the write, token must survive.

    Hard rule 3 is about ordering. If the store happened after the return — or after
    any further work — an interruption here would leave a dead token behind.
    """
    _seed(fake_keychain)

    real_store = auth._store_token
    observed = {}

    def store_then_die(token):
        real_store(token)
        observed["stored"] = _stored(fake_keychain)
        raise KeyboardInterrupt("simulated kill immediately after the write")

    monkeypatch.setattr(auth, "_store_token", store_then_die)

    with pytest.raises(KeyboardInterrupt):
        _call(_ok_handler)

    assert observed["stored"] == NEW_REFRESH
    assert _stored(fake_keychain) == NEW_REFRESH


def test_bootstrap_imports_env_token_into_keychain(fake_keychain, monkeypatch, capsys):
    monkeypatch.setenv("TOPLOGGER_REFRESH_TOKEN", OLD_REFRESH)
    assert _stored(fake_keychain) is None

    access = _call(_ok_handler)

    assert access.get_secret_value() == NEW_ACCESS
    assert _stored(fake_keychain) == NEW_REFRESH
    assert "delete TOPLOGGER_REFRESH_TOKEN from .env" in capsys.readouterr().out


def test_keychain_wins_over_stale_env_value(fake_keychain, monkeypatch):
    """After bootstrap the .env value is stale; the keychain must take precedence."""
    _seed(fake_keychain, "fake-refresh-token-current")
    monkeypatch.setenv("TOPLOGGER_REFRESH_TOKEN", "fake-refresh-token-stale")

    sent = {}

    def handler(request):
        import json

        sent["refreshToken"] = json.loads(request.content)["variables"]["refreshToken"]
        return _ok_handler(request)

    _call(handler)
    assert sent["refreshToken"] == "fake-refresh-token-current"


def test_missing_token_raises_with_instructions(fake_keychain, monkeypatch):
    monkeypatch.delenv("TOPLOGGER_REFRESH_TOKEN", raising=False)

    with pytest.raises(auth.RefreshTokenMissing) as excinfo:
        _call(_ok_handler)

    assert "browser" in str(excinfo.value).lower()


def test_blank_env_token_is_not_treated_as_a_token(fake_keychain, monkeypatch):
    monkeypatch.setenv("TOPLOGGER_REFRESH_TOKEN", "   ")

    with pytest.raises(auth.RefreshTokenMissing):
        _call(_ok_handler)


def test_rejected_token_raises_expired_with_instructions(fake_keychain):
    _seed(fake_keychain)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "errors": [
                    {"message": "not authenticated", "extensions": {"code": "UNAUTHENTICATED"}}
                ]
            },
        )

    with pytest.raises(auth.RefreshTokenExpired) as excinfo:
        _call(handler)

    message = str(excinfo.value)
    assert "browser" in message.lower()
    assert OLD_REFRESH not in message, "an error must never echo the token"


def test_failure_to_store_new_token_is_loud(fake_keychain, monkeypatch):
    """Worst case: old token dead, new one unsaveable. Must not be swallowed."""
    _seed(fake_keychain)

    def boom(token):
        raise OSError("keychain locked")

    monkeypatch.setattr(auth, "_store_token", boom)

    with pytest.raises(auth.RefreshTokenRotationFailed) as excinfo:
        _call(_ok_handler)

    assert "browser" in str(excinfo.value).lower()


def test_no_token_value_appears_in_any_auth_error(fake_keychain):
    """Hard rule 1: tokens never appear in exceptions."""
    _seed(fake_keychain)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "errors": [
                    {
                        "message": f"rejected {OLD_REFRESH}",
                        "extensions": {"code": "UNAUTHENTICATED"},
                    }
                ]
            },
        )

    with pytest.raises(auth.RefreshTokenExpired) as excinfo:
        _call(handler)

    assert OLD_REFRESH not in str(excinfo.value)
    assert OLD_REFRESH not in repr(excinfo.value)


def test_secret_str_does_not_leak_in_repr(fake_keychain):
    _seed(fake_keychain)
    access = _call(_ok_handler)
    assert NEW_ACCESS not in repr(access)
    assert NEW_ACCESS not in str(access)


# --- sync lock -------------------------------------------------------------------


def test_lock_is_reentrant_in_process(tmp_path):
    """Phase 2 wraps a whole sync; get_access_token() acquires again from inside."""
    lock = tmp_path / "sync.lock"
    with auth.sync_lock(lock), auth.sync_lock(lock):
        pass  # must not deadlock


def test_lock_released_after_block(tmp_path):
    lock = tmp_path / "sync.lock"
    with auth.sync_lock(lock):
        pass
    with auth.sync_lock(lock):
        pass


def _grab_lock(path, ready, result):
    """Child process: try to take the lock and report what happened."""
    from sources.toplogger import auth as child_auth

    try:
        with child_auth.sync_lock(path):
            result.value = 1
    except child_auth.SyncAlreadyRunning:
        result.value = 2
    except Exception:
        result.value = 3
    ready.set()


def test_second_process_fails_fast(tmp_path):
    """Two syncs must never overlap (hard rule 3)."""
    lock = tmp_path / "sync.lock"
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    result = ctx.Value("i", 0)

    with auth.sync_lock(lock):
        proc = ctx.Process(target=_grab_lock, args=(lock, ready, result))
        proc.start()
        proc.join(timeout=30)

    assert result.value == 2, "second process should raise SyncAlreadyRunning, not block"


# --- recovery from a dead token in the keychain ------------------------------------


def _rejecting_handler(request):
    """Reject whatever token is sent, as TopLogger does for a dead one."""
    return httpx.Response(
        200,
        json={
            "errors": [{"message": "not authenticated", "extensions": {"code": "UNAUTHENTICATED"}}]
        },
    )


def test_fresh_env_token_overrides_dead_keychain_token(fake_keychain, monkeypatch, capsys):
    """The trap this fixes: a dead token in the keychain shadowing a fresh paste.

    After a failed run the keychain can hold a rejected token. Pasting a new one into
    .env is the obvious fix, and it must actually take effect.
    """
    _seed(fake_keychain, "fake-refresh-token-dead")
    monkeypatch.setenv("TOPLOGGER_REFRESH_TOKEN", "fake-refresh-token-fresh")

    seen = []

    def handler(request):
        import json

        sent = json.loads(request.content)["variables"]["refreshToken"]
        seen.append(sent)
        if sent == "fake-refresh-token-dead":
            return _rejecting_handler(request)
        return _ok_handler(request)

    access = _call(handler)

    assert seen == ["fake-refresh-token-dead", "fake-refresh-token-fresh"]
    assert access.get_secret_value() == NEW_ACCESS
    assert _stored(fake_keychain) == NEW_REFRESH
    assert "retrying with the one in .env" in capsys.readouterr().out


def test_no_pointless_retry_when_env_holds_the_same_dead_token(fake_keychain, monkeypatch):
    """If .env repeats the token that was just rejected, don't burn a second request."""
    _seed(fake_keychain, "fake-refresh-token-dead")
    monkeypatch.setenv("TOPLOGGER_REFRESH_TOKEN", "fake-refresh-token-dead")

    calls = []

    def handler(request):
        calls.append(1)
        return _rejecting_handler(request)

    with pytest.raises(auth.RefreshTokenExpired):
        _call(handler)

    assert len(calls) == 1


def test_no_fallback_when_env_is_empty(fake_keychain, monkeypatch):
    _seed(fake_keychain, "fake-refresh-token-dead")
    monkeypatch.delenv("TOPLOGGER_REFRESH_TOKEN", raising=False)

    with pytest.raises(auth.RefreshTokenExpired):
        _call(_rejecting_handler)


def test_clear_stored_token_removes_it(fake_keychain, monkeypatch):
    deleted = []
    monkeypatch.setattr(auth.keyring, "delete_password", lambda s, u: deleted.append((s, u)))
    _seed(fake_keychain)

    auth.clear_stored_token()

    assert deleted, "clear_stored_token must call through to the keychain"


def test_expired_error_explains_the_keychain_precedence(fake_keychain, monkeypatch):
    monkeypatch.delenv("TOPLOGGER_REFRESH_TOKEN", raising=False)
    _seed(fake_keychain, "fake-refresh-token-dead")

    with pytest.raises(auth.RefreshTokenExpired) as excinfo:
        _call(_rejecting_handler)

    assert "keychain" in str(excinfo.value).lower()
