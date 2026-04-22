# ccforensics Initial Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Date:** 2026-04-21
**Goal:** Build a Python CLI (`ccforensics`) that parses Claude Code session JSONL files and attributes token cost to plugins, skills, subagent types, with per-session and aggregate reports.

**Architecture:** SQLite-indexed (`~/.cache/ccforensics/index.sqlite`) for fast re-queries, reconciled by `(path, mtime, size)`. Defensive versioned JSONL parser. LiteLLM pricing fetch with offline fallback. Cost attributed by tree reconstruction (`sourceToolUseID`/`parentToolUseId` + subagent-dir meta.json). Three-channel skill-activation detection (`Skill` tool, Read of SKILL.md, SessionStart hook injection). Context-carry cost estimated per skill with explicit ± band.

**Tech Stack:** Python 3.11+, `uv`, `click`, `rich`, `httpx`, `pydantic`, `platformdirs`, `pytest`, `ruff`, `mypy`.

**Spec:** [`docs/specs/design.md`](../specs/design.md)
**Problem statement:** [`docs/specs/problem-statement.md`](../specs/problem-statement.md)

---

## Plan Decomposition

Milestones M0–M10 map to the spec §7.1 sequencing:

| Milestone | Ends with demoable state | Status in this plan |
|---|---|---|
| M0 | Repo skeleton, CI green, entrypoint prints `--help` | ✅ Done |
| M1 | Parser + dedup ingesting real JSONL without crash | ✅ Done |
| M2 | Cost totals match ccusage within ±1% | ✅ Done |
| M3 | `index --rebuild` + `index --stats` working; idempotent | ✅ Done |
| M4 | `session --list` on real corpus; SC5 verified | ✅ Done (SC5 PASS — 3s vs 30s target) |
| M4.6 | Post-M4 polish — sanitization + grep help | Full task detail (pre-M5) |
| M5 | Tree reconstruction; per-bucket breakdown | Outline only — expand when M4.6 complete |
| M6 | Plugin registry + rollup; collision warning | Outline only |
| M7 | `session <id>` deep report; SC1 verified | Outline only |
| M8 | Skill ledger + ± band; SC2 + SC3 verified | Outline only |
| M9 | `aggregate` + `plugins` commands | Outline only |
| M10 | Polish, README, v0.1.0 tag; SC4 verified | Outline only |

**Why the split:** M4–M10 build on M0–M3's concrete code. Writing their task steps in full before that code exists produces either fabrications or placeholders. When we hit the end of M3, plan expansion for M4 is a 30-minute task informed by what actually landed. Same pattern for M5 onward: expand each when the prior milestone lands.

**Unplanned mid-M4 work that already landed:**
- **M4.4.5 — parser fix (`fix(models): normalize string-shaped message.content at parse time`, commit `fba40fd`).** Pre-existing bug from M1.2: `Message.content` was typed `list[ContentBlock]` but real Claude Code JSONL emits user text prompts as a bare `str`. Pydantic rejected those entries, silently dropping ~20% of real corpus messages and leaving 12307 sessions with `summary_source='none'` vs 62 with `'first-prompt'`. Fix normalizes at parse time. Post-fix: 11285 first-prompt, 1013 claude-summary, 72 none. SC5 was blocked on this and unblocked by the fix.

---

## File Structure (from spec §1)

Created in M0–M3:

- `pyproject.toml` — uv-managed, entrypoint `ccforensics = ccforensics.cli:main`.
- `src/ccforensics/__init__.py` — `__version__`.
- `src/ccforensics/cli.py` — click command tree. M0 stub; populated M3+.
- `src/ccforensics/paths.py` — `~/.claude` resolution, cwd-encoding/decoding, cache dir.
- `src/ccforensics/models.py` — pydantic types for JSONL records.
- `src/ccforensics/jsonl.py` — streaming parser + dedup.
- `src/ccforensics/pricing.py` — LiteLLM fetcher + disk cache + fuzzy resolver.
- `src/ccforensics/index.py` — SQLite schema, migrations, reconciliation.
- `tests/fixtures/` — synthetic JSONL corpora + frozen LiteLLM snapshot.
- `tests/test_*.py` — one per module.
- `.github/workflows/ci.yml` — ruff + mypy + pytest matrix.

Created later (M4+): `attribution.py`, `tree.py`, `registry.py`, `skills.py`, `report/*.py`, `export.py`.

---

## Milestone M0 — Repo Skeleton

**Exit:** `uv run ccforensics --help` prints the command tree. CI green on an empty project. Tracking issue open. Feature branch exists.

### Task M0.1: Initialize uv project

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `.gitignore`
- Create: `README.md` (stub)

- [ ] **Step 1: Run `uv init --package --lib` in the repo root**

```bash
cd /Users/jlixfeld/Documents/Development/projects/ccforensics
uv init --package --lib --name ccforensics
```

Expected: creates `pyproject.toml`, `src/ccforensics/__init__.py`, `README.md`.

- [ ] **Step 2: Replace `pyproject.toml` with the committed version**

```toml
[project]
name = "ccforensics"
version = "0.1.0"
description = "Claude Code session forensics — plugin, skill, and subagent cost attribution"
readme = "README.md"
requires-python = ">=3.11"
authors = [{ name = "Jason Lixfeld", email = "jason@lixfeld.ca" }]
license = { text = "MIT" }
dependencies = [
    "click>=8.1",
    "rich>=13.7",
    "httpx>=0.27",
    "pydantic>=2.6",
    "platformdirs>=4.2",
]

[project.scripts]
ccforensics = "ccforensics.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-cov>=5.0",
    "syrupy>=4.6",
    "ruff>=0.5",
    "mypy>=1.10",
]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "RUF"]
ignore = ["E501"]  # line length handled by formatter

[tool.mypy]
python_version = "3.11"
strict = true
warn_return_any = true
warn_unused_ignores = true

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"

[tool.coverage.run]
source = ["src/ccforensics"]
branch = true

[tool.coverage.report]
exclude_lines = ["pragma: no cover", "raise NotImplementedError"]
```

- [ ] **Step 3: Write `.python-version` and `.gitignore`**

`.python-version`:
```
3.11
```

`.gitignore`:
```
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
dist/
*.egg-info/
.coverage
.coverage.*
coverage.xml
htmlcov/
.pytest_cache/
.ruff_cache/
.mypy_cache/

# Virtual envs
.venv/
venv/

# uv
.uv/

# macOS
.DS_Store

# ccforensics cache (would never be checked in)
*.sqlite
*.sqlite-journal

# IDE
.vscode/
.idea/
```

- [ ] **Step 4: Run `uv sync` to install**

```bash
uv sync
```

Expected: creates `.venv/`, `uv.lock`, installs pinned deps.

- [ ] **Step 5: Commit**

```bash
git init
git add pyproject.toml .python-version .gitignore README.md uv.lock src/
git commit -m "chore: scaffold ccforensics package with uv"
```

---

### Task M0.2: CLI entrypoint stub

**Files:**
- Modify: `src/ccforensics/__init__.py`
- Create: `src/ccforensics/cli.py`
- Create: `tests/__init__.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI smoke test**

`tests/test_cli.py`:
```python
from click.testing import CliRunner

from ccforensics.cli import main


def test_help_prints_command_tree() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "ccforensics" in result.output.lower()
    assert "session" in result.output
    assert "aggregate" in result.output
    assert "plugins" in result.output
    assert "index" in result.output


def test_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output
```

- [ ] **Step 2: Run test — expect failure (module missing)**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: `ModuleNotFoundError: No module named 'ccforensics.cli'` or similar.

- [ ] **Step 3: Write `cli.py` with stubbed command tree**

`src/ccforensics/__init__.py`:
```python
__version__ = "0.1.0"
```

`src/ccforensics/cli.py`:
```python
from __future__ import annotations

import click

from . import __version__


@click.group()
@click.version_option(__version__, prog_name="ccforensics")
@click.option("-v", "--verbose", is_flag=True, help="Print per-session warnings.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """ccforensics — Claude Code session cost attribution."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose


@main.group()
def session() -> None:
    """Per-session listing and deep reports."""


@session.command("list")
def session_list() -> None:
    """List all discoverable sessions (not yet implemented)."""
    click.echo("session --list: not yet implemented (milestone M4)")


@main.command()
def aggregate() -> None:
    """Aggregate cost across a date range (not yet implemented)."""
    click.echo("aggregate: not yet implemented (milestone M9)")


@main.command()
def plugins() -> None:
    """Per-plugin all-time rollup (not yet implemented)."""
    click.echo("plugins: not yet implemented (milestone M9)")


@main.group()
def index() -> None:
    """SQLite index management."""


@index.command("stats")
def index_stats() -> None:
    """Show index stats (not yet implemented)."""
    click.echo("index --stats: not yet implemented (milestone M3)")


@index.command("rebuild")
def index_rebuild() -> None:
    """Drop and rebuild the index (not yet implemented)."""
    click.echo("index --rebuild: not yet implemented (milestone M3)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test — expect pass**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: both tests pass.

- [ ] **Step 5: Manual smoke — entrypoint installed**

```bash
uv run ccforensics --help
uv run ccforensics --version
```

Expected: help shows the `session`, `aggregate`, `plugins`, `index` subcommands; version prints `0.1.0`.

- [ ] **Step 6: Commit**

```bash
git add src/ccforensics/__init__.py src/ccforensics/cli.py tests/__init__.py tests/test_cli.py
git commit -m "feat(cli): entrypoint with stubbed command tree"
```

---

### Task M0.3: Paths module

**Files:**
- Create: `src/ccforensics/paths.py`
- Create: `tests/test_paths.py`

- [ ] **Step 1: Write failing test for path helpers**

`tests/test_paths.py`:
```python
from __future__ import annotations

from pathlib import Path

import pytest

from ccforensics.paths import (
    claude_home,
    claude_projects_dir,
    claude_plugins_cache_dir,
    ccforensics_cache_dir,
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
```

- [ ] **Step 2: Run test — expect failure**

```bash
uv run pytest tests/test_paths.py -v
```

Expected: import error.

- [ ] **Step 3: Implement paths module**

`src/ccforensics/paths.py`:
```python
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
```

- [ ] **Step 4: Run test — expect pass**

```bash
uv run pytest tests/test_paths.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/paths.py tests/test_paths.py
git commit -m "feat(paths): ~/.claude resolution and cwd encoding helpers"
```

---

### Task M0.4: CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write CI config**

`.github/workflows/ci.yml`:
```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest]
        python: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true

      - name: Set up Python
        run: uv python install ${{ matrix.python }}

      - name: Sync dependencies
        run: uv sync --all-extras --dev

      - name: Ruff check
        run: uv run ruff check

      - name: Ruff format check
        run: uv run ruff format --check

      - name: Mypy
        run: uv run mypy src/

      - name: Pytest with coverage
        run: uv run pytest --cov=ccforensics --cov-report=term-missing --cov-fail-under=85
```

- [ ] **Step 2: Run ruff + mypy locally to confirm clean**

```bash
uv run ruff check
uv run ruff format --check
uv run mypy src/
```

All three should exit 0. If format check fails, run `uv run ruff format` and recommit.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add lint + typecheck + test workflow"
```

---

### Task M0.5: GitHub repo + tracking issue + branch

**Files:** none (external actions)

- [ ] **Step 1: Create the GitHub repo**

```bash
gh repo create jlixfeld/ccforensics --public --source=. --remote=origin --description "Claude Code session forensics — plugin, skill, and subagent cost attribution"
git push -u origin main
```

Expected: repo created; main branch pushed; CI should begin running.

- [ ] **Step 2: Create tracking issue referencing the design doc**

```bash
gh issue create \
  --title "Initial implementation of ccforensics (v0.1.0)" \
  --body "Implementation plan: [docs/plans/2026-04-21-initial-implementation.md](./docs/plans/2026-04-21-initial-implementation.md)

Design spec: [docs/specs/design.md](./docs/specs/design.md)

Success criteria: SC1–SC6 per [docs/specs/problem-statement.md](./docs/specs/problem-statement.md).

Milestones: M0 (skeleton) → M1 (parser) → M2 (pricing) → M3 (index) → M4 (session --list) → M5 (tree) → M6 (plugins) → M7 (deep report) → M8 (skills) → M9 (aggregate + plugins cmds) → M10 (polish + v0.1.0)."
```

Note the issue number returned.

- [ ] **Step 3: Create feature branch**

```bash
git checkout -b feature/initial-implementation
git push -u origin feature/initial-implementation
```

- [ ] **Step 4: Verify CI ran green on main**

```bash
gh run list --limit 3
```

If the most recent run is failing, diagnose — common causes: `uv sync` without `--dev`, coverage below threshold on first empty pass (tests from M0.2/M0.3 should cover this).

**M0 exit criteria — verify all of these:**
- [ ] `uv run ccforensics --help` lists session/aggregate/plugins/index.
- [ ] `uv run pytest` is green (all 9 tests from M0.2 + M0.3).
- [ ] `uv run ruff check && uv run mypy src/` clean.
- [ ] CI green on main.
- [ ] Feature branch pushed.

---

## Milestone M1 — Parser + Deduplication

**Exit:** `jsonl.py` can stream-parse real JSONL files from `~/.claude/projects/` without crashing. Dedup produces deterministic output under input reorder. No cost annotation yet.

### Task M1.1: Synthetic fixture — basic session

**Files:**
- Create: `tests/fixtures/basic/session.jsonl`

- [ ] **Step 1: Write a minimal synthetic session fixture**

`tests/fixtures/basic/session.jsonl`:
```jsonl
{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z","cwd":"/home/test","isSidechain":false,"message":{"role":"user","content":[{"type":"text","text":"Hello"}]}}
{"type":"assistant","uuid":"u2","parentUuid":"u1","sessionId":"s1","timestamp":"2026-04-20T10:00:05Z","requestId":"req-a","message":{"id":"msg-1","role":"assistant","model":"claude-sonnet-4-5-20250929","content":[{"type":"text","text":"Hi"}],"usage":{"input_tokens":100,"output_tokens":5,"cache_read_input_tokens":500,"cache_creation_input_tokens":0}}}
{"type":"user","uuid":"u3","parentUuid":"u2","sessionId":"s1","timestamp":"2026-04-20T10:01:00Z","message":{"role":"user","content":[{"type":"text","text":"More"}]}}
{"type":"assistant","uuid":"u4","parentUuid":"u3","sessionId":"s1","timestamp":"2026-04-20T10:01:05Z","requestId":"req-b","message":{"id":"msg-2","role":"assistant","model":"claude-sonnet-4-5-20250929","content":[{"type":"tool_use","id":"toolu-1","name":"Read","input":{"file_path":"/foo"}}],"usage":{"input_tokens":150,"output_tokens":30,"cache_read_input_tokens":600,"cache_creation_input_tokens":100}}}
{"type":"user","uuid":"u5","parentUuid":"u4","sourceToolUseID":"toolu-1","sessionId":"s1","timestamp":"2026-04-20T10:01:06Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu-1","content":"file contents"}]}}
```

- [ ] **Step 2: Commit fixture (no code yet)**

```bash
git add tests/fixtures/basic/session.jsonl
git commit -m "test(fixtures): minimal synthetic session"
```

---

### Task M1.2: Pydantic model for transcript entries

**Files:**
- Create: `src/ccforensics/models.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: Write failing tests for the pydantic models**

`tests/test_models.py`:
```python
from __future__ import annotations

import json

from ccforensics.models import TranscriptEntry, parse_entry


def test_parses_user_entry() -> None:
    raw = json.loads('{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z","isSidechain":false,"message":{"role":"user","content":[{"type":"text","text":"Hello"}]}}')
    entry = parse_entry(raw)
    assert entry.type == "user"
    assert entry.uuid == "u1"
    assert entry.session_id == "s1"
    assert entry.is_sidechain is False


def test_parses_assistant_with_usage() -> None:
    raw = json.loads('{"type":"assistant","uuid":"u2","sessionId":"s1","timestamp":"2026-04-20T10:00:05Z","requestId":"req-a","message":{"id":"msg-1","role":"assistant","model":"claude-sonnet-4-5-20250929","content":[],"usage":{"input_tokens":100,"output_tokens":5,"cache_read_input_tokens":500,"cache_creation_input_tokens":0}}}')
    entry = parse_entry(raw)
    assert entry.type == "assistant"
    assert entry.request_id == "req-a"
    assert entry.message is not None
    assert entry.message.id == "msg-1"
    assert entry.message.model == "claude-sonnet-4-5-20250929"
    assert entry.message.usage is not None
    assert entry.message.usage.input_tokens == 100
    assert entry.message.usage.output_tokens == 5
    assert entry.message.usage.cache_read_input_tokens == 500
    assert entry.message.usage.cache_creation_input_tokens == 0


def test_field_normalization_legacy_parent_tool_use_id() -> None:
    # Older JSONL uses parentToolUseId; newer uses sourceToolUseID.
    raw_old = json.loads('{"type":"user","parentToolUseId":"toolu-1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z","message":null}')
    raw_new = json.loads('{"type":"user","sourceToolUseID":"toolu-1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z","message":null}')
    entry_old = parse_entry(raw_old)
    entry_new = parse_entry(raw_new)
    assert entry_old.source_tool_use_id == "toolu-1"
    assert entry_new.source_tool_use_id == "toolu-1"


def test_unknown_type_preserved() -> None:
    raw = json.loads('{"type":"permission-mode","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z","permissionMode":"bypassPermissions"}')
    entry = parse_entry(raw)
    assert entry.type == "permission-mode"
    # Extras preserved in __pydantic_extra__
    assert entry.extras.get("permissionMode") == "bypassPermissions"


def test_tolerates_missing_optional_fields() -> None:
    raw = json.loads('{"type":"user","timestamp":"2026-04-20T10:00:00Z"}')
    entry = parse_entry(raw)
    assert entry.type == "user"
    assert entry.uuid is None
    assert entry.session_id is None


def test_tool_use_content_block_parsed() -> None:
    raw = json.loads('{"type":"assistant","uuid":"u","timestamp":"2026-04-20T10:00:00Z","requestId":"r","message":{"id":"m","role":"assistant","model":"claude-sonnet-4-5","content":[{"type":"tool_use","id":"toolu-99","name":"Read","input":{"file_path":"/x"}}]}}')
    entry = parse_entry(raw)
    assert entry.message is not None
    blocks = entry.message.content
    assert len(blocks) == 1
    assert blocks[0].type == "tool_use"
    assert blocks[0].id == "toolu-99"
    assert blocks[0].name == "Read"


def test_attachment_hook_success_recognized() -> None:
    raw = json.loads('{"type":"attachment","timestamp":"2026-04-20T10:00:00Z","attachment":{"type":"hook_success","hookEvent":"SessionStart","stdout":"{\\"hookSpecificOutput\\":{}}","content":""}}')
    entry = parse_entry(raw)
    assert entry.type == "attachment"
    assert entry.attachment is not None
    assert entry.attachment.type == "hook_success"
    assert entry.attachment.hook_event == "SessionStart"
```

- [ ] **Step 2: Run tests — expect failure**

```bash
uv run pytest tests/test_models.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement `models.py`**

`src/ccforensics/models.py`:
```python
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class UsageStats(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


class ContentBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    # tool_use
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    # tool_result
    tool_use_id: str | None = None
    content: Any = None
    # text
    text: str | None = None


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    role: str | None = None
    model: str | None = None
    content: list[ContentBlock] = Field(default_factory=list)
    usage: UsageStats | None = None


class Attachment(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str | None = None
    hook_event: str | None = Field(default=None, alias="hookEvent")
    hook_name: str | None = Field(default=None, alias="hookName")
    tool_use_id: str | None = Field(default=None, alias="toolUseID")
    content: str | None = None
    stdout: str | None = None


class TranscriptEntry(BaseModel):
    """Permissive, versioned container for a single JSONL line.

    Required: type, timestamp. Everything else optional. Unknown top-level fields
    preserved in `.extras`. Normalizes legacy field names at parse time.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str
    timestamp: datetime
    uuid: str | None = None
    parent_uuid: str | None = Field(default=None, alias="parentUuid")
    session_id: str | None = Field(default=None, alias="sessionId")
    request_id: str | None = Field(default=None, alias="requestId")
    source_tool_use_id: str | None = Field(
        default=None,
        validation_alias="sourceToolUseID",
    )
    source_tool_assistant_uuid: str | None = Field(
        default=None,
        validation_alias="sourceToolAssistantUUID",
    )
    agent_id: str | None = Field(default=None, alias="agentId")
    cwd: str | None = None
    version: str | None = None
    is_sidechain: bool = Field(default=False, alias="isSidechain")
    is_meta: bool = Field(default=False, alias="isMeta")
    is_compact_summary: bool = Field(default=False, alias="isCompactSummary")
    slug: str | None = None
    leaf_uuid: str | None = Field(default=None, alias="leafUuid")
    summary: str | None = None
    user_type: str | None = Field(default=None, alias="userType")
    message: Message | None = None
    attachment: Attachment | None = None
    tool_use_result: Any = Field(default=None, alias="toolUseResult")

    @property
    def extras(self) -> dict[str, Any]:
        return self.__pydantic_extra__ or {}


KNOWN_TYPES: frozenset[str] = frozenset({
    "user", "assistant", "system", "attachment", "summary",
    "file-history-snapshot", "permission-mode", "last-prompt",
    "queue-operation", "tool_use", "tool_result",
})


def parse_entry(raw: dict[str, Any]) -> TranscriptEntry:
    """Normalize legacy field names, then pydantic-parse. Never raises on
    unknown types or extra fields."""
    # Legacy field rename — pydantic's validation_alias handles newer name,
    # but we also accept the legacy name for older transcripts.
    if "parentToolUseId" in raw and "sourceToolUseID" not in raw:
        raw["sourceToolUseID"] = raw.pop("parentToolUseId")
    if "parentToolAssistantUuid" in raw and "sourceToolAssistantUUID" not in raw:
        raw["sourceToolAssistantUUID"] = raw.pop("parentToolAssistantUuid")
    return TranscriptEntry.model_validate(raw)
```

- [ ] **Step 4: Run tests — expect pass**

```bash
uv run pytest tests/test_models.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/models.py tests/test_models.py
git commit -m "feat(models): pydantic TranscriptEntry with legacy-field normalization"
```

---

### Task M1.3: JSONL streaming parser

**Files:**
- Create: `src/ccforensics/jsonl.py`
- Create: `tests/test_jsonl_parse.py`
- Create: `tests/fixtures/truncated/session.jsonl`
- Create: `tests/fixtures/drift/session.jsonl`

- [ ] **Step 1: Write truncated + drift fixtures**

`tests/fixtures/truncated/session.jsonl` — last line intentionally incomplete:
```jsonl
{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z","message":null}
{"type":"assistant","uuid":"u2","sessionId":"s1","timestamp":"2026-04-20T10:00:05Z","requestId":"req-a","message":{"id":"m","role":"assistant","model":"claude-sonnet-4-5-20250929","content":[],"usage":{"input_tokens":10,"output_tokens":1}}}
{"type":"user","uuid":"u3","sessionId":"s1","timestamp":"2026-04-20T10:01
```

`tests/fixtures/drift/session.jsonl`:
```jsonl
{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-20T10:00:00Z","message":null}
{"type":"permission-mode","timestamp":"2026-04-20T10:00:00Z","permissionMode":"bypassPermissions","sessionId":"s1"}
{"type":"future-unknown-type","timestamp":"2026-04-20T10:00:01Z","sessionId":"s1","someFutureField":42}
{"type":"assistant","uuid":"u2","sessionId":"s1","timestamp":"2026-04-20T10:00:05Z","requestId":"req-a","message":{"id":"m","role":"assistant","model":"claude-sonnet-4-5-20250929","content":[],"usage":{"input_tokens":10,"output_tokens":1}}}
```

- [ ] **Step 2: Write failing test for streaming parser**

`tests/test_jsonl_parse.py`:
```python
from __future__ import annotations

from pathlib import Path

from ccforensics.jsonl import ParseResult, parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_basic_session() -> None:
    result = parse_file(FIXTURES / "basic" / "session.jsonl")
    assert len(result.entries) == 5
    assert result.warnings == []
    assert result.parse_errors == 0
    assert result.unknown_types == set()


def test_truncated_last_line_is_silently_skipped() -> None:
    result = parse_file(FIXTURES / "truncated" / "session.jsonl")
    # 3 valid entries; the truncated last line is dropped without error.
    assert len(result.entries) == 2
    assert result.parse_errors == 0  # last-line truncation is not counted as error
    assert result.truncated_tail is True


def test_unknown_type_recorded_not_crashed() -> None:
    result = parse_file(FIXTURES / "drift" / "session.jsonl")
    assert len(result.entries) == 4
    assert "future-unknown-type" in result.unknown_types
    # permission-mode IS in KNOWN_TYPES, so shouldn't appear in unknown_types
    assert "permission-mode" not in result.unknown_types
    assert len(result.warnings) >= 1


def test_missing_file_raises(tmp_path: Path) -> None:
    import pytest
    with pytest.raises(FileNotFoundError):
        parse_file(tmp_path / "nope.jsonl")
```

- [ ] **Step 3: Run — expect failure**

```bash
uv run pytest tests/test_jsonl_parse.py -v
```

Expected: `ImportError`.

- [ ] **Step 4: Implement streaming parser**

`src/ccforensics/jsonl.py`:
```python
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import KNOWN_TYPES, TranscriptEntry, parse_entry


@dataclass
class ParseResult:
    entries: list[TranscriptEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parse_errors: int = 0
    truncated_tail: bool = False
    unknown_types: set[str] = field(default_factory=set)
    seen_versions: set[str] = field(default_factory=set)


def parse_file(path: Path) -> ParseResult:
    """Stream-parse a JSONL file with defensive error handling.

    - Non-final line JSON-parse failure: counted + warned, continue.
    - Final line JSON-parse failure at EOF: silently marked as truncated_tail.
    - Unknown `type` values: recorded in unknown_types, entry kept.
    - File not found: raises FileNotFoundError.
    """
    if not path.exists():
        raise FileNotFoundError(path)

    result = ParseResult()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=False)
    last_idx = len(lines) - 1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            if i == last_idx:
                result.truncated_tail = True
            else:
                result.parse_errors += 1
                result.warnings.append(f"{path}:{i + 1}: malformed JSON, skipped")
            continue

        try:
            entry = parse_entry(raw)
        except Exception as e:
            result.parse_errors += 1
            result.warnings.append(f"{path}:{i + 1}: pydantic rejected line ({e.__class__.__name__}), skipped")
            continue

        if entry.type not in KNOWN_TYPES:
            if entry.type not in result.unknown_types:
                result.warnings.append(f"{path}: unknown type {entry.type!r} (kept, non-billable)")
            result.unknown_types.add(entry.type)

        if entry.version:
            result.seen_versions.add(entry.version)

        result.entries.append(entry)

    return result
```

- [ ] **Step 5: Run — expect pass**

```bash
uv run pytest tests/test_jsonl_parse.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/ccforensics/jsonl.py tests/test_jsonl_parse.py tests/fixtures/truncated tests/fixtures/drift
git commit -m "feat(jsonl): streaming parser with truncation + drift tolerance"
```

---

### Task M1.4: Deduplication

**Files:**
- Modify: `src/ccforensics/jsonl.py`
- Create: `tests/test_jsonl_dedup.py`
- Create: `tests/fixtures/dedup_collision/file_a.jsonl`
- Create: `tests/fixtures/dedup_collision/file_b.jsonl`

- [ ] **Step 1: Write dedup-collision fixtures**

Both files contain the same `(message.id, requestId)` pair — simulating resume/branch duplication.

`tests/fixtures/dedup_collision/file_a.jsonl`:
```jsonl
{"type":"assistant","uuid":"uA1","sessionId":"sA","timestamp":"2026-04-20T10:00:00Z","requestId":"req-shared","message":{"id":"msg-shared","role":"assistant","model":"claude-sonnet-4-5-20250929","content":[],"usage":{"input_tokens":100,"output_tokens":10}}}
{"type":"assistant","uuid":"uA2","sessionId":"sA","timestamp":"2026-04-20T10:01:00Z","requestId":"req-a-only","message":{"id":"msg-a-only","role":"assistant","model":"claude-sonnet-4-5-20250929","content":[],"usage":{"input_tokens":50,"output_tokens":5}}}
```

`tests/fixtures/dedup_collision/file_b.jsonl`:
```jsonl
{"type":"assistant","uuid":"uB1","sessionId":"sB","timestamp":"2026-04-20T10:05:00Z","requestId":"req-shared","message":{"id":"msg-shared","role":"assistant","model":"claude-sonnet-4-5-20250929","content":[],"usage":{"input_tokens":100,"output_tokens":10}}}
{"type":"assistant","uuid":"uB2","sessionId":"sB","timestamp":"2026-04-20T10:06:00Z","requestId":"req-b-only","message":{"id":"msg-b-only","role":"assistant","model":"claude-sonnet-4-5-20250929","content":[],"usage":{"input_tokens":60,"output_tokens":6}}}
```

- [ ] **Step 2: Write failing tests for dedup_key + dedup_entries**

`tests/test_jsonl_dedup.py`:
```python
from __future__ import annotations

import random
from pathlib import Path

from ccforensics.jsonl import dedup_entries, dedup_key, parse_file
from ccforensics.models import parse_entry

FIXTURES = Path(__file__).parent / "fixtures"


def test_dedup_key_prefers_message_id_plus_request_id() -> None:
    entry = parse_entry({
        "type": "assistant",
        "timestamp": "2026-04-20T10:00:00Z",
        "sessionId": "s1",
        "requestId": "req-a",
        "message": {"id": "msg-1", "role": "assistant", "content": []},
    })
    assert dedup_key(entry) == "req:msg-1:req-a"


def test_dedup_key_fallback_to_session_id() -> None:
    entry = parse_entry({
        "type": "assistant",
        "timestamp": "2026-04-20T10:00:00Z",
        "sessionId": "s1",
        "message": {"id": "msg-1", "role": "assistant", "content": []},
    })
    assert dedup_key(entry) == "session:msg-1:s1"


def test_dedup_key_returns_none_when_no_message_id() -> None:
    entry = parse_entry({
        "type": "user",
        "timestamp": "2026-04-20T10:00:00Z",
        "sessionId": "s1",
        "message": None,
    })
    assert dedup_key(entry) is None


def test_dedup_across_files_collapses_shared_key() -> None:
    a = parse_file(FIXTURES / "dedup_collision" / "file_a.jsonl")
    b = parse_file(FIXTURES / "dedup_collision" / "file_b.jsonl")
    merged = dedup_entries(a.entries + b.entries)
    keys = {dedup_key(e) for e in merged if dedup_key(e) is not None}
    # Expect: req:msg-shared:req-shared once, plus a-only and b-only.
    assert "req:msg-shared:req-shared" in keys
    assert "req:msg-a-only:req-a-only" in keys
    assert "req:msg-b-only:req-b-only" in keys
    assert len([e for e in merged if dedup_key(e) == "req:msg-shared:req-shared"]) == 1


def test_dedup_deterministic_under_shuffle() -> None:
    a = parse_file(FIXTURES / "dedup_collision" / "file_a.jsonl")
    b = parse_file(FIXTURES / "dedup_collision" / "file_b.jsonl")
    combined = a.entries + b.entries

    keys_runs: list[list[str | None]] = []
    for seed in (1, 2, 3, 42, 99):
        shuffled = combined.copy()
        random.Random(seed).shuffle(shuffled)
        merged = dedup_entries(shuffled)
        keys_runs.append(sorted(dedup_key(e) or "" for e in merged))

    for run in keys_runs[1:]:
        assert run == keys_runs[0], "dedup output must be deterministic regardless of input order"


def test_first_write_wins_earlier_timestamp() -> None:
    # The same dedup key appears twice; the earlier-timestamped entry should be retained.
    e1 = parse_entry({
        "type": "assistant",
        "timestamp": "2026-04-20T10:00:00Z",
        "sessionId": "s1",
        "requestId": "r",
        "message": {"id": "m", "role": "assistant", "content": []},
    })
    e2 = parse_entry({
        "type": "assistant",
        "timestamp": "2026-04-20T10:05:00Z",
        "sessionId": "s1",
        "requestId": "r",
        "message": {"id": "m", "role": "assistant", "content": []},
    })
    merged = dedup_entries([e2, e1])
    assert len(merged) == 1
    assert merged[0].timestamp == e1.timestamp
```

- [ ] **Step 3: Run — expect failure**

```bash
uv run pytest tests/test_jsonl_dedup.py -v
```

Expected: `ImportError: cannot import name 'dedup_key'`.

- [ ] **Step 4: Add `dedup_key` and `dedup_entries` to `jsonl.py`**

Append to `src/ccforensics/jsonl.py`:
```python
from .models import TranscriptEntry


def dedup_key(entry: TranscriptEntry) -> str | None:
    """Compute a tiered dedup key. Prefix prevents cross-tier collisions.

    Priority:
    1. req:<message.id>:<requestId>   (billing-accurate)
    2. session:<message.id>:<sessionId>  (fallback)
    None: entry has no message.id — passes through un-deduped.
    """
    mid = entry.message.id if entry.message else None
    if mid and entry.request_id:
        return f"req:{mid}:{entry.request_id}"
    if mid and entry.session_id:
        return f"session:{mid}:{entry.session_id}"
    return None


def dedup_entries(entries: list[TranscriptEntry]) -> list[TranscriptEntry]:
    """Dedup by key; on collision the earliest-timestamped entry wins.

    Entries with no dedup_key (e.g., system events) pass through unchanged
    and their order relative to each other is preserved.
    """
    first_seen: dict[str, TranscriptEntry] = {}
    keyless: list[TranscriptEntry] = []

    for entry in entries:
        k = dedup_key(entry)
        if k is None:
            keyless.append(entry)
            continue
        prev = first_seen.get(k)
        if prev is None or entry.timestamp < prev.timestamp:
            first_seen[k] = entry

    # Sorted output so test_dedup_deterministic_under_shuffle holds independent
    # of insertion order.
    deduped = list(first_seen.values())
    deduped.sort(key=lambda e: (e.timestamp, dedup_key(e) or ""))
    # Keyless entries sorted by timestamp for determinism too.
    keyless.sort(key=lambda e: (e.timestamp, e.uuid or ""))
    return deduped + keyless
```

- [ ] **Step 5: Run — expect pass**

```bash
uv run pytest tests/test_jsonl_dedup.py -v
```

Expected: all 6 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/ccforensics/jsonl.py tests/test_jsonl_dedup.py tests/fixtures/dedup_collision
git commit -m "feat(jsonl): tiered dedup with deterministic ordering"
```

---

### Task M1.5: Smoke-test against real JSONL

**Files:**
- Create: `tests/test_real_ingest.py`

- [ ] **Step 1: Write an opt-in real-corpus smoke test**

`tests/test_real_ingest.py`:
```python
"""Opt-in smoke test: ingest every real JSONL under ~/.claude/projects/.

Run with: CCFORENSICS_REAL=1 uv run pytest tests/test_real_ingest.py -v
Skipped by default in CI.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from ccforensics.jsonl import dedup_entries, parse_file
from ccforensics.paths import claude_projects_dir

pytestmark = pytest.mark.skipif(
    os.environ.get("CCFORENSICS_REAL") != "1",
    reason="real-corpus smoke test; set CCFORENSICS_REAL=1 to run",
)


def _all_jsonl_files() -> list[Path]:
    root = claude_projects_dir()
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"))


def test_parses_all_real_jsonl_without_crashing() -> None:
    files = _all_jsonl_files()
    assert len(files) > 0, "no JSONL files to test against"
    total_entries = 0
    total_warnings = 0
    for f in files:
        result = parse_file(f)
        total_entries += len(result.entries)
        total_warnings += len(result.warnings)
    assert total_entries > 0


def test_dedup_is_idempotent_across_real_corpus() -> None:
    all_entries: list = []
    for f in _all_jsonl_files():
        all_entries.extend(parse_file(f).entries)
    first = dedup_entries(all_entries)
    second = dedup_entries(first)
    assert len(first) == len(second)
```

- [ ] **Step 2: Run with real-corpus flag**

```bash
CCFORENSICS_REAL=1 uv run pytest tests/test_real_ingest.py -v
```

Expected: passes. If any single file crashes the parser (not merely warns), that's a real bug — fix the parser, don't change the fixture. Add a synthetic fixture that reproduces the crash, then fix.

- [ ] **Step 3: Commit**

```bash
git add tests/test_real_ingest.py
git commit -m "test: opt-in smoke test against real corpus"
```

**M1 exit criteria:**
- [ ] All unit + integration tests green.
- [ ] `CCFORENSICS_REAL=1 uv run pytest` passes against `~/.claude/projects/`.
- [ ] CI green.
- [ ] No parser crashes on real JSONL.

---

## Milestone M2 — Pricing + Cost Annotation

**Exit:** Every deduped message has `cost_usd` computed. Totals match ccusage on a redacted real session to ±1%.

### Task M2.1: Commit frozen LiteLLM pricing snapshot

**Files:**
- Create: `tests/fixtures/litellm/model_prices.json` (committed snapshot)

- [ ] **Step 1: Fetch today's LiteLLM pricing**

```bash
curl -sSL https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json -o tests/fixtures/litellm/model_prices.json
```

- [ ] **Step 2: Sanity-check it contains current Claude entries**

```bash
uv run python -c "import json; d = json.load(open('tests/fixtures/litellm/model_prices.json')); print([k for k in d if 'sonnet-4-5' in k or 'haiku-4-5' in k or 'opus-4' in k][:20])"
```

Expected: prints a non-empty list including `claude-sonnet-4-5-20250929` and `claude-haiku-4-5-20251001`.

- [ ] **Step 3: Commit the fixture**

```bash
git add tests/fixtures/litellm/model_prices.json
git commit -m "test(fixtures): freeze LiteLLM pricing snapshot for tests"
```

---

### Task M2.2: Pricing module — fetch, cache, resolve

**Files:**
- Create: `src/ccforensics/pricing.py`
- Create: `tests/test_pricing.py`

- [ ] **Step 1: Write failing tests for pricing resolution**

`tests/test_pricing.py`:
```python
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ccforensics.pricing import (
    ModelPrice,
    PricingCache,
    compute_message_cost,
    fallback_hardcoded,
    resolve_pricing,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def test_resolves_exact_model_name(pricing_data: dict) -> None:
    p = resolve_pricing("claude-sonnet-4-5-20250929", pricing_data)
    assert p is not None
    assert p.input_cost > 0
    assert p.output_cost > 0


def test_resolves_alias_via_substring(pricing_data: dict) -> None:
    # A hypothetical alias not a literal key but substring-matched.
    # "claude-sonnet-4-5" should find an entry with a longer name.
    p = resolve_pricing("claude-sonnet-4-5", pricing_data)
    assert p is not None


def test_returns_none_for_unknown(pricing_data: dict) -> None:
    assert resolve_pricing("definitely-not-a-real-model", pricing_data) is None


def test_model_price_fills_cache_fields_from_ratio_when_missing() -> None:
    entry = {"input_cost_per_token": 0.000003, "output_cost_per_token": 0.000015}
    p = ModelPrice.from_entry(entry)
    assert p.input_cost == 0.000003
    assert p.output_cost == 0.000015
    # cache_creation defaults to 25% of input when absent
    assert p.cache_creation_cost == pytest.approx(0.00000075)
    # cache_read defaults to 10% of input
    assert p.cache_read_cost == pytest.approx(0.0000003)


def test_model_price_uses_explicit_cache_fields_when_present() -> None:
    entry = {
        "input_cost_per_token": 0.000003,
        "output_cost_per_token": 0.000015,
        "cache_creation_input_token_cost": 0.00000375,
        "cache_read_input_token_cost": 0.0000003,
    }
    p = ModelPrice.from_entry(entry)
    assert p.cache_creation_cost == 0.00000375
    assert p.cache_read_cost == 0.0000003


def test_compute_message_cost() -> None:
    p = ModelPrice(
        input_cost=0.000003,
        output_cost=0.000015,
        cache_creation_cost=0.00000375,
        cache_read_cost=0.0000003,
    )
    cost = compute_message_cost(
        price=p,
        input_tokens=1000,
        output_tokens=200,
        cache_creation=500,
        cache_read=10000,
    )
    # 1000*3e-6 + 200*15e-6 + 500*3.75e-6 + 10000*0.3e-6
    expected = 0.003 + 0.003 + 0.001875 + 0.003
    assert cost == pytest.approx(expected)


def test_compute_message_cost_handles_none() -> None:
    p = ModelPrice(input_cost=1e-6, output_cost=1e-6, cache_creation_cost=1e-6, cache_read_cost=1e-6)
    # None tokens should be treated as 0.
    assert compute_message_cost(p, None, None, None, None) == 0.0


def test_fallback_hardcoded_covers_current_claude_models() -> None:
    data = fallback_hardcoded()
    assert "claude-sonnet-4-5-20250929" in data
    assert "claude-haiku-4-5-20251001" in data
    for key, entry in data.items():
        assert "input_cost_per_token" in entry
        assert "output_cost_per_token" in entry


def test_pricing_cache_reads_fresh_snapshot(tmp_path: Path, pricing_data: dict) -> None:
    cache_file = tmp_path / "litellm.json"
    cache_file.write_text(json.dumps({"fetched_at": 9999999999, "data": pricing_data}))
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=_unreachable_client())
    assert "claude-sonnet-4-5-20250929" in data


def test_pricing_cache_falls_back_to_stale_on_fetch_failure(
    tmp_path: Path, pricing_data: dict
) -> None:
    cache_file = tmp_path / "litellm.json"
    cache_file.write_text(json.dumps({"fetched_at": 0, "data": pricing_data}))  # stale
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=_failing_client())
    # Should return the stale data (and log a warning — not asserted here)
    assert "claude-sonnet-4-5-20250929" in data


def test_pricing_cache_falls_back_to_hardcoded_when_empty(tmp_path: Path) -> None:
    cache_file = tmp_path / "litellm.json"
    cache = PricingCache(cache_file=cache_file, ttl_seconds=86400)
    data = cache.load_or_fetch(http_client=_failing_client())
    assert "claude-sonnet-4-5-20250929" in data  # from hardcoded fallback


def _unreachable_client() -> httpx.Client:
    # If anything tries to actually use this, raise.
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected HTTP call")
    return httpx.Client(transport=httpx.MockTransport(handler))


def _failing_client() -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated offline")
    return httpx.Client(transport=httpx.MockTransport(handler))
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_pricing.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement `pricing.py`**

`src/ccforensics/pricing.py`:
```python
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("ccforensics.pricing")

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)


@dataclass(frozen=True)
class ModelPrice:
    input_cost: float
    output_cost: float
    cache_creation_cost: float
    cache_read_cost: float

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> ModelPrice:
        inp = float(entry.get("input_cost_per_token", 0.0) or 0.0)
        out = float(entry.get("output_cost_per_token", 0.0) or 0.0)
        cc = entry.get("cache_creation_input_token_cost")
        cr = entry.get("cache_read_input_token_cost")
        cache_create = float(cc) if cc is not None else inp * 0.25
        cache_read = float(cr) if cr is not None else inp * 0.10
        return cls(
            input_cost=inp,
            output_cost=out,
            cache_creation_cost=cache_create,
            cache_read_cost=cache_read,
        )


def _candidates(model: str) -> list[str]:
    return [
        model,
        f"anthropic/{model}",
        f"claude-3-5-{model}",
        f"claude-3-{model}",
        f"claude-{model}",
    ]


def resolve_pricing(model: str, data: dict[str, dict[str, Any]]) -> ModelPrice | None:
    """Resolve a model name to a ModelPrice via fuzzy lookup."""
    for c in _candidates(model):
        if c in data:
            return ModelPrice.from_entry(data[c])
    lowered = model.lower()
    for k, v in data.items():
        kl = k.lower()
        if lowered in kl or kl in lowered:
            return ModelPrice.from_entry(v)
    return None


def compute_message_cost(
    price: ModelPrice,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_creation: int | None,
    cache_read: int | None,
) -> float:
    return (
        (input_tokens or 0) * price.input_cost
        + (output_tokens or 0) * price.output_cost
        + (cache_creation or 0) * price.cache_creation_cost
        + (cache_read or 0) * price.cache_read_cost
    )


def fallback_hardcoded() -> dict[str, dict[str, float]]:
    """Minimal offline fallback covering current Claude models."""
    return {
        "claude-sonnet-4-5-20250929": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_creation_input_token_cost": 0.00000375,
            "cache_read_input_token_cost": 0.0000003,
        },
        "claude-haiku-4-5-20251001": {
            "input_cost_per_token": 0.000001,
            "output_cost_per_token": 0.000005,
            "cache_creation_input_token_cost": 0.00000125,
            "cache_read_input_token_cost": 0.0000001,
        },
        "claude-opus-4-1-20250805": {
            "input_cost_per_token": 0.000015,
            "output_cost_per_token": 0.000075,
            "cache_creation_input_token_cost": 0.00001875,
            "cache_read_input_token_cost": 0.0000015,
        },
        "claude-opus-4-5-20251101": {
            "input_cost_per_token": 0.000015,
            "output_cost_per_token": 0.000075,
            "cache_creation_input_token_cost": 0.00001875,
            "cache_read_input_token_cost": 0.0000015,
        },
        "claude-sonnet-4-20250514": {
            "input_cost_per_token": 0.000003,
            "output_cost_per_token": 0.000015,
            "cache_creation_input_token_cost": 0.00000375,
            "cache_read_input_token_cost": 0.0000003,
        },
        "claude-3-5-haiku-20241022": {
            "input_cost_per_token": 0.0000008,
            "output_cost_per_token": 0.000004,
            "cache_creation_input_token_cost": 0.000001,
            "cache_read_input_token_cost": 0.00000008,
        },
    }


@dataclass
class PricingCache:
    cache_file: Path
    ttl_seconds: int = 24 * 60 * 60

    def load_or_fetch(self, http_client: httpx.Client | None = None) -> dict[str, dict[str, Any]]:
        now = int(time.time())
        if self.cache_file.exists():
            try:
                wrapper = json.loads(self.cache_file.read_text())
                fetched_at = int(wrapper.get("fetched_at", 0))
                data = wrapper.get("data") or {}
                if data and now - fetched_at < self.ttl_seconds:
                    return data
                # Stale — attempt refresh, fall back to stale on failure.
                try:
                    return self._fetch_and_store(http_client, now)
                except Exception as e:
                    logger.warning("pricing refresh failed (%s); using stale cache", e)
                    return data
            except Exception as e:
                logger.warning("pricing cache unreadable (%s); refetching", e)
        # No cache; try fetch, else hardcoded.
        try:
            return self._fetch_and_store(http_client, now)
        except Exception as e:
            logger.warning("pricing fetch failed (%s); using hardcoded fallback", e)
            return fallback_hardcoded()

    def _fetch_and_store(self, client: httpx.Client | None, now: int) -> dict[str, Any]:
        client = client or httpx.Client(timeout=15.0)
        resp = client.get(LITELLM_URL)
        resp.raise_for_status()
        data = resp.json()
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text(json.dumps({"fetched_at": now, "data": data}))
        return data
```

- [ ] **Step 4: Run tests — expect pass**

```bash
uv run pytest tests/test_pricing.py -v
```

Expected: all 10 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/pricing.py tests/test_pricing.py
git commit -m "feat(pricing): LiteLLM fetcher + fuzzy resolver + offline fallback"
```

---

### Task M2.3: Annotate entries with cost

**Files:**
- Modify: `src/ccforensics/jsonl.py`
- Create: `tests/test_cost_annotation.py`

- [ ] **Step 1: Write failing test for cost annotation**

`tests/test_cost_annotation.py`:
```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccforensics.jsonl import annotate_cost, parse_file
from ccforensics.pricing import resolve_pricing

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def test_assistant_entries_get_cost(pricing_data: dict) -> None:
    result = parse_file(FIXTURES / "basic" / "session.jsonl")
    annotated = annotate_cost(result.entries, pricing_data)
    assistant = [e for e in annotated if e.entry.type == "assistant"]
    assert len(assistant) == 2
    for e in assistant:
        assert e.cost_usd is not None
        assert e.cost_usd > 0


def test_user_entries_have_zero_cost(pricing_data: dict) -> None:
    result = parse_file(FIXTURES / "basic" / "session.jsonl")
    annotated = annotate_cost(result.entries, pricing_data)
    user_entries = [e for e in annotated if e.entry.type == "user"]
    for e in user_entries:
        assert e.cost_usd == 0.0 or e.cost_usd is None


def test_missing_model_pricing_yields_none(pricing_data: dict) -> None:
    # Forge an entry with an unknown model.
    from ccforensics.models import parse_entry
    raw = {
        "type": "assistant",
        "uuid": "u",
        "sessionId": "s",
        "timestamp": "2026-04-20T10:00:00Z",
        "requestId": "r",
        "message": {"id": "m", "role": "assistant", "model": "imaginary-model-v99",
                    "content": [], "usage": {"input_tokens": 100, "output_tokens": 10}},
    }
    entry = parse_entry(raw)
    annotated = annotate_cost([entry], pricing_data)
    assert annotated[0].cost_usd is None
    assert annotated[0].pricing_unresolved_model == "imaginary-model-v99"
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_cost_annotation.py -v
```

- [ ] **Step 3: Implement `annotate_cost`**

Append to `src/ccforensics/jsonl.py`:
```python
from dataclasses import dataclass
from typing import Any

from .pricing import compute_message_cost, resolve_pricing


@dataclass
class AnnotatedEntry:
    entry: TranscriptEntry
    cost_usd: float | None
    pricing_unresolved_model: str | None = None


def annotate_cost(
    entries: list[TranscriptEntry],
    pricing_data: dict[str, dict[str, Any]],
) -> list[AnnotatedEntry]:
    out: list[AnnotatedEntry] = []
    unresolved: set[str] = set()
    for e in entries:
        if e.type != "assistant" or e.message is None or e.message.usage is None:
            out.append(AnnotatedEntry(entry=e, cost_usd=0.0 if e.type == "user" else None))
            continue
        model = e.message.model
        if model is None:
            out.append(AnnotatedEntry(entry=e, cost_usd=None))
            continue
        price = resolve_pricing(model, pricing_data)
        if price is None:
            if model not in unresolved:
                unresolved.add(model)
            out.append(AnnotatedEntry(entry=e, cost_usd=None, pricing_unresolved_model=model))
            continue
        usage = e.message.usage
        cost = compute_message_cost(
            price=price,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation=usage.cache_creation_input_tokens,
            cache_read=usage.cache_read_input_tokens,
        )
        out.append(AnnotatedEntry(entry=e, cost_usd=cost))
    return out
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_cost_annotation.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/jsonl.py tests/test_cost_annotation.py
git commit -m "feat(jsonl): annotate entries with per-message cost"
```

---

### Task M2.4: Ground-truth vs ccusage

**Files:**
- Create: `tests/fixtures/real/redacted_session.jsonl`
- Create: `tests/test_ground_truth.py`

This task validates cost math against an independent tool.

- [ ] **Step 1: Pick a real session + redact + capture ccusage total**

```bash
# Pick a session with moderate cost for comparison
uv run ccforensics session --list  # once M4 lands; for now, manually pick one from ~/.claude/projects/
```

For M2 (before M4 exists), pick a JSONL manually. Redact message text but preserve every `usage`, `model`, `requestId`, `id`, and timestamp.

Write a redaction script `scripts/redact_jsonl.py`:
```python
"""Redact a JSONL file for test fixtures.

Preserves everything cost-related; replaces text content with '[REDACTED]'.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def redact_content_block(block: dict) -> dict:
    t = block.get("type")
    if t == "text":
        return {"type": "text", "text": "[REDACTED]"}
    if t == "tool_use":
        return {"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": {}}
    if t == "tool_result":
        return {"type": "tool_result", "tool_use_id": block.get("tool_use_id"), "content": "[REDACTED]"}
    return {"type": t}


def redact(line: str) -> str:
    record = json.loads(line)
    msg = record.get("message")
    if msg and isinstance(msg.get("content"), list):
        msg["content"] = [redact_content_block(b) for b in msg["content"]]
    for k in ("summary", "slug"):
        if k in record and isinstance(record[k], str):
            record[k] = "[REDACTED]"
    if "cwd" in record:
        record["cwd"] = "/redacted"
    return json.dumps(record)


def main() -> None:
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open() as fin, dst.open("w") as fout:
        for line in fin:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                fout.write(redact(line) + "\n")
            except json.JSONDecodeError:
                continue  # drop truncated tail


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run redaction + capture ccusage total**

```bash
# Install ccusage for comparison (one-time)
npm install -g ccusage

# Pick a session file; copy + redact
mkdir -p tests/fixtures/real
uv run python scripts/redact_jsonl.py \
    ~/.claude/projects/<encoded-proj>/<session-uuid>.jsonl \
    tests/fixtures/real/redacted_session.jsonl

# Capture ccusage's total for the original (not the redacted)
ccusage session <session-uuid> --json > /tmp/ccusage-total.json
cat /tmp/ccusage-total.json  # note the total cost figure
```

Record the ccusage total in a comment at the top of the test.

- [ ] **Step 3: Write ground-truth test**

`tests/test_ground_truth.py`:
```python
"""Cost math sanity check against ccusage.

The redacted session was captured alongside a ccusage-reported total.
Our computation must match within ±1%.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccforensics.jsonl import annotate_cost, dedup_entries, parse_file

FIXTURES = Path(__file__).parent / "fixtures"

# Captured from `ccusage session <uuid>` at the time of redaction.
# Update this value when the fixture is refreshed.
CCUSAGE_TOTAL_USD = 5.2345  # TODO: replace with captured value


@pytest.fixture
def pricing_data() -> dict:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def test_total_cost_matches_ccusage_within_1pct(pricing_data: dict) -> None:
    result = parse_file(FIXTURES / "real" / "redacted_session.jsonl")
    deduped = dedup_entries(result.entries)
    annotated = annotate_cost(deduped, pricing_data)
    total = sum(a.cost_usd for a in annotated if a.cost_usd)
    tolerance = CCUSAGE_TOTAL_USD * 0.01
    assert abs(total - CCUSAGE_TOTAL_USD) < tolerance, (
        f"ccforensics computed ${total:.4f}, ccusage reported ${CCUSAGE_TOTAL_USD:.4f}, "
        f"diff ${abs(total - CCUSAGE_TOTAL_USD):.4f} exceeds ±1% tolerance ${tolerance:.4f}"
    )
```

- [ ] **Step 4: Fill in `CCUSAGE_TOTAL_USD` with actual captured value and run**

```bash
uv run pytest tests/test_ground_truth.py -v
```

If it fails by more than 1%, investigate:
- Model-name resolution missed an entry? (Check for `pricing_unresolved_model` in annotate_cost output.)
- Dedup collapsing records ccusage keeps separate (or vice versa)?
- Cache-tier pricing different between our LiteLLM snapshot and ccusage's runtime fetch?

Fix the parser or pricing logic; do not adjust the 1% tolerance.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/real/redacted_session.jsonl scripts/redact_jsonl.py tests/test_ground_truth.py
git commit -m "test: ground-truth cost parity with ccusage ±1%"
```

**M2 exit criteria:**
- [ ] All pricing + cost-annotation tests green.
- [ ] Ground-truth test passes against a real redacted session.
- [ ] CI green.

---

## Milestone M3 — SQLite Index + Reconciliation

**Exit:** `ccforensics index --rebuild` ingests `~/.claude/projects/` into the SQLite index; `ccforensics index --stats` reports counts. Re-running `--rebuild` with no file changes completes in < 1s.

### Task M3.1: Schema DDL + migrations

**Files:**
- Create: `src/ccforensics/index.py`
- Create: `tests/test_index_schema.py`

- [ ] **Step 1: Write failing test for schema creation**

`tests/test_index_schema.py`:
```python
from __future__ import annotations

import sqlite3
from pathlib import Path

from ccforensics.index import (
    CURRENT_SCHEMA_VERSION,
    ensure_schema,
    open_connection,
)


def test_fresh_db_applies_current_schema(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    conn.commit()

    cur = conn.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == CURRENT_SCHEMA_VERSION

    # Spot-check required tables exist.
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for required in (
        "files",
        "messages",
        "subagent_spawns",
        "skill_activations",
        "plugins",
        "user_level_artifacts",
        "session_summaries",
        "session_rollups",
    ):
        assert required in existing, f"missing table {required}"


def test_reapplying_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    ensure_schema(conn)  # should not raise
    cur = conn.execute("PRAGMA user_version")
    assert cur.fetchone()[0] == CURRENT_SCHEMA_VERSION
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_index_schema.py -v
```

- [ ] **Step 3: Implement `index.py` schema + migrations**

`src/ccforensics/index.py`:
```python
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
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_index_schema.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/index.py tests/test_index_schema.py
git commit -m "feat(index): SQLite schema + migration scaffold"
```

---

### Task M3.2: File reconciliation by (path, mtime, size)

**Files:**
- Modify: `src/ccforensics/index.py`
- Create: `tests/test_index_reconcile.py`

- [ ] **Step 1: Write failing test for reconciliation**

`tests/test_index_reconcile.py`:
```python
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ccforensics.index import (
    count_messages_for_file,
    ensure_schema,
    open_connection,
    reconcile_file,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def test_first_reconcile_inserts_files_and_messages(tmp_path: Path, pricing_data: dict) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)

    src = FIXTURES / "basic" / "session.jsonl"
    reconcile_file(conn, src, pricing_data)
    conn.commit()

    file_rows = conn.execute("SELECT path, session_id, kind FROM files").fetchall()
    assert len(file_rows) == 1
    assert file_rows[0][1] == "s1"
    assert file_rows[0][2] == "main"

    msg_count = count_messages_for_file(conn, src)
    assert msg_count > 0


def test_reconcile_unchanged_file_is_noop(tmp_path: Path, pricing_data: dict) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    src = FIXTURES / "basic" / "session.jsonl"

    reconcile_file(conn, src, pricing_data)
    conn.commit()
    first_last_parsed = conn.execute(
        "SELECT last_parsed_at FROM files WHERE path=?", (str(src),)
    ).fetchone()[0]

    time.sleep(1.1)
    reconcile_file(conn, src, pricing_data)
    conn.commit()
    second_last_parsed = conn.execute(
        "SELECT last_parsed_at FROM files WHERE path=?", (str(src),)
    ).fetchone()[0]

    assert first_last_parsed == second_last_parsed, "unchanged file should be skipped"


def test_reconcile_changed_file_replaces_messages(
    tmp_path: Path, pricing_data: dict
) -> None:
    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)

    # Copy the basic fixture into tmp so we can mutate it.
    src = tmp_path / "session.jsonl"
    src.write_bytes((FIXTURES / "basic" / "session.jsonl").read_bytes())

    reconcile_file(conn, src, pricing_data)
    conn.commit()
    initial_count = count_messages_for_file(conn, src)

    # Append a new record; mtime + size both change.
    new_line = (
        '{"type":"assistant","uuid":"u99","sessionId":"s1",'
        '"timestamp":"2026-04-20T10:10:00Z","requestId":"req-new",'
        '"message":{"id":"msg-new","role":"assistant",'
        '"model":"claude-sonnet-4-5-20250929","content":[],'
        '"usage":{"input_tokens":1,"output_tokens":1}}}\n'
    )
    with src.open("a") as f:
        f.write(new_line)
    # Force mtime change (same-second writes can collide on some FS).
    os.utime(src, (time.time() + 5, time.time() + 5))

    reconcile_file(conn, src, pricing_data)
    conn.commit()

    new_count = count_messages_for_file(conn, src)
    assert new_count == initial_count + 1
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_index_reconcile.py -v
```

- [ ] **Step 3: Implement reconciliation**

Append to `src/ccforensics/index.py`:
```python
from __future__ import annotations

import json
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


def _classify_file(path: Path) -> tuple[str, str | None, str]:
    """Return (kind, agent_id, session_id).

    - Main session: ~/.claude/projects/<enc>/<sessionId>.jsonl
    - Subagent:     ~/.claude/projects/<enc>/<sessionId>/subagents/agent-<id>.jsonl
    """
    name = path.name
    m = _SUBAGENT_FILENAME.match(name)
    if m and path.parent.name == "subagents":
        return ("subagent", m.group(1), path.parent.parent.name)
    # Main file: stem is the sessionId
    return ("main", None, path.stem)


def _row_is_unchanged(
    conn: sqlite3.Connection, path: Path, mtime_ns: int, size: int
) -> bool:
    cur = conn.execute(
        "SELECT mtime_ns, size FROM files WHERE path=?", (str(path),)
    )
    row = cur.fetchone()
    return bool(row) and row[0] == mtime_ns and row[1] == size


def _purge_file_rows(conn: sqlite3.Connection, path: Path) -> None:
    # Cascade on messages happens automatically via FK. For spawns + skills
    # we do it explicitly since they reference messages + sessions.
    s = str(path)
    conn.execute(
        "DELETE FROM subagent_spawns WHERE child_file_path=?", (s,)
    )
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
    # Locate primary tool_use_id + name if the assistant emitted a tool_use block.
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

    # dedup across the file itself (collapses intra-file duplicates immediately).
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
            str(path), mtime_ns, size, session_id, kind, agent_id,
            next(iter(result.seen_versions), None) if result.seen_versions else None,
            len(result.warnings), int(time.time()),
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
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_index_reconcile.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/index.py tests/test_index_reconcile.py
git commit -m "feat(index): reconcile files by (path, mtime, size) with cascade purge"
```

---

### Task M3.3: Walk `~/.claude/projects/` + full rebuild

**Files:**
- Modify: `src/ccforensics/index.py`
- Create: `tests/test_index_walk.py`

- [ ] **Step 1: Write failing test for walking + reconciling all files**

`tests/test_index_walk.py`:
```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccforensics.index import (
    ensure_schema,
    open_connection,
    reconcile_projects_dir,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def pricing_data() -> dict:
    return json.loads((FIXTURES / "litellm" / "model_prices.json").read_text())


def test_walk_indexes_all_jsonl(tmp_path: Path, pricing_data: dict) -> None:
    # Build a fake projects dir with 2 main sessions and 1 subagent dir.
    proj = tmp_path / "projects"
    sess_a = proj / "-home-test"
    sess_a.mkdir(parents=True)
    (sess_a / "sess-a.jsonl").write_bytes((FIXTURES / "basic" / "session.jsonl").read_bytes())

    sess_b = proj / "-home-test-other"
    sess_b.mkdir(parents=True)
    (sess_b / "sess-b.jsonl").write_bytes((FIXTURES / "drift" / "session.jsonl").read_bytes())
    # Subagent layout for sess-b:
    sub = sess_b / "sess-b" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-deadbeef.jsonl").write_bytes((FIXTURES / "basic" / "session.jsonl").read_bytes())

    db = tmp_path / "index.sqlite"
    conn = open_connection(db)
    ensure_schema(conn)
    stats = reconcile_projects_dir(conn, proj, pricing_data)
    conn.commit()

    assert stats.files_indexed == 3
    assert stats.files_changed == 3
    file_rows = conn.execute("SELECT kind FROM files").fetchall()
    kinds = {row[0] for row in file_rows}
    assert kinds == {"main", "subagent"}
```

- [ ] **Step 2: Run — expect failure**

```bash
uv run pytest tests/test_index_walk.py -v
```

- [ ] **Step 3: Implement `reconcile_projects_dir`**

Append to `src/ccforensics/index.py`:
```python
from dataclasses import dataclass


@dataclass
class ReconcileStats:
    files_scanned: int = 0
    files_indexed: int = 0
    files_changed: int = 0
    files_skipped_unchanged: int = 0


def reconcile_projects_dir(
    conn: sqlite3.Connection,
    projects_dir: Path,
    pricing_data: dict[str, Any],
) -> ReconcileStats:
    """Walk the Claude Code projects directory and reconcile every *.jsonl file.

    Includes both main session files and subagent files under <session>/subagents/.
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
        reconcile_file(conn, path, pricing_data)
        stats.files_indexed += 1
        stats.files_changed += 1
    return stats
```

- [ ] **Step 4: Run — expect pass**

```bash
uv run pytest tests/test_index_walk.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/index.py tests/test_index_walk.py
git commit -m "feat(index): walk ~/.claude/projects and reconcile all JSONL"
```

---

### Task M3.4: `index --rebuild` and `index --stats` CLI commands

**Files:**
- Modify: `src/ccforensics/cli.py`
- Modify: `src/ccforensics/index.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Append to `tests/test_cli.py`:
```python
from pathlib import Path

from click.testing import CliRunner

from ccforensics.cli import main as cli_main


def test_index_stats_on_missing_db(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli_main, ["index", "stats"])
    assert result.exit_code == 0
    # Fresh DB scenario should report zero counts without error.
    assert "files: 0" in result.output or "files=0" in result.output


def test_index_rebuild_on_empty_projects(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["index", "rebuild", "--yes"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Add `index_stats` helper to `index.py`**

Append to `src/ccforensics/index.py`:
```python
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
```

- [ ] **Step 3: Wire CLI commands**

Replace the `@index.command("stats")` and `@index.command("rebuild")` stubs in `src/ccforensics/cli.py`:

```python
import sqlite3
from datetime import datetime
from pathlib import Path

from .index import (
    collect_stats,
    ensure_schema,
    open_connection,
    reconcile_projects_dir,
)
from .paths import ccforensics_cache_dir, claude_projects_dir
from .pricing import PricingCache


def _index_db_path() -> Path:
    return ccforensics_cache_dir() / "index.sqlite"


@index.command("stats")
def index_stats() -> None:
    """Show index stats."""
    db = _index_db_path()
    conn = open_connection(db)
    ensure_schema(conn)
    stats = collect_stats(conn, db)
    lines = [
        f"files: {stats.files}",
        f"sessions: {stats.sessions}",
        f"messages: {stats.messages}",
        f"subagent_spawns: {stats.subagent_spawns}",
        f"skill_activations: {stats.skill_activations}",
        f"db_size_bytes: {stats.db_size_bytes}",
    ]
    if stats.last_refresh:
        lines.append(
            f"last_refresh: {datetime.utcfromtimestamp(stats.last_refresh).isoformat()}Z"
        )
    else:
        lines.append("last_refresh: never")
    click.echo("\n".join(lines))


@index.command("rebuild")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
def index_rebuild(yes: bool) -> None:
    """Drop and rebuild the index from scratch."""
    db = _index_db_path()
    if db.exists() and not yes:
        click.confirm(f"Delete and rebuild index at {db}?", abort=True)
    if db.exists():
        db.unlink()
    conn = open_connection(db)
    ensure_schema(conn)
    pricing = PricingCache(cache_file=ccforensics_cache_dir() / "litellm.json").load_or_fetch()
    stats = reconcile_projects_dir(conn, claude_projects_dir(), pricing)
    conn.commit()
    click.echo(
        f"indexed {stats.files_indexed} file(s); "
        f"scanned {stats.files_scanned}, skipped {stats.files_skipped_unchanged}"
    )
```

- [ ] **Step 4: Run CLI tests**

```bash
uv run pytest tests/test_cli.py -v
```

- [ ] **Step 5: Smoke-test against real corpus**

```bash
uv run ccforensics index rebuild --yes
uv run ccforensics index stats
# Expected: non-zero files, sessions, messages. Note db_size_bytes for later comparison.
```

- [ ] **Step 6: Verify no-change rebuild is fast**

```bash
time uv run ccforensics index rebuild --yes
# Expected: the files_scanned == files_skipped_unchanged + (few changed), completes in <5s
```

- [ ] **Step 7: Commit**

```bash
git add src/ccforensics/index.py src/ccforensics/cli.py tests/test_cli.py
git commit -m "feat(cli): index rebuild + stats commands"
```

**M3 exit criteria:**
- [ ] `uv run ccforensics index rebuild` ingests real corpus without crash.
- [ ] `uv run ccforensics index stats` reports counts.
- [ ] Re-running `--rebuild` on unchanged corpus completes in <5s.
- [ ] CI green. Coverage ≥85%.

---

## Milestone M4 — `session --list`

**Exit:** `uv run ccforensics session --list` renders a terminal table on the real corpus; `--project`, `--since`, `--until`, `--grep`, `--sort`, `--reverse`, `--limit`, `--json`, `--csv`, `--no-refresh` all wired. SC5 verified: pick a remembered session in ≤30s. CI green. Coverage ≥85%.

**Expansion rationale (written post-M3):** no schema migration needed. All numeric summary fields derive from the existing `messages` table; summary text + `cwd` come from re-parsing the session's main JSONL inside `_recompute_session_summary` (only called for sessions whose files changed this run — cheap in practice). Resolver, date-parse, and formatting helpers live under `report/` since they're presentation concerns shared with M7+.

### Task M4.1: Populate `session_summaries` during reconciliation

**Files:**
- Modify: `src/ccforensics/index.py`
- Create: `tests/test_session_summaries.py`

- [ ] **Step 1: Write failing tests**

`tests/test_session_summaries.py` covers:
- `started_at == min(message.ts)`, `last_active_at == max(message.ts)`, `duration_s` math.
- `turn_count` = count of `role='user' AND is_meta=0 AND is_sidechain=0`.
- `total_cost_usd` = SUM of non-NULL `cost_usd`; NULL iff every message's cost is NULL.
- Summary extraction priority:
  1. `type='summary'` entry whose `leafUuid` matches a message uuid in this session wins.
  2. Else: `isCompactSummary=true` entry in this session's main file, most recent.
  3. Else: first user prompt (`role='user'`, not meta, not sidechain) — sanitized.
  4. Else: `<no summary available>`.
- First-prompt sanitization:
  - Strip `<command-name>...</command-name>` and sibling `<command-message>`, `<command-args>` wrappers.
  - Replace IDE-attachment marker blocks with `📎 <path>` emoji form.
  - Skip hook-injection content entirely when it's the whole prompt body (fall through to next candidate).
  - Collapse internal newlines to single space.
  - Trim and cap at 1000 chars (store full; UI layer wraps for display).
- `project_path`: cwd of first message in session, else `decode_project_dirname(<encoded>)` from main file path.
- `project_display`: last segment of `project_path`, truncated to 30 chars.
- Recompute is idempotent: running reconcile twice yields identical `session_summaries` rows.
- Only touched sessions recompute: if session A's files are unchanged, its summary row is not rewritten.

Use a fixture builder that writes synthetic JSONL with known content: summary entry, user prompt with command-wrapper, cwd field, varying ts values.

- [ ] **Step 2: Extract a session-summary module inside `index.py`**

Add to `src/ccforensics/index.py` (near the top, after imports):

```python
_COMMAND_WRAPPER_RE = re.compile(
    r"<command-(name|message|args)>.*?</command-\1>",
    re.DOTALL,
)
_IDE_ATTACHMENT_RE = re.compile(
    r"<ide[^>]*>.*?<file[^>]*>(?P<path>[^<]+)</file>.*?</ide[^>]*>",
    re.DOTALL,
)
_HOOK_INJECTION_MARKER = "<session-start-hook>"


def _sanitize_prompt(text: str) -> str:
    """Strip command wrappers, replace IDE attachments, collapse whitespace, cap at 1000."""
    text = _COMMAND_WRAPPER_RE.sub("", text)
    text = _IDE_ATTACHMENT_RE.sub(lambda m: f"📎 {m.group('path').strip()}", text)
    text = " ".join(text.split())
    return text[:1000]


def _is_pure_hook_injection(text: str) -> bool:
    """Heuristic: treat as skip-this-candidate if it's dominated by hook bootstrap markers."""
    return _HOOK_INJECTION_MARKER in text and len(text) > 200
```

- [ ] **Step 3: Add `recompute_session_summary(conn, session_id)`**

It:
1. Looks up the main file row (`SELECT path FROM files WHERE session_id=? AND kind='main' LIMIT 1`).
2. Computes numeric fields via SQL aggregation on `messages` (started/last-active/duration/turn_count/total_cost).
3. If a main file exists, re-parses it with `parse_file()` to extract:
   - `cwd` (from first entry with `.cwd` set)
   - Summary candidates (scan for `type='summary'` with `leaf_uuid` in the session's uuid set, OR `is_compact_summary=True`)
   - First user prompt text (first `type='user'` entry where `message.role == 'user'`, skipping hook-injection-dominated prompts)
4. Applies priority chain, sanitizes, stores.
5. `INSERT OR REPLACE INTO session_summaries`.

Key SQL:
```sql
SELECT MIN(ts), MAX(ts),
       SUM(CASE WHEN role='user' AND is_meta=0 AND is_sidechain=0 THEN 1 ELSE 0 END),
       SUM(cost_usd),
       SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END)
  FROM messages WHERE session_id=?
```
The fifth column distinguishes "all NULL" (cost is NULL) from "some populated" (cost is the partial SUM).

- [ ] **Step 4: Wire into `reconcile_projects_dir`**

Track `session_ids_changed: set[str]` as files are indexed. After the file loop:
```python
for sid in session_ids_changed:
    recompute_session_summary(conn, sid)
conn.commit()
```

Return the set via `ReconcileStats.sessions_recomputed` so the CLI can report it.

- [ ] **Step 5: Run tests + smoke-run on real corpus**

```bash
uv run pytest tests/test_session_summaries.py -v
uv run ccforensics index rebuild --force --yes
sqlite3 ~/.cache/ccforensics/index.sqlite \
  "SELECT session_id, project_display, turn_count, total_cost_usd,
          substr(summary_text,1,60), summary_source
     FROM session_summaries ORDER BY last_active_at DESC LIMIT 10"
# Expected: recognizable session data; no NULL summary_source unless truly no-prompt.
```

- [ ] **Step 6: Commit**

```bash
git add src/ccforensics/index.py tests/test_session_summaries.py
git commit -m "feat(index): populate session_summaries during reconcile"
```

---

### Task M4.2: Shared report helpers (resolver, dates, format)

**Files:**
- Create: `src/ccforensics/report/__init__.py`
- Create: `src/ccforensics/report/resolver.py`
- Create: `src/ccforensics/report/_dates.py`
- Create: `src/ccforensics/report/_format.py`
- Create: `tests/test_report_resolver.py`
- Create: `tests/test_report_dates.py`
- Create: `tests/test_report_format.py`

- [ ] **Step 1: Write failing tests**

`tests/test_report_resolver.py`:
- Full UUID → returns unchanged.
- ≥6-char prefix unique → returns full.
- Prefix <6 chars → `ValueError("prefix must be ≥6 characters")`.
- Prefix matches 2 sessions → `AmbiguousPrefix` exception listing all matches.
- No match → `SessionNotFound(prefix)`.
- Absolute `.jsonl` path → resolved via `files.path`; file not in index → `SessionNotFound`.

`tests/test_report_dates.py`:
- `parse_since("2026-04-15")` → 2026-04-15 00:00:00 UTC.
- `parse_until("2026-04-15")` → 2026-04-15 23:59:59 UTC.
- `parse_since("7d")` at fixed `now=2026-04-22` → 2026-04-15 00:00:00 UTC.
- `parse_since("today")` → start of today.
- `parse_since("yesterday")` → start of yesterday.
- Bad input → `ValueError` with the bad spec in the message.
- Test uses `freezegun` or direct `now=` injection (prefer `now=` to avoid adding a dep).

`tests/test_report_format.py`:
- `format_duration(0)` → `"0s"`.
- `format_duration(47)` → `"47s"`.
- `format_duration(60)` → `"1m"`, `format_duration(2820)` → `"47m"`.
- `format_duration(15120)` → `"4h12m"`.
- `format_duration(183600)` → `"2d3h"`.
- `format_cost(None)` → `"$—"` (em-dash U+2014).
- `format_cost(0.0)` → `"$0.00"`.
- `format_cost(1.234)` → `"$1.23"` (round-half-even per Python `round()`; call out if it matters).

- [ ] **Step 2: Implement `resolver.py`**

```python
class AmbiguousPrefix(Exception):
    def __init__(self, prefix: str, matches: list[str]) -> None:
        super().__init__(f"{prefix!r} matches {len(matches)} sessions: {matches}")
        self.prefix = prefix
        self.matches = matches


class SessionNotFound(Exception):
    def __init__(self, spec: str) -> None:
        super().__init__(f"no session matches {spec!r}")
        self.spec = spec


def resolve_session_id(spec: str, conn: sqlite3.Connection) -> str:
    if spec.endswith(".jsonl") and os.path.isabs(spec):
        row = conn.execute(
            "SELECT session_id FROM files WHERE path=?", (spec,)
        ).fetchone()
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
```

- [ ] **Step 3: Implement `_dates.py`**

```python
from datetime import UTC, date, datetime, time, timedelta

_N_DAYS_RE = re.compile(r"^(\d+)d$")


def _parse_token(spec: str, now: datetime) -> date:
    spec = spec.strip().lower()
    if spec == "today":
        return now.date()
    if spec == "yesterday":
        return now.date() - timedelta(days=1)
    m = _N_DAYS_RE.match(spec)
    if m:
        return now.date() - timedelta(days=int(m.group(1)))
    try:
        return date.fromisoformat(spec)
    except ValueError as e:
        raise ValueError(f"unrecognized date spec: {spec!r}") from e


def parse_since(spec: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    d = _parse_token(spec, now)
    return datetime.combine(d, time.min, tzinfo=UTC)


def parse_until(spec: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    d = _parse_token(spec, now)
    return datetime.combine(d, time.max, tzinfo=UTC)
```

- [ ] **Step 4: Implement `_format.py`**

```python
_EM_DASH = "—"


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, m = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{m:02d}m" if m else f"{hours}h"
    days, h = divmod(hours, 24)
    return f"{days}d{h}h" if h else f"{days}d"


def format_cost(cost_usd: float | None) -> str:
    if cost_usd is None:
        return f"${_EM_DASH}"
    return f"${cost_usd:.2f}"
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_report_resolver.py tests/test_report_dates.py tests/test_report_format.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/ccforensics/report/ tests/test_report_*.py
git commit -m "feat(report): shared resolver, date, and format helpers"
```

---

### Task M4.3: `export.py` — shared JSON/CSV writers

**Files:**
- Create: `src/ccforensics/export.py`
- Create: `tests/test_export.py`

- [ ] **Step 1: Write failing tests**

`tests/test_export.py`:
- `write_json` output is `json.loads`-round-trippable, stable key order, UTF-8, indent=2, `ensure_ascii=False` (emoji survive).
- `write_csv` output parses with stdlib `csv.reader`, header row matches given headers in order, missing keys → empty string.
- `write_csv` with no rows still emits the header row.
- Round-trip: serialize then re-parse yields equivalent data.
- Use syrupy for snapshot of a representative session-list payload.

- [ ] **Step 2: Implement**

```python
from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from typing import Any, TextIO


def write_json(data: Any, out: TextIO) -> None:
    json.dump(data, out, indent=2, ensure_ascii=False, sort_keys=False)
    out.write("\n")


def write_csv(rows: Iterable[Mapping[str, Any]], headers: list[str], out: TextIO) -> None:
    writer = csv.DictWriter(out, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({h: _csv_cell(row.get(h)) for h in headers})


def _csv_cell(v: Any) -> str:
    if v is None:
        return ""
    return str(v)
```

Note: `sort_keys=False` because caller controls key order (stable schemas matter more than alphabetical).

- [ ] **Step 3: Run tests**

```bash
uv run pytest tests/test_export.py -v
```

- [ ] **Step 4: Commit**

```bash
git add src/ccforensics/export.py tests/test_export.py
git commit -m "feat(export): shared JSON/CSV writers"
```

---

### Task M4.4: `report/session_list.py` — query, filter, sort, render

**Files:**
- Create: `src/ccforensics/report/session_list.py`
- Create: `tests/test_report_session_list.py`

- [ ] **Step 1: Write failing tests**

`tests/test_report_session_list.py` covers:
- `query_session_list` with no filters returns all sessions from `session_summaries`, sorted by `last_active_at DESC`.
- `sort_key='cost'` sorts by `total_cost_usd DESC` (NULLs last).
- `sort_key='started'`, `'turns'` variants.
- `reverse=True` flips order.
- `limit=N` caps rows.
- `project='X'` filters via `project_path LIKE '%X%'` (case-insensitive).
- `grep='foo'` filters via `summary_text` case-insensitive substring.
- `since=dt1, until=dt2` filters on `last_active_at`.
- `shorten_session_ids`:
  - All unique at 6 chars → all returned at 6.
  - Two sessions share 6-char prefix → both extend to 7 (or more if still colliding).
  - Empty input → `{}`.
- `render_session_list(rows)` returns a `rich.Table` with expected column set and row count.
- `render_session_list(rows, verbose=True)` adds the source-badge column.
- Summary column is multi-line (no truncation): a 500-char summary remains intact in the cell's text.

- [ ] **Step 2: Implement `SessionListRow` + query**

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

SortKey = Literal["cost", "started", "last-active", "turns"]
_SORT_COLUMN: dict[SortKey, str] = {
    "cost": "total_cost_usd",
    "started": "started_at",
    "last-active": "last_active_at",
    "turns": "turn_count",
}


@dataclass
class SessionListRow:
    session_id: str
    project_path: str | None
    project_display: str | None
    started_at: int
    last_active_at: int
    duration_s: int
    turn_count: int
    total_cost_usd: float | None
    summary_text: str | None
    summary_source: str | None


def query_session_list(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    grep: str | None = None,
    sort_key: SortKey = "last-active",
    reverse: bool = False,
    limit: int | None = None,
) -> list[SessionListRow]:
    where: list[str] = []
    params: list[object] = []
    if project:
        where.append("LOWER(IFNULL(project_path,'')) LIKE ?")
        params.append(f"%{project.lower()}%")
    if since:
        where.append("last_active_at >= ?")
        params.append(int(since.timestamp()))
    if until:
        where.append("last_active_at <= ?")
        params.append(int(until.timestamp()))
    if grep:
        where.append("LOWER(IFNULL(summary_text,'')) LIKE ?")
        params.append(f"%{grep.lower()}%")

    col = _SORT_COLUMN[sort_key]
    # NULLs always sort last (both DESC default and ASC under --reverse).
    # Rationale: "cheapest first" shouldn't put unknown-cost rows at the top.
    direction = "ASC" if reverse else "DESC"
    sql = (
        "SELECT session_id, project_path, project_display, started_at, "
        "last_active_at, duration_s, turn_count, total_cost_usd, "
        "summary_text, summary_source FROM session_summaries"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {col} IS NULL, {col} {direction}, session_id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql, params)
    return [SessionListRow(*row) for row in cur.fetchall()]
```

- [ ] **Step 3: Implement `shorten_session_ids`**

```python
def shorten_session_ids(session_ids: list[str], min_chars: int = 6) -> dict[str, str]:
    ids = list(dict.fromkeys(session_ids))  # de-dup, preserve order
    if not ids:
        return {}
    lengths: dict[str, int] = {sid: min_chars for sid in ids}
    while True:
        seen: dict[str, list[str]] = {}
        for sid in ids:
            key = sid[: lengths[sid]]
            seen.setdefault(key, []).append(sid)
        collisions = [group for group in seen.values() if len(group) > 1]
        if not collisions:
            return {sid: sid[: lengths[sid]] for sid in ids}
        for group in collisions:
            for sid in group:
                lengths[sid] = min(len(sid), lengths[sid] + 1)
```

- [ ] **Step 4: Implement `render_session_list` with `rich.Table`**

```python
from rich.table import Table
from rich.text import Text

from .._format import format_cost, format_duration


_SOURCE_BADGE = {
    "claude-summary": "[C]",
    "first-prompt": "[F]",
    "none": "[-]",
}


def render_session_list(rows: list[SessionListRow], *, verbose: bool = False) -> Table:
    table = Table(show_lines=False, expand=True, pad_edge=False)
    short_ids = shorten_session_ids([r.session_id for r in rows])
    table.add_column("UUID", no_wrap=True)
    table.add_column("Project", no_wrap=True, max_width=30)
    table.add_column("Started", no_wrap=True)
    table.add_column("Dur", no_wrap=True, justify="right")
    table.add_column("Turns", no_wrap=True, justify="right")
    table.add_column("Cost", no_wrap=True, justify="right")
    if verbose:
        table.add_column("Src", no_wrap=True)
    table.add_column("Summary", overflow="fold")

    for r in rows:
        started = datetime.fromtimestamp(r.started_at, tz=UTC).strftime("%Y-%m-%d %H:%M")
        cells = [
            short_ids[r.session_id],
            (r.project_display or "")[:30],
            started,
            format_duration(r.duration_s),
            str(r.turn_count),
            format_cost(r.total_cost_usd),
        ]
        if verbose:
            cells.append(_SOURCE_BADGE.get(r.summary_source or "none", "[-]"))
        cells.append(Text(r.summary_text or "<no summary available>"))
        table.add_row(*cells)
    return table
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_report_session_list.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/ccforensics/report/session_list.py tests/test_report_session_list.py
git commit -m "feat(report): session_list query + rich render"
```

---

### Task M4.5: CLI wiring + integration test + SC5 verification

**Files:**
- Modify: `src/ccforensics/cli.py`
- Modify: `tests/test_cli.py`
- Create: `docs/sc5-verification.md`

- [ ] **Step 1: Write failing CLI integration tests**

Append to `tests/test_cli.py`:

```python
def test_session_list_no_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(cli_main, ["session", "list", "--no-refresh"])
    assert result.exit_code == 0


def test_session_list_json_export(tmp_path, monkeypatch):
    # Build a synthetic corpus, index it, dump --json, parse output.
    ...


def test_session_list_csv_export(tmp_path, monkeypatch):
    ...


def test_session_list_grep_filter(tmp_path, monkeypatch):
    ...
```

At least one test should write a real JSONL into the fixture projects dir, run `index rebuild --force --yes`, then `session list --json`, and assert the expected session appears with correct fields.

- [ ] **Step 2: Implement the CLI command**

Replace the `session_list` stub in `src/ccforensics/cli.py`:

```python
import sys

from rich.console import Console

from .export import write_csv, write_json
from .report._dates import parse_since, parse_until
from .report.session_list import query_session_list, render_session_list


@session.command("list")
@click.option("--project", help="Filter by project path substring (case-insensitive).")
@click.option("--since", help="Date filter: YYYY-MM-DD | Nd | today | yesterday")
@click.option("--until", help="Date filter: YYYY-MM-DD | Nd | today | yesterday")
@click.option("--grep", help="Case-insensitive substring on summary text.")
@click.option(
    "--sort", "sort_key",
    type=click.Choice(["cost", "started", "last-active", "turns"]),
    default="last-active",
)
@click.option("--reverse", is_flag=True, help="Reverse the sort order.")
@click.option("--limit", type=int, help="Cap the number of rows.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON to stdout.")
@click.option("--csv", "as_csv", is_flag=True, help="Emit CSV to stdout.")
@click.option("--no-refresh", is_flag=True, help="Skip index reconciliation.")
@click.pass_context
def session_list_cmd(
    ctx, project, since, until, grep, sort_key, reverse, limit, as_json, as_csv, no_refresh
):
    """List all discoverable sessions."""
    if as_json and as_csv:
        raise click.UsageError("--json and --csv are mutually exclusive")

    db = _index_db_path()
    conn = open_connection(db)
    ensure_schema(conn)

    if not no_refresh:
        pricing = PricingCache(cache_file=ccforensics_cache_dir() / "litellm.json").load_or_fetch()
        reconcile_projects_dir(conn, claude_projects_dir(), pricing)
        conn.commit()

    since_dt = parse_since(since) if since else None
    until_dt = parse_until(until) if until else None

    rows = query_session_list(
        conn,
        project=project,
        since=since_dt,
        until=until_dt,
        grep=grep,
        sort_key=sort_key,
        reverse=reverse,
        limit=limit,
    )

    if as_json:
        payload = [dataclasses.asdict(r) for r in rows]
        write_json(payload, sys.stdout)
        return
    if as_csv:
        headers = [
            "session_id", "project_path", "project_display",
            "started_at", "last_active_at", "duration_s",
            "turn_count", "total_cost_usd", "summary_text", "summary_source",
        ]
        write_csv((dataclasses.asdict(r) for r in rows), headers, sys.stdout)
        return

    Console().print(render_session_list(rows, verbose=ctx.obj.get("verbose", False)))
```

Data goes to stdout, warnings to stderr (`Console(stderr=True)` for the reconcile progress line, if added).

- [ ] **Step 3: Run unit + integration tests + lint + typecheck**

```bash
uv run pytest -x
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
```

- [ ] **Step 4: SC5 manual verification**

Pick a remembered past session. Time how long it takes to locate via:

```bash
time uv run ccforensics session list --sort last-active --limit 20
```

Then pipe to grep:
```bash
uv run ccforensics session list --no-refresh --grep "<keyword from memory>"
```

Write results to `docs/sc5-verification.md`:
- Date of test.
- Remembered session (one-line description, no sensitive content).
- Time to identify session via `--list` scan.
- Time to identify via `--grep`.
- SC5 pass/fail (≤30s target).
- Any summary-extraction surprises — tune if fallback chain picked a bad candidate.

- [ ] **Step 5: Commit**

```bash
git add src/ccforensics/cli.py tests/test_cli.py docs/sc5-verification.md
git commit -m "feat(cli): session --list with filter, sort, export"
```

**M4 exit criteria:**
- [x] `session --list` runs on real corpus and renders a recognizable table.
- [x] JSON + CSV exports parse cleanly.
- [x] All filters work: `--project`, `--since`, `--until`, `--grep`.
- [x] Sort + reverse + limit verified.
- [x] SC5 documented in `docs/sc5-verification.md` with pass/fail — PASS (~3s end-to-end).
- [x] CI green. Coverage ≥85% (actual: 95%).

---

## Milestone M4.6 — Post-M4 Sanitization + Help Text Polish

**Context:** M4.5 SC5 verification surfaced two small but concrete gaps that should land before M5 opens. Neither requires design work; both are in-place tweaks.

**Observations (from M4.5 SC5 run + subagent reports):**
1. `_sanitize_prompt` in `src/ccforensics/index.py` strips `<command-name>`, `<command-message>`, `<command-args>` wrappers, but NOT `<local-command-caveat>`, `<local-command-stdout>`, `<bash-input>`, `<bash-stdout>`, `<bash-stderr>`. These appear verbatim in real-corpus summaries, making them noisy. Sample: session `65c788` shows "Set model to Opus 4.7" as its summary (which is the stdout of a `/model` slash-command, not a useful first-prompt). Dozens of sessions show `<bash-input>cd ..</bash-input>`-style summaries that would be better filtered.
2. `--grep` searches `summary_text` only (by spec). Users may expect it to match project names; when they don't, a user types `--grep ccforensics` and gets zero hits on sessions that are about ccforensics. Fix is documentation only: tighten the option's `help=` text to say it searches summary text, and suggest `--project` for project-path matching.

**Exit:** Real-corpus `session --list --sort last-active --limit 20` shows summaries free of bash/command wrapper noise; `--grep` help text is explicit about its scope.

### Task M4.6.1: Extend sanitization to strip local-command + bash wrappers

**Files:**
- Modify: `src/ccforensics/index.py`
- Modify: `tests/test_session_summaries.py`

- [ ] **Step 1: Read the current sanitizer**

`src/ccforensics/index.py` currently has two regexes for sanitization:

```python
_COMMAND_WRAPPER_RE = re.compile(
    r"<command-(name|message|args)>.*?</command-\1>",
    re.DOTALL,
)
_IDE_ATTACHMENT_RE = re.compile(
    r"<ide[^>]*>.*?<file[^>]*>(?P<path>[^<]+)</file>.*?</ide[^>]*>",
    re.DOTALL,
)
```

Extend the wrapper regex (or add a sibling) so the following real-corpus tags also get stripped entirely: `<local-command-caveat>…</local-command-caveat>`, `<local-command-stdout>…</local-command-stdout>`, `<local-command-stderr>…</local-command-stderr>`, `<bash-input>…</bash-input>`, `<bash-stdout>…</bash-stdout>`, `<bash-stderr>…</bash-stderr>`.

**Why strip, not preserve:** these wrappers surround content Claude emitted or captured as part of tool execution, not user intent. The goal of the "first-prompt" summary is to reflect the user's actual first message — bash output and slash-command stdout are noise in that context.

- [ ] **Step 2: Write failing tests**

Add to `tests/test_session_summaries.py`:

```python
def test_sanitize_strips_local_command_wrappers() -> None:
    raw = "<local-command-caveat>Caveat: ...</local-command-caveat>Do the thing"
    assert _sanitize_prompt(raw) == "Do the thing"

    raw = "<local-command-stdout>Set model to Opus 4.7</local-command-stdout>"
    assert _sanitize_prompt(raw) == ""  # whole prompt was wrapped

    raw = "<local-command-stderr>err</local-command-stderr>actual prompt"
    assert _sanitize_prompt(raw) == "actual prompt"


def test_sanitize_strips_bash_wrappers() -> None:
    raw = "<bash-input>cd ..</bash-input>"
    assert _sanitize_prompt(raw) == ""

    raw = "<bash-stdout>hello</bash-stdout><bash-stderr>warn</bash-stderr>actual"
    assert _sanitize_prompt(raw) == "actual"


def test_sanitize_composite_real_corpus_shape() -> None:
    """Realistic shape seen in real-corpus sessions."""
    raw = (
        "<local-command-caveat>Caveat: The messages below were generated...</local-command-caveat>"
        "<bash-input>pwd</bash-input>"
        "<bash-stdout>/Users/x</bash-stdout>"
    )
    assert _sanitize_prompt(raw) == ""
```

The expectation "whole prompt was wrapped → empty string" is important: these summaries should fall through to the next candidate in the priority chain, not render as an empty string. The caller (`_extract_summary`) should treat empty-after-sanitize the same as no-text-block, skipping to the next user prompt. Verify this fall-through behavior has a test, too:

```python
def test_extract_summary_falls_through_empty_sanitized_prompt(tmp_path):
    """A session whose first 'user' message is entirely bash wrappers
    should fall through to the second user message if it has real text."""
    # Build a synthetic session with: msg1 (bash-input wrapper only),
    # msg2 (real prompt). Reconcile. Assert summary_source='first-prompt'
    # and summary_text reflects msg2.
```

- [ ] **Step 3: Extend the regex**

Approach A (extend existing): expand `_COMMAND_WRAPPER_RE` to an alternation over all the wrapper types. Likely cleaner:

```python
_COMMAND_WRAPPER_RE = re.compile(
    r"<(command-(?:name|message|args)|local-command-(?:caveat|stdout|stderr)|bash-(?:input|stdout|stderr))>.*?</\1>",
    re.DOTALL,
)
```

Approach B (second regex): add a new `_LOCAL_WRAPPER_RE` and apply it in `_sanitize_prompt` before the existing one. Two passes, simpler to reason about:

```python
_LOCAL_WRAPPER_RE = re.compile(
    r"<(local-command-(?:caveat|stdout|stderr)|bash-(?:input|stdout|stderr))>.*?</\1>",
    re.DOTALL,
)

def _sanitize_prompt(text: str) -> str:
    text = _LOCAL_WRAPPER_RE.sub("", text)
    text = _COMMAND_WRAPPER_RE.sub("", text)
    text = _IDE_ATTACHMENT_RE.sub(lambda m: f"📎 {m.group('path').strip()}", text)
    text = " ".join(text.split())
    return text[:1000]
```

Pick whichever reads cleaner. Approach B is more maintainable.

- [ ] **Step 4: Handle empty-after-sanitize fall-through**

In `_extract_summary` (or wherever the first-prompt selection loop lives in `index.py`), after `_sanitize_prompt`, check for empty string. If empty, `continue` to the next user prompt. The current code already does this when `_first_text_block` returns None; extend to cover empty post-sanitize too.

- [ ] **Step 5: Run tests + smoke-verify**

```bash
uv run pytest tests/test_session_summaries.py -v
uv run ccforensics index rebuild --force --yes
sqlite3 ~/.cache/ccforensics/index.sqlite \
  "SELECT substr(summary_text,1,80), COUNT(*) FROM session_summaries
   WHERE summary_text LIKE '<%' GROUP BY substr(summary_text,1,80)
   ORDER BY 2 DESC LIMIT 10"
# Expected: no rows, or rows that clearly aren't the local-command/bash wrappers.
```

Also spot-check that previously-noisy sessions now have readable summaries:

```bash
sqlite3 ~/.cache/ccforensics/index.sqlite \
  "SELECT substr(session_id,1,6), summary_source, substr(summary_text,1,100)
   FROM session_summaries ORDER BY last_active_at DESC LIMIT 20"
```

- [ ] **Step 6: Commit**

```bash
git commit -m "fix(index): strip local-command and bash wrappers from summaries"
```

---

### Task M4.6.2: Tighten `--grep` help text

**Files:**
- Modify: `src/ccforensics/cli.py`
- Modify: `tests/test_cli.py` (optional — only if an existing test asserts help text)

- [ ] **Step 1: Update the help string**

Current: `@click.option("--grep", help="Case-insensitive substring on summary text.")`

Updated:

```python
@click.option(
    "--grep",
    help=(
        "Case-insensitive substring on summary text. "
        "Does NOT match project paths or session IDs — use --project for project filtering."
    ),
)
```

- [ ] **Step 2: Commit**

```bash
git commit -m "docs(cli): clarify --grep scope in help text"
```

**M4.6 exit criteria:**
- [ ] Sanitizer strips `<local-command-*>` and `<bash-*>` wrappers; tests cover each.
- [ ] Empty-after-sanitize falls through to the next user prompt.
- [ ] Real-corpus smoke shows no `<local-command-*>`- or `<bash-*>`-prefixed summaries.
- [ ] `--grep` help text explicit about scope.
- [ ] CI green. Coverage ≥85%.

---

## Milestone M5 — Tree Reconstruction (Outline)

*Full task expansion to be written when M4 exits.*

**Objective:** Reconstruct parent→child tool-use graph within sessions and link parent→child subagent files. Populate `subagent_spawns` table. Attribute each message to `main` | `subagent:<type>` | `unattributed` bucket. Populate `session_rollups` for the main bucket categorization.

**Modules to create:**
- `src/ccforensics/tree.py`
- `src/ccforensics/attribution.py`

**Key subtasks:**
1. Within-session graph via `messages.tool_use_id` → `messages.source_tool_use_id`.
2. Subagent dir discovery: find sibling `.meta.json`, parse `{agentType, description}`.
3. Spawn linkage heuristic: nearest-before `tool_use name=Agent|Task` within same session, tie-break by `meta.json::agentType == input.subagent_type`, final tie-break by nearest timestamp.
4. Unresolvable spawn → log + row with `parent_message_dedup_key=NULL`; cost → `unattributed`.
5. Recursive cost rollup (subagent inside subagent).
6. Invariant test: `sum(buckets) == session_total` within 0.01%.
7. `session_rollups` population.

**New tests:**
- `tests/test_tree.py`: basic within-session graph, cross-file spawn linkage, multi-candidate tie-break, unresolvable spawn.
- `tests/test_attribution.py`: bucket classification, cost reconciliation invariant under synthetic fixtures with known totals.

---

## Milestone M6 — Plugin Registry (Outline)

*Full task expansion to be written when M5 exits.*

**Objective:** Scan `~/.claude/plugins/cache/*/` and user-level `~/.claude/{skills,agents}/`. Build `plugins` + `user_level_artifacts` tables. Roll `subagent:<type>` cost up to owning plugin in `session_rollups`. Emit one-time warning on skill-name collisions across locations.

**Modules to create:**
- `src/ccforensics/registry.py`

**Key subtasks:**
1. Walk `~/.claude/plugins/cache/<plugin>/[version]/.claude-plugin/plugin.json`.
2. Parse each manifest; index `agents/` dir for agent definitions; index `skills/` for skill paths.
3. Walk `~/.claude/skills/<name>/SKILL.md` and `~/.claude/agents/<name>.md`.
4. Build `agent-name → plugin` lookup; `user-level` when unowned; `builtin` for `general-purpose`/`Explore`/`Plan`/`statusline-setup`.
5. Collision detection: same skill name in both user-level and any plugin → warning listing paths.
6. `session_rollups` plugin-level population.

**New tests:**
- `tests/test_registry.py`: manifest parsing, user-level scan, collision warning (use real layout with `jlixfeld-claude-skills:pr-review-loop` vs `~/.claude/skills/pr-review-loop`).

---

## Milestone M7 — `session <id>` Deep Report (Outline)

*Full task expansion to be written when M6 exits.*

**Objective:** Full per-session report rendering five sections (header, cost by bucket, cost by plugin, skill ledger stub, parse notes). `--json` + `--csv` export. SC1 verified on the 2026-04-16 $109 session.

**Modules to create:**
- `src/ccforensics/report/session.py`

**Key subtasks:**
1. UUID-prefix resolver → full session ID.
2. Render header: dates, models, total, (± band once M8 lands).
3. Render bucket table from `session_rollups`.
4. Render plugin rollup.
5. Render unattributed explanation with `--include-unattributed` expansion.
6. Parse-notes section (schema_version, warning counts).
7. `--json` / `--csv` shapes; `docs/schema.md`.

**New tests:**
- `tests/test_report_session.py`: integration test that synthesizes a corpus with known subagent + plugin mix, invokes `ccforensics session <id> --json`, snapshot-compares the shape.

**Manual verification:** SC1 — run against 2026-04-16 session; eyeball totals and plugin list.

---

## Milestone M8 — Skills + Context-Carry ± (Outline)

*Full task expansion to be written when M7 exits.*

**Objective:** Detect skill activations via 3 channels. Estimate context-carry ± band. Correlate with downstream spawns. Render skill ledger in `session` report. SC2 + SC3 verified.

**Modules to create:**
- `src/ccforensics/skills.py`

**Key subtasks:**
1. Channel A: `tool_use.name == "Skill"` in assistant messages.
2. Channel B: `tool_use.name == "Read"` + `input.file_path` ends in `/skills/<name>/SKILL.md`.
3. Channel C: parse `attachment.stdout` for `SessionStart` hook → `hookSpecificOutput.additionalContext` → frontmatter regex.
4. Skill → plugin resolution via path (never name).
5. Content-size measurement (disk file size or `len(additionalContext)`).
6. ± band computation: lower = evicts on first compaction; upper = survives all compactions; point = midpoint. Compact detection: large `cache_read` drop or `isCompactSummary=true`.
7. Downstream spawn correlation within N turns (default 5).
8. Skill ledger rendering in `session <id>` output; footnotes collision (subagent-name shared).

**New tests:**
- `tests/test_skills.py`: each channel in isolation; bootstrap-content heuristic; frontmatter parsing; ± band math on a synthetic session with known compactions.
- `tests/integration/test_session_with_skills.py`: full synthetic corpus including hook injection + Read-load + Skill-tool activations.

**Manual verification:** SC2 + SC3 — 2026-04-16 session report shows Superpowers hook at turn 1, plausible context-carry estimates, spawn correlations.

---

## Milestone M9 — `aggregate` + `plugins` Commands (Outline)

*Full task expansion to be written when M8 exits.*

**Objective:** Ship `ccforensics aggregate` with `--group-by` dimensions and `ccforensics plugins` all-time rollup. JSON/CSV exports.

**Modules to create:**
- `src/ccforensics/report/aggregate.py`
- `src/ccforensics/report/plugins.py`

**Key subtasks:**
1. `aggregate` date-range filter applied to `session_summaries.last_active_at`.
2. `--group-by none|project|day|week|month|plugin`.
3. `plugins` command pulling from `session_rollups` where `bucket_kind='plugin'`, joined with `plugins` table.
4. Columns: plugin, total cost, session count, most-used subagent-type, most-used skill, first-seen, last-seen.

**New tests:**
- `tests/test_report_aggregate.py`, `tests/test_report_plugins.py`.

---

## Milestone M10 — Polish + v0.1.0 (Outline)

*Full task expansion to be written when M9 exits.*

**Objective:** README, manual pre-release pass, SC4 verified, v0.1.0 tag, `uv tool install` end-to-end.

**Subtasks:**
1. `README.md`: install (`uv tool install git+https://github.com/jlixfeld/ccforensics@v0.1.0`), quick start, each command with example output, JSON/CSV export schema link.
2. Manual validation pass (spec §6.5): `index --rebuild`, `session --list` scan, 3 remembered sessions, `plugins` rollup, 30d `aggregate`.
3. SC4 verification: run reports, identify 2 actionable workflow changes. If the reports don't support the conclusion, re-open the relevant milestone.
4. `CHANGELOG.md` for v0.1.0.
5. **UX polish: narrow-terminal `session --list` rendering.** M4.5 observation: on 80-column terminals, rich's `overflow="fold"` on the Summary column folds text to ~3 chars of usable width (the other columns consume the rest). Options: detect `Console.width < N` and switch to a compact mode (truncate summary with `…`); or detect non-TTY stdout and emit plain text instead of a rich table; or drop the Project column when narrow. Pick one, implement, test at three widths (60, 100, 200 cols).
6. PR `feature/initial-implementation` → `main`. Request review via `/pr-quality` per CLAUDE.md.
7. Squash-merge, tag `v0.1.0`, push tag.
8. End-to-end test: `uv tool install git+https://github.com/jlixfeld/ccforensics@v0.1.0` in a fresh shell, run `ccforensics --help`.

---

## Self-Review

### Spec coverage check

| Spec requirement | Task(s) |
|---|---|
| Parse `~/.claude/projects/**/*.jsonl` | M1.3, M3.3 |
| Dedup by `(message.id, requestId)` | M1.4 |
| Tree via `parentToolUseId`/`sourceToolUseID`/`agentId` | M5 (outline) |
| Per-message cost + per-node rollup | M2.3, M5, M7 |
| Per-subagent-type aggregates | M5, M9 |
| Plugin discovery + rollup | M6 |
| Skill detection (3 channels) | M8 |
| Cache-read ± band | M8 |
| Downstream-spawn correlation | M8 |
| `session --list` | M4 |
| `session <id>` deep report | M7 |
| `aggregate` with date range | M9 |
| `plugins` all-time | M9 |
| JSON + CSV export | M4, M7, M9 |
| Defensive parser + version tolerance | M1.2, M1.3 |
| LiteLLM pricing w/ offline fallback | M2.2 |
| SQLite index (don't rely on sessions-index.json) | M3.1–M3.3 |
| Invariant: no double-attribution | M5 tests |
| SC1–SC6 | M7 (SC1), M8 (SC2+SC3), M10 (SC4), M4 (SC5), M7 (SC6) |

No gaps.

### Placeholder scan

Full task detail sections (M0–M3) contain no placeholders. Later milestones are explicitly labeled "(Outline)" with the expansion policy stated in §"Plan Decomposition". This is a deliberate scoping decision — not a TBD.

### Type consistency check

- `TranscriptEntry` defined M1.2; referenced consistently in M1.3, M1.4, M2.3, M3.2.
- `AnnotatedEntry` defined M2.3 with fields `entry`, `cost_usd`, `pricing_unresolved_model` — used same in M3.2.
- `dedup_key` / `dedup_entries` defined M1.4, same signatures in M3.2.
- `ModelPrice` / `PricingCache` / `resolve_pricing` / `compute_message_cost` defined M2.2, referenced same in M2.3 and M3.2.
- `open_connection`, `ensure_schema`, `reconcile_file`, `reconcile_projects_dir`, `collect_stats`, `IndexStats`, `ReconcileStats` defined M3.1–M3.4, referenced same in M3.4 CLI wiring.

Consistent.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-04-21-initial-implementation.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task with two-stage review between tasks. Fast iteration, good for M0–M3 where tasks are tightly scoped.
2. **Inline Execution** — execute tasks in this session via `superpowers:executing-plans`, batched with checkpoints for review.

Which approach do you want?
