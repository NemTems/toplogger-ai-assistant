"""``python -m ingest`` — the sync commands.

This module is the composition root: it is where the concrete adapter is chosen.
:mod:`ingest.sync` below it speaks only the ``Source`` protocol and would not notice
a second adapter appearing.

Two other modules here do reach into ``sources.toplogger``, for helpers rather than
for field names: :mod:`ingest.raw_store` borrows the credential-shape check, and
:mod:`ingest.tokens` wraps the refresh-token flow. Both would want to move — the
check to a shared module, the token cache into the adapter — the day a second source
exists. Neither blocks that day arriving.

Every command holds the sync lock for its whole run (hard rule 3) and acquires at
most one access token (see :class:`ingest.tokens.AccessTokenProvider`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from config import get_settings
from ingest.raw_store import RAW_TIMESTAMP_FORMAT
from ingest.sync import SyncError, SyncReport, sync_catalog, sync_me, sync_stats, sync_toppers
from ingest.tokens import AccessTokenProvider
from sources.toplogger.adapter import TopLoggerSource
from sources.toplogger.auth import AuthError, sync_lock
from sources.toplogger.client import TopLoggerError

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Pull TopLogger data into dated, immutable raw files under data/raw/.",
)

DryRun = Annotated[bool, typer.Option("--dry-run", help="Make no requests; print the plan.")]
MaxClimbs = Annotated[
    int | None,
    typer.Option("--max-climbs", help="Only the first N climbs. For a cheap first run."),
]
GymId = Annotated[str | None, typer.Option("--gym-id", help="Override the configured gym.")]


def _source(gym_id: str | None = None) -> TopLoggerSource:
    """Build the adapter with a token supplier that acquires at most one token."""
    provider = AccessTokenProvider()
    return TopLoggerSource(gym_id=gym_id, token_supplier=provider.get)


def _report(result: SyncReport) -> None:
    """Print what happened, in a form that is useful in a scheduled job's log."""
    if result.planned:
        typer.echo(f"[dry run] {result.kind}: would call {', '.join(result.planned)}")
        typer.echo(f"[dry run] nothing written under {get_settings().raw_dir}")
        return

    typer.echo(f"{result.kind}: {result.requests} request(s)")
    for path in result.files:
        typer.echo(f"  wrote {path}")
    for key, value in result.notes.items():
        typer.echo(f"  {key}: {value}")


def _run(fn, *args: Any, **kwargs: Any) -> SyncReport:
    """Run one sync under the lock, turning known failures into clean exits.

    A traceback in a nightly job's log tells you less than one line does, and an
    exception rendered by typer risks printing something we redact everywhere else.
    """
    try:
        with sync_lock():
            result = fn(*args, **kwargs)
    except (SyncError, AuthError, TopLoggerError) as exc:
        typer.secho(f"{type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None

    _report(result)
    return result


@app.command()
def catalog(dry_run: DryRun = False, gym_id: GymId = None) -> None:
    """Snapshot the climbs currently on the wall, plus the gym's vocabulary."""
    _run(sync_catalog, _source(gym_id), dry_run=dry_run)


@app.command()
def stats(dry_run: DryRun = False, max_climbs: MaxClimbs = None, gym_id: GymId = None) -> None:
    """Snapshot per-climb grade and rating vote histograms. Needs a catalog first."""
    _run(sync_stats, _source(gym_id), max_climbs=max_climbs, dry_run=dry_run)


@app.command()
def toppers(dry_run: DryRun = False, max_climbs: MaxClimbs = None, gym_id: GymId = None) -> None:
    """Snapshot per-climb flash/redpoint counts. Aggregated before writing."""
    _run(sync_toppers, _source(gym_id), max_climbs=max_climbs, dry_run=dry_run)


@app.command()
def me(
    dry_run: DryRun = False,
    user_id: Annotated[str | None, typer.Option("--user-id")] = None,
    gym_id: GymId = None,
) -> None:
    """Snapshot your own climbing history and the provider's stats on it."""
    _run(sync_me, _source(gym_id), user_id=user_id, dry_run=dry_run)


@app.command(name="all")
def sync_all(dry_run: DryRun = False, max_climbs: MaxClimbs = None, gym_id: GymId = None) -> None:
    """Everything, in dependency order: catalog, then stats and toppers, then me.

    One source instance for the whole run, so the unauthenticated probe and the
    access token are each paid for once rather than four times.
    """
    source = _source(gym_id)
    try:
        with sync_lock():
            for result in (
                sync_catalog(source, dry_run=dry_run),
                sync_stats(source, max_climbs=max_climbs, dry_run=dry_run),
                sync_toppers(source, max_climbs=max_climbs, dry_run=dry_run),
                sync_me(source, dry_run=dry_run),
            ):
                _report(result)
    except (SyncError, AuthError, TopLoggerError) as exc:
        typer.secho(f"{type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None


@app.command()
def probe(gym_id: GymId = None) -> None:
    """Answer the open questions in docs/PROJECT_CONTEXT.md §6 with real requests.

    Writes no climbing data. Two things are asked:

    * Which operations actually require a token (§6.1). The adapter answers this as
      a side effect of trying each one unauthenticated first.
    * What fields ``climbUsers`` really exposes. Introspection is disabled, so we
      send a deliberately invalid selection and read the validation error, which
      names the valid fields — and runs before execution, so it costs no token.
    """
    source = _source(gym_id)
    findings: dict[str, Any] = {}

    with sync_lock():
        try:
            # Touching the catalog is what makes the adapter run its unauthenticated
            # probe; the payload is deliberately discarded, not written.
            source.fetch_catalog()
        except TopLoggerError as exc:
            typer.secho(f"catalog probe failed: {exc}", fg=typer.colors.YELLOW, err=True)

        try:
            # A plain unknown field, not a `__`-prefixed one: names starting with
            # `__` are reserved for introspection, which is disabled here, so they
            # risk being rejected for the wrong reason.
            findings["climb_users_fields"] = source.probe_selection(
                "climbUsers", "  zzzProbeUnknownField"
            )
        except TopLoggerError as exc:
            typer.secho(f"field probe failed: {exc}", fg=typer.colors.YELLOW, err=True)

    findings["auth_required"] = dict(source.auth_findings)
    findings["requests"] = source.request_count

    stamp = datetime.now(UTC).strftime(RAW_TIMESTAMP_FORMAT)
    out = get_settings().raw_dir / source.name / "_probe" / f"{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(findings, indent=2, sort_keys=True), encoding="utf-8")

    typer.echo(json.dumps(findings, indent=2, sort_keys=True))
    typer.echo(f"\nwrote {out}")
    typer.echo("Record these in docs/PROJECT_CONTEXT.md §6 — that is the point of this command.")


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m ingest`
    app()
