from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from rich import box
from rich.table import Table
from rich.text import Text

from ._format import format_cost, format_duration

SortKey = Literal["cost", "started", "last-active", "turns", "compact"]

# Each entry is the fully qualified ORDER-BY expression. Most live on
# ``session_summaries`` (``ss.*``); ``compact`` references the SELECT alias
# of the joined compaction-count subquery rather than a column on ``ss``.
_SORT_COLUMN: dict[SortKey, str] = {
    "cost": "ss.total_cost_usd",
    "started": "ss.started_at",
    "last-active": "ss.last_active_at",
    "turns": "ss.turn_count",
    "compact": "compaction_count",
}

_SOURCE_BADGE: dict[str, str] = {
    "claude-summary": "[C]",
    "first-prompt": "[F]",
    "none": "[-]",
}


@dataclass
class SessionListRow:
    session_id: str
    project_path: str | None
    project_display: str | None
    started_at: int
    last_active_at: int
    duration_s: int
    turn_count: int
    compaction_count: int
    total_cost_usd: float | None
    summary_text: str | None
    summary_source: str | None


def query_session_list(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    grep: str | None = None,
    model: str | None = None,
    sort_key: SortKey = "last-active",
    reverse: bool = False,
    limit: int | None = None,
) -> list[SessionListRow]:
    where: list[str] = []
    params: list[object] = []
    if project:
        where.append("LOWER(IFNULL(ss.project_path,'')) LIKE ?")
        params.append(f"%{project.lower()}%")
    if since:
        where.append("ss.last_active_at >= ?")
        params.append(int(since.timestamp()))
    if until:
        where.append("ss.last_active_at <= ?")
        params.append(int(until.timestamp()))
    if grep:
        where.append("LOWER(IFNULL(ss.summary_text,'')) LIKE ?")
        params.append(f"%{grep.lower()}%")
    if model:
        # Session-level membership filter: the returned cost column is still
        # the full session cost, not the per-model slice. Use
        # ``aggregate --model`` for per-model dollars. ``NOT LIKE '<%>'``
        # excludes Claude Code's angle-bracket placeholders (e.g.
        # ``<synthetic>``) so ``--model synth`` doesn't wrongly match them.
        # Normalize ``.`` → ``-`` so user-friendly forms like ``opus-4.7``
        # match the on-disk ``claude-opus-4-7`` model string.
        where.append(
            "ss.session_id IN ("
            "SELECT DISTINCT session_id FROM messages "
            "WHERE model IS NOT NULL AND model NOT LIKE '<%>' "
            "AND LOWER(model) LIKE ?"
            ")"
        )
        params.append(f"%{model.lower().replace('.', '-')}%")

    col = _SORT_COLUMN[sort_key]
    direction = "ASC" if reverse else "DESC"
    # Compaction count joins via a grouped subquery on ``files.kind='auto-compact'``.
    # Each ``agent-acompact-*.jsonl`` file is one compaction event; its
    # ``files.session_id`` is the parent session UUID (``_classify_file``
    # sets it from the path).
    sql = (
        "SELECT ss.session_id, ss.project_path, ss.project_display, "
        "ss.started_at, ss.last_active_at, ss.duration_s, ss.turn_count, "
        "COALESCE(c.compactions, 0) AS compaction_count, "
        "ss.total_cost_usd, ss.summary_text, ss.summary_source "
        "FROM session_summaries ss "
        "LEFT JOIN ("
        "SELECT session_id, COUNT(*) AS compactions "
        "FROM files WHERE kind='auto-compact' GROUP BY session_id"
        ") c ON c.session_id = ss.session_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    # NULLs sort last regardless of direction — matches "unresolved cost
    # shouldn't outrank a known $5 session just because you flipped reverse".
    # Within a primary-key tie, order by ``last_active_at DESC`` before
    # ``session_id``: on a real corpus, sort columns like ``cost`` and
    # ``turns`` routinely have huge tie-pools (thousands of $0 ingestion
    # sessions, many 1-turn sessions) and alphabetical-by-id makes ``--limit``
    # return arbitrary rows. Most-recent-first is the informative tiebreak.
    sql += f" ORDER BY {col} IS NULL, {col} {direction}, ss.last_active_at DESC, ss.session_id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql, params)
    return [SessionListRow(*row) for row in cur.fetchall()]


def shorten_session_ids(session_ids: list[str], min_chars: int = 6) -> dict[str, str]:
    """Return a {full_id: short_id} map, extending each prefix until unique.

    De-duplicates input while preserving order. Starts at ``min_chars`` and
    extends any colliding group by one char per round. A prefix is capped at
    the id's own length — if two ids are identical after dedup they can't
    collide, and if one id is a strict prefix of another, the shorter one
    simply returns its full length.
    """
    ids = list(dict.fromkeys(session_ids))
    if not ids:
        return {}
    lengths: dict[str, int] = {sid: min(len(sid), min_chars) for sid in ids}
    while True:
        seen: dict[str, list[str]] = {}
        for sid in ids:
            key = sid[: lengths[sid]]
            seen.setdefault(key, []).append(sid)
        collisions = [group for group in seen.values() if len(group) > 1]
        if not collisions:
            return {sid: sid[: lengths[sid]] for sid in ids}
        progressed = False
        for group in collisions:
            for sid in group:
                if lengths[sid] < len(sid):
                    lengths[sid] += 1
                    progressed = True
        if not progressed:
            # All colliding ids are already at full length — can't distinguish
            # further (would require identical ids, which dict.fromkeys ruled
            # out; or one id is a strict prefix of another at full length,
            # which is impossible for distinct ids). Return what we have.
            return {sid: sid[: lengths[sid]] for sid in ids}


def render_session_list(
    rows: list[SessionListRow],
    *,
    verbose: bool = False,
    console_width: int | None = None,
) -> Table:
    """Render session rows as a ``rich.Table``.

    Summary column uses ``overflow='fold'`` so long summaries wrap rather
    than truncate. The UUID column shows the shortest unique prefix per id.

    Narrow-terminal mode: when ``console_width`` is below 120 columns,
    the Project column is dropped so the Summary still has usable width.
    When ``console_width`` is ``None``, the terminal width is detected
    automatically.
    """
    width = console_width if console_width is not None else _detect_console_width()
    narrow = width < 120

    table = Table(
        title="Sessions",
        title_style="bold",
        box=box.HEAVY_HEAD,
        # ``show_lines=True`` draws a horizontal divider between every row.
        # Without it, multi-line cells (long Summary text wrapped via
        # ``overflow='fold'``) blend into the next row visually because
        # there's no rule separating them.
        show_lines=True,
        expand=True,
    )
    short_ids = shorten_session_ids([r.session_id for r in rows])
    table.add_column("UUID", no_wrap=True)
    if not narrow:
        table.add_column("Project", no_wrap=True, max_width=30)
    table.add_column("Started", no_wrap=True)
    table.add_column("Dur", no_wrap=True, justify="right")
    table.add_column("Turns", no_wrap=True, justify="right")
    table.add_column("Compact", no_wrap=True, justify="right")
    table.add_column("Cost", no_wrap=True, justify="right")
    if verbose:
        table.add_column("Src", no_wrap=True)
    table.add_column("Summary", overflow="fold")

    for r in rows:
        started = datetime.fromtimestamp(r.started_at, tz=UTC).strftime("%Y-%m-%d %H:%M")
        cells: list[str | Text] = [short_ids[r.session_id]]
        if not narrow:
            cells.append((r.project_display or "")[:30])
        cells.extend(
            [
                started,
                format_duration(r.duration_s),
                str(r.turn_count),
                str(r.compaction_count),
                Text(format_cost(r.total_cost_usd)),
            ]
        )
        if verbose:
            cells.append(_SOURCE_BADGE.get(r.summary_source or "none", "[-]"))
        cells.append(Text(r.summary_text or "<no summary available>"))
        table.add_row(*cells)

    if rows:
        # Totals: sum every numeric column displayed. Cost is None for sessions
        # with unresolved pricing — exclude those from the sum so the totals
        # cell still reflects the pricing-resolved subtotal rather than
        # silently coercing NULL to 0.
        total_dur = sum(r.duration_s for r in rows)
        total_turns = sum(r.turn_count for r in rows)
        total_compact = sum(r.compaction_count for r in rows)
        priced = [r.total_cost_usd for r in rows if r.total_cost_usd is not None]
        total_cost: float | None = sum(priced) if priced else None

        table.add_section()
        totals_cells: list[str | Text] = [Text("Totals", style="bold")]
        if not narrow:
            totals_cells.append("")
        totals_cells.extend(
            [
                "",
                format_duration(total_dur),
                f"{total_turns:,}",
                f"{total_compact:,}",
                Text(format_cost(total_cost), style="bold"),
            ]
        )
        if verbose:
            totals_cells.append("")
        totals_cells.append("")
        table.add_row(*totals_cells, style="bold")
    return table


def _detect_console_width() -> int:
    """Best-effort console width detection; defaults to 120 when unknown
    (so the default rendering doesn't trigger narrow-mode by accident)."""
    import os
    import shutil

    w = shutil.get_terminal_size(fallback=(120, 24)).columns
    # Honor explicit override for tests / non-TTY callers.
    override = os.environ.get("COLUMNS")
    if override and override.isdigit():
        return int(override)
    return w
