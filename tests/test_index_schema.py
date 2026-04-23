from __future__ import annotations

from pathlib import Path

import pytest

from ccforensics.index import (
    CURRENT_SCHEMA_VERSION,
    ensure_schema,
    open_connection,
)


def test_fresh_db_applies_current_schema(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    conn.commit()

    cur = conn.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == CURRENT_SCHEMA_VERSION

    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for required in (
        "files",
        "messages",
        "subagent_spawns",
        "skill_activations",
        "plugins",
        "user_level_artifacts",
        "session_summaries",
        "session_rollups",
    ):
        assert required in existing, f"missing table {required}"


def test_reapplying_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    ensure_schema(conn)
    cur = conn.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == CURRENT_SCHEMA_VERSION


def test_downgrade_refuses_newer_schema(tmp_path: Path) -> None:
    """Opening a DB whose user_version is ahead of this binary must fail
    loudly — otherwise an older ccforensics could silently corrupt a
    newer index."""
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    conn2 = open_connection(db)
    with pytest.raises(RuntimeError, match="newer than this ccforensics"):
        ensure_schema(conn2)
