"""Repo hygiene guard — the real acceptance check for Phase 0.

Verifies two things git itself can tell us, without ever touching the
network or any personal data:

1. Paths that would hold raw/derived data or secrets are actually gitignored.
2. No file *tracked* by git contains a credential-shaped string.

If this repo is not a git repository (or git is unavailable), the whole
module is skipped rather than failed — there is nothing to check.
"""

import re
import subprocess
from pathlib import Path

import pytest

from config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )


def _is_git_repo() -> bool:
    try:
        result = _git("rev-parse", "--is-inside-work-tree")
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


pytestmark = pytest.mark.skipif(
    not _is_git_repo(), reason="not a git repository / git executable unavailable"
)


# --- 1. gitignore coverage -------------------------------------------------


@pytest.mark.parametrize(
    "relpath",
    [
        "data/raw/x.json",
        "data/db/toplogger.sqlite",
        ".env",
    ],
)
def test_sensitive_paths_are_gitignored(relpath):
    result = _git("check-ignore", "-q", relpath)
    assert result.returncode == 0, (
        f"{relpath!r} is NOT covered by .gitignore (git check-ignore exit={result.returncode})"
    )


# --- 2. no credential-shaped strings in tracked files ----------------------
#
# We scan `git ls-files` (tracked files only — never data/ or .venv/, which
# aren't tracked anyway) for patterns that look like an actual leaked
# credential, not just prose that mentions the concept.
#
# Design notes on avoiding false positives while still catching a real leak:
#
# - *.md files are excluded outright. AGENTS.md and CLAUDE.md legitimately
#   discuss `authSignin`, "refresh token", etc. while describing the rules
#   against using them — scanning docs for these words would only ever find
#   prose, never a leak, so we don't scan docs at all.
# - This test file itself is excluded, since it necessarily contains these
#   patterns as string literals in order to define them.
# - "refresh_token" is matched only in *assignment* position with a quoted
#   literal value of plausible token length (e.g. `refresh_token = "abc..."`
#   or `"refresh_token": "abc..."`). Plain references to the field/key name
#   (`response["refresh_token"]`, `refresh_token: str`, passing a variable)
#   do not match — those are normal, expected code in sources/toplogger/.
# - "eyJ" (base64 JWT header prefix) is matched only when followed by a
#   run of base64url characters long enough to be an actual token fragment,
#   not a coincidental three-letter substring.
# - "TOPLOGGER_PASSWORD" and "authSignin(" are matched as bare substrings:
#   neither should ever appear in tracked code at all. `authSignin(` (with
#   the open paren, i.e. an actual call) deliberately does NOT match the
#   allowed `authSigninRefreshToken(` operation, since the character after
#   "authSignin" there is "R", not "(".
CREDENTIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "jwt-shaped string": re.compile(r"eyJ[A-Za-z0-9_-]{15,}"),
    "refresh_token literal assignment": re.compile(
        r"""refresh_token["']?\s*[:=]\s*["'][A-Za-z0-9_.-]{8,}["']"""
    ),
    "TOPLOGGER_PASSWORD env var": re.compile(r"TOPLOGGER_PASSWORD"),
    "authSignin( call (forbidden; only authSigninRefreshToken is allowed)": re.compile(
        r"authSignin\("
    ),
}

# Extensions that are binary / not worth decoding as text.
_SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".sqlite",
    ".db",
    ".woff",
    ".woff2",
    ".ttf",
}


def _tracked_files() -> list[str]:
    result = _git("ls-files")
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_no_credential_shaped_strings_in_tracked_files():
    offenders: list[tuple[str, str]] = []

    for relpath in _tracked_files():
        if relpath.endswith(".md"):
            continue
        if relpath == THIS_FILE:
            continue
        if Path(relpath).suffix.lower() in _SKIP_SUFFIXES:
            continue

        full_path = REPO_ROOT / relpath
        if not full_path.is_file():
            continue

        try:
            text = full_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary or unreadable file — not a text-based leak vector we can scan.
            continue

        for label, pattern in CREDENTIAL_PATTERNS.items():
            if pattern.search(text):
                offenders.append((relpath, label))

    assert not offenders, f"credential-shaped strings found in tracked files: {offenders}"


# --- 3. Settings never renders a credential-shaped value -------------------


def test_settings_repr_and_str_have_no_credential_shaped_value():
    settings = Settings()
    for rendered in (repr(settings), str(settings)):
        assert "eyJ" not in rendered
        assert "refresh_token" not in rendered.lower()
        assert "password" not in rendered.lower()
        assert "authsignin" not in rendered.lower()
