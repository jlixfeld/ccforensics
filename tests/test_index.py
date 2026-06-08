from __future__ import annotations

from pathlib import Path

from ccforensics.index import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    ensure_schema,
    open_connection,
)


def _apply_migrations_through(conn, target_version: int) -> None:
    """Bring a fresh DB up to exactly ``target_version`` (0-indexed migrations)."""
    for target in range(target_version):
        for ddl in MIGRATIONS[target]:
            conn.execute(ddl)
        conn.execute(f"PRAGMA user_version = {target + 1}")
    conn.commit()


def test_v6_migration_purges_phantom_workflow_sessions(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    # Build a pre-v6 (v5) database.
    _apply_migrations_through(conn, 5)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 5

    # Seed phantom rows exactly as the old misclassification wrote them.
    conn.execute(
        "INSERT INTO files (path, mtime_ns, size, session_id, kind, agent_id, "
        "schema_version, parse_warnings, last_parsed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "/p/-enc/SESS/subagents/workflows/wf_z/agent-dead.jsonl",
            123, 456, "agent-dead", "main", None, "5", 0, 0,
        ),
    )
    conn.execute(
        "INSERT INTO messages (dedup_key, file_path, session_id, role, type, ts) "
        "VALUES (?,?,?,?,?,?)",
        (
            "req:m1:r1",
            "/p/-enc/SESS/subagents/workflows/wf_z/agent-dead.jsonl",
            "agent-dead", "assistant", "assistant", 0,
        ),
    )
    conn.execute(
        "INSERT INTO session_rollups (session_id, bucket_kind, bucket_name, cost_usd, "
        "input_tokens, output_tokens, cache_create, cache_read) VALUES (?,?,?,?,?,?,?,?)",
        ("agent-dead", "main", "main", 1.0, 0, 0, 0, 0),
    )
    conn.execute(
        "INSERT INTO session_rollups (session_id, bucket_kind, bucket_name, cost_usd, "
        "input_tokens, output_tokens, cache_create, cache_read) VALUES (?,?,?,?,?,?,?,?)",
        ("journal", "main", "main", 0.0, 0, 0, 0, 0),
    )
    conn.commit()

    # Apply the v6 migration.
    ensure_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert CURRENT_SCHEMA_VERSION == 6
    assert conn.execute(
        "SELECT COUNT(*) FROM session_rollups WHERE session_id LIKE 'agent-%' OR session_id='journal'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE path LIKE '%/subagents/workflows/%'"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='agent-dead'").fetchone()[0] == 0
