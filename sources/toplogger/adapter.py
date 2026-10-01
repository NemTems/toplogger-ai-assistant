"""TopLogger's implementation of the :class:`~sources.base.Source` protocol.

This is the only module in the project that knows TopLogger's field names. Above it
sits `ingest/`, which speaks the protocol; below it sits `client.py`, which owns the
transport, the 1 rps throttle and retries. This layer owns three things client.py
cannot: which fields to ask for, when a token is needed, and — for toppers — the
aggregation that hard rule 4 requires.

Two decisions worth stating up front, because they look odd otherwise:

*No GraphQL variables.* Every argument is inlined as a validated literal instead.
Introspection is disabled (docs/PROJECT_CONTEXT.md §1), so we cannot know whether
`gymId` is declared `ID!` or `String!`, and a variable declared with the wrong type
fails validation before the server ever looks at the value. A literal has no
declared type to get wrong. This is the same reasoning `client.build_aliased_query`
applies to ids, and it is why `_render` validates every value it inlines.

*Auth is discovered, not assumed.* Open question §6.1 asks which of the public-looking
endpoints actually need a token. Rather than guess, each operation is tried once
without one; an `UNAUTHENTICATED`-class error promotes it to authenticated for the
rest of this instance's life. The answers accumulate in :attr:`auth_findings`, which
is the evidence §6.1 is waiting for.

The access token arrives as an injected `token_supplier`, never by calling
`auth.get_access_token()` from here: `sources/` must not import `ingest/`, and token
caching and refresh-token rotation belong to the caller that owns the sync lock.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from config import get_settings
from sources.toplogger.client import (
    AUTH_ERROR_CODES,
    GraphQLError,
    TopLoggerError,
    TransportError,
    post_aliased,
    post_graphql,
)

__all__ = [
    "DEFAULT_SESSION_CLIMB_LIMIT",
    "MAX_TOPPER_ROWS",
    "AuthenticationRequired",
    "TopLoggerSource",
    "aggregate_toppers",
]

SOURCE_NAME = "toplogger"

_QUERY_DIR = Path(__file__).parent / "queries"

# §6.2, answered live 2026-09-23: `climbUserDays(limit:)` is NOT capped at 10. The
# web app's 10 is a UI choice. Measured on a real account, the same window returned
# 297 ticks at limit 10 and 391 at limit 100, with a busiest day of 69 — so 10 was
# silently dropping about a quarter of the history. 100 clears the observed maximum
# with room to spare; raise it if a day ever comes back with exactly this many.
DEFAULT_SESSION_CLIMB_LIMIT = 100

# Ceiling on how many topper rows we will walk for a single climb. A page cap has to
# exist — a server that never returns a short page would otherwise loop forever — and
# expressing it in rows rather than pages keeps it meaningful when `page_size` changes.
MAX_TOPPER_ROWS = 5000

# docs/PROJECT_CONTEXT.md §3. `0` is still open (§6.5: "presumably tried-not-sent"),
# so it gets its own counter rather than being folded into either real outcome.
TICK_TYPE_FLASH = 2
TICK_TYPE_REDPOINT = 1
TICK_TYPE_UNCONFIRMED = 0

# The only two keys `aggregate_toppers` will read off a topper row. Everything else
# the server sends is dropped on the floor unread — hard rule 4's second defence,
# and the reason a name or user ID cannot survive aggregation even if the query file
# is edited to ask for one.
_TOPPER_TICK_TYPE_KEY = "tickType"
_TOPPER_DATE_KEY = "tickedFirstAtDate"  # confirmed live 2026-09-23 on type ClimbUser

_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


class AuthenticationRequired(TopLoggerError):
    """An operation needs an access token and this source was built without a supplier."""


# --- literal rendering -----------------------------------------------------
#
# Mirrors `client._check_literal`, which is private to that module. Duplicating one
# regex is the cheaper coupling: the alternative is reaching into client.py's
# internals from here, and neither module should be able to break the other's
# validation by accident.

_SAFE_LITERAL = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Raw values are constants in this module or built from ints we formatted ourselves,
# never caller input — but they are inlined unquoted, so they are still checked.
_SAFE_RAW = re.compile(r'^[A-Za-z0-9_{}:,.\s"-]{1,200}$')


class _Raw(str):
    """A value inlined verbatim rather than quoted — a GraphQL enum or input object.

    Needed because TopLogger is inconsistent about `climbType`: §2 captured
    `climbs(climbType: boulder)` unquoted but `climbUserDays(climbType: "boulder")`
    quoted. Both shapes are reproduced exactly as captured rather than normalised.
    """


def _render(name: str, value: Any) -> str:
    """Render one argument value as a GraphQL literal, refusing anything unsafe."""
    if isinstance(value, _Raw):
        if not _SAFE_RAW.match(value):
            raise ValueError(f"{name} is not a safe raw literal")
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if not _SAFE_LITERAL.match(value):
            raise ValueError(f"{name} is not a bare identifier and cannot be inlined")
        return f'"{value}"'
    raise ValueError(f"{name} has type {type(value).__name__}, which cannot be inlined")


def _render_args(args: Mapping[str, Any]) -> str:
    """Render an argument mapping, dropping any argument whose value is ``None``.

    Omitting is not the same as sending `null`: an absent optional filter means "no
    filter", which is what a caller passing ``None`` wants.
    """
    return ", ".join(f"{k}: {_render(k, v)}" for k, v in args.items() if v is not None)


def _document(operation_name: str, field: str, args: Mapping[str, Any], selection: str) -> str:
    """Build a single-field query document with its arguments inlined."""
    rendered = _render_args(args)
    call = f"{field}({rendered})" if rendered else field
    return f"query {operation_name} {{\n  {call} {{\n{selection}\n  }}\n}}"


def _with_nested_args(selection: str, args: Mapping[str, Any]) -> str:
    """Substitute ``(__ARGS__)`` in a selection set with rendered arguments.

    Some arguments sit on a *nested* field (`climbUserDays`, `gradeDistribution`), so
    they cannot go through :func:`_document`. The placeholder including its parens is
    what gets replaced, so an empty argument set leaves a bare field rather than the
    `field()` that GraphQL rejects.
    """
    rendered = _render_args(args)
    return selection.replace("(__ARGS__)", f"({rendered})" if rendered else "")


@lru_cache
def _selection(name: str) -> str:
    """Load ``queries/<name>.graphql`` as a selection set, stripped of its comments.

    The comments in those files are notes to us — which fields are guesses, which
    must never be added — and there is no reason to send them to TopLogger once per
    alias in a 25-climb batch.
    """
    text = (_QUERY_DIR / f"{name}.graphql").read_text()
    lines = [code for line in text.splitlines() if (code := line.split("#", 1)[0].rstrip()).strip()]
    return "\n".join(lines)


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    """Yield ``items`` in slices of at most ``size``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --- toppers aggregation ---------------------------------------------------


def aggregate_toppers(pages: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold raw ``climbUsers`` pages into per-climb counts, discarding identities.

    Hard rule 4's second defence. The query file already declines to ask for names,
    user IDs or avatars; this function additionally reads only two keys off each row,
    so nothing else can travel any further than this call stack — not to a raw file,
    not to the DB, not to a model.

    Returns:
        ``tick_count`` (every row returned), ``flash_count`` / ``redpoint_count`` /
        ``unconfirmed_count`` (`tickType` 2 / 1 / 0 per §3, with 0 kept separate
        because §6.5 has not settled what it means), and ``ticks_by_date``, a
        ``YYYY-MM-DD -> count`` histogram. A row with an unrecognised `tickType`
        counts toward ``tick_count`` only, so the three never silently absorb it.
    """
    flash = redpoint = unconfirmed = total = 0
    by_date: dict[str, int] = {}

    for page in pages:
        rows = (page or {}).get("data") or []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            total += 1

            tick_type = row.get(_TOPPER_TICK_TYPE_KEY)
            if tick_type == TICK_TYPE_FLASH:
                flash += 1
            elif tick_type == TICK_TYPE_REDPOINT:
                redpoint += 1
            elif tick_type == TICK_TYPE_UNCONFIRMED:
                unconfirmed += 1

            matched = _ISO_DATE.match(str(row.get(_TOPPER_DATE_KEY) or ""))
            if matched:
                day = matched.group(1)
                by_date[day] = by_date.get(day, 0) + 1

    return {
        "tick_count": total,
        "flash_count": flash,
        "redpoint_count": redpoint,
        "unconfirmed_count": unconfirmed,
        "ticks_by_date": dict(sorted(by_date.items())),
    }


# --- the adapter -----------------------------------------------------------


class TopLoggerSource:
    """A :class:`~sources.base.Source` backed by TopLogger's GraphQL endpoint."""

    def __init__(
        self,
        *,
        gym_id: str | None = None,
        token_supplier: Callable[[], SecretStr] | None = None,
        **post_kwargs: Any,
    ) -> None:
        """Build a source.

        Args:
            gym_id: Gym to fetch. Defaults to ``settings.gym_id``.
            token_supplier: Called to get an access token, and only when an operation
                has been *proven* to need one. Owning the token (caching it for its
                10 minutes, rotating the refresh token under the sync lock) is the
                caller's job — see the module docstring. Called afresh for **every**
                authenticated request; see :meth:`_token`.
            **post_kwargs: Forwarded to :func:`client.post_graphql`. Tests inject
                ``client=`` and ``sleep=``; production callers pass nothing.
        """
        self._gym_id = gym_id or get_settings().gym_id
        self._token_supplier = token_supplier
        self._post_kwargs = post_kwargs
        self._needs_auth: dict[str, bool] = {}
        self._request_count = 0

    @property
    def name(self) -> str:
        """Short, stable identifier used in raw-file paths and logs."""
        return SOURCE_NAME

    @property
    def auth_findings(self) -> dict[str, bool]:
        """Operation name -> whether it turned out to need a token.

        Empty until an operation has actually run. This is the evidence for open
        question §6.1 ("do gym metadata, per-climb stats and toppers work without a
        token?"), which is why it is reported rather than kept internal.
        """
        return dict(self._needs_auth)

    @property
    def request_count(self) -> int:
        """GraphQL operations this source has issued, for sync reporting.

        Counts operations, not TCP requests: transport-level retries happen inside
        :func:`client.post_graphql` and are not visible from here.
        """
        return self._request_count

    # --- transport -------------------------------------------------------

    def _token(self, operation_name: str) -> SecretStr:
        """Return an access token, or explain which operation needed one.

        Called once per authenticated request, and the result is deliberately not
        held on to. The supplier checks the token's age on each call and re-acquires
        before the 10-minute access token expires; memoising the ``SecretStr`` here
        would skip that check and quietly break any sync long enough to need it — a
        toppers crawl over a few hundred climbs is paginated at 1 rps, so it can
        easily outlive one token.
        """
        if self._token_supplier is None:
            raise AuthenticationRequired(
                f"{operation_name} requires an access token, but this TopLoggerSource "
                "was constructed without a token_supplier."
            )
        return self._token_supplier()

    def _post(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        bearer: SecretStr | None,
    ) -> dict[str, Any]:
        """Issue one operation, passing the bearer by value so errors cannot echo it."""
        self._request_count += 1
        return post_graphql(
            query,
            variables,
            bearer=bearer,
            secrets=(bearer,) if bearer is not None else (),
            **self._post_kwargs,
        )

    def _dispatch(self, operation_name: str, send: Callable[[SecretStr | None], Any]) -> Any:
        """Run ``send`` unauthenticated first, promoting the operation if that fails.

        The probe costs one extra request per *operation*, once per instance — not per
        request — because the finding is cached in ``_needs_auth``.

        The retry is *not* limited to :data:`AUTH_ERROR_CODES`. Confirmed live on
        2026-09-23: an unauthenticated ``climbUsers`` answers ``BAD_REQUEST`` for a
        real climb id but ``UNAUTHENTICATED`` for a nonexistent one — the server does
        not reliably say "sign in" when signing in is the fix. Since only the first
        attempt at each operation can trigger it, the cost of being wrong is one
        wasted request on a genuinely broken query; the cost of not retrying is a
        whole sync command that never works.

        If the authenticated retry fails too, *its* error is raised, with the
        unauthenticated one chained: having eliminated auth as the variable, the
        second attempt is the one that describes the real fault. Learned the hard
        way — raising the first error hid a ``perPage`` cap behind a stale
        "Please sign in".
        """
        if self._needs_auth.get(operation_name):
            return send(self._token(operation_name))

        try:
            result = send(None)
        except GraphQLError as exc:
            if self._token_supplier is None:
                # Nothing to retry with. If the server did say "sign in", say so in
                # those terms rather than passing its wording up.
                if AUTH_ERROR_CODES.intersection(exc.codes):
                    self._token(operation_name)  # raises AuthenticationRequired
                raise
            try:
                authenticated = send(self._token(operation_name))
            except GraphQLError as retry_exc:
                raise retry_exc from exc
            self._needs_auth[operation_name] = True
            return authenticated

        self._needs_auth[operation_name] = False
        return result

    def _call(
        self,
        operation_name: str,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Post one document through the unauthenticated-probe path."""
        return self._dispatch(
            operation_name, lambda bearer: self._post(query, variables, bearer=bearer)
        )

    def _call_field(
        self,
        operation_name: str,
        field: str,
        args: Mapping[str, Any],
        selection: str,
    ) -> Any:
        """Post a single-field query and return that field's payload."""
        data = self._call(operation_name, _document(operation_name, field, args, selection))
        return data.get(field)

    def _call_aliased(
        self,
        operation_name: str,
        field: str,
        selection: str,
        ids: Sequence[str],
        *,
        id_arg: str = "id",
        extra_args: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Post one aliased batch through the unauthenticated-probe path."""

        def send(bearer: SecretStr | None) -> dict[str, Any]:
            self._request_count += 1
            return post_aliased(
                operation_name,
                field,
                selection,
                ids,
                id_arg=id_arg,
                extra_args=extra_args,
                bearer=bearer,
                secrets=(bearer,) if bearer is not None else (),
                **self._post_kwargs,
            )

        return self._dispatch(operation_name, send)

    # --- public data -----------------------------------------------------

    def fetch_catalog(self) -> dict[str, Any]:
        """Return the climbs currently set on the wall, as TopLogger shapes them."""
        payload = self._call_field(
            "Catalog",
            "climbs",
            {"gymId": self._gym_id, "climbType": _Raw("boulder")},
            _selection("catalog"),
        )
        return payload or {}

    def fetch_gym_metadata(self) -> dict[str, Any]:
        """Return walls, hold colours, setters, climb groups and the tag taxonomy."""
        payload = self._call_field(
            "GymMetadata",
            "gym",
            {"gymId": self._gym_id},
            _selection("gym_metadata"),
        )
        return payload or {}

    def climb_ids(self, catalog: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> list[str]:
        """Pull the climb ids out of a catalog payload this source produced.

        Takes the payload exactly as :meth:`fetch_catalog` returned it. A bare row
        list is accepted too, so a caller reading a stored snapshot back off disk
        does not have to know which of the two got written. Order is preserved and
        duplicates collapse.

        Applies the §3 data-quality rule: a climb with ``grade == 0`` is an ungraded
        placeholder and is filtered out. Since this list is what decides which climbs
        get stats and popularity fetched, dropping them here also spends no requests
        on them. A row with no ``grade`` key at all is kept — absent is not zero.

        A payload missing the keys it should have yields whatever ids are actually
        there rather than raising: it is read off disk, where a truncated or
        half-written snapshot is a real possibility.
        """
        rows = catalog.get("data") if isinstance(catalog, Mapping) else catalog
        ids = (
            str(row["id"])
            for row in (rows or [])
            if isinstance(row, Mapping) and row.get("id") is not None and row.get("grade") != 0
        )
        return list(dict.fromkeys(ids))

    def fetch_climb_stats(self, climb_ids: list[str]) -> dict[str, Any]:
        """Return ``{climb_id: {gradeAdmin, gradeVoteStats, ratingVoteStats}}``.

        Batched with GraphQL aliases at ``settings.batch_size`` climbs per request
        (hard rule 5) — never one request per climb. An id the server answers with
        ``null`` is absent from the result rather than present as ``None``.
        """
        results: dict[str, Any] = {}
        for chunk in _chunks(climb_ids, get_settings().batch_size):
            results.update(
                self._call_aliased(
                    "ClimbStats",
                    "climb",
                    _selection("climb_stats"),
                    chunk,
                    id_arg="id",
                    extra_args={"gymId": self._gym_id},
                )
            )
        return results

    def fetch_climb_popularity(self, climb_ids: list[str]) -> dict[str, Any]:
        """Return ``{climb_id: counts}`` for the given climbs — no identities.

        Each climb's toppers list is paginated through and folded into counts by
        :func:`aggregate_toppers` before this returns. The un-aggregated pages never
        leave this method, so nothing downstream can accidentally persist them
        (hard rule 4).
        """
        return {climb_id: self._fetch_toppers(climb_id) for climb_id in climb_ids}

    def _fetch_toppers(self, climb_id: str) -> dict[str, Any]:
        """Page through one climb's toppers and return the aggregate."""
        per_page = get_settings().page_size
        page_cap = max(1, MAX_TOPPER_ROWS // per_page)
        pages: list[Mapping[str, Any]] = []

        for page in range(1, page_cap + 1):
            payload = self._call_field(
                "ClimbUsers",
                "climbUsers",
                {
                    "gymId": self._gym_id,
                    "climbId": climb_id,
                    "ticked": True,
                    # UNCONFIRMED: §2 names the `pagination` argument but not its input
                    # object's shape. `{page:, perPage:}` is TopLogger's usual pairing;
                    # a wrong guess fails validation loudly rather than paging wrongly.
                    # Shape confirmed live 2026-09-23: this validates, and the
                    # query then fails on auth rather than on the argument.
                    "pagination": _Raw(f"{{page: {page}, perPage: {per_page}}}"),
                },
                _selection("climb_users"),
            )
            if not payload:
                break
            pages.append(payload)
            # UNCONFIRMED: `climbUsers` is assumed to wrap its rows in `data`, as
            # `climbs` does. If it does not, the selection set fails validation.
            # A short page is the end of the list. Trusting the row count rather than
            # a `pagination { total }` field avoids a second unverified guess about
            # the response shape, and it is the condition that actually matters.
            if len(payload.get("data") or []) < per_page:
                break

        return aggregate_toppers(pages)

    # --- authenticated data ----------------------------------------------

    def fetch_user_history(
        self,
        user_id: str,
        *,
        limit: int = DEFAULT_SESSION_CLIMB_LIMIT,
        total_tries_min: int | None = None,
        stats_at_date_min: str | None = None,
        stats_at_date_max: str | None = None,
    ) -> dict[str, Any]:
        """Return the user's own sessions, with the climbs ticked in each.

        Includes climbs since stripped, which is why this is the only record of what
        was on the wall before snapshotting started.

        §2 selects straight on ``climbDays`` with no ``data { ... }`` wrapper, unlike
        ``climbs`` and ``climbUsers``, so this returns a list. It is wrapped as
        ``{"data": [...]}`` for a consistent payload shape; nothing inside a row is
        touched.

        Args:
            user_id: Whose history to fetch. Never anyone but the configured user.
            limit: ``climbUserDays(limit:)`` — climbs per session day. The web app
                sends 10; whether higher values are honoured is open question §6.2,
                so this is a parameter and not a constant. Call twice over the same
                ``stats_at_date_*`` window with different limits to settle it.
            total_tries_min: Optional ``totalTriesMin`` filter.
            stats_at_date_min: Optional ``YYYY-MM-DD`` lower bound.
            stats_at_date_max: Optional ``YYYY-MM-DD`` upper bound.
        """
        selection = _with_nested_args(
            _selection("user_history"),
            # Quoted here, unquoted in the catalog query — see `_Raw`.
            {"climbType": "boulder", "limit": limit},
        )
        payload = self._call_field(
            "UserHistory",
            "climbDays",
            {
                "userId": user_id,
                # `gymId` filters to one gym; `gymIdNotNull` is a *Boolean* meaning
                # "only days that happened at some gym". §2 listed the latter beside
                # `userId` and it reads like an id, but it is not — confirmed live
                # 2026-09-23 by "Boolean cannot represent a non boolean value".
                "gymId": self._gym_id,
                "gymIdNotNull": True,
                "totalTriesMin": total_tries_min,
                "statsAtDateMin": stats_at_date_min,
                "statsAtDateMax": stats_at_date_max,
            },
            selection,
        )
        return payload if isinstance(payload, dict) else {"data": payload or []}

    def fetch_user_stats(
        self,
        user_id: str,
        *,
        climbed_at_min: str | None = None,
        climbed_at_max: str | None = None,
    ) -> dict[str, Any]:
        """Return TopLogger's own grade distribution for the user, split redpoint/flash.

        Kept as a cross-check on anything we derive ourselves from
        :meth:`fetch_user_history`, not as the primary record.
        """
        selection = _with_nested_args(
            _selection("user_stats"),
            {
                # Enum here, string in user_history — confirmed live, not a typo.
                "climbType": _Raw("boulder"),
                "climbedAtMin": climbed_at_min,
                "climbedAtMax": climbed_at_max,
            },
        )
        payload = self._call_field("UserStats", "user", {"id": user_id}, selection)
        return payload or {}

    # --- schema probing ---------------------------------------------------

    def probe_selection(
        self,
        field: str,
        selection: str,
        extra_args: dict[str, str] | None = None,
    ) -> list[str]:
        """Ask the server to name the valid fields on ``field`` by failing validation.

        Introspection is disabled (§1), so a deliberately wrong selection set is the
        only way to learn a type's shape: the validation error reads *Cannot query
        field "x" on type "Y"*, often with suggestions. Validation runs before
        execution, so this costs no token and reads no data — which is why it is sent
        unauthenticated, bypassing the probe-and-promote path in :meth:`_dispatch`.
        This is how the UNCONFIRMED guesses in ``queries/`` get settled.

        Returns:
            The (redacted) GraphQL error messages, or an empty list if the selection
            unexpectedly validated.
        """
        query = _document("Probe", field, dict(extra_args or {}), f"    {selection}")
        self._request_count += 1
        try:
            post_graphql(query, **self._post_kwargs)
        except GraphQLError as exc:
            return list(exc.messages)
        except TransportError as exc:
            # Confirmed live 2026-09-23: TopLogger answers a query that fails
            # validation with HTTP 400, not a 200 carrying an `errors` array, so the
            # reason arrives as a transport error with the body attached.
            return [str(exc)]
        return []
