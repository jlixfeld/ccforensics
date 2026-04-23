from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccforensics.index import ensure_schema, open_connection
from ccforensics.registry import (
    classify_agent_source,
    discover_plugins,
    discover_user_level,
    populate_registry,
)


def _write_plugin(
    cache_dir: Path,
    marketplace: str,
    name: str,
    version: str,
    *,
    agents: list[str] | None = None,
    skills: list[str] | None = None,
) -> Path:
    install = cache_dir / marketplace / name / version
    (install / ".claude-plugin").mkdir(parents=True)
    (install / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": version,
                "description": f"stub for {name}",
            }
        )
    )
    if agents:
        (install / "agents").mkdir()
        for a in agents:
            (install / "agents" / f"{a}.md").write_text(f"# {a}\n")
    if skills:
        (install / "skills").mkdir()
        for s in skills:
            (install / "skills" / s).mkdir()
            (install / "skills" / s / "SKILL.md").write_text(f"# {s}\n")
    return install


def _write_user_skill(claude_home: Path, name: str) -> None:
    d = claude_home / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"# {name}\n")


def _write_user_agent(claude_home: Path, name: str) -> None:
    d = claude_home / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"# {name}\n")


# ---------- discover_plugins ----------


def test_discover_single_plugin(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _write_plugin(
        cache,
        "jlixfeld-claude-skills",
        "pr-review-loop",
        "0.1.0",
        agents=["reviewer"],
        skills=["pr-review-loop"],
    )
    plugins = discover_plugins(cache)
    assert len(plugins) == 1
    p = plugins[0]
    assert p.name == "pr-review-loop"
    assert p.version == "0.1.0"
    assert p.agent_names == ["reviewer"]
    assert len(p.skill_paths) == 1
    assert p.skill_paths[0].parent.name == "pr-review-loop"


def test_discover_multiple_versions_keeps_highest(tmp_path: Path) -> None:
    """plugins PK is name → we keep one row per plugin, choosing the
    highest version when the cache contains several."""
    cache = tmp_path / "cache"
    _write_plugin(cache, "m", "auto-review", "0.1.2")
    _write_plugin(cache, "m", "auto-review", "0.1.4")
    _write_plugin(cache, "m", "auto-review", "0.1.3")
    plugins = discover_plugins(cache)
    assert len(plugins) == 1
    assert plugins[0].version == "0.1.4"


def test_discover_ignores_missing_cache_dir(tmp_path: Path) -> None:
    """No crash on machines with no plugins installed."""
    assert discover_plugins(tmp_path / "does-not-exist") == []


def test_discover_plugins_tolerates_malformed_manifest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed plugin.json → skipped with warning, doesn't fail the scan."""
    cache = tmp_path / "cache"
    _write_plugin(cache, "m", "good", "1.0.0")
    bad = cache / "m" / "bad" / "1.0.0" / ".claude-plugin"
    bad.mkdir(parents=True)
    (bad / "plugin.json").write_text("{not valid")

    caplog.set_level("WARNING", logger="ccforensics.registry")
    plugins = discover_plugins(cache)
    names = {p.name for p in plugins}
    assert names == {"good"}
    assert any("bad" in r.getMessage() for r in caplog.records)


def test_discover_plugin_without_name_field_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = tmp_path / "cache"
    install = cache / "m" / "nameless" / "1.0.0" / ".claude-plugin"
    install.mkdir(parents=True)
    (install / "plugin.json").write_text(json.dumps({"version": "1.0.0"}))
    caplog.set_level("WARNING", logger="ccforensics.registry")
    assert discover_plugins(cache) == []
    assert any("name" in r.getMessage() for r in caplog.records)


# ---------- discover_user_level ----------


def test_discover_user_level_skills_and_agents(tmp_path: Path) -> None:
    _write_user_skill(tmp_path, "napkin-notes")
    _write_user_agent(tmp_path, "custom-agent")
    artifacts = discover_user_level(tmp_path)
    kinds = sorted((a.kind, a.name) for a in artifacts)
    assert kinds == [("agent", "custom-agent"), ("skill", "napkin-notes")]


def test_discover_user_level_missing_dirs_returns_empty(tmp_path: Path) -> None:
    assert discover_user_level(tmp_path) == []


# ---------- populate_registry ----------


def test_populate_registry_writes_rows(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    home = tmp_path / "home"
    _write_plugin(cache, "m", "pr-review-loop", "0.1.0", agents=["reviewer"])
    _write_user_skill(home, "pr-quality")
    _write_user_agent(home, "custom-agent")

    conn = open_connection(tmp_path / "idx.sqlite")
    ensure_schema(conn)
    populate_registry(conn, cache, home)
    conn.commit()

    plugin_row = conn.execute(
        "SELECT name, version, scope FROM plugins"
    ).fetchone()
    assert plugin_row == ("pr-review-loop", "0.1.0", "user")

    ul_rows = sorted(
        conn.execute("SELECT kind, name FROM user_level_artifacts").fetchall()
    )
    assert ul_rows == [("agent", "custom-agent"), ("skill", "pr-quality")]


def test_populate_registry_is_idempotent(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    home = tmp_path / "home"
    _write_plugin(cache, "m", "x", "1.0.0")
    _write_user_skill(home, "ux")

    conn = open_connection(tmp_path / "idx.sqlite")
    ensure_schema(conn)
    populate_registry(conn, cache, home)
    conn.commit()
    first = conn.execute(
        "SELECT * FROM plugins ORDER BY name"
    ).fetchall() + conn.execute(
        "SELECT * FROM user_level_artifacts ORDER BY path"
    ).fetchall()

    populate_registry(conn, cache, home)
    conn.commit()
    second = conn.execute(
        "SELECT * FROM plugins ORDER BY name"
    ).fetchall() + conn.execute(
        "SELECT * FROM user_level_artifacts ORDER BY path"
    ).fetchall()
    assert first == second


# ---------- collision detection ----------


def test_skill_name_collision_is_warned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """User-level skill named X + any plugin skill named X → warning."""
    cache = tmp_path / "cache"
    home = tmp_path / "home"
    _write_plugin(
        cache,
        "jlixfeld-claude-skills",
        "pr-review-loop",
        "0.1.0",
        skills=["pr-review-loop"],
    )
    _write_user_skill(home, "pr-review-loop")

    caplog.set_level("WARNING", logger="ccforensics.registry")
    conn = open_connection(tmp_path / "idx.sqlite")
    ensure_schema(conn)
    report = populate_registry(conn, cache, home)

    assert report.skill_collisions
    collision_name, locs = report.skill_collisions[0]
    assert collision_name == "pr-review-loop"
    assert any("pr-review-loop" in loc for loc in locs)
    assert any("user-level" in loc for loc in locs)
    assert any("skill name collision" in r.getMessage() for r in caplog.records)


def test_no_collision_when_only_plugin_or_only_user(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    home = tmp_path / "home"
    _write_plugin(cache, "m", "p", "1.0.0", skills=["s"])
    # No user-level skill with same name.
    conn = open_connection(tmp_path / "idx.sqlite")
    ensure_schema(conn)
    report = populate_registry(conn, cache, home)
    assert report.skill_collisions == []


# ---------- classify_agent_source ----------


def test_classify_builtin() -> None:
    assert classify_agent_source("general-purpose", set(), set()) == "builtin"
    assert classify_agent_source("Explore", set(), set()) == "builtin"
    assert classify_agent_source("Plan", set(), set()) == "builtin"


def test_classify_plugin_prefixed() -> None:
    plugins = {"pr-review-toolkit", "auto-review"}
    assert (
        classify_agent_source("pr-review-toolkit:code-reviewer", plugins, set())
        == "pr-review-toolkit"
    )


def test_classify_plugin_prefix_not_found_falls_through() -> None:
    """Bucket name with ':' but no matching plugin — not plugin, so
    user-level or unknown."""
    plugins = {"known-plugin"}
    assert (
        classify_agent_source("stranger:agent", plugins, set()) == "unknown"
    )


def test_classify_user_level_agent() -> None:
    assert classify_agent_source("my-custom", set(), {"my-custom"}) == "user-level"


def test_classify_unknown_fallback() -> None:
    assert classify_agent_source("mystery-agent", set(), set()) == "unknown"
