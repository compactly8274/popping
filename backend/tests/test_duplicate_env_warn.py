"""Tests for the .env duplicate-key scanner in app.main.

The function ``_warn_on_duplicate_env_keys`` is called once at
lifespan startup with the path of the env file pydantic-settings
is reading. It logs a WARNING for any key that appears more than
once with different values (pydantic-settings + os.environ set
semantics take the LAST one, so a duplicate with conflicting
values is a silent override). Same-value duplicates log at INFO.

The bug this guards: ``REDDIT_DIRECT_DISABLED=false`` and
``REDDIT_DIRECT_DISABLED=true`` in the same .env, with the
``true`` shadowing the ``false`` and silently turning off the
direct-Atom Reddit fetches. Operators had no way to see the
conflict in the container logs.

Tests use ``caplog`` to capture the log lines.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile

os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_USER", "x")
os.environ.setdefault("POSTGRES_PASSWORD", "x")
os.environ.setdefault("POSTGRES_DB", "x")
os.environ.setdefault("EMBEDDING_ENABLED", "false")
os.environ.setdefault("ASSETS_DIR", tempfile.mkdtemp(prefix="smoke-"))

sys.path.insert(0, "/tmp/popping-review/backend")

import pytest

from app.main import _warn_on_duplicate_env_keys


def _write_env(content: str) -> str:
    """Write ``content`` to a temp .env and return the path."""
    fd, path = tempfile.mkstemp(suffix=".env", prefix="popping-")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


class TestWarnOnDuplicateEnvKeys:
    def test_clean_env_no_log(self, caplog) -> None:
        path = _write_env("FOO=1\nBAR=2\nBAZ=3\n")
        try:
            with caplog.at_level(logging.INFO, logger="popping"):
                _warn_on_duplicate_env_keys(path)
            assert "FOO" not in caplog.text
            assert "BAR" not in caplog.text
            assert "BAZ" not in caplog.text
        finally:
            os.unlink(path)

    def test_conflicting_duplicate_warns(self, caplog) -> None:
        # The exact failure mode from the live popping deploy:
        # the same key set to different values at different
        # points in the file.
        path = _write_env(
            "REDDIT_DIRECT_DISABLED=false\n"
            "OTHER=1\n"
            "REDDIT_DIRECT_DISABLED=true\n"
        )
        try:
            with caplog.at_level(logging.INFO, logger="popping"):
                _warn_on_duplicate_env_keys(path)
            assert "REDDIT_DIRECT_DISABLED" in caplog.text
            assert "CONFLICTING" in caplog.text
            assert "line 1" in caplog.text
            assert "line 3" in caplog.text
            assert "effective='true'" in caplog.text
            # Other duplicates don't fire.
            assert "OTHER" not in caplog.text
        finally:
            os.unlink(path)

    def test_same_value_duplicate_infos(self, caplog) -> None:
        # A "duplicate" with the same value is harmless (it just
        # shadows with the same value) but still worth surfacing
        # at INFO so the operator knows the file has redundancy.
        path = _write_env("FOO=1\nBAR=2\nFOO=1\n")
        try:
            with caplog.at_level(logging.INFO, logger="popping"):
                _warn_on_duplicate_env_keys(path)
            assert "FOO" in caplog.text
            assert "same value" in caplog.text
            assert "CONFLICTING" not in caplog.text
        finally:
            os.unlink(path)

    def test_three_way_conflict(self, caplog) -> None:
        # Three different values for the same key. The warning
        # should list all three and the last one as the
        # effective value.
        path = _write_env("KEY=a\nKEY=b\nKEY=c\n")
        try:
            with caplog.at_level(logging.INFO, logger="popping"):
                _warn_on_duplicate_env_keys(path)
            assert "KEY" in caplog.text
            assert "line 1" in caplog.text
            assert "line 2" in caplog.text
            assert "line 3" in caplog.text
            assert "effective='c'" in caplog.text
        finally:
            os.unlink(path)

    def test_comments_and_blanks_skipped(self, caplog) -> None:
        # Comment lines and blank lines shouldn't count as
        # duplicate keys even if they contain an = sign.
        path = _write_env(
            "# FOO=ignored\n"
            "\n"
            "FOO=1\n"
            "  # indented comment\n"
            "FOO=2\n"
        )
        try:
            with caplog.at_level(logging.INFO, logger="popping"):
                _warn_on_duplicate_env_keys(path)
            # FOO appears twice (lines 3 and 5) with different
            # values. The comment on line 1 and the blank on
            # line 2 are skipped.
            assert "FOO" in caplog.text
            assert "CONFLICTING" in caplog.text
        finally:
            os.unlink(path)

    def test_quoted_values(self, caplog) -> None:
        # Quoted values are stripped of their quotes for the
        # comparison (matching pydantic-settings' own behavior).
        path = _write_env('KEY="abc"\nKEY=abc\n')
        try:
            with caplog.at_level(logging.INFO, logger="popping"):
                _warn_on_duplicate_env_keys(path)
            # Both lines parse to "abc" — same value, INFO not
            # WARNING. (pydantic-settings would also treat these
            # as the same value.)
            assert "KEY" in caplog.text
            assert "same value" in caplog.text
        finally:
            os.unlink(path)
