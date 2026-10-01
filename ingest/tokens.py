"""One access token per sync.

:func:`sources.toplogger.auth.get_access_token` rotates the stored refresh token on
every call, and a crash between TopLogger issuing the new token and the keychain
accepting it costs a manual browser login. Do not call it per request.

A sync acquires once and reuses the token. The access token lives 10 minutes and
``get_access_token`` does not return its expiry, so the cache is timed locally with
headroom.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from pydantic import SecretStr

from sources.toplogger import auth

__all__ = ["DEFAULT_TTL_SECONDS", "AccessTokenProvider"]

DEFAULT_TTL_SECONDS = 480.0
"""Eight minutes: two minutes of headroom under TopLogger's ten-minute access token."""


class AccessTokenProvider:
    """Hands out an access token, acquiring a new one only when the old one ages out.

    :meth:`get` is a bare ``Callable[[], SecretStr]``, which is what the adapter wants
    as its ``token_supplier`` — it asks for a token per request and this decides
    whether that costs a rotation.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        **post_kwargs: Any,
    ) -> None:
        """
        Args:
            ttl_seconds: How long a token is reused before being re-acquired. Must
                stay under the 10-minute server-side lifetime.
            clock: Monotonic-style clock, injectable for tests. Note this is the
                *cache's* clock; it is not forwarded to the transport.
            post_kwargs: Forwarded to :func:`auth.get_access_token` and on to
                ``post_graphql`` — tests inject a mock transport this way.
                Production callers pass nothing.
        """
        self._ttl = ttl_seconds
        self._clock = clock
        self._post_kwargs = post_kwargs
        self._token: SecretStr | None = None
        self._acquired_at = 0.0
        self._acquisitions = 0
        self._lock = threading.Lock()

    def get(self) -> SecretStr:
        """Return a usable access token, acquiring one if the cached one has aged out.

        Raises:
            AuthError: Whatever :func:`auth.get_access_token` raises — a missing,
                expired or unstorable refresh token.
        """
        with self._lock:
            if self._token is None or (self._clock() - self._acquired_at) >= self._ttl:
                # Timed from before the call: the ten minutes start when TopLogger
                # issues the token.
                started = self._clock()
                token = auth.get_access_token(**self._post_kwargs)
                # Only on success — a failed refresh must not reset the cache's age
                # and leave a stale token looking fresh.
                self._token = token
                self._acquired_at = started
                self._acquisitions += 1
            return self._token

    @property
    def acquisitions(self) -> int:
        """How many times a token was actually fetched (i.e. refresh tokens rotated)."""
        return self._acquisitions
