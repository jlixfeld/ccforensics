# ccforensics

Claude Code session forensics — plugin, skill, and subagent cost attribution.

**Status:** v0.1.0 in flight. See the [plan](docs/plans/2026-04-21-initial-implementation.md) for progress.

## What it does

Parses `~/.claude/projects/**/*.jsonl` and attributes every message's cost to exactly one bucket:

- `main` — assistant work in the primary session.
- `subagent:<type>` — work delegated via the `Agent`/`Task` tool (e.g. `subagent:pr-review-toolkit:code-reviewer`).
- `auto-compact` — Claude Code's internal context-compaction worker.
- `unattributed` — subagent files whose parent Agent/Task call can't be resolved.

**Hard invariant:** `sum(buckets) == total session cost` — enforced structurally by SQL, verified at real-corpus scale.

Subagent buckets roll up further into the owning plugin (from `~/.claude/plugins/cache/*/`), `user-level`, `builtin`, or `unknown`. Skill activations are detected via three channels (the `Skill` tool, `Read` of a `SKILL.md`, and `SessionStart` hook injection) and surfaced in a per-session ledger.

Answers: **"which of my installed plugins, skills, and subagents are driving my token costs, and are they worth what they cost?"**

## Install

Not yet published. When v0.1.0 ships:

```bash
uv tool install git+https://github.com/jlixfeld/ccforensics@v0.1.0
```

## Quick start

```bash
# Build or refresh the SQLite index (incremental by default)
ccforensics index rebuild

# Per-session list — most recent first
ccforensics session list --limit 20

# Deep report for one session (full id, >=6-char prefix, or .jsonl path)
ccforensics session show 1dbce6d7

# All-time cost by plugin
ccforensics plugins

# Last 30 days aggregated per project
ccforensics aggregate --since 30d --group-by project
```

## Commands

All commands refresh the index first by default; pass `--no-refresh` to skip. `--json` and `--csv` are mutually exclusive.

### `session list`

List sessions newest-first with summary, cost, duration, and turn count.

```
ccforensics session list [--project P] [--since D] [--until D]
                         [--grep PAT] [--sort cost|started|last-active|turns]
                         [--reverse] [--limit N]
                         [--json | --csv] [--no-refresh]
```

### `session show <spec>`

Deep per-session report: header, cost by bucket, cost by plugin, (optional) unattributed detail, skill activations, parse notes.

```
ccforensics session show <spec> [--include-unattributed]
                                [--json | --csv] [--no-refresh]
```

`<spec>` is a full session id, a prefix of ≥6 characters, or an absolute path to a session JSONL file.

### `aggregate`

Cost totals over a window, optionally grouped.

```
ccforensics aggregate [--since D] [--until D] [--project P]
                      [--group-by none|project|day|week|month|plugin]
                      [--json | --csv] [--no-refresh]
```

### `plugins`

Per-plugin rollup with top subagent type, top skill, and first/last seen.

```
ccforensics plugins [--since D] [--until D] [--json | --csv] [--no-refresh]
```

### `index rebuild` / `index stats`

```
ccforensics index rebuild [--force] [--yes]   # incremental by default
ccforensics index stats                        # row counts + last-refresh
```

`--force` drops and re-parses from scratch.

## Date formats

All `--since` / `--until` accept:

- `YYYY-MM-DD` — absolute date.
- `Nd` — N days ago (e.g. `30d`, `7d`).
- `today`, `yesterday`.

## How it works

The index lives at `~/.cache/ccforensics/index.sqlite` (or the platform equivalent) and contains:

- `files` — one row per JSONL file, reconciled by `(path, mtime, size)`.
- `messages` — dedup-collapsed per `(message.id, requestId)` with content-richest-wins tiebreak.
- `subagent_spawns` — one row per subagent JSONL, linked to its parent `Agent`/`Task` tool_use.
- `session_summaries` — per-session header data (project, total, summary).
- `session_rollups` — per-(session, bucket) token + cost totals.
- `skill_activations` — every detected skill load, with source and content size.
- `plugins` / `user_level_artifacts` — registry of installed plugin versions and user-level skills/agents.

Pricing is pulled from LiteLLM's [model_prices_and_context_window.json](https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json) once per 24h, cached on disk. Per-token cost = `input + output + cache_creation + cache_read`, matching ccusage within ±1% on overlapping sessions (SC2).

## Known limitations

- **Skill context-carry ± band is not yet computed.** Skill activations are detected, content size is measured, but `estimated_cost_usd` in the ledger is `NULL`. Deferred to M8.2.
- **Plugin-path match requires `/plugins/cache/` in the path.** Non-standard install locations may not classify correctly.
- **~0.5% of subagent spawns are unresolvable** (parent session rotated or no Agent/Task call before `ts_spawned`). Their cost lands in `unattributed`.

## Design + plan

- [Problem statement](docs/specs/problem-statement.md)
- [Design specification](docs/specs/design.md)
- [Initial implementation plan](docs/plans/2026-04-21-initial-implementation.md)

## License

MIT.
