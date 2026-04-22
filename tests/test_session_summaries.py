from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import (
    _is_pure_hook_injection,
    _sanitize_prompt,
    ensure_schema,
    open_connection,
    recompute_session_summary,
    reconcile_file,
    reconcile_projects_dir,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict[str, Any]:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


# ---------- fixture builder ----------


def _entry(
    *,
    type_: str,
    uuid: str,
    ts: str,
    role: str | None = None,
    text: str | None = None,
    is_meta: bool = False,
    is_sidechain: bool = False,
    is_compact_summary: bool = False,
    leaf_uuid: str | None = None,
    summary: str | None = None,
    cwd: str | None = None,
    session_id: str = "sess-1",
    request_id: str | None = None,
    msg_id: str | None = None,
    model: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "type": type_,
        "uuid": uuid,
        "sessionId": session_id,
        "timestamp": ts,
        "isSidechain": is_sidechain,
        "isMeta": is_meta,
    }
    if cwd is not None:
        rec["cwd"] = cwd
    if is_compact_summary:
        rec["isCompactSummary"] = True
    if leaf_uuid is not None:
        rec["leafUuid"] = leaf_uuid
    if summary is not None:
        rec["summary"] = summary
    if request_id is not None:
        rec["requestId"] = request_id
    if role is not None or text is not None or msg_id is not None or usage is not None:
        msg: dict[str, Any] = {}
        if role is not None:
            msg["role"] = role
        if msg_id is not None:
            msg["id"] = msg_id
        if model is not None:
            msg["model"] = model
        if text is not None:
            msg["content"] = [{"type": "text", "text": text}]
        else:
            msg["content"] = []
        if usage is not None:
            msg["usage"] = usage
        rec["message"] = msg
    return rec


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e))
            f.write("\n")


def _make_projects_dir(
    tmp_path: Path,
    *,
    session_id: str = "sess-1",
    encoded_dir: str = "-home-test-proj",
    entries: list[dict[str, Any]] | None = None,
) -> tuple[Path, Path]:
    """Return (projects_dir, jsonl_path)."""
    proj = tmp_path / "projects"
    enc = proj / encoded_dir
    p = enc / f"{session_id}.jsonl"
    _write_jsonl(p, entries or [])
    return proj, p


# ---------- _sanitize_prompt ----------


def test_sanitize_strips_command_wrappers() -> None:
    src = "<command-name>/foo</command-name><command-args>bar</command-args>real text"
    assert _sanitize_prompt(src) == "real text"


def test_sanitize_replaces_ide_attachment() -> None:
    src = (
        "Look at <ide><file path='x'>/abs/path/to/file.py</file><reason>open</reason></ide> please"
    )
    out = _sanitize_prompt(src)
    assert "/abs/path/to/file.py" in out
    assert "📎" in out
    assert "<ide" not in out


def test_sanitize_collapses_newlines() -> None:
    src = "first line\n\nsecond line\n   third"
    assert _sanitize_prompt(src) == "first line second line third"


def test_sanitize_caps_at_1000_chars() -> None:
    src = "x" * 5000
    out = _sanitize_prompt(src)
    assert len(out) == 1000


def test_sanitize_handles_empty_and_whitespace() -> None:
    assert _sanitize_prompt("") == ""
    assert _sanitize_prompt("   \n\n  ") == ""


def test_is_pure_hook_injection_true_for_long_marker_blob() -> None:
    blob = "<session-start-hook>" + ("a" * 500)
    assert _is_pure_hook_injection(blob) is True


def test_is_pure_hook_injection_false_for_normal_prompt() -> None:
    assert _is_pure_hook_injection("hello world") is False
    assert _is_pure_hook_injection("<session-start-hook> brief") is False


# ---------- numeric aggregations ----------


def test_numeric_fields_basic(tmp_path: Path, pricing_data: dict[str, Any]) -> None:
    entries = [
        _entry(
            type_="user",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="user",
            text="hi",
            cwd="/home/test/proj",
        ),
        _entry(
            type_="assistant",
            uuid="u2",
            ts="2026-04-20T10:00:30Z",
            role="assistant",
            text="hello",
            request_id="r1",
            msg_id="m1",
            model="claude-sonnet-4-5-20250929",
            usage={
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        ),
        _entry(
            type_="user",
            uuid="u3",
            ts="2026-04-20T10:01:00Z",
            role="user",
            text="more",
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT started_at, last_active_at, duration_s, turn_count, total_cost_usd "
        "FROM session_summaries WHERE session_id=?",
        ("sess-1",),
    ).fetchone()
    assert row is not None
    started, last_active, duration, turns, cost = row
    # 2026-04-20T10:00:00Z = 1776679200
    assert started == 1776679200
    assert last_active == 1776679200 + 60
    assert duration == 60
    assert turns == 2  # u1 and u3 (u2 is assistant)
    assert cost is not None
    assert cost >= 0.0


def test_turn_count_excludes_meta_and_sidechain(
    tmp_path: Path, pricing_data: dict[str, Any]
) -> None:
    entries = [
        _entry(
            type_="user",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="user",
            text="real prompt",
            cwd="/home/test",
        ),
        _entry(
            type_="user",
            uuid="u2",
            ts="2026-04-20T10:00:01Z",
            role="user",
            text="meta",
            is_meta=True,
        ),
        _entry(
            type_="user",
            uuid="u3",
            ts="2026-04-20T10:00:02Z",
            role="user",
            text="sidechain",
            is_sidechain=True,
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT turn_count FROM session_summaries WHERE session_id=?", ("sess-1",)
    ).fetchone()
    assert row[0] == 1


def test_total_cost_null_when_all_costs_null(tmp_path: Path, pricing_data: dict[str, Any]) -> None:
    """Assistant entry with an unresolvable model → cost_usd is NULL.
    Other entries are non-billable (cost_usd=0.0). Test forces ALL entries
    to be unresolvable-or-non-existent so the SUM stays NULL.
    """
    # Single assistant message with bogus model name → cost None.
    entries = [
        _entry(
            type_="assistant",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="assistant",
            text="hi",
            request_id="r1",
            msg_id="m-bogus",
            model="this-model-does-not-exist-anywhere",
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT total_cost_usd FROM session_summaries WHERE session_id=?", ("sess-1",)
    ).fetchone()
    assert row[0] is None


def test_total_cost_partial_sum_when_some_resolved(
    tmp_path: Path, pricing_data: dict[str, Any]
) -> None:
    entries = [
        _entry(
            type_="assistant",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="assistant",
            text="hi",
            request_id="r1",
            msg_id="m1",
            model="claude-sonnet-4-5-20250929",
            usage={"input_tokens": 100, "output_tokens": 5},
        ),
        _entry(
            type_="assistant",
            uuid="u2",
            ts="2026-04-20T10:00:30Z",
            role="assistant",
            text="ok",
            request_id="r2",
            msg_id="m2",
            model="this-model-does-not-exist",
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT total_cost_usd FROM session_summaries WHERE session_id=?", ("sess-1",)
    ).fetchone()
    assert row[0] is not None
    assert row[0] > 0.0


# ---------- summary extraction priority ----------


def test_summary_from_type_summary_with_matching_leaf_uuid(
    tmp_path: Path, pricing_data: dict[str, Any]
) -> None:
    entries = [
        _entry(
            type_="user",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="user",
            text="raw prompt that should be ignored",
            cwd="/home/test",
        ),
        _entry(
            type_="summary",
            uuid="s1",
            ts="2026-04-20T10:05:00Z",
            summary="Picked this Claude summary",
            leaf_uuid="u1",
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT summary_text, summary_source FROM session_summaries WHERE session_id=?",
        ("sess-1",),
    ).fetchone()
    assert row[0] == "Picked this Claude summary"
    assert row[1] == "claude-summary"


def test_summary_falls_back_to_compact_summary(
    tmp_path: Path, pricing_data: dict[str, Any]
) -> None:
    entries = [
        _entry(
            type_="user",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="user",
            text="should be ignored — compact summary wins",
            cwd="/home/test",
        ),
        _entry(
            type_="user",
            uuid="u2",
            ts="2026-04-20T10:05:00Z",
            role="user",
            text="An older compact summary",
            is_compact_summary=True,
        ),
        _entry(
            type_="user",
            uuid="u3",
            ts="2026-04-20T10:10:00Z",
            role="user",
            text="Most recent compact summary",
            is_compact_summary=True,
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT summary_text, summary_source FROM session_summaries WHERE session_id=?",
        ("sess-1",),
    ).fetchone()
    assert row[0] == "Most recent compact summary"
    assert row[1] == "claude-summary"


def test_summary_falls_back_to_first_user_prompt(
    tmp_path: Path, pricing_data: dict[str, Any]
) -> None:
    entries = [
        _entry(
            type_="user",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="user",
            text="<command-name>/foo</command-name>real first prompt",
            cwd="/home/test",
        ),
        _entry(
            type_="user",
            uuid="u2",
            ts="2026-04-20T10:01:00Z",
            role="user",
            text="second prompt",
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT summary_text, summary_source FROM session_summaries WHERE session_id=?",
        ("sess-1",),
    ).fetchone()
    assert row[0] == "real first prompt"
    assert row[1] == "first-prompt"


def test_first_prompt_skips_pure_hook_injection(
    tmp_path: Path, pricing_data: dict[str, Any]
) -> None:
    """If the first user prompt is dominated by a hook-injection blob,
    skip it and use the next eligible prompt."""
    big_hook = "<session-start-hook>" + ("X" * 500)
    entries = [
        _entry(
            type_="user",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="user",
            text=big_hook,
            cwd="/home/test",
        ),
        _entry(
            type_="user",
            uuid="u2",
            ts="2026-04-20T10:01:00Z",
            role="user",
            text="real human prompt",
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT summary_text, summary_source FROM session_summaries WHERE session_id=?",
        ("sess-1",),
    ).fetchone()
    assert row[0] == "real human prompt"
    assert row[1] == "first-prompt"


def test_summary_none_when_no_user_prompt(tmp_path: Path, pricing_data: dict[str, Any]) -> None:
    entries = [
        _entry(
            type_="assistant",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="assistant",
            text="hi",
            request_id="r1",
            msg_id="m1",
            model="claude-sonnet-4-5-20250929",
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT summary_text, summary_source FROM session_summaries WHERE session_id=?",
        ("sess-1",),
    ).fetchone()
    assert row[0] == "<no summary available>"
    assert row[1] == "none"


# ---------- project_path / project_display ----------


def test_project_path_from_first_cwd(tmp_path: Path, pricing_data: dict[str, Any]) -> None:
    entries = [
        _entry(
            type_="user",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="user",
            text="hi",
            cwd="/home/test/myproject",
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT project_path, project_display FROM session_summaries WHERE session_id=?",
        ("sess-1",),
    ).fetchone()
    assert row[0] == "/home/test/myproject"
    assert row[1] == "myproject"


def test_project_path_falls_back_to_decoded_dirname(
    tmp_path: Path, pricing_data: dict[str, Any]
) -> None:
    """No cwd in any entry → decode from encoded directory name."""
    entries = [
        _entry(
            type_="user",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="user",
            text="hi",
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, encoded_dir="-Users-jane-code-things", entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT project_path, project_display FROM session_summaries WHERE session_id=?",
        ("sess-1",),
    ).fetchone()
    assert row[0] == "/Users/jane/code/things"
    assert row[1] == "things"


def test_project_display_truncated_to_30(tmp_path: Path, pricing_data: dict[str, Any]) -> None:
    long_name = "a" * 60
    entries = [
        _entry(
            type_="user",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="user",
            text="hi",
            cwd=f"/parent/{long_name}",
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    row = conn.execute(
        "SELECT project_display FROM session_summaries WHERE session_id=?", ("sess-1",)
    ).fetchone()
    assert len(row[0]) == 30


# ---------- idempotency / change-tracking ----------


def test_recompute_is_idempotent(tmp_path: Path, pricing_data: dict[str, Any]) -> None:
    entries = [
        _entry(
            type_="user",
            uuid="u1",
            ts="2026-04-20T10:00:00Z",
            role="user",
            text="hi",
            cwd="/home/test",
        ),
    ]
    proj, _p = _make_projects_dir(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    first = conn.execute(
        "SELECT * FROM session_summaries WHERE session_id=?", ("sess-1",)
    ).fetchall()

    # Run again; nothing changed on disk → file is skipped, but the summary
    # row should remain identical.
    reconcile_projects_dir(conn, proj, pricing_data)
    second = conn.execute(
        "SELECT * FROM session_summaries WHERE session_id=?", ("sess-1",)
    ).fetchall()
    assert first == second

    # Force recompute — should still be byte-identical.
    recompute_session_summary(conn, "sess-1")
    conn.commit()
    third = conn.execute(
        "SELECT * FROM session_summaries WHERE session_id=?", ("sess-1",)
    ).fetchall()
    assert first == third


def test_only_touched_sessions_recompute(tmp_path: Path, pricing_data: dict[str, Any]) -> None:
    proj = tmp_path / "projects"
    enc_a = proj / "-home-test-a"
    enc_b = proj / "-home-test-b"

    entries_a = [
        _entry(
            type_="user",
            uuid="ua1",
            ts="2026-04-20T10:00:00Z",
            role="user",
            text="A first prompt",
            cwd="/home/test/a",
            session_id="sess-a",
        ),
    ]
    entries_b = [
        _entry(
            type_="user",
            uuid="ub1",
            ts="2026-04-20T11:00:00Z",
            role="user",
            text="B first prompt",
            cwd="/home/test/b",
            session_id="sess-b",
        ),
    ]
    _write_jsonl(enc_a / "sess-a.jsonl", entries_a)
    _write_jsonl(enc_b / "sess-b.jsonl", entries_b)

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    stats1 = reconcile_projects_dir(conn, proj, pricing_data)
    assert stats1.sessions_recomputed == {"sess-a", "sess-b"}

    # Mutate only session B's file.
    new_entries_b = [
        *entries_b,
        _entry(
            type_="user",
            uuid="ub2",
            ts="2026-04-20T11:30:00Z",
            role="user",
            text="B second prompt",
            session_id="sess-b",
        ),
    ]
    _write_jsonl(enc_b / "sess-b.jsonl", new_entries_b)
    os.utime(
        enc_b / "sess-b.jsonl",
        (time.time() + 5, time.time() + 5),
    )

    stats2 = reconcile_projects_dir(conn, proj, pricing_data)
    assert stats2.sessions_recomputed == {"sess-b"}

    # A's row was NOT touched — turn_count still 1.
    row_a = conn.execute(
        "SELECT turn_count FROM session_summaries WHERE session_id=?", ("sess-a",)
    ).fetchone()
    assert row_a[0] == 1
    row_b = conn.execute(
        "SELECT turn_count FROM session_summaries WHERE session_id=?", ("sess-b",)
    ).fetchone()
    assert row_b[0] == 2


# ---------- empty-session edge case ----------


def test_recompute_skipped_when_session_has_no_messages(
    tmp_path: Path, pricing_data: dict[str, Any]
) -> None:
    """A file with zero parseable entries → no messages → no summary row.

    We don't write a session_summaries row with bogus zero timestamps.
    """
    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    enc.mkdir(parents=True)
    (enc / "sess-empty.jsonl").write_text("")

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_file(conn, enc / "sess-empty.jsonl", pricing_data)
    conn.commit()
    recompute_session_summary(conn, "sess-empty")
    conn.commit()

    row = conn.execute(
        "SELECT * FROM session_summaries WHERE session_id=?", ("sess-empty",)
    ).fetchone()
    assert row is None
