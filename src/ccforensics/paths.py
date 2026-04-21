from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_cache_dir


def claude_home() -> Path:
    return Path(os.environ.get("HOME", "~")).expanduser() / ".claude"


def claude_projects_dir() -> Path:
    return claude_home() / "projects"


def claude_plugins_cache_dir() -> Path:
    return claude_home() / "plugins" / "cache"


def claude_user_skills_dir() -> Path:
    return claude_home() / "skills"


def claude_user_agents_dir() -> Path:
    return claude_home() / "agents"


def ccforensics_cache_dir() -> Path:
    d = Path(user_cache_dir("ccforensics"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def encode_project_path(p: Path) -> str:
    """Convert an absolute path to Claude Code's dir-name encoding."""
    s = str(p)
    if not s.startswith("/"):
        raise ValueError(f"absolute path required, got {p!r}")
    return "-" + s[1:].replace("/", "-")


def decode_project_dirname(name: str) -> Path:
    """Reverse of encode_project_path. LOSSY for paths containing dashes.
    Callers should prefer the cwd field from JSONL records when available.
    """
    if not name.startswith("-"):
        raise ValueError(f"expected leading '-', got {name!r}")
    return Path("/" + name[1:].replace("-", "/"))
