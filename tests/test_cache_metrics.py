"""Cache efficiency math — exact arithmetic over stored values."""

from __future__ import annotations

from dataclasses import dataclass

from ccforensics.report._cache import CacheMetrics, CacheRow, cache_metrics


@dataclass(frozen=True)
class FakePricing:
    input_cost: float
    cache_creation_cost: float
    cache_read_cost: float
    cache_creation_1h_cost: float = 0.0


def _resolver(table: dict[str, FakePricing]):
    def lookup(model: str) -> FakePricing | None:
        return table.get(model)

    return lookup


def test_cache_metrics_exact_savings_and_efficiency() -> None:
    rows = [
        CacheRow(
            model="claude-sonnet-4-5-20250929",
            input_tokens=1000,
            cache_creation=2000,
            cache_read=8000,
        )
    ]
    pricing = {
        "claude-sonnet-4-5-20250929": FakePricing(
            input_cost=3e-6,
            cache_creation_cost=3.75e-6,
            cache_read_cost=0.3e-6,
        )
    }
    m = cache_metrics(rows, _resolver(pricing))

    # savings = cache_read * (input - read) = 8000 * (3e-6 - 0.3e-6) = 0.0216
    assert abs(m.savings_usd - 0.0216) < 1e-9

    # eff_pct numerator = 8000 * 0.3e-6 = 0.0024
    # denom = 0.0024 + 2000*3.75e-6 + 1000*3e-6 = 0.0024 + 0.0075 + 0.003 = 0.0129
    # eff_pct = 0.0024 / 0.0129 * 100
    assert abs(m.eff_pct - (0.0024 / 0.0129 * 100)) < 1e-9

    assert m.rows_excluded_for_unknown_model == 0


def test_cache_metrics_zero_cache_read_returns_zero() -> None:
    rows = [
        CacheRow(
            model="claude-sonnet-4-5-20250929",
            input_tokens=1000,
            cache_creation=0,
            cache_read=0,
        )
    ]
    pricing = {
        "claude-sonnet-4-5-20250929": FakePricing(
            input_cost=3e-6, cache_creation_cost=3.75e-6, cache_read_cost=0.3e-6
        )
    }
    m = cache_metrics(rows, _resolver(pricing))
    assert m.savings_usd == 0.0
    assert m.eff_pct == 0.0


def test_cache_metrics_unknown_model_excluded_and_counted() -> None:
    rows = [
        CacheRow(model="known", input_tokens=1000, cache_creation=0, cache_read=2000),
        CacheRow(model="unknown-x", input_tokens=500, cache_creation=0, cache_read=500),
    ]
    pricing = {
        "known": FakePricing(input_cost=3e-6, cache_creation_cost=3.75e-6, cache_read_cost=0.3e-6)
    }
    m = cache_metrics(rows, _resolver(pricing))

    # Only the "known" row contributes:
    # savings = 2000 * (3e-6 - 0.3e-6) = 0.0054
    assert abs(m.savings_usd - 0.0054) < 1e-9
    assert m.rows_excluded_for_unknown_model == 1


def test_cache_metrics_empty_input() -> None:
    m = cache_metrics([], _resolver({}))
    assert m == CacheMetrics(savings_usd=0.0, eff_pct=0.0, rows_excluded_for_unknown_model=0)


def test_cache_metrics_1h_split_uses_1h_rate() -> None:
    """When the row carries the per-TTL split, 1h tokens price at the 1h rate
    and 5m tokens price at the 5m rate. Verifies the eff_pct denominator
    correctly mixes both rates."""
    rows = [
        CacheRow(
            model="opus",
            input_tokens=1000,
            cache_creation=3000,  # total = 1h + 5m
            cache_read=4000,
            cache_creation_1h=2000,
            cache_creation_5m=1000,
        )
    ]
    pricing = {
        "opus": FakePricing(
            input_cost=5e-6,
            cache_creation_cost=6.25e-6,
            cache_creation_1h_cost=10e-6,
            cache_read_cost=0.5e-6,
        )
    }
    m = cache_metrics(rows, _resolver(pricing))

    # savings = cache_read * (input - read) = 4000 * (5e-6 - 0.5e-6) = 0.018
    assert abs(m.savings_usd - 0.018) < 1e-9

    # num = 4000 * 0.5e-6 = 0.002
    # den = 0.002 + 1000*6.25e-6 + 2000*10e-6 + 1000*5e-6
    #     = 0.002 + 0.00625 + 0.02 + 0.005 = 0.03325
    expected_eff = 0.002 / 0.03325 * 100
    assert abs(m.eff_pct - expected_eff) < 1e-9


def test_cache_metrics_legacy_total_falls_back_to_5m_rate() -> None:
    """Pre-v5 rows (no split, only total) charge entire ``cache_creation``
    at the 5m rate — preserves prior cost semantics."""
    rows = [
        CacheRow(
            model="opus",
            input_tokens=1000,
            cache_creation=3000,
            cache_read=4000,
            # No split provided (defaults to 0/0)
        )
    ]
    pricing = {
        "opus": FakePricing(
            input_cost=5e-6,
            cache_creation_cost=6.25e-6,
            cache_creation_1h_cost=10e-6,
            cache_read_cost=0.5e-6,
        )
    }
    m = cache_metrics(rows, _resolver(pricing))

    # All 3000 tokens charged at 5m rate (no 1h contribution).
    # den = 4000*0.5e-6 + 3000*6.25e-6 + 0 + 1000*5e-6
    #     = 0.002 + 0.01875 + 0.005 = 0.02575
    expected_eff = 0.002 / 0.02575 * 100
    assert abs(m.eff_pct - expected_eff) < 1e-9


def test_cache_metrics_1h_split_costs_more_than_legacy_total() -> None:
    """Sanity: same total cache-creation tokens, but split into 1h, MUST
    produce a higher denominator than treating them all as 5m. This is the
    bug ccforensics was masking before v5."""
    pricing = {
        "opus": FakePricing(
            input_cost=5e-6,
            cache_creation_cost=6.25e-6,
            cache_creation_1h_cost=10e-6,
            cache_read_cost=0.5e-6,
        )
    }
    legacy_row = CacheRow(
        model="opus",
        input_tokens=0,
        cache_creation=10000,
        cache_read=0,
    )
    split_row = CacheRow(
        model="opus",
        input_tokens=0,
        cache_creation=10000,
        cache_read=0,
        cache_creation_1h=10000,
        cache_creation_5m=0,
    )
    legacy = cache_metrics([legacy_row], _resolver(pricing))
    split = cache_metrics([split_row], _resolver(pricing))
    # Same cache_read → identical savings, but eff_pct denominators differ.
    # With cache_read=0, eff_pct is 0 both sides — use a manual recompute
    # by adding a read row to anchor the numerator.
    anchor = CacheRow(model="opus", input_tokens=0, cache_creation=0, cache_read=1000)
    legacy2 = cache_metrics([legacy_row, anchor], _resolver(pricing))
    split2 = cache_metrics([split_row, anchor], _resolver(pricing))
    # 1h-charged denominator is larger → eff_pct is smaller.
    assert split2.eff_pct < legacy2.eff_pct
    assert split.savings_usd == legacy.savings_usd == 0.0
