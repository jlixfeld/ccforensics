from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ccforensics.index import ensure_schema, open_connection
from ccforensics.report.resolver import (
    AmbiguousPrefix,
    SessionNotFound,
    resolve_session_id,
)


def _insert_file(conn: sqlite3.Connection, path: str, session_id: str) -> None:
    conn.execute(
        """INSERT INTO files (path, mtime_ns, size, session_id, kind,
                              agent_id, schema_version, parse_warnings, last_parsed_at)
           VALUES (?,0,0,?,'main',NULL,NULL,0,0)""",
        (path, session_id),
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "idx.sqlite"
    c = open_connection(db)
    ensure_schema(c)
    return c


def test_full_uuid_returned_unchanged(conn: sqlite3.Connection) -> None:
    sid = "abcdef12-3456-7890-abcd-ef1234567890"
    _insert_file(conn, "/tmp/foo/abcdef12-3456-7890-abcd-ef1234567890.jsonl", sid)
    assert resolve_session_id(sid, conn) == sid


def test_prefix_six_chars_unique_returns_full(conn: sqlite3.Connection) -> None:
    sid = "abcdef1234567890"
    _insert_file(conn, "/tmp/foo/abcdef1234567890.jsonl", sid)
    assert resolve_session_id("abcdef", conn) == sid


def test_prefix_under_six_chars_raises_value_error(conn: sqlite3.Connection) -> None:
    _insert_file(conn, "/tmp/foo/abcdef1234567890.jsonl", "abcdef1234567890")
    with pytest.raises(ValueError, match="≥6 characters"):
        resolve_session_id("abcde", conn)


def test_ambiguous_prefix_raises(conn: sqlite3.Connection) -> None:
    _insert_file(conn, "/tmp/a/abcdef111.jsonl", "abcdef111")
    _insert_file(conn, "/tmp/a/abcdef222.jsonl", "abcdef222")
    with pytest.raises(AmbiguousPrefix) as excinfo:
        resolve_session_id("abcdef", conn)
    assert excinfo.value.prefix == "abcdef"
    assert excinfo.value.matches == ["abcdef111", "abcdef222"]
    assert "abcdef111" in str(excinfo.value)
    assert "abcdef222" in str(excinfo.value)


def test_no_prefix_match_raises_session_not_found(conn: sqlite3.Connection) -> None:
    _insert_file(conn, "/tmp/a/abcdef111.jsonl", "abcdef111")
    with pytest.raises(SessionNotFound) as excinfo:
        resolve_session_id("zzzzzz", conn)
    assert excinfo.value.spec == "zzzzzz"
    assert "zzzzzz" in str(excinfo.value)


def test_absolute_jsonl_path_resolves_via_files(conn: sqlite3.Connection) -> None:
    sid = "abcdef1234567890"
    path = "/tmp/projects/abcdef1234567890.jsonl"
    _insert_file(conn, path, sid)
    assert resolve_session_id(path, conn) == sid


def test_absolute_jsonl_path_not_in_index_raises(conn: sqlite3.Connection) -> None:
    missing = "/tmp/not-there/session.jsonl"
    with pytest.raises(SessionNotFound) as excinfo:
        resolve_session_id(missing, conn)
    assert excinfo.value.spec == missing
