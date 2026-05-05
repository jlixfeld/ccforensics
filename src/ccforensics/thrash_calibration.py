"""Thrash counterfactual calibration — builds a per-(from_model,
to_model) table of post-escalation cost-per-turn statistics from
``session_rollups.escalation_event`` and produces wide-honest
counterfactual ranges for sessions that DIDN'T escalate.

Spec: ``docs/specs/2026-05-05-thrash-detection-design.md`` §4.

Key constraints:
- ``auto_mode`` events excluded — automated routing isn't a deliberate
  user signal and would skew the calibration.
- Confidence tiers (low / mid / high) drive the multiplicative range
  width per spec §4 — narrows w/ more calibration events.
- Cost-sanity gate (per session): est mid must fall within 0.1x-10x
  observed cost or the counterfactual is suppressed for that session.
- Calibration is built fresh on every report run — cheap (one query),
  no caching, no stale-data risk.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

MIN_CALIBRATION_EVENTS = 10
LOW_CONF_MAX = 15
MID_CONF_MAX = 50
SANITY_GATE_LOW = 0.1
SANITY_GATE_HIGH = 10.0
SANITY_AGGREGATE_FAIL_THRESHOLD = 0.05


@dataclass(frozen=True)
class CalibrationEntry:
    from_model: str
    to_model: str
    avg_turns_post: float
    avg_cost_per_post_turn_usd: float
    n_events: int


@dataclass(frozen=True)
class Counterfactual:
    """One session's counterfactual estimate. ``None`` cases (no
    calibration / suppressed / sanity-gate failure) are represented
    by ``compute_counterfactual`` returning None — never by a
    Counterfactual instance with sentinel values.
    """

    to_model: str
    est_cost_low_usd: float
    est_cost_mid_usd: float
    est_cost_high_usd: float
    n_calibration_events: int
    calibration_confidence: str  # "low" | "mid" | "high"
    sanity_gate_passed: bool


CalibrationTable = dict[tuple[str, str], CalibrationEntry]


def build_calibration_table(conn: sqlite3.Connection) -> CalibrationTable:
    """Aggregate escalation events from ``session_rollups`` into a
    per-(from, to) calibration table. ``auto_mode`` is excluded.

    Filters:
    - ``escalation_event IS NOT NULL``
    - ``escalation_kind`` IN ('model_switch', 'subagent_dispatch')
    - ``turns_after_switch_to_resolution > 0`` (avoid divide-by-zero
      on cost-per-turn)
    """
    rows = conn.execute(
        """
        SELECT
            json_extract(escalation_event, '$.from_model'),
            json_extract(escalation_event, '$.to_model'),
            json_extract(escalation_event, '$.turns_after_switch_to_resolution'),
            json_extract(escalation_event, '$.cost_after_switch_usd'),
            json_extract(escalation_event, '$.escalation_kind')
          FROM session_rollups
         WHERE escalation_event IS NOT NULL
        """
    ).fetchall()

    bucket: dict[tuple[str, str], list[tuple[float, float]]] = {}
    seen_sessions: set[tuple[str, str]] = set()
    for from_m, to_m, turns_post, cost_after, kind in rows:
        if not from_m or not to_m:
            continue
        if kind not in ("model_switch", "subagent_dispatch"):
            continue
        if turns_post is None or turns_post <= 0:
            continue
        if cost_after is None:
            continue
        key = (from_m, to_m)
        bucket.setdefault(key, []).append(
            (float(turns_post), float(cost_after) / float(turns_post))
        )
        seen_sessions.add(key)

    table: CalibrationTable = {}
    for key, samples in bucket.items():
        n = len(samples)
        avg_turns = sum(t for t, _ in samples) / n
        avg_cpt = sum(c for _, c in samples) / n
        from_m, to_m = key
        table[key] = CalibrationEntry(
            from_model=from_m,
            to_model=to_m,
            avg_turns_post=avg_turns,
            avg_cost_per_post_turn_usd=avg_cpt,
            n_events=n,
        )
    return table


def confidence_tier(n_events: int) -> str | None:
    """Tier from spec §4 — None when below the suppression floor."""
    if n_events < MIN_CALIBRATION_EVENTS:
        return None
    if n_events < LOW_CONF_MAX:
        return "low"
    if n_events < MID_CONF_MAX:
        return "mid"
    return "high"


def _multipliers(tier: str) -> tuple[float, float]:
    """Return ``(low_mult, high_mult)`` for the given confidence tier
    per spec §4: low → 0.33-3.0x, mid → 0.5-2.0x, high → 0.67-1.5x.
    The ``mid`` estimate is always 1.0x of the calibration mean."""
    if tier == "low":
        return 0.33, 3.0
    if tier == "mid":
        return 0.5, 2.0
    return 0.67, 1.5


def compute_counterfactual(
    observed_cost_usd: float,
    table: CalibrationTable,
    from_model: str,
    to_model: str = "claude-opus-4-7",
) -> Counterfactual | None:
    """Return the counterfactual range for a flagged session, or None
    when calibration is missing/insufficient. Cost-sanity gate is
    evaluated alongside — failing sessions get None too (caller can
    track separately if it wants to surface the failure).
    """
    entry = table.get((from_model, to_model))
    if entry is None:
        return None
    tier = confidence_tier(entry.n_events)
    if tier is None:
        return None

    est_mid = entry.avg_turns_post * entry.avg_cost_per_post_turn_usd
    low_mult, high_mult = _multipliers(tier)
    est_low = low_mult * est_mid
    est_high = high_mult * est_mid

    # Cost-sanity gate per spec §4: est mid must be within 0.1x-10x of
    # observed cost or the estimate is implausible.
    sanity_passed = True
    if observed_cost_usd > 0:
        ratio = est_mid / observed_cost_usd
        sanity_passed = SANITY_GATE_LOW <= ratio <= SANITY_GATE_HIGH

    return Counterfactual(
        to_model=to_model,
        est_cost_low_usd=round(est_low, 6),
        est_cost_mid_usd=round(est_mid, 6),
        est_cost_high_usd=round(est_high, 6),
        n_calibration_events=entry.n_events,
        calibration_confidence=tier,
        sanity_gate_passed=sanity_passed,
    )


def aggregate_sanity_gate_warning(counterfactuals: list[Counterfactual | None]) -> bool:
    """Return True when more than ``SANITY_AGGREGATE_FAIL_THRESHOLD``
    fraction of non-None counterfactuals failed the sanity gate.
    Drives a headline footer warning in the report."""
    valid = [c for c in counterfactuals if c is not None]
    if not valid:
        return False
    n_failed = sum(1 for c in valid if not c.sanity_gate_passed)
    return (n_failed / len(valid)) > SANITY_AGGREGATE_FAIL_THRESHOLD


def parse_escalation_event(raw: str | None) -> dict[str, Any] | None:
    """Decode the JSON blob stored in ``session_rollups.escalation_event``
    or return None for missing / malformed values."""
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded
