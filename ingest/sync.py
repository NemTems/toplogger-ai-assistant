"""Sync commands: pull from a :class:`~sources.base.Source` and write raw files.

Source-agnostic by construction. Nothing here knows what GraphQL is, what a gym ID
is, or that TopLogger exists — it calls protocol methods and hands the results to
:mod:`ingest.raw_store`. When a payload has to be read back (``stats`` and
``toppers`` need the climb IDs from the newest catalog snapshot), the source that
produced it is asked to read it, via ``Source.climb_ids``.

Ordering matters: ``stats`` and ``toppers`` deliberately take their climb IDs from
the stored catalog rather than from a fresh fetch. Loaders read raw files only
(AGENTS.md), and pinning all three of a day's snapshots to the same catalog keeps
them describing the same wall even if a climb is stripped mid-sync.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import get_settings
from ingest.raw_store import latest_raw, read_raw, write_raw
from sources.base import Source

__all__ = [
    "SyncError",
    "SyncReport",
    "sync_catalog",
    "sync_me",
    "sync_stats",
    "sync_toppers",
]


class SyncError(Exception):
    """A sync could not run — usually a missing prerequisite, not a failure mid-flight."""


@dataclass(frozen=True)
class SyncReport:
    """What one sync command did, for the CLI to print and tests to assert on."""

    kind: str
    files: tuple[Path, ...] = ()
    requests: int = 0
    notes: dict[str, Any] = field(default_factory=dict)
    planned: tuple[str, ...] = ()
    """Dry-run only: the calls that *would* have been made."""


def _finish(
    kind: str,
    source: Source,
    files: list[Path],
    requests_before: int,
    notes: dict[str, Any] | None = None,
) -> SyncReport:
    """Assemble a report, folding in whatever the source learned along the way."""
    combined: dict[str, Any] = dict(notes or {})
    findings = getattr(source, "auth_findings", None)
    if findings:
        combined["auth_required"] = dict(findings)
    return SyncReport(
        kind=kind,
        files=tuple(files),
        requests=getattr(source, "request_count", requests_before) - requests_before,
        notes=combined,
    )


def _requests_so_far(source: Source) -> int:
    return getattr(source, "request_count", 0)


def sync_catalog(
    source: Source,
    *,
    dry_run: bool = False,
    root: Path | None = None,
) -> SyncReport:
    """Snapshot the current wall: the climbs on it and the gym's vocabulary.

    Two files, not one. The catalog changes daily; walls, hold colours and setters
    barely change at all — but a catalog row is unreadable without them, so a
    snapshot that cannot be interpreted on its own is not a snapshot.
    """
    if dry_run:
        return SyncReport(kind="catalog", planned=("fetch_catalog", "fetch_gym_metadata"))

    before = _requests_so_far(source)
    files = [
        write_raw(
            "catalog",
            source.fetch_catalog(),
            source=source.name,
            operation="fetch_catalog",
            root=root,
        ),
        write_raw(
            "gym",
            source.fetch_gym_metadata(),
            source=source.name,
            operation="fetch_gym_metadata",
            root=root,
        ),
    ]
    return _finish("catalog", source, files, before)


def _climb_ids_from_latest_catalog(
    source: Source,
    root: Path | None,
    max_climbs: int | None,
) -> tuple[list[str], Path]:
    """Read the newest catalog snapshot and ask the source for its climb IDs."""
    path = latest_raw("catalog", source=source.name, root=root)
    if path is None:
        raise SyncError(
            "no catalog snapshot on disk to take climb IDs from — run the `catalog` command first"
        )

    ids = source.climb_ids(read_raw(path)["payload"])
    if not ids:
        raise SyncError(f"the newest catalog snapshot ({path.name}) contains no climbs")
    if max_climbs is not None:
        ids = ids[:max_climbs]
    return ids, path


def sync_stats(
    source: Source,
    *,
    max_climbs: int | None = None,
    dry_run: bool = False,
    root: Path | None = None,
) -> SyncReport:
    """Snapshot per-climb grade and rating vote histograms.

    Public data, batched: the source is handed every climb ID at once and decides
    how to chunk them (hard rule 5 says batch, and it is the source that knows how).
    """
    if dry_run:
        return SyncReport(kind="stats", planned=("read latest catalog", "fetch_climb_stats"))

    ids, catalog_path = _climb_ids_from_latest_catalog(source, root, max_climbs)
    before = _requests_so_far(source)
    path = write_raw(
        "stats",
        source.fetch_climb_stats(ids),
        source=source.name,
        operation="fetch_climb_stats",
        variables={"climb_count": len(ids), "from_catalog": catalog_path.name},
        root=root,
    )
    return _finish("stats", source, [path], before, {"climbs": len(ids)})


def sync_toppers(
    source: Source,
    *,
    max_climbs: int | None = None,
    dry_run: bool = False,
    root: Path | None = None,
) -> SyncReport:
    """Snapshot per-climb flash/redpoint counts.

    Written with ``aggregated=True``: this is the one kind where "raw is immutable,
    saved untouched" does not hold. The upstream response carries other climbers'
    names and user IDs, and hard rule 4 outranks the convention — the source
    aggregates in memory and only counts ever reach this function.
    """
    if dry_run:
        return SyncReport(kind="toppers", planned=("read latest catalog", "fetch_climb_popularity"))

    ids, catalog_path = _climb_ids_from_latest_catalog(source, root, max_climbs)
    before = _requests_so_far(source)
    path = write_raw(
        "toppers",
        source.fetch_climb_popularity(ids),
        source=source.name,
        operation="fetch_climb_popularity",
        variables={"climb_count": len(ids), "from_catalog": catalog_path.name},
        aggregated=True,
        root=root,
    )
    return _finish("toppers", source, [path], before, {"climbs": len(ids)})


def sync_me(
    source: Source,
    *,
    user_id: str | None = None,
    dry_run: bool = False,
    root: Path | None = None,
) -> SyncReport:
    """Snapshot the authenticated user's own history and the provider's stats on it.

    Requires ``TOPLOGGER_USER_ID``. This is the only sync about a person, and it is
    about exactly one person.
    """
    if dry_run:
        return SyncReport(kind="me", planned=("fetch_user_history", "fetch_user_stats"))

    resolved = user_id or get_settings().user_id
    if not resolved:
        raise SyncError(
            "no user ID configured — set TOPLOGGER_USER_ID in .env or pass --user-id. "
            "It is never given a default, since it is personal data."
        )

    before = _requests_so_far(source)
    files = [
        write_raw(
            "history",
            source.fetch_user_history(resolved),
            source=source.name,
            operation="fetch_user_history",
            root=root,
        ),
        write_raw(
            "user_stats",
            source.fetch_user_stats(resolved),
            source=source.name,
            operation="fetch_user_stats",
            root=root,
        ),
    ]
    return _finish("me", source, files, before)
