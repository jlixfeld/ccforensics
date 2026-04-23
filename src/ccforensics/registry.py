"""Plugin + user-level registry; classify_agent_source resolves subagent ownership."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ArtifactKind = Literal["skill", "agent"]

logger = logging.getLogger("ccforensics.registry")

BUILTIN_AGENTS: frozenset[str] = frozenset(
    {"general-purpose", "Explore", "Plan", "statusline-setup"}
)

_VERSION_KEY_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def version_sort_key(v: str | None) -> tuple[int, ...]:
    """Sort key for version strings — prefers semver ordering, falls back
    to lexicographic for non-semver."""
    if not v:
        return (0,)
    m = _VERSION_KEY_RE.match(v)
    if m:
        return (1, int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (0,)


@dataclass
class DiscoveredPlugin:
    name: str
    version: str | None
    install_path: Path
    manifest: dict[str, Any]
    agent_names: list[str] = field(default_factory=list)
    skill_paths: list[Path] = field(default_factory=list)


@dataclass
class UserLevelArtifact:
    path: Path
    kind: ArtifactKind
    name: str


def _discover_one_plugin(manifest_path: Path) -> DiscoveredPlugin | None:
    """Parse ``<install_path>/.claude-plugin/plugin.json`` into a
    ``DiscoveredPlugin``. Returns ``None`` on unreadable/unparseable
    manifests (logged)."""
    install_path = manifest_path.parent.parent
    try:
        raw = manifest_path.read_text()
    except OSError:
        logger.warning("failed to read %s", manifest_path, exc_info=True)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("malformed plugin.json at %s", manifest_path, exc_info=True)
        return None
    if not isinstance(data, dict) or "name" not in data:
        logger.warning("plugin.json at %s missing required 'name' field; skipping", manifest_path)
        return None

    name = str(data["name"])
    version = data.get("version")
    version = str(version) if version is not None else None

    agent_names: list[str] = []
    agents_dir = install_path / "agents"
    if agents_dir.is_dir():
        agent_names = sorted(p.stem for p in agents_dir.glob("*.md"))

    skill_paths: list[Path] = []
    skills_dir = install_path / "skills"
    if skills_dir.is_dir():
        skill_paths = sorted(skills_dir.glob("*/SKILL.md"))

    return DiscoveredPlugin(
        name=name,
        version=version,
        install_path=install_path,
        manifest=data,
        agent_names=agent_names,
        skill_paths=skill_paths,
    )


def discover_plugins(plugins_cache_dir: Path) -> list[DiscoveredPlugin]:
    """Walk ``~/.claude/plugins/cache`` → ``<marketplace>/<plugin>/<ver>/.claude-plugin/plugin.json``.

    When multiple versions of the same plugin are present, keep the
    highest-versioned one (lexicographic on semver tuples).
    """
    if not plugins_cache_dir.is_dir():
        return []

    candidates: dict[str, list[DiscoveredPlugin]] = {}
    for manifest in plugins_cache_dir.glob("*/*/*/.claude-plugin/plugin.json"):
        p = _discover_one_plugin(manifest)
        if p is None:
            continue
        candidates.setdefault(p.name, []).append(p)

    out: list[DiscoveredPlugin] = []
    for versions in candidates.values():
        winner = max(versions, key=lambda p: version_sort_key(p.version))
        out.append(winner)
    out.sort(key=lambda p: p.name)
    return out


def discover_user_level(claude_home: Path) -> list[UserLevelArtifact]:
    """Scan ``~/.claude/skills/<name>/SKILL.md`` and
    ``~/.claude/agents/<name>.md``."""
    out: list[UserLevelArtifact] = []

    skills_dir = claude_home / "skills"
    if skills_dir.is_dir():
        for skill in sorted(skills_dir.glob("*/SKILL.md")):
            out.append(UserLevelArtifact(path=skill, kind="skill", name=skill.parent.name))

    agents_dir = claude_home / "agents"
    if agents_dir.is_dir():
        for agent in sorted(agents_dir.glob("*.md")):
            out.append(UserLevelArtifact(path=agent, kind="agent", name=agent.stem))

    return out


@dataclass
class CollisionReport:
    skill_collisions: list[tuple[str, list[str]]] = field(default_factory=list)
    agent_collisions: list[tuple[str, list[str]]] = field(default_factory=list)


def _detect_collisions(
    plugins: list[DiscoveredPlugin],
    user_level: list[UserLevelArtifact],
) -> CollisionReport:
    """Flag skill/agent names shared between user-level and any plugin."""
    plugin_skill_map: dict[str, list[str]] = {}
    plugin_agent_map: dict[str, list[str]] = {}
    for p in plugins:
        for skill_path in p.skill_paths:
            plugin_skill_map.setdefault(skill_path.parent.name, []).append(
                f"{p.name}:{skill_path.parent.name}"
            )
        for agent in p.agent_names:
            plugin_agent_map.setdefault(agent, []).append(f"{p.name}:{agent}")

    user_skills = {a.name for a in user_level if a.kind == "skill"}
    user_agents = {a.name for a in user_level if a.kind == "agent"}

    report = CollisionReport()
    for name, locations in plugin_skill_map.items():
        if name in user_skills:
            report.skill_collisions.append((name, [*locations, f"user-level:{name}"]))
    for name, locations in plugin_agent_map.items():
        if name in user_agents:
            report.agent_collisions.append((name, [*locations, f"user-level:{name}"]))
    return report


def populate_registry(
    conn: sqlite3.Connection,
    plugins_cache_dir: Path,
    claude_home: Path,
) -> CollisionReport:
    """Discover plugins + user-level artifacts and write them to the DB.
    Idempotent: deletes and re-inserts rows on each call."""
    plugins = discover_plugins(plugins_cache_dir)
    user_level = discover_user_level(claude_home)

    conn.execute("DELETE FROM plugins")
    for p in plugins:
        conn.execute(
            """INSERT INTO plugins (name, version, install_path, scope, manifest_json)
               VALUES (?,?,?,?,?)""",
            (
                p.name,
                p.version,
                str(p.install_path),
                "user",
                json.dumps(p.manifest, sort_keys=True),
            ),
        )

    conn.execute("DELETE FROM user_level_artifacts")
    for a in user_level:
        conn.execute(
            "INSERT OR REPLACE INTO user_level_artifacts (path, kind, name) VALUES (?,?,?)",
            (str(a.path), a.kind, a.name),
        )

    report = _detect_collisions(plugins, user_level)
    for name, locs in report.skill_collisions:
        logger.warning("skill name collision on '%s' across: %s", name, ", ".join(locs))
    for name, locs in report.agent_collisions:
        logger.warning("agent name collision on '%s' across: %s", name, ", ".join(locs))

    return report


def classify_agent_source(
    agent_type: str,
    plugin_names: set[str],
    user_level_agent_names: set[str],
) -> str:
    """Map a subagent_type string to its source.

    Returns one of: ``'builtin'``, the plugin name, ``'user-level'``,
    or ``'unknown'``.
    """
    if agent_type in BUILTIN_AGENTS:
        return "builtin"
    if ":" in agent_type:
        prefix = agent_type.split(":", 1)[0]
        if prefix in plugin_names:
            return prefix
    if agent_type in user_level_agent_names:
        return "user-level"
    return "unknown"


def load_plugin_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM plugins").fetchall()}


def load_user_level_agent_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM user_level_artifacts WHERE kind='agent'"
        ).fetchall()
    }
