"""Per-session deep report: header, buckets, plugin rollup, unattributed detail, notes."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..pricing import resolve_pricing
from ..registry import (
    classify_agent_source,
    load_plugin_names,
    load_user_level_agent_names,
)
from ._cache import CacheRow, cache_metrics
from ._format import (
    format_cost,
    format_duration,
    render_cache_line,
    render_service_tier_line,
)


@dataclass
class SessionHeader:
    session_id: str
    project_path: str | None
    started_at: int
    last_active_at: int
    duration_s: int
    turn_count: int
    compaction_count: int
    total_cost_usd: float | None
    models_seen: list[str]
    summary_text: str | None
    summary_source: str | None


@dataclass
class BucketRow:
    bucket_kind: str
    bucket_name: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_create: int
    cache_read: int


@dataclass
class ModelRow:
    """Per-model rollup for a single session. Rows with ``model IS NULL`` in
    ``messages`` are infrastructure (queue-operation, progress, system) and
    carry $0 — they're excluded at query time."""

    model: str
    cost_usd: float
    message_count: int
    input_tokens: int
    output_tokens: int
    cache_create: int
    cache_read: int


@dataclass
class PluginRow:
    """Plugin-level rollup.

    ``source`` is ``plugin-name | 'user-level' | 'builtin' | 'main' |
    'auto-compact' | 'unattributed' | 'unknown'``. One row per source.
    """

    source: str
    cost_usd: float
    session_fragment_count: int  # number of (bucket_kind, bucket_name) rows that mapped here


@dataclass
class UnattributedItem:
    child_file_path: str
    subagent_type: str | None
    cost_usd: float | None


@dataclass
class ParseNotes:
    schema_versions: list[str]
    parse_warnings_total: int
    files_count: int


@dataclass
class SkillLedgerEntry:
    activated_at: int
    skill_name: str
    plugin_name: str | None
    source: str
    content_size: int | None
    skill_path: str
    estimated_cost_usd: float | None
    estimated_cost_band_usd: float | None


@dataclass
class SessionReport:
    header: SessionHeader
    buckets: list[BucketRow] = field(default_factory=list)
    plugins: list[PluginRow] = field(default_factory=list)
    models: list[ModelRow] = field(default_factory=list)
    unattributed_items: list[UnattributedItem] = field(default_factory=list)
    parse_notes: ParseNotes | None = None
    skill_ledger: list[SkillLedgerEntry] = field(default_factory=list)
    # Cache fields are top-level per spec (``docs/specs/design.md``) — JSON
    # output emits them alongside ``service_tier_breakdown`` rather than
    # nested under a ``cache`` object. ``cache_eff_pct`` /
    # ``cache_savings_usd`` are cost-weighted; token-ratio overstates dollar
    # impact because cache_read is ~10x cheaper than input.
    # ``cache_excluded_unknown_models`` counts distinct models with no
    # resolvable pricing so the user can gauge completeness of the figures.
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_eff_pct: float = 0.0
    cache_savings_usd: float = 0.0
    cache_excluded_unknown_models: int = 0
    service_tier_breakdown: dict[str, int] = field(default_factory=dict)


class SessionReportNotFound(Exception):  # noqa: N818 — public name matches resolver style
    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id!r} has no summary row")
        self.session_id = session_id


def _load_header(conn: sqlite3.Connection, session_id: str) -> SessionHeader:
    row = conn.execute(
        """SELECT project_path, started_at, last_active_at, duration_s,
                  turn_count, total_cost_usd, summary_text, summary_source
             FROM session_summaries WHERE session_id=?""",
        (session_id,),
    ).fetchone()
    if row is None:
        raise SessionReportNotFound(session_id)
    # ``NOT LIKE '<%>'`` excludes Claude Code's angle-bracket placeholders
    # (e.g. ``<synthetic>`` on non-LLM-call assistant stubs).
    models = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT model FROM messages "
            "WHERE session_id=? AND model IS NOT NULL AND model NOT LIKE '<%>' "
            "ORDER BY model",
            (session_id,),
        ).fetchall()
    ]
    compaction_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM files WHERE session_id=? AND kind='auto-compact'",
            (session_id,),
        ).fetchone()[0]
        or 0
    )
    return SessionHeader(
        session_id=session_id,
        project_path=row[0],
        started_at=int(row[1]),
        last_active_at=int(row[2]),
        duration_s=int(row[3]),
        turn_count=int(row[4]),
        compaction_count=compaction_count,
        total_cost_usd=row[5],
        models_seen=models,
        summary_text=row[6],
        summary_source=row[7],
    )


def _load_models(conn: sqlite3.Connection, session_id: str) -> list[ModelRow]:
    # ``NOT LIKE '<%>'`` excludes Claude Code's angle-bracket placeholders
    # (e.g. ``<synthetic>`` on non-LLM-call assistant stubs) so they don't
    # appear as their own row in the per-model cost table.
    rows = conn.execute(
        """SELECT model,
                  COALESCE(SUM(cost_usd), 0),
                  COUNT(*),
                  COALESCE(SUM(input_tokens), 0),
                  COALESCE(SUM(output_tokens), 0),
                  COALESCE(SUM(cache_creation), 0),
                  COALESCE(SUM(cache_read), 0)
             FROM messages
            WHERE session_id=? AND model IS NOT NULL AND model NOT LIKE '<%>'
            GROUP BY model
            ORDER BY SUM(cost_usd) DESC, model""",
        (session_id,),
    ).fetchall()
    return [
        ModelRow(
            model=r[0],
            cost_usd=float(r[1] or 0.0),
            message_count=int(r[2] or 0),
            input_tokens=int(r[3] or 0),
            output_tokens=int(r[4] or 0),
            cache_create=int(r[5] or 0),
            cache_read=int(r[6] or 0),
        )
        for r in rows
    ]


def _load_buckets(conn: sqlite3.Connection, session_id: str) -> list[BucketRow]:
    rows = conn.execute(
        """SELECT bucket_kind, bucket_name, cost_usd,
                  input_tokens, output_tokens, cache_create, cache_read
             FROM session_rollups WHERE session_id=?
             ORDER BY cost_usd DESC, bucket_kind, bucket_name""",
        (session_id,),
    ).fetchall()
    return [
        BucketRow(
            bucket_kind=r[0],
            bucket_name=r[1],
            cost_usd=float(r[2] or 0.0),
            input_tokens=int(r[3] or 0),
            output_tokens=int(r[4] or 0),
            cache_create=int(r[5] or 0),
            cache_read=int(r[6] or 0),
        )
        for r in rows
    ]


def _rollup_plugins(conn: sqlite3.Connection, buckets: list[BucketRow]) -> list[PluginRow]:
    """Map each bucket to a plugin-level ``source`` and sum.

    Buckets other than ``subagent:*`` map to themselves (``main``,
    ``auto-compact``, ``unattributed``). ``subagent:<type>`` routes
    through ``classify_agent_source``.
    """
    plugins = load_plugin_names(conn)
    user_agents = load_user_level_agent_names(conn)

    totals: dict[str, tuple[float, int]] = {}
    for b in buckets:
        if b.bucket_kind == "subagent":
            source = classify_agent_source(b.bucket_name, plugins, user_agents)
        else:
            source = b.bucket_kind
        cost, count = totals.get(source, (0.0, 0))
        totals[source] = (cost + b.cost_usd, count + 1)
    out = [
        PluginRow(source=s, cost_usd=c, session_fragment_count=n) for s, (c, n) in totals.items()
    ]
    out.sort(key=lambda r: (-r.cost_usd, r.source))
    return out


def _load_unattributed_items(conn: sqlite3.Connection, session_id: str) -> list[UnattributedItem]:
    """Subagent files whose spawn row has null ``parent_message_dedup_key``
    OR whose ``subagent_type`` is null — both route to unattributed per
    the SQL bucket-CASE in ``attribution.py``."""
    rows = conn.execute(
        """SELECT s.child_file_path, s.subagent_type, s.total_cost_usd
             FROM subagent_spawns s
            WHERE s.parent_session_id=?
              AND (s.parent_message_dedup_key IS NULL OR s.subagent_type IS NULL)
            ORDER BY s.ts_spawned""",
        (session_id,),
    ).fetchall()
    return [
        UnattributedItem(
            child_file_path=r[0],
            subagent_type=r[1],
            cost_usd=r[2],
        )
        for r in rows
    ]


def _load_skill_ledger(conn: sqlite3.Connection, session_id: str) -> list[SkillLedgerEntry]:
    rows = conn.execute(
        """SELECT activated_at, skill_name, plugin_name, source, content_size,
                  skill_path, estimated_cost_usd, estimated_cost_band_usd
             FROM skill_activations WHERE session_id=?
             ORDER BY activated_at, id""",
        (session_id,),
    ).fetchall()
    return [
        SkillLedgerEntry(
            activated_at=int(r[0]),
            skill_name=r[1],
            plugin_name=r[2],
            source=r[3],
            content_size=r[4],
            skill_path=r[5],
            estimated_cost_usd=r[6],
            estimated_cost_band_usd=r[7],
        )
        for r in rows
    ]


def _load_parse_notes(conn: sqlite3.Connection, session_id: str) -> ParseNotes:
    rows = conn.execute(
        """SELECT schema_version, parse_warnings FROM files WHERE session_id=?""",
        (session_id,),
    ).fetchall()
    schema_versions = sorted({r[0] for r in rows if r[0] is not None})
    warnings_total = sum(int(r[1] or 0) for r in rows)
    return ParseNotes(
        schema_versions=schema_versions,
        parse_warnings_total=warnings_total,
        files_count=len(rows),
    )


def _load_cache_metrics(
    conn: sqlite3.Connection,
    session_id: str,
    pricing_data: dict[str, Any] | None,
) -> tuple[int, int, float, float, int]:
    """Per-model cache token sums + cost-weighted efficiency / savings.

    Returns ``(cache_read_tokens, cache_creation_tokens, eff_pct,
    savings_usd, excluded_count)``. Excludes ``model IS NULL``
    (infrastructure rows) and Claude Code's angle-bracket placeholders
    like ``<synthetic>`` for the same reasons as ``_load_models``.
    Without ``pricing_data`` we still report token totals but
    cost-derived metrics are zero — the only caller that skips pricing
    is a unit test stub.
    """
    rows_data = conn.execute(
        """SELECT model,
                  COALESCE(SUM(input_tokens), 0),
                  COALESCE(SUM(cache_creation), 0),
                  COALESCE(SUM(cache_read), 0)
             FROM messages
            WHERE session_id=?
              AND model IS NOT NULL
              AND model NOT LIKE '<%>'
            GROUP BY model""",
        (session_id,),
    ).fetchall()
    rows = [
        CacheRow(
            model=str(m),
            input_tokens=int(i or 0),
            cache_creation=int(cc or 0),
            cache_read=int(cr or 0),
        )
        for (m, i, cc, cr) in rows_data
    ]
    read_total = sum(r.cache_read for r in rows)
    create_total = sum(r.cache_creation for r in rows)
    if pricing_data is None:
        return (read_total, create_total, 0.0, 0.0, 0)
    metrics = cache_metrics(rows, lambda m: resolve_pricing(m, pricing_data))
    return (
        read_total,
        create_total,
        metrics.eff_pct,
        metrics.savings_usd,
        metrics.rows_excluded_for_unknown_model,
    )


def _load_service_tier_breakdown(conn: sqlite3.Connection, session_id: str) -> dict[str, int]:
    """Count of assistant messages per ``service_tier`` (NULL → 'unknown').

    Assistant role only — user/tool_result messages don't carry a tier and
    inflating the count with them would mislead. Excludes ``model IS NULL``
    (infrastructure rows) and Claude Code's ``<...>`` placeholders for
    symmetry with ``_load_cache_metrics``; those rows carry no real tier
    and would inflate the ``unknown`` count. Returns insertion-order dict
    keyed by tier.
    """
    rows = conn.execute(
        """SELECT COALESCE(service_tier, 'unknown') AS tier, COUNT(*)
             FROM messages
            WHERE session_id=? AND role='assistant'
              AND model IS NOT NULL
              AND model NOT LIKE '<%>'
            GROUP BY tier
            ORDER BY tier""",
        (session_id,),
    ).fetchall()
    return {str(tier): int(count) for tier, count in rows}


def build_session_report(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    include_unattributed: bool = False,
    pricing_data: dict[str, Any] | None = None,
) -> SessionReport:
    """Load everything needed for the report from the index in one pass.

    ``include_unattributed`` controls whether the detailed list of
    unresolvable subagent files is populated; the summary always reports
    the unattributed cost via the bucket table.

    ``pricing_data`` (LiteLLM-shaped dict) is required for the cache
    cost-savings + efficiency figures. When ``None`` the cache summary
    still reports token totals but cost-derived metrics are zero.
    """
    header = _load_header(conn, session_id)
    buckets = _load_buckets(conn, session_id)
    plugins = _rollup_plugins(conn, buckets)
    models = _load_models(conn, session_id)
    items = _load_unattributed_items(conn, session_id) if include_unattributed else []
    parse_notes = _load_parse_notes(conn, session_id)
    skill_ledger = _load_skill_ledger(conn, session_id)
    cache_read, cache_create, eff_pct, savings, excluded = _load_cache_metrics(
        conn, session_id, pricing_data
    )
    tier_breakdown = _load_service_tier_breakdown(conn, session_id)
    return SessionReport(
        header=header,
        buckets=buckets,
        plugins=plugins,
        models=models,
        unattributed_items=items,
        parse_notes=parse_notes,
        skill_ledger=skill_ledger,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_create,
        cache_eff_pct=eff_pct,
        cache_savings_usd=savings,
        cache_excluded_unknown_models=excluded,
        service_tier_breakdown=tier_breakdown,
    )


# ---------- rendering ----------


def _format_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _render_header(h: SessionHeader) -> RenderableType:
    """Two-column key/value grid wrapped in a titled panel.

    Each field is on its own line with right-aligned labels so values
    line up cleanly. Summary moved out to ``_render_summary`` so the
    header stays scannable at a glance and the summary can show in full.
    """
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan", justify="right", no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row("session:", h.session_id)
    grid.add_row("project:", h.project_path or "<unknown>")
    grid.add_row("started:", _format_ts(h.started_at))
    grid.add_row("last-active:", _format_ts(h.last_active_at))
    grid.add_row("duration:", format_duration(h.duration_s))
    grid.add_row("turns:", f"{h.turn_count:,}")
    grid.add_row("compactions:", f"{h.compaction_count:,}")
    grid.add_row("models:", ", ".join(h.models_seen) or "<none>")
    grid.add_row("total cost:", format_cost(h.total_cost_usd))
    # ``expand=False`` shrinks the panel to its content width — on a wide
    # terminal a full-width panel leaves a chasm of whitespace between the
    # right-aligned label column and the value column, which reads worse
    # than a snug panel.
    return Panel(grid, title="Session", border_style="cyan", padding=(0, 1), expand=False)


def _render_summary(h: SessionHeader) -> RenderableType | None:
    """Full summary text in its own panel; suppressed when source='none'.

    Earlier the header truncated the summary at 120 chars and inlined it,
    which routinely cut off mid-sentence. Showing the full text in a
    dedicated panel with ``overflow='fold'`` is the readable fix —
    ``session show`` is the deep report, not the at-a-glance list.
    """
    if not h.summary_text or h.summary_source == "none":
        return None
    body = Text(h.summary_text, overflow="fold")
    return Panel(body, title="Summary", border_style="dim", padding=(0, 1))


def _render_buckets(buckets: list[BucketRow]) -> Table:
    t = Table(title="Cost by bucket", box=box.HEAVY_HEAD, title_style="bold", show_lines=True)
    t.add_column("bucket", style="cyan")
    t.add_column("cost", justify="right")
    t.add_column("in", justify="right")
    t.add_column("out", justify="right")
    t.add_column("cache_create", justify="right")
    t.add_column("cache_read", justify="right")
    for b in buckets:
        name = b.bucket_name if b.bucket_kind == "subagent" else b.bucket_kind
        label = f"subagent:{name}" if b.bucket_kind == "subagent" else name
        t.add_row(
            label,
            format_cost(b.cost_usd),
            f"{b.input_tokens:,}",
            f"{b.output_tokens:,}",
            f"{b.cache_create:,}",
            f"{b.cache_read:,}",
        )
    if buckets:
        t.add_section()
        t.add_row(
            "Totals",
            format_cost(sum(b.cost_usd for b in buckets)),
            f"{sum(b.input_tokens for b in buckets):,}",
            f"{sum(b.output_tokens for b in buckets):,}",
            f"{sum(b.cache_create for b in buckets):,}",
            f"{sum(b.cache_read for b in buckets):,}",
            style="bold",
        )
    return t


def _render_models(models: list[ModelRow]) -> Table:
    t = Table(title="Cost by model", box=box.HEAVY_HEAD, title_style="bold", show_lines=True)
    t.add_column("model", style="yellow")
    t.add_column("cost", justify="right")
    t.add_column("msgs", justify="right")
    t.add_column("in", justify="right")
    t.add_column("out", justify="right")
    t.add_column("cache_create", justify="right")
    t.add_column("cache_read", justify="right")
    for m in models:
        t.add_row(
            m.model,
            format_cost(m.cost_usd),
            f"{m.message_count:,}",
            f"{m.input_tokens:,}",
            f"{m.output_tokens:,}",
            f"{m.cache_create:,}",
            f"{m.cache_read:,}",
        )
    if models:
        t.add_section()
        t.add_row(
            "Totals",
            format_cost(sum(m.cost_usd for m in models)),
            f"{sum(m.message_count for m in models):,}",
            f"{sum(m.input_tokens for m in models):,}",
            f"{sum(m.output_tokens for m in models):,}",
            f"{sum(m.cache_create for m in models):,}",
            f"{sum(m.cache_read for m in models):,}",
            style="bold",
        )
    return t


def _render_plugins(plugins: list[PluginRow]) -> Table:
    t = Table(title="Cost by plugin", box=box.HEAVY_HEAD, title_style="bold", show_lines=True)
    t.add_column("source", style="magenta")
    t.add_column("cost", justify="right")
    t.add_column("buckets", justify="right")
    for p in plugins:
        t.add_row(p.source, format_cost(p.cost_usd), str(p.session_fragment_count))
    if plugins:
        t.add_section()
        t.add_row(
            "Totals",
            format_cost(sum(p.cost_usd for p in plugins)),
            f"{sum(p.session_fragment_count for p in plugins):,}",
            style="bold",
        )
    return t


def _render_unattributed(items: list[UnattributedItem]) -> Table:
    t = Table(
        title="Unattributed subagent files", box=box.HEAVY_HEAD, title_style="bold", show_lines=True
    )
    t.add_column("subagent_type")
    t.add_column("cost", justify="right")
    t.add_column("path", overflow="fold")
    for item in items:
        t.add_row(
            item.subagent_type or "<none>",
            format_cost(item.cost_usd),
            item.child_file_path,
        )
    return t


def _render_skill_ledger(ledger: list[SkillLedgerEntry]) -> Table:
    t = Table(title="Skill activations", box=box.HEAVY_HEAD, title_style="bold", show_lines=True)
    t.add_column("when", style="dim")
    t.add_column("skill", style="green")
    t.add_column("plugin")
    t.add_column("source")
    t.add_column("size", justify="right")
    t.add_column("est.cost", justify="right")
    for entry in ledger:
        ts = datetime.fromtimestamp(entry.activated_at, tz=UTC).strftime("%m-%d %H:%M")
        size = f"{entry.content_size:,}" if entry.content_size is not None else "-"
        cost = (
            format_cost(entry.estimated_cost_usd) if entry.estimated_cost_usd is not None else "-"
        )
        t.add_row(
            ts,
            entry.skill_name,
            entry.plugin_name or "<user-level>",
            entry.source,
            size,
            cost,
        )
    return t


def _render_parse_notes(notes: ParseNotes) -> RenderableType:
    """Footer line, dim-styled so it sits below the data without competing
    with it visually."""
    return Text.assemble(
        ("files: ", "bold"),
        str(notes.files_count),
        ("   schema_versions: ", "bold"),
        ", ".join(notes.schema_versions) or "<none>",
        ("   parse_warnings: ", "bold"),
        str(notes.parse_warnings_total),
        style="dim",
    )


def render_session_report(report: SessionReport) -> RenderableType:
    """Stitch sections with a blank line between each so the rendered tables
    don't bleed into each other. Order: Session header → Summary → Cost by
    bucket → Cost by plugin → Cost by model → Unattributed → Skills → notes."""
    sections: list[RenderableType] = [_render_header(report.header)]
    summary = _render_summary(report.header)
    if summary is not None:
        sections.append(Text(""))
        sections.append(summary)
    # Blank ``Text("")`` between each table so consecutive tables don't
    # blend into one tall block when rendered with ``box.SIMPLE_HEAVY``.
    sections.append(Text(""))
    sections.append(_render_buckets(report.buckets))
    cache_text = render_cache_line(
        report.cache_read_tokens,
        report.cache_creation_tokens,
        report.cache_eff_pct,
        report.cache_savings_usd,
        report.cache_excluded_unknown_models,
    )
    if cache_text is not None:
        sections.append(Text(""))
        sections.append(cache_text)
    tier_text = render_service_tier_line(report.service_tier_breakdown)
    if tier_text is not None:
        sections.append(Text(""))
        sections.append(tier_text)
    sections.append(Text(""))
    sections.append(_render_plugins(report.plugins))
    if report.models:
        sections.append(Text(""))
        sections.append(_render_models(report.models))
    if report.unattributed_items:
        sections.append(Text(""))
        sections.append(_render_unattributed(report.unattributed_items))
    if report.skill_ledger:
        sections.append(Text(""))
        sections.append(_render_skill_ledger(report.skill_ledger))
    if report.parse_notes is not None:
        sections.append(Text(""))
        sections.append(_render_parse_notes(report.parse_notes))
    return Group(*sections)
