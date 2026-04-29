"""Per-plugin cost and session rollup."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from rich import box
from rich.table import Table

from ..attribution import _BUCKET_KIND_EXPR, _BUCKET_NAME_EXPR
from ..registry import (
    classify_agent_source,
    load_plugin_names,
    load_user_level_agent_names,
)
from ._format import format_cost


@dataclass
class PluginRollup:
    plugin: str
    total_cost_usd: float
    session_count: int
    most_used_agent_type: str | None
    agent_type_count: int
    most_used_skill: str | None
    skill_count: int
    most_used_model: str | None
    model_count: int
    first_seen: int | None
    last_seen: int | None


def _base_filters(since: datetime | None, until: datetime | None) -> tuple[list[str], list[object]]:
    where: list[str] = []
    params: list[object] = []
    if since:
        where.append("ss.last_active_at >= ?")
        params.append(int(since.timestamp()))
    if until:
        where.append("ss.last_active_at <= ?")
        params.append(int(until.timestamp()))
    return where, params


@dataclass
class _Slot:
    cost: float = 0.0
    sessions: set[str] = field(default_factory=set)
    type_cost: dict[str, float] = field(default_factory=dict)
    model_cost: dict[str, float] = field(default_factory=dict)
    first_seen: int | None = None
    last_seen: int | None = None


def query_plugins(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    model: str | None = None,
) -> list[PluginRollup]:
    """Per-plugin rollup within the optional date window.

    Includes ``user-level`` and ``builtin`` synthetic sources so reports
    surface all spawn cost, not just plugin-attributable cost. When
    ``model`` is set, cost / sessions / top-model are restricted to messages
    whose model matches the substring (case-insensitive); top-skill is left
    unfiltered because skill activations have no model dimension and the
    activation count is conceptually orthogonal to billable model cost.
    """
    where, params = _base_filters(since, until)
    # Exclude NULL models (infrastructure rows: queue-operation, progress,
    # system) and Claude Code's ``<...>`` placeholders so the top-model
    # ranking reflects real LLM calls only.
    where.append("m.model IS NOT NULL")
    where.append("m.model NOT LIKE '<%>'")
    if model:
        # Normalize ``.`` → ``-`` so ``opus-4.7`` matches the on-disk
        # ``claude-opus-4-7`` model string. ``opus`` / ``haiku`` are
        # unaffected since they have no dot to substitute.
        where.append("LOWER(m.model) LIKE ?")
        params.append(f"%{model.lower().replace('.', '-')}%")
    where_sql = " WHERE " + " AND ".join(where)

    plugins_known = load_plugin_names(conn)
    user_agents = load_user_level_agent_names(conn)

    # One pass over messages → bucket → plugin source. The bucket CASE
    # mirrors ``attribution._BUCKET_KIND_EXPR`` so the dollars on this
    # report tie out to the per-session rollup ledger.
    cost_sql = f"""
        SELECT {_BUCKET_KIND_EXPR} AS bk,
               {_BUCKET_NAME_EXPR} AS bn,
               m.session_id,
               m.cost_usd,
               m.model,
               ss.started_at,
               ss.last_active_at
          FROM messages m
          JOIN files f ON f.path = m.file_path
          LEFT JOIN subagent_spawns s ON s.child_file_path = f.path
          JOIN session_summaries ss ON ss.session_id = m.session_id
         {where_sql}
    """
    slots: dict[str, _Slot] = {}
    for row in conn.execute(cost_sql, params).fetchall():
        bk, bn, sid, cost, m_model, started_at, last_active_at = row
        if bk == "subagent":
            source = classify_agent_source(bn or "", plugins_known, user_agents)
        else:
            source = bk
        slot = slots.setdefault(source, _Slot())
        c = float(cost or 0.0)
        slot.cost += c
        slot.sessions.add(str(sid))
        if bk == "subagent" and bn:
            slot.type_cost[bn] = slot.type_cost.get(bn, 0.0) + c
        if m_model:
            slot.model_cost[m_model] = slot.model_cost.get(m_model, 0.0) + c
        if started_at is not None:
            slot.first_seen = (
                int(started_at)
                if slot.first_seen is None
                else min(slot.first_seen, int(started_at))
            )
        if last_active_at is not None:
            slot.last_seen = (
                int(last_active_at)
                if slot.last_seen is None
                else max(slot.last_seen, int(last_active_at))
            )

    # Most-used skill per plugin (from skill_activations). Not filtered by
    # ``model`` — skills have no model dimension, and a session that ran
    # opus messages may also have activated skills regardless of the cost
    # filter. Date window still applies via the join to session_summaries.
    skill_counts: dict[str, dict[str, int]] = {}
    where_skill: list[str] = []
    params_skill: list[object] = []
    if since:
        where_skill.append("ss.last_active_at >= ?")
        params_skill.append(int(since.timestamp()))
    if until:
        where_skill.append("ss.last_active_at <= ?")
        params_skill.append(int(until.timestamp()))
    skill_where_sql = " AND " + " AND ".join(where_skill) if where_skill else ""
    for row in conn.execute(
        f"""SELECT sa.plugin_name, sa.skill_name, COUNT(*)
              FROM skill_activations sa
              JOIN session_summaries ss ON ss.session_id = sa.session_id
             WHERE 1=1 {skill_where_sql}
             GROUP BY sa.plugin_name, sa.skill_name""",
        params_skill,
    ).fetchall():
        plugin, skill, count = row
        key = plugin or "user-level"
        skill_counts.setdefault(key, {})
        skill_counts[key][skill] = skill_counts[key].get(skill, 0) + int(count)

    out: list[PluginRollup] = []
    for source, slot in slots.items():
        if slot.type_cost:
            type_name, _ = max(slot.type_cost.items(), key=lambda kv: kv[1])
            type_n = len({bn for bn in slot.type_cost})
        else:
            type_name, type_n = None, 0
        if slot.model_cost:
            top_model, _ = max(slot.model_cost.items(), key=lambda kv: kv[1])
            top_model_n = len(slot.model_cost)
        else:
            top_model, top_model_n = None, 0
        skill_counter = skill_counts.get(source, {})
        if skill_counter:
            skill_name, skill_n = max(skill_counter.items(), key=lambda kv: kv[1])
        else:
            skill_name, skill_n = None, 0
        out.append(
            PluginRollup(
                plugin=source,
                total_cost_usd=slot.cost,
                session_count=len(slot.sessions),
                most_used_agent_type=type_name,
                agent_type_count=type_n,
                most_used_skill=skill_name,
                skill_count=skill_n,
                most_used_model=top_model,
                model_count=top_model_n,
                first_seen=slot.first_seen,
                last_seen=slot.last_seen,
            )
        )
    out.sort(key=lambda r: -r.total_cost_usd)
    return out


def render_plugins(rows: list[PluginRollup]) -> Table:
    t = Table(
        title="Plugin rollup",
        title_style="bold",
        box=box.HEAVY_HEAD,
        show_lines=True,
    )
    t.add_column("plugin", style="magenta")
    t.add_column("cost", justify="right")
    t.add_column("sessions", justify="right")
    t.add_column("top model")
    t.add_column("top subagent_type")
    t.add_column("top skill")
    t.add_column("first seen", style="dim")
    t.add_column("last seen", style="dim")
    for r in rows:
        first = (
            datetime.fromtimestamp(r.first_seen, tz=UTC).strftime("%Y-%m-%d")
            if r.first_seen is not None
            else "-"
        )
        last = (
            datetime.fromtimestamp(r.last_seen, tz=UTC).strftime("%Y-%m-%d")
            if r.last_seen is not None
            else "-"
        )
        agent = (
            f"{r.most_used_agent_type} ({r.agent_type_count})" if r.most_used_agent_type else "-"
        )
        skill = f"{r.most_used_skill} ({r.skill_count})" if r.most_used_skill else "-"
        model_cell = f"{r.most_used_model} ({r.model_count})" if r.most_used_model else "-"
        t.add_row(
            r.plugin,
            format_cost(r.total_cost_usd),
            str(r.session_count),
            model_cell,
            agent,
            skill,
            first,
            last,
        )
    if rows:
        # ``sessions`` totals the per-row count rather than distinct global
        # sessions, mirroring the aggregate-table convention. Same session can
        # contribute to multiple plugin sources (one main bucket + one
        # subagent bucket), so a deduped count would be lower than the sum
        # of what's displayed.
        t.add_section()
        t.add_row(
            "Totals",
            format_cost(sum(r.total_cost_usd for r in rows)),
            f"{sum(r.session_count for r in rows):,}",
            "",
            "",
            "",
            "",
            "",
            style="bold",
        )
    return t
