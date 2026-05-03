"""Aggregate cost across a date range, grouped by project / plugin / day / none."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from rich import box
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..pricing import resolve_pricing
from ..registry import (
    classify_agent_source,
    load_plugin_names,
    load_user_level_agent_names,
)
from ._cache import CacheRow, cache_metrics
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
    # Exclude NULL (infrastructure rows — queue-operation, progress, system)
    # and ``<...>`` placeholders (Claude Code writes e.g. ``<synthetic>`` on
    # non-LLM-call assistant stubs — those aren't real models).
    where.append("m.model IS NOT NULL")
    where.append("m.model NOT LIKE '<%>'")
    if model:
        # Normalize ``.`` → ``-`` so ``opus-4.7`` matches the on-disk
        # ``claude-opus-4-7`` model string. ``opus`` / ``haiku`` are
        # unaffected since they have no dot to substitute.
        where.append("LOWER(m.model) LIKE ?")
        params.append(f"%{model.lower().replace('.', '-')}%")
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


@dataclass
class AggregateReport:
    """Envelope for the ``aggregate`` command — the existing per-group rows
    plus scoped cache + service-tier metrics. The cache fields and
    ``service_tier_breakdown`` sit at the top level per spec
    (``docs/specs/design.md``); JSON output emits them as flat keys
    alongside ``rows``."""

    rows: list[AggregateRow]
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_eff_pct: float = 0.0
    cache_savings_usd: float = 0.0
    cache_excluded_unknown_models: int = 0
    service_tier_breakdown: dict[str, int] = field(default_factory=dict)


def _aggregate_cache_metrics(
    conn: sqlite3.Connection,
    *,
    since: datetime | None,
    until: datetime | None,
    project: str | None,
    model: str | None,
    pricing_data: dict[str, Any] | None,
) -> tuple[int, int, float, float, int]:
    """Cache token totals + cost-derived metrics over the same scope as
    ``query_aggregate``. Joins ``messages`` to ``session_summaries`` for the
    date / project filters and re-applies the ``--model`` filter when set,
    so the metrics line up with whichever rows the table already shows.
    Excludes ``model IS NULL`` (infrastructure rows) and Claude Code's
    ``<...>`` placeholders for the same reasons as the per-message
    aggregation in ``_query_messages_aggregate``.
    """
    where, params = _base_filters(since, until, project)
    where.append("m.model IS NOT NULL")
    where.append("m.model NOT LIKE '<%>'")
    if model:
        where.append("LOWER(m.model) LIKE ?")
        params.append(f"%{model.lower().replace('.', '-')}%")
    sql = f"""
        SELECT m.model,
               COALESCE(SUM(m.input_tokens), 0),
               COALESCE(SUM(m.cache_creation), 0),
               COALESCE(SUM(m.cache_read), 0)
          FROM messages m
          JOIN session_summaries ss ON ss.session_id = m.session_id
         WHERE {" AND ".join(where)}
         GROUP BY m.model
    """
    rows_data = conn.execute(sql, params).fetchall()
    rows = [
        CacheRow(
            model=str(m),
            input_tokens=int(i or 0),
            cache_creation=int(cc or 0),
            cache_read=int(cr or 0),
        )
        for (m, i, cc, cr) in rows_data
    ]
    cache_read_total = sum(r.cache_read for r in rows)
    cache_create_total = sum(r.cache_creation for r in rows)
    if pricing_data is None:
        return (cache_read_total, cache_create_total, 0.0, 0.0, 0)
    metrics = cache_metrics(rows, lambda mdl: resolve_pricing(mdl, pricing_data))
    return (
        cache_read_total,
        cache_create_total,
        metrics.eff_pct,
        metrics.savings_usd,
        metrics.rows_excluded_for_unknown_model,
    )


def _aggregate_service_tier_breakdown(
    conn: sqlite3.Connection,
    *,
    since: datetime | None,
    until: datetime | None,
    project: str | None,
    model: str | None,
) -> dict[str, int]:
    """Assistant-only service_tier counts. ``role='assistant'`` because
    user / tool_result messages don't carry a tier and inflating the
    count with them would mislead. NULL → 'unknown'."""
    where, params = _base_filters(since, until, project)
    where.append("m.role='assistant'")
    if model:
        where.append("m.model IS NOT NULL")
        where.append("LOWER(m.model) LIKE ?")
        params.append(f"%{model.lower().replace('.', '-')}%")
    sql = f"""
        SELECT COALESCE(m.service_tier, 'unknown') AS tier, COUNT(*)
          FROM messages m
          JOIN session_summaries ss ON ss.session_id = m.session_id
         WHERE {" AND ".join(where)}
         GROUP BY tier
         ORDER BY tier
    """
    rows = conn.execute(sql, params).fetchall()
    return {str(t): int(c) for t, c in rows}


def query_aggregate_report(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    project: str | None = None,
    model: str | None = None,
    group_by: GroupBy = "none",
    pricing_data: dict[str, Any] | None = None,
) -> AggregateReport:
    """Wraps ``query_aggregate`` and adds scoped cache + tier metrics.

    The same date / project / model filters apply to all three queries so
    the footer always describes the same rows the table shows. The
    ``--model`` + ``--group-by plugin`` rejection in ``query_aggregate``
    surfaces here unchanged — the wrapper does no extra validation.
    """
    rows = query_aggregate(
        conn,
        since=since,
        until=until,
        project=project,
        model=model,
        group_by=group_by,
    )
    cache_read, cache_create, eff_pct, savings, excluded = _aggregate_cache_metrics(
        conn,
        since=since,
        until=until,
        project=project,
        model=model,
        pricing_data=pricing_data,
    )
    tier_breakdown = _aggregate_service_tier_breakdown(
        conn,
        since=since,
        until=until,
        project=project,
        model=model,
    )
    return AggregateReport(
        rows=rows,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_create,
        cache_eff_pct=eff_pct,
        cache_savings_usd=savings,
        cache_excluded_unknown_models=excluded,
        service_tier_breakdown=tier_breakdown,
    )


def render_aggregate(rows: list[AggregateRow], group_by: str) -> Table:
    t = Table(
        title=f"Aggregate (group-by: {group_by})",
        title_style="bold",
        box=box.HEAVY_HEAD,
        show_lines=True,
    )
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
    if rows:
        # ``sessions`` totals the per-group session_count column rather than
        # the count of distinct sessions across groups — a session can land
        # in multiple groups (e.g. ``--group-by model``) so the column is
        # already a per-row count, not a deduped global. Summing the column
        # is the consistent definition of "what's displayed".
        t.add_section()
        t.add_row(
            "Totals",
            format_cost(sum(r.total_cost_usd for r in rows)),
            f"{sum(r.session_count for r in rows):,}",
            f"{sum(r.input_tokens for r in rows):,}",
            f"{sum(r.output_tokens for r in rows):,}",
            f"{sum(r.cache_create for r in rows):,}",
            f"{sum(r.cache_read for r in rows):,}",
            style="bold",
        )
    return t


def _human_count(n: int) -> str:
    """Compact integer formatting for the cache footer line. Mirrors
    ``session.py`` so the two reports use the same K/M presentation."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def render_aggregate_footer(report: AggregateReport) -> RenderableType | None:
    """Cache + service-tier footer lines below the aggregate table.

    Returns ``None`` when neither line applies — no cache activity AND no
    non-standard service tier — so callers can skip the blank-line spacer
    entirely. Suppressing 0% efficiency / standard-only tier output keeps
    the report from showing misleading footers on uncached / vanilla
    sessions; same rule as ``session show``.
    """
    pieces: list[RenderableType] = []
    if report.cache_read_tokens or report.cache_creation_tokens:
        eff_str = f"{report.cache_eff_pct:.1f}%" if report.cache_eff_pct else "—"
        line = (
            f"Cache: {_human_count(report.cache_read_tokens)} read · "
            f"{_human_count(report.cache_creation_tokens)} created · "
            f"{eff_str} efficiency · saved ${report.cache_savings_usd:.2f}"
        )
        if report.cache_excluded_unknown_models:
            line += (
                f"  (excluded {report.cache_excluded_unknown_models} model(s) "
                "with no resolvable pricing)"
            )
        pieces.append(Text(line, style="dim"))
    non_standard = any(t not in ("standard", "unknown") for t in report.service_tier_breakdown)
    if non_standard:
        parts = [f"{t} {c:,} msgs" for t, c in sorted(report.service_tier_breakdown.items())]
        pieces.append(
            Text(
                f"Service tiers: {' · '.join(parts)}  (non-standard pricing not yet applied)",
                style="dim",
            )
        )
    if not pieces:
        return None
    return Group(*pieces)
