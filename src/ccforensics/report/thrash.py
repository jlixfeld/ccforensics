"""Thrash report — list flagged sessions w/ headline aggregate, signal
evidence, and counterfactual cost ranges anchored on the calibration
table. Spec: ``docs/specs/2026-05-05-thrash-detection-design.md`` §5.

Output formats: text (default), JSON, CSV. ``--evidence`` expands per-
session signal payloads; ``--session ID`` drills into a single
session w/ full evidence + escalation_event JSON.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from rich import box
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..thrash import FLAG_THRESHOLD, MIN_FIRED_SIGNAL_TYPES, SIGNAL_VERSION
from ..thrash_calibration import (
    SANITY_AGGREGATE_FAIL_THRESHOLD,
    CalibrationTable,
    Counterfactual,
    aggregate_sanity_gate_warning,
    build_calibration_table,
    compute_counterfactual,
    parse_escalation_event,
)
from ._format import format_cost, format_duration

SortBy = Literal["score", "observed_cost", "est_savings_mid", "est_savings_high"]


@dataclass
class FlaggedSession:
    session_id: str
    summary: str
    primary_model: str
    thrash_score: float
    thrash_score_version: int
    turns: int
    wall_clock_seconds: int
    observed_cost_usd: float
    started_at: int
    signals: list[dict[str, Any]] = field(default_factory=list)
    escalation_event: dict[str, Any] | None = None
    counterfactual: Counterfactual | None = None


@dataclass
class HeadlineAggregate:
    scope_label: str
    scored_with_version: int
    n_flagged: int
    total_turns: int
    total_observed_cost_usd: float
    total_est_low_usd: float | None
    total_est_mid_usd: float | None
    total_est_high_usd: float | None
    sanity_warning: bool
    n_calibration_pairs: int


# ---------- query ----------


def _scope_params(
    since: datetime | None,
    until: datetime | None,
    session_id_filter: str | None,
) -> tuple[int | None, int | None, str | None]:
    """Return the three scope-bind parameters consumed by
    ``_FLAGGED_SESSIONS_SQL``. The query uses NULL-guarded conditions
    (``? IS NULL OR ...``) so each parameter is either a real bound
    value or NULL meaning "no filter on this dimension"."""
    return (
        int(since.timestamp()) if since is not None else None,
        int(until.timestamp()) if until is not None else None,
        session_id_filter,
    )


_FLAGGED_SESSIONS_SQL = (
    "SELECT "
    "ss.session_id, "
    "ss.summary_text, "
    "COALESCE(ss.total_cost_usd, 0.0) AS observed_cost_usd, "
    "ss.turn_count, "
    "ss.duration_s, "
    "ss.last_active_at, "
    "(SELECT MAX(thrash_score) FROM session_rollups "
    " WHERE session_id = ss.session_id) AS thrash_score, "
    "(SELECT MAX(thrash_score_version) FROM session_rollups "
    " WHERE session_id = ss.session_id) AS thrash_score_version, "
    "(SELECT escalation_event FROM session_rollups "
    " WHERE session_id = ss.session_id "
    "   AND escalation_event IS NOT NULL LIMIT 1) AS escalation_event_json "
    "FROM session_summaries ss "
    "WHERE (? IS NULL OR ss.last_active_at >= ?) "
    "  AND (? IS NULL OR ss.last_active_at <= ?) "
    "  AND (? IS NULL OR ss.session_id = ?)"
)


def query_flagged_sessions(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    min_score: float = FLAG_THRESHOLD,
    min_signals: int = MIN_FIRED_SIGNAL_TYPES,
    top: int = 25,
    sort: SortBy = "score",
    calibration_table: CalibrationTable | None = None,
    session_id_filter: str | None = None,
) -> list[FlaggedSession]:
    """Return flagged sessions ordered by ``sort`` (descending), capped
    at ``top``. Both gates apply: score >= ``min_score`` AND distinct
    signal-type count >= ``min_signals``.

    When ``session_id_filter`` is set, scope/score gates are bypassed
    so ``--session ID`` can drill into a single session regardless of
    flag status (useful for investigating a session you remember).
    """
    if calibration_table is None:
        calibration_table = build_calibration_table(conn)

    # Each filter dimension uses a ``? IS NULL OR ...`` NULL-guard so
    # the SQL body is a single literal string (no f-string / no concat
    # of variable fragments). User-supplied values still flow through
    # parameterized ``?`` placeholders. NULL means "no filter on this
    # dimension"; ``--session ID`` flips the session_id filter on and
    # leaves the others NULL since drill-in bypasses the date scope.
    since_p, until_p, sid_p = _scope_params(since, until, session_id_filter)
    if sid_p is not None:
        since_p = None
        until_p = None
    rows = conn.execute(
        _FLAGGED_SESSIONS_SQL,
        (since_p, since_p, until_p, until_p, sid_p, sid_p),
    ).fetchall()

    out: list[FlaggedSession] = []
    for (
        sid,
        summary,
        observed,
        turns,
        duration,
        last_active,
        score,
        score_ver,
        esc_json,
    ) in rows:
        if score is None:
            if session_id_filter is None:
                continue
            score = 0.0
            score_ver = SIGNAL_VERSION

        signals = _load_signals(conn, sid)
        if session_id_filter is None:
            if score < min_score:
                continue
            if len({s["signal_type"] for s in signals}) < min_signals:
                continue

        primary = _primary_model_for_session(conn, sid) or "unknown"
        escalation = parse_escalation_event(esc_json)
        cf: Counterfactual | None = None
        if calibration_table:
            cf = compute_counterfactual(
                observed_cost_usd=float(observed or 0.0),
                table=calibration_table,
                from_model=primary,
            )
        out.append(
            FlaggedSession(
                session_id=sid,
                summary=(summary or "").strip() or "(no summary)",
                primary_model=primary,
                thrash_score=float(score),
                thrash_score_version=int(score_ver or SIGNAL_VERSION),
                turns=int(turns or 0),
                wall_clock_seconds=int(duration or 0),
                observed_cost_usd=float(observed or 0.0),
                started_at=int(last_active or 0),
                signals=signals,
                escalation_event=escalation,
                counterfactual=cf,
            )
        )

    out.sort(key=lambda s: _sort_key(s, sort), reverse=True)
    return out[:top]


def _load_signals(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT signal_type, count, evidence
             FROM session_signals
            WHERE session_id = ?
            ORDER BY signal_type""",
        (session_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for sig_type, count, evidence_json in rows:
        try:
            evidence = json.loads(evidence_json) if evidence_json else {}
        except (TypeError, ValueError):
            evidence = {}
        out.append({"signal_type": sig_type, "count": count, "evidence": evidence})
    return out


def _primary_model_for_session(conn: sqlite3.Connection, session_id: str) -> str | None:
    """Pick the most-frequent model across the session's main file
    rows in ``messages``. Falls back to None if no model rows exist."""
    row = conn.execute(
        """SELECT m.model
             FROM messages m
             JOIN files f ON m.file_path = f.path
            WHERE m.session_id = ? AND f.kind = 'main' AND m.model IS NOT NULL
            GROUP BY m.model
            ORDER BY COUNT(*) DESC
            LIMIT 1""",
        (session_id,),
    ).fetchone()
    return row[0] if row else None


def _sort_key(session: FlaggedSession, sort: SortBy) -> float:
    if sort == "observed_cost":
        return session.observed_cost_usd
    if sort == "est_savings_mid":
        if session.counterfactual is None:
            return 0.0
        return max(0.0, session.observed_cost_usd - session.counterfactual.est_cost_mid_usd)
    if sort == "est_savings_high":
        if session.counterfactual is None:
            return 0.0
        return max(0.0, session.observed_cost_usd - session.counterfactual.est_cost_low_usd)
    return session.thrash_score


# ---------- headline aggregate ----------


def compute_headline(
    sessions: list[FlaggedSession],
    *,
    scope_label: str,
    calibration_table: CalibrationTable,
) -> HeadlineAggregate:
    n_total = len(sessions)
    total_turns = sum(s.turns for s in sessions)
    total_observed = sum(s.observed_cost_usd for s in sessions)
    cfs = [s.counterfactual for s in sessions]
    valid = [c for c in cfs if c is not None and c.sanity_gate_passed]
    if valid:
        total_low: float | None = round(sum(c.est_cost_low_usd for c in valid), 6)
        total_mid: float | None = round(sum(c.est_cost_mid_usd for c in valid), 6)
        total_high: float | None = round(sum(c.est_cost_high_usd for c in valid), 6)
    else:
        total_low = total_mid = total_high = None

    return HeadlineAggregate(
        scope_label=scope_label,
        scored_with_version=SIGNAL_VERSION,
        n_flagged=n_total,
        total_turns=total_turns,
        total_observed_cost_usd=round(total_observed, 6),
        total_est_low_usd=total_low,
        total_est_mid_usd=total_mid,
        total_est_high_usd=total_high,
        sanity_warning=aggregate_sanity_gate_warning(cfs),
        n_calibration_pairs=len(calibration_table),
    )


# ---------- text rendering ----------


CAVEATS_FOOTER = (
    "Caveats: cache-priming + selection bias may understate Opus cost on "
    "cold-start equivalent. Counterfactual is 'what user experienced when "
    "they did escalate', not 'what would have happened if they'd started "
    "on Opus'. See `ccforensics thrash --help` for full caveats."
)


def render_text(
    headline: HeadlineAggregate,
    sessions: list[FlaggedSession],
    *,
    show_evidence: bool = False,
) -> RenderableType:
    """Render the headline + per-session table. ``show_evidence``
    expands each session row to display every fired signal's evidence
    JSON underneath."""
    title = Text(
        f"ccforensics thrash — {headline.scope_label} "
        f"[scored with v{headline.scored_with_version}]",
        style="bold",
    )

    if headline.n_flagged == 0:
        empty = Text(
            "No flagged sessions in scope.",
            style="dim",
        )
        return Group(title, Text(""), empty)

    headline_lines = [
        Text(""),
        Text(
            f"{headline.n_flagged} flagged sessions · "
            f"{headline.total_turns} turns · "
            f"{format_cost(headline.total_observed_cost_usd)} observed",
        ),
    ]
    if (
        headline.total_est_mid_usd is not None
        and headline.total_est_low_usd is not None
        and headline.total_est_high_usd is not None
    ):
        headline_lines.append(
            Text(
                f"Estimated Opus counterfactual: "
                f"{format_cost(headline.total_est_low_usd)}-"
                f"{format_cost(headline.total_est_high_usd)} "
                f"(mid {format_cost(headline.total_est_mid_usd)})",
            )
        )
        savings_low = max(0.0, headline.total_observed_cost_usd - headline.total_est_high_usd)
        savings_mid = max(0.0, headline.total_observed_cost_usd - headline.total_est_mid_usd)
        savings_high = max(0.0, headline.total_observed_cost_usd - headline.total_est_low_usd)
        headline_lines.append(
            Text(
                f"Implied savings if escalated earlier: ~"
                f"{format_cost(savings_low)}-{format_cost(savings_high)} "
                f"(mid {format_cost(savings_mid)})",
            )
        )
    else:
        headline_lines.append(
            Text(
                f"Counterfactual not rendered (calibration insufficient — "
                f"{headline.n_calibration_pairs} (from→to) pair(s) on file).",
                style="dim",
            )
        )
    if headline.sanity_warning:
        headline_lines.append(
            Text(
                "Warning: > 5% of flagged sessions failed cost-sanity gate. "
                "Calibration data may be drifting from your usage pattern.",
                style="yellow",
            )
        )

    table = _render_session_table(sessions)

    blocks: list[RenderableType] = [title, *headline_lines, Text(""), table]
    if show_evidence:
        blocks.append(Text(""))
        for sess in sessions:
            blocks.append(_render_evidence_block(sess))
            blocks.append(Text(""))
    blocks.append(Text(""))
    blocks.append(Text(CAVEATS_FOOTER, style="dim"))
    return Group(*blocks)


def _render_session_table(sessions: list[FlaggedSession]) -> Table:
    table = Table(box=box.SIMPLE_HEAVY, show_lines=False)
    table.add_column("SESSION", overflow="fold")
    table.add_column("MODEL")
    table.add_column("SCORE", justify="right")
    table.add_column("SIGNALS", justify="right")
    table.add_column("TURNS", justify="right")
    table.add_column("WALL", justify="right")
    table.add_column("OBSERVED", justify="right")
    table.add_column("OPUS EST (mid)", justify="right")
    table.add_column("CAL", justify="right")

    for s in sessions:
        table.add_row(
            f"{s.session_id[:8]} · {s.summary[:32]}",
            _short_model(s.primary_model),
            f"{s.thrash_score:.2f}",
            str(len({sig["signal_type"] for sig in s.signals})),
            str(s.turns),
            format_duration(s.wall_clock_seconds),
            format_cost(s.observed_cost_usd),
            _format_counterfactual(s),
            _format_cal_marker(s),
        )
    return table


def _short_model(model: str) -> str:
    return model.replace("claude-", "").replace("-20250929", "").replace("-20251001", "")


def _format_counterfactual(session: FlaggedSession) -> str:
    cf = session.counterfactual
    if cf is None:
        return "[dim]insufficient cal[/dim]"
    if not cf.sanity_gate_passed:
        return "[yellow]sanity-gate failed[/yellow]"
    return (
        f"{format_cost(cf.est_cost_low_usd)}-"
        f"{format_cost(cf.est_cost_high_usd)} "
        f"({format_cost(cf.est_cost_mid_usd)})"
    )


def _format_cal_marker(session: FlaggedSession) -> str:
    cf = session.counterfactual
    if cf is None:
        return "[dim]—[/dim]"
    return f"{cf.n_calibration_events}{cf.calibration_confidence[0]}"


def _render_evidence_block(session: FlaggedSession) -> RenderableType:
    """Per-session evidence expansion. Lists every fired signal w/
    its count + a one-line evidence summary."""
    lines: list[RenderableType] = []
    title = Text(
        f"{session.session_id[:8]} · {session.summary[:48]} — "
        f"score {session.thrash_score:.2f} "
        f"[{_short_model(session.primary_model)}, {session.turns} turns, "
        f"{format_duration(session.wall_clock_seconds)} wall-clock]",
        style="bold",
    )
    lines.append(title)
    for sig in session.signals:
        lines.append(
            Text(f"  ├─ {sig['signal_type']:24s} (count {sig['count']})  {_evidence_summary(sig)}")
        )
    if session.counterfactual is not None and session.counterfactual.sanity_gate_passed:
        cf = session.counterfactual
        lines.append(
            Text(
                f"  Counterfactual: {format_cost(session.observed_cost_usd)} "
                f"observed vs {format_cost(cf.est_cost_low_usd)}-"
                f"{format_cost(cf.est_cost_high_usd)} est Opus "
                f"(mid {format_cost(cf.est_cost_mid_usd)}, "
                f"n={cf.n_calibration_events} [{cf.calibration_confidence} confidence])"
            )
        )
    if session.escalation_event is not None:
        ev = session.escalation_event
        lines.append(
            Text(
                f"  Escalation event ({ev.get('escalation_kind')}): "
                f"{ev.get('from_model')} → {ev.get('to_model')} "
                f"at turn {ev.get('turn_index')}, "
                f"resolution: {ev.get('resolution_marker')}"
            )
        )
    return Group(*lines)


def _evidence_summary(signal: dict[str, Any]) -> str:
    """One-line human summary of evidence per signal_type."""
    ev = signal["evidence"]
    sig_type = signal["signal_type"]
    if sig_type == "repeated_edit":
        return (
            f"{ev.get('file_path')} — {ev.get('edit_count')} edits "
            f"(turns {ev.get('first_turn')}-{ev.get('last_turn')})"
        )
    if sig_type == "repeated_error":
        return f'"{(ev.get("error_excerpt") or "")[:80]}" x{ev.get("occurrences")}'
    if sig_type == "user_correction":
        return f"matches {ev.get('matches')} on turns {ev.get('turn_indices')}"
    if sig_type == "tool_arg_churn":
        return (
            f"{ev.get('tool_name')} repeated {ev.get('repeats')}x (turns {ev.get('turn_indices')})"
        )
    if sig_type == "novelty_window":
        return (
            f"flat run {ev.get('max_flat_run')} turns "
            f"({ev.get('from_turn')}-{ev.get('to_turn')}); "
            f"text-jaccard {ev.get('text_jaccard_max')}"
        )
    if sig_type == "turn_cost_acceleration":
        return (
            f"slope {ev.get('slope_output_tokens_per_turn')} tokens/turn, r²={ev.get('r_squared')}"
        )
    if sig_type == "session_abandoned":
        return (
            f"{ev.get('total_turns')} turns; "
            f"last_role={ev.get('last_role')}; "
            f"last_tool_error={ev.get('last_tool_error')}"
        )
    if sig_type == "placeholder_emit":
        return f"{ev.get('files')} on turns {ev.get('turn_indices')}"
    if sig_type == "trajectory_length_zscore":
        return (
            f"{ev.get('session_turns')} turns vs baseline "
            f"{ev.get('user_baseline_mean_turns')}±"
            f"{ev.get('user_baseline_stddev')} (z={ev.get('z_score')})"
        )
    if sig_type == "test_regression":
        return (
            f"{ev.get('command_excerpt')} "
            f"({ev.get('fail_count_before')}→{ev.get('fail_count_after')} fails)"
        )
    return json.dumps(ev, sort_keys=True)[:100]


# ---------- JSON / CSV ----------


def render_json(headline: HeadlineAggregate, sessions: list[FlaggedSession]) -> str:
    payload = {
        "headline": _headline_to_dict(headline),
        "rows": [_session_to_dict(s) for s in sessions],
    }
    return json.dumps(payload, sort_keys=True, indent=2)


def _headline_to_dict(h: HeadlineAggregate) -> dict[str, Any]:
    return {
        "scope_label": h.scope_label,
        "scored_with_version": h.scored_with_version,
        "n_flagged": h.n_flagged,
        "total_turns": h.total_turns,
        "total_observed_cost_usd": h.total_observed_cost_usd,
        "total_est_opus_cost_low_usd": h.total_est_low_usd,
        "total_est_opus_cost_mid_usd": h.total_est_mid_usd,
        "total_est_opus_cost_high_usd": h.total_est_high_usd,
        "sanity_warning": h.sanity_warning,
        "n_calibration_pairs": h.n_calibration_pairs,
        "sanity_aggregate_threshold": SANITY_AGGREGATE_FAIL_THRESHOLD,
    }


def _session_to_dict(s: FlaggedSession) -> dict[str, Any]:
    return {
        "session_id": s.session_id,
        "summary": s.summary,
        "primary_model": s.primary_model,
        "thrash_score": s.thrash_score,
        "thrash_score_version": s.thrash_score_version,
        "signals": s.signals,
        "turns": s.turns,
        "wall_clock_seconds": s.wall_clock_seconds,
        "observed_cost_usd": s.observed_cost_usd,
        "escalation_event": s.escalation_event,
        "counterfactual": _cf_to_dict(s.counterfactual) if s.counterfactual else None,
    }


def _cf_to_dict(cf: Counterfactual) -> dict[str, Any]:
    return {
        "to_model": cf.to_model,
        "est_cost_low_usd": cf.est_cost_low_usd,
        "est_cost_mid_usd": cf.est_cost_mid_usd,
        "est_cost_high_usd": cf.est_cost_high_usd,
        "n_calibration_events": cf.n_calibration_events,
        "calibration_confidence": cf.calibration_confidence,
        "sanity_gate_passed": cf.sanity_gate_passed,
    }


def render_csv(sessions: list[FlaggedSession]) -> str:
    """CSV output — one row per session. Headline aggregate is text-only
    (one-line aggregates don't fit a row schema)."""
    buf = io.StringIO()
    fields = [
        "session_id",
        "summary",
        "primary_model",
        "thrash_score",
        "thrash_score_version",
        "n_signal_types",
        "turns",
        "wall_clock_seconds",
        "observed_cost_usd",
        "est_cost_low_usd",
        "est_cost_mid_usd",
        "est_cost_high_usd",
        "calibration_confidence",
        "n_calibration_events",
        "sanity_gate_passed",
        "escalation_kind",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for s in sessions:
        cf = s.counterfactual
        writer.writerow(
            {
                "session_id": s.session_id,
                "summary": s.summary,
                "primary_model": s.primary_model,
                "thrash_score": f"{s.thrash_score:.4f}",
                "thrash_score_version": s.thrash_score_version,
                "n_signal_types": len({sig["signal_type"] for sig in s.signals}),
                "turns": s.turns,
                "wall_clock_seconds": s.wall_clock_seconds,
                "observed_cost_usd": f"{s.observed_cost_usd:.6f}",
                "est_cost_low_usd": f"{cf.est_cost_low_usd:.6f}" if cf else "",
                "est_cost_mid_usd": f"{cf.est_cost_mid_usd:.6f}" if cf else "",
                "est_cost_high_usd": f"{cf.est_cost_high_usd:.6f}" if cf else "",
                "calibration_confidence": cf.calibration_confidence if cf else "",
                "n_calibration_events": cf.n_calibration_events if cf else "",
                "sanity_gate_passed": cf.sanity_gate_passed if cf else "",
                "escalation_kind": (
                    s.escalation_event.get("escalation_kind") if s.escalation_event else ""
                ),
            }
        )
    return buf.getvalue()
