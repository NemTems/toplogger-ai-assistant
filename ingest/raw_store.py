"""The on-disk contract for raw API responses.

Every response is written here, untouched and dated, before anything parses it.
Loaders, features and evals read these files and never the live API. The payload is
kept exactly as the provider sent it, and the envelope around it records where it
came from and when.

* **No credentials on disk.** A write is refused if the serialised bytes look like
  they carry a credential. Nothing is redacted: the payload is written verbatim or
  not at all.
* **Append-only.** A file is never overwritten. Two writes of the same kind in the
  same second get a ``_NN`` suffix — see :func:`write_raw`.
* **Atomic.** A killed run leaves no half-written file.

``aggregated=True`` marks the one kind not saved untouched. Topper data names other
climbers, so it is aggregated into per-climb counts in memory and only the counts
are written. The flag is in the envelope so a reader of the file can tell.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from config import get_settings
from sources.toplogger.client import looks_like_jwt

__all__ = [
    "RAW_TIMESTAMP_FORMAT",
    "RawWriteRefused",
    "latest_raw",
    "list_raw",
    "read_raw",
    "write_raw",
]

RAW_TIMESTAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"
"""UTC stamp naming a raw file. Lexicographic order is chronological."""

# `kind` and `source` become directory names.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Forbidden values shorter than this are not searched for: they are likely sentinels,
# not credentials, and would refuse writes by coincidence. Same floor as client.redact.
_MIN_FORBIDDEN_LENGTH = 8

# Same-second writes get `_01`.. appended. Two digits keeps them sorting after the
# bare name and among themselves; past that something is wrong with the clock.
_MAX_SAME_SECOND = 99


class RawWriteRefused(Exception):
    """A payload looked like it contained a credential; nothing was written."""


def write_raw(
    kind: str,
    payload: Any,
    *,
    source: str = "toplogger",
    operation: str | None = None,
    variables: Mapping[str, Any] | None = None,
    aggregated: bool = False,
    forbidden: Sequence[SecretStr | str] = (),
    root: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> Path:
    """Write ``payload`` to ``<root>/<source>/<kind>/<YYYY-MM-DD>T<HHMMSS>Z.json``.

    The file holds an envelope — ``source``, ``kind``, ``fetched_at``, ``operation``,
    ``variables``, ``aggregated`` — with the provider's response verbatim under
    ``payload``.

    Nothing is ever overwritten. If that path is already taken, because two writes of
    the same kind landed in the same second, the second one becomes
    ``...Z_01.json``, then ``_02``, and so on. The suffix sorts after the bare name,
    so :func:`latest_raw` stays correct.

    Args:
        kind: What was fetched — ``"catalog"``, ``"gym"``, ``"stats"``, ``"toppers"``,
            ``"me"``. Becomes a directory name.
        payload: The JSON-serialisable response, stored as-is.
        source: Adapter that produced it. Becomes a directory name.
        operation: GraphQL operation name, recorded for provenance.
        variables: Operation variables, recorded for provenance.
        aggregated: ``True`` when the payload was reduced in memory before writing
            (toppers — see the module docstring).
        forbidden: Exact secret values that must not appear anywhere in the file.
            Shape matching cannot catch a short opaque credential, so pass the ones
            the request actually carried.
        root: Directory holding the per-source trees. Defaults to ``settings.raw_dir``.
        now: Clock returning the moment of the fetch. Defaults to UTC now; injectable
            for tests.

    Returns:
        The path written.

    Raises:
        RawWriteRefused: The serialised file looked like it carried a credential.
            Nothing was written.
        FileExistsError: More than 100 writes of one kind in a single second.
        ValueError: ``kind`` or ``source`` is not usable as a path segment.
        TypeError: ``payload`` is not JSON-serialisable.
    """
    directory = (
        (get_settings().raw_dir if root is None else root)
        / _segment("source", source)
        / _segment("kind", kind)
    )
    moment = _as_utc((now or _utc_now)())

    envelope = {
        "source": source,
        "kind": kind,
        "fetched_at": _isoformat(moment),
        "operation": operation,
        "variables": dict(variables) if variables is not None else None,
        "aggregated": aggregated,
        "payload": payload,
    }
    # Serialise first: the guard runs on the exact bytes that would hit the disk, and
    # a non-serialisable payload fails here, before anything has been created.
    text = json.dumps(envelope, ensure_ascii=False)
    _refuse_credentials(text, source=source, kind=kind, forbidden=forbidden)

    directory.mkdir(parents=True, exist_ok=True)
    target = _free_path(directory, moment.strftime(RAW_TIMESTAMP_FORMAT))
    _atomic_write(target, text)
    return target


def read_raw(path: Path) -> dict[str, Any]:
    """Return the envelope stored at ``path``.

    Raises:
        ValueError: The file is not a JSON object, so it is not one of ours.
    """
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(envelope, dict):
        raise ValueError(f"{path} does not hold a raw envelope object")
    return envelope


def list_raw(kind: str, *, source: str = "toplogger", root: Path | None = None) -> list[Path]:
    """Return every raw file for ``kind``, oldest first."""
    directory = (
        (get_settings().raw_dir if root is None else root)
        / _segment("source", source)
        / _segment("kind", kind)
    )
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def latest_raw(kind: str, *, source: str = "toplogger", root: Path | None = None) -> Path | None:
    """Return the newest raw file for ``kind``, or ``None`` if there is none yet.

    "Newest" is by name, which :data:`RAW_TIMESTAMP_FORMAT` makes equivalent to
    newest by fetch time without stat-ing every file.
    """
    files = list_raw(kind, source=source, root=root)
    return files[-1] if files else None


def _refuse_credentials(
    text: str,
    *,
    source: str,
    kind: str,
    forbidden: Sequence[SecretStr | str],
) -> None:
    """Raise :class:`RawWriteRefused` if ``text`` looks like it carries a credential.

    Does not use ``client.redact``: its "40+ opaque characters" rule would fire on a
    long ``picPath`` segment.
    """
    if looks_like_jwt(text):
        raise RawWriteRefused(
            f"refusing to write {source}/{kind}: the response contains a JWT-shaped string. "
            "Nothing was written."
        )
    for secret in forbidden:
        value = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        if value and len(value) >= _MIN_FORBIDDEN_LENGTH and value in text:
            raise RawWriteRefused(
                f"refusing to write {source}/{kind}: the response contains a known secret. "
                "Nothing was written."
            )


def _atomic_write(target: Path, text: str) -> None:
    """Write ``text`` to ``target`` in one step, leaving nothing behind on failure.

    A loader may run against this directory at any time, so a reader sees either no
    file or the whole file.
    """
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}-", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _free_path(directory: Path, stamp: str) -> Path:
    """Return the first unused file name for ``stamp`` in ``directory``."""
    target = directory / f"{stamp}.json"
    if not target.exists():
        return target
    for n in range(1, _MAX_SAME_SECOND + 1):
        candidate = directory / f"{stamp}_{n:02d}.json"
        if not candidate.exists():
            return candidate
    raise FileExistsError(
        f"more than {_MAX_SAME_SECOND} raw writes in one second under {directory}"
    )


def _segment(name: str, value: str) -> str:
    """Return ``value`` if it is safe to use as a directory name."""
    if not isinstance(value, str) or not _SAFE_SEGMENT.match(value):
        raise ValueError(f"{name} must be a bare identifier, got {value!r}")
    return value


def _utc_now() -> datetime:
    """Current time, UTC-aware."""
    return datetime.now(UTC)


def _as_utc(moment: datetime) -> datetime:
    """Normalise a clock reading to UTC, treating a naive one as already UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _isoformat(moment: datetime) -> str:
    """ISO-8601 in UTC with a ``Z`` suffix."""
    return moment.isoformat().replace("+00:00", "Z")
