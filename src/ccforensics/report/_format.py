from __future__ import annotations

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
