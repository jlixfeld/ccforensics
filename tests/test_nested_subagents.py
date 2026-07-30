"""Depth-3+ (nested) subagent attribution.

A subagent that spawns its own subagent writes the child *flat* into
``<enc>/<sess>/subagents/`` — the same directory as a main-loop spawn — so the
path carries no evidence of the nesting. Only ``meta.json``'s ``parentAgentId``
does. These tests pin that the spawning Agent/Task call is located in the
parent *subagent's* transcript rather than the main session file, which is what
keeps a nested child's cost off an unrelated main-loop Agent call.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from ccforensics.index import (
    _spawn_parent_path,
    ensure_schema,
    open_connection,
    reconcile_projects_dir,
)
from ccforensics.models import SpawnMeta

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))


def _agent_call(
    *,
    uuid: str,
    session_id: str,
    ts: str,
    tool_use_id: str,
    subagent_type: str,
    msg_id: str,
    request_id: str,
    agent_id: str | None = None,
) -> dict:
    """An assistant turn whose single content block is an Agent tool_use."""
    entry = {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": ts,
        "isSidechain": agent_id is not None,
        "isMeta": False,
        "requestId": request_id,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Agent",
                    "input": {"subagent_type": subagent_type},
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }
    if agent_id is not None:
        entry["agentId"] = agent_id
    return entry


def _text_turn(
    *,
    uuid: str,
    session_id: str,
    ts: str,
    msg_id: str,
    request_id: str,
    agent_id: str | None = None,
    output_tokens: int = 20,
) -> dict:
    entry = {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": ts,
        "isSidechain": agent_id is not None,
        "isMeta": False,
        "requestId": request_id,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 50, "output_tokens": output_tokens},
        },
    }
    if agent_id is not None:
        entry["agentId"] = agent_id
    return entry


SESSION = "sess-nested"


def _build_nested_corpus(
    proj: Path,
    *,
    outer_agent_id: str = "bbbb1111",
    inner_agent_id: str = "cccc2222",
) -> dict[str, Path]:
    """main → Explore(outer) → Plan(inner).

    The main file and the outer subagent's file each contain exactly one Agent
    tool_use, so a spawn that resolves to the wrong transcript is unambiguous.
    """
    enc = proj / "-home-test-proj"
    main_path = enc / f"{SESSION}.jsonl"
    _write_jsonl(
        main_path,
        [
            {
                "type": "user",
                "uuid": "m-u1",
                "sessionId": SESSION,
                "timestamp": "2026-07-01T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "cwd": "/home/test/proj",
                "message": {"role": "user", "content": "explore then plan"},
            },
            _agent_call(
                uuid="m-agent",
                session_id=SESSION,
                ts="2026-07-01T10:00:10Z",
                tool_use_id="tu-main-explore",
                subagent_type="Explore",
                msg_id="msg-main",
                request_id="req-main",
            ),
        ],
    )

    sub_dir = enc / SESSION / "subagents"
    outer_path = sub_dir / f"agent-{outer_agent_id}.jsonl"
    _write_jsonl(
        outer_path,
        [
            _agent_call(
                uuid="o-agent",
                session_id=SESSION,
                ts="2026-07-01T10:00:30Z",
                tool_use_id="tu-outer-plan",
                subagent_type="Plan",
                msg_id="msg-outer",
                request_id="req-outer",
                agent_id=outer_agent_id,
            ),
            _text_turn(
                uuid="o-text",
                session_id=SESSION,
                ts="2026-07-01T10:01:30Z",
                msg_id="msg-outer-2",
                request_id="req-outer-2",
                agent_id=outer_agent_id,
            ),
        ],
    )
    (sub_dir / f"agent-{outer_agent_id}.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "explore the tree"})
    )

    inner_path = sub_dir / f"agent-{inner_agent_id}.jsonl"
    _write_jsonl(
        inner_path,
        [
            _text_turn(
                uuid="i-text",
                session_id=SESSION,
                ts="2026-07-01T10:01:00Z",
                msg_id="msg-inner",
                request_id="req-inner",
                agent_id=inner_agent_id,
                output_tokens=99,
            ),
        ],
    )
    # The load-bearing field: names the outer agent as the spawner.
    (sub_dir / f"agent-{inner_agent_id}.meta.json").write_text(
        json.dumps(
            {
                "agentType": "Plan",
                "description": "plan the work",
                "parentAgentId": outer_agent_id,
            }
        )
    )
    return {"main": main_path, "outer": outer_path, "inner": inner_path}


def _reconcile(tmp_path: Path, proj: Path, pricing_data: dict):
    conn = open_connection(tmp_path / "index.sqlite")
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)
    return conn


def test_nested_spawn_binds_to_parent_subagents_tool_use(
    tmp_path: Path, pricing_data: dict
) -> None:
    """The nested child must resolve to the Agent call inside the OUTER
    subagent's transcript, not the main file's Agent call. Binding to the main
    file is the pre-v7 bug: it silently charged the nested agent's work to a
    main-loop Agent call it had nothing to do with."""
    proj = tmp_path / "projects"
    paths = _build_nested_corpus(proj)
    conn = _reconcile(tmp_path, proj, pricing_data)

    row = conn.execute(
        """SELECT parent_message_dedup_key, parent_agent_id, spawn_depth, subagent_type
             FROM subagent_spawns WHERE child_file_path=?""",
        (str(paths["inner"]),),
    ).fetchone()
    assert row is not None
    parent_key, parent_agent_id, depth, subtype = row

    assert parent_agent_id == "bbbb1111"
    assert subtype == "Plan"
    assert depth == 3

    assert parent_key is not None, "nested spawn must resolve a parent message"
    parent_uuid, parent_file = conn.execute(
        "SELECT uuid, file_path FROM messages WHERE dedup_key=?", (parent_key,)
    ).fetchone()
    assert parent_uuid == "o-agent", "must bind to the outer subagent's Agent call"
    assert parent_file == str(paths["outer"])
    assert parent_file != str(paths["main"])


def test_direct_subagent_keeps_depth_two_and_null_parent_agent(
    tmp_path: Path, pricing_data: dict
) -> None:
    """The ordinary main-loop spawn is unchanged: no parentAgentId, depth 2,
    parent message in the main session file."""
    proj = tmp_path / "projects"
    paths = _build_nested_corpus(proj)
    conn = _reconcile(tmp_path, proj, pricing_data)

    row = conn.execute(
        """SELECT parent_message_dedup_key, parent_agent_id, spawn_depth
             FROM subagent_spawns WHERE child_file_path=?""",
        (str(paths["outer"]),),
    ).fetchone()
    parent_key, parent_agent_id, depth = row
    assert parent_agent_id is None
    assert depth == 2
    parent_uuid, parent_file = conn.execute(
        "SELECT uuid, file_path FROM messages WHERE dedup_key=?", (parent_key,)
    ).fetchone()
    assert parent_uuid == "m-agent"
    assert parent_file == str(paths["main"])


def test_nested_spawn_resolves_when_child_is_walked_before_parent(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Nested children are siblings of their parent, so the sorted walk can
    reach the child first — its parent's messages aren't in the DB yet. The
    post-walk re-resolution pass has to repair that."""
    proj = tmp_path / "projects"
    # Agent ids are hex; 'aaaa0000' sorts before 'ffff9999', so the walk reaches
    # the nested child before the parent it depends on.
    paths = _build_nested_corpus(proj, outer_agent_id="ffff9999", inner_agent_id="aaaa0000")
    assert str(paths["inner"]) < str(paths["outer"]), "test needs child-before-parent ordering"

    conn = _reconcile(tmp_path, proj, pricing_data)

    parent_key, depth = conn.execute(
        "SELECT parent_message_dedup_key, spawn_depth FROM subagent_spawns WHERE child_file_path=?",
        (str(paths["inner"]),),
    ).fetchone()
    assert parent_key is not None, "walk order must not strand the nested spawn"
    assert depth == 3
    parent_uuid = conn.execute(
        "SELECT uuid FROM messages WHERE dedup_key=?", (parent_key,)
    ).fetchone()[0]
    assert parent_uuid == "o-agent"


def test_reingesting_only_the_parent_subagent_restores_nested_linkage(
    tmp_path: Path, pricing_data: dict
) -> None:
    """Incremental reconcile where the *parent subagent* file changed but the
    main file and the nested child did not.

    Purging the parent's messages cascades the nested child's
    ``parent_message_dedup_key`` to NULL (``ON DELETE SET NULL``), and the
    child's own mtime is untouched so the walk never revisits it. Without a
    re-resolution pass for sessions that contain nesting, the child would
    silently fall into ``unattributed`` on the next run.
    """
    proj = tmp_path / "projects"
    paths = _build_nested_corpus(proj)
    conn = _reconcile(tmp_path, proj, pricing_data)

    def nested_parent_key() -> str | None:
        return conn.execute(
            "SELECT parent_message_dedup_key FROM subagent_spawns WHERE child_file_path=?",
            (str(paths["inner"]),),
        ).fetchone()[0]

    assert nested_parent_key() is not None

    # Append a turn to the parent subagent only; main + child untouched.
    with paths["outer"].open("a") as fh:
        fh.write(
            json.dumps(
                _text_turn(
                    uuid="o-text-2",
                    session_id=SESSION,
                    ts="2026-07-01T10:02:00Z",
                    msg_id="msg-outer-3",
                    request_id="req-outer-3",
                    agent_id="bbbb1111",
                )
            )
            + "\n"
        )

    reconcile_projects_dir(conn, proj, pricing_data)

    assert nested_parent_key() is not None, "nested linkage must survive a parent re-ingest"
    buckets = {
        row[0]
        for row in conn.execute(
            "SELECT bucket_name FROM session_rollups WHERE session_id=?", (SESSION,)
        )
    }
    assert "unattributed" not in buckets


def test_nested_spawn_cost_lands_in_its_own_subagent_bucket(
    tmp_path: Path, pricing_data: dict
) -> None:
    """A resolved nested spawn buckets as ``subagent:<its own type>`` — not
    ``unattributed`` — and the per-session invariant still holds."""
    proj = tmp_path / "projects"
    _build_nested_corpus(proj)
    conn = _reconcile(tmp_path, proj, pricing_data)

    buckets = {
        name: (kind, cost)
        for name, kind, cost in conn.execute(
            "SELECT bucket_name, bucket_kind, cost_usd FROM session_rollups WHERE session_id=?",
            (SESSION,),
        )
    }
    assert "Plan" in buckets, f"nested agent missing its own bucket: {buckets}"
    assert buckets["Plan"][0] == "subagent"
    assert buckets["Plan"][1] > 0
    assert "unattributed" not in buckets

    msg_total = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM messages WHERE session_id=?", (SESSION,)
    ).fetchone()[0]
    roll_total = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM session_rollups WHERE session_id=?", (SESSION,)
    ).fetchone()[0]
    assert msg_total == pytest.approx(roll_total, abs=1e-6)


def test_spawn_depth_counts_a_four_level_chain(tmp_path: Path, pricing_data: dict) -> None:
    """Depth is a chain walk, not a flag: main(1) → 2 → 3 → 4."""
    proj = tmp_path / "projects"
    paths = _build_nested_corpus(proj)
    sub_dir = paths["inner"].parent

    # Give the depth-3 agent a child of its own.
    _write_jsonl(
        paths["inner"],
        [
            _agent_call(
                uuid="i-agent",
                session_id=SESSION,
                ts="2026-07-01T10:01:05Z",
                tool_use_id="tu-inner-explore",
                subagent_type="Explore",
                msg_id="msg-inner",
                request_id="req-inner",
                agent_id="cccc2222",
            ),
        ],
    )
    deepest = sub_dir / "agent-dddd3333.jsonl"
    _write_jsonl(
        deepest,
        [
            _text_turn(
                uuid="d-text",
                session_id=SESSION,
                ts="2026-07-01T10:01:20Z",
                msg_id="msg-deepest",
                request_id="req-deepest",
                agent_id="dddd3333",
            )
        ],
    )
    (sub_dir / "agent-dddd3333.meta.json").write_text(
        json.dumps({"agentType": "Explore", "parentAgentId": "cccc2222"})
    )

    conn = _reconcile(tmp_path, proj, pricing_data)

    depths = dict(
        conn.execute(
            "SELECT child_agent_id, spawn_depth FROM subagent_spawns WHERE parent_session_id=?",
            (SESSION,),
        )
    )
    assert depths == {"bbbb1111": 2, "cccc2222": 3, "dddd3333": 4}


def test_missing_parent_transcript_falls_back_to_main_file(
    tmp_path: Path, pricing_data: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """``parentAgentId`` naming an agent with no transcript on disk must warn
    and degrade to the main-file scan rather than dropping the spawn row."""
    proj = tmp_path / "projects"
    paths = _build_nested_corpus(proj)
    (paths["outer"].parent / "agent-bbbb1111.jsonl").unlink()
    (paths["outer"].parent / "agent-bbbb1111.meta.json").unlink()

    with caplog.at_level("WARNING"):
        conn = _reconcile(tmp_path, proj, pricing_data)

    assert any("no agent-bbbb1111.jsonl exists" in r.getMessage() for r in caplog.records)
    parent_key, depth = conn.execute(
        "SELECT parent_message_dedup_key, spawn_depth FROM subagent_spawns WHERE child_file_path=?",
        (str(paths["inner"]),),
    ).fetchone()
    parent_uuid = conn.execute(
        "SELECT uuid FROM messages WHERE dedup_key=?", (parent_key,)
    ).fetchone()[0]
    assert parent_uuid == "m-agent", "degrades to the pre-v7 main-file scan"
    # The chain is broken, so depth is unknown rather than guessed.
    assert depth is None


def test_nested_child_of_a_workflow_agent_finds_its_parent(
    tmp_path: Path, pricing_data: dict
) -> None:
    """A workflow agent lives under ``subagents/workflows/wf_<id>/`` but a
    subagent it spawns is written flat into ``subagents/``. The parent lookup
    has to search the whole tree, not just the child's own directory."""
    proj = tmp_path / "projects"
    enc = proj / "-home-test-proj"
    _write_jsonl(
        enc / f"{SESSION}.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "m-wf",
                "sessionId": SESSION,
                "timestamp": "2026-07-01T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "requestId": "req-wf",
                "message": {
                    "id": "msg-wf",
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-wf",
                            "name": "Workflow",
                            "input": {"name": "audit"},
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        ],
    )
    sub_dir = enc / SESSION / "subagents"
    wf_dir = sub_dir / "workflows" / "wf_abc123"
    wf_agent = wf_dir / "agent-eeee4444.jsonl"
    _write_jsonl(
        wf_agent,
        [
            _agent_call(
                uuid="w-agent",
                session_id=SESSION,
                ts="2026-07-01T10:00:30Z",
                tool_use_id="tu-wf-child",
                subagent_type="Explore",
                msg_id="msg-wfagent",
                request_id="req-wfagent",
                agent_id="eeee4444",
            )
        ],
    )
    (wf_dir / "agent-eeee4444.meta.json").write_text(json.dumps({"agentType": "workflow-subagent"}))

    child = sub_dir / "agent-ffff5555.jsonl"
    _write_jsonl(
        child,
        [
            _text_turn(
                uuid="wc-text",
                session_id=SESSION,
                ts="2026-07-01T10:00:45Z",
                msg_id="msg-wfchild",
                request_id="req-wfchild",
                agent_id="ffff5555",
            )
        ],
    )
    (sub_dir / "agent-ffff5555.meta.json").write_text(
        json.dumps({"agentType": "Explore", "parentAgentId": "eeee4444"})
    )

    conn = _reconcile(tmp_path, proj, pricing_data)

    parent_key = conn.execute(
        "SELECT parent_message_dedup_key FROM subagent_spawns WHERE child_file_path=?",
        (str(child),),
    ).fetchone()[0]
    assert parent_key is not None
    parent_uuid, parent_file = conn.execute(
        "SELECT uuid, file_path FROM messages WHERE dedup_key=?", (parent_key,)
    ).fetchone()
    assert parent_uuid == "w-agent"
    assert parent_file == str(wf_agent)


def test_meta_json_parent_id_added_after_jsonl_indexed_is_picked_up(
    tmp_path: Path, pricing_data: dict
) -> None:
    """A subagent's ``.meta.json`` can gain (or be corrected to add)
    ``parentAgentId`` after its ``.jsonl`` has already stabilized and been
    indexed. Incremental reconcile keys off the ``.jsonl``'s own
    ``(mtime_ns, size)``; without independently tracking the sidecar's own
    stat, a stale main-loop binding would survive forever, because the
    ``.jsonl`` itself never changes again."""
    proj = tmp_path / "projects"
    paths = _build_nested_corpus(proj)
    inner_meta = paths["inner"].with_suffix(".meta.json")

    # Start without parentAgentId, as if the sidecar hadn't been corrected yet.
    inner_meta.write_text(json.dumps({"agentType": "Plan", "description": "plan the work"}))

    conn = _reconcile(tmp_path, proj, pricing_data)

    def spawn_row() -> tuple:
        return conn.execute(
            """SELECT parent_message_dedup_key, parent_agent_id, spawn_depth
                 FROM subagent_spawns WHERE child_file_path=?""",
            (str(paths["inner"]),),
        ).fetchone()

    parent_key, parent_agent_id, depth = spawn_row()
    assert parent_agent_id is None
    assert depth == 2
    parent_uuid = conn.execute(
        "SELECT uuid FROM messages WHERE dedup_key=?", (parent_key,)
    ).fetchone()[0]
    assert parent_uuid == "m-agent", "without parentAgentId, falls back to the main-loop binding"

    # Correct the sidecar in place — the child's own .jsonl is untouched.
    inner_meta.write_text(
        json.dumps(
            {
                "agentType": "Plan",
                "description": "plan the work",
                "parentAgentId": "bbbb1111",
            }
        )
    )

    reconcile_projects_dir(conn, proj, pricing_data)

    parent_key, parent_agent_id, depth = spawn_row()
    assert parent_agent_id == "bbbb1111", (
        "meta.json change must be picked up even though the .jsonl didn't change"
    )
    assert depth == 3
    parent_uuid = conn.execute(
        "SELECT uuid FROM messages WHERE dedup_key=?", (parent_key,)
    ).fetchone()[0]
    assert parent_uuid == "o-agent"


def test_stale_parent_cache_not_reused_by_post_walk_reresolution(
    tmp_path: Path, pricing_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded parent-transcript cache (``_load_parent_entries_cached``)
    must not serve a stale parse to the post-walk re-resolution pass.

    Simulates Claude Code still appending to the outer subagent's transcript:
    when the nested child is walked first (its parent hasn't fully landed on
    disk yet from the walk's point of view), the cache gets warmed with a
    parse that's missing the spawning Agent/Task call. Moments later in the
    same walk, ``reconcile_file`` re-ingests the outer file directly and sees
    the complete content. If the cache entry isn't evicted at that point, the
    post-walk re-resolution pass reuses the earlier incomplete parse and the
    nested spawn never resolves — even though ``messages`` already has the
    right row.
    """
    import ccforensics.index as index_mod

    proj = tmp_path / "projects"
    # 'aaaa0000' sorts before 'ffff9999', so the walk reaches the nested
    # child before the outer subagent that's its actual parent.
    paths = _build_nested_corpus(proj, outer_agent_id="ffff9999", inner_agent_id="aaaa0000")
    assert str(paths["inner"]) < str(paths["outer"]), "test needs child-before-parent ordering"

    real_parse_file = index_mod.parse_file
    outer_reads = {"n": 0}

    def flaky_parse_file(path: Path):
        result = real_parse_file(path)
        if path == paths["outer"]:
            outer_reads["n"] += 1
            if outer_reads["n"] == 1:
                # First read (triggered by the child's spawn discovery):
                # the outer file's spawning Agent/Task turn hasn't been
                # written yet.
                return dataclasses.replace(
                    result, entries=[e for e in result.entries if e.uuid != "o-agent"]
                )
        return result

    monkeypatch.setattr(index_mod, "parse_file", flaky_parse_file)

    conn = _reconcile(tmp_path, proj, pricing_data)

    assert outer_reads["n"] >= 2, "test setup must actually re-read the outer file"

    parent_key, depth = conn.execute(
        "SELECT parent_message_dedup_key, spawn_depth FROM subagent_spawns WHERE child_file_path=?",
        (str(paths["inner"]),),
    ).fetchone()
    assert parent_key is not None, (
        "post-walk re-resolution must not reuse the stale cached parent parse"
    )
    assert depth == 3
    parent_uuid = conn.execute(
        "SELECT uuid FROM messages WHERE dedup_key=?", (parent_key,)
    ).fetchone()[0]
    assert parent_uuid == "o-agent"


def test_spawn_parent_path_picks_transcript_by_meta(tmp_path: Path) -> None:
    """Unit-level: the path chosen for the parent scan is driven by meta."""
    sub_dir = tmp_path / "-enc" / "S" / "subagents"
    sub_dir.mkdir(parents=True)
    parent = sub_dir / "agent-p1.jsonl"
    parent.write_text("")
    child = sub_dir / "agent-c1.jsonl"

    assert _spawn_parent_path(child, None) == tmp_path / "-enc" / "S.jsonl"
    assert (
        _spawn_parent_path(child, SpawnMeta(agentType="Explore")) == tmp_path / "-enc" / "S.jsonl"
    )
    assert _spawn_parent_path(child, SpawnMeta(parentAgentId="p1")) == parent
    # Unknown parent agent → main-file fallback.
    assert (
        _spawn_parent_path(child, SpawnMeta(parentAgentId="nope")) == tmp_path / "-enc" / "S.jsonl"
    )
