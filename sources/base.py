"""The `Source` protocol: the adapter boundary for gym-climbing data providers.

Nothing downstream of `sources/` may know which provider supplied the data —
TopLogger is one implementation; a MoonBoard corpus or a TopLogger CSV export
would be others. This protocol therefore speaks only in climbing-domain terms
(catalog, climb stats, popularity, user history), never in a specific
provider's field names (no `gymId`, `climbUserDays`, `tickType`, etc.).

This is deliberately minimal for Phase 0 — a shape to implement against, not a
finished interface. It will grow in Phase 1/2 when `sources/toplogger/adapter.py`
implements it for real (auth, pagination, rate limiting live there, not here).
"""

from typing import Any, Protocol, runtime_checkable

# A raw, source-shaped payload: whatever JSON-like structure the provider's API
# returns. `sources/` implementations do not normalise this — that happens later,
# in `load/`, against the canonical schema.
RawPayload = dict[str, Any] | list[dict[str, Any]]


@runtime_checkable
class Source(Protocol):
    """A backend that can supply gym-climbing data.

    Each method returns a raw, provider-shaped payload for a single domain-level
    concept. Implementations are responsible for their own transport, auth,
    pagination, rate limiting and retries — callers only see these operations.
    """

    @property
    def name(self) -> str:
        """Short, stable identifier for this source (e.g. "toplogger").

        Used in raw-file paths (`data/raw/<name>/...`) and logs — never in
        anything a model sees as domain data.
        """
        ...

    def fetch_catalog(self) -> RawPayload:
        """Return the climbs currently set on the wall, plus gym metadata needed
        to make sense of them (walls, hold colours, setters, climb groups, tags).

        Public data. Reflects the *current* state of the wall only; a history of
        what was on the wall on past dates comes from diffing repeated snapshots,
        not from this call.
        """
        ...

    def fetch_climb_stats(self, climb_ids: list[str]) -> RawPayload:
        """Return per-climb difficulty and quality signals for the given climbs:
        grade vote histograms and star-rating vote histograms.

        Public data.
        """
        ...

    def fetch_climb_popularity(self, climb_ids: list[str]) -> RawPayload:
        """Return per-climb popularity counts for the given climbs — e.g. how many
        climbers have flashed or sent each one.

        Implementations MUST aggregate across climbers before returning: no
        individual climber's name, user ID or per-person tick record may appear
        in this payload. Only per-climb counts.
        """
        ...

    def fetch_user_history(self, user_id: str) -> RawPayload:
        """Return the authenticated user's own climbing history: sessions and
        per-climb attempts/outcomes over time, for the given user.

        Requires authentication. Returns data about `user_id` only — never about
        other climbers.
        """
        ...
