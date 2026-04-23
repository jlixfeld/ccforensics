"""Per-plugin cost and session rollup."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.table import Table

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


def query_plugins(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[PluginRollup]:
    """Per-plugin rollup within the optional date window.

    Includes ``user-level`` and ``builtin`` synthetic sources so reports
    surface all spawn cost, not just plugin-attributable cost.
    """
    where, params = _base_filters(since, until)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    plugins_known = load_plugin_names(conn)
    user_agents = load_user_level_agent_names(conn)

    @dataclass
    class _Slot:
        cost: float = 0.0
        first_seen: int | None = None
        last_seen: int | None = None

    # Cost + agent-type frequency, grouped by bucket.
    cost_sql = f"""
        SELECT r.bucket_kind, r.bucket_name,
               SUM(r.cost_usd) AS cost,
               COUNT(DISTINCT r.session_id) AS sessions,
               MIN(ss.started_at) AS first_seen,
               MAX(ss.last_active_at) AS last_seen
          FROM session_rollups r
          JOIN session_summaries ss ON ss.session_id = r.session_id
         {where_sql}
         GROUP BY r.bucket_kind, r.bucket_name
    """
    cost_by_source: dict[str, _Slot] = {}
    type_counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(cost_sql, params).fetchall():
        bk, bn, cost, sessions, first_seen, last_seen = row
        if bk == "subagent":
            source = classify_agent_source(bn or "", plugins_known, user_agents)
        else:
            source = bk
        slot = cost_by_source.setdefault(source, _Slot())
        slot.cost += float(cost or 0.0)
        if first_seen is not None:
            slot.first_seen = (
                int(first_seen)
                if slot.first_seen is None
                else min(slot.first_seen, int(first_seen))
            )
        if last_seen is not None:
            slot.last_seen = (
                int(last_seen) if slot.last_seen is None else max(slot.last_seen, int(last_seen))
            )
        if bk == "subagent" and bn:
            type_counts.setdefault(source, {})
            type_counts[source][bn] = type_counts[source].get(bn, 0) + int(sessions or 0)

    # Precise session_count per source (bucket-collapsed).
    session_sql = f"""
        SELECT r.bucket_kind, r.bucket_name, r.session_id
          FROM session_rollups r
          JOIN session_summaries ss ON ss.session_id = r.session_id
         {where_sql}
    """
    sessions_by_source: dict[str, set[str]] = {}
    for row in conn.execute(session_sql, params).fetchall():
        bk, bn, sid = row
        source = (
            classify_agent_source(bn or "", plugins_known, user_agents) if bk == "subagent" else bk
        )
        sessions_by_source.setdefault(source, set()).add(str(sid))

    # Most-used skill per plugin (from skill_activations).
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
    for source, slot in cost_by_source.items():
        type_counter = type_counts.get(source, {})
        if type_counter:
            type_name, type_n = max(type_counter.items(), key=lambda kv: kv[1])
        else:
            type_name, type_n = None, 0
        skill_counter = skill_counts.get(source, {})
        if skill_counter:
            skill_name, skill_n = max(skill_counter.items(), key=lambda kv: kv[1])
        else:
            skill_name, skill_n = None, 0
        out.append(
            PluginRollup(
                plugin=source,
                total_cost_usd=slot.cost,
                session_count=len(sessions_by_source.get(source, set())),
                most_used_agent_type=type_name,
                agent_type_count=type_n,
                most_used_skill=skill_name,
                skill_count=skill_n,
                first_seen=slot.first_seen,
                last_seen=slot.last_seen,
            )
        )
    out.sort(key=lambda r: -r.total_cost_usd)
    return out


def render_plugins(rows: list[PluginRollup]) -> Table:
    t = Table(title="Plugin rollup", show_edge=False)
    t.add_column("plugin", style="magenta")
    t.add_column("cost", justify="right")
    t.add_column("sessions", justify="right")
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
        t.add_row(
            r.plugin,
            format_cost(r.total_cost_usd),
            str(r.session_count),
            agent,
            skill,
            first,
            last,
        )
    return t
