"""Tests for the sync commands.

Driven by a stub ``Source``, not by the TopLogger adapter. That is the point of the
protocol: if these tests needed a real adapter, ``ingest/`` would not actually be
source-agnostic. Nothing here touches the network (hard rule 6).
"""

import json

import pytest

from ingest.raw_store import latest_raw, read_raw
from ingest.sync import SyncError, sync_catalog, sync_me, sync_stats, sync_toppers
from sources.base import Source

FAKE_IDS = ["climb1", "climb2", "climb3"]


class FakeSource:
    """A `Source` that counts its calls and returns canned payloads."""

    def __init__(self, *, auth_findings=None):
        self.calls: list[str] = []
        self.request_count = 0
        self._auth_findings = auth_findings or {}

    @property
    def name(self) -> str:
        return "fake"

    @property
    def auth_findings(self) -> dict[str, bool]:
        return self._auth_findings

    def _record(self, what: str, requests: int = 1):
        self.calls.append(what)
        self.request_count += requests

    def fetch_catalog(self):
        self._record("fetch_catalog")
        return {"climbs": [{"id": cid, "grade": 600} for cid in FAKE_IDS]}

    def fetch_gym_metadata(self):
        self._record("fetch_gym_metadata")
        return {"walls": [{"id": "w1", "name": "Mad Rock"}]}

    def climb_ids(self, catalog):
        return [c["id"] for c in catalog.get("climbs", [])]

    def fetch_climb_stats(self, climb_ids):
        self._record("fetch_climb_stats")
        return {cid: {"gradeVoteStats": []} for cid in climb_ids}

    def fetch_climb_popularity(self, climb_ids):
        self._record("fetch_climb_popularity")
        return {cid: {"tick_count": 3, "flash_count": 1} for cid in climb_ids}

    def fetch_user_history(self, user_id):
        self._record("fetch_user_history")
        return {"climbDays": []}

    def fetch_user_stats(self, user_id):
        self._record("fetch_user_stats")
        return {"gradeDistribution": []}


def test_fake_source_satisfies_the_protocol():
    """If this fails, the stub has drifted from `Source` and the other tests lie."""
    assert isinstance(FakeSource(), Source)


def test_catalog_writes_climbs_and_vocabulary(tmp_path):
    source = FakeSource()

    report = sync_catalog(source, root=tmp_path)

    assert source.calls == ["fetch_catalog", "fetch_gym_metadata"]
    assert len(report.files) == 2
    assert {p.parent.name for p in report.files} == {"catalog", "gym"}
    assert all(p.parent.parent.name == "fake" for p in report.files)


def test_catalog_payload_round_trips(tmp_path):
    source = FakeSource()
    report = sync_catalog(source, root=tmp_path)

    envelope = read_raw(report.files[0])
    assert envelope["payload"] == source.fetch_catalog()
    assert envelope["source"] == "fake"
    assert envelope["kind"] == "catalog"
    assert envelope["operation"] == "fetch_catalog"


def test_dry_run_makes_no_calls_and_writes_nothing(tmp_path):
    source = FakeSource()

    report = sync_catalog(source, dry_run=True, root=tmp_path)

    assert source.calls == []
    assert report.files == ()
    assert report.planned == ("fetch_catalog", "fetch_gym_metadata")
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.parametrize("command", [sync_stats, sync_toppers])
def test_dry_run_needs_no_catalog_on_disk(tmp_path, command):
    """A dry run must not fail just because the prerequisite is missing."""
    assert command(FakeSource(), dry_run=True, root=tmp_path).files == ()


@pytest.mark.parametrize("command", [sync_stats, sync_toppers])
def test_missing_catalog_is_a_clear_error(tmp_path, command):
    with pytest.raises(SyncError, match="catalog"):
        command(FakeSource(), root=tmp_path)


def test_stats_takes_climb_ids_from_the_stored_catalog(tmp_path):
    source = FakeSource()
    sync_catalog(source, root=tmp_path)

    report = sync_stats(source, root=tmp_path)

    payload = read_raw(report.files[0])["payload"]
    assert sorted(payload) == sorted(FAKE_IDS)
    assert report.notes["climbs"] == 3


def test_stats_records_which_catalog_it_used(tmp_path):
    """Provenance: a stats file is only meaningful against the wall it was taken from."""
    source = FakeSource()
    sync_catalog(source, root=tmp_path)
    catalog_path = latest_raw("catalog", source="fake", root=tmp_path)

    report = sync_stats(source, root=tmp_path)

    assert read_raw(report.files[0])["variables"]["from_catalog"] == catalog_path.name


def test_max_climbs_caps_the_run(tmp_path):
    source = FakeSource()
    sync_catalog(source, root=tmp_path)

    report = sync_stats(source, max_climbs=2, root=tmp_path)

    assert report.notes["climbs"] == 2
    assert len(read_raw(report.files[0])["payload"]) == 2


def test_toppers_is_marked_aggregated(tmp_path):
    """Hard rule 4 beats 'raw is immutable'; the file has to say so."""
    source = FakeSource()
    sync_catalog(source, root=tmp_path)

    report = sync_toppers(source, root=tmp_path)

    assert read_raw(report.files[0])["aggregated"] is True


def test_catalog_is_not_marked_aggregated(tmp_path):
    source = FakeSource()
    report = sync_catalog(source, root=tmp_path)
    assert read_raw(report.files[0])["aggregated"] is False


def test_me_requires_a_user_id(tmp_path, monkeypatch):
    monkeypatch.delenv("TOPLOGGER_USER_ID", raising=False)

    with pytest.raises(SyncError, match="user ID"):
        sync_me(FakeSource(), root=tmp_path)


def test_me_writes_history_and_stats(tmp_path):
    source = FakeSource()

    report = sync_me(source, user_id="me-123", root=tmp_path)

    assert source.calls == ["fetch_user_history", "fetch_user_stats"]
    assert {p.parent.name for p in report.files} == {"history", "user_stats"}


def test_user_id_is_not_written_into_the_envelope(tmp_path):
    """It is personal data and it is not provenance we need."""
    report = sync_me(FakeSource(), user_id="me-123", root=tmp_path)

    for path in report.files:
        assert "me-123" not in json.dumps(read_raw(path)["variables"] or {})


def test_report_counts_requests(tmp_path):
    source = FakeSource()
    report = sync_catalog(source, root=tmp_path)
    assert report.requests == 2


def test_auth_findings_reach_the_report(tmp_path):
    """The unauthenticated probe's answer is the point of running it."""
    source = FakeSource(auth_findings={"Climbs": False, "ClimbDays": True})

    report = sync_catalog(source, root=tmp_path)

    assert report.notes["auth_required"] == {"Climbs": False, "ClimbDays": True}


def test_snapshots_accumulate(tmp_path):
    """Second run of the day must not overwrite the first (AGENTS.md)."""
    source = FakeSource()

    first = sync_catalog(source, root=tmp_path)
    second = sync_catalog(source, root=tmp_path)

    assert first.files[0] != second.files[0]
    assert first.files[0].exists() and second.files[0].exists()
    assert len(list((tmp_path / "fake" / "catalog").glob("*.json"))) == 2
