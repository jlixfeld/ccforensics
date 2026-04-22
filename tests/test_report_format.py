from __future__ import annotations

from ccforensics.report._format import format_cost, format_duration


def test_format_duration_zero() -> None:
    assert format_duration(0) == "0s"


def test_format_duration_seconds_under_minute() -> None:
    assert format_duration(47) == "47s"


def test_format_duration_exactly_one_minute() -> None:
    assert format_duration(60) == "1m"


def test_format_duration_minutes_no_seconds_shown() -> None:
    assert format_duration(2820) == "47m"


def test_format_duration_hours_with_minutes() -> None:
    assert format_duration(15120) == "4h12m"


def test_format_duration_days_with_hours() -> None:
    assert format_duration(183600) == "2d3h"


def test_format_cost_none_is_em_dash() -> None:
    assert format_cost(None) == "$—"


def test_format_cost_zero() -> None:
    assert format_cost(0.0) == "$0.00"


def test_format_cost_rounds_to_cents() -> None:
    assert format_cost(1.234) == "$1.23"
