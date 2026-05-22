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
    # Wrapped/namespaced model whose tail IS an exact LiteLLM key — substring
    # fallback (kl-in-lowered direction) must resolve it and emit a warning.
    resolve_pricing("custom-wrap/claude-sonnet-4-5-20250929", pricing_data)
    assert any(
        "substring fallback" in rec.message
        and "custom-wrap/claude-sonnet-4-5-20250929" in rec.message
        for rec in caplog.records
    )


def test_exact_match_does_not_warn(pricing_data: dict, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.WARNING, logger="ccforensics.pricing")
    resolve_pricing("claude-sonnet-4-5-20250929", pricing_data)
    assert not any("substring fallback" in rec.message for rec in caplog.records)


def test_returns_none_for_unknown(pricing_data: dict) -> None:
    assert resolve_pricing("definitely-not-a-real-model", pricing_data) is None


def test_short_alias_prefix_returns_none_not_arbitrary_variant(pricing_data: dict) -> None:
    """A short bedrock/vertex prefix like ``us.anthropic.claude-sonnet`` must
    NOT silently resolve to one of several specific variants (4-5, 4-6, …)
    with different prices. Returning None forces the caller to provide a
    fully-qualified model name — the correct signal for 'unknown pricing'."""
    assert resolve_pricing("us.anthropic.claude-sonnet", pricing_data) is None


def test_substring_fallback_picks_longest_match(pricing_data: dict) -> None:
    """When multiple LiteLLM keys are substrings of the model name, the
    LONGEST (most specific) key must win — deterministic across runs."""
    # Both ``claude-sonnet-4-5`` and ``claude-sonnet-4-5-20250929`` exist as
    # keys with (possibly) different prices. A wrapper name containing both
    # must pick the longer, date-suffixed key.
    p = resolve_pricing("wrap/claude-sonnet-4-5-20250929/v1", pricing_data)
    assert p is not None
    # The long key's price matches the exact-entry price.
    expected = resolve_pricing("claude-sonnet-4-5-20250929", pricing_data)
    assert expected is not None
    assert p.input_cost == expected.input_cost
    assert p.output_cost == expected.output_cost


def test_model_price_fills_cache_fields_from_ratio_when_missing() -> None:
    entry = {"input_cost_per_token": 0.000003, "output_cost_per_token": 0.000015}
    p = ModelPrice.from_entry(entry)
    assert p.input_cost == 0.000003
    assert p.output_cost == 0.000015
    assert p.cache_creation_cost == pytest.approx(0.00000075)
    # 1h fallback: 2.0x input.
    assert p.cache_creation_1h_cost == pytest.approx(0.000006)
    assert p.cache_read_cost == pytest.approx(0.0000003)


def test_model_price_uses_explicit_cache_fields_when_present() -> None:
    entry = {
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
        "cache_creation_input_token_cost": 0.00000375,
        "cache_creation_input_token_cost_above_1hr": 0.000006,
        "cache_read_input_token_cost": 0.0000003,
    }
    p = ModelPrice.from_entry(entry)
    assert p.cache_creation_cost == 0.00000375
    assert p.cache_creation_1h_cost == 0.000006
    assert p.cache_read_cost == 0.0000003


def test_model_price_1h_falls_back_when_only_5m_present() -> None:
    """LiteLLM entries for models like sonnet-4.6 carry the 5m rate but not
    the 1h field — fallback is 2.0x input, NOT the 5m rate."""
    entry = {
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
        "cache_creation_input_token_cost": 0.00000375,
        "cache_read_input_token_cost": 0.0000003,
    }
    p = ModelPrice.from_entry(entry)
    assert p.cache_creation_cost == 0.00000375
    # 1h synthesized = input * 2.0 = 6e-6 (NOT the 5m rate of 3.75e-6)
    assert p.cache_creation_1h_cost == pytest.approx(0.000006)


def test_compute_message_cost() -> None:
    p = ModelPrice(
        input_cost=0.000003,
        output_cost=0.000015,
        cache_creation_cost=0.00000375,
        cache_creation_1h_cost=0.000006,
        cache_read_cost=0.0000003,
    )
    cost = compute_message_cost(
        price=p,
        input_tokens=1000,
        output_tokens=200,
        cache_creation=500,
        cache_read=10000,
    )
    # Legacy total-only path: all 500 cache_creation charged at 5m rate.
    expected = 0.003 + 0.003 + 0.001875 + 0.003
    assert cost == pytest.approx(expected)


def test_compute_message_cost_with_ttl_split() -> None:
    """When ``cache_creation_1h`` / ``cache_creation_5m`` provided, each
    bucket prices at its own rate; legacy ``cache_creation`` total is ignored."""
    p = ModelPrice(
        input_cost=0.000005,
        output_cost=0.000025,
        cache_creation_cost=0.00000625,
        cache_creation_1h_cost=0.00001,
        cache_read_cost=0.0000005,
    )
    cost = compute_message_cost(
        price=p,
        input_tokens=0,
        output_tokens=0,
        cache_creation=10000,  # ignored when split provided
        cache_read=0,
        cache_creation_1h=8000,
        cache_creation_5m=2000,
    )
    expected = 2000 * 0.00000625 + 8000 * 0.00001
    assert cost == pytest.approx(expected)
    # And it must NOT equal what the legacy path would have charged.
    legacy = compute_message_cost(
        price=p,
        input_tokens=0,
        output_tokens=0,
        cache_creation=10000,
        cache_read=0,
    )
    assert legacy != pytest.approx(expected)
    assert legacy < expected  # legacy under-counts


def test_compute_message_cost_split_with_only_5m() -> None:
    """Split where only 5m is present (1h=None) must NOT fall back to legacy
    total — that path is reached only when BOTH split values are None."""
    p = ModelPrice(
        input_cost=1e-6,
        output_cost=1e-6,
        cache_creation_cost=1.25e-6,
        cache_creation_1h_cost=2e-6,
        cache_read_cost=0.1e-6,
    )
    cost = compute_message_cost(
        price=p,
        input_tokens=0,
        output_tokens=0,
        cache_creation=9999,  # ignored when ANY split arg is non-None
        cache_read=0,
        cache_creation_5m=1000,
    )
    assert cost == pytest.approx(1000 * 1.25e-6)


def test_compute_message_cost_handles_none() -> None:
    p = ModelPrice(
        input_cost=1e-6,
        output_cost=1e-6,
        cache_creation_cost=1e-6,
        cache_creation_1h_cost=1e-6,
        cache_read_cost=1e-6,
    )
    assert compute_message_cost(p, None, None, None, None) == 0.0


def test_fallback_hardcoded_covers_current_claude_models() -> None:
    data = fallback_hardcoded()
    assert "claude-sonnet-4-5-20250929" in data
    assert "claude-haiku-4-5-20251001" in data
    assert "claude-opus-4-6" in data
    assert "claude-opus-4-7" in data
    assert "claude-sonnet-4-6" in data
    for _key, entry in data.items():
        assert "input_cost_per_token" in entry
        assert "output_cost_per_token" in entry


def test_fallback_hardcoded_includes_1h_rates_for_4x_models() -> None:
    """4.x models (opus/sonnet/haiku) all support 1h cache TTL; fallback must
    carry the explicit 1h rate so cost math doesn't synthesize via the 2.0x
    multiplier (which is the correct default but not what Anthropic actually
    charges for these models — they ARE at 2.0x, but pinning is more robust).
    """
    data = fallback_hardcoded()
    for key in (
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-5-20251101",
        "claude-opus-4-1-20250805",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-20250514",
        "claude-haiku-4-5-20251001",
    ):
        entry = data[key]
        assert "cache_creation_input_token_cost_above_1hr" in entry, key
        # Sanity: 1h rate must be 2.0x input.
        assert entry["cache_creation_input_token_cost_above_1hr"] == pytest.approx(
            entry["input_cost_per_token"] * 2.0
        ), key


def test_pricing_cache_reads_fresh_snapshot(tmp_path: Path, pricing_data: dict) -> None:
    cache_file = tmp_path / "litellm.json"
    cache_file.write_text(json.dumps({"fetched_at": 9999999999, "data": pricing_data}))
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=_unreachable_client())
    assert "claude-sonnet-4-5-20250929" in data
    assert cache.last_source == "cached"


def test_pricing_cache_falls_back_to_stale_on_fetch_failure(
    tmp_path: Path, pricing_data: dict
) -> None:
    cache_file = tmp_path / "litellm.json"
    cache_file.write_text(json.dumps({"fetched_at": 0, "data": pricing_data}))
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=_failing_client())
    assert "claude-sonnet-4-5-20250929" in data
    assert cache.last_source == "stale"


def test_pricing_cache_falls_back_to_hardcoded_when_empty(tmp_path: Path) -> None:
    cache_file = tmp_path / "litellm.json"
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=_failing_client())
    assert "claude-sonnet-4-5-20250929" in data
    assert cache.last_source == "fallback"


def test_pricing_cache_corrupt_json_refetches(tmp_path: Path, pricing_data: dict) -> None:
    """Malformed cache JSON must not crash — fall through to refetch."""
    cache_file = tmp_path / "litellm.json"
    cache_file.write_text("{not valid json")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pricing_data)

    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert cache.last_source == "fresh"
    assert "claude-sonnet-4-5-20250929" in data


def test_pricing_cache_missing_data_key_refetches(tmp_path: Path, pricing_data: dict) -> None:
    """Cache wrapper with no ``data`` key must refetch, not return empty dict."""
    cache_file = tmp_path / "litellm.json"
    cache_file.write_text(json.dumps({"fetched_at": 9999999999}))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pricing_data)

    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert cache.last_source == "fresh"
    assert "claude-sonnet-4-5-20250929" in data


def test_pricing_cache_string_fetched_at_falls_through(tmp_path: Path) -> None:
    """``fetched_at`` that can't be coerced to int must trigger refetch path."""
    cache_file = tmp_path / "litellm.json"
    cache_file.write_text(json.dumps({"fetched_at": "not-a-number", "data": {"claude": {}}}))
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=_failing_client())
    # Reader raised on int() → outer except → fallback chain.
    assert cache.last_source == "fallback"
    assert "claude-sonnet-4-5-20250929" in data


def test_pricing_cache_aborts_on_oversize_response(tmp_path: Path) -> None:
    """A misbehaving CDN returning gigabytes must not OOM the indexer.

    Simulates a huge body; the fetcher must raise before buffering it all
    and fall back cleanly (via the outer except path) to the hardcoded table.
    """
    oversize = b"x" * (11 * 1024 * 1024)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversize)

    cache_file = tmp_path / "litellm.json"
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    # Falls back because the fetch aborts at the size cap.
    assert cache.last_source == "fallback"
    assert "claude-sonnet-4-5-20250929" in data


def test_pricing_cache_fresh_fetch_marks_source(tmp_path: Path, pricing_data: dict) -> None:
    """A successful network fetch sets last_source='fresh'."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pricing_data)

    cache_file = tmp_path / "litellm.json"
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    cache.load_or_fetch(http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert cache.last_source == "fresh"


def _unreachable_client() -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected HTTP call")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _failing_client() -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated offline")

    return httpx.Client(transport=httpx.MockTransport(handler))
