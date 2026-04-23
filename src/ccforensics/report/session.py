"""Per-session deep report (spec §5.3).

Sections rendered:
1. Header — project, started/last-active, duration, turns, models, total cost.
2. Cost-by-bucket — ``session_rollups`` rows rendered as a table.
3. Cost-by-plugin — ``subagent:<type>`` rolled up via registry mapping,
   plus ``user-level``, ``builtin``, ``main``, ``auto-compact``,
   ``unattributed``.
4. Unattributed detail — when ``include_unattributed``, list the
   subagent files whose cost landed in the unattributed bucket.
5. Parse notes — schema versions seen + parse-warning counts.

Skill ledger (spec §5.3 section 4) is M8.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..registry import (
    classify_agent_source,
    load_plugin_names,
    load_user_level_agent_names,
)
from ._format import format_cost, format_duration


@dataclass
class SessionHeader:
    session_id: str
    project_path: str | None
    started_at: int
    last_active_at: int
    duration_s: int
    turn_count: int
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
class SessionReport:
    header: SessionHeader
    buckets: list[BucketRow] = field(default_factory=list)
    plugins: list[PluginRow] = field(default_factory=list)
    unattributed_items: list[UnattributedItem] = field(default_factory=list)
    parse_notes: ParseNotes | None = None


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
    models = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT model FROM messages WHERE session_id=? AND model IS NOT NULL "
            "ORDER BY model",
            (session_id,),
        ).fetchall()
    ]
    return SessionHeader(
        session_id=session_id,
        project_path=row[0],
        started_at=int(row[1]),
        last_active_at=int(row[2]),
        duration_s=int(row[3]),
        turn_count=int(row[4]),
        total_cost_usd=row[5],
        models_seen=models,
        summary_text=row[6],
        summary_source=row[7],
    )


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


def _rollup_plugins(
    conn: sqlite3.Connection, buckets: list[BucketRow]
) -> list[PluginRow]:
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
        PluginRow(source=s, cost_usd=c, session_fragment_count=n)
        for s, (c, n) in totals.items()
    ]
    out.sort(key=lambda r: (-r.cost_usd, r.source))
    return out


def _load_unattributed_items(
    conn: sqlite3.Connection, session_id: str
) -> list[UnattributedItem]:
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


def build_session_report(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    include_unattributed: bool = False,
) -> SessionReport:
    """Load everything needed for the report from the index in one pass.

    ``include_unattributed`` controls whether the detailed list of
    unresolvable subagent files is populated; the summary always reports
    the unattributed cost via the bucket table.
    """
    header = _load_header(conn, session_id)
    buckets = _load_buckets(conn, session_id)
    plugins = _rollup_plugins(conn, buckets)
    items = _load_unattributed_items(conn, session_id) if include_unattributed else []
    parse_notes = _load_parse_notes(conn, session_id)
    return SessionReport(
        header=header,
        buckets=buckets,
        plugins=plugins,
        unattributed_items=items,
        parse_notes=parse_notes,
    )


# ---------- rendering ----------


def _format_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _render_header(h: SessionHeader) -> RenderableType:
    lines = [
        Text.assemble(("session: ", "bold"), h.session_id),
        Text.assemble(("project: ", "bold"), h.project_path or "<unknown>"),
        Text.assemble(
            ("started: ", "bold"),
            _format_ts(h.started_at),
            ("   last-active: ", "bold"),
            _format_ts(h.last_active_at),
        ),
        Text.assemble(
            ("duration: ", "bold"),
            format_duration(h.duration_s),
            ("   turns: ", "bold"),
            str(h.turn_count),
        ),
        Text.assemble(
            ("models: ", "bold"),
            ", ".join(h.models_seen) or "<none>",
        ),
        Text.assemble(("total cost: ", "bold"), format_cost(h.total_cost_usd)),
    ]
    if h.summary_text:
        lines.append(
            Text.assemble(("summary: ", "bold"), h.summary_text[:120])
        )
    return Group(*lines)


def _render_buckets(buckets: list[BucketRow]) -> Table:
    t = Table(title="Cost by bucket", show_edge=False)
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
    return t


def _render_plugins(plugins: list[PluginRow]) -> Table:
    t = Table(title="Cost by plugin", show_edge=False)
    t.add_column("source", style="magenta")
    t.add_column("cost", justify="right")
    t.add_column("buckets", justify="right")
    for p in plugins:
        t.add_row(p.source, format_cost(p.cost_usd), str(p.session_fragment_count))
    return t


def _render_unattributed(items: list[UnattributedItem]) -> Table:
    t = Table(title="Unattributed subagent files", show_edge=False)
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


def _render_parse_notes(notes: ParseNotes) -> RenderableType:
    return Text.assemble(
        ("files: ", "bold"),
        str(notes.files_count),
        ("   schema_versions: ", "bold"),
        ", ".join(notes.schema_versions) or "<none>",
        ("   parse_warnings: ", "bold"),
        str(notes.parse_warnings_total),
    )


def render_session_report(report: SessionReport) -> RenderableType:
    sections: list[RenderableType] = [
        _render_header(report.header),
        _render_buckets(report.buckets),
        _render_plugins(report.plugins),
    ]
    if report.unattributed_items:
        sections.append(_render_unattributed(report.unattributed_items))
    if report.parse_notes is not None:
        sections.append(_render_parse_notes(report.parse_notes))
    return Group(*sections)
