from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import (
    count_messages_for_file,
    ensure_schema,
    open_connection,
    reconcile_file,
    reconcile_projects_dir,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def test_first_reconcile_inserts_files_and_messages(tmp_path: Path, pricing_data: dict) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)

    src = FIXTURES / "basic" / "s1.jsonl"
    reconcile_file(conn, src, pricing_data)
    conn.commit()

    file_rows = conn.execute("SELECT path, session_id, kind FROM files").fetchall()
    assert len(file_rows) == 1
    assert file_rows[0][1] == "s1"
    assert file_rows[0][2] == "main"

    msg_count = count_messages_for_file(conn, src)
    assert msg_count > 0


def test_reconcile_unchanged_file_is_noop(
    tmp_path: Path, pricing_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged file short-circuits before parse_file. Uses a parse counter
    rather than wall-clock sleep — the short-circuit fact is what matters,
    not whether `last_parsed_at` seconds-resolution tick forward.
    """
    from ccforensics import index as index_mod

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    src = FIXTURES / "basic" / "s1.jsonl"

    parse_calls = {"n": 0}
    real_parse = index_mod.parse_file

    def _counting_parse(path: Path) -> Any:
        parse_calls["n"] += 1
        return real_parse(path)

    monkeypatch.setattr(index_mod, "parse_file", _counting_parse)

    reconcile_file(conn, src, pricing_data)
    conn.commit()
    assert parse_calls["n"] == 1

    reconcile_file(conn, src, pricing_data)
    conn.commit()
    # Second call should hit _row_is_unchanged and skip parse_file entirely.
    assert parse_calls["n"] == 1, "unchanged file should be skipped"


def test_subagents_dir_with_malformed_filename_warns(
    tmp_path: Path, pricing_data: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """A file under subagents/ with the wrong naming pattern is classified
    as subagent (with no agent_id) AND logs a warning. Never silently main."""
    import logging

    from ccforensics.index import _classify_file

    caplog.set_level(logging.WARNING, logger="ccforensics.index")

    proj = tmp_path / "-home-test"
    sess_dir = proj / "sess-x" / "subagents"
    sess_dir.mkdir(parents=True)
    bad = sess_dir / "agent-WITHOUT-hex-pattern.jsonl"
    bad.write_text("")

    kind, agent_id, session_id = _classify_file(bad)
    assert kind == "subagent"
    assert agent_id is None
    assert session_id == "sess-x"
    assert any("doesn't match agent-<hex>.jsonl" in rec.message for rec in caplog.records)


def test_reconcile_changed_file_replaces_messages(tmp_path: Path, pricing_data: dict) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)

    src = tmp_path / "session.jsonl"
    src.write_bytes((FIXTURES / "basic" / "s1.jsonl").read_bytes())

    reconcile_file(conn, src, pricing_data)
    conn.commit()
    initial_count = count_messages_for_file(conn, src)

    new_line = (
        '{"type":"assistant","uuid":"u99","sessionId":"s1",'
        '"timestamp":"2026-04-20T10:10:00Z","requestId":"req-new",'
        '"message":{"id":"msg-new","role":"assistant",'
        '"model":"claude-sonnet-4-5-20250929","content":[],'
        '"usage":{"input_tokens":1,"output_tokens":1}}}\n'
    )
    with src.open("a") as f:
        f.write(new_line)
    os.utime(src, (time.time() + 5, time.time() + 5))

    reconcile_file(conn, src, pricing_data)
    conn.commit()

    new_count = count_messages_for_file(conn, src)
    assert new_count == initial_count + 1


# ---------- subagent_spawns population (M5.4) ----------


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e))
            f.write("\n")


def test_subagent_file_writes_spawn_row(tmp_path: Path, pricing_data: dict) -> None:
    """Parent with Agent tool_use + subagent JSONL + meta.json → spawn row
    with resolved parent linkage."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test-proj"
    session_id = "sess-parent"

    parent_entries = [
        {
            "type": "user",
            "uuid": "p-u1",
            "sessionId": session_id,
            "timestamp": "2026-04-22T10:00:00Z",
            "isSidechain": False,
            "isMeta": False,
            "cwd": "/home/test/proj",
            "message": {"role": "user", "content": "spawn an explorer"},
        },
        {
            "type": "assistant",
            "uuid": "p-u2",
            "sessionId": session_id,
            "timestamp": "2026-04-22T10:00:10Z",
            "isSidechain": False,
            "isMeta": False,
            "requestId": "r1",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-sonnet-4-5-20250929",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu-agent-1",
                        "name": "Agent",
                        "input": {"subagent_type": "Explore"},
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]
    _write_jsonl(enc / f"{session_id}.jsonl", parent_entries)

    sub_dir = enc / session_id / "subagents"
    sub_dir.mkdir(parents=True)
    agent_id = "abc123def456"
    child_path = sub_dir / f"agent-{agent_id}.jsonl"
    child_entries = [
        {
            "type": "user",
            "uuid": "c-u1",
            "sessionId": session_id,
            "agentId": agent_id,
            "timestamp": "2026-04-22T10:00:15Z",
            "isSidechain": True,
            "isMeta": False,
            "message": {"role": "user", "content": "walk src"},
        },
        {
            "type": "assistant",
            "uuid": "c-u2",
            "sessionId": session_id,
            "agentId": agent_id,
            "timestamp": "2026-04-22T10:00:20Z",
            "isSidechain": True,
            "isMeta": False,
            "requestId": "r2",
            "message": {
                "id": "m2",
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 50, "output_tokens": 20},
            },
        },
    ]
    _write_jsonl(child_path, child_entries)
    (sub_dir / f"agent-{agent_id}.meta.json").write_text(
        '{"agentType":"Explore","description":"walk src tree"}'
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        """SELECT parent_session_id, child_agent_id, child_file_path,
                  subagent_type, description, model,
                  parent_message_dedup_key
             FROM subagent_spawns WHERE child_file_path=?""",
        (str(child_path),),
    ).fetchone()
    assert row is not None
    assert row[0] == session_id
    assert row[1] == agent_id
    assert row[2] == str(child_path)
    assert row[3] == "Explore"
    assert row[4] == "walk src tree"
    assert row[5] == "claude-opus-4-7"
    parent_key = row[6]
    assert parent_key is not None
    parent_msg = conn.execute(
        "SELECT uuid FROM messages WHERE dedup_key=?", (parent_key,)
    ).fetchone()
    assert parent_msg is not None
    assert parent_msg[0] == "p-u2"


def test_subagent_unresolvable_writes_null_parent(tmp_path: Path, pricing_data: dict) -> None:
    """No parent Agent/Task call before ts_spawned → parent_message_dedup_key=NULL."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    session_id = "sess-orphan"

    _write_jsonl(
        enc / f"{session_id}.jsonl",
        [
            {
                "type": "user",
                "uuid": "p-u1",
                "sessionId": session_id,
                "timestamp": "2026-04-22T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "cwd": "/home/test",
                "message": {"role": "user", "content": "hi"},
            }
        ],
    )
    sub_dir = enc / session_id / "subagents"
    sub_dir.mkdir(parents=True)
    agent_id = "deadbeef"
    child_path = sub_dir / f"agent-{agent_id}.jsonl"
    _write_jsonl(
        child_path,
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": session_id,
                "agentId": agent_id,
                "timestamp": "2026-04-22T10:00:05Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "what"},
            }
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT subagent_type, parent_message_dedup_key FROM subagent_spawns "
        "WHERE child_file_path=?",
        (str(child_path),),
    ).fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None


def test_subagent_missing_meta_uses_parent_tool_use_subtype(
    tmp_path: Path, pricing_data: dict
) -> None:
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    session_id = "sess-nometa"

    _write_jsonl(
        enc / f"{session_id}.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "p-u1",
                "sessionId": session_id,
                "timestamp": "2026-04-22T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "requestId": "r1",
                "cwd": "/home/test",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5-20250929",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-a",
                            "name": "Agent",
                            "input": {"subagent_type": "general-purpose"},
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        ],
    )
    sub_dir = enc / session_id / "subagents"
    sub_dir.mkdir(parents=True)
    child_path = sub_dir / "agent-abc.jsonl"
    _write_jsonl(
        child_path,
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": session_id,
                "agentId": "abc",
                "timestamp": "2026-04-22T10:00:05Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "task"},
            }
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT subagent_type FROM subagent_spawns WHERE child_file_path=?",
        (str(child_path),),
    ).fetchone()
    assert row[0] == "general-purpose"


def test_autocompact_file_classified_separately_no_spawn_row(
    tmp_path: Path, pricing_data: dict
) -> None:
    """agent-acompact-<hex>.jsonl → kind='auto-compact'; no spawn row."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    session_id = "sess-compact"

    _write_jsonl(
        enc / f"{session_id}.jsonl",
        [
            {
                "type": "user",
                "uuid": "p-u1",
                "sessionId": session_id,
                "timestamp": "2026-04-22T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "cwd": "/home/test",
                "message": {"role": "user", "content": "do stuff"},
            }
        ],
    )
    sub_dir = enc / session_id / "subagents"
    sub_dir.mkdir(parents=True)
    compact_path = sub_dir / "agent-acompact-deadbeef1234.jsonl"
    _write_jsonl(
        compact_path,
        [
            {
                "type": "user",
                "uuid": "k-u1",
                "sessionId": session_id,
                "agentId": "acompact-deadbeef1234",
                "timestamp": "2026-04-22T10:05:00Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "summarize"},
            }
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    kind_row = conn.execute(
        "SELECT kind, agent_id FROM files WHERE path=?", (str(compact_path),)
    ).fetchone()
    assert kind_row[0] == "auto-compact"
    assert kind_row[1] == "acompact-deadbeef1234"

    spawn_count = conn.execute(
        "SELECT COUNT(*) FROM subagent_spawns WHERE child_file_path=?",
        (str(compact_path),),
    ).fetchone()[0]
    assert spawn_count == 0


def test_subagent_reconcile_is_idempotent(tmp_path: Path, pricing_data: dict) -> None:
    """Second reconcile on unchanged corpus → spawn row unchanged."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    session_id = "sess-idem"

    _write_jsonl(
        enc / f"{session_id}.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "p-u1",
                "sessionId": session_id,
                "timestamp": "2026-04-22T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "requestId": "r1",
                "cwd": "/home/test",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5-20250929",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu",
                            "name": "Agent",
                            "input": {"subagent_type": "Explore"},
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        ],
    )
    sub_dir = enc / session_id / "subagents"
    sub_dir.mkdir(parents=True)
    child_path = sub_dir / "agent-abc.jsonl"
    _write_jsonl(
        child_path,
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": session_id,
                "agentId": "abc",
                "timestamp": "2026-04-22T10:00:05Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "x"},
            }
        ],
    )
    (sub_dir / "agent-abc.meta.json").write_text('{"agentType":"Explore","description":"y"}')

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)
    first = conn.execute(
        "SELECT * FROM subagent_spawns WHERE child_file_path=?", (str(child_path),)
    ).fetchall()
    assert len(first) == 1

    reconcile_projects_dir(conn, proj, pricing_data)
    second = conn.execute(
        "SELECT * FROM subagent_spawns WHERE child_file_path=?", (str(child_path),)
    ).fetchall()
    assert first == second


def test_subagent_missing_parent_file_writes_null_parent(
    tmp_path: Path,
    pricing_data: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Parent main JSONL missing → spawn row with null parent + warning."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    session_id = "sess-gone"

    sub_dir = enc / session_id / "subagents"
    sub_dir.mkdir(parents=True)
    child_path = sub_dir / "agent-abc.jsonl"
    _write_jsonl(
        child_path,
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": session_id,
                "agentId": "abc",
                "timestamp": "2026-04-22T10:00:05Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "x"},
            }
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    caplog.set_level("WARNING", logger="ccforensics.index")
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT parent_message_dedup_key FROM subagent_spawns WHERE child_file_path=?",
        (str(child_path),),
    ).fetchone()
    assert row is not None
    assert row[0] is None
    # Warning mentions the parent-file path.
    parent_hint = f"{session_id}.jsonl"
    assert any(parent_hint in r.getMessage() for r in caplog.records)


def test_unresolved_pricing_model_emits_warning(
    tmp_path: Path, pricing_data: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """An assistant entry whose model isn't in the pricing table is recorded
    with NULL cost AND surfaces a warning — otherwise under-reporting totals
    silently would look indistinguishable from zero-cost sessions."""
    import logging as _lg

    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    enc.mkdir(parents=True)
    _write_jsonl(
        enc / "s.jsonl",
        [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": "s",
                "timestamp": "2026-04-22T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "message": {"role": "user", "content": "hi"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "sessionId": "s",
                "timestamp": "2026-04-22T10:00:05Z",
                "isSidechain": False,
                "isMeta": False,
                "requestId": "r1",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "model": "unreleased-model-xyz-20300101",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ],
    )

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    caplog.set_level(_lg.WARNING, logger="ccforensics.index")
    reconcile_projects_dir(conn, proj, pricing_data)

    assert any(
        "pricing unresolved" in r.getMessage() and "unreleased-model-xyz-20300101" in r.getMessage()
        for r in caplog.records
    )
    # The message row survives with NULL cost.
    row = conn.execute(
        "SELECT cost_usd FROM messages WHERE session_id='s' AND type='assistant'"
    ).fetchone()
    assert row is not None
    assert row[0] is None


def test_per_session_recompute_propagates_programmer_errors(
    tmp_path: Path,
    pricing_data: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KeyError/AttributeError inside per-session recompute is a real bug
    and MUST propagate, not get silently swallowed by a broad except.

    The catch in reconcile_projects_dir is narrowed to (OSError, sqlite3.Error)
    so TOCTOU on session files still gets isolated, but genuine programmer
    errors surface instead of corrupting attribution silently.
    """
    from ccforensics import index as index_mod

    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    enc.mkdir(parents=True)
    _write_jsonl(
        enc / "sess.jsonl",
        [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": "sess",
                "timestamp": "2026-04-22T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "message": {"role": "user", "content": "hi"},
            }
        ],
    )

    def _boom(*_a: object, **_k: object) -> None:
        raise KeyError("simulated programmer bug")

    monkeypatch.setattr(index_mod, "recompute_session_summary", _boom)

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    with pytest.raises(KeyError, match="simulated programmer bug"):
        reconcile_projects_dir(conn, proj, pricing_data)


def test_per_session_recompute_isolates_toctou_oserror(
    tmp_path: Path,
    pricing_data: dict,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A FileNotFoundError (TOCTOU: file deleted between walk + recompute)
    is the documented reason for the catch — it must stay isolated."""
    import logging as _lg

    from ccforensics import index as index_mod

    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    enc.mkdir(parents=True)
    _write_jsonl(
        enc / "sess.jsonl",
        [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": "sess",
                "timestamp": "2026-04-22T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "message": {"role": "user", "content": "hi"},
            }
        ],
    )

    def _raise_fnf(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("simulated rotate")

    monkeypatch.setattr(index_mod, "recompute_session_summary", _raise_fnf)

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    caplog.set_level(_lg.WARNING, logger="ccforensics.index")
    # Should NOT raise — isolated by the narrow catch.
    reconcile_projects_dir(conn, proj, pricing_data)
    # The decoupled recompute pipeline logs the specific step that failed;
    # this test only proves the FileNotFoundError stays isolated to the
    # summary step rather than aborting the entire session's processing.
    assert any(
        "recompute_session_summary failed" in r.getMessage() for r in caplog.records
    )


def test_schema_version_selection_is_deterministic(tmp_path: Path, pricing_data: dict) -> None:
    """A file containing multiple schema versions must store the minimum.

    Set iteration order is non-deterministic across runs, so picking via
    ``next(iter(set))`` can stably-but-surprisingly flip. We pin to the
    numerically lowest version via ``min()``.
    """
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "mvs"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            {
                "type": "user",
                "uuid": f"u-{sid}-1",
                "sessionId": sid,
                "timestamp": "2026-04-22T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "version": "2.0.70",
                "message": {"role": "user", "content": "hi"},
            },
            {
                "type": "user",
                "uuid": f"u-{sid}-2",
                "sessionId": sid,
                "timestamp": "2026-04-22T10:00:01Z",
                "isSidechain": False,
                "isMeta": False,
                "version": "1.0.65",
                "message": {"role": "user", "content": "again"},
            },
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute("SELECT schema_version FROM files WHERE session_id=?", (sid,)).fetchone()
    assert row is not None
    assert row[0] == "1.0.65"


def test_main_file_modified_after_subagent_indexed_does_not_raise(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Regression: if a main session file is re-processed after its subagent has
    been indexed (so ``subagent_spawns.parent_message_dedup_key`` already
    references a message in the main file), purging the main file's messages
    used to fail ``FOREIGN KEY constraint failed``. Post-fix, the FK cascades
    to NULL on delete and a re-resolve pass restores the linkage.
    """
    proj = tmp_path / "projects"
    enc = proj / "-home-test-proj"
    session_id = "sess-edit"

    parent_entries = [
        {
            "type": "user",
            "uuid": "p-u1",
            "sessionId": session_id,
            "timestamp": "2026-04-22T10:00:00Z",
            "isSidechain": False,
            "isMeta": False,
            "cwd": "/home/test/proj",
            "message": {"role": "user", "content": "spawn an explorer"},
        },
        {
            "type": "assistant",
            "uuid": "p-u2",
            "sessionId": session_id,
            "timestamp": "2026-04-22T10:00:10Z",
            "isSidechain": False,
            "isMeta": False,
            "requestId": "r1",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-sonnet-4-5-20250929",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu-agent-1",
                        "name": "Agent",
                        "input": {"subagent_type": "Explore"},
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]
    main_path = enc / f"{session_id}.jsonl"
    _write_jsonl(main_path, parent_entries)

    sub_dir = enc / session_id / "subagents"
    sub_dir.mkdir(parents=True)
    agent_id = "abc123def456"
    child_path = sub_dir / f"agent-{agent_id}.jsonl"
    _write_jsonl(
        child_path,
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": session_id,
                "agentId": agent_id,
                "timestamp": "2026-04-22T10:00:15Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "walk src"},
            },
            {
                "type": "assistant",
                "uuid": "c-u2",
                "sessionId": session_id,
                "agentId": agent_id,
                "timestamp": "2026-04-22T10:00:20Z",
                "isSidechain": True,
                "isMeta": False,
                "requestId": "r2",
                "message": {
                    "id": "m2",
                    "role": "assistant",
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input_tokens": 50, "output_tokens": 20},
                },
            },
        ],
    )
    (sub_dir / f"agent-{agent_id}.meta.json").write_text(
        '{"agentType":"Explore","description":"walk src tree"}'
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    parent_key_before = conn.execute(
        "SELECT parent_message_dedup_key FROM subagent_spawns WHERE child_file_path=?",
        (str(child_path),),
    ).fetchone()[0]
    assert parent_key_before is not None, "first reconcile should resolve spawn parent"

    # Append a new user entry to the main file so mtime + size both change.
    # The pre-existing ``p-u2`` assistant message (and its dedup_key) is
    # preserved across the re-parse, so spawn linkage must be restorable.
    appended = {
        "type": "user",
        "uuid": "p-u3",
        "sessionId": session_id,
        "timestamp": "2026-04-22T10:05:00Z",
        "isSidechain": False,
        "isMeta": False,
        "message": {"role": "user", "content": "follow up"},
    }
    with main_path.open("a") as f:
        f.write(json.dumps(appended))
        f.write("\n")
    # Force a mtime bump even if the clock didn't tick a full second.
    future = time.time() + 2
    os.utime(main_path, (future, future))

    # Must not raise ``sqlite3.IntegrityError: FOREIGN KEY constraint failed``.
    reconcile_projects_dir(conn, proj, pricing_data)

    parent_key_after = conn.execute(
        "SELECT parent_message_dedup_key FROM subagent_spawns WHERE child_file_path=?",
        (str(child_path),),
    ).fetchone()[0]
    assert parent_key_after is not None, (
        "spawn parent linkage should be re-resolved after main file re-ingest, not left NULL"
    )
    assert parent_key_after == parent_key_before, (
        "dedup_key is stable across re-ingest of the same parent message, so "
        "the re-resolved linkage should match"
    )
