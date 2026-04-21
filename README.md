# ccforensics

Claude Code session forensics — plugin, skill, and subagent cost attribution.

**Status:** in development (v0.1.0 in flight).

## What it does

Parses `~/.claude/projects/**/*.jsonl` to attribute session cost to:

- Main agent vs. subagent work (per named `subagent_type`)
- Each installed plugin (`~/.claude/plugins/cache/*/`)
- Each skill activation (Read, `Skill` tool, SessionStart hook injection) with context-carry ± band
- An "unattributed main agent work" bucket for anything the tool can't confidently place

Answers the question: **"which of my installed plugins, skills, and subagents are driving my token costs, and are they worth what they cost?"**

## Design + plan

- [Problem statement](docs/specs/problem-statement.md)
- [Design specification](docs/specs/design.md)
- [Initial implementation plan](docs/plans/2026-04-21-initial-implementation.md)

## Install

Not yet published. When v0.1.0 ships:

```bash
uv tool install git+https://github.com/jlixfeld/ccforensics@v0.1.0
```

## License

MIT.
