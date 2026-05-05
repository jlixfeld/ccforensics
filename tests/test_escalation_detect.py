"""Tests for the escalation event detector — model_switch + auto_mode
kinds (T5). Subagent_dispatch lives in T6 with cross-session helpers.
"""

from __future__ import annotations

from typing import Any

from ccforensics.models import parse_entry
from ccforensics.thrash import (
    SubagentSpawnInfo,
    detect_escalation,
    detect_subagent_escalation,
    model_tier,
    select_earliest_escalation,
)

# ---------- helpers ----------


def _ts(i: int) -> str:
    hour = 10 + i // 60
    minute = i % 60
    return f"2026-04-22T{hour:02d}:{minute:02d}:00Z"


def _user(uuid: str, ts: str, text: Any = "ok") -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": "s",
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "message": {"role": "user", "content": text},
    }


def _assistant(
    uuid: str,
    ts: str,
    *,
    model: str = "claude-sonnet-4-5-20250929",
    cost_usd: float = 0.0,
) -> dict[str, Any]:
    raw = {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "s",
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "requestId": f"r-{uuid}",
        "message": {
            "id": f"m-{uuid}",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }
    if cost_usd:
        raw["annotated_cost_usd"] = cost_usd
    return raw


# ---------- model_tier ----------


def test_model_tier_ranks_haiku_sonnet_opus() -> None:
    assert model_tier("claude-haiku-4-5") == 1
    assert model_tier("claude-sonnet-4-6") == 2
    assert model_tier("claude-opus-4-7") == 3
    assert model_tier("claude-sonnet-4-5-20250929") == 2


def test_model_tier_unknown_returns_zero() -> None:
    assert model_tier(None) == 0
    assert model_tier("") == 0
    assert model_tier("gpt-4o") == 0
    assert model_tier("claude-foo-1-0") == 0


# ---------- model_switch escalation ----------


def test_detect_escalation_sonnet_to_opus_records_event() -> None:
    raw = [_user("u-init", _ts(0), "go")]
    for i in range(5):
        raw.append(
            _assistant(
                f"a{i}",
                _ts(1 + i),
                model="claude-sonnet-4-6",
                cost_usd=0.10,
            )
        )
    raw.append(_assistant("a-opus1", _ts(6), model="claude-opus-4-7", cost_usd=0.50))
    raw.append(_assistant("a-opus2", _ts(7), model="claude-opus-4-7", cost_usd=0.40))

    entries = [parse_entry(r) for r in raw]
    event = detect_escalation(entries)

    assert event is not None
    assert event["escalation_kind"] == "model_switch"
    assert event["from_model"] == "claude-sonnet-4-6"
    assert event["to_model"] == "claude-opus-4-7"
    assert event["turn_index"] == 5
    assert abs(event["cost_before_switch_usd"] - 0.50) < 1e-6
    assert abs(event["cost_after_switch_usd"] - 0.90) < 1e-6
    assert event["subagent_prompt_excerpt"] is None


def test_detect_escalation_haiku_to_sonnet_counts() -> None:
    raw = [_user("u", _ts(0), "go")]
    raw.extend(_assistant(f"a{i}", _ts(1 + i), model="claude-haiku-4-5") for i in range(2))
    raw.append(_assistant("a-up", _ts(3), model="claude-sonnet-4-6"))
    entries = [parse_entry(r) for r in raw]
    event = detect_escalation(entries)
    assert event is not None
    assert event["escalation_kind"] == "model_switch"


def test_detect_escalation_same_tier_is_not_escalation() -> None:
    """Sonnet 4.6 -> Sonnet 4.7 is a model rev, not a tier escalation."""
    raw = [_user("u", _ts(0), "go")]
    raw.extend(_assistant(f"a{i}", _ts(1 + i), model="claude-sonnet-4-6") for i in range(3))
    raw.append(_assistant("a-rev", _ts(4), model="claude-sonnet-4-7"))
    entries = [parse_entry(r) for r in raw]
    assert detect_escalation(entries) is None


def test_detect_escalation_returns_none_for_pure_session() -> None:
    raw = [_user("u", _ts(0), "go")]
    raw.extend(_assistant(f"a{i}", _ts(1 + i), model="claude-sonnet-4-6") for i in range(10))
    entries = [parse_entry(r) for r in raw]
    assert detect_escalation(entries) is None


def test_detect_escalation_returns_none_for_too_short_session() -> None:
    raw = [_user("u", _ts(0), "go"), _assistant("a", _ts(1))]
    entries = [parse_entry(r) for r in raw]
    assert detect_escalation(entries) is None


def test_detect_escalation_records_first_event_only() -> None:
    """Multiple escalations in one session — only the first is recorded."""
    raw = [_user("u", _ts(0), "go")]
    raw.extend(_assistant(f"a{i}", _ts(1 + i), model="claude-sonnet-4-6") for i in range(3))
    raw.append(_assistant("a-first-up", _ts(4), model="claude-opus-4-7"))
    raw.append(_assistant("a-down", _ts(5), model="claude-sonnet-4-6"))
    raw.append(_assistant("a-up-again", _ts(6), model="claude-opus-4-7"))
    entries = [parse_entry(r) for r in raw]
    event = detect_escalation(entries)
    assert event is not None
    assert event["turn_index"] == 3  # the 4th assistant turn (0-indexed)


# ---------- auto_mode tagging ----------


def test_detect_escalation_auto_mode_tags_when_three_switches_in_first_20() -> None:
    """Sonnet -> Opus -> Sonnet -> Opus all in first 20 turns →
    auto_mode label, first switch is recorded."""
    models = [
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
    ]
    raw = [_user("u", _ts(0), "go")]
    for i, m in enumerate(models):
        raw.append(_assistant(f"a{i}", _ts(1 + i), model=m))
    entries = [parse_entry(r) for r in raw]
    event = detect_escalation(entries)
    assert event is not None
    assert event["escalation_kind"] == "auto_mode"
    assert event["turn_index"] == 1
    assert event["from_model"] == "claude-sonnet-4-6"
    assert event["to_model"] == "claude-opus-4-7"


def test_detect_escalation_two_switches_is_not_auto_mode() -> None:
    """Two switches in first 20 turns → just model_switch, not
    auto_mode — must clear the >= 3 threshold."""
    raw = [_user("u", _ts(0), "go")]
    raw.append(_assistant("a0", _ts(1), model="claude-sonnet-4-6"))
    raw.append(_assistant("a1", _ts(2), model="claude-opus-4-7"))
    raw.append(_assistant("a2", _ts(3), model="claude-sonnet-4-6"))
    raw.extend(_assistant(f"a-pad{i}", _ts(4 + i), model="claude-sonnet-4-6") for i in range(5))
    entries = [parse_entry(r) for r in raw]
    event = detect_escalation(entries)
    assert event is not None
    assert event["escalation_kind"] == "model_switch"


# ---------- resolution markers ----------


def test_resolution_marker_user_thanks() -> None:
    raw = [_user("u-init", _ts(0), "go")]
    for i in range(3):
        raw.append(_assistant(f"a-pre{i}", _ts(1 + i), model="claude-sonnet-4-6"))
    raw.append(_assistant("a-up", _ts(5), model="claude-opus-4-7"))
    raw.append(_assistant("a-post", _ts(6), model="claude-opus-4-7"))
    raw.append(_user("u-end", _ts(7), "thanks, that worked!"))
    entries = [parse_entry(r) for r in raw]
    event = detect_escalation(entries)
    assert event is not None
    assert event["resolution_marker"] == "user_thanks"


def test_resolution_marker_session_end_when_no_followup() -> None:
    raw = [_user("u", _ts(0), "go")]
    raw.extend(_assistant(f"a-pre{i}", _ts(1 + i), model="claude-sonnet-4-6") for i in range(3))
    raw.append(_assistant("a-up", _ts(5), model="claude-opus-4-7"))
    entries = [parse_entry(r) for r in raw]
    event = detect_escalation(entries)
    assert event is not None
    # session_end | tool_success_no_followup are both reasonable outcomes
    # for a session that ends w/ an assistant turn and no further user
    # interaction. Either is acceptable; just must not be user_thanks.
    assert event["resolution_marker"] != "user_thanks"


def test_wall_clock_seconds_populated() -> None:
    raw = [_user("u", _ts(0), "go")]
    raw.extend(_assistant(f"a-pre{i}", _ts(1 + i), model="claude-sonnet-4-6") for i in range(3))
    raw.append(_assistant("a-up", _ts(20), model="claude-opus-4-7"))
    raw.append(_assistant("a-post", _ts(30), model="claude-opus-4-7"))
    entries = [parse_entry(r) for r in raw]
    event = detect_escalation(entries)
    assert event is not None
    assert event["wall_clock_before_seconds"] > 0
    assert event["wall_clock_after_seconds"] > 0


# ---------- subagent_dispatch escalation ----------


def _sonnet_session_with_spawn(spawn_at_turn: int = 2) -> list[Any]:
    """Build a 5-turn Sonnet parent session. The orchestrator-side
    code is responsible for resolving the spawn → parent_turn_index;
    in tests we pass it directly via ``SubagentSpawnInfo``."""
    raw = [_user("u", _ts(0), "go")]
    for i in range(5):
        raw.append(_assistant(f"a{i}", _ts(1 + i), model="claude-sonnet-4-6", cost_usd=0.10))
    return [parse_entry(r) for r in raw]


def test_subagent_escalation_fires_when_child_tier_higher() -> None:
    entries = _sonnet_session_with_spawn(spawn_at_turn=2)
    spawns = [
        SubagentSpawnInfo(
            parent_message_dedup_key="key1",
            child_session_cost_usd=0.40,
            child_primary_model="claude-opus-4-7",
            child_prompt_excerpt="Fix the auth bug; trace token expiry logic",
            parent_assistant_turn_index=2,
        )
    ]
    event = detect_subagent_escalation(entries, spawns)
    assert event is not None
    assert event["escalation_kind"] == "subagent_dispatch"
    assert event["from_model"] == "claude-sonnet-4-6"
    assert event["to_model"] == "claude-opus-4-7"
    assert event["turn_index"] == 2
    assert event["subagent_prompt_excerpt"].startswith("Fix the auth bug")
    assert event["cost_after_switch_usd"] >= 0.40


def test_subagent_escalation_skips_same_tier_dispatch() -> None:
    """Sonnet -> Sonnet code-reviewer subagent = capability routing,
    not escalation. Per spec §0.1 distinction."""
    entries = _sonnet_session_with_spawn()
    spawns = [
        SubagentSpawnInfo(
            parent_message_dedup_key="key1",
            child_session_cost_usd=0.20,
            child_primary_model="claude-sonnet-4-6",
            child_prompt_excerpt="Review PR",
            parent_assistant_turn_index=2,
        )
    ]
    assert detect_subagent_escalation(entries, spawns) is None


def test_subagent_escalation_skips_lower_tier_dispatch() -> None:
    """Sonnet parent dispatching to Haiku subagent = no escalation."""
    entries = _sonnet_session_with_spawn()
    spawns = [
        SubagentSpawnInfo(
            parent_message_dedup_key="key1",
            child_session_cost_usd=0.05,
            child_primary_model="claude-haiku-4-5",
            child_prompt_excerpt="Quick lookup",
            parent_assistant_turn_index=2,
        )
    ]
    assert detect_subagent_escalation(entries, spawns) is None


def test_subagent_escalation_picks_earliest_qualifying_spawn() -> None:
    entries = _sonnet_session_with_spawn()
    spawns = [
        SubagentSpawnInfo(
            parent_message_dedup_key="key2",
            child_session_cost_usd=0.30,
            child_primary_model="claude-opus-4-7",
            child_prompt_excerpt="Later spawn",
            parent_assistant_turn_index=4,
        ),
        SubagentSpawnInfo(
            parent_message_dedup_key="key1",
            child_session_cost_usd=0.10,
            child_primary_model="claude-opus-4-7",
            child_prompt_excerpt="Earlier spawn",
            parent_assistant_turn_index=1,
        ),
    ]
    event = detect_subagent_escalation(entries, spawns)
    assert event is not None
    assert event["turn_index"] == 1
    assert event["subagent_prompt_excerpt"] == "Earlier spawn"


def test_subagent_escalation_returns_none_when_no_spawns() -> None:
    entries = _sonnet_session_with_spawn()
    assert detect_subagent_escalation(entries, []) is None


# ---------- select_earliest_escalation ----------


def test_select_earliest_picks_smallest_turn_index() -> None:
    a = {"turn_index": 5, "escalation_kind": "model_switch"}
    b = {"turn_index": 2, "escalation_kind": "subagent_dispatch"}
    chosen = select_earliest_escalation([a, b])
    assert chosen is b


def test_select_earliest_handles_none_in_list() -> None:
    a = {"turn_index": 5, "escalation_kind": "model_switch"}
    chosen = select_earliest_escalation([None, a, None])
    assert chosen is a


def test_select_earliest_returns_none_when_all_none() -> None:
    assert select_earliest_escalation([None, None]) is None
