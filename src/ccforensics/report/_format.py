from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text

_EM_DASH = "—"


def format_duration(seconds: float) -> str:
    """Format a non-negative second count as a compact human string.

    Accepts ``int`` or ``float``. Sub-second precision is truncated via
    ``int()`` before formatting.
    """
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, _s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, m = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{m:02d}m" if m else f"{hours}h"
    days, h = divmod(hours, 24)
    return f"{days}d{h}h" if h else f"{days}d"


def format_cost(cost_usd: float | None) -> str:
    """Format a USD cost. ``None`` renders as ``$—`` (em dash)."""
    if cost_usd is None:
        return f"${_EM_DASH}"
    return f"${cost_usd:.2f}"


def human_count(n: int) -> str:
    """Compact integer formatting for the cache footer line.

    Token totals routinely run into the millions; the full grouped form
    crowds the line. Single decimal in K/M is plenty of precision for a
    summary.
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def render_cache_line(
    read_tokens: int,
    creation_tokens: int,
    eff_pct: float,
    savings_usd: float,
    excluded_unknown_models: int,
) -> RenderableType | None:
    """Footer-style cache line shared by ``session show`` and ``aggregate``.

    Returns ``None`` when the scope had no cache activity at all — clean
    signal that caching wasn't in play vs. a misleading ``0.0% efficiency``
    message.
    """
    if read_tokens == 0 and creation_tokens == 0:
        return None
    eff_str = f"{eff_pct:.1f}%" if eff_pct else "—"
    line = (
        f"Cache: {human_count(read_tokens)} read · "
        f"{human_count(creation_tokens)} created · "
        f"{eff_str} efficiency · saved ${savings_usd:.2f}"
    )
    if excluded_unknown_models:
        line += f"  (excluded {excluded_unknown_models} model(s) with no resolvable pricing)"
    return Text(line, style="dim")


def render_service_tier_line(breakdown: dict[str, int]) -> RenderableType | None:
    """One-line service-tier breakdown shared by ``session show`` and
    ``aggregate``. Only emits when something non-standard appears —
    ``standard`` and ``unknown`` are the boring case and surfacing them on
    every report would be noise. Anything else (priority, batch) is worth
    flagging because pricing isn't tier-aware yet."""
    non_standard = any(t not in ("standard", "unknown") for t in breakdown)
    if not non_standard:
        return None
    parts = [f"{t} {c:,} msgs" for t, c in sorted(breakdown.items())]
    return Text(
        f"Service tiers: {' · '.join(parts)}  (non-standard pricing not yet applied)",
        style="dim",
    )
