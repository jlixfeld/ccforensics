from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ccforensics.index import (
    count_messages_for_file,
    ensure_schema,
    open_connection,
    reconcile_file,
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


def test_reconcile_unchanged_file_is_noop(tmp_path: Path, pricing_data: dict) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    src = FIXTURES / "basic" / "s1.jsonl"

    reconcile_file(conn, src, pricing_data)
    conn.commit()
    first_last_parsed = conn.execute(
        "SELECT last_parsed_at FROM files WHERE path=?", (str(src),)
    ).fetchone()[0]

    time.sleep(1.1)
    reconcile_file(conn, src, pricing_data)
    conn.commit()
    second_last_parsed = conn.execute(
        "SELECT last_parsed_at FROM files WHERE path=?", (str(src),)
    ).fetchone()[0]

    assert first_last_parsed == second_last_parsed, "unchanged file should be skipped"


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
