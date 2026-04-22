from __future__ import annotations

from pathlib import Path

import pytest

from ccforensics.paths import (
    ccforensics_cache_dir,
    claude_home,
    claude_plugins_cache_dir,
    claude_projects_dir,
    claude_user_agents_dir,
    claude_user_skills_dir,
    decode_project_dirname,
    encode_project_path,
)


def test_claude_home_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert claude_home() == tmp_path / ".claude"


def test_claude_projects_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert claude_projects_dir() == tmp_path / ".claude" / "projects"


def test_claude_plugins_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert claude_plugins_cache_dir() == tmp_path / ".claude" / "plugins" / "cache"


def test_ccforensics_cache_dir_is_under_user_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    got = ccforensics_cache_dir()
    assert got.name == "ccforensics"
    assert tmp_path in got.parents or tmp_path == got.parent


def test_decode_simple_path() -> None:
    decoded = decode_project_dirname("-Users-jlixfeld-Documents-Development-projects-ccforensics")
    assert decoded == Path("/Users/jlixfeld/Documents/Development/projects/ccforensics")


def test_decode_single_dir() -> None:
    assert decode_project_dirname("-tmp") == Path("/tmp")


def test_decode_is_lossy_note() -> None:
    # Encoding is lossy: dashes in real paths become separators.
    # Decoder just reverses the naive transform; caller prefers cwd from JSONL.
    decoded = decode_project_dirname("-var-folders-ab-cd")
    assert decoded == Path("/var/folders/ab/cd")


def test_encode_project_path_round_trip() -> None:
    p = Path("/Users/jlixfeld/Documents")
    assert encode_project_path(p) == "-Users-jlixfeld-Documents"


def test_encode_rejects_relative_path() -> None:
    with pytest.raises(ValueError, match="absolute path required"):
        encode_project_path(Path("relative/path"))


def test_decode_rejects_name_without_leading_dash() -> None:
    with pytest.raises(ValueError, match="expected leading '-'"):
        decode_project_dirname("bad-name")


def test_claude_user_skills_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert claude_user_skills_dir() == tmp_path / ".claude" / "skills"


def test_claude_user_agents_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert claude_user_agents_dir() == tmp_path / ".claude" / "agents"


def test_claude_home_raises_when_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Path.home() raises RuntimeError if it can't resolve a home dir.
    # Simulate by clearing HOME and the win/posix fallbacks.
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr("os.path.expanduser", lambda p: p)
    with pytest.raises(RuntimeError):
        claude_home()
