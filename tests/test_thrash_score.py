"""Tests for the composite scorer + ``populate_session_signals``
orchestrator. Synthetic Signal lists are sufficient to test the
scorer; the orchestrator is exercised end-to-end against a real
SQLite index w/ schema v4 applied.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import ensure_schema, open_connection
from ccforensics.models import parse_entry
from ccforensics.thrash import (
    FLAG_THRESHOLD,
    MIN_FIRED_SIGNAL_TYPES,
    SIGNAL_VERSION,
    THRESHOLDS,
    WEIGHTS,
    Signal,
    compute_thrash_score,
    is_flagged,
    populate_session_signals,
)

# ---------- composite scorer ----------


def test_empty_signals_score_is_zero() -> None:
    assert compute_thrash_score([]) == 0.0


def test_single_signal_below_flag_threshold() -> None:
    """Spec: a single fired signal must score < 0.40 under any count
    bonus, so the flag gate (composite + count gate) holds even
    without the count gate."""
    sig = Signal("novelty_window", count=10000, evidence={})
    assert compute_thrash_score([sig]) < FLAG_THRESHOLD


def test_two_signals_can_cross_flag_threshold() -> None:
    """Two top-weighted signals at threshold should clear the flag
    gate (sum 0.40 base ≥ FLAG_THRESHOLD)."""
    signals = [
        Signal("novelty_window", count=THRESHOLDS["novelty_window"], evidence={}),
        Signal("test_regression", count=THRESHOLDS["test_regression"], evidence={}),
    ]
    score = compute_thrash_score(signals)
    assert score >= FLAG_THRESHOLD


def test_score_saturates_at_one() -> None:
    signals = [Signal(name, count=THRESHOLDS[name] * 100, evidence={}) for name in WEIGHTS]
    assert compute_thrash_score(signals) == 1.0


def test_count_bonus_capped_per_signal() -> None:
    """No single signal can exceed 1.5x its weight via bonus."""
    weight = WEIGHTS["novelty_window"]
    sig = Signal("novelty_window", count=10**12, evidence={})
    assert compute_thrash_score([sig]) <= weight * 1.5 + 1e-9


def test_unknown_signal_type_ignored_by_scorer() -> None:
    """Forward-compat: a stale signal type produced by an older
    SIGNAL_VERSION must not crash the scorer."""
    signals = [Signal("nonexistent_signal_type", count=999, evidence={})]
    assert compute_thrash_score(signals) == 0.0


def test_is_flagged_requires_two_distinct_signal_types() -> None:
    high_single = [Signal("novelty_window", count=10**6, evidence={})]
    assert not is_flagged(high_single)


def test_is_flagged_true_when_both_gates_pass() -> None:
    signals = [
        Signal("novelty_window", count=THRESHOLDS["novelty_window"], evidence={}),
        Signal("test_regression", count=THRESHOLDS["test_regression"], evidence={}),
        Signal("repeated_edit", count=THRESHOLDS["repeated_edit"], evidence={}),
    ]
    assert is_flagged(signals)


def test_signal_version_is_pinned() -> None:
    """Bumping ``SIGNAL_VERSION`` is a deliberate breaking change —
    pin it here so a casual edit forces an explicit acknowledgment."""
    assert SIGNAL_VERSION == 1
    assert MIN_FIRED_SIGNAL_TYPES == 2


# ---------- populate_session_signals orchestrator ----------


def _ts(i: int) -> str:
    hour = 10 + i // 60
    minute = i % 60
    return f"2026-04-22T{hour:02d}:{minute:02d}:00Z"


def _assistant(uuid: str, ts: str, model: str = "claude-sonnet-4-6") -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": "s",
        "timestamp": ts,
        "isSidechain": False,
        "isMeta": False,
        "requestId": f"r-{uuid}",
        "message": {
            "id": f"m-{uuid}",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _seed_session_rollup(
    conn: Any, session_id: str, bucket_kinds: tuple[str, ...] = ("main",)
) -> None:
    """Insert minimal rollup rows so ``populate_session_signals``'s
    UPDATE has something to write to. Real reconcile would populate
    these via ``recompute_session_rollups``."""
    for kind in bucket_kinds:
        conn.execute(
            """INSERT INTO session_rollups
               (session_id, bucket_kind, bucket_name,
                cost_usd, input_tokens, output_tokens, cache_create, cache_read)
               VALUES (?, ?, ?, 0, 0, 0, 0, 0)""",
            (session_id, kind, kind),
        )


@pytest.fixture
def conn(tmp_path: Path) -> Any:
    db = tmp_path / "thrash.sqlite"
    c = open_connection(db)
    ensure_schema(c)
    return c


def test_populate_filters_short_sonnet_session(conn: Any) -> None:
    """Sonnet session w/ < 20 turns must NOT score; rollup gets NULL."""
    _seed_session_rollup(conn, "s1")
    raw = [_assistant(f"a{i}", _ts(i)) for i in range(5)]
    entries = [parse_entry(r) for r in raw]

    result = populate_session_signals(conn, "s1", entries)
    assert result is None

    score, version = conn.execute(
        "SELECT thrash_score, thrash_score_version FROM session_rollups WHERE session_id=?",
        ("s1",),
    ).fetchone()
    assert score is None
    assert version is None

    n_signals = conn.execute(
        "SELECT COUNT(*) FROM session_signals WHERE session_id=?", ("s1",)
    ).fetchone()[0]
    assert n_signals == 0


def test_populate_filters_opus_session(conn: Any) -> None:
    _seed_session_rollup(conn, "s-opus")
    raw = [_assistant(f"a{i}", _ts(i), model="claude-opus-4-7") for i in range(40)]
    entries = [parse_entry(r) for r in raw]

    result = populate_session_signals(conn, "s-opus", entries)
    assert result is None
    n_signals = conn.execute(
        "SELECT COUNT(*) FROM session_signals WHERE session_id=?", ("s-opus",)
    ).fetchone()[0]
    assert n_signals == 0


def test_populate_writes_score_and_signals_for_eligible_session(conn: Any) -> None:
    """Sonnet session w/ 25 turns + tool churn that fires multiple
    signals — should write rows and a non-NULL score."""
    _seed_session_rollup(conn, "s-elig", bucket_kinds=("main", "subagent:Explore"))

    # Construct a session that fires placeholder_emit + repeated_edit
    # + repeated_error + user_correction. Need to manually craft because
    # the orchestrator runs ALL extractors.
    raw: list[dict[str, Any]] = [
        {
            "type": "user",
            "uuid": "u-init",
            "sessionId": "s-elig",
            "timestamp": _ts(0),
            "isSidechain": False,
            "isMeta": False,
            "message": {"role": "user", "content": "go"},
        }
    ]
    # 5 edits w/ 2 distinct errors interleaved → repeated_edit
    for i in range(5):
        raw.append(
            {
                "type": "assistant",
                "uuid": f"a-edit{i}",
                "sessionId": "s-elig",
                "timestamp": _ts(1 + i * 2),
                "isSidechain": False,
                "isMeta": False,
                "requestId": f"r-edit{i}",
                "message": {
                    "id": f"m-edit{i}",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"te{i}",
                            "name": "Edit",
                            "input": {
                                "file_path": "/x/foo.py",
                                "new_string": "raise NotImplementedError",
                            },
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
        err_msg = (
            "AttributeError: x has no attr foo" if i % 2 == 0 else "TypeError: bad operand type"
        )
        raw.append(
            {
                "type": "user",
                "uuid": f"u-err{i}",
                "sessionId": "s-elig",
                "timestamp": _ts(2 + i * 2),
                "isSidechain": False,
                "isMeta": False,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"te{i}",
                            "content": err_msg,
                        }
                    ],
                },
            }
        )

    # Pad to >= 20 assistant turns w/ corrections
    next_ts = 11
    for i in range(20):
        raw.append(
            {
                "type": "assistant",
                "uuid": f"a-pad{i}",
                "sessionId": "s-elig",
                "timestamp": _ts(next_ts),
                "isSidechain": False,
                "isMeta": False,
                "requestId": f"r-pad{i}",
                "message": {
                    "id": f"m-pad{i}",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
        next_ts += 1
        raw.append(
            {
                "type": "user",
                "uuid": f"u-pad{i}",
                "sessionId": "s-elig",
                "timestamp": _ts(next_ts),
                "isSidechain": False,
                "isMeta": False,
                "message": {"role": "user", "content": "no, wrong"},
            }
        )
        next_ts += 1

    entries = [parse_entry(r) for r in raw]

    score = populate_session_signals(conn, "s-elig", entries)
    assert score is not None
    assert score > 0.0

    # All rollup rows for the session get the same score + version.
    rows = conn.execute(
        """SELECT bucket_kind, thrash_score, thrash_score_version
             FROM session_rollups WHERE session_id=?""",
        ("s-elig",),
    ).fetchall()
    assert len(rows) == 2
    assert all(r[1] == score for r in rows)
    assert all(r[2] == SIGNAL_VERSION for r in rows)

    # session_signals populated; evidence is JSON-decodable.
    sig_rows = conn.execute(
        """SELECT signal_type, count, evidence, signal_version
             FROM session_signals WHERE session_id=?""",
        ("s-elig",),
    ).fetchall()
    assert sig_rows
    types = {r[0] for r in sig_rows}
    assert "repeated_edit" in types
    assert "user_correction" in types
    assert "placeholder_emit" in types
    for _t, _c, ev, ver in sig_rows:
        assert ver == SIGNAL_VERSION
        json.loads(ev)  # must round-trip


def test_populate_clears_stale_signals_on_recompute(conn: Any) -> None:
    """A second populate call must DELETE prior session_signals rows
    so versions/results don't accumulate."""
    _seed_session_rollup(conn, "s-x")
    raw = [_assistant(f"a{i}", _ts(i)) for i in range(25)]
    entries = [parse_entry(r) for r in raw]

    # Seed a stale row directly so we can prove it gets cleared.
    conn.execute(
        """INSERT INTO session_signals
           (session_id, signal_type, count, evidence, signal_version)
           VALUES ('s-x', 'stale_type', 99, '{}', 0)"""
    )

    populate_session_signals(conn, "s-x", entries)

    n_stale = conn.execute(
        "SELECT COUNT(*) FROM session_signals WHERE session_id='s-x' AND signal_type='stale_type'"
    ).fetchone()[0]
    assert n_stale == 0


def test_populate_filtered_session_clears_stale_signals(conn: Any) -> None:
    """A session that becomes filtered (e.g., previously eligible,
    now Opus-majority) must have prior signals cleared."""
    _seed_session_rollup(conn, "s-y")
    conn.execute(
        """INSERT INTO session_signals
           (session_id, signal_type, count, evidence, signal_version)
           VALUES ('s-y', 'novelty_window', 7, '{}', 1)"""
    )

    raw = [_assistant(f"a{i}", _ts(i), model="claude-opus-4-7") for i in range(40)]
    entries = [parse_entry(r) for r in raw]
    populate_session_signals(conn, "s-y", entries)

    n_signals = conn.execute(
        "SELECT COUNT(*) FROM session_signals WHERE session_id='s-y'"
    ).fetchone()[0]
    assert n_signals == 0
