from __future__ import annotations

import csv
import io
import json

from syrupy.assertion import SnapshotAssertion

from ccforensics.export import _csv_cell, write_csv, write_json


def test_write_json_round_trip() -> None:
    buf = io.StringIO()
    payload = {"sessions": [{"id": "abc", "cost": 1.23}], "count": 1}
    write_json(payload, buf)
    assert json.loads(buf.getvalue()) == payload


def test_write_json_indent_is_two() -> None:
    buf = io.StringIO()
    write_json({"a": 1}, buf)
    # indent=2 produces a newline before "a" and 2-space indent.
    assert '{\n  "a": 1\n}' in buf.getvalue()


def test_write_json_trailing_newline() -> None:
    buf = io.StringIO()
    write_json({"a": 1}, buf)
    assert buf.getvalue().endswith("\n")


def test_write_json_preserves_insertion_order() -> None:
    buf = io.StringIO()
    # Keys intentionally out of alphabetical order.
    payload = {"zeta": 1, "alpha": 2, "mu": 3}
    write_json(payload, buf)
    text = buf.getvalue()
    assert text.index('"zeta"') < text.index('"alpha"') < text.index('"mu"')


def test_write_json_emoji_survives_as_utf8() -> None:
    buf = io.StringIO()
    payload = {"summary": "📎 /path/to/file"}
    write_json(payload, buf)
    text = buf.getvalue()
    # ensure_ascii=False: emoji appears literally, not as 📎.
    assert "📎" in text
    assert "\\u" not in text
    # And the UTF-8 byte sequence for 📎 (U+1F4CE) is F0 9F 93 8E.
    assert text.encode("utf-8").find(b"\xf0\x9f\x93\x8e") != -1


def test_write_json_empty_dict() -> None:
    buf = io.StringIO()
    write_json({}, buf)
    assert buf.getvalue() == "{}\n"


def test_write_json_empty_list() -> None:
    buf = io.StringIO()
    write_json([], buf)
    assert buf.getvalue() == "[]\n"


def test_write_csv_header_row_matches_headers_order() -> None:
    buf = io.StringIO()
    rows = [{"b": 1, "a": 2, "c": 3}]
    write_csv(rows, headers=["a", "b", "c"], out=buf)
    reader = csv.reader(io.StringIO(buf.getvalue()))
    header = next(reader)
    assert header == ["a", "b", "c"]


def test_write_csv_missing_key_is_empty_string() -> None:
    buf = io.StringIO()
    rows = [{"a": "x"}]  # missing "b"
    write_csv(rows, headers=["a", "b"], out=buf)
    parsed = list(csv.reader(io.StringIO(buf.getvalue())))
    assert parsed == [["a", "b"], ["x", ""]]


def test_write_csv_no_rows_still_emits_header() -> None:
    buf = io.StringIO()
    write_csv([], headers=["a", "b", "c"], out=buf)
    parsed = list(csv.reader(io.StringIO(buf.getvalue())))
    assert parsed == [["a", "b", "c"]]


def test_write_csv_round_trip() -> None:
    buf = io.StringIO()
    rows = [
        {"id": "s1", "cost": "1.23", "note": "hello"},
        {"id": "s2", "cost": "0.00", "note": "world"},
    ]
    write_csv(rows, headers=["id", "cost", "note"], out=buf)
    parsed = list(csv.DictReader(io.StringIO(buf.getvalue())))
    assert parsed == [
        {"id": "s1", "cost": "1.23", "note": "hello"},
        {"id": "s2", "cost": "0.00", "note": "world"},
    ]


def test_write_csv_quotes_commas() -> None:
    buf = io.StringIO()
    rows = [{"note": "hello, world"}]
    write_csv(rows, headers=["note"], out=buf)
    parsed = list(csv.reader(io.StringIO(buf.getvalue())))
    assert parsed == [["note"], ["hello, world"]]


def test_write_csv_quotes_newlines() -> None:
    buf = io.StringIO()
    rows = [{"note": "line1\nline2"}]
    write_csv(rows, headers=["note"], out=buf)
    parsed = list(csv.reader(io.StringIO(buf.getvalue())))
    assert parsed == [["note"], ["line1\nline2"]]


def test_write_csv_quotes_embedded_double_quotes() -> None:
    buf = io.StringIO()
    rows = [{"note": 'say "hi"'}]
    write_csv(rows, headers=["note"], out=buf)
    parsed = list(csv.reader(io.StringIO(buf.getvalue())))
    assert parsed == [["note"], ['say "hi"']]


def test_write_csv_extra_keys_are_ignored() -> None:
    buf = io.StringIO()
    rows = [{"a": 1, "b": 2, "extra": "dropped"}]
    write_csv(rows, headers=["a", "b"], out=buf)
    parsed = list(csv.reader(io.StringIO(buf.getvalue())))
    assert parsed == [["a", "b"], ["1", "2"]]


def test_csv_cell_none_is_empty_string() -> None:
    assert _csv_cell(None) == ""


def test_csv_cell_float() -> None:
    assert _csv_cell(1.23) == "1.23"


def test_csv_cell_int() -> None:
    assert _csv_cell(1) == "1"


def test_csv_cell_str() -> None:
    assert _csv_cell("hello") == "hello"


def test_csv_cell_bool() -> None:
    assert _csv_cell(True) == "true"
    assert _csv_cell(False) == "false"


def test_write_json_session_list_snapshot(snapshot: SnapshotAssertion) -> None:
    buf = io.StringIO()
    payload = {
        "sessions": [
            {
                "session_id": "abc123",
                "project": "ccforensics",
                "cost_usd": 1.23,
                "duration_s": 47,
                "summary": "📎 /path/to/attachment.pdf",
            },
            {
                "session_id": "def456",
                "project": "other",
                "cost_usd": None,
                "duration_s": 120,
                "summary": "refactor helper module",
            },
        ],
        "count": 2,
    }
    write_json(payload, buf)
    assert buf.getvalue() == snapshot
