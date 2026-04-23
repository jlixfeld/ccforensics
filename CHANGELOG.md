# Changelog

## [0.1.0] — unreleased

First public release.

### Commands

- `ccforensics index rebuild` / `index stats` — incremental SQLite index with `--force` full rebuild.
- `ccforensics session list` — newest-first session summary with filters, sorting, and JSON/CSV export.
- `ccforensics session show <spec>` — deep per-session report: header, cost by bucket, cost by plugin, skill activations, parse notes. `--include-unattributed` expands the detail list.
- `ccforensics aggregate` — cost totals over a window, groupable by `none | project | day | week | month | plugin`.
- `ccforensics plugins` — per-plugin rollup with top subagent type, top skill, first/last seen.

### Attribution model

- Every message classified into exactly one of `main`, `subagent:<type>`, `auto-compact`, `unattributed`.
- Hard invariant: `sum(rollups) == sum(messages.cost_usd)` per session (SQL-driven, verified 12,371/12,371 on the author's corpus).
- Subagent spawn linkage: nearest-before Agent|Task heuristic with `(type_match, timestamp)` composite rank — 99.5% resolution rate on the real corpus.
- Auto-compaction workers (`agent-acompact-*.jsonl`) get their own bucket — real billable cost, not silently routed to `unattributed`.

### Skill detection

- All three channels (spec §4.3): `Skill` tool call, `Read` of a `SKILL.md`, SessionStart hook injection with frontmatter fallback.
- Attribution anchored to the full `SKILL.md` path so name collisions don't confuse plugin vs. user-level.
- Context-carry ± cost band (spec §4.4) **not yet computed** — `estimated_cost_usd` is NULL in the ledger. Deferred to a follow-up release.

### Plugin registry

- Discovers plugins from `~/.claude/plugins/cache/*/<plugin>/<version>/` and user-level artifacts from `~/.claude/{skills,agents}/`.
- Multiple installed versions collapse to the highest semver.
- Name collisions between user-level and any plugin fire a warning at reconcile time.

### Notable design decisions

- **Dedup policy** on JSONL streamed-write collisions: content-richest wins (tool_use blocks first, then block count, then latest timestamp). Prior "earliest wins" rule was silently dropping streamed tool_use blocks when Claude Code emitted an empty-shell intermediate state first. Token usage is identical across duplicates so cost is unaffected.
- **Spawn → messages linkage** via recomputed dedup_key from the raw parent entry, not the stored `uuid` or `tool_use_id` columns. Handles the case where one LLM response emits N parallel Agent tool_uses sharing a dedup_key; the messages row only has column space for one tool_use_id.
- **Pyhton pin at 3.13** — the Claude Code project lives under `~/Documents/` which gets iCloud-synced; iCloud sets `UF_HIDDEN` on uv's `_editable_impl_<pkg>.pth`, which `site.py` then silently skips. Python 3.13 handles this no worse than 3.14 did, but the root cause is environmental (see `tests/conftest.py` for the test-level workaround).

### Known limitations

- **Skill context-carry ± band not yet computed.** See above.
- **~0.5% of subagent spawns unresolvable** — parent sessions rotated or no Agent/Task call before `ts_spawned`. Cost routed to `unattributed` per spec.
- **Non-standard install paths** (not under `/plugins/cache/`) may miss plugin classification for imported subagent_type strings.

### Platform

- Python 3.13 required.
- `uv`-managed. Entrypoint: `ccforensics`.
