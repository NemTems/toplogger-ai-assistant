"""GraphQL transport for TopLogger: rate limiting, retries and error mapping.

Deliberately thin. Phase 2 extends this with request batching and aliases
(``c1: climb(...) c2: climb(...)``); Phase 1 only needs a single mutation.

Nothing in this module may emit a token. Request headers and response bodies both
carry credentials, so error types here carry status codes and GraphQL error codes
only, and every string that escapes is passed through :func:`redact` (AGENTS.md
hard rule 1).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import SecretStr

from config import get_settings

__all__ = [
    "GraphQLError",
    "TopLoggerError",
    "TransportError",
    "post_graphql",
    "redact",
]

# Anything JWT-shaped, or any long opaque run that could be a token. Deliberately
# broad: a false positive costs a less readable error message, a false negative
# leaks a credential into a log.
_TOKEN_SHAPED = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){0,2}|[A-Za-z0-9_-]{40,}")

# Shape matching alone cannot catch a short opaque credential, so the secrets we
# actually sent are masked by value too. The floor stops a stray empty or
# one-character "secret" from shredding the whole message.
_MIN_SECRET_LENGTH = 8


def redact(text: str, *secrets: SecretStr | str | None) -> str:
    """Replace known ``secrets`` and anything token-shaped in ``text`` with ``<REDACTED>``.

    Shape matching is the backstop, not the guarantee: pass the credentials the
    request actually carried so a short opaque token is masked as well.
    """
    for secret in secrets:
        value = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        if value and len(value) >= _MIN_SECRET_LENGTH:
            text = text.replace(value, "<REDACTED>")
    return _TOKEN_SHAPED.sub("<REDACTED>", text)


class TopLoggerError(Exception):
    """Base class for every error raised against the TopLogger API."""


class TransportError(TopLoggerError):
    """A request failed at the HTTP level after exhausting retries."""


class GraphQLError(TopLoggerError):
    """The endpoint returned 200 with a non-empty ``errors`` array.

    Carries the GraphQL error ``codes`` (e.g. ``UNAUTHENTICATED``) so callers can
    branch on them without parsing message text. Codes come off the wire like
    messages do, so they are redacted too — a real code never looks token-shaped,
    so branching on them is unaffected.
    """

    def __init__(
        self,
        messages: list[str],
        codes: list[str],
        secrets: Sequence[SecretStr | str] = (),
    ) -> None:
        self.codes = [redact(c, *secrets) for c in codes]
        self.messages = [redact(m, *secrets) for m in messages]
        detail = "; ".join(self.messages) or "no message"
        super().__init__(f"GraphQL error {self.codes or ['<no code>']}: {detail}")


# Rate limiting is process-wide: one lock, one clock. Hard rule 5 caps us at
# 1 request/second and forbids parallel crawling, so serialising here is the point,
# not a limitation.
_rate_lock = threading.Lock()
_last_request_at: float | None = None


def _throttle(min_interval: float, *, sleep=time.sleep, clock=time.monotonic) -> None:
    """Block until at least ``min_interval`` seconds have passed since the last request.

    ``sleep`` and ``clock`` are injectable so tests can assert the spacing without
    actually waiting.
    """
    global _last_request_at
    with _rate_lock:
        now = clock()
        if _last_request_at is not None:
            wait = min_interval - (now - _last_request_at)
            if wait > 0:
                sleep(wait)
                now = clock()
        _last_request_at = now


def reset_rate_limiter() -> None:
    """Forget the last-request timestamp. For tests only."""
    global _last_request_at
    with _rate_lock:
        _last_request_at = None


def post_graphql(
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    bearer: SecretStr | None = None,
    secrets: Sequence[SecretStr | str] = (),
    retry: bool = True,
    client: httpx.Client | None = None,
    sleep=time.sleep,
    clock=time.monotonic,
) -> dict[str, Any]:
    """POST one GraphQL operation and return its ``data`` object.

    Args:
        query: The GraphQL document.
        variables: Operation variables, if any.
        bearer: Token for the ``Authorization`` header. Never logged.
        secrets: Credentials this request carries besides ``bearer`` (e.g. a token
            passed as a variable), masked out of any error this raises.
        retry: ``False`` for a non-idempotent operation, where replaying a request
            whose response was lost does real damage — refresh-token rotation
            being the case that matters here. The call then fails after one
            attempt instead of retrying transient errors.
        client: An ``httpx.Client`` to use instead of creating one. Tests pass a
            client built on ``httpx.MockTransport``; nothing else should pass this.

    Raises:
        TransportError: HTTP-level failure that survived the configured retries.
        GraphQLError: The response carried a GraphQL ``errors`` array.
    """
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer.get_secret_value()}"

    payload: dict[str, Any] = {"query": query, "variables": variables or {}}
    min_interval = 1.0 / settings.rate_limit_per_second
    all_secrets = [*secrets, bearer] if bearer is not None else list(secrets)
    last_attempt = settings.max_retries if retry else 0

    owns_client = client is None
    http = client or httpx.Client(timeout=settings.request_timeout_seconds)
    try:
        last_error: Exception | None = None
        for attempt in range(last_attempt + 1):
            _throttle(min_interval, sleep=sleep, clock=clock)
            try:
                response = http.post(settings.graphql_url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                # Connect errors and timeouts are transient: retry, unless the
                # operation is one we must not replay.
                last_error = exc
                if attempt < last_attempt:
                    sleep(2**attempt)
                    continue
                raise TransportError(
                    f"request to TopLogger failed after {_attempts(attempt)}: {type(exc).__name__}"
                ) from None

            if response.status_code >= 500:
                last_error = TransportError(f"server error {response.status_code}")
                if attempt < last_attempt:
                    sleep(2**attempt)
                    continue
                raise TransportError(
                    f"TopLogger returned {response.status_code} after {_attempts(attempt)}"
                )

            if response.status_code >= 400:
                # A 4xx is a request we got wrong, or a dead credential. Retrying
                # burns requests against a rate limit for no possible gain.
                raise TransportError(f"TopLogger returned {response.status_code}")

            return _parse(response, all_secrets)

        raise TransportError("retry loop exhausted") from last_error
    finally:
        if owns_client:
            http.close()


def _attempts(attempt: int) -> str:
    """``"1 attempt"`` / ``"3 attempts"`` for an error message."""
    count = attempt + 1
    return f"{count} attempt{'' if count == 1 else 's'}"


def _parse(
    response: httpx.Response,
    secrets: Sequence[SecretStr | str] = (),
) -> dict[str, Any]:
    """Pull ``data`` out of a 200 response, raising on a GraphQL ``errors`` array."""
    try:
        body = response.json()
    except ValueError:
        raise TransportError("TopLogger returned a non-JSON body") from None

    if not isinstance(body, dict):
        if isinstance(body, list):
            # Batched responses are a Phase 2 concern; reject rather than guess.
            raise TransportError("unexpected batched response for a single operation")
        # A bare JSON scalar is not a GraphQL response; say so instead of
        # tripping over AttributeError below.
        raise TransportError(f"TopLogger returned an unexpected JSON body ({type(body).__name__})")

    errors = body.get("errors") or []
    if errors:
        messages = [str(e.get("message", "")) for e in errors]
        codes = [
            str(e["extensions"]["code"])
            for e in errors
            if isinstance(e.get("extensions"), dict) and "code" in e["extensions"]
        ]
        raise GraphQLError(messages, codes, secrets)

    data = body.get("data")
    if data is None:
        raise TransportError("TopLogger returned no data and no errors")
    return data
