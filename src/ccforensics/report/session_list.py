from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from rich.table import Table
from rich.text import Text

from ._format import format_cost, format_duration

SortKey = Literal["cost", "started", "last-active", "turns"]

_SORT_COLUMN: dict[SortKey, str] = {
    "cost": "total_cost_usd",
    "started": "started_at",
    "last-active": "last_active_at",
    "turns": "turn_count",
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
    sort_key: SortKey = "last-active",
    reverse: bool = False,
    limit: int | None = None,
) -> list[SessionListRow]:
    where: list[str] = []
    params: list[object] = []
    if project:
        where.append("LOWER(IFNULL(project_path,'')) LIKE ?")
        params.append(f"%{project.lower()}%")
    if since:
        where.append("last_active_at >= ?")
        params.append(int(since.timestamp()))
    if until:
        where.append("last_active_at <= ?")
        params.append(int(until.timestamp()))
    if grep:
        where.append("LOWER(IFNULL(summary_text,'')) LIKE ?")
        params.append(f"%{grep.lower()}%")

    col = _SORT_COLUMN[sort_key]
    direction = "ASC" if reverse else "DESC"
    sql = (
        "SELECT session_id, project_path, project_display, started_at, "
        "last_active_at, duration_s, turn_count, total_cost_usd, "
        "summary_text, summary_source FROM session_summaries"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    # NULLs sort last regardless of direction — matches "unresolved cost
    # shouldn't outrank a known $5 session just because you flipped reverse".
    sql += f" ORDER BY {col} IS NULL, {col} {direction}, session_id"
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


def render_session_list(rows: list[SessionListRow], *, verbose: bool = False) -> Table:
    """Render session rows as a ``rich.Table``.

    Summary column uses ``overflow='fold'`` so long summaries wrap rather
    than truncate. The UUID column shows the shortest unique prefix per id.
    """
    table = Table(show_lines=False, expand=True, pad_edge=False)
    short_ids = shorten_session_ids([r.session_id for r in rows])
    table.add_column("UUID", no_wrap=True)
    table.add_column("Project", no_wrap=True, max_width=30)
    table.add_column("Started", no_wrap=True)
    table.add_column("Dur", no_wrap=True, justify="right")
    table.add_column("Turns", no_wrap=True, justify="right")
    table.add_column("Cost", no_wrap=True, justify="right")
    if verbose:
        table.add_column("Src", no_wrap=True)
    table.add_column("Summary", overflow="fold")

    for r in rows:
        started = datetime.fromtimestamp(r.started_at, tz=UTC).strftime("%Y-%m-%d %H:%M")
        cells: list[str | Text] = [
            short_ids[r.session_id],
            (r.project_display or "")[:30],
            started,
            format_duration(r.duration_s),
            str(r.turn_count),
            Text(format_cost(r.total_cost_usd)),
        ]
        if verbose:
            cells.append(_SOURCE_BADGE.get(r.summary_source or "none", "[-]"))
        cells.append(Text(r.summary_text or "<no summary available>"))
        table.add_row(*cells)
    return table
