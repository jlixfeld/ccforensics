from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ccforensics.report._dates import parse_since, parse_until


def test_parse_since_iso_date() -> None:
    result = parse_since("2026-04-15")
    assert result == datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC)


def test_parse_until_iso_date() -> None:
    result = parse_until("2026-04-15")
    assert result == datetime(2026, 4, 15, 23, 59, 59, 999999, tzinfo=UTC)


def test_parse_since_n_days_relative() -> None:
    now = datetime(2026, 4, 22, 15, 30, tzinfo=UTC)
    result = parse_since("7d", now=now)
    assert result == datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC)


def test_parse_since_today() -> None:
    now = datetime(2026, 4, 22, 15, 30, tzinfo=UTC)
    result = parse_since("today", now=now)
    assert result == datetime(2026, 4, 22, 0, 0, 0, tzinfo=UTC)


def test_parse_since_yesterday() -> None:
    now = datetime(2026, 4, 22, 15, 30, tzinfo=UTC)
    result = parse_since("yesterday", now=now)
    assert result == datetime(2026, 4, 21, 0, 0, 0, tzinfo=UTC)


def test_parse_until_today() -> None:
    now = datetime(2026, 4, 22, 15, 30, tzinfo=UTC)
    result = parse_until("today", now=now)
    assert result == datetime(2026, 4, 22, 23, 59, 59, 999999, tzinfo=UTC)


def test_parse_since_bad_input_raises_value_error() -> None:
    with pytest.raises(ValueError, match="not-a-date"):
        parse_since("not-a-date")


def test_parse_until_bad_input_raises_value_error() -> None:
    with pytest.raises(ValueError, match="garbage"):
        parse_until("garbage")


def test_parse_since_rejects_non_padded_iso() -> None:
    with pytest.raises(ValueError, match="2026-4-15"):
        parse_since("2026-4-15")


def test_parse_since_rejects_garbage_iso_like() -> None:
    with pytest.raises(ValueError, match="2026-13-01"):
        parse_since("2026-13-01")


def test_parse_since_accepts_padded_iso() -> None:
    result = parse_since("2026-04-15")
    assert result == datetime(2026, 4, 15, 0, 0, 0, tzinfo=UTC)
