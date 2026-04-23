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
