from __future__ import annotations

import os
import sqlite3


class AmbiguousPrefix(Exception):  # noqa: N818 — spec-mandated public name
    def __init__(self, prefix: str, matches: list[str]) -> None:
        super().__init__(f"{prefix!r} matches {len(matches)} sessions: {matches}")
        self.prefix = prefix
        self.matches = matches


class SessionNotFound(Exception):  # noqa: N818 — spec-mandated public name
    def __init__(self, spec: str) -> None:
        super().__init__(f"no session matches {spec!r}")
        self.spec = spec


def resolve_session_id(spec: str, conn: sqlite3.Connection) -> str:
    """Resolve a session spec (full id, prefix ≥6 chars, or absolute .jsonl path)."""
    if spec.endswith(".jsonl") and os.path.isabs(spec):
        row = conn.execute("SELECT session_id FROM files WHERE path=?", (spec,)).fetchone()
        if row is None:
            raise SessionNotFound(spec)
        return str(row[0])

    if len(spec) < 6:
        raise ValueError("session prefix must be ≥6 characters")

    rows = conn.execute(
        "SELECT DISTINCT session_id FROM files WHERE session_id LIKE ?",
        (spec + "%",),
    ).fetchall()
    if not rows:
        raise SessionNotFound(spec)
    if len(rows) > 1:
        raise AmbiguousPrefix(spec, sorted(r[0] for r in rows))
    return str(rows[0][0])
