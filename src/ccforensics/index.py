from __future__ import annotations

import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .jsonl import annotate_cost, dedup_key, parse_file
from .models import TranscriptEntry

logger = logging.getLogger("ccforensics.index")

_SUBAGENT_FILENAME = re.compile(r"^agent-([0-9a-f]+)\.jsonl$", re.IGNORECASE)

CURRENT_SCHEMA_VERSION = 1

# Ordered DDL statements applied when migrating from version N-1 to N.
# To add a schema version, append a new entry to MIGRATIONS and bump
# CURRENT_SCHEMA_VERSION.
MIGRATIONS: list[list[str]] = [
    # v0 -> v1: initial schema
    [
        """CREATE TABLE files (
            path            TEXT PRIMARY KEY,
            mtime_ns        INTEGER NOT NULL,
            size            INTEGER NOT NULL,
            session_id      TEXT NOT NULL,
            kind            TEXT NOT NULL,
            agent_id        TEXT,
            schema_version  TEXT,
            parse_warnings  INTEGER NOT NULL DEFAULT 0,
            last_parsed_at  INTEGER NOT NULL
        )""",
        "CREATE INDEX idx_files_session ON files(session_id)",
        """CREATE TABLE messages (
            dedup_key                   TEXT PRIMARY KEY,
            file_path                   TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
            session_id                  TEXT NOT NULL,
            uuid                        TEXT,
            parent_uuid                 TEXT,
            source_tool_use_id          TEXT,
            source_tool_assistant_uuid  TEXT,
            tool_use_id                 TEXT,
            tool_name                   TEXT,
            agent_id                    TEXT,
            role                        TEXT NOT NULL,
            type                        TEXT NOT NULL,
            model                       TEXT,
            ts                          INTEGER NOT NULL,
            is_sidechain                INTEGER NOT NULL DEFAULT 0,
            is_meta                     INTEGER NOT NULL DEFAULT 0,
            input_tokens                INTEGER,
            output_tokens               INTEGER,
            cache_creation              INTEGER,
            cache_read                  INTEGER,
            cost_usd                    REAL,
            raw_pointer                 INTEGER
        )""",
        "CREATE INDEX idx_messages_session ON messages(session_id)",
        "CREATE INDEX idx_messages_tool_use_id ON messages(tool_use_id)",
        "CREATE INDEX idx_messages_source_tool ON messages(source_tool_use_id)",
        "CREATE INDEX idx_messages_agent ON messages(agent_id)",
        "CREATE INDEX idx_messages_ts ON messages(ts)",
        """CREATE TABLE subagent_spawns (
            spawn_id                  TEXT PRIMARY KEY,
            parent_session_id         TEXT NOT NULL,
            parent_message_dedup_key  TEXT REFERENCES messages(dedup_key),
            child_agent_id            TEXT,
            child_file_path           TEXT,
            subagent_type             TEXT,
            description               TEXT,
            model                     TEXT,
            ts_spawned                INTEGER NOT NULL,
            ts_returned               INTEGER,
            total_cost_usd            REAL,
            total_input               INTEGER,
            total_output              INTEGER,
            total_cache_create        INTEGER,
            total_cache_read          INTEGER
        )""",
        "CREATE INDEX idx_spawns_session ON subagent_spawns(parent_session_id)",
        "CREATE INDEX idx_spawns_type ON subagent_spawns(subagent_type)",
        """CREATE TABLE skill_activations (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id               TEXT NOT NULL,
            skill_path               TEXT NOT NULL,
            skill_name               TEXT NOT NULL,
            plugin_name              TEXT,
            source                   TEXT NOT NULL,
            activated_at             INTEGER NOT NULL,
            activated_by_dedup_key   TEXT,
            content_size             INTEGER,
            estimated_cost_usd       REAL,
            estimated_cost_band_usd  REAL
        )""",
        "CREATE INDEX idx_skills_session ON skill_activations(session_id)",
        "CREATE INDEX idx_skills_plugin ON skill_activations(plugin_name)",
        """CREATE TABLE plugins (
            name           TEXT PRIMARY KEY,
            version        TEXT,
            install_path   TEXT NOT NULL,
            scope          TEXT,
            manifest_json  TEXT
        )""",
        """CREATE TABLE user_level_artifacts (
            path  TEXT PRIMARY KEY,
            kind  TEXT NOT NULL,
            name  TEXT NOT NULL
        )""",
        """CREATE TABLE session_summaries (
            session_id       TEXT PRIMARY KEY,
            project_path     TEXT,
            project_display  TEXT,
            started_at       INTEGER NOT NULL,
            last_active_at   INTEGER NOT NULL,
            duration_s       INTEGER NOT NULL,
            turn_count       INTEGER NOT NULL,
            total_cost_usd   REAL,
            summary_text     TEXT,
            summary_source   TEXT
        )""",
        """CREATE TABLE session_rollups (
            session_id     TEXT NOT NULL,
            bucket_kind    TEXT NOT NULL,
            bucket_name    TEXT NOT NULL,
            cost_usd       REAL NOT NULL,
            input_tokens   INTEGER NOT NULL,
            output_tokens  INTEGER NOT NULL,
            cache_create   INTEGER NOT NULL,
            cache_read     INTEGER NOT NULL,
            PRIMARY KEY (session_id, bucket_kind, bucket_name)
        )""",
    ],
]


def open_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA user_version")
    current = cur.fetchone()[0]
    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"index schema version {current} is newer than this ccforensics "
            f"build supports ({CURRENT_SCHEMA_VERSION}). Upgrade ccforensics."
        )
    for target in range(current, CURRENT_SCHEMA_VERSION):
        for ddl in MIGRATIONS[target]:
            conn.execute(ddl)
        conn.execute(f"PRAGMA user_version = {target + 1}")
    conn.commit()


def _classify_file(path: Path) -> tuple[str, str | None, str]:
    """Return ``(kind, agent_id, session_id)``.

    - Main session: ``~/.claude/projects/<enc>/<sessionId>.jsonl``
    - Subagent:     ``~/.claude/projects/<enc>/<sessionId>/subagents/agent-<id>.jsonl``
    """
    name = path.name
    m = _SUBAGENT_FILENAME.match(name)
    if m and path.parent.name == "subagents":
        return ("subagent", m.group(1), path.parent.parent.name)
    return ("main", None, path.stem)


def _row_is_unchanged(
    conn: sqlite3.Connection, path: Path, mtime_ns: int, size: int
) -> bool:
    cur = conn.execute("SELECT mtime_ns, size FROM files WHERE path=?", (str(path),))
    row = cur.fetchone()
    return bool(row) and row[0] == mtime_ns and row[1] == size


def _purge_file_rows(conn: sqlite3.Connection, path: Path) -> None:
    s = str(path)
    conn.execute("DELETE FROM subagent_spawns WHERE child_file_path=?", (s,))
    conn.execute(
        "DELETE FROM skill_activations WHERE activated_by_dedup_key IN "
        "(SELECT dedup_key FROM messages WHERE file_path=?)",
        (s,),
    )
    conn.execute("DELETE FROM messages WHERE file_path=?", (s,))
    conn.execute("DELETE FROM files WHERE path=?", (s,))


def _insert_message(
    conn: sqlite3.Connection,
    file_path: str,
    session_id: str,
    agent_id: str | None,
    entry: TranscriptEntry,
    cost_usd: float | None,
    key: str,
) -> None:
    msg = entry.message
    usage = msg.usage if msg else None
    tool_use_id = None
    tool_name = None
    if msg and msg.content:
        for block in msg.content:
            if block.type == "tool_use":
                tool_use_id = block.id
                tool_name = block.name
                break
    conn.execute(
        """INSERT OR REPLACE INTO messages (
            dedup_key, file_path, session_id, uuid, parent_uuid,
            source_tool_use_id, source_tool_assistant_uuid,
            tool_use_id, tool_name, agent_id, role, type, model, ts,
            is_sidechain, is_meta,
            input_tokens, output_tokens, cache_creation, cache_read, cost_usd
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            key,
            file_path,
            session_id,
            entry.uuid,
            entry.parent_uuid,
            entry.source_tool_use_id,
            entry.source_tool_assistant_uuid,
            tool_use_id,
            tool_name,
            agent_id or entry.agent_id,
            (msg.role if msg else entry.type),
            entry.type,
            msg.model if msg else None,
            int(entry.timestamp.timestamp()),
            1 if entry.is_sidechain else 0,
            1 if entry.is_meta else 0,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
            usage.cache_creation_input_tokens if usage else None,
            usage.cache_read_input_tokens if usage else None,
            cost_usd,
        ),
    )


def reconcile_file(
    conn: sqlite3.Connection, path: Path, pricing_data: dict[str, Any]
) -> None:
    stat = path.stat()
    mtime_ns = stat.st_mtime_ns
    size = stat.st_size

    if _row_is_unchanged(conn, path, mtime_ns, size):
        return

    _purge_file_rows(conn, path)

    kind, agent_id, session_id = _classify_file(path)

    result = parse_file(path)
    annotated = annotate_cost(result.entries, pricing_data)

    seen: dict[str, tuple[TranscriptEntry, float | None]] = {}
    kept_non_keyed: list[tuple[TranscriptEntry, float | None]] = []
    for a in annotated:
        key = dedup_key(a.entry)
        if key is None:
            kept_non_keyed.append((a.entry, a.cost_usd))
            continue
        prev = seen.get(key)
        if prev is None or a.entry.timestamp < prev[0].timestamp:
            seen[key] = (a.entry, a.cost_usd)

    conn.execute(
        """INSERT OR REPLACE INTO files
           (path, mtime_ns, size, session_id, kind, agent_id, schema_version,
            parse_warnings, last_parsed_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            str(path),
            mtime_ns,
            size,
            session_id,
            kind,
            agent_id,
            next(iter(result.seen_versions), None) if result.seen_versions else None,
            len(result.warnings),
            int(time.time()),
        ),
    )

    for key, (entry, cost) in seen.items():
        _insert_message(conn, str(path), session_id, agent_id, entry, cost, key)

    for i, (entry, cost) in enumerate(kept_non_keyed):
        synth = f"file:{path}:{i}"
        _insert_message(conn, str(path), session_id, agent_id, entry, cost, synth)


def count_messages_for_file(conn: sqlite3.Connection, path: Path) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE file_path=?", (str(path),)
    ).fetchone()
    return int(row[0])
