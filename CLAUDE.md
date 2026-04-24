# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ccforensics` is a CLI that parses `~/.claude/projects/**/*.jsonl` and attributes every message's cost to exactly one bucket — `main`, `subagent:<type>`, `auto-compact`, or `unattributed` — so a user can tell which plugins/skills/subagents are driving their Claude Code spend. See `docs/specs/design.md` for the normative design and `docs/specs/problem-statement.md` for motivation.

## Commands

All development uses `uv` (never `pip`). Python 3.13 required.

```bash
uv sync --all-extras --dev                              # install deps
uv run pytest                                           # full test suite
uv run pytest tests/test_attribution.py::test_foo       # single test
uv run pytest --cov=ccforensics --cov-report=term-missing --cov-fail-under=85  # CI coverage gate
uv run ruff check                                       # lint
uv run ruff format --check                              # format check
uv run mypy src/                                        # strict type check (configured in pyproject.toml)
uv run ccforensics --help                               # run the CLI locally
```

CI runs all five checks (ruff check, ruff format --check, mypy, pytest with ≥85% coverage) on Ubuntu + macOS with Python 3.13.

## macOS + iCloud workaround — run after every `uv sync`

This repo lives under `~/Documents/` which is iCloud-synced. iCloud sets `UF_HIDDEN` on uv-installed `.pth` files, and `site.py` silently skips hidden `.pth`s, breaking `import ccforensics` at test collection and CLI invocation. Fix after any `uv sync`, `uv sync --reinstall`, or `rm -rf .venv`:

```bash
chflags nohidden .venv/lib/python3.*/site-packages/*.pth
```

Do NOT paper over this with `pythonpath` in pyproject.toml or `dev-mode-exact = true` — CI does not have this problem and we want local config to match CI. Full context: `.claude/env_pth_hidden_flag.md`. `tests/conftest.py` prepends `src/` to `sys.path` as defense-in-depth.

## Architecture

The data flow is **filesystem → JSONL parser → SQLite index → reports**. The SQLite index lives at `~/.cache/ccforensics/index.sqlite` and is incrementally reconciled by `(path, mtime_ns, size)`.

### Reconcile pipeline (`src/ccforensics/index.py::reconcile_projects_dir`)

For every `*.jsonl` under `~/.claude/projects/` (sorted by string so `<enc>/<sess>.jsonl` is processed **before** `<enc>/<sess>/subagents/*.jsonl` — subagent spawn discovery needs the parent already indexed):

1. `_row_is_unchanged` — skip if `(mtime_ns, size)` match.
2. `_classify_file` — classify into `main` | `subagent` | `auto-compact` from filename pattern.
3. `reconcile_file` — purge stale rows, `jsonl.parse_file` → `jsonl.annotate_cost` (via `pricing.resolve_pricing`) → dedup → bulk `INSERT OR REPLACE INTO messages`.
4. `_reconcile_spawn` (subagents only) — load parent session entries, call `tree.discover_spawn` to link to the parent Agent/Task tool_use, then write `subagent_spawns` row. Parent linkage is recomputed via `dedup_key` of the raw parent entry, not by `uuid`/`tool_use_id` — because parallel Agent tool_uses in one LLM response share a `dedup_key` and the messages column only holds one `tool_use_id`.
5. Commit per file (mid-walk interrupt only drops the in-flight file).

After the walk: `registry.populate_registry` (plugin + user-level artifact scan), then for each touched session `recompute_session_summary` + `attribution.recompute_session_rollups` + `attribution.backfill_spawn_totals` + `skills.populate_from_session_files`.

### Attribution model (`src/ccforensics/attribution.py`)

The bucket decision is a single SQL CASE over `files.kind` and `subagent_spawns.parent_message_dedup_key`. **Hard invariant:** `sum(session_rollups.cost_usd) == sum(messages.cost_usd)` per session, tolerance `1e-6` — verified by `verify_invariant` and asserted in `test_attribution.py`. Don't add a code path that can route cost to more than one bucket or drop it entirely.

- `main`: primary session file.
- `subagent:<type>`: subagent file **and** `subagent_spawns` has a resolved `parent_message_dedup_key` + `subagent_type`.
- `auto-compact`: `agent-acompact-*.jsonl` — real billable cost from Claude Code's context-compaction worker, explicitly bucketed, not routed to `unattributed`.
- `unattributed`: subagent file whose parent Agent/Task call couldn't be resolved (~0.5% on real corpus).

### Dedup policy (`src/ccforensics/jsonl.py`)

Dedup key tiers (prefix prevents cross-tier collisions):

1. `req:<message.id>:<requestId>` — billing-accurate.
2. `session:<message.id>:<sessionId>` — fallback.
3. `None` — passed through un-deduped.

On collision the content-richest entry wins (`_dedup_preference`): `tool_use` block present > more non-empty content blocks > later timestamp. This matters because Claude Code sometimes writes the same LLM response twice (streamed text first, then tool_use on resolve); token usage is identical so cost is unaffected, but keeping the tool_use-bearing copy is load-bearing for cross-file spawn discovery in `tree.py`.

### Module responsibilities

- `models.py` — permissive pydantic schemas (`extra="allow"`) for transcript entries. Unknown fields preserved in `.extras`. `parse_entry` normalizes legacy field names (`parentToolUseId` → `sourceToolUseID`) and wraps bare-string `message.content` into a single text block.
- `jsonl.py` — streaming parser with a 16 MB line cap and truncated-tail detection; dedup; cost annotation.
- `pricing.py` — LiteLLM fetch (24h cache, 10 MB cap, `platformdirs`), fuzzy model resolver, hardcoded fallback for current Claude models.
- `tree.py` — within-session tool-use graph + `discover_spawn` nearest-before Agent|Task heuristic with `(type_match, timestamp)` composite rank.
- `index.py` — schema migrations (via `PRAGMA user_version`), reconcile orchestration, summary extraction priority chain.
- `attribution.py` — bucket SQL + invariant verifier.
- `registry.py` — `~/.claude/plugins/cache/<market>/<plugin>/<version>/` scan; multiple versions collapse to highest semver; user-level artifacts from `~/.claude/{skills,agents}/`. `BUILTIN_AGENTS` is the frozenset of Claude Code's built-ins.
- `skills.py` — activation detection via three channels (Skill tool call, Read of SKILL.md, SessionStart hook injection with frontmatter fallback). Anchored to full SKILL.md path so plugin vs. user-level name collisions don't confuse attribution.
- `report/` — query + render modules (`session.py`, `session_list.py`, `aggregate.py`, `plugins.py`, `resolver.py` for session-id prefix/path resolution, `_dates.py` for `Nd`/`YYYY-MM-DD`/`today`/`yesterday` parsing, `_format.py` for shared formatting).
- `cli.py` — click command tree. Every report command refreshes the index first by default (`--no-refresh` to skip), and `--json`/`--csv` are mutually exclusive.
- `paths.py` — `~/.claude` + `platformdirs` path resolution; `encode_project_path` / `decode_project_dirname` (**decode is lossy** for paths containing `-`; prefer the `cwd` field from JSONL when available).

## Test fixtures

Real session JSONL in `tests/fixtures/real/redacted_session.jsonl` is redacted via `scripts/redact_jsonl.py` before committing — the script preserves everything cost-related and replaces text content with `[REDACTED]`. Use the same script for any new real-data fixtures.

## Development workflow

Follow the global policy in `~/.claude/CLAUDE.md`: issue → branch → implement+test → PR. Never commit directly to `main`. Every bug fix and behaviour change must include tests — LLM-boundary tests must assert the **content** passed to the mock, not just that the call happened (see global CLAUDE.md).

Progress is tracked in `docs/plans/2026-04-21-initial-implementation.md`. Review findings live in `docs/reviews/`.
