from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccforensics.models import SpawnMeta, load_meta_json, parse_entry


def test_parses_user_entry() -> None:
    raw = json.loads(
        '{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z",'
        '"isSidechain":false,"message":{"role":"user","content":[{"type":"text","text":"Hello"}]}}'
    )
    entry = parse_entry(raw)
    assert entry.type == "user"
    assert entry.uuid == "u1"
    assert entry.session_id == "s1"
    assert entry.is_sidechain is False


def test_parses_assistant_with_usage() -> None:
    raw = json.loads(
        '{"type":"assistant","uuid":"u2","sessionId":"s1","timestamp":"2026-04-20T10:00:05Z",'
        '"requestId":"req-a","message":{"id":"msg-1","role":"assistant",'
        '"model":"claude-sonnet-4-5-20250929","content":[],'
        '"usage":{"input_tokens":100,"output_tokens":5,"cache_read_input_tokens":500,'
        '"cache_creation_input_tokens":0}}}'
    )
    entry = parse_entry(raw)
    assert entry.type == "assistant"
    assert entry.request_id == "req-a"
    assert entry.message is not None
    assert entry.message.id == "msg-1"
    assert entry.message.model == "claude-sonnet-4-5-20250929"
    assert entry.message.usage is not None
    assert entry.message.usage.input_tokens == 100
    assert entry.message.usage.output_tokens == 5
    assert entry.message.usage.cache_read_input_tokens == 500
    assert entry.message.usage.cache_creation_input_tokens == 0


def test_field_normalization_legacy_parent_tool_use_id() -> None:
    raw_old = json.loads(
        '{"type":"user","parentToolUseId":"toolu-1","sessionId":"s1",'
        '"timestamp":"2026-04-20T10:00:00Z","message":null}'
    )
    raw_new = json.loads(
        '{"type":"user","sourceToolUseID":"toolu-1","sessionId":"s1",'
        '"timestamp":"2026-04-20T10:00:00Z","message":null}'
    )
    entry_old = parse_entry(raw_old)
    entry_new = parse_entry(raw_new)
    assert entry_old.source_tool_use_id == "toolu-1"
    assert entry_new.source_tool_use_id == "toolu-1"


def test_unknown_type_preserved() -> None:
    raw = json.loads(
        '{"type":"permission-mode","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z",'
        '"permissionMode":"bypassPermissions"}'
    )
    entry = parse_entry(raw)
    assert entry.type == "permission-mode"
    assert entry.extras.get("permissionMode") == "bypassPermissions"


def test_tolerates_missing_optional_fields() -> None:
    raw = json.loads('{"type":"user","timestamp":"2026-04-20T10:00:00Z"}')
    entry = parse_entry(raw)
    assert entry.type == "user"
    assert entry.uuid is None
    assert entry.session_id is None


def test_tool_use_content_block_parsed() -> None:
    raw = json.loads(
        '{"type":"assistant","uuid":"u","timestamp":"2026-04-20T10:00:00Z","requestId":"r",'
        '"message":{"id":"m","role":"assistant","model":"claude-sonnet-4-5",'
        '"content":[{"type":"tool_use","id":"toolu-99","name":"Read",'
        '"input":{"file_path":"/x"}}]}}'
    )
    entry = parse_entry(raw)
    assert entry.message is not None
    blocks = entry.message.content
    assert len(blocks) == 1
    assert blocks[0].type == "tool_use"
    assert blocks[0].id == "toolu-99"
    assert blocks[0].name == "Read"


def test_parse_entry_accepts_string_content() -> None:
    """Real Claude Code JSONL emits ``message.content`` as a bare string for
    plain user text prompts. parse_entry must normalize that into a single
    text block rather than letting pydantic reject the entry."""
    raw = json.loads(
        '{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z",'
        '"message":{"role":"user","content":"hello"}}'
    )
    entry = parse_entry(raw)
    assert entry.message is not None
    assert len(entry.message.content) == 1
    block = entry.message.content[0]
    assert block.type == "text"
    assert block.text == "hello"


def test_parse_entry_accepts_list_content_unchanged() -> None:
    """When content is already a list, leave it alone — don't double-wrap."""
    raw = json.loads(
        '{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z",'
        '"message":{"role":"user","content":[{"type":"text","text":"hi"}]}}'
    )
    entry = parse_entry(raw)
    assert entry.message is not None
    assert len(entry.message.content) == 1
    assert entry.message.content[0].type == "text"
    assert entry.message.content[0].text == "hi"


def test_parse_entry_accepts_missing_content() -> None:
    """message present but content key absent → default empty list, no error."""
    raw = json.loads(
        '{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z",'
        '"message":{"role":"user"}}'
    )
    entry = parse_entry(raw)
    assert entry.message is not None
    assert entry.message.content == []


def test_parse_entry_accepts_empty_string_content() -> None:
    """Empty string content should normalize to one empty text block,
    preserving _first_text_block semantics (which returns None for empty)."""
    raw = json.loads(
        '{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z",'
        '"message":{"role":"user","content":""}}'
    )
    entry = parse_entry(raw)
    assert entry.message is not None
    assert len(entry.message.content) == 1
    assert entry.message.content[0].type == "text"
    assert entry.message.content[0].text == ""


def test_parse_entry_accepts_tool_result_list_content() -> None:
    """List-shaped content with a tool_result block (the other real-corpus
    shape for type='user' entries) must parse cleanly."""
    raw = json.loads(
        '{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z",'
        '"message":{"role":"user","content":[{"tool_use_id":"toolu-x",'
        '"type":"tool_result","content":"output"}]}}'
    )
    entry = parse_entry(raw)
    assert entry.message is not None
    assert len(entry.message.content) == 1
    block = entry.message.content[0]
    assert block.type == "tool_result"
    assert block.tool_use_id == "toolu-x"
    assert block.content == "output"


def test_parse_entry_message_none_does_not_raise() -> None:
    """message=None must not crash _normalize_message_content."""
    raw = json.loads('{"type":"user","timestamp":"2026-04-20T10:00:00Z","message":null}')
    entry = parse_entry(raw)
    assert entry.message is None


def test_attachment_hook_success_recognized() -> None:
    raw = json.loads(
        '{"type":"attachment","timestamp":"2026-04-20T10:00:00Z",'
        '"attachment":{"type":"hook_success","hookEvent":"SessionStart",'
        '"stdout":"{\\"hookSpecificOutput\\":{}}","content":""}}'
    )
    entry = parse_entry(raw)
    assert entry.type == "attachment"
    assert entry.attachment is not None
    assert entry.attachment.type == "hook_success"
    assert entry.attachment.hook_event == "SessionStart"


# ---------- SpawnMeta + load_meta_json ----------


def test_spawn_meta_parses_minimal_shape(tmp_path: Path) -> None:
    p = tmp_path / "agent-abc.meta.json"
    p.write_text('{"agentType":"Explore","description":"walk the src tree"}')
    meta = load_meta_json(p)
    assert meta is not None
    assert meta.agent_type == "Explore"
    assert meta.description == "walk the src tree"


def test_spawn_meta_allows_extra_fields(tmp_path: Path) -> None:
    """Future meta.json fields must not break the loader."""
    p = tmp_path / "agent-abc.meta.json"
    p.write_text('{"agentType":"general-purpose","description":"x","futureField":42}')
    meta = load_meta_json(p)
    assert meta is not None
    assert meta.agent_type == "general-purpose"


def test_load_meta_json_missing_file_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "agent-nope.meta.json"
    assert load_meta_json(p) is None


def test_load_meta_json_malformed_json_returns_none_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    p = tmp_path / "agent-bad.meta.json"
    p.write_text("{not valid json")
    caplog.set_level("WARNING", logger="ccforensics.models")
    assert load_meta_json(p) is None
    assert any("agent-bad" in r.getMessage() for r in caplog.records)


def test_load_meta_json_unexpected_schema_returns_none_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Top-level array instead of object — don't crash, log, return None."""
    p = tmp_path / "agent-weird.meta.json"
    p.write_text('["not","an","object"]')
    caplog.set_level("WARNING", logger="ccforensics.models")
    assert load_meta_json(p) is None


def test_load_meta_json_missing_agent_type_still_parses(tmp_path: Path) -> None:
    """Permissive: agentType may be absent. Return SpawnMeta with
    agent_type=None; let caller decide."""
    p = tmp_path / "agent-notype.meta.json"
    p.write_text('{"description":"only desc"}')
    meta = load_meta_json(p)
    assert meta is not None
    assert meta.agent_type is None
    assert meta.description == "only desc"


def test_spawn_meta_directly_constructible() -> None:
    """SpawnMeta can be built in-memory (used by tests in tree module)."""
    meta = SpawnMeta(agentType="Plan", description="plan the refactor")
    assert meta.agent_type == "Plan"
    assert meta.description == "plan the refactor"


def test_usage_stats_captures_service_tier() -> None:
    from ccforensics.models import UsageStats

    block = UsageStats.model_validate(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "service_tier": "priority",
        }
    )
    assert block.service_tier == "priority"


def test_usage_stats_service_tier_optional() -> None:
    from ccforensics.models import UsageStats

    block = UsageStats.model_validate({"input_tokens": 100, "output_tokens": 50})
    assert block.service_tier is None
