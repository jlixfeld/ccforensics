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


def test_schema_v3_creates_message_tool_uses_and_service_tier(tmp_path: Path) -> None:
    from ccforensics.index import CURRENT_SCHEMA_VERSION, ensure_schema, open_connection

    db = tmp_path / "v3.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)

    assert CURRENT_SCHEMA_VERSION >= 3

    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "service_tier" in cols

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "message_tool_uses" in tables

    mtu_cols = {row[1] for row in conn.execute("PRAGMA table_info(message_tool_uses)").fetchall()}
    assert mtu_cols == {
        "message_dedup_key",
        "ordinal",
        "tool_use_id",
        "tool_name",
        "mcp_server",
        "args_size_bytes",
    }


def test_schema_v4_creates_thrash_columns_and_session_signals(tmp_path: Path) -> None:
    db = tmp_path / "v4.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)

    assert CURRENT_SCHEMA_VERSION >= 4

    rollup_cols = {row[1] for row in conn.execute("PRAGMA table_info(session_rollups)").fetchall()}
    assert "thrash_score" in rollup_cols
    assert "thrash_score_version" in rollup_cols
    assert "escalation_event" in rollup_cols

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "session_signals" in tables

    sig_cols = {row[1] for row in conn.execute("PRAGMA table_info(session_signals)").fetchall()}
    assert sig_cols == {
        "session_id",
        "signal_type",
        "count",
        "evidence",
        "signal_version",
    }

    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='session_signals'"
        ).fetchall()
    }
    assert "idx_signals_session" in indexes


def test_schema_v4_cold_backfill_resets_file_mtime(tmp_path: Path) -> None:
    """v3 → v4 migration MUST reset files.mtime_ns to force re-reconcile so
    thrash signals + escalation events populate from existing files."""
    db = tmp_path / "v3.sqlite"
    conn = open_connection(db)
    # Apply migrations up to v3 by running ensure_schema after temporarily
    # capping at 3, then bump back and run the v3 → v4 step.
    import ccforensics.index as idx_mod

    real_version = idx_mod.CURRENT_SCHEMA_VERSION
    idx_mod.CURRENT_SCHEMA_VERSION = 3
    try:
        ensure_schema(conn)
    finally:
        idx_mod.CURRENT_SCHEMA_VERSION = real_version

    conn.execute(
        """INSERT INTO files (path, mtime_ns, size, session_id, kind, last_parsed_at)
           VALUES ('/x.jsonl', 999999, 100, 's', 'main', 0)"""
    )
    conn.commit()

    ensure_schema(conn)

    row = conn.execute("SELECT mtime_ns FROM files WHERE path='/x.jsonl'").fetchone()
    assert row[0] == 0, "v4 migration must reset mtime_ns to force cold backfill"


def test_schema_v3_cold_backfill_resets_file_mtime(tmp_path: Path) -> None:
    """v2 → v3 migration MUST reset files.mtime_ns to force re-reconcile so
    message_tool_uses and service_tier populate from existing files."""
    import ccforensics.index as idx_mod
    from ccforensics.index import ensure_schema, open_connection

    # Build a v2 db manually then migrate.
    db = tmp_path / "v2.sqlite"
    conn = open_connection(db)
    # Seed v0 → v2 by running existing migrations up to v2 only:
    conn.executescript(
        """
        CREATE TABLE files (
            path TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            agent_id TEXT,
            schema_version TEXT,
            parse_warnings INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
            dedup_key TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            type TEXT NOT NULL,
            ts INTEGER NOT NULL
        );
        INSERT INTO files (path, mtime_ns, size, session_id, kind)
        VALUES ('/x.jsonl', 999999, 100, 's', 'main');
        """
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()

    # Isolate v2 → v3 migration boundary: synthetic v2 schema lacks tables
    # touched by later migrations (e.g., session_rollups for v4). Cap to 3
    # so this test verifies only the v2 → v3 cold-backfill behavior.
    real_version = idx_mod.CURRENT_SCHEMA_VERSION
    idx_mod.CURRENT_SCHEMA_VERSION = 3
    try:
        ensure_schema(conn)
    finally:
        idx_mod.CURRENT_SCHEMA_VERSION = real_version

    row = conn.execute("SELECT mtime_ns FROM files WHERE path='/x.jsonl'").fetchone()
    assert row[0] == 0, "v3 migration must reset mtime_ns to force cold backfill"
