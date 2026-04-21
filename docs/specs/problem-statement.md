---
title: ccforensics — Problem Statement
date: 2026-04-21
status: normative
---

# Claude Code Usage Forensics Tool

## Problem

Existing Claude Code usage tools (ccusage, ccost, claude-devtools, lm-assist) provide session-level and turn-level cost breakdowns, but none answer the question I actually care about: **which of my installed plugins, skills, and subagents are driving my token costs, and are they worth what they cost?**

I run Claude Code with multiple plugins installed (Superpowers and others), plus custom skills and subagents defined at the user level. When a session costs $80, I want to know how that $80 breaks down across:

- Main agent work vs. subagent work
- Per named subagent (e.g., `code-reviewer`, `pr-review-loop`, `auto-review`, `Explore`)
- Per plugin (attributing agents and skills back to their owning plugin)
- Per skill activation (including skills injected via session-start hooks, not just Read-loaded ones)

And downstream: did the expensive subagent spawns and skill activations actually produce value, or were they overhead?

## Scope

### In scope

1. Parse all JSONL session files under `~/.claude/projects/<encoded-path>/*.jsonl`
2. Deduplicate messages by UUID (Claude Code sometimes writes the same UUID to multiple files during branching/resume — naive summing inflates totals)
3. Build a parent→child tree using `parentToolUseId` and `agentId` on subagent messages
4. Attribute token usage (input, output, cache-creation, cache-read) and cost to each node in the tree
5. Compute per-subagent-type aggregates (count of spawns, total cost, avg cost/spawn, model used)
6. Discover installed plugins by scanning `~/.claude/plugins/cache/*/` and build a registry mapping agent names and skill paths to their owning plugin
7. Roll up subagent cost to plugin-level using that registry
8. Detect skill activations in two ways:
   - Read tool calls where the path matches `**/skills/*/SKILL.md`
   - Session-start hook injections (system messages matching known bootstrap patterns — Superpowers' `<session-start-hook>` tag is one example)
9. Attribute a share of cache-read tokens to active skills (proportional to skill content size vs. total context, as a reasonable approximation — not claiming accounting precision)
10. Correlate skill activations with downstream Task spawns within N turns, as a best-effort heuristic for which skill caused which subagent work
11. Provide a session listing command for discovery — enough metadata to let a human identify the session they want to interrogate without opening raw JSONL
12. Output per-session reports and aggregate reports across a date range

### Out of scope

**Deterministic causal attribution for plugin hook scripts.** Plugins can ship hook scripts (SessionStart, PreToolUse, PostToolUse, etc.) that execute as shell/JS/Python and can decide — based on arbitrary logic — to emit Task calls, inject context, or trigger other work. The JSONL captures the *effect* (a Task call appears, a subagent runs) but not the *reason* (the hook script's internal decision logic).

The tool will correctly attribute cost to the subagent that ran and the plugin that owns it. The tool will **not** attempt to explain *why* a hook chose to fire by reading and interpreting plugin source code. That reasoning lives in files outside the JSONL, would require parsing arbitrary shell/JS/Python with unbounded external dependencies, and produces approximate answers for effort that doesn't pay off. If the user wants to know why a hook fired, they can read the hook script.

**Other explicit non-goals:**

- Measuring subagent *output quality* (whether the subagent's recommendations were correct)
- Real-time monitoring — this is a post-hoc analysis tool
- Web UI or dashboard — CLI with optional JSON/CSV export is sufficient
- Replacing ccusage / claude-devtools for the things they already do well

### Known ambiguities — document them, don't hide them

- Cache-read share attribution to skills is an estimate, not accounting. The report should show estimated ranges or confidence indicators, not false-precision dollar figures.
- When a name collides between a skill and a subagent (e.g., a `pr-review-loop` skill that also has a `pr-review-loop` subagent), prefer the subagent attribution (precise via `parentToolUseId`) and note the skill's context-carry cost separately.
- Double-attribution is worse than under-attribution. If the script can't confidently attribute a cost, bucket it as "unattributed main agent work" rather than guessing.

## Deliverables

1. A Python CLI tool, stdlib-only where feasible, minimal dependencies otherwise. Must be `uv tool install`-compatible.
2. Cost calculation uses LiteLLM's pricing data (as ccost does) rather than hardcoded rates, so it stays current as Anthropic pricing changes.
3. Commands:

### `forensics session --list`

List all discoverable sessions in a format a human can scan to pick the one they want to interrogate. None of the existing tools do this adequately — ccusage has UUIDs but no summaries, lm-assist uses first-user-prompt as summary, claude-code-log is closer but is a browsing UI not a scriptable CLI.

**Flags:**
- `--project <name>` — filter to one project
- `--since <date>` / `--until <date>` — date range filter
- `--grep <pattern>` — filter summaries by text match (case-insensitive substring)
- `--sort <cost|started|last-active|turns>` — default `last-active`
- `--reverse` — flip sort direction
- `--json` / `--csv` — export formats

**Output columns:**
- **Session UUID** — the identifier to pass to `forensics session <uuid>` for full analysis
- **Started** — timestamp of the first message in the session
- **Last active** — timestamp of the most recent message
- **Duration** — total wall-clock span (not tokens, not cost — time)
- **Project** — decoded project path (e.g., `Vulnerability-Assessment` rather than the url-encoded directory name)
- **Turns** — message count (rough proxy for session complexity)
- **Cost** — computed session cost (so you can sort or filter by it)
- **Summary** — one-line human-readable description, priority order:
  1. Claude-generated summary message in the JSONL if present (Claude Code writes these for `/resume`; look for summary-type messages or `isSummary` flags — take the most recent if multiple exist)
  2. First user prompt, truncated to ~80 chars, with newlines collapsed
  3. Fallback: `"<no summary available>"`

Truncate the displayed summary to a configurable width (default 80), but store the full summary in JSON/CSV exports.

**Default sort:** `last-active` descending (most recent first).

**Example output:**

```
UUID                                  Started        Last active    Dur    Project                    Turns  Cost      Summary
abc123de-4f56-...                     2026-04-16     2026-04-16     4h12m  Vulnerability-Assessment   247    $109.13   Initial scan and remediation plan for public internet assets
def456gh-7i89-...                     2026-04-17     2026-04-17     2h03m  Vulnerability-Assessment   98     $34.68    Follow-up on authentication findings, refactor auth middleware
```

### `forensics session <session-id-or-path>`

Detailed single-session report. Surface:

- Total session cost with model breakdown
- Subagent tree with cost per node and per `subagent_type`
- Plugin rollup (cost attributed to each installed plugin present in the session)
- Skill activation ledger (when activated, how long held in context, estimated cost share, downstream spawns correlated)
- "Unattributed main agent work" bucket with explanation of what fell into it

### `forensics aggregate --since <date> --until <date>`

Rollup across sessions in the window. Same attribution categories, summed.

### `forensics plugins`

Per-plugin cost rollup across all discoverable sessions. Answers "what has each installed plugin cost me, in aggregate?"

### Export

`--json` and `--csv` on all commands.

## Technical notes

- JSONL schema is documented across several community tools (ccusage, ccost, lm-assist). Reference those for field names rather than discovering them from scratch.
- Plugin install layout: `~/.claude/plugins/cache/<plugin-name>/` typically contains `agents/`, `skills/`, and a manifest. Read the manifest for authoritative name-to-plugin ownership.
- **Do not rely on `sessions-index.json`.** Claude Code maintains this index but it is known to go stale or corrupt (GitHub issue #29154 has community reports). The JSONL files on disk are the source of truth; the index is at best a hint. Scan and build the session list from JSONL directly.
- Claude Code JSONL format evolves. Write the parser defensively — unknown fields should not crash attribution; schema version drift should degrade gracefully to a warning rather than a failure.
- Deduplicate sessions by UUID across files (the same dedup rule applies to cost attribution and session listing).

## Success criteria

When I run this on my 2026-04-16 session that cost $109, I should be able to:

1. See the $109 broken down across main agent, each named subagent, and each installed plugin
2. See which skills were active for what portion of the session and their estimated context-carry cost
3. See whether expensive subagent spawns clustered around specific skill activations
4. Identify two specific changes to my workflow that would measurably reduce future costs without reducing the work getting done

When I run `forensics session --list` against my history, I should be able to:

1. Identify any past session I care about within 30 seconds, using the summary + date + project columns
2. Pipe the UUID of that session into `forensics session <uuid>` without copy-paste friction

The test of "done" for the overall tool: I run the reports, and I act on what they tell me.
