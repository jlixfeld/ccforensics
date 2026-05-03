"""Per-tool / per-MCP report — isolated cost is exact, shared exposure is
an upper bound, and the two columns must not be conflated."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import ensure_schema, open_connection, reconcile_projects_dir
from ccforensics.report.tools import ToolRow, query_tool_costs

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


def _user(uuid: str, sid: str, ts: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": sid,
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "message": {"role": "user", "content": "go"},
        **extra,
    }


def _assistant(
    uuid: str,
    sid: str,
    ts: str,
    *,
    msg_id: str,
    req_id: str,
    tools: list[tuple[str, str]],  # (tool_use_id, tool_name)
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": "ok"}]
    for tu_id, tu_name in tools:
        content.append(
            {"type": "tool_use", "id": tu_id, "name": tu_name, "input": {"x": 1}}
        )
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
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "service_tier": "standard",
            },
        },
    }


def _build_corpus(tmp_path: Path, pricing_data: dict) -> tuple[Any, list[str]]:
    """3 single-tool turns + 1 multi-tool turn. Returns (conn, [session_ids])."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-tools"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", cwd="/home/test"),
            _assistant(  # single Edit
                "u2", sid, "2026-04-22T10:00:01Z",
                msg_id="m1", req_id="r1",
                tools=[("tu1", "Edit")],
            ),
            _assistant(  # single Read
                "u3", sid, "2026-04-22T10:00:02Z",
                msg_id="m2", req_id="r2",
                tools=[("tu2", "Read")],
            ),
            _assistant(  # single MCP
                "u4", sid, "2026-04-22T10:00:03Z",
                msg_id="m3", req_id="r3",
                tools=[("tu3", "mcp__stratplaybook__query")],
            ),
            _assistant(  # multi-tool: Edit + MCP
                "u5", sid, "2026-04-22T10:00:04Z",
                msg_id="m4", req_id="r4",
                tools=[
                    ("tu4", "Edit"),
                    ("tu5", "mcp__stratplaybook__build"),
                ],
            ),
        ],
    )
    db = tmp_path / "i.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)
    return conn, [sid]


def test_query_tool_costs_isolated_and_shared(
    tmp_path: Path, pricing_data: dict
) -> None:
    conn, session_ids = _build_corpus(tmp_path, pricing_data)
    rows = query_tool_costs(conn, session_ids=session_ids, detail=False, top=50, sort="isolated_cost")
    by_key = {r.group_key: r for r in rows}

    # Edit: 1 isolated turn (m1), 1 shared turn (m4)
    assert by_key["Edit"].isolated_turns == 1
    assert by_key["Edit"].shared_turns == 1
    assert by_key["Edit"].invocations == 2

    # Read: 1 isolated, 0 shared
    assert by_key["Read"].isolated_turns == 1
    assert by_key["Read"].shared_turns == 0

    # mcp__stratplaybook (server roll-up): 1 isolated (m3 query), 1 shared (m4 build)
    assert by_key["stratplaybook"].group_kind == "mcp_server"
    assert by_key["stratplaybook"].isolated_turns == 1
    assert by_key["stratplaybook"].shared_turns == 1
    assert by_key["stratplaybook"].invocations == 2


def test_isolated_cost_sum_equals_single_tool_turn_cost_sum(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Spec invariant: sum(isolated_cost across all tools) == sum of cost
    of single-tool turns in messages. Exact equality."""
    conn, session_ids = _build_corpus(tmp_path, pricing_data)
    rows = query_tool_costs(conn, session_ids=session_ids, detail=False, top=50, sort="isolated_cost")

    isolated_total = sum(r.isolated_cost_usd for r in rows)

    # Reference: cost of messages whose dedup_key has exactly 1 row in
    # message_tool_uses (single-tool turns).
    expected = conn.execute(
        """SELECT COALESCE(SUM(m.cost_usd), 0)
           FROM messages m
           JOIN (
             SELECT message_dedup_key, COUNT(*) AS n
             FROM message_tool_uses GROUP BY message_dedup_key
           ) c ON c.message_dedup_key = m.dedup_key
           WHERE c.n = 1 AND m.session_id IN (?)""",
        (session_ids[0],),
    ).fetchone()[0]

    assert isolated_total == pytest.approx(expected, abs=1e-9)


def test_query_tool_costs_top_clamps(tmp_path: Path, pricing_data: dict) -> None:
    conn, session_ids = _build_corpus(tmp_path, pricing_data)
    rows = query_tool_costs(conn, session_ids=session_ids, detail=False, top=2, sort="isolated_cost")
    assert len(rows) <= 2


def test_query_tool_costs_sort_by_invocations(
    tmp_path: Path, pricing_data: dict
) -> None:
    conn, session_ids = _build_corpus(tmp_path, pricing_data)
    rows = query_tool_costs(conn, session_ids=session_ids, detail=False, top=50, sort="invocations")
    invocations = [r.invocations for r in rows]
    assert invocations == sorted(invocations, reverse=True)
