"""Tests for the GraphQL transport.

Every request is served by ``httpx.MockTransport``. Nothing here touches the network
(hard rule 6).
"""

import httpx
import pytest

from sources.toplogger.client import (
    GraphQLError,
    TransportError,
    post_graphql,
    redact,
    reset_rate_limiter,
)

FAKE_TOKEN = "fake-bearer-token-aaa"  # noqa: S105 - obviously fake, not a credential


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
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop"
    assert jwt not in redact(f"failed with {jwt}")
    assert "<REDACTED>" in redact(f"failed with {jwt}")


def test_graphql_error_message_redacts_tokens():
    """An API error that echoes the token back must not put it in our exception."""
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop"

    def handler(request):
        return httpx.Response(
            200,
            json={"errors": [{"message": f"bad token {jwt}", "extensions": {"code": "BAD"}}]},
        )

    with _client(handler) as http, pytest.raises(GraphQLError) as excinfo:
        post_graphql("query {}", client=http, sleep=lambda _: None)

    assert jwt not in str(excinfo.value)
    assert jwt not in repr(excinfo.value)
