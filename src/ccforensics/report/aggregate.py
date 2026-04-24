"""Aggregate cost across a date range, grouped by project / plugin / day / none."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from rich.table import Table

from ..registry import (
    classify_agent_source,
    load_plugin_names,
    load_user_level_agent_names,
)
from ._format import format_cost

GroupBy = Literal["none", "project", "day", "week", "month", "plugin", "model"]


@dataclass
class AggregateRow:
    group_key: str
    total_cost_usd: float
    session_count: int
    input_tokens: int
    output_tokens: int
    cache_create: int
    cache_read: int


_GROUP_TIME_FMT: dict[str, str] = {
    # strftime patterns sqlite supports. Inputs are unix epoch seconds.
    "day": "%Y-%m-%d",
    "week": "%Y-W%W",
    "month": "%Y-%m",
}


def _base_filters(
    since: datetime | None, until: datetime | None, project: str | None
) -> tuple[list[str], list[object]]:
    where: list[str] = []
    params: list[object] = []
    if since:
        where.append("ss.last_active_at >= ?")
        params.append(int(since.timestamp()))
    if until:
        where.append("ss.last_active_at <= ?")
        params.append(int(until.timestamp()))
    if project:
        where.append("LOWER(IFNULL(ss.project_path,'')) LIKE ?")
        params.append(f"%{project.lower()}%")
    return where, params


def _query_messages_aggregate(
    conn: sqlite3.Connection,
    *,
    since: datetime | None,
    until: datetime | None,
    project: str | None,
    model: str | None,
    group_expr: str,
    order_expr: str,
) -> list[AggregateRow]:
    """Aggregate directly from ``messages`` joined to ``session_summaries``.

    Used whenever the caller wants a per-model view — either ``group_by='model'``
    or a ``--model`` filter on any non-plugin group-by. ``session_rollups`` has
    no model dimension, so the path through ``messages`` is authoritative.
    NULL ``model`` (infrastructure rows: queue-operation, progress, etc.) is
    excluded; those have $0 cost anyway.
    """
    where, params = _base_filters(since, until, project)
    where.append("m.model IS NOT NULL")
    if model:
        where.append("LOWER(m.model) LIKE ?")
        params.append(f"%{model.lower()}%")
    sql = f"""
        SELECT {group_expr} AS group_key,
               COALESCE(SUM(m.cost_usd), 0) AS cost,
               COUNT(DISTINCT m.session_id) AS sessions,
               COALESCE(SUM(m.input_tokens), 0),
               COALESCE(SUM(m.output_tokens), 0),
               COALESCE(SUM(m.cache_creation), 0),
               COALESCE(SUM(m.cache_read), 0)
          FROM messages m
          JOIN session_summaries ss ON ss.session_id = m.session_id
         WHERE {" AND ".join(where)}
         GROUP BY group_key
         ORDER BY {order_expr}
    """
    rows = conn.execute(sql, params).fetchall()
    return [
        AggregateRow(
            group_key=str(r[0]) if r[0] is not None else "(all)",
            total_cost_usd=float(r[1] or 0.0),
            session_count=int(r[2] or 0),
            input_tokens=int(r[3] or 0),
            output_tokens=int(r[4] or 0),
            cache_create=int(r[5] or 0),
            cache_read=int(r[6] or 0),
        )
        for r in rows
    ]


def query_aggregate(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    project: str | None = None,
    model: str | None = None,
    group_by: GroupBy = "none",
) -> list[AggregateRow]:
    """Aggregate session_rollups sums over a window, grouped by key.

    ``group_by='plugin'`` expands ``subagent:<type>`` into the owning
    plugin via the registry; other groupings sum raw rollup rows
    without plugin expansion.

    When ``group_by='model'`` or ``model`` is set, the aggregation runs from
    the ``messages`` table directly — ``session_rollups`` has no model
    dimension, so a filter or grouping by model must use per-message cost.
    Combining ``model`` with ``group_by='plugin'`` is rejected: plugin
    bucketing and the model dimension share no common aggregation path in v1.
    """
    if model is not None and group_by == "plugin":
        raise ValueError(
            "--model filter is not compatible with --group-by plugin; "
            "plugin bucketing routes through session_rollups which has no "
            "model dimension"
        )

    if group_by == "model":
        return _query_messages_aggregate(
            conn,
            since=since,
            until=until,
            project=project,
            model=model,
            group_expr="m.model",
            order_expr="cost DESC",
        )
    if model is not None:
        # Non-plugin group-by + model filter: route through messages with the
        # appropriate group expression.
        if group_by == "none":
            return _query_messages_aggregate(
                conn,
                since=since,
                until=until,
                project=project,
                model=model,
                group_expr="'(all)'",
                order_expr="cost DESC",
            )
        if group_by == "project":
            return _query_messages_aggregate(
                conn,
                since=since,
                until=until,
                project=project,
                model=model,
                group_expr="COALESCE(ss.project_path, '<unknown>')",
                order_expr="cost DESC",
            )
        if group_by in _GROUP_TIME_FMT:
            fmt = _GROUP_TIME_FMT[group_by]
            # Embed the strftime literal directly (safe — fixed whitelist).
            return _query_messages_aggregate(
                conn,
                since=since,
                until=until,
                project=project,
                model=model,
                group_expr=f"strftime('{fmt}', ss.last_active_at, 'unixepoch')",
                order_expr="group_key",
            )

    where, params = _base_filters(since, until, project)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    if group_by == "none":
        sql = f"""
            SELECT '(all)' AS group_key,
                   COALESCE(SUM(r.cost_usd), 0) AS cost,
                   COUNT(DISTINCT ss.session_id) AS sessions,
                   COALESCE(SUM(r.input_tokens), 0),
                   COALESCE(SUM(r.output_tokens), 0),
                   COALESCE(SUM(r.cache_create), 0),
                   COALESCE(SUM(r.cache_read), 0)
              FROM session_summaries ss
         LEFT JOIN session_rollups r ON r.session_id = ss.session_id
            {where_sql}
        """
        rows = conn.execute(sql, params).fetchall()
    elif group_by == "project":
        sql = f"""
            SELECT COALESCE(ss.project_path, '<unknown>') AS group_key,
                   COALESCE(SUM(r.cost_usd), 0) AS cost,
                   COUNT(DISTINCT ss.session_id) AS sessions,
                   COALESCE(SUM(r.input_tokens), 0),
                   COALESCE(SUM(r.output_tokens), 0),
                   COALESCE(SUM(r.cache_create), 0),
                   COALESCE(SUM(r.cache_read), 0)
              FROM session_summaries ss
         LEFT JOIN session_rollups r ON r.session_id = ss.session_id
            {where_sql}
          GROUP BY group_key
          ORDER BY cost DESC
        """
        rows = conn.execute(sql, params).fetchall()
    elif group_by in _GROUP_TIME_FMT:
        fmt = _GROUP_TIME_FMT[group_by]
        sql = f"""
            SELECT strftime(?, ss.last_active_at, 'unixepoch') AS group_key,
                   COALESCE(SUM(r.cost_usd), 0) AS cost,
                   COUNT(DISTINCT ss.session_id) AS sessions,
                   COALESCE(SUM(r.input_tokens), 0),
                   COALESCE(SUM(r.output_tokens), 0),
                   COALESCE(SUM(r.cache_create), 0),
                   COALESCE(SUM(r.cache_read), 0)
              FROM session_summaries ss
         LEFT JOIN session_rollups r ON r.session_id = ss.session_id
            {where_sql}
          GROUP BY group_key
          ORDER BY group_key
        """
        rows = conn.execute(sql, [fmt, *params]).fetchall()
    elif group_by == "plugin":
        plugins = load_plugin_names(conn)
        user_agents = load_user_level_agent_names(conn)
        sql = f"""
            SELECT r.bucket_kind, r.bucket_name,
                   ss.session_id,
                   r.cost_usd, r.input_tokens, r.output_tokens,
                   r.cache_create, r.cache_read
              FROM session_summaries ss
         LEFT JOIN session_rollups r ON r.session_id = ss.session_id
            {where_sql}
        """
        buckets: dict[str, list[float | int]] = {}
        session_ids: dict[str, set[str]] = {}
        for r in conn.execute(sql, params).fetchall():
            bk, bn, sid, cost, inp, out, cc, cr = r
            if bk is None:
                continue
            key = classify_agent_source(bn or "", plugins, user_agents) if bk == "subagent" else bk
            bucket = buckets.setdefault(key, [0.0, 0, 0, 0, 0])
            bucket[0] += float(cost or 0.0)
            bucket[1] += int(inp or 0)
            bucket[2] += int(out or 0)
            bucket[3] += int(cc or 0)
            bucket[4] += int(cr or 0)
            session_ids.setdefault(key, set()).add(str(sid))
        return [
            AggregateRow(
                group_key=key,
                total_cost_usd=float(totals[0]),
                session_count=len(session_ids.get(key, set())),
                input_tokens=int(totals[1]),
                output_tokens=int(totals[2]),
                cache_create=int(totals[3]),
                cache_read=int(totals[4]),
            )
            for key, totals in sorted(buckets.items(), key=lambda kv: -float(kv[1][0]))
        ]
    else:
        raise ValueError(f"unknown group_by value: {group_by!r}")

    return [
        AggregateRow(
            group_key=str(r[0]),
            total_cost_usd=float(r[1] or 0.0),
            session_count=int(r[2] or 0),
            input_tokens=int(r[3] or 0),
            output_tokens=int(r[4] or 0),
            cache_create=int(r[5] or 0),
            cache_read=int(r[6] or 0),
        )
        for r in rows
    ]


def render_aggregate(rows: list[AggregateRow], group_by: str) -> Table:
    t = Table(title=f"Aggregate (group-by: {group_by})", show_edge=False)
    t.add_column("group", style="cyan")
    t.add_column("cost", justify="right")
    t.add_column("sessions", justify="right")
    t.add_column("in", justify="right")
    t.add_column("out", justify="right")
    t.add_column("cache_create", justify="right")
    t.add_column("cache_read", justify="right")
    for r in rows:
        t.add_row(
            r.group_key,
            format_cost(r.total_cost_usd),
            str(r.session_count),
            f"{r.input_tokens:,}",
            f"{r.output_tokens:,}",
            f"{r.cache_create:,}",
            f"{r.cache_read:,}",
        )
    return t
