"""Tests for the raw-file store.

Nothing here touches the network; the only I/O is into ``tmp_path``.

The JWT used against the credential guard is assembled at runtime:
``test_repo_hygiene.py`` fails on an ``eyJ...`` literal in any tracked file.
"""

import base64
import json
import os
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import SecretStr

from config import get_settings
from ingest.raw_store import (
    RAW_TIMESTAMP_FORMAT,
    RawWriteRefused,
    latest_raw,
    list_raw,
    read_raw,
    write_raw,
)

FAKE_SECRET = "fake-refresh-token-xyz"  # noqa: S105 - obviously fake, not a credential


def fake_jwt() -> str:
    """A JWT-shaped string built at runtime (see the module docstring)."""
    segments = [
        base64.urlsafe_b64encode(part).decode().rstrip("=")
        for part in (b'{"alg":"HS256"}', b'{"sub":"1"}')
    ]
    return ".".join([*segments, "abcdefghijklmnop"])


def at(hour: int = 12, minute: int = 0, second: int = 0):
    """A fixed-clock callable for ``write_raw(now=...)``."""
    moment = datetime(2026, 9, 23, hour, minute, second, tzinfo=UTC)
    return lambda: moment


# --- path scheme -----------------------------------------------------------


def test_path_follows_the_source_kind_timestamp_scheme(tmp_path):
    path = write_raw("catalog", {"climbs": []}, root=tmp_path, now=at(12, 34, 56))

    assert path.relative_to(tmp_path).as_posix() == "toplogger/catalog/2026-09-23T123456Z.json"


def test_timestamp_format_round_trips(tmp_path):
    path = write_raw("catalog", {}, root=tmp_path, now=at(1, 2, 3))

    parsed = datetime.strptime(path.stem, RAW_TIMESTAMP_FORMAT)
    assert (parsed.hour, parsed.minute, parsed.second) == (1, 2, 3)


def test_non_utc_clock_is_converted(tmp_path):
    """A clock in another zone must still name the file in UTC."""
    berlin = timezone(timedelta(hours=2))
    moment = datetime(2026, 9, 23, 14, 0, 0, tzinfo=berlin)

    path = write_raw("catalog", {}, root=tmp_path, now=lambda: moment)

    assert path.name == "2026-09-23T120000Z.json"
    assert read_raw(path)["fetched_at"] == "2026-09-23T12:00:00Z"


def test_defaults_to_the_configured_raw_dir(tmp_path):
    """conftest chdirs to tmp_path, so settings.raw_dir lands under it."""
    path = write_raw("catalog", {}, now=at())

    assert path.is_file()
    assert path.parent == get_settings().raw_dir / "toplogger" / "catalog"


def test_kind_that_would_escape_the_root_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="kind"):
        write_raw("../../etc", {}, root=tmp_path, now=at())


# --- envelope --------------------------------------------------------------


def test_envelope_records_provenance(tmp_path):
    path = write_raw(
        "stats",
        {"c0": {"grade": 600}},
        root=tmp_path,
        operation="gymClimbStats",
        variables={"gymId": "abc"},
        now=at(9, 30, 0),
    )

    envelope = read_raw(path)
    assert envelope == {
        "source": "toplogger",
        "kind": "stats",
        "fetched_at": "2026-09-23T09:30:00Z",
        "operation": "gymClimbStats",
        "variables": {"gymId": "abc"},
        "aggregated": False,
        "payload": {"c0": {"grade": 600}},
    }


def test_payload_is_stored_untouched(tmp_path):
    """What a loader reads back is what the API said."""
    payload = {
        "climbs": [
            {"id": "x1", "name": None, "grade": 617, "tags": ["slab", "crimp"]},
            {"id": "x2", "grade": 0, "nested": {"deep": [1, 2, {"three": True}]}},
        ],
        "unicode": "Mad Rock · set 31 Aug",
    }

    path = write_raw("catalog", payload, root=tmp_path, now=at())

    assert read_raw(path)["payload"] == payload


def test_aggregated_flag_round_trips(tmp_path):
    """Toppers are reduced in memory before writing; the file must say so."""
    path = write_raw("toppers", {"x1": {"flash": 3}}, aggregated=True, root=tmp_path, now=at())

    assert read_raw(path)["aggregated"] is True


def test_source_can_be_another_adapter(tmp_path):
    path = write_raw("problems", {}, source="moonboard", root=tmp_path, now=at())

    assert path.relative_to(tmp_path).parts[0] == "moonboard"
    assert read_raw(path)["source"] == "moonboard"


# --- credential guard ------------------------------------------------------


def test_refuses_a_jwt_shaped_string_anywhere_in_the_payload(tmp_path):
    payload = {"user": {"profile": {"session": fake_jwt()}}}

    with pytest.raises(RawWriteRefused):
        write_raw("me", payload, root=tmp_path, now=at())


def test_refuses_a_known_secret_by_value(tmp_path):
    """Shape matching cannot catch a short opaque token, so exact values are checked."""
    with pytest.raises(RawWriteRefused):
        write_raw(
            "me",
            {"echoed": f"token {FAKE_SECRET} accepted"},
            forbidden=[SecretStr(FAKE_SECRET)],
            root=tmp_path,
            now=at(),
        )


def test_a_known_secret_in_the_variables_is_also_caught(tmp_path):
    """The whole envelope is scanned, not just the payload."""
    with pytest.raises(RawWriteRefused):
        write_raw(
            "me",
            {"ok": True},
            variables={"refreshToken": FAKE_SECRET},
            forbidden=[FAKE_SECRET],
            root=tmp_path,
            now=at(),
        )


def test_refused_write_leaves_nothing_on_disk(tmp_path):
    with pytest.raises(RawWriteRefused):
        write_raw("me", {"session": fake_jwt()}, root=tmp_path, now=at())

    assert list(tmp_path.rglob("*")) == [], "a refused write must not create a file"


def test_refusal_does_not_echo_the_secret(tmp_path):
    """Not even the error that caught it may carry the value."""
    with pytest.raises(RawWriteRefused) as excinfo:
        write_raw("me", {"t": FAKE_SECRET}, forbidden=[FAKE_SECRET], root=tmp_path, now=at())

    assert FAKE_SECRET not in str(excinfo.value)
    assert FAKE_SECRET not in repr(excinfo.value)


def test_long_opaque_field_is_not_mistaken_for_a_credential(tmp_path):
    """A media path, a long opaque run of base64-ish characters, is written as-is.

    Catalog payloads are full of them.
    """
    pic = "a1b2c3d4e5f6g7h8i9j0" * 4

    path = write_raw("catalog", {"picPath": pic}, root=tmp_path, now=at())

    assert read_raw(path)["payload"]["picPath"] == pic


def test_short_forbidden_value_does_not_shred_a_write(tmp_path):
    """A one-character 'secret' would otherwise match every file ever written."""
    path = write_raw("catalog", {"grade": 600}, forbidden=["x"], root=tmp_path, now=at())

    assert path.is_file()


# --- append-only and atomicity ---------------------------------------------


def test_same_second_write_does_not_clobber(tmp_path):
    first = write_raw("catalog", {"n": 1}, root=tmp_path, now=at())
    second = write_raw("catalog", {"n": 2}, root=tmp_path, now=at())

    assert first != second
    assert second.name == "2026-09-23T120000Z_01.json"
    assert read_raw(first)["payload"] == {"n": 1}, "the first file must be untouched"
    assert read_raw(second)["payload"] == {"n": 2}


def test_latest_raw_returns_the_second_of_two_same_second_writes(tmp_path):
    """The accidental double-run: both writes survive and the newer one still wins."""
    first = write_raw("catalog", {"run": 1}, root=tmp_path, now=at())
    second = write_raw("catalog", {"run": 2}, root=tmp_path, now=at())

    assert first.exists() and second.exists()
    assert latest_raw("catalog", root=tmp_path) == second
    assert read_raw(second)["payload"] == {"run": 2}


def test_same_second_suffix_keeps_sorting_chronological(tmp_path):
    """latest_raw sorts by name, so a suffixed file must sort after the bare one."""
    paths = [write_raw("catalog", {"n": n}, root=tmp_path, now=at()) for n in range(3)]

    assert list_raw("catalog", root=tmp_path) == paths
    assert latest_raw("catalog", root=tmp_path) == paths[-1]


def test_the_suffix_separator_sorts_after_the_bare_stem(tmp_path):
    """A ``_01``-suffixed name sorts after the bare one.

    ``-`` (0x2D) sorts *before* ``.`` (0x2E), so a ``...Z-2.json`` suffix would compare
    older than ``...Z.json`` and latest_raw would hand a loader the first write of the
    second. ``_`` (0x5F) sorts after. latest_raw's name ordering depends on this.
    """
    first = write_raw("catalog", {"n": 1}, root=tmp_path, now=at())
    second = write_raw("catalog", {"n": 2}, root=tmp_path, now=at())

    assert first.name < second.name
    assert second.name.replace("_01", "-1") < first.name, "a '-' suffix would invert this"


def test_an_existing_file_is_never_overwritten(tmp_path):
    directory = tmp_path / "toplogger" / "catalog"
    directory.mkdir(parents=True)
    squatter = directory / "2026-09-23T120000Z.json"
    squatter.write_text("do not touch me")

    write_raw("catalog", {"n": 1}, root=tmp_path, now=at())

    assert squatter.read_text() == "do not touch me"


def test_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    """An interrupted run must not leave a half-written file for a loader to read."""

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="disk full"):
        write_raw("catalog", {"n": 1}, root=tmp_path, now=at())

    directory = tmp_path / "toplogger" / "catalog"
    assert list(directory.iterdir()) == [], "no temp file and no partial .json may survive"


def test_unserialisable_payload_creates_no_file(tmp_path):
    with pytest.raises(TypeError):
        write_raw("catalog", {"when": object()}, root=tmp_path, now=at())

    assert list(tmp_path.rglob("*.json")) == []


# --- listing ---------------------------------------------------------------


def test_latest_raw_returns_the_newest(tmp_path):
    write_raw("catalog", {"day": 1}, root=tmp_path, now=at(10))
    write_raw("catalog", {"day": 3}, root=tmp_path, now=at(12))
    write_raw("catalog", {"day": 2}, root=tmp_path, now=at(11))

    newest = latest_raw("catalog", root=tmp_path)

    assert newest is not None
    assert read_raw(newest)["payload"] == {"day": 3}


def test_latest_raw_is_none_when_nothing_was_written(tmp_path):
    assert latest_raw("catalog", root=tmp_path) is None


def test_listing_one_kind_ignores_the_others(tmp_path):
    write_raw("catalog", {}, root=tmp_path, now=at(10))
    write_raw("stats", {}, root=tmp_path, now=at(11))

    assert [p.parent.name for p in list_raw("catalog", root=tmp_path)] == ["catalog"]


def test_listing_ignores_non_json_files(tmp_path):
    written = write_raw("catalog", {}, root=tmp_path, now=at())
    (written.parent / "notes.txt").write_text("scratch")

    assert list_raw("catalog", root=tmp_path) == [written]


def test_read_raw_rejects_a_file_that_is_not_an_envelope(tmp_path):
    path = tmp_path / "bare.json"
    path.write_text(json.dumps([1, 2, 3]))

    with pytest.raises(ValueError, match="envelope"):
        read_raw(path)
