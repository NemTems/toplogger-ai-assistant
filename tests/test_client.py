"""Tests for the GraphQL transport.

Every request is served by ``httpx.MockTransport``. Nothing here touches the network
(hard rule 6).
"""

import base64

import httpx
import pytest
from pydantic import SecretStr

from sources.toplogger.client import (
    GraphQLError,
    TransportError,
    post_graphql,
    redact,
    reset_rate_limiter,
)

FAKE_TOKEN = "fake-bearer-token-aaa"  # noqa: S105 - obviously fake, not a credential


def fake_jwt() -> str:
    """A JWT-shaped string, assembled at runtime rather than written as a literal.

    ``test_repo_hygiene.py`` scans tracked files for ``eyJ...``; a hardcoded fake
    here would trip that scan and train us to ignore it.
    """
    segments = [
        base64.urlsafe_b64encode(part).decode().rstrip("=")
        for part in (b'{"alg":"HS256"}', b'{"sub":"1"}')
    ]
    return ".".join([*segments, "abcdefghijklmnop"])


@pytest.fixture(autouse=True)
def _clean_rate_limiter():
    reset_rate_limiter()
    yield
    reset_rate_limiter()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_returns_data_object():
    def handler(request):
        return httpx.Response(200, json={"data": {"thing": 1}})

    with _client(handler) as http:
        assert post_graphql("query {}", client=http, sleep=lambda _: None) == {"thing": 1}


def test_graphql_errors_raise_with_codes():
    def handler(request):
        return httpx.Response(
            200,
            json={"errors": [{"message": "nope", "extensions": {"code": "UNAUTHENTICATED"}}]},
        )

    with _client(handler) as http, pytest.raises(GraphQLError) as excinfo:
        post_graphql("query {}", client=http, sleep=lambda _: None)

    assert excinfo.value.codes == ["UNAUTHENTICATED"]


def test_retries_on_server_error_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"ok": True}})

    with _client(handler) as http:
        assert post_graphql("query {}", client=http, sleep=lambda _: None) == {"ok": True}
    assert len(calls) == 3


def test_does_not_retry_client_error():
    """A 4xx is a dead credential or a bad request — retrying only burns rate limit."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400)

    with _client(handler) as http, pytest.raises(TransportError):
        post_graphql("query {}", client=http, sleep=lambda _: None)

    assert len(calls) == 1


def test_authorization_header_is_sent():
    from pydantic import SecretStr

    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": {}})

    with _client(handler) as http:
        post_graphql("query {}", bearer=SecretStr(FAKE_TOKEN), client=http, sleep=lambda _: None)

    assert seen["auth"] == f"Bearer {FAKE_TOKEN}"


def test_rate_limiter_spaces_requests():
    """Hard rule 5: at most 1 request/second. Verified on a fake clock, not by waiting."""
    now = [0.0]
    slept = []

    def handler(request):
        return httpx.Response(200, json={"data": {}})

    with _client(handler) as http:
        for _ in range(3):
            post_graphql(
                "query {}",
                client=http,
                sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)),
                clock=lambda: now[0],
            )

    # First request is free; each subsequent one waits out the full interval.
    assert slept == [1.0, 1.0]


def test_non_json_body_is_a_transport_error():
    def handler(request):
        return httpx.Response(200, text="<html>maintenance</html>")

    with _client(handler) as http, pytest.raises(TransportError):
        post_graphql("query {}", client=http, sleep=lambda _: None)


def test_redact_masks_token_shaped_strings():
    jwt = fake_jwt()
    assert jwt not in redact(f"failed with {jwt}")
    assert "<REDACTED>" in redact(f"failed with {jwt}")


def test_graphql_error_message_redacts_tokens():
    """An API error that echoes the token back must not put it in our exception."""
    jwt = fake_jwt()

    def handler(request):
        return httpx.Response(
            200,
            json={"errors": [{"message": f"bad token {jwt}", "extensions": {"code": "BAD"}}]},
        )

    with _client(handler) as http, pytest.raises(GraphQLError) as excinfo:
        post_graphql("query {}", client=http, sleep=lambda _: None)

    assert jwt not in str(excinfo.value)
    assert jwt not in repr(excinfo.value)


def test_redact_masks_a_known_short_secret():
    """Shape matching cannot see a short opaque token — pass it in by value."""
    assert FAKE_TOKEN not in redact(f"rejected {FAKE_TOKEN}", SecretStr(FAKE_TOKEN))
    assert FAKE_TOKEN not in redact(f"rejected {FAKE_TOKEN}", FAKE_TOKEN)


def test_redact_ignores_an_empty_secret():
    """An empty or absurdly short 'secret' must not shred the whole message."""
    assert redact("plain message", "", None, "abc") == "plain message"


def test_graphql_error_redacts_the_bearer_token():
    """A short bearer token echoed back in a message must not reach the exception."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "errors": [{"message": f"bad token {FAKE_TOKEN}", "extensions": {"code": "BAD"}}]
            },
        )

    with _client(handler) as http, pytest.raises(GraphQLError) as excinfo:
        post_graphql("query {}", bearer=SecretStr(FAKE_TOKEN), client=http, sleep=lambda _: None)

    assert FAKE_TOKEN not in str(excinfo.value)
    assert FAKE_TOKEN not in repr(excinfo.value)


def test_graphql_error_redacts_the_code():
    """``extensions.code`` is untrusted input like the message is."""
    jwt = fake_jwt()

    def handler(request):
        return httpx.Response(200, json={"errors": [{"message": "x", "extensions": {"code": jwt}}]})

    with _client(handler) as http, pytest.raises(GraphQLError) as excinfo:
        post_graphql("query {}", client=http, sleep=lambda _: None)

    assert jwt not in str(excinfo.value)
    assert jwt not in excinfo.value.codes


def test_retry_false_makes_a_single_attempt_on_server_error():
    """Non-idempotent operations must not be replayed. See client.post_graphql."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    with _client(handler) as http, pytest.raises(TransportError):
        post_graphql("query {}", client=http, retry=False, sleep=lambda _: None)

    assert len(calls) == 1


def test_retry_false_makes_a_single_attempt_on_transport_error():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout("boom")

    with _client(handler) as http, pytest.raises(TransportError):
        post_graphql("query {}", client=http, retry=False, sleep=lambda _: None)

    assert len(calls) == 1


def test_scalar_json_body_is_a_transport_error():
    """A bare JSON scalar is not a GraphQL response — not an AttributeError either."""

    def handler(request):
        return httpx.Response(200, json="maintenance")

    with _client(handler) as http, pytest.raises(TransportError):
        post_graphql("query {}", client=http, sleep=lambda _: None)


def test_batched_response_is_rejected():
    def handler(request):
        return httpx.Response(200, json=[{"data": {}}])

    with _client(handler) as http, pytest.raises(TransportError, match="batched"):
        post_graphql("query {}", client=http, sleep=lambda _: None)
