# ccforensics

Claude Code session forensics — plugin, skill, and subagent cost attribution.

---

## Why this exists

I had a Claude Code session that cost $109. I had no idea where that money went.

The standard cost tools — ccusage, ccost, claude-devtools — gave me a total and a per-session breakdown. What they couldn't tell me was *which of my installed plugins, skills, and subagents were responsible for that $109*, and whether they were worth it.

I run Claude Code with a lot of plugins: Superpowers, auto-review, pr-review-toolkit, feature-dev, and others. When I send one message and ask Claude to "review this PR," what actually happens is a cascade: a parent session spawns a `pr-review-toolkit:code-reviewer` subagent, which spawns a `pr-review-toolkit:code-simplifier`, each loading skills into context and making independent LLM calls. The $109 is the sum of all of that. Existing tools see each subagent's JSONL file as an independent mystery session with no name and no parent.

The closest thing to an answer I could find was ccusage's documentation: *"Per-agent cost attribution is not available from JSONL data."* That turned out to be wrong — but understandably so. They're reading each session file in isolation. The data is there; it's just spread across multiple files and requires walking the parent→child spawn tree to connect them.

Claude Code writes each subagent as a **separate JSONL file** in `<session>/subagents/agent-<hex>.jsonl`. That file contains every LLM message the subagent made, with full token counts. The subagent's cost is the sum of its own file. The hard problem isn't counting the cost — it's linking that file back to the parent session's `Agent`/`Task` tool_use call, identifying the subagent type from the call's input or the sibling `meta.json`, and resolving which installed plugin owns that agent type.

Once you have that chain, you can answer the questions that actually matter: **which plugins are driving my spend, are they earning it, and what would I need to change to reduce costs without losing value?**

That's what ccforensics does.

---

## Install

```bash
uv tool install git+https://github.com/jlixfeld/ccforensics
```

Requires Python 3.13+.

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

Deep per-session report: cost by bucket, cost by plugin, skill activations, parse notes. Optionally include unattributed spawn detail.

```
ccforensics session show <spec> [--include-unattributed]
                                [--json | --csv] [--no-refresh]
```

`<spec>` accepts a full session id, a prefix of ≥6 characters, or an absolute path to a session JSONL file.

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
ccforensics index rebuild [--force] [--yes]   # incremental by default; --force re-parses everything
ccforensics index stats                        # row counts + last-refresh
```

## Date formats

All `--since` / `--until` accept:

- `YYYY-MM-DD` — absolute date.
- `Nd` — N days ago (e.g. `30d`, `7d`).
- `today`, `yesterday`.

## How it works

The index lives at `~/.cache/ccforensics/index.sqlite` and is incrementally reconciled on each run — only files that have changed since the last run are re-parsed. A no-change reconcile completes in under a second on large histories.

Every message's cost is attributed to exactly one bucket:

- `main` — assistant work in the primary session.
- `subagent:<type>` — work delegated via the `Agent`/`Task` tool (e.g. `subagent:pr-review-toolkit:code-reviewer`).
- `auto-compact` — Claude Code's internal context-compaction worker.
- `unattributed` — subagent files whose parent Agent/Task call can't be resolved (~0.5% on real corpus).

`sum(buckets) == total session cost` is a hard invariant enforced structurally by SQL.

Subagent buckets roll up to the owning plugin via a scan of `~/.claude/plugins/cache/*/`. Skill activations are detected via three channels — the `Skill` tool, `Read` of a `SKILL.md`, and `SessionStart` hook injection — and surfaced in a per-session ledger.

### Cross-file subagent linkage

For each subagent JSONL at `<session>/subagents/agent-<hex>.jsonl`:

1. Read the sibling `agent-<hex>.meta.json` for the authoritative `agentType`.
2. Scan the parent session for `Agent`/`Task` tool_use blocks emitted before the subagent's first message.
3. Rank candidates by `(type_match, timestamp)` — prefer the nearest-before call whose `input.subagent_type` matches the meta type.
4. That parent tool_use ID becomes the attribution anchor; the plugin registry resolves which plugin owns it.

Pricing is pulled from LiteLLM's model pricing data once per 24h, cached on disk, with a hardcoded fallback for current Claude models if the network is unavailable.

## Known limitations

- **Skill context-carry cost estimates are not yet computed.** Skill activations are detected and content size is measured, but the estimated cost band in the ledger is `NULL`.
- **Plugin-path matching requires `/plugins/cache/` in the path.** Non-standard install locations may not classify correctly.
- **~0.5% of subagent spawns are unresolvable** when the parent session rotated or had no `Agent`/`Task` call before the subagent's first message. Their cost lands in `unattributed`.

## License

MIT.
