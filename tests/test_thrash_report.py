"""Tests for the thrash report module (T8 + T9 + T10) — query +
headline aggregate + text/JSON/CSV rendering. Synthetic
session_summaries + session_rollups + session_signals rows are
seeded directly so we don't need a full reconcile pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from ccforensics.index import ensure_schema, open_connection
from ccforensics.report.thrash import (
    CAVEATS_FOOTER,
    compute_headline,
    query_flagged_sessions,
    render_csv,
    render_json,
    render_text,
)
from ccforensics.thrash import SIGNAL_VERSION
from ccforensics.thrash_calibration import (
    MIN_CALIBRATION_EVENTS,
    build_calibration_table,
)


@pytest.fixture
def conn(tmp_path: Path) -> Any:
    db = tmp_path / "report.sqlite"
    c = open_connection(db)
    ensure_schema(c)
    return c


def _seed_session(
    conn: Any,
    session_id: str,
    *,
    summary: str = "test session",
    cost_usd: float = 1.00,
    turn_count: int = 50,
    duration_s: int = 600,
    last_active_at: int = 1_700_000_000,
    thrash_score: float = 0.55,
    primary_model: str = "claude-sonnet-4-6",
    signals: list[tuple[str, int, dict]] | None = None,
    escalation: dict | None = None,
) -> None:
    """Seed a session_summaries + session_rollups + session_signals
    + a single messages row (so primary_model lookup succeeds)."""
    conn.execute(
        """INSERT INTO session_summaries
           (session_id, project_path, project_display, started_at,
            last_active_at, duration_s, turn_count, total_cost_usd,
            summary_text, summary_source)
           VALUES (?, '/p', 'p', 0, ?, ?, ?, ?, ?, 'test')""",
        (session_id, last_active_at, duration_s, turn_count, cost_usd, summary),
    )
    conn.execute(
        """INSERT INTO files (path, mtime_ns, size, session_id, kind, last_parsed_at)
           VALUES (?, 0, 0, ?, 'main', 0)""",
        (f"/tmp/{session_id}.jsonl", session_id),
    )
    conn.execute(
        """INSERT INTO messages
           (dedup_key, file_path, session_id, role, type, ts, model, cost_usd)
           VALUES (?, ?, ?, 'assistant', 'assistant', 0, ?, ?)""",
        (
            f"k-{session_id}",
            f"/tmp/{session_id}.jsonl",
            session_id,
            primary_model,
            cost_usd,
        ),
    )
    conn.execute(
        """INSERT INTO session_rollups
           (session_id, bucket_kind, bucket_name,
            cost_usd, input_tokens, output_tokens, cache_create, cache_read,
            thrash_score, thrash_score_version, escalation_event)
           VALUES (?, 'main', 'main', ?, 0, 0, 0, 0, ?, ?, ?)""",
        (
            session_id,
            cost_usd,
            thrash_score,
            SIGNAL_VERSION if thrash_score > 0 else None,
            json.dumps(escalation, sort_keys=True) if escalation else None,
        ),
    )
    for sig_type, count, evidence in signals or []:
        conn.execute(
            """INSERT INTO session_signals
               (session_id, signal_type, count, evidence, signal_version)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, sig_type, count, json.dumps(evidence), SIGNAL_VERSION),
        )


def _seed_calibration(conn: Any, n: int, *, cost_after: float = 0.20) -> None:
    """Bulk-seed escalation events to populate the calibration table."""
    for i in range(n):
        _seed_session(
            conn,
            f"cal-{i}",
            cost_usd=0.50,
            thrash_score=0.0,
            escalation={
                "turn_index": 5,
                "from_model": "claude-sonnet-4-6",
                "to_model": "claude-opus-4-7",
                "escalation_kind": "model_switch",
                "turns_after_switch_to_resolution": 2,
                "cost_before_switch_usd": 0.10,
                "cost_after_switch_usd": cost_after,
                "wall_clock_before_seconds": 30,
                "wall_clock_after_seconds": 30,
                "resolution_marker": "user_thanks",
                "subagent_prompt_excerpt": None,
            },
        )


# ---------- query_flagged_sessions ----------


def test_query_returns_only_sessions_meeting_both_gates(conn: Any) -> None:
    _seed_session(
        conn,
        "above-both",
        thrash_score=0.55,
        signals=[
            ("novelty_window", 8, {}),
            ("repeated_edit", 6, {}),
        ],
    )
    _seed_session(
        conn,
        "score-only",
        thrash_score=0.55,
        signals=[("novelty_window", 8, {})],  # only 1 signal type
    )
    _seed_session(
        conn,
        "signals-only",
        thrash_score=0.20,  # below FLAG_THRESHOLD
        signals=[
            ("novelty_window", 8, {}),
            ("repeated_edit", 6, {}),
        ],
    )

    rows = query_flagged_sessions(conn)
    ids = {r.session_id for r in rows}
    assert ids == {"above-both"}


def test_query_session_id_filter_bypasses_gates(conn: Any) -> None:
    _seed_session(conn, "drill-target", thrash_score=0.05, signals=[])
    rows = query_flagged_sessions(conn, session_id_filter="drill-target")
    assert len(rows) == 1
    assert rows[0].session_id == "drill-target"


def test_query_caps_at_top(conn: Any) -> None:
    for i in range(10):
        _seed_session(
            conn,
            f"sess-{i}",
            thrash_score=0.5 + i * 0.01,
            signals=[("novelty_window", 8, {}), ("repeated_edit", 6, {})],
        )
    rows = query_flagged_sessions(conn, top=3)
    assert len(rows) == 3


def test_query_sort_by_observed_cost(conn: Any) -> None:
    for i, cost in enumerate([0.50, 5.00, 1.00]):
        _seed_session(
            conn,
            f"s{i}",
            thrash_score=0.5,
            cost_usd=cost,
            signals=[("novelty_window", 8, {}), ("repeated_edit", 6, {})],
        )
    rows = query_flagged_sessions(conn, sort="observed_cost")
    costs = [r.observed_cost_usd for r in rows]
    assert costs == sorted(costs, reverse=True)


def test_query_loads_signals_with_evidence(conn: Any) -> None:
    _seed_session(
        conn,
        "with-evidence",
        thrash_score=0.6,
        signals=[
            ("novelty_window", 9, {"max_flat_run": 9, "from_turn": 1, "to_turn": 9}),
            ("repeated_edit", 5, {"file_path": "/x/foo.py"}),
        ],
    )
    rows = query_flagged_sessions(conn)
    assert len(rows) == 1
    sig_types = {s["signal_type"] for s in rows[0].signals}
    assert sig_types == {"novelty_window", "repeated_edit"}
    novelty = next(s for s in rows[0].signals if s["signal_type"] == "novelty_window")
    assert novelty["evidence"]["max_flat_run"] == 9


def test_query_attaches_counterfactual_when_calibration_available(conn: Any) -> None:
    _seed_calibration(conn, MIN_CALIBRATION_EVENTS, cost_after=0.20)
    _seed_session(
        conn,
        "flagged",
        cost_usd=2.00,
        thrash_score=0.6,
        signals=[("novelty_window", 8, {}), ("repeated_edit", 6, {})],
    )
    rows = query_flagged_sessions(conn)
    flagged = next(r for r in rows if r.session_id == "flagged")
    assert flagged.counterfactual is not None
    assert flagged.counterfactual.calibration_confidence == "low"


# ---------- compute_headline ----------


def test_headline_with_no_flagged_sessions(conn: Any) -> None:
    table = build_calibration_table(conn)
    h = compute_headline([], scope_label="last 30 days", calibration_table=table)
    assert h.n_flagged == 0
    assert h.total_observed_cost_usd == 0.0
    assert h.total_est_mid_usd is None


def test_headline_aggregates_observed_and_estimates(conn: Any) -> None:
    _seed_calibration(conn, MIN_CALIBRATION_EVENTS, cost_after=0.20)
    for i in range(3):
        _seed_session(
            conn,
            f"s{i}",
            cost_usd=1.00,
            thrash_score=0.6,
            signals=[("novelty_window", 8, {}), ("repeated_edit", 6, {})],
        )
    table = build_calibration_table(conn)
    rows = query_flagged_sessions(conn, calibration_table=table)
    h = compute_headline(rows, scope_label="last 30 days", calibration_table=table)
    assert h.n_flagged == 3
    assert h.total_observed_cost_usd == 3.0
    assert h.total_est_mid_usd is not None
    assert h.total_est_mid_usd > 0


# ---------- render_text ----------


def _stringify(rendered: Any) -> str:
    console = Console(record=True, width=160, color_system=None)
    console.print(rendered)
    return console.export_text()


def test_render_text_includes_headline_and_caveats(conn: Any) -> None:
    _seed_calibration(conn, MIN_CALIBRATION_EVENTS, cost_after=0.20)
    _seed_session(
        conn,
        "abcd1234",
        summary="hard debug",
        cost_usd=1.00,
        thrash_score=0.55,
        signals=[("novelty_window", 8, {}), ("repeated_edit", 6, {})],
    )
    table = build_calibration_table(conn)
    rows = query_flagged_sessions(conn, calibration_table=table)
    headline = compute_headline(rows, scope_label="last 30 days", calibration_table=table)
    out = _stringify(render_text(headline, rows))
    assert "ccforensics thrash" in out
    assert "scored with v1" in out
    assert "hard debug" in out
    assert "novelty_window" not in out  # collapsed mode hides signals
    assert CAVEATS_FOOTER[:40] in out


def test_render_text_evidence_mode_expands_signals(conn: Any) -> None:
    _seed_calibration(conn, MIN_CALIBRATION_EVENTS, cost_after=0.20)
    _seed_session(
        conn,
        "abcd1234",
        summary="hard debug",
        cost_usd=1.00,
        thrash_score=0.55,
        signals=[
            (
                "novelty_window",
                8,
                {"max_flat_run": 8, "from_turn": 1, "to_turn": 8, "text_jaccard_max": 0.9},
            ),
            (
                "repeated_edit",
                6,
                {"file_path": "/x/foo.py", "edit_count": 6, "first_turn": 1, "last_turn": 6},
            ),
        ],
    )
    table = build_calibration_table(conn)
    rows = query_flagged_sessions(conn, calibration_table=table)
    headline = compute_headline(rows, scope_label="last 30 days", calibration_table=table)
    out = _stringify(render_text(headline, rows, show_evidence=True))
    assert "novelty_window" in out
    assert "repeated_edit" in out
    assert "/x/foo.py" in out


def test_render_text_no_flagged_sessions(conn: Any) -> None:
    table = build_calibration_table(conn)
    headline = compute_headline([], scope_label="last 30 days", calibration_table=table)
    out = _stringify(render_text(headline, []))
    assert "No flagged sessions in scope" in out


# ---------- render_json ----------


def test_render_json_shape(conn: Any) -> None:
    _seed_calibration(conn, MIN_CALIBRATION_EVENTS, cost_after=0.20)
    _seed_session(
        conn,
        "json-sess",
        cost_usd=1.00,
        thrash_score=0.55,
        signals=[("novelty_window", 8, {}), ("repeated_edit", 6, {})],
    )
    table = build_calibration_table(conn)
    rows = query_flagged_sessions(conn, calibration_table=table)
    headline = compute_headline(rows, scope_label="last 30 days", calibration_table=table)
    payload = json.loads(render_json(headline, rows))
    assert "headline" in payload
    assert "rows" in payload
    assert payload["headline"]["n_flagged"] == 1
    assert payload["headline"]["scored_with_version"] == SIGNAL_VERSION
    row = payload["rows"][0]
    assert row["session_id"] == "json-sess"
    assert row["counterfactual"]["calibration_confidence"] == "low"
    assert row["counterfactual"]["sanity_gate_passed"] is True


# ---------- render_csv ----------


def test_render_csv_shape(conn: Any) -> None:
    _seed_calibration(conn, MIN_CALIBRATION_EVENTS, cost_after=0.20)
    _seed_session(
        conn,
        "csv-sess",
        cost_usd=1.00,
        thrash_score=0.55,
        signals=[("novelty_window", 8, {}), ("repeated_edit", 6, {})],
    )
    table = build_calibration_table(conn)
    rows = query_flagged_sessions(conn, calibration_table=table)
    csv_text = render_csv(rows)
    lines = csv_text.strip().splitlines()
    header = lines[0].split(",")
    assert "session_id" in header
    assert "thrash_score" in header
    assert "calibration_confidence" in header
    assert "csv-sess" in lines[1]


def test_render_csv_handles_no_counterfactual(conn: Any) -> None:
    """Sessions w/o calibration just get empty cells in CF columns."""
    _seed_session(
        conn,
        "no-cal",
        cost_usd=1.00,
        thrash_score=0.55,
        signals=[("novelty_window", 8, {}), ("repeated_edit", 6, {})],
    )
    table = build_calibration_table(conn)  # empty
    rows = query_flagged_sessions(conn, calibration_table=table)
    csv_text = render_csv(rows)
    assert "no-cal" in csv_text
