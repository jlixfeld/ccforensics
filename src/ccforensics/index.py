from __future__ import annotations

import sqlite3
from pathlib import Path

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
