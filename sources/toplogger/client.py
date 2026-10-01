"""GraphQL transport for TopLogger: rate limiting, retries, alias batching and error
mapping.

Nothing in this module may emit a token. Request headers and response bodies both
carry credentials, so error types here carry status codes and GraphQL error codes
only, and every string that escapes is passed through :func:`redact`.

Nothing in this module may send an operation it was not told about, either. Every
document is checked against :data:`ALLOWED_OPERATIONS` before the throttle or the
socket is touched, so `authSignin` can never be sent.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from pydantic import SecretStr

from config import get_settings

__all__ = [
    "ALLOWED_OPERATIONS",
    "AUTH_ERROR_CODES",
    "FORBIDDEN_FIELDS",
    "ForbiddenOperationError",
    "GraphQLError",
    "TopLoggerError",
    "TransportError",
    "build_aliased_query",
    "check_operation",
    "looks_like_jwt",
    "post_aliased",
    "post_graphql",
    "redact",
]

# GraphQL error codes that mean "you are not allowed to do this without a token".
# Shared so the adapter's unauthenticated probe and auth.py's dead-token check agree
# on what an auth failure looks like.
AUTH_ERROR_CODES = frozenset({"UNAUTHENTICATED", "UNAUTHORIZED", "FORBIDDEN"})

# A JWT is the only credential shape precise enough to assert on: `eyJ` is a
# base64url-encoded `{"` and essentially never occurs by accident.
_JWT_SHAPED = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){0,2}")

# For redaction we also swallow any long opaque run that *could* be a token, so this
# over-matches. Too broad to assert on — see :func:`looks_like_jwt`.
_TOKEN_SHAPED = re.compile(f"{_JWT_SHAPED.pattern}|[A-Za-z0-9_-]{{40,}}")


def looks_like_jwt(text: str) -> bool:
    """True if ``text`` contains something JWT-shaped.

    The precise half of :func:`redact`'s pattern, for callers that *refuse* on a
    match, e.g. before writing a raw data file. A long opaque value such as a
    ``picPath`` does not match.
    """
    return _JWT_SHAPED.search(text) is not None


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


class ForbiddenOperationError(TopLoggerError):
    """A document failed the operation allowlist and was never sent.

    Raised before throttling, so a refused document costs no request and leaves the
    rate limiter's clock alone. The message names the operation at most — never the
    document body or its variables, either of which can carry a token.
    """


# --- operation allowlist ---------------------------------------------------
#
# Auth only ever goes through `authSigninRefreshToken`, never `authSignin` (which
# needs a reCAPTCHA and so is manual-only). Every request passes through
# `post_graphql`, which refuses any document not declared here.
#
# Pairs, not bare names, so an allowlisted name cannot change kind: `Catalog` is a
# query and may only ever be sent as one. The single mutation is the refresh.
# Adding a line here adds a TopLogger operation.
ALLOWED_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("mutation", "AuthSigninRefreshToken"),  # auth._refresh
        ("query", "Catalog"),  # TopLoggerSource.fetch_catalog
        ("query", "GymMetadata"),  # TopLoggerSource.fetch_gym_metadata
        ("query", "ClimbStats"),  # TopLoggerSource.fetch_climb_stats, aliased
        ("query", "ClimbUsers"),  # TopLoggerSource._fetch_toppers
        ("query", "UserHistory"),  # TopLoggerSource.fetch_user_history
        ("query", "UserStats"),  # TopLoggerSource.fetch_user_stats
        # TopLoggerSource.probe_selection: a selection that fails validation, whose
        # error names a type's fields. Its selection is caller-supplied, so the
        # field check below applies regardless of the name.
        ("query", "Probe"),
    }
)

# Names refused anywhere in a document, whatever the operation is called. Checked
# independently of the allowlist, which alone would pass
# `query Probe { authSignin { ... } }`.
FORBIDDEN_FIELDS: frozenset[str] = frozenset({"authSignin"})

# A minimal GraphQL lexer: just enough to find the operation head and every name
# token with comments and string literals out of the way. Comments matter because
# queries/auth_signin_refresh_token.graphql is sent verbatim and its comment says
# "never paired with `authSignin`"; strings matter because a `{` inside one must not
# move the brace depth. Anything this cannot tokenise is refused, not guessed at.
_GRAPHQL_TOKEN = re.compile(
    r"""
    (?P<ignored>[\s,\ufeff]+|\#[^\n\r]*)
    | (?P<string>\"\"\"(?:\\\"\"\"|[^"]|"(?!""))*\"\"\"|"(?:\\.|[^"\\\n\r])*")
    | (?P<name>[_A-Za-z][_0-9A-Za-z]*)
    | (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<punct>\.\.\.|[!$&():=@\[\]{|}])
    """,
    re.VERBOSE,
)

_OPERATION_TYPES = frozenset({"query", "mutation", "subscription"})
_CLOSERS = {"{": "}", "(": ")", "[": "]"}


def _tokenize(document: str) -> list[tuple[str, str]]:
    """Return the ``(kind, text)`` name and punctuator tokens of ``document``.

    Comments, whitespace, commas, strings and numbers are dropped: none of them can
    name an operation or select a field.
    """
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(document):
        match = _GRAPHQL_TOKEN.match(document, pos)
        if match is None:
            # The offset, not the text: the text could be anything, a token included.
            raise ForbiddenOperationError(
                f"refusing to send a document that does not tokenise (offset {pos})"
            )
        if match.lastgroup in ("name", "punct"):
            tokens.append((match.lastgroup, match.group()))
        pos = match.end()
    return tokens


def _definition_heads(tokens: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Split a token stream into top-level definitions, keeping only each one's head.

    The head is everything at depth 0 — ``query Name ( @dir {`` — which is all the
    allowlist needs. A definition ends when a ``}`` brings the depth back to 0;
    brackets are tracked together so an object default inside a variable list
    cannot end a definition early. Unbalanced input is refused.
    """
    heads: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] | None = None
    stack: list[str] = []
    for kind, text in tokens:
        if not stack:
            if current is None:
                current = []
                heads.append(current)
            current.append((kind, text))
        if text in _CLOSERS:
            stack.append(_CLOSERS[text])
        elif text in _CLOSERS.values():
            if not stack or stack.pop() != text:
                raise ForbiddenOperationError(
                    "refusing to send a document with unbalanced brackets"
                )
            if not stack and text == "}":
                current = None
    if stack or current is not None:
        raise ForbiddenOperationError("refusing to send an incomplete document")
    return heads


def check_operation(document: str) -> tuple[str, str]:
    """Return ``(operation_type, name)`` if ``document`` may be sent, else raise.

    A document may be sent only if it holds exactly one definition, that definition
    is a *named* operation listed in :data:`ALLOWED_OPERATIONS` with the same type,
    and no name token anywhere in it is in :data:`FORBIDDEN_FIELDS`.

    Anonymous operations, multi-operation documents (the server would pick which
    one runs from ``operationName``, which is never sent) and fragments are refused.

    Raises:
        ForbiddenOperationError: The document fails any of the above. Raised before
            anything is sent; the message never quotes the document body.
    """
    tokens = _tokenize(document)

    # Checked first, regardless of operation name. A whole-token match, so
    # `authSigninRefreshToken` is a different name and passes. It also catches the
    # name used as an alias, argument or enum value.
    forbidden = sorted(FORBIDDEN_FIELDS.intersection(t for k, t in tokens if k == "name"))
    if forbidden:
        raise ForbiddenOperationError(
            f"refusing to send a document that references {', '.join(forbidden)} "
            "(CLAUDE.md hard rule 2: login is manual-only)"
        )

    heads = _definition_heads(tokens)
    if len(heads) != 1:
        raise ForbiddenOperationError(
            f"refusing to send a document with {len(heads)} definitions; exactly one "
            "named operation is allowed"
        )

    head = heads[0]
    op_type = head[0][1]
    if op_type == "{":
        raise ForbiddenOperationError("refusing to send an anonymous query shorthand")
    if op_type not in _OPERATION_TYPES:
        raise ForbiddenOperationError(
            "refusing to send a document whose definition is not an operation"
        )
    if len(head) < 2 or head[1][0] != "name":
        raise ForbiddenOperationError(f"refusing to send an anonymous {op_type}")

    name = head[1][1]
    if (op_type, name) not in ALLOWED_OPERATIONS:
        raise ForbiddenOperationError(
            f"refusing to send {op_type} {redact(name)}: not in ALLOWED_OPERATIONS"
        )
    return op_type, name


# Rate limiting is process-wide: one lock, one clock, so every request in the
# process is serialised and spaced, however many clients there are.
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
        query: The GraphQL document. Must hold exactly one named operation from
            :data:`ALLOWED_OPERATIONS`; see :func:`check_operation`.
        variables: Operation variables, if any.
        bearer: Token for the ``Authorization`` header. Never logged.
        secrets: Credentials this request carries besides ``bearer`` (e.g. a token
            passed as a variable), masked out of any error this raises.
        retry: ``False`` for a non-idempotent operation, where replaying a request
            whose response was lost does real damage — refresh-token rotation
            being the case that matters here. The call then fails after one
            attempt, transient errors included.
        client: An ``httpx.Client`` to use; one is created per call if omitted.
            Tests pass a client built on ``httpx.MockTransport``; nothing else
            should pass this.

    Raises:
        ForbiddenOperationError: ``query`` failed :func:`check_operation`. Nothing
            was sent and the rate limiter was not touched.
        TransportError: HTTP-level failure that survived the configured retries.
        GraphQLError: The response carried a GraphQL ``errors`` array.
    """
    # First, before settings, headers or the throttle: a refused document costs no
    # request and leaves the rate limiter untouched.
    check_operation(query)

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
                # A 4xx is a malformed request or a dead credential; a retry would
                # fail the same way, so it is never retried.
                raise TransportError(
                    f"TopLogger returned {response.status_code}"
                    f"{_body_excerpt(response, all_secrets)}"
                )

            return _parse(response, all_secrets)

        raise TransportError("retry loop exhausted") from last_error
    finally:
        if owns_client:
            http.close()


# Enough of an error body to name the offending field, not enough to paste a page
# of HTML into a log line.
_BODY_EXCERPT_CHARS = 600


def _body_excerpt(response: httpx.Response, secrets: Sequence[SecretStr | str]) -> str:
    """Return a short, redacted slice of an error response body, or ``""``.

    A 4xx from a GraphQL endpoint carries the reason — an unknown field, an argument
    of the wrong type. TopLogger answers a query that fails validation with 400, not
    a 200 carrying ``errors``, so this excerpt is where validation messages appear.

    The body is untrusted and may echo the request, so it is truncated and passed
    through :func:`redact` with the credentials this request carried.
    """
    try:
        text = response.text.strip()
    except (UnicodeDecodeError, ValueError):
        return ""
    if not text:
        return ""
    excerpt = redact(text[:_BODY_EXCERPT_CHARS], *secrets)
    suffix = "…" if len(text) > _BODY_EXCERPT_CHARS else ""
    return f": {' '.join(excerpt.split())}{suffix}"


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
            # Only single operations are sent, so a JSON-array response is unexpected.
            raise TransportError("unexpected batched response for a single operation")
        # A bare JSON scalar is not a GraphQL response.
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


# --- alias batching --------------------------------------------------------
#
# TopLogger's endpoint accepts GraphQL aliases, so one document can ask for many
# climbs at once: `c0: climb(id: "a") {...} c1: ...`. That is a single operation,
# so the response is never the JSON array `_parse` rejects.
#
# IDs and arguments are inlined as literals, so each must pass `_SAFE_LITERAL`
# first.

_SAFE_LITERAL = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_ALIAS_PREFIX = "c"


def _check_literal(name: str, value: str) -> str:
    """Return ``value`` if it is safe to inline into a GraphQL document."""
    if not isinstance(value, str) or not _SAFE_LITERAL.match(value):
        raise ValueError(f"{name} is not a bare identifier and cannot be inlined into a query")
    return value


def build_aliased_query(
    operation_name: str,
    field: str,
    selection: str,
    ids: Sequence[str],
    *,
    id_arg: str = "id",
    extra_args: Mapping[str, str] | None = None,
) -> str:
    """Build one document asking for ``field`` once per id, under ``c0..cN`` aliases.

    Args:
        operation_name: Name for the operation, for server-side logs.
        field: The field to alias, e.g. ``"climb"``.
        selection: The selection set body, without the outer braces.
        ids: The ids to fetch. Order fixes the alias numbering.
        id_arg: Argument name carrying the id.
        extra_args: Arguments repeated on every alias, e.g. ``{"gymId": ...}``.

    Raises:
        ValueError: ``ids`` is empty, or any id/argument is not a bare identifier.
    """
    if not ids:
        raise ValueError("cannot build an aliased query with no ids")

    args = {k: _check_literal(k, v) for k, v in (extra_args or {}).items()}
    fixed = "".join(f'{k}: "{v}", ' for k, v in args.items())

    lines = [
        f'  {_ALIAS_PREFIX}{i}: {field}({fixed}{id_arg}: "{_check_literal("id", cid)}") {{'
        f"\n{selection}\n  }}"
        for i, cid in enumerate(ids)
    ]
    return f"query {operation_name} {{\n" + "\n".join(lines) + "\n}"


def post_aliased(
    operation_name: str,
    field: str,
    selection: str,
    ids: Sequence[str],
    *,
    id_arg: str = "id",
    extra_args: Mapping[str, str] | None = None,
    **post_kwargs: Any,
) -> dict[str, Any]:
    """POST one aliased batch and return ``{id: payload}``.

    Aliases are mapped back to the ids that produced them, so callers never see the
    ``c0``/``c1`` naming. An id the server answered with ``null`` is omitted.
    """
    query = build_aliased_query(
        operation_name, field, selection, ids, id_arg=id_arg, extra_args=extra_args
    )
    data = post_graphql(query, **post_kwargs)
    return {
        cid: data[alias]
        for i, cid in enumerate(ids)
        if (alias := f"{_ALIAS_PREFIX}{i}") in data and data[alias] is not None
    }
