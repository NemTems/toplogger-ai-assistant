"""Tests for the cached access-token provider.

The keychain is a dict and HTTP is ``httpx.MockTransport``; nothing touches the real
keychain or the network (hard rule 6). Fixture tokens are deliberately not
JWT-shaped — see ``tests/test_auth.py``.
"""

from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from config import get_settings
from ingest.tokens import DEFAULT_TTL_SECONDS, AccessTokenProvider
from sources.toplogger import auth
from sources.toplogger.client import reset_rate_limiter

SEED_REFRESH = "fake-refresh-token-seed"  # noqa: S105


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch):
    """Replace the OS keychain with a dict, seeded with a usable refresh token."""
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(auth.keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(auth.keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    store[(get_settings().keyring_service, auth.KEYRING_USERNAME)] = SEED_REFRESH
    reset_rate_limiter()
    yield store
    reset_rate_limiter()


def stored(store) -> str | None:
    return store.get((get_settings().keyring_service, auth.KEYRING_USERNAME))


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def rotating_handler(calls: list[int]):
    """Answer every refresh with a fresh pair, as TopLogger does, and count the calls."""

    def handler(request):
        calls.append(1)
        n = len(calls)
        return httpx.Response(
            200,
            json={
                "data": {
                    "authSigninRefreshToken": {
                        "access": {"token": f"fake-access-token-{n:03d}"},
                        "refresh": {"token": f"fake-refresh-token-{n:03d}"},
                    }
                }
            },
        )

    return handler


def provider(handler, http, clock, **kwargs) -> AccessTokenProvider:
    return AccessTokenProvider(clock=clock, client=http, sleep=lambda _: None, **kwargs)


def test_returns_the_access_token():
    calls: list[int] = []
    with httpx.Client(transport=httpx.MockTransport(rotating_handler(calls))) as http:
        token = provider(None, http, FakeClock()).get()

    assert token.get_secret_value() == "fake-access-token-001"


def test_many_gets_acquire_exactly_one_token():
    """The whole point: a sync of hundreds of requests rotates the credential once."""
    calls: list[int] = []
    clock = FakeClock()

    with httpx.Client(transport=httpx.MockTransport(rotating_handler(calls))) as http:
        p = provider(None, http, clock)
        tokens = [p.get().get_secret_value() for _ in range(50)]

    assert len(calls) == 1, "get_access_token must be called once, not once per request"
    assert p.acquisitions == 1
    assert set(tokens) == {"fake-access-token-001"}


def test_one_acquisition_rotates_the_refresh_token_once(fake_keychain):
    """Hard rule 3: every acquisition burns a refresh token, so count them."""
    calls: list[int] = []

    with httpx.Client(transport=httpx.MockTransport(rotating_handler(calls))) as http:
        p = provider(None, http, FakeClock())
        for _ in range(10):
            p.get()

    assert stored(fake_keychain) == "fake-refresh-token-001"
    assert p.acquisitions == 1


def test_reacquires_once_the_token_has_aged_out():
    calls: list[int] = []
    clock = FakeClock()

    with httpx.Client(transport=httpx.MockTransport(rotating_handler(calls))) as http:
        p = provider(None, http, clock, ttl_seconds=480.0)
        first = p.get()
        clock.advance(480.0)
        second = p.get()

    assert first.get_secret_value() == "fake-access-token-001"
    assert second.get_secret_value() == "fake-access-token-002"
    assert p.acquisitions == 2


def test_does_not_reacquire_before_the_ttl():
    calls: list[int] = []
    clock = FakeClock()

    with httpx.Client(transport=httpx.MockTransport(rotating_handler(calls))) as http:
        p = provider(None, http, clock, ttl_seconds=480.0)
        p.get()
        clock.advance(479.0)
        p.get()

    assert p.acquisitions == 1


def test_default_ttl_leaves_headroom_under_the_ten_minute_expiry():
    assert DEFAULT_TTL_SECONDS < 600.0


def test_acquisitions_starts_at_zero():
    p = AccessTokenProvider()
    assert p.acquisitions == 0


def test_get_is_usable_as_a_bare_token_supplier():
    """The adapter takes a ``Callable[[], SecretStr]``; the bound method must fit."""
    calls: list[int] = []

    with httpx.Client(transport=httpx.MockTransport(rotating_handler(calls))) as http:
        supplier: Callable[[], SecretStr] = provider(None, http, FakeClock()).get
        token = supplier()

    assert token.get_secret_value() == "fake-access-token-001"


def test_a_failed_acquisition_is_not_cached(fake_keychain):
    """A rejected refresh must surface, not be swallowed into a half-filled cache."""

    def rejecting(request):
        return httpx.Response(
            200,
            json={
                "errors": [
                    {"message": "not authenticated", "extensions": {"code": "UNAUTHENTICATED"}}
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(rejecting)) as http:
        p = provider(None, http, FakeClock())
        with pytest.raises(auth.RefreshTokenExpired):
            p.get()

        assert p.acquisitions == 0


def test_provider_never_renders_the_token():
    """Hard rule 1: not in repr, not in str."""
    calls: list[int] = []

    with httpx.Client(transport=httpx.MockTransport(rotating_handler(calls))) as http:
        p = provider(None, http, FakeClock())
        token = p.get()

    assert token.get_secret_value() not in repr(p)
    assert token.get_secret_value() not in str(p)
    assert token.get_secret_value() not in repr(token)
