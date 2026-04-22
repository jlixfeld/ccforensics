from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ccforensics.pricing import (
    ModelPrice,
    PricingCache,
    compute_message_cost,
    fallback_hardcoded,
    resolve_pricing,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def test_resolves_exact_model_name(pricing_data: dict) -> None:
    p = resolve_pricing("claude-sonnet-4-5-20250929", pricing_data)
    assert p is not None
    assert p.input_cost > 0
    assert p.output_cost > 0


def test_resolves_alias_via_substring(pricing_data: dict) -> None:
    p = resolve_pricing("claude-sonnet-4-5", pricing_data)
    assert p is not None


def test_substring_fallback_emits_warning(
    pricing_data: dict, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.WARNING, logger="ccforensics.pricing")
    # Hypothetical future-version that isn't in any candidate (no
    # 'claude-{model}' / 'anthropic/{model}' match), so it falls through to
    # substring matching on something like the Bedrock alias.
    resolve_pricing("us.anthropic.claude-sonnet", pricing_data)
    assert any(
        "substring fallback" in rec.message and "us.anthropic.claude-sonnet" in rec.message
        for rec in caplog.records
    )


def test_exact_match_does_not_warn(pricing_data: dict, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.WARNING, logger="ccforensics.pricing")
    resolve_pricing("claude-sonnet-4-5-20250929", pricing_data)
    assert not any("substring fallback" in rec.message for rec in caplog.records)


def test_returns_none_for_unknown(pricing_data: dict) -> None:
    assert resolve_pricing("definitely-not-a-real-model", pricing_data) is None


def test_model_price_fills_cache_fields_from_ratio_when_missing() -> None:
    entry = {"input_cost_per_token": 0.000003, "output_cost_per_token": 0.000015}
    p = ModelPrice.from_entry(entry)
    assert p.input_cost == 0.000003
    assert p.output_cost == 0.000015
    assert p.cache_creation_cost == pytest.approx(0.00000075)
    assert p.cache_read_cost == pytest.approx(0.0000003)


def test_model_price_uses_explicit_cache_fields_when_present() -> None:
    entry = {
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
        "cache_creation_input_token_cost": 0.00000375,
        "cache_read_input_token_cost": 0.0000003,
    }
    p = ModelPrice.from_entry(entry)
    assert p.cache_creation_cost == 0.00000375
    assert p.cache_read_cost == 0.0000003


def test_compute_message_cost() -> None:
    p = ModelPrice(
        input_cost=0.000003,
        output_cost=0.000015,
        cache_creation_cost=0.00000375,
        cache_read_cost=0.0000003,
    )
    cost = compute_message_cost(
        price=p,
        input_tokens=1000,
        output_tokens=200,
        cache_creation=500,
        cache_read=10000,
    )
    expected = 0.003 + 0.003 + 0.001875 + 0.003
    assert cost == pytest.approx(expected)


def test_compute_message_cost_handles_none() -> None:
    p = ModelPrice(
        input_cost=1e-6, output_cost=1e-6, cache_creation_cost=1e-6, cache_read_cost=1e-6
    )
    assert compute_message_cost(p, None, None, None, None) == 0.0


def test_fallback_hardcoded_covers_current_claude_models() -> None:
    data = fallback_hardcoded()
    assert "claude-sonnet-4-5-20250929" in data
    assert "claude-haiku-4-5-20251001" in data
    for _key, entry in data.items():
        assert "input_cost_per_token" in entry
        assert "output_cost_per_token" in entry


def test_pricing_cache_reads_fresh_snapshot(tmp_path: Path, pricing_data: dict) -> None:
    cache_file = tmp_path / "litellm.json"
    cache_file.write_text(json.dumps({"fetched_at": 9999999999, "data": pricing_data}))
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=_unreachable_client())
    assert "claude-sonnet-4-5-20250929" in data


def test_pricing_cache_falls_back_to_stale_on_fetch_failure(
    tmp_path: Path, pricing_data: dict
) -> None:
    cache_file = tmp_path / "litellm.json"
    cache_file.write_text(json.dumps({"fetched_at": 0, "data": pricing_data}))
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=_failing_client())
    assert "claude-sonnet-4-5-20250929" in data


def test_pricing_cache_falls_back_to_hardcoded_when_empty(tmp_path: Path) -> None:
    cache_file = tmp_path / "litellm.json"
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=_failing_client())
    assert "claude-sonnet-4-5-20250929" in data


def _unreachable_client() -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected HTTP call")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _failing_client() -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated offline")

    return httpx.Client(transport=httpx.MockTransport(handler))
