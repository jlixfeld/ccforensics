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

# Per-tool / per-MCP-server spend (last 30 days)
ccforensics tools --since 30d
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

### `tools`

Per-tool / per-MCP-server spend with honest isolated/shared accounting.

```
ccforensics tools [--session SPEC] [--since D] [--until D] [--project P]
                  [--detail] [--top N] [--sort isolated_cost|invocations|shared_exposure]
                  [--json | --csv] [--no-refresh]
```

Default render is server-rolled — every `mcp__<server>__*` tool collapses to a single row per server. Pass `--detail` to expand to per-tool rows.

`Isolated $` is exact (turns where this tool was the only one emitted). `Shared $≤` is an upper bound (turns where this tool ran alongside others; the same turn's cost appears under each sibling — never sum across rows).

### `index rebuild` / `index stats`

```
ccforensics index rebuild [--force] [--yes]   # incremental by default; --force re-parses everything
ccforensics index stats                        # row counts + last-refresh
```

## Sample workflows

These walkthroughs use real questions a heavy Claude Code user might ask. Each one names a specific command sequence and explains how to read the output.

### "Where did my last expensive session's $109 go?"

Find the session, then drill in:

```bash
ccforensics session list --sort cost --limit 5
ccforensics session show <id>          # full id, >=6-char prefix, or .jsonl path
```

`session show` renders five blocks:

1. **Session header** — duration, turns, total cost, models seen, compaction count.
2. **Cost by bucket** — `main` vs `subagent:<type>` vs `auto-compact` vs `unattributed`. This is the most important table. If `subagent:pr-review-toolkit:code-reviewer` is `$78` of the `$109`, the PR review tool earned its keep — or didn't.
3. **Cost by plugin** — same numbers re-rolled up to the owning plugin (skips the `subagent:` prefix). Use this when you want the bottom line per plugin install.
4. **Cost by model** — separates Opus from Sonnet from Haiku spend. A subagent that should be running on Sonnet but lands on Opus is visible here.
5. **Skill activations** — every skill the session loaded, when, by which channel, content size in bytes.

The `Cache:` footer line under the bucket table tells you whether prompt caching paid for itself in this session — see "Is my prompt cache working" below.

`Σ buckets == total session cost` is a hard invariant. If the numbers don't add up the index is corrupt — `ccforensics index rebuild --force` rebuilds from scratch.

### "Which plugin should I uninstall?"

```bash
ccforensics plugins --since 90d
```

Reads as a leaderboard: cost, sessions touched, top subagent type, top skill, first/last seen. Three patterns to look for:

- **High cost, low session count** — one runaway session blew up the average. Cross-reference with `ccforensics session list --since 90d --sort cost` to find it.
- **High cost, high session count, recent** — actively used and expensive. Worth keeping if it's earning its keep; check whether the work it does is something you'd happily pay for at $X per task.
- **Low cost, last seen weeks ago** — uninstall candidate. The plugin is loaded but you stopped using it.

`subagent:pr-review-toolkit:code-reviewer` and similar verbose subagent types roll up to their owning plugin in the `source` column. User-level `~/.claude/agents/<name>.md` files appear as `user-level`.

### "Are my MCP servers paying for themselves?"

```bash
ccforensics tools --since 30d --sort isolated_cost
```

Default render is server-rolled — every `mcp__<server>__*` tool collapses to one row per server. Two columns matter:

- **`Isolated $`** — exact cost of turns where this tool was the *only* one the assistant emitted. This is the cleanest signal of "what does this tool cost me when I use it." Sum freely across rows.
- **`Shared $≤`** — upper bound for turns where this tool ran alongside other tools in the same assistant response. The same turn's cost shows up under each sibling tool. **Never sum the Shared $ column across rows** — you'll double-count. Use it as a per-tool ceiling, not a total.

To see which specific MCP tools inside a server are pulling weight, drop the rollup:

```bash
ccforensics tools --since 30d --detail --sort invocations
```

Now `mcp__stratplaybook` expands to `mcp__stratplaybook__query`, `mcp__stratplaybook__build`, etc. A server with one heavy-traffic tool and ten near-zero ones is a candidate for trimming the unused tool definitions out of the MCP config — Claude Code injects every tool's schema into context on every turn, so unused tools cost real tokens.

Scope to one session if you want to see what tools a specific session used:

```bash
ccforensics tools --session 1dbce6d7 --detail
```

### "Is my prompt cache working?"

`session show` and `aggregate` both surface a cache footer line:

```
Cache: 12.4M read · 1.2M created · 87.3% efficiency · saved $3.42
```

Read the four numbers:

- **`12.4M read`** — total tokens served from the cache. High = good, prompt prefixes are stable enough to hit.
- **`1.2M created`** — total tokens written into the cache. Cache writes cost ~25% more than ordinary input, so high-create-low-read is a bad signal: you're paying the write premium without amortizing it.
- **`87.3% efficiency`** — **cost-weighted** ratio of cache-read cost vs total token cost. Token-ratio efficiency would overstate the dollar savings (cache reads cost ~10× less than fresh input), so this number is honest.
- **`saved $3.42`** — `cache_read × (input_price − read_price)` summed per model. What the cache actually saved you in dollars vs running the same prompts uncached.

Aggregate cache stats across a window:

```bash
ccforensics aggregate --since 30d
```

Same footer line, scoped to the window. If the efficiency dips after a known prompt-prefix change (e.g. you swapped a system prompt or installed a new plugin that injects into every turn), the drop tells you the new prefix is invalidating the cache.

### "What changed last week?"

```bash
ccforensics aggregate --since 7d --group-by day
ccforensics aggregate --since 7d --group-by project
ccforensics aggregate --since 7d --group-by model
ccforensics plugins   --since 7d
```

Run all four and look for the outlier. Day groups surface the spike day; project groups isolate the project that caused it; model groups separate Opus blowups from Sonnet workhorses; plugin rollup tells you which plugin owned the cost.

Combine `--model` with `--group-by project` to ask "which project burned the most Opus":

```bash
ccforensics aggregate --since 30d --model opus --group-by project
```

The `--model` filter is a case-insensitive substring match on the model column (so `opus` matches `claude-opus-4-7`). Cost is the per-model slice, not whole-session cost.

### Exporting for spreadsheets / further analysis

Every report supports `--json` and `--csv` (mutually exclusive). The CLI is the canonical source — JSON output is suitable for piping into `jq` or your own scripts; CSV is row-shaped for spreadsheets.

```bash
ccforensics tools --since 30d --csv > tools.csv
ccforensics aggregate --since 30d --json | jq '.cache_savings_usd'
ccforensics plugins --since 90d --json
```

Note: `aggregate --json` returns an envelope `{rows: [...], cache_*: ..., service_tier_breakdown: {...}}`. Read `payload["rows"]`, not the top-level array.

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

### Cache efficiency

`session show` and `aggregate` surface a cache footer line in scope:

```
Cache: 12.4M read · 1.2M created · 87.3% efficiency · saved $3.42
```

Both numbers are exact arithmetic over stored token counts and per-model pricing. `efficiency` is **cost-weighted** — `cache_read` tokens cost ~10× less than `input` tokens, so a token-ratio efficiency overstates dollar savings. `saved` is `cache_read × (input_price − read_price)` summed per model.

## Known limitations

- **Skill context-carry cost estimates are not yet computed.** Skill activations are detected and content size is measured, but the estimated cost band in the ledger is `NULL`.
- **Plugin-path matching requires `/plugins/cache/` in the path.** Non-standard install locations may not classify correctly.
- **~0.5% of subagent spawns are unresolvable** when the parent session rotated or had no `Agent`/`Task` call before the subagent's first message. Their cost lands in `unattributed`.
- **Fast-mode pricing not yet applied.** `service_tier` (`standard | priority | batch`) is captured from every assistant message and surfaced as a breakdown line on `session show` / `aggregate` when a non-standard tier is present. Per-tier pricing is deferred until a real fast-mode session is verifiable end-to-end.

## License

MIT.
