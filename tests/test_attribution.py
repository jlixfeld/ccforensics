from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ccforensics.attribution import (
    backfill_spawn_totals,
    recompute_session_rollups,
    verify_invariant,
)
from ccforensics.index import (
    ensure_schema,
    open_connection,
    reconcile_projects_dir,
)

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


def _assistant(
    uuid: str,
    sid: str,
    ts: str,
    *,
    msg_id: str,
    req_id: str,
    model: str = "claude-sonnet-4-5-20250929",
    content: list[dict[str, Any]] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read: int = 0,
    cache_create: int = 0,
    **extra: Any,
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
            "model": model,
            "content": content or [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
            },
        },
        **extra,
    }


# ---------- basic bucket classification ----------


def test_main_only_session_single_bucket(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-main"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant("u2", sid, "2026-04-22T10:00:05Z", msg_id="m1", req_id="r1"),
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rollups = conn.execute(
        "SELECT bucket_kind, bucket_name, cost_usd FROM session_rollups WHERE session_id=?",
        (sid,),
    ).fetchall()
    assert len(rollups) == 1
    assert rollups[0][0] == "main"
    assert rollups[0][1] == "main"


def test_main_plus_subagent_two_buckets(tmp_path: Path, pricing_data: dict) -> None:
    """Main session + resolved subagent spawn → two rollup buckets."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-1"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "go", cwd="/home/test"),
            _assistant(
                "u2",
                sid,
                "2026-04-22T10:00:10Z",
                msg_id="m1",
                req_id="r1",
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Agent",
                        "input": {"subagent_type": "Explore"},
                    }
                ],
            ),
        ],
    )
    sub_dir = enc / sid / "subagents"
    sub_dir.mkdir(parents=True)
    child_path = sub_dir / "agent-abc.jsonl"
    _write_jsonl(
        child_path,
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": sid,
                "agentId": "abc",
                "timestamp": "2026-04-22T10:00:15Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "work"},
            },
            _assistant(
                "c-u2",
                sid,
                "2026-04-22T10:00:20Z",
                msg_id="m2",
                req_id="r2",
                model="claude-opus-4-7",
                agentId="abc",
                isSidechain=True,
            ),
        ],
    )
    (sub_dir / "agent-abc.meta.json").write_text('{"agentType":"Explore","description":"walk src"}')

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rollups = {
        (row[0], row[1]): row[2]
        for row in conn.execute(
            "SELECT bucket_kind, bucket_name, cost_usd FROM session_rollups WHERE session_id=?",
            (sid,),
        ).fetchall()
    }
    assert ("main", "main") in rollups
    assert ("subagent", "Explore") in rollups


def test_autocompact_own_bucket(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-c"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [_user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test")],
    )
    sub_dir = enc / sid / "subagents"
    sub_dir.mkdir(parents=True)
    _write_jsonl(
        sub_dir / "agent-acompact-deadbeef.jsonl",
        [
            _assistant(
                "k-u1",
                sid,
                "2026-04-22T10:05:00Z",
                msg_id="km1",
                req_id="kr1",
                agentId="acompact-deadbeef",
                isSidechain=True,
            ),
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    buckets = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT bucket_kind FROM session_rollups WHERE session_id=?",
            (sid,),
        ).fetchall()
    }
    assert "auto-compact" in buckets


def test_unresolved_spawn_goes_to_unattributed(tmp_path: Path, pricing_data: dict) -> None:
    """Subagent with no parent Agent/Task call before ts_spawned →
    unattributed bucket (spec §4.2)."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-u"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [_user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test")],
    )
    sub_dir = enc / sid / "subagents"
    sub_dir.mkdir(parents=True)
    _write_jsonl(
        sub_dir / "agent-abc.jsonl",
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": sid,
                "agentId": "abc",
                "timestamp": "2026-04-22T10:00:05Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "x"},
            },
            _assistant(
                "c-u2",
                sid,
                "2026-04-22T10:00:10Z",
                msg_id="m2",
                req_id="r2",
                agentId="abc",
                isSidechain=True,
            ),
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    buckets = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT bucket_kind FROM session_rollups WHERE session_id=?",
            (sid,),
        ).fetchall()
    }
    assert "unattributed" in buckets


# ---------- invariant ----------


def test_invariant_sum_buckets_equals_session_total(tmp_path: Path, pricing_data: dict) -> None:
    """Hard invariant (spec §4.2): sum(buckets) == session_total."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-inv"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "go", cwd="/home/test"),
            _assistant(
                "u2",
                sid,
                "2026-04-22T10:00:10Z",
                msg_id="m1",
                req_id="r1",
                input_tokens=1000,
                output_tokens=500,
                cache_read=10000,
                cache_create=100,
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Agent",
                        "input": {"subagent_type": "Explore"},
                    }
                ],
            ),
        ],
    )
    sub_dir = enc / sid / "subagents"
    sub_dir.mkdir(parents=True)
    _write_jsonl(
        sub_dir / "agent-abc.jsonl",
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": sid,
                "agentId": "abc",
                "timestamp": "2026-04-22T10:00:15Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "x"},
            },
            _assistant(
                "c-u2",
                sid,
                "2026-04-22T10:00:20Z",
                msg_id="m2",
                req_id="r2",
                model="claude-opus-4-7",
                input_tokens=2000,
                output_tokens=800,
                cache_read=50000,
                agentId="abc",
                isSidechain=True,
            ),
        ],
    )
    (sub_dir / "agent-abc.meta.json").write_text('{"agentType":"Explore","description":"x"}')

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    ok, session_total, rollup_total = verify_invariant(conn, sid)
    assert ok, f"session={session_total} rollup={rollup_total}"
    assert session_total > 0


def test_invariant_holds_when_pricing_unresolved(tmp_path: Path, pricing_data: dict) -> None:
    """A session with some messages having NULL cost_usd (unresolved pricing)
    must still satisfy the invariant — session_total and rollup_total both
    COALESCE NULL→0, so the equality holds on the resolvable slice."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-partial"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "go", cwd="/home/test"),
            _assistant(
                "u2",
                sid,
                "2026-04-22T10:00:10Z",
                msg_id="m1",
                req_id="r1",
                model="claude-sonnet-4-5-20250929",
                input_tokens=100,
                output_tokens=50,
            ),
            _assistant(
                "u3",
                sid,
                "2026-04-22T10:00:20Z",
                msg_id="m2",
                req_id="r2",
                model="unreleased-model-xyz-20300101",
                input_tokens=200,
                output_tokens=100,
            ),
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    ok, session_total, rollup_total = verify_invariant(conn, sid)
    assert ok, f"session={session_total} rollup={rollup_total}"
    # session_total reflects only the resolvable entry.
    assert session_total > 0
    # The unresolvable message is present but its cost_usd is NULL.
    nulls = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=? AND cost_usd IS NULL", (sid,)
    ).fetchone()[0]
    assert nulls == 1


# ---------- spawn totals backfill ----------


def test_spawn_totals_backfilled(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-bf"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _assistant(
                "u1",
                sid,
                "2026-04-22T10:00:00Z",
                msg_id="m1",
                req_id="r1",
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Agent",
                        "input": {"subagent_type": "Explore"},
                    }
                ],
                cwd="/home/test",
            ),
        ],
    )
    sub_dir = enc / sid / "subagents"
    sub_dir.mkdir(parents=True)
    child_path = sub_dir / "agent-abc.jsonl"
    _write_jsonl(
        child_path,
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": sid,
                "agentId": "abc",
                "timestamp": "2026-04-22T10:00:05Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "x"},
            },
            _assistant(
                "c-u2",
                sid,
                "2026-04-22T10:00:10Z",
                msg_id="m2",
                req_id="r2",
                input_tokens=500,
                output_tokens=300,
                agentId="abc",
                isSidechain=True,
            ),
        ],
    )
    (sub_dir / "agent-abc.meta.json").write_text('{"agentType":"Explore","description":"y"}')

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        """SELECT total_cost_usd, total_input, total_output, ts_returned
             FROM subagent_spawns WHERE child_file_path=?""",
        (str(child_path),),
    ).fetchone()
    assert row is not None
    total_cost, total_in, total_out, ts_returned = row
    assert total_in == 500
    assert total_out == 300
    assert total_cost is not None and total_cost > 0
    assert ts_returned is not None  # max(ts) of child messages


# ---------- idempotency ----------


def test_rollups_idempotent_on_reconcile(tmp_path: Path, pricing_data: dict) -> None:
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-idem"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test"),
            _assistant("u2", sid, "2026-04-22T10:00:05Z", msg_id="m1", req_id="r1"),
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)
    first = conn.execute(
        "SELECT * FROM session_rollups WHERE session_id=? ORDER BY bucket_kind",
        (sid,),
    ).fetchall()

    reconcile_projects_dir(conn, proj, pricing_data)
    second = conn.execute(
        "SELECT * FROM session_rollups WHERE session_id=? ORDER BY bucket_kind",
        (sid,),
    ).fetchall()
    assert first == second


def test_buckets_have_exact_per_bucket_token_counts(tmp_path: Path, pricing_data: dict) -> None:
    """Pin exact per-bucket numerics — not just invariant, but that the
    right tokens land in the right bucket."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-exact"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            _assistant(
                "u1",
                sid,
                "2026-04-22T10:00:00Z",
                msg_id="m1",
                req_id="r1",
                input_tokens=100,
                output_tokens=50,
                cache_read=1000,
                cache_create=10,
                content=[
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "Agent",
                        "input": {"subagent_type": "Explore"},
                    }
                ],
                cwd="/home/test",
            ),
        ],
    )
    sub_dir = enc / sid / "subagents"
    sub_dir.mkdir(parents=True)
    _write_jsonl(
        sub_dir / "agent-abc.jsonl",
        [
            {
                "type": "user",
                "uuid": "c-u1",
                "sessionId": sid,
                "agentId": "abc",
                "timestamp": "2026-04-22T10:00:05Z",
                "isSidechain": True,
                "isMeta": False,
                "message": {"role": "user", "content": "x"},
            },
            _assistant(
                "c-u2",
                sid,
                "2026-04-22T10:00:10Z",
                msg_id="m2",
                req_id="r2",
                input_tokens=2000,
                output_tokens=800,
                cache_read=5000,
                agentId="abc",
                isSidechain=True,
            ),
        ],
    )
    (sub_dir / "agent-abc.meta.json").write_text('{"agentType":"Explore","description":"x"}')
    _write_jsonl(
        sub_dir / "agent-acompact-abcdef.jsonl",
        [
            _assistant(
                "k-u1",
                sid,
                "2026-04-22T10:05:00Z",
                msg_id="km1",
                req_id="kr1",
                input_tokens=300,
                output_tokens=200,
                agentId="acompact-abcdef",
                isSidechain=True,
            ),
        ],
    )

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    by_bucket = {
        (row[0], row[1]): {"input": row[2], "output": row[3], "cache_read": row[4]}
        for row in conn.execute(
            """SELECT bucket_kind, bucket_name, input_tokens, output_tokens,
                      cache_read FROM session_rollups WHERE session_id=?""",
            (sid,),
        ).fetchall()
    }

    # Main bucket: only the one assistant message in the main file.
    assert by_bucket[("main", "main")]["input"] == 100
    assert by_bucket[("main", "main")]["output"] == 50
    assert by_bucket[("main", "main")]["cache_read"] == 1000

    # Explore bucket: the subagent's own messages only.
    assert by_bucket[("subagent", "Explore")]["input"] == 2000
    assert by_bucket[("subagent", "Explore")]["output"] == 800
    assert by_bucket[("subagent", "Explore")]["cache_read"] == 5000

    # Auto-compact bucket: just the compaction worker.
    assert by_bucket[("auto-compact", "auto-compact")]["input"] == 300
    assert by_bucket[("auto-compact", "auto-compact")]["output"] == 200


def test_rollup_helper_can_be_called_standalone(tmp_path: Path, pricing_data: dict) -> None:
    """recompute_session_rollups + backfill_spawn_totals are safe to call
    repeatedly outside the reconcile loop (e.g., in report code that
    wants fresh totals)."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-x"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [_user("u1", sid, "2026-04-22T10:00:00Z", "hi", cwd="/home/test")],
    )
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    recompute_session_rollups(conn, sid)
    backfill_spawn_totals(conn, sid)
    conn.commit()

    rows = conn.execute(
        "SELECT COUNT(*) FROM session_rollups WHERE session_id=?", (sid,)
    ).fetchone()
    assert rows[0] >= 1
