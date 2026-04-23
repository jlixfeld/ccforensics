"""Skill activation detection via three channels (Skill tool, Read, SessionStart hook)."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .models import TranscriptEntry

logger = logging.getLogger("ccforensics.skills")

# Plugin cache layout: ~/.claude/plugins/cache/<market>/<plugin>/<ver>/skills/<name>/SKILL.md
_SKILL_MD_PATH_RE = re.compile(r"/skills/([^/]+)/SKILL\.md$")
_PLUGIN_SKILL_PATH_RE = re.compile(
    r"plugins/cache/[^/]+/(?P<plugin>[^/]+)/[^/]+/skills/(?P<skill>[^/]+)/SKILL\.md$"
)
_FRONTMATTER_NAME_RE = re.compile(r"^---\s*\r?\n.*?^name:\s*([^\s]+)", re.MULTILINE | re.DOTALL)
# Bootstrap payload hints: SessionStart hooks that lack frontmatter but still
# carry the using-superpowers skill as context.
_HOOK_SKILL_HINTS = ("using-superpowers", "<session-start-hook>")

Source = Literal["skill-tool", "read", "hook-injection"]


@dataclass(frozen=True)
class SkillActivation:
    """One detected skill activation within a session."""

    session_id: str
    skill_path: str | None
    skill_name: str
    plugin_name: str | None
    source: Source
    activated_at: int
    activated_by_dedup_key: str | None
    content_size: int | None


@dataclass
class SkillResolver:
    """Map skill names to SKILL.md paths. Scans on-disk layout at construction."""

    claude_home: Path
    plugins_cache: Path

    def __post_init__(self) -> None:
        from .registry import version_sort_key

        self._plugin_paths: dict[str, Path] = {}
        self._user_paths: dict[str, Path] = {}
        if self.plugins_cache.is_dir():
            # Group installs by plugin name so we can pick the highest-version
            # one deterministically. Filesystem glob order is not stable.
            by_plugin: dict[str, list[tuple[str | None, Path]]] = {}
            for manifest in self.plugins_cache.glob("*/*/*/.claude-plugin/plugin.json"):
                try:
                    data = json.loads(manifest.read_text())
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning(
                        "skill resolver: skipping unreadable manifest %s (%s)", manifest, e
                    )
                    continue
                plugin_name = data.get("name")
                if not isinstance(plugin_name, str):
                    logger.warning(
                        "skill resolver: manifest %s has non-string 'name'; skipping", manifest
                    )
                    continue
                version = data.get("version")
                version = str(version) if version is not None else None
                by_plugin.setdefault(plugin_name, []).append((version, manifest.parent.parent))

            for plugin_name, installs in by_plugin.items():
                _, install = max(installs, key=lambda t: version_sort_key(t[0]))
                skills_dir = install / "skills"
                if not skills_dir.is_dir():
                    continue
                for skill_md in skills_dir.glob("*/SKILL.md"):
                    self._plugin_paths[f"{plugin_name}:{skill_md.parent.name}"] = skill_md
        user_skills = self.claude_home / "skills"
        if user_skills.is_dir():
            for skill_md in user_skills.glob("*/SKILL.md"):
                self._user_paths[skill_md.parent.name] = skill_md

    def resolve_name(self, name: str) -> tuple[Path | None, str | None]:
        """Map a skill name to ``(path, plugin_name)``.

        - ``<plugin>:<skill>`` form → plugin lookup.
        - Bare name → user-level lookup.
        - No match → ``(None, None)``.
        """
        if ":" in name:
            path = self._plugin_paths.get(name)
            if path is not None:
                plugin, _ = name.split(":", 1)
                return path, plugin
            return None, None
        path = self._user_paths.get(name)
        if path is not None:
            return path, None
        return None, None


def plugin_from_path(skill_path: str | Path) -> str | None:
    """Infer plugin name from an absolute SKILL.md path. ``None`` for
    user-level or unrecognized layouts."""
    s = str(skill_path)
    m = _PLUGIN_SKILL_PATH_RE.search(s)
    if m:
        return m.group("plugin")
    return None


def name_from_path(skill_path: str | Path) -> str | None:
    """Extract the skill name (``<name>`` from ``/skills/<name>/SKILL.md``)."""
    s = str(skill_path)
    m = _SKILL_MD_PATH_RE.search(s)
    if m:
        return m.group(1)
    return None


def _file_size_safe(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _detect_channel_a(
    entry: TranscriptEntry,
    session_id: str,
    resolver: SkillResolver,
    dedup_key_for: str | None,
) -> list[SkillActivation]:
    """Scan an assistant message's content for ``tool_use name='Skill'`` blocks."""
    out: list[SkillActivation] = []
    if entry.message is None or not entry.message.content:
        return out
    for block in entry.message.content:
        if block.type != "tool_use" or block.name != "Skill":
            continue
        inp = block.input or {}
        raw_name = inp.get("skill") if isinstance(inp, dict) else None
        if not isinstance(raw_name, str) or not raw_name:
            continue
        path_obj, plugin = resolver.resolve_name(raw_name)
        path_str = str(path_obj) if path_obj else None
        content_size = _file_size_safe(path_obj) if path_obj else None
        skill_name = name_from_path(path_str) if path_str else raw_name.split(":")[-1]
        out.append(
            SkillActivation(
                session_id=session_id,
                skill_path=path_str,
                skill_name=skill_name or raw_name,
                plugin_name=plugin,
                source="skill-tool",
                activated_at=int(entry.timestamp.timestamp()),
                activated_by_dedup_key=dedup_key_for,
                content_size=content_size,
            )
        )
    return out


def _detect_channel_b(
    entry: TranscriptEntry,
    session_id: str,
    dedup_key_for: str | None,
) -> list[SkillActivation]:
    """Scan for ``tool_use name='Read'`` blocks whose ``file_path`` ends
    in a SKILL.md path."""
    out: list[SkillActivation] = []
    if entry.message is None or not entry.message.content:
        return out
    for block in entry.message.content:
        if block.type != "tool_use" or block.name != "Read":
            continue
        inp = block.input or {}
        fp = inp.get("file_path") if isinstance(inp, dict) else None
        if not isinstance(fp, str) or "/SKILL.md" not in fp:
            continue
        skill_name = name_from_path(fp)
        if skill_name is None:
            continue
        plugin = plugin_from_path(fp)
        content_size = _file_size_safe(Path(fp))
        out.append(
            SkillActivation(
                session_id=session_id,
                skill_path=fp,
                skill_name=skill_name,
                plugin_name=plugin,
                source="read",
                activated_at=int(entry.timestamp.timestamp()),
                activated_by_dedup_key=dedup_key_for,
                content_size=content_size,
            )
        )
    return out


def _detect_channel_c(
    entry: TranscriptEntry,
    session_id: str,
    resolver: SkillResolver,
) -> list[SkillActivation]:
    """Parse ``attachment`` entries that inject SKILL.md content at
    SessionStart. Name comes from the frontmatter; hook-specific
    detection means there is no ``dedup_key`` (attachments aren't
    assistant turns).
    """
    out: list[SkillActivation] = []
    if entry.type != "attachment":
        return out
    att = entry.attachment
    if att is None or att.hook_event != "SessionStart" or not att.stdout:
        return out
    try:
        stdout_json = json.loads(att.stdout)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(
            "skill detection: SessionStart hook payload in session %s is not valid JSON (%s); "
            "skipping channel-C",
            session_id,
            e,
        )
        return out
    if not isinstance(stdout_json, dict):
        return out
    ac = stdout_json.get("hookSpecificOutput", {}).get("additionalContext")
    if not isinstance(ac, str) or not ac:
        return out
    m = _FRONTMATTER_NAME_RE.search(ac)
    if m is None:
        # Heuristic hint fallback: even without frontmatter, a
        # SessionStart payload mentioning using-superpowers is a known
        # bootstrap case.
        if not any(h in ac for h in _HOOK_SKILL_HINTS):
            return out
        name = "using-superpowers"
    else:
        name = m.group(1)
    path_obj, plugin = resolver.resolve_name(name)
    # For plain-name bootstraps like 'using-superpowers', add the
    # 'superpowers:' prefix so the lookup finds the plugin path.
    if path_obj is None and ":" not in name:
        path_obj, plugin = resolver.resolve_name(f"superpowers:{name}")
    path_str = str(path_obj) if path_obj else None
    out.append(
        SkillActivation(
            session_id=session_id,
            skill_path=path_str,
            skill_name=name,
            plugin_name=plugin,
            source="hook-injection",
            activated_at=int(entry.timestamp.timestamp()),
            activated_by_dedup_key=None,
            content_size=len(ac),
        )
    )
    return out


def detect_activations(
    entries: Iterable[TranscriptEntry],
    session_id: str,
    resolver: SkillResolver,
    dedup_key_by_uuid: dict[str, str] | None = None,
) -> list[SkillActivation]:
    """Scan a session's entries for skill activations across all 3 channels.

    ``dedup_key_by_uuid`` maps message ``uuid`` → ``dedup_key`` so each
    activation can link back to the (dedup-collapsed) ``messages`` row
    that triggered it. Pass an empty dict if linkage isn't needed.
    """
    keymap = dedup_key_by_uuid or {}
    out: list[SkillActivation] = []
    for entry in entries:
        if entry.session_id != session_id:
            continue
        dedup = keymap.get(entry.uuid or "")
        if entry.type == "assistant":
            out.extend(_detect_channel_a(entry, session_id, resolver, dedup))
            out.extend(_detect_channel_b(entry, session_id, dedup))
        elif entry.type == "attachment":
            out.extend(_detect_channel_c(entry, session_id, resolver))
    return out


def write_activations(
    conn: sqlite3.Connection,
    session_id: str,
    activations: list[SkillActivation],
) -> None:
    """Replace the session's activations in the DB.

    Idempotent per session: deletes prior rows and re-inserts. Leaves
    estimated_cost_usd/band NULL — cost estimation is deferred.
    """
    conn.execute("DELETE FROM skill_activations WHERE session_id = ?", (session_id,))
    for a in activations:
        if a.skill_path is None:
            # Skill name couldn't be resolved to a path on disk — skip
            # (schema requires NOT NULL). Name + source survive in logs
            # for triage; a future refinement can record unresolved
            # activations in a dedicated column or side table.
            logger.warning(
                "skill activation in session %s has no resolvable path "
                "(name=%s, source=%s); skipping row",
                session_id,
                a.skill_name,
                a.source,
            )
            continue
        conn.execute(
            """INSERT INTO skill_activations (
                session_id, skill_path, skill_name, plugin_name, source,
                activated_at, activated_by_dedup_key, content_size,
                estimated_cost_usd, estimated_cost_band_usd
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                a.session_id,
                a.skill_path,
                a.skill_name,
                a.plugin_name,
                a.source,
                a.activated_at,
                a.activated_by_dedup_key,
                a.content_size,
                None,
                None,
            ),
        )


def _dedup_key_map(entries: Iterable[TranscriptEntry]) -> dict[str, str]:
    """Build ``uuid → dedup_key`` from raw entries."""
    from .jsonl import dedup_key as _dk  # local import avoids cycle at module load

    out: dict[str, str] = {}
    for e in entries:
        if not e.uuid:
            continue
        k = _dk(e)
        if k is not None:
            out[e.uuid] = k
    return out


def detect_and_store(
    conn: sqlite3.Connection,
    session_id: str,
    entries: list[TranscriptEntry],
    resolver: SkillResolver,
) -> int:
    """Detect activations in ``entries`` and write them to the index.

    Returns the number of activation rows written.
    """
    keymap = _dedup_key_map(entries)
    activations = detect_activations(entries, session_id, resolver, keymap)
    write_activations(conn, session_id, activations)
    return sum(1 for a in activations if a.skill_path is not None)


def _collect_skill_dedup_key_map(conn: sqlite3.Connection, session_id: str) -> dict[str, str]:
    """Build ``uuid → dedup_key`` from the already-indexed ``messages`` row
    of ``session_id``. Used when we don't have the raw entries and only
    need linkage for writes."""
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT uuid, dedup_key FROM messages WHERE session_id=? AND uuid IS NOT NULL",
            (session_id,),
        ).fetchall()
    }


def populate_from_session_files(
    conn: sqlite3.Connection,
    session_id: str,
    resolver: SkillResolver,
) -> int:
    """Re-parse the session's files from disk and detect activations.

    Used by ``reconcile_projects_dir`` after each session's files are
    indexed. Attachments (channel C) only appear in the main file, so we
    only need to walk main + subagent files of this session. Returns
    activation-row count.
    """
    from .jsonl import parse_file  # local import to avoid cycle

    paths = [
        Path(row[0])
        for row in conn.execute(
            "SELECT path FROM files WHERE session_id=? ORDER BY kind", (session_id,)
        ).fetchall()
    ]
    keymap = _collect_skill_dedup_key_map(conn, session_id)
    all_activations: list[SkillActivation] = []
    for p in paths:
        try:
            entries = list(parse_file(p).entries)
        except (FileNotFoundError, OSError):
            logger.warning("skill detection: failed to read %s; skipping", p)
            continue
        all_activations.extend(detect_activations(entries, session_id, resolver, keymap))
    write_activations(conn, session_id, all_activations)
    return sum(1 for a in all_activations if a.skill_path is not None)


def build_resolver_from_paths() -> SkillResolver:
    """Build a ``SkillResolver`` using the default Claude Code install
    locations. Used by CLI callers that don't want to thread paths."""
    from .paths import claude_home, claude_plugins_cache_dir

    return SkillResolver(claude_home=claude_home(), plugins_cache=claude_plugins_cache_dir())
