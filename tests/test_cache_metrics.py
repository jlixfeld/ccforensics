"""Cache efficiency math — exact arithmetic over stored values."""

from __future__ import annotations

from dataclasses import dataclass

from ccforensics.report._cache import CacheMetrics, CacheRow, cache_metrics


@dataclass(frozen=True)
class FakePricing:
    input_cost: float
    cache_creation_cost: float
    cache_read_cost: float


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
        "known": FakePricing(
            input_cost=3e-6, cache_creation_cost=3.75e-6, cache_read_cost=0.3e-6
        )
    }
    m = cache_metrics(rows, _resolver(pricing))

    # Only the "known" row contributes:
    # savings = 2000 * (3e-6 - 0.3e-6) = 0.0054
    assert abs(m.savings_usd - 0.0054) < 1e-9
    assert m.rows_excluded_for_unknown_model == 1


def test_cache_metrics_empty_input() -> None:
    m = cache_metrics([], _resolver({}))
    assert m == CacheMetrics(savings_usd=0.0, eff_pct=0.0, rows_excluded_for_unknown_model=0)
