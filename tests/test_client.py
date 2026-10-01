"""Tests for the GraphQL transport.

Every request is served by ``httpx.MockTransport``. Nothing here touches the network.
"""

import base64
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from sources.toplogger.client import (
    ForbiddenOperationError,
    GraphQLError,
    TransportError,
    check_operation,
    post_graphql,
    redact,
    reset_rate_limiter,
)

FAKE_TOKEN = "fake-bearer-token-aaa"  # noqa: S105 - obviously fake, not a credential

# Any allowlisted operation will do for tests about the transport itself; the
# allowlist has its own section at the bottom of this file.
DOC = "query Catalog { climbs { data { id } } }"


def fake_jwt() -> str:
    """A JWT-shaped string, assembled at runtime.

    ``test_repo_hygiene.py`` fails on an ``eyJ...`` literal in any tracked file.
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
        assert post_graphql(DOC, client=http, sleep=lambda _: None) == {"thing": 1}


def test_graphql_errors_raise_with_codes():
    def handler(request):
        return httpx.Response(
            200,
            json={"errors": [{"message": "nope", "extensions": {"code": "UNAUTHENTICATED"}}]},
        )

    with _client(handler) as http, pytest.raises(GraphQLError) as excinfo:
        post_graphql(DOC, client=http, sleep=lambda _: None)

    assert excinfo.value.codes == ["UNAUTHENTICATED"]


def test_retries_on_server_error_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"ok": True}})

    with _client(handler) as http:
        assert post_graphql(DOC, client=http, sleep=lambda _: None) == {"ok": True}
    assert len(calls) == 3


def test_does_not_retry_client_error():
    """A 4xx is a dead credential or a bad request — retrying only burns rate limit."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400)

    with _client(handler) as http, pytest.raises(TransportError):
        post_graphql(DOC, client=http, sleep=lambda _: None)

    assert len(calls) == 1


def test_authorization_header_is_sent():
    from pydantic import SecretStr

    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": {}})

    with _client(handler) as http:
        post_graphql(DOC, bearer=SecretStr(FAKE_TOKEN), client=http, sleep=lambda _: None)

    assert seen["auth"] == f"Bearer {FAKE_TOKEN}"


def test_rate_limiter_spaces_requests():
    """At most 1 request/second, measured on a fake clock."""
    now = [0.0]
    slept = []

    def handler(request):
        return httpx.Response(200, json={"data": {}})

    with _client(handler) as http:
        for _ in range(3):
            post_graphql(
                DOC,
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
        post_graphql(DOC, client=http, sleep=lambda _: None)


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
        post_graphql(DOC, client=http, sleep=lambda _: None)

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
        post_graphql(DOC, bearer=SecretStr(FAKE_TOKEN), client=http, sleep=lambda _: None)

    assert FAKE_TOKEN not in str(excinfo.value)
    assert FAKE_TOKEN not in repr(excinfo.value)


def test_graphql_error_redacts_the_code():
    """``extensions.code`` is untrusted input like the message is."""
    jwt = fake_jwt()

    def handler(request):
        return httpx.Response(200, json={"errors": [{"message": "x", "extensions": {"code": jwt}}]})

    with _client(handler) as http, pytest.raises(GraphQLError) as excinfo:
        post_graphql(DOC, client=http, sleep=lambda _: None)

    assert jwt not in str(excinfo.value)
    assert jwt not in excinfo.value.codes


def test_retry_false_makes_a_single_attempt_on_server_error():
    """Non-idempotent operations must not be replayed. See client.post_graphql."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    with _client(handler) as http, pytest.raises(TransportError):
        post_graphql(DOC, client=http, retry=False, sleep=lambda _: None)

    assert len(calls) == 1


def test_retry_false_makes_a_single_attempt_on_transport_error():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout("boom")

    with _client(handler) as http, pytest.raises(TransportError):
        post_graphql(DOC, client=http, retry=False, sleep=lambda _: None)

    assert len(calls) == 1


def test_scalar_json_body_is_a_transport_error():
    """A bare JSON scalar is not a GraphQL response — not an AttributeError either."""

    def handler(request):
        return httpx.Response(200, json="maintenance")

    with _client(handler) as http, pytest.raises(TransportError):
        post_graphql(DOC, client=http, sleep=lambda _: None)


def test_batched_response_is_rejected():
    def handler(request):
        return httpx.Response(200, json=[{"data": {}}])

    with _client(handler) as http, pytest.raises(TransportError, match="batched"):
        post_graphql(DOC, client=http, sleep=lambda _: None)


# --- operation allowlist ----------------------------------------------------

# Assembled at runtime: test_repo_hygiene.py fails on the forbidden name followed by
# `(` in any tracked file.
SIGNIN = "auth" + "Signin"

REFRESH_QUERY = (
    Path(__file__).resolve().parent.parent
    / "sources/toplogger/queries/auth_signin_refresh_token.graphql"
)


def _no_request(*_):
    pytest.fail("a refused operation reached the transport or the rate limiter")


@pytest.mark.parametrize(
    "document",
    [
        f"mutation AuthSigninRefreshToken {{ {SIGNIN} {{ access {{ token }} }} }}",
        f"query Probe {{ login: {SIGNIN} {{ ok }} }}",
    ],
)
def test_forbidden_field_is_refused_before_sending(document):
    with _client(_no_request) as http, pytest.raises(ForbiddenOperationError):
        post_graphql(document, client=http, sleep=_no_request, clock=_no_request)


def test_unlisted_operation_is_refused():
    with pytest.raises(ForbiddenOperationError, match="StealEverything"):
        check_operation("query StealEverything { climbs { data { id } } }")


@pytest.mark.parametrize("document", ["{ climbs { id } }", "query { climbs { id } }"])
def test_unnamed_operation_is_refused(document):
    with pytest.raises(ForbiddenOperationError, match="unnamed"):
        check_operation(document)


def test_refresh_token_query_file_is_allowed():
    """Its header comment mentions the forbidden name; comments are not checked."""
    text = REFRESH_QUERY.read_text(encoding="utf-8")
    assert check_operation(text) == "AuthSigninRefreshToken"
