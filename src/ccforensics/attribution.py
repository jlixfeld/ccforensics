"""Classify messages into buckets and populate session_rollups."""

from __future__ import annotations

import logging
import sqlite3
from enum import StrEnum

logger = logging.getLogger("ccforensics.attribution")


class BucketKind(StrEnum):
    MAIN = "main"
    AUTO_COMPACT = "auto-compact"
    SUBAGENT = "subagent"
    UNATTRIBUTED = "unattributed"


_BUCKET_KIND_EXPR = f"""
    CASE
        WHEN f.kind = 'main'         THEN '{BucketKind.MAIN}'
        WHEN f.kind = 'auto-compact' THEN '{BucketKind.AUTO_COMPACT}'
        WHEN f.kind = 'subagent'
             AND s.parent_message_dedup_key IS NOT NULL
             AND s.subagent_type IS NOT NULL
            THEN '{BucketKind.SUBAGENT}'
        ELSE '{BucketKind.UNATTRIBUTED}'
    END
"""

_BUCKET_NAME_EXPR = f"""
    CASE
        WHEN f.kind = 'main'         THEN '{BucketKind.MAIN}'
        WHEN f.kind = 'auto-compact' THEN '{BucketKind.AUTO_COMPACT}'
        WHEN f.kind = 'subagent'
             AND s.parent_message_dedup_key IS NOT NULL
             AND s.subagent_type IS NOT NULL
            THEN s.subagent_type
        ELSE '{BucketKind.UNATTRIBUTED}'
    END
"""


def recompute_session_rollups(conn: sqlite3.Connection, session_id: str) -> None:
    """Delete and re-insert one rollup row per (bucket_kind, bucket_name)."""
    conn.execute("DELETE FROM session_rollups WHERE session_id = ?", (session_id,))
    conn.execute(
        f"""
        INSERT INTO session_rollups (
            session_id, bucket_kind, bucket_name,
            cost_usd, input_tokens, output_tokens,
            cache_create, cache_read
        )
        SELECT
            m.session_id,
            {_BUCKET_KIND_EXPR} AS bk,
            {_BUCKET_NAME_EXPR} AS bn,
            COALESCE(SUM(m.cost_usd), 0.0),
            COALESCE(SUM(m.input_tokens), 0),
            COALESCE(SUM(m.output_tokens), 0),
            COALESCE(SUM(m.cache_creation), 0),
            COALESCE(SUM(m.cache_read), 0)
        FROM messages m
        JOIN files f ON m.file_path = f.path
        LEFT JOIN subagent_spawns s ON f.path = s.child_file_path
        WHERE m.session_id = ?
        GROUP BY m.session_id, bk, bn
        """,
        (session_id,),
    )


def backfill_spawn_totals(conn: sqlite3.Connection, session_id: str) -> None:
    """Populate subagent_spawns.total_* — direct cost only, no nested rollup."""
    conn.execute(
        """
        UPDATE subagent_spawns
        SET
            total_cost_usd = (
                SELECT COALESCE(SUM(m.cost_usd), 0.0)
                  FROM messages m
                 WHERE m.file_path = subagent_spawns.child_file_path
            ),
            total_input = (
                SELECT COALESCE(SUM(m.input_tokens), 0)
                  FROM messages m
                 WHERE m.file_path = subagent_spawns.child_file_path
            ),
            total_output = (
                SELECT COALESCE(SUM(m.output_tokens), 0)
                  FROM messages m
                 WHERE m.file_path = subagent_spawns.child_file_path
            ),
            total_cache_create = (
                SELECT COALESCE(SUM(m.cache_creation), 0)
                  FROM messages m
                 WHERE m.file_path = subagent_spawns.child_file_path
            ),
            total_cache_read = (
                SELECT COALESCE(SUM(m.cache_read), 0)
                  FROM messages m
                 WHERE m.file_path = subagent_spawns.child_file_path
            ),
            ts_returned = (
                SELECT MAX(m.ts)
                  FROM messages m
                 WHERE m.file_path = subagent_spawns.child_file_path
            )
        WHERE parent_session_id = ?
        """,
        (session_id,),
    )


def find_invariant_violators(conn: sqlite3.Connection, tolerance: float = 1e-6) -> list[str]:
    """Return session_ids where ``sum(messages.cost_usd) != sum(rollups.cost_usd)``.

    A failed recompute (e.g. transient I/O reading the main file during
    ``recompute_session_summary``) leaves rollups behind the live messages
    table. Subsequent incremental reconciles don't catch the drift —
    ``_row_is_unchanged`` sees the file's mtime/size as unchanged so the
    session never makes it onto ``stats.sessions_recomputed`` again.

    This pre-aggregates both tables into per-session sums in two GROUP BY
    passes, then reports sessions whose totals differ. Used by the
    reconcile self-heal pass to identify stale rollups in one shot.
    """
    rows = conn.execute(
        """
        WITH msg_sums AS (
          SELECT session_id, COALESCE(SUM(cost_usd), 0.0) AS msg_cost
            FROM messages
           GROUP BY session_id
        ),
        roll_sums AS (
          SELECT session_id, COALESCE(SUM(cost_usd), 0.0) AS roll_cost
            FROM session_rollups
           GROUP BY session_id
        )
        SELECT ms.session_id
          FROM msg_sums ms
     LEFT JOIN roll_sums rs ON rs.session_id = ms.session_id
         WHERE ABS(ms.msg_cost - COALESCE(rs.roll_cost, 0.0)) > ?
        UNION
        SELECT rs2.session_id
          FROM roll_sums rs2
     LEFT JOIN msg_sums ms2 ON ms2.session_id = rs2.session_id
         WHERE ms2.session_id IS NULL AND rs2.roll_cost > ?
        """,
        (tolerance, tolerance),
    ).fetchall()
    return [str(r[0]) for r in rows]


def verify_invariant(
    conn: sqlite3.Connection, session_id: str, tolerance: float = 1e-6
) -> tuple[bool, float, float]:
    """Return (passed, session_total, rollup_total). NULL costs skipped."""
    session_total = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    rollup_total = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0.0) FROM session_rollups WHERE session_id = ?",
        (session_id,),
    ).fetchone()[0]
    passed = abs(float(session_total) - float(rollup_total)) <= tolerance
    return passed, float(session_total), float(rollup_total)
