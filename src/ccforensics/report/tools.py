"""Per-tool / per-MCP cost report.

Slicing semantics:
- group_key = mcp_server when mcp_server IS NOT NULL else tool_name (default
  view: server-rolled). With detail=True, group_key = tool_name always.
- isolated_turns = distinct messages where this tool/server is the ONLY tool
  emitted in the assistant turn. isolated_cost_usd = SUM(messages.cost_usd)
  over those turns. EXACT.
- shared_turns / shared_exposure_usd: turns emitting this tool alongside
  siblings. shared_exposure_usd is an UPPER BOUND — same turn cost will
  appear under each sibling's row. Documented in footer; never sum across
  rows.

This report does NOT participate in the bucket-attribution invariant.
It's an orthogonal slicing of the same cost data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

SortKey = Literal["isolated_cost", "invocations", "shared_exposure"]

_SORT_TO_SQL = {
    "isolated_cost": "isolated_cost_usd DESC",
    "invocations": "invocations DESC",
    "shared_exposure": "shared_exposure_usd DESC",
}


@dataclass(frozen=True)
class ToolRow:
    group_key: str
    group_kind: str  # 'native' | 'mcp_server' | 'mcp_tool' (when detail=True)
    invocations: int
    isolated_turns: int
    isolated_cost_usd: float
    shared_turns: int
    shared_exposure_usd: float


def query_tool_costs(
    conn: sqlite3.Connection,
    *,
    session_ids: list[str],
    detail: bool,
    top: int,
    sort: SortKey,
) -> list[ToolRow]:
    if not session_ids:
        return []
    if sort not in _SORT_TO_SQL:
        raise ValueError(f"invalid sort key: {sort!r}")

    group_key_expr = "mtu.tool_name" if detail else "COALESCE(mtu.mcp_server, mtu.tool_name)"
    group_kind_expr = (
        "CASE WHEN mtu.mcp_server IS NOT NULL THEN 'mcp_tool' ELSE 'native' END"
        if detail
        else "CASE WHEN mtu.mcp_server IS NOT NULL THEN 'mcp_server' ELSE 'native' END"
    )

    placeholders = ",".join("?" * len(session_ids))
    sql = f"""
WITH per_message_tool_count AS (
  SELECT message_dedup_key, COUNT(*) AS n_tools
  FROM message_tool_uses
  GROUP BY message_dedup_key
),
tool_keys AS (
  SELECT
    mtu.message_dedup_key,
    {group_key_expr} AS group_key,
    {group_kind_expr} AS group_kind
  FROM message_tool_uses mtu
)
SELECT
  tk.group_key,
  tk.group_kind,
  COUNT(*) AS invocations,
  COUNT(DISTINCT CASE WHEN pmtc.n_tools = 1 THEN tk.message_dedup_key END) AS isolated_turns,
  COALESCE(SUM(CASE WHEN pmtc.n_tools = 1 THEN m.cost_usd ELSE 0 END), 0) AS isolated_cost_usd,
  COUNT(DISTINCT CASE WHEN pmtc.n_tools > 1 THEN tk.message_dedup_key END) AS shared_turns,
  COALESCE(SUM(CASE WHEN pmtc.n_tools > 1 THEN m.cost_usd ELSE 0 END), 0) AS shared_exposure_usd
FROM tool_keys tk
JOIN per_message_tool_count pmtc USING (message_dedup_key)
JOIN messages m ON m.dedup_key = tk.message_dedup_key
WHERE m.session_id IN ({placeholders})
GROUP BY tk.group_key, tk.group_kind
ORDER BY {_SORT_TO_SQL[sort]}
LIMIT ?
"""
    rows = conn.execute(sql, (*session_ids, top)).fetchall()
    return [
        ToolRow(
            group_key=r[0],
            group_kind=r[1],
            invocations=r[2],
            isolated_turns=r[3],
            isolated_cost_usd=r[4],
            shared_turns=r[5],
            shared_exposure_usd=r[6],
        )
        for r in rows
    ]


_FOOTER = (
    "Isolated $ is exact. Shared $ is an upper bound — when a turn emits "
    "multiple tools, the same turn cost appears under each sibling. "
    "Do not sum the Shared $ column across rows."
)


def render_text(rows: list[ToolRow]) -> str:
    if not rows:
        return "No tool usage in scope.\n"

    name_w = max(len("TOOL / MCP SERVER"), max(len(_label(r)) for r in rows))
    header = (
        f"{'TOOL / MCP SERVER':<{name_w}}  "
        f"{'INVOCATIONS':>11}  {'ISOLATED TURNS':>14}  "
        f"{'ISOLATED $':>10}  {'SHARED TURNS':>12}  {'SHARED $≤':>9}"
    )
    sep = "─" * len(header)
    lines = [header, sep]
    iso_total = 0
    inv_total = 0
    iso_cost_total = 0.0
    shr_turn_total = 0
    shr_cost_total = 0.0
    for r in rows:
        lines.append(
            f"{_label(r):<{name_w}}  "
            f"{r.invocations:>11,}  {r.isolated_turns:>14,}  "
            f"{r.isolated_cost_usd:>10.2f}  {r.shared_turns:>12,}  "
            f"{r.shared_exposure_usd:>9.2f}"
        )
        iso_total += r.isolated_turns
        inv_total += r.invocations
        iso_cost_total += r.isolated_cost_usd
        shr_turn_total += r.shared_turns
        shr_cost_total += r.shared_exposure_usd
    lines.append(sep)
    lines.append(
        f"{'TOTALS':<{name_w}}  "
        f"{inv_total:>11,}  {iso_total:>14,}  "
        f"{iso_cost_total:>10.2f}  {shr_turn_total:>12,}  "
        f"{shr_cost_total:>9.2f}"
    )
    lines.append("")
    lines.append(_FOOTER)
    return "\n".join(lines) + "\n"


def _label(r: ToolRow) -> str:
    if r.group_kind == "mcp_server":
        return f"mcp__{r.group_key} (server)"
    return r.group_key


def render_json(rows: list[ToolRow]) -> dict[str, Any]:
    return {
        "rows": [
            {
                "group_key": r.group_key,
                "group_kind": r.group_kind,
                "invocations": r.invocations,
                "isolated_turns": r.isolated_turns,
                "isolated_cost_usd": r.isolated_cost_usd,
                "shared_turns": r.shared_turns,
                "shared_exposure_usd": r.shared_exposure_usd,
            }
            for r in rows
        ],
        "_meta": {"footer": _FOOTER},
    }


def render_csv(rows: list[ToolRow]) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "group_key",
            "group_kind",
            "invocations",
            "isolated_turns",
            "isolated_cost_usd",
            "shared_turns",
            "shared_exposure_usd",
        ]
    )
    for r in rows:
        w.writerow(
            [
                r.group_key,
                r.group_kind,
                r.invocations,
                r.isolated_turns,
                f"{r.isolated_cost_usd:.6f}",
                r.shared_turns,
                f"{r.shared_exposure_usd:.6f}",
            ]
        )
    return buf.getvalue()
