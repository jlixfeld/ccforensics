"""Attribution: classify each message into a bucket + populate session_rollups.

Buckets (spec §4.2):

- ``main``: message in a main session JSONL.
- ``subagent:<type>``: message in a subagent JSONL with a resolvable
  spawn. ``<type>`` comes from ``subagent_spawns.subagent_type``
  (meta.json::agentType else parent tool_use input.subagent_type).
- ``auto-compact``: message in an ``agent-acompact-<hex>.jsonl`` file —
  Claude Code's internal compaction worker; billable but not
  user-spawned.
- ``unattributed``: subagent JSONL with an unresolvable parent, or
  subagent_type missing entirely.

Hard invariant: ``sum(rollups) == sum(messages.cost_usd)`` per session,
because every message is in exactly one bucket. Classification is
SQL-driven, so the invariant is automatic rather than asserted.

``subagent_spawns.total_*`` columns are backfilled per-session as the
sum of each child file's own messages. Recursive rollup (a spawn
totalling its nested spawns' cost) is deferred — the per-file totals
are the right granularity for the session-level bucket view.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger("ccforensics.attribution")


_BUCKET_KIND_EXPR = """
    CASE
        WHEN f.kind = 'main'         THEN 'main'
        WHEN f.kind = 'auto-compact' THEN 'auto-compact'
        WHEN f.kind = 'subagent'
             AND s.parent_message_dedup_key IS NOT NULL
             AND s.subagent_type IS NOT NULL
            THEN 'subagent'
        ELSE 'unattributed'
    END
"""

_BUCKET_NAME_EXPR = """
    CASE
        WHEN f.kind = 'main'         THEN 'main'
        WHEN f.kind = 'auto-compact' THEN 'auto-compact'
        WHEN f.kind = 'subagent'
             AND s.parent_message_dedup_key IS NOT NULL
             AND s.subagent_type IS NOT NULL
            THEN s.subagent_type
        ELSE 'unattributed'
    END
"""


def recompute_session_rollups(conn: sqlite3.Connection, session_id: str) -> None:
    """Recompute ``session_rollups`` rows for ``session_id``.

    Deletes existing rows for the session and re-inserts one row per
    (bucket_kind, bucket_name) present in the session's messages.
    Sessions with zero messages produce no rows.
    """
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
    """Populate ``subagent_spawns.total_*`` for every spawn whose parent
    session is ``session_id`` — direct-cost only (the child file's own
    messages, no nested rollup).
    """
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
    """Check ``sum(rollups.cost_usd) == sum(messages.cost_usd)``.

    Returns ``(passed, session_total, rollup_total)``. NULL costs are
    ignored on both sides (messages with unresolvable pricing don't
    contribute to either). Tolerance is an absolute-difference check in
    dollars.
    """
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
