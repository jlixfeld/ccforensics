"""Cache efficiency + savings — exact arithmetic over stored values.

Both metrics are derivations from already-stored token counts and per-model
pricing. No estimation, no banding.

- savings_usd = sum_over_rows( cache_read * (input_price - read_price) )
- eff_pct     = sum( cache_read * read_price )
              / sum( cache_read*read_price + cc_5m*create_5m_price
                     + cc_1h*create_1h_price + input*input_price )
              * 100

Cache-creation tokens split by TTL (5-minute vs 1-hour) when the transcript
carries ``usage.cache_creation.ephemeral_*_input_tokens``. When only the
legacy total is available (older transcripts), the full amount is treated
as 5m for back-compat with prior behavior.

Models without resolvable pricing are excluded from the calculation and
counted separately so callers can surface a "N rows excluded — unknown
model pricing" footer warning.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol


class _PricingProto(Protocol):
    @property
    def input_cost(self) -> float: ...

    @property
    def cache_creation_cost(self) -> float: ...

    @property
    def cache_creation_1h_cost(self) -> float: ...

    @property
    def cache_read_cost(self) -> float: ...


@dataclass(frozen=True)
class CacheRow:
    model: str
    input_tokens: int
    cache_creation: int
    cache_read: int
    # Per-TTL split. When both are 0 and ``cache_creation`` > 0, the row
    # came from an older transcript (no ``usage.cache_creation`` block) —
    # the full ``cache_creation`` total is charged at the 5m rate to
    # preserve pre-v5 semantics.
    cache_creation_1h: int = 0
    cache_creation_5m: int = 0


@dataclass(frozen=True)
class CacheMetrics:
    savings_usd: float
    eff_pct: float
    rows_excluded_for_unknown_model: int


def cache_metrics(
    rows: Iterable[CacheRow],
    resolve_pricing: Callable[[str], _PricingProto | None],
) -> CacheMetrics:
    savings = 0.0
    num = 0.0
    den = 0.0
    excluded = 0

    for row in rows:
        pricing = resolve_pricing(row.model)
        if pricing is None:
            excluded += 1
            continue
        cc_1h = row.cache_creation_1h
        cc_5m = row.cache_creation_5m
        # Back-compat: row from pre-v5 schema (or transcript without the
        # split block) — fall back to charging total at 5m rate.
        if cc_1h == 0 and cc_5m == 0 and row.cache_creation > 0:
            cc_5m = row.cache_creation
        savings += row.cache_read * (pricing.input_cost - pricing.cache_read_cost)
        num += row.cache_read * pricing.cache_read_cost
        den += (
            row.cache_read * pricing.cache_read_cost
            + cc_5m * pricing.cache_creation_cost
            + cc_1h * pricing.cache_creation_1h_cost
            + row.input_tokens * pricing.input_cost
        )

    eff = (num / den * 100.0) if den > 0 else 0.0
    return CacheMetrics(
        savings_usd=savings,
        eff_pct=eff,
        rows_excluded_for_unknown_model=excluded,
    )
