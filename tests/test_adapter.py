"""Tests for the TopLogger source adapter.

Every request is served by ``httpx.MockTransport`` and every fixture is hand-written;
nothing here touches the network (hard rule 6).

The test that matters most is ``test_aggregate_toppers_discards_identities``: it feeds
the aggregator a page full of invented names and user IDs and asserts none of them
survive into the result. Hard rule 4 has no second chance — once an identity reaches a
raw file it has been persisted.
"""

import json
import re
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from config import get_settings
from sources.base import Source
from sources.toplogger import adapter
from sources.toplogger.adapter import (
    AuthenticationRequired,
    TopLoggerSource,
    aggregate_toppers,
)
from sources.toplogger.client import GraphQLError, reset_rate_limiter

FIXTURES = Path(__file__).parent / "fixtures" / "toplogger"

FAKE_TOKEN = "fake-access-token-aaa"  # noqa: S105 - obviously fake, not a credential


@pytest.fixture(autouse=True)
def _clean_rate_limiter():
    reset_rate_limiter()
    yield
    reset_rate_limiter()


@contextmanager
def source(handler, **kwargs):
    """A ``TopLoggerSource`` whose transport is ``handler`` and whose clock is fake."""
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        yield TopLoggerSource(client=http, sleep=lambda _: None, **kwargs)


def token_supplier():
    return SecretStr(FAKE_TOKEN)


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def query_of(request):
    return json.loads(request.content)["query"]


def gql_error(code="UNAUTHENTICATED", message="not authenticated"):
    return httpx.Response(
        200, json={"errors": [{"message": message, "extensions": {"code": code}}]}
    )


def set_setting(monkeypatch, name, value):
    """Override one ``Settings`` field for this test and drop the cached instance."""
    monkeypatch.setenv(f"TOPLOGGER_{name.upper()}", str(value))
    get_settings.cache_clear()


# --- the protocol ----------------------------------------------------------


def test_implements_the_source_protocol():
    assert isinstance(TopLoggerSource(), Source)


def test_name_is_stable():
    assert TopLoggerSource().name == "toplogger"


# --- the unauthenticated probe --------------------------------------------


def test_probe_costs_one_extra_request_once_per_operation():
    """Open question §6.1 is answered by trying, and the answer is cached per instance."""
    seen = []

    def handler(request):
        seen.append(request.headers.get("Authorization"))
        if request.headers.get("Authorization") is None:
            return gql_error()
        return httpx.Response(200, json={"data": {"climbs": {"data": []}}})

    with source(handler, token_supplier=token_supplier) as src:
        src.fetch_catalog()
        assert src.request_count == 2
        assert src.auth_findings == {"Catalog": True}

        src.fetch_catalog()
        assert src.request_count == 3

    assert seen == [None, f"Bearer {FAKE_TOKEN}", f"Bearer {FAKE_TOKEN}"]


def test_public_operation_never_sends_a_token():
    seen = []

    def handler(request):
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"data": {"climbs": {"data": []}}})

    def explode():
        raise AssertionError("a public operation must not ask for a token")

    with source(handler, token_supplier=explode) as src:
        src.fetch_catalog()
        src.fetch_catalog()

    assert seen == [None, None]


def test_public_operation_is_recorded_as_not_needing_auth():
    def handler(request):
        return httpx.Response(200, json={"data": {"gym": {"walls": []}}})

    with source(handler) as src:
        src.fetch_gym_metadata()

    assert src.auth_findings == {"GymMetadata": False}


def test_any_first_failure_is_retried_once_with_a_token():
    """The server does not reliably say "sign in" when signing in is the fix.

    Confirmed live 2026-09-23: an unauthenticated `climbUsers` answers BAD_REQUEST
    for a real climb id and UNAUTHENTICATED for a nonexistent one. So the first
    attempt at an operation is retried with a token whatever the code says.
    """
    seen = []

    def handler(request):
        seen.append(request.headers.get("Authorization"))
        if request.headers.get("Authorization") is None:
            return gql_error(code="BAD_REQUEST", message="Bad Request Exception")
        return httpx.Response(200, json={"data": {"climbs": {"data": []}}})

    with source(handler, token_supplier=token_supplier) as src:
        src.fetch_catalog()

    assert seen == [None, f"Bearer {FAKE_TOKEN}"]
    assert src.auth_findings == {"Catalog": True}


def test_a_query_broken_both_ways_raises_the_authenticated_error():
    """With auth eliminated as the variable, the second attempt names the real fault.

    Raising the *first* error instead once hid a `perPage` cap behind a stale
    "Please sign in" — the unauthenticated attempt's complaint was the wrong one.
    """
    calls = []

    def handler(request):
        calls.append(1)
        return gql_error(code="GRAPHQL_VALIDATION_FAILED", message="Cannot query field zzz")

    with source(handler, token_supplier=token_supplier) as src, pytest.raises(GraphQLError) as exc:
        src.fetch_catalog()

    assert len(calls) == 2, "retried exactly once, not more"
    assert "Cannot query field zzz" in str(exc.value)
    assert isinstance(exc.value.__cause__, GraphQLError), "the first error stays attached"
    assert src.auth_findings == {}, "a broken query must not be recorded as needing auth"


def test_a_failure_with_no_token_supplier_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return gql_error(code="GRAPHQL_VALIDATION_FAILED", message="Cannot query field")

    with source(handler) as src, pytest.raises(GraphQLError):
        src.fetch_catalog()

    assert len(calls) == 1


def test_missing_token_supplier_names_the_operation():
    def handler(request):
        return gql_error()

    with source(handler) as src, pytest.raises(AuthenticationRequired, match="Catalog"):
        src.fetch_catalog()


def test_token_supplier_is_called_for_every_authenticated_request():
    """The supplier re-checks the token's age on each call, so it must not be memoised.

    A toppers crawl is paginated at 1 rps and can outlive the 10-minute access token;
    a `SecretStr` captured once would make it die partway through.
    """
    supplied = []
    seen = []

    def counting_supplier():
        supplied.append(len(supplied))
        return SecretStr(f"fake-access-token-{len(supplied)}")

    def handler(request):
        header = request.headers.get("Authorization")
        seen.append(header)
        if header is None:
            return gql_error()
        return httpx.Response(200, json={"data": {"climbs": {"data": []}}})

    with source(handler, token_supplier=counting_supplier) as src:
        src.fetch_catalog()  # probes unauthenticated, then retries with token 1
        src.fetch_catalog()  # already known to need auth: token 2

    assert len(supplied) == 2
    assert seen == [None, "Bearer fake-access-token-1", "Bearer fake-access-token-2"]


def test_bearer_token_never_reaches_a_raised_exception():
    """Hard rule 1: an API error that echoes the token back must not carry it out."""

    def handler(request):
        if request.headers.get("Authorization") is None:
            return gql_error()
        return gql_error(code="BAD_USER_INPUT", message=f"bad token {FAKE_TOKEN}")

    with source(handler, token_supplier=token_supplier) as src, pytest.raises(GraphQLError) as exc:
        src.fetch_catalog()

    assert FAKE_TOKEN not in str(exc.value)
    assert FAKE_TOKEN not in repr(exc.value)


# --- catalog ---------------------------------------------------------------


def test_fetch_catalog_returns_the_payload_and_reads_its_ids():
    catalog = load_fixture("catalog.json")

    def handler(request):
        assert "climbType: boulder" in query_of(request)
        return httpx.Response(200, json={"data": {"climbs": catalog}})

    with source(handler) as src:
        payload = src.fetch_catalog()

    # clmb-ccc is the fixture's grade == 0 row, filtered per §3.
    assert src.climb_ids(payload) == ["clmb-aaa", "clmb-bbb"]


def test_climb_ids_accepts_a_bare_row_list_and_dedupes():
    rows = [{"id": "a"}, {"id": "b"}, {"id": "a"}, {"grade": 600}]
    assert TopLoggerSource().climb_ids(rows) == ["a", "b"]


def test_climb_ids_filters_ungraded_placeholders():
    """§3: `grade == 0` is a placeholder, not an easy climb. Absent is not zero."""
    rows = [
        {"id": "graded", "grade": 633},
        {"id": "placeholder", "grade": 0},
        {"id": "no-grade-key"},
        {"id": "null-grade", "grade": None},
    ]

    assert TopLoggerSource().climb_ids(rows) == ["graded", "no-grade-key", "null-grade"]


@pytest.mark.parametrize(
    "catalog",
    [
        {},
        {"data": None},
        {"data": []},
        [],
        {"data": [{"grade": 633}, None, "not-a-row", {"id": None}]},
    ],
)
def test_climb_ids_tolerates_a_malformed_payload(catalog):
    """It reads a snapshot off disk, where a truncated write is a real possibility."""
    assert TopLoggerSource().climb_ids(catalog) == []


# --- batching --------------------------------------------------------------

_ALIAS = re.compile(r'c(\d+): climb\(gymId: "[^"]*", id: "([^"]+)"\)')


def _alias_handler(requests):
    """Answer an aliased `climb` batch, echoing back which id produced each alias."""

    def handler(request):
        query = query_of(request)
        requests.append(query)
        return httpx.Response(
            200,
            json={
                "data": {
                    f"c{index}": {"gradeAdmin": 633, "echoedId": climb_id}
                    for index, climb_id in _ALIAS.findall(query)
                }
            },
        )

    return handler


def test_climb_stats_sends_one_request_for_a_single_batch():
    requests = []
    ids = ["clmb-aaa", "clmb-bbb", "clmb-ccc"]

    with source(_alias_handler(requests)) as src:
        stats = src.fetch_climb_stats(ids)

    assert len(requests) == 1
    assert src.request_count == 1
    assert {cid: stats[cid]["echoedId"] for cid in ids} == {cid: cid for cid in ids}


def test_climb_stats_chunks_above_the_batch_size(monkeypatch):
    set_setting(monkeypatch, "batch_size", 2)
    requests = []
    ids = [f"clmb-{n}" for n in range(5)]

    with source(_alias_handler(requests)) as src:
        stats = src.fetch_climb_stats(ids)

    assert len(requests) == 3
    assert [len(_ALIAS.findall(q)) for q in requests] == [2, 2, 1]
    assert {cid: stats[cid]["echoedId"] for cid in ids} == {cid: cid for cid in ids}


def test_climb_stats_of_nothing_issues_no_request():
    def handler(request):
        raise AssertionError("no climbs means no request")

    with source(handler) as src:
        assert src.fetch_climb_stats([]) == {}
    assert src.request_count == 0


# --- toppers ---------------------------------------------------------------

_PAGE = re.compile(r"page: (\d+)")


def _topper_row(tick_type=2, date="2026-09-01"):
    return {"tickType": tick_type, "tickedFirstAtDate": date}


def test_toppers_pagination_stops_on_a_short_page(monkeypatch):
    set_setting(monkeypatch, "page_size", 2)
    pages_served = []

    def handler(request):
        page = int(_PAGE.search(query_of(request)).group(1))
        pages_served.append(page)
        rows = [_topper_row(2), _topper_row(1)] if page == 1 else [_topper_row(0)]
        return httpx.Response(200, json={"data": {"climbUsers": {"data": rows}}})

    with source(handler) as src:
        result = src.fetch_climb_popularity(["clmb-aaa"])

    assert pages_served == [1, 2]
    assert result["clmb-aaa"]["tick_count"] == 3


def test_toppers_respects_the_page_cap(monkeypatch):
    """A server that never returns a short page must not be able to loop us forever."""
    set_setting(monkeypatch, "page_size", 2)
    monkeypatch.setattr(adapter, "MAX_TOPPER_ROWS", 4)
    pages_served = []

    def handler(request):
        pages_served.append(int(_PAGE.search(query_of(request)).group(1)))
        return httpx.Response(
            200,
            json={"data": {"climbUsers": {"data": [_topper_row(), _topper_row()]}}},
        )

    with source(handler) as src:
        result = src.fetch_climb_popularity(["clmb-aaa"])

    assert pages_served == [1, 2]
    assert result["clmb-aaa"]["tick_count"] == 4


def test_toppers_query_asks_for_no_identifying_field():
    """Hard rule 4, first defence: the selection set itself must stay identity-free."""
    fields = {
        line.strip().rstrip("{").strip() for line in adapter._selection("climb_users").splitlines()
    }
    assert fields.isdisjoint({"id", "uid", "user", "userId", "name", "fullName", "avatar"})


# --- aggregation -----------------------------------------------------------


def test_aggregate_toppers_discards_identities():
    """The regression test for hard rule 4. Counts right, identities gone."""
    fixture = load_fixture("climb_users_pages.json")

    result = aggregate_toppers(fixture["pages"])
    rendered = json.dumps(result)

    for identity in fixture["_identity_strings_that_must_not_survive"]:
        assert identity not in rendered

    assert result == {
        "tick_count": 5,
        "flash_count": 2,
        "redpoint_count": 2,
        "unconfirmed_count": 1,
        "ticks_by_date": {"2026-09-01": 2, "2026-09-02": 2, "2026-09-03": 1},
    }


def test_aggregate_toppers_decodes_tick_types():
    """§3: 2 is a flash, 1 a redpoint/top, 0 unconfirmed and counted on its own."""
    pages = [{"data": [_topper_row(2), _topper_row(2), _topper_row(1), _topper_row(0)]}]

    result = aggregate_toppers(pages)

    assert (result["flash_count"], result["redpoint_count"], result["unconfirmed_count"]) == (
        2,
        1,
        1,
    )
    assert result["tick_count"] == 4


def test_aggregate_toppers_buckets_by_calendar_day():
    pages = [
        {"data": [_topper_row(2, "2026-09-01"), _topper_row(1, "2026-09-01T23:59:00Z")]},
        {"data": [_topper_row(1, "2026-09-02"), _topper_row(2, None), _topper_row(2, "")]},
    ]

    assert aggregate_toppers(pages)["ticks_by_date"] == {"2026-09-01": 2, "2026-09-02": 1}


def test_aggregate_toppers_does_not_absorb_an_unknown_tick_type():
    """An unrecognised code counts as a tick but is not guessed into a real outcome."""
    result = aggregate_toppers([{"data": [_topper_row(2), _topper_row(9)]}])

    assert result["tick_count"] == 2
    assert result["flash_count"] + result["redpoint_count"] + result["unconfirmed_count"] == 1


def test_aggregate_toppers_of_nothing_is_all_zeros():
    assert aggregate_toppers([]) == {
        "tick_count": 0,
        "flash_count": 0,
        "redpoint_count": 0,
        "unconfirmed_count": 0,
        "ticks_by_date": {},
    }


# --- authenticated operations ---------------------------------------------


def test_user_history_limit_is_a_parameter_not_an_assumption():
    """§6.2: the same window at two limits, so the cap can be re-measured, not assumed.

    Answered live 2026-09-23 — `limit:` is honoured well above the web app's 10 — so
    the default is now 100. The parameter stays because the answer could change.
    """
    queries = []

    def handler(request):
        queries.append(query_of(request))
        return httpx.Response(200, json={"data": {"climbDays": []}})

    with source(handler) as src:
        src.fetch_user_history("usr-me", stats_at_date_min="2026-09-01")
        src.fetch_user_history("usr-me", stats_at_date_min="2026-09-01", limit=10)

    assert 'climbUserDays(climbType: "boulder", limit: 100)' in queries[0]
    assert 'climbUserDays(climbType: "boulder", limit: 10)' in queries[1]
    assert queries[0].replace("limit: 100", "limit: 10") == queries[1]


def test_user_history_omits_absent_filters():
    """An unset optional filter is left out entirely, not sent as null."""
    queries = []

    def handler(request):
        queries.append(query_of(request))
        return httpx.Response(200, json={"data": {"climbDays": []}})

    with source(handler) as src:
        src.fetch_user_history("usr-me")

    assert "statsAtDateMin" not in queries[0]
    assert "totalTriesMin" not in queries[0]


def test_user_history_wraps_a_bare_list():
    def handler(request):
        return httpx.Response(200, json={"data": {"climbDays": [{"statsAtDate": "2026-09-01"}]}})

    with source(handler) as src:
        assert src.fetch_user_history("usr-me") == {"data": [{"statsAtDate": "2026-09-01"}]}


def test_user_stats_sends_the_user_id_and_window():
    queries = []

    def handler(request):
        queries.append(query_of(request))
        return httpx.Response(200, json={"data": {"user": {"gradeDistribution": []}}})

    with source(handler) as src:
        src.fetch_user_stats("usr-me", climbed_at_min="2026-01-01")

    assert 'user(id: "usr-me")' in queries[0]
    assert 'climbedAtMin: "2026-01-01"' in queries[0]


def test_an_id_that_is_not_a_bare_identifier_is_refused_before_any_request():
    """Ids are inlined as literals, so validating them is not optional."""

    def handler(request):
        raise AssertionError("a bad id must never reach the network")

    with source(handler) as src, pytest.raises(ValueError, match="bare identifier"):
        src.fetch_user_stats('me") { secret } x(id: "')


# --- schema probing --------------------------------------------------------


def test_probe_selection_returns_the_validation_messages():
    """Introspection is off (§1), so a wrong selection is how we learn a type's shape."""

    def handler(request):
        assert request.headers.get("Authorization") is None
        return gql_error(
            code="GRAPHQL_VALIDATION_FAILED",
            message='Cannot query field "nope" on type "ClimbUser". Did you mean "tickType"?',
        )

    with source(handler) as src:
        messages = src.probe_selection("climbUsers", "nope", {"gymId": "gym-1"})

    assert messages == ['Cannot query field "nope" on type "ClimbUser". Did you mean "tickType"?']
    assert src.request_count == 1


def test_probe_selection_is_empty_when_the_selection_validated():
    def handler(request):
        return httpx.Response(200, json={"data": {"climbUsers": {}}})

    with source(handler) as src:
        assert src.probe_selection("climbUsers", "tickType") == []
