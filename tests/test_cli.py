"""Tests for the ``python -m ingest`` command surface.

The adapter is swapped for the stub `Source` from ``test_sync``: these tests are
about dispatch, flags and failure reporting, not about TopLogger. Nothing here
touches the network (hard rule 6).
"""

import pytest
from typer.testing import CliRunner

from ingest import cli
from tests.test_sync import FakeSource

runner = CliRunner()


@pytest.fixture(autouse=True)
def stub_source(monkeypatch):
    """Replace the composition root's adapter, so no token or socket is ever needed."""
    source = FakeSource()
    monkeypatch.setattr(cli, "_source", lambda gym_id=None: source)
    return source


@pytest.fixture(autouse=True)
def no_lock(monkeypatch, tmp_path):
    """Point the sync lock at tmp_path so tests never fight the developer's real one."""
    from contextlib import contextmanager

    from sources.toplogger import auth

    @contextmanager
    def _lock(lock_path=None):
        yield

    monkeypatch.setattr(cli, "sync_lock", _lock)
    monkeypatch.setattr(auth, "sync_lock", _lock)


def test_help_lists_every_command():
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    for command in ("catalog", "stats", "toppers", "me", "all", "probe"):
        assert command in result.output


def test_bare_invocation_shows_help():
    """`no_args_is_help`: a scheduled job typo should not silently do nothing."""
    assert runner.invoke(cli.app, []).exit_code != 0


def test_catalog_dry_run_makes_no_requests(stub_source):
    result = runner.invoke(cli.app, ["catalog", "--dry-run"])

    assert result.exit_code == 0
    assert stub_source.calls == []
    assert "dry run" in result.output


def test_catalog_writes_and_reports(stub_source):
    result = runner.invoke(cli.app, ["catalog"])

    assert result.exit_code == 0, result.output
    assert stub_source.calls == ["fetch_catalog", "fetch_gym_metadata"]
    assert "wrote" in result.output


def test_stats_without_a_catalog_exits_nonzero(stub_source):
    result = runner.invoke(cli.app, ["stats"])

    assert result.exit_code == 1
    assert "SyncError" in result.output
    assert stub_source.calls == []


def test_me_without_a_user_id_exits_nonzero(monkeypatch):
    monkeypatch.delenv("TOPLOGGER_USER_ID", raising=False)

    result = runner.invoke(cli.app, ["me"])

    assert result.exit_code == 1
    assert "SyncError" in result.output


def test_all_runs_every_sync_in_order(stub_source):
    result = runner.invoke(cli.app, ["all"])

    # `me` needs a user id, which is not configured in the isolated test env, so the
    # run is expected to stop there — what matters is that it got that far in order.
    assert stub_source.calls[:4] == [
        "fetch_catalog",
        "fetch_gym_metadata",
        "fetch_climb_stats",
        "fetch_climb_popularity",
    ]
    assert result.exit_code == 1


def test_all_uses_one_source_for_the_whole_run(stub_source, monkeypatch):
    """Otherwise the auth probe and the access token get paid for four times."""
    built = []
    monkeypatch.setattr(cli, "_source", lambda gym_id=None: (built.append(1), stub_source)[1])

    runner.invoke(cli.app, ["all", "--dry-run"])

    assert len(built) == 1
