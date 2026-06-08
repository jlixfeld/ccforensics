from __future__ import annotations

from pathlib import Path

from ccforensics.index import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    _classify_file,
    _parent_session_path,
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


def test_classify_workflow_agent_path() -> None:
    p = Path("/p/-enc/SESS-UUID/subagents/workflows/wf_2328ca35-f9d/agent-deadbeef.jsonl")
    assert _classify_file(p) == ("subagent", "deadbeef", "SESS-UUID")


def test_classify_direct_subagent_still_works() -> None:
    p = Path("/p/-enc/SESS-UUID/subagents/agent-abc.jsonl")
    assert _classify_file(p) == ("subagent", "abc", "SESS-UUID")


def test_classify_autocompact_still_works() -> None:
    p = Path("/p/-enc/SESS-UUID/subagents/agent-acompact-fff.jsonl")
    assert _classify_file(p) == ("auto-compact", "acompact-fff", "SESS-UUID")


def test_classify_main_still_works() -> None:
    p = Path("/p/-enc/SESS-UUID.jsonl")
    assert _classify_file(p) == ("main", None, "SESS-UUID")


def test_parent_session_path_workflow() -> None:
    child = Path("/p/-enc/SESS-UUID/subagents/workflows/wf_z/agent-dead.jsonl")
    assert _parent_session_path(child) == Path("/p/-enc/SESS-UUID.jsonl")


def test_parent_session_path_direct_subagent() -> None:
    child = Path("/p/-enc/SESS-UUID/subagents/agent-abc.jsonl")
    assert _parent_session_path(child) == Path("/p/-enc/SESS-UUID.jsonl")
