from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ccforensics.index import ensure_schema, open_connection
from ccforensics.report.session_list import (
    SessionListRow,
    query_session_list,
    render_session_list,
    shorten_session_ids,
)

# ---------- DB fixture helpers ----------


def _insert_summary(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    project_path: str | None = "/home/test/proj",
    project_display: str | None = "proj",
    started_at: int = 1_700_000_000,
    last_active_at: int = 1_700_000_060,
    duration_s: int = 60,
    turn_count: int = 1,
    total_cost_usd: float | None = 1.0,
    summary_text: str | None = "hello world",
    summary_source: str | None = "first-prompt",
) -> None:
    conn.execute(
        """INSERT INTO session_summaries (
            session_id, project_path, project_display,
            started_at, last_active_at, duration_s,
            turn_count, total_cost_usd,
            summary_text, summary_source
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            session_id,
            project_path,
            project_display,
            started_at,
            last_active_at,
            duration_s,
            turn_count,
            total_cost_usd,
            summary_text,
            summary_source,
        ),
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "index.sqlite"
    c = open_connection(db)
    ensure_schema(c)
    return c


# ---------- query_session_list: ordering ----------


def test_query_default_no_filters_sorted_by_last_active_desc(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", last_active_at=100)
    _insert_summary(conn, session_id="sB", last_active_at=300)
    _insert_summary(conn, session_id="sC", last_active_at=200)
    conn.commit()

    rows = query_session_list(conn)
    assert [r.session_id for r in rows] == ["sB", "sC", "sA"]


def test_query_sort_by_cost_desc_nulls_last(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", total_cost_usd=1.0)
    _insert_summary(conn, session_id="sB", total_cost_usd=None)
    _insert_summary(conn, session_id="sC", total_cost_usd=5.0)
    conn.commit()

    rows = query_session_list(conn, sort_key="cost")
    assert [r.session_id for r in rows] == ["sC", "sA", "sB"]


def test_query_sort_by_started_desc(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", started_at=100)
    _insert_summary(conn, session_id="sB", started_at=300)
    _insert_summary(conn, session_id="sC", started_at=200)
    conn.commit()

    rows = query_session_list(conn, sort_key="started")
    assert [r.session_id for r in rows] == ["sB", "sC", "sA"]


def test_query_sort_by_turns_desc(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", turn_count=2)
    _insert_summary(conn, session_id="sB", turn_count=10)
    _insert_summary(conn, session_id="sC", turn_count=5)
    conn.commit()

    rows = query_session_list(conn, sort_key="turns")
    assert [r.session_id for r in rows] == ["sB", "sC", "sA"]


def test_query_sort_by_compact_desc(conn: sqlite3.Connection) -> None:
    """``--sort compact`` ranks sessions by compaction count, most-compacted
    first. Tied counts fall back to the standard last-active tiebreak."""
    _insert_summary(conn, session_id="s-zero", last_active_at=100)
    _insert_summary(conn, session_id="s-three", last_active_at=200)
    _insert_summary(conn, session_id="s-one", last_active_at=300)
    # 3 auto-compact files for s-three, 1 for s-one, 0 for s-zero.
    rows_to_insert = [
        ("/fake/s-three-a.jsonl", "auto-compact", "s-three"),
        ("/fake/s-three-b.jsonl", "auto-compact", "s-three"),
        ("/fake/s-three-c.jsonl", "auto-compact", "s-three"),
        ("/fake/s-one-a.jsonl", "auto-compact", "s-one"),
    ]
    for path, kind, sid in rows_to_insert:
        conn.execute(
            "INSERT INTO files (path, mtime_ns, size, session_id, kind, agent_id, "
            "schema_version, parse_warnings, last_parsed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (path, 0, 0, sid, kind, None, None, 0, 0),
        )
    conn.commit()

    rows = query_session_list(conn, sort_key="compact")
    assert [r.session_id for r in rows] == ["s-three", "s-one", "s-zero"]

    # --reverse flips to ascending; ties still break by last_active_at DESC.
    rows = query_session_list(conn, sort_key="compact", reverse=True)
    assert [r.session_id for r in rows] == ["s-zero", "s-one", "s-three"]


def test_query_reverse_flips_order(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", last_active_at=100)
    _insert_summary(conn, session_id="sB", last_active_at=300)
    _insert_summary(conn, session_id="sC", last_active_at=200)
    conn.commit()

    rows = query_session_list(conn, reverse=True)
    assert [r.session_id for r in rows] == ["sA", "sC", "sB"]


def test_query_reverse_keeps_nulls_last(conn: sqlite3.Connection) -> None:
    """NULL-valued sort column stays at the end even under --reverse.

    Rationale: `--sort cost --reverse` means "cheapest first". Sessions with
    unresolved pricing (cost IS NULL) aren't "cheaper than cheapest" — they're
    unknown, and belong at the bottom in both directions.
    """
    _insert_summary(conn, session_id="s-hi", total_cost_usd=5.0, last_active_at=300)
    _insert_summary(conn, session_id="s-null", total_cost_usd=None, last_active_at=200)
    _insert_summary(conn, session_id="s-lo", total_cost_usd=1.0, last_active_at=100)
    conn.commit()

    # Default (DESC): s-hi, s-lo, s-null
    rows = query_session_list(conn, sort_key="cost")
    assert [r.session_id for r in rows] == ["s-hi", "s-lo", "s-null"]

    # Reversed (ASC): s-lo, s-hi, s-null  — NULL still last
    rows = query_session_list(conn, sort_key="cost", reverse=True)
    assert [r.session_id for r in rows] == ["s-lo", "s-hi", "s-null"]


def test_query_ties_break_by_last_active_desc_then_session_id(
    conn: sqlite3.Connection,
) -> None:
    """Within a sort-column tie, rows must come back most-recently-active first,
    not alphabetically by session_id.

    Motivation: on real corpora, ``--sort cost --reverse --limit N`` often has
    thousands of $0-cost ties (bulk ingestion sessions). Tiebreaking by
    session_id alphabetically made ``--limit`` return meaningless rows — the
    first-alphabetically 10 $0 sessions rather than anything useful.
    Tiebreaking by ``last_active_at DESC`` surfaces the most recent entries in
    the tie-pool, which is what a user skimming the top-N actually wants.
    """
    # Three $0 sessions; session_id alphabetical order is opposite to
    # last_active_at order so we can distinguish the tiebreakers.
    _insert_summary(conn, session_id="aaa", total_cost_usd=0.0, last_active_at=100)
    _insert_summary(conn, session_id="bbb", total_cost_usd=0.0, last_active_at=300)
    _insert_summary(conn, session_id="ccc", total_cost_usd=0.0, last_active_at=200)
    conn.commit()

    rows = query_session_list(conn, sort_key="cost", reverse=True)
    assert [r.session_id for r in rows] == ["bbb", "ccc", "aaa"]


def test_query_same_last_active_falls_back_to_session_id(
    conn: sqlite3.Connection,
) -> None:
    """When both the primary key and last_active_at tie, session_id keeps
    the ordering deterministic so --limit and JSON output are stable."""
    _insert_summary(conn, session_id="s-c", total_cost_usd=0.0, last_active_at=100)
    _insert_summary(conn, session_id="s-a", total_cost_usd=0.0, last_active_at=100)
    _insert_summary(conn, session_id="s-b", total_cost_usd=0.0, last_active_at=100)
    conn.commit()

    rows = query_session_list(conn, sort_key="cost")
    assert [r.session_id for r in rows] == ["s-a", "s-b", "s-c"]


def test_query_limit_caps_rows(conn: sqlite3.Connection) -> None:
    for i in range(5):
        _insert_summary(conn, session_id=f"s{i}", last_active_at=100 + i)
    conn.commit()

    rows = query_session_list(conn, limit=2)
    assert len(rows) == 2


# ---------- query_session_list: filters ----------


def test_query_filter_project_case_insensitive_like(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", project_path="/home/me/ccforensics")
    _insert_summary(conn, session_id="sB", project_path="/home/me/other")
    _insert_summary(conn, session_id="sC", project_path="/var/ccFORENSICS-clone")
    conn.commit()

    rows = query_session_list(conn, project="ccforensics")
    assert sorted(r.session_id for r in rows) == ["sA", "sC"]


def test_query_filter_project_handles_null_project_path(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", project_path=None)
    _insert_summary(conn, session_id="sB", project_path="/home/me/x")
    conn.commit()

    rows = query_session_list(conn, project="x")
    assert [r.session_id for r in rows] == ["sB"]


def test_query_filter_grep_case_insensitive(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", summary_text="Refactor the Parser")
    _insert_summary(conn, session_id="sB", summary_text="unrelated work")
    _insert_summary(conn, session_id="sC", summary_text="parser bug fix")
    conn.commit()

    rows = query_session_list(conn, grep="parser")
    assert sorted(r.session_id for r in rows) == ["sA", "sC"]


def test_query_filter_grep_handles_null_summary(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", summary_text=None)
    _insert_summary(conn, session_id="sB", summary_text="hello world")
    conn.commit()

    rows = query_session_list(conn, grep="hello")
    assert [r.session_id for r in rows] == ["sB"]


def _insert_file_and_message(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    model: str | None,
    dedup_key_suffix: str = "",
) -> None:
    """Minimal messages-table seed so ``--model`` filter has data to match.

    Writes a ``files`` row first (FK requirement) then a single assistant
    message with the given model.
    """
    path = f"/fake/{session_id}{dedup_key_suffix}.jsonl"
    conn.execute(
        """INSERT OR IGNORE INTO files (path, mtime_ns, size, session_id, kind,
               agent_id, schema_version, parse_warnings, last_parsed_at)
           VALUES (?, 0, 0, ?, 'main', NULL, NULL, 0, 0)""",
        (path, session_id),
    )
    conn.execute(
        """INSERT INTO messages (dedup_key, file_path, session_id, role, type,
               model, ts, is_sidechain, is_meta)
           VALUES (?, ?, ?, 'assistant', 'assistant', ?, 0, 0, 0)""",
        (f"k-{session_id}-{dedup_key_suffix or 'a'}", path, session_id, model),
    )


def test_query_filter_model_case_insensitive_substring(conn: sqlite3.Connection) -> None:
    """``--model opus`` returns sessions that had at least one message whose
    model matches the substring (case-insensitive). Mirrors --project shape."""
    _insert_summary(conn, session_id="s-opus")
    _insert_file_and_message(conn, session_id="s-opus", model="claude-opus-4-7")
    _insert_summary(conn, session_id="s-sonnet")
    _insert_file_and_message(conn, session_id="s-sonnet", model="claude-sonnet-4-5-20250929")
    _insert_summary(conn, session_id="s-mixed")
    _insert_file_and_message(conn, session_id="s-mixed", model="claude-opus-4-7")
    _insert_file_and_message(
        conn, session_id="s-mixed", model="claude-sonnet-4-5-20250929", dedup_key_suffix="b"
    )
    conn.commit()

    rows = query_session_list(conn, model="opus")
    assert sorted(r.session_id for r in rows) == ["s-mixed", "s-opus"]

    rows = query_session_list(conn, model="OPUS")
    assert sorted(r.session_id for r in rows) == ["s-mixed", "s-opus"]


def test_query_filter_model_dot_form_matches_dash_in_model(conn: sqlite3.Connection) -> None:
    """User-friendly ``opus-4.7`` (with a dot) matches the literal on-disk
    ``claude-opus-4-7`` (with a dash) — the filter substitutes ``.`` → ``-``
    before building the LIKE pattern."""
    _insert_summary(conn, session_id="s-opus")
    _insert_file_and_message(conn, session_id="s-opus", model="claude-opus-4-7")
    conn.commit()

    rows = query_session_list(conn, model="opus-4.7")
    assert [r.session_id for r in rows] == ["s-opus"]


def test_query_filter_model_ignores_null_model_rows(conn: sqlite3.Connection) -> None:
    """Sessions whose only messages have ``model IS NULL`` (infra rows —
    queue-operation, progress, etc.) must not match any model filter."""
    _insert_summary(conn, session_id="s-infra-only")
    _insert_file_and_message(conn, session_id="s-infra-only", model=None)
    conn.commit()

    rows = query_session_list(conn, model="opus")
    assert rows == []


def test_query_filter_model_ignores_synthetic_placeholder(conn: sqlite3.Connection) -> None:
    """A session whose only non-NULL model is Claude Code's ``<synthetic>``
    placeholder must not match a model filter — those aren't real models."""
    _insert_summary(conn, session_id="s-synth-only")
    _insert_file_and_message(conn, session_id="s-synth-only", model="<synthetic>")
    conn.commit()

    # Substring 'synth' matches the literal placeholder — if the filter
    # didn't exclude ``<...>`` it would wrongly return this session.
    rows = query_session_list(conn, model="synth")
    assert rows == []


def test_query_filter_since_until(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", last_active_at=100)
    _insert_summary(conn, session_id="sB", last_active_at=200)
    _insert_summary(conn, session_id="sC", last_active_at=300)
    conn.commit()

    since = datetime.fromtimestamp(150, tz=UTC)
    until = datetime.fromtimestamp(250, tz=UTC)
    rows = query_session_list(conn, since=since, until=until)
    assert [r.session_id for r in rows] == ["sB"]


def test_query_filter_since_only(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", last_active_at=100)
    _insert_summary(conn, session_id="sB", last_active_at=300)
    conn.commit()

    since = datetime.fromtimestamp(200, tz=UTC)
    rows = query_session_list(conn, since=since)
    assert [r.session_id for r in rows] == ["sB"]


def test_query_filter_until_only(conn: sqlite3.Connection) -> None:
    _insert_summary(conn, session_id="sA", last_active_at=100)
    _insert_summary(conn, session_id="sB", last_active_at=300)
    conn.commit()

    until = datetime.fromtimestamp(200, tz=UTC)
    rows = query_session_list(conn, until=until)
    assert [r.session_id for r in rows] == ["sA"]


def test_query_returns_session_list_rows(conn: sqlite3.Connection) -> None:
    _insert_summary(
        conn,
        session_id="sA",
        project_path="/p",
        project_display="p",
        started_at=10,
        last_active_at=20,
        duration_s=10,
        turn_count=3,
        total_cost_usd=1.5,
        summary_text="hi",
        summary_source="first-prompt",
    )
    conn.commit()

    rows = query_session_list(conn)
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, SessionListRow)
    assert r.session_id == "sA"
    assert r.project_path == "/p"
    assert r.project_display == "p"
    assert r.started_at == 10
    assert r.last_active_at == 20
    assert r.duration_s == 10
    assert r.turn_count == 3
    assert r.compaction_count == 0
    assert r.total_cost_usd == 1.5
    assert r.summary_text == "hi"
    assert r.summary_source == "first-prompt"


def test_query_compaction_count_reflects_auto_compact_files(conn: sqlite3.Connection) -> None:
    """``compaction_count`` is the number of ``agent-acompact-*.jsonl`` files
    indexed for the session. One row per compaction event."""
    _insert_summary(conn, session_id="s-with-compacts")
    _insert_summary(conn, session_id="s-no-compacts")
    # Two auto-compact files for s-with-compacts; the 'main' / 'subagent'
    # files don't contribute to the count.
    for path, kind, sid in [
        ("/fake/compact-a.jsonl", "auto-compact", "s-with-compacts"),
        ("/fake/compact-b.jsonl", "auto-compact", "s-with-compacts"),
        ("/fake/main.jsonl", "main", "s-with-compacts"),
        ("/fake/sub.jsonl", "subagent", "s-with-compacts"),
        ("/fake/main2.jsonl", "main", "s-no-compacts"),
    ]:
        conn.execute(
            "INSERT INTO files (path, mtime_ns, size, session_id, kind, agent_id, "
            "schema_version, parse_warnings, last_parsed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (path, 0, 0, sid, kind, None, None, 0, 0),
        )
    conn.commit()

    rows = {r.session_id: r for r in query_session_list(conn)}
    assert rows["s-with-compacts"].compaction_count == 2
    assert rows["s-no-compacts"].compaction_count == 0


# ---------- shorten_session_ids ----------


def test_shorten_empty_input_returns_empty_dict() -> None:
    assert shorten_session_ids([]) == {}


def test_shorten_all_unique_at_min_chars() -> None:
    ids = ["aaaaaa111", "bbbbbb222", "cccccc333"]
    out = shorten_session_ids(ids)
    assert out == {
        "aaaaaa111": "aaaaaa",
        "bbbbbb222": "bbbbbb",
        "cccccc333": "cccccc",
    }


def test_shorten_colliding_prefix_extends_to_7() -> None:
    # Two sessions share the 6-char prefix "abcdef".
    ids = ["abcdef1xxx", "abcdef2yyy", "zzzzzz000"]
    out = shorten_session_ids(ids)
    assert out["abcdef1xxx"] == "abcdef1"
    assert out["abcdef2yyy"] == "abcdef2"
    assert out["zzzzzz000"] == "zzzzzz"


def test_shorten_colliding_further_extends_beyond_7() -> None:
    # Three ids share "abcdef12" — must extend to at least 9 to distinguish all pairs.
    ids = ["abcdef12AA", "abcdef12BB", "abcdef12AC"]
    out = shorten_session_ids(ids)
    # All outputs must be prefixes of their source ids.
    for sid, short in out.items():
        assert sid.startswith(short)
    # All outputs must be unique.
    assert len(set(out.values())) == len(out)


def test_shorten_stops_at_full_length_when_ids_differ_only_beyond_prefix() -> None:
    # One id is the prefix of another. The longer id cannot be shortened below
    # what distinguishes it, and the shorter id is already the full string.
    ids = ["abcdef", "abcdef0"]
    out = shorten_session_ids(ids)
    assert out["abcdef"] == "abcdef"
    assert out["abcdef0"] == "abcdef0"


def test_shorten_mixed_length_uuids() -> None:
    # Full-length UUIDs (36 chars) with a common 6-char prefix.
    ids = [
        "aaaaaa-1111-2222-3333-444444444444",
        "aaaaaa-9999-8888-7777-666666666666",
        "zzzzzz-aaaa-bbbb-cccc-dddddddddddd",
    ]
    out = shorten_session_ids(ids)
    assert out[ids[0]].startswith("aaaaaa")
    assert out[ids[1]].startswith("aaaaaa")
    assert out[ids[2]] == "zzzzzz"
    assert out[ids[0]] != out[ids[1]]


def test_shorten_dedups_identical_inputs() -> None:
    ids = ["aaaaaa111", "aaaaaa111", "bbbbbb222"]
    out = shorten_session_ids(ids)
    assert len(out) == 2
    assert out["aaaaaa111"] == "aaaaaa"
    assert out["bbbbbb222"] == "bbbbbb"


# ---------- render_session_list ----------


def _row(
    *,
    session_id: str = "abcdef123456",
    project_display: str | None = "proj",
    started_at: int = 1_700_000_000,
    duration_s: int = 60,
    turn_count: int = 3,
    compaction_count: int = 0,
    total_cost_usd: float | None = 1.23,
    summary_text: str | None = "a short summary",
    summary_source: str | None = "first-prompt",
) -> SessionListRow:
    return SessionListRow(
        session_id=session_id,
        project_path="/home/me/proj",
        project_display=project_display,
        started_at=started_at,
        last_active_at=started_at + duration_s,
        duration_s=duration_s,
        turn_count=turn_count,
        compaction_count=compaction_count,
        total_cost_usd=total_cost_usd,
        summary_text=summary_text,
        summary_source=summary_source,
    )


def test_render_returns_rich_table() -> None:
    rows = [_row()]
    table = render_session_list(rows)
    assert isinstance(table, Table)


def test_render_default_columns_in_order() -> None:
    rows = [_row()]
    table = render_session_list(rows)
    headers = [col.header for col in table.columns]
    assert headers == [
        "UUID",
        "Project",
        "Started",
        "Dur",
        "Turns",
        "Compact",
        "Cost",
        "Summary",
    ]


def test_render_verbose_adds_source_column() -> None:
    rows = [_row()]
    table = render_session_list(rows, verbose=True)
    headers = [col.header for col in table.columns]
    assert headers == [
        "UUID",
        "Project",
        "Started",
        "Dur",
        "Turns",
        "Compact",
        "Cost",
        "Src",
        "Summary",
    ]


def test_render_row_count_includes_totals_row() -> None:
    """Three data rows + one totals row appended below a section divider."""
    rows = [
        _row(session_id="aaaaaa000000"),
        _row(session_id="bbbbbb111111"),
        _row(session_id="cccccc222222"),
    ]
    table = render_session_list(rows)
    assert table.row_count == 4


def test_render_empty_rows_returns_empty_table() -> None:
    """No data → no totals row either; column header set is still complete."""
    table = render_session_list([])
    assert table.row_count == 0
    headers = [col.header for col in table.columns]
    assert headers == [
        "UUID",
        "Project",
        "Started",
        "Dur",
        "Turns",
        "Compact",
        "Cost",
        "Summary",
    ]


def test_render_summary_preserved_untruncated() -> None:
    long_summary = "x" * 500
    rows = [_row(summary_text=long_summary)]
    table = render_session_list(rows)
    # Summary is the last column. The first cell carries the row data; the
    # second is the totals row's empty Summary placeholder. Its plain text
    # must contain the full summary verbatim (no truncation).
    summary_col = table.columns[-1]
    cells = list(summary_col.cells)
    assert len(cells) == 2
    cell = cells[0]
    plain = cell.plain if isinstance(cell, Text) else str(cell)
    assert plain == long_summary
    assert len(plain) == 500


def test_render_summary_none_shows_placeholder() -> None:
    rows = [_row(summary_text=None)]
    table = render_session_list(rows)
    summary_col = table.columns[-1]
    cells = list(summary_col.cells)
    cell = cells[0]
    plain = cell.plain if isinstance(cell, Text) else str(cell)
    assert plain == "<no summary available>"


def test_render_verbose_source_badge_mapping() -> None:
    rows = [
        _row(session_id="aaaaaa000000", summary_source="claude-summary"),
        _row(session_id="bbbbbb111111", summary_source="first-prompt"),
        _row(session_id="cccccc222222", summary_source="none"),
        _row(session_id="dddddd333333", summary_source=None),
    ]
    table = render_session_list(rows, verbose=True)
    # verbose columns: UUID, Project, Started, Dur, Turns, Compact, Cost, Src, Summary
    src_col = table.columns[7]
    assert src_col.header == "Src"
    badges = [c.plain if isinstance(c, Text) else str(c) for c in src_col.cells]
    # Last cell is the totals-row placeholder; data rows are the 4 badges.
    assert badges[:-1] == ["[C]", "[F]", "[-]", "[-]"]


def test_render_uses_shortened_session_ids() -> None:
    rows = [
        _row(session_id="abcdef1xxx"),
        _row(session_id="abcdef2yyy"),
    ]
    table = render_session_list(rows)
    uuid_col = table.columns[0]
    cells = [c.plain if isinstance(c, Text) else str(c) for c in uuid_col.cells]
    # Collisions at 6 chars → extend to 7. Final cell is the bold "Totals"
    # label from the appended totals row.
    assert cells[:-1] == ["abcdef1", "abcdef2"]
    assert cells[-1] == "Totals"


def test_render_cost_none_renders_em_dash() -> None:
    rows = [_row(total_cost_usd=None)]
    table = render_session_list(rows)
    # Cost column is index 6 now (UUID, Project, Started, Dur, Turns, Compact, Cost, ...).
    cost_col = table.columns[6]
    cell = next(iter(cost_col.cells))
    plain = cell.plain if isinstance(cell, Text) else str(cell)
    assert plain == "$—"


def test_render_to_console_contains_summary_text() -> None:
    """End-to-end: render through a Console and check the rendered text.

    Uses a terminal wide enough to avoid any soft-wrap artifact in the
    captured plaintext.
    """
    rows = [_row(summary_text="distinct-marker-abc")]
    table = render_session_list(rows)
    console = Console(width=400, record=True)
    console.print(table)
    out = console.export_text()
    assert "distinct-marker-abc" in out


def test_render_wide_terminal_includes_project_column() -> None:
    rows = [_row(project_display="proj-a")]
    table = render_session_list(rows, console_width=180)
    headers = [c.header for c in table.columns]
    assert "Project" in headers


def test_render_narrow_terminal_drops_project_column() -> None:
    """Below 120 cols the Project column is dropped so Summary has room."""
    rows = [_row(project_display="proj-a", summary_text="real summary here")]
    table = render_session_list(rows, console_width=80)
    headers = [c.header for c in table.columns]
    assert "Project" not in headers
    # Summary column still present.
    assert "Summary" in headers


def test_render_totals_row_sums_displayed_columns() -> None:
    """Totals row sums Dur, Turns, Compactions, and priced Cost. Sessions
    with NULL cost are excluded from the cost sum so the displayed total
    matches the sum of the ``$`` cells above it."""
    rows = [
        _row(session_id="s1", duration_s=60, turn_count=2, compaction_count=1, total_cost_usd=1.0),
        _row(session_id="s2", duration_s=120, turn_count=3, compaction_count=0, total_cost_usd=2.5),
        _row(session_id="s3", duration_s=30, turn_count=1, compaction_count=2, total_cost_usd=None),
    ]
    table = render_session_list(rows, console_width=180)
    # Find columns by header. Last cell of each is the totals-row content.
    by_header = {col.header: list(col.cells) for col in table.columns}
    # Totals row exists → each column has data_rows + 1 cells.
    assert len(by_header["UUID"]) == 4
    totals_uuid = by_header["UUID"][-1]
    plain_uuid = totals_uuid.plain if isinstance(totals_uuid, Text) else str(totals_uuid)
    assert plain_uuid == "Totals"

    # Turns column: "2", "3", "1", then totals "6".
    assert by_header["Turns"][-1] == "6"
    # Compact column: "1", "0", "2", then totals "3".
    assert by_header["Compact"][-1] == "3"
    # Cost column: $1.00, $2.50, $—, then totals $3.50 (NULL excluded).
    cost_total = by_header["Cost"][-1]
    plain_cost = cost_total.plain if isinstance(cost_total, Text) else str(cost_total)
    assert plain_cost == "$3.50"


def test_render_totals_row_all_null_cost_renders_em_dash() -> None:
    """Every row with NULL cost → totals cell is ``$—`` rather than ``$0.00``,
    because zero would imply a real measurement at $0 instead of "unknown"."""
    rows = [
        _row(session_id="s1", total_cost_usd=None),
        _row(session_id="s2", total_cost_usd=None),
    ]
    table = render_session_list(rows, console_width=180)
    by_header = {col.header: list(col.cells) for col in table.columns}
    cost_total = by_header["Cost"][-1]
    plain = cost_total.plain if isinstance(cost_total, Text) else str(cost_total)
    assert plain == "$—"


def test_render_boundary_at_120_is_wide() -> None:
    """120 is the wide threshold — 119 narrow, 120 wide."""
    rows = [_row()]
    narrow = render_session_list(rows, console_width=119)
    wide = render_session_list(rows, console_width=120)
    assert "Project" not in [c.header for c in narrow.columns]
    assert "Project" in [c.header for c in wide.columns]
