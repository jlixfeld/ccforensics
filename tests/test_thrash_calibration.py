"""Tests for thrash calibration table + counterfactual estimation
(T7). Builds synthetic ``session_rollups`` rows w/ escalation_event
JSON and asserts the calibration table arithmetic + confidence-tier
+ sanity-gate behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import ensure_schema, open_connection
from ccforensics.thrash_calibration import (
    LOW_CONF_MAX,
    MID_CONF_MAX,
    MIN_CALIBRATION_EVENTS,
    SANITY_AGGREGATE_FAIL_THRESHOLD,
    Counterfactual,
    aggregate_sanity_gate_warning,
    build_calibration_table,
    compute_counterfactual,
    confidence_tier,
    parse_escalation_event,
)


@pytest.fixture
def conn(tmp_path: Path) -> Any:
    db = tmp_path / "calib.sqlite"
    c = open_connection(db)
    ensure_schema(c)
    return c


def _seed_escalation(
    conn: Any,
    session_id: str,
    *,
    from_model: str = "claude-sonnet-4-6",
    to_model: str = "claude-opus-4-7",
    kind: str = "model_switch",
    turns_post: int = 3,
    cost_after: float = 0.30,
) -> None:
    """Insert a session_rollups row w/ a synthetic escalation_event."""
    payload = {
        "turn_index": 5,
        "from_model": from_model,
        "to_model": to_model,
        "escalation_kind": kind,
        "turns_after_switch_to_resolution": turns_post,
        "cost_before_switch_usd": 0.10,
        "cost_after_switch_usd": cost_after,
        "wall_clock_before_seconds": 60,
        "wall_clock_after_seconds": 60,
        "resolution_marker": "user_thanks",
        "subagent_prompt_excerpt": None,
    }
    conn.execute(
        """INSERT INTO session_rollups
           (session_id, bucket_kind, bucket_name,
            cost_usd, input_tokens, output_tokens, cache_create, cache_read,
            escalation_event)
           VALUES (?, 'main', 'main', 0, 0, 0, 0, 0, ?)""",
        (session_id, json.dumps(payload, sort_keys=True)),
    )


# ---------- confidence_tier ----------


def test_confidence_tier_below_min_returns_none() -> None:
    assert confidence_tier(MIN_CALIBRATION_EVENTS - 1) is None
    assert confidence_tier(0) is None


def test_confidence_tier_low_band() -> None:
    assert confidence_tier(MIN_CALIBRATION_EVENTS) == "low"
    assert confidence_tier(LOW_CONF_MAX - 1) == "low"


def test_confidence_tier_mid_band() -> None:
    assert confidence_tier(LOW_CONF_MAX) == "mid"
    assert confidence_tier(MID_CONF_MAX - 1) == "mid"


def test_confidence_tier_high_band() -> None:
    assert confidence_tier(MID_CONF_MAX) == "high"
    assert confidence_tier(500) == "high"


# ---------- build_calibration_table ----------


def test_calibration_table_empty_corpus(conn: Any) -> None:
    assert build_calibration_table(conn) == {}


def test_calibration_table_aggregates_per_pair(conn: Any) -> None:
    for i in range(5):
        _seed_escalation(conn, f"s{i}", turns_post=2, cost_after=0.20)
    for i in range(3):
        _seed_escalation(
            conn,
            f"haiku-{i}",
            from_model="claude-haiku-4-5",
            to_model="claude-sonnet-4-6",
            turns_post=4,
            cost_after=0.10,
        )

    table = build_calibration_table(conn)
    assert len(table) == 2
    sonnet_to_opus = table[("claude-sonnet-4-6", "claude-opus-4-7")]
    assert sonnet_to_opus.n_events == 5
    assert abs(sonnet_to_opus.avg_turns_post - 2.0) < 1e-9
    assert abs(sonnet_to_opus.avg_cost_per_post_turn_usd - 0.10) < 1e-9


def test_calibration_table_excludes_auto_mode(conn: Any) -> None:
    _seed_escalation(conn, "s-auto", kind="auto_mode")
    _seed_escalation(conn, "s-real", kind="model_switch")
    table = build_calibration_table(conn)
    entry = table[("claude-sonnet-4-6", "claude-opus-4-7")]
    assert entry.n_events == 1


def test_calibration_table_skips_zero_turn_events(conn: Any) -> None:
    """An escalation w/ turns_after_switch_to_resolution = 0 would
    divide-by-zero in the cost-per-turn calc — must be filtered."""
    _seed_escalation(conn, "s-degen", turns_post=0)
    _seed_escalation(conn, "s-good", turns_post=3)
    table = build_calibration_table(conn)
    entry = table[("claude-sonnet-4-6", "claude-opus-4-7")]
    assert entry.n_events == 1


# ---------- compute_counterfactual ----------


def test_counterfactual_none_when_below_min_events(conn: Any) -> None:
    for i in range(MIN_CALIBRATION_EVENTS - 1):
        _seed_escalation(conn, f"s{i}", turns_post=2, cost_after=0.20)
    table = build_calibration_table(conn)
    cf = compute_counterfactual(observed_cost_usd=2.0, table=table, from_model="claude-sonnet-4-6")
    assert cf is None


def test_counterfactual_low_tier_uses_wide_multipliers(conn: Any) -> None:
    for i in range(MIN_CALIBRATION_EVENTS):
        _seed_escalation(conn, f"s{i}", turns_post=2, cost_after=0.20)
    table = build_calibration_table(conn)
    cf = compute_counterfactual(observed_cost_usd=1.0, table=table, from_model="claude-sonnet-4-6")
    assert cf is not None
    assert cf.calibration_confidence == "low"
    # Mid = 2.0 turns x 0.10/turn = 0.20
    assert abs(cf.est_cost_mid_usd - 0.20) < 1e-6
    # Low band: 0.33x mid; High band: 3.0x mid
    assert abs(cf.est_cost_low_usd - 0.20 * 0.33) < 1e-6
    assert abs(cf.est_cost_high_usd - 0.20 * 3.0) < 1e-6


def test_counterfactual_mid_tier_uses_narrower_multipliers(conn: Any) -> None:
    for i in range(LOW_CONF_MAX):
        _seed_escalation(conn, f"s{i}", turns_post=2, cost_after=0.20)
    table = build_calibration_table(conn)
    cf = compute_counterfactual(observed_cost_usd=1.0, table=table, from_model="claude-sonnet-4-6")
    assert cf is not None
    assert cf.calibration_confidence == "mid"
    assert abs(cf.est_cost_low_usd - 0.20 * 0.5) < 1e-6
    assert abs(cf.est_cost_high_usd - 0.20 * 2.0) < 1e-6


def test_counterfactual_high_tier_uses_tightest_multipliers(conn: Any) -> None:
    for i in range(MID_CONF_MAX + 5):
        _seed_escalation(conn, f"s{i}", turns_post=2, cost_after=0.20)
    table = build_calibration_table(conn)
    cf = compute_counterfactual(observed_cost_usd=1.0, table=table, from_model="claude-sonnet-4-6")
    assert cf is not None
    assert cf.calibration_confidence == "high"
    assert abs(cf.est_cost_low_usd - 0.20 * 0.67) < 1e-6
    assert abs(cf.est_cost_high_usd - 0.20 * 1.5) < 1e-6


def test_counterfactual_sanity_gate_passes_when_in_band(conn: Any) -> None:
    for i in range(MIN_CALIBRATION_EVENTS):
        _seed_escalation(conn, f"s{i}", turns_post=2, cost_after=0.20)
    table = build_calibration_table(conn)
    cf = compute_counterfactual(observed_cost_usd=1.0, table=table, from_model="claude-sonnet-4-6")
    assert cf is not None
    assert cf.sanity_gate_passed is True


def test_counterfactual_sanity_gate_fails_when_estimate_dwarfs_observed(
    conn: Any,
) -> None:
    """Calibration mid = $1.00; observed = $0.05 → ratio = 20x ≥ 10x →
    sanity gate fails."""
    for i in range(MIN_CALIBRATION_EVENTS):
        _seed_escalation(conn, f"s{i}", turns_post=10, cost_after=1.00)
    table = build_calibration_table(conn)
    cf = compute_counterfactual(observed_cost_usd=0.05, table=table, from_model="claude-sonnet-4-6")
    assert cf is not None
    assert cf.sanity_gate_passed is False


def test_counterfactual_returns_none_when_pair_missing(conn: Any) -> None:
    for i in range(MIN_CALIBRATION_EVENTS):
        _seed_escalation(conn, f"s{i}", from_model="claude-haiku-4-5", to_model="claude-sonnet-4-6")
    table = build_calibration_table(conn)
    cf = compute_counterfactual(observed_cost_usd=1.0, table=table, from_model="claude-sonnet-4-6")
    assert cf is None


# ---------- aggregate_sanity_gate_warning ----------


def test_aggregate_sanity_gate_warning_true_when_threshold_exceeded() -> None:
    """If > 5% of counterfactuals failed sanity gate → warning fires."""
    cfs: list[Counterfactual | None] = []
    n_total = 100
    n_fail = int(n_total * SANITY_AGGREGATE_FAIL_THRESHOLD) + 5
    for i in range(n_total):
        cfs.append(
            Counterfactual(
                to_model="x",
                est_cost_low_usd=0.0,
                est_cost_mid_usd=0.0,
                est_cost_high_usd=0.0,
                n_calibration_events=20,
                calibration_confidence="mid",
                sanity_gate_passed=(i >= n_fail),
            )
        )
    assert aggregate_sanity_gate_warning(cfs) is True


def test_aggregate_sanity_gate_warning_false_when_clean() -> None:
    cfs = [Counterfactual("x", 0, 0, 0, 20, "mid", True) for _ in range(20)]
    assert aggregate_sanity_gate_warning(cfs) is False


def test_aggregate_sanity_gate_warning_handles_empty_input() -> None:
    assert aggregate_sanity_gate_warning([]) is False
    assert aggregate_sanity_gate_warning([None, None]) is False


# ---------- parse_escalation_event ----------


def test_parse_escalation_event_handles_missing_and_malformed() -> None:
    assert parse_escalation_event(None) is None
    assert parse_escalation_event("") is None
    assert parse_escalation_event("not json") is None
    assert parse_escalation_event("[1,2,3]") is None
    assert parse_escalation_event('{"x": 1}') == {"x": 1}
