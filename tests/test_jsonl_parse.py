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


def test_oversized_line_is_skipped_with_warning(tmp_path: Path) -> None:
    """A pathologically long line (> 16 MB, no newline) is dropped with a
    warning — bounds memory under hostile input. Subsequent well-formed
    lines still parse.
    """
    from ccforensics.jsonl import _MAX_LINE_BYTES

    path = tmp_path / "oversize.jsonl"
    # 17 MB of junk on line 1 (no newline until the end), then one valid entry.
    junk = "x" * (_MAX_LINE_BYTES + 1024)
    valid = (
        '{"type":"user","uuid":"u1","sessionId":"s",'
        '"timestamp":"2026-04-22T10:00:00Z","isSidechain":false,"isMeta":false,'
        '"message":{"role":"user","content":"ok"}}'
    )
    path.write_text(junk + "\n" + valid + "\n")

    result = parse_file(path)
    # The oversize line was dropped.
    assert result.parse_errors == 1
    assert any("exceeded" in w and "skipped" in w for w in result.warnings)
    # The valid trailing entry still parsed.
    assert len(result.entries) == 1
    assert result.entries[0].uuid == "u1"
