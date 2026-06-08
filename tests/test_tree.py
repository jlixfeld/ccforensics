from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ccforensics.models import SpawnMeta, TranscriptEntry, parse_entry
from ccforensics.tree import Spawn, _workflow_name, build_session_graph, discover_spawn

# ---------- fixture helpers ----------


def _entry(
    uuid: str,
    *,
    ts: str = "2026-04-22T10:00:00Z",
    type_: str = "assistant",
    tool_uses: list[tuple[str, str]] | None = None,
    source_tool_use_id: str | None = None,
    session_id: str = "sess-1",
) -> TranscriptEntry:
    """Build a TranscriptEntry via parse_entry."""
    raw: dict[str, Any] = {
        "type": type_,
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
    }
    if source_tool_use_id is not None:
        raw["sourceToolUseID"] = source_tool_use_id
    content: list[dict[str, Any]] = []
    for tuid, tname in tool_uses or []:
        content.append({"type": "tool_use", "id": tuid, "name": tname, "input": {}})
    raw["message"] = {"role": "assistant", "content": content}
    return parse_entry(raw)


def _assistant_with_agent(
    uuid: str,
    ts: str,
    tool_use_id: str,
    subagent_type: str | None,
    model: str | None = "claude-sonnet-4-5-20250929",
) -> TranscriptEntry:
    raw: dict[str, Any] = {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "parent-sess",
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "message": {
            "role": "assistant",
            "model": model,
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Agent",
                    "input": {"subagent_type": subagent_type} if subagent_type else {},
                }
            ],
        },
    }
    return parse_entry(raw)


def _child_first_entry(ts: str, child_agent_id: str) -> TranscriptEntry:
    raw: dict[str, Any] = {
        "type": "user",
        "uuid": "c-u1",
        "sessionId": "parent-sess",
        "timestamp": ts,
        "isSidechain": True,
        "isMeta": False,
        "agentId": child_agent_id,
        "message": {"role": "user", "content": "spawn prompt"},
    }
    return parse_entry(raw)


def _child_assistant(
    ts: str, child_agent_id: str, model: str = "claude-opus-4-7"
) -> TranscriptEntry:
    raw: dict[str, Any] = {
        "type": "assistant",
        "uuid": "c-u2",
        "sessionId": "parent-sess",
        "timestamp": ts,
        "isSidechain": True,
        "isMeta": False,
        "agentId": child_agent_id,
        "message": {"role": "assistant", "model": model, "content": []},
    }
    return parse_entry(raw)


# ---------- pass 1: build_session_graph ----------


def test_build_session_graph_simple_tool_use_result_pair() -> None:
    entries = [
        _entry("u1", tool_uses=[("tu1", "Read")]),
        _entry("u2", source_tool_use_id="tu1", type_="user"),
    ]
    g = build_session_graph(entries)
    assert g.emitter_of_tool_use["tu1"] == "u1"
    assert g.children_of_tool_use["tu1"] == ["u2"]
    assert g.parent_tool_use_id("u2") == "tu1"
    assert g.parent_tool_use_id("u1") is None
    assert g.orphan_children == []


def test_build_session_graph_multi_tool_use_per_message() -> None:
    """One assistant turn may emit multiple tool_use blocks."""
    entries = [
        _entry("u1", tool_uses=[("tu1", "Read"), ("tu2", "Grep")]),
        _entry("u2", ts="2026-04-22T10:00:01Z", source_tool_use_id="tu2", type_="user"),
        _entry("u3", ts="2026-04-22T10:00:02Z", source_tool_use_id="tu1", type_="user"),
    ]
    g = build_session_graph(entries)
    assert g.emitter_of_tool_use["tu1"] == "u1"
    assert g.emitter_of_tool_use["tu2"] == "u1"
    assert set(g.children_of_tool_use["tu1"]) == {"u3"}
    assert set(g.children_of_tool_use["tu2"]) == {"u2"}


def test_descendants_of_transitive_closure() -> None:
    """A tool_result that itself emits a further tool_use extends the chain."""
    entries = [
        _entry("u1", tool_uses=[("tu1", "Agent")]),
        _entry(
            "u2",
            ts="2026-04-22T10:00:01Z",
            source_tool_use_id="tu1",
            tool_uses=[("tu2", "Read")],
        ),
        _entry("u3", ts="2026-04-22T10:00:02Z", source_tool_use_id="tu2", type_="user"),
    ]
    g = build_session_graph(entries)
    assert g.descendants_of("tu1") == {"u2", "u3"}
    assert g.descendants_of("tu2") == {"u3"}


def test_orphan_child_when_parent_tool_use_missing() -> None:
    entries = [
        _entry("u1", source_tool_use_id="tu-missing", type_="user"),
        _entry("u2", ts="2026-04-22T10:00:01Z", tool_uses=[("tu2", "Read")]),
        _entry("u3", ts="2026-04-22T10:00:02Z", source_tool_use_id="tu2", type_="user"),
    ]
    g = build_session_graph(entries)
    assert g.orphan_children == ["u1"]
    for children in g.children_of_tool_use.values():
        assert "u1" not in children


def test_empty_session_returns_empty_graph() -> None:
    g = build_session_graph([])
    assert g.emitter_of_tool_use == {}
    assert g.children_of_tool_use == {}
    assert g.orphan_children == []
    assert g.descendants_of("anything") == set()
    assert g.parent_tool_use_id("anything") is None


def test_entries_without_uuid_are_skipped() -> None:
    """Summary records (leafUuid only, no uuid) aren't graph nodes."""
    raw = {
        "type": "summary",
        "timestamp": "2026-04-22T10:00:00Z",
        "leafUuid": "u1",
        "summary": "stub",
    }
    entries = [parse_entry(raw), _entry("u2", tool_uses=[("tu1", "Read")])]
    g = build_session_graph(entries)
    assert g.emitter_of_tool_use == {"tu1": "u2"}
    assert g.orphan_children == []


def test_duplicate_tool_use_id_keeps_first_emitter() -> None:
    """First-wins by timestamp for duplicate tool_use_id (should never
    happen in practice)."""
    entries = [
        _entry("u1", ts="2026-04-22T10:00:00Z", tool_uses=[("tu1", "Read")]),
        _entry("u2", ts="2026-04-22T10:00:05Z", tool_uses=[("tu1", "Grep")]),
    ]
    g = build_session_graph(entries)
    assert g.emitter_of_tool_use["tu1"] == "u1"


# ---------- pass 2: discover_spawn ----------


def test_discover_spawn_clean_match() -> None:
    parent = [
        _assistant_with_agent("p-u1", "2026-04-22T10:00:00Z", "tu-agent-1", "Explore"),
    ]
    child = [
        _child_first_entry("2026-04-22T10:00:05Z", "abc123"),
        _child_assistant("2026-04-22T10:00:10Z", "abc123", model="claude-opus-4-7"),
    ]
    meta = SpawnMeta(agentType="Explore", description="walk src")

    spawn = discover_spawn(
        parent_session_id="parent-sess",
        child_agent_id="abc123",
        child_file_path=Path("/fake/agent-abc123.jsonl"),
        child_entries=child,
        parent_entries=parent,
        meta=meta,
    )
    assert spawn is not None
    assert spawn.parent_message_uuid == "p-u1"
    assert spawn.parent_tool_use_id == "tu-agent-1"
    assert spawn.subagent_type == "Explore"
    assert spawn.description == "walk src"
    assert spawn.model_hint == "claude-opus-4-7"
    assert spawn.ts_spawned == datetime(2026, 4, 22, 10, 0, 5, tzinfo=UTC)


def test_discover_spawn_nearest_before_without_meta() -> None:
    """No meta → pure nearest-before."""
    parent = [
        _assistant_with_agent("p-u1", "2026-04-22T09:00:00Z", "tu-old", "Explore"),
        _assistant_with_agent("p-u2", "2026-04-22T10:00:00Z", "tu-near", "Plan"),
    ]
    child = [_child_first_entry("2026-04-22T10:00:05Z", "abc")]

    spawn = discover_spawn(
        parent_session_id="parent-sess",
        child_agent_id="abc",
        child_file_path=Path("/fake/agent-abc.jsonl"),
        child_entries=child,
        parent_entries=parent,
        meta=None,
    )
    assert spawn is not None
    assert spawn.parent_tool_use_id == "tu-near"
    assert spawn.subagent_type == "Plan"


def test_discover_spawn_type_match_beats_nearer_mismatch() -> None:
    parent = [
        _assistant_with_agent("p-u1", "2026-04-22T09:00:00Z", "tu-old-explore", "Explore"),
        _assistant_with_agent("p-u2", "2026-04-22T10:00:00Z", "tu-near-plan", "Plan"),
    ]
    child = [_child_first_entry("2026-04-22T10:00:05Z", "abc")]
    meta = SpawnMeta(agentType="Explore", description="x")

    spawn = discover_spawn(
        parent_session_id="parent-sess",
        child_agent_id="abc",
        child_file_path=Path("/fake/agent-abc.jsonl"),
        child_entries=child,
        parent_entries=parent,
        meta=meta,
    )
    assert spawn is not None
    assert spawn.parent_tool_use_id == "tu-old-explore"


def test_discover_spawn_multiple_matches_pick_nearest() -> None:
    parent = [
        _assistant_with_agent("p-u1", "2026-04-22T08:00:00Z", "tu-first", "Explore"),
        _assistant_with_agent("p-u2", "2026-04-22T09:00:00Z", "tu-middle", "Explore"),
        _assistant_with_agent("p-u3", "2026-04-22T10:00:00Z", "tu-latest", "Explore"),
    ]
    child = [_child_first_entry("2026-04-22T10:00:05Z", "abc")]
    meta = SpawnMeta(agentType="Explore")

    spawn = discover_spawn(
        parent_session_id="parent-sess",
        child_agent_id="abc",
        child_file_path=Path("/fake/agent-abc.jsonl"),
        child_entries=child,
        parent_entries=parent,
        meta=meta,
    )
    assert spawn is not None
    assert spawn.parent_tool_use_id == "tu-latest"


def test_discover_spawn_no_matches_falls_back_to_nearest() -> None:
    parent = [
        _assistant_with_agent("p-u1", "2026-04-22T09:00:00Z", "tu-plan-1", "Plan"),
        _assistant_with_agent("p-u2", "2026-04-22T10:00:00Z", "tu-plan-2", "Plan"),
    ]
    child = [_child_first_entry("2026-04-22T10:00:05Z", "abc")]
    meta = SpawnMeta(agentType="Explore")

    spawn = discover_spawn(
        parent_session_id="parent-sess",
        child_agent_id="abc",
        child_file_path=Path("/fake/agent-abc.jsonl"),
        child_entries=child,
        parent_entries=parent,
        meta=meta,
    )
    assert spawn is not None
    assert spawn.parent_tool_use_id == "tu-plan-2"
    # subagent_type from meta — authoritative even when the match failed.
    assert spawn.subagent_type == "Explore"


def test_discover_spawn_unresolvable_no_candidates() -> None:
    """Parent has no Agent/Task before ts_spawned → null parent fields."""
    parent = [
        _assistant_with_agent("p-u1", "2026-04-22T11:00:00Z", "tu-late", "Explore"),
    ]
    child = [_child_first_entry("2026-04-22T10:00:05Z", "abc")]
    meta = SpawnMeta(agentType="Explore")

    spawn = discover_spawn(
        parent_session_id="parent-sess",
        child_agent_id="abc",
        child_file_path=Path("/fake/agent-abc.jsonl"),
        child_entries=child,
        parent_entries=parent,
        meta=meta,
    )
    assert spawn is not None
    assert spawn.parent_message_uuid is None
    assert spawn.parent_tool_use_id is None
    assert spawn.subagent_type == "Explore"
    assert spawn.ts_spawned == datetime(2026, 4, 22, 10, 0, 5, tzinfo=UTC)


def test_discover_spawn_empty_child_returns_none() -> None:
    parent = [_assistant_with_agent("p-u1", "2026-04-22T10:00:00Z", "tu", "Explore")]
    assert (
        discover_spawn(
            parent_session_id="parent-sess",
            child_agent_id="abc",
            child_file_path=Path("/fake/agent-abc.jsonl"),
            child_entries=[],
            parent_entries=parent,
            meta=None,
        )
        is None
    )


def test_discover_spawn_model_hint_from_first_child_assistant() -> None:
    parent = [_assistant_with_agent("p-u1", "2026-04-22T10:00:00Z", "tu", "Explore")]
    child = [
        _child_first_entry("2026-04-22T10:00:05Z", "abc"),
        _child_assistant("2026-04-22T10:00:10Z", "abc", model="claude-haiku-4-5"),
    ]
    spawn = discover_spawn(
        parent_session_id="parent-sess",
        child_agent_id="abc",
        child_file_path=Path("/fake/agent-abc.jsonl"),
        child_entries=child,
        parent_entries=parent,
        meta=None,
    )
    assert spawn is not None
    assert spawn.model_hint == "claude-haiku-4-5"


def test_discover_spawn_model_hint_none_when_no_assistant() -> None:
    parent = [_assistant_with_agent("p-u1", "2026-04-22T10:00:00Z", "tu", "Explore")]
    child = [_child_first_entry("2026-04-22T10:00:05Z", "abc")]
    spawn = discover_spawn(
        parent_session_id="parent-sess",
        child_agent_id="abc",
        child_file_path=Path("/fake/agent-abc.jsonl"),
        child_entries=child,
        parent_entries=parent,
        meta=None,
    )
    assert spawn is not None
    assert spawn.model_hint is None


def test_spawn_dataclass_frozen() -> None:
    """Spawn is immutable — consumers can't mutate it."""
    spawn = Spawn(
        parent_session_id="s",
        child_agent_id="a",
        child_file_path="/x",
        subagent_type=None,
        description=None,
        ts_spawned=datetime(2026, 4, 22, tzinfo=UTC),
        parent_message_uuid=None,
        parent_tool_use_id=None,
        model_hint=None,
    )
    try:
        spawn.subagent_type = "Explore"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("Spawn should be frozen")


# ---------- _workflow_name ----------


def test_workflow_name_from_saved_name() -> None:
    assert _workflow_name({"name": "review-pr"}) == "review-pr"


def test_workflow_name_from_script_path_strips_wf_suffix() -> None:
    assert _workflow_name({"scriptPath": "/a/b/sdk-drift-audit-wf_2328ca35-f9d.js"}) == "sdk-drift-audit"


def test_workflow_name_from_inline_script_meta() -> None:
    script = "export const meta = {\n  name: 'find-flaky',\n  description: 'x',\n}"
    assert _workflow_name({"script": script}) == "find-flaky"


def test_workflow_name_prefers_name_over_script() -> None:
    assert _workflow_name({"name": "saved", "script": "name: 'inline'"}) == "saved"


def test_workflow_name_none_when_unparseable() -> None:
    assert _workflow_name({"script": "no meta here"}) is None
    assert _workflow_name({}) is None
    assert _workflow_name("not a dict") is None


# ---------- discover_spawn workflow ----------


def _wf_parent_entries() -> list[TranscriptEntry]:
    return [
        parse_entry({
            "type": "assistant",
            "uuid": "p1",
            "sessionId": "SESS",
            "timestamp": "2026-06-08T10:00:00Z",
            "requestId": "r1",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{
                    "type": "tool_use",
                    "id": "tu-wf",
                    "name": "Workflow",
                    "input": {"script": "export const meta = { name: 'sdk-drift-audit' }"},
                }],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }),
    ]


def _wf_child_entries() -> list[TranscriptEntry]:
    return [
        parse_entry({
            "type": "assistant",
            "uuid": "c1",
            "sessionId": "SESS",
            "agentId": "dead",
            "timestamp": "2026-06-08T10:00:30Z",
            "isSidechain": True,
            "requestId": "r2",
            "message": {
                "id": "m2",
                "role": "assistant",
                "model": "claude-haiku-4-5-20251001",
                "content": [{"type": "text", "text": "x"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }),
    ]


def test_discover_spawn_workflow_links_and_names() -> None:
    child_path = Path("/p/-enc/SESS/subagents/workflows/wf_2328ca35-f9d/agent-dead.jsonl")
    meta = SpawnMeta(agentType="Explore")  # per-agent type MUST be ignored for the bucket name
    spawn = discover_spawn(
        parent_session_id="SESS",
        child_agent_id="dead",
        child_file_path=child_path,
        child_entries=_wf_child_entries(),
        parent_entries=_wf_parent_entries(),
        meta=meta,
        is_workflow=True,
    )
    assert spawn is not None
    assert spawn.subagent_type == "workflow:sdk-drift-audit"
    assert spawn.parent_tool_use_id == "tu-wf"
    assert spawn.parent_message_uuid == "p1"
    assert spawn.model_hint == "claude-haiku-4-5-20251001"


def test_discover_spawn_workflow_falls_back_to_wf_id() -> None:
    child_path = Path("/p/-enc/SESS/subagents/workflows/wf_abc123/agent-dead.jsonl")
    parent = _wf_parent_entries()
    parent[0].message.content[0].input = {}  # type: ignore[index]  # wipe the script so no name extractable
    spawn = discover_spawn(
        parent_session_id="SESS",
        child_agent_id="dead",
        child_file_path=child_path,
        child_entries=_wf_child_entries(),
        parent_entries=parent,
        meta=None,
        is_workflow=True,
    )
    assert spawn is not None
    assert spawn.subagent_type == "workflow:wf_abc123"


def test_discover_spawn_workflow_unresolvable_parent() -> None:
    child_path = Path("/p/-enc/SESS/subagents/workflows/wf_abc123/agent-dead.jsonl")
    spawn = discover_spawn(
        parent_session_id="SESS",
        child_agent_id="dead",
        child_file_path=child_path,
        child_entries=_wf_child_entries(),
        parent_entries=[],
        meta=None,
        is_workflow=True,
    )
    assert spawn is not None
    assert spawn.parent_message_uuid is None
    assert spawn.subagent_type == "workflow:wf_abc123"


def test_discover_spawn_workflow_picks_nearest_before() -> None:
    """Two Workflow calls before the spawn → the latest-before one wins."""
    parent = [
        parse_entry({
            "type": "assistant",
            "uuid": "p-old",
            "sessionId": "SESS",
            "timestamp": "2026-06-08T10:00:00Z",
            "requestId": "r1",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{
                    "type": "tool_use", "id": "tu-old", "name": "Workflow",
                    "input": {"name": "older"},
                }],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }),
        parse_entry({
            "type": "assistant",
            "uuid": "p-new",
            "sessionId": "SESS",
            "timestamp": "2026-06-08T10:00:25Z",
            "requestId": "r1b",
            "message": {
                "id": "m1b",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{
                    "type": "tool_use", "id": "tu-new", "name": "Workflow",
                    "input": {"name": "newer"},
                }],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }),
    ]
    spawn = discover_spawn(
        parent_session_id="SESS",
        child_agent_id="dead",
        child_file_path=Path("/p/-enc/SESS/subagents/workflows/wf_x/agent-dead.jsonl"),
        child_entries=_wf_child_entries(),
        parent_entries=parent,
        meta=None,
        is_workflow=True,
    )
    assert spawn is not None
    assert spawn.parent_tool_use_id == "tu-new"
    assert spawn.subagent_type == "workflow:newer"
