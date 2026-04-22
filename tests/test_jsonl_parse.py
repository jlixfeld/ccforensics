from __future__ import annotations

from pathlib import Path

import pytest

from ccforensics.jsonl import parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_basic_session() -> None:
    result = parse_file(FIXTURES / "basic" / "s1.jsonl")
    assert len(result.entries) == 5
    assert result.warnings == []
    assert result.parse_errors == 0
    assert result.unknown_types == set()


def test_truncated_last_line_is_silently_skipped() -> None:
    result = parse_file(FIXTURES / "truncated" / "session.jsonl")
    assert len(result.entries) == 2
    assert result.parse_errors == 0
    assert result.truncated_tail is True


def test_unknown_type_recorded_not_crashed() -> None:
    result = parse_file(FIXTURES / "drift" / "session.jsonl")
    assert len(result.entries) == 4
    assert "future-unknown-type" in result.unknown_types
    assert "permission-mode" not in result.unknown_types
    assert len(result.warnings) >= 1


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_file(tmp_path / "nope.jsonl")
