from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccforensics.jsonl import annotate_cost, parse_file

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def test_assistant_entries_get_cost(pricing_data: dict) -> None:
    result = parse_file(FIXTURES / "basic" / "s1.jsonl")
    annotated = annotate_cost(result.entries, pricing_data)
    assistant = [e for e in annotated if e.entry.type == "assistant"]
    assert len(assistant) == 2
    for e in assistant:
        assert e.cost_usd is not None
        assert e.cost_usd > 0


def test_user_entries_have_zero_cost(pricing_data: dict) -> None:
    result = parse_file(FIXTURES / "basic" / "s1.jsonl")
    annotated = annotate_cost(result.entries, pricing_data)
    user_entries = [e for e in annotated if e.entry.type == "user"]
    assert len(user_entries) > 0
    for e in user_entries:
        assert e.cost_usd == 0.0


def test_non_billable_types_have_zero_cost(pricing_data: dict) -> None:
    """Non-assistant types (system, attachment, ...) all get 0.0, never None."""
    from ccforensics.models import parse_entry

    raws = [
        {
            "type": "attachment",
            "timestamp": "2026-04-20T10:00:00Z",
            "attachment": {"type": "hook_success", "hookEvent": "SessionStart"},
        },
        {
            "type": "permission-mode",
            "timestamp": "2026-04-20T10:00:00Z",
            "sessionId": "s",
        },
    ]
    entries = [parse_entry(r) for r in raws]
    annotated = annotate_cost(entries, pricing_data)
    for a in annotated:
        assert a.cost_usd == 0.0, f"{a.entry.type} should have cost_usd=0.0, got {a.cost_usd}"


def test_missing_model_pricing_yields_none(pricing_data: dict) -> None:
    from ccforensics.models import parse_entry

    raw = {
        "type": "assistant",
        "uuid": "u",
        "sessionId": "s",
        "timestamp": "2026-04-20T10:00:00Z",
        "requestId": "r",
        "message": {
            "id": "m",
            "role": "assistant",
            "model": "imaginary-model-v99",
            "content": [],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        },
    }
    entry = parse_entry(raw)
    annotated = annotate_cost([entry], pricing_data)
    assert annotated[0].cost_usd is None
    assert annotated[0].pricing_unresolved_model == "imaginary-model-v99"


def test_cost_uses_ttl_split_when_usage_cache_creation_present(pricing_data: dict) -> None:
    """Transcripts emitted by Claude Code 2.1.108+ carry a
    ``usage.cache_creation`` sub-object splitting tokens by TTL. The
    annotator must price each bucket at its own rate — 1h tokens at the 2x
    rate, 5m tokens at the 1.25x rate. Identical input with all tokens
    routed to 1h must cost MORE than the same input routed to 5m, and the
    annotator's cost must match the per-bucket math exactly."""
    from ccforensics.models import parse_entry
    from ccforensics.pricing import resolve_pricing

    def make(cache_block: dict) -> dict:
        return {
            "type": "assistant",
            "uuid": "u",
            "sessionId": "s",
            "timestamp": "2026-04-20T10:00:00Z",
            "requestId": "r",
            "message": {
                "id": "m",
                "role": "assistant",
                "model": "claude-opus-4-5-20251101",
                "content": [],
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 10000,
                    "cache_read_input_tokens": 0,
                    "cache_creation": cache_block,
                },
            },
        }

    raw_all_5m = make({"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 10000})
    raw_all_1h = make({"ephemeral_1h_input_tokens": 10000, "ephemeral_5m_input_tokens": 0})

    annotated_5m = annotate_cost([parse_entry(raw_all_5m)], pricing_data)[0]
    annotated_1h = annotate_cost([parse_entry(raw_all_1h)], pricing_data)[0]

    price = resolve_pricing("claude-opus-4-5-20251101", pricing_data)
    assert price is not None

    assert annotated_5m.cost_usd == pytest.approx(10000 * price.cache_creation_cost)
    assert annotated_1h.cost_usd == pytest.approx(10000 * price.cache_creation_1h_cost)
    # The bug ccforensics was masking: 1h MUST cost more than 5m for the
    # same token count. Pre-v5 these produced identical costs.
    assert annotated_1h.cost_usd is not None
    assert annotated_5m.cost_usd is not None
    assert annotated_1h.cost_usd > annotated_5m.cost_usd


def test_cost_back_compat_when_usage_cache_creation_absent(pricing_data: dict) -> None:
    """Older transcripts without the ``usage.cache_creation`` block must keep
    the prior pricing semantics: legacy ``cache_creation_input_tokens`` total
    charged at the 5m rate. Verifies the back-compat fallback in
    ``compute_message_cost``."""
    from ccforensics.models import parse_entry
    from ccforensics.pricing import resolve_pricing

    raw = {
        "type": "assistant",
        "uuid": "u",
        "sessionId": "s",
        "timestamp": "2026-04-20T10:00:00Z",
        "requestId": "r",
        "message": {
            "id": "m",
            "role": "assistant",
            "model": "claude-opus-4-5-20251101",
            "content": [],
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 10000,
                "cache_read_input_tokens": 0,
                # No "cache_creation" sub-object — pre-2.1.108 shape.
            },
        },
    }
    annotated = annotate_cost([parse_entry(raw)], pricing_data)[0]
    price = resolve_pricing("claude-opus-4-5-20251101", pricing_data)
    assert price is not None
    # Legacy total charged at 5m rate (preserving prior semantics).
    assert annotated.cost_usd == pytest.approx(10000 * price.cache_creation_cost)
