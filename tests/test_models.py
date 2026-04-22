from __future__ import annotations

import json

from ccforensics.models import parse_entry


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
