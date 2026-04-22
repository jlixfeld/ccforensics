from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .jsonl import annotate_cost, dedup_key, parse_file
from .models import TranscriptEntry
from .paths import decode_project_dirname

logger = logging.getLogger("ccforensics.index")

_SUBAGENT_FILENAME = re.compile(r"^agent-([0-9a-f]+)\.jsonl$", re.IGNORECASE)

_COMMAND_WRAPPER_RE = re.compile(
    r"<command-(name|message|args)>.*?</command-\1>",
    re.DOTALL,
)
_LOCAL_WRAPPER_RE = re.compile(
    r"<(local-command-(?:caveat|stdout|stderr)|bash-(?:input|stdout|stderr))>.*?</\1>",
    re.DOTALL,
)
_IDE_ATTACHMENT_RE = re.compile(
    r"<ide[^>]*>.*?<file[^>]*>(?P<path>[^<]+)</file>.*?</ide[^>]*>",
    re.DOTALL,
)
_HOOK_INJECTION_MARKER = "<session-start-hook>"


def _sanitize_prompt(text: str) -> str:
    """Strip command/bash wrappers, replace IDE attachments, collapse whitespace, cap at 1000.

    An empty string after sanitization is meaningful: callers treat it the
    same as "no text block" and fall through to the next candidate prompt.
    """
    text = _LOCAL_WRAPPER_RE.sub("", text)
    text = _COMMAND_WRAPPER_RE.sub("", text)
    text = _IDE_ATTACHMENT_RE.sub(lambda m: f"\U0001f4ce {m.group('path').strip()}", text)
    text = " ".join(text.split())
    return text[:1000]


def _is_pure_hook_injection(text: str) -> bool:
    """Heuristic: length-gated marker check to skip hook-bootstrap blobs as summaries.

    Returns True only when the text both contains the ``<session-start-hook>``
    marker AND exceeds 500 characters. Real hook-bootstrap payloads are
    multi-KB, so 500 comfortably separates them from a legitimate user
    message that merely mentions the marker string (e.g., a user asking
    about hooks) while still matching any plausible bootstrap blob.
    """
    return _HOOK_INJECTION_MARKER in text and len(text) > 500


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

    Files under a ``subagents/`` directory whose name doesn't match the
    ``agent-<hex>.jsonl`` pattern are classified as ``subagent`` with
    ``agent_id=None`` and a warning is logged — never silently mislabeled
    as ``main`` (which would make a future Claude Code rename of the
    subagent filename convention quietly mis-attribute every subagent
    file).
    """
    name = path.name
    if path.parent.name == "subagents":
        m = _SUBAGENT_FILENAME.match(name)
        if m:
            return ("subagent", m.group(1), path.parent.parent.name)
        logger.warning(
            "subagent file %s under subagents/ doesn't match agent-<hex>.jsonl; "
            "classifying as subagent with no agent_id",
            path,
        )
        return ("subagent", None, path.parent.parent.name)
    return ("main", None, path.stem)


def _row_is_unchanged(conn: sqlite3.Connection, path: Path, mtime_ns: int, size: int) -> bool:
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


def reconcile_file(conn: sqlite3.Connection, path: Path, pricing_data: dict[str, Any]) -> None:
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
    row = conn.execute("SELECT COUNT(*) FROM messages WHERE file_path=?", (str(path),)).fetchone()
    return int(row[0])


def _first_text_block(entry: TranscriptEntry) -> str | None:
    """Return the first ``text`` content block's text, or ``None``."""
    if entry.message is None or not entry.message.content:
        return None
    for block in entry.message.content:
        if block.type == "text" and block.text:
            return block.text
    return None


def _extract_summary(
    entries: list[TranscriptEntry],
    session_id: str,
) -> tuple[str, str]:
    """Apply the summary-extraction priority chain.

    Returns ``(summary_text, summary_source)`` where source is one of
    ``'claude-summary'``, ``'first-prompt'``, or ``'none'``.
    """
    session_uuids = {e.uuid for e in entries if e.uuid and e.session_id == session_id}

    # Priority 1: type='summary' whose leafUuid matches a uuid in this session.
    summary_matches: list[TranscriptEntry] = [
        e
        for e in entries
        if e.type == "summary" and e.leaf_uuid and e.leaf_uuid in session_uuids and e.summary
    ]
    if summary_matches:
        winner = max(summary_matches, key=lambda e: e.timestamp)
        # Sanitize even Claude-emitted summaries — they're plain text, but
        # collapse whitespace and cap length to keep the schema invariant.
        return (_sanitize_prompt(winner.summary or ""), "claude-summary")

    # Priority 2: isCompactSummary=true entries in this session, most recent.
    compact = [e for e in entries if e.is_compact_summary and e.session_id == session_id]
    if compact:
        winner = max(compact, key=lambda e: e.timestamp)
        text = _first_text_block(winner) or winner.summary or ""
        if text:
            return (_sanitize_prompt(text), "claude-summary")

    # Priority 3: first eligible user prompt — sanitized; skip pure-hook-injection.
    user_prompts = sorted(
        (
            e
            for e in entries
            if e.type == "user"
            and not e.is_meta
            and not e.is_sidechain
            and e.session_id == session_id
            and e.message is not None
            and e.message.role == "user"
        ),
        key=lambda e: e.timestamp,
    )
    for prompt in user_prompts:
        prompt_text = _first_text_block(prompt)
        if prompt_text is None:
            continue
        if _is_pure_hook_injection(prompt_text):
            continue
        sanitized = _sanitize_prompt(prompt_text)
        if sanitized:
            return (sanitized, "first-prompt")

    return ("<no summary available>", "none")


def recompute_session_summary(conn: sqlite3.Connection, session_id: str) -> None:
    """Recompute the ``session_summaries`` row for ``session_id``.

    Numeric fields come from SQL aggregation on ``messages``. Text fields
    come from re-parsing the session's main JSONL (when one exists). If the
    session has no rows in ``messages``, no summary row is written and any
    pre-existing one is left alone.

    Why we re-parse the main JSONL: the ``messages`` table doesn't store
    ``cwd`` or the per-entry ``summary``/text fields needed by the summary
    extraction chain (that would require a schema migration). Re-parsing is
    the cheapest way to recover those without widening the schema.

    Known drift: if the main file mutates between ``reconcile_file`` and
    this call, the ``cwd`` read here reflects post-reconcile disk state
    rather than what was indexed. This is acceptable because
    ``reconcile_projects_dir`` calls this immediately after the file walk
    (tiny window) and the ``FileNotFoundError`` path is caught by the
    caller's per-session error-isolation wrapper (see
    ``reconcile_projects_dir``), so a mid-pass delete doesn't abort the
    remaining recomputes.
    """
    agg = conn.execute(
        """SELECT MIN(ts), MAX(ts),
                  SUM(CASE WHEN role='user' AND is_meta=0 AND is_sidechain=0 THEN 1 ELSE 0 END),
                  SUM(cost_usd),
                  SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END)
             FROM messages WHERE session_id=?""",
        (session_id,),
    ).fetchone()
    started_at, last_active_at, turn_count, cost_sum, cost_non_null = agg
    if started_at is None:
        # No messages → no row to write.
        return

    total_cost: float | None = float(cost_sum) if cost_non_null else None
    duration_s = int(last_active_at) - int(started_at)
    turn_count = int(turn_count or 0)

    main_row = conn.execute(
        "SELECT path FROM files WHERE session_id=? AND kind='main' LIMIT 1",
        (session_id,),
    ).fetchone()

    project_path: str | None = None
    project_display: str | None = None
    summary_text = "<no summary available>"
    summary_source = "none"

    if main_row is not None:
        main_path = Path(main_row[0])
        # NOTE: double-parse — reconcile_file already parsed this file, but
        # cwd + per-entry summary text aren't stored in messages, so we
        # re-parse here. Caller (reconcile_projects_dir) must catch
        # FileNotFoundError / OSError for mid-pass deletes.
        result = parse_file(main_path)
        entries = result.entries

        # cwd from first entry that carries one (timestamp order).
        cwd_entry = next(
            (e for e in sorted(entries, key=lambda e: e.timestamp) if e.cwd),
            None,
        )
        if cwd_entry is not None and cwd_entry.cwd:
            project_path = cwd_entry.cwd
        else:
            try:
                project_path = str(decode_project_dirname(main_path.parent.name))
            except ValueError:
                project_path = None

        if project_path:
            project_display = Path(project_path).name[:30]

        summary_text, summary_source = _extract_summary(entries, session_id)

    conn.execute(
        """INSERT OR REPLACE INTO session_summaries (
            session_id, project_path, project_display,
            started_at, last_active_at, duration_s,
            turn_count, total_cost_usd,
            summary_text, summary_source
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            session_id,
            project_path,
            project_display,
            int(started_at),
            int(last_active_at),
            duration_s,
            turn_count,
            total_cost,
            summary_text,
            summary_source,
        ),
    )


@dataclass
class ReconcileStats:
    files_scanned: int = 0
    files_indexed: int = 0
    files_changed: int = 0
    files_skipped_unchanged: int = 0
    sessions_recomputed: set[str] = field(default_factory=set)


def reconcile_projects_dir(
    conn: sqlite3.Connection,
    projects_dir: Path,
    pricing_data: dict[str, Any],
) -> ReconcileStats:
    """Walk the Claude Code projects directory and reconcile every *.jsonl file.

    Includes both main session files and subagent files under <session>/subagents/.
    Commits per file so a mid-walk interrupt only loses the file currently
    being parsed (each ``reconcile_file`` is idempotent). After the file
    loop, every session whose files changed has its ``session_summaries``
    row recomputed.
    """
    stats = ReconcileStats()
    if not projects_dir.exists():
        return stats

    for path in sorted(projects_dir.rglob("*.jsonl")):
        stats.files_scanned += 1
        stat = path.stat()
        if _row_is_unchanged(conn, path, stat.st_mtime_ns, stat.st_size):
            stats.files_skipped_unchanged += 1
            continue
        _, _, session_id = _classify_file(path)
        reconcile_file(conn, path, pricing_data)
        conn.commit()
        stats.files_indexed += 1
        stats.files_changed += 1
        stats.sessions_recomputed.add(session_id)

    for sid in stats.sessions_recomputed:
        try:
            recompute_session_summary(conn, sid)
        except Exception:
            # TOCTOU: Claude Code may delete/rotate a main file between the
            # file walk and this pass, so parse_file raises FileNotFoundError.
            # Other I/O errors (permissions, disk read) are similarly
            # isolated here so one bad session can't abort the remaining
            # recomputes or lose the final commit for sessions already
            # summarized in this pass.
            logger.warning(
                "failed to recompute session_summaries for session_id=%s; skipping",
                sid,
                exc_info=True,
            )
    conn.commit()
    return stats


@dataclass
class IndexStats:
    files: int
    messages: int
    sessions: int
    subagent_spawns: int
    skill_activations: int
    db_size_bytes: int
    last_refresh: int | None


def collect_stats(conn: sqlite3.Connection, db_path: Path) -> IndexStats:
    def count(table: str) -> int:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0])

    def distinct_sessions() -> int:
        row = conn.execute("SELECT COUNT(DISTINCT session_id) FROM files").fetchone()
        return int(row[0])

    last_row = conn.execute("SELECT MAX(last_parsed_at) FROM files").fetchone()
    last_refresh = int(last_row[0]) if last_row and last_row[0] else None

    size = db_path.stat().st_size if db_path.exists() else 0

    return IndexStats(
        files=count("files"),
        messages=count("messages"),
        sessions=distinct_sessions(),
        subagent_spawns=count("subagent_spawns"),
        skill_activations=count("skill_activations"),
        db_size_bytes=size,
        last_refresh=last_refresh,
    )
