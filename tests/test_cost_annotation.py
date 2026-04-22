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
    for e in user_entries:
        assert e.cost_usd == 0.0 or e.cost_usd is None


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
