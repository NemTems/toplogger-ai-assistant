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


def redact(text: str) -> str:
    """Replace anything token-shaped in ``text`` with ``<REDACTED>``."""
    return _TOKEN_SHAPED.sub("<REDACTED>", text)


class TopLoggerError(Exception):
    """Base class for every error raised against the TopLogger API."""


class TransportError(TopLoggerError):
    """A request failed at the HTTP level after exhausting retries."""


class GraphQLError(TopLoggerError):
    """The endpoint returned 200 with a non-empty ``errors`` array.

    Carries the GraphQL error ``codes`` (e.g. ``UNAUTHENTICATED``) so callers can
    branch on them without parsing message text.
    """

    def __init__(self, messages: list[str], codes: list[str]) -> None:
        self.codes = codes
        self.messages = [redact(m) for m in messages]
        detail = "; ".join(self.messages) or "no message"
        super().__init__(f"GraphQL error {codes or ['<no code>']}: {detail}")


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
    client: httpx.Client | None = None,
    sleep=time.sleep,
    clock=time.monotonic,
) -> dict[str, Any]:
    """POST one GraphQL operation and return its ``data`` object.

    Args:
        query: The GraphQL document.
        variables: Operation variables, if any.
        bearer: Token for the ``Authorization`` header. Never logged.
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

    owns_client = client is None
    http = client or httpx.Client(timeout=settings.request_timeout_seconds)
    try:
        last_error: Exception | None = None
        for attempt in range(settings.max_retries + 1):
            _throttle(min_interval, sleep=sleep, clock=clock)
            try:
                response = http.post(settings.graphql_url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                # Connect errors and timeouts are transient: retry.
                last_error = exc
                if attempt < settings.max_retries:
                    sleep(2**attempt)
                    continue
                raise TransportError(
                    f"request to TopLogger failed after {attempt + 1} attempts: "
                    f"{type(exc).__name__}"
                ) from None

            if response.status_code >= 500:
                last_error = TransportError(f"server error {response.status_code}")
                if attempt < settings.max_retries:
                    sleep(2**attempt)
                    continue
                raise TransportError(
                    f"TopLogger returned {response.status_code} after {attempt + 1} attempts"
                )

            if response.status_code >= 400:
                # A 4xx is a request we got wrong, or a dead credential. Retrying
                # burns requests against a rate limit for no possible gain.
                raise TransportError(f"TopLogger returned {response.status_code}")

            return _parse(response)

        raise TransportError("retry loop exhausted") from last_error
    finally:
        if owns_client:
            http.close()


def _parse(response: httpx.Response) -> dict[str, Any]:
    """Pull ``data`` out of a 200 response, raising on a GraphQL ``errors`` array."""
    try:
        body = response.json()
    except ValueError:
        raise TransportError("TopLogger returned a non-JSON body") from None

    if isinstance(body, list):
        # Batched responses are a Phase 2 concern; reject rather than guess.
        raise TransportError("unexpected batched response for a single operation")

    errors = body.get("errors") or []
    if errors:
        messages = [str(e.get("message", "")) for e in errors]
        codes = [
            str(e["extensions"]["code"])
            for e in errors
            if isinstance(e.get("extensions"), dict) and "code" in e["extensions"]
        ]
        raise GraphQLError(messages, codes)

    data = body.get("data")
    if data is None:
        raise TransportError("TopLogger returned no data and no errors")
    return data
