from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ccforensics.index import ensure_schema, open_connection, reconcile_projects_dir
from ccforensics.models import parse_entry
from ccforensics.skills import (
    SkillResolver,
    detect_activations,
    name_from_path,
    plugin_from_path,
    populate_from_session_files,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict[str, Any]:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def _write_plugin_skill(
    plugins_cache: Path, plugin: str, skill: str, content: str = "# skill\n"
) -> Path:
    install = plugins_cache / "marketplace" / plugin / "0.1.0"
    (install / ".claude-plugin").mkdir(parents=True)
    (install / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": plugin, "version": "0.1.0"})
    )
    skill_md = install / "skills" / skill / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text(content)
    return skill_md


def _write_user_skill(claude_home: Path, skill: str, content: str = "# user skill\n") -> Path:
    skill_md = claude_home / "skills" / skill / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text(content)
    return skill_md


def _resolver(tmp_path: Path) -> SkillResolver:
    return SkillResolver(
        claude_home=tmp_path / "home",
        plugins_cache=tmp_path / "home" / "plugins" / "cache",
    )


def _assistant_with_tool_use(
    uuid: str,
    ts: str,
    *,
    tool_name: str,
    tool_input: dict,
    msg_id: str = "m1",
    req_id: str = "r1",
    session_id: str = "sess-1",
) -> Any:
    return parse_entry(
        {
            "type": "assistant",
            "uuid": uuid,
            "sessionId": session_id,
            "timestamp": ts,
            "isSidechain": False,
            "isMeta": False,
            "requestId": req_id,
            "message": {
                "id": msg_id,
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"tu-{uuid}",
                        "name": tool_name,
                        "input": tool_input,
                    }
                ],
            },
        }
    )


def _attachment_entry(
    uuid: str,
    ts: str,
    *,
    additional_context: str,
    session_id: str = "sess-1",
) -> Any:
    stdout = json.dumps({"hookSpecificOutput": {"additionalContext": additional_context}})
    return parse_entry(
        {
            "type": "attachment",
            "uuid": uuid,
            "sessionId": session_id,
            "timestamp": ts,
            "isSidechain": False,
            "isMeta": False,
            "attachment": {
                "type": "hook_success",
                "hookEvent": "SessionStart",
                "stdout": stdout,
                "content": "",
            },
        }
    )


# ---------- plugin_from_path / name_from_path ----------


def test_plugin_from_path_plugin_layout() -> None:
    p = "/Users/x/.claude/plugins/cache/marketplace/my-plugin/1.0.0/skills/brainstorming/SKILL.md"
    assert plugin_from_path(p) == "my-plugin"


def test_plugin_from_path_user_level_returns_none() -> None:
    p = "/Users/x/.claude/skills/napkin-notes/SKILL.md"
    assert plugin_from_path(p) is None


def test_name_from_path_extracts_skill_dir() -> None:
    assert (
        name_from_path("/Users/x/.claude/plugins/cache/m/p/v/skills/brainstorming/SKILL.md")
        == "brainstorming"
    )
    assert name_from_path("/Users/x/.claude/skills/napkin-notes/SKILL.md") == "napkin-notes"


# ---------- SkillResolver ----------


def test_resolver_finds_plugin_skill(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_plugin_skill(home / "plugins" / "cache", "superpowers", "brainstorming")
    r = _resolver(tmp_path)
    path, plugin = r.resolve_name("superpowers:brainstorming")
    assert path is not None
    assert plugin == "superpowers"
    assert path.name == "SKILL.md"


def test_resolver_finds_user_skill(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_user_skill(home, "napkin-notes")
    r = _resolver(tmp_path)
    path, plugin = r.resolve_name("napkin-notes")
    assert path is not None
    assert plugin is None


def test_resolver_miss_returns_none(tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    r = _resolver(tmp_path)
    assert r.resolve_name("does-not-exist") == (None, None)
    assert r.resolve_name("ghost:nope") == (None, None)


def test_resolver_warns_on_broken_plugin_manifest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed plugin.json must log a warning, not disappear silently —
    otherwise a single broken plugin erases its skills from every report."""
    import logging as _lg

    plugins_cache = tmp_path / "home" / "plugins" / "cache"
    install = plugins_cache / "marketplace" / "broken-plugin" / "0.1.0"
    (install / ".claude-plugin").mkdir(parents=True)
    (install / ".claude-plugin" / "plugin.json").write_text("{not valid json")

    caplog.set_level(_lg.WARNING, logger="ccforensics.skills")
    _resolver(tmp_path)
    assert any("unreadable manifest" in r.getMessage() for r in caplog.records)


def test_resolver_picks_highest_version_when_multiple_installed(tmp_path: Path) -> None:
    """When the plugin cache has two versions of the same plugin, the resolver
    must register the higher-versioned one's skill path — deterministically,
    not whichever glob surfaces first."""
    plugins_cache = tmp_path / "home" / "plugins" / "cache"
    # 0.1.10 (higher via numeric compare) and 0.1.2 (lower).
    for version, content in [("0.1.10", "# newer\n"), ("0.1.2", "# older\n")]:
        install = plugins_cache / "market" / "same-plugin" / version
        (install / ".claude-plugin").mkdir(parents=True)
        (install / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "same-plugin", "version": version})
        )
        skill_md = install / "skills" / "only-skill" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text(content)

    r = _resolver(tmp_path)
    path, plugin = r.resolve_name("same-plugin:only-skill")
    assert plugin == "same-plugin"
    assert path is not None
    # Path must point at the 0.1.10 install, not 0.1.2.
    assert "/0.1.10/" in str(path)
    assert path.read_text() == "# newer\n"


def test_resolver_plugin_and_user_skills_coexist_with_same_name(tmp_path: Path) -> None:
    """A plugin X and a user-level skill both named 'common' must resolve
    independently — plugin lookup via ``X:common``, user via ``common``.
    Pin this contract; collision warnings live in the registry layer."""
    _write_plugin_skill(
        tmp_path / "home" / "plugins" / "cache",
        "plug",
        "common",
        "# plugin\n",
    )
    _write_user_skill(tmp_path / "home", "common", "# user\n")

    r = _resolver(tmp_path)
    plugin_path, plugin_name = r.resolve_name("plug:common")
    user_path, user_plugin = r.resolve_name("common")

    assert plugin_name == "plug"
    assert plugin_path is not None and plugin_path.read_text() == "# plugin\n"
    assert user_plugin is None
    assert user_path is not None and user_path.read_text() == "# user\n"


def test_resolver_warns_on_non_string_plugin_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """plugin.json with a non-string ``name`` must warn, not skip silently."""
    import logging as _lg

    plugins_cache = tmp_path / "home" / "plugins" / "cache"
    install = plugins_cache / "marketplace" / "weird-plugin" / "0.1.0"
    (install / ".claude-plugin").mkdir(parents=True)
    (install / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": 42}))

    caplog.set_level(_lg.WARNING, logger="ccforensics.skills")
    _resolver(tmp_path)
    assert any("non-string 'name'" in r.getMessage() for r in caplog.records)


# ---------- Channel A: Skill tool ----------


def test_channel_a_detects_skill_tool(tmp_path: Path) -> None:
    _write_plugin_skill(
        tmp_path / "home" / "plugins" / "cache",
        "superpowers",
        "brainstorming",
        "# brainstorming\n" + "x" * 100,
    )
    entry = _assistant_with_tool_use(
        "u1",
        "2026-04-22T10:00:00Z",
        tool_name="Skill",
        tool_input={"skill": "superpowers:brainstorming"},
    )
    acts = detect_activations([entry], "sess-1", _resolver(tmp_path))
    assert len(acts) == 1
    a = acts[0]
    assert a.source == "skill-tool"
    assert a.skill_name == "brainstorming"
    assert a.plugin_name == "superpowers"
    assert a.skill_path is not None
    assert a.content_size is not None and a.content_size > 0


def test_channel_a_unresolvable_name_preserves_activation(tmp_path: Path) -> None:
    """A Skill tool_use for a name we can't resolve on disk still
    produces an activation record (without a path); write_activations
    will skip-and-log it."""
    (tmp_path / "home").mkdir()
    entry = _assistant_with_tool_use(
        "u1",
        "2026-04-22T10:00:00Z",
        tool_name="Skill",
        tool_input={"skill": "unknown:skill"},
    )
    acts = detect_activations([entry], "sess-1", _resolver(tmp_path))
    assert len(acts) == 1
    assert acts[0].skill_path is None
    assert acts[0].skill_name == "skill"


# ---------- Channel B: Read of SKILL.md ----------


def test_channel_b_detects_read_of_skill_md(tmp_path: Path) -> None:
    skill_md = _write_plugin_skill(
        tmp_path / "home" / "plugins" / "cache",
        "superpowers",
        "debugging",
    )
    entry = _assistant_with_tool_use(
        "u1",
        "2026-04-22T10:00:00Z",
        tool_name="Read",
        tool_input={"file_path": str(skill_md)},
    )
    acts = detect_activations([entry], "sess-1", _resolver(tmp_path))
    assert len(acts) == 1
    assert acts[0].source == "read"
    assert acts[0].skill_path == str(skill_md)
    assert acts[0].plugin_name == "superpowers"


def test_channel_b_ignores_non_skill_reads(tmp_path: Path) -> None:
    entry = _assistant_with_tool_use(
        "u1",
        "2026-04-22T10:00:00Z",
        tool_name="Read",
        tool_input={"file_path": "/some/other/file.py"},
    )
    assert detect_activations([entry], "sess-1", _resolver(tmp_path)) == []


def test_channel_a_and_channel_b_both_fire_for_same_skill(tmp_path: Path) -> None:
    """Current behavior: a session that both invokes Skill(name='X') AND
    Reads the SKILL.md for X produces TWO activations — one per channel.
    Pin this contract so it can't silently change.
    """
    skill_md = _write_plugin_skill(
        tmp_path / "home" / "plugins" / "cache",
        "superpowers",
        "debugging",
    )
    a_entry = _assistant_with_tool_use(
        "u1",
        "2026-04-22T10:00:00Z",
        tool_name="Skill",
        tool_input={"skill": "superpowers:debugging"},
        msg_id="m1",
        req_id="r1",
    )
    b_entry = _assistant_with_tool_use(
        "u2",
        "2026-04-22T10:00:05Z",
        tool_name="Read",
        tool_input={"file_path": str(skill_md)},
        msg_id="m2",
        req_id="r2",
    )
    acts = detect_activations([a_entry, b_entry], "sess-1", _resolver(tmp_path))
    sources = sorted(a.source for a in acts)
    assert sources == ["read", "skill-tool"]
    assert all(a.skill_name == "debugging" for a in acts)


# ---------- Channel C: SessionStart hook injection ----------


def test_channel_c_detects_hook_injection_with_frontmatter(tmp_path: Path) -> None:
    _write_plugin_skill(
        tmp_path / "home" / "plugins" / "cache",
        "superpowers",
        "using-superpowers",
    )
    additional = (
        "<EXTREMELY_IMPORTANT>\n"
        "---\n"
        "name: using-superpowers\n"
        "description: intro skill\n"
        "---\n"
        "content follows..."
    )
    entry = _attachment_entry("att-1", "2026-04-22T10:00:00Z", additional_context=additional)
    acts = detect_activations([entry], "sess-1", _resolver(tmp_path))
    assert len(acts) == 1
    assert acts[0].source == "hook-injection"
    assert acts[0].skill_name == "using-superpowers"
    assert acts[0].plugin_name == "superpowers"
    # content_size = len(additionalContext), not the on-disk SKILL.md size
    assert acts[0].content_size == len(additional)


def test_channel_c_fallback_bootstrap_heuristic(tmp_path: Path) -> None:
    """Even without parseable frontmatter, a SessionStart payload that
    mentions ``using-superpowers`` should be detected as the bootstrap
    skill."""
    _write_plugin_skill(
        tmp_path / "home" / "plugins" / "cache",
        "superpowers",
        "using-superpowers",
    )
    additional = (
        "<session-start-hook>\nYou have superpowers. Using the 'using-superpowers' skill..."
    )
    entry = _attachment_entry("att-1", "2026-04-22T10:00:00Z", additional_context=additional)
    acts = detect_activations([entry], "sess-1", _resolver(tmp_path))
    assert len(acts) == 1
    assert acts[0].skill_name == "using-superpowers"


def test_channel_c_malformed_stdout_is_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging as _lg

    entry = parse_entry(
        {
            "type": "attachment",
            "uuid": "att-1",
            "sessionId": "sess-1",
            "timestamp": "2026-04-22T10:00:00Z",
            "isSidechain": False,
            "isMeta": False,
            "attachment": {
                "type": "hook_success",
                "hookEvent": "SessionStart",
                "stdout": "not-valid-json",
                "content": "",
            },
        }
    )
    caplog.set_level(_lg.WARNING, logger="ccforensics.skills")
    assert detect_activations([entry], "sess-1", _resolver(tmp_path)) == []
    # Malformed payload must not disappear silently — a hook-format drift is
    # exactly the condition a forensics tool should flag.
    assert any(
        "not valid JSON" in r.getMessage() and "sess-1" in r.getMessage()
        for r in caplog.records
    )


def test_channel_c_ignores_other_hook_events(tmp_path: Path) -> None:
    entry = parse_entry(
        {
            "type": "attachment",
            "uuid": "att-1",
            "sessionId": "sess-1",
            "timestamp": "2026-04-22T10:00:00Z",
            "isSidechain": False,
            "isMeta": False,
            "attachment": {
                "type": "hook_success",
                "hookEvent": "PostToolUse",  # not SessionStart
                "stdout": '{"hookSpecificOutput":{"additionalContext":"irrelevant"}}',
                "content": "",
            },
        }
    )
    assert detect_activations([entry], "sess-1", _resolver(tmp_path)) == []


# ---------- dedup-key linkage ----------


def test_activation_links_dedup_key(tmp_path: Path) -> None:
    _write_plugin_skill(
        tmp_path / "home" / "plugins" / "cache",
        "superpowers",
        "brainstorming",
    )
    entry = _assistant_with_tool_use(
        "u1",
        "2026-04-22T10:00:00Z",
        tool_name="Skill",
        tool_input={"skill": "superpowers:brainstorming"},
    )
    keymap = {"u1": "req:m1:r1"}
    acts = detect_activations([entry], "sess-1", _resolver(tmp_path), keymap)
    assert acts[0].activated_by_dedup_key == "req:m1:r1"


# ---------- DB integration via reconcile ----------


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e))
            f.write("\n")


def test_populate_from_session_files_writes_rows(
    tmp_path: Path, pricing_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: synthetic session with a Skill tool_use; reconcile
    populates skill_activations."""
    # Fake Claude home / plugins paths so the resolver finds our
    # synthesized SKILL.md.
    fake_home = tmp_path / "fake-claude"
    _write_plugin_skill(fake_home / "plugins" / "cache", "superpowers", "brainstorming")

    import ccforensics.paths as paths_mod

    monkeypatch.setattr(paths_mod, "claude_home", lambda: fake_home)
    monkeypatch.setattr(
        paths_mod,
        "claude_plugins_cache_dir",
        lambda: fake_home / "plugins" / "cache",
    )

    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-skill"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": sid,
                "timestamp": "2026-04-22T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "cwd": "/home/test",
                "message": {"role": "user", "content": "hi"},
            },
            {
                "type": "assistant",
                "uuid": "u2",
                "sessionId": sid,
                "timestamp": "2026-04-22T10:00:10Z",
                "isSidechain": False,
                "isMeta": False,
                "requestId": "r1",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5-20250929",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu",
                            "name": "Skill",
                            "input": {"skill": "superpowers:brainstorming"},
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ],
    )

    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)

    rows = conn.execute(
        """SELECT skill_name, plugin_name, source FROM skill_activations
             WHERE session_id=?""",
        (sid,),
    ).fetchall()
    assert rows == [("brainstorming", "superpowers", "skill-tool")]


def test_populate_is_idempotent_across_reconciles(
    tmp_path: Path, pricing_data: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "fake-claude"
    _write_plugin_skill(fake_home / "plugins" / "cache", "superpowers", "brainstorming")
    import ccforensics.paths as paths_mod

    monkeypatch.setattr(paths_mod, "claude_home", lambda: fake_home)
    monkeypatch.setattr(
        paths_mod,
        "claude_plugins_cache_dir",
        lambda: fake_home / "plugins" / "cache",
    )

    proj = tmp_path / "projects"
    enc = proj / "-home-test"
    sid = "sess-skill2"
    _write_jsonl(
        enc / f"{sid}.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "u1",
                "sessionId": sid,
                "timestamp": "2026-04-22T10:00:00Z",
                "isSidechain": False,
                "isMeta": False,
                "requestId": "r1",
                "cwd": "/home/test",
                "message": {
                    "id": "m1",
                    "role": "assistant",
                    "model": "claude-sonnet-4-5-20250929",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu",
                            "name": "Skill",
                            "input": {"skill": "superpowers:brainstorming"},
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        ],
    )
    db = tmp_path / "idx.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    reconcile_projects_dir(conn, proj, pricing_data)
    first = conn.execute("SELECT * FROM skill_activations WHERE session_id=?", (sid,)).fetchall()
    assert len(first) == 1

    # Standalone helper call must not duplicate.
    from ccforensics.skills import build_resolver_from_paths

    populate_from_session_files(conn, sid, build_resolver_from_paths())
    conn.commit()
    second = conn.execute("SELECT * FROM skill_activations WHERE session_id=?", (sid,)).fetchall()
    # Source names and counts should match (ignoring auto-increment id).
    assert [r[1:] for r in first] == [r[1:] for r in second]
