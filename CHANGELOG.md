# Changelog

## [Unreleased]

### Fixed

- Cost under-counting on sessions using the 1-hour prompt cache TTL (default on Max subscriptions since Claude Code 2.1.108). Cache-creation tokens are now priced per-TTL: 5-minute tokens at the 1.25× input rate, 1-hour tokens at the 2.0× input rate. Previously the entire `usage.cache_creation_input_tokens` total was charged at the 5m rate, under-counting 1h cache-creation cost by ~37%. The bucket-attribution hard invariant continues to hold; reported totals now match what Anthropic actually bills.

### Added

- `usage.speed` capture (`standard` / `fast`) — precursor to fast-mode pricing. No pricing branch yet; field is captured so it's available when fast-mode rates land in LiteLLM.
- Pydantic model for `usage.cache_creation` sub-object (`ephemeral_1h_input_tokens` / `ephemeral_5m_input_tokens`).
- `claude-opus-4-6`, `claude-opus-4-7`, `claude-sonnet-4-6` entries in `pricing.fallback_hardcoded`.

### Changed

- Schema migrated v4 → v5: adds `messages.cache_creation_1h`, `messages.cache_creation_5m`, `messages.speed` columns. First command after upgrade triggers a one-shot full re-reconcile to cold-backfill the new columns. **Sessions with non-zero 1h cache-creation tokens will see higher reported `cost_usd` after backfill** — this is correcting the under-count, not a regression.
- `pricing.ModelPrice` gains `cache_creation_1h_cost` field. LiteLLM resolver reads `cache_creation_input_token_cost_above_1hr` with `input_cost * 2.0` fallback for entries that omit it.
- `pricing.compute_message_cost` accepts optional `cache_creation_1h` / `cache_creation_5m` kwargs. Legacy single-total calls fall back to the 5m rate, preserving prior cost semantics for transcripts that don't carry the TTL split.

## [0.2.0] — 2026-05-05

### Added

- `ccforensics thrash` — model-misuse detection for Sonnet/Haiku sessions where a higher-tier model would plausibly have been cheaper. Ten typed signal extractors (novelty_window, test_regression, repeated_edit, repeated_error, placeholder_emit, user_correction, trajectory_length_zscore, tool_arg_churn, turn_cost_acceleration, session_abandoned) plus a composite scorer. Both gates required to flag (composite score >= 0.40 AND >= 2 distinct signal types). Counterfactual cost ranges anchored on observed user-driven escalation events (model_switch, subagent_dispatch); auto_mode events tagged but excluded from calibration. Confidence tiers (low/mid/high) narrow the multiplicative range as more events accumulate; per-session sanity gate suppresses implausible estimates. `--evidence` expands signal payloads, `--session ID` drills into a specific session bypassing flag gates, `--json` / `--csv` for export.
- `ccforensics tools` — per-tool / per-MCP-server spend with honest isolated/shared accounting. `--detail` drills into individual MCP tools; `--top` clamps; `--sort {isolated_cost,invocations,shared_exposure}` sorts. Isolated cost is exact; shared exposure is an upper bound and labeled as such.
- Cache efficiency + cache savings on `session show` and `aggregate` — cost-weighted ratio plus `saved $X.XX` line, both exact arithmetic over stored values.
- `service_tier` capture on every message (precursor to fast-mode pricing). `session show` and `aggregate` render a breakdown line only when a non-standard tier is present.

### Changed

- Schema migrated v3 → v4 (adds `session_rollups.thrash_score`, `session_rollups.thrash_score_version`, `session_rollups.escalation_event` JSON, plus new `session_signals` table keyed by `(session_id, signal_type)`). First command after upgrade triggers a one-shot full re-reconcile to cold-backfill the new columns/table.
- Schema migrated v2 → v3 (adds `messages.service_tier` and a new `message_tool_uses` table). First command after upgrade triggers a one-shot full re-reconcile to cold-backfill the new columns/table.
- `aggregate --json` output is now an envelope `{rows: [...], cache_*: ..., service_tier_breakdown: {...}}` rather than a bare list of rows. Existing JSON consumers must read `payload["rows"]` instead of indexing the top-level array.

### Caveats / known limitations (thrash)

- Thresholds + signal weights are intuition-tuned. The labeled-set validation spike (precision/recall against hand-labeled corpus) is deferred to a follow-up — see `docs/specs/2026-05-05-thrash-detection-design.md` §6.
- Counterfactual is "what user experienced when they did escalate", not "what would have happened on a cold-start Opus session". Cache priming + selection bias may understate Opus cost on equivalent fresh sessions.
- `user_correction` regex is English-only; non-English correction signals silently miss.
- Signal version recorded per row (`session_signals.signal_version` + `session_rollups.thrash_score_version`) so future threshold changes can detect drift.

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
