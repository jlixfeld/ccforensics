from __future__ import annotations

import random
from pathlib import Path

from ccforensics.jsonl import dedup_entries, dedup_key, parse_file
from ccforensics.models import parse_entry

FIXTURES = Path(__file__).parent / "fixtures"


def test_dedup_key_prefers_message_id_plus_request_id() -> None:
    entry = parse_entry(
        {
            "type": "assistant",
            "timestamp": "2026-04-20T10:00:00Z",
            "sessionId": "s1",
            "requestId": "req-a",
            "message": {"id": "msg-1", "role": "assistant", "content": []},
        }
    )
    assert dedup_key(entry) == "req:msg-1:req-a"


def test_dedup_key_fallback_to_session_id() -> None:
    entry = parse_entry(
        {
            "type": "assistant",
            "timestamp": "2026-04-20T10:00:00Z",
            "sessionId": "s1",
            "message": {"id": "msg-1", "role": "assistant", "content": []},
        }
    )
    assert dedup_key(entry) == "session:msg-1:s1"


def test_dedup_key_returns_none_when_no_message_id() -> None:
    entry = parse_entry(
        {
            "type": "user",
            "timestamp": "2026-04-20T10:00:00Z",
            "sessionId": "s1",
            "message": None,
        }
    )
    assert dedup_key(entry) is None


def test_dedup_across_files_collapses_shared_key() -> None:
    a = parse_file(FIXTURES / "dedup_collision" / "file_a.jsonl")
    b = parse_file(FIXTURES / "dedup_collision" / "file_b.jsonl")
    merged = dedup_entries(a.entries + b.entries)
    keys = {dedup_key(e) for e in merged if dedup_key(e) is not None}
    assert "req:msg-shared:req-shared" in keys
    assert "req:msg-a-only:req-a-only" in keys
    assert "req:msg-b-only:req-b-only" in keys
    assert len([e for e in merged if dedup_key(e) == "req:msg-shared:req-shared"]) == 1


def test_dedup_deterministic_under_shuffle() -> None:
    a = parse_file(FIXTURES / "dedup_collision" / "file_a.jsonl")
    b = parse_file(FIXTURES / "dedup_collision" / "file_b.jsonl")
    combined = a.entries + b.entries

    keys_runs: list[list[str]] = []
    for seed in (1, 2, 3, 42, 99):
        shuffled = combined.copy()
        random.Random(seed).shuffle(shuffled)
        merged = dedup_entries(shuffled)
        keys_runs.append(sorted(dedup_key(e) or "" for e in merged))

    for run in keys_runs[1:]:
        assert run == keys_runs[0], "dedup output must be deterministic regardless of input order"


def test_dedup_latest_wins_when_content_equal() -> None:
    """Collision tiebreak: when both entries have equally-empty content,
    the latest-timestamped one wins. Prior policy was earliest-wins; it
    was flipped to handle Claude Code's streamed-write pattern where the
    richer state arrives later."""
    e1 = parse_entry(
        {
            "type": "assistant",
            "timestamp": "2026-04-20T10:00:00Z",
            "sessionId": "s1",
            "requestId": "r",
            "message": {"id": "m", "role": "assistant", "content": []},
        }
    )
    e2 = parse_entry(
        {
            "type": "assistant",
            "timestamp": "2026-04-20T10:05:00Z",
            "sessionId": "s1",
            "requestId": "r",
            "message": {"id": "m", "role": "assistant", "content": []},
        }
    )
    merged = dedup_entries([e2, e1])
    assert len(merged) == 1
    assert merged[0].timestamp == e2.timestamp


def test_dedup_tool_use_wins_over_empty() -> None:
    """Two entries share a dedup key; one has a tool_use block, the other
    is empty. The one with tool_use wins — even if it's later or earlier."""
    empty = parse_entry(
        {
            "type": "assistant",
            "timestamp": "2026-04-20T10:05:00Z",
            "sessionId": "s1",
            "requestId": "r",
            "message": {"id": "m", "role": "assistant", "content": []},
        }
    )
    with_tool = parse_entry(
        {
            "type": "assistant",
            "timestamp": "2026-04-20T10:00:00Z",  # earlier
            "sessionId": "s1",
            "requestId": "r",
            "message": {
                "id": "m",
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu1", "name": "Agent", "input": {}}],
            },
        }
    )
    for ordering in ([empty, with_tool], [with_tool, empty]):
        merged = dedup_entries(ordering)
        assert len(merged) == 1
        assert merged[0].timestamp == with_tool.timestamp


def test_dedup_more_content_wins_on_tool_use_tie() -> None:
    """Both have no tool_use but one has more non-empty text blocks → wins."""
    sparse = parse_entry(
        {
            "type": "assistant",
            "timestamp": "2026-04-20T10:05:00Z",
            "sessionId": "s1",
            "requestId": "r",
            "message": {
                "id": "m",
                "role": "assistant",
                "content": [{"type": "text", "text": "one"}],
            },
        }
    )
    rich = parse_entry(
        {
            "type": "assistant",
            "timestamp": "2026-04-20T10:00:00Z",
            "sessionId": "s1",
            "requestId": "r",
            "message": {
                "id": "m",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "one"},
                    {"type": "text", "text": "two"},
                ],
            },
        }
    )
    merged = dedup_entries([sparse, rich])
    assert len(merged) == 1
    assert merged[0].timestamp == rich.timestamp
