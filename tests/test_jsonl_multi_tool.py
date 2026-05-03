"""Multi-tool turn capture — every tool_use block lands in message_tool_uses.

Today the writer ``break``s after the first tool_use, so siblings are dropped
from indexed columns. Schema v3 adds message_tool_uses to capture all of them
without breaking ``messages.tool_name`` (load-bearing for tree.discover_spawn).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import ensure_schema, open_connection, reconcile_projects_dir

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict[str, Any]:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e))
            f.write("\n")


def _user(uuid: str, sid: str, ts: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": sid,
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "message": {"role": "user", "content": text},
        **extra,
    }


def _assistant_multi_tool(
    uuid: str, sid: str, ts: str, *, msg_id: str, req_id: str
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": sid,
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "requestId": req_id,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-sonnet-4-5-20250929",
            "content": [
                {"type": "text", "text": "ok"},
                {
                    "type": "tool_use",
                    "id": "tu_native",
                    "name": "Edit",
                    "input": {"file_path": "/x.py", "old": "a", "new": "b"},
                },
                {
                    "type": "tool_use",
                    "id": "tu_mcp1",
                    "name": "mcp__stratplaybook__query",
                    "input": {"query": "test"},
                },
                {
                    "type": "tool_use",
                    "id": "tu_mcp2",
                    "name": "mcp__strattrader-collector__get_bars",
                    "input": {"symbol": "AAPL"},
                },
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "service_tier": "standard",
            },
        },
    }


def test_multi_tool_turn_writes_all_tool_use_rows(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-multi"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "go", cwd="/home/test"),
            _assistant_multi_tool("u2", sid, "2026-04-22T10:00:05Z", msg_id="m1", req_id="r1"),
        ],
    )
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rows = conn.execute(
        """SELECT ordinal, tool_use_id, tool_name, mcp_server, args_size_bytes
           FROM message_tool_uses ORDER BY ordinal"""
    ).fetchall()

    # 3 tool_use blocks (text block does not produce a row); ordinals match
    # positions WITHIN the message.content array (text is at 0, tools at 1,2,3).
    assert len(rows) == 3
    assert rows[0] == (1, "tu_native", "Edit", None, rows[0][4])
    assert rows[1] == (
        2,
        "tu_mcp1",
        "mcp__stratplaybook__query",
        "stratplaybook",
        rows[1][4],
    )
    assert rows[2] == (
        3,
        "tu_mcp2",
        "mcp__strattrader-collector__get_bars",
        "strattrader-collector",
        rows[2][4],
    )
    # args_size_bytes is precise byte length of canonical JSON; non-zero for
    # all three, monotonically derivable but we only assert > 0 here.
    for r in rows:
        assert r[4] > 0


def test_multi_tool_messages_tool_name_unchanged(tmp_path: Path, pricing_data: dict) -> None:
    """Regression guard: messages.tool_name must still equal the FIRST tool_use's
    name. tree.discover_spawn relies on this."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-first-tool"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "go", cwd="/home/test"),
            _assistant_multi_tool("u2", sid, "2026-04-22T10:00:05Z", msg_id="m1", req_id="r1"),
        ],
    )
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute("SELECT tool_name, tool_use_id FROM messages WHERE uuid='u2'").fetchone()
    assert row[0] == "Edit"
    assert row[1] == "tu_native"


def test_service_tier_persisted_on_message(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-tier"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "go", cwd="/home/test"),
            _assistant_multi_tool("u2", sid, "2026-04-22T10:00:05Z", msg_id="m1", req_id="r1"),
        ],
    )
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute("SELECT service_tier FROM messages WHERE uuid='u2'").fetchone()
    assert row[0] == "standard"
